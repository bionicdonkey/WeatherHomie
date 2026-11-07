from __future__ import annotations

import hmac
import logging
from datetime import datetime
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select

from .. import geocode
from ..auth import COOKIE, SESSION_MAX_AGE, Principal
from ..locations import Location
from ..plugins import Health, Source, SourceContext, secret_fields
from ..timeutil import ensure_utc
from ..settings.sources import SourceSettingsError
from ..storage.tables import EventRow, ObservationRow


class Credentials(BaseModel):
    username: str
    password: str


UI_DEFAULTS = {
    "tile_url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
    "tile_attribution": "© OpenStreetMap contributors",
    "tile_url_dark": "",
    "radar_opacity": 0.8,
    "geocoder_url": geocode.DEFAULT_URL,
}


class UiSettings(BaseModel):
    tile_url: str = Field(UI_DEFAULTS["tile_url"], max_length=300,
                          description="Basemap tile URL template. If it contains {style}, that becomes 'light' or 'dark' to match "
                                      "the theme; otherwise dark mode darkens the tiles. Leave empty to show only the "
                                      "radar's own background layers (works offline).")
    tile_url_dark: str = Field("", max_length=300, description="Optional tile address to use instead in dark mode (for a tile server with a separate dark style).")
    tile_attribution: str = Field(UI_DEFAULTS["tile_attribution"], max_length=300)
    radar_opacity: float = Field(0.8, ge=0.1, le=1.0)
    geocoder_url: str = Field(geocode.DEFAULT_URL, max_length=200,
                              description="Nominatim server used for address search. Empty turns address search off.")

    @field_validator("geocoder_url")
    @classmethod
    def _geo(cls, v: str) -> str:
        return geocode.clean_base(v)


class PasswordChange(BaseModel):
    current_password: str
    new_password: str


class SetupBody(Credentials):
    setup_token: str | None = None


class SourceUpdate(BaseModel):
    enabled: bool | None = None
    interval_s: int | None = Field(default=None, ge=1)
    clear_interval: bool = False
    config: dict[str, Any] | None = None
    secrets: dict[str, str | None] | None = Field(
        default=None, description="write-only; null/'' deletes. Values are never returned.")


def build_router(core) -> APIRouter:
    r = APIRouter(prefix="/api")
    admin = Depends(core.auth.dependency("admin"))
    viewer = Depends(core.auth.dependency("viewer"))          # admin, member or read-only API token

    # ---------------- public ----------------
    @r.get("/health")
    async def health():
        return {"status": "ok"}

    @r.get("/auth/status")
    async def auth_status(request: Request):
        p = core.auth.principal(request)
        return {"authenticated": p is not None, "username": p.username if p else None, "role": p.role if p else None, "kind": p.kind if p else None,
                "csrf": p.csrf if p else None, "setup_required": not await core.auth.has_admin(), "sso": await core.oidc.public_info()}

    @r.post("/setup")
    async def setup(body: SetupBody, response: Response):
        if await core.auth.has_admin():
            raise HTTPException(409, "already set up")
        if core.auth.setup_token and not hmac.compare_digest((body.setup_token or "").encode(), core.auth.setup_token.encode()):
            raise HTTPException(403, "invalid setup token (see server log)")
        try:
            await core.auth.create_admin(body.username, body.password)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        core.auth.setup_token = None
        return {"ok": True}

    @r.post("/auth/login")
    async def login(body: Credentials, request: Request, response: Response):
        ip = core.auth.ip(request)
        if core.auth.login_throttle.blocked(ip):
            raise HTTPException(429, "too many attempts; try later")
        if not await core.auth.verify(body.username, body.password):
            core.auth.login_throttle.fail(ip)
            raise HTTPException(401, "invalid credentials")
        tok, csrf = core.auth.issue(body.username)
        response.set_cookie(COOKIE, tok, max_age=SESSION_MAX_AGE, httponly=True, samesite="strict",
                            secure=core.boot.cookie_secure, path="/")
        return {"csrf": csrf}

    @r.post("/auth/logout")
    async def logout(request: Request, response: Response):
        await core.auth.revoke(request.cookies.get(COOKIE))        # the token itself stops working, not just this browser's copy
        response.delete_cookie(COOKIE, path="/")
        return {"ok": True}

    @r.post("/auth/password")
    async def change_password(body: PasswordChange, request: Request, response: Response, p: Principal = admin):
        if p.kind != "local":
            raise HTTPException(403, "this account signs in through your identity provider; change its password there")
        ip = core.auth.ip(request)
        if core.auth.login_throttle.blocked(ip):
            raise HTTPException(429, "too many attempts; try later")
        try:
            await core.auth.change_password(p.username, body.current_password, body.new_password)
        except ValueError as e:
            core.auth.login_throttle.fail(ip)
            raise HTTPException(422, str(e)) from None
        response.delete_cookie(COOKIE, path="/")                    # every session, including this one, is now signed out
        return {"ok": True}

    # ---------------- locations ----------------
    @r.get("/locations")
    async def get_locations(p: Principal = viewer):
        return [l.model_dump() for l in await core.locations.list()]

    @r.put("/locations")
    async def put_locations(locs: list[Location], p: Principal = admin):
        try:
            await core.locations.replace(locs, p.username)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        return [l.model_dump() for l in locs]

    # ---------------- sources ----------------
    @r.get("/sources")
    async def list_sources(p: Principal = admin):
        status = await core.scheduler.status()
        out = []
        for name, cls in sorted(core.registry.all().items()):
            out.append({"meta": cls.meta.model_dump(mode="json"),
                        "schema": cls.ConfigModel.model_json_schema(),
                        "secret_fields": secret_fields(cls.ConfigModel),
                        "can_suggest": cls.suggest.__func__ is not Source.suggest.__func__,
                        "settings": await core.source_settings.view(name),
                        "health": status.get(name)})
        return out

    @r.put("/sources/{name}")
    async def update_source(name: str, body: SourceUpdate, p: Principal = admin):
        if name not in core.registry.all():
            raise HTTPException(404, "unknown source")
        try:
            await core.source_settings.update(
                name, enabled=body.enabled,
                interval_s=None if body.clear_interval else (body.interval_s if body.interval_s else ...),
                config=body.config, secrets=body.secrets, actor=p.username)
        except SourceSettingsError as e:
            raise HTTPException(422, str(e)) from None
        await core.scheduler.reload(name)   # only this source restarts
        return {"settings": await core.source_settings.view(name),
                "health": (await core.scheduler.status()).get(name)}

    @r.get("/sources/{name}/suggest")
    async def suggest_source(name: str, location_id: str | None = None, p: Principal = admin):
        """Settings that fit a location (nearest radar, station, state feed...). Nothing is saved: the UI offers them."""
        cls = core.registry.all().get(name)
        if cls is None:
            raise HTTPException(404, "unknown source")
        locs = [l.model_dump() for l in await core.locations.list()]
        if location_id:
            locs = [l for l in locs if l["id"] == location_id]
        if not locs:
            raise HTTPException(422, "add a location first (Settings), then ask again")
        async def _noop(_items): return None
        async def _locs(): return locs
        async with httpx.AsyncClient(headers={"User-Agent": core.boot.user_agent}, timeout=30, follow_redirects=True) as http:
            ctx = SourceContext(http=http, emit=_noop, health=Health(), locations=_locs, logger=logging.getLogger(f"stormtracker.suggest.{name}"),
                                data_dir=core.boot.data_dir, extras={"geocoder_url": await _geocoder()})
            try:
                s = await cls.suggest(locs[0], ctx, (await core.source_settings.view(name)).get("config") or {})
            except (httpx.HTTPError, OSError, ValueError, RuntimeError) as e:
                raise HTTPException(502, f"couldn't reach the service that lists the options ({type(e).__name__}); try again shortly") from None
        if s is None:
            raise HTTPException(404, "this source has nothing to suggest")
        return {"location": {"id": locs[0]["id"], "name": locs[0]["name"]}, **s.model_dump()}

    @r.get("/status")
    async def status(p: Principal = admin):
        return await core.scheduler.status()

    @r.get("/audit")
    async def audit(p: Principal = admin):
        return await core.store.audit()

    # ---------------- data ----------------
    def _when(v: str | None, name: str) -> datetime | None:
        if not v:
            return None
        try:
            return ensure_utc(datetime.fromisoformat(v.replace("Z", "+00:00")))
        except ValueError:
            raise HTTPException(422, f"{name} must be an ISO 8601 timestamp") from None

    @r.get("/observations")
    async def observations(source: str | None = None, station_id: str | None = None, limit: int = 100,
                           since: str | None = None, until: str | None = None,
                           order: Literal["asc", "desc"] = "desc", p: Principal = viewer):
        """Newest ``limit`` rows (<=20000) inside [since, until]; ``order`` only changes how they are returned."""
        q = select(ObservationRow).order_by(ObservationRow.ts.desc()).limit(min(max(limit, 1), 20000))
        if source:
            q = q.where(ObservationRow.source == source)
        if station_id:
            q = q.where(ObservationRow.station_id == station_id)
        if t := _when(since, "since"):
            q = q.where(ObservationRow.ts >= t)
        if t := _when(until, "until"):
            q = q.where(ObservationRow.ts <= t)
        async with core.db.session() as s:
            rows = list((await s.execute(q)).scalars())
        if order == "asc":
            rows.reverse()
        return [dict(source=o.source, station_id=o.station_id, lat=o.lat, lon=o.lon,
                     ts=o.ts.isoformat(), values=o.values) for o in rows]

    @r.get("/stations")
    async def stations(p: Principal = viewer):
        """Every (source, station) that has reported, with its newest report time and wind (for the map's arrows)."""
        q = (select(ObservationRow.source, ObservationRow.station_id, func.max(ObservationRow.ts),
                    func.count(ObservationRow.id))
             .group_by(ObservationRow.source, ObservationRow.station_id))
        async with core.db.session() as s:
            rows = (await s.execute(q)).all()
            out = []
            for src, sid, last, n in rows:
                o = (await s.execute(select(ObservationRow).where(
                    ObservationRow.source == src, ObservationRow.station_id == sid,
                    ObservationRow.ts == last))).scalars().first()
                v = o.values or {}
                wind = {k: v.get(k) for k in ("wind_speed_ms", "wind_gust_ms", "wind_dir_deg") if v.get(k) is not None}
                out.append({"source": src, "station_id": sid, "lat": o.lat, "lon": o.lon,
                            "last_ts": last.isoformat() if last else None, "count": n, "wind": wind or None})
        return sorted(out, key=lambda x: x["last_ts"] or "", reverse=True)

    @r.get("/events")
    async def events(source: str | None = None, event_type: str | None = None, limit: int = 100,
                     since: str | None = None, p: Principal = viewer):
        q = select(EventRow).order_by(EventRow.ts.desc()).limit(min(max(limit, 1), 1000))
        if source:
            q = q.where(EventRow.source == source)
        if event_type:
            q = q.where(EventRow.event_type == event_type)
        if t := _when(since, "since"):
            q = q.where(EventRow.ts >= t)
        async with core.db.session() as s:
            return [dict(source=e.source, event_type=e.event_type, external_id=e.external_id,
                         ts=e.ts.isoformat(), lat=e.lat, lon=e.lon, geometry=e.geometry,
                         expires=e.expires.isoformat() if e.expires else None, properties=e.properties)
                    for e in (await s.execute(q)).scalars()]

    @r.get("/forecast")
    async def model_forecast(p: Principal = viewer):
        """The latest model forecast (Open-Meteo...) per location: hourly convective ingredients plus a 12 h summary."""
        async with core.db.session() as s:
            rows = (await s.execute(select(EventRow).where(EventRow.external_id.like("forecast:%")).order_by(EventRow.ts.desc()))).scalars().all()
        seen, out = set(), []
        for e in rows:
            pr = e.properties or {}
            if pr.get("kind") != "model_forecast" or pr.get("location_id") in seen:
                continue
            seen.add(pr.get("location_id"))
            out.append({k: pr.get(k) for k in ("location_id", "model", "issued", "attribution", "summary", "hours")})
        return out

    @r.get("/wind")
    async def model_wind(p: Principal = viewer):
        """The model's current 10 m wind at a grid of points around home (for the map), per location; empty when not fetched."""
        async with core.db.session() as s:
            rows = (await s.execute(select(EventRow).where(EventRow.external_id.like("windfield:%")).order_by(EventRow.ts.desc()))).scalars().all()
        seen, out = set(), []
        for e in rows:
            pr = e.properties or {}
            if pr.get("kind") != "model_wind_field" or pr.get("location_id") in seen:
                continue
            seen.add(pr.get("location_id"))
            out.append({k: pr.get(k) for k in ("location_id", "model", "issued", "attribution", "points")})
        return out

    # ---------------- UI preferences (non-secret, stored in the DB like every other setting) ----------------
    @r.get("/ui")
    async def get_ui(p: Principal = viewer):
        return {**UI_DEFAULTS, **await core.store.get("ui", {})}

    @r.put("/ui")
    async def put_ui(body: UiSettings, p: Principal = admin):
        await core.store.set("ui", body.model_dump(), actor=p.username)
        core.ui_cache = None                                   # the page security policy follows the tile address
        return body.model_dump()

    async def _geocoder() -> str:
        return (await core.store.get("ui", {})).get("geocoder_url", geocode.DEFAULT_URL)

    @r.get("/geocode")
    async def geocode_search(q: str = Query(min_length=3, max_length=200), p: Principal = admin):
        """Address search for placing a location. Runs only when asked (a button), never as you type."""
        async with httpx.AsyncClient(headers={"User-Agent": core.boot.user_agent}, follow_redirects=True) as http:
            try:
                return {"results": await geocode.search(http, await _geocoder(), q)}
            except geocode.GeocodeError as e:
                raise HTTPException(502, str(e)) from None

    return r
