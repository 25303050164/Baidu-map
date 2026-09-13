import asyncio
import copy
import json

import httpx
import pytest

from app.analyses import RateGate
from test_live_boundary_comparison import source_data
from test_live_comparison import FakeClock, request
from tools.live_boundary_comparison import make_boundary_protocol, run_boundary
from tools.continue_boundary_comparison import ContinuationLedger, plan_continuation
from tools.live_smoke import LiveGuardError


def parent_fixture():
    protocol=make_boundary_protocol(*source_data())
    refs=[{**p,'duration':850 if i%2 else 950,'endpoint_verified':i<80,
           'reason':None if i<80 else 'endpoint_offset' if i==80 else 'temporary','attempts':1}
          for i,p in enumerate(protocol['validation'])]
    for p in refs[80:]: p['duration']=None
    events=[{'id':i+1,'phase':'reference','destination':p['point'],'reservedAt':'2026-09-13T09:32:00+00:00',
             **({'dispatchPerf':i,'responsePerf':i+.1,'http_status':200,'baidu_status':0,
                 'outcome':'success' if i<80 else 'endpoint_offset'} if i<81 else {})}
            for i,p in enumerate(protocol['validation'][:82])]
    ledger={'halted':'ledger_write_failed','counts':{'reference':82,'adaptive':0,'uniform':0,'radial':0},'events':events}
    return protocol,ledger,refs


def test_continuation_retains_sent_points_and_reservation_attempt():
    p,ledger,refs=parent_fixture()
    before=copy.deepcopy((ledger,refs))
    plan=plan_continuation(p,ledger,refs)
    assert len(plan['pending'])==15
    assert plan['pending'][0]['max_attempts']==1
    assert all(x['max_attempts']==2 for x in plan['pending'][1:])
    assert plan['new_limits']=={'reference':29,'adaptive':400,'uniform':400,'radial':400}
    assert plan['new_total_limit']==1229 and plan['cumulative_maximum']==1311
    assert (ledger,refs)==before


@pytest.mark.parametrize('change',['completed_algorithm','different_halt','missing_event','duplicate_reference'])
def test_unexpected_parent_cannot_resume(change):
    p,l,r=parent_fixture()
    if change=='completed_algorithm': l['counts']['adaptive']=1
    elif change=='different_halt': l['halted']='rate_limit'
    elif change=='missing_event': l['events'].pop()
    else: r[-1]=r[0]
    with pytest.raises(LiveGuardError): plan_continuation(p,l,r)


def test_new_ledger_blocks_retained_reference_and_third_cumulative_attempt(tmp_path):
    p,l,r=parent_fixture(); plan=plan_continuation(p,l,r)
    def req(point):
        original=request()
        return httpx.Request('GET',original.url.copy_set_param('destination',f'{point[1]},{point[0]}'))
    with ContinuationLedger(tmp_path,plan) as ledger:
        ledger.start_once()
        with pytest.raises(LiveGuardError): ledger.reserve('reference',req(p['validation'][0]['point']),1)
        pending=plan['pending'][0]['point']
        ledger.reserve('reference',req(pending),1)
        with pytest.raises(LiveGuardError): ledger.reserve('reference',req(pending),2)
        assert ledger.data['counts']['reference']==1
    with ContinuationLedger(tmp_path,plan) as ledger:
        with pytest.raises(LiveGuardError): ledger.start_once()


@pytest.mark.parametrize('limited',[False,True])
def test_mock_resume_collects_only_pending_then_three_methods(tmp_path,monkeypatch,limited):
    async def run():
        protocol,parent,old=parent_fixture()
        plan=plan_continuation(protocol,parent,old)
        clock,calls=FakeClock(),[]
        with ContinuationLedger(tmp_path,plan) as ledger:
            monkeypatch.setattr(ledger,'save',lambda:None)
            def handle(req):
                phase=ledger.data['events'][-1]['phase']
                lat,lng=map(float,req.url.params['destination'].split(','))
                calls.append((phase,(lng,lat)))
                if limited: return httpx.Response(200,json={'status':401})
                return httpx.Response(200,json={'status':0,'result':{'routes':[{'duration':950,
                    'steps':[{'start_location':{'lng':121.513926,'lat':31.313077},
                              'end_location':{'lng':lng,'lat':lat}}]}]}})
            result=await run_boundary(ledger,protocol,'fixture-only',inner=httpx.MockTransport(handle),
                clock=clock,gate=RateGate(3,clock=clock.time,sleep=clock.sleep),continuation=plan)
            assert all(point in {tuple(p['point']) for p in plan['pending']} for phase,point in calls if phase=='reference')
            if limited:
                assert len(calls)==1 and result['stages']['adaptive']['status']=='not_run'
            else:
                assert sum(phase=='reference' for phase,point in calls)==15
                assert result['stages']['reference']['valid']==95
                assert all(result['stages'][m]['status']=='completed' for m in ('adaptive','uniform','radial'))
                merged=json.loads((tmp_path/'reference.json').read_text())
                assert [x['duration'] for x in merged[:81]]==[x['duration'] for x in old[:81]]
                from tools.live_boundary_comparison import audit_boundary
                from tools.live_smoke import dump
                dump(tmp_path/'ledger.json',ledger.data)
                (tmp_path/'config.json').write_text('{}')
                audited=audit_boundary(tmp_path)
                assert audited['reference_measurement_status']=={'valid':95,'invalid_reference':1}
            assert sum(result['counts'].values())<=1229
            assert result['cumulative_counts']['reference']==82+result['counts']['reference']
    asyncio.run(run())
