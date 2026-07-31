import prompt_pipeline as pp


# 这个骨架的载体必须是「人工运输载体」（集装箱/校车/大巴/机身…），所以每张卡都要带上
# carrier 一类的字段：只给 beat_outline 的卡现在会被载体门禁打回，那正是它该做的事。
GOOD_NESTED_IDEA_FIELDS = {
    'carrier': 'shipping container',
    'title': '废弃集装箱埋入山坡改造成双舱避难所',
    'dna': 'shipping-container / buried-shelter / hatch-periscope',
}

GOOD_NESTED_OUTLINE = [
    '吊车吊装集装箱入基坑',
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


def nested_idea(outline=None, **overrides):
    idea = dict(GOOD_NESTED_IDEA_FIELDS)
    idea['pacing_skeleton'] = 'nested_space_payoff'
    idea['beat_outline'] = list(outline if outline is not None else GOOD_NESTED_OUTLINE)
    idea.update(overrides)
    return idea


def test_nested_space_reference_is_registered_and_normalized():
    assert pp.PACING_SKELETONS['nested_space_payoff']['label_zh'] == '双空间重置兑现'
    assert pp.normalize_pacing_skeleton_ids(['nested_space_payoff']) == ['nested_space_payoff']


def test_nested_space_reference_preserves_the_case_rhythm_not_its_subject():
    summary = pp.PACING_SKELETONS['nested_space_payoff']['summary']
    assert 'four-act progression rhythm' in summary
    assert 'usable entrance' in summary
    assert 'second pass through the same bottom-up material ladder' in summary
    assert 'soft furnishing' in summary
    assert 'worker-free wide reveal' in summary
    assert 'never copying the school-bus' in summary


def test_nested_space_reference_demands_a_delivered_man_made_carrier():
    """节拍参考此前只说「极端放置/隐蔽钩子」，没约束载体，模型于是一路挑冰洞/竖井这类
    原地自然载体——第一拍只能是清理，开场的运输钩子整个消失。"""
    summary = pp.PACING_SKELETONS['nested_space_payoff']['summary']
    assert 'MAN-MADE TRANSPORTABLE shell' in summary
    assert 'aircraft fuselage' in summary
    assert 'crane/flatbed/excavator' in summary


def test_nested_space_declares_two_separate_discontinuities():
    """这个骨架有两处不连续，缺一不可：

    T1 进主空间 = 一镜过门（有真实过门片段）；T2 主空间完工后重置到第二毛坯空间 = 硬切。
    2026-07-31 之前 threshold_variant 直接写成 hard_cut，指望那唯一一次硬切就是 T2；
    但 hard_cut 变体的 schema 文案把它定义成「进室内的过门拍」，模型只能花在 T1 上，
    第二空间于是永远不存在（run_1785463152800：12 张图里 4~12 全是同一个空间）。
    """
    brief = pp.apply_pacing_skeleton_to_brief({'mode': 'Standard'}, 'nested_space_payoff')
    assert brief['mode'] == 'Threshold'
    assert brief['threshold_variant'] == 'coaxial'
    assert brief['require_visible_threshold_video'] is True
    assert pp.space_reset_cut_required(brief) is True
    # 上游 brief 已经判成 pan 时保留它的几何，只有 hard_cut 才被改写成 coaxial
    panned = pp.apply_pacing_skeleton_to_brief(
        {'mode': 'Standard', 'threshold_variant': 'pan_left'}, 'nested_space_payoff')
    assert panned['threshold_variant'] == 'pan_left'


def test_space_reset_cut_is_scoped_to_this_skeleton():
    """换骨架复用同一份 brief 时必须翻回 False，不能留着上一次的 True。"""
    nested = pp.apply_pacing_skeleton_to_brief({'mode': 'Standard'}, 'nested_space_payoff')
    for other in ('linear_milestone', 'dual_payoff'):
        brief = pp.apply_pacing_skeleton_to_brief(dict(nested), other)
        assert pp.space_reset_cut_required(brief) is False


def test_nested_space_outline_passes_with_two_complete_functional_arcs():
    idea = nested_idea()
    assert pp.outline_skeleton_violations(idea) == []
    assert pp.pacing_skeleton_outline_violations(idea) == []
    # 载体运输拍并入第一幕后，结构下界从 9 抬到 10（见 _NESTED_STRUCTURAL_FLOOR）
    assert pp.compute_beats_floor(idea) == 10


def test_nested_space_rejects_an_in_situ_natural_carrier():
    """用户要的是集装箱/校车/大巴/机身这类被装备运过来的壳体：冰川洞、导弹井这种
    本来就长在原地的载体给不出第一拍的运输钩子，直接在激发侧打回。"""
    idea = nested_idea(carrier='glacier ice cave',
                       title='蓝冰冰川洞改造成隐居雪境卧室',
                       dna='glacier-ice-cave / refuge-den / self-material-window')
    errors = pp.pacing_skeleton_outline_violations(idea)
    assert any('MAN-MADE TRANSPORTABLE carrier' in error for error in errors)


def test_nested_space_accepts_the_whole_transported_carrier_family():
    for carrier, first_entry in [
        ('shipping container', '吊车吊装集装箱入基坑'),
        ('retired school bus', '平板车运抵退役校车落位'),
        ('coach bus', '吊装大巴车身沉入基坑'),
        ('airliner fuselage', '吊车吊装退役机身落位'),
        ('railway boxcar', '拖运车厢至场地就位'),
    ]:
        outline = list(GOOD_NESTED_OUTLINE)
        outline[0] = first_entry
        idea = nested_idea(outline, carrier=carrier, title=f'{carrier} 改造', dna=f'{carrier} / x / y')
        assert pp.pacing_skeleton_outline_violations(idea) == [], f'{carrier} 应被接受'


def test_nested_space_requires_the_first_beat_to_deliver_the_carrier():
    """载体对了也不够：第一拍必须是装备把它运到现场并落位，而不是对着已在原地的
    壳体开始清理——那正是用户说的「节拍不对」。"""
    for bad_first in ['清空箱内残留货架碎屑', '打磨除锈整片箱壁', '吊装钢制支撑框架']:
        outline = list(GOOD_NESTED_OUTLINE)
        outline[0] = bad_first
        errors = pp.pacing_skeleton_outline_violations(nested_idea(outline))
        assert any('FIRST beat_outline entry must deliver the carrier' in error
                   for error in errors), f'{bad_first} 不该被当成运输落位拍'


def test_nested_space_outline_rejects_a_partial_first_space_payoff():
    outline = list(GOOD_NESTED_OUTLINE)
    outline[5] = '继续安装第一空间墙板'
    errors = pp.pacing_skeleton_outline_violations(nested_idea(outline))
    assert any('primary space function' in error for error in errors)


def test_nested_space_outline_requires_exactly_one_raw_second_space_reset():
    outline = list(GOOD_NESTED_OUTLINE)
    outline[6] = '继续完善室内布局'
    errors = pp.pacing_skeleton_outline_violations(nested_idea(outline))
    assert any('exactly one declared reset' in error for error in errors)


def test_nested_reset_accepts_concrete_second_space_wording():
    """门禁旧版三条分支都要求出现「第二/另一/新…」这类序数词，而 ≤16 字、动词开头、
    点名具体里程碑的清单自然写成「硬切进入毛坯后舱」——于是整批 0 通过、掉进静态兜底，
    用户侧的现象就是「每次只出一张灵感卡」。这些具体写法必须认。"""
    for reset_entry in ['硬切进入毛坯后舱', '跳切至未施工的隔间', '硬切转入原始前舱',
                        '转场至毛坯储藏室', '硬切进入未修舱室']:
        outline = list(GOOD_NESTED_OUTLINE)
        outline[6] = reset_entry
        errors = pp.pacing_skeleton_outline_violations(nested_idea(outline))
        assert errors == [], f'{reset_entry} 应被认成合法重置拍，实际: {errors}'


def test_nested_reset_accepts_a_declared_cut_without_a_raw_state_word():
    """毛坯态词只对「切入/进入/转到 + …」那两支要求（那些动词普通施工拍里也有）。

    分支 1 用的是明确的剪辑术语，术语本身已经把「这是一次宣告式重置」说死了。旧版对三支
    一律复查，而清单每条 ≤16 字、还要动词开头 + 点名里程碑，硬塞「毛坯」经常挤掉空间名或
    动词——「硬切进入第二舱室」这种完全正确的写法被判掉，整张卡跟着被降级成单线。
    """
    for reset_entry in ['硬切进入第二舱室', '跳切至隔壁储藏室', '转场进入后舱']:
        outline = list(GOOD_NESTED_OUTLINE)
        outline[6] = reset_entry
        errors = pp.pacing_skeleton_outline_violations(nested_idea(outline))
        assert errors == [], f'{reset_entry} 应被认成合法重置拍，实际: {errors}'


def test_nested_reset_still_needs_a_raw_state_word_without_a_cut_term():
    """反向护栏：没有剪辑术语时，「切入/进入」这类动词在施工拍里也会出现，
    缺了毛坯态词就分不出这到底是不是一次重置——那一支必须继续查。"""
    outline = list(GOOD_NESTED_OUTLINE)
    outline[6] = '进入第二舱室继续施工'
    errors = pp.pacing_skeleton_outline_violations(nested_idea(outline))
    assert any('untouched/raw state' in e or 'exactly one declared reset' in e for e in errors)


def test_nested_cards_are_never_relabelled_into_another_skeleton():
    """降级救不了这个骨架：标签改成单线之后确实不骗人，但交付的是另一种片子
    ——用户勾的第二毛坯空间那一幕压根不存在。"""
    assert 'nested_space_payoff' in pp._NO_DOWNGRADE_SKELETONS


def test_nested_reset_written_as_a_doorway_travel_shot_is_named_as_such():
    """这个骨架的重置按定义是硬切（threshold_variant=hard_cut）。写成推镜过门时要说清
    是「写法不对」，而不是含糊的 found 0——错误串会被回喂给模型当返工说明。"""
    outline = list(GOOD_NESTED_OUTLINE)
    outline[6] = '推镜过门进入原始舱内'
    errors = pp.pacing_skeleton_outline_violations(nested_idea(outline))
    assert any('DECLARED HARD CUT' in error for error in errors)


def test_nested_reset_stays_unique_across_an_ordinary_two_room_outline():
    """放宽词表不能把普通施工拍也算成重置：多于一处一样会被否掉。"""
    idea = nested_idea([
        '吊车吊装集装箱入基坑', '清空第一空间碎屑落尘', '铺设第一空间防潮膜',
        '架设木龙骨与保温层', '封装储备区木饰面墙', '备齐储备厨房完成使用',
        '硬切进入毛坯后舱', '清运后舱锈屑与积渣', '铺设防潮膜与电路',
        '架设墙顶木龙骨', '封装保温内衬面板', '铺装成品地板与涂料',
        '布置卧榻与羊毛软装', '点亮卧室全景,人物入住',
    ])
    assert pp.pacing_skeleton_outline_violations(idea) == []


def test_nested_brief_declares_the_carrier_arrives_on_camera():
    """首帧口径的唯一开关：只有这个骨架把载体的到场当成 Beat 1，其余骨架的载体
    开拍前就在原地，首帧照旧对着载体拍。"""
    nested = pp.apply_pacing_skeleton_to_brief({'mode': 'Standard'}, 'nested_space_payoff')
    assert pp.carrier_arrives_on_camera(nested) is True
    for other in ('linear_milestone', 'dual_payoff'):
        brief = pp.apply_pacing_skeleton_to_brief({'mode': 'Standard'}, other)
        assert pp.carrier_arrives_on_camera(brief) is False
    # 换骨架复用同一份 brief 时必须翻回来，不能留着上一次的 True
    reused = pp.apply_pacing_skeleton_to_brief(dict(nested), 'linear_milestone')
    assert pp.carrier_arrives_on_camera(reused) is False


def test_image_1_must_not_show_a_carrier_that_is_still_being_delivered():
    """第一帧就画出载体 = Beat 1（把载体运过来）没有任何可交付的状态变化。
    这条是直出模式下唯一仍会触发首帧重生成的硬伤。"""
    brief = pp.apply_pacing_skeleton_to_brief(
        {'carrier': 'shipping container'}, 'nested_space_payoff')
    leaked = ('A static ultra-wide tripod shot: a rusted shipping container sits in the '
              'overgrown clearing; horizon line remains level.')
    errors = pp.check_image_1_carrier_absent(leaked, brief)
    assert errors and 'container' in errors[0]

    clean = ('A static ultra-wide tripod shot: an overgrown clearing of cracked slab, slumped '
             'earth banks and rusted fence wire; horizon line remains level.')
    assert pp.check_image_1_carrier_absent(clean, brief) == []


def test_image_1_carrier_check_is_scoped_to_delivered_carrier_projects():
    """其它骨架的首帧本来就该出现载体——这条检查绝不能对它们生效。"""
    brief = pp.apply_pacing_skeleton_to_brief(
        {'carrier': 'shipping container'}, 'linear_milestone')
    leaked = 'A rusted shipping container sits in the overgrown clearing.'
    assert pp.check_image_1_carrier_absent(leaked, brief) == []


def test_image_1_carrier_check_ignores_generic_site_scrap():
    """只用项目自己的载体词做判据：场地上的报废车轴、锈油罐是合理荒废景物，
    用整族词表会把干净的空场地首帧误伤成违规、白烧一次首帧生成。"""
    brief = pp.apply_pacing_skeleton_to_brief(
        {'carrier': 'airliner fuselage'}, 'nested_space_payoff')
    site = ('A static ultra-wide tripod shot: a derelict quarry floor with a rusted truck axle '
            'half-buried in gravel and weeds; horizon line remains level.')
    assert pp.check_image_1_carrier_absent(site, brief) == []


def test_delivery_beat_contract_names_the_machinery_and_the_empty_start_frame():
    """载体到场拍与其余拍口径不同：起始帧没有载体、主体是机械而不是「一个工人 + 一把
    手工工具」。不单独说清楚，通用规则会把它写成「在已经就位的壳体上干活」。"""
    ladder = [
        {'index': 1, 'operation': 'repair', 'description': 'deliver the shell',
         'bridge_stage': None, 'carrier_delivery': True},
        {'index': 2, 'operation': 'repair', 'description': 'bury it', 'bridge_stage': None},
        {'index': 3, 'operation': 'reward', 'description': 'reveal', 'bridge_stage': None},
    ]
    contract = pp._beat_contract(1, 3, ladder, 'Threshold', {'camera_dna': 'static shot'}, '')
    text = contract['family_contract']
    assert 'CARRIER DELIVERY' in text
    assert 'IMAGE 1 is the EMPTY SITE' in text
    assert 'crane' in text and 'flatbed' in text
    assert 'single-manual-tool rule does not apply' in text

    # 没有这个标记的拍一个字都不该多出来
    plain = pp._beat_contract(2, 3, ladder, 'Threshold', {'camera_dna': 'static shot'}, '')
    assert 'CARRIER DELIVERY' not in plain['family_contract']


def test_nested_only_static_fallbacks_keep_an_honest_nested_outline(monkeypatch):
    monkeypatch.setattr(pp, 'read_ledger', lambda: [])
    monkeypatch.setattr(pp, 'fetch_trend_snippet', lambda *args, **kwargs: '')
    monkeypatch.setattr(pp, 'fetch_custom_url_snippet', lambda *args, **kwargs: '')

    def fail_chat(*args, **kwargs):
        raise RuntimeError('offline test')

    monkeypatch.setattr(pp, '_chat', fail_chat)
    result = pp.run_ideate({}, count=3, pacing_skeleton_ids=['nested_space_payoff'])
    assert len(result['ideas']) == 3
    seen_titles = set()
    for idea in result['ideas']:
        assert idea['pacing_skeleton'] == 'nested_space_payoff'
        assert pp.outline_skeleton_violations(idea) == []
        assert pp.pacing_skeleton_outline_violations(idea) == []
        # 兜底选题本身也必须是人工运输载体，否则卡面标题与第一拍会互相打脸
        assert pp._nested_carrier_is_transportable(idea)
        seen_titles.add(idea['title'])
    assert len(seen_titles) == 3
