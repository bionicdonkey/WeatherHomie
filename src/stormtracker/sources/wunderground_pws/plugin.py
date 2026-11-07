"""Weather Underground personal-weather-station read-back (``api.weather.com`` PWS current conditions).

Use this when the console already uploads to Weather Underground (a built-in upload slot, separate
from "Customized") and you hold the PWS-contributor API key from your WU account. The plugin reads
your *own* station's latest observation back; it does not read other people's stations.

Notes: WU returns nothing (HTTP 204) when a station has not reported in the last hour, and the data is
only as fresh as the station's upload interval. The API key rides in the query string, so errors are
redacted centrally by the scheduler and httpx request logging is silenced in ``__main__``.
Check Weather Underground's current API terms before relying on this: the key is issued to PWS
contributors, and this plugin is for personal use of your own station's data only.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field

from ...models import Normalised, Observation
from ...plugins import Health, HealthState, Licence, RateLimit, Source, SourceMeta, secret_field
from ...timeutil import utcnow
from ..ecowitt.mapping import CONV, _num

API_URL = "https://api.weather.com/v2/pws/observations/current"
STALE_AFTER = timedelta(minutes=30)

# metric block (units=m): key -> (standard key, converter)
METRIC = {
    "temp": ("temp_c", "id"), "dewpt": ("dewpoint_c", "id"), "heatIndex": ("heat_index_c", "id"),
    "windChill": ("windchill_c", "id"), "windSpeed": ("wind_speed_ms", "kmh2ms"),
    "windGust": ("wind_gust_ms", "kmh2ms"), "pressure": ("pressure_hpa", "id"),
    "precipRate": ("rain_rate_mmh", "id"), "precipTotal": ("rain_daily_mm", "id"),
}
TOP = {"humidity": ("humidity_pct", "id"), "winddir": ("wind_dir_deg", "id"),
       "uv": ("uv_index", "id"), "solarRadiation": ("solar_wm2", "id")}


class WundergroundPwsConfig(BaseModel):
    api_key: str | None = secret_field("Weather Underground API key for PWS contributors.", required=True)
    wu_station_id: str = Field(..., pattern=r"^[A-Za-z0-9_-]{3,40}$",
                               description="your PWS id on Weather Underground, e.g. IABC123")
    station_id: str = Field("wu_pws", pattern=r"^[A-Za-z0-9_.-]{1,40}$", description="id stored with observations")
    location_id: str | None = Field(None, description="id of the configured location the station is at (default: first)")


def convert_observation(o: dict[str, Any]) -> dict[str, float]:
    out: dict[str, float] = {}
    for src, (key, conv) in TOP.items():
        v = _num(o.get(src))
        if v is not None:
            out[key] = round(CONV[conv](v), 4)
    for src, (key, conv) in METRIC.items():
        v = _num((o.get("metric") or {}).get(src))
        if v is not None:
            out[key] = round(CONV[conv](v), 4)
    return out


class WundergroundPws(Source):
    meta = SourceMeta(
        name="wunderground_pws",
        category="observations",
        description="Your own station read back from Weather Underground. Use when you upload to WU but the "
                    "console's Customized slot is taken.",
        poll_interval_s=120,
        rate_limit=RateLimit(max_requests=2, period_s=120),   # well inside WU's PWS key limits
        licence=Licence(type="personal", notes="PWS-contributor API key; your own station only, personal use. "
                                                 "Review Weather Underground's API terms."),
    )
    ConfigModel = WundergroundPwsConfig
    cfg: WundergroundPwsConfig

    def __init__(self, cfg, ctx):
        super().__init__(cfg, ctx)
        self._latest: datetime | None = None
        self._empty = False

    async def fetch(self) -> dict[str, Any]:
        r = await self.ctx.http.get(API_URL, timeout=20, params={
            "stationId": self.cfg.wu_station_id, "format": "json", "units": "m", "apiKey": self.cfg.api_key})
        loc = await self._location()
        received = utcnow().isoformat()
        if r.status_code == 204 or not r.content:
            return {"json": None, "received": received, "location": loc}
        r.raise_for_status()
        return {"json": r.json(), "received": received, "location": loc}

    def normalise(self, raw: dict[str, Any]) -> list[Normalised]:
        obs = ((raw["json"] or {}).get("observations") or [])
        self._empty = not obs
        if not obs:
            return []
        o = obs[0]
        epoch = _num(o.get("epoch"))
        ts = datetime.fromtimestamp(epoch, UTC) if epoch else datetime.fromisoformat(raw["received"])
        self._latest = ts
        values = convert_observation(o)
        if not values:
            return []
        loc = raw["location"]
        return [Observation(source=self.meta.name, station_id=self.cfg.station_id, lat=loc["lat"], lon=loc["lon"],
                            ts=ts, values=values)]

    async def _location(self) -> dict[str, Any]:
        return await self.location(self.cfg.location_id)

    async def health(self) -> Health:
        h = self.ctx.state
        if h.state == HealthState.OK:
            if self._empty:
                h.state, h.last_error = HealthState.DEGRADED, "WU has no observation for this station in the last hour"
            elif self._latest and utcnow() - self._latest > STALE_AFTER:
                h.state = HealthState.DEGRADED
                h.last_error = f"newest reading is {int((utcnow() - self._latest).total_seconds() // 60)} min old"
        return h
