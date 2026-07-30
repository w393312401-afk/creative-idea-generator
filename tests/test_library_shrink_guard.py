"""创意库「缩量覆盖」闸门（server_common.library_shrink_verdict）的行为契约。

背景：/api/library 的契约是"客户端始终持有完整数组、整份 POST 回来覆盖"，因此任何
一次状态错乱的写入都是一次静默的数据丢失。既有两道防护只堵住了两头——空列表覆盖非空
（2026-07-12 整库清零）、同一条创意内部 prompt_slots 与 frameRun 数量不自洽——中间
那一大片"非空 → 非空，但少了一条创意 / 某条少了几帧"没人管，2026-07-27 自动化测试
驱动真实页面把真库整份覆盖就是从这个缺口掉下去的。

这道闸门不禁止缩量，只要求**声明**缩量：合法缩量（删除单条创意、删除某一拍之后按
服务端 manifest 回写）都知道自己在删什么，声明与实际差异一致即放行；没声明的缩量
一律 409，让页面刷新后重来。
"""
import pytest

from server_common import library_shrink_verdict, parse_library_payload


def _idea(ident, title='某个创意', frames=None):
    idea = {'id': ident, 'title': title}
    if frames is not None:
        idea['frameRun'] = {'frames': [{'sequence': i + 1} for i in range(frames)]}
    return idea


class TestUndeclaredShrink:
    def test_missing_idea_is_rejected(self):
        old = [_idea('a'), _idea('b', '冰川洞穴卧室')]
        new = [_idea('a')]
        ok, msg, detail = library_shrink_verdict(old, new, {})
        assert ok is False
        assert detail['removed_ids'] == ['b']
        # 提示里要点名到底是哪条会消失，否则用户无法判断该不该刷新
        assert '冰川洞穴卧室' in msg

    def test_frame_count_drop_is_rejected(self):
        old = [_idea('a', frames=10)]
        new = [_idea('a', frames=9)]
        ok, _, detail = library_shrink_verdict(old, new, {})
        assert ok is False
        assert detail['frame_shrunk'] == [
            {'id': 'a', 'title': '某个创意', 'before': 10, 'after': 9}]

    def test_dropping_whole_framerun_is_rejected(self):
        """frameRun 整体消失＝该条创意的帧记录从 N 掉到 0，与少几帧同一类。"""
        old = [_idea('a', frames=10)]
        new = [_idea('a')]
        ok, _, detail = library_shrink_verdict(old, new, {})
        assert ok is False
        assert detail['frame_shrunk'][0]['after'] == 0

    def test_anonymous_records_fall_back_to_count(self):
        """无 id 的历史记录没有身份可比对，只能按条数兜底判定。"""
        old = [{'title': '无 id 甲'}, {'title': '无 id 乙'}]
        new = [{'title': '无 id 甲'}]
        ok, _, detail = library_shrink_verdict(old, new, {})
        assert ok is False
        assert detail['count_lost'] == 1

    def test_duplicate_ids_are_caught_by_the_count_backstop(self):
        """两条同 id 的记录在索引里折叠成一条，删掉其中一条按身份比对完全看不出来
        ——条数兜底就是为这类漏网补的。"""
        old = [_idea('a', '甲'), _idea('a', '乙')]
        new = [_idea('a', '甲')]
        ok, _, detail = library_shrink_verdict(old, new, {})
        assert ok is False
        assert detail['count_lost'] == 1

    def test_removed_ids_are_not_double_counted(self):
        """一条按身份认出来的消失，不该在条数兜底里再报一遍。"""
        old = [_idea('a'), _idea('b')]
        new = [_idea('a')]
        ok, _, detail = library_shrink_verdict(old, new, {})
        assert ok is False
        assert detail['removed_ids'] == ['b']
        assert detail['count_lost'] == 0


class TestDeclaredShrinkPasses:
    def test_declared_removal(self):
        old = [_idea('a'), _idea('b')]
        new = [_idea('a')]
        ok, msg, _ = library_shrink_verdict(old, new, {'removed_ids': ['b']})
        assert ok is True and msg is None

    def test_declared_frame_shrink(self):
        old = [_idea('a', frames=10)]
        new = [_idea('a', frames=9)]
        ok, _, _ = library_shrink_verdict(old, new, {'frame_shrink_ids': ['a']})
        assert ok is True

    def test_declaration_is_scoped_to_the_named_idea(self):
        """声明只豁免点名的那一条：同一次写入里别的创意少了帧，照样拦。"""
        old = [_idea('a', frames=10), _idea('b', frames=10)]
        new = [_idea('a', frames=9), _idea('b', frames=4)]
        ok, _, detail = library_shrink_verdict(old, new, {'frame_shrink_ids': ['a']})
        assert ok is False
        assert [f['id'] for f in detail['frame_shrunk']] == ['b']

    def test_over_declaring_is_not_an_error(self):
        """连点两次删除时，第二次那条 id 已经不在库里了——声明多了不该拦。"""
        old = [_idea('a')]
        new = [_idea('a')]
        ok, _, _ = library_shrink_verdict(old, new, {'removed_ids': ['b', 'c']})
        assert ok is True

    def test_declaring_an_unrelated_id_does_not_excuse_a_count_drop(self):
        """声明必须对得上实际消失的那一条。随便声明一个 id 就把条数兜底整个关掉，
        等于给状态错乱开了后门——真正丢的是那条无 id 的老记录，它照样得被拦下。

        无 id 的记录本来也删不干净：deleteFromLibrary 按 `item.id !== id` 过滤，
        id 为 undefined 时会把**所有**无 id 记录一起滤掉。这道拦截正好挡住那次误删。"""
        old = [_idea('a'), {'title': '无 id 的老记录'}]
        new = [_idea('a')]
        ok, _, detail = library_shrink_verdict(old, new, {'removed_ids': ['whatever']})
        assert ok is False
        assert detail['count_lost'] == 1

    def test_declared_removal_accounts_for_exactly_one_row(self):
        """声明的 id 确实消失了 → 它解释掉一条，其余条数必须照样对得上。"""
        old = [_idea('a'), _idea('b'), {'title': '无 id 的老记录'}]
        new = [_idea('a')]
        ok, _, detail = library_shrink_verdict(old, new, {'removed_ids': ['b']})
        assert ok is False          # 无 id 那条仍然是无端消失的
        assert detail['removed_ids'] == []
        assert detail['count_lost'] == 1


class TestNonShrinkingWrites:
    def test_growth_and_edits_pass(self):
        old = [_idea('a', frames=3)]
        new = [_idea('a', '改了标题', frames=5), _idea('b')]
        ok, _, _ = library_shrink_verdict(old, new, {})
        assert ok is True

    def test_identical_payload_passes(self):
        old = [_idea('a', frames=3)]
        ok, _, _ = library_shrink_verdict(old, list(old), {})
        assert ok is True

    def test_unreadable_existing_library_does_not_block(self):
        """磁盘上那份读不出来（None / 非数组）时无从比对，放行——空列表覆盖非空
        那道防护在上游已按"非空"保守处理过。"""
        ok, _, _ = library_shrink_verdict(None, [_idea('a')], {})
        assert ok is True

    def test_missing_intent_is_same_as_empty(self):
        old = [_idea('a')]
        ok, _, _ = library_shrink_verdict(old, list(old), None)
        assert ok is True


class TestPayloadParsing:
    def test_bare_array_is_legacy_contract(self):
        ideas, intent = parse_library_payload([_idea('a')])
        assert len(ideas) == 1 and intent == {}

    def test_envelope_carries_intent(self):
        ideas, intent = parse_library_payload(
            {'ideas': [_idea('a')], 'intent': {'removed_ids': ['b']}})
        assert len(ideas) == 1
        assert intent == {'removed_ids': ['b']}

    def test_envelope_without_valid_ideas_is_rejected(self):
        ideas, intent = parse_library_payload({'ideas': 'nope'})
        assert ideas is None and intent == {}

    def test_envelope_intent_may_be_absent(self):
        ideas, intent = parse_library_payload({'ideas': []})
        assert ideas == [] and intent == {}


def test_ids_are_compared_as_strings():
    """前端 id 是 `Date.now() + Math.random()` 拼出来的字符串，但导入的老库里
    可能是数字——两边类型不同不该被当成"这条消失了"。"""
    old = [{'id': 123, 'title': '数字 id'}]
    new = [{'id': '123', 'title': '数字 id'}]
    ok, _, _ = library_shrink_verdict(old, new, {})
    assert ok is True
