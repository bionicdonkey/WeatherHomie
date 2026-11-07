import math
from datetime import timedelta

import numpy as np
import pytest
from synth import G, PX_KM, T0, frame, moving

from stormtracker.geo import latlon_to_pixel, pixel_to_latlon
from stormtracker.nowcast.cells import (MAX_AGE_MIN, Tracker, detect_cells, home_approach, track_summary)
from stormtracker.nowcast.engine import Motion, estimate_motion, forecast, select_sequence, to_km
from stormtracker.nowcast.impact import confidence, home_impact, station_cross_check
from stormtracker.nowcast.settings import NowcastSettings

P = NowcastSettings()


# ---- sequence selection -------------------------------------------------------------------------------------------
def test_select_sequence_stops_at_a_gap_and_dedupes():
    z = np.zeros((4, 4), np.float32)
    fr = [(T0 + timedelta(minutes=m), z) for m in (0, 5, 10, 30, 35, 40, 40)]      # hole between 10 and 30; duplicate 40
    seq = select_sequence(fr)
    assert [int((t - T0).total_seconds() / 60) for t, _ in seq] == [30, 35, 40]
    assert len(select_sequence(fr, max_frames=2)) == 2
    assert select_sequence([]) == []


# ---- motion + forecast --------------------------------------------------------------------------------------------
def test_motion_speed_and_bearing_match_the_known_motion():
    seq = moving(4, v_px=(3, -2))                                   # 3 px east, 2 px north per 5 min
    m = estimate_motion(seq, G)
    expect_kmh = math.hypot(3, 2) * PX_KM * 12                       # px/5min -> km/h
    assert m.method == "lucas-kanade" and m.frames_used == 4
    assert m.speed_kmh == pytest.approx(expect_kmh, rel=0.03)
    assert m.bearing_deg == pytest.approx(math.degrees(math.atan2(3, 2)), abs=2)
    assert m.coherence > 0.8


def test_forecast_puts_the_storm_where_physics_says_after_90_minutes():
    seq = moving(4, start=(150, 300), v_px=(3, -2))
    m = estimate_motion(seq, G)
    fc = forecast(seq[-1][1], m, 18, 5)
    assert fc.shape == (18, 512, 512)
    row, col = np.unravel_index(np.argmax(fc[-1]), fc[-1].shape)
    assert (col, row) == (pytest.approx(150 + 3 * 3 + 3 * 18, abs=1.5), pytest.approx(300 - 2 * 3 - 2 * 18, abs=1.5))
    assert fc[0].max() == pytest.approx(40, rel=0.1)


def test_forecast_uses_the_actual_frame_spacing():
    """Frames 6 minutes apart: the per-minute speed must still come out right."""
    seq = moving(4, v_px=(6, 0), step_min=6)                         # 6 px / 6 min = 1 px/min
    m = estimate_motion(seq, G)
    assert m.v[0][seq[-1][1] > 5].mean() == pytest.approx(1.0, abs=0.05)
    fc = forecast(seq[-1][1], m, 4, 5)                               # 5 min steps -> 5 px per step
    assert np.unravel_index(np.argmax(fc[0]), fc[0].shape)[1] == pytest.approx(180 + 18 + 5, abs=1.5)


def test_no_rain_one_frame_and_nan_are_handled():
    z = [(T0 + timedelta(minutes=5 * k), np.zeros((512, 512), np.float32)) for k in range(3)]
    m = estimate_motion(z, G)
    assert m.method == "none" and "no rain" in m.reason or "almost no rain" in m.reason
    assert not forecast(z[-1][1], m, 3, 5).any()                    # nothing in, nothing out
    one = estimate_motion(moving(1), G)
    assert one.method == "none" and "two" in one.reason
    persist = forecast(moving(1)[0][1], one, 2, 5)                  # no motion known -> persistence, not a crash
    assert persist.shape == (2, 512, 512) and persist.max() > 30
    nan = [(t, np.where(a < 0.5, np.nan, a)) for t, a in moving(4)]  # NaN around the storm, as real frames have
    mm = estimate_motion(nan, G)
    assert mm.method == "lucas-kanade" and np.isfinite(mm.v).all()
    assert np.isfinite(forecast(nan[-1][1], mm, 3, 5)).all()


def test_absurd_speeds_are_clipped():
    a = [(T0, frame([(100, 256, 40, 12)])), (T0 + timedelta(minutes=5), frame([(160, 256, 40, 12)]))]   # 60 px/5 min = 720 km/h
    m = estimate_motion(a, G)
    assert (m.speed_kmh or 0) <= 150.5


# ---- cells ----------------------------------------------------------------------------------------------------------
def test_detect_cells_threshold_area_and_position():
    a = frame([(200, 200, 40, 12), (350, 300, 40, 12), (300, 100, 12, 3)])    # last one is tiny and weak
    cells = detect_cells(a, G, T0, P)
    assert len(cells) == 2
    c = cells[0]
    assert (c.col, c.row) == (pytest.approx(200, abs=0.6), pytest.approx(200, abs=0.6)) or (c.col, c.row) == (pytest.approx(350, abs=0.6), pytest.approx(300, abs=0.6))
    assert c.max_mmh == pytest.approx(40, rel=0.02) and c.area_km2 > 30
    assert (c.lat, c.lon) == pytest.approx(pixel_to_latlon(G, c.col, c.row))
    assert detect_cells(a, G, T0, NowcastSettings(cell_threshold_mmh=60)) == []          # nothing that intense
    assert len(detect_cells(a, G, T0, NowcastSettings(cell_threshold_mmh=5, min_cell_area_km2=1))) == 3
    assert detect_cells(np.zeros((512, 512), np.float32), G, T0, P) == []
    assert detect_cells(np.full((512, 512), np.nan, np.float32), G, T0, P) == []


def run_tracker(seqs):
    tr = Tracker()
    for ts, arr in seqs:
        tr.update(ts, detect_cells(arr, G, ts, P))
    return tr


def test_tracker_keeps_ids_and_measures_velocity():
    tr = run_tracker(moving(5, v_px=(5, 0)))                          # 5 px / 5 min = 30 km/h east
    (t,) = tr.current()
    assert len(t.obs) == 5
    s = track_summary(t, None, 90)
    assert s["speed_kmh"] == pytest.approx(30, rel=0.05) and s["bearing_deg"] == pytest.approx(90, abs=3)
    ids = {x.id for x in tr.tracks.values()}
    assert len(ids) == 1                                              # one storm, one id, however many frames


def test_two_parallel_cells_do_not_swap_ids():
    seq = [(T0 + timedelta(minutes=5 * k), frame([(150 + 6 * k, 200, 40, 10), (150 + 6 * k, 240, 40, 10)])) for k in range(6)]
    tr = run_tracker(seq)
    assert len(tr.tracks) == 2
    for t in tr.tracks.values():
        rows = {round(o.row) for o in t.obs}
        assert len(rows) == 1                                          # each track stays on its own lane


def test_new_cell_gets_a_new_id_and_stale_tracks_expire():
    a = frame([(200, 200, 40, 12)])
    tr = Tracker()
    tr.update(T0, detect_cells(a, G, T0, P))
    t1 = T0 + timedelta(minutes=5)
    tr.update(t1, detect_cells(frame([(200, 200, 40, 12), (400, 400, 40, 12)]), G, t1, P))
    assert len(tr.tracks) == 2
    t2 = t1 + timedelta(minutes=MAX_AGE_MIN + 5)
    tr.update(t2, [])
    assert tr.tracks == {}
    tr.update(t2 - timedelta(minutes=1), detect_cells(a, G, t2, P))   # out-of-order frame is ignored
    assert tr.tracks == {}


def test_home_approach_eta_and_non_cases():
    tr = run_tracker(moving(5, start=(100, 256), v_px=(5, 0)))        # now at col 120, heading east at 30 km/h
    (t,) = tr.current()
    home = to_km(G, 300, 256)                                         # 180 px = 90 km ahead on its path
    a = home_approach(t, home, 240)
    reach = t.last.radius_km + 1.0
    expect = (90 - reach) / 0.5                                       # km / (km/min)
    assert a["approaching"] and a["eta_min"] == pytest.approx(expect, abs=3)
    assert home_approach(t, home, 60)["approaching"] is False         # beyond a 60 min horizon
    assert home_approach(t, to_km(G, 300, 150), 240)["approaching"] is False      # passes 53 km to the side
    assert home_approach(t, to_km(G, 40, 256), 240)["approaching"] is False       # behind it: moving away
    over = home_approach(t, to_km(G, 120, 256), 240)
    assert over["approaching"] and over["eta_min"] == 0
    still = run_tracker([(T0 + timedelta(minutes=5 * k), frame([(120, 256, 40, 12)])) for k in range(5)])
    assert home_approach(still.current()[0], home, 240)["approaching"] is False    # stationary: no ETA


# ---- home impact ------------------------------------------------------------------------------------------------------
def impact_for(seq, home_px):
    m = estimate_motion(seq, G)
    fc = forecast(seq[-1][1], m, 18, 5)
    lat, lon = pixel_to_latlon(G, *home_px)
    return home_impact(seq[-1][1], fc, G, lat, lon, seq[-1][0], m, P), m


def test_impact_approaching_has_the_right_eta_peak_and_end():
    seq = moving(4, start=(180, 256), v_px=(5, 0))                    # now at col 195, 30 km/h
    imp, _ = impact_for(seq, (275, 256))
    # rain >= 1 mm/h reaches the 2 km window around home when the core is ~27 px away: (275-27-195)/5 steps * 5 min = 53 min
    assert imp["status"] == "approaching"
    assert imp["eta_lead_min"] == pytest.approx(53, abs=6)
    assert imp["peak_mmh"] > 30 and imp["peak_lead_min"] == pytest.approx(80, abs=6)    # core passes at (275-195)/5*5 = 80 min
    assert imp["end_lead_min"] is None or imp["end_lead_min"] > imp["peak_lead_min"]
    assert imp["confidence"]["level"] in ("medium", "low") and imp["confidence"]["value"] < 0.5   # an hour ahead: not high


def test_impact_raining_clear_and_range():
    seq = moving(4, start=(180, 256), v_px=(5, 0))
    raining, _ = impact_for(seq, (195, 256))
    assert raining["status"] == "raining" and raining["now_mmh"] > 30 and raining["end_lead_min"] is not None
    clear, _ = impact_for(seq, (195, 400))                            # 144 px (72 km) off the path
    assert clear["status"] == "clear" and clear["nearest_rain_km"] > 60
    assert clear["nearest_rain_bearing_deg"] is not None
    m = estimate_motion(seq, G)
    outside = home_impact(seq[-1][1], forecast(seq[-1][1], m, 3, 5), G, 10.0, 100.0, seq[-1][0], m, P)
    assert outside["status"] == "unknown"


def test_confidence_falls_with_lead_and_caps_heavy_rain():
    m = Motion(np.zeros((2, 4, 4), np.float32), "lucas-kanade", 4, 20.0, 90.0, 0.9)
    vals = [confidence(l, m, 5)["value"] for l in (0, 10, 30, 60, 90)]
    assert vals == sorted(vals, reverse=True) and vals[0] > 0.9
    assert confidence(60, m, 40)["value"] <= 0.45 and "heavy" in " ".join(confidence(60, m, 40)["reasons"])
    assert confidence(30, Motion(np.zeros((2, 4, 4), np.float32), "none", 1, reason="x"), 5)["value"] < confidence(30, m, 5)["value"]
    assert confidence(30, m, 5, tracks_agree=False)["value"] < confidence(30, m, 5)["value"]


def test_station_cross_check():
    now = T0
    ok = station_cross_check(True, {"rain_rate_mmh": 4.0, "pressure_hpa": 1006.0, "wind_gust_ms": 16.0}, now - timedelta(minutes=2), now, 1009.5, [{"distance_km": 6}, {"distance_km": 25}])
    assert ok["agrees_with_radar"] is True and ok["pressure_falling_fast"] is True and ok["gusty"] is True and ok["lightning_within_10km_30min"] == 1
    assert station_cross_check(True, {"rain_rate_mmh": 0.0}, now, now, None, [])["agrees_with_radar"] is False
    stale = station_cross_check(False, {"rain_rate_mmh": 0.0}, now - timedelta(minutes=40), now, None, [])
    assert stale["available"] is False and "min old" in stale["reason"]
    assert station_cross_check(False, None, None, now, None, []) == {"available": False}


def test_settings_validation():
    with pytest.raises(ValueError):
        NowcastSettings(horizon_min=500)
    with pytest.raises(ValueError):
        NowcastSettings(cell_threshold_mmh=0.1)
    assert NowcastSettings().horizon_min == 90
