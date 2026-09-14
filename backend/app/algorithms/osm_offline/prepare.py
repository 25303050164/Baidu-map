"""Deployment-only PBF construction. No imports of this module on routing paths."""
import hashlib
import math
import time
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path

import networkx as nx
from shapely.geometry import LineString

from .graph_store import GraphStore, OsmDataError, PEDESTRIAN_ATTRS, edge_travel_time, save_graph_cache


def sha256_file(path):
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def tag(value):
    if value is None or isinstance(value, float) and math.isnan(value):
        return None
    return str(value)


def simplify_walking_graph(graph):
    import osmnx as ox
    # OSMnx constructs merged geometry from node coordinates. Preserve endpoints
    # of already-curved segments so their intermediate vertices cannot be lost.
    graph = graph.copy()
    for u, v, data in graph.edges(data=True):
        if len(data["geometry"].coords) > 2:
            graph.nodes[u]["preserve_geometry"] = True
            graph.nodes[v]["preserve_geometry"] = True
    before_components = nx.number_weakly_connected_components(graph)
    length_before = sum(d["length"] for _, _, d in graph.edges(data=True))
    simplified = ox.simplification.simplify_graph(
        graph, edge_attrs_differ=["osmid", *PEDESTRIAN_ATTRS],
        node_attrs_include=["preserve_geometry", "barrier"], remove_rings=False,
        edge_attr_aggs={"length": sum, "travel_time_s": sum},
    )
    if nx.number_weakly_connected_components(simplified) != before_components:
        raise OsmDataError("simplification_lost_component")
    if not math.isclose(sum(d["length"] for _, _, d in simplified.edges(data=True)), length_before, rel_tol=1e-8):
        raise OsmDataError("simplification_lost_length")
    for _, _, data in simplified.edges(data=True):
        if any(isinstance(data[attr], list) for attr in PEDESTRIAN_ATTRS):
            raise OsmDataError("simplification_merged_pedestrian_attributes")
    return simplified


def graph_from_frames(nodes, edges, settings, *, simplify=True):
    # Pyrosm supplies the established walking filter; we only encode its selected
    # roads as a MultiDiGraph and honour explicit pedestrian direction tags.
    nodes = nodes.to_crs(settings.osm_metric_crs)
    edges = edges.to_crs(settings.osm_metric_crs)
    graph = nx.MultiDiGraph(crs=settings.osm_metric_crs)
    for record in nodes.to_dict("records"):
        node = int(record["id"])
        point = record["geometry"]
        graph.add_node(node, x=float(point.x), y=float(point.y))
        if tag(record.get("barrier")) is not None:
            graph.nodes[node]["barrier"] = tag(record["barrier"])
    zero_length = 0
    for record in edges.to_dict("records"):
        u, v = int(record["u"]), int(record["v"])
        line = record["geometry"]
        if not isinstance(line, LineString) or not line.is_valid:
            raise OsmDataError("pbf_invalid_edge_geometry")
        if line.length == 0:
            # A tiny invented cost would falsify time. Collect the count and fail
            # the whole preparation below instead of dropping connectivity.
            zero_length += 1
            continue
        data = {attr: tag(record.get(attr)) for attr in PEDESTRIAN_ATTRS}
        data.update(osmid=int(record["id"]), geometry=line, length=float(line.length))
        data["travel_time_s"] = edge_travel_time(data, settings.walk_speed_mps)
        # Coordinate order from Pyrosm ways follows u -> v before graph export.
        direction = data["oneway:foot"]
        forward = direction not in ("-1", "reverse") and tag(record.get("foot:forward")) != "no"
        backward = direction not in ("yes", "1", "true") and tag(record.get("foot:backward")) != "no"
        if forward:
            graph.add_edge(u, v, **data)
        if backward:
            graph.add_edge(v, u, **{**data, "geometry": LineString(list(line.coords)[::-1])})
    if zero_length:
        # Explicitly fail preparation, not silently remove a potential connector.
        raise OsmDataError("pbf_zero_length_edges")
    graph.graph.update(raw_nodes=graph.number_of_nodes(), raw_edges=graph.number_of_edges())
    if simplify:
        graph = simplify_walking_graph(graph)
    return graph


def prepare_graph(settings, *, source, downloaded_at, simplify=True):
    started = time.perf_counter()
    if not settings.osm_pbf_path.is_file():
        raise OsmDataError("pbf_file_missing")
    if settings.osm_data_version == "unconfigured":
        raise OsmDataError("osm_data_version_required")
    try:
        from pyrosm import OSM
    except ImportError:
        raise OsmDataError("pyrosm_build_dependency_missing") from None
    osm = OSM(str(settings.osm_pbf_path))
    nodes, edges = osm.get_network(network_type="walking", nodes=True,
                                  extra_attributes=[*PEDESTRIAN_ATTRS, "foot:forward", "foot:backward"])
    if nodes is None or edges is None or len(edges) == 0:
        raise OsmDataError("pbf_walking_network_missing")
    graph = graph_from_frames(nodes, edges, settings, simplify=simplify)
    # The PBF reader and GeoDataFrames can hold several city-sized arrays. They
    # are no longer needed for validation/serialization; release them promptly.
    del osm, nodes, edges
    metadata = {
        **graph.graph, "osm_data_version": settings.osm_data_version,
        "pbf_filename": settings.osm_pbf_path.name, "pbf_sha256": sha256_file(settings.osm_pbf_path),
        "source": source, "downloaded_at": downloaded_at,
        "built_at": datetime.now(timezone.utc).isoformat(), "walking_speed_mps": settings.walk_speed_mps,
        "simplification": {"enabled": simplify, "edge_attrs_differ": ["osmid", *PEDESTRIAN_ATTRS], "remove_rings": False},
        "retain_all": True, "node_count": graph.number_of_nodes(), "edge_count": graph.number_of_edges(),
        "cache_path": str(settings.osm_graph_cache_path), "build_seconds": time.perf_counter()-started,
        "versions": {name: version(name) for name in ("pyrosm", "networkx", "osmnx", "pyproj", "shapely")},
        "attribution": "Map data © OpenStreetMap contributors; Open Database License (ODbL)",
    }
    if settings.osm_coverage_boundary_path:
        from .coverage import read_coverage
        read_coverage(settings.osm_coverage_boundary_path)
        metadata["coverage_sha256"] = sha256_file(settings.osm_coverage_boundary_path)
    graph.graph.update(metadata)
    # Verify every edge, including simplification length/geometry consistency.
    normalized = GraphStore(graph, speed=settings.walk_speed_mps, crs=settings.osm_metric_crs)
    save_graph_cache(normalized.graph, settings.osm_graph_cache_path)
    return metadata
