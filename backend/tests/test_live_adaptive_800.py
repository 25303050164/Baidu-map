import asyncio
import json
import math
from dataclasses import asdict

import httpx
import pytest

from app.analyses import RateGate
from life_circle.models import IsochroneRequest
from test_live_comparison import FakeClock, request
from tools.live_adaptive_800 import Adaptive800Ledger, comparison_request, run_once
from tools.live_comparison import PROJECTION
from tools.live_smoke import LiveGuardError, ORIGIN


def test_request_changes_only_budget():
    original=asdict(IsochroneRequest(ORIGIN,'bd09ll',budget=400,qps=3,expand=False))
    changed=asdict(comparison_request(original))
    assert changed=={**original,'budget':800}
    assert Adaptive800Ledger.phase_limits=={'adaptive':800}
    assert Adaptive800Ledger.total_limit==800


def test_hard_limit_and_restart(tmp_path):
    with Adaptive800Ledger(tmp_path) as ledger:
        ledger.start_once()
        ledger.data['counts']['adaptive']=800
        ledger.save()
        with pytest.raises(LiveGuardError):ledger.reserve('adaptive',request(),100)
        with pytest.raises(LiveGuardError):ledger.reserve('reference',request(),100)
    with Adaptive800Ledger(tmp_path) as ledger:
        with pytest.raises(LiveGuardError):ledger.start_once()
        assert ledger.data['counts']['adaptive']==800


@pytest.mark.parametrize('mode',['success','retry','fatal','cancel','write_failure'])
def test_mock_run_protects_requests_and_secrets(tmp_path,monkeypatch,mode):
    async def check():
        clock=FakeClock();calls=[]
        with Adaptive800Ledger(tmp_path) as ledger:
            original_save=ledger.save
            def save():
                if mode=='write_failure' and ledger.data['events']:
                    ledger.data['halted']='ledger_write_failed'
                    raise LiveGuardError('fixture-secret')
                # Persistence and restart are tested separately; keep the full
                # 800-call mock flow in memory instead of fsyncing large JSON.
                if mode=='write_failure':original_save()
            monkeypatch.setattr(ledger,'save',save)
            def handle(req):
                calls.append(req)
                if mode=='fatal':return httpx.Response(200,json={'status':401,'message':'fixture-secret'})
                if mode=='retry' and len(calls)==1:return httpx.Response(503,json={})
                if mode=='cancel':ledger.token.cancel()
                def point(key):
                    lat,lng=map(float,req.url.params[key].split(','));return {'lng':lng,'lat':lat}
                start,end=point('origin'),point('destination')
                duration=math.hypot(*PROJECTION.to_local((end['lng'],end['lat'])))/1.2
                return httpx.Response(200,json={'status':0,'result':{'routes':[{'duration':duration,
                    'steps':[{'start_location':start,'end_location':end}]}]}})
            config=comparison_request(asdict(IsochroneRequest(ORIGIN,'bd09ll',budget=400,qps=3,expand=False)))
            report=await run_once(ledger,'fixture-secret',config,inner=httpx.MockTransport(handle),
                clock=clock,gate=RateGate(3,clock=clock.time,sleep=clock.sleep))
            assert len(calls)<=800
            assert report['accounting']['actual_calls']==len(calls)
            assert report['accounting']['reserved_attempts']<=800
            assert report['timing']['max_inflight']<=1
            assert report['timing']['max_in_one_second']<=3
            if mode=='success':
                assert report['status']=='completed'
                assert report['accounting']['actual_calls']>400
                assert (tmp_path/'adaptive.json').exists()
            elif mode=='retry':assert report['accounting']['retries']==1
            elif mode=='fatal':assert len(calls)==1 and report['halted']=='rate_limit'
            elif mode=='cancel':assert len(calls)==1 and report['status']=='cancelled'
            else:assert not calls and report['halted']=='ledger_write_failed'
            with pytest.raises(LiveGuardError):ledger.start_once()
            assert 'fixture-secret' not in json.dumps(report)
        assert 'fixture-secret' not in ''.join(p.read_text(encoding='utf-8') for p in tmp_path.glob('*.json'))
    asyncio.run(check())
