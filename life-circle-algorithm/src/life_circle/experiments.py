import csv
import html
import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import shapely
from shapely.geometry import MultiPolygon, Point, box

from .baselines import compute_radial
from .engine import compute_isochrone
from .field import contour_field, multipolygon
from .models import CancelToken, IsochroneRequest
from .providers import AnalyticProvider
from .scenarios import scenarios

METHODS = ("adaptive", "uniform", "radial", "no_active", "no_exploration")


def _boundary_points(geometry):
    arrays = []
    for polygon in multipolygon(geometry).geoms:
        for ring in [polygon.exterior, *polygon.interiors]:
            distances = np.linspace(0, ring.length, max(2, int(np.ceil(ring.length / 5))), endpoint=False)
            arrays.extend(shapely.line_interpolate_point(ring, distances))
    return arrays


def evaluate(prediction, truth, unknown, extent, *, truncated=False):
    prediction = prediction if prediction is not None else MultiPolygon()
    intersection = prediction.intersection(truth).area
    union = prediction.union(truth).area
    p95 = None
    if not truncated and not prediction.is_empty and not truth.is_empty:
        distances = np.concatenate([
            shapely.distance(_boundary_points(prediction), truth.boundary),
            shapely.distance(_boundary_points(truth), prediction.boundary),
        ])
        p95 = float(np.percentile(distances, 95))
    return {
        "iou": intersection / union if union else 1.,
        "false_inclusion_rate": 1 - intersection / prediction.area if prediction.area else 0.,
        "miss_rate": 1 - intersection / truth.area if truth.area else 0.,
        "boundary_p95_m": p95,
        "unknown_fraction": unknown.area / (2 * extent) ** 2,
        "components": len(multipolygon(prediction).geoms),
        "holes": sum(len(p.interiors) for p in multipolygon(prediction).geoms),
    }


def reference_geometry(scenario, extent=1600):
    if scenario.name in ("plane", "local_failure", "global_failure"):
        return Point(0, 0).buffer(1080, quad_segs=1024)
    # Separate dense reference; never exposed to the provider or sampler.
    axis = np.arange(-extent, extent + 1, 10.)
    x, y = np.meshgrid(axis, axis)
    return contour_field(axis, axis, scenario.truth(x, y), box(-extent, -extent, extent, extent))


def write_svg(path, prediction, truth, unknown, title, extent=1600):
    def draw(geometry, color, opacity):
        parts = []
        for polygon in multipolygon(geometry).geoms:
            rings = []
            for ring in [polygon.exterior, *polygon.interiors]:
                coordinates = list(ring.coords)
                rings.append("M " + " L ".join(f"{x:.3f},{-y:.3f}" for x, y in coordinates) + " Z")
            parts.append(f'<path d="{" ".join(rings)}" fill="{color}" fill-opacity="{opacity}" fill-rule="evenodd" stroke="{color}" stroke-width="5"/>')
        return "".join(parts)
    prediction = prediction if prediction is not None else MultiPolygon()
    path.write_text(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{-extent-80} {-extent-240} {2*extent+160} {2*extent+480}">'
        '<rect x="-1800" y="-1900" width="3600" height="3800" fill="white"/>'
        f'<text x="{-extent}" y="{-extent-130}" font-size="65">{html.escape(title)}</text>'
        + draw(unknown, "#94a3b8", .35) + draw(truth, "#16a34a", .18) + draw(prediction, "#2563eb", .30)
        + f'<circle r="15" fill="#111"/><text x="{-extent}" y="{extent+140}" font-size="55">绿：参考真值　蓝：估计可达　灰：未知（局部米制）</text></svg>',
        encoding="utf-8",
    )


async def run_suite(output, *, names=None, budgets=(200, 400, 800), progress=None):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    cases = scenarios()
    names = list(cases) if names is None else names
    if any(name not in cases for name in names):
        raise ValueError("未知合成场景")
    rows = []
    for name in names:
        scenario = cases[name]
        truth = reference_geometry(scenario)
        for budget in budgets:
            for method in METHODS:
                request = IsochroneRequest((116.4, 39.9), "bd09ll", budget=budget, expand=False)
                if method == "no_active":
                    request = replace(request, active_sampling=False)
                if method == "no_exploration":
                    request = replace(request, exploration_fraction=0)
                provider = AnalyticProvider(request.origin, scenario.observed)
                if method == "radial":
                    result = await compute_radial(request, provider, CancelToken())
                else:
                    result = await compute_isochrone(request, provider, CancelToken(), method="uniform" if method == "uniform" else "adaptive")
                unknown = result.local_unknown if result.local_unknown is not None else box(-1600, -1600, 1600, 1600)
                metrics = evaluate(result.local_geometry, truth, unknown, 1600, truncated="range_truncated" in result.warnings or truth.distance(box(-1600, -1600, 1600, 1600).boundary) < 1e-7)
                row = {"scenario": name, "method": method, "budget": budget, **metrics,
                       "requests": result.statistics.requests, "network_requests": result.statistics.network_requests,
                       "unique_positions": result.statistics.unique_positions, "retries": result.statistics.retries,
                       "seconds": result.statistics.total_seconds, "compute_seconds": result.statistics.compute_seconds,
                       "quality": result.quality, "stop_reason": result.stop_reason,
                       "active_points": result.statistics.active_points, "exploration_requests": result.statistics.exploration_requests,
                       "warnings": ",".join(result.warnings)}
                rows.append(row)
                stem = f"{name}-{budget}-{method}"
                (output / f"{stem}.json").write_text(json.dumps(result.to_dict(), ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8")
                write_svg(output / f"{stem}.svg", result.local_geometry, truth, unknown, stem)
            if progress:
                progress(f"{name}: budget={budget}, 5 methods complete")
    (output / "metrics.json").write_text(json.dumps(rows, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8")
    with (output / "metrics.csv").open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]) if rows else [])
        writer.writeheader()
        writer.writerows(rows)
    report = ["# 自适应网格离线实验报告", "", "全部为合成实验，真实网络请求数为 0。中心点仅是局部坐标原点，不代表已确认的社区。",
              "", "三种方法及两项消融采用相同中心、±1600 米范围、预算和未知规则；公平实验关闭范围扩展。每次使用独立 Provider 和任务缓存。",
              "", "匀速及故障场景使用高精度圆形参考，其余使用独立 10 米参考栅格；路网场景是 50 米测试图。面积指标纳入未知造成的漏纳；边界 P95 按双向边界每 5 米采样，截断或空边界不报告。",
              "", "均匀基线查询预算可覆盖的最大规则点阵，格中心仅由四角推导（起点保留零锚点）；扇形基线为 32 方向×4 距离层，再局部补测，具有星形表达限制。",
              "", "| 场景 | 方法 | 预算/实际调用 | IoU | 误纳率 | 漏纳率 | 边界P95米 | 未知比例 | 质量 |", "|---|---|---:|---:|---:|---:|---:|---:|---|"]
    for row in rows:
        p95 = "—" if row["boundary_p95_m"] is None else f'{row["boundary_p95_m"]:.1f}'
        report.append(f'| {row["scenario"]} | {row["method"]} | {row["budget"]}/{row["requests"]} | {row["iou"]:.4f} | {row["false_inclusion_rate"]:.4f} | {row["miss_rate"]:.4f} | {p95} | {row["unknown_fraction"]:.4f} | {row["quality"]} |')
    report.extend(["", "## 场景说明", "", *[f"- {name}：{cases[name].description}" for name in names], "", "每条记录有同名 JSON 结果及 SVG 几何图，原始指标见 metrics.json / metrics.csv。复杂场景不设统一精度通过线，不能从匀速场景或总体均值推出普遍优势。"])
    gate = [row for row in rows if row["scenario"] == "plane" and row["method"] == "adaptive" and row["budget"] == 800]
    if gate:
        row = gate[0]
        passed = row["iou"] >= .95 and row["boundary_p95_m"] is not None and row["boundary_p95_m"] <= 75
        report.extend(["", f"匀速 800 次预算回归门槛：{'通过' if passed else '未通过'}。IoU={row['iou']:.6f}，边界 P95={row['boundary_p95_m']:.3f} 米。"])
    (output / "report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    return rows
