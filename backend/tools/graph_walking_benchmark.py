"""E8.2 benchmark on a Baidu stand-in that walks the real OSM network.

Routes run on the configured OSM graph. A request point snaps to the nearest road
point -- the way Baidu returns a road endpoint -- and the returned duration is the
network length at ROUTE_SPEED_MPS; an endpoint more than 50 m away is marked
``endpoint_offset`` as BaiduProvider does. Truth is labelled at the *request*
position on a 10 m grid; a point whose snap exceeds 50 m is not valid evidence and
is left out of every metric. This is a 10 m simulation reference over OSM, not
Baidu's own network, and no Baidu request is ever made.

    python -m tools.graph_walking_benchmark --output OUT [--budgets 400 800] [--arms legacy loop E1ca] [--workers 4] [--save-runs]

Arms are resolved by tools.e82_variants. ``--save-runs`` keeps every run's
published regions and request points, and each origin's truth grid, for
tools.e82_error_anatomy.

OSM settings come from the environment (OSM_GRAPH_CACHE_PATH, OSM_DATA_VERSION, ...).
"""
import argparse
import asyncio
import json
import math
import statistics
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import shapely
from shapely.geometry import Point
from shapely.ops import substring, transform

from life_circle.coordinates import LocalProjection, normalize
from life_circle.models import CancelToken, RouteObservation
from app.algorithms.baidu_e82 import compute_e82
from app.algorithms.osm_offline.engine import OsmOfflineEngine
from app.algorithms.osm_offline.routing import cutoff_dijkstra_tree
from app.algorithms.osm_offline.snap import nearest_edge_source
from app.checkups.accessibility_stage import public_point
from app.config import load_settings
from app.engines.baidu_e82 import e82_request
from tools.diagnostic_common import no_network
from tools.e82_benchmark import freeze
from tools.e82_variants import describe, resolve_arm
from tools.live_smoke import dump

#: Walking speed of the stand-in's durations: the ratio of distance to duration in
#: real Baidu walking routes around the test centre is about 1.1 m/s.
ROUTE_SPEED_MPS = 1.1
OFFSET_LIMIT_M = 50.0
#: The network search runs this far (seconds at the graph's own speed); anything
#: further in the same component answers as clearly unreachable.
SEARCH_TIME_S = 1600.0
GRID_M = 10
BAND_M = 50
EXTENT_M = 1600
#: Seven places of different form (bd09ll): a river and campus edge, the dense
#: centre, the riverfront, a large-block business park, two suburbs, an old town.
ORIGINS = {
    'guodingyi': (121.513925, 31.313079),
    'people_square': (121.4800, 31.2380),
    'lujiazui': (121.5090, 31.2450),
    'xujiahui': (121.4430, 31.1990),
    'zhangjiang': (121.6010, 31.2090),
    'songjiang': (121.2330, 31.0380),
    'jiading': (121.2560, 31.3830),
}

_store = None
_truth = {}


class Walker:
    """One origin's network field: snap once, search once, answer any endpoint."""

    def __init__(self, store, origin, *, graph_speed):
        self.store, self.origin = store, normalize(origin)
        self.local = LocalProjection(self.origin)
        self.graph_speed = graph_speed
        x, y = store.projection.origin(self.origin)
        self.snap = nearest_edge_source(store, (x, y), speed=graph_speed, max_distance=200,
                                        threshold=1e9)
        # The tree costs are exactly cutoff_dijkstra's; it also lets route() read a path back.
        self.tree = cutoff_dijkstra_tree(store.graph, self.snap.seeds, SEARCH_TIME_S)
        self.times = {node: cost for node, (cost, _, _) in self.tree.items()}
        self.origin_component = store.component.get(self.snap.edge[0])
        self.route_origin = public_point(store.projection, (self.snap.point.x, self.snap.point.y))

    def _arrival(self, edge, geometry, position):
        """(seconds, how): the fastest way onto ``position`` along ``edge``.

        ``how`` is (node the leg starts from or None for the origin's own edge, the
        oriented edge, start and end along it), so route() can retrace it.
        """
        graph, (u, v, _) = self.store.graph, edge
        length = geometry.length or 1e-9
        travel = graph.edges[edge]['travel_time_s']
        best = (math.inf, None)

        def consider(seconds, how):
            nonlocal best
            if seconds < best[0]:
                best = (seconds, how)
        if u in self.times:
            consider(self.times[u] + travel * position / length, (u, edge, 0.0, position))
        for key, data in graph.get_edge_data(v, u, default={}).items():
            if abs(data['length'] - length) < 0.01 and v in self.times:
                consider(self.times[v] + data['travel_time_s'] * (length - position) / length,
                         (v, (v, u, key), 0.0, length - position))
        for source, start in self.snap.source_intervals:
            if source == edge and position >= start:
                consider(travel * (position - start) / length, (None, edge, start, position))
            elif source[0] == v and source[1] == u and (length - position) >= start:
                consider(graph.edges[source]['travel_time_s'] * ((length - position) - start) / length,
                         (None, source, start, length - position))
        return best

    def _time_on(self, edge, geometry, position):
        return self._arrival(edge, geometry, position)[0]

    def _nearest(self, x, y):
        point = Point(x, y)
        index = min(map(int, self.store.index.query_nearest(point, all_matches=True)))
        edge, geometry = self.store.edge_ids[index], self.store.geometries[index]
        return point, edge, geometry, geometry.project(point)

    def at(self, x, y):
        """(seconds at the graph speed or None when disconnected, snapped point, offset m)."""
        point, edge, geometry, position = self._nearest(x, y)
        snapped = geometry.interpolate(position)
        seconds = self._time_on(edge, geometry, position)
        if not math.isfinite(seconds):
            same = self.store.component.get(edge[0]) == self.origin_component
            seconds = SEARCH_TIME_S if same else None
        return seconds, snapped, point.distance(snapped)

    def _seed_leg(self, seed):
        """The stretch of the origin's own edge from its snap point to ``seed``, or None."""
        graph, best = self.store.graph, None
        for source, start in self.snap.source_intervals:
            if source[0] == seed and start == 0:
                return None  # the origin snaps onto the node itself
            if source[1] == seed:
                cost = graph.edges[source]['travel_time_s'] * (1 - start / graph.edges[source]['geometry'].length)
                if best is None or cost < best[0]:
                    best = (cost, source, start)
        if best is None:
            return None
        _, source, start = best
        line = graph.edges[source]['geometry']
        return substring(line, start, line.length)

    def route(self, x, y):
        """The fastest route to a request's road point: (metric vertices, graph seconds), or None.

        Built from the same search as at(): the shortest-path tree back to a seed, the
        origin's own edge up to that seed, and the last edge up to the snapped point.
        """
        _, edge, geometry, position = self._nearest(x, y)
        seconds, how = self._arrival(edge, geometry, position)
        if how is None or not math.isfinite(seconds):
            return None
        node, last, start, end = how
        graph = self.store.graph
        legs = []
        if node is not None:
            while True:
                _, previous, key = self.tree[node]
                if previous is None:
                    break
                legs.append(graph.edges[(previous, node, key)]['geometry'])
                node = previous
            legs.reverse()
            first = self._seed_leg(node)
            if first is not None:
                legs.insert(0, first)
        legs.append(substring(graph.edges[last]['geometry'], start, end))
        vertices = []
        for leg in legs:
            for xy in leg.coords:
                if not vertices or vertices[-1] != xy:
                    vertices.append(xy)
        along = [0.0]
        for a, b in zip(vertices, vertices[1:]):
            along.append(along[-1] + math.dist(a, b))
        # Geometry length and the time field agree up to rounding; the last vertex carries at()'s time.
        scale = seconds / along[-1] if along[-1] > 0 else 0.0
        return vertices, [metres * scale for metres in along]

    def duration(self, seconds):
        """Graph seconds → a walking-route duration at the stand-in's speed."""
        return seconds * self.graph_speed / ROUTE_SPEED_MPS


class GraphProvider:
    network = False

    def __init__(self, walker):
        self.walker, self.calls = walker, 0
        self.identity = ('graph-walking', walker.origin)

    async def query_walking_time(self, origin, destination, deadline):
        self.calls += 1
        walker = self.walker
        x, y = walker.store.projection.origin(destination)
        seconds, snapped, offset = walker.at(x, y)
        if seconds is None:
            return RouteObservation(destination, reason='no_route')
        duration = walker.duration(seconds)
        end = public_point(walker.store.projection, (snapped.x, snapped.y))
        path, path_seconds = [], []
        # Like Baidu, a route comes with its polyline and the time to each vertex;
        # past the search cutoff there is no route to read back.
        route = walker.route(x, y) if seconds < SEARCH_TIME_S else None
        if route is not None:
            vertices, graph_seconds = route
            path = [public_point(walker.store.projection, xy) for xy in vertices]
            path_seconds = [walker.duration(value) for value in graph_seconds]
        return RouteObservation(destination, duration, observed_duration=duration,
                                reason='endpoint_offset' if offset > OFFSET_LIMIT_M else None,
                                route_origin=walker.route_origin, route_destination=end,
                                request_origin=origin, distance_m=seconds * walker.graph_speed,
                                route_path=path, route_path_seconds=path_seconds,
                                origin_offset_m=walker.snap.distance_m, destination_offset_m=offset)


def truth_grid(walker):
    """Per 10 m request point: valid (snap ≤ 50 m) and reachable (≤ 900 s)."""
    xs = np.arange(-EXTENT_M + GRID_M / 2, EXTENT_M, GRID_M)
    size = len(xs)
    valid = np.zeros((size, size), dtype=bool)
    reach = np.zeros((size, size), dtype=bool)
    for j, ly in enumerate(xs):
        for i, lx in enumerate(xs):
            x, y = walker.store.projection.origin(walker.local.to_geographic((lx, ly)))
            seconds, _, offset = walker.at(x, y)
            if seconds is None or offset > OFFSET_LIMIT_M:
                continue
            valid[j, i] = True
            reach[j, i] = walker.duration(seconds) <= 900
    return xs, valid, reach


def band_mask(valid, reach):
    cells = BAND_M // GRID_M
    band = np.zeros_like(valid)
    size = valid.shape[0]
    for dy in range(-cells, cells + 1):
        for dx in range(-cells, cells + 1):
            if dx * dx + dy * dy > cells * cells:
                continue
            ys = slice(max(0, dy), size + min(0, dy))
            yd = slice(max(0, -dy), size + min(0, -dy))
            xs_ = slice(max(0, dx), size + min(0, dx))
            xd = slice(max(0, -dx), size + min(0, -dx))
            both = valid[yd, xd] & valid[ys, xs_]
            band[yd, xd] |= both & (reach[yd, xd] != reach[ys, xs_])
    return band


def _init():
    global _store
    settings = load_settings()
    engine = OsmOfflineEngine.load(settings)
    if engine.store is None:
        raise SystemExit(f'OSM graph unavailable: {engine.unavailable_reason}')
    _store = (engine.store, settings.walk_speed_mps)


def save_run(root, name, budget, arm, walker, truth, result):
    """What tools.e82_error_anatomy reads: regions, request points, the truth grid."""
    evidence = result.metadata.get('evidence', {})
    log = evidence.get('observationEvidence', [])
    safe = arm.replace(':', '_').replace('=', '-')  # overrides make arm names Windows cannot store
    dump(root / 'runs' / f'{name}-{budget}-{safe}.json', dict(
        origin=name, origin_bd09=list(walker.origin), budget=budget, arm=arm, stopReason=result.stop_reason,
        geometry=result.geometry, unknownRegion=result.unknown_region,
        requests=[e['destination'] for e in log],
        actual=[e['route_destination'] for e in log if e['accepted']],
        refinementLoop=evidence.get('refinementLoop')))
    path = root / 'truth' / f'{name}.npz'
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        xs, valid, reach = truth
        np.savez_compressed(path, xs=xs, valid=valid, reach=reach)


def _job(job):
    name, budget, arm, save_root = job
    store, speed = _store
    walker = Walker(store, ORIGINS[name], graph_speed=speed)
    if name not in _truth:
        _truth[name] = truth_grid(walker)
    xs, valid, reach = _truth[name]
    provider = GraphProvider(walker)
    refinement, config = resolve_arm(arm)
    started = time.perf_counter()

    async def go():
        with no_network():
            return await compute_e82(e82_request(walker.origin, budget, refinement=refinement), provider,
                                     CancelToken(), refinement=refinement, loop_config=config)
    result = asyncio.run(go())
    elapsed = time.perf_counter() - started
    if save_root is not None:
        save_run(save_root, name, budget, arm, walker, _truth[name], result)
    loop = result.metadata.get('evidence', {}).get('refinementLoop') or {}
    row = dict(origin=name, budget=budget, arm=arm, calls=provider.calls, elapsed_s=elapsed,
               stop_reason=loop.get('stopReason'), loop_calls=loop.get('calls'), loop_counts=loop.get('counts'),
               valid_share=float(valid.mean()), truth_reachable_m2=float(reach.sum() * GRID_M ** 2))
    if result.geometry is None:
        return dict(row, failed=True)
    pred = transform(lambda x, y, z=None: walker.local.to_local((x, y)), shapely.geometry.shape(result.geometry))
    gx, gy = np.meshgrid(xs, xs)
    inside = shapely.contains_xy(pred, gx, gy)
    p, t = inside[valid], reach[valid]
    band = band_mask(valid, reach)[valid]
    row.update(failed=False,
               iou=float((p & t).sum() / max(1, (p | t).sum())),
               accuracy=float((p == t).mean()),
               band_accuracy=float((p == t)[band].mean()) if band.any() else None,
               false_inclusion=float((p & ~t).sum() / max(1, p.sum())),
               false_exclusion=float((~p & t).sum() / max(1, t.sum())),
               unresolved_m2=float(transform(lambda x, y, z=None: walker.local.to_local((x, y)),
                                             shapely.geometry.shape(result.unknown_region)).area))
    return row


def summarize(rows):
    lines = ['| budget | arm | runs | failed | mean IoU | min IoU | accuracy | band acc | false incl. | false excl. | calls |',
             '|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|']
    groups = {}
    for row in rows:
        groups.setdefault((row['budget'], row['arm']), []).append(row)
    for (budget, arm), items in sorted(groups.items()):
        ok = [r for r in items if not r['failed']]
        mean = lambda key: statistics.fmean(r[key] for r in ok if r.get(key) is not None) if ok else float('nan')
        ious = [r.get('iou', 0) if not r['failed'] else 0 for r in items]
        lines.append(f"| {budget} | {arm} | {len(items)} | {len(items) - len(ok)} | {statistics.fmean(ious):.4f} | "
                     f"{min(ious):.4f} | {mean('accuracy'):.4f} | {mean('band_accuracy'):.4f} | "
                     f"{mean('false_inclusion'):.4f} | {mean('false_exclusion'):.4f} | "
                     f"{statistics.fmean(r['calls'] for r in items):.0f} |")
    arms = list(dict.fromkeys(r['arm'] for r in rows))
    by = {(r['origin'], r['budget'], r['arm']): r for r in rows}
    for budget in sorted({r['budget'] for r in rows}):
        lines += ['', f'| origin @ {budget}: IoU (calls) | ' + ' | '.join(arms) + ' |',
                  '|---|' + '---:|' * len(arms)]
        for name in ORIGINS:
            cells = []
            for arm in arms:
                r = by.get((name, budget, arm))
                cells.append('—' if r is None else 'failed' if r['failed'] else f"{r['iou']:.4f} ({r['calls']})")
            if any(cell != '—' for cell in cells):
                lines.append(f'| {name} | ' + ' | '.join(cells) + ' |')
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--budgets', nargs='+', type=int, default=[400, 800])
    parser.add_argument('--arms', nargs='+', default=['legacy', 'loop'])
    parser.add_argument('--origins', nargs='+', default=list(ORIGINS))
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--save-runs', action='store_true')
    args = parser.parse_args()
    variants = describe(args.arms)  # an unknown arm fails here, before the graph loads
    args.output.mkdir(parents=True, exist_ok=False)
    identity = freeze()
    save_root = args.output if args.save_runs else None
    # Origins are grouped per worker so each truth grid is computed once.
    jobs = [(name, budget, arm, save_root) for name in args.origins for budget in args.budgets for arm in args.arms]
    with ProcessPoolExecutor(max_workers=args.workers, initializer=_init) as pool:
        rows = list(pool.map(_job, jobs, chunksize=len(args.budgets) * len(args.arms)))
    settings = load_settings()
    dump(args.output / 'protocol.json', dict(origins={k: ORIGINS[k] for k in args.origins}, budgets=args.budgets,
         arms=args.arms, variants=variants, grid_m=GRID_M, band_m=BAND_M, route_speed_mps=ROUTE_SPEED_MPS,
         offset_limit_m=OFFSET_LIMIT_M, osm_data_version=settings.osm_data_version, live_calls=0, **identity))
    dump(args.output / 'metrics.json', rows)
    text = summarize(rows)
    (args.output / 'summary.md').write_text(text + '\n', encoding='utf-8')
    print(text)
    if freeze() != identity:
        raise SystemExit('source changed during the benchmark; results are not attributable')


if __name__ == '__main__':
    main()
