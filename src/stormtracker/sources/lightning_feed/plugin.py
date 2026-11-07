"""Lightning strikes pushed to StormTracker from any feed you are entitled to use.

There is deliberately no built-in client for a public lightning network. Blitzortung.org, the obvious free source, says
its servers are not meant to be queried directly by third-party applications and ties data access to running a registered
station, so connecting to it automatically would break its terms. Instead this source accepts strikes from whatever feeds
you do have access to (a Blitzortung station's own output or MQTT stream, a licensed commercial feed, a relay in n8n or
Node-RED) at ``POST /ingest/lightning_feed/<token>``:

    {"strikes": [{"time": "2026-11-06T10:15:02Z", "lat": -33.87, "lon": 151.21, "polarity": -1, "amplitude_ka": -12.5}]}

``time`` may be an ISO 8601 string or a Unix timestamp in seconds, milliseconds, microseconds or nanoseconds. A bare
list of strikes, or one strike object, is accepted too. Only strikes within ``max_distance_km`` of home are kept.
Your own Ecowitt lightning sensor needs none of this: the ``ecowitt`` source already records it.
"""
from __future__ import annotations

import hmac
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ...alerts.rules import haversine_km
from ...models import Event, Normalised
from ...plugins import Health, HealthState, Licence, RateLimit, Source, SourceMeta, secret_field
from ...timeutil import utcnow

MAX_BODY = 256 * 1024
MAX_STRIKES = 2000
MAX_AGE = timedelta(hours=6)
MAX_FUTURE = timedelta(minutes=5)


class LightningFeedConfig(BaseModel):
    push_token: str | None = secret_field("Random string used in the upload path: /ingest/lightning_feed/<token>. Pushes are refused until set.",
                                          required=True, generate=True)
    max_distance_km: float = Field(150.0, ge=5, le=2000, description="keep only strikes this close to home (km); the rest are ignored")
    location_id: str | None = Field(None, description="id of the configured location to measure from (default: the first)")


def parse_time(v: Any) -> datetime | None:
    """ISO 8601 text or a Unix timestamp in s / ms / us / ns (told apart by magnitude) -> aware UTC datetime."""
    try:
        if isinstance(v, bool):
            return None
        if isinstance(v, (int, float)) or (isinstance(v, str) and v.strip().replace(".", "", 1).lstrip("-").isdigit()):
            x = float(v)
            a = abs(x)                                       # tell the unit apart by size: 1e11 s would be the year 5138
            x = x / 1e9 if a >= 1e17 else x / 1e6 if a >= 1e14 else x / 1e3 if a >= 1e11 else x
            return datetime.fromtimestamp(x, UTC)
        if isinstance(v, str):
            d = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
            return d if d.tzinfo else d.replace(tzinfo=UTC)
    except (ValueError, OverflowError, OSError):
        return None
    return None


def _f(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) != float("inf") else None


def extract_strikes(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict) and isinstance(payload.get("strikes"), list):
        return payload["strikes"]
    if isinstance(payload, list):
        return payload
    return [payload] if isinstance(payload, dict) else []


class LightningFeed(Source):
    meta = SourceMeta(
        name="lightning_feed",
        category="lightning",
        description="Lightning strikes pushed from a feed you have access to (a Blitzortung station, a licensed feed, a relay). No built-in client for public networks.",
        poll_interval_s=0,
        rate_limit=RateLimit(max_requests=1, period_s=1),
        licence=Licence(type="personal", terms_url="https://www.blitzortung.org/en/",
                        notes="You are responsible for the terms of whatever feed you push. Blitzortung.org's servers are not meant to be queried by third-party apps; use your own station's output."),
    )
    ConfigModel = LightningFeedConfig
    cfg: LightningFeedConfig

    def __init__(self, cfg, ctx):
        super().__init__(cfg, ctx)
        self._last_push: datetime | None = None
        self._totals = {"received": 0, "kept": 0}

    @property
    def polling(self) -> bool:
        return False

    async def fetch(self) -> Any:
        raise RuntimeError("this source only receives pushes")

    def normalise(self, raw: dict[str, Any]) -> list[Normalised]:
        now = datetime.fromisoformat(raw["received"])
        loc = raw["location"]
        out: list[Normalised] = []
        seen: set[str] = set()
        for s in extract_strikes(raw["payload"])[:MAX_STRIKES]:
            if not isinstance(s, dict):
                continue
            ts = parse_time(s.get("time", s.get("ts", s.get("timestamp"))))
            lat = _f(s.get("lat", s.get("latitude")))
            lon = _f(s.get("lon", s.get("lng", s.get("longitude"))))
            if ts is None or lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
                continue
            if ts > now + MAX_FUTURE or ts < now - MAX_AGE:
                continue
            dist = haversine_km(loc["lat"], loc["lon"], lat, lon)
            if dist > self.cfg.max_distance_km:
                continue
            ext = f"lf:{int(ts.timestamp() * 1000)}:{lat:.3f}:{lon:.3f}"
            if ext in seen:
                continue
            seen.add(ext)
            props: dict[str, Any] = {"distance_km": round(dist, 1), "detected_by": "external feed"}
            pol, amp = _f(s.get("polarity", s.get("pol"))), _f(s.get("amplitude_ka", s.get("current_ka", s.get("amplitude"))))
            if pol is not None:
                props["polarity"] = -1 if pol < 0 else 1
            if amp is not None:
                props["amplitude_ka"] = amp
            out.append(Event(source=self.meta.name, event_type="lightning", external_id=ext, ts=ts, lat=lat, lon=lon, properties=props))
        return out

    async def handle_push(self, request: Any) -> JSONResponse:
        given = request.path_params.get("rest", "").strip("/").split("/")[0]
        if not given:                                      # forwarders that cannot change the path may use a header
            auth = request.headers.get("authorization", "")
            given = request.headers.get("x-stormtracker-token") or (auth[7:] if auth.lower().startswith("bearer ") else "")
        token = self.cfg.push_token
        if not token or not hmac.compare_digest(given.encode(), token.encode()):
            raise HTTPException(403, "invalid token")
        if request.method != "POST":
            raise HTTPException(405, "POST strikes as JSON")
        if int(request.headers.get("content-length") or 0) > MAX_BODY:
            raise HTTPException(413, "body too large")
        body = await request.body()
        if len(body) > MAX_BODY:
            raise HTTPException(413, "body too large")
        try:
            payload = json.loads(body)
        except ValueError:
            raise HTTPException(400, "body is not JSON") from None
        try:
            loc = await self.location(self.cfg.location_id)
        except RuntimeError as e:
            raise HTTPException(503, str(e)) from None
        strikes = extract_strikes(payload)
        events = self.normalise({"payload": payload, "received": utcnow().isoformat(), "location": loc})
        if events:
            await self.ctx.emit(events)
        self._last_push = utcnow()
        self._totals["received"] += len(strikes)
        self._totals["kept"] += len(events)
        h = self.ctx.state
        h.last_success, h.last_error, h.consecutive_failures, h.state = self._last_push, None, 0, HealthState.OK
        h.detail.update(last_push=self._last_push.isoformat(), strikes_received=self._totals["received"], strikes_kept=self._totals["kept"])
        return JSONResponse({"accepted": len(events), "ignored": len(strikes) - len(events)})

    async def health(self) -> Health:
        h = self.ctx.state
        if h.state == HealthState.OK and self._last_push is None:
            h.state, h.detail["note"] = HealthState.STARTING, "waiting for the first push"
        return h
