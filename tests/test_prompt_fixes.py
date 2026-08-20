import re
import json
import unittest
from unittest.mock import patch
from prompt_pipeline import (
    fix_image_clean_frame_proactive,
    check_nlvtr_violations,
    fix_sound_design,
    fix_video_opening,
    _flatten_to_text,
    normalize_packet,
    normalize_beat_ladder,
    check_stylistic_repetition,
    _aux_model,
    check_adjacent_frame_semantics_batch,
    _stage_scope_beat_directive,
)
from frame_generator import _extract_image_prompts


class TestSlotExtraction(unittest.TestCase):
    """Regression: _parse_prompt_slots returns {'body','meta'} dicts; _extract_image_prompts
    must unwrap them so 'prompt' is plain prose (a dict repr would reach the image model)
    and the BRIDGE meta flag sits at the top level where generate_frame_sequence reads it."""

    BLOCK = (
        "图片提示词\n"
        "图片 1:\n"
        "A static shot of the ruined cabin.\n"
        "\n"
        "图片 2 [BRIDGE]:\n"
        "CHANGE IN THIS FRAME: camera now at the sill.\n"
    )

    def test_prompt_is_plain_string(self):
        items = _extract_image_prompts(self.BLOCK)
        self.assertEqual(len(items), 2)
        for it in items:
            self.assertIsInstance(it['prompt'], str)
            self.assertNotIn("{'body'", it['prompt'])

    def test_bridge_meta_surfaces_at_top_level(self):
        items = _extract_image_prompts(self.BLOCK)
        self.assertEqual(items[0]['meta'], '')
        self.assertEqual(items[1]['meta'], 'BRIDGE')
        self.assertIn('camera now at the sill', items[1]['prompt'])

    def test_prompt_slots_list_structured_contract(self):
        # 结构化槽位契约：与后端解析器同语义，含同行冒号正文与 [BRIDGE] meta
        from prompt_pipeline import prompt_slots_list
        block = (
            "图片提示词\n"
            "图片 1: A static shot of the ruined cabin.\n"
            "\n"
            "图片 2 [BRIDGE]:\n"
            "CHANGE IN THIS FRAME: camera now at the sill.\n"
            "\n"
            "视频提示词\n"
            "视频 1:\n"
            "Use the provided first frame and last frame as exact composition anchors.\n"
        )
        slots = prompt_slots_list(block)
        self.assertEqual([s['index'] for s in slots['images']], [1, 2])
        self.assertEqual(slots['images'][1]['meta'], 'BRIDGE')
        # 同行冒号后的正文必须保留（前端旧正则曾静默丢弃这种形状）
        self.assertIn('ruined cabin', slots['images'][0]['body'])
        self.assertEqual([s['index'] for s in slots['videos']], [1])
        self.assertEqual(prompt_slots_list(''), {'images': [], 'videos': []})

class TestPromptFixes(unittest.TestCase):
    
    def test_fix_image_clean_frame_proactive(self):
        # Test worker reference replacement
        prompt_with_worker = "A worker is installing the bed rails."
        cleaned = fix_image_clean_frame_proactive(prompt_with_worker)
        self.assertNotIn("worker", cleaned.lower())
        self.assertIn("equipment", cleaned.lower())
        self.assertIn("installation", cleaned.lower())

        # Test negative sentence with worker is stripped of worker tokens to prevent diffusion hallucinations
        prompt_negative = "The room is clean with no workers present."
        cleaned_negative = fix_image_clean_frame_proactive(prompt_negative)
        self.assertNotIn("worker", cleaned_negative.lower())
        self.assertIn("clean", cleaned_negative.lower())

        # Test sweep replacement
        prompt_sweep = "The person is sweeping the floor."
        cleaned_sweep = fix_image_clean_frame_proactive(prompt_sweep)
        self.assertNotIn("sweeping", cleaned_sweep.lower())
        self.assertIn("swept dust", cleaned_sweep.lower())
        self.assertIn("object", cleaned_sweep.lower())  # person -> object

        # Test oil painting hangs on wall remains painting
        prompt_painting = "An oil painting hangs on the wall."
        cleaned_painting = fix_image_clean_frame_proactive(prompt_painting)
        self.assertIn("painting", cleaned_painting.lower())

    def test_check_nlvtr_violations(self):
        # Test % violation
        self.assertIn("Contains forbidden '%' symbol", check_nlvtr_violations("Progress is 50%"))
        
        # Test numeric range violation
        self.assertIn("Contains forbidden numeric range", check_nlvtr_violations("It measures 10 to 20 cm."))
        self.assertIn("Contains forbidden numeric range", check_nlvtr_violations("Range is 5-10 meters."))
        
        # Test acronym violation
        self.assertIn("Contains forbidden acronym 'GCTR'", check_nlvtr_violations("Using GCTR check."))
        self.assertIn("Contains forbidden acronym 'TSPA'", check_nlvtr_violations("Inside TSPA state."))

        # Test clean prompt
        self.assertEqual(len(check_nlvtr_violations("A clean description of the wooden table with tools on top.")), 0)

    def test_fix_sound_design(self):
        # Sound design missing -> should append sound clause
        prompt_no_sound = "The camera pans left."
        fixed = fix_sound_design(prompt_no_sound)
        self.assertIn("Sound effects include", fixed)
        self.assertIn("Ambient noise", fixed)

        # Sound design exists -> should remain unchanged
        prompt_with_sound = "Footsteps trigger a sound effect."
        fixed_existing = fix_sound_design(prompt_with_sound)
        self.assertEqual(fixed_existing, prompt_with_sound)

    def test_fix_video_opening(self):
        # Test empty or regular text
        prompt = "The camera slowly pans across the workspace."
        fixed = fix_video_opening(1, prompt)
        self.assertTrue(fixed.startswith("Use the provided first frame and last frame as exact composition anchors."))
        self.assertIn("IMAGE 1", fixed)
        self.assertIn("IMAGE 2", fixed)
        self.assertIn("The camera slowly pans", fixed)

        # Test already has partial opening
        prompt_with_partial = "Use the provided first frame. The camera zooms in."
        fixed_partial = fix_video_opening(2, prompt_with_partial)
        self.assertTrue(fixed_partial.startswith("Use the provided first frame and last frame as exact composition anchors."))
        self.assertIn("IMAGE 2", fixed_partial)
        self.assertIn("IMAGE 3", fixed_partial)

    def test_aux_model_defaults_to_low_cost_for_reasoning_agent(self):
        self.assertEqual(_aux_model({'model': 'gemini-3-flash-agent'}), 'gemini-3.5-flash-low')
        self.assertEqual(_aux_model({'model': 'gemini-3-flash-agent', 'cheapModel': 'cheap-json-model'}), 'cheap-json-model')
        self.assertEqual(_aux_model({'model': 'gpt-5.5'}), 'gpt-5.5')

    def test_batch_adjacent_frame_semantics_maps_failures_to_beats(self):
        fake_response = json.dumps([
            {
                "beat": 2,
                "monotonic_errors": ["finished floor reverted to raw subfloor"],
                "delta_errors": []
            },
            {
                "beat": 3,
                "monotonic_errors": [],
                "delta_errors": ["no new construction progress"]
            }
        ])
        images = {
            1: "raw room",
            2: "new finished floor",
            3: "raw subfloor again",
            4: "raw subfloor again",
        }
        with patch('prompt_pipeline._chat', return_value=fake_response) as mocked_chat:
            failures = check_adjacent_frame_semantics_batch({'model': 'gemini-3-flash-agent'}, images)
        self.assertIn(2, failures)
        self.assertIn(3, failures)
        self.assertIn("Monotonic state regression", failures[2][0])
        self.assertIn("Static frame violation", failures[3][0])
        self.assertEqual(mocked_chat.call_args.kwargs['model'], 'gemini-3.5-flash-low')

    def test_fix_primary_landmarks_no_replacement(self):
        from prompt_pipeline import fix_primary_landmarks
        packet = {
            'primary_landmarks': [
                {'name': 'sliding door frame sill', 'grid': 'Grid B2'},
                {'name': 'metal support beam', 'grid': 'Grid A1'}
            ]
        }
        
        # 'frame' and 'support beam' must NOT be replaced, and since they do not match the exact names,
        # both landmarks must be appended to the end of the prompt.
        prompt = "The horizon line remains level at 50-percent height of the frame. A worker inspects the support beam."
        fixed = fix_primary_landmarks(prompt, packet)
        
        # Original text remains untouched (no replacement)
        self.assertIn("height of the frame", fixed)
        self.assertIn("inspects the support beam", fixed)
        
        # Missing/imprecise landmarks are appended at the end — as prose, never as grid labels
        # (2026-08-05: grid cells in the prompt body were rendered into frames as literal letters)
        self.assertIn("Locked anchors: sliding door frame sill at the centre of the frame; "
                      "metal support beam in the upper left of the frame.", fixed)
        self.assertNotIn("Grid B2", fixed)
        self.assertNotIn("Grid A1", fixed)

    def test_threshold_bridge_stage_validation(self):
        # TBCP v4: the entire crossing is ONE beat (bridge_stage=1), and it must not land
        # earlier than index 3 (>= 2 ordinary exterior beats must precede it).
        _MIN_PRE_THRESHOLD_BEATS = 2

        def _bridge_1_idx(ladder):
            idx = -1
            for i, b in enumerate(ladder):
                if b.get('bridge_stage') == 1 and idx < 0:
                    idx = i
            return idx

        # 1. Valid: single bridge_stage=1 beat, at index >= _MIN_PRE_THRESHOLD_BEATS.
        valid_ladder = [
            {"index": 1, "operation": "clearing", "description": "Clear site", "bridge_stage": None},
            {"index": 2, "operation": "repair", "description": "Patch exterior", "bridge_stage": None},
            {"index": 3, "operation": "threshold", "description": "Cross doorway", "bridge_stage": 1},
            {"index": 4, "operation": "reward", "description": "Finished room", "bridge_stage": None}
        ]
        b1 = _bridge_1_idx(valid_ladder)
        violations = []
        if b1 < 0:
            violations.append("In Threshold mode, there must be exactly one beat with bridge_stage=1 carrying the entire crossing.")
        elif b1 < _MIN_PRE_THRESHOLD_BEATS:
            violations.append("The threshold crossing beat (bridge_stage=1) must be at index 3 or later.")
        self.assertEqual(len(violations), 0)

        # 2. Invalid: crossing beat placed too early (Beat 1), no run-up at all.
        invalid_ladder = [
            {"index": 1, "operation": "threshold", "description": "Cross doorway", "bridge_stage": 1},
            {"index": 2, "operation": "reward", "description": "Finished room", "bridge_stage": None}
        ]
        b1 = _bridge_1_idx(invalid_ladder)
        violations = []
        if b1 < 0:
            violations.append("In Threshold mode, there must be exactly one beat with bridge_stage=1 carrying the entire crossing.")
        elif b1 < _MIN_PRE_THRESHOLD_BEATS:
            violations.append("The threshold crossing beat (bridge_stage=1) must be at index 3 or later.")
        self.assertEqual(len(violations), 1)

    def test_parse_and_format_prompt_slots_metadata(self):
        from prompt_pipeline import _parse_prompt_slots, _format_prompt_block
        block = """图片提示词
图片 1 [TRAUMA]:
Trauma state image prompt here.

图片 8 [BRIDGE]:
Bridge state image prompt here.

视频提示词
视频 1:
Video prompt 1 here.

视频 8 [BRIDGE]:
Video prompt 8 here.
"""
        images, videos = _parse_prompt_slots(block)
        
        self.assertIn(1, images)
        self.assertEqual(images[1]['body'], "Trauma state image prompt here.")
        self.assertEqual(images[1]['meta'], "TRAUMA")
        
        self.assertIn(8, images)
        self.assertEqual(images[8]['body'], "Bridge state image prompt here.")
        self.assertEqual(images[8]['meta'], "BRIDGE")
        
        self.assertIn(1, videos)
        self.assertEqual(videos[1]['body'], "Video prompt 1 here.")
        self.assertEqual(videos[1]['meta'], "")
        
        self.assertIn(8, videos)
        self.assertEqual(videos[8]['body'], "Video prompt 8 here.")
        self.assertEqual(videos[8]['meta'], "BRIDGE")
        
        # Format back and check that [BRIDGE] and [TRAUMA] are kept
        formatted = _format_prompt_block(images, videos)
        self.assertIn("图片 1 [TRAUMA]:", formatted)
        self.assertIn("图片 8 [BRIDGE]:", formatted)
        self.assertIn("视频 8 [BRIDGE]:", formatted)
        self.assertIn("视频 1:", formatted)

    def test_build_partial_prompt_block_tags_bridge_and_grows_incrementally(self):
        """_build_partial_prompt_block powers the progressive per-beat SSE reveal
        (on_progress('beat_ready', ...)) as well as the final reassembly, so it must:
        (1) apply the same BRIDGE tagging the final assembly always has, and
        (2) produce a strictly growing, always-valid block as beats accumulate."""
        from prompt_pipeline import _build_partial_prompt_block, _parse_prompt_slots

        beat_ladder = [
            {"operation": "demo", "bridge_stage": None},
            {"operation": "cross", "bridge_stage": 1},
        ]

        # After only IMAGE 1 (the anchor) is compiled.
        images, videos, block = _build_partial_prompt_block({1: "trauma state"}, {}, beat_ladder)
        self.assertEqual(images[1], {"body": "trauma state", "meta": ""})
        self.assertEqual(videos, {})
        parsed_images, parsed_videos = _parse_prompt_slots(block)
        self.assertIn(1, parsed_images)
        self.assertEqual(parsed_videos, {})

        # After beat 1 (VIDEO 1 + IMAGE 2) and beat 2 (bridge_stage=1, so IMAGE 3 is BRIDGE).
        # TBCP v4: the single crossing beat's own VIDEO is the real, visible merged clip —
        # also tagged BRIDGE (never discarded/HOLD).
        compiled_images = {1: "trauma state", 2: "beat 1 result", 3: "bridge entry"}
        compiled_videos = {1: "video 1", 2: "bridge video 2"}
        images, videos, block = _build_partial_prompt_block(compiled_images, compiled_videos, beat_ladder)
        self.assertEqual(images[3]['meta'], "BRIDGE")  # IMAGE 3 follows beat_ladder[1] (bridge_stage=1)
        self.assertEqual(videos[2]['meta'], "BRIDGE")  # VIDEO 2 is beat_ladder[1] itself
        self.assertEqual(images[2]['meta'], "")
        self.assertEqual(videos[1]['meta'], "")
        self.assertIn("图片 3 [BRIDGE]:", block)
        self.assertIn("视频 2 [BRIDGE]:", block)

        # Pan variant: the same single beat carries turn_direction -> VIDEO meta is
        # "BRIDGE TURN" (IMAGE meta stays plain "BRIDGE" — the IMAGE side never changed).
        pan_ladder = [
            {"operation": "demo", "bridge_stage": None},
            {"operation": "cross", "bridge_stage": 1, "turn_direction": "left"},
        ]
        images, videos, block = _build_partial_prompt_block(compiled_images, compiled_videos, pan_ladder)
        self.assertEqual(images[3]['meta'], "BRIDGE")
        self.assertEqual(videos[2]['meta'], "BRIDGE TURN")
        self.assertIn("视频 2 [BRIDGE TURN]:", block)

    def test_checkpoint_is_failed_terminal_detects_poisoned_resume(self):
        """A checkpoint whose fallback_count already exceeds the quality gate is a failed-terminal
        snapshot that must NOT be resumed as-is (else every retry is a zero-work instant re-fail)."""
        from prompt_pipeline import _checkpoint_is_failed_terminal

        # Production gate limit is zero: any saved placeholder marks a diagnostic/failed terminal.
        self.assertTrue(_checkpoint_is_failed_terminal(
            {"fallback_count": 3, "pass_beats_done": [2, 3, 5]}, 7))
        self.assertTrue(_checkpoint_is_failed_terminal({"fallback_count": 2}, 7))
        # A clean checkpoint remains resumable.
        self.assertFalse(_checkpoint_is_failed_terminal({"fallback_count": 0}, 7))
        # Robust to missing field / non-dict.
        self.assertFalse(_checkpoint_is_failed_terminal({}, 7))
        self.assertFalse(_checkpoint_is_failed_terminal(None, 7))
        # Sequence length does not relax the production zero-placeholder gate.
        self.assertTrue(_checkpoint_is_failed_terminal({"fallback_count": 3}, 30))

    def test_local_trim_to_budget_fits_and_keeps_ends(self):
        """Local trim (used when the 8046 aux model is unreachable for compression) must bring an
        over-budget prompt under the word limit while preserving the first and last sentences."""
        from prompt_pipeline import _local_trim_to_budget
        prompt = ("Use the provided first frame as anchor. "
                  "Middle detail one is verbose. Middle detail two is verbose. "
                  "Middle detail three is verbose. Middle detail four is verbose. "
                  "Locked anchors horizon level.")
        trimmed = _local_trim_to_budget(prompt, 12)
        self.assertLessEqual(len(trimmed.split()), 12)
        self.assertTrue(trimmed.startswith("Use the provided first frame as anchor."))
        self.assertIn("Locked anchors", trimmed)
        # Already under budget -> returned unchanged.
        short = "Short prompt here."
        self.assertEqual(_local_trim_to_budget(short, 50), short)

class TestPacketShapeNormalization(unittest.TestCase):
    """Regression tests for the Beat-2 abort: the packet LLM returned worker_choreography
    as a nested dict, and check_stylistic_repetition crashed on dict.lower()."""

    # Shape taken verbatim from the real poisoned cache entry (rooftop tram project)
    DICT_CHOREOGRAPHY = {
        "trajectory": "Workers enter from Grid C1 and exit via Grid C1 before the final frame.",
        "silhouette": "one lone worker in a solid bright-neon-yellow safety vest",
        "manual_tool_lock": "tools are locked to the worker's hands with no morphing",
    }

    def test_flatten_to_text(self):
        self.assertEqual(_flatten_to_text("already text"), "already text")
        self.assertEqual(_flatten_to_text(None), "")
        self.assertEqual(_flatten_to_text(["a", "b"]), "a b")
        flat = _flatten_to_text(self.DICT_CHOREOGRAPHY)
        self.assertIsInstance(flat, str)
        self.assertIn("trajectory:", flat)
        self.assertIn("safety vest", flat)

    def test_normalize_packet_flattens_prose_fields(self):
        packet = {
            "camera_dna": {"lens": "14mm", "height": "1.6m"},
            "worker_choreography": dict(self.DICT_CHOREOGRAPHY),
            "passive_environment": {"direction": "left-to-right", "elements": "clouds"},
            "primary_landmarks": [{"name": "column", "grid": "Grid B2", "z_depth_scale": 50}],
            "frame_boundaries": {"left": {"grid": "B1", "feature": "wall"}},
            "lighting_phase_ladder": {"1": "ambient only", "2": ["temporary", "work light"]},
            "object_ledger": [{"name": "bucket", "z_depth_scale": 10}],
        }
        normalize_packet(packet)
        self.assertIsInstance(packet["camera_dna"], str)
        self.assertIsInstance(packet["worker_choreography"], str)
        self.assertIn("safety vest", packet["worker_choreography"])
        self.assertIsInstance(packet["passive_environment"], str)
        self.assertIsInstance(packet["primary_landmarks"][0]["z_depth_scale"], str)
        self.assertIsInstance(packet["frame_boundaries"]["left"], str)
        self.assertIsInstance(packet["lighting_phase_ladder"]["2"], str)
        self.assertIsInstance(packet["object_ledger"][0]["z_depth_scale"], str)

    def test_normalize_packet_keeps_clean_packet_unchanged(self):
        packet = {"camera_dna": "static tripod shot", "worker_choreography": "one lone worker"}
        normalize_packet(packet)
        self.assertEqual(packet["camera_dna"], "static tripod shot")
        self.assertEqual(packet["worker_choreography"], "one lone worker")

    def test_check_stylistic_repetition_survives_dict_packet(self):
        # Even an UN-normalized packet must not crash the validator (defense in depth)
        packet = {"camera_dna": {"lens": "14mm"}, "worker_choreography": dict(self.DICT_CHOREOGRAPHY)}
        curr = "The worker lays grey stone tiles across the floor in repeated pressing cycles."
        prev = "The worker hoists timber studs into place and drives nails along the frame."
        errors = check_stylistic_repetition(curr, prev, packet, is_video=True)
        self.assertIsInstance(errors, list)

    def test_omni_cut_table_does_not_shatter_into_repeated_sentence_fragments(self):
        """切点表是逐拍逐字相同的契约结构件，不能被当成"抄了上一拍"。

        2026-08-06：句级去重按裸 [.!?] 断句，小数点把切点表切成 "5, a full shot from 1"
        这类残片；残片当然逐拍相同，于是几乎每一拍都报一条"相似度 1.00"，把真正的
        语义复读淹掉（实测一单 8/11 拍都是它）。is_mostly_boilerplate 里针对切点表的
        白名单本来就是按整句写的，断句修对了才真的生效。
        """
        cut_table = (
            'Cut this eight-second clip on these marks and hold no other cuts — an establishing '
            'long shot from 0.0 to 1.5, a full shot from 1.5 to 2.9, a medium shot from 2.9 to '
            '4.9, a close-up from 4.9 to 6.4, and a wide outro shot from 6.4 to 8.0 seconds.'
        )
        curr = cut_table + ' The worker lays grey stone tiles across the floor in pressing cycles.'
        prev = cut_table + ' The worker hoists timber studs into place and drives nails home.'
        errors = check_stylistic_repetition(curr, prev, {}, is_video=True)
        self.assertEqual([e for e in errors if 'sentence is too similar' in e], [])

    def test_genuinely_duplicated_sentences_are_still_caught(self):
        # 句子里不能带 is_mostly_boilerplate 的环境/材料白名单词（tile、dust、seam…），
        # 否则它会被正当地当成"逐拍应当保持一致"的内容而豁免掉。
        repeated = 'The lone builder lifts each heavy granite slab onto the raised platform.'
        curr = repeated + ' A hush settles over the alcove.'
        prev = repeated + ' Rain taps against the shutters.'
        errors = check_stylistic_repetition(curr, prev, {}, is_video=True)
        self.assertTrue([e for e in errors if 'sentence is too similar' in e])

    def test_normalize_beat_ladder(self):
        ladder = [
            {"index": "1", "operation": "clearing", "description": {"text": "remove debris"}, "bridge_stage": None},
            {"index": 2, "operation": ["threshold", "approach"], "description": "push to sill", "bridge_stage": "1"},
        ]
        normalize_beat_ladder(ladder)
        self.assertEqual(ladder[0]["index"], 1)
        self.assertIsInstance(ladder[0]["description"], str)
        self.assertIn("remove debris", ladder[0]["description"])
        self.assertIsInstance(ladder[1]["operation"], str)
        self.assertEqual(ladder[1]["bridge_stage"], 1)

    def test_normalize_beat_ladder_fills_missing_operation_and_description(self):
        # 回归：description/operation 不在 milestone 门禁的必填清单里，threshold/reward/
        # 桥接拍还整段跳过那道门禁 —— LLM 漏给这两个键时，坏账一路漏到下游的字面取值
        # (beats_desc / beat_user) 才炸成 KeyError('description')，用户侧只看到一句
        # 「合成失败：'description'」。归一化必须在收口处补齐。
        ladder = normalize_beat_ladder([
            {"index": 1, "operation": "threshold", "bridge_stage": 1},
            {"index": 2, "description": "  ", "milestone_name": "interior cleared",
             "after_state": "the floor is swept back to bare boards"},
            {"index": 3, "operation": "", "description": None, "milestone_name": "roof panelled"},
            {"index": 4, "operation": "reward"},
        ])
        for beat in ladder:
            self.assertIsInstance(beat["description"], str)
            self.assertTrue(beat["description"].strip())
            self.assertIsInstance(beat["operation"], str)
            self.assertTrue(beat["operation"].strip())
        self.assertEqual(ladder[0]["operation"], "threshold")
        self.assertIn("interior cleared", ladder[1]["description"])
        self.assertIn("bare boards", ladder[1]["description"])
        self.assertEqual(ladder[2]["description"], "roof panelled")
        self.assertEqual(ladder[2]["operation"], "repair")
        # 下游的字面取值形态本身必须不再抛 KeyError
        "\n".join(f"Beat {b['index']}: {b['operation']} - {b['description']}" for b in ladder)

    def test_normalize_beat_ladder_keeps_declared_operation_and_description(self):
        # 补齐逻辑只填空缺，绝不覆盖 LLM 已经给出的真实内容
        ladder = normalize_beat_ladder([
            {"index": 1, "operation": "clearing", "description": "haul the debris out",
             "milestone_name": "site cleared"},
        ])
        self.assertEqual(ladder[0]["operation"], "clearing")
        self.assertEqual(ladder[0]["description"], "haul the debris out")

    def test_normalize_beat_ladder_object_lifecycle_keys(self):
        # 与 anchor_keywords 那组"缺失就补 []"的口径刻意相反：introduced_objects /
        # removed_objects 缺失时**必须保持缺失**，milestone_ladder_violations 要靠这个
        # 区分"没声明"（硬伤）和"声明了空数组"（合法）。只在键本来就存在时做形状归一化。
        ladder = normalize_beat_ladder([
            {"index": 1, "operation": "clearing", "description": "d1"},
            {"index": 2, "operation": "framing", "description": "d2",
             "introduced_objects": "single string form", "removed_objects": None},
            {"index": 3, "operation": "drywall", "description": "d3",
             "introduced_objects": ["cast-iron stove", "  ", 42]},
        ])
        self.assertNotIn("introduced_objects", ladder[0])
        self.assertNotIn("removed_objects", ladder[0])
        self.assertEqual(ladder[1]["introduced_objects"], ["single string form"])
        self.assertEqual(ladder[1]["removed_objects"], [])
        self.assertEqual(ladder[2]["introduced_objects"], ["cast-iron stove", "42"])
        self.assertNotIn("removed_objects", ladder[2])

    def test_normalize_beat_ladder_anchor_keywords(self):
        # SIGNATURE ANCHOR RULE (2026-07-20): the reward beat's anchor_keywords must
        # survive normalization as a clean list of non-empty strings, defaulting to []
        # everywhere it wasn't declared (only meaningful on the reward beat) or was
        # malformed (a bare string instead of a list, stray blanks, non-string items).
        ladder = [
            {"index": 1, "operation": "clearing", "description": "d1", "bridge_stage": None},
            {"index": 2, "operation": "reward", "description": "d2", "bridge_stage": None,
             "anchor_keywords": ["cast-iron valve stove", "  ", 42, "suspended above the hearth"]},
            {"index": 3, "operation": "reward", "description": "d3", "bridge_stage": None,
             "anchor_keywords": "single string form"},
        ]
        normalize_beat_ladder(ladder)
        self.assertEqual(ladder[0]["anchor_keywords"], [])
        self.assertEqual(ladder[1]["anchor_keywords"], ["cast-iron valve stove", "42", "suspended above the hearth"])
        self.assertEqual(ladder[2]["anchor_keywords"], ["single string form"])


class TestShotFamilySpatialLocks(unittest.TestCase):
    """空间逻辑修复回归：桥接后镜头族交接、锚点比例锁、锚点子句去重、
    桥接帧相机线保全、pan/tilt 封禁。四种坏形状均取自 2026-07-12 hollow-oak 实际产出。"""

    PACKET = {
        'camera_dna': (
            "Static wide-angle eighteen-millimeter tripod shot at one-point-five meters height, "
            "locked eye-level perspective facing the hollow oak trunk; horizon line remains level "
            "at fifty percent height."
        ),
        'geometry_lock': 'trunk shell fixed',
        'primary_landmarks': [
            {'name': 'decaying trunk base opening', 'grid': 'Grid C2', 'z_depth_scale': '35%'},
            {'name': 'curved interior cavity wall', 'grid': 'Grid B2', 'z_depth_scale': '65%'},
            {'name': 'misty forest canopy', 'grid': 'Grid A2', 'z_depth_scale': '45%'},
        ],
        'frame_boundaries': {'left': 'B1', 'right': 'B3', 'top': 'A2', 'bottom': 'C2'},
        'interior_camera_dna': (
            "Static tripod shot inside the hollow trunk chamber, same ultra-wide lens feel and same "
            "camera height, camera pitch locked level; the central vanishing axis stays centered on "
            "the rear cavity wall in Grid B2."
        ),
        'interior_primary_landmarks': [
            {'name': 'heartwood ridge', 'grid': 'Grid B2', 'z_depth_scale': '60%'},
            {'name': 'mossy root shelf', 'grid': 'Grid C2', 'z_depth_scale': '30%'},
        ],
    }
    LADDER = [
        {'index': 1, 'operation': 'clearing', 'description': 'clear debris', 'bridge_stage': None},
        {'index': 2, 'operation': 'framing', 'description': 'frame walls', 'bridge_stage': None},
        {'index': 3, 'operation': 'threshold', 'description': 'cross the sill', 'bridge_stage': 1},
        {'index': 4, 'operation': 'paneling', 'description': 'panel interior', 'bridge_stage': None},
        {'index': 5, 'operation': 'reward', 'description': 'final reveal', 'bridge_stage': None},
    ]

    def test_beat_space_family_hands_off_at_bridge(self):
        from prompt_pipeline import beat_space_family
        self.assertEqual(beat_space_family(self.LADDER, 1), 'exterior')
        self.assertEqual(beat_space_family(self.LADDER, 2), 'exterior')
        self.assertEqual(beat_space_family(self.LADDER, 3), 'interior')
        self.assertEqual(beat_space_family(self.LADDER, 4), 'interior')
        self.assertEqual(beat_space_family(self.LADDER, 5), 'interior')
        no_bridge = [dict(b, bridge_stage=None) for b in self.LADDER]
        for i in range(1, 6):
            self.assertEqual(beat_space_family(no_bridge, i), 'exterior')

    def test_fix_primary_landmarks_dedupes_and_locks_scale(self):
        # 实际产出的图4形状：LLM 简称+错比例的 Locked anchors 与追加的全名版并存
        from prompt_pipeline import fix_primary_landmarks
        prompt = (
            "Static wide-angle eighteen-millimeter tripod shot facing the hollow oak trunk. "
            "Horizon line remains level at fifty percent height. "
            "Locked anchors: trunk base at Grid C2 (35 percent height), cavity wall at Grid B2 "
            "(65 percent height), frame at Grid B2 (55 percent height), canopy at Grid A2 (45 percent height). "
            "Locked anchors: decaying trunk base opening at Grid C2, curved interior cavity wall at Grid B2, "
            "misty forest canopy at Grid A2."
        )
        fixed = fix_primary_landmarks(prompt, self.PACKET, family='exterior')
        self.assertEqual(fixed.lower().count('locked anchors:'), 1)
        self.assertIn('decaying trunk base opening across the lower centre of the frame, '
                      'rising to about a third of the frame height', fixed)
        self.assertIn('curved interior cavity wall at the centre of the frame, '
                      'rising to about two thirds of the frame height', fixed)
        self.assertIn('misty forest canopy across the upper centre of the frame, '
                      'rising to about two fifths of the frame height', fixed)
        self.assertNotIn('55 percent', fixed)
        # 规范锚点句本身绝不能带回记号（正文别处的旧记号由 scrub_spatial_notation 兜）
        stanza = fixed[fixed.lower().index('locked anchors:'):]
        self.assertNotRegex(stanza, r'Grid [A-C][1-3]|\d+ percent')

    def test_fix_primary_landmarks_interior_uses_interior_set(self):
        from prompt_pipeline import fix_primary_landmarks
        prompt = (
            "Blonde oak panels line the walls. Camera pitch locked level; the central vanishing axis "
            "stays centered. Locked anchors: decaying trunk base opening at Grid C2, curved interior "
            "cavity wall at Grid B2, misty forest canopy at Grid A2."
        )
        fixed = fix_primary_landmarks(prompt, self.PACKET, family='interior')
        self.assertNotIn('misty forest canopy', fixed)
        self.assertIn('heartwood ridge at the centre of the frame, '
                      'rising to about three fifths of the frame height', fixed)
        self.assertIn('mossy root shelf across the lower centre of the frame, '
                      'rising to about a third of the frame height', fixed)

    def test_fix_primary_landmarks_interior_without_registered_set_strips_exterior_stanza(self):
        from prompt_pipeline import fix_primary_landmarks
        packet_no_interior = dict(self.PACKET)
        packet_no_interior.pop('interior_primary_landmarks', None)
        prompt = (
            "The threshold edges hug the left and right boundaries. "
            "Locked anchors: decaying trunk base opening at Grid C2, curved interior cavity wall at "
            "Grid B2, misty forest canopy at Grid A2."
        )
        fixed = fix_primary_landmarks(prompt, packet_no_interior, family='interior')
        self.assertNotIn('locked anchors', fixed.lower())
        self.assertIn('threshold edges hug', fixed)

    def test_check_anchor_scale_lock_catches_oscillation(self):
        # 实际产出的图7/9形状：同一锚点比例 35→55 / 65→85 / 45→25 振荡
        from prompt_pipeline import check_anchor_scale_lock
        drifted = (
            "Locked anchors: decaying trunk base opening at Grid C2 holds a scale of fifty-five "
            "percent of frame height, curved interior cavity wall at Grid B2 holds a scale of "
            "eighty-five percent of frame height, misty forest canopy at Grid A2 holds a scale of "
            "twenty-five percent of frame height."
        )
        errs = check_anchor_scale_lock(drifted, self.PACKET, family='exterior')
        self.assertEqual(len(errs), 3)
        good = (
            "Locked anchors: decaying trunk base opening at Grid C2 holding 35 percent of frame "
            "height, curved interior cavity wall at Grid B2 holding sixty-five percent of frame "
            "height, misty forest canopy at Grid A2 holding forty-five percent of frame height. "
            "The horizon line remains perfectly level at exactly 50-percent height of the frame."
        )
        self.assertEqual(check_anchor_scale_lock(good, self.PACKET, family='exterior'), [])

    def test_check_worker_scale_lock_catches_mismatch(self):
        from prompt_pipeline import check_worker_scale_lock
        packet = dict(self.PACKET, worker_scale_percent='18%')
        drifted = ("At t=0s, one lone worker enters the frame from the Grid C1 edge, standing "
                   "roughly 40 percent of frame height; the worker hammers beams into place.")
        errs = check_worker_scale_lock(drifted, packet)
        self.assertEqual(len(errs), 1)
        # 判据按"桶"而不是按数字：40%（约五分之二）与 18%（约六分之一）分属不同档位
        self.assertIn('about a sixth of the frame height', errs[0])
        good = ("At t=0s, one lone worker enters the frame from the Grid C1 edge, standing "
               "roughly 18 percent of frame height; the worker hammers beams into place.")
        self.assertEqual(check_worker_scale_lock(good, packet), [])
        # No worker present -> never flags; no packet scale locked -> never flags either
        self.assertEqual(check_worker_scale_lock('A clean empty frame with no agents.', packet), [])
        self.assertEqual(check_worker_scale_lock(drifted, self.PACKET), [])

    def test_check_shot_family_leakage(self):
        from prompt_pipeline import check_shot_family_leakage
        # 实际产出的图7-10形状：穿越后仍把室外锚点钉回网格 + 提及 canopy/horizon
        leaked = (
            "Glowing brass sconces activate. Locked anchors: decaying trunk base opening at Grid C2, "
            "misty forest canopy at Grid A2. The horizon line remains perfectly level."
        )
        errs = check_shot_family_leakage(leaked, self.PACKET, family='interior')
        self.assertTrue(any('decaying trunk base opening' in e for e in errs))
        self.assertTrue(any('horizon' in e for e in errs))
        clean = (
            "Blonde oak panels line the walls. Interior anchors hold steady. Camera pitch locked "
            "level; the central vanishing axis stays centered."
        )
        self.assertEqual(check_shot_family_leakage(clean, self.PACKET, family='interior'), [])
        # 室外帧不受此检查约束
        self.assertEqual(check_shot_family_leakage(leaked, self.PACKET, family='exterior'), [])

    def test_bridge_image_keeps_a_camera_declaration(self):
        # 实际产出的图5/6形状：旧逻辑把静态相机句整句删除且不补任何相机声明
        from prompt_pipeline import apply_proactive_fixes
        beat = self.LADDER[2]  # bridge_stage 1 — the single threshold beat, family 'interior'
        image = (
            "Static wide-angle eighteen-millimeter tripod shot at one-point-five meters height, "
            "locked eye-level perspective facing the hollow oak trunk. Backlit by overcast daylight, "
            "a work light illuminates the dim interior's timber framing."
        )
        video = (
            "Use the provided first frame and last frame as exact composition anchors. Use IMAGE 3 as "
            "the actual first-frame image and IMAGE 4 as the actual last-frame image; every visible "
            "action must interpolate between those two frame images without inventing a third layout. "
            "Camera executes a coaxial forward push-in toward the doorway."
        )
        v, img = apply_proactive_fixes(3, video, image, self.PACKET, 'Threshold', False, True,
                                       beat=beat, config=None, family='interior')
        self.assertNotIn('facing the hollow oak trunk', img)
        self.assertIn('vanishing axis', img.lower())
        self.assertIn('rear cavity wall', img.lower())

    def test_pan_tilt_ban_is_negation_aware(self):
        from prompt_pipeline import check_camera_contradictions, fix_camera_contradictions
        # 实际产出的视频5形状：桥接段 180 度横摇
        bad = ("The camera pushes forward, panning 180 degrees across the chamber. "
               "Worker installs the frame.")
        errs = check_camera_contradictions(bad, True, ban_pan_tilt=True)
        self.assertTrue(any('pan' in e.lower() for e in errs))
        fixed = fix_camera_contradictions(bad, True, ban_pan_tilt=True)
        self.assertNotIn('panning', fixed)
        self.assertIn('Worker installs the frame.', fixed)
        # TBCP 护栏句（否定式）不得自伤；工人动作里的 pan 名词不受牵连
        guarded = ("The camera advances straight toward the doorway with no yaw, tilt, roll, or "
                   "side-step. The worker sweeps sawdust into a dust pan.")
        self.assertEqual(check_camera_contradictions(guarded, True, ban_pan_tilt=True), [])
        self.assertIn('dust pan', fix_camera_contradictions(guarded, True, ban_pan_tilt=True))

    def test_fix_horizon_line_family_aware(self):
        from prompt_pipeline import fix_horizon_line
        interior = "Blonde oak panels line the walls. The horizon line remains perfectly level at exactly 50-percent height of the frame."
        fixed = fix_horizon_line(interior, family='interior')
        self.assertNotIn('horizon', fixed.lower())
        self.assertIn('pitch locked level', fixed.lower())
        exterior = "The decaying trunk stands in mist."
        fixed_ext = fix_horizon_line(exterior, family='exterior')
        self.assertIn('horizon line', fixed_ext.lower())

    def test_found_carrier_scale_lock_holds_subject_scale_and_cavity_enclosure(self):
        """2026-08-05 实机：找到型载体此前没有任何主体尺度锁，IMAGE 1 把前景框景当成了主体，
        真正的载体缩成中景一粒，而那个"腔体"是穿透的——透过它能看见背景水面和树。"""
        from prompt_pipeline import fix_found_carrier_scale_lock
        packet = dict(self.PACKET, origin_contract={'mode': 'existing_restoration'})
        prompt = "Static wide tripod shot facing the hollow oak trunk. Locked anchors: trunk base at Grid C2."

        fixed = fix_found_carrier_scale_lock(prompt, packet, family='exterior')
        self.assertIn('longest visible dimension spanning roughly two-thirds', fixed)
        self.assertIn('dead-end enclosed volume', fixed)
        self.assertIn('Locked anchors: trunk base at Grid C2.', fixed)
        # 幂等：反复修复不得累积互相矛盾的尺度句
        self.assertEqual(fixed, fix_found_carrier_scale_lock(fixed, packet, family='exterior'))

        # 室内族、交付型载体两条都不该被这把锁碰到
        self.assertEqual(fix_found_carrier_scale_lock(prompt, packet, family='interior'), prompt)
        delivered = dict(packet, origin_contract={'mode': 'carrier_delivery_build'})
        self.assertEqual(fix_found_carrier_scale_lock(prompt, delivered, family='exterior'), prompt)

        # 没有登记室内的项目（纯外部修复）只锁尺度，不谈盲端腔体
        no_interior = {k: v for k, v in packet.items() if k != 'interior_camera_dna'}
        exterior_only = fix_found_carrier_scale_lock(prompt, no_interior, family='exterior')
        self.assertIn('longest visible dimension spanning roughly two-thirds', exterior_only)
        self.assertNotIn('dead-end enclosed volume', exterior_only)

    def test_normalize_packet_coerces_interior_fields(self):
        packet = {
            'camera_dna': 'static tripod shot',
            'interior_camera_dna': {'text': 'inside chamber shot'},
            'interior_primary_landmarks': [
                {'name': 'heartwood ridge', 'grid': {'cell': 'Grid B2'}, 'z_depth_scale': 60},
            ],
        }
        normalize_packet(packet)
        self.assertIsInstance(packet['interior_camera_dna'], str)
        self.assertIsInstance(packet['interior_primary_landmarks'][0]['grid'], str)
        self.assertIsInstance(packet['interior_primary_landmarks'][0]['z_depth_scale'], str)


class TestStageScopeBeatDirective(unittest.TestCase):
    def test_three_tiers_and_none_produce_distinct_text(self):
        large = _stage_scope_beat_directive('large')
        small = _stage_scope_beat_directive('small')
        default = _stage_scope_beat_directive('default')
        none_text = _stage_scope_beat_directive(None)
        texts = [large, small, default, none_text]
        self.assertEqual(len(set(texts)), len(texts))
        self.assertEqual(none_text, '')

    def test_large_tier_demands_full_coverage_language(self):
        text = _stage_scope_beat_directive('large')
        self.assertIn('ENTIRE', text)
        self.assertIn('MAJOR', text)
        self.assertIn('FRAME-WIDE', text)

    def test_small_tier_names_subregion_and_untouched_remainder(self):
        text = _stage_scope_beat_directive('small')
        self.assertIn('LOCALIZED', text)
        self.assertIn('sub-region', text)
        self.assertIn('untreated state', text)
        self.assertIn('never full coverage', text)

    def test_default_tier_permits_partial_phrasing(self):
        text = _stage_scope_beat_directive('default')
        self.assertIn('"one section"', text)
        self.assertIn('"part of"', text)
        self.assertIn('"begins to"', text)
        self.assertIn('PERMITTED', text)

    def test_unknown_scope_returns_empty_string(self):
        self.assertEqual(_stage_scope_beat_directive('medium'), '')
        self.assertEqual(_stage_scope_beat_directive(''), '')

    def test_img_before_after_substitution_generic_labels(self):
        # 'small' 档只引用 img_after（局部完工只跟"完工后"的这一帧比较），'default' 档
        # 两者都引用 —— 用 default 档验证通用占位符（默认值)能被正确替换进正文。
        text = _stage_scope_beat_directive('default')
        self.assertIn("this beat's starting IMAGE", text)
        self.assertIn("this beat's resulting IMAGE", text)

    def test_img_before_after_substitution_numbered_labels(self):
        text = _stage_scope_beat_directive('large', img_before='IMAGE 5', img_after='IMAGE 6')
        self.assertIn('IMAGE 5', text)
        self.assertIn('IMAGE 6', text)
        self.assertNotIn("this beat's starting IMAGE", text)


if __name__ == '__main__':
    unittest.main()
