import gzip
import os

import pytest
import server
from test_server_static_range import _make_handler
from web_runtime.compression import accepts_gzip


@pytest.mark.parametrize('header,expected', [
    ('gzip, deflate, br', True), ('GZip; q=0.5', True), ('*', True),
    ('gzip;q=0, *;q=1', False), ('br', False), ('', False),
    ('gzip;q=invalid', False), ('gzip;q=2', False), ('gzip;q=nan', False),
])
def test_encoding_negotiation(header, expected):
    assert accepts_gzip(header) is expected


@pytest.fixture
def asset(tmp_path, monkeypatch):
    path = tmp_path / 'app.js'
    data = b'const example = "compressible static content";\n' * 100
    path.write_bytes(data)
    monkeypatch.setattr(server.SparkRequestHandler, 'translate_path', lambda self, url: str(path))
    return path, data


def request(headers=None, head=False):
    handler, status, sent, body = _make_handler('/app.js', headers, is_head=head)
    handler.do_HEAD() if head else handler.do_GET()
    return status[0], sent, body.getvalue()


def test_gzip_get_and_head_match_original(asset):
    _, data = asset
    status, headers, body = request({'Accept-Encoding': 'gzip'})
    assert status == 200
    assert gzip.decompress(body) == data
    assert len(body) < len(data)
    assert headers['content-encoding'] == 'gzip'
    assert headers['vary'] == 'Accept-Encoding'
    assert int(headers['content-length']) == len(body)
    status, head_headers, head_body = request({'Accept-Encoding': 'gzip'}, head=True)
    assert status == 200 and head_body == b''
    assert head_headers == headers


def test_identity_and_range_remain_uncompressed(asset):
    _, data = asset
    for encoding in ('', 'gzip;q=0'):
        status, headers, body = request({'Accept-Encoding': encoding})
        assert status == 200 and body == data
        assert 'content-encoding' not in headers
        assert headers['vary'] == 'Accept-Encoding'
    status, headers, body = request({'Accept-Encoding': 'gzip', 'Range': 'bytes=10-99'})
    assert status == 206 and body == data[10:100]
    assert headers['content-range'] == f'bytes 10-99/{len(data)}'
    assert 'content-encoding' not in headers


def test_not_modified_keeps_vary(asset):
    _, headers, _ = request({'Accept-Encoding': 'gzip'})
    status, headers, body = request({'Accept-Encoding': 'gzip', 'If-None-Match': headers['etag']})
    assert status == 304 and body == b''
    assert headers['vary'] == 'Accept-Encoding'


def test_replaced_file_invalidates_compressed_cache(asset):
    path, data = asset
    request({'Accept-Encoding': 'gzip'})
    stat = path.stat()
    replacement = path.with_suffix('.tmp')
    updated = data.replace(b'example', b'changed')
    replacement.write_bytes(updated)
    os.utime(replacement, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    replacement.replace(path)
    _, _, body = request({'Accept-Encoding': 'gzip'})
    assert gzip.decompress(body) == updated


def test_media_is_not_compressed(asset):
    path, data = asset
    path.rename(path.with_suffix('.mp4'))
    # File type is taken from the resolved file, not from a misleading URL.
    handler, status, headers, body = _make_handler('/outputs/test.mp4', {'Accept-Encoding': 'gzip'})
    handler.translate_path = lambda url: str(path.with_suffix('.mp4'))
    handler.do_GET()
    assert status[0] == 200 and body.getvalue() == data
    assert 'content-encoding' not in headers


def test_compressed_cache_has_a_byte_limit(tmp_path, monkeypatch):
    from collections import OrderedDict
    from web_runtime import compression
    monkeypatch.setattr(compression, '_CACHE', OrderedDict())
    monkeypatch.setattr(compression, '_CACHE_BYTES', 0)
    monkeypatch.setattr(compression, '_MAX_CACHE_BYTES', 100)
    for index in range(12):
        path = tmp_path / f'{index}.js'
        raw = (f'const value = {index};\n' * 100).encode()
        path.write_bytes(raw)
        with path.open('rb') as stream:
            encoded = compression.gzip_body(stream, str(path), path.stat())
        assert gzip.decompress(encoded) == raw
        assert sum(map(len, compression._CACHE.values())) <= 100
