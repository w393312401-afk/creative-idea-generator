# tests/test_cover_concurrency.py
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch, MagicMock

import server
import server_common


class TestCoverConcurrency(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.old_output_root = server_common.OUTPUT_ROOT
        server_common.OUTPUT_ROOT = self.tmp_dir
        self.project_key = "test_concurrent_cover_project"
        self.project_dir = os.path.join(self.tmp_dir, self.project_key)
        os.makedirs(self.project_dir, exist_ok=True)

    def tearDown(self):
        server_common.OUTPUT_ROOT = self.old_output_root
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_concurrent_cover_worker_generates_multiple_candidates(self):
        task_id = "test_cover_concurrent_1"
        parent_task_id = "test_parent_spark_1"
        server.get_or_create_task(task_id, {"type": "cover", "theme": "Cabin", "project_key": self.project_key})
        server.get_or_create_task(parent_task_id, {"type": "spark", "result": {"covers": []}})

        generated_paths = []

        def mock_generate_text_image(config, prompt, target_path):
            generated_paths.append((prompt, target_path))
            with open(target_path, 'wb') as f:
                f.write(b"FAKE_WEBP_IMAGE_CONTENT")

        events_received = []

        def mock_notify_listeners(t_id, event, data):
            if t_id == task_id:
                events_received.append((event, data))

        with patch('server._chat', return_value="I BUILT A SECRET CABIN!"), \
             patch('server._generate_text_image', side_effect=mock_generate_text_image), \
             patch('server.notify_listeners', side_effect=mock_notify_listeners):

            server.generate_cover_worker(
                task_id=task_id,
                config={'candidateConcurrency': 4},
                parent_task_id=parent_task_id,
                title="秘密木屋",
                theme="Cabin",
                prompt_block="[IMG 001]\nBefore cabin\n[IMG 002]\nAfter cabin",
                project_key=self.project_key,
                concurrent=True,
                count=4
            )

        task = server.ACTIVE_TASKS.get(task_id)
        self.assertEqual(task["status"], "completed")
        result = task["result"]

        # Verify 4 covers were generated
        self.assertEqual(len(generated_paths), 4)
        self.assertEqual(len(result["covers"]), 4)
        self.assertEqual(result["content"], result["covers"][0])

        # Verify filenames end with _1.webp, _2.webp, _3.webp, _4.webp
        for idx, (prompt, path) in enumerate(generated_paths):
            self.assertTrue(path.endswith(f"_{idx + 1}.webp"))
            if idx > 0:
                self.assertIn(f"[Variation #{idx + 1}]", prompt)

        # Verify progress events
        generating_events = [data for evt, data in events_received if evt == 'cover_generating']
        self.assertGreaterEqual(len(generating_events), 4)
        final_gen = generating_events[-1]
        self.assertEqual(final_gen["completed"], 4)
        self.assertEqual(final_gen["total"], 4)

        # Verify parent task recorded all 4 covers
        parent_task = server.ACTIVE_TASKS.get(parent_task_id)
        self.assertEqual(len(parent_task["result"]["covers"]), 4)

        # Verify manifest active_cover is set
        manifest = server_common.read_manifest(self.project_dir)
        self.assertIsNotNone(manifest)
        self.assertEqual(manifest.get("active_cover"), result["content"])

    def test_standard_cover_worker_single_mode(self):
        task_id = "test_cover_standard_1"
        server.get_or_create_task(task_id, {"type": "cover", "theme": "Treehouse", "project_key": self.project_key})

        def mock_generate_text_image(config, prompt, target_path):
            with open(target_path, 'wb') as f:
                f.write(b"FAKE_SINGLE_WEBP")

        with patch('server._chat', return_value="EPIC TREEHOUSE!"), \
             patch('server._generate_text_image', side_effect=mock_generate_text_image):

            server.generate_cover_worker(
                task_id=task_id,
                config={},
                parent_task_id=None,
                title="树屋建造",
                theme="Treehouse",
                prompt_block="[IMG 001]\nBefore treehouse\n[IMG 002]\nAfter treehouse",
                project_key=self.project_key,
                concurrent=False,
                count=1
            )

        task = server.ACTIVE_TASKS.get(task_id)
        self.assertEqual(task["status"], "completed")
        self.assertEqual(len(task["result"]["covers"]), 1)
        self.assertEqual(task["result"]["content"], task["result"]["covers"][0])

    def test_concurrent_cover_worker_partial_success(self):
        task_id = "test_cover_partial_1"
        server.get_or_create_task(task_id, {"type": "cover", "theme": "Pool", "project_key": self.project_key})

        def mock_generate_with_one_failure(config, prompt, target_path):
            if "_2.webp" in target_path:
                raise RuntimeError("Gateway 502 error on variation 2")
            with open(target_path, 'wb') as f:
                f.write(b"OK_IMAGE")

        with patch('server._chat', return_value="EPIC POOL!"), \
             patch('server._generate_text_image', side_effect=mock_generate_with_one_failure):

            server.generate_cover_worker(
                task_id=task_id,
                config={'candidateConcurrency': 4},
                parent_task_id=None,
                title="水池改造",
                theme="Pool",
                prompt_block="[IMG 001]\nBefore\n[IMG 002]\nAfter",
                project_key=self.project_key,
                concurrent=True,
                count=4
            )

        task = server.ACTIVE_TASKS.get(task_id)
        self.assertEqual(task["status"], "completed")
        # 3 out of 4 succeeded
        self.assertEqual(len(task["result"]["covers"]), 3)

    def test_concurrent_cover_worker_all_fail(self):
        task_id = "test_cover_fail_1"
        server.get_or_create_task(task_id, {"type": "cover", "theme": "Cave", "project_key": self.project_key})

        def mock_fail_all(config, prompt, target_path):
            raise RuntimeError("API quota exhausted")

        with patch('server._chat', return_value="EPIC CAVE!"), \
             patch('server._generate_text_image', side_effect=mock_fail_all):

            server.generate_cover_worker(
                task_id=task_id,
                config={'candidateConcurrency': 4},
                parent_task_id=None,
                title="洞穴改造",
                theme="Cave",
                prompt_block="[IMG 001]\nBefore\n[IMG 002]\nAfter",
                project_key=self.project_key,
                concurrent=True,
                count=4
            )

        task = server.ACTIVE_TASKS.get(task_id)
        self.assertEqual(task["status"], "failed")
        self.assertIn("所有封面并发生成均失败", task["error"])

    def test_concurrent_cover_worker_cancelled(self):
        task_id = "test_cover_cancel_1"
        server.get_or_create_task(task_id, {"type": "cover", "theme": "Bunker", "project_key": self.project_key})
        server.ACTIVE_TASKS[task_id]["status"] = "cancelled"

        server.generate_cover_worker(
            task_id=task_id,
            config={'candidateConcurrency': 4},
            parent_task_id=None,
            title="地堡建造",
            theme="Bunker",
            prompt_block="[IMG 001]\nBefore\n[IMG 002]\nAfter",
            project_key=self.project_key,
            concurrent=True,
            count=4
        )

        task = server.ACTIVE_TASKS.get(task_id)
        self.assertEqual(task["status"], "cancelled")


if __name__ == '__main__':
    unittest.main()
