"""Fair bounded pagination and explicit collection/data-quality outcomes."""
import asyncio

from app.place_protocol import Pagination
from app.request_control import RequestStopped
from .models import CATEGORIES, PoiCollectionResult, PoiCollectRequest
from .normalize import merge_entities, normalize
from .planner import build_plan
from .runtime import utcnow


async def collect_pois(request, provider, runtime) -> PoiCollectionResult:
    if not isinstance(request, PoiCollectRequest):
        raise RequestStopped('invalid_request')
    if runtime.used:
        raise RequestStopped('run_already_started')
    plan = build_plan(request, runtime.config)
    if plan != runtime.plan:
        raise RequestStopped('plan_mismatch')
    if provider.network != runtime.live:
        raise RequestStopped('provider_mode_mismatch')
    runtime.used = True
    sequences = plan['sequences']
    coverage = [{**s, 'status': 'pending', 'pages': 0, 'returned': 0, 'total': None,
                 'warnings': [], 'stopReason': None, 'requestedPages': [], 'successfulPages': [],
                 'reportedTotals': [], 'pageErrors': []} for s in sequences]
    pagination = [Pagination() for _ in sequences]
    records, quarantine = [], []
    source = 'baidu_place' if runtime.live else 'synthetic'
    try:
        for page in range(plan['maxPages']):
            for index, sequence in enumerate(sequences):
                meta = coverage[index]
                if meta['status'] not in ('pending', 'running'):
                    continue
                try:
                    payload, reason = await runtime.fetch(provider, sequence, page)
                except RequestStopped as exc:
                    reason, payload = exc.reason, None
                    if reason != 'category_budget':
                        runtime.stop_reason = reason
                if payload is None:
                    meta['pageErrors'].append({'pageNum': page, 'reason': reason})
                    meta.update(status='partial' if meta['pages'] else 'failed', stopReason=reason)
                    continue
                state = pagination[index]
                reason = state.consume(payload, plan['maxPages'])
                meta['successfulPages'].append(page)
                meta['reportedTotals'].append({'pageNum': page, 'total': state.total})
                meta.update(pages=state.pages, returned=state.returned, total=state.total,
                            warnings=sorted(set(state.warnings)), status='running')
                for row_index, row in enumerate(payload['results']):
                    provenance = {'tileId': sequence['tileId'], 'category': sequence['category'],
                                  'sequenceId': sequence['sequenceId'], 'query': sequence['query'], 'pageNum': page}
                    try:
                        records.append(normalize(row, provenance, source))
                    except ValueError as exc:
                        quarantine.append({'reason': str(exc), 'rowIndex': row_index, 'provenance': provenance})
                if reason:
                    meta.update(status='completed' if reason == 'completed' else 'partial', stopReason=reason)
                    if reason != 'completed':
                        meta['warnings'] = sorted(set(meta['warnings'] + [reason]))
            if runtime.stop_reason or runtime.token.cancelled:
                break
    except asyncio.CancelledError:
        runtime.token.cancel()
        runtime.stop_reason = 'cancelled'
    if runtime.token.cancelled:
        runtime.stop_reason = 'cancelled'
    for meta in coverage:
        meta['requestedPages'] = sorted({e['pageNum'] for e in runtime.events
                                        if e['sequenceId'] == meta['sequenceId']})
        if meta['status'] in ('pending', 'running'):
            meta.update(status='partial' if meta['pages'] else 'failed', stopReason=runtime.stop_reason or 'unfinished')
    accepted, review, excluded, outside, merged = merge_entities(records, request, plan)
    completed = all(m['status'] == 'completed' for m in coverage)
    evidence = any(m['pages'] for m in coverage)
    status = 'completed' if completed else 'partial' if evidence else 'failed'
    if runtime.token.cancelled:
        status = 'cancelled'
    warnings = sorted({w for m in coverage for w in m['warnings']}
                      | {m['stopReason'] for m in coverage if m['stopReason'] not in (None, 'completed')})
    if quarantine:
        warnings.append('quarantined_records')
    if review:
        warnings.append('classification_needs_review')
    duplicate_groups = {p['possibleDuplicateGroup'] for p in accepted + review if p['possibleDuplicateGroup']}
    if duplicate_groups:
        warnings.append('possible_duplicates')
    if runtime.ledger:
        runtime.ledger.data['status'] = status
        try:
            runtime.persist()
        except RequestStopped:
            status = 'partial' if evidence else 'failed'
            warnings.append('audit_failure')
    return PoiCollectionResult(collection_id=runtime.config.run_id, provider='baidu_place' if runtime.live else 'synthetic',
        data_source=source, center=request.center, analysis_extent=plan['analysisExtent'], search_extent=plan['searchExtent'],
        started_at=runtime.started_at, finished_at=utcnow(), query_profile_version=request.query_profile_version,
        config_hash=plan['configHash'], query_status=status, pois=accepted, review_candidates=review,
        excluded_candidates=excluded, quarantine=quarantine + outside, query_coverage=coverage,
        counts_by_category={c: sum(p['category'] == c for p in accepted) for c in CATEGORIES},
        statistics={**runtime.metrics(), 'rawRecords': sum(m['returned'] for m in coverage),
            'invalidRecords': len(quarantine), 'outsideWindowRecords': len(outside), 'uidMergedRecords': merged,
            'acceptedRecords': len(accepted), 'reviewRecords': len(review), 'excludedRecords': len(excluded),
            'possibleDuplicateGroups': len(duplicate_groups)}, warnings=sorted(set(warnings)), stop_reason=runtime.stop_reason)
