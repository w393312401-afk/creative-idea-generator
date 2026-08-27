# -*- coding: utf-8 -*-
"""纯视频提示词链式生成通道 (T2V -> I2V Chain) 单元测试。

测试覆盖：
1. prepare_prompt_for_t2v: 净化图生图/两卡位插值样板句，保留运镜动作音效；
2. generate_video_chain_sequence:
   - Slot 1 执行纯文生视频 (T2V，无参考图输入)；
   - Slot 1 完成后自动抽取尾帧为 Slot 2 的参考帧；
   - Slot 2..N 依次执行单图生视频 (I2V 单参考帧)；
   - 自动记录 frames 与 videos 到 manifest.json 并调用 merge_project_videos；
3. 断点与子集槽位执行；
4. 5 列多宫格拼图生成。
"""

import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from video_generator import (
    prepare_prompt_for_t2v,
    rewrite_prompt_for_single_frame,
    generate_video_chain_sequence,
    generate_video_collage,
)


class TestPreparePromptForT2V(unittest.TestCase):
    """测试文生视频提示词净化。"""

    def test_strip_two_anchor_and_interpolation_boilerplate(self):
        prompt = (
            "Use the provided first frame and last frame as exact composition anchors. "
            "Use IMAGE 1 as the actual first-frame image and IMAGE 2 as the actual last-frame image; "
            "every visible action must interpolate between those two frame images without inventing a third layout. "
            "Continuous construction time-lapse, not real-time footage. "
            "Locked camera on a 24mm wide-angle lens at 1.3m chest level. "
            "A craftsman enters the cabin and sweeps the dusty floor with a broom. "
            "SFX: soft sweeping sound."
        )
        out = prepare_prompt_for_t2v(prompt)
        self.assertNotIn("first-frame", out.lower())
        self.assertNotIn("last-frame", out.lower())
        self.assertNotIn("exact composition anchors", out.lower())
        self.assertNotIn("interpolate between", out.lower())
        self.assertNotIn("IMAGE 1", out)
        self.assertNotIn("IMAGE 2", out)
        self.assertIn("Continuous construction time-lapse", out)
        self.assertIn("24mm wide-angle lens", out)
        self.assertIn("craftsman enters the cabin", out)
        self.assertIn("SFX: soft sweeping sound", out)

    def test_strip_single_anchor_opening(self):
        prompt = (
            "Use the provided reference image (IMAGE 1) as the sole starting-frame anchor for this clip "
            "— use IMAGE 1 as the actual first-frame image. There is no last-frame reference image for "
            "this clip, so the clip must reach its own finished state by the final frame; hold the same "
            "space, the same locked camera family, and every landmark from IMAGE 1 throughout, and "
            "invent no new layout, no new room, and no new structure. "
            "The worker uses a spirit level to check the timber framework. SFX: wooden tap."
        )
        out = prepare_prompt_for_t2v(prompt)
        self.assertNotIn("sole starting-frame anchor", out.lower())
        self.assertNotIn("no last-frame reference image", out.lower())
        self.assertNotIn("IMAGE 1", out)
        self.assertIn("The worker uses a spirit level", out)
        self.assertIn("SFX: wooden tap", out)

    def test_chinese_prompt_and_clean_text(self):
        prompt = (
            "图片 1 到 图片 2。工人手持铁锹铲除地面碎石，扬起微小灰尘。"
        )
        out = prepare_prompt_for_t2v(prompt)
        self.assertNotIn("图片 1", out)
        self.assertNotIn("图片 2", out)
        self.assertIn("工人手持铁锹铲除地面碎石", out)

    def test_empty_prompt(self):
        self.assertEqual(prepare_prompt_for_t2v(""), "")
        self.assertEqual(prepare_prompt_for_t2v(None), "")


class TestVideoChainSequenceGeneration(unittest.TestCase):
    """测试视频链全自动生成流程。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.config = {
            'videoModel': 'Veo 3.1 - Fast',
            'imageAspectRatio': '9:16',
            '_project_key': 'test_proj',
            '_merge_speed': 2,
        }
        self.title = 'test_proj'
        self.prompt_block = (
            "视频提示词\n\n"
            "视频 1:\n"
            "Use the provided reference image (IMAGE 1) as the first-frame image and IMAGE 2 as the last-frame image. "
            "A lone craftsman opens the rusted vault door and steps into the abandoned bunker. SFX: door creak.\n\n"
            "视频 2:\n"
            "Use the provided reference image (IMAGE 2) as first frame and IMAGE 3 as last frame. "
            "The craftsman uses a broom to sweep away debris on the concrete floor. SFX: sweeping sound.\n\n"
            "视频 3:\n"
            "Use the provided reference image (IMAGE 3) as first frame and IMAGE 4 as last frame. "
            "The craftsman lays down moisture barrier sheets across the floor. SFX: plastic rustle."
        )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    @patch('video_generator._get_project_dir')
    @patch('video_generator._get_google_fx_video_service')
    @patch('video_generator._extract_video_frame')
    @patch('video_generator.merge_project_videos')
    @patch('video_generator.generate_video_collage')
    def test_sequential_t2v_then_i2v_chain(self, mock_collage, mock_merge, mock_extract, mock_fx_svc, mock_proj_dir):
        mock_proj_dir.return_value = self.tmp
        mock_merge.return_value = {'file': 'videos/test_proj_2x.mp4', 'url': '/videos/test_proj_2x.mp4', 'speed': 2}
        mock_collage.return_value = os.path.join(self.tmp, 'frames', 'full_collage.jpg')

        recorded_requests = []

        def fake_generate_batch(reqs, on_progress=None, cancel_check=None):
            for req in reqs:
                recorded_requests.append(req)
                # Create fake downloaded mp4 in req.output_path
                fake_mp4 = os.path.join(req.output_path, 'fake_download.mp4')
                with open(fake_mp4, 'wb') as f:
                    f.write(b'fake_video_bytes')
            return [{'project_url': 'https://flow.google.com/test_canvas'}]

        mock_svc = MagicMock()
        mock_svc.generate_videos_batch_google_fx.side_effect = fake_generate_batch

        from integrations.google_fx import models
        mock_fx_svc.return_value = (mock_svc, models)

        def fake_extract(video_path, out_frame, pos, **kwargs):
            os.makedirs(os.path.dirname(out_frame), exist_ok=True)
            with open(out_frame, 'wb') as f:
                f.write(b'fake_frame_bytes')
            return True

        mock_extract.side_effect = fake_extract

        progress_events = []
        def on_progress(stage, data):
            progress_events.append((stage, data))

        res = generate_video_chain_sequence(
            self.config, self.title, self.prompt_block, on_progress=on_progress
        )

        # 1. 验证生成了 3 段视频
        self.assertEqual(len(recorded_requests), 3)

        # 2. 验证 Slot 1 为纯文生视频 (image='', end_image='')
        req1 = recorded_requests[0]
        self.assertEqual(req1.image, '')
        self.assertEqual(req1.end_image, '')
        self.assertNotIn('IMAGE 1', req1.prompt)
        self.assertNotIn('first-frame', req1.prompt.lower())
        self.assertIn('lone craftsman opens the rusted vault door', req1.prompt)

        # 3. 验证 Slot 2 为单图参考 (image=img_002.webp, end_image='')
        req2 = recorded_requests[1]
        self.assertTrue(req2.image.endswith('img_002.webp'))
        self.assertEqual(req2.end_image, '')
        self.assertIn('sole starting-frame anchor', req2.prompt.lower())
        self.assertIn('craftsman uses a broom', req2.prompt)

        # 4. 验证 Slot 3 为单图参考 (image=img_003.webp, end_image='')
        req3 = recorded_requests[2]
        self.assertTrue(req3.image.endswith('img_003.webp'))
        self.assertEqual(req3.end_image, '')
        self.assertIn('sole starting-frame anchor', req3.prompt.lower())
        self.assertIn('craftsman lays down moisture barrier sheets', req3.prompt)

        # 5. 验证 manifest 数据与自动合并
        manifest_path = os.path.join(self.tmp, 'manifest.json')
        self.assertTrue(os.path.exists(manifest_path))
        with open(manifest_path, 'r', encoding='utf-8') as f:
            m = json.load(f)

        self.assertEqual(m.get('generation_channel'), 'video_chain')
        self.assertEqual(len(m.get('videos', [])), 3)
        self.assertIn('merged_video', m)
        self.assertEqual(m['merged_video']['speed'], 2)

        # 6. 验证抽取出的 frames 列表
        slots_extracted = [f['slot'] for f in m.get('frames', [])]
        self.assertIn(1, slots_extracted)  # 首段首帧
        self.assertIn(2, slots_extracted)  # 第1段尾帧 (供第2段用)
        self.assertIn(3, slots_extracted)  # 第2段尾帧 (供第3段用)
        self.assertIn(4, slots_extracted)  # 第3段尾帧

    @patch('video_generator._get_project_dir')
    @patch('video_generator._get_google_fx_video_service')
    @patch('video_generator._extract_video_frame')
    @patch('video_generator.merge_project_videos')
    @patch('video_generator.generate_video_collage')
    def test_target_slots_subset_execution(self, mock_collage, mock_merge, mock_extract, mock_fx_svc, mock_proj_dir):
        """测试指定 target_slots=[2] 仅重跑 Slot 2。"""
        mock_proj_dir.return_value = self.tmp
        mock_merge.return_value = None
        mock_collage.return_value = None

        # 预先落盘 vid_001.mp4 与 img_002.webp
        videos_dir = os.path.join(self.tmp, 'videos')
        frames_dir = os.path.join(self.tmp, 'frames')
        os.makedirs(videos_dir, exist_ok=True)
        os.makedirs(frames_dir, exist_ok=True)
        with open(os.path.join(videos_dir, 'vid_001.mp4'), 'wb') as f:
            f.write(b'vid1')
        with open(os.path.join(frames_dir, 'img_002.webp'), 'wb') as f:
            f.write(b'frame2')

        recorded_requests = []
        def fake_generate_batch(reqs, on_progress=None, cancel_check=None):
            for req in reqs:
                recorded_requests.append(req)
                fake_mp4 = os.path.join(req.output_path, 'fake_download.mp4')
                with open(fake_mp4, 'wb') as f:
                    f.write(b'vid2')
            return []

        mock_svc = MagicMock()
        mock_svc.generate_videos_batch_google_fx.side_effect = fake_generate_batch
        from integrations.google_fx import models
        mock_fx_svc.return_value = (mock_svc, models)

        mock_extract.return_value = True

        res = generate_video_chain_sequence(
            self.config, self.title, self.prompt_block, target_slots=[2]
        )

        self.assertEqual(len(recorded_requests), 1)
        self.assertTrue(recorded_requests[0].image.endswith('img_002.webp'))

    @patch('video_generator._get_project_dir')
    @patch('video_generator._get_google_fx_video_service')
    @patch('video_generator._extract_video_frame')
    @patch('video_generator.merge_project_videos')
    @patch('video_generator.generate_video_collage')
    def test_missing_previous_video_reports_error_and_continues(self, mock_collage, mock_merge, mock_extract, mock_fx_svc, mock_proj_dir):
        """若上一段视频缺失且无法提取尾帧，槽位应被标记失败且向后安全处理。"""
        mock_proj_dir.return_value = self.tmp
        mock_merge.return_value = None
        mock_collage.return_value = None
        mock_extract.return_value = False

        recorded_requests = []
        def fake_generate_batch(reqs, on_progress=None, cancel_check=None):
            for req in reqs:
                recorded_requests.append(req)
                fake_mp4 = os.path.join(req.output_path, 'fake_download.mp4')
                with open(fake_mp4, 'wb') as f:
                    f.write(b'vid')
            return []

        mock_svc = MagicMock()
        mock_svc.generate_videos_batch_google_fx.side_effect = fake_generate_batch
        from integrations.google_fx import models
        mock_fx_svc.return_value = (mock_svc, models)

        errors = []
        def on_progress(stage, data):
            if stage == 'video_error':
                errors.append(data)

        # 仅请求 slot 2，但由于没有 slot 1 视频与 img_002.webp，提取会失败
        res = generate_video_chain_sequence(
            self.config, self.title, self.prompt_block, target_slots=[2], on_progress=on_progress
        )

        self.assertEqual(len(recorded_requests), 0)
        self.assertEqual(len(errors), 1)
        self.assertIn('缺少上一段视频', errors[0]['message'])

    @patch('video_generator._get_project_dir')
    @patch('video_generator._get_google_fx_video_service')
    @patch('video_generator._extract_video_frame')
    @patch('video_generator.merge_project_videos')
    @patch('video_generator.generate_video_collage')
    def test_user_cancellation_stops_chain(self, mock_collage, mock_merge, mock_extract, mock_fx_svc, mock_proj_dir):
        """用户取消信号应立即停止后续槽位。"""
        mock_proj_dir.return_value = self.tmp
        mock_merge.return_value = None
        mock_collage.return_value = None

        cancelled = {'flag': False}
        def on_progress(stage, data):
            if stage == 'cancel_check':
                return cancelled['flag']
            return None

        def fake_generate_batch(reqs, on_progress=None, cancel_check=None):
            # slot 1 完成后触发取消
            cancelled['flag'] = True
            fake_mp4 = os.path.join(reqs[0].output_path, 'fake_download.mp4')
            with open(fake_mp4, 'wb') as f:
                f.write(b'vid1')
            return []

        mock_svc = MagicMock()
        mock_svc.generate_videos_batch_google_fx.side_effect = fake_generate_batch
        from integrations.google_fx import models
        mock_fx_svc.return_value = (mock_svc, models)
        mock_extract.return_value = True

        res = generate_video_chain_sequence(
            self.config, self.title, self.prompt_block, on_progress=on_progress
        )

        # 仅生成了 Slot 1，Slot 2/3 被取消跳过
        self.assertEqual(mock_svc.generate_videos_batch_google_fx.call_count, 1)

    @patch('video_generator._get_project_dir')
    @patch('video_generator._get_google_fx_video_service')
    @patch('video_generator._extract_video_frame')
    @patch('video_generator.merge_project_videos')
    @patch('video_generator.generate_video_collage')
    def test_resume_and_reuse_existing_videos_in_chain(self, mock_collage, mock_merge, mock_extract, mock_fx_svc, mock_proj_dir):
        """测试已存在的有效视频在整链重跑时被复用，仅渲染缺失槽位。"""
        mock_proj_dir.return_value = self.tmp
        mock_merge.return_value = {'file': 'videos/merged.mp4', 'url': '/videos/merged.mp4', 'speed': 2}
        mock_collage.return_value = None

        # 预先创建 vid_001.mp4, vid_002.mp4 及 manifest.json
        videos_dir = os.path.join(self.tmp, 'videos')
        frames_dir = os.path.join(self.tmp, 'frames')
        os.makedirs(videos_dir, exist_ok=True)
        os.makedirs(frames_dir, exist_ok=True)
        with open(os.path.join(videos_dir, 'vid_001.mp4'), 'wb') as f:
            f.write(b'vid1_bytes')
        with open(os.path.join(videos_dir, 'vid_002.mp4'), 'wb') as f:
            f.write(b'vid2_bytes')

        manifest_path = os.path.join(self.tmp, 'manifest.json')
        with open(manifest_path, 'w', encoding='utf-8') as f:
            json.dump({
                'generation_channel': 'video_chain',
                'videos': [
                    {'slot': 1, 'sequence': 1, 'status': 'success', 'file': 'videos/vid_001.mp4'},
                    {'slot': 2, 'sequence': 2, 'status': 'success', 'file': 'videos/vid_002.mp4'},
                ]
            }, f)

        recorded_requests = []
        def fake_generate_batch(reqs, on_progress=None, cancel_check=None):
            for req in reqs:
                recorded_requests.append(req)
                fake_mp4 = os.path.join(req.output_path, 'fake_download.mp4')
                with open(fake_mp4, 'wb') as f:
                    f.write(b'vid3_bytes')
            return []

        mock_svc = MagicMock()
        mock_svc.generate_videos_batch_google_fx.side_effect = fake_generate_batch
        from integrations.google_fx import models
        mock_fx_svc.return_value = (mock_svc, models)

        def fake_extract(video_path, out_frame, pos, **kwargs):
            os.makedirs(os.path.dirname(out_frame), exist_ok=True)
            with open(out_frame, 'wb') as f:
                f.write(b'frame_bytes')
            return True

        mock_extract.side_effect = fake_extract

        res = generate_video_chain_sequence(
            self.config, self.title, self.prompt_block
        )

        # 验证只对 Slot 3 真正调用了 Google FX 生成
        self.assertEqual(len(recorded_requests), 1)
        self.assertTrue(recorded_requests[0].image.endswith('img_003.webp'))

        # 验证 manifest 中 1, 2, 3 全部成功
        with open(manifest_path, 'r', encoding='utf-8') as f:
            m = json.load(f)
        self.assertEqual(len(m.get('videos', [])), 3)
        self.assertTrue(all(v.get('status') == 'success' for v in m['videos']))


if __name__ == '__main__':
    unittest.main()
