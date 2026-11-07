"""/api/now and the MCP server."""
import base64
from datetime import UTC, datetime, timedelta

from stormtracker.models import Event, Observation

MODERN = "2026-07-28"
META = {"io.modelcontextprotocol/protocolVersion": MODERN, "io.modelcontextprotocol/clientInfo": {"name": "t", "version": "1"}, "io.modelcontextprotocol/clientCapabilities": {}}


async def seed(app):
    now = datetime.now(UTC).replace(microsecond=0)
    core = app.state.core
    await core.locations.replace([__import__("stormtracker.locations", fromlist=["Location"]).Location(id="home", name="Home", lat=-33.87, lon=151.21)], "t")
    await core.repo.save([
        Observation(source="ecowitt", station_id="home_station", lat=-33.87, lon=151.21, ts=now - timedelta(minutes=2),
                    values={"temp_c": 21.4, "wind_speed_ms": 5.0, "wind_gust_ms": 10.0, "wind_dir_deg": 200, "rain_rate_mmh": 0.0, "lightning_count": 3, "lightning_distance_km": 12, "x_wh57batt": 5}),
        Observation(source="bom_observations", station_id="bom_1", lat=-33.93, lon=151.23, ts=now - timedelta(minutes=20), values={"temp_c": 20.0}),
        Event(source="ecowitt", event_type="lightning", external_id="l1", ts=now - timedelta(minutes=5), properties={"distance_km": 8.0}),
        Event(source="bom_warnings", event_type="warning", external_id="w1", ts=now - timedelta(hours=1), expires=now + timedelta(hours=1),
              properties={"title": "Severe Thunderstorm Warning for Sydney", "in_area": True, "severe_thunderstorm": True, "category": "severe_thunderstorm", "link": "http://bom.example/w"}),
        Event(source="bom_warnings", event_type="warning", external_id="old", ts=now - timedelta(hours=9), expires=now - timedelta(hours=1), properties={"title": "Old one"}),
    ])
    return now


async def test_now_is_self_describing(app, agent):
    await seed(app)
    j = (await agent.get("/api/now")).json()
    assert "authority" in j and "Bureau of Meteorology" in j["authority"] and j["generated_at"].endswith("+00:00")
    own = j["conditions"][0]
    assert own["station_id"] == "home_station" and own["own_station"] and own["age_min"] in (2, 3) and own["stale"] is False
    v, u = own["values"], own["units"]
    assert v["temp_c"] == 21.4 and u["temp_c"] == "°C" and v["wind_gust_kmh"] == 36.0 and u["wind_gust_kmh"] == "km/h"
    assert "x_wh57batt" not in v                                                            # internal diagnostics stay out
    assert j["lightning"]["strikes_last_60_min"] == 1 and j["lightning"]["nearest_km"] == 8.0 and j["lightning"]["sensor"]["count"] == 3
    w = j["warnings"]
    assert w["count"] == 1 and w["in_force"][0]["for_your_area"] and w["in_force"][0]["severe_thunderstorm"]      # expired ones are dropped
    assert j["location"]["name"] == "Home"
    assert j["rain"]["radar_status"] == "unknown" and "note" in j["rain"]                    # no radar here: says so rather than guessing
    assert j["forecast"]["available"] is False
    assert {"conditions", "rain", "storm_outlook", "lightning", "warnings", "forecast", "alerts", "data_freshness"} <= set(j)


async def test_now_sections_and_validation(app, agent):
    await seed(app)
    j = (await agent.get("/api/now?sections=warnings,lightning")).json()
    assert set(j) == {"generated_at", "timezone", "authority", "warnings", "lightning"}
    assert (await agent.get("/api/now?sections=bogus")).status_code == 422


async def test_now_flags_stale_stations(app, agent):
    core = app.state.core
    await core.repo.save([Observation(source="ecowitt", station_id="s", lat=0, lon=0, ts=datetime.now(UTC) - timedelta(hours=5), values={"temp_c": 9.0})])
    c = (await agent.get("/api/now?sections=conditions")).json()["conditions"][0]
    assert c["stale"] is True and c["age_min"] >= 299


# ---- MCP ----
def rpc(method, params=None, id_=1, modern=False):
    p = dict(params or {})
    if modern:
        p["_meta"] = META
    return {"jsonrpc": "2.0", "id": id_, "method": method, **({"params": p} if p or modern else {})}


def mh(method, name=None):
    h = {"MCP-Protocol-Version": MODERN, "Mcp-Method": method}
    if name:
        h["Mcp-Name"] = name
    return h


async def test_mcp_needs_an_api_token(anon, admin, agent):
    r = await anon.post("/mcp", json=rpc("ping"))
    assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Bearer")
    assert (await anon.post("/mcp", json=rpc("ping"), headers={"Authorization": "Bearer st_x_y"})).status_code == 401
    assert (await admin.post("/mcp", json=rpc("ping"))).status_code == 403                  # a browser session is not an agent
    assert (await agent.post("/mcp", json=rpc("ping"))).status_code == 200


async def test_mcp_legacy_handshake_and_tools(app, agent):
    await seed(app)
    r = (await agent.post("/mcp", json=rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "c", "version": "1"}}))).json()
    assert r["result"]["protocolVersion"] == "2025-06-18" and "tools" in r["result"]["capabilities"] and "Mcp-Session-Id" not in r
    r = await agent.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert r.status_code == 202 and r.content == b""
    tools = (await agent.post("/mcp", json=rpc("tools/list"))).json()["result"]["tools"]
    names = [t["name"] for t in tools]
    assert "get_weather_now" in names and len(names) == len(set(names)) == 7
    assert all(t["annotations"]["readOnlyHint"] and t["inputSchema"]["type"] == "object" for t in tools)
    out = (await agent.post("/mcp", json=rpc("tools/call", {"name": "get_current_conditions", "arguments": {}}))).json()["result"]
    assert out["isError"] is False and "resultType" not in out
    assert out["structuredContent"]["conditions"][0]["values"]["temp_c"] == 21.4
    import json
    assert json.loads(out["content"][0]["text"]) == out["structuredContent"]
    unk = (await agent.post("/mcp", json=rpc("tools/call", {"name": "delete_everything"}))).json()
    assert unk["error"]["code"] == -32602
    nm = (await agent.post("/mcp", json=rpc("resources/list"))).json()
    assert nm["error"]["code"] == -32601


async def test_mcp_modern_requests_and_header_checks(app, agent):
    await seed(app)
    d = (await agent.post("/mcp", json=rpc("server/discover", modern=True), headers=mh("server/discover"))).json()["result"]
    assert d["resultType"] == "complete" and MODERN in d["supportedVersions"] and d["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "stormtracker"
    t = (await agent.post("/mcp", json=rpc("tools/list", modern=True), headers=mh("tools/list"))).json()["result"]
    assert t["resultType"] == "complete" and len(t["tools"]) == 7
    call = rpc("tools/call", {"name": "get_lightning", "arguments": {}}, modern=True)
    r = await agent.post("/mcp", json=call, headers=mh("tools/call", "get_lightning"))
    assert r.json()["result"]["resultType"] == "complete" and r.json()["result"]["structuredContent"]["lightning"]["nearest_km"] == 8.0
    enc = "=?base64?" + base64.b64encode(b"get_lightning").decode() + "?="
    assert (await agent.post("/mcp", json=call, headers=mh("tools/call", enc))).status_code == 200      # base64 sentinel decoded before comparing
    # header/body mismatches are 400 with -32020
    for bad in (mh("tools/call", "get_forecast"), mh("tools/list"), {"MCP-Protocol-Version": MODERN, "Mcp-Method": "tools/call"}, {"Mcp-Method": "tools/call", "Mcp-Name": "get_lightning"}):
        r = await agent.post("/mcp", json=call, headers=bad)
        assert r.status_code == 400 and r.json()["error"]["code"] == -32020, bad
    # unsupported version: 400 -32022 listing what is supported
    old = rpc("tools/list"); old["params"] = {"_meta": {**META, "io.modelcontextprotocol/protocolVersion": "1900-01-01"}}
    r = await agent.post("/mcp", json=old, headers={"MCP-Protocol-Version": "1900-01-01", "Mcp-Method": "tools/list"})
    assert r.status_code == 400 and r.json()["error"]["code"] == -32022 and MODERN in r.json()["error"]["data"]["supported"]
    r = await agent.post("/mcp", json=rpc("tools/list"), headers={"MCP-Protocol-Version": "1900-01-01"})
    assert r.status_code == 400 and r.json()["error"]["code"] == -32022
    r = await agent.post("/mcp", json=rpc("nope", modern=True), headers=mh("nope"))
    assert r.status_code == 404 and r.json()["error"]["code"] == -32601                                 # modern: unknown method is a 404


async def test_mcp_transport_rules(agent):
    assert (await agent.get("/mcp")).status_code == 405 and (await agent.delete("/mcp")).status_code == 405
    assert (await agent.post("/mcp", content=b"{nope", headers={"Content-Type": "application/json"})).status_code == 400
    assert (await agent.post("/mcp", json=rpc("ping"), headers={"Origin": "http://evil.example"})).status_code == 403
    assert (await agent.post("/mcp", json=rpc("ping"), headers={"Origin": "http://test"})).status_code == 200
    assert (await agent.post("/mcp", content=b" " * 70000 + b"{}")).status_code == 413
    assert (await agent.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "result": {}})).status_code == 202      # a stray response is ignored
    assert (await agent.post("/mcp", json=[rpc("ping", id_=1), rpc("ping", id_=2)])).json() == [{"jsonrpc": "2.0", "id": 1, "result": {}}, {"jsonrpc": "2.0", "id": 2, "result": {}}]
    assert (await agent.post("/mcp", json={"nope": 1})).status_code == 400


async def test_mcp_history_tool_and_input_checks(app, agent):
    now = await seed(app)
    await app.state.core.repo.save([Observation(source="ecowitt", station_id="home_station", lat=-33.87, lon=151.21, ts=now - timedelta(minutes=10 * i),
                                                values={"temp_c": 20 + i, "rain_rate_mmh": 0.0}) for i in range(5, 50)])
    ok = (await agent.post("/mcp", json=rpc("tools/call", {"name": "get_observation_history", "arguments": {"hours": 6}}))).json()["result"]
    d = ok["structuredContent"]
    assert ok["isError"] is False and d["station_id"] == "home_station" and 2 <= d["point_count"] <= 60 and d["units"]["temp_c"] == "°C"
    assert d["points"] == sorted(d["points"], key=lambda p: p["t"])
    for bad in ({"hours": 0}, {"hours": 999}, {"hours": "6"}, {"hours": True}, {"station_id": 5}):
        r = (await agent.post("/mcp", json=rpc("tools/call", {"name": "get_observation_history", "arguments": bad}))).json()["result"]
        assert r["isError"] is True, bad
    nope = (await agent.post("/mcp", json=rpc("tools/call", {"name": "get_observation_history", "arguments": {"station_id": "ghost"}}))).json()["result"]["structuredContent"]
    assert "no such station" in nope["error"]


async def test_mcp_cannot_change_anything(agent, app):
    """Every tool is a read: there is no tool that writes, and calling all of them leaves settings and the audit log alone."""
    core = app.state.core
    before = len(await core.store.audit(500))
    tools = (await agent.post("/mcp", json=rpc("tools/list"))).json()["result"]["tools"]
    for t in tools:
        args = {} if "properties" not in t["inputSchema"] else {"hours": 1}
        await agent.post("/mcp", json=rpc("tools/call", {"name": t["name"], "arguments": args}))
    assert len(await core.store.audit(500)) == before
    assert not any(w in t["name"] for t in tools for w in ("set", "update", "delete", "create", "send", "put"))
