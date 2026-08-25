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
import time
import unittest
from unittest.mock import MagicMock, patch

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


class TestPurgeExtractProducts(ReplicaTempRootCase):
    def test_purge_clears_the_peak_verify_roi_patches(self):
        """重抽帧要把峰值复核的原生特写切片一起清掉。

        切片名是从帧名派生的，换 fps 重抽之后整套帧名都变，不删就是一层层白留在盘上。
        """
        directory = os.path.join(self.tmp, 'job_purge')
        for sub in ('review_frames', 'storyboard', 'roi_patches'):
            os.makedirs(os.path.join(directory, sub), exist_ok=True)
        stale = os.path.join(directory, 'roi_patches', 'review_003_roi_action.jpg')
        with open(stale, 'wb') as f:
            f.write(b'stale-crop')

        rp._purge_extract_products(directory)

        self.assertFalse(os.path.exists(stale))
        self.assertFalse(os.path.isdir(os.path.join(directory, 'roi_patches')))
        self.assertFalse(os.path.isdir(os.path.join(directory, 'review_frames')))


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


class TestExtractProgress(ExtractedJobCase):
    """抽帧这一段此前是纯哑区：脚本只在结束时 print，页面上长视频要静默好几分钟。"""

    def test_frames_landing_on_disk_are_reported_as_progress(self):
        state = self._ingest()
        collage = os.path.join(rp.job_dir(state['job_id']), 'clip_collage.jpg')
        with open(collage, 'wb') as f:
            f.write(b'jpg')
        self._write_overview(state['job_id'], collage)
        frames_dir = os.path.join(rp.job_dir(state['job_id']), 'review_frames')
        events = []

        # 帧必须在 communicate 期间才落盘：run_extract 开头会 _purge_extract_products，
        # 把 review_frames 整个 rmtree 掉。预先铺好的帧在数帧线程跑起来之前就没了——
        # 这也正是真实时序（目录是抽帧脚本自己建的）。
        def fake_communicate(timeout=None):
            os.makedirs(frames_dir, exist_ok=True)
            for i in range(6):
                with open(os.path.join(frames_dir, f'review_{i:03d}.png'), 'wb') as f:
                    f.write(b'png')
            time.sleep(2.4)
            return ('', '')

        def fake_popen(*_a, **_kw):
            proc = MagicMock()
            proc.communicate.side_effect = fake_communicate
            proc.returncode = 0
            return proc

        with patch.object(rp.subprocess, 'Popen', side_effect=fake_popen), \
             patch.object(rp, '_analyzer_script', return_value='/fake/analyze.py'):
            rp.run_extract(state, on_progress=lambda stage, d: events.append((stage, d)),
                           base_fps=2)

        ticks = [d for stage, d in events
                 if stage == 'replica_stage' and d.get('stage') == 'extract' and 'done' in d]
        self.assertTrue(ticks, '抽帧过程中必须报出已落盘的帧数，否则这一段还是哑的')
        # 分母是估的（时长 × 基线 fps），所以百分比封顶在 95%——先走到头再倒退比不动更糟。
        self.assertTrue(all(t['done'] <= t['total'] * 0.95 + 1 for t in ticks))

    def test_without_a_progress_callback_it_stays_on_the_simple_path(self):
        """没人听进度时不该白起一条数文件的线程。"""
        state = self._ingest()
        collage = os.path.join(rp.job_dir(state['job_id']), 'clip_collage.jpg')
        with open(collage, 'wb') as f:
            f.write(b'jpg')
        self._write_overview(state['job_id'], collage)
        with patch.object(rp.subprocess, 'run') as run, \
             patch.object(rp.subprocess, 'Popen') as popen, \
             patch.object(rp, '_analyzer_script', return_value='/fake/analyze.py'):
            run.return_value.returncode = 0
            rp.run_extract(state, base_fps=2)
        popen.assert_not_called()


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

    def test_recompose_fast_path_when_existing_prompt_is_clean(self):
        """当已有 prompt_block 且已无禁用词违规时，重新合成走极速通道，不调用大模型。"""
        state = self._ingest()
        state['beats'] = {'beats': [{'id': 'B01'}], 'banned_elements': ['oven']}
        state['prompt_block'] = 'VIDEO 1: a worker carries woven baskets into the room'
        state['stage'] = 'audit_failed'
        state['validation'] = []
        rp._save_state(state)

        with patch('prompt_pipeline.compose_anchor_and_packet') as mock_anchor, \
             patch('prompt_pipeline.compose_remaining_beats') as mock_beats, \
             patch('server_common.write_library_item') as mock_write:
            out = rp.run_compose(state, {})
            mock_anchor.assert_not_called()
            mock_beats.assert_not_called()
            mock_write.assert_called_once()
            self.assertEqual(out['stage'], 'completed')
            self.assertEqual(out['banned_hits'], [])

    def test_reset_cache_refuses_the_fast_path(self):
        """勾了「清理合成缓存」还走极速通道，等于这个开关不存在。

        极速通道复用的正是上一轮的 prompt_block，而按这个开关的意思就是「那一份是旧
        规则产的」。这条是整个开关最容易被绕过的一处：条件里只要漏掉 reset_cache，
        audit_failed 的任务永远走不到清缓存那一步，且不报任何错。
        """
        state = self._ingest()
        state['beats'] = {'beats': [{'id': 'B01'}], 'banned_elements': ['oven']}
        state['prompt_block'] = 'VIDEO 1: a worker carries woven baskets into the room'
        state['stage'] = 'audit_failed'
        state['validation'] = []
        rp._save_state(state)

        with patch('prompt_pipeline.compose_anchor_and_packet',
                   return_value={'title': 't'}) as mock_anchor, \
             patch('prompt_pipeline.compose_remaining_beats',
                   return_value='VIDEO 1: a lone worker trowels the wall'), \
             patch('prompt_pipeline.clear_compose_caches',
                   return_value={'checkpoint': 1, 'packets': 2}) as mock_clear, \
             patch('server_common.write_library_item'):
            rp.run_compose(state, {}, reset_cache=True)
        mock_anchor.assert_called_once()
        mock_clear.assert_called_once()

    def test_reset_cache_clears_all_three_stores_and_skips_the_prefill(self):
        """三处缓存少清一处都等于没清：留下的那一处会把旧 Phase 1 产物带回这一轮。"""
        state = self._ingest()
        state['beats'] = {'beats': [{'id': 'B01'}], 'banned_elements': []}
        state['validation'] = []
        rp._save_state(state)
        # 磁盘上留一份 Phase 1 产物：不清的话它会被预填进断点存档。
        compose_path = rp._compose_state_path(state['job_id'])
        os.makedirs(os.path.dirname(compose_path), exist_ok=True)
        with open(compose_path, 'w', encoding='utf-8') as f:
            json.dump({'packet': {'x': 1}, 'beat_ladder': [{'operation': 'repair'}],
                       'title': 'old title'}, f)

        # 合成成功后 run_compose 会重新写一份自己的 compose_state.json，所以事后查文件
        # 在不在没有意义——要查的是「Phase 1 开跑的那一刻，旧的那一份已经不在了」。
        seen = {}

        def _phase1(*_a, **_kw):
            seen['stale_artifact_present'] = os.path.exists(compose_path)
            return {'title': 't'}

        with patch('prompt_pipeline.compose_anchor_and_packet', side_effect=_phase1), \
             patch('prompt_pipeline.compose_remaining_beats', return_value='IMAGE 1: a hut'), \
             patch('prompt_pipeline.clear_compose_caches',
                   return_value={'checkpoint': 1, 'packets': 3}) as mock_clear, \
             patch('prompt_pipeline.save_compose_checkpoint') as mock_prefill, \
             patch('server_common.write_library_item'):
            rp.run_compose(state, {}, reset_cache=True)
        mock_clear.assert_called_once()
        # 指纹必须是从本次 dims 算出来的非空串，不是 None——传错等于清了个不存在的键。
        self.assertTrue(mock_clear.call_args.args[0])
        mock_prefill.assert_not_called()
        self.assertIs(seen['stale_artifact_present'], False)

    def test_compose_without_the_flag_still_reuses_the_cache(self):
        """默认不清：断点续传省的是几分钟大模型钱，不该被这次改动顺手关掉。"""
        state = self._ingest()
        state['beats'] = {'beats': [{'id': 'B01'}], 'banned_elements': []}
        state['validation'] = []
        rp._save_state(state)
        compose_path = rp._compose_state_path(state['job_id'])
        os.makedirs(os.path.dirname(compose_path), exist_ok=True)
        with open(compose_path, 'w', encoding='utf-8') as f:
            json.dump({'packet': {'x': 1}, 'beat_ladder': [{'operation': 'repair'}],
                       'title': 'old title'}, f)

        with patch('prompt_pipeline.compose_anchor_and_packet', return_value={'title': 't'}), \
             patch('prompt_pipeline.compose_remaining_beats', return_value='IMAGE 1: a hut'), \
             patch('prompt_pipeline.clear_compose_caches') as mock_clear, \
             patch('prompt_pipeline.save_compose_checkpoint') as mock_prefill, \
             patch('server_common.write_library_item'):
            rp.run_compose(state, {})
        mock_clear.assert_not_called()
        mock_prefill.assert_called_once()

    def test_advance_forwards_the_reset_cache_flag(self):
        """开关是从 payload 一路传进来的。断在 advance 这一层，前端勾了也白勾。"""
        state = self._ingest()
        state['beats'] = {'beats': [{'id': 'B01'}], 'banned_elements': []}
        state['validation'] = []
        rp._save_state(state)
        with patch.object(rp, 'run_compose') as mock_compose:
            rp.advance_replica_job({}, state['job_id'], action='approve',
                                   payload={'reset_cache': True})
        self.assertIs(mock_compose.call_args.kwargs['reset_cache'], True)

        with patch.object(rp, 'run_compose') as mock_compose:
            rp.advance_replica_job({}, state['job_id'], action='approve', payload={})
        self.assertIs(mock_compose.call_args.kwargs['reset_cache'], False)

    def test_get_replica_status_auto_heals_audit_failed_when_clean(self):
        """读取任务状态时，若已无违规，自动自愈 audit_failed 状态为 completed 并落库。"""
        state = self._ingest()
        state['beats'] = {'beats': [{'id': 'B01'}], 'banned_elements': ['oven']}
        state['prompt_block'] = 'VIDEO 1: a worker places woven mats on the floor'
        state['stage'] = 'audit_failed'
        state['banned_hits'] = ['oven']
        state['validation'] = []
        rp._save_state(state)

        with patch('server_common.write_library_item') as mock_write:
            out = rp.get_replica_status(state['job_id'])
            self.assertEqual(out['stage'], 'completed')
            self.assertEqual(out['banned_hits'], [])
            mock_write.assert_called_once()

    def test_recluster_invalidates_prompt_block_and_compose_state(self):
        """重跑聚类时必须清除旧提示词与 compose_state.json，防止后续合成秒进快速通道复用旧提示词。"""
        state = self._ingest()
        state['prompt_block'] = 'VIDEO 1: old prompt text'
        state['stage'] = 'completed'
        c_path = rp._compose_state_path(state['job_id'])
        with open(c_path, 'w', encoding='utf-8') as f:
            json.dump({'packet': {}, 'beat_ladder': []}, f)
        state['compose_state_path'] = c_path
        rp._save_state(state)

        with patch('prompt_pipeline.reverse.extract_frame_facts', return_value={'facts': []}), \
             patch('prompt_pipeline.reverse.verify_peak_frames', return_value={'facts': []}), \
             patch('prompt_pipeline.reverse.cluster_beats', return_value={'beats': [{'id': 'B01'}], 'validation': []}), \
             patch('prompt_pipeline.reverse.translate_beats', return_value=1):
            out = rp.run_reverse(state, {})

        self.assertEqual(out['stage'], 'review_beats')
        self.assertIsNone(out['prompt_block'])
        self.assertIsNone(out['compose_state_path'])
        self.assertFalse(os.path.exists(c_path))

        # 后续再次点击合成提示词，必须真实调用大模型合成器，绝不能走快速通道秒成功
        with patch('prompt_pipeline.compose_anchor_and_packet', return_value={'title': 'T', 'packet': {}, 'beat_ladder': []}) as mock_anchor, \
             patch('prompt_pipeline.compose_remaining_beats', return_value='===PROMPTS===\nVIDEO 1: newly generated') as mock_beats, \
             patch('server_common.write_library_item'):
            composed_out = rp.run_compose(out, {})
            mock_anchor.assert_called_once()
            mock_beats.assert_called_once()
            self.assertIn('newly generated', composed_out['prompt_block'])

    def test_save_beats_invalidates_prompt_block_when_beats_change(self):
        """在 review_beats 阶段保存修改后的节拍，必须清理已有的旧 prompt_block。"""
        state = self._ingest()
        state['prompt_block'] = 'VIDEO 1: old prompt'
        state['stage'] = 'review_beats'
        rp._save_state(state)

        # 准备 overview.json
        ov_path = os.path.join(rp.job_dir(state['job_id']), 'video_overview.json')
        with open(ov_path, 'w', encoding='utf-8') as f:
            json.dump({'review_sampling': {'frames': []}, 'change_events': []}, f)

        saved = rp.save_beats(state['job_id'], {'beats': [{'id': 'B01', 'start': 0, 'end': 2}]})
        self.assertIsNone(saved['prompt_block'])

    def test_autofix_and_autobalance_invalidate_prompt_block(self):
        """AI 修复或自动平衡时序后，必须清理旧 prompt_block。"""
        state = self._ingest()
        state['prompt_block'] = 'VIDEO 1: old prompt'
        state['beats'] = {'beats': [{'id': 'B01', 'start': 0, 'end': 8, 'camera_movement': 'static'}]}
        state['stage'] = 'review_beats'
        rp._save_state(state)

        ov_path = os.path.join(rp.job_dir(state['job_id']), 'video_overview.json')
        with open(ov_path, 'w', encoding='utf-8') as f:
            json.dump({'review_sampling': {'frames': []}, 'change_events': []}, f)

        with patch('prompt_pipeline.reverse.autofix_beats', return_value=({'beats': [{'id': 'B01'}]}, 1)), \
             patch('prompt_pipeline.reverse.translate_beats', return_value=1):
            fixed_state, _ = rp.autofix_job_beats({}, state['job_id'])
            self.assertIsNone(fixed_state['prompt_block'])

        state['prompt_block'] = 'VIDEO 1: old prompt'
        rp._save_state(state)
        with patch('prompt_pipeline.reverse.autobalance_beats', return_value=({'beats': [{'id': 'B01'}]}, 1)), \
             patch('prompt_pipeline.reverse.translate_beats', return_value=1):
            balanced_state, _ = rp.autobalance_job_beats({}, state['job_id'])
            self.assertIsNone(balanced_state['prompt_block'])

    def test_autofix_with_nothing_to_fix_keeps_the_prompt_block_and_the_stage(self):
        """0 硬伤时按下「AI 修复硬伤」，此前是一次纯亏损的返工。

        `autofix_beats` 第一件事就是「没有 error 就原样返回」，而单拍工艺体检出的全是
        warn。于是模型一次都没被调用，外层却照样重跑一遍翻译（真花钱）、清掉已经合成好
        的 prompt_block、把 stage 从 completed 退回卡点，最后弹一句「已解决全部硬伤」。
        用户看到的是修完了，实际一个字没改，还得重合成一次。
        """
        state = self._ingest()
        state['prompt_block'] = 'VIDEO 1: composed prompt'
        state['beats'] = {'beats': [{'id': 'B01', 'start': 0, 'end': 8}]}
        state['stage'] = 'completed'
        rp._save_state(state)

        ov_path = os.path.join(rp.job_dir(state['job_id']), 'video_overview.json')
        with open(ov_path, 'w', encoding='utf-8') as f:
            json.dump({'review_sampling': {'frames': []}, 'change_events': []}, f)

        translate = MagicMock(return_value=1)
        with patch('prompt_pipeline.reverse.autofix_beats',
                   return_value=({'beats': [{'id': 'B01'}]}, 0)), \
             patch('prompt_pipeline.reverse.translate_beats', translate):
            out, count = rp.autofix_job_beats({}, state['job_id'])

        self.assertEqual(count, 0)
        self.assertEqual(out['prompt_block'], 'VIDEO 1: composed prompt')
        self.assertEqual(out['stage'], 'completed')
        translate.assert_not_called()



class TestComposedBlockHasNoDocumentWrapper(ReplicaTempRootCase):
    """合成器返回的是一份**带标记的文档**，不是提示词正文。

    `compose_remaining_beats` 的返回值长这样：
        ===TITLE=== / ===THEME=== / ===PROMPTS=== <正文> / ===AUDIT=== <一句说明>
    激发那条线在 server.py 里过一道 parse_sections 才落库；复刻这条线 2026-08-15
    之前是整份原样存进 state['prompt_block']。后果不只是"页面上多两行"：
    `_parse_prompt_slots` 的尾段正则是开放式的（…|\\Z），末尾那句
    「skill 直出模式：…」会被吃进**最后一段视频**的正文，跟着这一拍送进 i2v。
    """

    DOC = ('===TITLE===\n石屋工作室\n===THEME===\n把废弃石屋修成工作室\n===PROMPTS===\n'
           '图片提示词\n\n图片 1:\n一间石屋\n\n图片 2:\n屋顶已封\n\n'
           '视频提示词\n\n视频 1:\n工人抹平墙面\n'
           '===AUDIT===\nskill 直出模式：文本阶段无审查、无重写，批量直出+确定性修复一次成型。')

    def _compose(self, composed):
        state = self._ingest()
        state['beats'] = {'beats': [{'id': 'B01'}], 'banned_elements': []}
        state['validation'] = []
        rp._save_state(state)
        with patch('prompt_pipeline.compose_anchor_and_packet', return_value={'title': '石屋工作室'}), \
             patch('prompt_pipeline.compose_remaining_beats', return_value=composed), \
             patch('server_common.write_library_item'):
            return rp.run_compose(state, {})

    def test_head_and_tail_are_stripped_before_the_block_is_stored(self):
        out = self._compose(self.DOC)
        block = out['prompt_block']
        self.assertNotIn('===', block)
        self.assertNotIn('直出模式', block)
        self.assertNotIn('石屋工作室', block)      # TITLE 段
        self.assertNotIn('把废弃石屋修成工作室', block)  # THEME 段
        self.assertIn('工人抹平墙面', block)

    def test_the_audit_note_does_not_ride_along_on_the_last_video(self):
        from prompt_pipeline import _parse_prompt_slots
        out = self._compose(self.DOC)
        _images, videos = _parse_prompt_slots(out['prompt_block'])
        self.assertEqual(videos[1]['body'], '工人抹平墙面')

    def test_a_stale_job_is_cleaned_when_its_state_is_read_back(self):
        """存量任务的 state 文件里存着整份文档，不做数据迁移——读的时候剥。"""
        state = self._compose(self.DOC)
        state['prompt_block'] = self.DOC          # 把旧数据写回去，模拟存量任务
        rp._save_state(state)
        reloaded = rp._load_state(state['job_id'])
        self.assertNotIn('直出模式', reloaded['prompt_block'])
        self.assertIn('工人抹平墙面', reloaded['prompt_block'])

    def test_an_already_clean_block_is_not_touched(self):
        clean = '图片提示词\n图片 1:\n一间石屋\n\n视频提示词\n视频 1:\n工人抹平墙面\n'
        self.assertEqual(self._compose(clean)['prompt_block'], clean)


class TestComposeGateRevalidates(ReplicaTempRootCase):
    """合成卡点不信任一份可能来自旧版校验器的 validation 快照。

    2026-08-12：变体阶梯被按原片口径判了 24 项「缺少 evidence_frames」，校验器改好之后
    那份写死在 state 里的旧结论仍然拦着整单——快照过期比阶梯脏更难查。
    """

    def _variant_job(self):
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
        state['variant_of'] = 'replica_src'
        state['beats'] = {
            'video_duration_sec': 10.0, 'banned_elements': [],
            'variant_of': 'replica_src', 'mutation_axes': ['carrier'],
            'beats': [{
                'id': 'B01', 'start': 0.0, 'end': 10.0, 'stage': 'structural',
                'operation': 'work', 'visual_subject': 'a rusted bus shell',
                'visible_details': ['bare'], 'visible_action': 'a worker works',
                'visible_result': 'done', 'state_before': 'left half bare',
                'state_after': 'left half done',
                'package_operations': ['cut', 'fit', 'fasten'],
                'persistent_traces': ['dust film', 'screw head rows'],
                'workers_present': True, 'source_event_ids': ['E01'],
                'reference_frames': ['review_001.png'],
            }],
        }
        # 旧版校验器留下的快照：整拍都被判缺字段。
        state['validation'] = [
            {'level': 'error', 'code': 'missing_beat_field', 'message': 'B01 缺少字段 `evidence_frames`'},
            {'level': 'error', 'code': 'no_evidence', 'message': 'B01 没有证据帧，但它断言了动作与结果'},
        ]
        rp._save_state(state)
        return state

    def test_a_stale_error_snapshot_is_recomputed_and_cleared(self):
        state = self._variant_job()
        with patch('prompt_pipeline.compose_anchor_and_packet', return_value={'title': 't'}), \
             patch('prompt_pipeline.compose_remaining_beats',
                   return_value='VIDEO 1: a worker trowels the wall'), \
             patch('server_common.write_library_item'):
            out = rp.run_compose(state, {})
        self.assertEqual(out['stage'], 'completed')
        self.assertEqual([v for v in out['validation'] if v['level'] == 'error'], [])

    def test_a_real_error_still_blocks_after_recomputation(self):
        """重算不是放行：阶梯真脏的时候照样拦下。

        这里用阶段逆行当「真脏」的样本，而不是漏认领事件：后者对变体已降级为 warn
        （见 `test_an_unbound_event_is_only_a_warning_on_a_variant`）。
        """
        state = self._variant_job()
        head = state['beats']['beats'][0]
        head['stage'] = 'reveal'
        tail = dict(head)
        tail.update({'id': 'B02', 'start': 10.0, 'end': 12.0,
                     'stage': 'demolition', 'source_event_ids': []})
        state['beats']['beats'].append(tail)
        state['beats']['video_duration_sec'] = 12.0
        rp._save_state(state)
        with self.assertRaises(RuntimeError) as ctx:
            rp.run_compose(state, {})
        self.assertIn('B02', str(ctx.exception))

    def test_an_unbound_event_is_only_a_warning_on_a_variant(self):
        """变体不对原片的事件名册负责，漏认领不该拦下合成。

        `overview` 是从源 job 复制过来的**原片**事件表。变体一改拍数或时间窗就报
        `event_unbound`，而这条错任何文字改写都修不掉——判成 error 的后果是「AI 修复
        硬伤」每轮重写全表、跑满轮次仍然剩着，也就是「越修越坏」。
        """
        state = self._variant_job()
        state['beats']['beats'][0]['source_event_ids'] = []
        rp._save_state(state)
        with patch('prompt_pipeline.compose_anchor_and_packet', return_value={'title': 't'}),              patch('prompt_pipeline.compose_remaining_beats',
                   return_value='VIDEO 1: a worker trowels the wall'),              patch('server_common.write_library_item'):
            out = rp.run_compose(state, {})
        self.assertEqual(out['stage'], 'completed')
        self.assertEqual([v for v in out['validation'] if v['level'] == 'error'], [])
        self.assertTrue(any(v['code'] == 'event_unbound' and v['level'] == 'warn'
                            for v in out['validation']))


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


class TestPublishToProject(ReplicaTempRootCase):
    """「存入项目并打开激发结果」。

    复刻页的终点从"送去分步管线渲染"改成"落成一个项目"：项目才是所有下游动作
    （分步合成 / 一键合成 / 手动编辑 / 帧序列）的共同起点，只通向分步管线那一条路
    把用户锁死在一种渲染方式上。

    这条路径上两样东西缺一不可，缺了它们那条记录只是工作台上的一行标题：
      · project_key —— 媒体目录的命名空间（帧、视频、manifest 全挂它下面），
        而且必须**跨多次点击稳定**，否则第二次点会把已渲出的帧留在无人打开的目录里；
      · prompt_slots —— 槽位契约的权威解析，前端优先消费它。
    """

    BLOCK = ('图片提示词\n图片 1:\n一间石屋\n\n图片 2:\n屋顶已封\n\n'
             '视频提示词\n视频 1:\n工人抹平墙面\n')

    def _completed_job(self):
        state = self._ingest()
        state['beats'] = {'beats': [{'id': 'B01'}], 'banned_elements': []}
        state['validation'] = []
        rp._save_state(state)
        with patch('prompt_pipeline.compose_anchor_and_packet', return_value={'title': '石屋工作室'}), \
             patch('prompt_pipeline.compose_remaining_beats', return_value=self.BLOCK), \
             patch('server_common.write_library_item'):
            rp.run_compose(state, {})
        return state

    def test_the_published_item_is_a_real_project(self):
        state = self._completed_job()
        with patch('server_common.write_library_item') as write_item:
            item = rp.publish_to_project(state['job_id'])
        write_item.assert_called_once()
        self.assertEqual(item['id'], state['job_id'])
        self.assertTrue(item['project_key'].startswith('run_'))
        self.assertEqual([s['index'] for s in item['prompt_slots']['images']], [1, 2])
        self.assertEqual([s['index'] for s in item['prompt_slots']['videos']], [1])
        self.assertEqual(item['image_count'], 2)
        self.assertEqual(item['prompt_block'], self.BLOCK)

    def test_the_project_key_is_stable_across_repeated_saves(self):
        """再点一次「存入项目」不能换命名空间——那会把已渲出的帧丢在无人打开的目录里。"""
        state = self._completed_job()
        with patch('server_common.write_library_item'):
            first = rp.publish_to_project(state['job_id'])
            second = rp.publish_to_project(state['job_id'])
        self.assertEqual(first['project_key'], second['project_key'])
        self.assertEqual(rp._load_state(state['job_id'])['project_key'],
                         first['project_key'])

    def test_a_blocked_job_cannot_become_a_project(self):
        """门禁没过的提示词不该在工作台上露面，更不该被下游当成可渲染的项目。"""
        state = self._ingest()
        state['beats'] = {'beats': [{'id': 'B01'}], 'banned_elements': ['excavator']}
        state['validation'] = []
        rp._save_state(state)
        with patch('prompt_pipeline.compose_anchor_and_packet', return_value={'title': 't'}), \
             patch('prompt_pipeline.compose_remaining_beats',
                   return_value='视频提示词\n视频 1:\n一台 EXCAVATOR 转进画面\n'), \
             patch('server_common.write_library_item'):
            rp.run_compose(state, {})
        with patch('server_common.write_library_item') as write_item:
            with self.assertRaises(ValueError) as ctx:
                rp.publish_to_project(state['job_id'])
        self.assertIn('excavator', str(ctx.exception))
        write_item.assert_not_called()

    def test_a_failed_write_is_reported_not_swallowed(self):
        """合成收尾时入库失败可以吞（那只是附赠）；用户手点这个按钮时不能吞——
        静默失败会把他送进一个空的结果页，还以为是页面没刷新。"""
        state = self._completed_job()
        with patch('server_common.write_library_item', side_effect=OSError('磁盘满')):
            with self.assertRaises(OSError):
                rp.publish_to_project(state['job_id'])


# 节拍相关用例的公共夹具。单独一层是为了让撤销那组用例能复用它，而不是去继承
# TestSaveBeats——继承会把保存那一组的用例连带再跑一遍，跑绿的数字是假的。
class BeatsFixtureCase(ReplicaTempRootCase):
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


class TestSaveBeats(BeatsFixtureCase):
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


class TestUndoBeats(BeatsFixtureCase):
    """整份覆盖必须留一版可回退。

    autofix / 工艺精修 / 自动平衡 / 重跑聚类都是整份覆盖，此前磁盘上不留任何旧版：
    模型把一条手工调好的阶梯改坏了，只能重跑 Pass B——重新付钱，结果还不一样。
    """

    def test_overwriting_leaves_the_previous_version_on_disk(self):
        state = self._job_with_overview()
        rp.save_beats(state['job_id'], {'video_duration_sec': 10.0, 'banned_elements': [],
                                        'beats': [self._beat(operation='first')]})
        # 第一次写的时候盘上还没有旧版，不该凭空造一个。
        self.assertFalse(os.path.exists(rp._beats_prev_path(state['job_id'])))

        rp.save_beats(state['job_id'], {'video_duration_sec': 10.0, 'banned_elements': [],
                                        'beats': [self._beat(operation='second')]})
        self.assertTrue(os.path.exists(rp._beats_prev_path(state['job_id'])))

    def test_undo_restores_the_previous_ladder(self):
        state = self._job_with_overview()
        job_id = state['job_id']
        for op in ('first', 'second'):
            rp.save_beats(job_id, {'video_duration_sec': 10.0, 'banned_elements': [],
                                   'beats': [self._beat(operation=op)]})
        out = rp.undo_beats(job_id)
        self.assertEqual(out['beats']['beats'][0]['operation'], 'first')
        # 磁盘也要跟着回退，不能只改内存里那一份。
        reloaded = rp.get_replica_status(job_id)
        self.assertEqual(reloaded['beats']['beats'][0]['operation'], 'first')

    def test_undo_is_itself_undoable(self):
        """做成对调而不是单向恢复：误点一次撤销就把刚跑完的那一轮永久丢了，
        而那正是这个功能要防的事，不该由它自己再制造一次。"""
        state = self._job_with_overview()
        job_id = state['job_id']
        for op in ('first', 'second'):
            rp.save_beats(job_id, {'video_duration_sec': 10.0, 'banned_elements': [],
                                   'beats': [self._beat(operation=op)]})
        rp.undo_beats(job_id)
        out = rp.undo_beats(job_id)
        self.assertEqual(out['beats']['beats'][0]['operation'], 'second')

    def test_undo_invalidates_the_prompt_composed_from_the_ladder_it_replaced(self):
        """撤销之后还留着旧提示词，用户会拿着一份跟当前阶梯对不上的东西去交付，
        而页面上看不出任何异样。"""
        state = self._job_with_overview()
        job_id = state['job_id']
        for op in ('first', 'second'):
            rp.save_beats(job_id, {'video_duration_sec': 10.0, 'banned_elements': [],
                                   'beats': [self._beat(operation=op)]})
        cur = rp._load_state(job_id)
        cur['prompt_block'] = 'VIDEO 1: ...'
        cur['stage'] = 'completed'
        rp._save_state(cur)

        out = rp.undo_beats(job_id)
        self.assertFalse(out.get('prompt_block'))
        self.assertEqual(out['stage'], 'review_beats')

    def test_undo_without_a_previous_version_is_refused(self):
        state = self._job_with_overview()
        rp.save_beats(state['job_id'], {'video_duration_sec': 10.0, 'banned_elements': [],
                                        'beats': [self._beat()]})
        with self.assertRaises(ValueError):
            rp.undo_beats(state['job_id'])

    def test_status_tells_the_page_whether_undo_is_available(self):
        """没有可回退版本时摆一个点了必然报错的按钮，比不摆更糟。"""
        state = self._job_with_overview()
        job_id = state['job_id']
        rp.save_beats(job_id, {'video_duration_sec': 10.0, 'banned_elements': [],
                               'beats': [self._beat(operation='first')]})
        self.assertFalse(rp.get_replica_status(job_id)['beats_undo_available'])
        rp.save_beats(job_id, {'video_duration_sec': 10.0, 'banned_elements': [],
                               'beats': [self._beat(operation='second')]})
        self.assertTrue(rp.get_replica_status(job_id)['beats_undo_available'])


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


class TestOptimizationPlanCoverage(ReplicaTempRootCase):
    """测试复刻管线主优化计划中的核心机制与防线。"""

    def test_job_id_validation_rejects_traversal(self):
        """防止路径穿越非法字符。"""
        invalid_ids = ['../foo', 'job/123', 'job\\123', 'a' * 65, 'ab', 'job*id']
        for bad_id in invalid_ids:
            with self.assertRaises(ValueError):
                rp.validate_job_id(bad_id)

    def test_summary_index_fast_loading_and_state_slimming(self):
        """验证 .summary.json 索引生成与 list_replica_jobs 高速读取。"""
        state = self._ingest()
        state['beats'] = {'beats': [{'id': 'B01', 'operation': '拆除'}]}
        state['prompt_block'] = 'Prompt text...'
        rp._save_state(state)

        # 验证 summary 文件存在
        summary_path = os.path.join(rp.job_dir(state['job_id']), '.summary.json')
        self.assertTrue(os.path.exists(summary_path))

        # 验证 state 文件中 beats 被单独保存为 timelapse_beats.json (State Slimming)
        beats_path = os.path.join(rp.job_dir(state['job_id']), 'timelapse_beats.json')
        self.assertTrue(os.path.exists(beats_path))

        # 验证 list_replica_jobs 能读出列表并包含基本元数据
        jobs = rp.list_replica_jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]['job_id'], state['job_id'])
        self.assertEqual(jobs[0]['beat_count'], 1)

    def test_delete_protection_for_source_jobs_with_variants(self):
        """验证存在变体依赖时禁止误删源任务。"""
        source = self._ingest()
        variant_dir = os.path.join(rp.jobs_root(), 'replica_var123456')
        os.makedirs(variant_dir, exist_ok=True)
        var_state = {
            'job_id': 'replica_var123456',
            'stage': 'review_beats',
            'variant_of': source['job_id'],
            'created_at': '2026-08-14T00:00:00',
        }
        rp._save_state(var_state)

        # 未使用 force=True 时应当报错拦截
        with self.assertRaises(ValueError) as ctx:
            rp.delete_replica_job(source['job_id'], force=False)
        self.assertIn('变体', str(ctx.exception))

        # 使用 force=True 可以成功删除
        self.assertTrue(rp.delete_replica_job(source['job_id'], force=True))

    def test_compose_failed_state_transition(self):
        """合成报错时必须进入 compose_failed 终态，不能悬挂在 compose。"""
        state = self._ingest()
        state['stage'] = 'review_beats'
        state['beats'] = {'beats': [{'id': 'B01', 'operation': '拆除'}]}
        rp._save_state(state)

        with patch('replica_pipeline.run_audit', side_effect=RuntimeError('LLM Context Exceeded')):
            with patch('prompt_pipeline.compose_anchor_and_packet', return_value={'slots': {'images': ['a']}, 'anchor_prompt': 'p', 'packet': 'pk'}):
                with patch('prompt_pipeline.compose_remaining_beats', return_value={'blocks': ['b1']}):
                    with self.assertRaises(RuntimeError):
                        rp.run_compose(state, {})

        loaded = rp._load_state(state['job_id'])
        self.assertEqual(loaded['stage'], 'compose_failed')
        self.assertIn('LLM Context Exceeded', loaded['error'])

    def test_mutate_failed_state_transition(self):
        """变体改写报错时必须进入 mutate_failed 终态，不能悬挂在 mutate_beats。"""
        source = self._ingest()
        source['beats'] = {'beats': [{'id': 'B01', 'operation': '拆除'}]}
        rp._save_state(source)

        with patch('prompt_pipeline.reverse.mutate_beats', side_effect=ValueError('Mutation failed')):
            with self.assertRaises(ValueError):
                rp.run_mutate(source, {}, {'axes': ['carrier']})

        # 检查新派生的 variant job 状态
        all_jobs = rp.list_replica_jobs()
        variant_jobs = [j for j in all_jobs if j.get('variant_of') == source['job_id']]
        self.assertEqual(len(variant_jobs), 1)
        var_loaded = rp._load_state(variant_jobs[0]['job_id'])
        self.assertEqual(var_loaded['stage'], 'mutate_failed')

    def test_advance_valid_actions_enforcement(self):
        """验证 advance_replica_job 的合法 action 校验白名单。"""
        state = self._ingest()
        with self.assertRaises(ValueError) as ctx:
            rp.advance_replica_job({}, state['job_id'], action='hack_action')
        self.assertIn('不支持的 action', str(ctx.exception))

    def test_advance_autofix_action(self):
        """验证 advance_replica_job(action='autofix') 成功调用 autofix_job_beats 并更新状态。"""
        import prompt_pipeline as pp
        from prompt_pipeline import reverse
        state = self._ingest()
        jdir = rp.job_dir(state['job_id'])
        # 写入 overview 和初始 beats
        with open(os.path.join(jdir, 'video_overview.json'), 'w') as f:
            json.dump({'media_metadata': {'duration_sec': 10.0}, 'change_events': []}, f)
        beats = {
            'video_duration_sec': 10.0,
            'beats': [
                {'id': 'B01', 'start': 0.0, 'end': 5.0, 'stage': 'surface',
                 'package_operations': ['plaster', 'paint'], 'persistent_traces': ['mark1', 'mark2']},
                {'id': 'B02', 'start': 5.0, 'end': 10.0, 'stage': 'structural',
                 'package_operations': ['frame', 'weld'], 'persistent_traces': ['mark1', 'mark2']},
            ]
        }
        rp._write_beats(state, beats)
        rp._save_state(state)

        fixed_reply = json.dumps({
            'beats': [
                {'id': 'B01', 'stage': 'surface'},
                {'id': 'B02', 'stage': 'floor'},
            ]
        })
        with patch.object(pp, '_chat', return_value=fixed_reply), \
             patch.object(reverse, 'translate_beats', return_value=2):
            progress_calls = []
            res_state = rp.advance_replica_job(
                {}, state['job_id'], action='autofix',
                on_progress=lambda t, d: progress_calls.append((t, d))
            )
            self.assertEqual(res_state['stage'], 'review_beats')
            self.assertEqual(res_state['beats']['beats'][1]['stage'], 'floor')
            self.assertTrue(any(isinstance(d, dict) and 'AI 修复完成' in d.get('message', '') for _, d in progress_calls))

    def test_gc_replica_job_removes_intermediate_and_tmp_files(self):
        """验证 gc_replica_job 清理临时文件。"""
        state = self._ingest()
        jdir = rp.job_dir(state['job_id'])
        # 写入临时垃圾文件
        tmp_file = os.path.join(jdir, 'test.tmp')
        bad_reply = os.path.join(jdir, '.bad_reply_1.json')
        pass_b_dir = os.path.join(jdir, '.pass_b_sheets')
        os.makedirs(pass_b_dir, exist_ok=True)
        with open(tmp_file, 'w') as f: f.write('temp')
        with open(bad_reply, 'w') as f: f.write('bad')

        res = rp.gc_replica_job(state['job_id'])
        self.assertFalse(os.path.exists(tmp_file))
        self.assertFalse(os.path.exists(bad_reply))
        self.assertFalse(os.path.exists(pass_b_dir))
        self.assertIn('test.tmp', res['cleaned'])


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


class TestReplicaGovernance(ReplicaTempRootCase):
    """测试治理能力：重命名、归档瘦身、级联删除与注意力状态计算。"""

    def test_attention_calculation(self):
        self.assertEqual(rp.attention_of('confirm_cost'), 'waiting_you')
        self.assertEqual(rp.attention_of('review_beats'), 'waiting_you')
        self.assertEqual(rp.attention_of('audit_failed'), 'waiting_you')
        self.assertEqual(rp.attention_of('compose_failed'), 'waiting_you')
        self.assertEqual(rp.attention_of('mutate_failed'), 'waiting_you')
        self.assertEqual(rp.attention_of('completed'), 'done')
        self.assertEqual(rp.attention_of('archived'), 'archived')
        self.assertEqual(rp.attention_of('review_beats', has_active_task=True), 'running')
        self.assertEqual(rp.attention_of('ingest'), 'stalled')
        self.assertEqual(rp.attention_of('cancelled'), 'stalled')

    def test_rename_and_title_locked(self):
        state = self._ingest(b'fake', 'my_video.mp4')
        job_id = state['job_id']
        self.assertEqual(state['title'], 'my_video')
        self.assertFalse(state.get('title_locked'))

        # 人工重命名
        updated = rp.rename_replica_job(job_id, '人工锁定的自拟标题')
        self.assertEqual(updated['title'], '人工锁定的自拟标题')
        self.assertTrue(updated['title_locked'])

        # 检查 summary 也更新了
        summaries = rp.list_replica_jobs()
        row = next(r for r in summaries if r['job_id'] == job_id)
        self.assertEqual(row['title'], '人工锁定的自拟标题')
        self.assertTrue(row['title_locked'])

    def test_archive_replica_job(self):
        state = self._ingest(b'fake-video', 'source.mp4')
        job_id = state['job_id']
        jdir = rp.job_dir(job_id)

        # 伪造重资产
        rf_dir = os.path.join(jdir, 'review_frames')
        os.makedirs(rf_dir, exist_ok=True)
        with open(os.path.join(rf_dir, 'frame_001.png'), 'w') as f: f.write('big frame')

        sheet_dir = os.path.join(jdir, '.pass_b_sheets')
        os.makedirs(sheet_dir, exist_ok=True)
        with open(os.path.join(sheet_dir, 'sheet.jpg'), 'w') as f: f.write('sheet')

        collage_file = os.path.join(jdir, 'source_collage.jpg')
        with open(collage_file, 'w') as f: f.write('big collage')

        thumb_file = os.path.join(jdir, 'source_collage_thumb.jpg')
        with open(thumb_file, 'w') as f: f.write('thumb')

        beats_file = os.path.join(jdir, 'timelapse_beats.json')
        with open(beats_file, 'w') as f: f.write('{"beats": [{"id": "B01"}]}')

        # 归档
        archived = rp.archive_replica_job(job_id)
        self.assertEqual(archived['stage'], 'archived')
        self.assertTrue(archived['archived'])

        # 验证重资产已被删除
        self.assertFalse(os.path.exists(rf_dir))
        self.assertFalse(os.path.exists(sheet_dir))
        self.assertFalse(os.path.exists(collage_file))
        self.assertFalse(os.path.exists(state['video_path']))

        # 验证节拍与缩略图依然保留
        self.assertTrue(os.path.exists(thumb_file))
        self.assertTrue(os.path.exists(beats_file))
        self.assertTrue(os.path.exists(rp._state_path(job_id)))

    def test_delete_cascades_task_files(self):
        state = self._ingest(b'fake', 'test_delete.mp4')
        job_id = state['job_id']
        tid = f'replica_test_task_{job_id}'

        # 在 server_common 建立 fake 任务
        tasks_dir = os.path.join(self.tmp, 'tasks')
        os.makedirs(tasks_dir, exist_ok=True)
        with patch.object(server_common, 'TASKS_DIR', tasks_dir):
            server_common.get_or_create_task(tid, dimensions={'type': 'replica_extract', 'replica_job_id': job_id})
            tpaths = server_common._task_paths(tid, tasks_dir=tasks_dir)
            self.assertTrue(os.path.exists(tpaths[0]))

            # 删除 job
            self.assertTrue(rp.delete_replica_job(job_id))
            # 关联的 task 文件也应该被级联删除
            self.assertFalse(os.path.exists(tpaths[0]))


if __name__ == '__main__':
    unittest.main()


