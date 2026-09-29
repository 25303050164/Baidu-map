"""Sequential checkup orchestration over the explicit engine registry.

One task runs at a time. The shared account-level gate already paces every
route attempt, and the first release deliberately does not add in-flight
concurrency before its own timing acceptance. A second request is queued
rather than rejected.

``quota`` is the application's one allocation entry. A stage takes its attempts
from a pool built here for the task, so nothing can spend outside the ledger and
a task's own pools stay separate from every other task's.

A run publishes one immutable revision per stage: the boundary first, then the
facility retrieval over it, then the service-accessibility assessment, the
verification and finally the report. Each revision is a complete document, so a
reader never has to join two files to see what was established, and the result
hash of the later revision covers every group it repeats.
"""
import asyncio
import json
import math
import time
from contextlib import AsyncExitStack
from uuid import uuid4

from life_circle.coordinates import LocalProjection, normalize
from life_circle.models import CancelToken

from ..accessibility.grid import AssessmentCancelled
from ..algorithms.osm_offline.lazy import resolve
from ..cache import KeyedCache
from ..catalog import major_of
from ..contracts import Issue, Origin
from ..engines import EngineContext, IsochroneAsk, IsochroneSnapshot, canonical_hash
from ..engines.protocol import EngineCancelled
from ..poi.planner import RULES as POI_RULES
from ..poi_evidence import poi_evidence
from .accessibility_stage import AccessibilityOutcome, assess_accessibility
from .facilities import FacilityOutcome, collect_facilities, stale_for
from .models import (DETAIL_ROUTE_REQUESTS, DISTANCE_RULE, RULE_VERSION, TERMINAL, CheckupRequest,
                     CheckupSnapshot, CheckupTaskView, EngineRef, FacilityGroup, FacilityRoute,
                     ReportEvidence, ScopeEvidence, TaskProgress, new_trace)
from .progress import StepReporter, category_label
from .reporting_stage import build_report
from .routes import DETAIL_POOL, RoutesUnavailable, open_online as open_routes
from .store import CheckupStore, RequestIdConflict, TaskNotFound
from .verification_stage import (VerificationOutcome, carried_over, refusal as verification_refusal,
                                 usable_route, verify_facilities, within_rule)

# Engines currently enforce their own internal deadline; this is the task-level
# bound every stage shares.
DEADLINE_SECONDS = 1800
ENGINE_UNAVAILABLE = {
    "walking_ak_not_configured": "后端未配置百度步行服务 AK，百度边界搜索（E8.2）无法成圈。",
}
LATER_STAGES_NOTICE = "服务覆盖、灰区与报告阶段尚未接入，本修订只包含成圈结果与设施检索。"
#: The accessibility revision runs before verification, so its grey zones are
#: model-only *in that revision*; the next revision carries the route evidence.
VERIFICATION_NOTICE = "本修订尚未包含现实核验：这一版的灰区只有模型证据，核验阶段在其后发布。"
#: 修订文档里的空间分析组。摘要覆盖的正是这一组，不多不少。
ANALYSIS_FIELDS = ("accessibility", "service_gaps", "heatmap", "scores", "verification", "report")
#: Published with the analysis group and covered by its digest, but only when
#: present: revisions frozen before water reviews existed keep their identity.
OPTIONAL_ANALYSIS_FIELDS = ("water",)
#: 按任务保存的详情桶上限。它记在进程内：保护账户的是持久账本（每次尝试都先预约），
#: 重启或淘汰之后这个任务的详情额度从 20 重新计起，账本上的消耗不会被抹掉。任务记录的
#: 累计计数写的是整趟流水线的用量，点击详情不改写它 —— 让它和正在跑的阶段互相覆盖，
#: 只会把两边都算错。
DETAIL_BUCKETS = 256


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


def business_status_for(quality: str, facilities_status: str | None = None,
                        analysis_status: str | None = None) -> str:
    """How much of the checkup this revision can stand behind.

    Never ``complete``: the catalogue is never independently verified (§4.4), so
    even a run where every stage succeeded is ``partial`` — what it retrieved is
    not the same claim as what exists. A boundary the engine itself called
    unusable is ``insufficient``, and so is a facility stage or an assessment
    that could not run at all: without either, the revision cannot answer the
    question the checkup exists for, whatever the boundary is worth. A group that
    merely has gaps in it is not by itself ``insufficient`` — that is ``partial``,
    and the stage that is incomplete says which one it is.
    """
    if quality == "insufficient":
        return "insufficient"
    if facilities_status == "failed":
        return "insufficient"
    if analysis_status == "failed":
        return "insufficient"
    return "partial"


def _analysis_document(analysis: dict | None) -> dict | None:
    """The analysis group in its published form; the digest covers exactly this.

    ``None`` for a group that was never established, so a revision that never ran
    the assessment is not identified as one that ran it and found nothing.
    """
    if not analysis or all(analysis.get(field) is None for field in ANALYSIS_FIELDS):
        return None
    return {field: (None if analysis.get(field) is None
                    else analysis[field].model_dump(mode="json", by_alias=True))
            for field in ANALYSIS_FIELDS + OPTIONAL_ANALYSIS_FIELDS
            if field in ANALYSIS_FIELDS or analysis.get(field) is not None}


def _data_versions(osm: str, engine: str, analysis: dict | None) -> dict:
    """Every dataset a revision was computed from. ``waterReviews`` is listed
    even when empty once the water evidence exists, so "no review applied here"
    and "computed before reviews existed" read differently."""
    versions = {"osm": osm, "engine": engine}
    water = (analysis or {}).get("water")
    if water is not None:
        versions["waterReviews"] = [review["label"] for review in water.reviews]
    return versions


def _analysis_status(analysis: dict | None) -> str:
    evidence = (analysis or {}).get("accessibility")
    return "not_integrated" if evidence is None else evidence.status


class _SpentBudget:
    """A finished task's pools as its last revision froze them: a recompute spends none."""

    def __init__(self, state: dict):
        self._state = state

    def state(self) -> dict:
        return json.loads(json.dumps(self._state))


def _point(value) -> Origin | None:
    """一个观测端点，按契约的对象形式返回；没有就是 None。"""
    if value is None:
        return None
    lng, lat = value
    return Origin(lng=lng, lat=lat)


def _with_token(session, token):
    """Hand the task's cancel token to a transport session that checks it per attempt.

    Set as an attribute so injected transports keep their plain signature.
    """
    if token is not None:
        try:
            session.token = token
        except AttributeError:
            pass
    return session


def _assessment_progress(report):
    """Translate the assessment's own step callbacks into stored progress.

    The assessment runs in a worker thread; the store opens its own connection
    per write, so writing from there is safe. Cell ticks are throttled by the
    reporter; every step change is written at once.
    """
    def progress(step: str, *, major: str | None = None, index: int | None = None,
                 total: int | None = None, cells: int | None = None) -> None:
        if step == "category":
            report("category", key=major, count=cells, label=category_label(major, index, total),
                   throttle=bool(cells))
        else:
            report(step)
    return progress


class CheckupManager:
    def __init__(self, settings, registry, store: CheckupStore, quota, place_factory=None,
                 route_factory=None, offline=None):
        self.settings, self.registry, self.store, self.quota = settings, registry, store, quota
        # §3.4: a page is reusable across tasks only inside the configured window,
        # and inside its own task when no window is configured. One cache for the
        # process, outside every quota pool, so a hit costs no attempt.
        self.cache = KeyedCache(freshness_seconds=settings.cache_freshness_seconds)
        # The seam a deployment without a key, or an offline run, substitutes at.
        # ``route_factory`` is the same seam for the walking routes the
        # verification stage and the facility-detail endpoint ask for.
        self.place_factory = place_factory
        self.route_factory = route_factory
        # The walking graph the assessment runs on. It is not engine-specific: a
        # boundary computed by either algorithm is assessed against the same OSM
        # network, so the two engines differ in how the circle was drawn, not in
        # what "within 1000 m on foot" means.
        self.offline = offline
        # 详情桶按任务保存，点击设施的路线与核验阶段共享同一个任务的额度。
        self.detail_budgets: dict[str, object] = {}
        self.tokens: dict[str, CancelToken] = {}
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.worker: asyncio.Task | None = None
        self.closing = False

    # -- admission ---------------------------------------------------------

    def interrupt_unfinished(self) -> int:
        """Sweep tasks this process did not finish, as a restart does.

        Called once when the server starts serving, never from a constructor:
        an interrupted task stops being "running" because a new deployment took
        over, and merely building the app object is not that.
        """
        return self.store.interrupt_unfinished()

    def start(self) -> None:
        """Start the single worker on first use, so no lifecycle hook is required."""
        if self.worker is None or self.worker.done():
            self.closing = False
            self.worker = asyncio.create_task(self._serve())

    def submit(self, payload: CheckupRequest) -> tuple[CheckupTaskView, bool]:
        engine = self.registry.get(payload.engine)
        capabilities = engine.capabilities()
        budget = resolve_budget(capabilities, payload.isochrone.budget)
        # A deployment that cannot run the engine at all says so before a task
        # exists, instead of admitting one that can only fail without a reason.
        reason = getattr(engine, "unavailable_reason", lambda: None)()
        if reason is not None:
            raise CheckupError(503, "checkup_engine_unavailable", ENGINE_UNAVAILABLE.get(reason, reason))
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
        try:
            await self._stages(task_id, payload, record, token)
        finally:
            self.tokens.pop(task_id, None)

    async def _stages(self, task_id: str, payload: CheckupRequest, record, token) -> None:
        origin = normalize((payload.center.lng, payload.center.lat))
        # One budget for the whole task, decided at claim time: every pool is
        # fixed before the first attempt, so a stage can only spend its own
        # allowance and none of them can be widened by a later stage (§9.2).
        budget = self.quota.task_budget(
            isochrone=record.budget, poi=payload.facilities.max_poi_requests,
            route=payload.facilities.max_route_requests, detail=DETAIL_ROUTE_REQUESTS)
        # 这一趟的桶和点击详情用的是同一个对象，所以"一个任务 20 次详情"是共享的一个
        # 额度，而不是各记一本账。
        self._remember_budget(task_id, budget)
        # Every write of an in-stage step goes through this one reporter, so the
        # step's own clock survives the counter ticks that follow it.
        report = StepReporter(lambda **fields: self.store.update(task_id, **fields))
        context = EngineContext(
            task_id=task_id, token=token, deadline=time.monotonic() + DEADLINE_SECONDS,
            artifact_dir=self.store.artifact_dir(task_id),
            # The engine names its own sub-stage; the counts are its attempts
            # against the tier and what of them actually went to the network.
            on_progress=lambda snapshot: report(
                snapshot.stage, count=snapshot.requests, limit=snapshot.budget,
                requests=snapshot.requests, network_requests=snapshot.network_requests),
            # Its preparation counts edges or nothing: the task counters stay as they are.
            on_step=report)
        try:
            snapshot = await self.registry.get(record.engine).compute(
                IsochroneAsk(origin=origin, budget=record.budget), context)
        except EngineCancelled:
            # The user's cancel stopped the engine: the task ends, the worker serves on.
            self._finish(task_id, status="cancelled")
            return
        except asyncio.CancelledError:
            # The asyncio task itself is being cancelled (shutdown): never swallowed.
            self._finish(task_id, status="cancelled")
            raise
        except Exception:
            self._finish(task_id, status="failed", error="engine_execution_failed")
            return
        # The boundary revision is frozen even when the run is then cancelled:
        # an orderly stop never discards a result that has been paid for.
        self._publish_isochrone(task_id, payload, snapshot, budget)
        if token.cancelled:
            self._finish(task_id, status="cancelled",
                         business_status=business_status_for(snapshot.quality, "not_integrated"))
            return
        # The stage lasts as long as its queries do. The task counters follow the
        # pool as each attempt is reserved -- the boundary's own network count
        # plus what this stage has sent so far -- and the publish below writes
        # the stage's final account over them.
        self.store.update(task_id, stage="poi")
        boundary_network = snapshot.network_requests
        outcome = await collect_facilities(
            payload, snapshot, settings=self.settings, context=context, quota=self.quota,
            budget=budget, cache=self.cache, places_factory=self.place_factory,
            progress=lambda sent, limit: report(
                "places", count=sent, limit=limit, requests=boundary_network + sent,
                network_requests=boundary_network + sent))
        business = self._publish_facilities(task_id, payload, snapshot, budget, outcome)
        if token.cancelled:
            self._finish(task_id, status="cancelled", business_status=business)
            return
        try:
            assessment = await self._publish_accessibility(task_id, payload, snapshot, budget, outcome,
                                                           report=report, token=token)
        except AssessmentCancelled:
            self._finish(task_id, status="cancelled", business_status=business)
            return
        if token.cancelled:
            self._finish(task_id, status="cancelled", business_status=business)
            return
        # §6.3 verification asks for real routes before the report is assembled,
        # so the report can say what was verified and what stayed model-only.
        verification = await self._publish_verification(
            task_id, payload, snapshot, budget, outcome, assessment, deadline=context.deadline,
            report=report, token=token)
        if token.cancelled:
            self._finish(task_id, status="cancelled", business_status=business)
            return
        # §7.2 reporting repeats the assessment rather than recomputing it.
        self.store.update(task_id, stage="reporting")
        report("report")
        # Assembling and writing the report takes a second or more; off the event
        # loop, so status polls are answered meanwhile.
        business = await asyncio.to_thread(self._publish_reporting, task_id, payload, snapshot, budget,
                                           outcome, assessment, verification)
        self._finish(task_id, status="completed", business_status=business)
        self.store.update(task_id, stage="ready")

    # -- revisions ---------------------------------------------------------

    def _budgets(self, budget, snapshot) -> dict:
        """Every pool of the task and what each has spent so far.

        The isochrone entry reports what the engine counted **on the network**:
        both algorithms still pace on the legacy shared gate (§9.1) and stop on
        their own tier, so their attempts are not drawn from this counter. What
        the pool is for is bounding paid usage, and a run whose provider never
        leaves the process -- the synthetic transport, and tests -- spends none
        of it, however many boundary samples it computed locally.
        """
        state = budget.state()
        state["isochrone"]["spent"] = snapshot.network_requests
        return state

    def _base(self, task_id: str, payload: CheckupRequest, snapshot, *, revision: int, stage: str,
              business: str, budgets: dict, result_hash: str, warnings: list,
              facilities: dict | None, facilities_status: str, analysis: dict | None = None,
              extra_rules: dict | None = None, recomputed: dict | None = None) -> dict:
        """The part of a revision that does not depend on which stage published it.

        ``analysis`` is the group of spatial objects this revision carries; the
        digest covers exactly the same objects, so a group cannot be published
        under a digest that would not change with it.

        Whatever is still outstanding is appended here rather than by each stage,
        so a revision cannot forget to say which part of the checkup it does not
        contain — that notice is the difference between "nothing is missing" and
        "nobody looked".
        """
        analysis = analysis or {}
        document = _analysis_document(analysis)
        warnings = list(warnings) + self._pending_warnings(document, analysis)
        trace = new_trace(isochrone_hash=snapshot.isochrone_hash, result_hash=result_hash,
                          data_versions=_data_versions(self.settings.osm_data_version,
                                                       snapshot.engine_version, analysis),
                          budgets=budgets, extra_rule_versions=extra_rules)
        if recomputed is not None:
            trace = trace.model_copy(update={"recomputed": recomputed})
        return dict(
            task_id=task_id, revision=revision, generated_at=time.time(), center=payload.center,
            stage=stage, business_status=business,
            engine=EngineRef(engine_id=snapshot.engine_id, engine_version=snapshot.engine_version,
                             label=self.registry.get(snapshot.engine_id).capabilities().label),
            rules=DISTANCE_RULE,
            scope=ScopeEvidence(
                projection=self.settings.osm_metric_crs,
                data_version=self.settings.osm_data_version,
                coverage_supported=document is not None,
                assessment_domain_available=bool(
                    (analysis.get("accessibility") is not None
                     and analysis["accessibility"].domain_area_m2 is not None)),
                excluded_area_m2=None if analysis.get("accessibility") is None
                    else analysis["accessibility"].excluded_area_m2,
                model_support_available=bool(
                    analysis.get("accessibility") is not None
                    and analysis["accessibility"].status != "failed"),
                notes=self._pending_notices(document, analysis)),
            trace=trace,
            facilities=facilities, facilities_status=facilities_status,
            accessibility_status=_analysis_status(analysis), warnings=warnings,
            **{field: analysis.get(field) for field in ANALYSIS_FIELDS + OPTIONAL_ANALYSIS_FIELDS})

    def _pending_notices(self, document: dict | None, analysis: dict | None) -> list:
        """What this revision does not contain, in the reader's words.

        The analysis group absent means the assessment never ran at all; present
        without verification means it ran and every gap in it is model-only. Both
        are limitations of *this* revision, so both belong in its scope notes.
        """
        if document is None:
            return [LATER_STAGES_NOTICE]
        verification = (analysis or {}).get("verification")
        # 具名拒绝（这个部署没有路线服务）仍然是一次缺席：它带着原因发布在自己的字段里，
        # 但这一版确实没有现实核验，所以那条提示照发。
        if verification is None or getattr(verification, "status", None) == "not_integrated":
            return [VERIFICATION_NOTICE]
        return []

    def _pending_warnings(self, document: dict | None, analysis: dict | None) -> list:
        return [Issue(code="STAGES_NOT_INTEGRATED", message=notice, scope="checkup",
                      severity="pending")
                for notice in self._pending_notices(document, analysis)]

    def _result_hash(self, isochrone: dict, facilities: dict | None, *, facilities_status: str,
                     analysis: dict | None = None) -> str:
        """The full result digest, never the boundary-only one.

        It covers the boundary, the rules, the facility stage and the whole
        analysis group, so a document can never be re-identified after a group it
        repeats is changed, and it is owned by the store as the revision's identity.

        The stage's status belongs in the digest, not only its group: a stage
        that could not run is null exactly as a stage that never ran is, and the
        two revisions would otherwise share one identity while saying different
        things about the facility retrieval.
        """
        return canonical_hash({"isochrone": isochrone,
                               "rules": DISTANCE_RULE.model_dump(mode="json"),
                               "facilitiesStatus": facilities_status,
                               "facilities": facilities,
                               "analysis": _analysis_document(analysis)})

    def _common_warnings(self, snapshot) -> list:
        return [Issue(code="ALGORITHM_WARNING", message=message, scope="isochrone")
                for message in snapshot.warnings]

    def _publish_isochrone(self, task_id: str, payload: CheckupRequest, snapshot, budget) -> None:
        revision = self.store.get(task_id).revision + 1
        # The embedded engine snapshot uses the same wire naming as the rest of
        # the document, so a stored revision round-trips without translation.
        isochrone = snapshot.model_dump(mode="json", by_alias=True)
        result_hash = self._result_hash(isochrone, None, facilities_status="not_integrated")
        warnings = self._common_warnings(snapshot)
        document = CheckupSnapshot(
            isochrone=isochrone,
            **self._base(task_id, payload, snapshot, revision=revision, stage="isochrone",
                         business=business_status_for(snapshot.quality),
                         budgets=self._budgets(budget, snapshot), result_hash=result_hash,
                         warnings=warnings, facilities=None, facilities_status="not_integrated"))
        self._publish(task_id, "isochrone", document, revision, result_hash)
        # The engine consumed what the progress callback last reported; the
        # frozen snapshot is the authoritative count for the finished stage.
        # 两列都记网络尝试：本任务"已用多少次"说的是花了多少付费额度，而合成与离线路径
        # 一次也没发出去。成圈自己算过的边界采样数在 snapshot.statistics 里，不会丢。
        self.store.update(task_id, requests=snapshot.network_requests,
                          network_requests=snapshot.network_requests)

    def _publish_facilities(self, task_id: str, payload: CheckupRequest, snapshot, budget,
                            outcome: FacilityOutcome) -> str:
        """Freeze the facility revision, whatever the stage established."""
        revision = self.store.get(task_id).revision + 1
        isochrone = snapshot.model_dump(mode="json", by_alias=True)
        facilities = (outcome.group.model_dump(mode="json", by_alias=True)
                      if outcome.group is not None else None)
        result_hash = self._result_hash(isochrone, facilities,
                                        facilities_status=outcome.status)
        business = business_status_for(snapshot.quality, outcome.status)
        warnings = self._common_warnings(snapshot) + list(outcome.issues)
        document = CheckupSnapshot(
            isochrone=isochrone,
            **self._base(task_id, payload, snapshot, revision=revision, stage="poi",
                         business=business, budgets=self._budgets(budget, snapshot),
                         result_hash=result_hash, warnings=warnings, facilities=facilities,
                         facilities_status=outcome.status,
                         extra_rules={"classification": POI_RULES["version"]}))
        self._publish(task_id, "poi", document, revision, result_hash)
        # Cumulative across the stages: the boundary's attempts were counted
        # before this stage drew from the same budget object. 只累加网络尝试，
        # 与上一阶段的写法一致 —— 否则这一列会在本阶段发布时突然跳一大截。
        self.store.update(task_id, requests=snapshot.network_requests + outcome.requests,
                          network_requests=snapshot.network_requests + outcome.network_requests)
        return business

    def _analysis_objects(self, assessment: AccessibilityOutcome, report=None,
                          verification: VerificationOutcome | None = None) -> dict:
        """The analysis group's objects, keyed by their revision field names.

        The accessibility objects are repeated in every later revision rather
        than rebuilt, so the report and the verification describe exactly the
        assessment that was frozen — not a second computation of it.
        """
        return {"accessibility": assessment.accessibility, "service_gaps": assessment.service_gaps,
                "heatmap": assessment.heatmap, "scores": assessment.scores,
                "verification": None if verification is None else verification.evidence,
                "report": report, "water": assessment.water}

    async def _publish_accessibility(self, task_id: str, payload: CheckupRequest, snapshot,
                                     budget, outcome: FacilityOutcome, *,
                                     report=None, token=None) -> AccessibilityOutcome:
        """Run and freeze the accessibility assessment (§5–§7.1).

        The graph it needs is the deployment's OSM store, not the engine's: a
        boundary from either algorithm is assessed against the same walking
        network. A deployment without a graph publishes the same revision with a
        named refusal in it, never an empty 0% — a revision that says "not
        assessed" and one that says "nothing is covered" must not look alike.
        """
        self.store.update(task_id, stage="accessibility")
        group = (None if outcome.group is None
                 else outcome.group.model_dump(mode="json", by_alias=True))
        if report is not None and self.offline is not None:
            report("graph")
        resolved = await self._resolve_offline(report)
        inner = None if report is None else _assessment_progress(report)

        def progress(step, **detail):
            # Called for every step and every cell: the cancel checkpoint of the assessment.
            if token is not None and token.cancelled:
                raise AssessmentCancelled()
            if inner is not None:
                inner(step, **detail)
        assessment = await asyncio.to_thread(
            assess_accessibility, geometry=snapshot.geometry,
            unknown_region=snapshot.unknown_region,
            facilities=None if outcome.group is None else outcome.group.facilities,
            query_status=outcome.status, majors=tuple(payload.facilities.categories),
            store=None if resolved is None else resolved.store,
            coverage=None if resolved is None else resolved.coverage,
            version=self.settings.osm_data_version, settings=self.settings,
            progress=progress)
        objects = self._analysis_objects(assessment)
        revision = self.store.get(task_id).revision + 1
        isochrone = snapshot.model_dump(mode="json", by_alias=True)
        result_hash = self._result_hash(isochrone, group, facilities_status=outcome.status,
                                        analysis=objects)
        self._publish(task_id, "accessibility", CheckupSnapshot(
            isochrone=isochrone,
            **self._base(task_id, payload, snapshot, revision=revision, stage="accessibility",
                         business=business_status_for(snapshot.quality, outcome.status,
                                                      assessment.status),
                         budgets=self._budgets(budget, snapshot), result_hash=result_hash,
                         warnings=self._common_warnings(snapshot) + list(outcome.issues)
                         + list(assessment.issues),
                         facilities=group, facilities_status=outcome.status, analysis=objects,
                         extra_rules={"classification": POI_RULES["version"],
                                      "accessibility": RULE_VERSION})), revision,
            result_hash)
        return assessment

    async def _publish_verification(self, task_id: str, payload: CheckupRequest, snapshot, budget,
                                    outcome: FacilityOutcome, assessment: AccessibilityOutcome,
                                    *, deadline: float, report=None, token=None) -> VerificationOutcome:
        """Run and freeze the route verification (§6.3).

        A deployment without a walking-route service publishes the same revision
        with a named refusal in it: the grey zones then stand on model evidence
        alone, and the report says that in those words instead of leaving the
        section out. Nothing here rewrites the assessment — a route that
        disagrees with a cell flags it for refinement, and one route never
        re-verdicts a whole cell.
        """
        self.store.update(task_id, stage="verification")
        before = self.store.get(task_id)
        progress = None if report is None else (
            lambda done, candidates, attempts: report(
                "routes", count=done, limit=candidates, requests=before.requests + attempts,
                network_requests=before.network_requests + attempts))
        heatmap = (None if assessment.heatmap is None
                   else assessment.heatmap.model_dump(mode="json", by_alias=True))
        gaps = (None if assessment.service_gaps is None else assessment.service_gaps.zones)
        async with AsyncExitStack() as stack:
            try:
                routes = (self.route_factory(self.settings) if self.route_factory is not None
                          else await open_routes(self.settings, stack))
            except RoutesUnavailable as exc:
                result = verification_refusal(exc.reason)
            else:
                # The session takes the task's route bucket from the direction
                # pool, so verification, both engines and the click-detail route
                # all pass the one scheduling point of their service (§9.2).
                result = await verify_facilities(
                    facilities=None if outcome.group is None else outcome.group.facilities,
                    majors=tuple(payload.facilities.categories),
                    # 冻结的灰区是模型，而阶段读的是普通字典（和 facilities 一样）：
                    # 转换摆在边界上，阶段里就不会出现"模型还是字典"的两套读法。
                    zones=None if gaps is None else [zone.model_dump(mode="json", by_alias=True)
                                                     for zone in gaps],
                    heatmap=heatmap, entrances=assessment.entrances,
                    origin=normalize((payload.center.lng, payload.center.lat)),
                    session=_with_token(routes.session(self.quota.direction, budget=budget,
                                                       deadline=deadline), token),
                    progress=progress, token=token)
        self._freeze_verification(task_id, payload, snapshot, budget, outcome, assessment, result)
        # 核验阶段发的是真实路线请求，所以它也计入任务自己的请求数 —— 任务视图说
        # "本次体检发出了多少请求"，少算这一阶段就等于少报了一百多次调用。运行中的
        # 计数已经随每家候选写过，这里按阶段开始时的底数写定终值，不会重复累加。
        self.store.update(task_id, requests=before.requests + result.network_requests,
                          network_requests=before.network_requests + result.network_requests)
        return result

    def _freeze_verification(self, task_id: str, payload: CheckupRequest, snapshot, budget,
                             outcome: FacilityOutcome, assessment: AccessibilityOutcome,
                             result: VerificationOutcome, *, recomputed: dict | None = None) -> None:
        group = (None if outcome.group is None
                 else outcome.group.model_dump(mode="json", by_alias=True))
        objects = self._analysis_objects(assessment, verification=result)
        revision = self.store.get(task_id).revision + 1
        isochrone = snapshot.model_dump(mode="json", by_alias=True)
        result_hash = self._result_hash(isochrone, group, facilities_status=outcome.status,
                                        analysis=objects)
        self._publish(task_id, "verification", CheckupSnapshot(
            isochrone=isochrone,
            **self._base(task_id, payload, snapshot, revision=revision, stage="verification",
                         business=business_status_for(snapshot.quality, outcome.status,
                                                      assessment.status),
                         budgets=self._budgets(budget, snapshot), result_hash=result_hash,
                         warnings=self._common_warnings(snapshot) + list(outcome.issues)
                         + list(assessment.issues) + list(result.issues),
                         facilities=group, facilities_status=outcome.status, analysis=objects,
                         extra_rules={"classification": POI_RULES["version"],
                                      "accessibility": RULE_VERSION},
                         recomputed=recomputed)), revision,
            result_hash)

    def _publish_reporting(self, task_id: str, payload: CheckupRequest, snapshot, budget,
                           outcome: FacilityOutcome, assessment: AccessibilityOutcome,
                           verification: VerificationOutcome | None = None, *,
                           recomputed: dict | None = None) -> str:
        """Freeze the report revision: the same assessment, assembled for readers."""
        revision = self.store.get(task_id).revision + 1
        isochrone = snapshot.model_dump(mode="json", by_alias=True)
        group = (None if outcome.group is None
                 else outcome.group.model_dump(mode="json", by_alias=True))
        dump = lambda item: None if item is None else item.model_dump(mode="json", by_alias=True)
        evidence = assessment.accessibility
        report = build_report(
            task_id=task_id, revision=revision,
            # 报告汇总的是它自己所在那一版之前的结论；写进自己那一版的摘要会成环。
            source_result_hash=self.store.revision(task_id)["result_hash"],
            generated_at=time.time(),
            domain=None if evidence is None else evidence.domain,
            domain_area_m2=None if evidence is None else evidence.domain_area_m2,
            accessibility=dump(evidence), service_gaps=dump(assessment.service_gaps),
            heatmap=dump(assessment.heatmap), scores=dump(assessment.scores),
            facilities=group,
            verification=None if verification is None or verification.evidence is None
                         else verification.evidence.model_dump(mode="json", by_alias=True),
            water=dump(assessment.water))
        objects = self._analysis_objects(assessment, report, verification)
        result_hash = self._result_hash(isochrone, group, facilities_status=outcome.status,
                                        analysis=objects)
        business = business_status_for(snapshot.quality, outcome.status, assessment.status)
        self._publish(task_id, "reporting", CheckupSnapshot(
            isochrone=isochrone,
            **self._base(task_id, payload, snapshot, revision=revision, stage="reporting",
                         business=business, budgets=self._budgets(budget, snapshot),
                         result_hash=result_hash,
                         warnings=self._common_warnings(snapshot) + list(outcome.issues)
                         + list(assessment.issues),
                         facilities=group, facilities_status=outcome.status, analysis=objects,
                         extra_rules={"classification": POI_RULES["version"],
                                      "accessibility": RULE_VERSION},
                         recomputed=recomputed)), revision,
            result_hash)
        return business

    # -- recompute after a data correction ---------------------------------

    async def recompute(self, task_id: str, *, reason: str) -> list[int]:
        """Republish a finished task from its paid evidence after a data correction.

        Nothing is asked again and the original budgets stand as spent:

        * the boundary is rebuilt from the engine's stored samples when the
          engine reads the corrected data (Hybrid replays its ledger against the
          current obstacle layer), and kept as frozen when it does not;
        * the facility retrieval is carried over, but only while the new
          boundary leaves every record on the side it was counted on -- anything
          else needs a new retrieval, which is a new task, not a recompute;
        * the assessment is computed afresh, the verification routes are carried
          over with their entrance layer re-read, and the report is rebuilt.

        It publishes the verification and reporting revisions a run would, both
        marked ``trace.recomputed``, so the report still summarizes the revision
        before it and a reader can tell which revisions predate the correction.
        """
        record = self.get(task_id)
        if record.status not in TERMINAL or task_id in self.tokens:
            raise CheckupError(409, "checkup_task_running", "任务仍在运行，不能重算")
        previous, _ = self.snapshot(task_id)
        if previous.stage != "reporting" or previous.facilities is None:
            raise CheckupError(409, "checkup_recompute_needs_report", "只有已出报告的任务可以重算")
        payload = CheckupRequest(**record.payload)
        origin = normalize((payload.center.lng, payload.center.lat))
        frozen = IsochroneSnapshot(**previous.isochrone)
        engine = self.registry.get(record.engine)
        ledger_name = getattr(engine, "ledger_name", None)
        if ledger_name is None:
            # The engine does not read the obstacle layer: its boundary stands.
            snapshot, boundary = frozen, "unchanged"
        else:
            ledger = json.loads((self.store.artifact_dir(task_id) / ledger_name)
                                .read_text(encoding="utf-8"))
            context = EngineContext(task_id=task_id, token=CancelToken(),
                                    deadline=time.monotonic() + DEADLINE_SECONDS)
            snapshot = await engine.compute(IsochroneAsk(origin=origin, budget=record.budget),
                                            context, replay=ledger)
            # The samples are the original run's paid attempts; the replay itself
            # sent none, which ``trace.recomputed`` says.
            snapshot = snapshot.model_copy(update={
                "network_requests": frozen.network_requests,
                "statistics": {**snapshot.statistics, "networkRequests": frozen.network_requests,
                               "replayedFromLedger": True}})
            boundary = "replayed_from_ledger"
        group = previous.facilities
        stale = stale_for(group, frozen.geometry, snapshot.geometry, origin)
        if stale is not None:
            raise CheckupError(409, "checkup_recompute_needs_retrieval",
                               f"新边界与原设施检索不再一致（{stale}），需要新任务重新检索")
        outcome = FacilityOutcome(group=group, status=previous.facilities_status,
                                  issues=[issue for issue in previous.warnings
                                          if issue.scope == "facilities"])
        resolved = await self._resolve_offline()
        assessment = await asyncio.to_thread(
            assess_accessibility, geometry=snapshot.geometry,
            unknown_region=snapshot.unknown_region, facilities=group.facilities,
            query_status=outcome.status, majors=tuple(payload.facilities.categories),
            store=None if resolved is None else resolved.store,
            coverage=None if resolved is None else resolved.coverage,
            version=self.settings.osm_data_version, settings=self.settings)
        verification = (None if previous.verification is None
                        else carried_over(previous.verification, entrances=assessment.entrances,
                                          revision=previous.revision))
        recomputed = {"fromRevision": previous.revision, "reason": reason, "networkRequests": 0,
                      "boundary": boundary, "carriedOver": ["facilities", "verificationRoutes"],
                      "previousIsochroneHash": frozen.isochrone_hash,
                      "previousResultHash": previous.trace.result_hash}
        budget = _SpentBudget(previous.trace.budgets)
        published = []
        if verification is not None:
            self._freeze_verification(task_id, payload, snapshot, budget, outcome, assessment,
                                      verification, recomputed=recomputed)
            published.append(self.store.get(task_id).revision)
        business = self._publish_reporting(task_id, payload, snapshot, budget, outcome,
                                           assessment, verification, recomputed=recomputed)
        published.append(self.store.get(task_id).revision)
        self.store.update(task_id, stage="ready", business_status=business)
        return published

    async def _resolve_offline(self, report=None):
        """Resolve the walking graph off the event loop; None when there is none.

        A load in progress is told to ``report`` step by step, its edges counted.
        """
        if self.offline is None:
            return None
        try:
            return await resolve(self.offline, None if report is None else (
                lambda step, done, total: report(step, count=done, limit=total)))
        except Exception:
            # A graph that fails to load is the same answer as a deployment that
            # has none: the assessment says why, and the rest of the revision
            # still reports what the other stages established.
            return None

    def _publish(self, task_id: str, stage: str, document: CheckupSnapshot, revision: int,
                 result_hash: str) -> None:
        # Published files use wire naming, so a stored revision round-trips back
        # into the response model without a translation step.
        published = self.store.publish(task_id, stage=stage,
                                       snapshot=document.model_dump(mode="json", by_alias=True),
                                       result_hash=result_hash)
        if published != revision:
            raise RuntimeError("revision counter diverged")

    def _finish(self, task_id: str, *, status: str, business_status: str | None = None,
                error: str | None = None) -> None:
        """Close the task out without erasing what a revision already established."""
        fields = {"status": status, "finished_at": time.time()}
        if business_status is not None:
            fields["business_status"] = business_status
        if error is not None:
            fields["error"] = error
        self.store.update(task_id, **fields)

    # -- facility routes ----------------------------------------------------

    def _remember_budget(self, task_id: str, budget):
        """The task's own pools, kept for the click-detail route, oldest evicted."""
        if len(self.detail_budgets) >= DETAIL_BUCKETS and task_id not in self.detail_budgets:
            self.detail_budgets.pop(next(iter(self.detail_budgets)))
        self.detail_budgets[task_id] = budget
        return budget

    def detail_budget(self, task_id: str, record):
        """This task's detail pool, rebuilt for a task whose run is long gone."""
        budget = self.detail_budgets.get(task_id)
        if budget is None:
            budget = self.quota.task_budget(isochrone=record.budget, detail=DETAIL_ROUTE_REQUESTS)
            self._remember_budget(task_id, budget)
        return budget

    async def detail_route(self, task_id: str, facility_id: str) -> FacilityRoute:
        """§3.3 点击设施路线：一条独立详情证据，不改变任何已发布的结论。

        它走的是核验阶段同一条闸门 —— 同一个任务的详情桶、同一个 DIRECTION 服务池、
        同一个调度点 —— 所以点击既不能绕过配额，也不会把某次点击读成"评分变了"。
        想让新证据改变结论，必须重新评估并发布新修订。
        """
        snapshot, stored = self.snapshot(task_id)
        if snapshot.facilities is None:
            raise CheckupError(409, "checkup_facilities_not_ready", "设施结果尚未就绪")
        item = next((row for row in snapshot.facilities.facilities if row["id"] == facility_id),
                    None)
        if item is None:
            raise CheckupError(404, "checkup_facility_not_found", "该设施不在本次体检结果中")
        record = self.get(task_id)
        payload = CheckupRequest(**record.payload)
        origin = normalize((payload.center.lng, payload.center.lat))
        destination = normalize((item["location"]["lng"], item["location"]["lat"]))
        budget = self.detail_budget(task_id, record)
        deadline = time.monotonic() + DEADLINE_SECONDS
        async with AsyncExitStack() as stack:
            try:
                routes = (self.route_factory(self.settings) if self.route_factory is not None
                          else await open_routes(self.settings, stack))
            except RoutesUnavailable:
                raise CheckupError(409, "checkup_route_unavailable",
                                   "本部署没有可用的步行路线服务") from None
            session = routes.session(self.quota.direction, budget=budget, deadline=deadline)
            observation = await session(facility_id, origin, destination, pool=DETAIL_POOL)
            provider, attempts, network = session.identity, session.attempts, bool(routes.network)
            stopped = session.stop_reason
        if observation is None:
            # 没有结论的原因是具名的：额度用完和"路走不通"是两件事。
            if stopped == "task_budget_exhausted":
                raise CheckupError(429, "checkup_detail_budget_exhausted",
                                   f"本任务的详情路线额度已用完（{DETAIL_ROUTE_REQUESTS} 次）")
            raise CheckupError(409, "checkup_route_unavailable",
                               f"本次没有取到路线结论（{stopped or 'unknown'}）")
        strict = poi_evidence(observation, origin, destination, item["id"])
        # "可用"沿用核验阶段同一套：端点核实过、路线有结果、严格映射成立。端点对不上的
        # 那一条只进道路端点证据层，既不判覆盖，也不冒充严格证据。
        usable = usable_route(observation, destination) and strict.status != "pending"
        distance = observation.distance_m if usable else None
        returned = observation.distance_m if usable_route(observation, destination) else None
        x, y = LocalProjection(origin).to_local(destination)
        return FacilityRoute(
            task_id=task_id, revision=stored["revision"], facility_id=item["id"],
            category=item["category"], major_category=major_of(item["category"]),
            origin=Origin(lng=origin[0], lat=origin[1]),
            destination=Origin(lng=destination[0], lat=destination[1]),
            straight_line_m=round(math.hypot(x, y), 3), within_rule=within_rule(distance),
            route_distance_m=None if returned is None else round(returned, 3),
            duration_s=observation.duration, observed_duration_s=observation.observed_duration,
            poi_status=strict.status, poi_reason=strict.reason,
            evidence_grade="verified" if strict.status != "pending" else "model",
            route_origin=_point(observation.route_origin),
            route_destination=_point(observation.route_destination),
            origin_offset_m=observation.origin_offset_m,
            destination_offset_m=observation.destination_offset_m,
            reason=observation.reason, provider=provider, network=network, attempts=attempts,
            budget={"pool": DETAIL_POOL, "limit": budget.pools()[DETAIL_POOL],
                    "spent": budget.spent.get(DETAIL_POOL, 0),
                    "remaining": budget.remaining(DETAIL_POOL)},
            notes=["这条详情证据不改变已发布的评分：它只回答这一次点击，"
                   "要让新证据影响结论必须重新评估并生成新修订。",
                   "判定用的是返回的路线距离；直线距离只用来排序和保守筛选。"])

    # -- reads and cancellation -------------------------------------------

    def snapshot(self, task_id: str, revision: int | None = None):
        """The newest published revision, or a named not-ready refusal."""
        self.get(task_id)
        stored = self.store.revision(task_id, revision)
        if stored is None:
            raise CheckupError(409, "checkup_result_not_ready", "尚无可用结果快照")
        # Re-validated from the stored mapping so a hand-edited file cannot widen
        # the contract; serialization always goes through the model.
        return CheckupSnapshot(**stored["snapshot"]), stored

    def view(self, record) -> CheckupTaskView:
        # Every stored time is the wall clock (``time.time()``); a running task
        # is measured against the same clock, never a monotonic one.
        now = time.time()
        if record.started_at is None:
            elapsed = 0.0
        else:
            end = record.finished_at if record.finished_at is not None else now
            elapsed = max(0.0, end - record.started_at)
        return CheckupTaskView(
            task_id=record.task_id, client_request_id=record.client_request_id,
            engine=record.engine, status=record.status,
            business_status=record.business_status, stage=record.stage,
            revision=record.revision, budget=record.budget, requests=record.requests,
            network_requests=record.network_requests, elapsed_seconds=elapsed,
            created_at=record.created_at, cancel_requested=record.cancel_requested,
            error=record.error, server_time=now, started_at=record.started_at,
            finished_at=record.finished_at, stage_started_at=record.stage_started_at,
            last_activity_at=record.activity_at,
            progress=None if record.progress is None else TaskProgress(**record.progress))

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
        # The request is the client's, not the worker's: it does not count as
        # server activity. A queued task is closed here, which is its last event.
        self.store.update(task_id, cancel_requested=True, activity=False)
        if record.status == "queued":
            self.store.update(task_id, status="cancelled", finished_at=time.time())
        else:
            self.store.update(task_id, status="cancelling", activity=False)
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
