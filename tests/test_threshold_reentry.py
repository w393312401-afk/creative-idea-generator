"""复入场过门：回到已经施工过的空间时，不许把它重新画成废墟。

2026-08-15 用户复盘：外景已经交付成品温室（屋顶重建、门装好），镜头一推门进去，室内
又变回杂草、裸土、原始天花板。根因不在规划梯子——梯子是单调的——而在渲染层：
`ground_threshold_reveal_prompt` 的系统提示写死了「nobody has entered this space yet」，
并且调用侧 `item['prompt'] = grounded_body` 把组稿阶段写好的继承状态整条替换掉。
"""

import os
from unittest.mock import patch

from prompt_pipeline import (
    ground_threshold_reveal_prompt,
    threshold_reveal_continuity_clause,
)
from prompt_pipeline.frame_state import build_space_state_ledger, space_entry_context


def _ladder():
    """原片口径：室内清理 → 室内铺地板 → 出门 → 外景屋顶/门 → 进门 → 室内软装。"""
    return [
        {"index": 1, "space": "greenhouse interior", "operation": "clearing",
         "after_state": "all collapsed timber cleared, bare earth floor exposed"},
        {"index": 2, "space": "greenhouse interior", "operation": "flooring",
         "after_state": "wooden plank decking fully covers the slab, installation complete"},
        {"index": 3, "space": "greenhouse interior", "operation": "threshold",
         "after_state": "camera holds at the doorway"},
        {"index": 4, "space": "garden exterior", "operation": "framing",
         "after_state": "roof rebuilt with new glazing bars and green roof laid, complete"},
        {"index": 5, "space": "garden exterior", "operation": "painting",
         "after_state": "front doors hung and painted"},
        {"index": 6, "space": "garden exterior", "operation": "threshold",
         "after_state": "camera pushes through the front doors"},
        {"index": 7, "space": "greenhouse interior", "operation": "furnishing",
         "after_state": "bed and practical lighting placed"},
    ]


def test_ledger_skips_transition_beats():
    """过门拍不承载建造状态，记进账里会让「上次离开的样子」变成一句运镜描述。"""
    ledger = build_space_state_ledger(_ladder())
    assert [v["index"] for v in ledger["greenhouse interior"]] == [1, 2, 7]
    assert [v["index"] for v in ledger["garden exterior"]] == [4, 5]


def test_reentry_is_detected_and_carries_the_latest_state():
    ctx = space_entry_context(_ladder(), 7)
    assert ctx["first_entry"] is False
    assert ctx["last_seen_index"] == 2
    assert "decking fully covers the slab" in ctx["inherited_state"]
    # 被后续工序取代的中间态不回放：同时命令模型画裸土和画地板，画面只会二选一。
    assert "bare earth" not in ctx["inherited_state"]


def test_roof_completed_outside_is_carried_into_the_interior():
    """屋顶/天花板是一个物件被两个空间看见，按空间分账治不了它。"""
    ctx = space_entry_context(_ladder(), 7)
    assert "roof rebuilt" in ctx["carried_structural"]


def test_first_entry_does_not_inherit_a_raw_state_from_another_space():
    """清理拍的 "bare earth floor exposed" 命中 floor 关键词，但它不是「已完工」。"""
    ctx = space_entry_context(_ladder(), 4)
    assert ctx["first_entry"] is True
    assert ctx["inherited_state"] == ""
    assert ctx["carried_structural"] == ""


def test_missing_space_field_falls_back_to_first_entry():
    """老 manifest（2026-08-14 之前跑的）没有 space 字段，行为必须与改动前一致。"""
    ctx = space_entry_context([{"index": 1, "operation": "clearing", "after_state": "cleared"}], 9)
    assert ctx["first_entry"] is True
    assert ctx["inherited_state"] == ""


class _Captured:
    def __init__(self):
        self.system = None
        self.images = None
        self.user_text = None

    def __call__(self, config, system, user_text, image_paths, **kwargs):
        self.system = system
        self.user_text = user_text
        self.images = list(image_paths)
        return "a settled interior"


_COMPOSED = (
    "Static eye-level 14mm ultra-wide tripod shot at 1.6m height; camera pitch locked level. "
    "Locked anchors: discolored concrete sidewall joint along the mid-left of the frame. "
    "The completed prior work remains fully unchanged: interior walls are fully framed with "
    "wooden studs and insulated with yellow batts. New state delta: pale pine wall panelling "
    "now covers both side walls up to the ceiling curve."
)


def test_grounding_never_mandates_a_ruin():
    """整条流水线里再没有一处规定门后必须是原始态——那是组稿阶段的事。"""
    cap = _Captured()
    with patch("prompt_pipeline._multimodal_chat", cap):
        ground_threshold_reveal_prompt({}, "/tmp/ref.jpg", _COMPOSED)
    for banned in ("nobody has entered this space yet", "Untouched trauma",
                   "Zero intervention evidence", "already cleaned/repaired/painted"):
        assert banned not in cap.system
    assert "construction state is NOT yours to decide" in cap.system


def test_the_composed_prompt_is_what_gets_corrected():
    """组稿产物必须原文进到请求里——它是状态的唯一权威，不能被另写一条替换掉。"""
    cap = _Captured()
    with patch("prompt_pipeline._multimodal_chat", cap):
        ground_threshold_reveal_prompt({}, "/tmp/ref.jpg", _COMPOSED)
    assert _COMPOSED in cap.user_text
    assert "New state delta" in cap.user_text


def test_no_composed_prompt_means_no_grounding():
    """没有可改写的正文就别硬造一条——硬造正是老毛病的来源。"""
    with patch("prompt_pipeline._multimodal_chat", _Captured()):
        assert ground_threshold_reveal_prompt({}, "/tmp/ref.jpg", "") is None
        assert ground_threshold_reveal_prompt({}, "/tmp/ref.jpg", None) is None


def test_a_gutted_rewrite_is_rejected():
    """改写回来只剩一句话 = 机位/锚点/state delta 被丢了，沿用组稿产物。"""
    with patch("prompt_pipeline._multimodal_chat", return_value="A ruined concrete room."):
        assert ground_threshold_reveal_prompt({}, "/tmp/ref.jpg", _COMPOSED) is None


def test_ledger_context_reinforces_but_never_contradicts():
    cap = _Captured()
    with patch("prompt_pipeline._multimodal_chat", cap):
        ground_threshold_reveal_prompt(
            {}, "/tmp/ref.jpg", _COMPOSED,
            first_entry=False,
            inherited_state="decking installation complete",
            carried_structural="roof rebuilt, complete")
    assert "decking installation complete" in cap.user_text
    assert "roof rebuilt, complete" in cap.user_text
    assert _COMPOSED in cap.user_text


def test_last_seen_frame_is_attached_when_available(tmp_path):
    """闭门起帧下参考图里没有室内像素，这个空间上次那张真实帧必须一并递过去。"""
    last_seen = tmp_path / "img_003.webp"
    last_seen.write_bytes(b"x")
    cap = _Captured()
    with patch("prompt_pipeline._multimodal_chat", cap):
        ground_threshold_reveal_prompt(
            {}, "/tmp/ref.jpg", _COMPOSED, first_entry=False,
            last_seen_image_path=str(last_seen))
    assert cap.images == ["/tmp/ref.jpg", str(last_seen)]


def test_continuity_clause_is_empty_without_state():
    """没有前情就不该往提示词里塞一句空洞的延续声明。"""
    assert threshold_reveal_continuity_clause("", "") == ""


def test_continuity_clause_states_both_sources():
    clause = threshold_reveal_continuity_clause(
        "decking complete", "roof rebuilt, complete")
    assert "decking complete" in clause
    assert "roof rebuilt" in clause
    assert "under construction again" in clause


def test_reveal_falls_back_to_the_composed_prompt_when_the_call_fails():
    """订正失败不阻塞渲染链：返回 None，调用方沿用组稿阶段的提示词。"""
    with patch("prompt_pipeline._multimodal_chat", side_effect=RuntimeError("gateway down")):
        assert ground_threshold_reveal_prompt({}, "/tmp/ref.jpg", _COMPOSED) is None


def test_renderer_context_resolves_the_last_seen_frame(tmp_path):
    """第 2 拍的终点帧是 IMAGE 3——拍号换算错一位就会挂上邻居空间的帧。"""
    import frame_generator

    frames_dir = tmp_path / "frames"
    frames_dir.mkdir()
    (frames_dir / "img_003.webp").write_bytes(b"x")
    ctx = frame_generator._threshold_reveal_context(
        {"spatial_beats": _ladder()}, 8, str(frames_dir))
    assert ctx["first_entry"] is False
    assert ctx["last_seen_image_path"] == os.path.join(str(frames_dir), "img_003.webp")


def test_renderer_context_survives_a_manifest_without_beats(tmp_path):
    import frame_generator

    ctx = frame_generator._threshold_reveal_context({}, 5, str(tmp_path))
    assert ctx == {"first_entry": True, "inherited_state": "", "carried_structural": ""}


def test_a_ladder_without_any_space_label_never_claims_a_reentry():
    """老 manifest 每一拍都兜底成 'primary'，整条塌成一个桶。拿兜底值当判据，会把
    真正的新空间判成复入场，反过来命令模型把没动工的房间画成完工——整条退回首入场。"""
    ladder = [
        {"index": 1, "operation": "clearing", "after_state": "rubble cleared"},
        {"index": 2, "operation": "flooring", "after_state": "decking installation complete"},
        {"index": 3, "operation": "threshold", "after_state": "camera crosses"},
        {"index": 4, "operation": "framing", "after_state": "new frame raised"},
    ]
    ctx = space_entry_context(ladder, 4)
    assert ctx["first_entry"] is True
    assert ctx["inherited_state"] == ""
    assert ctx["carried_structural"] == ""


def test_renderer_hands_the_composed_prompt_to_the_grounding_call(tmp_path, monkeypatch):
    """真正出事的那一步：渲染层曾经 `item['prompt'] = grounded_body` 整条替换掉组稿产物。

    实测那一单 16 张图里 4 张是过门落点帧，四条提示词全都正确写着
    "Prior structural elements remain completely unchanged" / "New state delta: …"，
    全部被丢弃——回退和「这一拍的工序也没了」都是从这里来的。
    """
    import frame_generator
    import server_common

    seen = {}

    def fake_ground(config, reference, composed=None, **kw):
        seen['composed'] = composed
        return None  # 沿用组稿产物，本用例只关心传进去的是什么

    def fake_edit(cfg, prompt, reference_path, target_path, *a, **kw):
        os.makedirs(os.path.dirname(target_path), exist_ok=True)
        with open(target_path, 'wb') as f:
            f.write(b'x')

    monkeypatch.setattr(server_common, 'OUTPUT_ROOT', str(tmp_path))
    monkeypatch.setattr('prompt_pipeline.ground_threshold_reveal_prompt', fake_ground)
    monkeypatch.setattr(frame_generator, '_generate_image_edit', fake_edit)
    monkeypatch.setattr(frame_generator, '_generate_text_image',
                        lambda cfg, prompt, target: fake_edit(cfg, prompt, None, target))
    monkeypatch.setattr(frame_generator, 'detect_anchor_inertia', lambda *a, **kw: (False, 0.0))

    block = (
        "图片 1:\nexterior prompt\n\n"
        "图片 2:\nPrior structural elements remain completely unchanged. "
        "New state delta: pale pine panelling now covers both side walls.\n\n"
        "视频 1 [BRIDGE]:\nbridge video\n"
    )
    frame_generator.generate_frame_sequence(
        {'allowTextOnlyAnchor': True}, 'reveal_composed_prompt', block)

    assert 'composed' in seen, "过门落点帧没有走到接地调用"
    assert 'New state delta' in (seen['composed'] or ''), \
        "组稿产物没有传进接地调用——状态权威又被丢了"
