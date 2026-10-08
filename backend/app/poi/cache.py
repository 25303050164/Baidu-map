"""§3.4 place pages: one clean page is reusable, a failure never is.

The cache sits *outside* the quota pool, so a hit costs no attempt at all — that
is what "缓存命中不计新请求" means here, and it is structural rather than a
counter: the pool's scheduling point is simply never entered. Everything else is
§4.2.6's accounting, which this leaves alone: a replayed page produces the same
coverage record it produced live, so a page that came from a partial run stays
partial evidence. A cache cannot complete a set.

What the wrapper adds to the caller's accounting is one explicit fact per page:
whether this call dispatched an upstream request, read a stored page, or awaited
an identical in-flight request someone else paid for. The planner cannot tell
those apart from the payload — all three return the same page — and inferring it
from a counter that a concurrent run also moves is exactly the guess this record
replaces.
"""
from ..cache import cache_key
from .online import CACHED, LIVE, SHARED, PageResponse
from .planner import parameters

DEFAULT_COORDINATE_SYSTEM = 'bd09ll'


def page_key(sequence, page, *, provider, api_version,
             coordinate_system=DEFAULT_COORDINATE_SYSTEM):
    """A page's identity: coordinate system, query range and parameters, provider, version.

    The query range is the block's ``localMeters`` — the wire parameters already
    carry the centre and radius, and keeping the block's own extent in the key
    says which part of the domain the page was asked for rather than only which
    circle was searched.
    """
    return cache_key('page', coordinateSystem=coordinate_system, scope=sequence['localMeters'],
                     parameters=parameters(sequence, page), provider=provider,
                     apiVersion=api_version)


class CachedPages:
    """The planner's ``Fetch`` with a cache in front of the metered fetch.

    ``provider`` is the transport itself, not its name: a key that took the
    version from anywhere else could drift from the transport actually used.
    """

    def __init__(self, cache, fetch, *, provider, task_id,
                 coordinate_system=DEFAULT_COORDINATE_SYSTEM):
        self.cache, self.fetch, self.task_id = cache, fetch, task_id
        self.coordinate_system = coordinate_system
        self.identity, self.api_version = provider.identity, provider.api_version
        # What this run asked for, keyed the way a page record is: §3.4 wants the
        # report to carry the original data time, and a hit must not refresh it. A
        # page shared with an identical in-flight query reads as a cache use for
        # the same reason a stored one does — this run did not send the request.
        self.uses: dict[tuple, dict] = {}

    def key(self, sequence, page):
        return page_key(sequence, page, provider=self.identity,
                        api_version=self.api_version, coordinate_system=self.coordinate_system)

    async def __call__(self, sequence, page):
        answer = await self.cache.resolve(self.key(sequence, page),
                                         lambda: self.fetch(sequence, page), task_id=self.task_id)
        manner = LIVE if not answer.cached else (SHARED if answer.shared else CACHED)
        self.uses[(sequence['tileId'], sequence['category'], sequence['query'], page)] = {
            'sequenceId': sequence['sequenceId'], 'source': 'cache' if answer.cached else 'live',
            'obtainedAt': answer.obtained_at, 'delivery': manner}
        return PageResponse(answer.value, answer.reason, manner)
