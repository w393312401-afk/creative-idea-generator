"""JSON content negotiation preserves bytes, Unicode, response boundaries, and errors."""
import gzip
import json

import pytest
from test_server_static_range import _make_handler


PAYLOAD = {'items': [{'title': '测试项目', 'prompt': '完整正文需要保持原样。' * 120}] * 8}


def response(payload, encoding='', status=200, head=False):
    handler, statuses, headers, body = _make_handler(
        '/api/library/index', {'Accept-Encoding': encoding}, is_head=head)
    handler._send_json(payload, status)
    return statuses[0], headers, body.getvalue()


def test_large_json_gzip_roundtrip_and_smaller_transfer():
    status, headers, body = response(PAYLOAD, 'gzip, deflate, br')
    decoded = gzip.decompress(body)
    assert status == 200
    assert json.loads(decoded) == PAYLOAD
    assert len(body) < len(decoded) // 4
    assert headers['content-encoding'] == 'gzip'
    assert headers['vary'] == 'Accept-Encoding'
    assert int(headers['content-length']) == len(body)


@pytest.mark.parametrize('encoding', ['', 'br', 'gzip;q=0', 'gzip;q=0,*;q=1'])
def test_identity_fallback_is_valid_json_with_correct_length(encoding):
    _, headers, body = response(PAYLOAD, encoding)
    assert json.loads(body) == PAYLOAD
    assert 'content-encoding' not in headers
    assert headers['vary'] == 'Accept-Encoding'
    assert int(headers['content-length']) == len(body)


def test_tiny_json_is_not_compressed_and_error_status_survives():
    payload = {'error': '访问被拒绝'}
    status, headers, body = response(payload, 'gzip', status=401)
    assert status == 401 and json.loads(body) == payload
    assert 'content-encoding' not in headers
    assert int(headers['content-length']) == len(body)


def test_head_has_get_headers_without_a_body():
    _, get_headers, _ = response(PAYLOAD, 'gzip')
    _, head_headers, body = response(PAYLOAD, 'gzip', head=True)
    assert head_headers == get_headers
    assert body == b''
