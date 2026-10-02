# -*- coding: utf-8 -*-
"""Shared construction ledger, material ontology, and anchor geometry checks."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from prompt_pipeline.ontology import (
    UNKNOWN_ROLE, build_pack, infer_role, render_beat_title, detect_material_category,
    detect_asmr_bucket, MaterialPack,
)
from prompt_pipeline.object_ledger import (
    build_object_ledger, validate_object_ledger, validate_video_objects,
    allowed_video_objects, extract_objects, beats_declare_objects, annotate_beats,
)
from prompt_pipeline.anchor_geometry import (
    parse_camera, reproject_scale, cast_screen_percent, cast_scale_hint,
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

    def test_the_three_retrospective_defects_are_blocking(self):
        """证据自相矛盾的两类判死：凭空继承、完工回归。"""
        for v in self.violations:
            if v['rule'] in ('phantom', 'regression'):
                self.assertEqual(v['severity'], 'blocking', v['message'])

    def test_role_order_conventions_only_warn(self):
        """角色依赖表写的是常规工序顺序，不是物理定律——它不该拦交付。

        先立架后做排水垫层的井屋、先挂舱门后开窗洞的舱体都真实存在。仓库里唯一那份
        已交付母本（17 拍）就被这张表判死过三条，没有一条是复盘里的硬伤。
        """
        beats = [
            {'index': 1, 'state_after': 'structural frame erected'},
            {'index': 2, 'state_after': 'compacted aggregate sub-base laid'},
        ]
        inversions = [v for v in validate_object_ledger(beats) if v['rule'] == 'inversion']
        self.assertTrue(inversions)
        for v in inversions:
            self.assertEqual(v['severity'], 'warning', v['message'])

    def test_a_defect_the_mother_already_had_does_not_block_the_variant(self):
        """闸判的是变异弄坏了什么，不是这条梯子标不标准。

        母本自己就有的缺口会被 1:1 的骨架原样继承下来；拿它拦变体，拦的是母本的旧账。
        实测：仓库里那份 17 拍的已交付母本，不给基线时变体被判死三条，全部是继承来的。
        """
        alone = validate_object_ledger(self.beats)
        self.assertTrue([v for v in alone if v['severity'] == 'blocking'])

        against_itself = validate_object_ledger(self.beats, baseline_beats=self.beats)
        self.assertEqual([v for v in against_itself if v['severity'] == 'blocking'], [])
        self.assertTrue(all(v.get('inherited') for v in against_itself
                            if v['rule'] in ('phantom', 'regression')))

    def test_a_defect_the_variant_introduced_still_blocks(self):
        """母本干净、变体新造出来的缺口，照拦。"""
        clean = [
            {'index': 1, 'state_after': 'site cleared of debris', 'operation': 'strip out'},
            {'index': 2, 'state_after': 'compacted aggregate sub-base laid', 'operation': 'lay sub-base'},
        ]
        dirty = clean + [{'index': 3, 'state_before': 'twin windows already glazed',
                          'state_after': 'interior swept', 'operation': 'threshold'}]
        hits = validate_object_ledger(dirty, baseline_beats=clean)
        self.assertTrue([v for v in hits
                         if v['rule'] == 'phantom' and v['object'] == 'window'
                         and v['severity'] == 'blocking'])


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




class TestRoleInference(unittest.TestCase):
    """role 是母本与变体之间唯一被继承的东西，它判错，变体就建错东西。"""

    def test_operation_outranks_the_rest_of_the_headline(self):
        """一拍申报的活，压过画面里还有什么。

        改动前几个标题字段被拼成一段话一次过读，于是 `operation='hero reveal'` 的收尾拍
        因为 visual_subject 里提了一句窗被判成装窗，`furnish upper chamber` 因为画面里
        有炉子被判成取暖。实测母本 17 拍里这样错了 7 拍。
        """
        self.assertEqual(infer_role(
            {'operation': 'hero reveal', 'visual_subject': 'the window wall glows'}), 'hero')
        self.assertEqual(infer_role(
            {'operation': 'furnish upper chamber', 'visual_subject': 'stove and flue behind'}),
            'furnishing')
        self.assertEqual(infer_role(
            {'operation': 'plank floor', 'visual_subject': 'vapour barrier still visible'}),
            'flooring')

    def test_roof_is_a_role_of_its_own(self):
        """`shingle conical roof` 从前被判成防潮层——词表里根本没有屋面这个角色。"""
        self.assertEqual(infer_role({'operation': 'shingle conical roof'}), 'roof')
        pack = build_pack({'material': '耐候钢'})
        self.assertIn('roof', render_beat_title('roof', pack))

    def test_unreadable_beats_say_so_instead_of_defaulting_to_structure(self):
        """默认成骨架的代价：一条涂层拍被兜底渲染成钢门架，静默建错东西。"""
        self.assertEqual(infer_role({'operation': 'zzz qqq', 'stage': 'zzz'}), UNKNOWN_ROLE)
        notes = annotate_beats([{'index': 1, 'operation': 'zzz qqq'}])
        self.assertTrue([n for n in notes if n['rule'] == 'unknown_role'])


class TestBeatAnnotation(unittest.TestCase):
    """role 与物件申报在 Pass B 落盘时就地登记。"""

    def test_annotation_fills_missing_fields_only(self):
        beats = [{'index': 1, 'operation': 'erect frame',
                  'state_after': 'structural frame erected'},
                 {'index': 2, 'role': 'coating', 'operation': 'coat facade',
                  'produced_objects': ['protective_coating']}]
        annotate_beats(beats)
        self.assertEqual(beats[0]['role'], 'structure')
        self.assertIn('structural_frame', beats[0]['produced_objects'])
        # 已有的值一律不动：模型填的、人手改的，都比这里推出来的可信。
        self.assertEqual(beats[1]['role'], 'coating')
        self.assertEqual(beats[1]['produced_objects'], ['protective_coating'])

    def test_inferred_declarations_do_not_upgrade_the_ledger_to_blocking(self):
        """补出来的申报是正则从散文里捞的，不能拿去拦交付。

        实测推断模式误报率约两成（全部来自同一构件在图与视频里叫法不同）。只数
        「字段在不在」的话，登记一跑，母本线就"升级"成申报式，那两成误报直接变成
        拦单理由。
        """
        beats = [{'index': 1, 'state_after': 'structural frame erected'}]
        annotate_beats(beats)
        self.assertIn('produced_objects', beats[0])
        self.assertEqual(beats[0]['objects_source'], 'inferred')
        self.assertFalse(beats_declare_objects(beats))

        beats[0]['objects_source'] = 'declared'
        self.assertTrue(beats_declare_objects(beats))

    def test_a_declared_role_name_counts_as_its_objects(self):
        """申报写 role 名、账本比对 canonical id，两个命名空间必须打通。"""
        beats = [{'index': 1, 'produced_objects': ['structure'], 'objects_source': 'declared'},
                 {'index': 2, 'produced_objects': [], 'objects_source': 'declared'}]
        self.assertIn('structural_frame', allowed_video_objects(beats, 1))

    def test_a_same_role_naming_mismatch_warns_instead_of_blocking(self):
        """申报模式下同角色的另一种叫法只记 warning，不判死。

        改动前角色层兜底只在推断模式下生效，于是申报模式一遇到叫法不同就直接拦交付
        ——那正是推断模式量出两成误报的同一件事，只是换了个地方发生。视频说
        "cast-iron stove"、这一拍申报的是 flue_pipe，两者同属取暖工序。
        """
        beats = [{'index': 1, 'role': 'demolition', 'produced_objects': ['demolition'],
                  'objects_source': 'declared'},
                 {'index': 2, 'role': 'heating', 'produced_objects': ['flue_pipe'],
                  'objects_source': 'declared'}]
        hits = validate_video_objects(
            beats, {1: 'the worker seats the cast-iron stove on its pad'})
        self.assertTrue(hits)
        self.assertEqual(hits[0]['object'], 'stove')
        self.assertEqual(hits[0]['severity'], 'warning')

    def test_an_out_of_role_phantom_still_blocks(self):
        """角色也对不上的，照拦——VIDEO 1 在一条破拆拍上铺木地板就是这么抓住的。"""
        beats = [{'index': 1, 'role': 'demolition', 'produced_objects': ['demolition'],
                  'objects_source': 'declared'},
                 {'index': 2, 'role': 'demolition', 'produced_objects': ['demolition'],
                  'objects_source': 'declared'}]
        hits = validate_video_objects(
            beats, {1: 'the worker lays floorboards across the bed'})
        self.assertTrue(hits)
        self.assertEqual(hits[0]['object'], 'finish_floor')
        self.assertEqual(hits[0]['severity'], 'blocking')


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

    def test_aggregate_depth_is_not_read_as_a_focal_length(self):
        self.assertIsNone(parse_camera('a 70mm deep layer of basalt aggregate')['focal_mm'])

    def test_worker_scale_varies_with_the_lens(self):
        wide = cast_scale_hint('wide 20mm lens feel, camera height 2.6m')
        tele = cast_scale_hint('normal 35mm lens feel, camera height 1.6m')
        self.assertTrue(wide and tele)
        self.assertNotEqual(wide, tele)

    def test_worker_scale_is_empty_without_a_focal(self):
        self.assertEqual(cast_scale_hint('a static locked-off shot'), '')




if __name__ == '__main__':
    unittest.main()
