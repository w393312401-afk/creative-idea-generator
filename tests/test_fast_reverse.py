import json
import os
import tempfile
import unittest
from unittest.mock import patch, MagicMock

import server_common
import replica_pipeline as rp
import prompt_pipeline as pp
from prompt_pipeline.fast_reverse import (
    build_fast_reverse_system_prompt,
    collect_keyframe_images,
    fast_video_native_reverse
)


class TestFastReverse(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old_root = server_common.OUTPUT_ROOT
        server_common.OUTPUT_ROOT = self.tmp

    def tearDown(self):
        server_common.OUTPUT_ROOT = self.old_root

    def _setup_mock_job(self):
        job_dir = os.path.join(self.tmp, 'replica_jobs', 'replica_mock_rev')
        os.makedirs(os.path.join(job_dir, 'review_frames'), exist_ok=True)
        
        # Create fake frame files
        frame_paths = []
        for i in range(1, 6):
            p = os.path.join(job_dir, 'review_frames', f'frame_{i:03d}.jpg')
            with open(p, 'wb') as f:
                f.write(b'fake-jpg-data')
            frame_paths.append(p)

        collage = os.path.join(job_dir, 'clip_collage.jpg')
        with open(collage, 'wb') as f:
            f.write(b'fake-collage-data')

        overview = {
            'media_metadata': {'duration_sec': 25.0},
            'keyframe_collage': collage,
            'analysis_plan': {
                'required_frames': [os.path.basename(p) for p in frame_paths]
            },
            'review_sampling': {
                'frames': [{'frame_path': p, 'timestamp': i * 5.0} for i, p in enumerate(frame_paths)]
            },
            'change_events': [
                {'timestamp': 5.0, 'description': 'Clearing done'},
                {'timestamp': 15.0, 'description': 'Flooring installed'}
            ]
        }
        with open(os.path.join(job_dir, 'video_overview.json'), 'w', encoding='utf-8') as f:
            json.dump(overview, f)

        return job_dir, overview

    def test_build_fast_reverse_system_prompt(self):
        sys_p = build_fast_reverse_system_prompt()
        self.assertIn('Strict Monotonic Causal Order', sys_p)
        self.assertIn('timelapse_beats.json', sys_p)
        self.assertIn('package_operations', sys_p)
        self.assertIn('persistent_traces', sys_p)

    def test_collect_keyframe_images(self):
        job_dir, overview = self._setup_mock_job()
        images = collect_keyframe_images(job_dir, overview)
        self.assertGreaterEqual(len(images), 2)
        self.assertTrue(any('clip_collage.jpg' in p for p in images))

    def test_fast_video_native_reverse_end_to_end(self):
        job_dir, overview = self._setup_mock_job()
        mock_beats_response = json.dumps({
            'carrier': '地下避难所',
            'env': '荒芜林区地下',
            'trauma': '积水断梁废弃状态',
            'destiny_zh': '隐世静谧书房',
            'video_duration_sec': 25.0,
            'banned_elements': ['excavator'],
            'scene_constants': ['混凝土顶拱'],
            'beats': [
                {
                    'id': 'B01',
                    'index': 1,
                    'stage': 'clearing',
                    'operation': 'clearing',
                    'visible_action': 'Worker clears concrete rubble with iron shovel',
                    'visible_result': 'Bare concrete floor cleaned',
                    'package_operations': ['clearing_rubble', 'sweeping_dirt'],
                    'visible_details': ['concrete rubble', 'bare slab'],
                    'persistent_traces': ['sweep marks', 'cleaned floor'],
                    'space': 'main_space',
                    'camera_setup': '24mm_wide_chest_level',
                    'timestamp_start': 0.0,
                    'timestamp_end': 10.0,
                    'evidence_frames': ['frame_001.jpg', 'frame_002.jpg'],
                    'zh': {
                        'visible_action': '工人用铁铲清理混凝土瓦砾',
                        'visible_result': '地面清理干净',
                        'headline': '清理瓦砾'
                    }
                },
                {
                    'id': 'B02',
                    'index': 2,
                    'stage': 'flooring',
                    'operation': 'flooring',
                    'visible_action': 'Worker installs warm oak wood flooring',
                    'visible_result': 'Oak flooring fully laid',
                    'package_operations': ['installing_planks'],
                    'visible_details': ['oak timber planks'],
                    'persistent_traces': ['laid floor edge', 'sawdust'],
                    'space': 'main_space',
                    'camera_setup': '24mm_wide_chest_level',
                    'timestamp_start': 10.0,
                    'timestamp_end': 25.0,
                    'evidence_frames': ['frame_003.jpg', 'frame_005.jpg'],
                    'zh': {
                        'visible_action': '工人铺设温润橡木实木地板',
                        'visible_result': '地板铺设完成',
                        'headline': '铺设地板'
                    }
                }
            ]
        })

        with patch('prompt_pipeline._multimodal_chat', return_value=mock_beats_response):
            beats_doc = fast_video_native_reverse({}, job_dir)

        self.assertEqual(len(beats_doc['beats']), 2)
        self.assertEqual(beats_doc['beats'][0]['id'], 'B01')
        self.assertEqual(beats_doc['beats'][1]['id'], 'B02')
        self.assertTrue(os.path.exists(os.path.join(job_dir, 'timelapse_beats.json')))
        self.assertTrue(os.path.exists(os.path.join(job_dir, 'frame_facts.json')))

    def test_run_reverse_uses_fast_video_native_reverse(self):
        job_dir, overview = self._setup_mock_job()
        job_id = os.path.basename(job_dir)
        state = {
            'job_id': job_id,
            'video_name': 'test.mp4',
            'stage': 'confirm_cost',
            'video_path': os.path.join(job_dir, 'test.mp4')
        }
        rp._save_state(state)

        mock_beats_response = json.dumps({
            'carrier': '集装箱',
            'destiny_zh': '设计工坊',
            'video_duration_sec': 25.0,
            'beats': [
                {
                    'id': 'B01',
                    'index': 1,
                    'stage': 'clearing',
                    'operation': 'clearing',
                    'visible_action': 'Worker sweeps dirt',
                    'visible_result': 'Floor clean',
                    'zh': {'visible_action': '扫地', 'visible_result': '地面干净', 'headline': '扫地'}
                }
            ]
        })

        with patch('prompt_pipeline._multimodal_chat', return_value=mock_beats_response):
            res_state = rp.run_reverse(state, {})

        self.assertEqual(res_state['stage'], 'review_beats')
        beats_path = rp._beats_path(job_id)
        self.assertTrue(os.path.exists(beats_path))
        with open(beats_path, 'r', encoding='utf-8') as f:
            beats_on_disk = json.load(f)
        self.assertEqual(len(beats_on_disk['beats']), 1)
        # 走了哪条通道要留在状态里：卡点上「这份阶梯多准」全靠它回答。
        self.assertEqual(res_state.get('reverse_mode'), 'fast')
        self.assertIsNone(res_state.get('reverse_fallback'))

    def test_run_reverse_honours_deep_mode_from_config(self):
        """config.reverseMode='deep' 必须真的走标准 Pass A+B，一次都不碰极速通道。

        前端「反推通道」这个单选框的全部价值就在这一条上：选了深度却仍旧直读，
        用户付了 3~6 分钟的时间，拿到的还是 20 帧压缩图直出的那份阶梯。
        """
        job_dir, overview = self._setup_mock_job()
        job_id = os.path.basename(job_dir)
        state = {
            'job_id': job_id,
            'video_name': 'test.mp4',
            'stage': 'confirm_cost',
            'video_path': os.path.join(job_dir, 'test.mp4'),
        }
        rp._save_state(state)

        facts = {'frame_count': 2, 'model': 'stub', 'scope': 'plan', 'facts': []}
        beats = {'beats': [{'id': 'B01', 'index': 1, 'start': 0.0, 'end': 25.0,
                            'stage': 'demolition', 'operation': 'clearing'}]}

        with patch('prompt_pipeline.fast_reverse.fast_video_native_reverse') as fast,              patch('prompt_pipeline.reverse.extract_frame_facts', return_value=facts),              patch('prompt_pipeline.reverse.verify_peak_frames', side_effect=lambda c, d, f, **kw: f),              patch('prompt_pipeline.reverse.cluster_beats', return_value=beats),              patch('prompt_pipeline.reverse.translate_beats'):
            res_state = rp.run_reverse(state, {'reverseMode': 'deep'})

        fast.assert_not_called()
        self.assertEqual(res_state.get('reverse_mode'), 'deep')
        self.assertEqual(res_state['stage'], 'review_beats')

    def test_run_reverse_records_the_silent_fallback_to_deep(self):
        """极速通道炸了会静默降级到标准链路——降级本身没问题，不留痕才有问题。"""
        job_dir, overview = self._setup_mock_job()
        job_id = os.path.basename(job_dir)
        state = {
            'job_id': job_id,
            'video_name': 'test.mp4',
            'stage': 'confirm_cost',
            'video_path': os.path.join(job_dir, 'test.mp4'),
        }
        rp._save_state(state)

        facts = {'frame_count': 2, 'model': 'stub', 'scope': 'plan', 'facts': []}
        beats = {'beats': [{'id': 'B01', 'index': 1, 'start': 0.0, 'end': 25.0,
                            'stage': 'demolition', 'operation': 'clearing'}]}

        with patch('prompt_pipeline.fast_reverse.fast_video_native_reverse',
                   side_effect=ValueError('极速反推未识别出有效节拍')),              patch('prompt_pipeline.reverse.extract_frame_facts', return_value=facts),              patch('prompt_pipeline.reverse.verify_peak_frames', side_effect=lambda c, d, f, **kw: f),              patch('prompt_pipeline.reverse.cluster_beats', return_value=beats),              patch('prompt_pipeline.reverse.translate_beats'):
            res_state = rp.run_reverse(state, {})

        self.assertEqual(res_state.get('reverse_mode'), 'deep')
        self.assertIn('未识别出有效节拍', res_state.get('reverse_fallback') or '')


if __name__ == '__main__':
    unittest.main()

