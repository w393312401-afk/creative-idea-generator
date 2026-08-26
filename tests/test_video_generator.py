import json
import os
import shutil
import tempfile
import unittest

from unittest.mock import patch

from video_generator import (
    rewrite_prompt_for_two_card_ui,
    load_slot_frames,
    plan_video_slots,
    _ManifestWriter,
    _video_info,
    _BatchBridge,
    _merge_skip_missing,
    _normalize_merge_speed,
    _merge_filter,
    merge_project_videos,
    PartialMergeBlocked,
    _select_pool_account,
    _account_rotation_ring,
    _account_switch_interval,
    _next_unused_account,
    plan_generation_legs,
)

from datetime import datetime, timedelta


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

    def test_frame_continuity_failed_blocks_both_adjacent_videos(self):
        frames = self._make_frames(4)
        quality = {3: 'frame_continuity_failed'}
        plans = plan_video_slots(self.VIDEOS, frames, quality, self.videos_dir,
                                 gate_level='standard')
        self.assertEqual([p['action'] for p in plans], ['generate', 'blocked', 'blocked'])
        self.assertIn('场景连续性检查失败', plans[1]['reason'])
        off_plans = plan_video_slots(self.VIDEOS, frames, quality, self.videos_dir,
                                     gate_level='off')
        self.assertEqual([p['action'] for p in off_plans], ['generate', 'blocked', 'blocked'])

    def test_override_flagged_bypasses_sequence_review_block(self):
        """2026-07-23：前端"确认风险，强制生成"必须真正让后端放行——此前 UI 弹窗确认
        了也没用，plan_video_slots 仍按 quality_gate 硬拦，等于白问用户一遍。"""
        frames = self._make_frames(4)
        quality = {3: 'sequence_review_flagged'}
        plans = plan_video_slots(self.VIDEOS, frames, quality, self.videos_dir,
                                  gate_level='standard', override_flagged=True)
        self.assertEqual([p['action'] for p in plans], ['generate'] * 3)
        self.assertIn('确认风险强制生成', plans[1]['warning'])
        self.assertNotIn('warning', plans[0])
        # override_flagged=False（默认）仍照旧拦截，不能悄悄放宽默认行为
        plans = plan_video_slots(self.VIDEOS, frames, quality, self.videos_dir,
                                  gate_level='standard')
        self.assertEqual([p['action'] for p in plans], ['generate', 'blocked', 'blocked'])
        # 降级帧（i2i_fallback_degraded）等其它独立门禁不受 override_flagged 影响
        quality2 = {3: 'i2i_fallback_degraded'}
        plans = plan_video_slots(self.VIDEOS, frames, quality2, self.videos_dir,
                                  gate_level='standard', override_flagged=True)
        self.assertEqual([p['action'] for p in plans], ['generate', 'blocked', 'blocked'])

    def test_manually_flagged_frame_blocks_like_review_flagged(self):
        """人工主动描述过问题、还没修的帧（manual_flagged，见
        pipeline_orchestrator.set_manual_frame_issue）：与机器判定的坏帧同等对待，
        不烧视频生成额度；用户确认风险后可 override 放行。"""
        frames = self._make_frames(4)
        quality = {3: 'manual_flagged'}
        plans = plan_video_slots(self.VIDEOS, frames, quality, self.videos_dir,
                                  gate_level='standard')
        self.assertEqual([p['action'] for p in plans], ['generate', 'blocked', 'blocked'])
        self.assertIn('被人工标记存在问题', plans[1]['reason'])
        # off 档整体停用质检时不做事后拦截，与其它 gate 一致
        plans = plan_video_slots(self.VIDEOS, frames, quality, self.videos_dir,
                                  gate_level='off')
        self.assertEqual([p['action'] for p in plans], ['generate'] * 3)
        # 确认风险后放行，但带警告
        plans = plan_video_slots(self.VIDEOS, frames, quality, self.videos_dir,
                                  gate_level='standard', override_flagged=True)
        self.assertEqual([p['action'] for p in plans], ['generate'] * 3)
        self.assertIn('被人工标记存在问题', plans[1]['warning'])

    def test_stale_lineage_blocks_unless_gate_off(self):
        """2026-07-30：血统过期从"standard 拦 / lenient 警告"改成"除 off 档一律拦"。

        它和其它门禁不同类——一致性审查的判定有主观成分（宽松档放行是合理档位），
        血统过期是确定性事实：上游帧被单独换过，这一对锚点帧确实来自两条不同的 i2i
        链。lenient 档那条警告会和其它十几条混在一起滚过去，实际等于没拦（2026-07-27
        ice_cave slot3 事故就是这么走到成片的）。要放行走 override_flagged 或 off 档。
        """
        frames = self._make_frames(4)
        stale = {3}
        for level in ('standard', 'lenient'):
            plans = plan_video_slots(self.VIDEOS, frames, {}, self.videos_dir,
                                      gate_level=level, stale_slots=stale)
            self.assertEqual([p['action'] for p in plans],
                             ['generate', 'blocked', 'blocked'], level)
            self.assertIn('血统过期', plans[1]['reason'])
        # off = 整体停用质检，与其它门禁一致地不做事后拦截
        plans = plan_video_slots(self.VIDEOS, frames, {}, self.videos_dir,
                                  gate_level='off', stale_slots=stale)
        self.assertEqual([p['action'] for p in plans], ['generate'] * 3)
        self.assertIn('旧 i2i 链', plans[1]['warning'])
        self.assertNotIn('warning', plans[0])

    def test_override_flagged_bypasses_stale_lineage_block(self):
        """2026-07-24 修复：override_flagged 此前只豁免 vlm_qa_failed/
        sequence_review_flagged 这一道硬拦，血统过期这道独立门禁仍照旧拦截——
        用户在前端确认过风险，标准档下受影响的帧仍然不会被提交生成（不会上传
        到视频后端），等于确认没有全部生效。"""
        frames = self._make_frames(4)
        stale = {3}
        plans = plan_video_slots(self.VIDEOS, frames, {}, self.videos_dir,
                                  gate_level='standard', stale_slots=stale, override_flagged=True)
        self.assertEqual([p['action'] for p in plans], ['generate'] * 3)
        self.assertIn('旧 i2i 链', plans[1]['warning'])
        self.assertNotIn('warning', plans[0])

    def test_unreviewed_anchor_is_warned_about_not_blocked(self):
        """渲染期不再产生任何判定，帧默认停在 pending_manual_review。这不是坏帧，
        不该拦；但花视频额度那一刻必须把"这张没人看过"说出来。"""
        frames = self._make_frames(4)
        quality = {s: 'pending_manual_review' for s in (1, 2, 3, 4)}
        plans = plan_video_slots(self.VIDEOS, frames, quality, self.videos_dir,
                                  gate_level='standard')
        self.assertEqual([p['action'] for p in plans], ['generate'] * 3)
        self.assertIn('未经过一致性审查', plans[0]['warning'])


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
        # sequence 跟 slot 走，不再是批内序号（plan['seq'] 这里是 1）
        self.assertEqual(info['sequence'], 2)

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

    def test_explicit_override_keeps_anchor_mismatch_with_warning(self):
        records = []
        events = []

        class Writer:
            def record(self, info):
                records.append(info)

        with tempfile.TemporaryDirectory() as tmp:
            generated = os.path.join(tmp, 'generated.mp4')
            destination = os.path.join(tmp, 'vid_008.mp4')
            with open(generated, 'wb') as f:
                f.write(b'video')
            plan = {
                'slot': 8, 'seq': 1, 'prompt': 'p', 'dest_path': destination,
                'start_frame': os.path.join(tmp, 'img_008.webp'),
                'end_frame': os.path.join(tmp, 'img_009.webp'),
            }
            bridge = _BatchBridge(
                pending=[{'plan': plan}], total=1, video_model='omni',
                writer=Writer(),
                on_progress=lambda stage, payload: events.append((stage, payload)),
                allow_anchor_mismatch=True,
            )
            with patch('video_generator.verify_video_anchors',
                       return_value=(False, 'first=26.0, last=32.6')):
                result = bridge(0, 'video_done', {'video_url': generated})

            self.assertIsNone(result)
            self.assertTrue(os.path.exists(destination))
            self.assertEqual(records[0]['status'], 'success')
            self.assertTrue(records[0]['anchor_mismatch_overridden'])
            self.assertTrue(any(stage == 'video_warning' for stage, _ in events))

    def test_anchor_rejection_adapts_prompt_before_retry(self):
        records = []
        events = []

        class Writer:
            def record(self, info):
                records.append(info)

        class Req:
            prompt = 'Build the cabinets evenly.'

        with tempfile.TemporaryDirectory() as tmp:
            generated = os.path.join(tmp, 'generated.mp4')
            destination = os.path.join(tmp, 'vid_011.mp4')
            with open(generated, 'wb') as f:
                f.write(b'video')
            req = Req()
            plan = {
                'slot': 11, 'seq': 1, 'prompt': req.prompt, 'dest_path': destination,
                'start_frame': os.path.join(tmp, 'img_011.webp'),
                'end_frame': os.path.join(tmp, 'img_012.webp'),
            }
            bridge = _BatchBridge(
                pending=[{'plan': plan, 'req': req}], total=1, video_model='omni',
                writer=Writer(),
                on_progress=lambda stage, payload: events.append((stage, payload)),
            )
            with patch('video_generator.verify_video_anchors',
                       return_value=(False, 'first=4.3, last=27.3')):
                result = bridge(0, 'video_done', {'video_url': generated})

            self.assertEqual(result, 'rejected')
            self.assertIn('ANCHOR_RETRY_ADAPTATION:', req.prompt)
            self.assertIn('last-frame geometry', req.prompt)
            self.assertEqual(bridge.stats['anchor_rejections'], 1)
            self.assertEqual(bridge.stats['adaptive_retries'], 1)
            self.assertTrue(any(stage == 'video_retry_adapted' for stage, _ in events))


class TestMergeSkipMissing(unittest.TestCase):
    """2026-07-22 改版：强制合并不再用起始锚点帧定格+「缺失」标注填充缺口，改成直接
    跳过缺失/串片槽位、只拼接可用片段、2x 加速（用户要的"视频不全也能合成"）。同时
    覆盖前一天踩到的 bug：project_dir 本身是相对路径（server_common.OUTPUT_ROOT=
    'outputs'），output_path 必须是绝对路径，不能依赖 ffmpeg 子进程的 cwd。"""

    def setUp(self):
        self.base = tempfile.mkdtemp()
        self.orig_cwd = os.getcwd()
        os.chdir(self.base)
        # 相对路径项目目录，复刻 server_common._get_project_dir 的真实返回形态
        self.rel_project_dir = os.path.join('outputs', 'test_project')
        os.makedirs(os.path.join(self.rel_project_dir, 'videos'))
        self.good_video = os.path.abspath(
            os.path.join(self.rel_project_dir, 'videos', 'vid_001.mp4'))
        with open(self.good_video, 'wb') as f:
            f.write(b'fake')

    def tearDown(self):
        os.chdir(self.orig_cwd)
        shutil.rmtree(self.base, ignore_errors=True)

    def _fake_run(self, captured):
        def fake_run(cmd, cwd=None, **kwargs):
            captured.setdefault('calls', []).append(cmd)
            if cmd[0] == 'ffprobe':
                class Probe:
                    returncode = 0
                    stderr = ''
                    stdout = '5.0'
                return Probe()
            # 主合并调用：复刻真实 ffmpeg 行为——相对路径参数按子进程 cwd 解析，
            # 目标目录不存在就报错，不会自动创建目录
            out_arg = cmd[-1]
            resolved = out_arg if os.path.isabs(out_arg) else os.path.normpath(
                os.path.join(cwd or os.getcwd(), out_arg))
            if not os.path.isdir(os.path.dirname(resolved)):
                class Fail:
                    returncode = 1
                    stderr = f'Error opening output {out_arg}: No such file or directory'
                return Fail()
            with open(resolved, 'wb') as f:
                f.write(b'fake-mp4')
            class Ok:
                returncode = 0
                stderr = ''
            return Ok()
        return fake_run

    def test_skips_missing_slot_and_output_path_is_absolute(self):
        captured = {}
        with patch('video_generator.subprocess.run', side_effect=self._fake_run(captured)):
            result = _merge_skip_missing(
                self.rel_project_dir, {'title': 'Test Project'},
                expected_slots=[1, 2], good={1: self.good_video},
                missing=[2], mismatched=[])

        self.assertIsNotNone(result)
        self.assertEqual(result['status'], 'success')
        self.assertTrue(result['partial'])
        self.assertEqual(result['skipped_slots'], [2])
        ffmpeg_cmd = next(c for c in captured['calls'] if c[0] == 'ffmpeg')
        self.assertTrue(os.path.isabs(ffmpeg_cmd[-1]),
                        f"ffmpeg output arg must be absolute: {ffmpeg_cmd[-1]}")
        # 跳过模式不应再生成任何占位/字幕相关的临时文件
        self.assertFalse(any('_ph_' in c for call in captured['calls'] for c in call))

    def test_applies_selected_1_5x_speed(self):
        captured = {}
        with patch('video_generator.subprocess.run', side_effect=self._fake_run(captured)):
            result = _merge_skip_missing(
                self.rel_project_dir, {'title': 'Test Project'},
                expected_slots=[1, 2], good={1: self.good_video},
                missing=[2], mismatched=[], speed=1.5)

        ffmpeg_cmd = next(c for c in captured['calls'] if c[0] == 'ffmpeg')
        filter_value = ffmpeg_cmd[ffmpeg_cmd.index('-filter_complex') + 1]
        self.assertIn('setpts=0.6666666667*PTS', filter_value)
        self.assertTrue(ffmpeg_cmd[-1].endswith('_partial_1_5x.mp4'))
        self.assertEqual(result['speed'], 1.5)

    def test_merge_speed_validation_and_filters(self):
        self.assertEqual(_normalize_merge_speed('1'), 1.0)
        self.assertEqual(_normalize_merge_speed(1.5), 1.5)
        self.assertEqual(_normalize_merge_speed(2), 2.0)
        self.assertEqual(_merge_filter(1.0, True),
                         '[0:v]setpts=1*PTS[v];[0:a]atempo=1[a]')
        with self.assertRaises(ValueError):
            _normalize_merge_speed(3)

    def test_no_good_slots_returns_none(self):
        result = _merge_skip_missing(
            self.rel_project_dir, {'title': 'Test Project'},
            expected_slots=[1, 2], good={}, missing=[1, 2], mismatched=[])
        self.assertIsNone(result)

    def test_merge_project_videos_raises_partial_merge_blocked(self):
        """当视频片段不全且 allow_partial=False 时，必须拦截并抛出 PartialMergeBlocked。"""
        manifest = {
            'title': 'Partial Video Test',
            'frames': [
                {'slot': 1, 'sequence': 1, 'file': 'frames/img_001.webp'},
                {'slot': 2, 'sequence': 2, 'file': 'frames/img_002.webp'},
                {'slot': 3, 'sequence': 3, 'file': 'frames/img_003.webp'},
            ],
            'videos': [
                {'slot': 1, 'status': 'success', 'file': 'videos/vid_001.mp4'},
                # slot 2 is missing / failed
            ]
        }
        with open(os.path.join(self.rel_project_dir, 'manifest.json'), 'w', encoding='utf-8') as f:
            json.dump(manifest, f)

        # 默认 allow_partial=False 抛出异常
        with self.assertRaises(PartialMergeBlocked) as ctx:
            merge_project_videos(self.rel_project_dir, allow_partial=False)
        self.assertIn(2, ctx.exception.missing)

        # allow_partial=True 时跳过缺失槽位合并
        captured = {}
        with patch('video_generator.subprocess.run', side_effect=self._fake_run(captured)):
            result = merge_project_videos(self.rel_project_dir, allow_partial=True)
            self.assertIsNotNone(result)
            self.assertTrue(result.get('partial'))
            self.assertEqual(result.get('skipped_slots'), [2])


class TestMergeManualUploadTrust(unittest.TestCase):
    """2026-07-23 修复：手动上传（/api/upload_video）落盘前已经做过 verify_video_anchors
    校验，且首尾帧不符时要求用户显式 force=true 确认覆盖——merge_project_videos 之前
    对所有槽位一视同仁地重跑同一套锚点校验，等于把用户已经确认过的 force 覆盖再拦一次
    （'合成识别不了手动上传的视频'）。手动上传的槽位现在直接信任，不重新验锚点；
    自动生成（非 manual_upload）的槽位仍然照常校验，安全网没有被整体拆掉。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.videos_dir = os.path.join(self.tmp, 'videos')
        self.frames_dir = os.path.join(self.tmp, 'frames')
        os.makedirs(self.videos_dir)
        os.makedirs(self.frames_dir)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _touch(self, path):
        with open(path, 'wb') as f:
            f.write(b'x')
        return path

    def _write_manifest(self, frames_n, videos_entries, title='Manual Upload Trust Test'):
        frames = []
        for i in range(1, frames_n + 1):
            frame_path = os.path.join(self.frames_dir, f'img_{i:03d}.webp')
            self._touch(frame_path)
            frames.append({'slot': i, 'sequence': i,
                           'file': os.path.relpath(frame_path, self.tmp).replace('\\', '/')})
        manifest = {'title': title, 'frames': frames, 'videos': videos_entries}
        with open(os.path.join(self.tmp, 'manifest.json'), 'w', encoding='utf-8') as f:
            json.dump(manifest, f)

    def _fake_run_factory(self, captured):
        def fake_run(cmd, cwd=None, **kwargs):
            captured.setdefault('calls', []).append(cmd)
            if cmd[0] == 'ffprobe':
                class Probe:
                    returncode = 0
                    stderr = ''
                    stdout = '5.0'
                return Probe()
            i_idx = cmd.index('-i')
            with open(cmd[i_idx + 1], 'r', encoding='utf-8') as f:
                captured['concat_list'] = f.read()
            out_path = cmd[-1]
            os.makedirs(os.path.dirname(out_path) or '.', exist_ok=True)
            with open(out_path, 'wb') as f:
                f.write(b'fake-merged-mp4')

            class Ok:
                returncode = 0
                stderr = ''
            return Ok()
        return fake_run

    def test_manual_upload_slot_bypasses_anchor_mismatch(self):
        self._touch(os.path.join(self.videos_dir, 'vid_001.mp4'))
        self._write_manifest(2, [
            {'slot': 1, 'status': 'success', 'file': 'videos/vid_001.mp4',
             'start_anchor_slot': 1, 'source': 'manual_upload'},
        ])
        captured = {}
        # 即便锚点校验会判定不匹配（模拟用户已经 force 确认过的场景），手动上传的
        # 槽位也不应该被 merge 再次拦下。
        with patch('video_generator.verify_video_anchors', return_value=(False, 'forced mismatch')), \
             patch('video_generator.subprocess.run', side_effect=self._fake_run_factory(captured)):
            result = merge_project_videos(self.tmp)

        self.assertIsNotNone(result)
        self.assertEqual(result['status'], 'success')
        concat_lines = [l for l in captured['concat_list'].splitlines() if l.strip()]
        self.assertEqual(len(concat_lines), 1)

    def test_explicit_generated_override_bypasses_merge_recheck(self):
        self._touch(os.path.join(self.videos_dir, 'vid_001.mp4'))
        self._write_manifest(2, [
            {'slot': 1, 'status': 'success', 'file': 'videos/vid_001.mp4',
             'start_anchor_slot': 1, 'model': 'omni',
             'anchor_mismatch_overridden': True},
        ])
        captured = {}
        with patch('video_generator.verify_video_anchors', return_value=(False, 'forced mismatch')), \
             patch('video_generator.subprocess.run', side_effect=self._fake_run_factory(captured)):
            result = merge_project_videos(self.tmp)

        self.assertIsNotNone(result)
        self.assertEqual(result['status'], 'success')
        concat_lines = [l for l in captured['concat_list'].splitlines() if l.strip()]
        self.assertEqual(len(concat_lines), 1)

    def test_auto_generated_slot_still_blocked_on_anchor_mismatch(self):
        """非手动上传的槽位锚点不符时留警告日志，仍按合并容错策略并入成片。"""
        self._touch(os.path.join(self.videos_dir, 'vid_001.mp4'))
        self._write_manifest(2, [
            {'slot': 1, 'status': 'success', 'file': 'videos/vid_001.mp4',
             'start_anchor_slot': 1, 'model': 'i2v'},
        ])
        captured = {}
        with patch('video_generator.verify_video_anchors', return_value=(False, 'real mismatch')), \
             patch('video_generator.subprocess.run', side_effect=self._fake_run_factory(captured)):
            result = merge_project_videos(self.tmp)
        self.assertIsNotNone(result)
        self.assertEqual(result['status'], 'success')
        concat_lines = [l for l in captured['concat_list'].splitlines() if l.strip()]
        self.assertEqual(len(concat_lines), 1)

    def test_manual_upload_hero_clip_bypasses_anchor_mismatch(self):
        self._touch(os.path.join(self.videos_dir, 'vid_001.mp4'))
        self._touch(os.path.join(self.videos_dir, 'vid_002.mp4'))
        self._write_manifest(2, [
            {'slot': 1, 'status': 'success', 'file': 'videos/vid_001.mp4', 'start_anchor_slot': 1},
            {'slot': 2, 'status': 'success', 'file': 'videos/vid_002.mp4', 'start_anchor_slot': 2,
             'meta': 'HERO', 'is_hero': True, 'source': 'manual_upload'},
        ])
        captured = {}
        # 只让"英雄片段"这一路锚点判定为不匹配，验证正片槽位（非手动上传）走的仍是
        # 真实校验结果、没有被这次修复连带放松。
        def fake_verify(video_path, start_frame_path, end_frame_path, strict=False):
            if 'vid_002' in video_path:
                return False, 'forced mismatch'
            return True, 'ok'
        with patch('video_generator.verify_video_anchors', side_effect=fake_verify), \
             patch('video_generator.subprocess.run', side_effect=self._fake_run_factory(captured)):
            result = merge_project_videos(self.tmp)

        self.assertIsNotNone(result)
        self.assertEqual(result['status'], 'success')
        concat_lines = [l for l in captured['concat_list'].splitlines() if l.strip()]
        self.assertEqual(len(concat_lines), 2, "手动上传的英雄片段应该照样拼进成片")


class _FakeAccountPool:
    """duck-typed 假号池，避免依赖真实的 AdsPower 动态 import。"""

    def __init__(self, accounts=None, chosen=None):
        self._accounts = accounts or []
        self._chosen = chosen
        self.pick_calls = []
        self.mark_exhausted_calls = []

    def list_accounts(self):
        return self._accounts

    def pick_account(self, min_credit=1, *args, **kwargs):
        self.pick_calls.append(min_credit)
        return self._chosen

    def mark_exhausted(self, user_id, cooldown_hours=24.0):
        self.mark_exhausted_calls.append(user_id)


class TestSelectPoolAccount(unittest.TestCase):
    """号池自动选号钩子：池子为空/手动覆盖时完全不介入（向后兼容现有单选行为），
    非空且未手动指定时才自动选号并写回 config。"""

    def test_manual_override_skips_pool_entirely(self):
        pool = _FakeAccountPool(accounts=[{'user_id': 'a'}], chosen={'user_id': 'a'})
        config = {'googleFxUserId': 'manual_user'}
        result = _select_pool_account(config, pool)
        self.assertIsNone(result)
        self.assertEqual(config['googleFxUserId'], 'manual_user')
        self.assertEqual(pool.pick_calls, [])  # 完全没碰池子

    def test_empty_pool_is_noop(self):
        pool = _FakeAccountPool(accounts=[])
        config = {'googleFxUserId': ''}
        result = _select_pool_account(config, pool)
        self.assertIsNone(result)
        self.assertEqual(config.get('googleFxUserId'), '')
        self.assertEqual(pool.pick_calls, [])

    def test_picks_account_and_writes_back_to_config(self):
        pool = _FakeAccountPool(accounts=[{'user_id': 'a'}], chosen={'user_id': 'a', 'credit': 500})
        config = {}
        result = _select_pool_account(config, pool)
        self.assertEqual(result, 'a')
        self.assertEqual(config['googleFxUserId'], 'a')
        self.assertEqual(pool.pick_calls, [1])  # 默认 min_credit=1

    def test_custom_min_credit_threshold_is_forwarded(self):
        pool = _FakeAccountPool(accounts=[{'user_id': 'a'}], chosen={'user_id': 'a'})
        config = {'videoAccountPoolMinCredit': 50}
        _select_pool_account(config, pool)
        self.assertEqual(pool.pick_calls, [50])

    def test_no_eligible_account_raises_clear_error(self):
        pool = _FakeAccountPool(accounts=[{'user_id': 'a'}], chosen=None)
        config = {}
        with self.assertRaises(RuntimeError) as ctx:
            _select_pool_account(config, pool)
        self.assertIn('号池', str(ctx.exception))


class TestBatchBridgeCreditExhaustedDetection(unittest.TestCase):
    """账号池自动选号生成失败时，命中"积分耗尽"关键词应标记该账号冷却；
    手动单选/池子未参与本次生成时（pool_account_id 为 None）不应触碰账号池。"""

    class _Writer:
        def record(self, info):
            pass

    def _make_bridge(self, account_pool=None, pool_account_id=None):
        plan = {'slot': 1, 'seq': 1, 'prompt': 'p',
                'dest_path': os.path.join(os.getcwd(), 'vid_001.mp4')}
        return _BatchBridge(
            pending=[{'plan': plan}],
            total=1,
            video_model='veo',
            writer=self._Writer(),
            on_progress=lambda stage, payload: None,
            account_pool=account_pool,
            pool_account_id=pool_account_id,
        )

    def test_credit_exhausted_message_marks_account(self):
        pool = _FakeAccountPool()
        bridge = self._make_bridge(account_pool=pool, pool_account_id='acc_1')
        fake_helpers = type('FakeHelpers', (), {'is_credit_exhausted_message': staticmethod(lambda msg: True)})()
        with patch('video_generator._get_credit_helpers', return_value=fake_helpers):
            bridge(0, 'video_error', {'message': 'Insufficient credits'})
        self.assertEqual(pool.mark_exhausted_calls, ['acc_1'])

    def test_non_credit_failure_does_not_mark_account(self):
        pool = _FakeAccountPool()
        bridge = self._make_bridge(account_pool=pool, pool_account_id='acc_1')
        fake_helpers = type('FakeHelpers', (), {'is_credit_exhausted_message': staticmethod(lambda msg: False)})()
        with patch('video_generator._get_credit_helpers', return_value=fake_helpers):
            bridge(0, 'video_error', {'message': '生成失败：网络超时'})
        self.assertEqual(pool.mark_exhausted_calls, [])

    def test_no_pool_account_id_never_touches_pool(self):
        """手动单选/池子为空时 pool_account_id 为 None，不应尝试判定/标记。"""
        pool = _FakeAccountPool()
        bridge = self._make_bridge(account_pool=pool, pool_account_id=None)
        bridge(0, 'video_error', {'message': 'Insufficient credits'})
        self.assertEqual(pool.mark_exhausted_calls, [])


class TestAccountRotationRing(unittest.TestCase):
    """换号不换 IP 阶梯的轮转环：只收当前真正可用的号，本次已选中的号排最前。"""

    def _pool(self, accounts):
        return _FakeAccountPool(accounts=accounts)

    def test_orders_current_account_first(self):
        pool = self._pool([{'user_id': 'a'}, {'user_id': 'b'}, {'user_id': 'c'}])
        self.assertEqual(_account_rotation_ring({}, pool, 'b'), ['b', 'a', 'c'])

    def test_excludes_disabled_and_low_credit(self):
        pool = self._pool([
            {'user_id': 'a', 'credit': 100},
            {'user_id': 'b', 'disabled': True, 'credit': 100},
            {'user_id': 'c', 'credit': 0},
            {'user_id': 'd'},  # 积分未知：不排除
        ])
        self.assertEqual(_account_rotation_ring({}, pool, 'a'), ['a', 'd'])

    def test_excludes_cooling_down_account(self):
        future = (datetime.now() + timedelta(hours=2)).isoformat()
        past = (datetime.now() - timedelta(hours=2)).isoformat()
        pool = self._pool([
            {'user_id': 'a'},
            {'user_id': 'b', 'cooldown_until': future},
            {'user_id': 'c', 'cooldown_until': past},
            {'user_id': 'd', 'cooldown_until': 'garbage'},  # 解析不了不当冷却
        ])
        self.assertEqual(_account_rotation_ring({}, pool, 'a'), ['a', 'c', 'd'])

    def test_pool_read_failure_falls_back_to_single_account(self):
        class _Broken:
            def list_accounts(self):
                raise RuntimeError('AdsPower 不在线')
        self.assertEqual(_account_rotation_ring({}, _Broken(), 'a'), ['a'])


class TestPlanGenerationLegs(unittest.TestCase):
    """切腿：每 switch_interval 个请求换一个号，全程同一个 IP（换 IP 已全局关停）。"""

    def _items(self, n):
        return [{'plan': {'slot': i}} for i in range(n)]

    def test_single_account_pool_keeps_one_leg(self):
        """可换的号 ≤1 个时不切腿。"""
        for ring in ([], ['a']):
            legs = plan_generation_legs(self._items(12), ring, 5)
            self.assertEqual(len(legs), 1)
            self.assertEqual(len(legs[0]['items']), 12)
            self.assertIsNone(legs[0]['user_id'])

    def test_switches_account_every_interval(self):
        legs = plan_generation_legs(self._items(12), ['a', 'b', 'c'], 5)
        self.assertEqual([len(l['items']) for l in legs], [5, 5, 2])
        self.assertEqual([l['user_id'] for l in legs], ['a', 'b', 'c'])

    def test_account_ring_wraps_around(self):
        legs = plan_generation_legs(self._items(20), ['a', 'b'], 5)
        self.assertEqual([l['user_id'] for l in legs], ['a', 'b', 'a', 'b'])

    def test_never_emits_rotate_ip(self):
        """换 IP 已关停：腿计划里不该再出现任何换 IP 指示。"""
        for ring in ([], ['a'], ['a', 'b']):
            for leg in plan_generation_legs(self._items(20), ring, 5):
                self.assertNotIn('rotate_ip', leg)


class TestRotationKnobs(unittest.TestCase):
    """换号节拍的取值口径（配置键沿用历史名 googleFxIpRotateRequests）。"""

    def test_switch_interval_defaults_and_sanitizes(self):
        self.assertEqual(_account_switch_interval({}), 5)
        self.assertEqual(_account_switch_interval({'googleFxIpRotateRequests': 8}), 8)
        self.assertEqual(_account_switch_interval({'googleFxIpRotateRequests': 0}), 5)
        self.assertEqual(_account_switch_interval({'googleFxIpRotateRequests': 'x'}), 5)
        self.assertEqual(_account_switch_interval({'googleFxIpRotateRequests': -3}), 1)


class TestIpRotationDisabled(unittest.TestCase):
    """换 IP 全局关停的唯一出口：MIYA_ROTATE_THRESHOLD 恒定写成够不到的阈值。"""

    def test_threshold_pinned_regardless_of_config(self):
        import os
        from server_common import apply_google_fx_runtime_overrides, _IP_ROTATE_DISABLED
        for cfg in ({}, {'googleFxIpRotateRequests': 5}, {'googleFxIpRotateRequests': 1}):
            os.environ['MIYA_ROTATE_THRESHOLD'] = '5'
            apply_google_fx_runtime_overrides(cfg)
            self.assertEqual(os.environ['MIYA_ROTATE_THRESHOLD'], str(_IP_ROTATE_DISABLED))


class TestNextUnusedAccount(unittest.TestCase):
    """登录失效补跑时挑号：先在环里找没用过的，环里没了再回头问号池。"""

    def test_prefers_unused_ring_member(self):
        pool = _FakeAccountPool(chosen={'user_id': 'z'})
        self.assertEqual(_next_unused_account({}, pool, ['a', 'b', 'c'], {'a', 'b'}), 'c')
        self.assertEqual(pool.pick_calls, [])  # 环里够用就不打扰号池

    def test_falls_back_to_pool_pick(self):
        pool = _FakeAccountPool(chosen={'user_id': 'z'})
        self.assertEqual(_next_unused_account({}, pool, ['a'], {'a'}), 'z')

    def test_returns_none_when_everything_used(self):
        pool = _FakeAccountPool(chosen={'user_id': 'a'})
        self.assertIsNone(_next_unused_account({}, pool, ['a'], {'a'}))


class TestContinueVideoSequenceManualAndExistingSlots(unittest.TestCase):
    """测试继续生成视频序列时对手动上传、手动换位和已有视频槽位的识别与保留。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.videos_dir = os.path.join(self.tmp, 'videos')
        self.frames_dir = os.path.join(self.tmp, 'frames')
        os.makedirs(self.videos_dir)
        os.makedirs(self.frames_dir)
        self.VIDEOS = {
            1: 'move from IMAGE 1 to IMAGE 2',
            2: 'move from IMAGE 2 to IMAGE 3',
            3: 'move from IMAGE 3 to IMAGE 4',
        }

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _touch(self, path, content=b'fake-video-content'):
        with open(path, 'wb') as f:
            f.write(content)
        return path

    def _make_frames(self, n):
        frames = {}
        for i in range(1, n + 1):
            p = os.path.join(self.frames_dir, f'img_{i:03d}.webp')
            self._touch(p, b'frame')
            frames[i] = p
        return frames

    def test_manual_upload_is_reused_without_anchor_verification(self):
        """手动上传的视频即使锚点校验不符也不得被删除或重新生成。"""
        frames = self._make_frames(4)
        dest = self._touch(os.path.join(self.videos_dir, 'vid_002.mp4'))
        existing_videos = [
            {'slot': 2, 'source': 'manual_upload', 'model': 'manual_upload', 'status': 'success'}
        ]
        verify_called = []
        def fake_verify(*args, **kwargs):
            verify_called.append(args)
            return False, 'anchor mismatch'

        plans = plan_video_slots(
            self.VIDEOS, frames, {}, self.videos_dir,
            verify_fn=fake_verify,
            existing_videos=existing_videos
        )
        self.assertEqual(plans[1]['action'], 'reuse')
        self.assertFalse(plans[1]['delete_existing'])
        # 槽位 2 不应调用 verify_fn（直接信任手动上传）
        self.assertEqual(len(verify_called), 0)

    def test_manual_swap_is_reused_without_anchor_verification(self):
        """手动换位的视频即使首尾帧不匹配新槽位锚点也必须保留复用。"""
        frames = self._make_frames(4)
        self._touch(os.path.join(self.videos_dir, 'vid_002.mp4'))
        existing_videos = [
            {'slot': 2, 'source': 'manual_swap', 'swapped_from_slot': 1, 'status': 'success'}
        ]
        plans = plan_video_slots(
            self.VIDEOS, frames, {}, self.videos_dir,
            verify_fn=lambda *a, **k: (False, 'mismatch'),
            existing_videos=existing_videos
        )
        self.assertEqual(plans[1]['action'], 'reuse')
        self.assertFalse(plans[1]['delete_existing'])

    def test_existing_success_video_reused_if_anchors_not_stale(self):
        """已成功生成的视频在锚点帧未重渲（无 stale_slots）时应直接复用。"""
        frames = self._make_frames(4)
        self._touch(os.path.join(self.videos_dir, 'vid_001.mp4'))
        existing_videos = [
            {'slot': 1, 'status': 'success', 'model': 'Veo 3.1 - Lite'}
        ]
        plans = plan_video_slots(
            self.VIDEOS, frames, {}, self.videos_dir,
            verify_fn=lambda *a, **k: (False, 'minor drift'),
            stale_slots=set(),
            existing_videos=existing_videos
        )
        self.assertEqual(plans[0]['action'], 'reuse')
        self.assertFalse(plans[0]['delete_existing'])

    def test_existing_success_video_checks_anchors_if_stale(self):
        """若关联锚点帧重新渲染过（在 stale_slots 中），则必须核对锚点并拦截/重新生成。"""
        frames = self._make_frames(4)
        self._touch(os.path.join(self.videos_dir, 'vid_001.mp4'))
        existing_videos = [
            {'slot': 1, 'status': 'success', 'model': 'Veo 3.1 - Lite'}
        ]
        # 标准门禁下，血统过期被拦截，且旧文件被标记删除
        plans = plan_video_slots(
            self.VIDEOS, frames, {}, self.videos_dir,
            verify_fn=lambda *a, **k: (False, 'stale mismatch'),
            stale_slots={2},  # slot 1 结束帧是 frame 2，已过期
            existing_videos=existing_videos
        )
        self.assertEqual(plans[0]['action'], 'blocked')
        self.assertTrue(plans[0]['delete_existing'])

        # 用户确认风险强制放行时，转入重新生成
        override_plans = plan_video_slots(
            self.VIDEOS, frames, {}, self.videos_dir,
            verify_fn=lambda *a, **k: (False, 'stale mismatch'),
            stale_slots={2},
            override_flagged=True,
            existing_videos=existing_videos
        )
        self.assertEqual(override_plans[0]['action'], 'generate')
        self.assertTrue(override_plans[0]['delete_existing'])

    def test_explicit_retry_deletes_even_manual_upload(self):
        """用户显式重试指定槽位时，应允许重新生成覆盖。"""
        frames = self._make_frames(4)
        self._touch(os.path.join(self.videos_dir, 'vid_002.mp4'))
        existing_videos = [
            {'slot': 2, 'source': 'manual_upload', 'status': 'success'}
        ]
        plans = plan_video_slots(
            self.VIDEOS, frames, {}, self.videos_dir,
            target_slots=['2'],
            existing_videos=existing_videos
        )
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0]['slot'], 2)
        self.assertEqual(plans[0]['action'], 'generate')
        self.assertTrue(plans[0]['delete_existing'])



class TestDeclaredAnchorExtractionAndPlanning(unittest.TestCase):
    """测试从视频提示词中智能提取首尾锚点声明，并在槽位规划中正确绑定。"""

    def test_extract_various_declared_anchors(self):
        from video_generator import extract_declared_frame_anchors

        # 1. 显式双帧描述（Shot B 进场工序）
        p1 = (
            "Use the provided first frame and last frame as exact composition anchors. "
            "Use IMAGE 7 as the actual first-frame image and IMAGE 8 as the actual last-frame image; "
            "every visible action must interpolate between those two frame images without inventing a third layout."
        )
        self.assertEqual(extract_declared_frame_anchors(p1, 6, 7), (7, 8))

        # 2. 箭头格式
        p2 = "视频 6 [BRIDGE TRANSITION] (Image 7 -> Image 8): worker enters chamber..."
        self.assertEqual(extract_declared_frame_anchors(p2, 6, 7), (7, 8))

        # 3. 中文格式
        p3 = "使用图片 7 作为起始帧，图片 8 作为结束帧进行平滑过渡。"
        self.assertEqual(extract_declared_frame_anchors(p3, 6, 7), (7, 8))

        # 4. 英雄单帧展示
        p4 = "Use the provided reference image (IMAGE 11) as the sole starting-frame anchor for this clip."
        self.assertEqual(extract_declared_frame_anchors(p4, 11, None), (11, None))

        # 5. 无显式声明（回退到默认）
        p5 = "worker paints the floor continuously."
        self.assertEqual(extract_declared_frame_anchors(p5, 3, 4), (3, 4))

    def _eleven_frames(self, tmp):
        frames_dir = os.path.join(tmp, 'frames')
        os.makedirs(frames_dir, exist_ok=True)
        frames = {}
        for i in range(1, 12):
            p = os.path.join(frames_dir, f'img_{i:03d}.webp')
            with open(p, 'wb') as f:
                f.write(b'frame')
            frames[i] = p
        return frames

    _SHIFTED_SLOTS = {
        5: {
            'body': 'starting from the close-up shot of IMAGE 5... reveal ladder (IMAGE 6)',
            'meta': 'CUT - SHOT A TRANSITION',
        },
        6: {
            # 组稿阶段整段编号滑了一格：视频 6 本该是 IMAGE 6 -> IMAGE 7
            'body': (
                'Use the provided first frame and last frame as exact composition anchors. '
                'Use IMAGE 7 as the actual first-frame image and IMAGE 8 as the actual last-frame image; '
                'worker climbs down ladder and fastens oak timber panels matching IMAGE 8.'
            ),
            'meta': 'SHOT B INTERIOR ENTRY',
        },
    }

    def test_slot_contract_wins_over_declared_anchors(self):
        """正文声明的编号不得改选帧：错位的声明必须被拦下，而不是照着执行。"""
        tmp = tempfile.mkdtemp()
        try:
            videos_dir = os.path.join(tmp, 'videos')
            os.makedirs(videos_dir)
            frames = self._eleven_frames(tmp)

            plans = plan_video_slots(dict(self._SHIFTED_SLOTS), frames, {}, videos_dir)

            # slot 5：声明与契约一致，照常生成
            self.assertEqual(plans[0]['slot'], 5)
            self.assertEqual(plans[0]['start_anchor_slot'], 5)
            self.assertEqual(plans[0]['end_anchor_slot'], 6)
            self.assertIsNone(plans[0]['anchor_declaration_mismatch'])
            self.assertEqual(plans[0]['action'], 'generate')

            # slot 6：声明 7->8，契约 6->7。锚点仍按契约，且必须拦下来
            self.assertEqual(plans[1]['slot'], 6)
            self.assertEqual(plans[1]['start_anchor_slot'], 6)
            self.assertEqual(plans[1]['end_anchor_slot'], 7)
            self.assertEqual(plans[1]['start_frame'], frames[6])
            self.assertEqual(plans[1]['end_frame'], frames[7])
            self.assertEqual(plans[1]['action'], 'blocked')
            self.assertIn('编号错位', plans[1]['reason'])
            self.assertEqual(plans[1]['anchor_declaration_mismatch'], {
                'declared_start': 7, 'declared_end': 8,
                'contract_start': 6, 'contract_end': 7,
            })
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_declared_mismatch_override_generates_by_contract(self):
        """确认风险强制生成时：仍按契约取帧，只降级成告警，并把不符留痕。"""
        tmp = tempfile.mkdtemp()
        try:
            videos_dir = os.path.join(tmp, 'videos')
            os.makedirs(videos_dir)
            frames = self._eleven_frames(tmp)

            plans = plan_video_slots(dict(self._SHIFTED_SLOTS), frames, {}, videos_dir,
                                      override_flagged=True)
            slot6 = plans[1]
            self.assertEqual(slot6['action'], 'generate')
            self.assertEqual(slot6['start_frame'], frames[6])
            self.assertEqual(slot6['end_frame'], frames[7])
            self.assertIn('与槽位契约不符', slot6.get('warning', ''))
            # 文案改写按声明的编号做，别在提示词里留下裸的 IMAGE 7 / IMAGE 8
            self.assertIn('IMAGE 1', slot6['prompt'])
            self.assertIn('IMAGE 2', slot6['prompt'])
            self.assertNotIn('IMAGE 7', slot6['prompt'])
            self.assertNotIn('IMAGE 8', slot6['prompt'])
            # 质检档位关掉时同样只告警不拦
            off_plans = plan_video_slots(dict(self._SHIFTED_SLOTS), frames, {}, videos_dir,
                                          gate_level='off')
            self.assertEqual(off_plans[1]['action'], 'generate')
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_video_info_records_end_anchor_and_slot_sequence(self):
        """manifest 条目必须带 end_anchor_slot，且 sequence 用 slot 而不是批内序号。"""
        from video_generator import _video_info
        plan = {
            'slot': 7, 'seq': 1, 'prompt': 'x', 'dest_path': os.path.join(os.sep, 'tmp', 'v.mp4'),
            'start_anchor_slot': 7, 'end_anchor_slot': 8, 'meta': '',
        }
        info = _video_info(plan, 'Veo 3.1', status='failed', error='boom')
        self.assertEqual(info['slot'], 7)
        self.assertEqual(info['sequence'], 7)          # 不是批内序号 1
        self.assertEqual(info['start_anchor_slot'], 7)
        self.assertEqual(info['end_anchor_slot'], 8)

        hero = dict(plan, slot=11, end_anchor_slot=None, meta='HERO REVEAL')
        self.assertIsNone(_video_info(hero, 'Veo 3.1', status='failed')['end_anchor_slot'])


if __name__ == '__main__':
    unittest.main()


