import json
import os
import tempfile
import unittest
from unittest.mock import patch, MagicMock

import server_common
import replica_pipeline as rp
import prompt_pipeline as pp
from prompt_pipeline.fast_composer import (
    build_fast_composer_system_prompt,
    build_fast_composer_user_prompt,
    beats_to_ladder_payload,
    synthesize_drift_lock_packet,
    compose_replica_one_pass
)


# 标准链路那两条用例里 compose_remaining_beats 的返回值：run_compose 只对它做
# _prompt_block_only + 槽位摘要补全，内容本身不重要，能被解析出槽位就够。
COMPOSED_DOC = """===PROMPTS===
IMAGE 1 (毛坯):
A container.

VIDEO 1 (切割):
A worker cuts.

IMAGE 2 (完成):
A window.
"""


class TestFastComposer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old_root = server_common.OUTPUT_ROOT
        server_common.OUTPUT_ROOT = self.tmp

    def tearDown(self):
        server_common.OUTPUT_ROOT = self.old_root

    def test_build_system_prompt_includes_rules_and_banned(self):
        sys_prompt = build_fast_composer_system_prompt(
            banned_elements=['excavator', 'plastic chair'],
            scene_constants=['dark ceiling', 'concrete floor']
        )
        self.assertIn('9:16 vertical', sys_prompt)
        self.assertIn('videoVolume: 0.6', sys_prompt)
        self.assertIn('excavator', sys_prompt)
        self.assertIn('concrete floor', sys_prompt)
        self.assertIn('1.78m tall', sys_prompt)

    def test_build_user_prompt_formats_beats(self):
        beats = [
            {'visible_action': '冲洗地面', 'visible_result': '地面干净', 'package_operations': ['高压冲洗']},
            {'visible_action': '铺设龙骨', 'visible_result': '龙骨就位', 'package_operations': ['安装龙骨']}
        ]
        user_prompt = build_fast_composer_user_prompt('地下室改造', '地下室', beats)
        self.assertIn('Beat 1', user_prompt)
        self.assertIn('冲洗地面 -> 地面干净', user_prompt)
        self.assertIn('Beat 2', user_prompt)

    def test_beats_to_ladder_payload(self):
        beats = [
            {'visible_action': '打磨墙面', 'visible_result': '墙面平整', 'operation': 'clearing', 'space': 'exterior'},
            {'visible_action': '推门进入', 'visible_result': '进入室内', 'stage': 'transition', 'space': 'interior', 'turn_direction': 'left'}
        ]
        ladder = beats_to_ladder_payload(beats)
        self.assertEqual(len(ladder), 2)
        self.assertEqual(ladder[0]['index'], 1)
        self.assertEqual(ladder[0]['operation'], 'clearing')
        self.assertEqual(ladder[1]['bridge_stage'], 1)
        self.assertEqual(ladder[1]['turn_direction'], 'left')

    def test_compose_replica_one_pass_end_to_end(self):
        mock_output = """===TITLE===
废弃地下室改造成温馨卧室

===THEME===
地下室

===PROMPTS===
IMAGE 1 (毛坯初始状态):
A photoreal 9:16 wide shot of an abandoned underground bunker with rough concrete walls.

VIDEO 1 (高压冲洗地面):
IMAGE 1 to IMAGE 2 time-lapse. A lone worker sprays the floor with a high-pressure hose. ASMR sound effects: water hissing and spraying at 60% volume.

IMAGE 2 (地面冲洗干净):
A photoreal 9:16 wide shot of the clean bunker with wet floor marks drying to matte concrete.

VIDEO 2 (铺设木地板):
IMAGE 2 to IMAGE 3 time-lapse. Worker lays interlocking oak floor planks. ASMR sound effects: wood clicking into place.

IMAGE 3 (最终温馨卧室揭晓):
A photoreal 9:16 reveal shot of the finished cozy bedroom with warm lighting and matte oak flooring.
"""
        state = {
            'job_id': 'replica_test_123',
            'video_name': 'bunker.mp4',
            'beats': {
                'carrier': '地下室',
                'destiny_zh': '温馨卧室',
                'beats': [
                    {'visible_action': '高压冲洗地面', 'visible_result': '地面冲洗干净', 'operation': 'clearing'},
                    {'visible_action': '铺设木地板', 'visible_result': '木地板铺设完毕', 'operation': 'flooring'}
                ],
                'banned_elements': []
            }
        }
        with patch('prompt_pipeline._chat', return_value=mock_output):
            prompt_block, compose_state = compose_replica_one_pass({}, state)

        self.assertTrue('图片 1' in prompt_block or 'IMAGE 1' in prompt_block)
        self.assertTrue('视频 1' in prompt_block or 'VIDEO 1' in prompt_block)
        self.assertTrue('图片 2' in prompt_block or 'IMAGE 2' in prompt_block)
        self.assertTrue('视频 2' in prompt_block or 'VIDEO 2' in prompt_block)
        self.assertTrue('图片 3' in prompt_block or 'IMAGE 3' in prompt_block)
        self.assertEqual(compose_state['total_beats'], 2)
        self.assertEqual(len(compose_state['compiled_images']), 3)
        self.assertEqual(len(compose_state['compiled_videos']), 2)
        self.assertEqual(compose_state['title'], '废弃地下室改造成温馨卧室')

    def test_run_compose_uses_fast_one_pass(self):
        state = rp.ingest_video(b'video-bytes', 'test_clip.mp4')
        state['beats'] = {
            'carrier': '集装箱',
            'destiny_zh': '设计师工作室',
            'beats': [
                {'visible_action': '切割钢板', 'visible_result': '开出窗洞', 'operation': 'framing'},
            ],
            'banned_elements': ['excavator']
        }
        state['validation'] = []
        rp._save_state(state)

        mock_llm_output = """===TITLE===
废弃集装箱改造成设计师工作室

===THEME===
集装箱

===PROMPTS===
IMAGE 1 (原始毛坯集装箱):
A 9:16 wide shot of an abandoned rusted blue shipping container.

VIDEO 1 (切割窗洞工序):
A lone worker cuts a rectangular window opening using a plasma torch. ASMR sound effects: metal sparks and torch hiss.

IMAGE 2 (窗洞开设完成):
A 9:16 wide shot of the container with a clean framed window opening.
"""
        with patch('prompt_pipeline._chat', return_value=mock_llm_output), \
             patch('server_common.write_library_item') as mock_lib:
            out = rp.run_compose(state, {})

        self.assertEqual(out['stage'], 'completed')
        self.assertEqual(out['title'], '废弃集装箱改造成设计师工作室')
        self.assertTrue('图片 1' in out['prompt_block'] or 'IMAGE 1' in out['prompt_block'])
        self.assertTrue('图片 2' in out['prompt_block'] or 'IMAGE 2' in out['prompt_block'])
        self.assertTrue('视频 1' in out['prompt_block'] or 'VIDEO 1' in out['prompt_block'])
        # compose_state file must be created on disk
        c_path = rp._compose_state_path(state['job_id'])
        self.assertTrue(os.path.exists(c_path))
        with open(c_path, 'r', encoding='utf-8') as f:
            c_data = json.load(f)
            self.assertEqual(c_data['total_beats'], 1)
            self.assertEqual(len(c_data['compiled_images']), 2)
        # 走了哪条通道要留痕：两条通道的 packet 完全不是一回事（极速那条是写死的常量），
        # 而 prompt_block 上看不出区别——渲染出来空间漂移时靠这一笔才查得到源头。
        self.assertEqual(out.get('compose_mode'), 'fast')
        self.assertIsNone(out.get('compose_fallback'))

    def test_run_compose_honours_deep_mode_from_config(self):
        """config.composeMode='deep' 必须真的走标准 Phase 1+2，一次都不碰极速通道。

        前端「合成通道」这个单选框的全部价值就在这一条上：选了标准却仍旧直通，
        用户拿到的还是那份写死的空间锁定包、锚点也没跟原片首帧对过。
        """
        state = rp.ingest_video(b'video-bytes', 'test_clip.mp4')
        state['beats'] = {
            'carrier': '集装箱',
            'destiny_zh': '设计师工作室',
            'beats': [
                {'visible_action': '切割钢板', 'visible_result': '开出窗洞', 'operation': 'framing'},
            ],
            'banned_elements': [],
        }
        state['validation'] = []
        rp._save_state(state)

        composed = COMPOSED_DOC
        with patch('prompt_pipeline.fast_composer.compose_replica_one_pass') as fast,              patch('prompt_pipeline.compose_anchor_and_packet',
                   return_value={'title': 'T', 'image_1_prompt': 'A container.',
                                 'compiled_images': {}, 'packet': None}),              patch('prompt_pipeline.compose_remaining_beats', return_value=composed),              patch('server_common.write_library_item'):
            out = rp.run_compose(state, {'composeMode': 'deep'})

        fast.assert_not_called()
        self.assertEqual(out.get('compose_mode'), 'deep')

    def test_run_compose_records_the_silent_fallback_to_deep(self):
        """极速合成炸了会静默降级到标准链路——降级没问题，不留痕才有问题。"""
        state = rp.ingest_video(b'video-bytes', 'test_clip.mp4')
        state['beats'] = {
            'carrier': '集装箱',
            'destiny_zh': '设计师工作室',
            'beats': [
                {'visible_action': '切割钢板', 'visible_result': '开出窗洞', 'operation': 'framing'},
            ],
            'banned_elements': [],
        }
        state['validation'] = []
        rp._save_state(state)

        composed = COMPOSED_DOC
        with patch('prompt_pipeline.fast_composer.compose_replica_one_pass',
                   side_effect=ValueError('单轮直出解析失败')),              patch('prompt_pipeline.compose_anchor_and_packet',
                   return_value={'title': 'T', 'image_1_prompt': 'A container.',
                                 'compiled_images': {}, 'packet': None}),              patch('prompt_pipeline.compose_remaining_beats', return_value=composed),              patch('server_common.write_library_item'):
            out = rp.run_compose(state, {})

        self.assertEqual(out.get('compose_mode'), 'deep')
        self.assertIn('单轮直出解析失败', out.get('compose_fallback') or '')

    def test_save_prompt_in_place_fixes_audit_failed(self):
        state = rp.ingest_video(b'video-bytes', 'test_clip.mp4')
        state['stage'] = 'audit_failed'
        state['prompt_block'] = 'IMAGE 1:\nA worker in a hard hat.\n\nVIDEO 1:\nA worker.'
        state['beats'] = {'beats': [{'id': 'B01'}], 'banned_elements': ['hard hat']}
        state['banned_hits'] = ['hard hat']
        rp._save_state(state)

        with patch('server_common.write_library_item') as mock_lib:
            clean_text = 'IMAGE 1:\nA worker without helmet.\n\nVIDEO 1:\nA worker.'
            updated = rp.save_prompt(state['job_id'], clean_text)

        self.assertEqual(updated['stage'], 'completed')
        self.assertEqual(updated['banned_hits'], [])
        self.assertEqual(updated['prompt_block'], clean_text)
        mock_lib.assert_called_once()

    def test_purge_banned_from_prompt_auto_fixes(self):
        state = rp.ingest_video(b'video-bytes', 'test_clip.mp4')
        state['stage'] = 'audit_failed'
        state['prompt_block'] = 'IMAGE 1:\nA worker wearing a hard hat sprays the floor.'
        state['beats'] = {'beats': [{'id': 'B01'}], 'banned_elements': ['hard hat']}
        state['banned_hits'] = ['hard hat']
        rp._save_state(state)

        with patch('server_common.write_library_item') as mock_lib:
            updated = rp.purge_banned_from_prompt(state['job_id'])

        self.assertEqual(updated['stage'], 'completed')
        self.assertEqual(updated['banned_hits'], [])
        self.assertNotIn('hard hat', updated['prompt_block'])
        mock_lib.assert_called_once()


if __name__ == '__main__':
    unittest.main()


