"""Per-source settings: enabled flag, interval, config (plain) and secrets (encrypted)."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ValidationError

from ..plugins import PluginRegistry, secret_fields
from .store import SettingsStore


class SourceSettingsError(ValueError):
    pass


def _key(name: str, prefix: str = "source") -> str:
    return f"{prefix}.{name}"


def _secret_name(name: str, field: str, prefix: str = "source") -> str:
    return f"{prefix}.{name}.{field}"


class SourceSettings:
    """Enabled flag, config and encrypted secrets for the plugins in ``registry``.

    ``prefix`` namespaces the stored keys, so the same code manages data sources ("source") and alert
    outputs ("alert_output").
    """

    def __init__(self, store: SettingsStore, registry: PluginRegistry, prefix: str = "source"):
        self.store, self.registry, self.prefix = store, registry, prefix

    async def raw(self, name: str) -> dict[str, Any]:
        return await self.store.get(_key(name, self.prefix), {"enabled": False, "interval_s": None, "config": {}})

    async def secrets_set(self, name: str) -> dict[str, bool]:
        model = self.registry.get(name).ConfigModel
        return {f: await self.store.has_secret(_secret_name(name, f, self.prefix)) for f in secret_fields(model)}

    async def view(self, name: str) -> dict[str, Any]:
        """API-safe view: never contains secret values."""
        raw = await self.raw(name)
        model = self.registry.get(name).ConfigModel
        sec = secret_fields(model)
        return {"enabled": raw["enabled"], "interval_s": raw.get("interval_s"),
                "config": {k: v for k, v in raw["config"].items() if k not in sec},
                "secrets_set": await self.secrets_set(name)}

    async def build(self, name: str) -> tuple[BaseModel | None, list[str]]:
        """Return (validated ConfigModel instance | None, missing_secret_fields)."""
        raw = await self.raw(name)
        model = self.registry.get(name).ConfigModel
        data = dict(raw["config"])
        missing: list[str] = []
        for f in secret_fields(model):
            v = await self.store.get_secret(_secret_name(name, f, self.prefix))
            if v is not None:
                data[f] = v
        try:
            return model.model_validate(data), []
        except ValidationError as e:
            sec = set(secret_fields(model))
            missing = [str(err["loc"][0]) for err in e.errors()
                       if err["type"] == "missing" and err["loc"] and err["loc"][0] in sec]
            if len(missing) == len(e.errors()):
                return None, missing
            raise SourceSettingsError(_safe_errors(e)) from None

    async def update(self, name: str, *, enabled: bool | None = None, interval_s: int | None = ...,  # type: ignore[assignment]
                     config: dict[str, Any] | None = None, secrets: dict[str, str | None] | None = None,
                     actor: str = "admin") -> None:
        cls = self.registry.get(name)
        sec_fields = set(secret_fields(cls.ConfigModel))
        raw = await self.raw(name)
        if config is not None:
            bad = sec_fields & set(config)
            if bad:
                raise SourceSettingsError(f"secret fields must go in 'secrets', not 'config': {sorted(bad)}")
            raw["config"] = config
        if interval_s is not ...:
            if interval_s is not None and (getattr(cls.meta, "poll_interval_s", 0) == 0 or interval_s < 1):
                raise SourceSettingsError("invalid interval for this source")
            raw["interval_s"] = interval_s
        if enabled is not None:
            raw["enabled"] = enabled
        vet = getattr(cls, "validate_secret", None)       # optional hook: plugin-specific check of a secret value
        for f, v in (secrets or {}).items():
            if f not in sec_fields:
                raise SourceSettingsError(f"{f!r} is not a secret field of {name}")
            if vet and v:
                problem = vet(f, v)
                if problem:
                    raise SourceSettingsError(f"{f}: {problem}")
        # validate non-secret config shape now (secrets may be missing -> allowed unless enabling)
        try:
            cls.ConfigModel.model_validate(raw["config"])
        except ValidationError as e:
            real = [x for x in e.errors() if not (x["type"] == "missing" and x["loc"] and (
                x["loc"][0] in sec_fields or not raw["enabled"]))]
            if real:
                raise SourceSettingsError("; ".join(
                    f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in real)) from None
        for f, v in (secrets or {}).items():
            if v is None or v == "":
                await self.store.delete_secret(_secret_name(name, f, self.prefix), actor=actor)
            else:
                await self.store.set_secret(_secret_name(name, f, self.prefix), v, actor=actor)
        await self.store.set(_key(name, self.prefix), raw, actor=actor)


def _safe_errors(e: ValidationError) -> str:
    # omit 'input' so a rejected value (possibly a secret) never appears in messages
    return "; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors())
