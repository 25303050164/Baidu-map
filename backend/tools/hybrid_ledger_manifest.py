"""v1 hybrid 接口的运行残留：只读清单 + 显式清理。

`/api/v1/analysis/hybrid` 每跑一次就把 ``plan.json``、``ledger.json``、
``partial-result.json``、``diagnostics.json`` / ``failure.json``、``result.json`` 写进
``hybrid_ledger_dir/<task_id>/``，而它**从来不删**：`HybridManager.prune()` 清的只是内存里的
job（1800 秒、或最近 20 个），磁盘上那些文件既没有人读回来，也没有人来收。它们只是诊断残留。

所以判据不是另发明一套，而是把那段代码的口径搬到磁盘上：**内存里还留着哪几次运行，磁盘上
就留哪几次**。``--keep-runs`` 与 ``--keep-seconds`` 的默认值就是 ``prune()`` 里的 20 与 1800。

两个刻意的限制：

* **默认只读**。与体检明细的清单工具同一条规矩：为了被审阅而跑的工具，不该顺手改动它
  正在回答的那份数据。
* **只删 ``hybrid_ledger_dir`` 下的一级目录**，而且只删清单里逐条列出的那些。目录之外的东西
  （不是目录的条目、别的路径）只**报告**，绝不删 —— 一个清理工具删到它没在清单里承诺过的
  东西，是最不该发生的事。
"""
import argparse
import json
import shutil
import sys
import time
from pathlib import Path

#: 与 `HybridManager.prune()` 同源：内存里留最近这么多次运行，以及这么久之内的运行。
DEFAULT_KEEP_RUNS = 20
DEFAULT_KEEP_SECONDS = 1800.0


def _newest_mtime(directory: Path) -> float:
    """这一次运行最后一次写盘的时刻。目录自己的 mtime 只到目录项变化，不够用。"""
    newest = directory.stat().st_mtime
    for item in directory.rglob("*"):
        if item.is_file():
            newest = max(newest, item.stat().st_mtime)
    return newest


def _bytes(directory: Path) -> int:
    return sum(item.stat().st_size for item in directory.rglob("*") if item.is_file())


def inventory(root: Path, *, now: float | None = None, keep_runs: int = DEFAULT_KEEP_RUNS,
              keep_seconds: float = DEFAULT_KEEP_SECONDS) -> dict:
    """这份目录里每一次运行的清单：留哪些、清哪些、为什么，以及不属于它的东西。"""
    now = time.time() if now is None else now
    root = Path(root)
    if not root.is_dir():
        return {"hybridLedgerDir": str(root), "exists": False, "runs": [], "toClear": [],
                "kept": [], "foreignEntries": [], "totals": {}}
    directories = sorted((item for item in root.iterdir() if item.is_dir()),
                         key=_newest_mtime, reverse=True)
    runs, to_clear, kept = [], [], []
    for index, directory in enumerate(directories):
        newest = _newest_mtime(directory)
        files = sorted(item.name for item in directory.rglob("*") if item.is_file())
        recent = now - newest < keep_seconds
        row = {
            "taskId": directory.name,
            "finishedAt": newest,
            "ageSeconds": round(now - newest, 3),
            "bytes": _bytes(directory),
            "files": files,
            # 内存里的口径有两条，命中任一条就还在：时间之内，或者还在最近若干次里。
            "withinNewestRuns": index < keep_runs,
            "withinSeconds": recent,
        }
        row["keep"] = row["withinNewestRuns"] or row["withinSeconds"]
        row["reason"] = ("within_newest_runs" if row["withinNewestRuns"]
                         else "within_seconds" if recent else "beyond_memory_prune")
        runs.append(row)
        (kept if row["keep"] else to_clear).append(row)
    foreign = [{"name": item.name, "isDirectory": item.is_dir()}
               for item in sorted(root.iterdir()) if not item.is_dir()]
    return {
        "hybridLedgerDir": str(root),
        "exists": True,
        "keepRuns": keep_runs,
        "keepSeconds": keep_seconds,
        "runs": runs,
        "toClear": to_clear,
        "kept": kept,
        "foreignEntries": foreign,
        "totals": {
            "runs": len(runs),
            "toClear": len(to_clear),
            "bytesToClear": sum(row["bytes"] for row in to_clear),
            "bytesKept": sum(row["bytes"] for row in kept),
            "foreignEntries": len(foreign),
        },
    }


def apply(plan: dict) -> dict:
    """执行清单里的删除。只删清单里逐条列出的目录，别的一律不碰。"""
    root = Path(plan["hybridLedgerDir"])
    removed, failed = [], []
    for row in plan["toClear"]:
        directory = root / row["taskId"]
        # 再核一次它确实在 root 底下、确实是个目录：清单是几分钟前算的。
        if not directory.is_dir() or directory.parent != root:
            failed.append({"taskId": row["taskId"], "reason": "not_a_managed_directory"})
            continue
        freed = _bytes(directory)
        shutil.rmtree(directory, ignore_errors=True)
        (failed if directory.exists() else removed).append(
            {"taskId": row["taskId"], "bytes": freed})
    return {"removed": removed, "failed": failed,
            "bytesFreed": sum(row["bytes"] for row in removed)}


def report_text(plan: dict) -> str:
    if not plan.get("exists"):
        return f"hybrid 运行残留清单\n  目录不存在：{plan['hybridLedgerDir']}"
    totals = plan["totals"]
    lines = [
        "hybrid 运行残留清单（v1 /api/v1/analysis/hybrid 的诊断残留）",
        f"  目录：{plan['hybridLedgerDir']}",
        f"  内存保留口径：最近 {plan['keepRuns']} 次或 {plan['keepSeconds']:.0f} 秒之内",
        f"  运行 {totals['runs']} 次：清理 {totals['toClear']}"
        f"（{totals['bytesToClear']} 字节），保留 {totals['bytesKept']} 字节",
        "",
    ]
    for row in plan["toClear"]:
        lines.append(f"  清理 {row['taskId']}  {row['bytes']:>10} 字节"
                     f"  最后一次写盘 {row['ageSeconds'] / 3600:.1f} 小时前")
    for row in plan["foreignEntries"]:
        lines.append(f"  不动 {row['name']}（不是运行目录）")
    if not plan["toClear"]:
        lines.append("  没有需要清理的运行残留。")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="执行清单里的删除；不给这个参数时只打印清单")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出清单")
    parser.add_argument("--dir", type=Path, default=None,
                        help="hybrid_ledger_dir（默认取部署配置）")
    parser.add_argument("--env-file", type=Path, default=None, help="部署的 .env；不给则不读")
    parser.add_argument("--keep-runs", type=int, default=DEFAULT_KEEP_RUNS)
    parser.add_argument("--keep-seconds", type=float, default=DEFAULT_KEEP_SECONDS)
    options = parser.parse_args(argv)

    if options.dir is None:
        from app.config import Settings
        root = Settings(_env_file=options.env_file).hybrid_ledger_dir
    else:
        root = options.dir
    plan = inventory(root, keep_runs=options.keep_runs, keep_seconds=options.keep_seconds)
    if not options.apply:
        print(json.dumps(plan, ensure_ascii=False, indent=2) if options.json
              else report_text(plan))
        print("（只读清单：加 --apply 才会删除上面逐条列出的运行目录。）",
              file=sys.stderr if options.json else sys.stdout)
        return 0
    result = apply(plan)
    print(json.dumps({"plan": plan, "result": result}, ensure_ascii=False, indent=2)
          if options.json else report_text(plan) + "\n\n"
          + json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
