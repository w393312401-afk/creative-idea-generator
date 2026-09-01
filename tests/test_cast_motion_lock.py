"""帧序列净帧：活物一个都不进 IMAGE，人只活在 VIDEO 里。

这个文件原本锁的是相反的策略——「让 IMAGE 里的人偶动起来」。它是 2026-08-23（微缩树桩屋，
两个人偶全程一个坐姿）和 2026-08-25（河畔观景室 IMG 011-015，人物贴图五帧像素级重合）
两次实测之后，往静帧提示词里逐层加人物姿态句加出来的。

2026-08-31 用户拍板换路：那几层修复全都是在跟参考图掰手腕。帧序列走链式图生图，上一帧
里那个人是**像素**，提示词里「换个姿势」是**文字**，文字赢不了——所以再补一层也是白补。
静帧里根本没有人，就没有可抄的姿态。

于是策略变成两半，必须一起看（见 frame_state.PERSON_FREE_IMAGE_FRAMES）：
  · IMAGE 一律净帧：活物锚点不进正文、cast_action 不进白名单、静帧事实卡不发人物外形。
    净帧靠**字段缺席**实现，不靠「画面里没有人」这种否定句——否定句会触发扩散模型的
    粉色大象效应，fix_image_clean_frame_proactive 专门在删它。
  · VIDEO 承担全部人物表演，并且自带边界：人从画外入画、干完在最后一刻退回画外，
    这样每段视频的首尾锚点帧本来就是空的。

这个文件把两半各自的收口钉住，外加反推侧那条「cast_action 不许写成站位」的体检——那一条
跟净帧无关，照旧生效：VIDEO 侧仍然要人动起来。
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
    """活物锚点整条不进 IMAGE 正文，fix 与 check 两侧一起退。"""

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

    def test_a_living_anchor_never_reaches_the_image_body(self):
        """锚点句是逐帧复读的：把人写进去就是把他钉回去。"""
        clause = pp._canonical_anchor_clause(pp._family_landmarks(PACKET, 'exterior'))
        self.assertNotIn('figurines', clause)
        self.assertNotIn('the same figures in the same costume at the same size', clause)
        # 非活物锚点一条不少。
        self.assertIn('massive mossy forest tree trunk', clause)
        self.assertIn('dilapidated miniature timber stilt hut', clause)

    def test_a_person_free_stanza_is_not_flagged_for_the_missing_living_anchor(self):
        """净帧是对的写法，硬校验不许把它判成锚点漂移、回炉一轮把人写回去。"""
        clause = pp._canonical_anchor_clause(pp._family_landmarks(PACKET, 'exterior'))
        self.assertEqual(pp.check_primary_landmarks_exact_match(clause, PACKET, 'exterior'), [])

    def test_the_anchor_fix_does_not_append_a_stanza_just_for_the_missing_person(self):
        """人不在判据里：正文只缺人的时候不该被认成 missing、白拼一条锚点句。"""
        body = ('Static macro diorama eye-level shot. Locked anchors: massive mossy forest tree '
                'trunk in the upper left of the frame, rising to about three quarters of the frame '
                'height; dilapidated miniature timber stilt hut at the centre of the frame, rising '
                'to about half the frame height.')
        self.assertEqual(pp.fix_primary_landmarks(body, PACKET, 'exterior'), body)

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

    def test_a_missing_non_living_anchor_is_still_flagged(self):
        """退的只是活物那一条：真锚点掉了照样要报。"""
        prompt = ('Static macro diorama eye-level shot. Locked anchors: massive mossy forest tree '
                  'trunk in the upper left of the frame, rising to about three quarters of the frame '
                  'height.')
        errors = pp.check_primary_landmarks_exact_match(prompt, PACKET, 'exterior')
        self.assertTrue(any('timber stilt hut' in e for e in errors), errors)
        self.assertFalse(any('figurines' in e for e in errors), errors)


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


class PersonFreeDeltaTests(unittest.TestCase):
    """状态压缩的白名单里没有 cast_action —— 静帧净帧就是这么实现的。"""

    def test_the_cast_never_reaches_the_still(self):
        out = compile_delta_image_prompt(ORIGINAL, BEAT)
        self.assertNotIn('Cast in frame:', out)
        self.assertNotIn('figurines', out)
        self.assertNotIn('get up off the stone', out)

    def test_the_still_never_says_that_nobody_is_there(self):
        """否定式人物句是粉色大象：说「没有人」的静帧照样会渲出人。"""
        out = compile_delta_image_prompt(ORIGINAL, BEAT).lower()
        for phrase in ('no people', 'no person', 'nobody', 'no workers',
                       'empty of people', 'person-free'):
            self.assertNotIn(phrase, out)

    def test_the_state_fields_still_all_survive(self):
        """撤掉的只有人物那一格，其余白名单一条不少。"""
        out = compile_delta_image_prompt(ORIGINAL, BEAT)
        self.assertIn('Inherited state remains unchanged:', out)
        self.assertIn('Only visible construction delta in this frame:', out)
        self.assertIn('Completed terminal state:', out)
        self.assertIn('Completion extent:', out)
        self.assertIn('Visible physical evidence remains:', out)

    def test_the_clean_frame_guard_is_unconditional_again(self):
        """人物句撤掉之后，'no active construction' 不再需要为它让路。"""
        for beat in (BEAT, dict(BEAT, cast_action='stands atop ladder grinding overhead beams')):
            out = compile_delta_image_prompt(ORIGINAL, beat)
            self.assertIn('no active construction', out)
            self.assertNotIn('beyond the single cast pose', out)

    def test_a_beat_with_nobody_in_frame_compiles_the_same_way(self):
        beat = dict(BEAT)
        beat.pop('cast_action')
        self.assertEqual(compile_delta_image_prompt(ORIGINAL, beat),
                         compile_delta_image_prompt(ORIGINAL, BEAT))

    def test_over_budget_prompts_never_end_mid_sentence(self):
        """旧写法按词硬截，交付过 '…in central soil clearing. Show.' 这样的残句。"""
        beat = dict(BEAT)
        beat['after_state'] = ('the entire trench floor planed flat to a uniform grade ' * 12).strip()
        out = compile_delta_image_prompt(ORIGINAL, beat, max_words=60)
        self.assertTrue(out.endswith('.'), out[-60:])
        self.assertFalse(out.rstrip().endswith(' Show.'), out[-60:])

    def test_the_camera_and_anchor_sentences_are_still_preserved_verbatim(self):
        out = compile_delta_image_prompt(ORIGINAL, BEAT)
        self.assertTrue(out.startswith('Static macro diorama eye-level shot, 50-85mm macro lens.'))
        self.assertIn('Locked anchors: massive mossy forest tree trunk in the upper left', out)


class CastInFrameCheckTests(unittest.TestCase):
    """只判 VIDEO。IMAGE 里没有人是对的，判它就是逼着每张静帧写人。"""

    VIDEO_WITHOUT = ('Use the provided first frame and last frame as exact composition anchors. '
                     'The oversized hand presses a hardwood float across the trench floor.')
    VIDEO_WITH = (VIDEO_WITHOUT + ' The two figurines walk in from off-frame, rise off the stone '
                  'and turn toward the wall, then step back out of frame.')
    IMAGE_PERSON_FREE = 'One clean documentary photograph of the trench floor.'

    def test_video_without_the_cast_is_flagged(self):
        errors = pp.check_cast_in_frame(self.VIDEO_WITHOUT, self.IMAGE_PERSON_FREE, BEAT)
        self.assertEqual(len(errors), 1, errors)
        self.assertTrue(errors[0].startswith('VIDEO'), errors)

    def test_a_person_free_image_is_never_flagged(self):
        """本体：静帧不写人是净帧策略要求的，不是漏写。"""
        self.assertEqual(pp.check_cast_in_frame(self.VIDEO_WITH, self.IMAGE_PERSON_FREE, BEAT), [])

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
    """漏了人偶的 VIDEO 要走定向回炉；IMAGE 侧一个字都不报。"""

    def test_video_cast_omission_is_structural(self):
        errs = pp.check_cast_in_frame('the hand presses a float across the trench floor',
                                      'One clean documentary photograph of the trench floor.',
                                      BEAT)
        structural, rest = pp.split_structural_video_errors(errs)
        self.assertEqual(len(structural), 1, (structural, rest))
        self.assertEqual(rest, [])

    def test_a_person_free_image_produces_no_error_at_all(self):
        errs = pp.check_cast_in_frame('the two figurines walk in, rise off the stone and turn, '
                                      'then step back out of frame',
                                      'One clean documentary photograph of the trench floor.',
                                      BEAT)
        self.assertEqual(errs, [])
