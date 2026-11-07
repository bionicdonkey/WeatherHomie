"""Roles (admin / member / read-only token) and API tokens."""
import re

PUBLIC = {"/api/health", "/api/auth/status", "/api/setup", "/api/auth/login", "/api/auth/logout", "/api/auth/oidc/login", "/api/auth/oidc/callback"}
# every GET a member or API token may read; anything not listed here must refuse them
VIEWER = {
    ("GET", "/api/now"), ("GET", "/api/locations"), ("GET", "/api/observations"), ("GET", "/api/stations"), ("GET", "/api/events"),
    ("GET", "/api/forecast"), ("GET", "/api/wind"), ("GET", "/api/ui"),
    ("GET", "/api/radar/legend"), ("GET", "/api/radar/frames"), ("GET", "/api/radar/frames/{frame_id}.png"), ("GET", "/api/radar/overlays"),
    ("GET", "/api/radar/overlays/{radar_id}/{layer}.png"),
    ("GET", "/api/nowcast"), ("GET", "/api/nowcast/frames/{n}/{lead}.png"),
    ("GET", "/api/alerts/history"), ("GET", "/api/alerts/status"),
}


def all_routes(app):
    """Every /api route and method, read from the app's OpenAPI document (so nothing hand-listed can drift)."""
    for path, ops in app.openapi()["paths"].items():
        if path.startswith("/api/"):
            for m in sorted(ops):
                yield m.upper(), path


def concrete(path):
    return re.sub(r"\{(\w+)(?::\w+)?\}", lambda m: "1" if m.group(1) in ("frame_id", "n", "lead", "token_id") else "x", path)


async def test_every_non_viewer_route_refuses_members_and_tokens(app, member, agent):
    """The safety net: a route added later that forgets its permission check fails here."""
    seen = set()
    for m, path in all_routes(app):
        if path in PUBLIC:
            continue
        seen.add((m, path))
        if (m, path) in VIEWER:
            continue
        for who, c in (("member", member), ("token", agent)):
            r = await c.request(m, concrete(path), json={} if m != "GET" else None)
            assert r.status_code == 403, (who, m, path, r.status_code, r.text[:100])
    assert VIEWER <= seen, VIEWER - seen            # the allow-list names real routes


async def test_viewer_routes_work_for_members_and_tokens(member, agent):
    for c in (member, agent):
        for path in ("/api/now", "/api/locations", "/api/stations", "/api/events", "/api/forecast", "/api/wind", "/api/ui", "/api/observations", "/api/nowcast",
                     "/api/alerts/history", "/api/alerts/status", "/api/radar/frames", "/api/radar/legend", "/api/radar/overlays"):
            r = await c.get(path)
            assert r.status_code == 200, (path, r.status_code, r.text[:100])


async def test_unauthenticated_gets_401_not_403(anon):
    assert (await anon.get("/api/stations")).status_code == 401
    assert (await anon.get("/api/sources")).status_code == 401
    r = await anon.get("/api/now", headers={"Authorization": "Bearer st_nope_nope"})
    assert r.status_code == 401


async def test_member_cannot_change_anything_even_with_csrf(member):
    for m, path in (("PUT", "/api/ui"), ("PUT", "/api/locations"), ("POST", "/api/tokens"), ("PUT", "/api/sources/fake"), ("PUT", "/api/auth/oidc/settings"), ("POST", "/api/auth/password")):
        r = await member.request(m, path, json={})
        assert r.status_code == 403, (m, path, r.status_code)


async def test_member_session_status_reports_role(member):
    j = (await member.get("/api/auth/status")).json()
    assert j["authenticated"] and j["role"] == "member" and j["kind"] == "oidc" and j["username"] == "sso:mia"


async def test_old_sessions_without_a_role_are_still_admin(app, core):
    """Sessions issued before roles existed carry no role; they were all admin password sign-ins."""
    tok = core.auth.ser.dumps({"u": "admin", "c": "csrf", "n": "nonce12345", "e": core.auth.epoch})
    from httpx import ASGITransport, AsyncClient
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers={"X-CSRF-Token": "csrf"}) as c:
        c.cookies.set("st_session", tok)
        j = (await c.get("/api/auth/status")).json()
        assert j["authenticated"] and j["role"] == "admin" and j["kind"] == "local"
        assert (await c.get("/api/sources")).status_code == 200


async def test_sso_session_expires_on_its_own(app, core):
    from httpx import ASGITransport, AsyncClient
    tok, _ = core.auth.issue("sso:x", "member", "oidc", lifetime=-5)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        c.cookies.set("st_session", tok)
        assert (await c.get("/api/stations")).status_code == 401


async def test_forged_role_in_cookie_is_rejected(app, core):
    from httpx import ASGITransport, AsyncClient
    from itsdangerous import URLSafeTimedSerializer
    bad = URLSafeTimedSerializer("wrong-secret", salt="stormtracker-session").dumps({"u": "x", "c": "c", "n": "n12345678901", "e": 0, "r": "admin", "k": "local"})
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        c.cookies.set("st_session", bad)
        assert (await c.get("/api/sources")).status_code == 401


async def test_password_change_refused_for_sso_users(app, core):
    from httpx import ASGITransport, AsyncClient
    tok, csrf = core.auth.issue("sso:boss", "admin", "oidc", 3600)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers={"X-CSRF-Token": csrf}) as c:
        c.cookies.set("st_session", tok)
        assert (await c.get("/api/sources")).status_code == 200                      # an SSO admin is a full admin
        r = await c.post("/api/auth/password", json={"current_password": "a" * 12, "new_password": "b" * 12})
        assert r.status_code == 403 and "identity provider" in r.json()["detail"]


async def test_header_auth_stays_admin(tmp_path, registry):
    from httpx import ASGITransport, AsyncClient

    from stormtracker.main import create_app
    from tests.conftest import make_boot
    app = create_app(make_boot(tmp_path, trusted_auth_header="x-user", trusted_proxies=("127.0.0.1",)), registry)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app, client=("127.0.0.1", 1)), base_url="http://test", headers={"x-user": "alice"}) as c:
            j = (await c.get("/api/auth/status")).json()
            assert j["role"] == "admin" and j["kind"] == "header"
            assert (await c.get("/api/sources")).status_code == 200


# ---- tokens ----
async def test_token_lifecycle(admin, anon, app):
    r = await admin.post("/api/tokens", json={"name": "Claude desktop"})
    j = r.json()
    assert j["token"].startswith("st_") and j["token"].count("_") >= 2 and j["name"] == "Claude desktop"
    lst = (await admin.get("/api/tokens")).json()
    assert [t["name"] for t in lst] == ["Claude desktop"] and lst[0]["last_used"] is None
    assert "token" not in lst[0] and "secret_hash" not in lst[0] and j["token"] not in str(lst)
    h = {"Authorization": f"Bearer {j['token']}"}
    assert (await anon.get("/api/now", headers=h)).status_code == 200
    assert (await admin.get("/api/tokens")).json()[0]["last_used"] is not None
    assert (await anon.delete(f"/api/tokens/{j['id']}", headers=h)).status_code == 403          # a token cannot manage tokens
    assert (await admin.delete(f"/api/tokens/{j['id']}")).status_code == 200
    assert (await anon.get("/api/now", headers=h)).status_code == 401                              # revoked at once
    assert (await admin.delete(f"/api/tokens/{j['id']}")).status_code == 404


async def test_token_is_stored_hashed_and_audited(admin, core):
    j = (await admin.post("/api/tokens", json={"name": "x"})).json()
    from sqlalchemy import select

    from stormtracker.storage.tables import ApiToken
    async with core.db.session() as s:
        row = (await s.execute(select(ApiToken))).scalars().one()
    assert j["token"] not in (row.secret_hash, row.name) and len(row.secret_hash) == 64
    audit = await core.store.audit()
    assert any(a["action"] == "create_token" for a in audit) and j["token"] not in str(audit)


async def test_token_is_read_only_and_only_in_the_header(agent, anon, token):
    assert (await agent.post("/api/alerts/rules", json={})).status_code == 403
    assert (await agent.put("/api/ui", json={})).status_code == 403
    assert (await anon.get(f"/api/now?token={token}")).status_code == 401                          # never accepted in the URL
    assert (await anon.get("/api/now", headers={"Authorization": token})).status_code == 401       # needs the Bearer word


async def test_wrong_tokens_are_throttled(anon, token):
    for _ in range(30):
        assert (await anon.get("/api/now", headers={"Authorization": "Bearer st_aaaa_bbbb"})).status_code == 401
    assert (await anon.get("/api/now", headers={"Authorization": "Bearer st_aaaa_bbbb"})).status_code == 429
    assert (await anon.get("/api/now", headers={"Authorization": f"Bearer {token}"})).status_code == 429     # the address is blocked for a while


async def test_token_with_a_tampered_secret_fails(admin, anon):
    t = (await admin.post("/api/tokens", json={"name": "x"})).json()["token"]
    assert (await anon.get("/api/now", headers={"Authorization": f"Bearer {t[:-1]}{'A' if t[-1] != 'A' else 'B'}"})).status_code == 401


async def test_token_limit(admin):
    for i in range(50):
        assert (await admin.post("/api/tokens", json={"name": f"t{i}"})).status_code == 200
    assert (await admin.post("/api/tokens", json={"name": "one too many"})).status_code == 422
