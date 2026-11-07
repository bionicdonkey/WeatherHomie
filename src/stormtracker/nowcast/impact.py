"""What the forecast means for one place: will it rain there, when, how hard, and how sure are we."""
from __future__ import annotations

import math
from datetime import datetime, timedelta

import numpy as np

from ..geo import latlon_to_pixel
from ..models import Georef
from .engine import Motion, clean, disc_mask
from .settings import NowcastSettings

# Rough share of forecast pixels that verify at each lead time, measured by replaying stored radar frames
# (see nowcast.hindcast / ``python -m stormtracker.cli hindcast``). Light rain (>= 1 mm/h) at 10, 30, 60, 90 min.
# Heavy rain (>= 10 mm/h) is far less predictable; the confidence below is capped for it.
SKILL_LEAD_MIN = [0, 10, 30, 60, 90]
SKILL_LIGHT = [1.00, 0.67, 0.48, 0.32, 0.22]
HEAVY_CAP = 0.45


def _interp(lead: float, ys: list[float]) -> float:
    return float(np.interp(lead, SKILL_LEAD_MIN, ys))


def confidence(lead_min: float, motion: Motion, peak_mmh: float | None, tracks_agree: bool | None = None) -> dict:
    """A rough 0..1 trust figure with the reasons. A heuristic, not a calibrated probability."""
    reasons: list[str] = []
    c = _interp(lead_min, SKILL_LIGHT)
    reasons.append(f"{lead_min:.0f} min ahead")
    if motion.method == "none":
        c *= 0.4
        reasons.append(motion.reason or "no motion estimate")
    else:
        if motion.frames_used < 3:
            c *= 0.75
            reasons.append("only two radar frames to measure motion")
        if motion.coherence < 0.5:
            c *= 0.75
            reasons.append("rain moving in different directions")
    if tracks_agree is False:
        c *= 0.8
        reasons.append("storm cell tracks disagree with the motion field")
    if peak_mmh is not None and peak_mmh >= 10:
        c = min(c, HEAVY_CAP) if lead_min >= 20 else c
        if lead_min >= 20:
            reasons.append("heavy rain is hard to place this far ahead")
    c = max(0.05, min(0.95, c))
    return {"value": round(c, 2), "level": "high" if c >= 0.66 else "medium" if c >= 0.4 else "low", "reasons": reasons}


def home_impact(now_arr: np.ndarray, fc: np.ndarray, g: Georef, lat: float, lon: float, frame_ts: datetime,
                motion: Motion, p: NowcastSettings, tracks_agree: bool | None = None) -> dict:
    try:
        col, row = latlon_to_pixel(g, lat, lon)
    except ValueError:
        return {"status": "unknown", "reason": "home is not covered by this radar"}
    if not (0 <= col < g.width and 0 <= row < g.height):
        return {"status": "unknown", "reason": "home is outside this radar's range"}
    mask = disc_mask(g, col, row, p.home_radius_km)
    now = clean(now_arr)
    now_mm = float(now[mask].max())
    series = [float(f[mask].max()) for f in fc]
    leads = [(k + 1) * p.step_min for k in range(len(series))]
    thr = p.arrival_mmh

    def at(lead_min: float) -> str:
        return (frame_ts + timedelta(minutes=lead_min)).isoformat()

    out: dict = {"now_mmh": round(now_mm, 1), "arrival_threshold_mmh": thr,
                 "series": [{"lead_min": l, "mmh": round(v, 1)} for l, v in zip(leads, series)],
                 "nearest_rain_km": None, "nearest_rain_bearing_deg": None}
    wet = now >= thr
    if wet.any():                                           # how far away is the closest rain right now
        rr, cc = np.nonzero(wet)
        d = np.hypot(cc - col, rr - row)
        i = int(d.argmin())
        out["nearest_rain_km"] = round(float(d[i]) * g.pixel_size_m / 1000, 1)
        out["nearest_rain_bearing_deg"] = round(math.degrees(math.atan2(cc[i] - col, -(rr[i] - row))) % 360)

    if now_mm >= thr:
        end = next((l for l, v in zip(leads, series) if v < thr), None)
        window = [now_mm] + [v for l, v in zip(leads, series) if end is None or l < end]
        peak = max(window)
        out.update(status="raining", eta_lead_min=0, peak_mmh=round(peak, 1), end_lead_min=end,
                   end_ts=at(end) if end is not None else None)
        out["confidence"] = confidence(end if end is not None else leads[-1], motion, peak, tracks_agree)
        return out
    k = next((i for i, v in enumerate(series) if v >= thr), None)
    if k is None:
        out.update(status="clear", confidence=confidence(leads[-1], motion, None, tracks_agree))
        return out
    stop = next((i for i in range(k, len(series)) if series[i] < thr), len(series))
    peak_i = max(range(k, stop), key=lambda i: series[i])
    peak = series[peak_i]
    out.update(status="approaching", eta_lead_min=leads[k], eta_ts=at(leads[k]), peak_mmh=round(peak, 1),
               peak_lead_min=leads[peak_i], peak_ts=at(leads[peak_i]),
               end_lead_min=leads[stop] if stop < len(series) else None,
               end_ts=at(leads[stop]) if stop < len(series) else None)
    out["confidence"] = confidence(leads[k], motion, peak, tracks_agree)
    return out


def station_cross_check(radar_says_wet: bool, obs_values: dict | None, obs_ts: datetime | None, now: datetime,
                        pressure_1h_ago: float | None, lightning: list[dict]) -> dict:
    """Does the local station agree with the radar? Only reports what is available."""
    out: dict = {"available": obs_values is not None}
    if obs_values is None or obs_ts is None:
        return out
    age_min = (now - obs_ts).total_seconds() / 60
    out["age_min"] = round(age_min)
    if age_min > 20:
        out.update(available=False, reason=f"latest station reading is {round(age_min)} min old")
        return out
    rate = obs_values.get("rain_rate_mmh")
    if rate is not None:
        out["station_rain_mmh"] = round(rate, 1)
        out["agrees_with_radar"] = (rate >= 0.1) == bool(radar_says_wet)
    p = obs_values.get("pressure_hpa")
    if p is not None and pressure_1h_ago is not None:
        out["pressure_change_1h_hpa"] = round(p - pressure_1h_ago, 1)
        out["pressure_falling_fast"] = (p - pressure_1h_ago) <= -2.0
    g = obs_values.get("wind_gust_ms")
    if g is not None:
        out["gust_kmh"] = round(g * 3.6)
        out["gusty"] = g * 3.6 >= 50
    out["lightning_within_10km_30min"] = sum(1 for e in lightning if (e.get("distance_km") is not None and e["distance_km"] <= 10))
    return out
