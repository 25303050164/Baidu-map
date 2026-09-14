"""Each directed edge contributes only its reachable prefix.

For length L, weight w, arrival d(u), B: [0, L*clamp((B-d(u))/w)].
Virtual-source slices additionally start at the projection's linear reference.
Unioning these slices keeps disjoint end intervals disjoint, even when BOTH
endpoints are settled. Costs need not be identical in opposite directions.
"""
from dataclasses import dataclass

from shapely import union_all
from shapely.ops import substring


@dataclass
class ReachableNetwork:
    geometry: object
    segments: list
    full_edges: int
    partial_edges: int


def reachable_intervals(graph, distances, budget, source_intervals=()):
    ranges = {}
    for u, arrival in distances.items():
        for _, v, key, data in graph.out_edges(u, keys=True, data=True):
            end = data["geometry"].length * min(1, max(0, (budget-arrival)/data["travel_time_s"]))
            ranges.setdefault((u, v, key), []).append((0.0, end))
    for edge, start in source_intervals:
        data = graph.edges[edge]
        line = data["geometry"]
        end = min(line.length, start + line.length * budget/data["travel_time_s"])
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
        segments.extend(substring(line, start, end) for start, end in merged)
    # Zero-length intervals become Points: exactly-budget endpoints stay inside.
    return ReachableNetwork(union_all(segments), segments, full, partial)
