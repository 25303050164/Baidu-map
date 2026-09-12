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


class Center(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    lng: float = Field(ge=-180, le=180, allow_inf_nan=False)
    lat: float = Field(gt=-85, lt=85, allow_inf_nan=False)


class AnalysisInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    center: Center
    coordinateSystem: Literal["bd09ll"]
    budget: int = 400
    clientRequestId: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")

    @field_validator("budget")
    @classmethod
    def budgets(cls, value):
        if value not in (200, 400, 800):
            raise ValueError("budget must be 200, 400 or 800")
        return value

    def fingerprint(self):
        return normalize((self.center.lng, self.center.lat)), self.budget


TERMINAL = {"completed", "cancelled", "failed"}


@dataclass
class Job:
    task_id: str
    payload: AnalysisInput
    data_source: str
    started: float = field(default_factory=time.monotonic)
    status: str = "running"
    token: CancelToken = field(default_factory=CancelToken)
    progress: ProgressSnapshot | None = None
    result: dict | None = None
    error: str | None = None
    finished_at: float | None = None
    task: asyncio.Task | None = None

    def view(self):
        return {"taskId": self.task_id, "status": self.status,
                "stage": self.status if self.status in TERMINAL or self.status == "cancelling" else self.progress.stage if self.progress else "initializing",
                "requests": self.progress.requests if self.progress else 0,
                "networkRequests": self.progress.network_requests if self.progress else 0,
                "budget": self.payload.budget,
                "elapsedSeconds": max(0, (self.finished_at or time.monotonic()) - self.started),
                "dataSource": self.data_source, "error": self.error}


class RateGate:
    """Shared across jobs; each actual attempt passes here, including retries."""
    def __init__(self, qps, *, clock=time.monotonic, sleep=asyncio.sleep):
        self.interval = 1 / qps if qps else 0
        self.next_send = 0
        self.lock = asyncio.Lock()
        self.clock, self.sleep = clock, sleep

    async def wait(self, deadline):
        async with self.lock:
            now = self.clock()
            when = max(now, self.next_send)
            if when >= deadline:
                return False
            await self.sleep(max(0, when - now))
            self.next_send = self.clock() + self.interval
            return True


class LimitedProvider:
    network = True

    def __init__(self, provider, gate):
        self.provider, self.gate = provider, gate
        self.identity = provider.identity

    async def query_walking_time(self, origin, destination, deadline):
        if not await self.gate.wait(deadline):
            return RouteObservation(destination, reason="deadline")
        return await self.provider.query_walking_time(origin, destination, deadline)


class AnalysisManager:
    def __init__(self, settings, provider_factory=None):
        self.settings, self.provider_factory = settings, provider_factory
        self.jobs = {}
        self.gate = RateGate(settings.analysis_qps)

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
        if self.settings.analysis_provider == "baidu" and not self.provider_factory:
            if not self.settings.ak_configured or self.settings.analysis_qps is None:
                raise HTTPException(503, "请在后端配置步行服务 AK 和 ANALYSIS_QPS")
            if 143 / self.settings.analysis_qps >= 600:
                raise HTTPException(503, "配置的 QPS 无法在截止时间内完成初始化")
        source = "synthetic" if self.settings.analysis_provider == "synthetic" else "baidu_walking"
        job = Job(str(uuid4()), payload, source)
        self.jobs[job.task_id] = job
        job.task = asyncio.create_task(self.run(job))
        return job

    def update(self, job, progress):
        if job.status == "running":
            job.progress = progress

    async def run(self, job):
        try:
            async with AsyncExitStack() as stack:
                origin, budget = job.payload.fingerprint()
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
                request = IsochroneRequest(origin, "bd09ll", budget=budget,
                    qps=self.settings.analysis_qps if provider.network else None)
                result = await compute_isochrone(request, provider, job.token, on_progress=lambda p: self.update(job, p))
            # Commit only after transport cleanup; cancellation during cleanup wins.
            if job.token.cancelled:
                job.status = "cancelled"
            elif result.stop_reason == "geometry_error":
                job.status, job.error = "failed", "几何重建失败，请重试或检查采样证据"
            else:
                job.result = {"taskId": job.task_id, "dataSource": job.data_source,
                    "center": {"lng": origin[0], "lat": origin[1]}, "generatedAt": time.time(),
                    "facilitiesStatus": "not_integrated", "isochrone": result.to_dict()}
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

    @router.post("", status_code=202)
    async def create(payload: AnalysisInput):
        return manager.create(payload).view()

    @router.get("/{task_id}")
    async def status(task_id: str):
        return manager.get(task_id).view()

    @router.get("/{task_id}/result")
    async def result(task_id: str):
        job = manager.get(task_id)
        if job.status != "completed":
            raise HTTPException(409, "任务尚未完成或没有可用结果")
        return job.result

    @router.post("/{task_id}/cancel", status_code=202)
    async def cancel(task_id: str):
        return manager.cancel(manager.get(task_id)).view()

    return router
