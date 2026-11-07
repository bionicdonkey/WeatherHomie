"""Apprise: one destination that reaches 100+ services (Telegram, Discord, Slack, Pushover, Matrix, Signal, SMS...).

Paste one Apprise URL per line, for example ``tgram://<bot token>/<chat id>`` or ``discord://<webhook id>/<token>``.
The URL formats are documented at https://github.com/caronc/apprise/wiki. URLs carry tokens, so they are stored
encrypted and never shown again.
"""
from __future__ import annotations

import logging

import apprise
from pydantic import BaseModel

from ....plugins import secret_field
from ...base import Alert, AlertOutput, OutputMeta

NOTIFY_TYPE = {"info": apprise.NotifyType.INFO, "warning": apprise.NotifyType.WARNING, "critical": apprise.NotifyType.FAILURE}


class AppriseConfig(BaseModel):
    urls: str | None = secret_field(
        "Apprise service URLs, one per line (Telegram, Discord, Slack, Pushover, Matrix, SMS and many more). "
        "Format guide: https://github.com/caronc/apprise/wiki",
        required=True, multiline=True)


def _lines(text: str | None) -> list[str]:
    return [ln.strip() for ln in (text or "").splitlines() if ln.strip() and not ln.strip().startswith("#")]


class AppriseOutput(AlertOutput):
    meta = OutputMeta(name="apprise", description="Send alerts through Apprise to Telegram, Discord, Slack, Pushover and 100+ other services.")
    ConfigModel = AppriseConfig
    cfg: AppriseConfig

    @staticmethod
    def validate_secret(field: str, value: str) -> str | None:
        """Checked when you save, so a typo is caught then. The message never repeats the URL: it holds a token."""
        if field != "urls":
            return None
        lines = _lines(value)
        if not lines:
            return "add at least one Apprise URL"
        probe = apprise.Apprise()
        for n, ln in enumerate(lines, 1):
            if not probe.add(ln):
                return f"line {n} is not a service URL Apprise understands (see the format guide)"
        return None

    async def send(self, alert: Alert) -> None:
        ap = apprise.Apprise()
        for n, ln in enumerate(_lines(self.cfg.urls), 1):
            if not ap.add(ln):
                raise ValueError(f"line {n} is not a service URL Apprise understands")
        if not len(ap):
            raise ValueError("no Apprise URLs configured")
        kind = "info" if alert.kind in ("clear", "test") else alert.severity
        ntype = apprise.NotifyType.SUCCESS if alert.kind == "clear" else NOTIFY_TYPE[kind]
        body = alert.message + (f"\n{alert.url}" if alert.url else "")
        with apprise.LogCapture(level=logging.INFO, fmt="%(message)s") as log:
            ok = await ap.async_notify(title=alert.title, body=body, notify_type=ntype)
        if not ok:
            detail = " ".join(x.strip() for x in log.getvalue().splitlines() if x.strip())[:240]
            raise RuntimeError(f"Apprise could not deliver the message{': ' + detail if detail else ''}")
