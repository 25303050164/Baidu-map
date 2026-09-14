"""Cutoff Dijkstra with request-local virtual-source initial costs."""
import heapq
import itertools


def cutoff_dijkstra(graph, seeds, budget):
    sequence = itertools.count()
    queue = [(cost, next(sequence), node) for node, cost in seeds.items() if cost <= budget]
    heapq.heapify(queue)
    best = {node: cost for node, cost in seeds.items() if cost <= budget}
    settled = {}
    while queue:
        cost, _, node = heapq.heappop(queue)
        if node in settled:
            continue
        settled[node] = cost
        for neighbor, edges in graph.adj[node].items():
            for data in edges.values():
                candidate = cost + data["travel_time_s"]
                if candidate <= budget and candidate < best.get(neighbor, float("inf")):
                    best[neighbor] = candidate
                    heapq.heappush(queue, (candidate, next(sequence), neighbor))
    return settled
