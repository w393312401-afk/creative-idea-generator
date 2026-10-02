# -*- coding: utf-8 -*-
"""
Tests for Smart Multi-Modal Sampling (Audio Transients, Triad Frames, Spatial DNA).
"""

import sys
import os
import json
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

# Ensure scripts dir can be imported
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'skills', 'gemini-omni-restoration-composer', 'scripts'))
import analyze_timelapse_video as atv



class TestSmartSampling(unittest.TestCase):

    def test_extract_spatial_dna_vertical(self):
        meta = {"width": 1080, "height": 1920, "fps": 30.0, "duration_sec": 30.0, "has_audio": True}
        dna = atv.extract_spatial_dna(meta)
        self.assertEqual(dna["aspect_ratio"], "9:16")
        self.assertEqual(dna["width"], 1080)
        self.assertEqual(dna["height"], 1920)
        self.assertIn("24mm", dna["recommended_lens"])
        self.assertIn("pitch locked", dna["pitch_lock_clause"])

    def test_extract_spatial_dna_horizontal(self):
        meta = {"width": 1920, "height": 1080, "fps": 30.0, "duration_sec": 30.0, "has_audio": False}
        dna = atv.extract_spatial_dna(meta)
        self.assertEqual(dna["aspect_ratio"], "16:9")
        self.assertEqual(dna["width"], 1920)

    def test_detect_audio_transients_no_audio(self):
        res = atv.detect_audio_transients(Path("dummy.mp4"), has_audio=False)
        self.assertEqual(res, [])

    @patch("analyze_timelapse_video.run")
    def test_detect_audio_transients_with_spikes(self, mock_run):
        # Mock FFmpeg astats output with two loudness bursts
        mock_output = (
            "frame:0 pts_time:0.000\nlavfi.astats.Overall.RMS_level=-50.0\n"
            "frame:10 pts_time:0.500\nlavfi.astats.Overall.RMS_level=-22.0\n"
            "frame:20 pts_time:1.000\nlavfi.astats.Overall.RMS_level=-45.0\n"
            "frame:40 pts_time:2.000\nlavfi.astats.Overall.RMS_level=-18.0\n"
            "frame:60 pts_time:3.000\nlavfi.astats.Overall.RMS_level=-48.0\n"
        )
        mock_run.return_value = MagicMock(stdout=mock_output, stderr="")
        transients = atv.detect_audio_transients(Path("test.mp4"), has_audio=True)
        self.assertTrue(len(transients) >= 2)
        self.assertAlmostEqual(transients[0]["timestamp"], 0.5, places=1)
        self.assertGreaterEqual(transients[0]["delta_db"], 6.0)

    def test_review_timestamps_includes_audio_transients(self):
        transients = [
            {"timestamp": 4.52, "delta_db": 12.0, "rms_db": -18.0},
            {"timestamp": 9.15, "delta_db": 9.5, "rms_db": -20.0},
        ]
        timestamps = atv.review_timestamps(
            duration=15.0,
            fps=30.0,
            base_fps=1.0,
            dense_fps=2.0,
            audio_transients=transients,
        )
        # Verify transient points (4.52 and 9.15 or close values) are included
        has_t1 = any(abs(ts - 4.52) <= 0.1 for ts in timestamps)
        has_t2 = any(abs(ts - 9.15) <= 0.1 for ts in timestamps)
        self.assertTrue(has_t1, f"Expected 4.52s in timestamps: {timestamps}")
        self.assertTrue(has_t2, f"Expected 9.15s in timestamps: {timestamps}")


    def test_safe_print_unicode_emoji(self):
        # Must not throw even when string contains emojis like 🛠️
        emoji_str = "collage: outputs/🛠️_test/sample_collage.jpg"
        atv._safe_print(emoji_str)

        # Explicitly simulate a GBK stream that raises UnicodeEncodeError
        mock_stdout = MagicMock()
        mock_stdout.encoding = "gbk"
        def fake_print(*args, **kwargs):
            raise UnicodeEncodeError("gbk", "🛠️", 0, 1, "illegal multibyte sequence")
        with patch("builtins.print", side_effect=fake_print):
            # Should catch UnicodeEncodeError and fallback cleanly without throwing
            atv._safe_print(emoji_str)


if __name__ == "__main__":
    unittest.main()
