import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from frame_generator import (
    plan_fx_chunks,
    _fx_extract_uuid,
    _fx_find_ref_for,
    _fx_store_frame,
    generate_frame_sequence,
    QuotaExhaustedError,
)
import server_common
from prompt_pipeline import _parse_prompt_slots, _format_prompt_block, parse_sections

_UUID_A = '11111111-2222-3333-4444-555555555555'
_UUID_B = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'


class TestPlanFxChunks(unittest.TestCase):
    """Google FX 批次切分：只有连续序号能进同一批（批内链式参考按提交顺序挂前一张），
    且单批不超过外部脚本上限 5。"""

    def test_contiguous_run_splits_by_chunk_size(self):
        self.assertEqual(plan_fx_chunks(range(1, 13)),
                         [[1, 2, 3, 4, 5], [6, 7, 8, 9, 10], [11, 12]])

    def test_gap_breaks_chunk(self):
        # 帧 3 已存在被跳过 → 2 和 4 不能同批（4 的参考必须从 3 的留档接起）
        self.assertEqual(plan_fx_chunks([1, 2, 4, 5]), [[1, 2], [4, 5]])

    def test_single_target_retry(self):
        self.assertEqual(plan_fx_chunks([7]), [[7]])

    def test_unsorted_input_and_empty(self):
        self.assertEqual(plan_fx_chunks([5, 3, 4]), [[3, 4, 5]])
        self.assertEqual(plan_fx_chunks([]), [])


class TestFxUuidExtract(unittest.TestCase):
    def test_extracts_from_batch_filename(self):
        self.assertEqual(_fx_extract_uuid(f'C:/tmp/fx_batch_1751600000_0_{_UUID_A}.jpg'), _UUID_A)

    def test_no_uuid_returns_none(self):
        self.assertIsNone(_fx_extract_uuid('C:/tmp/img_001.webp'))
        self.assertIsNone(_fx_extract_uuid(None))


class TestFxSrcArchive(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.frames_dir = os.path.join(self.tmp, 'frames')
        os.makedirs(self.frames_dir)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_fx_jpg(self, uuid_str):
        from PIL import Image
        path = os.path.join(self.tmp, f'fx_batch_1751600000_0_{uuid_str}.jpg')
        Image.new('RGB', (8, 8), (200, 100, 50)).save(path, format='JPEG')
        return path

    def test_store_frame_writes_webp_and_uuid_archive(self):
        src = self._make_fx_jpg(_UUID_A)
        webp, fx_src, uuid_str = _fx_store_frame(src, self.frames_dir, 3)
        self.assertTrue(os.path.exists(webp))
        self.assertTrue(webp.endswith('img_003.webp'))
        self.assertEqual(uuid_str, _UUID_A)
        self.assertTrue(os.path.basename(fx_src).startswith('img_003_'))
        self.assertIn(_UUID_A, os.path.basename(fx_src))

    def test_store_frame_replaces_old_archive_for_same_slot(self):
        # 重试替换同一槽位时旧 UUID 留档必须清掉，否则按前缀找参考会命中旧图
        _fx_store_frame(self._make_fx_jpg(_UUID_A), self.frames_dir, 2)
        _fx_store_frame(self._make_fx_jpg(_UUID_B), self.frames_dir, 2)
        ref = _fx_find_ref_for(self.frames_dir, 3)
        self.assertIsNotNone(ref)
        self.assertIn(_UUID_B, os.path.basename(ref))
        self.assertNotIn(_UUID_A, os.listdir(os.path.join(self.frames_dir, 'fx_src')).__str__())

    def test_find_ref_semantics(self):
        # seq=1 起链无参考；留档缺失返回 None
        self.assertIsNone(_fx_find_ref_for(self.frames_dir, 1))
        self.assertIsNone(_fx_find_ref_for(self.frames_dir, 5))
        _fx_store_frame(self._make_fx_jpg(_UUID_A), self.frames_dir, 4)
        ref = _fx_find_ref_for(self.frames_dir, 5)
        self.assertIn('img_004_', os.path.basename(ref))


class TestFrameProgressEvents(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old_output_root = server_common.OUTPUT_ROOT
        server_common.OUTPUT_ROOT = self.tmp

    def tearDown(self):
        server_common.OUTPUT_ROOT = self.old_output_root
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_frame_start_and_retry_events_are_emitted(self):
        prompt_block = """图片 1:
first frame prompt

图片 2:
second frame prompt

视频 1:
visible construction change
"""
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

        with patch('frame_generator._generate_text_image', side_effect=fake_text_image), \
             patch('frame_generator._generate_image_edit', side_effect=fake_image_edit), \
             patch('prompt_pipeline.run_vlm_qa_check', side_effect=[(False, 'no visible delta'), (True, None)]), \
             patch('prompt_pipeline.fix_image_prompt_with_vlm_feedback', return_value='fixed prompt'), \
             patch('prompt_pipeline.clean_prompt_text', side_effect=lambda s: s), \
             patch('prompt_pipeline.fix_image_clean_frame_proactive', side_effect=lambda s: s), \
             patch('prompt_pipeline.fix_horizon_line', side_effect=lambda s: s), \
             patch('prompt_pipeline.fix_camera_contradictions', side_effect=lambda s, **kwargs: s), \
             patch('prompt_pipeline.fix_rhma_blur', side_effect=lambda s, **kwargs: s), \
             patch('prompt_pipeline.fix_camera_dna', side_effect=lambda s, dna: s):
            generate_frame_sequence(
                {},
                'progress_contract',
                prompt_block,
                on_progress=lambda stage, details: events.append((stage, details)),
            )

        stages = [stage for stage, _ in events]
        self.assertIn('frame_start', stages)
        self.assertIn('frame_retry', stages)
        starts = [details for stage, details in events if stage == 'frame_start']
        self.assertEqual(starts[0]['sequence'], 1)
        self.assertEqual(starts[1]['sequence'], 2)
        retry = next(details for stage, details in events if stage == 'frame_retry')
        self.assertEqual(retry['sequence'], 2)
        self.assertEqual(retry['attempt'], 1)
        self.assertEqual(retry['reason'], 'no visible delta')


class TestQuotaFallback(unittest.TestCase):
    """当主模型(Gemini)图片额度耗尽时，必须切到 config['imageEditFallbackModel']
    （effective_config 现在会透传这项），而不是让整个帧序列任务崩溃。"""

    _PROMPT_BLOCK = """图片 1:
first frame prompt

图片 2:
second frame prompt
"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old_output_root = server_common.OUTPUT_ROOT
        server_common.OUTPUT_ROOT = self.tmp

    def tearDown(self):
        server_common.OUTPUT_ROOT = self.old_output_root
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _write(target_path, content):
        os.makedirs(os.path.dirname(target_path), exist_ok=True)
        with open(target_path, 'wb') as f:
            f.write(content.encode('utf-8'))

    def test_fallback_model_used_when_primary_quota_exhausted(self):
        edit_calls = []

        def fake_text_image(config, prompt, target_path, *a, **kw):
            self._write(target_path, f'image:{prompt}')
            return False

        def fake_image_edit(config, prompt, reference_path, target_path, *a, **kw):
            edit_calls.append(config.get('imageModel'))
            if config.get('imageModel') == 'primary-model':
                raise QuotaExhaustedError('primary exhausted')
            self._write(target_path, f'edit:{prompt}')
            return False

        config = {'imageModel': 'primary-model', 'imageEditFallbackModel': 'fallback-model'}

        with patch('frame_generator._generate_text_image', side_effect=fake_text_image), \
             patch('frame_generator._generate_image_edit', side_effect=fake_image_edit):
            generate_frame_sequence(config, 'quota_fallback', self._PROMPT_BLOCK,
                                     on_progress=lambda stage, details: None)

        self.assertEqual(edit_calls, ['primary-model', 'fallback-model'])

    def test_last_resort_text_image_uses_fallback_model_not_exhausted_primary(self):
        text_calls = []

        def fake_text_image(config, prompt, target_path, *a, **kw):
            text_calls.append(config.get('imageModel'))
            self._write(target_path, f'image:{prompt}')
            return False

        def fake_image_edit(config, prompt, reference_path, target_path, *a, **kw):
            # Both primary and fallback edit attempts are quota-exhausted.
            raise QuotaExhaustedError('exhausted')

        config = {'imageModel': 'primary-model', 'imageEditFallbackModel': 'fallback-model'}

        with patch('frame_generator._generate_text_image', side_effect=fake_text_image), \
             patch('frame_generator._generate_image_edit', side_effect=fake_image_edit):
            generate_frame_sequence(config, 'quota_fallback_full', self._PROMPT_BLOCK,
                                     on_progress=lambda stage, details: None)

        # Frame 1 has no previous frame, so it always text-generates on the primary
        # model. Frame 2's image-edit exhausts both primary and fallback, so the
        # last-resort text-to-image call must use the fallback model, not the
        # already-exhausted primary one.
        self.assertEqual(text_calls, ['primary-model', 'fallback-model'])


class TestPromptBlockBracketSurvival(unittest.TestCase):
    """Regression: real prompt bodies routinely contain literal '[' / ']' characters
    ([BRIDGE] meta tags, 'Locked anchors: [C2, B2, A2]' shorthand). The old
    _strip_code_fences() JSON-extraction fallback sliced from the first stray '['
    anywhere in the ~20k-char block to the last ']' anywhere in it, silently
    dropping every slot outside that span. This was the real cause of "提示词前端
    显示不全" (missing image/video slots) — not a frontend truncation bug."""

    def _build_block(self, total_beats=16):
        images = {}
        for i in range(1, total_beats + 2):
            meta = 'BRIDGE' if i == 7 else ''
            images[i] = {'body': f'Static shot state {i}. Locked anchors: [C2, B2, A2].', 'meta': meta}
        videos = {}
        for i in range(1, total_beats + 1):
            meta = 'BRIDGE' if i == 5 else ''
            videos[i] = {'body': f'Use the provided first frame and last frame as exact composition anchors. VIDEO BODY {i}.', 'meta': meta}
        return _format_prompt_block(images, videos), total_beats

    def test_parse_prompt_slots_survives_bracket_notation(self):
        block, total_beats = self._build_block()
        images, videos = _parse_prompt_slots(block)
        self.assertEqual(sorted(images.keys()), list(range(1, total_beats + 2)))
        self.assertEqual(sorted(videos.keys()), list(range(1, total_beats + 1)))

    def test_parse_sections_survives_bracket_notation_in_full_output(self):
        block, total_beats = self._build_block()
        content = (
            "===TITLE===\n测试标题\n===THEME===\n测试主题\n===PROMPTS===\n"
            f"{block}\n===AUDIT===\n已修复 1 处：Beat 5 修复了xxx\n\n"
            "| 拍号 / Beat Index | 审核大项 | 审核判定说明 |\n|---|---|---|\n| 1 | test | ok |\n"
        )
        result = parse_sections(content)
        images, videos = _parse_prompt_slots(result['prompt_block'])
        self.assertEqual(sorted(images.keys()), list(range(1, total_beats + 2)))
        self.assertEqual(sorted(videos.keys()), list(range(1, total_beats + 1)))


if __name__ == '__main__':
    unittest.main()
