"""BOM weather warnings for a state (the public RSS feed), flagging severe thunderstorm warnings.

Each item in the feed is a warning currently in force. Every poll refreshes ``expires`` to "now + a grace period", so
a warning that BOM withdraws or lets lapse drops out of the feed and expires here shortly after; the alert rule only
considers warnings that have not expired. The feed is per state. Nothing is ever dropped: ``area_keywords`` only *marks*
warnings whose title mentions your area (``in_area``), so a warning worded unexpectedly still shows up and can be seen.

Terms: BOM public warnings are for personal use under its copyright notice and disclaimer. This source relays the
warning's title and link only; for anything safety-critical use BOM's own site/app and official emergency services.
"""
from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from ... import bom_catalogue as cat
from ... import geocode
from ...models import Event, Normalised
from ...plugins import Candidate, Health, HealthState, Licence, RateLimit, Source, SourceMeta, Suggestion
from ...timeutil import utcnow

MAX_BYTES = 1_000_000
CATEGORIES = [                       # first match wins: (keyword in the title, category)
    ("severe thunderstorm", "severe_thunderstorm"), ("thunderstorm", "thunderstorm"), ("flood", "flood"), ("tsunami", "tsunami"),
    ("cyclone", "tropical_cyclone"), ("fire weather", "fire_weather"), ("fire danger", "fire_weather"), ("marine wind", "marine_wind"),
    ("gale", "marine_wind"), ("strong wind", "wind"), ("damaging wind", "wind"), ("hazardous surf", "surf"), ("surf", "surf"),
    ("severe weather", "severe_weather"), ("heatwave", "heatwave"), ("frost", "frost"), ("sheep", "agricultural"), ("bushwalking", "bushwalking"),
]


class BomWarningsConfig(BaseModel):
    feed_url: str = Field("https://reg.bom.gov.au/fwo/IDZ00054.warnings_nsw.xml", description="BOM warnings feed for your state (this one is NSW and ACT)")
    area_keywords: str = Field("", description="words that mark a warning as being for your area when its title contains one, separated by commas. Titles name BOM's forecast districts and vary "
                                                "(for example 'Sydney Metropolitan', 'Sydney and Wollondilly/Wingecarribee', 'parts of Sydney'), so list several. Nothing is hidden: other warnings are kept and shown as the state's. Empty marks every warning.")
    grace_min: int = Field(25, ge=10, le=180, description="a warning counts as in force this long after it was last seen in the feed")

    @field_validator("feed_url")
    @classmethod
    def _url(cls, v: str) -> str:
        if not re.fullmatch(r"https?://([A-Za-z0-9-]+\.)*bom\.gov\.au/[^\s]+\.xml", v.strip()):
            raise ValueError("must be an http(s) address on bom.gov.au ending in .xml")
        return v.strip()

    @property
    def keywords(self) -> list[str]:
        return [k.strip().lower() for k in self.area_keywords.split(",") if k.strip()]


ISSUE_PREFIX = re.compile(r"^\d{1,2}/\d{2}:\d{2}\s+[A-Z]{2,5}\s+")      # "06/16:08 EDT ": when this issue went out, which changes on every re-issue


def identity(title: str) -> str:
    """The warning's identity: its title without the issue time, so a re-issue of the same warning is the same warning."""
    return ISSUE_PREFIX.sub("", title).strip().lower()


def categorise(title: str) -> str:
    t = title.lower()
    return next((cat for kw, cat in CATEGORIES if kw in t), "other")


def parse_feed(xml: bytes) -> list[dict[str, Any]]:
    """RSS bytes -> [{title, link, guid, ts}]. Refuses DTDs/entities and oversized bodies."""
    if len(xml) > MAX_BYTES:
        raise ValueError("feed is unexpectedly large")
    head = xml[:4096].lower()
    if b"<!doctype" in head or b"<!entity" in xml.lower():
        raise ValueError("feed contains a DTD or entity declaration; refusing to parse it")
    root = ET.fromstring(xml)
    if root.tag != "rss":
        raise ValueError("not an RSS feed")
    out = []
    for it in root.iter("item"):
        title = re.sub(r"\s+", " ", (it.findtext("title") or "")).strip()
        link = (it.findtext("link") or "").strip()
        try:
            ts = parsedate_to_datetime(it.findtext("pubDate") or "").astimezone(UTC)
        except (TypeError, ValueError):
            ts = None
        if title:
            out.append({"title": title, "link": link, "guid": (it.findtext("guid") or link).strip(), "ts": ts})
    return out


class BomWarnings(Source):
    meta = SourceMeta(
        name="bom_warnings",
        category="warnings",
        description="BOM weather warnings for your state, flagging severe thunderstorm warnings.",
        poll_interval_s=600,
        rate_limit=RateLimit(max_requests=1, period_s=300),
        licence=Licence(type="personal", terms_url="http://www.bom.gov.au/other/copyright.shtml",
                        attribution="Warnings: Commonwealth of Australia, Bureau of Meteorology",
                        notes="Free for personal use under BOM's copyright notice. Not a substitute for BOM's own warnings or emergency services."),
    )
    ConfigModel = BomWarningsConfig
    cfg: BomWarningsConfig

    @classmethod
    async def suggest(cls, location, ctx, current):
        """The state's warnings feed, from which state the nearest open BOM stations are in. Near a border the other
        state's feed is offered too. Area words are not guessed: warning titles name forecast districts, and mapping a
        point to a district needs BOM's district boundaries."""
        stations = await cat.load_stations(ctx.http, ctx.data_dir)
        ranked = [(km, s) for km, s in cat.nearest(stations, location["lat"], location["lon"], limit=40, max_km=150)
                  if cat.WARNING_FEEDS.get(s.state)]
        if not ranked:
            return Suggestion(candidates=[], note="No BOM station within 150 km of this location, so the state couldn't be worked out.")
        home_state = ranked[0][1].state
        out, seen = [], set()
        for km, st in ranked:
            fid = cat.WARNING_FEEDS[st.state]
            if fid in seen or (st.state != home_state and km > 30):
                continue
            seen.add(fid)
            url = cat.warnings_feed_url(st.state)
            out.append(Candidate(label=f"{cat.STATE_NAMES[st.state]} warnings", values={"feed_url": url}, current=current.get("feed_url") == url,
                                 detail=("nearest stations are here" if st.state == home_state else f"a station in {st.state} is only {km:.0f} km away")
                                 + f" · {fid}"))
        note = ("Warning titles use BOM's forecast-district names, which place names only roughly match, so treat any area words as a starting point "
                "and watch the source's \"for your area\" count.")
        base = ctx.extras.get("geocoder_url")
        if base:
            try:
                words = geocode.area_words(await geocode.reverse(ctx.http, base, location["lat"], location["lon"]))
            except geocode.GeocodeError as e:
                words, note = [], note + f" (Place names near you couldn't be looked up: {e}.)"
            if words:
                joined = ", ".join(words)
                out.append(Candidate(label="Area words from your address", values={"area_keywords": joined},
                                     current=(current.get("area_keywords") or "").strip().lower() == joined.lower(),
                                     detail=f"{joined} (place names around your home, from the address search server)"))
        if len(out) > 1:
            note = "You are close to a state border, so a warning for the other state's side can matter too. This source reads one feed; the other state's isn't covered unless you switch. " + note
        return Suggestion(candidates=out, note=note)

    async def fetch(self) -> dict[str, Any]:
        r = await self.ctx.http.get(self.cfg.feed_url, timeout=30)
        r.raise_for_status()
        return {"xml": r.content, "received": utcnow().isoformat()}

    def normalise(self, raw: dict[str, Any]) -> list[Normalised]:
        now = datetime.fromisoformat(raw["received"])
        keywords = self.cfg.keywords
        items = parse_feed(raw["xml"])
        events: list[Normalised] = []
        for it in items:
            cat = categorise(it["title"])
            in_area = not keywords or any(k in it["title"].lower() for k in keywords)
            digest = hashlib.sha1(identity(it["title"]).encode(), usedforsecurity=False).hexdigest()[:8]      # same warning re-issued: same id, refreshed in place
            events.append(Event(source=self.meta.name, event_type="warning", external_id=f"{it['guid']}#{digest}", ts=it["ts"] or now,
                                expires=now + timedelta(minutes=self.cfg.grace_min),
                                properties={"title": it["title"], "link": it["link"], "category": cat, "in_area": in_area,
                                            "severe_thunderstorm": cat == "severe_thunderstorm"}))
        self.ctx.state.detail.update(
            items_in_feed=len(items), for_your_area=sum(1 for e in events if e.properties["in_area"]),
            severe_thunderstorm=sum(1 for e in events if e.properties["severe_thunderstorm"]),
            severe_thunderstorm_for_your_area=sum(1 for e in events if e.properties["severe_thunderstorm"] and e.properties["in_area"]),
            note=("no warnings in force" if not items else "none of the warnings mention your area words (check the words if you expected one)" if keywords and not any(e.properties["in_area"] for e in events) else None))
        return events

    async def health(self) -> Health:
        return self.ctx.state
