import json
from pathlib import Path

import httpx
import pytest

from stormtracker.plugins import HealthState
from stormtracker.sources.wunderground_pws.plugin import API_URL, WundergroundPws, WundergroundPwsConfig

FX = Path(__file__).parents[2] / "fixtures" / "wunderground_pws"
KEY = "wu-secret-789"


def cfg():
    return WundergroundPwsConfig(api_key=KEY, wu_station_id="IDUMMY123")


async def test_current_metric_roundtrip(make_ctx):
    seen = []

    def h(req):
        seen.append(req)
        return httpx.Response(200, json=json.loads((FX / "current_metric.json").read_text()))
    p = WundergroundPws(cfg(), make_ctx(h))
    items = p.normalise(await p.fetch())
    q = seen[0].url.params
    assert str(seen[0].url).startswith(API_URL) and q["stationId"] == "IDUMMY123" and q["units"] == "m" and q["apiKey"] == KEY
    o = items[0]
    assert o.station_id == "wu_pws" and (o.lat, o.lon) == (-33.0, 151.0)
    assert o.ts.isoformat() == "2026-11-03T09:56:00+00:00"
    v = o.values
    assert v["temp_c"] == 20.0 and v["dewpoint_c"] == 15.2 and v["humidity_pct"] == 73 and v["wind_dir_deg"] == 225
    assert v["wind_speed_ms"] == pytest.approx(3.5) and v["wind_gust_ms"] == pytest.approx(7.1, abs=0.01)
    assert v["pressure_hpa"] == 1013.2 and v["rain_rate_mmh"] == 5.0 and v["rain_daily_mm"] == 12.4
    assert v["solar_wm2"] == 310.5 and v["uv_index"] == 3.0


async def test_204_no_recent_data_is_degraded_not_error(make_ctx):
    p = WundergroundPws(cfg(), make_ctx(lambda req: httpx.Response(204)))
    assert p.normalise(await p.fetch()) == []
    p.ctx.state.state = HealthState.OK
    h = await p.health()
    assert h.state == HealthState.DEGRADED and "last hour" in h.last_error


async def test_401_raises(make_ctx):
    p = WundergroundPws(cfg(), make_ctx(lambda req: httpx.Response(401, json={"success": False})))
    with pytest.raises(httpx.HTTPStatusError):
        await p.fetch()


def test_station_id_validated():
    with pytest.raises(ValueError):
        WundergroundPwsConfig(api_key=KEY, wu_station_id="bad id!")
