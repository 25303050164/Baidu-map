import asyncio
import time

import httpx
import pytest

from app.analyses import RateGate
from app.place_protocol import Pagination, response_error
from app.places import PlacesClient, classify
from life_circle.models import CancelToken


def page(ids, total=None):
    return {"status": 0, "total": total, "results": [{"uid": str(i)} for i in ids]}


def test_short_page_continues_and_repeated_page_stops():
    state = Pagination()
    assert state.consume(page([1], 5), 8) is None
    assert state.consume(page([1], 5), 8) == "pagination_anomaly"


@pytest.mark.parametrize("pages,expected", [
    ([page([1], 2), page([], 2)], "pagination_uncertain"),
    ([page([1], 2), page([2], 3), page([3], 3)], "pagination_uncertain"),
    ([page([1], 150), page([], 150)], "pagination_uncertain"),
    ([page([1]), page([])], "completed"),
])
def test_pagination_evidence(pages, expected):
    state = Pagination()
    for payload in pages:
        reason = state.consume(payload, 8)
    assert reason == expected


@pytest.mark.parametrize("http,payload,reason", [
    (200, {"status": False}, "invalid_response"),
    (200, {"status": 0, "result_type": "address_type", "results": []}, "non_poi_response"),
    (200, {"status": 302}, "quota"),
    (200, {"status": 210}, "permission"),
    (429, None, "rate_limit"),
])
def test_business_errors(http, payload, reason):
    assert response_error(http, payload) == reason


@pytest.mark.parametrize("name,tag", [("实验小学北门", "小学"), ("小学辅导班", ""), ("社区药店", "小学")])
def test_ambiguous_and_accessory_records(name, tag):
    assert classify(name, tag) is None


def test_cancel_during_permit_wait_sends_nothing():
    async def run():
        token = CancelToken()
        gate = RateGate(None)
        async def wait(deadline):
            token.cancel()
            return True
        gate.wait = wait
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: pytest.fail("unexpected send"))) as client:
            places = PlacesClient(client, "fixture", gate, token)
            payload, reason = await places.page((121, 31), "药店", 900, 0, time.monotonic()+5)
        assert payload is None and reason == "cancelled" and places.requests == 0
    asyncio.run(run())


def test_response_pacing_and_late_cancel():
    async def run():
        token, gate = CancelToken(), RateGate(None)
        completed = []
        gate.completed = completed.append
        def handle(request):
            assert gate.attempt_lock.locked()
            token.cancel()
            return httpx.Response(200, json=page([], 0))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            places = PlacesClient(client, "fixture", gate, token)
            payload, reason = await places.page((121, 31), "药店", 900, 0, time.monotonic()+5)
        assert completed == [None]
        assert payload is None and reason == "cancelled" and places.requests == 1
    asyncio.run(run())
