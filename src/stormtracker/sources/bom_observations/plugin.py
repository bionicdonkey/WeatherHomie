"""BOM automatic weather station observations (the JSON products behind bom.gov.au's observation tables).

One station per source instance, for example Sydney Observatory Hill (WMO 94768, product IDN60801). The product is the state
observations list (IDN60801 = NSW) and the station is its WMO number; both are in the page URL on bom.gov.au, e.g.
``/products/IDN60801/IDN60801.94768.shtml``. BOM updates stations about every 30 minutes and the JSON carries
the last ~3 days, so the first poll backfills the recent history and later polls add what is new.

Terms: BOM's public web products are for personal use under its copyright notice; they may not be redistributed or
used commercially without a registered subscription (see ``licence`` below).
"""
from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from pydantic import BaseModel, Field

from ... import bom_catalogue as cat
from ...models import Normalised, Observation
from ...plugins import Candidate, Health, HealthState, Licence, RateLimit, Source, SourceMeta, Suggestion
from ...timeutil import utcnow

COMPASS = {name: i * 22.5 for i, name in enumerate(
    ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"])}
STALE_AFTER = timedelta(minutes=100)       # a station normally reports every 30 minutes


class BomObservationsConfig(BaseModel):
    product_id: str = Field("IDN60801", pattern=r"^ID[A-Z]\d{5}$", description="BOM observations product for the state, e.g. IDN60801 (NSW)")
    wmo: int = Field(..., ge=1, le=99999, description="the station's WMO number, e.g. 94768 for Sydney Observatory Hill (it is in the station's page URL on bom.gov.au)")
    station_id: str | None = Field(None, pattern=r"^[A-Za-z0-9_.-]{1,40}$", description="id stored with observations (default: bom_<wmo>)")
    history_hours: int = Field(72, ge=1, le=96, description="how much recent history to keep from each download")
    http_base: str = Field("https://reg.bom.gov.au", pattern=r"^https?://[A-Za-z0-9.:-]+$", description="BOM web server")


def _num(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(str(v).strip())
    except ValueError:
        return None                                  # "-" and other placeholders
    return f if f == f else None


def convert_entry(e: dict[str, Any]) -> dict[str, float]:
    """One BOM observation record -> common observation values (SI-ish; BOM wind is km/h)."""
    out: dict[str, float] = {}

    def put(key: str, raw: Any, scale: float = 1.0) -> None:
        v = _num(raw)
        if v is not None:
            out[key] = round(v * scale, 3)
    put("temp_c", e.get("air_temp")); put("apparent_temp_c", e.get("apparent_t")); put("dewpoint_c", e.get("dewpt"))
    put("humidity_pct", e.get("rel_hum")); put("pressure_hpa", e.get("press_msl")); put("pressure_station_hpa", e.get("press"))
    put("wind_speed_ms", e.get("wind_spd_kmh"), 1 / 3.6); put("wind_gust_ms", e.get("gust_kmh"), 1 / 3.6)
    put("rain_since_9am_mm", e.get("rain_trace")); put("visibility_km", e.get("vis_km")); put("cloud_oktas", e.get("cloud_oktas"))
    put("delta_t_c", e.get("delta_t"))
    d = COMPASS.get(str(e.get("wind_dir", "")).strip().upper())
    if d is not None:                                # CALM has no direction
        out["wind_dir_deg"] = d
    return out


def parse_ts(e: dict[str, Any]) -> datetime | None:
    try:
        return datetime.strptime(str(e["aifstime_utc"]), "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    except (KeyError, ValueError):
        return None


class BomObservations(Source):
    meta = SourceMeta(
        name="bom_observations",
        category="observations",
        description="An official BOM weather station (for example Sydney Observatory Hill), about every 30 minutes, with 3 days of history.",
        poll_interval_s=600,
        rate_limit=RateLimit(max_requests=1, period_s=300),
        licence=Licence(type="personal", terms_url="http://www.bom.gov.au/other/copyright.shtml",
                        attribution="Data: Commonwealth of Australia, Bureau of Meteorology",
                        notes="BOM web products are free for personal use under its copyright notice; redistribution or commercial use needs a registered BOM subscription."),
    )
    ConfigModel = BomObservationsConfig
    cfg: BomObservationsConfig

    @classmethod
    async def suggest(cls, location, ctx, current):
        """The nearest open BOM stations that publish observations right now. The station list says where stations are;
        whether one has a live product, and in which state product, is checked by asking BOM for it."""
        base = current.get("http_base") or "https://reg.bom.gov.au"
        stations = await cat.load_stations(ctx.http, ctx.data_dir)
        out: list[Candidate] = []
        tried = 0
        for km, st in cat.nearest(stations, location["lat"], location["lon"], limit=12, max_km=80, need_wmo=True):
            if len(out) == 3 or tried >= 8:
                break
            for product in cat.obs_products(st.state):
                tried += 1
                try:
                    r = await ctx.http.get(f"{base}/fwo/{product}/{product}.{st.wmo}.json", timeout=20)
                    j = r.json() if r.status_code == 200 else None
                    newest = max((t for e in j["observations"]["data"][:6] if (t := parse_ts(e))), default=None)
                except (httpx.HTTPError, ValueError, KeyError, TypeError):
                    continue
                if newest is None:
                    continue
                age = (utcnow() - newest).total_seconds() / 60
                if age > 360:
                    break                                             # listed, but not reporting: skip this station
                out.append(Candidate(
                    label=st.name.title() if st.name.isupper() else st.name, values={"wmo": st.wmo, "product_id": product},
                    current=current.get("wmo") == st.wmo,
                    detail=f"{km:.0f} km away · {st.state} · WMO {st.wmo} · latest reading {age:.0f} min ago"))
                break
        if not out:
            return Suggestion(candidates=[], note="No open BOM station within 80 km is publishing observations right now.")
        return Suggestion(candidates=out, note="Nearest first. Stations differ in what they measure (some have no rain gauge or no wind), so "
                          "check the Station page after switching on.")

    async def fetch(self) -> dict[str, Any]:
        url = f"{self.cfg.http_base}/fwo/{self.cfg.product_id}/{self.cfg.product_id}.{self.cfg.wmo}.json"
        r = await self.ctx.http.get(url, timeout=30)
        r.raise_for_status()
        j = r.json()
        if not isinstance(j, dict) or "data" not in j.get("observations", {}):
            raise RuntimeError("unexpected reply: not a BOM observations product")
        return {"json": j, "received": utcnow().isoformat()}

    def normalise(self, raw: dict[str, Any]) -> list[Normalised]:
        data = raw["json"]["observations"]["data"]
        now = datetime.fromisoformat(raw["received"])
        station = self.cfg.station_id or f"bom_{self.cfg.wmo}"
        keep_from = now - timedelta(hours=self.cfg.history_hours)
        items: list[Normalised] = []
        for e in data:
            ts = parse_ts(e)
            vals = convert_entry(e)
            if ts is None or ts < keep_from or not vals or e.get("lat") is None or e.get("lon") is None:
                continue
            items.append(Observation(source=self.meta.name, station_id=station, lat=float(e["lat"]), lon=float(e["lon"]), ts=ts, values=vals))
        self._newest = max((i.ts for i in items), default=None)          # type: ignore[attr-defined]
        return items

    async def health(self) -> Health:
        h = self.ctx.state
        newest = getattr(self, "_newest", None)
        if h.state == HealthState.OK and newest is not None and utcnow() - newest > STALE_AFTER:
            h.state = HealthState.DEGRADED
            h.last_error = f"the station last reported {int((utcnow() - newest).total_seconds() // 60)} min ago"
        return h
