"""Does Baidu's time along a returned route match a direct query to a point on it?

Route-prefix evidence (tools.endpoint_route_prefix) takes any vertex reached within
900 s along a returned walking route as reachable. That is sound only if a direct
query to that vertex does not come back much slower than the time along the route.
This check measures it at the Guodingyi test origin with a hard budget:

- phase A: 20 routes to road points 1300 m out, every 18 degrees (one request each);
- phase B: on each returned route, the vertex nearest 600 s and the last vertex at or
  below 900 s, each queried directly (one request each, at most 40).

    python -m tools.route_prefix_check plan --output OUT
    python -m tools.route_prefix_check run --output OUT [--dry-run]
    python -m tools.route_prefix_check summarize --output OUT [--dry-run]

Times along a route come from the per-step durations (``route_path_seconds``); a
route without them is timed by distance at its own average pace, and says so.
Never retried; the live run is a one-time claim at an effective QPS of at most 3
(set ANALYSIS_QPS). ``--dry-run`` answers from the OSM graph stand-in, where a direct
query can never be slower than the route (Δ ≤ 0 by construction).
"""
import argparse
import asyncio
import json
import math
import statistics
import time
from pathlib import Path

from shapely.geometry import Point

from app.hybrid_api import content_hash
from app.persistence import atomic_dump
from app.test_origin import NORMALIZED_TEST_ORIGIN
from life_circle.coordinates import LocalProjection, normalize
from tools.e821_independent_validation import FATAL, claim, load_store, read

SCHEMA = 'route-prefix-check-v1'
ROUTES = 20
BUDGET = 60
RADIUS_M = 1300
ROAD_REACH_M = 40
OFFSET_LIMIT_M = 50
TARGETS_S = (600, 900)
MIN_FROM_ORIGIN_M = 50
#: Acceptance (proposed before the run): enough decidable pairs, small and rarely
#: positive differences, and no vertex the prefix calls reachable by a margin that a
#: direct query puts past 900 s.
PASS = dict(min_decidable=32, median_abs_s=20, p90_abs_s=60, max_share_over_60s=.10, margin_s=60)


def make_plan(store):
    origin = normalize(NORMALIZED_TEST_ORIGIN)
    from app.checkups.accessibility_stage import public_point
    ox, oy = store.projection.origin(origin)
    cases = []
    for k in range(ROUTES):
        azimuth = math.radians(18 * k + 9)
        for radius in (RADIUS_M, RADIUS_M - 50, RADIUS_M + 50, RADIUS_M - 100, RADIUS_M + 100):
            point = Point(ox + radius * math.cos(azimuth), oy + radius * math.sin(azimuth))
            index = min(map(int, store.index.query_nearest(point, all_matches=True)))
            geometry = store.geometries[index]
            snapped = geometry.interpolate(geometry.project(point))
            if point.distance(snapped) <= ROAD_REACH_M:
                cases.append(dict(id=f'r{k:02d}', azimuth_deg=18 * k + 9, radius_m=radius,
                                  coordinate=list(normalize(public_point(store.projection, (snapped.x, snapped.y))))))
                break
    return dict(schema=SCHEMA, origin=list(origin), cases=cases, budget=BUDGET, targets_s=list(TARGETS_S),
                pass_rule=PASS)


def timed_path(observation):
    """(local vertices, seconds, source) along a returned route, or None."""
    if not observation.route_path or observation.observed_duration is None:
        return None
    projection = LocalProjection(tuple(NORMALIZED_TEST_ORIGIN))
    local = [projection.to_local(p) for p in observation.route_path]
    if len(observation.route_path_seconds) == len(local):
        return local, list(observation.route_path_seconds), 'steps'
    along = [0.0]
    for a, b in zip(local, local[1:]):
        along.append(along[-1] + math.dist(a, b))
    if along[-1] <= 0:
        return None
    return local, [observation.observed_duration * d / along[-1] for d in along], 'distance'


def pick_targets(observation):
    """The vertex nearest 600 s and the last vertex at or below 900 s, away from the origin."""
    timed = timed_path(observation)
    if timed is None:
        return []
    local, seconds, source = timed
    usable = [(i, t) for i, (xy, t) in enumerate(zip(local, seconds))
              if t <= 900 and math.hypot(*xy) >= MIN_FROM_ORIGIN_M]
    if not usable:
        return []
    near_600 = min(usable, key=lambda item: abs(item[1] - 600))
    last_900 = max(usable, key=lambda item: item[1])
    picked = []
    for target, (index, t) in zip(TARGETS_S, (near_600, last_900)):
        if picked and picked[-1]['index'] == index:
            continue
        picked.append(dict(target_s=target, index=index, prefix_s=t, timing=source,
                           coordinate=list(normalize(observation.route_path[index]))))
    return picked


def evidence(case_id, coordinate, observation, **extra):
    return dict(id=case_id, request_coordinate=coordinate, observed_duration=observation.observed_duration,
                reason=observation.reason, endpoint_verified=observation.endpoint_verified,
                destination_offset_m=observation.destination_offset_m, distance_m=observation.distance_m,
                route_vertices=len(observation.route_path), timed_by_steps=bool(observation.route_path_seconds),
                collected_at=time.time(), **extra)


async def query_all(plan, provider, ledger_path):
    origin, used = tuple(plan['origin']), 0
    routes, direct, stop = [], [], None

    def save():
        atomic_dump(ledger_path, dict(requests_used=used, routes=routes, direct=direct, stop_reason=stop))

    targets = []
    for case in plan['cases']:
        if used >= BUDGET:
            break
        observation = await provider.query_walking_time(origin, tuple(case['coordinate']), time.monotonic() + 30)
        used += 1
        picked = pick_targets(observation) if observation.reason in (None, 'endpoint_offset') else []
        routes.append(evidence(case['id'], case['coordinate'], observation, targets=picked))
        targets += [dict(t, route=case['id']) for t in picked]
        save()
        if observation.reason in FATAL:
            stop = observation.reason
            save()
            return
    for target in targets:
        if used >= BUDGET:
            stop = 'budget'
            break
        observation = await provider.query_walking_time(origin, tuple(target['coordinate']), time.monotonic() + 30)
        used += 1
        direct.append(evidence(f"{target['route']}-{target['target_s']}", target['coordinate'], observation,
                               route=target['route'], target_s=target['target_s'], prefix_s=target['prefix_s'],
                               timing=target['timing']))
        save()
        if observation.reason in FATAL:
            stop = observation.reason
            break
    save()


def metrics(ledger):
    rows = []
    for d in ledger['direct']:
        decidable = (d['observed_duration'] is not None and d['endpoint_verified']
                     and d['reason'] in (None, 'endpoint_offset')
                     and (d['destination_offset_m'] or 0) <= OFFSET_LIMIT_M)
        rows.append(dict(d, decidable=decidable,
                         delta_s=d['observed_duration'] - d['prefix_s'] if decidable else None))
    decided = [r for r in rows if r['decidable']]

    def stats(items):
        deltas = [r['delta_s'] for r in items]
        if not deltas:
            return dict(n=0)
        absolute = sorted(abs(v) for v in deltas)
        return dict(n=len(deltas), median_abs_s=statistics.median(absolute),
                    p90_abs_s=absolute[min(len(absolute) - 1, int(.9 * len(absolute)))],
                    max_delta_s=max(deltas), min_delta_s=min(deltas),
                    share_over_60s=sum(v > 60 for v in deltas) / len(deltas))
    overall = stats(decided)
    unsafe = [r['id'] for r in decided if r['prefix_s'] <= 900 - PASS['margin_s'] and r['observed_duration'] > 900]
    label_900 = [r for r in decided if r['target_s'] == 900]
    passed = bool(len(decided) >= PASS['min_decidable'] and overall.get('n')
                  and overall['median_abs_s'] <= PASS['median_abs_s'] and overall['p90_abs_s'] <= PASS['p90_abs_s']
                  and overall['share_over_60s'] <= PASS['max_share_over_60s'] and not unsafe)
    return dict(requests_used=ledger['requests_used'], stop_reason=ledger['stop_reason'],
                routes=len(ledger['routes']), routes_timed_by_steps=sum(r['timed_by_steps'] for r in ledger['routes']),
                direct=len(rows), decidable=len(decided), overall=overall,
                by_target={str(t): stats([r for r in decided if r['target_s'] == t]) for t in TARGETS_S},
                reachable_agreement_900=(sum(r['observed_duration'] <= 900 for r in label_900), len(label_900)),
                unsafe=unsafe, passed=passed, rows=rows)


def render(result, dry_run):
    o = result['overall']
    fmt = lambda v: '—' if v is None else f'{v:.1f}'
    lines = [f"# Route-prefix consistency{' (dry run: OSM stand-in, not Baidu)' if dry_run else ''}", '',
             f"requests {result['requests_used']} / {BUDGET}, stop {result['stop_reason']}; routes {result['routes']} "
             f"({result['routes_timed_by_steps']} timed by step durations); direct queries {result['direct']}, "
             f"decidable {result['decidable']}", '',
             '| target | n | median abs(Δ) s | P90 abs(Δ) s | max Δ s | min Δ s | share Δ > +60 s |', '|---|---:|---:|---:|---:|---:|---:|']
    for name, s in [('all', o)] + [(f'{t} s', result['by_target'][str(t)]) for t in TARGETS_S]:
        if not s.get('n'):
            lines.append(f'| {name} | 0 | — | — | — | — | — |')
            continue
        lines.append(f"| {name} | {s['n']} | {fmt(s['median_abs_s'])} | {fmt(s['p90_abs_s'])} | {fmt(s['max_delta_s'])} | "
                     f"{fmt(s['min_delta_s'])} | {s['share_over_60s']:.0%} |")
    agree, total = result['reachable_agreement_900']
    lines += ['', f'Δ = direct − along the route. At the last vertex ≤ 900 s, {agree} of {total} direct queries '
                  f'are also ≤ 900 s. Unsafe (prefix ≤ {900 - PASS["margin_s"]} s, direct > 900 s): '
                  f"{result['unsafe'] or 'none'}.", '',
              f"Pass rule {PASS}: **{'pass' if result['passed'] else 'fail'}**"]
    return '\n'.join(lines)


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
        raise SystemExit('check_requires_qps_at_most_3 (set ANALYSIS_QPS)')
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
        raise ValueError('check_budget_exceeded')
    result = metrics(ledger)
    atomic_dump(target / 'metrics.json', result)
    text = render(result, dry_run)
    (target / 'summary.md').write_text(text + '\n', encoding='utf-8')
    print(text)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('command', choices=('plan', 'run', 'summarize'))
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    output = args.output.resolve()
    try:
        if args.command == 'plan':
            store, settings = load_store()
            plan = make_plan(store)
            plan.update(osm_data_version=settings.osm_data_version)
            output.mkdir(parents=True, exist_ok=True)
            claim(output / 'plan.json', plan)  # written once, before anything is seen
            print(json.dumps(dict(plan_hash=content_hash(plan), cases=len(plan['cases']))))
        elif args.command == 'run':
            run_command(output, args.dry_run)
        else:
            summarize_command(output, args.dry_run)
    except (ValueError, OSError) as exc:
        raise SystemExit(f'{type(exc).__name__}: {exc}')


if __name__ == '__main__':
    main()
