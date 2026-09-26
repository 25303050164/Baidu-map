"""Sequential checkup orchestration over the explicit engine registry.

One task runs at a time. The shared account-level gate already paces every
route attempt, and the first release deliberately does not add in-flight
concurrency before its own timing acceptance. A second request is queued
rather than rejected.
"""
import asyncio
import time
from uuid import uuid4

from life_circle.coordinates import normalize
from life_circle.models import CancelToken

from ..contracts import Issue
from ..engines import EngineContext, IsochroneAsk, canonical_hash
from .models import (DISTANCE_RULE, TERMINAL, CheckupRequest, CheckupSnapshot, CheckupTaskView,
                     EngineRef, ScopeEvidence, new_trace)
from .store import CheckupStore, RequestIdConflict, TaskNotFound

# Engines currently enforce their own internal deadline; this is the task-level
# bound the later stages will share.
DEADLINE_SECONDS = 1800
LATER_STAGES_NOTICE = "设施检索、服务覆盖与报告阶段尚未接入，本修订只包含成圈结果。"


class CheckupError(Exception):
    """Wire error carrying the v2 status and code."""

    def __init__(self, status_code: int, code: str, message: str):
        self.status_code, self.code, self.message = status_code, code, message
        super().__init__(code)


def resolve_budget(capabilities, requested: int | None) -> int:
    """Validate the tier against the named engine. No tier is shared, and the
    other algorithm is never entered as a substitute."""
    budget = capabilities.default_budget if requested is None else requested
    if budget not in capabilities.budgets:
        raise CheckupError(422, "checkup_unsupported_budget",
                           f"engine {capabilities.engine_id} supports budgets "
                           f"{list(capabilities.budgets)}")
    return budget


def business_status_for(quality: str) -> str:
    """Facilities and area scoring are not integrated yet, so never ``complete``."""
    return "insufficient" if quality == "insufficient" else "partial"


class CheckupManager:
    def __init__(self, settings, registry, store: CheckupStore):
        self.settings, self.registry, self.store = settings, registry, store
        self.tokens: dict[str, CancelToken] = {}
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.worker: asyncio.Task | None = None
        self.closing = False

    # -- admission ---------------------------------------------------------

    def start(self) -> None:
        """Start the single worker on first use, so no lifecycle hook is required."""
        if self.worker is None or self.worker.done():
            self.closing = False
            self.worker = asyncio.create_task(self._serve())

    def submit(self, payload: CheckupRequest) -> tuple[CheckupTaskView, bool]:
        engine = self.registry.get(payload.engine)
        capabilities = engine.capabilities()
        budget = resolve_budget(capabilities, payload.isochrone.budget)
        try:
            record, created = self.store.create(
                task_id=str(uuid4()), client_request_id=payload.client_request_id,
                engine=payload.engine, fingerprint=canonical_hash(payload.fingerprint()),
                payload=payload.model_dump(mode="json"), budget=budget)
        except RequestIdConflict:
            raise CheckupError(409, "checkup_request_id_conflict",
                               "该请求标识已用于不同参数的体检") from None
        if created:
            self.queue.put_nowait(record.task_id)
            self.start()
        return self.view(record), created

    # -- execution ---------------------------------------------------------

    async def _serve(self) -> None:
        while True:
            task_id = await self.queue.get()
            try:
                if not self.closing:
                    await self._run(task_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                # A task must reach a terminal state even when orchestration
                # itself fails; the reason is recorded, never the raw exception.
                self._finish(task_id, status="failed", error="orchestration_failed")
            finally:
                self.queue.task_done()

    async def _run(self, task_id: str) -> None:
        if not self.store.claim(task_id):
            return
        record = self.store.get(task_id)
        payload = CheckupRequest(**record.payload)
        token = CancelToken()
        self.tokens[task_id] = token
        origin = normalize((payload.center.lng, payload.center.lat))
        context = EngineContext(
            task_id=task_id, token=token, deadline=time.monotonic() + DEADLINE_SECONDS,
            artifact_dir=self.store.artifact_dir(task_id),
            on_progress=lambda snapshot: self.store.update(
                task_id, requests=snapshot.requests, network_requests=snapshot.network_requests))
        try:
            snapshot = await self.registry.get(record.engine).compute(
                IsochroneAsk(origin=origin, budget=record.budget), context)
        except asyncio.CancelledError:
            self._finish(task_id, status="cancelled")
            raise
        except Exception:
            self._finish(task_id, status="failed", error="engine_execution_failed")
            return
        finally:
            self.tokens.pop(task_id, None)
        self._publish(task_id, payload, snapshot, cancelled=token.cancelled)

    def _publish(self, task_id: str, payload: CheckupRequest, snapshot, *, cancelled: bool) -> None:
        """Freeze the isochrone revision even when the run is then cancelled."""
        record = self.store.get(task_id)
        revision = record.revision + 1
        business = business_status_for(snapshot.quality)
        # The embedded engine snapshot uses the same wire naming as the rest of
        # the document, so a stored revision round-trips without translation.
        isochrone = snapshot.model_dump(mode="json", by_alias=True)
        # The result hash covers the boundary, the rules and the (not yet
        # integrated) facility and analysis groups, so it can never be mistaken
        # for the boundary-only isochrone hash.
        result_hash = canonical_hash({
            "isochrone": isochrone, "rules": DISTANCE_RULE.model_dump(mode="json"),
            "facilities": None, "analysis": None})
        warnings = [Issue(code="ALGORITHM_WARNING", message=message, scope="isochrone")
                    for message in snapshot.warnings]
        warnings.append(Issue(code="STAGES_NOT_INTEGRATED", message=LATER_STAGES_NOTICE,
                              scope="checkup", severity="pending"))
        document = CheckupSnapshot(
            task_id=task_id, revision=revision, generated_at=time.time(), center=payload.center,
            stage="isochrone", business_status=business,
            engine=EngineRef(engine_id=snapshot.engine_id, engine_version=snapshot.engine_version,
                             label=self.registry.get(snapshot.engine_id).capabilities().label),
            isochrone=isochrone, rules=DISTANCE_RULE,
            scope=ScopeEvidence(projection=self.settings.osm_metric_crs,
                                data_version=self.settings.osm_data_version,
                                coverage_supported=False,
                                notes=["面积覆盖与灰区需要服务路网，尚未接入"]),
            trace=new_trace(isochrone_hash=snapshot.isochrone_hash, result_hash=result_hash,
                            data_versions={"osm": self.settings.osm_data_version,
                                           "engine": snapshot.engine_version},
                            budgets={"isochrone": record.budget}),
            facilities_status="not_integrated", warnings=warnings)
        # Published files use wire naming, so a stored revision round-trips back
        # into the response model without a translation step.
        published = self.store.publish(task_id, stage="isochrone",
                                       snapshot=document.model_dump(mode="json", by_alias=True),
                                       result_hash=result_hash)
        if published != revision:
            raise RuntimeError("revision counter diverged")
        # The engine consumed what the progress callback last reported; the
        # frozen snapshot is the authoritative count for the finished stage.
        self.store.update(task_id, requests=snapshot.requests_used,
                          network_requests=snapshot.network_requests)
        self._finish(task_id, status="cancelled" if cancelled else "completed",
                     business_status=business)

    def _finish(self, task_id: str, *, status: str, business_status: str | None = None,
                error: str | None = None) -> None:
        self.store.update(task_id, status=status, finished_at=time.time(),
                          business_status=business_status, error=error)

    # -- reads and cancellation -------------------------------------------

    def view(self, record) -> CheckupTaskView:
        if record.started_at is None:
            elapsed = 0.0
        else:
            end = record.finished_at if record.finished_at is not None else time.monotonic()
            elapsed = max(0.0, end - record.started_at)
        return CheckupTaskView(
            task_id=record.task_id, client_request_id=record.client_request_id,
            engine=record.engine, status=record.status,
            business_status=record.business_status, stage=record.stage,
            revision=record.revision, budget=record.budget, requests=record.requests,
            network_requests=record.network_requests, elapsed_seconds=elapsed,
            created_at=record.created_at, cancel_requested=record.cancel_requested,
            error=record.error)

    def get(self, task_id: str):
        try:
            return self.store.get(task_id)
        except TaskNotFound:
            raise CheckupError(404, "checkup_task_not_found", "任务不存在或已过期") from None

    def by_request(self, client_request_id: str):
        try:
            return self.store.by_request(client_request_id)
        except TaskNotFound:
            raise CheckupError(404, "checkup_request_not_found",
                               "该请求标识没有可用任务") from None

    def cancel(self, task_id: str):
        record = self.get(task_id)
        if record.status in TERMINAL:
            return self.view(record), False
        self.store.update(task_id, cancel_requested=True)
        if record.status == "queued":
            self.store.update(task_id, status="cancelled", finished_at=time.time())
        else:
            self.store.update(task_id, status="cancelling")
            token = self.tokens.get(task_id)
            if token is not None:
                token.cancel()
        return self.view(self.store.get(task_id)), True

    async def close(self) -> None:
        self.closing = True
        for token in list(self.tokens.values()):
            token.cancel()
        if self.worker is not None and not self.worker.done():
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
