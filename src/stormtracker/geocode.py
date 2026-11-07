"""Address search and reverse lookup through a Nominatim server (OpenStreetMap's geocoder).

The server address is a setting, so a self-hosted Nominatim works and the public one is only the default. The public
server's usage policy allows light, identified use (one request a second, no autocomplete), so searches here only
happen when the user asks, and calls are spaced out.
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Any

import httpx

DEFAULT_URL = "https://nominatim.openstreetmap.org"
_PLACE_KEYS = ("suburb", "village", "hamlet", "town", "city", "municipality", "county")
_GENERIC = re.compile(r"\b(city council|city|council|shire|regional|region|municipality|of)\b", re.I)
_gate = asyncio.Lock()
_last = 0.0


class GeocodeError(Exception):
    pass


def clean_base(url: str | None) -> str:
    u = (url or "").strip().rstrip("/")
    if u and not re.fullmatch(r"https?://[A-Za-z0-9._-]+(:\d{1,5})?(/[A-Za-z0-9._/-]*)?", u):
        raise ValueError("must be an http(s) address such as http://myserver:8180")
    return u


async def _get(http: httpx.AsyncClient, base: str, path: str, params: dict[str, Any]) -> Any:
    global _last
    if not base:
        raise GeocodeError("no address search server is set (Settings, under Map and address search)")
    async with _gate:                                      # at most one request a second, even with several tabs open
        wait = 1.05 - (time.monotonic() - _last)
        if wait > 0:
            await asyncio.sleep(wait)
        _last = time.monotonic()
        try:
            r = await http.get(f"{base}{path}", params={"format": "jsonv2", "addressdetails": 1, **params}, timeout=20)
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise GeocodeError(f"the address search server didn't answer ({type(e).__name__})") from None


async def search(http: httpx.AsyncClient, base: str, q: str, limit: int = 5) -> list[dict[str, Any]]:
    rows = await _get(http, base, "/search", {"q": q, "limit": limit, "countrycodes": "au"})
    out = []
    for x in rows if isinstance(rows, list) else []:
        try:
            out.append({"name": str(x["display_name"]), "lat": round(float(x["lat"]), 6), "lon": round(float(x["lon"]), 6),
                        "kind": str(x.get("addresstype") or x.get("type") or "")})
        except (KeyError, TypeError, ValueError):
            continue
    return out


async def reverse(http: httpx.AsyncClient, base: str, lat: float, lon: float) -> dict[str, Any]:
    j = await _get(http, base, "/reverse", {"lat": lat, "lon": lon, "zoom": 14})
    return j.get("address") or {} if isinstance(j, dict) else {}


def area_words(address: dict[str, Any]) -> list[str]:
    """Place names around a point, as words that might appear in a BOM warning title (suburb, town, city, council area).

    Council names are trimmed of generic words ("Example City Council" -> "Example"). These are guesses at BOM's wording."""
    words: list[str] = []
    for k in _PLACE_KEYS:
        v = address.get(k)
        if isinstance(v, str):
            v = _GENERIC.sub("", v).strip(" ,-") if k in ("municipality", "county") else v.strip()
            if len(v) >= 3 and v.lower() not in {w.lower() for w in words}:
                words.append(v)
    return words[:6]
