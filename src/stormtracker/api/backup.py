"""Backup and restore endpoints. Admin only; the passphrase travels in the POST body, never in a URL."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..auth import Principal
from ..backup import MIN_PASSPHRASE, BackupError

MAX_BACKUP_BYTES = 2_000_000


class ExportIn(BaseModel):
    include_secrets: bool = False
    passphrase: str | None = Field(None, max_length=500, description=f"required with include_secrets, at least {MIN_PASSPHRASE} characters")


class RestoreIn(BaseModel):
    backup: dict[str, Any]
    passphrase: str | None = Field(None, max_length=500)


def build_backup_router(core) -> APIRouter:
    r = APIRouter(prefix="/api/backup")
    admin = Depends(core.auth.dependency("admin"))
    svc = lambda: core.backups   # noqa: E731

    def guard(body: RestoreIn) -> None:
        import json
        if len(json.dumps(body.backup)) > MAX_BACKUP_BYTES:
            raise HTTPException(413, "that backup file is too large")

    @r.get("")
    async def status(p: Principal = admin):
        return {"undo": svc().undo_info(), "min_passphrase": MIN_PASSPHRASE}

    @r.post("/export")
    async def export(body: ExportIn, p: Principal = admin):
        if body.include_secrets and not body.passphrase:
            raise HTTPException(422, f"choose a passphrase of at least {MIN_PASSPHRASE} characters to protect the secrets")
        try:
            doc = await svc().export(body.passphrase if body.include_secrets else None, p.username)
        except BackupError as e:
            raise HTTPException(422, str(e)) from None
        return JSONResponse(doc, headers={"Cache-Control": "no-store"})

    @r.post("/preview")
    async def preview(body: RestoreIn, p: Principal = admin):
        guard(body)
        try:
            return await svc().preview(body.backup, body.passphrase)
        except BackupError as e:
            raise HTTPException(422, str(e)) from None

    @r.post("/restore")
    async def restore(body: RestoreIn, p: Principal = admin):
        guard(body)
        try:
            return await svc().restore(body.backup, body.passphrase, p.username)
        except BackupError as e:
            raise HTTPException(422, str(e)) from None

    @r.post("/undo")
    async def undo(p: Principal = admin):
        try:
            return await svc().undo(p.username)
        except BackupError as e:
            raise HTTPException(422, str(e)) from None

    return r
