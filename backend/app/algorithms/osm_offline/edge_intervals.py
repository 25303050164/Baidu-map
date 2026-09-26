"""Each directed edge contributes only its reachable prefix.

For length L, weight w, arrival d(u), B: [0, L*clamp((B-d(u))/w)].
Virtual-source slices additionally start at the projection's linear reference.
Unioning these slices keeps disjoint end intervals disjoint, even when BOTH
endpoints are settled. Costs need not be identical in opposite directions.

``weight`` follows ``routing.cutoff_dijkstra``: ``travel_time_s`` for the isochrone and the
metric ``length`` for the 1000 m walking-distance service field (方案 §5.4). Both must be
parameterized together — recovering intervals with time while the search ran on distance
would draw the boundary of a different question than the one that was asked.

``flipped`` marks a search that walks *against* the stored geometry (a reverse search from
facility entrances): the reachable part is then the tail of the line, and intervals have to
be measured from the far end instead of the near one.
"""
from dataclasses import dataclass

from shapely import union_all
from shapely.ops import substring

from .routing import TIME_WEIGHT


@dataclass
class ReachableNetwork:
    geometry: object
    segments: list
    full_edges: int
    partial_edges: int


def reachable_intervals(graph, distances, budget, source_intervals=(), weight=TIME_WEIGHT,
                        flipped=False):
    ranges = {}
    for u, arrival in distances.items():
        for _, v, key, data in graph.out_edges(u, keys=True, data=True):
            end = data["geometry"].length * min(1, max(0, (budget-arrival)/data[weight]))
            ranges.setdefault((u, v, key), []).append((0.0, end))
    for edge, start in source_intervals:
        data = graph.edges[edge]
        line = data["geometry"]
        end = min(line.length, start + line.length * budget/data[weight])
        ranges.setdefault(edge, []).append((start, end))
    segments = []
    full, partial = 0, 0
    for edge, intervals in ranges.items():
        merged = []
        for start, end in sorted(intervals):
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
            else:
                merged.append((start, end))
        line = graph.edges[edge]["geometry"]
        if len(merged) == 1 and merged[0] == (0, line.length):
            full += 1
        else:
            partial += 1
        segments.extend(
            # 反向搜索达到的是几何的尾段：与近端量出的 [start, end] 镜像。
            substring(line, line.length - end, line.length - start) if flipped
            else substring(line, start, end)
            for start, end in merged)
    # Zero-length intervals become Points: exactly-budget endpoints stay inside.
    return ReachableNetwork(union_all(segments), segments, full, partial)
