"""The route-prefix consistency check: which vertices it asks about, and how it judges."""
from life_circle.coordinates import LocalProjection
from life_circle.models import RouteObservation
from app.test_origin import NORMALIZED_TEST_ORIGIN
from tools.route_prefix_check import PASS, metrics, pick_targets

P = LocalProjection(tuple(NORMALIZED_TEST_ORIGIN))


def route(seconds_per_m=1.0, length=1200, step=30, steps_timed=True):
    path = [P.to_geographic((x, 0.0)) for x in range(0, length + 1, step)]
    seconds = [x * seconds_per_m for x in range(0, length + 1, step)]
    return RouteObservation(path[-1], seconds[-1], observed_duration=seconds[-1], route_path=path,
                            route_path_seconds=seconds if steps_timed else [])


def test_it_asks_about_the_vertex_nearest_600_s_and_the_last_one_within_900_s():
    picked = pick_targets(route())
    assert [p['target_s'] for p in picked] == [600, 900]
    assert [round(p['prefix_s']) for p in picked] == [600, 900]
    assert all(p['timing'] == 'steps' for p in picked)


def test_a_route_without_step_times_is_timed_by_distance_and_says_so():
    picked = pick_targets(route(steps_timed=False))
    assert [p['timing'] for p in picked] == ['distance', 'distance']
    assert round(picked[-1]['prefix_s']) == 900


def row(prefix, direct, target=900, offset=5):
    return dict(id='x', route='r', target_s=target, prefix_s=prefix, observed_duration=direct,
                endpoint_verified=True, reason=None, destination_offset_m=offset, timing='steps')


def ledger(rows):
    return dict(requests_used=len(rows) + 20, stop_reason=None, routes=[dict(timed_by_steps=True)] * 20,
                direct=rows)


def test_small_differences_pass_and_a_reachable_prefix_slower_than_900_fails():
    good = [row(600 + i, 600 + i + (i % 5), target=600) for i in range(20)] + \
           [row(880 - i, 880 - i - (i % 3)) for i in range(20)]
    result = metrics(ledger(good))
    assert result['decidable'] == 40 and result['passed']
    unsafe = good[:-1] + [row(800, 950)]
    failed = metrics(ledger(unsafe))
    assert not failed['passed'] and failed['unsafe'] == ['x']


def test_an_endpoint_too_far_from_the_vertex_is_not_decidable():
    result = metrics(ledger([row(700, 705, offset=PASS['margin_s'] + 80)]))
    assert result['decidable'] == 0 and not result['passed']
