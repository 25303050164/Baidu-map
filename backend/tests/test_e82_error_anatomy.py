"""Error anatomy on hand-built truth grids: parts, depth bands and re-entry."""
import numpy as np
from shapely.geometry import Point

from tools.e82_error_anatomy import anatomy, band_shares, beyond_first_crossing


def grid(step=10, extent=800):
    xs = np.arange(-extent + step / 2, extent, step)
    gx, gy = np.meshgrid(xs, xs)
    return xs, gx, gy


def test_a_lobe_the_boundary_missed_is_one_deep_false_exclusion_part():
    xs, gx, gy = grid()
    truth = (np.hypot(gx, gy) <= 500) | ((gx > 450) & (gx <= 650) & (np.abs(gy) <= 50))
    valid = np.ones_like(truth)
    result = anatomy(Point(0, 0).buffer(500, quad_segs=64), xs, valid, truth, requests=[(0, 300)])
    parts = result['fe']['parts']
    assert len(parts) == 1
    lobe = parts[0]
    assert 1.2 < lobe['area_ha'] < 1.8
    assert 140 < lobe['depth_m'] < 160
    assert min(lobe['azimuth_deg'], 360 - lobe['azimuth_deg']) < 5
    assert lobe['nearest_request_m'] > 500
    assert result['fe']['bands'][-1] > .2  # a lobe 150 m deep is mostly beyond 100 m
    assert result['fi']['area_ha'] < .05


def test_truth_past_an_unreachable_ring_counts_as_reentry():
    xs, gx, gy = grid()
    radius = np.hypot(gx, gy)
    reach = (radius <= 300) | ((radius >= 400) & (radius <= 500))
    beyond = beyond_first_crossing(xs, np.ones_like(reach), reach)
    assert beyond[(radius >= 420) & (radius <= 500)].mean() > .95
    assert not beyond[radius <= 290].any()


def test_band_shares_split_by_distance_and_sum_to_one():
    shares = band_shares(np.array([0, 10, 30, 60, 99, 150, 400]))
    assert shares == [2 / 7, 1 / 7, 2 / 7, 2 / 7]
    assert band_shares(np.array([])) == [0.0] * 4
