"""Reachable points along the routes Baidu returned, kept apart from measured evidence.

A route is a walkable path: the time to any vertex along it bounds the walking time
to that vertex from above, so a vertex reached within 900 s is reachable whatever
route Baidu would give for it directly. Past 900 s a route proves nothing; the point
where it crosses 900 s is only a hint where the boundary may run on that road.

These points never enter the BoundarySession: its records stay "measured actual
endpoints", with their exact-duration conflicts and repeat counts. The loop merges
them into its mesh at each rebuild, under its own ids (``rp-``, ``rc-``).
"""
import math

import numpy as np
import shapely

SOURCE_KIND = 'route_prefix'


class PrefixStore:
    def __init__(self, session, *, spacing_m=50, merge_m=10, max_seconds=900):
        self.session = session
        self.spacing_m, self.merge_m, self.max_seconds = spacing_m, merge_m, max_seconds
        self.records, self.crossings = [], []
        self._cells = {}
        self._seen = 0

    def _cell(self, xy):
        return math.floor(xy[0] / self.merge_m), math.floor(xy[1] / self.merge_m)

    def _near(self, cells, xy):
        cx, cy = self._cell(xy)
        return any(math.dist(xy, other['xy']) <= self.merge_m
                   for dx in (-1, 0, 1) for dy in (-1, 0, 1) for other in cells.get((cx + dx, cy + dy), ()))

    def absorb(self):
        """Take the routes of every observation ingested since the last call."""
        session = self.session
        observations, log = session.observations, session.log
        if self._seen >= len(observations):
            return 0
        # Measured evidence wins: no prefix point within merge distance of one.
        real = {}
        for xy in session._points:
            real.setdefault(self._cell(xy), []).append(dict(xy=xy))
        added = 0
        for observation, row in zip(observations[self._seen:], log[self._seen:]):
            path, seconds = observation.route_path, observation.route_path_seconds
            if not row['accepted'] or not path or len(seconds) != len(path):
                continue
            local = [session.projection.to_local(p) for p in path]
            last = None
            # A micrometre of slack: spacing survives the bd09 round trip of each vertex.
            spacing = self.spacing_m - 1e-6
            for xy, value in zip(local, seconds):
                if value > self.max_seconds:
                    break
                if last is not None and math.dist(xy, last) < spacing:
                    continue
                last = xy
                if math.hypot(*xy) < spacing or self._near(real, xy) or self._near(self._cells, xy):
                    continue
                record = dict(id=f'rp-{len(self.records):05d}', xy=list(xy), duration=float(value),
                              origin=session.origin, request_xy=list(xy), source_kind=SOURCE_KIND,
                              source_id=row['id'])
                self.records.append(record)
                self._cells.setdefault(self._cell(xy), []).append(record)
                added += 1
            if (observation.observed_duration or 0) > self.max_seconds:
                for (a, ta), (b, tb) in zip(zip(local, seconds), zip(local[1:], seconds[1:])):
                    if ta <= self.max_seconds < tb:
                        f = (self.max_seconds - ta) / (tb - ta)
                        self.crossings.append(dict(id=f'rc-{len(self.crossings):05d}', source_id=row['id'],
                                                   xy=[a[0] + f * (b[0] - a[0]), a[1] + f * (b[1] - a[1])]))
                        break
        self._seen = len(observations)
        return added

    def select(self, region, band_m):
        """Prefix points within ``band_m`` of the region's boundary or outside it.

        Road vertices deep inside would only split filled faces and hide the large
        ones interior exploration looks for.
        """
        if not self.records or region is None or region.is_empty:
            return list(self.records)
        xy = np.array([r['xy'] for r in self.records], dtype=float)
        points = shapely.points(xy)
        keep = shapely.dwithin(region.boundary, points, band_m) | ~shapely.intersects_xy(region, xy[:, 0], xy[:, 1])
        return [r for r, ok in zip(self.records, keep) if ok]
