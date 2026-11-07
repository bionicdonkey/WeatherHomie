from sqlalchemy import select

from stormtracker.plugins import HealthState
from stormtracker.storage.tables import AuditLog


async def test_requires_auth(client):
    assert (await client.get("/api/sources")).status_code == 401
    assert (await client.get("/api/health")).json() == {"status": "ok"}


async def test_login_bad_and_throttle(client):
    for _ in range(5):
        assert (await client.post("/api/auth/login", json={"username": "admin", "password": "x"})).status_code == 401
    assert (await client.post("/api/auth/login", json={"username": "admin", "password": "x"})).status_code == 429


async def test_csrf_enforced(client):
    r = await client.post("/api/auth/login", json={"username": "admin", "password": "correct-horse-battery"})
    assert r.status_code == 200
    assert (await client.put("/api/locations", json=[])).status_code == 403          # no token
    ok = await client.put("/api/locations", json=[], headers={"X-CSRF-Token": r.json()["csrf"]})
    assert ok.status_code == 200
    bad = await client.put("/api/locations", json=[], headers={"X-CSRF-Token": r.json()["csrf"],
                                                               "Origin": "http://evil.example"})
    assert bad.status_code == 403


async def test_locations_roundtrip_and_validation(admin):
    loc = {"id": "home", "name": "Home", "lat": -33.69, "lon": 151.24}
    assert (await admin.put("/api/locations", json=[loc])).status_code == 200
    assert (await admin.get("/api/locations")).json() == [loc]
    assert (await admin.put("/api/locations", json=[{**loc, "lat": 95}])).status_code == 422
    assert (await admin.put("/api/locations", json=[loc, loc])).status_code == 422


async def test_source_listing_has_schema_and_secret_flags(admin):
    srcs = {s["meta"]["name"]: s for s in (await admin.get("/api/sources")).json()}
    f = srcs["fake"]
    assert f["secret_fields"] == ["api_key"]
    assert f["meta"]["licence"]["type"] == "commercial-licensed"
    assert f["health"]["state"] == "disabled"
    assert f["schema"]["properties"]["api_key"]["secret"] is True


async def test_missing_credentials_guard(admin):
    r = await admin.put("/api/sources/fake", json={"enabled": True})
    assert r.status_code == 200
    assert r.json()["health"]["state"] == HealthState.MISSING_CREDENTIALS
    assert r.json()["health"]["detail"]["missing"] == ["api_key"]
    r = await admin.put("/api/sources/fake", json={"secrets": {"api_key": "s3cret"}})
    assert r.json()["health"]["state"] == "starting" or r.json()["health"]["state"] == "ok"


async def test_secrets_never_returned_or_audited_or_in_errors(admin, app):
    await admin.put("/api/sources/fake", json={"enabled": True, "secrets": {"api_key": "TOPSECRET"}})
    body = (await admin.get("/api/sources")).text + (await admin.get("/api/audit")).text
    assert "TOPSECRET" not in body
    assert (await admin.get("/api/sources")).json()  # still works
    # secret in plain config is rejected without echoing it
    r = await admin.put("/api/sources/fake", json={"config": {"api_key": "TOPSECRET2"}})
    assert r.status_code == 422 and "TOPSECRET2" not in r.text
    # secrets_set reflects state
    srcs = {s["meta"]["name"]: s for s in (await admin.get("/api/sources")).json()}
    assert srcs["fake"]["settings"]["secrets_set"] == {"api_key": True}
    # clearing a secret
    await admin.put("/api/sources/fake", json={"secrets": {"api_key": None}})
    srcs = {s["meta"]["name"]: s for s in (await admin.get("/api/sources")).json()}
    assert srcs["fake"]["settings"]["secrets_set"] == {"api_key": False}
    assert srcs["fake"]["health"]["state"] == "missing_credentials"
    async with app.state.core.db.session() as s:
        assert "TOPSECRET" not in str([r.detail for r in (await s.execute(select(AuditLog))).scalars()])


async def test_invalid_config_and_unknown_source(admin):
    assert (await admin.put("/api/sources/nope", json={"enabled": True})).status_code == 404
    assert (await admin.put("/api/sources/fake", json={"config": {"station": 5}})).status_code == 422
    assert (await admin.put("/api/sources/fake", json={"secrets": {"bogus": "x"}})).status_code == 422


async def test_push_ingest(admin):
    assert (await admin.post("/ingest/fake")).status_code == 404            # disabled
    await admin.put("/api/sources/fake", json={"enabled": True, "secrets": {"api_key": "k"}})
    r = await admin.post("/ingest/fake")
    assert r.json() == {"pushed": True}


async def test_settings_persist_across_restart(tmp_path, registry):
    from httpx import ASGITransport, AsyncClient
    from conftest import make_boot
    from stormtracker.main import create_app
    boot = make_boot(tmp_path, admin_password="correct-horse-battery")
    for i in range(2):
        app = create_app(boot, registry)
        async with app.router.lifespan_context(app):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
                r = await c.post("/api/auth/login", json={"username": "admin", "password": "correct-horse-battery"})
                h = {"X-CSRF-Token": r.json()["csrf"]}
                if i == 0:
                    await c.put("/api/sources/fake", json={"enabled": True, "secrets": {"api_key": "k"}}, headers=h)
                    await c.put("/api/locations", json=[{"id": "h", "name": "H", "lat": 1, "lon": 2}], headers=h)
                else:
                    s = {x["meta"]["name"]: x for x in (await c.get("/api/sources")).json()}
                    assert s["fake"]["settings"]["enabled"] and s["fake"]["settings"]["secrets_set"]["api_key"]
                    assert s["fake"]["health"]["state"] in ("starting", "ok")   # auto-started after restart
                    assert (await c.get("/api/locations")).json()[0]["id"] == "h"


async def test_first_run_setup_token(tmp_path, registry):
    from httpx import ASGITransport, AsyncClient
    from conftest import make_boot
    from stormtracker.main import create_app
    app = create_app(make_boot(tmp_path), registry)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            assert (await c.get("/api/auth/status")).json()["setup_required"] is True
            tok = app.state.core.auth.setup_token
            body = {"username": "me", "password": "long-enough-pw!"}
            assert (await c.post("/api/setup", json={**body, "setup_token": "wrong"})).status_code == 403
            assert (await c.post("/api/setup", json={**body, "password": "short", "setup_token": tok})).status_code == 422
            eleven = await c.post("/api/setup", json={**body, "password": "x" * 11, "setup_token": tok})
            assert eleven.status_code == 422 and "at least 12 characters" in eleven.text            # the form promises 12; the server enforces the same
            assert (await c.post("/api/setup", json={**body, "setup_token": None})).status_code == 403           # no token is not a way round it
            assert (await c.post("/api/setup", json={**body, "setup_token": tok})).status_code == 200
            assert (await c.post("/api/setup", json={**body, "setup_token": tok})).status_code == 409
            assert (await c.post("/api/auth/login", json={"username": "me", "password": body["password"]})).status_code == 200


async def test_running_source_sees_location_changes(admin, app):
    await admin.put("/api/sources/fake", json={"enabled": True, "secrets": {"api_key": "k"}})
    ctx = app.state.core.scheduler.running("fake").ctx
    assert await ctx.get_locations() == []
    await admin.put("/api/locations", json=[{"id": "home", "name": "Home", "lat": -33.0, "lon": 151.0}])
    assert (await ctx.get_locations())[0]["id"] == "home"


async def test_disabling_unconfigured_source_and_required_fields(admin, app):
    from stormtracker.sources.bom_radar.plugin import BomRadar
    app.state.core.registry.register(BomRadar)
    # a never-configured source can be saved disabled; required plain fields only bite when enabling
    assert (await admin.put("/api/sources/bom_radar", json={"enabled": False})).status_code == 200
    assert (await admin.put("/api/sources/bom_radar", json={"enabled": True})).status_code == 422
    r = await admin.put("/api/sources/bom_radar", json={"enabled": True, "config": {"radar_id": "IDR714"}})
    assert r.status_code == 200
