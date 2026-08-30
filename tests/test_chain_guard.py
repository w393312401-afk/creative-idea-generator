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
import contextlib
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
import frame_generator as fg


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

    def test_display_only_callers_can_opt_out_of_the_chain_fallback(self):
        """手动整套审查拿分级只用于展示、不决定任何动作（见
        pipeline_orchestrator._classify_review_severity）。那条路失败时把整单标成
        "会传染下游"是在编造判定，所以允许 on_error=None 换成"没分级"。
        停链那条路必须继续用默认的 chain 兜底——上面那个用例守着。"""
        with patch.object(cg, '_multimodal_chat', side_effect=Exception('LLM timeout')):
            self.assertEqual(cg.classify_chain_impact({}, ['问题一'], on_error=None), [])
        # 解析不出形状时同样走兜底
        with patch.object(cg, '_multimodal_chat', return_value='not json'):
            self.assertEqual(cg.classify_chain_impact({}, ['问题一'], on_error=None), [])
            self.assertEqual(cg.classify_chain_impact({}, ['问题一']), ['chain'])

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
            # 结构化问题清单也要落盘：定向修复读的正是它。缺了就退化成"一条本地
            # 问题、frames=[K]"，于是复核拿单张 K 去验一条"K-1 与 K 不一致"的判定
            # ——单张图证否不了，复核必然落进"仍存在"。
            issue = frame2['review_issues'][0]
            self.assertEqual(issue['frames'], [1, 2])
            self.assertEqual(issue['severity'], 'chain')
            self.assertEqual(issue['beat'], 1)

    def test_a_passing_recheck_takes_back_the_flag_it_had_set(self):
        """autofix 的次序是「fix_frame_issue（末尾 _reverify 可能盖 flag）→ 守卫再判
        一次」。守卫此前只在 halt 时写 gate，判过了什么都不写，于是那枚 flag 留在原地：
        循环报「✅ 复审通过，继续往下生成」，manifest 上这一帧却永远是 flagged——
        video_generator 的配对门禁认的正是这个字段。"""
        for origin in ('chain_guard', 'fix_reverify'):
            manifest = server_common.read_manifest(self.project_dir)
            frame2 = next(f for f in manifest['frames'] if f['sequence'] == 2)
            frame2.update(quality_gate='sequence_review_flagged', vlm_qa_reason='天花板结构坍塌',
                          flag_origin=origin, review_issues=[{'text': '天花板结构坍塌'}])
            server_common.write_manifest(self.project_dir, manifest)

            with patch.object(cg, 'check_beat_consistency', return_value=[]):
                res = cg.guard_beat({}, 'test_proj', self.prompt_block, 1, self.project_dir)
            self.assertEqual(res['verdict'], 'pass')

            frame2 = next(f for f in server_common.read_manifest(self.project_dir)['frames']
                          if f['sequence'] == 2)
            self.assertEqual(frame2['quality_gate'], 'pending_manual_review', origin)
            self.assertIsNone(frame2['vlm_qa_reason'])
            self.assertNotIn('flag_origin', frame2)
            self.assertNotIn('review_issues', frame2)

    def test_a_passing_recheck_never_touches_someone_elses_flag(self):
        """只认自己人盖的记号：人工标记与整套一致性审查的结论一概不碰。"""
        manifest = server_common.read_manifest(self.project_dir)
        frame2 = next(f for f in manifest['frames'] if f['sequence'] == 2)
        frame2.update(quality_gate='manual_flagged', manual_issue='门开反了')
        server_common.write_manifest(self.project_dir, manifest)

        with patch.object(cg, 'check_beat_consistency', return_value=[]):
            cg.guard_beat({}, 'test_proj', self.prompt_block, 1, self.project_dir)

        frame2 = next(f for f in server_common.read_manifest(self.project_dir)['frames']
                      if f['sequence'] == 2)
        self.assertEqual(frame2['quality_gate'], 'manual_flagged')
        self.assertEqual(frame2['manual_issue'], '门开反了')

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
        # 重渲内部不再重复审这一拍：紧接着的那次 guard_beat 就是它的复审
        self.assertTrue(fix_mock.call_args.kwargs.get('suppress_chain_guard'))
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

    def test_autofix_stops_when_the_triptych_gate_rolls_the_fix_back(self):
        """门禁判这次修复把画面改坏了 → 已自动退回上一版，帧图与提示词都回到修复前。
        再修一次是拿同样的输入跑同样的结果，每一轮还要白烧一整套守卫复审。当场转停链，
        让人来看——修后那一版留了档，门禁误判的话可以「采用修后版」。"""
        def fake_guard(config, title, pb, beat, project_dir, on_progress=None, allow_halt=True):
            return self._guard_result(beat, allow_halt)

        def fake_fix(config, title, pb, seq, on_progress=None, cascade_downstream=False, **kw):
            return {'prompt_block': pb, 'reason': 'r', 'reverify': None, 'undoable': False,
                    'rolled_back': True, 'rejected_fix': {'at': 'T'},
                    'triptych': {'verdict': 'regressed'}}

        manifest, events, _guard, fix_mock = self._autofix_env(fake_guard, fake_fix)

        self.assertEqual(fix_mock.call_count, 1, '回滚即止，不把上限烧完')
        self.assertEqual(manifest.get('halted_at_beat'), 1)
        self.assertIn('chain_guard_autofix_rolled_back', [e for e, _ in events])
        self.assertNotIn('chain_guard_autofix_done', [e for e, _ in events])
        self.assertFalse(os.path.exists(os.path.join(self.frames_dir, 'img_003.webp')))

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


class TestSingleGenerationChainGuard(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.project_dir = os.path.join(self.tmp_dir, 'outputs', 'test_single_proj')
        self.frames_dir = os.path.join(self.project_dir, 'frames')
        os.makedirs(self.frames_dir, exist_ok=True)
        self.orig_root = server_common.OUTPUT_ROOT
        server_common.OUTPUT_ROOT = os.path.join(self.tmp_dir, 'outputs')

        # 创建封面
        cover_path = os.path.join(self.project_dir, 'cover.png')
        Image.new('RGB', (64, 64), color='red').save(cover_path, 'PNG')

        self.prompt_block = (
            "IMAGE 1: Initial empty site\n"
            "VIDEO 1: Digging earth\n"
            "IMAGE 2: Excavated trench\n"
            "VIDEO 2: Pouring concrete\n"
            "IMAGE 3: Concrete foundation\n"
        )

    def tearDown(self):
        server_common.OUTPUT_ROOT = self.orig_root
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _fake_edit_gen(self, config, prompt, ref, target_path, control_prompt=None):
        Image.new('RGB', (64, 64), color='blue').save(target_path, 'WEBP')
        return 'standard'

    def _fake_text_gen(self, config, prompt, target_path):
        Image.new('RGB', (64, 64), color='red').save(target_path, 'WEBP')
        return 'standard'

    def test_single_generation_triggers_guard_beat_per_frame(self):
        """单次生成模式下，生成第 2、3 帧时每张都会自动调用 guard_beat 逐拍审查。"""
        guarded_beats = []
        def fake_guard_beat(config, title, prompt_block, beat, project_dir, on_progress=None, allow_halt=True):
            guarded_beats.append(beat)
            return {'verdict': 'pass', 'issues': [], 'halt': False}

        with patch('frame_generator._generate_image_edit', side_effect=self._fake_edit_gen), \
             patch('frame_generator._generate_text_image', side_effect=self._fake_text_gen), \
             patch('chain_guard.guard_beat', side_effect=fake_guard_beat) as mock_guard:
            res_manifest = fg.generate_frame_sequence(
                {'imageBackend': 'api', 'chainGuardMode': 'autofix', 'allowTextOnlyAnchor': True},
                'test_single_proj',
                self.prompt_block,
            )
            # 生成 3 帧：第 2 帧（beat 1）和第 3 帧（beat 2）均被审查
            self.assertEqual(guarded_beats, [1, 2])
            self.assertIsNone(res_manifest.get('halted_at_beat'))
            self.assertEqual(len(res_manifest.get('frames', [])), 3)

    def test_single_generation_halts_on_chain_violation_in_halt_mode(self):
        """单次生成模式在 halt 模式下检出 chain 违规 → 停链并中断后续帧生成。"""
        events = []
        def on_progress(evt, data):
            events.append((evt, data))

        fake_guard_res = {
            'verdict': 'flagged',
            'issues': [{'beat': 1, 'text': '地标严重漂移', 'severity': 'chain', 'verified': True}],
            'halt': True,
        }

        with patch('frame_generator._generate_image_edit', side_effect=self._fake_edit_gen), \
             patch('frame_generator._generate_text_image', side_effect=self._fake_text_gen), \
             patch('frame_generator.chain_guard_mode', return_value='halt'), \
             patch('chain_guard.guard_beat', return_value=fake_guard_res):
            res_manifest = fg.generate_frame_sequence(
                {'imageBackend': 'api', 'chainGuardMode': 'halt', 'allowTextOnlyAnchor': True},
                'test_single_proj',
                self.prompt_block,
                on_progress=on_progress,
            )
            # 第 1 拍（IMG 2）检出问题即停链
            self.assertEqual(res_manifest.get('halted_at_beat'), 1)
            halt_evts = [e for e in events if e[0] == 'chain_guard_halt']
            self.assertEqual(len(halt_evts), 1)
            self.assertEqual(halt_evts[0][1]['beat'], 1)
            # 第 3 帧不应当被生成
            self.assertFalse(os.path.exists(os.path.join(self.frames_dir, 'img_003.webp')))

    def test_single_generation_autofix_mode_invokes_fix_frame_issue(self):
        """单次生成模式在 autofix 档检出问题时就地自动调用 fix_frame_issue。"""
        events = []
        def on_progress(evt, data):
            events.append((evt, data))

        call_count = 0
        def guard_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # 第一次审：有结构级问题
                return {
                    'verdict': 'flagged',
                    'issues': [{'beat': 1, 'text': '材质突变', 'severity': 'chain', 'verified': True}],
                    'halt': True,
                }
            # 修复后复审：pass
            return {'verdict': 'pass', 'issues': [], 'halt': False}

        import pipeline_orchestrator as po
        with patch('frame_generator._generate_image_edit', side_effect=self._fake_edit_gen), \
             patch('frame_generator._generate_text_image', side_effect=self._fake_text_gen), \
             patch('frame_generator.chain_guard_mode', return_value='autofix'), \
             patch('chain_guard.guard_beat', side_effect=guard_side_effect), \
             patch.object(po, 'fix_frame_issue', return_value={'prompt_block': self.prompt_block}) as mock_fix:
            res_manifest = fg.generate_frame_sequence(
                {'imageBackend': 'api', 'chainGuardMode': 'autofix', 'allowTextOnlyAnchor': True},
                'test_single_proj',
                self.prompt_block,
                on_progress=on_progress,
            )
            # fix_frame_issue 被调用了一次
            self.assertEqual(mock_fix.call_count, 1)
            # 成功修复后复审通过，任务顺利跑完
            self.assertIsNone(res_manifest.get('halted_at_beat'))
            done_evts = [e for e in events if e[0] == 'chain_guard_autofix_done']
            self.assertEqual(len(done_evts), 1)

    def test_single_generation_suppress_chain_guard_skips_review(self):
        """chain_guard_review=False 时完全不触发 guard_beat。"""
        with patch('frame_generator._generate_image_edit', side_effect=self._fake_edit_gen), \
             patch('frame_generator._generate_text_image', side_effect=self._fake_text_gen), \
             patch('chain_guard.guard_beat') as mock_guard:
            res_manifest = fg.generate_frame_sequence(
                {'imageBackend': 'api', 'chainGuardMode': 'autofix', 'allowTextOnlyAnchor': True},
                'test_single_proj',
                self.prompt_block,
                chain_guard_review=False,
            )
            self.assertEqual(mock_guard.call_count, 0)
            self.assertIsNone(res_manifest.get('halted_at_beat'))


class TestAnchorGuardHalt(unittest.TestCase):
    """首帧锚点审查的停链结论必须被接住。

    此前三个渲染入口都只写了 `guard_anchor(...)`、返回值里的 halt 没人看：首帧判出
    结构级问题时不停链、不自动修、也不发 chain_guard_halt，只有 manifest 上悄悄多出
    一枚 sequence_review_flagged。首帧是整条 i2i 链的地基，它歪了后面每一帧都跟着歪，
    而屏幕上一声不响——用户要等帧网格下次从 manifest 重画才突然看见那枚 flag。
    """

    _FLAGGED = {
        'verdict': 'flagged',
        'issues': [{'beat': 0, 'layer': 'anchor', 'text': '机位俯仰角度过高',
                    'frames': [1], 'severity': 'chain', 'verified': True}],
        'halt': True,
    }
    _PASS = {'verdict': 'pass', 'issues': [], 'halt': False}

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.project_dir = os.path.join(self.tmp_dir, 'outputs', 'test_anchor_proj')
        self.frames_dir = os.path.join(self.project_dir, 'frames')
        os.makedirs(self.frames_dir, exist_ok=True)
        self.orig_root = server_common.OUTPUT_ROOT
        server_common.OUTPUT_ROOT = os.path.join(self.tmp_dir, 'outputs')
        Image.new('RGB', (64, 64), color='red').save(
            os.path.join(self.project_dir, 'cover.png'), 'PNG')
        self.prompt_block = (
            "IMAGE 1: Initial empty site\n"
            "VIDEO 1: Digging earth\n"
            "IMAGE 2: Excavated trench\n"
            "VIDEO 2: Pouring concrete\n"
            "IMAGE 3: Concrete foundation\n"
        )

    def tearDown(self):
        server_common.OUTPUT_ROOT = self.orig_root
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _fake_edit_gen(self, config, prompt, ref, target_path, control_prompt=None):
        Image.new('RGB', (64, 64), color='blue').save(target_path, 'WEBP')
        return 'standard'

    def _fake_text_gen(self, config, prompt, target_path):
        Image.new('RGB', (64, 64), color='red').save(target_path, 'WEBP')
        return 'standard'

    def _run_api_line(self, mode, anchor_side_effect, fix=None, events=None):
        import pipeline_orchestrator as po
        ctx = [
            patch('frame_generator._generate_image_edit', side_effect=self._fake_edit_gen),
            patch('frame_generator._generate_text_image', side_effect=self._fake_text_gen),
            patch('frame_generator.chain_guard_mode', return_value=mode),
            patch('chain_guard.guard_anchor', side_effect=anchor_side_effect),
            patch('chain_guard.guard_beat', return_value=dict(self._PASS)),
            patch.object(po, 'fix_frame_issue',
                         **({'side_effect': fix} if fix else {'return_value': None})),
        ]
        with contextlib.ExitStack() as stack:
            mocks = [stack.enter_context(c) for c in ctx]
            manifest = fg.generate_frame_sequence(
                {'imageBackend': 'api', 'chainGuardMode': mode, 'allowTextOnlyAnchor': True},
                'test_anchor_proj', self.prompt_block,
                on_progress=(lambda e, d: events.append((e, d))) if events is not None else None,
            )
        return manifest, mocks[-1]

    def test_flagged_anchor_halts_the_chain_before_any_downstream_frame(self):
        events = []
        manifest, _fix = self._run_api_line('halt', lambda *a, **k: dict(self._FLAGGED),
                                            events=events)
        # 停在首帧：beat 是 0，所以"停没停"只能看 halted_at_sequence
        self.assertEqual(manifest.get('halted_at_beat'), 0)
        self.assertEqual(manifest.get('halted_at_sequence'), 1)
        self.assertFalse(os.path.exists(os.path.join(self.frames_dir, 'img_002.webp')))
        halts = [d for e, d in events if e == 'chain_guard_halt']
        self.assertEqual(len(halts), 1)
        self.assertEqual(halts[0]['beat'], 0)
        self.assertEqual(halts[0]['sequence'], 1)

    def test_autofix_repairs_the_anchor_and_keeps_building_the_chain(self):
        events = []
        seen = []

        def anchor(*a, **k):
            seen.append(1)
            return dict(self._FLAGGED) if len(seen) == 1 else dict(self._PASS)

        def fix(config, title, pb, seq, on_progress=None, cascade_downstream=False, **kw):
            return {'prompt_block': pb, 'reason': 'r', 'reverify': None, 'undoable': True}

        manifest, fix_mock = self._run_api_line('autofix', anchor, fix=fix, events=events)
        self.assertEqual(fix_mock.call_count, 1)
        self.assertEqual(fix_mock.call_args.args[3], 1, '修的必须是首帧')
        self.assertTrue(fix_mock.call_args.kwargs.get('suppress_chain_guard'))
        self.assertIsNone(manifest.get('halted_at_sequence'))
        self.assertEqual(len(manifest.get('frames', [])), 3)
        self.assertIn('chain_guard_autofix_done', [e for e, _ in events])

    def test_soft_mode_autofixes_the_anchor_then_keeps_going_when_it_cannot_fix_it(self):
        """autofix_soft 与 autofix 只差最后一步：修满次数仍不过时不停链。

        这一档是为「首帧被机位类问题反复判死、整单停在 IMG 001、一帧可用序列都拿不到」
        开的口子。修还是要修（次数照 autofix 走满），修不动就记账放行。
        """
        events = []

        def fix(config, title, pb, seq, on_progress=None, cascade_downstream=False, **kw):
            return {'prompt_block': pb, 'reason': 'r', 'reverify': None, 'undoable': True}

        manifest, fix_mock = self._run_api_line(
            'autofix_soft', lambda *a, **k: dict(self._FLAGGED), fix=fix, events=events)

        # 修复照跑满 autofix 的次数——软档松的是结论，不是努力
        self.assertEqual(fix_mock.call_count, fg._CHAIN_GUARD_AUTOFIX_ATTEMPTS)
        # 但不停链：整条序列渲完
        self.assertIsNone(manifest.get('halted_at_sequence'))
        self.assertEqual(len(manifest.get('frames', [])), 3)
        self.assertEqual([e for e, _ in events if e == 'chain_guard_halt'], [])
        # 必须发软档事件：不发的话结构级问题在屏幕上完全无声（事件链没有 else 兜底）
        softs = [d for e, d in events if e == 'chain_guard_soft_continue']
        self.assertEqual(len(softs), 1)
        self.assertEqual(softs[0]['sequence'], 1)
        self.assertTrue(softs[0]['issues'], '问题清单要原样带给前端，否则用户不知道放行了什么')

    def test_soft_mode_stays_quiet_when_the_anchor_autofix_actually_worked(self):
        """修好了就没什么可放行的——软档事件只在"修不动"时发。"""
        events = []
        seen = []

        def anchor(*a, **k):
            seen.append(1)
            return dict(self._FLAGGED) if len(seen) == 1 else dict(self._PASS)

        def fix(config, title, pb, seq, on_progress=None, cascade_downstream=False, **kw):
            return {'prompt_block': pb, 'reason': 'r', 'reverify': None, 'undoable': True}

        manifest, _fix = self._run_api_line('autofix_soft', anchor, fix=fix, events=events)
        self.assertIsNone(manifest.get('halted_at_sequence'))
        self.assertIn('chain_guard_autofix_done', [e for e, _ in events])
        self.assertEqual([e for e, _ in events if e == 'chain_guard_soft_continue'], [])

    def test_report_mode_records_the_anchor_verdict_without_halting(self):
        events = []
        manifest, _fix = self._run_api_line('report', lambda *a, **k: dict(self._FLAGGED),
                                            events=events)
        self.assertIsNone(manifest.get('halted_at_sequence'))
        self.assertEqual([e for e, _ in events if e == 'chain_guard_halt'], [])
        self.assertEqual(len(manifest.get('frames', [])), 3)

    def test_frame_event_declares_that_a_guard_verdict_is_still_coming(self):
        """'frame' 事件里的 gate 是守卫**跑之前**的读数。不声明后面还有一道审查，
        前端就会替守卫抢答"质检通过"，几十秒后守卫判废，卡片下次重画突然变红。"""
        events = []
        self._run_api_line('halt', lambda *a, **k: dict(self._PASS), events=events)
        frame_evts = [d for e, d in events if e == 'frame']
        self.assertTrue(frame_evts)
        self.assertTrue(all(d.get('guard_pending') for d in frame_evts))

        events_off = []
        with patch('frame_generator._generate_image_edit', side_effect=self._fake_edit_gen), \
             patch('frame_generator._generate_text_image', side_effect=self._fake_text_gen), \
             patch('chain_guard.guard_anchor') as anchor_mock, \
             patch('chain_guard.guard_beat') as beat_mock:
            shutil.rmtree(self.frames_dir, ignore_errors=True)
            os.makedirs(self.frames_dir, exist_ok=True)
            fg.generate_frame_sequence(
                {'imageBackend': 'api', 'chainGuardMode': 'halt', 'allowTextOnlyAnchor': True},
                'test_anchor_proj', self.prompt_block,
                on_progress=lambda e, d: events_off.append((e, d)),
                chain_guard_review=False,
            )
        self.assertEqual(anchor_mock.call_count, 0)
        self.assertEqual(beat_mock.call_count, 0)
        self.assertFalse(any(d.get('guard_pending') for e, d in events_off if e == 'frame'))


class TestAnchorGuardHaltCandidateLine(unittest.TestCase):
    """4选1 线同款：首帧判废就停，别拿一张已知歪掉的地基往下建整条链。"""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.project_dir = os.path.join(self.tmp_dir, 'outputs', 'test_anchor_cand')
        self.frames_dir = os.path.join(self.project_dir, 'frames')
        os.makedirs(self.frames_dir, exist_ok=True)
        self.orig_root = server_common.OUTPUT_ROOT
        server_common.OUTPUT_ROOT = os.path.join(self.tmp_dir, 'outputs')
        self.prompt_block = (
            "IMAGE 1: Empty\n"
            "VIDEO 1: Dig\n"
            "IMAGE 2: Hole\n"
            "VIDEO 2: Pour\n"
            "IMAGE 3: Concrete\n"
        )

    def tearDown(self):
        server_common.OUTPUT_ROOT = self.orig_root
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_flagged_anchor_halts_the_candidate_line(self):
        cand_img = os.path.join(self.tmp_dir, 'cand_temp.webp')
        Image.new('RGB', (64, 64), color='green').save(cand_img, 'WEBP')
        flagged = {
            'verdict': 'flagged',
            'issues': [{'beat': 0, 'layer': 'anchor', 'text': '机位俯仰角度过高',
                        'frames': [1], 'severity': 'chain', 'verified': True}],
            'halt': True,
        }
        events = []
        with patch('candidate_selection_pipeline.generate_frame_candidates', return_value=[cand_img]), \
             patch('candidate_selection_pipeline.evaluate_and_select_best_candidate',
                   return_value={'best_index': 1, 'selection_reason': 'ok', 'candidates': [{'score': 90}]}), \
             patch('candidate_selection_pipeline._generate_full_collage_from_frames'), \
             patch('candidate_selection_pipeline.chain_guard_mode', return_value='halt'), \
             patch('chain_guard.guard_anchor', return_value=flagged), \
             patch('chain_guard.guard_beat', return_value={'verdict': 'pass', 'issues': [], 'halt': False}):
            manifest = csp.run_candidate_selection_frame_sequence(
                {'chainGuardMode': 'halt', 'imageModel': 'nano-banana-2'},
                'test_anchor_cand', self.prompt_block,
                on_progress=lambda e, d: events.append((e, d)),
                target_sequences=None,
            )
        self.assertEqual(manifest.get('halted_at_beat'), 0)
        self.assertEqual(manifest.get('halted_at_sequence'), 1)
        self.assertFalse(os.path.exists(os.path.join(self.frames_dir, 'img_002.webp')))
        halts = [d for e, d in events if e == 'chain_guard_halt']
        self.assertEqual(len(halts), 1)
        self.assertEqual(halts[0]['sequence'], 1)
        # 首帧那次 'frame' 事件必须自报"审查还没说话"
        frame_evts = [d for e, d in events if e == 'frame']
        self.assertTrue(frame_evts and frame_evts[0].get('guard_pending'))


class TestAutofixSoftMode(unittest.TestCase):
    """autofix_soft 档：修满次数仍不过时记账放行，一趟把整条序列渲完。"""

    def test_mode_helpers_cover_every_registered_mode(self):
        """档位语义只有 chain_guard 这一份。GATE_SETTINGS 里新增档位却忘了在这里
        定义它的 halt/autofix 语义，会静默落进"两个都是 False"（既不修也不停），
        看上去和 report 一模一样。"""
        registered = set(server_common._GATE_BY_KEY['chainGuardMode']['options'])
        self.assertEqual(registered, {'off', 'report', 'halt', 'autofix', 'autofix_soft'})
        expected = {
            'off': (False, False),
            'report': (False, False),
            'halt': (False, True),
            'autofix': (True, True),
            'autofix_soft': (True, False),
        }
        for mode, (autofix, halt) in expected.items():
            with self.subTest(mode=mode):
                self.assertEqual(cg.guard_autofix_enabled(mode), autofix)
                self.assertEqual(cg.guard_halt_enabled(mode), halt)

    def test_no_call_site_hardcodes_the_mode_literals(self):
        """三个渲染入口各抄一遍 halt 条件正是当初 guard_anchor 的 halt 被集体丢弃的
        成因。判据必须走 guard_halt_enabled / guard_autofix_enabled，任何一处漏改都
        会让那一条链在软档下照旧停。"""
        import re
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        pattern = re.compile(r"guard_mode\s*(==\s*'autofix'|in\s*\(\s*'halt')")
        for name in ('frame_generator.py', 'candidate_selection_pipeline.py', 'chain_guard.py'):
            with self.subTest(file=name):
                src = open(os.path.join(root, name), encoding='utf-8').read()
                self.assertIsNone(pattern.search(src),
                                  f'{name} 里还有手写的档位字面量，应改用 guard_*_enabled')

    def test_beat_guard_keeps_the_loop_going_in_soft_mode(self):
        """逐拍守卫也吃这一档：软档下 flagged 的拍不 break，链继续往前建。"""
        tmp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp_dir, True)
        project_dir = os.path.join(tmp_dir, 'outputs', 'test_soft_beat')
        frames_dir = os.path.join(project_dir, 'frames')
        os.makedirs(frames_dir, exist_ok=True)
        orig_root = server_common.OUTPUT_ROOT
        server_common.OUTPUT_ROOT = os.path.join(tmp_dir, 'outputs')
        self.addCleanup(lambda: setattr(server_common, 'OUTPUT_ROOT', orig_root))

        prompt_block = (
            "IMAGE 1: Empty\n"
            "VIDEO 1: Dig\n"
            "IMAGE 2: Hole\n"
            "VIDEO 2: Pour\n"
            "IMAGE 3: Concrete\n"
        )
        flagged = {
            'verdict': 'flagged',
            'issues': [{'beat': 1, 'text': '透视严重畸变', 'severity': 'chain', 'verified': True}],
            'halt': True,
        }
        cand_img = os.path.join(tmp_dir, 'cand_temp.webp')
        Image.new('RGB', (64, 64), color='green').save(cand_img, 'WEBP')

        events = []

        def fix(config, title, pb, seq, on_progress=None, cascade_downstream=False, **kw):
            return {'prompt_block': pb, 'reason': 'r', 'reverify': None, 'undoable': True}

        import pipeline_orchestrator as po
        with patch('candidate_selection_pipeline.generate_frame_candidates', return_value=[cand_img]), \
             patch('candidate_selection_pipeline.evaluate_and_select_best_candidate',
                   return_value={'best_index': 1, 'selection_reason': 'ok', 'candidates': [{'score': 90}]}), \
             patch('candidate_selection_pipeline._generate_full_collage_from_frames'), \
             patch('candidate_selection_pipeline.chain_guard_mode', return_value='autofix_soft'), \
             patch('chain_guard.guard_anchor', return_value={'verdict': 'pass', 'issues': [], 'halt': False}), \
             patch('chain_guard.guard_beat', return_value=flagged), \
             patch.object(po, 'fix_frame_issue', side_effect=fix):
            manifest = csp.run_candidate_selection_frame_sequence(
                {'chainGuardMode': 'autofix_soft', 'imageModel': 'nano-banana-2'},
                'test_soft_beat', prompt_block,
                on_progress=lambda e, d: events.append((e, d)),
                target_sequences=None,
            )

        self.assertIsNone(manifest.get('halted_at_sequence'))
        self.assertEqual([e for e, _ in events if e == 'chain_guard_halt'], [])
        # 每一拍都修不动 → 每一拍都发一条软档事件，且整条序列渲到底
        softs = [d for e, d in events if e == 'chain_guard_soft_continue']
        self.assertTrue(softs)
        self.assertTrue(os.path.exists(os.path.join(frames_dir, 'img_003.webp')),
                        '软档下停在半路就等于白开了这一档')


class TestAnchorClassifierCalibration(unittest.TestCase):
    """锚点比的是"我们重做的第一帧 vs 原片第一帧"，不是同一条链上的相邻两帧。
    分级器照 camera-drift 一律判 chain 时，首帧几乎必然被判死（实测连修 2 次都是
    同一条"俯角过高"）。"""

    def _capture_system_prompt(self, **kwargs):
        seen = {}

        def fake_chat(config, system_prompt, user_text, images, **kw):
            seen['system'] = system_prompt
            return '["cosmetic"]'

        with patch('chain_guard._multimodal_chat', side_effect=fake_chat):
            out = cg.classify_chain_impact({}, ['机位俯仰角度略高'], **kwargs)
        return seen.get('system', ''), out

    def test_anchor_layer_gets_the_camera_tolerance_rider(self):
        system, out = self._capture_system_prompt(layer='anchor')
        self.assertIn('ANCHOR-FRAME CALIBRATION', system)
        self.assertIn('DIFFERENT CAMERA FAMILY', system)
        self.assertEqual(out, ['cosmetic'])

    def test_beat_layer_keeps_the_uncalibrated_prompt(self):
        """逐拍层不许松：同一条链上相邻两帧的机位一动就是真漂移。"""
        system, _ = self._capture_system_prompt()
        self.assertNotIn('ANCHOR-FRAME CALIBRATION', system)

    def test_the_rider_is_appended_so_the_cached_prefix_is_untouched(self):
        system, _ = self._capture_system_prompt(layer='anchor')
        self.assertTrue(system.startswith(cg._CHAIN_CLASSIFIER_SYSTEM_PROMPT))

    def test_guard_anchor_classifies_with_the_anchor_calibration(self):
        """guard_anchor 忘了传 layer='anchor' 的话，上面那段校准写了也白写。"""
        tmp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp_dir, True)
        project_dir = os.path.join(tmp_dir, 'outputs', 'test_anchor_cls')
        frames_dir = os.path.join(project_dir, 'frames')
        os.makedirs(frames_dir, exist_ok=True)
        orig_root = server_common.OUTPUT_ROOT
        server_common.OUTPUT_ROOT = os.path.join(tmp_dir, 'outputs')
        self.addCleanup(lambda: setattr(server_common, 'OUTPUT_ROOT', orig_root))
        Image.new('RGB', (64, 64), color='red').save(
            os.path.join(frames_dir, 'img_001.webp'), 'WEBP')
        with open(os.path.join(project_dir, 'manifest.json'), 'w', encoding='utf-8') as f:
            json.dump({'title': 'test_anchor_cls',
                       'frames': [{'sequence': 1, 'file': 'frames/img_001.webp'}]}, f)

        with patch('chain_guard.check_anchor_consistency', return_value=['机位俯仰角度略高']), \
             patch('chain_guard._verify_review_violation', return_value=True), \
             patch('chain_guard.resolve_cover_reference', return_value=None), \
             patch('chain_guard.find_reference_frames_for_project', return_value=({}, {})), \
             patch('chain_guard.classify_chain_impact', return_value=['cosmetic']) as mock_cls:
            res = cg.guard_anchor({}, 'test_anchor_cls', 'IMAGE 1: Empty\nVIDEO 1: Dig\nIMAGE 2: Hole\n',
                                  project_dir)

        self.assertEqual(mock_cls.call_args.kwargs.get('layer'), 'anchor')
        # 降成 cosmetic 之后不再停链——这正是三组 4选1 全死在 IMG 001 的解法
        self.assertEqual(res['verdict'], 'flagged')
        self.assertFalse(res['halt'])


if __name__ == '__main__':
    unittest.main()

