import json
from types import SimpleNamespace

from tools import prepare_osm_cache


class Frame:
    def __init__(self, **columns):
        self.columns = list(columns)
        self.values = columns

    def __getitem__(self, key):
        return self.values[key]

    def __len__(self):
        return len(next(iter(self.values.values())))


def test_prepare_osm_cache_writes_bd09_graph_and_metadata(tmp_path, monkeypatch):
    pbf = tmp_path / "fixture.osm.pbf"
    pbf.write_bytes(b"fixture")
    nodes = Frame(id=[1, 2], lon=[121.5, 121.501], lat=[31.3, 31.3])
    edges = Frame(u=[1], v=[2], length=[100.0])
    monkeypatch.setattr(prepare_osm_cache, "_load_network", lambda *_: (nodes, edges))
    args = SimpleNamespace(
        pbf=pbf,
        output=tmp_path / "cache",
        city="上海市",
        data_version="fixture-v1",
        data_date="2026-09-14",
        downloaded_at=None,
        prepared_at="2026-09-14T00:00:00+00:00",
        coverage_bbox=None,
        input_crs="wgs84",
        walk_speed_mps=1.3,
        max_snap_distance_m=250.0,
        overwrite=True,
    )

    count_nodes, count_edges, coverage = prepare_osm_cache.prepare(args)
    metadata = json.loads((args.output / "metadata.json").read_text(encoding="utf-8"))
    graph = json.loads((args.output / "graph.json").read_text(encoding="utf-8"))

    assert (count_nodes, count_edges) == (2, 1)
    assert metadata["coordinateSystem"] == "bd09ll"
    assert metadata["coverageCity"] == "上海市"
    assert metadata["coverage"] == coverage
    assert graph["nodes"][0]["lng"] != 121.5
    assert graph["edges"][0]["durationS"] == round(100 / 1.3, 3)
