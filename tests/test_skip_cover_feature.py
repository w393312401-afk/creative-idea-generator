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
