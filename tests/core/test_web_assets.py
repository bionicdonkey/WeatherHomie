"""The UI is plain files (no build step), so the failure mode is a missing/misnamed file. Check every
reference resolves on disk and is actually served."""
import re
from pathlib import Path

WEB = Path(__file__).parents[2] / "src" / "stormtracker" / "web"


def refs_in_html(text):
    return re.findall(r'(?:href|src)="((?!https?:|data:|#)[^"]+)"', text)


def refs_in_css(text):
    return [u for u in re.findall(r'url\("?([^")]+)"?\)', text) if not u.startswith(("data:", "http"))]


def imports_in_js(text):
    return re.findall(r"""(?:from|import\()\s*['"](\.[^'"]+)['"]""", text)


def test_every_referenced_file_exists():
    missing = []
    index = (WEB / "index.html").read_text()
    for r in refs_in_html(index):
        if not (WEB / r).is_file():
            missing.append(("index.html", r))
    for css in (WEB / "css").glob("*.css"):
        for r in refs_in_css(css.read_text()):
            if not (css.parent / r).resolve().is_file():
                missing.append((css.name, r))
    for js in WEB.rglob("*.js"):
        if "vendor" in js.parts:
            continue
        for r in imports_in_js(js.read_text()):
            if not (js.parent / r).resolve().is_file():
                missing.append((str(js.relative_to(WEB)), r))
    assert not missing, missing


def test_leaflet_assets_and_licences_are_vendored():
    for f in ("leaflet.js", "leaflet.css", "LICENSE", "images/layers.png", "images/marker-icon.png"):
        assert (WEB / "vendor" / "leaflet" / f).is_file(), f
    assert (WEB / "vendor" / "fonts" / "OFL.txt").is_file()


async def test_ui_is_served_without_auth_and_data_is_not(client):
    r = await client.get("/")
    assert r.status_code == 200 and "StormTracker" in r.text
    index = (WEB / "index.html").read_text()
    for ref in refs_in_html(index):
        assert (await client.get("/" + ref)).status_code == 200, ref
    assert (await client.get("/js/main.js")).headers["content-type"].startswith(("text/javascript", "application/javascript"))
    assert (await client.get("/api/sources")).status_code == 401           # static shell is public; data is not
    assert (await client.get("/api/radar/frames")).status_code == 401


def test_ui_has_no_external_runtime_dependencies():
    """Self-hosted on a LAN: no CDN scripts, styles or fonts. (Map tiles are the one user-configurable exception.)"""
    for f in [WEB / "index.html", *(WEB / "css").glob("*.css"), *[p for p in WEB.rglob("*.js") if "vendor" not in p.parts]]:
        found = re.findall(r"https?://(?!www\.w3\.org)[^\s'\"<>)]+", f.read_text())
        # allowed: the default basemap, templated URLs, and example addresses shown in hint text (never fetched)
        found = [u for u in found if "tile.openstreetmap.org" not in u and "{" not in u and "//homeserver" not in u
                 and "nominatim.openstreetmap.org" not in u and "//192.168." not in u]     # the default address-search server, and LAN examples
        assert not found, (f.name, found)
