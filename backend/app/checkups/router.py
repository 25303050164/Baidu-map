"""Versioned checkup HTTP surface, separate from the two legacy task APIs."""
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from ..engines import STATUS_THRESHOLD_S
from .manager import CheckupError, CheckupManager
from .models import (DEFAULT_POI_REQUESTS, DEFAULT_ROUTE_REQUESTS, DETAIL_ROUTE_REQUESTS,
                     DISTANCE_RULE, MAX_POI_REQUESTS, MAX_ROUTE_REQUESTS, QUERY_PADDING_M,
                     RULE_VERSION, CheckupCapabilities, CheckupLayer, CheckupRequest,
                     CheckupSnapshot, CheckupTaskView)

# Layers this release can serve. Isochrone is published at the first stage;
# the heatmap, grey-zone and facility layers arrive with their own stages.
LAYER_IDS = ("isochrone",)
CACHE_CONTROL = "private, max-age=0, must-revalidate"


def _latest(manager: CheckupManager, task_id: str, revision: int | None = None):
    """The newest published revision, or an explicit not-ready refusal."""
    manager.get(task_id)
    stored = manager.store.revision(task_id, revision)
    if stored is None:
        raise CheckupError(409, "checkup_result_not_ready", "尚无可用结果快照")
    # Re-validated from the stored mapping so a hand-edited file cannot widen
    # the contract; serialization always goes through the model.
    return CheckupSnapshot(**stored["snapshot"]), stored


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
        etag = f'"{stored["result_hash"]}"'
        headers = {"ETag": etag, "Cache-Control": CACHE_CONTROL}
        # A revision is immutable, so a matching validator always means the
        # caller's copy is still current.
        if request.headers.get("if-none-match") in (etag, "*"):
            return Response(status_code=304, headers=headers)
        body = CheckupLayer(
            layer_id=layer_id, revision=stored["revision"],
            geometry=snapshot.isochrone.get("geometry"),
            # Drawing shell only; never a counting or assessment surface.
            display_geometry=snapshot.isochrone.get("display_geometry"),
            result_hash=stored["result_hash"])
        return JSONResponse(status_code=200, headers=headers,
                            content=body.model_dump(mode="json", by_alias=True))

    @router.post("/{task_id}/cancel", status_code=202, response_model=CheckupTaskView)
    async def cancel(task_id: str):
        return manager.cancel(task_id)[0]

    @router.post("/{task_id}/routes/{facility_id}")
    async def facility_route(task_id: str, facility_id: str):
        # A route detail is only ever issued for an identifier this task itself
        # retrieved, so there is nothing to look up before the facility stage.
        manager.get(task_id)
        raise CheckupError(409, "checkup_facilities_not_ready", "设施结果尚未就绪")

    return router


def capabilities_router(manager: CheckupManager, settings):
    router = APIRouter(prefix="/api/v2", tags=["checkups"])

    @router.get("/capabilities", response_model=CheckupCapabilities)
    async def capabilities():
        graph_configured = settings.osm_graph_cache_path is not None \
            and settings.osm_graph_cache_path.is_file()
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
            coverage={"metricCrs": settings.osm_metric_crs, "queryPaddingM": QUERY_PADDING_M,
                      "graphConfigured": graph_configured,
                      "coverageBoundaryConfigured": settings.osm_coverage_boundary_path is not None,
                      "completeDirectory": False},
            budgets={"poiRequests": DEFAULT_POI_REQUESTS, "routeRequests": DEFAULT_ROUTE_REQUESTS,
                     "detailRouteRequests": DETAIL_ROUTE_REQUESTS,
                     "maxPoiRequests": MAX_POI_REQUESTS, "maxRouteRequests": MAX_ROUTE_REQUESTS})

    return router
