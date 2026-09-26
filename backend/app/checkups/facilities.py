"""§4.2–§4.4: one facility retrieval over the computed boundary.

The pieces already exist and are only composed here: the adaptive planner
chooses the queries, the place session meters them, the cache sits outside the
quota pool, and the POI pipeline normalizes, classifies and merges what comes
back. What this module adds is the composition and the reporting.

Three properties are the point of it:

* **The counting region is the boundary.** Rows are clipped to the query range
  (an ``around`` search is a superset) and then counted where the computed
  boundary says they are, not where the search range does. A facility just
  outside the boundary is kept as evidence, never as a finding.
* **Failure is never a zero.** A page that failed, a stage that could not build a
  transport or a boundary that is no region all leave a named reason; an empty
  group is only ever the result of a run that actually happened.
* **The two quality axes stay apart.** ``queryStatus`` says what the queries
  established, ``catalogCompleteness`` is ``unverified`` because a keyword search
  cannot verify a real directory — no count here is a completeness claim.
"""
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field

from life_circle.coordinates import LocalProjection, normalize as normalize_point
from shapely.geometry import Point

from .. import catalog
from ..cache import KeyedCache
from ..contracts import Issue
from ..poi.cache import CachedPages
from ..poi.models import PoiCollectRequest, Point as WirePoint
from ..poi.normalize import merge_entities, normalize
from ..poi.online import OnlinePlanner, clip_to_domain
from .facility_stage import local_region, query_domain
from .models import QUERY_PADDING_M, CheckupRequest, FacilityGroup
from .places import POI_POOL, PlacesUnavailable, open_online

# Budgets the request itself cannot raise (``models.MAX_POI_REQUESTS``), which is
# why the stage reads its limit from the request object rather than from here.

# One page's own outcome, as the planner records it, plus where its bytes came
# from. The page's ``source`` stays the data source of the run; the cache's view
# of the same page is a different question and gets its own key.
FETCH_SOURCE = 'fetchSource'


@dataclass(frozen=True)
class FacilityOutcome:
    """What the stage established, and what the revision must say about it.

    ``requests`` counts the pages the planner attempted, ``network_requests`` the
    upstream attempts they cost: a page served by the cache is one of the first
    and not one of the second, and a retry is one of both.
    """
    group: FacilityGroup | None
    status: str
    requests: int = 0
    network_requests: int = 0
    issues: list[Issue] = field(default_factory=list)


def _refusal(reason: str, major_categories, *, status: str = 'failed') -> FacilityOutcome:
    """A stage that could not run: null group, a named reason, no counts."""
    messages = {
        'missing_ak': '未配置百度地图 AK，设施检索不可用。',
        'task_budget_exhausted': '本任务的 POI 预算已用尽，设施检索未开始。',
        'no_boundary_geometry': '成圈结果没有几何，设施检索没有可确定的范围。',
        'empty_boundary_geometry': '成圈几何不是可用区域，设施检索没有可确定的范围。',
        'invalid_boundary_geometry': '成圈几何无法解析，设施检索没有可确定的范围。',
        'empty_query_domain': '查询范围为空，设施检索未开始。',
    }
    # A reason the engine sent up is reported as itself, not as a missing message.
    message = messages.get(reason, f'设施检索不可用：{reason}')
    return FacilityOutcome(group=None, status=status, issues=[
        Issue(code="FACILITIES_UNAVAILABLE", message=message, scope="facilities",
              severity="error")])


def _status_for(query_status: str) -> str:
    """The snapshot's ``facilitiesStatus``.

    It describes the *query* axis: a completed retrieval is complete as a
    retrieval, which is not a claim that the catalogue is complete. A cancelled
    stage keeps what it retrieved, so it is partial, never a failure.
    """
    return {'completed': 'complete', 'cancelled': 'partial'}.get(query_status, query_status)


async def collect_facilities(payload: CheckupRequest, snapshot, *, settings, context, quota,
                             budget, cache: KeyedCache, places_factory=None) -> FacilityOutcome:
    """Run the one facility retrieval of a task and report it.

    ``places_factory`` substitutes the transport for an offline or fixture-backed
    run; without it the deployment's own key and client are used, or the stage is
    refused by name.
    """
    origin = normalize_point((payload.center.lng, payload.center.lat))
    majors = tuple(payload.facilities.categories)
    categories = catalog.poi_keys(majors)
    if snapshot.geometry is None:
        return _refusal('no_boundary_geometry', majors)
    try:
        domain, widened = query_domain(snapshot.geometry, origin, QUERY_PADDING_M)
        boundary = local_region(snapshot.geometry, origin)
    except ValueError as exc:
        return _refusal(str(exc), majors)
    if budget.remaining(POI_POOL) <= 0:
        return _refusal('task_budget_exhausted', majors)
    started = time.time()
    # What this stage sends is what it reserves: the pool counts the attempt
    # before the request goes out, so a refused or unclear one is part of it.
    reserved = budget.spent.get(POI_POOL, 0)
    async with AsyncExitStack() as stack:
        try:
            places = (places_factory(settings) if places_factory is not None
                      else await open_online(settings, stack))
        except PlacesUnavailable as exc:
            return _refusal(exc.reason, majors)
        source = 'baidu_place' if places.network else 'synthetic'
        # The cache wraps the metered session, so a hit never enters the pool's
        # scheduling point and §9.2's single reservation stays the only one.
        fetch = CachedPages(cache, places.session(quota.place, budget=budget,
                                                  deadline=context.deadline),
                            provider=places, task_id=context.task_id)
        planner = OnlinePlanner(domain=domain, origin=origin, categories=list(categories),
                                budget=budget.remaining(POI_POOL), source=source,
                                token=context.token)
        result = await planner.run(fetch)
    return _report(payload, snapshot, result, fetch, domain, widened, boundary, origin,
                   places, source, majors, budget, started,
                   network=budget.spent.get(POI_POOL, 0) - reserved)


def _report(payload, snapshot, result, fetch, domain, widened, boundary, origin, places,
            source, majors, budget, started, *, network: int) -> FacilityOutcome:
    categories = catalog.poi_keys(majors)
    records, quarantine = [], []
    for observation in result.observations:
        try:
            records.append(normalize(observation.row, observation.provenance, source))
        except ValueError as exc:
            quarantine.append({'reason': str(exc), 'provenance': observation.provenance})
    inside, outside_domain = clip_to_domain(records, domain, origin)
    projection = LocalProjection(origin)
    request = PoiCollectRequest(center=WirePoint(lng=float(payload.center.lng),
                                                  lat=float(payload.center.lat)),
                                coordinate_system='bd09ll', categories=list(categories))
    # ``within`` is the boundary, in metres, inclusive of its edge: a facility on
    # the boundary is inside the area being reported on.
    def counted(point):
        return boundary.covers(Point(*projection.to_local((point['lng'], point['lat']))))

    accepted, review, excluded, outside_boundary, merged = merge_entities(
        inside, request, within=counted)
    coverage = result.coverage
    for entry in coverage:
        for record in entry['pageRecords']:
            use = fetch.uses.get((record['tileId'], record['category'], record['query'],
                                  record['pageNum']))
            if use is not None:
                record[FETCH_SOURCE], record['obtainedAt'] = use['source'], use['obtainedAt']
    data_times = [record['obtainedAt'] for entry in coverage for record in entry['pageRecords']
                  if record['succeeded'] and 'obtainedAt' in record]
    live = sum(1 for use in fetch.uses.values() if use['source'] == 'live')
    duplicates = {item['possibleDuplicateGroup'] for item in accepted + review
                  if item['possibleDuplicateGroup']}
    warnings = sorted(
        set(result.warnings)
        | ({'quarantined_records'} if quarantine else set())
        | ({'classification_needs_review'} if review else set())
        | ({'possible_duplicates'} if duplicates else set())
        | ({'outside_boundary_records'} if outside_boundary else set())
        | ({'query_domain_widened'} if widened else set()))
    group = FacilityGroup(
        query_status=result.status, provider=places.identity, api_version=places.api_version,
        data_source=source,
        query_domain={
            'coordinateSystem': 'bd09ll', 'origin': [origin[0], origin[1]],
            'paddingMeters': QUERY_PADDING_M, 'widened': widened,
            'envelopeLocalMeters': list(domain.envelope),
            'polygonLocalMeters': [list(point) for point in domain.polygon],
        },
        data_obtained_at=min(data_times) if data_times else None,
        counts_by_category={major: sum(1 for item in accepted
                                       if catalog.major_of(item['category']) == major)
                            for major in majors},
        facilities=accepted, review_candidates=review, excluded_candidates=excluded,
        # Both kinds of record that are not findings for this region are kept
        # where they are: one whose row could not be read, one outside the query
        # range, and one inside the range but outside the boundary. The reason
        # tells them apart, and a nearby facility stays evidence for the region.
        quarantine=quarantine + outside_domain + outside_boundary, query_coverage=coverage,
        statistics={
            'rawRecords': sum(entry['returned'] for entry in coverage),
            'invalidRecords': len(quarantine),
            'outsideDomainRecords': len(outside_domain),
            'outsideBoundaryRecords': len(outside_boundary),
            'uidMergedRecords': merged,
            'acceptedRecords': len(accepted), 'reviewRecords': len(review),
            'excludedRecords': len(excluded),
            'possibleDuplicateGroups': len(duplicates),
            'queryAttempts': result.attempts, 'budget': result.budget,
            'budgetSpent': budget.spent.get(POI_POOL, 0),
            # Pages, not fetches: a page asked for twice is one page.
            'sequences': len(coverage), 'blocks': len(result.blocks),
            'livePages': live, 'cachedPages': len(fetch.uses) - live, 'pageAttempts': network,
            'engineQuality': snapshot.quality, 'elapsedSeconds': round(time.time() - started, 6),
        },
        warnings=warnings, stop_reason=result.stop_reason)
    issues = [Issue(code="FACILITY_WARNING", message=warning, scope="facilities")
              for warning in warnings]
    return FacilityOutcome(group=group, status=_status_for(result.status),
                           requests=result.attempts, network_requests=network, issues=issues)
