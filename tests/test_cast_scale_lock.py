# -*- coding: utf-8 -*-
"""人偶物理比例锁：测量 → 写进 packet → 钉进 IMAGE 正文。

2026-08-30 实测（replica_cf9a445bc52b 微缩草原庄园）：交付的九帧里人偶占比随机漂——
IMG 006 人偶压过整个地基、IMG 001/004 又缩成两个点。排查下来四层原因，这组用例逐层钉住：

  1. `synthesize_drift_lock_packet` 曾是完全写死的常量（三个入参一个没用），内容还是真人
     尺度的（1.3m 胸高 / 2.2m 层高 / 3.2×4.5×2.2m 载体），而且**没有任何比例键**。
  2. 整套比例机制（worker_scale_percent）是 VIDEO 单边的，IMAGE 侧一处都没有。
  3. 反推侧其实读到了 1:24（195 次），但没人把它接过来。
  4. 真实 cast_identity 没有尺度记号，反而把系统提示词里那句兜底的「1:24、拇指高」顶掉了。
"""

import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

import prompt_pipeline as pp
from prompt_pipeline import observed_grounding as og
from prompt_pipeline.fast_composer import synthesize_drift_lock_packet


def _facts(ratios):
    """ratios: [(比例分母, 出现次数)] -> 一份 frame_facts 结构。"""
    facts = []
    n = 0
    for denom, count in ratios:
        for _ in range(count):
            n += 1
            facts.append({
                'frame': f'review_{n:03d}.png',
                'cast_appearance': [f'1:{denom} scale African male figurine: slim build, blue shirt'],
            })
    return {'facts': facts}


class TestMeasureCastScale(unittest.TestCase):
    def setUp(self):
        self.job = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.job, ignore_errors=True)

    def _write(self, ratios):
        with open(os.path.join(self.job, 'frame_facts.json'), 'w', encoding='utf-8') as f:
            json.dump(_facts(ratios), f)

    def test_majority_vote_beats_the_stray_misreads(self):
        """线上真实分布：1:24 出现 195 次、1:50 出现 2 次。后者是逐帧读数的偶发误读。"""
        self._write([(24, 195), (50, 2)])
        scale = og.measure_cast_scale({'beats': [{}]}, self.job)
        self.assertIn('1:24', scale)
        self.assertIn('7.1cm', scale, '应当把比例折算成绝对身高：模型对裸比例记号不敏感')
        self.assertNotIn('1:50', scale)

    def test_no_majority_means_no_lock(self):
        """读数自己就没读准时不下锁——把噪声钉死比没有锁更糟。"""
        self._write([(24, 5), (12, 5), (50, 4)])
        self.assertIsNone(og.measure_cast_scale({'beats': [{}]}, self.job))

    def test_absurd_ratio_is_rejected(self):
        """1:170 折算下来 1cm 高，多半是把某个道具错认成人偶。"""
        self._write([(170, 30)])
        self.assertIsNone(og.measure_cast_scale({'beats': [{}]}, self.job))

    def test_no_data_returns_none_not_an_invented_ratio(self):
        self.assertIsNone(og.measure_cast_scale({'beats': [{}]}, self.job))
        self.assertIsNone(og.measure_cast_scale({}, '/nonexistent/path'))

    def test_cast_identity_alone_is_enough(self):
        """老任务没有 frame_facts，但识别项里带比例时仍应量得出来。"""
        scale = og.measure_cast_scale(
            {'beats': [{}]}, self.job,
            cast_identity=['1:24 scale figurine: blue shirt', '1:24 scale figurine: headwrap'])
        self.assertIn('1:24', scale)


class TestPacketNoLongerAHardcodedHumanScaleConstant(unittest.TestCase):
    BEATS = [
        {'camera_angle': 'high_angle', 'lens_feel': 'macro', 'camera_bearing': 'front'},
        {'camera_angle': 'high_angle', 'lens_feel': 'macro', 'camera_bearing': 'front'},
        {'camera_angle': 'low_angle', 'lens_feel': 'wide', 'camera_bearing': 'left'},
    ]

    def test_miniature_packet_carries_no_human_scale_metrics(self):
        """1:24 沙盘上说「相机 1.3m 胸高、层高 2.2m」是范畴错误——等于告诉图像模型
        这个世界是真人尺度的。用户看到的人偶变真人大小就是这么来的。"""
        pk = synthesize_drift_lock_packet('泥屋', self.BEATS, carrier='微缩庄园', is_miniature=True)
        dumped = json.dumps(pk, ensure_ascii=False)
        self.assertNotIn('1.3m', dumped)
        self.assertNotIn('2.2m', dumped)
        self.assertNotIn('3.2m', dumped)
        self.assertIn('diorama', dumped.lower())

    def test_fullscale_packet_keeps_its_human_metrics(self):
        pk = synthesize_drift_lock_packet('地下室', self.BEATS, is_miniature=False)
        self.assertIn('2.2m', json.dumps(pk))

    def test_camera_dna_now_actually_reads_the_beats(self):
        """这三个入参过去一个都没用上，packet 与本单毫无关系。"""
        pk = synthesize_drift_lock_packet('x', self.BEATS, carrier='微缩庄园', is_miniature=True)
        self.assertIn('macro', pk['camera_dna']['lens'], '没吃到观测到的焦段')
        self.assertIn('high_angle', pk['camera_dna']['attitude'], '没吃到观测到的机位')
        self.assertEqual(pk['primary_landmarks'][0]['name'], '微缩庄园', 'carrier 仍然没用上')

    def test_scale_keys_are_written_only_when_measured(self):
        """凭空写一个比例比没有更糟：校验器会拿它当权威，而它是编的。"""
        bare = synthesize_drift_lock_packet('x', self.BEATS, is_miniature=True)
        self.assertNotIn('cast_scale', bare)
        self.assertNotIn('worker_scale_percent', bare)
        locked = synthesize_drift_lock_packet('x', self.BEATS, is_miniature=True,
                                              cast_scale='1:24 scale — about 7.1cm tall')
        self.assertIn('1:24', locked['cast_scale'])


class TestEnforceCastScaleLockOnImages(unittest.TestCase):
    PACKET = {'cast_scale': '1:24 scale — each standing figurine is about 7.1cm tall'}

    BLOCK = (
        '图片提示词\n'
        '图片 1（破旧泥屋）:\nTwo miniature African figurines stand beside a ruined hut.\n\n'
        '图片 2（地基）:\nA close-up of an empty excavated trench with rebar grid. No living thing in frame.\n\n'
        '图片 3（砌墙）:\nThe figurine couple lean in to inspect the first brick course.\n\n'
        '视频提示词\n'
        '视频 1（拆除）:\nA giant hand clears the hut.\n'
    )

    def test_locks_only_the_slots_that_show_living_cast(self):
        locked, report = og.enforce_cast_scale_lock(self.BLOCK, self.PACKET)
        self.assertEqual(report['locked'], [1, 3])
        images, _ = pp._parse_prompt_slots(locked)
        self.assertIn('1:24', images[1]['body'])
        self.assertIn('7.1cm', images[3]['body'])
        self.assertNotIn('1:24', images[2]['body'], '空景帧不该被塞人偶比例')

    def test_video_slots_are_left_alone(self):
        """VIDEO 侧活物大小由首尾 IMAGE 锚点决定；另写一句只会和进退场模板打架。"""
        locked, _ = og.enforce_cast_scale_lock(self.BLOCK, self.PACKET)
        _, videos = pp._parse_prompt_slots(locked)
        self.assertNotIn('1:24', videos[1]['body'])

    def test_idempotent(self):
        """重跑不能叠加：几轮之后同一段里三四句互相矛盾的比例声明，模型只挑一句听。"""
        once, _ = og.enforce_cast_scale_lock(self.BLOCK, self.PACKET)
        twice, _ = og.enforce_cast_scale_lock(once, self.PACKET)
        self.assertEqual(once.lower().count('figurine scale lock:'),
                         twice.lower().count('figurine scale lock:'))

    def test_replaces_a_stale_lock_instead_of_stacking(self):
        once, _ = og.enforce_cast_scale_lock(self.BLOCK, self.PACKET)
        changed, _ = og.enforce_cast_scale_lock(
            once, {'cast_scale': '1:12 scale — about 14.2cm tall'})
        images, _ = pp._parse_prompt_slots(changed)
        self.assertIn('1:12', images[1]['body'])
        self.assertNotIn('1:24', images[1]['body'], '旧锁没被换掉，同段两个比例')

    def test_no_measured_scale_means_no_injection(self):
        same, report = og.enforce_cast_scale_lock(self.BLOCK, {})
        self.assertEqual(same, self.BLOCK)
        self.assertTrue(report['skipped'])

    def test_check_flags_a_self_written_conflicting_ratio(self):
        errs = og.check_cast_scale_lock(
            'Two 1:12 scale figurines stand by the wall.', self.PACKET)
        self.assertTrue(errs)
        self.assertIn('1:24', errs[0])
        self.assertEqual(
            og.check_cast_scale_lock('Two 1:24 scale figurines stand by the wall.', self.PACKET), [])

    def test_check_ignores_the_injected_clause_itself(self):
        """锁句自己带着 1:24，不能被当成"正文自写的冲突比例"。"""
        locked, _ = og.enforce_cast_scale_lock(self.BLOCK, self.PACKET)
        images, _ = pp._parse_prompt_slots(locked)
        self.assertEqual(og.check_cast_scale_lock(images[1]['body'], self.PACKET), [])


class TestCastIdentityScaleBackfill(unittest.TestCase):
    """真数据把兜底那句「1:24、拇指高」顶掉了——知道得越多锁得越松。"""

    def setUp(self):
        self.job = tempfile.mkdtemp()
        with open(os.path.join(self.job, 'frame_facts.json'), 'w', encoding='utf-8') as f:
            json.dump(_facts([(24, 20)]), f)

    def tearDown(self):
        shutil.rmtree(self.job, ignore_errors=True)

    def _compose_captured(self, cast_identity):
        from prompt_pipeline.fast_composer import compose_replica_one_pass
        state = {
            'job_id': 'x', 'job_dir': self.job, 'title': 'miniature diorama villa',
            'beats': {'carrier': '泥屋', 'destiny_zh': '庄园', 'cast_identity': cast_identity,
                      'beats': [{'visible_action': 'a', 'visible_result': 'b'}], 'banned_elements': []},
        }
        seen = {}

        def fake_chat(config, system, user, **kw):
            seen['system'] = system
            raise RuntimeError('stop')

        with patch.object(pp, '_chat', side_effect=fake_chat):
            try:
                compose_replica_one_pass({'skillProfile': 'miniature'}, state)
            except Exception:
                pass
        return seen.get('system', '')

    def test_figurine_without_a_ratio_gets_one_backfilled(self):
        sysp = self._compose_captured([
            'dark-skinned African male figurine: slim build, blue shirt, brown trousers'])
        self.assertIn('1:24', sysp, '识别项没有尺度记号时应当补上量出来的比例')

    def test_the_giant_human_hand_is_not_given_a_figurine_ratio(self):
        """「巨手工匠」是真人尺度的手，补个 1:24 就把整件事说反了。"""
        sysp = self._compose_captured([
            'the craftsman builder: light-skinned adult human right hand and forearm'])
        hand_line = [ln for ln in sysp.split('\n') if 'craftsman builder' in ln]
        self.assertTrue(hand_line)
        self.assertNotIn('1:24', hand_line[0])

    def test_an_existing_ratio_is_not_doubled(self):
        sysp = self._compose_captured(['1:24 scale figurine: blue shirt'])
        line = [ln for ln in sysp.split('\n') if 'blue shirt' in ln][0]
        self.assertEqual(line.count('1:24'), 1)


if __name__ == '__main__':
    unittest.main()
