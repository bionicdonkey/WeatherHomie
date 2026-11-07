"""DB-backed settings + encrypted secrets + audit log."""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import delete, select

from ..storage.db import Database
from ..storage.tables import AuditLog, Secret, Setting
from ..timeutil import utcnow
from .crypto import SecretBox, WrongMasterKey

_CANARY = "meta.canary"


class SettingsStore:
    def __init__(self, db: Database, box: SecretBox):
        self.db, self.box = db, box

    async def init(self) -> None:
        """Verify MASTER_KEY against a stored canary (creates it on first run)."""
        async with self.db.session() as s:
            row = await s.get(Secret, _CANARY)
            if row is None:
                s.add(Secret(name=_CANARY, blob=self.box.encrypt(_CANARY, "ok"), updated_at=utcnow()))
                await s.commit()
            else:
                try:
                    self.box.decrypt(_CANARY, row.blob)
                except WrongMasterKey as e:
                    raise WrongMasterKey("MASTER_KEY does not match the one used to create this database") from e

    # ---- plain settings -------------------------------------------------
    async def get(self, key: str, default: Any = None) -> Any:
        async with self.db.session() as s:
            row = await s.get(Setting, key)
            return default if row is None else row.value

    async def set(self, key: str, value: Any, *, actor: str = "system") -> None:
        json.dumps(value)  # fail early if not serialisable
        async with self.db.session() as s:
            row = await s.get(Setting, key)
            if row is None:
                s.add(Setting(key=key, value=value, version=1, updated_at=utcnow()))
            else:
                row.value, row.version, row.updated_at = value, row.version + 1, utcnow()
            s.add(AuditLog(ts=utcnow(), actor=actor, action="set", target=key, detail={"keys": _keys(value)}))
            await s.commit()

    async def delete(self, key: str, *, actor: str = "system") -> None:
        async with self.db.session() as s:
            await s.execute(delete(Setting).where(Setting.key == key))
            s.add(AuditLog(ts=utcnow(), actor=actor, action="delete", target=key, detail={}))
            await s.commit()

    # ---- secrets (write-only from the API's point of view) --------------
    async def set_secret(self, name: str, value: str, *, actor: str = "system") -> None:
        blob = self.box.encrypt(name, value)
        async with self.db.session() as s:
            row = await s.get(Secret, name)
            if row is None:
                s.add(Secret(name=name, blob=blob, updated_at=utcnow()))
            else:
                row.blob, row.updated_at = blob, utcnow()
            s.add(AuditLog(ts=utcnow(), actor=actor, action="set_secret", target=name, detail={}))
            await s.commit()

    async def delete_secret(self, name: str, *, actor: str = "system") -> None:
        async with self.db.session() as s:
            await s.execute(delete(Secret).where(Secret.name == name))
            s.add(AuditLog(ts=utcnow(), actor=actor, action="delete_secret", target=name, detail={}))
            await s.commit()

    async def has_secret(self, name: str) -> bool:
        async with self.db.session() as s:
            return await s.get(Secret, name) is not None

    async def get_secret(self, name: str) -> str | None:
        """Internal use only (building plugin config). Never expose via the API."""
        async with self.db.session() as s:
            row = await s.get(Secret, name)
            return None if row is None else self.box.decrypt(name, row.blob)

    async def audit(self, limit: int = 100) -> list[dict[str, Any]]:
        async with self.db.session() as s:
            rows = (await s.execute(select(AuditLog).order_by(AuditLog.id.desc()).limit(limit))).scalars()
            return [dict(ts=r.ts.isoformat(), actor=r.actor, action=r.action, target=r.target, detail=r.detail)
                    for r in rows]


def _keys(v: Any) -> list[str]:
    return sorted(v.keys()) if isinstance(v, dict) else []
