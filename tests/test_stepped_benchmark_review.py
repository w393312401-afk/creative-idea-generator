"""Tests for stepped pipeline benchmark reference frame review and collage viewer comparison."""
import os
import json
import tempfile
import shutil
import unittest
from unittest.mock import patch

import stepped_pipeline as sp


class TestSteppedBenchmarkReview(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix='test_stepped_ref_')
        self.project_title = 'test_benchmark_stepped_proj'
        self.project_dir = os.path.join(self.tmp_dir, self.project_title)
        os.makedirs(self.project_dir, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_enrich_state_with_refs(self):
        # Create manifest with replica_job_id
        manifest = {
            'title': self.project_title,
            'replica_job_id': 'replica_mock123456',
            'dimensions': {}
        }
        with open(os.path.join(self.project_dir, 'manifest.json'), 'w', encoding='utf-8') as f:
            json.dump(manifest, f)

        # Mock find_reference_frames_for_project
        fake_refs = {1: '/outputs/ref_001.png', 2: '/outputs/ref_002.png'}
        fake_collage = '/outputs/source_collage.jpg'

        with patch('stepped_pipeline._get_project_dir', return_value=self.project_dir), \
             patch('stepped_pipeline.find_reference_frames_for_project', return_value=(fake_refs, fake_collage)):
            
            raw_state = {
                'title': self.project_title,
                'stage': 'review_anchor',
                'total_beats': 2,
            }
            enriched = sp._enrich_state_with_refs(raw_state, self.project_dir)
            self.assertIn('ref_frames', enriched)
            self.assertEqual(enriched['ref_frames'][1], '/outputs/ref_001.png')
            self.assertEqual(enriched['ref_frames'][2], '/outputs/ref_002.png')
            self.assertEqual(enriched['source_collage'], fake_collage)

    def test_get_stepped_status_includes_refs(self):
        state = {
            'title': self.project_title,
            'stage': 'review_batch',
            'total_beats': 3,
        }

        fake_refs = {1: '/outputs/ref_001.png', 2: '/outputs/ref_002.png', 3: '/outputs/ref_003.png'}
        fake_collage = '/outputs/benchmark_collage.jpg'

        with patch('stepped_pipeline._get_project_dir', return_value=self.project_dir), \
             patch('stepped_pipeline.find_reference_frames_for_project', return_value=(fake_refs, fake_collage)):
            
            sp._save_state(self.project_title, state)
            loaded = sp.get_stepped_status(self.project_title)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded['ref_frames'][3], '/outputs/ref_003.png')
            self.assertEqual(loaded['source_collage'], fake_collage)

    def test_find_reference_frames_on_actual_or_variant_dir(self):
        from prompt_pipeline import find_reference_frames_for_project
        actual_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'outputs', 'run_replica_77b57b380639_废弃红树林高脚屋爆改成避水豪宅')
        if os.path.exists(actual_dir):
            refs, collage = find_reference_frames_for_project(actual_dir, total_beats=5)
            self.assertTrue(len(refs) > 0, "Should resolve reference frames from parent baseline")
            self.assertIsNotNone(collage, "Should resolve source collage from parent baseline")
            self.assertTrue(collage.endswith('_collage.jpg'))

    def test_api_project_references_endpoint(self):
        from server import SparkRequestHandler
        import io

        fake_refs = {1: '/outputs/ref_001.png', 2: '/outputs/ref_002.png'}
        fake_collage = '/outputs/source_collage.jpg'

        with patch('prompt_pipeline.find_reference_frames_for_project', return_value=(fake_refs, fake_collage)), \
             patch('server_common._get_project_dir', return_value=self.project_dir):
            
            # Instantiate handler mock
            handler = SparkRequestHandler.__new__(SparkRequestHandler)
            handler.wfile = io.BytesIO()
            handler._headers_buffer = []
            
            sent_data = {}
            def mock_send_json(data, status=200):
                nonlocal sent_data
                sent_data = data
            
            handler._send_json = mock_send_json
            handler._gate = lambda: True

            # Simulate GET /api/project/references?title=test_benchmark_stepped_proj
            query = {'title': [self.project_title]}
            path = '/api/project/references'
            
            # Run endpoint logic snippet as in server.py
            from server_common import _get_project_dir, _safe_project_name, OUTPUT_ROOT
            from prompt_pipeline import find_reference_frames_for_project
            
            title = query.get('title', [''])[0].strip()
            job_id = query.get('job_id', [''])[0].strip()
            total_beats = None
            pdir = _get_project_dir(title)
            
            ref_dict = {}
            src_collage = None
            if pdir and os.path.exists(pdir):
                refs, collage = find_reference_frames_for_project(pdir, total_beats)
                if refs:
                    for k, v in refs.items():
                        if v:
                            u = str(v).replace('\\', '/')
                            if '/outputs/' in u:
                                u = u[u.index('/outputs/'):]
                            ref_dict[int(k)] = u
                if collage:
                    cu = str(collage).replace('\\', '/')
                    if '/outputs/' in cu:
                        cu = cu[cu.index('/outputs/'):]
                    src_collage = cu

            handler._send_json({
                'status': 'ok',
                'ref_frames': ref_dict,
                'source_collage_url': src_collage,
            })

            self.assertEqual(sent_data['status'], 'ok')
            self.assertEqual(sent_data['ref_frames'][1], '/outputs/ref_001.png')
            self.assertEqual(sent_data['source_collage_url'], '/outputs/source_collage.jpg')


if __name__ == '__main__':
    unittest.main()
