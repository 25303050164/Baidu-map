"""Versioned checkup HTTP surface, separate from the two legacy task APIs."""
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from ..algorithms.hybrid_isochrone.water_review import review_catalog
from .. import catalog
from ..catalog import major_of
from ..engines import STATUS_THRESHOLD_S
from .manager import CheckupError, CheckupManager
from .models import (DEFAULT_POI_REQUESTS, DEFAULT_ROUTE_REQUESTS, DETAIL_ROUTE_REQUESTS,
                     DISTANCE_RULE, MAX_POI_REQUESTS, MAX_ROUTE_REQUESTS, QUERY_PADDING_M,
                     RULE_VERSION, CheckupCapabilities, CheckupLayer, CheckupRequest,
                     CheckupSnapshot, CheckupTaskView, FacilityExtensionDocument,
                     FacilityExtensionRequest, FacilityExtensionView, FacilityRoute)

# Layers this release can serve, one per published group. The boundary arrives
# with the first stage, the retrieved facilities with the second, the assessment
# with the third, the route evidence with the fourth and the report with the
# fifth. ``report`` is a document rather than a shape: it is served from the same
# endpoint so a client can address any group of one revision the same way, and
# the layer says which of the two it is by which field it fills.
LAYER_IDS = ("isochrone", "facilities", "accessibility", "service_gaps", "heatmap",
             "verification", "report")
CACHE_CONTROL = "private, max-age=0, must-revalidate"
# The balance counts this application's own attempts. The browser SDK, other
# applications and the console's accounting sit outside it, so the interface
# must not present it as the account's remaining allowance.
QUOTA_LABEL = "本应用预算余额（不含浏览器 SDK、其他应用及旧接口流量）"


def _latest(manager: CheckupManager, task_id: str, revision: int | None = None):
    """The newest published revision, or an explicit not-ready refusal."""
    return manager.snapshot(task_id, revision)


def _layer_geometry(snapshot, layer_id: str):
    """One layer of one revision: ``(geometry, display_geometry, document)``.

    A group that is absent is a named refusal, never an empty shape — an empty
    collection drawn on a map reads as "nothing here", which is exactly the
    claim the stage refused to make.
    """
    if layer_id == "isochrone":
        # Stored revisions use the wire (camelCase) names; the snake_case read never matched.
        display = snapshot.isochrone.get("displayGeometry", snapshot.isochrone.get("display_geometry"))
        return snapshot.isochrone.get("geometry"), display, None
    if layer_id == "facilities":
        if snapshot.facilities is None:
            raise CheckupError(409, "checkup_facilities_not_ready", "设施结果尚未就绪")
        # Only the accepted facilities are drawn. Review candidates, exclusions and
        # quarantine are evidence for the report, not findings on a map.
        return {"type": "FeatureCollection", "coordinateSystem": "bd09ll",
                "features": [{"type": "Feature",
                              "geometry": {"type": "Point",
                                           "coordinates": [item["location"]["lng"],
                                                           item["location"]["lat"]]},
                              "properties": {
                                  "id": item["id"], "name": item["name"],
                                  "category": item["category"],
                                  "majorCategory": major_of(item["category"]),
                                  "address": item["address"],
                                  "classificationStatus": item["classificationStatus"],
                                  "possibleDuplicateGroup": item["possibleDuplicateGroup"]}}
                             for item in snapshot.facilities.facilities]}, None, None
    if layer_id == "accessibility":
        return _accessibility_layer(snapshot)
    if layer_id == "service_gaps":
        return _service_gaps_layer(snapshot)
    if layer_id == "heatmap":
        return _heatmap_layer(snapshot)
    if layer_id == "verification":
        return _verification_layer(snapshot)
    return _report_layer(snapshot)


def _accessibility_layer(snapshot):
    """The frozen assessment domain, with the per-category areas it was divided into.

    The domain is the denominator every area in the report is taken against, so it
    is drawable on its own: without it a reader cannot tell an assessable 100% from
    a 100% of a boundary that was never assessed at all.
    """
    evidence = snapshot.accessibility
    if evidence is None:
        raise CheckupError(409, "checkup_accessibility_not_ready", "服务覆盖结果尚未就绪")
    if evidence.domain is None:
        raise CheckupError(409, "checkup_domain_unavailable",
                           "本次体检没有可用的评估域，无法绘制服务覆盖图层")
    return {"type": "FeatureCollection", "coordinateSystem": "bd09ll",
            "features": [{"type": "Feature", "geometry": evidence.domain,
                          "properties": {
                              "status": evidence.status,
                              "gridStepM": evidence.grid_step_m,
                              "refinedStepM": evidence.refined_step_m,
                              "domainAreaM2": evidence.domain_area_m2,
                              "excludedAreaM2": evidence.excluded_area_m2,
                              "views": evidence.views,
                              "notes": evidence.notes,
                              "categories": [
                                  {"category": item.category, "supported": item.supported,
                                   "coveredM2": item.covered_m2, "gapM2": item.gap_m2,
                                   "unknownM2": item.unknown_m2,
                                   "unavailableReason": item.unavailable_reason}
                                  for item in evidence.categories]}}]}, None, None


def _service_gaps_layer(snapshot):
    """The grey-zone polygons, each carrying the reason it is one and what to check next.

    Counted geometry is metric; the drawn geometry is the same object in bd09ll.
    A zone whose drawn geometry is missing is refused rather than skipped: dropping
    it would draw a map that disagrees with the reported gap area.
    """
    gaps = snapshot.service_gaps
    if gaps is None:
        raise CheckupError(409, "checkup_service_gaps_not_ready", "服务盲区结果尚未就绪")
    features = []
    for zone in gaps.zones:
        if zone.display_geometry is None:
            raise CheckupError(409, "checkup_zone_geometry_incomplete",
                               f"灰区 {zone.id} 缺少展示几何，图层与报告面积不一致")
        features.append({"type": "Feature", "geometry": zone.display_geometry,
                         "properties": {key: value for key, value
                                        in zone.model_dump(mode="json", by_alias=True).items()
                                        if key not in ("geometry", "displayGeometry")}})
    return {"type": "FeatureCollection", "coordinateSystem": "bd09ll", "features": features,
            "properties": {"status": gaps.status, "gapAreaM2": gaps.gap_area_m2,
                           "compositeAreaM2": gaps.composite_area_m2,
                           "byCategoryM2": gaps.by_category_m2,
                           "obstacleLayerAvailable": gaps.obstacle_layer_available,
                           "notes": gaps.notes}}, None, None


def _heatmap_layer(snapshot):
    """Measured walking distances, per category, as points.

    Display only: the points show where a distance was measured, never a coverage
    verdict — those come from the grid, and the numbers from the wall charts.
    """
    heat = snapshot.heatmap
    if heat is None:
        raise CheckupError(409, "checkup_heatmap_not_ready", "热力数据尚未就绪")
    features = [{"type": "Feature",
                 "geometry": {"type": "Point", "coordinates": [point["lng"], point["lat"]]},
                 "properties": {"category": category, "cell": point["cell"],
                                "distanceM": point["distanceM"], "status": point["status"],
                                "nearestFacility": point.get("nearestFacility"),
                                **({"reason": point["reason"]} if point.get("reason") else {})}}
                for category, points in sorted(heat.categories.items())
                for point in points]
    return {"type": "FeatureCollection", "coordinateSystem": "bd09ll", "features": features,
            "properties": {"metric": heat.metric, "estimated": heat.estimated,
                           "stepM": heat.step_m, "domain": heat.domain, "notes": heat.notes,
                           # 水体用的是哪份数据、复核过哪里：地图据此画出冲突与底图误绘。
                           # 早于水系复核的修订为 null，界面据此标出"旧版本"。
                           "water": (None if snapshot.water is None
                                     else snapshot.water.model_dump(mode="json", by_alias=True))},
            }, None, None


def _verification_layer(snapshot):
    """问过路的那几家设施，以及每一家问回来的结果。

    画的是设施自己的位置，不是路线端点：端点可能被吸附到几十米外的道路上，把它当成
    "设施在这里"就等于静默挪动了设施。端点、偏移、判定和冲突与否都在属性里，所以图上
    少一个点是不可能的，图上多一个点也是不可能的。
    """
    evidence = snapshot.verification
    if evidence is None:
        raise CheckupError(409, "checkup_verification_not_ready", "核验结果尚未就绪")
    if evidence.status == "not_integrated":
        # 具名拒绝，不是一个空集合：空集合在地图上读作"这里都核验过了、没问题"。
        raise CheckupError(409, "checkup_verification_not_integrated",
                           "本次体检没有进行现实核验，核验图层不可用")
    features = []
    for record in evidence.facilities:
        location = record.get("location")
        if location is None:
            raise CheckupError(409, "checkup_verification_geometry_incomplete",
                               f"核验记录 {record['facilityId']} 缺少设施位置，"
                               f"图层与已冻结的证据不一致")
        features.append({"type": "Feature",
                         "geometry": {"type": "Point",
                                      "coordinates": [location["lng"], location["lat"]]},
                         "properties": {key: value for key, value in record.items()
                                        if key != "location"}})
    return {"type": "FeatureCollection", "coordinateSystem": "bd09ll", "features": features,
            "properties": {"status": evidence.status, "provider": evidence.provider,
                           "checked": evidence.checked, "failed": evidence.failed,
                           "unresolved": evidence.unresolved, "conflicts": evidence.conflicts,
                           "queries": evidence.queries, "notes": evidence.notes}}, None, None


def _report_layer(snapshot):
    """The report document. Not a shape, so it fills ``document`` and leaves both
    geometry fields null — the client asked for this group by name and a silently
    empty map would not be the answer."""
    if snapshot.report is None:
        raise CheckupError(409, "checkup_report_not_ready", "体检报告尚未就绪")
    return None, None, snapshot.report.model_dump(mode="json", by_alias=True)


def checkup_router(manager: CheckupManager):
    router = APIRouter(prefix="/api/v2/checkups", tags=["checkups"])

    @router.post("", status_code=202, response_model=CheckupTaskView)
    async def create(payload: CheckupRequest):
        view, _created = manager.submit(payload)
        return view

    # Declared before the bare task route so the literal segment is not read as
    # a task id, which would otherwise shadow it.
    @router.get("/by-request/{client_request_id}", response_model=CheckupTaskView)
    async def by_request(client_request_id: str):
        return manager.view(manager.by_request(client_request_id))

    @router.get("/{task_id}", response_model=CheckupTaskView)
    async def status(task_id: str):
        return manager.view(manager.get(task_id))

    @router.get("/{task_id}/result", response_model=CheckupSnapshot)
    async def result(task_id: str):
        return _latest(manager, task_id)[0]

    @router.get("/{task_id}/layers/{layer_id}")
    async def layer(task_id: str, layer_id: str, request: Request, revision: int | None = None):
        if layer_id not in LAYER_IDS:
            raise CheckupError(404, "checkup_layer_not_found", "未知图层")
        snapshot, stored = _latest(manager, task_id, revision)
        # The validator names the layer as well as the revision: two layers of one
        # revision are different representations, and a shared digest would let a
        # client revalidate a layer it never fetched.
        etag = f'"{stored["result_hash"]}-{layer_id}"'
        headers = {"ETag": etag, "Cache-Control": CACHE_CONTROL}
        # A revision is immutable, so a matching validator always means the
        # caller's copy is still current.
        if request.headers.get("if-none-match") in (etag, "*"):
            return Response(status_code=304, headers=headers)
        geometry, display, document = _layer_geometry(snapshot, layer_id)
        body = CheckupLayer(
            layer_id=layer_id, revision=stored["revision"], geometry=geometry,
            # Drawing shell only; never a counting or assessment surface.
            display_geometry=display, document=document, result_hash=stored["result_hash"])
        return JSONResponse(status_code=200, headers=headers,
                            content=body.model_dump(mode="json", by_alias=True))

    @router.post("/{task_id}/cancel", status_code=202, response_model=CheckupTaskView)
    async def cancel(task_id: str):
        return manager.cancel(task_id)[0]

    # 按需补查是独立资源，不是原任务的新修订：它自己的标识、自己的预算，
    # 失败或被取消都不会改动已经发布的报告和评分。
    @router.post("/{task_id}/facility-extensions", status_code=202,
                 response_model=FacilityExtensionView)
    async def create_extension(task_id: str, payload: FacilityExtensionRequest):
        return manager.submit_extension(task_id, payload)

    @router.get("/{task_id}/facility-extensions", response_model=list[FacilityExtensionView])
    async def list_extensions(task_id: str):
        return manager.extensions(task_id)

    @router.get("/{task_id}/facility-extensions/{extension_id}", response_model=FacilityExtensionView)
    async def extension_status(task_id: str, extension_id: str):
        return manager.extension(task_id, extension_id)

    @router.get("/{task_id}/facility-extensions/{extension_id}/result",
                response_model=FacilityExtensionDocument)
    async def extension_result(task_id: str, extension_id: str):
        return manager.extension_document(task_id, extension_id)

    @router.post("/{task_id}/facility-extensions/{extension_id}/cancel", status_code=202,
                 response_model=FacilityExtensionView)
    async def cancel_extension(task_id: str, extension_id: str):
        return manager.cancel_extension(task_id, extension_id)

    @router.post("/{task_id}/routes/{facility_id}", response_model=FacilityRoute)
    async def facility_route(task_id: str, facility_id: str):
        # A route detail is only ever issued for an identifier this task itself
        # retrieved: an id from anywhere else is not a facility of this checkup,
        # which is a different answer from "the facilities are not there yet".
        # Both of those, and the spending, live in the manager (§3.3).
        return await manager.detail_route(task_id, facility_id)

    return router


def capabilities_router(manager: CheckupManager, settings, offline=None):
    router = APIRouter(prefix="/api/v2", tags=["checkups"])

    @router.get("/capabilities", response_model=CheckupCapabilities)
    async def capabilities():
        graph_configured = settings.osm_graph_cache_path is not None \
            and settings.osm_graph_cache_path.is_file()
        graph_state = "unavailable" if not graph_configured else (None if offline is None else offline.state)
        return CheckupCapabilities(
            engines=[item.model_dump(mode="json", by_alias=True)
                     for item in manager.registry.capabilities()],
            # ``DistanceRule`` stays in its own snake-case form; it is the same
            # rule object the legacy contracts and the assessment use.
            rules={"distance": DISTANCE_RULE.model_dump(mode="json"), "ruleVersion": RULE_VERSION,
                   "statusThresholdSeconds": STATUS_THRESHOLD_S,
                   "assessmentScope": "isochrone",
                   "bandIsNotRadiusExpansion": True},
            data_versions={"osm": settings.osm_data_version,
                           "projection": settings.osm_metric_crs},
            water_reviews=[item for item in review_catalog(getattr(settings, "water_review_dir", None))
                           if item["osmDataVersion"] == settings.osm_data_version],
            coverage={"metricCrs": settings.osm_metric_crs, "queryPaddingM": QUERY_PADDING_M,
                      "graphConfigured": graph_configured,
                      "graphState": graph_state,
                      "coverageBoundaryConfigured": settings.osm_coverage_boundary_path is not None,
                      "completeDirectory": False},
            budgets={"poiRequests": DEFAULT_POI_REQUESTS, "routeRequests": DEFAULT_ROUTE_REQUESTS,
                     "detailRouteRequests": DETAIL_ROUTE_REQUESTS,
                     "maxPoiRequests": MAX_POI_REQUESTS, "maxRouteRequests": MAX_ROUTE_REQUESTS,
                     # 每块每小类先取主关键词一次，所以一次检索的下界是"分块数 × 小类数"。
                     # 分块数由圈面包络决定（最多 4），请求体里算不出来，这里给出小类数，
                     # 客户端据此在提交前判断预算够不够，而不是提交后拿到一个失败任务。
                     "poiMinorCategories": {
                         "default": len(catalog.poi_keys(catalog.default_analysis_majors())),
                         "all": len(catalog.poi_keys(catalog.majors())),
                     },
                     "poiBlocksUpperBound": 4,
                     "poiRequestsIsLowerBound": True},
            # §3.4 的跨任务复用窗口。未配置时缓存只在同一个任务内复用，
            # 所以重复体检同一片区域不会省下任何请求；这个值要能被客户端看到。
            cache={"freshnessSeconds": settings.cache_freshness_seconds,
                   "crossTaskReuse": settings.cache_freshness_seconds is not None},
            # 类别选择器的唯一来源：展示组、十个大类、每类检索小类数、核心口径。
            facility_categories=catalog.facility_categories(),
            # Reported per request rather than cached: the tier switches at a
            # wall-clock instant and the day turns at Shanghai midnight.
            quota={**manager.quota.balance(), "label": QUOTA_LABEL})

    return router
