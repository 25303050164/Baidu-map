"""Adapt the team's E8.2 core to task/POI contracts without changing its evidence."""
from dataclasses import replace

from shapely.geometry import MultiPolygon, box, shape
from shapely.ops import transform

from life_circle.coordinates import LocalProjection
from life_circle.models import IsochroneResult, Statistics
from life_circle.providers import AnalyticProvider
from tools.endpoint_geometry import business_geometry
from tools.endpoint_multicross_boundary import compute_multicross_boundary
from tools.endpoint_refinement_loop import DEFAULT_CONFIG

ALGORITHM = 'local-multicross-e82'
# The algorithm identity stays the same; the refinement is versioned in the
# request's config_version. E8.2.1 is the closed-loop refinement; E8.2.2 is the
# same loop with the switches the 2026-10-01 experiments kept.
REFINEMENT_VERSIONS = {'legacy': ALGORITHM, 'loop': 'local-multicross-e82.1', 'loop2': 'local-multicross-e82.2'}
DEFAULT_REFINEMENT = 'loop'
# E8.2.2: gap probes taking turns with golden-angle ones, no stop on a batch that
# only hit the cache, route-prefix evidence with a 90 s margin (the live check saw
# a direct query up to 84 s slower than the time along the route), batched scans.
LOOP_CONFIGS = {'loop2': replace(DEFAULT_CONFIG, gap_explore=True, stall_continue=True, route_prefix=True,
                                 prefix_max_seconds=810, scan_batch=True)}
_BY_VERSION = {version: name for name, version in REFINEMENT_VERSIONS.items()}


class EndpointAnalyticProvider(AnalyticProvider):
    """Synthetic points have exact endpoints; never fabricate real route endpoints."""
    async def query_walking_time(self, origin, destination, deadline):
        observation = await super().query_walking_time(origin, destination, deadline)
        return replace(observation, request_origin=origin, route_origin=origin,
                       route_destination=destination, origin_offset_m=0, destination_offset_m=0)


async def compute_e82(request, provider, token, *, on_progress=None, refinement=None, loop_config=None):
    if refinement is None:
        # The request names its refinement; an unknown version string means legacy.
        refinement = _BY_VERSION.get(request.config_version, 'legacy')
    # A versioned refinement brings its own switches; ``loop_config`` is for offline
    # experiments only, and never overrides what a request's version names.
    if refinement in LOOP_CONFIGS:
        refinement, loop_config = 'loop', LOOP_CONFIGS[refinement]
    raw = await compute_multicross_boundary(request, provider, token,
        allow_network=provider.network, on_progress=on_progress, refinement=refinement,
        loop_config=loop_config)
    projection = LocalProjection(request.origin)
    domain = box(-request.extent, -request.extent, request.extent, request.extent)
    empty = business_geometry(MultiPolygon(), projection)

    def local(geometry):
        if geometry is None:
            return None
        return transform(lambda x, y, z=None: projection.to_local((x, y)), shape(geometry))

    geometry = raw['geometry']
    local_geometry = local(geometry)
    established = local_geometry is not None and not local_geometry.is_empty
    # Without a boundary nothing is established: the whole square is unresolved,
    # never inferred to be unreachable. With one, the region is what the solver
    # reports as unresolved; a run that reports none has none.
    unknown = raw.get('unknownRegion')
    if not established:
        unknown = business_geometry(domain, projection)
    elif unknown is None:
        unknown = empty
    local_unknown = local(unknown)
    stats = Statistics(**raw['_statistics'])
    stats.unknown_area = local_unknown.area
    stats.unfinished_boundary = raw['completion']['unresolvedEdges']
    warnings = ['e82_experimental_estimate', 'interior_not_independently_verified']
    if raw.get('truncated'):
        warnings.append('range_truncated')
    if raw['quality'] == 'experimental_evidence_conflict':
        warnings.append('known_negative_inside_estimate')
    loop = raw.get('refinementLoop') or {}
    if loop.get('carvedNegatives'):
        warnings.append('known_negative_carved')
    if raw.get('status') == 'cancelled' or raw['completion'].get('runComplete') is False:
        warnings.append('run_incomplete')
    quality = 'partial' if established else 'insufficient'
    return IsochroneResult(
        geometry=geometry, uncertain_region=raw.get('uncertaintyBand') or empty,
        unknown_region=unknown, computation_extent=business_geometry(domain, projection),
        quality=quality, stop_reason=raw['completion']['reason'], statistics=stats,
        warnings=warnings, config=request, local_geometry=local_geometry,
        local_unknown=local_unknown, time_bands=[dict(minutes=15, geometry=geometry)],
        sample_observations=raw['_observations'],
        metadata=dict(algorithm=ALGORITHM, validationStatus='not_independently_validated',
            completion=raw['completion'], assumption=raw['assumption'],
            unknownRegionMeaning=('solver-unresolved region; may overlap geometry; not an error bound'
                                  if established else 'no boundary established'),
            unresolvedArea=dict(insideM2=local_unknown.intersection(local_geometry).area if established else 0,
                                outsideM2=local_unknown.difference(local_geometry).area if established
                                else local_unknown.area),
            evidence={k: v for k, v in raw.items() if not k.startswith('_')}))
