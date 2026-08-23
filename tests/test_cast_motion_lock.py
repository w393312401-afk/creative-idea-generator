"""画面里的活物要动起来：人偶/工人/动物从上一拍的姿态动到这一拍的姿态。

2026-08-23 用户实测（run_replica_5a132d86d42d 微缩树桩屋）：交付视频里两个人偶全程
一个坐姿。beats 里 `cast_action` 每一拍都有值，是下游三层各抹掉它一次：

  1. 抽包把 "seated couple figurines on compacted soil" 登记成第三个 primary_landmark
     —— 活物当锚点，姿态还写进了名字；
  2. fix_primary_landmarks 逐帧复读这句、check_primary_landmarks_exact_match 再要求它
     逐字出现（连方位一起），于是「坐着」被钉死到全序列，composer 让人偶站起来会被
     判成锚点漂移、回炉换回原样；
  3. compile_delta_image_prompt 只保留机位句 + 锚点句，正文按白名单重拼，而白名单里
     没有 cast_action —— 21 张图里只有第 1 帧（不走这条压缩）写了人偶。

于是每一张关键帧里人偶姿态完全一致，首尾帧插值出来必然一动不动。

口径按 skill 包原文（composers/miniature.py 的 CAST IN FRAME 段）：人偶锁的是**身份、
服装、比例**，姿态/朝向/站位每一拍都该变。这个文件把三层各自的修复钉住。
"""

import unittest

import prompt_pipeline as pp
from prompt_pipeline.frame_state import compile_delta_image_prompt
from prompt_pipeline import reverse as rv


PACKET = {
    'primary_landmarks': [
        {'name': 'massive mossy forest tree trunk', 'grid': 'Grid A1', 'z_depth_scale': '75%'},
        {'name': 'dilapidated miniature timber stilt hut', 'grid': 'Grid B2', 'z_depth_scale': '50%'},
        {'name': 'seated couple figurines on compacted soil', 'grid': 'Grid C1', 'z_depth_scale': '20%'},
    ],
}


class LivingAnchorTests(unittest.TestCase):
    """第一、二层：活物锚点不许带姿态，也不许按方位硬判。"""

    def test_pose_is_stripped_from_a_living_anchor_name(self):
        lms = pp._family_landmarks(PACKET, 'exterior')
        names = [lm['name'] for lm in lms]
        self.assertIn('couple figurines on compacted soil', names)
        self.assertNotIn('seated couple figurines on compacted soil', names,
                         '活物锚点的名字里仍然钉着第一帧的姿态')

    def test_packet_on_disk_is_not_mutated(self):
        """消毒发生在读的那一刻：存量 packet 落盘时是什么样还是什么样。"""
        pp._family_landmarks(PACKET, 'exterior')
        self.assertEqual(PACKET['primary_landmarks'][2]['name'],
                         'seated couple figurines on compacted soil')

    def test_non_living_anchors_keep_their_bearing(self):
        clause = pp._canonical_anchor_clause(pp._family_landmarks(PACKET, 'exterior'))
        self.assertIn('massive mossy forest tree trunk in the upper left of the frame', clause)

    def test_living_anchor_locks_scale_and_frees_pose(self):
        clause = pp._canonical_anchor_clause(pp._family_landmarks(PACKET, 'exterior'))
        self.assertIn('the same figures in the same costume at the same size', clause)
        self.assertIn('rising to about a sixth of the frame height', clause)
        self.assertIn('free to take a new pose and a new spot this frame', clause)
        # 方位不写：会走动的东西钉方位等于钉站位。
        self.assertNotIn('couple figurines on compacted soil in the lower left', clause)

    def test_canonical_clause_passes_its_own_checks(self):
        clause = pp._canonical_anchor_clause(pp._family_landmarks(PACKET, 'exterior'))
        self.assertEqual(pp.check_primary_landmarks_exact_match(clause, PACKET, 'exterior'), [])
        self.assertEqual(pp.check_anchor_scale_lock(clause, PACKET, 'exterior'), [])

    def test_a_moved_figurine_is_not_flagged_as_anchor_drift(self):
        """人偶站起来、换到画面另一侧——这是对的，校验不许拦。"""
        prompt = ('Static macro diorama eye-level shot. Locked anchors: massive mossy forest tree '
                  'trunk in the upper left of the frame, rising to about three quarters of the frame '
                  'height; dilapidated miniature timber stilt hut at the centre of the frame, rising '
                  'to about half the frame height; couple figurines on compacted soil, the same '
                  'figures in the same costume at the same size, rising to about a sixth of the frame '
                  'height, now standing at the right edge of the plot facing the new wall.')
        self.assertEqual(pp.check_primary_landmarks_exact_match(prompt, PACKET, 'exterior'), [])

    def test_a_missing_living_anchor_is_still_flagged(self):
        """放开的只是姿态与方位：人偶整个消失了照样要报。"""
        prompt = ('Static macro diorama eye-level shot. Locked anchors: massive mossy forest tree '
                  'trunk in the upper left of the frame, rising to about three quarters of the frame '
                  'height; dilapidated miniature timber stilt hut at the centre of the frame, rising '
                  'to about half the frame height.')
        errors = pp.check_primary_landmarks_exact_match(prompt, PACKET, 'exterior')
        self.assertTrue(any('couple figurines' in e for e in errors), errors)


BEAT = {
    'operation': 'repair',
    'milestone_name': 'smooth clay subgrade graded',
    'after_state': '100% of trench floor planed flat to uniform grade.',
    'completion_extent': '100% of trench bed smoothed',
    'preserve_state': 'Sheared trench sidewalls and spoil mounds preserved.',
    'persistent_traces': ['float drag ridges across the clay floor', 'scraped berm line'],
    'cast_action': 'the two figurines get up off the stone and turn to face the new wall',
}

ORIGINAL = ('Static macro diorama eye-level shot, 50-85mm macro lens. Locked anchors: massive '
            'mossy forest tree trunk in the upper left of the frame, rising to about three quarters '
            'of the frame height. The hand works the trench floor with a hardwood float.')


class DeltaPromptCastTests(unittest.TestCase):
    """第三层：状态压缩的白名单必须收 cast_action。"""

    def test_cast_action_survives_the_delta_compile(self):
        out = compile_delta_image_prompt(ORIGINAL, BEAT)
        self.assertIn('Cast in frame:', out)
        self.assertIn('get up off the stone', out)

    def test_cast_line_carries_the_identity_lock(self):
        out = compile_delta_image_prompt(ORIGINAL, BEAT)
        self.assertIn('same identity, costume and scale as before', out)
        self.assertIn('never touching the work', out)

    def test_no_cast_line_when_nothing_alive_is_in_frame(self):
        beat = dict(BEAT)
        beat.pop('cast_action')
        self.assertNotIn('Cast in frame:', compile_delta_image_prompt(ORIGINAL, beat))

    def test_over_budget_prompts_never_end_mid_sentence(self):
        """旧写法按词硬截，交付过 '…in central soil clearing. Show.' 这样的残句。"""
        beat = dict(BEAT)
        beat['after_state'] = ('the entire trench floor planed flat to a uniform grade ' * 12).strip()
        out = compile_delta_image_prompt(ORIGINAL, beat, max_words=60)
        self.assertTrue(out.endswith('.'), out[-60:])
        self.assertFalse(out.rstrip().endswith(' Show.'), out[-60:])

    def test_the_cast_line_outranks_the_optional_state_fields(self):
        """预算紧张时先让位的是 traces / extent，人偶姿态句不让位。"""
        out = compile_delta_image_prompt(ORIGINAL, BEAT, max_words=90)
        self.assertIn('Cast in frame:', out)
        self.assertNotIn('Visible physical evidence remains:', out)


class CastInFrameCheckTests(unittest.TestCase):
    """VIDEO 侧：漏听了 CAST IN FRAME 要变成一次回炉，不是静默交付。"""

    VIDEO_WITHOUT = ('Use the provided first frame and last frame as exact composition anchors. '
                     'The oversized hand presses a hardwood float across the trench floor.')
    VIDEO_WITH = (VIDEO_WITHOUT + ' The two figurines rise off the stone and turn toward the wall.')
    IMAGE_WITH = 'Cast in frame: the two figurines get up off the stone — same identity, costume and scale.'

    def test_video_without_the_cast_is_flagged(self):
        errors = pp.check_cast_in_frame(self.VIDEO_WITHOUT, self.IMAGE_WITH, BEAT)
        self.assertEqual(len(errors), 1, errors)
        self.assertTrue(errors[0].startswith('VIDEO'), errors)

    def test_both_carrying_the_cast_is_clean(self):
        self.assertEqual(pp.check_cast_in_frame(self.VIDEO_WITH, self.IMAGE_WITH, BEAT), [])

    def test_a_beat_with_nobody_in_frame_is_never_flagged(self):
        beat = dict(BEAT)
        beat.pop('cast_action')
        self.assertEqual(pp.check_cast_in_frame(self.VIDEO_WITHOUT, ORIGINAL, beat), [])


class StaticCastActionWarningTests(unittest.TestCase):
    """反推侧：cast_action 写成站位而不是动作，要在体检里报出来。"""

    @staticmethod
    def _beat(bid, cast):
        return {
            'id': bid, 'stage': 'build', 'operation': 'masonry',
            'workers_present': True, 'cast_action': cast,
            'visible_action': 'the hand lays a course of blocks',
        }

    def _codes(self, cast):
        buckets, _ = rv._scan_beat_craft([self._beat('B04', cast)])
        return buckets

    def test_remain_wording_is_flagged(self):
        buckets = self._codes('two miniature figurines remain standing at the upper-left perimeter')
        self.assertEqual(buckets['static_cast_action'], ['B04'])

    def test_chinese_static_wording_is_flagged(self):
        buckets = self._codes('两个人偶保持原样，站位不变')
        self.assertEqual(buckets['static_cast_action'], ['B04'])

    def test_a_real_move_is_not_flagged(self):
        buckets = self._codes('the two figurines get up off the stone and turn to face the new wall')
        self.assertEqual(buckets['static_cast_action'], [])

    def test_the_symptom_code_is_repairable(self):
        """报了却不在可回炉码里，等于报完没人管。"""
        self.assertIn('static_cast_action', rv.CRAFT_REFINE_CODES)
        self.assertIn('static_cast_action', rv._CRAFT_ISSUE_BRIEFS)


if __name__ == '__main__':
    unittest.main()


class CastErrorRoutingTests(unittest.TestCase):
    """漏了人偶的 VIDEO 要走定向回炉，IMAGE 侧只留痕（它有确定性补句）。"""

    def test_video_cast_omission_is_structural(self):
        errs = pp.check_cast_in_frame('the hand presses a float across the trench floor',
                                      'Cast in frame: the two figurines get up off the stone.',
                                      BEAT)
        structural, rest = pp.split_structural_video_errors(errs)
        self.assertEqual(len(structural), 1, (structural, rest))
        self.assertEqual(rest, [])

    def test_image_cast_omission_only_leaves_a_trace(self):
        errs = pp.check_cast_in_frame('the two figurines rise off the stone and turn',
                                      'One clean documentary photograph of the trench floor.',
                                      BEAT)
        structural, rest = pp.split_structural_video_errors(errs)
        self.assertEqual(structural, [])
        self.assertEqual(len(rest), 1, rest)
