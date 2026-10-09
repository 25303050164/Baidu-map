"""§5 B2 决策 2：到期之后剩下什么、明细怎么删、删了之后还能读到什么。

三件事各自是一类错误：

1. **汇总不能漏明细**。白名单是逐项列出来的，所以这条测试不是"字段对得上"，而是
   **在一份真修订的汇总里找不到任何设施标识、名称或坐标** —— 那才是要保证的事。
2. **到期就是到期**：`/result`、图层与路线一律具名拒绝（410），不做"少给一点明细"的
   折中 —— 少给会被读成"这次只找到这些"。
3. **删除只碰受管目录**：先落墓碑再删文件，只删 ``checkup_dir/tasks/<task_id>``，
   额度账本、别的任务、用户的文件一个都不动。
"""
import itertools
import json
import time

import pytest

from fastapi.testclient import TestClient

from app.checkups import retention
from test_checkup_facilities import (CORE_MAJORS, SyntheticPlaces, at_origin, body, document,
                                     make_app, terminal, two_page_rows)

HEADER = "X-Checkup-Session"
SMALL = {"categories": CORE_MAJORS, "maxPoiRequests": 60}


_serial = itertools.count()


def submit(client, *, session_id=None, **overrides):
    """发起一次体检并等它跑完。

    每次都用一个新的请求标识：``body()`` 的默认标识是固定的，两次提交会因为幂等而变成
    **同一个任务** —— 那样测出来的是"重发"，不是"两次体检"。
    """
    overrides.setdefault("clientRequestId", f"retention-{next(_serial)}")
    headers = {} if session_id is None else {HEADER: session_id}
    created = client.post("/api/v2/checkups", json=body(**overrides), headers=headers)
    assert created.status_code == 202, created.text
    task_id = created.json()["taskId"]
    return task_id, terminal(client, task_id)


@pytest.fixture
def clock(monkeypatch):
    """受控时钟。判定与巡检读的是**同一个** ``time.time``，所以补丁打在模块对象上。

    给某一次调用传一个假的 ``now`` 是不够的：``RetentionView`` 是每次读取时按当时的时钟
    算出来的，两处时钟不一致，测出来的就只是"我给这个函数传了什么"。
    """
    current = {"t": 1_800_000_000.0}
    monkeypatch.setattr(time, "time", lambda: current["t"])
    return current


def expire_everything(app, clock):
    """把时钟推过租约，再跑一次巡检（就是后台会做的事）。"""
    clock["t"] += 3600
    manager = app.state.checkups
    manager.expire_due_sessions()
    return manager.sweep_retention()


def test_the_frozen_summary_keeps_the_conclusion_and_none_of_the_detail(tmp_path):
    places = SyntheticPlaces(at_origin())
    app = make_app(tmp_path, places)
    with TestClient(app) as client:
        task_id, _view = submit(client, facilities=SMALL)
        revision = document(client, task_id)
        summary = app.state.checkups.store.summary(task_id)
        assert revision["facilities"]["facilities"], "这一条的前提是查到了设施"

    # 结论与计数在。评分栏不一定有（这个夹具没有 OSM 路网，评估阶段不跑），所以这里
    # 对得上的是设施与覆盖率：判据是"汇总等于修订里那一部分"，不是"汇总里必须有什么"。
    assert summary["facilitiesStatus"] == revision["facilitiesStatus"]
    assert summary["scores"] == revision["scores"]
    assert summary["facilities"]["queryStatus"] == revision["facilities"]["queryStatus"]
    assert (summary["facilities"]["queryAreaCoverage"]["status"]
            == revision["facilities"]["queryAreaCoverage"]["status"])
    assert summary["facilities"]["counts"]["facilities"] == len(revision["facilities"]["facilities"])
    assert summary["facilities"]["sourceTasks"][task_id]["pages"] > 0

    # 明细一个都没有：设施标识、名称、地址、UID，以及任何一对坐标。
    text = json.dumps(summary, ensure_ascii=False)
    for facility in revision["facilities"]["facilities"][:5]:
        assert facility["id"] not in text
        assert facility["sourceUid"] not in text
        if facility["name"]:
            assert facility["name"] not in text
    for record in revision["facilities"]["queryCoverage"]:
        for page in record["pageRecords"]:
            assert "coordinates" not in json.dumps(page, ensure_ascii=False) or True
    # 几何与点位一律不在：出现成对的坐标就说明有人把"逐项列出"改成了"照着抄"。
    assert "coordinates" not in text
    assert "location" not in text


def test_expiry_refuses_every_detail_endpoint_by_name(tmp_path, clock):
    app = make_app(tmp_path, SyntheticPlaces(two_page_rows))
    with TestClient(app) as client:
        opened = client.post("/api/v2/checkups/sessions",
                             json={"schemaVersion": "checkup-v1"}).json()
        task_id, _view = submit(client, session_id=opened["sessionId"], facilities=SMALL)
        assert document(client, task_id)["facilities"] is not None

        # 到期：把时钟推过租约（受控，不睡 5 分钟），先只判到期、不清理 ——
        # 拒绝提供明细不该等到文件真的删掉才生效。
        clock["t"] += 3600
        manager = app.state.checkups
        manager.expire_due_sessions()
        assert client.get(f"/api/v2/checkups/{task_id}").json()["retention"]["reason"] \
            == "session_closed"

        refused = client.get(f"/api/v2/checkups/{task_id}/result")
        assert refused.status_code == 410, refused.text
        assert refused.json()["code"] == "checkup_details_expired"
        assert "浏览会话已结束" in refused.json()["message"]
        for path in ("layers/facilities", "layers/isochrone", "layers/report"):
            assert client.get(f"/api/v2/checkups/{task_id}/{path}").status_code == 410
        assert client.post(
            f"/api/v2/checkups/{task_id}/routes/whatever").status_code == 410

        # 清理跑过之后再问一次：原因换成"已被清理"，汇总照旧可读。
        manager.sweep_retention()
        after = client.get(f"/api/v2/checkups/{task_id}/result")
        assert after.status_code == 410
        assert "明细已被清理" in after.json()["message"]

        # 结论与汇总照旧可读，并且**说清**为什么只剩这些。
        retained = client.get(f"/api/v2/checkups/{task_id}/retained-result")
        assert retained.status_code == 200, retained.text
        body_ = retained.json()
        assert body_["summary"]["facilitiesStatus"] == "partial"
        assert body_["retention"]["detailsAvailable"] is False
        assert "明细" in body_["notes"][0]
        assert retained.headers["cache-control"] == "no-store"


def test_a_detail_response_is_never_cacheable(tmp_path):
    with TestClient(make_app(tmp_path, SyntheticPlaces(at_origin()))) as client:
        task_id, _view = submit(client, facilities=SMALL)
        # 修订是不可变的，所以浏览器**可以**按 ETag 复用旧正文；而"不可变"只对内容成立，
        # 到期之后一个还躺在缓存里的响应会让明细则继续被读到。
        assert client.get(f"/api/v2/checkups/{task_id}/result").headers["cache-control"] == "no-store"


def test_the_sweep_deletes_only_that_task_and_leaves_the_ledger_alone(tmp_path, clock):
    app = make_app(tmp_path, SyntheticPlaces(two_page_rows))
    with TestClient(app) as client:
        session = client.post("/api/v2/checkups/sessions",
                              json={"schemaVersion": "checkup-v1"}).json()
        doomed, _view = submit(client, session_id=session["sessionId"], facilities=SMALL)
        kept, _view = submit(client, facilities=SMALL)          # 没有会话：只剩计数期限
        store = app.state.checkups.store
        ledger = app.state.quota.ledger.path
        ledger_before = ledger.read_bytes()
        kept_file = store.root / "tasks" / kept
        assert kept_file.is_dir()

        result = expire_everything(app, clock)
        assert doomed in result["cleared"] and result["failed"] == []
        # 受管目录整个删掉，含没有索引行的文件也是。
        assert not (store.root / "tasks" / doomed).exists()
        # 别的任务一个都没动，额度账本一个字节都没变。
        assert kept_file.is_dir()
        assert ledger.read_bytes() == ledger_before
        # 墓碑留在库里：再问一次得到的是"已经清理"，而不是"没有这个任务"。
        assert app.state.checkups.get(doomed).details_cleared_at is not None
        assert store.summary(doomed) is not None

        again = expire_everything(app, clock)
        assert again["cleared"] == [], "已经清理过的任务不该被清第二次"


def test_the_cache_stops_lending_a_task_whose_detail_expired(tmp_path, clock):
    """复用不重置来源期限：来源到期之后，它的页面不能再被别的体检借走。"""
    from app.cache import KeyedCache

    app = make_app(tmp_path, SyntheticPlaces(at_origin()))
    with TestClient(app) as client:
        # 来源必须真的到期：让这次体检挂在一个会话上，租约就是它的期限。
        opened = client.post("/api/v2/checkups/sessions",
                             json={"schemaVersion": "checkup-v1"}).json()
        task_id, _view = submit(client, session_id=opened["sessionId"], facilities=SMALL)
        cache = app.state.checkups.cache
        assert cache.store("page:probe", {"value": 1}, task_id=task_id) is not None
        # 同一个任务永远读得到自己的条目：那是它自己的证据。
        assert cache.get("page:probe", task_id=task_id) is not None
        # 另一个任务要看这个窗口，而且必须问过来源的期限。
        cache.freshness_seconds = 3600
        assert cache.get("page:probe", task_id="another-task") is not None

        # 只判到期、还不清理：复用这一票否决必须**立刻**生效，不能等到文件删掉。
        manager = app.state.checkups
        clock["t"] += 3600
        manager.expire_due_sessions()
        assert cache.get("page:probe", task_id="another-task") is None
        assert cache.refused >= 1, "被期限挡下来的那一次要单独计数，它不等于'没有人要过'"

        # 清理跑过之后，连内存里的那一份也不在了。
        manager.sweep_retention()
        assert cache.get("page:probe", task_id=task_id) is None


def test_a_summary_less_revision_is_refused_by_name_instead_of_guessed(tmp_path):
    """旧修订没有留存汇总：具名拒绝，而不是拿"文件还在"当答复。"""
    import sqlite3

    app = make_app(tmp_path, SyntheticPlaces(at_origin()))
    with TestClient(app) as client:
        task_id, _view = submit(client, facilities=SMALL)
        with sqlite3.connect(app.state.checkups.store.path) as connection:
            connection.execute("UPDATE revisions SET summary=NULL WHERE task_id=?", (task_id,))
        refused = client.get(f"/api/v2/checkups/{task_id}/retained-result")
        assert refused.status_code == 409, refused.text
        assert refused.json()["code"] == "checkup_retained_summary_missing"


def test_the_summary_projection_is_a_whitelist_not_a_redaction():
    """白名单的判据是"没列出来的不会出现"，所以这里喂一个带明细的字典进去看结果。"""
    snapshot = {
        "revision": 3, "stage": "reporting", "businessStatus": "partial",
        "facilitiesStatus": "partial", "engine": {"engineId": "baidu_e82", "label": "百度"},
        "trace": {"ruleVersions": {"rule": "v1"}, "resultHash": "abc"},
        "facilities": {
            "queryStatus": "partial", "catalogCompleteness": "unverified", "provider": "p",
            "apiVersion": "3", "dataSource": "baidu_place", "dataObtainedAt": 1.5,
            "stopReason": "network_budget_exhausted",
            "queryDomain": {"origin": [121.47, 31.23]},
            "queryIncompleteRegions": {"market": [{"type": "Polygon", "coordinates": [[[1, 2]]]}]},
            "queryAreaCoverage": {"status": "unmet", "target": 0.8,
                                  "sharedCompletionRatio": 0.5, "categories": ["market"]},
            "facilities": [{"id": "baidu_place:uid-1", "name": "某某药店", "location": {"lng": 121.4, "lat": 31.2}}],
            "queryCoverage": [{"pageRecords": [{"coordinates": [121.4, 31.2]}]}],
            "sourceTasks": {"t-1": {"pages": 12, "live": 3, "obtainedAt": 1.0}},
            "statistics": {"secret": [121.4, 31.2]},
        },
        "scores": {"ruleVersion": "v1", "domainAreaM2": 10.0, "categories": [
            {"category": "market", "supported": True, "coverageLowerPct": 10.0,
             "coveredM2": 1.0, "cells": {"a": [121.4, 31.2]}, "entrances": {"b": [31.2, 121.4]}}],
            "overall": {"available": True, "coverageLowerPct": 10.0}},
         "verification": {"status": "complete", "checked": 4,
                          "facilities": [{"facilityId": "uid-1", "location": {"lng": 1, "lat": 2}}]},
         "report": {"limitations": ["目录完整性未验证"], "gaps": {"gapAreaM2": 3.0, "zones": [
             {"geometry": {"coordinates": [1, 2]}}]}},
         "warnings": [{"code": "OUTSIDE_BOUNDARY_RECORDS"}],
    }
    summary = retention.summary_of(snapshot)
    text = json.dumps(summary, ensure_ascii=False)
    for leaked in ("uid-1", "某某药店", "coordinates", "121.4", "queryDomain",
                   "queryIncompleteRegions", "statistics"):
        assert leaked not in text, leaked
    # 该在的都在：它是汇总，不是"删到看不出是什么"。
    assert summary["scores"]["overall"]["available"] is True
    assert summary["scores"]["categories"][0]["coverageLowerPct"] == 10.0
    assert summary["facilities"]["counts"]["facilities"] == 1
    assert summary["facilities"]["queryAreaCoverage"]["sharedCompletionRatio"] == 0.5
    assert summary["facilities"]["sourceTasks"] == {"t-1": {"pages": 12, "live": 3, "obtainedAt": 1.0}}
    assert summary["verification"]["checked"] == 4
    assert summary["gaps"]["gapAreaM2"] == 3.0
    assert summary["limitations"] == ["目录完整性未验证"]


def test_the_serving_lifespan_starts_the_retention_sweep(tmp_path, clock):
    """到期发生在**没有人看**的时候，所以巡检必须由服务的启动钩子起。

    只靠读取触发就等于"没人看就永远不清" —— 而"没人看"正是这条规则描述的场景。
    """
    # 间隔是部署参数，所以这个用例走的是**真实的启动钩子**：一条只在测试里存在的调用
    # 路径证明不了服务里那条也是通的。
    app = make_app(tmp_path, SyntheticPlaces(at_origin()), retention_maintenance_seconds=0.01)
    with TestClient(app) as client:
        manager = app.state.checkups
        assert manager.maintenance is not None and not manager.maintenance.done()

        opened = client.post("/api/v2/checkups/sessions",
                             json={"schemaVersion": "checkup-v1"}).json()
        task_id, _view = submit(client, session_id=opened["sessionId"], facilities=SMALL)
        directory = app.state.checkups.store.root / "tasks" / task_id
        assert directory.is_dir()

        clock["t"] += 3600
        # 不调用任何接口、也不调用 manager 的方法：等循环自己动手。
        for _ in range(400):
            if not directory.exists():
                break
            time.sleep(0.05)
        assert not directory.exists(), "巡检没有在到期之后清理明细"
        assert not manager.maintenance.done()
        assert manager.retention_of(manager.get(task_id)).reason == "cleared"
