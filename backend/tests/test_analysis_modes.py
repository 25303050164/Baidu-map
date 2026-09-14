import json
import time

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def cache_fixture(path):
    (path / "metadata.json").write_text(json.dumps({
        "coverageCity": "测试市",
        "dataVersion": "fixture-v1",
        "dataDate": "2026-09-12",
        "coverage": [116.38, 39.88, 116.42, 39.92],
        "coordinateSystem": "bd09ll",
    }), encoding="utf-8")
    (path / "graph.json").write_text(json.dumps({
        "nodes": [
            {"id": "a", "lng": 116.39, "lat": 39.89},
            {"id": "b", "lng": 116.41, "lat": 39.89},
            {"id": "c", "lng": 116.41, "lat": 39.91},
            {"id": "d", "lng": 116.39, "lat": 39.91},
        ],
        "edges": [
            {"from": "a", "to": "b"}, {"from": "b", "to": "c"},
            {"from": "c", "to": "d"}, {"from": "d", "to": "a"},
        ],
    }), encoding="utf-8")


def body(key="mode-test", mode=None, center=None):
    value = {"center": center or {"lng": 116.4, "lat": 39.9}, "coordinateSystem": "bd09ll",
             "budget": 200, "clientRequestId": key}
    if mode is not None:
        value["analysisMode"] = mode
    return value


def wait_for_terminal(client, task_id):
    for _ in range(200):
        result = client.get(f"/api/analyses/{task_id}").json()
        if result["status"] in {"completed", "failed", "cancelled"}:
            return result
        time.sleep(0.01)
    raise AssertionError("analysis task did not finish")


def test_capabilities_hide_unconfigured_osm_and_default_mode_is_baidu():
    settings = Settings(_env_file=None, analysis_provider="synthetic")
    with TestClient(create_app(settings)) as client:
        capabilities = client.get("/api/analyses/capabilities").json()
        assert capabilities["modes"]["baidu_online"]["available"] is True
        assert capabilities["modes"]["osm_offline"]["available"] is False
        created = client.post("/api/analyses", json=body()).json()
        assert created["analysisMode"] == "baidu_online"


def test_osm_task_is_offline_and_idempotency_includes_mode(tmp_path):
    cache_fixture(tmp_path)
    settings = Settings(_env_file=None, analysis_provider="synthetic", osm_cache_path=tmp_path)
    with TestClient(create_app(settings)) as client:
        capabilities = client.get("/api/analyses/capabilities").json()
        assert capabilities["modes"]["osm_offline"]["available"] is True
        created = client.post("/api/analyses", json=body(mode="osm_offline")).json()
        assert created["dataSource"] == "osm_offline"
        assert created["analysisMode"] == "osm_offline"
        assert client.post("/api/analyses", json=body(mode="baidu_online")).status_code == 409
        state = wait_for_terminal(client, created["taskId"])
        assert state["requests"] == state["networkRequests"] == 0
        result = client.get(f"/api/analyses/{created['taskId']}/result")
        assert result.status_code == 200
        payload = result.json()
        assert payload["analysisMode"] == "osm_offline"
        assert payload["provenance"]["osm"]["participated"] is True
        assert payload["provenance"]["osm"]["dataDate"] == "2026-09-12"


def test_hybrid_keeps_two_results_without_union(tmp_path):
    cache_fixture(tmp_path)
    settings = Settings(_env_file=None, analysis_provider="synthetic", osm_cache_path=tmp_path)
    with TestClient(create_app(settings)) as client:
        created = client.post("/api/analyses", json=body(mode="hybrid")).json()
        state = wait_for_terminal(client, created["taskId"])
        assert state["status"] == "completed"
        payload = client.get(f"/api/analyses/{created['taskId']}/result").json()
        assert payload["dataSource"] == "hybrid"
        assert payload["hybridResult"]["comparison"]["geometryMerged"] is False
        assert payload["hybridResult"]["baidu"]["participated"] is True
        assert payload["hybridResult"]["osm"]["participated"] is True


def test_osm_origin_outside_coverage_is_rejected(tmp_path):
    cache_fixture(tmp_path)
    settings = Settings(_env_file=None, analysis_provider="synthetic", osm_cache_path=tmp_path)
    with TestClient(create_app(settings)) as client:
        response = client.post("/api/analyses", json=body(mode="osm_offline", center={"lng": 117, "lat": 40}))
        assert response.status_code == 409
