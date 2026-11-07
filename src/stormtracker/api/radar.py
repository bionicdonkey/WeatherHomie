"""Map-facing radar endpoints: frame list, rendered frame PNGs, overlay PNGs, legend.

Frames and overlays are re-projected to Web Mercator on the server so Leaflet's image overlay lines up
exactly; rendered PNGs are cached beside the data and served immutable (a frame never changes).
"""
from __future__ import annotations

import asyncio
import re
from datetime import timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Path as PathParam, Query, Request
from fastapi.responses import FileResponse
from sqlalchemy import select

from ..auth import Principal
from ..models import Georef
from ..radar_render import RENDER_VERSION, bounds_for, frame_stats, legend, render_frame_png, render_overlay_png
from ..storage.frames import FrameStore
from ..storage.tables import RadarFrameRow
from ..timeutil import utcnow

RADAR_RE = re.compile(r"^[A-Za-z0-9_-]{1,30}$")
LAYER_RE = re.compile(r"^[A-Za-z0-9_]{1,30}$")
IMMUTABLE = {"Cache-Control": "private, max-age=86400, immutable"}
SHORT = {"Cache-Control": "private, max-age=300"}


def build_radar_router(core) -> APIRouter:
    r = APIRouter(prefix="/api/radar")
    admin = Depends(core.auth.dependency("admin"))
    viewer = Depends(core.auth.dependency("viewer"))          # admin, member or read-only API token
    overlay_root = core.boot.data_dir / "radar_overlays"
    stats_cache: dict[tuple, dict] = {}

    def _bounds(b) -> list[list[float]]:
        return [[round(b[0], 5), round(b[1], 5)], [round(b[2], 5), round(b[3], 5)]]

    @r.get("/legend")
    async def get_legend(p: Principal = viewer):
        return {"unit": "mm/h", "approximate": True, "classes": legend()}

    @r.get("/frames")
    async def frames(request: Request, radar_id: str | None = None, hours: float = Query(3, ge=0.1, le=72),
                     limit: int = Query(60, ge=1, le=500), p: Principal = viewer):
        q = select(RadarFrameRow).where(RadarFrameRow.ts >= utcnow() - timedelta(hours=hours))
        if radar_id:
            q = q.where(RadarFrameRow.radar_id == radar_id)
        q = q.order_by(RadarFrameRow.ts.desc()).limit(limit)
        async with core.db.session() as s:
            rows = list(reversed((await s.execute(q)).scalars().all()))
        locs = await core.locations.list()
        home = locs[0] if locs else None
        out = []
        for row in rows:
            g = Georef(**row.georef)
            item = {"id": row.id, "ts": row.ts.isoformat(), "radar_id": row.radar_id, "source": row.source,
                    "bounds": _bounds(bounds_for(g)), "home": None,
                    # versioned so a change to colours/projection is not hidden by the browser's immutable cache
                    "png": f"/api/radar/frames/{row.id}.png?v={RENDER_VERSION}"}
            if home:
                key = (row.id, home.lat, home.lon)
                if key not in stats_cache:
                    def calc(path=row.path, g=g):
                        arr, _ = FrameStore.read(path)
                        return frame_stats(arr, g, home.lat, home.lon)
                    try:
                        stats_cache[key] = await asyncio.to_thread(calc)
                    except FileNotFoundError:
                        stats_cache[key] = {}
                item["home"] = stats_cache[key] or None
            out.append(item)
        if len(stats_cache) > 2000:
            stats_cache.clear()
        return out

    @r.get("/frames/{frame_id:int}.png")
    async def frame_png(frame_id: int = PathParam(ge=1, le=2**31 - 1), p: Principal = viewer):
        async with core.db.session() as s:
            row = await s.get(RadarFrameRow, frame_id)
        if row is None:
            raise HTTPException(404, "no such frame")
        cache = Path(row.path).with_suffix(f".merc{RENDER_VERSION}.png")
        if not cache.exists():
            def build():
                arr, g = FrameStore.read(row.path)
                png, _ = render_frame_png(arr, g)
                tmp = cache.with_suffix(".tmp")
                tmp.write_bytes(png)
                tmp.replace(cache)
            try:
                await asyncio.to_thread(build)
            except FileNotFoundError:
                raise HTTPException(404, "frame file has been purged") from None
        return FileResponse(cache, media_type="image/png", headers=IMMUTABLE)

    @r.get("/overlays")
    async def overlays(p: Principal = viewer):
        out = []
        if overlay_root.is_dir():
            for d in sorted(overlay_root.iterdir()):
                if d.is_dir() and RADAR_RE.match(d.name):
                    layers = sorted(f.stem for f in d.glob("*.png") if LAYER_RE.match(f.stem))
                    if layers:
                        out.append({"radar_id": d.name, "layers": layers})
        return out

    @r.get("/overlays/{radar_id}/{layer}.png")
    async def overlay_png(radar_id: str, layer: str, p: Principal = viewer):
        if not RADAR_RE.match(radar_id) or not LAYER_RE.match(layer):
            raise HTTPException(404, "no such overlay")
        src = overlay_root / radar_id / f"{layer}.png"
        if not src.is_file():
            raise HTTPException(404, "no such overlay")
        cache = overlay_root / radar_id / ".mercator" / f"{layer}.v{RENDER_VERSION}.png"
        if not cache.exists() or cache.stat().st_mtime < src.stat().st_mtime:
            async with core.db.session() as s:
                row = (await s.execute(select(RadarFrameRow).where(RadarFrameRow.radar_id == radar_id)
                                       .order_by(RadarFrameRow.ts.desc()).limit(1))).scalar_one_or_none()
            if row is None:
                raise HTTPException(404, "no frame yet for this radar, so its georeference is unknown")
            g = Georef(**row.georef)

            def build():
                png, _ = render_overlay_png(src.read_bytes(), g)
                cache.parent.mkdir(exist_ok=True)
                tmp = cache.with_suffix(".tmp")
                tmp.write_bytes(png)
                tmp.replace(cache)
            try:
                await asyncio.to_thread(build)
            except ValueError as e:
                raise HTTPException(422, str(e)) from None
        return FileResponse(cache, media_type="image/png", headers=SHORT)

    return r
