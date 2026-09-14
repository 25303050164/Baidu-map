import os
from pathlib import Path

import networkx as nx
import pytest
from shapely.geometry import LineString, Point

from app.algorithms.osm_offline.prepare import graph_from_frames, prepare_graph, simplify_walking_graph
from app.algorithms.osm_offline.graph_store import GraphStore, OsmDataError, load_graph_cache
from app.config import Settings
from test_osm_offline_core import edge, graph


def test_simplify_retains_attribute_changes_curves_components():
    pytest.importorskip("osmnx")
    g = graph()
    edge(g, 0, 1, [(0, 0), (20, 0)], reverse=True, highway="footway")
    edge(g, 1, 2, [(20, 0), (40, 0)], reverse=True, highway="footway")
    edge(g, 2, 3, [(40, 0), (60, 0)], reverse=True, highway="steps")
    edge(g, 3, 4, [(60, 0), (70, 10), (80, 0)], reverse=True, highway="steps")
    edge(g, 10, 11, [(0, 50), (20, 50)], reverse=True, access="private")
    simplified = simplify_walking_graph(g)
    assert 1 not in simplified
    assert 2 in simplified and 3 in simplified and 4 in simplified
    assert nx.number_weakly_connected_components(simplified) == 2
    assert any(d["geometry"].equals(LineString([(60, 0), (70, 10), (80, 0)])) for _, _, d in simplified.edges(data=True))
    GraphStore(simplified, speed=1, crs="EPSG:32651")


@pytest.mark.parametrize("attr", ["foot", "access", "bridge", "tunnel", "service", "oneway:foot"])
def test_simplify_preserves_each_pedestrian_attribute(attr):
    pytest.importorskip("osmnx")
    g = graph()
    edge(g, 0, 1, [(0, 0), (20, 0)], reverse=True, **{attr: "yes"})
    edge(g, 1, 2, [(20, 0), (40, 0)], reverse=True, **{attr: "no"})
    assert 1 in simplify_walking_graph(g)


def test_frames_respect_foot_direction_and_retain_all():
    gpd = pytest.importorskip("geopandas")
    nodes = gpd.GeoDataFrame({"id": [0, 1, 2, 3]}, geometry=[Point(0, 0), Point(100, 0), Point(0, 100), Point(100, 100)], crs="EPSG:32651")
    edges = gpd.GeoDataFrame({"id": [5, 6], "u": [0, 2], "v": [1, 3], "oneway:foot": ["yes", "-1"]},
                             geometry=[LineString([(0, 0), (100, 0)]), LineString([(0, 100), (100, 100)])], crs=nodes.crs)
    result = graph_from_frames(nodes, edges, Settings(_env_file=None), simplify=False)
    assert set(result.edges()) == {(0, 1), (3, 2)}
    assert nx.number_weakly_connected_components(result) == 2


def test_pbf_missing_is_explained(tmp_path):
    cfg = Settings(_env_file=None, osm_pbf_path=tmp_path / "missing.osm.pbf")
    with pytest.raises(OsmDataError, match="pbf_file_missing"):
        prepare_graph(cfg, source="test", downloaded_at="2026-09-14")


@pytest.mark.skipif(not os.environ.get("OSM_PBF_PATH") or not Path(os.environ.get("OSM_PBF_PATH", "")).is_file(),
                    reason="Optional: configure an existing Shanghai OSM_PBF_PATH")
def test_real_shanghai_pbf_smoke(tmp_path):
    """Offline optional integration test; reads a local PBF and never downloads."""
    pytest.importorskip("pyrosm")
    cfg = Settings(_env_file=None, osm_graph_cache_path=tmp_path / "shanghai.osm-cache")
    meta = prepare_graph(cfg, source="local-integration", downloaded_at="local")
    g = load_graph_cache(cfg.osm_graph_cache_path)
    assert g.number_of_nodes() == meta["node_count"] > 100
    assert g.number_of_edges() == meta["edge_count"] > 100
    del g  # Do not retain a second city graph while exercising startup loading.
    from app.algorithms.osm_offline.engine import OsmOfflineEngine
    from app.geo.coordinates import wgs84_to_bd09
    from life_circle.models import IsochroneRequest
    result = OsmOfflineEngine.load(cfg).compute(IsochroneRequest(wgs84_to_bd09(121.5, 31.2), "bd09ll"))
    assert result.result.geometry is not None
    assert result.result.quality in ("usable", "partial")
    assert result.diagnostics["network_requests"] == 0
