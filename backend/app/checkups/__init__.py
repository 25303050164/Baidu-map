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


def build_checkups(settings, gate, offline, *, provider_factory=None,
                   hybrid_provider_factory=None):
    """Open the durable store and register both engines for the given settings."""
    from ..engines import build_registry

    store = CheckupStore(settings.checkup_dir)
    store.initialize()
    registry = build_registry(settings, gate, offline, provider_factory=provider_factory,
                              hybrid_provider_factory=hybrid_provider_factory)
    return CheckupManager(settings, registry, store)
