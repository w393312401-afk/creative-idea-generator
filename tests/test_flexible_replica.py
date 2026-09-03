# -*- coding: utf-8 -*-
"""灵活复刻六步改造的回归测试。

每条用例都钉在 2026-09-03 那批海蚀洞变体（20 图 + 19 视频）暴露出来的一个具体缺陷上
——那一批里三条硬闸该拦的一条没拦，因为当时**代码里根本没有闸**：`mutate.py` 全文件
零处 validate，`replica_pipeline.py` 从没调用过任何状态账校验。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from prompt_pipeline.ontology import (
    build_pack, infer_role, render_beat_title, detect_material_category, MaterialPack,
)
from prompt_pipeline.object_ledger import (
    build_object_ledger, validate_object_ledger, validate_video_objects,
    allowed_video_objects, extract_objects, beats_declare_objects,
)
from prompt_pipeline.topology import build_topology, validate_isomorphism
from prompt_pipeline.anchor_geometry import (
    parse_camera, reproject_scale, cast_screen_percent, cast_scale_hint,
)
from prompt_pipeline.mutate import (
    generate_orthogonal_variant, scrub_carrier_leak, render_beat_from_role,
)


def _seacave_ladder():
    """海蚀洞那一批的节拍骨架，保留了它全部四个真实缺陷。"""
    return [
        {'index': 1, 'state_after': 'sunken chassis, stagnant water, rotted driftwood'},
        {'index': 2, 'state_after': 'cargo bed fully dewatered and scraped clean',
         'visible_action': 'worker squeegees standing water', 'operation': 'dewater rock basin'},
        {'index': 3, 'state_after': 'compacted basalt aggregate sub-base leveled across the subfloor',
         'visible_action': 'worker rakes crushed basalt aggregate into a level bed',
         'operation': 'lay cobble subfloor'},
        {'index': 4, 'state_after': 'three portal arches erected and bolted',
         'visible_action': 'worker bolts the portal arch', 'operation': 'erect timber portal'},
        {'index': 5, 'state_after': 'left wall enclosed with riveted exterior cladding plates',
         'visible_action': 'worker rivets a cladding plate', 'operation': 'lay stone infill'},
        {'index': 6, 'state_after': 'twin door leaves hung on strap hinges with a locking bar',
         'visible_action': 'worker hoists the door leaf', 'operation': 'hang double doors'},
        {'index': 7, 'state_after': 'upper facade sealed in a matte protective coating',
         'visible_action': 'worker brushes the protective coating', 'operation': 'coat facade panels'},
        {'index': 8, 'state_after': 'flagstone landing and scrape grate installed',
         'visible_action': 'worker beds the flagstone pavers', 'operation': 'pave flagstone patio'},
        {'index': 9,
         'state_before': 'twin window cut-outs on the right steel wall; flue collar in the ceiling',
         'state_after': 'raw interior revealed', 'operation': 'threshold'},
        {'index': 10, 'state_after': 'interior sealed in a continuous taped vapour barrier',
         'visible_action': 'worker levels the crushed aggregate bed with a rake, then unrolls the vapour barrier',
         'operation': 'install vapor barrier'},
        {'index': 11, 'state_after': 'larch floor sleeper bays fastened',
         'visible_action': 'worker fixes the floor battens', 'operation': 'lay floor battens'},
        {'index': 12, 'state_after': 'wall framing and ceiling ribs erected; conduit and flue sleeve roughed in',
         'visible_action': 'worker fastens the conduit', 'operation': 'frame walls ceiling'},
    ]


class TestObjectLedgerGate(unittest.TestCase):
    """Step 2：三条硬闸。"""

    def setUp(self):
        self.beats = _seacave_ladder()
        self.violations = validate_object_ledger(self.beats)
        self.by_rule = {}
        for v in self.violations:
            self.by_rule.setdefault(v['rule'], []).append(v)

    def test_phantom_window_is_caught(self):
        """窗户在第 9 拍第一次作为锚点出现，此后被 12 张图继承，却从没有一拍建造过它。"""
        objects = {v['object'] for v in self.by_rule.get('phantom', [])}
        self.assertIn('window', objects)

    def test_flue_collar_referenced_before_it_exists(self):
        """穿顶领圈在第 9 拍已存在，第 12 拍才 roughed in。"""
        hits = [v for v in self.by_rule.get('phantom', []) if v['object'] == 'flue_collar']
        self.assertTrue(hits)
        self.assertEqual(hits[0]['beat'], 9)

    def test_completed_sub_base_is_not_reworked(self):
        """VIDEO 9 让工人重新耙已经压实找平的碎石层。"""
        objects = {v['object'] for v in self.by_rule.get('regression', [])}
        self.assertIn('sub_base', objects)

    def test_a_clean_ladder_passes(self):
        """闸不该对一条账目闭合的梯子报任何东西。"""
        clean = [
            {'index': 1, 'state_after': 'site cleared of debris', 'operation': 'strip out'},
            {'index': 2, 'state_after': 'compacted aggregate sub-base laid', 'operation': 'lay sub-base'},
            {'index': 3, 'state_after': 'structural frame erected', 'operation': 'erect frame'},
            {'index': 4, 'state_after': 'exterior cladding fixed', 'state_before': 'structural frame stands',
             'operation': 'sheathe exterior panel'},
        ]
        self.assertEqual(validate_object_ledger(clean), [])

    def test_every_violation_is_blocking(self):
        for v in self.violations:
            self.assertEqual(v['severity'], 'blocking')


class TestVideoDeltaGate(unittest.TestCase):
    """Step 3：视频里的建造产物必须 ⊆ 首尾帧差量 ∪ 已建成的东西。"""

    def test_phantom_floor_in_video_one(self):
        """VIDEO 1 的末帧写着「完全排空、刮擦干净」，视频却在铺木地板。"""
        beats = _seacave_ladder()
        for b in beats:                       # 申报式账本 → 精确比对 → 可拦单
            b.setdefault('produced_objects', [])
        beats[1]['produced_objects'] = ['demolition']
        videos = {1: 'the worker hauls in weathered timber planks and lays floorboards, '
                     'revealing the fully planked deck of the last frame'}
        hits = validate_video_objects(beats, videos)
        self.assertTrue(hits)
        self.assertEqual(hits[0]['object'], 'finish_floor')
        self.assertEqual(hits[0]['severity'], 'blocking')

    def test_inferred_ladders_only_warn(self):
        """散文推断出来的账本误报率实测约两成，只能记诊断，不能拦单。"""
        beats = _seacave_ladder()
        self.assertFalse(beats_declare_objects(beats))
        videos = {1: 'the worker lays floorboards across the bed'}
        hits = validate_video_objects(beats, videos)
        for h in hits:
            self.assertEqual(h['severity'], 'warning')

    def test_already_built_things_may_appear(self):
        """已经建成的东西当然可以在后续视频里出现——工人得站在上面干活。"""
        beats = _seacave_ladder()
        allowed = allowed_video_objects(beats, 5)
        self.assertIn('sub_base', allowed)
        self.assertIn('structural_frame', allowed)

    def test_screed_as_a_verb_is_not_an_object(self):
        """"distribute, screed, and tamp the gravel" 里的 screed 是动词。"""
        self.assertNotIn('screed', extract_objects(
            'he uses a rake to distribute, screed, and tamp the gravel'))
        self.assertIn('screed', extract_objects('a 40mm floor screed was poured'))


class TestOntologyRendering(unittest.TestCase):
    """Steps 1 & 5：角色本体与材质包。"""

    def test_beat_title_follows_the_material_not_the_mother(self):
        """母本叫 `erect timber portal`，变体用的是钢——名字必须跟着材料走。"""
        pack = build_pack({'material': '哑光黑耐候钢构件 + 黑色玄武岩打磨'})
        beat = {'operation': 'erect timber portal', 'state_after': 'three portal arches erected'}
        role = infer_role(beat)
        self.assertEqual(role, 'structure')
        title = render_beat_title(role, pack)
        self.assertNotIn('timber', title)
        self.assertIn('哑光黑耐候钢构件', title)

    def test_one_role_resolves_to_one_material_every_time(self):
        """同一句里既 slate 又 basalt，根因是同一构件被解析了两次、结果不同。"""
        pack = build_pack({'material': 'quarried basalt'})
        self.assertEqual(pack.resolve('paving'), pack.resolve('paving'))
        self.assertEqual(len({pack.resolve('paving') for _ in range(50)}), 1)

    def test_material_category_detection(self):
        self.assertEqual(detect_material_category('耐候钢与黄铜'), 'metal')
        self.assertEqual(detect_material_category('rammed earth and lime'), 'earth')
        self.assertEqual(detect_material_category('落叶松原木'), 'timber')
        self.assertEqual(detect_material_category(''), 'composite')

    def test_stage_outranks_body_prose(self):
        """stage='demolition' 的拆除拍，不该因为描写里一句 'soil ground' 被判成场地平整。"""
        beat = {'stage': 'demolition',
                'visible_action': 'craftsman lifts away the shack from soil ground'}
        self.assertEqual(infer_role(beat), 'demolition')

    def test_carrier_nouns_are_intercepted(self):
        """词典外的母本载体名词过去原样漏进变体。"""
        for leak in ('the rusted railcar interior', 'a scrapyard gravel ground',
                     'this camper utility zone', 'down to the blast doors'):
            self.assertEqual(scrub_carrier_leak(leak), '')
        clean = 'the riveted steel shell under the basalt overhang'
        self.assertEqual(scrub_carrier_leak(clean), clean)

    def test_fallback_prose_carries_the_new_axes(self):
        """兜底渲染是从零写的，但必须带上新材质——否则变体读起来和母本换了个说法。"""
        axes = {'material': '耐寒炭化实木', 'environment': '极地厚积雪冻土'}
        pack = build_pack(axes)
        out = render_beat_from_role({}, 'demolition', pack, axes)
        self.assertIn('耐寒炭化实木', out['visible_action'])
        self.assertIn('极地厚积雪冻土', out['visible_action'])


class TestTopologyIsomorphism(unittest.TestCase):
    """Step 6：冻因果拓扑，不冻拍数。"""

    def test_reordering_the_causal_chain_is_blocking(self):
        source = [
            {'index': 1, 'operation': 'erect structural frame'},
            {'index': 2, 'operation': 'sheathe exterior panel'},
        ]
        variant = [
            {'index': 1, 'operation': 'sheathe exterior panel'},
            {'index': 2, 'operation': 'erect structural frame'},
        ]
        issues = validate_isomorphism(source, variant)
        self.assertTrue(any(i['rule'] == 'topology' and i['severity'] == 'blocking'
                            for i in issues))

    def test_beat_count_is_elastic_when_the_caller_allows_it(self):
        source = [{'index': i, 'operation': op} for i, op in enumerate(
            ['strip out', 'lay sub-base', 'erect frame'], start=1)]
        variant = [{'index': i, 'operation': op} for i, op in enumerate(
            ['strip out', 'lay sub-base', 'erect frame', 'coat protective sealer'], start=1)]
        self.assertTrue(any(i['rule'] == 'beat_count' for i in validate_isomorphism(source, variant)))
        self.assertFalse(any(i['rule'] == 'beat_count' for i in
                             validate_isomorphism(source, variant, beat_tolerance=1)))

    def test_a_role_absent_from_the_variant_is_not_an_inversion(self):
        """不同材质工序数天然不同，缺席不算倒置。"""
        source = [{'index': 1, 'operation': 'pack insulation'},
                  {'index': 2, 'operation': 'lay finish floor'}]
        variant = [{'index': 1, 'operation': 'lay finish floor'}]
        self.assertFalse(any(i['rule'] == 'topology'
                             for i in validate_isomorphism(source, variant, beat_tolerance=1)))

    def test_chain_dedupes_consecutive_roles(self):
        beats = [{'operation': 'erect frame'}, {'operation': 'brace the portal frame'},
                 {'operation': 'lay finish floor'}]
        self.assertEqual(build_topology(beats)['chain'], ['structure', 'flooring'])


class TestAnchorGeometry(unittest.TestCase):
    """Step 4：占比随机位重算，不再复制。"""

    REF = 'wide 20mm lens feel, camera height 2.6m looking down thirty degrees'

    def test_a_longer_lens_means_a_larger_share_of_frame(self):
        cur = 'normal 35mm lens feel, camera height 1.6m facing squarely toward the portal'
        projected = reproject_scale(33, parse_camera(self.REF), parse_camera(cur))
        self.assertIsNotNone(projected)
        self.assertGreater(projected, 50)

    def test_same_lens_keeps_the_stored_value(self):
        same = 'wide 20mm lens feel, camera height 1.6m looking squarely onto the portal'
        self.assertEqual(reproject_scale(33, parse_camera(self.REF), parse_camera(same)), 33)

    def test_unreadable_focal_leaves_the_value_alone(self):
        """猜出来的焦段比不改更糟——读不出就不动。"""
        self.assertIsNone(reproject_scale(33, parse_camera(self.REF), parse_camera('a static shot')))

    def test_compound_pitch_words_are_not_truncated(self):
        """'thirty-five' 曾被 'thirty' 先吃掉。"""
        self.assertEqual(parse_camera('looking down thirty-five degrees')['pitch_deg'], 35.0)
        self.assertEqual(parse_camera('looking down twenty-five degrees')['pitch_deg'], 25.0)

    def test_aggregate_depth_is_not_read_as_a_focal_length(self):
        self.assertIsNone(parse_camera('a 70mm deep layer of basalt aggregate')['focal_mm'])

    def test_worker_scale_varies_with_the_lens(self):
        wide = cast_scale_hint('wide 20mm lens feel, camera height 2.6m')
        tele = cast_scale_hint('normal 35mm lens feel, camera height 1.6m')
        self.assertTrue(wide and tele)
        self.assertNotEqual(wide, tele)

    def test_worker_scale_is_empty_without_a_focal(self):
        self.assertEqual(cast_scale_hint('a static locked-off shot'), '')


class TestVariantGateIntegration(unittest.TestCase):
    """Step 2 的接线：变体在 spec 层就被拦下，此时还没有任何图像开销。"""

    def _baseline(self, beats):
        return {'pipeline_id': 'job_test', 'video_duration_sec': 30.0, 'beats': beats}

    def test_strict_variant_raises_on_an_unclosed_ledger(self):
        beats = [
            {'id': 'B01', 'index': 1, 'stage': 'structural',
             'state_after': 'window glazed into the wall', 'operation': 'glaze window'},
        ]
        with self.assertRaises(ValueError) as ctx:
            generate_orthogonal_variant(self._baseline(beats), preset='polar')
        self.assertIn('物件账', str(ctx.exception))

    def test_non_strict_reports_instead_of_raising(self):
        beats = [
            {'id': 'B01', 'index': 1, 'stage': 'structural',
             'state_after': 'window glazed into the wall', 'operation': 'glaze window'},
        ]
        doc = generate_orthogonal_variant(self._baseline(beats), preset='polar', strict=False)
        self.assertTrue(doc['ledger_violations'])
        self.assertTrue(doc['validation'])

    def test_variant_carries_its_material_pack(self):
        beats = [{'id': 'B01', 'index': 1, 'stage': 'demolition',
                  'state_after': 'shell stripped out', 'operation': 'strip out'}]
        doc = generate_orthogonal_variant(self._baseline(beats), preset='volcano', strict=False)
        self.assertIn('material_pack', doc)
        self.assertIn('roles', doc['material_pack'])

    def test_beat_titles_are_rendered_not_inherited(self):
        beats = [{'id': 'B01', 'index': 1, 'stage': 'structural',
                  'operation': 'erect timber portal',
                  'state_after': 'portal arches erected'}]
        doc = generate_orthogonal_variant(
            self._baseline(beats), mutation_axes={'material': '耐候钢'}, strict=False)
        self.assertNotIn('timber', doc['beats'][0]['operation'])


if __name__ == '__main__':
    unittest.main()
