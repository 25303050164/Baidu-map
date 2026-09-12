import asyncio
import threading

from life_circle.engine import compute_isochrone
from life_circle.models import IsochroneRequest
from life_circle.providers import AnalyticProvider


def test_progress_is_immutable_and_does_not_change_sampling():
    async def run():
        request = IsochroneRequest((116.404, 39.915), "bd09ll", budget=200)
        snapshots = []
        provider = lambda: AnalyticProvider(request.origin, lambda x, y: (x*x+y*y)**.5/1.2)
        actual = await compute_isochrone(request, provider(), on_progress=snapshots.append)
        reference = await compute_isochrone(request, provider())
        assert actual.geometry == reference.geometry
        assert actual.statistics.requests == reference.statistics.requests
        assert snapshots[0].stage == "initializing"
        assert snapshots[-1].stage == "completed"
        assert len(snapshots) > 10
        assert snapshots[-1].requests == actual.statistics.requests
        assert [p.requests for p in snapshots] == sorted(p.requests for p in snapshots)
        try:
            snapshots[0].requests = 99
            assert False, "snapshot must be immutable"
        except (AttributeError, TypeError):
            pass
    asyncio.run(run())


def test_reconstruction_runs_off_event_loop(monkeypatch):
    import life_circle.engine as engine
    original = engine.reconstruct
    caller = threading.get_ident()
    threads = []

    def reconstruct(*args):
        threads.append(threading.get_ident())
        return original(*args)

    monkeypatch.setattr(engine, "reconstruct", reconstruct)
    request = IsochroneRequest((116.404, 39.915), "bd09ll", budget=200)
    asyncio.run(compute_isochrone(request, AnalyticProvider(request.origin, lambda x, y: 2000)))
    assert threads and all(thread != caller for thread in threads)
