"""The online place transport (§4.3): the quota pool is the only reservation.

The command-line POI runtime carries its own ledger gate with an approval hash
and an execution window. §4.3 asks for its pure planning, paging and
classification abilities to be reused and for online tasks to get an
*independent production runtime*, so this transport reserves nothing itself and
fabricates no approval field: one attempt is one entry through the shared place
pool, which checks the service ceiling, the task bucket, the daily application
budget and the deadline at a single point before the request is sent (§9.2).

Retries and paging stay the planner's business — this only sends what it is
asked for, once per call. A sent-but-unclear attempt has already been reserved,
so it stays counted whether it answered or not.

``session`` is the whole transport interface the facility stage uses. A
deployment without a key, or an offline run, refuses or substitutes at that
seam; the stage does not know which transport it holds beyond the identity it
records.
"""
import time
from typing import Protocol

import httpx

from ..baidu import API_URL, silence_transport_logs
from ..poi.planner import parameters
from ..poi.provider import read_page

# The task's own pool name, as ``quota.TaskBudget`` spells it.
POI_POOL = 'poi'
# An upper bound on one response, well inside the provider's own patience; the
# task deadline always wins over it.
MAX_TIMEOUT_SECONDS = 8.0


class PlacesUnavailable(Exception):
    """No place transport can be built for this deployment."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class PlaceTransport(Protocol):
    identity: str
    api_version: str
    network: bool

    def session(self, pool, *, budget, deadline): ...


async def open_online(settings, stack) -> "OnlinePlaceTransport":
    """The production transport: one client for one stage, or a named refusal."""
    if not settings.ak_configured:
        raise PlacesUnavailable('missing_ak')
    silence_transport_logs()
    client = await stack.enter_async_context(
        httpx.AsyncClient(trust_env=False, follow_redirects=False))
    return OnlinePlaceTransport(client, settings.baidu_map_ak.get_secret_value())


class OnlinePlaceTransport:
    """Place v3 over one HTTP client, read by the same allowlist as the legacy one."""

    identity, api_version, network = 'baidu_place', '3.0', True

    def __init__(self, client, secret):
        self.client, self.secret = client, secret

    def session(self, pool, *, budget, deadline):
        return PlaceSession(self, pool, budget=budget, deadline=deadline)

    async def page(self, params, timeout):
        silence_transport_logs()
        response = await self.client.get(
            API_URL, params={**params, 'ak': self.secret},
            timeout=max(.01, min(MAX_TIMEOUT_SECONDS, timeout)), follow_redirects=False)
        try:
            payload = response.json() if response.status_code == 200 else None
        except ValueError:
            # A body that cannot be read is a sent-but-unclear attempt, not a
            # zero result: the caller records exactly that.
            payload = None
        return read_page(payload, response.status_code, self.secret)


class PlaceSession:
    """One task's place fetches, bound to its pool, budget and deadline.

    An instance is the adaptive planner's ``Fetch``: exactly one upstream attempt
    per call, always behind its reservation.
    """

    def __init__(self, transport, pool, *, budget, deadline):
        self.transport, self.pool, self.budget, self.deadline = transport, pool, budget, deadline

    async def __call__(self, sequence, page):
        async with self.pool.attempt(self.deadline, budget=self.budget, pool=POI_POOL) as attempt:
            payload, reason = await self.transport.page(
                parameters(sequence, page), self.deadline - time.monotonic())
            attempt.outcome(reason)
        return payload, reason
