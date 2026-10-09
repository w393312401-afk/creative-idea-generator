"""历史模型迁移后，真实请求的模型、网关和联网工具必须保持一致。"""
import base64
import json
from unittest.mock import Mock

import pytest

import prompt_pipeline as pp


@pytest.fixture
def gateway_config():
    # 显式使用测试网关与测试密钥，不依赖开发机的私有配置。
    return {
        'baseUrl': 'http://gemini.test/v1',
        'apiKey': 'gemini-test-key',
        'codexBaseUrl': 'http://codex.test/v1',
        'codexApiKey': 'codex-test-key',
    }


@pytest.mark.parametrize('source_model, expected_model', [
    ('gpt-4o', 'gpt-6.1-sol'),
    ('gpt-4.1', 'gpt-6.1-sol'),
    ('gpt-5.5', 'gpt-6.1-sol'),
    ('gpt-6.1-sol', 'gpt-6.1-sol'),
    ('gpt-6-astra', 'gpt-6-astra'),
    ('gpt-6-sol', 'gpt-6-sol'),
    ('gpt-6-luna', 'gpt-6-luna'),
])
def test_chat_gpt_model_uses_codex_gateway_and_native_search(
        monkeypatch, gateway_config, source_model, expected_model):
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.read.return_value = b'{"choices":[{"message":{"content":"test reply"}}]}'
    opener = Mock()
    opener.open.return_value = response
    monkeypatch.setattr(pp.urllib.request, 'build_opener', lambda *args: opener)

    result = pp._chat({**gateway_config, 'model': source_model},
                      'system', 'search request', enable_search=True)

    assert result == 'test reply'
    request = opener.open.call_args.args[0]
    payload = json.loads(request.data)
    assert request.full_url == 'http://codex.test/v1/chat/completions'
    assert request.get_header('Authorization') == 'Bearer codex-test-key'
    assert payload['model'] == expected_model
    assert payload['tools'] == [{'type': 'web_search'}]
    assert 'tool_choice' not in payload
    opener.open.assert_called_once()


def test_chat_retired_gemini_preserves_gemini_gateway_and_search_tool(monkeypatch, gateway_config):
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.read.return_value = b'{"choices":[{"message":{"content":"test reply"}}]}'
    opener = Mock()
    opener.open.return_value = response
    monkeypatch.setattr(pp.urllib.request, 'build_opener', lambda *args: opener)

    pp._chat({**gateway_config, 'model': 'gemini-3.7-flash-high'},
             'system', 'search request', enable_search=True)

    request = opener.open.call_args.args[0]
    payload = json.loads(request.data)
    assert request.full_url == 'http://gemini.test/v1/chat/completions'
    assert request.get_header('Authorization') == 'Bearer gemini-test-key'
    assert payload['model'] == 'gemini-3.8-flash-high'
    assert payload['tools'][0]['type'] == 'function'
    assert payload['tools'][0]['function']['name'] == 'web_search'


@pytest.mark.parametrize('explicit_model', [None, 'gpt-4.1'])
def test_multimodal_chat_migrates_model_before_routing(
        monkeypatch, tmp_path, gateway_config, explicit_model):
    image = tmp_path / 'reference.png'
    image_bytes = base64.b64decode(
        'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aXioAAAAASUVORK5CYII=')
    image.write_bytes(image_bytes)
    transport = Mock(return_value=b'{"choices":[{"message":{"content":"visual review"}}]}')
    monkeypatch.setattr(pp, '_execute_request_with_retry', transport)
    monkeypatch.setattr(pp.urllib.request, 'build_opener', lambda *args: Mock())
    # 一个入口来自历史全局配置，另一个来自调用方显式提供的辅助/审查模型。
    config = {**gateway_config, 'model': 'gpt-4o' if explicit_model is None
              else 'gemini-3.8-flash-high'}

    result = pp._multimodal_chat(config, 'system', 'review image', [str(image)],
                                 model=explicit_model)

    assert result == 'visual review'
    request = transport.call_args.args[0]
    payload = json.loads(request.data)
    assert request.full_url == 'http://codex.test/v1/chat/completions'
    assert request.get_header('Authorization') == 'Bearer codex-test-key'
    assert payload['model'] == 'gpt-6.1-sol'
    content = payload['messages'][1]['content']
    assert content[0] == {'type': 'text', 'text': 'review image'}
    assert content[1]['image_url']['url'] == (
        'data:image/png;base64,' + base64.b64encode(image_bytes).decode('ascii'))
    transport.assert_called_once()


def _fake_opener(monkeypatch, body=b'{"choices":[{"message":{"content":"claude reply"}}]}'):
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.read.return_value = body
    opener = Mock()
    opener.open.return_value = response
    monkeypatch.setattr(pp.urllib.request, 'build_opener', lambda *args: opener)
    return opener


@pytest.mark.parametrize('model, expected_cap', [
    ('claude-opus-5-5', 65536),
    ('claude-sonnet-5-5', 65536),
    ('claude-fable-5-1', 65536),
    ('claude-haiku-4-5', 64000),
])
def test_chat_claude_uses_claude_gateway_without_sampling_or_search_tools(
        monkeypatch, gateway_config, model, expected_cap):
    opener = _fake_opener(monkeypatch)
    config = {**gateway_config, 'claudeBaseUrl': 'http://claude.test/v1',
              'claudeApiKey': 'claude-test-key', 'model': 'gpt-6.1-sol'}

    result = pp._chat(config, 'system', 'write something', model=model,
                      temperature=0.85, max_tokens=65536, enable_search=True)

    assert result == 'claude reply'
    request = opener.open.call_args.args[0]
    payload = json.loads(request.data)
    assert request.full_url == 'http://claude.test/v1/chat/completions'
    assert request.get_header('Authorization') == 'Bearer claude-test-key'
    assert payload['model'] == model
    # Claude 5 系列拒收自定义采样参数；联网工具声明形状因网关而异，不凭猜测附加。
    assert 'temperature' not in payload
    assert 'top_p' not in payload
    assert 'tools' not in payload and 'tool_choice' not in payload
    assert payload['max_tokens'] == expected_cap
    assert payload['messages'][0] == {'role': 'system', 'content': 'system'}


def test_chat_claude_without_dedicated_gateway_uses_the_main_gateway(monkeypatch, gateway_config):
    opener = _fake_opener(monkeypatch)

    pp._chat({**gateway_config, 'model': 'claude-sonnet-5-5'}, 'system', 'hello')

    request = opener.open.call_args.args[0]
    assert request.full_url == 'http://gemini.test/v1/chat/completions'
    assert request.get_header('Authorization') == 'Bearer gemini-test-key'
    assert 'temperature' not in json.loads(request.data)


def test_chat_other_models_keep_their_sampling_parameters(monkeypatch, gateway_config):
    opener = _fake_opener(monkeypatch)

    pp._chat({**gateway_config, 'model': 'gpt-6.1-sol'}, 'system', 'hello', temperature=0.7)

    payload = json.loads(opener.open.call_args.args[0].data)
    assert payload['temperature'] == 0.7


def test_multimodal_chat_claude_uses_claude_gateway_without_temperature(
        monkeypatch, tmp_path, gateway_config):
    image = tmp_path / 'reference.png'
    image.write_bytes(base64.b64decode(
        'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aXioAAAAASUVORK5CYII='))
    transport = Mock(return_value=b'{"choices":[{"message":{"content":"visual review"}}]}')
    monkeypatch.setattr(pp, '_execute_request_with_retry', transport)
    monkeypatch.setattr(pp.urllib.request, 'build_opener', lambda *args: Mock())
    config = {**gateway_config, 'claudeBaseUrl': 'http://claude.test/v1', 'claudeApiKey': 'claude-test-key'}

    result = pp._multimodal_chat(config, 'system', 'review image', [str(image)],
                                 model='claude-sonnet-5-5')

    assert result == 'visual review'
    request = transport.call_args.args[0]
    payload = json.loads(request.data)
    assert request.full_url == 'http://claude.test/v1/chat/completions'
    assert request.get_header('Authorization') == 'Bearer claude-test-key'
    assert payload['model'] == 'claude-sonnet-5-5'
    assert 'temperature' not in payload
