"""Hostile and malformed input must produce a 4xx (or a clean 2xx), never a 500."""
import json
from urllib.parse import quote

import pytest
from httpx import ASGITransport, AsyncClient

NASTY = ["", " ", "null", "NaN", "inf", "-1", "0", "99999999999999999999", "1e309", "../../etc/passwd", "%00", "\x00", "a" * 5000, "' OR 1=1 --", "<script>alert(1)</script>", "😀", "2026-13-45T99:99:99", "9999-12-31T23:59:59+99:00"]


@pytest.fixture
async def ctx(admin, app):
    from stormtracker.sources.ecowitt.plugin import Ecowitt
    from stormtracker.sources.lightning_feed.plugin import LightningFeed
    app.state.core.registry.register(Ecowitt)
    app.state.core.registry.register(LightningFeed)
    await admin.put("/api/locations", json=[{"id": "home", "name": "Home", "lat": -33.0, "lon": 151.0}])
    await admin.put("/api/sources/ecowitt", json={"enabled": True, "secrets": {"push_token": "tok-" + "a" * 20}})
    await admin.put("/api/sources/lightning_feed", json={"enabled": True, "secrets": {"push_token": "tok-" + "b" * 20}})
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://device") as dev:
        yield admin, dev


def ok(r, where):
    assert r.status_code < 500, f"{where}: {r.status_code} {r.text[:200]}"


async def test_query_parameters_never_cause_a_server_error(ctx):
    admin, _ = ctx
    for v in NASTY:
        for path in ("/api/observations?limit={}", "/api/observations?since={}", "/api/observations?until={}", "/api/observations?source={}&station_id={}",
                     "/api/observations?order={}", "/api/events?since={}", "/api/events?limit={}", "/api/events?event_type={}", "/api/audit?x={}",
                     "/api/radar/frames?hours={}", "/api/radar/frames?limit={}", "/api/radar/frames?radar_id={}", "/api/alerts/history?limit={}",
                     "/api/radar/overlays/{}/background.png", "/api/radar/overlays/IDR713/{}.png"):
            ok(await admin.get(path.replace("{}", quote(v, safe=""))), path.replace("{}", repr(v)[:30]))


async def test_bodies_never_cause_a_server_error(ctx):
    admin, _ = ctx
    junk = [None, [], {}, "x", 5, {"a": {"b": {"c": [1] * 50}}}, [{"id": "x"}] * 300, {"enabled": "yes"}, {"enabled": None, "config": "nope"}, {"config": {"x": None}},
            {"secrets": {"push_token": 5}}, {"secrets": "nope"}, {"secrets": {"push_token": "a" * 100000}}, {"interval_s": "fast"}, {"interval_s": -5}]
    for body in junk:
        for method, path in (("PUT", "/api/locations"), ("PUT", "/api/sources/ecowitt"), ("PUT", "/api/sources/nope"), ("PUT", "/api/ui"), ("PUT", "/api/nowcast/settings"),
                             ("PUT", "/api/alerts/settings"), ("PUT", "/api/alerts/outputs/webhook"), ("PUT", "/api/alerts/outputs/apprise"), ("PUT", "/api/alerts/outputs/email"),
                             ("POST", "/api/alerts/rules"), ("PUT", "/api/alerts/rules/x"), ("POST", "/api/setup"), ("POST", "/api/auth/login")):
            r = await admin.request(method, path, content=json.dumps(body), headers={"Content-Type": "application/json"})
            ok(r, f"{method} {path} {str(body)[:40]}")
    r = await admin.post("/api/alerts/rules", content=b"\xff\xfe not json", headers={"Content-Type": "application/json"})
    ok(r, "binary body")


async def test_ingest_endpoints_survive_garbage(ctx):
    _, dev = ctx
    bodies = [b"", b"\x00\x01\x02", b"{", b'{"strikes": "x"}', b'{"strikes": [null, 1, "a", [], {"lat": "x"}]}', b"a=1&" * 5000, b"PASSKEY=&dateutc=x&tempf=nan&windspeedmph=inf",
              b"tempf=" + b"9" * 400, json.dumps({"strikes": [{"time": 10 ** 30, "lat": 1e999, "lon": "x"}]}).encode(), b"x" * 70000]
    for b in bodies:
        for path in ("/ingest/ecowitt/tok-" + "a" * 20, "/ingest/lightning_feed/tok-" + "b" * 20, "/ingest/nope/x", "/ingest/ecowitt/%00", "/ingest/ecowitt/" + "z" * 3000):
            for method in ("POST", "GET", "PUT"):
                for ct in ("application/x-www-form-urlencoded", "application/json", "text/plain", "multipart/form-data; boundary=x"):
                    r = await dev.request(method, path, content=b, headers={"Content-Type": ct})
                    ok(r, f"{method} {path[:30]} {ct} {b[:20]!r}")


async def test_unknown_methods_and_paths(ctx):
    admin, dev = ctx
    for m in ("DELETE", "PATCH", "TRACE", "OPTIONS", "HEAD"):
        for p in ("/api/sources", "/api/alerts/rules", "/api/nowcast", "/", "/ingest/ecowitt/x", "/api/radar/frames/1.png"):
            ok(await admin.request(m, p), f"{m} {p}")
    for p in ("/api/radar/frames/99999999999999999999.png", "/api/nowcast/frames/1/1e9.png", "/api/nowcast/frames/-1/5.png", "/../../etc/passwd", "/%2e%2e/%2e%2e/etc/passwd", "/js/../../../../etc/passwd"):
        r = await admin.get(p)
        ok(r, p)
        assert "root:" not in r.text
