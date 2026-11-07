from datetime import UTC, datetime, timedelta

from stormtracker.models import Event, Observation


def obs(src, sid, minutes_ago, temp, now):
    return Observation(source=src, station_id=sid, lat=-33.0, lon=151.0, ts=now - timedelta(minutes=minutes_ago),
                       values={"temp_c": temp})


async def test_observations_window_order_and_validation(admin, app):
    now = datetime.now(UTC).replace(microsecond=0)
    await app.state.core.repo.save([obs("ecowitt", "s1", m, 20 + m / 10, now) for m in (0, 30, 60, 90, 120)]
                                   + [obs("wu", "w1", 5, 1.0, now)])
    iso = lambda m: (now - timedelta(minutes=m)).isoformat()   # noqa: E731
    got = (await admin.get("/api/observations", params={"source": "ecowitt", "since": iso(70), "order": "asc"})).json()
    assert [o["values"]["temp_c"] for o in got] == [26.0, 23.0, 20.0]            # 60, 30, 0 min ago, oldest first
    got = (await admin.get("/api/observations", params={"source": "ecowitt", "since": iso(100), "until": iso(45)})).json()
    assert [o["values"]["temp_c"] for o in got] == [26.0, 29.0]                    # default order: newest first
    z = (await admin.get("/api/observations", params={"since": iso(10).replace("+00:00", "Z"), "source": "wu"})).json()
    assert len(z) == 1                                                             # 'Z' suffix accepted
    assert (await admin.get("/api/observations?since=yesterday")).status_code == 422
    assert len((await admin.get("/api/observations?limit=2&source=ecowitt")).json()) == 2


async def test_stations_lists_each_reporter_with_newest_first(admin, app):
    now = datetime.now(UTC).replace(microsecond=0)
    await app.state.core.repo.save([obs("ecowitt", "s1", 0, 20, now), obs("ecowitt", "s1", 30, 19, now),
                                    obs("wu", "w1", 5, 18, now)])
    st = (await admin.get("/api/stations")).json()
    assert [(s["source"], s["station_id"], s["count"]) for s in st] == [("ecowitt", "s1", 2), ("wu", "w1", 1)]
    assert st[0]["lat"] == -33.0 and st[0]["last_ts"] == now.isoformat()


async def test_events_since(admin, app):
    now = datetime.now(UTC).replace(microsecond=0)
    await app.state.core.repo.save([
        Event(source="ecowitt", event_type="lightning", external_id="a", ts=now - timedelta(hours=3)),
        Event(source="ecowitt", event_type="lightning", external_id="b", ts=now - timedelta(minutes=5))])
    got = (await admin.get("/api/events", params={"since": (now - timedelta(hours=1)).isoformat()})).json()
    assert [e["external_id"] for e in got] == ["b"]


async def test_ui_settings_defaults_roundtrip_and_validation(admin):
    d = (await admin.get("/api/ui")).json()
    assert "{z}/{x}/{y}" in d["tile_url"] and "OpenStreetMap" in d["tile_attribution"] and d["radar_opacity"] == 0.8
    r = await admin.put("/api/ui", json={"tile_url": "", "tile_attribution": "", "radar_opacity": 0.5})
    assert r.status_code == 200
    assert (await admin.get("/api/ui")).json()["tile_url"] == ""                   # empty = offline mode, persisted
    assert (await admin.put("/api/ui", json={"radar_opacity": 5})).status_code == 422


async def test_observations_limit_covers_what_the_station_page_asks_for(admin, app):
    """station.js requests limit=20000 (3 days at a 16 s interval is ~16k rows); the cap must not silently cut that."""
    now = datetime.now(UTC).replace(microsecond=0)
    await app.state.core.repo.save([obs("ecowitt", "s1", m, 20.0, now) for m in range(0, 5600)])
    got = (await admin.get("/api/observations", params={"source": "ecowitt", "limit": 20000, "order": "asc"})).json()
    assert len(got) == 5600 and got[0]["ts"] < got[-1]["ts"]


async def test_events_are_refreshed_in_place_but_observations_stay_immutable(admin, app):
    from datetime import timedelta
    now = datetime.now(UTC).replace(microsecond=0)
    repo = app.state.core.repo
    await repo.save([Event(source="w", event_type="warning", external_id="w1", ts=now, expires=now + timedelta(minutes=20), properties={"title": "Severe Thunderstorm Warning", "v": 1})])
    await repo.save([Event(source="w", event_type="warning", external_id="w1", ts=now, expires=now + timedelta(minutes=40), properties={"title": "Severe Thunderstorm Warning", "v": 2})])
    (e,) = (await admin.get("/api/events?event_type=warning")).json()
    assert e["properties"]["v"] == 2 and e["expires"] == (now + timedelta(minutes=40)).isoformat()           # same row, updated
    await repo.save([obs("s", "x", 0, 10.0, now)])
    await repo.save([obs("s", "x", 0, 99.0, now)])
    (o,) = (await admin.get("/api/observations?source=s")).json()
    assert o["values"]["temp_c"] == 10.0                                                                      # first reading wins
