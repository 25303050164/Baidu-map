import asyncio
import math

import httpx
import pytest

from life_circle.providers import BaiduProvider
from life_circle.models import CancelToken, IsochroneRequest
from life_circle.scheduler import Scheduler

ORIGIN = (116.4, 39.9)
DEST = (116.401, 39.901)


def response(routes):
    return {"status": 0, "result": {"routes": routes}}


def route(duration, start=ORIGIN, end=DEST):
    return {"duration": duration, "steps": [{"start_location": {"lng": start[0], "lat": start[1]}, "end_location": {"lng": end[0], "lat": end[1]}}]}


def query(payload, status=200):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(status, json=payload))) as client:
            return await BaiduProvider("fixture-only", client=client).query_walking_time(ORIGIN, DEST, math.inf)
    return asyncio.run(run())


def test_it01_request_order_units_and_minimum():
    async def run():
        def handle(request):
            assert request.url.path == "/directionlite/v1/walking"
            assert request.url.params["origin"] == "39.900000,116.400000"
            assert request.url.params["destination"] == "39.901000,116.401000"
            assert request.url.params["coord_type"] == request.url.params["ret_coordtype"] == "bd09ll"
            assert request.url.params["steps_info"] == "1"
            return httpx.Response(200, json=response([route(1000), route(899)]))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            observation = await BaiduProvider("fixture-only", client=client).query_walking_time(ORIGIN, DEST, math.inf)
            assert observation.duration == 899 and observation.endpoint_verified
    asyncio.run(run())


@pytest.mark.parametrize("payload,reason", [
    ({"status": 1}, "temporary"), ({"status": 2}, "invalid_parameter"),
    ({"status": 7}, "no_result"), ({"status": 240}, "permission"),
    ({"status": 302}, "quota"), ({"status": 401}, "rate_limit"),
    ({"status": 0}, "invalid_response"), ([], "invalid_response"),
    (response([]), "no_result"), (response([route(-1)]), "invalid_duration"),
])
def test_it01_response_errors(payload, reason):
    observation = query(payload)
    assert observation.duration is None and observation.reason == reason


@pytest.mark.parametrize("status,reason", [(429, "rate_limit"), (500, "temporary"), (403, "permission"), (400, "invalid_parameter")])
def test_it01_http_errors(status, reason):
    assert query({}, status).reason == reason


def test_ut16_snapped_endpoint_and_echo_are_distinguished():
    assert query(response([route(600, end=(116.411, 39.901))])).reason == "endpoint_offset"
    observation = query({"status": 0, "result": {"origin": {"lng": ORIGIN[0], "lat": ORIGIN[1]}, "routes": [{"duration": 600}]}})
    assert observation.duration == 600 and not observation.endpoint_verified
    # An invalid shortest route must not suppress a slower valid route.
    assert query(response([route(500, end=(116.411, 39.901)), route(800)])).duration == 800


def test_real_qps_required_and_retry_uses_budget():
    async def run():
        count = 0
        def handle(request):
            nonlocal count
            count += 1
            return httpx.Response(200, json={"status": 1} if count == 1 else response([route(700)]))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            provider = BaiduProvider("fixture-only", client=client)
            with pytest.raises(ValueError, match="QPS"):
                Scheduler(IsochroneRequest(ORIGIN, "bd09ll"), provider, CancelToken())
            s = Scheduler(IsochroneRequest(ORIGIN, "bd09ll", qps=10000), provider, CancelToken())
            assert (await s.query(DEST)).duration == 700
            assert s.stats.requests == 2 and s.stats.retries == 1
    asyncio.run(run())
