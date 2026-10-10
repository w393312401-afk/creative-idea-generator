# -*- coding: utf-8 -*-
"""过门帧的对标基准挂帧。

2026-08-31 复盘（海蚀洞穴微缩木屋单）：过门帧的 ref 长期「不是已经在室内，就是一张
特写」，逐拍审查每轮稳定报一条假的机位偏离，过门帧因此永远过不了。三个叠加的成因各有
一组用例守着：

  1. 取拍尾 —— 原片在空间边界上是硬切的，第 k 拍的拍尾帧与第 k+1 拍的拍首帧是同一张，
     跨越边界时取拍尾必然拿到室内图（TestBeatWindowAndSelection）；
  2. 序号偏移 —— 展开过门梯会插拍并重排编号，位置映射从那里开始整体错位
     （TestSourceBeatIndexStamping / test_mapping_survives_threshold_expansion）；
  3. 结构性缺口 —— 过门梯那几帧原片根本没拍过（test_threshold_slots_get_envelope_role）。
"""

import inspect
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import prompt_pipeline as pp


class TestSourceBeatIndexStamping(unittest.TestCase):
    """A：展开过门梯之后，每一拍还认得自己在原片里的位置。"""

    def _brief(self):
        return {
            'theme': '海蚀洞穴改造成微缩海景木屋',
            'carrier': 'sea cave',
            'mode': 'Threshold',
            'space_type': 'cave',
            'threshold_variant': 'coaxial',
        }

    def _ladder(self):
        return [
            {'index': 1, 'operation': 'gravel_clearing'},
            {'index': 2, 'operation': 'door_installation'},
            {'index': 3, 'operation': 'crossing', 'bridge_stage': 1},
            {'index': 4, 'operation': 'membrane_lining'},
            {'index': 5, 'operation': 'furnishing_reveal'},
        ]

    def test_expand_stamps_source_index_on_real_beats(self):
        out = pp.expand_spatial_transition_beats(self._ladder(), self._brief())
        real = [b for b in out if b.get('source_beat_index')]
        self.assertEqual([b['source_beat_index'] for b in real], [1, 2, 4, 5])
        # 展开之后交付位置已经跟原片拍序脱钩——这正是要靠 source_beat_index 回溯的原因。
        self.assertGreater(len(out), len(self._ladder()))

    def test_expand_marks_threshold_substeps_synthetic(self):
        out = pp.expand_spatial_transition_beats(self._ladder(), self._brief())
        subs = [b for b in out if b.get('synthetic_beat') == 'threshold_ladder']
        self.assertGreaterEqual(len(subs), 3)
        for b in subs:
            self.assertIsNone(b['source_beat_index'])
            self.assertEqual(b['crossing_source_beat_index'], 3)

    def test_finalize_renumbers_index_but_keeps_source_index(self):
        out = pp.expand_spatial_transition_beats(self._ladder(), self._brief())
        # index 被重排成连续的交付序号，source_beat_index 不受影响。
        self.assertEqual([b['index'] for b in out], list(range(1, len(out) + 1)))
        last = [b for b in out if b.get('source_beat_index')][-1]
        self.assertEqual(last['source_beat_index'], 5)
        self.assertNotEqual(last['index'], last['source_beat_index'])


class TestBeatWindowAndSelection(unittest.TestCase):
    """C：拍窗切断 + 切点/景别筛选。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.rf = os.path.join(self.tmp, 'review_frames')
        os.makedirs(self.rf)
        for n in range(190, 210):
            with open(os.path.join(self.rf, 'review_%03d.png' % n), 'wb') as f:
                f.write(b'x')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _exterior_beat(self):
        return {
            'space': 'exterior', 'start': 27.0, 'end': 29.0, 'observed_cuts': [],
            'coverage_frames': [
                {'frame': 'review_190.png', 'timestamp': 27.8},
                {'frame': 'review_193.png', 'timestamp': 28.3},
                # 与下一拍重叠的边界帧：它已经在室内。
                {'frame': 'review_199.png', 'timestamp': 29.0},
            ],
        }

    def _interior_beat(self):
        return {'space': 'main_interior', 'start': 29.0, 'end': 32.5,
                'coverage_frames': [{'frame': 'review_199.png', 'timestamp': 29.0}]}

    def test_window_drops_frame_shared_with_next_beat(self):
        names = [n for _ts, n in pp._beat_window_frames(self._exterior_beat(), self._interior_beat())]
        self.assertEqual(names, ['review_190.png', 'review_193.png'])

    def test_window_keeps_everything_without_a_next_beat(self):
        names = [n for _ts, n in pp._beat_window_frames(self._exterior_beat(), None)]
        self.assertIn('review_199.png', names)

    def test_select_picks_last_frame_still_outside(self):
        picked = pp._select_beat_ref_frame(self._exterior_beat(), self._interior_beat(),
                                           self.rf, {}, want_family='exterior')
        self.assertEqual(os.path.basename(picked), 'review_193.png')

    def test_select_refuses_cross_layer_beat(self):
        # 目标帧声明自己是外景，原片这一拍在室内：整拍弃用，不许硬塞。
        self.assertIsNone(
            pp._select_beat_ref_frame(self._interior_beat(), None, self.rf, {},
                                      want_family='exterior'))

    def test_select_skips_close_up_when_target_is_not_tight(self):
        facts = {'review_193.png': {'shot_scale': 'extreme_close'}}
        picked = pp._select_beat_ref_frame(self._exterior_beat(), self._interior_beat(),
                                           self.rf, facts, want_family='exterior')
        self.assertEqual(os.path.basename(picked), 'review_190.png')

    def test_select_skips_frames_next_to_a_source_cut(self):
        beat = self._exterior_beat()
        beat['observed_cuts'] = [28.35]
        picked = pp._select_beat_ref_frame(beat, self._interior_beat(), self.rf, {},
                                           want_family='exterior')
        self.assertEqual(os.path.basename(picked), 'review_190.png')

    def test_select_falls_back_rather_than_returning_nothing(self):
        # 全窗都是特写时，宁可给一张特写也别让这一拍凭空丢掉基准（逐级放宽的最后一档）。
        facts = {'review_190.png': {'shot_scale': 'close'},
                 'review_193.png': {'shot_scale': 'close'}}
        picked = pp._select_beat_ref_frame(self._exterior_beat(), self._interior_beat(),
                                           self.rf, facts, want_family='exterior')
        self.assertEqual(os.path.basename(picked), 'review_193.png')


class TestRefMappingEndToEnd(unittest.TestCase):
    """A+B+D：交付梯 → 原片拍序的挂帧，含过门梯的包络与兜底纪律。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        rf = os.path.join(self.tmp, 'review_frames')
        os.makedirs(rf)
        for n in range(1, 60):
            with open(os.path.join(rf, 'review_%03d.png' % n), 'wb') as f:
                f.write(b'x')

        # 原片 4 拍：外景 2 拍 → 硬切 → 室内 2 拍。原片没有任何一张门槛帧。
        beats = [
            {'index': 1, 'space': 'exterior', 'start': 0.0, 'end': 5.0,
             'coverage_frames': [{'frame': 'review_001.png', 'timestamp': 0.0},
                                 {'frame': 'review_010.png', 'timestamp': 4.5},
                                 {'frame': 'review_012.png', 'timestamp': 5.0}]},
            {'index': 2, 'space': 'exterior', 'start': 5.0, 'end': 10.0,
             'coverage_frames': [{'frame': 'review_012.png', 'timestamp': 5.0},
                                 {'frame': 'review_020.png', 'timestamp': 9.5},
                                 {'frame': 'review_022.png', 'timestamp': 10.0}]},
            {'index': 3, 'space': 'main_interior', 'start': 10.0, 'end': 15.0,
             'coverage_frames': [{'frame': 'review_022.png', 'timestamp': 10.0},
                                 {'frame': 'review_030.png', 'timestamp': 14.5},
                                 {'frame': 'review_032.png', 'timestamp': 15.0}]},
            {'index': 4, 'space': 'main_interior', 'start': 15.0, 'end': 20.0,
             'coverage_frames': [{'frame': 'review_032.png', 'timestamp': 15.0},
                                 {'frame': 'review_040.png', 'timestamp': 19.5}]},
        ]
        with open(os.path.join(self.tmp, 'timelapse_beats.json'), 'w', encoding='utf-8') as f:
            json.dump({'beats': beats}, f)
        self.beats = beats

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_ladder(self, ladder):
        with open(os.path.join(self.tmp, 'compose_state.json'), 'w', encoding='utf-8') as f:
            json.dump({'beat_ladder': ladder}, f)

    def test_mapping_survives_threshold_expansion(self):
        # 交付梯：原片 1、2 拍 → 3 个过门子拍 → 原片 3、4 拍。位置整体后移 3 格。
        self._write_ladder([
            {'source_beat_index': 1, 'result_space_family': 'exterior'},
            {'source_beat_index': 2, 'result_space_family': 'exterior'},
            {'source_beat_index': None, 'synthetic_beat': 'threshold_ladder',
             'result_space_family': 'exterior'},
            {'source_beat_index': None, 'synthetic_beat': 'threshold_ladder',
             'result_space_family': 'interior'},
            {'source_beat_index': None, 'synthetic_beat': 'threshold_ladder',
             'result_space_family': 'interior'},
            {'source_beat_index': 3, 'result_space_family': 'interior'},
            {'source_beat_index': 4, 'result_space_family': 'interior'},
        ])
        refs, roles, _ = pp.find_reference_frames_with_roles(self.tmp, total_beats=7)
        base = {k: os.path.basename(v) for k, v in refs.items()}

        # 过门之后的两拍挂回原片第 3、4 拍。按位置映射它们会去要第 6、7 拍——原片只有 4 拍。
        self.assertEqual(base[7], 'review_030.png')
        self.assertEqual(base[8], 'review_040.png')
        self.assertEqual(roles[7], 'benchmark')

    def test_threshold_slots_get_envelope_role(self):
        self._write_ladder([
            {'source_beat_index': 1, 'result_space_family': 'exterior'},
            {'source_beat_index': 2, 'result_space_family': 'exterior'},
            {'source_beat_index': None, 'synthetic_beat': 'threshold_ladder',
             'result_space_family': 'exterior'},
            {'source_beat_index': None, 'synthetic_beat': 'threshold_ladder',
             'result_space_family': 'interior'},
            {'source_beat_index': 3, 'result_space_family': 'interior'},
        ])
        refs, roles, _ = pp.find_reference_frames_with_roles(self.tmp, total_beats=5)
        base = {k: os.path.basename(v) for k, v in refs.items()}

        # 门槛侧：翻面前最后一张仍在室外的帧（不是边界帧 review_022）。
        self.assertEqual(base[4], 'review_020.png')
        self.assertEqual(roles[4], 'envelope')
        # 落定侧：翻面后第一张室内帧。
        self.assertEqual(base[5], 'review_022.png')
        self.assertEqual(roles[5], 'envelope')

    def test_camera_reframe_slot_gets_no_ref_at_all(self):
        self._write_ladder([
            {'source_beat_index': 1, 'result_space_family': 'exterior'},
            {'source_beat_index': None, 'synthetic_beat': 'camera_reframe',
             'result_space_family': 'exterior'},
            {'source_beat_index': 2, 'result_space_family': 'exterior'},
        ])
        refs, _roles, _ = pp.find_reference_frames_with_roles(self.tmp, total_beats=3)
        self.assertNotIn(3, refs)   # 纯机位重构，无可对标
        self.assertEqual(os.path.basename(refs[2]), 'review_010.png')
        self.assertEqual(os.path.basename(refs[4]), 'review_020.png')

    def test_boundary_frame_never_leaks_into_the_exterior_slot(self):
        # 没有交付梯的老单退回位置映射，但拍窗切断照样生效：第 2 拍（外景）的交付态
        # 不再是原片第一张室内帧 review_022。
        refs, _roles, _ = pp.find_reference_frames_with_roles(self.tmp, total_beats=4)
        self.assertEqual(os.path.basename(refs[3]), 'review_020.png')

    def test_storyboard_layout_falls_back_to_scene_filenames(self):
        # storyboard 布局：coverage 记的 review_NNN.png 在这个目录里一张都不存在，
        # 只有按原片拍序编号的 scene_NNN.png。去掉这条兜底会让整单挂帧塌成空。
        sb = os.path.join(self.tmp, 'storyboard')
        os.makedirs(sb)
        for n in range(1, 5):
            with open(os.path.join(sb, 'scene_%03d.png' % n), 'wb') as f:
                f.write(b'x')
        shutil.rmtree(os.path.join(self.tmp, 'review_frames'))

        refs, roles, _ = pp.find_reference_frames_with_roles(self.tmp, total_beats=4)
        self.assertEqual(os.path.basename(refs[3]), 'scene_002.png')
        self.assertEqual(roles[3], 'benchmark')

    def test_missing_project_dir_returns_the_full_triple(self):
        # 三元返回的早退分支：漏一个元素，任何拿不到项目目录的调用点都会当场解包失败。
        refs, roles, collage = pp.find_reference_frames_with_roles(
            os.path.join(self.tmp, 'does_not_exist'))
        self.assertEqual((refs, roles, collage), ({}, {}, None))

    def test_compat_wrapper_keeps_two_tuple_shape(self):
        # 老调用点（chain_guard 的锚点分支 / server / candidate_selection）仍然二元解包。
        refs, collage = pp.find_reference_frames_for_project(self.tmp, total_beats=4)
        self.assertEqual(os.path.basename(refs[3]), 'review_020.png')
        self.assertIsNone(collage)

    def test_proportional_fallback_disabled_when_beats_exist(self):
        # 拍表在、但交付梯要求的格数超出原片拍数：多出来的格不挂，绝不按比例硬摊。
        self._write_ladder([{'source_beat_index': n} for n in range(1, 9)])
        refs, _roles, _ = pp.find_reference_frames_with_roles(self.tmp, total_beats=8)
        self.assertEqual(sorted(refs), [1, 2, 3, 4, 5])


class TestFreeVocabularySpaceFamily(unittest.TestCase):
    """E：`space` 是自由词表，不是 'exterior' 这种枚举值。

    2026-08-31 复盘（Tiny House 拖车单）：真实单的 space 写的是 'outdoor trailer yard'
    / 'woodland clearing'，旧判据只做全等匹配，于是每一条外景拍都被判成 interior，
    外景那半条序列一格基准帧都挂不上。本类的 fixture 一律用自由词表原文——用
    'exterior' 当 fixture 正是当初漏掉这个洞的原因。"""

    def test_free_text_exterior_is_not_misread_as_interior(self):
        for space in ('outdoor trailer yard', 'woodland clearing', 'front yard'):
            self.assertEqual(pp._source_space_family({'space': space}), 'exterior', space)

    def test_free_text_interior_stays_interior(self):
        for space in ('main living studio', 'ensuite bathroom', 'cottage interior'):
            self.assertEqual(pp._source_space_family({'space': space}), 'interior', space)

    def test_ladder_map_outranks_the_word_fallback(self):
        # 交付梯自己说了算：词根兜底认不出的空间层，映射表也能判对。
        ladder = [{'observed_space': 'the flats behind the barn',
                   'result_space_family': 'exterior'}]
        fmap = pp._space_family_map_from_ladder(ladder)
        self.assertEqual(pp._source_space_family({'space': 'the flats behind the barn'}, fmap),
                         'exterior')
        # 没有映射表时它落到兜底，判成室内——正是要靠梯子救回来的那一档。
        self.assertEqual(pp._source_space_family({'space': 'the flats behind the barn'}),
                         'interior')

    def test_synthetic_beats_never_enter_the_map(self):
        # 插出来的拍没有原片对应 space，混进映射表会把真实拍的空间层带偏。
        ladder = [{'observed_space': 'main living studio', 'result_space_family': 'exterior',
                   'synthetic_beat': 'camera_reframe'}]
        self.assertEqual(pp._space_family_map_from_ladder(ladder), {})

    def test_crossing_boundary_found_on_free_text_beats(self):
        beats = [{'space': 'outdoor trailer yard'}, {'space': 'outdoor trailer yard'},
                 {'space': 'main living studio'}]
        self.assertEqual(pp._source_crossing_boundary(beats), (2, 3))


class TestSourceBeatAnchorStamping(unittest.TestCase):
    """F：一比一梯子的原片拍序锚点。

    2026-08-31 复盘：锚点原先只在 expand_spatial_transition_beats 里打，而一比一分支
    根本不走那个函数——复刻线交付的每一条梯子 source_beat_index 全是缺席，挂帧一路退到
    位置映射，「交付第 k 拍 == 原片第 k 拍」从此没有任何东西守着。"""

    def test_stamps_from_outline_refs_first(self):
        ladder = [{'outline_refs': [3]}, {'outline_refs': [4]}]
        self.assertEqual(pp.stamp_source_beat_anchors(ladder), 2)
        self.assertEqual([b['source_beat_index'] for b in ladder], [3, 4])

    def test_falls_back_to_position_without_outline_refs(self):
        ladder = [{}, {}, {}]
        pp.stamp_source_beat_anchors(ladder)
        self.assertEqual([b['source_beat_index'] for b in ladder], [1, 2, 3])

    def test_never_overwrites_an_existing_anchor(self):
        ladder = [{'source_beat_index': 9, 'outline_refs': [1]}]
        pp.stamp_source_beat_anchors(ladder)
        self.assertEqual(ladder[0]['source_beat_index'], 9)

    def test_synthetic_beats_keep_their_empty_anchor(self):
        # 空着是一句声明（「原片没拍过这一格」），补上位置号等于把声明改成谎话。
        ladder = [{'synthetic_beat': 'threshold_ladder', 'source_beat_index': None}]
        pp.stamp_source_beat_anchors(ladder)
        self.assertIsNone(ladder[0]['source_beat_index'])


class TestEstablishingWideRef(unittest.TestCase):
    """G：自动找全景帧。

    对标帧要核对的是机位、构图留白、空间骨架与道具尺度——这四件事只有全景说得清。
    窗内有全景就选窗内最靠后的那张（状态仍是本拍的交付态）；整拍都是特写时退到同空间
    最近的一张全景，角色降级成 establishing（它不是本拍的时刻）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.rf = os.path.join(self.tmp, 'review_frames')
        os.makedirs(self.rf)
        for n in range(1, 60):
            with open(os.path.join(self.rf, 'review_%03d.png' % n), 'wb') as f:
                f.write(b'x')
        # 第 1 拍有全景，第 2 拍全程特写，第 3 拍在另一个空间且有全景。
        self.beats = [
            {'index': 1, 'space': 'outdoor trailer yard', 'start': 0.0, 'end': 5.0,
             'coverage_frames': [{'frame': 'review_001.png', 'timestamp': 1.0},
                                 {'frame': 'review_002.png', 'timestamp': 3.0},
                                 {'frame': 'review_003.png', 'timestamp': 4.5}]},
            {'index': 2, 'space': 'outdoor trailer yard', 'start': 5.0, 'end': 10.0,
             'coverage_frames': [{'frame': 'review_010.png', 'timestamp': 6.0},
                                 {'frame': 'review_011.png', 'timestamp': 9.0}]},
            {'index': 3, 'space': 'main living studio', 'start': 10.0, 'end': 15.0,
             'coverage_frames': [{'frame': 'review_020.png', 'timestamp': 11.0},
                                 {'frame': 'review_021.png', 'timestamp': 14.0}]},
        ]
        self.facts = {
            'review_001.png': {'shot_scale': 'wide'},
            'review_002.png': {'shot_scale': 'wide'},
            'review_003.png': {'shot_scale': 'medium'},
            'review_010.png': {'shot_scale': 'close'},
            'review_011.png': {'shot_scale': 'close'},
            'review_020.png': {'shot_scale': 'wide'},
            'review_021.png': {'shot_scale': 'wide'},
        }

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_window_pick_prefers_the_last_wide_frame(self):
        # 窗尾那张是中景，但全景优先：取窗内最靠后的一张全景。
        picked = pp._select_beat_ref_frame(self.beats[0], self.beats[1], self.rf, self.facts,
                                           want_family='exterior')
        self.assertEqual(os.path.basename(picked), 'review_002.png')

    def test_cut_avoidance_still_outranks_the_wide_preference(self):
        # 压在切点上的全景空间语义会跳变：宁可要一张不贴切点的中景。
        beat = dict(self.beats[0], observed_cuts=[1.0, 3.0])
        picked = pp._select_beat_ref_frame(beat, self.beats[1], self.rf, self.facts,
                                           want_family='exterior')
        self.assertEqual(os.path.basename(picked), 'review_003.png')

    def test_tight_target_still_gets_a_tight_ref(self):
        picked = pp._select_beat_ref_frame(self.beats[1], self.beats[2], self.rf, self.facts,
                                           want_family='exterior', want_tight=True)
        self.assertEqual(os.path.basename(picked), 'review_011.png')

    def test_establishing_search_stays_inside_the_same_space(self):
        # 第 2 拍全程特写，同空间最近的一张全景是第 1 拍的 review_002；
        # 第 3 拍的全景在另一个空间，绝不许借。
        picked = pp._nearest_establishing_frame(self.beats, 2, self.rf, self.facts)
        self.assertEqual(os.path.basename(picked), 'review_002.png')

    def test_establishing_prefers_looking_backwards(self):
        # 往后找会拿到一张施工更完整的画面，写手照着它会把还没做的工序提前画出来。
        beats = list(self.beats)
        beats[2] = dict(beats[2], space='outdoor trailer yard')  # 同空间，但在本拍之后
        picked = pp._nearest_establishing_frame(beats, 2, self.rf, self.facts)
        self.assertEqual(os.path.basename(picked), 'review_002.png')

    def test_no_wide_anywhere_in_the_space_returns_nothing(self):
        facts = {k: {'shot_scale': 'close'} for k in self.facts}
        self.assertIsNone(pp._nearest_establishing_frame(self.beats, 2, self.rf, facts))

    def test_all_close_beat_gets_an_establishing_role_end_to_end(self):
        with open(os.path.join(self.tmp, 'timelapse_beats.json'), 'w', encoding='utf-8') as f:
            json.dump({'beats': self.beats}, f)
        with open(os.path.join(self.tmp, 'frame_facts.json'), 'w', encoding='utf-8') as f:
            json.dump({'facts': [dict(v, frame=k) for k, v in self.facts.items()]}, f)
        with open(os.path.join(self.tmp, 'compose_state.json'), 'w', encoding='utf-8') as f:
            json.dump({'beat_ladder': [
                {'source_beat_index': 1, 'result_space_family': 'exterior',
                 'observed_space': 'outdoor trailer yard'},
                {'source_beat_index': 2, 'result_space_family': 'exterior',
                 'observed_space': 'outdoor trailer yard'},
                {'source_beat_index': 3, 'result_space_family': 'interior',
                 'observed_space': 'main living studio'},
            ]}, f)
        refs, roles, _ = pp.find_reference_frames_with_roles(self.tmp, total_beats=3)
        base = {k: os.path.basename(v) for k, v in refs.items()}
        # IMG 2 = 交付第 1 拍：窗内有全景，照常 benchmark。
        self.assertEqual(base[2], 'review_002.png')
        self.assertEqual(roles[2], 'benchmark')
        # IMG 3 = 交付第 2 拍：整拍特写、而原片这一拍没声明自己是特写 → 同空间全景退档。
        self.assertEqual(base[3], 'review_002.png')
        self.assertEqual(roles[3], 'establishing')

    def test_source_beat_declared_tight_keeps_the_close_benchmark(self):
        # 原片这一拍本身就是特写：它的交付态就是一只手的距离，不该被换成全景。
        beats = [dict(b) for b in self.beats]
        beats[1]['shot_scale'] = 'close'
        with open(os.path.join(self.tmp, 'timelapse_beats.json'), 'w', encoding='utf-8') as f:
            json.dump({'beats': beats}, f)
        with open(os.path.join(self.tmp, 'frame_facts.json'), 'w', encoding='utf-8') as f:
            json.dump({'facts': [dict(v, frame=k) for k, v in self.facts.items()]}, f)
        with open(os.path.join(self.tmp, 'compose_state.json'), 'w', encoding='utf-8') as f:
            json.dump({'beat_ladder': [
                {'source_beat_index': 1, 'result_space_family': 'exterior'},
                {'source_beat_index': 2, 'result_space_family': 'exterior'},
                {'source_beat_index': 3, 'result_space_family': 'interior'},
            ]}, f)
        refs, roles, _ = pp.find_reference_frames_with_roles(self.tmp, total_beats=3)
        self.assertEqual(os.path.basename(refs[3]), 'review_011.png')
        self.assertEqual(roles[3], 'benchmark')


class TestCandidateScorerRoleAwareness(unittest.TestCase):
    """H：四选一打分必须分档读挂帧角色。

    打分那段把 IMAGE REF 写成「绝对基准，违者大幅扣分」。benchmark 该这么写，另外两档
    不行：envelope 的机位本来就该不一样，establishing 的施工进度本来就该不一样——不分档
    的话那几拍稳定挑中「最像那张错图」的候选。"""

    def _ref_block(self, role):
        import candidate_selection_pipeline as csp
        src = inspect.getsource(csp.evaluate_and_select_best_candidate)
        self.assertIn('ref_frame_role', src)
        return src

    def test_scorer_accepts_the_role_argument(self):
        import candidate_selection_pipeline as csp
        sig = inspect.signature(csp.evaluate_and_select_best_candidate)
        self.assertIn('ref_frame_role', sig.parameters)
        self.assertEqual(sig.parameters['ref_frame_role'].default, 'benchmark')

    def test_envelope_and_establishing_get_their_own_wording(self):
        src = self._ref_block('any')
        # 包络端点：不判机位。
        self.assertIn('包络端点参考图', src)
        self.assertIn('严禁据此比对机位视角', src)
        # 同空间全景：不判进度。
        self.assertIn('同空间全景参考图', src)
        self.assertIn('严禁据此比对施工进度', src)
        # 基准那一档的原措辞保留。
        self.assertIn('【绝对基准注意】', src)

    def test_pipeline_passes_the_role_through(self):
        import candidate_selection_pipeline as csp
        src = inspect.getsource(csp)
        self.assertIn('find_reference_frames_with_roles', src)
        self.assertIn("eval_kw['ref_frame_role']", src)


if __name__ == '__main__':
    unittest.main()
