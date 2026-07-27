import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

import server_common


class TestProjectNames(unittest.TestCase):
    def tearDown(self):
        server_common.set_project_key_context(None)

    def test_safe_project_name_preserves_chinese_theme(self):
        name = server_common._safe_project_name('退役有轨电车车厢改造成都市避世睡眠舱')
        self.assertIn('退役有轨电车车厢', name)
        self.assertNotRegex(name, r'[\\/:*?"<>|#?%&+=]')

    def test_get_project_dir_finds_legacy_ascii_hash_folder(self):
        tmp = tempfile.mkdtemp()
        try:
            title = '纯中文主题'
            legacy = server_common._legacy_ascii_project_name(title)
            legacy_dir = os.path.join(tmp, legacy)
            os.makedirs(legacy_dir)
            with patch.object(server_common, 'OUTPUT_ROOT', tmp):
                self.assertEqual(server_common._get_project_dir(title), legacy_dir)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_project_key_is_unique_per_compose_run_and_keeps_id_before_truncation(self):
        long_title = '同一个很长的创意标题' * 20
        first = server_common.make_idea_project_key('1001', long_title)
        second = server_common.make_idea_project_key('1002', long_title)

        self.assertNotEqual(first, second)
        self.assertTrue(server_common._safe_project_name(first).startswith('run_1001_'))
        self.assertTrue(server_common._safe_project_name(second).startswith('run_1002_'))

    def test_project_key_context_isolates_disk_dir_without_changing_display_title(self):
        with patch.object(server_common, 'OUTPUT_ROOT', '/tmp/spark-test-outputs'):
            display_dir = server_common._get_project_dir('相同展示标题')
            server_common.set_project_key_context('run_2002__相同展示标题')
            isolated_dir = server_common._get_project_dir('相同展示标题')
            server_common.set_project_key_context(None)
            restored_dir = server_common._get_project_dir('相同展示标题')

        self.assertNotEqual(display_dir, isolated_dir)
        self.assertIn('run_2002_', os.path.basename(isolated_dir))
        self.assertEqual(restored_dir, display_dir)


if __name__ == '__main__':
    unittest.main()
