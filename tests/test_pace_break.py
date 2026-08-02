"""段内推进节奏（2026-07-31「视频推进跳变」复盘）：

- video_generator.pace_verdict          —— 由速度曲线判「推进是否不匀」
- video_generator.compute_retime_factors —— 时间重映射的时长分配
- video_generator.frame_pair_contract    —— 新增的 too_wide 跨度带
- video_generator.check_video_process    —— 本地判据不受 VLM 开关管辖

判据阈值用两单真实成片标定（20 个施工拍里 10 个命中；停滞窗口系统性地扎堆在
5.0~6.5 秒），这里用合成曲线验证判据方向与边界保护，不复刻具体标定值。
"""
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

import video_generator
from video_generator import (
    _RETIME_MAX_FACTOR,
    _RETIME_MIN_FACTOR,
    _is_camera_move_slot,
    check_video_process,
    compute_retime_factors,
    frame_pair_contract,
    pace_verdict,
)


class TestPaceVerdict(unittest.TestCase):
    """8 秒片段、16 个步长。"""

    DURATION = 8.0

    def _verdict(self, steps):
        return pace_verdict(np.array(steps, dtype=float), self.DURATION)

    def test_even_progression_passes(self):
        broken, reason = self._verdict([8.0] * 16)
        self.assertFalse(broken)
        self.assertTrue(reason.startswith('pace_ok'))

    def test_natural_variation_passes(self):
        """真实的匀速片段步长本来就有起伏（实测 vid_001 峰值/中位 2.1x），不得误杀。"""
        broken, _ = self._verdict(
            [15.8, 17.7, 14.5, 8.6, 7.8, 9.6, 12.2, 7.9,
             4.3, 4.1, 5.1, 5.3, 9.4, 6.2, 8.5, 4.5])
        self.assertFalse(broken)

    def test_mid_clip_stall_is_flagged(self):
        """实测 vid_004 形态：推进到一半停住一秒，再突进补齐差量。"""
        broken, reason = self._verdict(
            [10.7, 6.7, 7.7, 17.7, 19.5, 14.5, 18.8, 14.5,
             13.4, 21.2, 2.1, 1.9, 17.5, 15.2, 6.7, 0.6])
        self.assertTrue(broken)
        self.assertIn('停滞', reason)

    def test_single_stall_step_is_not_enough(self):
        """一步偏低是渲染噪声，不是停滞——要连续 _PACE_STALL_RUN 步才算。"""
        steps = [8.0] * 16
        steps[5] = 0.5
        broken, _ = self._verdict(steps)
        self.assertFalse(broken)

    def test_tail_settle_is_exempt(self):
        """片尾静置是 Out-and-In 契约要求的干净交接帧，不是缺陷。"""
        steps = [8.0] * 14 + [0.3, 0.2]
        broken, _ = self._verdict(steps)
        self.assertFalse(broken)

    def test_head_settle_is_exempt(self):
        """片头静置是同一条接缝的另一端：i2v 起步时先保持输入锚点帧再开始动。

        只豁免片尾时，实测 36 个片段里 5 次 S1 命中有 4 次落在片头第一格。"""
        steps = [0.3, 0.2] + [8.0] * 14
        broken, _ = self._verdict(steps)
        self.assertFalse(broken)

    def test_stall_just_past_head_exempt_is_still_flagged(self):
        """豁免的是起步那两格，不是"片子前半段随便停"——越过豁免区仍要判停滞，
        且报出的秒数要加回豁免偏移（body 下标 != 曲线下标）。"""
        steps = [8.0] * 16
        steps[2] = steps[3] = 0.2
        broken, reason = self._verdict(steps)
        self.assertTrue(broken)
        self.assertIn('停滞', reason)
        self.assertIn('第 1.0', reason)   # 下标 2 × 0.5 秒/格

    def test_surge_is_flagged(self):
        """没有停滞，但差量压在一个窗口里补完。"""
        steps = [5.0] * 16
        steps[8] = 30.0
        broken, reason = self._verdict(steps)
        self.assertTrue(broken)
        self.assertIn('突进', reason)

    def test_absolute_floor_guards_stall_rule(self):
        """整体很猛的片子里 MAD 3.3 的一步仍在正常推进，不得按相对判据算成停滞。

        没有这条绝对下限，时间重映射抬高中位步长之后，一条**变得更匀**的片子会被
        凭空报出停滞。"""
        steps = [30.0] * 16
        steps[6] = steps[7] = 3.3        # 3.3 > _FROZEN_CLIP_MAD，绝对量上还在动
        broken, _ = self._verdict(steps)
        self.assertFalse(broken)

    def test_frozen_clip_is_flagged_by_absolute_rule(self):
        """全程冻结的曲线在相对判据下是"完美匀速"，必须靠绝对阈值兜住。"""
        broken, reason = self._verdict([0.8] * 16)
        self.assertTrue(broken)
        self.assertIn('几乎无变化', reason)

    def test_degenerate_profile_is_flagged_not_crashed(self):
        """过半步长为 0：中位数为 0，比值判据除不了，不能抛异常也不能放行。"""
        broken, _ = self._verdict([0.0] * 12 + [40.0] * 4)
        self.assertTrue(broken)


class TestComputeRetimeFactors(unittest.TestCase):
    def test_total_duration_is_preserved(self):
        steps = [1.0, 20.0, 1.0, 15.0, 3.0, 3.0, 9.0, 12.0,
                 2.0, 18.0, 7.0, 7.0, 4.0, 11.0, 6.0, 5.0]
        factors = compute_retime_factors(steps)
        self.assertAlmostEqual(sum(factors), len(steps), places=4)

    def test_factors_stay_within_bounds(self):
        factors = compute_retime_factors([0.1] * 8 + [50.0] * 8)
        for f in factors:
            self.assertGreaterEqual(f, _RETIME_MIN_FACTOR - 1e-6)
            self.assertLessEqual(f, _RETIME_MAX_FACTOR + 1e-6)

    def test_last_segment_is_pinned(self):
        """片尾静置段绝不压缩：压了就在下一段的拼接点制造新的跳变
        （实测尾帧与锚点帧 MAD 从 ~2 升到 6.0）。"""
        factors = compute_retime_factors([20.0] * 15 + [0.2])
        self.assertAlmostEqual(factors[-1], 1.0, places=6)

    def test_first_segment_is_pinned(self):
        """片头静置段同样绝不压缩：它接的是上一段的片尾静置，是同一条接缝。

        没有这条钉桩时实测 factors[0] 中位 0.54、23 个片段里 9 个顶到 0.5 下限，
        等于把每个剪辑点的缓冲砍掉一半。"""
        factors = compute_retime_factors([0.2] + [20.0] * 15)
        self.assertAlmostEqual(factors[0], 1.0, places=6)

    def test_total_duration_preserved_with_both_ends_pinned(self):
        """两端钉死之后中间段的目标和是 n-2，总时长必须仍然严格不变。"""
        factors = compute_retime_factors([0.1, 0.2] + [30.0] * 12 + [0.3, 0.1])
        self.assertAlmostEqual(sum(factors), 16.0, places=4)
        self.assertAlmostEqual(factors[0], 1.0, places=6)
        self.assertAlmostEqual(factors[-1], 1.0, places=6)

    def test_stalled_window_gets_less_screen_time(self):
        steps = [10.0] * 16
        steps[4] = 0.5
        factors = compute_retime_factors(steps)
        self.assertLess(factors[4], 1.0)
        self.assertGreater(factors[0], factors[4])

    def test_surge_window_gets_more_screen_time(self):
        steps = [5.0] * 16
        steps[9] = 40.0
        factors = compute_retime_factors(steps)
        self.assertGreater(factors[9], 1.0)

    def test_degenerate_input_returns_none(self):
        self.assertIsNone(compute_retime_factors([0.0] * 16))
        self.assertIsNone(compute_retime_factors([1.0]))


class TestFramePairTooWide(unittest.TestCase):
    """22~35 这一带此前静默通过：够不上"疑似空间断裂"，却已超出 8 秒 i2v 能连续
    插值的跨度。判据本身是 MAD 数值分带，这里直接打桩验证分带边界。"""

    def _verdict_at(self, mad):
        with patch.object(video_generator, '_load_gray_thumb') as thumb, \
             patch.object(video_generator.os.path, 'exists', return_value=True):
            thumb.side_effect = [np.zeros((4, 4)), np.full((4, 4), float(mad))]
            return frame_pair_contract('a.png', 'b.png')

    def test_normal_progression_is_ok(self):
        self.assertEqual(self._verdict_at(12.0)[0], 'ok')

    def test_wide_span_is_flagged(self):
        self.assertEqual(self._verdict_at(28.0)[0], 'too_wide')

    def test_break_still_wins_over_wide(self):
        self.assertEqual(self._verdict_at(47.0)[0], 'too_different')

    def test_copy_pair_still_too_similar(self):
        self.assertEqual(self._verdict_at(1.0)[0], 'too_similar')


class TestCameraMoveSlots(unittest.TestCase):
    def test_camera_move_tags_recognised(self):
        for meta in ('HERO', '[BRIDGE]', 'BRIDGE TURN', 'CUT', 'PACE 1.2 [CUT]'):
            self.assertTrue(_is_camera_move_slot(meta), meta)

    def test_ordinary_slot_is_not_camera_move(self):
        for meta in ('', None, 'PACE 1.21'):
            self.assertFalse(_is_camera_move_slot(meta), meta)


class TestLocalProbeRunsDespiteVlmSwitch(unittest.TestCase):
    """显式关闭 VLM 复审时，不该连带把免费的客观测量一起关掉。"""

    ARGS = ('vid.mp4', 's.png', 'e.png', 'prompt text')

    def test_pace_break_warns_even_while_vlm_gate_disabled(self):
        with patch.object(video_generator, 'detect_pace_break',
                          return_value=(True, '本地节奏检测：推进在第 5.0~6.0 秒停滞')):
            action, reason = check_video_process({
                'qaGateLevel': 'standard', 'videoProcessVlmReview': False,
            }, *self.ARGS)
        self.assertEqual(action, 'warn')
        self.assertIn('停滞', reason)

    def test_local_probe_never_rejects(self):
        """本地判据最高只到 warn：拒收要再烧一整段视频额度，而这道判据是提示性的。"""
        for level in ('standard', 'lenient'):
            with patch.object(video_generator, 'detect_pace_break',
                              return_value=(True, 'boom')):
                action, _ = check_video_process({'qaGateLevel': level}, *self.ARGS)
            self.assertEqual(action, 'warn', level)

    def test_camera_move_slot_skips_pace_probe(self):
        def _boom(*a, **k):
            raise AssertionError('运镜拍不适用施工推进的匀速判据')
        with patch.object(video_generator, 'detect_pace_break', _boom):
            action, reason = check_video_process(
                {'qaGateLevel': 'standard', 'videoProcessVlmReview': False},
                *self.ARGS, meta='[HERO]')
        self.assertEqual(action, 'accept')
        self.assertIn('运镜拍', reason)

    def test_off_level_still_short_circuits_everything(self):
        def _boom(*a, **k):
            raise AssertionError('off 档不得做任何测量')
        with patch.object(video_generator, 'detect_pace_break', _boom):
            action, reason = check_video_process({'qaGateLevel': 'off'}, *self.ARGS)
        self.assertEqual(action, 'accept')
        self.assertTrue(reason.startswith('Skipped'))

    def test_probe_failure_fails_open(self):
        with patch.object(video_generator, 'detect_pace_break',
                          return_value=(False, 'skipped:no_duration')):
            action, _ = check_video_process({'qaGateLevel': 'standard'}, *self.ARGS)
        self.assertEqual(action, 'accept')


class TestRetimeKeepsConstantFrameRate(unittest.TestCase):
    """setpts 只改时间戳、不增删帧，concat 出来是变帧率流；交给 libx264 时输出帧率
    由 ffmpeg 猜，压缩段的真实帧被直接丢掉——实测 8 秒片段 192 帧掉到 153 帧、
    avg_frame_rate 24 → 19.1、完全重复帧 10.5% → 27.1%。必须把源帧率锁回输出。"""

    def test_filter_locks_source_fps(self):
        filt = video_generator._retime_filter([1.0] * 4, 8.0, 24)
        self.assertIn('concat=n=4:v=1:a=0,fps=24[vout]', filt)

    def test_filter_omits_fps_when_probe_failed(self):
        """探测不到帧率时宁可保留旧行为，也不要用猜出来的帧率去重采样。"""
        for missing in (None, 0):
            filt = video_generator._retime_filter([1.0] * 4, 8.0, missing)
            self.assertIn('concat=n=4:v=1:a=0[vout]', filt)
            self.assertNotIn('fps=', filt)

    def test_retime_passes_probed_fps_into_filter(self):
        """光有滤镜没用，得真的接到 retime_clip_even 的 ffmpeg 命令上。

        重映射在生产上已停用（见 test_production_retime_is_disabled），这里显式打开
        开关只为把「一旦重新启用就必须锁 CFR」这条契约钉住——掉帧那次就是在开关开着
        的九天里发生的。"""
        stalled = [10.0] * 16
        stalled[6] = stalled[7] = 0.2          # 停在豁免区之外，确保判定为跳变
        broken_profile = (np.array(stalled), 8.0, None)
        with patch.object(video_generator, 'clip_step_profile',
                          return_value=broken_profile), \
             patch.object(video_generator, '_RETIME_ENABLED', True), \
             patch.object(video_generator, '_ffprobe_video_params',
                          return_value={'fps': 30.0, 'duration': 8.0}), \
             patch.object(video_generator.subprocess, 'run') as run:
            run.return_value.returncode = 0
            video_generator.retime_clip_even('a.mp4', 'b.mp4',
                                             tmp_dir=tempfile.gettempdir())
        filters = [call.args[0][call.args[0].index('-filter_complex') + 1]
                   for call in run.call_args_list
                   if '-filter_complex' in call.args[0]]
        self.assertTrue(filters, 'ffmpeg 未被调用或没带 -filter_complex')
        self.assertIn('fps=30', filters[0])


class TestRetimeSkipsEvenClips(unittest.TestCase):
    """无差别重映射实测会把个别本来匀速的片子拉出新的停滞窗口（修 5 伤 1）。
    只对判定为跳变的片段动手，顺带省掉一次整段转码。"""

    def test_even_clip_is_left_untouched(self):
        with patch.object(video_generator, 'clip_step_profile',
                          return_value=(np.array([8.0] * 16), 8.0, None)), \
             patch.object(video_generator, '_RETIME_ENABLED', True), \
             patch.object(video_generator.subprocess, 'run') as run:
            ok, reason = video_generator.retime_clip_even('a.mp4', 'b.mp4',
                                                          tmp_dir=tempfile.gettempdir())
        self.assertFalse(ok)
        self.assertIn('pace_ok', reason)
        run.assert_not_called()

    def test_camera_move_slot_is_not_retimed(self):
        """检测门豁免运镜拍，重映射层却只收到一串路径、拿不到 meta——一道门放行、
        另一道门照着误报动手改。两层判据必须同口径。"""
        with patch.object(video_generator, 'retime_clip_even',
                          return_value=(False, 'skipped:pace_ok')) as retime:
            paths, notes = video_generator.retime_clips_for_merge(
                ['a.mp4', 'b.mp4'], tempfile.gettempdir(),
                metas=['PACE 1.15', 'BRIDGE TURN'])
        self.assertEqual(paths[1], 'b.mp4')            # 原片直接进合并
        self.assertIn('camera_move_slot', notes[1])
        self.assertEqual([c.args[0] for c in retime.call_args_list], ['a.mp4'])

    def test_missing_metas_keeps_legacy_behaviour(self):
        """历史调用方/无 manifest 时按普通拍处理，与改造前一致。"""
        with patch.object(video_generator, 'retime_clip_even',
                          return_value=(False, 'skipped:pace_ok')) as retime:
            video_generator.retime_clips_for_merge(['a.mp4', 'b.mp4'],
                                                   tempfile.gettempdir())
        self.assertEqual(len(retime.call_args_list), 2)

    def test_slot_meta_map_recovers_hero_from_flag(self):
        """is_hero 本是 'HERO' in meta 的派生字段，但重生成时会从旧 manifest 继承，
        存在"只剩 is_hero、meta 里没有 HERO"的条目。"""
        meta_map = video_generator.slot_meta_map({'videos': [
            {'slot': 1, 'meta': 'PACE 1.15'},
            {'slot': 2, 'meta': '', 'is_hero': True},
        ]})
        self.assertFalse(_is_camera_move_slot(meta_map[1]))
        self.assertTrue(_is_camera_move_slot(meta_map[2]))

    def test_production_retime_is_disabled(self):
        """视频阶段不再用像素速度曲线修补上游锚点因果，也不改写 Omni 硬切节拍。"""
        self.assertFalse(video_generator._RETIME_ENABLED)
        with patch.object(video_generator, 'clip_step_profile') as profile:
            ok, reason = video_generator.retime_clip_even(
                'a.mp4', 'b.mp4', tmp_dir=tempfile.gettempdir())
        self.assertFalse(ok)
        self.assertEqual(reason, 'disabled')
        profile.assert_not_called()


if __name__ == '__main__':
    unittest.main()
