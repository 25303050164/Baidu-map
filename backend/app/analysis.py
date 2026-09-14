import asyncio
import math
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from life_circle.engine import compute_isochrone
from life_circle.models import CancelToken, IsochroneRequest
from life_circle.providers import AnalyticProvider
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator
from typing import Literal

from .contracts import AnalysisResponse, Data, Issue, Origin, Rules, Status, SyntheticRequest

router = APIRouter(prefix="/api/v1/analysis", tags=["N04/N05"], responses={
    422: {"model": AnalysisResponse, "description": "Invalid request"},
    500: {"model": AnalysisResponse, "description": "Internal failure"},
})
MOCK_DIR = Path(__file__).resolve().parents[1] / "mocks"


class OsmOfflineRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)
    origin: Origin | None = None
    center: Origin | None = None
    coordinate_system: Literal["bd09ll"] = Field(
        default="bd09ll", alias="coordinateSystem",
        validation_alias=AliasChoices("coordinate_system", "coordinateSystem"),
    )
    budget: Literal[200, 400, 800] = 400

    @model_validator(mode="after")
    def validate_request(self):
        if self.origin is None and self.center is None:
            raise ValueError("origin is required")
        return self


@router.get("/mock/{scenario}", response_model=AnalysisResponse)
def mock_analysis(scenario: Status):
    return AnalysisResponse.model_validate_json((MOCK_DIR / f"{scenario}.json").read_text(encoding="utf-8"))


@router.post("/osm_offline", response_model=AnalysisResponse)
def osm_offline_analysis(payload: OsmOfflineRequest, request: Request):
    """Synchronous cache diagnostic; normal clients use the task API instead."""
    manager = request.app.state.analyses
    engine = manager.osm_engine
    if engine is None:
        raise HTTPException(503, "OSM 离线缓存不可用")
    origin = payload.origin or payload.center
    assert origin is not None
    point = (origin.lng, origin.lat)
    if not manager.osm_cache.contains(point):
        raise HTTPException(409, "分析中心不在 OSM 覆盖范围内")
    result = asyncio.run(engine.run(point, payload.budget, CancelToken()))
    data = result.to_dict()
    failed = result.quality == "insufficient"
    return AnalysisResponse(
        status="failed" if failed else "partial", source="osm_offline", origin=origin,
        data=Data(geometry=data["geometry"], uncertain_region=data["uncertainRegion"], unknown_region=data["unknownRegion"], computation_extent=data["computationExtent"]),
        algorithm=data,
        warnings=[Issue(code="OSM_OFFLINE", message="OSM 离线路网诊断结果仅用于开发验收。", scope="all")],
        errors=[Issue(code="INSUFFICIENT_EVIDENCE", message="没有足够离线路网证据生成等时圈。", scope="isochrone", severity="error")] if failed else [],
    )


def run_synthetic(request: SyntheticRequest):
    origin = (request.origin.lng, request.origin.lat)
    config = IsochroneRequest(origin, request.coordinate_system, budget=request.budget, expand=False)

    def observed(x, y):
        if request.scenario == "global_failure" or (
            request.scenario == "local_failure" and 300 < x < 750 and -350 < y < 350
        ):
            return None
        return math.hypot(x, y) / 1.2

    result = asyncio.run(compute_isochrone(config, AnalyticProvider(origin, observed)))
    payload = result.to_dict()
    # Whole-analysis status cannot be complete before facilities/report are implemented.
    return AnalysisResponse(
        status="failed" if result.quality == "insufficient" else "partial",
        source="synthetic", algorithm_version="aca992d", origin=request.origin, rules=Rules(distance=request.distance_rule),
        data=Data(geometry=payload["geometry"], uncertain_region=payload["uncertainRegion"],
                  unknown_region=payload["unknownRegion"], computation_extent=payload["computationExtent"]),
        algorithm=payload,
        warnings=[Issue(code="SYNTHETIC_ONLY", message="合成时间场，不代表真实社区。", scope="all"),
                  Issue(code="RULES_PENDING", message="阶段0/N01业务口径尚待团队确认；参数详见N04。", scope="rules", severity="pending"),
                  Issue(code="MODULES_NOT_RUN", message="设施、盲区和报告未执行。", scope="facilities,blind_points,report")],
        errors=[Issue(code="INSUFFICIENT_EVIDENCE", message="没有足够步行证据生成等时圈。", scope="isochrone", severity="error")]
        if result.quality == "insufficient" else [],
    )


@router.post("/synthetic", response_model=AnalysisResponse)
def synthetic_analysis(request: SyntheticRequest):
    # FastAPI runs sync handlers in its worker pool: geometry work does not block the event loop.
    return run_synthetic(request)
