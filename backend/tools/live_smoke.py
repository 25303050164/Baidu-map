"""Explicit real-test harness. Persistent budgets; no credentials in disk evidence.

Run from backend: python -m tools.live_smoke --output D:/CodexOutputs/guodingyi-live-smoke --run-id <id>
This starts a guarded local server; startup alone sends no walking requests.
"""
import argparse
import asyncio
import importlib.metadata
import json
import math
import os
from pathlib import Path
import re
import subprocess
import time
from datetime import datetime, timezone

import httpx
from dotenv import dotenv_values
from fastapi.responses import HTMLResponse, JSONResponse

from app.persistence import atomic_dump, lock_file, unlock_file
from app.baidu import silence_transport_logs
from app.analyses import LimitedProvider, RateGate
from app.config import Settings
from app.main import create_app
from life_circle.coordinates import LocalProjection, normalize
from life_circle.models import CancelToken, IsochroneRequest
from life_circle.providers import BaiduProvider
from life_circle.scheduler import Scheduler

ORIGIN = (121.513926, 31.313077)
LIMITS = {"smoke": 16, "cancel": 4, "analysis": 200}
TOTAL = 220


def diagnostic_destination():
    return normalize(LocalProjection(ORIGIN).to_geographic((200, 0)))


def endpoint_structure(value, depth=0):
    """Bounded, fixed-key schema evidence; never store arbitrary string values."""
    result = {'type': type(value).__name__ if type(value) in (dict, list, str, int, float, bool, type(None)) else 'other'}
    if depth >= 5:
        return result
    if isinstance(value, dict):
        result['fields'] = {k: endpoint_structure(value[k], depth + 1)
            for k in ('lng', 'lat', 'start_location', 'end_location') if k in value}
    elif isinstance(value, list):
        result['length'] = len(value)
        if value:
            result['first'] = endpoint_structure(value[0], depth + 1)
            result['last'] = endpoint_structure(value[-1], depth + 1)
    elif type(value) in (float, int, str):
        numeric = type(value) is not str or (len(value) <= 18 and re.fullmatch(r'-?\d+(?:\.\d+)?', value))
        if numeric:
            try:
                number = float(value)
            except (ValueError, OverflowError):
                return result
            if math.isfinite(number) and abs(number) <= 180:
                result['value'] = value
    return result


class LiveGuardError(httpx.RequestError):
    pass


def dump(path, value):
    atomic_dump(path, value)


class Ledger:
    phase_limits = LIMITS
    total_limit = TOTAL

    def __init__(self, root):
        self.root = Path(root)
        self.file = self.root / 'ledger.json'
        self.lock_file = None

    def __enter__(self):
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock_file = (self.root / 'ledger.lock').open('a+b')
        try:
            lock_file(self.lock_file)
        except OSError:
            self.lock_file.close()
            self.lock_file = None
            raise LiveGuardError('ledger_locked') from None
        try:
            if self.file.exists():
                self.data = json.loads(self.file.read_text(encoding='utf-8'))
                assert self.data['version'] == 1
                assert self.data['origin'] == list(ORIGIN)
                assert set(self.data['counts']) == set(self.phase_limits)
                assert all(type(n) is int and 0 <= n <= self.phase_limits[k] for k, n in self.data['counts'].items())
                assert isinstance(self.data['events'], list)
            else:
                self.data = {'version': 1, 'origin': list(ORIGIN), 'counts': dict.fromkeys(self.phase_limits, 0),
                    'events': [], 'halted': None, 'map_checked': False, 'smoke_started': False,
                    'smoke_passed': False, 'cancel_started': False, 'cancel_passed': False,
                    'analysis_started': False, 'last_wall_send': 0}
                self.save()
        except Exception:
            self.__exit__(None, None, None)
            raise LiveGuardError('invalid_ledger_no_reset') from None
        return self

    def __exit__(self, *args):
        if self.lock_file:
            unlock_file(self.lock_file)
            self.lock_file.close()
            self.lock_file = None

    def save(self):
        dump(self.file, self.data)

    def resume_after_endpoint_fix(self):
        """Explicit continuation after revalidating recorded real endpoint evidence."""
        state = self.data
        if (state['halted'] != 'no_verified_route' or not state['map_checked']
                or state['cancel_started'] or state['analysis_started']
                or state['counts']['cancel'] or state['counts']['analysis']):
            raise LiveGuardError('resume_not_allowed')
        events = [e for e in state['events'] if e.get('diagnostic')]
        if len(events) != 1:
            raise LiveGuardError('resume_requires_diagnostic_evidence')
        event = events[0]
        try:
            assert event['http_status'] == 200 and event['baidu_status'] == 0
            assert event['origin'] == list(ORIGIN) and event['destination'] == list(diagnostic_destination())
            assert event['phase'] == 'smoke' and len(event['endpoint_structure']) == 1
            evidence = event['endpoint_structure'][0]
            def point(which, field):
                fields = evidence[which]['fields'][field]['fields']
                return {axis: fields[axis]['value'] for axis in ('lng', 'lat')}
            payload = {'status': 0, 'result': {'routes': [{'duration': event['duration'], 'steps': [{
                'start_location': point('first', 'start_location'), 'end_location': point('last', 'end_location')}]}]}}
            observation = BaiduProvider('offline-resume-only').parse(payload, ORIGIN, diagnostic_destination())
            assert observation.duration is not None and observation.endpoint_verified
        except (AssertionError, KeyError, TypeError, ValueError):
            raise LiveGuardError('resume_endpoint_revalidation_failed') from None
        state.setdefault('resumptions', []).append({'time': datetime.now(timezone.utc).isoformat(),
            'previous_halt': state['halted'], 'evidence_event_id': event['id'],
            'reason': 'user_requested_continuation_after_endpoint_parser_fix',
            'route_origin': observation.route_origin, 'route_destination': observation.route_destination,
            'counts_preserved': dict(state['counts'])})
        state['halted'] = None
        state['smoke_passed'] = True
        self.save()

    def reserve(self, phase, request, monotonic, *, diagnostic=False):
        if diagnostic and (phase != 'smoke' or self.data['halted'] != 'no_verified_route'
                or any(e.get('diagnostic') for e in self.data['events'])):
            raise LiveGuardError('diagnostic_not_allowed')
        if (self.data['halted'] and not diagnostic) or phase not in self.phase_limits:
            raise LiveGuardError('live_test_halted_or_unarmed')
        if self.data['counts'][phase] >= self.phase_limits[phase] or sum(self.data['counts'].values()) >= self.total_limit:
            raise LiveGuardError('live_budget_exhausted')
        if (request.method != 'GET' or request.url.host != 'api.map.baidu.com'
                or request.url.path != '/directionlite/v1/walking'):
            raise LiveGuardError('unexpected_live_endpoint')
        params = request.url.params
        def point(name):
            lat, lng = map(float, params[name].split(','))
            return list(normalize((lng, lat)))
        event = {'id': len(self.data['events']) + 1, 'phase': phase,
            'reservedAt': datetime.now(timezone.utc).isoformat(), 'sendMonotonic': monotonic,
            'origin': point('origin'), 'destination': point('destination'),
            'bd09_request': params.get('coord_type') == 'bd09ll' and params.get('ret_coordtype') == 'bd09ll',
            'steps_requested': params.get('steps_info') == '1', 'outcome': 'reserved'}
        if not event['bd09_request'] or not event['steps_requested']:
            raise LiveGuardError('unexpected_route_parameters')
        if diagnostic:
            previous = [e for e in self.data['events'] if e['destination'] == event['destination']]
            if (event['origin'] != list(ORIGIN) or event['destination'] != list(diagnostic_destination())
                    or len(previous) != 1 or previous[0]['phase'] != 'smoke'):
                raise LiveGuardError('diagnostic_requires_second_attempt_at_fixed_point')
            event['diagnostic'] = True
        self.data['counts'][phase] += 1
        self.data['last_wall_send'] = time.time()
        self.data['events'].append(event)
        self.save()  # Reserve durably before handing the request to the network.
        return event


class GuardedTransport(httpx.AsyncBaseTransport):
    def __init__(self, ledger, inner, *, clock=time.monotonic, sleep=asyncio.sleep, enforce_spacing=True):
        self.ledger, self.inner = ledger, inner
        self.clock, self.sleep = clock, sleep
        self.lock = asyncio.Lock()
        self.next_send = clock() + max(0, 1/3 - (time.time() - ledger.data['last_wall_send']))
        self.phase = None
        self.cancelled = False
        self.temporary_streak = 0
        self.diagnostic = False
        self.enforce_spacing = enforce_spacing

    async def handle_async_request(self, request):
        async with self.lock:
            if self.enforce_spacing:
                await self.sleep(max(0, self.next_send - self.clock()))
            while self.enforce_spacing and self.clock() < self.next_send and not self.cancelled:
                await self.sleep(max(self.next_send - self.clock(), time.get_clock_info('monotonic').resolution))
            if self.cancelled:
                raise LiveGuardError('cancelled_before_send')
            event = self.ledger.reserve(self.phase, request, self.clock(), diagnostic=self.diagnostic)
            self.next_send = self.clock() + 1/3
        start = self.clock()
        event['outcome'] = 'dispatched'
        self.ledger.save()
        try:
            response = await self.inner.handle_async_request(request)
            await response.aread()
            event['http_status'] = response.status_code
            try:
                payload = response.json()
            except ValueError:
                payload = None
            if isinstance(payload, dict) and type(payload.get('status')) is int:
                event['baidu_status'] = payload['status']
            else:
                event['baidu_status'] = None
            observation = BaiduProvider('offline-parser-only').parse(payload, tuple(event['origin']), tuple(event['destination']))
            code = response.status_code
            reason = observation.reason
            if code != 200:
                reason = 'permission' if code in (401, 403) else 'invalid_parameter' if code == 400 else 'rate_limit' if code == 429 else 'temporary' if code >= 500 else 'http_error'
            event.update({'outcome': reason or 'success', 'duration': observation.duration,
                'endpoint_verified': observation.endpoint_verified, 'route_origin': observation.route_origin,
                'route_destination': observation.route_destination})
            # Keep only numeric endpoint evidence, not raw messages or step/URL text.
            routes = payload.get('result', {}).get('routes', []) if isinstance(payload, dict) and isinstance(payload.get('result'), dict) else []
            event['route_count'] = len(routes) if isinstance(routes, list) else 0
            event['route_endpoints'] = []
            for route in routes[:20] if isinstance(routes, list) else []:
                steps = route.get('steps') if isinstance(route, dict) else None
                if self.diagnostic:
                    event.setdefault('endpoint_structure', []).append(endpoint_structure(steps))
                if isinstance(steps, list) and steps:
                    event['route_endpoints'].append({'start': BaiduProvider._endpoint(steps[0].get('start_location')) if isinstance(steps[0], dict) else None,
                        'end': BaiduProvider._endpoint(steps[-1].get('end_location')) if isinstance(steps[-1], dict) else None})
            if reason in ('permission', 'quota', 'invalid_parameter'):
                self.ledger.data['halted'] = reason
            self.temporary_streak = self.temporary_streak + 1 if reason in ('temporary', 'timeout', 'rate_limit') else 0
            if self.temporary_streak >= 10:
                self.ledger.data['halted'] = 'continuous_upstream_failure'
            return response
        except asyncio.CancelledError:
            event['outcome'] = 'cancelled_in_flight'
            raise
        except httpx.RequestError as exc:
            event['outcome'] = 'timeout' if isinstance(exc, httpx.TimeoutException) else 'network_error'
            self.temporary_streak += 1
            if self.temporary_streak >= 10:
                self.ledger.data['halted'] = 'continuous_upstream_failure'
            raise
        finally:
            event['elapsed_seconds'] = max(0, self.clock() - start)
            self.ledger.save()

    async def aclose(self):
        await self.inner.aclose()


class Session:
    def __init__(self, ledger, run_dir, settings, *, transport=None):
        self.ledger, self.run_dir, self.settings = ledger, run_dir, settings
        self.guard = GuardedTransport(ledger, transport if transport is not None else httpx.AsyncHTTPTransport(retries=0, trust_env=False))
        self.client = httpx.AsyncClient(transport=self.guard, trust_env=False, follow_redirects=False)
        self.provider = LimitedProvider(
            BaiduProvider(settings.baidu_map_ak.get_secret_value(), client=self.client),
            RateGate(settings.analysis_qps))
        self.smoke_lock = asyncio.Lock()

    def save(self, name, value):
        dump(self.run_dir / name, value)

    async def smoke(self):
        async with self.smoke_lock:
            state = self.ledger.data
            if state['halted'] or not state['map_checked'] or state['smoke_started']:
                return JSONResponse({'error': 'smoke_blocked_or_already_started'}, status_code=409)
            state['smoke_started'] = True
            self.ledger.save()
            self.guard.phase = 'smoke'
            projection = LocalProjection(ORIGIN)
            offsets = [(d, 0) for d in (200, -200)] + [(0, d) for d in (200, -200)] + [(d, 0) for d in (600, -600)] + [(0, d) for d in (600, -600)]
            request = IsochroneRequest(ORIGIN, 'bd09ll', budget=16, qps=3, concurrency=1)
            scheduler = Scheduler(request, self.provider, CancelToken())
            observations = await scheduler.observe_many([projection.to_geographic(p) for p in offsets])
            passed = not state['halted'] and any(o.duration is not None and o.endpoint_verified for o in observations)
            state['smoke_passed'] = passed
            if not passed and not state['halted']:
                state['halted'] = 'no_verified_route'
            self.guard.phase = None
            self.ledger.save()
            result = {'passed': passed, 'stop_reason': state['halted'] or scheduler.stop_reason,
                'counts': dict(state['counts']), 'samples': [{'destination': o.destination, 'duration': o.duration,
                'reason': o.reason, 'attempts': o.attempts, 'endpoint_verified': o.endpoint_verified,
                'route_origin': o.route_origin, 'route_destination': o.route_destination} for o in observations]}
            self.save('T01-smoke.json', result)
            return result


def build_app(session, browser_ak):
    app = create_app(session.settings, provider_factory=lambda _: session.provider)
    original_lifespan = app.router.lifespan_context
    from contextlib import asynccontextmanager
    @asynccontextmanager
    async def lifespan(app):
        try:
            async with original_lifespan(app):
                yield
        finally:
            await session.client.aclose()
    app.router.lifespan_context = lifespan

    @app.middleware('http')
    async def gate_tasks(request, call_next):
        state = session.ledger.data
        path = request.url.path
        if request.method == 'POST' and path == '/api/analyses':
            if state['halted'] or not state['smoke_passed']:
                return JSONResponse({'detail': 'live_test_not_ready'}, status_code=409)
            body = await request.json()
            if body.get('center') != {'lng': ORIGIN[0], 'lat': ORIGIN[1]} or body.get('budget') != 200:
                return JSONResponse({'detail': 'fixed_test_center_and_budget_required'}, status_code=422)
            phase = 'analysis' if state['cancel_passed'] else 'cancel'
            if state[phase + '_started']:
                return JSONResponse({'detail': 'phase_already_started_no_new_task'}, status_code=409)
            state[phase + '_started'] = True
            session.guard.phase = phase
            session.guard.cancelled = False
            session.ledger.save()
        if request.method == 'POST' and path.startswith('/api/analyses/') and path.endswith('/cancel'):
            session.guard.cancelled = True
            state['cancel_observed_at'] = time.monotonic()
            session.ledger.save()
        response = await call_next(request)
        return response

    @app.get('/live/status')
    async def status():
        return {k: v for k, v in session.ledger.data.items() if k != 'events'}

    @app.post('/live/confirm-map')
    async def confirm_map():
        session.ledger.data['map_checked'] = True
        session.ledger.save()
        return {'map_checked': True}

    @app.post('/live/smoke')
    async def smoke():
        return await session.smoke()

    @app.post('/live/finish-cancel')
    async def finish_cancel():
        jobs = list(app.state.analyses.jobs.values())
        state = session.ledger.data
        late_sends = [e for e in state['events'] if e['phase'] == 'cancel' and e['sendMonotonic'] > state.get('cancel_observed_at', float('inf'))]
        passed = len(jobs) == 1 and jobs[0].status == 'cancelled' and jobs[0].result is None and not late_sends and not state['halted']
        result = {'passed': passed, 'jobs': [j.view() for j in jobs], 'late_sends': len(late_sends), 'counts': dict(state['counts'])}
        state['cancel_passed'] = passed
        if not passed:
            state['halted'] = state['halted'] or 'cancel_validation_failed'
        session.guard.phase = None
        session.ledger.save()
        session.save('T02-cancel.json', result)
        return result

    @app.get('/live/map', response_class=HTMLResponse)
    async def map_page():
        template = Path(__file__).with_name('live_map.html').read_text(encoding='utf-8')
        return HTMLResponse(template.replace('__BROWSER_AK_JSON__', json.dumps(browser_ak)), headers={'Cache-Control': 'no-store'})

    @app.get('/live/export')
    async def export():
        jobs = list(app.state.analyses.jobs.values())
        data = [{'status': j.view(), 'result': j.result} for j in jobs]
        session.save('tasks.json', data)
        return {'saved': len(data)}

    return app


def main():
    parser = argparse.ArgumentParser(description='Guarded real walking test; startup sends no walking requests')
    parser.add_argument('--output', type=Path, default=Path('D:/CodexOutputs/guodingyi-live-smoke'))
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--port', type=int, default=8019)
    parser.add_argument('--diagnose-endpoint', action='store_true',
        help='One explicit second attempt at east 200 m; keeps the stop flag and original budgets')
    parser.add_argument('--resume-after-endpoint-fix', action='store_true',
        help='Revalidate stored real endpoint evidence and resume pending phases without resetting budgets')
    args = parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', args.run_id):
        raise SystemExit('Invalid run ID')
    silence_transport_logs()
    try:
        settings = Settings()
        assert settings.analysis_provider == 'baidu' and settings.ak_configured and settings.analysis_qps == 3
        frontend = dotenv_values(Path(__file__).resolve().parents[2] / 'life-circle-demo/.env.local')
        browser_ak = (frontend.get('VITE_BAIDU_MAP_AK') or '').strip()
        assert browser_ak
        with Ledger(args.output) as ledger:
            if args.resume_after_endpoint_fix and args.diagnose_endpoint:
                raise LiveGuardError('conflicting_modes')
            if args.diagnose_endpoint and (ledger.data['halted'] != 'no_verified_route'
                    or any(e.get('diagnostic') for e in ledger.data['events'])):
                raise LiveGuardError('diagnostic_not_allowed_or_already_used')
            run_dir = args.output / args.run_id
            run_dir.mkdir(parents=True, exist_ok=False)
            if args.resume_after_endpoint_fix:
                ledger.resume_after_endpoint_fix()
            session = Session(ledger, run_dir, settings)
            sha = subprocess.run(['git', 'rev-parse', 'HEAD'], capture_output=True, text=True, check=True).stdout.strip()
            session.save('config.json', {'origin': ORIGIN, 'coordinateSystem': 'bd09ll', 'qps': 3,
                'limits': LIMITS, 'total_limit': TOTAL, 'run_id': args.run_id, 'base_commit': sha,
                'started_utc': datetime.now(timezone.utc).isoformat(),
                'versions': {p: importlib.metadata.version(p) for p in ('fastapi', 'httpx', 'numpy', 'shapely', 'contourpy')}})
            if args.diagnose_endpoint:
                async def diagnose():
                    session.guard.phase = 'smoke'
                    session.guard.diagnostic = True
                    try:
                        observation = await session.provider.query_walking_time(ORIGIN, diagnostic_destination(), time.monotonic() + 8)
                        session.save('endpoint-diagnostic.json', {'duration': observation.duration,
                            'endpoint_verified': observation.endpoint_verified, 'reason': observation.reason,
                            'counts': ledger.data['counts'], 'halted': ledger.data['halted'],
                            'event': next((e for e in ledger.data['events'] if e.get('diagnostic')), None)})
                    finally:
                        await session.client.aclose()
                asyncio.run(diagnose())
                return
            import uvicorn
            uvicorn.run(build_app(session, browser_ak), host='127.0.0.1', port=args.port,
                access_log=False, log_level='critical')
    except Exception:
        raise SystemExit('Live harness stopped; configuration, ledger or port unavailable. Sensitive details suppressed.') from None


if __name__ == '__main__':
    main()
