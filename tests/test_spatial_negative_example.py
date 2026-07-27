# -*- coding: utf-8 -*-
"""反面教材回归测试：2026-07-12 hollow-oak 实际交付的整套空间错乱提示词（10 图 / 9 视频）。

这组产出暴露的空间逻辑缺陷（锚点比例振荡、双份矛盾锚点、桥接帧丢相机线、
穿越后仍钉室外锚点/回填 horizon、桥接段 180° 横摇）必须永远被管线抓住；
且经 apply_proactive_fixes 修复后不得再含任何一类空间违规。
提示词原文逐字保留，作为负样本 fixture 使用。
"""
import unittest

from prompt_pipeline import (
    apply_proactive_fixes,
    check_anchor_scale_lock,
    check_bridge_sterile,
    check_camera_contradictions,
    check_colon_label_style,
    check_image_static_state,
    check_pbisp_peek,
    check_shot_family_leakage,
    fix_primary_landmarks,
    fix_sound_design,
    image_space_family,
    beat_space_family,
)

PACKET = {
    'camera_dna': (
        "Static wide-angle eighteen-millimeter tripod shot at one-point-five meters height, "
        "locked eye-level perspective facing the hollow oak trunk; horizon line remains level "
        "at fifty percent height."
    ),
    'geometry_lock': 'hollow oak trunk shell fixed',
    'primary_landmarks': [
        {'name': 'decaying trunk base opening', 'grid': 'Grid C2', 'z_depth_scale': '35%'},
        {'name': 'curved interior cavity wall', 'grid': 'Grid B2', 'z_depth_scale': '65%'},
        {'name': 'misty forest canopy', 'grid': 'Grid A2', 'z_depth_scale': '45%'},
    ],
    'frame_boundaries': {'left': 'B1', 'right': 'B3', 'top': 'A2', 'bottom': 'C2'},
}

LADDER = [
    {'index': 1, 'operation': 'clearing', 'description': 'clear rot', 'bridge_stage': None},
    {'index': 2, 'operation': 'staging', 'description': 'stage materials', 'bridge_stage': None},
    {'index': 3, 'operation': 'treatment', 'description': 'treat heartwood', 'bridge_stage': None},
    {'index': 4, 'operation': 'threshold', 'description': 'approach sill', 'bridge_stage': 1},
    {'index': 5, 'operation': 'threshold', 'description': 'cross sill', 'bridge_stage': 2},
    {'index': 6, 'operation': 'lighting', 'description': 'sconces on', 'bridge_stage': None},
    {'index': 7, 'operation': 'paneling', 'description': 'oak panels', 'bridge_stage': None},
    {'index': 8, 'operation': 'fitting', 'description': 'glass door', 'bridge_stage': None},
    {'index': 9, 'operation': 'reward', 'description': 'final glow', 'bridge_stage': None},
]

# ---- 实际交付原文（反面教材）----
IMAGES = {
    1: {'body': "Static wide-angle eighteen-millimeter tripod shot at one-point-five meters height facing the hollow oak trunk; decaying trunk base opening at Grid C2; curved interior cavity wall at Grid B2; misty forest canopy at Grid A2. The decaying trunk base opening holds a scale of thirty-five percent of frame height, filled with wet rotting leaves. The curved interior cavity wall holds a scale of sixty-five percent of frame height, showing crumbling wood and damp green moss. The misty forest canopy holds a scale of forty-five percent of frame height. Left frame boundary is Grid B1 outer bark; right is Grid B3; top is Grid A2 branches; bottom is Grid C2 root base. The scene represents the untouched trauma state of natural decay. Relative positioning locks anchor wild mushrooms ten centimeters left of the trunk base. Ambient forest light only. Horizon line remains level at fifty percent height. Static shot empty of tools, materials, or activity.", 'meta': ''},
    2: {'body': "Static wide-angle eighteen-millimeter tripod shot at one-point-five meters height, locked eye-level perspective facing the hollow oak trunk. Horizon line remains level at fifty percent height; optical flow lines radiate symmetrically from Grid B2. Static materials: bolts, fasteners, bucket (C1); preservative, battery (C3); conduit (C2); glass door (B3). Locked anchors: decaying trunk base opening at Grid C2 holds a scale of thirty-five percent of frame height, curved interior cavity wall at Grid B2 holds a scale of sixty-five percent of frame height, misty forest canopy at Grid A2 holds a scale of forty-five percent of frame height.", 'meta': ''},
    3: {'body': "Static wide-angle eighteen-millimeter tripod shot at one-point-five meters height, locked eye-level perspective facing the hollow oak trunk. Clean heartwood is exposed. Scraped interior wall shows damp, treated wood. Open jug in Grid C3 has cap on floor. Horizon line remains level at fifty-percent height; optical flow lines radiate symmetrically from Grid B2. Locked anchors: decaying trunk base opening at Grid C2, curved interior cavity wall at Grid B2, misty forest canopy at Grid A2. Left frame boundary is Grid B1 outer bark; right is Grid B3; top is Grid A2 branches; bottom is Grid C2 root base.", 'meta': ''},
    4: {'body': "Static wide-angle eighteen-millimeter tripod shot at one-point-five meters height, locked eye-level perspective facing the hollow oak trunk. Horizon line remains level at fifty percent height. Timber studs and steel tracks line Grid B2 walls. A sawdust tray sits in Grid C2. Locked anchors: trunk base at Grid C2 (35 percent height), cavity wall at Grid B2 (65 percent height), frame at Grid B2 (55 percent height), canopy at Grid A2 (45 percent height). Frame boundaries: left Grid B1 bark, right Grid B3, top Grid A2 branches, bottom Grid C2 root. Locked anchors: decaying trunk base opening at Grid C2, curved interior cavity wall at Grid B2, misty forest canopy at Grid A2.", 'meta': ''},
    5: {'body': "Horizon line remains level at fifty-percent height. Backlit by overcast daylight, a work light illuminates the dim interior's timber and steel framing, screws, sawdust, and conduit. Locked anchors: decaying trunk base opening at Grid C2, curved interior cavity wall at Grid B2, misty forest canopy at Grid A2. Left boundary is Grid B1 left outer bark; right is Grid B3 right outer bark; top is Grid A2 branch split; bottom is Grid C2 mossy root base.", 'meta': 'BRIDGE'},
    6: {'body': "Locked anchors: decaying trunk base opening at Grid C2, curved interior cavity wall at Grid B2, misty forest canopy at Grid A2. Mounted on the walls in Grid B1 and B3, two brass sconces are being installed; one has a dangling wire, the other glows with a dim orange light. Scattered sawdust lies on the floor. The horizon line remains perfectly level at exactly 50-percent height of the frame.", 'meta': 'BRIDGE'},
    7: {'body': "Static wide-angle eighteen-millimeter tripod shot at one-point-five meters height, locked eye-level perspective facing the hollow oak trunk. Glowing brass sconces activate. Locked anchors: decaying trunk base opening at Grid C2 holds a scale of fifty-five percent of frame height, curved interior cavity wall at Grid B2 holds a scale of eighty-five percent of frame height, misty forest canopy at Grid A2 holds a scale of twenty-five percent of frame height. Left boundary is Grid B1 left bark; right is Grid B3 right bark; top is Grid A2 branches; bottom is Grid C2 mossy root. The horizon line remains perfectly level at exactly 50-percent height of the frame.", 'meta': ''},
    8: {'body': "Static wide-angle eighteen-millimeter tripod shot at one-point-five meters height, locked eye-level perspective facing the hollow oak trunk. Horizon line remains level at fifty-percent height. Locked anchors: decaying trunk base opening at Grid C2 at thirty-five percent height, curved interior cavity wall at Grid B2 at sixty-five percent height, misty forest canopy at Grid A2 at forty-five percent height. Boundaries: left is Grid B1, right is Grid B3, top is Grid A2, bottom is Grid C2. Blonde oak panels line walls. Warm brass sconces in Grid B1. Low-gloss oak flooring in Grid C2. Conduits hidden at Grid B2.", 'meta': ''},
    9: {'body': "Static wide-angle eighteen-millimeter tripod shot at one-point-five meters height, locked eye-level perspective facing the hollow oak trunk. Horizon line remains level at fifty percent height. B1: glowing sconces. C2: reflective floor. B2: glass door. A2: LED lights. Locked anchors: decaying trunk base opening at Grid C2 holds scale of 55 percent frame height, curved interior cavity wall at Grid B2 holds scale of 85 percent frame height, misty forest canopy at Grid A2 holds scale of 25 percent frame height. Left boundary is Grid B1, right is Grid B3, top is Grid A2, bottom is Grid C2.", 'meta': ''},
    10: {'body': "Static wide-angle eighteen-millimeter tripod shot at one-point-five meters height, locked eye-level perspective facing the hollow oak trunk. Horizon line remains level at fifty-percent height. Polished oak floor, wood panels, closed glass door glisten under warm light. Locked anchors: decaying trunk base opening at Grid C2 at thirty-five percent height, curved interior cavity wall at Grid B2 at sixty-five percent height, misty forest canopy at Grid A2 at forty-five percent height. Left boundary is Grid B1 left bark; right is Grid B3 right bark; top is Grid A2 canopy; bottom is Grid C2 forest floor. The highly reflective polished floor surface in Grid C1-C3 displays a heavily blurred, low-gloss, diffused reflection of the background; reflections are muted, dark, and highly out-of-focus, preventing high-frequency contrast or sharp details; realistic Fresnel falloff near the margins.", 'meta': ''},
}

_OPEN = ("Use the provided first frame and last frame as exact composition anchors. Use IMAGE {a} as the actual "
         "first-frame image and IMAGE {b} as the actual last-frame image; every visible action must interpolate "
         "between those two frame images without inventing a third layout. ")
VIDEOS = {
    1: {'body': _OPEN.format(a=1, b=2) + "At zero seconds, one lone worker enters the frame from the Grid C1 edge; the worker performs work, and by seven point five seconds, walks out of the frame through the Grid C1 edge, leaving the frame completely empty at eight seconds. continuous construction time-lapse, not real-time footage. Sound effects include the tool contact, material movement, and footsteps of this beat. Ambient noise is the steady enclosed room tone of the space.", 'meta': ''},
    2: {'body': _OPEN.format(a=2, b=3) + "Near-field sounds include steel scraping wood and liquid spraying, against a background of wind rustling forest leaves. continuous construction time-lapse, not real-time footage. Sound effects include the tool contact, material movement, and footsteps of this beat. Ambient noise is the steady enclosed room tone of the space.", 'meta': ''},
    3: {'body': _OPEN.format(a=3, b=4) + "At zero seconds, one lone worker enters the frame from the Grid C1 edge; the worker performs work, and by seven point five seconds, walks out of the frame through the Grid C1 edge, leaving the frame completely empty at eight seconds. continuous construction time-lapse, not real-time footage. Sound effects include the tool contact, material movement, and footsteps of this beat. Ambient noise is the steady enclosed room tone of the space.", 'meta': ''},
    4: {'body': _OPEN.format(a=4, b=5) + "Camera executes a coaxial forward push-in toward the doorway. The frame stays completely sterile of active workers. The approach leaves behind compression tracks on the mossy ground. Sound effects include bootsteps crunching on twigs and a faint generator hum. Ambient noise is the whispering wind through the canopy.", 'meta': 'BRIDGE'},
    5: {'body': _OPEN.format(a=5, b=6) + "Camera pushes forward, panning 180 degrees. Worker enters, installs frame, sweeps, exits. Traces: frame, paneling, sawdust. Sounds: scraping, footsteps. Ambient: cabin acoustics. Sound effects include the tool contact, material movement, and footsteps of this beat. Ambient noise is the steady enclosed room tone of the space.", 'meta': 'BRIDGE'},
    6: {'body': _OPEN.format(a=6, b=7) + "Sconces glow orange. The frame stays sterile of workers. Sound effects include a switch click and electrical hum. Ambient noise is the room tone. continuous construction time-lapse, not real-time footage.", 'meta': ''},
    7: {'body': _OPEN.format(a=7, b=8) + "Rhythmic scraping of a trowel and the sliding of wood boards blend with the background forest wind. continuous construction time-lapse, not real-time footage. Sound effects include the tool contact, material movement, and footsteps of this beat. Ambient noise is the steady enclosed room tone of the space.", 'meta': ''},
    8: {'body': _OPEN.format(a=8, b=9) + "Sound effects include a whirring drill and clicking hinges. Ambient noise is the steady enclosed room tone of the space. continuous construction time-lapse, not real-time footage.", 'meta': ''},
    9: {'body': _OPEN.format(a=9, b=10) + "Camera pans 5 degrees over 8 seconds. Lights brighten; fog drifts. No workers. Traces: silicone beads. Audio: wind, room tone, switch click. Sound effects include the tool contact, material movement, and footsteps of this beat. Ambient noise is the steady enclosed room tone of the space.", 'meta': ''},
}


def _family_of_beat(i):
    return beat_space_family(LADDER, i)


class TestNegativeExampleIsCaught(unittest.TestCase):
    """校验器必须抓住这套原始产出里的每一类空间违规。"""

    def test_slot_meta_family_mapping(self):
        expected = {1: 'exterior', 2: 'exterior', 3: 'exterior', 4: 'exterior',
                    5: 'interior', 6: 'interior', 7: 'interior', 8: 'interior',
                    9: 'interior', 10: 'interior'}
        got = {seq: image_space_family(VIDEOS, seq) for seq in IMAGES}
        self.assertEqual(got, expected)

    def test_scale_oscillation_flagged_in_family(self):
        # 图7/9 的 55/85/25 相对 packet 的 35/65/45：族内比例锁必须报 3 项漂移
        for seq in (7, 9):
            errs = check_anchor_scale_lock(IMAGES[seq]['body'], PACKET, family='exterior')
            self.assertEqual(len(errs), 3, f"IMAGE {seq}: {errs}")
        # 比例正确的图2不报
        self.assertEqual(check_anchor_scale_lock(IMAGES[2]['body'], PACKET, family='exterior'), [])

    def test_exterior_anchor_pinned_indoors_flagged(self):
        # 穿越后（图6-10）把室外锚点钉回网格 → 泄漏校验必须报
        for seq in (6, 7, 8, 9, 10):
            errs = check_shot_family_leakage(IMAGES[seq]['body'], PACKET, family='interior')
            self.assertTrue(any('pre-crossing exterior landmark' in e for e in errs),
                            f"IMAGE {seq} not flagged: {errs}")

    def test_horizon_indoors_flagged(self):
        errs = check_shot_family_leakage(IMAGES[7]['body'], PACKET, family='interior')
        self.assertTrue(any('horizon' in e for e in errs))

    def test_bridge_pan_flagged(self):
        errs = check_camera_contradictions(VIDEOS[5]['body'], True, ban_pan_tilt=True)
        self.assertTrue(any('pan' in e.lower() for e in errs))
        # 合规的桥接推进（视频4）不报
        self.assertEqual(check_camera_contradictions(VIDEOS[4]['body'], True, ban_pan_tilt=True), [])

    def test_bridge_worker_flagged(self):
        # TBCP：桥接段必须无工人；实际产出的视频5在穿越镜头里塞了完整施工
        errs = check_bridge_sterile(VIDEOS[5]['body'])
        self.assertTrue(any('sterile' in e for e in errs))
        # 视频4 显式声明 sterile → 不报
        self.assertEqual(check_bridge_sterile(VIDEOS[4]['body']), [])

    def test_colon_label_style_flagged(self):
        # 图9 的网格标签碎片与视频5 的 "Traces:" 标签（NLVTR 文字伪影风险）
        img9_errs = check_colon_label_style(IMAGES[9]['body'])
        self.assertTrue(any('grid-cell label' in e for e in img9_errs))
        vid5_errs = check_colon_label_style(VIDEOS[5]['body'])
        self.assertTrue(any('Traces' in e for e in vid5_errs))
        # 管线自己的规范化子句不误报
        self.assertEqual(check_colon_label_style(
            "Locked anchors: decaying trunk base opening at Grid C2 holding 35 percent of frame height."), [])
        self.assertEqual(check_colon_label_style(
            "Bridge sill-handoff frame: same ultra-wide lens feel, camera pitch locked level."), [])
        # 模板承认的音频标签风格不误报
        self.assertEqual(check_colon_label_style(
            "SFX: repetitive hiss of compressed air. Ambient noise: low rumbling of a distant generator."), [])

    def test_mid_action_image_flagged(self):
        # 图6 "two brass sconces are being installed" —— 静态帧混入进行中动作
        errs = check_image_static_state(IMAGES[6]['body'])
        self.assertTrue(any('mid-action' in e for e in errs))
        self.assertEqual(check_image_static_state(
            "Two brass sconces are mounted on the walls, fully installed and glowing."), [])

    def test_pbisp_peek_flagged(self):
        packet_with_interior = dict(PACKET, interior_primary_landmarks=[
            {'name': 'heartwood ridge', 'grid': 'Grid B2', 'z_depth_scale': '60%'},
        ])
        # 实际产出的桥前帧（图4）没有透过开口预览室内锚点
        errs = check_pbisp_peek(IMAGES[4]['body'], packet_with_interior)
        self.assertTrue(any('heartwood ridge' in e for e in errs))
        self.assertEqual(check_pbisp_peek(
            "Through the open trunk base, the heartwood ridge is already visible and sharp.",
            packet_with_interior), [])

    def test_pbisp_peek_video_side_flagged_as_structural(self):
        # 2026-07-22 森林瞭望塔实测单：图片3有门内预告室内锚点，视频2全篇没提，
        # 造成视频末尾跟静态末帧对不上（"结尾跳变"）。label='VIDEO' 让同一条
        # 检查也能查视频文本，且命中的错误文案要落进结构性硬伤标记表以触发回炉。
        from prompt_pipeline import check_pbisp_peek, _STRUCTURAL_VIDEO_ERROR_MARKERS
        packet_with_interior = dict(PACKET, interior_primary_landmarks=[
            {'name': 'heartwood ridge', 'grid': 'Grid B2', 'z_depth_scale': '60%'},
        ])
        video_missing_peek = (
            "Use the provided first frame and last frame as exact composition anchors. "
            "The worker repairs the stairs and railings throughout the clip."
        )
        errs = check_pbisp_peek(video_missing_peek, packet_with_interior, label='VIDEO')
        self.assertTrue(any('heartwood ridge' in e for e in errs))
        self.assertTrue(any(any(m in e for m in _STRUCTURAL_VIDEO_ERROR_MARKERS) for e in errs))
        self.assertEqual(check_pbisp_peek(
            "Through the open trunk base, the heartwood ridge stays visible and sharp across the clip.",
            packet_with_interior, label='VIDEO'), [])

    def test_video_process_content_contract(self):
        # 2026-07-12 17:18 实测单：IMAGE 对是全画幅大变化，VIDEO 却空心
        from prompt_pipeline import check_video_process_content
        OPEN = _OPEN.format(a=4, b=5)
        # 实测视频4形状：只有环境音+节奏句，零动作过程 → thin
        thin = OPEN + ("Ambient noise is the low hum of a distant generator. "
                       "This is a continuous construction time-lapse, not real-time footage.")
        errs = check_video_process_content(thin, is_bridge=False)
        self.assertTrue(any('no visible action' in e for e in errs))
        # 实测视频2形状：桥接段连相机推进都没写
        bridge_empty = _OPEN.format(a=2, b=3) + "Ambient noise: a deep, hollow room tone inside the wooden chamber."
        errs = check_video_process_content(bridge_empty, is_bridge=True)
        self.assertTrue(any('camera-translation' in e for e in errs))
        # 合规桥接（反面教材视频4：有 coaxial push-in）→ 通过
        self.assertEqual(check_video_process_content(VIDEOS[4]['body'], is_bridge=True), [])
        # 反面教材视频7形状：泥刀自己刮墙、无施工主体 → 幽灵施工
        errs = check_video_process_content(VIDEOS[7]['body'], is_bridge=False)
        self.assertTrue(any('ghost work' in e for e in errs))
        # sterile 声明 + 施工动作并存 → 矛盾
        contradiction = OPEN + ("Panels are nailed into place wall by wall until every surface is "
                                "covered and fastener rows appear along each seam. "
                                "The frame stays completely sterile of active workers.")
        errs = check_video_process_content(contradiction, is_bridge=False)
        self.assertTrue(any('cannot operate themselves' in e for e in errs))
        # 有工人执行的完整过程 → 通过
        good = OPEN + ("A lone worker in a solid pale shirt enters from the Grid C1 edge and nails "
                       "pale birch panels wall by wall; coverage grows continuously until every "
                       "visible surface is covered, and the worker exits by seven point five seconds.")
        self.assertEqual(check_video_process_content(good, is_bridge=False), [])

    def test_percent_phrase_parsing(self):
        # 2026-07-12 实测单：packet 把 z_depth_scale 写成整句 "15 percent of total frame height"，
        # 严格解析器返回 None → 比例锁整单静默失效
        from prompt_pipeline import _parse_percent_token
        self.assertEqual(_parse_percent_token('15 percent of total frame height'), 15)
        self.assertEqual(_parse_percent_token('forty-five percent of total frame height'), 45)
        self.assertEqual(_parse_percent_token('50 percent'), 50)
        self.assertEqual(_parse_percent_token('35%'), 35)
        self.assertEqual(_parse_percent_token('fifty-five'), 55)
        self.assertIsNone(_parse_percent_token('a large portion of the frame'))

    def test_locked_landmarks_variant_stanza_stripped(self):
        # 实测单图6形状：LLM 写 "Locked landmarks: ..."，修复器又追加 "Locked anchors: ..." → 双份
        packet = dict(PACKET, interior_primary_landmarks=[
            {'name': 'gnarled interior heartwood ridge', 'grid': 'Grid B2',
             'z_depth_scale': '50 percent of total frame height'},
            {'name': 'natural hollow knot hole cavity', 'grid': 'Grid A2',
             'z_depth_scale': '20 percent of total frame height'},
        ])
        prompt = ("Camera pitch locked level; the central vanishing axis stays centered. "
                  "Locked landmarks: gnarled interior ridge at Grid B2 at fifty percent height; "
                  "knot hole cavity at Grid A2 at twenty percent height. "
                  "Locked anchors: gnarled interior heartwood ridge at Grid B2, "
                  "natural hollow knot hole cavity at Grid A2.")
        fixed = fix_primary_landmarks(prompt, packet, family='interior')
        low = fixed.lower()
        self.assertEqual(low.count('locked anchors:') + low.count('locked landmarks:'), 1)
        self.assertIn('holding 50 percent of frame height', fixed)
        # "Locked anchors are ..."（图1形状）同样被视作 stanza 收编
        ext = ("Static shot facing the oak trunk. Locked anchors are helical screw piles at the "
               "base of the trunk at Grid C2, gaping natural opening of the hollow trunk at Grid B2, "
               "and ancient forest grove canopy at Grid A2. Horizon line remains level.")
        ext_packet = {
            'primary_landmarks': [
                {'name': 'helical screw piles at the base of the trunk', 'grid': 'Grid C2',
                 'z_depth_scale': '15 percent of total frame height'},
                {'name': 'gaping natural opening of the hollow trunk', 'grid': 'Grid B2',
                 'z_depth_scale': '45 percent of total frame height'},
                {'name': 'ancient forest grove canopy', 'grid': 'Grid A2',
                 'z_depth_scale': '30 percent of total frame height'},
            ],
        }
        ext_fixed = fix_primary_landmarks(ext, ext_packet, family='exterior')
        self.assertEqual(ext_fixed.lower().count('locked anchors'), 1)
        self.assertIn('holding 15 percent of frame height', ext_fixed)

    def test_state_delta_label_flagged(self):
        errs = check_colon_label_style("State delta: brass branch LED sconces are mounted in Grid B1.")
        self.assertTrue(errs)

    def test_out_and_in_no_double_entry_and_clean_grammar(self):
        from prompt_pipeline import fix_out_and_in
        # 实测单视频3形状：body 已有 enters/exits，旧检测词组太窄又贴了第二份进出模板
        body = ("A worker in a yellow vest enters, builds a timber frame, and exits. "
                "Nails and conduits remain.")
        self.assertEqual(fix_out_and_in(body, False, beat=None, packet=None), body)
        # 被动语态的拍描述不能拼进 'cycles of'（实测单曾产出破碎语法+双逗号）
        beat = {'operation': 'framing', 'description':
                'An independent internal timber framing structure and floor platform are erected inside the cavity.'}
        packet = {'worker_choreography':
                  'one lone worker in a solid bright-neon-yellow safety vest, a white hardhat, and solid dark blue work pants'}
        out = fix_out_and_in('A lone worker hammers beams into place inside the cavity.',
                             False, beat=beat, packet=packet)
        self.assertIn('cycles of the framing task', out)
        self.assertNotIn(',,', out)
        self.assertNotIn('are erected', out.split('cycles of')[-1])
        # 服装截断落在词边界，不再出现 "solid dark enters"
        self.assertNotIn('solid dark enters', out)

    def test_out_and_in_injects_locked_worker_scale(self):
        from prompt_pipeline import fix_out_and_in
        beat = {'operation': 'framing', 'description': 'timber frame erected inside the cavity.'}
        packet = {'worker_choreography': 'one lone worker in a solid bright-neon-yellow safety vest',
                  'worker_scale_percent': '18%'}
        out = fix_out_and_in('A lone worker hammers beams into place inside the cavity.',
                             False, beat=beat, packet=packet)
        self.assertIn('standing roughly 18 percent of frame height', out)
        self.assertNotIn(',,', out)
        # No worker_scale_percent locked on the packet -> clause degrades gracefully, no
        # stray punctuation left behind where the clause would have been.
        out_no_scale = fix_out_and_in('A lone worker hammers beams into place inside the cavity.',
                                      False, beat=beat,
                                      packet={'worker_choreography': packet['worker_choreography']})
        self.assertNotIn('percent of frame height', out_no_scale)
        self.assertNotIn(',,', out_no_scale)

    def test_out_and_in_multi_worker_injects_scale(self):
        from prompt_pipeline import fix_out_and_in
        packet = {'worker_scale_percent': '22%'}
        out = fix_out_and_in('Two workers assemble the frame together.', False, beat=None, packet=packet)
        self.assertIn('each standing roughly 22 percent of frame height', out)
        self.assertNotIn(',,', out)

    def test_sound_design_hum_hear_detected(self):
        # 实测单视频6形状："We hear sliding, clicks, and room hum." 曾被再贴一份棚内音底
        body = "We hear sliding, clicks, and room hum."
        self.assertEqual(fix_sound_design(body, family='interior'), body)

    def test_sound_design_no_double_audio_and_family_aware(self):
        # 实际产出的视频2：已有 "Near-field sounds include..." 却被旧逻辑再贴一份棚内音底
        raw_v2_audio = ("Near-field sounds include steel scraping wood and liquid spraying, "
                        "against a background of wind rustling forest leaves.")
        fixed = fix_sound_design(raw_v2_audio, family='exterior')
        self.assertEqual(fixed, raw_v2_audio)  # 不再追加第二份音频
        # 完全无音频的室外拍 → 补的是户外音底，不是 enclosed room tone
        no_audio = "The worker lays panels across the wall."
        fixed_ext = fix_sound_design(no_audio, family='exterior')
        self.assertIn('natural outdoor tone', fixed_ext)
        self.assertNotIn('enclosed room tone', fixed_ext)
        fixed_int = fix_sound_design(no_audio, family='interior')
        self.assertIn('enclosed room tone', fixed_int)


class TestNegativeExampleIsRepaired(unittest.TestCase):
    """整套反面教材经 apply_proactive_fixes 后不得再含任何一类空间违规。"""

    def _fixed_beat(self, i):
        beat = LADDER[i - 1]
        family = _family_of_beat(i)
        is_last = (i == 9)
        is_tr = beat['operation'] in ('threshold', 'reward')
        return family, apply_proactive_fixes(
            i, VIDEOS[i]['body'], IMAGES[i + 1]['body'], PACKET, 'Threshold',
            is_last, is_tr, beat=beat, config=None, family=family,
        )

    def test_all_beats_spatially_clean_after_fixes(self):
        for i in range(1, 10):
            family, (v_fixed, img_fixed) = self._fixed_beat(i)
            # 1) 族内比例锁
            self.assertEqual(check_anchor_scale_lock(img_fixed, PACKET, family), [],
                             f"beat {i} scale drift survived")
            # 2) 跨族泄漏（室外锚点钉网格 / 室内 horizon）
            self.assertEqual(check_shot_family_leakage(img_fixed, PACKET, family), [],
                             f"beat {i} family leakage survived")
            # 3) 图片相机声明存在且与族匹配
            low = img_fixed.lower()
            if family == 'exterior':
                self.assertIn('horizon line', low, f"beat {i} exterior image lost horizon lock")
            else:
                self.assertTrue('pitch locked' in low or 'vanishing axis' in low,
                                f"beat {i} {family} image has no camera attitude lock")
            # 4) 视频不再含未经许可的横摇/俯仰
            allow_sweep = LADDER[i - 1]['operation'] == 'reward'
            errs = check_camera_contradictions(v_fixed, LADDER[i - 1]['bridge_stage'] == 1,
                                               ban_pan_tilt=not allow_sweep)
            self.assertEqual(errs, [], f"beat {i} video camera errors survived: {errs}")

    def test_image4_dual_stanza_canonicalized(self):
        fixed = fix_primary_landmarks(IMAGES[4]['body'], PACKET, family='exterior')
        self.assertEqual(fixed.lower().count('locked anchors:'), 1)
        self.assertNotIn('55 percent', fixed)
        self.assertIn('decaying trunk base opening at Grid C2 holding 35 percent of frame height', fixed)

    def test_bridge_image5_regains_camera(self):
        _, (v, img) = self._fixed_beat(4)
        self.assertIn('vanishing axis', img.lower())
        self.assertIn('enclosed interior', img.lower())

    def test_video5_pan_stripped_work_kept(self):
        _, (v, img) = self._fixed_beat(5)
        self.assertNotIn('panning', v.lower())
        self.assertIn('installs frame', v)


if __name__ == '__main__':
    unittest.main()
