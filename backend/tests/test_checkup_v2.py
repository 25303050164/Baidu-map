"""M1: dual engines, v2 tasks, immutable revisions, legacy compatibility.

Every case runs offline: the E8.2 adapter is driven by the synthetic endpoint
provider and the Hybrid adapter by a local evidence provider, so no Baidu
request is ever issued by this file.
"""
import asyncio
import math
import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.algorithms.baidu_e82 import EndpointAnalyticProvider
from app.algorithms.hybrid_isochrone.extent import ALGORITHM_VERSION
from app.algorithms.hybrid_isochrone.models import Evidence, Validity
from app.checkups.store import CheckupStore
from app.config import Settings
from app.main import create_app

ORIGIN = (121.513925, 31.313079)
CENTER = {"lng": ORIGIN[0], "lat": ORIGIN[1]}


class FastGate:
    """No transport pacing. Account-level QPS policy is tested in test_rate_gate."""

    interval = 0
    qps = 10000

    def __init__(self):
        self.attempt_lock = asyncio.Lock()

    async def wait(self, deadline, *, cost=1):
        return True

    def completed(self, reason, *, cost=1):
        pass


class OfflineHybrid:
    """Local walking-time evidence; never a network provider."""

    network = False
    identity = ("offline-checkup-hybrid",)

    def __init__(self, projection):
        self.projection = projection

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def query_walking_time(self, origin, destination, deadline):
        a, b = self.projection.origin(origin), self.projection.origin(destination)
        duration = math.dist(a, b) / 1.2
        return Evidence(validity=Validity.REACHABLE if duration <= 900 else Validity.UNREACHABLE,
                        reason=None, duration=duration, returned_origin=origin,
                        returned_destination=destination, origin_offset_m=0,
                        destination_offset_m=0)


def analytic(origin):
    return EndpointAnalyticProvider(origin, lambda x, y: math.hypot(x, y) / 1.2)


def make_app(tmp_path, **overrides):
    settings = Settings(
        _env_file=None, baidu_map_ak="", analysis_provider="synthetic",
        checkup_dir=tmp_path / "checkups", hybrid_ledger_dir=tmp_path / "ledgers",
        quota_ledger_path=tmp_path / "quota.sqlite3",
        hybrid_obstacle_path=Path("missing-checkup-obstacles"),
        hybrid_risk_path=Path("missing-checkup-risks"), **overrides)
    app = create_app(settings, provider_factory=analytic,
                     hybrid_provider_factory=lambda projection, config: OfflineHybrid(projection))
    for engine in app.state.checkups.registry.engines.values():
        engine.gate = FastGate()
    return app


def body(**overrides):
    payload = {"schemaVersion": "checkup-v1", "clientRequestId": "checkup-1",
               "engine": "baidu_e82", "center": dict(CENTER), "coordinateSystem": "bd09ll",
               "isochrone": {"budget": 200}}
    payload.update(overrides)
    return payload


def wait_for(client, task_id, timeout=120.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        view = client.get(f"/api/v2/checkups/{task_id}").json()
        if view["status"] in ("completed", "failed", "cancelled"):
            return view
        time.sleep(0.05)
    raise AssertionError(f"task {task_id} did not finish: {view}")


# -- task contract ---------------------------------------------------------

def test_isochrone_stage_publishes_a_revision_without_a_second_facility_search(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("app.analyses.analyze_facilities",
                        lambda *a, **k: calls.append(a) or _unreachable())
    app = make_app(tmp_path)
    with TestClient(app) as client:
        created = client.post("/api/v2/checkups", json=body())
        assert created.status_code == 202
        task_id = created.json()["taskId"]
        view = wait_for(client, task_id)
        assert view["status"] == "completed" and view["stage"] == "isochrone"
        # Facilities are a later stage; nothing may issue their search now.
        assert calls == []
        assert view["requests"] == view["networkRequests"] == 0  # synthetic transport

        result = client.get(f"/api/v2/checkups/{task_id}/result")
        assert result.status_code == 200
        document = result.json()
        assert document["schemaVersion"] == "checkup-v1"
        assert document["revision"] == 1 and document["stage"] == "isochrone"
        assert document["engine"]["engineId"] == "baidu_e82"
        assert document["facilitiesStatus"] == "not_integrated"
        # A later stage is outstanding, so completeness is never claimed.
        assert document["businessStatus"] == "partial"
        assert any(w["code"] == "STAGES_NOT_INTEGRATED" for w in document["warnings"])
        # The boundary digest and the full result digest are different objects.
        trace = document["trace"]
        assert trace["isochroneHash"] != trace["resultHash"]
        assert document["isochrone"]["isochroneHash"] == trace["isochroneHash"]
        # The rule body is shared verbatim with the legacy contracts, so it
        # keeps that object's field names rather than being described twice.
        assert document["rules"]["threshold_m"] == 1000
        assert document["rules"]["tolerance_m"] == 100
        assert document["rules"]["metric"] == "walking_route"
        assert trace["budgets"] == {"isochrone": 200}


def test_identical_request_returns_the_same_task_and_runs_once(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        first = client.post("/api/v2/checkups", json=body())
        task_id = first.json()["taskId"]
        wait_for(client, task_id)
        published = client.get(f"/api/v2/checkups/{task_id}/result").json()["trace"]["resultHash"]

        repeat = client.post("/api/v2/checkups", json=body())
        assert repeat.status_code == 202 and repeat.json()["taskId"] == task_id
        assert repeat.json()["revision"] == 1
        assert client.get(f"/api/v2/checkups/by-request/checkup-1").json()["taskId"] == task_id
        assert client.get(f"/api/v2/checkups/{task_id}/result").json()["trace"]["resultHash"] == published
        assert len(app.state.checkups.store.revisions(task_id)) == 1


def test_same_request_id_with_different_parameters_is_a_conflict(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        client.post("/api/v2/checkups", json=body())
        changed = client.post("/api/v2/checkups", json=body(isochrone={"budget": 400}))
        assert changed.status_code == 409
        assert changed.json()["code"] == "checkup_request_id_conflict"


def test_unknown_engine_or_budget_tier_never_falls_back_to_the_other_algorithm(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        unknown = client.post("/api/v2/checkups", json=body(engine="osm_and_baidu_auto"))
        assert unknown.status_code == 404
        assert unknown.json()["code"] == "checkup_unknown_engine"
        unsupported = client.post("/api/v2/checkups", json=body(isochrone={"budget": 250}))
        assert unsupported.status_code == 422
        assert unsupported.json()["code"] == "checkup_unsupported_budget"
        # Hybrid accepts 200/400 only; the E8.2 tiers are not shared with it.
        assert client.post("/api/v2/checkups",
                           json=body(engine="osm_hybrid", isochrone={"budget": 800})
                           ).status_code == 422
        assert app.state.checkups.store.next_queued() is None


def test_invalid_payload_is_refused_with_the_checkup_envelope(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        for payload in (body(center={"lng": "SECRET-AK", "lat": 31.3}),
                        body(coordinateSystem="wgs84"), body(clientRequestId=""),
                        body(facilities={"maxPoiRequests": 6000}),
                        {**body(), "gridSize": 8}):
            response = client.post("/api/v2/checkups", json=payload)
            assert response.status_code == 422
            assert response.json()["code"] == "checkup_invalid_request"
            assert "SECRET" not in response.text
        assert client.get("/api/v2/checkups/missing/result").json()["code"] == "checkup_task_not_found"
        assert client.get("/api/v2/checkups/missing/nope").json()["code"] == "checkup_http_error"


def test_result_is_not_ready_before_the_first_revision(tmp_path):
    # A task whose creation response was lost, or that a restart interrupted,
    # must report "not ready" rather than an empty or partial document.
    store = CheckupStore(tmp_path / "checkups")
    store.initialize()
    store.create(task_id="orphan", client_request_id="lost-response", engine="baidu_e82",
                 fingerprint="f", payload=body(), budget=200)
    app = make_app(tmp_path)
    with TestClient(app) as client:
        assert client.get("/api/v2/checkups/orphan").json()["status"] == "failed"
        assert client.get("/api/v2/checkups/orphan/result").json()["code"] == "checkup_result_not_ready"
        assert client.post("/api/v2/checkups/orphan/routes/some-facility").json()["code"] == \
            "checkup_facilities_not_ready"


# -- cancellation ----------------------------------------------------------

def test_cancel_keeps_the_revisions_already_published(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        task_id = client.post("/api/v2/checkups", json=body()).json()["taskId"]
        wait_for(client, task_id)
        stored = app.state.checkups.store.revisions(task_id)
        cancelled = client.post(f"/api/v2/checkups/{task_id}/cancel")
        assert cancelled.status_code == 202
        # A finished task is terminal; cancelling it changes nothing.
        assert cancelled.json()["status"] == "completed"
        assert app.state.checkups.store.revisions(task_id) == stored
        assert client.get(f"/api/v2/checkups/{task_id}/result").json()["revision"] == 1


# -- revisions -------------------------------------------------------------

def test_a_published_revision_is_immutable_and_addressable(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        task_id = client.post("/api/v2/checkups", json=body()).json()["taskId"]
        wait_for(client, task_id)
        revision = app.state.checkups.store.revisions(task_id)[0]
        path = app.state.checkups.store.root / revision["payload"]
        before = path.read_bytes()

        first = client.get(f"/api/v2/checkups/{task_id}/layers/isochrone")
        assert first.status_code == 200
        etag = first.headers["etag"]
        assert first.json()["resultHash"] == revision["result_hash"]
        assert first.json()["revision"] == 1
        assert first.json()["geometry"] is not None

        assert path.read_bytes() == before
        # The validator identifies this exact revision.
        revalidated = client.get(f"/api/v2/checkups/{task_id}/layers/isochrone?revision=1",
                                 headers={"If-None-Match": etag})
        assert revalidated.status_code == 304
        assert client.get(f"/api/v2/checkups/{task_id}/layers/isochrone?revision=9",
                          headers={"If-None-Match": etag}).status_code == 409
        assert client.get(f"/api/v2/checkups/{task_id}/layers/heatmap").json()["code"] == \
            "checkup_layer_not_found"


def test_restart_marks_unfinished_tasks_interrupted_and_keeps_revisions(tmp_path):
    root = tmp_path / "checkups"
    store = CheckupStore(root)
    store.initialize()
    store.create(task_id="task-a", client_request_id="req-a", engine="baidu_e82",
                 fingerprint="f", payload={}, budget=200)
    assert store.claim("task-a")
    store.publish("task-a", stage="isochrone", snapshot={"stage": "isochrone"}, result_hash="h")

    restarted = CheckupStore(root)
    assert restarted.initialize() == 1
    record = restarted.get("task-a")
    assert record.status == "failed" and record.error == "interrupted_by_restart"
    # The boundary already paid for survives the restart; it is not re-requested.
    assert restarted.revision("task-a")["result_hash"] == "h"
    assert restarted.initialize() == 0


# -- the other engine ------------------------------------------------------

def test_hybrid_engine_publishes_its_own_boundary_under_its_own_identity(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        created = client.post("/api/v2/checkups",
                              json=body(engine="osm_hybrid", isochrone={"budget": 200}))
        assert created.status_code == 202
        task_id = created.json()["taskId"]
        view = wait_for(client, task_id)
        assert view["status"] == "completed", view
        document = client.get(f"/api/v2/checkups/{task_id}/result").json()
        assert document["engine"]["engineId"] == "osm_hybrid"
        assert document["engine"]["engineVersion"] == ALGORITHM_VERSION
        assert document["isochrone"]["algorithm"] == "hybrid"
        # No graph, risk or obstacle layer is configured here, so the Hybrid
        # result reports itself degraded instead of claiming a full run.
        assert document["isochrone"]["quality"] == "partial"
        assert any("hybrid_graph_available_false" == w for w in document["isochrone"]["warnings"])
        assert document["businessStatus"] == "partial"


def test_capabilities_report_both_engines_and_the_fixed_distance_rule(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        document = client.get("/api/v2/capabilities").json()
        engines = {engine["engineId"]: engine for engine in document["engines"]}
        assert set(engines) == {"baidu_e82", "osm_hybrid"}
        assert engines["baidu_e82"]["budgets"] == [200, 400, 800]
        assert engines["osm_hybrid"]["budgets"] == [200, 400]
        for engine in engines.values():
            assert engine["statusThresholdSeconds"] == 900
        rules = document["rules"]
        assert rules["ruleVersion"] == "walk-distance-1000-v1"
        assert rules["distance"]["threshold_m"] == 1000
        assert rules["distance"]["tolerance_m"] == 100
        # The uncertainty band is never presented as a radius enlargement.
        assert rules["bandIsNotRadiusExpansion"] is True
        assert document["budgets"]["poiRequests"] == 60
        assert document["budgets"]["routeRequests"] == 120
        assert document["coverage"]["queryPaddingM"] == 1300


def test_capabilities_report_the_application_budget_never_the_account_balance(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        quota = client.get("/api/v2/capabilities").json()["quota"]
        assert quota["tier"] == "current" and quota["matrixEnabled"] is False
        # The two services are separate pools, and only place has a day budget.
        assert quota["services"]["direction"] == {
            "qps": 16, "maxInflight": 1, "dailyBudget": None,
            "spentToday": None, "remainingToday": None}
        assert quota["services"]["place"] == {
            "qps": 8, "maxInflight": 1, "dailyBudget": 1600,
            "spentToday": 0, "remainingToday": 1600}
        # Nothing may present this as the account's own remaining allowance.
        assert quota["claimsAccountBalance"] is False
        assert "本应用预算余额" in quota["label"]
        assert app.state.quota is app.state.checkups.quota


def test_v2_contract_is_generated_without_touching_the_legacy_ones(tmp_path):
    """§10: a new contract must be checked alongside the old OpenAPI surface."""
    from tools.export_contract import typescript
    from app.contracts import AnalysisResponse, TaskResultResponse, TaskStatusResponse, OsmOfflineRequest
    from app.hybrid_contracts import HybridError, HybridRequest, HybridResultResponse

    app = make_app(tmp_path)
    with TestClient(app) as client:
        spec = client.get("/openapi.json").json()
    paths = spec["paths"]
    assert {"/api/v2/checkups", "/api/v2/checkups/by-request/{client_request_id}",
            "/api/v2/checkups/{task_id}", "/api/v2/checkups/{task_id}/result",
            "/api/v2/checkups/{task_id}/layers/{layer_id}", "/api/v2/checkups/{task_id}/cancel",
            "/api/v2/checkups/{task_id}/routes/{facility_id}",
            "/api/v2/capabilities"} <= set(paths)
    # Every legacy surface is still declared next to the versioned one.
    assert {"/api/analyses", "/api/analyses/by-request/{client_request_id}/cancel",
            "/api/analyses/{task_id}", "/api/analyses/{task_id}/cancel",
            "/api/analyses/{task_id}/result", "/api/analyses/{task_id}/routes/{facility_id}",
            "/api/v1/analysis/hybrid", "/api/v1/analysis/hybrid/by-request/{client_request_id}",
            "/api/v1/analysis/hybrid/{task_id}", "/api/v1/analysis/hybrid/{task_id}/result",
            "/api/v1/analysis/hybrid/{task_id}/cancel", "/api/v1/analysis/osm_offline",
            "/api/v1/analysis/synthetic", "/api/v1/analysis/mock/{scenario}"} <= set(paths)
    schemas = spec["components"]["schemas"]
    assert {"clientRequestId", "coordinateSystem", "schemaVersion"} <= set(
        schemas["CheckupRequest"]["properties"])
    # v2 adds no model to the generated demo contract, so the strict POI
    # evidence types keep the serialization test_poi_evidence locks down.
    legacy = typescript(
        [AnalysisResponse, TaskStatusResponse, TaskResultResponse, OsmOfflineRequest,
         HybridRequest, HybridResultResponse, HybridError],
        request_models=[OsmOfflineRequest, HybridRequest])
    assert legacy == (Path(__file__).resolve().parents[2]
                      / "life-circle-demo/src/api-contract.ts").read_text(encoding="utf-8")
    assert "Checkup" not in legacy and "checkup" not in legacy


def _unreachable():
    raise AssertionError("a checkup must not run the legacy facility stage")
