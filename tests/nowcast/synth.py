"""Synthetic radar: Gaussian storm cells with known motion, so every expectation can be derived by hand."""
from datetime import UTC, datetime, timedelta

import numpy as np

from stormtracker.sources.bom_radar.georef import georef_for

G = georef_for("IDR713")          # 512 px, 500 m/px, radar at (256, 256)
T0 = datetime(2026, 11, 6, 10, 0, tzinfo=UTC)
PX_KM = G.pixel_size_m / 1000


def blob(cx, cy, amp=40.0, sx=12.0, sy=12.0, shape=(512, 512)):
    y, x = np.mgrid[0:shape[0], 0:shape[1]]
    return (amp * np.exp(-(((x - cx) / sx) ** 2 + ((y - cy) / sy) ** 2))).astype(np.float32)


def frame(cells, nan_outside=False):
    """cells: [(col, row, amp, sigma)] -> mm/h grid (tiny values zeroed like real radar)."""
    a = np.zeros((512, 512), np.float32)
    for cx, cy, amp, s in cells:
        a = np.maximum(a, blob(cx, cy, amp, s, s))
    a[a < 0.2] = 0
    if nan_outside:
        yy, xx = np.ogrid[:512, :512]
        a[(xx - 256) ** 2 + (yy - 256) ** 2 > 255 ** 2] = np.nan
    return a


def moving(n, start=(180, 256), v_px=(5, 0), amp=40.0, sigma=12.0, step_min=5):
    """n frames of one cell moving v_px pixels per step: [(ts, array)] oldest first."""
    return [(T0 + timedelta(minutes=step_min * k), frame([(start[0] + v_px[0] * k, start[1] + v_px[1] * k, amp, sigma)])) for k in range(n)]
