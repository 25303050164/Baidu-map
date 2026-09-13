import asyncio
import json

import pytest

from app.poi.artifacts import csv_text, export_artifacts
from app.poi.models import PoiCollectRequest, RuntimeConfig
from app.poi.planner import build_plan
from app.poi.provider import ReplayProvider
from app.poi.runtime import PoiRuntime
from app.poi.service import collect_pois
from app.request_control import RequestStopped


FIXTURE = 'tests/fixtures/poi/collection.json'


def run_result():
    request = PoiCollectRequest(coordinateSystem='bd09ll')
    config = RuntimeConfig(runId='artifact-test')
    plan = build_plan(request, config)
    runtime = PoiRuntime(config, plan)
    result = asyncio.run(collect_pois(request, ReplayProvider.from_path(FIXTURE), runtime))
    return config, plan, result, runtime


def test_exports_are_reproducible_and_escape_csv_formulas(tmp_path):
    config, plan, result, runtime = run_result()
    value = {'id': 'x', 'name': '=1+1', 'category': 'market', 'classificationStatus': 'accepted',
             'location': {'lng': 121, 'lat': 31}}
    assert "'=1+1" in csv_text([value])
    output = tmp_path / 'run'
    export_artifacts(output, config, plan, result, runtime)
    expected = {'config-sanitized.json', 'query-plan.json', 'poi-result.json', 'poi-list.csv',
                'review.csv', 'query-coverage.json', 'metrics.json', 'ledger-final.json',
                'security-check.json', '验收报告.md'}
    assert {p.name for p in output.iterdir()} == expected
    assert json.loads((output / 'security-check.json').read_text(encoding='utf-8'))['passed'] is True
    assert 'synthetic' in (output / '验收报告.md').read_text(encoding='utf-8')
    snapshot = {p.name: p.read_bytes() for p in output.iterdir()}
    with pytest.raises(RequestStopped, match='output_already_exists'):
        export_artifacts(output, config, plan, result, runtime)
    assert snapshot == {p.name: p.read_bytes() for p in output.iterdir()}


def test_secret_scan_happens_before_output_creation(tmp_path):
    config, plan, result, runtime = run_result()
    output = tmp_path / 'secret'
    # The secret is not allowed to occur in any artifact, including human-readable reports.
    result.warnings.append('secret=fixture-secret')
    with pytest.raises(RequestStopped, match='sensitive_output'):
        export_artifacts(output, config, plan, result, runtime, secret='fixture-secret')
    assert not output.exists()
