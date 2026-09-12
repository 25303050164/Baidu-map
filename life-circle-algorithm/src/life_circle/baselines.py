"""32-ray star-shaped baseline; its representation cannot preserve holes."""
import math
import time

from shapely import union_all
from shapely.geometry import Polygon, box

from .coordinates import LocalProjection
from .field import business_geometry, multipolygon
from .models import CancelToken, IsochroneResult
from .scheduler import Scheduler


async def compute_radial(request, provider, cancel_token=None, *, clock=None):
    scheduler = Scheduler(request, provider, cancel_token or CancelToken(), clock)
    projection = LocalProjection(scheduler.origin)
    extent = request.extent
    rays = []
    for i in range(32):
        angle = i * 2 * math.pi / 32
        unit = (math.cos(angle), math.sin(angle))
        limit = extent / max(abs(unit[0]), abs(unit[1]))
        rays.append({"unit": unit, "limit": limit, "samples": {0.: scheduler.cache[scheduler.origin]}})
    if request.budget < 128 or (request.qps and 127 / request.qps >= request.deadline_seconds):
        raise ValueError("配置不足以完成初始化")
    points, keys = [], []
    for index, ray in enumerate(rays):
        for layer in range(1, 5):
            radius = ray["limit"] * layer / 4
            points.append(projection.to_geographic(tuple(radius * v for v in ray["unit"])))
            keys.append((index, radius))
    for (index, radius), observation in zip(keys, await scheduler.observe_many(points)):
        rays[index]["samples"][radius] = observation

    def outer_interval(ray):
        samples = sorted(ray["samples"].items())
        reachable = [i for i, (_, o) in enumerate(samples) if o.reachable]
        index = max(reachable)  # The zero anchor is always present.
        if index == len(samples) - 1:
            return samples[index], None
        return samples[index], samples[index + 1]

    while not scheduler._stopped():
        candidates = []
        for index, ray in enumerate(rays):
            lo, hi = outer_interval(ray)
            if hi and hi[1].duration is not None and hi[0] - lo[0] >= 2 * request.min_spacing:
                candidates.append((-(hi[0] - lo[0]), index, (lo[0] + hi[0]) / 2))
        if not candidates:
            break
        # Round-robin width priority, independent of provider completion ordering.
        for _, index, radius in sorted(candidates)[:min(scheduler.remaining, request.concurrency)]:
            ray = rays[index]
            point = projection.to_geographic(tuple(radius * v for v in ray["unit"]))
            ray["samples"][radius] = await scheduler.query(point)
            if scheduler._stopped():
                break
    scheduler.close()
    compute_started = time.perf_counter()
    boundaries, full, unknown_rays = [], [], []
    truncated = False
    for ray in rays:
        lo, hi = outer_interval(ray)
        radius = lo[0]
        unknown = hi is not None and hi[1].duration is None
        if hi is not None and not unknown:
            a, b = lo[1].duration, hi[1].duration
            radius += (900 - a) / (b - a) * (hi[0] - lo[0])
        elif hi is None:
            truncated = True
        boundaries.append(tuple(radius * v for v in ray["unit"]))
        full.append(tuple(ray["limit"] * v for v in ray["unit"]))
        unknown_rays.append(unknown)
    polygons, supports = [], []
    for index in range(32):
        other = (index + 1) % 32
        if not (unknown_rays[index] or unknown_rays[other]):
            supports.append(Polygon([(0, 0), full[index], full[other]]))
            polygon = Polygon([(0, 0), boundaries[index], boundaries[other]])
            if polygon.area > 0:
                polygons.append(polygon)
    domain = box(-extent, -extent, extent, extent)
    support = union_all(supports)
    unknown = domain.difference(support)
    geometry = multipolygon(union_all(polygons))
    warnings = ["star_shape_limitation"]
    if truncated:
        warnings.append("range_truncated")
    if any(unknown_rays):
        warnings.append("range_unknown")
    stats = scheduler.stats
    stats.unknown_area = unknown.area
    stats.compute_seconds = time.perf_counter() - compute_started
    stats.total_seconds = scheduler.clock.time() - scheduler.started
    insufficient = support.is_empty
    return IsochroneResult(
        None if insufficient else business_geometry(geometry, projection), business_geometry(domain, projection),
        business_geometry(unknown, projection), business_geometry(domain, projection),
        "insufficient" if insufficient else "partial", scheduler.stop_reason or "resolution_limit",
        stats, warnings, request, None if insufficient else geometry, unknown,
    )
