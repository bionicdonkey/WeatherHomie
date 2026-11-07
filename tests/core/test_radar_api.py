import io
from datetime import timedelta

import numpy as np
import pytest
from httpx import ASGITransport, AsyncClient
from PIL import Image

from stormtracker.geo import (grid_bounds, latlon_to_pixel, latlon_to_pixel_np, merc_lat, merc_y, mercator_grid,
                              pixel_to_latlon, sample_nearest)
from stormtracker.models import RadarFrame
from stormtracker.radar_render import RENDER_VERSION, colourise, frame_stats, legend, render_frame_png
from stormtracker.sources.bom_radar.georef import georef_for
from stormtracker.timeutil import utcnow

G = georef_for("IDR713")
HOME = {"id": "home", "name": "Home", "lat": -33.87, "lon": 151.21}


def test_vectorised_projection_matches_scalar_and_roundtrips():
    lat = np.array([-33.7008, -33.2, -34.1, -33.93])
    lon = np.array([151.2095, 150.8, 151.6, 150.9])
    c, r = latlon_to_pixel_np(G, lat, lon)
    for i in range(4):
        assert (c[i], r[i]) == pytest.approx(latlon_to_pixel(G, lat[i], lon[i]))
        assert pixel_to_latlon(G, c[i], r[i]) == pytest.approx((lat[i], lon[i]), abs=1e-6)
    assert (c[0], r[0]) == pytest.approx((256, 256))
    far = latlon_to_pixel_np(G, 33.0, -29.0)                  # antipodal side
    assert np.isnan(far[0]) and np.isnan(far[1])


def test_mercator_roundtrip():
    assert merc_lat(merc_y(np.array([-33.5, 10.0]))) == pytest.approx([-33.5, 10.0])


def test_mercator_grid_puts_known_points_on_the_right_output_pixel():
    """Plant a marker at a known lat/lon in the source grid; it must appear where lat/lon says in the output."""
    bounds, col, row = mercator_grid(G, 640)
    south, west, north, east = bounds
    h, w = col.shape
    for lat, lon in [(-33.7008, 151.2095), (-33.4, 150.9), (-34.0, 151.5)]:
        sc, sr = latlon_to_pixel(G, lat, lon)
        x = (lon - west) / (east - west) * w
        y = (float(merc_y(north)) - float(merc_y(lat))) / (float(merc_y(north)) - float(merc_y(south))) * h
        got_c, got_r = col[int(y), int(x)], row[int(y), int(x)]
        assert (got_c, got_r) == pytest.approx((sc, sr), abs=1.5)    # within ~1 source pixel (500 m)


def test_radar_image_extent_and_scale():
    s, w, n, e = grid_bounds(G)
    assert s < -33.7008 < n and w < 151.2095 < e
    # 128 km radius => ~256 km box: ~2.3 deg tall, ~2.8 deg wide at this latitude
    assert 2.2 < n - s < 2.45 and 2.6 < e - w < 2.9


def test_colourise_thresholds_and_transparency():
    a = np.array([[np.nan, 0.0, 0.1, 0.2, 0.6, 200.0]], np.float32)
    rgba = colourise(a)
    assert (rgba[0, :3, 3] == 0).all()                       # nan, 0, below lightest class
    assert rgba[0, 3, 3] > 0 and tuple(rgba[0, 3, :3]) == (205, 212, 255)   # lightest class drawn visibly, not near-white
    assert tuple(rgba[0, 5, :3]) == (40, 0, 0)               # heaviest class
    assert len(legend()) == 15 and legend()[0]["colour"] == "#cdd4ff" and legend()[1]["colour"] == "#b4b4ff"


def test_frame_stats_at_point_and_radius():
    arr = np.zeros((512, 512), np.float32)
    c, r = latlon_to_pixel(G, HOME["lat"], HOME["lon"])
    arr[int(r), int(c)] = 12.0                                # directly over home
    arr[int(r) - 20, int(c) + 20] = 40.0                      # ~14 km away
    arr[int(r) + 200, int(c)] = 99.0                          # ~100 km away
    st = frame_stats(arr, G, HOME["lat"], HOME["lon"], 25)
    assert st["at_point_mm_h"] == 12.0 and st["max_within_mm_h"] == 40.0
    out = frame_stats(arr, G, 10.0, 100.0, 25)                # nowhere near this radar
    assert out["at_point_mm_h"] is None


def test_render_frame_png_is_transparent_rgba_with_rain_in_the_right_place():
    arr = np.zeros((512, 512), np.float32)
    lat, lon = -33.5, 151.0
    c, r = latlon_to_pixel(G, lat, lon)
    arr[int(r) - 3:int(r) + 4, int(c) - 3:int(c) + 4] = 8.0
    png, (s, w, n, e) = render_frame_png(arr, G)
    img = np.asarray(Image.open(io.BytesIO(png)))
    assert img.shape[2] == 4 and img[..., 3].sum() > 0
    ys, xs = np.nonzero(img[..., 3])
    cx, cy = xs.mean() / img.shape[1], ys.mean() / img.shape[0]
    exp_x = (lon - w) / (e - w)
    exp_y = (float(merc_y(n)) - float(merc_y(lat))) / (float(merc_y(n)) - float(merc_y(s)))
    assert cx == pytest.approx(exp_x, abs=0.004) and cy == pytest.approx(exp_y, abs=0.004)


# ---- HTTP ---------------------------------------------------------------------------------------------
async def seed(app, n=3, with_overlay=True):
    core = app.state.core
    now = utcnow().replace(microsecond=0, second=0)
    c, r = latlon_to_pixel(G, HOME["lat"], HOME["lon"])
    for i in range(n):
        arr = np.zeros((512, 512), np.float32)
        arr[int(r) - 2:int(r) + 3, int(c) - 2:int(c) + 3] = float(i)
        await core.repo.save([RadarFrame(source="bom_radar", radar_id="IDR713",
                                         ts=now - timedelta(minutes=5 * (n - 1 - i)), rain_rate_mmh=arr, georef=G)])
    if with_overlay:
        d = core.boot.data_dir / "radar_overlays" / "IDR713"
        d.mkdir(parents=True, exist_ok=True)
        ov = np.zeros((512, 512, 4), np.uint8)
        ov[250:262, :, :] = (0, 0, 0, 255)
        Image.fromarray(ov, "RGBA").save(d / "background.png")


async def test_frames_list_and_png_need_only_the_session_cookie(admin, app):
    await admin.put("/api/locations", json=[HOME])
    await seed(app)
    fr = (await admin.get("/api/radar/frames?radar_id=IDR713")).json()
    assert len(fr) == 3 and [f["ts"] for f in fr] == sorted(f["ts"] for f in fr)
    assert [f["home"]["at_point_mm_h"] for f in fr] == [0.0, 1.0, 2.0]
    assert [f["home"]["max_within_mm_h"] for f in fr] == [0.0, 1.0, 2.0]
    assert all(f["png"] == f"/api/radar/frames/{f['id']}.png?v={RENDER_VERSION}" for f in fr)
    (s, w), (n, e) = fr[0]["bounds"]
    assert s < -33.7 < n and w < 151.2 < e
    # an <img src> carries a cookie but cannot add the CSRF header: GET must not demand it
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           cookies=dict(admin.cookies)) as browser:
        r = await browser.get(fr[-1]["png"])             # the versioned URL the UI actually uses
        assert r.status_code == 200 and r.headers["content-type"] == "image/png"
        assert "immutable" in r.headers["cache-control"]
        assert Image.open(io.BytesIO(r.content)).mode == "RGBA"


async def test_frame_png_is_cached_next_to_the_data_and_removed_with_it(admin, app):
    await seed(app, 1)
    fid = (await admin.get("/api/radar/frames")).json()[0]["id"]
    assert (await admin.get(f"/api/radar/frames/{fid}.png")).status_code == 200
    npz = next((app.state.core.boot.data_dir / "radar").rglob("*.npz"))
    cached = npz.with_suffix(f".merc{RENDER_VERSION}.png")
    stale_v1 = npz.with_suffix(".merc.png"); stale_v1.write_bytes(b"old")   # an older render must be cleaned up too
    assert cached.exists()
    app.state.core.repo.frames.delete(npz)
    assert not cached.exists() and not npz.exists() and not stale_v1.exists()


async def test_frames_unauthenticated_and_unknown(client, admin, app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anon:
        assert (await anon.get("/api/radar/frames")).status_code == 401
        assert (await anon.get("/api/radar/frames/1.png")).status_code == 401
        assert (await anon.get("/api/radar/overlays/IDR713/background.png")).status_code == 401
    assert (await admin.get("/api/radar/frames/999.png")).status_code == 404


async def test_overlay_listed_reprojected_and_traversal_refused(admin, app):
    await seed(app)
    assert (await admin.get("/api/radar/overlays")).json() == [{"radar_id": "IDR713", "layers": ["background"]}]
    r = await admin.get("/api/radar/overlays/IDR713/background.png")
    assert r.status_code == 200
    assert np.asarray(Image.open(io.BytesIO(r.content)))[..., 3].max() == 255
    for bad in ("/api/radar/overlays/IDR713/..%2F..%2Fstormtracker.db.png", "/api/radar/overlays/..%2F/background.png",
                "/api/radar/overlays/IDR713/nope.png", "/api/radar/overlays/IDR999/background.png"):
        assert (await admin.get(bad)).status_code == 404, bad


async def test_overlay_without_any_frame_is_a_clear_404(admin, app):
    d = app.state.core.boot.data_dir / "radar_overlays" / "IDR713"
    d.mkdir(parents=True)
    Image.new("RGBA", (512, 512)).save(d / "range.png")
    r = await admin.get("/api/radar/overlays/IDR713/range.png")
    assert r.status_code == 404 and "georeference" in r.json()["detail"]


async def test_legend(admin):
    j = (await admin.get("/api/radar/legend")).json()
    assert j["unit"] == "mm/h" and j["approximate"] is True and len(j["classes"]) == 15
