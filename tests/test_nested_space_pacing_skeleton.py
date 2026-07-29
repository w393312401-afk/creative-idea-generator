import prompt_pipeline as pp


GOOD_NESTED_OUTLINE = [
    '吊装并隐蔽废弃载体',
    '清空第一功能区残留物',
    '铺设第一空间防潮膜',
    '架设第一空间木龙骨',
    '封装第一空间内衬板',
    '备齐储备厨房完成使用',
    '硬切进入第二毛坯舱室',
    '清空第二舱室碎屑',
    '铺设防潮膜与电路',
    '架设墙顶木龙骨',
    '封装保温内衬面板',
    '点亮卧室全景,人物入住',
]


def test_nested_space_reference_is_registered_and_normalized():
    assert pp.PACING_SKELETONS['nested_space_payoff']['label_zh'] == '双空间重置兑现'
    assert pp.normalize_pacing_skeleton_ids(['nested_space_payoff']) == ['nested_space_payoff']


def test_nested_space_reference_declares_one_hard_cut_chain_reset():
    brief = pp.apply_pacing_skeleton_to_brief({'mode': 'Standard'}, 'nested_space_payoff')
    assert brief['mode'] == 'Threshold'
    assert brief['threshold_variant'] == 'hard_cut'
    assert brief['require_visible_threshold_video'] is False


def test_nested_space_outline_passes_with_two_complete_functional_arcs():
    idea = {'pacing_skeleton': 'nested_space_payoff', 'beat_outline': GOOD_NESTED_OUTLINE}
    assert pp.outline_skeleton_violations(idea) == []
    assert pp.pacing_skeleton_outline_violations(idea) == []
    assert pp.compute_beats_floor(idea) == 9


def test_nested_space_outline_rejects_a_partial_first_space_payoff():
    outline = list(GOOD_NESTED_OUTLINE)
    outline[5] = '继续安装第一空间墙板'
    errors = pp.pacing_skeleton_outline_violations(
        {'pacing_skeleton': 'nested_space_payoff', 'beat_outline': outline})
    assert any('primary space function' in error for error in errors)


def test_nested_space_outline_requires_exactly_one_raw_second_space_reset():
    outline = list(GOOD_NESTED_OUTLINE)
    outline[6] = '继续完善室内布局'
    errors = pp.pacing_skeleton_outline_violations(
        {'pacing_skeleton': 'nested_space_payoff', 'beat_outline': outline})
    assert any('exactly one declared reset' in error for error in errors)


def test_nested_only_static_fallbacks_keep_an_honest_nested_outline(monkeypatch):
    monkeypatch.setattr(pp, 'read_ledger', lambda: [])
    monkeypatch.setattr(pp, 'fetch_trend_snippet', lambda *args, **kwargs: '')
    monkeypatch.setattr(pp, 'fetch_custom_url_snippet', lambda *args, **kwargs: '')

    def fail_chat(*args, **kwargs):
        raise RuntimeError('offline test')

    monkeypatch.setattr(pp, '_chat', fail_chat)
    result = pp.run_ideate({}, count=3, pacing_skeleton_ids=['nested_space_payoff'])
    assert len(result['ideas']) == 3
    for idea in result['ideas']:
        assert idea['pacing_skeleton'] == 'nested_space_payoff'
        assert pp.pacing_skeleton_outline_violations(idea) == []
