"""M2: the facility stage over the computed boundary, end to end and offline.

Both engines run their own boundary first and then exactly one facility
retrieval over it. The transport here answers from a Python callable instead of
HTTP, but it reserves every attempt through the same pool the production session
uses, so pacing, the task budget and the daily ledger are exercised for real and
no request in this file leaves the machine.

What is under test is §4.4's and §3.4's semantics: the counting region is the
boundary and not the search range; a run that retrieved nothing is not a
directory with nothing in it; a refusal is named rather than numbered; a page
served from the cache costs no attempt and keeps its original data time; a
cancelled stage keeps what it retrieved; and a failure is never a zero.
"""
import asyncio
import contextlib
import json
import math
import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.algorithms.baidu_e82 import EndpointAnalyticProvider
from app.algorithms.hybrid_isochrone.models import Evidence, Validity
from app.checkups.places import POI_POOL
from app.checkups.routes import ROUTE_POOL
from app.config import Settings
from app.main import create_app
from app.poi.planner import RULES
from app.quota import BudgetExhausted, DeadlineReached
from life_circle.coordinates import LocalProjection, normalize
from life_circle.models import RouteObservation

ORIGIN = (121.513925, 31.313079)
CENTER = {"lng": ORIGIN[0], "lat": ORIGIN[1]}
SECRET = "checkup-place-secret"
#: The route transport's identity, as the verification and detail evidence report it.
SYNTHETIC_ROUTES = "synthetic:checkup-routes"


class FastGate:
    """No transport pacing: quota pacing is tested in test_rate_gate."""

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


# -- the synthetic transport -------------------------------------------------

def supported_tag(category):
    """One tag the dictionary classifies into this category, taken from it."""
    return sorted(RULES["supportedTags"][category])[0]


def offset(meters, *, east=1.0, north=0.0):
    """A geographic point that many metres from the request centre."""
    return LocalProjection(ORIGIN).to_geographic((meters * east, meters * north))


class SyntheticPlaces:
    """A ``PlaceTransport`` answered by a Python callable instead of HTTP.

    Every page it sends holds the pool's single in-flight slot behind a reserved
    attempt, exactly as the production session does, and ``sent`` is the one
    record of what a run really asked for.
    """

    identity, api_version, network = "synthetic:checkup-tests", "3.0", False

    def __init__(self, respond, *, delay=0.0):
        self.respond, self.delay, self.sent = respond, delay, []

    def session(self, pool, *, budget, deadline):
        return SyntheticPlaceSession(self, pool, budget=budget, deadline=deadline)


class SyntheticPlaceSession:
    def __init__(self, transport, pool, *, budget, deadline):
        self.transport, self.pool = transport, pool
        self.budget, self.deadline = budget, deadline

    async def __call__(self, sequence, page):
        async with self.pool.attempt(self.deadline, budget=self.budget, pool=POI_POOL) as attempt:
            if self.transport.delay:
                await asyncio.sleep(self.transport.delay)
            self.transport.sent.append((sequence["sequenceId"], page))
            payload, reason = self.transport.respond(sequence, page)
            attempt.outcome(reason)
        return payload, reason


class SyntheticRoutes:
    """A walking-route transport answered by a Python callable instead of HTTP.

    Same shape as ``SyntheticPlaces``: one reservation per attempt through the
    task's ``route`` bucket, so the budget, the pool and the pacing are the real
    ones and no request in this file leaves the machine.
    """

    identity, api_version, network = "synthetic:checkup-routes", "directionlite/v1/walking", False

    def __init__(self, respond):
        self.respond, self.sent = respond, []

    def session(self, pool, *, budget, deadline):
        return SyntheticRouteSession(self, pool, budget=budget, deadline=deadline)


class SyntheticRouteSession:
    def __init__(self, transport, pool, *, budget, deadline):
        self.transport, self.pool = transport, pool
        self.budget, self.deadline = budget, deadline
        self.attempts, self.stop_reason = 0, None

    @property
    def identity(self):
        return self.transport.identity

    async def __call__(self, facility_id, origin, destination, *, pool=ROUTE_POOL):
        try:
            async with self.pool.attempt(self.deadline, budget=self.budget, pool=pool) as attempt:
                self.attempts += 1
                self.transport.sent.append((facility_id, origin, destination))
                observation = self.transport.respond(origin, destination, facility_id)
                attempt.outcome(observation.reason)
        except BudgetExhausted:
            # 桶用尽和到点都不是"这条路走不通"，而是"这次没有结论"。生产会话在这里
            # 返回 None，替身也必须一样，否则测试会因为一个生产代码接住的异常而变红。
            self.stop_reason = 'task_budget_exhausted'
            return None
        except DeadlineReached:
            self.stop_reason = 'deadline'
            return None
        return observation


def straight_routes(*, detour=1.15, speed=1.2):
    """A responder whose route is the straight line, times a detour factor.

    The returned endpoints are the requested ones, so the strict POI mapping is
    the one under test here: this is the "the endpoints really did line up" case.
    """
    def respond(route_origin, destination, facility_id):
        # 上游收到的是归一化之后的坐标，回来的端点就是它 —— 所以这里回显的也是归一化
        # 之后的目标点。回显原始浮点数会让严格映射判定成"端点有偏移"（归一化把坐标截到
        # 6 位小数，折合江南一带约 0.08 米，超过 1e-5 米的阈值），那是测试替身失真。
        destination = normalize(destination)
        projection = LocalProjection(route_origin)
        distance = math.dist(projection.to_local(route_origin),
                             projection.to_local(destination)) * detour
        duration = distance / speed
        return RouteObservation(destination, duration, reason=None,
                                observed_duration=duration, endpoint_verified=True,
                                route_origin=route_origin, route_destination=destination,
                                distance_m=distance, route_path=[route_origin, destination],
                                origin_offset_m=0, destination_offset_m=0)
    return respond


def offset_routes(*, offset_m=80.0, detour=1.15):
    """A responder whose route ends 80 m away from the requested facility.

    That is what a road-snapped endpoint looks like, and §6.3 is explicit about
    what must happen next: the route stays route-endpoint evidence, it is never
    promoted to strict POI verification.
    """
    def respond(route_origin, destination, facility_id):
        projection = LocalProjection(route_origin)
        # 端点确实停在离目标 80 米的地方，而不是停在一个碰巧不等的位置：这样"偏移量"这个
        # 数字本身也被测到，而不只是"不相等"这件事。
        x, y = projection.to_local(normalize(destination))
        end = projection.to_geographic((x + offset_m, y))
        distance = math.dist(projection.to_local(route_origin),
                             projection.to_local(destination)) * detour
        duration = distance / 1.2
        return RouteObservation(destination, duration, reason=None,
                                observed_duration=duration, endpoint_verified=True,
                                route_origin=route_origin, route_destination=end,
                                distance_m=distance, route_path=[route_origin, end],
                                origin_offset_m=0.0, destination_offset_m=offset_m)
    return respond


def row(sequence, index, *, at, uid=None):
    """One provider row: a facility of the query's own category.

    The name and address come from that category's own vocabulary, so a row
    classifies as the category its query asked for and two categories never
    collide on the duplicate rule.
    """
    word = supported_tag(sequence["category"])
    return {"uid": uid or f"{sequence['category']}-{index}", "name": f"{word}样本",
            "address": f"{word}路{index}号", "location": {"lng": at[0], "lat": at[1]},
            "detail_info": {"classified_poi_tag": word, "parent_id": None}}


def every_page(points):
    """A responder that answers every page with one row per point."""
    def respond(sequence, page):
        results = [row(sequence, index, at=point) for index, point in enumerate(points)]
        return {"status": 0, "total": len(results), "result_type": "poi_type",
                "results": results}, None
    return respond


def at_origin(meters=0.0):
    """A responder answering every page with facilities near the request centre."""
    return every_page([offset(meters) if meters else ORIGIN])


def failing(reason="upstream_error"):
    return lambda sequence, page: (None, reason)


# -- scaffolding -------------------------------------------------------------

def make_app(tmp_path, places=None, routes=None, **overrides):
    """An offline checkup app. ``places=None`` is a deployment without a key.

    ``routes`` is the verification stage's transport; the default answers every
    route with the straight line, so a task that retrieves facilities also
    verifies them without reaching the network. ``overrides`` wins over the
    scratch defaults below: a test that needs a real obstacle layer, a different
    data version or an extra proxy must not have to rebuild the whole fixture.
    """
    routes = SyntheticRoutes(straight_routes()) if routes is None else routes
    configured = dict(
        baidu_map_ak=SECRET if places is not None else "",
        analysis_provider="synthetic", baidu_place_qps=10000, baidu_direction_qps=10000,
        checkup_dir=tmp_path / "checkups", hybrid_ledger_dir=tmp_path / "ledgers",
        quota_ledger_path=tmp_path / "quota.sqlite3",
        hybrid_obstacle_path=Path("missing-checkup-obstacles"),
        hybrid_risk_path=Path("missing-checkup-risks"))
    configured.update(overrides)
    settings = Settings(_env_file=None, **configured)
    app = create_app(settings, provider_factory=analytic,
                     hybrid_provider_factory=lambda projection, config: OfflineHybrid(projection),
                     place_factory=None if places is None else (lambda _settings: places),
                     route_factory=lambda _settings: routes)
    for engine in app.state.checkups.registry.engines.values():
        engine.gate = FastGate()
    return app


def body(**overrides):
    payload = {"schemaVersion": "checkup-v1", "clientRequestId": "checkup-1",
               "engine": "baidu_e82", "center": dict(CENTER), "coordinateSystem": "bd09ll",
               "isochrone": {"budget": 200}}
    payload.update(overrides)
    return payload


def terminal(client, task_id, timeout=120.0, until_stage=None):
    """Wait for a task to reach a terminal state, or for one stage to start."""
    deadline = time.monotonic() + timeout
    view = client.get(f"/api/v2/checkups/{task_id}").json()
    while time.monotonic() < deadline:
        if until_stage is not None and view["stage"] == until_stage:
            return view
        if until_stage is None and view["status"] in ("completed", "failed", "cancelled"):
            return view
        time.sleep(0.02)
        view = client.get(f"/api/v2/checkups/{task_id}").json()
    raise AssertionError(f"task {task_id} did not finish: {view}")


def run(client, payload):
    """Submit a task and wait for it to reach a terminal state."""
    created = client.post("/api/v2/checkups", json=payload)
    assert created.status_code == 202
    task_id = created.json()["taskId"]
    return task_id, terminal(client, task_id)


def document(client, task_id):
    response = client.get(f"/api/v2/checkups/{task_id}/result")
    assert response.status_code == 200, response.text
    return response.json()


def stored(app, task_id, index):
    """A published revision, read from the file the store froze."""
    revision = app.state.checkups.store.revisions(task_id)[index]
    path = app.state.checkups.store.root / revision["payload"]
    return revision, json.loads(path.read_text(encoding="utf-8")), path


# -- the loop ----------------------------------------------------------------

def test_both_engines_close_the_loop_over_their_own_boundary(tmp_path):
    for engine in ("baidu_e82", "osm_hybrid"):
        places = SyntheticPlaces(at_origin())
        app = make_app(tmp_path / engine, places)
        with TestClient(app) as client:
            task_id, view = run(client, body(engine=engine))
            assert view["status"] == "completed", view
            assert view["stage"] == "ready" and view["revision"] == 5
            # The facility stage sent 24 pages and the verification stage then
            # asked for one route per candidate: both are this task's requests,
            # and the counters report the task's own attempts rather than the
            # facility stage's share of them.
            assert len(places.sent) == 24
            assert view["networkRequests"] == 24 + 3
            assert view["requests"] >= view["networkRequests"]
            # This deployment has no walking graph, so the assessment refused and
            # the checkup is not called complete on the strength of a boundary and
            # a retrieval: the question it exists for is still unanswered.
            assert view["businessStatus"] == "insufficient"

            boundary_revision, first, _ = stored(app, task_id, 0)
            revision, document_, path = stored(app, task_id, 1)
            assert boundary_revision["stage"] == "isochrone" and revision["stage"] == "poi"
            # Revision 2 repeats the boundary it counted over, unchanged: the two
            # documents differ in the facility group and the hashes, not here.
            assert document_["isochrone"] == first["isochrone"]
            assert document_["trace"]["isochroneHash"] == first["trace"]["isochroneHash"]
            assert document_["trace"]["resultHash"] != first["trace"]["resultHash"]
            assert document_["revision"] == 2 and document_["stage"] == "poi"
            # The published file is what the interface serves.
            assert document(client, task_id) == stored(app, task_id, 4)[1]

            group = document_["facilities"]
            assert group["queryStatus"] == "completed"
            # §4.4: a finished keyword search is still not a verified directory.
            assert group["catalogCompleteness"] == "unverified"
            assert group["dataSource"] == "synthetic" and group["provider"] == places.identity
            assert group["countsByCategory"] == {"shopping": 1, "medical": 1, "education": 1}
            assert len(group["facilities"]) == 3
            assert {item["classificationStatus"] for item in group["facilities"]} == {"accepted"}
            assert group["statistics"]["uidMergedRecords"] == 24 - 3
            assert group["statistics"]["acceptedRecords"] == 3
            assert group["statistics"]["invalidRecords"] == 0
            assert group["statistics"]["outsideBoundaryRecords"] == 0
            assert group["statistics"]["cachedPages"] == 0
            assert group["statistics"]["livePages"] == 24
            assert group["statistics"]["budgetSpent"] == 24
            assert group["statistics"]["pageAttempts"] == 24
            assert group["stopReason"] is None and group["warnings"] == []
            assert group["queryDomain"]["paddingMeters"] == 1300
            assert group["dataObtainedAt"] > 0

            assert document_["facilitiesStatus"] == "complete"
            # This is the revision that added the retrieved facilities, and a
            # finished keyword search is still not a verified directory, so
            # completeness is never claimed — not here and not in the revision
            # that follows it.
            assert document_["businessStatus"] == "partial"
            assert app.state.checkups.store.revisions(task_id)[4]["stage"] == "reporting"
            assert document_["trace"]["ruleVersions"]["classification"] == RULES["version"]
            assert document_["trace"]["budgets"]["poi"] == {"limit": 60, "spent": 24}
            assert document_["trace"]["budgets"]["route"] == {"limit": 120, "spent": 0}
            assert document_["trace"]["budgets"]["detail"] == {"limit": 20, "spent": 0}
            assert document_["trace"]["budgets"]["isochrone"]["limit"] == 200

            # The key is a credential: it may not appear anywhere in a revision.
            assert SECRET not in path.read_text(encoding="utf-8")


def test_the_boundary_decides_what_counts_and_the_search_range_only_reaches(tmp_path):
    places = SyntheticPlaces(every_page([ORIGIN, offset(2000), offset(5000)]))
    app = make_app(tmp_path, places)
    with TestClient(app) as client:
        task_id, view = run(client, body())
        assert view["status"] == "completed", view
        _revision, document_, _path = stored(app, task_id, 1)
        group = document_["facilities"]
        # Only the point inside the computed boundary is a finding: the two
        # outside it changed the counts not at all.
        assert group["countsByCategory"] == {"shopping": 1, "medical": 1, "education": 1}
        assert len(group["facilities"]) == 3
        assert group["excludedCandidates"] == []
        # Inside the search range but outside the boundary: kept where it is, as
        # evidence for the region rather than as a finding in it. Outside the
        # range entirely: kept too, and told apart by its reason.
        reasons = {}
        for item in group["quarantine"]:
            reasons[item["reason"]] = reasons.get(item["reason"], 0) + 1
        assert reasons == {"outside_counting_region": 3, "outside_query_domain": len(places.sent)}
        statistics = group["statistics"]
        assert statistics["outsideBoundaryRecords"] == 3
        assert statistics["outsideDomainRecords"] == len(places.sent)
        assert statistics["acceptedRecords"] == 3
        assert "outside_boundary_records" in group["warnings"]
        # The queries completed. A zero would have to be read with this status,
        # which is why the revision reports both axes.
        assert group["queryStatus"] == "completed"
        assert group["catalogCompleteness"] == "unverified"


def test_a_deployment_without_a_key_names_its_refusal_and_never_a_zero(tmp_path):
    app = make_app(tmp_path, None)
    with TestClient(app) as client:
        task_id, view = run(client, body())
        assert view["status"] == "completed", view
        document_ = document(client, task_id)
        # Null is the absence of a result; an empty group would be a result that
        # says no facility exists, which this run never established.
        assert document_["facilities"] is None
        assert document_["facilitiesStatus"] == "failed"
        assert document_["businessStatus"] == "insufficient"
        refusals = [item for item in document_["warnings"]
                    if item["code"] == "FACILITIES_UNAVAILABLE"]
        assert len(refusals) == 1 and refusals[0]["severity"] == "error"
        assert "AK" in refusals[0]["message"]
        assert document_["trace"]["budgets"]["poi"] == {"limit": 60, "spent": 0}


def test_a_facility_run_that_fails_every_page_is_a_failed_query(tmp_path):
    places = SyntheticPlaces(failing())
    app = make_app(tmp_path, places)
    with TestClient(app) as client:
        task_id, view = run(client, body())
        assert view["status"] == "completed", view
        document_ = document(client, task_id)
        group = document_["facilities"]
        # The stage ran and reported itself: an empty list here is a failed
        # retrieval, which the status and the page errors both say.
        assert group["queryStatus"] == "failed"
        assert group["facilities"] == [] and group["countsByCategory"] == {
            "shopping": 0, "medical": 0, "education": 0}
        assert "upstream_error" in group["warnings"]
        assert all(record["pageErrors"] for record in group["queryCoverage"])
        assert all(not page["succeeded"]
                   for record in group["queryCoverage"] for page in record["pageRecords"])
        # A retry is an attempt: every one of them reserved its slot, so the
        # revision counts the pages it asked for and the attempts they cost.
        statistics = group["statistics"]
        assert len(places.sent) == statistics["pageAttempts"] == statistics["budgetSpent"] == 48
        assert statistics["queryAttempts"] == 48
        assert statistics["livePages"] == 24  # 24 distinct pages, each asked twice
        assert view["networkRequests"] == 48 and view["requests"] == 48
        assert document_["facilitiesStatus"] == "failed"
        assert document_["businessStatus"] == "insufficient"


def test_a_cancelled_facility_stage_keeps_what_it_retrieved(tmp_path):
    places = SyntheticPlaces(at_origin(), delay=0.05)
    app = make_app(tmp_path, places)
    with TestClient(app) as client:
        created = client.post("/api/v2/checkups", json=body())
        task_id = created.json()["taskId"]
        assert terminal(client, task_id, until_stage="poi")["stage"] == "poi"
        client.post(f"/api/v2/checkups/{task_id}/cancel")
        view = terminal(client, task_id)
        assert view["status"] == "cancelled", view
        # Both stages are on the record: the boundary was complete before the
        # stop, and the facility stage published what it had.
        assert view["revision"] == 2
        assert len(app.state.checkups.store.revisions(task_id)) == 2
        document_ = document(client, task_id)
        assert document_["facilitiesStatus"] == "partial"
        assert document_["businessStatus"] == "partial"
        group = document_["facilities"]
        assert group["queryStatus"] == "cancelled" and group["stopReason"] == "cancelled"
        # Whatever it did retrieve is still reported, and the queries it never
        # sent are visible as such.
        assert 0 <= group["statistics"]["queryAttempts"] < 24
        assert len(places.sent) <= group["statistics"]["queryAttempts"]
        assert group["statistics"]["acceptedRecords"] == len(group["facilities"])
        assert all(item["classificationStatus"] == "accepted" for item in group["facilities"])
        # The boundary revision still says what it said before the stage began.
        first = stored(app, task_id, 0)[1]
        assert document_["isochrone"] == first["isochrone"]


def test_a_cached_page_costs_no_attempt_and_keeps_its_data_time(tmp_path):
    places = SyntheticPlaces(at_origin())
    app = make_app(tmp_path, places, cache_freshness_seconds=60)
    with TestClient(app) as client:
        first_id, _view = run(client, body(clientRequestId="checkup-a"))
        first = document(client, first_id)
        assert len(places.sent) == 24
        second_id, view = run(client, body(clientRequestId="checkup-b"))
        assert view["status"] == "completed", view
        second = document(client, second_id)
        # §3.4: a hit is served outside the pool, so it spends nothing. The
        # route verification is a separate matter — no route is cached, so the
        # second task still asked once per candidate, and the counter reports
        # exactly those.
        assert len(places.sent) == 24
        assert view["networkRequests"] == 3
        group = second["facilities"]
        assert group["statistics"]["cachedPages"] == 24
        assert group["statistics"]["livePages"] == 0
        assert group["statistics"]["budgetSpent"] == 0
        assert group["statistics"]["pageAttempts"] == 0
        # The planner still worked through all 24 queries; it just did not have
        # to ask for any of them again.
        assert group["statistics"]["queryAttempts"] == 24
        # The same evidence, and the original data time rather than a new one.
        assert group["facilities"] == first["facilities"]["facilities"]
        assert group["countsByCategory"] == first["facilities"]["countsByCategory"]
        assert group["dataObtainedAt"] == first["facilities"]["dataObtainedAt"]
        # The page record's own source is the data source of the run; where the
        # bytes came from is a different question with a different key.
        record = group["queryCoverage"][0]["pageRecords"][0]
        assert record["source"] == "synthetic" and record["fetchSource"] == "cache"
        live = first["facilities"]["queryCoverage"][0]["pageRecords"][0]
        assert live["fetchSource"] == "live"


# -- the layers and the route placeholder ------------------------------------

def test_the_facility_layer_draws_findings_and_stays_tied_to_its_revision(tmp_path):
    places = SyntheticPlaces(every_page([ORIGIN, offset(2000)]))
    app = make_app(tmp_path, places)
    with TestClient(app) as client:
        task_id, _view = run(client, body())
        document_ = document(client, task_id)
        layer = client.get(f"/api/v2/checkups/{task_id}/layers/facilities")
        boundary = client.get(f"/api/v2/checkups/{task_id}/layers/isochrone")
        assert layer.status_code == boundary.status_code == 200
        assert layer.json()["revision"] == 5
        assert layer.json()["resultHash"] == document_["trace"]["resultHash"]
        assert boundary.json()["resultHash"] == document_["trace"]["resultHash"]
        # Two layers of one revision are different representations: a validator
        # for one must not tell a client its copy of the other is current.
        assert layer.headers["etag"] != boundary.headers["etag"]
        assert layer.json()["displayGeometry"] is None

        collection = layer.json()["geometry"]
        assert collection["type"] == "FeatureCollection"
        assert collection["coordinateSystem"] == "bd09ll"
        assert len(collection["features"]) == 3  # the refused point is not drawn
        properties = collection["features"][0]["properties"]
        assert collection["features"][0]["geometry"]["type"] == "Point"
        assert set(properties) == {"id", "name", "category", "majorCategory", "address",
                                  "classificationStatus", "possibleDuplicateGroup"}
        assert properties["category"] == "market" and properties["majorCategory"] == "shopping"
        assert properties["id"].startswith("synthetic:") and properties["name"]

        # The boundary revision has no facilities to draw, and says so.
        assert client.get(f"/api/v2/checkups/{task_id}/layers/facilities?revision=1").json()[
            "code"] == "checkup_facilities_not_ready"
        # This deployment has no walking graph, so the assessment measured no
        # distance at all. The heat layer refuses by name instead of serving an
        # empty collection, which would read as "nothing is far from anything".
        assert client.get(f"/api/v2/checkups/{task_id}/layers/heatmap").json()["code"] == \
            "checkup_heatmap_not_ready"


def test_a_route_detail_is_only_offered_for_a_facility_of_this_checkup(tmp_path):
    places = SyntheticPlaces(at_origin())
    app = make_app(tmp_path, places)
    with TestClient(app) as client:
        task_id, _view = run(client, body())
        document_ = document(client, task_id)
        known = document_["facilities"]["facilities"][0]["id"]
        before = app.state.checkups.store.revisions(task_id)
        # 服务池是唯一的分派点（§9.2）：记下每次通过它的调用，看点击是不是真的走了它。
        pool = app.state.quota.direction
        seen, budget = [], app.state.checkups.detail_budget(task_id, app.state.checkups.get(task_id))
        original = pool.attempt

        @contextlib.asynccontextmanager
        async def counted(deadline, **kwargs):
            seen.append({"pool": kwargs.get("pool"), "budget": kwargs.get("budget")})
            async with original(deadline, **kwargs) as handle:
                yield handle

        pool.attempt = counted

        detail = client.post(f"/api/v2/checkups/{task_id}/routes/{known}")
        assert detail.status_code == 200, detail.text
        answer = detail.json()
        # 详情证据回答的是已经发布的那一版，而不是产生一个新版本：§3.3 要的是独立
        # 证据，重新评估才生成新修订。
        assert answer["revision"] == document_["revision"] == 5
        assert answer["facilityId"] == known and answer["category"] == "market"
        assert answer["majorCategory"] == "shopping"
        assert answer["provider"] == SYNTHETIC_ROUTES
        assert answer["network"] is False and answer["attempts"] == 1
        # 这一家就在请求中心上，所以一次零距离的路线是这条路线本身的结论，不是"没走"。
        assert answer["straightLineM"] == answer["routeDistanceM"] == 0.0
        assert answer["withinRule"] is True and answer["evidenceGrade"] == "verified"
        assert answer["poiStatus"] == "verified_reachable" and answer["poiReason"] is None
        assert answer["budget"] == {"pool": "detail", "limit": 20, "spent": 1, "remaining": 19}
        assert answer["notes"] and "不改变已发布的评分" in answer["notes"][0]

        # 点击走的是核验阶段同一条闸门：同一个 DIRECTION 服务池，并且从详情桶里扣。
        # 账户保护靠的是这个池子（配额、限速、单飞行位），所以测试要看到它被经过。
        assert [(item["pool"], item["budget"]) for item in seen] == [("detail", budget)]
        again = client.post(f"/api/v2/checkups/{task_id}/routes/{known}").json()
        assert again["budget"]["spent"] == 2 and again["budget"]["remaining"] == 18
        assert len(seen) == 2

        # 没有任何一版被改写：点击不发布、不重算、不覆盖已冻结的结论。
        assert app.state.checkups.store.revisions(task_id) == before
        assert document(client, task_id)["trace"]["resultHash"] == \
            document_["trace"]["resultHash"]

        other = client.post(f"/api/v2/checkups/{task_id}/routes/baidu_place:nothing")
        assert other.status_code == 404 and other.json()["code"] == "checkup_facility_not_found"
        missing = client.post("/api/v2/checkups/nope/routes/some-facility")
        assert missing.status_code == 404 and missing.json()["code"] == "checkup_task_not_found"


def test_the_detail_pool_is_shared_between_clicks_and_named_when_it_runs_out(tmp_path):
    places = SyntheticPlaces(at_origin())
    app = make_app(tmp_path, places)
    with TestClient(app) as client:
        task_id, _view = run(client, body())
        known = document(client, task_id)["facilities"]["facilities"][0]["id"]
        for spent in range(1, 21):
            answer = client.post(f"/api/v2/checkups/{task_id}/routes/{known}")
            assert answer.status_code == 200, answer.text
            assert answer.json()["budget"]["spent"] == spent
        # 21 次是具名拒绝，不是一个 200 带着空结果：本任务的详情额度用完了。
        refused = client.post(f"/api/v2/checkups/{task_id}/routes/{known}")
        assert refused.status_code == 429
        assert refused.json()["code"] == "checkup_detail_budget_exhausted"


# -- the verification layer --------------------------------------------------

def test_the_verification_layer_draws_what_was_asked_and_what_came_back(tmp_path):
    places = SyntheticPlaces(at_origin())
    app = make_app(tmp_path, places, routes=SyntheticRoutes(offset_routes()))
    with TestClient(app) as client:
        task_id, _view = run(client, body())
        layer = client.get(f"/api/v2/checkups/{task_id}/layers/verification")
        assert layer.status_code == 200, layer.text
        body_ = layer.json()
        assert body_["revision"] == 5 and body_["geometry"] is not None
        properties = body_["geometry"]["properties"]
        assert properties["status"] == "partial" and properties["checked"] == 3
        assert properties["provider"] == SYNTHETIC_ROUTES
        collection = body_["geometry"]
        assert collection["coordinateSystem"] == "bd09ll"
        assert len(collection["features"]) == 3
        feature = collection["features"][0]
        # 画的是设施自己的位置，不是被吸附到 80 米外的那条路线端点。
        assert feature["geometry"]["coordinates"] == [ORIGIN[0], ORIGIN[1]]
        assert feature["properties"]["destinationOffsetM"] == 80.0
        assert feature["properties"]["poiStatus"] == "pending"
        assert feature["properties"]["poiReason"] == "endpoint_mapping_unconfirmed"
        # 端点偏移的这一条留在道路端点证据层，绝不提升为严格核验。
        assert feature["properties"]["evidenceGrade"] == "model"
        assert feature["properties"]["routeDestination"] is not None
        # 每一家都画出来了：图上少一个点就等于少报一次核验。
        assert {item["properties"]["facilityId"] for item in collection["features"]} == \
            {item["id"] for item in document(client, task_id)["facilities"]["facilities"]}
