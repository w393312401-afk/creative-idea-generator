"""generate_project_meta：一键补齐一单的中英双版主题 + 发布用话题行。

与 generate_social_titles 的差别就是这个模块要守的契约：
- 依据是**提示词集正文**（手动上传的单子 title/theme 往往只是个文件名，
  光凭它推出来的全是套话），所以摘要必须真的进 prompt；
- 摘要只取首/中/末三拍，整份集子不进 prompt（上万字、又贵又冲散注意力）；
- 任何失败都返回四个空串，绝不抛异常——它是补充信息，不允许拖垮调用方。
"""
import json
from unittest import mock

import prompt_pipeline
from prompt_pipeline import generate_project_meta, _prompt_block_digest


_BLOCK = """图片提示词

图片 1:
A rotted swamp cabin, collapsed roof, knee-deep mud.

图片 2:
Frame skeleton going up, fresh timber posts.

图片 3:
Walls sheathed, roof deck on.

图片 4:
Finished glass cabin glowing over the water at dusk.

视频提示词

视频 1:
Slow push in over the mud.
"""

_GOOD = json.dumps({
    'theme_cn': '沼泽废弃木屋爆改玻璃水上小屋',
    'theme_en': 'Swamp Cabin Rebuilt Into Glass Retreat',
    'tiktok': 'Swamp Cabin Glass Rebuild #Restoration #OffGridLiving #BeforeAndAfter #DIYBuild',
    'cn': '沼泽废屋爆改玻璃小屋 #旧物改造 #爆改 #解压 #治愈系',
}, ensure_ascii=False)


def _call(response, **kwargs):
    kwargs.setdefault('title', '导入的提示词集')
    kwargs.setdefault('prompt_block', _BLOCK)
    with mock.patch.object(prompt_pipeline, '_chat', return_value=response) as m:
        result = generate_project_meta({}, **kwargs)
    return result, m


def test_all_four_fields_pass_through():
    result, m = _call(_GOOD)
    assert result['theme_cn'] == '沼泽废弃木屋爆改玻璃水上小屋'
    assert result['theme_en'] == 'Swamp Cabin Rebuilt Into Glass Retreat'
    assert result['tiktok'].startswith('Swamp Cabin Glass Rebuild #Restoration')
    assert result['cn'].startswith('沼泽废屋爆改玻璃小屋 #旧物改造')
    assert m.call_count == 1


def test_prompt_carries_first_and_last_beat_not_the_whole_block():
    """首尾拍必须进 prompt（"从什么样变成什么样"就靠它俩），中间那些不进。"""
    _, m = _call(_GOOD)
    user_prompt = m.call_args[0][2]
    assert 'rotted swamp cabin' in user_prompt.lower()
    assert 'finished glass cabin' in user_prompt.lower()
    assert 'Frame skeleton' not in user_prompt          # 4 拍时只取首/中/末，第 2 拍不在其中
    assert 'Slow push in over the mud' not in user_prompt   # 视频拍不进摘要


def test_fenced_json_is_parsed():
    result, _ = _call('```json\n' + _GOOD + '\n```')
    assert result['theme_en'] == 'Swamp Cabin Rebuilt Into Glass Retreat'


def test_values_collapse_to_single_line_and_strip_labels():
    resp = json.dumps({
        'theme_cn': '主题：沼泽\n废屋改造',
        'theme_en': '  "Swamp Cabin\nRebuild"  ',
        'tiktok': 'Title: Epic Build\n#DIY',
        'cn': '标题：爆改小屋\n#改造',
    }, ensure_ascii=False)
    result, _ = _call(resp)
    assert result['theme_cn'] == '沼泽 废屋改造'
    assert result['theme_en'] == 'Swamp Cabin Rebuild'
    assert result['tiktok'] == 'Epic Build #DIY'
    assert result['cn'] == '爆改小屋 #改造'


def test_theme_fields_are_length_capped():
    result, _ = _call(json.dumps({'theme_cn': '改' * 500, 'theme_en': 'x' * 500}))
    assert len(result['theme_cn']) == 60
    assert len(result['theme_en']) == 80


def test_failures_return_four_empty_strings_not_raise():
    empty = {'theme_cn': '', 'theme_en': '', 'tiktok': '', 'cn': ''}
    for bad in ['not json at all', '["a", "b"]', '42']:
        result, _ = _call(bad)
        assert result == empty, bad
    with mock.patch.object(prompt_pipeline, '_chat', side_effect=RuntimeError('proxy down')):
        assert generate_project_meta({}, '标题', '主题', _BLOCK) == empty


def test_missing_keys_default_empty():
    result, _ = _call('{"tiktok": "Only English #Tag"}')
    assert result['tiktok'] == 'Only English #Tag'
    assert result['theme_cn'] == '' and result['theme_en'] == '' and result['cn'] == ''


def test_no_block_and_no_title_short_circuits_without_llm_call():
    with mock.patch.object(prompt_pipeline, '_chat') as m:
        result = generate_project_meta({}, '  ', '', '   ')
    assert result == {'theme_cn': '', 'theme_en': '', 'tiktok': '', 'cn': ''}
    m.assert_not_called()


def test_title_only_still_calls_the_model():
    """提示词集为空但有标题时照样推——总比工作台上那行完全没主题强。"""
    _, m = _call(_GOOD, prompt_block='', title='沼泽坠机残骸改造')
    assert m.call_count == 1
    assert '沼泽坠机残骸改造' in m.call_args[0][2]


# ── 摘要本身 ───────────────────────────────────────────────────────────────

def test_digest_picks_first_middle_last_and_counts_beats():
    digest = _prompt_block_digest(_BLOCK)
    assert 'Total beats: 4' in digest
    assert 'FIRST BEAT' in digest and 'MIDDLE BEAT' in digest and 'LAST BEAT' in digest


def test_digest_of_single_beat_has_no_middle_or_last():
    digest = _prompt_block_digest('图片 1:\nOnly one beat.\n')
    assert 'FIRST BEAT' in digest
    assert 'MIDDLE BEAT' not in digest and 'LAST BEAT' not in digest


def test_digest_truncates_each_slot():
    long_block = '图片 1:\n' + 'a' * 5000 + '\n'
    digest = _prompt_block_digest(long_block, per_slot=100)
    assert 'a' * 100 in digest
    assert 'a' * 101 not in digest


def test_digest_falls_back_to_head_truncation_when_no_slots_parse():
    digest = _prompt_block_digest('just a loose paragraph with no slot labels', per_slot=10)
    assert digest.startswith('just a loose')
    assert len(digest) <= 30


def test_digest_of_blank_is_empty():
    assert _prompt_block_digest('') == ''
    assert _prompt_block_digest(None) == ''
