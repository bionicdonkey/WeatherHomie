"""Read-only API tokens for agents and other apps.

A token looks like ``st_<id>_<secret>``. Only the SHA-256 of the whole token is stored (the secret is 256 random bits, so a
slow password hash would add latency to every request without adding safety). It is shown once when created and can be
revoked at any time. Tokens can read the weather data and nothing else: they cannot change settings or read secrets.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time

from sqlalchemy import delete, select

from .storage.db import Database
from .storage.tables import ApiToken, AuditLog
from .timeutil import utcnow

PREFIX = "st"
TOUCH_EVERY_S = 60.0          # last-used is recorded at most this often per token, so reads do not each cause a write
MAX_TOKENS = 50


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class TokenStore:
    def __init__(self, db: Database):
        self.db = db
        self._touched: dict[str, float] = {}

    async def create(self, name: str, actor: str) -> tuple[dict, str]:
        async with self.db.session() as s:
            if len((await s.execute(select(ApiToken.id))).all()) >= MAX_TOKENS:
                raise ValueError(f"at most {MAX_TOKENS} tokens; revoke some first")
            tid = secrets.token_hex(4)
            token = f"{PREFIX}_{tid}_{secrets.token_urlsafe(32)}"
            now = utcnow()
            s.add(ApiToken(id=tid, name=name, secret_hash=_hash(token), created_by=actor, created_at=now))
            s.add(AuditLog(ts=now, actor=actor, action="create_token", target=f"api_token:{tid}", detail={"name": name}))
            await s.commit()
        return {"id": tid, "name": name, "created_at": now.isoformat(), "last_used": None}, token

    async def list(self) -> list[dict]:
        async with self.db.session() as s:
            rows = (await s.execute(select(ApiToken).order_by(ApiToken.created_at))).scalars().all()
        return [{"id": r.id, "name": r.name, "created_by": r.created_by, "created_at": r.created_at.isoformat(),
                 "last_used": r.last_used.isoformat() if r.last_used else None} for r in rows]

    async def delete(self, tid: str, actor: str) -> bool:
        async with self.db.session() as s:
            res = await s.execute(delete(ApiToken).where(ApiToken.id == tid))
            if res.rowcount:
                s.add(AuditLog(ts=utcnow(), actor=actor, action="revoke_token", target=f"api_token:{tid}", detail={}))
            await s.commit()
        self._touched.pop(tid, None)
        return bool(res.rowcount)

    async def verify(self, token: str) -> str | None:
        """The token's name if it is valid, else None."""
        parts = token.split("_", 2)
        if len(parts) != 3 or parts[0] != PREFIX or not 1 <= len(parts[1]) <= 16:
            return None
        async with self.db.session() as s:
            row = await s.get(ApiToken, parts[1])
            if row is None or not hmac.compare_digest(row.secret_hash, _hash(token)):
                return None
            name = row.name
            now = time.monotonic()
            if now - self._touched.get(row.id, -1e9) >= TOUCH_EVERY_S:
                self._touched[row.id] = now
                row.last_used = utcnow()
                await s.commit()
        return name
