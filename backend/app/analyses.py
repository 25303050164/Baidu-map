"""Single-process, bounded analysis jobs. Never include transport errors in responses."""
import asyncio
import math
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Literal
from uuid import uuid4

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from life_circle.coordinates import normalize
from life_circle.engine import compute_isochrone
from life_circle.models import CancelToken, IsochroneRequest, ProgressSnapshot, RouteObservation
from life_circle.providers import AnalyticProvider, BaiduProvider

from .baidu import silence_transport_logs
from .contracts import (
    AnalysisCapabilitiesResponse,
    AnalysisMode,
    Data,
    HybridBranch,
    HybridComparison,
    HybridResult,
    Issue,
    ModeCapability,
    OsmProvenance,
    Provenance,
    RouteEvidence,
    Rules,
    SourceProvenance,
    TaskResultResponse,
    TaskStatusResponse,
    map_business_status,
)
from .rules import DistanceRule
from .facilities import analyze_facilities
from .osm import OsmCache, OsmOfflineEngine


class Center(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    lng: float = Field(ge=-180, le=180, allow_inf_nan=False)
    lat: float = Field(gt=-85, lt=85, allow_inf_nan=False)


class AnalysisInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    center: Center
    coordinateSystem: Literal["bd09ll"]
    budget: int = 400
    analysisMode: AnalysisMode = AnalysisMode.BAIDU_ONLINE
    clientRequestId: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")

    @field_validator("budget")
    @classmethod
    def budgets(cls, value):
        if value not in (200, 400, 800):
            raise ValueError("budget must be 200, 400 or 800")
        return value

    @field_validator("analysisMode", mode="before")
    @classmethod
    def analysis_modes(cls, value):
        if isinstance(value, str):
            try:
                return AnalysisMode(value)
            except ValueError:
                pass
        return value

    def fingerprint(self):
        return normalize((self.center.lng, self.center.lat)), self.budget, self.analysisMode


TERMINAL = {"completed", "cancelled", "failed"}


@dataclass
class Job:
    task_id: str
    payload: AnalysisInput
    data_source: str
    analysis_mode: AnalysisMode = AnalysisMode.BAIDU_ONLINE
    started: float = field(default_factory=time.monotonic)
    status: str = "running"
    token: CancelToken = field(default_factory=CancelToken)
    progress: ProgressSnapshot | None = None
    baidu_progress: ProgressSnapshot | None = None
    osm_progress: ProgressSnapshot | None = None
    result: dict | None = None
    error: str | None = None
    finished_at: float | None = None
    task: asyncio.Task | None = None
    route_clicks: int = 0
    route_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def business_status(self):
        if self.status == "failed":
            return "failed"
        if self.status != "completed" or not self.result:
            return None
        return self.result.get("businessStatus", "partial")

    def view(self):
        progress = self.baidu_progress if self.analysis_mode == AnalysisMode.HYBRID and self.baidu_progress else self.progress
        requests = progress.requests if progress else 0
        network_requests = progress.network_requests if progress else 0
        if self.analysis_mode == AnalysisMode.OSM_OFFLINE:
            # Offline samples are evidence, not Baidu API calls.
            requests = network_requests = 0
        return TaskStatusResponse(
            task_id=self.task_id, status=self.status, business_status=self.business_status(),
            stage=self.status if self.status in TERMINAL or self.status == "cancelling"
            else self.progress.stage if self.progress else "initializing",
            requests=requests,
            network_requests=network_requests,
            budget=self.payload.budget,
            elapsed_seconds=max(0, (self.finished_at or time.monotonic()) - self.started),
            analysis_mode=self.analysis_mode, data_source=self.data_source, error=self.error,
        ).model_dump(by_alias=True)


class RateGate:
    """Shared across jobs, including retries; space attempts after completion."""
    def __init__(self, qps, *, clock=time.monotonic, sleep=asyncio.sleep, spacing_clock=None):
        self.interval = 1 / qps if qps else 0
        self.next_send = 0
        self.lock = asyncio.Lock()
        self.attempt_lock = asyncio.Lock()
        self.clock, self.sleep = clock, sleep
        # On Python 3.11/Windows monotonic can be quantized to 15.625 ms.
        # Deadlines keep their original epoch; pacing uses the precise counter.
        self.spacing_clock = spacing_clock or (time.perf_counter if clock is time.monotonic else clock)

    async def wait(self, deadline):
        async with self.lock:
            now = self.spacing_clock()
            when = max(now, self.next_send)
            if self.clock() + when - now >= deadline:
                return False
            await self.sleep(max(0, when - now))
            # asyncio timers can wake before their requested time. Recheck the
            # clock under the lock rather than treating sleep as a permit.
            while self.spacing_clock() < when:
                if self.clock() >= deadline:
                    return False
                await self.sleep(max(when - self.spacing_clock(), time.get_clock_info('monotonic').resolution))
            if self.clock() >= deadline:
                return False
            self.next_send = self.spacing_clock() + self.interval
            return True

    def completed(self, reason):
        # Cool down all subsequent attempts, not just this destination's retry.
        cooldown = max(self.interval, 1) if reason in ('rate_limit', 'timeout', 'interrupted') else self.interval
        self.next_send = max(self.next_send, self.spacing_clock() + cooldown)


class LimitedProvider:
    network = True

    def __init__(self, provider, gate):
        self.provider, self.gate = provider, gate
        self.identity = provider.identity

    async def query_walking_time(self, origin, destination, deadline):
        # A permit alone cannot control server arrivals after DNS/TLS/pool delays.
        # Keep the shared slot through the response and start spacing afterwards.
        async with self.gate.attempt_lock:
            if not await self.gate.wait(deadline):
                return RouteObservation(destination, reason="deadline")
            reason = 'interrupted'
            try:
                result = await self.provider.query_walking_time(origin, destination, deadline)
                reason = result.reason
                return result
            finally:
                self.gate.completed(reason)


class AnalysisManager:
    def __init__(self, settings, provider_factory=None):
        self.settings, self.provider_factory = settings, provider_factory
        self.jobs = {}
        self.gate = RateGate(settings.analysis_qps)
        self.osm_cache = OsmCache.load(
            getattr(settings, "osm_cache_location", None),
            expected_version=getattr(settings, "osm_cache_version", None),
            speed_mps=getattr(settings, "osm_walk_speed_mps", 1.3),
        )
        self.osm_engine = OsmOfflineEngine(self.osm_cache) if self.osm_cache.available else None

    @property
    def baidu_available(self):
        return bool(
            self.provider_factory
            or self.settings.analysis_provider == "synthetic"
            or (self.settings.ak_configured and self.settings.analysis_qps is not None)
        )

    def capabilities(self):
        osm = self.osm_cache.info
        baidu = ModeCapability(
            available=self.baidu_available,
            availability="ready" if self.baidu_available else "unavailable",
            reason=None if self.baidu_available else "baidu_not_configured",
        )
        osm_capability = ModeCapability(
            available=self.osm_cache.available,
            availability=osm.availability,
            coverage_city=osm.coverage_city,
            data_version=osm.data_version,
            data_date=osm.data_date,
            limitations=list(osm.limitations),
            reason=osm.reason,
        )
        hybrid_available = self.baidu_available and self.osm_cache.available
        hybrid = ModeCapability(
            available=hybrid_available,
            availability="ready" if hybrid_available else "unavailable",
            dependencies=[AnalysisMode.BAIDU_ONLINE.value, AnalysisMode.OSM_OFFLINE.value],
            limitations=["百度与 OSM 并行计算，仅做结果对比，不直接合并几何"],
            reason=None if hybrid_available else "dependency_unavailable",
        )
        return AnalysisCapabilitiesResponse(modes={
            AnalysisMode.BAIDU_ONLINE.value: baidu,
            AnalysisMode.OSM_OFFLINE.value: osm_capability,
            AnalysisMode.HYBRID.value: hybrid,
        }).model_dump(by_alias=True)

    def _ensure_mode_available(self, mode: AnalysisMode, origin):
        if mode in (AnalysisMode.BAIDU_ONLINE, AnalysisMode.HYBRID) and not self.baidu_available:
            raise HTTPException(503, "百度在线分析暂未配置")
        if mode in (AnalysisMode.OSM_OFFLINE, AnalysisMode.HYBRID):
            if not self.osm_cache.available or self.osm_engine is None:
                raise HTTPException(503, "OSM 离线缓存不可用")
            if not self.osm_cache.contains(origin):
                raise HTTPException(409, "分析中心不在 OSM 覆盖范围内")

    def prune(self):
        terminal = sorted((job for job in self.jobs.values() if job.status in TERMINAL and job.finished_at is not None), key=lambda job: job.finished_at)
        for index, job in enumerate(terminal):
            if time.monotonic() - job.finished_at >= 1800 or index < len(terminal) - 20:
                del self.jobs[job.task_id]

    def get(self, task_id):
        self.prune()
        if task_id not in self.jobs:
            raise HTTPException(404, "任务不存在或已过期")
        return self.jobs[task_id]

    def create(self, payload):
        self.prune()
        for job in self.jobs.values():
            if job.payload.clientRequestId == payload.clientRequestId:
                if job.payload.fingerprint() != payload.fingerprint():
                    raise HTTPException(409, "请求标识已用于不同分析")
                return job
        if any(job.task and not job.task.done() for job in self.jobs.values()):
            raise HTTPException(409, "分析服务忙，请等待当前任务完成或取消后重试")
        origin = normalize((payload.center.lng, payload.center.lat))
        self._ensure_mode_available(payload.analysisMode, origin)
        if payload.analysisMode == AnalysisMode.BAIDU_ONLINE and self.settings.analysis_provider == "baidu" and not self.provider_factory:
            if 143 / self.settings.analysis_qps >= 600:
                raise HTTPException(503, "配置的 QPS 无法在截止时间内完成初始化")
        if payload.analysisMode == AnalysisMode.OSM_OFFLINE:
            source = "osm_offline"
        elif payload.analysisMode == AnalysisMode.HYBRID:
            source = "hybrid"
        else:
            source = "synthetic" if self.settings.analysis_provider == "synthetic" else "baidu_walking"
        job = Job(str(uuid4()), payload, source, payload.analysisMode)
        self.jobs[job.task_id] = job
        job.task = asyncio.create_task(self.run(job))
        return job

    def update(self, job, progress):
        if job.status == "running":
            job.progress = progress
            if progress.stage.startswith("baidu_"):
                job.baidu_progress = progress
            elif progress.stage.startswith("osm_"):
                job.osm_progress = progress

    async def _run_baidu(self, job, origin, budget, stack, *, stage_prefix=""):
        client = None
        if self.provider_factory:
            provider = self.provider_factory(origin)
            if hasattr(provider, "__aenter__"):
                provider = await stack.enter_async_context(provider)
        elif self.settings.analysis_provider == "synthetic":
            provider = AnalyticProvider(origin, lambda x, y: math.hypot(x, y) / 1.2)
        else:
            silence_transport_logs()
            client = await stack.enter_async_context(httpx.AsyncClient(trust_env=False, follow_redirects=False))
            provider = LimitedProvider(BaiduProvider(self.settings.baidu_map_ak.get_secret_value(), client=client), self.gate)

        def progress(snapshot):
            stage = f"{stage_prefix}{snapshot.stage}" if stage_prefix else snapshot.stage
            self.update(job, ProgressSnapshot(stage, snapshot.requests, snapshot.network_requests,
                                               snapshot.budget, snapshot.elapsed_seconds))

        request = IsochroneRequest(origin, "bd09ll", budget=budget,
            qps=self.settings.analysis_qps if provider.network else None)
        result = await compute_isochrone(request, provider, job.token, on_progress=progress)
        business = None
        if not self.provider_factory and provider.network and result.quality != "insufficient" and not job.token.cancelled:
            self.update(job, ProgressSnapshot(f"{stage_prefix}facilities", result.statistics.requests,
                                               result.statistics.network_requests, budget, time.monotonic()-job.started))
            business = await analyze_facilities(result, client, self.settings.baidu_map_ak.get_secret_value(), self.gate, job.token,
                deadline=job.started + 600)
        return result, business

    async def _run_osm(self, job, origin, budget):
        if self.osm_engine is None:
            raise RuntimeError("OSM cache unavailable")

        def progress(snapshot):
            self.update(job, ProgressSnapshot(f"osm_{snapshot.stage}", snapshot.requests,
                                               snapshot.network_requests, snapshot.budget, snapshot.elapsed_seconds))

        result = await self.osm_engine.run(origin, budget, job.token, progress)
        if self.osm_cache.near_boundary(origin) and "coverage_boundary" not in result.warnings:
            result.warnings.append("coverage_boundary")
            if result.quality == "usable":
                result.quality = "partial"
        return result

    @staticmethod
    def _branch(result, data_source, business_status):
        payload = result.to_dict()
        return HybridBranch(
            participated=True,
            data_source=data_source,
            business_status=business_status,
            isochrone=payload,
            warnings=payload["warnings"],
        )

    def _make_result(self, job, origin, primary, business, *, osm_result=None):
        payload = primary.to_dict()
        facilities_status = business[2].status if business else "not_integrated"
        primary_business_status = map_business_status(
            quality=payload["quality"],
            facilities_status=facilities_status,
            facilities=business[0] if business else None,
        )
        business_status = primary_business_status
        osm_status = None
        hybrid_result = None
        if osm_result is not None:
            osm_payload = osm_result.to_dict()
            osm_status = map_business_status(quality=osm_payload["quality"], facilities_status="not_integrated")
            if osm_status == "failed":
                business_status = "failed"
            hybrid_result = HybridResult(
                baidu=self._branch(primary, "synthetic" if self.settings.analysis_provider == "synthetic" else "baidu_walking", primary_business_status),
                osm=self._branch(osm_result, "osm_offline", osm_status),
                comparison=HybridComparison(notes=["百度与 OSM 双路计算，仅保留两套结果进行对比，不直接合并几何。"]),
            )

        if job.analysis_mode == AnalysisMode.OSM_OFFLINE:
            provenance = Provenance(osm=OsmProvenance(**self.osm_cache.provenance(participated=True)))
        elif job.analysis_mode == AnalysisMode.HYBRID:
            provenance = Provenance(
                osm=OsmProvenance(**self.osm_cache.provenance(participated=True)),
                baidu=SourceProvenance(
                    participated=True, availability="ready",
                    data_source="synthetic" if self.settings.analysis_provider == "synthetic" else "baidu_walking",
                ),
                strategy="parallel_comparison",
            )
        else:
            data_source = "synthetic" if self.settings.analysis_provider == "synthetic" else "baidu_walking"
            provenance = Provenance(baidu=SourceProvenance(participated=True, availability="ready", data_source=data_source))

        warnings = [Issue(code="ALGORITHM_WARNING", message=message, scope="isochrone") for message in payload["warnings"]]
        if osm_result is not None:
            warnings.append(Issue(code="HYBRID_COMPARISON", message="百度与 OSM 结果分别保留，未直接合并几何。", scope="hybrid"))
            warnings.extend(Issue(code="OSM_WARNING", message=message, scope="osm") for message in osm_result.to_dict()["warnings"])
        errors = ([Issue(code="INSUFFICIENT_EVIDENCE", message="没有足够步行证据生成等时圈。", scope="isochrone", severity="error")]
                   if business_status == "failed" else [])
        response = TaskResultResponse(
            task_id=job.task_id,
            task_status="completed",
            status=business_status,
            business_status=business_status,
            analysis_mode=job.analysis_mode,
            data_source=job.data_source,
            center={"lng": origin[0], "lat": origin[1]},
            generated_at=time.time(),
            facilities_status=facilities_status,
            facility_analysis=business[2] if business else None,
            rules=Rules(distance=DistanceRule(metric="walking_route", threshold_m=1000,
                inclusive=True, tolerance_m=100, assessment_scope="isochrone", category_policy="major_minor")),
            data=Data(geometry=payload["geometry"], uncertain_region=payload["uncertainRegion"],
                      unknown_region=payload["unknownRegion"], computation_extent=payload["computationExtent"]),
            algorithm=payload,
            warnings=warnings,
            errors=errors,
            provenance=provenance,
            hybrid_result=hybrid_result,
            isochrone=payload,
        ).model_dump(by_alias=True)
        if business:
            facilities, categories, evidence, report = business
            response["data"].update(
                facilities=[facility.model_dump() for facility in facilities],
                categories=[category.model_dump() for category in categories],
                report=report,
            )
            response["warnings"].extend(
                Issue(code="FACILITY_LIMITATION", message=message, scope="facilities").model_dump()
                for message in evidence.warnings
            )
        return response

    async def run(self, job):
        try:
            async with AsyncExitStack() as stack:
                origin, budget, _ = job.payload.fingerprint()
                if job.analysis_mode == AnalysisMode.OSM_OFFLINE:
                    result = await self._run_osm(job, origin, budget)
                    business = None
                    osm_result = None
                elif job.analysis_mode == AnalysisMode.HYBRID:
                    baidu_task = asyncio.create_task(self._run_baidu(job, origin, budget, stack, stage_prefix="baidu_"))
                    osm_task = asyncio.create_task(self._run_osm(job, origin, budget))
                    outcomes = await asyncio.gather(baidu_task, osm_task, return_exceptions=True)
                    if any(isinstance(outcome, BaseException) for outcome in outcomes):
                        raise RuntimeError("hybrid engine failure")
                    (result, business), osm_result = outcomes
                else:
                    result, business = await self._run_baidu(job, origin, budget, stack)
                    osm_result = None
            # Commit only after transport cleanup; cancellation during cleanup wins.
            if job.token.cancelled:
                job.status = "cancelled"
            elif result.stop_reason == "geometry_error" or (osm_result is not None and osm_result.stop_reason == "geometry_error"):
                job.status, job.error = "failed", "几何重建失败，请重试或检查采样证据"
            else:
                job.result = self._make_result(job, origin, result, business, osm_result=osm_result)
                job.status = "completed"
        except asyncio.CancelledError:
            job.token.cancel()
            job.status = "cancelled"
        except Exception:
            job.status = "cancelled" if job.token.cancelled else "failed"
            job.error = None if job.token.cancelled else "分析执行失败，请检查后端配置后重试"
        finally:
            job.finished_at = time.monotonic()
            self.prune()

    def cancel(self, job):
        if job.status not in TERMINAL:
            job.status = "cancelling"
            job.token.cancel()
        return job

    async def close(self):
        tasks = []
        for job in list(self.jobs.values()):
            self.cancel(job)
            if job.task:
                tasks.append(job.task)
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=10)
            for task in pending:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


def analysis_router(manager):
    router = APIRouter(prefix="/api/analyses", tags=["analyses"])

    @router.post("/{task_id}/routes/{facility_id}", response_model=RouteEvidence)
    async def facility_route(task_id: str, facility_id: str):
        job = manager.get(task_id)
        if job.analysis_mode == AnalysisMode.OSM_OFFLINE:
            raise HTTPException(409, "OSM 离线模式暂不支持设施路线")
        if job.status != "completed" or not job.result or not job.result.get("facilityAnalysis"):
            raise HTTPException(409, "设施结果尚未就绪")
        async with job.route_lock:
            evidence = job.result["facilityAnalysis"]
            if facility_id in evidence["routes"]:
                return evidence["routes"][facility_id]
            item = next((f for f in job.result["data"]["facilities"] if f["id"] == facility_id), None)
            if item is None:
                raise HTTPException(404, "设施不属于本次分析")
            if job.route_clicks >= 3:
                raise HTTPException(429, "本次分析的新增路线查询已达3次，请使用已有路线或重新分析")
            job.route_clicks += 1
            deadline = time.monotonic()+20
            origin = job.payload.fingerprint()[0]
            silence_transport_logs()
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
                provider = BaiduProvider(manager.settings.baidu_map_ak.get_secret_value(),client=client,destination_uid=facility_id,route_metric="distance")
                observed = await LimitedProvider(provider, manager.gate).query_walking_time(origin,(item["location"]["lng"],item["location"]["lat"]),deadline)
            value = RouteEvidence(distance_m=observed.distance_m,duration_s=observed.duration,endpoint_verified=observed.endpoint_verified,
                                  reason=observed.reason,path=observed.route_path if observed.endpoint_verified else []).model_dump()
            evidence["routes"][facility_id] = value
            evidence["network_requests"] += 1
            return value

    @router.post("", status_code=202, response_model=TaskStatusResponse)
    async def create(payload: AnalysisInput):
        return manager.create(payload).view()

    @router.get("/capabilities", response_model=AnalysisCapabilitiesResponse)
    async def capabilities():
        return manager.capabilities()

    @router.get("/{task_id}", response_model=TaskStatusResponse)
    async def status(task_id: str):
        return manager.get(task_id).view()

    @router.post("/by-request/{client_request_id}/cancel", status_code=202, response_model=TaskStatusResponse)
    async def cancel_by_request(client_request_id: str):
        # Recover a lost create response without replaying POST and starting new work.
        manager.prune()
        for job in manager.jobs.values():
            if job.payload.clientRequestId == client_request_id:
                return manager.cancel(job).view()
        raise HTTPException(404, "任务不存在或已过期")

    @router.get("/{task_id}/result", response_model=TaskResultResponse)
    async def result(task_id: str):
        job = manager.get(task_id)
        if job.status != "completed":
            raise HTTPException(409, "任务尚未完成或没有可用结果")
        return job.result

    @router.post("/{task_id}/cancel", status_code=202, response_model=TaskStatusResponse)
    async def cancel(task_id: str):
        return manager.cancel(manager.get(task_id)).view()

    return router
