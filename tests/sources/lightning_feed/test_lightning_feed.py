import json
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from stormtracker.plugins import HealthState
from stormtracker.sources.lightning_feed.plugin import LightningFeed, LightningFeedConfig, extract_strikes, parse_time
from stormtracker.timeutil import utcnow

TOKEN = "tok-" + "z" * 20
HOME = {"id": "home", "name": "Home", "lat": -33.87, "lon": 151.21}


def test_time_formats():
    t = datetime(2026, 11, 6, 10, 15, 2, tzinfo=UTC)
    secs = int(t.timestamp())
    for v in ("2026-11-06T10:15:02Z", "2026-11-06T10:15:02", "2026-11-06T21:15:02+11:00", secs, float(secs), str(secs), secs * 1000, secs * 10**6, secs * 10**9):
        got = parse_time(v)
        assert got is not None and abs((got - t).total_seconds()) < 0.001, v
    assert all(parse_time(v) is None for v in ("junk", None, True, [], {}, "2026-13-45", float("inf")))


def test_payload_shapes():
    s = {"time": 1, "lat": 1, "lon": 2}
    assert extract_strikes({"strikes": [s, s]}) == [s, s] and extract_strikes([s]) == [s] and extract_strikes(s) == [s]
    assert extract_strikes("nope") == [] and extract_strikes(None) == []


def norm(make_ctx, strikes, now=None, **cfg):
    p = LightningFeed(LightningFeedConfig(push_token=TOKEN, **cfg), make_ctx(lambda r: None))
    return p.normalise({"payload": strikes, "received": (now or utcnow()).isoformat(), "location": HOME})


def test_filters_by_distance_time_and_validity_and_computes_distance(make_ctx):
    now = utcnow()
    iso = lambda m: (now - timedelta(minutes=m)).isoformat()   # noqa: E731
    evs = norm(make_ctx, {"strikes": [
        {"time": iso(2), "lat": -33.88, "lon": 151.22, "polarity": -1, "amplitude_ka": -14.2},          # ~1.4 km
        {"time": iso(3), "latitude": -33.5, "longitude": 151.0},                                         # alternative field names, ~46 km
        {"time": iso(4), "lat": -37.8, "lon": 145.0},                                                    # Melbourne: beyond 150 km
        {"time": iso(60 * 7), "lat": -33.88, "lon": 151.22},                                             # older than 6 h
        {"time": (now + timedelta(hours=1)).isoformat(), "lat": -33.88, "lon": 151.22},                  # in the future
        {"time": iso(1), "lat": 95, "lon": 151.22}, {"time": iso(1), "lon": 151.22}, {"lat": -33.88, "lon": 151.22},
        "not an object", 17, None]}, now=now)
    assert len(evs) == 2
    near, far = sorted(evs, key=lambda e: e.properties["distance_km"])
    assert near.properties["distance_km"] == pytest.approx(1.4, abs=0.2) and near.properties["polarity"] == -1 and near.properties["amplitude_ka"] == -14.2
    assert far.properties["distance_km"] == pytest.approx(46, abs=3) and "polarity" not in far.properties
    assert all(e.event_type == "lightning" and e.source == "lightning_feed" and e.properties["detected_by"] == "external feed" for e in evs)
    assert len(norm(make_ctx, [{"time": iso(2), "lat": -37.8, "lon": 145.0}], now=now, max_distance_km=2000)) == 1   # distance is configurable


def test_duplicates_within_one_push_collapse_and_ids_are_stable(make_ctx):
    now = utcnow().replace(microsecond=0)
    s = {"time": now.isoformat(), "lat": -33.88, "lon": 151.22}
    a, b = norm(make_ctx, [s, dict(s), {**s, "lon": 151.23}], now=now), norm(make_ctx, [s], now=now)
    assert len(a) == 2 and a[0].external_id == b[0].external_id and a[0].external_id.startswith("lf:")


@pytest.fixture
async def feed(admin, app):
    app.state.core.registry.register(LightningFeed)
    await admin.put("/api/locations", json=[HOME])
    assert (await admin.put("/api/sources/lightning_feed", json={"enabled": True, "config": {"max_distance_km": 100}, "secrets": {"push_token": TOKEN}})).status_code == 200
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://feeder") as dev:
        yield dev, admin, app


def body(minutes_ago=2, **extra):
    return json.dumps({"strikes": [{"time": (utcnow() - timedelta(minutes=minutes_ago)).isoformat(), "lat": -33.88, "lon": 151.22, **extra}]})


async def test_push_stores_events_and_the_alert_rule_sees_them(feed):
    dev, admin, app = feed
    r = await dev.post(f"/ingest/lightning_feed/{TOKEN}", content=body(), headers={"Content-Type": "application/json"})
    assert r.status_code == 200 and r.json() == {"accepted": 1, "ignored": 0}
    (e,) = (await admin.get("/api/events?event_type=lightning")).json()
    assert e["source"] == "lightning_feed" and e["properties"]["distance_km"] < 3
    same = body()
    for _ in range(2):                                                                   # an identical push repeated (a retrying relay): one stored strike
        assert (await dev.post(f"/ingest/lightning_feed/{TOKEN}", content=same, headers={"Content-Type": "application/json"})).json()["accepted"] == 1
    assert len((await admin.get("/api/events?event_type=lightning")).json()) == 2        # the first body() plus the repeated one
    h = next(s for s in (await admin.get("/api/sources")).json() if s["meta"]["name"] == "lightning_feed")["health"]
    assert h["state"] == "ok" and h["detail"]["strikes_kept"] == 3
    # the 'lightning' alert rule reads these events (distance_km property) like any other lightning source
    rid = (await admin.post("/api/alerts/rules", json={"name": "Lightning", "cooldown_min": 0, "outputs": [], "params": {"type": "lightning", "within_km": 10, "within_min": 30}})).json()["id"]
    assert [c["key"] for c in (await admin.get("/api/alerts/preview")).json()[rid]] == ["lightning"]


async def test_auth_limits_and_errors(feed):
    dev, admin, _ = feed
    J = {"Content-Type": "application/json"}
    assert (await dev.post("/ingest/lightning_feed/wrong", content=body(), headers=J)).status_code == 403
    assert (await dev.post("/ingest/lightning_feed", content=body(), headers=J)).status_code == 403
    assert (await dev.post("/ingest/lightning_feed", content=body(), headers={**J, "X-Stormtracker-Token": TOKEN})).status_code == 200          # header token
    assert (await dev.post("/ingest/lightning_feed", content=body(), headers={**J, "Authorization": f"Bearer {TOKEN}"})).status_code == 200
    assert (await dev.get(f"/ingest/lightning_feed/{TOKEN}")).status_code == 405
    assert (await dev.post(f"/ingest/lightning_feed/{TOKEN}", content="not json", headers=J)).status_code == 400
    assert (await dev.post(f"/ingest/lightning_feed/{TOKEN}", content=b"x" * (300 * 1024), headers=J)).status_code == 413
    r = await dev.post(f"/ingest/lightning_feed/{TOKEN}", content=json.dumps({"strikes": [{"time": "junk", "lat": 0, "lon": 0}]}), headers=J)
    assert r.json() == {"accepted": 0, "ignored": 1}
    await admin.put("/api/locations", json=[])
    assert (await dev.post(f"/ingest/lightning_feed/{TOKEN}", content=body(), headers=J)).status_code == 503


async def test_unset_token_refuses_everything_and_health_waits_for_the_first_push(admin, app):
    app.state.core.registry.register(LightningFeed)
    await admin.put("/api/locations", json=[HOME])
    await admin.put("/api/sources/lightning_feed", json={"enabled": True})
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://d") as dev:
        # the token is required, so without it the source does not run at all: nothing is listening at the ingest URL
        assert (await dev.post("/ingest/lightning_feed/anything", content=body())).status_code == 404
    health = next(s for s in (await admin.get("/api/sources")).json() if s["meta"]["name"] == "lightning_feed")["health"]
    assert health["state"] == "missing_credentials"
    await admin.put("/api/sources/lightning_feed", json={"secrets": {"push_token": TOKEN}})
    h = next(s for s in (await admin.get("/api/sources")).json() if s["meta"]["name"] == "lightning_feed")["health"]
    assert h["state"] == "starting"                                                    # configured, waiting for the first push
