"""Orchestration only; never imports a provider or interpolation implementation."""
import logging
import time
from dataclasses import dataclass

from life_circle.models import IsochroneRequest, IsochroneResult, Statistics
from shapely.geometry import Point

from .coverage import boundary_hit, load_coverage
from .edge_intervals import reachable_intervals
from .graph_store import GraphStore, OsmDataError, load_graph_cache
from .polygonize import polygonize
from .routing import cutoff_dijkstra
from .snap import nearest_edge_source

logger = logging.getLogger(__name__)
EMPTY = {"type": "MultiPolygon", "coordinates": [], "coordinateSystem": "bd09ll"}


@dataclass
class OsmComputation:
    result: IsochroneResult
    diagnostics: dict
    # Internal debug/evaluation artifacts, deliberately outside the public schema.
    reachable_network: object = None
    snap_point: object = None

    def payload(self):
        payload = self.result.to_dict()
        # Shared dataclass contains sampling knobs for historical callers. Do not
        # present these unused knobs as part of the offline algorithm's model.
        payload["config"] = {
            "origin": self.result.config.origin, "coordinate_system": "bd09ll", "threshold": 900,
            "config_version": "osm-offline-v1",
            **{key: self.diagnostics[key] for key in ("osm_data_version", "metric_crs", "walking_speed_mps",
                                                     "snap_max_distance_m", "polygon_buffer_m", "coverage_margin_m")},
        }
        for key in ("uncertainRegion", "unknownRegion", "computationExtent"):
            payload[key] = None
        return {**payload, "algorithm": "osm_offline", "diagnostics": self.diagnostics}


class OsmOfflineEngine:
    def __init__(self, settings, store=None, coverage=None, unavailable_reason=None, coverage_reason=None):
        self.settings = settings
        self.store = store
        self.coverage = coverage
        self.unavailable_reason = unavailable_reason
        self.coverage_reason = coverage_reason

    @classmethod
    def load(cls, settings):
        # Only startup calls this. Failure does not take the other algorithm down.
        if settings.osm_data_version == "unconfigured":
            reason = "osm_data_version_unconfigured" if settings.osm_graph_cache_path.is_file() else "graph_cache_missing"
            return cls(settings, unavailable_reason=reason)
        try:
            graph = load_graph_cache(settings.osm_graph_cache_path)
            if graph.graph.get("osm_data_version") != settings.osm_data_version:
                raise OsmDataError("cache_data_version_mismatch")
            store = GraphStore(graph, speed=settings.walk_speed_mps, crs=settings.osm_metric_crs)
        except OsmDataError as exc:
            return cls(settings, unavailable_reason=str(exc))
        except Exception:
            return cls(settings, unavailable_reason="graph_cache_validation_failed")
        coverage, reason = None, None
        if settings.osm_coverage_boundary_path is None:
            reason = "coverage_check_unavailable"
        else:
            try:
                coverage = load_coverage(settings.osm_coverage_boundary_path, store.projection)
                expected = graph.graph.get("coverage_sha256")
                if expected:
                    from .prepare import sha256_file
                    if sha256_file(settings.osm_coverage_boundary_path) != expected:
                        raise OsmDataError("coverage_snapshot_mismatch")
            except Exception:
                coverage, reason = None, "coverage_check_invalid"
        return cls(settings, store, coverage, coverage_reason=reason)

    def compute(self, request: IsochroneRequest):
        start = time.perf_counter()
        config = self.settings
        diagnostics = {
            "algorithm": "osm_offline", "osm_data_version": config.osm_data_version,
            "input_crs": "BD09LL", "routing_crs": "EPSG:4326", "metric_crs": config.osm_metric_crs,
            "output_crs": "BD09LL", "walking_speed_mps": config.walk_speed_mps,
            "polygon_buffer_m": config.isochrone_buffer_m,
            "coverage_margin_m": config.osm_coverage_margin_m,
            "snap_max_distance_m": config.snap_max_distance_m,
            "polygonization_method": "metric_buffer_union_make_valid",
            "data_source": "OpenStreetMap", "attribution": "© OpenStreetMap contributors (ODbL)",
            "network_requests": 0, "coverage_check_available": self.coverage is not None,
            "coverage_boundary_hit": None, "snap_ms": 0.0, "routing_ms": 0.0,
            "edge_interval_ms": 0.0, "polygon_ms": 0.0, "reachable_nodes": 0,
            "reachable_segments": 0, "reachable_full_edges": 0, "reachable_partial_edges": 0,
        }
        warnings = []
        if self.coverage is None:
            warnings.append(self.coverage_reason or "coverage_check_unavailable")
        network, snapped, polygon, public = None, None, None, None
        quality, reason = "insufficient", "graph_cache_missing"
        stage = "graph"
        try:
            if self.store is None:
                raise OsmDataError(self.unavailable_reason or reason)
            store, graph = self.store, self.store.graph
            diagnostics.update(graph_nodes=store.node_count, graph_edges=store.edge_count, **store.diagnostics)
            if store.diagnostics["geometry_fallback_edges"]:
                warnings.append("edge_geometry_fallback")
            stage = "coordinate_conversion"
            origin = store.projection.origin(request.origin)
            if self.coverage is not None and not self.coverage.covers(Point(origin)):
                raise OsmDataError("origin_outside_coverage")
            stage = "snap"
            tick = time.perf_counter()
            snapped = nearest_edge_source(store, origin, speed=config.walk_speed_mps,
                                          max_distance=config.snap_max_distance_m, threshold=request.threshold)
            diagnostics.update(snap_ms=(time.perf_counter()-tick)*1000, snap_edge=list(snapped.edge),
                               snap_distance_m=snapped.distance_m, snap_time_s=snapped.time_s,
                               network_budget_s=snapped.budget_s)
            component_size = store.component_sizes[store.component[snapped.edge[0]]]
            diagnostics["component_nodes"] = component_size
            if component_size < 3:
                warnings.append("small_component")
            stage = "routing"
            tick = time.perf_counter()
            distances = cutoff_dijkstra(graph, snapped.seeds, snapped.budget_s)
            diagnostics.update(routing_ms=(time.perf_counter()-tick)*1000, reachable_nodes=len(distances))
            # No settled endpoints can still mean a valid slice of a long edge.
            diagnostics["endpoint_expansion"] = bool(distances)
            stage = "edge_intervals"
            tick = time.perf_counter()
            network = reachable_intervals(graph, distances, snapped.budget_s, snapped.source_intervals)
            diagnostics.update(edge_interval_ms=(time.perf_counter()-tick)*1000,
                               reachable_segments=len(network.segments), reachable_full_edges=network.full_edges,
                               reachable_partial_edges=network.partial_edges, reachable_length_m=network.geometry.length)
            if self.coverage is not None:
                hit = boundary_hit(network.geometry, self.coverage, config.osm_coverage_margin_m)
                diagnostics["coverage_boundary_hit"] = hit
                if hit:
                    warnings.append("graph_coverage_boundary")
            stage = "polygonization"
            tick = time.perf_counter()
            polygon = polygonize(network.geometry, config.isochrone_buffer_m)
            diagnostics["polygon_ms"] = (time.perf_counter()-tick)*1000
            stage = "output_coordinate_conversion"
            public = store.projection.public_geometry(polygon)
            quality = "partial" if warnings else "usable"
            reason = "graph_coverage_boundary" if "graph_coverage_boundary" in warnings else "network_budget"
        except OsmDataError as exc:
            reason = str(exc)
            diagnostics.update(exc.diagnostics)
            warnings.append(reason)
        except Exception:
            # No raw filesystem paths or third-party exceptions in the wire envelope.
            reason = f"{stage}_failed"
            warnings.append(reason)
        diagnostics.update(total_ms=(time.perf_counter()-start)*1000, reason=reason)
        stats = Statistics(total_seconds=diagnostics["total_ms"]/1000, compute_seconds=diagnostics["total_ms"]/1000)
        result = IsochroneResult(public, EMPTY.copy(), EMPTY.copy(), EMPTY.copy(), quality, reason,
                                 stats, sorted(set(warnings)), request, local_geometry=polygon)
        logger.info("osm_offline %s", diagnostics)
        return OsmComputation(result, diagnostics, network.geometry if network else None,
                              snapped.point if snapped else None)
