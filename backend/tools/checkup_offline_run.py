"""One full checkup against the real walking graph, with synthetic Baidu stand-ins.

Boundary (E8.2.1 over an analytic walking-time field), place search and routes are
synthetic; the accessibility stage runs on the configured OSM graph, obstacles and
water reviews. Nothing leaves the machine: external connections are refused.

    python -m tools.checkup_offline_run --output OUT [--budget 800]

OSM paths come from the environment (OSM_PBF_PATH, OSM_GRAPH_CACHE_PATH,
OSM_DATA_VERSION, OSM_COVERAGE_BOUNDARY_PATH, HYBRID_OBSTACLE_PATH, ...).
"""
import argparse
import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

from pydantic import SecretStr

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / 'tests'))

import conftest  # noqa: E402,F401  -- installs the external-connection guard first

from fastapi.testclient import TestClient  # noqa: E402
from life_circle.coordinates import LocalProjection  # noqa: E402

from app.config import load_settings  # noqa: E402
from test_checkup_facilities import (SyntheticPlaces, SyntheticRoutes, every_page,  # noqa: E402
                                     straight_routes)
from test_checkup_v2 import analytic  # noqa: E402

CENTER = {'lng': 121.513925, 'lat': 31.313079}
#: Synthetic facilities around the centre (local metres): some inside the circle,
#: some beyond it, so nearby service sources are exercised too.
FACILITY_OFFSETS = ((-900, 300), (400, -600), (1300, 200), (-200, -1200), (700, 900), (-1400, -100))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--budget', type=int, default=800)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    settings = load_settings().model_copy(update=dict(
        baidu_map_ak=SecretStr('offline-run-no-network'), analysis_provider='synthetic',
        baidu_place_qps=10000, baidu_direction_qps=10000, analysis_qps=None,
        checkup_dir=args.output / 'checkups', quota_ledger_path=args.output / 'quota.sqlite3',
        hybrid_ledger_dir=args.output / 'ledgers'))
    with patch('app.config.load_settings', return_value=settings):
        from app.main import create_app
    projection = LocalProjection((CENTER['lng'], CENTER['lat']))
    points = [projection.to_geographic(xy) for xy in FACILITY_OFFSETS]
    app = create_app(settings, provider_factory=analytic,
                     place_factory=lambda _settings: SyntheticPlaces(every_page(points)),
                     route_factory=lambda _settings: SyntheticRoutes(straight_routes()))
    started = time.monotonic()
    timeline = []
    with TestClient(app) as client:
        body = {'schemaVersion': 'checkup-v1', 'clientRequestId': 'offline-real-graph',
                'engine': 'baidu_e82', 'center': CENTER, 'coordinateSystem': 'bd09ll',
                'isochrone': {'budget': args.budget}}
        task = client.post('/api/v2/checkups', json=body).json()
        last = None
        while True:
            view = client.get(f"/api/v2/checkups/{task['taskId']}").json()
            stage = (view['status'], view.get('stage'))
            if stage != last:
                timeline.append(dict(at=round(time.monotonic() - started, 1), status=view['status'],
                                     stage=view.get('stage')))
                print(json.dumps(timeline[-1], ensure_ascii=False), flush=True)
                last = stage
            if view['status'] in ('completed', 'failed', 'cancelled', 'interrupted'):
                break
            time.sleep(1)
        result = client.get(f"/api/v2/checkups/{task['taskId']}/result").json()
    (args.output / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding='utf-8')
    (args.output / 'timeline.json').write_text(json.dumps(dict(view=view, timeline=timeline,
                                                               external=conftest.EXTERNAL), indent=1),
                                               encoding='utf-8')
    accessibility = result.get('accessibility') or {}
    print(json.dumps(dict(status=view['status'], error=view.get('error'),
                          accessibility=accessibility.get('status'),
                          categories=[(c['category'], c['supported'], round(c['coveredM2']),
                                       round(c['gapM2']), round(c['unknownM2']))
                                      for c in accessibility.get('categories') or []],
                          verification=(result.get('verification') or {}).get('spotCheckSummary'),
                          external_attempts=conftest.EXTERNAL['attempts']), ensure_ascii=False))


if __name__ == '__main__':
    main()
