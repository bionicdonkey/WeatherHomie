"""Who can get in: read-only API tokens, single sign-on (OpenID Connect) settings and its sign-in flow, and ``/api/now``."""
from __future__ import annotations

import html
import logging
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from .. import summary
from ..auth import COOKIE, SSO_SESSION_AGE, Principal
from ..oidc import FLOW_COOKIE, FLOW_MAX_AGE, OidcError, OidcSettings

log = logging.getLogger("stormtracker.access")


class TokenIn(BaseModel):
    name: str = Field(min_length=1, max_length=80, pattern=r"^[^\x00-\x1f]+$", description="What the token is for, e.g. 'Claude on the desktop'")


class OidcIn(OidcSettings):
    client_secret: str | None = Field(None, max_length=500, description="write-only; omit to keep the saved one, '' to remove it")


def _page(title: str, body: str, refresh_to: str | None = None, status: int = 200) -> HTMLResponse:
    meta = f'<meta http-equiv="refresh" content="0;url={html.escape(refresh_to)}">' if refresh_to else ""
    doc = ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
           f'{meta}<title>{html.escape(title)} – StormTracker</title>'
           '<style>body{font:16px/1.5 system-ui,sans-serif;max-width:30rem;margin:12vh auto;padding:0 20px;color:#1d2530}'
           '@media(prefers-color-scheme:dark){body{background:#12171d;color:#e6ebf1}a{color:#8cc4ff}}</style></head>'
           f'<body><h1>{html.escape(title)}</h1>{body}</body></html>')
    return HTMLResponse(doc, status_code=status, headers={"Cache-Control": "no-store"})


def build_access_router(core) -> APIRouter:
    r = APIRouter(prefix="/api")
    admin = Depends(core.auth.dependency("admin"))
    viewer = Depends(core.auth.dependency("viewer"))

    # ---- the one-call summary ----
    @r.get("/now")
    async def now(sections: str | None = Query(None, max_length=200, description=f"comma-separated subset of: {', '.join(summary.SECTIONS)}"),
                  p: Principal = viewer):
        """Everything about the weather at home in one document: values with units, ages and sources. Read-only; works with an API token."""
        want = [s.strip() for s in sections.split(",") if s.strip()] if sections else None
        if want and (bad := [s for s in want if s not in summary.SECTIONS]):
            raise HTTPException(422, f"unknown section(s): {', '.join(bad)}")
        return await summary.build(core, want)

    # ---- API tokens (managed by an admin signed in to the UI; a token cannot manage tokens) ----
    @r.get("/tokens")
    async def list_tokens(p: Principal = admin):
        return await core.auth.tokens.list()

    @r.post("/tokens")
    async def create_token(body: TokenIn, p: Principal = admin):
        try:
            row, token = await core.auth.tokens.create(body.name.strip(), p.username)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        return {**row, "token": token, "note": "Copy this now. It is shown once and can't be recovered; revoke it and make a new one if lost."}

    @r.delete("/tokens/{token_id}")
    async def revoke_token(token_id: str, p: Principal = admin):
        if not await core.auth.tokens.delete(token_id, p.username):
            raise HTTPException(404, "no such token")
        return {"ok": True}

    # ---- single sign-on settings ----
    async def oidc_view() -> dict:
        cfg = await core.oidc.settings()
        return {**cfg.model_dump(), "client_secret_set": await core.store.has_secret("auth.oidc.client_secret"),
                "redirect_uri": cfg.redirect_uri() if cfg.public_url else None, "ready": cfg.complete(),
                "session_hours": SSO_SESSION_AGE // 3600}

    @r.get("/auth/oidc/settings")
    async def get_oidc(p: Principal = admin):
        return await oidc_view()

    @r.put("/auth/oidc/settings")
    async def put_oidc(body: OidcIn, p: Principal = admin):
        # fields left out keep their saved value, so {"enabled": false} switches it off without wiping the rest
        cfg = OidcSettings(**{**(await core.oidc.settings()).model_dump(), **body.model_dump(exclude={"client_secret"}, exclude_unset=True)})
        if cfg.enabled and not cfg.complete():
            raise HTTPException(422, "to turn single sign-on on, fill in the issuer, client ID, StormTracker's address and at least one admin or member group")
        await core.oidc.save(cfg, body.client_secret, p.username)
        return await oidc_view()

    @r.post("/auth/oidc/test")
    async def test_oidc(p: Principal = admin):
        try:
            return await core.oidc.check()
        except OidcError as e:
            return {"ok": False, "error": str(e)}

    # ---- the browser flow (public: this is how people sign in) ----
    @r.get("/auth/oidc/login")
    async def oidc_login(request: Request, via: str | None = Query(None, max_length=10)):
        ip = core.auth.ip(request)
        if core.auth.login_throttle.blocked(ip):
            return _page("Too many attempts", "<p>Wait a few minutes, then try again.</p>", status=429)
        cfg = await core.oidc.settings()
        # The provider sends the browser back to public_url, and the sign-in cookie must be waiting on that same host. If this
        # page was reached some other way (the server's LAN address, say), restart the sign-in on the public address. The
        # marker stops a loop when a proxy doesn't pass the original Host header.
        if cfg.enabled and cfg.public_url and "via" not in request.query_params and request.headers.get("host") != urlsplit(cfg.public_url).netloc:
            return RedirectResponse(cfg.public_url.rstrip("/") + "/api/auth/oidc/login?via=public", status_code=302)
        try:
            url, cookie = await core.oidc.begin()
        except OidcError as e:
            return _page("Can't sign in", f"<p>{html.escape(str(e))}</p><p><a href=\"/\">Back to StormTracker</a></p>", status=503)
        resp = RedirectResponse(url, status_code=302)
        # Lax, not Strict: the provider sends the browser back from another site, and Strict cookies are not sent on that return trip.
        resp.set_cookie(FLOW_COOKIE, cookie, max_age=FLOW_MAX_AGE, httponly=True, samesite="lax", secure=core.boot.cookie_secure, path="/api/auth/oidc")
        return resp

    @r.get("/auth/oidc/callback")
    async def oidc_callback(request: Request, code: str | None = Query(None, max_length=2000), state: str | None = Query(None, max_length=200),
                            error: str | None = Query(None, max_length=100)):
        ip = core.auth.ip(request)
        if core.auth.login_throttle.blocked(ip):
            return _page("Too many attempts", "<p>Wait a few minutes, then try again.</p>", status=429)

        def fail(msg: str, status: int = 401) -> HTMLResponse:
            core.auth.login_throttle.fail(ip)
            resp = _page("Couldn't sign you in", f"<p>{html.escape(msg)}</p><p><a href=\"/\">Back to the sign-in page</a></p>", status=status)
            resp.delete_cookie(FLOW_COOKIE, path="/api/auth/oidc")
            return resp
        if error:
            return fail("The provider declined the sign-in." if error == "access_denied" else f"The provider reported a problem ({error}).")
        try:
            name, role = await core.oidc.finish(request.cookies.get(FLOW_COOKIE), code, state)
        except OidcError as e:
            return fail(str(e), 403 if "allowed to use" in str(e) else 401)
        tok, _ = core.auth.issue(f"sso:{name}", role, "oidc", SSO_SESSION_AGE)
        # A page that redirects itself, not a 302: the session cookie is SameSite=Strict, and a browser does not send it on a
        # navigation that began at another site, so a 302 would land on the sign-in page. A page-initiated refresh is same-site.
        resp = _page("Signed in", '<p>Taking you to StormTracker… <a href="/">Continue</a></p>', refresh_to="/")
        resp.set_cookie(COOKIE, tok, max_age=SSO_SESSION_AGE, httponly=True, samesite="strict", secure=core.boot.cookie_secure, path="/")
        resp.delete_cookie(FLOW_COOKIE, path="/api/auth/oidc")
        log.info("single sign-on: %s signed in as %s", name, role)
        return resp

    return r
