"""保留期清单：哪些明细该清、为什么、清掉之后还剩什么。

§5 B2 决策 2 对**已经落盘的历史数据**有明确口径：没有会话归属的历史报告视为已过期，明细
清除，保留不含明细的汇总与清理记录。运营者随后选的落地方式是"先给清单、人工确认之后再删"，
所以这个工具分两步：

* **默认只读**：打印清单（JSON 与人类可读两种），一个字节都不改。
* ``--apply`` 才动手，而且只删清单里逐条列出的 ``checkup_dir/tasks/<task_id>`` 目录。

判据与线上完全同源：调用 ``CheckupManager.retention_of``，不另写一套"什么算过期"。两套判据
迟早会分叉，而分叉的那一天没人知道该信谁。

额度账本、OSM 数据与任何别的东西都不在这个工具的删除范围内 —— 它只碰受管明细目录。
"""
import argparse
import json
import sys
import time
from pathlib import Path

from app.checkups import retention as retention_module
from app.config import Settings
from app.quota import Quota


def _manager(settings: Settings, *, read_only: bool = False):
    """只读地打开这份库：不建表、不迁移、不启动 worker、不发任何请求。"""
    from app.engines import build_registry
    from app.checkups import CheckupStore
    from app.checkups.manager import CheckupManager

    store = CheckupStore(settings.checkup_dir, read_only=read_only)
    store.create_schema()
    registry = build_registry(settings, None, None)
    return CheckupManager(settings, registry, store, Quota(settings))


def manifest(manager, *, now: float | None = None) -> dict:
    """这份库里每一条明细的去留。

    ``to_clear`` 与 ``kept`` 都是逐条列出来的，并且每条都带理由 —— 一份"共 51 条"的清单
    没法审阅。``orphan_directories`` 是磁盘上有目录、索引里没有任务的那些：只按索引删会把
    它们永远留在盘上，所以它们必须出现在清单里，而不是在删除时才发现。
    """
    store = manager.store
    known = {record.task_id for record in store.terminal_tasks()}
    rows, to_clear, kept = [], [], []
    for record in store.terminal_tasks():
        view = manager.retention_of(record, now=now)
        row = {
            "taskId": record.task_id,
            "status": record.status,
            "sessionId": record.session_id,
            "createdAt": record.created_at,
            "finishedAt": record.finished_at,
            "revision": record.revision,
            "detailsAvailable": view.details_available,
            "reason": view.reason,
            "expiresAt": view.expires_at,
            "alreadyCleared": record.details_cleared_at is not None,
            "directoryBytes": _directory_bytes(store.root / "tasks" / record.task_id),
        }
        rows.append(row)
        (kept if view.details_available else to_clear).append(row)
    orphans = [{"taskId": name,
                "bytes": _directory_bytes(store.root / "tasks" / name),
                "reason": "no_index_row"}
               for name in store.task_directories() if name not in known]
    baseline = store.retention_baseline_at()
    return {
        "checkupDir": str(store.root),
        "baselineAt": None if baseline == float("inf") else baseline,
        "baselineRecorded": baseline != float("inf"),
        "tasks": rows,
        "toClear": to_clear,
        "kept": kept,
        "orphanDirectories": orphans,
        "totals": {
            "tasks": len(rows),
            "toClear": len(to_clear),
            "alreadyCleared": sum(1 for row in rows if row["alreadyCleared"]),
            "orphanDirectories": len(orphans),
            "bytesToClear": sum(row["directoryBytes"] for row in to_clear)
            + sum(row["bytes"] for row in orphans),
            "bytesKept": sum(row["directoryBytes"] for row in kept),
        },
    }


def _directory_bytes(directory: Path) -> int:
    if not directory.is_dir():
        return 0
    return sum(item.stat().st_size for item in directory.rglob("*") if item.is_file())


def apply(manager, plan: dict) -> dict:
    """执行清单里列出的删除。

    逐条再判一次期限，而不是照着清单删：清单是几分钟前算的，而"这份数据现在还在期限内吗"
    必须由**执行的那一刻**回答 —— 一份被重新认领的会话不该因为一张旧清单而丢掉数据。
    """
    store = manager.store
    cleared, skipped, failed = [], [], []
    for row in plan["toClear"]:
        record = store.get(row["taskId"])
        if record.details_cleared_at is not None:
            skipped.append({"taskId": row["taskId"], "reason": "already_cleared"})
            continue
        if manager.retention_of(record).details_available:
            skipped.append({"taskId": row["taskId"], "reason": "no_longer_expired"})
            continue
        if not store.mark_details_cleared(record.task_id, now=time.time()):
            skipped.append({"taskId": row["taskId"], "reason": "lost_the_claim"})
            continue
        leftovers = retention_module.delete_details(store.root, record.task_id)
        manager.cache.drop_task(record.task_id)
        (failed if leftovers else cleared).append({"taskId": row["taskId"]})
    for row in plan["orphanDirectories"]:
        leftovers = retention_module.delete_details(store.root, row["taskId"])
        (failed if leftovers else cleared).append(
            {"taskId": row["taskId"], "reason": "orphan_directory"})
    return {"cleared": cleared, "skipped": skipped, "failed": failed}


def _report_text(plan: dict) -> str:
    lines = [
        f"体检明细保留期清单",
        f"  目录：{plan['checkupDir']}",
        f"  保留期基线：{plan['baselineAt']}"
        + ("" if plan["baselineRecorded"]
           else "（尚未记录：清单按「现有数据全部视为历史数据」列出；实施时会先写下基线）"),
        f"  任务 {plan['totals']['tasks']} 个："
        f"待清理 {plan['totals']['toClear']}（{plan['totals']['bytesToClear']} 字节，"
        f"含孤立目录 {plan['totals']['orphanDirectories']} 个），"
        f"仍保留 {plan['totals']['bytesKept']} 字节",
        "",
    ]
    for row in plan["toClear"]:
        lines.append(f"  清理 {row['taskId']}  {row['status']:<10} {row['reason']:<16}"
                     f" {row['directoryBytes']:>10} 字节")
    for row in plan["orphanDirectories"]:
        lines.append(f"  清理 {row['taskId']}  （索引里没有这一行） {row['bytes']:>10} 字节")
    if not plan["toClear"] and not plan["orphanDirectories"]:
        lines.append("  没有需要清理的明细。")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="执行清单里的删除；不给这个参数时只打印清单")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出清单")
    parser.add_argument("--read-only", action="store_true",
                        help="以只读方式打开这份库：不建表、不迁移、不写基线（默认）")
    parser.add_argument("--checkup-dir", type=Path, default=None,
                        help="体检数据目录（默认取部署配置）")
    parser.add_argument("--env-file", type=Path, default=None,
                        help="部署的 .env；不给则不读它")
    options = parser.parse_args(argv)

    settings = Settings(_env_file=options.env_file,
                        **({} if options.checkup_dir is None
                           else {"checkup_dir": options.checkup_dir}))
    # 清单工具**默认只读**：一份为了被审阅而跑的工具，不该顺手改动它正在回答的那份数据。
    # ``--apply`` 时仍然用只读连接算清单，写墓碑交给 apply 自己那一次写入。
    manager = _manager(settings, read_only=True)
    plan = manifest(manager)
    print(json.dumps(plan, ensure_ascii=False, indent=2)
          if options.json else _report_text(plan))
    if not options.apply:
        # JSON 输出要能直接被机器读走，所以这条说明走 stderr，不混进那一份 JSON 里。
        print("（只读清单：加 --apply 才会删除上面逐条列出的受管目录。）",
              file=sys.stderr if options.json else sys.stdout)
        return 0
    result = apply(manager, plan)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
