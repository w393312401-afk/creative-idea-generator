"""滚动现实校准 + 链中回望重锚定测试。

合成期写死的 Locked anchors 句在首帧 refine 之后再没对过账，链条越往后与现实差距
越大；逐帧质检又只比相邻对。新增链路：分段渲染（_render_frames_with_checkpoints，
段不跨镜头族）→ 段间检查点（_checkpoint_reality_sync）= 链中回望（检出真实漂移就地
重锚定，config['_reanchors'] 带内通道）+ 锚点句滚动校准（recalibrate_anchor_stanza
VLM 核对 + replace_locked_anchor_stanza 整句手术）。重锚定经 resolve_family_anchor
被逐帧漂移复查/恢复轮/收尾回望共同消费。
"""
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

import prompt_pipeline
import pipeline_orchestrator
from prompt_pipeline import (
    extract_locked_anchor_stanza,
    replace_locked_anchor_stanza,
    _stanza_anchor_names,
    recalibrate_anchor_stanza,
    resolve_family_anchor,
    is_judge_unavailable_verdict,
    _format_prompt_block,
)
from pipeline_orchestrator import (
    _checkpoint_reality_sync,
    _render_frames_with_checkpoints,
    _segment_progress,
)

# 2026-08-05：锚点句改成散文形态。格位标签与数字会被图像模型渲成画面上的文字
# （实测判废原因："画面中出现了多处异常的字母叠加渲染标记（A、A、C）"），
# recalibrate_anchor_stanza 现在也会拒收带记号或数字的校准结果。
STANZA = ("Locked anchors: rusted silo shell at the centre of the frame, rising to about two "
          "thirds of the frame height; concrete footing across the lower centre of the frame, "
          "rising to about a sixth of the frame height.")
NEW_STANZA = ("Locked anchors: rusted silo shell at the centre of the frame, rising to about "
              "half the frame height; concrete footing in the lower left of the frame, "
              "rising to about a sixth of the frame height.")


class TestStanzaSurgery(unittest.TestCase):
    """锚点句抽取/整句替换/名称解析——确定性手术的三个刀具。"""

    def test_extract_finds_canonical_stanza(self):
        prompt = f"Wide static shot of the silo. {STANZA} Golden hour light."
        self.assertEqual(extract_locked_anchor_stanza(prompt), STANZA)

    def test_extract_returns_none_when_absent(self):
        self.assertIsNone(extract_locked_anchor_stanza("Just a plain prompt."))
        self.assertIsNone(extract_locked_anchor_stanza(""))
        self.assertIsNone(extract_locked_anchor_stanza(None))

    def test_replace_swaps_stanza_in_place(self):
        prompt = f"Opening text. {STANZA} Closing text."
        out, replaced = replace_locked_anchor_stanza(prompt, NEW_STANZA)
        self.assertTrue(replaced)
        self.assertIn(NEW_STANZA, out)
        self.assertNotIn("65 percent", out)
        self.assertTrue(out.startswith("Opening text."))
        self.assertTrue(out.endswith("Closing text."))

    def test_replace_absorbs_duplicate_stanzas(self):
        prompt = f"Start. {STANZA} Middle. {STANZA} End."
        out, replaced = replace_locked_anchor_stanza(prompt, NEW_STANZA)
        self.assertTrue(replaced)
        self.assertEqual(out.count("Locked anchors:"), 1)

    def test_replace_noop_without_stanza(self):
        prompt = "No stanza here."
        out, replaced = replace_locked_anchor_stanza(prompt, NEW_STANZA)
        self.assertFalse(replaced)
        self.assertEqual(out, prompt)

    def test_stanza_anchor_names(self):
        self.assertEqual(_stanza_anchor_names(STANZA),
                         ['rusted silo shell', 'concrete footing'])


class TestRecalibrateAnchorStanza(unittest.TestCase):
    """VLM 校准步：合规修正采纳；UNCHANGED/格式违规/改名/服务异常/off 档一律 None。"""

    def _call(self, response, config=None, stanza=STANZA):
        with patch.object(prompt_pipeline, '_multimodal_chat', return_value=response):
            return recalibrate_anchor_stanza(config or {}, 'frame.webp', stanza)

    def test_valid_correction_accepted(self):
        self.assertEqual(self._call(NEW_STANZA), NEW_STANZA)

    def test_unchanged_returns_none(self):
        self.assertIsNone(self._call('UNCHANGED'))

    def test_format_violations_rejected(self):
        self.assertIsNone(self._call('The anchors moved a bit.'))          # 缺前缀
        self.assertIsNone(self._call(f'{NEW_STANZA} 50%'))                  # % 字形
        self.assertIsNone(self._call(NEW_STANZA.rstrip('.')))               # 缺句号
        self.assertIsNone(self._call(f'{NEW_STANZA} Extra sentence.'))      # 多句

    def test_notation_relapse_rejected(self):
        """判定模型退回旧格式时必须整句弃用：这句会写进链条剩余每一帧，
        放进去一个 Grid 标签或一个数字，等于把文字水印批量注入后半条链。"""
        relapsed = ("Locked anchors: rusted silo shell at Grid B2 holding 50 percent of frame "
                    "height; concrete footing at Grid C1 holding 20 percent of frame height.")
        self.assertIsNone(self._call(relapsed))

    def test_dropped_anchor_name_rejected(self):
        # 名称集合守恒：少了 concrete footing 的输出不可采纳
        bad = "Locked anchors: rusted silo shell at the centre of the frame, rising to about half the frame height."
        self.assertIsNone(self._call(bad))

    def test_identical_output_returns_none(self):
        self.assertIsNone(self._call(STANZA))

    def test_off_level_skips_without_vlm_call(self):
        def _boom(*a, **k):
            raise AssertionError('off 档不得发 VLM 请求')
        with patch.object(prompt_pipeline, '_multimodal_chat', _boom):
            self.assertIsNone(recalibrate_anchor_stanza({'qaGateLevel': 'off'}, 'f.webp', STANZA))

    def test_judge_error_returns_none(self):
        with patch.object(prompt_pipeline, '_multimodal_chat', side_effect=RuntimeError('boom')):
            self.assertIsNone(recalibrate_anchor_stanza({}, 'f.webp', STANZA))

    def test_empty_stanza_returns_none(self):
        self.assertIsNone(recalibrate_anchor_stanza({}, 'f.webp', None))


class TestResolveFamilyAnchor(unittest.TestCase):
    """重锚定感知的族锚解析：带内通道 config['_reanchors']。"""

    VIDEOS = {i: {'body': f'v{i}', 'meta': 'BRIDGE' if i == 4 else ''} for i in range(1, 8)}

    def test_no_marks_falls_back_to_family_anchor(self):
        self.assertEqual(resolve_family_anchor({}, self.VIDEOS, 3), 1)
        self.assertEqual(resolve_family_anchor({'_reanchors': []}, self.VIDEOS, 6), 5)

    def test_mark_rebases_downstream_frames_same_family(self):
        cfg = {'_reanchors': [3]}
        self.assertEqual(resolve_family_anchor(cfg, self.VIDEOS, 4), 3)
        self.assertEqual(resolve_family_anchor(cfg, self.VIDEOS, 2), 1)   # 重锚点之前不受影响
        self.assertEqual(resolve_family_anchor(cfg, self.VIDEOS, 3), 3)   # 重锚点帧即族锚本身

    def test_mark_does_not_leak_across_bridge(self):
        # 族1 的重锚定（IMG 3）不得影响 BRIDGE 之后族2（锚 5）的帧
        cfg = {'_reanchors': [3]}
        self.assertEqual(resolve_family_anchor(cfg, self.VIDEOS, 6), 5)

    def test_latest_mark_wins(self):
        cfg = {'_reanchors': [2, 3]}
        self.assertEqual(resolve_family_anchor(cfg, self.VIDEOS, 4), 3)

    def test_invalid_marks_ignored(self):
        cfg = {'_reanchors': ['x', None, 3]}
        self.assertEqual(resolve_family_anchor(cfg, self.VIDEOS, 4), 3)


class _TmpProjectCase(unittest.TestCase):
    TITLE = 'proj'

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.frames_dir = os.path.join(self.tmp, 'frames')
        os.makedirs(self.frames_dir)
        with open(os.path.join(self.tmp, 'manifest.json'), 'w', encoding='utf-8') as f:
            json.dump({'title': self.TITLE, 'frames': []}, f)
        self._patch = patch.object(pipeline_orchestrator, '_get_project_dir',
                                   lambda title: self.tmp)
        self._patch.start()
        self.events = []

    def tearDown(self):
        self._patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _touch_frames(self, seqs):
        for i in seqs:
            with open(os.path.join(self.frames_dir, f'img_{i:03d}.webp'), 'wb') as f:
                f.write(b'x')

    def _on_progress(self, stage, payload):
        self.events.append((stage, payload))
        return None

    def _manifest(self):
        with open(os.path.join(self.tmp, 'manifest.json'), 'r', encoding='utf-8') as f:
            return json.load(f)

    def _images(self, n, stanza=True):
        return {i: {'body': (f'frame {i} body. {STANZA}' if stanza else f'frame {i} body.'),
                    'meta': ''} for i in range(1, n + 1)}

    def _videos(self, n, bridge_at=None):
        return {i: {'body': f'video {i}', 'meta': 'BRIDGE' if i == bridge_at else ''}
                for i in range(1, n)}


class TestCheckpointRealitySync(_TmpProjectCase):
    """检查点两步：链中回望重锚定 + 锚点句校准（manifest 留痕 + 事件）。"""

    def test_real_drift_fail_triggers_reanchor(self):
        self._touch_frames(range(1, 6))
        images, videos = self._images(8), self._videos(8)
        config = {'_reanchors': []}
        with patch.object(pipeline_orchestrator, 'run_chain_tail_drift_check',
                          return_value=(False, 'FAIL: 地平线累积上漂')), \
             patch.object(pipeline_orchestrator, 'recalibrate_anchor_stanza', return_value=None):
            _checkpoint_reality_sync(config, self.TITLE, images, videos, list(range(1, 9)), 5,
                                     self.tmp, on_progress=self._on_progress)
        self.assertEqual(config['_reanchors'], [5])
        m = self._manifest()
        self.assertEqual(m['reanchors'][0]['new_anchor'], 5)
        stages = [s for s, _ in self.events]
        self.assertIn('chain_drift_check', stages)
        self.assertIn('reanchor', stages)
        drift_evt = dict(self.events[stages.index('chain_drift_check')][1])
        self.assertTrue(drift_evt.get('checkpoint'))

    def test_judge_unavailable_fail_does_not_reanchor(self):
        # strictGates fail-closed 的 FAIL 是服务异常不是漂移证据，不得据此重定基线
        self._touch_frames(range(1, 6))
        images, videos = self._images(8), self._videos(8)
        config = {'_reanchors': []}
        reason = 'FAIL: 视觉判定服务异常，严格模式(strictGates)拒绝放行: boom'
        self.assertTrue(is_judge_unavailable_verdict(reason))
        with patch.object(pipeline_orchestrator, 'run_chain_tail_drift_check',
                          return_value=(False, reason)), \
             patch.object(pipeline_orchestrator, 'recalibrate_anchor_stanza', return_value=None):
            _checkpoint_reality_sync(config, self.TITLE, images, videos, list(range(1, 9)), 5,
                                     self.tmp, on_progress=self._on_progress)
        self.assertEqual(config['_reanchors'], [])
        self.assertNotIn('reanchors', self._manifest())

    def test_recalibration_rewrites_remaining_prompts(self):
        self._touch_frames(range(1, 6))
        images, videos = self._images(8), self._videos(8)
        config = {'_reanchors': []}
        with patch.object(pipeline_orchestrator, 'run_chain_tail_drift_check',
                          return_value=(True, 'PASS')), \
             patch.object(pipeline_orchestrator, 'recalibrate_anchor_stanza',
                          return_value=NEW_STANZA):
            changed = _checkpoint_reality_sync(config, self.TITLE, images, videos,
                                               list(range(1, 9)), 5, self.tmp,
                                               on_progress=self._on_progress)
        self.assertTrue(changed)
        # 只有 latest 之后的剩余帧被改写
        for s in (6, 7, 8):
            self.assertIn(NEW_STANZA, images[s]['body'])
        for s in (1, 5):
            self.assertIn(STANZA, images[s]['body'])
        m = self._manifest()
        self.assertEqual(m['anchor_recalibrations'][0]['updated_slots'], 3)
        self.assertEqual(m['anchor_recalibrations'][0]['grounded_on'], 5)
        self.assertIn('anchor_recalibrated', [s for s, _ in self.events])

    def test_no_stanza_and_no_correction_are_noops(self):
        self._touch_frames(range(1, 6))
        config = {'_reanchors': []}
        videos = self._videos(8)
        with patch.object(pipeline_orchestrator, 'run_chain_tail_drift_check',
                          return_value=(True, 'PASS')):
            with patch.object(pipeline_orchestrator, 'recalibrate_anchor_stanza',
                              return_value=None):
                self.assertFalse(_checkpoint_reality_sync(
                    config, self.TITLE, self._images(8), videos, list(range(1, 9)), 5, self.tmp))
            def _boom(*a, **k):
                raise AssertionError('没有锚点句时不应发起校准')
            with patch.object(pipeline_orchestrator, 'recalibrate_anchor_stanza', _boom):
                self.assertFalse(_checkpoint_reality_sync(
                    config, self.TITLE, self._images(8, stanza=False), videos,
                    list(range(1, 9)), 5, self.tmp))


class TestRenderFramesWithCheckpoints(_TmpProjectCase):
    """分段渲染编排：段不跨族、已有帧不进目标、检查点位置、改写传播、退化路径。"""

    def _run(self, n_images, config=None, bridge_at=None, existing=(), sync_impl=None):
        images, videos = self._images(n_images), self._videos(n_images, bridge_at=bridge_at)
        self._touch_frames(existing)
        block = _format_prompt_block(images, videos)
        calls = []
        syncs = []

        def fake_generate(cfg, title, blk, on_progress=None, target_sequences=None):
            calls.append({'targets': list(target_sequences) if target_sequences else None,
                          'block': blk})
            for s in (target_sequences or []):
                self._touch_frames([s])
            if on_progress:
                on_progress('start', {'total': len(target_sequences or [])})
                for i, s in enumerate(target_sequences or [], 1):
                    on_progress('frame', {'current': i, 'total': len(target_sequences or []),
                                          'frame': {'sequence': s, 'slot': s}})

        def fake_sync(cfg, title, imgs, vids, members, latest, project_dir, on_progress=None):
            syncs.append(latest)
            if sync_impl:
                return sync_impl(imgs, latest)
            return False

        cfg = dict(config) if config is not None else {}
        with patch.object(pipeline_orchestrator, 'generate_frame_sequence', fake_generate), \
             patch.object(pipeline_orchestrator, '_checkpoint_reality_sync', fake_sync):
            out = _render_frames_with_checkpoints(cfg,
                                                  self.TITLE, block, self.tmp,
                                                  on_progress=self._on_progress)
        return calls, syncs, out, block

    def test_small_run_falls_back_to_single_call(self):
        calls, syncs, out, block = self._run(5)
        self.assertEqual(len(calls), 1)
        self.assertIsNone(calls[0]['targets'])
        self.assertEqual(syncs, [])
        self.assertEqual(out, block)

    def test_off_level_falls_back_to_single_call(self):
        calls, syncs, _, _ = self._run(12, config={'qaGateLevel': 'off'})
        self.assertEqual(len(calls), 1)
        self.assertIsNone(calls[0]['targets'])
        self.assertEqual(syncs, [])

    def test_segments_and_checkpoints_no_bridge(self):
        calls, syncs, _, _ = self._run(12)
        self.assertEqual([c['targets'] for c in calls],
                         [[1, 2, 3, 4, 5], [6, 7, 8, 9, 10], [11, 12]])
        # 族的最后一段交给收尾回望，检查点只在段 1、2 之后
        self.assertEqual(syncs, [5, 10])

    def test_segments_do_not_cross_bridge(self):
        # BRIDGE 在视频 6：族1 = IMG1-6，族2 = IMG7-12
        # IMG7 是族2 的**族锚帧**：走到族2 时它先单独渲一张并过族锚门
        # （_gate_family_anchor），之后同族其余帧才批量渲——族锚必须先于依赖它的帧成立，
        # 否则一次批量就把族身份和挂在它上面的 4 张帧一起下注（2026-08-02「客机变大巴」
        # 的形态）。它排在族1 渲完之后，因为它的 i2i 参考正是族1 的尾帧 IMG6。
        calls, syncs, _, _ = self._run(12, bridge_at=6)
        self.assertEqual([c['targets'] for c in calls],
                         [[1, 2, 3, 4, 5], [6], [7], [8, 9, 10, 11], [12]])
        self.assertEqual(syncs, [5, 11])

    def test_existing_frames_excluded_from_targets(self):
        calls, _, _, _ = self._run(12, existing=[1])
        self.assertEqual(calls[0]['targets'], [2, 3, 4, 5])

    def test_prompt_rewrite_propagates_to_later_segments(self):
        def sync_impl(imgs, latest):
            for s in [x for x in imgs if x > latest]:
                imgs[s] = {'body': imgs[s]['body'].replace(STANZA, NEW_STANZA),
                           'meta': imgs[s].get('meta', '')}
            return True
        calls, _, out, block = self._run(12, sync_impl=sync_impl)
        self.assertIn(NEW_STANZA, calls[1]['block'])   # 第二段收到改写后的 block
        self.assertNotIn(NEW_STANZA, calls[0]['block'])
        self.assertNotEqual(out, block)
        self.assertIn(NEW_STANZA, out)

    def test_progress_events_are_rectified(self):
        self._run(12)
        starts = [(s, p) for s, p in self.events if s == 'start']
        self.assertEqual(starts, [('start', {'total': 12})])   # 只放行一次，全局总数
        frame_currents = [p['current'] for s, p in self.events if s == 'frame']
        self.assertEqual(frame_currents, list(range(1, 13)))   # 跨段连续递增
        self.assertTrue(all(p['total'] == 12 for s, p in self.events if s == 'frame'))

    def test_family_anchor_gate_runs_on_the_head_of_each_later_family(self):
        """族锚门只判各族头帧，且用的是那一帧自己的提示词与镜头族。"""
        judged = []

        def _judge(config, image_path, image_prompt, family=None):
            judged.append((os.path.basename(image_path), family))
            return True, 'PASS'

        with patch.object(pipeline_orchestrator, 'check_family_anchor_compliance', _judge):
            self._run(12, bridge_at=6)
        self.assertEqual(judged, [('img_007.webp', 'interior')])

    def test_rejected_family_anchor_blocks_the_rest_of_the_chain(self):
        """族锚判定不过 = 整族都会长在错图上，必须中止，不做降级放行。"""
        with patch.object(pipeline_orchestrator, 'check_family_anchor_compliance',
                          lambda *a, **k: (False, 'FAIL: 画面是大巴内部，不是客机机身')), \
             patch.object(pipeline_orchestrator, 'fix_image_prompt_with_vlm_feedback',
                          side_effect=lambda c, p, r: p + '!'):
            with self.assertRaises(pipeline_orchestrator.AnchorRejected) as ctx:
                self._run(12, bridge_at=6)
        self.assertEqual(ctx.exception.sequence, 7)

    def test_family_anchor_gate_fails_open_when_the_judge_is_unavailable(self):
        """判定服务异常不是"族锚不合格"：照常往下渲，只留痕。"""
        with patch.object(pipeline_orchestrator, 'check_family_anchor_compliance',
                          lambda *a, **k: (False, 'FAIL: 判定服务异常')), \
             patch.object(pipeline_orchestrator, 'is_judge_unavailable_verdict',
                          lambda reason: True), \
             patch.object(pipeline_orchestrator, 'fix_image_prompt_with_vlm_feedback',
                          side_effect=lambda c, p, r: p):
            calls, _, _, _ = self._run(12, bridge_at=6)
        self.assertIn([8, 9, 10, 11], [c['targets'] for c in calls])


class TestSegmentProgress(unittest.TestCase):
    def test_cancel_check_passthrough_returns_value(self):
        cb = _segment_progress(lambda stage, details: True, 0, 10, False)
        self.assertTrue(cb('cancel_check', None))

    def test_none_on_progress_stays_none(self):
        self.assertIsNone(_segment_progress(None, 0, 10, True))


if __name__ == '__main__':
    unittest.main()


class TestManifestKeyPreservation(unittest.TestCase):
    """frame_generator 重建 manifest 时保留未知键：检查点在分段间隙写入的
    reanchors/anchor_recalibrations 不能被下一段渲染的逐帧落盘抹掉。"""

    def test_api_path_resume_keeps_checkpoint_records(self):
        import frame_generator
        tmp = tempfile.mkdtemp()
        try:
            frames_dir = os.path.join(tmp, 'frames')
            os.makedirs(frames_dir)
            for i in (1, 2):
                with open(os.path.join(frames_dir, f'img_{i:03d}.webp'), 'wb') as f:
                    f.write(b'x')
            with open(os.path.join(tmp, 'manifest.json'), 'w', encoding='utf-8') as f:
                json.dump({'title': 'proj', 'frames': [],
                           'reanchors': [{'family_anchor': 1, 'new_anchor': 2}],
                           'anchor_recalibrations': [{'grounded_on': 2}]}, f)
            block = _format_prompt_block({1: 'frame one.', 2: 'frame two.'}, {1: 'video one.'})
            with patch.object(frame_generator, '_get_project_dir', lambda t: tmp):
                # 两帧都已在盘上 + target_sequences=None：走 skip_api_call 断点续传路径，
                # 不发任何外部请求
                frame_generator.generate_frame_sequence({}, 'proj', block)
            with open(os.path.join(tmp, 'manifest.json'), 'r', encoding='utf-8') as f:
                m = json.load(f)
            self.assertEqual(m['reanchors'][0]['new_anchor'], 2)
            self.assertEqual(m['anchor_recalibrations'][0]['grounded_on'], 2)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
