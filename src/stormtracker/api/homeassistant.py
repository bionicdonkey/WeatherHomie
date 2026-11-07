"""Home Assistant (MQTT discovery) settings and actions. Admin only."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..auth import Principal
from ..ha_mqtt import ENTITIES, HaSettings


def build_ha_router(core) -> APIRouter:
    r = APIRouter(prefix="/api/homeassistant")
    admin = Depends(core.auth.dependency("admin"))

    async def view() -> dict:
        b, why = await core.ha.broker()
        return {"settings": (await core.ha.settings()).model_dump(), "status": core.ha.status,
                "broker": {"ready": b is not None, "host": b.host if b else None, "port": b.port if b else None, "note": why},
                "entities": [{"key": e["key"], "name": e["name"], "kind": e["component"]} for e in ENTITIES]}

    @r.get("")
    async def get_ha(p: Principal = admin):
        return await view()

    @r.put("")
    async def put_ha(body: HaSettings, p: Principal = admin):
        await core.ha.save(body, p.username)
        return await view()

    @r.post("/publish")
    async def publish_now(p: Principal = admin):
        """Send the discovery messages and the current state right now, so a problem shows up here rather than silently."""
        cfg = await core.ha.settings()
        try:
            await core.ha.cycle(cfg.model_copy(update={"enabled": True}))
            core.ha._announce = False
        except RuntimeError as e:
            raise HTTPException(502, str(e)) from None
        return await view()

    @r.post("/remove")
    async def remove(p: Principal = admin):
        try:
            n = await core.ha.remove()
        except RuntimeError as e:
            raise HTTPException(502, str(e)) from None
        return {"ok": True, "removed": n, "note": "Turn the integration off as well, or the entities come back at the next update."}

    return r
