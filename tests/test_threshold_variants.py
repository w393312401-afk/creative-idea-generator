# -*- coding: utf-8 -*-
"""TBCP v4（docs/threshold_protocol_revision.md §12）单拍收编过门协议的单元测试：

- 两态镜头族（exterior/interior）：coaxial 与 pan 变体统一收编成单一 bridge_stage=1 拍，
  不再有 sill/vestibule 中间态
- 声明式硬切（hard_cut / [CUT] 槽位）：族计算、族锚豁免、配对跳过、门禁预期缺失、
  血统分段（本次未改动，保持覆盖）
- P0 门框出画确定性校验（check_interior_door_clearance）
- 单一过门拍合并镜头（推进+转向）的过程内容校验（check_video_process_content is_turn）
- brief 变体声明归一化（threshold_variant/threshold_elevated）
"""
import os
import json
import shutil
import tempfile
import unittest
from unittest.mock import patch

from prompt_pipeline import (
    beat_space_family,
    image_space_family,
    family_anchor_seq,
    threshold_variant,
    threshold_elevated,
    normalize_beat_ladder,
    check_interior_door_clearance,
    check_video_process_content,
    check_post_reveal_cleanup_prompts,
    validate_beat_prompts,
    _build_partial_prompt_block,
    _parse_prompt_slots,
    _stage_scope_ladder_violations,
    HARD_CUT_VIDEO_PLACEHOLDER,
    is_legacy_hard_cut_placeholder,
    beat_is_crossing_clip,
    _beat_contract,
    fix_video_opening,
    check_video_opening,
    check_threshold_monotonic_inheritance,
)
from video_generator import plan_video_slots, merge_project_videos, PartialMergeBlocked
from frame_generator import update_manifest_stale_status


# 每个相位的同族伴随工序，用来把夹具梯子的 package_operations 补到下限 2 道
# （prompt_pipeline._MIN_PACKAGE_OPERATIONS）。与 deterministic_fallback_beat_ladder
# 里的 phase_companion 同源；同族配对不会额外触发相位冲突/材料层跨越判据。
_COMPANION = {
    'clearing': 'demolition', 'repair': 'placement', 'rough-in': 'wiring',
    'framing': 'insulation', 'drywall': 'paneling', 'flooring': 'painting',
    'painting': 'priming', 'furnishing': 'lighting',
}


def _ladder_coaxial(n=8, t=4):
    """Coaxial variant: the entire crossing is ONE beat (bridge_stage=1) at index t."""
    ladder = []
    for i in range(1, n + 1):
        b = {'index': i, 'operation': 'repair', 'description': f'step {i}', 'bridge_stage': None}
        if i == t:
            b.update(operation='threshold', bridge_stage=1)
        ladder.append(b)
    return ladder


def _ladder_pan(n=8, t=4, turn_direction='right'):
    """Pan variant: same single beat, but it also carries turn_direction — the same
    clip pushes through the threshold AND ends in one pan onto the interior axis."""
    ladder = _ladder_coaxial(n, t)
    ladder[t - 1]['turn_direction'] = turn_direction
    return ladder


def _ladder_cut(n=8, t=4):
    ladder = []
    for i in range(1, n + 1):
        b = {'index': i, 'operation': 'repair', 'description': f'step {i}', 'bridge_stage': None}
        if i == t:
            b.update(operation='threshold', hard_cut=True)
        ladder.append(b)
    return ladder


class TestBeatSpaceFamily(unittest.TestCase):
    def test_coaxial_single_bridge_beat(self):
        ladder = _ladder_coaxial(t=4)
        self.assertEqual(beat_space_family(ladder, 3), 'exterior')
        self.assertEqual(beat_space_family(ladder, 4), 'interior')
        self.assertEqual(beat_space_family(ladder, 5), 'interior')
        self.assertEqual(beat_space_family(ladder, 8), 'interior')

    def test_pan_variant_same_family_shape_as_coaxial(self):
        # The pan variant's turn happens inside the same beat's own VIDEO — it never
        # introduces a distinct family state (no more 'vestibule').
        ladder = _ladder_pan(t=4)
        self.assertEqual(beat_space_family(ladder, 3), 'exterior')
        self.assertEqual(beat_space_family(ladder, 4), 'interior')
        self.assertEqual(beat_space_family(ladder, 5), 'interior')
        self.assertEqual(beat_space_family(ladder, 8), 'interior')

    def test_hard_cut_no_sill(self):
        ladder = _ladder_cut(t=4)
        self.assertEqual(beat_space_family(ladder, 3), 'exterior')
        self.assertEqual(beat_space_family(ladder, 4), 'interior')
        self.assertEqual(beat_space_family(ladder, 8), 'interior')

    def test_no_crossing_all_exterior(self):
        ladder = [{'index': i, 'operation': 'repair', 'bridge_stage': None} for i in range(1, 5)]
        for i in range(1, 5):
            self.assertEqual(beat_space_family(ladder, i), 'exterior')


class TestImageSpaceFamily(unittest.TestCase):
    def test_single_bridge_meta(self):
        videos = {3: {'body': 'v', 'meta': ''}, 4: {'body': 'v', 'meta': 'BRIDGE'},
                  5: {'body': 'v', 'meta': ''}}
        self.assertEqual(image_space_family(videos, 4), 'exterior')
        self.assertEqual(image_space_family(videos, 5), 'interior')
        self.assertEqual(image_space_family(videos, 6), 'interior')

    def test_single_bridge_turn_meta(self):
        # Pan variant: still exactly one bridge-tagged video, just labeled TURN.
        videos = {3: {'body': 'v', 'meta': ''}, 4: {'body': 'v', 'meta': 'BRIDGE TURN'},
                  5: {'body': 'v', 'meta': ''}}
        self.assertEqual(image_space_family(videos, 4), 'exterior')
        self.assertEqual(image_space_family(videos, 5), 'interior')
        self.assertEqual(image_space_family(videos, 6), 'interior')

    def test_cut_meta(self):
        videos = {3: {'body': 'v', 'meta': ''}, 4: {'body': HARD_CUT_VIDEO_PLACEHOLDER, 'meta': 'CUT'},
                  5: {'body': 'v', 'meta': ''}}
        self.assertEqual(image_space_family(videos, 4), 'exterior')
        self.assertEqual(image_space_family(videos, 5), 'interior')
        self.assertEqual(image_space_family(videos, 6), 'interior')


class TestLegacyHardCutPlaceholder(unittest.TestCase):
    """2026-07-30：[CUT] 槽改为真实生成的跨越片段，占位声明只作为「识别旧单」的常量保留
    （旧单的 prompt_block 里仍是这段正文，那些单继续跳过生成）。"""

    def test_placeholder_is_recognized_as_legacy(self):
        self.assertTrue(is_legacy_hard_cut_placeholder(HARD_CUT_VIDEO_PLACEHOLDER))
        # 前后空白/大小写不影响识别（正文经过格式化回读）
        self.assertTrue(is_legacy_hard_cut_placeholder('\n  declared hard cut - no video clip...'))

    def test_real_crossing_prompt_is_not_legacy(self):
        body = ('Use the provided first frame and last frame as exact composition anchors. '
                'The sealed hatch is pushed open and the camera pushes through into the interior.')
        self.assertFalse(is_legacy_hard_cut_placeholder(body))
        self.assertFalse(is_legacy_hard_cut_placeholder(''))
        self.assertFalse(is_legacy_hard_cut_placeholder(None))


class TestFamilyAnchorSeq(unittest.TestCase):
    def test_cut_counts_as_family_boundary(self):
        videos = {3: {'body': 'v', 'meta': ''}, 4: {'body': 'v', 'meta': 'CUT'},
                  5: {'body': 'v', 'meta': ''}}
        self.assertEqual(family_anchor_seq(videos, 3), 1)
        self.assertEqual(family_anchor_seq(videos, 6), 5)

    def test_turn_bridge_moves_anchor_to_settle(self):
        videos = {4: {'body': 'v', 'meta': 'BRIDGE TURN'}, 5: {'body': 'v', 'meta': ''}}
        # The single merged crossing beat (pan variant) is the new family anchor.
        self.assertEqual(family_anchor_seq(videos, 7), 5)


class TestCrossingClipPredicate(unittest.TestCase):
    """视频侧「是不是跨越镜头」的唯一判据：bridge_stage==1 或 hard_cut。漏掉 hard_cut
    就会把切入拍当普通静止施工拍处理（运镜句被删、动作正文清空）。"""

    def test_bridge_and_cut_both_count(self):
        self.assertTrue(beat_is_crossing_clip({'bridge_stage': 1}))
        self.assertTrue(beat_is_crossing_clip({'hard_cut': True}))
        self.assertTrue(beat_is_crossing_clip({'bridge_stage': 1, 'turn_direction': 'left'}))

    def test_ordinary_beats_do_not(self):
        self.assertFalse(beat_is_crossing_clip({'operation': 'repair', 'bridge_stage': None}))
        self.assertFalse(beat_is_crossing_clip({'hard_cut': False}))
        self.assertFalse(beat_is_crossing_clip(None))


class TestThresholdVariantHelpers(unittest.TestCase):
    def test_normalization(self):
        self.assertEqual(threshold_variant({'threshold_variant': 'PAN_LEFT'}), 'pan_left')
        self.assertEqual(threshold_variant({'threshold_variant': 'hard_cut'}), 'hard_cut')
        self.assertEqual(threshold_variant({'threshold_variant': 'bogus'}), 'coaxial')
        self.assertEqual(threshold_variant({}), 'coaxial')
        self.assertEqual(threshold_variant(None), 'coaxial')

    def test_elevated_forced_false_for_hard_cut(self):
        self.assertTrue(threshold_elevated({'threshold_variant': 'pan_left', 'threshold_elevated': True}))
        self.assertFalse(threshold_elevated({'threshold_variant': 'hard_cut', 'threshold_elevated': True}))

    def test_normalize_beat_ladder_coerces_new_fields(self):
        ladder = normalize_beat_ladder([
            {'index': 1, 'operation': 'threshold', 'hard_cut': 'true', 'bridge_stage': None},
            {'index': 2, 'operation': 'threshold', 'bridge_stage': '1', 'turn_direction': 'Right'},
            {'index': 3, 'operation': 'repair', 'turn_direction': 'sideways'},
        ])
        self.assertIs(ladder[0]['hard_cut'], True)
        self.assertEqual(ladder[1]['bridge_stage'], 1)
        self.assertEqual(ladder[1]['turn_direction'], 'right')
        self.assertIsNone(ladder[2]['turn_direction'])

    def test_normalize_beat_ladder_coerces_stage_scope(self):
        ladder = normalize_beat_ladder([
            {'index': 1, 'operation': 'repair'},  # missing -> default
            {'index': 2, 'operation': 'repair', 'stage_scope': 'medium'},  # invalid -> default
            {'index': 3, 'operation': 'repair', 'stage_scope': 'LARGE'},
            {'index': 4, 'operation': 'repair', 'stage_scope': ' Small '},
            {'index': 5, 'operation': 'repair', 'stage_scope': 123},  # non-string -> default
        ])
        self.assertEqual(ladder[0]['stage_scope'], 'default')
        self.assertEqual(ladder[1]['stage_scope'], 'default')
        self.assertEqual(ladder[2]['stage_scope'], 'large')
        self.assertEqual(ladder[3]['stage_scope'], 'small')
        self.assertEqual(ladder[4]['stage_scope'], 'default')


class TestStageScopeQuota(unittest.TestCase):
    """2026-07-22: STAGE SCOPE quota rewritten from a global "exactly 1 large beat in the
    whole ladder" count to a per-operation-run rule — every run of consecutive beats
    sharing the same 'operation' must end its LAST beat with stage_scope='large' (that
    operation's own full-completion milestone), and no other beat in the run may be
    'large'. See docs/threshold_protocol_revision.md's alignment note and
    _stage_scope_ladder_violations' docstring for why the old global-1 quota starved
    every operation but one of ever reaching a real completion beat."""

    def _beats(self, ops_and_scopes, start=1):
        return [{'index': start + i, 'operation': op, 'stage_scope': scope}
                for i, (op, scope) in enumerate(ops_and_scopes)]

    def test_all_single_beat_runs_all_large_passes(self):
        # Normal/default case: every distinct operation gets exactly one beat, so every
        # eligible beat is its own (length-1) run and must be 'large'.
        ladder = self._beats([
            ('clearing', 'large'), ('repair', 'large'), ('flooring', 'large'),
            ('framing', 'large'), ('painting', 'large'),
        ])
        self.assertEqual(_stage_scope_ladder_violations(ladder), [])

    def test_single_beat_run_tagged_small_is_a_violation(self):
        ladder = self._beats([('clearing', 'small'), ('repair', 'large')])
        violations = _stage_scope_ladder_violations(ladder)
        self.assertTrue(any('stage_scope="large"' in v for v in violations))

    def test_multi_beat_run_last_beat_large_others_not_passes(self):
        # 'framing' deliberately split across 3 beats: build-up, build-up, completion.
        ladder = self._beats([
            ('clearing', 'large'),
            ('framing', 'default'), ('framing', 'small'), ('framing', 'large'),
            ('painting', 'large'),
        ])
        self.assertEqual(_stage_scope_ladder_violations(ladder), [])

    def test_multi_beat_run_missing_large_finish_is_a_violation(self):
        ladder = self._beats([
            ('framing', 'default'), ('framing', 'small'), ('framing', 'small'),
        ])
        violations = _stage_scope_ladder_violations(ladder)
        self.assertTrue(any('stage_scope="large"' in v for v in violations))

    def test_large_mid_run_is_a_violation(self):
        # 'large' claimed on a beat that is not the run's last beat.
        ladder = self._beats([
            ('framing', 'large'), ('framing', 'small'), ('framing', 'large'),
        ])
        violations = _stage_scope_ladder_violations(ladder)
        self.assertTrue(any('is not the last beat of its operation run' in v for v in violations))

    def test_non_contiguous_same_operation_forms_separate_runs(self):
        # Same operation value reappearing after a different operation in between forms
        # TWO independent runs, each needing its own 'large' finish.
        ladder = self._beats([
            ('repair', 'large'), ('flooring', 'large'), ('repair', 'large'),
        ])
        self.assertEqual(_stage_scope_ladder_violations(ladder), [])
        ladder2 = self._beats([
            ('repair', 'small'), ('flooring', 'large'), ('repair', 'large'),
        ])
        violations = _stage_scope_ladder_violations(ladder2)
        self.assertTrue(any('stage_scope="large"' in v for v in violations))

    def test_threshold_reward_bridge_hard_cut_beats_excluded_from_pool(self):
        # 唯一的合格拍(index 2)是 large；其余全是 threshold/reward/bridge/hard_cut，
        # 即便它们的 stage_scope 是垃圾值，也不该被计入合格池、不该触发违规。
        ladder = [
            {'index': 1, 'operation': 'threshold', 'bridge_stage': 1, 'stage_scope': 'small'},
            {'index': 2, 'operation': 'repair', 'stage_scope': 'large'},
            {'index': 3, 'operation': 'threshold', 'hard_cut': True, 'stage_scope': 'large'},
            {'index': 4, 'operation': 'reward', 'stage_scope': 'small'},
        ]
        self.assertEqual(_stage_scope_ladder_violations(ladder), [])

    def test_single_eligible_beat_must_be_large(self):
        ladder = self._beats([('repair', 'large')])
        self.assertEqual(_stage_scope_ladder_violations(ladder), [])
        ladder_bad = self._beats([('repair', 'default')])
        violations = _stage_scope_ladder_violations(ladder_bad)
        self.assertTrue(any('stage_scope="large"' in v for v in violations))

    def test_empty_ladder_is_no_violations(self):
        self.assertEqual(_stage_scope_ladder_violations([]), [])
        self.assertEqual(_stage_scope_ladder_violations(None), [])


class TestDoorClearanceCheck(unittest.TestCase):
    def test_flags_in_frame_door_element(self):
        errs = check_interior_door_clearance(
            'The doorway frames the view of the cabin. Walls are paneled.', family='interior')
        self.assertTrue(errs)

    def test_passes_behind_camera_wording(self):
        errs = check_interior_door_clearance(
            'Daylight from the entry behind the camera lays a bright wedge across the floor; '
            'the door frame is fully behind the camera and out of frame.', family='interior')
        self.assertEqual(errs, [])

    def test_silence_is_not_positive_clearance_evidence(self):
        errs = check_interior_door_clearance(
            'Interior stone walls, ceiling and floor fill the frame edge to edge.',
            family='interior')
        self.assertTrue(any('positively state' in err for err in errs))

    def test_archway_portal_and_entrance_are_covered(self):
        for label in ('arch', 'archway', 'portal', 'entrance'):
            with self.subTest(label=label):
                errs = check_interior_door_clearance(
                    f'The {label} frames the room from the foreground.', family='interior')
                self.assertTrue(errs)

    def test_exterior_exempt(self):
        p = 'The open doorway sits in Grid B2 with the sill line crossing the lower third.'
        self.assertEqual(check_interior_door_clearance(p, family='exterior'), [])

    def test_partial_transition_is_exempt_but_establish_is_not(self):
        common = dict(
            i=3, video_prompt='',
            image_prompt=('Camera pitch locked level; the archway rim and sill remain visible '
                          'around the partial first look.'),
            packet={}, mode='Standard', is_last=False,
            is_threshold_or_reveal=True, family='interior')
        partial = validate_beat_prompts(
            **common, beat={'bridge_stage': 2, 'transition_stage': 'threshold_partial'})
        self.assertFalse(any('Post-crossing interior IMAGE' in err for err in partial))

        establish = validate_beat_prompts(
            **common, beat={'bridge_stage': 3, 'transition_stage': 'interior_establish'})
        self.assertTrue(any('Post-crossing interior IMAGE' in err for err in establish))


class TestTurnVideoProcessCheck(unittest.TestCase):
    def test_turn_requires_both_translation_and_pan_description(self):
        # Missing both -> flagged.
        errs = check_video_process_content(
            'The camera holds while dust settles softly.', is_bridge=True, is_turn=True)
        self.assertTrue(errs)
        # Pan described but the push through the threshold is missing -> still flagged
        # (the merged clip needs BOTH movements written out, not just the turn).
        errs = check_video_process_content(
            'One smooth horizontal pan to the right sweeps onto the aisle axis.',
            is_bridge=True, is_turn=True)
        self.assertTrue(any('camera-translation' in e for e in errs))
        # Both the push and the closing pan are described -> passes.
        errs = check_video_process_content(
            'The camera glides forward through the open threshold, then one smooth '
            'horizontal pan to the right sweeps onto the aisle axis.',
            is_bridge=True, is_turn=True)
        self.assertEqual(errs, [])

    def test_normal_bridge_still_requires_translation(self):
        errs = check_video_process_content('Nothing moves.', is_bridge=True)
        self.assertTrue(errs)
        errs = check_video_process_content('The camera pushes forward across the sill.', is_bridge=True)
        self.assertEqual(errs, [])


class TestValidateBeatPromptsVariants(unittest.TestCase):
    PACKET = {'camera_dna': '', 'primary_landmarks': [], 'frame_boundaries': {}}

    CUT_IMAGE = ('Static tripod shot inside; camera pitch locked level; the central vanishing '
                 'axis stays centered.')

    def test_hard_cut_beat_is_validated_as_a_crossing_clip(self):
        """[CUT] 槽是真实生成的跨越片段：一条正常的过门镜头描述必须通过全套视频侧校验。"""
        beat = {'index': 4, 'operation': 'threshold', 'hard_cut': True, 'bridge_stage': None}
        video = ('Use the provided first frame and last frame as exact composition anchors. Use '
                 'IMAGE 4 as the actual first-frame image and IMAGE 5 as the actual last-frame '
                 'image; every visible action must interpolate between those two frame images '
                 'without inventing a third layout. The closed hatch is pushed open on camera and '
                 'the camera pushes forward in one continuous coaxial move through the opening, '
                 'settling fully inside; the frame stays completely sterile of workers throughout.')
        errs = validate_beat_prompts(
            4, video, self.CUT_IMAGE,
            self.PACKET, 'Threshold', False, True, beat=beat, family='interior')
        self.assertEqual([e for e in errs if 'VIDEO' in e or 'video' in e], [])

    def test_hard_cut_beat_without_any_camera_move_is_flagged(self):
        """回归护栏：占位声明式的正文（没有锚定开场、没有运镜动作）不再被静默放行——
        它正是"过门镜头不生成"的那种正文。"""
        beat = {'index': 4, 'operation': 'threshold', 'hard_cut': True, 'bridge_stage': None}
        errs = validate_beat_prompts(
            4, HARD_CUT_VIDEO_PLACEHOLDER, self.CUT_IMAGE,
            self.PACKET, 'Threshold', False, True, beat=beat, family='interior')
        # 占位声明没有插值锚定开场，也没有绑定首尾帧 —— 现在会被报出来
        self.assertTrue(any('missing required opening sentence' in e for e in errs))
        self.assertTrue(any('must bind IMAGE 4 (first frame) to IMAGE 5' in e for e in errs))

        # 一段完全静止、没有任何运镜的正文同样被报（跨越片段必须写出推进动作）
        static_body = (
            'Use the provided first frame and last frame as exact composition anchors. Use '
            'IMAGE 4 as the actual first-frame image and IMAGE 5 as the actual last-frame image; '
            'every visible action must interpolate between those two frame images without '
            'inventing a third layout. Dust hangs in the still air and the light shifts slowly.')
        errs = validate_beat_prompts(
            4, static_body, self.CUT_IMAGE,
            self.PACKET, 'Threshold', False, True, beat=beat, family='interior')
        self.assertTrue(any('camera-translation' in e for e in errs))

    def test_turn_beat_allows_pan_wording(self):
        # The single threshold/bridge beat (bridge_stage=1) with turn_direction set —
        # its VIDEO must narrate BOTH the push and the closing pan.
        beat = {'index': 6, 'operation': 'threshold', 'bridge_stage': 1, 'turn_direction': 'right'}
        video = ('Use the provided first frame and last frame as exact composition anchors. Use '
                 'IMAGE 6 as the actual first-frame image and IMAGE 7 as the actual last-frame '
                 'image; every visible action must interpolate between those two frame images '
                 'without inventing a third layout. The camera glides forward through the open '
                 'threshold, then pans smoothly to the right from the vestibule point, the window '
                 'band sliding in from the frame edge, completely sterile of workers throughout.')
        image = ('Static tripod shot inside the enclosed interior; camera pitch locked level; the '
                 'central vanishing axis stays centered on the rear interior wall in Grid B2.')
        errs = validate_beat_prompts(
            6, video, image, self.PACKET, 'Threshold', False, True, beat=beat, family='interior')
        self.assertEqual([e for e in errs if 'pan/tilt/orbit' in e], [])

    def test_non_turn_beat_still_bans_pan(self):
        beat = {'index': 3, 'operation': 'repair', 'bridge_stage': None}
        video = ('Use the provided first frame and last frame as exact composition anchors. '
                 'The camera pans left across the yard. continuous construction time-lapse, not real-time footage.')
        image = 'A static shot; horizon line remains perfectly level at exactly 50-percent height of the frame.'
        errs = validate_beat_prompts(
            3, video, image, self.PACKET, 'Standard', False, False, beat=beat, family='exterior')
        self.assertTrue(any('pan/tilt/orbit' in e for e in errs))


class TestPartialBlockMetas(unittest.TestCase):
    def test_pan_and_cut_metas_roundtrip(self):
        ladder = _ladder_pan(n=7, t=3, turn_direction='right')
        images = {i: f'image {i}' for i in range(1, 9)}
        videos = {i: f'video {i}' for i in range(1, 8)}
        f_imgs, f_vids, block = _build_partial_prompt_block(images, videos, ladder)
        # TBCP v4: the single crossing beat (index 3) tags its own IMAGE 4 as BRIDGE and
        # its own VIDEO 3 as BRIDGE TURN (turn_direction set) — no HOLD/SPAN split.
        self.assertEqual(f_imgs[4]['meta'], 'BRIDGE')
        self.assertEqual(f_vids[3]['meta'], 'BRIDGE TURN')
        # 解析回读保留 meta
        p_imgs, p_vids = _parse_prompt_slots(block)
        self.assertEqual(p_vids[3]['meta'], 'BRIDGE TURN')
        self.assertEqual(p_imgs[4]['meta'], 'BRIDGE')

        ladder_cut = _ladder_cut(n=7, t=3)
        f_imgs, f_vids, block = _build_partial_prompt_block(images, videos, ladder_cut)
        self.assertEqual(f_vids[3]['meta'], 'CUT')
        self.assertEqual(f_imgs[4]['meta'], 'CUT')
        p_imgs, p_vids = _parse_prompt_slots(block)
        self.assertEqual(p_vids[3]['meta'], 'CUT')
        self.assertEqual(p_imgs[4]['meta'], 'CUT')


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


class TestPlanVideoSlotsCut(_TmpDirCase):
    def test_cut_slot_now_generates_like_any_other(self):
        """2026-07-30：[CUT] 槽照常生成视频（正文是普通的跨越镜头描述），起止帧绑定
        与普通拍一致。此前它被无条件跳过，成片过门处只有两张静帧硬拼。"""
        frames = self._make_frames(4)
        videos = {1: {'body': 'v1', 'meta': ''},
                  2: {'body': 'the hatch is pushed open and the camera pushes through', 'meta': 'CUT'},
                  3: {'body': 'v3', 'meta': ''}}
        plans = plan_video_slots(videos, frames, {}, self.videos_dir)
        self.assertEqual([p['action'] for p in plans], ['generate', 'generate', 'generate'])
        self.assertEqual(plans[1]['start_anchor_slot'], 2)
        self.assertTrue(plans[1]['start_frame'] and plans[1]['end_frame'])

    def test_legacy_placeholder_body_now_generates_video(self):
        """旧单遗留的占位声明正文不再跳过生成，而是清洗为可执行的跨越运镜提示词后正常生成。"""
        frames = self._make_frames(4)
        videos = {1: {'body': 'v1', 'meta': ''},
                  2: {'body': HARD_CUT_VIDEO_PLACEHOLDER, 'meta': 'CUT'},
                  3: {'body': 'v3', 'meta': ''}}
        plans = plan_video_slots(videos, frames, {}, self.videos_dir)
        self.assertEqual([p['action'] for p in plans], ['generate', 'generate', 'generate'])
        self.assertEqual(plans[1]['start_anchor_slot'], 2)
        self.assertTrue(plans[1]['start_frame'] and plans[1]['end_frame'])
        self.assertIn('IMAGE 1', plans[1]['prompt'])
        self.assertIn('IMAGE 2', plans[1]['prompt'])
        self.assertNotIn('no video clip is generated', plans[1]['prompt'])

    def test_explicit_editorial_cut_now_generates_video(self):
        """显式声明剪辑硬切的提示词同样转换为可执行的跨越运镜提示词，照常生成视频。"""
        frames = self._make_frames(4)
        videos = {
            1: {'body': 'v1', 'meta': ''},
            2: {
                'body': ('This slot is an intentional editorial cut, not an interpolated '
                         'transformation. No generated in-between image or camera travel.'),
                'meta': 'CUT',
            },
            3: {'body': 'v3', 'meta': ''},
        }
        plans = plan_video_slots(videos, frames, {}, self.videos_dir)
        self.assertEqual([p['action'] for p in plans], ['generate', 'generate', 'generate'])
        self.assertEqual(plans[1]['start_anchor_slot'], 2)
        self.assertTrue(plans[1]['start_frame'] and plans[1]['end_frame'])
        self.assertIn('IMAGE 1', plans[1]['prompt'])
        self.assertIn('IMAGE 2', plans[1]['prompt'])

    def test_bridge_turn_slot_not_mistaken_for_cut(self):
        frames = self._make_frames(3)
        videos = {1: {'body': 'v1', 'meta': 'BRIDGE TURN'}, 2: {'body': 'v2', 'meta': ''}}
        plans = plan_video_slots(videos, frames, {}, self.videos_dir)
        self.assertEqual([p['action'] for p in plans], ['generate', 'generate'])
        # The single bridge beat's video binds normally (IMAGE slot -> IMAGE slot+1),
        # no start-anchor redirection.
        self.assertEqual(plans[0]['start_anchor_slot'], 1)


class TestMergeGateCut(_TmpDirCase):
    def _write_manifest(self, frames_n, videos_entries):
        frames = []
        for i in range(1, frames_n + 1):
            frames.append({'slot': i, 'sequence': i,
                           'file': os.path.relpath(os.path.join(self.frames_dir, f'img_{i:03d}.webp'),
                                                   self.tmp).replace('\\', '/')})
        manifest = {'title': 't', 'frames': frames, 'videos': videos_entries}
        with open(os.path.join(self.tmp, 'manifest.json'), 'w', encoding='utf-8') as f:
            json.dump(manifest, f)

    def test_skipped_cut_is_expected_gap_not_missing(self):
        self._make_frames(4)
        # 槽位 2 是声明式硬切（旧单记录）；槽位 3 真缺失 → 门禁跳过无文件的槽位后合并，missing 只计 [1, 3]（槽位 1 缺少实际 mp4 文件），不计 2
        self._write_manifest(4, [
            {'slot': 1, 'status': 'success', 'file': 'videos/vid_001.mp4'},
            {'slot': 2, 'status': 'skipped_cut'},
        ])
        with patch('video_generator._merge_skip_missing') as mock_skip_merge:
            merge_project_videos(self.tmp)
            self.assertTrue(mock_skip_merge.called)
            args = mock_skip_merge.call_args[0]
            missing = args[4]
            self.assertNotIn(2, missing)
            self.assertIn(3, missing)


class TestContinuousStaleLineage(_TmpDirCase):
    def _frames(self, metas):
        return [{'sequence': i, 'slot': i, 'meta': metas.get(i, '')} for i in sorted(set(list(metas) + list(range(1, 7))))]

    def test_regen_before_cut_stales_after_cut_too(self):
        manifest = {'frames': self._frames({4: 'CUT'})}
        update_manifest_stale_status(manifest, self.tmp, regenerated_sequences=[2], finalize=True)
        by_seq = {f['sequence']: f for f in manifest['frames']}
        self.assertTrue(by_seq[3].get('stale_lineage'))
        # CUT 仍以上一帧图生图，因此切点及其后也属于同一条派生链。
        self.assertTrue(by_seq[4].get('stale_lineage'))
        self.assertTrue(by_seq[5].get('stale_lineage'))
        self.assertTrue(by_seq[6].get('stale_lineage'))

    def test_regen_after_cut_stales_only_downstream_frames(self):
        manifest = {'frames': self._frames({4: 'CUT'})}
        update_manifest_stale_status(manifest, self.tmp, regenerated_sequences=[5], finalize=True)
        by_seq = {f['sequence']: f for f in manifest['frames']}
        self.assertFalse(by_seq[2].get('stale_lineage'))
        self.assertFalse(by_seq[3].get('stale_lineage'))
        self.assertFalse(by_seq[4].get('stale_lineage'))
        self.assertTrue(by_seq[6].get('stale_lineage'))

    def test_no_cut_behaviour_unchanged(self):
        manifest = {'frames': self._frames({})}
        update_manifest_stale_status(manifest, self.tmp, regenerated_sequences=[2], finalize=True)
        by_seq = {f['sequence']: f for f in manifest['frames']}
        self.assertTrue(all(by_seq[i].get('stale_lineage') for i in (3, 4, 5, 6)))
        self.assertFalse(by_seq[1].get('stale_lineage'))


# ─────────────────────────────────────────────────────────────────────────
# TBCP v4: single merged beat (bridge_stage=1 carries the entire crossing —
# push, door-frame wipe, settle, and — pan variant — the closing turn, all in
# one VIDEO bound normally from IMAGE i to IMAGE i+1) + first-interior-reveal
# decay-state wording.
# ─────────────────────────────────────────────────────────────────────────

class TestBeatContractBridgeFlags(unittest.TestCase):
    PACKET = {'camera_dna': '', 'primary_landmarks': [], 'frame_boundaries': {}}

    def _contract(self, ladder, i, total_beats=None, mode='Threshold'):
        return _beat_contract(i, total_beats or len(ladder), ladder, mode, self.PACKET, '')

    def test_coaxial_single_beat_flags(self):
        ladder = _ladder_coaxial(n=6, t=3)  # bridge_stage 1 at beat 3
        cross = self._contract(ladder, 3)
        self.assertTrue(cross['is_bridge'])
        self.assertFalse(cross['is_turn'])
        self.assertEqual(cross['family'], 'interior')
        self.assertTrue(cross['is_first_interior_reveal'])
        # The VIDEO binds normally from IMAGE 3 to IMAGE 4 — no redirection to any
        # other IMAGE number (no more HOLD beat to skip over).
        self.assertIn('IMAGE 3', cross['family_contract'])
        self.assertIn('IMAGE 4', cross['family_contract'])
        self.assertNotIn('IMAGE 2', cross['family_contract'])

        # A later, ordinary interior beat (no bridge_stage) must NOT carry the
        # first-reveal decay clause or the bridge flag.
        later = self._contract(ladder, 5)
        self.assertFalse(later['is_bridge'])
        self.assertFalse(later['is_first_interior_reveal'])
        self.assertNotIn('CONTINUITY, NOT A RESET', later['anchor_rule'])

    def test_pan_variant_turn_is_first_reveal_in_the_same_beat(self):
        ladder = _ladder_pan(n=6, t=3, turn_direction='right')  # single beat at 3, turn set
        cross = self._contract(ladder, 3)
        self.assertTrue(cross['is_bridge'])
        self.assertTrue(cross['is_turn'])
        self.assertEqual(cross['family'], 'interior')
        self.assertTrue(cross['is_first_interior_reveal'])
        self.assertIn('CONTINUITY, NOT A RESET', cross['anchor_rule'])
        # The merged clip's contract text must describe the closing pan, not a
        # separate turn beat.
        self.assertIn('pan', cross['family_contract'].lower())
        self.assertIn('to the right', cross['family_contract'])

    def test_decay_clause_absent_for_hard_cut_else_branch(self):
        # hard_cut already has its own dedicated decay wording (item 4 of its
        # anchor_rule) via the is_cut branch, not the first-reveal clause.
        ladder = _ladder_cut(n=6, t=3)
        cut = self._contract(ladder, 3)
        self.assertTrue(cut['is_cut'])
        self.assertIn('untouched pre-construction trauma', cut['anchor_rule'])
        self.assertNotIn('CONTINUITY, NOT A RESET', cut['anchor_rule'])

    def test_ordinary_interior_beat_states_door_clearance_only_once(self):
        # 2026-07-20 实机复盘：普通室内拍(非首现)的 anchor_rule 曾经和下面
        # family_contract 的"Door clearance (mandatory)"条目重复表述同一条门框出画
        # 要求，导致 LLM 在生成的 IMAGE 正文里把整句 camera_dna 复读了两遍（img_5/7/
        # 9/10/11 实例）。现在这条要求只应该出现在 family_contract 里一次，
        # anchor_rule 不应再重复它。
        ladder = _ladder_coaxial(n=6, t=3)  # bridge_stage 1 at beat 3
        later = self._contract(ladder, 5)  # ordinary interior beat, not first reveal
        self.assertNotIn('DOOR CLEARANCE', later['anchor_rule'])
        self.assertEqual(later['family_contract'].count('Door clearance (mandatory)'), 1)

    def test_cut_beat_contract_demands_a_real_generated_crossing_clip(self):
        """2026-07-30 回归护栏：切入拍的 VIDEO 契约必须要求一段真实片段（普通视频提示词、
        门在片段里被推开、绑定 IMAGE i -> i+1），不得再出现"占位声明/不生成片段"的口径。"""
        ladder = _ladder_cut(n=6, t=3)
        cut = self._contract(ladder, 3)
        contract = cut['family_contract']
        self.assertTrue(cut['is_cut'])
        self.assertIn('a real clip IS generated for this slot', contract)
        self.assertIn('pushed open on camera', contract)
        self.assertIn('IMAGE 3', contract)
        self.assertIn('IMAGE 4', contract)
        self.assertNotIn('placeholder', contract.lower())
        self.assertNotIn('no video clip', contract.lower())
        # 跨越片段的三条硬条款（纯运镜/全程同一状态/一镜到底）与 bridge 同权。
        # 2026-08-24：「全程废墟」改成「全程同一状态」——状态由这一拍自己申报。
        self.assertIn('sterile of workers', contract)
        self.assertIn('one state throughout', contract)
        self.assertIn('never a generic ruin', contract)
        self.assertIn('one unbroken take', contract.lower())

    def test_minimum_run_up_beat_ladder_helper_never_places_crossing_at_1_or_2(self):
        # Sanity check on the test helpers themselves — mirrors the real minimum
        # run-up rule (crossing beat index >= 3) enforced in compose_anchor_and_packet.
        ladder = _ladder_coaxial(n=8, t=4)
        bridge_idx = next(i for i, b in enumerate(ladder, start=1) if b.get('bridge_stage') == 1)
        self.assertGreaterEqual(bridge_idx, 3)


# ─────────────────────────────────────────────────────────────────────────
# 2026-07-26 用户实测两点：①过门帧有人工痕迹、不够原始；②过门帧之后必须再加一道
# 清理工序。①在契约里表现为首现帧的 UNTOUCHED TRAUMA STATE 条款加严（3 类衰败 +
# 零人工痕迹 + 不得摆放整齐），②表现为过门后第一拍恒为 clearing 并带自己的契约。
# 过门片段的视频提示词同时补上"全程原始废墟 / 一镜到底"两条。
# ─────────────────────────────────────────────────────────────────────────

class TestPostRevealCleanupContract(unittest.TestCase):
    PACKET = {'camera_dna': '', 'primary_landmarks': [], 'frame_boundaries': {}}

    def _ladder_with_cleanup(self, n=6, t=3, cut=False):
        ladder = _ladder_cut(n=n, t=t) if cut else _ladder_coaxial(n=n, t=t)
        ladder[t]['operation'] = 'clearing'  # beat t+1 (0-indexed t)
        return ladder

    def _contract(self, ladder, i, mode='Threshold'):
        return _beat_contract(i, len(ladder), ladder, mode, self.PACKET, '')

    def test_beat_after_crossing_is_the_cleanout_beat(self):
        ladder = self._ladder_with_cleanup(t=3)
        cleanup = self._contract(ladder, 4)
        self.assertTrue(cleanup['is_post_reveal_cleanup'])
        self.assertFalse(cleanup['is_first_interior_reveal'])
        self.assertIn('Post-crossing cleanout', cleanup['family_contract'])
        # 它清的正是上一张首现帧里的脏乱，所以契约必须点名起止两张图
        self.assertIn('IMAGE 4', cleanup['family_contract'])
        self.assertIn('IMAGE 5', cleanup['family_contract'])
        # 只搬东西不修东西——结构性衰败留给后面的修复拍
        self.assertIn('Nothing is repaired', cleanup['family_contract'])

    def test_hard_cut_variant_also_gets_the_cleanout_beat(self):
        ladder = self._ladder_with_cleanup(t=3, cut=True)
        self.assertTrue(self._contract(ladder, 4)['is_post_reveal_cleanup'])

    def test_crossing_beat_and_later_interior_beats_are_not_cleanup(self):
        ladder = self._ladder_with_cleanup(t=3)
        self.assertFalse(self._contract(ladder, 3)['is_post_reveal_cleanup'])  # 过门拍自己
        self.assertFalse(self._contract(ladder, 5)['is_post_reveal_cleanup'])  # 再往后的普通室内拍
        self.assertNotIn('Post-crossing cleanout', self._contract(ladder, 5)['family_contract'])

    def test_beat_after_crossing_that_is_not_clearing_gets_no_cleanout_contract(self):
        """2026-08-24 证据闸门：位置对了还不够，这一拍自己得真的申报清理。原片进门直接
        开工的片子不该被扣上一份「本拍是纯清理」的契约，凭空多出一道工序。"""
        ladder = _ladder_coaxial(n=6, t=3)
        ladder[3]['operation'] = 'framing'
        ladder[3]['milestone_name'] = 'stud wall framed along the north side'
        after = self._contract(ladder, 4)
        self.assertFalse(after['is_post_reveal_cleanup'])
        self.assertNotIn('Post-crossing cleanout', after['family_contract'])

    def test_standard_mode_never_marks_a_cleanup_beat(self):
        ladder = [{'index': i, 'operation': 'repair', 'description': f'step {i}',
                   'bridge_stage': None} for i in range(1, 6)]
        for i in range(1, 6):
            self.assertFalse(self._contract(ladder, i, mode='Standard')['is_post_reveal_cleanup'])

    def test_first_reveal_no_longer_dictates_a_ruin(self):
        """2026-08-24：首现帧的「必须是废墟」硬规则已删。剩下的是防倒退 + 一句
        「状态由这一拍自己申报」，两个方向的通用模板都不许再压上去。"""
        cross = self._contract(self._ladder_with_cleanup(t=3), 3)
        self.assertTrue(cross['is_first_interior_reveal'])
        self.assertNotIn('AT LEAST THREE', cross['anchor_rule'])
        self.assertNotIn('ZERO INTERVENTION EVIDENCE', cross['anchor_rule'])
        self.assertNotIn('UNARRANGED', cross['anchor_rule'])
        self.assertIn('never impose a generic ruin', cross['anchor_rule'])
        self.assertIn('never impose a generic tidy room', cross['anchor_rule'])
        self.assertIn('already CLOSED', cross['anchor_rule'])

    def test_crossing_clip_holds_one_state_and_the_take_unbroken(self):
        cross = self._contract(self._ladder_with_cleanup(t=3), 3)
        fc = cross['family_contract']
        self.assertIn('one state throughout', fc)
        self.assertIn('never a generic ruin', fc)
        self.assertIn('one unbroken take', fc)
        self.assertIn('either side of it', fc)

    def test_crossing_clip_with_outline_refs_remains_pure_and_sterile_of_work(self):
        """1:1 节拍映射下，过门拍依然是 100% 纯运镜拍，严禁要求在同一个镜头内继续施工。"""
        ladder = self._ladder_with_cleanup(t=3)
        ladder[2]['outline_refs'] = [3]
        ladder[2]['milestone_name'] = 'installing flooring'
        ladder[2]['package_operations'] = ['lay', 'fasten']
        cross = self._contract(ladder, 3)
        fc = cross['family_contract']
        self.assertIn('one state throughout', fc)
        self.assertNotIn('continues into real work', fc)
        self.assertIn('either side of it', fc)


class TestBridgeClipWorkContentCheck(unittest.TestCase):
    """过门片段是纯运镜：穿越途中不许有人在清理/施工（清理是下一拍的活）。"""

    BASE = ("Use the provided first frame and last frame as exact composition anchors. "
            "The camera pushes forward through the open threshold and settles fully inside. ")

    def test_cleanup_work_inside_the_crossing_clip_is_flagged(self):
        errs = check_video_process_content(
            self.BASE + "Along the way the debris is carried out and the rubble raked aside.",
            is_bridge=True)
        self.assertTrue(any('construction/cleanup work during the crossing' in e for e in errs))

    def test_construction_work_inside_the_crossing_clip_is_flagged(self):
        errs = check_video_process_content(
            self.BASE + "Fresh boards are being installed across the far wall as the camera arrives.",
            is_bridge=True)
        self.assertTrue(any('construction/cleanup work during the crossing' in e for e in errs))

    def test_pure_camera_move_over_decay_passes(self):
        # 同形名词遍地都是（peeling paint / stacked wreckage / rusted bolts），运镜措辞
        # 里也有 clears/sweeping——这些都不能误判成"有人在干活"
        errs = check_video_process_content(
            self.BASE + "The door frame clears the left boundary as the wedge of daylight goes "
            "sweeping across the debris-strewn floor; peeling paint, stacked wreckage and rusted "
            "bolts slide past at constant scale.",
            is_bridge=True)
        self.assertEqual(errs, [])


class TestPostRevealCleanupPromptCheck(unittest.TestCase):
    _check = staticmethod(check_post_reveal_cleanup_prompts)

    IMG_OK = ("Static wide interior shot; camera pitch locked level. The floor is fully cleared "
              "back to its bare original planking, every trace of rubble hauled out, while the "
              "rust streaks and cracked rafters overhead remain exactly as found.")
    VID_OK = ("Use the provided first frame and last frame as exact composition anchors. One lone "
              "worker is hauling the fallen rubble out by the barrow-load, the cleared area growing "
              "across the floor while the spoil crate fills.")

    def test_noop_when_not_the_cleanup_beat(self):
        self.assertEqual(self._check('anything', 'anything', False), [])

    def test_compliant_pair_passes(self):
        self.assertEqual(self._check(self.IMG_OK, self.VID_OK, True), [])

    def test_image_without_a_cleared_result_is_flagged(self):
        img = "Static wide interior shot. Rubble still covers the rusted floor plates."
        errs = self._check(img, self.VID_OK, True)
        self.assertTrue(any('never states the cleared result' in e for e in errs))

    def test_image_that_scrubs_away_all_decay_is_flagged(self):
        img = ("Static wide interior shot. The floor is fully cleared back to bare planking and "
               "every surface reads smooth and sound.")
        errs = self._check(img, self.VID_OK, True)
        self.assertTrue(any('keeps no surviving decay' in e for e in errs))

    def test_video_without_removal_work_is_flagged(self):
        vid = ("Use the provided first frame and last frame as exact composition anchors. Light "
               "shifts slowly across the room as dust settles.")
        errs = self._check(self.IMG_OK, vid, True)
        self.assertTrue(any('describes no removal work' in e for e in errs))


class TestPostCrossingCleanupLadderGate(unittest.TestCase):
    """节拍梯结构校验：过门后第一拍必须是 clearing，否则打回重生成并把违规写进重试提示。"""

    def _threshold_ladder(self, post_crossing_op):
        def _milestone(idx, op):
            return {
                'index': idx, 'operation': op, 'description': f'{op} work {idx}',
                'bridge_stage': None, 'stage_scope': 'large',
                'milestone_name': f'{op} stage {idx} complete',
                'before_state': f'stage {idx} not started',
                'after_state': f'the entire stage {idx} surface is complete',
                'completion_extent': 'the entire named zone',
                'changed_grid_cells': ['Grid B2', 'Grid C2'],
                # 普通施工拍要申报 2~3 道紧密工序；同族伴随工序不触发相位冲突判据。
                'package_operations': [op, _COMPANION[op]] if op in _COMPANION else [op],
                'primary_progress': 'coverage grows from zero to the full zone',
                'secondary_progress': 'the staged stock drains from full to empty',
                'persistent_traces': ['fastener marks', 'contact dust'],
                'preserve_state': 'all earlier permanent work remains unchanged',
                'introduced_objects': [],
                'removed_objects': [],
            }
        ladder = [_milestone(1, 'clearing'), _milestone(2, 'repair')]
        ladder.append({'index': 3, 'operation': 'threshold', 'bridge_stage': 1,
                       'description': 'the camera crosses the threshold and settles inside'})
        ladder.append(_milestone(4, post_crossing_op))
        ladder.append({'index': 5, 'operation': 'reward', 'bridge_stage': None,
                       'description': 'warm light fills the finished space'})
        return json.dumps(ladder)

    def _run(self, ladder_jsons):
        import prompt_pipeline as pp
        from unittest.mock import patch

        brief_json = json.dumps({
            'carrier': 'abandoned watermill', 'env': 'forest gorge',
            'trauma': 'roof collapsed and silted up', 'destiny': 'writing cabin',
            'destiny_zh': '书房', 'reward': 'lamplight fills the finished mill',
            'mode': 'Threshold', 'space_type': 'abandoned property',
            'threshold_variant': 'coaxial', 'carrier_slug': 'watermill',
        })
        packet_json = json.dumps({
            'camera_dna': 'static tripod shot; horizon line remains level at 50-percent height',
            'geometry_lock': 'fixed boundaries',
            'primary_landmarks': [{'name': 'mill door', 'grid': 'Grid B2', 'z_depth_scale': '40%'}],
            'frame_boundaries': {'left': 'B1', 'right': 'B3', 'top': 'A2', 'bottom': 'C2'},
            'object_ledger': [], 'worker_choreography': 'one lone worker',
            'lighting_phase_ladder': {str(i): 'ambient only' for i in range(1, 7)},
            'passive_environment': 'still gorge air', 'interest_budget': {},
        })
        ladders = iter(ladder_jsons)
        beat_users = []

        def fake_chat(config, system, user, **kwargs):
            if 'scene analysis agent' in system:
                return brief_json
            if 'construction planner' in system:
                beat_users.append(user)
                return next(ladders)
            if 'spatial consistency supervisor' in system:
                return packet_json
            if 'generate the very first IMAGE prompt' in system:
                return 'A static wide shot of the derelict watermill, silted and untouched.'
            raise AssertionError(f'Unexpected _chat call: {system[:60]!r}')

        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        dims = {'theme': '峡谷废弃水磨坊', 'anchors': [], 'complexity': '中等重工',
                'budget': '轻奢设计师级', 'ratio': '50%', 'creativity': '脑洞大开',
                'beats_count': 4, 'beat_count_mode': 'fixed'}
        patches = [
            patch.object(pp, 'COMPOSE_CHECKPOINT_PATH', os.path.join(tmp, 'ckpt.json')),
            patch.object(pp, 'CACHE_PATH', os.path.join(tmp, 'packet_cache.json')),
            patch.object(pp, 'load_reference_file', return_value=''),
            patch.object(pp, 'get_cropped_templates', return_value=''),
            patch.object(pp, 'append_to_used_topic_ledger', return_value=None),
            patch.object(pp, '_chat', side_effect=fake_chat),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        state = pp.compose_anchor_and_packet({}, dims)
        return state, beat_users

    def test_non_clearing_beat_after_the_crossing_is_rejected_and_fed_back(self):
        state, beat_users = self._run([self._threshold_ladder('framing'),
                                       self._threshold_ladder('clearing')])
        establish = next(i for i, b in enumerate(state['beat_ladder'])
                         if b.get('transition_stage') == 'interior_establish')
        self.assertEqual(state['beat_ladder'][establish + 1]['operation'], 'clearing')
        # 第二次调用必须带着上一轮的结构违规回去
        self.assertIn('PRIOR STRUCTURE VIOLATIONS', beat_users[1])
        self.assertIn('"clearing" operation', beat_users[1])

    def test_clearing_beat_after_the_crossing_is_accepted_first_try(self):
        state, beat_users = self._run([self._threshold_ladder('clearing')])
        self.assertEqual(len(beat_users), 1)
        establish = next(i for i, b in enumerate(state['beat_ladder'])
                         if b.get('transition_stage') == 'interior_establish')
        self.assertEqual(state['beat_ladder'][establish + 1]['operation'], 'clearing')
        # 生成侧也必须把这条硬规则写进 system prompt（这里只验证它在用户可见的契约里）
        self.assertEqual(state['beat_ladder'][2]['bridge_stage'], 1)


class TestVideoOpeningFirstFrameIndex(unittest.TestCase):
    def test_fix_video_opening_default_binds_i_to_ip1(self):
        out = fix_video_opening(5, 'some prior text')
        self.assertIn('Use IMAGE 5 as the actual first-frame image', out)
        self.assertIn('IMAGE 6 as the actual last-frame image', out)

    def test_fix_video_opening_override_binds_custom_first_frame(self):
        # No current threshold variant uses this override (the single bridge beat's
        # VIDEO always binds normally); it remains available as a generic hook.
        out = fix_video_opening(5, 'some prior text', first_frame_index=3)
        self.assertIn('Use IMAGE 3 as the actual first-frame image', out)
        self.assertIn('IMAGE 6 as the actual last-frame image', out)

    def test_check_video_opening_matches_override(self):
        prompt = fix_video_opening(5, '', first_frame_index=3)
        self.assertEqual(check_video_opening(5, prompt, first_frame_index=3), [])
        # Without the matching override, the default IMAGE 5 binding is expected
        # and this prompt (bound to IMAGE 3) correctly fails it.
        self.assertTrue(check_video_opening(5, prompt))

    def test_validate_beat_prompts_bridge_beat_gets_full_video_checks(self):
        # TBCP v4: the single threshold/bridge beat's VIDEO is a real, visible clip —
        # it must pass the same video-side checks as any ordinary beat (no more
        # HOLD-style blanket skip).
        beat = {'index': 3, 'operation': 'threshold', 'bridge_stage': 1}
        packet = {'camera_dna': '', 'primary_landmarks': [], 'frame_boundaries': {}}
        thin_video = 'Ambient noise only, nothing else.'
        errs = validate_beat_prompts(
            3, thin_video,
            'Static tripod shot inside; camera pitch locked level; the central vanishing '
            'axis stays centered.',
            packet, 'Threshold', False, True, beat=beat, family='interior')
        self.assertTrue([e for e in errs if 'VIDEO' in e or 'video' in e.lower()])


class TestSealedEntryBeforeCrossing(unittest.TestCase):
    """TBCP v7：过门前那一帧的门一律关死，三变体统一。

    旧规则（PBISP peek）要求相反：门开着、室内锚点已经透过门洞可见。它是为 i2i 服务的
    （给室内首帧一个可继承的锚点），但在 i2v 侧代价更大——半开的门递给视频模型一块低
    分辨率、基本靠脑补的室内，模型随后把它当成插值时必须对齐的既成事实；对不上时，镜头
    落地的室内就读作换了个世界，或者干脆没完全进去。门关死则无可对齐，揭示整个发生在
    片段内部。
    """
    PACKET = {'camera_dna': '', 'primary_landmarks': [], 'frame_boundaries': {},
              'interior_camera_dna': 'Static interior shot.',
              'interior_primary_landmarks': [
                  {'name': 'ribbed roof curve', 'grid': 'Grid B2', 'z_depth_scale': 'half'}]}

    def _contract(self, ladder, i, mode='Threshold'):
        return _beat_contract(i, len(ladder), ladder, mode, self.PACKET, '')

    def test_beat_before_the_crossing_must_seal_its_entry(self):
        before = self._contract(_ladder_coaxial(t=4), 3)
        self.assertTrue(before['is_pre_bridge'])
        self.assertIn('SEALED ENTRY (mandatory)', before['family_contract'])
        self.assertIn('CLOSED', before['family_contract'])
        # 没有门扇的毛坯载体：等价合规形态是洞里一片不透光的黑
        self.assertIn('unlit darkness', before['family_contract'])
        # 那一拍的 VIDEO 末帧就是这张闭门帧，所以它也不许在结尾开门
        self.assertIn('SEALED ENTRY continuity in VIDEO 3', before['family_contract'])

    def test_pbisp_peek_is_gone_from_the_pre_crossing_contract(self):
        before = self._contract(_ladder_coaxial(t=4), 3)
        self.assertNotIn('PBISP', before['family_contract'])
        self.assertNotIn('sneak-peek', before['family_contract'])
        self.assertNotIn('one-fifth of frame height', before['family_contract'])

    def test_crossing_clip_opens_the_entry_on_camera(self):
        for name, ladder in (('coaxial', _ladder_coaxial(t=4)),
                             ('pan', _ladder_pan(t=4)),
                             ('hard_cut', _ladder_cut(t=4))):
            with self.subTest(variant=name):
                cross = self._contract(ladder, 4)['family_contract']
                self.assertIn('no interior preview before the opening', cross)
                self.assertIn('pushed open on camera', cross)

    def test_bridge_clip_no_longer_assumes_inherited_anchors_are_already_visible(self):
        cross = self._contract(_ladder_coaxial(t=4), 4)['family_contract']
        self.assertIn('become visible for the first time as the entry opens', cross)
        self.assertNotIn('inherited interior anchors continuously scale up', cross)

    def test_adaptive_ladder_opens_the_entry_but_keeps_it_unreadable(self):
        """阶梯式过门（transition_stage）里"开门"本身就是一拍的里程碑，门当然是开的——
        规则在这条路径上的等价形态是：开口里不许有任何可读的室内。"""
        ladder = _ladder_coaxial(t=4)
        ladder[3].update(transition_stage='door_hardware_open', camera_family='exterior',
                         reveal_scope='none', light_source_state='daylight')
        brief = {'mode': 'Threshold',
                 'entrance_topology': {'hardware': ['door leaf', 'hinges', 'latch']}}
        cross = _beat_contract(4, len(ladder), ladder, 'Threshold', self.PACKET, '',
                               parsed_brief=brief)['family_contract']
        self.assertIn('OPENED BUT UNREADABLE', cross)
        self.assertIn('flat unlit darkness', cross)
        # 这条路径不吃闭门契约（门在这一拍就是要打开的）
        self.assertNotIn('SEALED ENTRY (mandatory)', cross)


class TestThresholdMonotonicInheritance(unittest.TestCase):
    """Scheme A: 过门时空单调继承与状态回退测试。"""

    def test_reverted_ceiling_trauma_is_flagged(self):
        bad_prompt = (
            "Static 14mm interior shot. Ceiling cracks and damp runoff cover the upper concrete, "
            "with water leaks from the ceiling. Coarse grey concrete sidewalls remain."
        )
        errors = check_threshold_monotonic_inheritance(bad_prompt, is_first_interior=True)
        self.assertTrue(len(errors) > 0)
        self.assertIn("reverted ceiling/roof trauma", errors[0])

    def test_inherited_completed_roof_passes(self):
        good_prompt = (
            "Static 14mm interior shot. The ceiling overhead inherits the newly completed timber rafter "
            "framing and clean black waterproof membrane from exterior beats with zero ceiling cracks. "
            "Coarse grey concrete sidewalls show original moisture streaks."
        )
        errors = check_threshold_monotonic_inheritance(good_prompt, is_first_interior=True)
        self.assertEqual(errors, [])

    def test_reverted_floor_debris_is_flagged_when_exterior_cleared(self):
        bad_prompt = (
            "Static 14mm interior shot. The floor has fallen timber and rotting leaf piles scattered "
            "across the muddy surface. The ceiling inherits timber rafters."
        )
        errors = check_threshold_monotonic_inheritance(
            bad_prompt, is_first_interior=True, exterior_history=['clearing', 'roofing']
        )
        self.assertTrue(len(errors) > 0)
        self.assertIn("reverted floor debris", errors[0])

    def test_clean_bare_earth_floor_passes(self):
        good_prompt = (
            "Static 14mm interior shot. The floor is clean bare earth swept free of loose debris. "
            "The ceiling inherits completed oak rafters. Coarse concrete walls remain."
        )
        errors = check_threshold_monotonic_inheritance(
            good_prompt, is_first_interior=True, exterior_history=['clearing', 'roofing']
        )
        self.assertEqual(errors, [])

    def test_non_first_interior_skips_check(self):
        # 普通后续室内施工帧由 ordinary milestone check 检查，不触发过门首帧拦截
        bad_prompt = "Static 14mm interior shot. Ceiling cracks exist."
        errors = check_threshold_monotonic_inheritance(bad_prompt, is_first_interior=False)
        self.assertEqual(errors, [])


if __name__ == '__main__':
    unittest.main()
