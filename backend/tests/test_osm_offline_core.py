import copy
import json
import subprocess
import sys

import networkx as nx
import pytest
from shapely.geometry import LineString, Point, box

from app.geo.coordinates import bd09_to_wgs84, wgs84_to_bd09
from app.geo.projection import MetricProjection
from app.algorithms.osm_offline.graph_store import (
    GraphStore, OsmDataError, PEDESTRIAN_ATTRS, edge_travel_time, load_graph_cache, save_graph_cache,
)
from app.algorithms.osm_offline.snap import nearest_edge_source
from app.algorithms.osm_offline.routing import cutoff_dijkstra
from app.algorithms.osm_offline.edge_intervals import reachable_intervals
from app.algorithms.osm_offline.polygonize import polygonize
from app.algorithms.osm_offline.coverage import boundary_hit, read_coverage


def graph():
    return nx.MultiDiGraph(crs="EPSG:32651", osm_data_version="test", walking_speed_mps=1)


def edge(g, u, v, coords, *, reverse=False, cost=None, **attributes):
    line = LineString(coords)
    g.add_node(u, x=coords[0][0], y=coords[0][1])
    g.add_node(v, x=coords[-1][0], y=coords[-1][1])
    data = dict.fromkeys(PEDESTRIAN_ATTRS)
    data.update(geometry=line, length=line.length, travel_time_s=cost if cost is not None else line.length, osmid=1)
    data.update(attributes)
    g.add_edge(u, v, **data)
    if reverse:
        g.add_edge(v, u, **{**data, "geometry": LineString(coords[::-1])})


def store(g):
    return GraphStore(g, speed=1, crs="EPSG:32651")


def snap(s, point):
    return nearest_edge_source(s, point, speed=1, max_distance=1000)


@pytest.mark.parametrize("point", [(121.5, 31.2), (121.521, 31.301), (121.1, 30.9)])
def test_coordinate_roundtrip(point):
    assert bd09_to_wgs84(*wgs84_to_bd09(*point)) == pytest.approx(point, abs=2e-6)
    p = MetricProjection("EPSG:32651")
    assert p.inverse.transform(*p.forward.transform(*point)) == pytest.approx(point, abs=1e-8)


def test_edge_cost():
    assert edge_travel_time({"length": 130}, 1.3) == 100


@pytest.mark.parametrize("cost,inside", [(899.999, True), (900, True), (900.001, False)])
def test_threshold_900_inclusive(cost, inside):
    g = graph()
    edge(g, "a", "b", [(0, 0), (cost, 0)])
    assert ("b" in cutoff_dijkstra(g, {"a": 0}, 900)) is inside
    result = reachable_intervals(g, {"a": 0}, 900)
    assert result.geometry.covers(Point(min(cost, 900), 0))


def test_basic_cutoff_dijkstra():
    g = graph()
    for a, b, x in [("a", "b", 0), ("b", "c", 400), ("c", "d", 800)]:
        edge(g, a, b, [(x, 0), (x+400, 0)], reverse=True)
    assert cutoff_dijkstra(g, {"a": 0}, 900) == {"a": 0, "b": 400, "c": 800}


def test_snap_middle_of_edge():
    g = graph()
    edge(g, "a", "b", [(0, 0), (400, 0)], reverse=True)
    s = snap(store(g), (200, 0))
    assert s.seeds == {"a": 200, "b": 200}
    assert s.point.equals(Point(200, 0))
    assert reachable_intervals(g, {}, 50, s.source_intervals).geometry.equals(LineString([(150, 0), (250, 0)]))


def test_directed_snap():
    g = graph()
    edge(g, "a", "b", [(0, 0), (400, 0)])
    s = snap(store(g), (200, 0))
    assert s.seeds == {"b": 200}
    network = reachable_intervals(g, cutoff_dijkstra(g, s.seeds, 50), 50, s.source_intervals)
    assert network.geometry.equals(LineString([(200, 0), (250, 0)]))
    endpoint = snap(store(g), (0, 0))
    assert endpoint.seeds["a"] == 0


def test_single_sided_partial_edge():
    g = graph()
    edge(g, "a", "b", [(0, 0), (100, 0)])
    network = reachable_intervals(g, {"a": 850}, 900)
    assert network.geometry.equals(LineString([(0, 0), (50, 0)]))


def test_boundary_edge_two_sided():
    g = graph()
    edge(g, "u", "v", [(0, 0), (200, 0)], reverse=True)
    network = reachable_intervals(g, {"u": 850, "v": 860}, 900)
    assert network.geometry.length == 90
    assert network.geometry.covers(Point(50, 0))
    assert network.geometry.covers(Point(160, 0))
    assert not network.geometry.covers(Point(100, 0))


def test_curved_partial_edge():
    g = graph()
    edge(g, "a", "b", [(0, 0), (0, 50), (50, 50)])
    network = reachable_intervals(g, {"a": 850}, 900)
    assert network.geometry.equals(LineString([(0, 0), (0, 50)]))


def test_edge_geometry_orientation():
    g = graph()
    edge(g, "a", "b", [(0, 0), (100, 0)])
    g.edges["a", "b", 0]["geometry"] = LineString([(100, 0), (0, 0)])
    indexed = store(g)
    assert indexed.diagnostics["geometry_reversed_edges"] == 1
    assert reachable_intervals(indexed.graph, {"a": 850}, 900).geometry.equals(LineString([(0, 0), (50, 0)]))


def test_disconnected_graph():
    g = graph()
    edge(g, "a", "b", [(0, 0), (100, 0)], reverse=True)
    edge(g, "x", "y", [(0, 1), (100, 1)], reverse=True)
    indexed = store(g)
    assert len(indexed.component_sizes) == 2
    s = snap(indexed, (50, 0))
    assert set(cutoff_dijkstra(indexed.graph, s.seeds, 900)) == {"a", "b"}


def test_snap_cost_consumes_budget():
    g = graph()
    edge(g, "a", "b", [(0, 0), (2000, 0)])
    s = snap(store(g), (1000, 130))
    assert s.time_s == 130 and s.budget_s == 770
    assert reachable_intervals(g, {}, s.budget_s, s.source_intervals).geometry.length == 770


def test_graph_cache_roundtrip(tmp_path):
    g = graph()
    edge(g, "a", "b", [(0, 0), (0, 50), (50, 50)], reverse=True, highway="footway", access="yes")
    target = tmp_path / "graph.osm-cache"
    save_graph_cache(g, target)
    # Exercise a genuinely separate interpreter (no live Shapely objects retained).
    subprocess.run([sys.executable, "-c", "from app.algorithms.osm_offline.graph_store import load_graph_cache, save_graph_cache; import sys; save_graph_cache(load_graph_cache(sys.argv[1]), sys.argv[1])", str(target)], check=True)
    reloaded = load_graph_cache(target)
    assert list(g.nodes(data=True)) == list(reloaded.nodes(data=True))
    assert list(g.edges(keys=True, data=True)) == list(reloaded.edges(keys=True, data=True))
    store(reloaded)


def test_extract_boundary_partial():
    assert boundary_hit(LineString([(0, 0), (950, 0)]), box(-1000, -1000, 1000, 1000), 100)
    assert not boundary_hit(LineString([(0, 0), (850, 0)]), box(-1000, -1000, 1000, 1000), 100)


def test_polygon_validity():
    g = graph()
    edge(g, "a", "b", [(0, 0), (100, 0)])
    result = polygonize(reachable_intervals(g, {"a": 0}, 900).geometry, 10)
    assert result.is_valid and not result.is_empty and result.geom_type == "Polygon"
    assert result.bounds == (-10, -10, 110, 10)


def test_global_graph_immutability():
    g = graph()
    edge(g, "a", "b", [(0, 0), (2000, 0)], reverse=True)
    indexed = store(g)
    before = copy.deepcopy(indexed.graph)
    for point in [(400, 20), (1400, 30)]:
        s = snap(indexed, point)
        d = cutoff_dijkstra(indexed.graph, s.seeds, s.budget_s)
        reachable_intervals(indexed.graph, d, s.budget_s, s.source_intervals)
    assert nx.utils.graphs_equal(before, indexed.graph)
    assert nx.is_frozen(indexed.graph)


def test_parallel_edges_do_not_create_reverse_permission():
    g = graph()
    edge(g, "a", "b", [(0, 0), (400, 0)])
    edge(g, "b", "a", [(400, 0), (200, 10), (0, 0)], osmid=2)
    assert "a" not in snap(store(g), (200, 0)).seeds


@pytest.mark.parametrize("crs", ["EPSG:4326", "EPSG:2263"])
def test_reject_non_metric_crs(crs):
    with pytest.raises(ValueError):
        MetricProjection(crs)


def test_invalid_cache(tmp_path):
    with pytest.raises(OsmDataError, match="missing"):
        load_graph_cache(tmp_path / "missing")
    target = tmp_path / "bad"
    target.write_text("not gzip")
    with pytest.raises(OsmDataError, match="corrupt"):
        load_graph_cache(target)


@pytest.mark.parametrize("attribute", ["length", "travel_time_s", "access"])
def test_cache_missing_attributes_rejected(attribute):
    g = graph()
    edge(g, "a", "b", [(0, 0), (100, 0)])
    del g.edges["a", "b", 0][attribute]
    with pytest.raises(OsmDataError):
        store(g)


def test_geometry_missing_fallback_and_endpoint_mismatch():
    g = graph()
    edge(g, "a", "b", [(0, 0), (100, 0)])
    del g.edges["a", "b", 0]["geometry"]
    assert store(g).diagnostics["geometry_fallback_edges"] == 1
    g.edges["a", "b", 0]["geometry"] = LineString([(200, 0), (300, 0)])
    with pytest.raises(OsmDataError, match="endpoint_mismatch"):
        store(g)


def test_snap_failures_and_empty_network():
    g = graph()
    with pytest.raises(OsmDataError, match="not_found"):
        snap(store(g), (0, 0))
    edge(g, "a", "b", [(0, 0), (100, 0)])
    with pytest.raises(OsmDataError, match="distance_exceeded") as failure:
        nearest_edge_source(store(g), (0, 130), speed=1, max_distance=100)
    assert failure.value.diagnostics["snap_distance_m"] == 130
    with pytest.raises(OsmDataError, match="budget_exhausted"):
        snap(store(g), (0, 900))


def test_mult_polygon_keeps_disconnected_islands():
    from shapely import union_all
    result = polygonize(union_all([LineString([(0, 0), (10, 0)]), LineString([(100, 0), (110, 0)])]), 5)
    assert result.geom_type == "MultiPolygon" and result.is_valid


def test_dijkstra_matches_networkx_virtual_source():
    # Independent NetworkX oracle checks seeded relaxation on cyclic multigraphs.
    import random
    rng = random.Random(9)
    for _ in range(20):
        g = nx.MultiDiGraph()
        g.add_nodes_from(range(12))
        for _ in range(50):
            g.add_edge(rng.randrange(12), rng.randrange(12), travel_time_s=rng.uniform(0.1, 400))
        seeds = {0: 65, 4: 100, 8: 901}
        expected_graph = g.copy()
        for node, cost in seeds.items():
            expected_graph.add_edge("virtual", node, travel_time_s=cost)
        expected = nx.single_source_dijkstra_path_length(expected_graph, "virtual", cutoff=900, weight="travel_time_s")
        del expected["virtual"]
        assert cutoff_dijkstra(g, seeds, 900) == expected


def test_coverage_poly_holes(tmp_path):
    target = tmp_path / "coverage.poly"
    target.write_text("test\n1\n0 0\n2 0\n2 2\n0 2\n0 0\nEND\n!1\n0.5 0.5\n1 0.5\n1 1\n0.5 1\n0.5 0.5\nEND\nEND\n")
    assert read_coverage(target).area == 3.75
