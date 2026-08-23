"""滚动批量窗口回归测试（2026-08-22）。

compose_remaining_beats 的批量通路本来就是按 composeBatchSize 分窗设计的（进度事件
早就在按 ceil(待生成拍数 / batch_size) 报 batch_total），但实现只把**第一个**窗口真的
批量发了出去，窗口之外的拍全部退回逐拍单发 —— 一单 21 拍就是 1 次批量 + 18 次单发。
这条回归在产物上完全看不出来（每拍内容都正常），只表现为合成慢得多，所以必须由测试
钉住"调用次数"这个观测量本身。

同时钉住两条不变量：
1. 每个窗口的 STARTING POINT 锚点取自**上一窗最后一拍已提交**的 IMAGE，而不是整单
   开头那张 —— 跨窗连续性靠这个，退化了就是各窗自说自话。
2. 过门/切入协议（TBCP）按整单是否存在过门拍加载，不再只看头几拍。过门拍落在第 5、6、
   12 拍是常态，旧写法下 tbcp_ref 恒为空串，那一拍的 system prompt 里没有协议正文。
"""
import os
import re
import shutil
import tempfile
import unittest
from unittest.mock import patch

import prompt_pipeline as pp


def _batch_beats_in(user_text):
    return [int(n) for n in re.findall(r'=+\s*BEAT\s+(\d+)\s*=+', user_text)]


def _starting_anchor_in(user_text):
    m = re.search(r'just continue forward from it\):\n(.+)', user_text)
    return m.group(1).strip() if m else ''


class TestRollingBatchWindows(unittest.TestCase):
    def setUp(self):
        self._tmp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self._tmp_dir, True)
        patches = [
            patch.object(pp, 'COMPOSE_CHECKPOINT_PATH', os.path.join(self._tmp_dir, 'ck.json')),
            patch.object(pp, 'get_cropped_templates', return_value=''),
            patch.object(pp, 'apply_proactive_fixes', side_effect=lambda i, v, im, *a, **k: (v, im)),
            patch.object(pp, 'validate_beat_prompts', return_value=[]),
            patch.object(pp, 'check_milestone_video_prompt', return_value=[]),
            patch.object(pp, 'check_milestone_image_prompt', return_value=[]),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _state(self, total_beats, bridge_at=None):
        ladder = []
        for i in range(1, total_beats + 1):
            beat = {'index': i, 'operation': 'repair', 'description': f'step {i}',
                    'bridge_stage': None}
            if bridge_at == i:
                beat['operation'] = 'threshold'
                beat['bridge_stage'] = 1
            ladder.append(beat)
        return {
            'theme': 'test theme',
            'total_beats': total_beats,
            'parsed_brief': {'mode': 'Threshold' if bridge_at else 'Standard', 'theme': 'test theme'},
            'title': 'Test Title',
            'beat_ladder': ladder,
            'packet': {'lighting_phase_ladder': {str(i): 'ambient only' for i in range(1, total_beats + 2)},
                       'object_ledger': []},
            'brief_fingerprint': f'fp-windows-{total_beats}-{bridge_at}',
            'image_1_prompt': 'IMAGE 1 body',
            'compiled_images': {1: 'IMAGE 1 body'},
            'compiled_videos': {},
        }

    @staticmethod
    def _recording_chat(log):
        def fake_chat(config, system, user, temperature=0.85, max_tokens=16384, timeout=240,
                      on_chunk=None, model=None, enable_search=False):
            beats = _batch_beats_in(user)
            if beats:
                log.append({'kind': 'batch', 'beats': beats, 'system': system,
                            'anchor': _starting_anchor_in(user)})
                return "\n".join(
                    f"===BEAT {b} VIDEO===\nVideo prompt for beat {b}\n"
                    f"===BEAT {b} IMAGE===\nImage prompt for beat {b + 1}\n"
                    f"===BEAT {b} TRACES===\n[]"
                    for b in beats)
            i = int(user.split('Generate prompts for Beat ')[1].split(':', 1)[0])
            log.append({'kind': 'single', 'beats': [i], 'system': system, 'anchor': ''})
            return (f"===VIDEO===\nVideo prompt for beat {i}\n"
                    f"===IMAGE===\nImage prompt for beat {i + 1}\n===TRACES===\n[]")
        return fake_chat

    def test_every_beat_goes_through_a_batch_window(self):
        """9 拍 / batch_size=3 = 3 次批量调用，零次逐拍单发。

        修复前：1 次批量（1-3 拍）+ 6 次单发。窗口大小显式钉住，测的是分窗机制本身，
        不是当下的默认值（默认值另有 test_default_batch_size 单独钉）。"""
        log = []
        with patch.object(pp, 'load_reference_file', return_value=''), \
                patch.object(pp, '_chat', side_effect=self._recording_chat(log)):
            out = pp.compose_remaining_beats({'composeBatchSize': 3}, self._state(9))

        self.assertEqual([c['beats'] for c in log], [[1, 2, 3], [4, 5, 6], [7, 8, 9]])
        self.assertEqual([c['kind'] for c in log], ['batch'] * 3)
        self.assertIn('Image prompt for beat 10', out)

    def test_window_starting_anchor_is_previous_window_last_image(self):
        """第 k+1 窗的起点锚点 = 第 k 窗最后一拍刚落盘的 IMAGE，不是整单首帧。"""
        log = []
        with patch.object(pp, 'load_reference_file', return_value=''), \
                patch.object(pp, '_chat', side_effect=self._recording_chat(log)):
            pp.compose_remaining_beats({'composeBatchSize': 3}, self._state(9))

        self.assertEqual([c['anchor'] for c in log],
                         ['IMAGE 1 body', 'Image prompt for beat 4', 'Image prompt for beat 7'])

    def test_default_batch_size_is_five(self):
        """默认窗口大小 5：9 拍 = [1-5] + [6-9] 两窗。

        默认值直接决定整单的模型调用次数，是这次提速的一个可调旋钮；调它要连着
        server_config.example.json 与 js/state.js 一起调，这里钉住代码侧的那一份。"""
        log = []
        with patch.object(pp, 'load_reference_file', return_value=''), \
                patch.object(pp, '_chat', side_effect=self._recording_chat(log)):
            pp.compose_remaining_beats({}, self._state(9))

        self.assertEqual([c['beats'] for c in log], [[1, 2, 3, 4, 5], [6, 7, 8, 9]])

    def test_batch_size_one_still_uses_the_batch_path(self):
        """composeBatchSize=1 是每窗一拍，仍走批量通路（而不是掉回单发通路）。"""
        log = []
        with patch.object(pp, 'load_reference_file', return_value=''), \
                patch.object(pp, '_chat', side_effect=self._recording_chat(log)):
            pp.compose_remaining_beats({'composeBatchSize': 1}, self._state(4))

        self.assertEqual([c['beats'] for c in log], [[1], [2], [3], [4]])
        self.assertEqual({c['kind'] for c in log}, {'batch'})

    def test_tbcp_reference_loads_for_a_crossing_outside_the_first_window(self):
        """过门拍在第 6 拍（第 2 窗）时，TBCP 协议正文仍必须进 system prompt。

        修复前 tbcp_ref 只看头 3 拍，过门拍一旦不在第一窗就永远拿不到协议正文。"""
        log = []
        marker = 'TBCP-PROTOCOL-BODY'

        def fake_ref(name, profile=None):
            return marker if 'threshold-bridge' in name else ''

        with patch.object(pp, 'load_reference_file', side_effect=fake_ref), \
                patch.object(pp, '_chat', side_effect=self._recording_chat(log)):
            pp.compose_remaining_beats({'composeBatchSize': 3}, self._state(9, bridge_at=6))

        window_with_bridge = [c for c in log if 6 in c['beats']]
        self.assertTrue(window_with_bridge, '第 6 拍应当由某个窗口生成')
        self.assertIn(marker, window_with_bridge[0]['system'])


if __name__ == '__main__':
    unittest.main()
