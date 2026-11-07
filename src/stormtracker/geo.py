"""Projection maths for gridded data (gnomonic grids as published for BOM radar) and Web Mercator.

Scalar and numpy-vectorised forms share one implementation so the map overlay is exactly as
accurate as the stored georeference.
"""
from __future__ import annotations

import math

import numpy as np

from .models import Georef

R = 6371000.0   # sphere radius, m (BOM corner coordinates fit this to <1 px at 500 m/px)
MAX_MERCATOR_LAT = 85.0511


def latlon_to_pixel_np(g: Georef, lat, lon):
    """Vectorised: lat/lon arrays (deg) -> fractional (col, row). Far-side points give NaN."""
    p0 = math.radians(g.lat0)
    p, dl = np.radians(np.asarray(lat, float)), np.radians(np.asarray(lon, float) - g.lon0)
    cosc = math.sin(p0) * np.sin(p) + math.cos(p0) * np.cos(p) * np.cos(dl)
    with np.errstate(divide="ignore", invalid="ignore"):
        x = R * np.cos(p) * np.sin(dl) / cosc
        y = R * (math.cos(p0) * np.sin(p) - math.sin(p0) * np.cos(p) * np.cos(dl)) / cosc
    far = cosc <= 0
    col = g.width / 2 + x / g.pixel_size_m
    row = g.height / 2 - y / g.pixel_size_m
    return np.where(far, np.nan, col), np.where(far, np.nan, row)


def latlon_to_pixel(g: Georef, lat: float, lon: float) -> tuple[float, float]:
    """Return fractional (col, row) in the image for a lat/lon."""
    col, row = latlon_to_pixel_np(g, lat, lon)
    if math.isnan(float(col)):
        raise ValueError("point is on the far side of the projection")
    return float(col), float(row)


def pixel_to_latlon(g: Georef, col: float, row: float) -> tuple[float, float]:
    x = (col - g.width / 2) * g.pixel_size_m
    y = (g.height / 2 - row) * g.pixel_size_m
    rho = math.hypot(x, y)
    if rho < 1e-9:
        return g.lat0, g.lon0
    c = math.atan(rho / R)
    p0 = math.radians(g.lat0)
    lat = math.asin(math.cos(c) * math.sin(p0) + y * math.sin(c) * math.cos(p0) / rho)
    lon = math.radians(g.lon0) + math.atan2(
        x * math.sin(c), rho * math.cos(p0) * math.cos(c) - y * math.sin(p0) * math.sin(c))
    return math.degrees(lat), math.degrees(lon)


# ---- Web Mercator -------------------------------------------------------------------------------
def merc_y(lat):
    lat = np.clip(np.asarray(lat, float), -MAX_MERCATOR_LAT, MAX_MERCATOR_LAT)
    return np.log(np.tan(np.pi / 4 + np.radians(lat) / 2))


def merc_lat(y):
    return np.degrees(2 * np.arctan(np.exp(np.asarray(y, float))) - np.pi / 2)


def grid_bounds(g: Georef) -> tuple[float, float, float, float]:
    """(south, west, north, east) of the grid: extremes over the whole edge, not just the corners
    (a gnomonic edge is curved in lat/lon)."""
    n = 33
    t = np.linspace(0, 1, n)
    cols = np.concatenate([t * g.width, t * g.width, np.zeros(n), np.full(n, g.width)])
    rows = np.concatenate([np.zeros(n), np.full(n, g.height), t * g.height, t * g.height])
    pts = [pixel_to_latlon(g, c, r) for c, r in zip(cols, rows)]
    lats, lons = zip(*pts)
    return min(lats), min(lons), max(lats), max(lons)


def mercator_grid(g: Georef, width: int = 640):
    """Output grid uniform in Web Mercator, which is what Leaflet's image overlay stretches linearly.

    Returns (bounds, col, row): ``col``/``row`` are float arrays (H_out, W_out) giving, for each output
    pixel centre, the fractional source pixel in the gnomonic grid (NaN outside it).
    """
    south, west, north, east = grid_bounds(g)
    y0, y1 = float(merc_y(south)), float(merc_y(north))
    dlon = math.radians(east - west)
    height = max(1, round(width * (y1 - y0) / dlon))
    lon = west + (np.arange(width) + 0.5) / width * (east - west)
    lat = merc_lat(y1 - (np.arange(height) + 0.5) / height * (y1 - y0))
    lon2, lat2 = np.meshgrid(lon, lat)
    col, row = latlon_to_pixel_np(g, lat2, lon2)
    outside = (col < 0) | (col >= g.width) | (row < 0) | (row >= g.height)
    col, row = np.where(outside, np.nan, col), np.where(outside, np.nan, row)
    return (south, west, north, east), col, row


def sample_nearest(src: np.ndarray, col: np.ndarray, row: np.ndarray, fill=np.nan):
    """Nearest-neighbour resample of ``src`` (H, W[, C]) at fractional (col, row); NaN coords -> fill."""
    ok = ~np.isnan(col)
    ci = np.where(ok, col, 0).astype(int)
    ri = np.where(ok, row, 0).astype(int)
    out = src[ri, ci]
    if src.ndim == 2:
        return np.where(ok, out, fill)
    return np.where(ok[..., None], out, fill)
