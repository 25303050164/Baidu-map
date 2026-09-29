"""Offline E8.2 benchmark through the production request shape; zero Baidu calls.

Every run goes through ``compute_e82`` with the request built by
``app.engines.baidu_e82.e82_request`` -- the same entry and parameters a checkup
uses -- so the numbers describe the production algorithm, not a research config.
Failed runs stay in every denominator.

    python -m tools.e82_benchmark run --output OUT --sets base development --budgets 400 800 --arms legacy
    python -m tools.e82_benchmark compare --baseline OUT_A --candidate OUT_B
"""
import argparse
import asyncio
import hashlib
import json
import math
import statistics
import subprocess
import time
from dataclasses import asdict
from pathlib import Path

import numpy as np
import shapely
from shapely.geometry import Polygon

from life_circle.field import GeometryError
from life_circle.models import CancelToken
from app.algorithms.baidu_e82 import compute_e82
from app.engines.baidu_e82 import e82_request
from tools.diagnostic_common import no_network
from tools.endpoint_e83_experiment import cases as stress_cases, metrics as shape_metrics
from tools.endpoint_multicross_experiment import CASES82, RotatedSynthetic, truth_for
from tools.endpoint_radial_experiment import ORIGIN, local
from tools.live_smoke import dump

REPO = Path(__file__).resolve().parents[2]
SETS = ('base', 'development', 'holdout')
# Point-classification grid for the whole computation square and the boundary band.
GRID_M = 20
BAND_M = 50


def case_set(name):
    if name == 'base':
        return [(case, truth_for(case), lambda c=case: RotatedSynthetic(c)) for case in CASES82]
    return stress_cases(name)


def family(case):
    return case.split('-')[0]


def freeze():
    """Code identity of this run: the commit plus a hash of any uncommitted diff."""
    def git(*args):
        return subprocess.run(['git', *args], cwd=REPO, capture_output=True, text=True, check=True).stdout
    diff = git('diff', 'HEAD', '--', 'backend', 'life-circle-algorithm')
    return dict(commit=git('rev-parse', 'HEAD').strip(), dirty=bool(diff.strip()),
                diff_sha256=hashlib.sha256(diff.encode()).hexdigest())


def classification(pred, truth, extent):
    xs = np.arange(-extent + GRID_M / 2, extent, GRID_M)
    x, y = np.meshgrid(xs, xs)
    x, y = x.ravel(), y.ravel()
    actual = shapely.contains_xy(truth, x, y)
    predicted = shapely.contains_xy(pred, x, y) if not pred.is_empty else np.zeros_like(actual)
    band = shapely.dwithin(truth.boundary, shapely.points(x, y), BAND_M)
    agree = actual == predicted
    return dict(grid_accuracy=float(agree.mean()),
                band_accuracy=float(agree[band].mean()) if band.any() else None,
                band_false_inclusion=int((predicted & ~actual & band).sum()),
                band_false_exclusion=int((~predicted & actual & band).sum()))


async def run_one(case, truth, factory, budget, arm):
    provider = factory()
    request = e82_request(ORIGIN, budget)
    options = {} if arm == 'legacy' else {'refinement': arm}
    started = time.perf_counter()
    row = dict(case=case, family=family(case), budget=budget, arm=arm)
    try:
        result = await compute_e82(request, provider, CancelToken(), **options)
    except GeometryError as error:
        return dict(row, calls=provider.calls, elapsed_s=time.perf_counter() - started,
                    valid=False, failed=True, failure=str(error)), None
    elapsed = time.perf_counter() - started
    payload = result.to_dict()
    evidence = payload.get('evidence', {})
    raw = dict(geometry=payload['geometry'], unknownRegion=payload['unknownRegion'],
               observationEvidence=evidence.get('observationEvidence', []),
               directions=evidence.get('directions', []), localRepair=evidence.get('localRepair', {}))
    measured = shape_metrics(raw, truth)
    log = raw['observationEvidence']
    accepted = [e for e in log if e['accepted']]
    pred = local(payload['geometry']) if payload['geometry'] is not None else Polygon()
    loop = evidence.get('refinementLoop') or {}
    row.update(calls=provider.calls, elapsed_s=elapsed,
               new_endpoints=len({tuple(e['route_destination']) for e in accepted}),
               rejected=len(log) - len(accepted), quality=payload['quality'],
               stop_reason=payload['stopReason'], warnings=payload['warnings'],
               completion=evidence.get('completion'),
               carved_m2=loop.get('carvedAreaM2'), loop_actions=loop.get('actions'), **measured)
    if measured.get('valid'):
        row.update(classification(pred, truth, request.extent))
    assert provider.calls <= budget, 'budget exceeded'
    return row, request


def summarize(rows):
    groups = {}
    for row in rows:
        groups.setdefault((row['set'], row['budget'], row['arm']), []).append(row)
    lines = ['| set | budget | arm | runs | failed | mean IoU | min IoU | mean P95 truth→outer (m) | '
             'mean P95 outer→truth (m) | band acc | grid acc | mean calls | mean s |',
             '|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|']
    for (name, budget, arm), items in sorted(groups.items()):
        ok = [r for r in items if r.get('valid') and not r.get('failed')]

        def mean(key, source=ok):
            values = [r[key] for r in source if r.get(key) is not None]
            return statistics.fmean(values) if values else float('nan')
        p95_in = [r['truth_to_outer']['p95_m'] for r in ok if r.get('truth_to_outer')]
        p95_out = [r['outer_to_truth']['p95_m'] for r in ok if r.get('outer_to_truth')]
        ious = [r.get('iou', 0) if r.get('valid') else 0 for r in items]
        lines.append(f"| {name} | {budget} | {arm} | {len(items)} | {sum(bool(r.get('failed')) for r in items)} | "
                     f"{statistics.fmean(ious):.4f} | {min(ious):.4f} | "
                     f"{statistics.fmean(p95_in) if p95_in else float('nan'):.1f} | "
                     f"{statistics.fmean(p95_out) if p95_out else float('nan'):.1f} | "
                     f"{mean('band_accuracy'):.4f} | {mean('grid_accuracy'):.4f} | "
                     f"{mean('calls', items):.1f} | {mean('elapsed_s', items):.2f} |")
    return '\n'.join(lines)


async def run(args):
    args.output.mkdir(parents=True, exist_ok=False)
    identity = freeze()
    rows, requests = [], {}
    for name in args.sets:
        for case, truth, factory in case_set(name):
            for budget in args.budgets:
                for arm in args.arms:
                    row, request = await run_one(case, truth, factory, budget, arm)
                    row['set'] = name
                    rows.append(row)
                    if request is not None:
                        requests.setdefault(str(budget), asdict(request))
                    print(json.dumps({k: row.get(k) for k in ('set', 'case', 'budget', 'arm', 'calls', 'iou', 'failed')}),
                          flush=True)
    dump(args.output / 'protocol.json', dict(sets=args.sets, budgets=args.budgets, arms=args.arms,
         effective_requests=requests, grid_m=GRID_M, band_m=BAND_M, live_calls=0, **identity))
    dump(args.output / 'metrics.json', rows)
    (args.output / 'summary.md').write_text(summarize(rows) + '\n', encoding='utf-8')
    if freeze() != identity:
        raise SystemExit('source changed during the benchmark; results are not attributable')
    print(summarize(rows))


def compare(args):
    base = {(r['set'], r['case'], r['budget']): r for r in json.loads((args.baseline / 'metrics.json').read_text('utf-8'))}
    cand = json.loads((args.candidate / 'metrics.json').read_text('utf-8'))
    lines = ['| set | case | budget | arm | ΔIoU | ΔP95 truth→outer (m) | ΔP95 outer→truth (m) | Δband acc | calls base→cand |',
             '|---|---|---:|---|---:|---:|---:|---:|---|']
    worse = []
    for r in cand:
        b = base.get((r['set'], r['case'], r['budget']))
        if b is None:
            continue
        def get(row, key, sub=None):
            value = row.get(key)
            if sub and isinstance(value, dict):
                value = value.get(sub)
            return value
        def delta(key, sub=None):
            x, y = get(r, key, sub), get(b, key, sub)
            return None if x is None or y is None else x - y
        d_iou = (r.get('iou') or 0) - (b.get('iou') or 0)
        d_in, d_out, d_band = delta('truth_to_outer', 'p95_m'), delta('outer_to_truth', 'p95_m'), delta('band_accuracy')
        fmt = lambda v, f: '—' if v is None else format(v, f)
        lines.append(f"| {r['set']} | {r['case']} | {r['budget']} | {r['arm']} | {d_iou:+.4f} | {fmt(d_in, '+.1f')} | "
                     f"{fmt(d_out, '+.1f')} | {fmt(d_band, '+.4f')} | {b['calls']}→{r['calls']} |")
        if d_iou < -0.01 or (d_in or 0) > 10 or (d_out or 0) > 10 or (r.get('failed') and not b.get('failed')):
            worse.append(f"{r['set']}/{r['case']}@{r['budget']}")
    text = '\n'.join(lines) + f"\n\nGuardrail alarms ({len(worse)}): {', '.join(worse) or 'none'}\n"
    if args.output:
        args.output.write_text(text, encoding='utf-8')
    print(text)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    runner = sub.add_parser('run')
    runner.add_argument('--output', type=Path, required=True)
    runner.add_argument('--sets', nargs='+', choices=SETS, default=['base', 'development'])
    runner.add_argument('--budgets', nargs='+', type=int, default=[400, 800])
    runner.add_argument('--arms', nargs='+', default=['legacy'])
    comparer = sub.add_parser('compare')
    comparer.add_argument('--baseline', type=Path, required=True)
    comparer.add_argument('--candidate', type=Path, required=True)
    comparer.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.command == 'compare':
        return compare(args)

    async def guarded():
        with no_network():
            await run(args)
    asyncio.run(guarded())


if __name__ == '__main__':
    main()
