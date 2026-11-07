import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from stormtracker.plugins import HealthState
from stormtracker.sources.ecowitt.plugin import Ecowitt, EcowittConfig, parse_dateutc
from stormtracker.timeutil import utcnow

FX = Path(__file__).parents[2] / "fixtures" / "ecowitt"
TOKEN = "tok-" + "a" * 20
LOC = {"id": "home", "name": "Home", "lat": -33.0, "lon": 151.0}


def body() -> str:
    return (FX / "ecowitt_push.form").read_text().strip()


@pytest.fixture
async def eco(admin, app):
    app.state.core.registry.register(Ecowitt)
    await admin.put("/api/locations", json=[LOC])
    r = await admin.put("/api/sources/ecowitt", json={"enabled": True, "secrets": {"push_token": TOKEN}})
    assert r.status_code == 200 and r.json()["health"]["state"] in ("ok", "starting")   # waiting for first push
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://device") as dev:   # no session cookie
        yield dev, admin, app


FORM = {"Content-Type": "application/x-www-form-urlencoded"}


async def test_post_ecowitt_protocol_stores_observation_and_lightning_event(eco, monkeypatch):
    dev, admin, _ = eco
    # the fixture's dateutc must be "recent" or the clock-skew guard substitutes receive time
    monkeypatch.setattr("stormtracker.sources.ecowitt.plugin.utcnow", lambda: datetime(2026, 11, 3, 9, 56, 30, tzinfo=UTC))
    r = await dev.post(f"/ingest/ecowitt/{TOKEN}", content=body(), headers=FORM)
    assert r.status_code == 200 and r.text == "success"
    obs = (await admin.get("/api/observations?source=ecowitt")).json()
    assert len(obs) == 1
    o = obs[0]
    assert o["station_id"] == "ecowitt" and (o["lat"], o["lon"]) == (-33.0, 151.0)
    assert o["ts"] == "2026-11-03T09:56:00+00:00"
    assert o["values"]["temp_c"] == pytest.approx(20.0) and o["values"]["rain_rate_mmh"] == pytest.approx(5.0, abs=0.05)
    ev = (await admin.get("/api/events?event_type=lightning")).json()
    assert len(ev) == 1 and ev[0]["properties"]["distance_km"] == 14 and ev[0]["properties"]["detected_by"] == "station_sensor"
    assert ev[0]["ts"] == datetime.fromtimestamp(1793699400, UTC).isoformat()
    # repeat upload (same sensor reading) does not duplicate the event
    await dev.post(f"/ingest/ecowitt/{TOKEN}", content=body().replace("09:56:00", "09:57:00"), headers=FORM)
    assert len((await admin.get("/api/events?event_type=lightning")).json()) == 1
    assert len((await admin.get("/api/observations?source=ecowitt")).json()) == 2


async def test_get_wunderground_protocol(eco):
    dev, admin, _ = eco
    r = await dev.get(f"/ingest/ecowitt/{TOKEN}?" + (FX / "wu_push.query").read_text().strip())
    assert r.status_code == 200 and r.text == "success"
    o = (await admin.get("/api/observations?source=ecowitt")).json()[0]
    assert o["values"]["pressure_hpa"] == pytest.approx(1013.2, abs=0.1) and o["values"]["rain_hourly_mm"] == pytest.approx(0.99, abs=0.01)
    assert (await admin.get("/api/events?event_type=lightning")).json() == []


async def test_token_required_and_constant_path_suffix_tolerated(eco):
    dev, _, _ = eco
    assert (await dev.post("/ingest/ecowitt/wrong", content=body(), headers=FORM)).status_code == 403
    assert (await dev.post("/ingest/ecowitt", content=body(), headers=FORM)).status_code == 403
    assert (await dev.post("/ingest/ecowitt/", content=body(), headers=FORM)).status_code == 403
    # some firmware appends the classic WU path after the configured one
    r = await dev.get(f"/ingest/ecowitt/{TOKEN}/weatherstation/updateweatherstation.php?tempf=60&dateutc=now")
    assert r.status_code == 200


async def test_unset_token_refuses_everything(admin, app):
    app.state.core.registry.register(Ecowitt)
    await admin.put("/api/locations", json=[LOC])
    await admin.put("/api/sources/ecowitt", json={"enabled": True})          # no token set
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://d") as dev:
        assert (await dev.post("/ingest/ecowitt/", content=body(), headers=FORM)).status_code == 403
        assert (await dev.post("/ingest/ecowitt/anything", content=body(), headers=FORM)).status_code == 403


async def test_disabled_source_404(admin, app):
    app.state.core.registry.register(Ecowitt)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://d") as dev:
        assert (await dev.post("/ingest/ecowitt/x", content=body(), headers=FORM)).status_code == 404


async def test_passkey_filter_and_no_location_and_size_limit(eco, app):
    dev, admin, _ = eco
    await admin.put("/api/sources/ecowitt", json={"secrets": {"allowed_passkey": "0123456789ABCDEF0123456789ABCDEF"}})
    assert (await dev.post(f"/ingest/ecowitt/{TOKEN}", content=body().replace("0123456789ABCDEF", "FFFFFFFFFFFFFFFF", 1),
                           headers=FORM)).status_code == 403
    assert (await dev.post(f"/ingest/ecowitt/{TOKEN}", content=body(), headers=FORM)).status_code == 200
    assert (await dev.post(f"/ingest/ecowitt/{TOKEN}", content=body(),
                           headers={**FORM, "content-length": "999999"})).status_code in (413, 400)
    await admin.put("/api/locations", json=[])
    assert (await dev.post(f"/ingest/ecowitt/{TOKEN}", content=body(), headers=FORM)).status_code == 503


async def test_clock_skew_and_now_fall_back_to_receive_time(eco):
    dev, admin, _ = eco
    await dev.post(f"/ingest/ecowitt/{TOKEN}", content="tempf=60&dateutc=2001-01-01+00:00:00", headers=FORM)
    await dev.post(f"/ingest/ecowitt/{TOKEN}", content="tempf=61&dateutc=now", headers=FORM)
    obs = (await admin.get("/api/observations?source=ecowitt")).json()
    assert len(obs) == 2
    for o in obs:
        assert abs(datetime.fromisoformat(o["ts"]) - utcnow()) < timedelta(minutes=1)


def test_parse_dateutc():
    assert parse_dateutc("2026-11-03 09:56:00") == datetime(2026, 11, 3, 9, 56, tzinfo=UTC)
    assert parse_dateutc("2026-11-03%2009:56:00") == datetime(2026, 11, 3, 9, 56, tzinfo=UTC)
    assert parse_dateutc("now") is None and parse_dateutc("garbage") is None and parse_dateutc(None) is None


async def test_health_reports_staleness_and_recovery(eco):
    dev, admin, app = eco
    src = app.state.core.scheduler.running("ecowitt")
    assert (await src.health()).state == HealthState.STARTING          # nothing received yet
    await dev.post(f"/ingest/ecowitt/{TOKEN}", content=body(), headers=FORM)
    h = await src.health()
    assert h.state == HealthState.OK and h.detail["model"] == "GW2000A"
    src._last_push = utcnow() - timedelta(minutes=40)
    h = await src.health()
    assert h.state == HealthState.DEGRADED and "no push received" in h.last_error
    await dev.post(f"/ingest/ecowitt/{TOKEN}", content=body(), headers=FORM)
    assert (await src.health()).state == HealthState.OK


async def test_push_only_is_not_polled_but_gateway_host_polls(eco, app):
    dev, admin, _ = eco
    sched = app.state.core.scheduler
    assert sched.runners["ecowitt"].task is None and sched.running("ecowitt").polling is False
    r = await admin.put("/api/sources/ecowitt", json={"config": {"gateway_host": "192.0.2.10"}})
    assert r.status_code == 200
    assert sched.running("ecowitt").polling is True and sched.runners["ecowitt"].task is not None


def test_config_validation():
    with pytest.raises(ValueError):
        EcowittConfig(gateway_host="http://192.0.2.10/get_livedata_info")
    with pytest.raises(ValueError):
        EcowittConfig(station_id="has space")
    assert EcowittConfig(gateway_host="192.0.2.10:8080").gateway_host == "192.0.2.10:8080"
    assert EcowittConfig(gateway_host="").gateway_host is None


async def test_gateway_poll_fetch_and_normalise(make_ctx):
    from stormtracker.models import Observation
    seen = []

    def handler(req: httpx.Request):
        seen.append(str(req.url))
        return httpx.Response(200, json=json.loads((FX / "gw_livedata_imperial.json").read_text()))

    src = Ecowitt(EcowittConfig(gateway_host="192.0.2.10"), make_ctx(handler))
    raw = await src.fetch()
    assert seen == ["http://192.0.2.10/get_livedata_info"]
    items = src.normalise(raw)
    assert len(items) == 1 and isinstance(items[0], Observation)
    assert items[0].values["temp_c"] == pytest.approx(26.22, abs=0.01) and items[0].ts.microsecond == 0


async def test_gateway_error_propagates_for_scheduler_backoff(make_ctx):
    src = Ecowitt(EcowittConfig(gateway_host="192.0.2.10"), make_ctx(lambda r: httpx.Response(404)))
    with pytest.raises(httpx.HTTPStatusError):
        await src.fetch()


async def test_forwarded_request_with_header_token(eco):
    """A forwarder that can't change the path may send the token as a header instead."""
    dev, admin, _ = eco
    for hdr in ({"X-Stormtracker-Token": TOKEN}, {"Authorization": f"Bearer {TOKEN}"}):
        r = await dev.post("/ingest/ecowitt", content=body(), headers={**FORM, **hdr})
        assert r.status_code == 200, hdr
    assert (await dev.post("/ingest/ecowitt", content=body(), headers={**FORM, "X-Stormtracker-Token": "nope"})).status_code == 403


# ---- forwarding ---------------------------------------------------------------------------------
HA_URL = "http://ha.lan:8123/api/webhook/SECRET-WEBHOOK-ID"
OTHER_URL = "https://relay.example.net/in?x=1"


async def _with_forwards(eco, urls, handler):
    dev, admin, app = eco
    r = await admin.put("/api/sources/ecowitt", json={"secrets": {"forward_urls": urls}})
    assert r.status_code == 200
    src = app.state.core.scheduler.running("ecowitt")
    src.ctx.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return dev, admin, src


async def _drain(src):
    import asyncio
    await asyncio.gather(*list(src._tasks))


async def test_forward_relays_body_unchanged_to_every_target_without_leaking_token(eco):
    seen = []

    async def h(req: httpx.Request):
        seen.append(req)
        return httpx.Response(200)
    dev, admin, src = await _with_forwards(eco, f"# comment\n{HA_URL}\n\n{OTHER_URL}\nnot a url\n", h)
    r = await dev.post(f"/ingest/ecowitt/{TOKEN}?a=1", content=body(), headers=FORM)
    assert r.status_code == 200
    await _drain(src)
    assert len(seen) == 2
    ha, other = seen
    assert ha.content == body().encode() and ha.method == "POST"
    assert str(ha.url) == HA_URL + "?a=1" and str(other.url) == OTHER_URL + "&a=1"
    assert ha.headers["content-type"] == FORM["Content-Type"] and ha.headers["x-stormtracker-forwarded"] == "1"
    assert all(TOKEN not in str(x.url) and TOKEN not in str(x.headers) for x in seen)   # our token never leaves
    d = (await admin.get("/api/sources")).json()
    eco_view = next(s for s in d if s["meta"]["name"] == "ecowitt")
    fwd = eco_view["health"]["detail"]["forward"]
    assert fwd["invalid_lines"] == 1 and [v["ok"] for v in fwd["targets"].values()] == [1, 1]
    assert set(fwd["targets"]) == {"1:ha.lan:8123", "2:relay.example.net:443"}
    assert "SECRET-WEBHOOK-ID" not in str(eco_view)                  # URL path/secret never in API output
    assert len((await admin.get("/api/observations?source=ecowitt")).json()) == 1   # and we still ingested it


async def test_forward_get_wunderground_protocol(eco):
    seen = []

    async def h(req):
        seen.append(req)
        return httpx.Response(200)
    dev, _, src = await _with_forwards(eco, HA_URL, h)
    q = (FX / "wu_push.query").read_text().strip()
    assert (await dev.get(f"/ingest/ecowitt/{TOKEN}?{q}")).status_code == 200
    await _drain(src)
    assert seen[0].method == "GET" and str(seen[0].url) == f"{HA_URL}?{q}" and seen[0].content == b""


async def test_forward_failures_never_affect_the_device_or_ingest(eco):
    async def h(req):
        if "relay" in req.url.host:
            raise httpx.ConnectError(f"boom {req.url}")
        return httpx.Response(503)
    dev, admin, src = await _with_forwards(eco, f"{HA_URL}\n{OTHER_URL}", h)
    r = await dev.post(f"/ingest/ecowitt/{TOKEN}", content=body(), headers=FORM)
    assert r.status_code == 200 and r.text == "success"
    await _drain(src)
    t = src.ctx.state.detail["forward"]["targets"]
    assert t["1:ha.lan:8123"]["failed"] == 1 and t["1:ha.lan:8123"]["last_error"] == "HTTP 503"
    assert t["2:relay.example.net:443"]["last_error"] == "ConnectError"
    assert "SECRET-WEBHOOK-ID" not in str(src.ctx.state.detail) and "boom" not in str(src.ctx.state.detail)
    assert len((await admin.get("/api/observations?source=ecowitt")).json()) == 1


async def test_slow_target_does_not_delay_the_device(eco):
    import asyncio
    gate = asyncio.Event()

    async def h(req):
        await gate.wait()
        return httpx.Response(200)
    dev, _, src = await _with_forwards(eco, HA_URL, h)
    r = await asyncio.wait_for(dev.post(f"/ingest/ecowitt/{TOKEN}", content=body(), headers=FORM), 5)
    assert r.status_code == 200
    assert src.ctx.state.detail["forward"]["targets"]["1:ha.lan:8123"]["pending"] == 1
    gate.set()
    await _drain(src)
    assert src.ctx.state.detail["forward"]["targets"]["1:ha.lan:8123"]["ok"] == 1


async def test_pending_cap_drops_instead_of_piling_up(eco):
    import asyncio
    from stormtracker.sources.ecowitt.plugin import FORWARD_MAX_PENDING
    gate = asyncio.Event()

    async def h(req):
        await gate.wait()
        return httpx.Response(200)
    dev, _, src = await _with_forwards(eco, HA_URL, h)
    for _ in range(FORWARD_MAX_PENDING + 3):
        assert (await dev.post(f"/ingest/ecowitt/{TOKEN}", content=body(), headers=FORM)).status_code == 200
    st = src.ctx.state.detail["forward"]["targets"]["1:ha.lan:8123"]
    assert st["pending"] == FORWARD_MAX_PENDING and st["dropped"] == 3
    gate.set()
    await _drain(src)


async def test_no_forward_loops_and_none_for_rejected_requests(eco):
    seen = []

    async def h(req):
        seen.append(req)
        return httpx.Response(200)
    dev, _, src = await _with_forwards(eco, HA_URL, h)
    # a request that already carries our hop header is ingested but not relayed again
    r = await dev.post(f"/ingest/ecowitt/{TOKEN}", content=body(), headers={**FORM, "X-Stormtracker-Forwarded": "1"})
    assert r.status_code == 200
    # wrong token: nothing relayed
    assert (await dev.post("/ingest/ecowitt/wrong", content=body(), headers=FORM)).status_code == 403
    await _drain(src)
    assert seen == []


def test_parse_forward_urls():
    from stormtracker.sources.ecowitt.plugin import parse_forward_urls
    urls, bad = parse_forward_urls("http://a:1/x\n  \n# c\nftp://x/y\nhttp://\n  https://b/z?k=v  \nhttp://h:notaport/")
    assert [u for u, _ in urls] == ["http://a:1/x", "https://b/z?k=v"]
    assert [l for _, l in urls] == ["1:a:1", "2:b:443"] and bad == 3
    assert parse_forward_urls(None) == ([], 0)


async def test_schema_marks_push_token_for_the_generate_button(eco):
    _, admin, _ = eco
    e = next(s for s in (await admin.get("/api/sources")).json() if s["meta"]["name"] == "ecowitt")
    props = e["schema"]["properties"]
    assert props["push_token"]["secret"] is True and props["push_token"]["generate"] is True
    assert "generate" not in props["allowed_passkey"]                 # only tokens we mint, not values copied from the device
    assert props["forward_urls"]["format"] == "textarea"
