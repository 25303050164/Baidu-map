"""§3.4 caching: key completeness, cross-task validity, and what is never stored."""
import asyncio

import httpx
import pytest

from app.cache import KeyedCache, cache_key
from app.catalog import majors, poi_keys
from app.poi import plan as poi_plan
from app.poi.cache import CachedPages, page_key
from app.poi.online import (PROCESSING_STEP_LIMIT, OnlinePlanner, QueryDomain, RunLimits)
from app.poi.planner import RULES, sequence
from app.poi.provider import ReplayProvider
from app.quota import BudgetExhausted
from app.request_control import RequestStopped
from life_circle.coordinates import LocalProjection

ORIGIN = (121.514, 31.313)
CATEGORIES = ('pharmacy', 'primary_school')
# Four coarse blocks, every keyword of each category, two pages apiece.
SEQUENCES = 4 * sum(len(RULES['queries'][category]) for category in CATEGORIES)
PAGES = 2 * SEQUENCES


def page_key_at(page):
    return cache_key('page', coordinateSystem='bd09ll', scope=[0.0, 0.0, 100.0, 100.0],
                     parameters={'query': '药店', 'page_num': page}, provider='synthetic',
                     apiVersion='3.0')


KEY_A, KEY_B, KEY_C = (page_key_at(page) for page in (0, 1, 2))


class Clock:
    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now


async def must_not_build():
    raise AssertionError('a cache hit must not build')


def payload(rows, total=None):
    return {'status': 0, 'result_type': 'poi_type', 'total': len(rows) if total is None else total,
            'results': rows}


def two_pages(sequence_, page):
    return payload([{'uid': f"synthetic-{sequence_['sequenceId']}-{page}-{index}",
                     'name': f'合成{index}', 'location': {'lng': ORIGIN[0], 'lat': ORIGIN[1]}}
                    for index in range(20)], total=40)


class Live:
    """A metered fetch stand-in: every call recorded here is a call that was paid for."""

    def __init__(self, default=two_pages):
        self.default, self.calls = default, []

    async def __call__(self, sequence_, page):
        self.calls.append((sequence_['tileId'], sequence_['category'], sequence_['query'], page))
        outcome = self.default(sequence_, page)
        return outcome if isinstance(outcome, tuple) else (outcome, None)


class Transport:
    identity, api_version = 'baidu_place', '3.0'


def planner(budget):
    return OnlinePlanner(domain=QueryDomain.circle(1300), origin=ORIGIN,
                         categories=CATEGORIES, budget=budget, source='synthetic')


def test_a_key_is_its_kind_and_components_and_nothing_else():
    named = {'coordinateSystem': 'bd09ll', 'scope': [0.0], 'provider': 'p', 'apiVersion': '1'}
    parameters = {'query': '药店', 'page_num': 0}
    assert cache_key('page', **named, parameters={'page_num': 0, 'query': '药店'}) == \
        cache_key('page', parameters=parameters, **named)
    assert cache_key('page', **named, parameters=parameters) != \
        cache_key('page', **named, parameters={'query': '药店', 'page_num': 1})
    with pytest.raises(ValueError, match='unknown cache kind'):
        cache_key('tile', provider='p')
    with pytest.raises(ValueError, match='missing: apiVersion'):
        cache_key('page', coordinateSystem='bd09ll', scope=[0.0], parameters={}, provider='p')
    with pytest.raises(ValueError, match='carries unnamed components: extra'):
        cache_key('page', **named, parameters={}, extra='x')
    # A component that is not canonical JSON cannot take part in a stable key.
    with pytest.raises(ValueError, match='canonical JSON'):
        cache_key('page', **named, parameters={'provider': object()})


def test_an_entry_is_reused_inside_its_task_and_nowhere_else_by_default():
    clock = Clock(100.0)
    cache = KeyedCache(clock=clock)
    assert cache.freshness_seconds is None
    cache.store(KEY_A, {'page': 0}, task_id='t1')
    clock.now = 1e9
    # The task that stored it keeps it however late it asks; another task has no
    # window to read it under, so it asks again rather than reading a stale page.
    assert cache.get(KEY_A, task_id='t1').value == {'page': 0}
    assert cache.get(KEY_A, task_id='t2') is None


def test_another_task_reads_an_entry_only_inside_the_configured_window():
    clock = Clock(100.0)
    cache = KeyedCache(freshness_seconds=300, clock=clock)
    cache.store(KEY_A, {'page': 0}, task_id='t1')
    clock.now = 400.0
    assert cache.get(KEY_A, task_id='t2') is not None
    clock.now = 400.001
    assert cache.get(KEY_A, task_id='t2') is None
    with pytest.raises(ValueError, match='freshness_seconds must be positive or None'):
        KeyedCache(freshness_seconds=0)
    with pytest.raises(ValueError, match='max_entries must be at least 1'):
        KeyedCache(max_entries=0)


def test_a_hit_reports_the_instant_the_data_was_obtained():
    clock = Clock(100.0)
    cache = KeyedCache(freshness_seconds=10_000, clock=clock)
    cache.store(KEY_A, {'page': 0}, task_id='t1')
    clock.now = 999.0
    answer = asyncio.run(cache.resolve(KEY_A, must_not_build, task_id='t2'))
    assert (answer.value, answer.reason, answer.obtained_at, answer.cached) == (
        {'page': 0}, None, 100.0, True)
    # The collection time moved by nearly a thousand seconds; the data time did not.
    assert answer.obtained_at != clock.now


def test_a_failure_is_returned_to_its_caller_and_never_written_down():
    calls = []

    async def failing():
        calls.append(1)
        return None, 'timeout'

    cache = KeyedCache()
    answer = asyncio.run(cache.resolve(KEY_A, failing, task_id='t1'))
    assert (answer.value, answer.reason, answer.cached) == (None, 'timeout', False)
    assert len(cache) == 0 and cache.inflight == {}
    # §3.4: a failure is not a zero result, so the next caller asks again.
    again = asyncio.run(cache.resolve(KEY_A, failing, task_id='t1'))
    assert again.reason == 'timeout' and len(calls) == 2


def test_a_refusal_is_not_an_entry_either():
    calls = []

    async def refusing():
        calls.append(1)
        raise RequestStopped('rate_limit')

    cache = KeyedCache()
    for attempt in range(2):
        with pytest.raises(RequestStopped):
            asyncio.run(cache.resolve(KEY_A, refusing, task_id='t1'))
        assert len(cache) == 0 and cache.inflight == {}
    assert len(calls) == 2


def test_a_store_must_have_a_value():
    cache = KeyedCache()
    with pytest.raises(ValueError, match='must have a value'):
        cache.store(KEY_A, None, task_id='t1')
    assert len(cache) == 0


def test_identical_in_flight_queries_are_coalesced():
    cache = KeyedCache()
    started, release, calls = asyncio.Event(), asyncio.Event(), []

    async def build():
        calls.append(1)
        started.set()
        await release.wait()
        return {'page': 0}, None

    async def share():
        first = asyncio.ensure_future(cache.resolve(KEY_A, build, task_id='t'))
        await started.wait()
        second = asyncio.ensure_future(cache.resolve(KEY_A, build, task_id='t'))
        await asyncio.sleep(0)
        release.set()
        return await asyncio.gather(first, second)

    answers = asyncio.run(share())
    assert len(calls) == 1
    assert [answer.value for answer in answers] == [{'page': 0}, {'page': 0}]
    # Only one of the two paid for it, and it says so.
    assert [answer.cached for answer in answers] == [False, True]


def test_a_coalesced_failure_is_reported_to_every_waiter_and_stored_for_none():
    cache = KeyedCache()
    release, calls = asyncio.Event(), []

    async def failing():
        calls.append(1)
        await release.wait()
        return None, 'upstream_error'

    async def share():
        first = asyncio.ensure_future(cache.resolve(KEY_A, failing, task_id='t'))
        await asyncio.sleep(0)
        second = asyncio.ensure_future(cache.resolve(KEY_A, failing, task_id='t'))
        await asyncio.sleep(0)
        release.set()
        return await asyncio.gather(first, second)

    answers = asyncio.run(share())
    assert [answer.reason for answer in answers] == ['upstream_error', 'upstream_error']
    assert len(calls) == 1 and len(cache) == 0


def test_the_cache_is_bounded_and_drops_the_least_recently_written_first():
    cache = KeyedCache(max_entries=2)
    cache.store(KEY_A, 'a', task_id='t')
    cache.store(KEY_B, 'b', task_id='t')
    cache.store(KEY_A, 'a2', task_id='t')
    cache.store(KEY_C, 'c', task_id='t')
    assert cache.get(KEY_A, task_id='t').value == 'a2'
    assert cache.get(KEY_C, task_id='t').value == 'c'
    assert cache.get(KEY_B, task_id='t') is None
    assert len(cache) == 2 and cache.stores == 4


def test_a_page_key_covers_the_block_the_page_the_provider_and_the_version():
    projection = LocalProjection(ORIGIN)
    block = sequence('r0c0', -1300, -1300, 1300, 'pharmacy', RULES['queries']['pharmacy'][0],
                     projection)
    neighbour = sequence('r0c1', 0, -1300, 1300, 'pharmacy', RULES['queries']['pharmacy'][0],
                         projection)
    keys = {
        'same': page_key(block, 0, provider='baidu_place', api_version='3.0'),
        'again': page_key(dict(block), 0, provider='baidu_place', api_version='3.0'),
        'page': page_key(block, 1, provider='baidu_place', api_version='3.0'),
        'block': page_key(neighbour, 0, provider='baidu_place', api_version='3.0'),
        'provider': page_key(block, 0, provider='synthetic:other', api_version='3.0'),
        'version': page_key(block, 0, provider='baidu_place', api_version='2.0'),
        'coordinate_system': page_key(block, 0, provider='baidu_place', api_version='3.0',
                                      coordinate_system='gcj02'),
    }
    assert keys['same'] == keys['again']
    assert len(set(keys.values())) == len(keys) - 1


def test_the_transport_supplies_the_provider_and_version_in_the_key():
    first = ReplayProvider({'source': 'synthetic', 'pages': {}})
    second = ReplayProvider({'source': 'synthetic', 'pages': {'药店:0': payload([])}})
    # A different fixture is a different provider identity, so one run of the
    # replay harness can never read another's page.
    assert first.identity != second.identity
    projection = LocalProjection(ORIGIN)
    block = sequence('r0c0', -1300, -1300, 1300, 'pharmacy', RULES['queries']['pharmacy'][0],
                     projection)
    assert CachedPages(KeyedCache(), Live(), provider=first, task_id='t').key(block, 0) != \
        CachedPages(KeyedCache(), Live(), provider=second, task_id='t').key(block, 0)


def test_a_warm_cache_reproduces_a_run_without_a_single_call():
    cache, live = KeyedCache(freshness_seconds=3600), Live()
    first = asyncio.run(planner(60).run(CachedPages(cache, live, provider=Transport(),
                                                    task_id='t1')))
    assert first.status == 'completed' and len(live.calls) == PAGES and len(cache) == PAGES

    async def refuse(sequence_, page):
        raise AssertionError('a warm cache must not reach the transport')

    adapter = CachedPages(cache, refuse, provider=Transport(), task_id='t2')
    second = asyncio.run(planner(60).run(adapter))
    assert second.coverage == first.coverage
    assert second.status == first.status == 'completed'
    assert len(adapter.uses) == PAGES
    assert {use['source'] for use in adapter.uses.values()} == {'cache'}


def test_pages_from_a_partial_run_are_reused_but_never_complete_the_set():
    cache, live = KeyedCache(freshness_seconds=3600), Live()
    adapter = CachedPages(cache, live, provider=Transport(), task_id='t1')
    first = asyncio.run(planner(2).run(adapter))
    assert first.status == 'partial' and first.stop_reason == 'budget_exhausted'
    assert len(live.calls) == 2 and len(cache) == 2 and len(first.observations) == 40
    assert {use['source'] for use in adapter.uses.values()} == {'live'}

    async def refuse(sequence_, page):
        raise AssertionError('the two paid pages must be served from the cache')

    second = CachedPages(cache, refuse, provider=Transport(), task_id='t2')
    replay = asyncio.run(planner(2).run(second))
    # The successful pages were reusable, and replaying them produced the very
    # same coverage: a partial set is not completed by having cached its pages.
    assert replay.coverage == first.coverage
    assert replay.status == 'partial' and replay.stop_reason == 'budget_exhausted'
    assert len(replay.observations) == 40
    assert {use['source'] for use in second.uses.values()} == {'cache'}
    assert replay.catalog_completeness == 'unverified'


def test_another_task_does_not_read_a_page_without_a_configured_window():
    cache, live = KeyedCache(), Live()
    adapter = CachedPages(cache, live, provider=Transport(), task_id='t1')
    asyncio.run(planner(1).run(adapter))
    assert len(cache) == 1 and len(live.calls) == 1

    second = CachedPages(cache, live, provider=Transport(), task_id='t2')
    asyncio.run(planner(1).run(second))
    assert len(live.calls) == 2
    assert {use['source'] for use in second.uses.values()} == {'live'}
    assert len(cache) == 1


def test_a_failed_page_is_asked_again_by_the_next_task():
    cache = KeyedCache()

    async def timeout(sequence_, page):
        return None, 'timeout'

    failed = asyncio.run(planner(1).run(CachedPages(cache, timeout, provider=Transport(),
                                                    task_id='t1')))
    assert failed.status == 'failed' and len(cache) == 0
    live = Live()
    retried = asyncio.run(planner(1).run(CachedPages(cache, live, provider=Transport(),
                                                     task_id='t2')))
    assert len(live.calls) == 1 and len(cache) == 1
    assert retried.status == 'partial' and len(retried.observations) == 20


def test_a_replayed_page_keeps_its_original_data_time_in_the_report():
    clock = Clock(1000.0)
    cache, live = KeyedCache(freshness_seconds=10_000, clock=clock), Live()
    first = CachedPages(cache, live, provider=Transport(), task_id='t1')
    asyncio.run(planner(1).run(first))
    assert [use['obtainedAt'] for use in first.uses.values()] == [1000.0]

    clock.now = 5000.0
    second = CachedPages(cache, live, provider=Transport(), task_id='t2')
    asyncio.run(planner(1).run(second))
    assert [use['source'] for use in second.uses.values()] == ['cache']
    assert [use['obtainedAt'] for use in second.uses.values()] == [1000.0]
    assert len(live.calls) == 1

    clock.now = 20_000.0
    third = CachedPages(cache, live, provider=Transport(), task_id='t3')
    asyncio.run(planner(1).run(third))
    assert [use['source'] for use in third.uses.values()] == ['live']
    assert [use['obtainedAt'] for use in third.uses.values()] == [20_000.0]
    assert len(live.calls) == 2


def test_a_cache_use_is_keyed_the_way_the_page_record_is():
    cache, live = KeyedCache(), Live()
    adapter = CachedPages(cache, live, provider=Transport(), task_id='t1')
    result = asyncio.run(planner(60).run(adapter))
    records = {tuple(record[key] for key in ('tileId', 'category', 'query', 'pageNum'))
               for item in result.coverage for record in item['pageRecords']}
    assert set(adapter.uses) == records
    for (tile, category, query, page), use in adapter.uses.items():
        assert use['sequenceId'] == f'{tile}:{category}:{query}'
        assert use['obtainedAt'] > 0


# -- §五 缓存重放与新增调用额度是两件事 --------------------------------------

ALL_MAJORS = list(majors())
ALL_MINORS = poi_keys(ALL_MAJORS)
#: 十类首轮：31 个检索小类 × 4 个查询分块。
ALL_PAGES = 4 * len(ALL_MINORS)
PRIMARY = {category: (RULES['queries'][category][0],) for category in ALL_MINORS}


def ten_category_planner(*, budget, limits=None):
    return OnlinePlanner(domain=QueryDomain.circle(1300), origin=ORIGIN,
                         categories=ALL_MINORS, budget=budget, source='synthetic',
                         queries=PRIMARY, limits=limits)


def spend_only_on_the_missing_pages(*, dispatched, allowance):
    """A metered page source: the first ``allowance`` calls go out, the rest refuse."""
    calls = []

    async def fetch(sequence_, page):
        if len(calls) >= allowance:
            raise BudgetExhausted('poi', allowance)
        calls.append((sequence_['sequenceId'], page))
        dispatched.append((sequence_['sequenceId'], page))
        return two_pages(sequence_, page), None

    return fetch


def test_the_precheck_plan_keys_are_the_keys_the_run_looks_up():
    """预检与执行共用一份计划：它算的键必须就是执行时要查的键。"""
    plan = poi_plan.initial_plan(QueryDomain.circle(1300), ORIGIN, ALL_MINORS,
                                 provider=Transport.identity, api_version=Transport.api_version)
    adapter = CachedPages(KeyedCache(), Live(), provider=Transport(), task_id='t')
    assert plan.page_count == ALL_PAGES
    assert [page.key for page in plan.pages] == [adapter.key(page.sequence, 0)
                                                 for page in plan.pages]


def test_more_than_sixty_cached_pages_do_not_stop_the_run_before_the_missing_ones():
    """§五：缓存里有一百多页要重放，新增调用另有额度 —— 处理页数不能被 60 卡住。

    同一份输入用旧的单层上限跑，第二轮只重放 60 页就停了，一页缺页都没请求到：
    下面把这两种口径并排放在一起，证明修的是"两把尺子"，而不是把计数器调大。
    """
    cache = KeyedCache(freshness_seconds=3600)
    plan = poi_plan.initial_plan(QueryDomain.circle(1300), ORIGIN, ALL_MINORS,
                                 provider=Transport.identity, api_version=Transport.api_version)
    for page in plan.pages:
        cache.store(page.key, two_pages(page.sequence, 0), task_id='t')
    assert plan.page_count == ALL_PAGES == 4 * len(ALL_MINORS) > 60

    # 旧的单层上限：60 次页面处理全花在重放上，缺页一页都没碰到。
    legacy_live = []
    legacy_adapter = CachedPages(cache, spend_only_on_the_missing_pages(
        dispatched=legacy_live, allowance=0), provider=Transport(), task_id='t')
    legacy = asyncio.run(ten_category_planner(budget=60).run(legacy_adapter))
    assert legacy.attempts == 60 and legacy.network_calls == 0
    assert legacy.stop_reason == 'budget_exhausted' and legacy.status == 'partial'
    assert {use['source'] for use in legacy_adapter.uses.values()} == {'cache'}
    assert {record['pageNum'] for entry in legacy.coverage
            for record in entry['pageRecords']} == {0}

    # 两把尺子：重放不花额度，额度只花在缺页上，并且全部页面都被处理到。
    dispatched = []
    adapter = CachedPages(cache, spend_only_on_the_missing_pages(
        dispatched=dispatched, allowance=3), provider=Transport(), task_id='t')
    planner = ten_category_planner(budget=3, limits=RunLimits(
        processing_steps=PROCESSING_STEP_LIMIT, drain_after_budget_refusal=True))
    result = asyncio.run(planner.run(adapter))
    assert result.attempts == 2 * ALL_PAGES          # 124 页重放 + 124 页缺页
    assert result.attempts > 60
    assert result.network_calls == len(dispatched) == 3
    assert result.status == 'partial'
    assert result.stop_reason == 'network_budget_exhausted'
    # 缓存那一百多页的观测一条不少，缺页的页面记录也各自留下了原因。
    assert len(result.observations) == 20 * (ALL_PAGES + 3)
    assert {record['reason'] for entry in result.coverage for record in entry['pageRecords']
            if not record['succeeded']} == {'task_budget_exhausted'}
    assert all(entry['successfulPages'] for entry in result.coverage)


def test_a_missing_page_after_a_fully_cached_first_round_is_partial_not_completed():
    """首轮全在缓存里、翻页缺额度：保留已取得结果，明确 partial，不误报完成。"""
    cache = KeyedCache(freshness_seconds=3600)
    planner = ten_category_planner(budget=0, limits=RunLimits(
        processing_steps=PROCESSING_STEP_LIMIT, drain_after_budget_refusal=True))
    adapter = CachedPages(cache, Live(), provider=Transport(), task_id='t')
    for state in planner.first_round:
        cache.store(adapter.key(state.mapping, 0), two_pages(state.mapping, 0), task_id='t')

    async def refuse(sequence_, page):
        raise BudgetExhausted('poi', 0)

    second = CachedPages(cache, refuse, provider=Transport(), task_id='t')
    result = asyncio.run(planner.run(second))
    assert result.network_calls == 0
    assert result.status == 'partial' and result.stop_reason == 'network_budget_exhausted'
    assert len(result.observations) == 20 * ALL_PAGES
    assert result.catalog_completeness == 'unverified'
    assert {use['source'] for use in second.uses.values()} == {'cache'}


def test_an_in_flight_page_shared_with_another_caller_is_not_a_new_call():
    """§五.1：等别人在飞的那一次不重复计费，也不算"这次取得的新页面"。"""
    cache, live = KeyedCache(), Live()
    started, release = asyncio.Event(), asyncio.Event()
    block = sequence('r0c0', -1300, -1300, 1300, 'pharmacy', RULES['queries']['pharmacy'][0],
                     LocalProjection(ORIGIN))

    async def blocking(sequence_, page):
        started.set()
        await release.wait()
        return two_pages(sequence_, page), None

    async def share():
        first = CachedPages(cache, blocking, provider=Transport(), task_id='t1')
        second = CachedPages(cache, live, provider=Transport(), task_id='t2')
        pending = asyncio.ensure_future(first(block, 0))
        await started.wait()
        # 第二个调用挂到同一个在飞请求上之后才放行：这样它读到的 manner 一定是"共享"，
        # 而不是碰巧读到已经存好的页面。
        shared_request = asyncio.ensure_future(second(block, 0))
        await asyncio.sleep(0)
        release.set()
        return await shared_request, await pending, second

    shared, _built, second = asyncio.run(share())
    assert shared.manner == 'shared'
    # 派发方是 t1 的那一路；t2 的传输一次都没被调用，共享的结果仍由派发方写入缓存。
    assert len(live.calls) == 0 and len(cache) == 1
    key = (block['tileId'], block['category'], block['query'], 0)
    assert second.uses[key]['delivery'] == 'shared'
    assert second.uses[key]['source'] == 'cache'


def test_a_failed_shared_call_is_not_a_dispatch_for_the_waiter():
    """§五.1：共享的那次调用失败了，账仍算在派发方头上 —— 等待方没有发过请求。"""
    cache = KeyedCache()
    entered, release = asyncio.Event(), asyncio.Event()

    async def failing(sequence_, page):
        entered.set()
        await release.wait()
        raise httpx.ReadTimeout('synthetic timeout')

    async def never(sequence_, page):
        raise AssertionError('the waiter must not dispatch')

    async def share():
        owner, waiter = planner(60), planner(60)
        owner_adapter = CachedPages(cache, failing, provider=Transport(), task_id='t1')
        waiter_adapter = CachedPages(cache, never, provider=Transport(), task_id='t2')
        block = owner.first_round[0].mapping
        assert waiter.first_round[0].mapping['sequenceId'] == block['sequenceId']
        owner_call = asyncio.ensure_future(owner._attempt(owner_adapter, block, 0))
        await entered.wait()
        waiter_call = asyncio.ensure_future(waiter._attempt(waiter_adapter, block, 0))
        await asyncio.sleep(0)
        release.set()
        return await owner_call, await waiter_call

    owner_answer, waiter_answer = asyncio.run(share())
    # 派发方：真的发了请求，失败也要计入。
    assert owner_answer.dispatched is True
    assert (owner_answer.manner, owner_answer.reason) == ('live', 'timeout')
    # 等待方：同一页、同一个失败原因，但它一次都没发。
    assert waiter_answer.dispatched is False
    assert (waiter_answer.manner, waiter_answer.reason) == ('shared', 'timeout')
    # 失败不写入成功缓存。
    assert len(cache) == 0


def test_only_the_dispatcher_counts_a_new_call_when_the_shared_call_fails():
    """§五.1 的整轮口径：等待方处理了同一页，但一次新增调用都不算。

    派发方自己发了多少次（含失败页的重试）就要记多少次 —— 这里用 fetch 的真实调用
    记录核对，而不是拿一个期望常数去比。
    """
    cache = KeyedCache()
    entered, release = asyncio.Event(), asyncio.Event()
    dispatched = []

    async def failing(sequence_, page):
        dispatched.append((sequence_['sequenceId'], page))
        entered.set()
        await release.wait()
        raise httpx.ReadTimeout('synthetic timeout')

    async def never(sequence_, page):
        raise AssertionError('the waiter must not dispatch')

    async def share():
        owner, waiter = planner(60), planner(1)
        owner_adapter = CachedPages(cache, failing, provider=Transport(), task_id='t1')
        waiter_adapter = CachedPages(cache, never, provider=Transport(), task_id='t2')
        owner_run = asyncio.ensure_future(owner.run(owner_adapter))
        await entered.wait()
        waiter_run = asyncio.ensure_future(waiter.run(waiter_adapter))
        await asyncio.sleep(0)
        release.set()
        return await owner_run, await waiter_run

    owner_result, waiter_result = asyncio.run(share())
    # 派发方：每一次真实调用都记一次，不多不少。
    assert owner_result.network_calls == len(dispatched) > 0
    # 等待方：同一页、同一个失败原因，但一次都没发。
    assert waiter_result.attempts == 1
    assert waiter_result.network_calls == 0
    assert {record['reason'] for entry in waiter_result.coverage
            for record in entry['pageRecords']} == {'timeout'}
