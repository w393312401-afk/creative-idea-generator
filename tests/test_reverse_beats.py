"""反推模式（prompt_pipeline.reverse）的校验器与变异器契约。

这些测试盯的是三条会静默失效、且失效后成片才看得出来的不变量：
  1. 反注入 —— Pass A 的调用链上不能出现任何主题/简报；
  2. 事件覆盖 —— 原片里每一次真实可见的变化恰好被一拍认领；
  3. 变异同构 —— 二创改内容不改骨架，且不把旧载体的施工顺序抄过去。
"""

import json
import os
import tempfile
import unittest
from unittest.mock import patch

import prompt_pipeline as pp
from prompt_pipeline import reverse


def _overview(events=('E01', 'E02'), frames=None):
    frames = frames if frames is not None else [
        {'index': 1, 'timestamp': 0.5, 'frame_path': '/job/review_frames/review_001.png'},
        {'index': 2, 'timestamp': 3.0, 'frame_path': '/job/review_frames/review_002.png'},
        {'index': 3, 'timestamp': 7.0, 'frame_path': '/job/review_frames/review_003.png'},
    ]
    return {
        'media_metadata': {'duration_sec': 10.0},
        'change_events': [
            {'event_id': eid, 'start': 1.0, 'peak': 2.0, 'end': 3.0,
             'evidence_frames': ['review_002.png']}
            for eid in events
        ],
        'review_sampling': {'frames': frames},
        'scenes': [],
    }


def _beat(bid, start, end, stage='structural', events=(), frames=('review_002.png',), **kw):
    beat = {
        'id': bid, 'start': start, 'end': end, 'stage': stage,
        'operation': kw.pop('operation', 'generic work'),
        'visual_subject': 'a wall',
        'visible_details': ['bare plaster'],
        'visible_action': 'a worker trowels the surface',
        'visible_result': 'the left half is coated',
        'state_before': 'left half bare plaster, right half bare plaster',
        'state_after': 'left half coated, right half bare plaster',
        # 合规的默认值：合成器硬闸要求每拍 2~3 道耦合工序、至少两条遗留痕迹。
        # 夹具本身不合规的话，「这条阶梯是干净的」这类断言就名不副实。
        'package_operations': ['mix', 'trowel', 'smooth'],
        'persistent_traces': ['trowel ridges', 'splatter flecks on the floor'],
        'workers_present': True,
        'source_event_ids': list(events),
        'evidence_frames': list(frames),
    }
    beat.update(kw)
    return beat


class TestEventCoverage(unittest.TestCase):
    """每个 change_event 必须且只能被一拍认领。"""

    def test_unbound_event_is_an_error(self):
        doc = {'video_duration_sec': 10.0, 'banned_elements': [],
               'beats': [_beat('B01', 0.0, 5.0, events=['E01'])]}
        codes = [v['code'] for v in reverse.validate_beats(doc, _overview(), schema=None)]
        self.assertIn('event_unbound', codes)

    def test_double_bound_event_is_an_error(self):
        doc = {'video_duration_sec': 10.0, 'banned_elements': [], 'beats': [
            _beat('B01', 0.0, 5.0, events=['E01', 'E02']),
            _beat('B02', 5.0, 10.0, events=['E02']),
        ]}
        codes = [v['code'] for v in reverse.validate_beats(doc, _overview(), schema=None)]
        self.assertIn('event_double_bound', codes)

    def test_full_coverage_is_clean(self):
        doc = {'video_duration_sec': 10.0, 'banned_elements': [], 'beats': [
            _beat('B01', 0.0, 5.0, events=['E01']),
            _beat('B02', 5.0, 10.0, stage='surface', events=['E02'],
                  frames=['review_003.png']),
        ]}
        errors = [v for v in reverse.validate_beats(doc, _overview(), schema=None)
                  if v['level'] == 'error']
        self.assertEqual(errors, [])

    def test_claiming_an_unknown_event_is_an_error(self):
        doc = {'video_duration_sec': 10.0, 'banned_elements': [], 'beats': [
            _beat('B01', 0.0, 5.0, events=['E01', 'E99']),
            _beat('B02', 5.0, 10.0, events=['E02'], frames=['review_003.png']),
        ]}
        codes = [v['code'] for v in reverse.validate_beats(doc, _overview(), schema=None)]
        self.assertIn('event_unknown', codes)


class TestEvidenceFrames(unittest.TestCase):
    def test_missing_evidence_frame_is_an_error(self):
        doc = {'video_duration_sec': 10.0, 'banned_elements': [],
               'beats': [_beat('B01', 0.0, 10.0, events=['E01', 'E02'], frames=[])]}
        codes = [v['code'] for v in reverse.validate_beats(doc, _overview(), schema=None)]
        self.assertIn('no_evidence', codes)

    def test_nonexistent_evidence_frame_is_an_error(self):
        doc = {'video_duration_sec': 10.0, 'banned_elements': [],
               'beats': [_beat('B01', 0.0, 10.0, events=['E01', 'E02'],
                               frames=['review_999.png'])]}
        codes = [v['code'] for v in reverse.validate_beats(doc, _overview(), schema=None)]
        self.assertIn('evidence_missing', codes)

    def test_evidence_outside_the_beat_window_only_warns(self):
        """时间戳落在窗外通常是拍窗划歪了，不是致命伤——高亮给人看，别打回重跑。"""
        doc = {'video_duration_sec': 10.0, 'banned_elements': [],
               'beats': [_beat('B01', 0.0, 1.0, events=['E01', 'E02'],
                               frames=['review_003.png'])]}
        found = [v for v in reverse.validate_beats(doc, _overview(), schema=None)
                 if v['code'] == 'evidence_out_of_window']
        self.assertTrue(found)
        self.assertEqual(found[0]['level'], 'warn')


class TestVariantEvidenceFrames(unittest.TestCase):
    """变体的证据帧已被 _merge_variant 改名成 reference_frames（构图参考，不再是事实断言）。

    校验器不知道这件事的话，每一拍都会同时报「缺少字段 evidence_frames」和「没有证据帧」——
    2026-08-12 一条 12 拍的干净变体阶梯就是这样凑出 24 项硬伤、在合成卡点上被判死的。
    """

    def _variant(self, **kw):
        beats = []
        for beat in (_beat('B01', 0.0, 5.0, events=['E01']),
                     _beat('B02', 5.0, 10.0, stage='surface', events=['E02'],
                           frames=['review_003.png'])):
            beat['reference_frames'] = beat.pop('evidence_frames')
            beats.append(beat)
        doc = {'video_duration_sec': 10.0, 'banned_elements': [], 'beats': beats,
               'variant_of': 'replica_src', 'mutation_axes': ['carrier']}
        doc.update(kw)
        return doc

    def test_a_clean_variant_has_no_errors(self):
        errors = [v for v in reverse.validate_beats(self._variant(), _overview())
                  if v['level'] == 'error']
        self.assertEqual(errors, [], errors)

    def test_a_nonexistent_reference_frame_only_warns(self):
        """变体不对原片的事实负责，缺一张构图参考不该拦住合成。"""
        doc = self._variant()
        doc['beats'][0]['reference_frames'] = ['review_999.png']
        found = [v for v in reverse.validate_beats(doc, _overview())
                 if v['code'] == 'evidence_missing']
        self.assertTrue(found)
        self.assertEqual(found[0]['level'], 'warn')
        self.assertIn('参考帧', found[0]['message'])

    def test_the_source_ladder_is_still_held_to_evidence(self):
        """豁免只对变体生效——原片阶梯少了证据帧仍然是硬伤。"""
        doc = {'video_duration_sec': 10.0, 'banned_elements': [],
               'beats': [_beat('B01', 0.0, 10.0, events=['E01', 'E02'], frames=[])]}
        codes = [v['code'] for v in reverse.validate_beats(doc, _overview())]
        self.assertIn('no_evidence', codes)
        self.assertTrue(any(c == 'missing_beat_field' for c in codes))


class TestConstructionOrder(unittest.TestCase):
    def test_stage_regression_is_an_error(self):
        """地面收尾之后又回去做结构：跨三级的倒挂，真的不可能。"""
        doc = {'video_duration_sec': 10.0, 'banned_elements': [], 'beats': [
            _beat('B01', 0.0, 5.0, stage='floor', events=['E01']),
            _beat('B02', 5.0, 10.0, stage='structural', events=['E02'],
                  frames=['review_003.png']),
        ]}
        codes = [v['code'] for v in reverse.validate_beats(doc, _overview(), schema=None)]
        self.assertIn('stage_regression', codes)

    def test_small_stage_backtrack_is_tolerated(self):
        """真实改造会来回穿插一两级，只有跨三级以上的倒挂才判。"""
        doc = {'video_duration_sec': 10.0, 'banned_elements': [], 'beats': [
            _beat('B01', 0.0, 5.0, stage='floor', events=['E01']),
            _beat('B02', 5.0, 10.0, stage='surface', events=['E02'],
                  frames=['review_003.png']),
        ]}
        errors = [v for v in reverse.validate_beats(doc, _overview(), schema=None)
                  if v['level'] == 'error']
        self.assertEqual(errors, [])

    def test_floor_before_wall_enclosure_is_not_a_regression(self):
        """先铺地板再立室内隔墙——自建里常见，画面里也拍得清清楚楚。

        容差从 ±1 放到 ±2 就是被这条真片子逼出来的（2026-08-13 木屋自建）：±1 会判
        它逆行，于是「如实标注」比「标错」更容易被判死，逼着人把墙体龙骨记成面层去
        骗过校验器。校验器把人推向说谎，比漏判更坏。
        """
        doc = {'video_duration_sec': 15.0, 'banned_elements': [], 'beats': [
            _beat('B01', 0.0, 5.0, stage='floor', events=['E01']),
            _beat('B02', 5.0, 10.0, stage='enclosure', events=['E02'],
                  frames=['review_003.png']),
            _beat('B03', 10.0, 15.0, stage='surface', frames=['review_003.png']),
        ]}
        errors = [v for v in reverse.validate_beats(doc, _overview(), schema=None)
                  if v['level'] == 'error']
        self.assertEqual(errors, [])

    def test_rough_in_after_enclosure_beats_the_backtrack_tolerance(self):
        """布线在封板之后是硬否决榜首，而它只差一级——不能被容差放过。"""
        doc = {'video_duration_sec': 10.0, 'banned_elements': [], 'beats': [
            _beat('B01', 0.0, 5.0, stage='enclosure', events=['E01']),
            _beat('B02', 5.0, 10.0, stage='rough_in', events=['E02'],
                  frames=['review_003.png']),
        ]}
        violations = reverse.validate_beats(doc, _overview(), schema=None)
        hit = next(v for v in violations if v['code'] == 'rough_in_after_enclosure')
        self.assertEqual(hit['level'], 'error')
        self.assertEqual(hit['beat_id'], 'B02')

    def test_rough_in_after_any_covering_stage_is_an_error(self):
        """遮盖层不止封板。面层、地面收尾做完再走线，一样要拆开刚做完的东西。

        这条以前是靠通用容差兜住的（surface=5 → rough_in=3 跨两级）。容差放宽到 ±2
        之后它会漏网，所以判据必须收在这条不吃容差的硬否决里，而不是靠容差的副作用。
        """
        for cover in ('surface', 'floor'):
            with self.subTest(cover=cover):
                doc = {'video_duration_sec': 10.0, 'banned_elements': [], 'beats': [
                    _beat('B01', 0.0, 5.0, stage=cover, events=['E01']),
                    _beat('B02', 5.0, 10.0, stage='rough_in', events=['E02'],
                          frames=['review_003.png']),
                ]}
                violations = reverse.validate_beats(doc, _overview(), schema=None)
                hit = next(v for v in violations if v['code'] == 'rough_in_after_enclosure')
                self.assertEqual(hit['level'], 'error')
                self.assertEqual(hit['beat_id'], 'B02')

    def test_power_chain_broken_when_fixtures_have_no_rough_in(self):
        doc = {'video_duration_sec': 10.0, 'banned_elements': [], 'beats': [
            _beat('B01', 0.0, 5.0, stage='demolition', events=['E01']),
            _beat('B02', 5.0, 10.0, stage='fixtures', events=['E02'],
                  frames=['review_003.png'], operation='installing chandelier and wall sconces'),
        ]}
        codes = [v['code'] for v in reverse.validate_beats(doc, _overview(), schema=None)]
        self.assertIn('power_chain_broken', codes)

    def test_solar_panel_fixture_without_rough_in_is_allowed(self):
        """太阳能光伏/电池/离网独立供电设备无需隐蔽布线拍，不误报 power_chain_broken。"""
        doc = {'video_duration_sec': 10.0, 'banned_elements': [], 'beats': [
            _beat('B01', 0.0, 5.0, stage='structural', events=['E01']),
            _beat('B02', 5.0, 10.0, stage='fixtures', events=['E02'],
                  frames=['review_003.png'], operation='solar panel mounting on rock ledge',
                  subject='mounting a monocrystalline solar photovoltaic panel'),
        ]}
        codes = [v['code'] for v in reverse.validate_beats(doc, _overview(), schema=None)]
        self.assertNotIn('power_chain_broken', codes)

    def test_non_electrical_fixtures_without_rough_in_allowed(self):
        """纯橱柜/水槽/实木门架等非通电设备无需隐蔽布线拍，不误报 power_chain_broken。"""
        doc = {'video_duration_sec': 10.0, 'banned_elements': [], 'beats': [
            _beat('B01', 0.0, 5.0, stage='enclosure', events=['E01']),
            _beat('B02', 5.0, 10.0, stage='fixtures', events=['E02'],
                  frames=['review_003.png'], operation='cabinetry_and_plumbing_install',
                  subject='Worker leveling sink base cabinet, installing countertop and ceramic sink',
                  action='mounts apron sink, solid timber countertop, and hangs solid pine door',
                  details=['solid pine batten door', 'ceramic apron-front sink', 'timber countertop']),
        ]}
        codes = [v['code'] for v in reverse.validate_beats(doc, _overview(), schema=None)]
        self.assertNotIn('power_chain_broken', codes)

    def test_lantern_and_battery_lighting_without_rough_in_allowed(self):
        """马灯/燃油灯/电池灯串等独立照明设备无需隐蔽布线拍，不误报 power_chain_broken。"""
        doc = {'video_duration_sec': 10.0, 'banned_elements': [], 'beats': [
            _beat('B01', 0.0, 5.0, stage='enclosure', events=['E01']),
            _beat('B02', 5.0, 10.0, stage='fixtures', events=['E02'],
                  frames=['review_003.png'], operation='lantern and fairy lights setup',
                  subject='hanging a metal hurricane lantern and battery-powered micro-LED string lights'),
        ]}
        codes = [v['code'] for v in reverse.validate_beats(doc, _overview(), schema=None)]
        self.assertNotIn('power_chain_broken', codes)

    def test_autofix_power_chain_mechanically_adds_banned_wiring(self):
        """autofix_beats 面对包含 power_chain_broken 错误的阶梯，机械层自动补齐 banned_elements 并通过校验。"""
        overview = _overview(('E01', 'E02'))
        doc = {
            'video_duration_sec': 10.0,
            'banned_elements': ['concrete mixer'],
            'scene_signature': 'rustic cabin',
            'beats': [
                _beat('B01', 0.0, 5.0, stage='surface', events=['E01']),
                _beat('B02', 5.0, 10.0, stage='fixtures', events=['E02'],
                      frames=['review_003.png'], operation='installing hardwired ceiling downlights and wall sconces'),
            ]
        }
        # 初始有 power_chain_broken
        violations = reverse.validate_beats(doc, overview)
        self.assertIn('power_chain_broken', [v['code'] for v in violations])

        # autofix 自动治愈
        fixed_doc, count = reverse.autofix_beats({}, doc, overview)
        self.assertGreaterEqual(count, 1)
        self.assertNotIn('power_chain_broken', [v['code'] for v in fixed_doc.get('validation') or []])
        self.assertTrue(any('wiring' in x.lower() for x in fixed_doc.get('banned_elements') or []))

    def test_multi_space_construction_order_no_false_regression(self):
        """室外完成（surface/fixtures）后进入室内从龙骨/封板开始施工，按空间隔离不误判阶段逆行。"""
        doc = {'video_duration_sec': 25.0, 'banned_elements': [], 'beats': [
            _beat('B01', 0.0, 5.0, stage='structural', events=['E01'], space='outdoor'),
            _beat('B02', 5.0, 10.0, stage='surface', events=['E02'], space='outdoor'),
            _beat('B03', 10.0, 12.0, stage='transition', operation='threshold', package=['threshold'], space='indoor'),
            _beat('B04', 12.0, 17.0, stage='enclosure', events=['E03'], space='indoor', frames=['review_003.png']),
            _beat('B05', 17.0, 22.0, stage='floor', space='indoor', frames=['review_003.png']),
            _beat('B06', 22.0, 25.0, stage='furnishing', space='indoor', frames=['review_003.png']),
        ]}
        errors = [v for v in reverse.validate_beats(doc, _overview(('E01', 'E02', 'E03')), schema=None)
                  if v['level'] == 'error']
        self.assertEqual(errors, [])

    def test_multi_space_rough_in_after_different_space_cover_valid(self):
        """室外封板（B01）不应阻拦室内（B03）的隐蔽工程。"""
        doc = {'video_duration_sec': 15.0, 'banned_elements': [], 'beats': [
            _beat('B01', 0.0, 5.0, stage='enclosure', events=['E01'], space='outdoor'),
            _beat('B02', 5.0, 7.0, stage='transition', operation='threshold', package=['threshold'], space='indoor'),
            _beat('B03', 7.0, 12.0, stage='rough_in', events=['E02'], space='indoor', frames=['review_003.png']),
        ]}
        errors = [v for v in reverse.validate_beats(doc, _overview(), schema=None)
                  if v['level'] == 'error']
        self.assertEqual(errors, [])

    def test_transition_beat_package_operations_valid(self):
        """过门拍 package_operations 只有 1 项（如 ['threshold']）或为空时不报 missing_beat_field。"""
        doc = {'video_duration_sec': 10.0, 'banned_elements': [], 'beats': [
            _beat('B01', 0.0, 5.0, stage='structural', events=['E01'], space='outdoor'),
            _beat('B02', 5.0, 10.0, stage='transition', operation='threshold', package=['threshold'], space='indoor'),
        ]}
        schema, _ = reverse._load_schema_or_reason()
        violations = reverse._validate_required_fields(doc, doc['beats'], schema)
        missing_errors = [v for v in violations if v['code'] == 'missing_beat_field']
        self.assertEqual(missing_errors, [])

    def test_real_world_multi_space_long_job_passes_validation(self):
        """验证实际长视频任务（22拍，包含室外/室内/睡眠凹室多个空间与太阳能设备）在新校验器下无硬伤。"""
        job_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'outputs', 'replica_jobs', 'replica_9898f9e639ab')
        beats_path = os.path.join(job_dir, 'timelapse_beats.json')
        overview_path = os.path.join(job_dir, 'video_overview.json')
        if os.path.exists(beats_path) and os.path.exists(overview_path):
            with open(beats_path, 'r', encoding='utf-8') as f:
                beats_doc = json.load(f)
            with open(overview_path, 'r', encoding='utf-8') as f:
                overview = json.load(f)
            violations = reverse.validate_beats(beats_doc, overview)
            errors = [v for v in violations if v.get('level') == 'error']
            self.assertEqual(errors, [])

    def test_finish_coat_before_primer_warns(self):
        doc = {'video_duration_sec': 10.0, 'banned_elements': [], 'beats': [
            _beat('B01', 0.0, 5.0, stage='surface', events=['E01'],
                  operation='rolling the finish coat'),
            _beat('B02', 5.0, 10.0, stage='surface', events=['E02'],
                  frames=['review_003.png'], operation='rolling primer'),
        ]}
        codes = [v['code'] for v in reverse.validate_beats(doc, _overview(), schema=None)]
        self.assertIn('finish_before_primer', codes)


class TestTimeAxis(unittest.TestCase):
    def test_overlapping_windows_are_an_error(self):
        doc = {'video_duration_sec': 10.0, 'banned_elements': [], 'beats': [
            _beat('B01', 0.0, 6.0, events=['E01']),
            _beat('B02', 4.0, 10.0, events=['E02'], frames=['review_003.png']),
        ]}
        codes = [v['code'] for v in reverse.validate_beats(doc, _overview(), schema=None)]
        self.assertIn('window_overlap', codes)

    def test_renumber_sorts_and_renames(self):
        doc = {'beats': [_beat('B07', 5.0, 9.0), _beat('beat_x', 0.0, 5.0)]}
        reverse._renumber_beats(doc)
        self.assertEqual([b['id'] for b in doc['beats']], ['B01', 'B02'])
        self.assertEqual(doc['beats'][0]['start'], 0.0)


class TestSchemaBackedFieldValidation(unittest.TestCase):
    """字段清单从 schema 文件读，不在校验器里再抄一份。"""

    def test_schema_file_is_readable_and_drives_required_fields(self):
        schema = reverse._load_schema()
        self.assertIsNotNone(schema, 'timelapse-beats.schema.json 应当能从技能包里读到')
        required = (schema.get('definitions') or {}).get('beat', {}).get('required') or []
        self.assertIn('source_event_ids', required)

        beat = _beat('B01', 0.0, 10.0, events=['E01', 'E02'])
        beat.pop('state_after')
        doc = {'video_duration_sec': 10.0, 'banned_elements': [], 'beats': [beat]}
        messages = [v['message'] for v in reverse.validate_beats(doc, _overview())]
        self.assertTrue(any('state_after' in m for m in messages))

    def test_an_unreadable_schema_is_reported_not_silently_skipped(self):
        """静默降级会让 UI 显示「已通过全部机械校验」，而字段校验其实压根没跑——
        用户据此以为阶梯是干净的。降级本身必须出现在 validation 列表里。"""
        doc = {'video_duration_sec': 10.0, 'banned_elements': [],
               'beats': [_beat('B01', 0.0, 10.0, events=['E01', 'E02'])]}
        with patch.object(reverse, '_schema_path', return_value='/nonexistent/schema.json'):
            violations = reverse.validate_beats(doc, _overview())
        hit = next(v for v in violations if v['code'] == 'schema_unavailable')
        self.assertEqual(hit['level'], 'warn')
        self.assertIn('跳过', hit['message'])

    def test_an_explicit_schema_argument_reports_nothing(self):
        """调用方自己给了 schema（或明确要求跳过）时不该冒出这条告警。"""
        doc = {'video_duration_sec': 10.0, 'banned_elements': [],
               'beats': [_beat('B01', 0.0, 10.0, events=['E01', 'E02'])]}
        codes = [v['code'] for v in reverse.validate_beats(doc, _overview(), schema={})]
        self.assertNotIn('schema_unavailable', codes)

    def test_empty_source_event_ids_is_legitimate(self):
        """一拍可以不认领任何变化事件（安静窗口，或整条视频压根没检出事件）。
        真正的覆盖不变量由 _validate_event_coverage 管，不该在字段校验里一刀切。"""
        beat = _beat('B01', 0.0, 10.0, events=[])
        doc = {'video_duration_sec': 10.0, 'banned_elements': [], 'beats': [beat]}
        overview = _overview(events=())
        messages = [v['message'] for v in reverse.validate_beats(doc, overview)]
        self.assertFalse(any('source_event_ids' in m for m in messages), messages)

    def test_empty_evidence_frames_is_still_rejected(self):
        """schema 给 evidence_frames 写了 minItems 1——空值严格度由 schema 决定。"""
        beat = _beat('B01', 0.0, 10.0, events=['E01', 'E02'], frames=[])
        doc = {'video_duration_sec': 10.0, 'banned_elements': [], 'beats': [beat]}
        messages = [v['message'] for v in reverse.validate_beats(doc, _overview())]
        self.assertTrue(any('evidence_frames' in m for m in messages))

    def test_blank_state_before_is_rejected(self):
        beat = _beat('B01', 0.0, 10.0, events=['E01', 'E02'], state_before='   ')
        doc = {'video_duration_sec': 10.0, 'banned_elements': [], 'beats': [beat]}
        messages = [v['message'] for v in reverse.validate_beats(doc, _overview())]
        self.assertTrue(any('state_before' in m for m in messages))

    def test_workers_present_false_is_not_treated_as_missing(self):
        """workers_present=False 是合法值，还是 IMAGE 锚点候选的判据——不能被空值判定吃掉。"""
        beat = _beat('B01', 0.0, 10.0, events=['E01', 'E02'], workers_present=False)
        doc = {'video_duration_sec': 10.0, 'banned_elements': [], 'beats': [beat]}
        messages = [v['message'] for v in reverse.validate_beats(doc, _overview())]
        self.assertFalse(any('workers_present' in m for m in messages))


class TestAntiPriming(unittest.TestCase):
    """Pass A 的调用链上不能出现任何主题/简报。破了这条，模型就开始脑补工序。"""

    def test_config_is_scrubbed_down_to_gateway_keys(self):
        dirty = {
            'baseUrl': 'http://x/v1', 'apiKey': 'k', 'model': 'm',
            'theme': '废弃石屋改造成隐藏工作室',
            '_project_key': 'replica_abc',
            'beat_outline': [{'text': '拆除屋顶'}],
            'dimensions': {'theme': 'leak'},
        }
        clean = reverse._scrub_config_for_pass_a(dirty)
        self.assertEqual(set(clean), {'baseUrl', 'apiKey', 'model'})
        self.assertNotIn('废弃石屋', json.dumps(clean, ensure_ascii=False))

    def test_extract_frame_facts_has_no_parameter_that_can_carry_a_brief(self):
        """签名即防护。加形参之前先想清楚：这是反注入唯一的结构性保证。

        `scope` 是送审档位枚举（REVIEW_SCOPES 里那三个字符串之一，认不出的一律回落到
        默认档），装不下主题/简报，所以它进签名不破坏这条不变量。
        """
        import inspect
        params = set(inspect.signature(reverse.extract_frame_facts).parameters)
        self.assertEqual(params,
                         {'config', 'job_dir', 'on_progress', 'degraded', 'batch_size', 'scope'})
        for banned in ('dimensions', 'brief', 'title', 'theme', 'kwargs'):
            self.assertNotIn(banned, params)

    def test_pass_a_system_prompt_forbids_inference(self):
        self.assertIn('If you cannot see it, it does not exist', reverse._PASS_A_SYSTEM)


class TestPassAParsing(unittest.TestCase):
    def test_frame_names_fall_back_to_position_when_model_renames_them(self):
        raw = json.dumps([
            {'frame': 'frame 1', 'subject': 'wall', 'workers_present': True, 'confidence': 0.9},
            {'frame': 'review_002.png', 'subject': 'floor', 'workers_present': False},
        ])
        out = reverse._parse_facts_array(raw, ['review_001.png', 'review_002.png'])
        self.assertEqual(set(out), {'review_001.png', 'review_002.png'})
        self.assertEqual(out['review_001.png']['subject'], 'wall')
        # 缺省 confidence 落到 0.5，而不是抛错或当成 0。
        self.assertEqual(out['review_002.png']['confidence'], 0.5)

    def test_extra_items_beyond_the_batch_are_dropped(self):
        raw = json.dumps([{'frame': 'x'}, {'frame': 'y'}, {'frame': 'z'}])
        out = reverse._parse_facts_array(raw, ['review_001.png'])
        self.assertEqual(list(out), ['review_001.png'])


class TestDegradedPlan(unittest.TestCase):
    def test_degraded_plan_keeps_event_evidence_plus_endpoints(self):
        overview = _overview()
        names = [f['frame'] if 'frame' in f else f['frame_path']
                 for f in reverse.degraded_plan_frames(overview)]
        names = [n.rsplit('/', 1)[-1] for n in names]
        self.assertEqual(set(names), {'review_001.png', 'review_002.png', 'review_003.png'})

    def test_cost_estimate_reports_both_modes(self):
        est = reverse.estimate_pass_a_cost(_overview())
        self.assertEqual(est['frame_count'], 3)
        self.assertEqual(est['batch_count'], 1)
        self.assertFalse(est['degraded'])
        self.assertEqual(est['scope'], 'plan')


class TestReviewScope(unittest.TestCase):
    """送审档位：analysis_plan 是硬下界，不是上界。

    长片走 adaptive 时计划档只挑约四成，于是「抽了四百帧、只识别了一百来张」——
    'all' 档存在的全部意义就是把这个上界打开。
    """

    def _overview_with_a_narrow_plan(self):
        overview = _overview()
        overview['analysis_plan'] = {
            'mode': 'adaptive',
            'required_frames': ['review_001.png'],
            'required_count': 1,
        }
        return overview

    def test_plan_scope_obeys_the_analysis_plan(self):
        frames = reverse.scope_frames(self._overview_with_a_narrow_plan(), 'plan')
        self.assertEqual([os.path.basename(f['frame_path']) for f in frames],
                         ['review_001.png'])

    def test_all_scope_sends_every_extracted_frame(self):
        frames = reverse.scope_frames(self._overview_with_a_narrow_plan(), 'all')
        self.assertEqual([os.path.basename(f['frame_path']) for f in frames],
                         ['review_001.png', 'review_002.png', 'review_003.png'])

    def test_all_scope_costs_more_than_the_plan_and_says_so(self):
        overview = self._overview_with_a_narrow_plan()
        plan = reverse.estimate_pass_a_cost(overview, scope='plan')
        every = reverse.estimate_pass_a_cost(overview, scope='all')
        self.assertEqual(plan['frame_count'], 1)
        self.assertEqual(every['frame_count'], 3)
        self.assertEqual(every['scope'], 'all')
        self.assertFalse(every['degraded'])

    def test_the_old_degraded_boolean_still_selects_the_degraded_scope(self):
        """磁盘上的老 job 状态与老前端只会传 degraded，不能因为多了档位就失效。"""
        self.assertEqual(reverse.normalize_review_scope(None, True), 'degraded')
        self.assertEqual(reverse.normalize_review_scope(None, False), 'plan')
        # 认不出来的档位名回落到默认档，而不是静默送全部帧（那是花钱的方向）。
        self.assertEqual(reverse.normalize_review_scope('everything', False), 'plan')
        # 两者都给时以 scope 为准。
        self.assertEqual(reverse.normalize_review_scope('all', True), 'all')


class TestFactsCacheIsKeyedByModel(unittest.TestCase):
    """帧事实缓存按模型分桶。

    键只有帧名的话，把逐帧识别换成强模型之后 Pass A 会原样命中弱模型留下的读数：
    用户付了强模型的价、拿到的还是上一轮的结论，页面上没有任何迹象。这正是「加了
    模型选择却等于没加」的形态。
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_another_model_does_not_inherit_the_first_models_readings(self):
        reverse._save_facts_cache(self.dir, 'flash', {'review_001.png': {'subject': 'wall'}})
        self.assertEqual(reverse._load_facts_cache(self.dir, 'flash'),
                         {'review_001.png': {'subject': 'wall'}})
        self.assertEqual(reverse._load_facts_cache(self.dir, 'pro'), {})

    def test_switching_back_is_still_free(self):
        reverse._save_facts_cache(self.dir, 'flash', {'review_001.png': {'subject': 'wall'}})
        reverse._save_facts_cache(self.dir, 'pro', {'review_001.png': {'subject': 'a wall'}})
        self.assertEqual(reverse._load_facts_cache(self.dir, 'flash')['review_001.png']['subject'],
                         'wall')

    def test_a_new_prompt_version_still_invalidates_everything(self):
        reverse._save_facts_cache(self.dir, 'flash', {'review_001.png': {}})
        path = reverse._facts_cache_path(self.dir)
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        data['prompt_version'] = 'v0'
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f)
        self.assertEqual(reverse._load_facts_cache(self.dir, 'flash'), {})

    def test_a_legacy_cache_is_claimed_by_the_model_that_wrote_the_facts(self):
        """升级当天正在跑的任务不该白付一次 Pass A：老格式没记模型，但磁盘上的
        frame_facts.json 记了，那份产物就是这批缓存生出来的。"""
        with open(reverse._facts_cache_path(self.dir), 'w', encoding='utf-8') as f:
            json.dump({'prompt_version': reverse.PASS_A_PROMPT_VERSION,
                       'frames': {'review_001.png': {'subject': 'wall'}}}, f)
        with open(os.path.join(self.dir, 'frame_facts.json'), 'w', encoding='utf-8') as f:
            json.dump({'model': 'flash', 'facts': []}, f)
        self.assertEqual(reverse._load_facts_cache(self.dir, 'flash'),
                         {'review_001.png': {'subject': 'wall'}})
        self.assertEqual(reverse._load_facts_cache(self.dir, 'pro'), {})

    def test_a_legacy_cache_with_no_traceable_model_is_dropped(self):
        """认不出是谁读的就只能丢：错认一个模型就是上面那个 bug 本身。"""
        with open(reverse._facts_cache_path(self.dir), 'w', encoding='utf-8') as f:
            json.dump({'prompt_version': reverse.PASS_A_PROMPT_VERSION,
                       'frames': {'review_001.png': {'subject': 'wall'}}}, f)
        self.assertEqual(reverse._load_facts_cache(self.dir, 'flash'), {})


class TestPassAModelSelection(unittest.TestCase):
    """反推段的模型选择：UI 上选了什么，必须真的送到调用里。"""

    def test_frame_facts_model_overrides_the_review_and_main_model(self):
        self.assertEqual(
            reverse._pass_a_model({'frameFactsModel': 'gemini-3.1-pro-high',
                                   'reviewModel': 'r', 'model': 'm'}),
            'gemini-3.1-pro-high')
        self.assertEqual(reverse._pass_a_model({'reviewModel': 'r', 'model': 'm'}), 'r')
        self.assertEqual(reverse._pass_a_model({'model': 'm'}), 'm')

    def test_model_keys_survive_the_anti_priming_scrub(self):
        """剥 config 是为了挡主题，不是挡模型名——剥掉了选择器就等于没接上。"""
        clean = reverse._scrub_config_for_pass_a({
            'frameFactsModel': 'gemini-3.1-pro-high', 'peakVerifyModel': 'off',
            'dimensions': {'theme': 'leak'},
        })
        self.assertEqual(clean, {'frameFactsModel': 'gemini-3.1-pro-high',
                                 'peakVerifyModel': 'off'})

    def test_peak_verify_can_be_switched_off_and_follows_the_main_model_by_default(self):
        self.assertIsNone(reverse._peak_verify_model({'model': 'm', 'peakVerifyModel': 'off'}))
        self.assertEqual(reverse._peak_verify_model({'model': 'm'}), 'm')
        self.assertEqual(reverse._peak_verify_model({'model': 'm', 'peakVerifyModel': ''}), 'm')
        self.assertEqual(
            reverse._peak_verify_model({'model': 'm', 'peakVerifyModel': 'claude-opus-4-6-thinking'}),
            'claude-opus-4-6-thinking')


class TestTemporaryObjects(unittest.TestCase):
    """2026-08-09 实测：合成器的清场预检打回了一条 20 拍的反推阶梯
    （"Beat 19 enters the furnishing phase but temporary construction objects are still
    present: ['grey floor tarp']"）。那个闸拦得对，但用户是在花掉一次合成调用之后才知道——
    人工卡点就该提前说。
    """

    def _ladder(self, **furnishing_kw):
        detail_beat = _beat('B01', 0.0, 5.0, stage='surface', events=['E01'],
                            visible_details=['a grey floor tarp covers the boards'])
        furnishing = _beat('B02', 5.0, 10.0, stage='furnishing', events=['E02'],
                           frames=['review_003.png'], **furnishing_kw)
        return {'video_duration_sec': 10.0, 'banned_elements': [],
                'beats': [detail_beat, furnishing]}

    def test_lingering_tarp_is_flagged_before_the_user_spends_a_compose_call(self):
        violations = reverse.validate_beats(self._ladder(), _overview(), schema=None)
        hit = next(v for v in violations if v['code'] == 'temporary_object_lingering')
        self.assertEqual(hit['beat_id'], 'B02')
        self.assertIn('tarp', hit['message'])
        # 给出可照做的两个改法，而不是只报"有问题"。
        self.assertIn('清场', hit['message'])

    def test_it_is_a_warning_not_a_hard_error(self):
        """原片里那块布是真实观察，不是错误。判成 error 会把合法观察堵死，
        何况这里是文本启发式，与合成器自己的物体台账口径不完全一致。"""
        violations = reverse.validate_beats(self._ladder(), _overview(), schema=None)
        hit = next(v for v in violations if v['code'] == 'temporary_object_lingering')
        self.assertEqual(hit['level'], 'warn')

    def test_an_on_camera_cleanup_clears_the_flag(self):
        clean = self._ladder(visible_action='a worker rolls up the grey floor tarp and carries it out')
        codes = [v['code'] for v in reverse.validate_beats(clean, _overview(), schema=None)]
        self.assertNotIn('temporary_object_lingering', codes)

    def test_verb_inflections_are_recognised(self):
        """第一版把 `roll up` 写死，遇到 "rolls up" 就漏判，已经清过场的阶梯照样被标黄。

        节拍一律是英文的——Pass A / Pass B 的提示词是英文，模型也照英文回。
        """
        for action in ('a worker rolls up the grey floor tarp and carries it out',
                       'the grey floor tarp is folded and hauled away',
                       'workers remove the grey floor tarp',
                       'the grey floor tarp is taken away before the furniture arrives'):
            with self.subTest(action=action):
                doc = self._ladder(visible_action=action)
                codes = [v['code'] for v in reverse.validate_beats(doc, _overview(), schema=None)]
                self.assertNotIn('temporary_object_lingering', codes)

    def test_a_removal_that_never_names_the_object_does_not_clear_it(self):
        """销账要求清场动作点到那个物体本身。点不到就保留告警——
        宁可多问一句，也不要静默地把该报的问题销掉。"""
        doc = self._ladder(visible_action='工人卷起地面防护布并搬走')
        codes = [v['code'] for v in reverse.validate_beats(doc, _overview(), schema=None)]
        self.assertIn('temporary_object_lingering', codes)

    def test_an_unrelated_removal_does_not_clear_the_tarp(self):
        """销账按 cue 走，不按整词交集——后者会被 a / the 这类虚词撞上，
        把该报的问题静默销掉。"""
        doc = self._ladder(visible_action='a worker removes the last of the rubble')
        codes = [v['code'] for v in reverse.validate_beats(doc, _overview(), schema=None)]
        self.assertIn('temporary_object_lingering', codes)

    def test_no_furnishing_beat_means_no_flag(self):
        doc = {'video_duration_sec': 10.0, 'banned_elements': [], 'beats': [
            _beat('B01', 0.0, 5.0, stage='surface', events=['E01'],
                  visible_details=['a grey floor tarp covers the boards']),
            _beat('B02', 5.0, 10.0, stage='surface', events=['E02'], frames=['review_003.png']),
        ]}
        codes = [v['code'] for v in reverse.validate_beats(doc, _overview(), schema=None)]
        self.assertNotIn('temporary_object_lingering', codes)

    def test_the_cue_list_comes_from_scene_state_not_a_local_copy(self):
        """规则本体只有一份。这里 import 它，抄一份就是第二份契约，迟早漂移。"""
        from prompt_pipeline.scene_state import TEMPORARY_OBJECT_CUES
        self.assertIn('tarp', TEMPORARY_OBJECT_CUES)
        self.assertIn('scaffold', TEMPORARY_OBJECT_CUES)
        src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                'prompt_pipeline', 'reverse.py'), encoding='utf-8').read()
        self.assertIn('from .scene_state import TEMPORARY_OBJECT_CUES', src)
        self.assertNotIn('"scaffolding"', src)


class TestJsonReplyParsing(unittest.TestCase):
    """2026-08-09 线上故障：Pass B 连续两次死在 `Expecting ',' delimiter`。

    让模型写描述性字段，它迟早会在字符串里留一个没转义的引号或裸换行。整批回复因此
    作废，几十次视觉调用的成果一起陪葬。
    """

    def test_clean_json_passes_through(self):
        self.assertEqual(reverse.parse_json_reply('{"a": 1}'), {'a': 1})

    def test_code_fences_are_stripped(self):
        self.assertEqual(reverse.parse_json_reply('```json\n{"a": 1}\n```'), {'a': 1})

    def test_unescaped_inner_quotes_are_repaired(self):
        raw = '{"visible_result": "the "before" wall is gone", "id": "B01"}'
        out = reverse.parse_json_reply(raw)
        self.assertEqual(out['visible_result'], 'the "before" wall is gone')
        self.assertEqual(out['id'], 'B01')

    def test_raw_newlines_inside_strings_are_repaired(self):
        raw = '{"state_after": "left half coated,\nright half bare"}'
        self.assertEqual(reverse.parse_json_reply(raw)['state_after'],
                         'left half coated,\nright half bare')

    def test_trailing_commas_are_repaired(self):
        self.assertEqual(reverse.parse_json_reply('{"beats": [1, 2,],}'), {'beats': [1, 2]})

    def test_escaped_quotes_are_left_alone(self):
        raw = '{"a": "he said \\"hi\\" once"}'
        self.assertEqual(reverse.parse_json_reply(raw)['a'], 'he said "hi" once')

    def test_apostrophes_and_colons_inside_strings_survive(self):
        raw = '{"a": "the worker\'s note: two-thirds done", "b": "x"}'
        out = reverse.parse_json_reply(raw)
        self.assertEqual(out['a'], "the worker's note: two-thirds done")

    def test_a_cut_off_reply_is_reported_as_truncation_not_a_format_error(self):
        """实测：21 拍的回复写到 32KB 撞上 max_tokens，报的是 `Expecting ',' delimiter`，
        看上去像格式问题，实际是输出太长。两种病、两种药，混在一起就永远修不对。"""
        cut = '{"beats": [{"id": "B01", "evidence_frames": ["review_001.png"]}, {"id": "B02"'
        with self.assertRaises(reverse.TruncatedReply):
            reverse.parse_json_reply(cut)

    def test_truncation_detection_ignores_brackets_inside_strings(self):
        self.assertFalse(reverse._looks_truncated('{"a": "a [bracket] and a {brace}"}'))
        self.assertTrue(reverse._looks_truncated('{"a": "unterminated'))

    def test_a_merely_malformed_reply_is_not_called_truncation(self):
        with self.assertRaises(ValueError) as ctx:
            reverse.parse_json_reply('{"a": 1 "b": 2}')
        self.assertNotIsInstance(ctx.exception, reverse.TruncatedReply)

    def test_pass_b_caps_the_evidence_frame_list(self):
        """每拍回一长串帧名是回复被截断的头号原因。"""
        self.assertIn('AT MOST THREE frames per beat', reverse._PASS_B_SYSTEM)

    def test_structurally_broken_reply_names_the_raw_text(self):
        """修不回来时，异常必须带上原始回复——上次故障日志里只有一句报错，
        没人知道模型到底写了什么。"""
        # 括号配平但内容坏掉 —— 与「被截断」是两回事，走的是另一条分支。
        with self.assertRaises(ValueError) as ctx:
            reverse.parse_json_reply('{"beats": [ this is not json at all ]}')
        self.assertIn('this is not json at all', str(ctx.exception))

    def test_a_truncated_reply_shows_where_it_stopped(self):
        cut = '{"beats": [{"id": "B01", "visible_action": "a worker fastens the last board"'
        with self.assertRaises(reverse.TruncatedReply) as ctx:
            reverse.parse_json_reply(cut)
        self.assertIn('fastens the last board', str(ctx.exception))

    def test_pass_a_array_survives_an_unescaped_quote(self):
        raw = ('[{"frame": "review_001.png", "subject": "a "clean" wall", '
               '"workers_present": false, "confidence": 0.9}]')
        out = reverse._parse_facts_array(raw, ['review_001.png'])
        self.assertEqual(out['review_001.png']['subject'], 'a "clean" wall')


class TestClusterRetryBudget(unittest.TestCase):
    """解析失败与校验未过是两笔独立预算。共用一个计数器的话，一次「模型忘了转义引号」
    就会吃掉本该留给结构回炉的那一轮。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        overview = {
            'source_video': '/x/clip.mp4',
            'media_metadata': {'duration_sec': 10.0},
            'change_events': [],
            'review_sampling': {'frames': [{'index': 1, 'timestamp': 2.0,
                                            'frame_path': '/x/review_001.png'}]},
            'scenes': [],
        }
        with open(os.path.join(self.tmp, 'video_overview.json'), 'w', encoding='utf-8') as f:
            json.dump(overview, f)

    def _good_reply(self):
        return json.dumps({'banned_elements': [], 'beats': [{
            'id': 'B01', 'start': 0.0, 'end': 10.0, 'stage': 'structural',
            'operation': 'work', 'visual_subject': 'wall', 'visible_details': ['bare'],
            'visible_action': 'works', 'visible_result': 'done',
            'state_before': 'left half bare', 'state_after': 'left half done',
            'package_operations': ['cut', 'fit', 'fasten'],
            'persistent_traces': ['dust film', 'screw head rows'], 'workers_present': True,
            'source_event_ids': [], 'evidence_frames': ['review_001.png'],
        }]})

    def test_a_broken_reply_is_retried_rather_than_killing_the_run(self):
        replies = ['{"beats": [ broken', self._good_reply()]
        with patch.object(pp, '_chat', side_effect=replies) as chat:
            doc = reverse.cluster_beats({}, self.tmp, facts_payload={'facts': []})
        self.assertEqual(chat.call_count, 2)
        self.assertEqual(len(doc['beats']), 1)

    def test_the_run_gives_up_after_the_parse_budget_is_spent(self):
        broken = ['{"beats": [ broken'] * (reverse._PARSE_RETRY_BUDGET + 1)
        with patch.object(pp, '_chat', side_effect=broken):
            with self.assertRaises(ValueError):
                reverse.cluster_beats({}, self.tmp, facts_payload={'facts': []})

    def test_a_bad_reply_is_dumped_for_debugging(self):
        replies = ['{"beats": [ broken', self._good_reply()]
        with patch.object(pp, '_chat', side_effect=replies):
            reverse.cluster_beats({}, self.tmp, facts_payload={'facts': []})
        dumps = [n for n in os.listdir(self.tmp) if n.startswith('.bad_reply_cluster_beats')]
        self.assertTrue(dumps, '解析失败的原始回复必须落盘，否则事后无从排查')

    def test_a_non_object_reply_is_rejected_outright(self):
        with patch.object(pp, '_chat', return_value='["not", "an", "object"]'):
            with self.assertRaises(ValueError) as ctx:
                reverse.cluster_beats({}, self.tmp, facts_payload={'facts': []})
        self.assertIn('list', str(ctx.exception))


class TestMutation(unittest.TestCase):
    """二创：改内容不改骨架。"""

    def _source(self):
        return {
            'video_duration_sec': 10.0,
            'pipeline_id': 'replica_src',
            'banned_elements': ['welding torch'],
            'beats': [
                _beat('B01', 0.0, 5.0, stage='demolition', events=['E01']),
                _beat('B02', 5.0, 10.0, stage='structural', events=['E02'],
                      frames=['review_003.png']),
            ],
        }

    def test_more_than_two_axes_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            reverse.mutate_beats({}, self._source(),
                                 {'axes': ['carrier', 'environment', 'material']})
        self.assertIn('选题发动机', str(ctx.exception))

    def test_no_axis_is_rejected(self):
        with self.assertRaises(ValueError):
            reverse.mutate_beats({}, self._source(), {'axes': []})

    def test_timing_skeleton_survives_the_model_trying_to_change_it(self):
        """模型回了改过的 id/start/end 也不算数——骨架以原文档为准。"""
        rogue = json.dumps({
            'banned_elements': ['brick footing'],
            'beats': [
                {'id': 'B01', 'start': 99.0, 'end': 199.0,
                 'visual_subject': 'a rusted bus shell',
                 'visible_action': 'a worker cuts out the corroded floor pan',
                 'source_event_ids': ['E77']},
                {'id': 'B02', 'visual_subject': 'the bus frame'},
            ],
        })
        with patch.object(pp, '_chat', return_value=rogue):
            variant = reverse.mutate_beats({}, self._source(),
                                           {'axes': ['carrier'], 'brief': '换成废弃巴士'})

        self.assertEqual([b['id'] for b in variant['beats']], ['B01', 'B02'])
        self.assertEqual([b['start'] for b in variant['beats']], [0.0, 5.0])
        self.assertEqual([b['end'] for b in variant['beats']], [5.0, 10.0])
        self.assertEqual(variant['beats'][0]['source_event_ids'], ['E01'])
        self.assertEqual([b['stage'] for b in variant['beats']], ['demolition', 'structural'])

    def test_content_fields_are_rewritten(self):
        rewritten = json.dumps({
            'banned_elements': ['brick footing'],
            'beats': [{'id': 'B01', 'visual_subject': 'a rusted bus shell',
                       'visible_action': 'a worker cuts out the corroded floor pan'}],
        })
        with patch.object(pp, '_chat', return_value=rewritten):
            variant = reverse.mutate_beats({}, self._source(), {'axes': ['carrier']})
        self.assertEqual(variant['beats'][0]['visual_subject'], 'a rusted bus shell')
        # 没被改写的拍保留原内容，不会被清空。
        self.assertEqual(variant['beats'][1]['visual_subject'], 'a wall')

    def test_evidence_frames_are_demoted_to_reference_frames(self):
        """变体不再对原片的事实负责：那些帧只提供机位与构图。"""
        with patch.object(pp, '_chat', return_value=json.dumps({'banned_elements': [], 'beats': []})):
            variant = reverse.mutate_beats({}, self._source(), {'axes': ['material']})
        for beat in variant['beats']:
            self.assertNotIn('evidence_frames', beat)
            self.assertTrue(beat['reference_frames'])

    def test_banned_elements_are_recomputed_not_inherited(self):
        """旧载体的「不存在物」对新载体毫无意义，继承它等于给新载体套错约束。"""
        with patch.object(pp, '_chat', return_value=json.dumps(
                {'banned_elements': ['brick footing'], 'beats': []})):
            variant = reverse.mutate_beats({}, self._source(), {'axes': ['carrier']})
        self.assertEqual(variant['banned_elements'], ['brick footing'])
        self.assertNotIn('welding torch', variant['banned_elements'])

    def test_variant_is_traceable_to_its_source(self):
        with patch.object(pp, '_chat', return_value=json.dumps({'banned_elements': [], 'beats': []})):
            variant = reverse.mutate_beats({}, self._source(), {'axes': ['reward']})
        self.assertEqual(variant['variant_of'], 'replica_src')
        self.assertEqual(variant['mutation_axes'], ['reward'])


class TestComposerContractAlignment(unittest.TestCase):
    """生产端（Pass B）与消费端（合成器 frame-state 硬闸）的契约必须对齐。

    这组测试是补上一个真实事故的缺口（2026-08-09）：Pass B 的提示词要求「一拍恰好一道
    工序」，而合成器硬闸要求「一拍 2~3 道」——生产端系统性产出消费端必须拒收的东西，
    五次合成尝试无一成功。此前所有合成测试都 patch 掉了 compose_*，因此没有一个测试
    跑过真实契约，这个矛盾就这么溜了过去。
    """

    def test_bounds_are_read_from_the_composer_not_hardcoded(self):
        import inspect
        from prompt_pipeline.frame_state import validate_frame_state_contract as fn
        params = inspect.signature(fn).parameters
        self.assertEqual(reverse._package_operation_bounds(),
                         (params['min_package_operations'].default,
                          params['max_package_operations'].default))

    def test_pass_b_prompt_does_not_demand_a_single_operation(self):
        """这句话正是事故的根因，别让它回来。"""
        self.assertNotIn('exactly ONE dominant physical operation', reverse._PASS_B_SYSTEM)
        self.assertIn('package_operations', reverse._PASS_B_SYSTEM)

    def test_a_single_operation_beat_is_rejected_at_the_gate(self):
        doc = {'video_duration_sec': 10.0, 'banned_elements': [], 'beats': [
            _beat('B01', 0.0, 10.0, events=['E01', 'E02'], package_operations=['fasten'])]}
        codes = [v['code'] for v in reverse.validate_beats(doc, _overview(), schema=None)]
        self.assertIn('package_operations_out_of_range', codes)

    def test_a_well_formed_milestone_beat_passes(self):
        doc = {'video_duration_sec': 10.0, 'banned_elements': [], 'beats': [
            _beat('B01', 0.0, 10.0, events=['E01', 'E02'],
                  package_operations=['cut', 'fit', 'fasten'],
                  persistent_traces=['saw dust ridges', 'screw head rows'])]}
        errors = [v for v in reverse.validate_beats(doc, _overview(), schema=None)
                  if v['level'] == 'error']
        self.assertEqual(errors, [])

    def test_fewer_than_two_traces_is_rejected(self):
        doc = {'video_duration_sec': 10.0, 'banned_elements': [], 'beats': [
            _beat('B01', 0.0, 10.0, events=['E01', 'E02'],
                  package_operations=['cut', 'fasten'], persistent_traces=['dust'])]}
        codes = [v['code'] for v in reverse.validate_beats(doc, _overview(), schema=None)]
        self.assertIn('too_few_traces', codes)

    def test_a_gate_clean_ladder_survives_the_composers_real_frame_state_gate(self):
        """端到端对齐：通过我这道闸的节拍，转成合成器的 ladder 形状后，
        必须能过合成器**真实的**硬闸。之前没有任何测试做过这件事。"""
        from prompt_pipeline.frame_state import (
            build_frame_state_contract, validate_frame_state_contract)

        beats = [
            _beat('B01', 0.0, 5.0, stage='demolition', events=['E01'],
                  package_operations=['pry', 'lift', 'carry out'],
                  persistent_traces=['pry gouges along the joists', 'grit trail to the doorway']),
            _beat('B02', 5.0, 10.0, stage='enclosure', events=['E02'],
                  frames=['review_003.png'],
                  package_operations=['cut', 'fit', 'fasten'],
                  persistent_traces=['saw dust ridges', 'screw head rows']),
        ]
        doc = {'video_duration_sec': 10.0, 'banned_elements': [], 'beats': beats}
        my_errors = [v for v in reverse.validate_beats(doc, _overview(), schema=None)
                     if v['level'] == 'error']
        self.assertEqual(my_errors, [], '先确认这条阶梯过了我这道闸')

        ladder = [{
            'index': i,
            'operation': b['operation'],
            'package_operations': b['package_operations'],
            'before_state': b['state_before'],
            'after_state': b['state_after'],
            'preserve_state': 'everything already built stays as it is',
            'changed_grid_cells': ['B2'],
            'persistent_traces': b['persistent_traces'],
            'milestone_name': b['visible_result'],
        } for i, b in enumerate(beats, start=1)]

        composer_errors = validate_frame_state_contract(build_frame_state_contract(ladder))
        package_or_trace = [e for e in composer_errors
                            if 'operations' in e or 'persistent traces' in e]
        self.assertEqual(package_or_trace, [],
                         f'合成器硬闸仍然拒收：{composer_errors}')

    def test_package_operations_reach_the_composer_outline(self):
        """不把工序写进清单，合成器只能从一句动作描述里猜，猜出一道工序就自己判死。"""
        doc = {'beats': [_beat('B01', 0.0, 5.0, package_operations=['cut', 'fit', 'fasten'])]}
        dims = reverse.beats_to_dimensions(doc)
        self.assertIn('cut', dims['beat_outline'][0]['text'])
        self.assertIn('fasten', dims['beat_outline'][0]['text'])


class TestHandoffToComposer(unittest.TestCase):
    def test_beat_outline_length_locks_the_beat_count(self):
        """beat_outline 非空会让 compose_anchor_and_packet 切进「清单一比一还原」，
        拍数锁死为清单长度。这是 1:1 复刻依赖的语义，长度对不上就不是复刻了。"""
        doc = {'banned_elements': ['scaffold'], 'beats': [
            _beat('B01', 0.0, 5.0), _beat('B02', 5.0, 10.0), _beat('B03', 10.0, 15.0)]}
        dims = reverse.beats_to_dimensions(doc)
        self.assertEqual(len(dims['beat_outline']), 3)
        self.assertTrue(all(entry['text'] for entry in dims['beat_outline']))
        self.assertEqual(dims['banned_elements'], ['scaffold'])
        self.assertTrue(dims['reverse_engineered'])

    def test_existing_theme_is_not_overwritten(self):
        doc = {'beats': [_beat('B01', 0.0, 5.0)]}
        dims = reverse.beats_to_dimensions(doc, {'theme': '用户自己写的主题'})
        self.assertEqual(dims['theme'], '用户自己写的主题')

    def test_the_fields_the_user_edits_actually_reach_the_composer(self):
        """人工卡点的存在理由。

        这条测试盯的是本模块最容易静默退化的一处：`beats_to_dimensions` 曾经只发
        text + op，于是用户在审核卡点上逐拍精修的起止状态、遗留痕迹、可见细节全部
        止步于这个函数——UI 摆出七个字段让人改，其中五个没有任何下游消费者。
        退回那个形态时，成片不会报错，只会悄悄不像原片。
        """
        doc = {'beats': [_beat(
            'B01', 0.0, 5.0,
            visible_details=['bare plaster', 'a taped seam'],
            persistent_traces=['trowel ridges', 'splatter flecks'],
            state_before='left two-thirds bare, right third primed',
            state_after='left two-thirds coated, right third primed')]}
        entry = reverse.beats_to_dimensions(doc)['beat_outline'][0]

        self.assertEqual(entry['mat'], ['bare plaster', 'a taped seam'])
        self.assertEqual(entry['trace'], 'trowel ridges; splatter flecks')
        self.assertEqual(entry['state_before'], 'left two-thirds bare, right third primed')
        self.assertEqual(entry['state_after'], 'left two-thirds coated, right third primed')

    def test_absent_rich_fields_are_omitted_rather_than_sent_empty(self):
        """空键会让 build_outline_plan_block 的 has_* 守卫误判，凭空长出一段
        「把 stage_scope 设成清单里的 scope」——规划器就会去编一个不存在的值。"""
        doc = {'beats': [_beat('B01', 0.0, 5.0, visible_details=[],
                               persistent_traces=[], state_before='', state_after='')]}
        entry = reverse.beats_to_dimensions(doc)['beat_outline'][0]
        for key in ('mat', 'trace', 'state_before', 'state_after'):
            self.assertNotIn(key, entry)

    def test_rich_fields_survive_the_composer_outline_normaliser(self):
        """绑定通路的另一端。这里直接调 prompt_pipeline 的归一化器与提示词段落生成，
        确认这几个键不会在半路被 `_outline_normalized_entries` 丢掉——两侧各自单测
        通过、中间那一跳却对不上，是这条链路最贵的失败方式。"""
        doc = {'beats': [_beat('B01', 0.0, 5.0,
                               state_before='left two-thirds bare',
                               state_after='left two-thirds coated')]}
        outline = reverse.beats_to_dimensions(doc)['beat_outline']

        normalized = pp._outline_normalized_entries(outline)
        self.assertEqual(normalized[0]['state_before'], 'left two-thirds bare')
        self.assertEqual(normalized[0]['state_after'], 'left two-thirds coated')

        _plan, block = pp.build_outline_plan_block(outline, len(outline))
        self.assertIn('STATE BEFORE: left two-thirds bare', block)
        self.assertIn('STATE AFTER: left two-thirds coated', block)
        # 光渲染出来还不够：没有那条绑定规则，模型会把它当背景资料而不是硬约束。
        self.assertIn('STATE PAIR', block)

    def test_a_plain_card_outline_gains_no_state_rule(self):
        """老卡片/手动主题不带这几个键，提示词里就不该出现这段规则。"""
        _plan, block = pp.build_outline_plan_block(
            [{'text': '拆除塌陷的屋顶板', 'op': 'demolition'}], 1)
        self.assertNotIn('STATE PAIR', block)

    def test_banned_hits_are_case_insensitive(self):
        hits = reverse.banned_element_hits(
            'A worker lifts a WELDING TORCH into frame.', ['welding torch', 'excavator'])
        self.assertEqual(hits, ['welding torch'])

    def test_no_banned_elements_means_no_hits(self):
        self.assertEqual(reverse.banned_element_hits('anything at all', []), [])

    def test_banned_hits_word_boundary_avoids_false_positives(self):
        """防止 oven 误杀 woven、bed 误杀 embedded、car 误杀 carpet 等假阳性。"""
        text = 'Worker arranging woven storage baskets, placing embedded LED fixtures on carpet, stripping tree bark.'
        banned = ['oven', 'bed', 'car', 'bar', 'pot', 'fan']
        self.assertEqual(reverse.banned_element_hits(text, banned), [])

        # 真实出现完整独立词时必须正常拦截
        real_violating_text = 'Worker installs an oven on the counter and platform bed.'
        self.assertEqual(sorted(reverse.banned_element_hits(real_violating_text, banned)), ['bed', 'oven'])

    def test_banned_hits_cjk_support(self):
        """中文禁用词支持自然匹配。"""
        text = '在厨房吧台角落安装了微波炉和电磁炉'
        self.assertEqual(reverse.banned_element_hits(text, ['微波炉', '洗碗机']), ['微波炉'])



class _SheetJob:
    """临时 job 目录 + 真实存在的帧文件。拼图那条路会 os.path.exists 每一帧。"""

    def __init__(self, count=5, missing=()):
        self.dir = tempfile.mkdtemp()
        self.frames = []
        for i in range(1, count + 1):
            name = f'review_{i:03d}.png'
            path = os.path.join(self.dir, name)
            if i not in missing:
                with open(path, 'wb') as f:
                    f.write(b'\x89PNG')
            self.frames.append({'index': i, 'timestamp': float(i), 'frame_path': path})
        self.overview = {
            'source_video': '/x/clip.mp4',
            'media_metadata': {'duration_sec': float(count)},
            'change_events': [],
            'review_sampling': {'frames': self.frames},
            'scenes': [],
        }
        with open(os.path.join(self.dir, 'video_overview.json'), 'w', encoding='utf-8') as f:
            json.dump(self.overview, f)

    def facts(self):
        return [{'frame': os.path.basename(e['frame_path']), 'timestamp': e['timestamp']}
                for e in self.frames]


def _fake_collage(frame_paths, output_path, columns=5, max_frames=25, tile_width=360):
    """替身拼图：写一个真文件出来并记下它拼了哪几帧、以及降采样开没开。

    签名必须跟 tools.collage.build_keyframe_collage 保持一致。替身少一个关键字参数、
    真实调用点多传一个就是 TypeError，而 build_pass_b_sheets 把异常吞成「退回纯文本
    聚类」——于是整组用例看到的是空拼图而不是报错，失败信息指向别处。
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(b'\xff\xd8\xff')
    _fake_collage.calls.append([p.name for p in frame_paths])
    _fake_collage.max_frames.append(max_frames)
    return output_path


class TestPassBSheets(unittest.TestCase):
    """Pass B 的视觉输入。

    原先 Pass B 只拿到 `_facts_digest` 那几十行文本去聚类——跨帧的东西（同一面墙的推进、
    痕迹还在不在）只活在像素里，文本里全丢了，模型只能靠文本相邻性猜里程碑边界。
    这一组盯的是拼图接上去之后最容易静默坏掉的地方：**格子与事实的对齐**。错位一格，
    模型会把 A 帧的画面当成 B 帧的事实读，比没有拼图坏得多。
    """

    def setUp(self):
        _fake_collage.calls = []
        _fake_collage.max_frames = []

    def test_sheets_never_let_the_collage_downsample(self):
        """拼图的下标契约不容许抽帧：第 k 格必须恰好是 FRAME FACTS 第 lo+k 条。

        build_keyframe_collage 默认 max_frames=25，会把超量输入抽稀成 25 张（那是给
        整片总览拼图用的）。这里必须显式传 0 关掉。page_size 目前是 20、撞不上 25
        只是巧合，谁把它调过 25，后面每一格都平移一位，而且是静默的。
        """
        job = _SheetJob(count=12)
        with patch('tools.collage.build_keyframe_collage', _fake_collage):
            reverse.build_pass_b_sheets(job.dir, job.overview, job.facts(), page_size=5)
        self.assertTrue(_fake_collage.max_frames)
        self.assertEqual(set(_fake_collage.max_frames), {0})

    def test_sheets_are_paged_and_carry_their_own_start_index(self):
        job = _SheetJob(count=12)
        with patch('tools.collage.build_keyframe_collage', _fake_collage):
            sheets = reverse.build_pass_b_sheets(job.dir, job.overview, job.facts(), page_size=5)
        self.assertEqual([s['start'] for s in sheets], [0, 5, 10])
        self.assertEqual([len(s['facts']) for s in sheets], [5, 5, 2])

    def test_a_missing_frame_breaks_the_page_instead_of_shifting_every_tile_after_it(self):
        """第 3 帧缺失。跳过它接上第 4 帧会让后面每一格都平移一位——那正是要防的。"""
        job = _SheetJob(count=6, missing=(3,))
        with patch('tools.collage.build_keyframe_collage', _fake_collage):
            sheets = reverse.build_pass_b_sheets(job.dir, job.overview, job.facts(), page_size=5)
        self.assertEqual([s['start'] for s in sheets], [0, 3])
        # 缺帧之后那一页的 start 必须是 3（真实下标），而不是 2（累加前几页的长度）。
        self.assertEqual([f['frame'] for f in sheets[1]['facts']],
                         ['review_004.png', 'review_005.png', 'review_006.png'])
        self.assertNotIn('review_003.png', sum(_fake_collage.calls, []))

    def test_the_layout_block_numbers_match_the_digest_numbering(self):
        job = _SheetJob(count=6, missing=(3,))
        facts = job.facts()
        with patch('tools.collage.build_keyframe_collage', _fake_collage):
            sheets = reverse.build_pass_b_sheets(job.dir, job.overview, facts, page_size=5)
        block = reverse._sheet_layout_block(sheets)
        digest = reverse._facts_digest(facts)
        # digest 的 #4 就是 review_004.png，layout 说第二张拼图从 #4 起——两处必须一致。
        self.assertIn('#4 [4.0s] review_004.png', digest)
        self.assertIn('SHEET 2: FRAME FACTS #4–#6', block)

    def test_no_ffmpeg_means_no_sheets_rather_than_a_half_set(self):
        job = _SheetJob(count=6)
        with patch('tools.collage.build_keyframe_collage', return_value=None):
            self.assertEqual(reverse.build_pass_b_sheets(job.dir, job.overview, job.facts()), [])

    def test_missing_frame_files_degrade_to_no_sheets(self):
        job = _SheetJob(count=3, missing=(1, 2, 3))
        self.assertEqual(reverse.build_pass_b_sheets(job.dir, job.overview, job.facts()), [])

    def _good_reply(self):
        return json.dumps({'banned_elements': [], 'beats': [{
            'id': 'B01', 'start': 0.0, 'end': 5.0, 'stage': 'structural',
            'operation': 'work', 'visual_subject': 'wall', 'visible_details': ['bare'],
            'visible_action': 'works', 'visible_result': 'done',
            'state_before': 'left half bare', 'state_after': 'left half done',
            'package_operations': ['cut', 'fit', 'fasten'],
            'persistent_traces': ['dust film', 'screw head rows'], 'workers_present': True,
            'source_event_ids': [], 'evidence_frames': ['review_001.png'],
        }]})

    def test_cluster_beats_sends_the_sheets_to_a_multimodal_call(self):
        job = _SheetJob(count=5)
        facts = {'facts': job.facts()}
        with patch('tools.collage.build_keyframe_collage', _fake_collage), \
                patch.object(pp, '_multimodal_chat', return_value=self._good_reply()) as mm, \
                patch.object(pp, '_chat') as text_chat:
            reverse.cluster_beats({'model': 'm'}, job.dir, facts_payload=facts)
        text_chat.assert_not_called()
        self.assertEqual(mm.call_count, 1)
        images = mm.call_args[0][3]
        self.assertTrue(images, '拼图存在时必须真的把图片传进去，否则这条改动等于没做')
        self.assertIn('CONTACT SHEETS', mm.call_args[0][2])

    def test_cluster_beats_still_runs_when_no_sheet_can_be_built(self):
        """拼图是增强，不是门禁。ffmpeg 缺失不该让反推跑不完。"""
        job = _SheetJob(count=3, missing=(1, 2, 3))
        with patch.object(pp, '_chat', return_value=self._good_reply()) as text_chat, \
                patch.object(pp, '_multimodal_chat') as mm:
            doc = reverse.cluster_beats({}, job.dir, facts_payload={'facts': job.facts()})
        mm.assert_not_called()
        self.assertEqual(text_chat.call_count, 1)
        self.assertEqual(len(doc['beats']), 1)

    def test_a_truncated_multimodal_reply_is_reworked_not_fatal(self):
        """多模态路径把截断报成异常，纯文本路径靠半截 JSON 认出来。两条路都得汇进
        「合并节拍、写短一点」的回炉分支——否则上了拼图之后一次截断就是整单失败。"""
        job = _SheetJob(count=5)
        replies = [pp.ResponseTruncated('cut off'), self._good_reply()]
        with patch('tools.collage.build_keyframe_collage', _fake_collage), \
                patch.object(pp, '_multimodal_chat', side_effect=replies) as mm:
            doc = reverse.cluster_beats({'model': 'm'}, job.dir,
                                        facts_payload={'facts': job.facts()})
        self.assertEqual(mm.call_count, 2)
        self.assertIn('CUT OFF', mm.call_args[0][2])
        self.assertEqual(len(doc['beats']), 1)


class TestPeakVerification(unittest.TestCase):
    """峰值帧复核默认开（2026-08-10）。

    原先默认关，省下几次调用的钱，代价是节拍边界——边界恰好落在 peak 帧上，那几帧被
    flash 读糊，整条阶梯就整体错位，后面所有合成调用都建在错的骨架上。
    """

    def setUp(self):
        self.job = _SheetJob(count=3)
        self.job.overview['change_events'] = [{
            'event_id': 'E01', 'start': 1.0, 'peak': 2.0, 'end': 3.0,
            'evidence_frames': ['review_001.png', 'review_002.png', 'review_003.png'],
        }]
        with open(os.path.join(self.job.dir, 'video_overview.json'), 'w', encoding='utf-8') as f:
            json.dump(self.job.overview, f)
        self.payload = {'facts': [dict(f, subject='blurry', confidence=0.3)
                                  for f in self.job.facts()]}

    def _reply(self):
        return json.dumps([{'frame': 'review_002.png', 'subject': 'a boarded ceiling',
                            'confidence': 0.9}])

    def test_it_runs_without_any_peak_verify_model_being_configured(self):
        with patch.object(pp, '_multimodal_chat', return_value=self._reply()) as mm:
            out = reverse.verify_peak_frames({'model': 'm-strong'}, self.job.dir, self.payload)
        self.assertEqual(mm.call_count, 1)
        self.assertEqual(mm.call_args.kwargs['model'], 'm-strong')
        refined = [f for f in out['facts'] if f['frame'] == 'review_002.png'][0]
        self.assertEqual(refined['subject'], 'a boarded ceiling')
        self.assertEqual(refined['verified_by'], 'm-strong')
        # 时间戳必须跟着复核结果走，否则这一帧在 Pass B 里落不进任何拍窗。
        self.assertEqual(refined['timestamp'], 2.0)

    def test_it_can_still_be_turned_off(self):
        for off in ('off', 'none', 'false', False):
            with patch.object(pp, '_multimodal_chat') as mm:
                reverse.verify_peak_frames({'model': 'm', 'peakVerifyModel': off},
                                           self.job.dir, self.payload)
            mm.assert_not_called()

    def test_a_failed_verification_leaves_pass_a_intact_instead_of_killing_the_job(self):
        """复核是增强，不是门禁。默认开之后这条尤其要紧——一次解析失败若能掀掉整单，
        等于把已经付过钱的 Pass A 一起赔进去。"""
        with patch.object(pp, '_multimodal_chat', side_effect=RuntimeError('gateway down')):
            out = reverse.verify_peak_frames({'model': 'm'}, self.job.dir, self.payload)
        self.assertEqual(len(out['facts']), 3)
        self.assertEqual(out['facts'][1]['subject'], 'blurry')

    def test_the_cost_estimate_reports_the_peak_calls_it_will_now_make(self):
        """默认加钱却不进预估，等于绕开了「先确认再烧钱」这道卡点本身。"""
        est = reverse.estimate_pass_a_cost(self.job.overview)
        self.assertEqual(est['peak_frame_count'], 1)
        self.assertEqual(est['peak_batch_count'], 1)


class TestSingleFrameSalvage(unittest.TestCase):
    """整批预算用尽后逐帧重试。

    多半是其中一两帧让模型写出了坏 JSON，整批作废会把另外八帧读得好好的观察一起赔进去
    ——缺帧在 Pass B 里表现为该窗口证据不足，节拍边界就落错。
    """

    def _fact(self, name):
        return json.dumps([{'frame': name, 'subject': 'wall', 'confidence': 0.8}])

    def test_a_doomed_batch_is_retried_frame_by_frame(self):
        job = _SheetJob(count=3)
        job.overview['analysis_plan'] = {
            'required_frames': [f'review_{i:03d}.png' for i in (1, 2, 3)]}
        with open(os.path.join(job.dir, 'video_overview.json'), 'w', encoding='utf-8') as f:
            json.dump(job.overview, f)

        broken = ['{ broken'] * (reverse._PARSE_RETRY_BUDGET + 1)
        # 逐帧重试里第二帧仍然坏，另外两帧救得回来。
        singles = [self._fact('review_001.png'), '{ still broken',
                   self._fact('review_003.png')]
        with patch.object(pp, '_multimodal_chat', side_effect=broken + singles) as mm:
            payload = reverse.extract_frame_facts({}, job.dir, batch_size=10)

        self.assertEqual(mm.call_count, len(broken) + 3)
        read = {f['frame']: f for f in payload['facts'] if f.get('subject')}
        self.assertEqual(set(read), {'review_001.png', 'review_003.png'})
        # 救不回来的那帧仍然占位，confidence 归零——Pass B 该看到「这帧没读到」，
        # 而不是这帧压根不存在。
        lost = [f for f in payload['facts'] if f['frame'] == 'review_002.png'][0]
        self.assertEqual(lost['confidence'], 0.0)

    def test_truncation_also_falls_through_to_the_per_frame_retry(self):
        """十帧的观察写不进 4096 tokens 是常事，解药和坏 JSON 一样是把这批拆小。"""
        job = _SheetJob(count=1)
        job.overview['analysis_plan'] = {'required_frames': ['review_001.png']}
        with open(os.path.join(job.dir, 'video_overview.json'), 'w', encoding='utf-8') as f:
            json.dump(job.overview, f)

        cut = [pp.ResponseTruncated('too long')] * (reverse._PARSE_RETRY_BUDGET + 1)
        with patch.object(pp, '_multimodal_chat', side_effect=cut) as mm:
            payload = reverse.extract_frame_facts({}, job.dir)
        # 单帧批次没有可拆的余地，用完预算就放弃——但不能把异常抛出去杀掉整单。
        self.assertEqual(mm.call_count, len(cut))
        self.assertEqual(payload['facts'][0]['confidence'], 0.0)


if __name__ == '__main__':
    unittest.main()


class TestBeatTranslation(unittest.TestCase):
    """中文对照：只为人工卡点服务，英文永远是唯一事实源。"""

    def _doc(self):
        return {'beats': [_beat('B01', 0.0, 5.0), _beat('B02', 5.0, 10.0)]}

    def test_translation_lands_in_zh_and_never_touches_the_english(self):
        reply = json.dumps({
            'B01': {'visual_subject': '一面墙', 'visible_action': '工人抹平墙面',
                    'persistent_traces': ['抹刀纹', '地面溅点']},
            'B02': {'visual_subject': '同一面墙'},
        })
        doc = self._doc()
        with patch.object(pp, '_chat', return_value=reply) as chat:
            count = reverse.translate_beats({}, doc)

        self.assertEqual(count, 2)
        self.assertEqual(chat.call_count, 1)
        self.assertEqual(doc['beats'][0]['zh']['visual_subject'], '一面墙')
        self.assertEqual(doc['beats'][0]['zh']['persistent_traces'], ['抹刀纹', '地面溅点'])
        # 英文原样不动——下游提示词、相位判定、banned 门禁读的都是它。
        self.assertEqual(doc['beats'][0]['visual_subject'], 'a wall')
        self.assertEqual(doc['beats'][0]['persistent_traces'],
                         ['trowel ridges', 'splatter flecks on the floor'])

    def test_a_length_mismatch_on_a_list_field_drops_that_field_only(self):
        """条数对不上的对照比没有对照更坏——核对的人会照着错位的那条点头。"""
        reply = json.dumps({'B01': {'visual_subject': '一面墙',
                                    'persistent_traces': ['只译了一条']}})
        doc = self._doc()
        with patch.object(pp, '_chat', return_value=reply):
            reverse.translate_beats({}, doc)
        self.assertEqual(doc['beats'][0]['zh'], {'visual_subject': '一面墙'})

    def test_a_failed_translation_is_never_fatal(self):
        doc = self._doc()
        with patch.object(pp, '_chat', side_effect=RuntimeError('gateway down')):
            self.assertEqual(reverse.translate_beats({}, doc), 0)
        self.assertNotIn('zh', doc['beats'][0])

    def test_editing_the_english_invalidates_only_that_field_translation(self):
        previous = self._doc()
        previous['beats'][0]['zh'] = {'visual_subject': '一面墙', 'visible_action': '工人抹平墙面'}
        edited = json.loads(json.dumps(previous))
        edited['beats'][0]['visible_action'] = 'a worker sands the surface instead'

        reverse.prune_stale_translations(previous, edited)
        self.assertEqual(edited['beats'][0]['zh'], {'visual_subject': '一面墙'})


class TestCoverageFrames(unittest.TestCase):
    """覆盖帧：拍窗越长越不能只靠那三张证据帧。

    这里盯的是一条会静默失效的不变量——覆盖帧是**派生**数据。用户在卡点上拆过拍、
    改过时间窗之后，如果它没跟着重算，卡片上就会出现「时间戳写着这一拍、画面是
    上一版拍窗」的帧：比没有覆盖帧更坏，因为人会照着它核对。
    """

    def _timeline_overview(self, step=0.5, duration=14.0):
        frames = []
        n = int(duration / step) + 1
        for i in range(n):
            frames.append({'index': i + 1, 'timestamp': round(i * step, 3),
                           'frame_path': f'/job/review_frames/review_{i + 1:03d}.png'})
        return _overview(frames=frames)

    def test_a_long_beat_gets_frames_across_its_whole_window(self):
        overview = self._timeline_overview()
        doc = {'video_duration_sec': 14.0, 'banned_elements': [],
               'beats': [_beat('B01', 3.6, 13.8, events=['E01', 'E02'])]}
        reverse.attach_coverage_frames(doc, overview)
        cov = doc['beats'][0]['coverage_frames']

        self.assertEqual(len(cov), reverse.COVERAGE_MAX_FRAMES)
        times = [c['timestamp'] for c in cov]
        self.assertEqual(times, sorted(times))
        # 证据帧最多三张，覆盖帧要铺满整段：首尾都得贴着拍窗边界。
        self.assertLessEqual(times[0], 4.1)
        self.assertGreaterEqual(times[-1], 13.3)
        # 最大空档不该比均分步长大太多，否则「不漏细节」是句空话。
        gaps = [b - a for a, b in zip(times, times[1:])]
        self.assertLess(max(gaps), 2.0)

    def test_a_short_beat_takes_every_frame_it_has(self):
        overview = self._timeline_overview()
        doc = {'video_duration_sec': 14.0, 'banned_elements': [],
               'beats': [_beat('B01', 0.0, 1.0, events=['E01', 'E02'])]}
        reverse.attach_coverage_frames(doc, overview)
        # 窗内只有 3 张（0/0.5/1.0）。抽帧密度是上限，这里造不出帧来。
        self.assertEqual([c['timestamp'] for c in doc['beats'][0]['coverage_frames']],
                         [0.0, 0.5, 1.0])

    def test_a_window_thinner_than_the_sampling_step_still_gets_a_frame(self):
        overview = self._timeline_overview(step=2.0)
        doc = {'video_duration_sec': 14.0, 'banned_elements': [],
               'beats': [_beat('B01', 5.1, 5.2, events=['E01', 'E02'])]}
        reverse.attach_coverage_frames(doc, overview)
        cov = doc['beats'][0]['coverage_frames']
        # 空着会被读成「这段没有画面」，而事实是这段没被单独抽到帧。
        self.assertEqual(len(cov), 1)
        self.assertEqual(cov[0]['timestamp'], 6.0)

    def test_editing_the_window_recomputes_coverage(self):
        overview = self._timeline_overview()
        doc = {'video_duration_sec': 14.0, 'banned_elements': [],
               'beats': [_beat('B01', 0.0, 14.0, events=['E01', 'E02'])]}
        reverse.attach_coverage_frames(doc, overview)
        stale = [c['frame'] for c in doc['beats'][0]['coverage_frames']]

        doc['beats'][0]['end'] = 3.0          # 用户在卡点上拆了一刀
        reverse.attach_coverage_frames(doc, overview)
        fresh = doc['beats'][0]['coverage_frames']
        self.assertNotEqual([c['frame'] for c in fresh], stale)
        self.assertTrue(all(c['timestamp'] <= 3.0 for c in fresh))

    def test_no_extracted_frames_means_no_coverage_field(self):
        doc = {'video_duration_sec': 14.0, 'banned_elements': [],
               'beats': [_beat('B01', 0.0, 3.0, coverage_frames=[{'frame': 'x.png',
                                                                 'timestamp': 1.0}])]}
        reverse.attach_coverage_frames(doc, _overview(frames=[]))
        # 留着上一版的覆盖帧等于指向一批可能已经不存在的文件。
        self.assertNotIn('coverage_frames', doc['beats'][0])

    def test_coverage_frames_do_not_disturb_validation(self):
        overview = self._timeline_overview()
        doc = {'video_duration_sec': 14.0, 'banned_elements': [], 'beats': [
            _beat('B01', 0.0, 7.0, events=['E01']),
            _beat('B02', 7.0, 14.0, events=['E02'], stage='enclosure'),
        ]}
        before = reverse.validate_beats(doc, overview)
        reverse.attach_coverage_frames(doc, overview)
        after = reverse.validate_beats(doc, overview)
        self.assertEqual([v['code'] for v in before], [v['code'] for v in after])


class TestKeyDrift(unittest.TestCase):
    """字段名漂移：内容都在，只是模型把键名写成了近义词。

    2026-08-13 的那一单，前五拍老实写 visible_action，从第六拍起整段漂成
    visual_action，于是校验器报十二条「缺少字段」——把用户挡在合成门外，去修一个
    根本不存在的缺失。这一组盯的是修法的边界：搬运可以，生成不行，覆盖更不行。
    """

    def _doc(self, **beat_kw):
        return {'video_duration_sec': 10.0, 'banned_elements': [],
                'beats': [_beat('B01', 0.0, 10.0, events=['E01', 'E02'], **beat_kw)]}

    def test_a_drifted_key_is_moved_back_and_stops_the_false_missing_field(self):
        doc = self._doc()
        beat = doc['beats'][0]
        beat['visual_action'] = beat.pop('visible_action')
        beat['visual_result'] = beat.pop('visible_result')
        codes = [v['code'] for v in reverse.validate_beats(doc, _overview())]
        self.assertIn('missing_beat_field', codes)   # 归一之前：假硬伤

        moved = reverse.normalize_beat_keys(doc)
        self.assertEqual({m['to'] for m in moved}, {'visible_action', 'visible_result'})
        self.assertNotIn('visual_action', beat)
        self.assertEqual(beat['visible_action'], 'a worker trowels the surface')
        errors = [v for v in reverse.validate_beats(doc, _overview()) if v['level'] == 'error']
        self.assertEqual(errors, [])

    def test_a_populated_canonical_key_is_never_overwritten(self):
        """两个名字都有值 = 模型写了两份说法，那是要看画面裁决的事，不能静默挑一份。"""
        doc = self._doc()
        beat = doc['beats'][0]
        beat['visual_action'] = 'a completely different claim'
        self.assertEqual(reverse.normalize_beat_keys(doc), [])
        self.assertEqual(beat['visible_action'], 'a worker trowels the surface')
        self.assertEqual(beat['visual_action'], 'a completely different claim')

    def test_an_empty_alias_never_displaces_a_missing_field(self):
        """错名字底下是空的，就还是「真的缺」。搬一个空值过去只是把硬伤藏起来。"""
        doc = self._doc()
        beat = doc['beats'][0]
        beat.pop('visible_action')
        beat['visual_action'] = ''
        self.assertEqual(reverse.normalize_beat_keys(doc), [])
        codes = [v['code'] for v in reverse.validate_beats(doc, _overview())]
        self.assertIn('missing_beat_field', codes)

    def test_the_repair_is_announced_not_silent(self):
        doc = self._doc()
        doc['beats'][0]['visual_action'] = doc['beats'][0].pop('visible_action')
        reverse.normalize_beat_keys(doc)
        warns = [v for v in reverse.validate_beats(doc, _overview())
                 if v['code'] == 'beat_keys_normalized']
        self.assertEqual(len(warns), 1)
        self.assertEqual(warns[0]['level'], 'warn')
        self.assertIn('visual_action→visible_action', warns[0]['message'])

    def test_a_clean_doc_never_gains_a_normalization_record(self):
        doc = self._doc()
        self.assertEqual(reverse.normalize_beat_keys(doc), [])
        self.assertNotIn('key_normalizations', doc)

    def test_the_record_survives_the_next_normalization_pass(self):
        """归一每次读状态、每次保存都会重跑，第二趟必然一处也搬不到。

        那一趟若把记录清掉，这条 warn 就只在用户看不见的那一瞬间存在过——修复重新
        变回静默的，而静默修复正是这条记录要防的事。
        """
        doc = self._doc()
        doc['beats'][0]['visual_action'] = doc['beats'][0].pop('visible_action')
        reverse.normalize_beat_keys(doc)
        self.assertEqual(reverse.normalize_beat_keys(doc), [])   # 第二趟：无事可搬

        warns = [v for v in reverse.validate_beats(doc, _overview())
                 if v['code'] == 'beat_keys_normalized']
        self.assertEqual(len(warns), 1)
        self.assertEqual(len(doc['key_normalizations']), 1)


class TestTimeWindows(unittest.TestCase):
    """定长时间窗：与节拍并列的第二套读法。

    节拍由模型按语义切，因此「某段时间被漏掉了」和「这段确实没事发生」在产物里长得
    一模一样。定长窗按固定 5 秒切、只统计画面增减，就是用来把这两者分开的。
    """

    def _facts(self, spec):
        """spec: [(timestamp, [可见物…]), …]"""
        return [{'frame': f'review_{i:03d}.png', 'timestamp': ts,
                 'materials': items, 'tools': [], 'traces': [],
                 'workers_present': True, 'completion_extent': f'state at {ts}s'}
                for i, (ts, items) in enumerate(spec, start=1)]

    def _dense(self, lo, hi, items, step=0.5):
        out, t = [], lo
        while t < hi - 1e-9:
            out.append((round(t, 3), list(items)))
            t += step
        return out

    def test_a_thing_that_shows_up_midway_is_reported_as_new(self):
        facts = self._facts(self._dense(0, 5, ['bare concrete floor'])
                            + self._dense(5, 10, ['bare concrete floor', 'yellow insulation batts']))
        w = reverse.analyze_time_windows(facts, 10.0)
        self.assertEqual(len(w), 2)
        self.assertIn('bare concrete floor', w[0]['baseline'])
        self.assertEqual(w[0]['appeared'], [])          # 第一窗没有「此前」
        self.assertIn('yellow insulation batts', w[1]['appeared'])

    def test_a_thing_seen_only_in_one_window_is_neither_new_nor_gone(self):
        """既报「新出现」又报「消失」是自相矛盾的一行，而它其实是最值得看的一类。"""
        facts = self._facts(self._dense(0, 5, ['floor'])
                            + self._dense(5, 10, ['floor', 'landscape rake'])
                            + self._dense(10, 15, ['floor']))
        w = reverse.analyze_time_windows(facts, 15.0)
        self.assertIn('landscape rake', w[1]['brief'])
        self.assertNotIn('landscape rake', w[1]['appeared'])
        self.assertNotIn('landscape rake', w[1]['vanished'])

    def test_one_phrase_never_lands_in_two_columns(self):
        """一条短语含多个词，不同的词会把它同时拉进两栏。跨栏去重，先到先得。"""
        facts = self._facts(self._dense(0, 5, ['gravel'])
                            + self._dense(5, 10, ['gravel', 'wide landscape rake'])
                            + self._dense(10, 15, ['gravel', 'wide landscape trim']))
        w = reverse.analyze_time_windows(facts, 15.0)
        for row in w:
            bag = row['baseline'] + row['appeared'] + row['vanished'] + row['brief']
            self.assertEqual(len(bag), len(set(x.lower() for x in bag)))

    def test_windows_tile_the_whole_video_including_a_short_tail(self):
        facts = self._facts(self._dense(0, 12, ['floor']))
        w = reverse.analyze_time_windows(facts, 12.0)
        self.assertEqual([(x['start'], x['end']) for x in w],
                         [(0.0, 5.0), (5.0, 10.0), (10.0, 12.0)])

    def test_no_facts_yields_no_windows_instead_of_a_crash(self):
        self.assertEqual(reverse.analyze_time_windows([], 10.0), [])
        self.assertEqual(reverse.analyze_time_windows(None, 0), [])

    def test_the_digest_drops_the_state_lines_when_there_are_too_many_windows(self):
        """摘要要喂进 Pass B，长视频不能让它把节拍聚类挤掉。"""
        facts = self._facts(self._dense(0, 300, ['floor']))
        w = reverse.analyze_time_windows(facts, 300.0)
        self.assertGreater(len(w), 48)
        self.assertNotIn('state at', reverse._windows_digest(w))
        self.assertIn('state at', reverse._windows_digest(w[:4]))


class TestUncoveredTime(unittest.TestCase):
    def test_a_gap_between_beats_is_reported(self):
        """此前只查重叠不查空洞：「漏了一段」和「这段没事发生」长得一模一样。"""
        doc = {'video_duration_sec': 20.0, 'banned_elements': [], 'beats': [
            _beat('B01', 0.0, 5.0, events=['E01']),
            _beat('B02', 12.0, 20.0, events=['E02'], frames=['review_003.png']),
        ]}
        hits = [v for v in reverse.validate_beats(doc, _overview(), schema=None)
                if v['code'] == 'window_uncovered']
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]['level'], 'warn')      # 空镜可以不属于任何里程碑
        self.assertIn('7.0s', hits[0]['message'])

    def test_a_missing_head_and_tail_are_both_reported(self):
        doc = {'video_duration_sec': 20.0, 'banned_elements': [], 'beats': [
            _beat('B01', 3.0, 10.0, events=['E01', 'E02']),
        ]}
        hits = [v for v in reverse.validate_beats(doc, _overview(), schema=None)
                if v['code'] == 'window_uncovered']
        self.assertEqual(len(hits), 2)

    def test_contiguous_beats_raise_nothing(self):
        doc = {'video_duration_sec': 10.0, 'banned_elements': [], 'beats': [
            _beat('B01', 0.0, 5.0, events=['E01']),
            _beat('B02', 5.0, 10.0, events=['E02'], frames=['review_003.png']),
        ]}
        self.assertEqual([v for v in reverse.validate_beats(doc, _overview(), schema=None)
                          if v['code'] == 'window_uncovered'], [])


class TestSceneConstants(unittest.TestCase):
    """场景恒常特征：与 banned_elements 对称的另一半。

    整条反推链路围绕**变化**而建，节拍只承载每拍 delta，于是不变的东西（墙上的污渍、
    青苔、常驻画面的工作灯）在数据结构里没有落脚点，一路被压掉——实测一条片子 57% 的
    帧都记到了污渍，而它在整条阶梯里一个字都没有。这一组盯的是它算得准、改得掉。
    """

    def _facts(self, n=20, always=('mossy concrete wall',), sometimes=('fresh timber',)):
        out = []
        for i in range(n):
            out.append({'frame': f'review_{i:03d}.png', 'timestamp': float(i),
                        'materials': list(always) + (list(sometimes) if i > n - 3 else []),
                        'tools': [], 'traces': [], 'workers_present': True})
        return out

    def test_a_thing_in_most_frames_becomes_a_constant(self):
        c = reverse.analyze_scene_constants(self._facts())
        self.assertIn('mossy concrete wall', c['materials'])
        self.assertNotIn('fresh timber', c['materials'])   # 只在最后两帧，是变化不是恒常

    def test_transient_tools_are_excluded_from_fixtures(self):
        facts = [
            {'frame': f'f_{i}.png', 'tools': ['cordless drill', 'tripod work light', 'utility knife'],
             'materials': ['concrete'], 'traces': []}
            for i in range(10)
        ]
        c = reverse.analyze_scene_constants(facts)
        # 手持工具（电钻、美工刀）属于瞬态工具，绝不能进入全片常驻器具列表
        self.assertNotIn('cordless drill', c.get('fixtures_in_shot', []))
        self.assertNotIn('utility knife', c.get('fixtures_in_shot', []))
        # 重型常驻照明设备（三脚架工作灯）允许作为常驻器具
        self.assertIn('tripod work light', c.get('fixtures_in_shot', []))

    def test_an_emptied_field_is_never_recomputed_back(self):
        """它进每一条合成提示词，所以用户必须删得掉。

        判「键在不在」而不是「值真不真」：按真值判定的话，用户把三栏全删空之后，
        下一次读状态就会把统计结果原样加回去——那个字段就永远删不掉了。
        """
        doc = {'beats': []}
        reverse.attach_scene_constants(doc, self._facts())
        self.assertTrue(doc['scene_constants']['materials'])

        doc['scene_constants'] = {}                      # 用户在卡点上删光
        reverse.attach_scene_constants(doc, self._facts())
        self.assertEqual(doc['scene_constants'], {})

    def test_no_facts_still_lands_the_key_so_it_is_not_recomputed_forever(self):
        doc = {'beats': []}
        reverse.attach_scene_constants(doc, [])
        self.assertIn('scene_constants', doc)

    def test_constants_and_signature_both_reach_the_dimensions(self):
        doc = {'video_duration_sec': 10.0, 'banned_elements': [],
               'scene_signature': 'A mossy concrete bunker in autumn woodland.',
               'scene_constants': {'materials': ['mossy concrete wall'], 'traces': []},
               'beats': [_beat('B01', 0.0, 10.0)]}
        dims = reverse.beats_to_dimensions(doc)
        self.assertEqual(dims['scene_constants'], {'materials': ['mossy concrete wall']})
        self.assertEqual(dims['scene_signature'], 'A mossy concrete bunker in autumn woodland.')

    def test_the_prompt_lines_carry_the_signature_first(self):
        lines = reverse.scene_constants_lines(
            {'materials': ['mossy concrete wall'], 'fixtures_in_shot': ['tripod work light']},
            'A mossy concrete bunker in autumn woodland.')
        self.assertTrue(lines[0].startswith('the place itself:'))
        self.assertTrue(any('tripod work light' in x for x in lines))
        self.assertEqual(reverse.scene_constants_lines({}, ''), [])


class TestAnchorGrounding(unittest.TestCase):
    """锚点图对齐真实首帧：整条链路上唯一一次让写手看见原片像素。"""

    def _overview(self, exists=True):
        path = __file__ if exists else '/nope/review_001.png'
        return {'review_sampling': {'frames': [
            {'frame_path': '/nope/review_009.png', 'timestamp': 9.0},
            {'frame_path': path, 'timestamp': 0.5},
        ]}}

    def test_the_earliest_existing_frame_is_chosen(self):
        self.assertEqual(reverse.anchor_reference_frame({}, self._overview()), __file__)

    def test_teaser_frame_is_skipped_for_real_start_frame(self):
        with tempfile.NamedTemporaryFile(suffix='_001.png', delete=False) as f1, \
             tempfile.NamedTemporaryFile(suffix='_002.png', delete=False) as f2:
            f1_path, f2_path = f1.name, f2.name
        try:
            overview = {'review_sampling': {'frames': [
                {'frame_path': f1_path, 'timestamp': 0.0},
                {'frame_path': f2_path, 'timestamp': 0.16},
            ]}}
            facts = [
                {'subject': 'A completed shelter clad in bark shingles stands on riverbank'},
                {'subject': 'A worker spraying white marking line onto wild grass'},
                {'subject': 'A worker cuts sod along outline'},
            ]
            self.assertEqual(reverse.anchor_reference_frame({}, overview, facts=facts), f2_path)
        finally:
            for p in (f1_path, f2_path):
                if os.path.exists(p):
                    os.unlink(p)

    def test_a_missing_file_yields_no_reference_instead_of_a_bad_path(self):
        self.assertIsNone(reverse.anchor_reference_frame({}, self._overview(exists=False)))
        self.assertIsNone(reverse.anchor_reference_frame({}, {}))

    def test_a_failed_call_keeps_the_composed_prompt(self):
        """失败软退是硬要求：这一步是纠偏，不该让整单合成挂掉。"""
        with patch.object(pp, '_multimodal_chat', side_effect=RuntimeError('gateway down')):
            self.assertEqual(
                reverse.ground_anchor_on_reference({}, 'original prompt', __file__),
                'original prompt')

    def test_an_empty_reply_keeps_the_composed_prompt(self):
        with patch.object(pp, '_multimodal_chat', return_value='   '):
            self.assertEqual(
                reverse.ground_anchor_on_reference({}, 'original prompt', __file__),
                'original prompt')

    def test_a_good_reply_replaces_it(self):
        with patch.object(pp, '_multimodal_chat', return_value=' grounded prompt '):
            self.assertEqual(
                reverse.ground_anchor_on_reference({}, 'original prompt', __file__),
                'grounded prompt')

    def test_no_reference_means_no_call_at_all(self):
        with patch.object(pp, '_multimodal_chat', side_effect=AssertionError('不该被调用')):
            self.assertEqual(reverse.ground_anchor_on_reference({}, 'p', None), 'p')


class TestPassAOptimizationTests(unittest.TestCase):
    """测试 Pass A 增量缓存、二分重试与峰值帧并发。"""

    def test_pass_a_bisection_retry(self):
        """当 >2 帧的批次解析失败时，先二分为两个子批次重试。"""
        import tempfile
        tmp_dir = tempfile.mkdtemp()
        frames = []
        for i in range(1, 5):
            p = os.path.join(tmp_dir, f'review_{i:03d}.png')
            with open(p, 'wb') as f: f.write(b'\x89PNG\r\n\x1a\n')
            frames.append({'index': i, 'timestamp': float(i), 'frame_path': p})

        overview = {'review_sampling': {'frames': frames}}
        with open(os.path.join(tmp_dir, 'video_overview.json'), 'w') as f:
            json.dump(overview, f)

        calls = []
        def mock_chat(*args, **kwargs):
            imgs = args[3]
            calls.append(len(imgs))
            if len(imgs) == 4:
                raise ValueError('4-frame response broken')
            # 2帧子批次返回正确 JSON
            ret = []
            for img_path in imgs:
                name = os.path.basename(img_path)
                ret.append({
                    'image_filename': name,
                    'subject': 'wall',
                    'action': 'painting',
                    'space': 'room',
                    'persistent_traces': ['paint on floor'],
                })
            return json.dumps(ret)

        with patch.object(pp, '_multimodal_chat', side_effect=mock_chat):
            facts = reverse.extract_frame_facts({}, tmp_dir)

        self.assertEqual(len(facts['facts']), 4)
        # 第一次尝试 4 帧失败后重试超额，接着二分为两个 2 帧子批次成功
        self.assertIn(4, calls)
        self.assertIn(2, calls)

    def test_verify_peak_frames_parallel_execution(self):
        """验证 verify_peak_frames 使用 _map_parallel 并行执行。"""
        import tempfile
        tmp_dir = tempfile.mkdtemp()
        f1 = os.path.join(tmp_dir, 'review_002.png')
        f2 = os.path.join(tmp_dir, 'review_005.png')
        with open(f1, 'wb') as f: f.write(b'png')
        with open(f2, 'wb') as f: f.write(b'png')

        overview = {
            'review_sampling': {
                'frames': [
                    {'frame_path': f1, 'timestamp': 1.0},
                    {'frame_path': f2, 'timestamp': 2.0},
                ]
            },
            'change_events': [
                {'event_id': 'E01', 'evidence_frames': ['review_001.png', 'review_002.png', 'review_003.png']},
                {'event_id': 'E02', 'evidence_frames': ['review_004.png', 'review_005.png', 'review_006.png']},
            ]
        }
        with open(os.path.join(tmp_dir, 'video_overview.json'), 'w') as f:
            json.dump(overview, f)
        raw_doc = {'facts': [{'frame': 'review_002.png', 'timestamp': 1.0}, {'frame': 'review_005.png', 'timestamp': 2.0}]}
        with patch.object(pp, '_map_parallel', return_value={
            0: {'review_002.png': {'subject': 'peak 1'}},
            1: {'review_005.png': {'subject': 'peak 2'}},
        }) as map_mock:
            res = reverse.verify_peak_frames({'model': 'test-model'}, tmp_dir, raw_doc)
            map_mock.assert_called_once()
            self.assertEqual(len(map_mock.call_args[0][1]), 1)
            self.assertEqual(res['peak_verified'], 2)


class TestAutofixBeats(unittest.TestCase):
    """AI 定向修复节拍阶梯硬伤的单元测试。"""

    def test_autofix_beats_clean_ladder_noop(self):
        """干净无错的阶梯不触发 LLM 调用，直接返回。"""
        overview = _overview(('E01', 'E02'))
        beats_doc = {
            'video_duration_sec': 10.0,
            'banned_elements': [],
            'scene_signature': 'a clean room',
            'beats': [
                _beat('B01', 0.0, 5.0, stage='demolition', events=('E01',)),
                _beat('B02', 5.0, 10.0, stage='surface', events=('E02',)),
            ]
        }
        with patch.object(pp, '_chat') as chat_mock:
            fixed_doc, fixed_count = reverse.autofix_beats({}, beats_doc, overview)
            chat_mock.assert_not_called()
            self.assertEqual(fixed_count, 0)
            self.assertEqual(len(fixed_doc['beats']), 2)

    def test_autofix_beats_fixes_stage_regression_and_rough_in(self):
        """阶梯包含阶段逆行与遮盖后隐蔽工程时，LLM 返回修正后能通过校验。"""
        overview = _overview(('E01', 'E02', 'E03'))
        # 构造有硬伤的节拍：B02 是 surface，B03 却逆行成 rough_in 且在 surface 之后
        beats_doc = {
            'video_duration_sec': 10.0,
            'banned_elements': [],
            'scene_signature': 'a room',
            'beats': [
                _beat('B01', 0.0, 3.0, stage='demolition', events=('E01',)),
                _beat('B02', 3.0, 6.0, stage='surface', events=('E02',)),
                _beat('B03', 6.0, 10.0, stage='rough_in', events=('E03',)),
            ]
        }
        initial_errors = [v for v in reverse.validate_beats(beats_doc, overview) if v['level'] == 'error']
        self.assertTrue(len(initial_errors) >= 1)

        # 模拟 LLM 修复：把 B03 改成 floor，符合施工顺序
        fixed_reply = json.dumps({
            'beats': [
                {'id': 'B01', 'stage': 'demolition', 'operation': 'clearing debris'},
                {'id': 'B02', 'stage': 'surface', 'operation': 'plastering'},
                {'id': 'B03', 'stage': 'floor', 'operation': 'installing wood planks'},
            ]
        })

        with patch.object(pp, '_chat', return_value=fixed_reply) as chat_mock:
            fixed_doc, fixed_count = reverse.autofix_beats({}, beats_doc, overview)
            chat_mock.assert_called_once()
            self.assertTrue(fixed_count >= 1)
            remaining_errors = [v for v in fixed_doc['validation'] if v['level'] == 'error']
            self.assertEqual(len(remaining_errors), 0)
            self.assertEqual(fixed_doc['beats'][2]['stage'], 'floor')
            # 验证时间与证据帧未丢失
            self.assertEqual(fixed_doc['beats'][2]['start'], 6.0)
            self.assertEqual(fixed_doc['beats'][2]['end'], 10.0)

    def test_autofix_beats_retries_on_bad_json(self):
        """首次返回非 JSON 时重试并成功修复。"""
        overview = _overview(('E01', 'E02'))
        beats_doc = {
            'video_duration_sec': 10.0,
            'banned_elements': [],
            'scene_signature': 'a room',
            'beats': [
                _beat('B01', 0.0, 5.0, stage='surface', events=('E01',)),
                _beat('B02', 5.0, 10.0, stage='structural', events=('E02',)), # 逆行
            ]
        }
        fixed_reply = json.dumps({
            'beats': [
                {'id': 'B01', 'stage': 'surface'},
                {'id': 'B02', 'stage': 'floor'},
            ]
        })
        with patch.object(pp, '_chat', side_effect=['not a json', fixed_reply]) as chat_mock:
            fixed_doc, fixed_count = reverse.autofix_beats({}, beats_doc, overview)
            self.assertEqual(chat_mock.call_count, 2)
            self.assertEqual(fixed_doc['beats'][1]['stage'], 'floor')
            remaining_errors = [v for v in fixed_doc['validation'] if v['level'] == 'error']
            self.assertEqual(len(remaining_errors), 0)

    def test_reconcile_event_coverage_resolves_double_bound_and_unbound(self):
        """测试自动解决多拍重复认领 E02/E07 的硬伤。"""
        overview = _overview(('E01', 'E02', 'E03'))
        overview['change_events'] = [
            {'event_id': 'E01', 'start': 0.0, 'end': 3.6, 'peak': 1.8},
            {'event_id': 'E02', 'start': 3.6, 'end': 10.9, 'peak': 5.5},
            {'event_id': 'E03', 'start': 10.9, 'end': 14.5, 'peak': 12.0},
        ]
        beats_doc = {
            'video_duration_sec': 14.5,
            'banned_elements': [],
            'scene_signature': 'a bunker',
            'beats': [
                _beat('B01', 0.0, 3.6, stage='demolition', events=('E01',)),
                # 拆拍导致的重复认领：B02 和 B03 均认领 E02
                _beat('B02', 3.6, 7.2, stage='structural', events=('E02',)),
                _beat('B03', 7.2, 10.9, stage='structural', events=('E02',)),
                _beat('B04', 10.9, 14.5, stage='surface', events=('E03',)),
            ]
        }
        # 此时有硬伤：E02 被多拍同时认领
        violations = reverse.validate_beats(beats_doc, overview)
        codes = [v['code'] for v in violations if v['level'] == 'error']
        self.assertIn('event_double_bound', codes)

        # 机械修复
        reverse.reconcile_event_coverage(beats_doc, overview)
        # 校验通过：E02 根据时间精确归属于 B02（包含 peak 5.5s），B03 为空，无重复认领
        fixed_violations = reverse.validate_beats(beats_doc, overview)
        fixed_errors = [v for v in fixed_violations if v['level'] == 'error']
        self.assertEqual(fixed_errors, [])
        self.assertEqual(beats_doc['beats'][1]['source_event_ids'], ['E02'])
        self.assertEqual(beats_doc['beats'][2]['source_event_ids'], [])

        # 测试 autofix_beats 面对包含 event_double_bound 的阶梯，在无须调用大模型的情况下也能直接通过机械层自愈
        broken_doc = {
            'video_duration_sec': 14.5,
            'banned_elements': [],
            'scene_signature': 'a bunker',
            'beats': [
                _beat('B01', 0.0, 3.6, stage='demolition', events=('E01',)),
                _beat('B02', 3.6, 7.2, stage='structural', events=('E02',)),
                _beat('B03', 7.2, 10.9, stage='structural', events=('E02',)),
                _beat('B04', 10.9, 14.5, stage='surface', events=('E03',)),
            ]
        }
        fixed_doc, count = reverse.autofix_beats({}, broken_doc, overview)
        self.assertGreaterEqual(count, 1)
        self.assertEqual([v for v in fixed_doc['validation'] if v['level'] == 'error'], [])

    def test_transition_beat_exempt_from_package_operations_check(self):
        """过门拍/运镜拍豁免 2~3 道施工工序与遗留痕迹检查。"""
        beats = [
            _beat('B01', 0.0, 3.5, stage='demolition', op='clear', pkg=('clear', 'haul')),
            _beat('B02', 3.5, 6.0, stage='transition', op='threshold', pkg=('threshold',), traces=()),
            _beat('B03', 6.0, 10.0, stage='floor', op='subfloor', pkg=('grade', 'compact')),
        ]
        errs = reverse._validate_composer_frame_contract(beats)
        self.assertEqual(errs, [])

    def test_autobalance_does_not_merge_transition_or_cross_space(self):
        """自动微拍平衡严禁合并过门运镜拍，严禁跨空间合并。"""
        beats_doc = {
            'video_duration_sec': 9.0,
            'beats': [
                {'id': 'B01', 'start': 0.0, 'end': 3.5, 'stage': 'demolition', 'space': 'exterior',
                 'operation': 'clear', 'package_operations': ['clear', 'haul']},
                # 1.5s 的过门微拍
                {'id': 'B02', 'start': 3.5, 'end': 5.0, 'stage': 'transition', 'space': 'exterior',
                 'operation': 'threshold', 'package_operations': ['threshold']},
                # 1.2s 的室内微拍
                {'id': 'B03', 'start': 5.0, 'end': 6.2, 'stage': 'demolition', 'space': 'interior',
                 'operation': 'clear', 'package_operations': ['rake', 'sweep']},
                {'id': 'B04', 'start': 6.2, 'end': 9.0, 'stage': 'floor', 'space': 'interior',
                 'operation': 'pave', 'package_operations': ['lay', 'fit']},
            ]
        }
        balanced, count = reverse.autobalance_beats(beats_doc, min_duration=2.0)
        stages = [b['stage'] for b in balanced['beats']]
        # 过门拍保持独立，未被并入 B01 或 B03
        self.assertIn('transition', stages)
        # B03 与 B04 在同空间 (interior) 合并，未跨到 exterior
        spaces = [b['space'] for b in balanced['beats']]
        self.assertEqual(spaces, ['exterior', 'exterior', 'interior'])


class TestMicroEngineeringForensics(unittest.TestCase):
    """验证微观工程细节提取、细粒度实体本体规范化与 ROI 局部切片生成。"""

    def test_canonicalize_expanded_entities(self):
        self.assertEqual(reverse.canonicalize_entity_phrase('18V cordless impact driver'), 'cordless impact driver')
        self.assertEqual(reverse.canonicalize_entity_phrase('pneumatic framing nail gun'), 'framing nailer')
        self.assertEqual(reverse.canonicalize_entity_phrase('black phosphate drywall screws'), 'drywall screws')
        self.assertEqual(reverse.canonicalize_entity_phrase('polyurethane expanding spray foam'), 'expanding PU foam sealant')
        self.assertEqual(reverse.canonicalize_entity_phrase('9mm OSB board'), 'OSB sheathing')
        self.assertEqual(reverse.canonicalize_entity_phrase('heavy-duty construction adhesive'), 'construction adhesive')
        self.assertEqual(reverse.canonicalize_entity_phrase('laser torpedo level'), 'spirit level')
        self.assertEqual(reverse.canonicalize_entity_phrase('tuck tape seam sealing'), 'seam sealing tape')

    def test_parse_facts_array_micro_details(self):
        raw_json = json.dumps([{
            'frame': 'review_005.png',
            'subject': 'worker securing wall studs',
            'spatial_zones': {'facade_and_walls': 'stud framing', 'floor': 'concrete'},
            'materials': ['2x4 lumber', 'black poly sheeting'],
            'material_specs': ['2x4 SPF timber studs (38x89mm)', '9mm OSB sheathing with raw matte texture'],
            'tools': ['cordless impact driver', 'spirit level'],
            'tool_specifics': ['18V brushless impact driver with magnetic bit holder'],
            'fastening_and_bonding': ['black drywall screws', 'construction adhesive'],
            'workers_present': True,
            'completion_extent': 'left wall framed',
            'traces': ['sawdust on floor'],
            'micro_traces': ['fine sawdust along cut lines', 'pencil layout cross-marks'],
            'confidence': 0.95
        }])
        facts = reverse._parse_facts_array(raw_json, ['review_005.png'])
        self.assertIn('review_005.png', facts)
        fact = facts['review_005.png']
        self.assertEqual(fact['materials'], ['timber framing studs', 'vapor barrier membrane'])
        self.assertEqual(fact['fastening_and_bonding'], ['drywall screws', 'construction adhesive'])
        self.assertEqual(fact['material_specs'], ['2x4 SPF timber studs (38x89mm)', '9mm OSB sheathing with raw matte texture'])
        self.assertEqual(fact['tool_specifics'], ['18V brushless impact driver with magnetic bit holder'])
        self.assertEqual(fact['micro_traces'], ['fine sawdust along cut lines', 'pencil layout cross-marks'])

    def test_drill_variants_collapse_to_one_entity(self):
        """钻的各种叫法必须归一到同一个实体。

        这条规则被删过一次（v3 重写词典时整条 drill 规则没了）。`tools` 是
        `_WINDOW_FACT_FIELDS` 成员，同一把钻在相邻帧里叫 drill / cordless drill 就会被
        逐窗统计读成"一个消失、一个出现"，节拍边界跟着错位——而这不会让任何校验器变红。
        """
        for phrase in ('drill', 'power drill', 'cordless drill', 'hammer drill',
                       'rotary drill', 'electric drill', 'cordless drill driver'):
            self.assertEqual(reverse.canonicalize_entity_phrase(phrase), 'cordless drill', phrase)

    def test_screw_gun_is_a_driver_not_a_nailer(self):
        """射钉枪那条规则不许再吃 `screw`——螺丝枪打的是螺丝，不是钉子。"""
        for phrase in ('screw gun', 'screwdriver', 'impact driver', 'cordless impact driver'):
            self.assertEqual(reverse.canonicalize_entity_phrase(phrase),
                             'cordless impact driver', phrase)
        for phrase in ('nail gun', 'pneumatic nailer', 'brad nailer', 'framing nailer'):
            self.assertEqual(reverse.canonicalize_entity_phrase(phrase), 'framing nailer', phrase)

    def test_level_prefix_is_required(self):
        """`level` 的前缀不能全可选，否则把根本不是器具的短语也归成水平尺。"""
        for phrase in ('level', 'spirit level', 'laser level', 'bubble level'):
            self.assertEqual(reverse.canonicalize_entity_phrase(phrase), 'spirit level', phrase)
        for phrase in ('floor level', 'eye level', 'water level in the trench'):
            self.assertEqual(reverse.canonicalize_entity_phrase(phrase), phrase)

    def test_parse_facts_array_strict_drops_unmatched_names(self):
        """峰值复核那一路给的图片多于帧数，按位兜底会把特写的读数挂到别的帧上。

        strict=True 必须只认名字对得上的对象——宁可这一帧不复核。
        """
        raw = json.dumps([
            {'frame': 'a.png', 'subject': 'real a'},
            {'frame': 'detail crop 1', 'subject': 'CROP LEAK'},
            {'frame': 'detail crop 2', 'subject': 'CROP LEAK'},
            {'frame': 'b.png', 'subject': 'real b'},
        ])
        expected = ['a.png', 'b.png', 'c.png']

        loose = reverse._parse_facts_array(raw, expected)
        self.assertEqual(loose['c.png']['subject'], 'CROP LEAK',
                         '按位兜底本来就会串位，这里固定住旧行为以说明 strict 的必要性')

        strict = reverse._parse_facts_array(raw, expected, strict=True)
        self.assertEqual(sorted(strict), ['a.png', 'b.png'])
        self.assertEqual(strict['a.png']['subject'], 'real a')
        self.assertEqual(strict['b.png']['subject'], 'real b')

    def test_merge_verified_fact_keeps_pass_a_on_blank_fields(self):
        """复核是增强不是门禁——这条得落到字段一级。

        复核提示词的重心在 material_specs / tool_specifics 上，模型漏答 materials/tools
        是常事；整条替换会把 Pass A 已经读对的实体清成空表，而 peak 帧正是节拍边界。
        """
        base = {
            'frame': 'f1.png', 'timestamp': 1.5, 'subject': 'pass A subject',
            'materials': ['plywood sheathing'], 'tools': ['cordless drill'],
            'traces': ['sawdust'], 'workers_present': True, 'confidence': 0.5,
        }
        refined = {
            'frame': 'f1.png', 'timestamp': None, 'subject': 'verified subject',
            'materials': [], 'tools': [], 'traces': ['fresh sawdust along cut lines'],
            'material_specs': ['9mm OSB sheathing'], 'workers_present': False,
            'confidence': 0.9,
        }
        merged = reverse._merge_verified_fact(base, refined, 'strong-model')

        # 漏答的字段落回 Pass A
        self.assertEqual(merged['materials'], ['plywood sheathing'])
        self.assertEqual(merged['tools'], ['cordless drill'])
        # 答了的字段以复核为准
        self.assertEqual(merged['subject'], 'verified subject')
        self.assertEqual(merged['traces'], ['fresh sawdust along cut lines'])
        self.assertEqual(merged['material_specs'], ['9mm OSB sheathing'])
        # bool/数值即便是 falsy 也算答了——workers_present: false 正是复核要给的结论
        self.assertIs(merged['workers_present'], False)
        self.assertEqual(merged['confidence'], 0.9)
        # timestamp 一律以 Pass A 为准：复核提示词里没有时间轴
        self.assertEqual(merged['timestamp'], 1.5)
        self.assertEqual(merged['verified_by'], 'strong-model')

    def test_extract_roi_patches_generation(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as td:
            img_path = os.path.join(td, 'frame_1080p.png')
            # 创建一张 1920x1080 的临时测试图像
            im = Image.new('RGB', (1920, 1080), color=(120, 150, 180))
            im.save(img_path)

            patches = reverse.extract_roi_patches(img_path, patch_size=512, max_patches=2, output_dir=td)
            self.assertEqual(len(patches), 2)
            for p in patches:
                self.assertTrue(os.path.exists(p))
                with Image.open(p) as p_im:
                    self.assertEqual(p_im.size, (512, 512))

    def test_extract_roi_patches_without_output_dir(self):
        """output_dir 省略时也要真的出切片。

        `tempfile` 曾漏了 import，NameError 被函数里的 except 吞掉，整个特写功能静默
        退回整帧——校验器全绿、日志一行没有。
        """
        from PIL import Image
        with tempfile.TemporaryDirectory() as td:
            img_path = os.path.join(td, 'frame_1080p.png')
            Image.new('RGB', (1920, 1080), color=(120, 150, 180)).save(img_path)

            patches = reverse.extract_roi_patches(img_path, patch_size=512, max_patches=2)
            self.assertEqual(len(patches), 2)
            self.assertNotIn(img_path, patches)
            for p in patches:
                with Image.open(p) as p_im:
                    self.assertEqual(p_im.size, (512, 512))

    def test_extract_roi_patches_limits_overlap(self):
        """两块 ROI 不许几乎重合。

        两块各自独立贴边内推时，16:9 上会重叠 55%——第二张图基本是第一张的副本，白花
        载荷还让"哪张图属于哪一帧"更难对位。矮画幅则应当只出一块，而不是出两块副本。
        """
        from PIL import Image

        # 切片是以 quality=85 存成 JPEG 的，逐像素编码会被有损压缩打坏，所以这里用整块
        # 平均亮度反推位置——线性渐变下平均亮度和 top 一一对应，而 JPEG 保得住均值。
        def gradient(path, w, h):
            """纵向线性渐变：第 y 行的亮度是 255 * y / h。"""
            im = Image.new('L', (w, h))
            im.putdata([(y * 255) // h for y in range(h) for _ in range(w)])
            im.convert('RGB').save(path)

        def patches_of(path, patch_size=512):
            return reverse.extract_roi_patches(path, patch_size=patch_size, max_patches=2,
                                               output_dir=os.path.dirname(path))

        def top_of(patch_path, h, patch_size=512):
            """从切片平均亮度反推它在原图里的 top。"""
            from PIL import ImageStat
            with Image.open(patch_path) as im:
                mean = ImageStat.Stat(im.convert('L')).mean[0]
            # mean ≈ 255 * (top + patch_size / 2) / h
            return mean * h / 255.0 - patch_size / 2.0

        with tempfile.TemporaryDirectory() as td:
            for w, h in ((1920, 1080), (3840, 2160), (1080, 1920)):
                p = os.path.join(td, f'f_{w}x{h}.png')
                gradient(p, w, h)
                patches = patches_of(p)
                self.assertEqual(len(patches), 2, f'{w}x{h} 应当出两块')

                # 重叠不许超过 patch 的 25%（即两个 top 至少相差 0.75 * patch）。
                # 留 8px 容差给 JPEG 与整数取整。
                sep = abs(top_of(patches[0], h) - top_of(patches[1], h))
                self.assertGreaterEqual(sep, int(512 * 0.75) - 8,
                                        f'{w}x{h} 两块重叠超过 25%（top 间距仅 {sep:.0f}px）')

            # 画幅高度不足 1.75 * patch_size 时，第二块只会是第一块的副本，应当只出一块。
            short = os.path.join(td, 'short.png')
            gradient(short, 1280, 700)
            self.assertEqual(len(patches_of(short)), 1)

    def test_facts_digest_includes_micro_details(self):
        facts = [{
            'frame': 'f01.png',
            'timestamp': 2.5,
            'subject': 'framing',
            'spatial_zones': {'facade_and_walls': 'studs'},
            'completion_extent': 'half wall',
            'materials': ['timber framing studs'],
            'material_specs': ['2x4 SPF studs'],
            'tools': ['cordless impact driver'],
            'tool_specifics': ['18V impact driver'],
            'fastening_and_bonding': ['drywall screws'],
            'traces': ['dust'],
            'micro_traces': ['fine sawdust'],
            'workers_present': True,
            'confidence': 0.9,
        }]
        digest = reverse._facts_digest(facts)
        self.assertIn('mat_specs=2x4 SPF studs', digest)
        self.assertIn('tool_specs=18V impact driver', digest)
        self.assertIn('fasteners=drywall screws', digest)
        self.assertIn('micro_traces=fine sawdust', digest)








class TestObservedShotCuts(unittest.TestCase):
    """原片这一拍是一个镜头还是切了三刀——多镜头交付唯一的事实底座。

    这组数据抽帧脚本一直在算（analyze_timelapse_video.detect_scene_change_points 的高阈值
    那一遍就是剪辑切点，低阈值那遍还专门扣掉了切点附近 0.3s 以免把切点误当状态变化），
    也一直写进 video_overview.json 的 cut_points，但在 attach_shot_cuts 之前**全线没有
    读者**：复刻线只取了聚合后的 pace_metrics 三个数拿去显示。逐拍接出来之后，
    「这一拍排三镜还是四镜」才不再是由片长凭空决定。

    这里钉的是三条会静默错的边界：边界那一刀不算内部切点、没有切点数据时字段必须
    缺席（而不是伪装成一镜）、二创变体原样跳过。
    """

    def _overview_with_cuts(self, cuts):
        overview = _overview()
        overview['cut_points'] = list(cuts)
        return overview

    def test_cut_points_are_sanitised(self):
        self.assertEqual(reverse.overview_cut_points(None), [])
        self.assertEqual(reverse.overview_cut_points({}), [])
        self.assertEqual(reverse.overview_cut_points({'cut_points': 'nope'}), [])
        # 排序、去重、丢掉负数与非数字
        self.assertEqual(
            reverse.overview_cut_points({'cut_points': [5.2, 1.0, 5.2, -3, 'x', 2.5]}),
            [1.0, 2.5, 5.2])

    def test_boundary_cuts_do_not_split_the_beat(self):
        """拍与拍的分界处本来就常压着一刀（Pass B 就是按变化聚的类）。

        把它算成内部切点，等于每一拍都凭空多一个镜头——而且是**每一拍**都多，
        错得整齐到看不出来。"""
        cuts = [4.0, 6.5, 10.0]
        self.assertEqual(reverse.observed_cuts_for_window(cuts, 4.0, 10.0), [6.5])
        # 边界容差 0.15s 与抽帧脚本给切点去重用的窗口同宽
        self.assertEqual(reverse.observed_cuts_for_window([4.1], 4.0, 10.0), [])
        self.assertEqual(reverse.observed_cuts_for_window([4.2], 4.0, 10.0), [4.2])

    def test_shot_count_is_cuts_plus_one(self):
        overview = self._overview_with_cuts([0.0, 3.0, 5.5, 9.0])
        doc = {'video_duration_sec': 14.0, 'banned_elements': [],
               'beats': [_beat('B01', 0.0, 9.0, events=['E01']),
                         _beat('B02', 9.0, 14.0, events=['E02'])]}
        reverse.attach_shot_cuts(doc, overview)
        first, second = doc['beats']
        self.assertEqual(first['observed_cuts'], [3.0, 5.5])
        self.assertEqual(first['observed_shot_count'], 3)
        # 第二拍内部一刀都没有 = 原片这一拍是一镜
        self.assertEqual(second['observed_cuts'], [])
        self.assertEqual(second['observed_shot_count'], 1)

    def test_editing_the_window_recomputes_the_count(self):
        """拆拍/并拍改的就是时间窗。留着上一版的镜头数，等于让人按别的拍窗做判断。"""
        overview = self._overview_with_cuts([3.0, 5.5])
        doc = {'video_duration_sec': 14.0, 'banned_elements': [],
               'beats': [_beat('B01', 0.0, 9.0, events=['E01'])]}
        reverse.attach_shot_cuts(doc, overview)
        self.assertEqual(doc['beats'][0]['observed_shot_count'], 3)
        doc['beats'][0]['end'] = 4.0
        reverse.attach_shot_cuts(doc, overview)
        self.assertEqual(doc['beats'][0]['observed_cuts'], [3.0])
        self.assertEqual(doc['beats'][0]['observed_shot_count'], 2)

    def test_missing_cut_data_leaves_the_field_absent(self):
        """未知与「一镜到底」必须分得开。

        老 job 的 overview 里没有 cut_points；抽帧环境异常时也可能整组缺失。
        这时候留下一个 1，下游会当成「原片确实是一镜」，据此挑最小镜头梯并在审核表上
        写一行言之凿凿的偏差说明——全是编的。"""
        doc = {'video_duration_sec': 14.0, 'banned_elements': [],
               'beats': [_beat('B01', 0.0, 9.0, events=['E01'],
                               observed_cuts=[3.0], observed_shot_count=2)]}
        reverse.attach_shot_cuts(doc, _overview())
        self.assertNotIn('observed_cuts', doc['beats'][0])
        self.assertNotIn('observed_shot_count', doc['beats'][0])
        self.assertIsNone(reverse.observed_shot_stats(doc))

    def test_variants_inherit_the_rhythm_instead_of_recomputing(self):
        """二创变体自己的目录里没有 overview（reference_frames 那条线）。

        按空切点重算只会把继承下来的镜头数一路抹平——与 shot_scale / camera_move
        被列为「节奏骨架，原样继承」是同一条纪律。"""
        doc = {'video_duration_sec': 14.0, 'banned_elements': [], 'variant_of': 'job-1',
               'mutation_axes': ['carrier'],
               'beats': [_beat('B01', 0.0, 9.0, events=['E01'],
                               observed_cuts=[3.0, 5.5], observed_shot_count=3)]}
        reverse.attach_shot_cuts(doc, _overview())
        self.assertEqual(doc['beats'][0]['observed_shot_count'], 3)

    def test_stats_summarise_the_whole_ladder(self):
        overview = self._overview_with_cuts([3.0, 5.5, 11.0])
        doc = {'video_duration_sec': 14.0, 'banned_elements': [],
               'beats': [_beat('B01', 0.0, 9.0, events=['E01']),
                         _beat('B02', 9.0, 14.0, events=['E02'])]}
        reverse.attach_shot_cuts(doc, overview)
        stats = reverse.observed_shot_stats(doc)
        self.assertEqual(stats['beats'], 2)
        self.assertEqual(stats['cuts'], 3)
        self.assertEqual(stats['single_shot_beats'], 0)
        self.assertEqual(stats['max_shots'], 3)
        self.assertEqual(stats['avg_shots'], 2.5)
