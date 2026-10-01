"""One live E8.2.2 boundary at the Guodingyi test origin, through the production engine.

BaiduE82Engine with E82_REFINEMENT=loop2 on the application's route gate, budget
400 (a hard ceiling: retries count), three attempts in flight at the configured
QPS. The snapshot is written in the shape of a checkup's isochrone revision, so
tools.e821_independent_validation can score it against the E8.2.1 polygon.

    python -m tools.e822_live_run --output OUT

A one-time claim; nothing is resumed.
"""
import argparse
import asyncio
import json
import time
from pathlib import Path

from app.config import load_settings
from app.engines.baidu_e82 import BaiduE82Engine
from app.engines.protocol import EngineContext, IsochroneAsk
from app.persistence import atomic_dump
from app.quota import Quota
from app.test_origin import NORMALIZED_TEST_ORIGIN
from life_circle.models import CancelToken
from tools.e821_independent_validation import claim

BUDGET = 400
INFLIGHT = 3


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    settings = load_settings().model_copy(update=dict(e82_refinement='loop2', baidu_direction_max_inflight=INFLIGHT))
    if settings.analysis_provider != 'baidu' or not settings.ak_configured:
        raise SystemExit('live_baidu_mode_required')
    quota = Quota(settings)
    engine = BaiduE82Engine(settings, quota.direction.gate)
    task_id = f'e822-live-{time.strftime("%Y%m%d-%H%M%S")}'
    claim(output / 'started.json', dict(task_id=task_id, budget=BUDGET, inflight=INFLIGHT,
                                        qps=quota.direction.gate.current_qps(), started_at=time.time()))
    reported = [0]

    def progress(snapshot):
        if snapshot.requests >= reported[0] + 50:
            reported[0] = snapshot.requests
            print(json.dumps(dict(stage=snapshot.stage, requests=snapshot.requests,
                                  seconds=round(snapshot.elapsed_seconds, 1))), flush=True)
    origin = tuple(NORMALIZED_TEST_ORIGIN)
    context = EngineContext(task_id=task_id, token=CancelToken(), deadline=time.monotonic() + 1200,
                            on_progress=progress)
    started = time.perf_counter()
    snapshot = asyncio.run(engine.compute(IsochroneAsk(origin=origin, budget=BUDGET), context))
    elapsed = time.perf_counter() - started
    isochrone = snapshot.model_dump(by_alias=True)
    atomic_dump(output / 'revision-e822-isochrone.json', dict(
        schemaVersion='e822-live-v1', taskId=task_id, generatedAt=time.time(),
        center=dict(lng=origin[0], lat=origin[1]), coordinateSystem='bd09ll', isochrone=isochrone,
        elapsedSeconds=elapsed))
    stats = isochrone['statistics']
    print(json.dumps(dict(task=task_id, config_version=isochrone['parameters']['config_version'],
                          requests=isochrone['requestsUsed'], network=isochrone['networkRequests'],
                          retries=stats['retries'], failures=stats['failures'], quality=isochrone['quality'],
                          stop=isochrone['stopReason'], elapsed_s=round(elapsed, 1),
                          warnings=isochrone['warnings']), ensure_ascii=False))


if __name__ == '__main__':
    main()
