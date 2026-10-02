"""The page gets gateway image model availability without receiving credentials."""
import json

import pytest

import server


@pytest.mark.parametrize('catalog', [
    {'status': 'known', 'models': ['gpt-image-2.5'], 'message': '已读取图片网关的可用型号。'},
    {'status': 'unknown', 'models': [], 'message': '暂时无法读取图片网关的可用型号。'},
])
def test_mode_publishes_image_model_availability(monkeypatch, catalog):
    monkeypatch.setattr(server, 'get_image_gateway_model_catalog', lambda: catalog)
    handler = object.__new__(server.SparkRequestHandler)
    handler.path, handler.headers = '/api/mode', {}
    sent = []
    handler._send_json = lambda body, status=200: sent.append((status, body))
    handler.do_GET()
    status, body = sent[0]
    assert status == 200
    assert body['image_gateway_models'] == catalog
    encoded = json.dumps(body['image_gateway_models'])
    assert 'apiKey' not in encoded and 'Authorization' not in encoded
