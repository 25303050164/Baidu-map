"""Reproducible offline case evaluation + figures; run from backend.

Requires matplotlib for report generation only. Never downloads map tiles.
Usage: python scripts/test_osm_visual_report.py --output docs/reviews/2026-09-14/osm-offline
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import socket
import subprocess
import sys
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import networkx as nx
import numpy as np
from shapely.geometry import LineString, Point, box, mapping, shape
from shapely import union_all
from life_circle.models import IsochroneRequest
from app.algorithms.osm_offline.engine import OsmOfflineEngine
from app.algorithms.osm_offline.graph_store import GraphStore, PEDESTRIAN_ATTRS
from app.algorithms.osm_offline.edge_intervals import reachable_intervals
from app.algorithms.osm_offline.snap import nearest_edge_source
from app.algorithms.osm_offline.routing import cutoff_dijkstra
from app.config import Settings
from app.geo.coordinates import wgs84_to_bd09

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent


def file_sha(path):
    with open(path, "rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def graph_fingerprint(graph):
    digest = hashlib.sha256()
    for node, attrs in graph.nodes(data=True):
        digest.update(repr((node, sorted(attrs.items()))).encode())
    for u, v, k, attrs in graph.edges(keys=True, data=True):
        digest.update(repr((u, v, k, sorted((key, value) for key, value in attrs.items() if key != "geometry"))).encode())
        digest.update(attrs["geometry"].wkb)
    return digest.hexdigest()


@contextmanager
def deny_network(counter):
    def forbidden(*args, **kwargs):
        counter["attempts"] += 1
        raise AssertionError("Network access forbidden during offline evaluation")
    with patch.object(socket.socket, "connect", forbidden), patch.object(socket.socket, "connect_ex", forbidden), \
         patch.object(socket, "create_connection", forbidden), patch.object(socket, "getaddrinfo", forbidden):
        yield


def parts(geometry, kind):
    if geometry is None or geometry.is_empty:
        return
    if geometry.geom_type == kind:
        yield geometry
    elif hasattr(geometry, "geoms"):
        for child in geometry.geoms:
            yield from parts(child, kind)


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")


def graph_edge(g, u, v, coords, reverse=False):
    line = LineString(coords)
    g.add_node(u, x=coords[0][0], y=coords[0][1])
    g.add_node(v, x=coords[-1][0], y=coords[-1][1])
    data = dict.fromkeys(PEDESTRIAN_ATTRS)
    data.update(geometry=line, length=line.length, travel_time_s=line.length, osmid=1)
    g.add_edge(u, v, **data)
    if reverse:
        g.add_edge(v, u, **{**data, "geometry": LineString(coords[::-1])})


def synthetic_cases():
    results = []
    g = nx.MultiDiGraph(crs="EPSG:32651")
    graph_edge(g, "u", "v", [(0, 0), (200, 0)], True)
    n = reachable_intervals(g, {"u": 850, "v": 860}, 900).geometry
    assert n.length == 90 and not n.covers(Point(100, 0))
    results.append(("两端可达，中间不可达", g.edges["u", "v", 0]["geometry"], n,
                    "u 侧 50 m + v 侧 40 m；中间 110 m 不可达", None))
    g = nx.MultiDiGraph(crs="EPSG:32651")
    graph_edge(g, "a", "b", [(0, 0), (400, 0)])
    store = GraphStore(g, speed=1, crs="EPSG:32651")
    s = nearest_edge_source(store, (200, 0), speed=1, max_distance=100, threshold=50)
    n = reachable_intervals(g, cutoff_dijkstra(g, s.seeds, s.budget_s), s.budget_s, s.source_intervals).geometry
    assert n.bounds == (200, 0, 250, 0)
    results.append(("单向道路中点出发", g.edges["a", "b", 0]["geometry"], n,
                    "A → B；测试预算 50 s、速度 1 m/s；仅 [200,250] m 可达", (200, 0)))
    g = nx.MultiDiGraph(crs="EPSG:32651")
    graph_edge(g, "a", "b", [(0, 0), (0, 50), (50, 50)])
    n = reachable_intervals(g, {"a": 850}, 900).geometry
    assert n.equals(LineString([(0, 0), (0, 50)]))
    results.append(("弯曲道路按实际弧长截取", g.edges["a", "b", 0]["geometry"], n,
                    "到 A 已用 850 s；剩余 50 s 沿竖直路段传播", (0, 0)))
    g = nx.MultiDiGraph(crs="EPSG:32651")
    graph_edge(g, "a", "b", [(0, 0), (2000, 0)])
    s = nearest_edge_source(GraphStore(g, speed=1, crs="EPSG:32651"), (500, 130), speed=1, max_distance=200)
    n = reachable_intervals(g, {}, s.budget_s, s.source_intervals).geometry
    assert s.time_s == 130 and n.length == 770
    results.append(("离路吸附消耗时间预算", g.edges["a", "b", 0]["geometry"], n,
                    "速度 1 m/s；吸附 130 m / 130 s，路网剩余 770 s", (500, 130)))
    return results


def setup_plotting():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager
    font = Path("C:/Windows/Fonts/msyh.ttc")
    if font.exists():
        font_manager.fontManager.addfont(str(font))
        plt.rcParams["font.family"] = font_manager.FontProperties(fname=str(font)).get_name()
    plt.rcParams.update({"axes.unicode_minus": False, "font.size": 10, "axes.spines.top": False,
                         "axes.spines.right": False, "figure.facecolor": "#ffffff", "axes.facecolor": "#f8fafc",
                         "text.color": "#15263d", "axes.labelcolor": "#475569", "axes.edgecolor": "#cbd5e1",
                         "xtick.color": "#64748b", "ytick.color": "#64748b", "savefig.facecolor": "#ffffff"})
    return plt


def draw_lines(ax, geometry, origin, **style):
    from matplotlib.collections import LineCollection
    values = [np.asarray(line.coords)[:, :2]-origin for line in parts(geometry, "LineString")]
    if values:
        ax.add_collection(LineCollection(values, **style))


def draw_polygons(ax, geometry, origin):
    from matplotlib.path import Path as PlotPath
    from matplotlib.patches import PathPatch
    from shapely.geometry.polygon import orient
    for poly in parts(geometry, "Polygon"):
        poly = orient(poly, sign=1)
        coords, codes = [], []
        for ring in [poly.exterior, *poly.interiors]:
            values = np.asarray(ring.coords)[:, :2]-origin
            coords.extend(values)
            codes.extend([PlotPath.MOVETO]+[PlotPath.LINETO]*(len(values)-2)+[PlotPath.CLOSEPOLY])
        ax.add_patch(PathPatch(PlotPath(coords, codes), facecolor="#bfdbfe", edgecolor="#60a5fa", linewidth=.4, alpha=.8))


def draw_case(ax, case, engine, label=True):
    result = case["computed"]
    origin = np.array(engine.store.projection.origin(case["origin"]))
    bounds = result.result.local_geometry.bounds
    radius = max(max(abs(bounds[0]-origin[0]), abs(bounds[2]-origin[0])),
                 max(abs(bounds[1]-origin[1]), abs(bounds[3]-origin[1]))) + 140
    extent = box(origin[0]-radius, origin[1]-radius, origin[0]+radius, origin[1]+radius)
    nearby = engine.store.index.query(extent, predicate="intersects")
    # Real cached roads only, clipped to the view. No online tiles or invented basemap.
    seen = set()
    for idx in nearby:
        road = engine.store.geometries[int(idx)]
        key = road.normalize().wkb
        if key in seen:
            continue
        seen.add(key)
        draw_lines(ax, road.intersection(extent), origin, colors="#cbd5e1", linewidths=.65, zorder=1)
    draw_polygons(ax, result.result.local_geometry, origin)
    draw_lines(ax, result.reachable_network, origin, colors="#167d69", linewidths=1.05, zorder=3)
    boundary = engine.coverage.boundary.intersection(extent)
    draw_lines(ax, boundary, origin, colors="#d97706", linewidths=2.3, linestyles="--", zorder=4)
    p = np.array(result.snap_point.coords[0])-origin
    ax.plot([0, p[0]], [0, p[1]], color="#dc2626", linestyle="--", linewidth=1.3, zorder=6)
    ax.scatter(*p, s=48, marker="D", facecolor="#ffffff", edgecolor="#111827", linewidth=1.3, zorder=7)
    ax.scatter(0, 0, s=130, marker="*", color="#dc2626", edgecolor="white", linewidth=.7, zorder=8)
    ax.annotate("起点", (0, 0), xytext=(9, 9), textcoords="offset points", fontsize=10, color="#b91c1c", zorder=9)
    ax.set(xlim=(-radius, radius), ylim=(-radius, radius), aspect="equal", xlabel="相对起点向东 / m", ylabel="相对起点向北 / m")
    ax.grid(color="#e2e8f0", linewidth=.5, zorder=0)
    d = result.diagnostics
    ax.set_title(f"{case['name']} · {result.result.quality}\n可达路网 {d['reachable_length_m']/1000:.2f} km · 吸附 {d['snap_distance_m']:.1f} m", loc="left", fontsize=12, pad=14)
    ax.text(.97, .96, "N ↑", transform=ax.transAxes, ha="right", va="top", fontsize=11)
    return extent


def figures(cases, engine, output):
    plt = setup_plotting()
    from matplotlib.lines import Line2D
    from matplotlib.patches import Patch
    legend = [Line2D([0],[0],color="#cbd5e1",lw=2,label="已知步行道路"),
              Line2D([0],[0],color="#167d69",lw=2,label="可达路网"),
              Patch(facecolor="#bfdbfe", label="25 m 展示缓冲面"),
              Line2D([0],[0],marker="*",color="#dc2626",lw=0,label="实际起点"),
              Line2D([0],[0],marker="D",color="#111827",markerfacecolor="white",lw=0,label="道路吸附点"),
              Line2D([0],[0],color="#d97706",ls="--",lw=2,label="数据覆盖边界")]
    for case in cases:
        fig, ax = plt.subplots(figsize=(9, 8.8))
        draw_case(ax, case, engine)
        fig.legend(handles=legend, loc="lower center", ncol=3, frameon=False, fontsize=10, bbox_to_anchor=(.5,.025))
        fig.text(.5,.012,"本地 OSM 路网 · EPSG:32651 · Map data © OpenStreetMap contributors / ODbL",ha="center",fontsize=8,color="#64748b")
        fig.subplots_adjust(left=.1,right=.97,top=.88,bottom=.17)
        fig.savefig(output/f"{case['id']}.png", dpi=150)
        plt.close(fig)
    fig, axes = plt.subplots(2,2,figsize=(14,13))
    for ax, case in zip(axes.flat, cases):
        draw_case(ax, case, engine)
    fig.suptitle("OSM_OFFLINE · 上海真实路网样例",fontsize=19,y=.98)
    fig.legend(handles=legend,loc="lower center",ncol=3,frameon=False,bbox_to_anchor=(.5,.025))
    fig.text(.5,.01,"固定阈值 900 s · 步速 1.3 m/s · buffer 25 m · 结果不是真实步行精度的证明",ha="center",fontsize=10,color="#64748b")
    fig.subplots_adjust(left=.08,right=.98,bottom=.14,top=.9,hspace=.35,wspace=.2)
    fig.savefig(output/"real-samples.png",dpi=150)
    plt.close(fig)
    fig, axes = plt.subplots(2,2,figsize=(13,8))
    for ax, (name, road, reachable, subtitle, source) in zip(axes.flat, synthetic_cases()):
        draw_lines(ax, road, np.array([0,0]), colors="#cbd5e1",linewidths=9,zorder=1)
        draw_lines(ax, reachable, np.array([0,0]), colors="#167d69",linewidths=6,zorder=2)
        if source:
            ax.scatter(*source,marker="*",s=140,color="#dc2626",zorder=4)
            if source[1] == 130:
                ax.plot([source[0],source[0]],[0,130],ls="--",color="#dc2626")
        ax.autoscale()
        ax.margins(x=.12,y=.5)
        if road.bounds[2] <= 200:
            ax.set_aspect("equal",adjustable="datalim")
        ax.set_title(name+"\n"+subtitle,loc="left",fontsize=11,pad=15)
        ax.set_xlabel("道路位置 / m")
        ax.set_ylabel("偏移 / m")
        ax.grid(color="#e2e8f0",lw=.5)
    fig.suptitle("边界算法样例 · 从实际函数输出绘图",fontsize=18,y=.99)
    fig.legend(handles=legend[:2]+[legend[3]],loc="lower center",ncol=3,frameon=False,bbox_to_anchor=(.5,.01))
    fig.subplots_adjust(left=.07,right=.97,bottom=.14,top=.83,hspace=.75,wspace=.27)
    fig.savefig(output/"algorithm-cases.png",dpi=150)
    plt.close(fig)
    fig, ax = plt.subplots(figsize=(10,5))
    components = [("snap_ms","吸附","#94a3b8"),("routing_ms","路由","#167d69"),
                  ("edge_interval_ms","区间","#2b6cb0"),("polygon_ms","成面","#60a5fa")]
    bottoms = np.zeros(len(cases))
    means = np.array([np.mean([d["total_ms"] for d in c["runs"]]) for c in cases])
    for key,label,color in components:
        values = np.array([np.mean([d[key] for d in c["runs"]]) for c in cases])
        ax.barh(np.arange(len(cases)),values,left=bottoms,color=color,label=label,height=.52)
        bottoms += values
    ax.barh(np.arange(len(cases)),np.maximum(0,means-bottoms),left=bottoms,color="#d5deeb",height=.52,label="转换 / 覆盖检查等")
    for i,c in enumerate(cases):
        lo,hi = min(d["total_ms"] for d in c["runs"]),max(d["total_ms"] for d in c["runs"])
        ax.text(means[i]+max(means)*.025,i,f"{means[i]:.1f} ms  [{lo:.1f}–{hi:.1f}]",va="center",fontsize=9)
    ax.set_yticks(range(len(cases)),[c["name"] for c in cases]); ax.invert_yaxis()
    ax.set_xlim(0,max(means)*1.65)
    ax.set_xlabel("单次算法耗时 / ms（每点 3 次，均值与范围）")
    ax.set_title("请求性能分解 · 不含首次缓存加载",loc="left",fontsize=14,pad=18)
    ax.legend(loc="lower center",bbox_to_anchor=(.5,-.36),ncol=5,frameon=False,fontsize=9)
    fig.subplots_adjust(left=.18,right=.96,top=.85,bottom=.3)
    fig.savefig(output/"performance.png",dpi=150)
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output",type=Path,default=BACKEND/"docs/reviews/2026-09-14/osm-offline")
    args = parser.parse_args(); output = args.output.resolve(); output.mkdir(parents=True,exist_ok=True)
    cfg = Settings(_env_file=None,osm_data_version="geofabrik-shanghai-20260912",
                   osm_graph_cache_path=ROOT/"data/osm/shanghai.osm-cache",
                   osm_coverage_boundary_path=ROOT/"data/osm/shanghai.poly")
    counter = {"attempts":0}
    print("Loading and validating local Shanghai cache...",flush=True)
    started = time.perf_counter()
    with deny_network(counter):
        engine = OsmOfflineEngine.load(cfg)
    assert engine.store is not None,engine.unavailable_reason
    assert engine.coverage is not None,engine.coverage_reason
    startup_s = time.perf_counter()-started
    print(f"Cache ready in {startup_s:.2f} s",flush=True)
    initial_hash = graph_fingerprint(engine.store.graph)
    definitions = [
        {"id":"sample-a","name":"上海样例 A","origin":(121.51108,31.20415)},
        {"id":"sample-b","name":"上海样例 B","origin":wgs84_to_bd09(121.4737,31.2304)},
        {"id":"sample-c","name":"上海样例 C","origin":wgs84_to_bd09(121.52,31.30)},
    ]
    candidates = engine.store.index.query(engine.coverage.boundary.buffer(70),predicate="intersects")
    boundary_origin = None
    for idx in sorted(map(int,candidates)):
        edge = engine.store.edge_ids[idx]
        if engine.store.component_sizes[engine.store.component[edge[0]]] < 100:
            continue
        midpoint = engine.store.geometries[idx].interpolate(.5,normalized=True)
        if engine.coverage.covers(midpoint) and midpoint.distance(engine.coverage.boundary) < 70:
            boundary_origin = wgs84_to_bd09(*engine.store.projection.inverse.transform(midpoint.x,midpoint.y))
            break
    assert boundary_origin is not None,"No suitable coverage boundary sample"
    definitions.append({"id":"sample-boundary","name":"覆盖边界样例","origin":boundary_origin})
    from fastapi.testclient import TestClient
    from app.main import create_app
    # Reuse the exact validated startup graph; the application still calls its
    # normal lifespan factory. Windows' local event-loop socketpair starts first.
    with patch.object(OsmOfflineEngine,"load",return_value=engine), TestClient(create_app(cfg)) as client:
        with deny_network(counter):
            cases = []
            for case in definitions:
                request = IsochroneRequest(case["origin"],"bd09ll",config_version="osm-offline-v1")
                results = [engine.compute(request) for _ in range(3)]
                first = results[0]
                assert first.result.geometry is not None,first.result.stop_reason
                assert shape(first.result.geometry).is_valid and not first.reachable_network.is_empty
                assert all(r.result.geometry == first.result.geometry for r in results)
                assert all(r.result.quality == first.result.quality for r in results)
                response = client.post("/api/v1/analysis/osm_offline",json={"origin":{"lng":case["origin"][0],"lat":case["origin"][1]},"coordinate_system":"bd09ll","algorithm":"osm_offline"})
                assert response.status_code == 200
                assert shape(response.json()["data"]["geometry"]).equals_exact(shape(first.result.geometry),0)
                assert response.json()["status"] == "partial"
                if case["id"] == "sample-boundary":
                    assert first.result.quality == "partial" and first.diagnostics["coverage_boundary_hit"] is True
                case.update(computed=first,runs=[r.diagnostics for r in results],http_status=response.status_code,
                            business_status=response.json()["status"],deterministic=True)
                cases.append(case)
                write_json(output/f"{case['id']}-geometry.geojson",first.result.geometry)
                debug = {"crs":"EPSG:32651","origin":engine.store.projection.origin(case["origin"]),
                         "snap_point":mapping(first.snap_point),"reachable_network":mapping(first.reachable_network),
                         "polygon":mapping(first.result.local_geometry)}
                write_json(output/f"{case['id']}-metric-debug.json",debug)
                print(json.dumps({"case":case["id"],"quality":first.result.quality,"ms":[r.diagnostics["total_ms"] for r in results]},ensure_ascii=True),flush=True)
            outside = engine.compute(IsochroneRequest(wgs84_to_bd09(116.4,39.9),"bd09ll"))
            assert outside.result.quality == "insufficient" and outside.result.stop_reason == "origin_outside_coverage"
            assert outside.result.geometry is None
            invalid = client.post("/api/v1/analysis/osm_offline",json={"origin":{"lng":121.5,"lat":31.2},"coordinate_system":"bd09ll","threshold":901})
            assert invalid.status_code == 422 and invalid.json()["status"] == "failed"
    assert counter["attempts"] == 0
    after_hash = graph_fingerprint(engine.store.graph)
    assert initial_hash == after_hash
    synthetic_cases()
    summary = {
        "tested_at":datetime.now(timezone.utc).isoformat(),
        "git_revision":subprocess.check_output(["git","rev-parse","HEAD"],cwd=ROOT,text=True).strip(),
        "python":platform.python_version(),"startup_seconds":startup_s,
        "snapshot":{"data_version":cfg.osm_data_version,"pbf_sha256":file_sha(ROOT/"data/osm/shanghai-260912.osm.pbf"),
                    "cache_sha256":file_sha(cfg.osm_graph_cache_path),"coverage_sha256":file_sha(cfg.osm_coverage_boundary_path),
                    "nodes":engine.store.node_count,"edges":engine.store.edge_count},
        "network_attempts":counter["attempts"],"graph_unchanged":initial_hash==after_hash,"graph_fingerprint":initial_hash,
        "cases":[{k:v for k,v in c.items() if k!="computed"} | {"quality":c["computed"].result.quality,"warnings":c["computed"].result.warnings} for c in cases],
        "negative_cases":{"outside_coverage":{"quality":outside.result.quality,"reason":outside.result.stop_reason},"threshold_901_http":invalid.status_code},
        "synthetic_visual_assertions":4,
        "scope":"Cached real OSM graph evaluation; no PBF rebuild or independent walking-time accuracy validation in this run.",
    }
    write_json(output/"evidence.json",summary)
    figures(cases,engine,output)
    print("Figures and evidence saved: "+str(output),flush=True)


if __name__ == "__main__":
    main()
