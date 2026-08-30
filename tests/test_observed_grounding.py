# -*- coding: utf-8 -*-
"""原片实拍画面接回合成前后两端（prompt_pipeline.observed_grounding）。

2026-08-30 实测（replica_cf9a445bc52b 微缩草原庄园）：IMG 001 与原片对标帧完全对不上。
合成器从头到尾看不到任何一帧原片，机位/景别/三区布局/前景植被只能凭一句动作描述空想。
这组用例钉住两件事——前置事实卡必须按槽位对号入座、后置对帧订正必须只动画面不动工序。
"""

import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

import prompt_pipeline as pp
from prompt_pipeline import observed_grounding as og


def _fact(frame, **kw):
    base = {
        'frame': frame,
        'subject': f'subject of {frame}',
        'shot_scale': 'wide',
        'camera_angle': 'high_angle',
        'camera_bearing': 'front',
        'lens_feel': 'tele',
        'subject_placement': 'hut centred, filling half the frame height, horizon on the upper third',
        'spatial_zones': {'floor': 'dry soil with sparse low green weed seedlings'},
        'materials': ['dried straw thatch'],
        'material_specs': ['mud plaster approx 8mm thick'],
        'cast_appearance': ['male figurine in blue shirt', 'female figurine in headwrap'],
        'micro_traces': ['hairline drying cracks'],
        'completion_extent': f'state at {frame}',
    }
    base.update(kw)
    return base


class TestObservedGrounding(unittest.TestCase):
    def setUp(self):
        self.job = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.job, 'review_frames'), exist_ok=True)
        self.frames = []
        for n in range(1, 7):
            name = f'review_{n:03d}.png'
            path = os.path.join(self.job, 'review_frames', name)
            with open(path, 'wb') as f:
                f.write(b'\x89PNG\r\n\x1a\n')
            self.frames.append(name)
        with open(os.path.join(self.job, 'frame_facts.json'), 'w', encoding='utf-8') as f:
            json.dump({'facts': [_fact(n) for n in self.frames]}, f)

        self.beats_doc = {
            'beats': [
                {'visible_action': '拆除茅屋', 'visible_result': '土台平整',
                 'camera_move': 'locked', 'cast_action': '人偶后退张望',
                 'observed_shot_count': 2, 'observed_shot_scales': ['wide', 'macro'],
                 'coverage_frames': [{'frame': 'review_001.png', 'timestamp': 0.0},
                                     {'frame': 'review_002.png', 'timestamp': 1.0},
                                     {'frame': 'review_003.png', 'timestamp': 2.0}],
                 'evidence_frames': ['review_001.png', 'review_002.png', 'review_003.png']},
                {'visible_action': '铺设地板', 'visible_result': '地板就位',
                 'camera_move': 'push in', 'cast_action': '人偶走上平台',
                 'coverage_frames': [{'frame': 'review_004.png', 'timestamp': 3.0},
                                     {'frame': 'review_005.png', 'timestamp': 4.0},
                                     {'frame': 'review_006.png', 'timestamp': 5.0}],
                 'evidence_frames': ['review_004.png', 'review_006.png']},
            ]
        }

    def tearDown(self):
        shutil.rmtree(self.job, ignore_errors=True)

    # ---------------- 前置：事实卡 ----------------

    def test_digest_slots_map_to_the_right_frames(self):
        """IMAGE 1 = 第 1 拍起点帧；IMAGE k+1 = 第 k 拍终点帧；VIDEO k = 第 k 拍。

        对错一位比没有更糟：写手会照着下一拍的画面写这一拍。
        """
        d = og.build_observed_digests(self.beats_doc, self.job)
        self.assertEqual(sorted(d['images']), [1, 2, 3])
        self.assertEqual(sorted(d['videos']), [1, 2])
        self.assertIn('review_001.png', d['images'][1])       # 第 1 拍起点
        self.assertIn('review_003.png', d['images'][2])       # 第 1 拍终点
        self.assertIn('review_006.png', d['images'][3])       # 第 2 拍终点
        self.assertTrue(d['image_frames'][1][0].endswith('review_001.png'))
        self.assertEqual(len(d['video_frames'][1]), 3)

    def test_digest_carries_the_fields_the_failed_run_lacked(self):
        """景别、构图占比、前景植被——正是那一单跑偏的三处。"""
        d = og.build_observed_digests(self.beats_doc, self.job)
        anchor = d['images'][1]
        self.assertIn('shot scale: wide', anchor)
        self.assertIn('horizon on the upper third', anchor)
        self.assertIn('sparse low green weed seedlings', anchor)

    def test_video_digest_carries_motion_not_a_second_copy_of_cast_looks(self):
        """运动侧要的是人偶这一拍在做什么，不是再抄一遍他们长什么样（最大的一栏）。"""
        d = og.build_observed_digests(self.beats_doc, self.job)
        v = d['videos'][1]
        self.assertIn('人偶后退张望', v)
        self.assertIn('Observed shot count in this beat: 2', v)
        self.assertIn('wide -> macro', v)
        self.assertNotIn('Observed living cast appearance', v)

    def test_missing_frame_facts_degrades_instead_of_raising(self):
        os.remove(os.path.join(self.job, 'frame_facts.json'))
        d = og.build_observed_digests(self.beats_doc, self.job)
        self.assertEqual(d['images'], {})
        # 拍级字段还在，运动那份仍然写得出来
        self.assertIn('Observed camera move: locked', d['videos'][1])

    def test_full_block_never_silently_drops_a_slot(self):
        """撞上限时按栏目降级、全片均摊，绝不砍掉末尾几拍。

        字符截断的后果是后段一行画面依据都没有，而写手看不出这一点——他会照常凭空想，
        跟改动之前一模一样。
        """
        with patch.object(og, '_FULL_BLOCK_CEILING', 2200):
            block = og.observed_digest_block(
                og.build_observed_digests(self.beats_doc, self.job), 2)
        for n in (1, 2, 3):
            self.assertIn(f'[IMAGE {n} —', block)
        for n in (1, 2):
            self.assertIn(f'[VIDEO {n} —', block)
        self.assertIn('omitted for length', block)
        # 降级后仍必须留着最要紧的那条
        self.assertIn('shot scale: wide', block)

    def test_full_block_names_the_slots_it_cannot_fit(self):
        """连最小档都装不下时按槽位整条丢并**点名**，不做半行处的字符截断。

        字符截断读的人无从知道后面还有几拍没列；点名之后写手至少知道自己这几拍没有画面
        依据。悄悄少给和明说少给，是两回事。
        """
        with patch.object(og, '_FULL_BLOCK_CEILING', 900):
            block = og.observed_digest_block(
                og.build_observed_digests(self.beats_doc, self.job), 2)
        self.assertIn('No observations are supplied for these slots', block)
        self.assertNotIn('truncated for length', block)
        # 点名的槽位必须真的不在正文里，别自相矛盾
        note = block.split('No observations are supplied for these slots:')[1]
        for label in ('IMAGE 3', 'VIDEO 2'):
            if label in note:
                self.assertNotIn(f'[{label} —', block)

    def test_beat_digest_block_only_carries_this_beat(self):
        """逐拍注入只发本拍：整份按拍数乘一遍会让写手在写第 1 拍时读到第 2 拍的画面。"""
        d = og.build_observed_digests(self.beats_doc, self.job)
        block = og.beat_digest_block(d, 1)
        self.assertIn('[VIDEO 1 —', block)
        self.assertIn('[IMAGE 2 —', block)
        self.assertNotIn('[VIDEO 2 —', block)
        self.assertNotIn('[IMAGE 3 —', block)

    # ---------------- 后置：对帧订正 ----------------

    PROMPT_BLOCK = (
        '图片提示词\n'
        '图片 1（破旧泥屋）:\nA high-angle close-up of a ruined miniature hut on bare soil, no vegetation anywhere.\n\n'
        '图片 2（土台平整）:\nA close-up of the cleared circular earth platform on bare soil.\n\n'
        '图片 3（地板就位）:\nA close-up of the finished plank floor.\n\n'
        '视频提示词\n'
        '视频 1（拆除茅屋）:\nA giant hand clears the hut while the camera slowly orbits around the diorama.\n\n'
        '视频 2（铺设地板）:\nA giant hand lays plank flooring while the camera orbits.\n'
    )

    @staticmethod
    def _reply(image=None, video=None, changed=('framing widened',)):
        payload = {'changed': list(changed)}
        if image is not None:
            payload['image_prompt'] = image
        if video is not None:
            payload['video_prompt'] = video
        return json.dumps(payload)

    def test_post_pass_rewrites_both_image_and_video_slots(self):
        long_img = ('An eye-level wide macro frame of the ruined miniature hut occupying about half '
                    'the frame height, sparse low green scrub across the foreground, horizon on the '
                    'upper third, open savannah margin on both sides of the structure.')
        long_vid = ('A giant hand clears the hut from a locked wide macro setup, cutting once into a '
                    'macro insert on the collapsing mud wall, while the two figurines step back and '
                    'track the falling thatch.')
        with patch.object(pp, '_multimodal_chat',
                          return_value=self._reply(image=long_img, video=long_vid)):
            block, report = og.ground_prompt_block_on_observed(
                {}, self.PROMPT_BLOCK, self.beats_doc, self.job)

        self.assertIn('sparse low green scrub', block)
        self.assertIn('cutting once into a', block)
        self.assertTrue(report['checked'] >= 2)
        self.assertTrue(report['corrected_images'])
        self.assertTrue(report['corrected_videos'])
        self.assertTrue(report['notes'])

    def test_post_pass_checks_the_opening_anchor_against_the_start_frame(self):
        """IMAGE 1 的依据是第 1 拍的**起点**帧，跟本拍终点不是同一个画面。"""
        seen = []

        def fake(config, system, user_text, image_paths, **kw):
            seen.append((user_text, tuple(os.path.basename(p) for p in image_paths)))
            return self._reply(image='x' * 400, video='y' * 400)

        with patch.object(pp, '_multimodal_chat', side_effect=fake):
            og.ground_prompt_block_on_observed({}, self.PROMPT_BLOCK, self.beats_doc, self.job)

        anchor_calls = [c for c in seen if 'IMAGE 1 prompt' in c[0]]
        self.assertEqual(len(anchor_calls), 1, '开场锚点没有单独对着起始帧校一次')
        self.assertEqual(anchor_calls[0][1], ('review_001.png',))

    def test_post_pass_rejects_a_truncated_rewrite(self):
        """模型敷衍/截断出来的短稿不能覆盖原稿——那比不改更糟。"""
        with patch.object(pp, '_multimodal_chat', return_value=self._reply(image='wider.', video='ok.')):
            block, report = og.ground_prompt_block_on_observed(
                {}, self.PROMPT_BLOCK, self.beats_doc, self.job)
        self.assertEqual(block, self.PROMPT_BLOCK)
        self.assertEqual(report['corrected_images'], [])
        self.assertEqual(report['corrected_videos'], [])

    def test_post_pass_survives_unparseable_and_failing_calls(self):
        with patch.object(pp, '_multimodal_chat', return_value='sorry, I cannot do that'):
            block, report = og.ground_prompt_block_on_observed(
                {}, self.PROMPT_BLOCK, self.beats_doc, self.job)
        self.assertEqual(block, self.PROMPT_BLOCK)

        with patch.object(pp, '_multimodal_chat', side_effect=RuntimeError('gateway down')):
            block, report = og.ground_prompt_block_on_observed(
                {}, self.PROMPT_BLOCK, self.beats_doc, self.job)
        self.assertEqual(block, self.PROMPT_BLOCK)
        self.assertTrue(report['checked'] >= 1)

    def test_post_pass_without_frames_reports_instead_of_pretending(self):
        shutil.rmtree(os.path.join(self.job, 'review_frames'))
        block, report = og.ground_prompt_block_on_observed(
            {}, self.PROMPT_BLOCK, self.beats_doc, self.job)
        self.assertEqual(block, self.PROMPT_BLOCK)
        self.assertTrue(report['skipped'])

    def test_post_pass_scope_forbids_touching_construction_progress(self):
        """SCOPE 是这道的安全带：拍表是用户逐拍核对过的，视觉模型只看得见三张图。"""
        sys_prompt = og._VERIFY_SYSTEM
        self.assertIn('You may NOT touch', sys_prompt)
        self.assertIn('never regress a finished state', sys_prompt)
        self.assertIn('correcting the photography, not re-planning the build', sys_prompt)


class TestObservedGroundingWiring(unittest.TestCase):
    """两条链路都要真的把事实卡发出去——只建不发是 2026-08-30 那个坑的形状。"""

    DIGESTS = {
        'images': {1: 'Observed camera: shot scale: wide', 2: 'IMG2 FACTS'},
        'videos': {1: 'VID1 FACTS'},
        'image_frames': {}, 'video_frames': {},
    }

    def test_fast_composer_user_prompt_carries_the_observed_block(self):
        from prompt_pipeline.fast_composer import build_fast_composer_user_prompt
        beats = [{'visible_action': 'a', 'visible_result': 'b'}]
        text = build_fast_composer_user_prompt('t', 'th', beats, observed_block='OBSERVED-MARKER')
        self.assertIn('OBSERVED-MARKER', text)
        # 不传就一字不变（非复刻线走的正是这条）
        self.assertNotIn('OBSERVED-MARKER',
                         build_fast_composer_user_prompt('t', 'th', beats))

    def test_batch_user_message_carries_the_per_beat_block(self):
        contracts = {1: {'beat': {'operation': 'clearing'}}, 2: {'beat': {'operation': 'flooring'}}}
        with patch.object(pp, '_beat_block_text', side_effect=lambda i, c: f'BEAT {i} BLOCK'):
            msg = pp._build_batch_user_message([1, 2], contracts, 'anchor',
                                               observed_digests=self.DIGESTS)
        self.assertIn('OBSERVED ORIGINAL FOOTAGE (BEAT 1)', msg)
        self.assertIn('VID1 FACTS', msg)
        self.assertIn('IMG2 FACTS', msg)
        # 第 2 拍没有事实卡，不应凭空造一个空标题出来
        self.assertNotIn('OBSERVED ORIGINAL FOOTAGE (BEAT 2)', msg)

        with patch.object(pp, '_beat_block_text', side_effect=lambda i, c: f'BEAT {i} BLOCK'):
            plain = pp._build_batch_user_message([1, 2], contracts, 'anchor')
        self.assertNotIn('OBSERVED ORIGINAL FOOTAGE', plain)


if __name__ == '__main__':
    unittest.main()
