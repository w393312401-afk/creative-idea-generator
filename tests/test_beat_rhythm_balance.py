"""推进节奏均衡（docs/pacing_rhythm_balance_plan.md，2026-07-31）。

问题定性：这不是「快」或「慢」，是**方差**。既有的每拍门禁全是「单拍内、成员资格式」
的判断，(3 工序, 3 格位, 跨 2 族) 和 (1 工序, 1 格位, 1 族) 都完全合法，实际视觉增量
差着 3.6 倍，而每段的屏幕时间都是恒定的 VIDEO_DURATION 秒。没有任何一条既有规则
比较拍 i 与拍 i-1。

三层覆盖，与方案的分层一一对应：
1. 度量 —— beat_delta_weight：从已声明字段派生的拍重标量
2. 约束 —— rhythm_ladder_violations：R1 硬天花板 / R2 相邻比值 / R3 曲线形状
3. 交付 —— beat_clip_seconds + video_generator 的按拍重变速合并
外加激发侧的条目重量门禁（outline_weight_violations / compute_beats_floor）。
"""
import unittest
from unittest.mock import patch

import prompt_pipeline as pp
import video_generator as vg


def _beat(index, operation='framing', scope='large', ops=None, grids=None,
          milestone=None, **extra):
    beat = {
        'index': index,
        'operation': operation,
        'stage_scope': scope,
        'package_operations': list(ops) if ops is not None else [operation],
        'changed_grid_cells': list(grids) if grids is not None else ['A1', 'B1'],
        'milestone_name': milestone or f'{operation} milestone {index}',
    }
    beat.update(extra)
    return beat


# ─────────────────────────────────────────────────────────────────────────
# 1. 度量：beat_delta_weight
# ─────────────────────────────────────────────────────────────────────────

class TestBeatDeltaWeight(unittest.TestCase):
    def test_final_retry_repairs_cross_phase_package_metadata(self):
        ladder = [
            _beat(1, operation='furnishing',
                  ops=['furnishing', 'wiring', 'lighting']),
            _beat(2, operation='rough-in', ops=['rough-in', 'drywall']),
        ]

        repairs = pp.repair_incompatible_package_operations(ladder)

        self.assertEqual(ladder[0]['package_operations'], ['furnishing', 'lighting'])
        # 裁到下限（2 道）以下就不裁：rough-in 与 drywall 不相容，删掉 drywall 只是把
        # 「相位冲突」换成「只剩一道工序」——同样要被 milestone/frame-state 闸报一次，
        # 却白丢了模型的申报内容。凭空补一道工序更不行，下游提示词会照着它写。
        self.assertEqual(ladder[1]['package_operations'], ['rough-in', 'drywall'])
        self.assertEqual(len(repairs), 1)

    def test_camera_beats_have_no_weight(self):
        """运镜拍返回 None 而不是 0：它们有专属契约、不承载施工增量，参与统计只会
        污染均值，而 0 会被相邻比值运算当成除零。"""
        for beat in (
            _beat(5, operation='threshold'),
            _beat(5, operation='reward'),
            _beat(5, bridge_stage=1),
            _beat(5, hard_cut=True),
        ):
            self.assertIsNone(pp.beat_delta_weight(beat), beat)

    def test_reference_weights_from_the_plan(self):
        """方案 §3.2 的拍重落点表——这张表就是四个权重常量的回归锁。

        2026-08-02：打包项由线性改成边际计价（见 _PACKAGE_MARGINAL_WEIGHT），
        单工序的三个落点原样保留，双/三工序两个落点按新公式重算。
        """
        light = _beat(1, operation='lighting', scope='default',
                      ops=['lighting'], grids=['B2'], milestone='pendant lamp hung')
        self.assertAlmostEqual(pp.beat_delta_weight(light), 0.60)

        light_large = dict(light, stage_scope='large')
        self.assertAlmostEqual(pp.beat_delta_weight(light_large), 1.60)

        framing = _beat(2, operation='framing', ops=['framing'], grids=['A1', 'B1'],
                        milestone='wall studs erected')
        self.assertAlmostEqual(pp.beat_delta_weight(framing), 1.90)

        # 1.5×1.6 + 0.3(格) + 0.4(族)
        two_layers = _beat(3, operation='drywall', ops=['drywall', 'priming'],
                           grids=['A1', 'B1'], milestone='walls boarded and primed')
        self.assertAlmostEqual(pp.beat_delta_weight(two_layers), 3.10)

        # 2.0×1.6 + 0.6(格) + 0.4(族)
        three_ops = _beat(4, operation='drywall', ops=['drywall', 'priming', 'painting'],
                          grids=['A1', 'B1', 'C1'], milestone='walls closed and painted')
        self.assertAlmostEqual(pp.beat_delta_weight(three_ops), 4.20)

    def test_real_data_spread_is_the_problem_being_measured(self):
        """§1.2 说明真实数据里 stage_scope 恒为 large。在那个形态下最重/最轻仍差 2.6 倍，
        而两者的屏幕时间完全相同——这个比值就是用户观感问题的量化表达。"""
        lightest = pp.beat_delta_weight(
            _beat(1, operation='lighting', ops=['lighting'], grids=['B2'],
                  milestone='pendant lamp hung'))
        heaviest = pp.beat_delta_weight(
            _beat(2, operation='drywall', ops=['drywall', 'priming', 'painting'],
                  grids=['A1', 'B1', 'C1'], milestone='walls closed and painted'))
        self.assertGreater(heaviest / lightest, 2.5)

    def test_package_pricing_keeps_the_gates_satisfiable(self):
        """线性计价时这套门禁在数值上自相矛盾：prompt 明写「允许最多三道紧密工序」，
        而 3 工序拍恒为 4.80 > hard_ceiling 3.20；且任何单工序拍挨着双工序拍的比值
        恒为 2.00 > neighbor_ratio 1.80。nested 的 FIXED SLOT BLUEPRINT 又把拍数钉死、
        材料层数多于槽位，打包是结构刚需——「必须打包」与「打包必违规」对撞，
        前两次重排被稳定烧在这上面。这条用例锁的就是「门禁必须有解」。"""
        nested = pp.skeleton_rhythm('nested_space_payoff')
        ceiling = nested['hard_ceiling']
        cap = nested['neighbor_ratio']

        # 同一区域、同一材料层内的紧凑打包（prompt 允许的上限形态）必须合法
        tight_three = _beat(1, operation='drywall',
                            ops=['drywall', 'taping', 'sanding'], grids=['A1'],
                            milestone='wall board closure complete')
        self.assertLessEqual(pp.beat_delta_weight(tight_three), ceiling)

        # 单工序拍与双工序拍相邻必须合法（否则唯一解是全序列同工序数）
        single = pp.beat_delta_weight(_beat(1, ops=['framing'], grids=['A1']))
        double = pp.beat_delta_weight(
            _beat(2, operation='drywall', ops=['drywall', 'taping'], grids=['A1']))
        self.assertLessEqual(double / single, cap)

        # 但摊开到多格位/多材料层的三工序拍仍要被打回——放宽的是计价方式，不是尺度
        sprawling = _beat(3, operation='drywall',
                          ops=['drywall', 'priming', 'painting'],
                          grids=['A1', 'B1', 'C1'],
                          milestone='walls closed and painted')
        self.assertGreater(pp.beat_delta_weight(sprawling), ceiling)

    def test_malformed_beat_does_not_raise(self):
        self.assertIsNone(pp.beat_delta_weight(None))
        self.assertIsNone(pp.beat_delta_weight('not a dict'))

    def test_undeclared_beat_is_none_not_lightest(self):
        """兜底 ladder（三次生成全失败时那条只有 operation/bridge_stage 的应急梯）
        一个里程碑字段都没有。把它们当「最轻拍」会把每一段都压到下限时长——在整单
        质量已经最差的那条路径上再叠一层节奏破坏。未申报 ≠ 最轻。"""
        self.assertIsNone(pp.beat_delta_weight({'index': 1, 'operation': 'repair'}))
        self.assertIsNone(pp.beat_delta_weight(
            {'index': 1, 'operation': 'repair', 'description': 'x', 'bridge_stage': None}))
        # 只要声明了任意一个，就照常计权
        self.assertIsNotNone(pp.beat_delta_weight(
            {'index': 1, 'operation': 'repair', 'changed_grid_cells': ['A1', 'B1']}))

    def test_layer_family_span_does_not_double_count_across_families(self):
        """每个词只能属于一族：跨族误命中会直接虚增族跨度、顶到 R1 天花板，
        代价是一次 150s 的白重排。"""
        # 'flooring' 只在饰面族，基层族不收 'subfloor' 这类含 'floor' 的词
        self.assertEqual(pp._layer_family_span(
            _beat(1, operation='flooring', ops=['flooring'], milestone='floor laid')), 1)
        self.assertEqual(pp._layer_family_span(
            _beat(1, operation='framing', ops=['framing', 'insulation'],
                  milestone='studs and batts')), 2)

    def test_layer_family_span_ignores_the_substrate_named_in_milestone_name(self):
        """milestone_name 是「终结在什么产物上」，产物名天然要带上它覆盖的基层。
        把它算进族跨度 = 给每个正确的覆盖拍白加 0.4 拍重，一路顶到 R1 天花板
        （改造前这是 server.log 里 "packs too much visible change" 的头号来源）。"""
        covering = _beat(1, operation='flooring', ops=['flooring'],
                         milestone='plank flooring laid over the insulated joists')
        self.assertEqual(pp._layer_family_span(covering), 1)
        self.assertAlmostEqual(pp.beat_delta_weight(covering), 1.90)
        # 真·两层打包必须申报在 package_operations 上，那里照常计族
        self.assertEqual(pp._layer_family_span(
            _beat(1, operation='flooring', ops=['flooring', 'furnishing'],
                  milestone='floor laid')), 2)


# ─────────────────────────────────────────────────────────────────────────
# 2. 约束：rhythm_ladder_violations
# ─────────────────────────────────────────────────────────────────────────

class TestRhythmLadderViolations(unittest.TestCase):
    def test_even_ladder_passes(self):
        ladder = [_beat(i, ops=['framing'], grids=['A1', 'B1']) for i in range(1, 6)]
        self.assertEqual(pp.rhythm_ladder_violations(ladder, 'linear_milestone'), [])

    def test_r1_hard_ceiling_flags_the_over_packed_beat(self):
        ladder = [
            _beat(1, ops=['framing'], grids=['A1', 'B1']),
            _beat(2, operation='drywall', ops=['drywall', 'priming', 'painting'],
                  grids=['A1', 'B1', 'C1'], milestone='walls closed and painted'),
            _beat(3, ops=['framing'], grids=['A1', 'B1'], milestone='more framing'),
        ]
        errors = pp.rhythm_ladder_violations(ladder, 'linear_milestone')
        self.assertTrue(any('Beat 2 packs too much visible change' in e for e in errors))

    def test_r2_neighbour_ratio_flags_the_lurch(self):
        """整套方案里性价比最高的一条：不管绝对值，只管不要突变。"""
        ladder = [
            _beat(1, operation='lighting', scope='default', ops=['lighting'],
                  grids=['B2'], milestone='lamp hung'),                     # 0.60
            _beat(2, operation='framing', ops=['framing'], grids=['A1', 'B1']),  # 1.90
        ]
        errors = pp.rhythm_ladder_violations(ladder, 'linear_milestone')
        self.assertTrue(any('very different' in e for e in errors), errors)
        self.assertTrue(any('Beats 1 and 2' in e for e in errors), errors)

    def test_r2_treats_beats_across_a_camera_beat_as_adjacent(self):
        """运镜拍在序列里被**跳过而不是断开**：过门拍是节奏上的呼吸点，它两侧的两个
        施工拍在观感上仍然前后衔接，相邻比值必须照查。"""
        ladder = [
            _beat(1, operation='lighting', scope='default', ops=['lighting'],
                  grids=['B2'], milestone='lamp hung'),                     # 0.60
            _beat(2, operation='threshold', bridge_stage=1),                # 无拍重
            _beat(3, operation='framing', ops=['framing'], grids=['A1', 'B1']),  # 1.90
        ]
        errors = pp.rhythm_ladder_violations(ladder, 'linear_milestone')
        self.assertTrue(any('Beats 1 and 3' in e for e in errors), errors)

    def test_gate_switch_turns_r1_and_r2_off(self):
        ladder = [
            _beat(1, operation='lighting', scope='default', ops=['lighting'], grids=['B2']),
            _beat(2, operation='drywall', ops=['drywall', 'priming', 'painting'],
                  grids=['A1', 'B1', 'C1']),
        ]
        with patch.object(pp, '_RHYTHM_GATE_ENFORCING', False):
            self.assertEqual(pp.rhythm_ladder_violations(ladder, 'linear_milestone'), [])

    def test_empty_and_malformed_ladders_are_noop(self):
        for ladder in ([], None, 'nope', [{'operation': 'reward'}]):
            self.assertEqual(pp.rhythm_ladder_violations(ladder, 'linear_milestone'), [])

    def test_unknown_skeleton_falls_back_to_default_rhythm(self):
        """新增创意类型时不写 'rhythm' 键也必须正常工作——这是本方案对
        「针对后续所有创意类型」承诺的兼容锁。"""
        ladder = [_beat(i, ops=['framing'], grids=['A1', 'B1']) for i in range(1, 4)]
        self.assertEqual(pp.rhythm_ladder_violations(ladder, 'a_skeleton_added_next_year'), [])
        self.assertEqual(pp.skeleton_rhythm('a_skeleton_added_next_year'),
                         pp.skeleton_rhythm(None))

    def test_declared_rhythm_overrides_only_the_keys_it_names(self):
        """逐项回落：dual 只声明了四项，其余（tail_accel_from 等）必须还是默认值。"""
        dual = pp.skeleton_rhythm('dual_payoff')
        self.assertEqual(dual['neighbor_ratio'], 1.8)
        self.assertEqual(dual['arc'], 'two_arcs')
        self.assertEqual(dual['tail_accel_from'], pp._DEFAULT_RHYTHM['tail_accel_from'])


class TestRhythmArcShape(unittest.TestCase):
    """R3 曲线形状。默认**关闭**（只记录不打回），所以每条用例都要显式打开。"""

    def test_arc_gate_is_off_by_default(self):
        # 收尾比开头重得多的序列：R3 开着会报，默认状态下必须一条都不报。
        # R1/R2 单独关掉，这条用例只锁 R3 的开关。
        ladder = ([_beat(i, operation='lighting', scope='default', ops=['lighting'],
                         grids=['B2'], milestone=f'lamp {i}') for i in range(1, 5)]
                  + [_beat(5, operation='drywall', ops=['drywall', 'priming'],
                           grids=['A1', 'B1'], milestone='boarded and primed')])
        self.assertFalse(pp._RHYTHM_ARC_ENFORCING,
                         'R3 必须默认关闭：它最容易在合法的特殊结构上误判')
        with patch.object(pp, '_RHYTHM_GATE_ENFORCING', False):
            self.assertEqual(pp.rhythm_ladder_violations(ladder, 'linear_milestone'), [])

    def test_front_load_plateau_flags_a_heavy_tail(self):
        ladder = ([_beat(i, operation='drywall', ops=['drywall', 'priming'],
                         grids=['A1', 'B1'], milestone=f'boarded {i}') for i in range(1, 5)]
                  + [_beat(5, operation='drywall', ops=['drywall', 'priming', 'painting'],
                           grids=['A1', 'B1', 'C1'], milestone='closed and painted'),
                     _beat(6, operation='drywall', ops=['drywall', 'priming', 'painting'],
                           grids=['A1', 'B1', 'C1'], milestone='second coat')])
        with patch.object(pp, '_RHYTHM_ARC_ENFORCING', True), \
             patch.object(pp, '_RHYTHM_GATE_ENFORCING', False):
            errors = pp.rhythm_ladder_violations(ladder, 'linear_milestone')
        self.assertTrue(any('closing stretch carries a heavier average' in e for e in errors), errors)

    def test_two_arcs_flags_an_unbalanced_second_act(self):
        ladder = (
            [_beat(i, operation='drywall', ops=['drywall', 'priming'], grids=['A1', 'B1'],
                   milestone=f'exterior {i}') for i in range(1, 4)]
            + [_beat(4, operation='threshold', bridge_stage=1)]
            + [_beat(i, operation='lighting', scope='default', ops=['lighting'],
                     grids=['B2'], milestone=f'lamp {i}') for i in range(5, 8)]
        )
        with patch.object(pp, '_RHYTHM_ARC_ENFORCING', True), \
             patch.object(pp, '_RHYTHM_GATE_ENFORCING', False):
            errors = pp.rhythm_ladder_violations(ladder, 'dual_payoff')
        self.assertTrue(any('two acts are unbalanced' in e for e in errors), errors)

    def test_two_arcs_reset_wants_a_faster_second_space(self):
        """观众在第一幕已经学完整套材料梯，第二遍走同一条梯子必须更快。"""
        first = [_beat(i, operation='framing', ops=['framing'], grids=['A1', 'B1'],
                       milestone=f'primary {i}') for i in range(1, 4)]
        cut = [_beat(4, operation='clearing', hard_cut=True)]
        # 第二幕和第一幕一样重 → ratio 1.0，超出 (0.75, 0.95)
        second = [_beat(i, operation='framing', ops=['framing'], grids=['A1', 'B1'],
                        milestone=f'secondary {i}') for i in range(5, 8)]
        with patch.object(pp, '_RHYTHM_ARC_ENFORCING', True), \
             patch.object(pp, '_RHYTHM_GATE_ENFORCING', False):
            errors = pp.rhythm_ladder_violations(first + cut + second, 'nested_space_payoff')
        self.assertTrue(any('second space runs as heavy as the first' in e for e in errors), errors)

    def test_short_acts_are_never_judged_on_shape(self):
        """任一幕不足两拍就不判形状：那是拍数结构本身的问题，已有专门门禁在管，
        在这里重复报错只会淹没真正的原因。"""
        ladder = [
            _beat(1, ops=['framing'], grids=['A1', 'B1']),
            _beat(2, operation='threshold', bridge_stage=1),
            _beat(3, operation='lighting', scope='default', ops=['lighting'], grids=['B2']),
        ]
        with patch.object(pp, '_RHYTHM_ARC_ENFORCING', True), \
             patch.object(pp, '_RHYTHM_GATE_ENFORCING', False):
            self.assertEqual(pp.rhythm_ladder_violations(ladder, 'dual_payoff'), [])


# ─────────────────────────────────────────────────────────────────────────
# 3. 交付：屏幕时间分配
# ─────────────────────────────────────────────────────────────────────────

class TestClipTiming(unittest.TestCase):
    def test_reference_weight_gets_exactly_the_nominal_duration(self):
        rhythm = pp.skeleton_rhythm('linear_milestone')
        low, high = rhythm['weight_band']
        self.assertEqual(pp.beat_clip_seconds((low + high) / 2, rhythm), pp.VIDEO_DURATION)
        self.assertEqual(pp.beat_clip_speed((low + high) / 2, rhythm), 1.0)

    def test_heavier_beats_get_more_time_but_sublinearly(self):
        """开平方是刻意的：信息量翻倍时屏幕时间只给约 1.4 倍——重拍恰恰是观众注意力
        最高的时刻，线性分配会让它拖沓。"""
        rhythm = pp.skeleton_rhythm('linear_milestone')
        ref = sum(rhythm['weight_band']) / 2
        doubled = pp.beat_clip_seconds(ref * 2, rhythm)
        self.assertGreater(doubled, pp.VIDEO_DURATION)
        self.assertLess(doubled, pp.VIDEO_DURATION * 2)
        self.assertAlmostEqual(doubled / pp.VIDEO_DURATION, 2 ** 0.5, places=1)

    def test_extremes_are_clamped(self):
        rhythm = pp.skeleton_rhythm('linear_milestone')
        self.assertEqual(pp.beat_clip_seconds(0.01, rhythm), pp._CLIP_SECONDS_MIN)
        self.assertEqual(pp.beat_clip_seconds(99.0, rhythm), pp._CLIP_SECONDS_MAX)

    def test_camera_beats_keep_the_nominal_duration(self):
        """过门拍与 reward 拍的时长是叙事设计的一部分，不该被施工密度调制。"""
        self.assertEqual(pp.beat_clip_speed(None), 1.0)
        self.assertEqual(pp.beat_clip_speed(0), 1.0)

    def test_pace_tag_lands_in_the_video_meta(self):
        """meta 是 compose -> 帧 -> 视频 -> manifest -> 合并 全程唯一幸存的每槽位
        元数据通道（generate_video_sequence 只收得到 prompt_block）。"""
        ladder = [
            _beat(1, operation='lighting', scope='default', ops=['lighting'], grids=['B2']),
            _beat(2, operation='threshold', bridge_stage=1),
        ]
        _, videos, block = pp._build_partial_prompt_block(
            {1: 'img1', 2: 'img2', 3: 'img3'}, {1: 'vid1', 2: 'vid2'},
            ladder, 'linear_milestone')
        self.assertIn('PACE', videos[1]['meta'])
        self.assertIn('PACE', block)
        # 运镜拍不标 PACE，且原有的 BRIDGE 标签不被顶掉
        self.assertEqual(videos[2]['meta'], 'BRIDGE')

    def test_clip_timing_switch_removes_every_pace_tag(self):
        ladder = [_beat(1, operation='lighting', scope='default', ops=['lighting'], grids=['B2'])]
        with patch.object(pp, '_RHYTHM_CLIP_TIMING', False):
            _, videos, block = pp._build_partial_prompt_block(
                {1: 'img1', 2: 'img2'}, {1: 'vid1'}, ladder, 'linear_milestone')
        self.assertEqual(videos[1]['meta'], '')
        self.assertNotIn('PACE', block)


class TestMergePacing(unittest.TestCase):
    def test_meta_parsing(self):
        self.assertAlmostEqual(vg._clip_speed_from_meta('PACE 1.21'), 1.21)
        self.assertAlmostEqual(vg._clip_speed_from_meta('BRIDGE TURN'), 1.0)
        self.assertAlmostEqual(vg._clip_speed_from_meta('HERO'), 1.0)
        self.assertAlmostEqual(vg._clip_speed_from_meta(''), 1.0)
        self.assertAlmostEqual(vg._clip_speed_from_meta(None), 1.0)
        # 畸形值不该把整次合并搞垮
        self.assertAlmostEqual(vg._clip_speed_from_meta('PACE 0'), 1.0)
        self.assertAlmostEqual(vg._clip_speed_from_meta('PACE 99'), vg._CLIP_SPEED_MAX)

    def test_global_speed_and_clip_speed_multiply(self):
        """并列处理会让两套时间缩放互相打架——setpts 用 clip/speed，atempo 用 speed/clip。"""
        f = vg._paced_merge_filter([1.25, 0.8], 2.0, False)
        self.assertIn('[0:v]setpts=0.625*PTS[v0]', f)
        self.assertIn('[1:v]setpts=0.4*PTS[v1]', f)
        self.assertIn('concat=n=2:v=1:a=0[v]', f)

    def test_atempo_is_chained_when_it_leaves_the_legal_range(self):
        """atempo 单次只接受 0.5~2.0。全局 2x 叠上一个慢速段时会到 3.4，
        直接写 atempo=3.4 会让整次合并失败，且原因埋在 ffmpeg 的 stderr 里。"""
        self.assertEqual(vg._atempo_chain(3.4), 'atempo=2,atempo=1.7')
        self.assertEqual(vg._atempo_chain(1.45), 'atempo=1.45')
        self.assertEqual(vg._atempo_chain(0.3), 'atempo=0.5,atempo=0.6')

    def test_every_atempo_factor_stays_legal_across_the_whole_range(self):
        for global_speed in (1.0, 1.5, 2.0):
            for clip in (vg._CLIP_SPEED_MIN, 0.71, 1.0, 1.375, vg._CLIP_SPEED_MAX):
                chain = vg._atempo_chain(global_speed / clip)
                for part in chain.split(','):
                    value = float(part.split('=')[1])
                    self.assertTrue(0.5 <= value <= 2.0, f'{chain} -> {value}')

    def test_audio_filter_interleaves_streams_for_concat(self):
        f = vg._paced_merge_filter([1.2, 0.9], 1.0, True)
        self.assertIn('[v0][a0][v1][a1]concat=n=2:v=1:a=1[v][a]', f)


# ─────────────────────────────────────────────────────────────────────────
# 4. 激发侧：条目重量与加权 beats_floor
# ─────────────────────────────────────────────────────────────────────────

class TestOutlineWeight(unittest.TestCase):
    def test_three_layer_entry_is_flagged(self):
        idea = {'beat_outline': [
            '清空舱内碎冰与积雪',
            '铺防水膜立龙骨并封板',      # 防水管线 + 龙骨框架 + 封板面板 = 三族
            '点亮灯带,人物入住',
        ]}
        errors = pp.outline_weight_violations(idea)
        self.assertTrue(any('铺防水膜立龙骨并封板' in e for e in errors), errors)

    def test_two_layer_entry_is_allowed(self):
        """取 3 而不是 2：同相位串联在卡片粒度上是合理表述，拦它必然误伤。"""
        idea = {'beat_outline': ['铺设防水膜与龙骨', '点亮灯带,人物入住']}
        self.assertEqual(pp.outline_weight_violations(idea), [])

    def test_two_layer_cross_phase_entry_is_flagged(self):
        """两族也不能跨越因果边界：饰面完成与家具备齐必须各有到达锚点。"""
        idea = {'beat_outline': [
            '清空舱内碎冰与积雪',
            '批刮微水泥饰面并安装储备厨房备齐',
            '点亮灯带,人物入住',
        ]}
        errors = pp.outline_weight_violations(idea)
        self.assertTrue(any('批刮微水泥' in e and '6->7' in e for e in errors), errors)

    def test_reward_entry_is_never_weighed(self):
        idea = {'beat_outline': ['清空舱内碎冰', '铺地板刷涂料并布置家具软装']}
        # 末条是 reward 揭示，不按施工条目算
        self.assertEqual(pp.outline_weight_violations(idea), [])

    def test_shipped_fallback_outlines_all_pass(self):
        """防误判最有效的一条：仓库自带的兜底清单必须全过，否则这道门禁上线即误伤
        （代价是 150s 重试白烧 + 最后掉进静态兜底列表，而兜底自己也不合格）。

        取法与 tests/test_outline_skeleton_gate.py 的同名用例一致：让 _chat 直接失败，
        run_ideate 就交付仓库自带的静态兜底选题。
        """
        for skeleton_ids in (None, ['dual_payoff'], ['nested_space_payoff']):
            with patch.object(pp, 'read_ledger', return_value=[]), \
                 patch.object(pp, 'fetch_trend_snippet', return_value=''), \
                 patch.object(pp, 'fetch_custom_url_snippet', return_value=''), \
                 patch.object(pp, 'persist_trend_refs', return_value=[]), \
                 patch.object(pp, 'load_trend_refs', return_value=[]), \
                 patch.object(pp, '_chat', side_effect=RuntimeError('down')):
                result = pp.run_ideate({}, count=3, pacing_skeleton_ids=skeleton_ids)
            self.assertGreaterEqual(len(result['ideas']), 3)
            for idea in result['ideas']:
                self.assertEqual(pp.outline_weight_violations(idea), [],
                                 f"兜底选题「{idea.get('title')}」被条目重量门禁误判")

    def test_malformed_input_is_noop(self):
        self.assertEqual(pp.outline_weight_violations(None), [])
        self.assertEqual(pp.outline_weight_violations({}), [])
        self.assertEqual(pp.outline_weight_violations({'beat_outline': []}), [])


class TestWeightedBeatsFloor(unittest.TestCase):
    def test_heavy_entries_raise_the_floor_above_a_plain_entry_count(self):
        """旧算法眼里「封板批腻子刷完墙面」和「装一盏吊灯」都是 1 条，于是一份条数少、
        每条却很重的清单算出的 floor 偏低，ladder 合法地把每个工序压成一拍。"""
        light = {'beat_outline': [
            '清空舱内积雪', '装一盏吊灯', '挂上窗帘', '摆好座椅', '点亮灯带,人物入住']}
        heavy = {'beat_outline': [
            '清空舱内积雪与基层找平', '铺设防水膜与电路管线', '立起龙骨并填充保温',
            '封板并做饰面涂料', '点亮灯带,人物入住']}
        self.assertGreater(pp.compute_beats_floor(heavy), pp.compute_beats_floor(light))

    def test_malformed_outline_falls_back_to_the_global_constant(self):
        """回落 = 完全保持改造前的行为，老任务/手动输入主题的路径不受影响。"""
        self.assertEqual(pp.compute_beats_floor(None), pp._MIN_ADAPTIVE_CONSTRUCTION_BEATS)
        self.assertEqual(pp.compute_beats_floor({}), pp._MIN_ADAPTIVE_CONSTRUCTION_BEATS)
        self.assertEqual(pp.compute_beats_floor({'beat_outline': ['只有一条']}),
                         pp._MIN_ADAPTIVE_CONSTRUCTION_BEATS)


# ─────────────────────────────────────────────────────────────────────────
# 5. 词表收编：_LAYER_FAMILIES 只此一份
# ─────────────────────────────────────────────────────────────────────────

class TestLayerFamilyTables(unittest.TestCase):
    def test_both_sides_declare_the_same_number_of_families(self):
        """中英两侧是同一条规则的两面，族数与顺序必须严格对应——
        改一边忘了另一边，两侧的宽严会静静地漂开（这正是收编前已经发生过的事）。"""
        self.assertEqual(len(pp._LAYER_FAMILIES), len(pp._LAYER_FAMILIES_EN))

    def test_consolidated_table_is_the_union_of_the_two_former_copies(self):
        """收编取并集而不是交集：并集只会让某一族更容易命中，从而抬高调用点的
        realized_layers、放松它们 `< 4` 那道门，不会造成新的误判。"""
        joined = '|'.join(pp._LAYER_FAMILIES)
        for token in ('隐蔽', '装备板', '清运', '找平'):
            self.assertIn(token, joined, token)


if __name__ == '__main__':
    unittest.main()
