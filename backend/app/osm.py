"""Small, network-free adapter for prepared OSM walking caches.

The application deliberately does not parse PBF files at request time.  A
prepared cache is a directory (or JSON file) containing ``metadata.json`` and
either a graph, line features, or pre-sampled route observations.  The loader
only exposes safe metadata to the API; filesystem paths and parser errors stay
inside this module.
"""
from __future__ import annotations

import asyncio
import heapq
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from life_circle.coordinates import normalize
from life_circle.engine import compute_isochrone
from life_circle.models import CancelToken, IsochroneRequest, RouteObservation


DEFAULT_LIMITATIONS = [
    "OSM 数据不是道路真值",
    "固定步行速度为 1.3 m/s",
    "门禁、临时通行、部分步行限制未建模",
    "buffer 仅用于面状展示",
]


def _first(value: dict[str, Any], *keys: str, default=None):
    for key in keys:
        if key in value and value[key] is not None:
            return value[key]
    return default


def _point(value: Any) -> tuple[float, float] | None:
    if isinstance(value, dict):
        lng = _first(value, "lng", "lon", "longitude", "x")
        lat = _first(value, "lat", "latitude", "y")
        value = [lng, lat] if lng is not None and lat is not None else _first(value, "coordinates", "point", "location")
        if isinstance(value, dict):
            lng = _first(value, "lng", "lon", "longitude", "x")
            lat = _first(value, "lat", "latitude", "y")
            value = [lng, lat]
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    if type(value[0]) not in (int, float) or type(value[1]) not in (int, float):
        return None
    try:
        return normalize((value[0], value[1]))
    except (TypeError, ValueError, OverflowError):
        return None


def _distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    latitude_scale = 111_320.0
    longitude_scale = latitude_scale * math.cos(math.radians((a[1] + b[1]) / 2))
    return math.hypot((a[0] - b[0]) * longitude_scale, (a[1] - b[1]) * latitude_scale)


def _bbox(value: Any) -> tuple[float, float, float, float] | None:
    if isinstance(value, dict):
        value = _first(value, "bbox", "bounds", "extent", "boundary", "geometry")
        if isinstance(value, dict) and value.get("type") == "Feature":
            value = value.get("geometry")
        if isinstance(value, dict) and value.get("type") in {"Polygon", "MultiPolygon"}:
            value = value.get("coordinates")
    if isinstance(value, (list, tuple)) and len(value) == 4 and all(
        type(item) in (int, float) for item in value
    ):
        west, south, east, north = map(float, value)
        if west <= east and south <= north:
            return west, south, east, north
    points: list[tuple[float, float]] = []

    def collect(node):
        point = _point(node)
        if point is not None:
            points.append(point)
        elif isinstance(node, (list, tuple)):
            for child in node:
                collect(child)
        elif isinstance(node, dict):
            collect(node.get("coordinates"))

    collect(value)
    if not points:
        return None
    return (
        min(point[0] for point in points),
        min(point[1] for point in points),
        max(point[0] for point in points),
        max(point[1] for point in points),
    )


def _geometry_from_bbox(bounds: tuple[float, float, float, float] | None) -> dict | None:
    if bounds is None:
        return None
    west, south, east, north = bounds
    return {
        "type": "Polygon",
        "coordinates": [[[west, south], [east, south], [east, north], [west, north], [west, south]]],
        "coordinateSystem": "bd09ll",
    }


def _ring_contains(point: tuple[float, float], ring: list) -> bool:
    inside = False
    x, y = point
    for index, current in enumerate(ring):
        previous = ring[index - 1]
        if not isinstance(current, (list, tuple)) or not isinstance(previous, (list, tuple)) or len(current) < 2 or len(previous) < 2:
            continue
        x1, y1, x2, y2 = current[0], current[1], previous[0], previous[1]
        if type(x1) not in (int, float) or type(y1) not in (int, float) or type(x2) not in (int, float) or type(y2) not in (int, float):
            continue
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def _boundary_contains(point: tuple[float, float], boundary: dict | None) -> bool:
    if not isinstance(boundary, dict) or boundary.get("type") not in {"Polygon", "MultiPolygon"}:
        return True
    coordinates = boundary.get("coordinates")
    polygons = [coordinates] if boundary.get("type") == "Polygon" else coordinates
    if not isinstance(polygons, list):
        return False
    for polygon in polygons:
        if isinstance(polygon, list) and polygon and _ring_contains(point, polygon[0]):
            if not any(_ring_contains(point, hole) for hole in polygon[1:] if isinstance(hole, list)):
                return True
    return False


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


@dataclass(frozen=True)
class CacheInfo:
    availability: str
    reason: str | None
    coverage_city: str | None
    data_version: str | None
    data_date: str | None
    downloaded_at: str | None
    prepared_at: str | None
    bounds: tuple[float, float, float, float] | None
    coverage_boundary: dict | None
    attribution: str
    license: str
    limitations: list[str]


class OsmCache:
    """Validated in-memory view of a prepared cache."""

    def __init__(self, info: CacheInfo, nodes=None, edges=None, observations=None):
        self.info = info
        self.nodes: dict[str, tuple[float, float]] = nodes or {}
        self.edges: dict[str, list[tuple[str, float, float]]] = edges or {}
        self.observations: dict[tuple[tuple[float, float], tuple[float, float]], RouteObservation] = observations or {}
        self.max_snap_distance = 250.0
        self.speed_mps = 1.3

    @property
    def available(self) -> bool:
        return self.info.availability in {"ready", "degraded"}

    @classmethod
    def load(cls, path: str | Path | None, *, expected_version: str | None = None, speed_mps: float = 1.3):
        if path is None or not str(path).strip():
            return cls(CacheInfo("unavailable", "not_configured", None, None, None, None, None, None, None,
                                 "© OpenStreetMap contributors", "ODbL", list(DEFAULT_LIMITATIONS)))
        root_path = Path(path).expanduser()
        if not root_path.exists():
            return cls(CacheInfo("unavailable", "cache_missing", None, None, None, None, None, None, None,
                                 "© OpenStreetMap contributors", "ODbL", list(DEFAULT_LIMITATIONS)))
        try:
            if root_path.is_dir():
                primary = next((root_path / name for name in ("metadata.json", "manifest.json", "cache.json", "osm-cache.json") if (root_path / name).is_file()), None)
                document = _read_json(primary) if primary else None
                metadata = (document or {}).get("metadata", document or {})
                graph = (document or {}).get("graph") or (document or {}).get("network")
                if graph is None:
                    for name in ("graph.json", "network.json", "roads.json", "observations.json"):
                        candidate = root_path / name
                        if candidate.is_file():
                            graph = _read_json(candidate)
                            break
            else:
                document = _read_json(root_path)
                metadata = (document or {}).get("metadata", document or {})
                graph = (document or {}).get("graph") or (document or {}).get("network")
            if not isinstance(metadata, dict):
                metadata = {}
            if not isinstance(graph, dict):
                graph = document if isinstance(document, dict) else {}
            if not metadata and isinstance(graph, dict) and isinstance(graph.get("metadata"), dict):
                metadata = graph["metadata"]
            if isinstance(graph, dict) and isinstance(graph.get("graph"), dict):
                graph = graph["graph"]
        except (OSError, ValueError):
            return cls(CacheInfo("unavailable", "cache_invalid", None, None, None, None, None, None, None,
                                 "© OpenStreetMap contributors", "ODbL", list(DEFAULT_LIMITATIONS)))

        crs = str(_first(metadata, "coordinateSystem", "coordinate_system", "crs", default="bd09ll")).lower()
        version = _first(metadata, "dataVersion", "data_version", "version")
        if crs not in {"bd09ll", "bd-09ll"}:
            return cls(CacheInfo("unavailable", "unsupported_crs", None, str(version) if version else None, None, None, None, None, None,
                                 "© OpenStreetMap contributors", "ODbL", list(DEFAULT_LIMITATIONS)))
        if expected_version and str(version or "") != expected_version:
            return cls(CacheInfo("unavailable", "version_mismatch", None, str(version), None, None, None, None, None,
                                 "© OpenStreetMap contributors", "ODbL", list(DEFAULT_LIMITATIONS)))

        coverage_value = _first(metadata, "coverageBoundary", "coverage_boundary", "coverage", "bbox", "bounds", "extent")
        bounds = _bbox(coverage_value)
        coverage_boundary = coverage_value if isinstance(coverage_value, dict) and coverage_value.get("type") in {"Polygon", "MultiPolygon"} else _geometry_from_bbox(bounds)
        city = _first(metadata, "coverageCity", "coverage_city", "city")
        limitations = _first(metadata, "limitations", default=list(DEFAULT_LIMITATIONS))
        if not isinstance(limitations, list) or not all(isinstance(item, str) for item in limitations):
            limitations = list(DEFAULT_LIMITATIONS)
        info = CacheInfo(
            "unavailable", None, str(city) if city is not None else None, str(version) if version is not None else None,
            _as_text(_first(metadata, "dataDate", "data_date")), _as_text(_first(metadata, "downloadedAt", "downloaded_at")),
            _as_text(_first(metadata, "preparedAt", "prepared_at")), bounds, coverage_boundary,
            str(_first(metadata, "attribution", default="© OpenStreetMap contributors")),
            str(_first(metadata, "license", default="ODbL")), limitations,
        )
        cache = cls(info)
        cache.speed_mps = speed_mps
        try:
            cache._load_graph(graph, speed_mps)
        except (TypeError, ValueError, KeyError, OverflowError):
            cache.nodes.clear()
            cache.edges.clear()
            cache.observations.clear()
        if bounds is None:
            cache.info = CacheInfo("unavailable", "coverage_missing", info.coverage_city, info.data_version, info.data_date,
                                   info.downloaded_at, info.prepared_at, None, None, info.attribution, info.license, info.limitations)
        elif not cache.nodes and not cache.observations:
            cache.info = CacheInfo("unavailable", "graph_missing", info.coverage_city, info.data_version, info.data_date,
                                   info.downloaded_at, info.prepared_at, info.bounds, info.coverage_boundary,
                                   info.attribution, info.license, info.limitations)
        else:
            # A cache with an explicit boundary and usable local data is ready.
            cache.info = CacheInfo("ready", None, info.coverage_city, info.data_version, info.data_date, info.downloaded_at,
                                   info.prepared_at, info.bounds, info.coverage_boundary, info.attribution, info.license, info.limitations)
        raw_snap = _first(metadata, "maxSnapDistanceM", "max_snap_distance_m")
        if type(raw_snap) in (int, float) and math.isfinite(raw_snap) and raw_snap > 0:
            cache.max_snap_distance = float(raw_snap)
        return cache

    def _load_graph(self, graph: dict[str, Any], speed_mps: float):
        nodes = graph.get("nodes", [])
        if isinstance(nodes, dict):
            items = [{"id": key, **value} if isinstance(value, dict) else {"id": key, "coordinates": value} for key, value in nodes.items()]
        elif isinstance(nodes, list):
            items = nodes
        else:
            items = []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            point = _point(item)
            identifier = str(_first(item, "id", "nodeId", "node_id", default=index))
            if point is not None:
                self.nodes[identifier] = point

        edges = graph.get("edges", graph.get("links", []))
        if isinstance(edges, list):
            for item in edges:
                if not isinstance(item, dict):
                    continue
                source = _first(item, "from", "source", "u")
                target = _first(item, "to", "target", "v")
                if source is None or target is None:
                    continue
                source, target = str(source), str(target)
                if source not in self.nodes or target not in self.nodes:
                    continue
                length = _first(item, "lengthM", "length_m", "distanceM", "distance_m", "distance")
                if type(length) not in (int, float) or not math.isfinite(length) or length < 0:
                    length = _distance(self.nodes[source], self.nodes[target])
                duration = _first(item, "durationS", "duration_s", "duration", "timeS", "time_s")
                if type(duration) not in (int, float) or not math.isfinite(duration) or duration < 0:
                    duration = float(length) / speed_mps
                self.edges.setdefault(source, []).append((target, float(duration), float(length)))
                if _first(item, "bidirectional", "twoWay", "two_way", default=True) is not False:
                    self.edges.setdefault(target, []).append((source, float(duration), float(length)))

        # Accept a GeoJSON FeatureCollection exported by a preparation script.
        features = graph.get("features")
        if isinstance(features, list):
            for feature in features:
                geometry = feature.get("geometry") if isinstance(feature, dict) else None
                coordinates = geometry.get("coordinates") if isinstance(geometry, dict) else None
                lines = coordinates if isinstance(coordinates, list) else []
                if geometry and geometry.get("type") == "LineString":
                    lines = [lines]
                for line in lines:
                    if not isinstance(line, list) or len(line) < 2:
                        continue
                    previous = None
                    for point in line:
                        current = _point(point)
                        if current is None:
                            continue
                        identifier = f"geo:{current[0]}:{current[1]}"
                        self.nodes[identifier] = current
                        if previous is not None:
                            distance = _distance(self.nodes[previous], current)
                            duration = distance / speed_mps
                            self.edges.setdefault(previous, []).append((identifier, duration, distance))
                            self.edges.setdefault(identifier, []).append((previous, duration, distance))
                        previous = identifier

        observations = graph.get("observations", graph.get("routes", []))
        if isinstance(observations, dict):
            observations = [observations]
        if isinstance(observations, list):
            for item in observations:
                if not isinstance(item, dict):
                    continue
                origin = _point(_first(item, "origin", "from"))
                destination = _point(_first(item, "destination", "to"))
                duration = _first(item, "durationS", "duration_s", "duration", "timeS", "time_s")
                if origin is None or destination is None or type(duration) not in (int, float) or not math.isfinite(duration) or duration < 0:
                    continue
                distance = _first(item, "distanceM", "distance_m", "distance")
                distance = float(distance) if type(distance) in (int, float) and math.isfinite(distance) and distance >= 0 else None
                path = [_point(point) for point in item.get("path", [])] if isinstance(item.get("path"), list) else []
                path = [point for point in path if point is not None]
                observation = RouteObservation(destination, float(duration), endpoint_verified=True, distance_m=distance, route_path=path)
                self.observations[(origin, destination)] = observation
                if item.get("bidirectional", False):
                    self.observations[(destination, origin)] = RouteObservation(origin, float(duration), endpoint_verified=True, distance_m=distance, route_path=list(reversed(path)))

    def contains(self, point: tuple[float, float]) -> bool:
        bounds = self.info.bounds
        return bool(bounds and bounds[0] <= point[0] <= bounds[2] and bounds[1] <= point[1] <= bounds[3]
                    and _boundary_contains(point, self.info.coverage_boundary))

    def near_boundary(self, point: tuple[float, float], margin_m: float = 3200) -> bool:
        bounds = self.info.bounds
        if not bounds or not self.contains(point):
            return False
        latitude_scale = 111_320.0
        longitude_scale = latitude_scale * math.cos(math.radians(point[1]))
        distances = ((point[0] - bounds[0]) * longitude_scale, (bounds[2] - point[0]) * longitude_scale,
                     (point[1] - bounds[1]) * latitude_scale, (bounds[3] - point[1]) * latitude_scale)
        return min(distances) <= margin_m

    def route(self, origin: tuple[float, float], destination: tuple[float, float]) -> RouteObservation:
        origin, destination = normalize(origin), normalize(destination)
        exact = self.observations.get((origin, destination))
        if exact is not None:
            return exact
        if not self.contains(origin) or not self.contains(destination):
            return RouteObservation(destination, reason="coverage_outside", endpoint_verified=False)
        if not self.nodes:
            return RouteObservation(destination, reason="no_result", endpoint_verified=False)
        start, start_distance = self._nearest(origin)
        end, end_distance = self._nearest(destination)
        if start is None or end is None or start_distance > self.max_snap_distance or end_distance > self.max_snap_distance:
            return RouteObservation(destination, reason="endpoint_unmatched", endpoint_verified=False)
        if start == end:
            return RouteObservation(destination, 0.0, endpoint_verified=True, distance_m=start_distance + end_distance, route_path=[destination])
        queue = [(0.0, 0.0, start, [start])]
        best: dict[str, tuple[float, float]] = {start: (0.0, 0.0)}
        found = None
        while queue:
            duration, distance, node, path = heapq.heappop(queue)
            if node == end:
                found = duration, distance, path
                break
            if duration > best[node][0]:
                continue
            for neighbour, edge_duration, edge_distance in self.edges.get(node, []):
                candidate = (duration + edge_duration, distance + edge_distance)
                if neighbour not in best or candidate[0] < best[neighbour][0]:
                    best[neighbour] = candidate
                    heapq.heappush(queue, (candidate[0], candidate[1], neighbour, path + [neighbour]))
        if found is None:
            return RouteObservation(destination, reason="no_result", endpoint_verified=False)
        duration, distance, path = found
        route_path = [self.nodes[node] for node in path]
        route_path.insert(0, origin)
        route_path.append(destination)
        return RouteObservation(destination, duration + (start_distance + end_distance) / self.speed_mps,
                                endpoint_verified=True, distance_m=distance + start_distance + end_distance, route_path=route_path)

    def _nearest(self, point):
        return min(((node, _distance(point, value)) for node, value in self.nodes.items()), key=lambda item: item[1], default=(None, math.inf))

    def provenance(self, *, participated: bool) -> dict:
        return {
            "participated": participated,
            "availability": self.info.availability,
            "coverageCity": self.info.coverage_city,
            "dataVersion": self.info.data_version,
            "dataDate": self.info.data_date,
            "downloadedAt": self.info.downloaded_at,
            "preparedAt": self.info.prepared_at,
            "coverageCheckAvailable": self.info.bounds is not None,
            "coverageBoundary": self.info.coverage_boundary,
            "attribution": self.info.attribution,
            "license": self.info.license,
            "limitations": list(self.info.limitations),
        }


class OsmOfflineProvider:
    network = False

    def __init__(self, cache: OsmCache):
        self.cache = cache
        self.identity = ("osm_offline", cache.info.data_version or "unknown")

    async def query_walking_time(self, origin, destination, deadline):
        if asyncio.get_running_loop().time() >= deadline:
            return RouteObservation(destination, reason="deadline", endpoint_verified=False)
        return await asyncio.to_thread(self.cache.route, origin, destination)


class OsmOfflineEngine:
    """Adapter around the shared isochrone algorithm and an offline provider."""

    def __init__(self, cache: OsmCache):
        self.cache = cache

    async def run(self, origin, budget, token: CancelToken, on_progress=None):
        request = IsochroneRequest(origin, "bd09ll", budget=budget, qps=None)
        return await compute_isochrone(request, OsmOfflineProvider(self.cache), token, on_progress=on_progress)


def _as_text(value: Any) -> str | None:
    return str(value) if value is not None else None
