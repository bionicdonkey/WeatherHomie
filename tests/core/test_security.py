import time

import pytest
from httpx import ASGITransport, AsyncClient

from stormtracker.auth import COOKIE, Auth, FailureThrottle
from stormtracker.sources.ecowitt.plugin import Ecowitt

PW = "correct-horse-battery"


async def anon(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ---- headers --------------------------------------------------------------------------------------------------------
async def test_security_headers_on_the_ui_and_the_api(admin, client):
    for path in ("/", "/js/main.js", "/api/health", "/api/sources"):
        r = await admin.get(path)
        assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-frame-options"] == "DENY", path
        assert r.headers["referrer-policy"] == "same-origin" and "geolocation=()" in r.headers["permissions-policy"], path
        csp = r.headers["content-security-policy"]
        assert "default-src 'self'" in csp and "script-src 'self'" in csp and "frame-ancestors 'none'" in csp and "object-src 'none'" in csp, path
        assert "'unsafe-eval'" not in csp and "script-src 'self' 'unsafe" not in csp, path          # scripts: our own files only
    assert (await admin.get("/api/sources")).headers["cache-control"] == "no-store"
    docs = await admin.get("/docs")                                                                  # FastAPI's docs page loads a CDN script: no CSP there
    assert docs.status_code == 200 and "content-security-policy" not in docs.headers and docs.headers["x-content-type-options"] == "nosniff"


def test_no_inline_script_in_the_ui_shell():
    from pathlib import Path
    html = (Path(__file__).parents[2] / "src" / "stormtracker" / "web" / "index.html").read_text()
    import re
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html), "an inline <script> would be blocked by the CSP"
    assert not re.search(r"\son\w+=", html)


# ---- sessions --------------------------------------------------------------------------------------------------------------
async def test_logout_really_ends_the_session_even_for_a_copied_cookie(app, admin):
    stolen = admin.cookies.get(COOKIE)
    csrf = admin.headers["X-CSRF-Token"]
    async with await anon(app) as other:                                                              # someone who copied the cookie
        other.cookies.set(COOKIE, stolen)
        assert (await other.get("/api/sources")).status_code == 200
        assert (await admin.post("/api/auth/logout")).status_code == 200
        assert (await other.get("/api/sources")).status_code == 401                                   # the token itself is dead
    assert csrf                                                                                        # (and the browser's cookie was cleared too)


async def test_revocation_survives_a_restart(app, admin):
    stolen = admin.cookies.get(COOKIE)
    core = app.state.core
    await admin.post("/api/auth/logout")
    fresh = Auth(core.boot, core.db, core.box.session_secret, core.store)                              # a restarted process: memory is gone
    await fresh.init()
    assert len(fresh.revoked) == 1
    d = fresh.ser.loads(stolen)
    assert d["n"] in fresh.revoked


async def test_change_password_ends_every_session_and_checks_the_current_one(app, admin):
    other_browser = admin.cookies.get(COOKIE)
    bad = await admin.post("/api/auth/password", json={"current_password": "wrong-password-xx", "new_password": "a-brand-new-passphrase"})
    assert bad.status_code == 422 and "current password is wrong" in bad.text
    short = await admin.post("/api/auth/password", json={"current_password": PW, "new_password": "short"})
    assert short.status_code == 422 and "at least 12 characters" in short.text
    r = await admin.post("/api/auth/password", json={"current_password": PW, "new_password": "a-brand-new-passphrase"})
    assert r.status_code == 200, r.text
    async with await anon(app) as c:
        c.cookies.set(COOKIE, other_browser)
        assert (await c.get("/api/sources")).status_code == 401                                        # every older session is gone
        assert (await c.post("/api/auth/login", json={"username": "admin", "password": PW})).status_code == 401              # old password refused
        ok = await c.post("/api/auth/login", json={"username": "admin", "password": "a-brand-new-passphrase"})
        assert ok.status_code == 200
        c.headers["X-CSRF-Token"] = ok.json()["csrf"]
        assert (await c.get("/api/sources")).status_code == 200
    core = app.state.core
    fresh = Auth(core.boot, core.db, core.box.session_secret, core.store)
    await fresh.init()
    assert fresh.epoch == core.auth.epoch == 1                                                          # persisted: a restart does not revive old sessions
    assert "a-brand-new-passphrase" not in (await core.store.get("auth.revoked", {})).__repr__()


# ---- throttling -------------------------------------------------------------------------------------------------------------
def test_failure_throttle_blocks_then_forgets_and_does_not_grow(monkeypatch):
    now = {"t": 1000.0}
    monkeypatch.setattr(time, "monotonic", lambda: now["t"])
    t = FailureThrottle(limit=3, window=300)
    for _ in range(3):
        assert not t.blocked("1.2.3.4")
        t.fail("1.2.3.4")
    assert t.blocked("1.2.3.4") and not t.blocked("5.6.7.8")
    now["t"] += 301                                                                                    # the window passes
    assert not t.blocked("1.2.3.4")
    for i in range(500):                                                                               # a scan from many addresses...
        t.fail(f"10.0.{i // 250}.{i % 250}")
    now["t"] += 700
    assert not t.blocked("10.0.0.1") and len(t._fails) <= 1                                            # ...is swept away, not remembered forever


async def test_guessing_ingest_tokens_gets_you_blocked(admin, app):
    app.state.core.registry.register(Ecowitt)
    await admin.put("/api/locations", json=[{"id": "home", "name": "Home", "lat": -33.0, "lon": 151.0}])
    await admin.put("/api/sources/ecowitt", json={"enabled": True, "secrets": {"push_token": "tok-" + "a" * 20}})
    async with await anon(app) as dev:
        codes = [(await dev.post(f"/ingest/ecowitt/guess{i}", content="tempf=60")).status_code for i in range(35)]
    assert codes[:30] == [403] * 30 and set(codes[30:]) == {429}


async def test_login_throttle_still_works(app):
    async with await anon(app) as c:
        codes = [(await c.post("/api/auth/login", json={"username": "admin", "password": f"wrong-{i}"})).status_code for i in range(7)]
    assert codes[:5] == [401] * 5 and codes[5:] == [429, 429]


# ---- the client address behind a reverse proxy ------------------------------------------------------------------------------
def test_client_ip_trusts_forwarded_for_only_from_a_trusted_proxy():
    from starlette.requests import Request
    from stormtracker.auth import client_ip

    def req(peer, xff=None):
        headers = [(b"x-forwarded-for", xff.encode())] if xff else []
        return Request({"type": "http", "headers": headers, "client": (peer, 1234), "method": "GET", "path": "/", "query_string": b""})
    trusted = ("172.18.0.1", "172.18.0.2")
    assert client_ip(req("203.0.113.9", "1.2.3.4"), trusted) == "203.0.113.9"              # not a proxy: the header is just a claim
    assert client_ip(req("172.18.0.1", "198.51.100.7"), trusted) == "198.51.100.7"          # a trusted proxy reports the real caller
    assert client_ip(req("172.18.0.1", "6.6.6.6, 198.51.100.7, 172.18.0.2"), trusted) == "198.51.100.7"   # spoofed left entries are ignored
    assert client_ip(req("172.18.0.1"), trusted) == "172.18.0.1" and client_ip(req("172.18.0.1", "172.18.0.2"), trusted) == "172.18.0.1"
    assert client_ip(req("10.0.0.5", "1.2.3.4"), ()) == "10.0.0.5"


async def test_one_visitor_failing_does_not_lock_out_everyone_behind_the_proxy(app):
    core = app.state.core
    core.boot.__dict__["trusted_proxies"] = ("127.0.0.1", "testclient")                      # the test transport reports its peer as "127.0.0.1"
    async with AsyncClient(transport=ASGITransport(app=app, client=("127.0.0.1", 5000)), base_url="http://test") as c:
        for _ in range(6):
            await c.post("/api/auth/login", json={"username": "admin", "password": "nope-nope-nope"}, headers={"X-Forwarded-For": "198.51.100.7"})
        assert (await c.post("/api/auth/login", json={"username": "admin", "password": "nope-nope-nope"}, headers={"X-Forwarded-For": "198.51.100.7"})).status_code == 429
        ok = await c.post("/api/auth/login", json={"username": "admin", "password": "correct-horse-battery"}, headers={"X-Forwarded-For": "203.0.113.50"})
        assert ok.status_code == 200                                                         # a different caller behind the same proxy is unaffected


async def test_sessions_from_before_nonces_existed_are_refused_after_the_upgrade(app, admin):
    core = app.state.core
    old = core.ser.dumps({"u": "admin", "c": "csrf-old"}) if hasattr(core, "ser") else core.auth.ser.dumps({"u": "admin", "c": "csrf-old"})   # the old token shape: no nonce, no epoch
    async with await anon(app) as c:
        c.cookies.set(COOKIE, old)
        assert (await c.get("/api/sources")).status_code == 401                                   # one fresh sign-in after upgrading, then revocation works
    assert (await admin.get("/api/sources")).status_code == 200


async def test_a_too_short_bootstrap_password_falls_back_to_the_setup_token_instead_of_crashing(tmp_path, registry):
    from conftest import make_boot
    from stormtracker.main import create_app
    app = create_app(make_boot(tmp_path, admin_password="short-pw"), registry)
    async with app.router.lifespan_context(app):                                                   # used to raise ValueError here: a restart loop
        assert app.state.core.auth.setup_token is not None
        async with await anon(app) as c:
            assert (await c.get("/api/auth/status")).json()["setup_required"] is True
