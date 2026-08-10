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


class TestConstructionOrder(unittest.TestCase):
    def test_stage_regression_is_an_error(self):
        """面层做完又回去做隐蔽工程 = 布线在封板之后，硬否决。"""
        doc = {'video_duration_sec': 10.0, 'banned_elements': [], 'beats': [
            _beat('B01', 0.0, 5.0, stage='surface', events=['E01']),
            _beat('B02', 5.0, 10.0, stage='rough_in', events=['E02'],
                  frames=['review_003.png']),
        ]}
        codes = [v['code'] for v in reverse.validate_beats(doc, _overview(), schema=None)]
        self.assertIn('stage_regression', codes)

    def test_small_stage_backtrack_is_tolerated(self):
        """真实改造会来回穿插一级，只有跨两级以上的倒挂才判。"""
        doc = {'video_duration_sec': 10.0, 'banned_elements': [], 'beats': [
            _beat('B01', 0.0, 5.0, stage='floor', events=['E01']),
            _beat('B02', 5.0, 10.0, stage='surface', events=['E02'],
                  frames=['review_003.png']),
        ]}
        errors = [v for v in reverse.validate_beats(doc, _overview(), schema=None)
                  if v['level'] == 'error']
        self.assertEqual(errors, [])

    def test_rough_in_after_enclosure_beats_the_backtrack_tolerance(self):
        """布线在封板之后是硬否决榜首，而它只差一级——不能被 ±1 容差放过。"""
        doc = {'video_duration_sec': 10.0, 'banned_elements': [], 'beats': [
            _beat('B01', 0.0, 5.0, stage='enclosure', events=['E01']),
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
                  frames=['review_003.png']),
        ]}
        codes = [v['code'] for v in reverse.validate_beats(doc, _overview(), schema=None)]
        self.assertIn('power_chain_broken', codes)

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
        """签名即防护。加形参之前先想清楚：这是反注入唯一的结构性保证。"""
        import inspect
        params = set(inspect.signature(reverse.extract_frame_facts).parameters)
        self.assertEqual(params, {'config', 'job_dir', 'on_progress', 'degraded', 'batch_size'})
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

    def test_banned_hits_are_case_insensitive(self):
        hits = reverse.banned_element_hits(
            'A worker lifts a WELDING TORCH into frame.', ['welding torch', 'excavator'])
        self.assertEqual(hits, ['welding torch'])

    def test_no_banned_elements_means_no_hits(self):
        self.assertEqual(reverse.banned_element_hits('anything at all', []), [])


if __name__ == '__main__':
    unittest.main()
