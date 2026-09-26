"""Per-service quota: separate gates, task buckets and a durable daily ledger.

The two Baidu services are separate pools, so they get separate gates; a route
attempt and a place attempt never consume each other's allowance. Pacing itself
is the existing response-paced ``RateGate`` — this module adds what it does not
own: the per-task buckets, the application-wide daily budget and the tier that
applies right now.

An attempt passes one scheduling point that checks the task bucket, reserves the
daily budget in a transaction, waits for its paced slot, and then holds the
service's single in-flight slot. Reserving happens *before* the request is sent,
so failed, retried and sent-but-unclear attempts all stay counted and a process
restart never clears the ledger. The day boundary is Asia/Shanghai.

The ledger is a "this application" budget. The browser SDK, other applications
and the console's own accounting sit outside it, so a remaining balance here is
never a claim about the account's real remaining allowance.

The pools are the outer scheduler: a transport wrapper asks its pool for the
attempt and must not also pace on the service's gate, which would pace twice and
deadlock on the shared in-flight lock.

What is *not* yet scheduled here: the legacy ``/api/analyses`` and
``/api/v1/analysis/hybrid`` stages still pace on their one conservative shared
gate, so their attempts are absent from this ledger. §9.1 keeps that gate at 3
until the per-service refactor lands, and moving it is a real traffic change
that needs its own timing acceptance. The balance below therefore describes the
stages that are metered, and says so.
"""
import sqlite3
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .analyses import RateGate

# The deployment's quota day. Fixed rather than read from the host zone, so a
# container in UTC and a workstation in CST agree on when the day turns.
SHANGHAI = timezone(timedelta(hours=8))

DIRECTION = "direction"
PLACE = "place"
SERVICES = (DIRECTION, PLACE)

# Per-task pools. Attempts are counted, not successes; retries are attempts.
DEFAULT_POI_ATTEMPTS = 60
DEFAULT_ROUTE_ATTEMPTS = 120
DEFAULT_DETAIL_ATTEMPTS = 20

LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_spend (
    service TEXT NOT NULL,
    day TEXT NOT NULL,
    spent INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL,
    PRIMARY KEY (service, day)
);
"""


class QuotaError(Exception):
    """Base for refusals that must stop an attempt before it is sent."""

    code = "quota_error"

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


class BudgetExhausted(QuotaError):
    """The task's own pool for this service is used up."""

    code = "task_budget_exhausted"

    def __init__(self, pool: str, limit: int):
        self.pool, self.limit = pool, limit
        super().__init__(f"{pool} budget exhausted ({limit} attempts)")


class DailyBudgetExhausted(QuotaError):
    """The application's daily allowance for this service is used up."""

    code = "daily_budget_exhausted"

    def __init__(self, service: str, budget: int):
        self.service, self.budget = service, budget
        super().__init__(f"{service} daily application budget exhausted ({budget})")


class MatrixDisabled(QuotaError):
    """The route matrix is billed per origin-destination pair and stays off."""

    code = "matrix_disabled"


class DeadlineReached(QuotaError):
    code = "deadline_reached"

    def __init__(self):
        super().__init__("deadline reached before the request was sent")


@dataclass(frozen=True)
class ServiceTier:
    """The ceilings that apply at one instant."""
    direction_qps: float
    place_qps: float
    place_daily_budget: int
    label: str


@dataclass
class TaskBudget:
    """One task's pools. Shared by every transport the task drives."""
    isochrone: int
    poi: int = DEFAULT_POI_ATTEMPTS
    route: int = DEFAULT_ROUTE_ATTEMPTS
    detail: int = DEFAULT_DETAIL_ATTEMPTS
    spent: dict = field(default_factory=dict)

    def pools(self) -> dict:
        return {"isochrone": self.isochrone, "poi": self.poi, "route": self.route,
                "detail": self.detail}

    def remaining(self, pool: str) -> int:
        limits = self.pools()
        if pool not in limits:
            raise KeyError(pool)
        return max(0, limits[pool] - self.spent.get(pool, 0))

    def consume(self, pool: str) -> int:
        """Take one attempt. Never refunded: a failed attempt still cost a call."""
        limits = self.pools()
        if pool not in limits:
            raise KeyError(pool)
        limit = limits[pool]
        used = self.spent.get(pool, 0)
        if used >= limit:
            raise BudgetExhausted(pool, limit)
        self.spent[pool] = used + 1
        return limit - self.spent[pool]

    def state(self) -> dict:
        return {name: {"limit": limit, "spent": self.spent.get(name, 0)}
                for name, limit in self.pools().items()}


class DailyLedger:
    """Durable per-day application spend for each service."""

    def __init__(self, path: Path, *, clock=time.time):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock

    def _connection(self):
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(LEDGER_SCHEMA)

    def day(self, now: float | None = None) -> str:
        moment = self.clock() if now is None else now
        return datetime.fromtimestamp(moment, SHANGHAI).date().isoformat()

    def spent(self, service: str, *, day: str | None = None) -> int:
        with self._connection() as connection:
            row = connection.execute("SELECT spent FROM daily_spend WHERE service=? AND day=?",
                                     (service, day or self.day())).fetchone()
        return row["spent"] if row else 0

    def remaining(self, service: str, budget: int, *, day: str | None = None) -> int:
        return max(0, budget - self.spent(service, day=day))

    def reserve(self, service: str, budget: int, *, cost: int = 1) -> int:
        """Reserve before sending. Raises when the day's allowance is gone.

        The reservation is a transaction, so two concurrent attempts cannot both
        take the last unit, and a crash after reserving leaves the spend recorded
        rather than silently returned.
        """
        if service not in SERVICES:
            raise KeyError(service)
        if type(cost) is not int or cost <= 0:
            raise ValueError("cost must be a positive integer")
        day = self.day()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT spent FROM daily_spend WHERE service=? AND day=?",
                                     (service, day)).fetchone()
            used = row["spent"] if row else 0
            if used + cost > budget:
                connection.execute("COMMIT")
                raise DailyBudgetExhausted(service, budget)
            connection.execute(
                "INSERT INTO daily_spend (service, day, spent, updated_at) VALUES (?,?,?,?)"
                " ON CONFLICT(service, day) DO UPDATE SET spent=excluded.spent,"
                " updated_at=excluded.updated_at",
                (service, day, used + cost, self.clock()))
            connection.execute("COMMIT")
        return budget - used - cost


class QuotaTier:
    """Which tier applies now. Re-evaluated at every allocation and startup."""

    def __init__(self, current: ServiceTier, fallback: ServiceTier, switch_at: datetime,
                 *, clock=time.time):
        self.current, self.fallback = current, fallback
        self.switch_at, self.clock = switch_at, clock

    def active(self) -> ServiceTier:
        if datetime.fromtimestamp(self.clock(), SHANGHAI) >= self.switch_at:
            return self.fallback
        return self.current


@dataclass
class Attempt:
    """Handle for one in-flight attempt; the transport reports its outcome here."""
    service: str
    reason: str | None = None
    reported: bool = False

    def outcome(self, reason: str | None) -> None:
        """``None`` is a clean response and only costs the paced interval."""
        self.reason = reason
        self.reported = True


class ServicePool:
    """One service's scheduling point: task bucket, daily budget, slot, pacing."""

    def __init__(self, name: str, gate: RateGate, ledger: DailyLedger, tier: QuotaTier,
                 *, max_inflight: int = 1):
        self.name, self.gate, self.ledger, self.tier = name, gate, ledger, tier
        self.max_inflight = max_inflight

    def ceiling(self) -> float:
        tier = self.tier.active()
        return tier.direction_qps if self.name == DIRECTION else tier.place_qps

    def _daily_budget(self) -> int | None:
        return self.tier.active().place_daily_budget if self.name == PLACE else None

    def remaining_today(self) -> int | None:
        budget = self._daily_budget()
        return None if budget is None else self.ledger.remaining(self.name, budget)

    @asynccontextmanager
    async def attempt(self, deadline: float, *, budget: TaskBudget | None = None,
                      pool: str | None = None, cost: int = 1):
        """Take every allowance, then hold the service's single in-flight slot."""
        if budget is not None and pool is not None:
            for _ in range(cost):
                budget.consume(pool)
        daily = self._daily_budget()
        if daily is not None:
            self.ledger.reserve(self.name, daily, cost=cost)
        ceiling = self.ceiling()
        # The service cap is authoritative for anything that passes through this
        # pool, including a caller that asked for a looser request-level QPS. It
        # is re-applied every attempt because the fallback instant can pass while
        # a long task is still running.
        self.gate.qps = ceiling
        self.gate.interval = 1 / ceiling
        if not await self.gate.wait(deadline, cost=cost):
            raise DeadlineReached()
        handle = Attempt(service=self.name)
        await self.gate.attempt_lock.acquire()
        try:
            yield handle
        finally:
            self.gate.attempt_lock.release()
            # An attempt that never reported an outcome is treated as
            # interrupted: silence must not read as a clean response.
            self.gate.completed(handle.reason if handle.reported else "interrupted",
                                cost=cost)


class Quota:
    """The single allocation entry for both algorithms and every facility stage."""

    def __init__(self, settings, *, clock=time.time, ledger_path: Path | None = None):
        current = ServiceTier(settings.baidu_direction_qps, settings.baidu_place_qps,
                              settings.baidu_place_daily_budget, "current")
        fallback = ServiceTier(settings.baidu_fallback_direction_qps,
                               settings.baidu_fallback_place_qps,
                               settings.baidu_fallback_place_daily_budget, "fallback")
        self.tiers = QuotaTier(current, fallback, settings.baidu_quota_fallback_at, clock=clock)
        self.ledger = DailyLedger(ledger_path or settings.quota_ledger_path, clock=clock)
        self.ledger.initialize()
        self.matrix_enabled = settings.baidu_matrix_enabled
        self.direction = ServicePool(DIRECTION, RateGate(current.direction_qps), self.ledger,
                                     self.tiers, max_inflight=settings.baidu_direction_max_inflight)
        self.place = ServicePool(PLACE, RateGate(current.place_qps), self.ledger, self.tiers,
                                 max_inflight=settings.baidu_place_max_inflight)

    def pool(self, name: str) -> ServicePool:
        if name == DIRECTION:
            return self.direction
        if name == PLACE:
            return self.place
        raise KeyError(name)

    def task_budget(self, *, isochrone: int, poi: int | None = None, route: int | None = None,
                    detail: int | None = None) -> TaskBudget:
        return TaskBudget(isochrone=isochrone,
                          poi=DEFAULT_POI_ATTEMPTS if poi is None else poi,
                          route=DEFAULT_ROUTE_ATTEMPTS if route is None else route,
                          detail=DEFAULT_DETAIL_ATTEMPTS if detail is None else detail)

    def require_matrix(self) -> None:
        """The route matrix is billed per origin-destination pair, not per call."""
        if not self.matrix_enabled:
            raise MatrixDisabled("BAIDU_MATRIX_ENABLED is false for this release")

    def balance(self) -> dict:
        """What the interface may call the application's own remaining budget."""
        active = self.tiers.active()
        day = self.ledger.day()
        return {
            "tier": active.label,
            "tierAppliesFrom": self.tiers.switch_at.isoformat(),
            "day": day,
            "services": {
                DIRECTION: {"qps": active.direction_qps,
                            "maxInflight": self.direction.max_inflight,
                            "dailyBudget": None, "spentToday": None, "remainingToday": None},
                PLACE: {"qps": active.place_qps, "maxInflight": self.place.max_inflight,
                        "dailyBudget": active.place_daily_budget,
                        "spentToday": self.ledger.spent(PLACE, day=day),
                        "remainingToday": self.remaining(PLACE)},
            },
            "matrixEnabled": self.matrix_enabled,
            # The browser SDK and other applications share the real account.
            "claimsAccountBalance": False,
        }

    def remaining(self, service: str) -> int | None:
        return self.pool(service).remaining_today()
