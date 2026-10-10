"""Static content versions and cache policy using isolated on-disk fixtures."""
import gzip
import os
import re

import pytest

from test_server_static_range import _make_handler
from web_runtime.assets import content_version


@pytest.fixture
def frontend(tmp_path):
    (tmp_path / 'js').mkdir()
    (tmp_path / 'css').mkdir()
    javascript = b'const message = "current frontend bytes";\n' * 80
    stylesheet = b'body { color: #222; background: #eee; }\n' * 80
    (tmp_path / 'js/app.js').write_bytes(javascript)
    (tmp_path / 'css/app.css').write_bytes(stylesheet)
    markup = '''<!doctype html><html><head>
<link rel="preload" as="style" href="css/app.css?v=old">
<link href="css/app.css?v=old" rel="stylesheet">
<link rel="stylesheet" href="https://fonts.example/style.css?v=external">
<link rel="icon" href="css/app.css?v=icon">
</head><body>中文内容
<!-- <script src="js/app.js?v=comment"></script> -->
<script>const literal = '<script src="js/app.js?v=inline">';</script>
<script onclick="src='js/app.js?v=attribute'" data-src="js/app.js?v=data"
        src="js/app.js?mode=full&amp;v=old#script" defer></script>
</body></html>'''.encode() + b'<!-- cache acceptance fixture padding -->' * 100
    (tmp_path / 'index.html').write_bytes(markup)
    return tmp_path, javascript, stylesheet, markup


def request(root, path='/index.html', headers=None, head=False):
    handler, statuses, sent, body = _make_handler(path, {'Host': 'spark.test', **(headers or {})}, is_head=head)
    handler.directory = str(root)
    handler.do_HEAD() if head else handler.do_GET()
    return statuses[0], sent, body.getvalue()


def test_html_versions_real_script_stylesheet_and_preload_only(frontend):
    root, javascript, stylesheet, original = frontend
    status, headers, body = request(root)
    assert status == 200 and headers['cache-control'] == 'no-store'
    assert int(headers['content-length']) == len(body)
    js_hash, css_hash = content_version(javascript), content_version(stylesheet)
    assert f'src="js/app.js?mode=full&amp;v={js_hash}#script"'.encode() in body
    assert body.count(f'href="css/app.css?v={css_hash}"'.encode()) == 2
    for untouched in (b'v=external', b'v=icon', b'v=comment', b'v=inline', b'v=attribute', b'v=data'):
        assert untouched in body
    assert '中文内容'.encode() in body
    assert (root / 'index.html').read_bytes() == original
    status, directory_headers, directory_body = request(root, '/')
    assert status == 200 and directory_body == body
    assert directory_headers['cache-control'] == 'no-store'


@pytest.mark.parametrize('method', ['in-place', 'atomic'])
def test_same_size_same_mtime_dependency_replacement_refreshes_html_and_etags(frontend, method):
    root, javascript, _, _ = frontend
    path = root / 'js/app.js'
    old_stat = path.stat()
    _, html_headers, _ = request(root, headers={'Accept-Encoding': 'gzip'})
    assert html_headers['content-encoding'] == 'gzip'
    _, js_headers, _ = request(root, '/js/app.js')
    changed = javascript.replace(b'current', b'updated')
    if method == 'atomic':
        replacement = root / 'replacement.tmp'
        replacement.write_bytes(changed)
        os.utime(replacement, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
        replacement.replace(path)
    else:
        path.write_bytes(changed)
        os.utime(path, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
    assert path.stat().st_size == old_stat.st_size
    assert path.stat().st_mtime_ns == old_stat.st_mtime_ns
    for encoding in ('', 'gzip'):
        status, headers, body = request(root, headers={
            'Accept-Encoding': encoding, 'If-Modified-Since': html_headers['last-modified'],
            'If-None-Match': html_headers['etag'],
        })
        if headers.get('content-encoding') == 'gzip':
            body = gzip.decompress(body)
        if encoding:
            assert headers['content-encoding'] == 'gzip'
        assert status == 200 and content_version(changed).encode() in body
        assert content_version(javascript).encode() not in body
    status, headers, body = request(root, '/js/app.js', {
        'If-None-Match': js_headers['etag'], 'If-Modified-Since': js_headers['last-modified'],
    })
    assert status == 200 and body == changed and headers['etag'] != js_headers['etag']
    old_url = '/js/app.js?v=' + content_version(javascript)
    assert request(root, old_url)[1]['cache-control'] == 'no-cache'


@pytest.mark.parametrize('asset', ['js/app.js', 'css/app.css'])
@pytest.mark.parametrize('version', ['current', 'old', 'none', 'duplicate'])
@pytest.mark.parametrize('head', [False, True])
def test_only_current_content_version_is_immutable(frontend, asset, version, head):
    root = frontend[0]
    raw = (root / asset).read_bytes()
    digest = content_version(raw)
    query = {'current': '?v=' + digest, 'old': '?v=old', 'none': '',
             'duplicate': '?v=' + digest + '&v=old'}[version]
    status, headers, body = request(root, '/' + asset + query, head=head)
    assert status == 200
    assert headers['cache-control'] == ('public, max-age=31536000, immutable' if version == 'current' else 'no-cache')
    assert body == (b'' if head else raw)
    assert int(headers['content-length']) == len(raw)


@pytest.mark.parametrize('head', [False, True])
@pytest.mark.parametrize('encoding', ['', 'gzip'])
def test_exact_etag_304_and_representation_negotiation(frontend, head, encoding):
    root, javascript, _, _ = frontend
    url = '/js/app.js?v=' + content_version(javascript)
    _, first, _ = request(root, url, {'Accept-Encoding': encoding})
    status, headers, body = request(root, url, {
        'Accept-Encoding': encoding, 'If-None-Match': '"unrelated", W/' + first['etag'],
    }, head=head)
    assert status == 304 and body == b''
    assert headers['etag'] == first['etag']
    assert headers['cache-control'] == first['cache-control']
    assert headers['vary'] == 'Accept-Encoding'
    assert 'content-length' not in headers
    _, identity, _ = request(root, url)
    _, compressed, compressed_body = request(root, url, {'Accept-Encoding': 'gzip'})
    assert compressed['etag'] != identity['etag']
    assert gzip.decompress(compressed_body) == javascript
    assert request(root, url, {'Accept-Encoding': 'gzip', 'If-None-Match': identity['etag']})[0] == 200


def test_ims_alone_cannot_hide_byte_changes(frontend):
    root = frontend[0]
    _, first, _ = request(root, '/js/app.js')
    assert request(root, '/js/app.js', {'If-Modified-Since': first['last-modified']})[0] == 200


@pytest.mark.parametrize('head', [False, True])
def test_rewritten_html_gzip_head_and_range_match_get(frontend, head):
    root = frontend[0]
    _, identity, raw = request(root)
    status, headers, body = request(root, headers={'Accept-Encoding': 'gzip'}, head=head)
    assert status == 200 and headers['cache-control'] == 'no-store'
    assert headers['content-encoding'] == 'gzip'
    _, get_headers, get_body = request(root, headers={'Accept-Encoding': 'gzip'})
    assert headers == get_headers
    assert body == (b'' if head else get_body)
    assert int(headers['content-length']) == len(get_body)
    assert (gzip.decompress(get_body) if headers.get('content-encoding') else get_body) == raw
    status, headers, body = request(root, headers={'Accept-Encoding': 'gzip', 'Range': 'bytes=20-90'}, head=head)
    assert status == 206 and body == (b'' if head else raw[20:91])
    assert headers['content-range'] == f'bytes 20-90/{len(raw)}'
    assert headers['etag'] == identity['etag']
    assert headers['cache-control'] == 'no-store'
    assert 'content-encoding' not in headers


def test_versioned_asset_range_and_errors_keep_correct_cache_policy(frontend):
    root, javascript, _, _ = frontend
    url = '/js/app.js?v=' + content_version(javascript)
    _, identity, _ = request(root, url)
    status, headers, body = request(root, url, {
        'Accept-Encoding': 'gzip', 'Range': 'bytes=-20', 'If-None-Match': identity['etag'],
    })
    assert status == 206 and body == javascript[-20:]
    assert headers['cache-control'].endswith('immutable')
    assert headers['etag'] == identity['etag']
    assert 'content-encoding' not in headers
    status, headers, body = request(root, url, {'Range': 'bytes=999999-'})
    assert status == 416 and body == b''
    assert headers['cache-control'] == 'no-store' and headers['content-length'] == '0'
    status, headers, _ = request(root, '/js/missing.js?v=' + content_version(javascript))
    assert status == 404 and headers['cache-control'] == 'no-store'


def test_private_output_and_api_paths_never_gain_immutable_cache(frontend):
    root, javascript, _, _ = frontend
    (root / 'outputs').mkdir()
    (root / 'outputs' / 'generated.js').write_bytes(javascript)
    (root / 'shared').mkdir()
    (root / 'shared' / 'private.js').write_bytes(javascript)
    for path, policy in (('/outputs/generated.js', 'no-cache'), ('/shared/private.js', 'no-store')):
        status, headers, _ = request(root, path + '?v=' + content_version(javascript))
        assert status == 200 and headers['cache-control'] == policy
    handler, _, headers, _ = _make_handler('/api/item.js?v=' + content_version(javascript))
    handler._spark_static_cache_control = 'public, max-age=31536000, immutable'
    handler._send_json({'private': 'value'})
    assert headers['cache-control'] == 'no-store'


def test_external_missing_blocked_and_out_of_root_references_remain_unchanged(frontend, tmp_path):
    root, javascript, _, _ = frontend
    outside = tmp_path.parent / (tmp_path.name + '-outside.js')
    outside.write_bytes(b'outside bytes')
    (root / 'js/outside.js').symlink_to(outside)
    markup = b'''<script src="https://elsewhere.test/js/app.js?v=external"></script>
<script src="//elsewhere.test/js/app.js?v=protocol"></script>
<script src="data:text/javascript,alert(1)"></script>
<script src="/js/missing.js?v=missing"></script>
<script src="/js/outside.js?v=outside"></script>
<script src="/js/%2e%2e/server_config.js?v=blocked"></script>
<script src="/outputs/generated.js?v=output"></script>
<script src="http://spark.test/js/app.js?v=local"></script>'''
    (root / 'index.html').write_bytes(markup)
    _, _, body = request(root)
    for key in (b'external', b'protocol', b'missing', b'outside', b'blocked', b'output'):
        assert b'v=' + key in body
    assert b'data:text/javascript,alert(1)' in body
    assert ('http://spark.test/js/app.js?v=' + content_version(javascript)).encode() in body
    assert request(root, '/js/%2e%2e/server_config.js')[0] == 404


def test_relative_assets_follow_html_base(frontend):
    root, javascript, _, _ = frontend
    (root / 'index.html').write_bytes(b'<base href="/js/"><script src="app.js?v=old"></script>')
    assert content_version(javascript).encode() in request(root)[2]
    (root / 'index.html').write_bytes(b'<base href="https://elsewhere.test/js/"><script src="app.js?v=old"></script>')
    assert b'src="app.js?v=old"' in request(root)[2]


@pytest.mark.parametrize('separator', ['\r', '\u2028', '\r\n'])
def test_html_attribute_offsets_follow_lf_lines(frontend, separator):
    root, javascript, _, _ = frontend
    markup = ('<html>' + separator + '<head>\n<script src="/js/app.js?v=old"></script>').encode()
    (root / 'index.html').write_bytes(markup)
    expected = markup.replace(b'/js/app.js?v=old', ('/js/app.js?v=' + content_version(javascript)).encode())
    assert request(root)[2] == expected


def test_encoded_text_extensions_still_have_cache_policy(frontend):
    root, javascript, _, _ = frontend
    assert request(root, '/index%2Ehtml')[1]['cache-control'] == 'no-store'
    assert request(root, '/js/app%2Ejs')[1]['cache-control'] == 'no-cache'
    assert request(root, '/js/app%2Ejs?v=' + content_version(javascript))[1]['cache-control'].endswith('immutable')


def test_reused_handler_does_not_leak_asset_policy(frontend):
    root, javascript, _, _ = frontend
    (root / 'outputs').mkdir()
    (root / 'outputs' / 'cover.png').write_bytes(b'completed image')
    handler, statuses, headers, body = _make_handler('/js/app.js?v=' + content_version(javascript))
    handler.directory = str(root)
    cache_headers = []
    original_send = handler.send_header
    def track_header(name, value):
        if name.lower() == 'cache-control':
            cache_headers.append(value)
        original_send(name, value)
    handler.send_header = track_header
    for path, policy in ((handler.path, 'public, max-age=31536000, immutable'),
                         ('/index.html', 'no-store'), ('/outputs/cover.png', 'no-cache'),
                         ('/js/app.js', 'no-cache'), ('/js/missing.js', 'no-store')):
        handler.path = path
        cache_headers.clear()
        headers.clear()
        body.seek(0)
        body.truncate()
        handler.do_GET()
        assert headers['cache-control'] == policy
        assert cache_headers == [policy], 'Every response sends one cache policy'
