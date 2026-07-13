import json
import os
import shutil
import tempfile
import unittest
from contextlib import contextmanager
from unittest.mock import patch

import server_common
import prompt_pipeline
from prompt_pipeline import (
    is_skipped_verdict,
    is_warn_verdict,
    run_vlm_qa_check,
    check_landmark_drift,
    check_anchor_frame_compliance,
    run_frame_qa_check,
)
from server_common import qa_gate_level, effective_config


@contextmanager
def _gate_sources(server_cfg=None, env_level=None):
    """隔离档位解析的三个来源：清空 SERVER_CONFIG、剔除/注入环境变量。
    否则测试结果会随开发机 server_config.json 里的 qaGateLevel 漂移。"""
    env = {k: v for k, v in os.environ.items() if k != 'SPARK_QA_GATE_LEVEL'}
    if env_level is not None:
        env['SPARK_QA_GATE_LEVEL'] = env_level
    with patch.dict(os.environ, env, clear=True), \
         patch.dict(server_common.SERVER_CONFIG, server_cfg or {}, clear=True):
        yield


class TestQaGateLevelResolution(unittest.TestCase):
    """qaGateLevel 档位解析：请求 config > server_config.json > 环境变量，
    非法值一律回退 standard（质检门静默消失比误杀更危险）。"""

    def test_default_is_standard(self):
        with _gate_sources():
            self.assertEqual(qa_gate_level({}), 'standard')
            self.assertEqual(qa_gate_level(None), 'standard')

    def test_request_config_wins_over_server_config(self):
        with _gate_sources(server_cfg={'qaGateLevel': 'off'}):
            self.assertEqual(qa_gate_level({'qaGateLevel': 'lenient'}), 'lenient')

    def test_server_config_used_when_request_lacks_key(self):
        with _gate_sources(server_cfg={'qaGateLevel': 'lenient'}):
            self.assertEqual(qa_gate_level({}), 'lenient')

    def test_env_var_used_as_last_resort(self):
        with _gate_sources(env_level='off'):
            self.assertEqual(qa_gate_level({}), 'off')

    def test_invalid_value_falls_back_to_standard(self):
        with _gate_sources():
            self.assertEqual(qa_gate_level({'qaGateLevel': 'yolo'}), 'standard')
        with _gate_sources(server_cfg={'qaGateLevel': 123}):
            self.assertEqual(qa_gate_level({}), 'standard')

    def test_value_is_case_insensitive(self):
        with _gate_sources():
            self.assertEqual(qa_gate_level({'qaGateLevel': ' LENIENT '}), 'lenient')

    def test_effective_config_passes_level_through_in_managed_mode(self):
        """服务端托管模式的白名单透传：漏掉这项就会复刻 imageEditFallbackModel
        当年『配置了但从未生效』的静默失效 bug。"""
        with _gate_sources(), patch.object(server_common, 'SERVER_MANAGED', True):
            merged = effective_config({'qaGateLevel': 'lenient'})
            self.assertEqual(merged.get('qaGateLevel'), 'lenient')
            self.assertEqual(qa_gate_level(merged), 'lenient')


class TestOffLevelSkipsAllGates(unittest.TestCase):
    """off 档：三个视觉门都不发 VLM 请求、直接放行，且带 Skipped 标记留痕
    （manifest 会记 auto_approved_degraded，而不是伪装成真实通过）。"""

    def test_all_three_gates_skip_without_calling_vlm(self):
        config = {'qaGateLevel': 'off'}
        with _gate_sources(), \
             patch.object(prompt_pipeline, '_multimodal_chat') as chat:
            verdicts = [
                run_vlm_qa_check(config, 'a.webp', 'b.webp', 'video prompt'),
                check_landmark_drift(config, 'img1.webp', 'imgN.webp'),
                check_anchor_frame_compliance(config, 'img1.webp', 'prompt', {}, {}),
            ]
        chat.assert_not_called()
        for passed, reason in verdicts:
            self.assertTrue(passed)
            self.assertTrue(is_skipped_verdict(reason))


class TestLenientAdjacentCheck(unittest.TestCase):
    """lenient 档邻帧质检：换用只拦 4 类硬伤的宽松提示词；
    PASS_WITH_WARNINGS 归一化为 WARN 放行，FAIL 仍然拦截。"""

    _CONFIG = {'qaGateLevel': 'lenient'}

    def test_uses_lenient_prompt(self):
        with _gate_sources(), \
             patch.object(prompt_pipeline, '_multimodal_chat', return_value='PASS') as chat:
            passed, reason = run_vlm_qa_check(self._CONFIG, 'a.webp', 'b.webp', 'video prompt')
        self.assertTrue(passed)
        self.assertFalse(is_warn_verdict(reason))
        system_prompt = chat.call_args.args[1]
        self.assertIn('LENIENT', system_prompt)
        self.assertIn('HARD FAILURES', system_prompt)
        self.assertNotIn('strict, professional', system_prompt)

    def test_pass_with_warnings_becomes_warn_verdict(self):
        with _gate_sources(), \
             patch.object(prompt_pipeline, '_multimodal_chat',
                          return_value='PASS_WITH_WARNINGS: 构图轻微偏移'):
            passed, reason = run_vlm_qa_check(self._CONFIG, 'a.webp', 'b.webp', 'video prompt')
        self.assertTrue(passed)
        self.assertTrue(is_warn_verdict(reason))
        self.assertIn('构图轻微偏移', reason)
        self.assertFalse(is_skipped_verdict(reason))

    def test_hard_failure_still_fails(self):
        with _gate_sources(), \
             patch.object(prompt_pipeline, '_multimodal_chat',
                          return_value='FAIL: 前后帧完全相同，编辑未执行'):
            passed, reason = run_vlm_qa_check(self._CONFIG, 'a.webp', 'b.webp', 'video prompt')
        self.assertFalse(passed)
        self.assertIn('完全相同', reason)

    def test_api_error_still_fails_open_with_skip_marker(self):
        config = dict(self._CONFIG)
        with _gate_sources(), \
             patch.object(prompt_pipeline, '_multimodal_chat',
                          side_effect=RuntimeError('vlm endpoint down')):
            passed, reason = run_vlm_qa_check(config, 'a.webp', 'b.webp', 'video prompt')
        self.assertTrue(passed)
        self.assertTrue(is_skipped_verdict(reason))
        self.assertEqual(config['_skipped_checks'], 1)

    def test_standard_prompt_untouched_by_default(self):
        with _gate_sources(), \
             patch.object(prompt_pipeline, '_multimodal_chat', return_value='PASS') as chat:
            run_vlm_qa_check({}, 'a.webp', 'b.webp', 'video prompt')
        system_prompt = chat.call_args.args[1]
        self.assertIn('strict, professional', system_prompt)
        self.assertNotIn('HARD FAILURES', system_prompt)


class TestLenientDriftBackstopDisabled(unittest.TestCase):
    """lenient 档停用跨帧地标漂移复查——这道门正是『构图/视角漂移』误杀的主力。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.img1 = os.path.join(self.tmp, 'img_001.webp')
        self.target = os.path.join(self.tmp, 'img_005.webp')
        for p in (self.img1, self.target):
            with open(p, 'wb') as f:
                f.write(b'x')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_direct_call_is_skipped_in_lenient(self):
        with _gate_sources(), \
             patch.object(prompt_pipeline, '_multimodal_chat') as chat:
            passed, reason = check_landmark_drift({'qaGateLevel': 'lenient'}, self.img1, self.target)
        chat.assert_not_called()
        self.assertTrue(passed)
        self.assertTrue(is_skipped_verdict(reason))

    def test_combined_qa_skips_drift_and_keeps_adjacent_verdict(self):
        with _gate_sources(), \
             patch.object(prompt_pipeline, 'run_vlm_qa_check',
                          return_value=(True, 'WARN: 视角轻微偏移')), \
             patch.object(prompt_pipeline, 'check_landmark_drift',
                          return_value=(False, 'FAIL: 地标漂移')) as drift:
            passed, reason = run_frame_qa_check(
                {'qaGateLevel': 'lenient'}, self.img1, 'prev.webp', self.target,
                'video prompt', seq=5)
        drift.assert_not_called()
        self.assertTrue(passed)
        self.assertEqual(reason, 'WARN: 视角轻微偏移')

    def test_standard_still_runs_drift_backstop(self):
        with _gate_sources(), \
             patch.object(prompt_pipeline, 'run_vlm_qa_check', return_value=(True, 'PASS')), \
             patch.object(prompt_pipeline, 'check_landmark_drift',
                          return_value=(False, 'FAIL: 地标漂移')) as drift:
            passed, reason = run_frame_qa_check(
                {}, self.img1, 'prev.webp', self.target, 'video prompt', seq=5)
        drift.assert_called_once()
        self.assertFalse(passed)
        self.assertIn('地标漂移', reason)


class TestLenientAnchorGate(unittest.TestCase):
    """lenient 档 IMAGE 1 锚点门：只拦人物机械/文字水印/完全跑题，
    损伤严重度、题材气质等降为 WARN 放行。"""

    _CONFIG = {'qaGateLevel': 'lenient'}
    _BRIEF = {'carrier': '巨树太空舱', 'env': '原始森林', 'trauma': '藤蔓吞没'}

    def test_uses_lenient_prompt_and_passes_warn(self):
        with _gate_sources(), \
             patch.object(prompt_pipeline, '_multimodal_chat',
                          return_value='PASS_WITH_WARNINGS: 损伤程度偏轻') as chat:
            passed, reason = check_anchor_frame_compliance(
                self._CONFIG, 'img1.webp', 'prompt', {}, self._BRIEF)
        self.assertTrue(passed)
        self.assertTrue(is_warn_verdict(reason))
        system_prompt = chat.call_args.args[1]
        self.assertIn('LENIENT', system_prompt)
        self.assertIn('巨树太空舱', system_prompt)

    def test_hard_failure_still_fails(self):
        with _gate_sources(), \
             patch.object(prompt_pipeline, '_multimodal_chat',
                          return_value='FAIL: 画面中出现一名工人'):
            passed, reason = check_anchor_frame_compliance(
                self._CONFIG, 'img1.webp', 'prompt', {}, self._BRIEF)
        self.assertFalse(passed)

    def test_api_error_fails_open(self):
        config = dict(self._CONFIG)
        with _gate_sources(), \
             patch.object(prompt_pipeline, '_multimodal_chat',
                          side_effect=RuntimeError('boom')):
            passed, reason = check_anchor_frame_compliance(
                config, 'img1.webp', 'prompt', {}, self._BRIEF)
        self.assertTrue(passed)
        self.assertTrue(is_skipped_verdict(reason))


class TestGateResponseFormatDrift(unittest.TestCase):
    """判定回复的格式漂移容错：备注要求用中文写，模型常输出全角冒号/空格代下划线。
    本仓库此前在纯文本配额信号、_strip_code_fences 上都栽过同类格式漂移的真实事故。"""

    _CONFIG = {'qaGateLevel': 'lenient'}

    def test_fullwidth_colon_note_is_preserved(self):
        with _gate_sources(), \
             patch.object(prompt_pipeline, '_multimodal_chat',
                          return_value='PASS_WITH_WARNINGS：镜头轻微偏移'):
            passed, reason = run_vlm_qa_check(self._CONFIG, 'a.webp', 'b.webp', 'video prompt')
        self.assertTrue(passed)
        self.assertEqual(reason, 'WARN: 镜头轻微偏移')

    def test_space_variant_still_counts_as_warn(self):
        with _gate_sources(), \
             patch.object(prompt_pipeline, '_multimodal_chat',
                          return_value='PASS WITH WARNINGS: 视角偏移'):
            passed, reason = run_vlm_qa_check(self._CONFIG, 'a.webp', 'b.webp', 'video prompt')
        self.assertTrue(passed)
        self.assertTrue(is_warn_verdict(reason))
        self.assertIn('视角偏移', reason)

    def test_bare_pass_with_warnings_without_note(self):
        with _gate_sources(), \
             patch.object(prompt_pipeline, '_multimodal_chat',
                          return_value='PASS_WITH_WARNINGS'):
            passed, reason = run_vlm_qa_check(self._CONFIG, 'a.webp', 'b.webp', 'video prompt')
        self.assertTrue(passed)
        self.assertTrue(is_warn_verdict(reason))


class TestOffLevelAnchorReuse(unittest.TestCase):
    """qaGateLevel=off 下分步渲染的锚点复用：off 档过门只会得到 auto_approved_degraded
    留痕，若照旧拒绝复用，每次续跑都会重渲一张新首帧接旧链（帧 2..N 仍挂在旧首帧上），
    正是指纹复用机制要防的锚链错位。standard 档对 degraded 记录仍必须重新过门。"""

    _PROMPT_BLOCK = (
        "图片提示词\n图片 1:\nfirst frame prompt\n\n图片 2:\nsecond frame prompt\n\n"
        "视频提示词\n视频 1:\nvideo one\n"
    )

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old_output_root = server_common.OUTPUT_ROOT
        server_common.OUTPUT_ROOT = self.tmp
        self.title = 'test_off_level_anchor_reuse'
        self.project_dir = server_common._get_project_dir(self.title)
        os.makedirs(os.path.join(self.project_dir, 'frames'), exist_ok=True)

    def tearDown(self):
        server_common.OUTPUT_ROOT = self.old_output_root
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_degraded_anchor(self):
        from pipeline_orchestrator import _prompt_fingerprint
        with open(os.path.join(self.project_dir, 'manifest.json'), 'w', encoding='utf-8') as f:
            json.dump({'frames': [{
                'sequence': 1,
                'quality_gate': 'auto_approved_degraded',
                'vlm_qa_reason': 'Skipped (qaGateLevel=off: 质检门已关闭)',
                'anchor_prompt_sha256': _prompt_fingerprint('first frame prompt'),
            }]}, f)
        with open(os.path.join(self.project_dir, 'frames', 'img_001.webp'), 'wb') as f:
            f.write(b'fake webp bytes')

    def _run_staged(self, config):
        from pipeline_orchestrator import run_staged_frame_rendering
        with patch('pipeline_orchestrator.generate_frame_sequence'), \
             patch('pipeline_orchestrator.check_anchor_frame_compliance',
                   return_value=(True, 'Skipped (qaGateLevel=off: 质检门已关闭)')) as mock_gate, \
             patch('pipeline_orchestrator.generate_video_sequence',
                   return_value={'videos': [{'slot': 1, 'status': 'success'}]}):
            result = run_staged_frame_rendering(config, self.title, self._PROMPT_BLOCK)
        return result, mock_gate

    def test_off_level_reuses_degraded_anchor(self):
        self._write_degraded_anchor()
        with _gate_sources():
            result, mock_gate = self._run_staged({'qaGateLevel': 'off'})
        self.assertEqual(result['status'], 'completed')
        mock_gate.assert_not_called()  # 复用成功：没有重新过门（也就不会重渲首帧）

    def test_standard_level_still_regates_degraded_anchor(self):
        self._write_degraded_anchor()
        with _gate_sources():
            result, mock_gate = self._run_staged({})
        self.assertEqual(result['status'], 'completed')
        mock_gate.assert_called_once()  # degraded 在 standard 档仍不可复用


class TestWarnVerdictLandsInManifest(unittest.TestCase):
    """API 帧生成路径：WARN 放行不触发重试，quality_gate 记 auto_approved，
    告警文本落进 manifest 的 vlm_qa_reason 供人工复核。"""

    _PROMPT_BLOCK = """图片 1:
first frame prompt

图片 2:
second frame prompt

视频 1:
visible construction change
"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old_output_root = server_common.OUTPUT_ROOT
        server_common.OUTPUT_ROOT = self.tmp

    def tearDown(self):
        server_common.OUTPUT_ROOT = self.old_output_root
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_warn_pass_records_reason_without_retry(self):
        from frame_generator import generate_frame_sequence

        events = []

        def write_fake_image(target_path, content):
            os.makedirs(os.path.dirname(target_path), exist_ok=True)
            with open(target_path, 'wb') as f:
                f.write(content.encode('utf-8'))

        def fake_text_image(config, prompt, target_path, *args, **kwargs):
            write_fake_image(target_path, f'image:{prompt}')
            return False

        def fake_image_edit(config, prompt, reference_path, target_path, *args, **kwargs):
            write_fake_image(target_path, f'edit:{prompt}')
            return False

        with _gate_sources(), \
             patch('frame_generator._generate_text_image', side_effect=fake_text_image), \
             patch('frame_generator._generate_image_edit', side_effect=fake_image_edit), \
             patch('prompt_pipeline.run_vlm_qa_check',
                   return_value=(True, 'WARN: 视角轻微偏移')):
            generate_frame_sequence(
                {'qaGateLevel': 'lenient'},
                'qa_gate_warn_contract',
                self._PROMPT_BLOCK,
                on_progress=lambda stage, details: events.append((stage, details)),
            )

        stages = [stage for stage, _ in events]
        self.assertNotIn('frame_retry', stages)

        manifest = None
        for root, _dirs, files in os.walk(self.tmp):
            if 'manifest.json' in files:
                with open(os.path.join(root, 'manifest.json'), 'r', encoding='utf-8') as f:
                    manifest = json.load(f)
                break
        self.assertIsNotNone(manifest, 'manifest.json 未落盘')
        frame2 = next(f for f in manifest['frames'] if f['sequence'] == 2)
        self.assertEqual(frame2['quality_gate'], 'auto_approved')
        self.assertEqual(frame2['vlm_qa_reason'], 'WARN: 视角轻微偏移')


if __name__ == '__main__':
    unittest.main()
