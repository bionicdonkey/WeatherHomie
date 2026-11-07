import numpy as np
import pytest
from bom_helpers import GREY, make_png
from PIL import Image
import io

from stormtracker.sources.bom_radar.georef import georef_for, latlon_to_pixel, parse_radar_id, pixel_to_latlon
from stormtracker.sources.bom_radar.palette import RAIN_CLASSES, default_class_rates, rgba_to_rain_rate
from stormtracker.sources.bom_radar.plugin import decode_frame


def test_class_rates_monotonic_and_inside_bins():
    rates = default_class_rates()
    lows = [lo for _, lo in RAIN_CLASSES]
    assert len(rates) == 15 and rates == sorted(rates)
    for r, lo, hi in zip(rates, lows, lows[1:] + [lows[-1] * 1.25]):
        assert lo < r < hi


def test_every_class_colour_maps_to_its_rate():
    rates = default_class_rates()
    arr, stats = decode_frame(make_png())
    for i, rate in enumerate(rates):
        assert arr[105, 25 + 12 * i] == pytest.approx(rate)
    assert stats["rain_pixels"] == 15 * 100


def test_non_data_pixels():
    arr, stats = decode_frame(make_png())
    assert np.isnan(arr[:16]).all()              # grey header bar + black text -> NaN, not rain
    assert (arr[200:, :] == 0).all()             # transparent -> 0 mm/h (no echo)
    rgba = np.zeros((2, 2, 4), np.uint8)
    rgba[0, 0] = (1, 2, 3, 255)                  # unknown opaque colour
    out, st = rgba_to_rain_rate(rgba)
    assert np.isnan(out[0, 0]) and out[1, 1] == 0 and st["unknown_pixels"] == 1


def test_class_rate_override_validated():
    arr, _ = decode_frame(make_png(), [float(i + 1) for i in range(15)])
    assert arr[105, 25] == 1.0 and arr[105, 25 + 12 * 14] == 15.0
    with pytest.raises(ValueError):
        decode_frame(make_png(), [1.0, 2.0])


def test_georef_pixel_sizes_and_validation():
    assert georef_for("IDR713").pixel_size_m == 500
    assert georef_for("IDR714").pixel_size_m == 250
    assert georef_for("IDR712").pixel_size_m == 1000
    assert georef_for("IDR711").pixel_size_m == 2000
    assert parse_radar_id("IDR713") == ("IDR71", 128)
    for bad in ("IDR71", "XYZ713", "IDR715", "idr713"):
        with pytest.raises(ValueError):
            parse_radar_id(bad)
    with pytest.raises(ValueError):
        georef_for("IDR993")                     # a site that isn't in BOM's list, no override
    assert georef_for("IDR993", -35.0, 149.0).lat0 == -35.0
    assert georef_for("IDR033").lat0 == pytest.approx(-34.264)       # others: BOM's published position
    assert georef_for("IDR713").lat0 == -33.7008                     # Terrey Hills keeps its exact, fitted position
    assert parse_radar_id("IDR1143") == ("IDR114", 128) and parse_radar_id("IDR1083") == ("IDR108", 128)


def test_projection_roundtrip_and_centre():
    g = georef_for("IDR713")
    assert latlon_to_pixel(g, g.lat0, g.lon0) == pytest.approx((256.0, 256.0))   # radar pixel, verified on BOM range rings
    for col, row in [(10, 20), (400.5, 100.25), (255.5, 0), (500, 500)]:
        la, lo = pixel_to_latlon(g, col, row)
        assert latlon_to_pixel(g, la, lo) == pytest.approx((col, row), abs=1e-6)
    # 64 km due north = 128 px up
    la, lo = pixel_to_latlon(g, 256.0, 256.0 - 128)
    assert lo == pytest.approx(g.lon0, abs=1e-6) and (la - g.lat0) * 111.2 == pytest.approx(64, rel=0.01)


def test_projection_matches_bom_overlay_landmark():
    """Terrey Hills town label dot sits at ~(261, 252) in BOM's own IDR713 locations overlay
    (measured on a real frame); our projection must agree to a few pixels."""
    col, row = latlon_to_pixel(georef_for("IDR713"), -33.685, 151.226)
    assert abs(col - 261) < 4 and abs(row - 252) < 4


# Corner coordinates published by BOM in radar_transparencies/coordinates/IDR713.map and IDR714.map
# (SW, NW, NE, SE). Facts, not imagery. The image extent must come out ~512 px square.
BOM_MAP_CORNERS = {
    "IDR713": [(-34.83832, 149.81175), (-32.53770, 149.84869), (-32.53762, 152.57796), (-34.83823, 152.61510)],
    "IDR714": [(-34.27134, 150.51477), (-33.12062, 150.52402), (-33.12061, 151.89801), (-34.27132, 151.90730)],
}


@pytest.mark.parametrize("rid", ["IDR713", "IDR714"])
def test_projection_agrees_with_bom_published_corners(rid):
    g = georef_for(rid)
    (sw, nw, ne, se) = [latlon_to_pixel(g, la, lo) for la, lo in BOM_MAP_CORNERS[rid]]
    assert abs((ne[0] - nw[0]) - 512) < 1.0 and abs((sw[1] - nw[1]) - 512) < 1.0   # extent is 512 px
    # corners lie on the image edge to within ~1.5 px (BOM's rounding / grid-origin convention)
    for col, row in (sw, nw, ne, se):
        assert min(abs(col - 0), abs(col - 512)) < 1.5 and min(abs(row - 0), abs(row - 512)) < 1.5
