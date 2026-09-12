import asyncio
import json

import numpy as np
import pytest
from shapely.geometry import Point, box

from life_circle.baselines import compute_radial
from life_circle.engine import compute_isochrone
from life_circle.experiments import evaluate, run_suite
from life_circle.models import CancelToken, IsochroneRequest
from life_circle.providers import AnalyticProvider
from life_circle.scenarios import scenarios

ORIGIN = (116.4, 39.9)


def test_metrics_identical_and_unknown_counts_as_missed():
    truth = box(-100, -100, 100, 100)
    exact = evaluate(truth, truth, box(200, 200, 300, 300), 1600)
    assert exact["iou"] == 1 and exact["boundary_p95_m"] == 0
    half = box(0, -100, 100, 100)
    metrics = evaluate(half, truth, truth.difference(half), 1600)
    assert metrics["miss_rate"] == .5 and metrics["false_inclusion_rate"] == 0
    assert evaluate(half, truth, half, 1600, truncated=True)["boundary_p95_m"] is None


@pytest.mark.parametrize("budget", [200, 400, 800])
def test_baselines_budget_and_uniform_maximal_grid(budget):
    async def run():
        request = IsochroneRequest(ORIGIN, "bd09ll", budget=budget, expand=False)
        provider = AnalyticProvider(ORIGIN, lambda x, y: np.hypot(x, y) / 1.2)
        uniform = await compute_isochrone(request, provider, CancelToken(), method="uniform")
        radial = await compute_radial(request, AnalyticProvider(ORIGIN, lambda x, y: np.hypot(x, y) / 1.2), CancelToken())
        n = int(np.sqrt(budget + 1))
        expected = n * n - (1 if n % 2 else 0)
        assert uniform.statistics.requests == expected
        for result in (uniform, radial):
            assert result.statistics.requests <= budget
            assert result.local_geometry.is_valid
            assert result.local_geometry.area > 3_000_000
    asyncio.run(run())


def test_uniform_uses_smaller_grid_if_even_square_exceeds_budget():
    result = asyncio.run(compute_isochrone(IsochroneRequest(ORIGIN, "bd09ll", budget=195), AnalyticProvider(ORIGIN, lambda x, y: 500), method="uniform"))
    assert result.statistics.requests == 168


def test_synthetic_road_graph_barriers_and_failure_truth():
    cases = scenarios()
    river = cases["river_bridge"]
    assert river.observed(200, 0) > river.observed(0, 200)
    assert cases["wall_entrance"].observed(300, 0) > 300 / 1.2
    assert cases["global_failure"].observed(0, 0) is None
    assert cases["global_failure"].truth(0, 0) == 0


def test_suite_writes_reproducible_outputs(tmp_path):
    rows = asyncio.run(run_suite(tmp_path, names=["plane"], budgets=[200]))
    assert len(rows) == 5
    assert {row["method"] for row in rows} == {"adaptive", "uniform", "radial", "no_active", "no_exploration"}
    assert all(row["requests"] <= 200 and row["network_requests"] == 0 for row in rows)
    assert len(json.loads((tmp_path / "metrics.json").read_text(encoding="utf-8"))) == 5
    assert (tmp_path / "report.md").exists()
    assert len(list(tmp_path.glob("*.svg"))) == 5
