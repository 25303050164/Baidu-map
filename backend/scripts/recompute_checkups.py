"""数据修订后离线重算已完成的体检任务：从已付费的证据重建，不联网、不花额度。

From backend::

    python scripts/recompute_checkups.py --reason "water_review guoding1-qiujiang@2026-09-28.1" TASK_ID...

任务目录取自 ``CHECKUP_DIR``（与服务同一个配置）。每个任务追加核验与报告两版修订，
``trace.recomputed`` 写明从哪一版来、为什么重算；旧修订原样保留。成圈读取修订后数据的
引擎（Hybrid）用存档台账回放重建边界，其余引擎的边界不变。新边界与原设施检索对不上时
拒绝重算并说明原因 —— 那需要新任务重新检索，而不是在旧检索上改数。

运行前先停掉指向同一目录的服务：修订计数由这一个进程独占推进。
"""
import argparse
import asyncio
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pydantic import SecretStr

from app.algorithms.osm_offline.lazy import LazyOsmOfflineEngine
from app.analyses import RateGate
from app.checkups import CheckupError, build_checkups
from app.config import load_settings


async def run(task_ids: list[str], reason: str) -> list[dict]:
    # 回放从不发请求；清空 AK，让任何意外的联网路径在出门之前就失败。
    settings = load_settings().model_copy(update={"baidu_map_ak": SecretStr("")})
    manager = build_checkups(settings, RateGate(settings.analysis_qps),
                             LazyOsmOfflineEngine(settings), quota=None)
    results = []
    for task_id in task_ids:
        before = manager.store.get(task_id).revision
        try:
            revisions = await manager.recompute(task_id, reason=reason)
        except CheckupError as exc:
            results.append({"taskId": task_id, "refused": exc.code, "message": exc.message})
            continue
        latest, _ = manager.snapshot(task_id)
        results.append({"taskId": task_id, "engine": latest.engine.engine_id,
                        "fromRevision": before, "published": revisions,
                        "isochroneHash": latest.trace.isochrone_hash,
                        "resultHash": latest.trace.result_hash,
                        "recomputed": latest.trace.recomputed,
                        "dataVersions": latest.trace.data_versions})
    return results


def main():
    parser = argparse.ArgumentParser(description="Recompute finished checkups offline after a data correction.")
    parser.add_argument("--reason", required=True, help="Recorded in trace.recomputed, e.g. the review id@version")
    parser.add_argument("task_ids", nargs="+")
    args = parser.parse_args()
    results = asyncio.run(run(args.task_ids, args.reason))
    print(json.dumps(results, ensure_ascii=False, indent=2))
    if any("refused" in item for item in results):
        sys.exit(1)


if __name__ == "__main__":
    main()
