"""Nowcast endpoints: latest tracked cells + forecast + home impact, forecast frame PNGs, settings."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from fastapi.responses import Response

from ..auth import Principal
from ..nowcast.settings import NowcastSettings

IMMUTABLE = {"Cache-Control": "private, max-age=3600, immutable"}


def build_nowcast_router(core) -> APIRouter:
    r = APIRouter(prefix="/api/nowcast")

    admin = Depends(core.auth.dependency("admin"))
    viewer = Depends(core.auth.dependency("viewer"))          # admin, member or read-only API token

    @r.get("")
    async def latest(p: Principal = viewer):
        return core.nowcast.public()

    @r.get("/frames/{n:int}/{lead:int}.png")
    async def frame(n: int = Path(ge=1, le=2**31 - 1), lead: int = Path(ge=1, le=100000), p: Principal = viewer):
        png = await core.nowcast.png(n, lead)
        if png is None:
            raise HTTPException(404, "that forecast has been replaced by a newer one")
        return Response(png, media_type="image/png", headers=IMMUTABLE)

    @r.get("/settings")
    async def get_settings(p: Principal = admin):
        return (await core.nowcast.settings()).model_dump()

    @r.put("/settings")
    async def put_settings(body: NowcastSettings, p: Principal = admin):
        await core.nowcast.save_settings(body, p.username)
        return body.model_dump()

    @r.post("/recompute")
    async def recompute(p: Principal = admin):
        await core.nowcast.run(force=True)
        return core.nowcast.public()

    return r
