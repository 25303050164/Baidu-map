"""§4.2 adaptive online planning; synthetic pages only, never a real transport."""
import asyncio
from collections import Counter
import math

import pytest

from app.poi.models import PoiCollectRequest
from app.poi.online import (FINEST_BLOCK_METERS, OnlinePlanner, QueryBlock, QueryDomain,
                            clip_to_domain, coarse_blocks)
from app.poi.planner import RULES, parameters
from app.quota import DailyBudgetExhausted
from app.request_control import RequestStopped
from life_circle.coordinates import LocalProjection

ORIGIN = (121.514, 31.313)
CATEGORIES = ('market', 'pharmacy', 'primary_school')


def row(uid, location=None):
    return {'uid': uid, 'name': f'合成{uid}', 'location': location or {'lng': ORIGIN[0], 'lat': ORIGIN[1]}}


def payload(rows, total=None):
    return {'status': 0, 'result_type': 'poi_type', 'total': len(rows) if total is None else total,
            'results': rows}


def page_of_one(sequence, page):
    """The default: one row, stated total 1 — a query that ends on its first page."""
    return payload([row(f"synthetic-{sequence['sequenceId']}-{page}")])


def two_pages(sequence, page):
    """Twenty rows and a stated total of forty, so every query asks for a second page."""
    return payload([row(f"synthetic-{sequence['sequenceId']}-{page}-{index}") for index in range(20)],
                   total=40)


class Synthetic:
    """A page source that records what was asked, in the order it was asked."""

    def __init__(self, default=page_of_one):
        self.default, self.calls = default, []

    async def __call__(self, sequence, page):
        self.calls.append((sequence['tileId'], sequence['category'], sequence['query'], page))
        outcome = self.default(sequence, page)
        return outcome if isinstance(outcome, tuple) else (outcome, None)


def build(domain, *, budget=60, categories=CATEGORIES, **kwargs):
    return OnlinePlanner(domain=domain, origin=ORIGIN, categories=categories, budget=budget,
                         source='synthetic', **kwargs)


def entry(result, tile_id, category):
    return next(item for item in result.coverage
                if item['tileId'] == tile_id and item['category'] == category
                and item['query'] == RULES['queries'][category][0])


def local(lng_offset, lat_offset=0.0):
    projection = LocalProjection(ORIGIN)
    lng, lat = projection.to_geographic((lng_offset, lat_offset))
    return {'lng': lng, 'lat': lat}


def test_coarse_blocks_are_the_envelope_split_in_two_keeping_only_intersecting_blocks():
    # A bar across the full width but only the lower part of its envelope: the
    # two upper blocks of the 2×2 split hold nothing of the domain.
    domain = QueryDomain(((0, 0), (100, 0), (100, 45), (0, 45)))
    blocks = coarse_blocks(domain)
    assert [block.tile_id for block in blocks] == ['r0c0', 'r0c1']
    assert [block.bounds for block in blocks] == [(0.0, 0.0, 50.0, 50.0), (50.0, 0.0, 100.0, 50.0)]
    looked = Synthetic()
    planner = build(domain)
    asyncio.run(planner.run(looked))
    assert {call[0] for call in looked.calls} == {'r0c0', 'r0c1'}


def test_a_circular_domain_starts_from_its_four_quadrants():
    planner = build(QueryDomain.circle(1300))
    assert [block.tile_id for block in planner.coarse] == ['r0c0', 'r0c1', 'r1c0', 'r1c1']
    assert [block.bounds for block in planner.coarse] == [
        (-1300.0, -1300.0, 0.0, 0.0), (0.0, -1300.0, 1300.0, 0.0),
        (-1300.0, 0.0, 0.0, 1300.0), (0.0, 0.0, 1300.0, 1300.0)]


def test_a_block_is_searched_with_its_circumscribed_circle_and_documented_parameters():
    planner = build(QueryDomain.circle(1300))
    mapping = next(iter(planner.sequences.values())).mapping
    parameters_sent = parameters(mapping, 0)
    assert parameters_sent['coord_type'] == 3 and parameters_sent['radius_limit'] == 'true'
    assert parameters_sent['scope'] == 2 and parameters_sent['page_size'] == 20
    assert parameters_sent['page_num'] == 0
    assert parameters_sent['radius'] == mapping['radius'] == math.ceil(1300/math.sqrt(2)) + 5
    assert mapping['localMeters'] == [-1300.0, -1300.0, 0.0, 0.0]


def test_the_first_round_issues_every_category_before_any_second_keyword():
    looked = Synthetic(default=two_pages)
    planner = build(QueryDomain.circle(1300))
    result = asyncio.run(planner.run(looked))
    rounds = len(planner.coarse)*sum(len(RULES['queries'][category]) for category in CATEGORIES)
    first_round = [call for call in looked.calls[:rounds]]
    assert all(call[3] == 0 for call in first_round)
    assert all(call[3] > 0 for call in looked.calls[rounds:])
    # The rotation is what keeps a category with more keywords from being
    # exhausted first: within each block, every category's first keyword is
    # issued before any category's second.
    for block in planner.coarse:
        calls = [call[1:3] for call in first_round if call[0] == block.tile_id]
        firsts = [index for index, (category, query) in enumerate(calls)
                  if query == RULES['queries'][category][0]]
        later = [index for index, (category, query) in enumerate(calls)
                 if query != RULES['queries'][category][0]]
        assert len(firsts) == len(CATEGORIES)
        assert later and max(firsts) < min(later)
    assert result.status == 'completed' and len(result.blocks) == 4


def test_a_truncated_block_is_queried_again_in_smaller_circles_for_that_keyword_only():
    pharmacy = RULES['queries']['pharmacy'][0]

    def crowded_page(sequence, page):
        if sequence['tileId'] == 'r0c0' and sequence['query'] == pharmacy:
            return payload([row(f"crowded-{page}-{index}") for index in range(20)], total=150)
        return page_of_one(sequence, page)

    looked = Synthetic(default=crowded_page)
    planner = build(QueryDomain.circle(1300), categories=('pharmacy', 'primary_school'), max_pages=2)
    result = asyncio.run(planner.run(looked))
    children = [block for block in result.blocks if block.tile_id.startswith('r0c0.')]
    assert len(children) == 4 and all(block.edge == 650 for block in children)
    repeated = {(call[0], call[1]) for call in looked.calls if call[0].startswith('r0c0.')}
    assert repeated == {(block.tile_id, 'pharmacy') for block in children}
    # The parent keeps its incomplete evidence, and the children resolve it.
    parent = entry(result, 'r0c0', 'pharmacy')
    assert parent['status'] == 'partial' and parent['truncated'] is True
    assert parent['stopReason'] == 'possible_truncation' and parent['pages'] == 2
    assert result.status == 'completed' and result.stop_reason is None


def test_a_finest_block_that_stays_saturated_cannot_report_completion():
    def saturated_page(sequence, page):
        return payload([row(f"crowded-{page}-{index}") for index in range(20)], total=150)

    planner = build(QueryDomain.circle(100), categories=('pharmacy',), max_pages=2)
    assert [block.edge for block in planner.coarse] == [100.0]*4
    result = asyncio.run(planner.run(Synthetic(default=saturated_page)))
    assert result.status == 'partial'
    assert 'finest_block_saturated' in result.warnings
    assert all('.' not in block.tile_id for block in result.blocks)
    assert all(item['truncated'] for item in result.coverage)


def test_a_repeated_page_is_evidence_of_an_unstable_query_not_of_completion():
    def repeating_page(sequence, page):
        return payload([row(f"repeated-{index}") for index in range(20)], total=40)

    planner = build(QueryDomain.circle(1300), categories=('pharmacy',))
    result = asyncio.run(planner.run(Synthetic(default=repeating_page)))
    assert result.status != 'completed'
    assert not any(item['status'] == 'completed' for item in result.coverage)
    assert {item['stopReason'] for item in result.coverage} == {'pagination_anomaly',
                                                                'budget_exhausted'}
    # The anomaly is a density signal, so the block is still worth subdividing.
    assert {block.tile_id for block in result.blocks} > {'r0c0', 'r0c1', 'r1c0', 'r1c1'}
    # A source that never stops repeating keeps subdividing until the task budget
    # is gone: §4.2.5 puts subdivision under that budget rather than outside it.
    assert result.stop_reason == 'budget_exhausted' and result.attempts == 60


def test_a_failed_page_is_retried_once_inside_the_same_budget():
    pharmacy = RULES['queries']['pharmacy'][0]

    def flaky_once():
        """One refusal per block for the first keyword, then clean pages."""
        seen = Counter()

        def fetch(sequence, page):
            if sequence['query'] == pharmacy and page == 0:
                seen[sequence['tileId']] += 1
                if seen[sequence['tileId']] == 1:
                    return None, 'timeout'
            return page_of_one(sequence, page)
        return fetch

    planner = build(QueryDomain.circle(1300), categories=('pharmacy',))
    result = asyncio.run(planner.run(Synthetic(default=flaky_once())))
    assert result.status == 'completed'
    records = entry(result, 'r0c0', 'pharmacy')['pageRecords']
    assert [(record['succeeded'], record['reason']) for record in records] == [
        (False, 'timeout'), (True, None)]
    # Eight first pages, four of which were refused once and retried once.
    assert result.attempts == 12 and len(result.observations) == 8
    starved = build(QueryDomain.circle(1300), categories=('pharmacy',), budget=1)
    failed = asyncio.run(starved.run(Synthetic(default=flaky_once())))
    assert failed.status == 'failed' and failed.stop_reason == 'budget_exhausted'
    assert failed.attempts == 1 and entry(failed, 'r0c0', 'pharmacy')['pages'] == 0
    assert entry(failed, 'r0c0', 'pharmacy')['stopReason'] == 'timeout'


def test_budget_exhaustion_keeps_the_rows_it_obtained_and_says_partial():
    planner = build(QueryDomain.circle(1300), budget=2)
    result = asyncio.run(planner.run(Synthetic()))
    assert result.status == 'partial' and result.stop_reason == 'budget_exhausted'
    assert result.attempts == 2 and len(result.observations) == 2
    assert len(result.coverage) == 4 * sum(len(RULES['queries'][c]) for c in CATEGORIES)
    assert [item['status'] for item in result.coverage][:2] == ['completed', 'completed']
    assert {item['stopReason'] for item in result.coverage[2:]} == {'budget_exhausted'}


@pytest.mark.parametrize('refusal,reason', [
    (RequestStopped('rate_limit'), 'rate_limit'),
    (DailyBudgetExhausted('place', 1600), 'daily_budget_exhausted'),
])
def test_a_scheduling_refusal_stops_the_run_and_never_reads_as_completion(refusal, reason):
    async def refuse(sequence, page):
        raise refusal

    result = asyncio.run(build(QueryDomain.circle(1300)).run(refuse))
    assert result.status == 'failed'
    assert result.stop_reason == reason and reason in result.warnings
    assert {item['stopReason'] for item in result.coverage} == {reason}


def test_clipping_keeps_the_domain_and_names_what_it_dropped():
    domain = QueryDomain.circle(300)
    kept, rejected = clip_to_domain([
        {'sourceUid': 'synthetic-in', 'location': local(10), 'provenance': ['p']},
        {'sourceUid': 'synthetic-edge', 'location': local(300), 'provenance': []},
        {'sourceUid': 'synthetic-out', 'location': local(400, 400), 'provenance': []},
    ], domain, ORIGIN)
    assert [record['sourceUid'] for record in kept] == ['synthetic-in', 'synthetic-edge']
    assert rejected == [{'sourceUid': 'synthetic-out', 'reason': 'outside_query_domain',
                         'provenance': []}]


def test_every_page_record_carries_its_own_request_success_total_and_source():
    looked = Synthetic(default=two_pages)
    result = asyncio.run(build(QueryDomain.circle(1300), categories=('pharmacy',)).run(looked))
    first = result.coverage[0]
    assert [(record['pageNum'], record['requested'], record['succeeded'], record['reason'],
             record['total'], record['returned'], record['source'])
            for record in first['pageRecords']] == [
        (0, True, True, None, 40, 20, 'synthetic'), (1, True, True, None, 40, 20, 'synthetic')]
    assert first['requestedPages'] == [0, 1] and first['successfulPages'] == [0, 1]
    assert first['reportedTotals'] == [{'pageNum': 0, 'total': 40}, {'pageNum': 1, 'total': 40}]
    assert first['pageErrors'] == [] and first['returned'] == 40 and first['total'] == 40
    assert first['source'] == 'synthetic' and first['truncated'] is False
    assert first['localMeters'] == [-1300.0, -1300.0, 0.0, 0.0]


def test_the_schedule_is_a_function_of_the_domain_and_the_budget():
    domain = QueryDomain.circle(1300)
    first, second = Synthetic(default=two_pages), Synthetic(default=two_pages)
    runs = [asyncio.run(build(domain).run(source)) for source in (first, second)]
    assert first.calls == second.calls
    assert runs[0].coverage == runs[1].coverage
    assert runs[0].status == runs[1].status == 'completed'


def test_the_planner_cannot_express_a_complete_catalog():
    result = asyncio.run(build(QueryDomain.circle(1300)).run(Synthetic()))
    assert result.catalog_completeness == 'unverified'
    with pytest.raises(AttributeError):
        result.catalog_completeness = 'complete'


def test_the_finest_block_edge_is_two_hundred_and_fifty_metres():
    block = QueryBlock('r0c0', 0, 0, 500)
    children = block.children()
    assert [child.tile_id for child in children] == ['r0c0.0', 'r0c0.1', 'r0c0.2', 'r0c0.3']
    assert [(child.x, child.y, child.edge) for child in children] == [
        (0.0, 0.0, 250.0), (250.0, 0.0, 250.0), (0.0, 250.0, 250.0), (250.0, 250.0, 250.0)]
    assert children[0].children() == ()
    assert QueryBlock('r0c0', 0, 0, 250).subdividable() is False
    assert QueryBlock('r0c0', 0, 0, FINEST_BLOCK_METERS*2 - 0.1).children() == ()
    with pytest.raises(ValueError, match='invalid_query_block'):
        QueryBlock('r0c0', 0, 0, 0)


def test_an_empty_page_or_a_changed_total_keeps_incomplete_evidence():
    builders = {
        # A page arrives before the stated total does, then the query stops.
        'empty_page': lambda page: payload([], total=30) if page else
                      payload([row(f"empty-{index}") for index in range(20)], total=30),
        # The stated total changes between pages, so the paging cannot be trusted.
        'changed_total': lambda page: payload(
            [row(f"changed-{page}-{index}") for index in range(20 if page else 10)],
            total=25 if page else 30),
    }

    def run(name):
        def table(sequence, page):
            return builders[name](page) if sequence['category'] == 'pharmacy' \
                else page_of_one(sequence, page)
        planner = build(QueryDomain.circle(1300), categories=('pharmacy', 'primary_school'))
        return planner, asyncio.run(planner.run(Synthetic(default=table)))

    for name in builders:
        planner, result = run(name)
        for item in result.coverage:
            if item['category'] == 'pharmacy' and '.' not in item['tileId']:
                assert item['status'] == 'partial', name
                assert item['stopReason'] == 'pagination_uncertain', name
                assert 'pagination_uncertain' in item['warnings'], name
        assert result.status != 'completed', name
        assert {block.tile_id for block in result.blocks} > {'r0c0', 'r0c1', 'r1c0', 'r1c1'}, name


def test_invalid_configuration_is_refused():
    with pytest.raises(ValueError, match='window outside supported projection'):
        build(QueryDomain.circle(20_000_000))
    with pytest.raises(ValueError, match='invalid_query_domain'):
        QueryDomain(((0, 0), (1, 1)))
    with pytest.raises(ValueError, match='empty_query_domain'):
        build(QueryDomain(((0.0, 0.0), (0.0, 0.0), (0.0, 0.0))))
    with pytest.raises(ValueError, match='unknown_category'):
        # ``supermarket`` 现在在设施目录里（购物），换一个目录里真的没有的名字。
        build(QueryDomain.circle(1300), categories=('no_such_category',))
    with pytest.raises(ValueError, match='budget must be a positive integer'):
        build(QueryDomain.circle(1300), budget=0)
    with pytest.raises(ValueError, match='at least one category required'):
        build(QueryDomain.circle(1300), categories=())


def test_the_planner_takes_the_request_centre():
    request = PoiCollectRequest(coordinateSystem='bd09ll')
    planner = OnlinePlanner(domain=QueryDomain.circle(1300),
                            origin=(request.center.lng, request.center.lat),
                            categories=CATEGORIES, budget=60, source='synthetic')
    assert len(planner.coarse) == 4
    assert len(planner.sequences) == 4 * sum(len(RULES['queries'][c]) for c in CATEGORIES)


def test_a_throttled_page_is_retried_once_and_the_round_keeps_going():
    """一次并发超限不再终止整轮：重试成功之后，剩下的查询照常跑完。

    这是"十大类下百度并发限制导致拿不到结果"的直接修复：以前第一次 401 就让整轮归零，
    现在它只花掉一次有界重试，并且仍被记进它自己的分块记录里。
    """
    looked = []

    class ThrottleOnce:
        async def __call__(self, sequence, page):
            looked.append(sequence['sequenceId'])
            if len(looked) == 1:
                return None, 'rate_limit'
            return page_of_one(sequence, page), None

    planner = build(QueryDomain.circle(1300), budget=10, categories=('pharmacy',))
    result = asyncio.run(planner.run(ThrottleOnce()))
    assert result.stop_reason is None
    assert result.status == 'completed'
    assert 'rate_limit' not in result.warnings
    # 第一次失败留在它自己的页面记录里，没有被抹掉，也没有被读成"这里没有设施"。
    errors = [error for item in result.coverage for error in item['pageErrors']]
    assert [error['reason'] for error in errors] == ['rate_limit']
    assert errors[0]['pageNum'] == 0


def test_persistent_throttling_pauses_after_one_bounded_retry_and_keeps_progress():
    """持续限流：同一页重试一次就暂停本轮，不是每一类都先花一次请求再说。"""
    looked = []

    class AlwaysThrottled:
        async def __call__(self, sequence, page):
            looked.append(sequence['sequenceId'])
            return None, 'rate_limit'

    planner = build(QueryDomain.circle(1300), budget=60, categories=('pharmacy',))
    result = asyncio.run(planner.run(AlwaysThrottled()))
    assert len(looked) == 2 and looked[0] == looked[1]
    assert result.attempts == 2
    assert result.stop_reason == 'rate_limit' and 'rate_limit' in result.warnings
    assert result.status == 'failed'
    assert {item['stopReason'] for item in result.coverage} == {'rate_limit'}
