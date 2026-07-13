"""Autonomous staged pipelines for the restoration-prompt-composer production app.

Three entry points, sharing the same render/gate/recovery machinery:

- render_and_gate_single_frame: render ONE frame and run it through the Anchor
  Acceptance Gate, synchronously, returning the verdict directly (no task_id/polling).
  This is what makes staging real for a CONVERSATIONAL skill invocation: an agent
  following SKILL.md's Steps 1-11 directly in chat calls this (via /api/render_anchor,
  see server.py) mid-turn, right after composing IMAGE 1's prompt and before composing
  anything else, and only continues once it gets a real pass/fail back.

- run_autonomous_pipeline: dimensions in, everything out. Composes IMAGE 1's prompt
  itself (compose_anchor_and_packet), gates it via render_and_gate_single_frame, refines
  the Drift Lock packet against the accepted render, then composes and renders the rest
  (compose_remaining_beats). Used by server.py's /api/auto_run — the GUI/API-driven,
  dimensions-first path.

- run_staged_frame_rendering: an ALREADY-composed prompt_block in, staged rendering
  out. For the case where an agent already wrote the full IMAGE/VIDEO prompt text
  (typically AFTER already gating IMAGE 1 itself via render_and_gate_single_frame mid-
  turn) and now wants the rest rendered. If IMAGE 1 is already 'auto_approved' in
  manifest.json AND the recorded anchor prompt fingerprint matches this prompt_block's
  IMAGE 1, this skips re-gating it — otherwise it gates it here too, so calling this
  endpoint alone (without a prior render_and_gate_single_frame call) still stages
  correctly and a stale manifest from an earlier same-titled run cannot bypass the
  gate. Used by server.py's /api/render_staged, which scripts/generate_frames.py
  calls instead of the old one-shot /api/generate_frames.

All three paths end with (or lead into) the same autonomous recovery passes over any
leftover 'vlm_qa_failed' frame and any rejected/blocked video clip, so nothing ever
dead-ends in a manual-review state.
"""
import os
import json
import time
import hashlib

from server_common import _get_project_dir, read_manifest, write_manifest, manifest_lock, qa_gate_level
from prompt_pipeline import (
    compose_anchor_and_packet,
    compose_remaining_beats,
    check_anchor_frame_compliance,
    refine_packet_from_accepted_anchor,
    fix_image_prompt_with_vlm_feedback,
    run_frame_qa_check,
    run_chain_tail_drift_check,
    family_anchor_seq,
    resolve_family_anchor,
    extract_locked_anchor_stanza,
    replace_locked_anchor_stanza,
    recalibrate_anchor_stanza,
    is_judge_unavailable_verdict,
    image_space_family,
    is_skipped_verdict,
    clean_prompt_text,
    fix_image_clean_frame_proactive,
    fix_horizon_line,
    fix_camera_contradictions,
    prompt_slots_list,
    _format_prompt_block,
    _parse_prompt_slots,
)
from frame_generator import generate_frame_sequence
from video_generator import generate_video_sequence

_MAX_ANCHOR_ATTEMPTS = 3
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


def _prompt_fingerprint(prompt):
    """空白归一化后的提示词 sha256。锚点门通过时记入 manifest，复用旧判定前必须比对：
    同名项目复跑/换了首帧提示词时，陈旧的 auto_approved 不得再绕过首帧门。"""
    normalized = ' '.join((prompt or '').split())
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest()


def _set_manifest_quality_gate(project_dir, sequence, quality_gate, reason=None, prompt_fingerprint=None):
    """Overwrite one frame's recorded quality_gate/vlm_qa_reason in manifest.json.
    Needed because generate_frame_sequence's own per-frame QA never produces a real
    verdict for frame 1 (it has no prior frame to compare motion against), so the
    Anchor Acceptance Gate's verdict has to be written back out-of-band."""
    # read_manifest/write_manifest：同一把项目级锁 + 原子替换（读改写不再互相覆盖）
    with manifest_lock(project_dir):
        manifest = read_manifest(project_dir)
        if not manifest:
            return
        for frame in manifest.get('frames', []):
            if frame.get('sequence') == sequence:
                frame['quality_gate'] = quality_gate
                frame['vlm_qa_reason'] = reason
                if prompt_fingerprint is not None:
                    frame['anchor_prompt_sha256'] = prompt_fingerprint
                break
        write_manifest(project_dir, manifest)


def _retry_frame_until_pass(config, title, sequence, images, videos, judge, on_progress=None,
                             max_attempts=_MAX_ANCHOR_ATTEMPTS):
    """Render `sequence` from `images`/`videos` (same shape _parse_prompt_slots returns),
    ask `judge(image_path, prompt) -> (passed, reason)`, and on failure correct that
    slot's prompt via fix_image_prompt_with_vlm_feedback and re-render. Mutates
    images[sequence] in place with the final prompt actually used. Shared by the frame-1
    Anchor Acceptance Gate and the post-render autonomous recovery pass over any
    leftover 'vlm_qa_failed' frame.
    Returns (passed: bool, reason: str)."""
    item = images[sequence]
    prompt = item['body'] if isinstance(item, dict) else item
    meta = item.get('meta', '') if isinstance(item, dict) else ''
    reason = None
    for attempt in range(1, max_attempts + 1):
        images[sequence] = {'body': prompt, 'meta': meta}
        prompt_block = _format_prompt_block(images, videos)
        generate_frame_sequence(config, title, prompt_block, on_progress=on_progress, target_sequences=[sequence])
        passed, reason = judge(_frame_path(title, sequence), prompt)
        if on_progress:
            on_progress('anchor_check', {'sequence': sequence, 'attempt': attempt, 'passed': passed, 'reason': reason})
        if passed:
            return True, reason
        if attempt < max_attempts:
            if on_progress:
                on_progress('anchor_retry', {'sequence': sequence, 'attempt': attempt, 'reason': reason})
            prompt = fix_image_prompt_with_vlm_feedback(config, prompt, reason)
            # 改写后重套确定性修复（与 frame_generator 两条 VLM 改写路径同款、镜头族感知）：
            # 裸用改写产物曾让干净帧被回填 horizon、静态相机声明被当"运动矛盾"删除。
            _family = image_space_family(videos, sequence)
            prompt = clean_prompt_text(prompt)
            prompt = fix_image_clean_frame_proactive(prompt)
            prompt = fix_horizon_line(prompt, family=_family)
            prompt = fix_camera_contradictions(prompt, is_bridge=(_family == 'sill'))
    return False, reason


def render_and_gate_single_frame(config, title, sequence, prompt, meta='', judge=None, on_progress=None):
    """Render exactly one frame and run it through an acceptance gate synchronously,
    returning the final verdict directly (no task_id/polling) so a caller — including a
    conversational agent mid-turn — can decide what to do next before composing anything
    else. Defaults to the Anchor Acceptance Gate judged purely from the prompt text
    (no separate Drift Lock packet available); callers with a real packet/parsed_brief
    (run_autonomous_pipeline) pass their own `judge`.
    Returns {'status': 'auto_approved'|'auto_approved_degraded'|'needs_human_review',
    'reason', 'prompt', 'image_path', 'project_dir'}. auto_approved_degraded 表示判定
    服务异常被 fail-open 放行——帧没有经过真实核验，只是没被拦。"""
    if judge is None:
        def judge(image_path, current_prompt):
            return check_anchor_frame_compliance(config, image_path, current_prompt, {}, {})

    images = {sequence: {'body': prompt, 'meta': meta}}
    passed, reason = _retry_frame_until_pass(config, title, sequence, images, {}, judge, on_progress=on_progress)
    project_dir = _get_project_dir(title)
    if passed:
        status = 'auto_approved_degraded' if is_skipped_verdict(reason) else 'auto_approved'
    else:
        status = 'needs_human_review'
    _set_manifest_quality_gate(project_dir, sequence, status, reason,
                               prompt_fingerprint=_prompt_fingerprint(images[sequence]['body']))
    return {
        'status': status,
        'reason': reason,
        'prompt': images[sequence]['body'],
        'image_path': _frame_path(title, sequence),
        'project_dir': project_dir,
    }


def _recover_failed_frames(config, title, prompt_block, project_dir, on_progress=None):
    """Autonomous recovery pass: retry any frame still stuck at 'vlm_qa_failed' after
    the main render pass's own retries, using the same judge-and-fix loop as the Anchor
    Acceptance Gate but checking frame-to-frame motion continuity (run_vlm_qa_check)
    instead of tone/rule compliance. Returns the prompt_block, re-formatted only if a
    recovered prompt actually changed."""
    images, videos = _parse_prompt_slots(prompt_block)
    manifest_path = os.path.join(project_dir, 'manifest.json')
    if not os.path.exists(manifest_path):
        return prompt_block
    with open(manifest_path, 'r', encoding='utf-8') as f:
        manifest = json.load(f)
    failed_seqs = [f['sequence'] for f in manifest.get('frames', []) if f.get('quality_gate') == 'vlm_qa_failed']
    for seq in failed_seqs:
        video_item = videos.get(seq - 1)
        video_prompt = video_item['body'] if isinstance(video_item, dict) else (video_item or '')
        item = images.get(seq) or {}
        # Per the delivery contract (SKILL.md Step 10), only VIDEO slots ever carry the
        # [BRIDGE] tag -- IMAGE slots never do. The incoming transition's own tag (VIDEO
        # seq-1) is what actually determines whether IMAGE seq is a bridge frame; the
        # image's own meta is kept as a fallback for the server's internal beat_ladder
        # composition path, which also stamps images directly.
        is_bridge = (
            'BRIDGE' in (item.get('meta', '') if isinstance(item, dict) else '').upper()
            or 'BRIDGE' in (video_item.get('meta', '') if isinstance(video_item, dict) else '').upper()
        )

        def frame_judge(image_path, _prompt, _seq=seq, _video_prompt=video_prompt, _is_bridge=is_bridge):
            if not _video_prompt:
                return True, 'no video prompt to check against'
            prev_path = _frame_path(title, _seq - 1)
            # Drift-check against the CURRENT shot family's anchor, not always IMAGE 1 --
            # once a threshold crossing has happened, IMAGE 1's exterior family is no longer
            # a meaningful comparison for interior frames. None means "this frame IS the
            # family anchor itself, nothing to compare against yet" (mirrors the seq<=2 skip).
            # resolve_family_anchor：链中检查点重锚定过的族以新基线为准。
            anchor_seq = resolve_family_anchor(config, videos, _seq)
            anchor_path = _frame_path(title, anchor_seq) if anchor_seq != _seq else None
            return run_frame_qa_check(config, anchor_path, prev_path, image_path, _video_prompt, _seq, is_bridge=_is_bridge, anchor_seq=anchor_seq)

        passed, reason = _retry_frame_until_pass(
            config, title, seq, images, videos, frame_judge,
            on_progress=on_progress, max_attempts=_MAX_RECOVERY_ATTEMPTS,
        )
        if passed:
            status = 'auto_approved_degraded' if is_skipped_verdict(reason) else 'auto_approved'
        else:
            status = 'vlm_qa_failed'
        _set_manifest_quality_gate(project_dir, seq, status, reason)
    if failed_seqs:
        prompt_block = _format_prompt_block(images, videos)
    return prompt_block


def _supervised_mode(config):
    """关键点监修模式开关（config['supervisedMode']，默认关闭=全自动）。开启后流水线
    在判定器最弱的关键点异步暂停等人工确认：首帧过门后、镜头族交接锚点帧渲染后、
    降级/未过检帧汇总处。"""
    return bool(isinstance(config, dict) and config.get('supervisedMode'))


def _review_timeout(config):
    """监修暂停的超时秒数（超时自动采用并继续，挂机不至于永久卡住）。默认 600。"""
    try:
        return max(30, int(config.get('reviewTimeoutSeconds', 600)))
    except (TypeError, ValueError):
        return 600


def _review_image_url(image_path):
    try:
        rel = os.path.relpath(image_path, os.path.dirname(os.path.abspath(__file__)))
        return '/' + rel.replace('\\', '/')
    except Exception:
        return None


def _await_frame_review(config, title, seq, image_path, context, on_progress):
    """监修暂停点：广播 review_pause 后轮询用户决策直到 采用/重渲/超时/取消。

    决策经任务带内通道回传：前端 POST /api/frame-review 把决策写进任务记录，
    worker 的 progress_cb 对 'review_poll' 探测返回并清除它（与 'cancel_check'
    同款、不入事件史）。超时自动采用——监修是加严不是门禁，人不在场时链条
    照常推进，超时事实留在 review_resume 事件里。
    返回 'adopt' | 'rerender'；用户取消任务时抛 ConnectionError（与渲染层一致）。"""
    if on_progress is None:
        return 'adopt'
    timeout = _review_timeout(config)
    seq_label = f"IMG {seq:03d}" if isinstance(seq, int) else '本阶段结果'
    on_progress('review_pause', {
        'sequence': seq,
        'image_url': _review_image_url(image_path) if image_path else None,
        'context': context,
        'timeout_seconds': timeout,
        'message': f"⏸️ 监修暂停：请确认 {seq_label}（{context}）——采用或重渲，{timeout}s 无操作自动采用",
    })
    started = time.time()
    while True:
        if on_progress('cancel_check', None):
            raise ConnectionError('用户取消了生成任务')
        decision = on_progress('review_poll', {'sequence': seq})
        if decision in ('adopt', 'rerender'):
            on_progress('review_resume', {
                'sequence': seq, 'decision': decision,
                'message': f"▶️ 监修继续：{seq_label} {'已采用' if decision == 'adopt' else '将重渲'}",
            })
            return decision
        if time.time() - started > timeout:
            on_progress('review_resume', {
                'sequence': seq, 'decision': 'adopt', 'timeout': True,
                'message': f"▶️ 监修超时（{timeout}s 无操作），自动采用 {seq_label} 并继续",
            })
            return 'adopt'
        time.sleep(2)


_MAX_REVIEW_RERENDERS = 3


def _supervised_degraded_summary(config, title, project_dir, on_progress=None):
    """监修模式的收尾暂停点：帧渲染+恢复轮全部结束后，若仍有降级/未过检帧
    （auto_approved_degraded=判定服务异常放行未核验、vlm_qa_failed=重试用尽仍未过），
    在烧视频额度之前暂停告知，让用户决定继续还是取消任务去手动重渲。
    只有"继续"一种决策（重渲入口在帧网格的单帧重试按钮里）。"""
    if not _supervised_mode(config):
        return
    manifest = read_manifest(project_dir) or {}
    bad = [f.get('sequence') for f in manifest.get('frames', [])
           if isinstance(f, dict) and f.get('quality_gate') in ('vlm_qa_failed', 'auto_approved_degraded')]
    if not bad:
        return
    seqs = ', '.join(f"IMG {s:03d}" for s in bad if isinstance(s, int))
    _await_frame_review(
        config, title, None, None,
        f"以下帧带降级/未过检标记：{seqs}。继续将开始生成视频；如需重渲请取消任务后在帧网格单独重试",
        on_progress,
    )


def _checkpoint_interval(config):
    """检查点间隔（每 K 帧做一次现实同步）。默认 5（与 FX ≤5 连续序号一批对齐）；
    config['realityCheckpointInterval'] 可覆盖，0/负值 = 关闭检查点机制。"""
    try:
        return int(config.get('realityCheckpointInterval', 5))
    except (TypeError, ValueError):
        return 5


def _segment_progress(on_progress, offset, grand_total, first_segment):
    """分段渲染的进度事件整流：generate_frame_sequence 每次调用都发自己的 'start' 和
    按段内计数的 'frame' 事件，不整流的话前端进度条每段都清零重来。只放行第一段的
    'start'（换成全局总数），'frame' 的 current 加上已完成偏移，所有 payload 的 total
    改写为全局总数。cancel_check 探测原样透传且返回值必须回传（取消依赖它）。"""
    if on_progress is None:
        return None

    def _cb(stage, details):
        if stage == 'start':
            if first_segment:
                return on_progress('start', {'total': grand_total})
            return None
        if isinstance(details, dict):
            d = dict(details)
            if 'total' in d:
                d['total'] = grand_total
            if stage == 'frame' and isinstance(d.get('current'), (int, float)):
                d['current'] = offset + d['current']
            return on_progress(stage, d)
        return on_progress(stage, details)
    return _cb


def _append_manifest_list(project_dir, key, entry):
    """向 manifest 的列表键追加一条记录（项目级锁 + 原子替换）。"""
    with manifest_lock(project_dir):
        manifest = read_manifest(project_dir)
        if manifest:
            manifest.setdefault(key, []).append(entry)
            write_manifest(project_dir, manifest)


def _checkpoint_reality_sync(config, title, images, videos, members, latest_seq,
                             project_dir, on_progress=None):
    """检查点现实同步（滚动现实校准 + 链中回望重锚定）。在一个镜头族的分段渲染
    间隙调用，latest_seq 是刚渲染完的段尾帧。两步：

    1. 链中回望：当前族锚→链中→latest 三帧比对累积漂移（与收尾的链尾回望同一判定）。
       检出真实漂移（排除判定服务异常的 fail-closed FAIL）时就地重锚定——把 latest
       立为该族新基线（config['_reanchors'] 带内通道 + manifest['reanchors'] 留痕），
       此后的逐帧漂移复查、收尾回望、下一次校准都以新基线为准。漂移无法廉价撤销，
       继续拿旧锚当基线只会连环误杀，向前重定基线让链条后半段自洽。

    2. 锚点句校准：合成期写进剩余提示词的 Locked anchors 句停留在 packet 的预想值
       （首帧 refine 之后再没对过账）。拿 latest 真实帧核对格位/画幅占比，有明确
       出入时把修正句整句替换进该族全部剩余提示词（fix_primary_landmarks 已保证
       同族锚点句全同，整句替换是确定性手术）。

    返回 True 若剩余提示词被改写（调用方需重排 prompt_block）。全程 fail-open，
    任何一步失败只跳过，不拦渲染。"""
    changed = False

    # ── 1. 链中回望 + 重锚定 ──
    head = resolve_family_anchor(config, videos, latest_seq)
    pool = [s for s in members
            if head <= s <= latest_seq and os.path.exists(_frame_path(title, s))]
    if len(pool) >= 3:
        mid = pool[len(pool) // 2]
        tail = pool[-1]
        if mid not in (pool[0], tail):
            passed, reason = run_chain_tail_drift_check(
                config,
                _frame_path(title, pool[0]), _frame_path(title, mid), _frame_path(title, tail),
                anchor_seq=pool[0], mid_seq=mid, tail_seq=tail,
                anchor_is_first_frame=(pool[0] == 1),
            )
            if on_progress:
                verdict = '通过' if passed else '检出累积漂移'
                detail = f"（{reason}）" if reason and reason != 'PASS' else ''
                on_progress('chain_drift_check', {
                    'family_anchor': pool[0], 'mid': mid, 'tail': tail,
                    'passed': bool(passed), 'reason': reason, 'checkpoint': True,
                    'message': f"链中回望 IMG {pool[0]:03d}→{mid:03d}→{tail:03d}：{verdict}{detail}",
                })
            if not passed and not is_judge_unavailable_verdict(reason):
                config.setdefault('_reanchors', []).append(int(tail))
                entry = {'family_anchor': pool[0], 'new_anchor': int(tail), 'reason': reason}
                _append_manifest_list(project_dir, 'reanchors', entry)
                if on_progress:
                    on_progress('reanchor', {
                        **entry,
                        'message': (f"检出链中累积漂移，已把 IMG {tail:03d} 立为该镜头族的"
                                    f"新锚点基线（后续漂移复查与回望以其为准）"),
                    })

    # ── 2. 锚点句滚动校准 ──
    remaining = [s for s in members if s > latest_seq]
    old_stanza = None
    for s in remaining:
        item = images.get(s)
        body = item['body'] if isinstance(item, dict) else (item or '')
        old_stanza = extract_locked_anchor_stanza(body)
        if old_stanza:
            break
    if not old_stanza:
        return changed
    new_stanza = recalibrate_anchor_stanza(config, _frame_path(title, latest_seq), old_stanza)
    if not new_stanza:
        return changed
    updated = 0
    for s in remaining:
        item = images.get(s)
        body = item['body'] if isinstance(item, dict) else (item or '')
        meta = item.get('meta', '') if isinstance(item, dict) else ''
        new_body, replaced = replace_locked_anchor_stanza(body, new_stanza)
        if replaced:
            images[s] = {'body': new_body, 'meta': meta}
            updated += 1
    if updated:
        changed = True
        _append_manifest_list(project_dir, 'anchor_recalibrations', {
            'grounded_on': int(latest_seq), 'updated_slots': updated,
            'from': old_stanza, 'to': new_stanza,
        })
        if on_progress:
            on_progress('anchor_recalibrated', {
                'grounded_on': int(latest_seq), 'updated_slots': updated, 'stanza': new_stanza,
                'message': (f"检查点校准：以 IMG {latest_seq:03d} 真实画面为准，"
                            f"修正了剩余 {updated} 个提示词的锁定锚点句"),
            })
    return changed


def _render_frames_with_checkpoints(config, title, prompt_block, project_dir, on_progress=None):
    """分段渲染 + 检查点现实同步。把整链渲染切成每 K 帧一段（K=_checkpoint_interval，
    段不跨镜头族），段间做 _checkpoint_reality_sync。返回（可能被校准改写过的）
    prompt_block，供下游恢复轮/回望/视频生成使用。

    qaGateLevel=off、间隔关闭、或本次待渲染帧数不超过一段时，退回原有的单次全量
    调用（target_sequences=None 自带断点续传跳已有帧 + 清全部血统标记的语义）。
    分段调用显式传 target_sequences 会禁用帧级续传跳过，故段目标里只放缺失帧。"""
    images, videos = _parse_prompt_slots(prompt_block)
    seqs = sorted(images)
    interval = _checkpoint_interval(config)
    supervised = _supervised_mode(config)
    missing = {s for s in seqs if not os.path.exists(_frame_path(title, s))}
    # 监修模式强制走分段路径（哪怕链很短/质检 off），否则暂停点无处安放；
    # 全自动模式维持原退化条件
    if interval <= 0 or not missing or \
            (not supervised and (qa_gate_level(config) == 'off' or len(missing) <= interval)):
        generate_frame_sequence(config, title, prompt_block, on_progress=on_progress,
                                target_sequences=None)
        return prompt_block

    config['_reanchors'] = []
    # 镜头族连续段（BRIDGE 处断开，检查点不跨族——锚点句与漂移基线都是族内概念）
    runs = []
    for seq in seqs:
        fam = family_anchor_seq(videos, seq)
        if runs and runs[-1][0] == fam:
            runs[-1][1].append(seq)
        else:
            runs.append((fam, [seq]))

    grand_total = len(missing)
    offset = 0
    first_segment = True
    changed = False
    for _fam, members in runs:
        # 监修模式：族锚帧（首帧/桥接交接帧）单独成段——渲染后立即暂停人工确认，
        # 确认通过之前不渲染任何将链在它身上的后续帧
        review_anchor = supervised and members[0] in missing
        if review_anchor and len(members) > 1:
            rest = members[1:]
            segments = [[members[0]]] + [rest[i:i + interval] for i in range(0, len(rest), interval)]
        else:
            segments = [members[i:i + interval] for i in range(0, len(members), interval)]
        for si, seg in enumerate(segments):
            targets = [s for s in seg if s in missing]
            if targets:
                block = _format_prompt_block(images, videos)
                generate_frame_sequence(
                    config, title, block,
                    on_progress=_segment_progress(on_progress, offset, grand_total, first_segment),
                    target_sequences=targets,
                )
                offset += len(targets)
                first_segment = False
            if review_anchor and si == 0 and targets:
                anchor = members[0]
                context = ('首帧——整链的视觉基因' if anchor == 1
                           else '镜头族交接锚点帧——桥接后的新基线')
                decision = _await_frame_review(config, title, anchor,
                                               _frame_path(title, anchor), context, on_progress)
                rerenders = 0
                while decision == 'rerender' and rerenders < _MAX_REVIEW_RERENDERS:
                    rerenders += 1
                    generate_frame_sequence(
                        config, title, _format_prompt_block(images, videos),
                        on_progress=_segment_progress(on_progress, offset - len(targets),
                                                      grand_total, False),
                        target_sequences=[anchor],
                    )
                    decision = _await_frame_review(config, title, anchor,
                                                   _frame_path(title, anchor), context, on_progress)
                continue  # 锚点确认段不做现实同步检查点（单帧无链可比）
            # 段间检查点：族的最后一段交给收尾链尾回望，没渲染新帧的段没有新现实可同步
            if si == len(segments) - 1 or not targets:
                continue
            try:
                if _checkpoint_reality_sync(config, title, images, videos, members, seg[-1],
                                            project_dir, on_progress=on_progress):
                    changed = True
            except Exception as e:
                print(f"[CHECKPOINT] 现实同步检查点异常（不拦截渲染）: {e}")
    return _format_prompt_block(images, videos) if changed else prompt_block


def _chain_drift_lookback(config, title, prompt_block, project_dir, on_progress=None):
    """链尾回望检查：全部帧渲染（含恢复轮）结束后，按镜头族各取 锚点/链中/链尾 三帧，
    一次 VLM 调用比对累积漂移。逐帧质检只看相邻对，每步都合格的缓慢偏移在链尾可能已
    很可观——这是"链尾对链头"组合唯一被比对的地方。

    检测型门：结果写进 manifest['chain_drift'] 并广播 chain_drift_check 事件，任何档位
    都不拦截视频生成（累积漂移无廉价自动修复，重渲链尾单帧修不了整条链）；off 档跳过。
    整个过程对流水线非致命：任何异常只打日志，不中断任务。"""
    if qa_gate_level(config) == 'off':
        return
    try:
        images, videos = _parse_prompt_slots(prompt_block)
        runs = {}
        for seq in sorted(images):
            # resolve_family_anchor：链中重锚定过的族在重锚点处自然分段，
            # 收尾回望的每个子链都对着自己实际生效的基线比
            runs.setdefault(resolve_family_anchor(config, videos, seq), []).append(seq)
        results = []
        for anchor in sorted(runs):
            members = [s for s in runs[anchor] if os.path.exists(_frame_path(title, s))]
            # 少于 4 帧的族没有"链中"可言，锚点/相邻检查已覆盖
            if len(members) < 4:
                continue
            head, tail = members[0], members[-1]
            mid = members[len(members) // 2]
            passed, reason = run_chain_tail_drift_check(
                config,
                _frame_path(title, head), _frame_path(title, mid), _frame_path(title, tail),
                anchor_seq=head, mid_seq=mid, tail_seq=tail,
                anchor_is_first_frame=(head == 1),
            )
            entry = {'family_anchor': head, 'mid': mid, 'tail': tail,
                     'passed': bool(passed), 'reason': reason}
            results.append(entry)
            if on_progress:
                verdict = '通过' if passed else '检出累积漂移'
                detail = f"（{reason}）" if reason and reason != 'PASS' else ''
                on_progress('chain_drift_check', {
                    **entry,
                    'message': f"链尾回望 IMG {head:03d}→{mid:03d}→{tail:03d}：{verdict}{detail}",
                })
        if results:
            with manifest_lock(project_dir):
                manifest = read_manifest(project_dir)
                if manifest:
                    manifest['chain_drift'] = results
                    write_manifest(project_dir, manifest)
    except Exception as e:
        print(f"[CHAIN DRIFT] 链尾回望检查异常（不拦截流程）: {e}")


def render_frames_for_task(config, title, prompt_block, on_progress=None):
    """/api/generate_frames 整单渲染的编排入口：分段渲染 + 检查点现实同步 + 监修暂停
    + 收尾链尾回望，与 staged/auto 流水线共享同一套机制（此前一次性端点直调
    generate_frame_sequence，检查点/回望/监修对主界面的帧序列按钮完全不生效）。
    不做锚点门/恢复轮/视频——保持该端点"只渲帧"的语义。
    返回 manifest（与 generate_frame_sequence 同约定，带 manifest/project_dir 瞬态键）。"""
    project_dir = _get_project_dir(title)
    prompt_block = _render_frames_with_checkpoints(config, title, prompt_block, project_dir,
                                                   on_progress=on_progress)
    _chain_drift_lookback(config, title, prompt_block, project_dir, on_progress=on_progress)
    manifest = read_manifest(project_dir) or {}
    manifest_path = os.path.join(project_dir, 'manifest.json')
    manifest['manifest'] = '/' + os.path.relpath(
        manifest_path, os.path.dirname(os.path.abspath(__file__))).replace('\\', '/')
    manifest['project_dir'] = os.path.abspath(project_dir)
    return manifest


def _render_videos_with_recovery(config, title, prompt_block, on_progress=None):
    """Render all videos, then run one autonomous retry pass over any slot that came
    back rejected/blocked (e.g. a failed Google FX anchor-match) instead of leaving it
    for a human to notice and re-trigger manually."""
    video_result = generate_video_sequence(config, title, prompt_block, on_progress=on_progress)
    failed_slots = [v['slot'] for v in video_result.get('videos', []) if v.get('status') != 'success']
    if failed_slots:
        if on_progress:
            on_progress('video_retry_autonomous', {'slots': failed_slots})
        video_result = generate_video_sequence(
            config, title, prompt_block, on_progress=on_progress, target_slots=failed_slots,
        )
    return video_result


def run_autonomous_pipeline(config, dimensions, on_progress=None):
    """Runs the full staged pipeline autonomously, composing its own prompt text.
    Returns a result dict with status='completed', or status='needs_human_review' if
    IMAGE 1 still fails the Anchor Acceptance Gate after _MAX_ANCHOR_ATTEMPTS retries —
    a rare escape hatch, not the routine path."""
    state = compose_anchor_and_packet(config, dimensions, on_progress=on_progress)
    title = state['title']

    def anchor_judge(image_path, prompt):
        return check_anchor_frame_compliance(config, image_path, prompt, state['packet'], state['parsed_brief'])

    gate = render_and_gate_single_frame(
        config, title, 1, state['image_1_prompt'], judge=anchor_judge, on_progress=on_progress,
    )
    state['image_1_prompt'] = gate['prompt']
    state['compiled_images'][1] = gate['prompt']

    if gate['status'] not in ('auto_approved', 'auto_approved_degraded'):
        if on_progress:
            on_progress('needs_human_review', {'sequence': 1, 'reason': gate['reason']})
        return {'title': title, 'status': 'needs_human_review', 'reason': gate['reason'], 'project_dir': gate['project_dir']}

    # 监修：首帧过自动门后暂停人工确认（判定器最弱、代价最高的节点——整链视觉基因）
    _rerenders = 0
    while _supervised_mode(config):
        decision = _await_frame_review(config, title, 1, gate['image_path'],
                                       '首帧已过自动锚点门——整链的视觉基因', on_progress)
        if decision != 'rerender' or _rerenders >= _MAX_REVIEW_RERENDERS:
            break
        _rerenders += 1
        gate = render_and_gate_single_frame(
            config, title, 1, state['image_1_prompt'], judge=anchor_judge, on_progress=on_progress,
        )
        state['image_1_prompt'] = gate['prompt']
        state['compiled_images'][1] = gate['prompt']
        if gate['status'] not in ('auto_approved', 'auto_approved_degraded'):
            if on_progress:
                on_progress('needs_human_review', {'sequence': 1, 'reason': gate['reason']})
            return {'title': title, 'status': 'needs_human_review', 'reason': gate['reason'], 'project_dir': gate['project_dir']}

    if on_progress:
        on_progress('packet_refine_start', {'message': '正在依据已确认的首帧修正 Drift Lock 数据包...'})
    state['packet'] = refine_packet_from_accepted_anchor(config, gate['image_path'], state['packet'])
    if on_progress:
        on_progress('packet_refined', {'message': 'Drift Lock 数据包已依据实际渲染结果修正。'})

    project_dir = gate['project_dir']
    prompt_block = compose_remaining_beats(config, state, on_progress=on_progress)
    prompt_block = _render_frames_with_checkpoints(config, title, prompt_block, project_dir, on_progress=on_progress)
    prompt_block = _recover_failed_frames(config, title, prompt_block, project_dir, on_progress=on_progress)
    _chain_drift_lookback(config, title, prompt_block, project_dir, on_progress=on_progress)
    _supervised_degraded_summary(config, title, project_dir, on_progress=on_progress)
    video_result = _render_videos_with_recovery(config, title, prompt_block, on_progress=on_progress)

    return {
        'title': title,
        'status': 'completed',
        'prompt_block': prompt_block,
        'prompt_slots': prompt_slots_list(prompt_block),
        'project_dir': project_dir,
        'videos': video_result,
    }


def run_staged_frame_rendering(config, title, prompt_block, on_progress=None):
    """Runs staged, gated rendering over an ALREADY-composed prompt_block, without
    re-deriving any prompt text. If IMAGE 1 is already 'auto_approved' in manifest.json
    (typically because the calling agent already gated it inline via
    render_and_gate_single_frame / /api/render_anchor before composing the rest) AND
    the anchor prompt fingerprint recorded at gate time matches this prompt_block's
    IMAGE 1, this skips re-gating it — otherwise it gates it here, so this endpoint
    alone still stages correctly and a stale same-titled manifest can't bypass the gate.
    Returns a result dict with status='completed' or 'needs_human_review'."""
    images, videos = _parse_prompt_slots(prompt_block)
    if 1 not in images:
        raise RuntimeError('未在 prompt_block 中找到 图片 1: 提示词，无法分步渲染')

    project_dir = _get_project_dir(title)

    item = images[1]
    prompt = item['body'] if isinstance(item, dict) else item
    meta = item.get('meta', '') if isinstance(item, dict) else ''

    # 复用旧判定的前提：不只是 manifest 记着 auto_approved，验锚时记录的提示词指纹还得与
    # 本次 IMAGE 1 一致，且过门的锚点图还在盘上——图被清理后跳过验锚，下游会重渲一张
    # 从未过门的新首帧接着整链构图。同名项目复跑/改过首帧提示词时旧判定作废，必须重新过门；
    # auto_approved_degraded（判定服务异常放行）不可复用，同样重新过门。
    # 例外：qaGateLevel=off 时锚点门本就被主动关闭，过门只会得到同样的 degraded 记录，
    # 重新过门却会重渲一张新首帧去接旧链（帧 2..N 仍挂在旧首帧上）——正是指纹复用要防的
    # 锚链错位。故 off 档下 degraded 记录同样可复用（指纹与图仍必须齐全）。
    frame_1 = _frame_manifest_entry(project_dir, 1) or {}
    _reusable_gates = ('auto_approved',) if qa_gate_level(config) != 'off' else ('auto_approved', 'auto_approved_degraded')
    if (frame_1.get('quality_gate') in _reusable_gates
            and frame_1.get('anchor_prompt_sha256') == _prompt_fingerprint(prompt)
            and os.path.exists(_frame_path(title, 1))):
        if on_progress:
            on_progress('anchor_check', {'sequence': 1, 'attempt': 0, 'passed': True, 'reason': '此前已通过判定且提示词未变，跳过重复渲染'})
    else:
        gate = render_and_gate_single_frame(config, title, 1, prompt, meta=meta, on_progress=on_progress)
        images[1] = {'body': gate['prompt'], 'meta': meta}
        if gate['status'] not in ('auto_approved', 'auto_approved_degraded'):
            if on_progress:
                on_progress('needs_human_review', {'sequence': 1, 'reason': gate['reason']})
            return {'title': title, 'status': 'needs_human_review', 'reason': gate['reason'], 'project_dir': project_dir}
        # 监修：首帧过自动门后暂停人工确认（指纹复用的旧首帧不重复打扰）
        _rerenders = 0
        while _supervised_mode(config):
            decision = _await_frame_review(config, title, 1, gate['image_path'],
                                           '首帧已过自动锚点门——整链的视觉基因', on_progress)
            if decision != 'rerender' or _rerenders >= _MAX_REVIEW_RERENDERS:
                break
            _rerenders += 1
            gate = render_and_gate_single_frame(config, title, 1, images[1]['body'], meta=meta, on_progress=on_progress)
            images[1] = {'body': gate['prompt'], 'meta': meta}
            if gate['status'] not in ('auto_approved', 'auto_approved_degraded'):
                if on_progress:
                    on_progress('needs_human_review', {'sequence': 1, 'reason': gate['reason']})
                return {'title': title, 'status': 'needs_human_review', 'reason': gate['reason'], 'project_dir': project_dir}

    prompt_block = _format_prompt_block(images, videos)
    prompt_block = _render_frames_with_checkpoints(config, title, prompt_block, project_dir, on_progress=on_progress)
    prompt_block = _recover_failed_frames(config, title, prompt_block, project_dir, on_progress=on_progress)
    _chain_drift_lookback(config, title, prompt_block, project_dir, on_progress=on_progress)
    _supervised_degraded_summary(config, title, project_dir, on_progress=on_progress)
    video_result = _render_videos_with_recovery(config, title, prompt_block, on_progress=on_progress)

    return {
        'title': title,
        'status': 'completed',
        'prompt_block': prompt_block,
        'prompt_slots': prompt_slots_list(prompt_block),
        'project_dir': project_dir,
        'videos': video_result,
    }
