import json
import os
import shutil
import tempfile
import unittest

from unittest.mock import patch

import video_generator
from video_generator import (
    rewrite_prompt_for_two_card_ui,
    load_slot_frames,
    plan_video_slots,
    _ManifestWriter,
    _video_info,
    _BatchBridge,
    generate_video_sequence,
    merge_project_videos,
    VideoMergeBlocked,
)


class TestVideoGenSerialLock(unittest.TestCase):
    """Direction-4 fix: the shared-AdsPower-browser serial lock used to only wrap the
    manual /api/generate_videos call site in server.py, leaving the auto_run/render_staged
    paths (which reach generate_video_sequence via pipeline_orchestrator) unguarded. It now
    lives inside generate_video_sequence itself so every caller is serialized the same way."""

    def test_generate_video_sequence_holds_the_serial_lock_while_running(self):
        observed = {}

        def fake_impl(config, title, prompt_block, on_progress=None, target_slots=None):
            observed['locked'] = video_generator._VIDEO_GEN_SERIAL_LOCK.locked()
            return {'videos': []}

        with patch('video_generator._generate_video_sequence_locked', side_effect=fake_impl):
            generate_video_sequence({}, 'proj', 'block')

        self.assertTrue(observed['locked'])
        # Lock must be released again afterward, not held forever.
        self.assertFalse(video_generator._VIDEO_GEN_SERIAL_LOCK.locked())

    def test_concurrent_calls_are_serialized_not_parallel(self):
        import threading
        import time

        order = []
        entered = threading.Event()

        def fake_impl(config, title, prompt_block, on_progress=None, target_slots=None):
            order.append(('start', title))
            if title == 'first':
                entered.set()
                time.sleep(0.2)
            order.append(('end', title))
            return {'videos': []}

        with patch('video_generator._generate_video_sequence_locked', side_effect=fake_impl):
            t1 = threading.Thread(target=lambda: generate_video_sequence({}, 'first', 'block'))
            t1.start()
            entered.wait(timeout=2)
            t2 = threading.Thread(target=lambda: generate_video_sequence({}, 'second', 'block'))
            t2.start()
            t1.join(timeout=2)
            t2.join(timeout=2)

        # 'first' must fully finish (its 'end') before 'second' can even start — proving the
        # second caller (simulating an auto_run/render_staged task) blocked on the lock
        # instead of running concurrently against the shared browser session.
        self.assertEqual(order, [('start', 'first'), ('end', 'first'), ('start', 'second'), ('end', 'second')])


class TestMergeProjectVideosGates(unittest.TestCase):
    """Direction-7 fix: merge_project_videos is now the single source of truth for 'is this
    project complete enough to merge' — server.py's generate_videos_worker used to duplicate
    this judgment with its own slot-completeness derivation (from prompt_block instead of
    manifest.frames) before even calling merge_project_videos. A deliberate gate rejection
    raises VideoMergeBlocked (a RuntimeError subclass, so /api/merge_videos's generic
    `except Exception` still works unchanged) instead of a bare RuntimeError, so callers can
    tell 'refused on purpose' apart from an unexpected failure."""

    def setUp(self):
        self.project_dir = tempfile.mkdtemp()
        self.videos_dir = os.path.join(self.project_dir, 'videos')
        os.makedirs(self.videos_dir, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.project_dir, ignore_errors=True)

    def _write_manifest(self, frames, videos):
        with open(os.path.join(self.project_dir, 'manifest.json'), 'w', encoding='utf-8') as f:
            json.dump({'frames': frames, 'videos': videos}, f)

    def _touch_video_file(self, name):
        path = os.path.join(self.videos_dir, name)
        with open(path, 'wb') as f:
            f.write(b'fake video bytes')
        return f'/videos/{name}'

    def test_raises_video_merge_blocked_when_a_slot_is_missing(self):
        # 3 frames -> expected video slots 1..2; only slot 1 has a successful video.
        slot1_rel = self._touch_video_file('vid_001.mp4')
        self._write_manifest(
            frames=[{'slot': 1}, {'slot': 2}, {'slot': 3}],
            videos=[{'slot': 1, 'status': 'success', 'file': slot1_rel}],
        )
        with self.assertRaises(VideoMergeBlocked) as ctx:
            merge_project_videos(self.project_dir)
        self.assertIn('2', str(ctx.exception))

    def test_video_merge_blocked_is_a_runtime_error_subclass(self):
        # /api/merge_videos catches plain `except Exception` — must keep working unchanged.
        self.assertTrue(issubclass(VideoMergeBlocked, RuntimeError))

    def test_allow_partial_skips_the_completeness_gate(self):
        slot1_rel = self._touch_video_file('vid_001.mp4')
        self._write_manifest(
            frames=[{'slot': 1}, {'slot': 2}, {'slot': 3}],
            videos=[{'slot': 1, 'status': 'success', 'file': slot1_rel}],
        )
        # Should not raise — allow_partial bypasses gate 1 (and gate 2, since only one
        # video file exists to concat, ffmpeg construction below is exercised but the
        # gates themselves are what this test cares about).
        try:
            merge_project_videos(self.project_dir, allow_partial=True)
        except VideoMergeBlocked:
            self.fail("allow_partial=True must not raise VideoMergeBlocked")
        except Exception:
            pass  # ffmpeg/concat-stage failures in this minimal fixture are out of scope here

    def test_returns_none_when_manifest_missing(self):
        empty_dir = tempfile.mkdtemp()
        try:
            self.assertIsNone(merge_project_videos(empty_dir))
        finally:
            shutil.rmtree(empty_dir, ignore_errors=True)


class TestPromptRewrite(unittest.TestCase):
    """IMAGE slot / slot+1 → IMAGE 1 / IMAGE 2（Flow 两卡位 UI）。"""

    def test_english_and_chinese_rewrite(self):
        p = "Use IMAGE 3 as the start and image 4 as the end. 图片 3 到 图片 4。"
        out = rewrite_prompt_for_two_card_ui(p, 3)
        self.assertIn("IMAGE 1 as the start", out)
        self.assertIn("IMAGE 2 as the end", out)
        self.assertIn("IMAGE 1 到 IMAGE 2", out)
        self.assertNotIn("IMAGE 3", out)
        self.assertNotIn("图片 3", out)

    def test_no_false_match_on_longer_numbers(self):
        # slot=1 不得误改 IMAGE 12 / IMAGE 13
        out = rewrite_prompt_for_two_card_ui("IMAGE 12 and IMAGE 13 stay.", 1)
        self.assertIn("IMAGE 12", out)
        self.assertIn("IMAGE 13", out)


class _TmpDirCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.videos_dir = os.path.join(self.tmp, 'videos')
        self.frames_dir = os.path.join(self.tmp, 'frames')
        os.makedirs(self.videos_dir)
        os.makedirs(self.frames_dir)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _touch(self, path, content=b'x'):
        with open(path, 'wb') as f:
            f.write(content)
        return path

    def _make_frames(self, n):
        slot_to_path = {}
        for i in range(1, n + 1):
            slot_to_path[i] = self._touch(os.path.join(self.frames_dir, f'img_{i:03d}.webp'))
        return slot_to_path


class TestPlanVideoSlots(_TmpDirCase):
    VIDEOS = {1: 'move from IMAGE 1 to IMAGE 2',
              2: {'body': 'move from IMAGE 2 to IMAGE 3'},
              3: 'move from IMAGE 3 to IMAGE 4'}

    def test_full_run_generates_all(self):
        frames = self._make_frames(4)
        plans = plan_video_slots(self.VIDEOS, frames, {}, self.videos_dir)
        self.assertEqual([p['action'] for p in plans], ['generate'] * 3)
        self.assertEqual([p['slot'] for p in plans], [1, 2, 3])
        self.assertEqual([p['seq'] for p in plans], [1, 2, 3])
        # 帧配对：slot i 用 frame i / i+1
        self.assertEqual(plans[1]['start_frame'], frames[2])
        self.assertEqual(plans[1]['end_frame'], frames[3])
        # dict 形式的槽位体（{'body':...}）被解包为纯文本
        self.assertEqual(plans[1]['prompt'], 'move from IMAGE 1 to IMAGE 2')

    def test_breakpoint_resume_reuses_existing_video(self):
        frames = self._make_frames(4)
        self._touch(os.path.join(self.videos_dir, 'vid_002.mp4'))
        plans = plan_video_slots(self.VIDEOS, frames, {}, self.videos_dir)
        self.assertEqual([p['action'] for p in plans], ['generate', 'reuse', 'generate'])

    def test_empty_existing_video_is_not_reused(self):
        frames = self._make_frames(4)
        self._touch(os.path.join(self.videos_dir, 'vid_002.mp4'), content=b'')
        plans = plan_video_slots(self.VIDEOS, frames, {}, self.videos_dir)
        self.assertEqual(plans[1]['action'], 'generate')

    def test_explicit_retry_targets_and_marks_delete(self):
        frames = self._make_frames(4)
        self._touch(os.path.join(self.videos_dir, 'vid_002.mp4'))
        plans = plan_video_slots(self.VIDEOS, frames, {}, self.videos_dir, target_slots=['2'])
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0]['slot'], 2)
        self.assertEqual(plans[0]['action'], 'generate')
        self.assertTrue(plans[0]['delete_existing'])

    def test_missing_end_frame_blocks(self):
        frames = self._make_frames(3)  # 缺 frame 4 → slot 3 无尾帧
        plans = plan_video_slots(self.VIDEOS, frames, {}, self.videos_dir)
        self.assertEqual(plans[2]['action'], 'blocked')
        self.assertIn('结束帧 IMAGE 4', plans[2]['reason'])

    def test_degraded_frame_blocks_both_adjacent_slots(self):
        frames = self._make_frames(4)
        quality = {3: 'i2i_fallback_degraded'}
        plans = plan_video_slots(self.VIDEOS, frames, quality, self.videos_dir)
        # frame 3 降级 → slot 2（尾帧）和 slot 3（起始帧）都拦截
        self.assertEqual([p['action'] for p in plans], ['generate', 'blocked', 'blocked'])
        self.assertIn('降级帧', plans[1]['reason'])


class TestLoadSlotFrames(_TmpDirCase):
    def test_fallback_guesses_by_naming_convention(self):
        self._make_frames(2)
        slot_to_path, quality = load_slot_frames({}, self.frames_dir, 3)
        self.assertEqual(sorted(slot_to_path.keys()), [1, 2])
        self.assertEqual(quality, {})

    def test_manifest_frames_take_precedence(self):
        manifest = {'frames': [
            {'slot': 1, 'file': '/outputs/p/frames/img_001.webp', 'quality_gate': None},
            {'slot': 2, 'file': '/outputs/p/frames/img_002.webp', 'quality_gate': 'i2i_fallback_degraded'},
        ]}
        slot_to_path, quality = load_slot_frames(manifest, self.frames_dir, 2)
        self.assertEqual(sorted(slot_to_path.keys()), [1, 2])
        self.assertEqual(quality[2], 'i2i_fallback_degraded')
        self.assertTrue(slot_to_path[1].replace('\\', '/').endswith('outputs/p/frames/img_001.webp'))


class TestManifestWriter(_TmpDirCase):
    def test_incremental_merge_keeps_other_slots_and_orders(self):
        manifest_path = os.path.join(self.tmp, 'manifest.json')
        data = {'videos': [
            {'slot': 1, 'status': 'success', 'file': 'old1'},
            {'slot': 3, 'status': 'failed', 'file': ''},
        ]}
        writer = _ManifestWriter(manifest_path, data, all_slots=[1, 2, 3])
        writer.record({'slot': 3, 'status': 'success', 'file': 'new3'})
        writer.record({'slot': 2, 'status': 'success', 'file': 'new2'})

        with open(manifest_path, 'r', encoding='utf-8') as f:
            on_disk = json.load(f)
        slots = [v['slot'] for v in on_disk['videos']]
        self.assertEqual(slots, [1, 2, 3])                      # 槽位升序
        by_slot = {v['slot']: v for v in on_disk['videos']}
        self.assertEqual(by_slot[1]['file'], 'old1')            # 未触碰的槽位保留
        self.assertEqual(by_slot[3]['file'], 'new3')            # 后写覆盖先写
        self.assertEqual(by_slot[2]['file'], 'new2')

    def test_same_slot_last_write_wins(self):
        manifest_path = os.path.join(self.tmp, 'manifest.json')
        writer = _ManifestWriter(manifest_path, {}, all_slots=[1])
        writer.record({'slot': 1, 'status': 'failed', 'file': '', 'error': 'x'})
        writer.record({'slot': 1, 'status': 'success', 'file': 'v1'})
        with open(manifest_path, 'r', encoding='utf-8') as f:
            on_disk = json.load(f)
        self.assertEqual(len(on_disk['videos']), 1)
        self.assertEqual(on_disk['videos'][0]['status'], 'success')


class TestVideoInfo(unittest.TestCase):
    PLAN = {'slot': 2, 'seq': 1, 'prompt': 'p', 'dest_path': os.path.join(os.getcwd(), 'x.mp4')}

    def test_failed_info_has_empty_paths_and_error(self):
        info = _video_info(self.PLAN, 'veo', status='failed', error='boom')
        self.assertEqual(info['file'], '')
        self.assertEqual(info['url'], '')
        self.assertEqual(info['status'], 'failed')
        self.assertEqual(info['error'], 'boom')
        self.assertEqual(info['slot'], 2)
        self.assertEqual(info['sequence'], 1)

    def test_success_info_has_relative_url(self):
        info = _video_info(self.PLAN, 'veo', status='success')
        self.assertTrue(info['url'].startswith('/'))
        self.assertNotIn('error', info)


class TestVideoProgressEvents(unittest.TestCase):
    def test_batch_bridge_keeps_slot_and_sequence_fields_distinct(self):
        records = []
        events = []

        class Writer:
            def record(self, info):
                records.append(info)

        plan = {
            'slot': 7,
            'seq': 2,
            'prompt': 'p',
            'dest_path': os.path.join(os.getcwd(), 'vid_007.mp4'),
        }
        bridge = _BatchBridge(
            pending=[{'plan': plan}],
            total=5,
            video_model='veo',
            writer=Writer(),
            on_progress=lambda stage, payload: events.append((stage, payload)),
        )

        bridge(0, 'video_start', {})
        bridge(0, 'video_error', {'message': 'boom'})

        start = events[0]
        self.assertEqual(start[0], 'video_start')
        self.assertEqual(start[1]['index'], 7)
        self.assertEqual(start[1]['current'], 2)
        self.assertEqual(start[1]['total'], 5)

        error = events[1]
        self.assertEqual(error[0], 'video_error')
        self.assertEqual(error[1]['index'], 7)
        self.assertEqual(error[1]['current'], 2)
        self.assertEqual(error[1]['total'], 5)
        self.assertEqual(error[1]['message'], 'boom')
        self.assertEqual(records[0]['slot'], 7)


if __name__ == '__main__':
    unittest.main()
