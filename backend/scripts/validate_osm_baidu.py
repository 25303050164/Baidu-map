"""Validate the OSM_OFFLINE 15-minute result against live Baidu walking routes.

The script deliberately makes one Baidu request per case (no retry) and keeps
the API key out of every output file. It uses the real, versioned local OSM
cache and compares point membership in the OSM polygon with Baidu's
``duration <= 900`` classification.

Run from ``backend``::

    .venv/Scripts/python.exe scripts/validate_osm_baidu.py --cases 60
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import html
import json
import math
from pathlib import Path
import platform
import subprocess
import sys
import time

import httpx
import numpy as np
from shapely.geometry import LineString, Point, box

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
sys.path.insert(0, str(BACKEND))

from app.algorithms.osm_offline.engine import OsmOfflineEngine  # noqa: E402
from app.config import Settings  # noqa: E402
from app.geo.coordinates import wgs84_to_bd09  # noqa: E402
from life_circle.models import IsochroneRequest, RouteObservation  # noqa: E402
from life_circle.providers import BaiduProvider  # noqa: E402


ORIGIN = (121.51108, 31.20415)  # existing Shanghai sample A, BD09LL
THRESHOLD_S = 900.0
DEFAULT_CASES = 60


def file_sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def geometry_parts(geometry, kind: str):
    if geometry is None or geometry.is_empty:
        return
    if geometry.geom_type == kind:
        yield geometry
    elif hasattr(geometry, "geoms"):
        for child in geometry.geoms:
            yield from geometry_parts(child, kind)


def interpolate_lines(geometry, fraction: float) -> Point:
    """Length-weighted interpolation over a LineString/MultiLineString."""
    lines = list(geometry_parts(geometry, "LineString"))
    if not lines:
        raise ValueError("reachable_network_empty")
    lengths = [line.length for line in lines]
    total = sum(lengths)
    target = min(max(float(fraction), 0.0), 1.0) * total
    walked = 0.0
    for line, length in zip(lines, lengths):
        if target <= walked + length or line is lines[-1]:
            return line.interpolate(max(0.0, target - walked))
        walked += length
    return lines[-1].interpolate(lines[-1].length)


def metric_to_bd09(projection, point: Point) -> tuple[float, float]:
    lng, lat = projection.inverse.transform(point.x, point.y, errcheck=True)
    return tuple(round(float(v), 6) for v in wgs84_to_bd09(lng, lat))


def generate_cases(engine: OsmOfflineEngine, result, origin: tuple[float, float], count: int) -> list[dict]:
    """Create a deterministic 3-way stratified sample around the OSM polygon."""
    if count < 50 or count > 80:
        raise ValueError("cases_must_be_between_50_and_80")
    polygon = result.result.local_geometry
    network = result.reachable_network
    if polygon is None or polygon.is_empty or network is None or network.is_empty:
        raise ValueError("osm_result_has_no_geometry")

    n_network = count // 3
    n_boundary = count // 3
    n_ring = count - n_network - n_boundary
    projection = engine.store.projection
    origin_xy = np.asarray(projection.origin(origin), dtype=float)
    reference = polygon.representative_point()
    rows: list[dict] = []
    used: set[tuple[float, float]] = set()

    def add(group: str, index: int, point: Point, target_tag: str, radius_m: float | None = None):
        lng, lat = metric_to_bd09(projection, point)
        # Rounded BD09 coordinates are what the API receives. Keep them unique.
        if (lng, lat) in used:
            angle = (index + 1) * 0.73
            point = Point(point.x + 8.0 * math.cos(angle), point.y + 8.0 * math.sin(angle))
            lng, lat = metric_to_bd09(projection, point)
        used.add((lng, lat))
        rows.append(
            {
                "id": f"{group}-{index + 1:02d}",
                "group": group,
                "target": target_tag,
                "lng": lng,
                "lat": lat,
                "metric_x": float(point.x),
                "metric_y": float(point.y),
                "offset_e_m": float(point.x - origin_xy[0]),
                "offset_n_m": float(point.y - origin_xy[1]),
                "distance_from_origin_m": float(point.distance(Point(*origin_xy))),
                "radius_m": radius_m,
            }
        )

    # Points sampled directly from reachable OSM roads. They should represent
    # the positive class and exercise the route endpoint handling in Baidu.
    for i in range(n_network):
        point = interpolate_lines(network, (i + 0.5) / n_network)
        add("road", i, point, "路网内")

    # Sample the OSM boundary and move outward by a fixed metric offset. The
    # loop handles narrow islands/holes where the first offset remains inside.
    for i in range(n_boundary):
        boundary_point = interpolate_lines(polygon.boundary, (i + 0.5) / n_boundary)
        dx, dy = boundary_point.x - reference.x, boundary_point.y - reference.y
        norm = math.hypot(dx, dy)
        if norm < 1e-6:
            dx, dy, norm = 1.0, 0.0, 1.0
        point = boundary_point
        offset = 180.0
        for _ in range(5):
            point = Point(boundary_point.x + dx / norm * offset, boundary_point.y + dy / norm * offset)
            if not polygon.covers(point):
                break
            offset *= 1.6
        add("boundary", i, point, "边界外侧", offset)

    # A reproducible outer ring provides clearly negative cases while staying
    # close enough for the Baidu walking service to return useful routes.
    for i in range(n_ring):
        angle = 2.0 * math.pi * (i + 0.5) / n_ring
        radius = 1450.0 if i % 2 == 0 else 1800.0
        point = Point(origin_xy[0] + radius * math.cos(angle), origin_xy[1] + radius * math.sin(angle))
        while polygon.covers(point) and radius < 3000:
            radius += 250.0
            point = Point(origin_xy[0] + radius * math.cos(angle), origin_xy[1] + radius * math.sin(angle))
        add("ring", i, point, "外环", radius)

    if len(rows) != count:
        raise AssertionError(f"expected {count} cases, got {len(rows)}")
    for row in rows:
        row["osm_inside"] = bool(polygon.covers(Point(row["metric_x"], row["metric_y"])))
    return rows


async def query_baidu(settings: Settings, cases: list[dict], origin: tuple[float, float]) -> list[dict]:
    """Run one paced, non-retried request for every case and return safe fields."""
    if not settings.ak_configured:
        raise RuntimeError("baidu_map_ak_not_configured")
    qps = float(settings.analysis_qps or 2.0)
    interval = 1.0 / qps
    responses: list[dict] = []
    transport_errors: list[str] = []

    async def capture_response(response: httpx.Response):
        safe = {"http_status": int(response.status_code), "baidu_status": None}
        try:
            await response.aread()
            payload = response.json()
            if isinstance(payload, dict) and type(payload.get("status")) is int:
                safe["baidu_status"] = int(payload["status"])
        except (ValueError, TypeError, json.JSONDecodeError):
            safe["invalid_json"] = True
        except Exception:
            safe["invalid_json"] = True
        responses.append(safe)

    class ObservedClient:
        """Keep only exception class names when the transport cannot connect."""

        def __init__(self, inner):
            self.inner = inner

        async def get(self, *args, **kwargs):
            try:
                return await self.inner.get(*args, **kwargs)
            except Exception as exc:
                transport_errors.append(type(exc).__name__)
                raise

    # The provider's normal client defaults are retained so configured desktop
    # proxies can be used. Redirects remain disabled and no response is saved.
    async with httpx.AsyncClient(follow_redirects=False, event_hooks={"response": [capture_response]}) as client:
        provider = BaiduProvider(settings.baidu_map_ak.get_secret_value(), client=ObservedClient(client), route_metric="duration")
        last_start = None
        for index, row in enumerate(cases, start=1):
            if last_start is not None:
                await asyncio.sleep(max(0.0, interval - (time.monotonic() - last_start)))
            last_start = time.monotonic()
            started = time.perf_counter()
            event_index = len(responses)
            transport_index = len(transport_errors)
            try:
                observation = await provider.query_walking_time(
                    origin,
                    (row["lng"], row["lat"]),
                    time.monotonic() + 15.0,
                )
            except Exception:
                observation = RouteObservation((row["lng"], row["lat"]), reason="client_error")
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            event = responses[event_index] if event_index < len(responses) else {}
            duration = observation.duration
            row.update(
                {
                    "baidu_duration_s": None if duration is None else round(float(duration), 3),
                    "baidu_inside": None if duration is None else bool(duration <= THRESHOLD_S),
                    "baidu_reason": observation.reason,
                    "endpoint_verified": bool(duration is not None and observation.endpoint_verified),
                    "baidu_distance_m": None if observation.distance_m is None else round(float(observation.distance_m), 2),
                    "http_status": event.get("http_status"),
                    "baidu_status": event.get("baidu_status"),
                    "transport_error": transport_errors[-1] if len(transport_errors) > transport_index else None,
                    "elapsed_ms": round(elapsed_ms, 1),
                }
            )
            if index == 1 or index % 10 == 0 or index == len(cases):
                print(f"Baidu cases {index}/{len(cases)}", flush=True)
    return cases


def classify(row: dict) -> str:
    if row.get("baidu_inside") is None:
        return "unknown"
    if row["osm_inside"] and row["baidu_inside"]:
        return "true_positive"
    if (not row["osm_inside"]) and (not row["baidu_inside"]):
        return "true_negative"
    if row["osm_inside"] and not row["baidu_inside"]:
        return "false_positive"
    return "false_negative"


def classification_metrics(rows: list[dict]) -> dict:
    valid = [row for row in rows if row.get("baidu_inside") is not None]
    tp = sum(row["osm_inside"] and row["baidu_inside"] for row in valid)
    tn = sum((not row["osm_inside"]) and (not row["baidu_inside"]) for row in valid)
    fp = sum(row["osm_inside"] and (not row["baidu_inside"]) for row in valid)
    fn = sum((not row["osm_inside"]) and row["baidu_inside"] for row in valid)
    n = len(valid)
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    specificity = tn / (tn + fp) if tn + fp else None
    accuracy = (tp + tn) / n if n else None
    f1 = 2 * precision * recall / (precision + recall) if precision is not None and recall is not None and precision + recall else None
    balanced = (recall + specificity) / 2 if recall is not None and specificity is not None else None
    mismatches = [row for row in valid if row["osm_inside"] != row["baidu_inside"]]
    margins = [abs(float(row["baidu_duration_s"]) - THRESHOLD_S) for row in mismatches]
    return {
        "total": len(rows),
        "valid_baidu": n,
        "unknown_baidu": len(rows) - n,
        "unknown_rate": (len(rows) - n) / len(rows) if rows else None,
        "tp": int(tp),
        "tn": int(tn),
        "fp": int(fp),
        "fn": int(fn),
        "accuracy": accuracy,
        "precision": precision,
        "recall": recall,
        "specificity": specificity,
        "f1": f1,
        "balanced_accuracy": balanced,
        "mismatches": len(mismatches),
        "mismatch_rate": len(mismatches) / n if n else None,
        "mismatch_threshold_margin_s_median": float(np.median(margins)) if margins else None,
        "mismatch_threshold_margin_s_min": float(min(margins)) if margins else None,
    }


def all_metrics(rows: list[dict]) -> dict:
    for row in rows:
        row["classification"] = classify(row)
    groups = {name: classification_metrics([row for row in rows if row["group"] == name]) for name in ("road", "boundary", "ring")}
    by_reason = Counter(row.get("baidu_reason") or "ok" for row in rows)
    by_http = Counter(str(row.get("http_status")) if row.get("http_status") is not None else "none" for row in rows)
    by_status = Counter(str(row.get("baidu_status")) if row.get("baidu_status") is not None else "none" for row in rows)
    by_transport = Counter(row.get("transport_error") or "none" for row in rows)
    durations = [row["baidu_duration_s"] for row in rows if row.get("baidu_duration_s") is not None]
    return {
        "overall": classification_metrics(rows),
        "groups": groups,
        "reason_counts": dict(sorted(by_reason.items())),
        "http_status_counts": dict(sorted(by_http.items())),
        "baidu_status_counts": dict(sorted(by_status.items())),
        "transport_error_counts": dict(sorted(by_transport.items())),
        "endpoint_verified_count": sum(bool(row.get("endpoint_verified")) for row in rows),
        "duration_seconds": {
            "min": min(durations) if durations else None,
            "median": float(np.median(durations)) if durations else None,
            "max": max(durations) if durations else None,
        },
        "total_elapsed_seconds": round(sum(row.get("elapsed_ms") or 0 for row in rows) / 1000.0, 2),
    }


def setup_plotting():
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager

    font_path = Path("C:/Windows/Fonts/msyh.ttc")
    if font_path.exists():
        font_manager.fontManager.addfont(str(font_path))
        plt.rcParams["font.family"] = font_manager.FontProperties(fname=str(font_path)).get_name()
    plt.rcParams.update(
        {
            "axes.unicode_minus": False,
            "font.size": 10,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "figure.facecolor": "#ffffff",
            "axes.facecolor": "#f8fafc",
            "text.color": "#15263d",
            "axes.labelcolor": "#475569",
            "axes.edgecolor": "#cbd5e1",
            "xtick.color": "#64748b",
            "ytick.color": "#64748b",
            "savefig.facecolor": "#ffffff",
        }
    )
    return plt


def draw_geometry_lines(ax, geometry, origin_xy, **style):
    from matplotlib.collections import LineCollection

    values = [np.asarray(line.coords)[:, :2] - origin_xy for line in geometry_parts(geometry, "LineString")]
    if values:
        ax.add_collection(LineCollection(values, **style))


def draw_geometry_polygons(ax, geometry, origin_xy):
    from matplotlib.patches import Polygon as PlotPolygon

    for poly in geometry_parts(geometry, "Polygon"):
        coords = np.asarray(poly.exterior.coords)[:, :2] - origin_xy
        ax.add_patch(PlotPolygon(coords, closed=True, facecolor="#bfdbfe", edgecolor="#60a5fa", linewidth=0.6, alpha=0.55))


def make_figures(output: Path, engine: OsmOfflineEngine, result, rows: list[dict]) -> None:
    plt = setup_plotting()
    origin_xy = np.asarray(engine.store.projection.origin(ORIGIN), dtype=float)
    polygon, network = result.result.local_geometry, result.reachable_network
    colors = {
        "true_positive": "#16866f",
        "true_negative": "#64748b",
        "false_positive": "#d97706",
        "false_negative": "#7c3aed",
        "unknown": "#94a3b8",
    }
    labels = {
        "true_positive": "双方判定在 15 分钟内",
        "true_negative": "双方判定超过 15 分钟",
        "false_positive": "OSM 内、百度外",
        "false_negative": "OSM 外、百度内",
        "unknown": "百度无有效路线",
    }

    fig, ax = plt.subplots(figsize=(10.5, 8.5))
    draw_geometry_polygons(ax, polygon, origin_xy)
    draw_geometry_lines(ax, network, origin_xy, colors="#167d69", linewidths=1.0, alpha=0.9, zorder=2)
    # Show a light local road context from the validated cache.
    extent = box(origin_xy[0] - 2200, origin_xy[1] - 2200, origin_xy[0] + 2200, origin_xy[1] + 2200)
    for idx in engine.store.index.query(extent, predicate="intersects"):
        road = engine.store.geometries[int(idx)]
        draw_geometry_lines(ax, road.intersection(extent), origin_xy, colors="#cbd5e1", linewidths=0.45, alpha=0.45, zorder=1)
    # Redraw the OSM result above the context.
    draw_geometry_polygons(ax, polygon, origin_xy)
    draw_geometry_lines(ax, network, origin_xy, colors="#167d69", linewidths=1.0, alpha=0.9, zorder=2)
    for row in rows:
        x, y = row["offset_e_m"], row["offset_n_m"]
        ax.scatter(x, y, s=34, color=colors[row["classification"]], edgecolors="white", linewidths=0.45, zorder=4)
    ax.scatter(0, 0, marker="*", s=170, color="#dc2626", edgecolors="white", linewidths=0.8, zorder=5)
    ax.set_aspect("equal", adjustable="box")
    ax.set_xlabel("相对起点向东 / m")
    ax.set_ylabel("相对起点向北 / m")
    ax.set_title("百度 API × OSM_OFFLINE · 60 个测试点空间分布", loc="left", fontsize=14, pad=14)
    ax.grid(color="#e2e8f0", linewidth=0.5, zorder=0)
    from matplotlib.lines import Line2D
    from matplotlib.patches import Patch

    legend = [Patch(facecolor="#bfdbfe", edgecolor="#60a5fa", alpha=0.55, label="OSM 15 分钟展示面")]
    legend.extend(Line2D([0], [0], marker="o", linestyle="", color=color, markeredgecolor="white", label=labels[key]) for key, color in colors.items())
    ax.legend(handles=legend, loc="upper center", bbox_to_anchor=(0.5, -0.11), ncol=3, frameon=False, fontsize=9)
    fig.subplots_adjust(left=0.09, right=0.98, top=0.9, bottom=0.2)
    fig.savefig(output / "validation-points.png", dpi=160)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(11, 5.5))
    x = np.arange(1, len(rows) + 1)
    for key, color in colors.items():
        selected = [i for i, row in enumerate(rows) if row["classification"] == key and row.get("baidu_duration_s") is not None]
        if selected:
            ax.scatter(x[selected], [rows[i]["baidu_duration_s"] for i in selected], color=color, s=28, label=labels[key], zorder=3)
    ax.axhline(THRESHOLD_S, color="#dc2626", linestyle="--", linewidth=1.2, label="900 s 阈值")
    ax.set_xlim(0, len(rows) + 1)
    ax.set_xlabel("测试点编号（road → boundary → ring）")
    ax.set_ylabel("百度步行耗时 / s")
    ax.set_title("百度路线耗时与 15 分钟阈值", loc="left", fontsize=14, pad=14)
    ax.grid(color="#e2e8f0", linewidth=0.5)
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.2), ncol=3, frameon=False, fontsize=9)
    fig.subplots_adjust(left=0.09, right=0.98, top=0.88, bottom=0.27)
    fig.savefig(output / "duration-threshold.png", dpi=160)
    plt.close(fig)

    metrics = classification_metrics(rows)
    matrix = np.array([[metrics["tp"], metrics["fn"]], [metrics["fp"], metrics["tn"]]], dtype=float)
    fig, ax = plt.subplots(figsize=(5.8, 5.1))
    image = ax.imshow(matrix, cmap="Blues", vmin=0, vmax=max(1, matrix.max()))
    ax.set_xticks([0, 1], ["百度 ≤ 900 s", "百度 > 900 s"])
    ax.set_yticks([0, 1], ["OSM ≤ 900 s", "OSM > 900 s"])
    for (i, j), value in np.ndenumerate(matrix):
        ax.text(j, i, f"{int(value)}", ha="center", va="center", fontsize=16, color="#15263d")
    ax.set_title("有效百度响应的混淆矩阵", loc="left", fontsize=14, pad=14)
    fig.colorbar(image, ax=ax, fraction=0.045, pad=0.04, label="测试点数")
    fig.tight_layout()
    fig.savefig(output / "confusion-matrix.png", dpi=160)
    plt.close(fig)


def fmt_pct(value) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def fmt_num(value, digits=1) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def make_reports(output: Path, settings: Settings, engine: OsmOfflineEngine, result, rows: list[dict], metrics: dict, started_at: str) -> None:
    overall = metrics["overall"]
    git_revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    evidence = {
        "tested_at": started_at,
        "git_revision": git_revision,
        "python": platform.python_version(),
        "origin_bd09ll": list(ORIGIN),
        "threshold_seconds": THRESHOLD_S,
        "baidu_endpoint": "/directionlite/v1/walking",
        "request_policy": {"cases": len(rows), "one_request_per_case": True, "retries": 0, "qps": float(settings.analysis_qps or 2.0)},
        "osm": {
            "data_version": settings.osm_data_version,
            "metric_crs": settings.osm_metric_crs,
            "quality": result.result.quality,
            "stop_reason": result.result.stop_reason,
            "reachable_length_m": round(float(result.diagnostics.get("reachable_length_m", 0)), 2),
            "polygon_buffer_m": settings.isochrone_buffer_m,
            "cache_sha256": file_sha(settings.osm_graph_cache_path),
            "coverage_sha256": file_sha(settings.osm_coverage_boundary_path),
        },
        "metrics": metrics,
        "cases": rows,
        "interpretation": "OSM classification is local polygon membership; Baidu classification is duration <= 900 seconds. Baidu is a comparison reference, not an independent ground truth.",
    }
    write_json(output / "evidence.json", evidence)

    response_rows_list = []
    for row in rows:
        duration_text = "—" if row.get("baidu_duration_s") is None else f"{row['baidu_duration_s']:.1f}"
        baidu_text = "内" if row["baidu_inside"] is True else "外" if row["baidu_inside"] is False else "无效"
        response_rows_list.append(
            f"<tr><td>{html.escape(row['id'])}</td><td>{html.escape(row['group'])}</td><td>{row['lng']:.6f}, {row['lat']:.6f}</td>"
            f"<td>{'内' if row['osm_inside'] else '外'}</td><td>{baidu_text}</td><td>{duration_text}</td>"
            f"<td>{html.escape(row['classification'])}</td><td>{html.escape(row.get('baidu_reason') or 'ok')}</td></tr>"
        )
    response_rows = "".join(response_rows_list)
    group_rows = "".join(
        f"<tr><td>{name}</td><td>{data['total']}</td><td>{data['valid_baidu']}</td><td>{data['tp']}</td><td>{data['tn']}</td><td>{data['fp']}</td><td>{data['fn']}</td><td>{fmt_pct(data['accuracy'])}</td><td>{fmt_pct(data['f1'])}</td></tr>"
        for name, data in metrics["groups"].items()
    )
    html_report = f"""<!doctype html>
<html lang=\"zh-CN\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>百度 API × OSM_OFFLINE 验证报告</title>
<style>body{{font-family:Segoe UI,Microsoft YaHei,sans-serif;color:#15263d;max-width:1180px;margin:32px auto;padding:0 22px;line-height:1.55}}h1{{margin-bottom:4px}}h2{{margin-top:32px;border-bottom:1px solid #dbe4ee;padding-bottom:6px}}.muted{{color:#64748b}}.grid{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px}}.metric{{border:1px solid #dbe4ee;border-radius:8px;padding:14px;background:#f8fafc}}.metric b{{display:block;font-size:24px}}img{{max-width:100%;height:auto;display:block;margin:14px 0 24px;border:1px solid #dbe4ee}}table{{border-collapse:collapse;width:100%;font-size:13px}}th,td{{padding:6px 8px;border-bottom:1px solid #e2e8f0;text-align:left}}th{{background:#f8fafc}}.scroll{{overflow:auto}}code{{background:#eef2f7;padding:1px 4px;border-radius:3px}}</style></head>
<body><h1>百度 API × OSM_OFFLINE 15 分钟验证报告</h1><p class=\"muted\">样例 A · 起点 BD09LL {ORIGIN[0]:.6f}, {ORIGIN[1]:.6f} · 生成 {len(rows)} 个点 · {html.escape(started_at)}</p>
<p>比较定义：OSM 使用离线 15 分钟展示面是否覆盖测试点；百度使用步行路线耗时是否 ≤ 900 秒。每个点只发起一次请求，未知响应不计入混淆矩阵。</p>
<div class=\"grid\"><div class=\"metric\">有效百度响应<b>{overall['valid_baidu']}/{overall['total']}</b><span class=\"muted\">成功率 {fmt_pct(1-overall['unknown_rate'] if overall['unknown_rate'] is not None else None)}</span></div><div class=\"metric\">一致率<b>{fmt_pct(overall['accuracy'])}</b><span class=\"muted\">有效响应口径</span></div><div class=\"metric\">F1<b>{fmt_pct(overall['f1'])}</b><span class=\"muted\">OSM 内部判定为正类</span></div></div>
<h2>空间分布</h2><img src=\"validation-points.png\" alt=\"OSM 面、可达路网和百度分类点的空间分布\"><h2>耗时与阈值</h2><img src=\"duration-threshold.png\" alt=\"每个测试点的百度步行耗时与 900 秒阈值\"><h2>混淆矩阵</h2><img src=\"confusion-matrix.png\" alt=\"OSM 与百度分类的混淆矩阵\">
<h2>总体指标</h2><table><tr><th>指标</th><th>数值</th></tr><tr><td>TP / TN / FP / FN</td><td>{overall['tp']} / {overall['tn']} / {overall['fp']} / {overall['fn']}</td></tr><tr><td>准确率</td><td>{fmt_pct(overall['accuracy'])}</td></tr><tr><td>精确率</td><td>{fmt_pct(overall['precision'])}</td></tr><tr><td>召回率</td><td>{fmt_pct(overall['recall'])}</td></tr><tr><td>特异度</td><td>{fmt_pct(overall['specificity'])}</td></tr><tr><td>平衡准确率</td><td>{fmt_pct(overall['balanced_accuracy'])}</td></tr><tr><td>不一致点</td><td>{overall['mismatches']}（{fmt_pct(overall['mismatch_rate'])}）</td></tr><tr><td>不一致点距 900 秒的中位绝对差</td><td>{fmt_num(overall['mismatch_threshold_margin_s_median'],1)} s</td></tr></table>
<h2>分层结果</h2><div class=\"scroll\"><table><tr><th>分组</th><th>总数</th><th>有效</th><th>TP</th><th>TN</th><th>FP</th><th>FN</th><th>准确率</th><th>F1</th></tr>{group_rows}</table></div>
<h2>百度响应</h2><p>原因计数：<code>{html.escape(json.dumps(metrics['reason_counts'], ensure_ascii=False))}</code>；HTTP 状态：<code>{html.escape(json.dumps(metrics['http_status_counts'], ensure_ascii=False))}</code>；百度状态：<code>{html.escape(json.dumps(metrics['baidu_status_counts'], ensure_ascii=False))}</code>；传输错误：<code>{html.escape(json.dumps(metrics['transport_error_counts'], ensure_ascii=False))}</code>。端点校验通过 {metrics['endpoint_verified_count']}/{len(rows)}。</p>
<h2>测试点明细</h2><div class=\"scroll\"><table><tr><th>编号</th><th>分组</th><th>BD09LL</th><th>OSM</th><th>百度</th><th>耗时 / s</th><th>分类</th><th>原因</th></tr>{response_rows}</table></div>
<p class=\"muted\">OSM 数据版本 {html.escape(settings.osm_data_version)} · CRS {html.escape(settings.osm_metric_crs)} · 百度接口 <code>/directionlite/v1/walking</code> · Git {html.escape(git_revision[:12])} · 原始安全证据见 <a href=\"evidence.json\">evidence.json</a>。</p></body></html>"""
    (output / "report.html").write_text(html_report, encoding="utf-8")

    md = [
        "# 百度 API × OSM_OFFLINE 15 分钟验证报告",
        "",
        f"- 起点：BD09LL `{ORIGIN[0]:.6f}, {ORIGIN[1]:.6f}`（上海样例 A）",
        f"- 测试点：`{len(rows)}` 个（road / boundary / ring 各 `{len(rows)//3}`、`{len(rows)//3}`、`{len(rows)-2*(len(rows)//3)}`）",
        f"- 阈值：`{THRESHOLD_S:.0f} s`；每点一次请求、无重试；百度 QPS：`{float(settings.analysis_qps or 2.0):g}`",
        f"- 生成时间：`{started_at}`；Git：`{git_revision}`",
        "",
        "## 结果",
        "",
        f"有效百度响应 **{overall['valid_baidu']}/{overall['total']}**，未知率 **{fmt_pct(overall['unknown_rate'])}**。在有效响应上，OSM 与百度分类一致率 **{fmt_pct(overall['accuracy'])}**，F1 **{fmt_pct(overall['f1'])}**。",
        "",
        "| TP | TN | FP | FN | 精确率 | 召回率 | 特异度 | 平衡准确率 |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|",
        f"| {overall['tp']} | {overall['tn']} | {overall['fp']} | {overall['fn']} | {fmt_pct(overall['precision'])} | {fmt_pct(overall['recall'])} | {fmt_pct(overall['specificity'])} | {fmt_pct(overall['balanced_accuracy'])} |",
        "",
        "比较定义：OSM 为离线多边形覆盖判定，百度为路线耗时 ≤ 900 秒。百度 API 作为外部比较参考，不能单独证明任何一方是真实步行时间的绝对真值。",
        "",
        "## 可视化",
        "",
        "![空间分布](validation-points.png)",
        "",
        "![耗时与阈值](duration-threshold.png)",
        "",
        "![混淆矩阵](confusion-matrix.png)",
        "",
        "## 分层结果",
        "",
        "| 分组 | 总数 | 有效 | TP | TN | FP | FN | 准确率 | F1 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, data in metrics["groups"].items():
        md.append(f"| {name} | {data['total']} | {data['valid_baidu']} | {data['tp']} | {data['tn']} | {data['fp']} | {data['fn']} | {fmt_pct(data['accuracy'])} | {fmt_pct(data['f1'])} |")
    md.extend(
        [
            "",
            "## 响应摘要",
            "",
            f"- 原因计数：`{json.dumps(metrics['reason_counts'], ensure_ascii=False)}`",
            f"- HTTP 状态：`{json.dumps(metrics['http_status_counts'], ensure_ascii=False)}`",
            f"- 百度状态：`{json.dumps(metrics['baidu_status_counts'], ensure_ascii=False)}`",
            f"- 传输错误：`{json.dumps(metrics['transport_error_counts'], ensure_ascii=False)}`",
            f"- 端点校验通过：`{metrics['endpoint_verified_count']}/{len(rows)}`",
            f"- 有效百度耗时范围：`{fmt_num(metrics['duration_seconds']['min'])}`–`{fmt_num(metrics['duration_seconds']['max'])}` s，中位数 `{fmt_num(metrics['duration_seconds']['median'])}` s",
            "",
            "完整逐点记录（已去除 API 密钥和原始响应）见 [evidence.json](evidence.json)，浏览器版见 [report.html](report.html)。",
        ]
    )
    (output / "BAIDU_VALIDATION_REPORT.md").write_text("\n".join(md) + "\n", encoding="utf-8")


def load_engine() -> tuple[Settings, OsmOfflineEngine]:
    settings = Settings(
        osm_data_version="geofabrik-shanghai-20260912",
        osm_graph_cache_path=ROOT / "data/osm/shanghai.osm-cache",
        osm_coverage_boundary_path=ROOT / "data/osm/shanghai.poly",
    )
    engine = OsmOfflineEngine.load(settings)
    if engine.store is None or engine.coverage is None:
        raise RuntimeError(engine.unavailable_reason or engine.coverage_reason or "osm_cache_unavailable")
    return settings, engine


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=int, default=DEFAULT_CASES, help="number of validation points (50-80)")
    parser.add_argument("--output", type=Path, default=BACKEND / "docs/reviews/2026-09-14/baidu-validation-60")
    args = parser.parse_args()
    if not 50 <= args.cases <= 80:
        parser.error("--cases must be between 50 and 80")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now(timezone.utc).isoformat()

    print("Loading the validated local OSM cache...", flush=True)
    settings, engine = load_engine()
    request = IsochroneRequest(ORIGIN, "bd09ll", config_version="osm-offline-v1")
    result = engine.compute(request)
    if result.result.quality == "insufficient" or result.result.local_geometry is None:
        raise RuntimeError(f"osm_compute_failed:{result.result.stop_reason}")
    rows = generate_cases(engine, result, ORIGIN, args.cases)
    print(f"Generated {len(rows)} deterministic cases; calling Baidu at QPS {float(settings.analysis_qps or 2.0):g}...", flush=True)
    rows = asyncio.run(query_baidu(settings, rows, ORIGIN))
    metrics = all_metrics(rows)
    make_figures(output, engine, result, rows)
    make_reports(output, settings, engine, result, rows, metrics, started_at)
    print(json.dumps({"output": str(output), "overall": metrics["overall"], "reasons": metrics["reason_counts"]}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
