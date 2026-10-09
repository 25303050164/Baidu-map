"""§5 B2 决策 2：会话与保留期的第一条期限。

运营者的口径是**两个条件任一先到即过期**：

1. 用户关闭浏览器 —— 落实为"本应用最后一个标签页关闭"，允许最多 5 分钟的检测宽限
   （刷新、短暂断网、多标签页共享会话都不该误清）；
2. 该次体检之后的**第三次新体检结束** —— 失败/取消也计数；幂等重发、重试、补查、新修订
   都不新建任务行，因此不计数。

外加两条历史口径：早于本库保留期基线的记录视为已过期；已经清理过的按墓碑回答。

这里钉住的是**判定**，不是清理：到期之后明细还能不能提供由 ``RetentionView`` 回答，
实际删文件在别处。
"""
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from test_checkup_facilities import (CORE_MAJORS, SyntheticPlaces, at_origin, body,
                                     make_app, terminal, two_page_rows)

SESSIONS = "/api/v2/checkups/sessions"
#: 会话标识走请求头：它属于调用方，不属于某一次体检的参数（也就不进入请求指纹）。
HEADER = "X-Checkup-Session"
LEASE = 300.0
SMALL = {"categories": CORE_MAJORS, "maxPoiRequests": 60}


@pytest.fixture
def clock(monkeypatch):
    """受控时钟。store 与 manager 都 ``import time``，所以补丁打在模块对象上，两边一起走。"""
    current = {"t": 1_800_000_000.0}
    monkeypatch.setattr(time, "time", lambda: current["t"])
    return current


def open_session(client, **overrides):
    payload = {"schemaVersion": "checkup-v1"}
    payload.update(overrides)
    response = client.post(SESSIONS, json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def heartbeat(client, session_id, tab_id):
    return client.post(f"{SESSIONS}/{session_id}/tabs/{tab_id}/heartbeat")


def close(client, session_id, tab_id):
    return client.post(f"{SESSIONS}/{session_id}/tabs/{tab_id}/close")


def submit(client, *, session_id=None, **overrides):
    """发起一次体检并等它跑完，返回 ``(task_id, task_view)``。"""
    headers = {} if session_id is None else {HEADER: session_id}
    created = client.post("/api/v2/checkups", json=body(**overrides), headers=headers)
    assert created.status_code == 202, created.text
    task_id = created.json()["taskId"]
    return task_id, terminal(client, task_id)


def task_view(client, task_id):
    response = client.get(f"/api/v2/checkups/{task_id}")
    assert response.status_code == 200, response.text
    return response.json()


def test_two_tabs_share_one_session_and_it_stays_live_while_one_is_open(tmp_path, clock):
    with TestClient(make_app(tmp_path, SyntheticPlaces(at_origin()))) as client:
        first = open_session(client)
        assert first["resumed"] is False
        assert first["openTabs"] == 1
        assert first["expiresAt"] == pytest.approx(clock["t"] + LEASE)
        # 第二个标签页加入同一个会话：同一浏览器共用一个会话，各自登记自己的标签页。
        second = open_session(client, sessionId=first["sessionId"])
        assert second["resumed"] is True and second["sessionId"] == first["sessionId"]
        assert second["openTabs"] == 2

        # 关掉一个标签页，另一个还在：会话不因为它而结束，续租照常把它推后。
        assert close(client, first["sessionId"], first["tabId"]).json()["openTabs"] == 1
        clock["t"] += 60
        assert heartbeat(client, first["sessionId"], second["tabId"]).status_code == 200
        clock["t"] += LEASE - 1
        assert heartbeat(client, first["sessionId"], second["tabId"]).status_code == 200

        # 两个标签页都关掉之后，宽限一到就到期 —— 与"最后一个标签页关闭"一致。
        assert close(client, first["sessionId"], second["tabId"]).json()["openTabs"] == 0
        clock["t"] += LEASE + 1
        assert heartbeat(client, first["sessionId"], second["tabId"]).status_code == 409


def test_a_refresh_keeps_its_tab_and_does_not_count_as_a_second_one(tmp_path):
    with TestClient(make_app(tmp_path, SyntheticPlaces(at_origin()))) as client:
        opened = open_session(client)
        again = open_session(client, sessionId=opened["sessionId"], tabId=opened["tabId"])
        assert again["tabId"] == opened["tabId"]
        assert again["openTabs"] == 1, "同一个标签页刷新不该被算成多开了一个"


def test_a_closed_last_tab_expires_after_the_grace_and_cannot_be_revived(tmp_path, clock):
    with TestClient(make_app(tmp_path, SyntheticPlaces(at_origin()))) as client:
        opened = open_session(client)
        session_id, tab_id = opened["sessionId"], opened["tabId"]
        assert close(client, session_id, tab_id).json()["openTabs"] == 0

        # 宽限期内（刷新、短暂断网）还接得上，而这次续租把租约推后了。
        clock["t"] += LEASE - 1
        assert heartbeat(client, session_id, tab_id).status_code == 200
        clock["t"] += LEASE + 1
        late = heartbeat(client, session_id, tab_id)
        assert late.status_code == 409, late.text
        assert late.json()["code"] == "checkup_session_expired"
        # 迟到的心跳不能复活它，用同一个标识重新打开也不能：那正是这条规则的唯一意义。
        revived = client.post(SESSIONS, json={"schemaVersion": "checkup-v1",
                                             "sessionId": session_id})
        assert revived.status_code == 409, revived.text
        assert revived.json()["code"] == "checkup_session_expired"
        # 刷新页面会开一个新的会话，而不是假装什么都没发生。
        fresh = open_session(client)
        assert fresh["resumed"] is False and fresh["sessionId"] != session_id


def test_an_unknown_session_identifier_starts_a_new_session_instead_of_failing(tmp_path):
    """服务端不认识这个标识（换了后端、清了库）时新开一个 —— 而不是假装续上了。"""
    with TestClient(make_app(tmp_path, SyntheticPlaces(at_origin()))) as client:
        opened = open_session(client, sessionId="session-from-another-backend")
        assert opened["resumed"] is False
        assert opened["sessionId"] != "session-from-another-backend"


def test_a_checkup_expires_with_the_session_that_asked_for_it(tmp_path, clock):
    with TestClient(make_app(tmp_path, SyntheticPlaces(at_origin()))) as client:
        opened = open_session(client)
        task_id, _view = submit(client, session_id=opened["sessionId"], facilities=SMALL)
        retention = task_view(client, task_id)["retention"]
        # 租约到期时刻 = 最后一个标签页还在的时刻 + 宽限：这次体检就创建在同一刻。
        assert retention["detailsAvailable"] is True
        assert retention["expiresAt"] == pytest.approx(opened["expiresAt"])
        assert retention["reason"] is None

        clock["t"] += LEASE + 1
        expired = task_view(client, task_id)["retention"]
        assert expired["detailsAvailable"] is False
        assert expired["reason"] == "session_closed"


def test_the_third_later_checkup_ends_the_earlier_ones_retention(tmp_path, clock):
    with TestClient(make_app(tmp_path, SyntheticPlaces(at_origin()))) as client:
        first, _view = submit(client, facilities=SMALL)
        assert task_view(client, first)["retention"]["detailsAvailable"] is True

        for index in range(2):
            submit(client, clientRequestId=f"later-{index}", facilities=SMALL)
        # 才两次：期限还没到，而且它报的是"还没有第三次"，不是一个猜的时刻。
        assert task_view(client, first)["retention"]["detailsAvailable"] is True

        third, third_view = submit(client, clientRequestId="later-2", facilities=SMALL)
        after = task_view(client, first)["retention"]
        assert after["detailsAvailable"] is False
        assert after["reason"] == "superseded"
        # 第三次结束的那一刻就是它的到期时刻。
        assert after["expiresAt"] == pytest.approx(third_view["finishedAt"])
        # 后来者自己还在期限内（它后面还没有三次）。
        assert task_view(client, third)["retention"]["detailsAvailable"] is True


def test_a_cancelled_checkup_still_counts_as_the_third_one(tmp_path, clock):
    """规则说的是"第三次新体检结束"，不是"第三次成功的新体检"。取消也是一次新体检。"""
    with TestClient(make_app(tmp_path, SyntheticPlaces(at_origin()))) as client:
        first, _view = submit(client, facilities=SMALL)
        submit(client, clientRequestId="later-0", facilities=SMALL)
        submit(client, clientRequestId="later-1", facilities=SMALL)
        queued = client.post("/api/v2/checkups",
                             json=body(clientRequestId="later-2", facilities=SMALL)).json()
        cancelled = client.post(f"/api/v2/checkups/{queued['taskId']}/cancel")
        assert cancelled.status_code in (200, 202), cancelled.text
        for _ in range(200):
            view = task_view(client, queued["taskId"])
            if view["status"] in ("cancelled", "completed", "failed"):
                break
            time.sleep(0.02)
        assert task_view(client, first)["retention"]["reason"] == "superseded"


def test_retries_and_extensions_do_not_count_as_new_checkups(tmp_path, clock):
    with TestClient(make_app(tmp_path, SyntheticPlaces(two_page_rows))) as client:
        task_id, _view = submit(client, facilities=SMALL)
        before = task_view(client, task_id)["retention"]
        created = client.post(f"/api/v2/checkups/{task_id}/retries",
                              json={"schemaVersion": "checkup-v1", "clientRequestId": "retry-1",
                                    "maxPoiRequests": 120})
        assert created.status_code == 202, created.text
        # 重试在同一次体检上发布新修订，不新建任务：它不能把别人的期限提前。
        assert task_view(client, task_id)["retention"]["expiresAt"] == before["expiresAt"]

        extension = client.post(f"/api/v2/checkups/{task_id}/facility-extensions",
                                json={"schemaVersion": "checkup-v1",
                                      "clientRequestId": "ext-1",
                                      "categories": ["dining"], "maxPoiRequests": 60})
        assert extension.status_code == 202, extension.text
        assert task_view(client, task_id)["retention"]["expiresAt"] == before["expiresAt"]


def test_a_report_written_before_the_retention_baseline_is_treated_as_expired(tmp_path, clock):
    """运营者的口径：没有会话归属的历史报告视为已过期（明细不再提供，只留汇总）。"""
    app = make_app(tmp_path, SyntheticPlaces(at_origin()))
    with TestClient(app) as client:
        task_id, _view = submit(client, facilities=SMALL)
        assert task_view(client, task_id)["retention"]["detailsAvailable"] is True

        # 把这份库的保留期基线推到这次体检之后：从这一刻起它就是"基线之前的数据"。
        store = app.state.checkups.store
        with sqlite3.connect(store.path) as connection:
            connection.execute("INSERT OR REPLACE INTO meta (key, value) VALUES"
                               " ('retention_baseline_at', ?)", (repr(1_900_000_000.0),))
        legacy = task_view(client, task_id)["retention"]
        assert legacy["detailsAvailable"] is False
        assert legacy["reason"] == "legacy"


def test_a_checkup_without_a_session_has_no_lease_to_expire(tmp_path, clock):
    """没有会话归属的任务只剩"三次后续体检"这一条期限 —— 命令行与脚本仍然能用。"""
    with TestClient(make_app(tmp_path, SyntheticPlaces(at_origin()))) as client:
        task_id, _view = submit(client, facilities=SMALL)
        retention = task_view(client, task_id)["retention"]
        assert retention["detailsAvailable"] is True
        assert retention["expiresAt"] is None


def test_the_session_reports_how_many_checkups_it_owns(tmp_path, clock):
    with TestClient(make_app(tmp_path, SyntheticPlaces(at_origin()))) as client:
        opened = open_session(client)
        submit(client, session_id=opened["sessionId"], facilities=SMALL)
        assert heartbeat(client, opened["sessionId"], opened["tabId"]).json()["tasks"] == 1


def test_session_endpoints_refuse_an_unknown_session(tmp_path):
    with TestClient(make_app(tmp_path, SyntheticPlaces(at_origin()))) as client:
        missing = client.post(f"{SESSIONS}/nope/tabs/nope/heartbeat")
        assert missing.status_code == 404, missing.text
        assert missing.json()["code"] == "checkup_session_not_found"
        assert client.post(f"{SESSIONS}/nope/tabs/nope/close").status_code == 404
