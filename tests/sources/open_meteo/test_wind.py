"""Model wind: hourly gusts and direction, the wind-arrow grid, and /api/wind."""
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from stormtracker.sources.open_meteo.plugin import GRID_N, OpenMeteo, OpenMeteoConfig, build_hours, grid_points, parse_wind_grid, summarise
from tests.sources.open_meteo.test_open_meteo import DOC, HOME, RECEIVED


def test_hourly_gusts_and_direction_reach_the_hours_and_the_summary():
    t0 = int(RECEIVED.timestamp()) // 3600 * 3600
    h = {"time": [t0, t0 + 3600, t0 + 7200], "wind_speed_10m": [5, 9, 6], "wind_direction_10m": [200, 250, 180], "wind_gusts_10m": [9.0, 21.5, 12.0]}
    hours = build_hours(h)
    assert hours[1]["gust_10m_ms"] == 21.5 and hours[1]["wind_dir_10m_deg"] == 250
    s = summarise(hours, datetime.fromtimestamp(t0, UTC))
    assert s["max_gust_ms"] == 21.5 and s["max_gust_at"] == hours[1]["t"]
    assert build_hours({"time": [t0]})[0]["gust_10m_ms"] is None                      # a model without gusts: None, not an error


def test_grid_is_centred_evenly_spaced_and_widens_in_longitude():
    pts = grid_points(-33.87, 151.21, 25)
    assert len(pts) == GRID_N ** 2 and pts[12] == (-33.87, 151.21)                    # the middle point is home
    lats = sorted({p[0] for p in pts})
    assert lats[1] - lats[0] == pytest.approx(25 / 111.2, abs=0.002) and lats[-1] - lats[0] == pytest.approx(4 * 25 / 111.2, abs=0.005)
    lons = sorted({p[1] for p in pts})
    assert lons[1] - lons[0] > lats[1] - lats[0]                                       # a degree of longitude is shorter than one of latitude
    assert len(grid_points(-80, 0, 25)) == 25                                         # near a pole it doesn't divide by zero


def test_parse_skips_bad_points_and_accepts_a_single_object():
    pts = [(-33.0, 150.0), (-33.1, 150.0), (-33.2, 150.0)]
    reply = [{"current": {"wind_speed_10m": 4.0, "wind_direction_10m": 180, "wind_gusts_10m": 8.0}}, {"current": {"wind_speed_10m": None}}, {"error": True}]
    assert parse_wind_grid(reply, pts) == [{"lat": -33.0, "lon": 150.0, "speed_ms": 4.0, "dir_deg": 180, "gust_ms": 8.0}]
    one = {"current": {"wind_speed_10m": 3.0, "wind_direction_10m": 90}}
    assert parse_wind_grid(one, pts[:1])[0]["gust_ms"] is None and parse_wind_grid("junk", pts) == []


async def test_fetch_makes_one_extra_request_for_the_grid_and_survives_its_failure(make_ctx):
    seen = []

    def ok(req):
        seen.append(req)
        if "hourly" in req.url.params:
            return httpx.Response(200, json=DOC)
        n = len(req.url.params["latitude"].split(","))
        return httpx.Response(200, json=[{"current": {"wind_speed_10m": 6.0 + i, "wind_direction_10m": 200, "wind_gusts_10m": 11.0}} for i in range(n)])
    p = OpenMeteo(OpenMeteoConfig(location_id="home", wind_grid_km=20), make_ctx(ok, locations=[HOME]))
    raw = await p.fetch()
    assert len(seen) == 2 and seen[1].url.params["current"] == "wind_speed_10m,wind_direction_10m,wind_gusts_10m" and len(seen[1].url.params["latitude"].split(",")) == 25
    raw["received"] = RECEIVED.isoformat()
    forecast, field = p.normalise(raw)
    assert forecast.external_id == "forecast:home" and field.external_id == "windfield:home" and len(field.properties["points"]) == 25
    assert field.properties["kind"] == "model_wind_field" and p.ctx.state.detail["wind_grid_points"] == 25

    def grid_fails(req):
        return httpx.Response(200, json=DOC) if "hourly" in req.url.params else httpx.Response(429, json={"error": True, "reason": "limit"})
    p2 = OpenMeteo(OpenMeteoConfig(location_id="home"), make_ctx(grid_fails, locations=[HOME]))
    raw2 = await p2.fetch()
    assert "grid" not in raw2 and raw2["grid_error"] == "HTTPStatusError"
    raw2["received"] = RECEIVED.isoformat()
    assert [e.external_id for e in p2.normalise(raw2)] == ["forecast:home"]                       # the forecast is unaffected

    off = OpenMeteo(OpenMeteoConfig(location_id="home", wind_grid=False), make_ctx(ok, locations=[HOME]))
    seen.clear(); raw3 = await off.fetch()
    assert len(seen) == 1 and "grid" not in raw3
    raw3["received"] = RECEIVED.isoformat(); off.normalise(raw3)
    assert off.ctx.state.detail["wind_grid_points"] == "off"


async def test_api_wind_serves_the_latest_field_and_refreshes_in_place(admin, app, make_ctx):
    p = OpenMeteo(OpenMeteoConfig(), make_ctx(lambda r: httpx.Response(200), locations=[HOME]))
    repo = app.state.core.repo
    grid = {"points": grid_points(HOME["lat"], HOME["lon"], 25), "json": [{"current": {"wind_speed_10m": 5.0, "wind_direction_10m": 220}}] * 25}
    for k in (0, 1):
        await repo.save(p.normalise({"json": DOC, "received": (RECEIVED + timedelta(hours=k)).isoformat(), "location": HOME, "grid": grid}))
    got = (await admin.get("/api/wind")).json()
    assert len(got) == 1 and got[0]["location_id"] == "home" and len(got[0]["points"]) == 25 and got[0]["issued"] == (RECEIVED + timedelta(hours=1)).isoformat()
    assert len((await admin.get("/api/forecast")).json()) == 1                                    # the forecast endpoint is not polluted by the field
