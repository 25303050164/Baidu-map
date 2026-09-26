"""Explicit isochrone engines. A request names one engine; nothing switches silently.

The adapters here call each algorithm's compute-only seam. They never call the
legacy job managers, whose post-isochrone facility stage would issue a second,
duplicate facility search for the same task.
"""
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from life_circle.models import CancelToken, ProgressSnapshot

# Fixed for the first release; the facility service standard stays walking
# distance 1000 m and does not become a 900 s standard here.
STATUS_THRESHOLD_S = 900

Quality = Literal["usable", "partial", "insufficient"]


class EngineModel(BaseModel):
    """Camel-case on the wire, like every other HTTP contract in the project."""
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, populate_by_name=True,
                              alias_generator=to_camel)


def canonical_hash(value) -> str:
    """Stable digest over JSON-representable evidence; never a signature."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


class EngineCapabilities(EngineModel):
    """What an engine will accept. Validated on the request, never guessed."""
    engine_id: str
    label: str
    engine_version: str
    budgets: tuple[int, ...]
    default_budget: int
    requires_osm_graph: bool
    status_threshold_seconds: Literal[900] = STATUS_THRESHOLD_S
    notes: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class IsochroneAsk:
    """The only inputs an engine may receive. Thresholds stay server-side."""
    origin: tuple[float, float]
    budget: int


@dataclass
class EngineContext:
    """Task-scoped handles. Engines never allocate quota or storage themselves."""
    task_id: str
    token: CancelToken
    deadline: float
    artifact_dir: Path | None = None
    on_progress: Callable[[ProgressSnapshot], None] | None = None

    def progress(self, snapshot: ProgressSnapshot) -> None:
        if self.on_progress is not None:
            self.on_progress(snapshot)


class IsochroneSnapshot(EngineModel):
    """A frozen isochrone result with its own identity and quality evidence.

    ``display_geometry`` is a shell for drawing only: it never participates in
    counting, the assessment domain or heatmap clipping.
    """
    engine_id: str
    engine_version: str
    algorithm: str
    parameters: dict
    geometry: dict | None
    display_geometry: dict | None
    unknown_region: dict | None
    uncertain_region: dict | None
    computation_extent: dict | None
    quality: Quality
    stop_reason: str
    warnings: list[str]
    statistics: dict
    requests_used: int = Field(default=0, ge=0)
    network_requests: int = Field(default=0, ge=0)
    isochrone_hash: str

    @classmethod
    def build(cls, *, engine_id: str, engine_version: str, algorithm: str,
              parameters: dict, geometry: dict | None, display_geometry: dict | None,
              unknown_region: dict | None, uncertain_region: dict | None,
              computation_extent: dict | None, quality: Quality, stop_reason: str,
              warnings: list[str], statistics: dict, requests_used: int = 0,
              network_requests: int = 0) -> "IsochroneSnapshot":
        # The isochrone hash identifies the boundary only. POI, rules and the
        # spatial analysis are covered by the separate full result hash, so a
        # later facility stage cannot silently inherit this digest.
        isochrone_hash = canonical_hash({
            "engineId": engine_id, "engineVersion": engine_version,
            "algorithm": algorithm, "parameters": parameters,
            "geometry": geometry, "unknownRegion": unknown_region,
            "computationExtent": computation_extent,
        })
        return cls(engine_id=engine_id, engine_version=engine_version, algorithm=algorithm,
                   parameters=parameters, geometry=geometry, display_geometry=display_geometry,
                   unknown_region=unknown_region, uncertain_region=uncertain_region,
                   computation_extent=computation_extent, quality=quality, stop_reason=stop_reason,
                   warnings=warnings, statistics=statistics, isochrone_hash=isochrone_hash)


@runtime_checkable
class IsochroneEngine(Protocol):
    engine_id: str

    def capabilities(self) -> EngineCapabilities: ...

    async def compute(self, ask: IsochroneAsk, context: EngineContext) -> IsochroneSnapshot: ...


class UnknownEngine(LookupError):
    """Raised for an engine id or budget tier the registry does not serve."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class EngineRegistry:
    """Explicit engine table. An unsupported id or tier is a request error."""

    def __init__(self, engines):
        self.engines = {engine.engine_id: engine for engine in engines}

    def get(self, engine_id: str):
        if engine_id not in self.engines:
            raise UnknownEngine("unknown_engine")
        return self.engines[engine_id]

    def capabilities(self) -> list[EngineCapabilities]:
        return [engine.capabilities() for engine in self.engines.values()]

    def validate(self, engine_id: str, budget: int):
        engine = self.get(engine_id)
        if budget not in engine.capabilities().budgets:
            raise UnknownEngine("unsupported_budget")
        return engine
