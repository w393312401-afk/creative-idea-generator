import os
import json
import shutil
import tempfile
import unittest

from manifest_store import (
    QualityGate,
    load_manifest,
    save_manifest,
    get_frame,
    get_frame_quality_gate,
    set_frame_quality_gate,
)


class TestManifestStore(unittest.TestCase):
    """Direction-4 refactor: the single shared manifest.json access layer, replacing the
    three independent hand-rolled json.load/json.dump implementations that used to live in
    frame_generator.py / video_generator.py / pipeline_orchestrator.py."""

    def setUp(self):
        self.project_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.project_dir, ignore_errors=True)

    def _manifest_path(self):
        return os.path.join(self.project_dir, 'manifest.json')

    def test_load_manifest_returns_empty_dict_when_missing(self):
        self.assertEqual(load_manifest(self.project_dir), {})

    def test_save_then_load_roundtrips(self):
        save_manifest(self.project_dir, {'frames': [{'sequence': 1}]})
        self.assertEqual(load_manifest(self.project_dir), {'frames': [{'sequence': 1}]})

    def test_get_frame_finds_by_sequence(self):
        manifest = {'frames': [{'sequence': 1, 'url': 'a'}, {'sequence': 2, 'url': 'b'}]}
        self.assertEqual(get_frame(manifest, 2), {'sequence': 2, 'url': 'b'})
        self.assertIsNone(get_frame(manifest, 3))

    def test_get_frame_quality_gate_none_when_manifest_missing(self):
        self.assertIsNone(get_frame_quality_gate(self.project_dir, 1))

    def test_get_frame_quality_gate_none_when_frame_missing(self):
        save_manifest(self.project_dir, {'frames': [{'sequence': 1, 'quality_gate': 'auto_approved'}]})
        self.assertIsNone(get_frame_quality_gate(self.project_dir, 2))

    def test_set_frame_quality_gate_updates_existing_frame_on_disk(self):
        save_manifest(self.project_dir, {'frames': [{'sequence': 1, 'quality_gate': 'pending_manual_review'}]})
        set_frame_quality_gate(self.project_dir, 1, QualityGate.AUTO_APPROVED, reason='PASS')

        on_disk = load_manifest(self.project_dir)
        self.assertEqual(on_disk['frames'][0]['quality_gate'], 'auto_approved')
        self.assertEqual(on_disk['frames'][0]['vlm_qa_reason'], 'PASS')
        # QualityGate members are real strings, so this is what actually lands in the JSON file.
        with open(self._manifest_path(), 'r', encoding='utf-8') as f:
            raw = f.read()
        self.assertIn('"auto_approved"', raw)
        self.assertNotIn('QualityGate', raw)

    def test_set_frame_quality_gate_is_a_noop_when_manifest_missing(self):
        set_frame_quality_gate(self.project_dir, 1, QualityGate.AUTO_APPROVED)
        self.assertEqual(load_manifest(self.project_dir), {})

    def test_set_frame_quality_gate_is_a_noop_when_frame_missing(self):
        save_manifest(self.project_dir, {'frames': [{'sequence': 1, 'quality_gate': 'pending_manual_review'}]})
        set_frame_quality_gate(self.project_dir, 99, QualityGate.AUTO_APPROVED)
        on_disk = load_manifest(self.project_dir)
        self.assertEqual(on_disk['frames'][0]['quality_gate'], 'pending_manual_review')

    def test_quality_gate_members_compare_equal_to_their_string_literals(self):
        # Existing code across the codebase compares quality_gate fields against bare
        # string literals (e.g. frame.get('quality_gate') == 'vlm_qa_failed'); the enum
        # must stay drop-in compatible with that during the incremental migration.
        self.assertEqual(QualityGate.AUTO_APPROVED, 'auto_approved')
        self.assertEqual(QualityGate.PENDING_MANUAL_REVIEW, 'pending_manual_review')
        self.assertEqual(QualityGate.VLM_QA_FAILED, 'vlm_qa_failed')
        self.assertEqual(QualityGate.I2I_FALLBACK_DEGRADED, 'i2i_fallback_degraded')
        self.assertEqual(QualityGate.NEEDS_HUMAN_REVIEW, 'needs_human_review')


if __name__ == '__main__':
    unittest.main()
