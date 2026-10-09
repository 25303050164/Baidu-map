"""保留期清单工具：默认只读，``--apply`` 才删，而且只删受管目录。

这条工具的存在理由是运营者的选择：历史数据"先给清单、人工确认之后再删"。所以最重要的
两条测试是**它默认什么都不改**，以及**它只碰 checkup_dir 下的受管目录** —— 额度账本、
别的目录、以及清单之外的任务一个都不能动。
"""
import json
import sqlite3
import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.checkups import CheckupStore
from app.checkups.manager import CheckupManager
from app.config import Settings
from app.quota import Quota
from test_checkup_facilities import (CORE_MAJORS, SyntheticPlaces, at_origin, body, make_app,
                                     terminal)
from tools import retention_manifest

SMALL = {"categories": CORE_MAJORS, "maxPoiRequests": 60}


def manager_for(settings):
    """只读地组装一个 manager（与工具里的做法一致：不开 worker、不发请求）。"""
    from app.engines import build_registry
    store = CheckupStore(settings.checkup_dir)
    store.create_schema()
    return CheckupManager(settings, build_registry(settings, None, None), store, Quota(settings))


def run_one(client, **overrides):
    overrides.setdefault("clientRequestId", "manifest-1")
    created = client.post("/api/v2/checkups", json=body(facilities=SMALL, **overrides))
    task_id = created.json()["taskId"]
    return task_id, terminal(client, task_id)


def test_the_manifest_lists_history_with_a_reason_and_changes_nothing(tmp_path, monkeypatch):
    app = make_app(tmp_path, SyntheticPlaces(at_origin()))
    with TestClient(app) as client:
        task_id, _view = run_one(client)
        store = app.state.checkups.store
        directory = store.root / "tasks" / task_id
        before = sorted(item.name for item in directory.iterdir())
        # 让这份任务成为"保留期基线之前的数据"：历史数据的判据就是这个。
        with sqlite3.connect(store.path) as connection:
            connection.execute("INSERT OR REPLACE INTO meta (key, value) VALUES"
                               " ('retention_baseline_at', ?)", (repr(1_900_000_000.0),))

        manager = manager_for(Settings(_env_file=None, checkup_dir=store.root,
                                       hybrid_ledger_dir=tmp_path / "ledgers",
                                       quota_ledger_path=tmp_path / "quota.sqlite3"))
        plan = retention_manifest.manifest(manager)

        assert [row["taskId"] for row in plan["toClear"]] == [task_id]
        assert plan["toClear"][0]["reason"] == "legacy"
        assert plan["totals"]["bytesToClear"] > 0
        # 只读：一个字节都没动，连墓碑都没落。
        assert sorted(item.name for item in directory.iterdir()) == before
        assert manager.store.get(task_id).details_cleared_at is None
        # 人类可读的那一份也要说得出"清谁、为什么"。
        text = retention_manifest._report_text(plan)
        assert task_id in text and "legacy" in text


def test_apply_deletes_only_the_listed_managed_directories(tmp_path):
    app = make_app(tmp_path, SyntheticPlaces(at_origin()))
    with TestClient(app) as client:
        doomed, _view = run_one(client, clientRequestId="manifest-doomed")
        kept, _view = run_one(client, clientRequestId="manifest-kept")
        store = app.state.checkups.store
        with sqlite3.connect(store.path) as connection:
            # 只把其中一个推成历史数据。
            created = connection.execute(
                "SELECT created_at FROM tasks WHERE task_id=?", (doomed,)).fetchone()[0]
            connection.execute("INSERT OR REPLACE INTO meta (key, value) VALUES"
                               " ('retention_baseline_at', ?)", (repr(created + 1),))
        ledger = app.state.quota.ledger.path
        ledger_before = ledger.read_bytes()
        outside = tmp_path / "not-managed.txt"
        outside.write_text("别动我", encoding="utf-8")

        manager = manager_for(Settings(_env_file=None, checkup_dir=store.root,
                                       hybrid_ledger_dir=tmp_path / "ledgers",
                                       quota_ledger_path=tmp_path / "quota.sqlite3"))
        plan = retention_manifest.manifest(manager)
        result = retention_manifest.apply(manager, plan)

        assert [row["taskId"] for row in result["cleared"]] == [doomed]
        assert not (store.root / "tasks" / doomed).exists()
        assert (store.root / "tasks" / kept).is_dir()
        assert ledger.read_bytes() == ledger_before
        assert outside.read_text(encoding="utf-8") == "别动我"
        # 墓碑留下：以后问起它是"明细已被清理"，不是"没有这个任务"。
        assert manager.store.get(doomed).details_cleared_at is not None
        # 跑第二次是空操作。
        assert retention_manifest.apply(manager, retention_manifest.manifest(manager))["cleared"] == []


def test_apply_rechecks_the_deadline_instead_of_trusting_the_plan(tmp_path):
    """清单是几分钟前算的：执行时再判一次，重新被认领的会话不该因为一张旧清单丢数据。"""
    app = make_app(tmp_path, SyntheticPlaces(at_origin()))
    with TestClient(app) as client:
        task_id, _view = run_one(client)
        store = app.state.checkups.store
        manager = manager_for(Settings(_env_file=None, checkup_dir=store.root,
                                       hybrid_ledger_dir=tmp_path / "ledgers",
                                       quota_ledger_path=tmp_path / "quota.sqlite3"))
        plan = retention_manifest.manifest(manager)
        # 手工造一张"过期"的清单，指着一条其实还在期限内的记录。
        plan["toClear"] = [{"taskId": task_id}]
        result = retention_manifest.apply(manager, plan)
        assert result["cleared"] == []
        assert result["skipped"] == [{"taskId": task_id, "reason": "no_longer_expired"}]
        assert (store.root / "tasks" / task_id).is_dir()


def test_orphan_directories_are_listed_and_removed(tmp_path):
    """索引里没有行的目录：只按索引清理会把真实明细永远留在盘上。"""
    app = make_app(tmp_path, SyntheticPlaces(at_origin()))
    with TestClient(app) as client:
        run_one(client)
        store = app.state.checkups.store
        orphan = store.root / "tasks" / "orphan-task"
        orphan.mkdir(parents=True)
        (orphan / "revision-0001-isochrone.json").write_text(json.dumps({"secret": "明细"}), encoding="utf-8")

        manager = manager_for(Settings(_env_file=None, checkup_dir=store.root,
                                       hybrid_ledger_dir=tmp_path / "ledgers",
                                       quota_ledger_path=tmp_path / "quota.sqlite3"))
        plan = retention_manifest.manifest(manager)
        assert [row["taskId"] for row in plan["orphanDirectories"]] == ["orphan-task"]
        assert plan["orphanDirectories"][0]["bytes"] > 0

        result = retention_manifest.apply(manager, plan)
        assert {"taskId": "orphan-task", "reason": "orphan_directory"} in result["cleared"]
        assert not orphan.exists()
