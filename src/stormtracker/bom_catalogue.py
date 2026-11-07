"""What BOM publishes and where, so settings can be worked out from a location instead of looked up by hand.

* The station list (BOM product IDCJMC0014) gives each open station's position, state and WMO number. It is fetched on
  demand and cached under ``<data_dir>/cache``, never stored in the repository (BOM's terms for redistributing it are
  not confirmed, and it is 2.6 MB that changes slowly).
* The per-state product ids (observations and warnings feeds) are fixed by BOM and listed here.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import NamedTuple

import httpx

from .alerts.rules import haversine_km

STATIONS_URL = "https://reg.bom.gov.au/climate/data/lists_by_element/stations.txt"
CACHE_TTL_S = 30 * 86400
MIN_STATIONS = 100            # fewer than this parsed means the file changed shape, not that Australia lost its stations

# state -> warnings feed (RSS) and the letter used in its observation products (IDx60801 / IDx60901)
WARNING_FEEDS = {"NSW": "IDZ00054", "ACT": "IDZ00054", "NT": "IDZ00055", "QLD": "IDZ00056", "SA": "IDZ00057",
                 "TAS": "IDZ00058", "VIC": "IDZ00059", "WA": "IDZ00060"}
OBS_LETTER = {"NSW": "N", "ACT": "N", "NT": "D", "QLD": "Q", "SA": "S", "TAS": "T", "VIC": "V", "WA": "W"}
STATE_NAMES = {"NSW": "NSW and ACT", "ACT": "NSW and ACT", "NT": "NT", "QLD": "Queensland", "SA": "South Australia",
               "TAS": "Tasmania", "VIC": "Victoria", "WA": "Western Australia"}


def warnings_feed_url(state: str, base: str = "https://reg.bom.gov.au") -> str | None:
    fid = WARNING_FEEDS.get(state)
    return f"{base}/fwo/{fid}.warnings_{'nsw' if state == 'ACT' else state.lower()}.xml" if fid else None


def obs_products(state: str) -> list[str]:
    """Observation products to try for a station in ``state``, most likely first."""
    letter = OBS_LETTER.get(state)
    if not letter:
        return []
    out = [f"ID{letter}60801", f"ID{letter}60901"]
    if state == "ACT":
        out.insert(0, "IDN60903")
    return out


class Station(NamedTuple):
    site: str
    name: str
    lat: float
    lon: float
    state: str
    wmo: int | None


_LINE = re.compile(r"^\s*(?P<site>\d+)\s+\S+\s+(?P<name>.*?)\s+(?P<start>\d{4})\s+(?P<end>\d{4}|\.\.)\s+(?P<lat>-?\d+\.\d+)\s+"
                   r"(?P<lon>-?\d+\.\d+)\s+.{1,14}?\s+(?P<sta>[A-Z]{2,3})\s+\S+\s+\S+\s+(?P<wmo>\d+|\.\.)\s*$")


def parse_stations(text: str) -> list[Station]:
    """Open stations only (no end year). A line that doesn't parse is skipped rather than failing the list."""
    out = []
    for line in text.splitlines():
        m = _LINE.match(line)
        if m and m["end"] == "..":
            out.append(Station(m["site"], re.sub(r"\b(Aws|Amo|Raaf)\b", lambda x: x[0].upper(), m["name"].strip().title()), float(m["lat"]), float(m["lon"]), m["sta"],
                               int(m["wmo"]) if m["wmo"].isdigit() else None))
    return out


async def load_stations(http: httpx.AsyncClient, data_dir: Path | None, *, now: float | None = None) -> list[Station]:
    """The open-station list, from the cache while it is fresh, else downloaded (a stale cache beats no list)."""
    now = time.time() if now is None else now
    path = (data_dir / "cache" / "bom_stations.json") if data_dir else None
    cached: list[Station] | None = None
    if path and path.exists():
        try:
            blob = json.loads(path.read_text())
            cached = [Station(*x) for x in blob["stations"]]
            if now - blob["fetched"] < CACHE_TTL_S:
                return cached
        except (OSError, ValueError, TypeError, KeyError):
            cached = None
    try:
        r = await http.get(STATIONS_URL, timeout=60)
        r.raise_for_status()
        stations = parse_stations(r.text)
        if len(stations) < MIN_STATIONS:
            raise ValueError("BOM station list looked wrong")
    except (httpx.HTTPError, ValueError):
        if cached:
            return cached
        raise
    if path:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"fetched": now, "stations": stations}))
            tmp.replace(path)
        except OSError:
            pass
    return stations


def nearest(stations: list[Station], lat: float, lon: float, *, limit: int = 10, max_km: float = 200,
            need_wmo: bool = False) -> list[tuple[float, Station]]:
    ranked = sorted(((haversine_km(lat, lon, s.lat, s.lon), s) for s in stations if not need_wmo or s.wmo), key=lambda x: x[0])
    return [x for x in ranked[:limit] if x[0] <= max_km]
