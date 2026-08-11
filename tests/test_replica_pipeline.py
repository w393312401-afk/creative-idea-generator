"""复刻流水线（replica_pipeline）的状态机与门禁契约。

盯的是四件在真实使用里最容易悄悄坏掉、且坏了要到成片才发现的事：
  1. 拼贴图门禁 —— 缺了它就等于没看过整条序列就定义节拍；
  2. 视频去重 —— 重传同一条视频不该再跑一遍几分钟的 ffmpeg；
  3. 硬伤闸门 —— 校验没过的节拍不能进合成；
  4. job_id 来自请求体，删除时不能让它走出 jobs 目录。
"""

import json
import os
import tempfile
import unittest
from unittest.mock import patch

import server_common
import replica_pipeline as rp


class ReplicaTempRootCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old_root = server_common.OUTPUT_ROOT
        server_common.OUTPUT_ROOT = self.tmp

    def tearDown(self):
        server_common.OUTPUT_ROOT = self.old_root

    def _ingest(self, payload=b'fake-video-bytes', name='clip.mp4'):
        with patch.object(rp, '_probe', return_value={'duration_sec': 12.0}):
            return rp.ingest_video(payload, name)


class TestIngest(ReplicaTempRootCase):
    def test_ingest_creates_a_job_with_the_video_on_disk(self):
        state = self._ingest()
        self.assertEqual(state['stage'], 'ingest')
        self.assertTrue(os.path.exists(state['video_path']))
        self.assertEqual(state['media']['duration_sec'], 12.0)
        self.assertTrue(os.path.exists(rp._state_path(state['job_id'])))

    def test_same_video_reuses_the_existing_job(self):
        """抽帧是几分钟的 ffmpeg。同一条视频重传一次就重跑一遍纯属浪费。"""
        first = self._ingest()
        second = self._ingest()
        self.assertEqual(second['job_id'], first['job_id'])
        self.assertTrue(second.get('reused'))
        self.assertEqual(len(rp.list_replica_jobs()), 1)

    def test_different_videos_get_different_jobs(self):
        first = self._ingest(b'aaa', 'a.mp4')
        second = self._ingest(b'bbb', 'b.mp4')
        self.assertNotEqual(first['job_id'], second['job_id'])
        self.assertEqual(len(rp.list_replica_jobs()), 2)

    def test_extensionless_filename_still_lands_on_disk(self):
        state = self._ingest(b'x', 'no-extension')
        self.assertTrue(state['video_name'].endswith('.mp4'))
        self.assertTrue(os.path.exists(state['video_path']))

    def test_path_separators_in_the_filename_cannot_escape_the_job_dir(self):
        state = self._ingest(b'x', '../../etc/passwd')
        self.assertEqual(os.path.dirname(state['video_path']), rp.job_dir(state['job_id']))


class ExtractedJobCase(ReplicaTempRootCase):
    """抽帧产物的夹具。抽帧密度那一组用例复用同一份，不另抄一份 overview。"""

    def _write_overview(self, job_id, collage):
        payload = {
            'source_video': 'x.mp4',
            'media_metadata': {'duration_sec': 10.0},
            'keyframe_collage': collage,
            'change_events': [],
            'change_event_count': 0,
            'analysis_plan': {'mode': 'full', 'required_count': 3, 'total_frames': 3,
                              'required_frames': ['review_001.png']},
            'review_sampling': {'frame_count': 3, 'contact_sheets': [],
                                'frames': [{'index': 1, 'timestamp': 0.5,
                                            'frame_path': '/x/review_001.png'}]},
            'scenes': [],
        }
        path = os.path.join(rp.job_dir(job_id), 'video_overview.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(payload, f)
        return path

    def _extracted_job(self, base_fps=None):
        state = self._ingest()
        collage = os.path.join(rp.job_dir(state['job_id']), 'clip_collage.jpg')
        with open(collage, 'wb') as f:
            f.write(b'jpg')
        self._write_overview(state['job_id'], collage)
        with patch.object(rp.subprocess, 'run') as run, \
             patch.object(rp, '_analyzer_script', return_value='/fake/analyze.py'):
            run.return_value.returncode = 0
            return rp.run_extract(state, base_fps=base_fps), collage


class TestExtractGate(ExtractedJobCase):
    def test_missing_collage_is_a_hard_failure(self):
        """手工跑时人会看到脚本 stdout 的 FAILED；产品化后没人看 stdout，
        所以必须在这里转成硬失败，否则就会出现「没见过整条序列就定义节拍」。"""
        state = self._ingest()
        self._write_overview(state['job_id'], None)
        with patch.object(rp.subprocess, 'run') as run, \
             patch.object(rp, '_analyzer_script', return_value='/fake/analyze.py'):
            run.return_value.returncode = 0
            with self.assertRaises(RuntimeError) as ctx:
                rp.run_extract(state)
        self.assertIn('拼贴图', str(ctx.exception))

    def test_collage_pointing_at_a_missing_file_also_fails(self):
        state = self._ingest()
        self._write_overview(state['job_id'], '/nonexistent/collage.jpg')
        with patch.object(rp.subprocess, 'run') as run, \
             patch.object(rp, '_analyzer_script', return_value='/fake/analyze.py'):
            run.return_value.returncode = 0
            with self.assertRaises(RuntimeError):
                rp.run_extract(state)

    def test_extract_stops_at_the_cost_gate_instead_of_running_on_into_pass_a(self):
        """成本卡点必须在代码里真的存在。

        2026-08-10 之前 extract 结束直接续跑 Pass A，预估只作为一行 SSE 文案闪过去，
        UI 上那对「完整 / 降级」单选框首跑时根本没机会出现——每一单都默默走了完整档。
        Pass A 是整条线唯一的大额支出，它前面必须有个真的停顿。
        """
        out, collage = self._extracted_job()
        self.assertEqual(out['stage'], 'confirm_cost')
        self.assertEqual(out['overview']['collage'], collage)
        # 停下来的同时必须已经算好预估，否则这个卡点上没东西可看。三档都要在：
        # 用户在这个卡点上比的就是「多花多少钱换多少帧」。
        self.assertIn('full', out['cost_estimate'])
        self.assertIn('degraded', out['cost_estimate'])
        self.assertIn('all', out['cost_estimate'])

    def test_extract_only_entry_point_does_not_touch_pass_a(self):
        state = self._ingest()
        collage = os.path.join(rp.job_dir(state['job_id']), 'clip_collage.jpg')
        with open(collage, 'wb') as f:
            f.write(b'jpg')
        self._write_overview(state['job_id'], collage)
        with patch.object(rp.subprocess, 'run') as run, \
             patch.object(rp, '_analyzer_script', return_value='/fake/analyze.py'), \
             patch.object(rp, 'run_reverse') as reverse_call:
            run.return_value.returncode = 0
            out = rp.extract_replica_job({}, state['job_id'])
        self.assertEqual(out['stage'], 'confirm_cost')
        reverse_call.assert_not_called()

    def test_returning_to_the_cost_gate_does_not_re_extract(self):
        """换个采样档位回来时不该再跑一遍几分钟的 ffmpeg。"""
        out, _collage = self._extracted_job()
        out['stage'] = 'review_beats'
        rp._save_state(out)
        with patch.object(rp, 'run_extract') as extract_call:
            back = rp.extract_replica_job({}, out['job_id'])
        extract_call.assert_not_called()
        self.assertEqual(back['stage'], 'confirm_cost')

    def test_a_job_that_died_during_extract_re_extracts_on_retry(self):
        """判据是磁盘上有没有 overview，不是 stage——只看 stage 会让抽帧失败的 job
        直接掉进 Pass A 报「找不到 overview」，用户永远重试不出来。"""
        state = self._ingest()
        state['stage'] = 'extract'
        rp._save_state(state)
        with patch.object(rp, 'run_extract', side_effect=RuntimeError('re-extract reached')) as ex:
            with self.assertRaises(RuntimeError):
                rp.start_replica_job({}, state['job_id'])
        self.assertTrue(ex.called)

    def test_retry_clears_the_previous_failure_marker(self):
        """跑成功了前端还挂着旧的错误横幅，用户会以为又失败了。"""
        state = self._ingest()
        state['error'] = 'Expecting \',\' delimiter: line 156 column 6'
        state['stage'] = 'cluster_beats'
        rp._save_state(state)

        seen = {}

        def _fake_extract(st, on_progress=None):
            seen['error_at_entry'] = st['error']
            raise RuntimeError('stop here')

        with patch.object(rp, 'run_extract', side_effect=_fake_extract):
            with self.assertRaises(RuntimeError):
                rp.start_replica_job({}, state['job_id'])
        self.assertIsNone(seen['error_at_entry'])

    def test_analyzer_failure_surfaces_its_stderr(self):
        state = self._ingest()
        with patch.object(rp.subprocess, 'run') as run, \
             patch.object(rp, '_analyzer_script', return_value='/fake/analyze.py'):
            run.return_value.returncode = 1
            run.return_value.stderr = 'ffmpeg not found'
            with self.assertRaises(RuntimeError) as ctx:
                rp.run_extract(state)
        self.assertIn('ffmpeg not found', str(ctx.exception))


class TestSamplingDensity(ExtractedJobCase):
    """抽帧密度可选（用户反馈：识别到的图片太少）。

    盯两件会静默坏掉的事：选的密度要真的传进抽帧脚本；换了密度必须重抽**并且**
    把上一档的帧事实清掉——帧文件名是序号不是时间戳，留着缓存就会把上一档某一秒
    的观察安在这一档另一秒的帧上，全程不报错。
    """

    def _run_extract_capturing_argv(self, state, base_fps=None):
        collage = os.path.join(rp.job_dir(state['job_id']), 'clip_collage.jpg')
        with open(collage, 'wb') as f:
            f.write(b'jpg')
        self._write_overview(state['job_id'], collage)
        with patch.object(rp.subprocess, 'run') as run, \
             patch.object(rp, '_analyzer_script', return_value='/fake/analyze.py'):
            run.return_value.returncode = 0
            out = rp.run_extract(state, base_fps=base_fps)
        return out, run.call_args[0][0]

    def test_chosen_density_reaches_the_analyzer_and_lands_in_the_state(self):
        out, argv = self._run_extract_capturing_argv(self._ingest(), base_fps=4)
        self.assertEqual(argv[argv.index('--base-fps') + 1], '4.0')
        # 密采窗跟着基线走：基线抬上去还留着默认 6fps，等于密采不再密。
        self.assertEqual(argv[argv.index('--dense-fps') + 1], '12.0')
        self.assertEqual(out['sampling'], {'base_fps': 4.0, 'dense_fps': 12.0})

    def test_an_unknown_density_falls_back_to_the_default_tier(self):
        """不做四舍五入到最近档：静默改档位比明确回落到默认档更难查。"""
        _out, argv = self._run_extract_capturing_argv(self._ingest(), base_fps=9.7)
        self.assertEqual(argv[argv.index('--base-fps') + 1], '2.0')

    def test_a_re_extract_without_a_density_keeps_the_one_the_job_used(self):
        state = self._ingest()
        self._run_extract_capturing_argv(state, base_fps=3)
        _out, argv = self._run_extract_capturing_argv(state, base_fps=None)
        self.assertEqual(argv[argv.index('--base-fps') + 1], '3.0')

    def test_changing_the_density_forces_a_re_extract(self):
        """「按新密度重抽帧」按钮的全部意思就是重抽。沿用旧帧等于这个按钮什么也没做。"""
        out, _collage = self._extracted_job(base_fps=2)
        with patch.object(rp, 'run_extract') as extract_call:
            rp.extract_replica_job({}, out['job_id'], base_fps=4)
        extract_call.assert_called_once()
        self.assertEqual(extract_call.call_args.kwargs['base_fps'], 4)

    def test_the_same_density_still_skips_the_re_extract(self):
        """抽帧是几分钟的 ffmpeg。只换送审档位回到卡点时不该再跑一遍。"""
        out, _collage = self._extracted_job(base_fps=2)
        out['stage'] = 'review_beats'
        rp._save_state(out)
        with patch.object(rp, 'run_extract') as extract_call:
            back = rp.extract_replica_job({}, out['job_id'], base_fps=2)
        extract_call.assert_not_called()
        self.assertEqual(back['stage'], 'confirm_cost')

    def test_re_extract_drops_the_stale_frame_facts_cache(self):
        state = self._ingest()
        self._run_extract_capturing_argv(state, base_fps=2)
        directory = rp.job_dir(state['job_id'])
        os.makedirs(os.path.join(directory, 'review_frames'), exist_ok=True)
        stale = os.path.join(directory, 'review_frames', 'review_001.png')
        with open(stale, 'wb') as f:
            f.write(b'png')
        cache = os.path.join(directory, '.frame_facts_cache.json')
        with open(cache, 'w', encoding='utf-8') as f:
            json.dump({'prompt_version': 'v1', 'frames': {'review_001.png': {}}}, f)

        self._run_extract_capturing_argv(state, base_fps=4)
        self.assertFalse(os.path.exists(cache))
        self.assertFalse(os.path.exists(stale))


class TestReviewScopeThreading(ReplicaTempRootCase):
    """送审档位要一路走到 Pass A。断在中间的话，UI 上选了「全部」照样只送计划档。"""

    def _job_ready_for_reverse(self):
        state = self._ingest()
        state['stage'] = 'confirm_cost'
        rp._save_state(state)
        os.makedirs(rp.job_dir(state['job_id']), exist_ok=True)
        with open(os.path.join(rp.job_dir(state['job_id']), 'video_overview.json'),
                  'w', encoding='utf-8') as f:
            json.dump({'review_sampling': {'frames': []}}, f)
        return state

    def test_scope_reaches_pass_a_and_is_remembered_on_the_job(self):
        state = self._job_ready_for_reverse()
        with patch('prompt_pipeline.reverse.extract_frame_facts',
                   side_effect=RuntimeError('stop after pass a call')) as pass_a:
            with self.assertRaises(RuntimeError):
                rp.start_replica_job({}, state['job_id'], scope='all')
        self.assertEqual(pass_a.call_args.kwargs['scope'], 'all')
        # recluster 会拿这个字段重跑，存错等于下一次静默换档。
        self.assertEqual(rp._load_state(state['job_id'])['review_scope'], 'all')

    def test_the_old_degraded_boolean_still_works(self):
        state = self._job_ready_for_reverse()
        with patch('prompt_pipeline.reverse.extract_frame_facts',
                   side_effect=RuntimeError('stop after pass a call')) as pass_a:
            with self.assertRaises(RuntimeError):
                rp.start_replica_job({}, state['job_id'], degraded=True)
        self.assertEqual(pass_a.call_args.kwargs['scope'], 'degraded')


class TestComposeGate(ReplicaTempRootCase):
    def _job_with_beats(self, validation):
        state = self._ingest()
        state['beats'] = {'beats': [{'id': 'B01'}], 'banned_elements': []}
        state['validation'] = validation
        rp._save_state(state)
        return state

    def test_unresolved_errors_block_composition(self):
        state = self._job_with_beats([
            {'level': 'error', 'code': 'event_unbound', 'message': '变化事件 E01 没有被任何一拍认领'}])
        with self.assertRaises(RuntimeError) as ctx:
            rp.run_compose(state, {})
        self.assertIn('E01', str(ctx.exception))

    def test_warnings_alone_do_not_block(self):
        state = self._job_with_beats([{'level': 'warn', 'code': 'vague_state', 'message': '含糊'}])
        with patch('prompt_pipeline.compose_anchor_and_packet',
                   return_value={'title': '石屋工作室'}) as phase1, \
             patch('prompt_pipeline.compose_remaining_beats',
                   return_value='IMAGE 1: a stone hut'):
            out = rp.run_compose(state, {})
        self.assertTrue(phase1.called)
        self.assertEqual(out['stage'], 'completed')
        self.assertEqual(out['title'], '石屋工作室')

    def test_banned_elements_hit_blocks_delivery_instead_of_just_reporting(self):
        """2026-08-10：命中从"记一笔然后照常交付"改成真的拦下。

        旧行为下这里断言的是 stage == 'completed' —— 扫出命中、写进创意库、任务显示
        已完成，只在文案里说一句"交付前必须重写"。没有任何东西拦着，用户拿它去渲染，
        画面里就长出原片根本没有的东西。门禁不堵就只是报告单。
        """
        state = self._ingest()
        state['beats'] = {'beats': [{'id': 'B01'}], 'banned_elements': ['excavator']}
        state['validation'] = []
        rp._save_state(state)
        with patch('prompt_pipeline.compose_anchor_and_packet', return_value={'title': 't'}), \
             patch('prompt_pipeline.compose_remaining_beats',
                   return_value='VIDEO 1: an EXCAVATOR swings into frame'), \
             patch('server_common.write_library_item') as write_item:
            out = rp.run_compose(state, {})
        self.assertEqual(out['banned_hits'], ['excavator'])
        self.assertEqual(out['stage'], 'audit_failed')
        # 不入库：项目工作台上不该出现一份带幻觉元素的提示词。
        write_item.assert_not_called()

    def test_a_clean_prompt_still_completes_and_publishes(self):
        state = self._ingest()
        state['beats'] = {'beats': [{'id': 'B01'}], 'banned_elements': ['excavator']}
        state['validation'] = []
        rp._save_state(state)
        with patch('prompt_pipeline.compose_anchor_and_packet', return_value={'title': 't'}), \
             patch('prompt_pipeline.compose_remaining_beats',
                   return_value='VIDEO 1: a lone worker trowels the wall'), \
             patch('server_common.write_library_item') as write_item:
            out = rp.run_compose(state, {})
        self.assertEqual(out['banned_hits'], [])
        self.assertEqual(out['stage'], 'completed')
        write_item.assert_called_once()

    def test_a_blocked_job_cannot_be_handed_off_to_the_renderer(self):
        """拦下交付却还能送去渲染，等于没拦。"""
        state = self._ingest()
        state['beats'] = {'beats': [{'id': 'B01'}], 'banned_elements': ['excavator']}
        state['validation'] = []
        rp._save_state(state)
        with patch('prompt_pipeline.compose_anchor_and_packet', return_value={'title': 't'}), \
             patch('prompt_pipeline.compose_remaining_beats',
                   return_value='VIDEO 1: an EXCAVATOR swings into frame'), \
             patch('server_common.write_library_item'):
            rp.run_compose(state, {})
        with self.assertRaises(ValueError) as ctx:
            rp.handoff_to_render(state['job_id'])
        self.assertIn('excavator', str(ctx.exception))


class TestStageCatalogAndFrameUrls(ReplicaTempRootCase):
    def test_every_stage_has_a_label(self):
        """漏一个 stage，工作台上就露出 `confirm_cost` 这种内部名。"""
        for stage in rp.STAGES:
            self.assertIn(stage, rp.STAGE_LABELS, f'{stage} 少了中文标签')

    def test_every_stage_belongs_to_exactly_one_visible_phase(self):
        """四阶段阶梯要能标出任意 stage 的位置；漏掉的会静默落回第一段。"""
        for stage in rp.STAGES:
            if stage == 'cancelled':
                continue
            owners = [k for k, _lab, stages in rp.PHASES if stage in stages]
            self.assertEqual(len(owners), 1, f'{stage} 归属于 {owners}，应当恰好一个阶段')

    def test_job_rows_carry_the_label_so_the_frontend_need_not_copy_it(self):
        state = self._ingest()
        row = next(r for r in rp.list_replica_jobs() if r['job_id'] == state['job_id'])
        self.assertEqual(row['stage_label'], rp.STAGE_LABELS['ingest'])
        self.assertEqual(row['phase'], 'material')

    def test_frame_urls_come_from_disk_not_from_a_filename_guess(self):
        """目录布局是抽帧脚本的实现细节。前端拿正则猜 storyboard/review_frames，
        脚本一改目录名，证据帧就在最需要看图的地方碎成一片。"""
        state = self._ingest()
        for sub, name in (('review_frames', 'review_001.png'), ('storyboard', 'scene_01.png')):
            folder = os.path.join(rp.job_dir(state['job_id']), sub)
            os.makedirs(folder, exist_ok=True)
            with open(os.path.join(folder, name), 'wb') as f:
                f.write(b'png')
        urls = rp.get_replica_status(state['job_id'])['frame_urls']
        self.assertTrue(urls['review_001.png'].endswith('/review_frames/review_001.png'))
        self.assertTrue(urls['scene_01.png'].endswith('/storyboard/scene_01.png'))

    def test_a_variant_resolves_frames_against_its_source_job(self):
        """变体自己不存帧，几百张 PNG 不该复制一遍。"""
        source = self._ingest()
        folder = os.path.join(rp.job_dir(source['job_id']), 'review_frames')
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, 'review_007.png'), 'wb') as f:
            f.write(b'png')
        urls = rp.frame_urls({'job_id': 'replica_variant', 'variant_of': source['job_id']})
        self.assertIn(source['job_id'], urls['review_007.png'])


class TestHandoffToRenderer(ReplicaTempRootCase):
    """「送去分步管线渲染」。

    这条交接最容易悄悄坏在一个地方：只把 dimensions 递过去。分步管线的
    start_stepped_pipeline 自己会调一遍 compose_anchor_and_packet，于是渲染用的是
    **重新合成的另一份**提示词——既白付一次 Phase 1 的钱，又让 banned 门禁失去意义
    （审的是 A，渲的是 B）。所以交接必须带上已合成的 Phase 1 产物。
    """

    def _completed_job(self):
        state = self._ingest()
        state['beats'] = {'beats': [{'id': 'B01'}], 'banned_elements': []}
        state['validation'] = []
        rp._save_state(state)
        compose_state = {
            'title': '石屋工作室', 'theme': 't', 'total_beats': 2,
            'parsed_brief': {'mode': 'Standard'}, 'beat_ladder': [{'index': 1}],
            'packet': {'world_lock': 'x'}, 'brief_fingerprint': 'fp',
            'image_1_prompt': 'IMAGE 1: ...',
            'compiled_images': {1: 'img one'}, 'compiled_videos': {1: 'vid one'},
        }
        with patch('prompt_pipeline.compose_anchor_and_packet', return_value=compose_state), \
             patch('prompt_pipeline.compose_remaining_beats', return_value='IMAGE 1: a stone hut'), \
             patch('server_common.write_library_item'):
            rp.run_compose(state, {})
        return state

    def test_handoff_carries_the_already_composed_phase_one(self):
        state = self._completed_job()
        dims, title, precomposed = rp.handoff_to_render(state['job_id'])
        self.assertEqual(title, '石屋工作室')
        self.assertTrue(dims.get('reverse_engineered'))
        self.assertIsNotNone(precomposed)
        self.assertEqual(precomposed['packet'], {'world_lock': 'x'})
        self.assertEqual(precomposed['beat_ladder'], [{'index': 1}])

    def test_compiled_slot_keys_come_back_as_integers(self):
        """compiled_images/videos 在合成器里是 {int: str}，过一趟 JSON 会变成 {"1": str}。
        分步管线按整数下标取用，不还原就在第一拍 KeyError。"""
        state = self._completed_job()
        _dims, _title, precomposed = rp.handoff_to_render(state['job_id'])
        self.assertEqual(precomposed['compiled_images'], {1: 'img one'})
        self.assertEqual(precomposed['compiled_videos'], {1: 'vid one'})

    def test_a_job_without_a_prompt_block_is_rejected(self):
        state = self._ingest()
        state['beats'] = {'beats': [{'id': 'B01'}], 'banned_elements': []}
        rp._save_state(state)
        with self.assertRaises(ValueError):
            rp.handoff_to_render(state['job_id'])


class TestSaveBeats(ReplicaTempRootCase):
    def _job_with_overview(self):
        state = self._ingest()
        payload = {
            'media_metadata': {'duration_sec': 10.0},
            'change_events': [{'event_id': 'E01', 'start': 1.0, 'peak': 2.0, 'end': 3.0,
                               'evidence_frames': ['review_001.png']}],
            'review_sampling': {'frames': [{'index': 1, 'timestamp': 2.0,
                                            'frame_path': '/x/review_001.png'}]},
            'scenes': [],
        }
        with open(os.path.join(rp.job_dir(state['job_id']), 'video_overview.json'),
                  'w', encoding='utf-8') as f:
            json.dump(payload, f)
        return state

    def _beat(self, **kw):
        beat = {
            'id': 'B01', 'start': 0.0, 'end': 10.0, 'stage': 'structural',
            'operation': 'work', 'visual_subject': 'wall', 'visible_details': ['bare'],
            'visible_action': 'a worker works', 'visible_result': 'done',
            'state_before': 'left half bare', 'state_after': 'left half done',
            # 合成器硬闸要求每拍 2~3 道耦合工序、至少两条遗留痕迹（见 frame_state.py）。
            'package_operations': ['cut', 'fit', 'fasten'],
            'persistent_traces': ['dust film', 'screw head rows'], 'workers_present': True,
            'source_event_ids': ['E01'], 'evidence_frames': ['review_001.png'],
        }
        beat.update(kw)
        return beat

    def test_user_edits_are_revalidated_not_just_stored(self):
        """用户拆合了拍就可能拆出新的事件覆盖漏洞——只存不验等于把漏洞放行。"""
        state = self._job_with_overview()
        edited = {'video_duration_sec': 10.0, 'banned_elements': [],
                  'beats': [self._beat(source_event_ids=[])]}
        out = rp.save_beats(state['job_id'], edited)
        codes = [v['code'] for v in out['validation']]
        self.assertIn('event_unbound', codes)
        self.assertTrue(out['beats']['edited_by_user'])

    def test_clean_edit_validates_clean_and_persists(self):
        state = self._job_with_overview()
        out = rp.save_beats(state['job_id'], {
            'video_duration_sec': 10.0, 'banned_elements': ['excavator'],
            'beats': [self._beat()]})
        self.assertEqual([v for v in out['validation'] if v['level'] == 'error'], [])
        reloaded = rp.get_replica_status(state['job_id'])
        self.assertEqual(reloaded['beats']['banned_elements'], ['excavator'])

    def test_saving_an_edited_field_invalidates_its_stale_chinese_mirror(self):
        """前端只回传英文字段，zh 是从上一版原样带回来的——不清理就会出现「中文还是
        旧的、英文已经改了」，而核对的人看的是中文。"""
        state = self._job_with_overview()
        rp.save_beats(state['job_id'], {
            'video_duration_sec': 10.0, 'banned_elements': [],
            'beats': [self._beat(zh={'visible_action': '工人干活', 'visible_result': '完成'})]})

        out = rp.save_beats(state['job_id'], {
            'video_duration_sec': 10.0, 'banned_elements': [],
            'beats': [self._beat(visible_action='a worker sands it down instead',
                                 zh={'visible_action': '工人干活', 'visible_result': '完成'})]})
        self.assertEqual(out['beats']['beats'][0]['zh'], {'visible_result': '完成'})

    def test_empty_beats_are_rejected(self):
        state = self._job_with_overview()
        with self.assertRaises(ValueError):
            rp.save_beats(state['job_id'], {'beats': []})

    def test_unknown_job_is_rejected(self):
        with self.assertRaises(ValueError):
            rp.save_beats('replica_nope', {'beats': [self._beat()]})


class TestDeleteGuard(ReplicaTempRootCase):
    def test_job_id_cannot_escape_the_jobs_directory(self):
        """job_id 直接来自请求体。"""
        outside = os.path.join(self.tmp, 'precious')
        os.makedirs(outside, exist_ok=True)
        with self.assertRaises(ValueError):
            rp.delete_replica_job('../precious')
        self.assertTrue(os.path.isdir(outside))

    def test_deleting_a_real_job_removes_it(self):
        state = self._ingest()
        self.assertTrue(rp.delete_replica_job(state['job_id']))
        self.assertEqual(rp.list_replica_jobs(), [])

    def test_deleting_a_missing_job_is_a_no_op(self):
        self.assertFalse(rp.delete_replica_job('replica_missing'))


class TestVariantBranch(ReplicaTempRootCase):
    def test_variant_gets_its_own_job_and_points_back_at_the_source(self):
        state = self._ingest()
        state['beats'] = {'beats': [{'id': 'B01', 'start': 0.0, 'end': 5.0}],
                          'banned_elements': [], 'pipeline_id': state['job_id']}
        rp._save_state(state)

        variant_doc = {'beats': [{'id': 'B01', 'start': 0.0, 'end': 5.0}],
                       'banned_elements': ['brick'], 'validation': []}
        with patch('prompt_pipeline.reverse.mutate_beats', return_value=variant_doc):
            variant = rp.run_mutate(state, {}, {'axes': ['carrier'], 'brief': '换成巴士'})

        self.assertNotEqual(variant['job_id'], state['job_id'])
        self.assertEqual(variant['variant_of'], state['job_id'])
        self.assertEqual(variant['stage'], 'review_beats')
        # 变体自己不存帧，指回源 job 的目录——不该复制几百张 PNG。
        self.assertEqual(variant['source_frames_dir'], rp.job_dir(state['job_id']))
        self.assertIsNone(variant['video_sha256'],
                          '变体不该参与视频去重，否则会顶掉源 job')

    def test_variant_without_beats_is_rejected(self):
        state = self._ingest()
        with self.assertRaises(ValueError):
            rp.run_mutate(state, {}, {'axes': ['carrier']})


if __name__ == '__main__':
    unittest.main()


class TestComposeFailureTranslation(ReplicaTempRootCase):
    """预检报错要变成「照着能改」的中文，而不是原样贴一串英文规则。"""

    def _failure(self, detail):
        from prompt_pipeline import ComposeFailure
        return ComposeFailure(
            'Structured scene-state preflight rejected the beat ladder before prompt '
            f'generation: {detail}', 'QUALITY_GATE_FAILED')

    def test_each_violation_is_translated_and_mapped_back_to_a_beat_id(self):
        error = self._failure(
            'Beat 9 declares 1 operations; a frame must carry 2 to 3 tightly coupled '
            'operations that share one terminal product. | '
            'Beat 3 declares fewer than two visible persistent traces.')
        beats = {'beats': [{'id': 'B%02d' % i} for i in range(1, 10)]}

        out = str(rp._translate_compose_failure(error, beats))
        self.assertIn('第 9 拍只申报了 1 道工序', out)
        self.assertIn('B09', out)          # 用户在卡点上认得的编号
        self.assertIn('第 3 拍的「遗留痕迹」少于两条', out)
        self.assertIn('B03', out)
        self.assertIn('共 2 项', out)
        # 原始英文规则不再糊在用户脸上。
        self.assertNotIn('tightly coupled', out)

    def test_an_untranslated_rule_is_still_shown_rather_than_swallowed(self):
        error = self._failure('Some rule nobody has translated yet.')
        out = str(rp._translate_compose_failure(error, {'beats': []}))
        self.assertIn('Some rule nobody has translated yet.', out)

    def test_unrelated_errors_pass_through_untouched(self):
        error = RuntimeError('上游网关 503')
        self.assertIs(rp._translate_compose_failure(error, {'beats': []}), error)
