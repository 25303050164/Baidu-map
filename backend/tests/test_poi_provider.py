"""Live adapter exercised exclusively with HTTPX MockTransport and synthetic keys."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import time

import httpx
import pytest

from app.analyses import RateGate
from app.config import Settings
from app.poi.models import PoiCollectRequest, RuntimeConfig
from app.poi.planner import build_plan, parameters
from app.poi.provider import BaiduPoiProvider
from app.poi.runtime import PoiLedger, PoiRuntime
from app.poi.service import collect_pois
from app.request_control import RequestStopped


def live_context(tmp_path, **overrides):
    request = PoiCollectRequest(coordinateSystem='bd09ll')
    config = RuntimeConfig(runId='mock-live', authorized=True, qps=2,
        windowStart=datetime.now(timezone.utc)-timedelta(minutes=1),
        windowEnd=datetime.now(timezone.utc)+timedelta(minutes=10), **overrides)
    plan = build_plan(request, config)
    config.approved_config_hash = plan['configHash']
    return request, config, plan, PoiLedger(tmp_path / 'ledger', config, plan['configHash'])


def empty():
    return {'status': 0, 'result_type': 'poi_type', 'results': [], 'total': 0}


def test_v3_parameters_cache_and_secret_whitelist(tmp_path):
    async def run():
        request, config, plan, ledger = live_context(tmp_path)
        sends = []
        def respond(req):
            sends.append(req)
            assert req.url.path == '/place/v3/around'
            assert req.url.params['coord_type'] == '3'
            assert req.url.params['radius_limit'] == 'true'
            assert req.url.params['page_size'] == '20'
            assert 'ret_coordtype' not in req.url.params
            assert req.url.params['location'] == parameters(plan['sequences'][0], 0)['location']
            return httpx.Response(200, json={**empty(), 'total': 1, 'results': [
                {'uid': 'synthetic-1', 'name': '合成药店 mock-secret',
                 'location': {'lng': 121.514, 'lat': 31.313},
                 'telephone': 'do-not-export', 'detail_info': {'classified_poi_tag': '医疗;药店',
                 'detail_url': 'https://example.invalid/?ak=mock-secret'}}]})
        with ledger:
            runtime = PoiRuntime(config, plan, live=True, ledger=ledger, gate=RateGate(None))
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                provider = BaiduPoiProvider(client, Settings(baidu_map_ak='mock-secret'))
                first, reason = await runtime.fetch(provider, plan['sequences'][0], 0)
                assert reason is None
                second, _ = await runtime.fetch(provider, plan['sequences'][0], 0)
                assert first == second and len(sends) == 1 and runtime.cache_hits == 1
                assert 'mock-secret' not in json.dumps(first)
                assert 'do-not-export' not in json.dumps(first)
                await runtime.fetch(provider, plan['sequences'][0], 1)
                assert len(sends) == 2
            assert runtime.metrics()['confirmedSent'] == 2
            assert 'mock-secret' not in ledger.file.read_text(encoding='utf-8')
    asyncio.run(run())


@pytest.mark.parametrize('status,reason', [(2, 'parameter_error'), (210, 'permission'),
                                         (302, 'quota'), (401, 'rate_limit')])
def test_business_stop_errors_prevent_all_later_dispatch(tmp_path, status, reason):
    async def run():
        request, config, plan, ledger = live_context(tmp_path)
        sends = []
        def respond(req):
            sends.append(req)
            return httpx.Response(200, json={'status': status})
        with ledger:
            runtime = PoiRuntime(config, plan, live=True, ledger=ledger, gate=RateGate(None))
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                result = await collect_pois(request, BaiduPoiProvider(client, Settings(baidu_map_ak='mock-secret')), runtime)
            assert len(sends) == 1 and result.query_status == 'failed' and result.stop_reason == reason
            assert runtime.metrics()['retries'] == 0
    asyncio.run(run())


@pytest.mark.parametrize('budget,expected_sends', [(1, 1), (2, 2)])
def test_timeout_retry_consumes_the_last_budget_and_hides_transport_exception(tmp_path, budget, expected_sends):
    async def run():
        request, config, plan, ledger = live_context(tmp_path, totalBudget=budget)
        sends = []
        def respond(req):
            sends.append(req)
            raise httpx.ReadTimeout('https://example.invalid/?ak=mock-secret', request=req)
        with ledger:
            runtime = PoiRuntime(config, plan, live=True, ledger=ledger, gate=RateGate(None))
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                result = await collect_pois(request, BaiduPoiProvider(client, Settings(baidu_map_ak='mock-secret')), runtime)
            assert len(sends) == expected_sends
            assert result.statistics['conservativeReservations'] == expected_sends
            assert 'mock-secret' not in result.model_dump_json() + ledger.file.read_text(encoding='utf-8')
    asyncio.run(run())


@pytest.mark.parametrize('stop', ['cancelled', 'deadline'])
def test_late_response_does_not_replace_stop_or_schedule_more_pages(tmp_path, stop):
    async def run():
        request, config, plan, ledger = live_context(tmp_path)
        sends = []
        def respond(req):
            sends.append(req)
            if stop == 'cancelled':
                runtime.token.cancel()
            else:
                runtime.deadline = time.monotonic()-1
            return httpx.Response(200, json=empty())
        with ledger:
            runtime = PoiRuntime(config, plan, live=True, ledger=ledger, gate=RateGate(None))
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                result = await collect_pois(request, BaiduPoiProvider(client, Settings(baidu_map_ak='mock-secret')), runtime)
            assert len(sends) == 1 and result.stop_reason == stop
            assert result.query_status == ('cancelled' if stop == 'cancelled' else 'failed')
            assert not any(q['successfulPages'] for q in result.query_coverage)
    asyncio.run(run())


def test_failed_reservation_write_stops_transport_and_restart(tmp_path, monkeypatch):
    async def run():
        request, config, plan, ledger = live_context(tmp_path)
        with ledger:
            runtime = PoiRuntime(config, plan, live=True, ledger=ledger, gate=RateGate(None))
            def fail_save():
                raise RequestStopped('audit_failure')
            monkeypatch.setattr(ledger, 'save', fail_save)
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: pytest.fail('unexpected send'))) as client:
                result = await collect_pois(request, BaiduPoiProvider(client, Settings(baidu_map_ak='mock-secret')), runtime)
            assert result.stop_reason == 'audit_failure' and result.query_status == 'failed'
        with pytest.raises(RequestStopped, match='run_already_started'):
            with PoiLedger(ledger.root, config, plan['configHash']):
                pass
    asyncio.run(run())
