"""Autonomous staged pipelines for the gemini-veo-restoration-composer production app.

**Rendering never reviews.** 2026-08-05: every consistency/acceptance gate that used to
run inside the render loop is gone — the anchor acceptance gate on IMAGE 1, the per-family
anchor gate, the periodic reality-checkpoint recalibration, and the chain-tail drift
lookback. Frames 1..N now render unconditionally, as fast as the backend allows, and no
step below rewrites a prompt or re-renders a frame on a judge's say-so.

The one cross-frame consistency review that remains is **manual and out-of-band**:
_sequence_consistency_review (exposed as run_sequence_consistency_review), which the user
triggers from the frame grid after the sequence is done. No entry point here runs it for
you, and it never blocks video generation.

Four entry points, sharing the same render/recovery machinery:

- render_single_frame: render ONE frame synchronously and return where it landed (no
  task_id/polling), so a caller — including a conversational agent mid-turn via
  /api/render_anchor — can look at it before composing anything else.

- render_frames_for_task: an ALREADY-composed prompt_block in, all missing frames
  rendered out. Used by server.py's /api/generate_frames — the main "帧序列" button's
  entry point. Does not generate video.

- run_autonomous_pipeline: dimensions in, everything out. Composes IMAGE 1's prompt
  itself (compose_anchor_and_packet), renders it, refines the Drift Lock packet against
  that render, then composes and renders the rest (compose_remaining_beats), and
  generates video. Used by server.py's /api/auto_run.

- run_staged_frame_rendering: an ALREADY-composed prompt_block in, rendering out. For
  the case where an agent already wrote the full IMAGE/VIDEO prompt text and now wants
  the rest rendered. Used by server.py's /api/render_staged, which
  scripts/generate_frames.py calls instead of the old one-shot /api/generate_frames.

All entry points lead into the same autonomous recovery pass over any rejected/blocked
video clip, so nothing ever dead-ends in a manual-review state.
"""
import json
import os
import shutil
from datetime import datetime

from server_common import (
    _get_project_dir, read_manifest, write_manifest, manifest_lock,
    IMG2IMG_CONTROL_PROMPT,
    frame_content_hash, drop_stale_review_verdicts, REAL_REVIEW_VERDICTS,
)
from prompt_pipeline import (
    frame_review_status, merge_review_results,
    outline_items_by_beat, outline_delivery_log_line,
    compose_anchor_and_packet,
    compose_remaining_beats,
    refine_packet_from_accepted_anchor,
    fix_image_prompt_with_vlm_feedback,
    check_full_sequence_consistency,
    _verify_review_violation,
    fix_beat_from_sequence_review,
    image_space_family,
    clean_prompt_text,
    fix_image_clean_frame_proactive,
    fix_horizon_line,
    fix_camera_contradictions,
    prompt_slots_list,
    _format_prompt_block,
    _parse_prompt_slots,
)
from frame_generator import (
    generate_frame_sequence, _generate_image_edit, _image_edit_model,
    _measure_image_pixels, _chat_transport_is_full_quality, chat_transport_note,
    update_manifest_stale_status, CHAT_TRANSPORT,
)
from video_generator import generate_video_sequence

_MAX_RECOVERY_ATTEMPTS = 2


def _frame_path(title, sequence):
    return os.path.join(_get_project_dir(title), 'frames', f'img_{sequence:03d}.webp')


def _frame_manifest_entry(project_dir, sequence):
    """Read a single frame's manifest.json entry, or None if no manifest/frame entry
    exists yet."""
    manifest = read_manifest(project_dir)
    if not manifest:
        return None
    for frame in manifest.get('frames', []):
        if frame.get('sequence') == sequence:
            return frame
    return None


def _frame_quality_gate(project_dir, sequence):
    """Read a single frame's recorded quality_gate from manifest.json, or None if no
    manifest/frame entry exists yet."""
    entry = _frame_manifest_entry(project_dir, sequence)
    return entry.get('quality_gate') if entry else None


def _record_review_fingerprints(project_dir, title, sequences):
    """把本轮审查实际看过的帧内容哈希记进 manifest（review_frames_sha256: {seq: hash}）。

    每帧记的是"它参与的那两拍所涉及的帧"的哈希——帧 seq 的结论同时依赖 seq-1/seq/seq+1
    三张图，其中任何一张被重渲，这个结论就该作废。修完 IMG 005 之后，beat 4/5 的判定其实
    已经作废，IMG 004 和 IMG 006 却仍挂着 sequence_reviewed_pass，前端显示的"全部审查
    通过"从那一刻起就是假的。

    哈希按 sequences 的**邻域**（seq-1/seq/seq+1）算，而不是只算 sequences 自己：增量
    审查（只重审失效的那几拍）下，边界帧的邻居这一轮没被重审，但它的结论依然依赖那张
    邻居图。漏记的话，那张邻居图之后被重渲时这条结论不会作废——增量审查会一直认为它
    还有效，永远不再复查。"""
    wanted = {int(s) for s in sequences}
    neighborhood = {s + d for s in wanted for d in (-1, 0, 1)}
    hashes = {s: frame_content_hash(_frame_path(title, s)) for s in sorted(neighborhood)}
    with manifest_lock(project_dir):
        manifest = read_manifest(project_dir)
        if not manifest:
            return
        for frame in manifest.get('frames', []):
            seq = frame.get('sequence')
            # 只给本轮真的审过的帧盖指纹；邻居的哈希只是拿来填进它们的 related 里，
            # 不能顺手把邻居也标成"刚审过"
            if seq not in wanted:
                continue
            related = [s for s in (seq - 1, seq, seq + 1) if s in hashes and hashes[s]]
            if not related:
                continue
            frame['review_frames_sha256'] = {str(s): hashes[s] for s in related}
            frame['reviewed_at'] = datetime.now().isoformat(timespec='seconds')
        write_manifest(project_dir, manifest)


def persist_outline_delivery_ledger(project_dir, ledger, title=None):
    """把卡片工序交付总账落进项目 manifest —— 帧审查阶段唯一拿得到它的地方。

    一致性审查是用户在帧网格上手动触发的独立入口（run_sequence_consistency_review），
    跟合成那次运行不在同一个进程生命周期里，config['_outline_delivery_ledger'] 到不了
    那边，必须落盘。选 manifest 而不是新开一个文件：它本来就是这条边界上的存量载体
    （spatial_beats / spatial_contract 同款投影），两个渲染后端都刻意保留未知键。

    落盘失败一律吞掉：总账在本次 run 的 result 里已经有完整一份，留痕不该拖垮出单。"""
    if not ledger:
        return False
    try:
        os.makedirs(project_dir, exist_ok=True)
        with manifest_lock(project_dir):
            manifest = read_manifest(project_dir) or {'title': title or '', 'frames': []}
            manifest['outline_delivery_ledger'] = ledger
            write_manifest(project_dir, manifest)
        return True
    except OSError as e:
        print(f"[OUTLINE-AUDIT] 交付总账落盘失败（不影响本单交付）: {e}")
        return False


def _outline_items_for_review(project_dir):
    """manifest 里的交付总账 → 帧审查按拍取用的工序投影（没有就返回空 dict）。"""
    manifest = read_manifest(project_dir) or {}
    ledger = manifest.get('outline_delivery_ledger')
    if not isinstance(ledger, list) or not ledger:
        return {}
    return outline_items_by_beat(ledger)


def _record_outline_frame_verdicts(project_dir, verdicts, skeleton=None):
    """把画面层的逐条工序判定写回 manifest 里的那份总账，并打一行观测日志。

    灰度期（prompt_pipeline._OUTLINE_FRAME_GATE_ENFORCING=False）这是这类判定的**唯一**
    去处：不进 failures、不碰 quality_gate、不触发任何重渲。
    · 隐蔽工序（rough-in/framing 后紧接封板）的 not_applicable 不被覆盖——那是确定性
      结论，VLM 说"看不见"是对的观察、错的结论；
    · 没落点（dropped）的行同样不碰：没人认领就无从谈画面交付。"""
    if not verdicts:
        return None
    with manifest_lock(project_dir):
        manifest = read_manifest(project_dir) or {}
        ledger = manifest.get('outline_delivery_ledger')
        if not isinstance(ledger, list) or not ledger:
            return None
        for row in ledger:
            if not isinstance(row, dict) or row.get('plan_verdict') == 'dropped' \
                    or row.get('frame_verdict') == 'not_applicable':
                continue
            verdict = verdicts.get(str(row.get('index')))
            if verdict:
                row['frame_verdict'] = verdict
        manifest['outline_delivery_ledger'] = ledger
        write_manifest(project_dir, manifest)
    line = outline_delivery_log_line(ledger, skeleton)
    if line:
        print(line)
    return ledger


def invalidate_stale_review_verdicts(project_dir, on_progress=None):
    """drop_stale_review_verdicts 的"读-改-写 manifest"外壳：帧文件变了就让它相关的
    审查结论作废，并广播一条说明。返回被作废的帧序号列表。"""
    with manifest_lock(project_dir):
        manifest = read_manifest(project_dir)
        if not manifest:
            return []
        changed = drop_stale_review_verdicts(manifest, project_dir)
        if changed:
            write_manifest(project_dir, manifest)
    if changed and on_progress:
        names = '、'.join(f'{s:03d}' for s in sorted(changed))
        on_progress('review_invalidated', {
            'sequences': sorted(changed),
            'message': f'帧内容已变化，IMG {names} 的一致性审查结论已作废，请重新运行审查',
        })
    return changed


_UNSET = object()


def _set_manifest_quality_gate(project_dir, sequence, quality_gate, reason=None,
                               review_issues=_UNSET, respect_manual_flag=False):
    """Overwrite one frame's recorded quality_gate/vlm_qa_reason in manifest.json.
    渲染路径本身不再产生任何判定（帧一律落 pending_manual_review），所以这里的写入方
    只剩手动一致性审查与人工标记两条。

    review_issues：一致性审查的结构化违规记录（每条含 layer/beat/frames/verified，见
    prompt_pipeline.check_full_sequence_consistency）。vlm_qa_reason 只是给人看的摘要，
    '；'.join 之后哪一层检出的、涉及哪几帧、复核确认没有全丢了——修完也没法验证这一条
    到底解决没有。默认 _UNSET＝不碰这个字段；传 None 表示显式清空。

    respect_manual_flag=True（一致性审查的两个写入点专用）：该帧当前是 'manual_flagged'
    时不夺走这个标记，只把机器判定存进 manual_flag_prev_gate（撤销人工标记时回落到最新
    的机器结论）。

    2026-07-30 修复：此前这里无条件覆盖 quality_gate，于是"用户描述了这一帧的问题 →
    随后跑一次一致性审查 → 机器没看出那个问题"就会把 manual_flagged 洗成
    sequence_reviewed_pass。视频门禁看的正是 quality_gate（见
    video_generator._FLAGGED_QUALITY_GATES），那道硬拦就此消失；而 manual_issue 字段
    还留着，帧网格照旧显示「人工标记」徽标——界面说标了、门禁说没标，是最坏的一种
    不一致。set_manual_frame_issue 的注释早就写明两者应当并存（机器判定进
    vlm_qa_reason，人的描述进 manual_issue），只是审查的写入端没有遵守。"""
    # read_manifest/write_manifest：同一把项目级锁 + 原子替换（读改写不再互相覆盖）
    with manifest_lock(project_dir):
        manifest = read_manifest(project_dir)
        if not manifest:
            return
        for frame in manifest.get('frames', []):
            if frame.get('sequence') == sequence:
                if respect_manual_flag and frame.get('quality_gate') == 'manual_flagged':
                    frame['manual_flag_prev_gate'] = quality_gate
                else:
                    frame['quality_gate'] = quality_gate
                frame['vlm_qa_reason'] = reason
                if review_issues is not _UNSET:
                    if review_issues:
                        frame['review_issues'] = review_issues
                    else:
                        frame.pop('review_issues', None)
                break
        write_manifest(project_dir, manifest)


def set_manual_frame_issue(title, sequence, description):
    """人工主动描述某一帧的问题：把描述写进 manifest 该帧的 manual_issue，并把
    quality_gate 标成 'manual_flagged'（被覆盖的原值存进 manual_flag_prev_gate 供撤销
    时回退）。供 server.py 的 /api/flag_frame_issue 调用。

    与机器判定分开存放：一致性审查的结论留在 vlm_qa_reason，人的描述留在
    manual_issue——两者可以并存（人看过审查结论后再补充自己发现的问题），
    fix_frame_issue 会把两份都当成待修问题一起交给提示词改写。之所以不直接覆盖
    vlm_qa_reason：那样人一描述就抹掉了机器的原始判定，事后无从对照谁看漏了什么。

    description 传空串＝撤销人工标记：清掉 manual_issue，quality_gate 回退到
    manual_flag_prev_gate（缺失时按 vlm_qa_reason 有无落到 sequence_review_flagged /
    pending_manual_review）。

    返回更新后的帧条目 dict；manifest 或该帧不存在时抛 RuntimeError——人工描述的是
    自己看到的画面，落不了盘必须当场报错，不能静默丢弃。"""
    project_dir = _get_project_dir(title)
    description = (description or '').strip()
    with manifest_lock(project_dir):
        manifest = read_manifest(project_dir)
        if not manifest:
            raise RuntimeError(f'项目「{title}」尚无 manifest，无法标记帧问题')
        target = None
        for frame in manifest.get('frames', []):
            if frame.get('sequence') == sequence:
                target = frame
                break
        if target is None:
            raise RuntimeError(f'manifest 中没有第 {sequence} 帧，无法标记问题')

        if description:
            if target.get('quality_gate') != 'manual_flagged':
                target['manual_flag_prev_gate'] = target.get('quality_gate')
            target['manual_issue'] = description
            target['quality_gate'] = 'manual_flagged'
        else:
            target.pop('manual_issue', None)
            prev = target.pop('manual_flag_prev_gate', None)
            if target.get('quality_gate') == 'manual_flagged':
                if not prev:
                    prev = 'sequence_review_flagged' if target.get('vlm_qa_reason') else 'pending_manual_review'
                target['quality_gate'] = prev
        write_manifest(project_dir, manifest)
        return dict(target)


def _clear_manual_frame_issue(project_dir, sequence):
    """修复完成后清掉该帧的人工问题描述：问题已经按描述改过提示词重渲了，留着
    manual_issue 会让帧网格继续显示「人工标记」徽标，看着像没修。gate 仍停在
    manual_flagged 的话一并回落到 pending_manual_review（重渲后的画面是否真的解决了
    问题，仍待人工再看一眼）。"""
    with manifest_lock(project_dir):
        manifest = read_manifest(project_dir)
        if not manifest:
            return
        for frame in manifest.get('frames', []):
            if frame.get('sequence') == sequence:
                frame.pop('manual_issue', None)
                frame.pop('manual_flag_prev_gate', None)
                if frame.get('quality_gate') == 'manual_flagged':
                    frame['quality_gate'] = 'pending_manual_review'
                break
        write_manifest(project_dir, manifest)


def render_single_frame(config, title, sequence, prompt, meta='', on_progress=None):
    """Render exactly one frame synchronously and return where it landed (no
    task_id/polling), so a caller — including a conversational agent mid-turn via
    /api/render_anchor — can look at the result before composing anything else.

    2026-08-05：本函数此前是「锚帧验收门」（渲染 → VLM 判定 → 按反馈改写提示词重抽 →
    仍不过就中止整条链）。那套门连同其余所有生成期一致性审查一并移除，这里只剩渲染：
    帧照常落盘、manifest 记 pending_manual_review（＝没人看过），要不要复核由用户在帧
    网格上手动决定。

    Returns {'status': 'rendered', 'prompt', 'image_path', 'project_dir'}。"""
    images = {sequence: {'body': prompt, 'meta': meta}}
    generate_frame_sequence(config, title, _format_prompt_block(images, {}),
                            on_progress=on_progress, target_sequences=[sequence])
    project_dir = _get_project_dir(title)
    return {
        'status': 'rendered',
        'prompt': prompt,
        'image_path': _frame_path(title, sequence),
        'project_dir': project_dir,
    }


def _valid_verdict_sequences(project_dir, sequences):
    """这些帧里，哪几帧的审查结论此刻仍然成立。

    **必须在 invalidate_stale_review_verdicts 之后调用**：作废逻辑已经把"所看帧图变过"
    的结论清成 pending_manual_review 并摘掉 review_frames_sha256，所以此刻"还带着指纹的
    真实结论"就等于"仍然成立的结论"，这里不再自己比一遍哈希（比第二遍就是第二套口径）。

    人工标记压着机器判定时真实结论存在 manual_flag_prev_gate 里（见
    _set_manifest_quality_gate 的 respect_manual_flag），同样算数——人工标记是给人看的
    待办，不代表机器那一拍没审过。"""
    wanted = {int(s) for s in sequences}
    manifest = read_manifest(project_dir) or {}
    out = set()
    for frame in manifest.get('frames') or []:
        seq = frame.get('sequence')
        if seq not in wanted or not frame.get('review_frames_sha256'):
            continue
        if (frame.get('quality_gate') in REAL_REVIEW_VERDICTS
                or frame.get('manual_flag_prev_gate') in REAL_REVIEW_VERDICTS):
            out.add(seq)
    return out


def _manifest_review_summary(project_dir, sequences):
    """这段已渲染序列**此刻**的审查状态（从 manifest 现读）：
    {'flagged': {seq: 原因}, 'unreviewed': [seq, ...]}。

    增量审查下本轮只审了几拍，光按本轮结果汇总会把上几轮标出来、还没修的问题说没了。
    人工标记压着机器判定时真实结论在 manual_flag_prev_gate 里，一并算进来。

    2026-08-05 修正"未审"的口径：以前只把 sequence_review_skipped（审查跑过但失败）
    算未审，于是 pending_manual_review——**从来没被审过**的初始态——既不进 flagged 也不进
    unreviewed，在所有汇总里静默读作"没问题"。实测代价：某一单 12 帧里 4 帧是这个状态、
    vlm_qa_reason 全空，而汇总报的是"审查通过"。审查是手动触发的（见
    _sequence_consistency_review 的 2026-07-24 变更），渲染继续往后跑而审查停在第 8 帧
    是完全正常的路径，所以这个状态很常见，不是异常。
    现在的口径只有一条：**没有真实结论（REAL_REVIEW_VERDICTS）就是未审**——manifest 里
    没有这一帧的记录同样算未审，而不是当它不存在。"""
    wanted = {int(s) for s in sequences}
    manifest = read_manifest(project_dir) or {}
    flagged, unreviewed = {}, []
    seen = set()
    for frame in manifest.get('frames') or []:
        seq = frame.get('sequence')
        if seq not in wanted:
            continue
        seen.add(seq)
        gate = frame.get('quality_gate')
        prev = frame.get('manual_flag_prev_gate')
        if 'sequence_review_flagged' in (gate, prev):
            flagged[seq] = frame.get('vlm_qa_reason') or '（未记录原因）'
        elif gate not in REAL_REVIEW_VERDICTS and prev not in REAL_REVIEW_VERDICTS:
            unreviewed.append(seq)
    # manifest 里根本没有记录的帧也是未审——漏记不等于通过
    unreviewed.extend(sorted(wanted - seen))
    return {'flagged': flagged, 'unreviewed': sorted(set(unreviewed))}


def _beats_needing_review(rendered, valid_seqs):
    """需要（重）审的拍号：第 b 拍看的是 IMG b 与 IMG b+1，两张里任何一张没有仍然
    成立的结论，这一拍就得重审。

    结论作废是**邻域**级的（帧 X 变了，X-1/X/X+1 的结论一起作废，见
    _record_review_fingerprints），所以"某一拍要重审"必然意味着它的两张帧都已经失效，
    两边不会打架。"""
    ordered = sorted(rendered)
    total_beats = len(ordered) - 1
    return [b for b in range(1, total_beats + 1)
            if ordered[b - 1] not in valid_seqs or ordered[b] not in valid_seqs]


def _sequence_consistency_review(config, title, prompt_block, project_dir, on_progress=None,
                                 full=False):
    """整套序列渲染完成后的一致性审查：对着真实已渲染画面统一跑一次施工顺序/SCUP
    审查（prompt_pipeline.check_full_sequence_consistency），取代原来逐帧/盲文本的
    质检门。

    2026-07-23 行为变更：发现问题**不再自动改写提示词重渲**。此前会自动跑最多两轮
    "改提示词→重渲→再审"，全程不问人；现在检出问题就立即停手，只把原因写进
    vlm_qa_reason 供人工核对——真正的改写+重渲要等用户在帧网格确认后点击
    「修复此帧问题」（fix_frame_issue）才会发生，避免在人没看过问题描述之前
    就先斩后奏地改动构图。有问题的帧标 'sequence_review_flagged'；其余标
    'sequence_reviewed_pass'。返回（未被改写的）prompt_block。

    2026-07-24 行为变更：不再被任何渲染入口自动调用——三处调用点
    （render_frames_for_task / run_autonomous_pipeline / run_staged_frame_rendering）
    已移除自动触发，改成用户在帧网格手动点按钮才跑（见下方公开入口
    run_sequence_consistency_review），渲染完直接进视频阶段不再等它。

    2026-07-25：结论落盘时会连同"本轮看的是哪几张图"的内容哈希一起记下
    （_record_review_fingerprints），任何一帧此后被重渲都会让相关结论自动作废
    （invalidate_stale_review_verdicts），不再出现修完一帧后邻帧还挂着过期"审查通过"。

    2026-08-02 增量审查（full=False，默认）：只重审"结论已经失效"的那几拍，仍然成立的
    结论原样保留、连同它们的帧一起不再送审。此前每次都是全量——十几帧的单子修完三帧
    再审一遍，要把已经审干净的十来拍连同跨帧窗口整批重烧，几分钟起步，于是"修完就重审"
    这个本该最顺手的动作反而没人愿意做。判定材料早就齐了（review_frames_sha256 +
    drop_stale_review_verdicts），送审收窄的开关也早就有（only_beats / global_only_beats，
    降级重试一直在用），这里只是把两者接上。

    full=True：强制全量重审，不复用任何既有结论。跨帧层是按窗口切的（global_review_windows），
    增量只重跑覆盖失效拍的那几个窗口——某一帧的改动理论上可能影响别的窗口里的判断，
    这条出口就是留给"想让整链重新互相比一遍"的时候用的。"""
    images, videos = _parse_prompt_slots(prompt_block)
    total_beats = len(images) - 1
    if total_beats <= 0:
        return prompt_block

    # 开审前先清一遍过期结论：上一轮审查之后有帧被重渲的话，那些 pass/flagged 早已
    # 不成立，留着会让本轮的"哪些帧还需要审"判断建立在假信息上。
    invalidate_stale_review_verdicts(project_dir, on_progress=on_progress)

    # 只审"从 IMAGE 1 起连续渲出来的那一段"。此前只要缺任何一帧就整体放弃、一拍都不
    # 审，逐帧手动生成到一半想中途查一下完全办不到；而跨帧规则（地标坐标锁、载体身份）
    # 本来就是从链头往后比的，连续前缀是它唯一有意义的作用域——中间断开后面的帧接的是
    # 另一条链，混进来只会制造假阳性。
    all_seqs = sorted(images)
    rendered = []
    for seq in all_seqs:
        if not os.path.exists(_frame_path(title, seq)):
            break
        rendered.append(seq)
    frame_paths = {s: _frame_path(title, s) for s in rendered}
    partial = len(rendered) < len(all_seqs)
    if len(rendered) < 2:
        # 连一拍都凑不齐（0/1 帧）：没有任何可比对的画面对，如实早退且不触碰 manifest
        if on_progress:
            on_progress('sequence_review_result', {
                'passed': False, 'skipped': True,
                'message': f'已渲染的帧不足以构成一拍（{len(rendered)}/{len(all_seqs)}），'
                           f'审查已跳过——请至少渲出前两帧再重试。',
            })
        return prompt_block

    # 增量：仍然成立的结论不再重审（full=True 时全部重来）。
    reusable = set() if full else _valid_verdict_sequences(project_dir, rendered)
    beats_to_review = _beats_needing_review(rendered, reusable)
    all_beats = list(range(1, len(rendered)))
    incremental = len(beats_to_review) < len(all_beats)
    # 本轮真正会被写结论的帧＝这几拍涉及的帧里，**结论已经失效的那些**。
    #
    # 减掉 reusable 这一步不是省事，是正确性：一拍被选中重审，可能只是因为它另一头
    # 的帧变了。比如 IMG 006 被修过、IMG 005 上一轮被判过问题——beat 5（005→006）
    # 必须重审，但 IMG 005 的"有问题"是 beat 4 判的，而 beat 4 这轮压根没跑。把
    # 005 一起重写就会拿一个没人复查过的"通过"洗掉它身上还没修的问题。
    #
    # 反过来，留下的每一帧（结论已失效的那些）的**两拍都在 beats_to_review 里**
    # ——结论作废是邻域级的，帧 X 失效必然让 beat X-1 与 beat X 一起进重审名单。
    # 所以 frame_review_status 那套"两拍都审过才算通过"的口径原样成立，这里不需要
    # 第二套状态推导。
    affected = sorted({s for b in beats_to_review
                       for s in (rendered[b - 1], rendered[b])} - reusable)

    if not beats_to_review:
        # 一拍都不用重审：如实说明，不烧任何调用、不碰 manifest。仍然要把"上几轮标出来、
        # 还没修的问题"报出来——这一趟没发现新问题，不等于这套序列干净。
        if on_progress:
            state = _manifest_review_summary(project_dir, rendered)
            tail = ''
            if state['flagged']:
                names = '、'.join(f'{s:03d}' for s in sorted(state['flagged']))
                tail = f'；仍有 {len(state["flagged"])} 帧带着尚未修复的问题（IMG {names}）'
            elif state['unreviewed']:
                names = '、'.join(f'{s:03d}' for s in state['unreviewed'])
                tail = f'；另有 {len(state["unreviewed"])} 帧此前未审完（IMG {names}），可用「全量重审」补齐'
            on_progress('sequence_review_result', {
                'passed': not state['flagged'] and not state['unreviewed'],
                'partial': partial, 'reviewed_sequences': [],
                'unreviewed_sequences': state['unreviewed'],
                'reused_beats': len(all_beats), 'rendered_count': len(rendered),
                'message': (f'一致性审查：全部 {len(all_beats)} 拍的结论仍然成立'
                            f'（帧图自上次审查后没有变化），本轮未重复审查{tail}。'),
            })
        return prompt_block

    if on_progress:
        scope = (f'本轮只需重审 {len(beats_to_review)}/{len(all_beats)} 拍'
                 f'（其余 {len(all_beats) - len(beats_to_review)} 拍的结论仍然成立，直接复用）'
                 if incremental else f'本轮审查全部 {len(all_beats)} 拍')
        if partial:
            on_progress('sequence_review', {
                'message': f'仅前 {len(rendered)}/{len(all_seqs)} 帧已渲染，'
                           f'先对这一段做一致性审查（其余帧渲完后可再跑一次补齐）。{scope}...',
            })
        else:
            on_progress('sequence_review', {'message': f'正在做一致性审查：{scope}...'})
    # 卡片工序在**画面**上交付了没有：工序原文从 manifest 的交付总账里取，沿逐拍层
    # 透传下去（见 prompt_pipeline.outline_frame_review_block）。老单/没落过总账的单
    # 拿到空 dict，那一层的审查措辞与改造前逐字相同。
    # 没有总账的单（老单/手填维度直出）连这个入参都不传，整条审查链路的调用形状
    # 与改造前逐字相同。
    outline_kw = {}
    outline_items = _outline_items_for_review(project_dir)
    if outline_items:
        outline_kw['outline_items'] = outline_items
    final_result = check_full_sequence_consistency(
        config, prompt_block, frame_paths, on_progress=on_progress,
        only_beats=(beats_to_review if incremental else None),
        global_only_beats=(beats_to_review if incremental else None),
        **outline_kw)
    if final_result is None:
        # 整轮彻底没跑起来（超时/网关异常）≠ 审查通过。降级重试一次：帧图压小 +
        # 超时放宽。2026-07-15 事故里这里曾直接 fail-open 放行整单。
        if on_progress:
            on_progress('sequence_review', {
                'message': '一致性审查调用失败（超时/网关异常），压缩帧图降级重试一次...',
            })
        final_result = check_full_sequence_consistency(
            config, prompt_block, frame_paths, degraded=True, on_progress=on_progress,
            only_beats=(beats_to_review if incremental else None),
            global_only_beats=(beats_to_review if incremental else None),
            **outline_kw)
    elif final_result.get('unreviewed_beats') or not final_result.get('global_reviewed'):
        # 部分没审成：只降级重跑这几拍（外加没跑成的跨帧层），不再把已经审干净的
        # 整批重来一遍——一次网络抖动此前会让整单多烧一遍全部调用。
        retry_beats = list(final_result.get('unreviewed_beats') or [])
        # 跨帧层现在按窗口分批（prompt_pipeline.global_review_windows）：个别窗口没跑成
        # 时 global_reviewed 仍是 True（其余窗口有判定），只看它就会跳过补跑，那几拍在
        # 重试后反而被洗成"已审"——比不重试更糟。只有"跑过且一个窗口都没漏"才允许跳过；
        # 有漏的窗口就带着 global_only_beats 精确补跑那几个窗口，不整层重来。
        global_unreviewed = list(final_result.get('global_unreviewed_beats') or [])
        skip_global = bool(final_result.get('global_reviewed')) and not global_unreviewed
        if on_progress:
            scope = (f"第 {'、'.join(str(b) for b in retry_beats)} 拍" if retry_beats else '')
            scope += ('' if skip_global else ('与跨帧审查' if scope else '跨帧审查'))
            on_progress('sequence_review', {
                'message': f'{scope}未跑成，压缩帧图降级重试这部分...',
            })
        retry = check_full_sequence_consistency(config, prompt_block, frame_paths, degraded=True,
                                                only_beats=retry_beats, skip_global=skip_global,
                                                global_only_beats=(global_unreviewed or None),
                                                on_progress=on_progress, **outline_kw)
        final_result = merge_review_results(final_result, retry)

    if final_result is None:
        # 审查两次（常规+降级）都彻底没跑起来。如实标"未经审查"，绝不盖
        # sequence_reviewed_pass 假章——但**只标那些本来就没有真实结论的帧**：上一轮
        # 成功审查留下的 reviewed_pass / review_flagged 是真实信息，帧文件这期间没被
        # 动过（审查不渲图），一次网关抖动或一次用户取消不该把它们全部清零
        # （2026-07-25：取消被 except Exception 吞掉后正是走到这里，把整单洗成未审查）。
        kept = 0
        for seq in rendered:
            if _frame_quality_gate(project_dir, seq) in REAL_REVIEW_VERDICTS:
                kept += 1
                continue
            _set_manifest_quality_gate(project_dir, seq, 'sequence_review_skipped',
                                       '一致性审查服务不可用（降级重试仍失败），此帧未经整套序列审查',
                                       respect_manual_flag=True)
        if on_progress:
            tail = f'（{kept} 帧保留了上一轮的审查结论）' if kept else ''
            on_progress('sequence_review_result', {
                'passed': False, 'skipped': True,
                'message': f'一致性审查服务不可用（降级重试仍失败）：未审过的帧已如实标记为「未经审查」{tail}，请留意画面一致性。',
            })
        return prompt_block

    # 卡片工序的画面判定先落账（灰度期它只到这里为止，绝不参与下面的 quality_gate）
    _record_outline_frame_verdicts(project_dir, final_result.get('outline_frame_verdicts'))

    statuses = frame_review_status(rendered, final_result)
    gate_by_status = {'flagged': 'sequence_review_flagged',
                      'reviewed': 'sequence_reviewed_pass',
                      'unreviewed': 'sequence_review_skipped'}
    # 结构化违规按"归属帧"（beat+1，即该拍的到达画面）分组落盘
    issues_by_seq = {}
    for issue in (final_result.get('issues') or []):
        if issue.get('verified') is False:
            continue  # 复核否决的不落盘，否则帧上会留着一堆已被推翻的指控
        issues_by_seq.setdefault(issue.get('beat', 0) + 1, []).append(issue)
    # 只写本轮真的审过的那些帧：其余帧的结论仍然成立（reusable），重新盖一遍章
    # 只会把 reviewed_at 刷新成谎话，还会把它们上一轮的 review_issues 抹掉。
    for seq in affected:
        status, reason = statuses.get(seq, ('unreviewed', '未参与本轮审查'))
        # respect_manual_flag：机器没看出人已经指出来的问题，不代表那个问题不存在。
        # 人工标记必须活过一次审查，否则视频门禁的硬拦会被静默摘掉。
        _set_manifest_quality_gate(project_dir, seq, gate_by_status[status], reason,
                                   review_issues=issues_by_seq.get(seq) or None,
                                   respect_manual_flag=True)
    # 把"本轮看的是哪几张图"钉进 manifest：之后任何一帧被重渲，
    # invalidate_stale_review_verdicts 就能据此让相关结论自动作废
    _record_review_fingerprints(project_dir, title,
                                [s for s in affected if statuses.get(s, ('', ))[0] != 'unreviewed'])

    # 汇总说的是"这段已渲染序列此刻是什么状态"，而不是"本轮审了什么"——增量审查下
    # 本轮只看了几拍，光报本轮结果会把上几轮标出来、还没修的问题说没了。
    state = _manifest_review_summary(project_dir, rendered)
    flagged_seqs = sorted(state['flagged'])
    unreviewed_seqs = state['unreviewed']
    reused_beats = len(all_beats) - len(beats_to_review)

    # 只审了连续前缀时必须说明还剩几帧没渲——否则一句"审查通过"会被读成整单通过
    partial_note = (f'（本轮只覆盖已渲染的前 {len(rendered)}/{len(all_seqs)} 帧，'
                    f'其余帧渲完后请再跑一次）' if partial else '')
    reuse_note = f'（本轮增量重审 {len(beats_to_review)} 拍，另有 {reused_beats} 拍沿用既有结论）' \
        if reused_beats else ''

    if not flagged_seqs and not unreviewed_seqs:
        if on_progress:
            on_progress('sequence_review_result', {
                'passed': True, 'partial': partial,
                'reviewed_sequences': sorted(affected),
                'reused_beats': reused_beats, 'rendered_count': len(rendered),
                **({'message': (f'已渲染的前 {len(rendered)} 帧一致性审查通过'
                                f'{partial_note}{reuse_note}')}
                   if (partial or reuse_note) else {}),
            })
        return prompt_block

    if on_progress:
        parts = []
        if flagged_seqs:
            detail = '；'.join(f"IMG {s:03d}: {state['flagged'][s]}" for s in flagged_seqs)
            parts.append(f'发现 {len(flagged_seqs)} 帧存在问题（{detail}）——已保留渲染结果、'
                         f'未自动修改，请在帧网格确认后点击「修复此帧问题」')
        if unreviewed_seqs:
            # 漏审必须说出来：此前这些帧会被静默盖成"审查通过"
            parts.append(f'另有 {len(unreviewed_seqs)} 帧未审完'
                         f'（IMG {"、".join(f"{s:03d}" for s in unreviewed_seqs)}），'
                         f'已标记为「未审查」，可重跑审查补齐')
        on_progress('sequence_review_result', {
            'passed': False, 'beats': sorted((final_result.get('failures') or {}).keys()),
            'unreviewed_sequences': unreviewed_seqs,
            'partial': partial, 'reviewed_sequences': sorted(affected),
            'reused_beats': reused_beats, 'rendered_count': len(rendered),
            'message': '一致性审查' + '；'.join(parts) + '。' + partial_note + reuse_note,
        })
    return prompt_block


def run_sequence_consistency_review(config, title, prompt_block, on_progress=None, full=False):
    """`_sequence_consistency_review` 的手动触发入口：供 server.py 的
    /api/sequence_review 调用——2026-07-24 起该审查不再被任何渲染入口自动跑，
    用户需在帧网格确认整套序列已渲染完成后手动点按钮触发。不阻塞视频生成，纯粹
    是一个可选的人工检查工具。

    full=True＝强制全量重审（前端的「全量重审」入口）；默认走增量，只重审结论已经
    失效的那几拍。"""
    project_dir = _get_project_dir(title)
    return _sequence_consistency_review(config, title, prompt_block, project_dir,
                                        on_progress=on_progress, full=full)


def _fix_frame_via_image_edit(config, title, sequence, new_prompt, on_progress=None):
    """首帧定向修复通道：拿首帧自己已渲出的图
    当参考做自编辑（reference_path 与 target_path 相同；_generate_image_edit 会
    先把参考图整个读进内存再写目标文件，同路径自编辑不会读到被截断的半成品）。
    非首帧不需要这条路：seq>1 时 generate_frame_sequence 天然走图生图链式编辑，
    直接调它即可（见 fix_frame_issue）。"""
    target_path = _frame_path(title, sequence)
    if not os.path.exists(target_path):
        raise RuntimeError(f'第 {sequence} 帧尚未渲染，无法编辑修复')
    # _generate_image_edit 是直连网关的 API 实现，不认 imageBackend='google_fx'
    # （浏览器自动化）——那条后端没有"编辑单帧"的等价能力。宁可明确报错，也不要
    # 静默绕过用户选择的后端去打一个可能根本没配置好的直连网关。
    if (config.get('imageBackend') or 'api').strip().lower() == 'google_fx':
        raise RuntimeError('首帧的定向修复暂不支持 google_fx 后端，请在配置中心切到 api 后端后重试')

    if on_progress:
        on_progress('frame_start', {'sequence': sequence, 'total': 1})

    model = _image_edit_model(config)
    # 配额耗尽原样上抛：定向修复本就是"改这一处、其余不动"，换个模型重画整张
    # 反而会把已确认的构图一起改掉——降级机制已整体取消，见 QuotaExhaustedError。
    # 唯一的例外是「同模型换传输通道」（网关 /images/edits 号池墙，见
    # frame_generator.CHAT_TRANSPORT）：模型不变所以构图不会被重画，但请求 2K/4K 时
    # 那条通道只给 1K，得跟着改 manifest 上的留痕，不能让修复过的帧看着像全分辨率帧。
    transport = _generate_image_edit(config, new_prompt, target_path, target_path,
                                     control_prompt=IMG2IMG_CONTROL_PROMPT)
    if transport == CHAT_TRANSPORT and on_progress:
        on_progress('transport_fallback', {
            'sequence': sequence, 'transport': transport,
            'degraded': not _chat_transport_is_full_quality(config),
            'message': f"IMG {sequence:03d} {chat_transport_note(config)}",
        })

    project_dir = _get_project_dir(title)
    with manifest_lock(project_dir):
        manifest = read_manifest(project_dir)
        if manifest:
            for frame in manifest.get('frames', []):
                if frame.get('sequence') == sequence:
                    frame['prompt'] = new_prompt
                    frame['model'] = model
                    frame['retry_count'] = frame.get('retry_count', 0) + 1
                    frame['quality_gate'] = 'pending_manual_review'
                    frame['vlm_qa_reason'] = None
                    if transport == CHAT_TRANSPORT:
                        frame['transport'] = transport
                        frame['actual_pixels'] = _measure_image_pixels(target_path)
                        frame.pop('degraded_reason', None)
                        if not _chat_transport_is_full_quality(config):
                            frame['degraded_reason'] = chat_transport_note(config)
                    else:
                        # /images/edits 重渲过 = 之前的通道留痕已经过期，清掉才不误导
                        frame.pop('transport', None)
                        frame.pop('degraded_reason', None)
                        frame.pop('actual_pixels', None)
                    break
            # 与所有其它渲染路径同一个收尾（generate_frame_sequence 内部也调它）：
            # 这一帧的画面已经换了，其后各帧仍派生自旧图 → stale_lineage；相邻拍的
            # 一致性审查结论作废；已合并成片与视频清单作废。此前这条通道自己开锁写
            # manifest、绕过了整个收尾，于是「重试首帧」会标记下游、「修复首帧」不会，
            # 同一件事两种结果；成片也会留在清单里，看着像还对得上。
            update_manifest_stale_status(manifest, project_dir,
                                         regenerated_sequences=[sequence], finalize=True)
            write_manifest(project_dir, manifest)

    if on_progress:
        entry = _frame_manifest_entry(project_dir, sequence) or {}
        on_progress('frame', {'current': 1, 'total': 1, 'frame': entry})
    return {'sequence': sequence, 'image_path': target_path, 'project_dir': project_dir}


FIX_SNAPSHOT_DIR = '.frame_fixes'


def _fix_snapshot_dir(project_dir, sequence):
    return os.path.join(project_dir, FIX_SNAPSHOT_DIR, f'{sequence:03d}')


def save_fix_snapshot(project_dir, title, sequence, entry, image_item, video_beat, video_item):
    """修复前把"这一帧此刻的样子"整份存下来，供 undo_frame_fix 原样放回。

    修复是**覆盖写同一个文件**（img_00N.webp），旧图此前不留档：改坏了只能盲重渲
    碰运气，也没法拿前后两张对比着看"到底是改好了还是改坏了"。删除整拍早就有
    .deleted_slots 快照这套东西（见 server.py 的 /api/restore_slot），修复没有。

    存的是三样：帧图本体、该帧的 manifest 条目（问题描述、结构化违规、门禁结论都在
    里面）、以及提示词里这一帧与它前一拍视频的正文——修复会同时改写这两段文本，
    只把图片放回来而提示词留在改写后的版本，下一次重渲又会渲回"修复后"的样子。

    只保留最近一次：一帧连修三轮之后，人想回到的是"上一版"，不是三轮之前的考古现场。
    返回落盘的快照元数据（含 'at' 时间戳）。"""
    snap_dir = _fix_snapshot_dir(project_dir, sequence)
    shutil.rmtree(snap_dir, ignore_errors=True)
    os.makedirs(snap_dir, exist_ok=True)

    frame_path = _frame_path(title, sequence)
    if os.path.exists(frame_path):
        shutil.copyfile(frame_path, os.path.join(snap_dir, os.path.basename(frame_path)))
    meta = {
        'sequence': sequence,
        'at': datetime.now().isoformat(timespec='seconds'),
        # url 由渲染路径现算，与快照无关；fix_backup 指向的正是这个即将被覆盖的
        # 快照目录，一起存回去会让撤销之后的条目宣称"还有一版可退"（其实没有了）
        'frame': {k: v for k, v in (entry or {}).items() if k not in ('url', 'fix_backup')},
        'image': dict(image_item or {}),
        'video_beat': video_beat if video_item is not None else None,
        'video': dict(video_item or {}) if video_item is not None else None,
    }
    with open(os.path.join(snap_dir, 'snapshot.json'), 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    return meta


def drop_fix_snapshots(project_dir, manifest=None, sequences=None):
    """丢掉修复快照与帧条目上的 fix_backup 记号。sequences=None ＝全部丢掉。

    **槽位重新编号之后必须调用**（删除整拍、撤销删除、手动上传覆盖）：快照目录按
    槽位号存（.frame_fixes/005/），编号一变，`fix_backup` 记号跟着 manifest 条目
    前移到 004，而 .frame_fixes/004 里躺的是另一帧的旧图——点一下「撤销修复」就会
    把张冠李戴的画面退回到这一格。宁可丢掉可撤销性，也不能撤销出错的图。

    manifest 给了就就地摘掉记号（调用方负责写回）；没给就只清目录。"""
    root = os.path.join(project_dir, FIX_SNAPSHOT_DIR)
    if sequences is None:
        shutil.rmtree(root, ignore_errors=True)
    else:
        for seq in sequences:
            shutil.rmtree(_fix_snapshot_dir(project_dir, seq), ignore_errors=True)
    if not isinstance(manifest, dict):
        return
    wanted = None if sequences is None else {int(s) for s in sequences}
    for frame in manifest.get('frames') or []:
        if wanted is None or frame.get('sequence') in wanted:
            frame.pop('fix_backup', None)


def _mark_fix_backup(project_dir, sequence, snapshot_at, reason):
    """在帧条目上留一枚"这一帧有可退回的上一版"的记号，供前端画「撤销修复」按钮。

    必须在重渲之后写：重渲会整体改写（generate_frame_sequence）或覆盖若干字段
    （_fix_frame_via_image_edit）这条 manifest 条目，写在前面会被冲掉。"""
    with manifest_lock(project_dir):
        manifest = read_manifest(project_dir)
        if not manifest:
            return
        for frame in manifest.get('frames', []):
            if frame.get('sequence') == sequence:
                frame['fix_backup'] = {'at': snapshot_at, 'reason': reason}
                break
        write_manifest(project_dir, manifest)


def _read_fix_snapshot(project_dir, sequence):
    path = os.path.join(_fix_snapshot_dir(project_dir, sequence), 'snapshot.json')
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def undo_frame_fix(title, sequence, prompt_block):
    """撤销这一帧最近一次定向修复：把快照里的帧图、manifest 条目、两段提示词正文
    原样放回，回到修复前的状态（问题描述与结构化违规一并回来，可以重新修一次）。

    只回滚这一帧涉及的槽位，**不整体还原 prompt_block**：修完 003 又修了 005 之后
    撤销 003，整体还原会把 005 的修复一起吞掉。

    与其它写帧路径共用同一个收尾（update_manifest_stale_status finalize）：画面又
    变了一次，下游帧的血统标记、相邻拍的审查结论、已合并成片都要跟着作废。

    返回 {'prompt_block': ..., 'frame': ..., 'at': 快照时间}。没有快照时报错——
    没有可回退的版本时静默"成功"比报错更糟。"""
    project_dir = _get_project_dir(title)
    snap = _read_fix_snapshot(project_dir, sequence)
    if not snap:
        raise RuntimeError(f'IMG {sequence:03d} 没有可撤销的修复记录'
                           f'（只保留最近一次修复的快照，撤销过一次后就没有了）')

    snap_dir = _fix_snapshot_dir(project_dir, sequence)
    frame_path = _frame_path(title, sequence)
    saved_image = os.path.join(snap_dir, os.path.basename(frame_path))
    if os.path.exists(saved_image):
        os.makedirs(os.path.dirname(frame_path), exist_ok=True)
        shutil.copyfile(saved_image, frame_path)

    # 提示词：只换回这一帧自己的正文，以及修复当时被一起改写的那条视频过渡
    images, videos = _parse_prompt_slots(prompt_block)
    if snap.get('image') and sequence in images:
        images[sequence] = dict(snap['image'])
    video_beat = snap.get('video_beat')
    if snap.get('video') and video_beat in videos:
        videos[video_beat] = dict(snap['video'])
    restored_block = _format_prompt_block(images, videos)

    saved_entry = snap.get('frame') or {}
    with manifest_lock(project_dir):
        manifest = read_manifest(project_dir)
        if manifest:
            for idx, frame in enumerate(manifest.get('frames', [])):
                if frame.get('sequence') != sequence:
                    continue
                # url 是渲染路径算出来的、与快照无关，原样留着；其余字段整条换回去，
                # 免得"修复时新加的字段"（transport/degraded_reason…）留在原地误导人
                restored = dict(saved_entry)
                if frame.get('url'):
                    restored['url'] = frame['url']
                manifest['frames'][idx] = restored
                break
            update_manifest_stale_status(manifest, project_dir,
                                         regenerated_sequences=[sequence], finalize=True)
            write_manifest(project_dir, manifest)

    shutil.rmtree(snap_dir, ignore_errors=True)
    return {'prompt_block': restored_block,
            'frame': _frame_manifest_entry(project_dir, sequence) or {},
            'at': snap.get('at')}


def fix_frame_issue(config, title, prompt_block, sequence, on_progress=None, manual_reason=None):
    """人工确认修复流程的落地点：`_sequence_consistency_review` 只标记问题、不
    自动改写重渲，人工在帧网格看过 vlm_qa_reason 后点击「修复此帧问题」才会真正
    触发这里——针对被标记的具体问题做定向提示词优化，再重渲。

    问题来源有两条，可并存也可单独成立：机器的一致性审查判定（vlm_qa_reason）与
    人工主动描述（manual_issue，见 set_manual_frame_issue）。manual_reason 是人在
    修复对话框里现场写的描述——先落盘再修，中途失败描述也不会丢。审查没标记过、
    人也没描述过的帧仍然报错：修复入口不该对着一张"看起来没问题"的帧空转一次
    VLM 改写。

    非首帧沿用一致性审查的定向重写（fix_beat_from_sequence_review，同时改前一拍
    的视频过渡文本，保持"过渡描述"与"到达画面"一致）；首帧没有前置视频过渡，
    走单提示词反馈重写（fix_image_prompt_with_vlm_feedback）。
    重渲一律走图生图——首帧走 _fix_frame_via_image_edit 的自编辑通道，非首帧走
    generate_frame_sequence(target_sequences=[sequence])（seq>1 天然图生图链式
    编辑，不需要特殊处理）。

    重渲之后会对着新画面把这几条问题逐条再验一遍（_reverify_frame_issues），结果放在
    返回值的 'reverify' 里——修复流程此前是开环的，重渲完没人回答"到底修好没有"。

    返回 {'prompt_block': ..., 'reason': ..., 'reverify': {...}|None}。"""
    project_dir = _get_project_dir(title)
    manual_reason = (manual_reason or '').strip()
    if manual_reason:
        set_manual_frame_issue(title, sequence, manual_reason)
    entry = _frame_manifest_entry(project_dir, sequence) or {}
    manual_issue = (entry.get('manual_issue') or '').strip()
    auto_reason = (entry.get('vlm_qa_reason') or '').strip()
    # 人工描述排在前面：人看的是真实画面、指的是具体哪里不对，比机器判定更该被
    # 优先满足；两者重复时只留一份，别让改写模型对着同一句话改两遍。
    issues = [t for t in (manual_issue, auto_reason) if t]
    if len(issues) == 2 and issues[0] == issues[1]:
        issues = issues[:1]
    if not issues:
        raise RuntimeError(f'IMG {sequence:03d} 当前没有记录待修复的问题——'
                           f'请先在帧网格点「描述问题」写下这一帧哪里不对，或先运行一致性审查')
    reason = '；'.join(issues)

    # 修复后复审要用的结构化问题清单，必须在重渲前取出：重渲会整体改写这一帧的
    # manifest 条目，review_issues 到那时已经没了。审查没留下结构化记录时（旧 manifest
    # 或人工描述）退化成一条本地问题，复核只看这一帧自己。
    recorded_issues = [dict(i) for i in (entry.get('review_issues') or []) if isinstance(i, dict)]
    if not recorded_issues:
        recorded_issues = [{'text': t, 'layer': 'manual' if t == manual_issue else 'local',
                            'beat': sequence - 1, 'frames': [sequence]} for t in issues]

    images, videos = _parse_prompt_slots(prompt_block)
    if sequence not in images:
        raise RuntimeError(f'提示词中未找到第 {sequence} 帧')
    image_item = images[sequence]
    image_body = image_item['body'] if isinstance(image_item, dict) else image_item
    image_meta = image_item.get('meta', '') if isinstance(image_item, dict) else ''

    video_beat = sequence - 1
    video_item = videos.get(video_beat) if video_beat >= 1 else None

    # 动手之前先把"修复前的样子"整份存下来（帧图 + manifest 条目 + 这两段提示词），
    # 修坏了可以一键退回去（undo_frame_fix）。修复是覆盖写同一个文件，不存就没了。
    snapshot = save_fix_snapshot(project_dir, title, sequence, entry, image_item,
                                 video_beat, video_item)

    if on_progress:
        on_progress('frame_issue_fix_start', {
            'sequence': sequence, 'reason': reason,
            'message': f"🔧 正在依据问题描述优化 IMG {sequence:03d} 的提示词（{reason}）…",
        })

    if video_item is not None:
        video_body = video_item['body'] if isinstance(video_item, dict) else video_item
        video_meta = video_item.get('meta', '') if isinstance(video_item, dict) else ''
        new_video_body, new_image_body = fix_beat_from_sequence_review(
            config, video_body, image_body, issues, video_meta=video_meta)
        if new_video_body != video_body:
            videos[video_beat] = {'body': new_video_body, 'meta': video_meta}
    else:
        new_image_body = fix_image_prompt_with_vlm_feedback(config, image_body, reason)

    _family = image_space_family(videos, sequence)
    new_image_body = clean_prompt_text(new_image_body)
    new_image_body = fix_image_clean_frame_proactive(new_image_body)
    new_image_body = fix_horizon_line(new_image_body, family=_family)
    new_image_body = fix_camera_contradictions(new_image_body)
    images[sequence] = {'body': new_image_body, 'meta': image_meta}
    new_prompt_block = _format_prompt_block(images, videos)

    if on_progress:
        on_progress('frame_issue_fix_render', {
            'sequence': sequence,
            'message': f"🎨 正在以图生图方式重渲 IMG {sequence:03d}…",
        })

    if sequence == 1:
        _fix_frame_via_image_edit(config, title, sequence, new_image_body, on_progress=on_progress)
    else:
        generate_frame_sequence(config, title, new_prompt_block, on_progress=on_progress,
                                target_sequences=[sequence])

    # 重渲成功才清人工描述：中途抛错时描述必须原样留在 manifest 上，否则人得重新
    # 把问题再描述一遍。
    if manual_issue:
        _clear_manual_frame_issue(project_dir, sequence)

    # 重渲之后才盖"可撤销"的记号：重渲会整体改写这条 manifest 条目，写在前面会被冲掉。
    # 中途抛错时也不该有这枚记号——那次修复没落地，没有"新版本"需要退回。
    _mark_fix_backup(project_dir, sequence, snapshot.get('at'), reason)

    verify = _reverify_frame_issues(config, title, sequence, recorded_issues, on_progress=on_progress)
    return {'prompt_block': new_prompt_block, 'reason': reason, 'reverify': verify,
            'undoable': True}


def _reverify_frame_issues(config, title, sequence, recorded_issues, on_progress=None):
    """修复重渲之后，对着新画面把刚才那几条问题逐条再验一遍，回答"到底修好没有"。

    此前的修复流程是开环的：重渲完就把 gate 设回 pending_manual_review 走人，那条问题
    是否真的解决了没有任何人回答——用户只能自己盯着看，或者把整套序列的审查重跑一遍
    （十几次多模态调用）。有了结构化的 review_issues（每条带 layer/frames/text），这里
    只需对每条问题各跑一次窄口径复核（_verify_review_violation），几秒钟就能给出结论。

    仍然存在的问题写回 manifest（quality_gate 保持 sequence_review_flagged 并只留未解决
    的那几条）；全部解决则落 pending_manual_review 等人最终确认。复核本身没跑成的
    （返回 None）一律按"仍存在"保守处理，不谎报修复成功。
    返回 {'resolved': [...], 'remaining': [...]}；没有结构化问题可验时返回 None。"""
    issues = [i for i in (recorded_issues or []) if isinstance(i, dict) and i.get('text')]
    if not issues:
        return None
    project_dir = _get_project_dir(title)

    def _paths(issue):
        seqs = issue.get('frames') or [sequence]
        out = [_frame_path(title, s) for s in seqs]
        return [p for p in out if os.path.exists(p)] or [_frame_path(title, sequence)]

    if on_progress:
        on_progress('frame_issue_reverify', {
            'sequence': sequence, 'count': len(issues),
            'message': f"🔎 正在对着新画面复核 IMG {sequence:03d} 的 {len(issues)} 条问题是否已解决…",
        })

    resolved, remaining = [], []
    for issue in issues:
        verdict = _verify_review_violation(config, issue['text'], _paths(issue))
        # False = 复核明确说"这个问题在新图里不存在了"＝已解决；
        # True/None = 仍存在或没验成，都按未解决处理（宁可让人再看一眼，不谎报成功）
        (resolved if verdict is False else remaining).append(issue)

    if remaining:
        _set_manifest_quality_gate(
            project_dir, sequence, 'sequence_review_flagged',
            '；'.join(i['text'] for i in remaining), review_issues=remaining)
    else:
        _set_manifest_quality_gate(project_dir, sequence, 'pending_manual_review', None,
                                   review_issues=None)

    if on_progress:
        if remaining:
            msg = (f"⚠️ IMG {sequence:03d}：{len(resolved)} 条已解决，仍有 {len(remaining)} 条存在"
                   f"（{'；'.join(i['text'] for i in remaining)}），可再修一次或人工确认")
        else:
            msg = f"✅ IMG {sequence:03d}：修复前记录的 {len(resolved)} 条问题在新画面中均已消失"
        on_progress('frame_issue_reverify_result', {
            'sequence': sequence,
            'resolved': [i['text'] for i in resolved],
            'remaining': [i['text'] for i in remaining],
            'message': msg,
        })
    return {'resolved': [i['text'] for i in resolved], 'remaining': [i['text'] for i in remaining]}


def render_frames_for_task(config, title, prompt_block, on_progress=None):
    """/api/generate_frames 整单渲染的编排入口：把缺失的帧一次渲完，不做视频——保持
    该端点"只渲帧"的语义。

    渲染期不跑任何审查（2026-08-05：锚帧验收门、检查点现实同步、链尾漂移回望一并
    移除）。整套序列一致性审查改由用户在帧网格上手动触发，见
    run_sequence_consistency_review。
    返回 manifest（与 generate_frame_sequence 同约定，带 manifest/project_dir 瞬态键）。"""
    project_dir = _get_project_dir(title)
    generate_frame_sequence(config, title, prompt_block, on_progress=on_progress,
                            target_sequences=None)
    manifest = read_manifest(project_dir) or {}
    manifest_path = os.path.join(project_dir, 'manifest.json')
    manifest['manifest'] = '/' + os.path.relpath(
        manifest_path, os.path.dirname(os.path.abspath(__file__))).replace('\\', '/')
    manifest['project_dir'] = os.path.abspath(project_dir)
    return manifest


def _render_videos_with_recovery(config, title, prompt_block, on_progress=None,
                                 project_dir=None):
    """Render all videos, then run one autonomous retry pass over any slot that came
    back rejected/blocked (e.g. a failed Google FX anchor-match) instead of leaving it
    for a human to notice and re-trigger manually."""
    video_result = generate_video_sequence(config, title, prompt_block, on_progress=on_progress)
    # 'skipped_cut'（旧单的硬切占位槽位，新单的 [CUT] 槽照常生成）是预期缺失，
    # 不进恢复重试轮；'skipped_bridge_hold'
    # 已停用（单一过门拍收编后不再有需要跳过的 HOLD 槽位），仅为兼容旧 manifest 保留
    failed_slots = [v['slot'] for v in video_result.get('videos', [])
                    if v.get('status') not in ('success', 'skipped_cut', 'skipped_bridge_hold')]
    if failed_slots:
        if on_progress:
            on_progress('video_retry_autonomous', {'slots': failed_slots})
        video_result = generate_video_sequence(
            config, title, prompt_block, on_progress=on_progress, target_slots=failed_slots,
        )
    return video_result


def run_autonomous_pipeline(config, dimensions, on_progress=None):
    """Runs the full staged pipeline autonomously, composing its own prompt text."""
    state = compose_anchor_and_packet(config, dimensions, on_progress=on_progress)
    title = state['title']
    # IMAGE 1 先单独渲一张：下面的 refine_packet_from_accepted_anchor 要对着这张真实
    # 画面修正 Drift Lock 数据包，剩余各拍的提示词都由修正后的包生成。渲完不做任何
    # 判定——渲染期审查已整体移除。
    rendered = render_single_frame(
        config, title, 1, state['image_1_prompt'], on_progress=on_progress,
    )

    if on_progress:
        on_progress('packet_refine_start', {'message': '正在依据已确认的首帧修正 Drift Lock 数据包...'})
    state['packet'] = refine_packet_from_accepted_anchor(
        config, rendered['image_path'], state['packet'], state.get('parsed_brief'))
    # Persist the auditable world/topology ledger separately from prose prompts.  Unknown manifest
    # keys are intentionally preserved by both render backends, so later per-frame writes keep it.
    with manifest_lock(rendered['project_dir']):
        _manifest = read_manifest(rendered['project_dir']) or {'title': title, 'frames': []}
        _manifest['spatial_contract'] = {
            key: state['packet'].get(key) or state.get('parsed_brief', {}).get(key)
            for key in ('world_lock', 'carrier_envelope', 'entrance_topology', 'space_graph',
                        'camera_palette')
        }
        _manifest['spatial_beats'] = [
            {key: beat.get(key) for key in (
                'index', 'space_id', 'transition_stage', 'camera_family', 'reveal_scope',
                'light_source_state', 'operation', 'package_operations', 'milestone_name',
                'before_state', 'after_state', 'preserve_state', 'changed_grid_cells',
                'persistent_traces', 'hard_cut', 'bridge_stage', 'turn_direction')}
            for beat in state.get('beat_ladder', []) if isinstance(beat, dict)
        ]
        write_manifest(rendered['project_dir'], _manifest)
    if on_progress:
        on_progress('packet_refined', {'message': 'Drift Lock 数据包已依据实际渲染结果修正。'})

    project_dir = rendered['project_dir']
    prompt_block = compose_remaining_beats(config, state, on_progress=on_progress)
    # 卡片工序交付总账落盘：帧渲染完之后用户手动触发的一致性审查在另一个进程生命周期里，
    # config 上那份到不了那边（见 persist_outline_delivery_ledger）
    persist_outline_delivery_ledger(
        project_dir, (config or {}).get('_outline_delivery_ledger'), title=title)
    generate_frame_sequence(config, title, prompt_block, on_progress=on_progress,
                            target_sequences=None)
    video_result = _render_videos_with_recovery(config, title, prompt_block, on_progress=on_progress,
                                                project_dir=project_dir)

    return {
        'title': title,
        'status': 'completed',
        'prompt_block': prompt_block,
        'prompt_slots': prompt_slots_list(prompt_block),
        'project_dir': project_dir,
        'videos': video_result,
    }


def run_staged_frame_rendering(config, title, prompt_block, on_progress=None):
    """Renders an ALREADY-composed prompt_block, without re-deriving any prompt text,
    then generates video. Frames already on disk are reused (generate_frame_sequence's
    own resume semantics), so an agent that rendered IMAGE 1 inline via
    /api/render_anchor before composing the rest does not pay for it twice.
    Returns a result dict with status='completed'."""
    images, _videos = _parse_prompt_slots(prompt_block)
    if 1 not in images:
        raise RuntimeError('未在 prompt_block 中找到 图片 1: 提示词，无法分步渲染')

    project_dir = _get_project_dir(title)
    generate_frame_sequence(config, title, prompt_block, on_progress=on_progress,
                            target_sequences=None)
    video_result = _render_videos_with_recovery(config, title, prompt_block, on_progress=on_progress,
                                                project_dir=project_dir)

    return {
        'title': title,
        'status': 'completed',
        'prompt_block': prompt_block,
        'prompt_slots': prompt_slots_list(prompt_block),
        'project_dir': project_dir,
        'videos': video_result,
    }
