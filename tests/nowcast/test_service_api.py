import io
from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from PIL import Image
from synth import G, T0, moving

from stormtracker.geo import pixel_to_latlon
from stormtracker.models import Observation, RadarFrame

HOME_PX = (275, 256)
LAT, LON = pixel_to_latlon(G, *HOME_PX)
HOME = {"id": "home", "name": "Home", "lat": LAT, "lon": LON}


async def seed(app, seq, radar="IDR713"):
    for ts, arr in seq:
        await app.state.core.repo.save([RadarFrame(source="bom_radar", radar_id=radar, ts=ts, rain_rate_mmh=arr, georef=G)])


async def test_nothing_stored_is_a_clear_not_an_error(admin):
    j = (await admin.get("/api/nowcast")).json()
    assert j["available"] is False and j["state"] in ("starting", "waiting")


async def test_storm_heading_for_home_end_to_end(admin, app):
    await admin.put("/api/locations", json=[HOME])
    await seed(app, moving(5, start=(170, 256), v_px=(5, 0)))                 # newest frame: core at col 190, 30 km/h east
    j = (await admin.post("/api/nowcast/recompute")).json()
    assert j["available"] and j["radar_id"] == "IDR713" and j["frame_ts"] == (T0 + timedelta(minutes=20)).isoformat()
    imp = j["impact"]
    assert imp["status"] == "approaching" and imp["peak_mmh"] > 30
    assert imp["eta_lead_min"] == pytest.approx((275 - 27 - 190), abs=6)       # 5 px per 5 min => 1 px/min
    assert imp["eta_ts"] == (T0 + timedelta(minutes=20 + imp["eta_lead_min"])).isoformat()
    m = j["motion"]
    assert m["method"] == "lucas-kanade" and m["speed_kmh"] == pytest.approx(30, rel=0.06) and m["bearing_deg"] == pytest.approx(90, abs=3)
    assert len(j["leads"]) == 18 and j["leads"][0]["lead_min"] == 5 and j["leads"][-1]["lead_min"] == 90
    # every forecast frame carries the same at-home / within-25-km stats as observed frames, for the timeline
    at_home = [l["home"]["at_point_mm_h"] for l in j["leads"]]
    assert at_home[0] == 0 and max(at_home) > 30 and all(l["home"]["max_within_mm_h"] is not None for l in j["leads"])
    assert at_home.index(max(at_home)) + 1 == pytest.approx(imp["peak_lead_min"] / 5, abs=2)
    (cell,) = [c for c in j["cells"] if c["max_mmh"] > 30]            # (every tracked cell is returned, near or far)
    assert cell["speed_kmh"] == pytest.approx(30, rel=0.06) and cell["home"]["approaching"] is True
    # Different questions: the field forecast says when rain >= 1 mm/h first reaches home; the cell track says when the
    # heavy core (>= 10 mm/h) does. The core arrives after the first drops, but not wildly later.
    assert imp["eta_lead_min"] <= cell["home"]["eta_min"] <= imp["eta_lead_min"] + 15
    assert len(cell["track"]) == 5 and j["home"]["name"] == "Home"
    assert j["status"]["state"] == "ok" and j["status"]["duration_ms"] > 0


async def test_forecast_frame_pngs_are_served_and_stale_runs_404(admin, app):
    await admin.put("/api/locations", json=[HOME])
    await seed(app, moving(4, start=(170, 256), v_px=(5, 0)))
    j = (await admin.post("/api/nowcast/recompute")).json()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", cookies=dict(admin.cookies)) as browser:   # <img>: cookie, no CSRF header
        r = await browser.get(j["leads"][5]["png"])
        assert r.status_code == 200 and r.headers["content-type"] == "image/png" and "immutable" in r.headers["cache-control"]
        assert Image.open(io.BytesIO(r.content)).mode == "RGBA"
        assert (await browser.get(f"/api/nowcast/frames/{j['run']}/7.png")).status_code == 404       # not a multiple of the step
        assert (await browser.get(f"/api/nowcast/frames/{j['run']}/95.png")).status_code == 404       # beyond the horizon
    j2 = (await admin.post("/api/nowcast/recompute")).json()
    assert j2["run"] == j["run"] + 1
    assert (await admin.get(j["leads"][0]["png"])).status_code == 404                                  # replaced by the newer run
    assert (await admin.get(j2["leads"][0]["png"])).status_code == 200


async def test_forecast_png_has_the_storm_ahead_of_where_it_is_now(admin, app):
    import numpy as np
    await admin.put("/api/locations", json=[HOME])
    await seed(app, moving(4, start=(170, 256), v_px=(5, 0)))
    j = (await admin.post("/api/nowcast/recompute")).json()
    (s, w), (n, e) = j["bounds"]

    def centre_lon(png):
        a = np.asarray(Image.open(io.BytesIO(png)))[..., 3] > 0
        xs = np.nonzero(a.any(axis=0))[0]
        return w + (xs.mean() / a.shape[1]) * (e - w)
    early = centre_lon((await admin.get(j["leads"][0]["png"])).content)       # +5 min
    late = centre_lon((await admin.get(j["leads"][8]["png"])).content)        # +45 min
    assert late > early + 0.2                                                    # moved east (~35 km in 40 min)


async def test_settings_roundtrip_validate_and_take_effect(admin, app):
    await admin.put("/api/locations", json=[HOME])
    await seed(app, moving(4, start=(170, 256), v_px=(5, 0)))
    d = (await admin.get("/api/nowcast/settings")).json()
    assert d["horizon_min"] == 90 and d["enabled"] is True
    assert (await admin.put("/api/nowcast/settings", json={**d, "horizon_min": 5000})).status_code == 422
    assert (await admin.put("/api/nowcast/settings", json={**d, "horizon_min": 45, "step_min": 5})).status_code == 200
    j = (await admin.get("/api/nowcast")).json()
    assert j["horizon_min"] == 45 and len(j["leads"]) == 9                       # applied immediately, not at the next frame
    assert (await admin.put("/api/nowcast/settings", json={**d, "enabled": False})).status_code == 200
    off = (await admin.get("/api/nowcast")).json()
    assert off["available"] is False and off["state"] == "disabled"


async def test_station_cross_check_uses_the_latest_reading(admin, app):
    from stormtracker.timeutil import utcnow
    await admin.put("/api/locations", json=[HOME])
    await seed(app, moving(4, start=(170, 256), v_px=(5, 0)))
    now = utcnow().replace(microsecond=0)
    await app.state.core.repo.save([
        Observation(source="ecowitt", station_id="s", lat=LAT, lon=LON, ts=now - timedelta(minutes=60), values={"pressure_hpa": 1010.0, "rain_rate_mmh": 0.0}),
        Observation(source="ecowitt", station_id="s", lat=LAT, lon=LON, ts=now - timedelta(minutes=1), values={"pressure_hpa": 1006.5, "rain_rate_mmh": 0.0, "wind_gust_ms": 5.0})])
    st = (await admin.post("/api/nowcast/recompute")).json()["impact"]["station"]
    assert st["available"] and st["agrees_with_radar"] is True and st["pressure_change_1h_hpa"] == -3.5 and st["pressure_falling_fast"] is True


async def test_auth_required(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anon:
        for path in ("/api/nowcast", "/api/nowcast/settings", "/api/nowcast/frames/1/5.png"):
            assert (await anon.get(path)).status_code == 401, path
        assert (await anon.post("/api/nowcast/recompute")).status_code == 401
        assert (await anon.put("/api/nowcast/settings", json={})).status_code == 401


async def test_many_frames_with_a_gap_still_forecast_from_the_recent_run(admin, app):
    await admin.put("/api/locations", json=[HOME])
    old = moving(3, start=(100, 256), v_px=(5, 0))
    new = [(ts + timedelta(minutes=60), a) for ts, a in moving(3, start=(170, 256), v_px=(5, 0))]       # 45 min hole in between
    await seed(app, old + new)
    j = (await admin.post("/api/nowcast/recompute")).json()
    assert j["available"] and j["motion"]["frames_used"] == 3 and j["motion"]["speed_kmh"] == pytest.approx(30, rel=0.08)
