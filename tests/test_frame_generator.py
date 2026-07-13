import io
import os
import shutil
import tempfile
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from frame_generator import (
    plan_fx_chunks,
    _fx_extract_uuid,
    _fx_find_ref_for,
    _fx_store_frame,
    generate_frame_sequence,
    _execute_request_with_retry,
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
        # 渲染完成→质检判定之间必须有显式事件（判定可长达 90s×2，不能对前端静默）
        self.assertIn('frame_qa', stages)
        qa = next(details for stage, details in events if stage == 'frame_qa')
        self.assertEqual(qa['sequence'], 2)
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

    def _read_manifest(self, title):
        import json
        for root, _dirs, files in os.walk(self.tmp):
            if 'manifest.json' in files:
                with open(os.path.join(root, 'manifest.json'), 'r', encoding='utf-8') as f:
                    return json.load(f)
        return None

    def test_fallback_model_used_when_primary_quota_exhausted_without_degrade_mark(self):
        """兜底模型仍走图生图且挂同一张参考帧：链路不断，绝不能再打
        i2i_fallback_degraded——那个标记会让下游视频门禁拦掉相邻两段视频。"""
        edit_calls = []

        def fake_text_image(config, prompt, target_path, *a, **kw):
            self._write(target_path, f'image:{prompt}')

        def fake_image_edit(config, prompt, reference_path, target_path, *a, **kw):
            edit_calls.append(config.get('imageModel'))
            if config.get('imageModel') == 'primary-model':
                raise QuotaExhaustedError('primary exhausted')
            self._write(target_path, f'edit:{prompt}')

        config = {'imageModel': 'primary-model', 'imageEditFallbackModel': 'fallback-model'}

        events = []
        with patch('frame_generator._generate_text_image', side_effect=fake_text_image), \
             patch('frame_generator._generate_image_edit', side_effect=fake_image_edit):
            generate_frame_sequence(config, 'quota_fallback', self._PROMPT_BLOCK,
                                     on_progress=lambda stage, details: events.append((stage, details)))

        self.assertEqual(edit_calls, ['primary-model', 'fallback-model'])
        manifest = self._read_manifest('quota_fallback')
        frame2 = next(f for f in manifest['frames'] if f['sequence'] == 2)
        self.assertNotEqual(frame2['quality_gate'], 'i2i_fallback_degraded')
        self.assertIn('fallback-model', frame2['model'])  # 真实使用的模型留痕
        # 切换兜底必须显式广播：否则前端刚看到"此路终止"下一秒又见转圈，读起来像卡死
        fb = next(details for stage, details in events if stage == 'model_fallback')
        self.assertEqual(fb['sequence'], 2)
        self.assertEqual(fb['to'], 'fallback-model')

    def test_no_silent_text_image_fallback_when_both_edit_models_fail(self):
        """主模型与兜底模型的图生图都失败时：必须整帧明确报错等用户重试，
        绝不静默丢参考图改文生图重画——那会产出真正断链的帧（构图跳变根源）。"""
        text_calls = []

        def fake_text_image(config, prompt, target_path, *a, **kw):
            text_calls.append(config.get('imageModel'))
            self._write(target_path, f'image:{prompt}')

        def fake_image_edit(config, prompt, reference_path, target_path, *a, **kw):
            # Both primary and fallback edit attempts are quota-exhausted.
            raise QuotaExhaustedError('exhausted')

        config = {'imageModel': 'primary-model', 'imageEditFallbackModel': 'fallback-model'}

        with patch('frame_generator._generate_text_image', side_effect=fake_text_image), \
             patch('frame_generator._generate_image_edit', side_effect=fake_image_edit):
            with self.assertRaises(RuntimeError) as ctx:
                generate_frame_sequence(config, 'quota_fallback_full', self._PROMPT_BLOCK,
                                         on_progress=lambda stage, details: None)

        self.assertIn('兜底模型', str(ctx.exception))
        # 只有第 1 帧（本就没有参考帧）允许文生图；第 2 帧绝不允许退到文生图
        self.assertEqual(text_calls, ['primary-model'])
        # 第 1 帧已落盘，重试时断点续传直接复用
        manifest = self._read_manifest('quota_fallback_full')
        self.assertTrue(any(f['sequence'] == 1 for f in manifest['frames']))


class TestQuotaSignalDetection(unittest.TestCase):
    """The account-pool token broker in front of the gateway sometimes fails with a
    plain-text body (no JSON, no "QUOTA_EXHAUSTED" marker) like:
      "Max retries exhausted. Last error: Token error: No accounts available with
       quota for model: gemini-3-pro-image"
    This must still be classified as quota exhaustion (raise QuotaExhaustedError on
    the first attempt) so imageEditFallbackModel switch-over fires, instead of being
    treated as a generic retryable 502 that burns through all retry attempts and
    then fails the whole frame."""

    class _FakeOpener:
        def __init__(self, code, body_text):
            self.code = code
            self.body_text = body_text
            self.calls = 0

        def open(self, req, timeout=None):
            self.calls += 1
            fp = io.BytesIO(self.body_text.encode('utf-8'))
            raise urllib.error.HTTPError(
                req.full_url if hasattr(req, 'full_url') else 'http://x',
                self.code, 'Bad Gateway', {}, fp,
            )

    def _make_request(self):
        return urllib.request.Request('http://example.invalid/v1/images/edits', data=b'', method='POST')

    def test_token_broker_no_accounts_available_raises_quota_error_on_first_attempt(self):
        body = ("Max retries exhausted. Last error: Token error: No accounts "
                "available with quota for model: gemini-3-pro-image")
        opener = self._FakeOpener(502, body)

        with self.assertRaises(QuotaExhaustedError) as ctx:
            _execute_request_with_retry(self._make_request(), opener=opener, timeout=1)

        # Must fail fast: no point burning retries once the broker has already
        # exhausted its own retries across the whole account pool.
        self.assertEqual(opener.calls, 1)
        self.assertIn('No accounts available', str(ctx.exception))

    def test_unrelated_502_still_retries_and_eventually_raises_http_error(self):
        opener = self._FakeOpener(502, 'upstream connection reset')

        with self.assertRaises(urllib.error.HTTPError):
            _execute_request_with_retry(self._make_request(), opener=opener, timeout=1,
                                         max_attempts=2, initial_delay=0.01)

        self.assertEqual(opener.calls, 2)

    def test_on_attempt_hook_fires_once_per_attempt(self):
        """图像站实时动态依赖 on_attempt 汇报"第 N 次尝试"；回调异常不得影响请求。"""
        opener = self._FakeOpener(502, 'upstream connection reset')
        calls = []

        def hook(attempt, max_attempts):
            calls.append((attempt, max_attempts))
            raise RuntimeError('hook 异常必须被吞掉')

        with self.assertRaises(urllib.error.HTTPError):
            _execute_request_with_retry(self._make_request(), opener=opener, timeout=1,
                                         max_attempts=2, initial_delay=0.01, on_attempt=hook)

        self.assertEqual(calls, [(1, 2), (2, 2)])
        self.assertEqual(opener.calls, 2)

    def test_upstream_sink_broadcasts_every_failed_attempt_immediately(self):
        """毫秒级同步的根基：每次上游失败必须立刻广播（含还剩几次重试），
        而不是等整条退避链烧完。最后一次失败 retry_in 为 None（已放弃）。"""
        from frame_generator import set_upstream_event_sink
        opener = self._FakeOpener(502, 'upstream connection reset')
        events = []
        set_upstream_event_sink(events.append)
        try:
            with self.assertRaises(urllib.error.HTTPError):
                _execute_request_with_retry(self._make_request(), opener=opener, timeout=1,
                                             max_attempts=2, initial_delay=0.01)
        finally:
            set_upstream_event_sink(None)

        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]['attempt'], 1)
        self.assertEqual(events[0]['max_attempts'], 2)
        self.assertIn('HTTP 502', events[0]['error'])
        self.assertIsNotNone(events[0]['retry_in'])   # 第一次失败：还会重试
        self.assertIsNone(events[1]['retry_in'])      # 最后一次失败：已放弃

    def test_upstream_sink_fires_on_quota_failfast_and_is_thread_local(self):
        """配额类 fail-fast 也要广播一次；sink 是线程本地的，别的线程不受影响。"""
        from frame_generator import set_upstream_event_sink
        body = ("Max retries exhausted. Last error: Token error: No accounts "
                "available with quota for model: gemini-3-pro-image")
        opener = self._FakeOpener(502, body)
        events = []
        set_upstream_event_sink(events.append)
        try:
            with self.assertRaises(QuotaExhaustedError):
                _execute_request_with_retry(self._make_request(), opener=opener, timeout=1)
        finally:
            set_upstream_event_sink(None)
        self.assertEqual(len(events), 1)
        self.assertIn('配额耗尽', events[0]['error'])

        # 未注册 sink 的线程独立运作：主线程注册的回调不会被其它线程触发
        import threading
        other_events = []
        set_upstream_event_sink(other_events.append)
        try:
            errs = []
            def _run():
                op = self._FakeOpener(502, 'upstream connection reset')
                try:
                    _execute_request_with_retry(self._make_request(), opener=op, timeout=1,
                                                 max_attempts=1, initial_delay=0.01)
                except Exception as e:
                    errs.append(e)
            th = threading.Thread(target=_run)
            th.start()
            th.join(timeout=10)
            self.assertEqual(other_events, [])  # 子线程的失败不会串进主线程的 sink
            self.assertTrue(errs)
        finally:
            set_upstream_event_sink(None)


class TestImageTaskStageReporting(unittest.TestCase):
    """图像站任务的阶段汇报：只在 pending 时写入；终态由 _finish_image_task 落定，
    且不得丢失 created_at（前端靠它算总用时）。"""

    def test_stage_writes_only_while_pending_and_created_at_survives_finish(self):
        import server_common
        from frame_generator import _set_image_task_stage, _finish_image_task

        with patch.dict(server_common.IMAGE_TASKS, {}, clear=True):
            server_common.IMAGE_TASKS['t1'] = {
                'status': 'pending', 'result': None, 'error': None,
                'stage': '任务已受理，排队中', 'created_at': 123.0,
            }
            _set_image_task_stage('t1', '上游模型渲染中')
            self.assertEqual(server_common.IMAGE_TASKS['t1']['stage'], '上游模型渲染中')

            _finish_image_task('t1', {'status': 'completed', 'result': {'data': []}, 'error': None})
            self.assertEqual(server_common.IMAGE_TASKS['t1']['created_at'], 123.0)
            self.assertEqual(server_common.IMAGE_TASKS['t1']['status'], 'completed')

            _set_image_task_stage('t1', '终态后不得回退')
            self.assertNotEqual(server_common.IMAGE_TASKS['t1'].get('stage'), '终态后不得回退')

    def test_cancelled_task_is_not_overwritten_and_stage_ignored(self):
        import server_common
        from frame_generator import _set_image_task_stage, _finish_image_task

        with patch.dict(server_common.IMAGE_TASKS, {}, clear=True):
            server_common.IMAGE_TASKS['t2'] = {'status': 'cancelled', 'result': None, 'error': '用户取消了任务'}
            _set_image_task_stage('t2', '上游模型渲染中')
            self.assertNotIn('stage', server_common.IMAGE_TASKS['t2'])
            _finish_image_task('t2', {'status': 'completed', 'result': {}, 'error': None})
            self.assertEqual(server_common.IMAGE_TASKS['t2']['status'], 'cancelled')


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
