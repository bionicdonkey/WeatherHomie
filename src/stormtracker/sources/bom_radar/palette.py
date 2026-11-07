"""BOM radar colour palette -> rain rate (mm/h).

WHAT IS VERIFIED
  * The 15 rain colours and their light->heavy ORDER, taken from BOM's own legend
    (radar_transparencies/IDR.legend.0.png) and cross-checked against real frames.
  * Non-rain pixels: fully transparent = no echo; opaque grey (192,192,192) is the header bar and
    opaque black (0,0,0) is annotation text. These are NOT data and map to NaN.

WHAT IS ASSUMED
  * BOM's rain-rate legend carries NO numeric values (only "Light / Moderate / Heavy"). The lower
    bounds below are the commonly used approximate class edges, NOT an official BOM table. Treat
    mm/h values as approximate (class resolution, not measurement). They can be overridden per
    source in settings (``class_rates_mmh``) if you obtain an authoritative table (e.g. a
    licensed BOM feed).

Representative rate for a class = geometric mean of its lower and upper edge (top class: 1.25x lower).
"""
from __future__ import annotations

import numpy as np

# (R, G, B), lower bound mm/h -- ordered light -> heavy exactly as in BOM's legend.
RAIN_CLASSES: list[tuple[tuple[int, int, int], float]] = [
    ((245, 245, 255), 0.2),
    ((180, 180, 255), 0.5),
    ((120, 120, 255), 1.5),
    ((20, 20, 255), 2.5),
    ((0, 216, 195), 4.0),
    ((0, 150, 144), 6.0),
    ((0, 102, 102), 10.0),
    ((255, 255, 0), 15.0),
    ((255, 200, 0), 20.0),
    ((255, 150, 0), 30.0),
    ((255, 100, 0), 40.0),
    ((255, 0, 0), 50.0),
    ((200, 0, 0), 75.0),
    ((120, 0, 0), 100.0),
    ((40, 0, 0), 150.0),
]


def default_class_rates() -> list[float]:
    lows = [lo for _, lo in RAIN_CLASSES]
    highs = lows[1:] + [lows[-1] * 1.25]
    return [round(float(np.sqrt(lo * hi)), 3) for lo, hi in zip(lows, highs)]


def _key(r, g, b):
    return (np.asarray(r, np.uint32) << 16) | (np.asarray(g, np.uint32) << 8) | np.asarray(b, np.uint32)


def rgba_to_rain_rate(rgba: np.ndarray, class_rates: list[float] | None = None) -> tuple[np.ndarray, dict]:
    """(H, W, 4) uint8 -> (H, W) float32 mm/h plus stats.

    transparent -> 0.0 (no echo); known rain colour -> class rate; anything else opaque -> NaN.
    """
    rates = class_rates or default_class_rates()
    if len(rates) != len(RAIN_CLASSES):
        raise ValueError(f"class_rates must have {len(RAIN_CLASSES)} values")
    out = np.full(rgba.shape[:2], np.nan, dtype=np.float32)
    transparent = rgba[..., 3] == 0
    out[transparent] = 0.0
    key = _key(rgba[..., 0], rgba[..., 1], rgba[..., 2])
    for ((r, g, b), _), rate in zip(RAIN_CLASSES, rates):
        out[(~transparent) & (key == _key(r, g, b))] = rate
    unknown = int(np.isnan(out).sum())
    return out, {"unknown_pixels": unknown, "rain_pixels": int((out > 0).sum())}
