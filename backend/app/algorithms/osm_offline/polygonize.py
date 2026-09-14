from shapely import make_valid, union_all

from .graph_store import OsmDataError


def polygonize(network, buffer_m):
    if network.is_empty:
        raise OsmDataError("routing_no_geometry")
    # GraphStore enforces projected metre units before any geometry is routed.
    result = make_valid(network.buffer(buffer_m))
    if result.geom_type == "GeometryCollection":
        result = union_all([part for part in result.geoms if part.geom_type in ("Polygon", "MultiPolygon")])
    if result.is_empty or result.geom_type not in ("Polygon", "MultiPolygon") or not result.is_valid:
        raise OsmDataError("polygonization_failed")
    return result
