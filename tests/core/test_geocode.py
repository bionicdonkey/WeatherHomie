"""Address search, area-word hints and the tile-server allowance in the page security policy."""
import httpx
import pytest

from stormtracker import bom_catalogue as cat
from stormtracker import geocode
from stormtracker.main import tile_http_origin
from stormtracker.sources.bom_warnings.plugin import BomWarnings
from tests.core.test_suggest import HOME, STATIONS

PLACE = {"suburb": "Exampleton", "town": "Exampleton", "city": "Sydney", "county": "Exampleton City Council", "state": "New South Wales"}


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(geocode, "_last", -10_000.0)
    monkeypatch.setattr(cat, "MIN_STATIONS", 3)


def test_area_words_are_place_names_without_council_boilerplate():
    assert geocode.area_words(PLACE) == ["Exampleton", "Sydney"]
    assert geocode.area_words({"village": "Smallville", "municipality": "Exampleton City Council"}) == ["Smallville", "Exampleton"]
    assert geocode.area_words({"county": "Shire of Hawkesbury"}) == ["Hawkesbury"] and geocode.area_words({"state": "NSW"}) == []
    assert geocode.area_words({"suburb": "Ab"}) == []


def test_server_address_is_validated():
    assert geocode.clean_base("http://192.168.1.5:8180/") == "http://192.168.1.5:8180" and geocode.clean_base("") == ""
    for bad in ("ftp://x", "http://x y", "javascript:alert(1)", "http://a@b"):
        with pytest.raises(ValueError):
            geocode.clean_base(bad)


async def test_search_and_reverse_parse_nominatim_replies_and_errors_are_plain():
    def handler(req):
        if req.url.path == "/search":
            assert req.url.params["countrycodes"] == "au" and req.url.params["q"] == "Smallville"
            return httpx.Response(200, json=[{"display_name": "Smallville, NSW", "lat": "-33.87", "lon": "151.21", "addresstype": "village"}, {"bad": 1}])
        if req.url.path == "/reverse":
            return httpx.Response(200, json={"address": PLACE})
        return httpx.Response(503)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        assert await geocode.search(http, "http://n", "Smallville") == [{"name": "Smallville, NSW", "lat": -33.87, "lon": 151.21, "kind": "village"}]
        assert (await geocode.reverse(http, "http://n", -33.7, 151.2))["suburb"] == "Exampleton"
        with pytest.raises(geocode.GeocodeError, match="didn't answer"):
            await geocode._get(http, "http://n", "/nope", {})
        with pytest.raises(geocode.GeocodeError, match="no address search server"):
            await geocode.search(http, "", "Smallville")


async def test_warnings_suggest_offers_area_words_from_the_address_server(make_ctx, tmp_path):
    def handler(req):
        return httpx.Response(200, json={"address": PLACE}) if req.url.path == "/reverse" else httpx.Response(200, text=STATIONS)
    ctx = make_ctx(handler, [HOME]); ctx.data_dir = tmp_path; ctx.extras = {"geocoder_url": "http://n"}
    s = await BomWarnings.suggest(HOME, ctx, {"area_keywords": "exampleton, sydney"})
    words = s.candidates[-1]
    assert words.values == {"area_keywords": "Exampleton, Sydney"} and words.current           # same words, any case: marked as in use
    ctx2 = make_ctx(lambda r: httpx.Response(503) if r.url.path == "/reverse" else httpx.Response(200, text=STATIONS), [HOME])
    ctx2.data_dir = tmp_path; ctx2.extras = {"geocoder_url": "http://n"}
    s = await BomWarnings.suggest(HOME, ctx2, {})
    assert len(s.candidates) == 1 and "couldn't be looked up" in s.note                    # the feed suggestion still works


def test_only_a_plain_http_tile_origin_is_added_to_the_policy():
    assert tile_http_origin("http://192.168.1.5:9000/tile/{z}/{x}/{y}.png") == "http://192.168.1.5:9000"
    assert tile_http_origin("http://tiles.lan/styles/{style}/{z}/{x}/{y}.png") == "http://tiles.lan"
    for no in ("https://tile.openstreetmap.org/{z}/{x}/{y}.png", "", "http://u:p@host/x", "http://a;b.com/x", "javascript:alert(1)", "http://host with space/x"):
        assert tile_http_origin(no) == "", no


async def test_routes_search_validate_and_widen_the_policy_only_for_the_saved_tile_origin(admin, app, monkeypatch):
    seen = {}

    async def fake(http, base, path, params):
        seen.update(base=base, path=path)
        return [{"display_name": "Smallville, NSW", "lat": "-33.8", "lon": "150.6", "addresstype": "village"}]
    monkeypatch.setattr(geocode, "_get", fake)
    r = await admin.get("/api/geocode?q=Smallville")
    assert r.json()["results"][0]["lat"] == -33.8 and seen["base"] == geocode.DEFAULT_URL
    assert (await admin.get("/api/geocode?q=ab")).status_code == 422
    base = {"tile_attribution": "x", "radar_opacity": 0.8}
    assert (await admin.put("/api/ui", json={**base, "tile_url": "", "geocoder_url": "http://my-nominatim:8180"})).status_code == 200
    await admin.get("/api/geocode?q=Smallville"); assert seen["base"] == "http://my-nominatim:8180"
    assert (await admin.put("/api/ui", json={**base, "tile_url": "", "geocoder_url": "javascript:x"})).status_code == 422
    h1 = (await admin.get("/")).headers["content-security-policy"]
    assert "http://192.168" not in h1
    await admin.put("/api/ui", json={**base, "tile_url": "http://192.168.1.9:9000/{z}/{x}/{y}.png", "geocoder_url": ""})
    h2 = (await admin.get("/")).headers["content-security-policy"]
    assert "img-src 'self' data: blob: https: http://192.168.1.9:9000;" in h2
    await admin.put("/api/ui", json={**base, "tile_url": "https://t.example/{z}/{x}/{y}.png", "geocoder_url": ""})
    assert "http://192.168" not in (await admin.get("/")).headers["content-security-policy"]
    app.state.core.registry  # noqa: B018
