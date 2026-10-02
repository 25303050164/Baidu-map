"""Route-prefix evidence: what is taken from a route, and what the loop does with it."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace

from shapely.geometry import Point, box, shape

from life_circle.models import CancelToken, RouteObservation
from app.engines.baidu_e82 import e82_request
from tools.endpoint_multicross_boundary import compute_multicross_boundary
from tools.endpoint_radial_experiment import ORIGIN, P, Synthetic
from tools.endpoint_refinement_loop import DEFAULT_CONFIG
from tools.endpoint_route_prefix import PrefixStore


def session_with(routes, measured=()):
    """A stand-in session: one accepted observation per (local path, seconds) route."""
    observations, log = [], []
    for i, (path, seconds) in enumerate(routes):
        geographic = [P.to_geographic(xy) for xy in path]
        observations.append(RouteObservation(geographic[-1], seconds[-1], observed_duration=seconds[-1],
                                             route_path=geographic, route_path_seconds=seconds))
        log.append(dict(id=f'e6-{i:05d}', accepted=True))
    return SimpleNamespace(projection=P, observations=observations, log=log, origin=['o'],
                           _points={tuple(xy): None for xy in measured})


def east(length=1000, step=25, pace=1.0):
    path = [(x, 0.0) for x in range(0, length + 1, step)]
    return path, [x * pace for x, _ in path]


def xs(store):
    return sorted(round(r['xy'][0]) for r in store.records)


def test_points_within_900_s_are_thinned_along_the_route_and_the_crossing_is_kept():
    store = PrefixStore(session_with([east()]))
    store.absorb()
    # Every 50 m from 50 m out (the origin's own vicinity is not evidence) up to 900 s.
    assert xs(store) == list(range(50, 901, 50))
    assert all(r['source_kind'] == 'route_prefix' and r['duration'] <= 900 for r in store.records)
    assert [round(c['xy'][0]) for c in store.crossings] == [900]


def test_repeats_merge_and_measured_evidence_wins():
    session = session_with([east(), east()], measured=[(300.0, 2.0)])
    store = PrefixStore(session)
    store.absorb()
    assert 300 not in xs(store) and len(xs(store)) == 17
    assert store.absorb() == 0  # nothing new since the last call


def test_only_points_near_the_boundary_or_outside_are_selected():
    store = PrefixStore(session_with([east()]))
    store.absorb()
    selected = store.select(box(-500, -500, 500, 500), 150)
    assert sorted(round(r['xy'][0]) for r in selected) == list(range(350, 901, 50))


class PathSynthetic(Synthetic):
    """The circle field, with a straight route whose time grows with distance."""
    async def query_walking_time(self, origin, destination, deadline):
        observation = await super().query_walking_time(origin, destination, deadline)
        if observation.observed_duration is None or observation.route_destination is None:
            return observation
        x, y = P.to_local(observation.route_destination)
        path = [P.to_geographic((x * i / 5, y * i / 5)) for i in range(6)]
        return replace(observation, route_path=path,
                       route_path_seconds=[observation.observed_duration * i / 5 for i in range(6)])


def test_the_loop_with_route_evidence_is_deterministic_and_within_budget():
    config = replace(DEFAULT_CONFIG, route_prefix=True, crossing_hints=True)

    def once():
        provider = PathSynthetic('ellipse')
        result = asyncio.run(compute_multicross_boundary(
            e82_request(ORIGIN, 200), provider, CancelToken(), refinement='loop', loop_config=config))
        return result, provider
    first, provider = once()
    second, _ = once()
    assert first['calls'] == provider.calls <= 200
    assert first['geometry'] == second['geometry']
    assert first['refinementLoop']['routePrefix']['points'] > 0
    geometry = shape(first['geometry'])
    for evidence in first['observationEvidence']:
        if evidence['accepted'] and evidence['observed_duration'] > 900:
            assert not geometry.covers(Point(evidence['route_destination']))
