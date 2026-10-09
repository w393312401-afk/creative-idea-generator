"""Continue one video run until its requested slots are delivered or cancelled.

Only orchestration state belongs to this module. Providers and reconciliation
retain ownership of media and submission receipts, including unresolved ones.
"""

import math
import os
import time
from datetime import datetime

import server_common as common


DEFAULT_RETRY_DELAYS = (60, 120, 240, 480, 660)
DEFAULT_MAX_SLOT_ATTEMPTS = 6
_DELIVERED = frozenset({'success', 'skipped_cut', 'skipped_bridge_hold'})
_INPUT_ERROR_CODES = frozenset({
    'configuration', 'unsupported_model', 'unsupported_reference_mode',
    'unsupported_options', 'frame_missing', 'frame_invalid', 'prompt_missing',
    'media_tools_missing', 'authorization', 'output', 'callback',
})
_INPUT_ERROR_TEXT = (
    '未找到已生成的帧图像', '未找到任何视频提示词', '视频提示词不能为空',
    '所需的起始帧', '所需的结束帧', '首尾帧文件缺失', '首尾帧图片无效',
    'Flow2API 服务地址或密钥未配置正确', '当前 Flow2API 视频通道仅支持',
    'Flow2API 不支持所选时长', 'Flow2API 身份验证失败，请检查服务端密钥',
    '缺少 ffmpeg 或 ffprobe', '视频输出目录不可用', '视频交付状态保存失败',
    '视频任务进度保存失败',
    '持续视频恢复进度保存失败',
    '属于降级帧', '已拦截该段视频生成', '血统过期），已拦截',
)


class VideoRecoveryBlockedError(RuntimeError):
    """A correction to configuration or source frames is required to continue."""

    def __init__(self, slots, message):
        self.slots = sorted(slots)
        self.reasons = dict(slots) if isinstance(slots, dict) else {}
        super().__init__(message)


class VideoRecoveryExhaustedError(VideoRecoveryBlockedError):
    """A slot kept failing after the configured number of paid submissions."""


def max_slot_attempts(config):
    """Paid submissions allowed per slot before automatic retry gives up."""
    value = (config or {}).get('videoMaxSlotAttempts')
    try:
        value = int(value)
    except (TypeError, ValueError):
        return DEFAULT_MAX_SLOT_ATTEMPTS
    return value if value >= 1 else DEFAULT_MAX_SLOT_ATTEMPTS


def slot_failure_message(manifest, slot):
    row = _rows(manifest or {}).get(slot) or {}
    attempt = row.get('last_attempt') or {}
    return str(attempt.get('error') or row.get('error') or '生成失败')


def exhausted_message(slots, attempts, manifest):
    return '；'.join(
        f'视频 {slot} 已自动提交 {attempts.get(slot, 0)} 次仍未成功，已停止自动重试'
        f'（最后一次：{slot_failure_message(manifest, slot)}），请检查提示词或首尾帧后手动重新生成'
        for slot in sorted(slots))


def _cancelled(cancel_check):
    return bool(cancel_check and cancel_check())


def _check_cancel(cancel_check):
    if _cancelled(cancel_check):
        raise ConnectionError('视频生成已取消，已保留成功视频')


def wait_for_video_recovery(delay, cancel_check=None):
    """Wait with at most half a second of cancellation latency."""
    deadline = time.monotonic() + delay
    while True:
        _check_cancel(cancel_check)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 0.5))


def _slot(video):
    try:
        return int(video.get('slot', video.get('sequence')))
    except (ValueError, TypeError):
        return None


def _rows(manifest):
    return {_slot(row): row for row in manifest.get('videos', [])
            if isinstance(row, dict) and _slot(row) is not None}


def _asset_present(row, directory):
    """A ``success`` row only counts while its MP4 exists and is non-empty."""
    if row.get('status') != 'success':
        return True
    raw = str(row.get('file') or '')
    candidates = [raw, os.path.join(os.path.dirname(__file__), raw.lstrip('/')),
                  os.path.join(directory, 'videos', os.path.basename(raw))]
    try:
        return bool(raw) and any(os.path.isfile(path) and os.path.getsize(path) > 0
                                 for path in candidates)
    except OSError:
        return False


def _successes(manifest, directory):
    successes = set()
    for slot, row in _rows(manifest).items():
        if row.get('status') not in _DELIVERED:
            continue
        attempt = row.get('last_attempt') or {}
        # An explicit retry can preserve its previous MP4 while the new attempt
        # fails. That preserved artifact does not fulfil the new request.
        if (attempt.get('status') in {'failed', 'cancelled', 'running'}
                or attempt.get('submission_pending') is True):
            continue
        if _asset_present(row, directory):
            successes.add(slot)
    return successes


def delivered_slots(manifest, directory):
    """Slots whose latest attempt delivered a present asset."""
    return _successes(manifest or {}, directory)


def _existing_deliveries(manifest, directory):
    """Delivered assets whatever the latest attempt says (retained previous MP4s).

    A whole-order run never asked to replace these, so it must reuse them: an
    earlier cancelled/failed whole-order attempt leaves every delivered row with
    ``last_attempt.status == 'cancelled'`` and the MP4 retained.
    """
    return {slot for slot, row in _rows(manifest).items()
            if row.get('status') in _DELIVERED and _asset_present(row, directory)}


def _manifest(value, directory, fallback):
    if isinstance(value, dict):
        if isinstance(value.get('manifest'), dict):
            return value['manifest']
        if isinstance(value.get('videos'), list):
            return value
    return common.read_manifest(directory) or fallback


def _input_error(error):
    if isinstance(error, (ValueError, TypeError, FileNotFoundError)):
        return True
    return (getattr(error, 'code', None) in _INPUT_ERROR_CODES
            or any(text in str(error) for text in _INPUT_ERROR_TEXT))


def _blocked(manifest, slots):
    blocked = {}
    for slot, row in _rows(manifest).items():
        if slot not in slots:
            continue
        attempt = row.get('last_attempt') or {}
        error = attempt.get('error') or row.get('error') or ''
        code = attempt.get('error_code') or row.get('error_code')
        if (row.get('status') == 'blocked' or code in _INPUT_ERROR_CODES
                or any(text in str(error) for text in _INPUT_ERROR_TEXT)):
            blocked[slot] = str(error or '源帧或配置不满足生成条件')
    if blocked:
        raise VideoRecoveryBlockedError(blocked, '；'.join(
            f'视频 {slot}：{message}' for slot, message in sorted(blocked.items())))


def _query_only_slots(manifest, slots, outcomes, provider=None, delivered_slots=()):
    """Known accepted operations must be observed and downloaded, never replayed."""
    held = set()
    outcome_by_slot = {_slot(row): row for row in outcomes if isinstance(row, dict)}
    for slot, row in _rows(manifest).items():
        if slot not in slots or slot in delivered_slots:
            continue
        attempt = row.get('last_attempt') or {}
        operation = attempt.get('operation_id') or attempt.get('upstream_task_id')
        state = outcome_by_slot.get(slot, {}).get('state') or attempt.get('recovery_state')
        # An upstream terminal failure/refusal is definitive permission to retry.
        if state in {'failed', 'refused'}:
            continue
        if (attempt.get('submission_pending') is False
                and attempt.get('confirmed') is not True
                and state != 'recovery_failed'):
            continue
        native_pending = (attempt.get('submission_pending') is True
                          and (attempt.get('provider') or provider) != 'flow2api')
        if (native_pending or attempt.get('upstream_accepted') is True
                or operation and (attempt.get('submission_pending') is True
                                 or attempt.get('confirmed') is True
                                 or state in {'pending', 'not_found', 'confirmed', 'recovery_failed'}
                                 or attempt.get('submission_pending') is not False)):
            held.add(slot)
    return held


def _attempt_age(attempt, now):
    raw = attempt.get('started_at')
    if not raw:
        return None
    try:
        return now - datetime.fromisoformat(str(raw).replace('Z', '+00:00')).timestamp()
    except (TypeError, ValueError):
        return None


def execute_video_generation_with_recovery(
        config, title, prompt_block, *, generate_fn=None, reconcile_fn=None,
        wait_fn=None, target_slots=None, on_progress=None, cancel_check=None,
        override_flagged=False, is_chain=False, recovery_only=False,
        first_pass_only=False, project_dir=None, retry_delays=DEFAULT_RETRY_DELAYS,
        attempt_counts=None, **generation_kwargs):
    """Run the existing generator, then recover only this run's unfinished slots.

    ``generate_fn`` has the normal/chain generator signature. ``reconcile_fn``
    accepts (project_key, slots, config, latest_only=True, cancel_check=...).
    ``wait_fn`` accepts (seconds, cancel_check), permitting zero waits in tests.
    Physical account/browser leases should be scoped inside ``generate_fn``.
    With the feature disabled this is exactly one call to the original generator.
    ``first_pass_only`` applies that first-call safety guard without waiting;
    progressive schedulers can finish other ready work before the recovery loop.
    ``attempt_counts`` (slot -> paid submissions) is updated in place so a
    progressive scheduler shares one per-slot budget across its calls; a slot
    reaching ``videoMaxSlotAttempts`` is given up instead of retried forever.
    """
    if generate_fn is None:
        from video_generator import generate_video_chain_sequence, generate_video_sequence
        generate_fn = generate_video_chain_sequence if is_chain else generate_video_sequence

    def generate(slots):
        return generate_fn(config, title, prompt_block, on_progress=on_progress,
                           target_slots=slots, override_flagged=override_flagged,
                           **generation_kwargs)

    if config.get('videoContinuousGeneration') is not True:
        return generate(target_slots)

    delays = tuple(float(value) for value in retry_delays)
    if not delays or any(not math.isfinite(value) or value < 0 for value in delays):
        raise ValueError('持续视频重试间隔必须是非负有限秒数')
    from video_generator import video_submission_slots
    targets = set(video_submission_slots(prompt_block, target_slots, is_chain=is_chain))
    if not targets:
        raise ValueError('未找到本次需要生成的视频槽位')
    chain_slots = sorted(video_submission_slots(prompt_block, is_chain=True)) if is_chain else []
    if reconcile_fn is None:
        from video_submission_recovery import reconcile_video_submissions
        reconcile_fn = reconcile_video_submissions
    wait_fn = wait_fn or wait_for_video_recovery
    if cancel_check is None and on_progress:
        cancel_check = lambda: bool(on_progress('cancel_check', None))

    directory = os.path.abspath(project_dir or common._get_project_dir(title))
    project_key = config.get('_project_key') or title
    latest = common.read_manifest(directory) or {}
    completed = (_successes(latest, directory) & targets) if recovery_only else set()
    retry_round = 0
    phase = 'querying' if recovery_only else 'retrying'
    next_retry_at = None
    last_message = ''
    attempts = attempt_counts if attempt_counts is not None else {}
    attempt_limit = max_slot_attempts(config)
    gave_up = set()

    def clear_completed_stats(manifest):
        stats = dict(manifest.get('video_generation_stats') or {})
        last_run = dict(stats.get('last_run') or {})
        for key in ('failed_slots', 'cancelled_slots'):
            last_run[key] = [slot for slot in last_run.get(key, [])
                             if _slot({'slot': slot}) not in targets]
        last_run['planned_slots'] = len(targets)
        stats['last_run'] = last_run
        manifest['video_generation_stats'] = stats

    def publish(new_phase, slots, message, *, status='running', retry_at=None):
        nonlocal phase, next_retry_at
        phase, next_retry_at = new_phase, retry_at
        details = {'phase': phase, 'slots': sorted(slots), 'retry_round': retry_round,
                   'next_retry_at': retry_at, 'completed_slots': sorted(completed),
                   'message': message, 'status': status}
        os.makedirs(directory, exist_ok=True)
        # Do not write our stale video/frame snapshot back over provider changes.
        with common.manifest_lock(directory):
            current = common.read_manifest(directory) or {}
            current['video_recovery'] = dict(details)
            if status == 'complete':
                clear_completed_stats(current)
            common.write_manifest(directory, current)
            saved = common.read_manifest(directory)
            if not isinstance(saved, dict) or saved.get('video_recovery') != details:
                raise RuntimeError('持续视频恢复进度保存失败，已停止继续提交')
        if on_progress:
            on_progress('video_recovery', details)
        return details

    def refresh(value, scope=None):
        nonlocal latest
        latest = _manifest(value, directory, latest)
        completed.update(_successes(latest, directory) & targets
                         & (targets if scope is None else set(scope)))

    provider = config.get('videoProvider') or 'google_fx'
    # Flow2API can leave an accepted operation ``pending`` forever (for example
    # ``poll_failed``). Past the provider's own video timeout the original is
    # treated as dead, so one possible duplicate charge replaces an endless stall.
    hold_limit = float(config.get('flow2apiVideoTimeoutSeconds') or 1800)
    held_since = {}

    def held_slots(slots, outcomes):
        nonlocal last_message
        held = _query_only_slots(latest, slots, outcomes, provider,
                                 _successes(latest, directory))
        rows, now, clock = _rows(latest), time.time(), time.monotonic()
        released = set()
        for slot in held:
            attempt = (rows.get(slot) or {}).get('last_attempt') or {}
            if (attempt.get('provider') or provider) != 'flow2api':
                continue
            held_since.setdefault(slot, clock)
            age = max(_attempt_age(attempt, now) or 0, clock - held_since[slot])
            if age >= hold_limit:
                released.add(slot)
        if released:
            last_message = (f'视频 {"、".join(map(str, sorted(released)))} 原提交超过 '
                            f'{round(hold_limit / 60)} 分钟仍未确认，按失败处理并重新提交')
        return held - released

    def chain_blockers(slots):
        if not is_chain:
            return set()
        delivered = _successes(latest, directory)
        blocked = set()
        for slot in slots:
            predecessors = {previous for previous in chain_slots if previous < slot}
            if slot > chain_slots[0]:
                predecessors.add(slot - 1)
            for previous in predecessors:
                if (previous not in delivered
                        or previous in targets and previous not in completed):
                    blocked.add(previous)
        return blocked

    def choose_retry_slots(candidates):
        if not is_chain or not (targets - completed):
            return set(candidates)
        # A chain must never use the tail of an older retained predecessor.
        # Complete the earliest unfinished target before any later target.
        first = min(targets - completed)
        if first not in candidates or chain_blockers({first}):
            return set()
        return {first}

    def query(slots):
        nonlocal last_message
        slots = set(slots) | chain_blockers(slots)
        _check_cancel(cancel_check)
        publish('querying', slots, '正在核对原提交并取回已完成的视频')
        before = set(completed)
        outcomes = []
        try:
            result = reconcile_fn(project_key, sorted(slots), config,
                                  latest_only=True, cancel_check=cancel_check)
            refresh(result, scope=slots)
            outcomes = (result.get('outcomes') or []) if isinstance(result, dict) else []
        except Exception as error:
            _check_cancel(cancel_check)
            if _input_error(error):
                raise
            last_message = '原提交暂时查询失败，将继续核对'
            refresh(None, scope=slots)
        _check_cancel(cancel_check)
        if on_progress:
            rows = _rows(latest)
            for slot in sorted(completed - before):
                on_progress('video_done', {'index': slot, 'video': dict(rows[slot]),
                                          'current': len(completed), 'total': len(targets)})
        return outcomes

    def run_round(slots):
        nonlocal last_message
        _check_cancel(cancel_check)
        publish('retrying', targets if slots is None else slots,
                '正在生成视频' if retry_round == 0 else '正在补齐尚未成功的视频')
        for slot in (targets if slots is None else slots):
            attempts[slot] = attempts.get(slot, 0) + 1
        try:
            refresh(generate(slots))
        except Exception as error:
            _check_cancel(cancel_check)
            if _input_error(error):
                raise
            last_message = str(error)
            refresh(None)
        _check_cancel(cancel_check)

    try:
        _check_cancel(cancel_check)
        if not recovery_only:
            held = held_slots(targets, [])
            external_predecessors = chain_blockers(targets) - targets
            if held or external_predecessors:
                # A retained operation can outlive its original worker. Query it
                # before even the first round rather than issuing another POST.
                outcomes = query(held | external_predecessors)
                if target_slots is None:
                    # Whole-order run: a pending slot only blocks itself. Delivered
                    # slots are reused, not resubmitted as an explicit retry list.
                    completed.update((_existing_deliveries(latest, directory) & targets) - held)
                first_submissions = targets - completed - held_slots(targets, outcomes)
                first_submissions = choose_retry_slots(first_submissions)
                if first_submissions:
                    run_round(sorted(first_submissions))
            else:
                # Preserve explicit target regeneration semantics for delivered
                # assets and unknown submissions, which have no accepted task.
                run_round(target_slots)
        if first_pass_only:
            state = publish(phase, targets - completed,
                            '本批就绪视频已处理，继续等待其余片段就绪')
            result = dict(latest)
            result['video_recovery'] = state
            result.setdefault('project_dir', directory)
            result.setdefault('manifest', '/' + os.path.relpath(
                os.path.join(directory, 'manifest.json'), os.path.dirname(__file__)).replace('\\', '/'))
            return result
        while targets - completed - gave_up:
            remaining = targets - completed - gave_up
            _blocked(latest, remaining)
            retry_round += 1
            outcomes = query(remaining)
            remaining = targets - completed - gave_up
            if not remaining:
                break
            _blocked(latest, remaining)
            delay = delays[min(retry_round - 1, len(delays) - 1)]
            publish('waiting', remaining,
                    '等待后继续核对和补齐视频' + (f'：{last_message}' if last_message else ''),
                    retry_at=time.time() + delay)
            wait_fn(delay, cancel_check)
            _check_cancel(cancel_check)
            if not choose_retry_slots(remaining - held_slots(remaining, outcomes)):
                # Accepted operations only need another observation after waiting.
                continue
            # Query again immediately before a new paid submission: the original
            # could have completed while this worker was sleeping.
            outcomes = query(remaining)
            remaining = targets - completed - gave_up
            if not remaining:
                break
            _blocked(latest, remaining)
            retryable = remaining - held_slots(remaining, outcomes)
            retryable = choose_retry_slots(retryable)
            exhausted = {slot for slot in retryable if attempts.get(slot, 0) >= attempt_limit}
            if exhausted:
                gave_up.update(exhausted)
                retryable -= exhausted
                if is_chain:
                    # Every later chain segment depends on this one.
                    break
            if retryable:
                run_round(sorted(retryable))
        if gave_up:
            raise VideoRecoveryExhaustedError(
                {slot: slot_failure_message(latest, slot) for slot in gave_up},
                exhausted_message(gave_up, attempts, latest))
        state = publish(phase, [], '本次目标视频已全部完成', status='complete')
        result = dict(latest)
        clear_completed_stats(result)
        result['video_recovery'] = state
        result.setdefault('project_dir', directory)
        result.setdefault('manifest', '/' + os.path.relpath(
            os.path.join(directory, 'manifest.json'), os.path.dirname(__file__)).replace('\\', '/'))
        return result
    except ConnectionError as error:
        refresh(None)
        cancelled = _cancelled(cancel_check)
        publish(phase, targets - completed,
                '视频生成已取消，已保留成功视频' if cancelled else str(error),
                status='cancelled' if cancelled else 'failed')
        raise
    except Exception as error:
        publish(phase, targets - completed, str(error), status='failed')
        raise
