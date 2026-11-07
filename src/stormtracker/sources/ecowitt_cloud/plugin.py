"""Ecowitt.net cloud API source (``/api/v3/device/real_time``).

Use this when the console's single "Customized" upload slot is already taken: the device keeps
uploading to Ecowitt.net as it does now and this plugin reads the device's latest values back with
your own application key + API key. Cost: the data is as fresh as the device's Ecowitt.net upload
interval (typically 1-5 min) and depends on the cloud being up.

Every value comes back as ``{"time": epoch, "unit": "...", "value": "..."}``; values are converted by
the unit string the API reports, so the result is correct whatever unit ids were requested. A value
whose unit is not recognised is skipped (and counted in health detail), never guessed.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from pydantic import BaseModel, Field

from ...models import Normalised, Observation
from ...plugins import Health, HealthState, Licence, RateLimit, Source, SourceMeta, secret_field
from ...timeutil import utcnow
from ..ecowitt.mapping import CONV, _num, _slug

API_URL = "https://api.ecowitt.net/api/v3/device/real_time"
# Ask for metric so the unit strings we convert from are the identity case. Ids are from Ecowitt's
# API v3 docs; if one is wrong the unit string in the reply still drives a correct conversion.
UNIT_PARAMS = {"temp_unitid": 1, "pressure_unitid": 3, "wind_speed_unitid": 6,
               "rainfall_unitid": 12, "solar_irradiance_unitid": 16}
STALE_AFTER = timedelta(minutes=30)

# lower-cased unit text -> converter name (see ecowitt.mapping.CONV)
UNIT_CONV = {
    "℃": "id", "°c": "id", "c": "id", "℉": "f2c", "°f": "f2c", "f": "f2c",
    "hpa": "id", "mbar": "id", "inhg": "inhg2hpa", "mmhg": "mmhg2hpa",
    "m/s": "id", "km/h": "kmh2ms", "kph": "kmh2ms", "mph": "mph2ms", "knots": "kn2ms", "kn": "kn2ms",
    "mm": "id", "in": "in2mm", "mm/hr": "id", "mm/h": "id", "in/hr": "in2mm", "in/h": "in2mm",
    "w/m2": "id", "w/m²": "id", "km": "id", "mi": "mi2km",
    "%": "id", "": "id", "°": "id", "º": "id", "µg/m3": "id", "μg/m3": "id", "ug/m3": "id", "µg/m³": "id",
    "μg/m³": "id",
}

# (group, leaf) -> standard key; ``{n}`` is the channel number for numbered groups.
EXACT: dict[tuple[str, str], str] = {
    ("outdoor", "temperature"): "temp_c", ("outdoor", "feels_like"): "feels_like_c",
    ("outdoor", "app_temp"): "apparent_temp_c", ("outdoor", "dew_point"): "dewpoint_c",
    ("outdoor", "humidity"): "humidity_pct",
    ("indoor", "temperature"): "temp_in_c", ("indoor", "humidity"): "humidity_in_pct",
    ("solar_and_uvi", "solar"): "solar_wm2", ("solar_and_uvi", "uvi"): "uv_index",
    ("wind", "wind_speed"): "wind_speed_ms", ("wind", "wind_gust"): "wind_gust_ms",
    ("wind", "wind_direction"): "wind_dir_deg",
    ("pressure", "relative"): "pressure_hpa", ("pressure", "absolute"): "pressure_abs_hpa",
    ("lightning", "distance"): "lightning_distance_km", ("lightning", "count"): "lightning_count",
}
RAIN_LEAVES = {"rain_rate": "rain_rate_mmh", "event": "rain_event_mm", "hourly": "rain_hourly_mm",
               "daily": "rain_daily_mm", "weekly": "rain_weekly_mm", "monthly": "rain_monthly_mm",
               "yearly": "rain_yearly_mm"}
for _leaf, _key in RAIN_LEAVES.items():
    EXACT[("rainfall", _leaf)] = _key
    EXACT[("rainfall_piezo", _leaf)] = _key.replace("_mm", "_piezo_mm")   # rain_rate_mmh -> rain_rate_piezo_mmh

CHANNEL: list[tuple[re.Pattern[str], dict[str, str]]] = [
    (re.compile(r"^temp_and_humidity_ch(\d+)$"), {"temperature": "temp_c_{n}", "humidity": "humidity_pct_{n}",
                                                  "dew_point": "dewpoint_c_{n}", "feels_like": "feels_like_c_{n}"}),
    (re.compile(r"^soil_ch(\d+)$"), {"soilmoisture": "soil_moisture_pct_{n}"}),
    (re.compile(r"^pm25_ch(\d+)$"), {"pm25": "pm25_ugm3_{n}"}),
    (re.compile(r"^leaf_ch(\d+)$"), {"leaf_wetness": "leaf_wetness_pct_{n}"}),
    (re.compile(r"^temp_ch(\d+)$"), {"temperature": "probe_temp_c_{n}"}),
]
SKIP_GROUPS = {"battery"}


class EcowittCloudConfig(BaseModel):
    application_key: str | None = secret_field(
        "Ecowitt.net application key (ecowitt.net -> Profile -> API keys).", required=True)
    api_key: str | None = secret_field("Ecowitt.net API key (same page as the application key).", required=True)
    mac: str = Field(..., pattern=r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$",
                     description="device MAC address as shown under Devices on ecowitt.net, e.g. AA:BB:CC:DD:EE:FF")
    station_id: str = Field("ecowitt_cloud", pattern=r"^[A-Za-z0-9_.-]{1,40}$", description="id stored with observations")
    location_id: str | None = Field(None, description="id of the configured location the station is at (default: first)")


def _convert(value: Any, unit: str | None) -> float | None | bool:
    """-> float, or None for non-numeric, or False when the unit is unknown (skip, don't guess)."""
    n = _num(value)
    if n is None:
        return None
    conv = UNIT_CONV.get((unit or "").strip().lower())
    return False if conv is None else round(CONV[conv](n), 4)


def convert_realtime(data: dict[str, Any]) -> tuple[dict[str, float], datetime | None, int]:
    """``data`` object of a real_time reply -> (values, newest leaf timestamp, skipped-for-unit count)."""
    values: dict[str, float] = {}
    newest: float | None = None
    skipped = 0
    for group, leaves in data.items():
        if group in SKIP_GROUPS or not isinstance(leaves, dict):
            continue
        for leaf, item in leaves.items():
            if not isinstance(item, dict) or "value" not in item:
                continue
            key = EXACT.get((group, leaf))
            if key is None:
                for rx, table in CHANNEL:
                    m = rx.match(group)
                    if m and leaf in table:
                        key = table[leaf].format(n=m.group(1))
                        break
            if key is None:
                key = f"x_{_slug(group)}_{_slug(leaf)}"
            v = _convert(item.get("value"), item.get("unit"))
            if v is False:
                skipped += 1
                continue
            if v is None:
                continue
            values[key] = v
            t = _num(item.get("time"))
            if t and (newest is None or t > newest):
                newest = t
    return values, (datetime.fromtimestamp(newest, UTC) if newest else None), skipped


class EcowittCloud(Source):
    meta = SourceMeta(
        name="ecowitt_cloud",
        category="observations",
        description="Your own Ecowitt station read back from the Ecowitt.net cloud API. Use when the console's "
                    "Customized upload slot is already in use.",
        poll_interval_s=60,
        rate_limit=RateLimit(max_requests=2, period_s=60),
        licence=Licence(type="personal", notes="Your own device's data via your own Ecowitt.net API keys "
                                           "(owner access only). Keep to personal use."),
    )
    ConfigModel = EcowittCloudConfig
    cfg: EcowittCloudConfig

    def __init__(self, cfg, ctx):
        super().__init__(cfg, ctx)
        self._latest: datetime | None = None

    async def fetch(self) -> dict[str, Any]:
        params = {"application_key": self.cfg.application_key, "api_key": self.cfg.api_key,
                  "mac": self.cfg.mac, "call_back": "all", **UNIT_PARAMS}
        r = await self.ctx.http.get(API_URL, params=params, timeout=20)
        r.raise_for_status()
        j = r.json()
        if j.get("code") != 0:
            raise RuntimeError(f"Ecowitt API error code {j.get('code')}: {j.get('msg')}")
        return {"json": j, "received": utcnow().isoformat(), "location": await self._location()}

    def normalise(self, raw: dict[str, Any]) -> list[Normalised]:
        values, ts, skipped = convert_realtime(raw["json"].get("data") or {})
        self._latest = ts
        self.ctx.state.detail.update(latest_data_time=ts.isoformat() if ts else None, skipped_unknown_unit=skipped)
        if not values:
            return []
        loc = raw["location"]
        return [Observation(source=self.meta.name, station_id=self.cfg.station_id, lat=loc["lat"], lon=loc["lon"],
                            ts=ts or datetime.fromisoformat(raw["received"]), values=values)]

    async def _location(self) -> dict[str, Any]:
        return await self.location(self.cfg.location_id)

    async def health(self) -> Health:
        h = self.ctx.state
        # The cloud keeps returning the last values when the device stops uploading; surface that.
        if h.state == HealthState.OK and self._latest and utcnow() - self._latest > STALE_AFTER:
            h.state = HealthState.DEGRADED
            h.last_error = (f"newest reading is {int((utcnow() - self._latest).total_seconds() // 60)} min old: "
                            "the device is not uploading to Ecowitt.net")
        return h
