"""Deterministic frame-state contracts for restoration prompt sequences.

The language model may describe a beat fluently while still reversing construction
order or packing several unrelated states into one frame.  This module converts the
beat ladder into a small, inspectable state transition contract and rejects those
errors before any image-generation credits are spent.
"""

from __future__ import annotations

import re
from typing import Any

from .scene_state import PERSISTENT_STRUCTURAL_CUES, scrub_planning_annotations


TRANSITION_OPERATIONS = {"threshold", "reward", "reframe", "reveal"}

# cast_action 是**一拍**的身体动线，写的是「先 A、然后 B」（'stands atop ladder grinding
# overhead beams, then crouches grinding floor grid welds'）。VIDEO 要的就是这条动线，
# 但 IMAGE 是一张静帧，渲不出「先 A 然后 B」——模型必须二选一，而上一帧正作为锚点参考图
# 摆在它面前，最省力的一选就是「照抄上一帧那个人」。2026-08-25 实测（run_replica_06edb…
# 河畔观景室）交付出来的正是这个：IMG 011-015 背景从毛石换到成品木地板，人物贴图五帧
# 像素级重合。所以 IMAGE 侧只取动线**落点**那一段——与 _beat_block_text 的
# "this beat's IMAGE shows them settled in that pose" 是同一个口径。
_CAST_SEQUENCE_RE = re.compile(r',?\s+(?:and\s+)?then\s+', re.IGNORECASE)


def _settled_cast_pose(cast: str) -> str:
    """The pose a beat's cast LANDS in — the last leg of its 'A, then B' motion line."""
    legs = [leg.strip(" ,;") for leg in _CAST_SEQUENCE_RE.split(_text(cast)) if leg.strip(" ,;")]
    return legs[-1] if legs else _text(cast)


# 「他们不碰活儿」是**微缩线专有**的规则（composers/miniature.py 第 2 节："They watch;
# they never build"：施工全部由画外的巨人手完成，人偶只是住户）。复刻线的工人本人就是
# 施工者，对着 'crouches grinding floor grid welds' 再写一句 never touching the work，
# 是在同一句话里给模型两条互斥指令——而它手上正拿着上一帧当参考图，化解矛盾最省力的
# 办法就是两条都不执行、把人原样搬过来。
#
# compile_delta_image_prompt 只收得到 beat，拿不到 profile（apply_proactive_fixes 的
# family 是 interior/exterior，不是线别），所以判据取自 cast 文本自身：它描述了对活儿的
# 接触，这句就是假的，不写；写的是旁观动线才写。两条线的实际取值分得开——微缩线是
# 'get up off the stone and turn to face the new wall'（无工具无施工动词），复刻线是
# 'crouches grinding floor grid welds'。拿不准时按「不写」走：漏掉一句真约束，比写进
# 一句假约束便宜。
_CAST_TOUCHES_WORK_RE = re.compile(
    r'\b(?:grind\w*|weld\w*|shovel\w*|dig\w*|rake\w*|spray\w*|paint\w*|prime\w*|saw\w*|cut\w*|'
    r'nail\w*|screw\w*|drill\w*|hammer\w*|trowel\w*|plaster\w*|tile\w*|lay\w*|laid|install\w*|'
    r'fit\w*|fasten\w*|mount\w*|attach\w*|seal\w*|glue\w*|sand\w*|smooth\w*|screed\w*|float\w*|'
    r'unroll\w*|roll\w*|stuff\w*|press\w*|push\w*|lift\w*|carr\w*|haul\w*|place\w*|set\w*|'
    r'align\w*|click\w*|trim\w*|clamp\w*|solder\w*|wire\w*|pour\w*|mix\w*|brush\w*|scrape\w*|'
    r'sweep\w*|blow\w*|clean\w*|clear\w*|strip\w*|panel\w*|board\w*|batt\w*|plank\w*)\b',
    re.IGNORECASE)


def _cast_is_bystander(cast: str) -> bool:
    """True when the cast text describes watching/moving, not working the build."""
    return not _CAST_TOUCHES_WORK_RE.search(_text(cast))

# "这个面又变回生料/裸露了" 的措辞。只认字面词是不够的：一句完工描述里出现这些词，
# 十有八九是被否定掉的（"…with no bare subfloor left exposed"、"…with no bare patches
# remaining"），那恰恰是**完工**的说法，不是回退。2026-08-06 实测：兜底梯子的
# flooring / painting 两档 after_state 都带 "no bare …"，跟在任何一条含
# complete/finished 的前序状态后面就被这道闸判成"显式回退"，整单直接 QUALITY_GATE_FAILED。
# 同样，"raw OSB panels", "raw timber", "raw brick masonry" 等是正向施工安装的材料
# 材质描述（未饰面基材/原木/欧松板），不是空间倒退。
_REGRESSION_WORDS = r"(?:absent|missing|removed|bare|raw|open again)"
_NEGATED_REGRESSION_RE = re.compile(
    rf"\b(?:no|not|never|without|zero|free of)\b(?:\W+\w+){{0,3}}?\W+{_REGRESSION_WORDS}\b",
    re.IGNORECASE,
)
_MATERIAL_RAW_RE = re.compile(
    r"\b(?:raw|bare)\s+(?:osb|plywood|timber|wood|brick|masonry|stone|concrete|lumber|pine|board|boards|panel|panels|aggregate|sheathing|flake|flakes|flak|plank|planks|slat|slats|log|logs|rock|rocks|unrendered|substrate|finish)\b",
    re.IGNORECASE,
)
_REGRESSION_RE = re.compile(rf"\b{_REGRESSION_WORDS}\b", re.IGNORECASE)


def _asserts_regression(after: str) -> bool:
    """after_state 是否**正面主张**某个面回到了生料/缺失状态（否定式说法及材料名不算）。"""
    cleaned = _NEGATED_REGRESSION_RE.sub(" ", after or "")
    cleaned = _MATERIAL_RAW_RE.sub(" ", cleaned)
    return bool(_REGRESSION_RE.search(cleaned))

# Coarse construction phases.  These are deliberately conservative: equal ranks are
# allowed, forward movement is allowed, and a lower rank is only allowed for an
# explicit clearing/removal operation or a registered new space.
_PHASE_RANK = {
    "clearing": 0,
    "demolition": 0,
    "repair": 1,
    "placement": 1,
    "rough-in": 2,
    "rough_in": 2,
    "wiring": 2,
    "plumbing": 2,
    "framing": 3,
    "insulation": 4,
    "drywall": 5,
    "paneling": 5,
    "priming": 6,
    "painting": 6,
    "flooring": 6,
    "lighting": 7,
    "furnishing": 8,
}


def _text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _space_id(beat: dict[str, Any]) -> str:
    # `space` 排在最前：复刻线的空间标签走 reverse.normalize_beat_spaces 落在这个键上，
    # 而 _BEAT_KEY_ALIASES 又会把 space_id/room/location 统统搬进 `space`。只认 space_id
    # 的话，整条复刻梯子在这里会塌成一个 "primary" 桶——按空间分账的判据全部失效。
    return _text(
        beat.get("space") or beat.get("space_id") or beat.get("space_family") or "primary"
    ).lower()


def _phase(beat: dict[str, Any]) -> int | None:
    tokens = [_text(beat.get("operation")).lower()]
    tokens.extend(_text(x).lower() for x in (beat.get("package_operations") or []))
    ranks = [_PHASE_RANK[t] for t in tokens if t in _PHASE_RANK]
    return min(ranks) if ranks else None


def build_frame_state_contract(beat_ladder: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return a serializable state transition record for every delivered beat."""
    contracts: list[dict[str, Any]] = []
    for position, beat in enumerate(beat_ladder or [], start=1):
        beat = beat if isinstance(beat, dict) else {}
        op = _text(beat.get("operation")).lower()
        package = [_text(x).lower() for x in (beat.get("package_operations") or []) if _text(x)]
        contracts.append({
            "beat": int(beat.get("index") or position),
            "space": _space_id(beat),
            "operation": op,
            "before": _text(beat.get("before_state")),
            "delta": _text(beat.get("milestone_name") or beat.get("description")),
            "after": _text(beat.get("after_state")),
            "preserve": _text(beat.get("preserve_state")),
            "forbidden": _text(beat.get("forbidden_state") or "all later-stage results and any regression"),
            "changed_grid_cells": list(dict.fromkeys(beat.get("changed_grid_cells") or [])),
            "package_operations": package,
            "persistent_traces": [_text(x) for x in (beat.get("persistent_traces") or []) if _text(x)],
            "phase": _phase(beat),
            "transition": bool(
                op in TRANSITION_OPERATIONS
                or beat.get("stage") in ("transition", "threshold", "reveal")
                or beat.get("bridge_stage")
                or beat.get("hard_cut")
                or "reveal" in op
                or "reward" in op
            ),
        })
    return contracts


# ── 按空间的状态账 ───────────────────────────────────────────────────────────
#
# build_frame_state_contract 回答不了过门揭示真正要问的那个问题：镜头正要推进去的这个
# 空间，**之前进来过没有**？进来过的话，上次离开时它长什么样？
#
# 整条流水线原本不需要问——规划期指令写死了「a crossing moves the CAMERA, never the
# construction state: everything completed in an earlier space ... is never revisited」
# （__init__.py:9080），靠「永不回头」来保证不回退。2026-08-14 改成按原片逐拍 space
# 序列标过门之后，`外 → 内 → 外 → 内` 成了常态，这条保证就塌了：回到旧空间的那一拍
# 没有任何数据告诉渲染层「这里已经铺完地板了」，于是过门揭示照着「没人进来过」的口径
# 把它重新画成废墟（2026-08-15 用户复盘：外景已是成品温室，一进门又是杂草与裸土）。
#
# 这份账只服务提示词，不做硬闸。理由见 _PHASE_RANK 的排序本身就不可靠（framing=3 排在
# rough-in=2 前面，而真实工序是先框架后铺管），拿它判死会误伤正常梯子。


def _completed_phrases(after: str) -> str:
    """一条 after_state 里能直接写进提示词的散文。

    必须过 scrub_planning_annotations：after_state 会带「（工序：…）」这类规划期标注，
    它们顺着这条路会一路流进交付出去的英文提示词正文（见 scene_state 同名函数的注释）。
    """
    return scrub_planning_annotations(_text(after)).strip(" ,;.")


def build_space_state_ledger(beat_ladder: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """空间 → 这个空间被拍到过的每一拍 [{'index','after','phase','operation'}, …]。

    只收录承载施工增量的拍：过门/reward/桥接拍本身不改变任何建造状态，把它们记进来
    会让「上次离开时的样子」变成一句运镜描述。
    """
    ledger: dict[str, list[dict[str, Any]]] = {}
    for position, beat in enumerate(beat_ladder or [], start=1):
        if not isinstance(beat, dict):
            continue
        op = _text(beat.get("operation")).lower()
        if (op in TRANSITION_OPERATIONS
                or beat.get("stage") in ("transition", "threshold", "reveal")
                or beat.get("bridge_stage")
                or beat.get("hard_cut")
                or "reveal" in op
                or "reward" in op):
            continue
        after = _completed_phrases(beat.get("after_state"))
        if not after:
            continue
        ledger.setdefault(_space_id(beat), []).append({
            "index": int(beat.get("index") or position),
            "after": after,
            "phase": _phase(beat),
            "operation": op,
        })
    return ledger


def _carried_structural(ledger: dict[str, list[dict[str, Any]]], space: str, limit: int = 3) -> str:
    """在**别的**空间完成、但从这个空间也看得见的结构件终态。

    屋顶/天花板/地面是一个物件被两个空间同时看见：外景拍申报「屋顶重建完成」，室内拍的
    天花板状态和它此前没有任何数据关联，于是进门后天花板又回到原始态。按空间分账解决
    不了这一类——它恰恰是跨空间的，所以单独捞一遍结构件关键词。
    """
    picked: list[str] = []
    for other, visits in (ledger or {}).items():
        if other == space:
            continue
        for visit in visits:
            after = visit.get("after") or ""
            # 正面主张「又变回生料/裸露」的终态一律不算「已完成、必须照着画」：清理拍的
            # "bare earth floor exposed" 命中 floor 关键词，不滤掉的话会被当成结构件成果
            # 递给首次进外景的那一拍，等于命令模型把裸土画进一个还没动工的空间。
            if not _matches_structural(after) or _asserts_regression(after):
                continue
            if after not in picked:
                picked.append(after)
    return "; ".join(picked[-limit:])


def _matches_structural(text: str) -> bool:
    lowered = (text or "").lower()
    return any(cue in lowered for cue in PERSISTENT_STRUCTURAL_CUES)


def space_entry_context(
    beat_ladder: list[dict[str, Any]],
    beat_index: int,
    *,
    limit: int = 3,
) -> dict[str, Any]:
    """镜头在第 ``beat_index`` 拍进入的那个空间的前情，供过门揭示提示词使用。

    返回 ``first_entry``（这个空间此前没有任何施工拍）、``inherited_state``（上次离开时
    的终态，最多 ``limit`` 条）、``carried_structural``（别处完成但这里看得见的结构件）、
    ``last_seen_index``（上一次拍到这个空间的拍号，渲染层据此挂上那张真实帧）。

    找不到梯子/找不到这一拍时返回 ``first_entry=True`` 的空前情——等价于改动前的行为，
    老 manifest（没有 space 字段的）因此一切照旧。
    """
    beats = [b for b in (beat_ladder or []) if isinstance(b, dict)]
    unknown = {"space": "", "first_entry": True, "inherited_state": "",
               "carried_structural": "", "last_seen_index": None}
    if not beats:
        return unknown

    # 一拍都没有显式空间标签 = 这份梯子根本没有空间信息（2026-08-14 之前的 manifest，
    # 或投影漏了 space 键）。此时 _space_id 会把每一拍都兜底成 "primary"，整条序列塌成
    # 一个桶——于是**真正的新空间**也会被判成复入场，反过来命令模型把一个还没动工的
    # 房间画成完工。宁可整条退回改动前的首入场行为，也不能拿兜底值当判据。
    if not any(_text(b.get("space") or b.get("space_id") or b.get("space_family")) for b in beats):
        return unknown

    current = None
    for position, beat in enumerate(beats, start=1):
        if int(beat.get("index") or position) == int(beat_index):
            current = beat
            break
    if current is None:
        return {"space": "", "first_entry": True, "inherited_state": "",
                "carried_structural": "", "last_seen_index": None}

    space = _space_id(current)
    ledger = build_space_state_ledger(beats)
    prior = [v for v in ledger.get(space, []) if v["index"] < int(beat_index)]

    # 最近一条终态永远保留；更早的那些若正面主张「裸露/缺失」，说明它已经被后续工序
    # 取代了（先清出裸土、再铺地板），把它一并回放等于同时命令模型画裸土和画地板。
    seen: list[str] = []
    for offset, visit in enumerate(prior[-limit:]):
        is_latest = offset == len(prior[-limit:]) - 1
        if not is_latest and _asserts_regression(visit["after"]):
            continue
        if visit["after"] not in seen:
            seen.append(visit["after"])

    return {
        "space": space,
        "first_entry": not prior,
        "inherited_state": "; ".join(seen),
        "carried_structural": _carried_structural(ledger, space, limit=limit),
        "last_seen_index": prior[-1]["index"] if prior else None,
    }


def validate_frame_state_contract(
    contracts: list[dict[str, Any]],
    *,
    min_package_operations: int = 2,
    max_package_operations: int = 3,
) -> list[str]:
    """Validate one-delta-per-frame and monotonic construction state.

    The check is intentionally text-light.  It validates declared structured fields,
    not prose similarity, so it is deterministic and safe to run as a hard gate.
    """
    errors: list[str] = []
    seen_after: dict[str, str] = {}
    seen_transitions: dict[tuple[str, str], int] = {}

    for expected, item in enumerate(contracts or [], start=1):
        idx = item.get("beat") or expected
        if idx != expected:
            errors.append(f"Beat {idx} is out of sequence; expected beat {expected}.")
        if item.get("transition"):
            # A transition carries no construction state, so the milestone checks below do not
            # apply — but it must still be a distinct step.  Expanding one crossing marker into
            # several stages used to clone the marker's state fields verbatim, and skipping the
            # whole loop body meant the identical stages passed clean and shipped as several
            # consecutive clips of the same crossing.
            signature = (_text(item.get("delta")).lower(), _text(item.get("after")).lower())
            if any(signature):
                first = seen_transitions.get(signature)
                if first is not None:
                    errors.append(
                        f"Beat {idx} is a transition that repeats beat {first} verbatim; every "
                        f"crossing stage needs its own camera-position state."
                    )
                else:
                    seen_transitions[signature] = idx
            continue

        missing = [name for name in ("before", "delta", "after", "preserve") if not item.get(name)]
        if missing:
            errors.append(f"Beat {idx} frame-state contract is missing: {', '.join(missing)}.")

        package = item.get("package_operations") or []
        # 口径与 milestone_ladder_violations / ladder schema 第 13 条严格一致：
        # 一个普通施工拍申报 2~3 道紧密相关的工序。三处曾各写各的（schema 说 1~3、
        # 里程碑闸判 1~3、这里判 1~2），于是一条 3 工序的收口拍能一路通过规划验收，
        # 再被这道循环外硬闸判死，整单只拿到一句报错。改这里时务必同步那两处。
        if not min_package_operations <= len(package) <= max_package_operations:
            errors.append(
                f"Beat {idx} declares {len(package)} operations; a frame must carry "
                f"{min_package_operations} to {max_package_operations} tightly coupled operations "
                f"that share one terminal product."
            )
        if len(item.get("changed_grid_cells") or []) > 3:
            errors.append(f"Beat {idx} changes more than three composition cells.")
        if len(item.get("persistent_traces") or []) < 2:
            errors.append(f"Beat {idx} declares fewer than two visible persistent traces.")

        space = item.get("space") or "primary"
        op = item.get("operation") or ""
        after = _text(item.get("after")).lower()
        previous_after = seen_after.get(space)
        # Exact or near-exact state rewrites are a strong signal that the planner did not
        # actually advance this frame.  Avoid broad semantic guessing here.
        if previous_after and after == previous_after:
            errors.append(f"Beat {idx} repeats the preceding terminal state instead of advancing it.")
        if after:
            seen_after[space] = after

        # Catch explicit wording of a completed surface becoming raw/absent again.  This
        # complements the operation-rank check without trying to understand all materials.
        if previous_after and _asserts_regression(after):
            if op not in {"clearing", "demolition", "repair"} and any(
                word in previous_after for word in ("complete", "completed", "finished", "sealed", "installed")
            ):
                errors.append(f"Beat {idx} explicitly regresses a previously completed state.")

    return errors


def compile_delta_image_prompt(
    original: str,
    beat: dict[str, Any] | None,
    *,
    max_words: int = 160,
) -> str:
    """Compile an ordinary milestone IMAGE into a concise state-delta prompt.

    Camera and locked-anchor sentences are preserved verbatim.  The remaining prose is
    replaced by the ladder's authoritative state fields, making the current frame's one
    requested change much more salient to the image model.
    """
    if not isinstance(beat, dict):
        return _text(original)
    op = _text(beat.get("operation")).lower()
    if (op in TRANSITION_OPERATIONS
            or beat.get("stage") in ("transition", "threshold", "reveal")
            or beat.get("bridge_stage")
            or beat.get("hard_cut")
            or "reveal" in op
            or "reward" in op):
        return _text(original)
    required = ("milestone_name", "after_state", "completion_extent", "preserve_state")
    if any(not _text(beat.get(field)) for field in required):
        # Standalone repair helpers and legacy callers may provide only an operation.
        # They have no authoritative state delta to compile, so preserve their prompt.
        return _text(original)

    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", _text(original)) if s.strip()]
    camera = sentences[0] if sentences else "Generate one documentary photograph from the locked camera."
    anchor = next((s for s in sentences if s.lower().startswith("locked anchors:")), "")
    parts = [camera]
    if anchor and anchor != camera:
        parts.append(anchor)

    preserve = _text(beat.get("preserve_state"))
    milestone = _text(beat.get("milestone_name"))
    after = _text(beat.get("after_state"))
    extent = _text(beat.get("completion_extent"))
    cast = _settled_cast_pose(beat.get("cast_action"))
    traces = [_text(x) for x in (beat.get("persistent_traces") or []) if _text(x)][:3]

    # 白名单里必须有 cast_action。2026-08-23 实测：这一步把 composer 写的正文整段丢掉、
    # 只按下面这几个字段重拼，人偶的姿态句就是在这里消失的——21 张图里只有第 1 帧
    # （不走这条压缩）写了人偶，其余每一帧只剩锚点句里那个钉死的坐姿。
    #
    # 显示顺序固定；超长时按 drop_order 从后往前整句让位。让位次序是「谁重复得最多谁先走」：
    # completion_extent 与 after_state 讲的基本是同一件事，persistent_traces 是锦上添花，
    # 继承句在锚点句之外再兜一层。机位/锚点/本拍增量/人偶姿态/收尾那句净帧声明永不退——
    # 人偶姿态句正是 2026-08-23 那条「交付出来一动不动」的修复本体，退了等于没改；
    # 净帧那句带着防倒退约束，是 reward 帧倒退老账的兜底。
    blocks = {
        'preserve': f"Inherited state remains unchanged: {preserve}." if preserve else '',
        'delta': f"Only visible construction delta in this frame: {milestone}.",
        # 「同一个人」与「同一个姿势」是两件事，这一句必须两边都写。只写前半句的后果
        # 是确定的：2026-08-25 实测那条河畔片里，工人不是 primary_landmark，于是
        # _canonical_anchor_clause 那句 "free to take a new pose and a new spot this frame"
        # （活物锚点专有）根本没机会出现，整张提示词里关于人的指令只剩「同一身份、同样
        # 穿着、同样大小」——纯粹的保持一致，配上上一帧当参考图，交付出来就是五帧同一张
        # 人物贴图。放开姿态的配重不能只挂在锚点那条路上：画面里有活物就得给。
        'cast': (f"Cast in frame: {cast} — same identity, costume and scale as before, but a "
                 f"visibly different pose and position from the previous frame, never that "
                 f"posture copied over"
                 f"{', and never touching the work' if _cast_is_bystander(cast) else ''}."
                 ) if cast else '',
        'after': f"Completed terminal state: {after}." if after else '',
        'extent': f"Completion extent: {extent}." if extent else '',
        'traces': (f"Visible physical evidence remains: {', '.join(traces)}."
                   if traces else ''),
        # 防倒退与净帧原本是分开的两句（共三十词）。人偶姿态句进白名单后 180 词的硬顶
        # 塞不下，合并成一句省下七个词——两条约束一条不少，只是不再各占一句。
        # 「no active construction」这一条的本意是「别把下一阶段的施工画进来、别让在建
        # 状态糊掉本帧要读的那个完成度」。但 cast 本身就在施工时（复刻线），它跟上面那句
        # Cast in frame 是字面冲突的：一句要求画出这个人在磨焊缝，一句要求画面里没有施工。
        # 模型只能二选一，而参考图就在手边——于是两句都不执行，人照抄上一帧。所以 cast
        # 动手时把这一条收窄到「除上面那一个姿态之外」，本意一字不少，冲突消掉。
        'guard': ("One clean documentary photograph: no unrelated work, no later-stage result, "
                  "no regression of any previously completed feature, "
                  + ("no active construction"
                     if (not cast or _cast_is_bystander(cast))
                     else "no construction activity beyond the single cast pose stated above")
                  + ", no text artifacts."),
    }
    order = ['preserve', 'delta', 'cast', 'after', 'extent', 'traces', 'guard']
    drop_order = ['traces', 'extent', 'after', 'preserve']

    def _assemble():
        return " ".join(parts + [blocks[k] for k in order if blocks[k]])

    # 整句让位，绝不切在句子中间。旧写法按词硬截，交付过 "…in central soil clearing. Show."
    # 这样的残句（2026-08-23 实测帧 2：被切掉的正是 "Show no unrelated work…"）。
    while len(_assemble().split()) > max_words and drop_order:
        blocks[drop_order.pop(0)] = ''
    return _assemble()
