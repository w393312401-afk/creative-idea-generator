"""链尾回望检查测试。

逐帧质检只比相邻对/族锚对单帧，缓慢累积漂移可以帧帧合格、链尾却已偏离。
新增链路：帧渲染（含恢复轮）全部结束后，pipeline_orchestrator._chain_drift_lookback
按镜头族各取 锚点/链中/链尾 三帧，一次 VLM 调用（run_chain_tail_drift_check）比对，
结果写 manifest['chain_drift'] + 广播 chain_drift_check 事件；检测型门，永不拦截。
"""
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

import prompt_pipeline
import pipeline_orchestrator
from prompt_pipeline import run_chain_tail_drift_check, is_skipped_verdict, _format_prompt_block
from pipeline_orchestrator import _chain_drift_lookback


class TestRunChainTailDriftCheck(unittest.TestCase):
    """判定层：三态解析、off 短路、lenient 照跑（与 check_landmark_drift 的 lenient
    停用不同——这道门只留痕不重渲，没有误杀成本）、判定异常 fail-open。"""

    ARGS = ('a.png', 'm.png', 't.png')

    def test_pass_and_fail_parse(self):
        with patch.object(prompt_pipeline, '_multimodal_chat', return_value='PASS'):
            self.assertEqual(run_chain_tail_drift_check({}, *self.ARGS), (True, 'PASS'))
        with patch.object(prompt_pipeline, '_multimodal_chat', return_value='FAIL: 地平线累积上漂'):
            passed, reason = run_chain_tail_drift_check({}, *self.ARGS)
        self.assertFalse(passed)
        self.assertIn('地平线', reason)

    def test_off_level_skips_without_vlm_call(self):
        def _boom(*a, **k):
            raise AssertionError('off 档不得发 VLM 请求')
        with patch.object(prompt_pipeline, '_multimodal_chat', _boom):
            passed, reason = run_chain_tail_drift_check({'qaGateLevel': 'off'}, *self.ARGS)
        self.assertTrue(passed)
        self.assertTrue(is_skipped_verdict(reason))

    def test_lenient_level_still_runs(self):
        calls = []

        def _fake(config, system, user, paths, model=None):
            calls.append(paths)
            return 'PASS'
        with patch.object(prompt_pipeline, '_multimodal_chat', _fake):
            passed, _ = run_chain_tail_drift_check({'qaGateLevel': 'lenient'}, *self.ARGS)
        self.assertTrue(passed)
        self.assertEqual(calls, [['a.png', 'm.png', 't.png']])

    def test_judge_error_fails_open(self):
        with patch.object(prompt_pipeline, '_multimodal_chat', side_effect=RuntimeError('boom')):
            passed, reason = run_chain_tail_drift_check({}, *self.ARGS)
        self.assertTrue(passed)
        self.assertTrue(is_skipped_verdict(reason))


class TestChainDriftLookback(unittest.TestCase):
    """编排层：按镜头族分链取样（BRIDGE 处断族）、manifest 落盘、事件广播、
    短链跳过、off 档跳过、异常不拦截流水线。"""

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

    def _touch_frames(self, n):
        for i in range(1, n + 1):
            with open(os.path.join(self.frames_dir, f'img_{i:03d}.webp'), 'wb') as f:
                f.write(b'x')

    def _block(self, n_images, bridge_at=None):
        images = {i: f'image {i} body' for i in range(1, n_images + 1)}
        videos = {}
        for i in range(1, n_images):
            meta = 'BRIDGE' if i == bridge_at else ''
            videos[i] = {'body': f'video {i} body', 'meta': meta}
        return _format_prompt_block(images, videos)

    def _on_progress(self, stage, payload):
        self.events.append((stage, payload))

    def _manifest(self):
        with open(os.path.join(self.tmp, 'manifest.json'), 'r', encoding='utf-8') as f:
            return json.load(f)

    def test_families_split_at_bridge_and_sampling(self):
        self._touch_frames(8)
        calls = []

        def _fake(config, a, m, t, anchor_seq=1, mid_seq=None, tail_seq=None,
                  anchor_is_first_frame=True):
            calls.append((anchor_seq, mid_seq, tail_seq, anchor_is_first_frame))
            return (True, 'PASS') if anchor_seq == 1 else (False, 'FAIL: 室内族累积漂移')
        # 漂移 FAIL 现在会先重生成尾帧再复检（chainDriftRegen=False 关掉，本例只验取样）
        with patch.object(pipeline_orchestrator, 'run_chain_tail_drift_check', _fake):
            _chain_drift_lookback({'chainDriftRegen': False}, self.TITLE,
                                  self._block(8, bridge_at=4), self.tmp,
                                  on_progress=self._on_progress)
        # 族1 = IMG1-4（BRIDGE 前），族2 = IMG5-8；各取 锚点/链中/链尾
        self.assertEqual(calls, [(1, 3, 4, True), (5, 7, 8, False)])
        entries = self._manifest()['chain_drift']
        self.assertEqual([e['passed'] for e in entries], [True, False])
        self.assertEqual([e['tail'] for e in entries], [4, 8])
        stages = [s for s, _ in self.events]
        self.assertEqual(stages, ['chain_drift_check', 'chain_drift_check', 'chain_drift_blocking'])
        self.assertIn('链尾回望', self.events[0][1]['message'])
        self.assertIn('检出累积漂移', self.events[1][1]['message'])
        # 未修好的漂移家族必须阻塞视频生成，而不是只留一条记录
        blocking = self._manifest()['chain_drift_blocking']
        self.assertEqual([b['family_anchor'] for b in blocking], [5])

    def test_drift_fail_regenerates_tail_then_clears(self):
        """FAIL → 重生成尾帧及其下游 → 复检通过 → 不再阻塞。"""
        self._touch_frames(6)
        verdicts = iter([(False, 'FAIL: 累积漂移'), (True, 'PASS')])
        rendered = []

        def _fake_render(config, title, block, on_progress=None, target_sequences=None):
            rendered.append(list(target_sequences or []))
            return {'title': title}

        with patch.object(pipeline_orchestrator, 'run_chain_tail_drift_check',
                          lambda *a, **k: next(verdicts)), \
             patch.object(pipeline_orchestrator, 'generate_frame_sequence', _fake_render):
            _chain_drift_lookback({}, self.TITLE, self._block(6), self.tmp,
                                  on_progress=self._on_progress)

        self.assertEqual(rendered, [[6]])
        entry = self._manifest()['chain_drift'][0]
        self.assertTrue(entry['passed'])
        self.assertEqual(entry['regen_rounds'], 1)
        self.assertEqual(entry['regenerated'], [6])
        self.assertNotIn('chain_drift_blocking', self._manifest())

    def test_judge_unavailable_fail_never_regenerates_or_blocks(self):
        """判定服务异常导致的 fail-closed FAIL 不是漂移：既不重渲也不阻塞。"""
        self._touch_frames(6)
        with patch.object(pipeline_orchestrator, 'run_chain_tail_drift_check',
                          lambda *a, **k: (False, 'FAIL: 判定服务异常')), \
             patch.object(pipeline_orchestrator, 'is_judge_unavailable_verdict',
                          lambda reason: True), \
             patch.object(pipeline_orchestrator, 'generate_frame_sequence') as mock_render:
            _chain_drift_lookback({}, self.TITLE, self._block(6), self.tmp)

        mock_render.assert_not_called()
        self.assertNotIn('chain_drift_blocking', self._manifest())

    def test_single_family_without_bridge(self):
        self._touch_frames(6)
        calls = []
        with patch.object(pipeline_orchestrator, 'run_chain_tail_drift_check',
                          lambda *a, **k: (calls.append(k), (True, 'PASS'))[1]):
            _chain_drift_lookback({}, self.TITLE, self._block(6), self.tmp)
        self.assertEqual(len(calls), 1)
        self.assertEqual((calls[0]['anchor_seq'], calls[0]['mid_seq'], calls[0]['tail_seq']),
                         (1, 4, 6))
        self.assertTrue(calls[0]['anchor_is_first_frame'])

    def test_short_chain_is_skipped(self):
        self._touch_frames(3)

        def _boom(*a, **k):
            raise AssertionError('少于 4 帧的族不应触发链尾回望')
        with patch.object(pipeline_orchestrator, 'run_chain_tail_drift_check', _boom):
            _chain_drift_lookback({}, self.TITLE, self._block(3), self.tmp)
        self.assertNotIn('chain_drift', self._manifest())

    def test_missing_frame_files_are_excluded(self):
        # 8 帧的链只有前 5 帧在盘上：取样只在存在的帧里做
        self._touch_frames(5)
        calls = []
        with patch.object(pipeline_orchestrator, 'run_chain_tail_drift_check',
                          lambda *a, **k: (calls.append(k), (True, 'PASS'))[1]):
            _chain_drift_lookback({}, self.TITLE, self._block(8), self.tmp)
        self.assertEqual(calls[0]['tail_seq'], 5)

    def test_off_level_skips_entirely(self):
        self._touch_frames(8)

        def _boom(*a, **k):
            raise AssertionError('off 档不应触发链尾回望')
        with patch.object(pipeline_orchestrator, 'run_chain_tail_drift_check', _boom):
            _chain_drift_lookback({'qaGateLevel': 'off'}, self.TITLE, self._block(8), self.tmp)
        self.assertNotIn('chain_drift', self._manifest())

    def test_unexpected_exception_never_blocks_pipeline(self):
        self._touch_frames(8)
        with patch.object(pipeline_orchestrator, 'run_chain_tail_drift_check',
                          side_effect=RuntimeError('boom')):
            _chain_drift_lookback({}, self.TITLE, self._block(8), self.tmp)  # 不得抛出
        self.assertNotIn('chain_drift', self._manifest())


if __name__ == '__main__':
    unittest.main()
