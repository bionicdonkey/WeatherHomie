"""Plugin interface for data sources.

A source is a folder ``stormtracker/sources/<name>/`` with a ``plugin.py`` defining a
``Source`` subclass. Nothing in core needs to change to add one.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar, Literal

import httpx
from pydantic import BaseModel, Field

from ..models import Normalised

Category = Literal["radar", "observations", "lightning", "warnings", "model_forecast"]


class RateLimit(BaseModel):
    max_requests: int = Field(gt=0)
    period_s: float = Field(gt=0)


class Licence(BaseModel):
    """Declares usage terms so the UI can surface them and paid feeds can be added later."""
    type: Literal["open", "personal", "commercial-licensed"] = "personal"
    terms_url: str | None = None
    attribution: str | None = None
    notes: str | None = None


class SourceMeta(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    category: Category
    description: str = ""
    poll_interval_s: int = Field(ge=0, description="default; 0 = push-only (no polling)")
    rate_limit: RateLimit = RateLimit(max_requests=1, period_s=1)
    licence: Licence = Licence()


def secret_field(description: str = "", *, required: bool = False, multiline: bool = False,
                 generate: bool | str = False) -> Any:
    """Mark a ConfigModel field as secret: stored encrypted, write-only via the API.

    ``required=True`` means the source can't be enabled until the secret is set.
    """
    extra: dict[str, Any] = {"secret": True}
    if multiline:
        extra["format"] = "textarea"      # UI hint: one value per line
    if generate:
        # UI hint: offer a "Generate" button (random token made in the browser). A string is the hint shown with
        # the new value, with {value} standing for it; True means "show the ingest URL" (push-style sources).
        extra["generate"] = generate
    if required:
        return Field(..., description=description, json_schema_extra=extra)
    return Field(default=None, description=description, json_schema_extra=extra)


def secret_fields(model: type[BaseModel]) -> list[str]:
    return [n for n, f in model.model_fields.items()
            if isinstance(f.json_schema_extra, dict) and f.json_schema_extra.get("secret")]


class HealthState(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"
    DOWN = "down"
    DISABLED = "disabled"
    MISSING_CREDENTIALS = "missing_credentials"
    STARTING = "starting"


class Health(BaseModel):
    state: HealthState = HealthState.STARTING
    last_success: datetime | None = None
    last_attempt: datetime | None = None
    last_error: str | None = None
    consecutive_failures: int = 0
    detail: dict[str, Any] = Field(default_factory=dict)


Emit = Callable[[list[Normalised]], Awaitable[None]]


class SourceContext:
    """Everything a plugin may use. Plugins never read env vars or the DB directly."""

    def __init__(self, *, http: httpx.AsyncClient, emit: Emit, health: Health,
                 locations: Callable[[], Awaitable[list[dict[str, Any]]]], logger: logging.Logger,
                 data_dir: Path | None = None, extras: dict[str, Any] | None = None):
        self.http = http          # carries the identifying User-Agent
        self.emit = emit          # persist normalised items (used by push sources)
        self.state = health       # scheduler-maintained
        self.get_locations = locations   # async; always returns the current locations
        self.log = logger
        self.data_dir = data_dir  # persistent dir for plugin-owned static files
        self.extras = extras or {}   # optional app-level hints for suggest(), e.g. the address-search server


class Candidate(BaseModel):
    """One suggested setting for a source, derived from a configured location."""
    label: str
    detail: str = ""
    values: dict[str, Any]             # config fields to fill in (nothing is saved until the user saves the form)
    current: bool = False              # already what this source uses


class Suggestion(BaseModel):
    candidates: list[Candidate]
    note: str = ""


class NoConfig(BaseModel):
    """Default ConfigModel for sources with no settings."""


class Source(ABC):
    meta: ClassVar[SourceMeta]
    ConfigModel: ClassVar[type[BaseModel]] = NoConfig

    def __init__(self, cfg: BaseModel, ctx: SourceContext):
        self.cfg = cfg
        self.ctx = ctx

    @abstractmethod
    async def fetch(self) -> Any:
        """Network I/O only. Return a raw payload; raise on failure."""

    @abstractmethod
    def normalise(self, raw: Any) -> list[Normalised]:
        """Pure conversion of a raw payload into the common model (fixture-testable)."""

    @classmethod
    async def suggest(cls, location: dict[str, Any], ctx: SourceContext, current: dict[str, Any]) -> Suggestion | None:
        """Optional: settings that fit ``location`` (e.g. the nearest radar or station), best first.

        Called when the user asks, never on its own, and never saved by the server: the UI offers the choices and the
        user decides. ``current`` is the saved config. Return None if the source has nothing to suggest.
        """
        return None

    @property
    def polling(self) -> bool:
        """False = this configuration only receives pushes, so the scheduler won't poll it."""
        return True

    async def location(self, location_id: str | None = None) -> dict[str, Any]:
        """The configured location this source is about (by id, else the first), as {id, name, lat, lon}."""
        locs = await self.ctx.get_locations()
        if location_id:
            locs = [x for x in locs if x["id"] == location_id]
        if not locs:
            raise RuntimeError("no matching location configured; add a location first")
        return locs[0]

    async def health(self) -> Health:
        """Default: scheduler-recorded health. Override to add active checks."""
        return self.ctx.state

    async def handle_push(self, request: Any) -> Any:
        """Push-style sources implement this; served at GET/POST /ingest/<name>[/<rest>].

        The sub-path is available as ``request.path_params["rest"]`` (the plugin does its own auth).
        """
        raise NotImplementedError

    async def on_saved(self, items: list[Normalised]) -> None:
        """Called by the scheduler after ``items`` were persisted successfully (no-op by default)."""

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
