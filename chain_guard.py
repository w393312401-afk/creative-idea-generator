"""帧序列链上守卫（Chain Guard）：

在生成循环中每帧落地（seq >= 2）后实时执行逐拍一致性审查与影响分级。
- 判：check_beat_consistency
- 复核：_verify_review_violation
- 分级：classify_chain_impact（纯文本分类 chain vs cosmetic）
- 结构级（chain）问题在 halt 档下停链等人，避免下游整链报废重渲。

首帧（seq == 1）没有"上一拍"可比，走 guard_anchor 对标原片/封面；三个渲染入口
统一经由 run_anchor_guard 拿它的停链结论（含 autofix 一轮），别再各写各的。
"""
import json
import os
import sys
import time

from server_common import (
    frame_content_hash, read_manifest, write_manifest, manifest_lock, log,
    resolve_cover_reference, GenerationCancelled,
)
from prompt_pipeline import (
    check_beat_consistency,
    check_anchor_consistency,
    _verify_review_violation,
    _parse_prompt_slots,
    _outline_items_for_beat,
    _multimodal_chat,
    _strip_code_fences,
    find_reference_frames_for_project,
    find_reference_frames_with_roles,
)
from pipeline_orchestrator import _outline_items_for_review


# ── 守卫档位语义（唯一真源，与 server_common.GATE_SETTINGS 的 chainGuardMode 对齐）──
# 'off'          不审
# 'report'       审 + 记账，不修不停
# 'halt'         审 + 一检出结构级问题就停链
# 'autofix'      审 + 就地自动修复，修满次数仍不过则停链
# 'autofix_soft' 审 + 就地自动修复，修满次数仍不过**照旧往下渲**，只留 flag 等人收尾
#
# autofix_soft 的由来（2026-08-30）：首帧锚点是全链唯一"修不好就整单停"的关卡，而它
# 判死的理由大多是机位俯角这类靠重写提示词掰不动的维度（见 check_anchor_consistency
# 规则 2）。于是一整单停在 IMG 001，用户一帧可用的序列都拿不到。软档把"停链"换成
# "记账 + 继续"：guard_anchor / guard_beat 该写的 sequence_review_flagged 照写，
# 收尾由前端 summarizeRunQuality 汇总成「N 帧一致性审查未过」，人一次性挑着修。
#
# 这两个判据必须走函数、不许在调用点手写档位字面量：三个渲染入口各抄一遍 halt 条件
# 正是 run_anchor_guard 文档里记的那个成因。
_AUTOFIX_MODES = ('autofix', 'autofix_soft')
_HALT_MODES = ('halt', 'autofix')


def guard_autofix_enabled(guard_mode) -> bool:
    """该档位是否要在检出结构级问题后就地自动修复。"""
    return guard_mode in _AUTOFIX_MODES


def guard_halt_enabled(guard_mode) -> bool:
    """该档位是否允许把"检出结构级问题"升级成停链。"""
    return guard_mode in _HALT_MODES


_CHAIN_CLASSIFIER_SYSTEM_PROMPT = (
    "You are a visual consistency triage classifier for an AI image sequence generation pipeline. "
    "In this pipeline, each frame is generated via image-to-image (i2i) referencing the previous frame. "
    "Classify each given consistency violation into one of two severities based on whether "
    "it will propagate down the i2i chain:\n\n"
    "- \"chain\": Structural, envelope, perspective, spatial topology, carrier identity, camera family, "
    "benchmark camera/spatial DNA drift against the viral reference, "
    "isometric diamond grid skewing, loss of horizontal ground baseline, cavernous hall or narrow tunnel distortion, "
    "frozen static mannequin/living cast dynamic reflex loss, "
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


# 锚点帧（IMAGE 1）专用的分级校准。逐拍守卫比的是"上一帧 → 这一帧"，同一条链上的
# 两张图，机位一动就是真漂移；锚点比的却是"我们重做的第一帧 vs 爆款原片的第一帧"，
# 两者本来就不是同一次拍摄，规则 2 又要求匹配俯仰角/焦段/留白比例，于是 VLM 几乎总能
# 说出一句"俯角偏高"，分级器再照 camera-drift 一律判 chain，首帧必然被判死（实测连修
# 2 次仍是同一条理由）。下面这段把"同一机位族、只是没对齐"降成 cosmetic——锚点是重演
# 不是复拍，下游 i2i 继承的是锚点自己立的机位，略偏但自洽并不会顺着链传下去。
# 构图之外的规则（工序因果、毛坯初始态、空间拓扑/包络尺度）一概不放松。
_ANCHOR_CLASSIFIER_RIDER = (
    "\n\nANCHOR-FRAME CALIBRATION — this batch comes from auditing the opening anchor frame "
    "against the viral benchmark's first frame, NOT from a beat-to-beat comparison inside one chain. "
    "The anchor is a re-creation, not a re-shoot, and downstream i2i frames inherit whatever viewpoint "
    "the anchor itself establishes.\n"
    "A camera/framing difference is \"chain\" here ONLY if the shot reads as a DIFFERENT CAMERA FAMILY "
    "or a different space: aerial/top-down vs eye-level, interior vs exterior, a wide establishing shot "
    "collapsed into a close-up, or an envelope-scale breach (cavernous hall expansion, narrow tunnel "
    "distortion, lost horizontal ground baseline, skewed isometric grid).\n"
    "An imperfect match of pitch angle, camera height, focal-length feel, framing offset or "
    "negative-space ratio — same shot type, just not aligned with the benchmark — is \"cosmetic\".\n"
    "Everything else above is unchanged: construction-order/causality violations, premature finished "
    "elements in the raw initial state, and spatial-topology breaches remain \"chain\"."
)


def classify_chain_impact(config, texts, timeout=30, on_error='chain', layer=None):
    """对一组已复核的违规做纯文本影响分级（chain vs cosmetic）。

    分级器异常/解析失败时 fail-safe 兜底为 'chain'（停链只赔一次重渲，放行要赔整条下游）。

    layer='anchor'：追加 _ANCHOR_CLASSIFIER_RIDER，按锚点口径给机位/构图类差异松绑。
    追加在系统提示词**尾部**，不动可缓存的前缀。

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
    system_prompt = _CHAIN_CLASSIFIER_SYSTEM_PROMPT
    if layer == 'anchor':
        system_prompt += _ANCHOR_CLASSIFIER_RIDER
    try:
        response = _multimodal_chat(
            config, system_prompt, user_text,
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
               allow_halt=True, ref_path=None):
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

    # 爆款原片对标关键帧（Beat b 审查的是从 Frame b -> Frame b+1 的交付，对标 Frame b+1 终点）
    # 只认 beat+1 这一格。挂帧那边「挂不出合规帧就不挂」是**声明**，不是缺失：
    # 原来的 `or ref_dict.get(beat)` 会在这一格空着时向前借上一拍的帧，正好在过门
    # 前后借到跨空间层的图，把假违规又请回来（2026-08-31）。
    if ref_path is None:
        try:
            ref_dict, role_dict, _ = find_reference_frames_with_roles(project_dir, total_beats)
            ref_path = ref_dict.get(beat + 1) or ref_dict.get(str(beat + 1))
            ref_role = role_dict.get(beat + 1) or role_dict.get(str(beat + 1)) or 'benchmark'
        except Exception:
            ref_path = None
            ref_role = 'benchmark'
    else:
        ref_role = 'benchmark'

    extra_kw = {}
    if ref_path:
        extra_kw['ref_frame_path'] = ref_path
        extra_kw['ref_frame_role'] = ref_role

    # 1. 判：逐拍一致性审查
    raw_issues = check_beat_consistency(
        config, prompt_block, beat, total_beats,
        prev_path, target_path,
        outline_items=outline_items,
        outline_out=outline_out,
        **extra_kw
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
                # 结构化问题清单也要落盘。定向修复读的正是它（pipeline_orchestrator
                # .fix_frame_issue 的 recorded_issues），缺了就退化成"一条本地问题、
                # frames=[K]"，于是修完复核时拿**单张 K** 去验一条"K-1 与 K 不一致"
                # 的判定——单张图根本证否不了，复核必然落进"仍存在"。
                # 这里每条都带着 beat / layer / frames=[K-1, K] / severity。
                target_frame['review_issues'] = [dict(i) for i in formatted_issues]
            elif verdict == 'pass' and target_frame.get('flag_origin') in ('chain_guard', 'fix_reverify'):
                # 复审通过 → 收回自己这一路盖出来的 flag。autofix 的次序是
                # 「fix_frame_issue（末尾的 _reverify 可能盖 flag）→ 本函数再判一次」，
                # 本函数此前只在 halt 时写 gate、判过了什么都不写，于是那枚 flag 留在
                # 原地：循环报「✅ 复审通过，继续往下生成」，manifest 上这一帧却永远
                # 是 flagged，视频配对门禁一直拦着。只认自己人盖的记号，人工标记
                # （manual_flagged）与整套审查的结论一概不碰。
                target_frame.pop('flag_origin', None)
                target_frame.pop('review_issues', None)
                if target_frame.get('quality_gate') == 'sequence_review_flagged':
                    target_frame['quality_gate'] = 'pending_manual_review'
                    target_frame['vlm_qa_reason'] = None
            try:
                write_manifest(project_dir, manifest)
            except Exception as e:
                log('WARN', 'CHAIN_GUARD', f"保存 manifest inline_beat_review 异常: {e}")

    if on_progress:
        msg = f"逐拍审查 第 {beat} 拍（IMG {prev_seq:03d}→{target_seq:03d}）：{'未完成' if verdict == 'unreviewed' else ('检出 ' + str(len(formatted_issues)) + ' 处问题' if formatted_issues else '合格')}"
        on_progress('chain_guard_beat', {
            'beat': beat,
            'sequence': target_seq,
            'verdict': verdict,
            'issues': formatted_issues,
            'halt': halt,
            'message': msg,
        })

    return {
        'verdict': verdict,
        'issues': formatted_issues,
        'halt': halt,
        'inline_record': inline_record,
    }


def guard_anchor(config, title, prompt_block, project_dir, on_progress=None,
                 allow_halt=True, ref_path=None):
    """对第 1 帧（IMG 001 锚点帧）执行基准与对标审查与落盘。
    返回 dict:
      {
        'verdict': 'pass' | 'flagged' | 'unreviewed',
        'issues': [...],
        'halt': bool,
        'inline_record': {...}
      }
    """
    frames_dir = os.path.join(project_dir, 'frames')
    target_path = os.path.join(frames_dir, 'img_001.webp')
    if not os.path.exists(target_path):
        return {'verdict': 'unreviewed', 'issues': [], 'halt': False}

    images, _ = _parse_prompt_slots(prompt_block)
    total_beats = max(1, len(images) - 1)

    if ref_path is None:
        try:
            ref_dict, _ = find_reference_frames_for_project(project_dir, total_beats)
            ref_path = ref_dict.get(1)
        except Exception:
            ref_path = None

    # 这里要的是"这个项目磁盘上已有的那张封面"。project_cover_path 是**新封面的写入
    # 路径**生成器（文件名带当前毫秒时间戳、还会 makedirs），而且它的入参是 project_key
    # 不是 project_dir——传目录进去会在 outputs/ 下凭空造出一个同名的垃圾项目目录，
    # 返回的路径又永远不存在，COVER 比对分支于是恒为死代码。
    try:
        cover_path = resolve_cover_reference(config, title)
    except Exception:
        cover_path = None

    raw_issues = check_anchor_consistency(
        config, prompt_block, target_path,
        ref_frame_path=ref_path, cover_path=cover_path
    )

    formatted_issues = []
    if raw_issues is None:
        verdict = 'unreviewed'
        halt = False
    elif not raw_issues:
        verdict = 'pass'
        halt = False
    else:
        survived = []
        verify_imgs = [target_path]
        if ref_path and os.path.exists(ref_path):
            verify_imgs.append(ref_path)
        for text in raw_issues:
            v = _verify_review_violation(config, text, verify_imgs)
            if v is not False:
                survived.append({'text': text, 'verified': v})
        if not survived:
            verdict = 'pass'
            halt = False
        else:
            severities = classify_chain_impact(
                config, [item['text'] for item in survived], layer='anchor')
            for item, sev in zip(survived, severities):
                formatted_issues.append({
                    'beat': 0,
                    'layer': 'anchor',
                    'text': item['text'],
                    'frames': [1],
                    'verified': item['verified'],
                    'severity': sev,
                })
            verdict = 'flagged'
            halt = allow_halt and any(i.get('severity') == 'chain' for i in formatted_issues)

    target_hash = frame_content_hash(target_path)
    frames_sha256 = {'1': target_hash}

    inline_record = {
        'beat': 0,
        'sequence': 1,
        'verdict': verdict,
        'issues': formatted_issues,
        'frames_sha256': frames_sha256,
        'at': time.strftime('%Y-%m-%dT%H:%M:%S'),
    }

    with manifest_lock(project_dir):
        manifest = read_manifest(project_dir) or {}
        frames = manifest.get('frames') or []
        target_frame = next((f for f in frames if f.get('sequence') == 1), None)
        if target_frame is not None:
            target_frame['inline_anchor_review'] = inline_record
            if halt:
                chain_issues = [i['text'] for i in formatted_issues if i.get('severity') == 'chain']
                target_frame['quality_gate'] = 'sequence_review_flagged'
                target_frame['vlm_qa_reason'] = '；'.join(chain_issues or [i['text'] for i in formatted_issues])
                target_frame['flag_origin'] = 'chain_guard'
                target_frame['review_issues'] = [dict(i) for i in formatted_issues]
            elif verdict == 'pass' and target_frame.get('flag_origin') in ('chain_guard', 'fix_reverify'):
                # 只收回自己这一路盖出来的 flag。与 guard_beat 同一道护栏：无条件写
                # pending_manual_review 会把一枚已经拿到的 sequence_reviewed_pass
                # 反手降级成"待人工核对"。
                target_frame.pop('flag_origin', None)
                target_frame.pop('review_issues', None)
                if target_frame.get('quality_gate') == 'sequence_review_flagged':
                    target_frame['quality_gate'] = 'pending_manual_review'
                    target_frame['vlm_qa_reason'] = None
        try:
            write_manifest(project_dir, manifest)
        except Exception as e:
            log('WARN', 'CHAIN_GUARD', f"保存 manifest inline_anchor_review 异常: {e}")

    if on_progress:
        msg = f"首帧锚点审查（IMG 001）：{'未完成' if verdict == 'unreviewed' else ('检出 ' + str(len(formatted_issues)) + ' 处问题' if formatted_issues else '合格')}"
        on_progress('chain_guard_anchor', {
            'sequence': 1,
            'verdict': verdict,
            'issues': formatted_issues,
            'halt': halt,
            'message': msg,
        })

    return {
        'verdict': verdict,
        'issues': formatted_issues,
        'halt': halt,
        'inline_record': inline_record,
    }


def run_anchor_guard(config, title, prompt_block, project_dir, *, guard_mode,
                     forward_build, on_progress=None, on_manifest_dirty=None,
                     autofix_attempts=2):
    """首帧锚点守卫的完整一轮：审查 →（autofix 档）自动修复重审 → 给出停链结论。

    为什么单独有这么一个函数：`guard_beat` 的 halt 结论在三个渲染入口都被接住了，
    `guard_anchor` 的却一直被原地丢弃——三处调用点都只写了 `guard_anchor(...)`，
    返回值里的 halt 没人看。于是首帧判出结构级问题时：不停链、不自动修、也不发
    `chain_guard_halt`，只有 manifest 上悄悄多出一枚 `sequence_review_flagged`。
    首帧是整条 i2i 链的地基，它歪了后面每一帧都跟着歪，而屏幕上一声不响——用户
    要等帧网格下次从 manifest 重画才突然看见那枚 flag（"当时说通过、过一会儿说
    有问题"）。把逐拍守卫的那套动作原样补给锚点，并且只此一份：三个入口各抄一遍
    正是当初漏掉的成因。

    on_manifest_dirty：守卫与自动修复都是隔着磁盘改 manifest 的，每改完一次就调它，
    让调用方把自己那份内存副本追上去，否则调用方收尾整份写盘会把结论盖掉。

    返回 {
      'halt': 调用方是否该停链（已经含 guard_mode 判断，report 档恒为 False），
      'issues': [...],
      'prompt_block': 自动修复可能改写过的正文（没改就是传进来那份）,
    }
    """
    def _sync():
        if on_manifest_dirty:
            on_manifest_dirty()

    guard_res = guard_anchor(
        config, title, prompt_block, project_dir,
        on_progress=on_progress,
        allow_halt=forward_build,
    )
    _sync()

    # autofix：就地走一遍「修复此帧问题」，复审过了就接着往下渲。首帧没有前置视频
    # 过渡，fix_frame_issue 内部会自动走单提示词反馈重写那一支。
    if guard_res.get('halt') and guard_autofix_enabled(guard_mode) and forward_build:
        from pipeline_orchestrator import fix_frame_issue
        for attempt in range(1, autofix_attempts + 1):
            texts = '；'.join(
                i.get('text') or '' for i in (guard_res.get('issues') or [])
                if i.get('severity') == 'chain') or '结构级链式问题'
            if on_progress:
                on_progress('chain_guard_autofix', {
                    'beat': 0, 'sequence': 1,
                    'attempt': attempt, 'max_attempts': autofix_attempts,
                    'issues': guard_res.get('issues', []),
                    'message': f"🔧 首帧锚点检出结构级问题，正在就地自动修复 IMG 001"
                               f"（第 {attempt}/{autofix_attempts} 次）：{texts}",
                })
            try:
                fix_res = fix_frame_issue(
                    config, title, prompt_block, 1,
                    on_progress=on_progress, cascade_downstream=False,
                    suppress_chain_guard=True,
                )
            except GenerationCancelled:
                raise
            except Exception as fix_err:
                # 修复没跑成 ≠ 这帧没问题：guard_res 仍是 halt，跳出去让停链结论生效。
                log('WARN', 'CHAIN_GUARD',
                    f"IMG 001 自动修复第 {attempt} 次未跑成，转为停链: {fix_err}")
                break

            if (fix_res or {}).get('rolled_back'):
                log('WARN', 'CHAIN_GUARD',
                    f"IMG 001 自动修复第 {attempt} 次被三联屏门禁退回"
                    f"（画面已还原），转为停链等人处理")
                if on_progress:
                    on_progress('chain_guard_autofix_rolled_back', {
                        'beat': 0, 'sequence': 1, 'attempt': attempt,
                        'triptych': (fix_res or {}).get('triptych'),
                        'rejected_fix': (fix_res or {}).get('rejected_fix'),
                        'message': f"↩️ IMG 001 第 {attempt} 次自动修复被三联屏门禁退回，"
                                   f"画面已还原为修复前，转为停链等人处理",
                    })
                _sync()
                break

            new_block = (fix_res or {}).get('prompt_block')
            if new_block:
                # 首帧正文被改写过了：后面每一拍的守卫都要拿改写后的全文当上下文，
                # 否则它对着旧文本判新画面。
                prompt_block = new_block
            _sync()

            guard_res = guard_anchor(
                config, title, prompt_block, project_dir,
                on_progress=on_progress, allow_halt=True,
            )
            _sync()
            if not guard_res.get('halt'):
                if on_progress:
                    on_progress('chain_guard_autofix_done', {
                        'beat': 0, 'sequence': 1, 'attempt': attempt,
                        'message': f"✅ IMG 001 自动修复后复审通过（第 {attempt} 次），继续往下生成",
                    })
                break

    # halt 档一检出就停；autofix 档修满次数仍不过才停；report / autofix_soft 只记账
    # 不停链（与逐拍守卫同一口径：guard_anchor 该写的 flag 已经写进 manifest 了）。
    still_flagged = bool(guard_res.get('halt'))
    halt = still_flagged and guard_halt_enabled(guard_mode)
    if halt and on_progress:
        tail = ('' if guard_mode == 'halt'
                else f"（已自动修复 {autofix_attempts} 次仍未通过）")
        on_progress('chain_guard_halt', {
            'beat': 0,
            'sequence': 1,
            'issues': guard_res.get('issues', []),
            'autofix_exhausted': guard_autofix_enabled(guard_mode),
            'message': f"首帧锚点审查（IMG 001）检出结构级问题{tail}，生成已自动暂停——"
                       f"首帧是整条 i2i 链的地基，请先修复它再续渲。",
        })
    elif still_flagged and guard_mode == 'autofix_soft' and on_progress:
        # 软档：问题照记（manifest 上这一帧仍是 sequence_review_flagged），链继续往下走。
        # 必须发这条事件——否则"检出结构级问题"在软档下屏幕上一声不响，用户只会在
        # 收尾的质量风险汇总里突然看见一句"1 帧一致性审查未过"，不知道它从哪来。
        on_progress('chain_guard_soft_continue', {
            'beat': 0,
            'sequence': 1,
            'issues': guard_res.get('issues', []),
            'autofix_exhausted': True,
            'message': f"⚠️ 首帧锚点审查（IMG 001）仍有结构级问题（已自动修复 "
                       f"{autofix_attempts} 次），软档不停链——已记入待复核清单，继续往下渲。",
        })

    return {
        'halt': halt,
        'issues': guard_res.get('issues', []),
        'prompt_block': prompt_block,
    }
