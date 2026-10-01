"""The OSM stand-in returns routes the way Baidu does: a polyline and the time to each vertex."""
import math

import networkx as nx
import pytest
from shapely.geometry import LineString

from app.algorithms.osm_offline.graph_store import PEDESTRIAN_ATTRS, GraphStore
from app.algorithms.osm_offline.routing import cutoff_dijkstra, cutoff_dijkstra_tree
from app.checkups.accessibility_stage import public_point
from tools.graph_walking_benchmark import Walker

# A small street pattern near Shanghai in UTM 51N, so bd09 conversions are realistic.
X0, Y0 = 357000.0, 3462000.0


def network():
    g = nx.MultiDiGraph(crs="EPSG:32651", osm_data_version="test", walking_speed_mps=1)

    def street(u, v, coords):
        coords = [(X0 + x, Y0 + y) for x, y in coords]
        line = LineString(coords)
        g.add_node(u, x=coords[0][0], y=coords[0][1])
        g.add_node(v, x=coords[-1][0], y=coords[-1][1])
        data = dict.fromkeys(PEDESTRIAN_ATTRS)
        data.update(geometry=line, length=line.length, travel_time_s=line.length, osmid=hash((u, v)) % 1000)
        g.add_edge(u, v, **data)
        g.add_edge(v, u, **{**data, "geometry": LineString(coords[::-1])})
    street("a", "b", [(0, 0), (200, 0), (400, 0)])
    street("b", "c", [(400, 0), (400, 300)])
    street("a", "d", [(0, 0), (0, 500)])
    street("d", "c", [(0, 500), (400, 500), (400, 300)])  # the long way round
    return GraphStore(g, speed=1, crs="EPSG:32651")


def walker():
    store = network()
    return store, Walker(store, public_point(store.projection, (X0 + 10, Y0)), graph_speed=1)


def test_the_tree_search_has_exactly_the_plain_search_costs():
    store = network()
    seeds = {"a": 0.0}
    assert {n: c for n, (c, _, _) in cutoff_dijkstra_tree(store.graph, seeds, 2000).items()} == \
        cutoff_dijkstra(store.graph, seeds, 2000)


def test_the_route_ends_at_the_snapped_point_with_the_time_at_reports():
    store, w = walker()
    x, y = X0 + 405, Y0 + 250
    seconds, snapped, _ = w.at(x, y)
    vertices, times = w.route(x, y)
    assert math.dist(vertices[-1], (snapped.x, snapped.y)) < 1e-6
    assert times[-1] == pytest.approx(seconds)
    assert times == sorted(times) and times[0] == 0
    # Out along a-b from the origin's snap point, then up b-c: never the long way.
    assert math.dist(vertices[0], (w.snap.point.x, w.snap.point.y)) < 1e-6
    assert seconds == pytest.approx(390 + 250, abs=0.1)  # the origin is rounded to six decimals in bd09


def test_no_vertex_is_reached_later_by_a_direct_query_than_along_the_route():
    store, w = walker()
    vertices, times = w.route(X0 + 380, Y0 + 500)
    for (x, y), along in zip(vertices, times):
        direct, _, _ = w.at(x, y)
        assert direct <= along + 1e-6
