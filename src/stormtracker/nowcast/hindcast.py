"""Replay stored radar frames to measure how good the nowcast really is.

For every stored frame that has a few consecutive predecessors, forecast ahead and compare with the frame
actually observed at that lead time. Reported against *persistence* (assuming nothing moves), which is the
baseline any nowcast must beat. Used to calibrate the confidence figures and shown by ``stormtracker.cli hindcast``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from ..models import Georef
from ..storage.frames import FrameStore
from .engine import clean, estimate_motion, forecast, select_sequence

TOL_MIN = 2.5


@dataclass
class Score:
    hits: int = 0
    misses: int = 0
    false_alarms: int = 0
    n: int = 0
    sq_err: float = 0.0

    def add(self, pred: np.ndarray, obs: np.ndarray, thr: float) -> None:
        p, o = pred >= thr, obs >= thr
        self.hits += int((p & o).sum()); self.misses += int((~p & o).sum()); self.false_alarms += int((p & ~o).sum())

    @property
    def csi(self) -> float | None:        # critical success index: 1 = perfect, 0 = no skill
        d = self.hits + self.misses + self.false_alarms
        return None if d == 0 else self.hits / d

    @property
    def pod(self) -> float | None:
        d = self.hits + self.misses
        return None if d == 0 else self.hits / d

    @property
    def far(self) -> float | None:
        d = self.hits + self.false_alarms
        return None if d == 0 else self.false_alarms / d


@dataclass
class Report:
    frames: int = 0
    cases: dict[int, int] = field(default_factory=dict)
    nowcast: dict[tuple[int, float], Score] = field(default_factory=dict)
    persistence: dict[tuple[int, float], Score] = field(default_factory=dict)

    def rows(self) -> list[dict]:
        out = []
        for (lead, thr), s in sorted(self.nowcast.items()):
            b = self.persistence[(lead, thr)]
            out.append({"lead_min": lead, "threshold_mmh": thr, "cases": self.cases.get(lead, 0),
                        "csi_nowcast": s.csi, "csi_persistence": b.csi, "pod_nowcast": s.pod, "far_nowcast": s.far})
        return out


def load_frames(root: Path) -> list[tuple[datetime, Path, Georef]]:
    out = []
    for js in sorted(Path(root).rglob("*.json")):
        try:
            meta = json.loads(js.read_text())
            out.append((datetime.fromisoformat(meta["ts"]), js.with_suffix(".npz"), Georef(**meta["georef"])))
        except (OSError, ValueError, KeyError):
            continue
    return sorted(out, key=lambda f: f[0])


def evaluate(root: Path, leads=(10, 30, 60), thresholds=(1.0, 10.0), step_min: int = 5,
             min_rain_px: int = 200, every: int = 1) -> Report:
    """``min_rain_px``: skip start times with (almost) no rain, which would flatter both methods."""
    frames = load_frames(root)
    rep = Report(frames=len(frames))
    by_ts = {f[0]: f for f in frames}
    ts_list = [f[0] for f in frames]
    cache: dict[datetime, np.ndarray] = {}

    def arr(ts: datetime) -> np.ndarray:
        if ts not in cache:
            cache[ts] = clean(FrameStore.read(by_ts[ts][1])[0])
            if len(cache) > 40:
                cache.pop(next(iter(cache)))
        return cache[ts]

    max_lead = max(leads)
    for i, t in enumerate(ts_list):
        if i % every:
            continue
        g = by_ts[t][2]
        seq = select_sequence([(x, arr(x)) for x in ts_list[max(0, i - 5): i + 1]])
        if len(seq) < 3 or seq[-1][0] != t or int((arr(t) >= thresholds[0]).sum()) < min_rain_px:
            continue
        motion = estimate_motion(seq, g)
        fc = forecast(arr(t), motion, max_lead // step_min, step_min)
        for lead in leads:
            target = t + timedelta(minutes=lead)
            match = next((x for x in ts_list[i + 1:] if abs((x - target).total_seconds()) / 60 <= TOL_MIN), None)
            if match is None:
                continue
            obs = arr(match)
            pred = fc[lead // step_min - 1]
            rep.cases[lead] = rep.cases.get(lead, 0) + 1
            for thr in thresholds:
                rep.nowcast.setdefault((lead, thr), Score()).add(pred, obs, thr)
                rep.persistence.setdefault((lead, thr), Score()).add(arr(t), obs, thr)
    return rep


def format_report(rep: Report) -> str:
    f = lambda v: "  n/a" if v is None else f"{v:5.2f}"   # noqa: E731
    lines = [f"{rep.frames} frames; critical success index (1 = perfect, 0 = no skill)",
             "lead  rain>=  cases  nowcast  persistence  POD   FAR"]
    for r in rep.rows():
        lines.append(f"{r['lead_min']:>3}m {r['threshold_mmh']:>5.0f}  {r['cases']:>6}   {f(r['csi_nowcast'])}      {f(r['csi_persistence'])}      "
                     f"{f(r['pod_nowcast'])} {f(r['far_nowcast'])}")
    return "\n".join(lines)
