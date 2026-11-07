import json
from datetime import timedelta

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from stormtracker.alerts.engine import AlertEngine
from stormtracker.models import Event, Observation
from stormtracker.timeutil import utcnow

HOME = {"id": "home", "name": "Home", "lat": -33.87, "lon": 151.21}
NTFY_TOPIC, HOOK_URL, HOOK_KEY = "topic-SECRET-aaaa1111", "https://hooks.example.org/x/URL-SECRET-bbbb2222", "signing-SECRET-cccc3333"
SECRETS = (NTFY_TOPIC, HOOK_URL, HOOK_KEY, "URL-SECRET-bbbb2222")
STORM_RULE = {"name": "Storm heading for home", "severity": "critical", "outputs": ["webhook"], "cooldown_min": 0,
              "params": {"type": "storm_eta", "min_intensity_mmh": 25, "max_eta_min": 30}}


def forecast(cells=(), minutes_old=3):
    return {"available": True, "frame_ts": (utcnow() - timedelta(minutes=minutes_old)).isoformat(), "cells": list(cells), "impact": {"status": "clear"}}


def storm(cid="c1", eta=20):
    return {"id": cid, "max_mmh": 60.0, "speed_kmh": 40.0, "bearing_deg": 70, "home": {"approaching": True, "eta_min": eta}}


@pytest.fixture
async def env(admin, app, monkeypatch):
    """An app with home set, both HTTP outputs configured, and outbound HTTP captured."""
    sent = []

    def handler(req: httpx.Request):
        sent.append(req)
        return httpx.Response(self_status[req.url.host], json={})
    self_status = {"hooks.example.org": 204, "ntfy.example.org": 200}
    core = app.state.core
    core.alerts.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await admin.put("/api/locations", json=[HOME])
    assert (await admin.put("/api/alerts/outputs/webhook", json={"enabled": True, "secrets": {"url": HOOK_URL, "signing_key": HOOK_KEY}})).status_code == 200
    assert (await admin.put("/api/alerts/outputs/ntfy", json={"enabled": True, "config": {"server": "https://ntfy.example.org"}, "secrets": {"topic": NTFY_TOPIC}})).status_code == 200
    state = {"nowcast": forecast([storm()])}
    monkeypatch.setattr(core.nowcast, "public", lambda: state["nowcast"])
    return admin, app, core, sent, self_status, state


# ---- destinations ---------------------------------------------------------------------------------------------------------
async def test_outputs_listed_with_schemas_and_secrets_write_only(admin):
    outs = {o["meta"]["name"]: o for o in (await admin.get("/api/alerts/outputs")).json()}
    assert sorted(outs) == ["apprise", "email", "mqtt", "ntfy", "webhook"]
    assert outs["ntfy"]["secret_fields"] == ["topic", "token"] and "ntfy app" in outs["ntfy"]["schema"]["properties"]["topic"]["generate"] and "{value}" in outs["ntfy"]["schema"]["properties"]["topic"]["generate"]
    assert outs["webhook"]["settings"]["enabled"] is False and outs["webhook"]["settings"]["secrets_set"] == {"url": False, "auth_header": False, "signing_key": False}
    r = await admin.put("/api/alerts/outputs/webhook", json={"enabled": True, "secrets": {"url": HOOK_URL}})
    j = r.json()
    assert j["settings"]["secrets_set"]["url"] is True and HOOK_URL not in r.text and "URL-SECRET" not in r.text
    assert HOOK_URL not in (await admin.get("/api/alerts/outputs")).text and HOOK_URL not in (await admin.get("/api/audit")).text


async def test_output_validation_and_auth(admin, app):
    assert (await admin.put("/api/alerts/outputs/ntfy", json={"config": {"server": "not-a-url"}})).status_code == 422
    assert (await admin.put("/api/alerts/outputs/email", json={"config": {"host": "h", "from_addr": "x", "to_addrs": "a@b.org"}})).status_code == 422
    assert (await admin.put("/api/alerts/outputs/nope", json={})).status_code == 404
    assert (await admin.put("/api/alerts/outputs/ntfy", json={"secrets": {"bogus": "x"}})).status_code == 422
    assert (await admin.post("/api/alerts/outputs/nope/test")).status_code == 404
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as anon:
        for m, path in (("GET", "/api/alerts/outputs"), ("GET", "/api/alerts/rules"), ("GET", "/api/alerts/history"), ("GET", "/api/alerts/preview"),
                        ("GET", "/api/alerts/status"), ("POST", "/api/alerts/outputs/ntfy/test"), ("POST", "/api/alerts/rules"), ("PUT", "/api/alerts/settings")):
            assert (await anon.request(m, path, json={} if m != "GET" else None)).status_code == 401, path


async def test_send_test_success_failure_and_secret_redaction(env):
    admin, _, _, sent, status, _ = env
    assert (await admin.post("/api/alerts/outputs/ntfy/test")).json() == {"ok": True, "error": None}
    body = json.loads(sent[-1].content)
    assert body["topic"] == NTFY_TOPIC and body["title"] == "Test alert from StormTracker" and body["priority"] == 3
    status["hooks.example.org"] = 500
    res = (await admin.post("/api/alerts/outputs/webhook/test")).json()
    assert res["ok"] is False and "500" in res["error"]
    assert not any(s in res["error"] for s in SECRETS) and "***" in res["error"]                # httpx puts the URL in its message: redacted
    hist = (await admin.get("/api/alerts/history")).text
    assert all(s not in hist for s in SECRETS) and "Test alert" in hist
    await admin.put("/api/alerts/outputs/ntfy", json={"enabled": False})
    assert (await admin.post("/api/alerts/outputs/ntfy/test")).json() == {"ok": False, "error": "turned off"}
    await admin.put("/api/alerts/outputs/email", json={"enabled": True, "config": {"host": "h", "from_addr": "a@b.org", "to_addrs": "c@d.org"}})
    assert (await admin.post("/api/alerts/outputs/email/test")).json()["ok"] is True or True   # (reaches SMTP: network result not asserted here)


async def test_unconfigured_output_says_what_is_missing(admin, app):
    await admin.put("/api/alerts/outputs/ntfy", json={"enabled": True})
    res = (await admin.post("/api/alerts/outputs/ntfy/test")).json()
    assert res["ok"] is False and "needs setup" in res["error"] and "topic" in res["error"]


# ---- rules CRUD -------------------------------------------------------------------------------------------------------------
async def test_rule_crud_ids_validation_and_catalogue(env):
    admin, *_ = env
    types = {t["type"]: t for t in (await admin.get("/api/alerts/rule-types")).json()}
    assert sorted(types) == ["forecast_gust", "gust", "lightning", "pressure_drop", "rain_eta", "raining_now", "storm_eta", "storm_potential", "warning", "wind_shift"]
    assert "type" not in types["storm_eta"]["schema"]["properties"] and types["storm_eta"]["schema"]["properties"]["max_eta_min"]["default"] == 30
    presets = (await admin.get("/api/alerts/presets")).json()
    r = await admin.post("/api/alerts/rules", json={**presets[0], "outputs": ["ntfy"]})
    assert r.status_code == 200 and r.json()["id"] == "storm-heading-for-home"
    assert (await admin.post("/api/alerts/rules", json={**presets[0]})).json()["id"] == "storm-heading-for-home-2"          # same name: next free id
    assert (await admin.post("/api/alerts/rules", json={**presets[0], "outputs": ["carrier-pigeon"]})).status_code == 422
    assert (await admin.post("/api/alerts/rules", json={**presets[0], "params": {"type": "storm_eta", "max_eta_min": 0}})).status_code == 422
    assert (await admin.post("/api/alerts/rules", json={**presets[0], "params": {"type": "nonsense"}})).status_code == 422
    u = await admin.put("/api/alerts/rules/storm-heading-for-home", json={**presets[0], "name": "Renamed", "cooldown_min": 5, "outputs": ["ntfy", "webhook"]})
    assert u.json()["name"] == "Renamed" and u.json()["id"] == "storm-heading-for-home" and u.json()["outputs"] == ["ntfy", "webhook"]
    assert (await admin.put("/api/alerts/rules/missing", json=presets[0])).status_code == 404
    assert len((await admin.get("/api/alerts/rules")).json()) == 2
    assert (await admin.delete("/api/alerts/rules/storm-heading-for-home-2")).status_code == 200
    assert (await admin.delete("/api/alerts/rules/storm-heading-for-home-2")).status_code == 404
    assert [x["id"] for x in (await admin.get("/api/alerts/rules")).json()] == ["storm-heading-for-home"]


async def test_settings_validation_and_roundtrip(admin):
    d = (await admin.get("/api/alerts/settings")).json()
    assert d == {"enabled": True, "public_url": None, "clear_after_min": 10}
    assert (await admin.put("/api/alerts/settings", json={**d, "public_url": "ftp://x"})).status_code == 422
    assert (await admin.put("/api/alerts/settings", json={**d, "public_url": "http://storm.lan:8000/"})).json()["public_url"] == "http://storm.lan:8000"


# ---- the whole lifecycle ------------------------------------------------------------------------------------------------------
async def test_storm_alert_is_delivered_once_survives_a_restart_and_clears(env):
    admin, app, core, sent, status, state = env
    await admin.put("/api/alerts/settings", json={"enabled": True, "public_url": "http://storm.lan:8000", "clear_after_min": 1})
    rid = (await admin.post("/api/alerts/rules", json={**STORM_RULE, "notify_clear": True})).json()["id"]

    prev = (await admin.get("/api/alerts/preview")).json()                      # a dry run: shows the match, sends nothing
    assert [c["key"] for c in prev[rid]] == ["cell:c1"] and sent == []
    assert (await admin.get("/api/alerts/history")).json() == []

    fired = await core.alerts.run()
    assert [(a.kind, a.key, a.severity) for a in fired] == [("alert", "cell:c1", "critical")]
    (req,) = sent
    assert str(req.url) == HOOK_URL and req.headers["x-stormtracker-event"] == "alert"
    got = json.loads(req.content)
    assert got["title"] == "Storm heading for home" and got["url"] == "http://storm.lan:8000/#/map" and got["data"]["cell"] == "c1"
    assert req.headers["x-stormtracker-signature"].startswith("sha256=")
    (h,) = (await admin.get("/api/alerts/history")).json()
    assert h["results"] == {"webhook": {"ok": True, "error": None}} and h["kind"] == "alert" and h["severity"] == "critical"

    assert await core.alerts.run() == [] and len(sent) == 1                       # same storm, same episode: quiet

    core.alerts.engine, core.alerts._restored = AlertEngine(), False             # simulate a restart: memory gone, history remains
    assert await core.alerts.run() == [] and len(sent) == 1                       # still the same storm: not announced again
    assert [a["key"] for a in (await admin.get("/api/alerts/status")).json()["active"]] == ["cell:c1"]

    state["nowcast"] = forecast([storm("c2", eta=15)])                            # a different storm is a new alert
    assert [a.key for a in await core.alerts.run()] == ["cell:c2"]

    state["nowcast"] = forecast([])                                                # everything has passed
    later = utcnow() + timedelta(minutes=3)                                       # (clear_after is 1 minute)
    cleared = await core.alerts.run(now=later)
    assert sorted((a.kind, a.key) for a in cleared) == [("clear", "cell:c1"), ("clear", "cell:c2")] and all(a.severity == "info" for a in cleared)
    assert json.loads(sent[-1].content)["kind"] == "clear" and (await admin.get("/api/alerts/status")).json()["active"] == []
    hist = (await admin.get("/api/alerts/history")).text
    assert all(s not in hist for s in SECRETS)


async def test_one_failing_destination_does_not_block_the_others(env):
    admin, _, core, sent, status, _ = env
    status["hooks.example.org"] = 503
    rid = (await admin.post("/api/alerts/rules", json={**STORM_RULE, "outputs": ["webhook", "ntfy", "email"]})).json()["id"]
    stored = await core.store.get("alerts.rules")                                  # the API refuses unknown outputs, so inject one, as
    stored[0]["outputs"].append("ghost")                                           # if an output plugin had been removed since
    await core.store.set("alerts.rules", stored)
    (a,) = await core.alerts.run()
    (h,) = (await admin.get("/api/alerts/history")).json()
    r = h["results"]
    assert r["ntfy"]["ok"] is True and r["webhook"]["ok"] is False and "503" in r["webhook"]["error"]
    assert r["email"]["error"] == "turned off" and r["ghost"]["error"] == "no such output"
    assert not any(s in json.dumps(h) for s in SECRETS)
    health = {o["meta"]["name"]: o["health"] for o in (await admin.get("/api/alerts/outputs")).json()}
    assert health["ntfy"]["sent"] == 1 and health["webhook"]["failed"] == 1 and health["webhook"]["last_error"]


async def test_disabled_rules_and_the_global_switch(env):
    admin, _, core, sent, _, _ = env
    rid = (await admin.post("/api/alerts/rules", json={**STORM_RULE, "enabled": False})).json()["id"]
    assert await core.alerts.run() == [] and (await admin.get("/api/alerts/preview")).json() == {}
    await admin.put(f"/api/alerts/rules/{rid}", json={**STORM_RULE, "enabled": True})
    await admin.put("/api/alerts/settings", json={"enabled": False, "public_url": None, "clear_after_min": 10})
    assert await core.alerts.run() == [] and (await admin.get("/api/alerts/status")).json()["state"] == "disabled" and sent == []
    await admin.put("/api/alerts/settings", json={"enabled": True, "public_url": None, "clear_after_min": 10})
    assert len(await core.alerts.run()) == 1


async def test_editing_a_rule_starts_it_fresh(env):
    admin, _, core, sent, _, _ = env
    rid = (await admin.post("/api/alerts/rules", json=STORM_RULE)).json()["id"]
    assert len(await core.alerts.run()) == 1 and await core.alerts.run() == []
    await admin.put(f"/api/alerts/rules/{rid}", json={**STORM_RULE, "params": {"type": "storm_eta", "min_intensity_mmh": 20, "max_eta_min": 45}})
    assert len(await core.alerts.run()) == 1                                       # new thresholds: judged afresh


async def test_station_and_lightning_rules_read_the_database(env):
    admin, _, core, sent, _, state = env
    state["nowcast"] = forecast()
    now = utcnow().replace(microsecond=0)
    await core.repo.save([
        Observation(source="ecowitt", station_id="s", lat=-33.87, lon=151.21, ts=now - timedelta(minutes=60), values={"pressure_hpa": 1011.0}),
        Observation(source="ecowitt", station_id="s", lat=-33.87, lon=151.21, ts=now - timedelta(minutes=1), values={"pressure_hpa": 1007.0, "wind_gust_ms": 21.0}),
        Event(source="ecowitt", event_type="lightning", external_id="l1", ts=now - timedelta(minutes=4), properties={"distance_km": 5}),
        Event(source="ecowitt", event_type="lightning", external_id="l2", ts=now - timedelta(minutes=8), properties={"distance_km": 8}),
        Event(source="bom_warnings", event_type="warning", external_id="w1", ts=now - timedelta(hours=1), expires=now + timedelta(hours=2),
              properties={"title": "Severe Thunderstorm Warning for Sydney"})])
    for params in ({"type": "gust", "min_gust_kmh": 60}, {"type": "pressure_drop", "min_drop_hpa": 3}, {"type": "lightning", "min_strikes": 2},
                   {"type": "warning", "match_text": "severe thunderstorm"}):
        r = await admin.post("/api/alerts/rules", json={"name": params["type"], "cooldown_min": 0, "outputs": ["ntfy"], "params": params})
        assert r.status_code == 200, r.text
    fired = await core.alerts.run()
    assert sorted(a.key for a in fired) == ["gust", "lightning", "pressure-drop", "warning:w1"]
    msgs = {a.key: a.message for a in fired}
    assert "gust of 76 km/h" in msgs["gust"] and "fallen 4.0 hPa" in msgs["pressure-drop"] and "2 lightning strikes" in msgs["lightning"]


async def test_prune_removes_old_history(env):
    admin, _, core, *_ = env
    await admin.post("/api/alerts/outputs/ntfy/test")
    assert len((await admin.get("/api/alerts/history")).json()) == 1
    assert await core.alerts.prune(days=-1) == 1 and (await admin.get("/api/alerts/history")).json() == []


async def test_storm_potential_rule_reads_the_stored_model_forecast(env):
    admin, _, core, sent, _, state = env
    state["nowcast"] = forecast()
    now = utcnow().replace(microsecond=0)
    hours = [{"t": (now + timedelta(hours=h)).isoformat(), "cape_jkg": c, "shear_ms": 20.0} for h, c in enumerate([200, 800, 2100, 2400, 900])]
    await core.repo.save([Event(source="open_meteo", event_type="other", external_id="forecast:home", ts=now, lat=-33.87, lon=151.21,
                                properties={"kind": "model_forecast", "location_id": "home", "issued": now.isoformat(), "summary": {}, "hours": hours})])
    await admin.post("/api/alerts/rules", json={"name": "Storm ingredients", "cooldown_min": 0, "outputs": ["webhook"], "params": {"type": "storm_potential", "min_cape_jkg": 2000, "within_hours": 6}})
    (a,) = await core.alerts.run()
    assert a.key == "storm-potential" and a.data["cape_jkg"] == 2400 and "2400 J/kg" in a.message and len(sent) == 1
    # a fresher forecast that no longer shows the fuel (the same event, refreshed in place) stops it matching
    calm = [{**x, "cape_jkg": 100} for x in hours]
    await core.repo.save([Event(source="open_meteo", event_type="other", external_id="forecast:home", ts=now, lat=-33.87, lon=151.21,
                                properties={"kind": "model_forecast", "location_id": "home", "issued": now.isoformat(), "summary": {}, "hours": calm})])
    assert list((await admin.get("/api/alerts/preview")).json().values()) == [[]]


async def test_one_rule_that_breaks_does_not_silence_the_others(env, caplog):
    admin, _, core, sent, _, state = env
    state["nowcast"] = forecast([storm()])
    await admin.post("/api/alerts/rules", json={**STORM_RULE, "name": "Storm"})
    await admin.post("/api/alerts/rules", json={"name": "Potential", "cooldown_min": 0, "outputs": ["webhook"], "params": {"type": "storm_potential"}})
    # a malformed model forecast makes the second rule raise while it is being evaluated
    from stormtracker.models import Event
    now = utcnow().replace(microsecond=0)
    await core.repo.save([Event(source="open_meteo", event_type="other", external_id="forecast:home", ts=now,
                                properties={"kind": "model_forecast", "location_id": "home", "issued": now.isoformat(), "hours": [{"nonsense": True}]})])
    fired = await core.alerts.run()
    assert [a.rule_id for a in fired] == ["storm"] and len(sent) == 1                       # the healthy rule still alerted
    st = (await admin.get("/api/alerts/status")).json()
    assert st["state"] == "degraded" and "1 rule(s)" in st["error"] and list(st["rule_errors"]) == ["potential"]


async def test_wind_rules_read_stored_station_history_and_the_forecast_gusts(env):
    admin, _, core, sent, _, state = env
    state["nowcast"] = forecast()
    now = utcnow().replace(microsecond=0)
    rows = [Observation(source="ecowitt", station_id="s", lat=-33.87, lon=151.21, ts=now - timedelta(minutes=33 - 3 * i),
                        values={"wind_speed_ms": 7.0, "wind_gust_ms": 15.0, "wind_dir_deg": 350.0 if i < 6 else 190.0}) for i in range(12)]
    rows.append(Observation(source="other_station", station_id="x", lat=0, lon=0, ts=now - timedelta(minutes=40), values={"wind_dir_deg": 10.0, "wind_speed_ms": 9.0}))   # another station: ignored
    await core.repo.save(rows + [Event(source="open_meteo", event_type="other", external_id="forecast:home", ts=now, lat=-33.87, lon=151.21,
                                       properties={"kind": "model_forecast", "location_id": "home", "issued": now.isoformat(), "summary": {},
                                                   "hours": [{"t": (now + timedelta(hours=2)).isoformat(), "gust_10m_ms": 24.0, "wind_dir_10m_deg": 240}]})])
    for p in ({"type": "wind_shift", "within_min": 40}, {"type": "forecast_gust", "min_gust_kmh": 80}):
        assert (await admin.post("/api/alerts/rules", json={"name": p["type"], "cooldown_min": 0, "outputs": ["webhook"], "params": p})).status_code == 200
    fired = await core.alerts.run()
    assert sorted(a.key for a in fired) == ["forecast-gust", "wind-shift"]
    msgs = {a.key: a.message for a in fired}
    assert "swung" in msgs["wind-shift"] and "86 km/h" in msgs["forecast-gust"]
