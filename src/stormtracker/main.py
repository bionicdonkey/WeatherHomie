from __future__ import annotations

import asyncio
import logging
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .alerts.base import AlertOutput
from .alerts.service import AlertService
from .api.access import build_access_router
from .api.alerts import build_alerts_router
from .api.backup import build_backup_router
from .api.homeassistant import build_ha_router
from .api.nowcast import build_nowcast_router
from .api.radar import build_radar_router
from .api.routes import build_router
from .auth import Auth
from .backup import Backups
from .bootstrap import Bootstrap
from .ha_mqtt import HomeAssistantMqtt
from .locations import Locations
from .mcp import build_mcp_router
from .oidc import Oidc
from .nowcast.service import NowcastService
from .plugins import PluginRegistry
from .scheduler import Scheduler
from .settings import SecretBox, SettingsStore
from .settings.sources import SourceSettings
from .storage.db import Database, Repository
from .storage.frames import FrameStore

log = logging.getLogger("stormtracker")
DEFAULT_RETENTION = {"frame_days": 2.0, "obs_days": 90.0, "event_days": 90.0}


@dataclass
class Core:
    boot: Bootstrap
    db: Database
    box: SecretBox
    store: SettingsStore
    registry: PluginRegistry
    source_settings: SourceSettings
    locations: Locations
    repo: Repository
    scheduler: Scheduler
    auth: Auth
    nowcast: Any = None
    alert_registry: Any = None
    alert_outputs: Any = None
    alerts: Any = None
    oidc: Any = None
    ha: Any = None
    backups: Any = None
    ui_cache: Any = None        # (tile origin for the page security policy), reset when UI settings change


_ORIGIN = re.compile(r"^http://[A-Za-z0-9.-]+(:\d{1,5})?$")


def tile_http_origin(tile_url: str) -> str:
    """``http://host:port`` of a plain-http tile address, or "" (https is already allowed, and anything odd is ignored)."""
    try:
        u = urlsplit((tile_url or "").replace("{", "x").replace("}", "x"))
        origin = f"{u.scheme}://{u.netloc}" if u.scheme == "http" and "@" not in u.netloc else ""
    except ValueError:
        return ""
    return origin if _ORIGIN.fullmatch(origin) else ""


def build_core(boot: Bootstrap, registry: PluginRegistry | None = None) -> Core:
    boot.data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(boot.database_url)
    box = SecretBox(boot.master_key)
    store = SettingsStore(db, box)
    if registry is None:
        registry = PluginRegistry()
        registry.discover()
    locs = Locations(store)
    repo = Repository(db, FrameStore(boot.data_dir / "radar"))
    ss = SourceSettings(store, registry)

    async def loc_dicts() -> list[dict[str, Any]]:
        return [l.model_dump() for l in await locs.list()]

    sched = Scheduler(registry, ss, repo, user_agent=boot.user_agent, locations_provider=loc_dicts, data_dir=boot.data_dir)
    core = Core(boot, db, box, store, registry, ss, locs, repo, sched, Auth(boot, db, box.session_secret, store))
    core.oidc = Oidc(core)
    core.ha = HomeAssistantMqtt(core)
    core.nowcast = NowcastService(core)
    core.alert_registry = PluginRegistry(base=AlertOutput, package="stormtracker.alerts.outputs")
    core.alert_registry.discover()
    core.alert_outputs = SourceSettings(store, core.alert_registry, prefix="alert_output")
    core.alerts = AlertService(core, core.alert_registry, core.alert_outputs, user_agent=boot.user_agent)
    core.backups = Backups(core)
    return core


async def _retention_loop(core: Core) -> None:
    while True:
        try:
            cfg = {**DEFAULT_RETENTION, **await core.store.get("retention", {})}
            res = await core.repo.purge(frame_days=cfg["frame_days"], obs_days=cfg["obs_days"],
                                        event_days=cfg["event_days"])
            res["alerts"] = await core.alerts.prune(cfg["event_days"])
            log.info("retention purge: %s", res)
        except Exception:
            log.exception("retention purge failed")
        await asyncio.sleep(3600)


def create_app(boot: Bootstrap | None = None, registry: PluginRegistry | None = None) -> FastAPI:
    boot = boot or Bootstrap.from_env()
    core = build_core(boot, registry)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await core.db.create_all()
        await core.store.init()          # raises if MASTER_KEY is wrong
        await core.auth.init()
        if core.auth.setup_token:
            log.warning("First-run setup token (needed for POST /api/setup): %s", core.auth.setup_token)
        await core.scheduler.start()
        await core.nowcast.start()
        await core.alerts.start()
        await core.ha.start()
        task = asyncio.create_task(_retention_loop(core))
        yield
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)   # let an in-flight purge unwind before the DB goes away
        await core.ha.stop()
        await core.alerts.stop()
        await core.nowcast.stop()
        await core.scheduler.stop()
        await core.db.dispose()

    app = FastAPI(title="StormTracker", lifespan=lifespan)
    app.state.core = core

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        # FastAPI's default repeats the submitted value in the reply, which would echo a passphrase or client secret back
        return JSONResponse({"detail": [{"loc": e.get("loc"), "msg": e.get("msg"), "type": e.get("type")} for e in exc.errors()]}, status_code=422)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault("Permissions-Policy", "geolocation=(), camera=(), microphone=(), payment=()")
        if request.url.path.startswith("/api/") or request.url.path.startswith("/ingest/"):
            resp.headers.setdefault("Cache-Control", "no-store")          # readings and settings are not for shared caches
        if not request.url.path.startswith(("/docs", "/redoc")):    # FastAPI's docs pages load their scripts from a CDN
            # Self-hosted UI: scripts, fonts and XHR only from here. Images may come from https (the map tiles are configurable),
            # and from the one plain-http origin the admin saved as the tile address (a tile server on the home network).
            if core.ui_cache is None:
                ui = await core.store.get("ui", {})
                core.ui_cache = (" ".join(dict.fromkeys(o for o in (tile_http_origin(ui.get("tile_url", "")), tile_http_origin(ui.get("tile_url_dark", ""))) if o)),)
            extra = f" {core.ui_cache[0]}" if core.ui_cache[0] else ""
            resp.headers.setdefault("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                                    f"img-src 'self' data: blob: https:{extra}; font-src 'self'; connect-src 'self'; object-src 'none'; "
                                    "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
        return resp
    app.include_router(build_router(core))
    app.include_router(build_radar_router(core))
    app.include_router(build_nowcast_router(core))
    app.include_router(build_alerts_router(core))
    app.include_router(build_access_router(core))
    app.include_router(build_ha_router(core))
    app.include_router(build_backup_router(core))
    app.include_router(build_mcp_router(core))

    @app.api_route("/ingest/{name}", methods=["GET", "POST"], include_in_schema=False)
    @app.api_route("/ingest/{name}/{rest:path}", methods=["GET", "POST"], include_in_schema=False)
    async def ingest(name: str, request: Request, rest: str = ""):
        src = core.scheduler.running(name)
        if src is None:
            raise HTTPException(404, "source not enabled")
        ip = core.auth.ip(request)
        if core.auth.ingest_throttle.blocked(ip):
            raise HTTPException(429, "too many failed attempts; try again later")
        try:
            return await src.handle_push(request)
        except NotImplementedError:
            raise HTTPException(404, "source does not accept pushes") from None
        except HTTPException as e:
            if e.status_code == 403:                          # a wrong token or device: count it, so guessing gets nowhere
                core.auth.ingest_throttle.fail(ip)
            raise

    web = Path(__file__).parent / "web"
    if any(web.glob("*.html")):
        app.mount("/", StaticFiles(directory=web, html=True), name="web")
    return app
