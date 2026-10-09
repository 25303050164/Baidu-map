import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import dev


def test_launcher_node_range_matches_frontend_package_and_lock():
    package = json.loads((ROOT / "life-circle-demo" / "package.json").read_text(encoding="utf-8"))
    lock = json.loads((ROOT / "life-circle-demo" / "package-lock.json").read_text(encoding="utf-8"))

    assert package["engines"]["node"] == dev.NODE_ENGINE_RANGE
    assert lock["packages"][""]["engines"]["node"] == dev.NODE_ENGINE_RANGE


def test_launcher_accepts_only_vite_supported_node_ranges():
    cases = {
        "v20.18.1": False,
        "v20.19.0": True,
        "v20.19.9": True,
        "v21.7.0": False,
        "v22.11.0": False,
        "v22.12.0": True,
        "v24.15.0": True,
    }

    for raw, expected in cases.items():
        version = dev.parse_node_version(raw)
        assert version is not None
        assert dev.node_version_supported(version) is expected


def test_launcher_rejects_unparseable_node_version():
    assert dev.parse_node_version("node 24") is None
