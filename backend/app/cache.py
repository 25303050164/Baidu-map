"""§3.4 caches: what may be reused, for how long, and by whom.

Four kinds are named — a successful request page, a facility-set snapshot, a route
and a spatial result. Each is keyed by everything that would change the answer, so
a different coordinate system, query range, provider or version is a different
entry rather than a stale hit. ``cache_key`` owns that rule and refuses a key that
omits one of §3.4's components, so the omission surfaces here instead of surfacing
later as a wrong answer.

Three properties carry the rest of §3.4:

* A failure is never written down. An attempt that failed, or was sent and left
  unclear, is returned to its caller — which still has to count it — and nothing
  is stored, so the next caller asks again instead of reading a zero result.
* An entry is reusable across tasks only inside a configured window, and only
  inside the task that stored it when no window is configured: cross-task
  validity is a deployment's data-use decision, never an assumption and never
  permanent.
* An entry keeps the instant its data was obtained. A hit reports that original
  time, so a cached answer cannot refresh a collection time.

Identical in-flight queries are coalesced: the second caller awaits the first
call's result rather than issuing a second request, and both are told that they
did not pay for it. The cache is per process and single event loop, so its maps
need no lock.
"""
import asyncio
import hashlib
import json
import time
from dataclasses import dataclass

# §3.4's component lists, in the names the callers here use. The set is closed: a
# kind missing from this table has no key rule, and a key that omits a component
# or carries an unnamed one is refused rather than quietly made narrower.
COMPONENTS = {
    # Coordinate system, query range and parameters, provider and version.
    'page': ('coordinateSystem', 'scope', 'parameters', 'provider', 'apiVersion'),
    # The same, plus the rule version that classified the set being snapshotted.
    'facility_snapshot': ('coordinateSystem', 'scope', 'ruleVersion', 'provider', 'apiVersion'),
    # Spatial results additionally carry the algorithm, the rule, the grid, and
    # the POI and road-network versions.
    'spatial': ('coordinateSystem', 'scope', 'algorithm', 'ruleVersion', 'grid',
                'poiVersion', 'networkVersion'),
    # A route carries its direction, the UID, the target entrance, the routing
    # metric and the evidence strategy.
    'route': ('coordinateSystem', 'direction', 'uid', 'entrance', 'metric',
              'evidenceStrategy'),
}
# M2 produces pages. The other three rows name what their callers must supply
# when M4 and M5 add them: a route or spatial key cannot be built without its
# versions, so the omission surfaces at the key instead of as a stale hit.


def _canonical(components):
    try:
        return json.dumps(components, sort_keys=True, separators=(',', ':'),
                          ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        # A component that is not canonical JSON — an object instead of its
        # identity, a non-finite number — cannot take part in a stable key.
        raise ValueError('cache key components must be canonical JSON') from None


def cache_key(kind, **components):
    """The one key shape, so no caller invents its own."""
    required = COMPONENTS.get(kind)
    if required is None:
        raise ValueError(f'unknown cache kind: {kind}')
    missing = [name for name in required if components.get(name) is None]
    if missing:
        raise ValueError(f'{kind} cache key missing: ' + ', '.join(missing))
    unnamed = sorted(set(components) - set(required))
    if unnamed:
        raise ValueError(f'{kind} cache key carries unnamed components: ' + ', '.join(unnamed))
    return kind + ':' + hashlib.sha256(_canonical(components).encode()).hexdigest()


@dataclass(frozen=True)
class Entry:
    value: object
    task_id: str
    obtained_at: float


@dataclass(frozen=True)
class Answer:
    """One resolve's outcome.

    ``cached`` is True when this call did not itself build the value: it read a
    stored entry, or it awaited an identical in-flight query. It is the flag a
    caller uses to decide whether it owes an upstream request.

    ``shared`` separates the two ways that can happen, because they are not the
    same fact: a stored entry is a page this deployment already obtained, while a
    shared in-flight query is a page *being* obtained right now, and whose result
    is not evidence until it succeeds. A precheck may read the first; it must not
    treat the second as one.

    ``source_task_id`` is **who paid for this value**: the task that stored it, or
    the task whose in-flight request this call awaited. §5 B2 决策 2 makes a
    session's deadline apply to the data as well as to the reports that show it, so
    a caller has to be able to say which deadline governs what it just read — and
    a derived report has to record where its bytes came from, because "reuse does
    not reset the source's deadline" is only enforceable if the source is named.
    """
    value: object
    reason: str | None
    obtained_at: float
    cached: bool
    shared: bool = False
    source_task_id: str | None = None


class SharedBuildFailed(Exception):
    """An in-flight build this caller waited on failed, and it was not this caller's call.

    Awaited sharing is only an optimisation for the *caller*: someone else already
    reserved and dispatched the request, so the outcome — success or failure — is
    theirs to account for. Without this distinction the failure reaches the waiter
    as a plain transport exception, and a caller that classifies exceptions by type
    reads it as "I dispatched and it timed out", which is a call it never made.

    The original exception is kept as ``cause`` so the caller can still name the
    reason it ended in, and so an exception this layer does not classify keeps
    propagating exactly as it did before.
    """

    def __init__(self, cause: BaseException):
        self.cause = cause
        super().__init__(f'shared build failed: {cause}')


class KeyedCache:
    def __init__(self, *, freshness_seconds=None, max_entries=1024, clock=time.time,
                 reused=None):
        """``reused`` answers "may a task other than the one that stored this read it".

        It is the seam §5 B2 决策 2 needs: the window alone cannot express a
        deadline that is an event (the last tab closing, the third later checkup
        finishing) rather than a number of seconds. It is consulted **only** for a
        cross-task read — a task's own entries are its own evidence, and denying
        those would make an expiring session re-fetch pages it already paid for.
        """
        if freshness_seconds is not None and not freshness_seconds > 0:
            raise ValueError('freshness_seconds must be positive or None')
        if max_entries < 1:
            raise ValueError('max_entries must be at least 1')
        self.freshness_seconds = freshness_seconds
        self.max_entries = max_entries
        self.clock = clock
        #: ``None`` means "no deadline is configured", which is not "everything is
        #: allowed": cross-task reuse still needs a window.
        self.reused = reused
        self.entries: dict[str, Entry] = {}
        self.inflight: dict[str, asyncio.Future] = {}
        self.hits = 0
        self.stores = 0
        self.refused = 0

    def __len__(self):
        return len(self.entries)

    def fresh(self, entry, *, task_id):
        """Same task always; another task only inside the window *and* by permission."""
        if entry.task_id == task_id:
            return True
        if self.freshness_seconds is None:
            return False
        if self.reused is not None and not self.reused(entry.task_id):
            return False
        return self.clock() - entry.obtained_at <= self.freshness_seconds

    def get(self, key, *, task_id):
        entry = self.entries.get(key)
        if entry is None:
            return None
        if self.fresh(entry, task_id=task_id):
            return entry
        if entry.task_id != task_id:
            # Counted apart from a miss: "the source's deadline has passed" and
            # "nobody has asked for this yet" lead to different next steps.
            self.refused += 1
        return None

    def drop_task(self, task_id: str) -> int:
        """Forget every entry one task stored. Called when its detail is deleted.

        This is the memory half of the same decision: the files are gone, so a
        copy of the pages they contained must not stay reachable in the process
        that deleted them.
        """
        doomed = [key for key, entry in self.entries.items() if entry.task_id == task_id]
        for key in doomed:
            self.entries.pop(key, None)
        return len(doomed)

    def store(self, key, value, *, task_id):
        if value is None:
            raise ValueError('a cache entry must have a value')
        entry = Entry(value=value, task_id=task_id, obtained_at=self.clock())
        # Re-storing moves a key to the end, so eviction takes the least recently
        # written first and the cache stays bounded in a process that never
        # restarts.
        self.entries.pop(key, None)
        self.entries[key] = entry
        while len(self.entries) > self.max_entries:
            self.entries.pop(next(iter(self.entries)))
        self.stores += 1
        return entry

    async def resolve(self, key, build, *, task_id):
        """Read, or build once and share the build with identical waiters.

        ``build`` performs one upstream attempt and returns ``(value, reason)``.
        """
        entry = self.get(key, task_id=task_id)
        if entry is not None:
            self.hits += 1
            return Answer(entry.value, None, entry.obtained_at, True,
                          source_task_id=entry.task_id)
        pending = self.inflight.get(key)
        if pending is not None:
            self.hits += 1
            # Shielded: a cancelled waiter must not cancel the attempt whose
            # result the other waiters are still owed.
            try:
                shared = await asyncio.shield(pending)
            except Exception as exc:  # CancelledError is not this: a cancelled waiter leaves.
                # The builder owns this attempt — including its failure. Reported as a
                # shared outcome so the waiter cannot count a call it did not make.
                raise SharedBuildFailed(exc) from None
            return Answer(shared.value, shared.reason, shared.obtained_at, True, True,
                          source_task_id=shared.source_task_id)
        future = asyncio.ensure_future(self._build(key, build, task_id))
        self.inflight[key] = future
        return await asyncio.shield(future)

    async def _build(self, key, build, task_id):
        try:
            value, reason = await build()
        finally:
            self.inflight.pop(key, None)
        if value is None or reason is not None:
            return Answer(value, reason, self.clock(), False, source_task_id=task_id)
        entry = self.store(key, value, task_id=task_id)
        return Answer(entry.value, None, entry.obtained_at, False, source_task_id=entry.task_id)
