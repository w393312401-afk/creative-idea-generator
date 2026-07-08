import os
import json
import shutil
import tempfile
import unittest
from unittest.mock import patch

import server_common
from pipeline_orchestrator import (
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
        self.assertEqual(images[1]['body'], 'corrected prompt')

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

    def test_needs_human_review_when_anchor_gate_never_passes(self):
        state = self._fake_state()

        def fake_render(config, title, prompt_block, on_progress=None, target_sequences=None):
            self._write_manifest([{'sequence': 1, 'quality_gate': 'pending_manual_review'}])
            return {'title': title}

        with patch('pipeline_orchestrator.compose_anchor_and_packet', return_value=state), \
             patch('pipeline_orchestrator.generate_frame_sequence', side_effect=fake_render) as mock_render, \
             patch('pipeline_orchestrator.check_anchor_frame_compliance', return_value=(False, 'still shows a generic room')), \
             patch('pipeline_orchestrator.fix_image_prompt_with_vlm_feedback', side_effect=lambda c, p, r: p + '!'), \
             patch('pipeline_orchestrator.refine_packet_from_accepted_anchor') as mock_refine, \
             patch('pipeline_orchestrator.compose_remaining_beats') as mock_phase2, \
             patch('pipeline_orchestrator.generate_video_sequence') as mock_video:
            result = run_autonomous_pipeline({}, {'theme': 'x'})

        self.assertEqual(result['status'], 'needs_human_review')
        self.assertEqual(mock_render.call_count, 3)  # _MAX_ANCHOR_ATTEMPTS
        mock_refine.assert_not_called()
        mock_phase2.assert_not_called()
        mock_video.assert_not_called()
        self.assertEqual(self._read_manifest()['frames'][0]['quality_gate'], 'needs_human_review')

    def test_recovery_pass_retries_failed_frame_and_marks_approved(self):
        state = self._fake_state()
        render_calls = []

        def fake_render(config, title, prompt_block, on_progress=None, target_sequences=None):
            render_calls.append(target_sequences)
            if target_sequences == [1]:
                self._write_manifest([{'sequence': 1, 'quality_gate': 'pending_manual_review'}])
            elif target_sequences is None:
                # Main render pass: frame 2 comes out stuck at vlm_qa_failed.
                self._write_manifest([
                    {'sequence': 1, 'quality_gate': 'auto_approved'},
                    {'sequence': 2, 'quality_gate': 'vlm_qa_failed'},
                ])
            elif target_sequences == [2]:
                pass  # recovery re-render; manifest gate gets overwritten explicitly below
            return {'title': title}

        prompt_block_with_slots = (
            "图片提示词\n图片 1:\nfirst frame\n\n图片 2:\nsecond frame\n\n"
            "视频提示词\n视频 1:\nvideo one\n"
        )

        with patch('pipeline_orchestrator.compose_anchor_and_packet', return_value=state), \
             patch('pipeline_orchestrator.generate_frame_sequence', side_effect=fake_render), \
             patch('pipeline_orchestrator.check_anchor_frame_compliance', return_value=(True, 'PASS')), \
             patch('pipeline_orchestrator.refine_packet_from_accepted_anchor', return_value={'camera_dna': 'refined'}), \
             patch('pipeline_orchestrator.compose_remaining_beats', return_value=prompt_block_with_slots), \
             patch('pipeline_orchestrator.run_frame_qa_check', return_value=(True, 'PASS')) as mock_vlm, \
             patch('pipeline_orchestrator.generate_video_sequence', return_value={'videos': [{'slot': 1, 'status': 'success'}]}):
            result = run_autonomous_pipeline({}, {'theme': 'x'})

        self.assertEqual(result['status'], 'completed')
        mock_vlm.assert_called_once()
        self.assertIn([2], render_calls)
        frames_by_seq = {f['sequence']: f for f in self._read_manifest()['frames']}
        self.assertEqual(frames_by_seq[2]['quality_gate'], 'auto_approved')


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

    def test_happy_path_gates_frame_1_without_recomposing_text(self):
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
             patch('pipeline_orchestrator.check_anchor_frame_compliance', return_value=(True, 'PASS')) as mock_gate, \
             patch('pipeline_orchestrator.compose_anchor_and_packet') as mock_phase1, \
             patch('pipeline_orchestrator.compose_remaining_beats') as mock_phase2, \
             patch('pipeline_orchestrator.generate_video_sequence', return_value={'videos': [{'slot': 1, 'status': 'success'}]}):
            result = run_staged_frame_rendering({}, self.title, self.PROMPT_BLOCK)

        self.assertEqual(result['status'], 'completed')
        mock_gate.assert_called_once()
        mock_phase1.assert_not_called()  # no text (re)composition in this path
        mock_phase2.assert_not_called()
        self.assertEqual(self._read_manifest()['frames'][0]['quality_gate'], 'auto_approved')

    def test_needs_human_review_when_anchor_never_passes(self):
        def fake_render(config, title, prompt_block, on_progress=None, target_sequences=None):
            self._write_manifest([{'sequence': 1, 'quality_gate': 'pending_manual_review'}])
            return {'title': title}

        with patch('pipeline_orchestrator.generate_frame_sequence', side_effect=fake_render) as mock_render, \
             patch('pipeline_orchestrator.check_anchor_frame_compliance', return_value=(False, 'tone mismatch')), \
             patch('pipeline_orchestrator.fix_image_prompt_with_vlm_feedback', side_effect=lambda c, p, r: p + '!'), \
             patch('pipeline_orchestrator.generate_video_sequence') as mock_video:
            result = run_staged_frame_rendering({}, self.title, self.PROMPT_BLOCK)

        self.assertEqual(result['status'], 'needs_human_review')
        self.assertEqual(mock_render.call_count, 3)
        mock_video.assert_not_called()
        self.assertEqual(self._read_manifest()['frames'][0]['quality_gate'], 'needs_human_review')

    def test_raises_when_prompt_block_has_no_image_1(self):
        with self.assertRaises(RuntimeError):
            run_staged_frame_rendering({}, self.title, "图片提示词\n图片 2:\nsomething\n")

    def test_skips_regating_frame_1_if_already_auto_approved(self):
        """If an agent already gated IMAGE 1 inline via render_and_gate_single_frame
        (e.g. through /api/render_anchor) before handing off the full prompt_block here,
        this must not re-render or re-judge frame 1 a second time."""
        self._write_manifest([{'sequence': 1, 'quality_gate': 'auto_approved', 'vlm_qa_reason': 'PASS'}])

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


if __name__ == '__main__':
    unittest.main()
