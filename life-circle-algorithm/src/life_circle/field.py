from dataclasses import dataclass

import contourpy
import numpy as np
from shapely import union_all
from shapely.geometry import MultiPolygon, Polygon, box, mapping
from shapely.ops import transform


class GeometryError(ValueError):
    pass


def multipolygon(geometry):
    if geometry.is_empty:
        return MultiPolygon()
    if geometry.geom_type == "Polygon":
        return MultiPolygon([geometry])
    if geometry.geom_type == "MultiPolygon":
        return geometry
    if geometry.geom_type == "GeometryCollection":
        return MultiPolygon([p for g in geometry.geoms for p in multipolygon(g).geoms])
    return MultiPolygon()


def contour_field(x, y, z, support, threshold=900):
    generator = contourpy.contour_generator(
        x=x, y=y, z=np.ma.masked_invalid(z), name="serial", corner_mask=False,
        quad_as_tri=True, fill_type="OuterOffset", z_interp="Linear", chunk_count=(1, 1),
    )
    points, offsets = generator.filled(-1, threshold)
    polygons = []
    for coordinates, boundaries in zip(points, offsets):
        rings = [coordinates[a:b] for a, b in zip(boundaries, boundaries[1:])]
        # Exact-threshold isolated points are not polygonal area. Do not repair
        # non-degenerate invalid rings (including zero-signed-area bow ties).
        unique = np.unique(rings[0], axis=0)
        if len(unique) < 3 or np.linalg.matrix_rank(unique - unique[0]) < 2:
            continue
        polygon = Polygon(rings[0], rings[1:])
        if not polygon.is_valid:
            raise GeometryError("等值线生成无效多边形")
        if not polygon.is_empty and polygon.area > 0:
            polygons.append(polygon)
    geometry = multipolygon(union_all(polygons).intersection(support))
    if not geometry.is_valid:
        raise GeometryError("裁剪后的可达几何无效")
    return geometry


@dataclass
class FieldResult:
    geometry: object
    unknown: object
    support: object
    x: np.ndarray
    z: np.ndarray


def reconstruct(mesh, raster_size):
    extent = mesh.extent
    axis = np.linspace(-extent, extent, int(np.ceil(2 * extent / raster_size)) + 1)
    z = np.full((len(axis), len(axis)), np.nan)
    support = []
    for polygon, vertices, values in mesh.triangles():
        if any(value is None for value in values):
            continue
        support.append(polygon)
        vertices = np.asarray(vertices)
        lo = np.searchsorted(axis, vertices.min(axis=0) - 1e-8)
        hi = np.searchsorted(axis, vertices.max(axis=0) + 1e-8, side="right")
        xx, yy = np.meshgrid(axis[lo[0]:hi[0]], axis[lo[1]:hi[1]])
        a, b, c = vertices
        denominator = (b[1] - c[1]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[1] - c[1])
        u = ((b[1] - c[1]) * (xx - c[0]) + (c[0] - b[0]) * (yy - c[1])) / denominator
        v = ((c[1] - a[1]) * (xx - c[0]) + (a[0] - c[0]) * (yy - c[1])) / denominator
        w = 1 - u - v
        mask = (u >= -1e-9) & (v >= -1e-9) & (w >= -1e-9)
        view = z[lo[1]:hi[1], lo[0]:hi[0]]
        view[mask] = (u * values[0] + v * values[1] + w * values[2])[mask]
    support = union_all(support)
    # corner_mask=False also masks every output cell touching an unknown node.
    # Count that conservative halo as unknown, not as confirmed unreachable.
    invalid = ~np.isfinite(z)
    masked_cells = invalid[:-1, :-1] | invalid[1:, :-1] | invalid[:-1, 1:] | invalid[1:, 1:]
    masked_runs = []
    for row, mask in enumerate(masked_cells):
        changes = np.diff(np.r_[False, mask, False].astype(int))
        for start, end in zip(np.flatnonzero(changes == 1), np.flatnonzero(changes == -1)):
            masked_runs.append(box(axis[start], axis[row], axis[end], axis[row + 1]))
    if masked_runs:
        support = support.difference(union_all(masked_runs))
    unknown = box(-extent, -extent, extent, extent).difference(support)
    geometry = contour_field(axis, axis, z, support)
    return FieldResult(geometry, unknown, support, axis, z)


def business_geometry(geometry, projection):
    def convert(x, y, z=None):
        return (np.asarray(x) / projection.sx + projection.origin[0], np.asarray(y) / projection.sy + projection.origin[1])
    result = dict(mapping(transform(convert, multipolygon(geometry))))
    result["coordinateSystem"] = "bd09ll"
    return result
