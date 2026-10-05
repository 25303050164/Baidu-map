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


class MetredAnalyticProvider(EndpointAnalyticProvider):
    """A provider that answer from local geometry but bills like a networked one.

    The legacy scheduler charges one network attempt per call when the provider
    says ``network``. This one answers from the same analytic geometry, so the
    counters move without a socket -- which is what makes "attempts stay
    counted" testable offline.
    """
    network = True


def make_app(tmp_path, provider_factory=None, **overrides):
    settings = Settings(
        _env_file=None, baidu_map_ak="", analysis_provider="synthetic",
        checkup_dir=tmp_path / "checkups", hybrid_ledger_dir=tmp_path / "ledgers",
        quota_ledger_path=tmp_path / "quota.sqlite3",
        hybrid_obstacle_path=Path("missing-checkup-obstacles"),
        hybrid_risk_path=Path("missing-checkup-risks"), **overrides)
    app = create_app(settings, provider_factory=provider_factory or analytic,
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
        # One revision per stage, and the task is only ready once the last one is
        # published: isochrone, facilities, accessibility, verification, report.
        assert view["status"] == "completed" and view["stage"] == "ready"
        assert view["revision"] == 5
        # Facilities are this checkup's own stage; nothing may issue the legacy
        # search for them, and this deployment has no place key to send one.
        assert calls == []
        # 合成 provider 不出进程，两列因此都是 0：它们记的是花掉的付费尝试，而这一步一次
        # 也没发出去。成圈自己算过的边界采样数留在 snapshot.statistics 里，不在这里冒充网络用量。
        assert view["requests"] == view["networkRequests"] == 0

        result = client.get(f"/api/v2/checkups/{task_id}/result")
        assert result.status_code == 200
        document = result.json()
        assert document["schemaVersion"] == "checkup-v1"
        assert document["revision"] == 5 and document["stage"] == "reporting"
        assert document["engine"]["engineId"] == "baidu_e82"
        # The facility stage could not run without a key, and says so instead of
        # returning an empty facility group that would read as "none exist".
        assert document["facilities"] is None
        assert document["facilitiesStatus"] == "failed"
        assert "FACILITIES_UNAVAILABLE" in {item["code"] for item in document["warnings"]}
        # Whatever is outstanding, completeness is never claimed.
        assert document["businessStatus"] == "insufficient"
        assert any(w["code"] == "STAGES_NOT_INTEGRATED" for w in document["warnings"])
        # The assessment runs even when the facility stage failed, so the revision
        # answers "why is there no coverage" instead of publishing a group-shaped
        # hole. The two blockers are named where each belongs: this deployment has
        # no place key (FACILITIES_UNAVAILABLE above) and no walking graph either,
        # and the graph is the assessment's own first precondition.
        assert document["accessibilityStatus"] == "failed"
        assert document["accessibility"]["status"] == "failed"
        assert {item["unavailableReason"] for item in document["accessibility"]["categories"]} == \
            {"osm_graph_unavailable"}
        assert document["serviceGaps"] is None and document["scores"] is None
        assert document["heatmap"] is None
        # The verification stage ran and refused by name: this deployment has no
        # route key, so its revision carries the refusal rather than a count of
        # zero that would read as "checked everything, found nothing".
        assert document["verification"]["status"] == "not_integrated"
        assert document["verification"]["reason"] == "missing_ak"
        assert document["verification"]["checked"] == 0
        # The report exists and states what it could not establish; a report that
        # is absent and a report that is empty must not look alike.
        assert document["report"]["reportId"] == f"{task_id}:5"
        assert document["report"]["verification"]["available"] is False
        assert document["report"]["verification"]["status"] == "not_integrated"
        assert document["scope"]["coverageSupported"] is True
        assert document["scope"]["modelSupportAvailable"] is False
        # The boundary digest and the full result digest are different objects.
        trace = document["trace"]
        assert trace["isochroneHash"] != trace["resultHash"]
        assert document["isochrone"]["isochroneHash"] == trace["isochroneHash"]
        # The report summarises the revision before it — the verification one —
        # so it pins that revision's digest. Its own digest would make the
        # document describe itself, and no reader could recompute it.
        assert document["report"]["evidence"]["sourceResultHash"] == \
            app.state.checkups.store.revisions(task_id)[3]["result_hash"]
        # The rule body is shared verbatim with the legacy contracts, so it
        # keeps that object's field names rather than being described twice.
        assert document["rules"]["threshold_m"] == 1000
        assert document["rules"]["tolerance_m"] == 100
        assert document["rules"]["metric"] == "walking_route"
        # Every pool of the task is on the record, spent or not. The synthetic
        # transport issues no request, so the boundary reports none.
        assert trace["budgets"] == {"isochrone": {"limit": 200, "spent": 0},
                                    "poi": {"limit": 60, "spent": 0},
                                    "route": {"limit": 120, "spent": 0},
                                    "detail": {"limit": 20, "spent": 0}}

        # The first revision is the boundary alone, published before the stage
        # that follows it and still addressable on its own.
        earlier = app.state.checkups.store.revisions(task_id)[0]
        assert earlier["stage"] == "isochrone"
        boundary = client.get(f"/api/v2/checkups/{task_id}/layers/isochrone?revision=1")
        assert boundary.status_code == 200
        assert boundary.json()["resultHash"] == earlier["result_hash"]
        assert boundary.json()["resultHash"] != trace["resultHash"]


def test_the_boundary_attempts_stay_counted_when_the_next_stage_publishes(tmp_path):
    """一次真实体检暴露过这里。

    成圈快照的两个计数曾经在组装时被吃掉，于是 400 次真实百度请求在任务上记成 0，
    ``trace.budgets.isochrone.spent`` 也是 0；更糟的是设施阶段发布时会重写这两个计数器，
    界面上的数字因此从 226 掉回 28 —— 跑得越久，显示的用量越少。计数只增不减。
    """
    app = make_app(tmp_path, provider_factory=lambda origin: MetredAnalyticProvider(
        origin, lambda x, y: math.hypot(x, y) / 1.2), analysis_qps=16)
    with TestClient(app) as client:
        created = client.post("/api/v2/checkups", json=body())
        assert created.status_code == 202
        task_id = created.json()["taskId"]
        view = wait_for(client, task_id)
        assert view["status"] == "completed", view
        document = client.get(f"/api/v2/checkups/{task_id}/result").json()
        # 成圈这一次确实发过请求（这个 provider 按网络计费），所以计数必须是非零的。
        boundary = document["isochrone"]
        spent = boundary["networkRequests"]
        assert spent > 0
        # 两列说的是同一件事：花掉的付费尝试。设施阶段这一步没有密钥，加不出任何东西，
        # 所以任务上的数必须还是成圈自己数出来的那个，而不是被重写成 0。
        assert view["requests"] == view["networkRequests"] == spent
        assert document["trace"]["budgets"]["isochrone"] == {"limit": 200, "spent": spent}
        # 成圈自己数的边界采样数留在快照里，没有被挪走，也没有被当成网络用量。
        assert boundary["requestsUsed"] == boundary["statistics"]["requests"] == spent


def test_identical_request_returns_the_same_task_and_runs_once(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        first = client.post("/api/v2/checkups", json=body())
        task_id = first.json()["taskId"]
        wait_for(client, task_id)
        published = client.get(f"/api/v2/checkups/{task_id}/result").json()["trace"]["resultHash"]

        repeat = client.post("/api/v2/checkups", json=body())
        assert repeat.status_code == 202 and repeat.json()["taskId"] == task_id
        assert repeat.json()["revision"] == 5
        assert client.get(f"/api/v2/checkups/by-request/checkup-1").json()["taskId"] == task_id
        assert client.get(f"/api/v2/checkups/{task_id}/result").json()["trace"]["resultHash"] == published
        # One run, one revision per stage, and never a second run.
        assert [item["stage"] for item in app.state.checkups.store.revisions(task_id)] == \
            ["isochrone", "poi", "accessibility", "verification", "reporting"]


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
        # A route detail names a facility of a published revision, so a task
        # with no revision at all is refused for that reason and not another.
        assert client.post("/api/v2/checkups/orphan/routes/some-facility").json()["code"] == \
            "checkup_result_not_ready"


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
        assert client.get(f"/api/v2/checkups/{task_id}/result").json()["revision"] == 5


# -- revisions -------------------------------------------------------------

def test_a_published_revision_is_immutable_and_addressable(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        task_id = client.post("/api/v2/checkups", json=body()).json()["taskId"]
        wait_for(client, task_id)
        revisions = app.state.checkups.store.revisions(task_id)
        revision = revisions[-1]
        assert revisions[0]["revision"] == 1 and revision["revision"] == 5
        path = app.state.checkups.store.root / revision["payload"]
        before = path.read_bytes()

        first = client.get(f"/api/v2/checkups/{task_id}/layers/isochrone")
        assert first.status_code == 200
        etag = first.headers["etag"]
        assert first.json()["resultHash"] == revision["result_hash"]
        assert first.json()["revision"] == 5
        assert first.json()["geometry"] is not None

        assert path.read_bytes() == before
        # The validator identifies this exact revision.
        revalidated = client.get(f"/api/v2/checkups/{task_id}/layers/isochrone?revision=5",
                                 headers={"If-None-Match": etag})
        assert revalidated.status_code == 304
        assert client.get(f"/api/v2/checkups/{task_id}/layers/isochrone?revision=9",
                          headers={"If-None-Match": etag}).status_code == 409
        # A layer this release does not serve, a layer whose group this revision
        # does not contain and a layer whose group is there but holds no geometry
        # are three different answers. None of them is an empty shape: an empty
        # collection drawn on a map reads as "nothing here", which is the claim
        # the assessment refused to make.
        assert client.get(f"/api/v2/checkups/{task_id}/layers/sunshine").json()["code"] == \
            "checkup_layer_not_found"
        refused = {layer_id: client.get(f"/api/v2/checkups/{task_id}/layers/{layer_id}").json()
                   for layer_id in ("accessibility", "service_gaps", "heatmap", "verification")}
        assert {item["code"] for item in refused.values()} == {
            "checkup_domain_unavailable", "checkup_service_gaps_not_ready",
            "checkup_heatmap_not_ready", "checkup_verification_not_integrated"}
        # The report is a document rather than a shape: it is served from the same
        # endpoint, and it fills the field that says so.
        report = client.get(f"/api/v2/checkups/{task_id}/layers/report").json()
        assert report["geometry"] is None and report["displayGeometry"] is None
        assert report["document"]["reportId"] == f"{task_id}:5"


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


def test_building_the_app_does_not_interrupt_a_running_task(tmp_path):
    """构造 app 对象不是一次部署事件。

    `tools/export_contract.py` 为了导出 OpenAPI 会 `from app.main import app`。清点要是
    发生在构造里，导出一次契约就会把别人正在跑的体检判成中断 —— 那是已经付过费的请求，
    也永远不会再有结果。所以构造只建表，清点留给"开始服务"这一刻。
    """
    store = CheckupStore(tmp_path / "checkups")
    store.create_schema()
    store.create(task_id="live", client_request_id="live", engine="baidu_e82",
                 fingerprint="f", payload=body(), budget=200)
    assert store.claim("live")
    make_app(tmp_path)
    assert store.get("live").status == "running"
    # 而真的开始服务时它就是重启：同一个任务这时才被判成中断。
    with TestClient(make_app(tmp_path)):
        pass
    assert store.get("live").status == "failed"
    assert store.get("live").error == "interrupted_by_restart"


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
        # This deployment has no place key either, so the facility stage of this
        # Hybrid task failed the same way: the run is not called complete on the
        # strength of a boundary alone.
        assert document["facilities"] is None
        assert document["facilitiesStatus"] == "failed"
        assert document["businessStatus"] == "insufficient"


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
        assert document["coverage"]["graphState"] == "unavailable"


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
    assert {"/api/analyses", "/api/analyses/by-request/{client_request_id}",
            "/api/analyses/by-request/{client_request_id}/cancel",
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
