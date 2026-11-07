"""Open-Meteo model forecast for home: convective parameters (CAPE, lifted index, shear) and rain chance.

Open-Meteo (https://open-meteo.com) is free for non-commercial use without a key, under CC BY 4.0 (credit it where
the data is shown). The forecast is a model, not an observation, so it is not stored as observations: one event per
location holds the next ``forecast_hours`` hours and is refreshed in place on every poll. ``/api/forecast`` serves it.

Storm potential is a rule of thumb on the next 12 hours' peak CAPE (J/kg): under 250 none, to 1000 low, to 2500
moderate, above that high; "organised" means 0-6 km-ish bulk shear of at least 15 m/s as well, which favours
longer-lived storms. It says the ingredients are there, not that a storm will happen.
"""
from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field

from ...models import Event, Normalised
from ...plugins import Licence, RateLimit, Source, SourceMeta
from ...timeutil import utcnow

HOURLY = ["cape", "lifted_index", "convective_inhibition", "precipitation_probability", "precipitation", "temperature_2m",
          "wind_speed_10m", "wind_direction_10m", "wind_gusts_10m", "wind_speed_500hPa", "wind_direction_500hPa", "freezing_level_height"]
CURRENT_WIND = "wind_speed_10m,wind_direction_10m,wind_gusts_10m"
GRID_N = 5                                   # 5 x 5 model points around home for the map's wind arrows
SUMMARY_HOURS = 12
ORGANISED_SHEAR_MS = 15.0


class OpenMeteoConfig(BaseModel):
    location_id: str | None = Field(None, description="id of the configured location to forecast for (default: the first)")
    forecast_hours: int = Field(48, ge=12, le=168, description="how many hours ahead to keep")
    wind_grid: bool = Field(True, description="also fetch the model's wind at a 5 x 5 grid of points around home, for the map's wind arrows (one extra request an hour)")
    wind_grid_km: int = Field(25, ge=10, le=100, description="distance between grid points, km")
    http_base: str = Field("https://api.open-meteo.com", pattern=r"^https?://[A-Za-z0-9.:-]+$", description="Open-Meteo server")


def _uv(speed: float | None, direction: float | None) -> tuple[float, float] | None:
    """Meteorological wind (speed, direction it blows FROM) -> (u east, v north) in m/s."""
    if speed is None or direction is None:
        return None
    r = math.radians(direction)
    return -speed * math.sin(r), -speed * math.cos(r)


def bulk_shear(w10: tuple[float | None, float | None], w500: tuple[float | None, float | None]) -> float | None:
    """Magnitude of the vector wind difference between the surface (10 m) and 500 hPa (~5.5 km), m/s."""
    a, b = _uv(*w10), _uv(*w500)
    return None if a is None or b is None else round(math.hypot(b[0] - a[0], b[1] - a[1]), 1)


def potential(max_cape: float | None, max_shear: float | None) -> dict[str, Any]:
    c = max_cape or 0
    level = "none" if c < 250 else "low" if c < 1000 else "moderate" if c < 2500 else "high"
    return {"level": level, "organised": bool(level in ("moderate", "high") and (max_shear or 0) >= ORGANISED_SHEAR_MS)}


def build_hours(h: dict[str, list]) -> list[dict[str, Any]]:
    n = len(h.get("time", []))
    get = lambda key, i: (h.get(key) or [None] * n)[i]   # noqa: E731
    out = []
    for i in range(n):
        out.append({
            "t": datetime.fromtimestamp(h["time"][i], UTC).isoformat(),
            "cape_jkg": get("cape", i), "lifted_index": get("lifted_index", i), "cin_jkg": get("convective_inhibition", i),
            "precip_prob_pct": get("precipitation_probability", i), "precip_mm": get("precipitation", i), "temp_c": get("temperature_2m", i),
            "wind_10m_ms": get("wind_speed_10m", i), "wind_dir_10m_deg": get("wind_direction_10m", i), "gust_10m_ms": get("wind_gusts_10m", i), "wind_500hpa_ms": get("wind_speed_500hPa", i),
            "shear_ms": bulk_shear((get("wind_speed_10m", i), get("wind_direction_10m", i)), (get("wind_speed_500hPa", i), get("wind_direction_500hPa", i))),
            "freezing_level_m": get("freezing_level_height", i)})
    return out


def summarise(hours: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
    end = now + timedelta(hours=SUMMARY_HOURS)
    win = [x for x in hours if now - timedelta(minutes=59) <= datetime.fromisoformat(x["t"]) <= end]
    if not win:
        return {"hours": SUMMARY_HOURS, "potential": potential(None, None)}
    pick = lambda key, f: f((x for x in win if x[key] is not None), key=lambda x: x[key], default=None)   # noqa: E731
    gu = pick("gust_10m_ms", max)
    top, low, sh, pp = pick("cape_jkg", max), pick("lifted_index", min), pick("shear_ms", max), pick("precip_prob_pct", max)
    return {"hours": SUMMARY_HOURS, "max_cape_jkg": top["cape_jkg"] if top else None, "max_cape_at": top["t"] if top else None,
            "min_lifted_index": low["lifted_index"] if low else None, "max_shear_ms": sh["shear_ms"] if sh else None,
            "max_precip_prob_pct": pp["precip_prob_pct"] if pp else None,
            "max_gust_ms": gu["gust_10m_ms"] if gu else None, "max_gust_at": gu["t"] if gu else None, "total_precip_mm": round(sum(x["precip_mm"] or 0 for x in win), 1),
            "potential": potential(top["cape_jkg"] if top else None, sh["shear_ms"] if sh else None)}


def grid_points(lat: float, lon: float, spacing_km: float, n: int = GRID_N) -> list[tuple[float, float]]:
    """An n x n grid of points centred on (lat, lon), spacing_km apart (longitude steps widen with latitude)."""
    dlat = spacing_km / 111.2
    dlon = spacing_km / (111.2 * max(math.cos(math.radians(lat)), 0.2))
    half = (n - 1) / 2
    return [(round(lat + (i - half) * dlat, 3), round(lon + (j - half) * dlon, 3)) for i in range(n) for j in range(n)]


def parse_wind_grid(reply: Any, pts: list[tuple[float, float]]) -> list[dict[str, Any]]:
    """Open-Meteo's multi-location reply (a list, one entry per point, in order) -> arrows. Bad entries are skipped."""
    rows = reply if isinstance(reply, list) else [reply]
    out = []
    for (lat, lon), r in zip(pts, rows, strict=False):
        c = (r or {}).get("current") if isinstance(r, dict) else None
        if not isinstance(c, dict) or c.get("wind_speed_10m") is None or c.get("wind_direction_10m") is None:
            continue
        out.append({"lat": lat, "lon": lon, "speed_ms": c["wind_speed_10m"], "dir_deg": c["wind_direction_10m"], "gust_ms": c.get("wind_gusts_10m")})
    return out


class OpenMeteo(Source):
    meta = SourceMeta(
        name="open_meteo",
        category="model_forecast",
        description="Model forecast for home from Open-Meteo: storm ingredients (CAPE, lifted index, shear) and chance of rain, hourly.",
        poll_interval_s=3600,
        rate_limit=RateLimit(max_requests=2, period_s=3600),
        licence=Licence(type="personal", terms_url="https://open-meteo.com/en/terms", attribution="Weather data by Open-Meteo.com (CC BY 4.0)",
                        notes="Free for non-commercial use with attribution; no key needed. A model forecast, not an observation."),
    )
    ConfigModel = OpenMeteoConfig
    cfg: OpenMeteoConfig

    async def fetch(self) -> dict[str, Any]:
        loc = await self.location(self.cfg.location_id)
        r = await self.ctx.http.get(f"{self.cfg.http_base}/v1/forecast", timeout=30, params={
            "latitude": round(loc["lat"], 3), "longitude": round(loc["lon"], 3), "hourly": ",".join(HOURLY), "wind_speed_unit": "ms",
            "timezone": "UTC", "timeformat": "unixtime", "forecast_hours": self.cfg.forecast_hours, "past_hours": 0})
        if r.status_code >= 400:
            reason = ""
            try:
                reason = str(r.json().get("reason", ""))[:160]
            except ValueError:
                pass
            raise RuntimeError(f"Open-Meteo answered {r.status_code}{': ' + reason if reason else ''}")
        raw: dict[str, Any] = {"json": r.json(), "received": utcnow().isoformat(), "location": loc}
        if self.cfg.wind_grid:
            pts = grid_points(loc["lat"], loc["lon"], self.cfg.wind_grid_km)
            try:                                                   # best effort: the forecast above is the source's real job
                g = await self.ctx.http.get(f"{self.cfg.http_base}/v1/forecast", timeout=30, params={
                    "latitude": ",".join(str(p[0]) for p in pts), "longitude": ",".join(str(p[1]) for p in pts), "current": CURRENT_WIND,
                    "wind_speed_unit": "ms", "timezone": "UTC", "timeformat": "unixtime"})
                g.raise_for_status()
                raw["grid"] = {"points": pts, "json": g.json()}
            except Exception as e:                                 # noqa: BLE001
                self.ctx.log.info("open-meteo wind grid unavailable: %s", e)
                raw["grid_error"] = type(e).__name__
        return raw

    def normalise(self, raw: dict[str, Any]) -> list[Normalised]:
        j, loc = raw["json"], raw["location"]
        if not isinstance(j.get("hourly"), dict) or not j["hourly"].get("time"):
            raise RuntimeError("unexpected reply: no hourly forecast")
        now = datetime.fromisoformat(raw["received"])
        hours = build_hours(j["hourly"])
        summary = summarise(hours, now)
        self.ctx.state.detail.update(potential=summary["potential"]["level"], max_cape_jkg=summary.get("max_cape_jkg"), hours=len(hours))
        out: list[Normalised] = [Event(source=self.meta.name, event_type="other", external_id=f"forecast:{loc['id']}", ts=now, lat=loc["lat"], lon=loc["lon"],
                      properties={"kind": "model_forecast", "model": "open-meteo", "location_id": loc["id"], "issued": now.isoformat(),
                                  "attribution": self.meta.licence.attribution, "summary": summary, "hours": hours})]
        grid = raw.get("grid")
        points = parse_wind_grid(grid["json"], [tuple(p) for p in grid["points"]]) if grid else []
        self.ctx.state.detail["wind_grid_points"] = len(points) if self.cfg.wind_grid else "off"
        if points:                                                  # a refreshed record like the forecast: the latest model winds around home
            out.append(Event(source=self.meta.name, event_type="other", external_id=f"windfield:{loc['id']}", ts=now, lat=loc["lat"], lon=loc["lon"],
                             properties={"kind": "model_wind_field", "model": "open-meteo", "location_id": loc["id"], "issued": now.isoformat(),
                                         "attribution": self.meta.licence.attribution, "points": points}))
        return out
