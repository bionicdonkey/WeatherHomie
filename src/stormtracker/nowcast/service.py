"""Background service: turns each new stored radar frame into tracked cells, a forecast and a home impact."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import numpy as np
from sqlalchemy import select

from ..geo import latlon_to_pixel
from ..models import Georef
from ..radar_render import RENDER_VERSION, bounds_for, frame_stats, render_frame_png
from ..storage.frames import FrameStore
from ..storage.tables import EventRow, ObservationRow, RadarFrameRow
from ..timeutil import utcnow
from .cells import Tracker, detect_cells, track_summary
from .engine import Motion, estimate_motion, forecast, select_sequence, to_km
from .impact import home_impact, station_cross_check
from .settings import NowcastSettings

log = logging.getLogger("stormtracker.nowcast")
LOOKBACK_MIN = 100
POLL_S = 30


@dataclass
class Run:
    n: int
    radar_id: str
    georef: Georef
    frame_ts: Any
    fc: np.ndarray                      # (steps, H, W) float16
    step_min: int
    public: dict
    png: dict[int, bytes] = field(default_factory=dict)


class NowcastService:
    def __init__(self, core) -> None:
        self.core = core
        self.tracker = Tracker()
        self._tracker_key: tuple | None = None
        self.latest: Run | None = None
        self._last_frame_id: int | None = None
        self._n = 0
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self.status: dict[str, Any] = {"state": "starting", "last_run": None, "duration_ms": None, "error": None}

    # ---- settings -----------------------------------------------------------------------------------
    async def settings(self) -> NowcastSettings:
        return NowcastSettings(**(await self.core.store.get("nowcast", {})))

    async def save_settings(self, s: NowcastSettings, actor: str) -> None:
        await self.core.store.set("nowcast", s.model_dump(), actor=actor)
        await self.run(force=True)

    # ---- lifecycle ----------------------------------------------------------------------------------
    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="nowcast")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _loop(self) -> None:
        while True:
            await self.run()
            await asyncio.sleep(POLL_S)

    # ---- one pass -----------------------------------------------------------------------------------
    async def run(self, force: bool = False) -> None:
        async with self._lock:
            t0 = time.monotonic()
            try:
                p = await self.settings()
                if not p.enabled:
                    self.status.update(state="disabled", error=None)
                    self.latest = None
                    return
                rows = await self._recent_frames()
                if not rows:
                    self.status.update(state="waiting", error=None, detail="no radar frames stored yet")
                    return
                newest = rows[-1]
                if not force and newest.id == self._last_frame_id and self.latest is not None:
                    return
                locs = await self.core.locations.list()
                home = (locs[0].lat, locs[0].lon, locs[0].name) if locs else None
                station = await self._station_inputs(newest.ts)
                self._n += 1
                run = await asyncio.to_thread(self._compute, rows, p, home, station, self._n)
                self.latest, self._last_frame_id = run, newest.id
                dt = int((time.monotonic() - t0) * 1000)
                self.status.update(state="ok", last_run=utcnow().isoformat(), duration_ms=dt, error=None,
                                   frame_ts=run.frame_ts.isoformat(), detail=None)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - never let a processing bug stop the app
                log.exception("nowcast run failed")
                self.status.update(state="error", error=f"{type(e).__name__}: {e}"[:300])

    async def _recent_frames(self) -> list[RadarFrameRow]:
        async with self.core.db.session() as s:
            newest = (await s.execute(select(RadarFrameRow).order_by(RadarFrameRow.ts.desc()).limit(1))).scalar_one_or_none()
            if newest is None:
                return []
            q = (select(RadarFrameRow).where(RadarFrameRow.radar_id == newest.radar_id,
                                             RadarFrameRow.ts >= newest.ts - timedelta(minutes=LOOKBACK_MIN))
                 .order_by(RadarFrameRow.ts))
            return list((await s.execute(q)).scalars().all())

    async def _station_inputs(self, around) -> dict | None:
        """Latest reading from the freshest station, the pressure an hour earlier, and recent lightning."""
        async with self.core.db.session() as s:
            o = (await s.execute(select(ObservationRow).order_by(ObservationRow.ts.desc()).limit(1))).scalar_one_or_none()
            if o is None:
                return None
            earlier = (await s.execute(select(ObservationRow).where(
                ObservationRow.source == o.source, ObservationRow.station_id == o.station_id,
                ObservationRow.ts <= o.ts - timedelta(minutes=45), ObservationRow.ts >= o.ts - timedelta(minutes=75))
                .order_by(ObservationRow.ts.desc()).limit(1))).scalar_one_or_none()
            ev = (await s.execute(select(EventRow).where(
                EventRow.event_type == "lightning", EventRow.ts >= utcnow() - timedelta(minutes=30)))).scalars().all()
            return {"values": o.values, "ts": o.ts, "p_1h": (earlier.values or {}).get("pressure_hpa") if earlier else None,
                    "lightning": [dict(e.properties or {}) for e in ev]}

    # ---- the heavy part (runs in a worker thread) ----------------------------------------------------
    def _compute(self, rows: list[RadarFrameRow], p: NowcastSettings, home, station, n: int) -> Run:
        key = (p.cell_threshold_mmh, p.min_cell_area_km2)
        if key != self._tracker_key or (self.tracker.last_ts and rows[-1].ts < self.tracker.last_ts):
            self.tracker, self._tracker_key = Tracker(), key            # settings changed or time went backwards
        frames: list[tuple] = []
        g: Georef | None = None
        for r in rows:
            try:
                arr, g = FrameStore.read(r.path)
            except FileNotFoundError:
                continue                                                  # purged since the DB query
            frames.append((r.ts, arr))
            if self.tracker.last_ts is None or r.ts > self.tracker.last_ts:
                self.tracker.update(r.ts, detect_cells(arr, g, r.ts, p))
        if not frames or g is None:
            raise RuntimeError("radar frame files are missing")
        ts, now_arr = frames[-1]
        seq = select_sequence(frames)
        motion: Motion = estimate_motion(seq, g)
        steps = max(1, p.horizon_min // p.step_min)
        fc = forecast(now_arr, motion, steps, p.step_min)

        home_xy = None
        if home:
            try:
                home_xy = to_km(g, *latlon_to_pixel(g, home[0], home[1]))
            except ValueError:
                home_xy = None
        cells = [track_summary(t, home_xy, p.horizon_min) for t in self.tracker.current()]
        tracks_agree = _tracks_agree(cells, motion)
        impact = home_impact(now_arr, fc, g, home[0], home[1], ts, motion, p, tracks_agree) if home else {"status": "unknown", "reason": "no home location set"}
        if home and station:
            impact["station"] = station_cross_check(impact.get("now_mmh", 0) >= p.arrival_mmh, station["values"], station["ts"],
                                                    utcnow(), station["p_1h"], station["lightning"])
        leads = [{"lead_min": (k + 1) * p.step_min, "valid_ts": (ts + timedelta(minutes=(k + 1) * p.step_min)).isoformat(),
                  "png": f"/api/nowcast/frames/{n}/{(k + 1) * p.step_min}.png?v={RENDER_VERSION}",
                  # same shape as the observed frames' stats, so the timeline can draw forecast columns the same way
                  "home": frame_stats(fc[k], g, home[0], home[1]) if home else None} for k in range(steps)]
        south, west, north, east = bounds_for(g)
        public = {
            "available": True, "run": n, "radar_id": rows[-1].radar_id, "frame_ts": ts.isoformat(),
            "computed_at": utcnow().isoformat(), "step_min": p.step_min, "horizon_min": p.horizon_min,
            "bounds": [[round(south, 5), round(west, 5)], [round(north, 5), round(east, 5)]],
            "leads": leads, "cells": cells, "impact": impact,
            "motion": {"method": motion.method, "frames_used": motion.frames_used, "speed_kmh": _r(motion.speed_kmh),
                       "bearing_deg": None if motion.bearing_deg is None else round(motion.bearing_deg),
                       "coherence": round(motion.coherence, 2), "reason": motion.reason},
            "home": None if not home else {"name": home[2], "lat": home[0], "lon": home[1]},
        }
        return Run(n, rows[-1].radar_id, g, ts, fc.astype(np.float16), p.step_min, public)

    # ---- served to the API ----------------------------------------------------------------------------
    def public(self) -> dict:
        if self.latest is None:
            return {"available": False, "state": self.status["state"], "reason": self.status.get("detail") or self.status.get("error")}
        return {**self.latest.public, "status": self.status}

    async def png(self, n: int, lead: int) -> bytes | None:
        run = self.latest
        if run is None or run.n != n or lead % run.step_min or not (0 < lead <= run.step_min * len(run.fc)):
            return None
        if lead not in run.png:
            arr = run.fc[lead // run.step_min - 1].astype(np.float32)
            run.png[lead], _ = await asyncio.to_thread(render_frame_png, arr, run.georef)
        return run.png[lead]


def _r(v: float | None) -> float | None:
    return None if v is None else round(v, 1)


def _tracks_agree(cells: list[dict], motion: Motion) -> bool | None:
    """Do the tracked cells move roughly the way the optical flow says? None if we can't tell."""
    if motion.method == "none" or motion.speed_kmh is None or motion.bearing_deg is None:
        return None
    moving = [c for c in cells if c.get("speed_kmh") and c.get("bearing_deg") is not None and c["max_mmh"] >= 10]
    if not moving:
        return None
    ok = 0
    for c in moving:
        d = abs((c["bearing_deg"] - motion.bearing_deg + 180) % 360 - 180)
        if d <= 45 and abs(c["speed_kmh"] - motion.speed_kmh) <= max(10.0, 0.5 * motion.speed_kmh):
            ok += 1
    return ok / len(moving) >= 0.5
