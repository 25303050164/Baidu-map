"""v1 hybrid 运行残留的清单工具：口径与内存里的 prune 同源，默认只读，只删受管目录。

要钉住的三件事：

1. **判据来自代码**：内存里留最近 20 次或 1800 秒之内的运行，磁盘上就留同样的那些 ——
   两处判据不一致的那一天，"清单说该清"与"服务还在用"就会各说各话。
2. **默认什么都不改**，``--apply`` 才删。
3. **只删 root 下的一级目录**，而且只删清单里列出的：别的东西只报告。
"""
import json
import os
import time
from pathlib import Path

from tools import hybrid_ledger_manifest as tool


def make_run(root: Path, task_id: str, *, age_seconds: float, files=("plan.json", "ledger.json"),
             now: float = 2_000_000_000.0, size: int = 10) -> Path:
    """造一次运行：目录与文件的 mtime 都按 age 摆到过去。"""
    directory = root / task_id
    directory.mkdir(parents=True)
    for name in files:
        (directory / name).write_text("x" * size, encoding="utf-8")
    stamp = now - age_seconds
    for item in [directory, *directory.rglob("*")]:
        os.utime(item, (stamp, stamp))
    return directory


def test_keeps_what_the_memory_prune_keeps_and_clears_the_rest(tmp_path):
    root = tmp_path / "ledgers"
    root.mkdir()
    now = 2_000_000_000.0
    # 26 次运行（25 次很老 + 1 次刚写过）：内存里只留最近 20 次，所以最老的 6 次该被清。
    # 编号越大越老，所以最老的是 run-19..run-24。
    for index in range(25):
        make_run(root, f"run-{index:02d}", age_seconds=10_000 + index, now=now)
    make_run(root, "run-fresh", age_seconds=1.0, now=now)

    plan = tool.inventory(root, now=now)
    cleared = {row["taskId"] for row in plan["toClear"]}
    kept = {row["taskId"] for row in plan["kept"]}
    assert cleared == {f"run-{i:02d}" for i in range(19, 25)}, sorted(cleared)
    assert "run-fresh" in kept and len(kept) == 20
    assert plan["totals"]["bytesToClear"] == 6 * 2 * 10
    assert {row["reason"] for row in plan["toClear"]} == {"beyond_memory_prune"}
    # 清单自己也要说清"为什么留"：这两条理由就是内存 prune 的两条。
    assert {row["reason"] for row in plan["kept"]} == {"within_newest_runs"}


def test_a_directory_is_cleared_only_by_the_runs_rule_not_by_name(tmp_path):
    """名字不像 task id 的目录也照样按同一条判据处理：判据是时间与数量，不是名字。"""
    root = tmp_path / "ledgers"
    root.mkdir()
    now = 2_000_000_000.0
    make_run(root, "not-a-uuid", age_seconds=99_999, now=now)
    make_run(root, "recent", age_seconds=5, now=now)
    plan = tool.inventory(root, now=now, keep_runs=0, keep_seconds=60)
    assert [row["taskId"] for row in plan["toClear"]] == ["not-a-uuid"]
    assert [row["taskId"] for row in plan["kept"]] == ["recent"]


def test_the_inventory_reports_foreign_entries_and_never_deletes_them(tmp_path):
    root = tmp_path / "ledgers"
    root.mkdir()
    now = 2_000_000_000.0
    make_run(root, "old-run", age_seconds=99_999, now=now)
    (root / "notes.txt").write_text("别动我", encoding="utf-8")

    plan = tool.inventory(root, now=now, keep_runs=0, keep_seconds=60)
    assert [row["name"] for row in plan["foreignEntries"]] == ["notes.txt"]
    tool.apply(plan)
    assert (root / "notes.txt").read_text(encoding="utf-8") == "别动我"
    assert not (root / "old-run").exists()


def test_apply_removes_only_the_listed_runs_and_is_idempotent(tmp_path):
    root = tmp_path / "ledgers"
    root.mkdir()
    now = 2_000_000_000.0
    stale = make_run(root, "stale", age_seconds=99_999, now=now)
    fresh = make_run(root, "fresh", age_seconds=1, now=now)

    plan = tool.inventory(root, now=now, keep_runs=0, keep_seconds=60)
    result = tool.apply(plan)
    assert [row["taskId"] for row in result["removed"]] == ["stale"]
    assert result["failed"] == [] and result["bytesFreed"] == 20
    assert not stale.exists() and fresh.is_dir()
    # 跑第二次：清单空了，什么都不做。
    assert tool.apply(tool.inventory(root, now=now, keep_runs=0,
                                     keep_seconds=60))["removed"] == []


def test_the_command_line_only_apply_changes_anything(tmp_path, capsys):
    root = tmp_path / "ledgers"
    root.mkdir()
    now = time.time()
    make_run(root, "stale", age_seconds=99_999, now=now)
    before = sorted(item.name for item in root.iterdir())

    flags = ["--dir", str(root), "--keep-runs", "0", "--keep-seconds", "60"]
    assert tool.main([*flags, "--json"]) == 0
    assert sorted(item.name for item in root.iterdir()) == before, "只读清单不该改动任何东西"
    capsys.readouterr()

    assert tool.main([*flags, "--apply", "--json"]) == 0
    assert list(root.iterdir()) == []
    output = json.loads(capsys.readouterr().out)
    assert [row["taskId"] for row in output["result"]["removed"]] == ["stale"]


def test_a_missing_directory_is_reported_not_created(tmp_path):
    missing = tmp_path / "nope"
    plan = tool.inventory(missing)
    assert plan["exists"] is False and plan["toClear"] == []
    assert "目录不存在" in tool.report_text(plan)
    assert not missing.exists()
