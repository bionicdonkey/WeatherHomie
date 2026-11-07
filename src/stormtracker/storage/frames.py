"""Radar frames on disk: float16 .npz plus the georeference in a JSON sidecar."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import numpy as np

from ..models import Georef, RadarFrame


class FrameStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def _path(self, f: RadarFrame) -> Path:
        return self.root / f.source / f.radar_id / f"{f.ts:%Y%m%dT%H%M%SZ}.npz"

    def _write(self, f: RadarFrame) -> Path:
        p = self._path(f)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp.npz")
        np.savez_compressed(tmp, rain_rate_mmh=f.rain_rate_mmh.astype(np.float16))
        tmp.replace(p)  # atomic
        p.with_suffix(".json").write_text(json.dumps(
            {"source": f.source, "radar_id": f.radar_id, "ts": f.ts.isoformat(), "georef": f.georef.model_dump()}))
        return p

    async def write(self, f: RadarFrame) -> Path:
        return await asyncio.to_thread(self._write, f)

    @staticmethod
    def read(path: str | Path) -> tuple[np.ndarray, Georef]:
        p = Path(path)
        with np.load(p) as z:
            arr = z["rain_rate_mmh"].astype(np.float32)
        meta = json.loads(p.with_suffix(".json").read_text())
        return arr, Georef(**meta["georef"])

    def delete(self, path: str | Path) -> None:
        p = Path(path)
        p.unlink(missing_ok=True)
        p.with_suffix(".json").unlink(missing_ok=True)
        for cached in p.parent.glob(f"{p.stem}.merc*.png"):   # cached map renderings (any render version)
            cached.unlink(missing_ok=True)
