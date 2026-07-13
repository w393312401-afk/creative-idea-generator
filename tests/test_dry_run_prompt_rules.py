"""Golden/characterization tests for the no-LLM fix_*/check_* rule chain.

These pin CURRENT behavior of apply_proactive_fixes()+validate_beat_prompts() (config=None,
skip_llm_checks=True) against real historical-bug-shaped fixtures, using tools.dry_run_prompt_rules
so a rule change can be verified in milliseconds without spinning up an LLM. This is the safety
net for later prompt_pipeline.py refactors: if a golden value here changes unexpectedly, the
refactor altered rule *behavior*, not just structure.
"""
import unittest
from pathlib import Path

from tools.dry_run_prompt_rules import dry_run_beat, load_fixture

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "prompt_rules"


class TestDryRunPromptRulesGolden(unittest.TestCase):

    def _run(self, name):
        return dry_run_beat(load_fixture(FIXTURES_DIR / name))

    def test_bridge1_camera_dna_is_not_double_stamped(self):
        result = self._run("bridge1_camera_dna_no_double_stamp.json")
        self.assertEqual(result["mechanical_errors"], [])
        # The IMAGE prompt already opened with camera-DNA-shaped text (camera height/lens
        # feel), so fix_camera_dna's prefix-keyword guard must leave it untouched rather than
        # prepending the bridge_stage=1 canonical camera_dna a second time.
        self.assertEqual(result["before"]["image_prompt"], result["after"]["image_prompt"])
        self.assertEqual(result["after"]["image_prompt"].lower().count("camera height"), 1)
        self.assertEqual(result["after"]["image_prompt"].lower().count("coaxial forward-pushing"), 1)

    def test_bridge2_removes_static_contradiction_but_keeps_the_movement_sentence(self):
        result = self._run("bridge2_removes_static_contradiction.json")
        self.assertEqual(result["mechanical_errors"], [])
        after_video = result["after"]["video_prompt"]
        self.assertNotIn("static tripod", after_video)
        self.assertNotIn("camera remains locked", after_video)
        self.assertIn("crosses the sill toward the warmly lit interior", after_video)

    def test_anti_jump_cut_guardrail_sentence_survives_intact_and_unflagged(self):
        result = self._run("anti_jump_cut_guardrail_survives_cleanup.json")
        guardrail = ("Transition shortcuts like cross-dissolves, fade-ins, or jump cuts are "
                     "strictly forbidden; every visible change must occur through continuous, "
                     "physically traceable actions in real time.")
        self.assertIn(guardrail, result["after"]["video_prompt"])
        self.assertEqual(result["mechanical_errors"], [])

    def test_landmark_locked_anchor_appended_when_missing(self):
        result = self._run("landmark_locked_anchor_appended.json")
        self.assertEqual(result["mechanical_errors"], [])
        self.assertNotIn("Locked anchors", result["before"]["image_prompt"])
        self.assertIn(
            "Locked anchors: brass reading lamp at Grid C2, walnut window seat at Grid B3.",
            result["after"]["image_prompt"],
        )

    def test_worker_reference_scrubbed_from_image_but_kept_in_video(self):
        result = self._run("worker_clean_frame_scrub_and_out_and_in.json")
        self.assertEqual(result["mechanical_errors"], [])
        after_image = result["after"]["image_prompt"]
        self.assertNotIn("worker", after_image.lower())
        self.assertIn("equipment", after_image)
        after_video = result["after"]["video_prompt"]
        self.assertIn("builder", after_video)
        self.assertIn(f"walks out of the frame through the Grid C1 edge", after_video)

    def test_well_formed_beat_passes_through_unmodified(self):
        result = self._run("well_formed_clean_pass.json")
        self.assertEqual(result["mechanical_errors"], [])
        self.assertEqual(result["before"]["image_prompt"], result["after"]["image_prompt"])
        self.assertEqual(result["before"]["video_prompt"], result["after"]["video_prompt"])
        self.assertEqual(result["diff"]["image"], [])
        self.assertEqual(result["diff"]["video"], [])

    def test_every_fixture_in_the_directory_is_covered_by_this_suite(self):
        # Guards against silently-unexercised fixtures: every *.json dropped into
        # tests/fixtures/prompt_rules/ must be referenced by a test above.
        covered = {
            "bridge1_camera_dna_no_double_stamp.json",
            "bridge2_removes_static_contradiction.json",
            "anti_jump_cut_guardrail_survives_cleanup.json",
            "landmark_locked_anchor_appended.json",
            "worker_clean_frame_scrub_and_out_and_in.json",
            "well_formed_clean_pass.json",
        }
        on_disk = {p.name for p in FIXTURES_DIR.glob("*.json")}
        self.assertEqual(on_disk, covered)


if __name__ == "__main__":
    unittest.main()
