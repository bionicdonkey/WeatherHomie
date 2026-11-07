from datetime import UTC, datetime

import httpx
import numpy as np
import pytest
from bom_helpers import REAL, ftp, make_png, make_source  # noqa: F401  (fixtures)

from stormtracker.sources.bom_radar.plugin import decode_frame, parse_frame_name

PNG = make_png()


def names(*hhmm):
    return [f"IDR713.T.20261103{t}.png" for t in hhmm]


def https_ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=PNG)


def test_parse_frame_name():
    assert parse_frame_name("IDR713.T.202611030759.png") == ("IDR713", datetime(2026, 11, 3, 7, 59, tzinfo=UTC))
    for bad in ("IDR713.background.png", "IDR713.T.2026110307.png", "IDR713.T.202611030759.gif", "x"):
        assert parse_frame_name(bad) is None


async def test_fetch_normalise_and_dedupe(make_source, ftp):
    ftp.names = names("0749", "0754", "0759") + ["IDR714.T.202611030759.png", "IDR713.range.png"]
    s = make_source(https_ok, max_frames=2)
    raw = await s.fetch()
    assert sorted(raw["frames"]) == names("0754", "0759")           # newest 2 only, other radar ignored
    frames = s.normalise(raw)
    await s.on_saved(frames)                                         # scheduler does this after saving
    assert [f.ts for f in frames] == [datetime(2026, 11, 3, 7, 54, tzinfo=UTC), datetime(2026, 11, 3, 7, 59, tzinfo=UTC)]
    assert frames[0].radar_id == "IDR713" and frames[0].georef.pixel_size_m == 500
    assert frames[0].rain_rate_mmh.shape == (512, 512) and frames[0].rain_rate_mmh.dtype == np.float32
    # second poll with one new frame only downloads that one
    ftp.names += names("0804")
    raw2 = await s.fetch()
    assert sorted(raw2["frames"]) == names("0804")
    await s.on_saved(s.normalise(raw2))
    assert (await s.fetch())["frames"] == {}


async def test_frames_retried_if_normalise_or_save_fails(make_source, ftp):
    ftp.names = names("0759")
    s = make_source(https_ok)
    raw = await s.fetch()
    assert list(raw["frames"]) == names("0759")
    # processing failed (never reached on_saved) -> the next poll must fetch it again
    assert list((await s.fetch())["frames"]) == names("0759")
    await s.on_saved(s.normalise(raw))
    assert (await s.fetch())["frames"] == {}


def test_radar_id_is_required():
    from stormtracker.sources.bom_radar.plugin import BomRadarConfig
    with pytest.raises(ValueError):
        BomRadarConfig()


async def test_overlays_saved_once_and_failure_tolerated(make_source, ftp, tmp_path):
    ftp.names = names("0759")
    calls = []

    def handler(req):
        calls.append(req.url.path)
        if "topography" in req.url.path:
            return httpx.Response(500)
        return httpx.Response(200, content=PNG)

    s = make_source(handler, use_ftp_fallback=False)
    await s.fetch()
    d = tmp_path / "radar_overlays" / "IDR713"
    assert (d / "background.png").exists() and (d / "range.png").exists() and (d / "locations.png").exists()
    assert not (d / "topography.png").exists()                       # failed overlay didn't fail the poll
    n = len(calls)
    ftp.names += names("0804")
    await s.fetch()
    assert sum("/radar_transparencies/" in c for c in calls[n:]) == 1  # only the failed layer retried


async def test_https_failure_falls_back_to_ftp(make_source, ftp):
    ftp.names = names("0759")
    ftp.files = {names("0759")[0]: PNG, "IDR713.background.png": PNG, "IDR713.topography.png": PNG,
                 "IDR713.locations.png": PNG, "IDR713.range.png": PNG}
    s = make_source(lambda r: httpx.Response(404))
    raw = await s.fetch()
    assert list(raw["frames"]) == names("0759") and names("0759")[0] in ftp.retrieved


async def test_no_fallback_raises(make_source, ftp):
    ftp.names = names("0759")
    s = make_source(lambda r: httpx.Response(503), use_ftp_fallback=False)
    with pytest.raises(httpx.HTTPStatusError):
        await s.fetch()


async def test_html_error_page_with_200_is_rejected(make_source, ftp):
    ftp.names = names("0759")
    s = make_source(lambda r: httpx.Response(200, content=b"<html>blocked</html>"), use_ftp_fallback=False)
    with pytest.raises(ValueError):
        await s.fetch()


def test_normalise_rejects_wrong_size_and_changed_palette(make_source):
    s = make_source(https_ok)
    with pytest.raises(ValueError, match="size"):
        s.normalise({"frames": {names("0759")[0]: make_png(size=256)}})
    from PIL import Image
    import io
    buf = io.BytesIO()
    Image.new("RGBA", (512, 512), (9, 99, 199, 255)).save(buf, "PNG")   # everything unknown
    with pytest.raises(ValueError, match="palette"):
        s.normalise({"frames": {names("0759")[0]: buf.getvalue()}})


def test_config_validation():
    from stormtracker.sources.bom_radar.plugin import BomRadarConfig
    with pytest.raises(ValueError):
        BomRadarConfig(radar_id="IDR9")
    with pytest.raises(ValueError):
        BomRadarConfig(radar_id="IDR713", overlay_layers=["../etc"])


async def test_frames_persist_via_repository(make_source, ftp, tmp_path):
    from stormtracker.storage.db import Database, Repository
    from stormtracker.storage.frames import FrameStore
    ftp.names = names("0759")
    s = make_source(https_ok)
    frames = s.normalise(await s.fetch())
    db = Database(f"sqlite+aiosqlite:///{tmp_path}/r.db")
    await db.create_all()
    repo = Repository(db, FrameStore(tmp_path / "radar"))
    assert await repo.save(frames) == 1 and await repo.save(frames) == 0   # idempotent
    f = next((tmp_path / "radar").rglob("*.npz"))
    arr, g = FrameStore.read(f)
    assert g == s.georef and np.isnan(arr[0]).all()
    await db.dispose()


real = sorted(REAL.glob("IDR713.T.*.png"))


@pytest.mark.skipif(not real, reason="no real BOM frames in tests/fixtures/bom_radar/real")
def test_real_frame_decodes_sensibly():
    arr, st = decode_frame(real[-1].read_bytes())
    assert arr.shape == (512, 512)
    assert np.isnan(arr[:16]).all()                       # header bar masked
    assert st["unknown_pixels"] < 0.05 * arr.size         # only header + footer text/overlay annotations
    assert st["rain_pixels"] > 1000 and np.nanmax(arr) > 1
    from stormtracker.sources.bom_radar.palette import default_class_rates
    assert set(np.unique(arr[~np.isnan(arr)])) <= {np.float32(0), *map(np.float32, default_class_rates())}
