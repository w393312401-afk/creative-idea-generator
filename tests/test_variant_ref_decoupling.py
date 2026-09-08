# -*- coding: utf-8 -*-
import os
import json
import shutil
import tempfile
import unittest

import prompt_pipeline as pp
import chain_guard


class TestVariantRefDecoupling(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='test_variant_ref_')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_baseline_project_gets_reference_frames(self):
        # 建立母本目录与抽帧
        rf_dir = os.path.join(self.tmp, 'review_frames')
        os.makedirs(rf_dir, exist_ok=True)
        img1 = os.path.join(rf_dir, 'review_001.png')
        with open(img1, 'w') as f:
            f.write('fake')

        tb_path = os.path.join(self.tmp, 'timelapse_beats.json')
        with open(tb_path, 'w', encoding='utf-8') as f:
            json.dump({
                'pipeline_id': 'replica_baseline_01',
                'job_type': 'baseline',
                'beats': [
                    {'id': 'B01', 'coverage_frames': [{'frame': 'review_001.png'}]}
                ]
            }, f)

        col_path = os.path.join(self.tmp, 'source_collage.jpg')
        with open(col_path, 'w') as f:
            f.write('fake')

        self.assertFalse(pp.is_variant_project(self.tmp))
        refs, roles, collage = pp.find_reference_frames_with_roles(self.tmp, total_beats=1)
        self.assertIn(1, refs)
        self.assertEqual(roles.get(1), 'benchmark')
        self.assertEqual(collage, col_path)

    def test_variant_project_blocks_benchmark_frames_but_keeps_collage(self):
        # 1. 建立母本目录
        parent_dir = os.path.join(self.tmp, 'parent_baseline')
        parent_rf = os.path.join(parent_dir, 'review_frames')
        os.makedirs(parent_rf, exist_ok=True)
        with open(os.path.join(parent_rf, 'review_001.png'), 'w') as f:
            f.write('sea_cave_fake_img')

        with open(os.path.join(parent_dir, 'timelapse_beats.json'), 'w', encoding='utf-8') as f:
            json.dump({
                'pipeline_id': 'replica_parent123',
                'job_type': 'baseline',
                'beats': [
                    {'id': 'B01', 'coverage_frames': [{'frame': 'review_001.png'}]}
                ]
            }, f)

        parent_col = os.path.join(parent_dir, 'parent_collage.jpg')
        with open(parent_col, 'w') as f:
            f.write('fake_parent_collage')

        # 2. 建立变体目录
        var_dir = os.path.join(self.tmp, 'run_replica_variant456_二创变体_高山避险')
        os.makedirs(var_dir, exist_ok=True)
        with open(os.path.join(var_dir, 'manifest.json'), 'w', encoding='utf-8') as f:
            json.dump({
                'title': '高山避险小屋 · 二创变体',
                'parent_baseline_id': 'replica_parent123',
                'job_type': 'variant',
                'is_variant': True,
                'frames': []
            }, f)

        # 模拟 replica_pipeline 的 job_dir 能索引到 parent
        from unittest.mock import patch
        with patch('replica_pipeline.job_dir', return_value=parent_dir), \
             patch('replica_pipeline.validate_job_id', return_value=True):
            self.assertTrue(pp.is_variant_project(var_dir))
            refs, roles, collage = pp.find_reference_frames_with_roles(var_dir, total_beats=1)
            # 变体必须清空逐拍 benchmark 挂帧！
            self.assertEqual(refs, {})
            self.assertEqual(roles, {})
            # 变体仍然保留母本拼图用于宏观比对
            self.assertEqual(collage, parent_col)

    def test_guard_anchor_passes_no_ref_for_variant(self):
        from unittest.mock import patch
        var_dir = os.path.join(self.tmp, 'run_replica_v789_二创变体_雪山')
        os.makedirs(os.path.join(var_dir, 'frames'), exist_ok=True)
        img1 = os.path.join(var_dir, 'frames', 'img_001.webp')
        with open(img1, 'w') as f:
            f.write('fake')
        with open(os.path.join(var_dir, 'manifest.json'), 'w', encoding='utf-8') as f:
            json.dump({
                'title': '高山小屋 · 二创变体',
                'parent_baseline_id': 'replica_parent123',
                'job_type': 'variant',
                'is_variant': True,
                'frames': [{'sequence': 1, 'file': img1}]
            }, f)

        prompt_block = "图片 1:\nGenerate mountain cabin\n\n视频 1:\nWorker builds"
        with patch('chain_guard.check_anchor_consistency', return_value=[]) as mock_check:
            res = chain_guard.guard_anchor(
                {}, '高山小屋 · 二创变体', prompt_block, var_dir,
                ref_path='some_accidental_ref_path.png'
            )
            self.assertEqual(res['verdict'], 'pass')
            mock_check.assert_called_once()
            _, kwargs = mock_check.call_args
            self.assertIsNone(kwargs.get('ref_frame_path'))


if __name__ == '__main__':
    unittest.main()
