"""4选1 模式的归属：模式是「这一单的属性」，不是「这个浏览器的属性」。

事故口径：帧序列跑到一半刷新页面（或换浏览器/换设备）后再继续生成，每帧渲完
不做 AI 鉴别就直接下一帧——因为前端的 checkbox 与 localStorage 都空了，请求体里
发来一个只是默认值的 'standard'，旧口径把它当成用户明确要求，把一单 4选1 的活
降级成单图直出。这里锁住 server_common.resolve_candidate_selection_mode 的判定：
只有带着 generation_mode_explicit 的 'standard' 才压得过项目 manifest 的记录。
"""
import pytest

from server_common import resolve_candidate_selection_mode


CAND = {'generation_mode': 'candidate_selection'}
STD = {'generation_mode': 'standard'}


def _browser_default_standard():
    """刷新后前端的样子：开关是页面默认的关，且没表过态。"""
    return {'generation_mode': 'standard', 'candidate_selection': False}


def test_refresh_does_not_downgrade_a_candidate_project():
    """刷新页面后续跑：项目记着自己是 4选1，前端的默认 standard 压不过它。"""
    assert resolve_candidate_selection_mode(
        _browser_default_standard(), STD, CAND, default=False) is True


def test_explicit_standard_wins_over_manifest():
    """用户真把开关拨到关：以用户为准，哪怕这单历史上是 4选1。"""
    body = dict(_browser_default_standard(), generation_mode_explicit=True)
    assert resolve_candidate_selection_mode(body, STD, CAND, default=False) is False


def test_explicit_candidate_entry_needs_no_manifest_backing():
    """点「🎯 4选1 智能生成」：开启信号直接算数，不必项目背书。"""
    body = {'generation_mode': 'candidate_selection', 'candidate_selection': True,
            'generation_mode_explicit': True}
    assert resolve_candidate_selection_mode(body, {}, STD, default=False) is True


def test_standard_project_stays_standard():
    """项目本来就是标准模式：不会被兜底逻辑意外升级成 4选1。"""
    assert resolve_candidate_selection_mode(
        _browser_default_standard(), {}, STD, default=False) is False


@pytest.mark.parametrize('manifest', [None, {}, {'generation_mode': ''}])
def test_no_project_record_falls_back_to_request(manifest):
    """新项目/老项目没有模式记录：回落到请求里的值，与改动前一致。"""
    assert resolve_candidate_selection_mode(
        _browser_default_standard(), {}, manifest, default=False) is False


def test_fix_line_keeps_its_candidate_default():
    """修复线（/api/fix_frame_issue）的历史默认是 4选1，无信号时不变。"""
    assert resolve_candidate_selection_mode({}, {}, {}, default=True) is True


def test_fix_line_follows_manifest_after_refresh():
    """修复线同样吃 manifest 兜底：刷新后修某一帧，仍走 4选1 候选优选。"""
    assert resolve_candidate_selection_mode(
        _browser_default_standard(), STD, CAND, default=True) is True


def test_any_standard_vote_within_one_source_wins():
    """同一来源里多个键表态冲突时，沿用旧口径：任一处说 standard 就是 standard。"""
    body = {'generation_mode': 'candidate_selection', 'candidate_selection': False,
            'generation_mode_explicit': True}
    assert resolve_candidate_selection_mode(body, {}, CAND, default=False) is False
