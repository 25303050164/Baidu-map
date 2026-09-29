"""E8.2 benchmark on a Baidu stand-in that walks the real OSM network.

Routes run on the configured OSM graph. A request point snaps to the nearest road
point -- the way Baidu returns a road endpoint -- and the returned duration is the
network length at ROUTE_SPEED_MPS; an endpoint more than 50 m away is marked
``endpoint_offset`` as BaiduProvider does. Truth is labelled at the *request*
position on a 10 m grid; a point whose snap exceeds 50 m is not valid evidence and
is left out of every metric. This is a 10 m simulation reference over OSM, not
Baidu's own network, and no Baidu request is ever made.

    python -m tools.graph_walking_benchmark --output OUT [--budgets 400 800] [--arms legacy loop] [--workers 4]

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
from shapely.ops import transform

from life_circle.coordinates import LocalProjection, normalize
from life_circle.models import CancelToken, RouteObservation
from app.algorithms.baidu_e82 import compute_e82
from app.algorithms.osm_offline.engine import OsmOfflineEngine
from app.algorithms.osm_offline.routing import cutoff_dijkstra
from app.algorithms.osm_offline.snap import nearest_edge_source
from app.checkups.accessibility_stage import public_point
from app.config import load_settings
from app.engines.baidu_e82 import e82_request
from tools.diagnostic_common import no_network
from tools.e82_benchmark import freeze
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
        self.times = cutoff_dijkstra(store.graph, self.snap.seeds, SEARCH_TIME_S)
        self.origin_component = store.component.get(self.snap.edge[0])
        self.route_origin = public_point(store.projection, (self.snap.point.x, self.snap.point.y))

    def _time_on(self, edge, geometry, position):
        graph, (u, v, _) = self.store.graph, edge
        length = geometry.length or 1e-9
        travel = graph.edges[edge]['travel_time_s']
        candidates = []
        if u in self.times:
            candidates.append(self.times[u] + travel * position / length)
        for data in graph.get_edge_data(v, u, default={}).values():
            if abs(data['length'] - length) < 0.01 and v in self.times:
                candidates.append(self.times[v] + data['travel_time_s'] * (length - position) / length)
        for source, start in self.snap.source_intervals:
            if source == edge and position >= start:
                candidates.append(travel * (position - start) / length)
            elif source[0] == v and source[1] == u and (length - position) >= start:
                candidates.append(graph.edges[source]['travel_time_s'] * ((length - position) - start) / length)
        return min(candidates, default=math.inf)

    def at(self, x, y):
        """(seconds at the graph speed or None when disconnected, snapped point, offset m)."""
        point = Point(x, y)
        index = min(map(int, self.store.index.query_nearest(point, all_matches=True)))
        edge, geometry = self.store.edge_ids[index], self.store.geometries[index]
        position = geometry.project(point)
        snapped = geometry.interpolate(position)
        seconds = self._time_on(edge, geometry, position)
        if not math.isfinite(seconds):
            same = self.store.component.get(edge[0]) == self.origin_component
            seconds = SEARCH_TIME_S if same else None
        return seconds, snapped, point.distance(snapped)

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
        return RouteObservation(destination, duration, observed_duration=duration,
                                reason='endpoint_offset' if offset > OFFSET_LIMIT_M else None,
                                route_origin=walker.route_origin, route_destination=end,
                                request_origin=origin, distance_m=seconds * walker.graph_speed,
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


def _job(job):
    name, budget, arm = job
    store, speed = _store
    walker = Walker(store, ORIGINS[name], graph_speed=speed)
    if name not in _truth:
        _truth[name] = truth_grid(walker)
    xs, valid, reach = _truth[name]
    provider = GraphProvider(walker)
    started = time.perf_counter()

    async def go():
        with no_network():
            return await compute_e82(e82_request(walker.origin, budget, refinement=arm), provider,
                                     CancelToken(), refinement=arm)
    result = asyncio.run(go())
    elapsed = time.perf_counter() - started
    row = dict(origin=name, budget=budget, arm=arm, calls=provider.calls, elapsed_s=elapsed,
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
    lines += ['', '| origin | budget | IoU legacy → loop | band acc legacy → loop | calls legacy → loop |',
              '|---|---:|---|---|---|']
    by = {(r['origin'], r['budget'], r['arm']): r for r in rows}
    for name in ORIGINS:
        for budget in sorted({r['budget'] for r in rows}):
            a, b = by.get((name, budget, 'legacy')), by.get((name, budget, 'loop'))
            if a and b and not a['failed'] and not b['failed']:
                lines.append(f"| {name} | {budget} | {a['iou']:.4f} → {b['iou']:.4f} | "
                             f"{a['band_accuracy']:.4f} → {b['band_accuracy']:.4f} | {a['calls']} → {b['calls']} |")
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--budgets', nargs='+', type=int, default=[400, 800])
    parser.add_argument('--arms', nargs='+', default=['legacy', 'loop'])
    parser.add_argument('--origins', nargs='+', default=list(ORIGINS))
    parser.add_argument('--workers', type=int, default=4)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    identity = freeze()
    # Origins are grouped per worker so each truth grid is computed once.
    jobs = [(name, budget, arm) for name in args.origins for budget in args.budgets for arm in args.arms]
    with ProcessPoolExecutor(max_workers=args.workers, initializer=_init) as pool:
        rows = list(pool.map(_job, jobs, chunksize=len(args.budgets) * len(args.arms)))
    settings = load_settings()
    dump(args.output / 'protocol.json', dict(origins={k: ORIGINS[k] for k in args.origins}, budgets=args.budgets,
         arms=args.arms, grid_m=GRID_M, band_m=BAND_M, route_speed_mps=ROUTE_SPEED_MPS,
         offset_limit_m=OFFSET_LIMIT_M, osm_data_version=settings.osm_data_version, live_calls=0, **identity))
    dump(args.output / 'metrics.json', rows)
    text = summarize(rows)
    (args.output / 'summary.md').write_text(text + '\n', encoding='utf-8')
    print(text)


if __name__ == '__main__':
    main()
