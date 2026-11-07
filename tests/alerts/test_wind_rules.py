from datetime import UTC, datetime, timedelta

import pytest

from stormtracker.alerts.rules import ForecastGust, Rule, Snapshot, WindShift, evaluate, wind_shift

NOW = datetime(2026, 11, 7, 6, 0, tzinfo=UTC)
HOME = {"name": "Home", "lat": -33.87, "lon": 151.21}


def rule(params):
    return Rule(id="r", name="r", params=params)


def hist(dirs, speed=6.0, gust=14.0, step=3, end=NOW):
    n = len(dirs)
    return [{"ts": end - timedelta(minutes=step * (n - 1 - i)), "speed_ms": speed, "gust_ms": gust, "dir_deg": d} for i, d in enumerate(dirs)]


def test_a_sudden_swing_is_found_and_measured_across_north():
    h = hist([350, 355, 5, 10, 8, 12, 190, 200, 195, 205, 200, 198])               # a northerly change to southerly in ~33 min
    r = wind_shift(h, NOW, 40)
    assert r["shift_deg"] == pytest.approx(180, abs=25) and r["gust_kmh"] == pytest.approx(50.4, abs=0.1)
    steady = wind_shift(hist([350, 355, 5, 10, 8, 12, 355, 3, 7, 1, 5, 9]), NOW, 40)
    assert steady["shift_deg"] < 15                                                  # wobbling around north is not a swing, though 350 -> 10 spans the 0 line


def test_not_enough_or_too_light_or_too_short_readings_say_nothing():
    assert wind_shift(hist([10, 200, 10, 200]), NOW, 30) is None                      # too few readings
    assert wind_shift(hist([350] * 6 + [190] * 6, speed=1.0, gust=1.5), NOW, 40) is None   # calm: direction means little
    assert wind_shift(hist([350] * 4 + [190] * 4, step=1), NOW, 30) is None           # only 7 minutes of data in a 30 minute window
    old = hist([350] * 6 + [190] * 6, end=NOW - timedelta(hours=3))
    assert wind_shift(old, NOW, 30) is None                                           # nothing recent


def test_wind_shift_rule_needs_both_the_swing_and_the_gusts():
    h = hist([350] * 6 + [190] * 6)
    r = evaluate(rule(WindShift(min_shift_deg=60, within_min=40, min_gust_kmh=30)), Snapshot(now=NOW, wind_history=h))
    assert len(r) == 1 and r[0].key == "wind-shift" and "south" in r[0].message and "north" in r[0].message and r[0].data["shift_deg"] >= 150
    assert evaluate(rule(WindShift(min_shift_deg=60, within_min=40, min_gust_kmh=80)), Snapshot(now=NOW, wind_history=h)) == []      # gusts too gentle
    assert evaluate(rule(WindShift(min_shift_deg=179, within_min=40, min_gust_kmh=30)), Snapshot(now=NOW, wind_history=hist([350] * 6 + [100] * 6))) == []
    assert evaluate(rule(WindShift()), Snapshot(now=NOW)) == []                       # no history at all


def forecast(hours_gusts):
    return {"issued": NOW.isoformat(), "hours": [{"t": (NOW + timedelta(hours=i + 1)).isoformat(), "gust_10m_ms": g, "wind_dir_10m_deg": 250} for i, g in enumerate(hours_gusts)]}


def test_forecast_gust_looks_ahead_picks_the_peak_and_ignores_stale_forecasts():
    s = Snapshot(now=NOW, model_forecast=forecast([8, 12, 22, 15]))                   # peak 22 m/s = 79 km/h, three hours out
    (c,) = evaluate(rule(ForecastGust(min_gust_kmh=70, within_hours=6)), s)
    assert c.data["gust_kmh"] == 79 and "79 km/h" in c.message and "west-south-west" in c.message
    assert evaluate(rule(ForecastGust(min_gust_kmh=70, within_hours=2)), s) == []     # the peak is beyond the window
    assert evaluate(rule(ForecastGust(min_gust_kmh=90, within_hours=6)), s) == []
    old = {**forecast([30]), "issued": (NOW - timedelta(hours=5)).isoformat()}
    assert evaluate(rule(ForecastGust(min_gust_kmh=70)), Snapshot(now=NOW, model_forecast=old)) == []
    assert evaluate(rule(ForecastGust(min_gust_kmh=70)), Snapshot(now=NOW, model_forecast=forecast([None, None]))) == []   # a model without gusts
