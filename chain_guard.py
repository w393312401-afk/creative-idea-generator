"""帧序列链上守卫（Chain Guard）：

在生成循环中每帧落地（seq >= 2）后实时执行逐拍一致性审查与影响分级。
- 判：check_beat_consistency
- 复核：_verify_review_violation
- 分级：classify_chain_impact（纯文本分类 chain vs cosmetic）
- 结构级（chain）问题在 halt 档下停链等人，避免下游整链报废重渲。
"""
import json
import os
import sys
import time

from server_common import (
    frame_content_hash, read_manifest, write_manifest, manifest_lock, log,
)
from prompt_pipeline import (
    check_beat_consistency,
    _verify_review_violation,
    _parse_prompt_slots,
    _outline_items_for_beat,
    _multimodal_chat,
    _strip_code_fences,
)
from pipeline_orchestrator import _outline_items_for_review


_CHAIN_CLASSIFIER_SYSTEM_PROMPT = (
    "You are a visual consistency triage classifier for an AI image sequence generation pipeline. "
    "In this pipeline, each frame is generated via image-to-image (i2i) referencing the previous frame. "
    "Classify each given consistency violation into one of two severities based on whether "
    "it will propagate down the i2i chain:\n\n"
    "- \"chain\": Structural, envelope, perspective, spatial topology, carrier identity, camera family, "
    "wall/floor/roof layout breaches, or irreversible deformation that will infect downstream frames. "
    "ALSO chain, regardless of which element it happens to involve: any construction-ORDER or CAUSALITY "
    "violation — something appearing, switched on, finished, removed, or regressed before the beat that "
    "makes it possible (lights lit before any wiring/power beat, a fixture present before its install beat, "
    "a finished surface reverting to raw, a sealed element reopening). These persist into every later frame "
    "and make the beat that was supposed to produce them nonsensical, so they propagate even when the "
    "element itself sounds minor.\n"
    "- \"cosmetic\": ONLY differences in how the SAME physical state looks — lighting ratio or exposure, "
    "minor colour-temperature shift, subtle material grain drift, minor prop clutter, trivial framing or "
    "composition detail. If the two frames disagree about WHAT HAS BEEN BUILT, INSTALLED, POWERED, OR "
    "REMOVED rather than about how it looks, it is \"chain\", not \"cosmetic\".\n\n"
    "Respond STRICTLY with a JSON array of strings corresponding 1:1 to the input items, "
    "where each string is either \"chain\" or \"cosmetic\". Do not include any other text."
)


def classify_chain_impact(config, texts, timeout=30, on_error='chain'):
    """对一组已复核的违规做纯文本影响分级（chain vs cosmetic）。

    分级器异常/解析失败时 fail-safe 兜底为 'chain'（停链只赔一次重渲，放行要赔整条下游）。

    on_error：兜底取值。停链判定必须用默认的 'chain'。传 None 则失败时返回空列表——
    给**只拿分级当展示**的调用方（手动整套审查那条路，见
    pipeline_orchestrator._sequence_consistency_review）：那里分级不决定任何动作，
    一次调用失败就把整单标成"会传染下游"是在编造判定，不如如实标成"未分级"。
    """
    if not texts:
        return []
    user_text = (
        "Classify the following violations:\n"
        + json.dumps(list(texts), ensure_ascii=False, indent=2)
    )
    fallback = ([on_error] * len(texts)) if on_error else []
    try:
        response = _multimodal_chat(
            config, _CHAIN_CLASSIFIER_SYSTEM_PROMPT, user_text,
            [], max_tokens=300, timeout=timeout
        )
        data = json.loads(_strip_code_fences(response))
        if not isinstance(data, list) or len(data) != len(texts):
            return fallback
        out = []
        for item in data:
            val = str(item).strip().lower()
            out.append('cosmetic' if val == 'cosmetic' else 'chain')
        return out
    except Exception as e:
        if sys.stdout:
            print(f"[CHAIN GUARD][CLASSIFY] 分级器调用失败，fail-safe 兜底为 chain: {e}")
        return fallback


def guard_beat(config, title, prompt_block, beat, project_dir, on_progress=None,
               allow_halt=True):
    """对第 beat 拍（IMG beat → IMG beat+1）执行链上守卫审查与落盘。

    allow_halt=False：照常审查落盘，但 halt 恒为 False。定向重渲（单帧重试 /
    fix_frame_issue 的连带重渲）专用——那种场合下游帧本来就要被这一趟重新盖掉，
    中途停链只会留下一条修了一半的链：上游换了新图、下游还挂着旧血统，正是
    cascade_downstream 要消灭的 stale_lineage。守卫在**向前建链**时才有停的意义。

    返回 dict:
      {
        'verdict': 'pass' | 'flagged' | 'unreviewed',
        'issues': [...],
        'halt': bool,
        'inline_record': {...}
      }
    """
    prev_seq = beat
    target_seq = beat + 1
    frames_dir = os.path.join(project_dir, 'frames')
    prev_path = os.path.join(frames_dir, f'img_{prev_seq:03d}.webp')
    target_path = os.path.join(frames_dir, f'img_{target_seq:03d}.webp')

    if not os.path.exists(prev_path) or not os.path.exists(target_path):
        return {'verdict': 'unreviewed', 'issues': [], 'halt': False}

    images, _ = _parse_prompt_slots(prompt_block)
    total_beats = max(1, len(images) - 1)

    outline_items = _outline_items_for_beat(_outline_items_for_review(project_dir), beat)

    # 卡片工序的画面交付判定必须**接住**：收尾那趟审查现在会跳过守卫过的拍
    # （_valid_inline_beats → local_beats），逐拍层是这些判定唯一的产地。不传
    # outline_out 的话 check_beat_consistency 会把它们算完就扔，交付总账的
    # frame_verdict 从此永远空着——静默丢字段，不报错。
    outline_out = {} if outline_items else None

    # 1. 判：逐拍一致性审查
    raw_issues = check_beat_consistency(
        config, prompt_block, beat, total_beats,
        prev_path, target_path,
        outline_items=outline_items,
        outline_out=outline_out,
    )

    formatted_issues = []
    if raw_issues is None:
        verdict = 'unreviewed'
        halt = False
    elif not raw_issues:
        verdict = 'pass'
        halt = False
    else:
        # 2. 复核：逐条复核
        survived = []
        for text in raw_issues:
            v = _verify_review_violation(config, text, [prev_path, target_path])
            if v is not False:
                survived.append({'text': text, 'verified': v})
        
        if not survived:
            verdict = 'pass'
            halt = False
        else:
            # 3. 分级：纯文本分类
            severities = classify_chain_impact(config, [item['text'] for item in survived])
            for item, sev in zip(survived, severities):
                formatted_issues.append({
                    'beat': beat,
                    'layer': 'local',
                    'text': item['text'],
                    'frames': [beat, beat + 1],
                    'verified': item['verified'],
                    'severity': sev,
                })
            verdict = 'flagged'
            halt = allow_halt and any(i.get('severity') == 'chain' for i in formatted_issues)

    prev_hash = frame_content_hash(prev_path)
    target_hash = frame_content_hash(target_path)
    frames_sha256 = {
        str(beat): prev_hash,
        str(beat + 1): target_hash,
    }

    inline_record = {
        'beat': beat,
        'verdict': verdict,
        'issues': formatted_issues,
        'frames_sha256': frames_sha256,
        'at': time.strftime('%Y-%m-%dT%H:%M:%S'),
    }
    if outline_out:
        inline_record['outline_frame_verdicts'] = dict(outline_out)

    # 写 manifest。走 manifest_lock + write_manifest，与本仓库其余写入端一致：
    # 生成循环、stale 状态收尾、审查落盘都在同一份 manifest 上做读-改-写，
    # 裸 json.dump 会把并发写入端的改动整份盖掉。
    with manifest_lock(project_dir):
        manifest = read_manifest(project_dir) or {}
        frames = manifest.get('frames') or []
        target_frame = next((f for f in frames if f.get('sequence') == target_seq), None)
        if target_frame is not None:
            target_frame['inline_beat_review'] = inline_record
            # 只有真的要停链才写 flag：cosmetic 违规按方案是「记账不停链」，
            # 到收尾那趟由 _inline_result 合并进 failures 再统一盖章。
            if halt:
                chain_issues = [i['text'] for i in formatted_issues if i.get('severity') == 'chain']
                target_frame['quality_gate'] = 'sequence_review_flagged'
                target_frame['vlm_qa_reason'] = '；'.join(chain_issues or [i['text'] for i in formatted_issues])
                target_frame['flag_origin'] = 'chain_guard'
            try:
                write_manifest(project_dir, manifest)
            except Exception as e:
                log('WARN', 'CHAIN_GUARD', f"保存 manifest inline_beat_review 异常: {e}")

    return {
        'verdict': verdict,
        'issues': formatted_issues,
        'halt': halt,
        'inline_record': inline_record,
    }
