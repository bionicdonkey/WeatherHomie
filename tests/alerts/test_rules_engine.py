from datetime import UTC, datetime, timedelta

import pytest

from stormtracker.alerts.engine import AlertEngine
from stormtracker.alerts.rules import PRESETS, Rule, Snapshot, evaluate, haversine_km

NOW = datetime(2026, 11, 6, 10, 30, tzinfo=UTC)
HOME = {"name": "Home", "lat": -33.87, "lon": 151.21}


def rule(params, **kw):
    return Rule(id=kw.pop("id", "r1"), name=kw.pop("name", "A rule"), params=params, **kw)


def nowcast(cells=(), impact=None, frame_age_min=3):
    return {"available": True, "frame_ts": (NOW - timedelta(minutes=frame_age_min)).isoformat(), "cells": list(cells), "impact": impact or {"status": "clear"}}


def cell(cid="0905-1", mmh=60.0, eta=20, approaching=True, speed=40.0, bearing=70):
    return {"id": cid, "max_mmh": mmh, "speed_kmh": speed, "bearing_deg": bearing, "home": {"approaching": approaching, "eta_min": eta}}


def snap(**kw):
    return Snapshot(now=NOW, home=kw.pop("home", HOME), **kw)


# ---- storm_eta ------------------------------------------------------------------------------------------------------
def test_storm_eta_fires_for_a_heavy_cell_arriving_soon():
    r = rule({"type": "storm_eta", "min_intensity_mmh": 25, "max_eta_min": 30})
    (c,) = evaluate(r, snap(nowcast=nowcast([cell(eta=20)])))
    assert c.key == "cell:0905-1" and c.title == "Storm heading for home"
    # the cell's eta is counted from the radar frame time (3 min ago), so ~17 min remain from now
    assert c.data["eta_min"] == 17 and "60 mm/h" in c.message and "east-north-east" in c.message and "40 km/h" in c.message
    assert "in about 17 min" in c.message and "(about 9:47 pm)" in c.message and "Radar at 9:27 pm" in c.message   # 10:47 UTC = 9:47 pm Sydney (AEDT, UTC+11)


@pytest.mark.parametrize("cells,why", [
    ([cell(mmh=20)], "too weak"), ([cell(eta=60)], "too far off"), ([cell(approaching=False)], "not heading here"),
    ([cell(eta=None)], "no eta"), ([cell(eta=-20)], "already passed")])
def test_storm_eta_ignores(cells, why):
    r = rule({"type": "storm_eta", "min_intensity_mmh": 25, "max_eta_min": 30})
    assert evaluate(r, snap(nowcast=nowcast(cells))) == [], why


def test_stale_radar_or_no_forecast_never_alerts():
    r = rule({"type": "storm_eta"})
    assert evaluate(r, snap(nowcast=nowcast([cell()], frame_age_min=40))) == []             # radar is 40 min old
    assert evaluate(r, snap(nowcast={"available": False})) == []
    assert evaluate(r, snap(nowcast=None)) == []
    assert evaluate(r, snap(nowcast=nowcast([cell()]), home=None)) == []


def test_two_cells_make_two_conditions_with_their_own_keys():
    r = rule({"type": "storm_eta"})
    cs = evaluate(r, snap(nowcast=nowcast([cell("a", eta=15), cell("b", eta=25)])))
    assert sorted(c.key for c in cs) == ["cell:a", "cell:b"]


# ---- rain_eta / raining_now -----------------------------------------------------------------------------------------
def impact(status="approaching", eta=15, peak=18.0, conf="medium", now_mmh=0.0):
    return {"status": status, "eta_ts": (NOW + timedelta(minutes=eta)).isoformat(), "peak_mmh": peak, "now_mmh": now_mmh, "confidence": {"level": conf}}


def test_rain_eta_respects_peak_time_and_confidence():
    r = rule({"type": "rain_eta", "min_peak_mmh": 5, "max_eta_min": 30, "min_confidence": "medium"})
    assert len(evaluate(r, snap(nowcast=nowcast(impact=impact())))) == 1
    assert evaluate(r, snap(nowcast=nowcast(impact=impact(conf="low")))) == []              # not sure enough
    assert evaluate(r, snap(nowcast=nowcast(impact=impact(peak=2.0)))) == []               # too light
    assert evaluate(r, snap(nowcast=nowcast(impact=impact(eta=50)))) == []                 # too far off
    assert evaluate(r, snap(nowcast=nowcast(impact=impact(status="clear")))) == []


def test_raining_now_uses_the_higher_of_radar_and_station():
    r = rule({"type": "raining_now", "min_rate_mmh": 5})
    radar = nowcast(impact={"status": "raining", "now_mmh": 12.0})
    (c,) = evaluate(r, snap(nowcast=radar))
    assert c.key == "raining" and c.data["source"] == "radar" and "12 mm/h" in c.message
    (c,) = evaluate(r, snap(obs={"rain_rate_mmh": 20.0}, obs_ts=NOW - timedelta(minutes=1), nowcast=radar))
    assert c.data["source"] == "your station"
    assert evaluate(r, snap(obs={"rain_rate_mmh": 20.0}, obs_ts=NOW - timedelta(minutes=40))) == []    # station reading is stale
    assert evaluate(r, snap(obs={"rain_rate_mmh": 1.0}, obs_ts=NOW)) == []


# ---- lightning ------------------------------------------------------------------------------------------------------
def test_lightning_by_station_distance_or_by_coordinates():
    r = rule({"type": "lightning", "within_km": 10, "within_min": 30, "min_strikes": 2})
    s = snap(lightning=[{"ts": NOW - timedelta(minutes=5), "distance_km": 6, "lat": None, "lon": None},
                        {"ts": NOW - timedelta(minutes=9), "distance_km": None, "lat": -33.88, "lon": 151.22},     # ~1.4 km by coordinates
                        {"ts": NOW - timedelta(minutes=2), "distance_km": 25, "lat": None, "lon": None},             # too far
                        {"ts": NOW - timedelta(minutes=50), "distance_km": 2, "lat": None, "lon": None}])            # too old
    (c,) = evaluate(r, s)
    assert c.data["strikes"] == 2 and c.data["closest_km"] == pytest.approx(1.4, abs=0.2) and "closest was 1 km" in c.message
    assert evaluate(rule({"type": "lightning", "min_strikes": 3}), s) == []
    assert haversine_km(-33.87, 151.21, -33.87, 152.21) == pytest.approx(92.5, abs=0.5)


# ---- warnings, gusts, pressure -----------------------------------------------------------------------------------------
def test_warning_matches_text_and_expiry():
    r = rule({"type": "warning", "match_text": "severe thunderstorm"})
    w = lambda i, text, exp=None: {"external_id": i, "ts": NOW - timedelta(hours=1), "expires": exp, "properties": {"title": text}}   # noqa: E731
    got = evaluate(r, snap(warnings=[w("a", "Severe Thunderstorm Warning for Sydney"), w("b", "Flood Watch"),
                                    w("c", "Severe thunderstorm warning", exp=NOW - timedelta(minutes=1)), w("d", "SEVERE THUNDERSTORM WARNING", exp=NOW + timedelta(hours=1))]))
    assert sorted(c.key for c in got) == ["warning:a", "warning:d"]


def test_gust_and_pressure_use_fresh_station_data():
    g = rule({"type": "gust", "min_gust_kmh": 60})
    (c,) = evaluate(g, snap(obs={"wind_gust_ms": 20.0}, obs_ts=NOW))
    assert c.data["gust_kmh"] == 72
    assert evaluate(g, snap(obs={"wind_gust_ms": 10.0}, obs_ts=NOW)) == [] and evaluate(g, snap(obs={"wind_gust_ms": 20.0}, obs_ts=NOW - timedelta(hours=1))) == []
    p = rule({"type": "pressure_drop", "min_drop_hpa": 3})
    (c,) = evaluate(p, snap(obs={"pressure_hpa": 1006.0}, obs_ts=NOW, pressure_1h_ago=1010.0))
    assert c.data["drop_hpa"] == 4.0
    assert evaluate(p, snap(obs={"pressure_hpa": 1008.5}, obs_ts=NOW, pressure_1h_ago=1010.0)) == []
    assert evaluate(p, snap(obs={"pressure_hpa": 1006.0}, obs_ts=NOW, pressure_1h_ago=None)) == []


def test_rule_validation_and_presets():
    for bad in ({"type": "storm_eta", "min_intensity_mmh": 0}, {"type": "nope"}, {"type": "lightning", "within_km": 9999}):
        with pytest.raises(ValueError):
            rule(bad)
    with pytest.raises(ValueError):
        Rule(id="Bad Id", name="x", params={"type": "gust"})
    assert rule({"type": "gust"}, outputs=["ntfy", "ntfy", "mqtt"]).outputs == ["ntfy", "mqtt"]
    for i, pr in enumerate(PRESETS):
        Rule(id=f"p{i}", **pr)                                                           # every shipped preset is a valid rule


# ---- the state machine ---------------------------------------------------------------------------------------------------
class C:   # minimal stand-in for a Condition
    def __init__(self, key, title="T", message="m"):
        self.key, self.title, self.message, self.data, self.ended_title = key, title, message, {}, "T is over"


def minutes(n):
    return NOW + timedelta(minutes=n)


def test_an_episode_alerts_once_then_stays_quiet():
    e, r = AlertEngine(), rule({"type": "gust"}, cooldown_min=0)
    assert [f.kind for f in e.step(minutes(0), r, [C("a")])] == ["alert"]
    for m in (1, 2, 30, 300):
        assert e.step(minutes(m), r, [C("a")]) == []


def test_cooldown_spaces_out_different_keys_and_the_second_fires_later_if_still_true():
    e, r = AlertEngine(), rule({"type": "gust"}, cooldown_min=30)
    fired = e.step(minutes(0), r, [C("a"), C("b")])
    assert [f.condition_key for f in fired] == ["a"]                                 # one now...
    assert e.step(minutes(10), r, [C("a"), C("b")]) == []                            # ...b waits out the cooldown
    assert [f.condition_key for f in e.step(minutes(31), r, [C("a"), C("b")])] == ["b"]
    e2 = AlertEngine()
    e2.step(minutes(0), r, [C("a"), C("b")])
    assert e2.step(minutes(40), r, [C("a")]) == []                                   # b went away while waiting: never sent


def test_reminders_only_when_asked_for():
    e, r = AlertEngine(), rule({"type": "gust"}, cooldown_min=0, repeat_after_min=20)
    e.step(minutes(0), r, [C("a")])
    assert e.step(minutes(19), r, [C("a")]) == []
    (f,) = e.step(minutes(21), r, [C("a")])
    assert f.kind == "alert" and f.message.startswith("Still going.")
    assert e.step(minutes(30), r, [C("a")]) == []                                    # the clock restarted at the reminder


def test_the_all_clear_waits_for_the_condition_to_stay_gone_and_ignores_flapping():
    e, r = AlertEngine(clear_after_min=10), rule({"type": "gust"}, cooldown_min=0, notify_clear=True)
    e.step(minutes(0), r, [C("a")])
    assert e.step(minutes(5), r, []) == []                                           # gone for 5 min: not over yet
    assert e.step(minutes(8), r, [C("a")]) == []                                     # flickered back: same episode, no new alert
    assert e.step(minutes(12), r, []) == []
    (f,) = e.step(minutes(19), r, [])                                                # gone 11 min since last seen
    assert f.kind == "clear" and f.title == "T is over"
    (f,) = e.step(minutes(25), r, [C("a")])                                           # a genuinely new episode alerts again
    assert f.kind == "alert"
    quiet, r2 = AlertEngine(clear_after_min=10), rule({"type": "gust"}, cooldown_min=0, notify_clear=False)
    quiet.step(minutes(0), r2, [C("a")])
    assert quiet.step(minutes(30), r2, []) == [] and quiet.episodes == {}            # ends silently


def test_restore_after_restart_prevents_a_second_announcement():
    e, r = AlertEngine(), rule({"type": "gust"}, cooldown_min=30)
    e.restore("r1", "a", minutes(-5), minutes(0))
    assert e.step(minutes(0), r, [C("a")]) == []                                     # ongoing: already announced before the restart
    assert e.step(minutes(1), r, [C("b")]) == []                                     # and the rule is still cooling down
    assert [f.condition_key for f in e.step(minutes(26), r, [C("b")])] == ["b"]


def test_forget_resets_a_rule_only():
    e, r1, r2 = AlertEngine(), rule({"type": "gust"}, id="r1", cooldown_min=60), rule({"type": "gust"}, id="r2", cooldown_min=60)
    e.step(minutes(0), r1, [C("a")]); e.step(minutes(0), r2, [C("a")])
    e.forget("r1")
    assert len(e.step(minutes(1), r1, [C("a")])) == 1 and e.step(minutes(1), r2, [C("a")]) == []


# ---- storm_potential ----------------------------------------------------------------------------------------------------
def forecast_hours(cape_by_hour, shear=21.0, issued_ago_min=20):
    return {"issued": (NOW - timedelta(minutes=issued_ago_min)).isoformat(),
            "hours": [{"t": (NOW + timedelta(hours=h)).isoformat(), "cape_jkg": c, "shear_ms": shear} for h, c in enumerate(cape_by_hour)]}


def test_storm_potential_looks_ahead_only_as_far_as_asked_and_ignores_stale_forecasts():
    r = rule({"type": "storm_potential", "min_cape_jkg": 1500, "within_hours": 4})
    (c,) = evaluate(r, snap(model_forecast=forecast_hours([100, 400, 900, 2200, 2600, 3000])))          # 2600 at +4 h counts; 3000 at +5 h is out of range
    assert c.key == "storm-potential" and c.data["cape_jkg"] == 2600 and "2600 J/kg" in c.message and "strong wind shear" in c.message and "not a forecast of a storm" in c.message
    assert evaluate(r, snap(model_forecast=forecast_hours([100, 400, 900, 1000, 1400, 3000]))) == []   # the big number is beyond the window
    assert evaluate(r, snap(model_forecast=forecast_hours([3000], issued_ago_min=300))) == []           # forecast older than 3 h
    assert evaluate(r, snap(model_forecast=None)) == []
    organised = rule({"type": "storm_potential", "min_cape_jkg": 1500, "organised_only": True})
    assert evaluate(organised, snap(model_forecast=forecast_hours([2000], shear=8.0))) == []
    assert len(evaluate(organised, snap(model_forecast=forecast_hours([2000], shear=18.0)))) == 1


def test_times_in_messages_carry_the_weekday_only_when_it_is_not_today():
    from stormtracker.alerts.rules import local
    t = datetime(2026, 11, 6, 23, 0, tzinfo=UTC)                         # Sat 7 Nov, 10:00 am Sydney
    assert local(t) == "10:00 am"
    assert local(t, ref=datetime(2026, 11, 7, 0, 30, tzinfo=UTC)) == "10:00 am"       # same Sydney day (11:30 am Saturday)
    assert local(t, ref=datetime(2026, 11, 6, 11, 0, tzinfo=UTC)) == "Sat 10:00 am"   # asked on Friday night
    r = rule({"type": "storm_potential", "min_cape_jkg": 100, "within_hours": 20})
    (c,) = evaluate(r, Snapshot(now=datetime(2026, 11, 6, 11, 0, tzinfo=UTC), home=HOME,
                                model_forecast={"issued": "2026-11-06T10:50:00+00:00", "hours": [{"t": t.isoformat(), "cape_jkg": 900, "shear_ms": 5.0}]}))
    assert "around Sat 10:00 am" in c.message
