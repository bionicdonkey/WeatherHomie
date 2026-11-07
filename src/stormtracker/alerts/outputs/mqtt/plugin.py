"""MQTT: publish each alert as JSON.

Topics: ``<prefix>/alerts/<rule id>`` (not retained) and ``<prefix>/last_alert`` (retained, so a new subscriber, such as
Home Assistant after a restart, immediately sees the latest one).
"""
from __future__ import annotations

import json

from pydantic import BaseModel, Field

from ....plugins import secret_field
from ...base import Alert, AlertOutput, OutputMeta
from .client import publish


class MqttConfig(BaseModel):
    host: str = Field(..., min_length=1, max_length=253, description="MQTT broker")
    port: int = Field(1883, ge=1, le=65535, description="1883 plain, 8883 for TLS")
    tls: bool = Field(False, description="connect with TLS")
    username: str | None = Field(None, description="login name, if the broker needs one")
    password: str | None = secret_field("Password for the login.")
    topic_prefix: str = Field("stormtracker", pattern=r"^[A-Za-z0-9_/-]{1,100}$", description="topics are <prefix>/alerts/<rule> and <prefix>/last_alert")
    client_id: str = Field("stormtracker", pattern=r"^[A-Za-z0-9_-]{1,23}$")


class Mqtt(AlertOutput):
    meta = OutputMeta(name="mqtt", description="Publish alerts to an MQTT broker (Home Assistant, Node-RED...).")
    ConfigModel = MqttConfig
    cfg: MqttConfig

    async def send(self, alert: Alert) -> None:
        c = self.cfg
        payload = json.dumps(alert.model_dump(mode="json"), separators=(",", ":")).encode()
        prefix = c.topic_prefix.strip("/")
        await publish(c.host, c.port, [(f"{prefix}/alerts/{alert.rule_id}", payload, False), (f"{prefix}/last_alert", payload, True)],
                      client_id=c.client_id, username=c.username or None, password=c.password if c.username else None, tls=c.tls)
