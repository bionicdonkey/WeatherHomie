from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta

from sqlalchemy import delete, event, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from ..models import Event, Observation, RadarFrame
from ..timeutil import utcnow
from .frames import FrameStore
from .tables import Base, EventRow, ObservationRow, RadarFrameRow


def _insert(dialect: str):
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    return insert


class Database:
    def __init__(self, url: str):
        self.engine: AsyncEngine = create_async_engine(url)
        if self.engine.dialect.name == "sqlite":
            @event.listens_for(self.engine.sync_engine, "connect")
            def _pragmas(conn, _):  # WAL so readers don't block the writer
                cur = conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA foreign_keys=ON")
                cur.close()
        self.session: async_sessionmaker[AsyncSession] = async_sessionmaker(self.engine, expire_on_commit=False)

    async def create_all(self) -> None:
        async with self.engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)

    async def dispose(self) -> None:
        await self.engine.dispose()


class Repository:
    """Persists normalised items. Re-polling is safe: duplicate observations and frames are ignored, and an event
    seen again (same source + external_id) is refreshed in place, so a warning's text or expiry can be updated."""

    def __init__(self, db: Database, frames: FrameStore):
        self.db, self.frames = db, frames

    async def save(self, items: Sequence[Observation | Event | RadarFrame]) -> int:
        n = 0
        for it in items:
            if isinstance(it, Observation):
                n += await self._ignore_dupes(ObservationRow, dict(
                    source=it.source, station_id=it.station_id, lat=it.lat, lon=it.lon, ts=it.ts, values=it.values))
            elif isinstance(it, Event):
                n += await self._upsert_event(dict(
                    source=it.source, event_type=it.event_type, external_id=it.external_id, ts=it.ts,
                    lat=it.lat, lon=it.lon, geometry=it.geometry, expires=it.expires, properties=it.properties))
            elif isinstance(it, RadarFrame):
                path = await self.frames.write(it)
                n += await self._ignore_dupes(RadarFrameRow, dict(
                    source=it.source, radar_id=it.radar_id, ts=it.ts, path=str(path), georef=it.georef.model_dump()))
        return n

    async def _ignore_dupes(self, table, row: dict) -> int:
        async with self.db.session() as s:
            res = await s.execute(_insert(self.db.engine.dialect.name)(table).values(**row).on_conflict_do_nothing())
            await s.commit()
            return res.rowcount or 0

    async def _upsert_event(self, row: dict) -> int:
        async with self.db.session() as s:
            stmt = _insert(self.db.engine.dialect.name)(EventRow).values(**row)
            refresh = {k: stmt.excluded[k] for k in ("event_type", "ts", "lat", "lon", "geometry", "expires", "properties")}
            res = await s.execute(stmt.on_conflict_do_update(index_elements=["source", "external_id"], set_=refresh))
            await s.commit()
            return res.rowcount or 0

    async def purge(self, *, frame_days: float, obs_days: float, event_days: float) -> dict[str, int]:
        now = utcnow()
        out = {}
        async with self.db.session() as s:
            old = (await s.execute(select(RadarFrameRow).where(
                RadarFrameRow.ts < now - timedelta(days=frame_days)))).scalars().all()
            for r in old:
                self.frames.delete(r.path)
            out["frames"] = len(old)
            if old:
                await s.execute(delete(RadarFrameRow).where(RadarFrameRow.id.in_([r.id for r in old])))
            r1 = await s.execute(delete(ObservationRow).where(ObservationRow.ts < now - timedelta(days=obs_days)))
            r2 = await s.execute(delete(EventRow).where(EventRow.ts < now - timedelta(days=event_days)))
            out["observations"], out["events"] = r1.rowcount or 0, r2.rowcount or 0
            await s.commit()
        return out
