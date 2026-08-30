"""Unit tests for benchmark-aligned sequence review and collage macro audit."""
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch, MagicMock

import prompt_pipeline as pp
import chain_guard as cg


class TestBenchmarkAlignedReview(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix='test_benchmark_review_')
        self.config = {'provider': 'mock'}
        self.prompt_block = (
            "IMAGE 1 (ESTABLISH): Raw dusty interior with exposed concrete walls.\n"
            "VIDEO 1: Worker clears dust and debris with broom.\n"
            "IMAGE 2 (CLEANED): Dust and loose rubble completely removed.\n"
            "VIDEO 2: Worker installs wooden floor joists on ground.\n"
            "IMAGE 3 (FRAMED): Wooden floor joists aligned across the entire floor.\n"
        )
        self.before_img = os.path.join(self.tmp_dir, 'img_001.webp')
        self.after_img = os.path.join(self.tmp_dir, 'img_002.webp')
        self.ref_img = os.path.join(self.tmp_dir, 'ref_001.png')
        with open(self.before_img, 'wb') as f:
            f.write(b'fake_before')
        with open(self.after_img, 'wb') as f:
            f.write(b'fake_after')
        with open(self.ref_img, 'wb') as f:
            f.write(b'fake_ref')

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_check_beat_consistency_without_ref_frame(self):
        with patch.object(pp, '_multimodal_chat', return_value='[]') as mock_chat:
            res = pp.check_beat_consistency(
                self.config, self.prompt_block, 1, 2,
                self.before_img, self.after_img
            )
            self.assertEqual(res, [])
            # Should have sent 2 images
            self.assertEqual(len(mock_chat.call_args[0][3]), 2)
            self.assertNotIn("IMAGE REF", mock_chat.call_args[0][2])

    def test_check_beat_consistency_with_ref_frame(self):
        mock_resp = '["机位偏离爆款原片：原片为25度俯拍，生成图退化为平视"]'
        with patch.object(pp, '_multimodal_chat', return_value=mock_resp) as mock_chat:
            res = pp.check_beat_consistency(
                self.config, self.prompt_block, 1, 2,
                self.before_img, self.after_img,
                ref_frame_path=self.ref_img
            )
            self.assertEqual(len(res), 1)
            self.assertIn("机位偏离爆款原片", res[0])
            # Should have sent 3 images
            self.assertEqual(len(mock_chat.call_args[0][3]), 3)
            self.assertEqual(mock_chat.call_args[0][3][2], self.ref_img)
            self.assertIn("IMAGE REF", mock_chat.call_args[0][2])

    def test_check_collage_macro_alignment(self):
        c1 = os.path.join(self.tmp_dir, 'video_collage.jpg')
        c2 = os.path.join(self.tmp_dir, 'full_collage.jpg')
        with open(c1, 'wb') as f: f.write(b'c1')
        with open(c2, 'wb') as f: f.write(b'c2')

        mock_resp = '["全剧节奏曲线偏差：粗装修阶段过早收尾"]'
        with patch.object(pp, '_multimodal_chat', return_value=mock_resp) as mock_chat:
            issues = pp.check_collage_macro_alignment(self.config, c1, c2)
            self.assertEqual(len(issues), 1)
            self.assertIn("节奏曲线", issues[0])
            self.assertEqual(len(mock_chat.call_args[0][3]), 2)

    def test_find_reference_frames_for_project(self):
        # Test on real output dir if present
        actual_dir = os.path.abspath('outputs/run_replica_77b57b380639_雨林泥滩烂木棚爆改避洪高脚庄园')
        if os.path.exists(actual_dir):
            real_refs, real_col = pp.find_reference_frames_for_project(actual_dir, total_beats=17)
            print(f"REAL REFS COUNT: {len(real_refs)}, REAL COLLAGE: {real_col}")
            for k in sorted(real_refs.keys())[:5]:
                print(f"  Beat {k}: {real_refs[k]}")

        # Create project directory with storyboard and source collage
        pdir = os.path.join(self.tmp_dir, 'test_proj')
        sdir = os.path.join(pdir, 'storyboard')
        os.makedirs(sdir, exist_ok=True)
        c_src = os.path.join(pdir, 'benchmark_video_collage.jpg')
        with open(c_src, 'wb') as f: f.write(b'collage')

        kf1 = os.path.join(sdir, 'scene_001.png')
        kf2 = os.path.join(sdir, 'scene_002.png')
        kf3 = os.path.join(sdir, 'scene_003.png')
        for k in (kf1, kf2, kf3):
            with open(k, 'wb') as f: f.write(b'kf')

        refs, collage_path = pp.find_reference_frames_for_project(pdir, total_beats=2)
        self.assertEqual(collage_path, c_src)
        self.assertIn(1, refs)
        self.assertIn(2, refs)

    def test_guard_beat_with_ref_frame(self):
        frames_dir = os.path.join(self.tmp_dir, 'frames')
        os.makedirs(frames_dir, exist_ok=True)
        img1 = os.path.join(frames_dir, 'img_001.webp')
        img2 = os.path.join(frames_dir, 'img_002.webp')
        with open(img1, 'wb') as f: f.write(b'1')
        with open(img2, 'wb') as f: f.write(b'2')
        manifest = {'title': 'Test', 'frames': [{'sequence': 1}, {'sequence': 2}]}
        with open(os.path.join(self.tmp_dir, 'manifest.json'), 'w') as f:
            json.dump(manifest, f)

        with patch.object(cg, 'check_beat_consistency', return_value=['机位偏离爆款原片']) as mock_check, \
             patch.object(cg, '_verify_review_violation', return_value=True), \
             patch.object(cg, 'classify_chain_impact', return_value=['chain']):
            res = cg.guard_beat(
                self.config, 'Test', self.prompt_block, 1, self.tmp_dir,
                allow_halt=True, ref_path=self.ref_img
            )
            self.assertEqual(res['verdict'], 'flagged')
            self.assertTrue(res['halt'])
            self.assertEqual(mock_check.call_args[1].get('ref_frame_path'), self.ref_img)

    def test_check_full_sequence_consistency_with_benchmark_and_collage(self):
        f1 = os.path.join(self.tmp_dir, 'img_001.webp')
        f2 = os.path.join(self.tmp_dir, 'img_002.webp')
        f3 = os.path.join(self.tmp_dir, 'img_003.webp')
        c_src = os.path.join(self.tmp_dir, 'source_collage.jpg')
        c_gen = os.path.join(self.tmp_dir, 'full_collage.jpg')
        with open(f3, 'wb') as f: f.write(b'3')
        with open(c_src, 'wb') as f: f.write(b'c_src')
        with open(c_gen, 'wb') as f: f.write(b'c_gen')

        frame_paths = {1: f1, 2: f2, 3: f3}
        ref_paths = {1: self.ref_img, 2: self.ref_img}

        with patch.object(pp, 'check_beat_consistency', return_value=[]) as mock_beat, \
             patch.object(pp, 'check_global_sequence_consistency', return_value={}), \
             patch.object(pp, 'check_collage_macro_alignment', return_value=['拼图整体节奏偏快']):
            res = pp.check_full_sequence_consistency(
                self.config, self.prompt_block, frame_paths,
                ref_frame_paths=ref_paths,
                source_collage_path=c_src,
                rendered_collage_path=c_gen
            )
            self.assertEqual(res['failures'], {})
            self.assertEqual(res['collage_macro_issues'], ['拼图整体节奏偏快'])
            self.assertTrue(any(i['layer'] == 'collage_macro' for i in res['issues']))
            # Ensure ref_frame_path was passed to beat check
            self.assertEqual(mock_beat.call_args[1].get('ref_frame_path'), self.ref_img)

    def test_check_anchor_consistency(self):
        mock_resp = '["首帧机位与爆款原片不符：原片为低角度仰拍，生成图为平视"]'
        with patch.object(pp, '_multimodal_chat', return_value=mock_resp) as mock_chat:
            res = pp.check_anchor_consistency(
                self.config, self.prompt_block, self.before_img, ref_frame_path=self.ref_img
            )
            self.assertEqual(len(res), 1)
            self.assertIn("首帧机位与爆款原片不符", res[0])
            self.assertEqual(len(mock_chat.call_args[0][3]), 2)
            self.assertEqual(mock_chat.call_args[0][3][1], self.ref_img)

    def test_guard_anchor_frame(self):
        frames_dir = os.path.join(self.tmp_dir, 'frames')
        os.makedirs(frames_dir, exist_ok=True)
        img1 = os.path.join(frames_dir, 'img_001.webp')
        with open(img1, 'wb') as f: f.write(b'1')
        manifest = {'title': 'Test', 'frames': [{'sequence': 1}]}
        with open(os.path.join(self.tmp_dir, 'manifest.json'), 'w') as f:
            json.dump(manifest, f)

        with patch.object(cg, 'check_anchor_consistency', return_value=['首帧机位偏离爆款']), \
             patch.object(cg, '_verify_review_violation', return_value=True), \
             patch.object(cg, 'classify_chain_impact', return_value=['chain']):
            res = cg.guard_anchor(
                self.config, 'Test', self.prompt_block, self.tmp_dir,
                allow_halt=True, ref_path=self.ref_img
            )
            self.assertEqual(res['verdict'], 'flagged')
            self.assertTrue(res['halt'])
            self.assertEqual(res['issues'][0]['layer'], 'anchor')

    def test_evaluate_and_select_best_candidate_with_ref_frame(self):
        import candidate_selection_pipeline as csp
        cand1 = os.path.join(self.tmp_dir, 'cand_001.webp')
        with open(cand1, 'wb') as f: f.write(b'cand1')

        mock_vlm_resp = json.dumps({
            "candidates": [{
                "index": 1, "score": 92,
                "strengths": "高度还原爆款原片俯仰机位",
                "defects": "无明显缺陷"
            }],
            "best_index": 1,
            "selection_reason": "完美契合爆款机位与施工提示词"
        })
        with patch.object(csp, '_multimodal_chat', return_value=mock_vlm_resp) as mock_chat:
            res = csp.evaluate_and_select_best_candidate(
                self.config, 'IMAGE 1 prompt', None, [cand1], 1,
                ref_frame_path=self.ref_img
            )
            self.assertEqual(res['best_index'], 1)
            self.assertEqual(res['candidates'][0]['score'], 92)
            self.assertIn("高度还原爆款原片", res['candidates'][0]['strengths'])
            # Verify ref_frame_path was sent in multimodal chat
            self.assertIn(self.ref_img, mock_chat.call_args[1]['image_paths'])


if __name__ == '__main__':
    unittest.main()
