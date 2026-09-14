"""Dataset coverage, never inferred from graph dead ends or administrative names."""
import json
from pathlib import Path

from shapely import union_all
from shapely.geometry import Polygon, shape
from shapely.ops import transform

from .graph_store import OsmDataError


def read_coverage(path):
    path = Path(path)
    if path.suffix == ".poly":
        # Osmosis/Geofabrik polygon format, including multiple shells and holes.
        lines = iter(path.read_text(encoding="utf-8").strip().splitlines()[1:])
        shells, holes = [], []
        for label in lines:
            label = label.strip()
            if label == "END":
                break
            ring = []
            for line in lines:
                if line.strip() == "END":
                    break
                ring.append(tuple(map(float, line.split())))
            (holes if label.startswith("!") else shells).append(Polygon(ring))
        geometry = union_all(shells).difference(union_all(holes))
    else:
        data = json.loads(path.read_text(encoding="utf-8"))
        if "crs" in data:
            raise OsmDataError("coverage_requires_wgs84_geojson")
        if data["type"] == "FeatureCollection":
            geometry = union_all([shape(f["geometry"]) for f in data["features"]])
        else:
            geometry = shape(data["geometry"] if data["type"] == "Feature" else data)
    if geometry.is_empty or not geometry.is_valid or geometry.geom_type not in ("Polygon", "MultiPolygon"):
        raise OsmDataError("coverage_invalid")
    if not (-180 <= geometry.bounds[0] <= geometry.bounds[2] <= 180 and -90 <= geometry.bounds[1] <= geometry.bounds[3] <= 90):
        raise OsmDataError("coverage_requires_wgs84")
    return geometry


def load_coverage(path, projection):
    return transform(lambda x, y, z=None: projection.forward.transform(x, y, errcheck=True), read_coverage(path))


def boundary_hit(network, coverage, margin):
    return not coverage.covers(network) or network.distance(coverage.boundary) <= margin
