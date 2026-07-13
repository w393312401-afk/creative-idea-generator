"""generate_social_titles：发布用双语标题行（TikTok 英文整行 / 国内社媒中文整行）。

契约：
- 单次 aux 调用返回严格 JSON {"tiktok", "cn"}，两值都被清洗成可原样粘贴的单行；
- 任何失败（网络、非 JSON、非 dict）都返回 {'tiktok': '', 'cn': ''}，绝不抛异常——
  它挂在激发收尾，失败不允许拖垮出单。
"""
import json
from unittest import mock

import pytest

import prompt_pipeline
from prompt_pipeline import generate_social_titles, _sanitize_social_line


def _call_with_response(response, title='沼泽坠落客机改造', theme='机舱改造'):
    with mock.patch.object(prompt_pipeline, '_chat', return_value=response) as m:
        result = generate_social_titles({}, title, theme)
    return result, m


def test_clean_json_passthrough():
    resp = json.dumps({
        'tiktok': 'Swamp Plane Cabin Rescue #Restoration #OffGridLiving #BeforeAndAfter #DIYBuild',
        'cn': '沼泽客机爆改避世小屋 #旧物改造 #爆改 #解压 #治愈系',
    }, ensure_ascii=False)
    result, m = _call_with_response(resp)
    assert result['tiktok'] == 'Swamp Plane Cabin Rescue #Restoration #OffGridLiving #BeforeAndAfter #DIYBuild'
    assert result['cn'] == '沼泽客机爆改避世小屋 #旧物改造 #爆改 #解压 #治愈系'
    assert m.call_count == 1


def test_fenced_json_is_parsed():
    resp = '```json\n{"tiktok": "Epic Cabin Build #DIY", "cn": "爆改小屋 #改造"}\n```'
    result, _ = _call_with_response(resp)
    assert result == {'tiktok': 'Epic Cabin Build #DIY', 'cn': '爆改小屋 #改造'}


def test_values_collapse_to_single_line_and_strip_labels():
    resp = json.dumps({
        'tiktok': '  "Title: Epic Build\n#DIY  #Cabin"  ',
        'cn': '标题：爆改小屋\n#改造 #解压',
    }, ensure_ascii=False)
    result, _ = _call_with_response(resp)
    assert '\n' not in result['tiktok'] and '"' not in result['tiktok']
    assert result['tiktok'] == 'Epic Build #DIY #Cabin'
    assert result['cn'] == '爆改小屋 #改造 #解压'


def test_chat_exception_returns_empty_not_raises():
    with mock.patch.object(prompt_pipeline, '_chat', side_effect=RuntimeError('proxy down')):
        result = generate_social_titles({}, '标题', '主题')
    assert result == {'tiktok': '', 'cn': ''}


def test_non_json_and_non_dict_return_empty():
    for bad in ['not json at all', '["a", "b"]', '42']:
        result, _ = _call_with_response(bad)
        assert result == {'tiktok': '', 'cn': ''}, bad


def test_missing_keys_default_empty():
    result, _ = _call_with_response('{"tiktok": "Only English #Tag"}')
    assert result['tiktok'] == 'Only English #Tag'
    assert result['cn'] == ''


def test_blank_title_short_circuits_without_llm_call():
    with mock.patch.object(prompt_pipeline, '_chat') as m:
        result = generate_social_titles({}, '   ', '主题')
    assert result == {'tiktok': '', 'cn': ''}
    m.assert_not_called()


def test_sanitize_caps_length():
    assert len(_sanitize_social_line('x' * 999)) == 250
    assert _sanitize_social_line(None) == ''


# ---------------------------------------------------------------------------
# 激发收尾接线：background_worker 完成时必须把两行发布标题挂到 result 上
# ---------------------------------------------------------------------------

_COMPOSE_CONTENT = """===TITLE===
测试改造标题
===THEME===
测试主题
===PROMPTS===
图片 1: a test image prompt
===AUDIT===
PASS — 无违规
"""


@pytest.fixture
def _isolated_tasks(tmp_path, monkeypatch):
    import server_common
    monkeypatch.chdir(tmp_path)
    import os
    os.makedirs("tasks", exist_ok=True)
    monkeypatch.setattr(server_common, "ACTIVE_TASKS", {})


def _run_background_worker(monkeypatch, social_return):
    import server
    calls = {}

    def fake_social(config, title, theme=''):
        calls['args'] = (title, theme)
        return social_return

    monkeypatch.setattr(server, 'call_llm', lambda config, dimensions, on_progress=None: _COMPOSE_CONTENT)
    monkeypatch.setattr(prompt_pipeline, 'generate_social_titles', fake_social)
    server.background_worker('900001', {}, {'theme': '机舱改造', 'creativity': '大胆'})
    import server_common
    return server_common.ACTIVE_TASKS['900001'], calls


def test_background_worker_attaches_social_titles(monkeypatch, _isolated_tasks):
    t, calls = _run_background_worker(monkeypatch, {
        'tiktok': 'Epic Cabin Build #DIY #BeforeAndAfter',
        'cn': '爆改机舱 #改造 #解压',
    })
    assert t['status'] == 'completed'
    assert t['result']['social_title_en'] == 'Epic Cabin Build #DIY #BeforeAndAfter'
    assert t['result']['social_title_cn'] == '爆改机舱 #改造 #解压'
    # 用的是解析出的中文标题和维度里的主题
    assert calls['args'] == ('测试改造标题', '机舱改造')


def test_background_worker_omits_fields_when_generation_empty(monkeypatch, _isolated_tasks):
    t, _ = _run_background_worker(monkeypatch, {'tiktok': '', 'cn': ''})
    assert t['status'] == 'completed'
    assert 'social_title_en' not in t['result']
    assert 'social_title_cn' not in t['result']
