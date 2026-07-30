"""Autonomous staged pipelines for the restoration-prompt-composer production app.

No per-frame gating beyond IMAGE 1 in the GUI/API paths: frames 2..N render
unconditionally as fast as possible. Cross-frame consistency review against the real
rendered images (_sequence_consistency_review, exposed as run_sequence_consistency_review)
is no longer run automatically after rendering — 2026-07-24: it's a manual, on-demand
check the user triggers from the frame grid once the sequence is done, not a step any
entry point below runs for you. The only review during rendering itself is corrective,
not gating: periodic reality-checkpoint recalibration and a final chain-tail drift
lookback (_checkpoint_reality_sync / _chain_drift_lookback), which rewrite drifting
prompts in place but never block or retry a render.

IMAGE 1 (the "trauma state" anchor every later frame visually chains from) is the one
exception: it goes through the real Anchor Acceptance Gate (check_anchor_frame_compliance)
in every entry point below, since a not-raw-enough/too-clean/intervention-tainted anchor
poisons the whole downstream chain and was previously only ever caught by the
conversational skill path. GUI/API callers pass hard_fail_status='auto_approved_degraded'
to render_and_gate_single_frame so a persistently unconvincing anchor never hard-blocks —
after _MAX_ANCHOR_ATTEMPTS honest VLM-feedback retries it proceeds with the best attempt,
flagged in the manifest, never a dead end.

Four entry points, sharing the same render/recovery machinery:

- render_and_gate_single_frame: render ONE frame and run it through a caller-supplied
  `judge`, synchronously, returning the verdict directly (no task_id/polling). The
  conversational skill invocation (via /api/render_anchor, see server.py; an agent
  following SKILL.md's Steps 1-11 mid-turn) and run_staged_frame_rendering both still
  pass no `judge`/`_no_gate_judge`-style always-pass behavior where a human/agent is
  driving and a real dead end is meaningful; run_autonomous_pipeline and
  render_frames_for_task pass a real judge with hard_fail_status='auto_approved_degraded'
  (never a dead end, see above).

- render_frames_for_task: an ALREADY-composed prompt_block in, gates+renders IMAGE 1 if
  it isn't on disk yet, then does segmented rendering + checkpoint sync + chain-tail
  lookback over the rest (sequence review is manual — see run_sequence_consistency_review;
  the "关键点监修模式" human-confirmation pauses that used to live here were removed
  2026-07-24, the pipeline never blocks waiting on a person now). Used by server.py's
  /api/generate_frames — the main "帧序列" button's entry point.

- run_autonomous_pipeline: dimensions in, everything out. Composes IMAGE 1's prompt
  itself (compose_anchor_and_packet), gates+renders it, refines the Drift Lock packet
  against that render, then composes and renders the rest (compose_remaining_beats),
  and generates video — sequence consistency review is not run automatically here
  either; trigger it manually via run_sequence_consistency_review if wanted before
  spending video quota. Used by server.py's /api/auto_run — the GUI/API-driven,
  dimensions-first path.

- run_staged_frame_rendering: an ALREADY-composed prompt_block in, staged rendering
  out. For the case where an agent already wrote the full IMAGE/VIDEO prompt text
  and now wants the rest rendered. Used by server.py's /api/render_staged, which
  scripts/generate_frames.py calls instead of the old one-shot /api/generate_frames.
  Keeps the pre-existing no-gate/needs_human_review dead-end contract unchanged since
  an agent is driving it.

All entry points lead into the same autonomous recovery pass over any rejected/blocked
video clip, so nothing past IMAGE 1 ever dead-ends in a manual-review state.
"""
import os
import hashlib
from datetime import datetime

from server_common import (
    _get_project_dir, read_manifest, write_manifest, manifest_lock, qa_gate_level,
    IMG2IMG_CONTROL_PROMPT, GenerationCancelled,
    frame_content_hash, drop_stale_review_verdicts, REAL_REVIEW_VERDICTS,
)
from prompt_pipeline import (
    frame_review_status, merge_review_results,
    compose_anchor_and_packet,
    compose_remaining_beats,
    check_anchor_frame_compliance,
    refine_packet_from_accepted_anchor,
    fix_image_prompt_with_vlm_feedback,
    check_full_sequence_consistency,
    _verify_review_violation,
    fix_beat_from_sequence_review,
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
from frame_generator import (
    generate_frame_sequence, _generate_image_edit, _image_edit_model,
    _measure_image_pixels, _chat_transport_is_full_quality, chat_transport_note,
    CHAT_TRANSPORT,
)
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


def _record_review_fingerprints(project_dir, title, sequences):
    """把本轮审查实际看过的帧内容哈希记进 manifest（review_frames_sha256: {seq: hash}）。

    每帧记的是"它参与的那两拍所涉及的帧"的哈希——帧 seq 的结论同时依赖 seq-1/seq/seq+1
    三张图，其中任何一张被重渲，这个结论就该作废。锚点门早有 anchor_prompt_sha256 这套
    指纹复用机制，一致性审查此前完全没有对应物：修完 IMG 005 之后，beat 4/5 的判定其实
    已经作废，IMG 004 和 IMG 006 却仍挂着 sequence_reviewed_pass，前端显示的"全部审查
    通过"从那一刻起就是假的。"""
    hashes = {s: frame_content_hash(_frame_path(title, s)) for s in sequences}
    with manifest_lock(project_dir):
        manifest = read_manifest(project_dir)
        if not manifest:
            return
        for frame in manifest.get('frames', []):
            seq = frame.get('sequence')
            if seq not in hashes:
                continue
            related = [s for s in (seq - 1, seq, seq + 1) if s in hashes and hashes[s]]
            if not related:
                continue
            frame['review_frames_sha256'] = {str(s): hashes[s] for s in related}
            frame['reviewed_at'] = datetime.now().isoformat(timespec='seconds')
        write_manifest(project_dir, manifest)


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


def _set_manifest_quality_gate(project_dir, sequence, quality_gate, reason=None, prompt_fingerprint=None,
                               review_issues=_UNSET):
    """Overwrite one frame's recorded quality_gate/vlm_qa_reason in manifest.json.
    Needed because generate_frame_sequence's own per-frame QA never produces a real
    verdict for frame 1 (it has no prior frame to compare motion against), so the
    Anchor Acceptance Gate's verdict has to be written back out-of-band.

    review_issues：一致性审查的结构化违规记录（每条含 layer/beat/frames/verified，见
    prompt_pipeline.check_full_sequence_consistency）。vlm_qa_reason 只是给人看的摘要，
    '；'.join 之后哪一层检出的、涉及哪几帧、复核确认没有全丢了——修完也没法验证这一条
    到底解决没有。默认 _UNSET＝不碰这个字段（锚点门等其它调用方与审查无关）；传 None
    表示显式清空。"""
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
            # An IMAGE is always a still frame regardless of family — never allow
            # moving-camera wording (bridge motion belongs only in the VIDEO prompt).
            prompt = fix_camera_contradictions(prompt)
    return False, reason


def render_and_gate_single_frame(config, title, sequence, prompt, meta='', judge=None, on_progress=None,
                                 hard_fail_status='needs_human_review'):
    """Render exactly one frame and run it through an acceptance gate synchronously,
    returning the final verdict directly (no task_id/polling) so a caller — including a
    conversational agent mid-turn — can decide what to do next before composing anything
    else. Defaults to the Anchor Acceptance Gate judged purely from the prompt text
    (no separate Drift Lock packet available); callers with a real packet/parsed_brief
    (run_autonomous_pipeline) pass their own `judge`.
    `hard_fail_status` controls what a still-failing verdict is recorded/returned as after
    _MAX_ANCHOR_ATTEMPTS is exhausted: the conversational-skill/staged-script callers keep
    the default 'needs_human_review' (a human is driving, they should see the dead end);
    the autonomous GUI/API callers pass 'auto_approved_degraded' instead so a persistently
    unconvincing anchor never hard-blocks the pipeline — it proceeds with the best attempt,
    flagged in the manifest for visibility, same as any other degraded/unverified frame.
    Returns {'status': 'auto_approved'|'auto_approved_degraded'|'needs_human_review',
    'reason', 'prompt', 'image_path', 'project_dir'}. auto_approved_degraded 表示判定
    服务异常被 fail-open 放行，或判定真跑过但重试耗尽仍未通过——两种情况帧都没有
    真正过检，只是没被拦，区别只在 reason 文本。"""
    if judge is None:
        def judge(image_path, current_prompt):
            return check_anchor_frame_compliance(config, image_path, current_prompt, {}, {})

    images = {sequence: {'body': prompt, 'meta': meta}}
    passed, reason = _retry_frame_until_pass(config, title, sequence, images, {}, judge, on_progress=on_progress)
    project_dir = _get_project_dir(title)
    if passed:
        status = 'auto_approved_degraded' if is_skipped_verdict(reason) else 'auto_approved'
    else:
        status = hard_fail_status
    _set_manifest_quality_gate(project_dir, sequence, status, reason,
                               prompt_fingerprint=_prompt_fingerprint(images[sequence]['body']))
    return {
        'status': status,
        'reason': reason,
        'prompt': images[sequence]['body'],
        'image_path': _frame_path(title, sequence),
        'project_dir': project_dir,
    }



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
    missing = {s for s in seqs if not os.path.exists(_frame_path(title, s))}
    if interval <= 0 or not missing or qa_gate_level(config) == 'off' or len(missing) <= interval:
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
            # 段间检查点：族的最后一段交给收尾链尾回望，没渲染新帧的段没有新现实可同步
            if si == len(segments) - 1 or not targets:
                continue
            try:
                if _checkpoint_reality_sync(config, title, images, videos, members, seg[-1],
                                            project_dir, on_progress=on_progress):
                    changed = True
            except GenerationCancelled:
                # 取消不是"检查点异常"：吞掉它，段间检查点（含一次 VLM 调用）里点的取消
                # 会被咽下去，循环接着渲下一段——用户看到的就是"取消了没用"。
                # 与 _chain_drift_lookback 同一处理（见那里的同款注释）。
                raise
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
    except GenerationCancelled:
        # 取消不是"检查异常"：吞掉它会让整条流水线在用户点了取消之后继续往下跑
        raise
    except Exception as e:
        print(f"[CHAIN DRIFT] 链尾回望检查异常（不拦截流程）: {e}")


def _sequence_consistency_review(config, title, prompt_block, project_dir, on_progress=None):
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
    （invalidate_stale_review_verdicts），不再出现修完一帧后邻帧还挂着过期"审查通过"。"""
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

    if on_progress:
        if partial:
            on_progress('sequence_review', {
                'message': f'仅前 {len(rendered)}/{len(all_seqs)} 帧已渲染，'
                           f'先对这一段做一致性审查（其余帧渲完后可再跑一次补齐）...',
            })
        else:
            on_progress('sequence_review', {'message': '正在对整套已渲染序列做一致性审查...'})
    final_result = check_full_sequence_consistency(config, prompt_block, frame_paths,
                                                   on_progress=on_progress)
    if final_result is None:
        # 整轮彻底没跑起来（超时/网关异常）≠ 审查通过。降级重试一次：帧图压小 +
        # 超时放宽。2026-07-15 事故里这里曾直接 fail-open 放行整单。
        if on_progress:
            on_progress('sequence_review', {
                'message': '一致性审查调用失败（超时/网关异常），压缩帧图降级重试一次...',
            })
        final_result = check_full_sequence_consistency(config, prompt_block, frame_paths,
                                                       degraded=True, on_progress=on_progress)
    elif final_result.get('unreviewed_beats') or not final_result.get('global_reviewed'):
        # 部分没审成：只降级重跑这几拍（外加没跑成的跨帧层），不再把已经审干净的
        # 整批重来一遍——一次网络抖动此前会让整单多烧一遍全部调用。
        retry_beats = list(final_result.get('unreviewed_beats') or [])
        skip_global = bool(final_result.get('global_reviewed'))
        if on_progress:
            scope = (f"第 {'、'.join(str(b) for b in retry_beats)} 拍" if retry_beats else '')
            scope += ('' if skip_global else ('与跨帧审查' if scope else '跨帧审查'))
            on_progress('sequence_review', {
                'message': f'{scope}未跑成，压缩帧图降级重试这部分...',
            })
        retry = check_full_sequence_consistency(config, prompt_block, frame_paths, degraded=True,
                                                only_beats=retry_beats, skip_global=skip_global,
                                                on_progress=on_progress)
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
                                       '一致性审查服务不可用（降级重试仍失败），此帧未经整套序列审查')
        if on_progress:
            tail = f'（{kept} 帧保留了上一轮的审查结论）' if kept else ''
            on_progress('sequence_review_result', {
                'passed': False, 'skipped': True,
                'message': f'一致性审查服务不可用（降级重试仍失败）：未审过的帧已如实标记为「未经审查」{tail}，请留意画面一致性。',
            })
        return prompt_block

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
    for seq in rendered:
        status, reason = statuses.get(seq, ('unreviewed', '未参与本轮审查'))
        _set_manifest_quality_gate(project_dir, seq, gate_by_status[status], reason,
                                   review_issues=issues_by_seq.get(seq) or None)
    # 把"本轮看的是哪几张图"钉进 manifest：之后任何一帧被重渲，
    # invalidate_stale_review_verdicts 就能据此让相关结论自动作废
    _record_review_fingerprints(project_dir, title,
                                [s for s in rendered if statuses.get(s, ('', ))[0] != 'unreviewed'])

    failures = final_result.get('failures') or {}
    flagged_seqs = sorted(s for s, (st, _) in statuses.items() if st == 'flagged')
    unreviewed_seqs = sorted(s for s, (st, _) in statuses.items() if st == 'unreviewed')

    # 只审了连续前缀时必须说明还剩几帧没渲——否则一句"审查通过"会被读成整单通过
    partial_note = (f'（本轮只覆盖已渲染的前 {len(rendered)}/{len(all_seqs)} 帧，'
                    f'其余帧渲完后请再跑一次）' if partial else '')

    if not flagged_seqs and not unreviewed_seqs:
        if on_progress:
            on_progress('sequence_review_result', {
                'passed': True, 'partial': partial,
                'reviewed_sequences': rendered,
                **({'message': f'已渲染的前 {len(rendered)} 帧一致性审查通过{partial_note}'}
                   if partial else {}),
            })
        return prompt_block

    if on_progress:
        parts = []
        if flagged_seqs:
            detail = '；'.join(f"IMG {s:03d}: {'；'.join(failures.get(s - 1, []))}" for s in flagged_seqs)
            parts.append(f'发现 {len(flagged_seqs)} 帧存在问题（{detail}）——已保留渲染结果、'
                         f'未自动修改，请在帧网格确认后点击「修复此帧问题」')
        if unreviewed_seqs:
            # 漏审必须说出来：此前这些帧会被静默盖成"审查通过"
            parts.append(f'另有 {len(unreviewed_seqs)} 帧未审完'
                         f'（IMG {"、".join(f"{s:03d}" for s in unreviewed_seqs)}），'
                         f'已标记为「未审查」，可重跑审查补齐')
        on_progress('sequence_review_result', {
            'passed': False, 'beats': sorted(failures.keys()),
            'unreviewed_sequences': unreviewed_seqs,
            'partial': partial, 'reviewed_sequences': rendered,
            'message': '一致性审查' + '；'.join(parts) + '。' + partial_note,
        })
    return prompt_block


def run_sequence_consistency_review(config, title, prompt_block, on_progress=None):
    """`_sequence_consistency_review` 的手动触发入口：供 server.py 的
    /api/sequence_review 调用——2026-07-24 起该审查不再被任何渲染入口自动跑，
    用户需在帧网格确认整套序列已渲染完成后手动点按钮触发。不阻塞视频生成，纯粹
    是一个可选的人工检查工具。"""
    project_dir = _get_project_dir(title)
    return _sequence_consistency_review(config, title, prompt_block, project_dir, on_progress=on_progress)


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
            write_manifest(project_dir, manifest)

    if on_progress:
        entry = _frame_manifest_entry(project_dir, sequence) or {}
        on_progress('frame', {'current': 1, 'total': 1, 'frame': entry})
    return {'sequence': sequence, 'image_path': target_path, 'project_dir': project_dir}


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
    走单提示词反馈重写（fix_image_prompt_with_vlm_feedback，与锚点门重试同款）。
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

    verify = _reverify_frame_issues(config, title, sequence, recorded_issues, on_progress=on_progress)
    return {'prompt_block': new_prompt_block, 'reason': reason, 'reverify': verify}


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
    """/api/generate_frames 整单渲染的编排入口：分段渲染 + 检查点现实同步 + 收尾链尾
    回望，与 staged/auto 流水线共享同一套机制（此前一次性端点直调
    generate_frame_sequence，检查点/回望对主界面的帧序列按钮完全不生效）。
    整套序列一致性审查不在此自动触发，改为用户手动点按钮
    （见 run_sequence_consistency_review）。不做视频——保持该端点"只渲帧"的语义。
    2026-07-24 起「关键点监修模式」（人工确认暂停）功能已整体移除，流水线全程不再
    暂停等人工确认。
    返回 manifest（与 generate_frame_sequence 同约定，带 manifest/project_dir 瞬态键）。"""
    project_dir = _get_project_dir(title)

    # IMAGE 1 的"够不够原始"(零干预痕迹/损伤烈度/monumental 气质) 此前从未被真正
    # 复核过：check_anchor_frame_compliance 这套 Anchor Acceptance Gate 只接在
    # run_autonomous_pipeline 和对话式技能路径，"激发创意"实际驱动的这条主界面
    # 帧序列按钮此前直接分段渲染，完全绕过了它——首帧原不原始全靠合成 LLM 写的
    # 那段 prompt 自觉，渲染结果从未被回查。这里补一道轻量复核：只对首帧、判定
    # 沿用 qaGateLevel(off 跳过/lenient/standard)，失败用既有 VLM 反馈改写 prompt
    # 再渲（_MAX_ANCHOR_ATTEMPTS 次），耗尽仍不过也按 auto_approved_degraded 放行、
    # 绝不硬阻断——不影响其余帧的直出速度。
    images, videos = _parse_prompt_slots(prompt_block)
    if 1 in images and not os.path.exists(_frame_path(title, 1)):
        item = images[1]
        prompt = item['body'] if isinstance(item, dict) else item
        meta = item.get('meta', '') if isinstance(item, dict) else ''
        gate = render_and_gate_single_frame(
            config, title, 1, prompt, meta=meta, on_progress=on_progress,
            hard_fail_status='auto_approved_degraded',
        )
        images[1] = {'body': gate['prompt'], 'meta': meta}
        prompt_block = _format_prompt_block(images, videos)

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


def _no_gate_judge(image_path, prompt):
    """恒真判定：仅供 run_staged_frame_rendering（agent/脚本驱动的 /api/render_staged
    路径）使用——那条路径有人/agent 在场，真判定失败时的 needs_human_review 死路是
    有意义的终态，本函数按既有约定保持不变。run_autonomous_pipeline 已改用真实判定
    （见下方 _image1_judge），不再依赖本函数。仍走 render_and_gate_single_frame/
    _retry_frame_until_pass 的既有管线（manifest 回写等）。"""
    return True, None


def run_autonomous_pipeline(config, dimensions, on_progress=None):
    """Runs the full staged pipeline autonomously, composing its own prompt text."""
    state = compose_anchor_and_packet(config, dimensions, on_progress=on_progress)
    title = state['title']

    def _image1_judge(image_path, current_prompt):
        return check_anchor_frame_compliance(config, image_path, current_prompt, state['packet'], state['parsed_brief'])

    gate = render_and_gate_single_frame(
        config, title, 1, state['image_1_prompt'], judge=_image1_judge, on_progress=on_progress,
        hard_fail_status='auto_approved_degraded',
    )
    state['image_1_prompt'] = gate['prompt']
    state['compiled_images'][1] = gate['prompt']

    if on_progress:
        on_progress('packet_refine_start', {'message': '正在依据已确认的首帧修正 Drift Lock 数据包...'})
    state['packet'] = refine_packet_from_accepted_anchor(
        config, gate['image_path'], state['packet'], state.get('parsed_brief'))
    if on_progress:
        on_progress('packet_refined', {'message': 'Drift Lock 数据包已依据实际渲染结果修正。'})

    project_dir = gate['project_dir']
    prompt_block = compose_remaining_beats(config, state, on_progress=on_progress)
    prompt_block = _render_frames_with_checkpoints(config, title, prompt_block, project_dir, on_progress=on_progress)
    _chain_drift_lookback(config, title, prompt_block, project_dir, on_progress=on_progress)
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
        gate = render_and_gate_single_frame(config, title, 1, prompt, meta=meta, judge=_no_gate_judge, on_progress=on_progress)
        images[1] = {'body': gate['prompt'], 'meta': meta}
        if gate['status'] not in ('auto_approved', 'auto_approved_degraded'):
            if on_progress:
                on_progress('needs_human_review', {'sequence': 1, 'reason': gate['reason']})
            return {'title': title, 'status': 'needs_human_review', 'reason': gate['reason'], 'project_dir': project_dir}

    prompt_block = _format_prompt_block(images, videos)
    prompt_block = _render_frames_with_checkpoints(config, title, prompt_block, project_dir, on_progress=on_progress)
    _chain_drift_lookback(config, title, prompt_block, project_dir, on_progress=on_progress)
    video_result = _render_videos_with_recovery(config, title, prompt_block, on_progress=on_progress)

    return {
        'title': title,
        'status': 'completed',
        'prompt_block': prompt_block,
        'prompt_slots': prompt_slots_list(prompt_block),
        'project_dir': project_dir,
        'videos': video_result,
    }
