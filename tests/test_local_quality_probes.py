"""本地质量判据（2026-07-15 盐湖贝壳单复盘 P1/P2）：

- video_generator.frame_pair_contract —— i2v 提交前首尾锚点帧相似度双向契约
- frame_generator.detect_anchor_inertia —— 桥接/换族帧渲后 i2i 惯性检测
- video_generator.detect_frozen_clip —— 冻结片段本地预筛（环境异常 fail-open 路径）

阈值都用真实事故数据标定过（复制帧对 MAD≤2.2、正常推进 4.8~17.3、空间断裂 47），
这里用合成图验证判据方向与失败保护，不复刻具体标定值。
"""
import os
import shutil
import tempfile
import unittest

from PIL import Image

from video_generator import frame_pair_contract, detect_frozen_clip
from frame_generator import detect_anchor_inertia


class _ImgCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _img(self, name, color, size=(64, 114)):
        p = os.path.join(self.tmp, name)
        Image.new('RGB', size, color).save(p, 'PNG')
        return p

    def _noisy_img(self, name, base=200, stripe=0):
        """带横条纹的图，跟纯色图拉开可控差异。"""
        p = os.path.join(self.tmp, name)
        im = Image.new('L', (64, 114), base)
        if stripe:
            px = im.load()
            for y in range(0, 114, 2):
                for x in range(64):
                    px[x, y] = max(0, base - stripe)
        im.save(p, 'PNG')
        return p


class TestFramePairContract(_ImgCase):
    def test_identical_pair_is_too_similar(self):
        a = self._img('a.png', (128, 128, 128))
        b = self._img('b.png', (128, 128, 128))
        verdict, mad = frame_pair_contract(a, b)
        self.assertEqual(verdict, 'too_similar')
        self.assertAlmostEqual(mad, 0.0, places=3)

    def test_opposite_pair_is_too_different(self):
        a = self._img('a.png', (0, 0, 0))
        b = self._img('b.png', (255, 255, 255))
        verdict, mad = frame_pair_contract(a, b)
        self.assertEqual(verdict, 'too_different')
        self.assertGreater(mad, 200)

    def test_moderate_difference_is_ok(self):
        a = self._noisy_img('a.png', base=200, stripe=0)
        b = self._noisy_img('b.png', base=200, stripe=20)  # 半数行暗 20 → MAD≈10
        verdict, mad = frame_pair_contract(a, b)
        self.assertEqual(verdict, 'ok')
        self.assertGreater(mad, 3.0)
        self.assertLess(mad, 35.0)

    def test_missing_file_is_skipped_not_blocking(self):
        a = self._img('a.png', (10, 10, 10))
        self.assertEqual(frame_pair_contract(a, os.path.join(self.tmp, 'nope.png')),
                         ('skipped', None))
        self.assertEqual(frame_pair_contract(None, a), ('skipped', None))

    def test_unreadable_file_is_skipped(self):
        a = self._img('a.png', (10, 10, 10))
        bad = os.path.join(self.tmp, 'bad.png')
        with open(bad, 'wb') as f:
            f.write(b'not an image')
        self.assertEqual(frame_pair_contract(a, bad), ('skipped', None))


class TestDetectAnchorInertia(_ImgCase):
    def test_near_copy_of_reference_is_stuck(self):
        rendered = self._img('r.png', (120, 120, 120))
        reference = self._img('ref.png', (120, 120, 120))
        stuck, mad = detect_anchor_inertia(rendered, reference)
        self.assertTrue(stuck)
        self.assertLess(mad, 3.0)

    def test_real_space_change_is_not_stuck(self):
        rendered = self._img('r.png', (30, 30, 30))
        reference = self._img('ref.png', (220, 220, 220))
        stuck, mad = detect_anchor_inertia(rendered, reference)
        self.assertFalse(stuck)
        self.assertGreater(mad, 35.0)

    def test_missing_or_unreadable_files_fail_open(self):
        a = self._img('a.png', (10, 10, 10))
        self.assertEqual(detect_anchor_inertia(a, os.path.join(self.tmp, 'nope.png')),
                         (False, None))
        self.assertEqual(detect_anchor_inertia(None, a), (False, None))


class TestDetectFrozenClipFailOpen(_ImgCase):
    def test_missing_video_is_skipped_not_frozen(self):
        mids = [self._img(f'm{i}.png', (100, 100, 100)) for i in range(3)]
        frozen, reason = detect_frozen_clip(os.path.join(self.tmp, 'nope.mp4'), mids, self.tmp)
        self.assertFalse(frozen)
        self.assertTrue(reason.startswith('skipped:'))

    def test_too_few_samples_is_skipped(self):
        # first 帧能抽出来但中段样本不足 → 判据放弃而不是误判冻结
        frozen, reason = detect_frozen_clip(os.path.join(self.tmp, 'nope.mp4'), [], self.tmp)
        self.assertFalse(frozen)
        self.assertTrue(reason.startswith('skipped:'))


if __name__ == '__main__':
    unittest.main()
