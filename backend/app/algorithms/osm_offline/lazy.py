"""Thread-safe lazy holder for the optional city-wide OSM graph."""
from __future__ import annotations

import asyncio
import threading

from ... import bulk_load


class LazyOsmOfflineEngine:
    def __init__(self, settings):
        self.settings = settings
        self.state = "unloaded"
        self._engine = None
        self._lock = threading.Lock()
        #: How far a load in progress has got, ``(step, done, total)``; None
        #: otherwise. Whoever waits for the graph reads it (see ``resolve``).
        self.loading = None

    def _report(self, step, done=None, total=None):
        self.loading = (step, done, total)

    def get(self):
        if self._engine is not None:
            return self._engine
        with self._lock:
            if self._engine is not None:
                return self._engine
            self.state = "loading"
            try:
                from .engine import OsmOfflineEngine
                # Millions of objects, for the life of the process.
                with bulk_load.long_lived():
                    engine = OsmOfflineEngine.load(self.settings, progress=self._report)
                self._engine = engine
                self.state = "ready" if engine.store is not None else "unavailable"
                return engine
            except Exception:
                self.state = "unavailable"
                raise
            finally:
                self.loading = None

    @property
    def store(self):
        return self.get().store

    @property
    def coverage(self):
        return self.get().coverage

    def compute(self, request):
        return self.get().compute(request)


async def resolve(offline, report=None, *, every=1.0):
    """``offline.get()`` off the event loop, telling ``report`` how a load is going.

    The first use of the city graph takes minutes, and the load may be another
    caller's (the one before, a warm-up): every waiter reads the same account,
    ``report(step, done, total)``, once per ``every`` seconds when it has moved.
    """
    pending = asyncio.ensure_future(asyncio.to_thread(offline.get))
    shown = None
    try:
        while not (await asyncio.wait({pending}, timeout=every))[0]:
            now = getattr(offline, "loading", None)
            if report is not None and now is not None and now != shown:
                shown = now
                report(*now)
    except asyncio.CancelledError:
        pending.cancel()
        raise
    return pending.result()
