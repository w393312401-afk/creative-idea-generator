import os
import tempfile
import unittest
from unittest.mock import patch

from prompt_pipeline import (
    _slugify,
    _ledger_recent_topic_dnas,
    append_to_used_topic_ledger,
)


LEDGER_HEADER = (
    "# Used Topic Ledger — 已用选题账本\n\n"
    "## Topic DNA Rows\n\n"
    "| Date | Topic DNA (carrier / destiny / twist) | 一句话选题 | Source | Avoid Notes |\n"
    "|---|---|---|---|---|\n"
)


class TestSlugify(unittest.TestCase):
    def test_lowercases_and_hyphenates(self):
        self.assertEqual(_slugify("Off-Grid Micro Home"), "off-grid-micro-home")

    def test_collapses_whitespace_and_existing_hyphens(self):
        self.assertEqual(_slugify("  giant   hollow  oak "), "giant-hollow-oak")

    def test_empty_or_none_falls_back_to_unknown(self):
        self.assertEqual(_slugify(""), "unknown")
        self.assertEqual(_slugify(None), "unknown")


class TestUsedTopicLedger(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.references_dir = os.path.join(self.tmpdir.name, "references")
        os.makedirs(self.references_dir, exist_ok=True)
        self.ledger_path = os.path.join(self.references_dir, "used-topic-ledger.md")
        self.skill_dir_patch = patch("prompt_pipeline.SKILL_DIR", self.tmpdir.name)
        self.skill_dir_patch.start()
        self.addCleanup(self.skill_dir_patch.stop)

    def _write_ledger(self, body=""):
        with open(self.ledger_path, "w", encoding="utf-8") as f:
            f.write(LEDGER_HEADER + body)

    def _read_ledger(self):
        with open(self.ledger_path, "r", encoding="utf-8") as f:
            return f.read()

    def test_missing_ledger_file_is_a_noop(self):
        # No ledger file created in this test's tmpdir.
        append_to_used_topic_ledger({}, {"theme": "x"})
        self.assertFalse(os.path.exists(self.ledger_path))

    def test_brief_parse_failed_skips_write_entirely(self):
        self._write_ledger()
        append_to_used_topic_ledger(
            {"carrier": "x", "destiny": "finished design"},
            {"theme": "x", "topic_dna": "natural / refuge-den / self-material-window"},
            brief_parse_failed=True,
        )
        self.assertEqual(self._read_ledger(), LEDGER_HEADER)

    def test_topic_dna_passthrough_is_verbatim_no_reslugify(self):
        self._write_ledger()
        dna = "natural / refuge-den / self-material-window"
        append_to_used_topic_ledger({}, {"theme": "做一个测试主题", "topic_dna": dna})
        content = self._read_ledger()
        self.assertIn(f"| {dna} |", content)

    def test_builds_short_fingerprint_from_parsed_brief_when_no_topic_dna(self):
        self._write_ledger()
        parsed_brief = {
            "carrier_family": "man-made",
            "destiny": "a very long descriptive sentence that should never end up in the ledger",
            "destiny_family": "off-grid micro-home",
        }
        dimensions = {"theme": "x", "anchors": ["Brass Porthole Lighting"]}
        append_to_used_topic_ledger(parsed_brief, dimensions)
        content = self._read_ledger()
        self.assertIn("man-made / off-grid-micro-home / brass-porthole-lighting", content)
        self.assertNotIn("a-very-long-descriptive-sentence", content)

    def test_falls_back_to_destiny_when_destiny_family_missing(self):
        self._write_ledger()
        parsed_brief = {"carrier_family": "vehicle", "destiny": "off-grid micro-home"}
        dimensions = {"theme": "x"}
        append_to_used_topic_ledger(parsed_brief, dimensions)
        content = self._read_ledger()
        self.assertIn("vehicle / off-grid-micro-home / custom-twist", content)

    def test_duplicate_topic_dna_in_recent_rows_is_skipped(self):
        dna = "natural / refuge-den / self-material-window"
        self._write_ledger(f"| 2026-07-11 | {dna} | 做一个测试主题 | GUI Generation | notes |\n")
        append_to_used_topic_ledger({}, {"theme": "做一个测试主题2", "topic_dna": dna})
        content = self._read_ledger()
        # Still exactly one row with this DNA — the second call was skipped.
        self.assertEqual(content.count(dna), 1)

    def test_distinct_topic_dna_is_appended_alongside_existing_rows(self):
        old_dna = "natural / refuge-den / self-material-window"
        self._write_ledger(f"| 2026-07-11 | {old_dna} | 旧主题 | GUI Generation | notes |\n")
        new_dna = "vehicle / off-grid-micro-home / porthole-lighting"
        append_to_used_topic_ledger({}, {"theme": "新主题", "topic_dna": new_dna})
        content = self._read_ledger()
        self.assertIn(old_dna, content)
        self.assertIn(new_dna, content)

    def test_ledger_recent_topic_dnas_reads_from_tail_not_parsed_table(self):
        # Simulate the real file's structure: a stray heading interrupts the table mid-file.
        body = (
            "| 2026-06-22 | living-tree / bedroom / self-material-window | 旧 | src | notes |\n"
            "\n## Avoid List (cliché)\n\n- some cliche line\n"
            "| 2026-07-11 | vehicle / micro-home / porthole-lighting | 新 | src | notes |\n"
        )
        self._write_ledger(body)
        recent = _ledger_recent_topic_dnas(self.ledger_path, tail_lines=20)
        self.assertIn("vehicle / micro-home / porthole-lighting", recent)


if __name__ == "__main__":
    unittest.main()
