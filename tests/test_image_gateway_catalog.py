"""Image registry availability is read offline in tests and cached per gateway credential."""
import io
import json
import urllib.error
from unittest.mock import Mock

import pytest

import server_common
from server_common import get_image_gateway_model_catalog


@pytest.fixture
def catalog_environment(monkeypatch, tmp_path):
    monkeypatch.setattr(server_common, 'SERVER_CONFIG', {})
    monkeypatch.setattr(server_common, '_IMAGE_GATEWAY_MODEL_CATALOG_CACHE', {})
    monkeypatch.setattr(server_common, '_COCKPIT_IMAGE_SIDECAR_DIR', tmp_path / 'sidecar')
    monkeypatch.setattr(server_common, 'effective_config', lambda config: dict(config or {}))
    monkeypatch.setattr(server_common, 'resolve_gateway',
                        lambda model, config: (config.get('codexBaseUrl', 'http://image.test/v1'),
                                               config.get('codexApiKey', 'synthetic-key')))
    clock = [100.0]
    monkeypatch.setattr(server_common.time, 'monotonic', lambda: clock[0])
    opener = Mock()
    monkeypatch.setattr(server_common.urllib.request, 'build_opener', lambda *args: opener)
    return clock, opener


def _registry(opener, data):
    opener.open.side_effect = lambda *args, **kwargs: io.BytesIO(json.dumps(data).encode())


@pytest.fixture
def cockpit_sidecar(catalog_environment):
    directory = server_common._COCKPIT_IMAGE_SIDECAR_DIR
    directory.mkdir()
    (directory / 'config.json').write_text(json.dumps({
        'host': '127.0.0.1', 'port': 52692, 'api-keys': ['sidecar-synthetic-key'],
    }))
    (directory / 'manifest.json').write_text(json.dumps({
        'imageGenerationModel': 'gpt-image-2.5-sunburst',
    }))
    return directory, {'codexBaseUrl': 'http://127.0.0.1:52692/v1',
                       'codexApiKey': 'sidecar-synthetic-key'}


def test_known_registry_publishes_only_registered_gpt_2_5_ids(catalog_environment):
    _, opener = catalog_environment
    _registry(opener, {'data': [
        {'id': 'gpt-image-2'}, {'id': 'gpt-image-2-2026-04-21'},
        {'id': 'gpt-image-2.5'}, {'id': 'gpt-image-2.5-flare'},
        {'id': 'gpt-image-2.5-sunburst-2026-09-30'}, {'id': 'gpt-image-2.5'},
        {'id': 'gemini-3.1-flash-image'}, {'id': 'gpt-6.1-sol'},
    ]})
    result = get_image_gateway_model_catalog()
    assert result['status'] == 'known'
    assert result['models'] == ['gpt-image-2.5', 'gpt-image-2.5-flare',
                                'gpt-image-2.5-sunburst-2026-09-30']
    assert 'gpt-image-2.5-sunburst' not in result['models']
    request = opener.open.call_args.args[0]
    assert request.get_method() == 'GET'
    assert request.full_url == 'http://image.test/v1/models'
    assert request.get_header('Authorization') == 'Bearer synthetic-key'
    assert opener.open.call_args.kwargs['timeout'] == 3


@pytest.mark.parametrize('rows', ([], [{'id': 'gpt-6.1-sol'}], [{'id': 'gpt-image-2'}]))
def test_empty_image_registry_is_known_not_unknown(catalog_environment, rows):
    _, opener = catalog_environment
    _registry(opener, {'data': rows})
    result = get_image_gateway_model_catalog()
    assert result['status'] == 'known'
    assert result['models'] == []


@pytest.mark.parametrize('registry', ({}, [], {'data': None}, {'data': {}},
                                     {'data': ['gpt-image-2.5']}, {'data': [{}]},
                                     {'data': [{'id': None}]}, {'data': [{'id': ' '}]},
                                     {'data': [{'id': 'gpt-image-2.5'}, {'id': 2}]}))
def test_invalid_registry_remains_unknown(catalog_environment, registry):
    _, opener = catalog_environment
    _registry(opener, registry)
    result = get_image_gateway_model_catalog()
    assert result['status'] == 'unknown'
    assert result['models'] == []


def test_non_json_response_remains_unknown(catalog_environment):
    _, opener = catalog_environment
    opener.open.side_effect = lambda *args, **kwargs: io.BytesIO(b'gateway warming up')
    assert get_image_gateway_model_catalog()['status'] == 'unknown'


def test_success_cache_expires_after_sixty_seconds_and_cannot_be_mutated(catalog_environment):
    clock, opener = catalog_environment
    _registry(opener, {'data': [{'id': 'gpt-image-2.5'}]})
    first = get_image_gateway_model_catalog()
    first['models'].append('gpt-image-2.5-sunburst')
    clock[0] += 59
    assert get_image_gateway_model_catalog()['models'] == ['gpt-image-2.5']
    assert opener.open.call_count == 1
    clock[0] += 1
    get_image_gateway_model_catalog()
    assert opener.open.call_count == 2


def test_connection_failure_has_no_retry_and_uses_ten_second_cache(catalog_environment, capsys):
    clock, opener = catalog_environment
    opener.open.side_effect = urllib.error.URLError('http://private.test/v1?key=private-credential')
    result = get_image_gateway_model_catalog()
    assert result['status'] == 'unknown'
    assert result['models'] == []
    clock[0] += 9
    get_image_gateway_model_catalog()
    assert opener.open.call_count == 1
    clock[0] += 1
    get_image_gateway_model_catalog()
    assert opener.open.call_count == 2
    visible = json.dumps(result) + capsys.readouterr().out
    assert 'private.test' not in visible
    assert 'private-credential' not in visible


def test_cache_keys_change_with_gateway_or_credential_and_contain_neither(catalog_environment):
    _, opener = catalog_environment
    _registry(opener, {'data': [{'id': 'gpt-image-2.5'}]})
    configs = [
        {'codexBaseUrl': 'http://first.test/v1', 'codexApiKey': 'first-private-key'},
        {'codexBaseUrl': 'http://first.test/v1', 'codexApiKey': 'second-private-key'},
        {'codexBaseUrl': 'http://second.test/v1', 'codexApiKey': 'second-private-key'},
    ]
    for config in configs:
        get_image_gateway_model_catalog(config)
    get_image_gateway_model_catalog(configs[0])
    assert opener.open.call_count == 3
    assert len(server_common._IMAGE_GATEWAY_MODEL_CATALOG_CACHE) == 3
    keys = list(server_common._IMAGE_GATEWAY_MODEL_CATALOG_CACHE)
    assert all(len(key) == 64 for key in keys)
    assert not any('private-key' in key or '.test' in key for key in keys)


@pytest.mark.parametrize('rows', ([], [{'id': 'gpt-image-2.5'}],
                                 [{'id': 'gpt-image-2.5-sunburst'}]))
def test_matching_cockpit_runtime_adds_only_its_configured_variant(
        catalog_environment, cockpit_sidecar, rows, capsys):
    _, opener = catalog_environment
    directory, config = cockpit_sidecar
    _registry(opener, {'data': rows})
    result = get_image_gateway_model_catalog(config)
    assert result['status'] == 'known'
    assert result['models'] == list(dict.fromkeys(
        [row['id'] for row in rows] + ['gpt-image-2.5-sunburst']))
    assert 'gpt-image-2.5-flare' not in result['models']
    assert result['image_tool_model'] == 'gpt-image-2.5-sunburst'
    assert result['source'] == 'configured_image_tool_model'
    visible = json.dumps(result) + capsys.readouterr().out
    assert str(directory) not in visible
    assert 'sidecar-synthetic-key' not in visible
    assert '127.0.0.1' not in visible


@pytest.mark.parametrize('host,url', (
    ('127.0.0.1', 'http://127.0.0.1:52692/v1/'),
    ('localhost', 'http://localhost:52692/v1'),
    ('::1', 'http://[::1]:52692/v1'),
))
def test_cockpit_metadata_requires_matching_loopback_endpoint(
        catalog_environment, cockpit_sidecar, host, url):
    _, opener = catalog_environment
    directory, config = cockpit_sidecar
    settings = json.loads((directory / 'config.json').read_text())
    settings['host'] = host
    (directory / 'config.json').write_text(json.dumps(settings))
    config['codexBaseUrl'] = url
    _registry(opener, {'data': [{'id': 'gpt-image-2.5'}]})
    assert get_image_gateway_model_catalog(config)['image_tool_model'] == 'gpt-image-2.5-sunburst'


@pytest.mark.parametrize('url,host,port,key', (
    ('http://image.test:52692/v1', 'image.test', 52692, 'sidecar-synthetic-key'),
    ('http://192.168.1.10:52692/v1', '192.168.1.10', 52692, 'sidecar-synthetic-key'),
    ('http://127.0.0.2:52692/v1', '127.0.0.1', 52692, 'sidecar-synthetic-key'),
    ('http://127.0.0.1:52692/v1', '0.0.0.0', 52692, 'sidecar-synthetic-key'),
    ('http://127.0.0.1:52693/v1', '127.0.0.1', 52692, 'sidecar-synthetic-key'),
    ('http://127.0.0.1:52692/v1', '127.0.0.1', '52692', 'sidecar-synthetic-key'),
    ('http://127.0.0.1:52692/v1', '127.0.0.1', 52692, 'different-synthetic-key'),
    ('http://127.0.0.1:52692/v1', '127.0.0.1', 52692, ''),
    ('http://127.0.0.1:52692/other/v1', '127.0.0.1', 52692, 'sidecar-synthetic-key'),
    ('http://127.0.0.1:52692/v1?other=1', '127.0.0.1', 52692, 'sidecar-synthetic-key'),
    ('http://127.0.0.1:52692/v1#other', '127.0.0.1', 52692, 'sidecar-synthetic-key'),
    ('http://user@127.0.0.1:52692/v1', '127.0.0.1', 52692, 'sidecar-synthetic-key'),
    ('ftp://127.0.0.1:52692/v1', '127.0.0.1', 52692, 'sidecar-synthetic-key'),
))
def test_unmatched_gateway_does_not_inherit_cockpit_models(
        catalog_environment, cockpit_sidecar, url, host, port, key):
    _, opener = catalog_environment
    directory, config = cockpit_sidecar
    settings = json.loads((directory / 'config.json').read_text())
    settings.update(host=host, port=port)
    (directory / 'config.json').write_text(json.dumps(settings))
    config.update(codexBaseUrl=url, codexApiKey=key)
    _registry(opener, {'data': [{'id': 'gpt-image-2.5'}]})
    result = get_image_gateway_model_catalog(config)
    assert result['status'] == 'known'
    assert result['models'] == ['gpt-image-2.5']
    assert 'source' not in result
    assert 'image_tool_model' not in result


@pytest.mark.parametrize('model', ('gpt-image-2.5-flare',
                                  'gpt-image-2.5-sunburst-2026-09-30'))
def test_runtime_catalog_preserves_exact_supported_variant(
        catalog_environment, cockpit_sidecar, model):
    _, opener = catalog_environment
    directory, config = cockpit_sidecar
    (directory / 'manifest.json').write_text(json.dumps({'imageGenerationModel': model}))
    _registry(opener, {'data': [{'id': 'gpt-image-2.5'}]})
    result = get_image_gateway_model_catalog(config)
    assert result['models'] == ['gpt-image-2.5', model]
    assert result['image_tool_model'] == model


@pytest.mark.parametrize('manifest', ({}, [], {'imageGenerationModel': None},
                                     {'imageGenerationModel': 'gpt-image-2'},
                                     {'imageGenerationModel': 'gpt-image-2.5'},
                                     {'imageGenerationModel': 'gpt-image-2.5-custom'},
                                     {'imageGenerationModel': 'gpt-image-2.5-sunburst-4k'},
                                     {'imageGenerationModel': 'gpt-image-2.5-sunburst-secret'}))
def test_invalid_or_non_variant_runtime_model_does_not_supplement_registry(
        catalog_environment, cockpit_sidecar, manifest):
    _, opener = catalog_environment
    directory, config = cockpit_sidecar
    (directory / 'manifest.json').write_text(json.dumps(manifest))
    _registry(opener, {'data': []})
    result = get_image_gateway_model_catalog(config)
    assert result['status'] == 'known'
    assert result['models'] == []
    assert 'image_tool_model' not in result


@pytest.mark.parametrize('filename,content', (
    ('config.json', None), ('config.json', '{invalid'), ('config.json', '[]'),
    ('config.json', '{"host":"127.0.0.1","port":52692,"api-keys":"sidecar-synthetic-key"}'),
    ('manifest.json', None), ('manifest.json', '{invalid'),
))
def test_missing_or_invalid_local_metadata_uses_http_registry_only(
        catalog_environment, cockpit_sidecar, filename, content):
    _, opener = catalog_environment
    directory, config = cockpit_sidecar
    path = directory / filename
    if content is None:
        path.unlink()
    else:
        path.write_text(content)
    # Saved preferences never substitute for a missing/invalid runtime manifest.
    (directory.parent / 'config.json').write_text(json.dumps({
        'imageGenerationModel': 'gpt-image-2.5-sunburst',
    }))
    _registry(opener, {'data': [{'id': 'gpt-image-2.5'}]})
    result = get_image_gateway_model_catalog(config)
    assert result['models'] == ['gpt-image-2.5']
    assert 'image_tool_model' not in result


@pytest.mark.parametrize('response', ('failure', 'invalid'))
def test_local_runtime_model_does_not_hide_unknown_http_catalog(
        catalog_environment, cockpit_sidecar, response):
    _, opener = catalog_environment
    _, config = cockpit_sidecar
    if response == 'failure':
        opener.open.side_effect = urllib.error.URLError('gateway unavailable')
    else:
        _registry(opener, {'data': [{}]})
    result = get_image_gateway_model_catalog(config)
    assert result['status'] == 'unknown'
    assert result['models'] == []
    assert 'source' not in result
    assert 'image_tool_model' not in result
    assert opener.open.call_count == 1


def test_runtime_tool_model_change_invalidates_catalog_cache_immediately(
        catalog_environment, cockpit_sidecar):
    _, opener = catalog_environment
    directory, config = cockpit_sidecar
    _registry(opener, {'data': [{'id': 'gpt-image-2.5'}]})
    first = get_image_gateway_model_catalog(config)
    assert first['image_tool_model'] == 'gpt-image-2.5-sunburst'
    assert get_image_gateway_model_catalog(config) == first
    assert opener.open.call_count == 1
    (directory / 'manifest.json').write_text(json.dumps({
        'imageGenerationModel': 'gpt-image-2.5-flare',
    }))
    second = get_image_gateway_model_catalog(config)
    assert second['models'] == ['gpt-image-2.5', 'gpt-image-2.5-flare']
    assert second['image_tool_model'] == 'gpt-image-2.5-flare'
    assert opener.open.call_count == 2
    (directory / 'manifest.json').unlink()
    third = get_image_gateway_model_catalog(config)
    assert third['models'] == ['gpt-image-2.5']
    assert 'image_tool_model' not in third
    assert opener.open.call_count == 3


def test_verified_responses_capability_publishes_flare_without_inferring_snapshots(
        catalog_environment, cockpit_sidecar):
    _, opener = catalog_environment
    _, config = cockpit_sidecar
    server_common.SERVER_CONFIG['codexImageResponsesModels'] = [
        'gpt-image-2.5-flare', 'gpt-image-2.5-flare-2026-09-08', 'gpt-image-2.5-custom',
    ]
    _registry(opener, {'data': [{'id': 'gpt-image-2.5'}]})
    result = get_image_gateway_model_catalog(config)
    assert result['models'] == ['gpt-image-2.5', 'gpt-image-2.5-sunburst', 'gpt-image-2.5-flare']
    assert result['responses_models'] == ['gpt-image-2.5-flare']
    assert result['image_tool_model'] == 'gpt-image-2.5-sunburst'


@pytest.mark.parametrize('change', ('endpoint', 'credential', 'manifest'))
def test_responses_capability_requires_the_matching_runtime(
        catalog_environment, cockpit_sidecar, change):
    _, opener = catalog_environment
    directory, config = cockpit_sidecar
    server_common.SERVER_CONFIG['codexImageResponsesModels'] = ['gpt-image-2.5-flare']
    if change == 'endpoint':
        config['codexBaseUrl'] = 'http://127.0.0.1:52693/v1'
    elif change == 'credential':
        config['codexApiKey'] = 'unmatched-synthetic-key'
    else:
        (directory / 'manifest.json').unlink()
    _registry(opener, {'data': [{'id': 'gpt-image-2.5'}]})
    result = get_image_gateway_model_catalog(config)
    assert result['models'] == ['gpt-image-2.5']
    assert 'responses_models' not in result


def test_client_cannot_enable_responses_capability(catalog_environment, cockpit_sidecar):
    _, opener = catalog_environment
    _, config = cockpit_sidecar
    config['codexImageResponsesModels'] = ['gpt-image-2.5-flare']
    _registry(opener, {'data': [{'id': 'gpt-image-2.5'}]})
    result = get_image_gateway_model_catalog(config)
    assert result['models'] == ['gpt-image-2.5', 'gpt-image-2.5-sunburst']
    assert 'responses_models' not in result


def test_responses_capability_change_updates_cache_and_lists_are_copied(
        catalog_environment, cockpit_sidecar):
    _, opener = catalog_environment
    _, config = cockpit_sidecar
    _registry(opener, {'data': [{'id': 'gpt-image-2.5'}]})
    native = get_image_gateway_model_catalog(config)
    assert 'responses_models' not in native
    server_common.SERVER_CONFIG['codexImageResponsesModels'] = ['gpt-image-2.5-flare']
    responses = get_image_gateway_model_catalog(config)
    assert responses['responses_models'] == ['gpt-image-2.5-flare']
    responses['responses_models'].clear()
    responses['models'].clear()
    cached = get_image_gateway_model_catalog(config)
    assert cached['responses_models'] == ['gpt-image-2.5-flare']
    assert 'gpt-image-2.5-flare' in cached['models']
    cached['responses_models'].append('gpt-image-2.5-sunburst')
    assert get_image_gateway_model_catalog(config)['responses_models'] == ['gpt-image-2.5-flare']
    assert opener.open.call_count == 2
    server_common.SERVER_CONFIG['codexImageResponsesModels'] = []
    assert get_image_gateway_model_catalog(config) == native


def test_opt_in_does_not_hide_http_catalog_failure(catalog_environment, cockpit_sidecar):
    _, opener = catalog_environment
    _, config = cockpit_sidecar
    server_common.SERVER_CONFIG['codexImageResponsesModels'] = ['gpt-image-2.5-flare']
    opener.open.side_effect = urllib.error.URLError('gateway unavailable')
    result = get_image_gateway_model_catalog(config)
    assert result['status'] == 'unknown'
    assert result['models'] == []
    assert 'responses_models' not in result
