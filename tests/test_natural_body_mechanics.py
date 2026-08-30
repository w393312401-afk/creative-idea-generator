"""排查「视频人物肢体活动太机械，不拟人」的根因修复：worker/cast 的动作此前全靠
Action-Tool Triad（某个可见变化由某个工具动作造成）与 posture-progression（起点姿态->
终点姿态）框架描述，没有一句人体力学的正向要求——EVEN_RATE 治好了"推进跳变"却顺带把
"节奏该匀速"也写死，模型读出来的是两姿态间的匀速滑块，不是真人。

fix_natural_body_mechanics / check_natural_body_mechanics 是与 fix_pacing_control /
check_pacing_control（_EVEN_RATE）同一模式的确定性兜底：出现人体时缺了自然力学正向
要求就补一句，有则不动；没有人体、或正文已声明"画面无人"（sterile/no workers）时不碰。
"""
import unittest

from prompt_pipeline import (
    fix_natural_body_mechanics,
    check_natural_body_mechanics,
    _NATURAL_MOTION_MARKER,
)


class TestFixNaturalBodyMechanics(unittest.TestCase):
    def test_appends_clause_when_worker_present(self):
        prompt = "A lone worker scoops dirt with a steel shovel."
        fixed = fix_natural_body_mechanics(prompt)
        self.assertIn(_NATURAL_MOTION_MARKER, fixed.lower())
        self.assertTrue(fixed.startswith(prompt.rstrip('.')))

    def test_idempotent_when_marker_already_present(self):
        prompt = (
            "A lone worker scoops dirt. The worker's own body moves with real human "
            f"mechanics, {_NATURAL_MOTION_MARKER}, weight shifting naturally."
        )
        fixed = fix_natural_body_mechanics(prompt)
        self.assertEqual(fixed, prompt)
        self.assertEqual(fixed.lower().count(_NATURAL_MOTION_MARKER), 1)

    def test_no_human_subject_left_untouched(self):
        prompt = "Waves lap against the empty dock as gulls circle overhead."
        self.assertEqual(fix_natural_body_mechanics(prompt), prompt)

    def test_sterile_declaration_left_untouched(self):
        prompt = "The frame stays completely sterile of workers throughout the crossing."
        self.assertEqual(fix_natural_body_mechanics(prompt), prompt)

    def test_figurine_and_occupant_also_trigger_fix(self):
        for word in ('figurine', 'occupant', 'resident'):
            prompt = f"The {word} steps closer to inspect the finished work."
            fixed = fix_natural_body_mechanics(prompt)
            self.assertIn(_NATURAL_MOTION_MARKER, fixed.lower(), word)

    def test_empty_prompt_safe(self):
        self.assertEqual(fix_natural_body_mechanics(""), "")
        self.assertEqual(fix_natural_body_mechanics(None), "")


class TestCheckNaturalBodyMechanics(unittest.TestCase):
    def test_flags_worker_without_clause(self):
        errs = check_natural_body_mechanics("A lone worker hammers nails into the beam.")
        self.assertTrue(errs)

    def test_passes_after_fix(self):
        fixed = fix_natural_body_mechanics("A lone worker hammers nails into the beam.")
        self.assertEqual(check_natural_body_mechanics(fixed), [])

    def test_no_human_subject_passes(self):
        self.assertEqual(
            check_natural_body_mechanics("A river winds past the empty clearing."), [])

    def test_sterile_declaration_passes(self):
        self.assertEqual(
            check_natural_body_mechanics("No workers are present in this clean frame."), [])


if __name__ == '__main__':
    unittest.main()
