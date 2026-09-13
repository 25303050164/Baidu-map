"""Fixed 96-point boundary reference experiment; 1392 reserved attempts maximum."""
import argparse
import asyncio
import csv
from collections import Counter
from contextlib import ExitStack
from datetime import datetime, timezone
from importlib.metadata import version
import json
import math
from pathlib import Path
import subprocess
import sys
import time

import httpx
from shapely.geometry import Point, box

from app.analyses import LimitedProvider, RateGate
from app.baidu import silence_transport_logs
from app.config import Settings
from life_circle.baselines import compute_radial
from life_circle.coordinates import normalize
from life_circle.engine import compute_isochrone
from life_circle.field import multipolygon
from life_circle.models import IsochroneRequest
from life_circle.providers import BaiduProvider
from life_circle.scheduler import Clock, Scheduler
from tools.comparison_audit import file_hash
from tools.live_comparison import (ExperimentLedger, ComparisonTransport, METHODS, PROJECTION,
    digest, valid_reference, phase_accounting, point_metrics, sample_record, write_artifacts)
from tools.live_smoke import Ledger, LiveGuardError, ORIGIN, dump
from tools.qps_review import QpsLedger, metrics as timing_metrics

ROOT=Path('D:/CodexOutputs/guodingyi-boundary-comparison-v2')
SOURCE=Path('D:/CodexOutputs/guodingyi-stability-comparison-20260913')
LIMITS={'reference':192,'adaptive':400,'uniform':400,'radial':400}


class BoundaryLedger(ExperimentLedger):
    phase_limits=LIMITS
    total_limit=1392

    def save(self):
        try:
            Ledger.save(self)
        except OSError as error:
            self.data['halted']='ledger_write_failed'
            self.data.setdefault('recording_error',{'operation':'ledger_atomic_save',
                'errno':error.errno,'winerror':getattr(error,'winerror',None),
                'event_id':len(self.data['events'])})
            raise LiveGuardError('ledger_write_failed') from None


class ProgressTransport(ComparisonTransport):
    """Observe stdout progress; do not open the live ledger from another process."""
    def __init__(self,*args,progress=None,**kwargs):
        super().__init__(*args,**kwargs)
        self.progress=progress

    async def handle_async_request(self,request):
        response=await super().handle_async_request(request)
        counts=phase_accounting(self.ledger.data['events'],self.phase)
        if self.progress and (counts['actual_calls']==1 or counts['actual_calls']%20==0):
            self.progress({'stage':self.phase,'status':'progress',**counts})
        return response


def make_boundary_protocol(old_protocol, references):
    if old_protocol['origin']!=list(ORIGIN) or old_protocol['coordinateSystem']!='bd09ll':
        raise ValueError('reference_coordinate_mismatch')
    by_point={tuple(s['point']):s for s in references}
    if len(by_point)!=256 or len(old_protocol['validation'])!=256:
        raise ValueError('reference_source_incomplete')
    cells={tuple(p['cell']):p for p in old_protocol['validation']}
    if set(cells)!={(x,y) for x in range(16) for y in range(16)}:
        raise ValueError('reference_cells_invalid')
    pairs,validation=[],[]
    for cell,a in sorted(cells.items()):
        for dx,dy in ((1,0),(0,1)):
            b=cells.get((cell[0]+dx,cell[1]+dy))
            if b is None: continue
            labels=[by_point[tuple(p['point'])] for p in (a,b)]
            if not all(valid_reference(s) and type(s['duration']) in (int,float)
                       and math.isfinite(s['duration']) and s['duration']>=0 for s in labels): continue
            if (labels[0]['duration']<=900)==(labels[1]['duration']<=900): continue
            pair_id=len(pairs)+1
            pairs.append({'id':pair_id,'cells':[a['cell'],b['cell']], 'points':[a['point'],b['point']],
                          'durations':[s['duration'] for s in labels]})
            for fraction in (.2,.4,.6,.8):
                local=tuple((1-fraction)*x+fraction*y for x,y in zip(a['local'],b['local']))
                point=normalize(PROJECTION.to_geographic(local))
                xy=PROJECTION.to_local(point)
                if tuple(point) in by_point or max(map(abs,xy))>1600:
                    raise ValueError('candidate_overlap_or_outside')
                validation.append({'id':len(validation)+1,'pair_id':pair_id,'fraction':fraction,
                                   'point':list(point),'local':list(xy)})
    if len(pairs)!=24 or len(validation)!=96 or len({tuple(p['point']) for p in validation})!=96:
        raise ValueError('fixed_boundary_population_changed')
    return {'origin':list(ORIGIN),'coordinateSystem':'bd09ll','threshold':900,'extent':1600,
            'expand':False,'qps':3,'effective_concurrency':1,'timeout':8,'max_attempts':2,
            'deadline_seconds':600,'seed':20260911,'limits':dict(LIMITS),'total_limit':1392,
            'order':list(METHODS),'pairs':pairs,'validation':validation,
            'validation_sha256':digest(validation),'source_protocol_sha256':digest(old_protocol),
            'source_reference_sha256':digest(sorted(references,key=lambda s:tuple(s['point']))),
            'selection':'four_neighbor_opposite_valid_labels; fractions .2,.4,.6,.8; no algorithm input'}


def reference_gate(references, *, require_complete=True):
    valid=[s for s in references if valid_reference(s)]
    positive=sum(s['duration']<=900 for s in valid)
    negative=len(valid)-positive
    return {'passed':(not require_complete or len(references)==96) and len(valid)>=60 and min(positive,negative)>=10,
            'count':len(references),'valid':len(valid),'invalid':len(references)-len(valid),
            'reachable':positive,'unreachable':negative}


def pair_coverage(references, excluded=()):
    excluded=set(excluded)
    return [{'pair_id':i,**dict(Counter('excluded' if tuple(s['point']) in excluded else
             'invalid' if not valid_reference(s) else 'reachable' if s['duration']<=900 else 'unreachable'
             for s in references if s['pair_id']==i))} for i in range(1,25)]


async def run_boundary(ledger, protocol, ak, *, inner, clock=None, gate=None, progress=None, continuation=None):
    clock=clock or Clock()
    ledger.clock=clock.time
    transport=ProgressTransport(ledger,inner,clock=clock.time if gate is not None else time.perf_counter,progress=progress)
    shared_gate=gate or RateGate(3)
    if protocol['limits']!=LIMITS or protocol['order']!=list(METHODS):
        raise LiveGuardError('protocol_mismatch')
    if (ledger.root/'protocol.json').exists():
        if json.loads((ledger.root/'protocol.json').read_text(encoding='utf-8'))!=protocol:
            raise LiveGuardError('frozen_protocol_mismatch')
    else: dump(ledger.root/'protocol.json',protocol)
    ledger.start_once()
    report={'stages':{p:{'status':'not_run'} for p in LIMITS},'metrics':[],
            'protocol_sha256':digest(protocol),'started_utc':ledger.data['started_utc']}
    results,references={},[]
    def begin(phase):
        ledger.deadline=clock.time()+600
        ledger.batch=0
        transport.phase=phase
        report['stages'][phase]={'status':'running'}
        if progress: progress({'stage':phase,'status':'starting'})
    async with httpx.AsyncClient(transport=transport,trust_env=False,follow_redirects=False) as client:
        provider=LimitedProvider(BaiduProvider(ak,client=client),shared_gate)
        try:
            begin('reference')
            started=clock.time()
            if continuation is None:
                request=IsochroneRequest(ORIGIN,'bd09ll',budget=192,qps=3,concurrency=1,expand=False)
                scheduler=Scheduler(request,provider,ledger.token,clock=clock)
                observations=await scheduler.observe_many([tuple(p['point']) for p in protocol['validation']])
                for p,o in zip(protocol['validation'],observations):
                    if tuple(p['point'])!=o.destination: raise LiveGuardError('reference_order_mismatch')
                    references.append(sample_record(o,id=p['id'],pair_id=p['pair_id'],fraction=p['fraction']))
            else:
                if continuation['protocol_sha256']!=digest(protocol): raise LiveGuardError('continuation_protocol_changed')
                merged={tuple(s['point']):dict(s) for s in continuation['parent_references']}
                for p in continuation['pending']:
                    if clock.time()>=ledger.deadline:
                        ledger.data['halted']=ledger.data['halted'] or 'reference_deadline'
                    if ledger.data['halted'] or ledger.token.cancelled: break
                    request=IsochroneRequest(ORIGIN,'bd09ll',budget=p['max_attempts'],max_attempts=p['max_attempts'],
                        qps=3,concurrency=1,expand=False,deadline_seconds=ledger.deadline-clock.time())
                    scheduler=Scheduler(request,provider,ledger.token,clock=clock)
                    observation=await scheduler.query(tuple(p['point']))
                    merged[tuple(p['point'])]=sample_record(observation,id=p['id'],pair_id=p['pair_id'],fraction=p['fraction'],
                        collection_run=ledger.root.name,collected_at_utc=datetime.fromtimestamp(observation.collected_at,timezone.utc).isoformat())
                references=[merged[tuple(p['point'])] for p in protocol['validation']]
            dump(ledger.root/'reference.json',references)
            coverage=reference_gate(references)
            if 'scheduler' in locals() and scheduler.stop_reason in ('cancelled','upstream_failure'):
                ledger.data['halted']=ledger.data['halted'] or scheduler.stop_reason
            passed=coverage['passed'] and not ledger.data['halted']
            report['stages']['reference']={**coverage,'coverage_passed':coverage['passed'],'passed':passed,
                                          'status':'passed' if passed else 'failed',
                                          'elapsed_seconds':clock.time()-started}
            if not passed: ledger.data['halted']=ledger.data['halted'] or 'reference_coverage_insufficient'
            for method in protocol['order']:
                if ledger.data['halted'] or ledger.token.cancelled: break
                begin(method)
                started=clock.time()
                request=IsochroneRequest(ORIGIN,'bd09ll',budget=400,qps=3,expand=False)
                try:
                    if method=='radial': result=await compute_radial(request,provider,ledger.token,clock=clock)
                    else: result=await compute_isochrone(request,provider,ledger.token,clock=clock,method=method)
                    dump(ledger.root/f'{method}.json',result.to_dict())
                    if result.stop_reason in ('cancelled','upstream_failure'):
                        ledger.data['halted']=ledger.data['halted'] or result.stop_reason
                    failed=result.stop_reason=='geometry_error' or bool(ledger.data['halted'])
                    if not failed: results[method]=result
                    report['stages'][method]={'status':'failed' if failed else 'completed',
                        'quality':result.quality,'stop_reason':result.stop_reason,'elapsed_seconds':clock.time()-started,
                        'provider_failures':dict(result.statistics.failures)}
                except asyncio.CancelledError: raise
                except Exception:
                    report['stages'][method]={'status':'failed','reason':'algorithm_exception',
                                             'elapsed_seconds':clock.time()-started}
                if progress: progress({'stage':method,'status':report['stages'][method]['status'],
                                       **phase_accounting(ledger.data['events'],method)})
        except asyncio.CancelledError:
            ledger.token.cancel()
            ledger.data['halted']=ledger.data['halted'] or 'cancelled'
        except Exception:
            ledger.data['halted']=ledger.data['halted'] or 'experiment_error'
        finally:
            if ledger.token.cancelled: ledger.data['halted']=ledger.data['halted'] or 'cancelled'
            for state in report['stages'].values():
                if state['status']=='running': state.update(status='failed',reason=ledger.data['halted'] or 'interrupted')
            sampled={tuple(e['destination']) for e in ledger.data['events'] if e['phase'] in METHODS}
            for method in METHODS:
                result=results.get(method)
                geometry=result.local_geometry if result else None
                unknown=result.local_unknown if result else box(-1600,-1600,1600,1600)
                report['metrics'].append({'method':method,**report['stages'][method],
                    **phase_accounting(ledger.data['events'],method),**point_metrics(geometry,unknown,references,sampled),
                    'unknown_area_fraction':unknown.area/(3200**2) if unknown is not None else None,
                    'components':len(multipolygon(geometry).geoms) if geometry is not None else None,
                    'holes':sum(len(p.interiors) for p in multipolygon(geometry).geoms) if geometry is not None else None})
                if not (ledger.root/f'{method}.json').exists():
                    dump(ledger.root/f'{method}.json',{'status':report['stages'][method]['status'],'isochrone':None})
            point_rows=[]
            for sample in references:
                row={**sample,'excluded_overlap':tuple(sample['point']) in sampled,'predictions':{}}
                for method in METHODS:
                    result=results.get(method)
                    p=Point(sample['local'])
                    row['predictions'][method]='unknown' if result is None or result.local_geometry is None or (
                        result.local_unknown is not None and result.local_unknown.covers(p)) else (
                        'reachable' if result.local_geometry.covers(p) else 'unreachable')
                point_rows.append(row)
            dump(ledger.root/'point-evaluation.json',point_rows)
            report.update(counts=dict(ledger.data['counts']),total_limit=ledger.total_limit,halted=ledger.data['halted'],
                accounting={p:phase_accounting(ledger.data['events'],p) for p in LIMITS},
                timing=timing_metrics(ledger.data['events']),pair_coverage=pair_coverage(references,sampled),
                post_overlap_coverage=reference_gate([s for s in references if tuple(s['point']) not in sampled],require_complete=False),
                finished_utc=datetime.now(timezone.utc).isoformat())
            if continuation is not None:
                report['parent_counts']=continuation['parent_counts']
                report['parent_accounting']=continuation['parent_accounting']
                report['cumulative_counts']={p:continuation['parent_counts'][p]+ledger.data['counts'][p] for p in LIMITS}
                report['cumulative_maximum']=continuation['cumulative_maximum']
            ledger.save()
            write_artifacts(ledger.root,report,results,references)
    return report


def audit_boundary(root):
    """Additive post-run audit: reconcile scheduler placeholders with handoff evidence."""
    root=Path(root)
    source_names=['config.json','protocol.json','ledger.json','reference.json','result.json','metrics.json',
                  'metrics.csv','point-evaluation.json',*[f'{m}.json' for m in METHODS]]
    hashes={name:file_hash(root/name) for name in source_names}
    ledger=json.loads((root/'ledger.json').read_text(encoding='utf-8'))
    report=json.loads((root/'result.json').read_text(encoding='utf-8'))
    points=json.loads((root/'point-evaluation.json').read_text(encoding='utf-8'))
    state=report['stages']['reference']
    state['coverage_passed']=reference_gate(points)['passed']
    state['passed']=state['status']=='passed' and state['coverage_passed'] and not report['halted']
    for sample in points:
        events=[e for e in ledger['events'] if e['phase']=='reference' and e['destination']==sample['point']]
        actual=[e for e in events if 'dispatchPerf' in e]
        inherited=sample.get('prior_confirmed_calls',0)
        sample['measurement_status']=('valid' if valid_reference(sample) else 'invalid_reference') if actual or inherited else (
            'reserved_without_confirmed_send' if events else 'not_sent_after_stop')
        sample['confirmed_calls']=len(actual)+inherited
        sample['inherited_confirmed_calls']=inherited
        sample['transport_outcomes']=[e['outcome'] for e in actual]
    report['reference_measurement_status']=dict(Counter(p['measurement_status'] for p in points))
    report['audit']={'source_sha256':hashes,'audit_tool_sha256':file_hash(Path(__file__)),
                     'note':'original artifacts preserved; coverage_passed differs from stage passed'}
    dump(root/'audited-result.json',report)
    dump(root/'audited-point-evaluation.json',points)
    dump(root/'audited-metrics.json',report['metrics'])
    with (root/'audited-metrics.csv').open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=list(dict.fromkeys(k for row in report['metrics'] for k in row)))
        writer.writeheader()
        writer.writerows({k:json.dumps(v,ensure_ascii=False) if isinstance(v,(list,dict)) else v for k,v in row.items()} for row in report['metrics'])
    if any(file_hash(root/name)!=h for name,h in hashes.items()): raise ValueError('original_artifact_changed')
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    group=parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--prepare',action='store_true')
    group.add_argument('--execute-live',action='store_true')
    group.add_argument('--audit',action='store_true')
    args=parser.parse_args()
    silence_transport_logs()
    try:
        if args.audit:
            result=audit_boundary(ROOT)
            print(json.dumps({'counts':result['counts'],'halted':result['halted'],
                              'reference_measurement_status':result['reference_measurement_status']}))
            return
        repo=Path(__file__).resolve().parents[2]
        def git(*args): return subprocess.check_output(['git','-C',str(repo),*args],text=True,stderr=subprocess.DEVNULL).strip()
        manifest=json.loads((repo/'backend/docs/comparison-v1_冻结清单.json').read_text(encoding='utf-8'))
        for filename,expected in manifest['files_sha256'].items():
            if file_hash(SOURCE/filename)!=expected: raise LiveGuardError('frozen_source_changed')
        protocol=make_boundary_protocol(json.loads((SOURCE/'protocol.json').read_text(encoding='utf-8')),
                                        json.loads((SOURCE/'reference.json').read_text(encoding='utf-8')))
        if args.prepare:
            if (ROOT/'started.marker').exists() or (ROOT/'ledger.json').exists():
                raise LiveGuardError('experiment_already_prepared_or_started')
            if (ROOT/'protocol.json').exists() and json.loads((ROOT/'protocol.json').read_text(encoding='utf-8'))!=protocol:
                raise LiveGuardError('frozen_protocol_mismatch')
            dump(ROOT/'protocol.json',protocol)
            print(json.dumps({'points':96,'pairs':24,'validation_sha256':protocol['validation_sha256'],'total_limit':1392}))
            return
        settings=Settings()
        if not(settings.analysis_provider=='baidu' and settings.ak_configured and settings.analysis_qps==3):
            raise LiveGuardError('invalid_live_configuration')
        if git('status','--porcelain'): raise LiveGuardError('commit_tools_before_live_execution')
        if git('diff','comparison-v1','--','life-circle-algorithm','backend/app'):
            raise LiveGuardError('algorithm_or_service_changed')
        if not (ROOT/'protocol.json').exists(): raise LiveGuardError('prepare_points_first')
        if json.loads((ROOT/'protocol.json').read_text(encoding='utf-8'))!=protocol:
            raise LiveGuardError('frozen_protocol_mismatch')
        with ExitStack() as stack:
            old=[]
            for cls,name in ((Ledger,'guodingyi-live-smoke'),(QpsLedger,'guodingyi-qps-review'),
                             (QpsLedger,'guodingyi-qps-response-paced-review'),(ExperimentLedger,SOURCE.name)):
                path=Path('D:/CodexOutputs')/name
                if not (path/'ledger.json').exists(): raise LiveGuardError('missing_historical_ledger')
                old.append(stack.enter_context(cls(path)))
            hashes={str(p.file):file_hash(p.file) for p in old}
            ledger=stack.enter_context(BoundaryLedger(ROOT))
            if ledger.data.get('experiment_started') or sum(ledger.data['counts'].values()) or (ROOT/'started.marker').exists():
                raise LiveGuardError('experiment_already_started_no_resume')
            sources=[p for p in (repo/'backend/tools').glob('*.py')]
            dump(ROOT/'config.json',{'head':git('rev-parse','HEAD'),'comparison_v1':git('rev-parse','comparison-v1^{commit}'),
                'algorithm_baseline':git('rev-parse','guodingyi-baseline-20260913^{commit}'),
                'protocol_sha256':digest(protocol),'source_sha256':{p.relative_to(repo).as_posix():file_hash(p) for p in sources},
                'python':sys.version.split()[0],'packages':{p:version(p) for p in ('httpx','numpy','shapely','contourpy','fastapi')},
                'prior_ledgers':hashes,'started_utc':datetime.now(timezone.utc).isoformat()})
            result=asyncio.run(run_boundary(ledger,protocol,settings.baidu_map_ak.get_secret_value(),
                inner=httpx.AsyncHTTPTransport(retries=0,trust_env=False),progress=lambda p:print(json.dumps(p),flush=True)))
            result['prior_ledgers_unchanged']=all(file_hash(Path(p))==h for p,h in hashes.items())
            dump(ROOT/'result.json',result)
            print(json.dumps({'counts':result['counts'],'halted':result['halted'],
                              'prior_ledgers_unchanged':result['prior_ledgers_unchanged']}))
    except Exception:
        raise SystemExit('Boundary experiment stopped; sensitive details suppressed. Inspect sanitized evidence.') from None


if __name__=='__main__': main()
