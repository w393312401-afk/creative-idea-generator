"""参考图留档自愈：上传挂载拿到的画布 UUID 必须回流到 fx_src 留档。

中选候选的媒体 URL 里解析不出 UUID 时，_fx_store_frame 会把留档落成
img_NNN_nouuid.jpg。_fx_find_ref_for 认不出它 → 下一帧的链式参考退化成无 UUID 的
chain_ref_NNN.jpg → FX 侧只能上传。此前这个状态是**永久**的：那一帧每渲一次、
每点一次「修复此帧问题」都要重传同一张图。

上传成功的那一刻图已经在画布上并有了 UUID，把它接回来改名，代价就只付一次。
"""
import inspect
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import frame_generator as fg

_UUID = '0dc5975b-3a93-4a62-b288-62541eee0db2'


class TestFxReferenceUuidHealing(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.frames_dir = os.path.join(self.tmp, 'frames')
        self.src_dir = fg._fx_src_dir(self.frames_dir)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _touch(self, name, body=b'x'):
        path = os.path.join(self.src_dir, name)
        with open(path, 'wb') as f:
            f.write(body)
        return path

    def test_nouuid_archive_is_renamed_and_becomes_findable(self):
        self._touch('img_017_nouuid.jpg', b'original-fx-bytes')
        self.assertIsNone(fg._fx_find_ref_for(self.frames_dir, 18))   # 改名前认不出

        self.assertTrue(fg._fx_heal_frame_uuid(self.frames_dir, 17, _UUID))

        found = fg._fx_find_ref_for(self.frames_dir, 18)
        self.assertEqual(os.path.basename(found), f'img_017_{_UUID}.jpg')
        # 原始字节保留（留档仍是那张 FX 原图，只是名字补上了 UUID）
        with open(found, 'rb') as f:
            self.assertEqual(f.read(), b'original-fx-bytes')

    def test_existing_real_uuid_archive_is_never_touched(self):
        keep = self._touch(f'img_017_{_UUID}.jpg', b'good')
        other = '11111111-2222-3333-4444-555555555555'
        self.assertFalse(fg._fx_heal_frame_uuid(self.frames_dir, 17, other))
        self.assertTrue(os.path.exists(keep))
        self.assertFalse(os.path.exists(os.path.join(self.src_dir, f'img_017_{other}.jpg')))

    def test_garbage_uuid_is_rejected(self):
        self._touch('img_017_nouuid.jpg')
        for bad in (None, '', 'nouuid', 'not-a-uuid', _UUID + 'tail'):
            self.assertFalse(fg._fx_heal_frame_uuid(self.frames_dir, 17, bad), bad)
        self.assertTrue(os.path.exists(os.path.join(self.src_dir, 'img_017_nouuid.jpg')))

    def test_healing_is_idempotent(self):
        self._touch('img_017_nouuid.jpg')
        self.assertTrue(fg._fx_heal_frame_uuid(self.frames_dir, 17, _UUID))
        self.assertFalse(fg._fx_heal_frame_uuid(self.frames_dir, 17, _UUID))

    def test_other_sequences_are_not_collateral(self):
        self._touch('img_016_nouuid.jpg')
        self.assertFalse(fg._fx_heal_frame_uuid(self.frames_dir, 17, _UUID))
        self.assertTrue(os.path.exists(os.path.join(self.src_dir, 'img_016_nouuid.jpg')))


class TestUploadReturnsUuid(unittest.TestCase):
    """上传挂载必须把 UUID 交回来，否则自愈无米下锅（该函数要真浏览器，只查源码契约）。"""

    def test_upload_fallback_returns_the_new_uuid(self):
        from integrations.google_fx.services import google_fx_helpers as h
        src = inspect.getsource(h._upload_image_to_canvas_and_mount)
        self.assertIn('return new_uuid', src)
        self.assertNotIn('return True', src)

    def test_batch_result_carries_uploaded_reference_uuids(self):
        from integrations.google_fx.services import google_fx_image as gi
        src = inspect.getsource(gi._generate_images_batch_google_fx_single_attempt)
        self.assertIn('uploaded_reference_uuids', src)


if __name__ == '__main__':
    unittest.main()
