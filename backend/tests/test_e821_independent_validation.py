import random

import pytest
from shapely.geometry import Point, box

from app.geo.projection import MetricProjection
from app.test_origin import NORMALIZED_TEST_ORIGIN
from tools import e821_independent_validation as v


def test_strata_partition_and_only_disagreement_separates_the_polygons():
    old, new = box(0, 0, 1000, 1000), box(200, 0, 1200, 1000)
    regions = v.strata(old, new, box(-2000, -2000, 3000, 3000))
    names = list(regions)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            assert regions[a].intersection(regions[b]).area < 1e-6
    rng = random.Random(1)
    for _ in range(2000):
        p = Point(rng.uniform(-500, 1700), rng.uniform(-500, 1500))
        if regions['new_only'].covers(p):
            assert new.covers(p) and not old.covers(p)
        elif regions['old_only'].covers(p):
            assert old.covers(p) and not new.covers(p)
        elif any(r.covers(p) for r in regions.values()):
            assert old.covers(p) == new.covers(p)
    assert regions['new_only'].area == regions['old_only'].area == pytest.approx(200 * 1000)


def test_draw_is_seeded_spaced_and_near_roads():
    projection = MetricProjection(v.METRIC_CRS)
    x, y = projection.origin(NORMALIZED_TEST_ORIGIN)
    regions = dict(a=box(x, y, x + 800, y + 800), b=box(x - 800, y, x, y + 800))
    reach = lambda xy: 10.0 if xy[0] > x - 400 else None  # the far half has no road
    first = v.draw(regions, dict(a=10, b=5), projection, reach, seed=7)
    assert first == v.draw(regions, dict(a=10, b=5), projection, reach, seed=7)
    assert [r['group'] for r in first] == ['a'] * 10 + ['b'] * 5
    assert all(r['xy'][0] > x - 400 for r in first)
    points = [r['xy'] for r in first]
    assert min(((p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2) ** .5
               for i, p in enumerate(points) for q in points[i + 1:]) >= v.MIN_SPACING_M
    with pytest.raises(ValueError, match='insufficient'):
        v.draw(regions, dict(b=5), projection, lambda xy: None, seed=7, attempts=500)


def _case(i, group, old, new):
    return dict(id=f'p{i:03d}', group=group, coordinate=[121.5 + i * 1e-4, 31.3], xy=[0, 0],
                old_inside=old, new_inside=new, new_unresolved=False)


def _sample(i, duration, reason=None, verified=True):
    return dict(id=f'p{i:03d}', observed_duration=duration, reason=reason, endpoint_verified=verified)


def test_metrics_scores_both_polygons_on_the_same_points():
    cases = [_case(1, 'new_only', False, True), _case(2, 'new_only', False, True),
             _case(3, 'old_only', True, False), _case(4, 'boundary', True, True),
             _case(5, 'boundary', False, False), _case(6, 'interior', True, True),
             _case(7, 'exterior', False, False)]
    plan = dict(cases=cases, allocation=dict(new_only=2, old_only=1, boundary=2, interior=1, exterior=1),
                stratum_area_m2=dict(new_only=800, old_only=200, boundary=4000, interior=5000, exterior=2000))
    samples = [_sample(1, 700), _sample(2, 910), _sample(3, 1200), _sample(4, 400),
               _sample(5, 950, 'endpoint_offset'), _sample(6, 300, verified=False), _sample(7, 1500)]
    result = v.metrics(plan, dict(requests_used=7, samples=samples))
    new, old = result['new_strict']['overall'], result['old_strict']['overall']
    assert (new['tp'], new['tn'], new['fp'], new['fn']) == (2, 2, 1, 0)
    assert (old['tp'], old['tn'], old['fp'], old['fn']) == (1, 2, 1, 1)
    assert result['new_tolerant']['overall']['within_tolerance'] == 1  # 910 s is within 15 s
    assert result['unknown_reasons'] == {'endpoint_offset': 1, 'missing_endpoints': 1}
    paired = result['paired']
    assert (paired['decidable'], paired['new_right'], paired['old_right']) == (3, 2, 1)
    assert paired['new_only'] == dict(decidable=2, new_right=1, old_right=1, reachable=1)
    # new_only: one right each way, no gain; old_only: 200 m² the old polygon wrongly included
    assert paired['net_area_gain_m2'] == 200
    assert v.sign_test(2, 1) == pytest.approx(1.0)
    assert v.sign_test(10, 0) == pytest.approx(2 / 1024)
