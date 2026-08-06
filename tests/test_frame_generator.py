import io
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from frame_generator import (
    plan_fx_chunks,
    fx_bridge_target_sequences,
    split_fx_chunks_at_heads,
    fx_prompt_with_bridge_control,
    plan_frame_chunk_accounts,
    _fx_extract_uuid,
    _fx_find_ref_for,
    _fx_store_frame,
    generate_frame_sequence,
    _execute_request_with_retry,
    _generate_image_edit,
    _image_edit_api_size,
    _image_size_to_api_size,
    CHAT_TRANSPORT,
    reset_edits_pool_state,
    QuotaExhaustedError,
    _match_color_lab,
)
import server_common
from server_common import gpt_image_pixel_size
from prompt_pipeline import _parse_prompt_slots, _format_prompt_block, parse_sections

_UUID_A = '11111111-2222-3333-4444-555555555555'
_UUID_B = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'


def _write_test_image(path, size):
    """写一张真实可解码的图：降档通道的取证靠真实像素尺寸，假字节顶不了。"""
    from PIL import Image
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    Image.new('RGB', size, (90, 110, 130)).save(
        path, format='WEBP' if path.lower().endswith('.webp') else 'PNG')


def _make_test_cover(root, name='test_cover.webp'):
    path = os.path.join(root, 'covers', name)
    _write_test_image(path, (72, 128))
    return path


def _test_image_data_url(size):
    import base64
    from PIL import Image
    buf = io.BytesIO()
    Image.new('RGB', size, (120, 100, 80)).save(buf, format='JPEG')
    return 'data:image/jpeg;base64,' + base64.b64encode(buf.getvalue()).decode('ascii')


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

    def test_bridge_target_starts_a_fresh_batch(self):
        prompts = {i: {'prompt': f'image {i}', 'meta': ''} for i in range(1, 8)}
        videos = {4: {'body': 'cross the threshold', 'meta': '[BRIDGE]'}}
        heads = fx_bridge_target_sequences(prompts, videos)
        self.assertEqual(heads, {5})
        self.assertEqual(
            split_fx_chunks_at_heads(plan_fx_chunks(prompts), heads),
            [[1, 2, 3, 4], [5], [6, 7]],
        )

    def test_fx_bridge_control_is_inlined_and_idempotent(self):
        prompts = {5: {'prompt': 'Camera settles inside.', 'meta': ''}}
        videos = {4: {'body': 'cross', 'meta': '[BRIDGE]'}}
        rendered = fx_prompt_with_bridge_control(5, prompts[5], prompts, videos)
        self.assertIn('camera viewpoint is actively advancing forward', rendered)
        prompts[5]['prompt'] = rendered
        self.assertEqual(
            fx_prompt_with_bridge_control(5, prompts[5], prompts, videos), rendered)

    def test_fx_bridge_turn_uses_turn_control(self):
        prompts = {5: {'prompt': 'Camera settles inside.', 'meta': ''}}
        videos = {4: {'body': 'cross and pan', 'meta': '[BRIDGE TURN]'}}
        rendered = fx_prompt_with_bridge_control(5, prompts[5], prompts, videos)
        self.assertIn('ROTATES horizontally', rendered)


class TestPlanFrameChunkAccounts(unittest.TestCase):
    """只换号、不换 IP：每批绑一个号池账号，IP 全程不动（换 IP 已全局关停）。"""

    def test_single_account_pool_changes_nothing(self):
        """可换的号 ≤1 个时不介入——账号沿用调用方设好的。"""
        chunks = [[1, 2, 3, 4, 5], [6, 7, 8, 9, 10]]
        for ring in ([], ['a']):
            plans = plan_frame_chunk_accounts(chunks, ring, 5)
            self.assertEqual(plans, [{'user_id': None}] * 2)

    def test_switches_account_per_batch(self):
        chunks = [[1, 2, 3, 4, 5], [6, 7, 8, 9, 10], [11, 12]]
        plans = plan_frame_chunk_accounts(chunks, ['a', 'b', 'c'], 5)
        self.assertEqual([p['user_id'] for p in plans], ['a', 'b', 'c'])

    def test_ring_wraps_around(self):
        chunks = [[1, 2, 3, 4, 5]] * 4
        plans = plan_frame_chunk_accounts(chunks, ['a', 'b'], 5)
        self.assertEqual([p['user_id'] for p in plans], ['a', 'b', 'a', 'b'])

    def test_interval_larger_than_batch_keeps_account_across_batches(self):
        """节拍 10 帧 + 每批 5 帧 = 两批共用一个号。"""
        chunks = [[1, 2, 3, 4, 5]] * 4
        plans = plan_frame_chunk_accounts(chunks, ['a', 'b'], 10)
        self.assertEqual([p['user_id'] for p in plans], ['a', 'a', 'b', 'b'])

    def test_never_emits_rotate_ip(self):
        """换 IP 已关停：计划里不该再出现任何换 IP 指示。"""
        chunks = [[1, 2, 3, 4, 5]] * 4
        for ring in ([], ['a'], ['a', 'b', 'c']):
            for plan in plan_frame_chunk_accounts(chunks, ring, 5):
                self.assertNotIn('rotate_ip', plan)

    def test_short_chunks_from_cut_heads_still_accumulate_to_interval(self):
        """硬切把批次切碎（1 帧、2 帧）时，按累计帧数而不是按批数换号。"""
        chunks = [[1], [2, 3], [4, 5, 6], [7, 8]]
        plans = plan_frame_chunk_accounts(chunks, ['a', 'b'], 5)
        self.assertEqual([p['user_id'] for p in plans], ['a', 'a', 'a', 'b'])

    def test_no_chunks(self):
        self.assertEqual(plan_frame_chunk_accounts([], ['a', 'b'], 5), [])


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


class TestLabColorMatching(unittest.TestCase):
    """LAB matching must use OpenCV's uint8 channel ranges without cooling images."""

    def setUp(self):
        try:
            import cv2  # noqa: F401
            import numpy  # noqa: F401
        except ImportError:
            self.skipTest('OpenCV/numpy are not installed')
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        if hasattr(self, 'tmp'):
            shutil.rmtree(self.tmp, ignore_errors=True)

    def test_identical_warm_image_preserves_white_balance(self):
        from PIL import Image, ImageChops

        source = os.path.join(self.tmp, 'warm-source.png')
        output = os.path.join(self.tmp, 'warm-output.png')
        # RGB with a clear warm balance. The old -127..127 a/b clipping removes
        # its yellow component and makes this image substantially cooler.
        original = Image.new('RGB', (32, 32), (230, 200, 170))
        original.save(source)

        _match_color_lab(source, source, output)

        result = Image.open(output).convert('RGB')
        extrema = ImageChops.difference(original, result).getextrema()
        self.assertLessEqual(max(high for _low, high in extrema), 3)


class TestFrameProgressEvents(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old_output_root = server_common.OUTPUT_ROOT
        server_common.OUTPUT_ROOT = self.tmp
        self.cover = _make_test_cover(self.tmp)

    def tearDown(self):
        server_common.OUTPUT_ROOT = self.old_output_root
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_frame_start_events_are_emitted_with_no_per_frame_qa(self):
        """渲染期不做任何视觉判定：只发 frame_start/frame，不发 frame_qa/frame_retry，
        每帧落 manifest 的 quality_gate 都是 'pending_manual_review'。一致性审查只能由
        用户手动触发（见 pipeline_orchestrator._sequence_consistency_review）。"""
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
             patch('prompt_pipeline._multimodal_chat',
                   side_effect=AssertionError('rendering must never call a visual judge')):
            manifest = generate_frame_sequence(
                {'coverReferencePath': self.cover},
                'progress_contract',
                prompt_block,
                on_progress=lambda stage, details: events.append((stage, details)),
            )

        stages = [stage for stage, _ in events]
        self.assertIn('frame_start', stages)
        self.assertNotIn('frame_retry', stages)
        self.assertNotIn('frame_qa', stages)
        starts = [details for stage, details in events if stage == 'frame_start']
        self.assertEqual(starts[0]['sequence'], 1)
        self.assertEqual(starts[1]['sequence'], 2)
        for frame in manifest['frames']:
            self.assertEqual(frame['quality_gate'], 'pending_manual_review')

    def test_subset_prompt_keeps_its_real_slot_and_uses_durable_parent(self):
        """A prompt block containing only IMAGE 3 must render slot 3, never slot 1."""
        frames_dir = os.path.join(server_common._get_project_dir('subset_slot_3'), 'frames')
        os.makedirs(frames_dir, exist_ok=True)
        parent = os.path.join(frames_dir, 'img_002.webp')
        _write_test_image(parent, (72, 128))
        calls = []

        def fake_image_edit(config, prompt, reference_path, target_path, *args, **kwargs):
            calls.append((prompt, reference_path, target_path))
            _write_test_image(target_path, (72, 128))
            return False

        with patch('frame_generator._generate_image_edit', side_effect=fake_image_edit):
            manifest = generate_frame_sequence(
                {'coverReferencePath': self.cover},
                'subset_slot_3',
                '图片 3:\nthird-slot prompt\n',
                on_progress=lambda *args: None,
                target_sequences=[3],
            )

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], 'third-slot prompt')
        self.assertEqual(calls[0][1], parent)
        self.assertTrue(calls[0][2].endswith('img_003.webp'))
        self.assertEqual([(f['sequence'], f['slot']) for f in manifest['frames']], [(3, 3)])


class TestQuotaFallback(unittest.TestCase):
    """主模型图片额度耗尽 = 就地明确失败。

    自动降级到兜底模型（曾配 gpt-image-2）已整体取消：换模型渲出来的帧会丢
    材质做旧风格、还会凭空发明提示词没提过的结构件，并因图生图链式编辑被下一
    帧当"已确认事实"继承。已渲好的帧由断点续传保住，补额度后重试即可接着渲。"""

    _PROMPT_BLOCK = """图片 1:
first frame prompt

图片 2:
second frame prompt
"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old_output_root = server_common.OUTPUT_ROOT
        server_common.OUTPUT_ROOT = self.tmp
        self.cover = _make_test_cover(self.tmp)

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

    def test_quota_exhaustion_aborts_instead_of_switching_models(self):
        """配额耗尽必须原样抛 QuotaExhaustedError，且绝不试第二个模型：
        配了 imageEditFallbackModel 也一样（该键已不再透传，留在这里是防回归）。"""
        edit_calls = []

        def fake_text_image(config, prompt, target_path, *a, **kw):
            self._write(target_path, f'image:{prompt}')

        def fake_image_edit(config, prompt, reference_path, target_path, *a, **kw):
            edit_calls.append(config.get('imageModel'))
            if len(edit_calls) > 1:
                raise QuotaExhaustedError('primary exhausted')
            self._write(target_path, f'edit:{prompt}')

        config = {'imageModel': 'primary-model', 'imageEditFallbackModel': 'fallback-model',
                  'coverReferencePath': self.cover}

        events = []
        with patch('frame_generator._generate_text_image', side_effect=fake_text_image), \
             patch('frame_generator._generate_image_edit', side_effect=fake_image_edit):
            with self.assertRaises(QuotaExhaustedError):
                generate_frame_sequence(config, 'quota_fallback', self._PROMPT_BLOCK,
                                         on_progress=lambda stage, details: events.append((stage, details)))

        # 只打了主模型一枪，没有第二个模型的重试
        self.assertEqual(edit_calls, ['primary-model', 'primary-model'])
        self.assertEqual([s for s, _d in events if s == 'model_fallback'], [])
        # 第 1 帧已落盘：补额度后重试靠断点续传直接复用，不白烧
        manifest = self._read_manifest('quota_fallback')
        self.assertTrue(any(f['sequence'] == 1 for f in manifest['frames']))

    def test_no_silent_text_image_fallback_when_edit_quota_exhausted(self):
        """图生图配额耗尽时绝不静默丢参考图改文生图重画——那会产出真正断链的
        帧（构图跳变根源）。第一帧也必须使用封面图生图。"""
        text_calls = []

        def fake_text_image(config, prompt, target_path, *a, **kw):
            text_calls.append(config.get('imageModel'))
            self._write(target_path, f'image:{prompt}')

        def fake_image_edit(config, prompt, reference_path, target_path, *a, **kw):
            raise QuotaExhaustedError('exhausted')

        config = {'imageModel': 'primary-model', 'coverReferencePath': self.cover}

        with patch('frame_generator._generate_text_image', side_effect=fake_text_image), \
             patch('frame_generator._generate_image_edit', side_effect=fake_image_edit):
            with self.assertRaises(QuotaExhaustedError):
                generate_frame_sequence(config, 'quota_fallback_full', self._PROMPT_BLOCK,
                                         on_progress=lambda stage, details: None)

        self.assertEqual(text_calls, [])
        manifest = self._read_manifest('quota_fallback_full')
        self.assertFalse(manifest and manifest.get('frames'))


class TestChatTransportFallback(unittest.TestCase):
    """网关的 /images/edits 被写死路由到 gemini-3-pro-image 号池：请求里写哪个图像模型
    都一样，号池没额度就一律 502「No accounts available with quota for model:
    gemini-3-pro-image」——第 1 帧走 /images/generations（flash-image 号池）没事、第 2 帧
    起必挂就是这个原因。同一个网关的 /chat/completions 用同一个模型名带参考图能正常
    图生图，所以撞这堵墙时换通道续渲（模型不变，链上一致性不受影响）。这条通道固定出
    1K 档，请求 2K/4K 时才是降档——留痕按实际情况标，不乱扣帽子。"""

    _BROKER_BODY = ('Max retries exhausted. Last error: Token error: No accounts '
                    'available with quota for model: gemini-3-pro-image')

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cover = _make_test_cover(self.tmp)
        self.ref_path = os.path.join(self.tmp, 'img_001.webp')
        self.target_path = os.path.join(self.tmp, 'img_002.webp')
        _write_test_image(self.ref_path, (720, 1280))
        self.chat_payloads = []
        reset_edits_pool_state()

    def tearDown(self):
        reset_edits_pool_state()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fake_transport(self, chat_result='image'):
        """/images/edits 一律撞号池墙；/chat/completions 按 chat_result 表现。"""
        import json

        def fake_execute(req, *args, **kwargs):
            url = req.full_url if hasattr(req, 'full_url') else str(req)
            if '/images/edits' in url:
                raise QuotaExhaustedError(f'您的图片生成配额已耗尽 (QUOTA_EXHAUSTED)。 {self._BROKER_BODY}')
            if '/chat/completions' not in url:
                raise AssertionError(f'unexpected upstream call: {url}')
            self.chat_payloads.append(json.loads(req.data.decode('utf-8')))
            if chat_result == 'quota':
                raise QuotaExhaustedError(f'您的图片生成配额已耗尽 (QUOTA_EXHAUSTED)。 {self._BROKER_BODY}')
            if chat_result == 'no_image':
                content = '抱歉，我无法生成这张图片。'
            else:
                content = f'![image]({_test_image_data_url((768, 1376))})'
            return json.dumps({'choices': [{'message': {'role': 'assistant', 'content': content}}]}).encode('utf-8')

        return fake_execute

    def test_edit_quota_wall_is_served_by_chat_transport_with_the_same_model(self):
        config = {'imageModel': 'nano-banana-2', 'imageAspectRatio': '9:16',
                  'imageQuality': '2K', 'apiKey': 'k'}

        with patch('frame_generator._execute_request_with_retry', side_effect=self._fake_transport()):
            transport = _generate_image_edit(config, 'second frame prompt',
                                             self.ref_path, self.target_path)

        self.assertEqual(transport, CHAT_TRANSPORT)
        self.assertTrue(os.path.getsize(self.target_path) > 0)
        self.assertEqual(len(self.chat_payloads), 1)
        payload = self.chat_payloads[0]
        # 模型不变：界面别名换算后的裸模型名，绝不带比例/画质魔法后缀（带后缀上游判 404）
        self.assertEqual(payload['model'], 'gemini-3.1-flash-image')
        # 出图比例只有顶层 size 能控（aspect_ratio/image_size/提示词都无效）
        self.assertEqual(payload['size'], '9:16')
        # 参考图必须内联进去——否则这就不是图生图，而是断链的文生图
        blocks = payload['messages'][0]['content']
        self.assertEqual([b['type'] for b in blocks], ['text', 'image_url'])
        self.assertTrue(blocks[1]['image_url']['url'].startswith('data:image/png;base64,'))
        self.assertIn('second frame prompt', blocks[0]['text'])

    def test_codex_routed_model_has_no_chat_transport_and_still_fails_fast(self):
        """gpt-image-2 走的是另一个网关(codex)，没有这条等价通道——不许拿 gemini
        的 chat 通道去顶，那就是换模型了。"""
        config = {'imageModel': 'gpt-image-2', 'imageAspectRatio': '9:16', 'codexApiKey': 'k'}

        with patch('frame_generator._execute_request_with_retry', side_effect=self._fake_transport()):
            with self.assertRaises(QuotaExhaustedError):
                _generate_image_edit(config, 'p', self.ref_path, self.target_path)

        self.assertEqual(self.chat_payloads, [])

    def test_transport_edits_never_falls_back(self):
        config = {'imageModel': 'nano-banana-2', 'imageAspectRatio': '9:16',
                  'imageEditTransport': 'edits', 'apiKey': 'k'}

        with patch('frame_generator._execute_request_with_retry', side_effect=self._fake_transport()):
            with self.assertRaises(QuotaExhaustedError):
                _generate_image_edit(config, 'p', self.ref_path, self.target_path)

        self.assertEqual(self.chat_payloads, [])

    def test_windows_edits_uses_verified_pixel_size_and_quality_fields(self):
        """Windows 8046 edits 走已实测成功的 size + image_size，不再发旧 aspect_ratio。"""
        requests = []

        def fake_execute(req, *args, **kwargs):
            requests.append(req)
            data_url = _test_image_data_url((768, 1376))
            b64 = data_url.split(',', 1)[1]
            return json.dumps({'data': [{'b64_json': b64}]}).encode('utf-8')

        config = {
            'imageModel': 'nano-banana-2',
            'imageAspectRatio': '9:16',
            'imageQuality': '2K',
            'imageEditTransport': 'edits',
            'apiKey': 'k',
        }
        with patch('frame_generator._execute_request_with_retry', side_effect=fake_execute):
            transport = _generate_image_edit(config, 'p', self.ref_path, self.target_path)

        self.assertIsNone(transport)
        self.assertEqual(len(requests), 1)
        body = requests[0].data.decode('latin-1')
        self.assertIn('name="model"\r\n\r\ngemini-3.1-flash-image\r\n', body)
        self.assertIn('name="size"\r\n\r\n720x1280\r\n', body)
        self.assertIn('name="image_size"\r\n\r\n2K\r\n', body)
        self.assertIn('name="response_format"\r\n\r\nb64_json\r\n', body)
        self.assertIn('name="image"; filename="reference.png"', body)
        self.assertNotIn('name="aspect_ratio"', body)

    def test_windows_edits_size_mapping(self):
        self.assertEqual(_image_edit_api_size('1:1'), '1024x1024')
        self.assertEqual(_image_edit_api_size('9:16'), '720x1280')
        self.assertEqual(_image_edit_api_size('16:9'), '1280x720')
        self.assertEqual(_image_edit_api_size('4:3'), '1216x896')
        self.assertEqual(_image_edit_api_size('720x1280'), '720x1280')

    def test_transport_chat_never_sends_the_doomed_edits_request(self):
        """网关补丁缺失的机器上直接指定 chat 通道：那一枪必挂（~1s + 一整张参考图
        上传），一次都不该发。"""
        config = {'imageModel': 'nano-banana-2', 'imageAspectRatio': '9:16',
                  'imageEditTransport': 'chat', 'apiKey': 'k'}
        edits_calls = []

        def fake_execute(req, *args, **kwargs):
            url = req.full_url if hasattr(req, 'full_url') else str(req)
            if '/images/edits' in url:
                edits_calls.append(url)
                raise AssertionError('imageEditTransport=chat 时不许打 /images/edits')
            return self._fake_transport()(req, *args, **kwargs)

        with patch('frame_generator._execute_request_with_retry', side_effect=fake_execute):
            transport = _generate_image_edit(config, 'p', self.ref_path, self.target_path)

        self.assertEqual(transport, CHAT_TRANSPORT)
        self.assertEqual(edits_calls, [])
        self.assertEqual(len(self.chat_payloads), 1)

    def test_pool_wall_is_remembered_so_later_frames_skip_the_doomed_request(self):
        """撞过一次墙就够了：熔断后同一网关的后续帧直接走 chat 通道，不再每帧都
        白发一次必挂的 /images/edits（那一枪要 ~1s，还要把整张参考图传上去）。"""
        config = {'imageModel': 'nano-banana-2', 'imageAspectRatio': '9:16', 'apiKey': 'k'}
        edits_attempts = []

        def fake_execute(req, *args, **kwargs):
            url = req.full_url if hasattr(req, 'full_url') else str(req)
            if '/images/edits' in url:
                edits_attempts.append(url)
            return self._fake_transport()(req, *args, **kwargs)

        with patch('frame_generator._execute_request_with_retry', side_effect=fake_execute):
            for _ in range(3):
                self.assertEqual(
                    _generate_image_edit(config, 'p', self.ref_path, self.target_path),
                    CHAT_TRANSPORT)

        self.assertEqual(len(edits_attempts), 1)   # 只有第一帧探了一次路
        self.assertEqual(len(self.chat_payloads), 3)

        # 熔断只活在进程内：网关补好后重启服务（= 清状态）会重新探路
        reset_edits_pool_state()
        with patch('frame_generator._execute_request_with_retry', side_effect=fake_execute):
            _generate_image_edit(config, 'p', self.ref_path, self.target_path)
        self.assertEqual(len(edits_attempts), 2)

    def test_codex_gateway_is_not_short_circuited_by_the_gemini_pool_wall(self):
        """熔断按网关记：gemini 网关的 edits 死了，不该让走 codex 网关的
        gpt-image-2 也跳过它自己的 edits 请求。

        两个网关地址都在 config 里写死：resolve_gateway 的取值链是
        config.codexBaseUrl → SERVER_CONFIG.codexBaseUrl → 默认 65038，此前这里靠
        默认值、断言里硬编码 '65038'，于是任何在 server_config.json 里改过
        codexBaseUrl 的开发机上这条测试都必然失败（实测本机配的是 52692）——
        测试不该依赖开发机的真实配置。"""
        gemini_base = 'http://127.0.0.1:8046/v1'
        codex_base = 'http://127.0.0.1:65038/v1'
        gemini_cfg = {'imageModel': 'nano-banana-2', 'imageAspectRatio': '9:16', 'apiKey': 'k',
                      'baseUrl': gemini_base}
        codex_cfg = {'imageModel': 'gpt-image-2', 'imageAspectRatio': '9:16', 'codexApiKey': 'k',
                     'codexBaseUrl': codex_base}
        codex_edits = []

        def fake_execute(req, *args, **kwargs):
            url = req.full_url if hasattr(req, 'full_url') else str(req)
            if url.startswith(codex_base):
                codex_edits.append(url)
                raise RuntimeError('codex gateway down')
            return self._fake_transport()(req, *args, **kwargs)

        with patch('frame_generator._execute_request_with_retry', side_effect=fake_execute), \
             patch('time.sleep'):
            _generate_image_edit(gemini_cfg, 'p', self.ref_path, self.target_path)
            with self.assertRaises(RuntimeError):
                _generate_image_edit(codex_cfg, 'p', self.ref_path, self.target_path)

        self.assertTrue(codex_edits)  # codex 那条路照打，没被 gemini 的熔断带累

    def test_chat_transport_failure_keeps_quota_exhaustion_as_the_reported_cause(self):
        """兜底通道也没救时，报出去的必须还是"配额耗尽"这个真因，chat 通道自己的
        失败只作为附注——否则用户会去排查一个假问题。"""
        config = {'imageModel': 'nano-banana-2', 'imageAspectRatio': '9:16', 'apiKey': 'k'}

        # chat 通道也撞配额墙：真因原样上抛（断点续传据此保住已渲好的帧）
        with patch('frame_generator._execute_request_with_retry',
                   side_effect=self._fake_transport('quota')):
            with self.assertRaises(QuotaExhaustedError) as ctx:
                _generate_image_edit(config, 'p', self.ref_path, self.target_path)
        self.assertIn('QUOTA_EXHAUSTED', str(ctx.exception))

        # chat 通道回包里没有图（不是配额问题）：这是另一种失败，报错要说它自己的事，
        # 别硬套成"配额耗尽"——但也照样烧完外层重试才放弃。
        reset_edits_pool_state()
        with patch('frame_generator._execute_request_with_retry',
                   side_effect=self._fake_transport('no_image')), \
             patch('time.sleep'):
            with self.assertRaises(RuntimeError) as ctx:
                _generate_image_edit(config, 'p', self.ref_path, self.target_path)
        self.assertNotIsInstance(ctx.exception, QuotaExhaustedError)
        self.assertIn('没有图片', str(ctx.exception))

    def test_recoverable_pool_wall_is_not_broadcast_as_an_upstream_failure(self):
        """探路那一枪撞墙 = 换条路接着渲，不是"任务要挂了"。前端把 upstream 事件
        一律渲成「⚠️ 上游报错…此路终止，任务即将报错结束」，而这一帧其实好好地在
        chat 通道上渲完了——播报这句话就是撒谎吓人。换通道由调用方的
        transport_fallback 事件如实播报，这里一个 upstream 事件都不许推。"""
        from frame_generator import set_upstream_event_sink
        config = {'imageModel': 'nano-banana-2', 'imageAspectRatio': '9:16', 'apiKey': 'k'}
        edits_kwargs = []

        def fake_execute(req, *args, **kwargs):
            url = req.full_url if hasattr(req, 'full_url') else str(req)
            if '/images/edits' in url:
                edits_kwargs.append(kwargs)
            return self._fake_transport()(req, *args, **kwargs)

        events = []
        set_upstream_event_sink(events.append)
        try:
            with patch('frame_generator._execute_request_with_retry', side_effect=fake_execute):
                transport = _generate_image_edit(config, 'p', self.ref_path, self.target_path)
        finally:
            set_upstream_event_sink(None)

        self.assertEqual(transport, CHAT_TRANSPORT)
        self.assertEqual(events, [])
        # 那一枪自己也不许把配额耗尽播出去：有等价通道可换时它不是终点
        self.assertEqual(edits_kwargs[0].get('emit_quota_failure'), False)

    def test_dead_end_pool_wall_is_still_broadcast(self):
        """反过来：换不了通道时（transport=edits / 没有等价 chat 模型），撞墙就是
        真终点，必须照旧广播——不能为了不吓人把真failure也一起吞了。"""
        edits_kwargs = []

        def fake_execute(req, *args, **kwargs):
            url = req.full_url if hasattr(req, 'full_url') else str(req)
            if '/images/edits' in url:
                edits_kwargs.append(kwargs)
            return self._fake_transport()(req, *args, **kwargs)

        with patch('frame_generator._execute_request_with_retry', side_effect=fake_execute):
            with self.assertRaises(QuotaExhaustedError):
                _generate_image_edit({'imageModel': 'nano-banana-2', 'imageAspectRatio': '9:16',
                                      'imageEditTransport': 'edits', 'apiKey': 'k'},
                                     'p', self.ref_path, self.target_path)
            with self.assertRaises(QuotaExhaustedError):
                _generate_image_edit({'imageModel': 'gpt-image-2', 'imageAspectRatio': '9:16',
                                      'codexApiKey': 'k'},
                                     'p', self.ref_path, self.target_path)

        self.assertEqual([kw.get('emit_quota_failure') for kw in edits_kwargs], [True, True])

    def test_managed_mode_actually_honours_the_transport_switch(self):
        """`imageEditTransport` 写在 server_config.json 里、托管模式下却被
        effective_config 的白名单丢掉，等于"配了但从未生效"：配 chat 的机器每个
        进程照样先打一枪必挂的 edits。同一个口子漏过的第二个键（第一个是
        qaGateLevel），所以这里钉死它。"""
        from server_common import effective_config
        with patch.object(server_common, 'SERVER_MANAGED', True), \
             patch.dict(server_common.SERVER_CONFIG,
                        {'imageEditTransport': 'chat'}, clear=False):
            merged = effective_config({})
        self.assertEqual(merged.get('imageEditTransport'), 'chat')

        edits_calls = []

        def fake_execute(req, *args, **kwargs):
            url = req.full_url if hasattr(req, 'full_url') else str(req)
            if '/images/edits' in url:
                edits_calls.append(url)
            return self._fake_transport()(req, *args, **kwargs)

        merged.update({'imageModel': 'nano-banana-2', 'imageAspectRatio': '9:16', 'apiKey': 'k'})
        with patch('frame_generator._execute_request_with_retry', side_effect=fake_execute):
            self.assertEqual(
                _generate_image_edit(merged, 'p', self.ref_path, self.target_path),
                CHAT_TRANSPORT)
        self.assertEqual(edits_calls, [])

    def test_resume_keeps_the_degradation_mark_on_reused_frames(self):
        """断点续传复用盘上已有的降档帧时，留痕必须跟着沿用——重放一次 manifest
        不能把上一轮那张 768x1376 的图洗成"正常帧"。"""
        old_root = server_common.OUTPUT_ROOT
        server_common.OUTPUT_ROOT = self.tmp
        prompt_block = "图片 1:\nfirst frame prompt\n\n图片 2:\nsecond frame prompt\n"

        def fake_text_image(config, prompt, target_path, *a, **kw):
            _write_test_image(target_path, (1536, 2752))

        def fake_image_edit(config, prompt, reference_path, target_path, *a, **kw):
            _write_test_image(target_path, (768, 1376))
            return CHAT_TRANSPORT

        try:
            with patch('frame_generator._generate_text_image', side_effect=fake_text_image), \
                 patch('frame_generator._generate_image_edit', side_effect=fake_image_edit):
                generate_frame_sequence({'coverReferencePath': self.cover}, 'chat_transport_resume', prompt_block,
                                        on_progress=lambda s, d: None)
            # 第二轮：两帧都已在盘上，一枪不打，纯复用 manifest
            with patch('frame_generator._generate_text_image',
                       side_effect=AssertionError('续传不该重渲已有帧')), \
                 patch('frame_generator._generate_image_edit',
                       side_effect=AssertionError('续传不该重渲已有帧')):
                manifest = generate_frame_sequence({'coverReferencePath': self.cover}, 'chat_transport_resume', prompt_block,
                                                   on_progress=lambda s, d: None)
        finally:
            server_common.OUTPUT_ROOT = old_root

        frames = {f['sequence']: f for f in manifest['frames']}
        self.assertEqual(frames[2]['transport'], CHAT_TRANSPORT)
        self.assertEqual(frames[2]['actual_pixels'], '768x1376')
        self.assertEqual(frames[1]['transport'], CHAT_TRANSPORT)

    def test_degraded_frames_are_recorded_in_manifest_and_announced_live(self):
        """降档帧绝不伪装成正常帧：manifest 记 transport/degraded_reason/真实像素，
        进度流当场播报——补额度后可对这些帧定向重渲换回全分辨率。"""
        import json
        old_root = server_common.OUTPUT_ROOT
        server_common.OUTPUT_ROOT = self.tmp
        events = []

        def fake_text_image(config, prompt, target_path, *a, **kw):
            _write_test_image(target_path, (1536, 2752))

        def fake_image_edit(config, prompt, reference_path, target_path, *a, **kw):
            _write_test_image(target_path, (768, 1376))
            return CHAT_TRANSPORT

        try:
            with patch('frame_generator._generate_text_image', side_effect=fake_text_image), \
                 patch('frame_generator._generate_image_edit', side_effect=fake_image_edit):
                manifest = generate_frame_sequence(
                    {'imageQuality': '2K', 'coverReferencePath': self.cover}, 'chat_transport_trace',
                    "图片 1:\nfirst frame prompt\n\n图片 2:\nsecond frame prompt\n",
                    on_progress=lambda stage, details: events.append((stage, details)))
        finally:
            server_common.OUTPUT_ROOT = old_root

        frames = {f['sequence']: f for f in manifest['frames']}
        self.assertEqual(frames[1]['transport'], CHAT_TRANSPORT)
        self.assertEqual(frames[2]['transport'], CHAT_TRANSPORT)
        self.assertIn('分辨率降档', frames[2]['degraded_reason'])
        self.assertEqual(frames[2]['actual_pixels'], '768x1376')
        self.assertEqual(frames[2]['image_size'], '2K')  # 请求的档位如实保留

        announced = [d for s, d in events if s == 'transport_fallback']
        self.assertEqual([d['sequence'] for d in announced], [1, 2])
        self.assertIn('IMG 001', announced[0]['message'])
        self.assertTrue(announced[0]['degraded'])
        # manifest 落盘的内容与返回值一致（前端读的是盘上那份）
        with open(os.path.join(manifest['project_dir'], 'manifest.json'), encoding='utf-8') as f:
            on_disk = {f_['sequence']: f_ for f_ in json.load(f)['frames']}
        self.assertEqual(on_disk[2]['transport'], CHAT_TRANSPORT)

    def test_one_k_request_is_not_marked_degraded(self):
        """chat 通道固定出 1K 档（9:16 → 768x1376，与 /images/edits 的 image_size=1K
        逐像素同档）：本单请求就是 1K 时画质没有任何损失，不许扣"降档"帽子——否则
        每一帧都挂个假警告，真降档的单子反而看不出来。"""
        old_root = server_common.OUTPUT_ROOT
        server_common.OUTPUT_ROOT = self.tmp
        events = []

        def fake_text_image(config, prompt, target_path, *a, **kw):
            _write_test_image(target_path, (768, 1376))

        def fake_image_edit(config, prompt, reference_path, target_path, *a, **kw):
            _write_test_image(target_path, (768, 1376))
            return CHAT_TRANSPORT

        try:
            with patch('frame_generator._generate_text_image', side_effect=fake_text_image), \
                 patch('frame_generator._generate_image_edit', side_effect=fake_image_edit):
                manifest = generate_frame_sequence(
                    {'imageQuality': '1K', 'coverReferencePath': self.cover}, 'chat_transport_1k',
                    "图片 1:\nfirst frame prompt\n\n图片 2:\nsecond frame prompt\n",
                    on_progress=lambda stage, details: events.append((stage, details)))
        finally:
            server_common.OUTPUT_ROOT = old_root

        frame = {f['sequence']: f for f in manifest['frames']}[2]
        self.assertEqual(frame['transport'], CHAT_TRANSPORT)   # 通道照样留痕
        self.assertEqual(frame['actual_pixels'], '768x1376')
        self.assertNotIn('degraded_reason', frame)             # 但不是降档
        announced = [d for s, d in events if s == 'transport_fallback']
        self.assertFalse(announced[0]['degraded'])
        self.assertIn('画质无损失', announced[0]['message'])


class TestQuotaSignalDetection(unittest.TestCase):
    """The account-pool token broker in front of the gateway sometimes fails with a
    plain-text body (no JSON, no "QUOTA_EXHAUSTED" marker) like:
      "Max retries exhausted. Last error: Token error: No accounts available with
       quota for model: gemini-3-pro-image"
    This must still be classified as quota exhaustion (raise QuotaExhaustedError on
    the first attempt) so the run stops on the real cause, instead of being treated
    as a generic retryable 502 that burns through all retry attempts first."""

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

    def test_emit_quota_failure_false_silences_the_broadcast_but_not_the_error(self):
        """探路请求专用：调用方有等价通道可换时，配额墙不是终点——异常照抛（调用
        方据此换路），但不往进度流推那句「此路终止，任务即将报错结束」。"""
        from frame_generator import set_upstream_event_sink
        body = ("Max retries exhausted. Last error: Token error: No accounts "
                "available with quota for model: gemini-3-pro-image")
        opener = self._FakeOpener(502, body)
        events = []
        set_upstream_event_sink(events.append)
        try:
            with self.assertRaises(QuotaExhaustedError):
                _execute_request_with_retry(self._make_request(), opener=opener, timeout=1,
                                            emit_quota_failure=False)
        finally:
            set_upstream_event_sink(None)
        self.assertEqual(events, [])
        self.assertEqual(opener.calls, 1)   # 照旧 fail-fast，不烧重试

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


class TestGptImagePixelSize(unittest.TestCase):
    """gpt-image-2 routes to the real OpenAI-shaped codex gateway (65038), which only
    understands the 'size' enum (1024x1024 / 1024x1536 / 1536x1024 / auto) — not the
    'w:h' ratio strings ('9:16') that the Gemini gateway (8046) accepts via model-name
    suffixing. Sending '9:16' straight through silently falls back to square output.
    This was the real cause of "gpt文生图不是9:16"."""

    def test_portrait_ratio_maps_to_tall_pixels(self):
        self.assertEqual(gpt_image_pixel_size('9:16'), '1024x1536')
        self.assertEqual(gpt_image_pixel_size('2:3'), '1024x1536')

    def test_landscape_ratio_maps_to_wide_pixels(self):
        self.assertEqual(gpt_image_pixel_size('16:9'), '1536x1024')
        self.assertEqual(gpt_image_pixel_size('3:2'), '1536x1024')
        self.assertEqual(gpt_image_pixel_size('21:9'), '1536x1024')

    def test_square_ratio_maps_to_square_pixels(self):
        self.assertEqual(gpt_image_pixel_size('1:1'), '1024x1024')

    def test_missing_or_malformed_ratio_falls_back_to_square(self):
        self.assertEqual(gpt_image_pixel_size(None), '1024x1024')
        self.assertEqual(gpt_image_pixel_size(''), '1024x1024')
        self.assertEqual(gpt_image_pixel_size('garbage'), '1024x1024')

    def test_image_size_to_api_size_only_converts_for_gpt_image(self):
        # Gemini gateway still gets the raw ratio string — its contract expects it.
        self.assertEqual(_image_size_to_api_size('9:16', model='gemini-3.1-flash-image'), '9:16')
        self.assertEqual(_image_size_to_api_size('9:16', model=None), '9:16')
        # gpt-image-2 gets the converted pixel size.
        self.assertEqual(_image_size_to_api_size('9:16', model='gpt-image-2'), '1024x1536')


class TestAnchorInertiaQuotaFallback(unittest.TestCase):
    """P1 惯性加强重试始终保持 i2i，且与主路径共用配额处置。"""

    _PROMPT_BLOCK = """图片 1:
exterior prompt

图片 2:
interior prompt

视频 1 [BRIDGE]:
bridge video
"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old_output_root = server_common.OUTPUT_ROOT
        server_common.OUTPUT_ROOT = self.tmp
        self.cover = _make_test_cover(self.tmp)

    def tearDown(self):
        server_common.OUTPUT_ROOT = self.old_output_root
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _write(target_path, content):
        os.makedirs(os.path.dirname(target_path), exist_ok=True)
        with open(target_path, 'wb') as f:
            f.write(content.encode('utf-8'))

    def _run(self, config, retry_error=None):
        events = []
        edit_calls = []

        def fake_image_edit(cfg, prompt, reference_path, target_path, *a, **kw):
            edit_calls.append(reference_path)
            if len(edit_calls) == 3 and retry_error:
                raise retry_error
            self._write(target_path, 'edit:stuck-duplicate')

        config = dict(config, coverReferencePath=self.cover)
        with patch('frame_generator._generate_text_image') as text_image, \
             patch('frame_generator._generate_image_edit', side_effect=fake_image_edit), \
             patch('frame_generator.detect_anchor_inertia', return_value=(True, 1.5)):
            manifest = generate_frame_sequence(
                config, 'inertia_quota_fallback', self._PROMPT_BLOCK,
                on_progress=lambda stage, details: events.append((stage, details)))
        text_image.assert_not_called()
        return manifest, events, edit_calls

    def test_inertia_i2i_quota_exhaustion_aborts_the_run(self):
        """惯性重渲撞上配额耗尽：原样上抛，不切模型、不留复读帧继续往下渲。"""
        config = {'imageModel': 'primary-model', 'imageEditFallbackModel': 'fallback-model'}
        with self.assertRaises(QuotaExhaustedError):
            self._run(config, QuotaExhaustedError('primary exhausted'))

    def test_inertia_i2i_non_quota_failure_keeps_frame_with_reason(self):
        """非配额原因的 i2i 加强重试失败：保留原帧并留痕，不炸掉整单。"""
        config = {'imageModel': 'primary-model'}
        manifest, _, edit_calls = self._run(config, RuntimeError('i2i upstream boom'))

        self.assertEqual(len(edit_calls), 3)
        frame2 = next(f for f in manifest['frames'] if f['sequence'] == 2)
        self.assertIn('anchor_inertia', frame2['vlm_qa_reason'])
        self.assertIn('i2i 加强重试失败', frame2['vlm_qa_reason'])
        with open(po_frame_path('inertia_quota_fallback', 2), 'rb') as f:
            self.assertEqual(f.read(), b'edit:stuck-duplicate')


def po_frame_path(title, seq):
    project_dir = server_common._get_project_dir(title)
    return os.path.join(project_dir, 'frames', f'img_{seq:03d}.webp')


class TestDecodeImageAspectCrop(unittest.TestCase):
    """网关对部分 t2i 模型（实测 gpt-image-2）无视 size/aspect_ratio 固定出方图，
    闭源改不了——落盘前必须按配置比例居中裁剪，否则方帧混进 9:16 链。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _square_png_b64(self, size=128):
        import base64
        from PIL import Image
        buf = io.BytesIO()
        Image.new('RGB', (size, size), (200, 50, 50)).save(buf, format='PNG')
        return base64.b64encode(buf.getvalue()).decode('ascii')

    def test_square_output_is_cropped_to_configured_aspect(self):
        from PIL import Image
        from frame_generator import _decode_or_download_image
        target = os.path.join(self.tmp, 'img_001.webp')
        _decode_or_download_image({'b64_json': self._square_png_b64()}, target,
                                  {'imageAspectRatio': '9:16'})
        with Image.open(target) as im:
            w, h = im.size
        self.assertAlmostEqual(w / h, 9 / 16, delta=0.02)

    def test_matching_aspect_is_untouched_and_auto_skips_crop(self):
        from PIL import Image
        from frame_generator import _decode_or_download_image
        target = os.path.join(self.tmp, 'img_002.webp')
        _decode_or_download_image({'b64_json': self._square_png_b64()}, target,
                                  {'imageAspectRatio': 'auto'})
        with Image.open(target) as im:
            self.assertEqual(im.size, (128, 128))


if __name__ == '__main__':
    unittest.main()
