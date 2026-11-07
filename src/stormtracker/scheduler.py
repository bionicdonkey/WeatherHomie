"""Runs each enabled source on its own interval.

Isolation guarantees: every source runs in its own asyncio task; all exceptions are caught,
recorded in that source's Health, and retried with exponential backoff + jitter. A failing
source never affects another.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import httpx

from .models import Normalised
from .plugins import Health, HealthState, PluginRegistry, RateLimit, Source, SourceContext, secret_fields
from .settings.sources import SourceSettings
from .storage.db import Repository
from .timeutil import utcnow

log = logging.getLogger(__name__)


def redact(source: Source | None, text: str) -> str:
    """Blank out the source's own secret values (e.g. API keys in a failed request URL) before the
    text is stored in Health or logged."""
    if source is None:
        return text
    for name in secret_fields(type(source.cfg)):
        v = getattr(source.cfg, name, None)
        if v:
            text = text.replace(str(v), "***")
            for line in str(v).splitlines():            # a list of URLs: an error usually mentions one entry, not the whole list
                if len(line.strip()) >= 6:
                    text = text.replace(line.strip(), "***")
    return text


class RateLimiter:
    """Sliding window: at most ``max_requests`` per ``period_s``."""

    def __init__(self, rl: RateLimit, clock=time.monotonic):
        self.rl, self._clock = rl, clock
        self._hits: deque[float] = deque()

    def wait_time(self) -> float:
        now = self._clock()
        while self._hits and now - self._hits[0] >= self.rl.period_s:
            self._hits.popleft()
        if len(self._hits) < self.rl.max_requests:
            return 0.0
        return self.rl.period_s - (now - self._hits[0])

    async def acquire(self) -> None:
        while (w := self.wait_time()) > 0:
            await asyncio.sleep(w)
        self._hits.append(self._clock())


def backoff_delay(failures: int, base: float = 5.0, cap: float = 900.0, rng=random.random) -> float:
    """Exponential backoff with +/-25% jitter, capped."""
    d = min(cap, base * (2 ** max(0, failures - 1)))
    return d * (0.75 + 0.5 * rng())


@dataclass
class Runner:
    name: str
    health: Health
    source: Source | None = None
    task: asyncio.Task | None = None
    http: httpx.AsyncClient | None = None
    interval_s: float = 0
    limiter: RateLimiter | None = None


class Scheduler:
    def __init__(self, registry: PluginRegistry, settings: SourceSettings, repo: Repository,
                 *, user_agent: str, locations_provider, data_dir=None, sleep=asyncio.sleep):
        self.registry, self.settings, self.repo = registry, settings, repo
        self.user_agent, self._locations, self.data_dir = user_agent, locations_provider, data_dir
        self.runners: dict[str, Runner] = {}
        self._sleep = sleep
        self.startup_jitter = 5.0   # max seconds to stagger first polls

    # ---- lifecycle ---------------------------------------------------
    async def start(self) -> None:
        for name in self.registry.all():
            await self.reload(name)

    async def stop(self) -> None:
        for name in list(self.runners):
            await self._teardown(name)

    async def reload(self, name: str) -> None:
        """(Re)start one source from its current DB settings, leaving others untouched."""
        await self._teardown(name)
        cls = self.registry.get(name)
        runner = Runner(name=name, health=Health(state=HealthState.DISABLED))
        self.runners[name] = runner
        raw = await self.settings.raw(name)
        if not raw.get("enabled"):
            return
        try:
            cfg, missing = await self.settings.build(name)
        except Exception as e:  # invalid stored config must not stop other sources
            runner.health = Health(state=HealthState.DOWN, last_error=f"invalid config: {e}")
            return
        if cfg is None:
            runner.health = Health(state=HealthState.MISSING_CREDENTIALS, detail={"missing": missing})
            return
        runner.health = Health(state=HealthState.STARTING)
        runner.http = httpx.AsyncClient(headers={"User-Agent": self.user_agent}, timeout=30, follow_redirects=True)

        async def emit(items: list[Normalised]) -> None:
            await self.repo.save(items)

        ctx = SourceContext(http=runner.http, emit=emit, health=runner.health,
                            locations=self._locations, logger=logging.getLogger(f"source.{name}"),
                            data_dir=self.data_dir)
        try:
            runner.source = cls(cfg, ctx)
            await runner.source.start()
        except Exception as e:
            runner.health.state, runner.health.last_error = HealthState.DOWN, f"start failed: {e}"
            return
        runner.limiter = RateLimiter(cls.meta.rate_limit)
        interval = raw.get("interval_s") or cls.meta.poll_interval_s
        # never poll faster than the plugin's declared minimum interval
        runner.interval_s = max(interval, cls.meta.poll_interval_s) if cls.meta.poll_interval_s else 0
        if runner.interval_s > 0 and runner.source.polling:
            runner.task = asyncio.create_task(self._loop(runner), name=f"source:{name}")
        else:
            runner.health.state = HealthState.OK  # push-only: ready

    async def _teardown(self, name: str) -> None:
        r = self.runners.pop(name, None)
        if r is None:
            return
        if r.task:
            r.task.cancel()
            await asyncio.gather(r.task, return_exceptions=True)
        if r.source:
            try:
                await r.source.stop()
            except Exception:
                log.exception("stop failed for %s", name)
        if r.http:
            await r.http.aclose()

    # ---- polling -----------------------------------------------------
    async def run_once(self, runner: Runner) -> bool:
        h = runner.health
        h.last_attempt = utcnow()
        try:
            if not (runner.source and runner.limiter):
                raise RuntimeError("source is not running")
            await runner.limiter.acquire()
            raw = await runner.source.fetch()
            items = runner.source.normalise(raw)
            await self.repo.save(items)
            await runner.source.on_saved(items)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            h.consecutive_failures += 1
            h.last_error = redact(runner.source, f"{type(e).__name__}: {e}")[:500]
            h.state = HealthState.DOWN if h.consecutive_failures >= 3 else HealthState.DEGRADED
            log.warning("source %s failed (%d): %s", runner.name, h.consecutive_failures, h.last_error)
            return False
        h.consecutive_failures, h.last_error = 0, None
        h.last_success, h.state = utcnow(), HealthState.OK
        h.detail["last_items"] = len(items)
        return True

    async def _loop(self, runner: Runner) -> None:
        # small startup jitter so sources don't all fire at the same instant
        await self._sleep(random.random() * min(self.startup_jitter, runner.interval_s))
        while True:
            ok = await self.run_once(runner)
            delay = runner.interval_s if ok else max(
                runner.interval_s, backoff_delay(runner.health.consecutive_failures))
            await self._sleep(delay)

    # ---- push + status -----------------------------------------------
    def running(self, name: str) -> Source | None:
        r = self.runners.get(name)
        return r.source if r and r.source else None

    async def status(self) -> dict[str, Any]:
        out = {}
        for name, r in self.runners.items():
            h = await r.source.health() if r.source else r.health
            out[name] = h.model_dump(mode="json")
        return out
