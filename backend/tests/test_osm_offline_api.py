import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import gc
import json
import socket
import threading
from types import SimpleNamespace

from fastapi.testclient import TestClient
from life_circle.models import IsochroneRequest
import networkx as nx
import pytest
from shapely.geometry import LineString, box, mapping, shape

from app.algorithms.osm_offline.engine import OsmOfflineEngine
from app.algorithms.osm_offline.lazy import LazyOsmOfflineEngine, resolve
from app.algorithms.osm_offline.graph_store import GraphStore, PEDESTRIAN_ATTRS, save_graph_cache
from app.config import Settings
from app.geo.coordinates import wgs84_to_bd09
from app.geo.projection import MetricProjection
from app.main import create_app as production_app
from app.osm_api import router as internal_osm_router


def create_app(settings):
    """Retained OSM internals, isolated test-only route."""
    app = production_app(settings)
    app.include_router(internal_osm_router)
    return app

ORIGIN = wgs84_to_bd09(121.5, 31.2)


def fixture(tmp_path, *, coverage=True):
    config = Settings(_env_file=None, osm_data_version="test", walk_speed_mps=1,
                      osm_graph_cache_path=tmp_path / "test.osm-cache")
    projection = MetricProjection(config.osm_metric_crs)
    x, y = projection.origin(ORIGIN)
    g = nx.MultiDiGraph(crs=config.osm_metric_crs, osm_data_version="test")
    for i, dx in enumerate((-1500, 0, 1500)):
        g.add_node(i, x=x+dx, y=y)
    for u, v in ((0, 1), (1, 0), (1, 2), (2, 1)):
        a, b = g.nodes[u], g.nodes[v]
        line = LineString([(a["x"], a["y"]), (b["x"], b["y"])])
        g.add_edge(u, v, **dict.fromkeys(PEDESTRIAN_ATTRS), osmid=1, geometry=line,
                   length=1500, travel_time_s=1500)
    save_graph_cache(g, config.osm_graph_cache_path)
    return config, GraphStore(g, speed=1, crs=config.osm_metric_crs), box(x-5000, y-5000, x+5000, y+5000) if coverage else None


def request():
    return IsochroneRequest(ORIGIN, "bd09ll")


def test_engine_polygon_and_diagnostics(tmp_path):
    cfg, store, coverage = fixture(tmp_path)
    result = OsmOfflineEngine(cfg, store, coverage).compute(request())
    assert result.result.quality == "usable"
    assert result.reachable_network.length == pytest.approx(1800)
    geometry = shape(result.result.geometry)
    assert geometry.is_valid and not geometry.is_empty
    assert geometry.bounds[0] < ORIGIN[0] < geometry.bounds[2]
    assert result.diagnostics["network_requests"] == 0
    assert all(result.diagnostics[key] >= 0 for key in ("snap_ms", "routing_ms", "edge_interval_ms", "polygon_ms", "total_ms"))


def test_extract_boundary_partial_quality(tmp_path):
    cfg, store, _ = fixture(tmp_path)
    x, y = store.projection.origin(ORIGIN)
    result = OsmOfflineEngine(cfg, store, box(x-950, y-1000, x+950, y+1000)).compute(request())
    assert result.result.quality == "partial"
    assert result.diagnostics["coverage_boundary_hit"] is True
    assert "graph_coverage_boundary" in result.result.warnings


def test_missing_coverage_not_usable(tmp_path):
    cfg, store, _ = fixture(tmp_path)
    result = OsmOfflineEngine(cfg, store).compute(request())
    assert result.result.quality == "partial" and result.result.geometry is not None
    assert result.diagnostics["coverage_check_available"] is False


def test_outside_coverage_insufficient(tmp_path):
    cfg, store, _ = fixture(tmp_path)
    result = OsmOfflineEngine(cfg, store, box(0, 0, 10, 10)).compute(request())
    assert result.result.quality == "insufficient"
    assert result.result.stop_reason == "origin_outside_coverage"
    assert result.result.geometry is None


def test_api_offline_and_422_envelope(tmp_path, monkeypatch):
    cfg, store, coverage = fixture(tmp_path)
    def forbidden(*args, **kwargs):
        pytest.fail("OSM runtime attempted network access")
    import life_circle.engine
    monkeypatch.setattr(life_circle.engine, "compute_isochrone", forbidden)
    with TestClient(create_app(cfg)) as client:
        # Windows creates a loopback socketpair to start its event loop first.
        monkeypatch.setattr(socket, "create_connection", forbidden)
        monkeypatch.setattr(socket.socket, "connect", forbidden)
        client.app.state.osm_offline = OsmOfflineEngine(cfg, store, coverage)
        body = {"origin": {"lng": ORIGIN[0], "lat": ORIGIN[1]}, "coordinate_system": "bd09ll", "algorithm": "osm_offline"}
        response = client.post("/api/v1/analysis/osm_offline", json=body)
        assert response.status_code == 200
        payload = response.json()
        assert payload["algorithm"]["algorithm"] == "osm_offline"
        assert payload["algorithm"]["quality"] == "usable"
        assert payload["status"] == "partial"  # Facilities deliberately unexecuted.
        assert "success" not in payload
        bad = client.post("/api/v1/analysis/osm_offline", json={**body, "threshold": 901})
        assert bad.status_code == 422
        assert bad.json()["status"] == "failed"


def test_missing_cache_api_not_empty(tmp_path):
    cfg = Settings(_env_file=None, osm_graph_cache_path=tmp_path / "missing")
    with TestClient(create_app(cfg)) as client:
        response = client.post("/api/v1/analysis/osm_offline", json={"origin": {"lng":121.5,"lat":31.2}, "coordinate_system":"bd09ll"})
        assert response.status_code == 200
        assert response.json()["status"] == "failed"
        assert response.json()["algorithm"]["quality"] == "insufficient"
        assert response.json()["algorithm"]["stopReason"] == "graph_cache_missing"


def test_load_once_concurrent_graph_immutability(tmp_path, monkeypatch):
    cfg, store, coverage = fixture(tmp_path)
    import app.algorithms.osm_offline.engine as module
    original = module.load_graph_cache
    calls = []
    def load(path, *args):
        calls.append(path)
        return original(path, *args)
    monkeypatch.setattr(module, "load_graph_cache", load)
    with TestClient(create_app(cfg)) as client:
        engine = client.app.state.osm_offline
        before = nx.node_link_data(engine.store.graph)
        with ThreadPoolExecutor(max_workers=4) as pool:
            outputs = list(pool.map(engine.compute, [request()]*8))
        assert len(calls) == 1
        assert all(o.result.geometry == outputs[0].result.geometry for o in outputs)
        assert before == nx.node_link_data(engine.store.graph)


def test_the_parsed_graph_is_freed_without_a_cyclic_collection(tmp_path):
    cfg, _, _ = fixture(tmp_path)
    x, y = MetricProjection(cfg.osm_metric_crs).origin(ORIGIN)
    g = nx.MultiDiGraph(crs=cfg.osm_metric_crs, osm_data_version="test")
    for i in range(300):
        g.add_node(i, x=x + 10 * i, y=y)
    for u, v in [(i, i + 1) for i in range(299)] + [(i + 1, i) for i in range(299)]:
        line = LineString([(g.nodes[u]["x"], y), (g.nodes[v]["x"], y)])
        g.add_edge(u, v, **dict.fromkeys(PEDESTRIAN_ATTRS), osmid=1, geometry=line, length=10, travel_time_s=10)
    save_graph_cache(g, cfg.osm_graph_cache_path)
    del g, line
    gc.collect()
    gc.disable()
    try:
        engine = OsmOfflineEngine.load(cfg)
        left = gc.collect()
    finally:
        gc.enable()
    assert engine.store is not None
    # Only the emptied graph's own shell: its node, edge and attribute dicts are gone already.
    assert left < 30


def test_loading_the_graph_runs_no_full_collection_and_freezes_it(tmp_path, monkeypatch):
    cfg, _, _ = fixture(tmp_path)
    import app.algorithms.osm_offline.engine as module
    original, loading, full = module.load_graph_cache, [], []

    def load(path, *args):
        loading.append(True)
        try:
            # Enough new containers for a full collection under the thresholds below.
            kept = [[i] for i in range(20_000)]
            return original(path, *args)
        finally:
            del kept
            loading.clear()

    def watch(phase, info):
        if phase == "start" and info["generation"] == 2 and loading:
            full.append(info)

    monkeypatch.setattr(module, "load_graph_cache", load)
    threshold = gc.get_threshold()
    gc.callbacks.append(watch)
    # The rest of the test process out of the way (a full collection triggers only once
    # the old generation has grown by a quarter since the last one), and thresholds low.
    gc.freeze()
    gc.collect()
    gc.set_threshold(10, 1, 1)
    try:
        # The same allocations outside the load do trigger full collections.
        loading.append(True)
        kept = [[i] for i in range(20_000)]
        loading.clear()
        assert full
        del kept
        full.clear()
        lazy = LazyOsmOfflineEngine(cfg)
        assert lazy.get().store is not None
        assert full == []
        assert gc.get_threshold() == (10, 1, 1)
        assert gc.get_freeze_count() > 0
    finally:
        gc.set_threshold(*threshold)
        gc.callbacks.remove(watch)
        gc.unfreeze()


def line_graph(cfg, nodes):
    x, y = MetricProjection(cfg.osm_metric_crs).origin(ORIGIN)
    g = nx.MultiDiGraph(crs=cfg.osm_metric_crs, osm_data_version="test")
    for i in range(nodes):
        g.add_node(i, x=x + 10 * i, y=y)
    for u, v in [(i, i + 1) for i in range(nodes - 1)] + [(i + 1, i) for i in range(nodes - 1)]:
        line = LineString([(g.nodes[u]["x"], y), (g.nodes[v]["x"], y)])
        g.add_edge(u, v, **dict.fromkeys(PEDESTRIAN_ATTRS), osmid=1, geometry=line, length=10, travel_time_s=10)
    return g


def test_the_first_load_tells_how_far_it_has_got(tmp_path, monkeypatch):
    cfg, _, _ = fixture(tmp_path)
    save_graph_cache(line_graph(cfg, 300), cfg.osm_graph_cache_path)
    import app.algorithms.osm_offline.graph_store as module
    monkeypatch.setattr(module, "PROGRESS_EDGES", 100)
    heard = []
    engine = OsmOfflineEngine.load(cfg, progress=lambda *told: heard.append(told))
    assert engine.store is not None and engine.store.edge_count == 598
    steps = [told[0] for told in heard]
    assert sorted(set(steps), key=steps.index) == ["graph_read", "graph_build", "graph_check", "graph_index"]
    assert heard[0] == ("graph_read",) and heard[-1] == ("graph_index",)
    # Counts of edges really done, each step against its own known total.
    for step in ("graph_build", "graph_check"):
        assert [told[1:] for told in heard if told[0] == step] == [(n, 598) for n in range(0, 598, 100)]


def test_the_lazy_holder_shows_its_load_while_it_runs(tmp_path, monkeypatch):
    cfg, _, _ = fixture(tmp_path)
    lazy = LazyOsmOfflineEngine(cfg)
    import app.algorithms.osm_offline.engine as module
    original, seen = module.GraphStore, []

    def store(graph, **kwargs):
        seen.append(lazy.loading)
        return original(graph, **kwargs)

    monkeypatch.setattr(module, "GraphStore", store)
    assert lazy.loading is None
    assert lazy.get().store is not None
    assert seen == [("graph_build", 0, 4)]
    assert lazy.loading is None and lazy.state == "ready"


def test_whoever_waits_for_the_graph_hears_each_change_once():
    offline = SimpleNamespace(loading=None)
    steps = [("graph_read", None, None), ("graph_build", 0, 90), ("graph_build", 0, 90),
             ("graph_build", 40, 90), ("graph_check", 0, 90), ("graph_index", None, None)]
    moved, heard = threading.Event(), []

    def get():
        for step in steps:
            offline.loading = step
            moved.wait(1)
            moved.clear()
        offline.loading = None
        return "engine"

    def report(*told):
        heard.append(told)
        moved.set()

    offline.get = get
    assert asyncio.run(resolve(offline, report, every=0.01)) == "engine"
    # The repeated account is told once; nothing is told once the load is over.
    assert heard == [steps[0], steps[1], steps[3], steps[4], steps[5]]


def test_a_failed_load_reaches_whoever_waits_for_it():
    def get():
        raise RuntimeError("graph_cache_corrupt")
    with pytest.raises(RuntimeError, match="graph_cache_corrupt"):
        asyncio.run(resolve(SimpleNamespace(get=get), every=0.01))


def test_bad_cache_config_and_coverage(tmp_path):
    cfg, _, _ = fixture(tmp_path)
    cfg.walk_speed_mps = 2
    assert OsmOfflineEngine.load(cfg).unavailable_reason == "cache_speed_or_cost_mismatch"
    cfg.walk_speed_mps = 1
    cfg.osm_data_version = "other"
    assert OsmOfflineEngine.load(cfg).unavailable_reason == "cache_data_version_mismatch"
    cfg.osm_data_version = "test"
    cfg.osm_coverage_boundary_path = tmp_path / "missing.geojson"
    assert OsmOfflineEngine.load(cfg).coverage_reason == "coverage_check_invalid"


def test_unconfigured_osm_does_not_read_city_cache(tmp_path, monkeypatch):
    cfg, _, _ = fixture(tmp_path)
    cfg.osm_data_version = "unconfigured"
    import app.algorithms.osm_offline.engine as module
    def forbidden(path):
        pytest.fail("Unconfigured service read the whole city cache")
    monkeypatch.setattr(module, "load_graph_cache", forbidden)
    assert OsmOfflineEngine.load(cfg).unavailable_reason == "osm_data_version_unconfigured"


def test_request_typescript_defaults_follow_pydantic():
    from app.contracts import OsmOfflineRequest
    from tools.export_contract import typescript
    generated = typescript([OsmOfflineRequest], request_models=[OsmOfflineRequest])
    assert 'algorithm?: "osm_offline"' in generated
    assert 'threshold?: 900' in generated
    assert 'origin: Origin' in generated


def test_500_uses_existing_envelope(tmp_path, monkeypatch):
    cfg, _, _ = fixture(tmp_path)
    with TestClient(create_app(cfg), raise_server_exceptions=False) as client:
        def broken(request):
            raise RuntimeError("private details")
        monkeypatch.setattr(client.app.state.osm_offline, "compute", broken)
        response = client.post("/api/v1/analysis/osm_offline", json={"origin":{"lng":121.5,"lat":31.2},"coordinate_system":"bd09ll"})
        assert response.status_code == 500
        assert response.json()["errors"][0]["code"] == "INTERNAL_ERROR"
        assert "private details" not in response.text
