# -*- coding: utf-8 -*-
"""链头（IMG 001）的封面参考图到底能不能被找到。

2026-09-03 复盘（run_replica_9d2e50e291c5 海蚀洞海景木屋）：封面 13:21 就已经落在项目
目录里，两分钟后开跑的帧序列却报「IMG 001 无封面可用，链头改为纯文本生成」——链头丢掉
图参考、退化成纯文生图，那段刚被对帧订正重写成「复述原片首帧」的文字于是成了唯一的
控制项，画面自然就长成原片首帧。

两个成因各有一组用例守着：

  1. 项目目录靠标题现算。复刻线的磁盘命名空间是 `run_<job>__<标题>` 经 `_safe_project_name`
     截到 60 字符之后的样子，只拿到人类标题的调用方算出来的是另一个（不存在的）目录。
     渲染层已经算过一次真正的目录了——`project_dir` 入参就是让它把那份结果传进来。
  2. `active_cover` 这个字段一直只有前端手选封面那条路会写。自动生成的封面从来没被
     登记过，谁想找它都只能靠「listdir 挑 mtime 最新的一张」。
"""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server_common


class TestCoverReferenceLookup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old_root = server_common.OUTPUT_ROOT
        server_common.OUTPUT_ROOT = self.tmp
        # 复刻线真实的磁盘命名空间：run_<job>__<标题> 过一遍 _safe_project_name。
        self.title = '改造成海蚀洞海景木屋工作室 · 爆款 1:1 复刻 · TikTok - Make Your Day.mp4'
        self.project_key = server_common.make_idea_project_key('replica_9d2e50e291c5', self.title)
        self.project_dir = os.path.join(self.tmp, server_common._safe_project_name(self.project_key))
        os.makedirs(self.project_dir)
        self.cover = os.path.join(self.project_dir, 'cover_1788326449785.webp')
        with open(self.cover, 'wb') as f:
            f.write(b'cover-bytes')

    def tearDown(self):
        server_common.OUTPUT_ROOT = self.old_root
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_title_alone_cannot_reach_the_cover(self):
        """成因本身。标题算出来的目录跟封面所在的目录不是同一个。"""
        derived = server_common._get_project_dir(self.title)
        self.assertNotEqual(os.path.abspath(derived), os.path.abspath(self.project_dir))
        self.assertIsNone(server_common.resolve_cover_reference({}, self.title))

    def test_the_caller_can_hand_over_the_directory_it_is_writing_into(self):
        hit = server_common.resolve_cover_reference({}, self.title, project_dir=self.project_dir)
        self.assertEqual(os.path.abspath(hit), os.path.abspath(self.cover))

    def test_the_project_key_path_still_works_on_its_own(self):
        """既有口径不变：拿得到 _project_key 的调用方照旧能找到，不必传目录。"""
        hit = server_common.resolve_cover_reference(
            {'_project_key': self.project_key}, self.title)
        self.assertEqual(os.path.abspath(hit), os.path.abspath(self.cover))

    def test_a_registered_active_cover_beats_the_newest_file(self):
        """登记过就按登记的来——这正是自动生成封面此前缺的那个写入方。"""
        newer = os.path.join(self.project_dir, 'cover_1788326449999.webp')
        with open(newer, 'wb') as f:
            f.write(b'newer-bytes')
        os.utime(newer, (time.time() + 60, time.time() + 60))

        # 未登记时按 mtime 取最新的那张。
        self.assertEqual(
            os.path.abspath(server_common.resolve_cover_reference(
                {}, self.title, project_dir=self.project_dir)),
            os.path.abspath(newer))

        rel = '/outputs/' + os.path.basename(self.project_dir) + '/' + os.path.basename(self.cover)
        with open(os.path.join(self.project_dir, 'manifest.json'), 'w', encoding='utf-8') as f:
            json.dump({'active_cover': rel}, f)
        # OUTPUT_ROOT 被指到临时目录，'/outputs/...' 这种前端形状要能落回来。
        self.assertEqual(
            os.path.abspath(server_common.manifest_cover_role(self.project_dir, 'active_cover') or ''),
            os.path.abspath(self.cover))

    def test_a_frame1_role_of_none_still_means_no_cover(self):
        """用户明确说「这一单链头不要封面」时，多给一个目录不该把它翻回来。"""
        with open(os.path.join(self.project_dir, 'manifest.json'), 'w', encoding='utf-8') as f:
            json.dump({'cover_roles': {'frame1': 'none'}}, f)
        self.assertIsNone(server_common.manifest_cover_role(self.project_dir, 'frame1', 'active_cover'))

    def test_skip_toggle_short_circuits_before_any_lookup(self):
        for cfg in ({'skipCoverReference': True}, {'allowTextOnlyAnchor': True},
                    {'coverReferencePath': 'none'}):
            self.assertIsNone(server_common.resolve_cover_reference(
                cfg, self.title, project_dir=self.project_dir), cfg)


if __name__ == '__main__':
    unittest.main()
