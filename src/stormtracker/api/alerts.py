"""Alert endpoints: destinations (outputs), rules, history, and a dry-run of what matches right now."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..alerts.rules import PRESETS, RULE_TYPES, AlertSettings, Rule
from ..auth import Principal
from ..plugins import secret_fields
from ..settings.sources import SourceSettingsError


class OutputUpdate(BaseModel):
    enabled: bool | None = None
    config: dict[str, Any] | None = None
    secrets: dict[str, str | None] | None = Field(default=None, description="write-only; null/'' deletes. Values are never returned.")


class RuleIn(Rule):
    id: str | None = None                      # omitted when creating: made from the name


def build_alerts_router(core) -> APIRouter:
    r = APIRouter(prefix="/api/alerts")

    admin = Depends(core.auth.dependency("admin"))
    viewer = Depends(core.auth.dependency("viewer"))          # admin, member or read-only API token
    svc = lambda: core.alerts   # noqa: E731

    # ---- destinations ----
    async def output_view(name: str) -> dict:
        cls = core.alert_registry.get(name)
        return {"meta": cls.meta.model_dump(), "schema": cls.ConfigModel.model_json_schema(), "secret_fields": secret_fields(cls.ConfigModel),
                "settings": await core.alert_outputs.view(name), "health": svc().output_health.get(name)}

    @r.get("/outputs")
    async def outputs(p: Principal = admin):
        return [await output_view(n) for n in sorted(core.alert_registry.all())]

    @r.put("/outputs/{name}")
    async def update_output(name: str, body: OutputUpdate, p: Principal = admin):
        if name not in core.alert_registry.all():
            raise HTTPException(404, "unknown output")
        try:
            await core.alert_outputs.update(name, enabled=body.enabled, config=body.config, secrets=body.secrets, actor=p.username)
        except SourceSettingsError as e:
            raise HTTPException(422, str(e)) from None
        return await output_view(name)

    @r.post("/outputs/{name}/test")
    async def test_output(name: str, p: Principal = admin):
        if name not in core.alert_registry.all():
            raise HTTPException(404, "unknown output")
        return await svc().test_output(name)               # {"ok": bool, "error": str | None}: a failed delivery is a result, not a server error

    # ---- rules ----
    @r.get("/rule-types")
    async def rule_types(p: Principal = admin):
        out = []
        for t, (model, title, desc) in RULE_TYPES.items():
            schema = model.model_json_schema()
            schema["properties"].pop("type", None)
            out.append({"type": t, "title": title, "description": desc, "schema": schema})
        return out

    @r.get("/presets")
    async def presets(p: Principal = admin):
        return PRESETS

    @r.get("/rules")
    async def rules(p: Principal = admin):
        return [x.model_dump(mode="json") for x in await svc().rules()]

    async def _save(rule: Rule, actor: str, *, replacing: str | None) -> dict:
        existing = await svc().rules()
        others = [x for x in existing if x.id != replacing]
        if any(x.id == rule.id for x in others):
            raise HTTPException(409, "a rule with that id already exists")
        try:
            await svc().save_rules([*others, rule] if replacing is None else [rule if x.id == replacing else x for x in existing], actor)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        svc().engine.forget(rule.id)                       # edited rules start fresh: new thresholds, new episodes
        return rule.model_dump(mode="json")

    @r.post("/rules")
    async def create_rule(body: RuleIn, p: Principal = admin):
        from ..alerts.service import slugify
        existing = {x.id for x in await svc().rules()}
        base = body.id or slugify(body.name)
        rid, n = base, 2
        while rid in existing:
            rid, n = f"{base}-{n}", n + 1
        return await _save(Rule(**{**body.model_dump(), "id": rid}), p.username, replacing=None)

    @r.put("/rules/{rule_id}")
    async def update_rule(rule_id: str, body: RuleIn, p: Principal = admin):
        if rule_id not in {x.id for x in await svc().rules()}:
            raise HTTPException(404, "no such rule")
        return await _save(Rule(**{**body.model_dump(), "id": rule_id}), p.username, replacing=rule_id)

    @r.delete("/rules/{rule_id}")
    async def delete_rule(rule_id: str, p: Principal = admin):
        if not await svc().delete_rule(rule_id, p.username):
            raise HTTPException(404, "no such rule")
        return {"deleted": rule_id}

    @r.get("/preview")
    async def preview(p: Principal = admin):
        return await svc().preview()

    # ---- settings, history, status ----
    @r.get("/settings")
    async def get_settings(p: Principal = admin):
        return (await svc().settings()).model_dump()

    @r.put("/settings")
    async def put_settings(body: AlertSettings, p: Principal = admin):
        await svc().save_settings(body, p.username)
        return body.model_dump()

    @r.get("/history")
    async def history(limit: int = 50, p: Principal = viewer):
        rows = await svc().history(limit)
        if p.role != "admin":                         # which destinations got a message, and any delivery errors, are for admins
            rows = [{k: v for k, v in r.items() if k != "results"} for r in rows]
        return rows

    @r.get("/status")
    async def status(p: Principal = viewer):
        return {**svc().status, "active": svc().active()}

    return r
