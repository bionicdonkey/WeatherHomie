import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from stormtracker.plugins import HealthState
from stormtracker.sources.bom_observations.plugin import BomObservations, BomObservationsConfig, convert_entry, parse_ts

FX = Path(__file__).parents[2] / "fixtures" / "bom_observations"
SYN = json.loads((FX / "IDN60801.99999.synthetic.json").read_text())
RECEIVED = "2026-11-06T11:20:00+00:00"


def plugin(make_ctx, handler=None, **cfg):
    handler = handler or (lambda r: httpx.Response(200, json=SYN))
    return BomObservations(BomObservationsConfig(wmo=99999, **cfg), make_ctx(handler))


def test_conversion_units_placeholders_and_calm():
    a, b, c, _ = SYN["observations"]["data"]
    v = convert_entry(a)
    assert v == {"temp_c": 17.4, "apparent_temp_c": 17.7, "dewpoint_c": 13.5, "humidity_pct": 78.0, "pressure_hpa": 1015.6, "pressure_station_hpa": 1015.4,
                 "wind_speed_ms": 5.0, "wind_gust_ms": 10.0, "rain_since_9am_mm": 2.4, "visibility_km": 10.0, "delta_t_c": 2.2, "wind_dir_deg": 247.5}
    v = convert_entry(b)                                          # CALM: speed 0, no direction; null gust, "-" visibility skipped
    assert v["wind_speed_ms"] == 0.0 and "wind_dir_deg" not in v and "wind_gust_ms" not in v and "visibility_km" not in v
    v = convert_entry(c)                                          # "-" and null placeholders never become numbers
    assert "apparent_temp_c" not in v and "pressure_hpa" not in v and "dewpoint_c" not in v and "rain_since_9am_mm" not in v
    assert v["temp_c"] == 18.6 and v["wind_dir_deg"] == 337.5 and v["wind_gust_ms"] == pytest.approx(6.944, abs=0.001)
    assert convert_entry({}) == {} and all(k == k.lower() for k in convert_entry(a))


def test_timestamps_are_utc_from_the_aifs_field():
    assert parse_ts({"aifstime_utc": "20261106110000"}) == datetime(2026, 11, 6, 11, 0, tzinfo=UTC)
    assert parse_ts({"aifstime_utc": "garbage"}) is None and parse_ts({}) is None


def test_normalise_keeps_recent_history_only_and_uses_the_station_position(make_ctx):
    p = plugin(make_ctx, history_hours=48)
    items = p.normalise({"json": SYN, "received": RECEIVED})
    assert [i.ts.isoformat() for i in items] == ["2026-11-06T11:00:00+00:00", "2026-11-06T10:30:00+00:00", "2026-11-06T10:00:00+00:00"]   # the Nov 3 entry is older than 48 h
    assert all(i.source == "bom_observations" and i.station_id == "bom_99999" and (i.lat, i.lon) == (-33.93, 151.23) for i in items)
    assert len(plugin(make_ctx, history_hours=96).normalise({"json": SYN, "received": RECEIVED})) == 4
    assert plugin(make_ctx, station_id="my_station").normalise({"json": SYN, "received": RECEIVED})[0].station_id == "my_station"


def test_records_without_usable_data_or_position_are_skipped(make_ctx):
    bad = {"observations": {"data": [{"aifstime_utc": "20261106110000", "lat": -33.93, "lon": 151.23},                  # nothing numeric
                                      {"aifstime_utc": "20261106103000", "air_temp": 10.0},                           # no position
                                      {"aifstime_utc": "nonsense", "air_temp": 10.0, "lat": -33.93, "lon": 151.23}]}}   # no time
    assert plugin(make_ctx).normalise({"json": bad, "received": RECEIVED}) == []


async def test_fetch_requests_the_product_url_and_rejects_other_replies(make_ctx):
    seen = []

    def h(req):
        seen.append(req)
        return httpx.Response(200, json=SYN)
    p = plugin(make_ctx, h, product_id="IDN60801")
    raw = await p.fetch()
    assert str(seen[0].url) == "https://reg.bom.gov.au/fwo/IDN60801/IDN60801.99999.json" and raw["json"] is SYN or raw["json"] == SYN
    with pytest.raises(RuntimeError, match="not a BOM observations product"):
        await plugin(make_ctx, lambda r: httpx.Response(200, json={"hello": "world"})).fetch()
    with pytest.raises(RuntimeError):
        await plugin(make_ctx, lambda r: httpx.Response(200, json=[1, 2])).fetch()
    with pytest.raises(httpx.HTTPStatusError):
        await plugin(make_ctx, lambda r: httpx.Response(404)).fetch()


async def test_health_goes_degraded_when_the_station_stops_reporting(make_ctx, monkeypatch):
    clock = {"now": datetime(2026, 11, 6, 11, 50, tzinfo=UTC)}                      # the clock is pinned: no dependence on today's date
    monkeypatch.setattr("stormtracker.sources.bom_observations.plugin.utcnow", lambda: clock["now"])
    p = plugin(make_ctx)
    p.normalise({"json": SYN, "received": RECEIVED})                                # newest reading 11:00
    p.ctx.state.state = HealthState.OK
    assert (await p.health()).state == HealthState.OK                               # 50 min old: normal for a half-hourly station
    clock["now"] = datetime(2026, 11, 6, 13, 0, tzinfo=UTC)                         # 2 h old
    h = await p.health()
    assert h.state == HealthState.DEGRADED and "last reported 120 min ago" in h.last_error


def test_config_validation():
    for bad in ({"wmo": 0}, {"wmo": 99999, "product_id": "nope"}, {"wmo": 99999, "history_hours": 500}, {"wmo": 99999, "http_base": "ftp://x"}):
        with pytest.raises(ValueError):
            BomObservationsConfig(**bad)
    assert BomObservationsConfig(wmo=94768).product_id == "IDN60801"
    assert BomObservations.meta.licence.type == "personal" and "copyright" in BomObservations.meta.licence.terms_url


async def test_end_to_end_through_the_api_and_idempotent(admin, app):
    app.state.core.registry.register(BomObservations)
    app.state.core.alerts.http  # (touch: the app is fully built)
    from stormtracker.models import Observation
    p = BomObservations(BomObservationsConfig(wmo=99999), type("Ctx", (), {})())
    items = p.normalise({"json": SYN, "received": RECEIVED})
    repo = app.state.core.repo
    assert await repo.save(items) == 4 and await repo.save(items) == 0                   # a second identical poll adds nothing
    got = (await admin.get("/api/observations?source=bom_observations&order=asc&limit=10")).json()
    assert len(got) == 4 and got[-1]["values"]["temp_c"] == 17.4 and got[-1]["ts"] == "2026-11-06T11:00:00+00:00"


REAL = sorted((FX / "real").glob("IDN*.*.json"))


@pytest.mark.skipif(not REAL, reason="no real BOM download in tests/fixtures/bom_observations/real/ (git-ignored; BOM's copyright applies)")
def test_a_real_bom_download_parses(make_ctx):
    wmo = int(REAL[0].name.split(".")[1])
    real = json.loads(REAL[0].read_text())
    p = BomObservations(BomObservationsConfig(wmo=wmo, history_hours=96), make_ctx(lambda r: httpx.Response(200)))
    newest = max(datetime.strptime(e["aifstime_utc"], "%Y%m%d%H%M%S").replace(tzinfo=UTC) for e in real["observations"]["data"])
    items = p.normalise({"json": real, "received": (newest + timedelta(minutes=10)).isoformat()})
    assert len(items) > 100 and items[0].station_id == f"bom_{wmo}" and -90 < items[0].lat < 0
    assert all(-5 < i.values["temp_c"] < 50 and 900 < i.values["pressure_hpa"] < 1100 and 0 <= i.values["humidity_pct"] <= 100 for i in items if "pressure_hpa" in i.values)
    assert len({i.ts for i in items}) == len(items)                                      # no duplicate times: safe for the unique constraint
