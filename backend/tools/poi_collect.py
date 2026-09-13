"""B1 CLI. Plan/replay never instantiate a network provider or read credentials."""
import argparse
import asyncio
from contextlib import ExitStack
import json
from pathlib import Path
import sys

from app.persistence import atomic_dump
from app.poi.artifacts import export_artifacts
from app.poi.models import CollectionConfig
from app.poi.planner import build_plan
from app.poi.provider import ReplayProvider
from app.poi.runtime import PoiLedger, PoiRuntime
from app.poi.service import collect_pois
from app.request_control import RequestStopped

BACKEND = Path(__file__).resolve().parents[1]
LEDGER_ROOT = BACKEND / '.poi-ledgers'


async def execute(args, config, plan):
    live = args.mode.startswith('live-')
    expected_phase = 'smoke' if args.mode == 'live-smoke' else 'collect'
    if live and config.runtime.phase != expected_phase:
        raise RequestStopped('phase_mismatch')
    if args.output.exists():
        raise RequestStopped('output_already_exists')
    with ExitStack() as stack:
        runtime = PoiRuntime(config.runtime, plan, live=live)
        if live:
            # Check authorization before creating the one-shot authoritative ledger.
            try:
                runtime.authorize()
            except RequestStopped as exc:
                if exc.reason != 'ledger_required':
                    raise
            from app.config import load_settings
            from app.poi.provider import BaiduPoiProvider
            import httpx
            settings = load_settings()
            if not settings.ak_configured:
                raise RequestStopped('missing_ak')
            runtime.ledger = stack.enter_context(PoiLedger(LEDGER_ROOT / config.runtime.run_id, config.runtime, plan['configHash']))
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
                result = await collect_pois(config.request, BaiduPoiProvider(client, settings), runtime)
            secret = settings.baidu_map_ak.get_secret_value()
        else:
            if args.fixtures is None:
                raise RequestStopped('fixtures_required')
            result = await collect_pois(config.request, ReplayProvider.from_path(args.fixtures), runtime)
            secret = ''
        export_artifacts(args.output, config, plan, result, runtime, secret=secret)
        return {'mode': args.mode, 'queryStatus': result.query_status, 'output': str(args.output),
                'networkReservations': result.statistics['networkReservations']}, 0 if result.query_status == 'completed' else 2


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', required=True, choices=('plan', 'replay', 'live-smoke', 'live-collect'))
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--fixtures', type=Path, help='Synthetic response JSON file')
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        config = CollectionConfig.model_validate_json(args.config.read_text(encoding='utf-8-sig'))
        plan = build_plan(config.request, config.runtime)
        if args.mode == 'plan':
            if args.output.exists():
                raise RequestStopped('output_already_exists')
            args.output.mkdir(parents=True)
            atomic_dump(args.output / 'query-plan.json', plan)
            atomic_dump(args.output / 'config-sanitized.json', config.model_dump(mode='json', by_alias=True))
            outcome, code = {'mode': 'plan', 'configHash': plan['configHash'], 'sequences': len(plan['sequences']),
                             'requestUpperBound': plan['requestUpperBound'], 'networkReservations': 0}, 0
        else:
            outcome, code = asyncio.run(execute(args, config, plan))
        print(json.dumps(outcome, ensure_ascii=False))
        return code
    except RequestStopped as exc:
        print(json.dumps({'error': exc.reason, 'mode': args.mode}))
        return 1
    except KeyboardInterrupt:
        print('{"error":"cancelled"}')
        return 130
    except Exception:
        # Validation errors, filesystem paths and raw network exceptions can contain secrets.
        print('{"error":"configuration_or_execution_failed"}')
        return 1


if __name__ == '__main__':
    sys.exit(main())
