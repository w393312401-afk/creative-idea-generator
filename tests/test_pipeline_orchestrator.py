import os
import json
import shutil
import tempfile
import unittest
from unittest.mock import patch

import server_common
from pipeline_orchestrator import (
    _prompt_fingerprint,
    _retry_frame_until_pass,
    render_and_gate_single_frame,
    run_autonomous_pipeline,
    run_staged_frame_rendering,
)


class TestRetryFrameUntilPass(unittest.TestCase):
    """Shared retry loop behind both the frame-1 Anchor Acceptance Gate and the
    post-render autonomous recovery pass over leftover 'vlm_qa_failed' frames."""

    def test_retries_and_corrects_prompt_until_pass(self):
        judge_calls = []

        def judge(image_path, prompt):
            judge_calls.append(prompt)
            if len(judge_calls) < 2:
                return False, 'workers visible in frame'
            return True, 'PASS'

        images = {1: {'body': 'original prompt', 'meta': ''}}
        with patch('pipeline_orchestrator.generate_frame_sequence') as mock_render, \
             patch('pipeline_orchestrator.fix_image_prompt_with_vlm_feedback', return_value='corrected prompt') as mock_fix:
            passed, reason = _retry_frame_until_pass({}, 'test project', 1, images, {}, judge)

        self.assertTrue(passed)
        self.assertEqual(reason, 'PASS')
        self.assertEqual(mock_render.call_count, 2)
        mock_fix.assert_called_once_with({}, 'original prompt', 'workers visible in frame')
        # 改写产物随后过确定性修复链（镜头族感知）：这里是 exterior 帧且无相机行，
        # fix_horizon_line 会补上地平线锁 —— 存回的是修复后的版本，不再是裸改写文本。
        self.assertTrue(images[1]['body'].startswith('corrected prompt'))
        self.assertIn('horizon line', images[1]['body'].lower())

    def test_gives_up_after_max_attempts(self):
        def judge(image_path, prompt):
            return False, 'still bad'

        images = {1: {'body': 'p', 'meta': ''}}
        with patch('pipeline_orchestrator.generate_frame_sequence') as mock_render, \
             patch('pipeline_orchestrator.fix_image_prompt_with_vlm_feedback', side_effect=lambda c, p, r: p + '!'):
            passed, reason = _retry_frame_until_pass({}, 'test project', 1, images, {}, judge, max_attempts=3)

        self.assertFalse(passed)
        self.assertEqual(reason, 'still bad')
        self.assertEqual(mock_render.call_count, 3)


class TestRunAutonomousPipeline(unittest.TestCase):
    """run_autonomous_pipeline: compose IMAGE 1 -> render+gate it -> refine packet ->
    compose remaining beats -> render remaining frames -> videos, with no manual-review
    dead end anywhere in between."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old_output_root = server_common.OUTPUT_ROOT
        server_common.OUTPUT_ROOT = self.tmp
        self.title = 'test_autonomous_project'
        self.project_dir = server_common._get_project_dir(self.title)
        os.makedirs(os.path.join(self.project_dir, 'frames'), exist_ok=True)

    def tearDown(self):
        server_common.OUTPUT_ROOT = self.old_output_root
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_manifest(self, frames):
        with open(os.path.join(self.project_dir, 'manifest.json'), 'w', encoding='utf-8') as f:
            json.dump({'frames': frames}, f)

    def _read_manifest(self):
        with open(os.path.join(self.project_dir, 'manifest.json'), 'r', encoding='utf-8') as f:
            return json.load(f)

    def _fake_state(self):
        return {
            'title': self.title,
            'image_1_prompt': 'first frame prompt',
            'packet': {'camera_dna': 'x'},
            'parsed_brief': {'carrier': 'x'},
            'compiled_images': {1: 'first frame prompt'},
        }

    def test_happy_path_marks_frame_1_auto_approved_and_runs_all_stages(self):
        state = self._fake_state()

        def fake_render(config, title, prompt_block, on_progress=None, target_sequences=None):
            # Models generate_frame_sequence's real resume behavior: an already-
            # rendered frame (with a manifest entry already on disk) is skipped and
            # its recorded quality_gate is preserved, not clobbered by a full re-run.
            manifest_path = os.path.join(self.project_dir, 'manifest.json')
            frames = []
            if os.path.exists(manifest_path):
                with open(manifest_path, 'r', encoding='utf-8') as f:
                    frames = json.load(f).get('frames', [])
            existing_seqs = {f['sequence'] for f in frames}
            wanted = target_sequences if target_sequences is not None else [1]
            for seq in wanted:
                if seq not in existing_seqs:
                    frames.append({'sequence': seq, 'quality_gate': 'pending_manual_review'})
            self._write_manifest(frames)
            return {'title': title}

        with patch('pipeline_orchestrator.compose_anchor_and_packet', return_value=state), \
             patch('pipeline_orchestrator.generate_frame_sequence', side_effect=fake_render), \
             patch('pipeline_orchestrator.check_anchor_frame_compliance', return_value=(True, 'PASS')), \
             patch('pipeline_orchestrator.refine_packet_from_accepted_anchor', return_value={'camera_dna': 'refined'}) as mock_refine, \
             patch('pipeline_orchestrator.compose_remaining_beats', return_value='FULL PROMPT BLOCK') as mock_phase2, \
             patch('pipeline_orchestrator.generate_video_sequence', return_value={'videos': [{'slot': 1, 'status': 'success'}]}) as mock_video:
            result = run_autonomous_pipeline({}, {'theme': 'x'})

        self.assertEqual(result['status'], 'completed')
        mock_refine.assert_called_once()
        mock_phase2.assert_called_once()
        mock_video.assert_called_once()
        self.assertEqual(self._read_manifest()['frames'][0]['quality_gate'], 'auto_approved')

    def test_frame_1_is_never_gated_and_uses_no_gate_judge(self):
        """The IMAGE-1 Anchor Acceptance Gate has been removed from the GUI/API path:
        run_autonomous_pipeline renders frame 1 unconditionally via _no_gate_judge and
        never calls check_anchor_frame_compliance, so a 'needs_human_review' dead end
        for frame 1 is no longer reachable from this entry point."""
        state = self._fake_state()

        def fake_render(config, title, prompt_block, on_progress=None, target_sequences=None):
            # Preserve whatever's already on disk (e.g. the gate step's 'auto_approved'
            # write for frame 1) instead of clobbering it on the later full-sequence call.
            manifest_path = os.path.join(self.project_dir, 'manifest.json')
            frames = []
            if os.path.exists(manifest_path):
                with open(manifest_path, 'r', encoding='utf-8') as f:
                    frames = json.load(f).get('frames', [])
            existing_seqs = {f['sequence'] for f in frames}
            wanted = target_sequences if target_sequences is not None else [1]
            for seq in wanted:
                if seq not in existing_seqs:
                    frames.append({'sequence': seq, 'quality_gate': 'pending_manual_review'})
            self._write_manifest(frames)
            return {'title': title}

        with patch('pipeline_orchestrator.compose_anchor_and_packet', return_value=state), \
             patch('pipeline_orchestrator.generate_frame_sequence', side_effect=fake_render), \
             patch('pipeline_orchestrator.check_anchor_frame_compliance') as mock_gate, \
             patch('pipeline_orchestrator.refine_packet_from_accepted_anchor', return_value={'camera_dna': 'refined'}) as mock_refine, \
             patch('pipeline_orchestrator.compose_remaining_beats', return_value='FULL PROMPT BLOCK') as mock_phase2, \
             patch('pipeline_orchestrator.generate_video_sequence', return_value={'videos': [{'slot': 1, 'status': 'success'}]}) as mock_video:
            result = run_autonomous_pipeline({}, {'theme': 'x'})

        self.assertEqual(result['status'], 'completed')
        mock_gate.assert_not_called()
        mock_refine.assert_called_once()
        mock_phase2.assert_called_once()
        mock_video.assert_called_once()
        self.assertEqual(self._read_manifest()['frames'][0]['quality_gate'], 'auto_approved')


class TestRunStagedFrameRendering(unittest.TestCase):
    """run_staged_frame_rendering: the render-only staged path for an ALREADY-composed
    prompt_block (e.g. written directly by an agent following this skill's own Steps
    1-11 in conversation), used by /api/render_staged and scripts/generate_frames.py.
    Unlike run_autonomous_pipeline, it must never re-derive prompt text itself."""

    PROMPT_BLOCK = (
        "图片提示词\n图片 1:\nfirst frame prompt\n\n图片 2:\nsecond frame prompt\n\n"
        "视频提示词\n视频 1:\nvideo one\n"
    )

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old_output_root = server_common.OUTPUT_ROOT
        server_common.OUTPUT_ROOT = self.tmp
        self.title = 'test_staged_render_project'
        self.project_dir = server_common._get_project_dir(self.title)
        os.makedirs(os.path.join(self.project_dir, 'frames'), exist_ok=True)

    def tearDown(self):
        server_common.OUTPUT_ROOT = self.old_output_root
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_manifest(self, frames):
        with open(os.path.join(self.project_dir, 'manifest.json'), 'w', encoding='utf-8') as f:
            json.dump({'frames': frames}, f)

    def _read_manifest(self):
        with open(os.path.join(self.project_dir, 'manifest.json'), 'r', encoding='utf-8') as f:
            return json.load(f)

    def _write_frame_file(self, sequence):
        path = os.path.join(self.project_dir, 'frames', f'img_{sequence:03d}.webp')
        with open(path, 'wb') as f:
            f.write(b'fake webp bytes')

    def test_happy_path_renders_frame_1_without_recomposing_text(self):
        """No Anchor Acceptance Gate on this path any more: frame 1 renders via
        _no_gate_judge (check_anchor_frame_compliance is never called) and is recorded
        auto_approved unconditionally."""
        def fake_render(config, title, prompt_block, on_progress=None, target_sequences=None):
            manifest_path = os.path.join(self.project_dir, 'manifest.json')
            frames = []
            if os.path.exists(manifest_path):
                with open(manifest_path, 'r', encoding='utf-8') as f:
                    frames = json.load(f).get('frames', [])
            existing_seqs = {f['sequence'] for f in frames}
            wanted = target_sequences if target_sequences is not None else [1, 2]
            for seq in wanted:
                if seq not in existing_seqs:
                    frames.append({'sequence': seq, 'quality_gate': 'pending_manual_review'})
            self._write_manifest(frames)
            return {'title': title}

        with patch('pipeline_orchestrator.generate_frame_sequence', side_effect=fake_render), \
             patch('pipeline_orchestrator.check_anchor_frame_compliance') as mock_gate, \
             patch('pipeline_orchestrator.compose_anchor_and_packet') as mock_phase1, \
             patch('pipeline_orchestrator.compose_remaining_beats') as mock_phase2, \
             patch('pipeline_orchestrator.generate_video_sequence', return_value={'videos': [{'slot': 1, 'status': 'success'}]}):
            result = run_staged_frame_rendering({}, self.title, self.PROMPT_BLOCK)

        self.assertEqual(result['status'], 'completed')
        mock_gate.assert_not_called()
        mock_phase1.assert_not_called()  # no text (re)composition in this path
        mock_phase2.assert_not_called()
        self.assertEqual(self._read_manifest()['frames'][0]['quality_gate'], 'auto_approved')

    def test_raises_when_prompt_block_has_no_image_1(self):
        with self.assertRaises(RuntimeError):
            run_staged_frame_rendering({}, self.title, "图片提示词\n图片 2:\nsomething\n")

    def test_skips_regating_frame_1_if_already_auto_approved(self):
        """If an agent already gated IMAGE 1 inline via render_and_gate_single_frame
        (e.g. through /api/render_anchor) before handing off the full prompt_block here,
        this must not re-render or re-judge frame 1 a second time. Reuse additionally
        requires the anchor prompt fingerprint recorded at gate time to match AND the
        gated anchor image to still exist on disk."""
        self._write_manifest([{
            'sequence': 1, 'quality_gate': 'auto_approved', 'vlm_qa_reason': 'PASS',
            'anchor_prompt_sha256': _prompt_fingerprint('first frame prompt'),
        }])
        self._write_frame_file(1)

        def fake_render(config, title, prompt_block, on_progress=None, target_sequences=None):
            return {'title': title}

        with patch('pipeline_orchestrator.generate_frame_sequence', side_effect=fake_render), \
             patch('pipeline_orchestrator.check_anchor_frame_compliance') as mock_gate, \
             patch('pipeline_orchestrator.fix_image_prompt_with_vlm_feedback') as mock_fix, \
             patch('pipeline_orchestrator.generate_video_sequence', return_value={'videos': [{'slot': 1, 'status': 'success'}]}):
            result = run_staged_frame_rendering({}, self.title, self.PROMPT_BLOCK)

        self.assertEqual(result['status'], 'completed')
        mock_gate.assert_not_called()
        mock_fix.assert_not_called()
        self.assertEqual(self._read_manifest()['frames'][0]['quality_gate'], 'auto_approved')

    def test_regates_frame_1_when_stale_manifest_has_no_fingerprint(self):
        """A stale manifest from an earlier same-titled run says 'auto_approved' but has
        no anchor prompt fingerprint (or a mismatching one) — reusing it would skip
        re-rendering a prompt that was never actually rendered under this run. Must
        re-render (there is no gate to re-judge any more, just a re-render + record)."""
        self._write_manifest([{'sequence': 1, 'quality_gate': 'auto_approved', 'vlm_qa_reason': 'PASS'}])

        def fake_render(config, title, prompt_block, on_progress=None, target_sequences=None):
            return {'title': title}

        with patch('pipeline_orchestrator.generate_frame_sequence', side_effect=fake_render) as mock_render, \
             patch('pipeline_orchestrator.check_anchor_frame_compliance') as mock_gate, \
             patch('pipeline_orchestrator.generate_video_sequence', return_value={'videos': [{'slot': 1, 'status': 'success'}]}):
            result = run_staged_frame_rendering({}, self.title, self.PROMPT_BLOCK)

        self.assertEqual(result['status'], 'completed')
        mock_gate.assert_not_called()
        self.assertIn([1], [c.kwargs.get('target_sequences') for c in mock_render.call_args_list])
        frame_1 = self._read_manifest()['frames'][0]
        self.assertEqual(frame_1['quality_gate'], 'auto_approved')
        self.assertEqual(frame_1['anchor_prompt_sha256'], _prompt_fingerprint('first frame prompt'))

    def test_regates_frame_1_when_gated_image_missing_from_disk(self):
        """manifest 记录与指纹都对得上，但过门的锚点图已被清理：复用会让下游重渲一张
        从未渲染过的新首帧接着整链构图，必须重新渲染。"""
        self._write_manifest([{
            'sequence': 1, 'quality_gate': 'auto_approved', 'vlm_qa_reason': 'PASS',
            'anchor_prompt_sha256': _prompt_fingerprint('first frame prompt'),
        }])
        # 不写 frames/img_001.webp —— 图不在盘上

        def fake_render(config, title, prompt_block, on_progress=None, target_sequences=None):
            return {'title': title}

        with patch('pipeline_orchestrator.generate_frame_sequence', side_effect=fake_render) as mock_render, \
             patch('pipeline_orchestrator.check_anchor_frame_compliance') as mock_gate, \
             patch('pipeline_orchestrator.generate_video_sequence', return_value={'videos': [{'slot': 1, 'status': 'success'}]}):
            result = run_staged_frame_rendering({}, self.title, self.PROMPT_BLOCK)

        self.assertEqual(result['status'], 'completed')
        mock_gate.assert_not_called()
        self.assertIn([1], [c.kwargs.get('target_sequences') for c in mock_render.call_args_list])

    def test_regates_frame_1_when_prompt_changed_since_gate(self):
        """auto_approved with a fingerprint from a DIFFERENT prompt (the agent edited
        IMAGE 1 after gating it) must not be reused."""
        self._write_manifest([{
            'sequence': 1, 'quality_gate': 'auto_approved', 'vlm_qa_reason': 'PASS',
            'anchor_prompt_sha256': _prompt_fingerprint('some other prompt entirely'),
        }])
        self._write_frame_file(1)

        def fake_render(config, title, prompt_block, on_progress=None, target_sequences=None):
            return {'title': title}

        with patch('pipeline_orchestrator.generate_frame_sequence', side_effect=fake_render) as mock_render, \
             patch('pipeline_orchestrator.check_anchor_frame_compliance') as mock_gate, \
             patch('pipeline_orchestrator.generate_video_sequence', return_value={'videos': [{'slot': 1, 'status': 'success'}]}):
            result = run_staged_frame_rendering({}, self.title, self.PROMPT_BLOCK)

        self.assertEqual(result['status'], 'completed')
        mock_gate.assert_not_called()
        self.assertIn([1], [c.kwargs.get('target_sequences') for c in mock_render.call_args_list])


class TestRenderAndGateSingleFrame(unittest.TestCase):
    """render_and_gate_single_frame: the shared render+gate primitive behind
    /api/render_anchor (a synchronous, blocking endpoint a conversational agent calls
    mid-turn to get a real verdict on IMAGE 1 before composing anything else) and behind
    run_autonomous_pipeline/run_staged_frame_rendering's own frame-1 handling."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old_output_root = server_common.OUTPUT_ROOT
        server_common.OUTPUT_ROOT = self.tmp
        self.title = 'test_single_frame_gate_project'
        self.project_dir = server_common._get_project_dir(self.title)
        os.makedirs(os.path.join(self.project_dir, 'frames'), exist_ok=True)

    def tearDown(self):
        server_common.OUTPUT_ROOT = self.old_output_root
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_manifest(self, frames):
        with open(os.path.join(self.project_dir, 'manifest.json'), 'w', encoding='utf-8') as f:
            json.dump({'frames': frames}, f)

    def _read_manifest(self):
        with open(os.path.join(self.project_dir, 'manifest.json'), 'r', encoding='utf-8') as f:
            return json.load(f)

    def test_default_judge_uses_bare_anchor_gate_and_writes_manifest(self):
        def fake_render(config, title, prompt_block, on_progress=None, target_sequences=None):
            self._write_manifest([{'sequence': 1, 'quality_gate': 'pending_manual_review'}])
            return {'title': title}

        with patch('pipeline_orchestrator.generate_frame_sequence', side_effect=fake_render), \
             patch('pipeline_orchestrator.check_anchor_frame_compliance', return_value=(True, 'PASS')) as mock_gate:
            result = render_and_gate_single_frame({}, self.title, 1, 'a static shot of the ruin')

        self.assertEqual(result['status'], 'auto_approved')
        self.assertEqual(result['reason'], 'PASS')
        self.assertEqual(result['prompt'], 'a static shot of the ruin')
        mock_gate.assert_called_once()
        # bare-prompt path: packet/parsed_brief default to {} since no separate packet exists
        self.assertEqual(mock_gate.call_args.args[3], {})
        self.assertEqual(mock_gate.call_args.args[4], {})
        self.assertEqual(self._read_manifest()['frames'][0]['quality_gate'], 'auto_approved')

    def test_custom_judge_is_used_when_provided(self):
        custom_calls = []

        def custom_judge(image_path, prompt):
            custom_calls.append(prompt)
            return True, 'custom PASS'

        with patch('pipeline_orchestrator.generate_frame_sequence'), \
             patch('pipeline_orchestrator.check_anchor_frame_compliance') as mock_gate:
            result = render_and_gate_single_frame({}, self.title, 1, 'a prompt', judge=custom_judge)

        self.assertEqual(result['status'], 'auto_approved')
        self.assertEqual(result['reason'], 'custom PASS')
        self.assertEqual(custom_calls, ['a prompt'])
        mock_gate.assert_not_called()

    def test_needs_human_review_after_exhausting_retries(self):
        def fake_render(config, title, prompt_block, on_progress=None, target_sequences=None):
            self._write_manifest([{'sequence': 1, 'quality_gate': 'pending_manual_review'}])
            return {'title': title}

        with patch('pipeline_orchestrator.generate_frame_sequence', side_effect=fake_render), \
             patch('pipeline_orchestrator.check_anchor_frame_compliance', return_value=(False, 'still generic')), \
             patch('pipeline_orchestrator.fix_image_prompt_with_vlm_feedback', side_effect=lambda c, p, r: p + '!'):
            result = render_and_gate_single_frame({}, self.title, 1, 'a prompt')

        self.assertEqual(result['status'], 'needs_human_review')
        self.assertEqual(result['reason'], 'still generic')
        self.assertEqual(self._read_manifest()['frames'][0]['quality_gate'], 'needs_human_review')

    def test_skipped_verdict_returns_degraded_status_and_records_fingerprint(self):
        """判定服务异常时 judge 返回 Skipped 放行：状态必须是 auto_approved_degraded
        （帧未经真实核验），且 manifest 记录验锚提示词指纹供后续复用比对。"""
        def fake_render(config, title, prompt_block, on_progress=None, target_sequences=None):
            self._write_manifest([{'sequence': 1, 'quality_gate': 'pending_manual_review'}])
            return {'title': title}

        with patch('pipeline_orchestrator.generate_frame_sequence', side_effect=fake_render), \
             patch('pipeline_orchestrator.check_anchor_frame_compliance',
                   return_value=(True, 'Skipped (API Error: boom)')):
            result = render_and_gate_single_frame({}, self.title, 1, 'a prompt')

        self.assertEqual(result['status'], 'auto_approved_degraded')
        frame_1 = self._read_manifest()['frames'][0]
        self.assertEqual(frame_1['quality_gate'], 'auto_approved_degraded')
        self.assertEqual(frame_1['anchor_prompt_sha256'], _prompt_fingerprint('a prompt'))


if __name__ == '__main__':
    unittest.main()
