"""「机器判过、人判废」的样本必须回灌进逐拍审查的 rubric。

由来：用户的判废标准比这套 rubric 严，而质检档位已经是默认的 standard（全量严检）。
档位拉满还漏，说明漏掉的是**维度**而不是严格度——再调严一档也看不见它本来就没在查的
东西。缺口的定义因此很明确：机器判了 sequence_reviewed_pass、人随后标了 manual_issue
的那些帧。

数据一直在盘上（set_manual_frame_issue 把人的描述写进 manual_issue，把被覆盖的机器判定
存进 manual_flag_prev_gate，其 docstring 明说这是为了"事后对照谁看漏了什么"），只是从来
没有任何代码回读过它。这个文件守的是那条回读链路。
"""
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

import prompt_pipeline as pp
import server_common


class _BlindSpotCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        pp._BLIND_SPOT_CACHE.update({'at': 0.0, 'block': ''})

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        pp._BLIND_SPOT_CACHE.update({'at': 0.0, 'block': ''})

    def _project(self, name, frames, title=None):
        project_dir = os.path.join(self.tmp, name)
        os.makedirs(project_dir, exist_ok=True)
        server_common.write_manifest(project_dir, {'title': title or name, 'frames': frames})
        return project_dir

    def _collect(self, **kw):
        return server_common.collect_operator_blind_spots(output_root=self.tmp, **kw)


class TestCollector(_BlindSpotCase):
    def test_collects_only_defects_the_machine_had_passed(self):
        self._project('p1', [
            # 盲区：机器放行，人判废
            {'sequence': 1, 'quality_gate': 'manual_flagged',
             'manual_flag_prev_gate': 'sequence_reviewed_pass',
             'manual_issue': '木纹是贴图感，没有真实材质厚度'},
            # 不是盲区：机器已经报过问题，人只是补充
            {'sequence': 2, 'quality_gate': 'manual_flagged',
             'manual_flag_prev_gate': 'sequence_review_flagged',
             'manual_issue': '同一个问题人也看到了'},
            # 不是盲区：机器从没判过
            {'sequence': 3, 'quality_gate': 'manual_flagged',
             'manual_flag_prev_gate': 'pending_manual_review',
             'manual_issue': '这帧压根没被审过'},
            # 没有人工描述
            {'sequence': 4, 'quality_gate': 'sequence_reviewed_pass'},
        ])
        texts = [s['text'] for s in self._collect()]
        self.assertEqual(texts, ['木纹是贴图感，没有真实材质厚度'])

    def test_repeated_reports_are_deduped_and_ranked_by_count(self):
        """同一类毛病被标很多次，出现次数本身就是权重。"""
        self._project('p1', [
            {'sequence': s, 'quality_gate': 'manual_flagged',
             'manual_flag_prev_gate': 'sequence_reviewed_pass',
             'manual_issue': '塑料感'} for s in (1, 2, 3)
        ] + [
            {'sequence': 4, 'quality_gate': 'manual_flagged',
             'manual_flag_prev_gate': 'sequence_reviewed_pass',
             'manual_issue': '构图垮了'},
        ])
        spots = self._collect()
        self.assertEqual([s['text'] for s in spots], ['塑料感', '构图垮了'])
        self.assertEqual(spots[0]['count'], 3)
        self.assertEqual(spots[1]['count'], 1)

    def test_spans_multiple_projects(self):
        self._project('p1', [{'sequence': 1, 'quality_gate': 'manual_flagged',
                              'manual_flag_prev_gate': 'sequence_reviewed_pass',
                              'manual_issue': 'A 问题'}])
        self._project('p2', [{'sequence': 1, 'quality_gate': 'manual_flagged',
                              'manual_flag_prev_gate': 'sequence_reviewed_pass',
                              'manual_issue': 'B 问题'}])
        self.assertEqual({s['text'] for s in self._collect()}, {'A 问题', 'B 问题'})

    def test_limit_and_truncation_are_respected(self):
        self._project('p1', [
            {'sequence': s, 'quality_gate': 'manual_flagged',
             'manual_flag_prev_gate': 'sequence_reviewed_pass',
             'manual_issue': f'问题{s}' + '啊' * 500} for s in range(1, 20)
        ])
        spots = self._collect(limit=3, max_chars=40)
        self.assertEqual(len(spots), 3)
        self.assertTrue(all(len(s['text']) <= 40 for s in spots))

    def test_missing_root_returns_empty_not_raise(self):
        """采集是增强信号不是门禁：目录异常绝不能冒泡。"""
        self.assertEqual(
            server_common.collect_operator_blind_spots(output_root=os.path.join(self.tmp, 'nope')),
            [])

    def test_corrupt_manifest_is_skipped_not_fatal(self):
        good = self._project('p1', [{'sequence': 1, 'quality_gate': 'manual_flagged',
                                     'manual_flag_prev_gate': 'sequence_reviewed_pass',
                                     'manual_issue': '好的那条'}])
        bad_dir = os.path.join(self.tmp, 'p2')
        os.makedirs(bad_dir)
        with open(os.path.join(bad_dir, 'manifest.json'), 'w', encoding='utf-8') as f:
            f.write('{ this is not json')
        self.assertEqual([s['text'] for s in self._collect()], ['好的那条'])
        self.assertTrue(os.path.isdir(good))


class TestPromptBlock(_BlindSpotCase):
    def test_empty_ledger_yields_empty_block(self):
        self.assertEqual(server_common.operator_blind_spot_block(output_root=self.tmp), '')

    def test_block_carries_the_operator_wording_and_repeat_weight(self):
        self._project('p1', [
            {'sequence': s, 'quality_gate': 'manual_flagged',
             'manual_flag_prev_gate': 'sequence_reviewed_pass',
             'manual_issue': '墙面反光像塑料'} for s in (1, 2)
        ])
        block = server_common.operator_blind_spot_block(output_root=self.tmp)
        self.assertIn('Operator-Reported Blind Spots', block)
        self.assertIn('墙面反光像塑料', block)
        self.assertIn('reported 2 times', block)
        # 必须保留"同样的置信门槛"这层约束，否则回灌会变成硬塞假问题的来源
        self.assertIn('concretely visible', block)


class TestInjectionIntoReview(_BlindSpotCase):
    def test_block_is_appended_so_the_cached_prefix_is_untouched(self):
        """系统提示词是常量以求缓存命中；盲区块只能追加在尾部。"""
        base = pp._local_beat_review_system_prompt()
        with patch.object(pp, 'operator_blind_spot_block', return_value='\n\nBLIND SPOT BLOCK'):
            block = pp._cached_blind_spot_block(force=True)
        combined = base + block
        self.assertTrue(combined.startswith(base))
        self.assertTrue(combined.endswith('BLIND SPOT BLOCK'))

    def test_cache_avoids_rescanning_once_per_beat(self):
        with patch.object(pp, 'operator_blind_spot_block', return_value='X') as m:
            pp._cached_blind_spot_block(force=True)
            for _ in range(10):
                pp._cached_blind_spot_block()
        self.assertEqual(m.call_count, 1)

    def test_collector_failure_degrades_to_empty_block(self):
        with patch.object(pp, 'operator_blind_spot_block', side_effect=OSError('boom')):
            self.assertEqual(pp._cached_blind_spot_block(force=True), '')


if __name__ == '__main__':
    unittest.main()
