"""成片首帧烧录封面（video_generator.prepend_cover_intro 一路到 merge_project_videos）。

平台取缩略图吃的是成片第一帧，而正片第一帧是施工前的空场——这条链路存在的唯一理由
就是让缩略图变成封面图。两个静默出错点在这里钉死：
  1) 封面静帧段的**编码时长要先按 speed 放大**，因为合并阶段还会整体除以 speed。
     漏掉这一步时，2 倍速下"一帧"会变成半帧、直接被编码器丢掉——成片看起来完全正常，
     只有缩略图默默回到空场。
  2) 多输入（PACE）合并路径下封面段的 clip_speed 必须是 1.0 并**占住队首那一位**，
     否则 clip_speeds 与 video_files 错位，每段都会套上别人的时间缩放。
"""
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

import server_common
import video_generator
from video_generator import (
    normalize_cover_burn,
    resolve_merge_cover,
    build_cover_intro_clip,
    merge_project_videos,
)


class TestNormalizeCoverBurn(unittest.TestCase):
    def test_档位归一(self):
        self.assertEqual(normalize_cover_burn('frame'), 0.0)   # 默认：正好一帧
        self.assertEqual(normalize_cover_burn(True), 0.0)
        self.assertEqual(normalize_cover_burn(0), 0.0)
        self.assertEqual(normalize_cover_burn('0.5s'), 0.5)
        self.assertEqual(normalize_cover_burn(1), 1.0)
        self.assertEqual(normalize_cover_burn('1'), 1.0)

    def test_关闭与非法值一律不烧(self):
        for value in (None, False, 'off', 'none', '', 'abc', -1):
            self.assertIsNone(normalize_cover_burn(value), value)

    def test_停留时长有上限(self):
        self.assertEqual(normalize_cover_burn(999), video_generator.COVER_BURN_MAX_SECONDS)


class TestResolveMergeCover(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        # 封面的 outputs/ 边界基于 server_common.OUTPUT_ROOT；指到临时目录后
        # 这里的 cover_*.webp 才是"合法的项目产物"。
        self._patcher = patch.object(server_common, 'OUTPUT_ROOT', self.tmp)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _cover(self, name, age=0):
        path = os.path.join(self.tmp, name)
        with open(path, 'wb') as f:
            f.write(b'cover-bytes')
        if age:
            os.utime(path, (0, 1_000_000 - age))
        return path

    def test_优先级_请求_用途_主封面(self):
        requested = self._cover('cover_requested.webp')
        role_video = self._cover('cover_video.webp')
        role_project = self._cover('cover_project.webp')
        active = self._cover('cover_active.webp')
        manifest = {'cover_roles': {'video': role_video, 'project': role_project},
                    'active_cover': active}

        self.assertEqual(resolve_merge_cover(self.tmp, manifest, requested), requested)
        self.assertEqual(resolve_merge_cover(self.tmp, manifest), role_video)
        manifest['cover_roles'].pop('video')
        self.assertEqual(resolve_merge_cover(self.tmp, manifest), role_project)
        manifest['cover_roles'].clear()
        self.assertEqual(resolve_merge_cover(self.tmp, manifest), active)

    def test_没有登记时回落到目录里最新的一张(self):
        self._cover('cover_old.webp', age=500)
        newest = self._cover('cover_new.webp')
        self.assertEqual(resolve_merge_cover(self.tmp, {}), newest)

    def test_outputs_之外的路径不会被烧进成片(self):
        outside = tempfile.mkdtemp()
        try:
            stray = os.path.join(outside, 'stray.webp')
            with open(stray, 'wb') as f:
                f.write(b'x')
            # 越界的显式指定既不被采纳，也不该把整次合并带崩：回落到目录里的封面
            fallback = self._cover('cover_fallback.webp')
            self.assertEqual(resolve_merge_cover(self.tmp, {}, stray), fallback)
        finally:
            shutil.rmtree(outside, ignore_errors=True)

    def test_没有任何封面时返回_None(self):
        self.assertIsNone(resolve_merge_cover(self.tmp, {}))


class TestBuildCoverIntroClip(unittest.TestCase):
    """封面静帧段的编码参数。"""

    PARAMS = {'width': 1080, 'height': 1920, 'fps': 30.0, 'duration': 8.0}

    def _build(self, seconds, speed, has_audio=False):
        captured = {}

        def fake_run(cmd, **kwargs):
            captured['cmd'] = cmd
            with open(cmd[-1], 'wb') as f:
                f.write(b'intro')

            class Ok:
                returncode = 0
                stderr = ''
                stdout = ''
            return Ok()

        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        out = os.path.join(tmp, 'intro.mp4')
        with patch.object(video_generator, '_ffprobe_video_params', return_value=self.PARAMS), \
             patch.object(video_generator, '_ffprobe_audio_params',
                          return_value={'sample_rate': 48000, 'channels': 2}), \
             patch('video_generator.subprocess.run', side_effect=fake_run):
            result = build_cover_intro_clip('cover.webp', 'clip.mp4', out,
                                            seconds=seconds, speed=speed, has_audio=has_audio)
        return result, captured['cmd']

    def test_单帧在_2_倍速下编码_2_帧(self):
        """合并阶段会整体除以 speed，所以这里必须先乘回去——否则一帧被丢，缩略图又变回空场。"""
        _, cmd = self._build(seconds=0.0, speed=2.0)
        self.assertEqual(cmd[cmd.index('-frames:v') + 1], '2')

    def test_单帧在_无加速下编码_1_帧(self):
        _, cmd = self._build(seconds=0.0, speed=1.0)
        self.assertEqual(cmd[cmd.index('-frames:v') + 1], '1')

    def test_停留一秒按帧率和倍速换算(self):
        _, cmd = self._build(seconds=1.0, speed=2.0)  # 30fps * 1s * 2x
        self.assertEqual(cmd[cmd.index('-frames:v') + 1], '60')
        self.assertAlmostEqual(float(cmd[cmd.index('-t') + 1]), 2.0, places=3)

    def test_画幅对齐首个片段且补黑边不裁切(self):
        _, cmd = self._build(seconds=0.0, speed=1.0)
        vf = cmd[cmd.index('-vf') + 1]
        self.assertIn('scale=1080:1920:force_original_aspect_ratio=decrease', vf)
        self.assertIn('pad=1080:1920', vf)
        self.assertIn('setsar=1', vf)

    def test_成片有音轨时配等长静音(self):
        _, cmd = self._build(seconds=0.0, speed=1.0, has_audio=True)
        self.assertIn('anullsrc=channel_layout=stereo:sample_rate=48000', ' '.join(cmd))
        self.assertEqual(cmd[cmd.index('-ar') + 1], '48000')

    def test_无音轨时不带音频输入(self):
        _, cmd = self._build(seconds=0.0, speed=1.0)
        self.assertNotIn('anullsrc', ' '.join(cmd))

    def test_探不到画幅时放弃烧录而不是抛异常(self):
        with patch.object(video_generator, '_ffprobe_video_params', return_value=None):
            self.assertIsNone(build_cover_intro_clip('cover.webp', 'clip.mp4', 'out.mp4'))


class TestMergeBurnsCoverFirstFrame(unittest.TestCase):
    """合并主路径：封面段进队首、进 concat 清单、进返回值。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.videos_dir = os.path.join(self.tmp, 'videos')
        self.frames_dir = os.path.join(self.tmp, 'frames')
        os.makedirs(self.videos_dir)
        os.makedirs(self.frames_dir)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _touch(self, path, data=b'x'):
        with open(path, 'wb') as f:
            f.write(data)
        return path

    def _write_manifest(self, clips=2, extra=None):
        frames, videos = [], []
        for i in range(1, clips + 2):
            frames.append({'slot': i, 'sequence': i,
                           'file': os.path.relpath(self._touch(
                               os.path.join(self.frames_dir, f'img_{i:03d}.webp')),
                               self.tmp).replace('\\', '/')})
        for slot in range(1, clips + 1):
            self._touch(os.path.join(self.videos_dir, f'vid_{slot:03d}.mp4'))
            videos.append({'slot': slot, 'status': 'success',
                           'file': f'videos/vid_{slot:03d}.mp4', 'source': 'manual_upload'})
        manifest = {'title': '首帧封面测试', 'frames': frames, 'videos': videos}
        manifest.update(extra or {})
        with open(os.path.join(self.tmp, 'manifest.json'), 'w', encoding='utf-8') as f:
            json.dump(manifest, f)

    def _fake_run_factory(self, captured):
        def fake_run(cmd, **kwargs):
            captured.setdefault('calls', []).append(cmd)
            if cmd[0] == 'ffprobe':
                class Probe:
                    returncode = 0
                    stderr = ''
                    stdout = '5.0'
                return Probe()
            if '-loop' in cmd:            # 封面静帧段
                captured['intro_cmd'] = cmd
            elif '-f' in cmd and 'concat' in cmd:
                with open(cmd[cmd.index('-i') + 1], 'r', encoding='utf-8') as f:
                    captured['concat_list'] = f.read()
            self._touch(cmd[-1], b'fake-mp4')

            class Ok:
                returncode = 0
                stderr = ''
            return Ok()
        return fake_run

    def _merge(self, captured, **kwargs):
        # 段内节奏重映射会额外跑一轮 ffmpeg 探测/编码，与本用例无关，整体关掉
        with patch.object(video_generator, 'retime_clips_for_merge',
                          side_effect=lambda files, tmp, metas=None: (list(files), [])), \
             patch.object(video_generator, '_ffprobe_video_params',
                          return_value={'width': 1080, 'height': 1920, 'fps': 30.0, 'duration': 8.0}), \
             patch('video_generator.verify_video_anchors', return_value=(True, '')), \
             patch('video_generator.subprocess.run', side_effect=self._fake_run_factory(captured)):
            return merge_project_videos(self.tmp, **kwargs)

    def test_封面被接在所有片段之前(self):
        self._touch(os.path.join(self.tmp, 'cover_001.webp'), b'cover')
        self._write_manifest()
        captured = {}
        result = self._merge(captured)

        lines = [l for l in captured['concat_list'].splitlines() if l.strip()]
        self.assertEqual(len(lines), 3)                       # 封面 + 2 段正片
        self.assertIn('cover_intro.mp4', lines[0])
        self.assertIn('cover_001.webp', result['cover_first_frame']['url'])
        self.assertEqual(result['cover_first_frame']['seconds'], 0.0)

    def test_按用途登记的那张优先于目录里最新的(self):
        self._touch(os.path.join(self.tmp, 'cover_newest.webp'), b'cover')
        picked = self._touch(os.path.join(self.tmp, 'cover_picked.webp'), b'cover')
        os.utime(picked, (0, 1))          # 故意做成"最旧"，证明选中的不是靠 mtime 赢的
        with patch.object(server_common, 'OUTPUT_ROOT', self.tmp):
            self._write_manifest(extra={'cover_roles': {'video': picked}})
            captured = {}
            result = self._merge(captured)
        self.assertIn('cover_picked.webp', result['cover_first_frame']['url'])

    def test_关闭时成片里没有封面段(self):
        self._touch(os.path.join(self.tmp, 'cover_001.webp'), b'cover')
        self._write_manifest()
        captured = {}
        result = self._merge(captured, cover_burn='off')

        lines = [l for l in captured['concat_list'].splitlines() if l.strip()]
        self.assertEqual(len(lines), 2)
        self.assertNotIn('cover_first_frame', result)

    def test_没有封面时合并照常完成(self):
        self._write_manifest()
        captured = {}
        result = self._merge(captured)
        self.assertEqual(result['status'], 'success')
        self.assertNotIn('cover_first_frame', result)
        lines = [l for l in captured['concat_list'].splitlines() if l.strip()]
        self.assertEqual(len(lines), 2)

    def test_封面编码失败不影响成片(self):
        """fail-open：封面是锦上添花，绝不能因为它让用户拿不到成片。"""
        self._touch(os.path.join(self.tmp, 'cover_001.webp'), b'cover')
        self._write_manifest()
        captured = {}
        with patch.object(video_generator, 'build_cover_intro_clip', return_value=None):
            result = self._merge(captured)
        self.assertEqual(result['status'], 'success')
        self.assertNotIn('cover_first_frame', result)

    def test_按拍重变速时封面占住队首且系数为_1(self):
        """clip_speeds 与 video_files 必须逐位对齐，否则每段都套上别人的时间缩放。"""
        self._touch(os.path.join(self.tmp, 'cover_001.webp'), b'cover')
        self._write_manifest()
        with open(os.path.join(self.tmp, 'manifest.json'), 'r+', encoding='utf-8') as f:
            data = json.load(f)
            data['videos'][0]['clip_speed'] = 1.25
            f.seek(0), f.truncate()
            json.dump(data, f)

        captured = {}
        result = self._merge(captured)
        merge_cmd = next(c for c in captured['calls']
                         if c[0] == 'ffmpeg' and '-filter_complex' in c and '-loop' not in c)
        filt = merge_cmd[merge_cmd.index('-filter_complex') + 1]
        inputs = [merge_cmd[i + 1] for i, tok in enumerate(merge_cmd) if tok == '-i']
        self.assertIn('cover_intro.mp4', inputs[0])
        # 封面段（输入 0）：clip_speed=1.0 → setpts=1/2；正片首段带 PACE 1.25 → 0.625
        self.assertIn('[0:v]setpts=0.5*PTS[v0]', filt)
        self.assertIn('[1:v]setpts=0.625*PTS[v1]', filt)
        self.assertIn('cover_001.webp', result['cover_first_frame']['url'])


class TestCoverRolesFeedTheOtherTwoUses(unittest.TestCase):
    """同一份用途登记要同时喂到另外两个用途：帧 1 的参考图与项目卡片缩略图。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.clean = os.path.join(self.tmp, 'cover_clean.webp')
        self.hooked = os.path.join(self.tmp, 'cover_with_text.webp')
        for path in (self.clean, self.hooked):
            with open(path, 'wb') as f:
                f.write(b'cover-bytes')
        self._patchers = [patch.object(server_common, 'OUTPUT_ROOT', self.tmp),
                          patch.object(server_common, '_get_project_dir', lambda key: self.tmp)]
        for p in self._patchers:
            p.start()

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_manifest(self, roles, active=None):
        data = {'title': 't', 'cover_roles': roles}
        if active:
            data['active_cover'] = active
        with open(os.path.join(self.tmp, 'manifest.json'), 'w', encoding='utf-8') as f:
            json.dump(data, f)

    def test_帧1参考图用登记的干净封面而不是最新那张(self):
        """带文案的封面当参考图会把文字带进生成画面——这正是要分开选的理由。"""
        os.utime(self.hooked, None)      # 带文案的那张更新，回落逻辑会选它
        self._write_manifest({'frame1': self.clean})
        picked = server_common.resolve_cover_reference({}, 't')
        self.assertEqual(os.path.basename(picked), 'cover_clean.webp')

    def test_本次请求显式指定仍然压过登记(self):
        self._write_manifest({'frame1': self.clean})
        picked = server_common.resolve_cover_reference({'coverReferencePath': self.hooked}, 't')
        self.assertEqual(os.path.basename(picked), 'cover_with_text.webp')

    def test_没收藏的项目也认登记的项目封面(self):
        """磁盘扫描那条路（未收藏进点子库的项目）默认按 mtime 取最新一张，登记要压过它。"""
        self._write_manifest({'project': self.clean})
        os.utime(self.hooked, None)
        stats = server_common._proj_asset_stats(None, 't', os.path.dirname(self.tmp))
        self.assertTrue(stats['cover'].endswith('cover_clean.webp'), stats['cover'])

    def test_项目卡片缩略图取_project_用途(self):
        item = {'covers': ['/outputs/p/cover_1.webp', '/outputs/p/cover_2.webp'],
                'activeCoverUrl': '/outputs/p/cover_1.webp',
                'coverRoles': {'project': '/outputs/p/cover_2.webp'}}
        self.assertEqual(server_common.item_project_cover(item), '/outputs/p/cover_2.webp')
        item.pop('coverRoles')
        self.assertEqual(server_common.item_project_cover(item), '/outputs/p/cover_1.webp')
        item.pop('activeCoverUrl')
        self.assertEqual(server_common.item_project_cover(item), '/outputs/p/cover_1.webp')


class TestCoverRolesEndpoint(unittest.TestCase):
    """/api/cover_roles：把「哪个用途用哪张封面」落进 manifest。

    落盘是必须的——自动合并与断线恢复都发生在没有请求体的服务端，只认磁盘上这份。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.covers = {name: self._cover(name) for name in
                       ('cover_a.webp', 'cover_b.webp', 'cover_c.webp')}
        self._patchers = [
            patch.object(server_common, 'OUTPUT_ROOT', self.tmp),
            patch.object(__import__('server'), '_get_project_dir', lambda title: self.tmp),
        ]
        for p in self._patchers:
            p.start()

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _cover(self, name):
        path = os.path.join(self.tmp, name)
        with open(path, 'wb') as f:
            f.write(b'cover-bytes')
        return path

    def _post(self, payload):
        import io
        from email.message import Message
        import server

        raw = json.dumps(payload).encode('utf-8')
        handler = object.__new__(server.SparkRequestHandler)
        handler.path = '/api/cover_roles'
        headers = Message()
        headers['Content-Type'] = 'application/json'
        headers['Content-Length'] = str(len(raw))
        handler.headers = headers
        handler.rfile = io.BytesIO(raw)
        sent = []
        handler._send_json = lambda obj, status=200: sent.append((obj, status))
        server.SparkRequestHandler.do_POST(handler)
        return sent[0]

    def _manifest(self):
        with open(os.path.join(self.tmp, 'manifest.json'), 'r', encoding='utf-8') as f:
            return json.load(f)

    def test_三个用途分别落进_manifest(self):
        body, status = self._post({
            'title': '封面用途', 'active_cover': self.covers['cover_a.webp'],
            'roles': {'project': self.covers['cover_a.webp'],
                      'video': self.covers['cover_b.webp'],
                      'frame1': self.covers['cover_c.webp']},
        })
        self.assertEqual(status, 200, body)
        roles = self._manifest()['cover_roles']
        self.assertTrue(roles['project'].endswith('cover_a.webp'))
        self.assertTrue(roles['video'].endswith('cover_b.webp'))
        self.assertTrue(roles['frame1'].endswith('cover_c.webp'))
        self.assertTrue(self._manifest()['active_cover'].endswith('cover_a.webp'))

    def test_空值表示该用途回落主封面(self):
        self._post({'title': '封面用途', 'roles': {'video': self.covers['cover_b.webp']}})
        self._post({'title': '封面用途', 'roles': {'video': ''}})
        self.assertEqual(self._manifest()['cover_roles'], {})

    def test_outputs_之外的路径被拒绝且不落盘(self):
        outside = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, outside, True)
        stray = os.path.join(outside, 'stray.webp')
        with open(stray, 'wb') as f:
            f.write(b'x')

        body, status = self._post({'title': '封面用途', 'roles': {'video': stray}})
        self.assertEqual(status, 200, body)
        self.assertEqual(body['rejected'], ['video'])
        self.assertEqual(self._manifest()['cover_roles'], {})

    def test_登记后合并会烧这一张(self):
        """端到端：接口写下的用途，合并侧（resolve_merge_cover）当场就认。"""
        self._post({'title': '封面用途', 'roles': {'video': self.covers['cover_b.webp']}})
        picked = resolve_merge_cover(self.tmp, self._manifest())
        self.assertEqual(os.path.basename(picked), 'cover_b.webp')

    def test_缺少标题时报错(self):
        body, status = self._post({'roles': {}})
        self.assertEqual(status, 400, body)


if __name__ == '__main__':
    unittest.main()
