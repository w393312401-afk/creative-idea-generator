"""qaGateLevel 档位解析与判定回复的格式容错。

2026-08-05：生成期一致性审查已整体移除，qaGateLevel 如今只作用于手动一致性审查与
视频门禁。原先针对逐帧质检门/漂移复查/锚帧验收门的 off/lenient 用例随那些门一并删除。
"""
import json
import os
import shutil
import tempfile
import unittest
from contextlib import contextmanager
from unittest.mock import patch

import server_common
import prompt_pipeline
from prompt_pipeline import is_warn_verdict
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
        """服务端托管模式的白名单透传：这份白名单是唯一的透传口，漏掉一项就是
        『配置了但从未生效』的静默失效（qaGateLevel 曾经就这么丢过一次）。"""
        with _gate_sources(), patch.object(server_common, 'SERVER_MANAGED', True):
            merged = effective_config({'qaGateLevel': 'lenient'})
            self.assertEqual(merged.get('qaGateLevel'), 'lenient')
            self.assertEqual(qa_gate_level(merged), 'lenient')

    def test_effective_config_passes_continuity_settings_in_managed_mode(self):
        with _gate_sources(), patch.object(server_common, 'SERVER_MANAGED', True):
            merged = effective_config({
                'frameContinuityMode': 'strict',
                'frameContinuityMaxRetries': 2,
                'frameContinuityLocalEdit': 'off',
                'autoSplitHighRiskBeats': True,
            })
            self.assertEqual(merged['frameContinuityMode'], 'strict')
            self.assertEqual(merged['frameContinuityMaxRetries'], 2)
            self.assertTrue(merged['autoSplitHighRiskBeats'])

    def test_nonmanaged_server_config_supplies_continuity_defaults(self):
        with _gate_sources(server_cfg={
                'frameContinuityMode': 'off', 'frameContinuityMaxRetries': 0,
        }), patch.object(server_common, 'SERVER_MANAGED', False):
            merged = effective_config({})
            self.assertEqual(merged['frameContinuityMode'], 'off')
            self.assertEqual(merged['frameContinuityMaxRetries'], 0)



class TestGateResponseFormatDrift(unittest.TestCase):
    """判定回复的格式漂移容错：备注要求用中文写，模型常输出全角冒号/空格代下划线。
    本仓库此前在纯文本配额信号、_strip_code_fences 上都栽过同类格式漂移的真实事故。
    判据取自仍在生产路径上的视频施工过程复审（与手动一致性审查共用 _parse_gate_response）。"""

    _IMGS = {'start_frame_path': 'a.webp', 'mid_frame_paths': ['m.webp'],
             'end_frame_path': 'b.webp', 'video_prompt': 'video prompt'}

    def _run(self, response):
        with _gate_sources(), \
             patch.object(prompt_pipeline, '_multimodal_chat', return_value=response):
            return prompt_pipeline.run_video_process_check({}, **self._IMGS)

    def test_fullwidth_colon_note_is_preserved(self):
        passed, reason = self._run('PASS_WITH_WARNINGS：镜头轻微偏移')
        self.assertTrue(passed)
        self.assertEqual(reason, 'WARN: 镜头轻微偏移')

    def test_space_variant_still_counts_as_warn(self):
        passed, reason = self._run('PASS WITH WARNINGS: 视角偏移')
        self.assertTrue(passed)
        self.assertTrue(is_warn_verdict(reason))
        self.assertIn('视角偏移', reason)

    def test_bare_pass_with_warnings_without_note(self):
        passed, reason = self._run('PASS_WITH_WARNINGS')
        self.assertTrue(passed)
        self.assertTrue(is_warn_verdict(reason))


class TestNoPerFrameQaInGenerateFrameSequence(unittest.TestCase):
    """API 帧生成路径不做任何逐帧判定：每帧无条件记 pending_manual_review。
    一致性审查只能由用户手动触发（见 pipeline_orchestrator._sequence_consistency_review），
    qaGateLevel 对渲染路径没有任何影响。"""

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
        from PIL import Image
        covers_dir = os.path.join(self.tmp, 'covers')
        os.makedirs(covers_dir, exist_ok=True)
        self.cover = os.path.join(covers_dir, 'qa_gate_cover.webp')
        Image.new('RGB', (36, 64), (100, 110, 120)).save(self.cover, format='WEBP')

    def tearDown(self):
        server_common.OUTPUT_ROOT = self.old_output_root
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_retry_and_no_qa_call_regardless_of_gate_level(self):
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
             patch.object(prompt_pipeline, '_multimodal_chat',
                          side_effect=AssertionError('rendering must never call a visual judge')):
            generate_frame_sequence(
                {'qaGateLevel': 'lenient', 'coverReferencePath': self.cover},
                'qa_gate_warn_contract',
                self._PROMPT_BLOCK,
                on_progress=lambda stage, details: events.append((stage, details)),
            )

        stages = [stage for stage, _ in events]
        self.assertNotIn('frame_retry', stages)
        self.assertNotIn('frame_qa', stages)

        manifest = None
        for root, _dirs, files in os.walk(self.tmp):
            if 'manifest.json' in files:
                with open(os.path.join(root, 'manifest.json'), 'r', encoding='utf-8') as f:
                    manifest = json.load(f)
                break
        self.assertIsNotNone(manifest, 'manifest.json 未落盘')
        frame2 = next(f for f in manifest['frames'] if f['sequence'] == 2)
        self.assertEqual(frame2['quality_gate'], 'pending_manual_review')


if __name__ == '__main__':
    unittest.main()
