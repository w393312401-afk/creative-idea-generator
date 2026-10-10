"""静态文件服务与 HTTP 206 Partial Content (Range) 的接口与协议契约单测。"""
import io
import os
import datetime
from email.utils import format_datetime
import pytest

import server


class DummyWfile(io.BytesIO):
    pass


class DummyHeaders(dict):
    def get(self, k, default=None):
        for key, val in self.items():
            if key.lower() == k.lower():
                return val
        return default


def _make_handler(path, headers=None, is_head=False):
    h = object.__new__(server.SparkRequestHandler)
    h.path = path
    h.command = 'HEAD' if is_head else 'GET'
    h.request_version = 'HTTP/1.1'
    h.headers = DummyHeaders(headers or {})
    h.wfile = DummyWfile()
    h._headers_buffer = []
    h.responses = {}
    h._spark_status_code = 200
    h.close_connection = False

    # 捕获状态码与响应头
    sent_headers = {}
    sent_status = [None]

    def fake_send_response(code, message=None):
        sent_status[0] = code
        h._spark_status_code = code

    def fake_send_header(keyword, value):
        sent_headers[keyword.lower()] = str(value)

    def fake_end_headers():
        # Exercise the real cache/CORS policy, suppressing only socket I/O.
        h.flush_headers = lambda: None
        server.SparkRequestHandler.end_headers(h)

    def fake_send_error(code, message=None, explain=None):
        sent_status[0] = code
        h._spark_status_code = code
        fake_end_headers()

    h.send_response = fake_send_response
    h.send_header = fake_send_header
    h.end_headers = fake_end_headers
    h.send_error = fake_send_error

    return h, sent_status, sent_headers, h.wfile


@pytest.mark.parametrize('path', [
    '/outputs/project/.pipeline_state.json',
    '/outputs/project/%2eframe_facts_cache.json',
    '/outputs/project/.internal/state.json',
])
@pytest.mark.parametrize('method', ['GET', 'HEAD'])
def test_hidden_output_state_is_not_served(path, method):
    h, status, _, wfile = _make_handler(path, is_head=method == 'HEAD')
    getattr(h, 'do_' + method)()
    assert status[0] == 404
    assert wfile.getvalue() == b''


@pytest.fixture
def sample_video_file(tmp_path, monkeypatch):
    """创建一个 1000 字节的假 MP4 文件，并打桩 translate_path。"""
    test_file = tmp_path / "test_video.mp4"
    data = bytes(range(256)) * 3 + bytes(range(232))  # exactly 1000 bytes
    test_file.write_bytes(data)

    def fake_translate_path(self, path):
        if path.startswith('/outputs/test_video.mp4'):
            return str(test_file)
        return str(tmp_path / path.lstrip('/'))

    monkeypatch.setattr(server.SparkRequestHandler, 'translate_path', fake_translate_path)
    return test_file, data


def test_full_get_without_range(sample_video_file):
    test_file, data = sample_video_file
    h, status, headers, wfile = _make_handler('/outputs/test_video.mp4')

    h.do_GET()

    assert status[0] == 200
    assert headers['content-type'] == 'video/mp4'
    assert headers['content-length'] == '1000'
    assert headers['accept-ranges'] == 'bytes'
    assert headers['etag'].startswith('"')
    assert headers['cache-control'] == 'no-cache'
    assert headers['access-control-allow-origin'] == '*'
    assert 'range' in headers['access-control-allow-headers'].lower()
    assert 'content-range' in headers['access-control-expose-headers'].lower()
    assert wfile.getvalue() == data


def test_range_closed_interval(sample_video_file):
    test_file, data = sample_video_file
    h, status, headers, wfile = _make_handler('/outputs/test_video.mp4', {'Range': 'bytes=0-499'})

    h.do_GET()

    assert status[0] == 206
    assert headers['content-type'] == 'video/mp4'
    assert headers['content-range'] == 'bytes 0-499/1000'
    assert headers['content-length'] == '500'
    assert headers['accept-ranges'] == 'bytes'
    assert wfile.getvalue() == data[0:500]


def test_range_open_start(sample_video_file):
    test_file, data = sample_video_file
    h, status, headers, wfile = _make_handler('/outputs/test_video.mp4', {'Range': 'bytes=500-'})

    h.do_GET()

    assert status[0] == 206
    assert headers['content-range'] == 'bytes 500-999/1000'
    assert headers['content-length'] == '500'
    assert wfile.getvalue() == data[500:1000]


def test_range_suffix(sample_video_file):
    test_file, data = sample_video_file
    h, status, headers, wfile = _make_handler('/outputs/test_video.mp4', {'Range': 'bytes=-200'})

    h.do_GET()

    assert status[0] == 206
    assert headers['content-range'] == 'bytes 800-999/1000'
    assert headers['content-length'] == '200'
    assert wfile.getvalue() == data[800:1000]


def test_range_out_of_bounds(sample_video_file):
    test_file, data = sample_video_file
    h, status, headers, wfile = _make_handler('/outputs/test_video.mp4', {'Range': 'bytes=1500-2000'})

    h.do_GET()

    assert status[0] == 416
    assert headers['content-range'] == 'bytes */1000'
    assert wfile.getvalue() == b''


def test_head_with_range(sample_video_file):
    test_file, data = sample_video_file
    h, status, headers, wfile = _make_handler('/outputs/test_video.mp4', {'Range': 'bytes=100-299'}, is_head=True)

    h.do_HEAD()

    assert status[0] == 206
    assert headers['content-range'] == 'bytes 100-299/1000'
    assert headers['content-length'] == '200'
    assert wfile.getvalue() == b''


def test_head_without_range(sample_video_file):
    test_file, data = sample_video_file
    h, status, headers, wfile = _make_handler('/outputs/test_video.mp4', is_head=True)

    h.do_HEAD()

    assert status[0] == 200
    assert headers['content-length'] == '1000'
    assert headers['accept-ranges'] == 'bytes'
    assert wfile.getvalue() == b''


@pytest.mark.parametrize('is_head', [False, True], ids=['get', 'head'])
@pytest.mark.parametrize('validator', [
    'strong', 'weak', 'list', 'weak-list', 'wildcard',
])
def test_output_media_etag_not_modified(sample_video_file, is_head, validator):
    h, _, first_headers, _ = _make_handler('/outputs/test_video.mp4')
    h.do_GET()
    etag = first_headers['etag']
    requested = {
        'strong': etag,
        'weak': 'W/' + etag,
        'list': '"different", ' + etag,
        'weak-list': ' W/"different", W/' + etag + ' ',
        'wildcard': '*',
    }[validator]
    h, status, headers, body = _make_handler('/outputs/test_video.mp4', {
        'If-None-Match': requested,
        'If-Modified-Since': 'Thu, 01 Jan 1970 00:00:00 GMT',
    }, is_head=is_head)
    h.do_HEAD() if is_head else h.do_GET()
    assert status[0] == 304
    assert headers['etag'] == etag
    assert headers['last-modified'] == first_headers['last-modified']
    assert headers['cache-control'] == 'no-cache'
    assert 'content-length' not in headers
    assert body.getvalue() == b''


@pytest.mark.parametrize('validator', ['"different"', 'W/"different"', '"one", W/"two"', ''])
def test_output_media_etag_takes_precedence_over_ims(sample_video_file, validator):
    _, data = sample_video_file
    h, _, first_headers, _ = _make_handler('/outputs/test_video.mp4')
    h.do_GET()
    h, status, headers, body = _make_handler('/outputs/test_video.mp4', {
        'If-None-Match': validator,
        'If-Modified-Since': first_headers['last-modified'],
    })
    h.do_GET()
    assert status[0] == 200
    assert headers['etag'] == first_headers['etag']
    assert body.getvalue() == data


def test_output_media_legacy_ims_returns_completed_body(sample_video_file):
    _, data = sample_video_file
    h, _, first_headers, _ = _make_handler('/outputs/test_video.mp4')
    h.do_GET()
    h, status, headers, body = _make_handler('/outputs/test_video.mp4', {
        'If-Modified-Since': first_headers['last-modified'],
    })
    h.do_GET()
    assert status[0] == 200
    assert headers['etag'] == first_headers['etag']
    assert body.getvalue() == data


@pytest.mark.parametrize('updated,elapsed_ns', [
    (b'new image', 100_000_000), (b'longer new image', 0),
], ids=['same-size-new-nanosecond', 'changed-size-same-timestamp'])
def test_output_image_rewrite_within_same_second_invalidates_etag(tmp_path, monkeypatch, updated, elapsed_ns):
    path = tmp_path / 'cover.webp'
    path.write_bytes(b'old image')
    base_ns = 1_700_000_000_100_000_000
    os.utime(path, ns=(base_ns, base_ns))
    monkeypatch.setattr(server.SparkRequestHandler, 'translate_path', lambda self, url: str(path))
    h, _, first_headers, _ = _make_handler('/outputs/project/cover.webp')
    h.do_GET()
    path.write_bytes(updated)
    new_ns = base_ns + elapsed_ns
    os.utime(path, ns=(new_ns, new_ns))
    h, status, headers, body = _make_handler('/outputs/project/cover.webp', {
        'If-None-Match': first_headers['etag'],
        'If-Modified-Since': first_headers['last-modified'],
    })
    h.do_GET()
    assert status[0] == 200
    assert headers['last-modified'] == first_headers['last-modified']
    assert headers['etag'] != first_headers['etag']
    assert body.getvalue() == updated


@pytest.mark.parametrize('is_head', [False, True], ids=['get', 'head'])
def test_empty_output_media_is_not_cacheable_then_filled_in_same_second(tmp_path, monkeypatch, is_head):
    path = tmp_path / 'cover.webp'
    path.touch()
    base_ns = 1_700_000_000_100_000_000
    os.utime(path, ns=(base_ns, base_ns))
    monkeypatch.setattr(server.SparkRequestHandler, 'translate_path', lambda self, url: str(path))
    h, status, headers, body = _make_handler('/outputs/project/cover.webp', is_head=is_head)
    h.do_HEAD() if is_head else h.do_GET()
    assert status[0] == 404
    assert headers['cache-control'] == 'no-store'
    assert 'etag' not in headers
    assert body.getvalue() == b''
    path.write_bytes(b'completed image')
    new_ns = base_ns + 100_000_000
    os.utime(path, ns=(new_ns, new_ns))
    legacy_date = datetime.datetime.fromtimestamp(base_ns // 1_000_000_000,
                                                 datetime.timezone.utc)
    h, status, headers, body = _make_handler('/outputs/project/cover.webp', {
        'If-Modified-Since': format_datetime(legacy_date, usegmt=True),
    })
    h.do_GET()
    assert status[0] == 200
    assert headers['content-length'] == str(len(b'completed image'))
    assert 'etag' in headers
    assert body.getvalue() == b'completed image'


def test_empty_ordinary_static_file_remains_servable(tmp_path, monkeypatch):
    path = tmp_path / 'empty.txt'
    path.touch()
    monkeypatch.setattr(server.SparkRequestHandler, 'translate_path', lambda self, url: str(path))
    h, status, headers, body = _make_handler('/empty.txt')
    h.do_GET()
    assert status[0] == 200
    assert headers['content-length'] == '0'
    assert body.getvalue() == b''


def test_output_media_range_still_serves_bytes_with_matching_etag(sample_video_file):
    _, data = sample_video_file
    h, _, first_headers, _ = _make_handler('/outputs/test_video.mp4')
    h.do_GET()
    h, status, headers, body = _make_handler('/outputs/test_video.mp4', {
        'Range': 'bytes=0-99', 'If-None-Match': first_headers['etag'],
    })
    h.do_GET()
    assert status[0] == 206
    assert headers['etag'] == first_headers['etag']
    assert headers['content-range'] == 'bytes 0-99/1000'
    assert body.getvalue() == data[:100]


def test_generated_svg_etag_distinguishes_gzip_representation(tmp_path, monkeypatch):
    path = tmp_path / 'generated.svg'
    data = b'<svg><!-- generated graphic --></svg>' * 100
    path.write_bytes(data)
    monkeypatch.setattr(server.SparkRequestHandler, 'translate_path', lambda self, url: str(path))
    h, _, identity_headers, _ = _make_handler('/outputs/project/generated.svg')
    h.do_GET()
    h, status, gzip_headers, _ = _make_handler('/outputs/project/generated.svg', {
        'Accept-Encoding': 'gzip', 'If-None-Match': identity_headers['etag'],
    })
    h.do_GET()
    assert status[0] == 200
    assert gzip_headers['etag'] != identity_headers['etag']
    assert gzip_headers['content-encoding'] == 'gzip'
    h, status, headers, body = _make_handler('/outputs/project/generated.svg', {
        'Accept-Encoding': 'gzip', 'If-None-Match': gzip_headers['etag'],
    })
    h.do_GET()
    assert status[0] == 304
    assert headers['etag'] == gzip_headers['etag']
    assert headers['vary'] == 'Accept-Encoding'
    assert body.getvalue() == b''


def test_blocked_static_path():
    h, status, headers, wfile = _make_handler('/server.py')

    h.do_GET()

    assert status[0] == 404
