"""The alert message and the plugin interface for alert outputs (ntfy, webhook, MQTT, email...)."""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, ClassVar, Literal

import httpx
from pydantic import BaseModel, Field

from ..plugins import NoConfig

Severity = Literal["info", "warning", "critical"]
Kind = Literal["alert", "clear", "test"]


class Alert(BaseModel):
    """What gets delivered. Plain text title/message for humans, ``data`` for machines."""
    id: str
    ts: datetime
    kind: Kind = "alert"
    rule_id: str
    rule_name: str
    key: str                                  # what this is about, e.g. "cell:0905-3": the dedup identity
    severity: Severity = "warning"
    title: str
    message: str
    url: str | None = None                    # where to look (the UI), when a public address is configured
    data: dict[str, Any] = Field(default_factory=dict)


class OutputMeta(BaseModel):
    name: str
    description: str = ""


class OutputContext:
    """What an output may use. Outputs never read env vars or the DB directly."""

    def __init__(self, http: httpx.AsyncClient, logger: logging.Logger):
        self.http = http
        self.log = logger


class AlertOutput(ABC):
    meta: ClassVar[OutputMeta]
    ConfigModel: ClassVar[type[BaseModel]] = NoConfig

    def __init__(self, cfg: BaseModel, ctx: OutputContext):
        self.cfg = cfg
        self.ctx = ctx

    @abstractmethod
    async def send(self, alert: Alert) -> None:
        """Deliver one alert. Raise on failure; the service records it and carries on with the other outputs."""
