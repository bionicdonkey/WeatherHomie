"""ntfy (https://ntfy.sh, or your own server): push notifications to phones and desktops.

Uses ntfy's JSON publish API, so titles and messages can contain any characters. On the public server the topic
name is the only thing keeping strangers from reading your alerts, so it is stored as a secret: pick a long random one
(the UI can generate it), or run your own server with access tokens.
"""
from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator

from ....plugins import secret_field
from ...base import Alert, AlertOutput, OutputMeta

PRIORITY = {"info": 3, "warning": 4, "critical": 5}
TAGS = {"info": ["cloud_with_rain"], "warning": ["warning"], "critical": ["rotating_light"]}


class NtfyConfig(BaseModel):
    server: str = Field("https://ntfy.sh", description="ntfy server address")
    topic: str | None = secret_field("Topic to publish to. On ntfy.sh anyone who knows it can read your alerts, so make it long and random.",
                                     required=True,
                                     generate="Subscribe to this topic in the ntfy app (on this server): {value}")
    token: str | None = secret_field("Access token, for a server or topic that needs a login (optional).")

    @field_validator("server")
    @classmethod
    def _server(cls, v: str) -> str:
        v = v.strip().rstrip("/")
        if not re.fullmatch(r"https?://[^\s/]+(/[^\s]*)?", v):
            raise ValueError("must be an http:// or https:// address")
        return v


class Ntfy(AlertOutput):
    meta = OutputMeta(name="ntfy", description="Push notifications through ntfy (ntfy.sh or your own server).")
    ConfigModel = NtfyConfig
    cfg: NtfyConfig

    async def send(self, alert: Alert) -> None:
        sev = "info" if alert.kind in ("clear", "test") else alert.severity
        body = {"topic": self.cfg.topic, "title": alert.title, "message": alert.message, "priority": 2 if alert.kind == "clear" else PRIORITY[sev],
                "tags": ["white_check_mark"] if alert.kind == "clear" else ["test_tube"] if alert.kind == "test" else TAGS[sev]}
        if alert.url:
            body["click"] = alert.url
        headers = {"Authorization": f"Bearer {self.cfg.token}"} if self.cfg.token else {}
        r = await self.ctx.http.post(self.cfg.server, json=body, headers=headers, timeout=15)
        r.raise_for_status()
