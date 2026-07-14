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
    HARD_CUT_VIDEO_PLACEHOLDER,
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
        self.assertEqual(f_vids[3]['meta'], 'BRIDGE')
        self.assertEqual(f_vids[4]['meta'], 'BRIDGE')
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


if __name__ == '__main__':
    unittest.main()
