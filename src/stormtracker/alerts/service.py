"""Runs the alert rules against the live situation and delivers what fires."""
from __future__ import annotations

import asyncio
import logging
import re
import uuid
from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy import delete, select

from ..plugins import PluginRegistry
from ..scheduler import redact
from ..settings.sources import SourceSettings
from ..storage.tables import AlertLogRow, EventRow, ObservationRow
from ..timeutil import utcnow
from .base import Alert, OutputContext
from .engine import AlertEngine, Fire
from .rules import AlertSettings, Condition, Rule, Snapshot, evaluate

log = logging.getLogger("stormtracker.alerts")
POLL_S = 30
RESTORE_WINDOW = timedelta(hours=6)
RULES_KEY = "alerts.rules"
SETTINGS_KEY = "alerts.settings"


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:30] or "rule"


class AlertService:
    def __init__(self, core, registry: PluginRegistry, outputs: SourceSettings, user_agent: str = "StormTracker") -> None:
        self.core, self.registry, self.outputs = core, registry, outputs
        self.http = httpx.AsyncClient(headers={"User-Agent": user_agent}, timeout=20)
        self.engine = AlertEngine()
        self._restored = False
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self.status: dict[str, Any] = {"state": "starting", "last_run": None, "error": None}
        self.output_health: dict[str, dict[str, Any]] = {}

    # ---- settings and rules (stored in the DB like everything else) ------------------------------------------
    async def settings(self) -> AlertSettings:
        return AlertSettings(**(await self.core.store.get(SETTINGS_KEY, {})))

    async def save_settings(self, s: AlertSettings, actor: str) -> None:
        await self.core.store.set(SETTINGS_KEY, s.model_dump(), actor=actor)

    async def rules(self) -> list[Rule]:
        out = []
        for raw in await self.core.store.get(RULES_KEY, []):
            try:
                out.append(Rule(**raw))
            except ValueError:
                log.warning("ignoring a stored alert rule that no longer validates: %s", raw.get("id"))
        return out

    async def save_rules(self, rules: list[Rule], actor: str) -> None:
        unknown = sorted({o for r in rules for o in r.outputs if o not in self.registry.all()})
        if unknown:
            raise ValueError(f"unknown output(s): {', '.join(unknown)}")
        await self.core.store.set(RULES_KEY, [r.model_dump(mode="json") for r in rules], actor=actor)

    async def delete_rule(self, rule_id: str, actor: str) -> bool:
        rules = await self.rules()
        keep = [r for r in rules if r.id != rule_id]
        if len(keep) == len(rules):
            return False
        await self.save_rules(keep, actor)
        self.engine.forget(rule_id)
        return True

    # ---- lifecycle ----------------------------------------------------------------------------------------------
    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="alerts")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        await self.http.aclose()

    async def _loop(self) -> None:
        await asyncio.sleep(5)                                   # let the first nowcast finish before judging anything
        while True:
            await self.run()
            await asyncio.sleep(POLL_S)

    # ---- the situation ------------------------------------------------------------------------------------------
    async def snapshot(self, now=None) -> Snapshot:
        now = now or utcnow()
        locs = await self.core.locations.list()
        snap = Snapshot(now=now, home={"name": locs[0].name, "lat": locs[0].lat, "lon": locs[0].lon} if locs else None,
                        nowcast=self.core.nowcast.public() if self.core.nowcast else None)
        async with self.core.db.session() as s:
            o = (await s.execute(select(ObservationRow).order_by(ObservationRow.ts.desc()).limit(1))).scalar_one_or_none()
            if o is not None:
                snap.obs, snap.obs_ts = dict(o.values), o.ts
                earlier = (await s.execute(select(ObservationRow).where(
                    ObservationRow.source == o.source, ObservationRow.station_id == o.station_id,
                    ObservationRow.ts <= o.ts - timedelta(minutes=45), ObservationRow.ts >= o.ts - timedelta(minutes=75))
                    .order_by(ObservationRow.ts.desc()).limit(1))).scalar_one_or_none()
                snap.pressure_1h_ago = (earlier.values or {}).get("pressure_hpa") if earlier else None
                for w in (await s.execute(select(ObservationRow).where(
                        ObservationRow.source == o.source, ObservationRow.station_id == o.station_id, ObservationRow.ts >= o.ts - timedelta(hours=2))
                        .order_by(ObservationRow.ts.asc()).limit(1500))).scalars():
                    v = w.values or {}
                    snap.wind_history.append({"ts": w.ts, "speed_ms": v.get("wind_speed_ms"), "gust_ms": v.get("wind_gust_ms"), "dir_deg": v.get("wind_dir_deg")})
            for e in (await s.execute(select(EventRow).where(EventRow.event_type == "lightning", EventRow.ts >= now - timedelta(hours=6))
                                      .order_by(EventRow.ts.desc()).limit(2000))).scalars():
                snap.lightning.append({"ts": e.ts, "lat": e.lat, "lon": e.lon, "distance_km": (e.properties or {}).get("distance_km")})
            mf = (await s.execute(select(EventRow).where(EventRow.external_id.like("forecast:%")).order_by(EventRow.ts.desc()).limit(1))).scalar_one_or_none()
            if mf is not None and (mf.properties or {}).get("kind") == "model_forecast":
                snap.model_forecast = mf.properties
            for e in (await s.execute(select(EventRow).where(EventRow.event_type == "warning", EventRow.ts >= now - timedelta(hours=48))
                                      .order_by(EventRow.ts.desc()).limit(200))).scalars():
                snap.warnings.append({"external_id": e.external_id, "ts": e.ts, "expires": e.expires, "properties": e.properties or {}})
        return snap

    async def preview(self, now=None) -> dict[str, list[dict]]:
        """What each enabled rule matches right now, without sending anything or changing any state."""
        snap = await self.snapshot(now)
        return {r.id: [{"key": c.key, "title": c.title, "message": c.message} for c in evaluate(r, snap)]
                for r in await self.rules() if r.enabled}

    # ---- one pass -----------------------------------------------------------------------------------------------
    async def run(self, now=None, send: bool = True) -> list[Alert]:
        async with self._lock:
            try:
                st = await self.settings()
                if not st.enabled:
                    self.status.update(state="disabled", error=None)
                    return []
                snap = await self.snapshot(now)
                rules = await self.rules()
                self.engine.clear_after = timedelta(minutes=st.clear_after_min)
                if not self._restored:
                    await self._restore(rules, snap.now)
                fired: list[Alert] = []
                rule_errors: dict[str, str] = {}
                for rule in rules:
                    if not rule.enabled:
                        self.engine.forget(rule.id)
                        continue
                    try:
                        conditions = evaluate(rule, snap)
                    except Exception as e:  # noqa: BLE001 - one rule tripping over odd data must not silence the others
                        log.exception("alert rule %s failed to evaluate", rule.id)
                        rule_errors[rule.id] = f"{type(e).__name__}: {e}"[:200]
                        continue
                    for f in self.engine.step(snap.now, rule, conditions):
                        a = self._alert(f, snap, st)
                        fired.append(a)
                        if send:
                            await self._record(a, await self.deliver(a, rule.outputs))
                self.status.update(state="ok" if not rule_errors else "degraded", last_run=snap.now.isoformat(),
                                   error=None if not rule_errors else f"{len(rule_errors)} rule(s) could not be checked", rule_errors=rule_errors)
                return fired
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - a bug here must not take the app down
                log.exception("alert evaluation failed")
                self.status.update(state="error", error=f"{type(e).__name__}: {e}"[:300])
                return []

    def _alert(self, f: Fire, snap: Snapshot, st: AlertSettings) -> Alert:
        return Alert(id=uuid.uuid4().hex[:12], ts=snap.now, kind=f.kind, rule_id=f.rule.id, rule_name=f.rule.name, key=f.condition_key,
                     severity=f.rule.severity if f.kind == "alert" else "info", title=f.title, message=f.message,
                     url=f"{st.public_url}/#/map" if st.public_url else None, data=f.data)

    async def _restore(self, rules: list[Rule], now) -> None:
        """After a restart, treat recently alerted conditions as ongoing so they are not announced a second time."""
        ids = {r.id for r in rules if r.enabled}
        async with self.core.db.session() as s:
            rows = (await s.execute(select(AlertLogRow).where(AlertLogRow.kind == "alert", AlertLogRow.ts >= now - RESTORE_WINDOW)
                                    .order_by(AlertLogRow.ts))).scalars().all()
        for r in rows:
            if r.rule_id in ids:
                self.engine.restore(r.rule_id, r.key, r.ts, now, r.title)
        self._restored = True

    # ---- delivery -----------------------------------------------------------------------------------------------
    async def deliver(self, alert: Alert, names: list[str]) -> dict[str, dict]:
        async def one(name: str) -> tuple[str, dict]:
            out = None
            try:
                cls = self.registry.get(name)
            except KeyError:
                return name, {"ok": False, "error": "no such output"}
            try:
                if not (await self.outputs.raw(name))["enabled"]:
                    return name, {"ok": False, "error": "turned off"}
                cfg, missing = await self.outputs.build(name)
                if cfg is None:
                    return name, {"ok": False, "error": f"needs setup: {', '.join(missing) or 'invalid settings'}"}
                out = cls(cfg, OutputContext(self.http, logging.getLogger(f"alerts.{name}")))
                await asyncio.wait_for(out.send(alert), 30)
                res: dict = {"ok": True, "error": None}
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - one broken output must not stop the others
                msg = f"{type(e).__name__}: {e}"
                res = {"ok": False, "error": (redact(out, msg) if out is not None else msg)[:300]}
            h = self.output_health.setdefault(name, {"sent": 0, "failed": 0, "last_success": None, "last_error": None})
            if res["ok"]:
                h["sent"] += 1
                h["last_success"], h["last_error"] = utcnow().isoformat(), None
            else:
                h["failed"] += 1
                h["last_error"] = res["error"]
            return name, res
        return dict(await asyncio.gather(*(one(n) for n in names)))

    async def test_output(self, name: str) -> dict:
        alert = Alert(id=uuid.uuid4().hex[:12], ts=utcnow(), kind="test", rule_id="test", rule_name="Test", key="test", severity="info",
                      title="Test alert from StormTracker", message="If you can read this, this destination works.",
                      url=((await self.settings()).public_url or None))
        res = (await self.deliver(alert, [name]))[name]
        await self._record(alert, {name: res})
        return res

    async def _record(self, a: Alert, results: dict[str, dict]) -> None:
        async with self.core.db.session() as s:
            s.add(AlertLogRow(ts=a.ts, rule_id=a.rule_id, rule_name=a.rule_name, key=a.key[:200], kind=a.kind, severity=a.severity,
                              title=a.title[:200], message=a.message, data=a.data, results=results))
            await s.commit()

    async def history(self, limit: int = 50) -> list[dict]:
        async with self.core.db.session() as s:
            rows = (await s.execute(select(AlertLogRow).order_by(AlertLogRow.id.desc()).limit(min(max(limit, 1), 500)))).scalars().all()
        return [{"id": r.id, "ts": r.ts.isoformat(), "rule_id": r.rule_id, "rule_name": r.rule_name, "kind": r.kind, "severity": r.severity,
                 "title": r.title, "message": r.message, "results": r.results} for r in rows]

    async def prune(self, days: float) -> int:
        async with self.core.db.session() as s:
            n = (await s.execute(delete(AlertLogRow).where(AlertLogRow.ts < utcnow() - timedelta(days=days)))).rowcount
            await s.commit()
        return n or 0

    def active(self) -> list[dict]:
        return [{"rule_id": rid, "key": key, "title": ep.title, "since": ep.sent_at.isoformat()} for (rid, key), ep in self.engine.episodes.items()]
