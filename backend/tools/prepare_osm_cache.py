"""Prepare the JSON cache consumed by ``app.osm`` from a local OSM PBF.

This is an offline preparation tool.  It deliberately does not download data
or run during an API request.  Keep its optional dependencies in a separate
environment when the backend runtime should remain small.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path


EARTH_RADIUS_M = 6_378_137.0
PI = math.pi
X_PI = PI * 3000.0 / 180.0
DEFAULT_SPEED_MPS = 1.3
DEFAULT_SNAP_DISTANCE_M = 250.0
DEFAULT_LIMITATIONS = [
    "OSM 数据不是道路真值",
    "固定步行速度为 1.3 m/s",
    "门禁、临时通行、部分步行限制未建模",
    "buffer 仅用于面状展示",
]
BACKEND_DIR = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = BACKEND_DIR / "data" / "osm-cache"


def _number(value, name):
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be numeric") from None
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def _distance(a, b):
    latitude_scale = 111_320.0
    longitude_scale = latitude_scale * math.cos(math.radians((a[1] + b[1]) / 2))
    return math.hypot((a[0] - b[0]) * longitude_scale, (a[1] - b[1]) * latitude_scale)


def _out_of_china(lng, lat):
    return not (72.004 <= lng <= 137.8347 and 0.8293 <= lat <= 55.8271)


def _transform_lat(x, y):
    value = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    value += (20.0 * math.sin(6.0 * x * PI) + 20.0 * math.sin(2.0 * x * PI)) * 2.0 / 3.0
    value += (20.0 * math.sin(y * PI) + 40.0 * math.sin(y / 3.0 * PI)) * 2.0 / 3.0
    value += (160.0 * math.sin(y / 12.0 * PI) + 320.0 * math.sin(y * PI / 30.0)) * 2.0 / 3.0
    return value


def _transform_lng(x, y):
    value = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    value += (20.0 * math.sin(6.0 * x * PI) + 20.0 * math.sin(2.0 * x * PI)) * 2.0 / 3.0
    value += (20.0 * math.sin(x * PI) + 40.0 * math.sin(x / 3.0 * PI)) * 2.0 / 3.0
    value += (150.0 * math.sin(x / 12.0 * PI) + 300.0 * math.sin(x / 30.0 * PI)) * 2.0 / 3.0
    return value


def wgs84_to_bd09(lng, lat):
    """Convert OSM WGS84 coordinates to the BD09LL wire coordinates."""
    lng, lat = _number(lng, "longitude"), _number(lat, "latitude")
    if _out_of_china(lng, lat):
        return round(lng, 6), round(lat, 6)
    dlat = _transform_lat(lng - 105.0, lat - 35.0)
    dlng = _transform_lng(lng - 105.0, lat - 35.0)
    rad_lat = lat / 180.0 * PI
    magic = 1 - 0.00669342162296594323 * math.sin(rad_lat) ** 2
    sqrt_magic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((EARTH_RADIUS_M * (1 - 0.00669342162296594323)) / (magic * sqrt_magic) * PI)
    dlng = (dlng * 180.0) / (EARTH_RADIUS_M / sqrt_magic * math.cos(rad_lat) * PI)
    gcj_lng, gcj_lat = lng + dlng, lat + dlat
    z = math.sqrt(gcj_lng * gcj_lng + gcj_lat * gcj_lat) + 0.00002 * math.sin(gcj_lat * X_PI)
    theta = math.atan2(gcj_lat, gcj_lng) + 0.000003 * math.cos(gcj_lng * X_PI)
    return round(z * math.cos(theta) + 0.0065, 6), round(z * math.sin(theta) + 0.006, 6)


def _convert_point(lng, lat, input_crs):
    return wgs84_to_bd09(lng, lat) if input_crs == "wgs84" else (round(float(lng), 6), round(float(lat), 6))


def _coordinates(frame):
    columns = set(frame.columns)
    if {"lon", "lat"}.issubset(columns):
        return list(frame["lon"]), list(frame["lat"])
    if {"lng", "lat"}.issubset(columns):
        return list(frame["lng"]), list(frame["lat"])
    if "geometry" in columns:
        return [geometry.x for geometry in frame["geometry"]], [geometry.y for geometry in frame["geometry"]]
    raise ValueError("OSM nodes do not contain lon/lat or geometry columns")


def _coverage(points, bbox, input_crs):
    if bbox is None:
        return [
            round(min(point[0] for point in points), 6),
            round(min(point[1] for point in points), 6),
            round(max(point[0] for point in points), 6),
            round(max(point[1] for point in points), 6),
        ]
    west, south, east, north = bbox
    corners = [_convert_point(west, south, input_crs), _convert_point(west, north, input_crs),
               _convert_point(east, south, input_crs), _convert_point(east, north, input_crs)]
    return [
        round(min(point[0] for point in corners), 6),
        round(min(point[1] for point in corners), 6),
        round(max(point[0] for point in corners), 6),
        round(max(point[1] for point in corners), 6),
    ]


def _load_network(pbf, bbox):
    try:
        from pyrosm import OSM
    except ImportError:
        raise RuntimeError(
            "pyrosm is not installed; install backend/requirements-osm-prep.txt in the OSM preparation environment"
        ) from None
    reader = OSM(str(pbf), bounding_box=list(bbox) if bbox is not None else None)
    nodes, edges = reader.get_network(network_type="walking", nodes=True)
    if len(nodes) == 0 or len(edges) == 0:
        raise RuntimeError("the PBF contains no walkable nodes or edges in the requested coverage")
    return nodes, edges


def prepare(args):
    pbf = args.pbf.expanduser().resolve()
    output = args.output.expanduser().resolve()
    if not pbf.is_file():
        raise FileNotFoundError(f"PBF not found: {pbf}")
    if not args.overwrite and any((output / name).exists() for name in ("metadata.json", "graph.json")):
        raise FileExistsError(f"cache already exists; pass --overwrite: {output}")
    output.mkdir(parents=True, exist_ok=True)

    bbox = tuple(args.coverage_bbox) if args.coverage_bbox else None
    nodes, edges = _load_network(pbf, bbox)
    if "id" not in nodes.columns:
        raise RuntimeError("OSM nodes must contain an id column")
    node_ids = list(nodes["id"])
    lngs, lats = _coordinates(nodes)
    points = {}
    for node_id, lng, lat in zip(node_ids, lngs, lats):
        point = _convert_point(lng, lat, args.input_crs)
        if not (-180 <= point[0] <= 180 and -85 < point[1] < 85):
            continue
        points[str(node_id)] = point
    if not points:
        raise RuntimeError("no valid geographic nodes were found")

    if not {"u", "v"}.issubset(set(edges.columns)):
        raise RuntimeError("OSM edges must contain u and v node ids")
    edge_lengths = list(edges["length"]) if "length" in edges.columns else [None] * len(edges)
    prepared_edges = []
    for source, target, length in zip(edges["u"], edges["v"], edge_lengths):
        source, target = str(source), str(target)
        if source not in points or target not in points:
            continue
        try:
            distance = _number(length, "edge length") if length is not None else _distance(points[source], points[target])
        except ValueError:
            distance = _distance(points[source], points[target])
        if distance < 0:
            continue
        prepared_edges.append({
            "from": source,
            "to": target,
            "lengthM": round(distance, 3),
            "durationS": round(distance / args.walk_speed_mps, 3),
            "bidirectional": True,
        })
    if not prepared_edges:
        raise RuntimeError("no usable walkable edges were found")

    coverage = _coverage(list(points.values()), bbox, args.input_crs)
    prepared_at = args.prepared_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    graph = {
        "nodes": [{"id": node_id, "lng": point[0], "lat": point[1]} for node_id, point in points.items()],
        "edges": prepared_edges,
    }
    metadata = {
        "coordinateSystem": "bd09ll",
        "coverageCity": args.city,
        "coverage": coverage,
        "dataVersion": args.data_version,
        "dataDate": args.data_date,
        "downloadedAt": args.downloaded_at,
        "preparedAt": prepared_at,
        "sourceFormat": "osm.pbf",
        "sourceCrs": args.input_crs,
        "walkSpeedMps": args.walk_speed_mps,
        "maxSnapDistanceM": args.max_snap_distance_m,
        "attribution": "© OpenStreetMap contributors",
        "license": "ODbL",
        "limitations": list(DEFAULT_LIMITATIONS),
    }
    _write_json(output / "metadata.json", metadata)
    _write_json(output / "graph.json", graph)
    return len(points), len(prepared_edges), coverage


def _write_json(path, value):
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def parser():
    result = argparse.ArgumentParser(description="Prepare a local OSM walking graph for the Life Circle backend.")
    result.add_argument("--pbf", type=Path, required=True, help="local .osm.pbf file; no network download is performed")
    result.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    result.add_argument("--city", required=True, help="coverage city shown by the capabilities API")
    result.add_argument("--data-version", required=True, help="immutable snapshot identifier")
    result.add_argument("--data-date", required=True, help="OSM snapshot date, e.g. 2026-09-13")
    result.add_argument("--downloaded-at", help="download/archive timestamp; leave unset if unknown")
    result.add_argument("--prepared-at", help="cache preparation timestamp; defaults to current UTC time")
    result.add_argument("--coverage-bbox", nargs=4, type=float, metavar=("WEST", "SOUTH", "EAST", "NORTH"))
    result.add_argument("--input-crs", choices=("wgs84", "bd09ll"), default="wgs84")
    result.add_argument("--walk-speed-mps", type=float, default=DEFAULT_SPEED_MPS)
    result.add_argument("--max-snap-distance-m", type=float, default=DEFAULT_SNAP_DISTANCE_M)
    result.add_argument("--overwrite", action="store_true", help="replace existing metadata.json and graph.json")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    if args.walk_speed_mps <= 0 or not math.isfinite(args.walk_speed_mps):
        parser().error("--walk-speed-mps must be a finite positive number")
    if args.max_snap_distance_m <= 0 or not math.isfinite(args.max_snap_distance_m):
        parser().error("--max-snap-distance-m must be a finite positive number")
    if args.coverage_bbox and not (args.coverage_bbox[0] < args.coverage_bbox[2] and args.coverage_bbox[1] < args.coverage_bbox[3]):
        parser().error("--coverage-bbox must be west,south,east,north")
    try:
        nodes, edges, coverage = prepare(args)
    except (FileNotFoundError, FileExistsError, RuntimeError, ValueError) as error:
        print(f"OSM cache preparation failed: {error}", file=sys.stderr)
        return 2
    print(f"OSM cache ready: {args.output}")
    print(f"nodes={nodes} edges={edges} coverage={coverage}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
