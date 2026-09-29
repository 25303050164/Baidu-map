"""Field-reviewed corrections to the OSM water layer, scoped to the area each review checked.

A review is a committed JSON file (``data/water-reviews/*.json``) written after
comparing the OSM obstacle layer with independent evidence (imagery, a second
map vendor). It may only do five things, and only inside its own ``extent``:

* replace the width of named OSM water lines with a measured one, along the
  stretch where it was measured (``corridor``, which may reach past ``extent``);
* add water the OSM layer omits, when independent sources agree it is water;
* confirm that an OSM bridge spans the channel, so a bridge the measured width
  outgrows still counts as a crossing;
* mark areas where the sources disagree and nothing settled it -- these are
  neither water nor land, and the assessment reports them as unknown;
* record where the Baidu basemap draws water on verified land -- display only.

A review applies to exactly one OSM extract (``appliesTo``). Outside its extent,
or against another extract, the OSM layer is used unchanged and the area counts
as unreviewed.
"""
from dataclasses import dataclass, field
from functools import lru_cache
import json
from pathlib import Path

from shapely import make_valid
from shapely.geometry import shape
from shapely.ops import transform, unary_union

from ...geo.projection import MetricProjection

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class WaterReview:
    review_id: str
    version: str
    extent: object
    #: ``{osm_id: (width_m, corridor)}`` -- the width holds only inside its corridor.
    widths: dict = field(default_factory=dict)
    supplements: tuple = ()
    conflicts: tuple = ()
    misdrawn: tuple = ()
    crossings: frozenset = frozenset()
    payload: dict = field(default_factory=dict, compare=False, repr=False)

    @property
    def label(self) -> str:
        return f'{self.review_id}@{self.version}'


def _metric(projection, geometry):
    g = transform(projection.forward.transform, shape(geometry))
    return make_valid(g) if not g.is_valid else g


def parse_review(payload, projection, source):
    """One review, or ``(None, reason)`` when it is malformed or for another extract."""
    if payload.get('schemaVersion') != SCHEMA_VERSION or payload.get('coordinateSystem') != 'wgs84':
        return None, 'water_review_schema_invalid'
    applies = payload.get('appliesTo') or {}
    if (applies.get('osmDataVersion') != source.get('osm_data_version')
            or applies.get('sourcePbfSha256') != source.get('source_pbf_sha256')):
        return None, 'water_review_version_mismatch'
    rows = lambda key: tuple((_metric(projection, item['geometry']), item) for item in payload.get(key, []))
    extent = _metric(projection, payload['extent'])
    widths = {}
    for reach in payload.get('reaches', []):
        width = reach.get('widthM')
        if reach.get('status') == 'confirmed' and isinstance(width, (int, float)) and 0 < width <= 1000:
            corridor = _metric(projection, reach['corridor']) if reach.get('corridor') else extent
            widths[int(reach['osmId'])] = (float(width), corridor)
    return WaterReview(review_id=payload['reviewId'], version=payload['version'],
                       extent=extent, widths=widths,
                       supplements=tuple(r for r in rows('supplements') if r[1].get('status') == 'confirmed'),
                       conflicts=rows('conflicts'), misdrawn=rows('basemapMisdrawn'),
                       crossings=frozenset(int(c['osmId']) for c in payload.get('crossings', [])
                                           if c.get('status') == 'confirmed'),
                       payload=payload), None


@lru_cache(maxsize=4)
def _cached_reviews(directory, stamp, crs, osm_version, pbf_sha):
    projection = MetricProjection(crs)
    source = {'osm_data_version': osm_version, 'source_pbf_sha256': pbf_sha}
    reviews, rejected = [], []
    for name, _mtime, _size in stamp:
        path = Path(directory) / name
        try:
            review, reason = parse_review(json.loads(path.read_text(encoding='utf-8')), projection, source)
        except (OSError, ValueError, KeyError, TypeError):
            review, reason = None, 'water_review_schema_invalid'
        if review is None:
            rejected.append((name, reason))
        else:
            reviews.append(review)
    return tuple(reviews), tuple(rejected)


def load_reviews(directory, projection, source):
    """All review files in ``directory``: ``(applicable, rejected)``.

    Rejected files keep their name and reason; the caller decides whether a
    rejected review touches its extent and must be reported.
    """
    if directory is None:
        return (), ()
    try:
        directory = Path(directory).resolve()
        files = sorted(directory.glob('*.json'))
        stamp = tuple((f.name, f.stat().st_mtime_ns, f.stat().st_size) for f in files)
    except OSError:
        return (), ()
    return _cached_reviews(str(directory), stamp, str(projection.crs),
                           source.get('osm_data_version'), source.get('source_pbf_sha256'))


def review_catalog(directory):
    """Which reviews this deployment carries, for the capabilities endpoint.

    ``bbox`` is the extent's bounds in bd09ll, so a client can tell whether a
    stored revision computed before a review lies inside it (and is outdated).
    """
    from ...geo.coordinates import wgs84_to_bd09
    items = []
    if directory is None:
        return items
    try:
        files = sorted(Path(directory).glob('*.json'))
    except OSError:
        return items
    for path in files:
        try:
            payload = json.loads(path.read_text(encoding='utf-8'))
            lng0, lat0, lng1, lat1 = shape(payload['extent']).bounds
            (a, b), (c, d) = wgs84_to_bd09(lng0, lat0), wgs84_to_bd09(lng1, lat1)
            items.append({'reviewId': payload['reviewId'], 'version': payload['version'],
                          'label': f"{payload['reviewId']}@{payload['version']}",
                          'title': payload.get('title'), 'reviewedAt': payload.get('reviewedAt'),
                          'osmDataVersion': (payload.get('appliesTo') or {}).get('osmDataVersion'),
                          'bbox': [a, b, c, d]})
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return items


def rejected_extent(directory, projection, name):
    """A rejected review's extent, so a stale review over the current area is reported."""
    try:
        payload = json.loads((Path(directory) / name).read_text(encoding='utf-8'))
        return _metric(projection, payload['extent'])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def union(geometries):
    parts = [g for g in geometries if g is not None and not g.is_empty]
    return unary_union(parts) if parts else None
