"""Synthetic B1 acceptance regressions; never use a real transport or credential."""
import asyncio
from pathlib import Path

import pytest

from app.poi.artifacts import export_artifacts
from app.poi.models import PoiCollectRequest, RuntimeConfig
from app.poi.normalize import classify, merge_entities, normalize
from app.poi.planner import build_plan, parameters
from app.poi.provider import ReplayProvider, whitelist
from app.poi.runtime import PoiRuntime
from app.poi.service import collect_pois


def setup_run(**kwargs):
    request = PoiCollectRequest(coordinateSystem='bd09ll', **kwargs)
    config = RuntimeConfig(runId='acceptance')
    plan = build_plan(request, config)
    return request, config, plan, PoiRuntime(config, plan)


@pytest.mark.parametrize('name,tags', [
    ('合成小学餐馆', ['餐饮;小学主题餐厅']),
    ('合成商店', ['购物;小学用品']),
    ('合成商店', ['购物;药店设备']),
    ('合成药店', ['医疗;药店', '教育;中学']),
    ('合成小学正门', ['教育;小学']),
])
def test_misleading_tags_and_accessories_never_accepted(name, tags):
    assert classify(name, tags)[1] != 'accepted'


@pytest.mark.parametrize('name,tags,status', [
    ('合成小学', ['教育培训;小学'], 'accepted'),
    ('合成小学辅导班', ['教育培训;小学'], 'excluded'),
    ('合成农贸市场-1号门', ['购物;农贸市场'], 'excluded'),
    ('合成小学', ['教育培训;培训机构'], 'excluded'),
])
def test_v3_parent_education_tag_is_not_a_training_business(name, tags, status):
    assert classify(name, tags)[1] == status


def test_uid_name_address_and_parent_conflicts_are_preserved():
    request, _, plan, _ = setup_run()
    base = {'uid': 'synthetic-conflict', 'name': '合成药店甲', 'address': '合成地址甲',
            'location': {'lng': 121.514, 'lat': 31.313},
            'detail_info': {'classified_poi_tag': '医疗;药店', 'parent_id': 'synthetic-parent-a'}}
    other = {**base, 'name': '合成药店乙', 'address': '合成地址乙',
             'detail_info': {**base['detail_info'], 'parent_id': 'synthetic-parent-b'}}
    records = [normalize(row, {'tileId': 'r0c0', 'query': '药店', 'pageNum': i}, 'synthetic')
               for i, row in enumerate([base, other])]
    accepted, review, _, _, _ = merge_entities(records, request, plan)
    assert not accepted and len(review) == 1
    assert {'uid_name_conflict', 'uid_address_conflict', 'uid_parent_conflict'} <= set(review[0]['conflicts'])


def test_subset_request_keeps_other_categories_out_of_production_list():
    request, _, plan, runtime = setup_run(categories=['primary_school'])
    row = {'uid': 'synthetic-pharmacy', 'name': '合成药店',
           'location': {'lng': 121.514, 'lat': 31.313},
           'detail_info': {'classified_poi_tag': '医疗;药店'}}
    fixture = {'source': 'synthetic', 'pages': {'小学:0':
               {'status': 0, 'result_type': 'poi_type', 'total': 1, 'results': [row]}}}
    result = asyncio.run(collect_pois(request, ReplayProvider(fixture), runtime))
    assert not result.pois
    assert result.excluded_candidates[0]['classificationEvidence'] == ['category_not_requested']


def test_wire_coordinates_use_six_decimals_and_preserve_plan_precision():
    _, _, plan, _ = setup_run()
    sequence = plan['sequences'][0]
    lat, lng = parameters(sequence, 0)['location'].split(',')
    assert lat == f"{sequence['center'][1]:.6f}"
    assert lng == f"{sequence['center'][0]:.6f}"


def test_failed_page_is_locatable_separately_from_successful_pages():
    request, _, _, runtime = setup_run(categories=['primary_school'])
    fixture = {'source': 'synthetic', 'pages': {'小学:0':
               {'status': 0, 'result_type': 'poi_type', 'total': 1, 'results': []}}}
    result = asyncio.run(collect_pois(request, ReplayProvider(fixture), runtime))
    meta = result.query_coverage[0]
    assert meta['requestedPages'] == [0] and meta['successfulPages'] == [0]
    assert meta['reportedTotals'] == [{'pageNum': 0, 'total': 1}]
    assert meta['status'] == 'partial'
    failed = asyncio.run(collect_pois(request, ReplayProvider({'source': 'synthetic', 'pages': {}}),
                                    setup_run(categories=['primary_school'])[3]))
    assert failed.query_coverage[0]['pageErrors'] == [{'pageNum': 0, 'reason': 'fixture_missing'}]


def test_nested_transport_fields_do_not_escape_whitelist():
    payload = {'status': 0, 'results': [{'uid': 'synthetic-1', 'name': '合成药店',
        'location': {'lng': 121.514, 'lat': 31.313, 'unrelated': 'private'},
        'address': {'unrelated': 'private'},
        'detail_info': {'parent_id': {'unrelated': 'private'},
                        'classified_poi_tag': ['private'],
                        'navi_location': {'lng': 121.514, 'lat': 31.313, 'unrelated': 'private'}}}]}
    result = whitelist(payload)
    assert 'private' not in str(result)
    assert result['results'][0]['location'] == {'lng': 121.514, 'lat': 31.313}


def test_export_failure_does_not_publish_incomplete_result(tmp_path, monkeypatch):
    request, config, plan, runtime = setup_run()
    result = asyncio.run(collect_pois(request, ReplayProvider.from_path('tests/fixtures/poi/collection.json'), runtime))
    original = Path.write_text
    def fail_csv(path, *args, **kwargs):
        if path.name == 'poi-list.csv':
            raise OSError('synthetic disk failure')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'write_text', fail_csv)
    output = tmp_path / 'published'
    with pytest.raises(OSError):
        export_artifacts(output, config, plan, result, runtime)
    assert not output.exists()
