"""E8.2.1 closed-loop refinement: what the loop may spend, conclude and publish."""
import asyncio
import random

from shapely.geometry import MultiPolygon, Point, box, shape

from life_circle.models import CancelToken
from app.algorithms.baidu_e82 import compute_e82
from app.engines.baidu_e82 import e82_request
from tools.endpoint_multicross_boundary import close_patch_evidence, compute_multicross_boundary, mixed_edges
from tools.endpoint_multicross_experiment import RotatedSynthetic, truth_for
from tools.endpoint_radial_experiment import ORIGIN, local
from tools.endpoint_refinement_loop import (LoopState, Mesh, carve_conflicts, mesh_close_patch_evidence,
                                            mesh_mixed_edges)


def iou(result, case):
    pred, truth = local(result['geometry']), truth_for(case)
    return pred.intersection(truth).area / pred.union(truth).area


def run(case, budget, refinement, token=None):
    provider = RotatedSynthetic(case)
    result = asyncio.run(compute_multicross_boundary(
        e82_request(ORIGIN, budget, refinement=refinement), provider, token or CancelToken(),
        refinement=refinement))
    return result, provider


def records(seed, count=60):
    rng = random.Random(seed)
    return [dict(id=f'r{i}', xy=[rng.uniform(-400, 400), rng.uniform(-400, 400)],
                 duration=rng.choice((400, 1900)), origin=['same']) for i in range(count)]


# -- the shared mesh is the legacy geometry, only faster ---------------------------

def test_the_shared_mesh_reproduces_the_legacy_patch_assembly():
    for seed in range(5):
        evidence = records(seed)
        patch, base = box(-250, -250, 150, 200), box(-350, -300, 300, 350)
        blocked = [(10.0, 10.0)]
        legacy = close_patch_evidence(evidence, patch, base, blocked, target=25)
        fast = mesh_close_patch_evidence(Mesh(evidence), evidence, patch, base, blocked, target=25)
        for key in ('estimate', 'reachable', 'unreachable', 'unknown', 'combined_candidate'):
            assert legacy[key].symmetric_difference(fast[key]).area < 1e-3, (seed, key)
        assert legacy['conflicts'] == fast['conflicts']
        assert set(mixed_edges(evidence, patch, 25)) == set(mesh_mixed_edges(Mesh(evidence), patch, 25))


def test_faces_cached_inside_one_patch_assemble_a_wider_patch_exactly_as_fresh_ones():
    for seed in range(5):
        evidence = records(seed)
        mesh, faces = Mesh(evidence), {}
        base = box(-350, -300, 300, 350)
        # The second patch contains the first: every face cached there is reused here.
        for patch in (box(-250, -250, 150, 200), box(-300, -280, 250, 300)):
            fresh = mesh_close_patch_evidence(mesh, evidence, patch, base, [], target=25)
            cached = mesh_close_patch_evidence(mesh, evidence, patch, base, [], target=25, faces=faces)
            for key in ('estimate', 'reachable', 'unreachable', 'unknown', 'combined_candidate'):
                assert fresh[key].symmetric_difference(cached[key]).area < 1e-6, (seed, key)
            assert fresh['conflicts'] == cached['conflicts']
        assert faces, seed


def test_faces_that_do_not_form_a_coverage_go_to_the_overlay_unchanged():
    import numpy as np
    from tools.endpoint_geometry import polygon_union
    from tools.endpoint_refinement_loop import _coverage
    faces = np.array([box(0, 0, 2, 2), box(1, 1, 3, 3)], dtype=object)  # overlapping
    merged = _coverage(faces)
    assert len(merged) == 2
    assert abs(polygon_union(merged).area - 7) < 1e-9
    tiles = np.array([box(0, 0, 1, 1), box(1, 0, 2, 1)], dtype=object)  # a true coverage
    assert len(_coverage(tiles)) == 1 and abs(_coverage(tiles)[0].area - 2) < 1e-9


def test_rebuilds_run_on_a_worker_thread_not_the_event_loop(monkeypatch):
    import threading
    import tools.endpoint_refinement_loop as loop
    on_loop_thread, rebuild = [], loop.rebuild

    def spy(state):
        on_loop_thread.append(threading.current_thread() is threading.main_thread())
        return rebuild(state)
    monkeypatch.setattr(loop, 'rebuild', spy)
    run('circle', 200, 'loop')
    assert on_loop_thread and not any(on_loop_thread)


# -- spending: explore past the smooth case, never past the budget ----------------

def test_a_smooth_circle_keeps_exploring_but_the_budget_is_a_ceiling():
    legacy, _ = run('circle', 400, 'legacy')
    loop, provider = run('circle', 400, 'loop')
    # The legacy path stops at 112 calls when no patch opens; the loop keeps going,
    # and stops by itself once nothing is worth a query.
    assert legacy['calls'] == 112
    assert 112 < loop['calls'] == provider.calls <= 400
    assert loop['refinementLoop']['stopReason'] in ('no_ambiguity_left', 'budget')
    assert iou(loop, 'circle') >= iou(legacy, 'circle')


def test_budgets_are_hard_limits_for_every_tier():
    for budget in (200, 400, 800):
        result, provider = run('reentry_rotated33', budget, 'loop')
        assert result['calls'] == provider.calls <= budget
        actions = result['refinementLoop']['calls']
        assert sum(actions.values()) + 0 <= budget


def test_a_narrow_protrusion_between_rays_is_found():
    legacy, _ = run('narrow', 400, 'legacy')
    loop, _ = run('narrow', 400, 'loop')
    # 16 rays miss the spike; directions and exploration between them find it.
    assert iou(loop, 'narrow') > iou(legacy, 'narrow') + 0.02


def test_the_loop_is_deterministic():
    first, _ = run('reentry_rotated11', 400, 'loop')
    second, _ = run('reentry_rotated11', 400, 'loop')
    assert first['geometry'] == second['geometry']
    assert first['calls'] == second['calls']
    assert first['refinementLoop']['counts'] == second['refinementLoop']['counts']


def test_a_cancelled_run_publishes_no_boundary_and_the_adapter_says_why():
    token = CancelToken()
    token.cancel()
    provider = RotatedSynthetic('circle')
    result = asyncio.run(compute_e82(e82_request(ORIGIN, 400), provider, token))
    assert result.geometry is None and provider.calls == 0
    # Nothing established: the whole square is unresolved, never "unreachable".
    assert abs(local(result.unknown_region).area - box(-1600, -1600, 1600, 1600).area) < 1e-3
    assert 'run_incomplete' in result.warnings or result.stop_reason == 'cancelled'


# -- publication -----------------------------------------------------------------

def test_known_negatives_are_never_covered_by_the_published_boundary():
    for case in ('reentry_diagnostic', 'island_diagnostic', 'snap80'):
        result, _ = run(case, 400, 'loop')
        if result['geometry'] is None:
            continue
        geometry = shape(result['geometry'])
        for evidence in result['observationEvidence']:
            if evidence['accepted'] and evidence['observed_duration'] > 900:
                assert not geometry.covers(Point(evidence['route_destination'])), case


def test_carving_removes_the_negative_and_never_a_positive():
    class _Scheduler:
        stats = type('S', (), {'requests': 0})()
        remaining = 0
    class _Session:
        domain = box(-500, -500, 500, 500)
        scheduler = _Scheduler()
        records = [dict(id='n', xy=[0, 0], duration=1900), dict(id='p1', xy=[8, 0], duration=400),
                   dict(id='p2', xy=[-200, 0], duration=400)]
    state = LoopState.__new__(LoopState)
    state.session = _Session()
    estimate = box(-300, -300, 300, 300)
    carved, disk, items = carve_conflicts(state, estimate)
    assert not carved.covers(Point(0, 0))
    assert carved.covers(Point(8, 0)) and carved.covers(Point(-200, 0))
    # min(50, 0.49 d) with the nearest positive 8 m away: no lower bound that could reach it.
    assert items == [dict(id='n', radiusM=0.49 * 8)]
    assert 0 < disk.area < 3.2 * (0.49 * 8) ** 2


def test_the_unresolved_region_is_reported_with_its_overlap():
    provider = RotatedSynthetic('reentry_rotated33')
    result = asyncio.run(compute_e82(e82_request(ORIGIN, 400), provider, CancelToken()))
    area = result.metadata['unresolvedArea']
    unknown = local(result.unknown_region)
    assert abs(area['insideM2'] + area['outsideM2'] - unknown.area) < 1
    assert 'not an error bound' in result.metadata['unknownRegionMeaning']
    assert result.to_dict()['config']['config_version'] == 'local-multicross-e82.1'
    assert result.to_dict()['algorithm'] == 'local-multicross-e82'


def test_a_legacy_run_without_patches_no_longer_calls_the_whole_square_unknown():
    provider = RotatedSynthetic('circle')
    result = asyncio.run(compute_e82(e82_request(ORIGIN, 400, refinement='legacy'), provider,
                                     CancelToken()))
    assert result.geometry is not None
    assert local(result.unknown_region).area == 0
    assert isinstance(local(result.unknown_region), MultiPolygon)
