import pytest
import os
import server_common
import frame_generator

def test_skip_cover_resolve_cover_reference():
    # 1. 当显式设置 skipCoverReference=True 或 coverReferencePath='none' 时，resolve_cover_reference 必须返回 None
    assert server_common.resolve_cover_reference({'skipCoverReference': True}, 'test_project') is None
    assert server_common.resolve_cover_reference({'coverReferencePath': 'none'}, 'test_project') is None
    assert server_common.resolve_cover_reference({'allowTextOnlyAnchor': True}, 'test_project') is None

def test_text_only_anchor_allowed():
    # 2. 检查 frame_generator 中的 text_only_anchor_allowed
    assert frame_generator.text_only_anchor_allowed({'skipCoverReference': True}) is True
    assert frame_generator.text_only_anchor_allowed({'coverReferencePath': 'none'}) is True
    assert frame_generator.text_only_anchor_allowed({'allowTextOnlyAnchor': True}) is True
    assert frame_generator.text_only_anchor_allowed({}) is False

def test_index_html_has_skip_cover_toggle():
    html_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'index.html')
    with open(html_path, 'r', encoding='utf-8') as f:
        html = f.read()
    assert 'id="frames-skip-cover-toggle"' in html, "index.html 必须包含 frames-skip-cover-toggle"
    assert '第一帧纯文生图' in html

def test_media_renderer_supports_none_role():
    js_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'js', 'media_renderer.js')
    with open(js_path, 'r', encoding='utf-8') as f:
        js = f.read()
    assert "roles[role] === 'none'" in js
    assert "不使用（纯文生图）" in js


def test_index_html_settings_pop_default_open():
    html_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'index.html')
    with open(html_path, 'r', encoding='utf-8') as f:
        html = f.read()
    # 确保设置弹层默认展开（不带 hidden 属性），且按钮带有 active 样式
    assert '<div class="section-pop" id="frames-settings-pop">' in html
    assert 'data-pop="frames-settings-pop"' in html
    assert 'class="section-tool-btn active" data-pop="frames-settings-pop"' in html
    assert '<div class="section-pop" id="videos-settings-pop">' in html
    assert 'class="section-tool-btn active" data-pop="videos-settings-pop"' in html


def test_http_url_cover_candidate_path(tmp_path, monkeypatch):
    out = tmp_path / 'outputs'
    out.mkdir()
    p_dir = out / 'my_project'
    p_dir.mkdir()
    cover_file = p_dir / 'cover_001.webp'
    cover_file.write_bytes(b'dummy image content')

    monkeypatch.setattr(server_common, 'OUTPUT_ROOT', str(out))
    
    # 1. 相对路径
    cand = server_common._cover_candidate_path('/outputs/my_project/cover_001.webp')
    assert cand is not None and os.path.samefile(cand, str(cover_file))

    # 2. 带协议与端口的完整 HTTP / HTTPS URL
    cand_http = server_common._cover_candidate_path('http://127.0.0.1:8046/outputs/my_project/cover_001.webp')
    assert cand_http is not None and os.path.samefile(cand_http, str(cover_file))

    cand_https = server_common._cover_candidate_path('https://localhost:3000/outputs/my_project/cover_001.webp?t=12345')
    assert cand_https is not None and os.path.samefile(cand_https, str(cover_file))

