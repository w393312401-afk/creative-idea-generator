import json
import os
import shutil
import tempfile
import unittest

from video_generator import (
    rewrite_prompt_for_two_card_ui,
    load_slot_frames,
    load_drift_break_slots,
    plan_video_slots,
    _ManifestWriter,
    _video_info,
    _BatchBridge,
)


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
        # 默认 verify_fn=None -> 真实 verify_video_anchors；对着 _touch() 出的假 mp4
        # 提不出真帧，非严格模式下按 fail-open 处理，仍旧复用（不依赖真实 ffmpeg 环境）。
        frames = self._make_frames(4)
        self._touch(os.path.join(self.videos_dir, 'vid_002.mp4'))
        plans = plan_video_slots(self.VIDEOS, frames, {}, self.videos_dir)
        self.assertEqual([p['action'] for p in plans], ['generate', 'reuse', 'generate'])

    def test_empty_existing_video_is_not_reused(self):
        frames = self._make_frames(4)
        self._touch(os.path.join(self.videos_dir, 'vid_002.mp4'), content=b'')
        plans = plan_video_slots(self.VIDEOS, frames, {}, self.videos_dir)
        self.assertEqual(plans[1]['action'], 'generate')

    def test_stale_video_is_not_reused_and_gets_deleted(self):
        # 起止帧在上一轮失败后被单独重渲，旧片段这时已过期：verify_fn 判定不符时
        # 不得复用，应转入重新生成并标记删除旧文件（呼应 spark-video-mixup-postmortem）。
        frames = self._make_frames(4)
        self._touch(os.path.join(self.videos_dir, 'vid_002.mp4'))
        plans = plan_video_slots(self.VIDEOS, frames, {}, self.videos_dir,
                                  verify_fn=lambda *a, **k: (False, 'mismatch'))
        self.assertEqual(plans[1]['action'], 'generate')
        self.assertTrue(plans[1]['delete_existing'])
        self.assertIn('mismatch', plans[1]['reason'])

    def test_reuse_check_passes_dest_start_end_and_strict(self):
        frames = self._make_frames(4)
        dest = self._touch(os.path.join(self.videos_dir, 'vid_002.mp4'))
        calls = []

        def fake_verify(video_path, start, end, strict=False):
            calls.append((video_path, start, end, strict))
            return True, 'ok'

        plans = plan_video_slots(self.VIDEOS, frames, {}, self.videos_dir,
                                  strict=True, verify_fn=fake_verify)
        self.assertEqual(plans[1]['action'], 'reuse')
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], (dest, frames[2], frames[3], True))

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

    def test_vlm_qa_failed_frame_blocks_pairing(self):
        # 2026-07-12 前该缺陷曾让 16/16 视频盖着 4 张已知坏帧生成
        frames = self._make_frames(4)
        quality = {3: 'vlm_qa_failed'}
        plans = plan_video_slots(self.VIDEOS, frames, quality, self.videos_dir,
                                  gate_level='standard')
        self.assertEqual([p['action'] for p in plans], ['generate', 'blocked', 'blocked'])
        self.assertIn('vlm_qa_failed', plans[1]['reason'])
        # lenient 档同样拦（该档下 vlm_qa_failed 只会由硬伤产生）
        plans = plan_video_slots(self.VIDEOS, frames, quality, self.videos_dir,
                                  gate_level='lenient')
        self.assertEqual(plans[1]['action'], 'blocked')
        # off 档放行（质检整体停用时不做事后拦截）
        plans = plan_video_slots(self.VIDEOS, frames, quality, self.videos_dir,
                                  gate_level='off')
        self.assertEqual([p['action'] for p in plans], ['generate'] * 3)

    def test_stale_lineage_blocks_standard_warns_lenient(self):
        frames = self._make_frames(4)
        stale = {3}
        plans = plan_video_slots(self.VIDEOS, frames, {}, self.videos_dir,
                                  gate_level='standard', stale_slots=stale)
        self.assertEqual([p['action'] for p in plans], ['generate', 'blocked', 'blocked'])
        self.assertIn('血统过期', plans[1]['reason'])
        plans = plan_video_slots(self.VIDEOS, frames, {}, self.videos_dir,
                                  gate_level='lenient', stale_slots=stale)
        self.assertEqual([p['action'] for p in plans], ['generate'] * 3)
        self.assertIn('旧 i2i 链', plans[1]['warning'])
        self.assertNotIn('warning', plans[0])

    def test_chain_drift_break_blocks_standard_warns_lenient(self):
        """链回望 FAIL 段（manifest.chain_drift passed=False）覆盖的槽位：standard 拦、
        lenient 警告放行、off 放行。2026-07-15 盐湖贝壳单 anchor=6/tail=9 FAIL 被无视，
        vid_006 在室外/室内两张无关帧之间自由变形——此门即为该事故补的。"""
        frames = self._make_frames(4)
        drift = {2, 3}  # 模拟 anchor=2 / tail=4 的 FAIL 段
        plans = plan_video_slots(self.VIDEOS, frames, {}, self.videos_dir,
                                  gate_level='standard', drift_slots=drift)
        self.assertEqual([p['action'] for p in plans], ['generate', 'blocked', 'blocked'])
        self.assertIn('空间断裂', plans[1]['reason'])
        plans = plan_video_slots(self.VIDEOS, frames, {}, self.videos_dir,
                                  gate_level='lenient', drift_slots=drift)
        self.assertEqual([p['action'] for p in plans], ['generate'] * 3)
        self.assertIn('空间断裂', plans[1]['warning'])
        self.assertNotIn('warning', plans[0])
        plans = plan_video_slots(self.VIDEOS, frames, {}, self.videos_dir,
                                  gate_level='off', drift_slots=drift)
        self.assertEqual([p['action'] for p in plans], ['generate'] * 3)

    def test_stale_and_drift_warnings_are_both_kept_on_lenient(self):
        frames = self._make_frames(4)
        plans = plan_video_slots(self.VIDEOS, frames, {}, self.videos_dir,
                                  gate_level='lenient', stale_slots={2}, drift_slots={2})
        w = plans[1]['warning']
        self.assertIn('旧 i2i 链', w)
        self.assertIn('空间断裂', w)


class TestLoadDriftBreakSlots(unittest.TestCase):
    """manifest.chain_drift FAIL 段 → 受影响视频槽位（anchor..tail-1）。"""

    def test_failed_entry_covers_anchor_to_tail_minus_one(self):
        manifest = {'chain_drift': [
            {'family_anchor': 1, 'mid': 3, 'tail': 4, 'passed': True, 'reason': 'PASS'},
            {'family_anchor': 6, 'mid': 8, 'tail': 9, 'passed': False, 'reason': 'FAIL: 断裂'},
        ]}
        # 2026-07-15 实案：anchor=6/tail=9 FAIL → vid 6/7/8 都可能横跨断裂
        self.assertEqual(load_drift_break_slots(manifest), {6, 7, 8})

    def test_passed_and_malformed_entries_are_ignored(self):
        manifest = {'chain_drift': [
            {'family_anchor': 1, 'tail': 4, 'passed': True},
            {'family_anchor': 'x', 'tail': 9, 'passed': False},
            {'tail': 9, 'passed': False},
            'not a dict',
        ]}
        self.assertEqual(load_drift_break_slots(manifest), set())

    def test_missing_chain_drift_key_or_empty_manifest(self):
        self.assertEqual(load_drift_break_slots({}), set())
        self.assertEqual(load_drift_break_slots(None), set())


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


class TestStaleLineage(_TmpDirCase):
    """部分重生帧的 i2i 血统标记：frame_generator.update_manifest_stale_status 写、
    video_generator.load_stale_slots 读。"""

    def _manifest(self, n):
        return {'frames': [
            {'slot': i, 'sequence': i, 'file': f'/outputs/p/frames/img_{i:03d}.webp'}
            for i in range(1, n + 1)
        ]}

    def test_partial_regen_marks_downstream(self):
        from frame_generator import update_manifest_stale_status
        from video_generator import load_stale_slots
        manifest = self._manifest(6)
        update_manifest_stale_status(manifest, self.tmp, regenerated_sequences=[3], finalize=True)
        self.assertEqual(load_stale_slots(manifest), {4, 5, 6})
        # 上游帧（1、2）与被重生帧本身（3）不受影响
        self.assertNotIn('stale_lineage', manifest['frames'][0])
        self.assertNotIn('stale_lineage', manifest['frames'][2])

    def test_full_regen_clears_all_marks(self):
        from frame_generator import update_manifest_stale_status
        from video_generator import load_stale_slots
        manifest = self._manifest(4)
        update_manifest_stale_status(manifest, self.tmp, regenerated_sequences=[2], finalize=True)
        self.assertEqual(load_stale_slots(manifest), {3, 4})
        update_manifest_stale_status(manifest, self.tmp, regenerated_sequences=None, finalize=True)
        self.assertEqual(load_stale_slots(manifest), set())

    def test_downstream_regen_clears_its_own_mark(self):
        from frame_generator import update_manifest_stale_status
        from video_generator import load_stale_slots
        manifest = self._manifest(5)
        update_manifest_stale_status(manifest, self.tmp, regenerated_sequences=[2], finalize=True)
        self.assertEqual(load_stale_slots(manifest), {3, 4, 5})
        # 用户顺序重渲 3-5 → 全部清除
        update_manifest_stale_status(manifest, self.tmp, regenerated_sequences=[3, 4, 5], finalize=True)
        self.assertEqual(load_stale_slots(manifest), set())

    def test_non_finalize_call_keeps_old_behavior(self):
        from frame_generator import update_manifest_stale_status
        manifest = dict(self._manifest(3), merged_video='x', videos=[{'slot': 1}])
        update_manifest_stale_status(manifest, self.tmp)
        self.assertNotIn('merged_video', manifest)
        self.assertEqual(manifest['videos'], [])
        self.assertTrue(all('stale_lineage' not in f for f in manifest['frames']))


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
