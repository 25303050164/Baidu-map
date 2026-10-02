"""Where an E8.2 boundary is wrong, on runs kept by graph_walking_benchmark --save-runs.

For every run: false exclusion (truth reachable, outside the published region) and
false inclusion (truth unreachable, inside it) on the 10 m truth grid, split by
distance to the published boundary; their connected parts of 0.25 ha or more with
where they sit, how deep they reach and how far the nearest request was; and how
much of the truth lies beyond the first unreachable stretch along its own ray
(re-entry), which a star-shaped search can only find locally.

    python -m tools.e82_error_anatomy analyze --run DIR [--baseline-arm loop] [--baseline DIR]

Writes DIR/anatomy.json and DIR/anatomy.md. 0 Baidu calls; no graph needed.
"""
import argparse
import json
import math
import statistics
from collections import deque
from pathlib import Path

import numpy as np
import shapely
from shapely.geometry import shape
from shapely.ops import transform

from life_circle.coordinates import LocalProjection
from tools.live_smoke import dump

BANDS_M = (25, 50, 100)
MIN_PART_M2 = 2500
REENTRY_BINS = 720
REENTRY_RUN = 3  # consecutive unreachable samples that count as a crossing


def to_local(geometry, origin):
    projection = LocalProjection(tuple(origin))
    return transform(lambda x, y, z=None: projection.to_local((x, y)), shape(geometry))


def band_shares(distances):
    """Share of cells per distance band to the boundary: ≤25, 25–50, 50–100, >100 m."""
    if not len(distances):
        return [0.0] * (len(BANDS_M) + 1)
    edges = (0,) + BANDS_M
    shares = [float(np.mean((distances > lo if i else distances >= lo) & (distances <= hi)))
              for i, (lo, hi) in enumerate(zip(edges, edges[1:]))]
    return shares + [float(np.mean(distances > BANDS_M[-1]))]


def parts(mask):
    """4-connected cell groups of a boolean grid, as arrays of (row, column)."""
    seen = np.zeros_like(mask, dtype=bool)
    found = []
    for start in map(tuple, np.argwhere(mask)):
        if seen[start]:
            continue
        seen[start] = True
        queue, cells = deque([start]), []
        while queue:
            y, x = queue.popleft()
            cells.append((y, x))
            for ny, nx in ((y + 1, x), (y - 1, x), (y, x + 1), (y, x - 1)):
                if 0 <= ny < mask.shape[0] and 0 <= nx < mask.shape[1] and mask[ny, nx] and not seen[ny, nx]:
                    seen[ny, nx] = True
                    queue.append((ny, nx))
        found.append(np.array(cells))
    return found


def beyond_first_crossing(xs, valid, reach):
    """Cells lying past the first unreachable stretch along the ray from the origin."""
    step = float(xs[1] - xs[0])
    radii = np.arange(step / 2, math.hypot(xs[-1], xs[-1]), step)
    theta = (np.arange(REENTRY_BINS) + .5) * 2 * math.pi / REENTRY_BINS
    px, py = np.outer(np.cos(theta), radii), np.outer(np.sin(theta), radii)
    size = len(xs)
    ii = np.clip(np.round((px - xs[0]) / step).astype(int), 0, size - 1)
    jj = np.clip(np.round((py - xs[0]) / step).astype(int), 0, size - 1)
    inside = (np.abs(px) <= xs[-1]) & (np.abs(py) <= xs[-1])
    sampled_valid = valid[jj, ii] & inside
    unreachable = sampled_valid & ~reach[jj, ii]
    first = np.full(REENTRY_BINS, np.inf)
    for b in range(REENTRY_BINS):
        run = 0
        for k, radius in enumerate(radii):
            if unreachable[b, k]:
                run += 1
                if run >= REENTRY_RUN:
                    first[b] = radius
                    break
            elif sampled_valid[b, k]:
                run = 0
    gx, gy = np.meshgrid(xs, xs)
    cell_bin = np.minimum((np.arctan2(gy, gx) % (2 * math.pi) / (2 * math.pi) * REENTRY_BINS).astype(int),
                          REENTRY_BINS - 1)
    return np.hypot(gx, gy) > first[cell_bin] + step


def anatomy(pred, xs, valid, reach, requests=(), beyond=None):
    """Error anatomy of one published region against one truth grid (local metres)."""
    step = float(xs[1] - xs[0])
    cell_m2 = step * step
    gx, gy = np.meshgrid(xs, xs)
    inside = shapely.contains_xy(pred, gx, gy) if not pred.is_empty else np.zeros_like(valid)
    p, t = inside[valid], reach[valid]
    result = dict(iou=float((p & t).sum() / max(1, (p | t).sum())),
                  accuracy=float((p == t).mean()))
    boundary = pred.boundary
    tree = shapely.STRtree(shapely.points(np.asarray(requests, dtype=float))) if len(requests) else None
    for label, mask in (('fe', valid & reach & ~inside), ('fi', valid & ~reach & inside)):
        cells = np.argwhere(mask)
        distances = (shapely.distance(boundary, shapely.points(xs[cells[:, 1]], xs[cells[:, 0]]))
                     if len(cells) and not pred.is_empty else np.full(len(cells), np.inf))
        depth = np.zeros(mask.shape)
        depth[mask] = distances
        big = []
        for cells_of_part in parts(mask):
            area = len(cells_of_part) * cell_m2
            if area < MIN_PART_M2:
                continue
            cy, cx = xs[cells_of_part[:, 0]].mean(), xs[cells_of_part[:, 1]].mean()
            nearest = None
            if tree is not None:
                index = tree.nearest(shapely.Point(cx, cy))
                nearest = float(shapely.distance(shapely.Point(cx, cy), tree.geometries[index]))
            big.append(dict(area_ha=area / 1e4, r_m=float(math.hypot(cx, cy)),
                            azimuth_deg=float(math.degrees(math.atan2(cy, cx)) % 360),
                            depth_m=float(depth[cells_of_part[:, 0], cells_of_part[:, 1]].max()),
                            nearest_request_m=nearest,
                            beyond_crossing=None if beyond is None else
                            float(beyond[cells_of_part[:, 0], cells_of_part[:, 1]].mean())))
        big.sort(key=lambda part: -part['area_ha'])
        result[label] = dict(area_ha=float(len(cells) * cell_m2 / 1e4), bands=band_shares(distances),
                             parts=big, beyond_crossing=None if beyond is None or not len(cells)
                             else float(beyond[mask].mean()))
    if beyond is not None:
        result['truth_beyond_crossing'] = float(beyond[valid & reach].mean())
    return result


def analyze_dir(root):
    runs = sorted((root / 'runs').glob('*.json'))
    truths, beyonds, out = {}, {}, []
    for path in runs:
        run = json.loads(path.read_text('utf-8'))
        name = run['origin']
        if name not in truths:
            data = np.load(root / 'truth' / f'{name}.npz')
            truths[name] = (data['xs'], data['valid'], data['reach'])
            beyonds[name] = beyond_first_crossing(*truths[name])
        xs, valid, reach = truths[name]
        projection = LocalProjection(tuple(run['origin_bd09']))
        requests = [projection.to_local(p) for p in run['requests']]
        pred = to_local(run['geometry'], run['origin_bd09']) if run['geometry'] else shapely.Polygon()
        row = anatomy(pred, xs, valid, reach, requests, beyonds[name])
        loop = run.get('refinementLoop') or {}
        row.update(origin=name, budget=run['budget'], arm=run['arm'], calls=len(run['requests']),
                   stop=loop.get('stopReason'), counts=loop.get('counts'))
        out.append(row)
    return out


def report(rows, baseline_rows=None):
    base = {(r['origin'], r['budget']): r for r in (baseline_rows or [])}
    lines = ['| budget | arm | mean IoU | ΔIoU vs base | FE ha | FI ha | FE >100 m | FI >100 m | FE parts ≥0.25 ha | calls |',
             '|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|']
    groups = {}
    for r in rows:
        groups.setdefault((r['budget'], r['arm']), []).append(r)
    for (budget, arm), items in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1] != 'loop', kv[0][1])):
        deltas = [r['iou'] - base[(r['origin'], budget)]['iou'] for r in items if (r['origin'], budget) in base]
        delta = f'{statistics.fmean(deltas):+.4f}' if deltas else '—'

        def mean(value):
            return statistics.fmean(value(r) for r in items)
        lines.append(f"| {budget} | {arm} | {mean(lambda r: r['iou']):.4f} | {delta} | "
                     f"{mean(lambda r: r['fe']['area_ha']):.2f} | {mean(lambda r: r['fi']['area_ha']):.2f} | "
                     f"{mean(lambda r: r['fe']['bands'][-1]):.1%} | {mean(lambda r: r['fi']['bands'][-1]):.1%} | "
                     f"{mean(lambda r: len(r['fe']['parts'])):.1f} | {mean(lambda r: r['calls']):.0f} |")
    lines += ['', '| origin | budget | arm | IoU | ΔIoU | FE >100 m | largest FE part (ha, depth m, nearest request m) | stop |',
              '|---|---:|---|---:|---:|---:|---|---|']
    for r in sorted(rows, key=lambda r: (r['origin'], r['budget'], r['arm'] != 'loop', r['arm'])):
        b = base.get((r['origin'], r['budget']))
        part = r['fe']['parts'][0] if r['fe']['parts'] else None
        described = '—'
        if part is not None:
            nearest = '—' if part['nearest_request_m'] is None else f"{part['nearest_request_m']:.0f}"
            described = f"{part['area_ha']:.2f}, {part['depth_m']:.0f}, {nearest}"
        lines.append(f"| {r['origin']} | {r['budget']} | {r['arm']} | {r['iou']:.4f} | "
                     f"{'—' if b is None else format(r['iou'] - b['iou'], '+.4f')} | {r['fe']['bands'][-1]:.1%} | "
                     f"{described} | {r['stop']} |")
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    analyze = sub.add_parser('analyze')
    analyze.add_argument('--run', type=Path, required=True)
    analyze.add_argument('--baseline', type=Path, help='another run directory (default: this one)')
    analyze.add_argument('--baseline-arm', default='loop')
    args = parser.parse_args()
    rows = analyze_dir(args.run)
    source = analyze_dir(args.baseline) if args.baseline else rows
    baseline = [r for r in source if r['arm'] == args.baseline_arm]
    dump(args.run / 'anatomy.json', rows)
    text = report(rows, baseline)
    (args.run / 'anatomy.md').write_text(text + '\n', encoding='utf-8')
    print(text)


if __name__ == '__main__':
    main()
