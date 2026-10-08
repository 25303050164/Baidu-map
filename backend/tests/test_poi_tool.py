"""CLI gates and offline execution are tested without network access."""
import json
from pathlib import Path

import pytest

from app.poi.models import CollectionConfig, PoiCollectRequest, RuntimeConfig
from app.poi.planner import build_plan
from tools.poi_collect import main


def test_the_shipped_example_config_is_valid_and_fully_replayable():
    """示例配置与回放夹具都要跟着词典走，否则离线 CLI 会以一个笼统的错误码失败。

    两处都曾在词典扩容后失效：示例只列了三个类的预算，而 ``RuntimeConfig`` 要求逐类
    给全（于是校验直接拒绝，``main`` 只能回 ``configuration_or_execution_failed``）；
    回放夹具缺了新增的市场关键词页面（那一页回 ``fixture_missing``，整轮降级成
    ``partial``，退出码 2）。用例把这两件事分开报，失败时说得出是哪一条、缺哪一页。
    """
    config = CollectionConfig.model_validate_json(
        Path('tools/poi-example.json').read_text(encoding='utf-8-sig'))
    plan = build_plan(config.request, config.runtime)
    fixture = json.loads(Path('tests/fixtures/poi/collection.json').read_text(encoding='utf-8'))
    missing = sorted({f"{sequence['query']}:0" for sequence in plan['sequences']}
                     - set(fixture['pages']))
    assert missing == [], f'回放夹具缺这些首轮页面：{missing}'


@pytest.mark.parametrize('mode', ['plan', 'replay'])
def test_offline_cli_never_reads_credentials_or_sends_http(tmp_path, monkeypatch, capsys, mode):
    import app.config
    import httpx
    def forbidden(*args, **kwargs):
        pytest.fail('offline mode accessed credentials or HTTP transport')
    monkeypatch.setattr(app.config, 'load_settings', forbidden)
    monkeypatch.setattr(httpx.AsyncClient, 'get', forbidden)
    output = tmp_path / mode
    args = ['--mode', mode, '--config', 'tools/poi-example.json', '--output', str(output)]
    if mode == 'replay':
        args += ['--fixtures', 'tests/fixtures/poi/collection.json']
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)['networkReservations'] == 0
    assert main(args) == 1
    assert json.loads(capsys.readouterr().out)['error'] == 'output_already_exists'


@pytest.mark.parametrize('mode,phase', [('live-smoke', 'smoke'), ('live-collect', 'collect')])
def test_live_cli_requires_authorization_before_credentials_or_ledger(tmp_path, monkeypatch, capsys, mode, phase):
    import app.config
    def forbidden():
        pytest.fail('authorization gate was bypassed')
    monkeypatch.setattr(app.config, 'load_settings', forbidden)
    config = CollectionConfig(request=PoiCollectRequest(coordinateSystem='bd09ll'),
                              runtime=RuntimeConfig(runId='not-authorized', phase=phase))
    path = tmp_path / 'config.json'
    path.write_text(config.model_dump_json(by_alias=True), encoding='utf-8')
    output = tmp_path / 'out'
    assert main(['--mode', mode, '--config', str(path), '--output', str(output)]) == 1
    assert json.loads(capsys.readouterr().out)['error'] == 'live_not_authorized'
    assert not output.exists()
