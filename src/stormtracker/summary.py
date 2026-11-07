"""One self-describing picture of the weather at home, for agents and other apps.

``build(core, sections)`` is the single place that turns stored readings, the radar nowcast, warnings and the model forecast
into plain JSON. ``GET /api/now`` and the MCP tools both call it, so they cannot disagree. Every number carries its unit
(a ``units`` map beside each block of values), a timestamp, and where it came from.

It only reads what StormTracker already holds. Nothing here fetches from the network or writes anything.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

from .alerts.rules import haversine_km
from .storage.tables import EventRow, ObservationRow
from .timeutil import ensure_utc, utcnow

SECTIONS = ("location", "conditions", "rain", "storm_outlook", "lightning", "warnings", "forecast", "alerts", "data_freshness")

AUTHORITY = ("Radar and model figures are estimates for one place, not official warnings. The Bureau of Meteorology (bom.gov.au) is the "
             "authority for severe weather warnings: if a warning is listed here, follow it, and if one is expected but missing, check BOM directly.")

UNITS = {
    "temp_c": "°C", "apparent_temp_c": "°C", "dewpoint_c": "°C", "temp_in_c": "°C", "delta_t_c": "°C",
    "humidity_pct": "%", "humidity_in_pct": "%", "pressure_hpa": "hPa", "pressure_abs_hpa": "hPa", "pressure_station_hpa": "hPa",
    "wind_speed_ms": "m/s", "wind_gust_ms": "m/s", "wind_speed_kmh": "km/h", "wind_gust_kmh": "km/h",
    "wind_dir_deg": "° (direction the wind comes from)", "wind_dir_avg10m_deg": "° (direction the wind comes from)",
    "wind_gust_max_daily_ms": "m/s", "rain_rate_mmh": "mm/h", "rain_hourly_mm": "mm", "rain_daily_mm": "mm", "rain_event_mm": "mm",
    "rain_weekly_mm": "mm", "rain_monthly_mm": "mm", "rain_yearly_mm": "mm", "rain_since_9am_mm": "mm",
    "solar_wm2": "W/m²", "uv_index": "index", "visibility_km": "km", "cloud_oktas": "oktas",
    "lightning_count": "strikes (sensor's running count)", "lightning_distance_km": "km (distance of the sensor's last strike)",
}
OWN_SOURCES = ("ecowitt", "ecowitt_cloud", "wunderground_pws")      # a station you own, as opposed to an official BOM one
STALE_MIN = 100


def _units_for(values: dict[str, Any]) -> dict[str, str]:
    return {k: UNITS[k] for k in values if k in UNITS}


def _clean(values: dict[str, Any]) -> dict[str, Any]:
    v = {k: x for k, x in (values or {}).items() if not k.startswith("x_") and isinstance(x, (int, float)) and not isinstance(x, bool)}
    for ms, kmh in (("wind_speed_ms", "wind_speed_kmh"), ("wind_gust_ms", "wind_gust_kmh")):
        if ms in v:
            v[kmh] = round(v[ms] * 3.6, 1)
    return v


def _age_min(ts: datetime, now: datetime) -> int:
    return max(0, round((now - ts).total_seconds() / 60))


async def _home(core) -> dict[str, Any] | None:
    locs = await core.locations.list()
    return locs[0].model_dump() if locs else None


async def conditions(core, home, now, limit: int = 3) -> list[dict[str, Any]]:
    async with core.db.session() as s:
        keys = (await s.execute(select(ObservationRow.source, ObservationRow.station_id).distinct())).all()
        out = []
        for src, sid in keys:
            o = (await s.execute(select(ObservationRow).where(ObservationRow.source == src, ObservationRow.station_id == sid)
                                 .order_by(ObservationRow.ts.desc()).limit(1))).scalars().first()
            if o is None:
                continue
            v = _clean(o.values)
            km = round(haversine_km(home["lat"], home["lon"], o.lat, o.lon), 1) if home else None
            age = _age_min(o.ts, now)
            out.append({"station_id": sid, "source": src, "own_station": src in OWN_SOURCES, "distance_from_home_km": km,
                        "observed_at": o.ts.isoformat(), "age_min": age, "stale": age > STALE_MIN, "values": v, "units": _units_for(v)})
    out.sort(key=lambda x: (not x["own_station"], x["stale"], x["distance_from_home_km"] if x["distance_from_home_km"] is not None else 1e9))
    return out[:limit]


def _nowcast(core) -> dict[str, Any]:
    return core.nowcast.public() if core.nowcast else {"available": False}


def rain(nc: dict[str, Any], stations: list[dict[str, Any]]) -> dict[str, Any]:
    imp = nc.get("impact") or {}
    own = next((s for s in stations if s["own_station"] and not s["stale"]), None)
    out: dict[str, Any] = {"radar_status": imp.get("status", "unknown"),
                           "summary": _rain_sentence(imp), "units": {"now_mmh": "mm/h", "peak_mmh": "mm/h", "eta_lead_min": "minutes from the radar frame"}}
    for k in ("now_mmh", "eta_lead_min", "eta_ts", "peak_mmh", "peak_ts", "end_lead_min", "end_ts", "nearest_rain_km", "nearest_rain_bearing_deg"):
        if imp.get(k) is not None:
            out[k] = imp[k]
    if imp.get("confidence"):
        out["confidence"] = imp["confidence"]
    if nc.get("frame_ts"):
        out["radar_frame_at"] = nc["frame_ts"]
    if own:
        v = own["values"]
        out["station"] = {k: v[k] for k in ("rain_rate_mmh", "rain_hourly_mm", "rain_daily_mm") if k in v} | {"station_id": own["station_id"], "observed_at": own["observed_at"]}
    if not nc.get("available"):
        out["note"] = nc.get("reason") or "radar nowcast is not available right now"
    return out


def _rain_sentence(imp: dict[str, Any]) -> str:
    st = imp.get("status")
    if st == "raining":
        end = imp.get("end_lead_min")
        return f"Raining at home now ({imp.get('now_mmh')} mm/h)" + (f", expected to ease in about {end} minutes." if end else ", and the radar sees no end within the forecast window.")
    if st == "approaching":
        return f"Rain is expected at home in about {imp.get('eta_lead_min')} minutes (peak about {imp.get('peak_mmh')} mm/h)."
    if st == "clear":
        return "No rain is forecast to reach home in the radar forecast window."
    return imp.get("reason") or "No radar-based answer is available right now."


def storm_outlook(nc: dict[str, Any], cells_limit: int = 5) -> dict[str, Any]:
    if not nc.get("available"):
        return {"available": False, "reason": nc.get("reason") or "no radar nowcast right now"}
    cells = []
    for c in nc.get("cells") or []:
        h = c.get("home") or {}
        cells.append({"id": c["id"], "lat": c["lat"], "lon": c["lon"], "max_mmh": c["max_mmh"], "area_km2": c["area_km2"],
                      "speed_kmh": c["speed_kmh"], "heading_deg": c["bearing_deg"], "age_min": c["age_min"],
                      "intensifying_mmh": c.get("intensity_change_mmh"), "relative_to_home": h or None})
    cells.sort(key=lambda c: (c["relative_to_home"] or {}).get("distance_km", 1e9))
    m = nc.get("motion") or {}
    return {"available": True, "radar_id": nc.get("radar_id"), "radar_frame_at": nc.get("frame_ts"), "horizon_min": nc.get("horizon_min"),
            "motion": {"speed_kmh": m.get("speed_kmh"), "heading_deg": m.get("bearing_deg"), "method": m.get("method"), "coherence": m.get("coherence")},
            "tracked_cells": cells[:cells_limit], "tracked_cell_count": len(cells),
            "units": {"max_mmh": "mm/h", "area_km2": "km²", "speed_kmh": "km/h", "heading_deg": "° (direction the cell is moving towards)", "age_min": "minutes tracked"},
            "note": "Cells are rain areas tracked on radar. relative_to_home (when present) is the tracker's own estimate of how each cell moves relative to home."}


async def lightning(core, stations, now) -> dict[str, Any]:
    since = now - timedelta(minutes=60)
    async with core.db.session() as s:
        evs = (await s.execute(select(EventRow).where(EventRow.event_type == "lightning", EventRow.ts >= since).order_by(EventRow.ts.desc()).limit(500))).scalars().all()
    dist = [d for e in evs if (d := (e.properties or {}).get("distance_km")) is not None]
    own = next((s for s in stations if s["own_station"]), None)
    v = (own or {}).get("values", {})
    return {"strikes_last_60_min": len(evs), "nearest_km": round(min(dist), 1) if dist else None,
            "latest_at": evs[0].ts.isoformat() if evs else None,
            "within_10km_last_30_min": sum(1 for e in evs if e.ts >= now - timedelta(minutes=30) and (e.properties or {}).get("distance_km") is not None and e.properties["distance_km"] <= 10),
            "sensor": {"station_id": own["station_id"], "count": v.get("lightning_count"), "last_strike_distance_km": v.get("lightning_distance_km")} if own and "lightning_count" in v else None,
            "units": {"nearest_km": "km"},
            "note": "Strikes come from your own station's sensor (distance only, up to roughly 40 km) and any lightning feed you have connected."}


async def warnings(core, now) -> dict[str, Any]:
    async with core.db.session() as s:
        evs = (await s.execute(select(EventRow).where(EventRow.event_type == "warning", EventRow.ts >= now - timedelta(days=2)).order_by(EventRow.ts.desc()).limit(100))).scalars().all()
    items = []
    for e in evs:
        if e.expires and ensure_utc(e.expires) < now:
            continue
        p = e.properties or {}
        items.append({"title": p.get("title"), "category": p.get("category"), "for_your_area": bool(p.get("in_area")),
                      "severe_thunderstorm": bool(p.get("severe_thunderstorm")), "issued_at": e.ts.isoformat(), "link": p.get("link"), "source": e.source})
    return {"in_force": items, "count": len(items), "for_your_area_count": sum(1 for i in items if i["for_your_area"]),
            "note": "These are the Bureau of Meteorology's own warnings as published, matched against your area words. 'for_your_area' is a text match, so check the link for the precise area."}


async def forecast(core, now, hours: int = 12) -> dict[str, Any]:
    async with core.db.session() as s:
        rows = (await s.execute(select(EventRow).where(EventRow.external_id.like("forecast:%")).order_by(EventRow.ts.desc()))).scalars().all()
    for e in rows:
        p = e.properties or {}
        if p.get("kind") != "model_forecast":
            continue
        nxt = []
        for h in p.get("hours") or []:
            t = datetime.fromisoformat(h["t"])
            if now - timedelta(minutes=59) <= t <= now + timedelta(hours=hours):
                nxt.append({k: h.get(k) for k in ("t", "temp_c", "precip_prob_pct", "precip_mm", "gust_10m_ms", "wind_10m_ms", "wind_dir_10m_deg", "cape_jkg", "lifted_index", "shear_ms")})
        return {"model": p.get("model"), "issued": p.get("issued"), "attribution": p.get("attribution"), "summary_next_12h": p.get("summary"),
                "next_hours": nxt, "units": {"temp_c": "°C", "precip_prob_pct": "%", "precip_mm": "mm", "gust_10m_ms": "m/s", "wind_10m_ms": "m/s",
                                             "wind_dir_10m_deg": "° (direction the wind comes from)", "cape_jkg": "J/kg", "lifted_index": "K", "shear_ms": "m/s"},
                "note": "A weather model's guess for a grid square at 10 m height, not a measurement. Local gusts can be stronger."}
    return {"available": False, "note": "no model forecast stored (turn on the open_meteo source)"}


async def build(core, sections: tuple[str, ...] | list[str] | None = None) -> dict[str, Any]:
    want = [s for s in (sections or SECTIONS) if s in SECTIONS]
    now = utcnow()
    home = await _home(core)
    out: dict[str, Any] = {"generated_at": now.isoformat(), "timezone": "Australia/Sydney (all timestamps here are UTC)", "authority": AUTHORITY}
    nc = _nowcast(core) if {"rain", "storm_outlook"} & set(want) else {}
    stations = await conditions(core, home, now) if {"conditions", "rain", "lightning"} & set(want) else []
    for sec in want:
        if sec == "location":
            out["location"] = home or {"note": "no home location set"}
        elif sec == "conditions":
            out["conditions"] = stations
        elif sec == "rain":
            out["rain"] = rain(nc, stations)
        elif sec == "storm_outlook":
            out["storm_outlook"] = storm_outlook(nc)
        elif sec == "lightning":
            out["lightning"] = await lightning(core, stations, now)
        elif sec == "warnings":
            out["warnings"] = await warnings(core, now)
        elif sec == "forecast":
            out["forecast"] = await forecast(core, now)
        elif sec == "alerts":
            out["alerts"] = {"active": core.alerts.active() if core.alerts else []}
        elif sec == "data_freshness":
            st = await core.scheduler.status()
            out["data_freshness"] = {n: {"state": h.get("state"), "last_success": h.get("last_success")} for n, h in st.items() if h.get("state") != "disabled"}
    return out
