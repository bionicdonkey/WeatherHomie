from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel

from stormtracker.bootstrap import Bootstrap
from stormtracker.main import create_app
from stormtracker.models import Event, Observation
from stormtracker.plugins import (Licence, NoConfig, PluginRegistry, RateLimit, Source, SourceMeta,
                                  secret_field)

MASTER = "k" * 40


class FakeConfig(BaseModel):
    station: str = "S1"
    api_key: str = secret_field("API key", required=True)


class FakeSource(Source):
    meta = SourceMeta(name="fake", category="observations", poll_interval_s=60,
                      rate_limit=RateLimit(max_requests=2, period_s=1),
                      licence=Licence(type="commercial-licensed"))
    ConfigModel = FakeConfig
    fail = False
    calls = 0

    async def fetch(self):
        FakeSource.calls += 1
        if FakeSource.fail:
            raise RuntimeError("boom")
        return {"t": 21.5, "key": self.cfg.api_key}

    def normalise(self, raw):
        ts = datetime(2026, 1, 1, tzinfo=UTC)
        return [Observation(source="fake", station_id=self.cfg.station, lat=-33.7, lon=151.2, ts=ts,
                            values={"temp_c": raw["t"]}),
                Event(source="fake", event_type="warning", external_id="w1", ts=ts)]

    async def handle_push(self, request):
        return {"pushed": True}


class BoomSource(FakeSource):
    meta = SourceMeta(name="boom", category="radar", poll_interval_s=60)
    ConfigModel = NoConfig

    async def fetch(self):
        raise RuntimeError("always")


def make_boot(tmp: Path, **kw) -> Bootstrap:
    return Bootstrap(database_url=f"sqlite+aiosqlite:///{tmp}/t.db", data_dir=tmp, master_key=MASTER,
                     cookie_secure=False, **kw)


@pytest.fixture
def registry():
    FakeSource.fail, FakeSource.calls = False, 0
    r = PluginRegistry()
    r.register(FakeSource)
    r.register(BoomSource)
    return r


@pytest.fixture
async def app(tmp_path, registry):
    app = create_app(make_boot(tmp_path, admin_password="correct-horse-battery"), registry)
    async with app.router.lifespan_context(app):
        yield app


@pytest.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
async def admin(client):
    r = await client.post("/api/login" if False else "/api/auth/login",
                          json={"username": "admin", "password": "correct-horse-battery"})
    assert r.status_code == 200
    client.headers["X-CSRF-Token"] = r.json()["csrf"]
    return client


@pytest.fixture
def make_ctx():
    """Build a SourceContext with a mock HTTP transport and one location (for plugin unit tests)."""
    import logging

    import httpx as _httpx

    from stormtracker.plugins import Health, SourceContext

    def _make(handler, locations=None):
        async def locs():
            return locations if locations is not None else [{"id": "home", "name": "Home", "lat": -33.0, "lon": 151.0}]

        async def emit(items):
            pass

        return SourceContext(http=_httpx.AsyncClient(transport=_httpx.MockTransport(handler)), emit=emit,
                             health=Health(), locations=locs, logger=logging.getLogger("t"))
    return _make


# ---- access control fixtures (roles, API tokens, single sign-on) ----
@pytest.fixture(scope="session")
def signer():
    from tests.access.jwtkit import Signer
    return Signer()


@pytest.fixture
def core(app):
    return app.state.core


@pytest.fixture
async def member(app, core):
    tok, csrf = core.auth.issue("sso:mia", "member", "oidc", 3600)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers={"X-CSRF-Token": csrf}) as c:
        c.cookies.set("st_session", tok)
        yield c


@pytest.fixture
async def anon(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
async def token(admin):
    r = await admin.post("/api/tokens", json={"name": "agent"})
    assert r.status_code == 200, r.text
    return r.json()["token"]


@pytest.fixture
async def agent(app, token):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": f"Bearer {token}"}) as c:
        yield c
