"""Turn stored rain-rate grids / overlay images into map-ready PNGs (Web Mercator, transparent)."""
from __future__ import annotations

import io
import math
from pathlib import Path

import numpy as np
from PIL import Image

from .geo import grid_bounds, latlon_to_pixel, mercator_grid, sample_nearest
from .models import Georef
from .sources.bom_radar.palette import RAIN_CLASSES

# Display scale: BOM's familiar light->heavy colours, thresholds are the (approximate) class lower bounds.
# BOM's lightest class is near-white (245,245,255), which vanishes on a light basemap, so it is drawn as a
# visible pale blue; the order stays light -> heavy. The legend uses the same table.
RENDER_VERSION = 2                      # bump when colours/projection change: cached PNGs are keyed by it
_DISPLAY = [c for c, _ in RAIN_CLASSES]
_DISPLAY[0] = (205, 212, 255)
LOWS = np.array([lo for _, lo in RAIN_CLASSES], dtype=np.float32)
COLOURS = np.array(_DISPLAY, dtype=np.uint8)
ALPHA = 215
OUT_WIDTH = 640


def legend() -> list[dict]:
    return [{"min_mm_h": float(lo), "colour": "#%02x%02x%02x" % tuple(int(x) for x in c)}
            for c, lo in zip(_DISPLAY, [lo for _, lo in RAIN_CLASSES])]


def colourise(mmh: np.ndarray) -> np.ndarray:
    """(H, W) mm/h -> (H, W, 4) uint8. NaN and anything under the lightest class are transparent."""
    idx = np.digitize(np.nan_to_num(mmh, nan=0.0), LOWS) - 1        # -1 => below the lightest class
    rgba = np.zeros(mmh.shape + (4,), np.uint8)
    hit = idx >= 0
    rgba[hit, :3] = COLOURS[idx[hit]]
    rgba[hit, 3] = ALPHA
    return rgba


def _png(rgba: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(buf, "PNG", optimize=True)
    return buf.getvalue()


def render_frame_png(arr: np.ndarray, g: Georef) -> tuple[bytes, tuple[float, float, float, float]]:
    bounds, col, row = mercator_grid(g, OUT_WIDTH)
    mm = sample_nearest(arr, col, row, fill=np.nan)
    return _png(colourise(mm)), bounds


def render_overlay_png(src_png: bytes, g: Georef) -> tuple[bytes, tuple[float, float, float, float]]:
    img = np.asarray(Image.open(io.BytesIO(src_png)).convert("RGBA"))
    if img.shape[:2] != (g.height, g.width):
        raise ValueError(f"overlay is {img.shape[1]}x{img.shape[0]}, expected {g.width}x{g.height}")
    bounds, col, row = mercator_grid(g, OUT_WIDTH)
    out = sample_nearest(img, col, row, fill=0).astype(np.uint8)
    return _png(out), bounds


def bounds_for(g: Georef) -> tuple[float, float, float, float]:
    return grid_bounds(g)


def frame_stats(arr: np.ndarray, g: Georef, lat: float, lon: float, radius_km: float = 25.0) -> dict:
    """Rain at a point: value at the nearest pixel (max of the 3x3 around it) and the max within radius."""
    try:
        c, r = latlon_to_pixel(g, lat, lon)
    except ValueError:
        return {"at_point_mm_h": None, "max_within_mm_h": None, "radius_km": radius_km}
    ci, ri = int(round(c)), int(round(r))
    if not (0 <= ci < g.width and 0 <= ri < g.height):
        return {"at_point_mm_h": None, "max_within_mm_h": None, "radius_km": radius_km}
    win = arr[max(0, ri - 1): ri + 2, max(0, ci - 1): ci + 2]
    at = float(np.nanmax(win)) if np.isfinite(win).any() else None
    rad_px = radius_km * 1000 / g.pixel_size_m
    yy, xx = np.ogrid[: g.height, : g.width]
    mask = (xx - c) ** 2 + (yy - r) ** 2 <= rad_px ** 2
    vals = arr[mask]
    mx = float(np.nanmax(vals)) if np.isfinite(vals).any() else None
    rnd = lambda v: None if v is None else round(v, 2)   # noqa: E731
    return {"at_point_mm_h": rnd(at), "max_within_mm_h": rnd(mx), "radius_km": radius_km}
