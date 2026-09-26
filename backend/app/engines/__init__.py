"""Explicit isochrone engine adapters and their capability table."""
from .baidu_e82 import BaiduE82Engine
from .osm_hybrid import OsmHybridEngine
from .protocol import (STATUS_THRESHOLD_S, EngineCapabilities, EngineContext, EngineRegistry,
                       IsochroneAsk, IsochroneSnapshot, IsochroneEngine, UnknownEngine,
                       canonical_hash)

__all__ = ["BaiduE82Engine", "OsmHybridEngine", "EngineCapabilities", "EngineContext",
           "EngineRegistry", "IsochroneAsk", "IsochroneSnapshot", "IsochroneEngine",
           "UnknownEngine", "canonical_hash", "build_registry", "STATUS_THRESHOLD_S"]


def build_registry(settings, gate, offline, *, provider_factory=None,
                   hybrid_provider_factory=None) -> EngineRegistry:
    """Register both engines. Neither is a fallback for the other."""
    return EngineRegistry([
        BaiduE82Engine(settings, gate, provider_factory),
        OsmHybridEngine(settings, gate, offline, hybrid_provider_factory),
    ])
