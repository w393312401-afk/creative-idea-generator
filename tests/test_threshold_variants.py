# -*- coding: utf-8 -*-
"""TBCP v2 过门协议修订（docs/threshold_protocol_revision.md）的单元测试：

- 三态镜头族扩展为四态（exterior/sill/vestibule/interior），pan 变体三段桥
- 声明式硬切（hard_cut / [CUT] 槽位）：族计算、族锚豁免、配对跳过、门禁预期缺失、
  血统分段
- P0 门框出画确定性校验（check_interior_door_clearance）
- Bridge-3 摇镜 clip 的过程内容校验（check_video_process_content is_turn）
- brief 变体声明归一化（threshold_variant/threshold_elevated）
"""
import os
import json
import shutil
import tempfile
import unittest

from prompt_pipeline import (
    beat_space_family,
    image_space_family,
    family_anchor_seq,
    threshold_variant,
    threshold_elevated,
    normalize_beat_ladder,
    check_interior_door_clearance,
    check_video_process_content,
    validate_beat_prompts,
    _build_partial_prompt_block,
    _parse_prompt_slots,
    _stage_scope_ladder_violations,
    HARD_CUT_VIDEO_PLACEHOLDER,
    BRIDGE_HOLD_VIDEO_PLACEHOLDER,
    _beat_contract,
    fix_video_opening,
    check_video_opening,
)
from video_generator import plan_video_slots, merge_project_videos, PartialMergeBlocked
from frame_generator import update_manifest_stale_status


def _ladder_coaxial(n=8, t=4):
    ladder = []
    for i in range(1, n + 1):
        b = {'index': i, 'operation': 'repair', 'description': f'step {i}', 'bridge_stage': None}
        if i == t:
            b.update(operation='threshold', bridge_stage=1)
        elif i == t + 1:
            b.update(operation='threshold', bridge_stage=2)
        ladder.append(b)
    return ladder


def _ladder_pan(n=9, t=4):
    ladder = _ladder_coaxial(n, t)
    ladder[t + 1].update(operation='threshold', bridge_stage=3, turn_direction='right')
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
    def test_coaxial_two_bridge_unchanged(self):
        ladder = _ladder_coaxial(t=4)
        self.assertEqual(beat_space_family(ladder, 3), 'exterior')
        self.assertEqual(beat_space_family(ladder, 4), 'sill')
        self.assertEqual(beat_space_family(ladder, 5), 'interior')
        self.assertEqual(beat_space_family(ladder, 8), 'interior')

    def test_pan_three_bridge_has_vestibule(self):
        ladder = _ladder_pan(t=4)
        self.assertEqual(beat_space_family(ladder, 3), 'exterior')
        self.assertEqual(beat_space_family(ladder, 4), 'sill')
        self.assertEqual(beat_space_family(ladder, 5), 'vestibule')
        self.assertEqual(beat_space_family(ladder, 6), 'interior')
        self.assertEqual(beat_space_family(ladder, 9), 'interior')

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
    def test_two_bridge_metas(self):
        videos = {3: {'body': 'v', 'meta': ''}, 4: {'body': 'v', 'meta': 'BRIDGE'},
                  5: {'body': 'v', 'meta': 'BRIDGE'}, 6: {'body': 'v', 'meta': ''}}
        self.assertEqual(image_space_family(videos, 4), 'exterior')
        self.assertEqual(image_space_family(videos, 5), 'sill')
        self.assertEqual(image_space_family(videos, 6), 'interior')

    def test_three_bridge_metas_vestibule(self):
        videos = {4: {'body': 'v', 'meta': 'BRIDGE'}, 5: {'body': 'v', 'meta': 'BRIDGE'},
                  6: {'body': 'v', 'meta': 'BRIDGE TURN'}, 7: {'body': 'v', 'meta': ''}}
        self.assertEqual(image_space_family(videos, 4), 'exterior')
        self.assertEqual(image_space_family(videos, 5), 'sill')
        self.assertEqual(image_space_family(videos, 6), 'vestibule')
        self.assertEqual(image_space_family(videos, 7), 'interior')
        self.assertEqual(image_space_family(videos, 8), 'interior')

    def test_cut_meta(self):
        videos = {3: {'body': 'v', 'meta': ''}, 4: {'body': HARD_CUT_VIDEO_PLACEHOLDER, 'meta': 'CUT'},
                  5: {'body': 'v', 'meta': ''}}
        self.assertEqual(image_space_family(videos, 4), 'exterior')
        self.assertEqual(image_space_family(videos, 5), 'interior')
        self.assertEqual(image_space_family(videos, 6), 'interior')


class TestFamilyAnchorSeq(unittest.TestCase):
    def test_cut_counts_as_family_boundary(self):
        videos = {3: {'body': 'v', 'meta': ''}, 4: {'body': 'v', 'meta': 'CUT'},
                  5: {'body': 'v', 'meta': ''}}
        self.assertEqual(family_anchor_seq(videos, 3), 1)
        self.assertEqual(family_anchor_seq(videos, 6), 5)

    def test_turn_bridge_moves_anchor_to_settle(self):
        videos = {4: {'body': 'v', 'meta': 'BRIDGE'}, 5: {'body': 'v', 'meta': 'BRIDGE'},
                  6: {'body': 'v', 'meta': 'BRIDGE TURN'}, 7: {'body': 'v', 'meta': ''}}
        # pan 变体的族锚是摇镜落点帧（B-3 产物）
        self.assertEqual(family_anchor_seq(videos, 9), 7)


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
            {'index': 2, 'operation': 'threshold', 'bridge_stage': '3', 'turn_direction': 'Right'},
            {'index': 3, 'operation': 'repair', 'turn_direction': 'sideways'},
        ])
        self.assertIs(ladder[0]['hard_cut'], True)
        self.assertEqual(ladder[1]['bridge_stage'], 3)
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
    def _ladder(self, scopes, reward=True):
        ladder = [{'index': i + 1, 'operation': 'repair', 'stage_scope': s}
                  for i, s in enumerate(scopes)]
        if reward:
            ladder.append({'index': len(ladder) + 1, 'operation': 'reward', 'stage_scope': 'large'})
        return ladder

    def test_exactly_one_large_and_two_to_three_small_passes(self):
        ladder = self._ladder(['large', 'small', 'small', 'default', 'default'])
        self.assertEqual(_stage_scope_ladder_violations(ladder), [])

    def test_three_small_also_passes(self):
        ladder = self._ladder(['large', 'small', 'small', 'small', 'default'])
        self.assertEqual(_stage_scope_ladder_violations(ladder), [])

    def test_zero_large_is_a_violation(self):
        ladder = self._ladder(['small', 'small', 'default', 'default'])
        violations = _stage_scope_ladder_violations(ladder)
        self.assertTrue(any('stage_scope="large"' in v for v in violations))

    def test_two_large_is_a_violation(self):
        ladder = self._ladder(['large', 'large', 'small', 'small', 'default'])
        violations = _stage_scope_ladder_violations(ladder)
        self.assertTrue(any('stage_scope="large"' in v for v in violations))

    def test_small_count_out_of_range_is_a_violation(self):
        ladder = self._ladder(['large', 'small', 'default', 'default', 'default'])
        violations = _stage_scope_ladder_violations(ladder)
        self.assertTrue(any('stage_scope="small"' in v for v in violations))

        ladder = self._ladder(['large', 'small', 'small', 'small', 'small', 'default'])
        violations = _stage_scope_ladder_violations(ladder)
        self.assertTrue(any('stage_scope="small"' in v for v in violations))

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

    def test_small_pool_narrows_small_quota_range(self):
        # 只有 2 个合格拍：1 large + 1 small 已经是这个池子能装下的上限，不该要求 2-3 个 small。
        ladder = self._ladder(['large', 'small'])
        self.assertEqual(_stage_scope_ladder_violations(ladder), [])

    def test_single_eligible_beat_only_checks_large(self):
        ladder = self._ladder(['large'])
        self.assertEqual(_stage_scope_ladder_violations(ladder), [])

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

    def test_vestibule_also_checked(self):
        errs = check_interior_door_clearance('The threshold edges hug the boundaries.', family='vestibule')
        self.assertTrue(errs)

    def test_exterior_and_sill_exempt(self):
        p = 'The open doorway sits in Grid B2 with the sill line crossing the lower third.'
        self.assertEqual(check_interior_door_clearance(p, family='exterior'), [])
        self.assertEqual(check_interior_door_clearance(p, family='sill'), [])


class TestTurnVideoProcessCheck(unittest.TestCase):
    def test_turn_requires_pan_description(self):
        errs = check_video_process_content(
        'The camera holds while dust settles softly.', is_bridge=True, is_turn=True)
        self.assertTrue(errs)
        errs = check_video_process_content(
            'One smooth horizontal pan to the right sweeps onto the aisle axis.',
            is_bridge=True, is_turn=True)
        self.assertEqual(errs, [])

    def test_normal_bridge_still_requires_translation(self):
        errs = check_video_process_content('Nothing moves.', is_bridge=True)
        self.assertTrue(errs)
        errs = check_video_process_content('The camera pushes forward across the sill.', is_bridge=True)
        self.assertEqual(errs, [])


class TestValidateBeatPromptsVariants(unittest.TestCase):
    PACKET = {'camera_dna': '', 'primary_landmarks': [], 'frame_boundaries': {}}

    def test_hard_cut_beat_skips_video_checks(self):
        beat = {'index': 4, 'operation': 'threshold', 'hard_cut': True, 'bridge_stage': None}
        errs = validate_beat_prompts(
            4, HARD_CUT_VIDEO_PLACEHOLDER,
            'Static tripod shot inside; camera pitch locked level; the central vanishing axis stays centered.',
            self.PACKET, 'Threshold', False, True, beat=beat, family='interior')
        self.assertEqual([e for e in errs if 'VIDEO' in e or 'video' in e], [])

    def test_turn_beat_allows_pan_wording(self):
        beat = {'index': 6, 'operation': 'threshold', 'bridge_stage': 3, 'turn_direction': 'right'}
        video = ('The camera pans smoothly to the right from the vestibule point, the window band '
                 'sliding in from the frame edge, completely sterile of workers throughout.')
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
        ladder = _ladder_pan(n=7, t=3)
        images = {i: f'image {i}' for i in range(1, 9)}
        videos = {i: f'video {i}' for i in range(1, 8)}
        f_imgs, f_vids, block = _build_partial_prompt_block(images, videos, ladder)
        # TBCP v3: bridge_stage=1 (HOLD, discarded internal placeholder) and bridge_stage=2
        # (SPAN, the sole visible merged crossing clip) get distinct meta tags.
        self.assertEqual(f_vids[3]['meta'], 'BRIDGE HOLD')
        self.assertEqual(f_vids[4]['meta'], 'BRIDGE SPAN')
        self.assertEqual(f_vids[5]['meta'], 'BRIDGE TURN')
        # 解析回读保留 meta
        p_imgs, p_vids = _parse_prompt_slots(block)
        self.assertEqual(p_vids[5]['meta'], 'BRIDGE TURN')

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
    def test_cut_slot_skipped_others_generate(self):
        frames = self._make_frames(4)
        videos = {1: {'body': 'v1', 'meta': ''},
                  2: {'body': HARD_CUT_VIDEO_PLACEHOLDER, 'meta': 'CUT'},
                  3: {'body': 'v3', 'meta': ''}}
        plans = plan_video_slots(videos, frames, {}, self.videos_dir)
        self.assertEqual([p['action'] for p in plans], ['generate', 'skip_cut', 'generate'])
        # 切槽不因缺帧/降级被误判为 blocked
        self.assertIn('硬切', plans[1]['reason'])

    def test_bridge_turn_slot_not_mistaken_for_cut(self):
        frames = self._make_frames(3)
        videos = {1: {'body': 'v1', 'meta': 'BRIDGE TURN'}, 2: {'body': 'v2', 'meta': ''}}
        plans = plan_video_slots(videos, frames, {}, self.videos_dir)
        self.assertEqual([p['action'] for p in plans], ['generate', 'generate'])


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
        # 槽位 2 是声明式硬切；槽位 3 真缺失 → 门禁只报 3，不报 2
        self._write_manifest(4, [
            {'slot': 1, 'status': 'success', 'file': 'videos/vid_001.mp4'},
            {'slot': 2, 'status': 'skipped_cut'},
        ])
        with self.assertRaises(PartialMergeBlocked) as ctx:
            merge_project_videos(self.tmp)
        self.assertNotIn(2, ctx.exception.missing)
        self.assertIn(3, ctx.exception.missing)


class TestStaleLineageSegments(_TmpDirCase):
    def _frames(self, metas):
        return [{'sequence': i, 'slot': i, 'meta': metas.get(i, '')} for i in sorted(set(list(metas) + list(range(1, 7))))]

    def test_regen_before_cut_does_not_stale_after_cut(self):
        manifest = {'frames': self._frames({4: 'CUT'})}
        update_manifest_stale_status(manifest, self.tmp, regenerated_sequences=[2], finalize=True)
        by_seq = {f['sequence']: f for f in manifest['frames']}
        self.assertTrue(by_seq[3].get('stale_lineage'))
        # 切点帧（t2i 新链头）及其后不派生自旧链，不得被标 stale
        self.assertFalse(by_seq[4].get('stale_lineage'))
        self.assertFalse(by_seq[5].get('stale_lineage'))
        self.assertFalse(by_seq[6].get('stale_lineage'))

    def test_regen_after_cut_stales_only_its_segment(self):
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
# TBCP v3: merged crossing clip (bridge_stage=1 HOLD discarded, bridge_stage=2
# SPAN is the sole visible clip, redirected to the pre-HOLD exterior anchor) +
# first-interior-reveal decay-state wording.
# ─────────────────────────────────────────────────────────────────────────

class TestBeatContractBridgeFlags(unittest.TestCase):
    PACKET = {'camera_dna': '', 'primary_landmarks': [], 'frame_boundaries': {}}

    def _contract(self, ladder, i, total_beats=None, mode='Threshold'):
        return _beat_contract(i, total_beats or len(ladder), ladder, mode, self.PACKET, '')

    def test_coaxial_hold_and_span_flags(self):
        ladder = _ladder_coaxial(n=6, t=3)  # bridge_stage 1 at beat 3, 2 at beat 4
        hold = self._contract(ladder, 3)
        self.assertTrue(hold['is_bridge_hold'])
        self.assertFalse(hold['is_bridge_span'])
        self.assertFalse(hold['is_first_interior_reveal'])

        span = self._contract(ladder, 4)
        self.assertFalse(span['is_bridge_hold'])
        self.assertTrue(span['is_bridge_span'])
        self.assertEqual(span['family'], 'interior')
        self.assertTrue(span['is_first_interior_reveal'])
        # SPAN's video anchors from the pre-HOLD exterior beat (i-1 = 3), not
        # its own preceding IMAGE (the internal-only Sill Handoff frame).
        self.assertIn('IMAGE 3', span['family_contract'])
        self.assertIn('IMAGE 5', span['family_contract'])

        # A later, ordinary interior beat (no bridge_stage) must NOT carry the
        # first-reveal decay clause or the HOLD/SPAN flags.
        later = self._contract(ladder, 5)
        self.assertFalse(later['is_bridge_hold'])
        self.assertFalse(later['is_bridge_span'])
        self.assertFalse(later['is_first_interior_reveal'])
        self.assertNotIn('UNTOUCHED TRAUMA STATE', later['anchor_rule'])

    def test_pan_span_is_vestibule_and_turn_is_first_reveal(self):
        ladder = _ladder_pan(n=7, t=3)  # bridge_stage 1/2/3 at beats 3/4/5
        span = self._contract(ladder, 4)
        self.assertTrue(span['is_bridge_span'])
        self.assertEqual(span['family'], 'vestibule')
        # Literal scope is family=='interior', not 'vestibule' — but the user
        # confirmed the vestibule frame should still get a (lighter) decay clause
        # via the family_contract bullet and the vestibule anchor_rule branch.
        self.assertFalse(span['is_first_interior_reveal'])
        self.assertIn('untouched weathering', span['anchor_rule'])
        self.assertIn('First interior reveal', span['family_contract'])

        turn = self._contract(ladder, 5)
        self.assertTrue(turn['is_turn'])
        self.assertEqual(turn['family'], 'interior')
        self.assertTrue(turn['is_first_interior_reveal'])
        self.assertIn('UNTOUCHED TRAUMA STATE', turn['anchor_rule'])

    def test_decay_clause_absent_for_hard_cut_else_branch(self):
        # hard_cut already has its own dedicated decay wording (item 4 of its
        # anchor_rule) via the is_cut branch, not the first-reveal clause.
        ladder = _ladder_cut(n=6, t=3)
        cut = self._contract(ladder, 3)
        self.assertTrue(cut['is_cut'])
        self.assertIn('untouched pre-construction trauma', cut['anchor_rule'])
        self.assertNotIn('UNTOUCHED TRAUMA STATE', cut['anchor_rule'])

    def test_ordinary_interior_beat_states_door_clearance_only_once(self):
        # 2026-07-20 实机复盘：普通室内拍(非首现)的 anchor_rule 曾经和下面
        # family_contract 的"Door clearance (mandatory)"条目重复表述同一条门框出画
        # 要求，导致 LLM 在生成的 IMAGE 正文里把整句 camera_dna 复读了两遍（img_5/7/
        # 9/10/11 实例）。现在这条要求只应该出现在 family_contract 里一次，
        # anchor_rule 不应再重复它。
        ladder = _ladder_coaxial(n=6, t=3)  # bridge_stage 1 at beat 3, 2 at beat 4
        later = self._contract(ladder, 5)  # ordinary interior beat, not first reveal
        self.assertNotIn('DOOR CLEARANCE', later['anchor_rule'])
        self.assertEqual(later['family_contract'].count('Door clearance (mandatory)'), 1)


class TestVideoOpeningFirstFrameIndex(unittest.TestCase):
    def test_fix_video_opening_default_binds_i_to_ip1(self):
        out = fix_video_opening(5, 'some prior text')
        self.assertIn('Use IMAGE 5 as the actual first-frame image', out)
        self.assertIn('IMAGE 6 as the actual last-frame image', out)

    def test_fix_video_opening_override_binds_custom_first_frame(self):
        out = fix_video_opening(5, 'some prior text', first_frame_index=3)
        self.assertIn('Use IMAGE 3 as the actual first-frame image', out)
        self.assertIn('IMAGE 6 as the actual last-frame image', out)

    def test_check_video_opening_matches_override(self):
        prompt = fix_video_opening(5, '', first_frame_index=3)
        self.assertEqual(check_video_opening(5, prompt, first_frame_index=3), [])
        # Without the matching override, the default IMAGE 5 binding is expected
        # and this prompt (bound to IMAGE 3) correctly fails it.
        self.assertTrue(check_video_opening(5, prompt))

    def test_validate_beat_prompts_hold_beat_skips_all_video_checks(self):
        beat = {'index': 3, 'operation': 'threshold', 'bridge_stage': 1}
        packet = {'camera_dna': '', 'primary_landmarks': [], 'frame_boundaries': {}}
        errs = validate_beat_prompts(
            3, BRIDGE_HOLD_VIDEO_PLACEHOLDER,
            'This IMAGE is the TBCP Sill Handoff frame; camera pitch locked level; '
            'the central vanishing axis stays centered.',
            packet, 'Threshold', False, True, beat=beat, family='sill')
        self.assertEqual([e for e in errs if 'VIDEO' in e or 'video' in e.lower()], [])


class TestPlanVideoSlotsBridgeHold(_TmpDirCase):
    def test_hold_slot_skipped_span_slot_redirects_start_anchor(self):
        # Beats: 1=exterior, 2=HOLD (bridge_stage 1), 3=SPAN (bridge_stage 2).
        # SPAN's video slot (3) redirects its start anchor to slot-1=2, the exterior
        # IMAGE produced by beat 1 — NOT slot 3 (its own default start, the internal
        # Sill Handoff frame HOLD produced) and NOT slot 1.
        frames = self._make_frames(4)  # slots 1..4
        videos = {
            1: {'body': 'exterior approach', 'meta': ''},
            2: {'body': 'HOLD placeholder', 'meta': 'BRIDGE HOLD'},
            3: {'body': 'Use IMAGE 2 as the actual first-frame image and IMAGE 4 as the '
                        'actual last-frame image.', 'meta': 'BRIDGE SPAN'},
        }
        plans = plan_video_slots(videos, frames, {}, self.videos_dir)
        by_slot = {p['slot']: p for p in plans}

        self.assertEqual(by_slot[2]['action'], 'skip_bridge_hold')
        self.assertIn('BRIDGE HOLD', by_slot[2]['reason'])

        span = by_slot[3]
        self.assertEqual(span['action'], 'generate')
        self.assertEqual(span['start_anchor_slot'], 2)  # slot 3 - 1
        self.assertEqual(span['start_frame'], frames[2])
        self.assertEqual(span['end_frame'], frames[4])
        # The rewritten two-card prompt binds the true start anchor (IMAGE 2) to the
        # first-frame card, not the discarded HOLD beat's internal Sill Handoff frame.
        self.assertIn('IMAGE 1 as the actual first-frame image', span['prompt'])

    def test_span_slot_blocked_when_start_anchor_frame_missing(self):
        frames = self._make_frames(4)
        del frames[2]  # slot 2 (start_anchor_slot = slot 3 - 1) missing
        videos = {2: {'body': 'HOLD', 'meta': 'BRIDGE HOLD'},
                  3: {'body': 'SPAN', 'meta': 'BRIDGE SPAN'}}
        plans = plan_video_slots(videos, frames, {}, self.videos_dir)
        by_slot = {p['slot']: p for p in plans}
        self.assertEqual(by_slot[3]['action'], 'blocked')
        self.assertIn('IMAGE 2', by_slot[3]['reason'])


class TestMergeGateBridgeHold(_TmpDirCase):
    def _write_manifest(self, frames_n, videos_entries):
        frames = []
        for i in range(1, frames_n + 1):
            frames.append({'slot': i, 'sequence': i,
                           'file': os.path.relpath(os.path.join(self.frames_dir, f'img_{i:03d}.webp'),
                                                   self.tmp).replace('\\', '/')})
        manifest = {'title': 't', 'frames': frames, 'videos': videos_entries}
        with open(os.path.join(self.tmp, 'manifest.json'), 'w', encoding='utf-8') as f:
            json.dump(manifest, f)

    def test_skipped_bridge_hold_is_expected_gap_not_missing(self):
        self._make_frames(4)
        # 槽位 2 是 HOLD 内部占位；槽位 4 真缺失 → 门禁只报 4，不报 2
        self._write_manifest(4, [
            {'slot': 1, 'status': 'success', 'file': 'videos/vid_001.mp4'},
            {'slot': 2, 'status': 'skipped_bridge_hold'},
        ])
        with self.assertRaises(PartialMergeBlocked) as ctx:
            merge_project_videos(self.tmp)
        self.assertNotIn(2, ctx.exception.missing)
        self.assertIn(3, ctx.exception.missing)


if __name__ == '__main__':
    unittest.main()
