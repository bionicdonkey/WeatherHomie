"""A small, read-only MCP (Model Context Protocol) server over Streamable HTTP, so an AI agent can use StormTracker as its weather source.

Hand-written on purpose: it only offers tools, holds no sessions and needs no SDK. It speaks both protocol eras, because
agent clients are split:

* modern (2026-07-28): every request carries its protocol version in ``params._meta`` and in the ``MCP-Protocol-Version``,
  ``Mcp-Method`` and ``Mcp-Name`` headers, which are checked against the body; ``server/discover`` describes the server.
* legacy (2025-03-26 to 2025-11-25): the ``initialize`` handshake. No session id is issued; nothing needs one.

Every tool only reads data StormTracker already holds (see ``summary.py``). Nothing here can change a setting, read a secret or
send an alert. Access needs a read-only API token (Settings → Access), sent as ``Authorization: Bearer``.
"""
from __future__ import annotations

import base64
import binascii
import json
import time
from collections import defaultdict, deque
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from . import __version__, summary
from .timeutil import utcnow

MODERN = "2026-07-28"
LEGACY = ("2025-11-25", "2025-06-18", "2025-03-26")
SUPPORTED = (MODERN, *LEGACY)
VER = "io.modelcontextprotocol/protocolVersion"
MAX_BODY = 64 * 1024
CALLS_PER_MIN = 120
SERVER_INFO = {"name": "stormtracker", "title": "StormTracker", "version": __version__}
INSTRUCTIONS = ("Weather for one home location in Australia: a personal weather station, BOM observations and warnings, radar-based rain and storm "
                "ETAs, lightning and a model forecast. All tools are read-only. Every number has a unit. Estimates from radar or a model are not "
                "official warnings; the Bureau of Meteorology is the authority for severe weather warnings, so mention any warning in force. "
                "Timestamps are UTC; the home is on Australia/Sydney time. Start with get_weather_now for a complete picture.")

_NO_ARGS = {"type": "object", "additionalProperties": False}
_RO = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
TOOLS: list[dict[str, Any]] = [
    {"name": "get_weather_now", "title": "Weather now (everything)", "inputSchema": _NO_ARGS, "annotations": _RO,
     "description": "The complete current picture for home: conditions, rain and storm outlook, lightning, warnings in force, the next hours of model forecast and any active StormTracker alerts."},
    {"name": "get_current_conditions", "title": "Current conditions", "inputSchema": _NO_ARGS, "annotations": _RO,
     "description": "Latest readings from up to three stations (your own first, then the nearest BOM stations): temperature, humidity, pressure, wind, gusts, rain. Each has an age and a stale flag."},
    {"name": "get_rain_outlook", "title": "Rain and storm outlook", "inputSchema": _NO_ARGS, "annotations": _RO,
     "description": "Will it rain at home, when, how hard and how sure the radar nowcast is, plus the nearest tracked storm cells and how they move."},
    {"name": "get_warnings", "title": "Weather warnings", "inputSchema": _NO_ARGS, "annotations": _RO,
     "description": "BOM warnings currently in force (flagging those matching the home area) and the active StormTracker alerts."},
    {"name": "get_lightning", "title": "Lightning", "inputSchema": _NO_ARGS, "annotations": _RO,
     "description": "Strikes in the last hour from the station's lightning sensor and any connected feed: count, nearest distance, latest time."},
    {"name": "get_forecast", "title": "Model forecast", "inputSchema": _NO_ARGS, "annotations": _RO,
     "description": "The weather model's next 12 hours for home: rain chance, gusts, temperature and storm ingredients (CAPE, lifted index, shear)."},
    {"name": "get_observation_history", "title": "Recent station history", "annotations": _RO,
     "description": "Recent readings from one station, thinned to at most 60 points: temperature, humidity, pressure, wind, gusts and rain. Use it for questions like how much rain fell or how the wind changed.",
     "inputSchema": {"type": "object", "additionalProperties": False, "properties": {
         "hours": {"type": "integer", "minimum": 1, "maximum": 48, "default": 6, "description": "how far back to look"},
         "station_id": {"type": "string", "description": "a station_id from get_current_conditions; default is your own station"}}}},
]
_SECTIONS = {
    "get_weather_now": None,
    "get_current_conditions": ("location", "conditions"),
    "get_rain_outlook": ("location", "rain", "storm_outlook"),
    "get_warnings": ("warnings", "alerts"),
    "get_lightning": ("lightning",),
    "get_forecast": ("forecast",),
}
_calls: dict[str, deque[float]] = defaultdict(deque)


class RpcError(Exception):
    def __init__(self, code: int, message: str, status: int = 200, data: Any = None):
        self.code, self.message, self.status, self.data = code, message, status, data


def _err(id_: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    e: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        e["data"] = data
    return {"jsonrpc": "2.0", "id": id_, "error": e}


def _decode_header(v: str) -> str:
    if v.startswith("=?base64?") and v.endswith("?="):
        try:
            return base64.b64decode(v[9:-2], validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            raise RpcError(-32020, "Header mismatch: Mcp-Name is not valid base64", 400) from None
    return v


def _check_modern_headers(msg: dict[str, Any], headers: Any) -> None:
    method, params = msg.get("method"), msg.get("params") or {}
    if headers.get("mcp-protocol-version") != params["_meta"][VER]:
        raise RpcError(-32020, "Header mismatch: MCP-Protocol-Version does not match the request's protocol version", 400)
    if headers.get("mcp-method") != method:
        raise RpcError(-32020, "Header mismatch: Mcp-Method is missing or does not match the request method", 400)
    if method in ("tools/call", "resources/read", "prompts/get"):
        want = params.get("name") if method != "resources/read" else params.get("uri")
        got = headers.get("mcp-name")
        if got is None or _decode_header(got) != want:
            raise RpcError(-32020, "Header mismatch: Mcp-Name is missing or does not match the request", 400)


def _tools_result(tools: list[dict[str, Any]], modern: bool) -> dict[str, Any]:
    out: dict[str, Any] = {"tools": tools}
    if modern:
        out.update(resultType="complete", ttlMs=300000, cacheScope="private")
    return out


def _tool_text(payload: Any, modern: bool, error: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {"content": [{"type": "text", "text": json.dumps(payload, separators=(",", ":"), default=str)}], "isError": error}
    if not error and isinstance(payload, dict):
        out["structuredContent"] = payload
    if modern:
        out["resultType"] = "complete"
    return out


async def _history(core, hours: int, station_id: str | None) -> dict[str, Any]:
    now = utcnow()
    stations = await summary.conditions(core, (await summary._home(core)), now, limit=10)
    pick = next((s for s in stations if s["station_id"] == station_id), None) if station_id else (stations[0] if stations else None)
    if pick is None:
        return {"error": "no such station; use a station_id from get_current_conditions" if station_id else "no station has reported yet"}
    from datetime import timedelta

    from sqlalchemy import select

    from .storage.tables import ObservationRow
    async with core.db.session() as s:
        rows = (await s.execute(select(ObservationRow).where(
            ObservationRow.source == pick["source"], ObservationRow.station_id == pick["station_id"], ObservationRow.ts >= now - timedelta(hours=hours))
            .order_by(ObservationRow.ts))).scalars().all()
    step = max(1, -(-len(rows) // 60))
    keep = ("temp_c", "humidity_pct", "pressure_hpa", "wind_speed_kmh", "wind_gust_kmh", "wind_dir_deg", "rain_rate_mmh", "rain_daily_mm")
    pts = []
    for o in rows[::step]:
        v = summary._clean(o.values)
        pts.append({"t": o.ts.isoformat(), **{k: v[k] for k in keep if k in v}})
    if rows and pts and pts[-1]["t"] != rows[-1].ts.isoformat():
        v = summary._clean(rows[-1].values)
        pts.append({"t": rows[-1].ts.isoformat(), **{k: v[k] for k in keep if k in v}})
    return {"station_id": pick["station_id"], "source": pick["source"], "hours": hours, "points": pts, "point_count": len(pts),
            "units": {k: summary.UNITS[k] for k in keep}, "authority": summary.AUTHORITY}


async def call_tool(core, params: dict[str, Any], modern: bool) -> dict[str, Any]:
    name, args = params.get("name"), params.get("arguments") or {}
    if name not in {t["name"] for t in TOOLS}:
        raise RpcError(-32602, f"Unknown tool: {name}")
    try:
        if name == "get_observation_history":
            hours = args.get("hours", 6)
            sid = args.get("station_id")
            if isinstance(hours, bool) or not isinstance(hours, int) or not 1 <= hours <= 48 or (sid is not None and (not isinstance(sid, str) or len(sid) > 100)):
                return _tool_text("hours must be a whole number from 1 to 48, and station_id a short string", modern, error=True)
            data = await _history(core, hours, sid)
        else:
            data = await summary.build(core, _SECTIONS[name])
    except Exception as e:  # noqa: BLE001 - a failed read is a tool error the model can report, not a server crash
        return _tool_text(f"Couldn't read the weather data ({type(e).__name__}).", modern, error=True)
    return _tool_text(data, modern)


async def handle(core, msg: Any, headers: Any, who: str) -> tuple[int, Any]:
    """One JSON-RPC message -> (HTTP status, JSON body or None for 202)."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return 400, _err(None, -32600, "Invalid Request")
    mid, method = msg.get("id"), msg.get("method")
    if not isinstance(method, str):                                    # a response from the client: nothing to do
        return 202, None
    params = msg.get("params")
    if params is not None and not isinstance(params, dict):
        return 400, _err(mid, -32602, "params must be an object")
    meta = (params or {}).get("_meta")
    modern = isinstance(meta, dict) and VER in meta
    try:
        hv = headers.get("mcp-protocol-version")
        ver = meta[VER] if modern else hv
        if modern:
            if not isinstance(ver, str) or ver not in SUPPORTED:
                raise RpcError(-32022, "Unsupported protocol version", 400, {"supported": list(SUPPORTED), "requested": ver})
            _check_modern_headers(msg, headers)
        elif hv is not None and hv not in SUPPORTED:
            raise RpcError(-32022, "Unsupported protocol version", 400, {"supported": list(SUPPORTED), "requested": hv})

        if mid is None:                                                 # a notification (e.g. notifications/initialized)
            return 202, None
        if method == "ping":
            return 200, {"jsonrpc": "2.0", "id": mid, "result": {}}
        if method == "initialize":
            asked = (params or {}).get("protocolVersion")
            use = asked if asked in LEGACY else LEGACY[0]
            return 200, {"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": use, "capabilities": {"tools": {"listChanged": False}},
                                                                  "serverInfo": SERVER_INFO, "instructions": INSTRUCTIONS}}
        if method == "server/discover":
            return 200, {"jsonrpc": "2.0", "id": mid, "result": {
                "resultType": "complete", "supportedVersions": list(SUPPORTED), "capabilities": {"tools": {"listChanged": False}},
                "_meta": {"io.modelcontextprotocol/serverInfo": SERVER_INFO}, "instructions": INSTRUCTIONS, "ttlMs": 300000, "cacheScope": "private"}}
        if method == "tools/list":
            return 200, {"jsonrpc": "2.0", "id": mid, "result": _tools_result(TOOLS, modern)}
        if method == "tools/call":
            if not _allow(who):
                return 200, {"jsonrpc": "2.0", "id": mid, "result": _tool_text("Too many calls; wait a minute and try again.", modern, error=True)}
            return 200, {"jsonrpc": "2.0", "id": mid, "result": await call_tool(core, params or {}, modern)}
        raise RpcError(-32601, f"Method not found: {method}", 404 if modern else 200)
    except RpcError as e:
        return e.status, _err(mid, e.code, e.message, e.data)


def _allow(who: str) -> bool:
    q, now = _calls[who], time.monotonic()
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= CALLS_PER_MIN:
        return False
    q.append(now)
    return True


def build_mcp_router(core) -> APIRouter:
    r = APIRouter()

    @r.api_route("/mcp", methods=["POST", "GET", "DELETE"], include_in_schema=False)
    async def endpoint(request: Request):
        if request.method != "POST":
            return Response(status_code=405, headers={"Allow": "POST"})       # no standalone SSE stream and no sessions
        origin = request.headers.get("origin")
        if origin and urlparse(origin).netloc != request.headers.get("host"):
            raise HTTPException(403, "origin not allowed")                    # DNS-rebinding guard from the MCP transport rules
        try:
            p = await core.auth.require(request, "viewer", token_may_write=True)
        except HTTPException as e:
            if e.status_code == 401:
                raise HTTPException(401, e.detail, headers={"WWW-Authenticate": 'Bearer realm="stormtracker"'}) from None
            raise
        if p.kind != "token":
            raise HTTPException(403, "the MCP endpoint needs an API token (Settings, Access)")
        raw = await request.body()
        if len(raw) > MAX_BODY:
            raise HTTPException(413, "request too large")
        try:
            body = json.loads(raw)
        except ValueError:
            return JSONResponse(_err(None, -32700, "Parse error"), status_code=400)
        msgs = body if isinstance(body, list) else [body]
        if not msgs or len(msgs) > 20:
            return JSONResponse(_err(None, -32600, "Invalid Request"), status_code=400)
        results = [await handle(core, m, request.headers, p.username) for m in msgs]
        out = [b for _, b in results if b is not None]
        if not out:
            return Response(status_code=202)
        status = results[0][0] if len(msgs) == 1 else 200
        return JSONResponse(out if isinstance(body, list) else out[0], status_code=status)

    return r
