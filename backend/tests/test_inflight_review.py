"""The in-flight review's audit: it measures overlap and halts rather than paces."""
import asyncio

import httpx
import pytest

from tools.inflight_review import AuditHalt, AuditTransport, metrics


def test_metrics_count_overlap_rolling_seconds_and_rate_limits():
    events = [dict(dispatch=0.0, response=0.5, http_status=200, baidu_status=0),
              dict(dispatch=0.1, response=0.6, http_status=200, baidu_status=0),
              dict(dispatch=0.7, response=0.8, http_status=200, baidu_status=401)]
    measured = metrics(events, 2, 10)
    assert measured['max_inflight'] == 2
    assert measured['max_in_one_second'] == 3
    assert measured['rate_limited'] == 1


def test_the_audit_halts_before_one_attempt_too_many_and_then_stops_everything():
    async def run():
        release = asyncio.Event()

        class Slow(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                await release.wait()
                return httpx.Response(200, json={'status': 0})
        events = []
        audit = AuditTransport(Slow(), inflight=2, qps=100, events=events)
        request = httpx.Request('GET', 'https://example.invalid/')
        first = asyncio.create_task(audit.handle_async_request(request))
        second = asyncio.create_task(audit.handle_async_request(request))
        await asyncio.sleep(0)
        with pytest.raises(AuditHalt):
            await audit.handle_async_request(request)
        release.set()
        await asyncio.gather(first, second)
        with pytest.raises(AuditHalt):  # halted for good, not only while two are open
            await audit.handle_async_request(request)
        assert len(events) == 2 and audit.halted == 'inflight_exceeded'
        assert all(e['baidu_status'] == 0 for e in events)
    asyncio.run(run())
