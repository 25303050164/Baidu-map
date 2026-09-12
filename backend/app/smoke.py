import argparse
import json
from pathlib import Path

from .baidu import new_record, verify_baidu
from .config import BACKEND_DIR, load_settings


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one real Baidu request; never pass AK as an argument.")
    parser.add_argument("--record", type=Path, default=BACKEND_DIR / "logs" / "baidu-smoke.jsonl")
    args = parser.parse_args()
    try:
        settings = load_settings()
    except RuntimeError:
        record = new_record()
        record["outcome"] = "config_error"
    else:
        record = verify_baidu(settings)
    line = json.dumps(record, ensure_ascii=True)
    print(line)
    try:
        args.record.parent.mkdir(parents=True, exist_ok=True)
        with args.record.open("a", encoding="utf-8") as output:
            output.write(line + "\n")
    except OSError:
        print("Could not save audit record; check output directory permissions.")
        return 2
    return 0 if record["outcome"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
