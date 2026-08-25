"""Authoritative structured scene-state transitions and deterministic prompt skeletons."""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any


TRANSITIONS = {"threshold", "reward", "reframe"}
REMOVAL = {"clearing", "demolition", "removal", "excavation"}
INSTALLATION = {"installation", "framing", "rough-in", "rough_in", "wiring", "plumbing", "lighting"}
WET_WORK = {"wet-work", "wet_work", "plastering", "concrete", "priming", "painting"}
COVERING = {"covering", "drywall", "paneling", "flooring", "finishing"}
FURNISHING = {"furnishing", "placement", "move-in", "move_in"}

# 临时态 vs 持久态物体生命周期规则（子串匹配，风格与 __init__.py 里的
# _ANCHOR_RETIRE_CUES/_ANCHOR_TRANSFORM_CUES 一致）。TEMPORARY 是施工过程中的
# 工具/设备，一旦进入 furnishing 阶段就必须已经清场；PERSISTENT_STRUCTURAL 是
# 承重/铺装类结构件，一旦 introduced 就不许再出现在任何一拍的 removed_objects
# 里——这两类都是关键词提示，不是穷举分类，误报风险由调用方按需要降级为
# warning-only（见 validate_scene_states 的 6/6 事故复盘同款克制原则）：反推复刻线
# 就是这么降级的，两条规则在那里都只记诊断不拦单。
TEMPORARY_OBJECT_CUES = {
    "scaffold", "scaffolding", "ladder", "crane", "tarp", "tarpaulin",
    "temporary brace", "temporary bracing", "work platform", "mixer", "generator",
    "power tool", "toolbox", "tool cart", "workbench", "site fencing", "site fence",
    "dust sheet", "drop cloth", "safety barrier", "construction light",
}
PERSISTENT_STRUCTURAL_CUES = {
    "floor", "flooring", "arch", "archway", "threshold",
    "foundation", "load-bearing wall", "structural wall", "roof", "ceiling beam",
}


def _matches_cue(name: str, cues: set[str]) -> bool:
    lname = _text(name).lower()
    return bool(lname) and any(cue in lname for cue in cues)


def _is_persistent_structural(name: str) -> bool:
    """这个物体名是不是"一旦铺设就全程继承"的结构件。

    临时态优先：PERSISTENT_STRUCTURAL_CUES 是子串匹配，"floor protection sheet"、
    "floor-level dust sheet" 这类名字里带 floor 的**防护耗材**会被误锁成结构件，
    之后它们照施工纪律被清场时反倒挨一条"结构件不许拆除"。名字同时命中两类提示时，
    临时态说了算——它描述的是物件用途，比 floor 这个位置词更接近本义。
    """
    return (_matches_cue(name, PERSISTENT_STRUCTURAL_CUES)
            and not _matches_cue(name, TEMPORARY_OBJECT_CUES))


def _text(value: Any) -> str:
    return " ".join(str(value or "").split())


# 规划期才有意义的标注：它们是写给**规划模型**读的，不是提示词正文的一部分。
#   （工序：saw、rake、sweep）  reverse.beats_to_dimensions 挂在清单条目正文后的耦合工序
#   ；本拍认领的卡片工序终产物：X  compile_outline_beat_ladder 给终态去重挂的中文尾巴
#   A → B                        清单条目"动作 → 结果"的记号连接
# 三者都会顺着 milestone_name / after_state 流进最终交付的英文提示词里。
_PACKAGE_ANNOTATION_RE = re.compile(r"\s*（工序：[^）]*）")
_CLAIMED_PRODUCT_RE = re.compile(r"\s*；本拍认领的卡片工序终产物：\s*")


def scrub_planning_annotations(text: str) -> str:
    """最终提示词正文的收口：剥掉规划期标注，换成散文。

    与 scrub_spatial_notation 同一道防线、同一个理由：标注从哪条路进来都拦得住。
    确定性拼装（compile_video_skeleton）只是其中一条；更多的是合成模型**照抄**了
    上下文里那份带标注的 milestone_name，自己把「（工序：…）」写进了英文正文。
    """
    if not text:
        return text
    out = _PACKAGE_ANNOTATION_RE.sub("", text)
    out = _CLAIMED_PRODUCT_RE.sub(", delivering: ", out)
    # 箭头前那个句号一并吃掉：清单条目写的是「动作。 → 结果」，不吃就留下 "…planks., resulting in"。
    out = re.sub(r"[ \t]*\.?[ \t]*→[ \t]*", ", resulting in ", out)
    return re.sub(r"[ \t]{2,}", " ", out)


def _prompt_text(value: Any) -> str:
    """写进**交付出去的提示词**的标量文本：先规整空白，再剥掉规划期标注。

    _text 仍然是内部口径（校验、状态账、去重都靠它逐字比对，不能在那一层改写）；
    只有正文渲染这一层做这道转译。
    """
    out = scrub_planning_annotations(_text(value))
    # 末尾标点也去掉：这些片段要被拼进骨架句里，句号由骨架自己补，留着就是 "deck.." 和 "deck., ending with"。
    return " ".join(out.split()).strip(" ,;.") if out else out


def classify_material_flow(operation: str) -> str:
    op = _text(operation).lower().replace("_", "-")
    if op in REMOVAL:
        return "removal"
    if op in INSTALLATION:
        return "installation"
    if op in WET_WORK:
        return "wet_work"
    if op in COVERING:
        return "covering"
    if op in FURNISHING:
        return "furnishing"
    return "transition" if op in TRANSITIONS else "other"


def default_material_flow(operation: str) -> list[dict[str, str]]:
    family = classify_material_flow(operation)
    flows = {
        "removal": [("site_waste", "decrease"), ("waste_container", "increase"), ("offsite_waste", "increase")],
        "installation": [("offsite_inventory", "decrease"), ("installed_components", "increase")],
        "wet_work": [("wet_material", "decrease"), ("finished_surface", "increase"), ("cured_surface_next_anchor", "increase")],
        "covering": [("uncovered_area", "decrease"), ("finished_area", "increase")],
        "furnishing": [("offsite_objects", "decrease"), ("indoor_objects", "increase")],
    }
    return [{"store": store, "direction": direction} for store, direction in flows.get(family, [])]


def build_scene_states(beat_ladder: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compile a ladder into state[N+1] = state[N] + validated_delta[N]."""
    states: list[dict[str, Any]] = []
    permanent: dict[str, Any] = {}
    known_objects: set[str] = set()
    previous_summary = "initial accepted scene state"
    for position, source in enumerate(beat_ladder or [], 1):
        beat = source if isinstance(source, dict) else {}
        operation = _text(beat.get("operation")).lower()
        before = deepcopy(permanent)
        introduced = [str(x) for x in (beat.get("introduced_objects") or []) if _text(x)]
        removed = [str(x) for x in (beat.get("removed_objects") or []) if _text(x)]
        # 豁免范围必须与 milestone_ladder_violations 的规划环校验完全一致:那道校验
        # 在 operation 属于 TRANSITIONS 或 bridge_stage/hard_cut 为真时整段跳过(它们
        # 有自己的契约,不走"缺可见里程碑字段"这条),给模型的反馈回路只覆盖到这个范围。
        # 这里是最终收口前不会再重排的硬闸——豁免更窄就会拒收一个模型从未被要求补全、
        # 也没有机会补全的字段(尤其是 expand_spatial_transition_beats 展开出的运镜/
        # reframe 拍,它们直接起手就没有这两个键)。空数组与"没声明"的区别才是本函数
        # 存在的意义:后者会让下面的物体生命周期校验失去数据来源。
        requires_declaration = (
            operation not in TRANSITIONS
            and not beat.get("bridge_stage")
            and not beat.get("hard_cut")
        )
        delta = {
            "milestone": _text(beat.get("milestone_name") or beat.get("description")),
            "terminal_state": _text(beat.get("after_state")),
            "completion_extent": _text(beat.get("completion_extent")),
        }
        if operation not in TRANSITIONS and not beat.get("bridge_stage") and not beat.get("hard_cut"):
            permanent[f"beat_{position}"] = deepcopy(delta)
        for name in introduced:
            known_objects.add(name.lower())
        for name in removed:
            known_objects.discard(name.lower())
        states.append({
            "beat": int(beat.get("index") or position),
            "operation_type": operation,
            "changed_cells": list(dict.fromkeys(beat.get("changed_grid_cells") or [])),
            "before": before,
            "before_summary": previous_summary,
            "delta": delta,
            "after": deepcopy(permanent),
            "after_summary": delta["terminal_state"] or delta["milestone"],
            "preserve": [_text(beat.get("preserve_state"))] if _text(beat.get("preserve_state")) else [],
            "introduced_objects": introduced,
            "removed_objects": removed,
            "introduced_declared": (not requires_declaration) or "introduced_objects" in beat,
            "removed_declared": (not requires_declaration) or "removed_objects" in beat,
            "persistent_traces": [_text(x) for x in (beat.get("persistent_traces") or []) if _text(x)],
            "material_flow": deepcopy(beat.get("material_flow") or default_material_flow(operation)),
            "anchor_camera": {
                "family": _text(beat.get("camera_family") or beat.get("space_id") or "primary"),
                "locked": True,
                "world_coordinates_mutable": False,
            },
            "edit_shots": list(beat.get("edit_shots") or []),
            "known_objects_after": sorted(known_objects),
        })
        if operation not in TRANSITIONS and not beat.get("bridge_stage") and not beat.get("hard_cut"):
            previous_summary = delta["terminal_state"] or delta["milestone"]
    return states


def validate_material_flow(state: dict[str, Any]) -> list[str]:
    expected = default_material_flow(state.get("operation_type", ""))
    if not expected:
        return []
    actual = state.get("material_flow") or []
    pairs = {(str(x.get("store")), str(x.get("direction"))) for x in actual if isinstance(x, dict)}
    missing = [x for x in expected if (x["store"], x["direction"]) not in pairs]
    return [f"Beat {state.get('beat')} material flow is missing {x['store']}:{x['direction']}." for x in missing]


def validate_scene_states(states: list[dict[str, Any]],
                          reverse_engineered: bool = False,
                          advisories: list[str] | None = None) -> list[str]:
    """校验场景状态账。

    `reverse_engineered`：这条阶梯是对一条真实成片的**转录**（反推复刻线），不是原创
    施工方案。打开它会关掉两条**施工纪律**类规则：

      · 「进入 furnishing 前临时件必须清场」——原创单里画面还立着脚手架就搬家具进场
        是失误；复刻单里那块防护布是从原片上读下来的观察结果，原片就是那么拍的。
      · 「持久结构件一旦铺设不得拆除」——2026-08-25 起。它和上一条是同一类判断：审的
        是"该不该这么施工"，不是"状态账对不对得上"（先 introduce 后 remove 本身在账上
        完全自洽）。实际打回的那单是一拍拆掉了自己先铺的 OSB 结构底板；无论原片真是
        先铺后起，还是转录时把"被面层盖住、不再可见"写成了 removed_objects，让用户回
        去手改一份 1:1 转录都不是修复——那等于要求他把转录改得不像原片。这与清场那条
        当年 6/6 全倒的事故是同一个坑。

    其余各条（时序、before 承接、同一物体重复移除、material_flow）对两条线一视同仁：
    它们查的是状态账自身对不对得上，与题材来源无关。

    `advisories`：传入一个列表，被上面两条豁免放过的判据会原样写进去。豁免不等于这些
    观察没有价值——它们照旧落进 compose 诊断（config['_scene_state_advisories']），只是
    不再拦下整单。
    """
    errors: list[str] = []
    advisories = advisories if advisories is not None else []
    previous_after: dict[str, Any] = {}
    known: set[str] = set()
    # 2026-08-06 修复：系统提示词（__init__.py 的 object-lifecycle 字段说明与
    # OBJECT LIFECYCLE RULE）明确许诺 removed_objects 可以是"原始现场/trauma 状态"
    # 自带、从未被任何一拍 introduced_objects 声明过的物体——这类物体在翻新题材里
    # 几乎每单都有（早期清理拍扫掉的残垣碎石、锈蚀五金）。这里原先无条件要求先声明
    # 引入才能移除，跟提示词的承诺互相矛盾：模型照契约写、校验按另一套口径拒收，
    # 实测自本闸上线以来 6/6 次真实合成全部倒在这条报错上。真正该拦的缺陷不是
    # "移除一个没声明过的物体"（那多半就是合法的原始现场物），而是"同一个物体被
    # 移除了两次、中间没有重新引入"——那才是状态账对不上的信号。
    ever_removed: set[str] = set()
    # 持久态锁定集：一旦某个物体名匹配 PERSISTENT_STRUCTURAL_CUES 并被 introduced，
    # 就永久记在这里——后续任何一拍都不许再把它写进 removed_objects（地板/拱门这类
    # 结构件一旦铺设，全程继承，不存在"先装后拆"的合法叙事）。
    persistent_locked: set[str] = set()
    for expected, state in enumerate(states or [], 1):
        idx = state.get("beat") or expected
        if idx != expected:
            errors.append(f"Beat {idx} is out of sequence; expected {expected}.")
        if state.get("before") != previous_after:
            errors.append(f"Beat {idx} before state does not equal the preceding validated after state.")
        if not state.get("introduced_declared", True):
            errors.append(
                f"Beat {idx} does not declare introduced_objects (required even if empty; "
                f"use [] when this beat introduces no new object).")
        if not state.get("removed_declared", True):
            errors.append(
                f"Beat {idx} does not declare removed_objects (required even if empty; "
                f"use [] when this beat removes nothing).")
        for name in state.get("removed_objects") or []:
            key = name.lower()
            if key in ever_removed and key not in known:
                errors.append(
                    f"Beat {idx} removes object '{name}' again without a fresh introduction "
                    f"since it was last removed.")
            if key in persistent_locked:
                (advisories if reverse_engineered else errors).append(
                    f"Beat {idx} removes '{name}', a persistent structural element that was "
                    f"already laid; persistent elements must be inherited through every "
                    f"subsequent beat and are never removed.")
            ever_removed.add(key)
        for name in state.get("introduced_objects") or []:
            known.add(name.lower())
            if _is_persistent_structural(name):
                persistent_locked.add(name.lower())
        for name in state.get("removed_objects") or []:
            known.discard(name.lower())
        # 临时态清场：进入 furnishing 阶段时，任何仍"存活"（已 introduced 未
        # removed）且匹配 TEMPORARY_OBJECT_CUES 的施工工具/设备都必须已经清场——
        # furnishing 拍写的是搬入家具，不该还有脚手架、工具箱、临时支撑立在画面里。
        if classify_material_flow(state.get("operation_type", "")) == "furnishing":
            lingering = sorted(n for n in known if _matches_cue(n, TEMPORARY_OBJECT_CUES))
            if lingering:
                (advisories if reverse_engineered else errors).append(
                    f"Beat {idx} enters the furnishing phase but temporary construction "
                    f"objects are still present and not yet removed: {lingering}.")
        errors.extend(validate_material_flow(state))
        previous_after = deepcopy(state.get("after") or {})
    return errors


def _delta_prose(delta: Any) -> str:
    """状态账里的 delta（dict）→ 交给渲染模型的一句话。

    这里必须逐字段拼：delta 是 build_scene_states 造的三键字典，直接丢进 f-string 会
    走 str(dict)，把花括号、引号和 milestone/terminal_state/completion_extent 这些
    **内部键名**原样写进交付出去的提示词（2026-08-13 从复刻单的 prompt_block 里捞出
    28 处）。键名是我们排状态账用的，不是给渲染模型读的英文。
    """
    if not isinstance(delta, dict):
        return _prompt_text(delta)
    milestone = _prompt_text(delta.get("milestone"))
    terminal = _prompt_text(delta.get("terminal_state"))
    extent = _prompt_text(delta.get("completion_extent"))
    parts = [x for x in (milestone,
                         f"ending with {terminal}" if terminal else "",
                         f"completed across {extent}" if extent else "") if x]
    return ", ".join(parts)


def _accumulated_prose(accumulated: Any) -> str:
    """before/after 累积账（{beat_N: delta} 映射）→ 逐拍已完成产物的一串散文。"""
    if not isinstance(accumulated, dict):
        return _prompt_text(accumulated)
    return "; ".join(x for x in (_delta_prose(v) for v in accumulated.values()) if x)


def _flow_prose(flows: Any) -> str:
    """material_flow（[{store, direction}, …]）→ 'site waste decreases, offsite waste increases'。

    同 _delta_prose：原先走 _text 会把整个列表的 Python repr 写进提示词。
    """
    if not isinstance(flows, (list, tuple)):
        return _prompt_text(flows)
    phrases = []
    for item in flows:
        if not isinstance(item, dict):
            phrase = _prompt_text(item)
        else:
            store = _prompt_text(item.get("store")).replace("_", " ")
            verb = {"increase": "increases", "decrease": "decreases"}.get(
                _prompt_text(item.get("direction")), "changes")
            phrase = f"{store} {verb}".strip() if store else ""
        if phrase:
            phrases.append(phrase)
    return ", ".join(phrases)


def compile_image_skeleton(camera_dna: str, landmarks: list[Any], state: dict[str, Any], lighting: str) -> str:
    return " ".join(filter(None, [
        f"Camera DNA: {_prompt_text(camera_dna)}.",
        f"Locked landmarks: {', '.join(_prompt_text(x) for x in landmarks if _prompt_text(x))}.",
        f"Inherited state: {_accumulated_prose(state.get('before'))}.",
        f"Only delta: {_delta_prose(state.get('delta'))}.",
        f"Preserve: {', '.join(_prompt_text(x) for x in (state.get('preserve') or []) if _prompt_text(x))}.",
        f"Lighting: {_prompt_text(lighting)}.",
    ]))


def compile_video_skeleton(state: dict[str, Any]) -> str:
    return " ".join([
        f"First-frame state: {_prompt_text(state.get('before_summary'))}.",
        "The tool makes its first visible contact before any state changes.",
        f"Repeated physical actions execute only this delta: {_delta_prose(state.get('delta'))}.",
        f"Material path: {_flow_prose(state.get('material_flow'))}.",
        f"Last-frame state: {_prompt_text(state.get('after_summary'))}.",
    ])
