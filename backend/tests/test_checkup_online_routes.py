"""Exercise the production session and Baidu parser; only the HTTP boundary is mocked."""
import asyncio
import time
from contextlib import asynccontextmanager

import httpx
import pytest

from app.checkups.routes import OnlineRouteTransport
from app.checkups.routes import RouteSession
from app.checkups.verification_stage import verify_facilities
from app.quota import TaskBudget

ORIGIN, DESTINATION = (121.513925, 31.313079), (121.514925, 31.313079)


class Pool:
    @asynccontextmanager
    async def attempt(self, deadline, *, budget, pool):
        budget.consume(pool)
        yield self

    def outcome(self, reason):
        self.reason = reason


@pytest.mark.parametrize('facility_id,uid', [('baidu_place:real-uid', 'real-uid'), ('osm:123', None)])
def test_production_session_sends_http_and_preserves_route_evidence(facility_id, uid):
    sent = []

    def handler(request):
        sent.append(request)
        assert request.url.params.get('destination_uid') == uid
        assert 0 < request.extensions['timeout']['read'] <= 8
        return httpx.Response(200, json={'status': 0, 'result': {'routes': [{
            'duration': 300, 'distance': 420,
            'steps': [{'start_location': dict(zip(('lng', 'lat'), ORIGIN)),
                       'end_location': dict(zip(('lng', 'lat'), DESTINATION))}],
        }]}})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            transport = OnlineRouteTransport(client, 'offline-key')
            session = transport.session(Pool(), budget=TaskBudget(isochrone=400),
                                        deadline=time.monotonic() + 20)
            outcome = await verify_facilities(
                facilities=[{'id': facility_id, 'category': 'pharmacy',
                             'location': dict(zip(('lng', 'lat'), DESTINATION))}],
                majors=['medical'], zones=[], heatmap={}, entrances={}, session=session, origin=ORIGIN)
            record = outcome.evidence.facilities[0]
            assert record['routeDistanceM'] == 420
            assert record['durationS'] == 300 and record['withinRule'] is True
            assert record['poiStatus'] == 'verified_reachable'
            assert record['endpointVerified'] is True
            assert session.attempts == outcome.network_requests == len(sent) == 1

    asyncio.run(run())


def test_transport_timeout_does_not_verify_missing_endpoints():
    def handler(request):
        raise httpx.ReadTimeout('offline timeout', request=request)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await OnlineRouteTransport(client, 'offline-key').route(
                'baidu_place:uid', ORIGIN, DESTINATION, 8)
            assert result.reason == 'timeout'
            assert result.endpoint_verified is False
            assert result.route_origin is None and result.route_destination is None

    asyncio.run(run())


def test_expired_transport_never_sends():
    sent = []

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: sent.append(r))) as client:
            result = await OnlineRouteTransport(client, 'offline-key').route(
                'baidu_place:uid', ORIGIN, DESTINATION, -1)
            assert result.reason == 'deadline' and result.endpoint_verified is False

    asyncio.run(run())
    assert not sent


def test_session_recomputes_timeout_after_waiting_for_slot(monkeypatch):
    from life_circle.models import RouteObservation
    now, timeouts = [100.0], []
    monkeypatch.setattr('app.checkups.routes.time.monotonic', lambda: now[0])

    class DelayedPool(Pool):
        @asynccontextmanager
        async def attempt(self, deadline, **kwargs):
            now[0] = 109.0
            yield self

    class Transport:
        async def route(self, facility_id, origin, destination, timeout):
            timeouts.append(timeout)
            return RouteObservation(destination, reason='no_result', endpoint_verified=False)

    async def run():
        session = RouteSession(Transport(), DelayedPool(), budget=None, deadline=110.0)
        await session('baidu_place:uid', ORIGIN, DESTINATION)
    asyncio.run(run())
    assert timeouts == [1.0]
