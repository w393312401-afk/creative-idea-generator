"""英雄展示视频（收尾步骤，2026-07-22，2026-07-23 改为只上传首帧）：帧序列生成
完毕后，额外用最后一张整体完工图作为唯一来源锚点（只作首帧，不设结束锚点），生成
一条手持展示视频提示词（视频 total_beats+1 [HERO]），并接入实际的视频生成/合成
管线。三层覆盖：

1. prompt_pipeline._compose_hero_showcase_video / compose_remaining_beats —— 提示词
   生成是锦上添花的附加步骤，失败不影响整单合成。
2. video_generator.plan_video_slots —— HERO 槽位只有起始锚点帧，没有独立的
   "下一张"结束帧（end_frame 为 None），且不应触发"首尾近乎相同"的误报警告。
3. video_generator.merge_project_videos —— HERO 片段是可选附加片段，不参与主体序列
   的完整性门禁；成功时追加在成片末尾，失败/缺失时静默跳过、不阻塞主体合并。

2026-07-31：`_HERO_SHOWCASE_ENABLED` 默认改为 False（收尾不再连放两条完工镜头，
见 docs/pacing_rhythm_balance_plan.md §7）。第 1 层的用例因此显式把开关打开再跑——
它们锁的是「开关打开时这条管线仍然完好」，这正是选择关开关而不是删代码的前提；
关闭态本身由 TestHeroShowcaseDisabledByDefault 单独锁。第 2、3 层是纯下游逻辑，
不读这个开关（存量项目的 manifest 里还有 HERO 槽位，且手动上传路径仍需可用），
所以保持原样运行。
"""
import json
import os
import re
import shutil
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

import prompt_pipeline as pp
from video_generator import plan_video_slots, merge_project_videos, PartialMergeBlocked

# 导入期快照：下面的用例会 patch 这个常量，快照让「仓库默认值」这条锁不受 patch 影响。
_HERO_ENABLED_DEFAULT = pp._HERO_SHOWCASE_ENABLED


# ─────────────────────────────────────────────────────────────────────────
# 1. prompt_pipeline: 提示词生成本身
# ─────────────────────────────────────────────────────────────────────────

class _HeroComposeFixture:
    """开关两态共用的夹具（不是 TestCase，所以不会把用例也继承过去）。

    子类用 HERO_ENABLED 声明自己要跑在哪一态上。
    """

    HERO_ENABLED = True

    def setUp(self):
        self._tmp_dir = tempfile.mkdtemp()
        self._path_patch = patch.object(pp, 'COMPOSE_CHECKPOINT_PATH', os.path.join(self._tmp_dir, 'compose_checkpoints.json'))
        self._path_patch.start()
        self.fingerprint = 'fp-hero-showcase-test'

        patches = [
            patch.object(pp, '_HERO_SHOWCASE_ENABLED', self.HERO_ENABLED),
            patch.object(pp, 'load_reference_file', return_value=''),
            patch.object(pp, 'get_cropped_templates', return_value=''),
            patch.object(pp, 'apply_proactive_fixes', side_effect=lambda i, v, im, *a, **k: (v, im)),
            patch.object(pp, 'validate_beat_prompts', return_value=[]),
            patch.object(pp, 'check_milestone_video_prompt', return_value=[]),
            patch.object(pp, 'check_milestone_image_prompt', return_value=[]),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self._path_patch.stop()
        shutil.rmtree(self._tmp_dir, ignore_errors=True)

    def _make_state(self, total_beats=3):
        return {
            'theme': 'test theme',
            'total_beats': total_beats,
            'parsed_brief': {
                'mode': 'Standard', 'theme': 'test theme',
                'destiny': 'a cozy reading nook', 'carrier': 'abandoned tram',
                'signature_anchor': 'a reclaimed brass ship porthole window',
            },
            'title': 'Test Title',
            'beat_ladder': [{'index': i, 'operation': 'repair', 'description': f'step {i}', 'bridge_stage': None}
                            for i in range(1, total_beats + 1)],
            'packet': {
                'lighting_phase_ladder': {str(i): 'ambient only' for i in range(1, total_beats + 2)},
                'object_ledger': [], 'camera_dna': 'eye-level static tripod',
                'primary_landmarks': [{'name': 'porthole window', 'grid': 'B2'}],
            },
            'brief_fingerprint': self.fingerprint,
            'image_1_prompt': 'IMAGE 1 body',
            'compiled_images': {1: 'IMAGE 1 body'},
            'compiled_videos': {},
        }

    @staticmethod
    def _beat_numbers_from_batch_user(user_text):
        return [int(n) for n in re.findall(r'====================\s*BEAT\s+(\d+)\s*====================', user_text)]

    def _fake_chat(self, calls, hero_response='A slow handheld push-in toward the porthole window, '
                                              'settling into a closer framing. Ambient noise is a quiet room tone.'):
        """批量拍生成 + 英雄展示视频调用共用的假 _chat：按 system 提示词是否带
        SINGLE-FRAME SOURCE 标记区分两种请求形状。"""
        def fake_chat(config, system, user, temperature=0.85, max_tokens=16384, timeout=240, on_chunk=None, model=None, **kwargs):
            if 'SINGLE-FRAME SOURCE' in system:
                calls.append('hero')
                return hero_response
            batch_beats = self._beat_numbers_from_batch_user(user)
            calls.extend(batch_beats)
            return "\n".join(
                f"===BEAT {b} VIDEO===\nVideo prompt for beat {b}\n===BEAT {b} IMAGE===\nImage prompt for beat {b + 1}\n===BEAT {b} TRACES===\n[]"
                for b in batch_beats
            )
        return fake_chat


class TestComposeHeroShowcaseVideo(_HeroComposeFixture, unittest.TestCase):
    """开关打开时的行为。锁的是「HERO 管线本身没被拆掉」——这正是 2026-07-31
    选择关开关而不是删代码的前提（存量项目的 manifest 里还有 HERO 槽位）。"""

    HERO_ENABLED = True

    def test_hero_video_appended_after_normal_beats_succeed(self):
        state = self._make_state(total_beats=3)
        calls = []
        with patch.object(pp, '_chat', side_effect=self._fake_chat(calls)):
            output = pp.compose_remaining_beats({}, state)

        self.assertIn('hero', calls)
        self.assertIn('视频 4 [HERO]:', output)
        self.assertIn('reference image (IMAGE 4)', output)
        self.assertIn('use IMAGE 4 as the actual first-frame image', output)
        self.assertNotIn('reference image (IMAGE 1)', output)
        self.assertIn('push-in toward the porthole window', output)
        # 正常拍的视频/图片槽位不受影响
        self.assertIn('视频 3:', output)
        self.assertIn('图片 4:', output)
        # 没有对应的"图片 5"——英雄视频没有独立的结束锚点帧
        self.assertNotIn('图片 5', output)

    def test_hero_video_failure_does_not_break_compose(self):
        state = self._make_state(total_beats=3)
        calls = []

        def flaky_chat(config, system, user, **kwargs):
            if 'SINGLE-FRAME SOURCE' in system:
                calls.append('hero')
                raise RuntimeError('simulated 8046 proxy timeout')
            batch_beats = self._beat_numbers_from_batch_user(user)
            calls.extend(batch_beats)
            return "\n".join(
                f"===BEAT {b} VIDEO===\nVideo prompt for beat {b}\n===BEAT {b} IMAGE===\nImage prompt for beat {b + 1}\n===BEAT {b} TRACES===\n[]"
                for b in batch_beats
            )

        with patch.object(pp, '_chat', side_effect=flaky_chat):
            output = pp.compose_remaining_beats({}, state)  # must not raise

        self.assertEqual(calls.count('hero'), 3, "英雄视频调用应重试满 3 次后放弃")
        self.assertNotIn('[HERO]', output)
        self.assertIn('视频 3:', output)  # 主体序列照常交付
        self.assertIsNone(pp.load_compose_checkpoint(self.fingerprint))


class TestHeroShowcaseDisabledByDefault(_HeroComposeFixture, unittest.TestCase):
    """关闭态（2026-07-31 起的默认）：收尾不再追加第二条完工镜头。

    整个 HERO 步骤——包括那次 LLM 调用——都必须消失，主体序列则完全不受影响。
    """

    HERO_ENABLED = False

    def test_default_is_disabled(self):
        # 读的是导入期快照，不是 setUp 里 patch 过的值——这条锁的是仓库里的默认值本身。
        self.assertFalse(
            _HERO_ENABLED_DEFAULT,
            "英雄展示视频应默认关闭（docs/pacing_rhythm_balance_plan.md §7）")

    def test_no_hero_slot_and_no_llm_call_when_disabled(self):
        state = self._make_state(total_beats=3)
        calls = []
        with patch.object(pp, '_chat', side_effect=self._fake_chat(calls)):
            output = pp.compose_remaining_beats({}, state)

        self.assertNotIn('hero', calls, "关闭时不应再为 HERO 发起 LLM 调用")
        self.assertNotIn('[HERO]', output)
        self.assertNotIn('视频 4', output, "视频槽位应止于 reward 拍（视频 3）")
        # 主体序列与最终揭示图照常交付
        self.assertIn('视频 3:', output)
        self.assertIn('图片 4:', output)

    def test_compose_hero_returns_empty_without_touching_state(self):
        state = self._make_state(total_beats=3)
        state['compiled_images'][4] = 'IMAGE 4 finished reveal'
        with patch.object(pp, '_chat', side_effect=AssertionError('不应被调用')):
            self.assertEqual(pp._compose_hero_showcase_video({}, state), '')


# ─────────────────────────────────────────────────────────────────────────
# 2. video_generator.plan_video_slots: HERO 单帧槽位规划
# ─────────────────────────────────────────────────────────────────────────

class _TmpDirCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.videos_dir = os.path.join(self.tmp, 'videos')
        self.frames_dir = os.path.join(self.tmp, 'frames')
        os.makedirs(self.videos_dir)
        os.makedirs(self.frames_dir)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _touch(self, path, content=b'x'):
        with open(path, 'wb') as f:
            f.write(content)
        return path

    def _solid_png(self, path, color=(128, 128, 128)):
        Image.new('RGB', (64, 114), color).save(path, 'PNG')
        return path


class TestPlanVideoSlotsHero(_TmpDirCase):
    def test_hero_slot_has_only_start_frame_no_end_anchor(self):
        frame2 = self._touch(os.path.join(self.frames_dir, 'img_002.webp'))
        frames = {1: self._touch(os.path.join(self.frames_dir, 'img_001.webp')), 2: frame2}
        videos = {1: {'body': 'v1 from IMAGE 1 to IMAGE 2', 'meta': ''},
                  2: {'body': 'hero showcase clip', 'meta': 'HERO'}}
        plans = plan_video_slots(videos, frames, {}, self.videos_dir)
        hero_plan = next(p for p in plans if p['slot'] == 2)
        self.assertEqual(hero_plan['action'], 'generate')
        self.assertEqual(hero_plan['start_frame'], frame2)
        self.assertIsNone(hero_plan['end_frame'], "英雄展示视频只上传首帧，不应有独立的结束锚点")
        self.assertEqual(hero_plan['start_anchor_slot'], 2)

    def test_hero_slot_blocked_when_anchor_frame_missing(self):
        frames = {1: self._touch(os.path.join(self.frames_dir, 'img_001.webp'))}  # 帧 2 缺失
        videos = {2: {'body': 'hero showcase clip', 'meta': 'HERO'}}
        plans = plan_video_slots(videos, frames, {}, self.videos_dir)
        self.assertEqual(plans[0]['action'], 'blocked')
        self.assertIn('IMAGE 2', plans[0]['reason'])

    def test_hero_slot_skips_too_similar_warning_on_identical_frame(self):
        # 没有结束锚点帧（end_frame=None），frame_pair_contract 应自动跳过比较，
        # 不会误报"首尾近乎相同"
        frame_path = self._solid_png(os.path.join(self.frames_dir, 'img_002.webp'))
        frames = {1: self._touch(os.path.join(self.frames_dir, 'img_001.webp')), 2: frame_path}
        videos = {2: {'body': 'hero showcase clip', 'meta': 'HERO'}}
        plans = plan_video_slots(videos, frames, {}, self.videos_dir)
        self.assertNotIn('warning', plans[0])

    def test_normal_slot_still_warns_on_identical_frames(self):
        # 对照组：普通槽位（非 HERO）用两张一模一样的图，警告逻辑应照常触发——
        # 证明上面那条豁免只对 HERO 生效，不是把检查整体删掉了。
        p1 = self._solid_png(os.path.join(self.frames_dir, 'img_001.webp'))
        p2 = self._solid_png(os.path.join(self.frames_dir, 'img_002.webp'))
        frames = {1: p1, 2: p2}
        videos = {1: {'body': 'v1', 'meta': ''}}
        plans = plan_video_slots(videos, frames, {}, self.videos_dir)
        self.assertIn('warning', plans[0])
        self.assertIn('近乎相同', plans[0]['warning'])


# ─────────────────────────────────────────────────────────────────────────
# 3. video_generator.merge_project_videos: 可选附加英雄片段
# ─────────────────────────────────────────────────────────────────────────

class TestMergeHeroClip(_TmpDirCase):
    def _write_manifest(self, frames_n, videos_entries, title='Hero Merge Test'):
        frames = []
        for i in range(1, frames_n + 1):
            frames.append({'slot': i, 'sequence': i,
                           'file': os.path.relpath(os.path.join(self.frames_dir, f'img_{i:03d}.webp'),
                                                   self.tmp).replace('\\', '/')})
            self._touch(os.path.join(self.frames_dir, f'img_{i:03d}.webp'))
        manifest = {'title': title, 'frames': frames, 'videos': videos_entries}
        with open(os.path.join(self.tmp, 'manifest.json'), 'w', encoding='utf-8') as f:
            json.dump(manifest, f)

    def _fake_run_factory(self, captured):
        def fake_run(cmd, cwd=None, **kwargs):
            captured.setdefault('calls', []).append(cmd)
            if cmd[0] == 'ffprobe':
                class Probe:
                    returncode = 0
                    stderr = ''
                    stdout = '5.0'
                return Probe()
            # ffmpeg concat merge: capture the concat list content before it's deleted
            i_idx = cmd.index('-i')
            concat_list_path = cmd[i_idx + 1]
            with open(concat_list_path, 'r', encoding='utf-8') as f:
                captured['concat_list'] = f.read()
            out_path = cmd[-1]
            os.makedirs(os.path.dirname(out_path) or '.', exist_ok=True)
            with open(out_path, 'wb') as f:
                f.write(b'fake-merged-mp4')

            class Ok:
                returncode = 0
                stderr = ''
            return Ok()
        return fake_run

    def test_hero_clip_appended_last_when_present_and_valid(self):
        vid1 = self._touch(os.path.join(self.videos_dir, 'vid_001.mp4'))
        vid2 = self._touch(os.path.join(self.videos_dir, 'vid_002.mp4'))
        vid_hero = self._touch(os.path.join(self.videos_dir, 'vid_003.mp4'))
        self._write_manifest(3, [
            {'slot': 1, 'status': 'success', 'file': 'videos/vid_001.mp4', 'start_anchor_slot': 1},
            {'slot': 2, 'status': 'success', 'file': 'videos/vid_002.mp4', 'start_anchor_slot': 2},
            {'slot': 3, 'status': 'success', 'file': 'videos/vid_003.mp4', 'start_anchor_slot': 3,
             'meta': 'HERO', 'is_hero': True},
        ])
        captured = {}
        with patch('video_generator.subprocess.run', side_effect=self._fake_run_factory(captured)):
            result = merge_project_videos(self.tmp)

        self.assertIsNotNone(result)
        self.assertEqual(result['status'], 'success')
        concat_lines = [l for l in captured['concat_list'].splitlines() if l.strip()]
        self.assertEqual(len(concat_lines), 3)
        self.assertIn(os.path.basename(vid_hero), concat_lines[-1],
                      "英雄片段必须排在拼接列表的最后一位")
        self.assertNotIn(os.path.basename(vid1), concat_lines[-1])

    def test_failed_hero_clip_is_skipped_without_blocking_main_merge(self):
        self._touch(os.path.join(self.videos_dir, 'vid_001.mp4'))
        self._touch(os.path.join(self.videos_dir, 'vid_002.mp4'))
        self._write_manifest(3, [
            {'slot': 1, 'status': 'success', 'file': 'videos/vid_001.mp4', 'start_anchor_slot': 1},
            {'slot': 2, 'status': 'success', 'file': 'videos/vid_002.mp4', 'start_anchor_slot': 2},
            {'slot': 3, 'status': 'failed', 'error': 'i2v gateway timeout',
             'meta': 'HERO', 'is_hero': True},
        ])
        captured = {}
        with patch('video_generator.subprocess.run', side_effect=self._fake_run_factory(captured)):
            result = merge_project_videos(self.tmp)  # must not raise PartialMergeBlocked

        self.assertIsNotNone(result)
        self.assertEqual(result['status'], 'success')
        concat_lines = [l for l in captured['concat_list'].splitlines() if l.strip()]
        self.assertEqual(len(concat_lines), 2, "失败的英雄片段不应计入拼接列表")

    def test_missing_hero_entry_behaves_exactly_like_before_the_feature(self):
        self._touch(os.path.join(self.videos_dir, 'vid_001.mp4'))
        self._touch(os.path.join(self.videos_dir, 'vid_002.mp4'))
        self._write_manifest(3, [
            {'slot': 1, 'status': 'success', 'file': 'videos/vid_001.mp4', 'start_anchor_slot': 1},
            {'slot': 2, 'status': 'success', 'file': 'videos/vid_002.mp4', 'start_anchor_slot': 2},
        ])
        captured = {}
        with patch('video_generator.subprocess.run', side_effect=self._fake_run_factory(captured)):
            result = merge_project_videos(self.tmp)

        self.assertIsNotNone(result)
        self.assertEqual(result['status'], 'success')
        concat_lines = [l for l in captured['concat_list'].splitlines() if l.strip()]
        self.assertEqual(len(concat_lines), 2)


if __name__ == '__main__':
    unittest.main()
