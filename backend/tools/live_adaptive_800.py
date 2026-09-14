"""One explicitly authorized adaptive run, at most 800 attempts; never resume."""
import argparse
import asyncio
from contextlib import ExitStack
from dataclasses import asdict
from datetime import datetime, timezone
from importlib.metadata import version
import json
from pathlib import Path
import subprocess
import sys
import time

import httpx

from app.analyses import LimitedProvider, RateGate
from app.baidu import silence_transport_logs
from app.config import Settings
from life_circle.engine import compute_isochrone
from life_circle.models import IsochroneRequest
from life_circle.providers import BaiduProvider
from life_circle.scheduler import Clock
from tools.comparison_audit import file_hash
from tools.continue_boundary_comparison import ContinuationLedger
from tools.live_boundary_comparison import BoundaryLedger, ProgressTransport
from tools.live_comparison import ExperimentLedger, phase_accounting
from tools.live_smoke import Ledger, LiveGuardError, ORIGIN, dump
from tools.qps_review import QpsLedger, metrics as timing_metrics

ROOT=Path('D:/CodexOutputs/guodingyi-adaptive-800-20260913')
SOURCE=Path('D:/CodexOutputs/guodingyi-boundary-comparison-v2-continuation')


class Adaptive800Ledger(BoundaryLedger):
    phase_limits={'adaptive':800}
    total_limit=800


def comparison_request(previous):
    original=IsochroneRequest(**previous)
    if (original.origin!=ORIGIN or original.budget!=400 or original.qps!=3 or original.expand
        or original.extent!=1600 or original.timeout!=8 or original.max_attempts!=2
        or original.deadline_seconds!=600 or original.seed!=20260911):
        raise LiveGuardError('unexpected_baseline_configuration')
    return IsochroneRequest(**{**asdict(original),'budget':800})


async def run_once(ledger,ak,request,*,inner,clock=None,gate=None,progress=None):
    if request.budget!=800 or ledger.phase_limits!={'adaptive':800} or ledger.total_limit!=800:
        raise LiveGuardError('unexpected_run_budget')
    clock=clock or Clock()
    ledger.clock=clock.time
    ledger.start_once()
    ledger.deadline=clock.time()+600
    transport=ProgressTransport(ledger,inner,clock=clock.time if gate is not None else time.perf_counter,progress=progress)
    transport.phase='adaptive'
    report={'status':'running','budget':800,'started_utc':datetime.now(timezone.utc).isoformat(),
        'request':asdict(request),'reference_collection':'not_run_existing_fixed_reference_only'}
    started=time.perf_counter()
    try:
        async with httpx.AsyncClient(transport=transport,trust_env=False,follow_redirects=False) as client:
            provider=LimitedProvider(BaiduProvider(ak,client=client),gate or RateGate(3))
            result=await compute_isochrone(request,provider,ledger.token,clock=clock)
            report.update(quality=result.quality,stop_reason=result.stop_reason)
            if ledger.token.cancelled:
                report['status']='cancelled'
            elif ledger.data['halted'] or result.stop_reason in ('geometry_error','upstream_failure'):
                report['status']='failed'
            else:
                report['status']='completed'
                dump(ledger.root/'adaptive.json',result.to_dict())
    except asyncio.CancelledError:
        ledger.token.cancel()
        ledger.data['halted']=ledger.data['halted'] or 'cancelled'
        report['status']='cancelled'
    except Exception:
        ledger.data['halted']=ledger.data['halted'] or 'algorithm_exception'
        report['status']='failed'
    finally:
        if ledger.token.cancelled:
            ledger.data['halted']=ledger.data['halted'] or 'cancelled'
        try:
            ledger.save()
        except Exception:
            ledger.data['halted']='ledger_write_failed'
            report['status']='failed'
        report.update(elapsed_seconds=time.perf_counter()-started,halted=ledger.data['halted'],
            accounting=phase_accounting(ledger.data['events'],'adaptive'),
            timing=timing_metrics(ledger.data['events']),finished_utc=datetime.now(timezone.utc).isoformat())
        dump(ledger.root/'run.json',report)
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute-live',action='store_true',required=True)
    parser.parse_args()
    silence_transport_logs()
    try:
        repo=Path(__file__).resolve().parents[2]
        def git(*args):
            return subprocess.check_output(['git','-C',str(repo),*args],text=True,stderr=subprocess.DEVNULL).strip()
        if git('diff','a147c81','--','life-circle-algorithm/src','backend/app'):
            raise LiveGuardError('algorithm_or_business_code_changed')
        if (ROOT/'started.marker').exists():
            raise LiveGuardError('already_started_no_restart')
        settings=Settings()
        if not(settings.analysis_provider=='baidu' and settings.ak_configured and settings.analysis_qps==3):
            raise LiveGuardError('invalid_live_configuration')
        previous=json.loads((SOURCE/'adaptive.json').read_text(encoding='utf-8'))
        request=comparison_request(previous['config'])
        with ExitStack() as stack:
            # Holding only the established lock file prevents concurrent harnesses.
            # Never hold a reader open on the active JSON ledger on Windows.
            paths=((Ledger,'guodingyi-live-smoke'),(QpsLedger,'guodingyi-qps-review'),
                (QpsLedger,'guodingyi-qps-response-paced-review'),
                (ExperimentLedger,'guodingyi-stability-comparison-20260913'),
                (BoundaryLedger,'guodingyi-boundary-comparison-v2'))
            old=[stack.enter_context(cls(Path('D:/CodexOutputs')/name)) for cls,name in paths]
            plan=json.loads((SOURCE/'continuation.json').read_text(encoding='utf-8'))
            old.append(stack.enter_context(ContinuationLedger(SOURCE,plan)))
            prior_hashes={str(o.file):file_hash(o.file) for o in old}
            ledger=stack.enter_context(Adaptive800Ledger(ROOT))
            if ledger.data.get('experiment_started') or sum(ledger.data['counts'].values()):
                raise LiveGuardError('already_started_no_restart')
            source_files=list((repo/'life-circle-algorithm/src/life_circle').glob('*.py'))
            source_files+=list((repo/'backend/app').glob('*.py'))+list((repo/'backend/tools').glob('*.py'))
            hashes={p.relative_to(repo).as_posix():file_hash(p) for p in source_files}
            for p in source_files:
                target=ROOT/'runtime-source'/p.relative_to(repo)
                target.parent.mkdir(parents=True,exist_ok=True)
                target.write_bytes(p.read_bytes())
            inputs={str(SOURCE/n):file_hash(SOURCE/n) for n in
                ('protocol.json','reference.json','adaptive.json','ledger.json','audited-point-evaluation.json')}
            dump(ROOT/'config.json',{'head':git('rev-parse','HEAD'),
                'algorithm_baseline':git('rev-parse','a147c81'),
                'runtime_source_sha256':hashes,'input_sha256':inputs,'prior_ledger_sha256':prior_hashes,
                'request':asdict(request),'total_limit':800,'effective_concurrency':1,
                'scope':'one adaptive run; no new reference collection; no automatic restart',
                'python':sys.version.split()[0],'packages':{p:version(p) for p in ('httpx','numpy','shapely','contourpy','fastapi')},
                'tool_version':'source snapshot and SHA-256; new entrypoint not committed',
                'created_utc':datetime.now(timezone.utc).isoformat()})
            report=asyncio.run(run_once(ledger,settings.baidu_map_ak.get_secret_value(),request,
                inner=httpx.AsyncHTTPTransport(retries=0,trust_env=False),
                progress=lambda p:print(json.dumps(p),flush=True)))
            unchanged=all(file_hash(Path(p))==h for p,h in {**prior_hashes,**inputs}.items())
            dump(ROOT/'source-integrity.json',{'prior_inputs_unchanged':unchanged,
                'runtime_source_unchanged':all(file_hash(repo/p)==h for p,h in hashes.items())})
            print(json.dumps({'status':report['status'],'accounting':report['accounting'],
                'halted':report['halted'],'prior_inputs_unchanged':unchanged}),flush=True)
    except Exception:
        raise SystemExit('Adaptive 800 run stopped; sensitive details suppressed. No automatic restart.') from None


if __name__=='__main__':
    main()
