from dataclasses import dataclass

from shapely.geometry import Point

from .graph_store import OsmDataError


@dataclass(frozen=True)
class Snap:
    edge: tuple
    point: Point
    distance_m: float
    time_s: float
    budget_s: float
    seeds: dict
    # (directed edge, start distance along oriented geometry)
    source_intervals: tuple


def nearest_edge_source(store, origin, *, speed, max_distance, threshold=900):
    if not store.edge_ids:
        raise OsmDataError("nearest_edge_not_found")
    point = Point(origin)
    # All equidistant matches; stable edge ordering makes tie-breaking reproducible.
    matches = store.index.query_nearest(point, all_matches=True)
    edge = store.edge_ids[min(map(int, matches))]
    graph = store.graph
    u, v, _ = edge
    geometry = graph.edges[edge]["geometry"]
    position = geometry.project(point)
    projected = geometry.interpolate(position)
    distance = point.distance(projected)
    time_s = distance / speed
    budget = threshold - time_s
    evidence = {"snap_edge": list(edge), "snap_distance_m": distance,
                "snap_time_s": time_s, "network_budget_s": budget}
    if distance > max_distance:
        raise OsmDataError("snap_distance_exceeded", evidence)
    if budget <= 0:
        raise OsmDataError("snap_budget_exhausted", evidence)
    seeds, intervals = {}, []
    # Only the chosen physical edge and its reverse, never a nearby parallel way
    # or a crossing bridge. A shared coordinate does not create a graph junction.
    candidates = [edge]
    for key, data in graph.get_edge_data(v, u, default={}).items():
        reverse = (v, u, key)
        same_way = data.get("osmid", data.get("id")) == graph.edges[edge].get("osmid", graph.edges[edge].get("id"))
        if reverse != edge and same_way and data["geometry"].equals(geometry):
            candidates.append(reverse)
    for current in candidates:
        data = graph.edges[current]
        line = data["geometry"]
        start = line.project(projected)
        intervals.append((current, start))
        cost = data["travel_time_s"] * (1 - start / line.length)
        endpoint = current[1]
        seeds[endpoint] = min(seeds.get(endpoint, float("inf")), cost)
        if start == 0:
            seeds[current[0]] = 0.0
    return Snap(edge, projected, distance, time_s, budget, seeds, tuple(intervals))
