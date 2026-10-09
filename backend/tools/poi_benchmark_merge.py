"""C2: an experimental grouped-keyword plan, deliberately outside the live path.

`OnlinePlanner` sends one keyword per (block, category) query. This module sends one
query per *group* of keywords joined with ``$``, and measures what that costs and what
it loses. It is an experiment: nothing here is reachable from the application, the
production planner and its cache keys are untouched, and the arm declares its own
``planVersion`` and transport identity so no page it caches can ever be read as an
independent-query page.

Three constraints come straight from the C1 audit and are enforced in code rather than
left to a reader's judgement:

* **Only keywords of one minor category may share a group.** Category attribution
  survives merging (``classify`` reads the facility's own name and tag), but the
  *completeness* evidence does not: ``OnlinePlanner._covered`` is keyed by
  (block, category, keyword), and a union page cannot assert it for any single
  keyword. Grouping within one category keeps the (block, category) unit definable;
  grouping across categories destroys it for both. Cross-category plans are still
  built and measured here, because the C2 matrix asks for that comparison and the
  cost of the unsafe option is exactly what needs measuring.
* **A merged page never marks its member keywords covered.** Members are reported
  ``unknown`` and their UIDs are credited to the group, not to a keyword.
* **Charging stays where it is.** Every page still goes through
  ``ServicePool.attempt()``; the runner adds no second accounting path and no way to
  fund a call outside the task bucket, the daily ledger and the deadline.

What it cannot do is stated in ``poi_query_benchmark``'s report next to the numbers:
an offline fixture can show that the implementation and its accounting are
self-consistent, and can show which ways merging is *worse*. It cannot show that a
real union query returns the same facilities as separate queries, because the
documentation never claims that and no live call was authorised.
"""
import asyncio
import time
from collections import deque
from dataclasses import dataclass, field

import httpx

from app import catalog
from app.cache import SharedBuildFailed
from app.poi.online import (FATAL, MAX_PAGE_ATTEMPTS, MAX_PAGES, PROCESSING_LIMIT_REACHED,
                           PROCESSING_STEP_LIMIT, RETRYABLE, THROTTLING, BUDGET_REFUSALS,
                           DENSITY_SIGNALS, LIVE, REFUSED, SHARED, Observation, PageResponse,
                           QueryBlock, QueryDomain, coarse_blocks, sequence)
from app.poi.planner import RULES
from app.quota import QuotaError
from app.request_control import RequestStopped
from life_circle.coordinates import LocalProjection

from tools.poi_benchmark_world import SyntheticService

#: The experiment's own plan version. It is part of the transport identity, and the
#: transport identity is part of every page cache key, so an experimental page can
#: never be served to — or by — the independent-query plan.
PLAN_VERSION = 'merge-poc-v1'
IDENTITY = f'synthetic:poi-benchmark-{PLAN_VERSION}'


class MergeService(SyntheticService):
    """The same world, under the experiment's own cache namespace."""

    identity = IDENTITY
    api_version = f'3.0+{PLAN_VERSION}'


@dataclass(frozen=True)
class GroupPlan:
    """One way of packing keywords into queries."""

    key: str
    label: str
    granularity: str            # 'synonym' | 'same-major' | 'cross-major'
    groups: tuple               # tuple of tuple of (category, keyword)

    @property
    def group_sizes(self) -> dict:
        counts: dict[int, int] = {}
        for group in self.groups:
            counts[len(group)] = counts.get(len(group), 0) + 1
        return dict(sorted(counts.items()))

    @property
    def cross_category_groups(self) -> int:
        return sum(1 for group in self.groups if len({c for c, _ in group}) > 1)

    @property
    def member_keywords(self) -> int:
        return sum(len(group) for group in self.groups)


def synonym_plan(categories) -> GroupPlan:
    """One group per minor category: every keyword of that category, merged.

    The lowest-risk packing the C1 audit allows, because the (block, category) unit
    that carries completeness stays intact.
    """
    groups = tuple(tuple((category, keyword) for keyword in RULES['queries'][category])
                   for category in categories)
    return GroupPlan('synonym', '同义词组（每小类一组）', 'synonym', groups)


def chunked_plan(categories, size: int) -> GroupPlan:
    """Mechanical packing of *size* keywords in dictionary order, crossing categories.

    This is the packing `scheme.md` §6 C1 warns against: it is included so its cost
    and its attribution loss can be measured rather than asserted.
    """
    flat = [(category, keyword) for category in categories
            for keyword in RULES['queries'][category]]
    groups = tuple(tuple(flat[start:start + size]) for start in range(0, len(flat), size))
    return GroupPlan(f'chunk{size}', f'机械装箱（每组 {size} 词，跨类）', 'cross-major', groups)


def same_major_plan(categories, size: int) -> GroupPlan:
    """Pack within one major category, never across it."""
    groups = []
    for major in dict.fromkeys(catalog.major_of(category) or category for category in categories):
        flat = [(category, keyword) for category in categories
                if (catalog.major_of(category) or category) == major
                for keyword in RULES['queries'][category]]
        groups.extend(tuple(flat[start:start + size]) for start in range(0, len(flat), size))
    return GroupPlan(f'major{size}', f'同大类装箱（每组 {size} 词）', 'same-major', tuple(groups))


@dataclass
class _GroupedQuery:
    """One (block, group) query and what it established."""

    block: QueryBlock
    mapping: dict
    group: tuple
    page: int = 0
    page_attempts: int = 0
    status: str = 'pending'
    stop: str | None = None
    pages: list = field(default_factory=list)
    pagination: object = None

    def entry(self, source: str) -> dict:
        succeeded = [record for record in self.pages if record['succeeded']]
        return {**self.mapping, 'source': source, 'status': self.status,
                'pages': len(succeeded),
                'returned': sum(record['returned'] for record in succeeded),
                'total': succeeded[-1]['total'] if succeeded else None,
                'truncated': self.stop in ('possible_truncation', 'page_limit'),
                'warnings': sorted({w for record in self.pages for w in record['warnings']}),
                'stopReason': self.stop,
                'requestedPages': [record['pageNum'] for record in self.pages],
                'successfulPages': [record['pageNum'] for record in succeeded],
                'reportedTotals': [{'pageNum': record['pageNum'], 'total': record['total']}
                                   for record in succeeded],
                'pageErrors': [{'pageNum': record['pageNum'], 'reason': record['reason']}
                               for record in self.pages if not record['succeeded']],
                'pageRecords': [dict(record) for record in self.pages]}


@dataclass(frozen=True)
class GroupedResult:
    """The shape the shared metering reads, so both arms are measured identically."""

    status: str
    coverage: list
    observations: list
    attempts: int
    network_calls: int
    stop_reason: str | None
    warnings: list
    blocks: tuple
    budget_refusals: int = 0
    incomplete: dict = field(default_factory=dict)

    @property
    def catalog_completeness(self) -> str:
        return 'unverified'


class GroupedRunner:
    """The experimental scheduler: same rules as the planner, different grouping.

    Retry policy, the fatal/throttling distinction, the drain-after-budget-refusal
    behaviour, the local processing ceiling and the subdivision trigger are mirrored
    from ``OnlinePlanner`` on purpose: a comparison in which the candidate is allowed
    to stop earlier, or to skip the subdivision the baseline pays for, would measure
    the difference between two policies rather than between two groupings.
    """

    def __init__(self, *, domain, origin, plan: GroupPlan, source='synthetic', token=None,
                 deadline=None, step_limit=PROCESSING_STEP_LIMIT, max_pages=MAX_PAGES):
        self.domain, self.origin, self.plan = domain, origin, plan
        self.source, self.token, self.deadline = source, token, deadline
        self.step_limit, self.max_pages = step_limit, max_pages
        self.projection = LocalProjection((origin[0], origin[1]))
        self.coarse = coarse_blocks(domain)
        if not self.coarse:
            raise ValueError('empty_query_domain')
        self.blocks = {block.tile_id: block for block in self.coarse}
        self.children: dict[str, tuple] = {}
        self.queries: dict[str, _GroupedQuery] = {}
        self.queue = deque()
        self.observations: list = []
        self.attempts = 0
        self.network_calls = 0
        self.budget_refusals = 0
        self.stopped: str | None = None
        self.warnings: list = []
        for block in self.coarse:
            for index, group in enumerate(plan.groups):
                self._enqueue(block, group, index)

    def _enqueue(self, block, group, index):
        keywords = [keyword for _, keyword in group]
        query = '$'.join(keywords)
        category = group[0][0] if len({c for c, _ in group}) == 1 else f"group{index}"
        key = (block.tile_id, query)
        if key in self.queries:
            return None
        from app.place_protocol import Pagination
        state = _GroupedQuery(block=block, group=group, pagination=Pagination(),
                              mapping=sequence(block.tile_id, block.x, block.y, block.edge,
                                               category, query, self.projection))
        self.queries[key] = state
        self.queue.append(state)
        return state

    async def _resolve(self, fetch, mapping, page) -> PageResponse:
        """Mirrors ``OnlinePlanner._attempt``: who really paid for this page.

        Kept as its own copy rather than calling the private method, because this is an
        experiment that must not change the production planner to accommodate it. Any
        divergence between the two is a defect in this file.
        """
        try:
            answer = await fetch(mapping, page)
        except SharedBuildFailed as shared:
            if isinstance(shared.cause, RequestStopped):
                return PageResponse(None, shared.cause.reason, SHARED)
            if isinstance(shared.cause, QuotaError):
                return PageResponse(None, shared.cause.code, SHARED)
            if isinstance(shared.cause, httpx.TimeoutException):
                return PageResponse(None, 'timeout', SHARED)
            if isinstance(shared.cause, httpx.RequestError):
                return PageResponse(None, 'network_error', SHARED)
            raise shared.cause from None
        except RequestStopped as exc:
            return PageResponse(None, exc.reason, REFUSED)
        except QuotaError as exc:
            return PageResponse(None, exc.code, REFUSED)
        except httpx.TimeoutException:
            return PageResponse(None, 'timeout', LIVE)
        except httpx.RequestError:
            return PageResponse(None, 'network_error', LIVE)
        if isinstance(answer, PageResponse):
            return answer
        payload, reason = answer
        return PageResponse(payload, reason, LIVE)

    def _finish(self, state, stop):
        if state.status not in ('pending', 'more'):
            return
        state.stop = stop
        state.status = 'completed' if stop == 'completed' else (
            'partial' if state.pagination.pages else 'failed')
        if stop in DENSITY_SIGNALS:
            self._subdivide(state)

    def _subdivide(self, state):
        children = state.block.children()
        if not children:
            self.warnings.append('finest_block_saturated')
            return
        self.children[state.block.tile_id] = children
        index = self.plan.groups.index(state.group)
        for child in children:
            self.blocks.setdefault(child.tile_id, child)
            self._enqueue(child, state.group, index)

    async def _step(self, fetch, state):
        page = state.page
        state.page_attempts += 1
        self.attempts += 1
        response = await self._resolve(fetch, state.mapping, page)
        if response.dispatched:
            self.network_calls += 1
        payload, reason = response.payload, response.reason
        record = {'tileId': state.block.tile_id, 'category': state.mapping['category'],
                  'query': state.mapping['query'], 'pageNum': page, 'requested': True,
                  'succeeded': payload is not None, 'source': self.source, 'reason': reason,
                  'total': None, 'returned': 0, 'truncated': False, 'warnings': []}
        state.pages.append(record)
        if payload is None:
            drain = reason in BUDGET_REFUSALS
            if reason in FATAL and not drain:
                self.stopped = reason
            if drain:
                self.budget_refusals += 1
            if (reason in RETRYABLE and state.page_attempts < MAX_PAGE_ATTEMPTS
                    and self.attempts < self.step_limit and self.stopped is None):
                if reason in THROTTLING:
                    self.queue.appendleft(state)
                else:
                    self.queue.append(state)
                return
            if reason in THROTTLING and self.stopped is None:
                self.stopped = reason
            self._finish(state, reason)
            return
        before = len(state.pagination.warnings)
        stop = state.pagination.consume(payload, self.max_pages)
        added = state.pagination.warnings[before:]
        record.update(total=state.pagination.total, returned=len(payload['results']),
                      warnings=sorted(added), truncated='possible_truncation' in added)
        provenance = {'tileId': state.block.tile_id, 'category': state.mapping['category'],
                      'sequenceId': state.mapping['sequenceId'], 'query': state.mapping['query'],
                      'pageNum': page}
        self.observations.extend(Observation(provenance, row) for row in payload['results'])
        state.page_attempts = 0
        state.page = page + 1
        if stop is None:
            state.status = 'more'
            self.queue.append(state)
            return
        self._finish(state, stop)

    async def run(self, fetch) -> GroupedResult:
        try:
            while self.queue and self.stopped is None:
                await asyncio.sleep(0)
                if self.token is not None and self.token.cancelled:
                    self.stopped = 'cancelled'
                    break
                if self.deadline is not None and time.monotonic() >= self.deadline:
                    self.stopped = 'deadline_reached'
                    break
                state = self.queue.popleft()
                if state.status not in ('pending', 'more'):
                    continue
                if self.attempts >= self.step_limit:
                    self.stopped = PROCESSING_LIMIT_REACHED
                    break
                await self._step(fetch, state)
        except asyncio.CancelledError:
            if self.token is not None:
                self.token.cancel()
            self.stopped = 'cancelled'
        if self.stopped is None and self.budget_refusals:
            self.stopped = 'network_budget_exhausted'
        return self.result()

    def _covered(self, state) -> bool:
        return state.status == 'completed'

    def result(self) -> GroupedResult:
        for state in self.queries.values():
            if state.status in ('pending', 'more'):
                self._finish(state, self.stopped or 'unfinished')
        coverage = [state.entry(self.source) for state in self.queries.values()]
        attempted = any(state.pagination.pages for state in self.queries.values())
        covered = all(self._covered(state) for state in self.queries.values())
        if self.token is not None and self.token.cancelled:
            status = 'cancelled'
        elif covered:
            status = 'completed'
        else:
            status = 'partial' if attempted else 'failed'
        warnings = sorted(set(self.warnings)
                          | {state.stop for state in self.queries.values()
                             if state.stop not in (None, 'completed')}
                          | ({self.stopped} if self.stopped else set()))
        return GroupedResult(status=status, coverage=coverage, observations=list(self.observations),
                             attempts=self.attempts, network_calls=self.network_calls,
                             stop_reason=self.stopped, warnings=warnings,
                             blocks=tuple(self.blocks.values()), budget_refusals=self.budget_refusals,
                             incomplete=self.incomplete_blocks())

    def incomplete_blocks(self) -> dict:
        """Per minor category, where the *group* covering it did not finish.

        A group spanning several categories marks every one of them incomplete: the
        page cannot say which member keyword's area was covered, so none of them may
        be read as finished.
        """
        marks: dict[str, list] = {}
        for state in self.queries.values():
            if state.status == 'completed':
                continue
            for category, _ in state.group:
                marks.setdefault(category, [])
                if state.block.bounds not in marks[category]:
                    marks[category].append(state.block.bounds)
        return {category: sorted(bounds) for category, bounds in sorted(marks.items())}


def attribution_report(plan: GroupPlan, categories) -> dict:
    """What this grouping makes unverifiable, said in numbers rather than adjectives.

    Every member keyword of every merged group loses its own completeness assertion.
    The count is the size of that loss, and it is why the conservative fallback in the
    C1 audit is an independent follow-up query for any keyword that needs one.
    """
    per_category: dict[str, int] = {}
    for group in plan.groups:
        for category, keyword in group:
            per_category.setdefault(category, 0)
            if len(group) > 1:
                per_category[category] += 1
    return {
        'granularity': plan.granularity,
        'groups': len(plan.groups),
        'groupSizes': plan.group_sizes,
        'crossCategoryGroups': plan.cross_category_groups,
        'memberKeywords': plan.member_keywords,
        'keywordsWithoutOwnCompleteness': sum(per_category.values()),
        'perCategoryUnverifiedKeywords': dict(sorted(per_category.items())),
        'categoriesRequested': len(tuple(categories)),
    }


def compare_arms(baseline: dict, arm: dict) -> dict:
    """Relative recall against the independent-query baseline's own UID set.

    This is the comparison §6 C0 asks for and the only recall figure an offline run
    may state. It is *relative*: the baseline is not ground truth about any real
    directory, so a shortfall here means "this plan found fewer of the facilities the
    independent plan found", never "this plan misses real facilities".
    """
    base = {category: set(item['uids'])
            for category, item in (baseline['coverage'] or {}).get('perMinor', {}).items()}
    other = {category: set(item['uids'])
             for category, item in (arm['coverage'] or {}).get('perMinor', {}).items()}
    per_minor, missing_total, added_total, base_total = {}, 0, 0, 0
    for category in sorted(set(base) | set(other)):
        expected, actual = base.get(category, set()), other.get(category, set())
        missing, added = sorted(expected - actual), sorted(actual - expected)
        missing_total += len(missing)
        added_total += len(added)
        base_total += len(expected)
        per_minor[category] = {'baselineUids': len(expected), 'armUids': len(actual),
                              'missing': missing, 'added': added,
                              'missingCount': len(missing), 'addedCount': len(added)}
    per_major: dict[str, dict] = {}
    for category, item in per_minor.items():
        major = (baseline['coverage'] or {}).get('perMinor', {}).get(category, {}).get('major', category)
        bucket = per_major.setdefault(major, {'baselineUids': 0, 'armUids': 0,
                                             'missingCount': 0, 'addedCount': 0})
        for field_name in ('baselineUids', 'armUids', 'missingCount', 'addedCount'):
            bucket[field_name] += item[field_name]
    return {'perMinor': per_minor, 'perMajor': per_major,
            'baselineUids': base_total, 'missingCount': missing_total, 'addedCount': added_total,
            'relativeRecall': (round((base_total - missing_total) / base_total, 4)
                               if base_total else None)}
