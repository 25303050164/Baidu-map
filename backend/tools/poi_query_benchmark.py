"""C0: measure what a facility retrieval really spends and really finds.

The question here is not "how many pages does the plan theoretically ask for" but
"how many calls actually left the process, what did they cost the task bucket and
the day's ledger, and which facilities did they establish". Every number therefore
comes from the real retrieval path — the adaptive planner, the real page keys and
cache, and the one atomic reservation point — over a synthetic directory this tool
owns.

Run from ``backend``::

    .venv/bin/python -m tools.poi_query_benchmark --suite baseline \
        --output ../.tmp/poi-benchmark

Per scenario it emits: the per-page schedule (block, category, keyword, page,
delivery, outcome), dispatched calls, retries, cache hits and shares, the task
bucket and daily ledger deltas, effective unique POI UIDs per minor and major
category, pagination/truncation/stop reasons, classification outcomes, and latency
percentiles. It also asserts the accounting identities it depends on — dispatch
counts must equal both budget deltas and the session's own call log — and exits
non-zero when one fails, because a benchmark that reports a number it cannot check
is worse than no benchmark.

Two things are kept apart on purpose:

* **Relative recall** is measured against the independent-query baseline's own UID
  set. That is a comparison between two plans over one world.
* **Fixture coverage** is measured against the directory the fixture generated. It
  is a property of the fixture, reported for diagnostics only. It is never a
  real-world recall figure: the independent baseline is not ground truth about a
  real directory, and a synthetic world is not one at all.
"""
import argparse
import asyncio
import json
import math
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app import catalog
from app.cache import KeyedCache
from app.checkups.places import POI_POOL
from app.config import Settings
from app.poi import plan as poi_plan
from app.poi.cache import CachedPages
from app.poi.models import PoiCollectRequest
from app.poi.models import Point as WirePoint
from app.poi.normalize import merge_entities, normalize
from app.poi.online import (PROCESSING_STEP_LIMIT, OnlinePlanner, QueryDomain, RunLimits,
                            clip_to_domain)
from app.poi.planner import RULES
from app.quota import PLACE, Quota
from life_circle.coordinates import LocalProjection
from life_circle.models import CancelToken
from pydantic import SecretStr

from tools.poi_benchmark_guard import Guard, install
from tools.poi_benchmark_world import (MODEL_ASSUMPTIONS, Fault, SyntheticService, build_world)

#: The fixture's geometric centre, and the area one task must cover. Kept here
#: rather than derived from a boundary engine: this tool measures retrieval cost and
#: coverage, and a boundary would add a second variable to every comparison.
ORIGIN = (116.405, 39.916)
DOMAIN_RADIUS_M = 750.0
#: Fixed seed: every number in the report must be reproducible from this file alone.
SEED = 20261009
#: One scenario's wall-clock ceiling. Real deadlines are the planner's business; this
#: only stops a fixture bug from hanging the tool.
RUN_TIMEOUT_S = 600.0
#: Explicit experiment budget for "the allowance is not what is binding" scenarios.
#: Not a product default and not a proposal — the local processing ceiling still caps
#: the run, which is exactly what such a scenario is meant to reveal.
GENEROUS_BUDGET = 100_000
#: The single-task default the C0/C2 evidence was measured at. Pinned instead of read
#: from ``models.DEFAULT_POI_REQUESTS``: the deployment default became 240 on
#: 2026-10-09, and a scenario named ``-60`` that silently ran at 240 would make the
#: recorded results unreproducible while still looking comparable to them.
RECORDED_DEFAULT_BUDGET = 60
#: Fixture pacing. Production pacing is exercised by the test suite, not here.
OFFLINE_QPS = 10_000.0
SHANGHAI = ZoneInfo('Asia/Shanghai')
#: The controlled instant the ledger's "today" is read from. Deliberately after the
#: configured tier switch, so the run reports the tier that actually applies now.
CONTROLLED_INSTANT = datetime(2026, 10, 9, 12, 0, tzinfo=SHANGHAI)


def controlled_clock() -> float:
    return CONTROLLED_INSTANT.timestamp()


@dataclass(frozen=True)
class Scenario:
    """One world, one budget, one arm — the unit the report tabulates."""

    key: str
    label: str
    density_per_km2: int
    task_budget: int
    #: The day's ceiling. Generous by default so that a scenario measuring "what does
    #: this plan cost" is not silently answering "what does this day allow"; a scenario
    #: that *means* to hit the day sets it explicitly. Each scenario also gets its own
    #: ledger, because the controlled clock pins every scenario to one day.
    daily_budget: int = GENEROUS_BUDGET
    majors: tuple | None = None
    fault: Fault | None = None
    extra_keyword_share: float = 0.0
    prefilled: bool = False

    @property
    def categories(self):
        majors = catalog.default_analysis_majors() if self.majors is None else self.majors
        return catalog.poi_keys(majors)


BASELINE_SCENARIOS = (
    Scenario('sparse-generous', '稀疏 · 冷启动 · 额度充足', 300, GENEROUS_BUDGET),
    Scenario('moderate-generous', '适中 · 冷启动 · 额度充足', 1200, GENEROUS_BUDGET),
    Scenario('dense-generous', '密集 · 冷启动 · 额度充足', 5000, GENEROUS_BUDGET),
    Scenario('dense-default-60', '密集 · 冷启动 · 默认单任务 60', 5000, RECORDED_DEFAULT_BUDGET),
    Scenario('dense-ten-majors-60', '密集 · 十类 · 默认单任务 60', 5000, RECORDED_DEFAULT_BUDGET,
             majors=catalog.majors()),
    Scenario('dense-ten-majors-generous', '密集 · 十类 · 额度充足', 5000, GENEROUS_BUDGET,
             majors=catalog.majors()),
    Scenario('moderate-prefilled-zero', '适中 · 全缓存 · 任务与日余额 0', 1200, 0,
             daily_budget=0, prefilled=True),
    Scenario('dense-no-match', '密集夹具 · 服务答"无匹配"', 5000, GENEROUS_BUDGET,
             fault=Fault(results_mode='no_match')),
    Scenario('dense-empty-with-total', '密集夹具 · 空页但 total 为正', 5000, GENEROUS_BUDGET,
             fault=Fault(results_mode='empty_with_total')),
    Scenario('dense-rate-limit', '密集夹具 · 持续限流', 5000, GENEROUS_BUDGET,
             fault=Fault(reason='rate_limit')),
    Scenario('dense-upstream-error', '密集夹具 · 持续上游错误', 5000, GENEROUS_BUDGET,
             fault=Fault(reason='upstream_error')),
    Scenario('dense-quota', '密集夹具 · 上游配额用尽', 5000, GENEROUS_BUDGET,
             fault=Fault(reason='quota')),
    Scenario('dense-first-call-fails', '密集夹具 · 首屏超时后恢复', 5000, GENEROUS_BUDGET,
             fault=Fault(reason='timeout', from_call=1)),
)

SUITES = {'baseline': BASELINE_SCENARIOS}

def benchmark_settings(*, daily_budget: int, ledger_path: Path) -> Settings:
    """Offline settings: no dotenv file, no real key, no pacing.

    ``_env_file=None`` keeps ``backend/.env`` — which may hold a real AK — out of the
    process entirely rather than trusting it to be absent, and the placeholder key
    makes any attempt to use one obvious. The connection guard refuses the traffic
    regardless, so these are three independent reasons a real call cannot happen.
    """
    return Settings(
        _env_file=None,
        analysis_provider='synthetic',
        baidu_map_ak=SecretStr('benchmark-offline-no-network'),
        analysis_qps=OFFLINE_QPS,
        baidu_place_qps=OFFLINE_QPS,
        baidu_direction_qps=OFFLINE_QPS,
        baidu_fallback_place_qps=OFFLINE_QPS,
        baidu_fallback_direction_qps=OFFLINE_QPS,
        baidu_place_daily_budget=daily_budget,
        baidu_fallback_place_daily_budget=daily_budget,
        quota_ledger_path=ledger_path)


class BenchmarkSession:
    """One arm's place fetches: one reservation per attempt through the real pool.

    The production session's shape exactly — the pool is entered once per page and
    the outcome is reported to it — so a refusal stops the call *before* it is sent
    and a sent call is already counted. ``dispatches`` is this session's own record
    of what really went out, kept so the planner's counter can be checked against
    something that is not the planner.
    """

    def __init__(self, service: SyntheticService, pool, *, budget, deadline, hook=None):
        self.service, self.pool, self.budget, self.deadline = service, pool, budget, deadline
        self.dispatches = []
        #: Called after each dispatch with the running count. The cancellation scenario
        #: uses it to cancel mid-run, which is how a real cancel arrives: not before the
        #: run starts, but between two pages.
        self.hook = hook

    async def __call__(self, sequence, page):
        async with self.pool.attempt(self.deadline, budget=self.budget, pool=POI_POOL) as attempt:
            started = time.perf_counter()
            payload, reason = self.service.respond(sequence, page)
            attempt.outcome(reason)
            self.dispatches.append({'tileId': sequence['tileId'], 'category': sequence['category'],
                                    'query': sequence['query'], 'page': page,
                                    'seconds': time.perf_counter() - started,
                                    'reason': reason, 'answered': payload is not None})
            if self.hook is not None:
                self.hook(len(self.dispatches))
        return payload, reason


def accounted_schedule(result, fetch) -> list:
    """Every page the run processed, with who paid for it.

    The planner's own page records say what each request established; the cache
    wrapper says whether it was dispatched, replayed or shared. Neither alone answers
    §6 C0's question, and inferring the second from a counter the planner also moves
    is the guess this join exists to avoid.
    """
    schedule = []
    for entry in result.coverage:
        for record in entry['pageRecords']:
            use = fetch.uses.get((record['tileId'], record['category'], record['query'],
                                  record['pageNum'])) or {}
            schedule.append({
                'tileId': record['tileId'], 'category': record['category'],
                'query': record['query'], 'page': record['pageNum'],
                'delivery': use.get('delivery', 'refused' if not record['succeeded'] else None),
                'answered': bool(record['succeeded']), 'reason': record['reason'],
                'returned': record['returned'], 'total': record['total'],
                'truncated': bool(record['truncated']), 'warnings': record['warnings']})
    return schedule


def keyword_completeness(result) -> list:
    """One row per (block, category, keyword): what that keyword did and did not establish."""
    rows = []
    for entry in result.coverage:
        rows.append({
            'tileId': entry['tileId'], 'category': entry['category'], 'query': entry['query'],
            'status': entry['status'], 'stopReason': entry['stopReason'],
            'pages': entry['pages'], 'returned': entry['returned'], 'total': entry['total'],
            'truncated': entry['truncated'], 'warnings': entry['warnings'],
            'requestedPages': entry['requestedPages'], 'pageErrors': entry['pageErrors']})
    return sorted(rows, key=lambda row: (row['tileId'], row['category'], row['query']))


def evaluate(world, result, domain, categories) -> dict:
    """Standard result handling, then the two coverage views C0 asks for."""
    records, quarantine = [], 0
    for observation in result.observations:
        try:
            records.append(normalize(observation.row, observation.provenance, 'synthetic'))
        except ValueError:
            quarantine += 1
    inside, outside_window = clip_to_domain(records, domain, ORIGIN)
    request = PoiCollectRequest(center=WirePoint(lng=ORIGIN[0], lat=ORIGIN[1]),
                                coordinate_system='bd09ll', categories=list(categories))
    projection = LocalProjection(ORIGIN)
    accepted, review, excluded, outside_counted, merged_away = merge_entities(
        inside, request,
        within=lambda point: domain.contains(projection.to_local((point['lng'], point['lat']))))
    intended = world.intended_uids()

    def by_category(items) -> dict:
        grouped: dict[str, set] = {}
        for item in items:
            if item['category']:
                grouped.setdefault(item['category'], set()).add(item['sourceUid'])
        return grouped

    accepted_by, review_by = by_category(accepted), by_category(review)
    per_minor = {}
    for category in categories:
        uids = accepted_by.get(category, set())
        fixture = intended.get(category, frozenset())
        per_minor[category] = {
            'major': catalog.major_of(category) or category,
            'acceptedUids': len(uids),
            'reviewUids': len(review_by.get(category, set())),
            'fixtureUids': len(fixture),
            'fixtureCovered': len(uids & fixture),
            'uids': sorted(uids)}
    per_major: dict[str, set] = {}
    for category, item in per_minor.items():
        per_major.setdefault(item['major'], set()).update(item['uids'])
    return {
        'observations': len(result.observations),
        'quarantined': quarantine,
        'outsideQueryDomain': len(outside_window),
        'outsideCountingRegion': len(outside_counted),
        'duplicateRecordsMerged': merged_away,
        'accepted': len(accepted), 'needsReview': len(review), 'excluded': len(excluded),
        'perMinor': per_minor,
        'perMajor': {major: {'acceptedUids': len(uids), 'uids': sorted(uids)}
                     for major, uids in sorted(per_major.items())},
        'catalogCompleteness': result.catalog_completeness,
    }


def percentiles(values) -> dict:
    if not values:
        return {'count': 0, 'p50': None, 'p95': None, 'max': None, 'totalSeconds': 0.0}
    ordered = sorted(values)

    def pick(fraction):
        return ordered[min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))]

    return {'count': len(ordered), 'p50': round(pick(0.50), 6), 'p95': round(pick(0.95), 6),
            'max': round(ordered[-1], 6), 'totalSeconds': round(sum(ordered), 6)}


def measure_result(*, result, fetch, session, quota, task, ledger_before, spent_before, world,
                   domain, categories, guard, service_calls, extra=None) -> dict:
    """The one measurement definition. Both arms go through it, on purpose.

    Two arms costed by two similar-looking blocks of code would eventually disagree
    about what a dispatch or a cache hit is, and the comparison would then be between
    two measurements rather than between two plans. The accounting identities below
    are asserted here for the same reason: a number this tool cannot check is not
    evidence.
    """
    ledger_delta = quota.ledger.spent(PLACE) - ledger_before
    task_delta = task.spent.get(POI_POOL, 0) - spent_before
    schedule = accounted_schedule(result, fetch)
    deliveries = {'live': 0, 'cache': 0, 'shared': 0, 'refused': 0}
    for item in schedule:
        deliveries[item['delivery'] if item['delivery'] in deliveries else 'refused'] += 1
    checks = {
        'dispatchedEqualsTaskDelta': result.network_calls == task_delta,
        'dispatchedEqualsLedgerDelta': result.network_calls == ledger_delta,
        'dispatchedEqualsSessionCalls': result.network_calls == len(session.dispatches),
        'processedEqualsSchedule': result.attempts == len(schedule),
        'deliveriesReconcile': sum(deliveries.values()) == len(schedule),
        'noExternalConnections': guard.attempts == 0,
    }
    measured = {
        'dispatched': result.network_calls, 'pagesProcessed': result.attempts,
        'taskBudgetDelta': task_delta, 'dailyLedgerDelta': ledger_delta,
        'status': result.status, 'stopReason': result.stop_reason,
        'warnings': sorted(result.warnings),
        'blocks': len(result.blocks), 'sequences': len(result.coverage),
        'allowanceRefusals': getattr(result, 'budget_refusals', None), 'deliveries': deliveries,
        'retriedPages': sum(1 for item in schedule if item['page'] > 0 or not item['answered']),
        'sessionCalls': len(session.dispatches),
        'latency': percentiles([item['seconds'] for item in session.dispatches]),
        'schedule': schedule, 'keywordRuns': keyword_completeness(result),
        'coverage': evaluate(world, result, domain, categories),
        'checks': checks, 'invariantsHold': all(checks.values()),
        'serviceCalls': service_calls}
    measured.update(extra or {})
    return measured


async def run_arm(*, scenario, world, cache, ledger_path, task_id, guard, arm='baseline',
                  budget=None, daily_budget=None, plan_version='baseline-primary-keywords',
                  token=None, hook=None, queries=None) -> dict:
    """One measured retrieval: precheck, run, and every number it produced."""
    categories = tuple(scenario.categories)
    budget = scenario.task_budget if budget is None else budget
    daily_budget = scenario.daily_budget if daily_budget is None else daily_budget
    settings = benchmark_settings(daily_budget=daily_budget, ledger_path=ledger_path)
    quota = Quota(settings, clock=controlled_clock, ledger_path=ledger_path)
    task = quota.task_budget(isochrone=1, poi=budget)
    domain = QueryDomain.circle(DOMAIN_RADIUS_M)
    provider, api_version = SyntheticService.identity, SyntheticService.api_version
    record = {'arm': arm, 'planVersion': plan_version, 'scenario': scenario.key,
              'label': scenario.label, 'densityPerKm2': scenario.density_per_km2,
              'taskBudget': budget, 'dailyBudget': daily_budget,
              'tier': quota.tiers.active().label, 'day': quota.ledger.day(),
              'minorCategories': len(categories), 'categories': list(categories),
              'worldPlaces': len(world),
              'keywordsPerCategory': {c: len(RULES['queries'][c]) for c in categories}}

    # The stage's own precheck, reused rather than restated: it decides on the pages
    # the planner will really ask for, under the keys the cache really uses.
    plan = poi_plan.initial_plan(domain, ORIGIN, categories, provider=provider,
                                 api_version=api_version, queries=queries)
    estimate = poi_plan.estimate(plan, cache, task_id=task_id,
                                 remaining_task_budget=task.remaining(POI_POOL),
                                 remaining_daily_budget=quota.remaining(PLACE))
    record['initialPlan'] = estimate.as_contract()
    refusal = poi_plan.admission_refusal(estimate)
    record['admissionRefusal'] = refusal
    if refusal is not None:
        record.update(dispatched=0, pagesProcessed=0, taskBudgetDelta=0, dailyLedgerDelta=0,
                      status='refused', stopReason=refusal, schedule=[], keywordRuns=[],
                      coverage=None, latency=percentiles([]), checks={}, invariantsHold=True,
                      allowanceRefusals=0, retriedPages=0, sessionCalls=0, serviceCalls=0,
                      blocks=0, sequences=0, warnings=[],
                      deliveries={'live': 0, 'cache': 0, 'shared': 0, 'refused': 0})
        return record

    deadline = time.monotonic() + RUN_TIMEOUT_S
    service = SyntheticService(world, fault=scenario.fault)
    session = BenchmarkSession(service, quota.place, budget=task, deadline=deadline, hook=hook)
    fetch = CachedPages(cache, session, provider=service, task_id=task_id)
    planner = OnlinePlanner(domain=domain, origin=ORIGIN, categories=list(categories),
                            queries=poi_plan.primary_queries(categories) if queries is None else queries,
                            budget=task.remaining(POI_POOL), source='synthetic',
                            limits=RunLimits(processing_steps=PROCESSING_STEP_LIMIT,
                                             drain_after_budget_refusal=True),
                            token=token, deadline=deadline)
    ledger_before, spent_before = quota.ledger.spent(PLACE), task.spent.get(POI_POOL, 0)
    result = await planner.run(fetch)
    record.update(measure_result(result=result, fetch=fetch, session=session, quota=quota, task=task,
                                 ledger_before=ledger_before, spent_before=spent_before,
                                 world=world, domain=domain, categories=categories, guard=guard,
                                 service_calls=service.calls,
                                 # The planner keeps this count on itself rather than on the
                                 # result it returns, and it is the stage's own diagnostic:
                                 # pages the allowance refused, which cost nothing.
                                 extra={'allowanceRefusals': planner.budget_refusals}))
    return record


async def run_suite(scenarios, *, output: Path, seed: int, only: str | None) -> dict:
    guard = install(Guard())
    arms = []
    for scenario in scenarios:
        if only and scenario.key != only:
            continue
        categories = tuple(scenario.categories)
        world = build_world(half_meters=DOMAIN_RADIUS_M, origin=ORIGIN, categories=categories,
                            density_per_km2=scenario.density_per_km2, seed=seed,
                            extra_keyword_share=scenario.extra_keyword_share)
        cache = KeyedCache()
        task_id = f'benchmark:{scenario.key}'
        # One ledger per scenario: the controlled clock puts every scenario on the same
        # day, so a shared ledger would let one scenario's spend refuse the next one's
        # cold first round — which is not a property of any plan under test.
        ledger_path = output / f'ledger-{scenario.key}.sqlite3'
        if ledger_path.exists():
            ledger_path.unlink()
        # A prefill run shares the task id, because that is what "reusable in the same
        # task" means for the cache; it gets its own quota and ledger so the measured
        # run's spend is only its own.
        if scenario.prefilled:
            await run_arm(scenario=scenario, world=world, cache=cache, ledger_path=ledger_path,
                          task_id=task_id, guard=guard, arm='prefill',
                          budget=GENEROUS_BUDGET, daily_budget=GENEROUS_BUDGET)
        arms.append(await run_arm(scenario=scenario, world=world, cache=cache,
                                  ledger_path=ledger_path, task_id=task_id, guard=guard))
    return {'command': 'tools.poi_query_benchmark', 'suite': 'baseline', 'seed': seed,
            'origin': list(ORIGIN), 'domainRadiusMeters': DOMAIN_RADIUS_M,
            'processingStepLimit': PROCESSING_STEP_LIMIT, 'generousBudget': GENEROUS_BUDGET,
            'controlledInstant': CONTROLLED_INSTANT.isoformat(),
            'modelAssumptions': list(MODEL_ASSUMPTIONS), 'guard': guard.report(), 'arms': arms}


def _table(rows, headers) -> str:
    lines = ['| ' + ' | '.join(headers) + ' |',
             '| ' + ' | '.join('---' for _ in headers) + ' |']
    for row in rows:
        lines.append('| ' + ' | '.join(str(cell) for cell in row) + ' |')
    return '\n'.join(lines)


def summary_markdown(report: dict) -> str:
    """The human-readable half of the evidence; the JSON keeps the per-page detail."""
    arms = report['arms']
    cost = _table(
        [(arm['scenario'], arm['label'], arm['densityPerKm2'], arm['taskBudget'],
          arm['worldPlaces'], arm['pagesProcessed'], arm['dispatched'], arm['taskBudgetDelta'],
          arm['dailyLedgerDelta'], arm['allowanceRefusals'], arm['status'],
          arm['stopReason'] or '—') for arm in arms],
        ['场景', '说明', '密度/平方千米', '任务预算', '夹具设施数', '处理页数', '真实派发',
         '任务桶增量', '日账本增量', '额度拒绝页', '状态', '停止原因'])
    delivery = _table(
        [(arm['scenario'], arm['deliveries']['live'], arm['deliveries']['cache'],
          arm['deliveries']['shared'], arm['deliveries']['refused'], arm['retriedPages'],
          arm['latency']['count'], arm['latency']['p50'], arm['latency']['p95'],
          arm['latency']['max']) for arm in arms],
        ['场景', 'live', 'cache', 'shared', 'refused', '含重试页', '计时样本', 'p50 秒',
         'p95 秒', '最大 秒'])
    checks = _table(
        [(arm['scenario'], '✅' if arm['invariantsHold'] else '❌',
          ', '.join(name for name, ok in arm['checks'].items() if not ok) or '—') for arm in arms],
        ['场景', '计量恒等式', '未成立的检查'])
    majors = sorted({major for arm in arms if arm['coverage']
                     for major in arm['coverage']['perMajor']})
    coverage = _table(
        [(arm['scenario'], arm['coverage']['accepted'] if arm['coverage'] else 0,
          arm['coverage']['needsReview'] if arm['coverage'] else 0,
          arm['coverage']['excluded'] if arm['coverage'] else 0,
          arm['coverage']['outsideCountingRegion'] if arm['coverage'] else 0,
          arm['coverage']['duplicateRecordsMerged'] if arm['coverage'] else 0,
          arm['coverage']['catalogCompleteness'] if arm['coverage'] else '—',
          *(arm['coverage']['perMajor'].get(major, {}).get('acceptedUids', 0) if arm['coverage']
            else 0 for major in majors)) for arm in arms],
        ['场景', '接收 UID', '待复核', '排除', '区域外', '重复归并', '目录完整性',
         *[catalog.major_label(major) for major in majors]])
    return f"""# C0 基线基准（合成夹具）

- 命令：`cd backend && .venv/bin/python -m tools.poi_query_benchmark --suite baseline --output <目录>`
- 随机种子：`{report['seed']}`；几何中心：`{report['origin']}`；查询半径：`{report['domainRadiusMeters']}` 米
- 受控时刻（决定日账本的"今天"与生效档位）：`{report['controlledInstant']}`
- 本地处理步数上限：`{report['processingStepLimit']}`（与网络额度是两把尺子）
- 外连尝试：**{report['guard']['attempts']} 次**

## 1. 派发成本与状态

{cost}

## 2. 交付方式、重试与用时

{delivery}

> `live` 是真正发出的上游调用；`cache` 是读已存页面；`shared` 是等待同一次 in-flight 调用；
> `refused` 是派发前被额度拒绝、因此**没有**产生任何调用。`cache` 与 `shared` 都不计入预算。

## 3. 覆盖与语义一致性

{coverage}

## 4. 计量恒等式

{checks}

## 5. 夹具建模假设（离线结论的边界）

{chr(10).join(f'{index}. {item}' for index, item in enumerate(report['modelAssumptions'], 1))}

**这些数字不能支持**：真实百度目录的召回率、真实接口合并后的结果、任何"生产可用"结论。
夹具覆盖列只作为诊断，不是真实召回率。
"""


@dataclass(frozen=True)
class CompareCase(Scenario):
    """One world plus the plans to run over it. Every plan sees the same world.

    ``plans`` names the arms: ``base`` is the production primary-keyword plan, and the
    others are groupings built by ``tools.poi_benchmark_merge``. Each arm gets its own
    cache and its own task id, so no arm can read another's pages — which is also what
    the per-plan cache namespace enforces.
    """

    cancel_after: int | None = None
    plans: tuple = ('base', 'synonym')
    #: Share of a category's facilities reachable only through a *non-primary* keyword.
    #: Zero is this fixture's default because it is what the baseline plan assumes when
    #: it sends primary keywords only; the comparison needs it non-zero, or the extra
    #: keywords match nothing and merging them is a silent no-op. 0.4 is an explicit
    #: modelling assumption, not a measurement: it says "the synonyms in the dictionary
    #: are there because they find facilities the primary word does not".
    extra_keyword_share: float = 0.4


COMPARE_CASES = (
    CompareCase('sparse-cold', '稀疏 · 冷启动 · 额度充足', 300, GENEROUS_BUDGET,
                plans=('base', 'allkw', 'synonym', 'chunk3')),
    CompareCase('moderate-cold', '适中 · 冷启动 · 额度充足', 1200, GENEROUS_BUDGET,
                plans=('base', 'allkw', 'synonym', 'chunk2', 'chunk3', 'chunk5', 'chunk10')),
    CompareCase('dense-cold', '密集 · 冷启动 · 额度充足', 5000, GENEROUS_BUDGET,
                plans=('base', 'allkw', 'synonym')),
    CompareCase('dense-tight-60', '密集 · 紧预算 60', 5000, RECORDED_DEFAULT_BUDGET,
                plans=('base', 'allkw', 'synonym')),
    CompareCase('moderate-same-major', '适中 · 同大类装箱', 1200, GENEROUS_BUDGET,
                plans=('base', 'allkw', 'major3')),
    CompareCase('moderate-full-cache', '适中 · 全缓存重放（任务与日余额 0）', 1200, 0,
                daily_budget=0, prefilled=True, plans=('base', 'synonym')),
    CompareCase('moderate-no-match', '适中 · 服务答"无匹配"', 1200, GENEROUS_BUDGET,
                fault=Fault(results_mode='no_match'), plans=('base', 'synonym')),
    CompareCase('moderate-empty-with-total', '适中 · 空页但 total 为正', 1200, GENEROUS_BUDGET,
                fault=Fault(results_mode='empty_with_total'), plans=('base', 'synonym')),
    CompareCase('moderate-rate-limit', '适中 · 持续限流', 1200, GENEROUS_BUDGET,
                fault=Fault(reason='rate_limit'), plans=('base', 'synonym')),
    CompareCase('dense-quota', '密集 · 上游配额用尽', 5000, GENEROUS_BUDGET,
                fault=Fault(reason='quota'), plans=('base', 'synonym')),
    CompareCase('moderate-cancel-40', '适中 · 第 40 次派发后取消', 1200, GENEROUS_BUDGET,
                cancel_after=40, plans=('base', 'synonym')),
)


SUITES['compare'] = COMPARE_CASES


def plan_for(key: str, categories):
    """The grouping a plan key names; ``None`` is the independent-query baseline."""
    from tools.poi_benchmark_merge import chunked_plan, same_major_plan, synonym_plan

    if key == 'base':
        return None
    if key == 'synonym':
        return synonym_plan(categories)
    chunked = re.fullmatch(r'chunk(\d+)', key)
    if chunked:
        return chunked_plan(categories, int(chunked.group(1)))
    same_major = re.fullmatch(r'major(\d+)', key)
    if same_major:
        return same_major_plan(categories, int(same_major.group(1)))
    raise ValueError(f'unknown plan: {key}')


async def run_grouped_arm(*, case, plan, world, cache, ledger_path, task_id, guard,
                          budget, daily_budget, token=None, hook=None) -> dict:
    """One measured grouped run, costed by the same :func:`measure_result`.

    The admission rule is the production one, fed with the candidate's own first
    round: it would be a rigged comparison if the baseline were refused for a cold
    first round and the candidate were not.
    """
    from tools.poi_benchmark_merge import (IDENTITY, PLAN_VERSION, GroupedRunner,
                                          MergeService, attribution_report, initial_estimate)

    categories = tuple(case.categories)
    settings = benchmark_settings(daily_budget=daily_budget, ledger_path=ledger_path)
    quota = Quota(settings, clock=controlled_clock, ledger_path=ledger_path)
    task = quota.task_budget(isochrone=1, poi=budget)
    domain = QueryDomain.circle(DOMAIN_RADIUS_M)
    record = {'arm': plan.key, 'planVersion': PLAN_VERSION, 'scenario': case.key,
              'label': f'{case.label} · {plan.label}', 'densityPerKm2': case.density_per_km2,
              'taskBudget': budget, 'dailyBudget': daily_budget,
              'tier': quota.tiers.active().label, 'day': quota.ledger.day(),
              'minorCategories': len(categories), 'categories': list(categories),
              'worldPlaces': len(world), 'transportIdentity': IDENTITY,
              'extraKeywordShare': case.extra_keyword_share,
              'attribution': attribution_report(plan, categories)}
    estimate = initial_estimate(plan, domain, ORIGIN, provider=IDENTITY,
                                api_version=MergeService.api_version, cache=cache,
                                task_id=task_id,
                                remaining_task_budget=task.remaining(POI_POOL),
                                remaining_daily_budget=quota.remaining(PLACE))
    record['initialPlan'] = estimate.as_contract()
    refusal = poi_plan.admission_refusal(estimate)
    record['admissionRefusal'] = refusal
    if refusal is not None:
        record.update(dispatched=0, pagesProcessed=0, taskBudgetDelta=0, dailyLedgerDelta=0,
                      status='refused', stopReason=refusal, schedule=[], keywordRuns=[],
                      coverage=None, latency=percentiles([]), checks={}, invariantsHold=True,
                      allowanceRefusals=0, retriedPages=0, sessionCalls=0, serviceCalls=0,
                      blocks=0, sequences=0, warnings=[],
                      deliveries={'live': 0, 'cache': 0, 'shared': 0, 'refused': 0})
        return record
    deadline = time.monotonic() + RUN_TIMEOUT_S
    service = MergeService(world, fault=case.fault)
    session = BenchmarkSession(service, quota.place, budget=task, deadline=deadline, hook=hook)
    fetch = CachedPages(cache, session, provider=service, task_id=task_id)
    runner = GroupedRunner(domain=domain, origin=ORIGIN, plan=plan, source='synthetic',
                           token=token, deadline=deadline)
    ledger_before, spent_before = quota.ledger.spent(PLACE), task.spent.get(POI_POOL, 0)
    result = await runner.run(fetch)
    record.update(measure_result(
        result=result, fetch=fetch, session=session, quota=quota, task=task,
        ledger_before=ledger_before, spent_before=spent_before, world=world, domain=domain,
        categories=categories, guard=guard, service_calls=service.calls,
        extra={'groups': len(plan.groups), 'groupSizes': plan.group_sizes}))
    return record


async def run_compare_suite(cases, *, output: Path, seed: int, only_case: str | None,
                            only_plan: str | None) -> dict:
    from tools.poi_benchmark_merge import PLAN_VERSION, compare_arms

    guard = install(Guard())
    output.mkdir(parents=True, exist_ok=True)
    results = []
    for case in cases:
        if only_case and case.key != only_case:
            continue
        categories = tuple(case.categories)
        world = build_world(half_meters=DOMAIN_RADIUS_M, origin=ORIGIN, categories=categories,
                            density_per_km2=case.density_per_km2, seed=seed,
                            extra_keyword_share=case.extra_keyword_share)
        arms = []
        for plan_key in case.plans:
            if only_plan and plan_key != only_plan:
                continue
            # ``allkw`` is the fair equal-coverage comparison: the *production* planner
            # with every keyword of every category sent as its own query. Without it,
            # a merged arm would look better or worse only because it sends more
            # keywords than the primary-keyword baseline does, which is a coverage
            # change rather than a query-plan change.
            all_keywords = plan_key == 'allkw'
            plan = None if all_keywords else plan_for(plan_key, categories)
            queries = ({category: tuple(RULES['queries'][category]) for category in categories}
                       if all_keywords else None)
            plan_version = ('baseline-all-keywords' if all_keywords
                            else 'baseline-primary-keywords')
            cache = KeyedCache()
            task_id = f'benchmark:{case.key}:{plan_key}'
            # One ledger per (case, plan): the arms must be independent measurements,
            # and the clock pins them all to the same day.
            ledger_path = output / f'ledger-{case.key}-{plan_key}.sqlite3'
            if ledger_path.exists():
                ledger_path.unlink()

            async def execute(*, budget, daily_budget, label, token=None, hook=None):
                if plan is None:
                    return await run_arm(scenario=case, world=world, cache=cache,
                                         ledger_path=ledger_path, task_id=task_id, guard=guard,
                                         arm=label, budget=budget, daily_budget=daily_budget,
                                         token=token, hook=hook, queries=queries,
                                         plan_version=plan_version)
                return await run_grouped_arm(case=case, plan=plan, world=world, cache=cache,
                                             ledger_path=ledger_path, task_id=task_id, guard=guard,
                                             budget=budget, daily_budget=daily_budget,
                                             token=token, hook=hook)

            if case.prefilled:
                # Each plan is pre-filled under its own identity, then measured with the
                # day and the task bucket at zero: a replay must cost nothing.
                await execute(budget=GENEROUS_BUDGET, daily_budget=GENEROUS_BUDGET,
                              label='prefill')
            token = CancelToken() if case.cancel_after else None

            def hook(count, token=token, limit=case.cancel_after):
                if token is not None and count >= limit:
                    token.cancel()

            arms.append(await execute(budget=case.task_budget, daily_budget=case.daily_budget,
                                      label=plan_key, token=token,
                                      hook=hook if case.cancel_after else None))
        baseline = next((item for item in arms if item['arm'] == 'base'), None)
        for arm in arms:
            if baseline is not None and arm.get('coverage') and baseline.get('coverage'):
                arm['vsBaseline'] = compare_arms(baseline, arm)
        results.append({'case': case.key, 'label': case.label,
                        'densityPerKm2': case.density_per_km2,
                        'world': world.composition(), 'arms': arms})
    return {'command': 'tools.poi_query_benchmark', 'suite': 'compare', 'seed': seed,
            'origin': list(ORIGIN), 'domainRadiusMeters': DOMAIN_RADIUS_M,
            'processingStepLimit': PROCESSING_STEP_LIMIT, 'generousBudget': GENEROUS_BUDGET,
            'controlledInstant': CONTROLLED_INSTANT.isoformat(), 'planVersion': PLAN_VERSION,
            'modelAssumptions': list(MODEL_ASSUMPTIONS), 'guard': guard.report(),
            'cases': results}


def compare_summary_markdown(report: dict) -> str:
    """The C2 comparison: whole-run cost next to relative recall, per arm."""
    rows, recall_rows, attribution_rows, check_rows = [], [], [], []
    for case in report['cases']:
        for arm in case['arms']:
            rows.append((case['case'], arm['arm'], arm['taskBudget'], arm['dispatched'],
                         arm['pagesProcessed'], arm['taskBudgetDelta'], arm['dailyLedgerDelta'],
                         arm['allowanceRefusals'], arm['status'], arm['stopReason'] or '—'))
            checks = arm.get('checks') or {}
            check_rows.append((case['case'], arm['arm'],
                               '✅' if arm['invariantsHold'] else '❌',
                               ', '.join(name for name, ok in checks.items() if not ok) or '—'))
            if 'vsBaseline' in arm:
                diff = arm['vsBaseline']
                worst = sorted((item['missingCount'], category)
                               for category, item in diff['perMinor'].items() if item['missingCount'])
                recall_rows.append((case['case'], arm['arm'], diff['baselineUids'],
                                    diff['missingCount'], diff['addedCount'],
                                    f"{diff['relativeRecall']:.2%}" if diff['relativeRecall'] is not None else '—',
                                    '、'.join(f'{category}({count})' for count, category in worst[:4]) or '—'))
            if arm.get('attribution'):
                attribution = arm['attribution']
                attribution_rows.append((case['case'], arm['arm'], attribution['granularity'],
                                         attribution['groups'], attribution['crossCategoryGroups'],
                                         attribution['memberKeywords'],
                                         attribution['keywordsWithoutOwnCompleteness']))
    return f"""# C2 隔离 PoC：独立查询 vs 分组关键词计划

- 命令：`cd backend && .venv/bin/python -m tools.poi_query_benchmark --suite compare --output <目录>`
- 计划版本：`{report['planVersion']}`（分组臂使用独立 transport 身份，因此页面缓存键与生产互不相通）
- 随机种子：`{report['seed']}`；几何中心：`{report['origin']}`；查询半径：`{report['domainRadiusMeters']}` 米
- 受控时刻：`{report['controlledInstant']}`；本地处理步数上限：`{report['processingStepLimit']}`
- 外连尝试：**{report['guard']['attempts']} 次**

## 1. 整轮成本（不是首轮请求数）

{_table(rows, ['场景', '臂', '任务预算', '真实派发', '处理页数', '任务桶增量', '日账本增量', '额度拒绝页', '状态', '停止原因'])}

> `base` 是生产的"每类只发主关键词"计划；`synonym` 是每小类一组；`chunkN` 是每组 N 词的机械跨类装箱；
> `majorN` 是同大类内装箱。分组臂的派发数**包含细分带来的追加查询**，这正是"只数首轮"会看错的地方。

## 2. 相对独立查询基线的召回

{_table(recall_rows, ['场景', '臂', '基线 UID 数', '漏失', '新增', '相对召回', '漏失最多的小类'])}

> 这是**相对**召回：基线本身不是任何真实目录的地面真值，所以"漏失"只说明该计划找到的设施比独立
> 查询少，**不**说明它漏掉了真实设施。`新增` 通常来自分组改变了分页顺序，不代表更好的召回。

## 3. 归属损失

{_table(attribution_rows, ['场景', '臂', '合并粒度', '组数', '跨类组数', '参与合并的关键词', '失去自身完整性断言的关键词'])}

> 每一行都表示：这些关键词的（块, 小类, 关键词）级完整性**无法再由并集页断言**。按 `scheme.md`
> §6 C3，它们只能记为 `unknown/incomplete`，需要结论时必须追加独立查询（另行消耗额度）。

## 4. 计量恒等式

{_table(check_rows, ['场景', '臂', '计量恒等式', '未成立的检查'])}

## 5. 结论边界

{chr(10).join(f'{index}. {item}' for index, item in enumerate(report['modelAssumptions'], 1))}

**本文件不能支持**：真实百度接口的合并召回、上线收益承诺、任何"质量已达到可用水平"的结论。
依 `scheme.md` §6 C2，离线夹具只能给出"技术方案可试验 / 不可试验"。
"""


async def main_async(args) -> int:
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    if args.suite == 'compare':
        report = await run_compare_suite(SUITES['compare'], output=output, seed=args.seed,
                                         only_case=args.scenario, only_plan=args.plan)
        markdown = compare_summary_markdown(report)
        units = len(report['cases'])
        failures = [f"{case['case']}/{arm['arm']}" for case in report['cases']
                    for arm in case['arms'] if not arm['invariantsHold']]
    else:
        report = await run_suite(SUITES['baseline'], output=output, seed=args.seed,
                                 only=args.scenario)
        markdown = summary_markdown(report)
        units = len(report['arms'])
        failures = [arm['scenario'] for arm in report['arms'] if not arm['invariantsHold']]
    report['seconds'] = round(time.perf_counter() - started, 3)
    detail = output / f'{args.suite}-detail.json'
    detail.write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str),
                      encoding='utf-8')
    (output / f'{args.suite}-summary.md').write_text(markdown, encoding='utf-8')
    print(f'{args.suite}: {units} unit(s) in {report["seconds"]}s → {detail}')
    print(f'external connection attempts: {report["guard"]["attempts"]}')
    if failures:
        print(f'invariant failures: {", ".join(failures)}', file=sys.stderr)
        return 1
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--suite', choices=sorted(SUITES), default='baseline')
    parser.add_argument('--output', default='../.tmp/poi-benchmark',
                        help='产物目录（默认 ../.tmp/poi-benchmark，仓库忽略）')
    parser.add_argument('--seed', type=int, default=SEED)
    parser.add_argument('--scenario', help='只运行一个场景（compare 套件下为 case 名），便于定位')
    parser.add_argument('--plan', help='compare 套件下只运行一个臂，便于定位')
    return asyncio.run(main_async(parser.parse_args(argv)))


if __name__ == '__main__':
    raise SystemExit(main())
