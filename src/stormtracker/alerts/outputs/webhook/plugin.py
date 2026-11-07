"""Webhook: POST each alert as JSON to a URL (n8n, Home Assistant, Node-RED, your own service...).

The body is ``Alert`` as JSON. Optionally the request carries an Authorization header and an HMAC-SHA256
signature of the exact body bytes (``X-StormTracker-Signature: sha256=<hex>``) so the receiver can check it came from here.
"""
from __future__ import annotations

import hashlib
import hmac
import json

from pydantic import BaseModel, Field

from ....plugins import secret_field
from ...base import Alert, AlertOutput, OutputMeta


class WebhookConfig(BaseModel):
    url: str | None = secret_field("Where to POST. Stored encrypted because URLs often contain secrets (n8n and Home Assistant webhook ids).", required=True)
    auth_header: str | None = secret_field("Sent as the Authorization header, for example 'Bearer abc123' (optional).")
    signing_key: str | None = secret_field("If set, each request is signed with this key so the receiver can verify it (optional).",
                                           generate="Give the receiver this key so it can verify requests: {value}")


def sign(key: str, body: bytes) -> str:
    return "sha256=" + hmac.new(key.encode(), body, hashlib.sha256).hexdigest()


class Webhook(AlertOutput):
    meta = OutputMeta(name="webhook", description="POST each alert as JSON to a URL, optionally signed.")
    ConfigModel = WebhookConfig
    cfg: WebhookConfig

    async def send(self, alert: Alert) -> None:
        body = json.dumps(alert.model_dump(mode="json"), separators=(",", ":")).encode()
        headers = {"Content-Type": "application/json", "X-StormTracker-Event": alert.kind}
        if self.cfg.auth_header:
            headers["Authorization"] = self.cfg.auth_header
        if self.cfg.signing_key:
            headers["X-StormTracker-Signature"] = sign(self.cfg.signing_key, body)
        r = await self.ctx.http.post(self.cfg.url, content=body, headers=headers, timeout=15)
        r.raise_for_status()
