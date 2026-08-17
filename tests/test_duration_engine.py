"""单元测试：按秒数拆拍与双轴时序引擎 (tests/test_duration_engine.py)."""

import unittest
import math
from prompt_pipeline.duration_engine import (
    convert_time_axes,
    calculate_beat_word_quota,
    allocate_beat_durations,
    validate_beat_duration_budget,
    _get_beat_weight,
    _build_atempo_filter,
    DEFAULT_SPEED_MULTIPLIER,
    DEFAULT_TARGET_SCREEN_SEC,
    MIN_BEAT_SCREEN_SEC,
    MAX_BEAT_SCREEN_SEC,
    MAX_REWARD_SCREEN_SEC,
)
import prompt_pipeline as pp


class TestDurationEngine(unittest.TestCase):
    """测试双轴时序引擎核心计算与分配。"""

    def test_convert_time_axes_basic(self):
        """测试 2.0x 默认倍速及自定义倍速下的双轴时间换算。"""
        # 屏幕 30s -> 物理 60s
        s, a = convert_time_axes(screen_sec=30.0, speed=2.0)
        self.assertEqual(s, 30.0)
        self.assertEqual(a, 60.0)

        # 物理 60s -> 屏幕 30s
        s, a = convert_time_axes(action_sec=60.0, speed=2.0)
        self.assertEqual(s, 30.0)
        self.assertEqual(a, 60.0)

        # 1.5x 倍速
        s, a = convert_time_axes(screen_sec=20.0, speed=1.5)
        self.assertEqual(s, 20.0)
        self.assertEqual(a, 30.0)

    def test_calculate_beat_word_quota(self):
        """测试旁白字数配额与 20% ASMR 呼吸留白。"""
        # 3.0 秒屏幕时长，中文 4.2 字/秒，20% 留白 (有效语音时间 2.4s)
        # 2.4s * 4.2 = 10.08 -> floor 为 10 字
        res = calculate_beat_word_quota(3.0, lang='zh', wps=4.2, silence_ratio=0.20)
        self.assertEqual(res['max_words'], 10)
        self.assertEqual(res['voiceover_sec'], 2.4)
        self.assertEqual(res['silence_sec'], 0.6)

        # 英文配额
        res_en = calculate_beat_word_quota(4.0, lang='en', wps=3.0, silence_ratio=0.20)
        # 3.2s * 3.0 = 9.6 -> floor 为 9 词
        self.assertEqual(res_en['max_words'], 9)
        self.assertEqual(res_en['voiceover_sec'], 3.2)
        self.assertEqual(res_en['silence_sec'], 0.8)

    def test_atempo_filter_chain(self):
        """测试 FFmpeg atempo 滤镜链合法性。"""
        self.assertEqual(_build_atempo_filter(2.0), 'atempo=2')
        self.assertEqual(_build_atempo_filter(1.5), 'atempo=1.5')
        # 超出 2.0 拆解
        self.assertEqual(_build_atempo_filter(3.0), 'atempo=2,atempo=1.5')
        # 低于 0.5 拆解
        self.assertEqual(_build_atempo_filter(0.4), 'atempo=0.5,atempo=0.8')

    def test_allocate_beat_durations_15s_5beats(self):
        """测试 15 秒 5 拍快剪时序分配与闭环整定。"""
        beats = [
            {'id': 'B01', 'stage': 'demolition', 'operation': '破土', 'stage_scope': 'large', 'package_operations': ['清理']},
            {'id': 'B02', 'stage': 'structural', 'operation': '梁架', 'stage_scope': 'large', 'package_operations': ['立柱', '横梁']},
            {'id': 'B03', 'stage': 'enclosure', 'operation': '封板', 'stage_scope': 'large', 'package_operations': ['保温', '木板']},
            {'id': 'B04', 'stage': 'fixtures', 'operation': '装灯', 'stage_scope': 'small', 'package_operations': ['灯带']},
            {'id': 'B05', 'stage': 'reveal', 'operation': 'reward', 'stage_scope': 'default', 'package_operations': ['全景']},
        ]
        result = allocate_beat_durations(beats, target_total_screen_sec=15.0, speed_multiplier=2.0)
        self.assertEqual(len(result), 5)

        # 验证总屏幕时间闭环精确等于 15.0s
        total_screen = sum(b['screen_duration_sec'] for b in result)
        self.assertAlmostEqual(total_screen, 15.0, places=1)

        # 验证总物理生成时间为 30.0s
        total_action = sum(b['action_duration_sec'] for b in result)
        self.assertAlmostEqual(total_action, 30.0, places=1)

        # 验证每一拍屏幕时长均在下限以上
        for b in result:
            self.assertGreaterEqual(b['screen_duration_sec'], MIN_BEAT_SCREEN_SEC)
            self.assertEqual(b['speed_factor'], 2.0)
            self.assertIn('setpts_expr', b)
            self.assertIn('voiceover_quota', b)

    def test_allocate_beat_durations_30s_8beats(self):
        """测试 30 秒 8 拍标准改造时序分配与拍重调制。"""
        beats = [
            {'id': 'B01', 'stage': 'demolition', 'operation': '清理', 'stage_scope': 'large', 'package_operations': ['清渣']},
            {'id': 'B02', 'stage': 'structural', 'operation': '垫层', 'stage_scope': 'large', 'package_operations': ['碎石', '防潮']},
            {'id': 'B03', 'stage': 'structural', 'operation': '梁架', 'stage_scope': 'large', 'package_operations': ['主梁', '螺栓']},
            {'id': 'B04', 'stage': 'threshold', 'operation': 'threshold', 'stage_scope': 'default'},
            {'id': 'B05', 'stage': 'enclosure', 'operation': '内板', 'stage_scope': 'large', 'package_operations': ['岩棉', '饰面']},
            {'id': 'B06', 'stage': 'surface', 'operation': '地台', 'stage_scope': 'large', 'package_operations': ['地板', '床架']},
            {'id': 'B07', 'stage': 'fixtures', 'operation': '灯具', 'stage_scope': 'small', 'package_operations': ['氛围灯']},
            {'id': 'B08', 'stage': 'reveal', 'operation': 'reward', 'stage_scope': 'default'},
        ]
        result = allocate_beat_durations(beats, target_total_screen_sec=30.0, speed_multiplier=2.0)
        self.assertEqual(len(result), 8)

        # 验证总时长精确对齐 30.0s
        total_screen = sum(b['screen_duration_sec'] for b in result)
        self.assertAlmostEqual(total_screen, 30.0, places=1)

        # 验证重工序（如 B02、B05 双工序 large 拍）获得的屏幕时长高于轻工序（B07 单工序 small 拍）
        b02_dur = next(b['screen_duration_sec'] for b in result if b['id'] == 'B02')
        b07_dur = next(b['screen_duration_sec'] for b in result if b['id'] == 'B07')
        self.assertGreater(b02_dur, b07_dur)

    def test_validate_beat_duration_budget_catches_violations(self):
        """测试预算与门禁校验函数对违规项的拦截。"""
        bad_beats = [
            # 1. 拍长过短（< 1.8s）
            {'id': 'B01', 'stage': 'demolition', 'screen_duration_sec': 1.2, 'delta_weight': 1.0},
            # 2. 相邻拍突变（1.2 -> 5.5，比值 > 1.8）且拍长过长
            {'id': 'B02', 'stage': 'structural', 'screen_duration_sec': 5.5, 'delta_weight': 2.0},
            # 3. 拍重超标（w = 4.2 > 3.4）需要强制拆拍
            {'id': 'B03', 'stage': 'surface', 'screen_duration_sec': 4.0, 'delta_weight': 4.2},
        ]
        violations = validate_beat_duration_budget(bad_beats, target_total_screen_sec=30.0)
        types = [v['type'] for v in violations]

        self.assertIn('total_duration_mismatch', types)
        self.assertIn('beat_too_short', types)
        self.assertIn('duration_jump_too_steep', types)
        self.assertIn('beat_weight_exceeds_ceiling', types)

    def test_prompt_pipeline_exports(self):
        """测试 prompt_pipeline 模块导出的可用性与兼容性。"""
        self.assertTrue(hasattr(pp, 'allocate_beat_durations'))
        self.assertTrue(hasattr(pp, 'validate_beat_duration_budget'))
        self.assertTrue(hasattr(pp, 'convert_time_axes'))
    def test_autobalance_beats_splits_and_merges(self):
        """测试对真实用户案例（包含 7.3s, 9.967s 超长拍与 1.2s 微拍）的自动平衡与拆拍。"""
        from prompt_pipeline import reverse
        beats_doc = {
            'video_duration_sec': 38.0,
            'speed_multiplier': 2.0,
            'beats': [
                {'id': 'B01', 'start': 0.0, 'end': 3.6, 'stage': 'demolition', 'operation': 'clearing', 'package_operations': ['clear']},
                {'id': 'B02', 'start': 3.6, 'end': 10.9, 'stage': 'structural', 'operation': 'framing', 'package_operations': ['frame', 'fasten']}, # 7.3s > 6.0s (拆拍)
                {'id': 'B03', 'start': 10.9, 'end': 14.5, 'stage': 'enclosure', 'operation': 'boarding', 'package_operations': ['sheath']},
                {'id': 'B04', 'start': 14.5, 'end': 19.3, 'stage': 'surface', 'operation': 'plastering', 'package_operations': ['plaster']},
                {'id': 'B05', 'start': 19.3, 'end': 24.8, 'stage': 'floor', 'operation': 'flooring', 'package_operations': ['lay']},
                {'id': 'B06', 'start': 24.8, 'end': 26.0, 'stage': 'furnishing', 'operation': 'furnishing', 'package_operations': ['place']}, # 1.2s < 2.0s (合并)
                {'id': 'B07', 'start': 26.0, 'end': 35.967, 'stage': 'reveal', 'operation': 'reveal', 'package_operations': ['light', 'admire']}, # ~10.0s > 6.0s (拆拍)
            ]
        }
        balanced, count = reverse.autobalance_beats(beats_doc)
        self.assertGreater(count, 0)
        
        # 拆拍后所有拍的时长都在 1.5s ~ 6.0s 之间
        beats = balanced['beats']
        for b in beats:
            span = b['end'] - b['start']
            self.assertLessEqual(span, 6.0)
            self.assertGreaterEqual(span, 1.8)
            # 确认 2x 倍速与旁白字段已注入
            self.assertIn('screen_duration_sec', b)
            self.assertIn('action_duration_sec', b)
            self.assertIn('voiceover_quota', b)
            self.assertEqual(b['action_duration_sec'], round(b['screen_duration_sec'] * 2.0, 1))

        # 序号重新排序 B01 ~ B0N
        expected_ids = [f'B{i:02d}' for i in range(1, len(beats) + 1)]
        self.assertEqual([b['id'] for b in beats], expected_ids)


if __name__ == '__main__':
    unittest.main()
