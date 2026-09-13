"""Explicit one-time continuation authorized after the v2 recording interruption."""
import argparse
import asyncio
from collections import Counter
from contextlib import ExitStack
from datetime import datetime, timezone
from importlib.metadata import version
import json
from pathlib import Path
import subprocess
import sys

import httpx

from app.baidu import silence_transport_logs
from app.config import Settings
from tools.comparison_audit import file_hash
from tools.live_boundary_comparison import BoundaryLedger, run_boundary, ROOT as PARENT
from tools.live_comparison import ExperimentLedger, METHODS, digest, phase_accounting
from tools.live_smoke import Ledger, LiveGuardError, dump
from tools.qps_review import QpsLedger

ROOT=Path('D:/CodexOutputs/guodingyi-boundary-comparison-v2-continuation')
PARENT_LEDGER_SHA256='8a42614afab51d5631cbe7e8917c92c11bce2e9a5235a5c97374d2091c4d24fc'
POINTS_SHA256='f079d6a312934204a55dfbc6ce74b0968b0971998eaa6f84ffa830351cffb436'


def plan_continuation(protocol,ledger,references):
    expected={'reference':82,'adaptive':0,'uniform':0,'radial':0}
    if ledger['halted']!='ledger_write_failed' or ledger['counts']!=expected:
        raise LiveGuardError('parent_state_not_resumable')
    events=ledger['events']
    if len(events)!=82 or Counter(e['phase'] for e in events)!=Counter({'reference':82}):
        raise LiveGuardError('parent_event_count_mismatch')
    sent={tuple(e['destination']) for e in events if 'dispatchPerf' in e}
    targets={tuple(p['point']) for p in protocol['validation']}
    refs={tuple(s['point']):s for s in references}
    if len(sent)!=81 or len(references)!=96 or set(refs)!=targets:
        raise LiveGuardError('parent_reference_mismatch')
    if len({e['id'] for e in events})!=82 or not sent<=targets:
        raise LiveGuardError('parent_event_identity_mismatch')
    previous=Counter(tuple(e['destination']) for e in events)
    pending=[{**p,'max_attempts':2-previous[tuple(p['point'])],
              'previous_reservations':previous[tuple(p['point'])]} for p in protocol['validation'] if tuple(p['point']) not in sent]
    if len(pending)!=15 or sum(p['max_attempts'] for p in pending)!=29:
        raise LiveGuardError('continuation_population_changed')
    retained=[]
    for p in protocol['validation']:
        point=tuple(p['point'])
        matching=[e for e in events if tuple(e['destination'])==point and 'dispatchPerf' in e]
        retained.append({**refs[point],'collection_run':PARENT.name,
                         'prior_confirmed_calls':len(matching),
                         'collected_at_utc':matching[-1].get('reservedAt') if matching else None})
    limits={'reference':29,'adaptive':400,'uniform':400,'radial':400}
    return {'protocol_sha256':digest(protocol),'pending':pending,'parent_references':retained,
            'parent_counts':dict(expected),'parent_accounting':{p:phase_accounting(events,p) for p in expected},
            'new_limits':limits,'new_total_limit':1229,'cumulative_maximum':1311,
            'authorization':'user explicitly requested continuation after recording fix; no automatic restart'}


class ContinuationLedger(BoundaryLedger):
    phase_limits={'reference':29,'adaptive':400,'uniform':400,'radial':400}
    total_limit=1229

    def __init__(self,root,plan):
        super().__init__(root)
        self.pending={tuple(p['point']):p for p in plan['pending']}

    def reserve(self,phase,request,monotonic,*,diagnostic=False):
        lat,lng=map(float,request.url.params['destination'].split(','))
        point=(lng,lat)
        previous=0
        if phase=='reference':
            if point not in self.pending: raise LiveGuardError('reference_already_sent_or_not_fixed')
            spec=self.pending[point]
            current=sum(e['phase']==phase and tuple(e['destination'])==point for e in self.data['events'])
            if current>=spec['max_attempts']: raise LiveGuardError('cumulative_destination_attempt_budget')
            previous=spec['previous_reservations']
        event=super().reserve(phase,request,monotonic,diagnostic=diagnostic)
        event['previous_reservations']=previous
        event['cumulative_attempt']=event['attempt']+previous
        self.save()
        return event


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    group=parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--prepare',action='store_true')
    group.add_argument('--execute-live',action='store_true')
    args=parser.parse_args()
    silence_transport_logs()
    try:
        repo=Path(__file__).resolve().parents[2]
        def git(*args):return subprocess.check_output(['git','-C',str(repo),*args],text=True,stderr=subprocess.DEVNULL).strip()
        if file_hash(PARENT/'ledger.json')!=PARENT_LEDGER_SHA256: raise LiveGuardError('parent_ledger_changed')
        protocol=json.loads((PARENT/'protocol.json').read_text(encoding='utf-8'))
        if digest(protocol['validation'])!=POINTS_SHA256: raise LiveGuardError('fixed_points_changed')
        parent=json.loads((PARENT/'ledger.json').read_text(encoding='utf-8'))
        references=json.loads((PARENT/'reference.json').read_text(encoding='utf-8'))
        source_audit=json.loads((PARENT/'audited-result.json').read_text(encoding='utf-8'))
        source_hashes=source_audit['audit']['source_sha256']
        if not all(file_hash(PARENT/name)==h for name,h in source_hashes.items()):
            raise LiveGuardError('parent_original_artifacts_changed')
        plan=plan_continuation(protocol,parent,references)
        if args.prepare:
            if (ROOT/'ledger.json').exists() or (ROOT/'started.marker').exists(): raise LiveGuardError('continuation_already_started')
            if (ROOT/'continuation.json').exists() and json.loads((ROOT/'continuation.json').read_text(encoding='utf-8'))!=plan:
                raise LiveGuardError('continuation_plan_changed')
            dump(ROOT/'protocol.json',protocol)
            dump(ROOT/'continuation.json',plan)
            print(json.dumps({'pending':len(plan['pending']),'new_limit':1229,'cumulative_maximum':1311}))
            return
        if git('status','--porcelain') or git('diff','comparison-v1','--','life-circle-algorithm','backend/app'):
            raise LiveGuardError('tools_not_committed_or_algorithm_changed')
        if json.loads((ROOT/'continuation.json').read_text(encoding='utf-8'))!=plan:
            raise LiveGuardError('prepare_continuation_first')
        settings=Settings()
        if not(settings.analysis_provider=='baidu' and settings.ak_configured and settings.analysis_qps==3):
            raise LiveGuardError('invalid_live_configuration')
        with ExitStack() as stack:
            old=[]
            for cls,path in ((Ledger,Path('D:/CodexOutputs/guodingyi-live-smoke')),
                (QpsLedger,Path('D:/CodexOutputs/guodingyi-qps-review')),
                (QpsLedger,Path('D:/CodexOutputs/guodingyi-qps-response-paced-review')),
                (ExperimentLedger,Path('D:/CodexOutputs/guodingyi-stability-comparison-20260913')),
                (BoundaryLedger,PARENT)):
                old.append(stack.enter_context(cls(path)))
            hashes={str(p.file):file_hash(p.file) for p in old}
            ledger=stack.enter_context(ContinuationLedger(ROOT,plan))
            if ledger.data.get('experiment_started') or sum(ledger.data['counts'].values()) or (ROOT/'started.marker').exists():
                raise LiveGuardError('continuation_already_started_no_restart')
            dump(ROOT/'config.json',{'head':git('rev-parse','HEAD'),'comparison_v1':git('rev-parse','comparison-v1^{commit}'),
                'python':sys.version.split()[0],'packages':{p:version(p) for p in ('httpx','numpy','shapely','contourpy','fastapi')},
                'source_sha256':{p.relative_to(repo).as_posix():file_hash(p) for p in (repo/'backend/tools').glob('*.py')},
                'parent_original_sha256':source_hashes,'prior_ledgers':hashes,'protocol_sha256':digest(protocol),
                'continuation_sha256':digest(plan),'new_limits':plan['new_limits'],'new_total_limit':1229,
                'parent_reserved_attempts':82,'cumulative_maximum':1311,'started_utc':datetime.now(timezone.utc).isoformat()})
            result=asyncio.run(run_boundary(ledger,protocol,settings.baidu_map_ak.get_secret_value(),
                inner=httpx.AsyncHTTPTransport(retries=0,trust_env=False),continuation=plan,
                progress=lambda p:print(json.dumps(p),flush=True)))
            result['prior_ledgers_unchanged']=all(file_hash(Path(p))==h for p,h in hashes.items())
            dump(ROOT/'result.json',result)
            print(json.dumps({'counts':result['counts'],'cumulative_counts':result['cumulative_counts'],
                              'halted':result['halted'],'prior_ledgers_unchanged':result['prior_ledgers_unchanged']}))
    except Exception:
        raise SystemExit('Continuation stopped; sensitive details suppressed. Inspect sanitized evidence after process exit.') from None


if __name__=='__main__': main()
