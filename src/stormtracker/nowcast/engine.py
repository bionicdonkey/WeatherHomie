"""Motion estimation and extrapolation (pySTEPS) plus the helpers around it."""
from __future__ import annotations

import math
import warnings
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

from ..models import Georef

MAX_GAP_MIN = 7.5          # radar frames come every ~5 min; a bigger hole breaks the motion estimate
MIN_RAIN_PX = 30           # fewer wet pixels than this and there is nothing to track
MAX_SPEED_KMH = 150.0      # faster than any real storm: treat as an estimation error
FLOOR_MMH = 0.2            # forecast values below this are interpolation fringe, not rain


@dataclass
class Motion:
    """Motion field in pixels/minute (index 0 = columns/east, 1 = rows/south) plus how far to trust it."""
    v: np.ndarray                       # (2, H, W) float32 px/min
    method: str                         # 'lucas-kanade' | 'cells' | 'none'
    frames_used: int
    speed_kmh: float | None = None      # median over rain pixels
    bearing_deg: float | None = None    # direction the rain is moving TOWARDS (0 = N, 90 = E)
    coherence: float = 0.0              # share of rain pixels moving about as the median does (0..1)
    reason: str | None = None


def _pixel_km(g: Georef) -> float:
    return g.pixel_size_m / 1000.0


def select_sequence(frames: list[tuple[datetime, np.ndarray]], max_frames: int = 4,
                    max_gap_min: float = MAX_GAP_MIN) -> list[tuple[datetime, np.ndarray]]:
    """The newest run of frames with no gap larger than ``max_gap_min`` (oldest first, at most ``max_frames``)."""
    out: list[tuple[datetime, np.ndarray]] = []
    for ts, arr in sorted(frames, key=lambda f: f[0], reverse=True):
        if out and (out[-1][0] - ts).total_seconds() / 60 > max_gap_min:
            break
        if out and ts == out[-1][0]:
            continue
        out.append((ts, arr))
        if len(out) == max_frames:
            break
    return list(reversed(out))


def clean(arr: np.ndarray) -> np.ndarray:
    a = np.nan_to_num(np.asarray(arr, np.float32), nan=0.0, posinf=0.0, neginf=0.0)
    a[a < 0] = 0
    return a


def estimate_motion(seq: list[tuple[datetime, np.ndarray]], g: Georef) -> Motion:
    """Dense Lucas-Kanade optical flow (pySTEPS) over the most recent frames."""
    zero = np.zeros((2, g.height, g.width), np.float32)
    if len(seq) < 2:
        return Motion(zero, "none", len(seq), reason="need at least two consecutive radar frames")
    stack = np.stack([clean(a) for _, a in seq])
    wet = stack[-1] >= 0.5
    if int(wet.sum()) < MIN_RAIN_PX:
        return Motion(zero, "none", len(seq), reason="almost no rain to track")
    dt_min = (seq[-1][0] - seq[0][0]).total_seconds() / 60 / (len(seq) - 1)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from pysteps.motion.lucaskanade import dense_lucaskanade
        from pysteps.utils.transformation import dB_transform
        db, _ = dB_transform(stack, threshold=0.1, zerovalue=-15.0)
        try:
            v = np.asarray(dense_lucaskanade(db, verbose=False), np.float32)
        except Exception as e:  # noqa: BLE001 - pySTEPS raises on degenerate input
            return Motion(zero, "none", len(seq), reason=f"motion estimate failed ({type(e).__name__})")
    v = v / dt_min                                                     # px/frame -> px/min
    max_px_min = MAX_SPEED_KMH / 60 / _pixel_km(g)
    speed = np.hypot(v[0], v[1])
    v = np.where(speed > max_px_min, v * (max_px_min / np.maximum(speed, 1e-6)), v)
    rain = stack[-1] >= 1.0
    sel = rain if int(rain.sum()) >= MIN_RAIN_PX else wet
    mvx, mvy = float(np.median(v[0][sel])), float(np.median(v[1][sel]))
    mspeed = math.hypot(mvx, mvy)
    tol = max(0.3 * mspeed, 0.05)
    coherence = float((np.hypot(v[0][sel] - mvx, v[1][sel] - mvy) <= tol).mean())
    return Motion(v, "lucas-kanade", len(seq), speed_kmh=mspeed * _pixel_km(g) * 60,
                  bearing_deg=(math.degrees(math.atan2(mvx, -mvy)) % 360) if mspeed > 1e-3 else None, coherence=coherence)


def forecast(arr: np.ndarray, motion: Motion, steps: int, step_min: int) -> np.ndarray:
    """Advect ``arr`` along the motion field: (steps, H, W) float32, frame k is lead (k+1)*step_min minutes."""
    a = clean(arr)
    if motion.method == "none" or not np.any(motion.v):
        return np.repeat(a[None], steps, axis=0)                     # persistence: nothing better is known
    from pysteps.extrapolation.semilagrangian import extrapolate
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        out = extrapolate(a, (motion.v * step_min).astype(np.float32), steps, outval=0.0, allow_nonfinite_values=True)
    out = np.asarray(out, np.float32)
    out[out < FLOOR_MMH] = 0.0
    return out


# ---- geometry helpers (image plane, km east/north of the radar) ---------------------------------------------------
def to_km(g: Georef, col: float, row: float) -> tuple[float, float]:
    k = _pixel_km(g)
    return (col - g.width / 2) * k, (g.height / 2 - row) * k


def disc_mask(g: Georef, col: float, row: float, radius_km: float) -> np.ndarray:
    r = radius_km / _pixel_km(g)
    yy, xx = np.ogrid[: g.height, : g.width]
    return (xx - col) ** 2 + (yy - row) ** 2 <= max(r, 0.75) ** 2          # at least ~1 pixel wide


@dataclass
class Quality:
    frames_used: int
    coherence: float
    method: str
    notes: list[str] = field(default_factory=list)
