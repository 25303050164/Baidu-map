"""Formal independent validation of E8.2.1 at the Guodingyi test origin (plan step 5).

One frozen set of 100 points scores two polygons at once: the 09-27 checkup (old
E8.2, budget 400) and the 09-29 smoke checkup (E8.2.1, budget 800), both from the
same origin. Points are drawn with a fixed seed before any request, in strata that
partition the area of interest: inside only the new polygon, inside only the old
one, within 50 m of either boundary, inside both, and up to 400 m outside both.
Outside the two disagreement strata both polygons predict the same label, so the
paired comparison rests on those alone. Each side of the disagreement has its own
allocation: near roads the old-only side is a few thousand square metres, which a
single uniform stratum would leave unsampled.

Truth follows the circle's own semantics at the *request* position: one walking
route from the origin, reachable when its duration is at most 900 s, decidable only
when both route ends lie within 50 m of the requested points (BaiduProvider's
``endpoint_offset`` rule). Candidates are kept within 40 m of the OSM walking
network so that few requests land where Baidu cannot label a point at all. A
fp/fn within 15 s of 900 s is also reported as within tolerance, as in
hybrid-validation-v3.

    python -m tools.e821_independent_validation plan --output OUT
    python -m tools.e821_independent_validation run --output OUT [--dry-run]
    python -m tools.e821_independent_validation summarize --output OUT [--dry-run]

Each point costs one request, never retried. The live run is a one-time claim and
never resumes. ``--dry-run`` answers from the OSM graph stand-in under a network
block and writes to ``OUT/dry-run``. OSM settings come from the environment.
"""
import argparse
import asyncio
import hashlib
import json
import math
import random
import time
from collections import Counter
from pathlib import Path

import numpy as np
import shapely
from shapely.geometry import Point, mapping, shape

from app.geo.coordinates import wgs84_to_bd09
from app.geo.projection import MetricProjection
from app.hybrid_api import content_hash
from app.persistence import atomic_dump
from app.test_origin import NORMALIZED_TEST_ORIGIN
from life_circle.coordinates import normalize

BACKEND = Path(__file__).resolve().parents[1]
OLD_REPORT = BACKEND / 'docs/assets/checkup-v2-live-recheck-20260927/baidu_e82/report.json'
NEW_REVISION = BACKEND / '.checkups-smoke/tasks/3877da1c-c4cb-4b1b-b678-d4d71673eb81/revision-0001-isochrone.json'
SCHEMA = 'e821-independent-validation-v1'
METRIC_CRS = 'EPSG:32651'
SEED = 20260929
BUDGET = 100
ALLOCATION = dict(new_only=34, old_only=6, boundary=40, interior=10, exterior=10)
DISAGREEMENT = ('new_only', 'old_only')
THRESHOLD_S = 900
TOLERANCE_S = 15
OFFSET_LIMIT_M = 50
BAND_M = 50
EXTERIOR_M = 400
ROAD_REACH_M = 40
MIN_SPACING_M = 25
#: Answers after which no further request is sent.
FATAL = ('permission', 'quota')


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def claim(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as stream:
        json.dump(payload, stream, ensure_ascii=False, allow_nan=False)


def to_metric(projection, geometry):
    return shapely.transform(geometry, lambda c: np.array([projection.origin(tuple(p)) for p in c]))


def strata(old, new, extent):
    """Disjoint regions (metric). Predictions differ only in the two disagreement strata."""
    differ = old.symmetric_difference(new)
    band = old.boundary.union(new.boundary).buffer(BAND_M).difference(differ)
    both, union = old.intersection(new), old.union(new)
    regions = dict(new_only=new.difference(old), old_only=old.difference(new), boundary=band,
                   interior=both.difference(band),
                   exterior=union.buffer(EXTERIOR_M).difference(union).difference(band))
    return {name: region.intersection(extent) for name, region in regions.items()}


def draw(regions, allocation, projection, road_distance, *, seed=SEED, attempts=400000):
    """Seeded candidates per stratum; exact positions are the normalized bd09 points."""
    rng = random.Random(seed)
    rows, taken = [], []
    for group, target in allocation.items():
        region = regions[group]
        if not target:
            continue
        if region.is_empty:
            raise ValueError(f'empty_stratum:{group}')
        shapely.prepare(region)
        xmin, ymin, xmax, ymax = region.bounds
        selected = 0
        for _ in range(attempts):
            x, y = rng.uniform(xmin, xmax), rng.uniform(ymin, ymax)
            if not shapely.contains_xy(region, x, y):
                continue
            coordinate = normalize(wgs84_to_bd09(*projection.inverse.transform(x, y)))
            exact = projection.origin(coordinate)
            if not shapely.contains_xy(region, *exact):
                continue
            if any(math.dist(exact, other) < MIN_SPACING_M for other in taken):
                continue
            reach = road_distance(exact)
            if reach is None or reach > ROAD_REACH_M:
                continue
            taken.append(exact)
            rows.append(dict(id=f'p{len(rows) + 1:03d}', group=group, coordinate=list(coordinate),
                             xy=list(exact), road_distance_m=round(reach, 2)))
            selected += 1
            if selected == target:
                break
        if selected != target:
            raise ValueError(f'stratum_candidates_insufficient:{group}')
    return rows


E82_VERSIONS = ('local-multicross-e82.1', 'local-multicross-e82.2')


def shown(path):
    """A source path as recorded: relative to the repository when it lies inside."""
    path = Path(path).resolve()
    try:
        return str(path.relative_to(BACKEND.parent))
    except ValueError:
        return str(path)


def sources(old_path=OLD_REPORT, new_path=NEW_REVISION, old_label='09-27 checkup, E8.2 legacy, budget 400',
            new_label='09-29 smoke checkup, E8.2.1, budget 800'):
    """The two polygons. The old one is a report's domain or an isochrone revision; the
    new one an E8.2.1 or E8.2.2 isochrone revision from the test origin."""
    old, new = read(old_path), read(new_path)
    iso = new['isochrone']
    if iso['parameters']['config_version'] not in E82_VERSIONS:
        raise ValueError('new_polygon_not_e821_or_e822')
    if normalize((new['center']['lng'], new['center']['lat'])) != NORMALIZED_TEST_ORIGIN:
        raise ValueError('new_polygon_other_origin')
    old_polygon = old['isochrone']['geometry'] if 'isochrone' in old else old['document']['domain']
    strip = lambda g: {k: v for k, v in g.items() if k != 'coordinateSystem'}
    for g in (old_polygon, iso['geometry']):
        if g.get('coordinateSystem') != 'bd09ll':
            raise ValueError('polygon_not_bd09ll')
    old_source = dict(path=shown(old_path), sha256=file_hash(old_path), label=old_label)
    if 'isochrone' in old:
        old_source.update(taskId=old['taskId'], isochroneHash=old['isochrone']['isochroneHash'],
                          configVersion=old['isochrone']['parameters']['config_version'],
                          requestsUsed=old['isochrone']['requestsUsed'])
    else:
        old_source.update(resultHash=old['resultHash'])
    return dict(old=strip(old_polygon), new=strip(iso['geometry']),
                new_unresolved=strip(iso['unknownRegion']), extent=strip(iso['computationExtent']),
                old_source=old_source,
                new_source=dict(path=shown(new_path), sha256=file_hash(new_path), label=new_label,
                                taskId=new['taskId'], isochroneHash=iso['isochroneHash'],
                                configVersion=iso['parameters']['config_version'],
                                requestsUsed=iso['requestsUsed']))


def proportional_allocation(regions, total=ALLOCATION['new_only'] + ALLOCATION['old_only'], least=6):
    """The disagreement points split by stratum area, at least ``least`` on each side;
    the other strata keep their fixed allocation. Decided before any point is drawn."""
    new_area, old_area = regions['new_only'].area, regions['old_only'].area
    share = new_area / (new_area + old_area) if new_area + old_area else .5
    new_only = min(total - least, max(least, round(total * share)))
    return dict(ALLOCATION, new_only=new_only, old_only=total - new_only)


def make_plan(road_distance, source, *, proportional=False):
    projection = MetricProjection(METRIC_CRS)
    old_bd, new_bd = shape(source['old']), shape(source['new'])
    unresolved_bd = shape(source['new_unresolved'])
    old, new = to_metric(projection, old_bd), to_metric(projection, new_bd)
    regions = strata(old, new, to_metric(projection, shape(source['extent'])))
    allocation = proportional_allocation(regions) if proportional else ALLOCATION
    cases = draw(regions, allocation, projection, road_distance)
    for case in cases:
        point, xy = Point(case['coordinate']), Point(case['xy'])
        case.update(old_inside=bool(old_bd.covers(point)), new_inside=bool(new_bd.covers(point)),
                    new_unresolved=bool(unresolved_bd.covers(point)),
                    old_boundary_m=round(old.boundary.distance(xy), 2),
                    new_boundary_m=round(new.boundary.distance(xy), 2))
    assert len(cases) == BUDGET and len({tuple(c['coordinate']) for c in cases}) == BUDGET
    return dict(schema_version=SCHEMA, origin=list(NORMALIZED_TEST_ORIGIN), seed=SEED, metric_crs=METRIC_CRS,
                budget=BUDGET, allocation=allocation,
                rules=dict(threshold_s=THRESHOLD_S, tolerance_s=TOLERANCE_S, offset_limit_m=OFFSET_LIMIT_M,
                           band_m=BAND_M, exterior_m=EXTERIOR_M, road_reach_m=ROAD_REACH_M,
                           min_spacing_m=MIN_SPACING_M, retries=0),
                stratum_area_m2={k: round(v.area, 1) for k, v in regions.items()},
                polygon_area_m2=dict(old=round(old.area, 1), new=round(new.area, 1)),
                sources={k: source[k] for k in ('old_source', 'new_source')},
                polygons={k: source[k] for k in ('old', 'new', 'new_unresolved')},
                cases=cases,
                scope='fixed_origin_stratified_independent_points_not_area_accuracy')


def truth(sample):
    """(reachable or None, reason) at the requested position."""
    if sample.get('observed_duration') is None:
        return None, sample.get('reason') or 'no_duration'
    if sample.get('reason') is not None:
        return None, sample['reason']
    if not sample.get('endpoint_verified'):
        return None, 'missing_endpoints'
    return sample['observed_duration'] <= THRESHOLD_S, None


def classify(prediction, reachable, duration):
    if reachable is None:
        return 'api_unknown', 'api_unknown'
    strict = ('tp' if prediction and reachable else 'fp' if prediction else
              'fn' if reachable else 'tn')
    near = duration is not None and abs(duration - THRESHOLD_S) <= TOLERANCE_S
    return strict, 'within_tolerance' if strict in ('fp', 'fn') and near else strict


def wilson(success, count):
    if not count:
        return None
    z = 1.959963984540054
    p = success / count
    denominator = 1 + z * z / count
    middle = (p + z * z / (2 * count)) / denominator
    half = z * math.sqrt(p * (1 - p) / count + z * z / (4 * count * count)) / denominator
    return [round(max(0, middle - half), 4), round(min(1, middle + half), 4)]


def sign_test(wins, losses):
    """Two-sided exact binomial p for discordant pairs."""
    n = wins + losses
    if not n:
        return None
    tail = sum(math.comb(n, k) for k in range(min(wins, losses) + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def summarize_rows(rows, key):
    c = Counter(r[key] for r in rows)
    tp, tn, fp, fn, tol = (c[k] for k in ('tp', 'tn', 'fp', 'fn', 'within_tolerance'))
    decidable = tp + tn + fp + fn + tol
    divide = lambda a, b: round(a / b, 4) if b else None
    return dict(planned=len(rows), decidable=decidable, tp=tp, tn=tn, fp=fp, fn=fn,
                within_tolerance=tol, api_unknown=c['api_unknown'],
                accuracy=divide(tp + tn + tol, decidable), accuracy_wilson_95=wilson(tp + tn + tol, decidable),
                false_inclusion_rate=divide(fp, tp + fp), false_exclusion_rate=divide(fn, tp + fn))


def metrics(plan, ledger):
    observed = {s['id']: s for s in ledger['samples']}
    rows = []
    for case in plan['cases']:
        sample = observed.get(case['id'], {})
        reachable, why = truth(sample) if sample else (None, 'not_requested')
        duration = sample.get('observed_duration')
        row = dict(case, reachable=reachable, unknown_reason=why, duration=duration,
                   destination_offset_m=sample.get('destination_offset_m'))
        for arm in ('old', 'new'):
            row[f'{arm}_strict'], row[f'{arm}_tolerant'] = classify(case[f'{arm}_inside'], reachable, duration)
        rows.append(row)
    areas = plan['stratum_area_m2']
    result = dict(schema_version=SCHEMA, requests_used=ledger['requests_used'], stop_reason=ledger.get('stop_reason'),
                  unknown_reasons=dict(Counter(r['unknown_reason'] for r in rows if r['reachable'] is None)))
    for arm in ('old', 'new'):
        for mode in ('strict', 'tolerant'):
            key = f'{arm}_{mode}'
            strata_ = {g: summarize_rows([r for r in rows if r['group'] == g], key) for g in plan['allocation']}
            weighted = [(areas[g], s['accuracy']) for g, s in strata_.items() if s['accuracy'] is not None]
            total = sum(a for a, _ in weighted)
            result[key] = dict(overall=summarize_rows(rows, key), strata=strata_,
                               area_weighted_accuracy=round(sum(a * p for a, p in weighted) / total, 4) if total else None)
    # Only the disagreement strata separate the two polygons.
    paired, gain = {}, 0.0
    for group in DISAGREEMENT:
        dis = [r for r in rows if r['group'] == group and r['reachable'] is not None
               and r['old_inside'] != r['new_inside']]
        new_right = sum(r['new_inside'] == r['reachable'] for r in dis)
        paired[group] = dict(decidable=len(dis), new_right=new_right, old_right=len(dis) - new_right,
                             reachable=sum(bool(r['reachable']) for r in dis))
        if dis:
            gain += areas[group] * (2 * new_right - len(dis)) / len(dis)
    new_right = sum(p['new_right'] for p in paired.values())
    old_right = sum(p['old_right'] for p in paired.values())
    result['paired'] = dict(paired, decidable=new_right + old_right, new_right=new_right, old_right=old_right,
                            sign_test_p=sign_test(new_right, old_right), net_area_gain_m2=round(gain))
    unresolved = [r for r in rows if r['new_unresolved']]
    result['new_unresolved_region'] = dict(inside=summarize_rows(unresolved, 'new_strict'),
                                           outside=summarize_rows([r for r in rows if not r['new_unresolved']],
                                                                  'new_strict'))
    result['cases'] = rows
    return result


def evidence(case, observation):
    return dict(id=case['id'], request_coordinate=case['coordinate'],
                observed_duration=observation.observed_duration, reason=observation.reason,
                endpoint_verified=observation.endpoint_verified, distance_m=observation.distance_m,
                origin_offset_m=observation.origin_offset_m,
                destination_offset_m=observation.destination_offset_m,
                route_destination=list(observation.route_destination) if observation.route_destination else None,
                collected_at=time.time())


async def query_all(plan, provider, ledger_path):
    samples, stop = [], None
    origin = tuple(plan['origin'])
    for case in plan['cases'][:BUDGET]:
        observation = await provider.query_walking_time(origin, tuple(case['coordinate']), time.monotonic() + 30)
        samples.append(evidence(case, observation))
        atomic_dump(ledger_path, dict(requests_used=len(samples), samples=samples, stop_reason=None))
        if len(samples) % 10 == 0:
            print(json.dumps(dict(stage='validation', requests=len(samples))), flush=True)
        if observation.reason in FATAL:
            stop = observation.reason
            break
    atomic_dump(ledger_path, dict(requests_used=len(samples), samples=samples, stop_reason=stop))


def load_store():
    from app.algorithms.osm_offline.engine import OsmOfflineEngine
    from app.config import load_settings
    settings = load_settings()
    engine = OsmOfflineEngine.load(settings)
    if engine.store is None:
        raise SystemExit(f'OSM graph unavailable: {engine.unavailable_reason}')
    if engine.store.projection.crs != MetricProjection(METRIC_CRS).crs:
        raise SystemExit('OSM graph uses another metric CRS')
    return engine.store, settings


def road_distance_from(store):
    def reach(xy):
        index, distance = store.index.query_nearest(Point(xy), max_distance=ROAD_REACH_M * 2,
                                                    return_distance=True)
        return float(distance.min()) if len(distance) else None
    return reach


def plan_command(output, replace=None, pair=None):
    supersedes = []
    previous = output / 'plan.json'
    if previous.exists():
        # Only an unused plan may be redrawn, and the old one stays on record.
        if not replace:
            raise ValueError('plan_exists')
        if (output / 'started.json').exists() or (output / 'dry-run' / 'started.json').exists():
            raise ValueError('plan_already_used')
        prior = read(previous)
        supersedes = [*prior.get('supersedes', []), dict(plan_hash=content_hash(prior), reason=replace)]
        previous.rename(output / f"plan-superseded-{supersedes[-1]['plan_hash'][:8]}.json")
    store, settings = load_store()
    # Another pair of polygons splits the disagreement points by area: its strata
    # sizes are unknown beforehand, and the split is fixed before any point is drawn.
    plan = make_plan(road_distance_from(store), sources(**pair) if pair else sources(), proportional=bool(pair))
    plan.update(osm_data_version=settings.osm_data_version, supersedes=supersedes)
    output.mkdir(parents=True, exist_ok=True)
    # A plan is written once; a redraw would no longer be independent of what was seen.
    claim(output / 'plan.json', plan)
    counts = Counter((c['group'], c['old_inside'], c['new_inside']) for c in plan['cases'])
    print(json.dumps(dict(plan_hash=content_hash(plan), strata=plan['stratum_area_m2'],
                          polygons=plan['polygon_area_m2'],
                          predictions={f'{g}:{o:d}{n:d}': v for (g, o, n), v in sorted(counts.items())})))


def run_command(output, dry_run):
    plan = read(output / 'plan.json')
    target = output / 'dry-run' if dry_run else output
    started = dict(plan_hash=content_hash(plan), dry_run=dry_run, budget=BUDGET, retries=0, started_at=time.time())
    if dry_run:
        from tools.diagnostic_common import no_network
        from tools.graph_walking_benchmark import GraphProvider, Walker
        atomic_dump(target / 'started.json', started)
        store, settings = load_store()
        provider = GraphProvider(Walker(store, tuple(plan['origin']), graph_speed=settings.walk_speed_mps))

        async def offline():
            # Inside the loop: its own self-pipe needs a loopback socket.
            with no_network():
                await query_all(plan, provider, target / 'ledger.json')
        asyncio.run(offline())
    else:
        claim(target / 'started.json', started)
        asyncio.run(_live(plan, target / 'ledger.json'))
    return summarize_command(output, dry_run)


async def _live(plan, ledger_path):
    import httpx
    from life_circle.providers import BaiduProvider
    from app.analyses import LimitedProvider, effective_qps
    from app.baidu import silence_transport_logs
    from app.config import load_settings
    from app.quota import Quota
    settings = load_settings()
    if not settings.ak_configured:
        raise SystemExit('walking_ak_not_configured')
    gate = Quota(settings).direction.gate
    qps = effective_qps(settings, gate)
    if not qps or qps > 3:
        raise SystemExit('validation_requires_qps_at_most_3')
    silence_transport_logs()
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
        provider = LimitedProvider(BaiduProvider(settings.baidu_map_ak.get_secret_value(), client=client), gate)
        await query_all(plan, provider, ledger_path)


def summarize_command(output, dry_run):
    plan = read(output / 'plan.json')
    target = output / 'dry-run' if dry_run else output
    if read(target / 'started.json')['plan_hash'] != content_hash(plan):
        raise ValueError('plan_changed_after_start')
    ledger = read(target / 'ledger.json')
    if ledger['requests_used'] > BUDGET:
        raise ValueError('validation_budget_exceeded')
    result = metrics(plan, ledger)
    atomic_dump(target / 'metrics.json', result)
    text = render(plan, result, dry_run)
    (target / 'summary.md').write_text(text + '\n', encoding='utf-8')
    atomic_dump(target / 'points.geojson', dict(type='FeatureCollection', features=[
        dict(type='Feature', geometry=mapping(Point(r['coordinate'])),
             properties={k: r[k] for k in ('id', 'group', 'old_inside', 'new_inside', 'new_unresolved',
                                           'reachable', 'unknown_reason', 'duration', 'old_strict', 'new_strict')})
        for r in result['cases']]))
    print(text)
    return result


def render(plan, result, dry_run):
    pct = lambda v: '—' if v is None else f'{v * 100:.1f}%'
    ci = lambda v: '—' if not v else f'{v[0] * 100:.0f}–{v[1] * 100:.0f}%'
    lines = [f"# E8.2.1 independent validation{' (dry run: OSM stand-in, not Baidu)' if dry_run else ''}", '',
             f"requests {result['requests_used']} / {BUDGET}, stop {result['stop_reason']}, "
             f"undecidable {sum(result['unknown_reasons'].values())} {result['unknown_reasons']}", '',
             '| polygon | rule | decidable | accuracy | 95% CI | area-weighted | false incl. | false excl. | tp/tn/fp/fn/tol |',
             '|---|---|---:|---:|---|---:|---:|---:|---|']
    for arm in ('old', 'new'):
        for mode in ('strict', 'tolerant'):
            m = result[f'{arm}_{mode}']
            o = m['overall']
            lines.append(f"| {arm} | {mode} | {o['decidable']} | {pct(o['accuracy'])} | {ci(o['accuracy_wilson_95'])} | "
                         f"{pct(m['area_weighted_accuracy'])} | {pct(o['false_inclusion_rate'])} | "
                         f"{pct(o['false_exclusion_rate'])} | {o['tp']}/{o['tn']}/{o['fp']}/{o['fn']}/{o['within_tolerance']} |")
    lines += ['', '| stratum | area m² | planned | decidable | old accuracy | new accuracy |', '|---|---:|---:|---:|---:|---:|']
    for group in plan['allocation']:
        a, b = result['old_strict']['strata'][group], result['new_strict']['strata'][group]
        lines.append(f"| {group} | {plan['stratum_area_m2'][group]:.0f} | {a['planned']} | {a['decidable']} | "
                     f"{pct(a['accuracy'])} | {pct(b['accuracy'])} |")
    p = result['paired']
    lines += ['', f"disagreement (decidable {p['decidable']}): new right {p['new_right']}, old right {p['old_right']}, "
              f"sign test p={p['sign_test_p'] if p['sign_test_p'] is None else round(p['sign_test_p'], 4)}; "
              + '; '.join(f"{g}: {p[g]['reachable']} of {p[g]['decidable']} reachable" for g in DISAGREEMENT)
              + f"; net correctly labelled area ≈ {p['net_area_gain_m2']} m² (new minus old)",
              '', 'new polygon, strict, by its solver-unresolved region: '
              f"inside {pct(result['new_unresolved_region']['inside']['accuracy'])} "
              f"(n={result['new_unresolved_region']['inside']['decidable']}), "
              f"outside {pct(result['new_unresolved_region']['outside']['accuracy'])} "
              f"(n={result['new_unresolved_region']['outside']['decidable']})"]
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('command', choices=('plan', 'run', 'summarize'))
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--replace', metavar='REASON', help='redraw an unused plan, keeping the old one on record')
    parser.add_argument('--old', type=Path, help='old polygon: a report or an isochrone revision (plan only)')
    parser.add_argument('--new', type=Path, help='new polygon: an E8.2.1/E8.2.2 isochrone revision (plan only)')
    parser.add_argument('--old-label')
    parser.add_argument('--new-label')
    args = parser.parse_args()
    output = args.output.resolve()
    pair = None
    if args.old or args.new:
        if not (args.old and args.new and args.old_label and args.new_label):
            raise SystemExit('--old, --new, --old-label and --new-label go together')
        pair = dict(old_path=args.old.resolve(), new_path=args.new.resolve(),
                    old_label=args.old_label, new_label=args.new_label)
    try:
        if args.command == 'plan':
            plan_command(output, args.replace, pair)
        elif args.command == 'run':
            run_command(output, args.dry_run)
        else:
            summarize_command(output, args.dry_run)
    except (ValueError, OSError) as exc:
        # No provider text, request URL or credential in the terminal.
        print(json.dumps(dict(error='validation_failed', exception_type=type(exc).__name__,
                              detail=str(exc) if isinstance(exc, ValueError) else None)), flush=True)
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
