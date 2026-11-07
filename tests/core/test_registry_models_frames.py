from datetime import UTC, datetime, timedelta, timezone

import numpy as np
import pytest

from stormtracker.models import Georef, Observation, RadarFrame
from stormtracker.plugins import PluginRegistry
from stormtracker.storage.db import Database, Repository
from stormtracker.storage.frames import FrameStore
from stormtracker.timeutil import to_display


def test_naive_timestamp_rejected_and_normalised():
    with pytest.raises(ValueError):
        Observation(source="s", station_id="a", lat=0, lon=0, ts=datetime(2026, 1, 1), values={})
    o = Observation(source="s", station_id="a", lat=0, lon=0,
                    ts=datetime(2026, 1, 1, 10, tzinfo=timezone(timedelta(hours=10))), values={"temp_c": 1})
    assert o.ts == datetime(2026, 1, 1, 0, tzinfo=UTC) and o.ts.utcoffset() == timedelta(0)


def test_observation_key_validation():
    with pytest.raises(ValueError):
        Observation(source="s", station_id="a", lat=0, lon=0, ts=datetime(2026, 1, 1, tzinfo=UTC),
                    values={"Temp C": 1})


def test_display_in_sydney():
    d = to_display(datetime(2026, 1, 1, 0, tzinfo=UTC))
    assert d.utcoffset() == timedelta(hours=11)   # AEDT


def test_discovery_loads_good_skips_broken(tmp_path, monkeypatch):
    pkg = tmp_path / "plugpkg"
    (pkg / "good").mkdir(parents=True)
    (pkg / "bad").mkdir()
    (pkg / "__init__.py").write_text("")
    for d in ("good", "bad"):
        (pkg / d / "__init__.py").write_text("")
    (pkg / "bad" / "plugin.py").write_text("raise RuntimeError('broken')")
    (pkg / "good" / "plugin.py").write_text(
        "from stormtracker.plugins import Source, SourceMeta\n"
        "class G(Source):\n"
        "    meta = SourceMeta(name='good', category='warnings', poll_interval_s=10)\n"
        "    async def fetch(self): return 1\n"
        "    def normalise(self, raw): return []\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    r = PluginRegistry()
    r.discover("plugpkg")
    assert list(r.all()) == ["good"]


async def test_frame_roundtrip_and_retention(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path}/f.db")
    await db.create_all()
    repo = Repository(db, FrameStore(tmp_path / "radar"))
    g = Georef(lat0=-33.7, lon0=151.2, pixel_size_m=1000, width=4, height=3)
    arr = np.array([[0, 1.5, 10, np.nan]] * 3, dtype=np.float32)
    now = datetime.now(UTC)
    for ts in (now, now - timedelta(days=5)):
        await repo.save([RadarFrame(source="r", radar_id="IDR71", ts=ts, rain_rate_mmh=arr, georef=g)])
    files = list((tmp_path / "radar").rglob("*.npz"))
    assert len(files) == 2
    a, gg = FrameStore.read(sorted(files)[-1])
    assert gg == g and np.allclose(a[:, :3], arr[:, :3], rtol=1e-3) and np.isnan(a[0, 3])
    res = await repo.purge(frame_days=2, obs_days=90, event_days=90)
    assert res["frames"] == 1
    assert len(list((tmp_path / "radar").rglob("*.npz"))) == 1
    assert len(list((tmp_path / "radar").rglob("*.json"))) == 1
    await db.dispose()
