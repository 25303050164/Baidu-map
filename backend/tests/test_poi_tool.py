"""CLI gates and offline execution are tested without network access."""
import json

import pytest

from app.poi.models import CollectionConfig, PoiCollectRequest, RuntimeConfig
from tools.poi_collect import main


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
