"""An independent offline entry point using the existing analysis envelope."""
from fastapi import APIRouter, Request
from life_circle.models import IsochroneRequest

from .contracts import AnalysisResponse, Data, Issue, OsmOfflineRequest, map_business_status

router = APIRouter(prefix="/api/v1/analysis", tags=["OSM Offline"], responses={
    422: {"model": AnalysisResponse}, 500: {"model": AnalysisResponse},
})


@router.post("/osm_offline", response_model=AnalysisResponse)
def offline_analysis(request: OsmOfflineRequest, http_request: Request):
    engine = http_request.app.state.osm_offline
    config = IsochroneRequest((request.origin.lng, request.origin.lat), request.coordinate_system,
                             threshold=request.threshold, config_version="osm-offline-v1")
    computed = engine.compute(config)
    result = computed.result
    insufficient = result.quality == "insufficient"
    return AnalysisResponse(
        status=map_business_status(quality=result.quality, facilities_status="not_integrated"),
        source="system", algorithm_version="osm-offline-v1", origin=request.origin,
        # No interpolation-style uncertainty/unknown field is claimed for OSM.
        data=Data(geometry=result.geometry), algorithm=computed.payload(),
        warnings=[Issue(code=warning.upper(), message=warning, scope="isochrone") for warning in result.warnings]
        + [Issue(code="OSM_BASELINE", message="OSM 离线基线，不是真值；buffer 仅用于面状展示。", scope="isochrone"),
           Issue(code="MODULES_NOT_RUN", message="设施、盲区和报告未执行。", scope="facilities,blind_points,report")],
        errors=[Issue(code="INSUFFICIENT_EVIDENCE", message=result.stop_reason, scope="isochrone", severity="error")]
        if insufficient else [],
    )
