"""Home Assistant MQTT discovery."""
import asyncio
import json
import re
from datetime import UTC, datetime, timedelta

from stormtracker.ha_mqtt import BINARY, ENTITIES, HaSettings, discovery_messages, state_doc
from stormtracker.models import Event, Observation
from tests.alerts.test_outputs import broker

# device classes Home Assistant allows each unit for (the ones used here)
OK_UNITS = {"temperature": {"°C"}, "humidity": {"%"}, "atmospheric_pressure": {"hPa"}, "wind_speed": {"km/h"}, "precipitation_intensity": {"mm/h"},
            "precipitation": {"mm"}, "irradiance": {"W/m²"}, "duration": {"min"}, "distance": {"km"}}


def test_discovery_messages_are_well_formed():
    cfg = HaSettings(enabled=True, interval_s=60)
    msgs = discovery_messages(cfg)
    assert len(msgs) == len(ENTITIES) == len({e["key"] for e in ENTITIES})
    keys, uids = set(state_doc({})), set()
    for topic, payload, retain in msgs:
        assert retain is True and re.fullmatch(r"homeassistant/(sensor|binary_sensor)/stormtracker/[a-z0-9_]+/config", topic), topic
        c = json.loads(payload)
        assert c["unique_id"] not in uids; uids.add(c["unique_id"])
        assert re.fullmatch(r"[A-Za-z0-9_-]+", c["unique_id"])
        assert c["state_topic"] == "stormtracker/state" and c["expire_after"] == 180 and c["has_entity_name"] is True
        assert re.fullmatch(r"\{\{ value_json\.(\w+) \}\}", c["value_template"]) and re.search(r"\.(\w+) ", c["value_template"]).group(1) in keys
        assert c["device"]["identifiers"] == ["stormtracker_stormtracker"]
        dc, unit = c.get("device_class"), c.get("unit_of_measurement")
        if dc in OK_UNITS:
            assert unit in OK_UNITS[dc], (c["name"], dc, unit)
        if "binary_sensor" in topic:
            assert c["payload_on"] == "ON" and c["payload_off"] == "OFF" and "unit_of_measurement" not in c and "state_class" not in c
        if dc == "enum":
            assert c["options"] and "unit_of_measurement" not in c and "state_class" not in c        # Home Assistant rejects these combinations
    assert {e["key"] for e in ENTITIES} == keys                                                       # an entity for every key and the reverse


def test_every_key_is_always_present_and_unknowns_are_null():
    d = state_doc({})
    assert set(d) == {e["key"] for e in ENTITIES}
    assert all(d[k] == "OFF" for k in BINARY)
    assert d["temp_c"] is None and d["rain_outlook"] == "unknown" and d["forecast_storm_potential"] == "unknown" and d["warnings_count"] == 0
    json.dumps(d)                                                                                     # serialisable as is


NOW = {
    "generated_at": "2026-11-07T01:00:00+00:00",
    "conditions": [{"own_station": True, "stale": False, "values": {"temp_c": 22.5, "wind_gust_kmh": 40.0, "rain_rate_mmh": 0.0, "rain_daily_mm": 3.2, "uv_index": 6}}],
    "rain": {"radar_status": "approaching", "eta_lead_min": 20, "peak_mmh": 14.0, "nearest_rain_km": 9.5},
    "storm_outlook": {"tracked_cells": [{"relative_to_home": {"distance_km": 18.2, "approaching": True, "eta_min": 25.4}},
                                        {"relative_to_home": {"distance_km": 7.0, "approaching": False, "eta_min": None}}]},
    "lightning": {"strikes_last_60_min": 4, "nearest_km": 6.0, "within_10km_last_30_min": 2},
    "warnings": {"count": 2, "for_your_area_count": 1, "in_force": [{"severe_thunderstorm": True, "for_your_area": True}, {"severe_thunderstorm": False, "for_your_area": False}]},
    "forecast": {"summary_next_12h": {"max_gust_ms": 15.0, "max_cape_jkg": 1800, "max_precip_prob_pct": 70, "potential": {"level": "moderate"}}},
    "alerts": {"active": [{"title": "x"}]},
}


def test_state_from_a_busy_situation():
    d = state_doc(NOW)
    assert (d["temp_c"], d["wind_gust_kmh"], d["rain_today_mm"], d["uv_index"]) == (22.5, 40.0, 3.2, 6)
    assert (d["rain_outlook"], d["rain_eta_min"], d["rain_peak_mmh"], d["nearest_rain_km"]) == ("approaching", 20, 14.0, 9.5)
    assert d["storm_nearest_km"] == 7.0 and d["storm_eta_min"] == 25.4                                  # nearest cell overall; ETA only from approaching ones
    assert (d["lightning_strikes_1h"], d["lightning_nearest_km"], d["lightning_nearby"]) == (4, 6.0, "ON")
    assert (d["warnings_count"], d["warnings_area_count"], d["severe_warning_area"]) == (2, 1, "ON")
    assert (d["forecast_max_gust_kmh"], d["forecast_storm_potential"], d["forecast_max_cape_jkg"]) == (54, "moderate", 1800)
    assert (d["alerts_active"], d["alert_active"], d["rain_expected"], d["raining"]) == (1, "ON", "ON", "OFF")
    assert d["updated"] == "2026-11-07T01:00:00+00:00"


def test_raining_comes_from_radar_or_the_station_and_stale_stations_are_ignored():
    assert state_doc({"rain": {"radar_status": "raining"}})["rain_eta_min"] == 0
    assert state_doc({"conditions": [{"own_station": True, "stale": False, "values": {"rain_rate_mmh": 2.0}}]})["raining"] == "ON"
    assert state_doc({"conditions": [{"own_station": True, "stale": True, "values": {"temp_c": 30}}]})["temp_c"] is None
    assert state_doc({"conditions": [{"own_station": False, "stale": False, "values": {"temp_c": 30}}]})["temp_c"] is None      # only your own station feeds the entities


async def setup_broker(admin, port, **extra):
    r = await admin.put("/api/alerts/outputs/mqtt", json={"enabled": False, "config": {"host": "127.0.0.1", "port": port, **extra}})
    assert r.status_code == 200, r.text


async def test_publish_now_sends_discovery_then_only_state(admin, app):
    b, srv, port = await broker()
    async with srv:
        await setup_broker(admin, port)
        r = await admin.post("/api/homeassistant/publish")
        assert r.status_code == 200 and r.json()["status"]["state"] == "ok" and r.json()["broker"]["ready"]
        first = list(b.publishes)
        assert len(first) == len(ENTITIES) + 1 and all(p["qos"] == 1 for p in first)
        disc = [p for p in first if p["topic"].endswith("/config")]
        state = [p for p in first if p["topic"] == "stormtracker/state"]
        assert len(disc) == len(ENTITIES) and all(p["retain"] for p in disc)
        assert len(state) == 1 and state[0]["retain"] is False                                       # Home Assistant restores its own state
        assert set(json.loads(state[0]["payload"])) == {e["key"] for e in ENTITIES}
        assert b.connects[0]["client_id"] == "stormtracker-ha"                                       # not the alert destination's id
        b.publishes.clear()
        await admin.post("/api/homeassistant/publish")
        assert [p["topic"] for p in b.publishes] == ["stormtracker/state"]                           # discovery is not repeated every cycle


async def test_state_reflects_stored_data(admin, app):
    core = app.state.core
    now = datetime.now(UTC)
    await core.repo.save([Observation(source="ecowitt", station_id="s", lat=0, lon=0, ts=now - timedelta(minutes=1), values={"temp_c": 19.5, "wind_gust_ms": 10.0}),
                          Event(source="ecowitt", event_type="lightning", external_id="a", ts=now - timedelta(minutes=2), properties={"distance_km": 4.0})])
    b, srv, port = await broker()
    async with srv:
        await setup_broker(admin, port)
        await admin.post("/api/homeassistant/publish")
        d = json.loads(next(p for p in b.publishes if p["topic"] == "stormtracker/state")["payload"])
    assert d["temp_c"] == 19.5 and d["wind_gust_kmh"] == 36.0 and d["lightning_strikes_1h"] == 1 and d["lightning_nearby"] == "ON"


async def test_settings_change_topics_and_login_is_used(admin):
    b, srv, port = await broker(creds=("u", "p"))
    async with srv:
        await setup_broker(admin, port, username="u")
        await admin.put("/api/alerts/outputs/mqtt", json={"secrets": {"password": "p"}})
        r = await admin.put("/api/homeassistant", json={"enabled": True, "discovery_prefix": "ha", "base_topic": "home-storm", "interval_s": 30})
        assert r.status_code == 200 and r.json()["settings"]["interval_s"] == 30
        await admin.post("/api/homeassistant/publish")
    assert b.connects[0]["user"] == "u" and b.connects[0]["pw"] == "p"
    assert any(p["topic"] == "ha/sensor/home-storm/temp_c/config" for p in b.publishes) and any(p["topic"] == "home-storm/state" for p in b.publishes)
    assert json.loads(next(p for p in b.publishes if p["topic"].endswith("/temp_c/config"))["payload"])["expire_after"] == 90


async def test_without_a_broker_it_says_so(admin):
    r = await admin.post("/api/homeassistant/publish")
    assert r.status_code == 502 and "MQTT destination" in r.json()["detail"]
    j = (await admin.get("/api/homeassistant")).json()
    assert j["broker"]["ready"] is False and j["broker"]["note"]
    assert (await admin.post("/api/homeassistant/remove")).status_code == 502


async def test_unreachable_broker_is_a_clear_error(admin):
    await setup_broker(admin, 1)
    r = await admin.post("/api/homeassistant/publish")
    assert r.status_code == 502 and "couldn't publish" in r.json()["detail"]


async def test_remove_clears_every_discovery_topic(admin):
    b, srv, port = await broker()
    async with srv:
        await setup_broker(admin, port)
        r = await admin.post("/api/homeassistant/remove")
    assert r.status_code == 200 and r.json()["removed"] == len(ENTITIES)
    assert len(b.publishes) == len(ENTITIES) + 1 and all(p["payload"] == b"" and p["retain"] for p in b.publishes)
    assert sum(p["topic"].endswith("/config") for p in b.publishes) == len(ENTITIES)


async def test_the_background_loop_publishes_when_switched_on(admin, app):
    b, srv, port = await broker()
    async with srv:
        await setup_broker(admin, port)
        await admin.put("/api/homeassistant", json={"enabled": True, "interval_s": 15})
        for _ in range(40):
            if len(b.publishes) > len(ENTITIES):
                break
            await asyncio.sleep(0.1)
    assert len(b.publishes) == len(ENTITIES) + 1
    assert app.state.core.ha.status["state"] == "ok"


async def test_settings_validation_and_permissions(admin, member, agent):
    for bad in ({"interval_s": 5}, {"interval_s": 99999}, {"base_topic": "a/b"}, {"base_topic": ""}, {"discovery_prefix": "has space"}):
        assert (await admin.put("/api/homeassistant", json=bad)).status_code == 422, bad
    for c in (member, agent):
        assert (await c.get("/api/homeassistant")).status_code == 403
        assert (await c.post("/api/homeassistant/publish")).status_code == 403


async def test_a_dropped_connection_is_a_clear_502_not_a_500(admin, monkeypatch):
    from stormtracker import ha_mqtt
    b, srv, port = await broker()
    async with srv:
        await setup_broker(admin, port)
        async def dropped(*a, **k):
            raise asyncio.IncompleteReadError(b"", 2)
        monkeypatch.setattr(ha_mqtt, "publish", dropped)
        for path in ("/api/homeassistant/publish", "/api/homeassistant/remove"):
            r = await admin.post(path)
            assert r.status_code == 502 and "couldn't publish" in r.json()["detail"], path


async def test_two_publishes_never_use_the_broker_at_once(admin, app):
    """A second connection with the same client id would make a real broker drop the first."""
    b, srv, port = await broker()
    active, peak, real = 0, 0, b.handle

    async def counting(r, w):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await real(r, w)
        finally:
            active -= 1
    srv.close()
    srv = await asyncio.start_server(counting, "127.0.0.1", port)
    async with srv:
        await setup_broker(admin, port)
        ha = app.state.core.ha
        cfg = await ha.settings()
        await asyncio.gather(ha.cycle(cfg), ha.cycle(cfg), ha.remove())
    assert peak == 1 and len(b.connects) == 3


def test_entity_names_do_not_repeat_the_device_name():
    assert not [e["name"] for e in ENTITIES if e["name"].lower().startswith("stormtracker")]
    assert [e for e in ENTITIES if e["key"] == "wind_dir_deg"][0]["sc"] is None
