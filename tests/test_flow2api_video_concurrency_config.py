"""Flow2API concurrency has one server source and a separate browser limit."""

import json

import pytest

import server_common as sc
from fx_console import FX_CONFIG_SPEC, FxConfigStore, validate_patch


@pytest.mark.parametrize('managed', [True, False])
@pytest.mark.parametrize('count', [1, 3, 10])
def test_generation_requests_cannot_override_saved_concurrency(monkeypatch, managed, count):
    monkeypatch.setattr(sc, 'SERVER_MANAGED', managed)
    monkeypatch.setattr(sc, 'SERVER_CONFIG', {
        'videoProvider': 'flow2api', 'flow2apiVideoConcurrency': count,
        'fxMaxConcurrent': 1,
    })
    effective = sc.effective_config({'flow2apiVideoConcurrency': 99})
    assert effective['flow2apiVideoConcurrency'] == count
    assert sc.video_service_config_report()['flow2apiVideoConcurrency'] == count


@pytest.mark.parametrize('managed', [True, False])
def test_missing_server_setting_uses_default_despite_stale_client(monkeypatch, managed):
    monkeypatch.setattr(sc, 'SERVER_MANAGED', managed)
    monkeypatch.setattr(sc, 'SERVER_CONFIG', {})
    effective = sc.effective_config({'flow2apiVideoConcurrency': 1})
    assert effective['flow2apiVideoConcurrency'] == 3
    assert sc.video_service_config_report()['flow2apiVideoConcurrency'] == 3


@pytest.mark.parametrize(('raw', 'expected'), [
    ('7', 7), (0, 1), (-2, 1), (15, 10),
    (None, 3), ('invalid', 3), (True, 3), (2.5, 3),
])
def test_file_and_environment_values_are_normalized_consistently(monkeypatch, raw, expected):
    monkeypatch.setattr(sc, 'SERVER_CONFIG', {'flow2apiVideoConcurrency': raw})
    assert sc.effective_config({})['flow2apiVideoConcurrency'] == expected
    assert sc.video_service_config_report()['flow2apiVideoConcurrency'] == expected
    assert FxConfigStore(sc.SERVER_CONFIG, '/unused/config.json').current()['flow2apiVideoConcurrency'] == expected


def test_concurrency_saves_and_reloads_without_changing_browser_limit(tmp_path, monkeypatch):
    monkeypatch.setenv('SPARK_FLOW2API_VIDEO_CONCURRENCY', '3')
    config = {'fxMaxConcurrent': 1, 'flow2apiApiKey': 'private-key'}
    path = tmp_path / 'config.json'
    store = FxConfigStore(config, path)
    response = store.save({'flow2apiVideoConcurrency': 10})
    disk = json.loads(path.read_text())
    assert disk['flow2apiVideoConcurrency'] == 10
    assert disk['fxMaxConcurrent'] == 1
    assert disk['flow2apiApiKey'] == 'private-key'
    assert response['changed'] == {'flow2apiVideoConcurrency': 10}
    assert response['config']['flow2apiVideoConcurrency'] == 10
    assert 'private-key' not in json.dumps(response)
    assert FxConfigStore(disk, path).current()['flow2apiVideoConcurrency'] == 10
    spec = store.schema()['flow2apiVideoConcurrency']
    assert (spec['min'], spec['max'], spec['default'], spec['hot']) == (1, 10, 3, True)
    assert FX_CONFIG_SPEC['fxMaxConcurrent']['max'] == 4


@pytest.mark.parametrize('value', [1, 3, 10, '5'])
def test_config_patch_accepts_valid_concurrency(value):
    assert validate_patch({'flow2apiVideoConcurrency': value}) == {'flow2apiVideoConcurrency': int(value)}


@pytest.mark.parametrize('value', [0, 11, -1, 'invalid', True, 2.5])
def test_config_patch_rejects_invalid_concurrency(value):
    with pytest.raises(ValueError):
        validate_patch({'flow2apiVideoConcurrency': value})


def test_environment_concurrency_is_used_by_effective_config(monkeypatch, tmp_path):
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({'flow2apiVideoConcurrency': 2}))
    monkeypatch.setattr(sc, 'SERVER_CONFIG_FILE', str(path))
    monkeypatch.setenv('SPARK_FLOW2API_VIDEO_CONCURRENCY', '7')
    loaded = sc._load_server_config()
    monkeypatch.setattr(sc, 'SERVER_CONFIG', loaded)
    assert sc.effective_config({'flow2apiVideoConcurrency': 1})['flow2apiVideoConcurrency'] == 7
