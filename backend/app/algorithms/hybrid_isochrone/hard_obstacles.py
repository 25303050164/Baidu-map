"""Versioned, spatially indexed water constraints, independent of route labels."""
from dataclasses import dataclass, field
from functools import lru_cache
import json
import math
import re
from pathlib import Path

from shapely import make_valid
from shapely.geometry import MultiPolygon, Point, LineString, shape, mapping
from shapely.ops import transform, unary_union, linemerge
from shapely.strtree import STRtree

from ...geo.projection import MetricProjection
from .polygon_builder import multipolygon
from .water_review import load_reviews, rejected_extent


def width_m(value):
    match = re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*(?:m)?\s*', str(value or ''))
    width = float(match[1]) if match else None
    return width if width is not None and math.isfinite(width) and 0 < width <= 1000 else None


def yes(value):
    return value is not None and str(value).lower() not in ('no', '0', 'false', 'none', '')


def walkable_bridge(tags):
    if not yes(tags.get('bridge')) or yes(tags.get('tunnel')):
        return False
    foot, access = tags.get('foot'), tags.get('access')
    if foot in ('no', 'private') or (access in ('no', 'private') and foot not in ('yes', 'designated', 'permissive')):
        return False
    return foot in ('yes', 'designated', 'permissive') or tags.get('highway') in (
        'footway', 'path', 'pedestrian', 'steps', 'residential', 'living_street',
        'service', 'unclassified', 'tertiary', 'secondary', 'primary', 'cycleway') and foot != 'use_sidepath'


@dataclass
class LocalObstacles:
    water: object = field(default_factory=lambda: MultiPolygon([]))
    bridges: list = field(default_factory=list)
    unresolved: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    source: dict = field(default_factory=dict)
    unresolved_lines: list = field(default_factory=list)
    #: Reviewed "sources disagree" areas: neither water nor land (water_review.py).
    conflicts: object = field(default_factory=lambda: MultiPolygon([]))
    conflict_items: list = field(default_factory=list)
    #: Reviews applied here, and the part of the local extent they checked.
    reviews: list = field(default_factory=list)
    reviewed: object = field(default_factory=lambda: MultiPolygon([]))
    #: ``(review label, osm_id, width_m, buffered surface)`` for each reviewed width.
    reach_items: list = field(default_factory=list)
    #: ``{bridge osm_id: review label}`` for crossings a review confirmed.
    crossings: dict = field(default_factory=dict)
    #: The metric extent this layer was loaded for.
    extent: object = None

    @property
    def uncertain(self):
        """Geometry no fill may touch: water lines without a width, and data conflicts."""
        return [*self.unresolved_lines, *getattr(self.conflicts, 'geoms', [])]

    def mask(self, samples, shell, bridge_width=3.0):
        """Bridge exception needs dry endpoints and positive evidence on both banks.

        The exception is clipped to the existing coverage shell. It never grows
        coverage on the far bank or paints the surface above a tunnel.
        """
        positives = [Point(s.xy) for s in samples if s.evidence.reachable is True]
        passages, records = [], []
        for line, tags in self.bridges:
            if not walkable_bridge(tags) or not line.intersects(self.water):
                continue
            if line.geom_type == 'MultiLineString':
                line = linemerge(line)
            if line.geom_type != 'LineString':
                continue
            review = self.crossings.get(tags.get('osm_id'))
            if review:
                # A reviewed crossing spans the channel; a measured width wider
                # than the OSM segment must not strand it mid-river.
                line = extend_to_dry(line, self.water)
            ends = [Point(line.coords[0]), Point(line.coords[-1])]
            # A partial bridge segment ending in water is not a complete crossing.
            if any(self.water.contains(p) for p in ends):
                continue
            if not all(any(p.distance(s) <= 100 and not self.water.covers(s)
                           and LineString([p, s]).intersection(self.water).length <= .1
                           for s in positives) for p in ends):
                continue
            width = width_m(tags.get('width')) or bridge_width
            corridor = line.buffer(width / 2, cap_style=2).intersection(shell)
            passages.append(corridor)
            records.append(dict(osm_id=tags.get('osm_id'), width_m=width,
                                width_source='osm' if width_m(tags.get('width')) else 'display_default',
                                evidence_policy='positive_sample_within_100m_at_each_dry_endpoint',
                                **({'span_source': 'water_review', 'water_review': review} if review else {})))
        corridors = multipolygon(unary_union(passages))
        return multipolygon(self.water.difference(corridors)), corridors, records


def extend_to_dry(line, water, limit=10.0, step=.5):
    """Prolong each end that sits in water along the bridge axis, at most ``limit`` metres."""
    coords = list(line.coords)
    for end, inner in ((0, 1), (-1, -2)):
        (x, y), (ix, iy) = coords[end][:2], coords[inner][:2]
        length = ((x - ix) ** 2 + (y - iy) ** 2) ** .5
        if not length or not water.contains(Point(x, y)):
            continue
        ux, uy = (x - ix) / length, (y - iy) / length
        d = step
        while d <= limit and water.contains(Point(x + ux * d, y + uy * d)):
            d += step
        coords[end] = (x + ux * d, y + uy * d)
    return LineString(coords)


class ObstacleIndex:
    def __init__(self, payload, projection, version):
        if payload.get('schema_version') != 1 or payload.get('coordinate_system') != 'wgs84':
            raise ValueError('hard_obstacle_schema_invalid')
        if payload.get('osm_data_version') != version:
            raise ValueError('hard_obstacle_version_mismatch')
        self.rows = []
        self.source = {k: payload.get(k) for k in ('osm_data_version', 'source_pbf_sha256', 'schema_version')}
        for f in payload['features']:
            g = transform(projection.forward.transform, shape(f['geometry']))
            if not g.is_empty:
                self.rows.append((make_valid(g), f['properties']))
        self.index = STRtree([g for g, _ in self.rows])

    def local(self, extent, reviews=()):
        reviews = [r for r in reviews
                   if r.extent.intersects(extent) or any(c.intersects(extent) for _, c in r.widths.values())]
        water, lines, bridges, unresolved, unresolved_lines = [], [], [], [], []
        # Include complete nearby bridge geometries, not just the clipped middle.
        for i in sorted(map(int, self.index.query(extent.buffer(200), predicate='intersects'))):
            g, p = self.rows[i]
            underground = yes(p.get('tunnel')) or p.get('location') == 'underground' or yes(p.get('covered'))
            if p.get('kind') == 'water' and not underground:
                if g.geom_type in ('Polygon', 'MultiPolygon'):
                    water.append(g.intersection(extent))
                elif g.geom_type in ('LineString', 'MultiLineString'):
                    lines.append((g.intersection(extent), p))
            elif p.get('kind') == 'bridge':
                bridges.append((g, p))
        # Water a review confirmed and OSM omits is water like any other surface.
        water.extend(g.intersection(extent) for r in reviews for g, _ in r.supplements if g.intersects(extent))
        surface = multipolygon(unary_union(water))
        buffered, reach_items = [], []
        for line, p in lines:
            if line.is_empty or line.difference(surface.buffer(1)).length <= 1:
                continue
            # A reviewed width replaces the tag only along the stretch where it was measured.
            for review in reviews:
                width, corridor = review.widths.get(p.get('osm_id'), (None, None))
                if width and line.intersects(corridor):
                    reach = line.intersection(corridor).buffer(width / 2).intersection(extent)
                    buffered.append(reach)
                    reach_items.append((review.label, p.get('osm_id'), width, reach))
                    line = line.difference(corridor)
            if line.is_empty or line.difference(surface.buffer(1)).length <= 1:
                continue
            width = width_m(p.get('width'))
            if width:
                buffered.append(line.buffer(width / 2).intersection(extent))
            else:
                unresolved_lines.append(line)
                unresolved.append(dict(osm_id=p.get('osm_id'), reason='water_line_missing_surface_or_width',
                                       uncovered_length_m=line.difference(surface.buffer(1)).length,
                                       geometry=mapping(line)))
        water = multipolygon(unary_union([surface, *buffered]))
        conflict_items = [(g.intersection(extent), item) for r in reviews for g, item in r.conflicts
                          if g.intersects(extent)]
        crossings = {osm_id: r.label for r in reviews for osm_id in r.crossings}
        source = dict(self.source)
        if reviews:
            source['water_reviews'] = [r.label for r in reviews]
        return LocalObstacles(water, bridges, unresolved,
                              ['hard_obstacle_water_lines_unresolved'] if unresolved else [], source, unresolved_lines,
                              conflicts=multipolygon(unary_union([g for g, _ in conflict_items])),
                              conflict_items=conflict_items, reviews=reviews, reach_items=reach_items,
                              crossings=crossings, extent=extent,
                              reviewed=multipolygon(unary_union([r.extent.intersection(extent) for r in reviews])))


@lru_cache(maxsize=2)
def _cached_index(path, mtime_ns, size, crs, version):
    return ObstacleIndex(json.loads(Path(path).read_text(encoding='utf-8')), MetricProjection(crs), version)


def load_obstacles(path, projection, version, extent, reviews_dir=None):
    try:
        path = Path(path)
        stat = path.stat()
        index = _cached_index(str(path.resolve()), stat.st_mtime_ns, stat.st_size,
                              str(projection.crs), version)
    except (OSError, ValueError, KeyError, TypeError):
        return LocalObstacles(warnings=['hard_obstacle_layer_unavailable_or_invalid'])
    reviews, rejected = load_reviews(reviews_dir, projection, index.source)
    local = index.local(extent, reviews)
    # A review written for another extract (or unreadable) that covers this area
    # means known-bad OSM data is in use here: say so rather than apply it blindly.
    stale = [name for name, _ in rejected
             if (g := rejected_extent(reviews_dir, projection, name)) is None or g.intersects(extent)]
    if stale:
        local.warnings.append('water_review_not_applied')
        local.source['water_reviews_rejected'] = stale
    return local
