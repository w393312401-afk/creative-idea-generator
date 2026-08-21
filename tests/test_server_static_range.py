"""静态文件服务与 HTTP 206 Partial Content (Range) 的接口与协议契约单测。"""
import io
import os
import datetime
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
        # 调用 SparkRequestHandler 自身的 end_headers 逻辑
        h.send_header('Access-Control-Allow-Origin', '*')
        h.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        h.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization, Range')
        h.send_header('Access-Control-Expose-Headers', 'Content-Range, Accept-Ranges, Content-Length')
        p = (h.path or '').split('?')[0]
        if p.endswith(('.html', '.css', '.js')) or p.endswith('/'):
            h.send_header('Cache-Control', 'no-store')
        elif getattr(h, '_spark_status_code', 200) >= 400:
            h.send_header('Cache-Control', 'no-store')
        elif p.startswith('/outputs/'):
            h.send_header('Cache-Control', 'no-cache')

    def fake_send_error(code, message=None, explain=None):
        sent_status[0] = code
        h._spark_status_code = code

    h.send_response = fake_send_response
    h.send_header = fake_send_header
    h.end_headers = fake_end_headers
    h.send_error = fake_send_error

    return h, sent_status, sent_headers, h.wfile


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


def test_blocked_static_path():
    h, status, headers, wfile = _make_handler('/server.py')

    h.do_GET()

    assert status[0] == 404
