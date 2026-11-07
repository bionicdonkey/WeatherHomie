"""Opt-in: runs the storage paths against a real Postgres. Set TEST_DATABASE_URL, for example

    docker run -d --name pg -e POSTGRES_PASSWORD=pgtest -e POSTGRES_DB=storm -p 127.0.0.1:15432:5432 postgres:16-alpine
    TEST_DATABASE_URL=postgresql+asyncpg://postgres:pgtest@127.0.0.1:15432/storm pytest tests/core/test_postgres.py

The tables are created in whatever database that URL points at, so use a throwaway one.
"""
import os
from datetime import timedelta

import httpx
import numpy as np
import pytest
from httpx import ASGITransport, AsyncClient

from stormtracker.models import Event, Observation, RadarFrame
from stormtracker.sources.bom_radar.georef import georef_for
from stormtracker.timeutil import utcnow

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="set TEST_DATABASE_URL to run against Postgres")


async def test_the_whole_storage_path_on_postgres(tmp_path):
    from conftest import make_boot
    from stormtracker.main import create_app
    boot = make_boot(tmp_path, admin_password="correct-horse-battery")
    boot = type(boot)(**{**boot.__dict__, "database_url": URL})
    app = create_app(boot)
    async with app.router.lifespan_context(app):
        core = app.state.core
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post("/api/auth/login", json={"username": "admin", "password": "correct-horse-battery"})
            c.headers["X-CSRF-Token"] = r.json()["csrf"]
            now = utcnow().replace(microsecond=0, second=0)
            assert (await c.put("/api/locations", json=[{"id": "home", "name": "Home", "lat": -33.83, "lon": 151.23}])).status_code == 200      # JSON column
            r = await c.put("/api/alerts/outputs/webhook", json={"enabled": True, "secrets": {"url": "https://example.org/SECRET"}})              # encrypted blob
            assert r.json()["settings"]["secrets_set"]["url"] is True
            obs = [Observation(source="s", station_id="x", lat=-33.83, lon=151.23, ts=now - timedelta(minutes=m), values={"temp_c": 20.0 + m}) for m in range(0, 120, 10)]
            assert await core.repo.save(obs) == 12 and await core.repo.save(obs) == 0                                                           # idempotent
            got = (await c.get("/api/observations", params={"source": "s", "since": (now - timedelta(minutes=45)).isoformat(), "order": "asc"})).json()
            assert len(got) == 5 and got[-1]["ts"].endswith("+00:00")                                                                          # time zones survive
            ev = lambda v, m: Event(source="w", event_type="warning", external_id="w1", ts=now, expires=now + timedelta(minutes=m), properties={"v": v})   # noqa: E731
            await core.repo.save([ev(1, 20)])
            await core.repo.save([ev(2, 40)])
            (e,) = (await c.get("/api/events?event_type=warning")).json()
            assert e["properties"]["v"] == 2                                                                                                    # ON CONFLICT DO UPDATE
            y, x = np.mgrid[0:512, 0:512]
            for k in range(4):
                arr = (40 * np.exp(-(((x - 180 - 5 * k) / 12) ** 2 + ((y - 256) / 12) ** 2))).astype(np.float32)
                await core.repo.save([RadarFrame(source="bom_radar", radar_id="IDR713", ts=now - timedelta(minutes=5 * (3 - k)), rain_rate_mmh=arr, georef=georef_for("IDR713"))])
            assert len((await c.get("/api/radar/frames")).json()) == 4
            assert (await c.post("/api/nowcast/recompute")).json()["available"] is True
            core.alerts.http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(204)))
            await c.post("/api/alerts/rules", json={"name": "Gust", "cooldown_min": 0, "outputs": ["webhook"], "params": {"type": "gust", "min_gust_kmh": 10}})
            await core.repo.save([Observation(source="e", station_id="s", lat=-33.83, lon=151.23, ts=now, values={"wind_gust_ms": 20.0})])
            assert len(await core.alerts.run()) == 1 and len((await c.get("/api/alerts/history")).json()) == 1
            await c.post("/api/auth/logout")
            assert len(await core.store.get("auth.revoked")) == 1
            assert (await core.repo.purge(frame_days=0.0001, obs_days=0.0001, event_days=0.0001))["frames"] >= 1
        # leave the throwaway database clean for the next run
        from stormtracker.storage.tables import Base
        async with core.db.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
