"""IMAGE 侧缺陷合并回炉（2026-08-22）。

此前每一条 IMAGE 缺陷各有一次定向回炉，串成一条最长 8 段的链：一拍命中三条就连打三次
模型（实测 145 拍里 26 拍 ≥2 条），而且链是有序的——后一段重写可以把前一段的修复悄悄
改回去，reverify_beat_repairs 只能事后把残留报出来、改不回去。

合并成一次调用后，采纳条件必须**不比串行链宽松**，这正是本文件要钉住的：
- 至少修好一条才算数（一条没修好 = 保留原稿，和单发回炉复验不过是同一条保底）；
- 一条新的都不许引入（这是串行链做不到的那条，反而更严）；
- 单类缺陷只打一次（成本与从前的单发回炉持平），多类缺陷才允许带反馈重试一轮。
"""
import unittest
from unittest.mock import patch

import prompt_pipeline as pp


# 两类容易构造的确定性缺陷：
# - placeholder：正文里出现 VIDEO 专用词 'sterile'（check_image_decay_placeholder）
# - stage_scope：stage_scope='large' 却没在自己那句里claim全覆盖（check_stage_scope_wording）
CAMERA = "A static ultra-wide tripod shot at 1.6m height."
BOTH_BROKEN = (f"{CAMERA} The frame is sterile of activity. "
               "New oak boards now cover part of the floor near the doorway.")
BOTH_FIXED = (f"{CAMERA} Warm light rakes the bare walls. "
              "New oak boards now cover the entire floor area wall to wall.")
ONLY_PLACEHOLDER_FIXED = (f"{CAMERA} Warm light rakes the bare walls. "
                          "New oak boards now cover part of the floor near the doorway.")
BEAT = {'index': 3, 'operation': 'flooring'}


def _defect_kwargs():
    return dict(beat=BEAT, stage_scope='large')


class TestCollectImageDefects(unittest.TestCase):
    def test_detects_each_class_independently(self):
        self.assertEqual(
            sorted(pp.collect_image_defects(3, BOTH_BROKEN, **_defect_kwargs())),
            ['placeholder', 'stage_scope'])
        self.assertEqual(pp.collect_image_defects(3, BOTH_FIXED, **_defect_kwargs()), {})

    def test_clean_prompt_has_no_defects(self):
        self.assertEqual(pp.collect_image_defects(3, BOTH_FIXED, **_defect_kwargs()), {})


class TestRepairImageDefects(unittest.TestCase):
    def _repair(self, prompt, replies):
        defects = pp.collect_image_defects(3, prompt, **_defect_kwargs())
        with patch.object(pp, '_chat', side_effect=replies) as chat:
            out, adopted, residual = pp.repair_image_defects(
                {}, 3, prompt, defects, **_defect_kwargs())
        return out, adopted, residual, chat

    def test_two_defects_fixed_in_one_call(self):
        out, adopted, residual, chat = self._repair(BOTH_BROKEN, [BOTH_FIXED])
        self.assertTrue(adopted)
        self.assertEqual(out, BOTH_FIXED)
        self.assertEqual(residual, {})
        self.assertEqual(chat.call_count, 1, '两类缺陷应当一次调用修完，而不是各打一次')

    def test_partial_fix_is_adopted_with_residual_reported(self):
        """修好一条、剩一条 → 采纳并如实报出残留（等价于串行链里一段成功一段失败）。"""
        out, adopted, residual, chat = self._repair(BOTH_BROKEN, [ONLY_PLACEHOLDER_FIXED])
        self.assertTrue(adopted)
        self.assertEqual(out, ONLY_PLACEHOLDER_FIXED)
        self.assertEqual(sorted(residual), ['stage_scope'])
        self.assertEqual(chat.call_count, 1)

    def test_rewrite_that_fixes_nothing_keeps_the_original(self):
        """一条都没修好 → 保留原稿。多类缺陷时允许带着拒绝原因重试一轮。"""
        out, adopted, residual, chat = self._repair(BOTH_BROKEN, [BOTH_BROKEN, BOTH_BROKEN])
        self.assertFalse(adopted)
        self.assertEqual(out, BOTH_BROKEN)
        self.assertEqual(sorted(residual), ['placeholder', 'stage_scope'])
        self.assertEqual(chat.call_count, 2)
        second_user = chat.call_args_list[1].args[2]
        self.assertIn('Your previous attempt was rejected', second_user)

    def test_second_attempt_can_still_succeed(self):
        out, adopted, residual, chat = self._repair(BOTH_BROKEN, [BOTH_BROKEN, BOTH_FIXED])
        self.assertTrue(adopted)
        self.assertEqual(out, BOTH_FIXED)
        self.assertEqual(chat.call_count, 2)

    def test_single_defect_never_costs_a_second_call(self):
        """单类缺陷的调用成本必须与从前的单发回炉持平（1 次），不能因为合并反而变贵。"""
        prompt = ONLY_PLACEHOLDER_FIXED  # 只缺全覆盖措辞
        self.assertEqual(sorted(pp.collect_image_defects(3, prompt, **_defect_kwargs())),
                         ['stage_scope'])
        out, adopted, residual, chat = self._repair(prompt, [prompt])
        self.assertFalse(adopted)
        self.assertEqual(chat.call_count, 1)

    def test_rewrite_introducing_a_new_defect_class_is_rejected(self):
        """修一条踩坏另一条 = 失败的修复。串行链做不到这条，合并版必须做到。"""
        prompt = ONLY_PLACEHOLDER_FIXED           # 只有 stage_scope 一条
        broke_it = (f"{CAMERA} The frame is sterile of activity. "
                    "New oak boards now cover the entire floor area wall to wall.")
        # 这一稿修好了 stage_scope，却把 'sterile' 又写了回来
        self.assertEqual(sorted(pp.collect_image_defects(3, broke_it, **_defect_kwargs())),
                         ['placeholder'])
        out, adopted, residual, chat = self._repair(prompt, [broke_it])
        self.assertFalse(adopted, '引入新缺陷类别的重写稿必须被弃掉')
        self.assertEqual(out, prompt)

    def test_llm_failure_keeps_the_original(self):
        defects = pp.collect_image_defects(3, BOTH_BROKEN, **_defect_kwargs())
        with patch.object(pp, '_chat', side_effect=RuntimeError('gateway down')):
            out, adopted, residual = pp.repair_image_defects(
                {}, 3, BOTH_BROKEN, defects, **_defect_kwargs())
        self.assertFalse(adopted)
        self.assertEqual(out, BOTH_BROKEN)

    def test_no_defects_makes_no_call(self):
        with patch.object(pp, '_chat', side_effect=AssertionError('不该发起调用')) as chat:
            out, adopted, residual = pp.repair_image_defects(
                {}, 3, BOTH_FIXED, {}, **_defect_kwargs())
        self.assertFalse(adopted)
        self.assertEqual(chat.call_count, 0)

    def test_every_detected_class_contributes_a_directive(self):
        """system prompt 必须把命中的每一类的重写方向都带上——少带一类，那一类就等于
        没进回炉，模型只会修它看得见的那几条。"""
        defects = pp.collect_image_defects(3, BOTH_BROKEN, **_defect_kwargs())
        with patch.object(pp, '_chat', side_effect=[BOTH_FIXED]) as chat:
            pp.repair_image_defects({}, 3, BOTH_BROKEN, defects, **_defect_kwargs())
        system = chat.call_args_list[0].args[1]
        self.assertIn('STERILE', system)
        self.assertIn('STAGE SCOPE WORDING', system)


class TestImageDefectOrderCoversEveryCollector(unittest.TestCase):
    """_IMAGE_DEFECT_ORDER 少列一类 = 那一类永远拿不到重写方向（system prompt 里没有它
    对应的段落），却仍然参与复验——重写稿因此必然被判成"没修好"。这条把两边钉在一起。"""

    def test_order_covers_all_collectable_classes(self):
        broken_everything = (
            f"{CAMERA} The frame is sterile of activity. "
            "New oak boards now cover part of the floor near the doorway.")
        collected = set(pp.collect_image_defects(3, broken_everything, **_defect_kwargs()))
        self.assertTrue(collected)
        self.assertTrue(collected <= set(pp._IMAGE_DEFECT_ORDER),
                        f"这些缺陷类别不在 _IMAGE_DEFECT_ORDER 里: "
                        f"{collected - set(pp._IMAGE_DEFECT_ORDER)}")
        for key in pp._IMAGE_DEFECT_ORDER:
            self.assertTrue(
                pp._image_defect_directive(key, broken_everything, beat=BEAT,
                                           stage_scope='large'),
                f"{key} 没有对应的重写方向正文")


if __name__ == '__main__':
    unittest.main()
