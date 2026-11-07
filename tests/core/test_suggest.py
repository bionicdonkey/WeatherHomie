"""'Fill from my location': suggestions for the BOM sources, from synthetic station lists and feeds."""
import json
from datetime import timedelta

import httpx
import pytest

from stormtracker import bom_catalogue as cat
from stormtracker.sources.bom_observations.plugin import BomObservations
from stormtracker.sources.bom_radar.plugin import BomRadar
from stormtracker.sources.bom_warnings.plugin import BomWarnings
from stormtracker.timeutil import utcnow

HOME = {"id": "home", "name": "Home", "lat": -33.87, "lon": 151.21}

HEADER = """Bureau of Meteorology product IDCJMC0014.                                       Produced: 06 Nov 2026

   Site  Dist  Site name                                 Start     End      Lat       Lon Source         STA Height (m)   Bar_ht    WMO
------- ----- ---------------------------------------- ------- ------- -------- --------- -------------- --- ---------- -------- ------
"""


def row(site, name, start, end, lat, lon, sta, wmo):
    return f" {site} {site[:2]}    {name:<40} {start:>7} {end:>7} {lat:>8} {lon:>9} {'GPS':<14} {sta:<3} {'81.2':>10} {'..':>8} {wmo:>6}\n"


STATIONS = HEADER + "".join([
    row("067121", "EXAMPLE AIRFIELD", 2018, "..", -33.91, 151.23, "NSW", 99101),            # open, but BOM has no product for it
    row("067104", "HARBOUR EXAMPLE AWS", 1995, "..", -33.92, 151.26, "NSW", 99102),
    row("067113", "NORTH LAKES AWS", 2001, "..", -33.75, 151.21, "NSW", 99103),
    row("067050", "OLD SCHOOL EXAMPLE", 1919, 1929, -33.91, 151.28, "NSW", ".."),           # closed
    row("067093", "WEIR EXAMPLE", 1990, "..", -33.89, 151.16, "NSW", ".."),                 # open, no WMO number
    row("072150", "WAGGA WAGGA AMO", 1941, "..", -35.1583, 147.4575, "NSW", 94910),
    row("040001", "FAR AWAY", 1980, "..", -27.0, 153.0, "QLD", 94000),
    "\n(c) Copyright Commonwealth of Australia 2026\n"])


def test_parse_station_list_keeps_open_stations_and_wmo():
    st = cat.parse_stations(STATIONS)
    names = [s.name for s in st]
    assert "Old School Example" not in names and "Harbour Example AWS" in names and "Example Airfield" in names
    assert {s.name: s.wmo for s in st}["Weir Example"] is None
    d, s = cat.nearest(st, HOME["lat"], HOME["lon"], limit=1, need_wmo=True)[0]
    assert s.wmo == 99101 and d == pytest.approx(5.0, abs=0.5)
    assert cat.warnings_feed_url("NSW").endswith("IDZ00054.warnings_nsw.xml") and cat.warnings_feed_url("ACT").endswith("IDZ00054.warnings_nsw.xml")
    assert cat.warnings_feed_url("VIC").endswith("IDZ00059.warnings_vic.xml") and cat.warnings_feed_url("ANT") is None
    assert cat.obs_products("NT") == ["IDD60801", "IDD60901"] and cat.obs_products("ACT")[0] == "IDN60903"


async def test_station_list_is_cached_and_a_stale_cache_survives_an_outage(tmp_path):
    hits = []

    def handler(req):
        hits.append(1)
        return httpx.Response(200, text=STATIONS) if len(hits) == 1 else httpx.Response(503)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        a = await cat.load_stations(http, tmp_path, now=1_000_000)
        b = await cat.load_stations(http, tmp_path, now=1_000_100)            # fresh: no second request
        assert len(hits) == 1 and a == b
        c = await cat.load_stations(http, tmp_path, now=1_000_000 + cat.CACHE_TTL_S + 5)   # expired + BOM down: keep the old list
        assert len(hits) == 2 and c == a
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503))) as http:
        with pytest.raises(httpx.HTTPError):
            await cat.load_stations(http, tmp_path / "empty", now=5)


def obs_json(minutes_ago):
    t = utcnow() - timedelta(minutes=minutes_ago)
    return {"observations": {"data": [{"aifstime_utc": t.strftime("%Y%m%d%H%M%S"), "air_temp": 18.0, "lat": -33.93, "lon": 151.23}]}}


@pytest.fixture(autouse=True)
def small_lists(monkeypatch):
    monkeypatch.setattr(cat, "MIN_STATIONS", 3)          # the synthetic list is tiny; the real one has thousands


@pytest.fixture
def ctx_for(make_ctx, tmp_path):
    def _make(handler, sub="a"):
        ctx = make_ctx(handler, [HOME])
        ctx.data_dir = tmp_path / sub                       # each list gets its own cache directory
        return ctx
    return _make


async def test_observations_suggest_probes_for_a_live_product_and_skips_dead_ones(ctx_for):
    seen = []

    def handler(req):
        u = str(req.url)
        seen.append(u)
        if u.endswith("stations.txt"):
            return httpx.Response(200, text=STATIONS)
        if "99102" in u and "IDN60801" in u:
            return httpx.Response(200, json=obs_json(25))
        if "99103" in u and "IDN60901" in u:                         # the second product for this one
            return httpx.Response(200, json=obs_json(10))
        return httpx.Response(404)
    s = await BomObservations.suggest(HOME, ctx_for(handler), {"wmo": 99102})
    assert [c.values for c in s.candidates] == [{"wmo": 99102, "product_id": "IDN60801"}, {"wmo": 99103, "product_id": "IDN60901"}]
    first = s.candidates[0]
    assert first.label == "Harbour Example AWS" and first.current and "WMO 99102" in first.detail and "25 min" in first.detail
    assert not s.candidates[1].current
    assert any("99101" in u for u in seen)                          # the nearest was tried first, then passed over


async def test_observations_suggest_skips_a_station_that_stopped_reporting_and_handles_none(ctx_for):
    def handler(req):
        if str(req.url).endswith("stations.txt"):
            return httpx.Response(200, text=STATIONS)
        return httpx.Response(200, json=obs_json(60 * 20))           # 20 hours old
    s = await BomObservations.suggest(HOME, ctx_for(handler), {})
    assert s.candidates == [] and "No open BOM station" in s.note


async def test_warnings_suggest_picks_the_state_feed_and_flags_a_border(ctx_for):
    def handler(req):
        return httpx.Response(200, text=STATIONS)
    s = await BomWarnings.suggest(HOME, ctx_for(handler), {})
    assert len(s.candidates) == 1 and s.candidates[0].values["feed_url"].endswith("IDZ00054.warnings_nsw.xml")
    assert "starting point" in s.note and len(s.candidates) == 1          # no address server given: no area words offered
    # a point on the Queensland border: the nearest station is in QLD, with a NSW one 20 km away
    border = {**HOME, "lat": -28.5, "lon": 153.5}
    text = HEADER + row("058001", "NSW SIDE", 1990, "..", -28.67, 153.5, "NSW", 94001) + row("040002", "QLD SIDE", 1990, "..", -28.4, 153.5, "QLD", 94002) + row("040003", "QLD INLAND", 1990, "..", -28.2, 153.0, "QLD", 94003)
    s = await BomWarnings.suggest(border, ctx_for(lambda r: httpx.Response(200, text=text), "b"), {"feed_url": cat.warnings_feed_url("QLD")})
    assert [c.values["feed_url"].rsplit("/", 1)[1] for c in s.candidates] == ["IDZ00056.warnings_qld.xml", "IDZ00054.warnings_nsw.xml"]
    assert s.candidates[0].current and not s.candidates[1].current and "state border" in s.note


class FakeFtp:
    NAMES = ["IDR713.T.202611061200.png", "IDR033.T.202611061200.png", "IDR032.T.202611061200.png", "IDR043.T.202611061200.png",
             "IDR00004.T.202611061200.png", "IDR023.T.202611061200.png"]            # IDR03 only has the 256 km product here; Newcastle too far

    @staticmethod
    def list_names(directory, prefix):
        return [n for n in FakeFtp.NAMES if n.startswith(prefix)]


async def test_radar_suggest_ranks_by_distance_and_does_not_prefer_the_current_one(ctx_for, monkeypatch):
    monkeypatch.setattr(BomRadar, "ftp", FakeFtp)
    s = await BomRadar.suggest(HOME, ctx_for(lambda r: httpx.Response(404)), {"radar_id": "IDR033"})
    ids = [c.values["radar_id"] for c in s.candidates]
    assert ids[:2] == ["IDR713", "IDR033"]                           # Terrey Hills is nearer, and is not the one already in use
    by = {c.values["radar_id"]: c for c in s.candidates}
    assert by["IDR033"].current and not by["IDR713"].current
    assert "128 km range" in by["IDR713"].detail and "about 1 km" in by["IDR033"].detail and "about 1 km" not in by["IDR713"].detail
    assert "isn't always the better one" in s.note
    assert all(c.values["radar_id"][:-1] != "IDR02" for c in s.candidates)      # Melbourne is not within 400 km


async def test_radar_suggest_without_the_ftp_listing_still_ranks_and_says_so(ctx_for, monkeypatch):
    class Down:
        @staticmethod
        def list_names(d, p):
            raise OSError("ftp down")
    monkeypatch.setattr(BomRadar, "ftp", Down)
    s = await BomRadar.suggest(HOME, ctx_for(lambda r: httpx.Response(404)), {})
    assert s.candidates[0].values["radar_id"] == "IDR713" and "by distance only" in s.note


async def test_suggest_route_needs_a_location_and_never_saves(admin, app, monkeypatch):
    from stormtracker.sources.open_meteo.plugin import OpenMeteo
    for c in (BomRadar, BomObservations, BomWarnings, OpenMeteo):
        app.state.core.registry.register(c)
    monkeypatch.setattr(BomRadar, "ftp", FakeFtp)
    assert (await admin.get("/api/sources/bom_radar/suggest")).status_code == 422             # no location yet
    await admin.put("/api/locations", json=[HOME])
    r = await admin.get("/api/sources/bom_radar/suggest")
    assert r.status_code == 200 and r.json()["location"]["id"] == "home" and r.json()["candidates"][0]["values"]["radar_id"] == "IDR713"
    assert (await admin.get("/api/sources/bom_radar/suggest?location_id=nope")).status_code == 422
    assert (await admin.get("/api/sources/open_meteo/suggest")).status_code == 404            # nothing to suggest
    assert (await admin.get("/api/sources/nonexistent/suggest")).status_code == 404
    srcs = {s["meta"]["name"]: s for s in (await admin.get("/api/sources")).json()}
    assert srcs["bom_radar"]["can_suggest"] and srcs["bom_observations"]["can_suggest"] and srcs["bom_warnings"]["can_suggest"]
    assert not srcs["open_meteo"]["can_suggest"]
    assert srcs["bom_radar"]["settings"]["config"] in ({}, None) or "radar_id" not in srcs["bom_radar"]["settings"]["config"]   # not saved
