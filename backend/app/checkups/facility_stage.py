"""The facility stage: §4.1's query range and the retrieval over it.

The range is the computed boundary expanded by :data:`QUERY_PADDING_M` in the
metric plane. That margin covers the current uncertainty band, entrance offsets
and approximate coordinate conversion — it is an engineering allowance and never
evidence that the real directory is complete.
"""
from shapely import make_valid
from shapely.geometry import Polygon, box, shape
from shapely.ops import transform, unary_union

from life_circle.coordinates import LocalProjection

from ..poi.online import QueryDomain
from .models import QUERY_PADDING_M

# Round joins of the buffer: eight segments per quarter circle keep a 1300 m
# offset within a few metres of a true circular one, far inside the block
# granularity the planner works at.
QUAD_SEGS = 8


def _polygonal(geometry):
    """The polygonal parts of a boundary geometry; anything else is not a region."""
    parts = getattr(geometry, 'geoms', None) or [geometry]
    return [part for part in parts if isinstance(part, Polygon) and not part.is_empty]


def local_region(geometry, origin):
    """The published boundary in the metric plane, ready for a containment test.

    Every spatial question in this stage is asked in metres about the request
    centre (§5.5), so the boundary is projected once and then answered with
    shapely rather than by comparing degrees.
    """
    projection = LocalProjection(origin)
    try:
        # The published geometry is in the request's own coordinate system, which
        # is what ``LocalProjection`` projects from; nothing re-derives it here.
        return make_valid(transform(lambda x, y, z=None: projection.to_local((x, y)),
                                    shape(geometry)))
    except Exception:
        raise ValueError('invalid_boundary_geometry') from None


def query_domain(geometry, origin, padding=QUERY_PADDING_M) -> tuple[QueryDomain, bool]:
    """§4.1: the boundary expanded by the margin, as the planner's domain.

    Returns the domain and whether it is wider than §4.1's expansion. Holes are
    left to the buffer, which is exactly what expanding a region means: one
    narrower than the margin closes, a wider one shrinks by the margin. Two
    things can then make the range wider than §4.1 asks. A boundary whose parts
    lie further apart than twice the margin buffers into disjoint ranges; one
    planner domain cannot hold both, so the range becomes their envelope. And the
    domain carries a single ring, so a hole that survives the margin is not part
    of it. Both only ever search more than §4.1 asks for, never less, and a
    facility is counted where the boundary puts it, not where the search range
    does.

    Two refusals, because they need different things done about them: a shape
    that cannot be read at all is an engine's payload to look at, while a shape
    that is readable but is no region is an empty boundary. Both are raised
    without the underlying message, which quotes the payload back.
    """
    parts = _polygonal(local_region(geometry, origin))
    if not parts:
        raise ValueError('empty_boundary_geometry')
    padded = unary_union([part.buffer(padding, quad_segs=QUAD_SEGS) for part in parts])
    if padded.is_empty:
        raise ValueError('empty_query_domain')
    widened = False
    if not isinstance(padded, Polygon):
        # Disjoint padded ranges: widen rather than drop one of them.
        padded, widened = box(*padded.bounds), True
    # A hole that outlived the margin is a place the boundary excludes and the
    # search range cannot: over-searching costs queries and never manufactures a
    # finding, which is reported rather than hidden.
    widened = widened or bool(padded.interiors)
    ring = tuple((float(x), float(y)) for x, y in padded.exterior.coords[:-1])
    return QueryDomain(ring), widened
