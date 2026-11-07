from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from stormtracker.alerts.rules import Rule, Snapshot, evaluate
from stormtracker.plugins import HealthState
from stormtracker.sources.bom_warnings.plugin import BomWarnings, BomWarningsConfig, categorise, parse_feed

FX = Path(__file__).parents[2] / "fixtures" / "bom_warnings"
XML = (FX / "warnings_nsw.synthetic.xml").read_bytes()
NOW = "2026-11-06T11:30:00+00:00"


def plugin(make_ctx, body=XML, **cfg):
    return BomWarnings(BomWarningsConfig(**cfg), make_ctx(lambda r: httpx.Response(200, content=body)))


def test_parse_feed_cleans_titles_and_times():
    items = parse_feed(XML)
    assert len(items) == 4
    t = items[0]
    assert t["title"] == "06/16:08 EDT Severe Thunderstorm Warning for Sydney, Illawarra and parts of the Hunter"      # newline collapsed
    assert t["ts"] == datetime(2026, 11, 6, 5, 8, 23, tzinfo=UTC) and t["guid"].endswith("/thunderstorm.shtml")


def test_categories_with_severe_thunderstorm_first():
    assert categorise("Severe Thunderstorm Warning for Sydney") == "severe_thunderstorm"
    assert categorise("Thunderstorm Warning") == "thunderstorm"
    assert [categorise(t) for t in ("Marine Wind Warning Summary", "Hazardous Surf Warning", "Fire Weather Warning for X", "Flood Watch", "Something Odd")] == \
        ["marine_wind", "surf", "fire_weather", "flood", "other"]


def test_events_flag_severe_thunderstorm_and_expire_unless_seen_again(make_ctx):
    p = plugin(make_ctx)
    evs = p.normalise({"xml": XML, "received": NOW})
    assert len(evs) == 4 and all(e.event_type == "warning" and e.source == "bom_warnings" for e in evs)
    sev = [e for e in evs if e.properties["severe_thunderstorm"]]
    assert len(sev) == 1 and sev[0].properties["category"] == "severe_thunderstorm" and "thunderstorm.shtml" in sev[0].properties["link"]
    assert all(e.expires == datetime.fromisoformat(NOW) + timedelta(minutes=25) for e in evs)
    assert p.ctx.state.detail["items_in_feed"] == 4 and p.ctx.state.detail["severe_thunderstorm"] == 1
    assert len({e.external_id for e in evs}) == 4


def test_a_reissue_of_the_same_warning_keeps_its_identity_even_though_the_issue_time_changes(make_ctx):
    """BOM titles start with the issue time ("06/16:08 EDT ..."), which changes on every re-issue of the same warning."""
    p = plugin(make_ctx)
    first = p.normalise({"xml": XML, "received": NOW})[0]
    reissued = XML.replace(b"06/16:08 EDT Severe Thunderstorm", b"06/16:41 EDT Severe Thunderstorm").replace(b"Fri, 06 Nov 2026 05:08:23 GMT", b"Fri, 06 Nov 2026 05:41:02 GMT")
    second = p.normalise({"xml": reissued, "received": NOW})[0]
    assert first.external_id == second.external_id                                           # same warning: refreshed in place, not a new one
    assert first.properties["title"] != second.properties["title"] and second.ts == datetime(2026, 11, 6, 5, 41, 2, tzinfo=UTC)
    changed = XML.replace(b"for Sydney, Illawarra", b"for Sydney, Illawarra, Hawkesbury")    # genuinely different content under the same link
    third = p.normalise({"xml": changed, "received": NOW})[0]
    assert third.external_id != first.external_id and third.external_id.split("#")[0] == first.external_id.split("#")[0]


def test_identity_strips_only_the_issue_time():
    from stormtracker.sources.bom_warnings.plugin import identity
    assert identity("06/16:08 EDT Severe Thunderstorm Warning for Sydney") == identity("6/17:30 AEDT Severe Thunderstorm Warning for Sydney") == "severe thunderstorm warning for sydney"
    assert identity("Severe Thunderstorm Warning for Sydney") == "severe thunderstorm warning for sydney"           # no prefix: unchanged
    assert identity("Final Flood Warning for the Macintyre Brook") != identity("Minor Flood Warning for the Macintyre Brook")


def test_area_keywords_mark_warnings_but_never_hide_any(make_ctx):
    p = plugin(make_ctx, area_keywords="sydney, hawkesbury")
    evs = p.normalise({"xml": XML, "received": NOW})
    assert len(evs) == 4                                                                      # nothing dropped
    assert {e.properties["category"]: e.properties["in_area"] for e in evs} == {"severe_thunderstorm": True, "marine_wind": False, "surf": False, "fire_weather": False}
    assert p.ctx.state.detail["for_your_area"] == 1 and p.ctx.state.detail["severe_thunderstorm_for_your_area"] == 1 and p.ctx.state.detail["note"] is None
    p2 = plugin(make_ctx, area_keywords="canberra")                                           # words that match nothing: says so, instead of silently showing nothing
    evs = p2.normalise({"xml": XML, "received": NOW})
    assert len(evs) == 4 and not any(e.properties["in_area"] for e in evs) and "none of the warnings mention your area words" in p2.ctx.state.detail["note"]
    assert all(e.properties["in_area"] for e in plugin(make_ctx, area_keywords=" , ").normalise({"xml": XML, "received": NOW}))     # no words: every warning counts


def test_empty_feed_is_fine_and_hostile_xml_is_refused(make_ctx):
    p = plugin(make_ctx)
    assert p.normalise({"xml": (FX / "empty.synthetic.xml").read_bytes(), "received": NOW}) == [] and p.ctx.state.detail["items_in_feed"] == 0
    bomb = b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]><rss><channel><item><title>&b;</title></item></channel></rss>'
    with pytest.raises(ValueError, match="DTD or entity"):
        parse_feed(bomb)
    with pytest.raises(ValueError, match="large"):
        parse_feed(b"<rss>" + b"x" * 2_000_000)
    with pytest.raises(ValueError, match="not an RSS"):
        parse_feed(b"<html><body/></html>")
    with pytest.raises(ET_PARSE_ERRORS):
        parse_feed(b"this is not xml")


import xml.etree.ElementTree as _ET  # noqa: E402
ET_PARSE_ERRORS = _ET.ParseError


async def test_fetch_and_config(make_ctx):
    seen = []

    def h(req):
        seen.append(req)
        return httpx.Response(200, content=XML)
    p = BomWarnings(BomWarningsConfig(), make_ctx(h))
    raw = await p.fetch()
    assert str(seen[0].url) == "https://reg.bom.gov.au/fwo/IDZ00054.warnings_nsw.xml" and raw["xml"] == XML
    with pytest.raises(httpx.HTTPStatusError):
        await BomWarnings(BomWarningsConfig(), make_ctx(lambda r: httpx.Response(503))).fetch()
    for bad in ("https://evil.example.com/x.xml", "http://bom.gov.au.evil.com/x.xml", "https://reg.bom.gov.au/x.html", "file:///etc/passwd"):
        with pytest.raises(ValueError):
            BomWarningsConfig(feed_url=bad)
    assert BomWarningsConfig(feed_url="http://www.bom.gov.au/fwo/IDZ00055.warnings_vic.xml").grace_min == 25


async def test_a_withdrawn_warning_stops_alerting_and_the_rule_sees_the_live_one(admin, app, make_ctx):
    """Poll twice: the warning is in the feed, then it is gone. The alert rule fires only while it is in force."""
    repo = app.state.core.repo
    p = plugin(make_ctx, area_keywords="sydney")
    rule = Rule(id="w", name="Severe thunderstorm", params={"type": "warning", "match_text": "severe thunderstorm"})
    t0 = datetime(2026, 11, 6, 11, 30, tzinfo=UTC)
    await repo.save(p.normalise({"xml": XML, "received": t0.isoformat()}))
    stored = (await admin.get("/api/events?event_type=warning")).json()
    assert len(stored) == 4 and sum(e["properties"]["in_area"] for e in stored) == 1                    # all kept, one marked as yours
    stored = [e for e in stored if e["properties"]["severe_thunderstorm"]]
    assert len(stored) == 1

    def snap(now):
        from stormtracker.storage.tables import EventRow  # noqa: F401
        return Snapshot(now=now, warnings=[{"external_id": e["external_id"], "ts": datetime.fromisoformat(e["ts"]), "properties": e["properties"],
                                            "expires": datetime.fromisoformat(e["expires"]) if e["expires"] else None} for e in stored])
    assert [c.key for c in evaluate(rule, snap(t0 + timedelta(minutes=5)))] == [f"warning:{stored[0]['external_id']}"]
    assert evaluate(rule, snap(t0 + timedelta(minutes=40))) == []                    # 25 min grace passed with no further poll: withdrawn
    await repo.save(p.normalise({"xml": XML, "received": (t0 + timedelta(minutes=30)).isoformat()}))          # still in the feed: refreshed in place
    stored = [e for e in (await admin.get("/api/events?event_type=warning")).json() if e["properties"]["severe_thunderstorm"]]
    assert len(stored) == 1 and evaluate(rule, snap(t0 + timedelta(minutes=40)))
    assert stored[0]["expires"] == (t0 + timedelta(minutes=55)).isoformat()
    # the rule ignores a warning that is not marked for your area unless asked to look at all of them
    other = [{"external_id": "x", "ts": t0, "expires": t0 + timedelta(hours=1), "properties": {"title": "Severe Thunderstorm Warning for Newcastle", "in_area": False}}]
    assert evaluate(rule, Snapshot(now=t0, warnings=other)) == []
    assert len(evaluate(Rule(id="w2", name="Any", params={"type": "warning", "match_text": "severe thunderstorm", "area_only": False}), Snapshot(now=t0, warnings=other))) == 1


@pytest.mark.skipif(not (FX / "real" / "IDZ00054.warnings_nsw.xml").exists(), reason="no real BOM feed download")
def test_a_real_feed_parses(make_ctx):
    raw = (FX / "real" / "IDZ00054.warnings_nsw.xml").read_bytes()
    items = parse_feed(raw)
    evs = plugin(make_ctx).normalise({"xml": raw, "received": NOW})
    assert len(evs) == len(items) and all(e.properties["title"] and e.properties["category"] for e in evs)
