"""Versioned checkup tasks: engine adapters, durable revisions and the v2 API."""
from .manager import CheckupError, CheckupManager, business_status_for, resolve_budget
from .models import (SCHEMA_VERSION, RULE_VERSION, CheckupRequest, CheckupSnapshot,
                     CheckupTaskView)
from .router import capabilities_router, checkup_router
from .store import CheckupStore, RequestIdConflict, TaskNotFound

__all__ = ["CheckupError", "CheckupManager", "CheckupStore", "CheckupRequest", "CheckupSnapshot",
           "CheckupTaskView", "RequestIdConflict", "TaskNotFound", "build_checkups",
           "capabilities_router", "checkup_router", "business_status_for", "resolve_budget",
           "SCHEMA_VERSION", "RULE_VERSION"]


def build_checkups(settings, gate, offline, *, quota, provider_factory=None,
                   hybrid_provider_factory=None, place_factory=None, route_factory=None):
    """Open the durable store and register both engines for the given settings.

    ``quota`` is injected rather than built here: one allocation entry is shared
    by every stage of the application, so it outlives this manager. ``place_factory``
    and ``route_factory`` are the facility stage's and the verification stage's
    transport seams, ``None`` meaning the deployment's own. ``offline`` is the
    walking-network provider; the assessment uses the same one the OSM engine
    does, because "within 1000 m on foot" does not depend on which algorithm drew
    the circle.
    """
    from ..engines import build_registry

    store = CheckupStore(settings.checkup_dir)
    store.initialize()
    registry = build_registry(settings, gate, offline, provider_factory=provider_factory,
                              hybrid_provider_factory=hybrid_provider_factory)
    return CheckupManager(settings, registry, store, quota, place_factory=place_factory,
                          route_factory=route_factory, offline=offline)
