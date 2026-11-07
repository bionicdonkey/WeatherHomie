"""OpenID Connect sign-in (authorization code flow with PKCE) with group-to-role mapping.

Works with any standards-following provider (authentik, Keycloak, Authelia, Google...). Needs no extra library: ``httpx`` talks
to the provider and ``cryptography`` checks the ID token's signature.

* Settings live in the database and are edited in the UI. The client secret is stored encrypted and is write-only.
* The person must be in at least one of the admin or member groups, otherwise they are refused (never defaulted to member).
* The role is decided at sign-in; single sign-on sessions are shorter than password sessions so a group change takes effect soon.
* Password sign-in stays available as the way back in if the provider is down or misconfigured.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa, utils
from itsdangerous import BadSignature, URLSafeTimedSerializer
from pydantic import BaseModel, Field, field_validator

log = logging.getLogger("stormtracker.oidc")
KEY = "auth.oidc"
SECRET = "auth.oidc.client_secret"
FLOW_COOKIE = "st_oidc"
FLOW_MAX_AGE = 600
CLOCK_SKEW = 60
CACHE_S = 3600
_LOCAL = {"localhost", "127.0.0.1", "::1"}


class OidcError(Exception):
    """Something to tell the person signing in, in plain words."""


class OidcSettings(BaseModel):
    enabled: bool = False
    label: str = Field("Single sign-on", max_length=40, description="Text on the sign-in button")
    issuer: str = Field("", max_length=300, description="The provider's issuer address. For authentik it is the application's OpenID configuration issuer, usually ending in /application/o/<slug>/")
    client_id: str = Field("", max_length=200)
    public_url: str = Field("", max_length=300, description="The address people use to reach StormTracker (https). The provider sends them back here.")
    scopes: str = Field("openid profile email", max_length=200, description="Must include openid. Most providers send groups with profile.")
    username_claim: str = Field("preferred_username", max_length=60)
    groups_claim: str = Field("groups", max_length=60)
    admin_groups: list[str] = Field(default_factory=list, max_length=20, description="People in any of these groups get the admin role")
    member_groups: list[str] = Field(default_factory=list, max_length=20, description="People in any of these groups (and none of the admin groups) get the member role: they can view the weather but change nothing")

    @field_validator("issuer", "public_url")
    @classmethod
    def _url(cls, v: str) -> str:
        v = v.strip()
        if not v:
            return v
        u = urlsplit(v)
        if u.scheme not in ("http", "https") or not u.hostname or u.username or u.query or u.fragment:
            raise ValueError("must be an http(s) address with no login details, query or fragment")
        if u.scheme == "http" and u.hostname not in _LOCAL:
            raise ValueError("must use https (plain http is only accepted for localhost)")
        return v

    @field_validator("scopes")
    @classmethod
    def _scopes(cls, v: str) -> str:
        parts = v.split()
        if "openid" not in parts:
            raise ValueError("scopes must include openid")
        return " ".join(parts)

    @field_validator("admin_groups", "member_groups")
    @classmethod
    def _groups(cls, v: list[str]) -> list[str]:
        out = [g.strip() for g in v if g and g.strip()]
        if any(len(g) > 200 for g in out):
            raise ValueError("group names are limited to 200 characters")
        return list(dict.fromkeys(out))

    def redirect_uri(self) -> str:
        return self.public_url.rstrip("/") + "/api/auth/oidc/callback"

    def complete(self) -> bool:
        return bool(self.issuer and self.client_id and self.public_url and (self.admin_groups or self.member_groups))


# ---- tokens ---------------------------------------------------------------------------------------------
def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _int(s: str) -> int:
    return int.from_bytes(_b64d(s), "big")


def verify_jwt(token: str, jwks: dict[str, Any], client_secret: str | None) -> dict[str, Any]:
    """The claims of a signed JWT, or OidcError. RS256 and ES256 against the provider's keys; HS256 against the client secret."""
    try:
        h, p, sig = token.split(".")
        header, claims = json.loads(_b64d(h)), json.loads(_b64d(p))
        signature = _b64d(sig)
    except (ValueError, TypeError):
        raise OidcError("the provider sent an ID token that couldn't be read") from None
    if not isinstance(header, dict) or not isinstance(claims, dict):
        raise OidcError("the provider sent an ID token that couldn't be read")
    alg, signing_input = header.get("alg"), f"{h}.{p}".encode()
    ok = False
    if alg == "HS256":
        if not client_secret:
            raise OidcError("the provider signed the ID token with the client secret, but none is saved here")
        ok = hmac.compare_digest(hmac.new(client_secret.encode(), signing_input, hashlib.sha256).digest(), signature)
    elif alg in ("RS256", "ES256"):
        kty, kid = ("RSA" if alg == "RS256" else "EC"), header.get("kid")
        for k in (jwks.get("keys") or []):
            if k.get("kty") != kty or k.get("use") not in (None, "sig") or (kid is not None and k.get("kid") != kid):
                continue
            try:
                if kty == "RSA":
                    rsa.RSAPublicNumbers(_int(k["e"]), _int(k["n"])).public_key().verify(signature, signing_input, padding.PKCS1v15(), hashes.SHA256())
                else:
                    if k.get("crv") != "P-256" or len(signature) != 64:
                        continue
                    key = ec.EllipticCurvePublicNumbers(_int(k["x"]), _int(k["y"]), ec.SECP256R1()).public_key()
                    key.verify(utils.encode_dss_signature(int.from_bytes(signature[:32], "big"), int.from_bytes(signature[32:], "big")), signing_input, ec.ECDSA(hashes.SHA256()))
                ok = True
                break
            except (InvalidSignature, KeyError, ValueError, TypeError):
                continue
    else:
        raise OidcError(f"the ID token uses a signing method ({alg}) this app doesn't accept")
    if not ok:
        raise OidcError("the ID token's signature didn't check out")
    return claims


def check_claims(c: dict[str, Any], *, issuer: str, client_id: str, nonce: str, now: float | None = None) -> None:
    now = time.time() if now is None else now
    if c.get("iss") != issuer:
        raise OidcError("the ID token came from a different issuer than the one configured")
    aud = c.get("aud")
    auds = aud if isinstance(aud, list) else [aud]
    if client_id not in auds:
        raise OidcError("the ID token wasn't issued for this app (client ID mismatch)")
    if len(auds) > 1 and c.get("azp") != client_id:
        raise OidcError("the ID token's authorised party doesn't match this app")
    exp = c.get("exp")
    if not isinstance(exp, (int, float)) or exp < now - CLOCK_SKEW:
        raise OidcError("the ID token has expired")
    iat = c.get("iat")
    if isinstance(iat, (int, float)) and iat > now + 300:
        raise OidcError("the ID token claims to be issued in the future; check the clocks on both servers")
    if not isinstance(c.get("nonce"), str) or not hmac.compare_digest(c["nonce"], nonce):
        raise OidcError("the sign-in response didn't match the request (nonce)")
    if not c.get("sub"):
        raise OidcError("the ID token has no subject")


def map_role(groups: Any, cfg: OidcSettings) -> str | None:
    have = {groups} if isinstance(groups, str) else {g for g in (groups or []) if isinstance(g, str)} if isinstance(groups, list) else set()
    if have & set(cfg.admin_groups):
        return "admin"
    if have & set(cfg.member_groups):
        return "member"
    return None


# ---- the service ----------------------------------------------------------------------------------------
class Oidc:
    def __init__(self, core) -> None:
        self.core = core
        self.flow = URLSafeTimedSerializer(core.box.session_secret, salt="stormtracker-oidc-flow")
        self._disc: dict[str, tuple[float, dict]] = {}
        self._jwks: dict[str, tuple[float, dict]] = {}

    async def settings(self) -> OidcSettings:
        try:
            return OidcSettings(**(await self.core.store.get(KEY, {})))
        except ValueError:
            return OidcSettings()

    async def save(self, cfg: OidcSettings, client_secret: str | None, actor: str) -> None:
        await self.core.store.set(KEY, cfg.model_dump(), actor=actor)
        if client_secret is not None:
            if client_secret == "":
                await self.core.store.delete_secret(SECRET, actor=actor)
            else:
                await self.core.store.set_secret(SECRET, client_secret, actor=actor)
        self._disc.clear(); self._jwks.clear()

    def forget(self) -> None:
        """Drop cached provider details (after settings are replaced)."""
        self._disc.clear(); self._jwks.clear()

    async def public_info(self) -> dict[str, Any]:
        cfg = await self.settings()
        on = cfg.enabled and cfg.complete()
        return {"enabled": on, "label": cfg.label if on else None}

    def _http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(headers={"User-Agent": self.core.boot.user_agent}, timeout=15, follow_redirects=False)

    async def discovery(self, cfg: OidcSettings, force: bool = False) -> dict[str, Any]:
        hit = self._disc.get(cfg.issuer)
        if hit and not force and time.monotonic() - hit[0] < CACHE_S:
            return hit[1]
        url = cfg.issuer.rstrip("/") + "/.well-known/openid-configuration"
        try:
            async with self._http() as http:
                r = await http.get(url)
                r.raise_for_status()
                d = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise OidcError(f"couldn't read the provider's configuration ({type(e).__name__}); check the issuer address") from None
        if not isinstance(d, dict) or str(d.get("issuer", "")).rstrip("/") != cfg.issuer.rstrip("/"):
            raise OidcError("the provider's issuer doesn't match the one configured")
        for k in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            u = urlsplit(str(d.get(k, "")))
            if u.scheme not in ("http", "https") or not u.hostname or (u.scheme == "http" and u.hostname not in _LOCAL):
                raise OidcError(f"the provider's {k.replace('_', ' ')} isn't a usable https address")
        self._disc[cfg.issuer] = (time.monotonic(), d)
        return d

    async def keys(self, disc: dict[str, Any], force: bool = False) -> dict[str, Any]:
        uri = disc["jwks_uri"]
        hit = self._jwks.get(uri)
        if hit and not force and time.monotonic() - hit[0] < CACHE_S:
            return hit[1]
        try:
            async with self._http() as http:
                r = await http.get(uri)
                r.raise_for_status()
                j = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise OidcError(f"couldn't fetch the provider's signing keys ({type(e).__name__})") from None
        self._jwks[uri] = (time.monotonic(), j)
        return j

    async def check(self) -> dict[str, Any]:
        """For the Test button: can we read the provider's configuration and keys?"""
        cfg = await self.settings()
        if not cfg.issuer:
            raise OidcError("set the issuer address first")
        d = await self.discovery(cfg, force=True)
        j = await self.keys(d, force=True)
        return {"ok": True, "issuer": d["issuer"], "signing_keys": len(j.get("keys") or []), "has_userinfo": bool(d.get("userinfo_endpoint")),
                "supports_pkce": "S256" in (d.get("code_challenge_methods_supported") or ["S256"]), "redirect_uri": cfg.redirect_uri() if cfg.public_url else None}

    # ---- sign-in -------------------------------------------------------------------------------------
    async def begin(self) -> tuple[str, str]:
        """(address to send the browser to, signed flow cookie value)."""
        cfg = await self.settings()
        if not (cfg.enabled and cfg.complete()):
            raise OidcError("Single sign-on is not turned on")
        d = await self.discovery(cfg)
        state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
        q = {"response_type": "code", "client_id": cfg.client_id, "redirect_uri": cfg.redirect_uri(), "scope": cfg.scopes, "state": state, "nonce": nonce,
             "code_challenge": _b64e(hashlib.sha256(verifier.encode()).digest()), "code_challenge_method": "S256"}
        sep = "&" if "?" in d["authorization_endpoint"] else "?"
        return d["authorization_endpoint"] + sep + urlencode(q), self.flow.dumps({"s": state, "n": nonce, "v": verifier})

    async def finish(self, cookie: str | None, code: str | None, state: str | None) -> tuple[str, str]:
        """(username, role) for a valid sign-in. Raises OidcError with a readable reason otherwise."""
        cfg = await self.settings()
        if not (cfg.enabled and cfg.complete()):
            raise OidcError("Single sign-on is not turned on")
        try:
            flow = self.flow.loads(cookie or "", max_age=FLOW_MAX_AGE)
        except BadSignature:
            raise OidcError("this sign-in attempt expired or didn't start here. Start again from the sign-in page") from None
        if not code or not state or not hmac.compare_digest(state, flow["s"]):
            raise OidcError("the sign-in response didn't match the request. Start again from the sign-in page")
        d = await self.discovery(cfg)
        secret = await self.core.store.get_secret(SECRET)
        form = {"grant_type": "authorization_code", "code": code, "redirect_uri": cfg.redirect_uri(), "client_id": cfg.client_id, "code_verifier": flow["v"]}
        auth = None
        if secret:
            if "client_secret_post" in (d.get("token_endpoint_auth_methods_supported") or ["client_secret_post"]):
                form["client_secret"] = secret
            else:
                auth = httpx.BasicAuth(cfg.client_id, secret)
        try:
            async with self._http() as http:
                r = await http.post(d["token_endpoint"], data=form, auth=auth, headers={"Accept": "application/json"})
                try:
                    tok = r.json()
                except ValueError:
                    tok = {}
                if not isinstance(tok, dict):
                    tok = {}
                if r.status_code != 200:
                    log.warning("OIDC token exchange refused: %s %s", r.status_code, str(tok.get("error"))[:80])
                    raise OidcError(f"the provider refused the sign-in ({tok.get('error') or r.status_code}). Check the client ID, client secret and redirect address")
            idt = tok.get("id_token")
            if not isinstance(idt, str):
                raise OidcError("the provider didn't return an ID token; make sure the openid scope is allowed")
            try:
                claims = verify_jwt(idt, await self.keys(d), secret)
            except OidcError:
                claims = verify_jwt(idt, await self.keys(d, force=True), secret)       # the provider may have rotated its keys
            check_claims(claims, issuer=d["issuer"], client_id=cfg.client_id, nonce=flow["n"])
            groups = claims.get(cfg.groups_claim)
            if groups is None and d.get("userinfo_endpoint") and isinstance(tok.get("access_token"), str):
                async with self._http() as http:
                    ur = await http.get(d["userinfo_endpoint"], headers={"Authorization": f"Bearer {tok['access_token']}"})
                    info = ur.json() if ur.status_code == 200 else {}
                if isinstance(info, dict) and info.get("sub") == claims["sub"]:
                    groups = info.get(cfg.groups_claim)
                    for k in (cfg.username_claim, "email"):
                        claims.setdefault(k, info.get(k))
        except (httpx.HTTPError, ValueError) as e:
            raise OidcError(f"couldn't complete the sign-in with the provider ({type(e).__name__})") from None
        role = map_role(groups, cfg)
        name = str(claims.get(cfg.username_claim) or claims.get("email") or claims["sub"])[:80]
        if role is None:
            log.info("OIDC sign-in refused for %s: not in an allowed group", name)
            raise OidcError(f"{name} isn't in a group that's allowed to use StormTracker. Ask an admin to add you to one.")
        return name, role
