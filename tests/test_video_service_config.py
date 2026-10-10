"""Video gateway settings stay separate from text/image and browser credentials."""

import json

import pytest

import server_common as sc
from fx_console import FX_CONFIG_SPEC, FxConfigStore, validate_patch


@pytest.mark.parametrize('managed', [True, False])
def test_video_gateway_uses_server_credentials_and_preserves_other_gateways(monkeypatch, managed):
    monkeypatch.setattr(sc, 'SERVER_MANAGED', managed)
    server = {
        'baseUrl': 'http://127.0.0.1:8046/v1', 'apiKey': 'text-server',
        'videoProvider': 'flow2api', 'flow2apiBaseUrl': 'http://127.0.0.1:38000/v1/',
        'flow2apiApiKey': 'video-server', 'videoModel': 'Omni Flash',
        'flow2apiVideoTimeoutSeconds': 1500,
        'videoDuration': '10', 'videoResolution': '360p', 'videoRefMode': 'VIDEO_FRAMES',
    }
    monkeypatch.setattr(sc, 'SERVER_CONFIG', server)
    client = {
        'baseUrl': 'http://client-gateway/v1', 'apiKey': 'text-client',
        'videoProvider': 'google_fx', 'flow2apiBaseUrl': 'http://untrusted.invalid/v1',
        'flow2apiApiKey': 'video-client', 'videoModel': 'Veo 3.1 - Fast',
        'flow2apiVideoTimeoutSeconds': 1,
        'videoDuration': '4', 'videoResolution': '720p', 'imageAspectRatio': '9:16',
    }
    result = sc.effective_config(client)
    assert result['videoProvider'] == 'flow2api'
    assert result['flow2apiBaseUrl'] == 'http://127.0.0.1:38000/v1'
    assert result['flow2apiApiKey'] == 'video-server'
    assert result['flow2apiVideoTimeoutSeconds'] == 1500
    assert result['baseUrl'] == (server if managed else client)['baseUrl']
    assert result['apiKey'] == (server if managed else client)['apiKey']
    assert result['videoModel'] == 'Omni Flash'
    assert result['videoDuration'] == '10'
    assert result['videoResolution'] == '360p'
    assert result['videoRefMode'] == 'VIDEO_FRAMES'
    assert result['imageAspectRatio'] == '9:16'
    assert client['flow2apiApiKey'] == 'video-client'


@pytest.mark.parametrize('managed', [True, False])
def test_absent_server_config_keeps_existing_provider_and_rejects_client_credentials(monkeypatch, managed):
    monkeypatch.setattr(sc, 'SERVER_MANAGED', managed)
    monkeypatch.setattr(sc, 'SERVER_CONFIG', {})
    result = sc.effective_config({
        'videoProvider': 'flow2api', 'flow2apiApiKey': 'injected',
        'flow2apiBaseUrl': 'http://untrusted.invalid',
    })
    assert result['videoProvider'] == 'google_fx'
    assert result['flow2apiApiKey'] == ''
    assert result['flow2apiBaseUrl'] == 'http://127.0.0.1:38000/v1'
    assert result['flow2apiVideoTimeoutSeconds'] == 1800


def test_public_report_contains_only_safe_video_fields(monkeypatch):
    monkeypatch.setattr(sc, 'SERVER_CONFIG', {
        'videoProvider': 'flow2api', 'videoDuration': '10', 'videoResolution': '360p',
        'flow2apiApiKey': 'private-test-key', 'apiKey': 'another-private-key',
        'flow2apiBaseUrl': 'http://private-hostname/v1', 'imageAspectRatio': '9:16',
    })
    report = sc.video_service_config_report()
    assert set(report) == {
        'videoProvider', 'videoModel', 'videoDuration', 'videoResolution',
        'videoRefMode', 'flow2apiConfigured',
        'flow2apiVideoConcurrency', 'videoRetryCount', 'videoContinuousGeneration',
    }
    assert report['flow2apiConfigured'] is True
    assert report['videoDuration'] == '10'
    assert report['videoResolution'] == '360p'
    assert 'private' not in json.dumps(report)


def test_public_report_defaults_to_existing_video_service(monkeypatch):
    monkeypatch.setattr(sc, 'SERVER_CONFIG', {})
    report = sc.video_service_config_report()
    assert report['videoProvider'] == 'google_fx'
    assert report['flow2apiConfigured'] is False


def test_public_store_saves_provider_without_exposing_connection(tmp_path):
    config = {'flow2apiApiKey': 'private-key', 'flow2apiBaseUrl': 'http://private-host/v1'}
    store = FxConfigStore(config, tmp_path / 'config.json')
    result = store.save({'videoProvider': 'flow2api'})
    assert result['config']['videoProvider'] == 'flow2api'
    assert config['flow2apiApiKey'] == 'private-key'
    assert 'private' not in json.dumps(result)
    assert 'flow2apiApiKey' not in store.schema()
    assert 'flow2apiBaseUrl' not in store.schema()
    assert FX_CONFIG_SPEC['videoProvider']['hot'] is True


@pytest.mark.parametrize('patch', [
    {'videoProvider': 'unknown'}, {'flow2apiApiKey': 'injected'},
    {'flow2apiBaseUrl': 'http://untrusted.invalid'},
])
def test_public_config_rejects_unknown_provider_and_connection_edits(patch):
    with pytest.raises(ValueError):
        validate_patch(patch)


def test_server_env_overrides_flow_connection_only(monkeypatch, tmp_path):
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({'baseUrl': 'http://127.0.0.1:8046/v1'}))
    monkeypatch.setattr(sc, 'SERVER_CONFIG_FILE', str(path))
    monkeypatch.delenv('SPARK_BASE_URL', raising=False)
    monkeypatch.setenv('SPARK_VIDEO_PROVIDER', 'flow2api')
    monkeypatch.setenv('SPARK_FLOW2API_BASE_URL', 'http://127.0.0.1:38000/v1')
    monkeypatch.setenv('SPARK_FLOW2API_API_KEY', 'environment-private-key')
    result = sc._load_server_config()
    assert result['baseUrl'] == 'http://127.0.0.1:8046/v1'
    assert result['flow2apiApiKey'] == 'environment-private-key'
    assert result['videoProvider'] == 'flow2api'


@pytest.mark.parametrize('managed', [True, False])
@pytest.mark.parametrize('enabled', [True, False])
def test_continuous_generation_is_server_authoritative(monkeypatch, managed, enabled):
    monkeypatch.setattr(sc, 'SERVER_MANAGED', managed)
    monkeypatch.setattr(sc, 'SERVER_CONFIG', {'videoContinuousGeneration': enabled})
    result = sc.effective_config({'videoContinuousGeneration': not enabled})
    assert result['videoContinuousGeneration'] is enabled
    assert sc.video_service_config_report()['videoContinuousGeneration'] is enabled


def test_continuous_generation_defaults_on(monkeypatch):
    monkeypatch.setattr(sc, 'SERVER_CONFIG', {})
    assert sc.effective_config({})['videoContinuousGeneration'] is True
    assert FX_CONFIG_SPEC['videoContinuousGeneration']['default'] is True
