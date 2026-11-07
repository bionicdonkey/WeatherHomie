import asyncio
import json
import logging
from datetime import UTC, datetime

import httpx
import pytest

from stormtracker.alerts.base import Alert, OutputContext
from stormtracker.alerts.outputs.apprise.plugin import AppriseConfig, AppriseOutput
from stormtracker.scheduler import redact

ALERT = Alert(id="a1", ts=datetime(2026, 11, 6, 10, 30, tzinfo=UTC), rule_id="storm", rule_name="Storm", key="cell:1", severity="critical",
              title="Storm heading for home", message="A storm cell should reach home in about 17 min.", url="http://storm.lan:8000/#/map")
CTX = OutputContext(httpx.AsyncClient(), logging.getLogger("t"))


class Receiver:
    """A tiny HTTP service: records requests, answers with a chosen status."""
    def __init__(self, status=200):
        self.requests, self.status = [], status

    async def handle(self, r, w):
        head = await r.readuntil(b"\r\n\r\n")
        lines = head.decode().split("\r\n")
        headers = {k.lower(): v for k, v in (ln.split(": ", 1) for ln in lines[1:] if ": " in ln)}
        body = await r.readexactly(int(headers.get("content-length", 0)))
        self.requests.append({"line": lines[0], "body": body})
        w.write(f"HTTP/1.1 {self.status} X\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{{}}".encode())
        await w.drain()
        w.close()


async def receiver(**kw):
    rc = Receiver(**kw)
    srv = await asyncio.start_server(rc.handle, "127.0.0.1", 0)
    return rc, srv, srv.sockets[0].getsockname()[1]


async def test_delivers_through_apprise_with_title_type_and_link():
    rc, srv, port = await receiver()
    async with srv:
        await AppriseOutput(AppriseConfig(urls=f"json://127.0.0.1:{port}/hook"), CTX).send(ALERT)
    (req,) = rc.requests
    body = json.loads(req["body"])
    assert req["line"].startswith("POST /hook") and body["title"] == "Storm heading for home"
    assert "about 17 min" in body["message"] and "http://storm.lan:8000/#/map" in body["message"]
    assert body["type"] == "failure"                                           # critical -> Apprise's loudest type


async def test_notify_types_follow_severity_and_kind():
    rc, srv, port = await receiver()
    async with srv:
        out = AppriseOutput(AppriseConfig(urls=f"json://127.0.0.1:{port}/h"), CTX)
        for update, want in (({"severity": "warning"}, "warning"), ({"severity": "info"}, "info"), ({"kind": "clear"}, "success"), ({"kind": "test"}, "info")):
            await out.send(ALERT.model_copy(update=update))
    assert [json.loads(r["body"])["type"] for r in rc.requests] == ["warning", "info", "success", "info"]


async def test_several_urls_one_per_line_comments_ignored():
    a, srv_a, pa = await receiver()
    b, srv_b, pb = await receiver()
    async with srv_a, srv_b:
        urls = f"# my phone and my desk\njson://127.0.0.1:{pa}/a\n\n  json://127.0.0.1:{pb}/b  "
        await AppriseOutput(AppriseConfig(urls=urls), CTX).send(ALERT)
    assert len(a.requests) == 1 and len(b.requests) == 1


async def test_a_failing_service_is_reported_not_swallowed():
    rc, srv, port = await receiver(status=500)
    async with srv:
        with pytest.raises(RuntimeError, match="Apprise could not deliver"):
            await AppriseOutput(AppriseConfig(urls=f"json://127.0.0.1:{port}/h"), CTX).send(ALERT)
    ok, srv2, port2 = await receiver()
    async with srv2:                                                          # one good + one dead: still an error, the good one was sent
        with pytest.raises(RuntimeError):
            await AppriseOutput(AppriseConfig(urls=f"json://127.0.0.1:{port2}/h\njson://127.0.0.1:1/dead"), CTX).send(ALERT)
    assert len(ok.requests) == 1


async def test_bad_urls_are_rejected_without_repeating_them():
    with pytest.raises(ValueError) as e:
        await AppriseOutput(AppriseConfig(urls="json://127.0.0.1:9/h\nbogus://SECRET-TOKEN-123"), CTX).send(ALERT)
    assert "line 2" in str(e.value) and "SECRET-TOKEN-123" not in str(e.value)
    v = AppriseOutput.validate_secret
    assert v("urls", "json://a/b\nntfy://t") is None and "line 2" in v("urls", "json://a/b\nbogus://x") and "SECRET" not in v("urls", "bogus://SECRET")
    assert v("urls", "  \n# only a comment") == "add at least one Apprise URL"


def test_redaction_covers_each_url_in_a_list():
    out = AppriseOutput(AppriseConfig(urls="tgram://BOT-TOKEN-aaaa/123\ndiscord://HOOK-ID-bbbb/HOOK-TOKEN"), CTX)
    msg = "Failed to send to discord://HOOK-ID-bbbb/HOOK-TOKEN: 401"                # an error that quotes just one of them
    assert "HOOK-ID-bbbb" not in redact(out, msg) and "***" in redact(out, msg)
    assert "tgram://BOT-TOKEN-aaaa/123" not in redact(out, "x tgram://BOT-TOKEN-aaaa/123 y")


async def test_api_validates_on_save_and_never_echoes_the_urls(admin, app):
    r = await admin.put("/api/alerts/outputs/apprise", json={"enabled": True, "secrets": {"urls": "json://h/x\nbogus://TOKEN-XYZ-987"}})
    assert r.status_code == 422 and "line 2" in r.text and "TOKEN-XYZ-987" not in r.text
    r = await admin.put("/api/alerts/outputs/apprise", json={"enabled": True, "secrets": {"urls": "json://127.0.0.1:9/ok-TOKEN-XYZ-987"}})
    assert r.status_code == 200 and r.json()["settings"]["secrets_set"] == {"urls": True} and "TOKEN-XYZ-987" not in r.text
    assert "TOKEN-XYZ-987" not in (await admin.get("/api/alerts/outputs")).text and "TOKEN-XYZ-987" not in (await admin.get("/api/audit")).text
    assert next(o for o in (await admin.get("/api/alerts/outputs")).json() if o["meta"]["name"] == "apprise")["schema"]["properties"]["urls"]["format"] == "textarea"


async def test_a_test_message_through_the_service_end_to_end(admin, app):
    rc, srv, port = await receiver()
    async with srv:
        await admin.put("/api/alerts/outputs/apprise", json={"enabled": True, "secrets": {"urls": f"json://127.0.0.1:{port}/api"}})
        assert (await admin.post("/api/alerts/outputs/apprise/test")).json() == {"ok": True, "error": None}
    assert json.loads(rc.requests[0]["body"])["title"] == "Test alert from StormTracker"
    rc2, srv2, port2 = await receiver(status=502)
    async with srv2:
        await admin.put("/api/alerts/outputs/apprise", json={"secrets": {"urls": f"json://127.0.0.1:{port2}/api"}})
        res = (await admin.post("/api/alerts/outputs/apprise/test")).json()
    assert res["ok"] is False and "Apprise could not deliver" in res["error"]
