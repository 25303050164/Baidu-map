from pyproj import CRS, Transformer
from shapely.geometry import mapping
from shapely.ops import transform

from .coordinates import bd09_to_wgs84, wgs84_to_bd09


class MetricProjection:
    def __init__(self, crs):
        self.crs = CRS.from_user_input(crs)
        if not self.crs.is_projected or any(a.unit_conversion_factor != 1 for a in self.crs.axis_info[:2]):
            raise ValueError("metric_crs_must_use_metres")
        self.forward = Transformer.from_crs(4326, self.crs, always_xy=True)
        self.inverse = Transformer.from_crs(self.crs, 4326, always_xy=True)

    def origin(self, bd09):
        return self.forward.transform(*bd09_to_wgs84(*bd09), errcheck=True)

    def public_geometry(self, geometry):
        # Shapely falls back to scalar callbacks for this scalar converter.
        def convert(x, y, z=None):
            return wgs84_to_bd09(*self.inverse.transform(x, y, errcheck=True))
        result = transform(convert, geometry)
        if not result.is_valid:
            raise ValueError("public_geometry_invalid")
        return {**mapping(result), "coordinateSystem": "bd09ll"}
