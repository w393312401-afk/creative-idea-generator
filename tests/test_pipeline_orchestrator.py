"""编排层的四个渲染入口，在「渲染期不做任何审查」这一契约下的行为。

2026-08-05：锚帧验收门、族锚硬闸、检查点现实同步、链尾漂移回望整体移除。本文件此前
大半是这些门的用例（判定通过/失败/重抽/指纹复用），随之删除。留下的用例只问一件事：
每条入口是否把该渲的帧渲了、该跑的阶段按序跑了，且**一次判定调用都不发**。
"""
import os
import json
import shutil
import tempfile
import unittest
from unittest.mock import patch

import server_common
import prompt_pipeline
from pipeline_orchestrator import (
    render_single_frame,
    render_frames_for_task,
    run_autonomous_pipeline,
    run_staged_frame_rendering,
)


class _ProjectTestCase(unittest.TestCase):
    """共用的临时项目目录 + 一个仿真的 generate_frame_sequence。

    仿真版复刻真实实现的断点续传语义：盘上已有的帧不重渲，manifest 里已记的
    quality_gate 原样保留。"""

    title = 'test_project'

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old_output_root = server_common.OUTPUT_ROOT
        server_common.OUTPUT_ROOT = self.tmp
        self.project_dir = server_common._get_project_dir(self.title)
        os.makedirs(os.path.join(self.project_dir, 'frames'), exist_ok=True)
        self.render_calls = []

    def tearDown(self):
        server_common.OUTPUT_ROOT = self.old_output_root
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _manifest_path(self):
        return os.path.join(self.project_dir, 'manifest.json')

    def _write_manifest(self, frames):
        with open(self._manifest_path(), 'w', encoding='utf-8') as f:
            json.dump({'frames': frames}, f)

    def _read_manifest(self):
        with open(self._manifest_path(), 'r', encoding='utf-8') as f:
            return json.load(f)

    def _write_frame_file(self, sequence):
        path = os.path.join(self.project_dir, 'frames', f'img_{sequence:03d}.webp')
        with open(path, 'wb') as f:
            f.write(b'fake webp bytes')

    def _fake_render(self, default_seqs):
        def _render(config, title, prompt_block, on_progress=None, target_sequences=None):
            self.render_calls.append({'block': prompt_block, 'targets': target_sequences})
            frames = []
            if os.path.exists(self._manifest_path()):
                frames = self._read_manifest().get('frames', [])
            existing = {f['sequence'] for f in frames}
            for seq in (target_sequences if target_sequences is not None else default_seqs):
                if seq not in existing:
                    frames.append({'sequence': seq, 'quality_gate': 'pending_manual_review'})
                    self._write_frame_file(seq)
            self._write_manifest(frames)
            return {'title': title}
        return _render

    def assertNoJudgeCalled(self, mock_judge):
        """渲染路径上一次多模态判定调用都不该发生。"""
        mock_judge.assert_not_called()


class TestRenderSingleFrame(_ProjectTestCase):
    """render_single_frame：/api/render_anchor 背后的同步单帧渲染。只渲，不判。"""

    title = 'test_single_frame_project'

    def test_renders_the_frame_and_reports_where_it_landed(self):
        with patch('pipeline_orchestrator.generate_frame_sequence',
                   side_effect=self._fake_render([1])), \
             patch.object(prompt_pipeline, '_multimodal_chat') as mock_judge:
            result = render_single_frame({}, self.title, 1, 'a static shot of the ruin')

        self.assertEqual(result['status'], 'rendered')
        self.assertEqual(result['prompt'], 'a static shot of the ruin')
        self.assertTrue(result['image_path'].endswith('img_001.webp'))
        self.assertEqual(result['project_dir'], self.project_dir)
        self.assertNoJudgeCalled(mock_judge)

    def test_renders_only_the_requested_sequence(self):
        with patch('pipeline_orchestrator.generate_frame_sequence',
                   side_effect=self._fake_render([3])), \
             patch.object(prompt_pipeline, '_multimodal_chat'):
            render_single_frame({}, self.title, 3, 'a prompt')

        self.assertEqual(self.render_calls[0]['targets'], [3])

    def test_never_rewrites_the_prompt(self):
        """旧实现会在判定失败后按 VLM 反馈改写提示词再渲，返回的是改写后的文本。
        判定没了，返回的必须逐字是调用方交进来的那段——否则交付的提示词与盘上
        那张图对不上。"""
        prompt = 'exactly this text, unchanged'
        with patch('pipeline_orchestrator.generate_frame_sequence',
                   side_effect=self._fake_render([1])), \
             patch('pipeline_orchestrator.fix_image_prompt_with_vlm_feedback') as mock_fix:
            result = render_single_frame({}, self.title, 1, prompt)

        self.assertEqual(result['prompt'], prompt)
        mock_fix.assert_not_called()


class TestRenderFramesForTask(_ProjectTestCase):
    """render_frames_for_task：/api/generate_frames 整单渲染。渲完即止，不做视频。"""

    title = 'test_frames_task_project'
    PROMPT_BLOCK = (
        "图片提示词\n图片 1:\nfirst frame prompt\n\n图片 2:\nsecond frame prompt\n\n"
        "视频提示词\n视频 1:\nvideo one\n"
    )

    def test_renders_the_whole_sequence_in_one_resumable_call(self):
        with patch('pipeline_orchestrator.generate_frame_sequence',
                   side_effect=self._fake_render([1, 2])), \
             patch.object(prompt_pipeline, '_multimodal_chat') as mock_judge:
            manifest = render_frames_for_task({}, self.title, self.PROMPT_BLOCK)

        # target_sequences=None 才带断点续传语义（已有帧跳过）；拆成分段调用会丢掉它
        self.assertEqual([c['targets'] for c in self.render_calls], [None])
        self.assertEqual([f['sequence'] for f in manifest['frames']], [1, 2])
        self.assertNoJudgeCalled(mock_judge)

    def test_does_not_generate_video(self):
        with patch('pipeline_orchestrator.generate_frame_sequence',
                   side_effect=self._fake_render([1, 2])), \
             patch('pipeline_orchestrator.generate_video_sequence') as mock_video:
            render_frames_for_task({}, self.title, self.PROMPT_BLOCK)
        mock_video.assert_not_called()

    def test_returns_manifest_with_transient_path_keys(self):
        with patch('pipeline_orchestrator.generate_frame_sequence',
                   side_effect=self._fake_render([1, 2])):
            manifest = render_frames_for_task({}, self.title, self.PROMPT_BLOCK)
        self.assertTrue(manifest['manifest'].endswith('manifest.json'))
        self.assertEqual(manifest['project_dir'], os.path.abspath(self.project_dir))

    def test_frames_already_on_disk_keep_their_recorded_verdict(self):
        """手动一致性审查的结论存在 manifest 上。重跑整单不得把它洗回 pending。"""
        self._write_frame_file(1)
        self._write_manifest([{'sequence': 1, 'quality_gate': 'sequence_reviewed_pass'}])

        with patch('pipeline_orchestrator.generate_frame_sequence',
                   side_effect=self._fake_render([1, 2])):
            manifest = render_frames_for_task({}, self.title, self.PROMPT_BLOCK)

        by_seq = {f['sequence']: f for f in manifest['frames']}
        self.assertEqual(by_seq[1]['quality_gate'], 'sequence_reviewed_pass')
        self.assertEqual(by_seq[2]['quality_gate'], 'pending_manual_review')


class TestRunAutonomousPipeline(_ProjectTestCase):
    """run_autonomous_pipeline：合成首帧 → 渲首帧 → 依实拍修正数据包 → 合成剩余拍
    → 渲剩余帧 → 出视频。全程无判定、无死路。"""

    title = 'test_autonomous_project'

    def _fake_state(self):
        return {
            'title': self.title,
            'image_1_prompt': 'first frame prompt',
            'packet': {'camera_dna': 'x'},
            'parsed_brief': {'carrier': 'x'},
            'compiled_images': {1: 'first frame prompt'},
        }

    def test_happy_path_runs_every_stage_without_judging_anything(self):
        with patch('pipeline_orchestrator.compose_anchor_and_packet', return_value=self._fake_state()), \
             patch('pipeline_orchestrator.generate_frame_sequence',
                   side_effect=self._fake_render([1])), \
             patch('pipeline_orchestrator.refine_packet_from_accepted_anchor',
                   return_value={'camera_dna': 'refined'}) as mock_refine, \
             patch('pipeline_orchestrator.compose_remaining_beats',
                   return_value='FULL PROMPT BLOCK') as mock_phase2, \
             patch('pipeline_orchestrator.generate_video_sequence',
                   return_value={'videos': [{'slot': 1, 'status': 'success'}]}) as mock_video, \
             patch.object(prompt_pipeline, '_multimodal_chat') as mock_judge:
            result = run_autonomous_pipeline({}, {'theme': 'x'})

        self.assertEqual(result['status'], 'completed')
        mock_refine.assert_called_once()
        mock_phase2.assert_called_once()
        mock_video.assert_called_once()
        self.assertNoJudgeCalled(mock_judge)
        self.assertEqual(self._read_manifest()['frames'][0]['quality_gate'],
                         'pending_manual_review')

    def test_packet_is_refined_against_the_real_rendered_anchor(self):
        """数据包修正的输入必须是刚渲出的那张图的路径——剩余各拍的提示词都由它生成。"""
        with patch('pipeline_orchestrator.compose_anchor_and_packet', return_value=self._fake_state()), \
             patch('pipeline_orchestrator.generate_frame_sequence',
                   side_effect=self._fake_render([1])), \
             patch('pipeline_orchestrator.refine_packet_from_accepted_anchor',
                   return_value={'camera_dna': 'refined'}) as mock_refine, \
             patch('pipeline_orchestrator.compose_remaining_beats', return_value='FULL PROMPT BLOCK'), \
             patch('pipeline_orchestrator.generate_video_sequence', return_value={'videos': []}):
            run_autonomous_pipeline({}, {'theme': 'x'})

        anchor_path = mock_refine.call_args.args[1]
        self.assertTrue(anchor_path.endswith('img_001.webp'))
        self.assertTrue(os.path.exists(anchor_path))

    def test_anchor_renders_before_the_remaining_beats_are_composed(self):
        """顺序是契约的一部分：剩余拍要依据修正后的包写，包又要依据实拍修正。"""
        order = []
        real_render = self._fake_render([1])

        def _render(*args, **kwargs):
            order.append(('render', kwargs.get('target_sequences')))
            return real_render(*args, **kwargs)

        def _refine(*args, **kwargs):
            order.append(('refine', None))
            return {'camera_dna': 'r'}

        def _compose_rest(*args, **kwargs):
            order.append(('compose_rest', None))
            return 'BLOCK'

        with patch('pipeline_orchestrator.compose_anchor_and_packet', return_value=self._fake_state()), \
             patch('pipeline_orchestrator.generate_frame_sequence', side_effect=_render), \
             patch('pipeline_orchestrator.refine_packet_from_accepted_anchor', side_effect=_refine), \
             patch('pipeline_orchestrator.compose_remaining_beats', side_effect=_compose_rest), \
             patch('pipeline_orchestrator.generate_video_sequence', return_value={'videos': []}):
            run_autonomous_pipeline({}, {'theme': 'x'})

        self.assertEqual([stage for stage, _ in order],
                         ['render', 'refine', 'compose_rest', 'render'])
        self.assertEqual(order[0][1], [1])     # 首帧单独渲
        self.assertIsNone(order[3][1])         # 剩余帧走整单续传


class TestRunStagedFrameRendering(_ProjectTestCase):
    """run_staged_frame_rendering：/api/render_staged 的渲染入口。收到的是**已经写好**
    的 prompt_block，绝不自己再合成一遍文本。"""

    title = 'test_staged_render_project'
    PROMPT_BLOCK = (
        "图片提示词\n图片 1:\nfirst frame prompt\n\n图片 2:\nsecond frame prompt\n\n"
        "视频提示词\n视频 1:\nvideo one\n"
    )

    def test_happy_path_renders_and_makes_video_without_recomposing_text(self):
        with patch('pipeline_orchestrator.generate_frame_sequence',
                   side_effect=self._fake_render([1, 2])), \
             patch('pipeline_orchestrator.compose_anchor_and_packet') as mock_phase1, \
             patch('pipeline_orchestrator.compose_remaining_beats') as mock_phase2, \
             patch('pipeline_orchestrator.generate_video_sequence',
                   return_value={'videos': [{'slot': 1, 'status': 'success'}]}) as mock_video, \
             patch.object(prompt_pipeline, '_multimodal_chat') as mock_judge:
            result = run_staged_frame_rendering({}, self.title, self.PROMPT_BLOCK)

        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['prompt_block'], self.PROMPT_BLOCK)
        mock_phase1.assert_not_called()
        mock_phase2.assert_not_called()
        mock_video.assert_called_once()
        self.assertNoJudgeCalled(mock_judge)

    def test_frame_1_rendered_inline_by_the_agent_is_not_paid_for_twice(self):
        """agent 常在合成剩余提示词之前经 /api/render_anchor 先渲一张首帧。这里必须
        走 generate_frame_sequence 的断点续传（target_sequences=None）把它复用掉。"""
        self._write_frame_file(1)
        self._write_manifest([{'sequence': 1, 'quality_gate': 'pending_manual_review'}])

        with patch('pipeline_orchestrator.generate_frame_sequence',
                   side_effect=self._fake_render([1, 2])), \
             patch('pipeline_orchestrator.generate_video_sequence', return_value={'videos': []}):
            run_staged_frame_rendering({}, self.title, self.PROMPT_BLOCK)

        self.assertEqual([c['targets'] for c in self.render_calls], [None])

    def test_raises_when_prompt_block_has_no_image_1(self):
        with self.assertRaises(RuntimeError):
            run_staged_frame_rendering({}, self.title, "图片提示词\n图片 2:\nsomething\n")


if __name__ == '__main__':
    unittest.main()
