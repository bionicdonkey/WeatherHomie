"""Single sign-on: token checks, role mapping, and the whole browser flow against a stub identity provider."""
import base64
import hashlib
import json
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from stormtracker.oidc import OidcError, OidcSettings, check_claims, map_role, verify_jwt

from tests.access.jwtkit import claims

ISS = "https://idp.test/app/"
SETTINGS = {"enabled": True, "label": "Sign in with Authentik", "issuer": ISS, "client_id": "stormtracker", "public_url": "https://st.test",
            "admin_groups": ["storm-admins"], "member_groups": ["storm-members"], "client_secret": "s3cret-value"}


# ---- pure checks ----
def test_verify_jwt_accepts_each_supported_algorithm(signer):
    jwks = signer.jwks()
    assert verify_jwt(signer.sign(claims(), "RS256", "r1"), jwks, None)["sub"] == "u1"
    assert verify_jwt(signer.sign(claims(), "RS256"), jwks, None)["sub"] == "u1"                   # no kid: any key that fits
    assert verify_jwt(signer.sign(claims(), "ES256", "e1"), jwks, None)["sub"] == "u1"
    assert verify_jwt(signer.sign(claims(), "HS256", secret="shh"), jwks, "shh")["sub"] == "u1"


def test_verify_jwt_rejects_forgeries(signer):
    from tests.access.jwtkit import Signer
    jwks = signer.jwks()
    other = Signer()
    for tok, secret in ((other.sign(claims(), "RS256", "r1"), None),                              # signed by someone else
                        (other.sign(claims(), "ES256", "e1"), None),
                        (signer.sign(claims(), "RS256", "nokid"), None),                          # names a key the provider doesn't have
                        (signer.sign(claims(), "HS256", secret="attacker"), "real"),
                        (signer.sign(claims(), "HS256", secret="x"), None),                       # HS256 with no client secret saved
                        (signer.sign(claims(), "none"), None),                                    # "alg: none" must never pass
                        ("a.b", None), ("not a token", None), ("", None)):
        with pytest.raises(OidcError):
            verify_jwt(tok, jwks, secret)
    good = signer.sign(claims(), "RS256", "r1")
    h, p, s = good.split(".")
    tampered = f"{h}.{base64.urlsafe_b64encode(json.dumps(claims(sub='admin-user')).encode()).rstrip(b'=').decode()}.{s}"
    with pytest.raises(OidcError):
        verify_jwt(tampered, jwks, None)
    # algorithm confusion: an HS256 token "signed" with the public key text must not be accepted as RS256
    with pytest.raises(OidcError):
        verify_jwt(signer.sign(claims(), "HS256", secret=json.dumps(jwks)), jwks, None)


def test_claim_checks():
    ok = dict(issuer=ISS, client_id="stormtracker", nonce="N")
    check_claims(claims(), **ok)
    check_claims(claims(aud=["other", "stormtracker"], azp="stormtracker"), **ok)
    for bad in (claims(iss="https://evil.test/"), claims(aud="someone-else"), claims(aud=["stormtracker", "x"]), claims(aud=["stormtracker", "x"], azp="x"),
                claims(exp=int(time.time()) - 600), claims(exp="soon"), claims(nonce="other"), claims(iat=int(time.time()) + 3600), claims(sub="")):
        with pytest.raises(OidcError):
            check_claims(bad, **ok)
    d = claims(); del d["nonce"]
    with pytest.raises(OidcError):
        check_claims(d, **ok)


def test_group_mapping():
    cfg = OidcSettings(admin_groups=["A"], member_groups=["M"])
    assert map_role(["M", "A"], cfg) == "admin" and map_role(["M"], cfg) == "member" and map_role("A", cfg) == "admin"
    assert map_role(["a", "m"], cfg) is None                                                       # exact, case-sensitive
    assert map_role([], cfg) is None and map_role(None, cfg) is None and map_role({"A": 1}, cfg) is None and map_role([1, None], cfg) is None
    assert map_role(["A"], OidcSettings(member_groups=["M"])) is None


# ---- settings API ----
async def test_settings_are_validated_and_the_secret_never_comes_back(admin, anon):
    assert (await admin.get("/api/auth/oidc/settings")).json()["enabled"] is False
    r = await admin.put("/api/auth/oidc/settings", json=SETTINGS)
    j = r.json()
    assert r.status_code == 200 and j["client_secret_set"] is True and j["redirect_uri"] == "https://st.test/api/auth/oidc/callback" and j["ready"]
    assert "s3cret-value" not in r.text and "s3cret-value" not in (await admin.get("/api/auth/oidc/settings")).text
    assert "client_secret" not in (await admin.get("/api/auth/oidc/settings")).json()
    st = (await anon.get("/api/auth/status")).json()["sso"]
    assert st == {"enabled": True, "label": "Sign in with Authentik"}
    # omitting the secret keeps it; '' removes it
    r = await admin.put("/api/auth/oidc/settings", json={k: v for k, v in SETTINGS.items() if k != "client_secret"})
    assert r.json()["client_secret_set"] is True
    r = await admin.put("/api/auth/oidc/settings", json={**SETTINGS, "client_secret": ""})
    assert r.json()["client_secret_set"] is False


async def test_settings_validation(admin):
    for bad in ({"issuer": "http://idp.example.com/"}, {"issuer": "javascript:alert(1)"}, {"issuer": "https://user:pw@idp.test/"}, {"public_url": "ftp://x"},
                {"scopes": "profile email"}, {"admin_groups": [], "member_groups": []}, {"issuer": ""}, {"client_id": ""}, {"public_url": ""}):
        r = await admin.put("/api/auth/oidc/settings", json={**SETTINGS, **bad})
        assert r.status_code == 422, bad
    assert (await admin.put("/api/auth/oidc/settings", json={**SETTINGS, "issuer": "http://localhost:9000/", "public_url": "http://localhost:8000"})).status_code == 200
    off = await admin.put("/api/auth/oidc/settings", json={"enabled": False})              # turning it off needs nothing else
    assert off.status_code == 200 and off.json()["enabled"] is False
    assert off.json()["issuer"] == "http://localhost:9000/" and off.json()["admin_groups"] == ["storm-admins"]       # and keeps the rest


async def test_sso_off_means_login_is_unavailable(anon):
    r = await anon.get("/api/auth/oidc/login")
    assert r.status_code == 503 and "turned on" in r.text
    assert (await anon.get("/api/auth/status")).json()["sso"]["enabled"] is False


# ---- the browser flow ----
class Idp:
    def __init__(self, signer):
        self.signer, self.keys, self.groups, self.alg, self.over = signer, signer, ["storm-admins"], "RS256", {}
        self.nonce = self.challenge = None
        self.userinfo, self.token_status, self.omit_groups, self.token_calls, self.plain = None, 200, False, [], False

    def handler(self, req: httpx.Request) -> httpx.Response:
        u = str(req.url)
        if u.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json={"issuer": ISS, "authorization_endpoint": "https://idp.test/authorize", "token_endpoint": "https://idp.test/token",
                                             "jwks_uri": "https://idp.test/jwks", "userinfo_endpoint": "https://idp.test/userinfo",
                                             "token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic"]})
        if u == "https://idp.test/jwks":
            return httpx.Response(200, json=self.keys.jwks())
        if u == "https://idp.test/token":
            form = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
            self.token_calls.append(form)
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": "invalid_grant"})
            verifier_ok = base64.urlsafe_b64encode(hashlib.sha256(form.get("code_verifier", "").encode()).digest()).rstrip(b"=").decode() == self.challenge
            if not (verifier_ok and form.get("client_secret") == "s3cret-value" and form.get("redirect_uri") == "https://st.test/api/auth/oidc/callback"
                    and form.get("code") == "good-code"):
                return httpx.Response(400, json={"error": "invalid_grant"})
            c = {**claims(nonce=self.nonce, preferred_username="mia", sub="u-mia"), **self.over}
            if not self.omit_groups:
                c["groups"] = self.groups
            body = {"id_token": self.signer.sign(c, self.alg, "r1" if self.alg == "RS256" else "e1", secret="s3cret-value"), "access_token": "at"}
            return httpx.Response(200, content=json.dumps(body).encode()) if self.plain else httpx.Response(200, json=body)
        if u == "https://idp.test/userinfo":
            return httpx.Response(200, json={"sub": "u-mia", "groups": self.userinfo or []})
        return httpx.Response(404)


@pytest.fixture
async def idp(app, admin, anon, signer, monkeypatch):
    i = Idp(signer)
    anon.headers["host"] = "st.test"                        # people reach StormTracker at its public address
    core = app.state.core
    monkeypatch.setattr(type(core.oidc), "_http", lambda self: httpx.AsyncClient(transport=httpx.MockTransport(i.handler)))
    assert (await admin.put("/api/auth/oidc/settings", json=SETTINGS)).status_code == 200
    return i


async def begin(anon, idp):
    r = await anon.get("/api/auth/oidc/login")
    assert r.status_code == 302, r.text
    loc = urlsplit(r.headers["location"])
    q = {k: v[0] for k, v in parse_qs(loc.query).items()}
    idp.nonce, idp.challenge = q["nonce"], q["code_challenge"]
    return r, q


async def test_login_redirect_is_a_proper_pkce_request(anon, idp):
    r, q = await begin(anon, idp)
    assert r.headers["location"].startswith("https://idp.test/authorize?")
    assert q["response_type"] == "code" and q["client_id"] == "stormtracker" and q["code_challenge_method"] == "S256"
    assert q["redirect_uri"] == "https://st.test/api/auth/oidc/callback" and q["scope"] == "openid profile email" and len(q["state"]) >= 24 and len(q["nonce"]) >= 24
    cookie = r.headers["set-cookie"].lower()
    assert "st_oidc=" in cookie and "samesite=lax" in cookie and "httponly" in cookie and "path=/api/auth/oidc" in cookie       # Lax so it survives the return trip
    assert "client_secret" not in r.headers["location"]


@pytest.mark.parametrize("groups,role", [(["storm-admins"], "admin"), (["storm-members"], "member"), (["storm-members", "storm-admins"], "admin"), ("storm-members", "member")])
async def test_full_sign_in_maps_groups_to_roles(app, anon, idp, groups, role):
    idp.groups = groups
    r, q = await begin(anon, idp)
    cb = await anon.get("/api/auth/oidc/callback", params={"code": "good-code", "state": q["state"]})
    assert cb.status_code == 200 and 'http-equiv="refresh"' in cb.text and 'url=/"' in cb.text           # a self-redirecting page, not a 302
    sc = cb.headers.get_list("set-cookie")
    sess = next(c for c in sc if c.startswith("st_session="))
    assert "samesite=strict" in sess.lower() and "httponly" in sess.lower()
    assert any(c.startswith("st_oidc=") and ("max-age=0" in c.lower() or "expires" in c.lower()) for c in sc)    # the flow cookie is cleared
    me = (await anon.get("/api/auth/status")).json()
    assert me["authenticated"] and me["role"] == role and me["kind"] == "oidc" and me["username"] == "sso:mia"
    assert (await anon.get("/api/stations")).status_code == 200
    expect_admin = 200 if role == "admin" else 403
    assert (await anon.get("/api/sources")).status_code == expect_admin
    assert idp.token_calls and idp.token_calls[-1]["client_id"] == "stormtracker"
    # signing out ends this session for good
    out = await anon.post("/api/auth/logout", headers={"X-CSRF-Token": me["csrf"]})
    assert out.status_code == 200 and (await anon.get("/api/stations")).status_code == 401


async def test_user_in_no_mapped_group_is_refused(anon, idp):
    idp.groups = ["something-else"]
    _, q = await begin(anon, idp)
    cb = await anon.get("/api/auth/oidc/callback", params={"code": "good-code", "state": q["state"]})
    assert cb.status_code == 403 and "isn&#x27;t in a group" in cb.text.replace("’", "&#x27;") or "isn't in a group" in cb.text
    assert not any(c.startswith("st_session=") for c in cb.headers.get_list("set-cookie"))
    assert (await anon.get("/api/auth/status")).json()["authenticated"] is False
    idp.groups = []
    _, q = await begin(anon, idp)
    assert (await anon.get("/api/auth/oidc/callback", params={"code": "good-code", "state": q["state"]})).status_code == 403


async def test_groups_can_come_from_userinfo(anon, idp):
    idp.omit_groups, idp.userinfo = True, ["storm-members"]
    _, q = await begin(anon, idp)
    assert (await anon.get("/api/auth/oidc/callback", params={"code": "good-code", "state": q["state"]})).status_code == 200
    assert (await anon.get("/api/auth/status")).json()["role"] == "member"


@pytest.mark.parametrize("over", [{"iss": "https://evil.test/"}, {"aud": "other-app"}, {"exp": 1}, {"nonce": "replayed"}])
async def test_bad_id_tokens_are_refused(anon, idp, over):
    idp.over = over
    _, q = await begin(anon, idp)
    idp.over = over if "nonce" not in over else {}
    if "nonce" in over:
        idp.nonce = "replayed-from-another-login"
    cb = await anon.get("/api/auth/oidc/callback", params={"code": "good-code", "state": q["state"]})
    assert cb.status_code == 401 and not any(c.startswith("st_session=") for c in cb.headers.get_list("set-cookie"))


async def test_state_and_flow_cookie_are_enforced(anon, idp, app):
    _, q = await begin(anon, idp)
    assert (await anon.get("/api/auth/oidc/callback", params={"code": "good-code", "state": "forged"})).status_code == 401
    assert (await anon.get("/api/auth/oidc/callback", params={"code": "good-code"})).status_code == 401
    assert (await anon.get("/api/auth/oidc/callback", params={"state": q["state"]})).status_code == 401
    app.state.core.auth.login_throttle._fails.clear()
    anon.cookies.clear()                                                                       # no flow cookie: it didn't start in this browser
    assert (await anon.get("/api/auth/oidc/callback", params={"code": "good-code", "state": q["state"]})).status_code == 401
    anon.cookies.set("st_oidc", "tampered.value", path="/api/auth/oidc")
    assert (await anon.get("/api/auth/oidc/callback", params={"code": "good-code", "state": q["state"]})).status_code == 401
    assert (await anon.get("/api/auth/oidc/callback", params={"error": "access_denied"})).status_code == 401


async def test_provider_errors_are_explained(anon, idp):
    _, q = await begin(anon, idp)
    idp.token_status = 400
    cb = await anon.get("/api/auth/oidc/callback", params={"code": "good-code", "state": q["state"]})
    assert cb.status_code == 401 and "refused the sign-in" in cb.text and "s3cret" not in cb.text


async def test_a_tampered_id_token_signature_is_refused(anon, idp, signer):
    from tests.access.jwtkit import Signer
    idp.signer = Signer()                                                    # the token endpoint signs with a key the jwks doesn't list
    _, q = await begin(anon, idp)
    cb = await anon.get("/api/auth/oidc/callback", params={"code": "good-code", "state": q["state"]})
    assert cb.status_code == 401


async def test_failed_sign_ins_are_throttled(anon, idp):
    for _ in range(5):
        assert (await anon.get("/api/auth/oidc/callback", params={"code": "x", "state": "y"})).status_code == 401
    assert (await anon.get("/api/auth/oidc/callback", params={"code": "x", "state": "y"})).status_code == 429
    assert (await anon.get("/api/auth/oidc/login")).status_code == 429


async def test_test_button(admin, idp):
    j = (await admin.post("/api/auth/oidc/test")).json()
    assert j["ok"] and j["signing_keys"] == 2 and j["redirect_uri"] == "https://st.test/api/auth/oidc/callback"


async def test_password_login_still_works_with_sso_on(admin, idp, anon):
    assert (await anon.post("/api/auth/login", json={"username": "admin", "password": "correct-horse-battery"})).status_code == 200


async def test_provider_that_forgets_the_json_content_type_still_works(anon, idp):
    idp.plain = True
    _, q = await begin(anon, idp)
    assert (await anon.get("/api/auth/oidc/callback", params={"code": "good-code", "state": q["state"]})).status_code == 200


async def test_es256_and_userinfo_free_provider(anon, idp):
    idp.alg = "ES256"
    _, q = await begin(anon, idp)
    assert (await anon.get("/api/auth/oidc/callback", params={"code": "good-code", "state": q["state"]})).status_code == 200


async def test_sign_in_started_on_another_host_restarts_on_the_public_address(anon, idp):
    """The provider returns the browser to public_url, so the flow cookie has to be set there, not on the LAN address."""
    anon.headers["host"] = "192.168.1.10:8000"
    r = await anon.get("/api/auth/oidc/login")
    assert r.status_code == 302 and r.headers["location"] == "https://st.test/api/auth/oidc/login?via=public"
    assert "st_oidc" not in r.headers.get("set-cookie", "")
    r2 = await anon.get("/api/auth/oidc/login?via=public")                               # no loop if a proxy hides the original Host
    assert r2.status_code == 302 and r2.headers["location"].startswith("https://idp.test/authorize")
    anon.headers["host"] = "st.test"
    assert (await anon.get("/api/auth/oidc/login")).headers["location"].startswith("https://idp.test/authorize")


async def test_members_do_not_see_delivery_details(app, member, admin):
    from datetime import UTC, datetime

    from stormtracker.storage.tables import AlertLogRow
    async with app.state.core.db.session() as s:
        s.add(AlertLogRow(ts=datetime.now(UTC), rule_id="r", rule_name="Rule", key="k", kind="alert", severity="info", title="T", message="M", data={},
                          results={"webhook": {"ok": False, "error": "connect failed"}}))
        await s.commit()
    assert "results" in (await admin.get("/api/alerts/history")).json()[0]
    m = (await member.get("/api/alerts/history")).json()
    assert m and "results" not in m[0] and "connect failed" not in str(m)


async def test_validation_errors_do_not_echo_what_was_sent(admin):
    r = await admin.put("/api/auth/oidc/settings", json={"client_id": "x" * 500, "client_secret": "y" * 600})
    assert r.status_code == 422 and "y" * 20 not in r.text and "x" * 20 not in r.text
    assert r.json()["detail"][0]["msg"]
