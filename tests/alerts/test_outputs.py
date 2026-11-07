import asyncio
import hashlib
import hmac
import json
import logging
import struct
from datetime import UTC, datetime

import httpx
import pytest

from stormtracker.alerts.base import Alert, OutputContext
from stormtracker.alerts.outputs.email.plugin import Email, EmailConfig
from stormtracker.alerts.outputs.mqtt.client import MqttError, read_packet, publish as mqtt_publish
from stormtracker.alerts.outputs.mqtt.plugin import Mqtt, MqttConfig
from stormtracker.alerts.outputs.ntfy.plugin import Ntfy, NtfyConfig
from stormtracker.alerts.outputs.webhook.plugin import Webhook, WebhookConfig, sign

ALERT = Alert(id="abc123", ts=datetime(2026, 11, 6, 10, 30, tzinfo=UTC), rule_id="storm", rule_name="Storm heading for home", key="cell:1",
              severity="critical", title="Storm heading for home — réaliste ⛈", message="A storm cell of 60 mm/h should reach home in about 17 min.",
              url="http://stormtracker.lan:8000/#/map", data={"eta_min": 17})


def ctx(handler):
    return OutputContext(httpx.AsyncClient(transport=httpx.MockTransport(handler)), logging.getLogger("t"))


# ---- ntfy -------------------------------------------------------------------------------------------------------------
async def test_ntfy_json_publish_with_priority_tags_click_and_token():
    seen = []

    def h(req):
        seen.append(req)
        return httpx.Response(200, json={"id": "x"})
    cfg = NtfyConfig(server="https://ntfy.example.org/", topic="secret-topic-123", token="tk_abc")
    await Ntfy(cfg, ctx(h)).send(ALERT)
    r = seen[0]
    body = json.loads(r.content)
    assert str(r.url) == "https://ntfy.example.org" and r.headers["authorization"] == "Bearer tk_abc"
    assert body == {"topic": "secret-topic-123", "title": ALERT.title, "message": ALERT.message, "priority": 5, "tags": ["rotating_light"], "click": ALERT.url}
    assert "secret-topic-123" not in str(r.url)                                          # the topic is never in the URL
    for kind, prio in (("clear", 2), ("test", 3)):
        await Ntfy(NtfyConfig(topic="t"), ctx(h)).send(ALERT.model_copy(update={"kind": kind}))
        assert json.loads(seen[-1].content)["priority"] == prio
    assert "authorization" not in seen[-1].headers                                      # no token configured: no header
    await Ntfy(NtfyConfig(topic="t"), ctx(h)).send(ALERT.model_copy(update={"severity": "info", "url": None}))
    assert "click" not in json.loads(seen[-1].content) and json.loads(seen[-1].content)["priority"] == 3


async def test_ntfy_http_error_raises_and_server_is_validated():
    with pytest.raises(httpx.HTTPStatusError):
        await Ntfy(NtfyConfig(topic="t"), ctx(lambda r: httpx.Response(403))).send(ALERT)
    with pytest.raises(ValueError):
        NtfyConfig(server="ntfy.sh", topic="t")


# ---- webhook ----------------------------------------------------------------------------------------------------------
async def test_webhook_posts_json_and_the_signature_verifies_against_the_exact_bytes():
    seen = []

    def h(req):
        seen.append(req)
        return httpx.Response(204)
    cfg = WebhookConfig(url="https://hooks.example.org/x/SECRET", auth_header="Bearer abc", signing_key="shared-key")
    await Webhook(cfg, ctx(h)).send(ALERT)
    r = seen[0]
    got = json.loads(r.content)
    assert got["title"] == ALERT.title and got["rule_id"] == "storm" and got["kind"] == "alert" and got["data"] == {"eta_min": 17} and got["ts"].startswith("2026-11-06T10:30")
    assert r.headers["authorization"] == "Bearer abc" and r.headers["x-stormtracker-event"] == "alert"
    expect = "sha256=" + hmac.new(b"shared-key", r.content, hashlib.sha256).hexdigest()
    assert r.headers["x-stormtracker-signature"] == expect == sign("shared-key", r.content)
    await Webhook(WebhookConfig(url="https://hooks.example.org/y"), ctx(h)).send(ALERT)
    assert "x-stormtracker-signature" not in seen[-1].headers and "authorization" not in seen[-1].headers
    with pytest.raises(httpx.HTTPStatusError):
        await Webhook(WebhookConfig(url="https://hooks.example.org/y"), ctx(lambda r: httpx.Response(500))).send(ALERT)


# ---- email: a real SMTP conversation ---------------------------------------------------------------------------------------
class FakeSmtp:
    def __init__(self, require_login=None):
        self.messages, self.logins, self.require_login = [], [], require_login

    async def handle(self, r, w):
        w.write(b"220 fake ESMTP\r\n")
        data_mode, buf, authed = False, [], False
        while True:
            line = await r.readline()
            if not line:
                return
            if data_mode:
                if line == b".\r\n":
                    self.messages.append(b"".join(buf).decode())
                    w.write(b"250 queued\r\n"); data_mode = False; buf = []
                else:
                    buf.append(line)
                await w.drain(); continue
            cmd = line.decode().strip()
            up = cmd.upper()
            if up.startswith("EHLO"):
                w.write(b"250-fake\r\n250-AUTH PLAIN\r\n250 8BITMIME\r\n" if self.require_login else b"250-fake\r\n250 8BITMIME\r\n")
            elif up.startswith("AUTH PLAIN"):
                import base64
                _, user, pw = base64.b64decode(cmd.split()[2]).split(b"\0")
                self.logins.append((user.decode(), pw.decode()))
                ok = (user.decode(), pw.decode()) == self.require_login
                authed = ok
                w.write(b"235 ok\r\n" if ok else b"535 bad credentials\r\n")
            elif up.startswith(("MAIL", "RCPT")):
                w.write(b"250 ok\r\n" if (authed or not self.require_login) else b"530 auth required\r\n")
            elif up == "DATA":
                w.write(b"354 go\r\n"); data_mode = True
            elif up == "QUIT":
                w.write(b"221 bye\r\n"); await w.drain(); w.close(); return
            else:
                w.write(b"250 ok\r\n")
            await w.drain()


async def smtp_server(**kw):
    f = FakeSmtp(**kw)
    srv = await asyncio.start_server(f.handle, "127.0.0.1", 0)
    return f, srv, srv.sockets[0].getsockname()[1]


async def test_email_is_delivered_with_subject_body_link_and_all_recipients():
    f, srv, port = await smtp_server()
    async with srv:
        cfg = EmailConfig(host="127.0.0.1", port=port, security="none", from_addr="storm@example.org", to_addrs="a@example.org, b@example.org\nc@example.org")
        await Email(cfg, ctx(None)).send(ALERT)
    (msg,) = f.messages
    assert "Subject: [StormTracker] Storm heading for home" in msg and "To: a@example.org, b@example.org, c@example.org" in msg and "From: storm@example.org" in msg
    assert "A storm cell of 60 mm/h" in msg and "http://stormtracker.lan:8000/#/map" in msg and "Rule: Storm heading for home" in msg


async def test_email_login_and_failures():
    f, srv, port = await smtp_server(require_login=("user", "pw-secret"))
    async with srv:
        base = dict(host="127.0.0.1", port=port, security="none", from_addr="s@example.org", to_addrs="a@example.org", username="user")
        await Email(EmailConfig(**base, password="pw-secret"), ctx(None)).send(ALERT)
        assert f.logins == [("user", "pw-secret")] and len(f.messages) == 1
        with pytest.raises(Exception) as e:
            await Email(EmailConfig(**base, password="wrong"), ctx(None)).send(ALERT)
        assert "535" in str(e.value) and "bad credentials" in str(e.value)
    with pytest.raises(OSError):                                                         # nothing listening any more
        await Email(EmailConfig(host="127.0.0.1", port=port, security="none", from_addr="s@example.org", to_addrs="a@example.org"), ctx(None)).send(ALERT)


def test_email_address_validation():
    base = dict(host="h", from_addr="s@example.org")
    for bad in ("", "nobody", "a@b", "a@example.org, oops"):
        with pytest.raises(ValueError):
            EmailConfig(**base, to_addrs=bad)
    with pytest.raises(ValueError):
        EmailConfig(host="h", from_addr="nope", to_addrs="a@example.org")
    assert EmailConfig(**base, to_addrs="a@example.org;b@example.org  c@example.org").to_addrs == "a@example.org, b@example.org, c@example.org"


# ---- MQTT: a broker that speaks the actual wire protocol ------------------------------------------------------------------
class FakeBroker:
    def __init__(self, creds=None, drop_acks=False):
        self.connects, self.publishes, self.creds, self.drop_acks = [], [], creds, drop_acks

    async def handle(self, r, w):
        try:
            first, body = await read_packet(r)
            assert first == 0x10
            n = struct.unpack("!H", body[:2])[0]
            assert body[2:2 + n] == b"MQTT" and body[2 + n] == 4                         # protocol MQTT 3.1.1
            flags, keepalive = body[3 + n], struct.unpack("!H", body[4 + n:6 + n])[0]
            rest, fields = body[6 + n:], []
            while rest:
                ln = struct.unpack("!H", rest[:2])[0]
                fields.append(rest[2:2 + ln].decode()); rest = rest[2 + ln:]
            client_id, user, pw = fields[0], fields[1] if flags & 0x80 else None, fields[2] if flags & 0x40 else None
            self.connects.append({"client_id": client_id, "user": user, "pw": pw, "clean": bool(flags & 0x02), "keepalive": keepalive})
            if self.creds and (user, pw) != self.creds:
                w.write(b"\x20\x02\x00\x04"); await w.drain(); return                    # bad username or password
            w.write(b"\x20\x02\x00\x00"); await w.drain()
            while True:
                first, body = await read_packet(r)
                if first >> 4 == 14:                                                     # DISCONNECT
                    return
                assert first >> 4 == 3
                qos, retain = (first >> 1) & 3, bool(first & 1)
                tl = struct.unpack("!H", body[:2])[0]
                topic, off = body[2:2 + tl].decode(), 2 + tl
                pid = struct.unpack("!H", body[off:off + 2])[0] if qos else None
                payload = body[off + (2 if qos else 0):]
                self.publishes.append({"topic": topic, "payload": payload, "qos": qos, "retain": retain})
                if qos and not self.drop_acks:
                    w.write(b"\x40\x02" + struct.pack("!H", pid)); await w.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            w.close()


async def broker(**kw):
    b = FakeBroker(**kw)
    srv = await asyncio.start_server(b.handle, "127.0.0.1", 0)
    return b, srv, srv.sockets[0].getsockname()[1]


async def test_mqtt_publishes_two_topics_at_qos1_with_the_right_retain_flags():
    b, srv, port = await broker(creds=("user", "pw"))
    async with srv:
        cfg = MqttConfig(host="127.0.0.1", port=port, username="user", password="pw", topic_prefix="home/storm/")
        await Mqtt(cfg, ctx(None)).send(ALERT)
    assert b.connects == [{"client_id": "stormtracker", "user": "user", "pw": "pw", "clean": True, "keepalive": 30}]
    a, last = b.publishes
    assert (a["topic"], a["qos"], a["retain"]) == ("home/storm/alerts/storm", 1, False)
    assert (last["topic"], last["qos"], last["retain"]) == ("home/storm/last_alert", 1, True)
    assert json.loads(a["payload"])["title"] == ALERT.title and json.loads(last["payload"])["data"] == {"eta_min": 17}
    assert a["payload"].decode("utf-8")                                                  # valid UTF-8 even with accents and an emoji


async def test_mqtt_anonymous_refusal_and_unreachable_broker():
    b, srv, port = await broker()
    async with srv:
        await Mqtt(MqttConfig(host="127.0.0.1", port=port), ctx(None)).send(ALERT)
        assert b.connects[0]["user"] is None and b.connects[0]["pw"] is None and len(b.publishes) == 2
    b2, srv2, port2 = await broker(creds=("user", "right"))
    async with srv2:
        with pytest.raises(MqttError, match="bad username or password"):
            await Mqtt(MqttConfig(host="127.0.0.1", port=port2, username="user", password="wrong"), ctx(None)).send(ALERT)
    with pytest.raises(OSError):
        await Mqtt(MqttConfig(host="127.0.0.1", port=port2), ctx(None)).send(ALERT)


async def test_mqtt_missing_ack_times_out_instead_of_hanging():
    b, srv, port = await broker(drop_acks=True)
    async with srv:
        with pytest.raises((TimeoutError, asyncio.TimeoutError)):
            await mqtt_publish("127.0.0.1", port, [("t", b"x", False)], client_id="c", timeout=0.5)


def test_mqtt_config_validation():
    with pytest.raises(ValueError):
        MqttConfig(host="h", topic_prefix="bad topic!")
    with pytest.raises(ValueError):
        MqttConfig(host="h", client_id="x" * 30)
