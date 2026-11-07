"""Turns "what is true right now" into "what to send": dedup, cooldowns, reminders and all-clears.

One *episode* is a condition (a rule + a key such as a particular storm cell) from the moment it first holds
until it has been gone for ``clear_after_min``. An episode alerts once. It can remind (``repeat_after_min``)
and say it is over (``notify_clear``). A rule's ``cooldown_min`` spaces out alerts across different keys, so two
cells arriving together do not send two messages at once; the second goes out when the cooldown ends if it still holds.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .rules import Condition, Rule


@dataclass
class Episode:
    sent_at: datetime
    last_seen: datetime
    title: str
    ended_title: str | None


@dataclass
class Fire:
    rule: Rule
    kind: str                        # "alert" | "clear"
    condition_key: str
    title: str
    message: str
    data: dict


class AlertEngine:
    def __init__(self, clear_after_min: int = 10) -> None:
        self.clear_after = timedelta(minutes=clear_after_min)
        self.episodes: dict[tuple[str, str], Episode] = {}
        self.last_fire: dict[str, datetime] = {}                 # rule id -> when it last alerted

    def restore(self, rule_id: str, key: str, sent_at: datetime, now: datetime, title: str = "") -> None:
        """Re-create an episode from the alert history after a restart, so an ongoing condition does not alert again."""
        self.episodes[(rule_id, key)] = Episode(sent_at, now, title, None)
        if rule_id not in self.last_fire or sent_at > self.last_fire[rule_id]:
            self.last_fire[rule_id] = sent_at

    def step(self, now: datetime, rule: Rule, conditions: list[Condition]) -> list[Fire]:
        fires: list[Fire] = []
        seen = {c.key: c for c in conditions}
        cooldown = timedelta(minutes=rule.cooldown_min)
        for key, c in seen.items():
            ep = self.episodes.get((rule.id, key))
            if ep is None:
                last = self.last_fire.get(rule.id)
                if last is not None and now - last < cooldown:
                    continue                                       # wait: it fires once the cooldown ends, if it still holds
                self.episodes[(rule.id, key)] = Episode(now, now, c.title, c.ended_title)
                self.last_fire[rule.id] = now
                fires.append(Fire(rule, "alert", key, c.title, c.message, c.data))
                continue
            ep.last_seen, ep.title, ep.ended_title = now, c.title, c.ended_title
            if rule.repeat_after_min and now - ep.sent_at >= timedelta(minutes=rule.repeat_after_min):
                ep.sent_at = now
                self.last_fire[rule.id] = now
                fires.append(Fire(rule, "alert", key, c.title, "Still going. " + c.message, c.data))
        for (rid, key), ep in list(self.episodes.items()):
            if rid != rule.id or key in seen or now - ep.last_seen < self.clear_after:
                continue
            del self.episodes[(rid, key)]
            if rule.notify_clear:
                fires.append(Fire(rule, "clear", key, ep.ended_title or f"{ep.title}: over", f"{ep.title} no longer applies.", {}))
        return fires

    def forget(self, rule_id: str) -> None:
        """A rule was deleted or switched off: drop its episodes so turning it back on starts fresh."""
        for k in [k for k in self.episodes if k[0] == rule_id]:
            del self.episodes[k]
        self.last_fire.pop(rule_id, None)
