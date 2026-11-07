"""Backup and restore of StormTracker's configuration.

What is in a backup: everything you set up in the UI: locations, map settings, retention, forecast and alert settings, alert
rules and destinations, every source's settings, Home Assistant and single sign-on settings. With a passphrase it can also
carry the secrets (API keys, push tokens, the SSO client secret), encrypted with AES-256-GCM under a key derived from that
passphrase (scrypt), so the file can be restored on an installation with a different ``MASTER_KEY``.

What is not: observations, events, radar frames (data, not settings), the admin password, API tokens, and sign-in sessions.

A restore replaces the configuration with the backup's. It checks the whole file first and applies it in one database
transaction, so a bad file changes nothing, and it keeps a snapshot of what it replaced so the last restore can be undone.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from pydantic import ValidationError
from sqlalchemy import select

from . import __version__
from .alerts.rules import AlertSettings, Rule
from .api.routes import UiSettings
from .ha_mqtt import HaSettings
from .locations import Location
from .nowcast.settings import NowcastSettings
from .oidc import OidcSettings
from .plugins import secret_fields
from .storage.tables import AuditLog, Secret, Setting
from .timeutil import utcnow

log = logging.getLogger("stormtracker.backup")
FORMAT, UNDO_FORMAT, VERSION = "stormtracker-backup", "stormtracker-undo", 1
OIDC_SECRET = "auth.oidc.client_secret"
FIXED_KEYS = ("locations", "ui", "retention", "nowcast", "alerts.rules", "alerts.settings", "auth.oidc", "mqtt.ha")
MIN_PASSPHRASE = 12
MAX_KEYS = 500
KEEP_SNAPSHOTS = 3
SCRYPT = {"n": 2 ** 15, "r": 8, "p": 1}
SCRYPT_MAX = {"n": 2 ** 16, "r": 8, "p": 2}
CHECK_TEXT = "stormtracker-backup-check"


class BackupError(ValueError):
    """A problem with the backup file or passphrase, in words for the person restoring."""


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def _unb64(s: Any) -> bytes:
    try:
        return base64.b64decode(str(s), validate=True)
    except (ValueError, TypeError):
        raise BackupError("the backup file is damaged (bad encoding)") from None


def _canon(v: Any) -> str:
    return json.dumps(v, sort_keys=True, separators=(",", ":"), default=str)


def _derive(passphrase: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return Scrypt(salt=salt, length=32, n=n, r=r, p=p).derive(passphrase.encode())


def _seal(key: bytes, name: str, text: str) -> str:
    nonce = os.urandom(12)
    return _b64(nonce + AESGCM(key).encrypt(nonce, text.encode(), name.encode()))


def _open(key: bytes, name: str, blob: str) -> str:
    raw = _unb64(blob)
    try:
        return AESGCM(key).decrypt(raw[:12], raw[12:], name.encode()).decode()
    except (InvalidTag, UnicodeDecodeError, ValueError):
        raise BackupError("a secret in the backup is damaged or was tampered with") from None


class Backups:
    def __init__(self, core) -> None:
        self.core = core
        self._lock = asyncio.Lock()

    # ---- what may be backed up ----------------------------------------------------------------------------
    def _plugins(self) -> dict[str, dict[str, Any]]:
        return {"source": self.core.registry.all(), "alert_output": self.core.alert_registry.all()}

    def valid_key(self, key: str) -> bool:
        if key in FIXED_KEYS:
            return True
        prefix, _, name = key.partition(".")
        return name in self._plugins().get(prefix, {})

    def secret_names(self) -> set[str]:
        out = {OIDC_SECRET}
        for prefix, reg in self._plugins().items():
            for name, cls in reg.items():
                out |= {f"{prefix}.{name}.{f}" for f in secret_fields(cls.ConfigModel)}
        return out

    def check_value(self, key: str, v: Any) -> Any:
        """The value as the running app would store it, or BackupError."""
        try:
            if key == "locations":
                if not isinstance(v, list):
                    raise ValueError("must be a list")
                locs = [Location(**x).model_dump() for x in v]
                if len({x["id"] for x in locs}) != len(locs):
                    raise ValueError("duplicate place id")
                return locs
            if key == "ui":
                return UiSettings(**v).model_dump()
            if key == "nowcast":
                return NowcastSettings(**v).model_dump()
            if key == "alerts.settings":
                return AlertSettings(**v).model_dump()
            if key == "alerts.rules":
                if not isinstance(v, list):
                    raise ValueError("must be a list")
                rules = [Rule(**x).model_dump(mode="json") for x in v]
                if len({r["id"] for r in rules}) != len(rules):
                    raise ValueError("duplicate rule id")
                return rules
            if key == "auth.oidc":
                return OidcSettings(**v).model_dump()
            if key == "mqtt.ha":
                return HaSettings(**v).model_dump()
            if key == "retention":
                if not isinstance(v, dict) or set(v) - {"frame_days", "obs_days", "event_days"}:
                    raise ValueError("unexpected fields")
                if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not 0 < x <= 3650 for x in v.values()):
                    raise ValueError("each retention must be a number of days between 0 and 3650")
                return dict(v)
            prefix, _, name = key.partition(".")
            return self._check_plugin(prefix, name, v)
        except (ValidationError, TypeError, ValueError, KeyError) as e:
            detail = "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()) if isinstance(e, ValidationError) else str(e)
            raise BackupError(f"{key}: {detail}") from None

    def _check_plugin(self, prefix: str, name: str, v: Any) -> dict[str, Any]:
        cls = self._plugins()[prefix][name]
        if not isinstance(v, dict) or set(v) - {"enabled", "interval_s", "config"}:
            raise ValueError("unexpected fields")
        enabled, interval, config = v.get("enabled", False), v.get("interval_s"), v.get("config", {})
        if not isinstance(enabled, bool) or not isinstance(config, dict):
            raise ValueError("enabled must be true or false and config an object")
        if interval is not None and (isinstance(interval, bool) or not isinstance(interval, int) or interval < 1 or getattr(cls.meta, "poll_interval_s", 0) == 0):
            raise ValueError("invalid interval for this source")
        secrets = set(secret_fields(cls.ConfigModel))
        if secrets & set(config):
            raise ValueError(f"settings contain secret fields ({', '.join(sorted(secrets & set(config)))}); secrets are kept apart")
        try:
            cls.ConfigModel.model_validate(config)
        except ValidationError as e:
            real = [x for x in e.errors() if not (x["type"] == "missing" and x["loc"] and (x["loc"][0] in secrets or not enabled))]
            if real:
                raise ValueError("; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in real)) from None
        return {"enabled": enabled, "interval_s": interval, "config": config}

    async def _current(self) -> tuple[dict[str, Any], set[str]]:
        async with self.core.db.session() as s:
            settings = {r.key: r.value for r in (await s.execute(select(Setting))).scalars() if self.valid_key(r.key)}
            names = {r.name for r in (await s.execute(select(Secret))).scalars()} & self.secret_names()
        return settings, names

    # ---- export ---------------------------------------------------------------------------------------------
    async def export(self, passphrase: str | None, actor: str) -> dict[str, Any]:
        if passphrase is not None and len(passphrase) < MIN_PASSPHRASE:
            raise BackupError(f"the passphrase needs at least {MIN_PASSPHRASE} characters")
        settings, names = await self._current()
        doc: dict[str, Any] = {"format": FORMAT, "version": VERSION, "created_at": utcnow().isoformat(), "app_version": __version__,
                               "settings": dict(sorted(settings.items())), "secrets": None}
        if passphrase is not None:
            salt = os.urandom(16)
            key = await asyncio.to_thread(_derive, passphrase, salt, **SCRYPT)
            items = {n: _seal(key, n, await self.core.store.get_secret(n)) for n in sorted(names)}
            doc["secrets"] = {"kdf": {"name": "scrypt", "salt": _b64(salt), **SCRYPT}, "check": _seal(key, "check", CHECK_TEXT), "items": items}
        async with self.core.db.session() as s:
            s.add(AuditLog(ts=utcnow(), actor=actor, action="export_backup", target="backup", detail={"settings": len(settings), "secrets": len(names) if passphrase else 0}))
            await s.commit()
        return doc

    # ---- reading a backup ---------------------------------------------------------------------------------
    async def _read(self, doc: Any, passphrase: str | None) -> tuple[dict[str, Any], dict[str, str] | None, list[dict[str, str]], list[str]]:
        """(validated settings, secrets or None, ignored items, notes). Raises BackupError; nothing is changed."""
        if not isinstance(doc, dict) or doc.get("format") != FORMAT:
            raise BackupError("this isn't a StormTracker backup file")
        ver = doc.get("version")
        if not isinstance(ver, int) or isinstance(ver, bool):
            raise BackupError("the backup file has no version number")
        if ver > VERSION:
            raise BackupError("this backup was made by a newer StormTracker; update this one first")
        raw = doc.get("settings")
        if not isinstance(raw, dict) or len(raw) > MAX_KEYS:
            raise BackupError("the backup has no readable settings")
        settings, ignored, problems = {}, [], []
        for key, val in raw.items():
            if not isinstance(key, str) or not self.valid_key(key):
                ignored.append({"key": str(key)[:80], "reason": "not a setting this version knows (or its source isn't installed)"})
                continue
            try:
                settings[key] = self.check_value(key, val)
            except BackupError as e:
                problems.append(str(e))
        if problems:
            raise BackupError("the backup has settings that don't validate, so nothing was changed: " + " | ".join(problems[:6]))
        notes: list[str] = []
        secrets: dict[str, str] | None = None
        sec = doc.get("secrets")
        if sec is not None:
            if not passphrase:
                notes.append("This backup includes secrets, but no passphrase was given, so they were skipped. Your current secrets are kept.")
            else:
                secrets = await self._decrypt(sec, passphrase, ignored)
        return settings, secrets, ignored, notes

    async def _decrypt(self, sec: Any, passphrase: str, ignored: list[dict[str, str]]) -> dict[str, str]:
        try:
            kdf, items = sec["kdf"], sec["items"]
            n, r, p = int(kdf["n"]), int(kdf["r"]), int(kdf["p"])
            if kdf.get("name") != "scrypt" or n < 2 ** 14 or n & (n - 1) or n > SCRYPT_MAX["n"] or not 1 <= r <= SCRYPT_MAX["r"] or not 1 <= p <= SCRYPT_MAX["p"] or not isinstance(items, dict) or len(items) > MAX_KEYS:
                raise BackupError("the backup's encryption settings are outside what this version accepts")
            salt = _unb64(kdf["salt"])
            if not 8 <= len(salt) <= 64:
                raise BackupError("the backup's encryption settings are damaged")
        except BackupError:
            raise
        except (KeyError, TypeError, ValueError):
            raise BackupError("the backup's secrets section is damaged") from None
        key = await asyncio.to_thread(_derive, passphrase, salt, n, r, p)
        try:
            if _open(key, "check", sec["check"]) != CHECK_TEXT:
                raise BackupError("wrong passphrase")
        except BackupError:
            raise BackupError("wrong passphrase for this backup") from None
        allowed, out = self.secret_names(), {}
        for name, blob in items.items():
            if name not in allowed:
                ignored.append({"key": f"secret {str(name)[:60]}", "reason": "not a secret this version knows"})
                continue
            out[name] = _open(key, name, blob)
        return out

    # ---- preview ----------------------------------------------------------------------------------------------
    async def preview(self, doc: Any, passphrase: str | None) -> dict[str, Any]:
        settings, secrets, ignored, notes = await self._read(doc, passphrase)
        cur, cur_secrets = await self._current()
        rows = []
        for key in sorted(set(settings) | set(cur)):
            st = "removed" if key not in settings else "new" if key not in cur else "same" if _canon(settings[key]) == _canon(cur[key]) else "changed"
            rows.append({"key": key, "status": st})
        changed = {r["key"] for r in rows if r["status"] != "same"}
        warnings = list(notes)
        if "auth.oidc" in changed:
            o = settings.get("auth.oidc") or {}
            warnings.append("Single sign-on settings will change" + (f": {o.get('issuer') or 'no issuer'}, admin groups {o.get('admin_groups') or 'none'}, member groups {o.get('member_groups') or 'none'}." if o else " (they will be reset)."))
        outs = sorted(k.split(".", 1)[1] for k in changed if k.startswith("alert_output."))
        if outs:
            warnings.append(f"Alert destinations will change: {', '.join(outs)}. Check where alerts will be sent.")
        if "mqtt.ha" in changed or "alert_output.mqtt" in changed:
            warnings.append("The MQTT broker or Home Assistant settings will change.")
        have = set(secrets) if secrets is not None else cur_secrets
        for prefix, reg in self._plugins().items():
            for name, cls in reg.items():
                v = settings.get(f"{prefix}.{name}")
                if not v or not v["enabled"] or f"{prefix}.{name}" not in changed:
                    continue
                need = [f for f in secret_fields(cls.ConfigModel) if cls.ConfigModel.model_fields[f].is_required() and f"{prefix}.{name}.{f}" not in have]
                if need:
                    warnings.append(f"{name} is on in this backup but still needs {', '.join(need)} entered (secrets aren't restored).")
        sec_info = {"included": secrets is not None, "count": len(secrets or {}),
                    "added": sorted(set(secrets or {}) - cur_secrets) if secrets is not None else [],
                    "removed": sorted(cur_secrets - set(secrets)) if secrets is not None else []}
        return {"created_at": doc.get("created_at"), "settings": rows, "ignored": ignored, "secrets": sec_info, "warnings": warnings,
                "will_change": len(changed)}

    # ---- restore and undo -------------------------------------------------------------------------------------
    @property
    def _dir(self) -> Path:
        return self.core.boot.data_dir / "backups"

    def _snapshots(self) -> list[Path]:
        return sorted(self._dir.glob("pre-restore-*.json")) if self._dir.is_dir() else []

    def undo_info(self) -> dict[str, Any] | None:
        snaps = self._snapshots()
        if not snaps:
            return None
        try:
            d = json.loads(snaps[-1].read_text())
            return {"created_at": d["created_at"], "settings": len(d["settings"]), "secrets": len(d["secrets"])}
        except (OSError, ValueError, KeyError):
            return None

    async def _snapshot(self) -> None:
        settings, names = await self._current()
        blobs = {}
        async with self.core.db.session() as s:
            for r in (await s.execute(select(Secret))).scalars():
                if r.name in names:
                    blobs[r.name] = _b64(r.blob)
        self._dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self._dir / f"pre-restore-{utcnow().strftime('%Y%m%dT%H%M%S%f')}.json"
        body = json.dumps({"format": UNDO_FORMAT, "version": VERSION, "created_at": utcnow().isoformat(), "settings": settings, "secrets": blobs})
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(body)
        for old in self._snapshots()[:-KEEP_SNAPSHOTS]:
            old.unlink(missing_ok=True)

    async def _apply(self, settings: dict[str, Any], secrets: dict[str, str] | None, actor: str, action: str, detail: dict[str, Any]) -> set[str]:
        """Write everything in one transaction. Returns the setting keys that changed or went away."""
        touched: set[str] = set()
        async with self.core.db.session() as s:
            existing = {r.key: r for r in (await s.execute(select(Setting))).scalars() if self.valid_key(r.key)}
            now = utcnow()
            for key, val in settings.items():
                row = existing.get(key)
                if row is None:
                    s.add(Setting(key=key, value=val, version=1, updated_at=now))
                    touched.add(key)
                elif _canon(row.value) != _canon(val):
                    row.value, row.version, row.updated_at = val, row.version + 1, now
                    touched.add(key)
            for key, row in existing.items():
                if key not in settings:
                    await s.delete(row)
                    touched.add(key)
            if secrets is not None:
                allowed = self.secret_names()
                have = {r.name: r for r in (await s.execute(select(Secret))).scalars() if r.name in allowed}
                for name, text in secrets.items():
                    blob = self.core.box.encrypt(name, text)
                    if name in have:
                        have[name].blob, have[name].updated_at = blob, now
                    else:
                        s.add(Secret(name=name, blob=blob, updated_at=now))
                for name, row in have.items():
                    if name not in secrets:
                        await s.delete(row)
            s.add(AuditLog(ts=now, actor=actor, action=action, target="backup", detail=detail))
            await s.commit()
        return touched

    async def _reload(self, touched: set[str], secrets_replaced: bool) -> None:
        names = {k.split(".", 1)[1] for k in touched if k.startswith("source.")}
        if secrets_replaced:
            names |= set(self.core.registry.all())          # a source may have new credentials even when its settings didn't change
        for name in sorted(names):
            try:
                await self.core.scheduler.reload(name)
            except Exception:  # noqa: BLE001 - one source failing to restart must not undo the restore
                log.exception("reloading source %s after a restore failed", name)
        self.core.ui_cache = None
        self.core.oidc.forget()
        self.core.ha.poke(announce=True)

    async def restore(self, doc: Any, passphrase: str | None, actor: str) -> dict[str, Any]:
        async with self._lock:
            settings, secrets, _ign, _notes = await self._read(doc, passphrase)
            plan = await self.preview(doc, passphrase)
            await self._snapshot()
            touched = await self._apply(settings, secrets, actor, "restore_backup", {"settings": len(settings), "secrets": None if secrets is None else len(secrets)})
            await self._reload(touched, secrets is not None)
        log.info("backup restored by %s: %d settings changed", actor, len(touched))
        return {**plan, "restored": True, "changed": sorted(touched)}

    async def undo(self, actor: str) -> dict[str, Any]:
        async with self._lock:
            snaps = self._snapshots()
            if not snaps:
                raise BackupError("there is no restore to undo")
            try:
                d = json.loads(snaps[-1].read_text())
                if d.get("format") != UNDO_FORMAT:
                    raise ValueError
                settings = {k: self.check_value(k, v) for k, v in d["settings"].items() if self.valid_key(k)}
                secrets = {n: self.core.box.decrypt(n, _unb64(b)) for n, b in d["secrets"].items() if n in self.secret_names()}
            except BackupError:
                raise
            except Exception:  # noqa: BLE001 - unreadable file, or written under a different MASTER_KEY
                raise BackupError("the saved copy from before the last restore can't be read here") from None
            touched = await self._apply(settings, secrets, actor, "undo_restore", {"settings": len(settings), "secrets": len(secrets)})
            snaps[-1].unlink(missing_ok=True)
            await self._reload(touched, True)
        return {"undone": True, "changed": sorted(touched)}
