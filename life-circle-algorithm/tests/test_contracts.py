import asyncio
import math

import pytest

from life_circle.models import CancelToken, IsochroneRequest, RouteObservation
from life_circle.coordinates import LocalProjection, normalize
from life_circle.scheduler import Scheduler

ORIGIN = (116.4, 39.9)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def time(self):
        return self.now

    async def sleep(self, seconds):
        self.now += seconds
        await asyncio.sleep(0)


class StubProvider:
    network = False
    identity = ("stub", "v1")

    def __init__(self, duration=900, reason=None):
        self.duration = duration
        self.reason = reason
        self.calls = []

    async def query_walking_time(self, origin, destination, deadline):
        self.calls.append(destination)
        return RouteObservation(destination, self.duration, self.reason)


def test_ut01_coordinates():
    p = LocalProjection(ORIGIN)
    assert p.to_local(ORIGIN) == (0, 0)
    assert p.to_local((116.401, 39.9))[0] > 0
    assert p.to_local((116.4, 39.901))[1] > 0
    assert p.to_local(p.to_geographic((123.4, -567.8))) == pytest.approx((123.4, -567.8), abs=1e-8)
    assert normalize((116.40000001, 39.90000001)) == ORIGIN
    with pytest.raises(ValueError):
        IsochroneRequest(ORIGIN, "wgs84")
    with pytest.raises(ValueError):
        IsochroneRequest((math.nan, 39), "bd09ll")


@pytest.mark.parametrize("duration,reachable", [(899, True), (900, True), (900.1, False)])
def test_ut02_exact_threshold(duration, reachable):
    assert RouteObservation(ORIGIN, duration).reachable is reachable


@pytest.mark.parametrize("duration", [None, -1, math.nan, math.inf, "900", True])
def test_ut03_invalid_time_is_unknown(duration):
    observation = RouteObservation(ORIGIN, duration)
    assert observation.duration is None
    assert observation.reachable is None


def test_ut13_concurrent_budget_retry_and_cache():
    async def run():
        request = IsochroneRequest(ORIGIN, "bd09ll", budget=3)
        provider = StubProvider(None, "temporary")
        scheduler = Scheduler(request, provider, CancelToken(), FakeClock())
        points = [(116.401 + i / 1000, 39.9) for i in range(6)]
        results = await scheduler.observe_many(points)
        assert len(provider.calls) == scheduler.stats.requests == 3
        assert all(o.duration is None for o in results)
        before = len(provider.calls)
        await asyncio.gather(scheduler.query(points[0]), scheduler.query(points[0]))
        assert len(provider.calls) == before
        assert scheduler.stats.cache_hits >= 2
    asyncio.run(run())


def test_qps_and_failures_are_counted():
    async def run():
        clock = FakeClock()
        request = IsochroneRequest(ORIGIN, "bd09ll", qps=2)
        scheduler = Scheduler(request, StubProvider(), CancelToken(), clock)
        await scheduler.observe_many([(116.401 + i / 1000, 39.9) for i in range(4)])
        assert clock.now >= 1.5
        assert scheduler.stats.requests == 4
    asyncio.run(run())


def test_ut14_cancel_and_late_response():
    async def run():
        entered = asyncio.Event()
        token = CancelToken()

        class Slow(StubProvider):
            async def query_walking_time(self, origin, destination, deadline):
                entered.set()
                await asyncio.sleep(10)
                return RouteObservation(destination, 1)

        scheduler = Scheduler(IsochroneRequest(ORIGIN, "bd09ll"), Slow(), token)
        task = asyncio.create_task(scheduler.query((116.401, 39.9)))
        await entered.wait()
        token.cancel()
        observation = await asyncio.wait_for(task, 1)
        assert observation.duration is None
        assert scheduler.stop_reason == "cancelled"
        scheduler.close()
        count = scheduler.stats.requests
        assert (await scheduler.query((116.402, 39.9))).duration is None
        assert scheduler.stats.requests == count
    asyncio.run(run())


def test_temporary_failure_breaker_and_fatal_error():
    async def run():
        for reason, expected in [("temporary", "upstream_failure"), ("quota", "quota"), ("permission", "permission")]:
            provider = StubProvider(None, reason)
            s = Scheduler(IsochroneRequest(ORIGIN, "bd09ll"), provider, CancelToken(), FakeClock())
            await s.observe_many([(116.401 + i / 1000, 39.9) for i in range(20)])
            assert s.stop_reason == expected
            assert s.stats.requests <= (12 if reason == "temporary" else 2)
    asyncio.run(run())


def test_ut14_timeout_is_retried_then_unknown():
    async def run():
        class Timeout(StubProvider):
            async def query_walking_time(self, origin, destination, deadline):
                await asyncio.sleep(1)
                return RouteObservation(destination, 1)
        s = Scheduler(IsochroneRequest(ORIGIN, "bd09ll", timeout=.001), Timeout(), CancelToken())
        observation = await s.query((116.401, 39.9))
        assert observation.reason == "timeout" and observation.attempts == 2
        assert s.stats.requests == 2 and s.stats.retries == 1
    asyncio.run(run())


def test_ut14_deadline_blocks_next_query():
    async def run():
        clock = FakeClock()
        s = Scheduler(IsochroneRequest(ORIGIN, "bd09ll", deadline_seconds=1), StubProvider(), CancelToken(), clock)
        clock.now = 2
        assert (await s.query((116.401, 39.9))).reason == "deadline"
        assert s.stats.requests == 0
    asyncio.run(run())


def test_ut17_ordered_retry_allocation_at_budget_boundary():
    async def run(reverse):
        class Transient(StubProvider):
            async def query_walking_time(self, origin, destination, deadline):
                self.calls.append(destination)
                if (destination[0] > 116.402) == reverse:
                    await asyncio.sleep(0)
                return RouteObservation(destination, None, "temporary") if self.calls.count(destination) == 1 else RouteObservation(destination, 700)
        s = Scheduler(IsochroneRequest(ORIGIN, "bd09ll", budget=3), Transient(), CancelToken())
        observations = await s.observe_many([(116.401, 39.9), (116.403, 39.9)])
        return [(o.destination, o.duration, o.reason, o.attempts) for o in observations]
    assert asyncio.run(run(True)) == asyncio.run(run(False))


def test_ut14_late_provider_result_cannot_update_closed_task():
    async def run():
        release = asyncio.Event()
        class Late(StubProvider):
            async def query_walking_time(self, origin, destination, deadline):
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    await release.wait()
                    return RouteObservation(destination, 1)
        token = CancelToken()
        s = Scheduler(IsochroneRequest(ORIGIN, "bd09ll", timeout=.001, max_attempts=1), Late(), token)
        task = asyncio.create_task(s.query((116.401, 39.9)))
        try:
            observation = await asyncio.wait_for(asyncio.shield(task), .1)
            s.close()
            before = dict(s.cache)
            release.set()
            await asyncio.sleep(0)
            assert observation.duration is None and s.cache == before
        finally:
            release.set()
            await task
    asyncio.run(run())


def test_observation_records_actual_request_origin():
    async def run():
        s = Scheduler(IsochroneRequest((116.40000001, 39.9), "bd09ll"), StubProvider(), CancelToken())
        observation = await s.query((116.40100001, 39.9))
        assert observation.request_origin == ORIGIN
        assert observation.destination == (116.401, 39.9)
    asyncio.run(run())


def test_exploration_phase_cap_includes_retries():
    async def run():
        s = Scheduler(IsochroneRequest(ORIGIN, "bd09ll"), StubProvider(None, "temporary"), CancelToken())
        result = await s.observe_many([(116.401 + i / 1000, 39.9) for i in range(8)], request_limit=9)
        assert s.stats.requests == 9
        assert s.stop_reason is None
        assert all(o.duration is None for o in result)
        await s.query((116.42, 39.9))
        assert s.stats.requests == 11
    asyncio.run(run())
