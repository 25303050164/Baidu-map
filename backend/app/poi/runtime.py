"""Independent run budgets and fail-closed dispatch. No task A budget reuse."""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import time
import math
from statistics import median

import httpx

from app.analyses import RateGate
from app.persistence import atomic_dump, lock_file, unlock_file
from app.place_protocol import RETRY_ERRORS, STOP_ERRORS
from app.request_control import RequestStopped, request_slot
from life_circle.models import CancelToken
from .planner import build_plan, digest, parameters
from .models import PoiCollectRequest


def utcnow():
    return datetime.now(timezone.utc).isoformat()


class PoiLedger:
    def __init__(self, root, config, config_hash):
        self.root, self.config, self.config_hash = Path(root), config, config_hash
        self.file = self.root / 'ledger.json'
        self.stream = None

    def __enter__(self):
        self.root.mkdir(parents=True, exist_ok=True)
        self.stream = (self.root / 'ledger.lock').open('a+b')
        try:
            lock_file(self.stream)
        except OSError:
            self.stream.close()
            self.stream = None
            raise RequestStopped('ledger_locked') from None
        try:
            if self.file.exists():
                # Every ledger identifies one authorized run. Never reset or resume it implicitly.
                json.loads(self.file.read_text(encoding='utf-8'))
                raise RequestStopped('run_already_started')
            self.data = {'schemaVersion': 'poi-ledger-v1', 'runId': self.config.run_id,
                'configHash': self.config_hash, 'phase': self.config.phase, 'events': [],
                'counts': dict.fromkeys(self.config.category_budgets, 0), 'status': 'started'}
            self.save()
        except Exception:
            self.__exit__(None, None, None)
            raise
        return self

    def save(self):
        try:
            atomic_dump(self.file, self.data)
        except Exception:
            raise RequestStopped('audit_failure') from None

    def __exit__(self, *args):
        if self.stream:
            try:
                unlock_file(self.stream)
            finally:
                self.stream.close()
                self.stream = None


class PoiRuntime:
    def __init__(self, config, plan, *, live=False, ledger=None, token=None, gate=None):
        self.config, self.plan = config.model_copy(deep=True), deepcopy(plan)
        if build_plan(PoiCollectRequest.model_validate(plan['request']), self.config) != self.plan:
            raise RequestStopped('plan_mismatch')
        self.live, self.ledger = live, ledger
        self.token = token or CancelToken()
        self.gate = gate or RateGate(self.config.qps if live else None)
        self.deadline = time.monotonic() + self.config.deadline_seconds
        self.started_at = utcnow()
        self.stop_reason = None
        self.counts = dict.fromkeys(config.category_budgets, 0)
        self.events, self.cache = [], {}
        self.cache_hits = 0
        self.limiter_wait_ms = 0
        self.used = False
        self.lock = asyncio.Lock()
        self.allowed = {(s['category'], digest(parameters(s, p))) for s in plan['sequences'] for p in range(8)}

    def authorize(self):
        c, now = self.config, datetime.now(timezone.utc)
        if (not self.live or c.authorized is not True or c.qps is None
                or c.approved_config_hash != self.plan['configHash']
                or c.window_start is None or c.window_end is None
                or not c.window_start <= now < c.window_end):
            raise RequestStopped('live_not_authorized')
        if self.ledger is None or self.ledger.stream is None:
            raise RequestStopped('ledger_required')
        if self.ledger.config_hash != self.plan['configHash'] or self.ledger.config.run_id != c.run_id:
            raise RequestStopped('ledger_mismatch')
        if (self.ledger.data.get('runId') != c.run_id or self.ledger.data.get('configHash') != self.plan['configHash']
                or self.ledger.data.get('status') != 'started'):
            raise RequestStopped('run_not_active')

    def check(self, category):
        if self.stop_reason:
            raise RequestStopped(self.stop_reason)
        if self.token.cancelled:
            raise RequestStopped('cancelled')
        if time.monotonic() >= self.deadline:
            raise RequestStopped('deadline')
        if self.live:
            self.authorize()
        if sum(self.counts.values()) >= self.config.total_budget:
            raise RequestStopped('total_budget')
        if self.counts[category] >= self.config.category_budgets[category]:
            raise RequestStopped('category_budget')

    def persist(self):
        if self.ledger:
            self.ledger.data.update(events=deepcopy(self.events), counts=dict(self.counts), stopReason=self.stop_reason)
            try:
                self.ledger.save()
            except RequestStopped:
                self.stop_reason = 'audit_failure'
                raise

    async def fetch(self, provider, sequence, page):
        params = parameters(sequence, page)
        category = sequence['category']
        if (category, digest(params)) not in self.allowed:
            raise RequestStopped('unplanned_request')
        if provider.network != self.live:
            raise RequestStopped('provider_mode_mismatch')
        key = digest({'version': provider.api_version, 'provider': provider.identity, 'params': params})
        async with self.lock:
            if self.token.cancelled or self.stop_reason or time.monotonic() >= self.deadline:
                raise RequestStopped('cancelled' if self.token.cancelled else self.stop_reason or 'deadline')
            if key in self.cache:
                self.cache_hits += 1
                return deepcopy(self.cache[key]), None
            for attempt in range(2):
                self.check(category)
                wait_started = time.monotonic()
                async with request_slot(self.gate, self.token, self.deadline) as outcome:
                    self.limiter_wait_ms += (time.monotonic()-wait_started)*1000
                    self.check(category)  # Recheck after pacing, including the authorization window.
                    event = {'id': len(self.events)+1, 'category': category, 'sequenceId': sequence['sequenceId'],
                        'pageNum': page, 'attempt': attempt+1, 'reservedAt': utcnow(), 'state': 'reserved',
                        'network': self.live, 'paramsHash': digest(params), 'elapsedMs': 0}
                    self.counts[category] += 1
                    self.events.append(event)
                    self.persist()  # Reserve before handing anything to the provider.
                    start = time.monotonic()
                    payload, reason = None, 'interrupted'
                    try:
                        event['state'] = 'dispatch_started'
                        self.persist()
                        payload, reason = await provider.page(params, self)
                        if not self.live:
                            event['state'] = 'replayed'
                    except httpx.TimeoutException:
                        reason = 'timeout'
                    except httpx.RequestError:
                        reason = 'network_error'
                    except RequestStopped as exc:
                        reason = exc.reason
                    except Exception:
                        reason = 'invalid_response'
                    finally:
                        outcome['reason'] = reason
                        event.update(reason=reason, elapsedMs=round((time.monotonic()-start)*1000))
                        self.persist()
                if reason in STOP_ERRORS or reason in ('audit_failure', 'live_not_authorized'):
                    self.stop_reason = reason
                if self.token.cancelled or time.monotonic() >= self.deadline:
                    raise RequestStopped('cancelled' if self.token.cancelled else 'deadline')
                if reason is None:
                    self.cache[key] = deepcopy(payload)
                    return payload, None
                if reason not in RETRY_ERRORS or attempt:
                    return None, reason

    def metrics(self):
        network = [e for e in self.events if e['network']]
        confirmed = sum(e['state'] == 'response_received' for e in network)
        durations = sorted(e['elapsedMs'] for e in network if e['state'] == 'response_received')
        return {'attempts': len(self.events), 'networkReservations': len(network),
            'confirmedSent': confirmed, 'conservativeReservations': len(network)-confirmed,
            'retries': sum(e['attempt'] > 1 for e in self.events), 'cacheHits': self.cache_hits,
            'countsByCategory': dict(self.counts), 'timingSource': 'network' if self.live else 'replay',
            'successfulResponses': sum(e.get('reason') is None for e in network if e['state'] == 'response_received'),
            'limiterWaitMs': round(self.limiter_wait_ms),
            'responseMedianMs': median(durations) if durations else None,
            'responseP95Ms': durations[math.ceil(len(durations)*.95)-1] if durations else None}
