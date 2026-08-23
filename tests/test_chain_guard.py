"""测试 chain_guard.py 链上守卫核心逻辑与停链规则：

1. 干净拍不写 flag，记录 pass
2. check_beat_consistency 返回 None 时记 unreviewed，不阻断
3. 复核否决（False）的问题丢弃，不阻断
4. 分级器失败时 fail-safe 兜底为 chain
5. 检出 chain 违规在 halt 模式下停链并写 sequence_review_flagged + flag_origin='chain_guard'，且不写 review_frames_sha256
6. report 模式下记账不停链
"""
import json
import os
import sys
import shutil
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image

import server_common
import prompt_pipeline as pp
import chain_guard as cg
import candidate_selection_pipeline as csp


class TestChainGuard(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.project_dir = os.path.join(self.tmp_dir, 'outputs', 'test_proj')
        self.frames_dir = os.path.join(self.project_dir, 'frames')
        os.makedirs(self.frames_dir, exist_ok=True)
        self.orig_root = server_common.OUTPUT_ROOT
        server_common.OUTPUT_ROOT = os.path.join(self.tmp_dir, 'outputs')

        # 创建两张测试图片
        img = Image.new('RGB', (64, 64), color='red')
        img.save(os.path.join(self.frames_dir, 'img_001.webp'), 'WEBP')
        img2 = Image.new('RGB', (64, 64), color='blue')
        img2.save(os.path.join(self.frames_dir, 'img_002.webp'), 'WEBP')

        # 基础 manifest
        self.manifest = {
            'title': 'test_proj',
            'frames': [
                {'sequence': 1, 'file': 'frames/img_001.webp', 'quality_gate': 'auto_approved'},
                {'sequence': 2, 'file': 'frames/img_002.webp', 'quality_gate': 'auto_approved'},
            ]
        }
        with open(os.path.join(self.project_dir, 'manifest.json'), 'w', encoding='utf-8') as f:
            json.dump(self.manifest, f)

        self.prompt_block = (
            "IMAGE 1: Initial empty site\n"
            "VIDEO 1: Digging earth\n"
            "IMAGE 2: Excavated trench\n"
        )

    def tearDown(self):
        server_common.OUTPUT_ROOT = self.orig_root
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_clean_beat_records_pass_and_does_not_halt(self):
        with patch.object(cg, 'check_beat_consistency', return_value=[]):
            res = cg.guard_beat({}, 'test_proj', self.prompt_block, 1, self.project_dir)
            self.assertEqual(res['verdict'], 'pass')
            self.assertEqual(res['issues'], [])
            self.assertFalse(res['halt'])

            manifest = server_common.read_manifest(self.project_dir)
            frame2 = manifest['frames'][1]
            self.assertIn('inline_beat_review', frame2)
            self.assertEqual(frame2['inline_beat_review']['verdict'], 'pass')
            self.assertEqual(frame2['quality_gate'], 'auto_approved')
            self.assertNotIn('review_frames_sha256', frame2)

    def test_none_verdict_treated_as_unreviewed_and_does_not_halt(self):
        with patch.object(cg, 'check_beat_consistency', return_value=None):
            res = cg.guard_beat({}, 'test_proj', self.prompt_block, 1, self.project_dir)
            self.assertEqual(res['verdict'], 'unreviewed')
            self.assertFalse(res['halt'])

            manifest = server_common.read_manifest(self.project_dir)
            frame2 = manifest['frames'][1]
            self.assertEqual(frame2['inline_beat_review']['verdict'], 'unreviewed')
            self.assertEqual(frame2['quality_gate'], 'auto_approved')

    def test_rejected_violations_by_verifier_pass_and_do_not_halt(self):
        with patch.object(cg, 'check_beat_consistency', return_value=['门框材质突变']), \
             patch.object(cg, '_verify_review_violation', return_value=False):
            res = cg.guard_beat({}, 'test_proj', self.prompt_block, 1, self.project_dir)
            self.assertEqual(res['verdict'], 'pass')
            self.assertEqual(res['issues'], [])
            self.assertFalse(res['halt'])

    def test_classifier_failure_falls_back_to_chain_severity(self):
        with patch.object(cg, '_multimodal_chat', side_effect=Exception('LLM timeout')):
            severities = cg.classify_chain_impact({}, ['问题一', '问题二'])
            self.assertEqual(severities, ['chain', 'chain'])

    def test_chain_issue_triggers_halt_and_sets_flag_without_review_frames_sha256(self):
        with patch.object(cg, 'check_beat_consistency', return_value=['天花板结构坍塌']), \
             patch.object(cg, '_verify_review_violation', return_value=True), \
             patch.object(cg, 'classify_chain_impact', return_value=['chain']):
            res = cg.guard_beat({}, 'test_proj', self.prompt_block, 1, self.project_dir)
            self.assertEqual(res['verdict'], 'flagged')
            self.assertTrue(res['halt'])

            manifest = server_common.read_manifest(self.project_dir)
            frame2 = manifest['frames'][1]
            self.assertEqual(frame2['quality_gate'], 'sequence_review_flagged')
            self.assertIn('天花板结构坍塌', frame2['vlm_qa_reason'])
            self.assertEqual(frame2.get('flag_origin'), 'chain_guard')
            # 关键：绝不写 review_frames_sha256
            self.assertNotIn('review_frames_sha256', frame2)

    def test_cosmetic_issue_does_not_halt(self):
        with patch.object(cg, 'check_beat_consistency', return_value=['微弱光比变化']), \
             patch.object(cg, '_verify_review_violation', return_value=True), \
             patch.object(cg, 'classify_chain_impact', return_value=['cosmetic']):
            res = cg.guard_beat({}, 'test_proj', self.prompt_block, 1, self.project_dir)
            self.assertEqual(res['verdict'], 'flagged')
            self.assertFalse(res['halt'])

            manifest = server_common.read_manifest(self.project_dir)
            frame2 = manifest['frames'][1]
            self.assertEqual(frame2['inline_beat_review']['verdict'], 'flagged')
            self.assertEqual(frame2['quality_gate'], 'auto_approved')

    def test_pipeline_loop_halts_and_breaks_on_chain_guard_halt(self):
        """向前建链（target_sequences=None）时检出 chain 违规 → 停链、不再渲下游。"""
        events = []
        def on_progress(evt, data):
            events.append((evt, data))

        prompt_block = (
            "IMAGE 1: Empty\n"
            "VIDEO 1: Dig\n"
            "IMAGE 2: Hole\n"
            "VIDEO 2: Pour\n"
            "IMAGE 3: Concrete\n"
        )
        fake_guard_res = {
            'verdict': 'flagged',
            'issues': [{'beat': 1, 'text': '透视严重畸变', 'severity': 'chain', 'verified': True}],
            'halt': True,
        }

        # IMG 002 必须不存在，否则整单重跑会把它当"已渲好"跳过（skip_generation）
        os.remove(os.path.join(self.frames_dir, 'img_002.webp'))

        # 准备伪造的候选图
        cand_img = os.path.join(self.frames_dir, 'cand_temp.webp')
        Image.new('RGB', (64, 64), color='green').save(cand_img, 'WEBP')
        with patch('candidate_selection_pipeline.generate_frame_candidates', return_value=[cand_img]), \
             patch('candidate_selection_pipeline.evaluate_and_select_best_candidate', return_value={'best_index': 1, 'selection_reason': 'ok', 'candidates': [{'score': 90}]}), \
             patch('candidate_selection_pipeline._generate_full_collage_from_frames'), \
             patch('candidate_selection_pipeline.chain_guard_mode', return_value='halt'), \
             patch('chain_guard.guard_beat', return_value=fake_guard_res) as mock_guard:
            res_manifest = csp.run_candidate_selection_frame_sequence(
                {'chainGuardMode': 'halt', 'imageModel': 'nano-banana-2'},
                'test_proj',
                prompt_block,
                on_progress=on_progress,
                target_sequences=None,
            )
            self.assertEqual(res_manifest.get('halted_at_beat'), 1)
            halt_evts = [e for e in events if e[0] == 'chain_guard_halt']
            self.assertEqual(len(halt_evts), 1)
            self.assertEqual(halt_evts[0][1]['beat'], 1)
            # 向前建链才允许停
            self.assertTrue(mock_guard.call_args.kwargs.get('allow_halt'))
            # 停链之后下游帧不该再被渲出来
            self.assertFalse(os.path.exists(os.path.join(self.frames_dir, 'img_003.webp')))

    def test_targeted_rerender_never_halts_the_cascade(self):
        """定向重渲（fix_frame_issue 的连带重渲）只审不停：中途 break 会留下
        上游新图 + 下游旧血统的半截链，正是 cascade_downstream 要消灭的东西。"""
        events = []
        def on_progress(evt, data):
            events.append((evt, data))

        prompt_block = (
            "IMAGE 1: Empty\n"
            "VIDEO 1: Dig\n"
            "IMAGE 2: Hole\n"
            "VIDEO 2: Pour\n"
            "IMAGE 3: Concrete\n"
        )
        cand_img = os.path.join(self.frames_dir, 'cand_temp.webp')
        Image.new('RGB', (64, 64), color='green').save(cand_img, 'WEBP')

        # 真的走一遍 guard_beat：它自己按 allow_halt=False 把 halt 压成 False。
        # 档位刻意用 autofix——定向重渲里若还触发自动修复，就是 fix_frame_issue
        # 调回 run_candidate_selection_frame_sequence 的无限递归。
        import pipeline_orchestrator as po
        with patch('candidate_selection_pipeline.generate_frame_candidates', return_value=[cand_img]), \
             patch('candidate_selection_pipeline.evaluate_and_select_best_candidate', return_value={'best_index': 1, 'selection_reason': 'ok', 'candidates': [{'score': 90}]}), \
             patch('candidate_selection_pipeline._generate_full_collage_from_frames'), \
             patch('candidate_selection_pipeline.chain_guard_mode', return_value='autofix'), \
             patch.object(po, 'fix_frame_issue') as no_fix, \
             patch.object(cg, 'check_beat_consistency', return_value=['透视严重畸变']), \
             patch.object(cg, '_verify_review_violation', return_value=True), \
             patch.object(cg, 'classify_chain_impact', return_value=['chain']):
            res_manifest = csp.run_candidate_selection_frame_sequence(
                {'chainGuardMode': 'halt', 'imageModel': 'nano-banana-2'},
                'test_proj',
                prompt_block,
                on_progress=on_progress,
                target_sequences=[2, 3],
            )
            self.assertIsNone(res_manifest.get('halted_at_beat'))
            self.assertEqual([e for e in events if e[0] == 'chain_guard_halt'], [])
            # 防递归：定向重渲里绝不触发自动修复
            self.assertEqual(no_fix.call_count, 0)
            # 整条 cascade 跑完
            self.assertTrue(os.path.exists(os.path.join(self.frames_dir, 'img_003.webp')))
            # 但问题照常记账（chain 级、只是不停链）
            manifest = server_common.read_manifest(self.project_dir)
            frame2 = next(f for f in manifest['frames'] if f.get('sequence') == 2)
            self.assertEqual(frame2['inline_beat_review']['verdict'], 'flagged')
            self.assertEqual(frame2['inline_beat_review']['issues'][0]['severity'], 'chain')
            # allow_halt=False 时不写 flag，交由收尾那趟统一盖章
            self.assertNotIn('flag_origin', frame2)

    # ── autofix 档 ────────────────────────────────────────────────────────
    _AUTOFIX_BLOCK = (
        "IMAGE 1: Empty\n"
        "VIDEO 1: Dig\n"
        "IMAGE 2: Hole\n"
        "VIDEO 2: Pour\n"
        "IMAGE 3: Concrete\n"
    )

    def _autofix_env(self, guard_side_effect, fix_side_effect):
        """跑一趟 autofix 档的整单生成，返回 (manifest, events, guard_mock, fix_mock)。"""
        os.remove(os.path.join(self.frames_dir, 'img_002.webp'))
        cand_img = os.path.join(self.frames_dir, 'cand_temp.webp')
        Image.new('RGB', (64, 64), color='green').save(cand_img, 'WEBP')
        events = []
        import pipeline_orchestrator as po
        with patch('candidate_selection_pipeline.generate_frame_candidates', return_value=[cand_img]), \
             patch('candidate_selection_pipeline.evaluate_and_select_best_candidate', return_value={'best_index': 1, 'selection_reason': 'ok', 'candidates': [{'score': 90}]}), \
             patch('candidate_selection_pipeline._generate_full_collage_from_frames'), \
             patch('candidate_selection_pipeline.chain_guard_mode', return_value='autofix'), \
             patch('chain_guard.guard_beat', side_effect=guard_side_effect) as guard_mock, \
             patch.object(po, 'fix_frame_issue', side_effect=fix_side_effect) as fix_mock:
            manifest = csp.run_candidate_selection_frame_sequence(
                {'chainGuardMode': 'autofix', 'imageModel': 'nano-banana-2'},
                'test_proj', self._AUTOFIX_BLOCK,
                on_progress=lambda e, d: events.append((e, d)),
                target_sequences=None,
            )
        return manifest, events, guard_mock, fix_mock

    @staticmethod
    def _guard_result(beat, halted):
        return {
            'verdict': 'flagged' if halted else 'pass',
            'issues': ([{'beat': beat, 'text': '透视严重畸变', 'severity': 'chain', 'verified': True}]
                       if halted else []),
            'halt': halted,
        }

    def test_autofix_repairs_the_beat_and_keeps_generating(self):
        seen = []

        def fake_guard(config, title, pb, beat, project_dir, on_progress=None, allow_halt=True):
            seen.append(beat)
            return self._guard_result(beat, allow_halt and len(seen) == 1)

        def fake_fix(config, title, pb, seq, on_progress=None, cascade_downstream=False, **kw):
            return {'prompt_block': pb + '\nIMAGE 9: fixed', 'reason': 'r',
                    'reverify': None, 'undoable': True}

        manifest, events, _guard, fix_mock = self._autofix_env(fake_guard, fake_fix)

        # 没停链，下游帧照渲
        self.assertIsNone(manifest.get('halted_at_beat'))
        self.assertTrue(os.path.exists(os.path.join(self.frames_dir, 'img_003.webp')))
        # 修了一次，且没连带重渲（下游此刻还不存在）
        self.assertEqual(fix_mock.call_count, 1)
        self.assertFalse(fix_mock.call_args.kwargs.get('cascade_downstream'))
        # 改写后的提示词正文回传给调用方，但**不落盘**
        self.assertIn('IMAGE 9: fixed', manifest.get('prompt_block') or '')
        self.assertNotIn('prompt_block', server_common.read_manifest(self.project_dir))
        # 两条事件都发了出去（前端事件链没有兜底分支，不发就是静默消失）
        kinds = [e for e, _ in events]
        self.assertIn('chain_guard_autofix', kinds)
        self.assertIn('chain_guard_autofix_done', kinds)

    def test_autofix_gives_up_and_halts_after_exhausting_attempts(self):
        def fake_guard(config, title, pb, beat, project_dir, on_progress=None, allow_halt=True):
            return self._guard_result(beat, allow_halt)

        def fake_fix(config, title, pb, seq, on_progress=None, cascade_downstream=False, **kw):
            return {'prompt_block': pb, 'reason': 'r', 'reverify': None, 'undoable': True}

        manifest, events, _guard, fix_mock = self._autofix_env(fake_guard, fake_fix)

        # 修满上限仍不过 → 退化成停链，绝不拿一张已知有结构问题的图当下游基底
        self.assertEqual(fix_mock.call_count, csp._CHAIN_GUARD_AUTOFIX_ATTEMPTS)
        self.assertEqual(manifest.get('halted_at_beat'), 1)
        self.assertFalse(os.path.exists(os.path.join(self.frames_dir, 'img_003.webp')))
        halts = [d for e, d in events if e == 'chain_guard_halt']
        self.assertEqual(len(halts), 1)
        self.assertTrue(halts[0]['autofix_exhausted'])
        self.assertNotIn('chain_guard_autofix_done', [e for e, _ in events])

    def test_autofix_failure_halts_instead_of_rendering_on_a_known_bad_frame(self):
        """修复本身没跑成（网关异常）≠ 这帧没问题：必须停链，不能带着已知坏图往下渲。"""
        def fake_guard(config, title, pb, beat, project_dir, on_progress=None, allow_halt=True):
            return self._guard_result(beat, allow_halt)

        def boom(config, title, pb, seq, on_progress=None, cascade_downstream=False, **kw):
            raise RuntimeError('gateway 502')

        manifest, events, _guard, fix_mock = self._autofix_env(fake_guard, boom)

        self.assertEqual(fix_mock.call_count, 1)   # 抛错即止，不重试到上限
        self.assertEqual(manifest.get('halted_at_beat'), 1)
        self.assertFalse(os.path.exists(os.path.join(self.frames_dir, 'img_003.webp')))

    def test_previous_halt_marker_does_not_leak_into_the_next_run(self):
        """上一趟停链留下的 halted_at_beat 不能被"未知键原样继承"带进下一趟——
        带进来的话，这一趟哪怕顺利渲到底，前端也会一直把这单报成「已暂停」。"""
        manifest = server_common.read_manifest(self.project_dir)
        manifest['halted_at_beat'] = 1
        server_common.write_manifest(self.project_dir, manifest)

        with patch('candidate_selection_pipeline._generate_full_collage_from_frames'), \
             patch('candidate_selection_pipeline.chain_guard_mode', return_value='off'):
            res_manifest = csp.run_candidate_selection_frame_sequence(
                {'imageModel': 'nano-banana-2'}, 'test_proj', self.prompt_block,
                target_sequences=None,
            )
        self.assertIsNone(res_manifest.get('halted_at_beat'))
        self.assertNotIn('halted_at_beat', server_common.read_manifest(self.project_dir))

    def test_allow_halt_false_records_issue_but_never_halts(self):
        with patch.object(cg, 'check_beat_consistency', return_value=['天花板结构坍塌']), \
             patch.object(cg, '_verify_review_violation', return_value=True), \
             patch.object(cg, 'classify_chain_impact', return_value=['chain']):
            res = cg.guard_beat({}, 'test_proj', self.prompt_block, 1, self.project_dir,
                                allow_halt=False)
            self.assertEqual(res['verdict'], 'flagged')
            self.assertFalse(res['halt'])
            manifest = server_common.read_manifest(self.project_dir)
            frame2 = manifest['frames'][1]
            self.assertEqual(frame2['inline_beat_review']['issues'][0]['severity'], 'chain')
            self.assertEqual(frame2['quality_gate'], 'auto_approved')
            self.assertNotIn('flag_origin', frame2)

    def test_outline_verdicts_are_carried_into_the_inline_record(self):
        """卡片工序的画面判定必须被守卫接住：收尾那趟会跳过守卫过的拍，
        逐拍层是这些判定唯一的产地，不接就永远静默丢失。"""
        def fake_check(config, prompt_block, beat, total_beats, before, after,
                       timeout=60, outline_items=None, outline_out=None):
            if outline_out is not None:
                outline_out.update({'3': 'missing', '4': 'visible'})
            return []

        with patch.object(cg, '_outline_items_for_review',
                          return_value={'1': [{'index': 3, 'text': '铺地板'},
                                              {'index': 4, 'text': '装踢脚线'}]}), \
             patch.object(cg, 'check_beat_consistency', side_effect=fake_check):
            res = cg.guard_beat({}, 'test_proj', self.prompt_block, 1, self.project_dir)
            self.assertEqual(res['verdict'], 'pass')
            self.assertEqual(res['inline_record']['outline_frame_verdicts'],
                             {'3': 'missing', '4': 'visible'})
            manifest = server_common.read_manifest(self.project_dir)
            frame2 = manifest['frames'][1]
            self.assertEqual(frame2['inline_beat_review']['outline_frame_verdicts'],
                             {'3': 'missing', '4': 'visible'})

    def test_pipeline_loop_does_not_break_in_report_mode(self):
        events = []
        def on_progress(evt, data):
            events.append((evt, data))

        prompt_block = (
            "IMAGE 1: Empty\n"
            "VIDEO 1: Dig\n"
            "IMAGE 2: Hole\n"
            "VIDEO 2: Pour\n"
            "IMAGE 3: Concrete\n"
        )
        fake_guard_res = {
            'verdict': 'flagged',
            'issues': [{'beat': 1, 'text': '透视严重畸变', 'severity': 'chain', 'verified': True}],
            'halt': True,
        }

        cand_img = os.path.join(self.frames_dir, 'cand_temp.webp')
        Image.new('RGB', (64, 64), color='green').save(cand_img, 'WEBP')
        with patch('candidate_selection_pipeline.generate_frame_candidates', return_value=[cand_img]), \
             patch('candidate_selection_pipeline.evaluate_and_select_best_candidate', return_value={'best_index': 1, 'selection_reason': 'ok', 'candidates': [{'score': 90}]}), \
             patch('candidate_selection_pipeline._generate_full_collage_from_frames'), \
             patch('candidate_selection_pipeline.chain_guard_mode', return_value='report'), \
             patch('chain_guard.guard_beat', return_value=fake_guard_res):
            res_manifest = csp.run_candidate_selection_frame_sequence(
                {'chainGuardMode': 'report', 'imageModel': 'nano-banana-2'},
                'test_proj',
                prompt_block,
                on_progress=on_progress,
                target_sequences=[2, 3],
            )
            # report 模式下不应当写 halted_at_beat 且不应当 break
            self.assertIsNone(res_manifest.get('halted_at_beat'))
            halt_evts = [e for e in events if e[0] == 'chain_guard_halt']
            self.assertEqual(len(halt_evts), 0)


if __name__ == '__main__':
    unittest.main()
