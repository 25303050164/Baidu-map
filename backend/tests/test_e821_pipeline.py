"""E8.2.1 pipeline: one quota entry, cancellation, nearby service sources, two-layer
verification with a local recompute, and the small contract fixes around them.

Every case is offline; the conftest guard refuses any external connection.
"""
import asyncio
import time
from contextlib import AsyncExitStack

import pytest
from fastapi.testclient import TestClient
from life_circle.coordinates import normalize
from life_circle.models import CancelToken, RouteObservation

from app.accessibility.grid import GAP
from app.algorithms.hybrid_isochrone.cache import EvidenceSession
from app.algorithms.hybrid_isochrone.models import HybridConfig
from app.analyses import RateGate
from app.checkups import places as place_transport, routes as route_transport
from app.checkups.manager import _service_sources
from app.checkups.models import FacilityGroup
from app.checkups.router import _layer_geometry
from app.checkups.verification_stage import (CENTER_ATTEMPTS, judge_route, spot_check_plan,
                                             verify_facilities)
from app.config import Settings
from app.engines.protocol import EngineCancelled
from app.geo.projection import MetricProjection
from app.poi.normalize import merge_entities
from app.quota import AttemptCancelled, Quota, TieredGate
from app.scoring import CategoryAreas, category_score

import test_accessibility_stage as acc
from test_checkup_v2 import body, make_app, wait_for

ORIGIN = normalize((121.513925, 31.313079))


# -- one quota entry --------------------------------------------------------------

def test_the_shared_gate_follows_the_tier_and_a_cap_only_lowers_it(tmp_path):
    now = [0.0]
    gate = TieredGate(lambda: 16 if now[0] < 1 else 2, cap=None)
    assert gate.current_qps() == 16
    now[0] = 5
    asyncio.run(gate.wait(time.monotonic() + 5))
    assert gate.qps == 2 and gate.interval == 0.5
    capped = TieredGate(lambda: 16, cap=3)
    assert capped.current_qps() == 3
    # A cap above the tier never raises it.
    assert TieredGate(lambda: 2, cap=30).current_qps() == 2


def test_every_direction_caller_paces_on_one_gate(tmp_path):
    app = make_app(tmp_path)
    quota = app.state.quota
    assert app.state.analyses.gate is quota.direction.gate
    assert app.state.hybrid.gate is quota.direction.gate
    assert app.state.analyses.place_gate is quota.place.gate


def test_a_cancelled_token_stops_an_attempt_before_anything_is_reserved(tmp_path):
    quota = Quota(Settings(_env_file=None, quota_ledger_path=tmp_path / "q.sqlite3"))
    token = CancelToken()
    token.cancel()
    budget = quota.task_budget(isochrone=400)

    async def run():
        async with quota.direction.attempt(time.monotonic() + 5, budget=budget, pool="route",
                                           token=token):
            pytest.fail("a cancelled attempt must not be sent")
    with pytest.raises(AttemptCancelled):
        asyncio.run(run())
    assert budget.spent.get("route", 0) == 0


def test_a_hybrid_session_paces_itself_and_leaves_the_shared_gate_alone(tmp_path):
    gate = RateGate(16)
    before = gate.interval

    class Offline:
        network = False
        identity = ("offline",)
    EvidenceSession(ORIGIN, MetricProjection("EPSG:32651"), HybridConfig(), Offline(), gate,
                    path=tmp_path / "ledger.json")
    assert gate.interval == before


# -- the endpoint-tolerance layer --------------------------------------------------

def _route(distance, origin_offset, destination_offset, destination):
    return RouteObservation(destination, distance / 1.1, observed_duration=distance / 1.1,
                            endpoint_verified=True, distance_m=distance, route_origin=ORIGIN,
                            route_destination=destination, request_origin=ORIGIN,
                            origin_offset_m=origin_offset, destination_offset_m=destination_offset)


def test_small_endpoint_offsets_give_an_access_estimate_that_decides():
    destination = normalize((121.5150, 31.3140))
    judged = judge_route(_route(820, 12, 30, destination), ORIGIN, destination, "f")
    assert judged["layer"] == "endpoint_tolerance"
    assert judged["estimate"] == pytest.approx(862)
    assert judged["within"] is True
    # The strict layer is kept as an extra flag; it did not decide.
    assert judged["strict"].status == "pending"


def test_offsets_past_the_limit_or_an_unresolved_entrance_do_not_decide():
    destination = normalize((121.5150, 31.3140))
    too_far = judge_route(_route(600, 5, 60, destination), ORIGIN, destination, "f")
    unresolved = judge_route(_route(600, 5, 20, destination), ORIGIN, destination, "f",
                             entrance_status="unresolved")
    for judged in (too_far, unresolved):
        assert judged["layer"] is None and judged["within"] is None
    band = judge_route(_route(1030, 10, 20, destination), ORIGIN, destination, "f")
    assert band["layer"] == "endpoint_tolerance" and band["within"] is None


# -- coverage spot checks and the local recompute ---------------------------------

class _Pool:
    def attempt(self, deadline, *, budget=None, pool=None, token=None):
        return _Attempt()


class _Attempt:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def outcome(self, reason):
        pass


class _Routes:
    """Answers a route by a distance table keyed by destination facility id."""

    def __init__(self, distances):
        self.distances, self.sent = distances, []

    def session(self, pool, *, budget, deadline):
        return route_transport.RouteSession(self, pool, budget=budget, deadline=deadline)

    identity, network = "offline-routes", False

    async def route(self, facility_id, origin, destination, timeout):
        self.sent.append((facility_id, origin))
        distance = self.distances[facility_id]
        return RouteObservation(destination, distance / 1.1, observed_duration=distance / 1.1,
                                endpoint_verified=True, distance_m=distance, route_origin=origin,
                                route_destination=destination, request_origin=origin,
                                origin_offset_m=5.0, destination_offset_m=10.0)


def _facility(fid, dx, category="pharmacy"):
    item = acc.facility(dx, facility_id=fid, category=category)
    return item


def _heat(status, dx, *, distance=None, nearest=None, cell="0:1:1"):
    lng, lat = acc.public(dx)
    return {"cell": cell, "lng": lng, "lat": lat, "distanceM": distance,
            "nearestFacility": nearest, "status": status}


def _verify(heat_points, facilities, distances, token=None):
    routes = _Routes(distances)
    session = routes.session(_Pool(), budget=None, deadline=time.monotonic() + 60)
    outcome = asyncio.run(verify_facilities(
        facilities=facilities, majors=("medical",), zones=[], entrances={},
        heatmap={"categories": {"medical": heat_points}}, session=session,
        origin=normalize(acc.public(0)), token=token))
    return routes, outcome


def test_a_gap_with_a_route_within_the_rule_is_a_conflict_handed_to_the_recompute():
    facilities = [_facility("near", 400)]
    routes, outcome = _verify([_heat(GAP, 0, cell="0:0:0")], facilities, {"near": 500})
    spot = outcome.evidence.spot_checks[0]
    assert spot["outcome"] == "gap_route_within" and spot["accessDistanceM"] == pytest.approx(515)
    assert outcome.overrides == [{"major": "medical", "cell": "0:0:0", "lng": spot["lng"],
                                  "lat": spot["lat"], "verdict": "covered", "contradicted": "gap"}]
    assert outcome.evidence.conflicts and outcome.status == "partial"


def test_a_covered_cell_whose_nearest_facility_fails_asks_one_alternative():
    facilities = [_facility("model", 300), _facility("other", 500)]
    point = _heat("covered", 0, distance=880, nearest="model")
    _routes, agreed = _verify([point], facilities, {"model": 1300, "other": 700})
    assert agreed.evidence.spot_checks[0]["outcome"] == "agree_alternative"
    assert not agreed.overrides
    _routes, failed = _verify([point], facilities, {"model": 1300, "other": 1400})
    assert failed.evidence.spot_checks[0]["outcome"] == "covered_not_confirmed"
    # One facility failing never makes a gap: the cell becomes unknown.
    assert failed.overrides[0]["verdict"] == "unknown"
    assert failed.overrides[0]["contradicted"] == "covered"


def test_the_center_layer_and_the_spot_layer_keep_their_own_budgets():
    facilities = [_facility(f"f{i}", 200 + i) for i in range(60)]
    heat = [_heat(GAP, 0, cell=f"0:{i}:0") for i in range(10)]
    routes, outcome = _verify(heat, facilities, {f"f{i}": 600 for i in range(60)})
    queries = outcome.evidence.queries
    assert queries["centerAttempts"] <= CENTER_ATTEMPTS
    assert queries["spotAttempts"] > 0
    assert outcome.evidence.spot_check_summary["checked"] == 10


def test_a_cancelled_verification_sends_nothing_more():
    token = CancelToken()
    token.cancel()
    routes, outcome = _verify([_heat(GAP, 0)], [_facility("near", 400)], {"near": 500}, token=token)
    assert routes.sent == []
    assert outcome.evidence.queries["stopReason"] == "cancelled"


def test_the_local_recompute_decides_only_the_measured_cell():
    baseline = acc.run()
    medical = lambda outcome: next(item for item in outcome.accessibility.categories
                                   if item.category == "medical")
    before = medical(baseline)
    # Far along the road the pharmacy is out of reach: the model calls this row a gap
    # (the row just north is unknown: its far support points cannot attach).
    lng, lat = acc.public(2400, -21)
    verified = [{"major": "medical", "lng": lng, "lat": lat, "verdict": "covered",
                 "contradicted": "gap"}]
    after = medical(acc.run(verified=verified))
    # One 25 m cell turns covered; its three siblings, whose gap the route disproved,
    # become unknown. The domain area is unchanged.
    assert after.covered_m2 == pytest.approx(before.covered_m2 + 625, abs=1)
    assert after.gap_m2 == pytest.approx(before.gap_m2 - 2500, abs=1)
    assert after.unknown_m2 == pytest.approx(before.unknown_m2 + 1875, abs=1)


# -- nearby facilities serve without being counted ---------------------------------

def test_an_accepted_facility_outside_the_boundary_is_a_service_source_not_a_count():
    group = FacilityGroup(query_status="completed", provider="p", api_version="3.0",
                          data_source="synthetic", query_domain={},
                          facilities=[{"id": "in"}], nearby_facilities=[{"id": "out"}])
    assert [item["id"] for item in _service_sources(group)] == ["in", "out"]
    # Coverage near the boundary comes from the outside pharmacy.
    only_far = acc.run(facilities=[acc.facility(2500)])
    # 300 m west of the boundary, still on the test road (it ends 400 m west).
    with_outside = acc.run(facilities=[acc.facility(2500), acc.facility(-300, facility_id="f-out")])
    covered = lambda outcome: next(item for item in outcome.accessibility.categories
                                   if item.category == "medical").covered_m2
    assert covered(with_outside) > covered(only_far) + 10000


def test_merge_entities_classifies_the_outside_records_into_nearby():
    from app.poi.models import PoiCollectRequest, Point as WirePoint
    from app.poi.normalize import normalize as normalize_row
    from test_checkup_facilities import supported_tag
    request = PoiCollectRequest(center=WirePoint(lng=ORIGIN[0], lat=ORIGIN[1]),
                                coordinate_system="bd09ll", categories=["pharmacy"])
    word = supported_tag("pharmacy")
    record = normalize_row({"uid": "x", "name": f"{word}样本", "address": f"{word}路1号",
                            "location": {"lng": 121.52, "lat": 31.32},
                            "detail_info": {"classified_poi_tag": word, "parent_id": None}},
                           {"tileId": "t", "query": "q", "pageNum": 0}, "baidu_place")
    nearby = []
    accepted, review, excluded, outside, _ = merge_entities([record], request,
                                                            within=lambda point: False, nearby=nearby)
    assert accepted == [] and len(outside) == 1
    assert [item["id"] for item in nearby] == ["baidu_place:x"]
    assert nearby[0]["countingRegion"] == "outside"


# -- retrieval completeness, cell by cell ---------------------------------------------

def _pharmacy_block(dx0, dx1):
    """An unfinished pharmacy query block over the test road, as the retrieval reports it."""
    corners = [acc.public(dx0, -300), acc.public(dx1, -300), acc.public(dx1, 300),
               acc.public(dx0, 300), acc.public(dx0, -300)]
    return {"pharmacy": [{"type": "Polygon", "coordinates": [[list(c) for c in corners]]}]}


def test_an_unfinished_block_only_blocks_the_gaps_within_service_reach():
    gap = lambda outcome: next(item for item in outcome.accessibility.categories
                               if item.category == "medical").gap_m2
    complete = acc.run()
    # The old task-wide switch: one unfinished block anywhere, no gap anywhere.
    assert gap(acc.run(query_status="partial")) == 0
    # A block far beyond service reach leaves every gap standing...
    assert gap(acc.run(query_status="partial", incomplete=_pharmacy_block(6000, 6200))) == gap(complete)
    # ...one near the far end withdraws only the gaps it could serve.
    near = gap(acc.run(query_status="partial", incomplete=_pharmacy_block(3200, 3400)))
    assert 0 < near < gap(complete)


def test_a_planner_that_finished_nothing_reports_every_block_unfinished():
    from app.poi.online import OnlinePlanner, QueryDomain

    async def failing(sequence, page):
        return None, "timeout"
    planner = OnlinePlanner(domain=QueryDomain.circle(1300), origin=ORIGIN,
                            categories=["pharmacy"], budget=8, source="synthetic")
    result = asyncio.run(planner.run(failing))
    assert result.status in ("failed", "partial")
    unfinished = result.incomplete["pharmacy"]
    assert {bounds for bounds in unfinished} >= {block.bounds for block in planner.coarse}


# -- small contract fixes ------------------------------------------------------------

def test_a_category_without_spatial_support_is_undetermined_even_if_its_areas_disagree():
    score = category_score("medical", CategoryAreas(covered_m2=1, gap_m2=0, unknown_m2=0),
                           domain_area_m2=100.0, spatial_support=False,
                           unavailable_reason="areas_do_not_match_domain")
    assert score.supported is False and score.coverage_lower_pct is None


def test_synthetic_mode_never_opens_a_live_transport_even_with_a_key():
    settings = Settings(_env_file=None, baidu_map_ak="k" * 32, analysis_provider="synthetic")

    async def run():
        async with AsyncExitStack() as stack:
            with pytest.raises(place_transport.PlacesUnavailable, match="synthetic_mode_offline"):
                await place_transport.open_online(settings, stack)
            with pytest.raises(route_transport.RoutesUnavailable, match="synthetic_mode_offline"):
                await route_transport.open_online(settings, stack)
    asyncio.run(run())


def test_synthetic_mode_says_so_where_the_engine_is_chosen(tmp_path, caplog):
    from app.main import create_app

    def e82(provider):
        settings = Settings(_env_file=None, baidu_map_ak="k" * 32, analysis_provider=provider,
                            checkup_dir=tmp_path / provider, quota_ledger_path=tmp_path / f"{provider}.sqlite3")
        with caplog.at_level("WARNING", logger="app.main"), TestClient(create_app(settings)) as client:
            engines = client.get("/api/v2/capabilities").json()["engines"]
        return next(e for e in engines if e["engineId"] == "baidu_e82")

    synthetic = e82("synthetic")
    assert "合成替身" in synthetic["label"]
    assert any("正圆" in note and "不是百度实测" in note for note in synthetic["notes"])
    # The real-mode claim would be false here.
    assert not any("百度实际返回" in note for note in synthetic["notes"])
    assert any("ANALYSIS_PROVIDER=synthetic" in r.getMessage() for r in caplog.records)
    caplog.clear()
    real = e82("baidu")
    assert real["label"] == "百度边界搜索（E8.2）"
    assert any("百度实际返回" in note for note in real["notes"])
    assert not any("ANALYSIS_PROVIDER=synthetic" in r.getMessage() for r in caplog.records)


def test_the_isochrone_layer_returns_the_stored_display_shell():
    class Snapshot:
        isochrone = {"geometry": {"type": "MultiPolygon"}, "displayGeometry": {"type": "Polygon"}}
    geometry, display, _ = _layer_geometry(Snapshot(), "isochrone")
    assert display == {"type": "Polygon"}


def test_an_engine_that_cannot_run_is_refused_at_admission(tmp_path):
    settings = Settings(_env_file=None, baidu_map_ak="", analysis_provider="baidu",
                        checkup_dir=tmp_path / "checkups", quota_ledger_path=tmp_path / "q.sqlite3")
    from app.main import create_app
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.post("/api/v2/checkups", json=body())
    assert response.status_code == 503
    assert response.json()["code"] == "checkup_engine_unavailable"


def test_a_business_cancel_from_an_engine_leaves_the_worker_serving(tmp_path):
    app = make_app(tmp_path)

    class Cancelling:
        engine_id = "baidu_e82"

        def __init__(self, real):
            self.real = real

        def capabilities(self):
            return self.real.capabilities()

        async def compute(self, ask, context):
            raise EngineCancelled()
    registry = app.state.checkups.registry
    registry.engines["baidu_e82"] = Cancelling(registry.engines["baidu_e82"])
    with TestClient(app) as client:
        first = client.post("/api/v2/checkups", json=body(clientRequestId="a")).json()
        second = client.post("/api/v2/checkups", json=body(clientRequestId="b")).json()
        assert wait_for(client, first["taskId"])["status"] == "cancelled"
        # The second task was still served: the worker did not die with the first.
        assert wait_for(client, second["taskId"])["status"] == "cancelled"
