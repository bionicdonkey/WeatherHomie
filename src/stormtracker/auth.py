"""Admin auth: argon2 password, signed session cookie, CSRF token, optional trusted-header SSO."""
from __future__ import annotations

import hmac
import logging
import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from urllib.parse import urlparse

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import HTTPException, Request
from itsdangerous import BadSignature, URLSafeTimedSerializer

from .bootstrap import Bootstrap
from .storage.db import Database
from .storage.tables import AdminUser
from .tokens import TokenStore
from .timeutil import utcnow

log = logging.getLogger(__name__)
COOKIE = "st_session"
MIN_PASSWORD = 12
SESSION_MAX_AGE = 7 * 24 * 3600
SSO_SESSION_AGE = 12 * 3600          # roles are decided at sign-in, so single sign-on sessions are shorter
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_ph = PasswordHasher()


@dataclass
class Principal:
    """Who is calling. ``role``: admin (everything), member (read the weather) or reader (an API token: read-only).
    ``kind``: local (password), oidc (single sign-on), header (trusted proxy) or token."""
    username: str
    csrf: str | None  # None for trusted-header and token auth
    role: str = "admin"
    kind: str = "local"


class FailureThrottle:
    """Blocks a key (an IP) after ``limit`` failures inside ``window`` seconds. Idle keys are dropped, so it cannot grow forever."""

    def __init__(self, limit: int, window: float = 300.0):
        self.limit, self.window = limit, window
        self._fails: dict[str, deque[float]] = defaultdict(deque)
        self._swept = time.monotonic()

    def _sweep(self, now: float) -> None:
        if now - self._swept < self.window:
            return
        self._swept = now
        for k in [k for k, q in self._fails.items() if not q or now - q[-1] > self.window]:
            del self._fails[k]

    def blocked(self, key: str) -> bool:
        now = time.monotonic()
        self._sweep(now)
        q = self._fails.get(key)
        if q is None:
            return False
        while q and now - q[0] > self.window:
            q.popleft()
        return len(q) >= self.limit

    def fail(self, key: str) -> None:
        self._fails[key].append(time.monotonic())


def client_ip(request: Request, trusted_proxies: tuple[str, ...]) -> str:
    """The caller's address. Behind a proxy we trust, that is the right-most X-Forwarded-For hop that is not itself a trusted
    proxy; otherwise (and for any untrusted sender, whose header could say anything) it is the socket peer."""
    peer = request.client.host if request.client else "?"
    if peer not in trusted_proxies:
        return peer
    hops = [h.strip() for h in request.headers.get("x-forwarded-for", "").split(",") if h.strip()]
    for h in reversed(hops):
        if h not in trusted_proxies:
            return h
    return peer


class Auth:
    def __init__(self, boot: Bootstrap, db: Database, session_secret: str, store=None):
        self.boot, self.db, self.store = boot, db, store
        self.ser = URLSafeTimedSerializer(session_secret, salt="stormtracker-session")
        self.login_throttle = FailureThrottle(limit=5)
        self.ingest_throttle = FailureThrottle(limit=30)      # wrong tokens on /ingest/*: someone guessing
        self.token_throttle = FailureThrottle(limit=30)       # wrong API tokens
        self.tokens = TokenStore(db)
        self.setup_token: str | None = None
        self.epoch = 0                                         # bumped when the password changes: every older session dies
        self.revoked: dict[str, float] = {}                    # session nonce -> when it would have expired anyway (logged out)

    def ip(self, request: Request) -> str:
        return client_ip(request, self.boot.trusted_proxies)

    async def has_admin(self) -> bool:
        from sqlalchemy import func, select
        async with self.db.session() as s:
            return (await s.execute(select(func.count()).select_from(AdminUser))).scalar_one() > 0

    async def create_admin(self, username: str, password: str) -> None:
        if len(password) < MIN_PASSWORD:
            raise ValueError(f"password must be at least {MIN_PASSWORD} characters")
        async with self.db.session() as s:
            s.add(AdminUser(username=username, password_hash=_ph.hash(password), created_at=utcnow()))
            await s.commit()

    async def init(self) -> None:
        if self.store is not None:
            self.epoch = int(await self.store.get("auth.epoch", 0))
            now = time.time()
            self.revoked = {n: exp for n, exp in (await self.store.get("auth.revoked", {})).items() if exp > now}
        if not await self.has_admin():
            if self.boot.admin_password and len(self.boot.admin_password) >= MIN_PASSWORD:
                await self.create_admin(self.boot.admin_username, self.boot.admin_password)
            else:
                if self.boot.admin_password:      # do not crash-loop the container over it: fall back to the one-time setup token
                    log.error("ADMIN_PASSWORD is shorter than %d characters, so no account was created from it. "
                              "Use the setup token below to create the admin in the browser, or set a longer password.", MIN_PASSWORD)
                self.setup_token = secrets.token_urlsafe(16)

    async def verify(self, username: str, password: str) -> bool:
        async with self.db.session() as s:
            u = await s.get(AdminUser, username)
        try:
            if u is None:
                _ph.verify(_DUMMY, password)  # equalise timing
                return False
            return _ph.verify(u.password_hash, password)
        except VerifyMismatchError:
            return False

    async def change_password(self, username: str, current: str, new: str) -> None:
        """Verify the current password, set a new one, and end every existing session (including this one: sign in again)."""
        if not await self.verify(username, current):
            raise ValueError("the current password is wrong")
        if len(new) < MIN_PASSWORD:
            raise ValueError(f"password must be at least {MIN_PASSWORD} characters")
        async with self.db.session() as s:
            u = await s.get(AdminUser, username)
            u.password_hash = _ph.hash(new)
            await s.commit()
        self.epoch += 1
        if self.store is not None:
            await self.store.set("auth.epoch", self.epoch, actor=username)

    async def revoke(self, token: str | None, actor: str = "admin") -> None:
        """Sign out for real: the token stops working everywhere, not just in this browser."""
        try:
            d = self.ser.loads(token or "", max_age=SESSION_MAX_AGE)
        except BadSignature:
            return
        if d.get("n"):
            now = time.time()
            self.revoked = {n: e for n, e in self.revoked.items() if e > now}
            self.revoked[d["n"]] = now + SESSION_MAX_AGE
            if self.store is not None:
                await self.store.set("auth.revoked", self.revoked, actor=actor)

    # ---- sessions ----
    def issue(self, username: str, role: str = "admin", kind: str = "local", lifetime: int | None = None) -> tuple[str, str]:
        csrf = secrets.token_urlsafe(24)
        d = {"u": username, "c": csrf, "n": secrets.token_urlsafe(12), "e": self.epoch, "r": role, "k": kind}
        if lifetime:
            d["x"] = int(time.time()) + lifetime
        return self.ser.dumps(d), csrf

    def principal(self, request: Request) -> Principal | None:
        """The signed-in person from the proxy header or the session cookie (API tokens are handled in ``who``)."""
        h = self.boot.trusted_auth_header
        if h and request.headers.get(h) and request.client and request.client.host in self.boot.trusted_proxies:
            return Principal(request.headers[h], None, "admin", "header")
        tok = request.cookies.get(COOKIE)
        if not tok:
            return None
        try:
            d = self.ser.loads(tok, max_age=SESSION_MAX_AGE)
        except BadSignature:
            return None
        if d.get("e", 0) != self.epoch or not d.get("n") or d["n"] in self.revoked:
            return None                                       # password changed since, signed out, or an old-style token (no nonce): sign in again
        if d.get("x") and time.time() > d["x"]:
            return None
        role = d.get("r", "admin")                           # sessions from before roles existed were all admin password sign-ins
        if role not in ("admin", "member"):
            return None
        return Principal(d["u"], d["c"], role, d.get("k", "local"))

    async def who(self, request: Request) -> Principal | None:
        """Like ``principal`` but also understands ``Authorization: Bearer st_...`` (checked first)."""
        auth = request.headers.get("authorization", "")
        if auth[:7].lower() == "bearer ":
            ip = self.ip(request)
            if self.token_throttle.blocked(ip):
                raise HTTPException(429, "too many failed attempts; try again later")
            t = await self.tokens.verify(auth[7:].strip())
            if t is None:
                self.token_throttle.fail(ip)
                raise HTTPException(401, "invalid or revoked API token")
            return Principal(f"token:{t}", None, "reader", "token")
        return self.principal(request)

    async def require(self, request: Request, need: str = "admin", *, token_may_write: bool = False) -> Principal:
        """401 when nobody is signed in; 403 when someone is but may not do this (the UI treats 401 as "session ended")."""
        p = await self.who(request)
        if p is None:
            raise HTTPException(401, "authentication required")
        unsafe = request.method not in SAFE_METHODS
        if p.kind == "token" and unsafe and not token_may_write:
            raise HTTPException(403, "API tokens are read-only")
        if need == "admin" and p.role != "admin":
            raise HTTPException(403, "this needs an admin account")
        if unsafe and p.kind != "token":
            origin = request.headers.get("origin")
            if origin and urlparse(origin).netloc != request.headers.get("host"):
                raise HTTPException(403, "cross-origin request refused")
            if p.csrf is not None and not hmac.compare_digest(request.headers.get("x-csrf-token", ""), p.csrf):
                raise HTTPException(403, "missing or invalid CSRF token")
        return p

    def dependency(self, need: str):
        """A FastAPI dependency: ``need`` is "admin" or "viewer" (admin, member or API token)."""
        async def dep(request: Request) -> Principal:
            return await self.require(request, need)
        return dep


_DUMMY = _ph.hash("dummy-password-for-timing")
