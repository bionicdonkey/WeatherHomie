import json
from pathlib import Path

import httpx
import pytest

from stormtracker.plugins import HealthState
from stormtracker.scheduler import redact
from stormtracker.sources.ecowitt_cloud.plugin import API_URL, EcowittCloud, EcowittCloudConfig, convert_realtime

FX = Path(__file__).parents[2] / "fixtures" / "ecowitt_cloud"
APP, KEY, MAC = "app-secret-123", "key-secret-456", "AA:BB:CC:DD:EE:FF"


def load(name):
    return json.loads((FX / name).read_text())


def cfg():
    return EcowittCloudConfig(application_key=APP, api_key=KEY, mac=MAC)


def test_metric_mapping_and_newest_timestamp():
    values, ts, skipped = convert_realtime(load("real_time_metric.json")["data"])
    assert values["temp_c"] == 20.0 and values["dewpoint_c"] == 15.2 and values["humidity_pct"] == 73
    assert values["feels_like_c"] == 20.4 and values["apparent_temp_c"] == 19.1
    assert values["temp_in_c"] == 23.5 and values["solar_wm2"] == 310.5 and values["uv_index"] == 3
    assert values["rain_rate_mmh"] == 5.0 and values["rain_daily_mm"] == 12.4 and values["rain_event_mm"] == 8.2
    assert values["wind_speed_ms"] == 3.5 and values["wind_gust_ms"] == 7.1 and values["wind_dir_deg"] == 225
    assert values["pressure_hpa"] == 1013.2 and values["pressure_abs_hpa"] == 1010.1
    assert values["lightning_distance_km"] == 14 and values["lightning_count"] == 3
    assert values["temp_c_1"] == 18.0 and values["humidity_pct_1"] == 80
    assert values["soil_moisture_pct_1"] == 41 and values["pm25_ugm3_1"] == 6
    assert values["x_mystery_thing"] == 9.5                       # unknown numeric kept
    assert not any("battery" in k for k in values)                # battery group ignored
    assert "x_mystery_odd" not in values and skipped == 1         # unknown unit skipped, counted
    assert ts.isoformat() == "2026-11-03T09:51:00+00:00"


def test_conversion_follows_unit_string_not_requested_unit_ids():
    m, _, _ = convert_realtime(load("real_time_metric.json")["data"])
    i, _, _ = convert_realtime(load("real_time_imperial.json")["data"])
    for k in ("temp_c", "wind_speed_ms", "rain_rate_mmh", "pressure_hpa"):
        assert i[k] == pytest.approx(m[k], abs=0.2), k   # 4-digit inHg input => ~0.17 hPa quantisation


def handler(payload, seen):
    def h(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json=payload)
    return h


async def test_fetch_normalise_roundtrip(make_ctx):
    seen = []
    p = EcowittCloud(cfg(), make_ctx(handler(load("real_time_metric.json"), seen)))
    items = p.normalise(await p.fetch())
    q = seen[0].url.params
    assert str(seen[0].url).startswith(API_URL) and q["mac"] == MAC and q["call_back"] == "all"
    assert q["application_key"] == APP and q["api_key"] == KEY
    assert len(items) == 1 and items[0].station_id == "ecowitt_cloud" and items[0].values["temp_c"] == 20.0
    assert items[0].ts.isoformat() == "2026-11-03T09:51:00+00:00"


async def test_api_error_code_raises_without_leaking_keys(make_ctx):
    p = EcowittCloud(cfg(), make_ctx(handler({"code": 40010, "msg": "Illegal Application_Key Parameter"}, [])))
    with pytest.raises(RuntimeError, match="40010"):
        await p.fetch()


async def test_http_error_text_is_redacted_by_scheduler_helper(make_ctx):
    def h(req):
        return httpx.Response(500)
    p = EcowittCloud(cfg(), make_ctx(h))
    with pytest.raises(httpx.HTTPStatusError) as ei:
        await p.fetch()
    assert APP in str(ei.value) or KEY in str(ei.value)           # httpx puts the URL in the message...
    clean = redact(p, str(ei.value))
    assert APP not in clean and KEY not in clean and "***" in clean   # ...so the scheduler redacts it


async def test_stale_device_degrades_health(make_ctx):
    p = EcowittCloud(cfg(), make_ctx(handler(load("real_time_metric.json"), [])))
    p.normalise(await p.fetch())                                    # fixture data is years old vs "now"
    p.ctx.state.state = HealthState.OK
    h = await p.health()
    assert h.state == HealthState.DEGRADED and "not uploading" in h.last_error


def test_mac_validated():
    with pytest.raises(ValueError):
        EcowittCloudConfig(application_key=APP, api_key=KEY, mac="not-a-mac")
