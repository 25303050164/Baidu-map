"""Versioned checkup wire contract.

Separate from the legacy ``/api/analyses`` and ``/api/v1/analysis/hybrid``
contracts. Nothing here is added to the generated demo contract, so the strict
POI evidence types keep their existing serialization byte-for-byte.
"""
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

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
    """What the boundary alone supports today; the assessment domain is M4 work."""
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
    facilities_status: Literal["not_integrated", "complete", "partial", "failed"]
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
    result_hash: str


class CheckupCapabilities(CheckupModel):
    schema_version: str = SCHEMA_VERSION
    engines: list[dict]
    rules: dict
    data_versions: dict
    coverage: dict
    budgets: dict


def new_trace(*, isochrone_hash: str, result_hash: str, data_versions: dict,
              budgets: dict) -> TraceEvidence:
    return TraceEvidence(
        data_versions=data_versions,
        rule_versions={"distance": RULE_VERSION, "status_threshold_s": "900"},
        isochrone_hash=isochrone_hash, result_hash=result_hash, budgets=budgets)


def generated_at() -> float:
    return time.time()
