"""Tests for the Video Prompt Visual Delta Optimization Gate.

Covers:
- prompt_pipeline.video_optimizer.optimize_single_video_prompt
- prompt_pipeline.video_optimizer.optimize_video_prompts_for_sequence
- server.py generate_videos_worker & /api/optimize_video_prompts endpoint
- pipeline_orchestrator.py run_autonomous_pipeline & run_staged_frame_rendering
- stepped_pipeline.py advance_stepped_pipeline (final_review stage)
"""
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from PIL import Image

import server_common
import prompt_pipeline as pp
from prompt_pipeline.video_optimizer import (
    optimize_single_video_prompt,
    optimize_video_prompts_for_sequence,
)


class TestOptimizeSingleVideoPrompt(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.img1_path = os.path.join(self.tmp_dir, 'img_001.webp')
        self.img2_path = os.path.join(self.tmp_dir, 'img_002.webp')
        Image.new('RGB', (100, 100), color='red').save(self.img1_path)
        Image.new('RGB', (100, 100), color='blue').save(self.img2_path)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_normal_optimization(self):
        vlm_resp = json.dumps({
            "optimized_prompt": (
                "Use the provided first frame and last frame as exact composition anchors. "
                "Use IMAGE 1 as the actual first-frame image and IMAGE 2 as the actual last-frame image; "
                "every visible action must interpolate between those two frame images without inventing a third layout. "
                "At zero seconds a lone male worker (1.78m tall, occupying ~35% of vertical frame height) "
                "clears the ground with a steel shovel. Continuous construction time-lapse, not real-time footage. "
                "Sound effects: gritty shovel scraping on stone, heavy thuds."
            ),
            "visual_delta_summary": "Cleared leaves and exposed gravel floor."
        })
        with patch.object(pp, '_multimodal_chat', return_value=vlm_resp) as mock_chat:
            res = optimize_single_video_prompt(
                {}, self.img1_path, self.img2_path, "Original draft video 1", slot_index=1
            )
            self.assertTrue(mock_chat.called)
            self.assertIn("Use the provided first frame and last frame as exact composition anchors", res)
            self.assertIn("lone male worker", res)
            self.assertIn("Sound effects", res)

    def test_missing_images_safe_fallback(self):
        res = optimize_single_video_prompt(
            {}, "nonexistent_1.webp", self.img2_path, "Original draft video 1", slot_index=1
        )
        self.assertEqual(res, "Original draft video 1")

        res2 = optimize_single_video_prompt(
            {}, self.img1_path, None, "Original draft video 1", slot_index=1
        )
        self.assertEqual(res2, "Original draft video 1")

    def test_vlm_exception_fallback(self):
        with patch.object(pp, '_multimodal_chat', side_effect=RuntimeError("Gateway timeout")):
            res = optimize_single_video_prompt(
                {}, self.img1_path, self.img2_path, "Original draft video 1", slot_index=1
            )
            self.assertEqual(res, "Original draft video 1")

    def test_ensures_anchor_header_if_missing_in_vlm_response(self):
        vlm_resp = json.dumps({
            "optimized_prompt": (
                "At zero seconds the lone worker measures and bolts five low-profile rafters. "
                "Continuous construction time-lapse, not real-time footage. Sound effects: drill chatter."
            ),
            "visual_delta_summary": "Added roof rafters."
        })
        with patch.object(pp, '_multimodal_chat', return_value=vlm_resp):
            res = optimize_single_video_prompt(
                {}, self.img1_path, self.img2_path, "Original draft", slot_index=2, start_seq=2, end_seq=3
            )
            self.assertTrue(res.startswith("Use the provided first frame and last frame as exact composition anchors."))
            self.assertIn("Use IMAGE 2 as the actual first-frame image and IMAGE 3 as the actual last-frame image", res)


class TestOptimizeVideoPromptsForSequence(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.project_title = "test_opt_project"
        self.project_dir = os.path.join(self.tmp_dir, self.project_title)
        self.frames_dir = os.path.join(self.project_dir, 'frames')
        os.makedirs(self.frames_dir, exist_ok=True)

        # Create dummy frame images
        for i in range(1, 4):
            img_p = os.path.join(self.frames_dir, f'img_{i:03d}.webp')
            Image.new('RGB', (100, 100), color='green').save(img_p)

        self.prompt_block = (
            "图片提示词\n"
            "图片 1:\nImage 1 prompt\n\n"
            "图片 2:\nImage 2 prompt\n\n"
            "图片 3:\nImage 3 prompt\n\n"
            "视频提示词\n"
            "视频 1:\nDraft video 1 prompt\n\n"
            "视频 2:\nDraft video 2 prompt\n"
        )

        self.patch_dir = patch.object(server_common, '_get_project_dir', return_value=self.project_dir)
        self.patch_dir.start()

    def tearDown(self):
        self.patch_dir.stop()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_sequence_optimization_updates_prompts_and_manifest(self):
        def fake_vlm(config, system, user_text, images, **kwargs):
            if "IMAGE 1" in user_text:
                return json.dumps({"optimized_prompt": "Optimized VIDEO 1 body with anchors."})
            elif "IMAGE 2" in user_text:
                return json.dumps({"optimized_prompt": "Optimized VIDEO 2 body with anchors."})
            return json.dumps({"optimized_prompt": "Optimized fallback."})

        events = []
        def progress_cb(stage, details):
            events.append((stage, details))

        with patch.object(pp, '_multimodal_chat', side_effect=fake_vlm):
            new_block = optimize_video_prompts_for_sequence(
                {}, self.project_title, self.prompt_block, on_progress=progress_cb
            )

            self.assertIn("Optimized VIDEO 1 body", new_block)
            self.assertIn("Optimized VIDEO 2 body", new_block)

            # Check manifest.json updated
            manifest = server_common.read_manifest(self.project_dir)
            self.assertIsNotNone(manifest)
            self.assertIn("Optimized VIDEO 1 body", manifest.get('prompt_block', ''))
            
            # Check video_prompt_optimizations persisted
            opts = manifest.get('video_prompt_optimizations', {})
            self.assertIn('1', opts)
            self.assertIn('2', opts)
            self.assertIn('fingerprint', opts['1'])
            self.assertIn('Optimized VIDEO 1 body with anchors.', opts['1']['optimized_prompt'])

            # Check events emitted
            stage_names = [e[0] for e in events]
            self.assertIn('video_optimization_start', stage_names)
            self.assertIn('video_optimization_slot', stage_names)
            self.assertIn('prompt_block_updated', stage_names)

    def test_caching_and_persistence_avoids_repeated_vlm_on_restart(self):
        vlm_calls = []
        def fake_vlm(config, system, user_text, images, **kwargs):
            vlm_calls.append(user_text)
            if "IMAGE 1" in user_text:
                return json.dumps({"optimized_prompt": "Optimized VIDEO 1 body."})
            return json.dumps({"optimized_prompt": "Optimized VIDEO 2 body."})

        with patch.object(pp, '_multimodal_chat', side_effect=fake_vlm):
            # First run: optimizes and persists
            first_block = optimize_video_prompts_for_sequence(
                {}, self.project_title, self.prompt_block
            )
            self.assertEqual(len(vlm_calls), 2)
            self.assertIn("Optimized VIDEO 1 body", first_block)

            # Second run (restart/rerun): should hit persistent cache, 0 VLM calls
            vlm_calls.clear()
            second_block = optimize_video_prompts_for_sequence(
                {}, self.project_title, first_block
            )
            self.assertEqual(len(vlm_calls), 0)
            self.assertEqual(second_block, first_block)

            # Even if old un-optimized prompt_block is passed, it recovers from persistent cache
            third_block = optimize_video_prompts_for_sequence(
                {}, self.project_title, self.prompt_block
            )
            self.assertEqual(len(vlm_calls), 0)
            self.assertIn("Optimized VIDEO 1 body", third_block)

    def test_incremental_optimization_when_single_frame_changed(self):
        vlm_calls = []
        def fake_vlm(config, system, user_text, images, **kwargs):
            vlm_calls.append(user_text)
            if "IMAGE 1" in user_text:
                return json.dumps({"optimized_prompt": "Re-optimized VIDEO 1 body."})
            elif "IMAGE 2" in user_text:
                return json.dumps({"optimized_prompt": "Re-optimized VIDEO 2 body."})
            return json.dumps({"optimized_prompt": "Re-optimized fallback."})

        with patch.object(pp, '_multimodal_chat', side_effect=fake_vlm):
            # Initial run
            opt_block = optimize_video_prompts_for_sequence(
                {}, self.project_title, self.prompt_block
            )
            self.assertEqual(len(vlm_calls), 2)

            # Now modify only img_001.webp (affects only slot 1, not slot 2)
            img1_path = os.path.join(self.frames_dir, 'img_001.webp')
            Image.new('RGB', (120, 120), color='yellow').save(img1_path)

            vlm_calls.clear()
            new_block = optimize_video_prompts_for_sequence(
                {}, self.project_title, opt_block
            )
            # Only slot 1 should call VLM because img_001 changed; slot 2 (img_002 -> img_003) is cached!
            self.assertEqual(len(vlm_calls), 1)
            self.assertIn("IMAGE 1", vlm_calls[0])

    def test_force_flag_bypasses_cache(self):
        vlm_calls = []
        def fake_vlm(config, system, user_text, images, **kwargs):
            vlm_calls.append(user_text)
            return json.dumps({"optimized_prompt": "Forced optimization."})

        with patch.object(pp, '_multimodal_chat', side_effect=fake_vlm):
            # First run
            optimize_video_prompts_for_sequence(
                {}, self.project_title, self.prompt_block
            )
            self.assertEqual(len(vlm_calls), 2)

            # Second run with force=True
            vlm_calls.clear()
            optimize_video_prompts_for_sequence(
                {}, self.project_title, self.prompt_block, force=True
            )
            self.assertEqual(len(vlm_calls), 2)

    def test_empty_prompt_block_loads_from_manifest(self):
        manifest_data = {
            'title': self.project_title,
            'prompt_block': self.prompt_block,
            'frames': [],
            'videos': [],
        }
        server_common.write_manifest(self.project_dir, manifest_data)

        with patch.object(pp, '_multimodal_chat', return_value=json.dumps({"optimized_prompt": "Optimized from manifest."})):
            res = optimize_video_prompts_for_sequence({}, self.project_title, "")
            self.assertIn("Optimized from manifest", res)

    def test_disabled_by_config_flag(self):
        cfg = {'optimizeVideoPromptsBeforeGen': False}
        with patch.object(pp, '_multimodal_chat') as mock_vlm:
            res = optimize_video_prompts_for_sequence(cfg, self.project_title, self.prompt_block)
            self.assertEqual(res, self.prompt_block)
            self.assertFalse(mock_vlm.called)

    def test_target_slots_subset(self):
        def fake_vlm(config, system, user_text, images, **kwargs):
            return json.dumps({"optimized_prompt": "Only slot 2 optimized."})

        with patch.object(pp, '_multimodal_chat', side_effect=fake_vlm):
            new_block = optimize_video_prompts_for_sequence(
                {}, self.project_title, self.prompt_block, target_slots=[2]
            )
            # Slot 1 should stay original draft, slot 2 should be updated
            self.assertIn("Draft video 1 prompt", new_block)
            self.assertIn("Only slot 2 optimized.", new_block)


class TestPipelineGateIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.project_title = "test_pipeline_opt"
        self.project_dir = os.path.join(self.tmp_dir, self.project_title)
        os.makedirs(self.project_dir, exist_ok=True)
        self.patch_dir = patch.object(server_common, '_get_project_dir', return_value=self.project_dir)
        self.patch_dir.start()

    def tearDown(self):
        self.patch_dir.stop()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_orchestrator_autonomous_calls_optimization(self):
        import pipeline_orchestrator as po
        with patch.object(po, 'compose_anchor_and_packet', return_value={'title': self.project_title, 'image_1_prompt': 'p1', 'packet': {}}), \
             patch.object(po, 'render_single_frame', return_value={'project_dir': self.project_dir, 'image_path': 'img_001.webp'}), \
             patch.object(po, 'refine_packet_from_accepted_anchor', return_value={}), \
             patch.object(po, 'compose_remaining_beats', return_value='PROMPTS:\n图片 1:\nP1\n图片 2:\nP2\n视频 1:\nV1\n'), \
             patch.object(po, 'generate_frame_sequence'), \
             patch.object(po, 'optimize_video_prompts_for_sequence', return_value='OPTIMIZED PROMPTS') as mock_opt, \
             patch.object(po, '_render_videos_with_recovery', return_value={'videos': []}):

            res = po.run_autonomous_pipeline({}, {})
            self.assertTrue(mock_opt.called)
            self.assertEqual(res['prompt_block'], 'OPTIMIZED PROMPTS')

    def test_orchestrator_staged_calls_optimization(self):
        import pipeline_orchestrator as po
        prompt_block = "图片 1:\nP1\n图片 2:\nP2\n视频 1:\nV1\n"
        with patch.object(po, 'generate_frame_sequence'), \
             patch.object(po, 'optimize_video_prompts_for_sequence', return_value='OPTIMIZED STAGED') as mock_opt, \
             patch.object(po, '_render_videos_with_recovery', return_value={'videos': []}):

            res = po.run_staged_frame_rendering({}, self.project_title, prompt_block)
            self.assertTrue(mock_opt.called)
            self.assertEqual(res['prompt_block'], 'OPTIMIZED STAGED')

    def test_stepped_pipeline_final_review_calls_optimization(self):
        import stepped_pipeline as sp
        state = {
            'pipeline_id': 'stepped_test',
            'title': self.project_title,
            'stage': 'final_review',
            'prompt_block': '图片 1:\nP1\n图片 2:\nP2\n视频 1:\nV1\n',
        }
        sp._save_state(self.project_title, state)

        with patch.object(sp, 'optimize_video_prompts_for_sequence', return_value='OPTIMIZED STEPPED') as mock_opt, \
             patch.object(sp, '_render_videos_with_recovery', return_value={'videos': []}):

            res = sp.advance_stepped_pipeline(self.project_title, action='approve', config={})
            self.assertTrue(mock_opt.called)
            self.assertEqual(res['prompt_block'], 'OPTIMIZED STEPPED')
            self.assertEqual(res['stage'], 'completed')

    def test_server_generate_videos_worker_calls_optimization(self):
        import server
        prompt_block = "图片 1:\nP1\n图片 2:\nP2\n视频 1:\nV1\n"
        with patch('server.optimize_video_prompts_for_sequence', return_value='OPTIMIZED PROMPTS') as mock_opt, \
             patch('server.generate_video_sequence', return_value={'videos': [{'slot': 1, 'status': 'success'}]}) as mock_gen, \
             patch('server.merge_project_videos', return_value={'status': 'ok'}):

            task_id = "test_vid_task_123"
            server.get_or_create_task(task_id)
            server.generate_videos_worker(
                task_id, {}, self.project_title, prompt_block, target_slots=None
            )
            self.assertTrue(mock_opt.called)
            # The prompt_block passed to generate_video_sequence should be the optimized one
            self.assertEqual(mock_gen.call_args[0][2], 'OPTIMIZED PROMPTS')

    def test_server_optimize_video_prompts_endpoint(self):
        import io
        import server
        prompt_block = "图片 1:\nP1\n图片 2:\nP2\n视频 1:\nV1\n"
        with patch('server.optimize_video_prompts_for_sequence', return_value='OPTIMIZED ENDPOINT') as mock_opt:
            req = MagicMock()
            req.command = 'POST'
            req.path = '/api/optimize_video_prompts'
            body = json.dumps({
                'title': self.project_title,
                'prompt_block': prompt_block,
                'target_slots': [1],
            }).encode('utf-8')
            req.headers = {'Content-Length': str(len(body)), 'Content-Type': 'application/json'}
            req.rfile = io.BytesIO(body)
            req.wfile = io.BytesIO()

            with patch('server.access_ok', return_value=True):
                handler = server.SparkRequestHandler.__new__(server.SparkRequestHandler)
                handler.headers = req.headers
                handler.rfile = req.rfile
                handler.wfile = req.wfile
                handler.path = '/api/optimize_video_prompts'
                
                # Mock _send_json
                sent = {}
                def fake_send_json(data, status=200):
                    sent['data'] = data
                    sent['status'] = status
                handler._send_json = fake_send_json

                handler.do_POST()
                self.assertTrue(mock_opt.called)
                self.assertEqual(sent.get('status'), 200)
                self.assertEqual(sent.get('data', {}).get('prompt_block'), 'OPTIMIZED ENDPOINT')


if __name__ == '__main__':
    unittest.main()
