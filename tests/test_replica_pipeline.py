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


class TestExtractGate(ReplicaTempRootCase):
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

    def test_successful_extract_lands_on_review_frames_with_a_cost_estimate(self):
        state = self._ingest()
        collage = os.path.join(rp.job_dir(state['job_id']), 'clip_collage.jpg')
        with open(collage, 'wb') as f:
            f.write(b'jpg')
        self._write_overview(state['job_id'], collage)

        with patch.object(rp.subprocess, 'run') as run, \
             patch.object(rp, '_analyzer_script', return_value='/fake/analyze.py'):
            run.return_value.returncode = 0
            out = rp.run_extract(state)

        self.assertEqual(out['stage'], 'review_frames')
        self.assertEqual(out['overview']['collage'], collage)
        # 开跑前必须能把预估摆给用户看，否则用户是在盲烧视觉调用的钱。
        self.assertIn('full', out['cost_estimate'])
        self.assertIn('degraded', out['cost_estimate'])

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

    def test_banned_elements_hit_in_the_prompt_is_reported(self):
        state = self._ingest()
        state['beats'] = {'beats': [{'id': 'B01'}], 'banned_elements': ['excavator']}
        state['validation'] = []
        rp._save_state(state)
        with patch('prompt_pipeline.compose_anchor_and_packet', return_value={'title': 't'}), \
             patch('prompt_pipeline.compose_remaining_beats',
                   return_value='VIDEO 1: an EXCAVATOR swings into frame'):
            out = rp.run_compose(state, {})
        self.assertEqual(out['banned_hits'], ['excavator'])
        # 命中不阻断交付——它是「交付前必须重写」的标记，不是流水线故障。
        self.assertEqual(out['stage'], 'completed')


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
