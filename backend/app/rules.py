"""N04: independent, explicit time and distance decisions; no implicit metric."""
import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class DistanceRule(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    metric: Literal["unconfirmed", "straight_line", "walking_route"] = "unconfirmed"
    threshold_m: float = Field(default=1000, gt=0)
    inclusive: bool | None = None
    tolerance_m: float | None = Field(default=None, ge=0)
    assessment_scope: Literal["unconfirmed", "isochrone", "community", "sample"] = "unconfirmed"
    # ``per_category`` is retained as a compatibility value; new payloads use
    # ``major_minor`` to make the shopping/medical/education hierarchy explicit.
    category_policy: Literal["unconfirmed", "per_category", "major_minor"] = "unconfirmed"


def distance_within(value: float | None, measured_metric: str, rule: DistanceRule) -> bool | None:
    if (rule.metric == "unconfirmed" or measured_metric != rule.metric
            or rule.inclusive is None or rule.tolerance_m is None
            or type(value) not in (float, int) or not math.isfinite(value) or value < 0):
        return None
    # Tolerance is an uncertainty band, never an automatic radius enlargement.
    if abs(value - rule.threshold_m) <= rule.tolerance_m and value != rule.threshold_m:
        return None
    return value <= rule.threshold_m if rule.inclusive else value < rule.threshold_m


def category_service(values: list[bool | None], complete: bool, rule: DistanceRule) -> str:
    if (rule.metric == "unconfirmed" or rule.inclusive is None or rule.tolerance_m is None
            or rule.assessment_scope == "unconfirmed" or rule.category_policy == "unconfirmed"):
        return "unknown"
    if True in values:
        return "covered"
    return "blind" if complete and all(v is False for v in values) else "unknown"
