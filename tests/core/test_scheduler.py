import asyncio

from conftest import FakeSource

from stormtracker.plugins import HealthState, RateLimit
from stormtracker.scheduler import RateLimiter, backoff_delay


def test_backoff_grows_and_caps():
    mid = lambda: 0.5  # no jitter offset  # noqa: E731
    assert backoff_delay(1, rng=mid) == 5
    assert backoff_delay(2, rng=mid) == 10
    assert backoff_delay(3, rng=mid) == 20
    assert backoff_delay(50, rng=mid) == 900
    assert 3.75 <= backoff_delay(1, rng=lambda: 0) and backoff_delay(1, rng=lambda: 1) <= 6.25


def test_rate_limiter_window():
    t = [0.0]
    rl = RateLimiter(RateLimit(max_requests=2, period_s=10), clock=lambda: t[0])
    rl._hits.extend([0.0, 1.0])
    t[0] = 5.0
    assert rl.wait_time() == 5.0          # oldest hit expires at t=10
    t[0] = 10.0
    assert rl.wait_time() == 0.0


async def test_failure_isolated_and_recorded(admin, app):
    sched = app.state.core.scheduler
    r = await admin.put("/api/sources/boom", json={"enabled": True})
    assert r.status_code == 200
    r = await admin.put("/api/sources/fake", json={"enabled": True, "secrets": {"api_key": "k1"}})
    assert r.status_code == 200
    boom, fake = sched.runners["boom"], sched.runners["fake"]
    assert await sched.run_once(boom) is False
    assert boom.health.state == HealthState.DEGRADED and "always" in boom.health.last_error
    for _ in range(2):
        await sched.run_once(boom)
    assert boom.health.state == HealthState.DOWN
    # the other source is unaffected and persists data
    assert await sched.run_once(fake) is True
    assert fake.health.state == HealthState.OK
    obs = (await admin.get("/api/observations")).json()
    assert obs[0]["values"] == {"temp_c": 21.5} and obs[0]["ts"].endswith("+00:00")
    # re-poll is idempotent (duplicate rows ignored)
    await sched.run_once(fake)
    assert len((await admin.get("/api/observations")).json()) == 1
    assert len((await admin.get("/api/events")).json()) == 1


async def test_recovery_resets_failures(admin, app):
    sched = app.state.core.scheduler
    await admin.put("/api/sources/fake", json={"enabled": True, "secrets": {"api_key": "k"}})
    fake = sched.runners["fake"]
    FakeSource.fail = True
    await sched.run_once(fake)
    assert fake.health.consecutive_failures == 1
    FakeSource.fail = False
    assert await sched.run_once(fake)
    assert fake.health.consecutive_failures == 0 and fake.health.last_error is None


async def test_loop_runs_and_cancels(admin, app):
    sched = app.state.core.scheduler
    sched.startup_jitter = 0
    await admin.put("/api/sources/fake", json={"enabled": True, "secrets": {"api_key": "k"}})
    await asyncio.sleep(0.2)
    assert FakeSource.calls >= 1           # loop fired after startup jitter-free first run
    await admin.put("/api/sources/fake", json={"enabled": False})
    assert sched.runners["fake"].task is None
