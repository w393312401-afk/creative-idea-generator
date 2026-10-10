"""Frame 1 uses the cover image as reference but always uses 图片 1 as its prompt."""

import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

import server_common
from frame_generator import generate_frame_sequence


class TestFirstFrameUsesImageOnePrompt(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old_output_root = server_common.OUTPUT_ROOT
        server_common.OUTPUT_ROOT = self.tmp
        covers_dir = os.path.join(self.tmp, 'covers')
        os.makedirs(covers_dir, exist_ok=True)
        self.cover = os.path.join(covers_dir, 'project_cover_1.webp')
        Image.new('RGB', (36, 64), (120, 100, 80)).save(self.cover, format='WEBP')
        self.text_calls = []
        self.edit_calls = []

    def tearDown(self):
        server_common.OUTPUT_ROOT = self.old_output_root
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _write(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        Image.new('RGB', (36, 64), (90, 110, 130)).save(path, format='WEBP')

    def test_cover_is_reference_but_prompt_is_image_one(self):
        def fake_text(config, prompt, target_path, *args, **kwargs):
            self.text_calls.append(prompt)
            self._write(target_path)

        def fake_edit(config, prompt, reference_path, target_path, **kwargs):
            self.edit_calls.append((prompt, reference_path, kwargs.get('control_prompt')))
            self._write(target_path)

        prompt_block = "图片 1:\nuntouched first frame\n\n图片 2:\nfirst construction stage\n"
        config = {'coverReferencePath': self.cover}
        with patch('frame_generator._generate_text_image', side_effect=fake_text), \
             patch('frame_generator._generate_image_edit', side_effect=fake_edit):
            manifest = generate_frame_sequence(config, 'project', prompt_block)

        self.assertEqual(self.text_calls, [])
        self.assertEqual(len(self.edit_calls), 2)
        self.assertEqual(self.edit_calls[0][0], 'untouched first frame')
        self.assertEqual(os.path.abspath(self.edit_calls[0][1]), os.path.abspath(self.cover))
        self.assertEqual(self.edit_calls[0][2], '')
        self.assertEqual(self.edit_calls[1][0], 'first construction stage')
        self.assertTrue(self.edit_calls[1][1].endswith('img_001.webp'))
        first = next(frame for frame in manifest['frames'] if frame['sequence'] == 1)
        self.assertTrue(first['reference'].endswith(os.path.basename(self.cover)))
        self.assertEqual(first['prompt'], 'untouched first frame')
        self.assertEqual(first['anchor_reference'], 'cover')

    def test_missing_cover_falls_back_to_text_only_anchor(self):
        """封面缺失不再是硬闸。

        以前这里会 raise「必须先生成或选择封面图」，链头只能图生图。那道闸已经
        撤掉（同一批改动也删了 server.py 两个下单接口上的同款前置校验），无封面
        时链头一律退化为纯文生图——但**只有链头**：第 2 帧起仍必须挂上一帧图参考，
        否则等于每帧各画各的，血统断在第一拍。
        """
        os.remove(self.cover)

        def fake_text(config, prompt, target_path, *args, **kwargs):
            self.text_calls.append(prompt)
            self._write(target_path)

        def fake_edit(config, prompt, reference_path, target_path, **kwargs):
            self.edit_calls.append((prompt, reference_path))
            self._write(target_path)

        prompt_block = "图片 1:\nuntouched first frame\n\n图片 2:\nfirst construction stage\n"
        with patch('frame_generator._generate_text_image', side_effect=fake_text), \
             patch('frame_generator._generate_image_edit', side_effect=fake_edit):
            manifest = generate_frame_sequence({}, 'project_without_cover', prompt_block)

        self.assertEqual(self.text_calls, ['untouched first frame'])
        self.assertEqual(len(self.edit_calls), 1)
        self.assertTrue(self.edit_calls[0][1].endswith('img_001.webp'))
        first = next(frame for frame in manifest['frames'] if frame['sequence'] == 1)
        self.assertIsNone(first['reference'])

    def test_stepped_anchor_may_render_text_only_without_cover(self):
        """分步任务渲首帧时封面还写不出来（见 text_only_anchor_allowed）：
        链头显式退化为纯文生图，但第 2 帧起仍必须挂上一帧图参考。"""
        os.remove(self.cover)

        def fake_text(config, prompt, target_path, *args, **kwargs):
            self.text_calls.append(prompt)
            self._write(target_path)

        def fake_edit(config, prompt, reference_path, target_path, **kwargs):
            self.edit_calls.append((prompt, reference_path))
            self._write(target_path)

        prompt_block = "图片 1:\nuntouched first frame\n\n图片 2:\nfirst construction stage\n"
        with patch('frame_generator._generate_text_image', side_effect=fake_text), \
             patch('frame_generator._generate_image_edit', side_effect=fake_edit):
            manifest = generate_frame_sequence(
                {'allowTextOnlyAnchor': True}, 'stepped_project', prompt_block)

        self.assertEqual(self.text_calls, ['untouched first frame'])
        self.assertEqual(len(self.edit_calls), 1)
        self.assertTrue(self.edit_calls[0][1].endswith('img_001.webp'))
        first = next(frame for frame in manifest['frames'] if frame['sequence'] == 1)
        self.assertIsNone(first['reference'])

    def test_first_frame_edit_failure_never_falls_back_to_t2i(self):
        prompt_block = "图片 1:\nuntouched first frame\n"

        with patch('frame_generator._generate_text_image') as text_image, \
             patch('frame_generator._generate_image_edit', side_effect=RuntimeError('edit failed')):
            with self.assertRaisesRegex(RuntimeError, '第 1 帧图生图失败'):
                generate_frame_sequence({'coverReferencePath': self.cover}, 'project', prompt_block)

        text_image.assert_not_called()

    def test_cut_frame_still_uses_previous_frame_as_reference(self):
        def fake_edit(config, prompt, reference_path, target_path, **kwargs):
            self.edit_calls.append((prompt, reference_path, kwargs.get('control_prompt')))
            self._write(target_path)

        prompt_block = (
            "图片 1:\nexterior frame\n\n"
            "图片 2:\ninterior frame\n\n"
            "视频 1 [CUT]:\nhard cut indoors\n"
        )
        with patch('frame_generator._generate_text_image') as text_image, \
             patch('frame_generator._generate_image_edit', side_effect=fake_edit):
            generate_frame_sequence({'coverReferencePath': self.cover}, 'project', prompt_block)

        text_image.assert_not_called()
        self.assertEqual(len(self.edit_calls), 2)
        self.assertTrue(self.edit_calls[1][1].endswith('img_001.webp'))


if __name__ == '__main__':
    unittest.main()
