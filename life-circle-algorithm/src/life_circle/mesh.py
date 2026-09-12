"""A quadtree with a global sample registry and conforming shared-edge fans."""
import math
from dataclasses import dataclass

from shapely.geometry import Polygon, box


@dataclass(frozen=True, order=True)
class Cell:
    x: float
    y: float
    size: float

    @property
    def center(self):
        return (round(self.x + self.size / 2, 8), round(self.y + self.size / 2, 8))

    @property
    def corners(self):
        x, y, s = self.x, self.y, self.size
        return [(round(a, 8), round(b, 8)) for a, b in [(x, y), (x + s, y), (x + s, y + s), (x, y + s)]]

    @property
    def polygon(self):
        return box(self.x, self.y, self.x + self.size, self.y + self.size)


class Mesh:
    def __init__(self, extent, size):
        self.extent = extent
        self.leaves = set(self.coarse_cells(extent, size))
        self.samples = {}
        self.active_points = set()
        self._index_count = -1

    @staticmethod
    def coarse_cells(extent, size):
        count = round(2 * extent / size)
        return [Cell(-extent + x * size, -extent + y * size, size) for x in range(count) for y in range(count)]

    def required_points(self, cells=None):
        return sorted({p for c in (self.leaves if cells is None else cells) for p in [*c.corners, c.center]})

    def children(self, cell):
        half = cell.size / 2
        return [Cell(cell.x + dx, cell.y + dy, half) for dx in (0, half) for dy in (0, half)]

    def split(self, cell):
        self.leaves.remove(cell)
        self.leaves.update(self.children(cell))

    def expand(self, extent, size):
        old = self.extent
        added = [c for c in self.coarse_cells(extent, size) if not (-old <= c.x and c.x + c.size <= old and -old <= c.y and c.y + c.size <= old)]
        self.leaves.update(added)
        self.extent = extent
        return added

    def edges(self, cell):
        if self._index_count != len(self.samples):
            self._horizontal, self._vertical = {}, {}
            for x, y in self.samples:
                self._horizontal.setdefault(y, []).append(x)
                self._vertical.setdefault(x, []).append(y)
            for index in (self._horizontal, self._vertical):
                for key in index:
                    index[key].sort()
            self._index_count = len(self.samples)
        (x, y), _, (right_x, top_y), _ = cell.corners
        bottom = [(v, y) for v in self._horizontal.get(y, []) if x <= v <= right_x]
        right = [(right_x, v) for v in self._vertical.get(right_x, []) if y <= v <= top_y]
        top = [(v, top_y) for v in reversed(self._horizontal.get(top_y, [])) if x <= v <= right_x]
        left = [(x, v) for v in reversed(self._vertical.get(x, [])) if y <= v <= top_y]
        return [bottom, right, top, left]

    def observations(self, cell):
        points = {cell.center, *cell.corners, *(p for edge in self.edges(cell) for p in edge)}
        return [self.samples.get(p) for p in sorted(points)]

    def priority(self, cell, band=120):
        valid = [o.duration for o in self.observations(cell) if o is not None and o.duration is not None]
        if not valid:
            return 0, False, 0
        crossing = min(valid) <= 900 < max(valid)
        near = max(0, 1 - min(abs(t - 900) for t in valid) / band)
        corners = [self.samples.get(p) for p in cell.corners]
        center = self.samples.get(cell.center)
        residual = 0
        if center and center.duration is not None and all(o and o.duration is not None for o in corners):
            residual = abs(center.duration - sum(o.duration for o in corners) / 4)
        span = max(valid) - min(valid)
        candidate = crossing or min(abs(t - 900) for t in valid) <= band or residual > 60 or span > 300
        score = cell.size / 400 * (4 * crossing + 2 * near + 2 * min(residual / 120, 1) + min(span / 300, 1))
        return score, candidate, residual

    def active_candidates(self, cell, spacing=25):
        candidates = []
        for edge in self.edges(cell):
            if sum(p in self.active_points for p in edge) >= 2:
                continue
            for a, b in zip(edge, edge[1:]):
                ta, tb = self.samples[a].duration, self.samples[b].duration
                if ta is None or tb is None or (ta <= 900) == (tb <= 900):
                    continue
                alpha = max(.1, min(.9, (900 - ta) / (tb - ta)))
                point = (a[0] + alpha * (b[0] - a[0]), a[1] + alpha * (b[1] - a[1]))
                if all(math.dist(point, existing) >= spacing - 1e-8 for existing in self.samples):
                    candidates.append(point)
        return sorted(set(candidates))

    def triangles(self):
        result = []
        for cell in sorted(self.leaves):
            for edge in self.edges(cell):
                for a, b in zip(edge, edge[1:]):
                    vertices = [cell.center, a, b]
                    observations = [self.samples.get(p) for p in vertices]
                    values = [o.duration if o else None for o in observations]
                    result.append((Polygon(vertices), vertices, values))
        return result
