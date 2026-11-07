"""Ecowitt / Wunderground field mapping -> the common observation schema (SI-ish units).

Sources of the field names:
  * Ecowitt-protocol push and Wunderground-protocol push: names and units as used by the console
    firmware, cross-checked against the open-source ``aioecowitt`` sensor map (imperial by default:
    ``*f``, ``*in``, ``*mph``; metric variants end ``*c``, ``*mm``, ``*kmh``, ``*hpa``).
  * Gateway HTTP API ``get_livedata_info`` (Ecowitt "HTTP API interface Protocol" V1.0.6): values are
    strings carrying a unit, e.g. "79.2" + unit "F", "0.00 in/Hr".

Anything numeric that is not in the table is kept under ``x_<field>`` so extra sensors are never lost.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

# ---- unit conversions ------------------------------------------------------------------------
IN_MM = 25.4
INHG_HPA = 33.8638866667
MPH_MS = 0.44704
KMH_MS = 1 / 3.6
MI_KM = 1.609344

CONV: dict[str, Callable[[float], float]] = {
    "id": lambda x: x,
    "f2c": lambda x: (x - 32.0) / 1.8,
    "in2mm": lambda x: x * IN_MM,
    "inhg2hpa": lambda x: x * INHG_HPA,
    "mmhg2hpa": lambda x: x * 1.33322387415,
    "mph2ms": lambda x: x * MPH_MS,
    "kmh2ms": lambda x: x * KMH_MS,
    "kn2ms": lambda x: x * 0.514444,
    "mi2km": lambda x: x * MI_KM,
}

# Exact-name fields (Ecowitt push, Wunderground push): name -> (standard key, converter)
EXACT: dict[str, tuple[str, str]] = {
    # temperature / humidity
    "tempf": ("temp_c", "f2c"), "tempc": ("temp_c", "id"),
    "tempinf": ("temp_in_c", "f2c"), "tempinc": ("temp_in_c", "id"),
    "indoortempf": ("temp_in_c", "f2c"),
    "dewpointf": ("dewpoint_c", "f2c"), "dewpointc": ("dewpoint_c", "id"), "dewptf": ("dewpoint_c", "f2c"),
    "windchillf": ("windchill_c", "f2c"), "windchillc": ("windchill_c", "id"),
    "tempfeelsf": ("feels_like_c", "f2c"), "tempfeelsc": ("feels_like_c", "id"),
    "humidity": ("humidity_pct", "id"), "humidityin": ("humidity_in_pct", "id"),
    "indoorhumidity": ("humidity_in_pct", "id"),
    # pressure
    "baromrelin": ("pressure_hpa", "inhg2hpa"), "baromrelhpa": ("pressure_hpa", "id"),
    "baromin": ("pressure_hpa", "inhg2hpa"),
    "baromabsin": ("pressure_abs_hpa", "inhg2hpa"), "baromabshpa": ("pressure_abs_hpa", "id"),
    # wind
    "winddir": ("wind_dir_deg", "id"), "winddir_avg10m": ("wind_dir_avg10m_deg", "id"),
    "windspeedmph": ("wind_speed_ms", "mph2ms"), "windspeedkmh": ("wind_speed_ms", "kmh2ms"),
    "windgustmph": ("wind_gust_ms", "mph2ms"), "windgustkmh": ("wind_gust_ms", "kmh2ms"),
    "maxdailygust": ("wind_gust_max_daily_ms", "mph2ms"), "maxdailygustkmh": ("wind_gust_max_daily_ms", "kmh2ms"),
    "windspdmph_avg10m": ("wind_speed_avg10m_ms", "mph2ms"), "windspdkmh_avg10m": ("wind_speed_avg10m_ms", "kmh2ms"),
    # rain (tipping bucket) -- Wunderground "rainin" is the last-hour total
    "rainratein": ("rain_rate_mmh", "in2mm"), "rainratemm": ("rain_rate_mmh", "id"),
    "eventrainin": ("rain_event_mm", "in2mm"), "eventrainmm": ("rain_event_mm", "id"),
    "hourlyrainin": ("rain_hourly_mm", "in2mm"), "hourlyrainmm": ("rain_hourly_mm", "id"),
    "rainin": ("rain_hourly_mm", "in2mm"),
    "dailyrainin": ("rain_daily_mm", "in2mm"), "dailyrainmm": ("rain_daily_mm", "id"),
    "weeklyrainin": ("rain_weekly_mm", "in2mm"), "weeklyrainmm": ("rain_weekly_mm", "id"),
    "monthlyrainin": ("rain_monthly_mm", "in2mm"), "monthlyrainmm": ("rain_monthly_mm", "id"),
    "yearlyrainin": ("rain_yearly_mm", "in2mm"), "yearlyrainmm": ("rain_yearly_mm", "id"),
    "totalrainin": ("rain_total_mm", "in2mm"), "totalrainmm": ("rain_total_mm", "id"),
    # rain (piezo, e.g. WS90)
    "rrain_piezo": ("rain_rate_piezo_mmh", "in2mm"), "rrain_piezomm": ("rain_rate_piezo_mmh", "id"),
    "erain_piezo": ("rain_event_piezo_mm", "in2mm"), "erain_piezomm": ("rain_event_piezo_mm", "id"),
    "hrain_piezo": ("rain_hourly_piezo_mm", "in2mm"), "hrain_piezomm": ("rain_hourly_piezo_mm", "id"),
    "drain_piezo": ("rain_daily_piezo_mm", "in2mm"), "drain_piezomm": ("rain_daily_piezo_mm", "id"),
    "wrain_piezo": ("rain_weekly_piezo_mm", "in2mm"), "wrain_piezomm": ("rain_weekly_piezo_mm", "id"),
    "mrain_piezo": ("rain_monthly_piezo_mm", "in2mm"), "mrain_piezomm": ("rain_monthly_piezo_mm", "id"),
    "yrain_piezo": ("rain_yearly_piezo_mm", "in2mm"), "yrain_piezomm": ("rain_yearly_piezo_mm", "id"),
    # sun
    "solarradiation": ("solar_wm2", "id"), "uv": ("uv_index", "id"), "UV": ("uv_index", "id"),
    "solarradiation_lux": ("solar_lux", "id"),
    # lightning sensor (events are built separately from lightning_time)
    "lightning": ("lightning_distance_km", "id"), "lightning_mi": ("lightning_distance_km", "mi2km"),
    "lightning_num": ("lightning_count", "id"),
    # misc
    "AqPM2.5": ("pm25_ugm3", "id"), "soilmoisture": ("soil_moisture_pct", "id"), "soiltempf": ("soil_temp_c", "f2c"),
}

# Numbered extra sensors: regex -> (key template, converter)
PATTERNS: list[tuple[re.Pattern[str], str, str]] = [
    (re.compile(r"^temp(\d+)f$"), "temp_c_{0}", "f2c"),
    (re.compile(r"^temp(\d+)c$"), "temp_c_{0}", "id"),
    (re.compile(r"^humidity(\d+)$"), "humidity_pct_{0}", "id"),
    (re.compile(r"^soilmoisture(\d+)$"), "soil_moisture_pct_{0}", "id"),
    (re.compile(r"^soiltemp(\d+)f$"), "soil_temp_c_{0}", "f2c"),
    (re.compile(r"^pm25_ch(\d+)$"), "pm25_ugm3_{0}", "id"),
    (re.compile(r"^leafwetness_ch(\d+)$"), "leaf_wetness_pct_{0}", "id"),
    (re.compile(r"^leak_ch(\d+)$"), "leak_{0}", "id"),
    (re.compile(r"^dewpoint(\d+)f$"), "dewpoint_c_{0}", "f2c"),
]

# Fields that describe the upload, not the weather
METADATA = {"PASSKEY", "stationtype", "model", "freq", "dateutc", "runtime", "interval", "heap", "fields",
            "ID", "PASSWORD", "action", "realtime", "rtfreq", "softwaretype", "lightning_time", "tempunit", "id"}


def _num(v: Any) -> float | None:
    try:
        f = float(str(v).strip().rstrip("%"))
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) != float("inf") else None


def _slug(k: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", k.lower()).strip("_")


def convert_push_fields(fields: dict[str, str]) -> dict[str, float]:
    """Ecowitt- or Wunderground-protocol form/query fields -> common observation values."""
    out: dict[str, float] = {}
    for name, raw in fields.items():
        if name in METADATA:
            continue
        v = _num(raw)
        if v is None:
            continue
        if name in EXACT:
            key, conv = EXACT[name]
        else:
            for rx, tmpl, c in PATTERNS:
                m = rx.match(name)
                if m:
                    key, conv = tmpl.format(*m.groups()), c
                    break
            else:
                key, conv = "x_" + _slug(name), "id"
        if not key or key == "x_":
            continue
        out[key] = round(CONV[conv](v), 4)
    return out


# ---- gateway HTTP API (get_livedata_info) ---------------------------------------------------

_UNIT_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*([A-Za-z/%²0-9°]*)\s*$")
_UNIT_CONV = {  # lower-cased unit token -> (converter, family)
    "f": "f2c", "c": "id", "℃": "id", "℉": "f2c",
    "in": "in2mm", "mm": "id", "in/hr": "in2mm", "mm/hr": "id", "in/h": "in2mm", "mm/h": "id",
    "mph": "mph2ms", "m/s": "id", "km/h": "kmh2ms", "knots": "kn2ms", "kn": "kn2ms",
    "inhg": "inhg2hpa", "hpa": "id", "mmhg": "mmhg2hpa",
    "km": "id", "mi": "mi2km", "": "id", "%": "id",
}

# id -> (key, kind) ; kind picks the conversion family when the unit text is absent
LIVE_IDS = {
    "0x02": "temp_c", "0x03": "dewpoint_c", "0x04": "windchill_c", "0x05": "heat_index_c",
    "3": "feels_like_c", "0x07": "humidity_pct", "0x0A": "wind_dir_deg", "0x0B": "wind_speed_ms",
    "0x0C": "wind_gust_ms", "0x19": "wind_gust_max_daily_ms", "0x17": "uv_index",
}
SOLAR_ID = "0x15"
RAIN_IDS = {"0x0D": "rain_event_mm", "0x0E": "rain_rate_mmh", "0x10": "rain_daily_mm", "0x11": "rain_weekly_mm",
            "0x12": "rain_monthly_mm", "0x13": "rain_yearly_mm"}


def parse_value(val: Any, unit: str | None = None) -> float | None:
    """'79.2' + unit 'F', '0.00 in/Hr', '65%', '29.40 inHg' -> SI-ish float."""
    if val is None:
        return None
    m = _UNIT_RE.match(str(val))
    if not m:
        return None
    num, inline = float(m.group(1)), m.group(2)
    token = (inline or unit or "").lower()
    conv = _UNIT_CONV.get(token)
    if conv is None:
        return None
    return round(CONV[conv](num), 4)


def convert_livedata(j: dict[str, Any]) -> dict[str, float]:
    """Gateway ``get_livedata_info`` JSON -> common observation values."""
    out: dict[str, float] = {}

    def put(key: str, v: float | None) -> None:
        if v is not None:
            out[key] = v

    for it in j.get("common_list", []):
        i = str(it.get("id", ""))
        i = i if i.startswith("0x") or i == "3" else i
        if i in LIVE_IDS:
            put(LIVE_IDS[i], parse_value(it.get("val"), it.get("unit")))
        elif i == SOLAR_ID:
            raw = str(it.get("val", ""))
            m = _UNIT_RE.match(raw)
            if m and m.group(2).lower() in ("w/m2", "w/m²"):
                put("solar_wm2", float(m.group(1)))
            elif m and m.group(2).lower() == "lux":
                put("solar_lux", float(m.group(1)))
    for block, suffix in (("rain", ""), ("piezoRain", "_piezo")):
        for it in j.get(block, []):
            key = RAIN_IDS.get(str(it.get("id", "")))
            if key:
                k = key.replace("_mmh", suffix + "_mmh") if key.endswith("_mmh") else key.replace("_mm", suffix + "_mm")
                put(k, parse_value(it.get("val"), it.get("unit")))
    for w in j.get("wh25", []):
        put("temp_in_c", parse_value(w.get("intemp"), w.get("unit")))
        put("humidity_in_pct", parse_value(w.get("inhumi")))
        put("pressure_abs_hpa", parse_value(w.get("abs")))
        put("pressure_hpa", parse_value(w.get("rel")))
    for c in j.get("ch_aisle", []):
        n = c.get("channel")
        put(f"temp_c_{n}", parse_value(c.get("temp"), c.get("unit")))
        put(f"humidity_pct_{n}", parse_value(c.get("humidity")))
    for c in j.get("ch_temp", []):
        put(f"probe_temp_c_{c.get('channel')}", parse_value(c.get("temp"), c.get("unit")))
    for c in j.get("ch_soil", []):
        put(f"soil_moisture_pct_{c.get('channel')}", parse_value(c.get("humidity")))
    for c in j.get("ch_pm25", []):
        put(f"pm25_ugm3_{c.get('channel')}", _num(c.get("PM25")))
    for lt in j.get("lightning", []):
        put("lightning_distance_km", parse_value(lt.get("distance")))
        put("lightning_count", _num(lt.get("count")))
    return out
