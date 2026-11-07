"""Alert rules: what to watch for. Each rule type is a small pydantic model plus a pure evaluator that turns a
snapshot of the current situation into zero or more conditions. Delivery, dedup and cooldowns live elsewhere."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator

from .base import Severity

TZ = ZoneInfo("Australia/Sydney")
STALE_RADAR_MIN = 25          # forecasts built on radar older than this are not alerted on
STALE_OBS_MIN = 15


# ---- parameters per rule type -------------------------------------------------------------------------------------
class StormEta(BaseModel):
    type: Literal["storm_eta"] = "storm_eta"
    min_intensity_mmh: float = Field(25.0, ge=1, le=300, title="Storm core at least (mm/h)", description="How heavy the storm core has to be.")
    max_eta_min: int = Field(30, ge=1, le=180, title="Arrives within (minutes)", description="Alert when the core is expected at home within this long.")


class RainEta(BaseModel):
    type: Literal["rain_eta"] = "rain_eta"
    min_peak_mmh: float = Field(5.0, ge=0.5, le=300, title="Peak at home at least (mm/h)", description="How heavy the forecast says it will get at home.")
    max_eta_min: int = Field(30, ge=1, le=180, title="Starts within (minutes)", description="Alert when rain is expected at home within this long.")
    min_confidence: Literal["low", "medium", "high"] = Field("low", title="Only when the forecast is at least this sure", description="Forecasts less sure than this are ignored.")


class RainingNow(BaseModel):
    type: Literal["raining_now"] = "raining_now"
    min_rate_mmh: float = Field(5.0, ge=0.1, le=300, title="Rain at least (mm/h)", description="Judged by the radar or your station, whichever is higher.")


class Lightning(BaseModel):
    type: Literal["lightning"] = "lightning"
    within_km: float = Field(10.0, ge=1, le=200, title="Within (km of home)", description="How close the strikes have to be.")
    within_min: int = Field(30, ge=1, le=360, title="In the last (minutes)", description="How recent the strikes have to be.")
    min_strikes: int = Field(1, ge=1, le=1000, title="At least this many strikes", description="How many strikes it takes.")


class WarningMatch(BaseModel):
    type: Literal["warning"] = "warning"
    match_text: str = Field("Severe Thunderstorm", max_length=100, title="Warning text contains", description="Not case sensitive. A word or phrase.")
    area_only: bool = Field(True, title="Only warnings marked for my area", description="Uses the area words set on the BOM warnings source. With no words set, every warning counts as yours.")


class Gust(BaseModel):
    type: Literal["gust"] = "gust"
    min_gust_kmh: float = Field(60.0, ge=10, le=300, title="Gust at least (km/h)", description="Measured by your station.")


class StormPotential(BaseModel):
    type: Literal["storm_potential"] = "storm_potential"
    min_cape_jkg: float = Field(1500.0, ge=100, le=6000, title="Model CAPE at least (J/kg)", description="The model's storm fuel. 1000 is moderate, 2500 and up is high.")
    within_hours: int = Field(6, ge=1, le=48, title="Within the next (hours)", description="Only look this far ahead.")
    organised_only: bool = Field(False, title="Only when there is strong wind shear too", description="Shear favours long-lived, organised storms.")


class ForecastGust(BaseModel):
    type: Literal["forecast_gust"] = "forecast_gust"
    min_gust_kmh: float = Field(70.0, ge=20, le=300, title="Forecast gust at least (km/h)", description="The weather model's 10 m gust forecast for home.")
    within_hours: int = Field(12, ge=1, le=48, title="Within the next (hours)", description="Only look this far ahead.")


class WindShift(BaseModel):
    type: Literal["wind_shift"] = "wind_shift"
    min_shift_deg: float = Field(60.0, ge=20, le=180, title="Wind direction changed by at least (degrees)", description="Measured by your station: a sudden change of direction often marks a gust front or southerly change.")
    within_min: int = Field(30, ge=5, le=120, title="Within (minutes)", description="How quickly the change happened.")
    min_gust_kmh: float = Field(30.0, ge=5, le=200, title="And the gusts now at least (km/h)", description="Ignores gentle shifts, such as a light sea breeze arriving.")


class PressureDrop(BaseModel):
    type: Literal["pressure_drop"] = "pressure_drop"
    min_drop_hpa: float = Field(3.0, ge=0.5, le=30, title="Fall in one hour at least (hPa)", description="Measured by your station.")


Params = Annotated[StormEta | RainEta | RainingNow | Lightning | WarningMatch | Gust | ForecastGust | WindShift | StormPotential | PressureDrop, Field(discriminator="type")]
RULE_TYPES: dict[str, tuple[type[BaseModel], str, str]] = {   # type -> (model, title, one-line description)
    "storm_eta": (StormEta, "Storm heading for home", "A tracked storm cell is expected to reach home soon."),
    "rain_eta": (RainEta, "Rain about to start", "The forecast says rain will reach home soon."),
    "raining_now": (RainingNow, "Raining at home", "It is raining at home, by radar or your station."),
    "lightning": (Lightning, "Lightning nearby", "Lightning strikes close to home."),
    "warning": (WarningMatch, "Weather warning", "A warning is in force whose text matches."),
    "gust": (Gust, "Strong gusts", "Your station recorded a strong gust."),
    "forecast_gust": (ForecastGust, "Strong gusts forecast", "The weather model forecasts strong gusts at home."),
    "wind_shift": (WindShift, "Sudden wind change", "Your station's wind swung round quickly, often a gust front or southerly change."),
    "storm_potential": (StormPotential, "Storm ingredients forecast", "The weather model says conditions are ripe for storms later today."),
    "pressure_drop": (PressureDrop, "Pressure falling fast", "Air pressure at your station is dropping quickly."),
}


class Rule(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,39}$")
    name: str = Field(min_length=1, max_length=80)
    enabled: bool = True
    params: Params
    outputs: list[str] = Field(default_factory=list, description="names of the outputs that deliver this rule's alerts")
    severity: Severity = "warning"
    cooldown_min: int = Field(30, ge=0, le=1440, description="least minutes between alerts from this rule")
    repeat_after_min: int = Field(0, ge=0, le=1440, description="remind again if it is still going after this long (0 = never)")
    notify_clear: bool = Field(False, description="also send a message when it is over")

    @field_validator("outputs")
    @classmethod
    def _unique(cls, v: list[str]) -> list[str]:
        return list(dict.fromkeys(v))


class AlertSettings(BaseModel):
    enabled: bool = True
    public_url: str | None = Field(None, max_length=200, description="address of this server as you reach it, added to alerts as a link")
    clear_after_min: int = Field(10, ge=1, le=120, description="a condition must be gone this long before it counts as over")

    @field_validator("public_url")
    @classmethod
    def _url(cls, v: str | None) -> str | None:
        v = (v or "").strip().rstrip("/")
        if v and not v.startswith(("http://", "https://")):
            raise ValueError("must start with http:// or https://")
        return v or None


PRESETS: list[dict[str, Any]] = [
    {"name": "Storm heading for home", "severity": "critical", "cooldown_min": 20, "notify_clear": False,
     "params": {"type": "storm_eta", "min_intensity_mmh": 25, "max_eta_min": 30}},
    {"name": "Rain about to start", "severity": "info", "cooldown_min": 60, "notify_clear": False,
     "params": {"type": "rain_eta", "min_peak_mmh": 5, "max_eta_min": 30, "min_confidence": "low"}},
    {"name": "Lightning within 10 km", "severity": "warning", "cooldown_min": 30, "notify_clear": True,
     "params": {"type": "lightning", "within_km": 10, "within_min": 30, "min_strikes": 1}},
    {"name": "Severe thunderstorm warning", "severity": "critical", "cooldown_min": 30, "notify_clear": False,
     "params": {"type": "warning", "match_text": "Severe Thunderstorm"}},
    {"name": "Damaging gusts", "severity": "warning", "cooldown_min": 30, "notify_clear": False,
     "params": {"type": "gust", "min_gust_kmh": 60}},
    {"name": "Strong gusts forecast", "severity": "info", "cooldown_min": 180, "notify_clear": False,
     "params": {"type": "forecast_gust", "min_gust_kmh": 70, "within_hours": 12}},
    {"name": "Sudden wind change", "severity": "warning", "cooldown_min": 60, "notify_clear": False,
     "params": {"type": "wind_shift", "min_shift_deg": 60, "within_min": 30, "min_gust_kmh": 30}},
]


# ---- the situation, and what a rule makes of it -------------------------------------------------------------------------
@dataclass
class Snapshot:
    now: datetime
    home: dict[str, Any] | None = None                   # {name, lat, lon}
    nowcast: dict[str, Any] | None = None                # GET /api/nowcast, when available
    obs: dict[str, Any] | None = None                    # latest station reading values
    obs_ts: datetime | None = None
    pressure_1h_ago: float | None = None
    lightning: list[dict[str, Any]] = field(default_factory=list)   # {ts, lat, lon, distance_km}
    warnings: list[dict[str, Any]] = field(default_factory=list)    # {external_id, ts, expires, properties}
    wind_history: list[dict[str, Any]] = field(default_factory=list)   # {ts, speed_ms, gust_ms, dir_deg} from the latest reading's station, oldest first, last 2 h
    model_forecast: dict[str, Any] | None = None         # the model forecast event's properties (Open-Meteo): {issued, hours, summary}


@dataclass
class Condition:
    key: str
    title: str
    message: str
    data: dict[str, Any] = field(default_factory=dict)
    ended_title: str | None = None                       # wording for the "it's over" message


def local(ts: datetime, ref: datetime | None = None) -> str:
    """Sydney clock time, with the weekday in front when it is not the same day as ``ref`` (so "10:00 am" is never ambiguous)."""
    t = ts.astimezone(TZ)
    day = f"{t:%a} " if ref is not None and t.date() != ref.astimezone(TZ).date() else ""
    return f"{day}{t.strftime('%-I:%M %p').lower()}"


def _in(minutes: float) -> str:
    m = max(0, round(minutes))
    return "now" if m <= 1 else f"in about {m} min" if m < 90 else f"in about {m / 60:.1f} h"


def _mm(v: float) -> str:
    return f"{v:.1f}" if v < 10 else f"{v:.0f}"


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 12742 * math.asin(math.sqrt(a))


def _forecast_ok(s: Snapshot) -> dict | None:
    nc = s.nowcast
    if not nc or not nc.get("available") or not s.home:
        return None
    frame = datetime.fromisoformat(nc["frame_ts"])
    return nc if (s.now - frame) <= timedelta(minutes=STALE_RADAR_MIN) else None


def _fresh_obs(s: Snapshot) -> dict | None:
    return s.obs if s.obs and s.obs_ts and (s.now - s.obs_ts) <= timedelta(minutes=STALE_OBS_MIN) else None


_LEVEL = {"low": 0, "medium": 1, "high": 2}


def evaluate(rule: Rule, s: Snapshot) -> list[Condition]:
    p = rule.params
    out: list[Condition] = []
    if isinstance(p, StormEta):
        nc = _forecast_ok(s)
        for c in (nc or {}).get("cells", []):
            h = c.get("home") or {}
            if not h.get("approaching") or h.get("eta_min") is None or c["max_mmh"] < p.min_intensity_mmh:
                continue
            at = datetime.fromisoformat(nc["frame_ts"]) + timedelta(minutes=h["eta_min"])
            left = (at - s.now).total_seconds() / 60
            if -5 <= left <= p.max_eta_min:
                head = f", heading {_compass(c['bearing_deg'])} at {c['speed_kmh']:.0f} km/h" if c.get("bearing_deg") is not None and c.get("speed_kmh") else ""
                out.append(Condition(f"cell:{c['id']}", "Storm heading for home",
                                     f"A storm cell of {_mm(c['max_mmh'])} mm/h{head} should reach home {_in(left)} (about {local(at)}). Radar at {local(datetime.fromisoformat(nc['frame_ts']))}.",
                                     {"cell": c["id"], "intensity_mmh": c["max_mmh"], "eta_min": round(left), "eta_ts": at.isoformat(), "speed_kmh": c.get("speed_kmh"), "bearing_deg": c.get("bearing_deg")},
                                     ended_title="Storm has passed or moved away"))
    elif isinstance(p, RainEta):
        nc = _forecast_ok(s)
        imp = (nc or {}).get("impact") or {}
        if imp.get("status") == "approaching" and imp.get("peak_mmh", 0) >= p.min_peak_mmh:
            conf = (imp.get("confidence") or {}).get("level", "low")
            left = (datetime.fromisoformat(imp["eta_ts"]) - s.now).total_seconds() / 60
            if _LEVEL[conf] >= _LEVEL[p.min_confidence] and -5 <= left <= p.max_eta_min:
                out.append(Condition("rain-approaching", "Rain about to start",
                                     f"Rain should reach home {_in(left)} (about {local(datetime.fromisoformat(imp['eta_ts']))}), peaking near {_mm(imp['peak_mmh'])} mm/h. Confidence {conf}.",
                                     {"eta_min": round(left), "peak_mmh": imp["peak_mmh"], "confidence": conf}, ended_title="Rain forecast has cleared"))
    elif isinstance(p, RainingNow):
        nc = _forecast_ok(s)
        radar = (nc or {}).get("impact", {}).get("now_mmh", 0) if (nc or {}).get("impact", {}).get("status") == "raining" else 0
        stn = (_fresh_obs(s) or {}).get("rain_rate_mmh") or 0
        best = max(radar or 0, stn or 0)
        if best >= p.min_rate_mmh:
            src = "your station" if stn >= (radar or 0) else "radar"
            out.append(Condition("raining", "Raining at home", f"It is raining at home at about {_mm(best)} mm/h ({src}).",
                                 {"rate_mmh": best, "source": src}, ended_title="Rain at home has eased"))
    elif isinstance(p, Lightning) and s.home:
        since = s.now - timedelta(minutes=p.within_min)
        near = []
        for e in s.lightning:
            if e["ts"] < since:
                continue
            d = e.get("distance_km")
            if d is None and e.get("lat") is not None:
                d = haversine_km(s.home["lat"], s.home["lon"], e["lat"], e["lon"])
            if d is not None and d <= p.within_km:
                near.append(d)
        if len(near) >= p.min_strikes:
            out.append(Condition("lightning", "Lightning nearby",
                                 f"{len(near)} lightning strike{'s' if len(near) != 1 else ''} within {p.within_km:g} km of home in the last {p.within_min} min; the closest was {min(near):.0f} km away.",
                                 {"strikes": len(near), "closest_km": round(min(near), 1)}, ended_title="No more lightning nearby"))
    elif isinstance(p, WarningMatch):
        needle = p.match_text.lower()
        for w in s.warnings:
            if w.get("expires") and w["expires"] <= s.now:
                continue
            if p.area_only and (w.get("properties") or {}).get("in_area") is False:
                continue
            text = " ".join(str(v) for v in (w.get("properties") or {}).values() if isinstance(v, str)).lower()
            if needle in text:
                title = (w.get("properties") or {}).get("title") or (w.get("properties") or {}).get("headline") or "Weather warning"
                out.append(Condition(f"warning:{w['external_id']}", str(title)[:120], f"{title}. Issued {local(w['ts'])}.",
                                     {"external_id": w["external_id"]}, ended_title="Warning is no longer in force"))
    elif isinstance(p, Gust):
        v = _fresh_obs(s)
        g = None if v is None or v.get("wind_gust_ms") is None else v["wind_gust_ms"] * 3.6
        if g is not None and g >= p.min_gust_kmh:
            out.append(Condition("gust", "Strong gusts", f"Your station recorded a gust of {g:.0f} km/h.", {"gust_kmh": round(g)}, ended_title="Gusts have eased"))
    elif isinstance(p, ForecastGust):
        mf = s.model_forecast
        if mf and s.now - datetime.fromisoformat(mf["issued"]) <= timedelta(hours=3):
            end = s.now + timedelta(hours=p.within_hours)
            win = [x for x in mf["hours"] if s.now - timedelta(minutes=59) <= datetime.fromisoformat(x["t"]) <= end and x.get("gust_10m_ms") is not None]
            if win:
                top = max(win, key=lambda x: x["gust_10m_ms"])
                kmh = top["gust_10m_ms"] * 3.6
                if kmh >= p.min_gust_kmh:
                    at = datetime.fromisoformat(top["t"])
                    d = top.get("wind_dir_10m_deg")
                    out.append(Condition("forecast-gust", "Strong gusts forecast",
                                         f"The weather model forecasts gusts up to {kmh:.0f} km/h around {local(at, s.now)}{f' from the {_compass(d)}' if d is not None else ''}.",
                                         {"gust_kmh": round(kmh), "at": top["t"]}, ended_title="Forecast gusts have eased"))
    elif isinstance(p, WindShift):
        shift = wind_shift(s.wind_history, s.now, p.within_min)
        if shift and shift["shift_deg"] >= p.min_shift_deg and shift["gust_kmh"] >= p.min_gust_kmh:
            out.append(Condition("wind-shift", "Sudden wind change",
                                 f"The wind swung {shift['shift_deg']:.0f}° in {shift['minutes']:.0f} min, from the {_compass(shift['from_deg'])} to the {_compass(shift['to_deg'])}, "
                                 f"with gusts of {shift['gust_kmh']:.0f} km/h. Often a gust front or southerly change.",
                                 {k: round(v) for k, v in shift.items()}, ended_title="Wind has settled"))
    elif isinstance(p, StormPotential):
        mf = s.model_forecast
        if mf and s.now - datetime.fromisoformat(mf["issued"]) <= timedelta(hours=3):
            end = s.now + timedelta(hours=p.within_hours)
            win = [x for x in mf["hours"] if s.now - timedelta(minutes=59) <= datetime.fromisoformat(x["t"]) <= end and x.get("cape_jkg") is not None]
            if win:
                top = max(win, key=lambda x: x["cape_jkg"])
                shear = max((x["shear_ms"] for x in win if x.get("shear_ms") is not None), default=0)
                if top["cape_jkg"] >= p.min_cape_jkg and (not p.organised_only or shear >= 15):
                    at = datetime.fromisoformat(top["t"])
                    out.append(Condition("storm-potential", "Conditions favour storms",
                                         f"The weather model has storm fuel building: CAPE up to {top['cape_jkg']:.0f} J/kg around {local(at, s.now)}"
                                         f"{', with strong wind shear' if shear >= 15 else ''}. This is the ingredients, not a forecast of a storm.",
                                         {"cape_jkg": top["cape_jkg"], "at": top["t"], "shear_ms": shear}, ended_title="Storm ingredients have eased"))
    elif isinstance(p, PressureDrop):
        v = _fresh_obs(s)
        if v and v.get("pressure_hpa") is not None and s.pressure_1h_ago is not None:
            drop = s.pressure_1h_ago - v["pressure_hpa"]
            if drop >= p.min_drop_hpa:
                out.append(Condition("pressure-drop", "Pressure falling fast", f"Air pressure has fallen {drop:.1f} hPa in the last hour, to {v['pressure_hpa']:.0f} hPa.",
                                     {"drop_hpa": round(drop, 1)}, ended_title="Pressure has stopped falling quickly"))
    return out


def _circ_mean(degs: list[float]) -> float:
    x = sum(math.cos(math.radians(d)) for d in degs)
    y = sum(math.sin(math.radians(d)) for d in degs)
    return math.degrees(math.atan2(y, x)) % 360


def _angle(a: float, b: float) -> float:
    return abs((a - b + 180) % 360 - 180)


def wind_shift(history: list[dict[str, Any]], now: datetime, within_min: int) -> dict[str, float] | None:
    """How far the wind direction swung over the last ``within_min`` minutes, from a station's readings (oldest first).

    Compares the average direction of the oldest third of the window with the newest third (so one noisy reading can't
    cause it), counting only readings with a speed of at least 2 m/s: below that a direction means very little.
    ``gust_kmh`` is the strongest gust or wind in the newest third."""
    since = now - timedelta(minutes=within_min)
    rows = [r for r in history if r["ts"] >= since and r.get("dir_deg") is not None and (r.get("speed_ms") or 0) >= 2.0]
    if len(rows) < 6 or (rows[-1]["ts"] - rows[0]["ts"]).total_seconds() < within_min * 60 * 0.4:
        return None
    k = max(2, len(rows) // 3)
    old, new = rows[:k], rows[-k:]
    a, b = _circ_mean([r["dir_deg"] for r in old]), _circ_mean([r["dir_deg"] for r in new])
    gust = max((r.get("gust_ms") or r.get("speed_ms") or 0) for r in new) * 3.6
    return {"shift_deg": _angle(a, b), "from_deg": a, "to_deg": b, "gust_kmh": gust, "minutes": (new[-1]["ts"] - old[0]["ts"]).total_seconds() / 60}


_COMPASS = ["north", "north-north-east", "north-east", "east-north-east", "east", "east-south-east", "south-east", "south-south-east",
            "south", "south-south-west", "south-west", "west-south-west", "west", "west-north-west", "north-west", "north-north-west"]


def _compass(deg: float) -> str:
    return _COMPASS[round((deg % 360) / 22.5) % 16]
