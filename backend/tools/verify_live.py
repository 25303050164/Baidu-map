"""Explicit live walking verification. Stores only allowlisted response evidence."""
import argparse
import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import time

import httpx
from life_circle.coordinates import LocalProjection
from life_circle.engine import compute_isochrone
from life_circle.models import CancelToken, IsochroneRequest
from life_circle.providers import BaiduProvider
from life_circle.scheduler import Scheduler

from app.baidu import silence_transport_logs
from app.config import load_settings

ORIGIN = (121.526090, 31.259560)


async def run(args):
    settings = load_settings()
    if not settings.ak_configured or settings.analysis_qps is None:
        raise ValueError("Configure backend AK and ANALYSIS_QPS before live verification")
    silence_transport_logs()
    args.output.mkdir(parents=True, exist_ok=True)
    if args.full:
        from app.analyses import AnalysisInput, AnalysisManager
        manager = AnalysisManager(settings)
        job = manager.create(AnalysisInput(center={"lng": ORIGIN[0], "lat": ORIGIN[1]}, coordinateSystem="bd09ll", budget=args.budget, clientRequestId="live-full-verification"))
        while not job.task.done():
            await asyncio.wait([job.task], timeout=10)
            print(json.dumps(job.view()), flush=True)
        if job.result:
            (args.output / "analysis.json").write_text(json.dumps(job.result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
            print(json.dumps({"outcome": "full_result", "status": job.result["status"], "facilities": len(job.result["data"]["facilities"] or []), "facility_analysis": {k:v for k,v in (job.result["facilityAnalysis"] or {}).items() if k not in ("queries","routes","assessments")}}, ensure_ascii=False), flush=True)
        await manager.close()
        return
    records = []

    async def record(response):
        await response.aread()
        entry = {"at": datetime.now(timezone.utc).isoformat(), "http_status": response.status_code}
        try:
            payload = response.json()
            entry["baidu_status"] = payload.get("status")
            routes = payload.get("result", {}).get("routes", [])
            entry["routes"] = [{k: r.get(k) for k in ("distance", "duration")} for r in routes if isinstance(r, dict)]
            if len(records) == 0 and routes:
                route = routes[0]
                steps = route.get("steps")
                entry["route_keys"] = list(route)
                entry["steps_type"] = type(steps).__name__
                if isinstance(steps, list) and steps:
                    entry["first_step_keys"] = list(steps[0]) if isinstance(steps[0], dict) else []
                    entry["first_step_endpoints"] = {k: steps[0].get(k) for k in ("start_location", "end_location", "startLocation", "endLocation")}
        except (ValueError, AttributeError, TypeError):
            entry["invalid_response"] = True
        records.append(entry)
        with (args.output / "requests.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, ensure_ascii=False) + "\n")

    async with httpx.AsyncClient(trust_env=False, follow_redirects=False, event_hooks={"response": [record]}) as client:
        provider = BaiduProvider(settings.baidu_map_ak.get_secret_value(), client=client)
        request = IsochroneRequest(ORIGIN, "bd09ll", budget=args.budget, qps=settings.analysis_qps)
        projection = LocalProjection(ORIGIN)
        points = [projection.to_geographic((x, y)) for x, y in [(200, 0), (0, 200), (-200, 0)]]
        scheduler = Scheduler(request, provider, CancelToken())
        observations = await scheduler.observe_many(points)
        probe = {"origin": ORIGIN, "qps": settings.analysis_qps,
                 "observations": [asdict(o) for o in observations], "statistics": asdict(scheduler.stats)}
        (args.output / "probes.json").write_text(json.dumps(probe, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"stage": "probes", "observations": [{"duration": o.duration, "reason": o.reason, "endpoint_verified": o.endpoint_verified} for o in observations], "requests": scheduler.stats.requests}), flush=True)
        if not any(o.duration is not None for o in observations) or args.probe_only:
            return
        await asyncio.sleep(1 / settings.analysis_qps)
        last = time.monotonic()

        def progress(p):
            nonlocal last
            if time.monotonic() - last >= 10:
                print(json.dumps(asdict(p)), flush=True)
                last = time.monotonic()

        result = await compute_isochrone(request, provider, on_progress=progress)
        (args.output / "isochrone.json").write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        print(json.dumps({"stage": "finished", "quality": result.quality, "stop_reason": result.stop_reason,
                          "requests": result.statistics.network_requests, "failures": result.statistics.failures,
                          "total_seconds": result.statistics.total_seconds, "warnings": result.warnings}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--budget", type=int, default=200)
    parser.add_argument("--probe-only", action="store_true")
    parser.add_argument("--full", action="store_true")
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except Exception as exc:
        print(json.dumps({"outcome": "verification_error", "type": type(exc).__name__}), flush=True)
        raise SystemExit(1)
