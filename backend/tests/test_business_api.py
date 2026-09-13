import copy
import math
import time

import httpx
from fastapi.testclient import TestClient

from app.config import Settings
from app.contracts import TaskResultResponse
from app.main import create_app


def test_full_http_business_chain_and_server_owned_route_ids(monkeypatch):
    actual_client = httpx.AsyncClient
    calls = []
    def handle(r):
        calls.append(r.url.path)
        if "place/v3" in r.url.path:
            name = r.url.params["query"]
            return httpx.Response(200,json={"status":0,"total":1,"results":[{"uid":name,"name":"测试"+name,"location":{"lng":116.405,"lat":39.915}}]})
        a,b = [tuple(map(float,r.url.params[k].split(",")))[::-1] for k in ("origin","destination")]
        distance = math.dist(a,b)*111000
        return httpx.Response(200,json={"status":0,"result":{"routes":[{"duration":distance/1.2,"distance":distance,"steps":[{"start_location":{"lng":str(a[0]),"lat":str(a[1])},"end_location":{"lng":str(b[0]),"lat":str(b[1])},"path":f"{a[0]},{a[1]};{b[0]},{b[1]}"}]}]}})
    monkeypatch.setattr("app.analyses.httpx.AsyncClient",lambda **kw:actual_client(transport=httpx.MockTransport(handle),**kw))
    app=create_app(Settings(_env_file=None,analysis_provider="baidu",baidu_map_ak="test-secret",analysis_qps=10000))
    with TestClient(app) as client:
        task=client.post("/api/analyses",json={"center":{"lng":116.404,"lat":39.915},"coordinateSystem":"bd09ll","budget":200,"clientRequestId":"business"}).json()["taskId"]
        for _ in range(500):
            state=client.get(f"/api/analyses/{task}").json()
            if state["status"] in ("completed","failed"):
                break
            time.sleep(.01)
        assert state["status"]=="completed"
        result=client.get(f"/api/analyses/{task}/result").json()
        TaskResultResponse.model_validate(result)
        assert result["facilityAnalysis"]["assessed_points"]==9
        assert len(result["data"]["facilities"])==5 and result["data"]["report"]
        assert result["status"]=="partial" and "test-secret" not in str(result)
        before=len(calls)
        cached=next(iter(result["facilityAnalysis"]["routes"]))
        assert client.post(f"/api/analyses/{task}/routes/{cached}").status_code==200
        assert len(calls)==before
        assert client.post(f"/api/analyses/{task}/routes/not-stored").status_code==404
        stored=app.state.analyses.jobs[task].result["data"]["facilities"]
        for i in range(4):
            item=copy.deepcopy(stored[0]);item["id"]=f"extra-{i}";stored.append(item)
            assert client.post(f"/api/analyses/{task}/routes/extra-{i}").status_code==(200 if i<3 else 429)
        assert len(calls)==before+3
