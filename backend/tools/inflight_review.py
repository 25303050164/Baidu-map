"""Live check of the route gate with more than one attempt in flight (QPS-002 follow-up).

Baidu answered status 401 (concurrency exceeded) when arrivals were compressed in
2026-09. Before BAIDU_DIRECTION_MAX_INFLIGHT may go above one, each round sends 20
attempts at most -- retries included -- through the production Scheduler
(concurrency N), LimitedProvider and a RateGate with N slots, and an audit transport
records every dispatch and response. The audit adds no pacing; it halts the round
if more than N attempts would be in flight or a rolling second would exceed the QPS.

    python -m tools.inflight_review --round inflight-2 --output OUT
    python -m tools.inflight_review --round inflight-3 --output OUT

A round passes when attempts really overlapped (2 <= max in flight <= N), no rolling
second exceeded the QPS, no answer was a rate limit, and every route came back
with verified endpoints. Each round is a one-time claim; nothing is resumed.
"""
import argparse
import asyncio
import json
import math
import time
from collections import Counter
from pathlib import Path

import httpx

from app.analyses import LimitedProvider, RateGate
from app.baidu import silence_transport_logs
from app.config import load_settings
from app.persistence import atomic_dump
from app.test_origin import NORMALIZED_TEST_ORIGIN
from life_circle.coordinates import LocalProjection, normalize
from life_circle.models import CancelToken, IsochroneRequest
from life_circle.providers import BaiduProvider
from life_circle.scheduler import Scheduler
from tools.e821_independent_validation import claim

ROUNDS = {'inflight-2': dict(inflight=2, qps=10), 'inflight-3': dict(inflight=3, qps=30)}
LIMIT = 20
DISTANCES_M = (200, 300, 400, 500, 600)


class AuditHalt(Exception):
    pass


class AuditTransport(httpx.AsyncBaseTransport):
    """Records dispatch/response times; halts instead of pacing when a limit would break."""

    def __init__(self, inner, *, inflight, qps, events):
        self.inner, self.inflight, self.qps, self.events = inner, inflight, qps, events
        self.halted = None

    async def handle_async_request(self, request):
        # The scheduler reads a provider exception as one failed attempt and goes on:
        # once halted, every later attempt stops here, before the network.
        if self.halted:
            raise AuditHalt(self.halted)
        now = time.perf_counter()
        sent = [e['dispatch'] for e in self.events]
        open_now = sum(e['dispatch'] <= now and e.get('response', math.inf) > now for e in self.events)
        if open_now >= self.inflight:
            self.halted = 'inflight_exceeded'
            raise AuditHalt(self.halted)
        if sum(now - 1 < t <= now for t in sent) >= self.qps:
            self.halted = 'rolling_second_exceeded'
            raise AuditHalt(self.halted)
        event = dict(dispatch=now)
        self.events.append(event)
        try:
            response = await self.inner.handle_async_request(request)
            await response.aread()
            event['http_status'] = response.status_code
            try:
                event['baidu_status'] = json.loads(response.content).get('status')
            except ValueError:
                event['baidu_status'] = None
            return response
        finally:
            event['response'] = time.perf_counter()

    async def aclose(self):
        await self.inner.aclose()


def metrics(events, inflight, qps):
    times = [e['dispatch'] for e in events]
    overlap = max((sum(e['dispatch'] <= t < e.get('response', math.inf) for e in events) for t in times), default=0)
    gaps = [b - a for a, b in zip(times, times[1:])]
    statuses = Counter(str(e.get('baidu_status')) for e in events)
    return dict(sends=len(events), max_inflight=overlap, max_in_one_second=max(
        (sum(t <= s < t + 1 for s in times) for t in times), default=0),
        minimum_gap_s=min(gaps, default=None), http_status=dict(Counter(str(e.get('http_status')) for e in events)),
        baidu_status=dict(statuses), rate_limited=statuses.get('401', 0) + statuses.get('402', 0)
        + sum(e.get('http_status') == 429 for e in events),
        elapsed_s=(max(e.get('response', t) for e, t in zip(events, times)) - times[0]) if times else 0,
        target_inflight=inflight, target_qps=qps)


async def run_round(settings, inflight, qps, transport):
    projection = LocalProjection(tuple(NORMALIZED_TEST_ORIGIN))
    points = [normalize(projection.to_geographic((d * dx, d * dy)))
              for d in DISTANCES_M for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))]
    async with httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False) as client:
        provider = LimitedProvider(BaiduProvider(settings.baidu_map_ak.get_secret_value(), client=client),
                                   RateGate(qps, max_inflight=inflight))
        request = IsochroneRequest(tuple(NORMALIZED_TEST_ORIGIN), 'bd09ll', budget=LIMIT, qps=qps,
                                   concurrency=inflight, deadline_seconds=120)
        scheduler = Scheduler(request, provider, CancelToken())
        observations = await scheduler.observe_many(points)
        return scheduler, observations


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--round', choices=sorted(ROUNDS), required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    spec = ROUNDS[args.round]
    root = args.output.resolve() / args.round
    settings = load_settings()
    if not settings.ak_configured:
        raise SystemExit('walking_ak_not_configured')
    if spec['qps'] > settings.baidu_fallback_direction_qps and spec['qps'] > settings.baidu_direction_qps:
        raise SystemExit('round_qps_above_the_configured_entitlement')
    claim(root / 'started.json', dict(round=args.round, limit=LIMIT, started_at=time.time(), **spec))
    silence_transport_logs()
    events = []
    transport = AuditTransport(httpx.AsyncHTTPTransport(retries=0, trust_env=False),
                               inflight=spec['inflight'], qps=spec['qps'], events=events)
    scheduler, observations = asyncio.run(run_round(settings, spec['inflight'], spec['qps'], transport))
    halted = transport.halted
    measured = metrics(events, spec['inflight'], spec['qps'])
    verified = sum(o.duration is not None and o.endpoint_verified for o in observations)
    passed = bool(halted is None and 2 <= measured['max_inflight'] <= spec['inflight']
                  and measured['max_in_one_second'] <= spec['qps'] and measured['rate_limited'] == 0
                  and observations and verified == len(observations) and measured['sends'] <= LIMIT)
    result = dict(round=args.round, passed=passed, halted=halted, metrics=measured,
                  attempts=scheduler.stats.requests, retries=scheduler.stats.retries,
                  verified_routes=verified, points=len(observations),
                  reasons=dict(Counter(str(o.reason) for o in observations)),
                  events=[{k: v for k, v in e.items()} for e in events])
    atomic_dump(root / 'result.json', result)
    print(json.dumps({k: v for k, v in result.items() if k != 'events'}, ensure_ascii=False))


if __name__ == '__main__':
    main()
