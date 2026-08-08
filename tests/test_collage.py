import shutil
import tempfile
import unittest
from pathlib import Path

from tools.collage import build_keyframe_collage, resolve_binary


@unittest.skipUnless(resolve_binary('ffmpeg'), 'ffmpeg binary not available on this machine')
class TestBuildKeyframeCollage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_frame(self, name, color):
        from PIL import Image
        path = Path(self.tmp) / name
        Image.new('RGB', (64, 96), color).save(path)
        return path

    def test_builds_a_tiled_jpg_from_frame_paths(self):
        frames = [self._make_frame(f'img_{i:03d}.png', (10 * i % 255, 20, 30)) for i in range(1, 8)]
        output = Path(self.tmp) / 'demo_collage.jpg'
        result = build_keyframe_collage(frames, output, columns=5)
        self.assertEqual(result, output)
        self.assertTrue(output.exists())
        self.assertGreater(output.stat().st_size, 0)

    def test_empty_frame_list_returns_none(self):
        output = Path(self.tmp) / 'empty_collage.jpg'
        self.assertIsNone(build_keyframe_collage([], output))


if __name__ == '__main__':
    unittest.main()
