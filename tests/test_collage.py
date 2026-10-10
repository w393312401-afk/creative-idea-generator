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

    def test_max_frames_downsampling(self):
        # Create 60 frames
        frames = [self._make_frame(f'img_{i:03d}.png', (i % 255, 100, 150)) for i in range(60)]
        output = Path(self.tmp) / 'downsampled_collage.jpg'
        result = build_keyframe_collage(frames, output, columns=5, max_frames=20, tile_width=120)
        self.assertEqual(result, output)
        self.assertTrue(output.exists())
        self.assertGreater(output.stat().st_size, 0)
        # Check image dimensions (5 cols, 4 rows of 120 width -> width = 600 + padding)
        from PIL import Image
        with Image.open(output) as im:
            self.assertLess(im.height, 2000)
            self.assertGreater(im.width, 300)

    def test_max_frames_zero_keeps_every_frame(self):
        """带下标契约的拼图（Pass B 的 sheet）靠 max_frames=0 关掉降采样。

        抽掉任何一张，第 k 格就不再对应 FRAME FACTS 第 lo+k 条，模型会把 A 帧的画面
        当成 B 帧的事实读——比没有拼图更坏。这里用行数反证一张都没丢：30 张 5 列必须
        排满 6 行，而默认的 max_frames=25 只会排 5 行。
        """
        from PIL import Image
        frames = [self._make_frame(f'z_{i:03d}.png', (i % 255, 60, 90)) for i in range(30)]

        kept = Path(self.tmp) / 'kept_collage.jpg'
        self.assertEqual(build_keyframe_collage(frames, kept, columns=5, max_frames=0), kept)
        with Image.open(kept) as im:
            kept_h = im.height

        sampled = Path(self.tmp) / 'sampled_collage.jpg'
        self.assertEqual(build_keyframe_collage(frames, sampled, columns=5), sampled)
        with Image.open(sampled) as im:
            sampled_h = im.height

        # 6 行 vs 5 行：不降采样的那张必须更高
        self.assertGreater(kept_h, sampled_h)

    def test_incomplete_last_row_is_black_not_a_repeated_frame(self):
        """排不满的最后一行必须是黑格，不能是复制的末帧。

        这张拼图会当作「整条序列一览」喂给模型，
        补出来的复制格会被读成「结尾停留了很久」，节拍梯上给结尾多分时长。21 张 5 列
        正是线上撞到的那一档：真帧 21 张，第 22–25 格必须是黑的。
        """
        from PIL import Image
        frames = [self._make_frame(f'p_{i:03d}.png', (200, 0, 0)) for i in range(21)]
        output = Path(self.tmp) / 'padded_collage.jpg'
        self.assertEqual(
            build_keyframe_collage(frames, output, columns=5, max_frames=0, tile_width=120),
            output)

        margin, padding, cols, rows = 4, 4, 5, 5
        with Image.open(output) as im:
            im = im.convert('RGB')
            tile_w = (im.width - 2 * margin - (cols - 1) * padding) / cols
            tile_h = (im.height - 2 * margin - (rows - 1) * padding) / rows

            def centre(index):
                row, col = divmod(index, cols)
                return (int(margin + col * (tile_w + padding) + tile_w / 2),
                        int(margin + row * (tile_h + padding) + tile_h / 2))

            self.assertGreater(im.getpixel(centre(20))[0], 150)   # 第 21 格是真的末帧
            for k in range(21, 25):
                self.assertLess(max(im.getpixel(centre(k))), 40, f'第 {k + 1} 格不是黑格')

    def test_max_frames_one_does_not_blow_up(self):
        """max_frames=1 曾经直接 ZeroDivisionError（step 要除以 max_frames-1）。"""
        frames = [self._make_frame(f'o_{i:03d}.png', (i % 255, 30, 40)) for i in range(10)]
        output = Path(self.tmp) / 'single_collage.jpg'
        self.assertEqual(build_keyframe_collage(frames, output, columns=5, max_frames=1), output)
        self.assertGreater(output.stat().st_size, 0)

if __name__ == '__main__':
    unittest.main()

