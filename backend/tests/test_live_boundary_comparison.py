import asyncio
import json
import math

import httpx
import pytest

from app.analyses import RateGate
from tools.live_comparison import make_protocol, PROJECTION
from tools.live_boundary_comparison import BoundaryLedger, make_boundary_protocol, reference_gate, run_boundary
from tools.live_smoke import LiveGuardError
from test_live_comparison import FakeClock


def source_data():
    protocol=make_protocol()
    refs=[{**p,'duration':900 if 5<=p['cell'][0]<=10 and 5<=p['cell'][1]<=10 else 1000,
           'endpoint_verified':True} for p in protocol['validation']]
    return protocol,refs


def test_boundary_generation_uses_only_fixed_reference_pairs():
    protocol,refs=source_data()
    result=make_boundary_protocol(protocol,refs)
    assert result==make_boundary_protocol(protocol,list(reversed(refs)))
    assert len(result['pairs'])==24 and len(result['validation'])==96
    assert len({tuple(x['point']) for x in result['validation']})==96
    assert all(x['fraction'] in (.2,.4,.6,.8) for x in result['validation'])
    assert result['total_limit']==1392 and result['limits']['reference']==192
    assert result['order']==['adaptive','uniform','radial']
    old={tuple(x['point']) for x in refs}
    assert all(tuple(x['point']) not in old for x in result['validation'])
    refs[0]['duration']=None
    assert make_boundary_protocol(protocol,refs)['validation']==result['validation']


@pytest.mark.parametrize('valid,positive,passed',[(60,10,True),(60,51,False),(59,20,False),(96,86,True),(96,87,False)])
def test_reference_coverage_gate(valid,positive,passed):
    refs=[{'duration':900 if i<positive else 901,'endpoint_verified':i<valid} for i in range(96)]
    assert reference_gate(refs)['passed']==passed


def test_post_overlap_gate_does_not_require_all_original_points():
    refs=[{'duration':850 if i<20 else 950,'endpoint_verified':True} for i in range(80)]
    assert reference_gate(refs,require_complete=False)['passed']
    assert not reference_gate(refs)['passed']


def test_boundary_stage_budget_persists_and_no_resume(tmp_path):
    from test_live_comparison import request
    with BoundaryLedger(tmp_path) as ledger:
        ledger.start_once()
        ledger.data['counts']['reference']=192
        with pytest.raises(LiveGuardError): ledger.reserve('reference',request(),1)
    with BoundaryLedger(tmp_path) as ledger:
        with pytest.raises(LiveGuardError): ledger.start_once()


@pytest.mark.parametrize('mode',['success','bad_reference','limited','method_error','local_write_error'])
def test_mock_boundary_pipeline_isolated_and_guarded(tmp_path,monkeypatch,mode):
    import tools.live_boundary_comparison as module
    async def run():
        protocol=make_boundary_protocol(*source_data())
        reference_points={tuple(p['point']) for p in protocol['validation']}
        clock,calls=FakeClock(),{}
        with BoundaryLedger(tmp_path) as ledger:
            monkeypatch.setattr(ledger,'save',lambda:None)
            if mode=='local_write_error':
                failed=[]
                def save():
                    if len(ledger.data['events'])==82 and not failed:
                        failed.append(1)
                        ledger.data['halted']='ledger_write_failed'
                        raise LiveGuardError('ledger_write_failed')
                monkeypatch.setattr(ledger,'save',save)
            if mode=='method_error':
                async def fail(*args,**kwargs): raise RuntimeError('fixture-never-save')
                monkeypatch.setattr(module,'compute_radial',fail)
            def handle(req):
                phase=ledger.data['events'][-1]['phase']
                calls[phase]=calls.get(phase,0)+1
                if mode=='limited': return httpx.Response(200,json={'status':401})
                if mode=='bad_reference': return httpx.Response(200,json={'status':7})
                def point(key):
                    lat,lng=map(float,req.url.params[key].split(','))
                    return {'lng':lng,'lat':lat}
                start,end=point('origin'),point('destination')
                dest=(end['lng'],end['lat'])
                if phase=='reference':
                    assert dest in reference_points
                    duration=850 if calls[phase]%2 else 950
                else:
                    duration=math.hypot(*PROJECTION.to_local(dest))/1.2
                return httpx.Response(200,json={'status':0,'result':{'routes':[{'duration':duration,
                    'steps':[{'start_location':start,'end_location':end}]}]}})
            result=await run_boundary(ledger,protocol,'fixture-never-save',inner=httpx.MockTransport(handle),
                clock=clock,gate=RateGate(3,clock=clock.time,sleep=clock.sleep))
            if mode=='local_write_error':
                assert calls=={'reference':81}
                assert result['stages']['reference']['coverage_passed']
                assert not result['stages']['reference']['passed']
                assert result['stages']['reference']['status']=='failed'
            elif mode=='limited': assert calls=={'reference':1}
            elif mode=='bad_reference': assert set(calls)=={'reference'}
            else:
                assert calls['reference']==96
                assert result['stages']['reference']['passed']
                assert result['stages']['adaptive']['status']=='completed'
                assert result['stages']['uniform']['status']=='completed'
                if mode=='method_error':
                    assert result['metrics'][2]['unknown_point_fraction']==1
                    assert result['stages']['radial']['status']=='failed'
                else: assert result['stages']['radial']['status']=='completed'
            assert sum(result['counts'].values())<=1392
            assert len(result['metrics'])==3
            assert all(result['counts'][p]<=n for p,n in ledger.phase_limits.items())
            assert (tmp_path/'comparison.svg').exists()
            from tools.live_boundary_comparison import audit_boundary
            from tools.comparison_audit import file_hash
            (tmp_path/'config.json').write_text('{}')
            before=file_hash(tmp_path/'result.json')
            audited=audit_boundary(tmp_path)
            assert file_hash(tmp_path/'result.json')==before
            assert sum(audited['reference_measurement_status'].values())==len(json.loads((tmp_path/'reference.json').read_text()))
            if mode=='local_write_error':
                assert not audited['stages']['reference']['passed']
        assert 'fixture-never-save' not in ''.join(p.read_text(encoding='utf-8') for p in tmp_path.glob('*.json'))
    asyncio.run(run())


def test_boundary_write_error_retains_only_numeric_diagnostics(tmp_path,monkeypatch):
    import tools.live_smoke as smoke
    with BoundaryLedger(tmp_path) as ledger:
        def fail(*args): raise PermissionError(13,'fixture-secret-url')
        monkeypatch.setattr(smoke,'dump',fail)
        with pytest.raises(LiveGuardError): ledger.save()
        assert ledger.data['recording_error']['errno']==13
        assert 'fixture-secret-url' not in json.dumps(ledger.data)


def test_progress_can_monitor_sends_without_opening_ledger(tmp_path):
    from tools.live_boundary_comparison import ProgressTransport
    from test_live_comparison import request
    async def run():
        with BoundaryLedger(tmp_path) as ledger:
            clock=FakeClock()
            reports=[]
            transport=ProgressTransport(ledger,httpx.MockTransport(lambda r:httpx.Response(200,json={'status':7})),
                                        clock=clock.time,progress=reports.append)
            transport.phase='reference'
            await transport.handle_async_request(request())
            assert reports[0]['actual_calls']==1
            assert 'fixture' not in json.dumps(reports)
    asyncio.run(run())


@pytest.mark.skipif(__import__('sys').platform!='win32',reason='Windows file replacement regression')
def test_windows_shared_delete_reader_blocks_replace_and_error_stays_halted(tmp_path):
    import ctypes
    from ctypes import wintypes
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
    kernel.CreateFileW.restype=wintypes.HANDLE
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    with BoundaryLedger(tmp_path) as ledger:
        ledger.start_once()
        handle=kernel.CreateFileW(str(ledger.file),0x80000000,7,None,3,0,None)
        assert handle!=wintypes.HANDLE(-1).value
        try:
            with pytest.raises(LiveGuardError): ledger.save()
            assert ledger.data['recording_error']['winerror']==5
        finally: kernel.CloseHandle(handle)
        ledger.save()
        assert ledger.data['halted']=='ledger_write_failed'
        with pytest.raises(LiveGuardError): ledger.start_once()
