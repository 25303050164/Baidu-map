"""From backend: python scripts/prepare_osm_graph.py --source URL --downloaded-at DATE."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import load_settings
from app.algorithms.osm_offline.prepare import prepare_graph
from app.algorithms.osm_offline.graph_store import OsmDataError


def main():
    parser = argparse.ArgumentParser(description="Build a local OSM walking graph; never downloads data.")
    parser.add_argument("--source", required=True, help="Provenance URL, recorded only; never requested")
    parser.add_argument("--downloaded-at", required=True, help="Snapshot download date (ISO 8601)")
    parser.add_argument("--no-simplify", action="store_true")
    args = parser.parse_args()
    settings = load_settings()
    try:
        metadata = prepare_graph(settings, source=args.source, downloaded_at=args.downloaded_at,
                                 simplify=not args.no_simplify)
    except OsmDataError as exc:
        parser.exit(1, f"OSM preparation failed: {exc}\n")
    except Exception as exc:
        parser.exit(1, f"OSM preparation failed: graph_preparation_failed ({type(exc).__name__})\n")
    target = settings.osm_graph_cache_path.parent / "graph_metadata.json"
    target.write_text(json.dumps(metadata, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    # Windows terminals may use GBK, which cannot print the attribution symbol.
    print(json.dumps(metadata, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
