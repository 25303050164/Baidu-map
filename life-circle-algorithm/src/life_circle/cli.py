import argparse
import asyncio
import json
from dataclasses import replace
from pathlib import Path

from .baselines import compute_radial
from .engine import compute_isochrone
from .experiments import METHODS, run_suite
from .models import IsochroneRequest
from .providers import AnalyticProvider
from .scenarios import scenarios


def main():
    parser = argparse.ArgumentParser(description="15 分钟生活圈离线算法与对比实验（不调用真实 API）")
    sub = parser.add_subparsers(dest="command", required=True)
    suite = sub.add_parser("benchmark", help="三算法及两项消融")
    suite.add_argument("--scenarios", nargs="+")
    suite.add_argument("--budgets", nargs="+", type=int, default=[200, 400, 800])
    suite.add_argument("--output", type=Path, required=True)
    sample = sub.add_parser("compute", help="单个合成场景")
    sample.add_argument("--scenario", default="plane")
    sample.add_argument("--method", choices=METHODS, default="adaptive")
    sample.add_argument("--budget", type=int, default=400)
    sample.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "benchmark":
        rows = asyncio.run(run_suite(args.output, names=args.scenarios, budgets=args.budgets, progress=lambda message: print(message, flush=True)))
        gate = [r for r in rows if r["scenario"] == "plane" and r["method"] == "adaptive" and r["budget"] == 800]
        if any(r["iou"] < .95 or r["boundary_p95_m"] is None or r["boundary_p95_m"] > 75 for r in gate):
            raise SystemExit("匀速回归门槛未通过，见报告")
    else:
        cases = scenarios()
        if args.scenario not in cases:
            parser.error("未知场景")
        request = IsochroneRequest((116.4, 39.9), "bd09ll", budget=args.budget)
        if args.method == "no_active":
            request = replace(request, active_sampling=False)
        elif args.method == "no_exploration":
            request = replace(request, exploration_fraction=0)
        provider = AnalyticProvider(request.origin, cases[args.scenario].observed)
        if args.method == "radial":
            result = asyncio.run(compute_radial(request, provider))
        else:
            result = asyncio.run(compute_isochrone(request, provider, method="uniform" if args.method == "uniform" else "adaptive"))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result.to_dict(), ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
