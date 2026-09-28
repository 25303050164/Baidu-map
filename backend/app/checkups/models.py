"""Versioned checkup wire contract.

Separate from the legacy ``/api/analyses`` and ``/api/v1/analysis/hybrid``
contracts. Nothing here is added to the generated demo contract, so the strict
POI evidence types keep their existing serialization byte-for-byte.
"""
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

from ..accessibility.grid import GRID_STEP_M, MAX_LEAF_CELLS, REFINED_STEP_M
from ..accessibility.service_graph import SEARCH_CUTOFF_M
from ..accessibility.zones import COMPOSITE_MIN_CATEGORIES, MIN_LABEL_AREA_M2
from ..contracts import Issue, MajorCategory, Origin
from ..facilities import RULE as DISTANCE_RULE
from ..rules import DistanceRule

SCHEMA_VERSION = "checkup-v1"
RULE_VERSION = "walk-distance-1000-v1"

TaskStatus = Literal["queued", "running", "cancelling", "completed", "failed", "cancelled"]
BusinessStatus = Literal["complete", "partial", "insufficient"]
Stage = Literal["isochrone", "poi", "accessibility", "verification", "reporting", "ready"]

# Fixed first-release budgets. A request may lower them, never raise them.
DEFAULT_POI_REQUESTS = 60
DEFAULT_ROUTE_REQUESTS = 120
MAX_POI_REQUESTS = 60
MAX_ROUTE_REQUESTS = 120
DETAIL_ROUTE_REQUESTS = 20

# The facility query domain is the computed boundary expanded by this margin in
# the metric plane; it is an engineering allowance, not evidence of a complete
# directory. Declared here so no request can widen it.
QUERY_PADDING_M = 1300

TERMINAL: frozenset[str] = frozenset({"completed", "failed", "cancelled"})


class CheckupModel(BaseModel):
    """Camel-case wire naming, like the rest of the project's HTTP contracts."""
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, populate_by_name=True,
                              alias_generator=to_camel)


class CheckupIsochrone(CheckupModel):
    # The supported tiers are per engine; the registry rejects the rest with 422.
    budget: int | None = Field(default=None, ge=1, le=800)


class CheckupFacilities(CheckupModel):
    categories: tuple[MajorCategory, ...] = ("shopping", "medical", "education")
    max_poi_requests: int = Field(default=DEFAULT_POI_REQUESTS, ge=1, le=MAX_POI_REQUESTS)
    max_route_requests: int = Field(default=DEFAULT_ROUTE_REQUESTS, ge=1, le=MAX_ROUTE_REQUESTS)

    @model_validator(mode="after")
    def unique_categories(self):
        if len(set(self.categories)) != len(self.categories):
            raise ValueError("duplicate facility category")
        if not self.categories:
            raise ValueError("at least one facility category is required")
        return self


class CheckupRequest(CheckupModel):
    schema_version: Literal["checkup-v1"] = SCHEMA_VERSION
    client_request_id: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")
    engine: str
    center: Origin
    coordinate_system: Literal["bd09ll"] = "bd09ll"
    isochrone: CheckupIsochrone = Field(default_factory=CheckupIsochrone)
    facilities: CheckupFacilities = Field(default_factory=CheckupFacilities)

    def fingerprint(self) -> dict:
        """Identity-free input digest; the request id names, it does not configure."""
        return self.model_dump(mode="json", exclude={"client_request_id"})


class EngineRef(CheckupModel):
    engine_id: str
    engine_version: str
    label: str


class ScopeEvidence(CheckupModel):
    """What this revision's analysis covers, and what it had to leave out.

    ``coverage_supported`` says the spatial analysis group is *present* in this
    revision; whether that analysis could reach a conclusion is
    ``accessibility_status``, and whether a domain was frozen at all is
    ``assessment_domain_available``. A revision can carry the group and still
    report nothing: those are different facts, so they are different fields.
    """
    projection: str
    data_version: str
    coverage_supported: bool
    assessment_domain_available: bool = False
    excluded_area_m2: float | None = None
    model_support_available: bool = False
    notes: list[str] = Field(default_factory=list)


class TraceEvidence(CheckupModel):
    data_versions: dict
    rule_versions: dict
    isochrone_hash: str
    result_hash: str
    budgets: dict
    #: 数据修订后离线重算出的一版：从哪一版来、为什么重算。重算不发网络请求，
    #: ``budgets`` 仍是原任务花掉的额度。
    recomputed: dict | None = None


class FacilityGroup(CheckupModel):
    """§4.4's two quality axes, reported side by side and never conflated.

    ``query_status`` is what the planned queries and their spatial range
    established; ``catalog_completeness`` is whether the real directory was
    independently verified, which a keyword search can never establish, so it is
    ``unverified`` here by construction and not by omission.

    ``counts_by_category`` is what the run *retrieved* inside the computed
    boundary, keyed by the request's major categories. A zero is an observed
    zero: it must be read with ``query_status``, never as "none exist" (§4.4).
    """
    query_status: Literal["completed", "partial", "failed", "cancelled"]
    catalog_completeness: Literal["unverified"] = "unverified"
    provider: str
    api_version: str
    data_source: Literal["baidu_place", "synthetic"]
    query_domain: dict
    data_obtained_at: float | None = None
    counts_by_category: dict[str, int] = Field(default_factory=dict)
    facilities: list[dict] = Field(default_factory=list)
    review_candidates: list[dict] = Field(default_factory=list)
    excluded_candidates: list[dict] = Field(default_factory=list)
    quarantine: list[dict] = Field(default_factory=list)
    query_coverage: list[dict] = Field(default_factory=list)
    statistics: dict = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    stop_reason: str | None = None


class CategoryCoverage(CheckupModel):
    """一类在冻结评估域内的三态面积（§7.1）。

    面积只在这里出现一次：:class:`ScoreEvidence` 报的是由这些面积算出的百分比，
    不再复制一份面积。``C + G + U`` 与 ``domain_area_m2`` 必须相符，不符的类别按
    ``supported=False`` 报出 ``unavailable_reason``，绝不归一化后照常给分。
    """
    category: str
    supported: bool
    covered_m2: float = 0.0
    gap_m2: float = 0.0
    unknown_m2: float = 0.0
    domain_area_m2: float | None = None
    unavailable_reason: str | None = None
    #: 叶格统计：细分是否到顶、每态各多少格。``refined`` 为细分成 25 米的父格数。
    cells: dict = Field(default_factory=dict)
    entrances: dict = Field(default_factory=dict)
    #: 该类别的模型可达面（bd09ll 几何）：供 ``accessibility`` 图层绘制。它是模型
    #: 估计的展示面，面积结论一律来自网格。
    coverage: dict | None = None


class AccessibilityEvidence(CheckupModel):
    """§5–§6 的服务可达性结果：评估域、网格参数与逐类别的三态面积。

    ``domain_area_m2`` 是冻结的分母 A。``excluded_area_m2`` 是被数据集覆盖范围
    裁掉的面积 —— 那是"不在结论范围内"，不是"没有服务"。
    """
    status: Literal["complete", "partial", "failed"] = "failed"
    domain: dict | None = None
    domain_area_m2: float | None = None
    excluded_area_m2: float | None = None
    grid_step_m: float = GRID_STEP_M
    refined_step_m: float = REFINED_STEP_M
    max_leaf_cells: int = MAX_LEAF_CELLS
    search_cutoff_m: float = SEARCH_CUTOFF_M
    views: dict = Field(default_factory=dict)
    categories: list[CategoryCoverage] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class ServiceZone(CheckupModel):
    """一片灰区在冻结修订里的样子（§6.5）。

    ``zones`` 曾经是裸 dict，于是"这一片有多大、为什么是灰区、建议查什么"只存在于
    ``Zone.as_dict()`` 的写法里：写错了没有一处会报错，读的人也没有类型可依。把它写成
    模型之后，冻结出来的清单自带校验，前端契约也从同一份定义生成。

    两份几何必须来自同一个对象：``geometry`` 是**统计几何**（米制，面积以它为准），
    ``displayGeometry`` 是同一对象的 bd09ll 形态。展示简化改变统计几何是 §6.5 明令禁止的。
    """
    id: str
    index: int = 0
    categories: list[str] = Field(default_factory=list)
    kind: str
    area_m2: float
    parts: int = 0
    cell_ids: list[str] = Field(default_factory=list)
    label_visible: bool = False
    suspected: bool = True
    evidence_grade: str = "model"
    query_status: str = "complete"
    nearest_facility: str | None = None
    reason: str | None = None
    suggestion: str
    geometry: dict | None = None
    geometry_system: Literal["metric"] = "metric"
    display_geometry: dict | None = None


class ServiceGapsEvidence(CheckupModel):
    """§6.5 的灰区清单：连通分量、面积与逐区的解释。

    ``by_category_m2`` 是每类灰区的并集面积；``composite_area_m2`` 是"三大类至少
    两类为 gap"的并集面积。两者都不与逐区面积相加混用。
    """
    status: Literal["complete", "partial", "failed"] = "failed"
    zones: list[ServiceZone] = Field(default_factory=list)
    by_category_m2: dict[str, float] = Field(default_factory=dict)
    composite_area_m2: float = 0.0
    gap_area_m2: float = 0.0
    composite_min_categories: int = COMPOSITE_MIN_CATEGORIES
    min_label_area_m2: float = MIN_LABEL_AREA_M2
    obstacle_layer_available: bool = False
    notes: list[str] = Field(default_factory=list)


class HeatmapEvidence(CheckupModel):
    """设施密度热力的数据面：每格到最近设施模型距离（§5.4）。

    这是**模型估计**：它只回答"路网上走 1000 米能到哪里"，不是实测、不是设施数量、
    也不是人口覆盖。没有模型路径的格不出现在热力里 —— 它们的结论由灰区层与未知面积
    表达，不在这里被画成一个"距离为零"的热点。
    """
    metric: Literal["walking_route"] = "walking_route"
    estimated: Literal[True] = True
    step_m: float = GRID_STEP_M
    domain: dict | None = None
    categories: dict[str, list[dict]] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


class WaterDataEvidence(CheckupModel):
    """这一版的水体障碍到底是什么：数据来源、版本、复核过的范围与仍然冲突的地方。

    热力与灰区把水体当障碍用；底图上画的水面并不参与计算。两者不一致时，读者需要
    知道计算用的是哪一份、哪一片被独立证据核对过 —— 否则底图上的一片"水"下面画着
    覆盖，看起来就像"水面上有服务"。``conflict_area_m2`` 是评估域里来源互相矛盾、
    又没有证据裁决的面积：那里的格一律是"数据冲突／未知"，不是覆盖，也不是灰区。

    ``reviews`` 按复核文件原样带出（几何已转 bd09ll），范围之外的水系是 OSM 原样，
    ``unreviewed_area_m2`` 说的就是这一部分。
    """
    obstacle_layer_available: bool = False
    osm_data_version: str | None = None
    source_pbf_sha256: str | None = None
    reviews: list[dict] = Field(default_factory=list)
    rejected_reviews: list[str] = Field(default_factory=list)
    domain_area_m2: float | None = None
    reviewed_area_m2: float = 0.0
    unreviewed_area_m2: float | None = None
    conflict_area_m2: float = 0.0
    statements: list[str] = Field(default_factory=list)


class ScoreRow(CheckupModel):
    """一个类别的区间分。``supported`` 为假时百分比全是 null —— "没有空间支持"和
    "支持了但算出来是 0%"必须能分辨。"""
    category: str
    supported: bool | None = None
    coverage_lower_pct: float | None = None
    coverage_upper_pct: float | None = None
    assessable_pct: float | None = None
    unknown_pct: float | None = None
    interval_width_pct: float | None = None
    interval_degenerate: bool | None = None
    unavailable_reason: str | None = None


class OverallScore(CheckupModel):
    """:func:`overall_score` 的两种结果写在同一个模型里：给得出区间，或者给不出。

    ``available`` 是唯一的分支开关，``reason`` 给不出时说明是哪一类（类别不齐、缺空间
    支持）；其余字段在两个分支里都是可空的，界面因此不需要按分支换类型。
    """
    available: bool
    reason: str | None = None
    missing_categories: list[str] = Field(default_factory=list)
    coverage_lower_pct: float | None = None
    coverage_upper_pct: float | None = None
    assessable_pct: float | None = None
    unknown_pct: float | None = None
    categories: list[str] = Field(default_factory=list)
    weights: dict[str, float] = Field(default_factory=dict)


class ScoreEvidence(CheckupModel):
    """§7.1 的覆盖区间分：固定分母、上下界与可评估率。

    只报百分比与总体分，不重复 :class:`AccessibilityEvidence` 里的面积。总体分在
    三大类不齐备或有一类没有空间支持时是"无法给出"，而不是把已分析的类别重新加权。
    """
    rule_version: str = RULE_VERSION
    domain_area_m2: float | None = None
    categories: list[ScoreRow] = Field(default_factory=list)
    overall: OverallScore | None = None
    formula: dict = Field(default_factory=dict)


class CoverageRow(ScoreRow):
    """报告里的一行：区间分加上它赖以计算的面积与格数。

    继承 :class:`ScoreRow` 不是省字，而是写明"报告的每一行就是一个类别的分数行，附上
    这些分数是从哪块面积算出来的"。合并成一个模型之后，报告里同类别的数字只可能出现
    一次 —— 面积与分数分栏写，读者就只能自己把两栏对起来。
    """
    covered_m2: float | None = None
    gap_m2: float | None = None
    unknown_m2: float | None = None
    cells: dict = Field(default_factory=dict)
    entrances: dict = Field(default_factory=dict)
    evidence_grade: str = "model"


class ReportGaps(CheckupModel):
    """报告的灰区栏目：既可以是完整清单，也可以是"这一阶段没跑"的具名缺席。

    缺席时 ``status`` 是 ``failed`` 且 ``reason`` 有话说，``zones`` 为空 —— 省略这一栏
    在读者看来就是"没有灰区问题"，这比一份带原因的缺席错得多。
    """
    status: Literal["complete", "partial", "failed"] = "failed"
    reason: str | None = None
    zones: list[ServiceZone] = Field(default_factory=list)
    by_category_m2: dict[str, float] = Field(default_factory=dict)
    composite_area_m2: float = 0.0
    gap_area_m2: float = 0.0
    min_label_area_m2: float = MIN_LABEL_AREA_M2
    composite_min_categories: int = COMPOSITE_MIN_CATEGORIES
    obstacle_layer_available: bool = False
    notes: list[str] = Field(default_factory=list)


class ReportVerification(CheckupModel):
    """报告的核验栏目。``available`` 与 ``status`` 是两件事：这个部署有没有核验服务
    （available），以及核验跑到什么程度（status）。把 ``not_integrated`` 读成
    ``available: True`` 会让"没有核验"看起来像"核验过、没发现问题"。"""
    available: bool
    status: str
    provider: str | None = None
    checked: int = 0
    failed: int = 0
    unresolved: int = 0
    facilities: list[dict] = Field(default_factory=list)
    conflicts: list[dict] = Field(default_factory=list)
    queries: dict = Field(default_factory=dict)
    reason: str | None = None
    notes: list[str] = Field(default_factory=list)


class ReportQuality(CheckupModel):
    """证据质量栏：这份结论建立在什么之上，以及这一版缺了什么。"""
    schema_version: str = SCHEMA_VERSION
    rule_version: str = RULE_VERSION
    source_result_hash: str
    views: dict | None = None
    grid: dict = Field(default_factory=dict)
    excluded_area_m2: float | None = None
    obstacle_layer_available: bool | None = None
    heatmap_estimated: bool | None = None
    catalog_completeness: str = "unverified"
    query_status: str | None = None
    notes: list[str] = Field(default_factory=list)


class ReportEvidence(CheckupModel):
    """§7.2 报告快照：一份可离线复核的完整结论。

    ``limitations`` 是这个报告不能回答的问题（目录完整性、人口覆盖、设施容量、
    政策准入、与建设选址的关系）。它随报告一起冻结，不能由界面自行省略。
    """
    report_id: str
    generated_at: float
    schema_version: Literal["checkup-v1"] = SCHEMA_VERSION
    domain: dict | None = None
    domain_area_m2: float | None = None
    categories: list[CoverageRow] = Field(default_factory=list)
    overall: OverallScore | None = None
    # 三栏都是必填：一份"漏了核验栏"的报告与一份"核验缺席"的报告必须长得不一样，
    # 前者是组装错误，后者要带着原因出现在读者面前。
    gaps: ReportGaps
    verification: ReportVerification
    evidence: ReportQuality
    #: 数据来源栏：水体障碍来自哪一份数据、哪一份复核、复核覆盖了多少。几何不在报告里，
    #: 在同一版修订的 ``water`` 栏。早于水系复核的报告没有这一栏。
    data_sources: dict | None = None
    limitations: list[str] = Field(default_factory=list)


class VerificationEvidence(CheckupModel):
    """§6.3 的百度核验阶段：能核实的与核实不了的都留下名字。

    ``checked`` 是真实发起过路线核验的设施；``unresolved`` 是核验后仍然接入不了的
    入口；``conflicts`` 是"模型说覆盖、路线说走不通"这类分歧所在的那几格。三者都不是
    "目录已完整"的证据 —— 目录完整性永远是 ``unverified``。
    """
    status: Literal["complete", "partial", "failed", "not_integrated"] = "not_integrated"
    provider: str | None = None
    checked: int = 0
    failed: int = 0
    unresolved: int = 0
    facilities: list[dict] = Field(default_factory=list)
    #: 模型结论与路线证据对不上的格子。带着这条标记的格是"待细化"，不是"已改判"：
    #: 一条路线只说明这条路线的两端，改判整格需要重新评估（新修订）。
    conflicts: list[dict] = Field(default_factory=list)
    queries: dict = Field(default_factory=dict)
    reason: str | None = None
    notes: list[str] = Field(default_factory=list)


class CheckupSnapshot(CheckupModel):
    """One immutable stage revision. Published content is never rewritten."""
    schema_version: Literal["checkup-v1"] = SCHEMA_VERSION
    task_id: str
    revision: int = Field(ge=1)
    generated_at: float
    center: Origin
    coordinate_system: Literal["bd09ll"] = "bd09ll"
    stage: Stage
    business_status: BusinessStatus
    engine: EngineRef
    isochrone: dict
    # The rule body keeps ``DistanceRule``'s own field names: it is the same
    # object the legacy contracts and the assessment use, and one rule must not
    # be described two ways. Everything else in this document is camelCase.
    rules: DistanceRule
    scope: ScopeEvidence
    trace: TraceEvidence
    # Absent is not the same as empty: null means the facility stage could not
    # establish a result at all (no key, no boundary), an empty group means it
    # ran and retrieved nothing. ``facilities_status`` describes the *query*
    # axis only — "complete" there never means the catalogue is verified.
    facilities: FacilityGroup | None = None
    facilities_status: Literal["not_integrated", "complete", "partial", "failed"]
    # The accessibility group is the same rule: null means the assessment could
    # not run (no OSM graph, no boundary, no facilities to attach), an object
    # with no usable category means it ran and could establish nothing.
    accessibility: AccessibilityEvidence | None = None
    accessibility_status: Literal["not_integrated", "complete", "partial", "failed"] = \
        "not_integrated"
    service_gaps: ServiceGapsEvidence | None = None
    heatmap: HeatmapEvidence | None = None
    scores: ScoreEvidence | None = None
    verification: VerificationEvidence | None = None
    report: ReportEvidence | None = None
    #: 水体障碍的来源与复核范围；早于水系复核的修订没有这一栏（null）。
    water: WaterDataEvidence | None = None
    warnings: list[Issue] = Field(default_factory=list)


class CheckupTaskView(CheckupModel):
    task_id: str
    client_request_id: str
    engine: str
    status: TaskStatus
    business_status: BusinessStatus | None = None
    stage: Stage | None = None
    revision: int = Field(default=0, ge=0)
    budget: int
    requests: int = Field(default=0, ge=0)
    network_requests: int = Field(default=0, ge=0)
    elapsed_seconds: float = Field(default=0, ge=0)
    created_at: float
    cancel_requested: bool = False
    error: str | None = None

    def is_terminal(self) -> bool:
        return self.status in TERMINAL


class CheckupLayer(CheckupModel):
    layer_id: str
    revision: int = Field(ge=1)
    geometry: dict | None
    display_geometry: dict | None = None
    # A layer is usually a shape. The report is a document, and it is served from
    # this endpoint too so every published group of a revision is addressable the
    # same way; it fills this field and leaves both geometry fields null, which is
    # how a client tells the two kinds apart.
    document: dict | None = None
    result_hash: str


class FacilityRoute(CheckupModel):
    """§3.3 点击设施路线：一条独立详情证据，不改变任何已发布的结论。

    ``revision`` 是要它回答的那一版，不是它产生的版本：详情证据不发布新修订，所以
    ``withinRule`` 说的是"这条路线怎么走"，不是"评分变成了什么"。要让新的证据改变
    结论，必须重新评估并生成新修订。
    """
    task_id: str
    revision: int = Field(ge=1)
    facility_id: str
    category: str
    major_category: MajorCategory | None = None
    origin: Origin
    destination: Origin
    straight_line_m: float | None = None
    #: 判定规则看的是返回的路线距离；落在误差带里或没有可用距离时为 None。
    within_rule: bool | None = None
    route_distance_m: float | None = None
    duration_s: float | None = None
    observed_duration_s: float | None = None
    #: 严格 POI 证据这一层：端点不重合就是 pending，详情证据也不放宽它。
    poi_status: Literal["pending", "verified_reachable", "verified_unreachable"] = "pending"
    poi_reason: str | None = None
    evidence_grade: Literal["verified", "model"] = "model"
    route_origin: Origin | None = None
    route_destination: Origin | None = None
    origin_offset_m: float | None = None
    destination_offset_m: float | None = None
    reason: str | None = None
    provider: str
    network: bool = False
    attempts: int = Field(default=0, ge=0)
    #: 详情桶的余额。它是这个任务的限额，不是账户余额（见 /api/v2/capabilities）。
    budget: dict = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


class CheckupCapabilities(CheckupModel):
    schema_version: str = SCHEMA_VERSION
    engines: list[dict]
    rules: dict
    data_versions: dict
    coverage: dict
    budgets: dict
    # The application's own remaining allowance, never the account's.
    quota: dict
    #: Water reviews this deployment applies: ``[{reviewId, version, label, title, bbox}]``.
    water_reviews: list[dict] = Field(default_factory=list)


def new_trace(*, isochrone_hash: str, result_hash: str, data_versions: dict,
              budgets: dict, extra_rule_versions: dict | None = None) -> TraceEvidence:
    """The trace of one revision. A stage names the rule versions it added."""
    return TraceEvidence(
        data_versions=data_versions,
        rule_versions={"distance": RULE_VERSION, "status_threshold_s": "900",
                       **(extra_rule_versions or {})},
        isochrone_hash=isochrone_hash, result_hash=result_hash, budgets=budgets)


def generated_at() -> float:
    return time.time()
