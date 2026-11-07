"""Email over SMTP (STARTTLS, implicit TLS, or plain for a local relay)."""
from __future__ import annotations

import asyncio
import re
import smtplib
import ssl
from email.message import EmailMessage
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from ....plugins import secret_field
from ...base import Alert, AlertOutput, OutputMeta

ADDR = re.compile(r"^[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+$")


class EmailConfig(BaseModel):
    host: str = Field(..., min_length=1, max_length=253, description="SMTP server")
    port: int = Field(587, ge=1, le=65535)
    security: Literal["starttls", "ssl", "none"] = Field("starttls", description="starttls (port 587), ssl (port 465), or none for a trusted local relay")
    username: str | None = Field(None, description="login name, if the server needs one")
    password: str | None = secret_field("Password or app password for the login.")
    from_addr: str = Field(..., description="sender address")
    to_addrs: str = Field(..., description="who gets alerts: one or more addresses separated by commas, spaces or new lines")
    subject_prefix: str = Field("[StormTracker] ", max_length=40)

    @field_validator("from_addr")
    @classmethod
    def _from(cls, v: str) -> str:
        if not ADDR.match(v.strip()):
            raise ValueError("not a valid email address")
        return v.strip()

    @field_validator("to_addrs")
    @classmethod
    def _to(cls, v: str) -> str:
        addrs = [a for a in re.split(r"[,\s;]+", v.strip()) if a]
        bad = [a for a in addrs if not ADDR.match(a)]
        if not addrs or bad:
            raise ValueError("needs at least one valid email address" if not addrs else f"not a valid email address: {bad[0]}")
        return ", ".join(addrs)


class Email(AlertOutput):
    meta = OutputMeta(name="email", description="Email alerts through an SMTP server.")
    ConfigModel = EmailConfig
    cfg: EmailConfig

    def build(self, alert: Alert) -> EmailMessage:
        m = EmailMessage()
        m["From"], m["To"] = self.cfg.from_addr, self.cfg.to_addrs
        m["Subject"] = f"{self.cfg.subject_prefix}{alert.title}"
        lines = [alert.message, ""]
        if alert.url:
            lines += [f"Open StormTracker: {alert.url}", ""]
        lines.append(f"Rule: {alert.rule_name}")
        m.set_content("\n".join(lines))
        return m

    def _send_sync(self, m: EmailMessage) -> None:
        c = self.cfg
        ctx = ssl.create_default_context()
        smtp = smtplib.SMTP_SSL(c.host, c.port, timeout=20, context=ctx) if c.security == "ssl" else smtplib.SMTP(c.host, c.port, timeout=20)
        with smtp:
            smtp.ehlo()
            if c.security == "starttls":
                smtp.starttls(context=ctx)
                smtp.ehlo()
            if c.username and c.password:
                smtp.login(c.username, c.password)
            smtp.send_message(m)

    async def send(self, alert: Alert) -> None:
        await asyncio.to_thread(self._send_sync, self.build(alert))
