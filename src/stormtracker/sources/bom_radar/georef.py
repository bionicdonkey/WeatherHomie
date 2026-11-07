"""Radar geometry.

BOM single-site images are 512x512 px, gnomonic projection centred on the radar (BOM publishes this
in radar_transparencies/coordinates/IDRnnnx.map); product suffix 1/2/3/4 = 512/256/128/64 km radius.
Verified on IDR713 by fitting BOM's own range-ring overlay: ring centre = pixel (256.0, 256.0),
100 km ring radius = 199.99 px => 500 m/px.
"""
from __future__ import annotations

import re

from ...models import Georef
from .sites_table import SITES

IMAGE_PX = 512
RANGE_KM = {"1": 512, "2": 256, "3": 128, "4": 64}

# Radar site coordinates (WGS84). Add entries here or override per source in settings.
RADAR_SITES: dict[str, tuple[float, float]] = {
    "IDR71": (-33.7008, 151.2095),   # Sydney (Terrey Hills); from BOM IDR713.map center_latitude/longitude
}


def parse_radar_id(radar_id: str) -> tuple[str, int]:
    m = re.fullmatch(r"IDR(\d{2,3})([1-4])", radar_id)
    if not m:
        raise ValueError(f"unsupported radar id {radar_id!r} (expected IDRnnR with R in 1-4, e.g. IDR713)")
    return f"IDR{m.group(1)}", RANGE_KM[m.group(2)]


def georef_for(radar_id: str, lat: float | None = None, lon: float | None = None) -> Georef:
    site, range_km = parse_radar_id(radar_id)
    if lat is None or lon is None:
        if site in RADAR_SITES:
            lat, lon = RADAR_SITES[site]
        elif site in SITES:                      # BOM's published position: good to about 1 km (2 px on the image)
            lat, lon = SITES[site][2:]
        else:
            raise ValueError(f"unknown radar site {site}; set radar_lat/radar_lon in the source settings")
    return Georef(lat0=lat, lon0=lon, pixel_size_m=range_km * 2000 / IMAGE_PX, width=IMAGE_PX, height=IMAGE_PX)


# ---- projection helpers live in the core (shared with the map renderer) ---------------------------
from ...geo import latlon_to_pixel, pixel_to_latlon  # noqa: E402,F401
