"""Storm cell detection and frame-to-frame tracking."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
from scipy import ndimage
from scipy.optimize import linear_sum_assignment

from ..geo import pixel_to_latlon
from ..models import Georef
from .engine import clean, to_km
from .settings import NowcastSettings

MAX_CELLS = 40
MATCH_RADIUS_KM = 12.0          # a cell moving 140 km/h covers 12 km in 5 minutes
MAX_AGE_MIN = 15.0              # a track not seen for this long is dropped
HISTORY = 6                     # observations kept per track
MIN_SPEED_KMH = 8.0             # slower than this is "stationary" for arrival purposes


@dataclass
class CellObs:
    ts: datetime
    col: float
    row: float
    x_km: float
    y_km: float
    lat: float
    lon: float
    area_km2: float
    max_mmh: float
    mean_mmh: float

    @property
    def radius_km(self) -> float:
        return math.sqrt(self.area_km2 / math.pi)


def detect_cells(arr: np.ndarray, g: Georef, ts: datetime, p: NowcastSettings) -> list[CellObs]:
    """Connected areas at or above the cell threshold (8-connected), biggest/most intense first."""
    a = clean(arr)
    lab, n = ndimage.label(a >= p.cell_threshold_mmh, structure=np.ones((3, 3)))
    if n == 0:
        return []
    px_km2 = (g.pixel_size_m / 1000.0) ** 2
    idx = np.arange(1, n + 1)
    areas = ndimage.sum(np.ones_like(a), lab, idx) * px_km2
    keep = [i for i, ar in zip(idx, areas) if ar >= p.min_cell_area_km2]
    if not keep:
        return []
    com = ndimage.center_of_mass(a, lab, keep)            # intensity-weighted: sits on the core, not the fringe
    mx = ndimage.maximum(a, lab, keep)
    mean = ndimage.mean(a, lab, keep)
    area = {i: ar for i, ar in zip(idx, areas)}
    out = []
    for i, (row, col), m, mn in zip(keep, com, mx, mean):
        x, y = to_km(g, col, row)
        lat, lon = pixel_to_latlon(g, col, row)
        out.append(CellObs(ts, float(col), float(row), x, y, lat, lon, float(area[i]), float(m), float(mn)))
    out.sort(key=lambda c: c.max_mmh * c.area_km2, reverse=True)
    return out[:MAX_CELLS]


@dataclass
class Track:
    id: str
    first_ts: datetime
    obs: list[CellObs] = field(default_factory=list)

    @property
    def last(self) -> CellObs:
        return self.obs[-1]

    def velocity_km_min(self) -> tuple[float, float] | None:
        """Least-squares slope of the centroid over the last few observations (needs >= 2, spanning >= 4 min)."""
        pts = self.obs[-HISTORY:]
        if len(pts) < 2:
            return None
        t = np.array([(o.ts - pts[0].ts).total_seconds() / 60 for o in pts])
        if t[-1] - t[0] < 4.0:
            return None
        return float(np.polyfit(t, [o.x_km for o in pts], 1)[0]), float(np.polyfit(t, [o.y_km for o in pts], 1)[0])


class Tracker:
    """Incremental tracker: feed frames oldest-first with ``update``."""

    def __init__(self) -> None:
        self.tracks: dict[str, Track] = {}
        self.last_ts: datetime | None = None
        self._seq = 0

    def _new_id(self, ts: datetime) -> str:
        self._seq += 1
        return f"{ts:%H%M}-{self._seq}"

    def update(self, ts: datetime, cells: list[CellObs]) -> None:
        if self.last_ts is not None and ts <= self.last_ts:
            return
        for tid in [t for t, tr in self.tracks.items() if (ts - tr.last.ts).total_seconds() / 60 > MAX_AGE_MIN]:
            del self.tracks[tid]
        active = list(self.tracks.values())
        assigned: set[int] = set()
        if active and cells:
            cost = np.full((len(active), len(cells)), 1e6)
            for i, tr in enumerate(active):
                dt = (ts - tr.last.ts).total_seconds() / 60
                v = tr.velocity_km_min() or (0.0, 0.0)
                px, py = tr.last.x_km + v[0] * dt, tr.last.y_km + v[1] * dt
                for j, c in enumerate(cells):
                    d = math.hypot(c.x_km - px, c.y_km - py)
                    if d <= max(MATCH_RADIUS_KM, tr.last.radius_km + c.radius_km):
                        cost[i, j] = d
            for i, j in zip(*linear_sum_assignment(cost)):
                if cost[i, j] < 1e6:
                    active[i].obs.append(cells[j])
                    active[i].obs[:] = active[i].obs[-HISTORY:]
                    assigned.add(j)
        for j, c in enumerate(cells):
            if j not in assigned:
                tid = self._new_id(ts)
                self.tracks[tid] = Track(tid, ts, [c])
        self.last_ts = ts

    def current(self) -> list[Track]:
        """Tracks seen in the most recent frame."""
        return [t for t in self.tracks.values() if t.last.ts == self.last_ts]


def home_approach(tr: Track, home_xy: tuple[float, float], horizon_min: float) -> dict:
    """Kinematic arrival estimate: where does this cell's path (constant velocity) pass relative to home?"""
    c = tr.last
    v = tr.velocity_km_min()
    hx, hy = home_xy
    dist_now = math.hypot(c.x_km - hx, c.y_km - hy)
    out: dict = {"distance_km": round(dist_now, 1), "approaching": False, "eta_min": None, "closest_km": None}
    reach = c.radius_km + 1.0
    if dist_now <= reach:
        return {**out, "approaching": True, "eta_min": 0.0, "closest_km": round(dist_now, 1)}
    if v is None:
        return out
    speed = math.hypot(*v)
    if speed * 60 < MIN_SPEED_KMH:
        return out
    rx, ry = hx - c.x_km, hy - c.y_km
    t_cpa = (rx * v[0] + ry * v[1]) / (speed ** 2)                 # minutes until closest approach
    if t_cpa <= 0:
        return {**out, "closest_km": round(dist_now, 1)}            # already moving away
    cpa = abs(rx * v[1] - ry * v[0]) / speed                        # closest distance along the path
    out["closest_km"] = round(cpa, 1)
    if cpa <= reach:
        t_edge = max(0.0, t_cpa - math.sqrt(max(0.0, reach ** 2 - cpa ** 2)) / speed)
        if t_edge <= horizon_min:
            out.update(approaching=True, eta_min=round(t_edge, 1))
    return out


def track_summary(tr: Track, home_xy: tuple[float, float] | None, horizon_min: float) -> dict:
    c = tr.last
    v = tr.velocity_km_min()
    speed = math.hypot(*v) * 60 if v else None
    bearing = (math.degrees(math.atan2(v[0], v[1])) % 360) if v and speed and speed > 1 else None
    rising = None
    if len(tr.obs) >= 3:
        rising = c.max_mmh - tr.obs[-3].max_mmh
    d = {"id": tr.id, "lat": round(c.lat, 4), "lon": round(c.lon, 4), "area_km2": round(c.area_km2, 1),
         "radius_km": round(c.radius_km, 1), "max_mmh": round(c.max_mmh, 1), "mean_mmh": round(c.mean_mmh, 1),
         "speed_kmh": None if speed is None else round(speed, 1), "bearing_deg": None if bearing is None else round(bearing),
         "age_min": round((c.ts - tr.first_ts).total_seconds() / 60),
         "intensity_change_mmh": None if rising is None else round(rising, 1),
         "track": [[round(o.lat, 4), round(o.lon, 4)] for o in tr.obs]}
    if home_xy is not None:
        d["home"] = home_approach(tr, home_xy, horizon_min)
    return d
