"""Cutoff Dijkstra with request-local virtual-source initial costs.

The search is run in whatever cost the caller declares through ``weight``: the time field
``travel_time_s`` for the 15-minute isochrone, and the metric ``length`` for the 1000 m
walking-distance service field (方案 §5.4). ``length`` and ``travel_time_s`` are both
checked by ``GraphStore`` (``travel_time_s == length / walk_speed_mps``), so the two
weights can never drift apart into two different graphs.
"""
import heapq
import itertools

#: 权重字段名：时间（秒）与距离（米）。``GraphStore`` 校验两者一致（time == length / speed）。
TIME_WEIGHT = "travel_time_s"
LENGTH_WEIGHT = "length"


def cutoff_dijkstra(graph, seeds, budget, weight=TIME_WEIGHT):
    """Settled nodes with their cost from the seeds, ignoring everything beyond ``budget``.

    ``seeds`` are initial costs in the same unit as ``weight`` (virtual sources attached to
    an edge carry the cost of the way from the real source to that attachment point).

    Seeds whose node is absent from ``graph`` are dropped rather than raising. That is not
    a defensive special case: the service field runs the same seed set on two filtered
    views (方案 §5.2), and an entrance attached to a road only the optimistic view allows
    has no node at all in the strict view. Dropping it is the honest answer -- no permitted
    edge touches that node, so no route in the strict view can pass through it. Every node
    in the result is therefore a node of ``graph``, which is what keeps the interval
    recovery downstream from looking up an edge the graph does not have.
    """
    owned = cutoff_dijkstra_owners(graph, {node: (cost, None) for node, cost in seeds.items()},
                                   budget, weight=weight)
    return {node: cost for node, (cost, _) in owned.items()}


def cutoff_dijkstra_owners(graph, seeds, budget, weight=TIME_WEIGHT):
    """Same search, but every settled node also carries **which** source reached it.

    ``seeds`` maps a node to ``(cost, owner)``; the result maps a node to ``(cost, owner)``.
    The owner is the seed the winning route started from, so a multi-source search over all
    facility entrances simultaneously answers "how far" and "which facility" (方案 §6.2
    requires the nearest known facility per cell as part of the cell's evidence).

    Ties keep the owner that got there first at the lower cost: the relaxation is strict
    (``candidate < best``) and the heap is ordered by insertion sequence, so the result is
    deterministic for a given seed order.
    """
    sequence = itertools.count()
    available = []
    for node, (cost, owner) in seeds.items():
        if cost <= budget and node in graph:
            available.append((cost, node, owner))
    queue = [(cost, next(sequence), node, owner) for cost, node, owner in available]
    heapq.heapify(queue)
    best = {node: (cost, owner) for cost, node, owner in available}
    settled = {}
    while queue:
        cost, _, node, owner = heapq.heappop(queue)
        if node in settled:
            continue
        settled[node] = (cost, owner)
        for neighbor, edges in graph.adj[node].items():
            for data in edges.values():
                candidate = cost + data[weight]
                current = best.get(neighbor)
                if candidate <= budget and (current is None or candidate < current[0]):
                    best[neighbor] = (candidate, owner)
                    heapq.heappush(queue, (candidate, next(sequence), neighbor, owner))
    return settled
