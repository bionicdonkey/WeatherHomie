import json
from pathlib import Path
from urllib.parse import parse_qsl

import pytest

from stormtracker.sources.ecowitt.mapping import convert_livedata, convert_push_fields, parse_value

FX = Path(__file__).parents[2] / "fixtures" / "ecowitt"


def form(name):
    return dict(parse_qsl((FX / name).read_text().strip(), keep_blank_values=True))


def test_ecowitt_push_fixture_converts_to_si():
    v = convert_push_fields(form("ecowitt_push.form"))
    assert v["temp_c"] == pytest.approx(20.0) and v["temp_in_c"] == pytest.approx(22.5)
    assert v["humidity_pct"] == 55 and v["humidity_in_pct"] == 45
    assert v["pressure_hpa"] == pytest.approx(1013.21, abs=0.01) and v["pressure_abs_hpa"] == pytest.approx(1009.5, abs=0.1)
    assert v["wind_speed_ms"] == pytest.approx(4.4704) and v["wind_gust_ms"] == pytest.approx(6.9, abs=0.05)
    assert v["wind_dir_deg"] == 225 and v["wind_gust_max_daily_ms"] == pytest.approx(10.0, abs=0.05)
    assert v["rain_rate_mmh"] == pytest.approx(5.0, abs=0.05) and v["rain_daily_mm"] == pytest.approx(2.0, abs=0.05)
    assert v["solar_wm2"] == pytest.approx(412.3) and v["uv_index"] == 4
    # extra sensors are handled generically
    assert v["temp_c_1"] == pytest.approx(10.0) and v["humidity_pct_1"] == 80 and v["soil_moisture_pct_1"] == 33
    assert v["pm25_ugm3_1"] == 12.5 and v["lightning_count"] == 3 and v["lightning_distance_km"] == 14
    # unknown numeric fields are kept (battery flags) rather than dropped; metadata/strings are not
    assert v["x_wh65batt"] == 0 and v["x_batt1"] == 0
    assert "PASSKEY" not in v and "model" not in v and "dateutc" not in v and "lightning_time" not in v
    assert all(k == k.lower() for k in v)


def test_wunderground_protocol_fixture():
    v = convert_push_fields(form("wu_push.query"))
    assert v["temp_c"] == pytest.approx(20.0) and v["dewpoint_c"] == pytest.approx(10.56, abs=0.01)
    assert v["pressure_hpa"] == pytest.approx(1013.21, abs=0.01)
    assert v["rain_hourly_mm"] == pytest.approx(0.99, abs=0.01) and v["rain_daily_mm"] == pytest.approx(2.0, abs=0.01)
    assert v["temp_in_c"] == pytest.approx(22.5) and v["humidity_in_pct"] == 45 and v["uv_index"] == 4
    assert not any(k in v for k in ("id", "x_password", "x_id", "x_softwaretype"))


def test_metric_fields_not_double_converted():
    v = convert_push_fields({"tempc": "20.5", "baromrelhpa": "1010.0", "windspeedkmh": "36", "rainratemm": "7.5",
                             "dailyrainmm": "3.0", "lightning_mi": "10"})
    assert v == {"temp_c": 20.5, "pressure_hpa": 1010.0, "wind_speed_ms": 10.0, "rain_rate_mmh": 7.5,
                 "rain_daily_mm": 3.0, "lightning_distance_km": pytest.approx(16.0934)}


def test_bad_values_skipped():
    assert convert_push_fields({"tempf": "", "humidity": "None", "winddir": "nan", "uv": "inf", "Weird Key!": "1"}) == {
        "x_weird_key": 1.0}


def test_parse_value_units():
    assert parse_value("79.2", "F") == pytest.approx(26.22, abs=0.01)
    assert parse_value("0.50 in/Hr") == pytest.approx(12.7)
    assert parse_value("29.40 inHg") == pytest.approx(995.6, abs=0.1)
    assert parse_value("65%") == 65 and parse_value("34 km") == 34
    assert parse_value("None") is None and parse_value("5 furlongs") is None and parse_value(None) is None


def test_livedata_imperial_and_metric():
    v = convert_livedata(json.loads((FX / "gw_livedata_imperial.json").read_text()))
    assert v["temp_c"] == pytest.approx(26.22, abs=0.01) and v["humidity_pct"] == 65 and v["wind_dir_deg"] == 37
    assert v["wind_gust_ms"] == pytest.approx(0.5, abs=0.01) and v["solar_wm2"] == 0 and v["uv_index"] == 0
    assert v["rain_rate_mmh"] == pytest.approx(12.7) and v["rain_daily_mm"] == pytest.approx(25.4)
    assert v["rain_rate_piezo_mmh"] == pytest.approx(2.54) and v["rain_daily_piezo_mm"] == pytest.approx(5.08)
    assert v["pressure_hpa"] == pytest.approx(995.6, abs=0.1) and v["temp_in_c"] == pytest.approx(27.22, abs=0.01)
    assert v["temp_c_1"] == pytest.approx(26.78, abs=0.01) and "humidity_pct_1" not in v   # "None" humidity skipped
    assert v["humidity_pct_2"] == 64 and v["soil_moisture_pct_1"] == 33 and v["pm25_ugm3_1"] == 27.0
    assert v["lightning_distance_km"] == 34 and v["lightning_count"] == 2
    m = convert_livedata(json.loads((FX / "gw_livedata_metric.json").read_text()))
    assert m["temp_c"] == 20.0 and m["wind_speed_ms"] == 5.0 and m["wind_gust_ms"] == pytest.approx(5.0)
    assert m["rain_rate_mmh"] == 12.0 and m["rain_daily_mm"] == 3.0 and m["pressure_hpa"] == 1013.2
