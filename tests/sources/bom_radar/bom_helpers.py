from __future__ import annotations

import io
import logging
from pathlib import Path

import httpx
import numpy as np
import pytest
from PIL import Image

from stormtracker.plugins import Health, SourceContext
from stormtracker.sources.bom_radar.palette import RAIN_CLASSES

GREY, BLACK = (192, 192, 192), (0, 0, 0)
REAL = Path(__file__).parents[2] / "fixtures" / "bom_radar" / "real"


def make_png(size: int = 512, classes: bool = True) -> bytes:
    """Synthetic frame using BOM's real palette layout: idx0 transparent, 1 grey bar, 2 black text,
    then one block per rain class."""
    pal = [(0, 0, 0), GREY, BLACK] + [c for c, _ in RAIN_CLASSES]
    im = Image.new("P", (size, size), 0)
    flat = [v for rgb in pal for v in rgb]
    im.putpalette(flat + [0] * (768 - len(flat)))
    px = np.zeros((size, size), dtype=np.uint8)
    px[:16, :] = 1                 # header bar
    px[2:10, 5:40] = 2             # header text
    if classes:
        for i in range(len(RAIN_CLASSES)):   # 15 blocks of 10x10 down row 100
            px[100:110, 20 + 12 * i: 30 + 12 * i] = 3 + i
    im = Image.fromarray(px, "P")
    im.putpalette(flat + [0] * (768 - len(flat)))
    buf = io.BytesIO()
    im.save(buf, "PNG", transparency=0)
    return buf.getvalue()


class FakeFtp:
    names: list[str] = []
    files: dict[str, bytes] = {}
    retrieved: list[str] = []

    @classmethod
    def list_names(cls, directory, prefix):
        return [n for n in cls.names if n.startswith(prefix)]

    @classmethod
    def retrieve(cls, directory, name):
        cls.retrieved.append(name)
        return cls.files[name]


@pytest.fixture
def ftp():
    FakeFtp.names, FakeFtp.files, FakeFtp.retrieved = [], {}, []
    return FakeFtp


@pytest.fixture
def make_source(tmp_path):
    from stormtracker.sources.bom_radar.plugin import BomRadar, BomRadarConfig

    def _make(handler, **cfg):
        async def locs():
            return []

        async def emit(items):
            pass

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        ctx = SourceContext(http=client, emit=emit, health=Health(), locations=locs,
                            logger=logging.getLogger("t"), data_dir=tmp_path)
        s = BomRadar(BomRadarConfig(**{"radar_id": "IDR713", **cfg}), ctx)
        s.ftp = FakeFtp
        return s
    return _make
