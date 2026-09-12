import asyncio
from dataclasses import replace

import numpy as np
import pytest
from shapely.geometry import Point, box

from life_circle.models import CancelToken, IsochroneRequest, RouteObservation
from life_circle.mesh import Cell, Mesh
from life_circle.field import reconstruct, contour_field
from life_circle.engine import compute_isochrone
from life_circle.providers import AnalyticProvider
from life_circle.scheduler import Scheduler
from life_circle.coordinates import LocalProjection

ORIGIN = (116.4, 39.9)


def sample(mesh, function):
    for point in mesh.required_points():
        mesh.samples[point] = RouteObservation(point, function(*point))


def test_ut04_initialization_and_shared_split():
    mesh = Mesh(1600, 400)
    assert len(mesh.leaves) == 64
    assert len(mesh.required_points()) == 145
    cells = sorted(mesh.leaves)
    initial = set(mesh.required_points())
    mesh.split(cells[0])
    assert len(set(mesh.required_points()) - initial) == 8
    mesh.split(cells[1])
    assert len(set(mesh.required_points()) - initial) == 15


def test_ut04_initial_provider_count_exact():
    async def run():
        mesh = Mesh(1600, 400)
        p = LocalProjection(ORIGIN)
        provider = AnalyticProvider(ORIGIN, lambda x, y: 1000)
        s = Scheduler(IsochroneRequest(ORIGIN, "bd09ll"), provider, CancelToken())
        await s.observe_many([p.to_geographic(point) for point in mesh.required_points()])
        assert provider.calls == s.stats.requests == 144
        assert s.stats.unique_positions == 145
    asyncio.run(run())


def test_ut07_nonmonotone_shared_edge_keeps_multiple_crossings():
    mesh = Mesh(50, 100)
    sample(mesh, lambda x, y: 1200)
    for x, value in [(-50, 600), (0, 1200), (50, 600)]:
        mesh.samples[(x, -50)] = RouteObservation((x, -50), value)
    cell = next(iter(mesh.leaves))
    assert (-25, -50) in mesh.active_candidates(cell)
    assert (25, -50) in mesh.active_candidates(cell)
    mesh.active_points.update({(-25, -50), (25, -50)})
    for point in mesh.active_points:
        mesh.samples[point] = RouteObservation(point, 900)
    assert not any(p[1] == -50 for p in mesh.active_candidates(cell))


@pytest.mark.parametrize("center,corners", [(500, 1200), (1200, 500)])
def test_ut05_ut06_center_anomaly(center, corners):
    mesh = Mesh(200, 400)
    sample(mesh, lambda x, y: center if x == y == 0 else corners)
    cell = next(iter(mesh.leaves))
    score, candidate, residual = mesh.priority(cell, 120)
    assert candidate and score > 0 and residual > 60


def test_ut08_active_sample_changes_reconstruction():
    mesh = Mesh(50, 100)
    sample(mesh, lambda x, y: 900 + 4 * x)
    before = reconstruct(mesh, 10).geometry
    mesh.samples[(0, -50)] = RouteObservation((0, -50), 300)
    after = reconstruct(mesh, 10).geometry
    assert after.area > before.area + 100


def test_ut09_shared_edge_has_no_cracks():
    mesh = Mesh(400, 400)
    mesh.split(sorted(mesh.leaves)[0])
    sample(mesh, lambda x, y: 100)
    result = reconstruct(mesh, 25)
    assert result.geometry.is_valid
    assert result.geometry.area == pytest.approx(800 ** 2)
    assert result.unknown.is_empty
    # The fan triangles exactly partition the domain, including hanging nodes.
    triangles = mesh.triangles()
    assert sum(t[0].area for t in triangles) == pytest.approx(800 ** 2)


def test_ut10_saddle_threshold_plateau_deterministic():
    axis = np.array([-25., 0., 25.])
    for z in (np.full((3, 3), 900.), np.array([[800, 1000, 800], [1000, 900, 1000], [800, 1000, 800]])):
        a = contour_field(axis, axis, z, box(-25, -25, 25, 25))
        b = contour_field(axis, axis, z, box(-25, -25, 25, 25))
        assert a.is_valid and a.equals(b)
    assert contour_field(axis, axis, np.full((3, 3), 900.), box(-25, -25, 25, 25)).area == 2500


@pytest.mark.parametrize("transpose", [False, True])
def test_ut10_threshold_line_is_empty_area(transpose):
    axis = np.array([-25., 0., 25.])
    z = np.array([[1000, 1000, 1000], [900, 900, 900], [1000, 1000, 1000]])
    geometry = contour_field(axis, axis, z.T if transpose else z, box(-25, -25, 25, 25))
    assert geometry.is_empty and geometry.is_valid


def test_ut11_holes_and_components():
    axis = np.arange(-1500, 1501, 25.)
    x, y = np.meshgrid(axis, axis)
    z = 900 + np.maximum(300 - np.hypot(x, y), np.hypot(x, y) - 1000)
    geometry = contour_field(axis, axis, z, box(-1500, -1500, 1500, 1500))
    assert sum(len(p.interiors) for p in geometry.geoms) == 1
    assert not geometry.covers(Point(0, 0))
    z = 900 + np.minimum(np.hypot(x - 700, y) - 300, np.hypot(x + 700, y) - 300)
    assert len(contour_field(axis, axis, z, box(-1500, -1500, 1500, 1500)).geoms) == 2


def test_ut12_unknown_gap_never_filled():
    mesh = Mesh(400, 400)
    sample(mesh, lambda x, y: None if (x, y) == (0, 0) else 100)
    result = reconstruct(mesh, 25)
    assert result.unknown.area > 0
    assert result.geometry.intersection(result.unknown).area < 1e-7
    # Support clipping catches gaps even smaller than an output raster cell.
    axis = np.array([-25., 0., 25.])
    support = box(-25, -25, 25, 25).difference(box(7, 7, 8, 8))
    geometry = contour_field(axis, axis, np.zeros((3, 3)), support)
    assert geometry.intersection(box(7, 7, 8, 8)).area == 0


def test_ut12_masked_output_cells_count_toward_unknown():
    mesh = Mesh(50, 100)
    sample(mesh, lambda x, y: 100)
    mesh.samples[(-50, -50)] = RouteObservation((-50, -50), None)
    result = reconstruct(mesh, 25)
    # All supported times are reachable, so every omitted area is unknown.
    assert result.geometry.area + result.unknown.area == pytest.approx(10000)


def test_valid_empty_is_different_from_no_support():
    mesh = Mesh(50, 100)
    sample(mesh, lambda x, y: 1000)
    result = reconstruct(mesh, 25)
    assert result.geometry.is_empty and not result.support.is_empty


def test_budget_stopped_boundaries_are_reported():
    result = asyncio.run(compute_isochrone(
        IsochroneRequest(ORIGIN, "bd09ll", budget=145),
        AnalyticProvider(ORIGIN, lambda x, y: np.hypot(x, y) / 1.2)))
    assert result.quality == "partial"
    assert result.statistics.unfinished_boundary > 0
    assert result.stop_reason == "budget"


def test_expansion_reuses_all_initial_samples():
    result = asyncio.run(compute_isochrone(
        IsochroneRequest(ORIGIN, "bd09ll", budget=544, exploration_fraction=0),
        AnalyticProvider(ORIGIN, lambda x, y: 1)))
    assert result.statistics.requests == 544
    assert result.statistics.unique_positions == 545
    assert result.local_geometry.area == pytest.approx(6400 ** 2)
    assert "range_truncated" in result.warnings


def test_ut04_engine_144_queries():
    result = asyncio.run(compute_isochrone(
        IsochroneRequest(ORIGIN, "bd09ll", exploration_fraction=0, expand=False),
        AnalyticProvider(ORIGIN, lambda x, y: 2000), CancelToken()))
    assert result.statistics.requests >= 144  # The mathematical zero anchor triggers local refinement.
    assert result.statistics.unique_positions >= 145


def test_ut12_all_failed_is_insufficient():
    result = asyncio.run(compute_isochrone(
        IsochroneRequest(ORIGIN, "bd09ll"), AnalyticProvider(ORIGIN, lambda x, y: None), CancelToken()))
    assert result.geometry is None and result.quality == "insufficient"
    assert result.statistics.unknown_area == 3200 ** 2
    assert result.statistics.exploration_requests == 0


def test_ut15_range_expansion_budget_insufficient():
    request = IsochroneRequest(ORIGIN, "bd09ll", budget=200)
    result = asyncio.run(compute_isochrone(request, AnalyticProvider(ORIGIN, lambda x, y: 1), CancelToken()))
    assert "range_truncated" in result.warnings
    assert result.quality == "partial"
    assert result.statistics.requests <= 200


def test_plane_800_regression_and_ut17_completion_order():
    async def run():
        request = IsochroneRequest(ORIGIN, "bd09ll", budget=800)
        function = lambda x, y: np.hypot(x, y) / 1.2
        a = await compute_isochrone(request, AnalyticProvider(ORIGIN, function), CancelToken())

        class Reordered(AnalyticProvider):
            async def query_walking_time(self, origin, destination, deadline):
                if destination[0] > origin[0]:
                    await asyncio.sleep(0)
                return await super().query_walking_time(origin, destination, deadline)

        b = await compute_isochrone(request, Reordered(ORIGIN, function), CancelToken())
        truth = Point(0, 0).buffer(1080, quad_segs=256)
        assert a.local_geometry.intersection(truth).area / a.local_geometry.union(truth).area >= .95
        assert a.local_geometry.equals(b.local_geometry)
        assert a.statistics.requests == b.statistics.requests <= 800
    asyncio.run(run())


def test_initialization_configuration_rejected_before_calls():
    provider = AnalyticProvider(ORIGIN, lambda x, y: 0)
    for request in (IsochroneRequest(ORIGIN, "bd09ll", budget=100), IsochroneRequest(ORIGIN, "bd09ll", qps=.1)):
        with pytest.raises(ValueError, match="初始化"):
            asyncio.run(compute_isochrone(request, provider, CancelToken()))
    assert provider.calls == 0


def test_geometry_error_is_explicit_and_never_repaired(monkeypatch):
    from life_circle.field import GeometryError
    def broken(*args):
        raise GeometryError("fixture")
    monkeypatch.setattr("life_circle.engine.reconstruct", broken)
    result = asyncio.run(compute_isochrone(IsochroneRequest(ORIGIN, "bd09ll", budget=144), AnalyticProvider(ORIGIN, lambda x, y: 1000)))
    assert result.geometry is None and result.stop_reason == "geometry_error"
    assert result.statistics.unknown_area == 3200 ** 2


def test_active_samples_actually_used_by_engine():
    from dataclasses import replace
    request = IsochroneRequest(ORIGIN, "bd09ll", budget=800)
    function = lambda x, y: np.hypot(x, y) / 1.2
    active = asyncio.run(compute_isochrone(request, AnalyticProvider(ORIGIN, function)))
    disabled = asyncio.run(compute_isochrone(replace(request, active_sampling=False), AnalyticProvider(ORIGIN, function)))
    assert active.statistics.active_points > 0
    assert disabled.statistics.active_points == 0
    assert active.local_geometry.symmetric_difference(disabled.local_geometry).area > 1
