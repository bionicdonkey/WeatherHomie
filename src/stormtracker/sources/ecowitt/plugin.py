"""Ecowitt local station source.

Two ways in, both normalised to the common schema:

* **Push** (works with every Ecowitt console/gateway, incl. EasyWeatherPro consoles): set the device's
  "Customized" upload to this server. Protocol "Ecowitt" (HTTP POST form) or "Wunderground" (HTTP GET
  query) are both accepted. Path: ``/ingest/ecowitt/<push token>`` (the token is a secret you set here).
  The same endpoint accepts requests **forwarded** by another receiver (reverse proxy mirror, n8n, ...),
  with the token either in the path or in an ``X-Stormtracker-Token`` / ``Authorization: Bearer`` header.
* **Forwarding**: ``forward_urls`` relays each accepted push, byte-for-byte, to other receivers (e.g.
  Home Assistant's Ecowitt webhook) so the single Customized upload slot can feed several systems.
* **Poll** (gateways only, e.g. GW1100/GW2000/GW3000): set ``gateway_host`` and the plugin reads the
  gateway's local HTTP API ``/get_livedata_info``. Not all consoles implement it (the HP25xx
  EasyWeatherPro consoles do not), and the binary TCP API is unmaintained upstream, so it is not used.
"""
from __future__ import annotations

import asyncio
import hmac
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from fastapi import HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field, field_validator

from ...models import Event, Normalised, Observation
from ...plugins import Health, HealthState, Licence, RateLimit, Source, SourceMeta, secret_field
from ...timeutil import utcnow
from .mapping import convert_livedata, convert_push_fields, _num

MAX_BODY = 64 * 1024
STALE_AFTER = timedelta(minutes=15)
FORWARD_TIMEOUT_S = 5
FORWARD_MAX_PENDING = 5          # per target; beyond this, pushes are dropped for that target (counted)
FORWARDED_HEADER = "x-stormtracker-forwarded"   # set on relayed requests; refused on the way in (loop guard)


def parse_forward_urls(text: str | None) -> tuple[list[tuple[str, str]], int]:
    """One URL per line (blank lines and ``#`` comments ignored) -> ([(url, safe label)], invalid count).

    The label is ``host:port`` only: URLs can carry secrets (e.g. a Home Assistant webhook id), so the
    path/query must never reach logs, health or the API.
    """
    out: list[tuple[str, str]] = []
    bad = 0
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            u = urlsplit(line)
            host, port = u.hostname, u.port
        except ValueError:
            bad += 1
            continue
        if u.scheme not in ("http", "https") or not host:
            bad += 1
            continue
        label = f"{host}:{port or (443 if u.scheme == 'https' else 80)}"
        out.append((line, f"{len(out) + 1}:{label}"))
    return out, bad


class EcowittConfig(BaseModel):
    push_token: str | None = secret_field(
        "Random string used in the device's upload path: /ingest/ecowitt/<token>. Pushes are refused until set.",
        generate=True)
    allowed_passkey: str | None = secret_field(
        "Optional: only accept pushes whose PASSKEY (Ecowitt protocol) matches this value.")
    station_id: str = Field("ecowitt", pattern=r"^[A-Za-z0-9_.-]{1,40}$", description="id stored with observations")
    location_id: str | None = Field(None, description="id of the configured location this station is at (default: first)")
    forward_urls: str | None = secret_field(
        "Relay every accepted push, unchanged, to these URLs (one per line), e.g. Home Assistant's Ecowitt "
        "webhook. Stored encrypted because URLs can contain secrets. A slow or failing target never affects ingestion.",
        multiline=True)
    gateway_host: str | None = Field(None, description="gateway IP/host to poll via HTTP (omit for push-only)")
    max_clock_skew_s: int = Field(3600, ge=60, description="device timestamp further than this from now is replaced by receive time")

    @field_validator("gateway_host")
    @classmethod
    def _host(cls, v: str | None) -> str | None:
        if v and not re.fullmatch(r"[A-Za-z0-9.\-]{1,253}(:\d{1,5})?", v):
            raise ValueError("gateway_host must be a hostname or IP, optionally with :port (no scheme/path)")
        return v or None


def parse_dateutc(v: str | None) -> datetime | None:
    """Ecowitt/WU ``dateutc`` is UTC 'YYYY-MM-DD HH:MM:SS' (or 'now')."""
    if not v or v.strip().lower() == "now":
        return None
    try:
        return datetime.strptime(v.strip().replace("%20", " ").replace("+", " "), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    except ValueError:
        return None


class Ecowitt(Source):
    meta = SourceMeta(
        name="ecowitt",
        category="observations",
        description="Your own Ecowitt station: customized-upload push (Ecowitt or Wunderground protocol) "
                    "and optional gateway HTTP polling. Includes the lightning sensor if fitted.",
        poll_interval_s=30,
        rate_limit=RateLimit(max_requests=2, period_s=30),
        licence=Licence(type="open", notes="Your own station's data, collected on your LAN."),
    )
    ConfigModel = EcowittConfig
    cfg: EcowittConfig

    def __init__(self, cfg, ctx):
        super().__init__(cfg, ctx)
        self._last_push: datetime | None = None
        self._targets, self._bad_forward = parse_forward_urls(cfg.forward_urls)
        self._fwd: dict[str, dict[str, Any]] = {
            label: {"ok": 0, "failed": 0, "dropped": 0, "pending": 0, "last_status": None, "last_error": None}
            for _, label in self._targets}
        self._tasks: set[asyncio.Task] = set()
        if self._targets or self._bad_forward:
            self.ctx.state.detail["forward"] = {"targets": self._fwd, "invalid_lines": self._bad_forward}

    async def stop(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    # ---- polling (gateways only) -------------------------------------------------------------
    @property
    def polling(self) -> bool:
        return bool(self.cfg.gateway_host)

    async def fetch(self) -> dict[str, Any]:
        if not self.cfg.gateway_host:
            raise RuntimeError("gateway_host not configured")
        r = await self.ctx.http.get(f"http://{self.cfg.gateway_host}/get_livedata_info", timeout=10)
        r.raise_for_status()
        return {"kind": "livedata", "json": r.json(), "received": utcnow().isoformat(),
                "location": await self._location()}

    # ---- conversion (pure) -----------------------------------------------------------------
    def normalise(self, raw: dict[str, Any]) -> list[Normalised]:
        received = datetime.fromisoformat(raw["received"])
        loc = raw["location"]
        items: list[Normalised] = []
        if raw["kind"] == "push":
            fields: dict[str, str] = raw["fields"]
            values = convert_push_fields(fields)
            ts = parse_dateutc(fields.get("dateutc"))
            if ts is None or abs(ts - received) > timedelta(seconds=self.cfg.max_clock_skew_s):
                ts = received
            lt = _num(fields.get("lightning_time"))
            if lt and lt > 0:
                items.append(Event(
                    source=self.meta.name, event_type="lightning", external_id=f"{self.cfg.station_id}:{int(lt)}",
                    ts=datetime.fromtimestamp(int(lt), UTC),
                    properties={"distance_km": values.get("lightning_distance_km"), "detected_by": "station_sensor",
                                "station_id": self.cfg.station_id, "station_lat": loc["lat"], "station_lon": loc["lon"]}))
        else:
            values = convert_livedata(raw["json"])
            ts = received.replace(microsecond=0)
        if values:
            items.insert(0, Observation(source=self.meta.name, station_id=self.cfg.station_id,
                                        lat=loc["lat"], lon=loc["lon"], ts=ts, values=values))
        return items

    async def _location(self) -> dict[str, Any]:
        return await self.location(self.cfg.location_id)

    # ---- push ----------------------------------------------------------------------------
    async def handle_push(self, request: Any) -> PlainTextResponse:
        token = self.cfg.push_token
        given = request.path_params.get("rest", "").strip("/").split("/")[0]   # tolerate suffixes the device appends
        if not given:   # forwarders (proxy/n8n/HA) that can't change the path may send the token in a header
            auth = request.headers.get("authorization", "")
            given = request.headers.get("x-stormtracker-token") or (auth[7:] if auth.lower().startswith("bearer ") else "")
        if not token or not hmac.compare_digest(given.encode(), token.encode()):
            raise HTTPException(403, "invalid token")
        if int(request.headers.get("content-length") or 0) > MAX_BODY:
            raise HTTPException(413, "body too large")
        body = await request.body() if request.method == "POST" else b""
        if len(body) > MAX_BODY:
            raise HTTPException(413, "body too large")
        fields: dict[str, str] = dict(request.query_params)
        if request.method == "POST":
            fields.update({k: str(v) for k, v in (await request.form()).items()})
        if self.cfg.allowed_passkey and not hmac.compare_digest(
                fields.get("PASSKEY", "").encode(), self.cfg.allowed_passkey.encode()):
            raise HTTPException(403, "unexpected device")
        self._forward(request, body)       # authenticated + well-formed: relay, independent of our own ingest
        try:
            loc = await self._location()
        except RuntimeError as e:
            raise HTTPException(503, str(e)) from None
        raw = {"kind": "push", "fields": fields, "received": utcnow().isoformat(), "location": loc}
        items = self.normalise(raw)
        if items:
            await self.ctx.emit(items)
        self._last_push = utcnow()
        h = self.ctx.state
        h.last_success, h.last_error, h.consecutive_failures = self._last_push, None, 0
        h.state = HealthState.OK
        h.detail.update(last_push=self._last_push.isoformat(), fields=len(fields),
                        model=fields.get("model") or fields.get("stationtype"))
        return PlainTextResponse("success")

    # ---- forwarding ------------------------------------------------------------------------
    def _forward(self, request: Any, body: bytes) -> None:
        """Fire-and-forget relay of the raw request to each configured target."""
        if not self._targets:
            return
        if request.headers.get(FORWARDED_HEADER):      # already relayed once: never relay again (loop guard)
            return
        headers = {FORWARDED_HEADER: "1"}
        if request.headers.get("content-type"):
            headers["content-type"] = request.headers["content-type"]
        query = request.url.query
        for url, label in self._targets:
            st = self._fwd[label]
            if st["pending"] >= FORWARD_MAX_PENDING:
                st["dropped"] += 1
                continue
            target = url + (("&" if "?" in url else "?") + query if query else "")
            st["pending"] += 1
            task = asyncio.create_task(self._send(request.method, target, body, headers, st))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _send(self, method: str, url: str, body: bytes, headers: dict[str, str], st: dict[str, Any]) -> None:
        try:
            r = await self.ctx.http.request(method, url, content=body or None, headers=headers,
                                            timeout=FORWARD_TIMEOUT_S)
            st["last_status"] = r.status_code
            if r.status_code < 400:
                st["ok"] += 1
                st["last_error"] = None
            else:
                st["failed"] += 1
                st["last_error"] = f"HTTP {r.status_code}"
        except asyncio.CancelledError:
            raise
        except Exception as e:   # noqa: BLE001 - type name only: httpx messages contain the URL
            st["failed"] += 1
            st["last_status"], st["last_error"] = None, type(e).__name__
        finally:
            st["pending"] -= 1

    async def health(self) -> Health:
        h = self.ctx.state
        if not self.cfg.gateway_host and h.state == HealthState.OK:
            last = self._last_push
            if last is None:
                h.state, h.detail["note"] = HealthState.STARTING, "waiting for the first push from the device"
            elif utcnow() - last > STALE_AFTER:
                h.state = HealthState.DEGRADED
                h.last_error = f"no push received for {int((utcnow() - last).total_seconds() // 60)} min"
        return h
