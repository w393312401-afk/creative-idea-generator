"""回读残留（reverify_beat_repairs）只能留痕放行，不能当硬门。

2026-08-08 实测事故：一单 10 拍的合成在 Beat 8 上报
「合成任务超过硬时限。」（COMPOSE_TIMEOUT），日志里每一拍都出现
`Step 5: Individually composing Beat N`，末尾还有一句原因为空的
`[DIRECT] Beat 8 硬门仍未通过，重试整拍: []`。

根因：批量通路和单拍通路的 hard_gate_errors 里都掺进了整份 residual
（`remaining_milestone_errors + payoff_blocking + list(residual or [])`）。
于是任何一条本该「仅记录」的回读残留都会——

1. 让整批批量稿作废，10/10 拍全部退回昂贵的单拍通路；
2. 在单拍通路里再触发整拍重试，每次白烧一整轮 LLM 调用；
3. 打印时只展示 milestone+payoff 两类，所以出现「重试整拍: []」这种空原因。

硬门只有两类：里程碑骨架残缺（check_milestone_*）和终帧倒退
（payoff_blocking_residual）。其余残留由 record_beat_audit 记成
rework_failed / needs_attention，在审核面板上可见即可。
"""
import os
import re
import shutil
import tempfile
import unittest
from unittest.mock import patch

import prompt_pipeline as pp


_NON_BLOCKING_RESIDUAL = [
    "This beat is stage_scope=\"large\" but its IMAGE prompt does not claim full/entire coverage",
]


class TestResidualDoesNotForceRegeneration(unittest.TestCase):

    def setUp(self):
        self._tmp_dir = tempfile.mkdtemp()
        self._path_patch = patch.object(
            pp, 'COMPOSE_CHECKPOINT_PATH', os.path.join(self._tmp_dir, 'compose_checkpoints.json'))
        self._path_patch.start()
        self.addCleanup(self._path_patch.stop)
        self.addCleanup(shutil.rmtree, self._tmp_dir, True)
        self.fingerprint = 'fp-residual-not-a-hard-gate'

        # 只保留「每拍一次 _chat」这条真实路径，其余重活换成便宜桩实现。
        for p in [
            patch.object(pp, 'load_reference_file', return_value=''),
            patch.object(pp, 'get_cropped_templates', return_value=''),
            patch.object(pp, 'apply_proactive_fixes', side_effect=lambda i, v, im, *a, **k: (v, im)),
            patch.object(pp, 'validate_beat_prompts', return_value=[]),
            patch.object(pp, 'check_milestone_video_prompt', return_value=[]),
            patch.object(pp, 'check_milestone_image_prompt', return_value=[]),
        ]:
            p.start()
            self.addCleanup(p.stop)

    def _make_state(self, total_beats=3):
        return {
            'theme': 'test theme',
            'total_beats': total_beats,
            'parsed_brief': {'mode': 'Standard', 'theme': 'test theme'},
            'title': 'Test Title',
            'beat_ladder': [{'index': i, 'operation': 'repair', 'description': f'step {i}',
                             'bridge_stage': None}
                            for i in range(1, total_beats + 1)],
            'packet': {'lighting_phase_ladder': {str(i): 'ambient only'
                                                 for i in range(1, total_beats + 2)},
                       'object_ledger': []},
            'brief_fingerprint': self.fingerprint,
            'image_1_prompt': 'IMAGE 1 body',
            'compiled_images': {1: 'IMAGE 1 body'},
            'compiled_videos': {},
        }

    @staticmethod
    def _fake_chat_factory(calls):
        """同 test_compose_checkpoint_resume：批量与单拍两种请求形态都作答，
        `calls` 收集每一次真实生成过的拍号（批量通路一次记多拍）。"""
        def fake_chat(config, system, user, temperature=0.85, max_tokens=16384,
                      timeout=240, on_chunk=None, model=None):
            batch_beats = [int(n) for n in re.findall(
                r'====================\s*BEAT\s+(\d+)\s*====================', user)]
            if batch_beats:
                calls.extend(batch_beats)
                return "\n".join(
                    f"===BEAT {b} VIDEO===\nVideo prompt for beat {b}\n"
                    f"===BEAT {b} IMAGE===\nImage prompt for beat {b + 1}\n"
                    f"===BEAT {b} TRACES===\n[]"
                    for b in batch_beats)
            marker = 'Generate prompts for Beat '
            beat_i = int(user[user.index(marker) + len(marker):].split(':', 1)[0])
            calls.append(beat_i)
            return (f"===VIDEO===\nVideo prompt for beat {beat_i}\n"
                    f"===IMAGE===\nImage prompt for beat {beat_i + 1}\n===TRACES===\n[]")
        return fake_chat

    def test_non_blocking_residual_does_not_discard_the_batch_result(self):
        """每拍回读都有残留时，批量稿仍应被采纳：每拍恰好生成一次，
        不退回单拍通路，更不整拍重试。"""
        state = self._make_state(total_beats=3)
        calls = []
        with patch.object(pp, 'reverify_beat_repairs', return_value=list(_NON_BLOCKING_RESIDUAL)), \
             patch.object(pp, '_chat', side_effect=self._fake_chat_factory(calls)):
            output = pp.compose_remaining_beats({}, state)

        self.assertEqual(calls, [1, 2, 3],
                         "回读残留是留痕项，不能让每一拍都重新生成一遍")
        self.assertIn('Image prompt for beat 4', output)

    def test_residual_is_still_recorded_in_the_audit(self):
        """放行不等于隐瞒：残留必须进 _beat_audit，审核面板上仍看得见。"""
        state = self._make_state(total_beats=2)
        config = {}
        with patch.object(pp, 'reverify_beat_repairs', return_value=list(_NON_BLOCKING_RESIDUAL)), \
             patch.object(pp, '_chat', side_effect=self._fake_chat_factory([])):
            pp.compose_remaining_beats(config, state)

        audit = config.get('_beat_audit') or []
        self.assertTrue(audit, "留痕放行的拍必须留下审计记录")
        self.assertTrue(any(rec.get('residual') for rec in audit),
                        f"残留原文必须原样记进审计条目: {audit}")

    def test_payoff_regression_on_the_last_beat_still_blocks(self):
        """终帧倒退仍是硬门：命中 payoff 标记的残留必须触发重生成。"""
        residual = ['Envelope regression: roof/ceiling was already sealed']
        state = self._make_state(total_beats=3)
        calls = []
        with patch.object(pp, 'reverify_beat_repairs', return_value=list(residual)), \
             patch.object(pp, '_chat', side_effect=self._fake_chat_factory(calls)):
            # 桩残留永远不消失，重试用尽后按生产模式如实失败（不接受占位符交付）
            with self.assertRaises(pp.ComposeFailure):
                pp.compose_remaining_beats({}, state)

        self.assertGreater(calls.count(3), 1,
                           "终帧（第 3 拍）命中 payoff 倒退必须重生成，不能留痕放行")
        self.assertEqual(calls.count(1), 1, "非终帧不受 payoff 硬门影响")


if __name__ == '__main__':
    unittest.main()
