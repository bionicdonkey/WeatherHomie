import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from stormtracker.sources.open_meteo.plugin import (HOURLY, OpenMeteo, OpenMeteoConfig, bulk_shear, build_hours, potential, summarise)

FX = Path(__file__).parents[2] / "fixtures" / "open_meteo"
DOC = json.loads((FX / "forecast_convective.synthetic.json").read_text())
HOME = {"id": "home", "name": "Home", "lat": -33.87, "lon": 151.21}
RECEIVED = datetime(2026, 11, 6, 23, 20, tzinfo=UTC)          # the fixture starts at 23:00 UTC


def test_bulk_shear_is_the_vector_difference_between_surface_and_500hpa_wind():
    assert bulk_shear((3.0, 90), (18.0, 270)) == 21.0                    # easterly at the surface, westerly aloft: they add
    assert bulk_shear((10.0, 270), (10.0, 270)) == 0.0                   # same wind: no shear
    assert bulk_shear((10.0, 0), (10.0, 90)) == pytest.approx(14.1, abs=0.05)
    assert bulk_shear((None, 90), (18.0, 270)) is None and bulk_shear((3.0, None), (18.0, 270)) is None


def test_storm_potential_thresholds_and_organisation():
    assert potential(100, 30)["level"] == "none" and potential(None, None) == {"level": "none", "organised": False}
    assert potential(600, 5)["level"] == "low" and potential(1800, 5) == {"level": "moderate", "organised": False}
    assert potential(1800, 16) == {"level": "moderate", "organised": True} and potential(3000, 20) == {"level": "high", "organised": True}
    assert potential(600, 40)["organised"] is False                                  # shear alone is not enough without instability


def test_hours_and_summary_from_the_fixture():
    hours = build_hours(DOC["hourly"])
    assert len(hours) == 24 and hours[0]["t"] == "2026-11-06T23:00:00+00:00" and hours[0]["shear_ms"] == 21.0
    s = summarise(hours, RECEIVED)
    assert s["max_cape_jkg"] == pytest.approx(2600, rel=0.01) and s["max_cape_at"] == "2026-11-07T04:00:00+00:00"       # 3 pm Sydney
    assert s["min_lifted_index"] == pytest.approx(-3.0, abs=0.1) and s["max_shear_ms"] == 21.0
    assert s["potential"] == {"level": "high", "organised": True} and s["total_precip_mm"] > 1
    late = summarise(hours, RECEIVED + timedelta(hours=40))                          # nothing in the window: no crash, no potential
    assert late["potential"]["level"] == "none"


def test_missing_variables_become_none_not_errors():
    h = {"time": [1793923200, 1793926800], "cape": [100.0, None]}
    hours = build_hours(h)
    assert hours[0]["cape_jkg"] == 100.0 and hours[1]["cape_jkg"] is None and hours[0]["shear_ms"] is None and hours[0]["precip_prob_pct"] is None


async def test_fetch_asks_for_the_right_things_and_normalise_makes_one_refreshable_event(make_ctx):
    seen = []

    def h(req):
        seen.append(req)
        return httpx.Response(200, json=DOC)
    p = OpenMeteo(OpenMeteoConfig(location_id="home", forecast_hours=24), make_ctx(h, locations=[HOME]))
    raw = await p.fetch()
    q = seen[0].url.params
    assert str(seen[0].url).startswith("https://api.open-meteo.com/v1/forecast") and q["latitude"] == "-33.87" and q["longitude"] == "151.21"
    assert q["wind_speed_unit"] == "ms" and q["timezone"] == "UTC" and q["timeformat"] == "unixtime" and q["forecast_hours"] == "24" and q["hourly"] == ",".join(HOURLY)
    raw["received"] = RECEIVED.isoformat()
    (ev,) = p.normalise(raw)
    assert ev.event_type == "other" and ev.external_id == "forecast:home" and ev.source == "open_meteo" and ev.ts == RECEIVED
    pr = ev.properties
    assert pr["kind"] == "model_forecast" and pr["summary"]["potential"]["level"] == "high" and len(pr["hours"]) == 24 and "Open-Meteo" in pr["attribution"]
    assert p.ctx.state.detail["potential"] == "high"


async def test_errors_are_explained_and_a_location_is_required(make_ctx):
    def bad(req):
        return httpx.Response(400, json={"error": True, "reason": "Latitude must be in range of -90 to 90°. Given: 999.0."})
    with pytest.raises(RuntimeError, match="400: Latitude must be in range"):
        await OpenMeteo(OpenMeteoConfig(), make_ctx(bad, locations=[HOME])).fetch()
    with pytest.raises(RuntimeError, match="no matching location"):
        await OpenMeteo(OpenMeteoConfig(), make_ctx(lambda r: httpx.Response(200, json=DOC), locations=[])).fetch()
    with pytest.raises(RuntimeError, match="no matching location"):
        await OpenMeteo(OpenMeteoConfig(location_id="elsewhere"), make_ctx(lambda r: httpx.Response(200, json=DOC), locations=[HOME])).fetch()
    p = OpenMeteo(OpenMeteoConfig(), make_ctx(lambda r: httpx.Response(200), locations=[HOME]))
    with pytest.raises(RuntimeError, match="no hourly forecast"):
        p.normalise({"json": {"nothing": 1}, "received": RECEIVED.isoformat(), "location": HOME})
    with pytest.raises(ValueError):
        OpenMeteoConfig(forecast_hours=5)


async def test_api_serves_the_latest_forecast_and_refreshes_it_in_place(admin, app, make_ctx):
    p = OpenMeteo(OpenMeteoConfig(), make_ctx(lambda r: httpx.Response(200), locations=[HOME]))
    repo = app.state.core.repo
    await repo.save(p.normalise({"json": DOC, "received": RECEIVED.isoformat(), "location": HOME}))
    await repo.save(p.normalise({"json": DOC, "received": (RECEIVED + timedelta(hours=1)).isoformat(), "location": HOME}))
    got = (await admin.get("/api/forecast")).json()
    assert len(got) == 1 and got[0]["location_id"] == "home" and got[0]["issued"] == (RECEIVED + timedelta(hours=1)).isoformat()
    assert got[0]["summary"]["potential"]["level"] == "high" and len(got[0]["hours"]) == 24 and "Open-Meteo" in got[0]["attribution"]
    assert (await admin.get("/api/events?event_type=other")).json()[0]["external_id"] == "forecast:home"
