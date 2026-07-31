# -*- coding: utf-8 -*-
"""帧序列已经在画布上了，视频阶段就不该再上传一轮同样的图。

背景：帧序列是在项目绑定的 Flow 画布上生成的（manifest.google_fx_project_url），
每张帧的画布媒体 UUID 记在 manifest.frames[].fx_uuid 里。视频阶段回到的就是同一
个画布，那些帧仍是项目资产——可此前视频链对此一无所知，每批任务都要把同一批帧
重新 set_input_files 上传一遍（十几张图起步 ~1 分钟，还平白多一轮上传推高风控）。

本文件钉死新链路的两端：
  1. video_generator 把 manifest 里的 fx_uuid 透传进 VideoRequest；
  2. 视频服务只在确认当前页就是绑定画布时认领这些 UUID，且必须过画布 DOM 校验，
     校验不过照旧上传——绝不凭 manifest 一面之词挂一张不在画布上的图。
"""

import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

import server_common
import video_generator as VG
from integrations.google_fx import models
from integrations.google_fx.services import google_fx_video as V


UUID_1 = "11111111-1111-4111-8111-111111111111"
UUID_2 = "22222222-2222-4222-8222-222222222222"
UUID_3 = "33333333-3333-4333-8333-333333333333"

PROJECT_CANVAS = "https://labs.google/fx/tools/flow/project/frames-canvas"


# ── 1. manifest → 路径/UUID 映射 ────────────────────────────────────────────

class TestLoadFrameCanvasUuids(unittest.TestCase):
    def test_maps_frame_paths_to_their_canvas_uuid(self):
        manifest = {'frames': [
            {'slot': 1, 'fx_uuid': UUID_1},
            {'slot': 2, 'fx_uuid': UUID_2},
        ]}
        slot_to_path = {1: '/p/img_001.webp', 2: '/p/img_002.webp'}

        assert VG.load_frame_canvas_uuids(manifest, slot_to_path) == {
            '/p/img_001.webp': UUID_1,
            '/p/img_002.webp': UUID_2,
        }

    def test_frames_without_uuid_are_left_out(self):
        """人工换上去的图会被清掉 fx_uuid（server 的上传帧分支）——那张图
        不在画布上，必须照旧上传。"""
        manifest = {'frames': [
            {'slot': 1, 'fx_uuid': UUID_1},
            {'slot': 2, 'source': 'manual_upload'},
        ]}
        slot_to_path = {1: '/p/img_001.webp', 2: '/p/img_002.webp'}

        assert VG.load_frame_canvas_uuids(manifest, slot_to_path) == {
            '/p/img_001.webp': UUID_1,
        }

    def test_same_path_claimed_by_two_uuids_is_dropped(self):
        """同一个文件被两条记录指向不同画布图 = 记录已经乱了，宁可重传也不挂错帧。"""
        manifest = {'frames': [
            {'slot': 1, 'fx_uuid': UUID_1},
            {'slot': 2, 'fx_uuid': UUID_2},
        ]}
        slot_to_path = {1: '/p/img_001.webp', 2: '/p/img_001.webp'}

        assert VG.load_frame_canvas_uuids(manifest, slot_to_path) == {}

    def test_missing_manifest_is_not_an_error(self):
        assert VG.load_frame_canvas_uuids({}, {1: '/p/img_001.webp'}) == {}
        assert VG.load_frame_canvas_uuids(None, None) == {}


# ── 2. video_generator 透传 ────────────────────────────────────────────────

_PROMPT_BLOCK = (
    "图片提示词\n图片 1:\nfirst frame prompt\n\n图片 2:\nsecond frame prompt\n\n"
    "视频提示词\n视频 1:\nvideo one\n"
)


class _FakeVideoService:
    """只记录收到的 VideoRequest，不真的开浏览器。"""

    def __init__(self):
        self.reqs = []

    def generate_videos_batch_google_fx(self, reqs, on_progress=None, cancel_check=None):
        self.reqs = list(reqs)
        return [{"status": "failed", "video_url": None, "message": "stub"} for _ in reqs]


class TestVideoRequestCarriesCanvasUuids(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old_root = server_common.OUTPUT_ROOT
        server_common.OUTPUT_ROOT = self.tmp
        self.title = 'test_canvas_frame_reuse'
        self.project_dir = server_common._get_project_dir(self.title)
        os.makedirs(os.path.join(self.project_dir, 'frames'), exist_ok=True)
        self.service = _FakeVideoService()

    def tearDown(self):
        server_common.OUTPUT_ROOT = self.old_root
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_project(self, frames):
        for seq in (1, 2):
            with open(os.path.join(self.project_dir, 'frames', f'img_{seq:03d}.webp'), 'wb') as f:
                f.write(b'fake webp bytes %d' % seq)
        manifest = {'frames': frames, 'google_fx_project_url': PROJECT_CANVAS}
        with open(os.path.join(self.project_dir, 'manifest.json'), 'w', encoding='utf-8') as f:
            json.dump(manifest, f)

    def _frame(self, seq, **extra):
        rel = os.path.relpath(
            os.path.join(self.project_dir, 'frames', f'img_{seq:03d}.webp'),
            os.path.dirname(os.path.abspath(VG.__file__)),
        ).replace('\\', '/')
        return dict({'slot': seq, 'sequence': seq, 'file': rel}, **extra)

    def _run(self):
        with patch.object(VG, 'apply_google_fx_runtime_overrides', lambda *a, **k: None), \
             patch.object(VG, '_get_account_pool_service', lambda: object()), \
             patch.object(VG, '_get_google_fx_video_service',
                          lambda: (self.service, models)):
            VG.generate_video_sequence({'googleFxUserId': 'manual-env'},
                                       self.title, _PROMPT_BLOCK)
        assert self.service.reqs, "视频请求没被送出去，测试装配有问题"
        return self.service.reqs[0]

    def test_known_frames_are_declared_as_canvas_assets(self):
        self._write_project([self._frame(1, fx_uuid=UUID_1), self._frame(2, fx_uuid=UUID_2)])

        req = self._run()

        assert req.image_uuid == UUID_1
        assert req.end_image_uuid == UUID_2
        # 绑定画布仍照旧由 _run_leg 挂上：视频就在帧序列那张画布上跑
        assert req.project_url == PROJECT_CANVAS

    def test_manually_replaced_frame_declares_no_uuid(self):
        """人工换过的帧没有 fx_uuid，绝不能拿旧 UUID 顶替——那会挂上被换掉的老图。"""
        self._write_project([self._frame(1, fx_uuid=UUID_1),
                             self._frame(2, source='manual_upload')])

        req = self._run()

        assert req.image_uuid == UUID_1
        assert req.end_image_uuid == ""


# ── 3. 视频服务：认领 vs 上传 ──────────────────────────────────────────────

class _Req:
    def __init__(self, image, end_image, image_uuid="", end_image_uuid=""):
        self.image = image
        self.end_image = end_image
        self.image_uuid = image_uuid
        self.end_image_uuid = end_image_uuid
        self.prompt = "p"
        self.model = "Veo 3.1"
        self.ratio = "9:16"


def _runner(chunk):
    return V._ChunkRunner(total_reqs=len(chunk), chunk_start=0, chunk=chunk,
                          all_slices={}, on_progress=None, cancel_check=None)


class TestCanvasAssetsAreClaimedInsteadOfUploaded(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.start = os.path.join(self.tmp, 'img_001.webp')
        self.end = os.path.join(self.tmp, 'img_002.webp')
        for p in (self.start, self.end):
            with open(p, 'wb') as f:
                f.write(b'frame')
        self.req = _Req(self.start, self.end, image_uuid=UUID_1, end_image_uuid=UUID_2)
        self.uploads = []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_upload_references(self, runner, on_canvas):
        with patch.object(V, '_get_panel_uuids', lambda page: set(on_canvas)), \
             patch.object(V, '_upload_image_to_canvas', self._fake_upload), \
             patch.object(V, '_find_add2_btn', lambda page: object()), \
             patch.object(V, '_verify_and_fix_fx_config', lambda *a, **k: None), \
             patch.object(V, 'random_sleep', lambda *a, **k: None), \
             patch.object(V.time, 'sleep', lambda *_a: None):
            return runner._upload_references(object(), [(0, self.req)])

    def _fake_upload(self, page, local_path, timeout=45, extra_known_uuids=None):
        self.uploads.append(local_path)
        return UUID_3

    def test_bound_canvas_claims_declared_uuids_without_uploading(self):
        runner = _runner([self.req])
        runner.canvas_is_bound = True

        mapping = self._run_upload_references(runner, on_canvas={UUID_1, UUID_2})

        assert self.uploads == [], "帧已经在画布上了，不该再传一遍"
        assert mapping == {self.start: UUID_1, self.end: UUID_2}

    def test_declared_uuid_absent_from_canvas_still_gets_uploaded(self):
        """manifest 说在画布上、画布 DOM 里却找不到（画布被清过/换了账号）：
        认领必须落空并改走上传，绝不能挂一张不在画布上的图。"""
        runner = _runner([self.req])
        runner.canvas_is_bound = True

        mapping = self._run_upload_references(runner, on_canvas={UUID_3})

        assert sorted(self.uploads) == sorted([self.start, self.end])
        # 上传拿到的是真实 UUID（这里两张都被打桩成同一个），撞车映射整体作废
        assert mapping == {}

    def test_unbound_canvas_ignores_declared_uuids(self):
        """本批自建的新画布上根本没有这些图，声明的 UUID 一律不算数。"""
        runner = _runner([self.req])
        runner.canvas_is_bound = False

        self._run_upload_references(runner, on_canvas={UUID_1, UUID_2})

        assert sorted(self.uploads) == sorted([self.start, self.end])


class TestPreexistingTilesAreNotAdopted(unittest.TestCase):
    def test_old_card_on_the_bound_canvas_is_never_adopted(self):
        """绑定画布上有这个本地项目历次生成留下的成品卡片，提示词切片当然对得上。
        不排除的话，用户点"重试这一段"会在重试轮把上次那段旧片认领回来。"""
        req = _Req("", "")
        runner = V._ChunkRunner(total_reqs=1, chunk_start=0, chunk=[req],
                                all_slices={0: "slice"}, on_progress=None, cancel_check=None)
        runner.gen_retry_used = 1
        runner.preexisting_tile_ids = {"fe_id_old"}

        with patch.object(V, '_scan_canvas_tiles', lambda page: [
            {"tileId": "fe_id_old", "originalTileId": "fe_id_old",
             "textClean": "slice", "videoSrc": "http://v/old.mp4", "failed": False},
        ]):
            adopted, still = runner._adopt_completed_tiles(object(), [(0, req)])

        assert adopted == []
        assert still == [(0, req)]

    def test_snapshot_only_runs_on_a_bound_canvas(self):
        """自建的新项目画布本来就是空的，不需要也不该记基线。"""
        runner = _runner([])
        runner.canvas_is_bound = False

        with patch.object(V, '_scan_canvas_tiles',
                          lambda page: [{"tileId": "x", "originalTileId": None}]):
            runner._snapshot_preexisting_tiles(object())

        assert runner.preexisting_tile_ids == set()

    def test_bound_canvas_snapshot_covers_both_tile_id_forms(self):
        runner = _runner([])
        runner.canvas_is_bound = True

        with patch.object(V, '_scan_canvas_tiles', lambda page: [
            {"tileId": "fe_new", "originalTileId": "be_old"},
            {"tileId": "fe_other", "originalTileId": None},
        ]):
            runner._snapshot_preexisting_tiles(object())

        assert runner.preexisting_tile_ids == {"fe_new", "be_old", "fe_other"}


if __name__ == '__main__':
    unittest.main()
