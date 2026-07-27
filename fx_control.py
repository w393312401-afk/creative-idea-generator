"""Google FX control plane: ordered admission, dispatch state and audit trail.

FX_CONTROL 是驱动 AdsPower/Flow 浏览器的**唯一** admission 入口。所有需要浏览器的
动作（帧/视频/分步渲染/自治管线、积分探针、自检、选择器探针）都必须经过
`slot()`，这样"谁在占用浏览器、谁在排队、为什么进不去"才是可观测的单一事实。

设计要点：
- 可重入（2026-07-26）：同一执行上下文里嵌套取 slot 直接放行。号池选号时的
  stale 积分探测就发生在生成任务已经持有 slot 的线程里，不放行会自锁。
- 队列等待可取消、可超时；执行中超时由外部看门狗按 `overdue_active()` 处理
  （控制面不能杀线程，只能把超时事实暴露出来）。
- waiting/active 落盘：进程重启后上次的 active 会被回放成 orphaned 记录，
  控制台能回答"上次为什么中断"。
- 审计按大小轮转，读取从文件尾部反向进行，避免文件越长状态接口越慢。
"""

import contextlib
import contextvars
import json
import os
import threading
import time
from datetime import datetime, timezone


class FxQueueCancelled(ConnectionError):
    """任务在等待浏览器队列时被取消。"""


class FxQueueTimeout(TimeoutError):
    """任务等待浏览器队列超过 queue_wait_timeout_seconds。"""


_CURRENT_TASK_ID = contextvars.ContextVar('spark_fx_task_id', default=None)
_SLOT_DEPTH = contextvars.ContextVar('spark_fx_slot_depth', default=0)
_ACCOUNT_PIN = contextvars.ContextVar('spark_fx_account_pin', default=None)

# 审计文件轮转参数（可用环境变量覆盖，和 logger 的轮转口径保持一致）
AUDIT_MAX_BYTES = int(os.environ.get('SPARK_FX_AUDIT_MAX_BYTES', str(4 * 1024 * 1024)))
AUDIT_BACKUP_COUNT = int(os.environ.get('SPARK_FX_AUDIT_BACKUP_COUNT', '3'))

LIMIT_SPEC = {
    # 同时进入浏览器临界区的任务数。Flow 画布不是并发安全的，默认 1；
    # 调大只在"多 AdsPower profile 并行"验证过之后才有意义。
    'max_concurrent': {'min': 1, 'max': 8, 'default': 1},
    # 单个任务在浏览器里的最长执行时间（秒），0 = 不限。超时由外部看门狗取消。
    'task_timeout_seconds': {'min': 0, 'max': 86400, 'default': 300},
    # 单个任务排队等待的最长时间（秒），0 = 不限。超时抛 FxQueueTimeout。
    'queue_wait_timeout_seconds': {'min': 0, 'max': 86400, 'default': 0},
}

# 按 kind 的并发配额；未列出的 kind 受 max_concurrent 约束即可。
KIND_LIMIT_DEFAULT = {}


def current_fx_task_id():
    return _CURRENT_TASK_ID.get()


def holds_fx_slot():
    """当前执行上下文是否已经在浏览器临界区内。"""
    return _SLOT_DEPTH.get() > 0


def current_account_pin():
    """当前上下文被定向绑定的 AdsPower user_id（未绑定返回 None）。"""
    return _ACCOUNT_PIN.get()


class FxControlPlane:
    """Google FX 浏览器的排队/调度/审计控制面。

    Worker 线程可以在排队期间存在，但只有被选中的任务进入浏览器临界区，
    因此队列顺序与控制模式始终是显式且可观测的。
    """

    def __init__(self, state_path, audit_path):
        self.state_path = str(state_path)
        self.audit_path = str(audit_path)
        self._condition = threading.Condition()
        self._waiting = []
        self._active = {}
        self._sequence = 0
        self._audit_lock = threading.Lock()
        persisted = self._read_json(self.state_path, {})
        self._accepting = persisted.get('accepting', True) is not False
        self._processing_paused = bool(persisted.get('processing_paused', False))
        self._limits = {key: spec['default'] for key, spec in LIMIT_SPEC.items()}
        for key, value in (persisted.get('limits') or {}).items():
            if key in LIMIT_SPEC:
                try:
                    self._limits[key] = self._clamp_limit(key, value)
                except (TypeError, ValueError):
                    pass
        self._kind_limits = {}
        for kind, value in (persisted.get('kind_limits') or {}).items():
            try:
                self._kind_limits[str(kind)] = max(1, int(value))
            except (TypeError, ValueError):
                pass
        # 上次进程退出时仍在执行/排队的任务：回放成孤儿记录，供控制台解释中断原因。
        self._orphaned = []
        for row in list((persisted.get('active') or {}).values()) + list(persisted.get('waiting') or []):
            if isinstance(row, dict) and row.get('task_id'):
                self._orphaned.append({
                    'task_id': row.get('task_id'),
                    'kind': row.get('kind'),
                    'was': 'active' if row.get('started_at') else 'waiting',
                    'seen_at': persisted.get('updated_at'),
                })
        if self._orphaned:
            self.audit('queue.orphaned_on_start',
                       details={'count': len(self._orphaned),
                                'task_ids': [row['task_id'] for row in self._orphaned][:20]})
        self._persist_state()

    # ── 持久化 ────────────────────────────────────────────

    @staticmethod
    def _clamp_limit(key, value):
        spec = LIMIT_SPEC[key]
        value = int(value)
        return max(spec['min'], min(spec['max'], value))

    @staticmethod
    def _read_json(path, fallback):
        try:
            with open(path, 'r', encoding='utf-8') as handle:
                return json.load(handle)
        except Exception:
            return fallback

    def _persist_state(self):
        """把控制模式 + 队列现状写盘。调用方必须已持有 _condition 或确保一致性。"""
        payload = {
            'accepting': self._accepting,
            'processing_paused': self._processing_paused,
            'limits': dict(self._limits),
            'kind_limits': dict(self._kind_limits),
            'active': {task_id: dict(row) for task_id, row in self._active.items()},
            'waiting': [dict(row) for row in self._waiting],
            'updated_at': datetime.now(timezone.utc).astimezone().isoformat(),
        }
        try:
            os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
            tmp = self.state_path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
            os.replace(tmp, self.state_path)
        except Exception:
            # 队列状态落盘只服务于"重启后能解释中断"，写不下去不应该影响调度本身。
            pass

    # ── 审计 ──────────────────────────────────────────────

    def _rotate_audit_if_needed(self):
        try:
            if os.path.getsize(self.audit_path) < AUDIT_MAX_BYTES:
                return
        except OSError:
            return
        for index in range(AUDIT_BACKUP_COUNT - 1, 0, -1):
            src = f'{self.audit_path}.{index}'
            dst = f'{self.audit_path}.{index + 1}'
            if os.path.exists(src):
                try:
                    os.replace(src, dst)
                except OSError:
                    pass
        try:
            os.replace(self.audit_path, f'{self.audit_path}.1')
        except OSError:
            pass

    def audit(self, action, task_id=None, details=None, actor='local'):
        row = {
            'at': datetime.now(timezone.utc).astimezone().isoformat(),
            'action': action,
            'task_id': task_id,
            'actor': actor,
            'details': details or {},
        }
        try:
            with self._audit_lock:
                os.makedirs(os.path.dirname(self.audit_path), exist_ok=True)
                self._rotate_audit_if_needed()
                with open(self.audit_path, 'a', encoding='utf-8') as handle:
                    handle.write(json.dumps(row, ensure_ascii=False, default=str) + '\n')
        except Exception:
            pass
        return row

    def recent_audit(self, limit=50, action_prefix=None, task_id=None):
        """从文件尾部反向读取审计行，最新的在前。

        全量读 + 全行 json.loads 会让状态接口随审计文件增长线性变慢，所以这里
        只按需要的行数从尾部回读固定大小的块。
        """
        limit = max(1, min(int(limit), 500))
        wanted = limit if (action_prefix or task_id) else limit
        rows = []
        try:
            with open(self.audit_path, 'rb') as handle:
                handle.seek(0, os.SEEK_END)
                position = handle.tell()
                buffer = b''
                # 过滤条件下可能要翻更多行才能凑够 limit 条，最多回读 2MB。
                budget = 2 * 1024 * 1024 if (action_prefix or task_id) else 512 * 1024
                chunk_size = 64 * 1024
                read_total = 0
                while position > 0 and len(rows) < wanted and read_total < budget:
                    step = min(chunk_size, position)
                    position -= step
                    handle.seek(position)
                    buffer = handle.read(step) + buffer
                    read_total += step
                    lines = buffer.split(b'\n')
                    buffer = lines.pop(0) if position > 0 else b''
                    for raw in reversed(lines):
                        if not raw.strip():
                            continue
                        try:
                            row = json.loads(raw.decode('utf-8'))
                        except Exception:
                            continue
                        if action_prefix and not str(row.get('action', '')).startswith(action_prefix):
                            continue
                        if task_id and str(row.get('task_id') or '') != str(task_id):
                            continue
                        rows.append(row)
                        if len(rows) >= wanted:
                            break
        except Exception:
            return []
        return rows[:limit]

    # ── 控制模式与限额 ────────────────────────────────────

    def admission(self, kind=None):
        with self._condition:
            if self._accepting:
                return True, None
            mode = 'paused' if self._processing_paused else 'draining'
            return False, f'Google FX 服务当前处于 {mode} 模式，暂不接收新任务'

    def set_mode(self, action, actor='local'):
        with self._condition:
            if action == 'resume':
                self._accepting = True
                self._processing_paused = False
            elif action == 'pause':
                self._accepting = False
                self._processing_paused = True
            elif action == 'drain':
                self._accepting = False
                self._processing_paused = False
            else:
                raise ValueError('action 必须是 resume、pause 或 drain')
            self._persist_state()
            self._condition.notify_all()
            snapshot = self._snapshot_locked()
        self.audit(f'control.{action}', details={'mode': snapshot['mode']}, actor=actor)
        return snapshot

    def set_limits(self, patch, actor='local'):
        """更新调度限额：max_concurrent / task_timeout_seconds / queue_wait_timeout_seconds
        以及按 kind 的并发配额 kind_limits（{kind: 并发数}）。"""
        if not isinstance(patch, dict) or not patch:
            raise ValueError('limits 必须是非空对象')
        unknown = sorted(set(patch) - set(LIMIT_SPEC) - {'kind_limits'})
        if unknown:
            raise ValueError(f'不支持的限额字段: {", ".join(unknown)}')
        before = {}
        with self._condition:
            for key, value in patch.items():
                if key == 'kind_limits':
                    if not isinstance(value, dict):
                        raise ValueError('kind_limits 必须是对象')
                    cleaned = {}
                    for kind, raw in value.items():
                        try:
                            slots = int(raw)
                        except (TypeError, ValueError):
                            raise ValueError(f'kind_limits.{kind} 必须是整数')
                        if slots < 1 or slots > LIMIT_SPEC['max_concurrent']['max']:
                            raise ValueError(f'kind_limits.{kind} 必须在 1~{LIMIT_SPEC["max_concurrent"]["max"]} 之间')
                        cleaned[str(kind)] = slots
                    before['kind_limits'] = dict(self._kind_limits)
                    self._kind_limits = cleaned
                    continue
                try:
                    clean = self._clamp_limit(key, value)
                except (TypeError, ValueError):
                    raise ValueError(f'{key} 必须是整数')
                before[key] = self._limits[key]
                self._limits[key] = clean
            self._persist_state()
            self._condition.notify_all()
            snapshot = self._snapshot_locked()
        self.audit('control.limits', details={'before': before, 'after': self.limits()}, actor=actor)
        return snapshot

    def limits(self):
        with self._condition:
            return {**self._limits, 'kind_limits': dict(self._kind_limits)}

    def reprioritize(self, task_id, priority, actor='local'):
        priority = max(-100, min(100, int(priority)))
        with self._condition:
            entry = next((row for row in self._waiting if row['task_id'] == str(task_id)), None)
            if entry is None:
                raise KeyError('任务不在等待队列中')
            before = entry['priority']
            entry['priority'] = priority
            self._persist_state()
            self._condition.notify_all()
        self.audit('queue.reprioritize', task_id, {'before': before, 'after': priority}, actor)
        return self.snapshot()

    def pin_account(self, task_id, user_id, actor='local'):
        """给还在排队的任务定向绑定一个 AdsPower 账号；空值 = 解除绑定。

        任务进入临界区时 slot() 会把它写进 _ACCOUNT_PIN 上下文，
        utils/account_binding 的解析顺序保证浏览器落到这个账号上。
        """
        user_id = str(user_id or '').strip()
        with self._condition:
            entry = next((row for row in self._waiting if row['task_id'] == str(task_id)), None)
            if entry is None:
                raise KeyError('只能给排队中的任务定向账号（已在执行的任务请先取消）')
            before = entry.get('account_pin') or ''
            entry['account_pin'] = user_id or None
            self._persist_state()
        self.audit('queue.pin_account', task_id, {'before': before, 'after': user_id}, actor)
        return self.snapshot()

    # ── 快照 ──────────────────────────────────────────────

    def _ordered_waiting(self):
        return sorted(self._waiting, key=lambda row: (-row['priority'], row['sequence']))

    def _kind_capacity(self, kind):
        return self._kind_limits.get(str(kind), self._limits['max_concurrent'])

    def _next_startable(self):
        """返回当前可以放行的等待条目（按优先级/FIFO，跳过被 kind 配额挡住的）。

        不做严格队首阻塞：某个 kind 的配额用满时，后面其它 kind 的任务可以先走，
        否则一个 kind 的配额上限会把整条队列堵死。
        """
        if self._processing_paused:
            return None
        if len(self._active) >= self._limits['max_concurrent']:
            return None
        active_by_kind = {}
        for row in self._active.values():
            active_by_kind[row['kind']] = active_by_kind.get(row['kind'], 0) + 1
        for entry in self._ordered_waiting():
            if active_by_kind.get(entry['kind'], 0) < self._kind_capacity(entry['kind']):
                return entry
        return None

    def _snapshot_locked(self):
        now = time.time()
        waiting = self._ordered_waiting()
        mode = ('paused' if self._processing_paused else
                ('running' if self._accepting else 'draining'))
        active_rows = []
        for row in self._active.values():
            elapsed = now - row['started_at']
            timeout = self._limits['task_timeout_seconds']
            active_rows.append({
                **row,
                'elapsed_seconds': round(elapsed, 1),
                'overdue': bool(timeout and elapsed > timeout),
            })
        active_rows.sort(key=lambda row: row['started_at'])
        return {
            'mode': mode,
            'accepting': self._accepting,
            'processing_paused': self._processing_paused,
            # active 保持"单任务对象"形状以兼容既有前端；并发 >1 时看 active_list。
            'active': dict(active_rows[0]) if active_rows else None,
            'active_list': active_rows,
            'active_count': len(active_rows),
            'waiting': [{**row, 'wait_seconds': round(now - row['enqueued_at'], 1)}
                        for row in waiting],
            'waiting_count': len(waiting),
            'limits': {**self._limits, 'kind_limits': dict(self._kind_limits)},
            'orphaned': list(self._orphaned),
        }

    def snapshot(self):
        with self._condition:
            return self._snapshot_locked()

    def overdue_active(self):
        """执行时间超过 task_timeout_seconds 的活跃任务（看门狗据此发取消信号）。"""
        with self._condition:
            timeout = self._limits['task_timeout_seconds']
            if not timeout:
                return []
            now = time.time()
            return [{**row, 'elapsed_seconds': round(now - row['started_at'], 1)}
                    for row in self._active.values() if now - row['started_at'] > timeout]

    def clear_orphaned(self, actor='local'):
        with self._condition:
            count = len(self._orphaned)
            self._orphaned = []
            self._persist_state()
        if count:
            self.audit('queue.orphaned_cleared', details={'count': count}, actor=actor)
        return count

    def force_release_active(self, task_id=None, actor='local'):
        """强制释放卡在临界区的底层任务（或清空全部卡死槽位）。"""
        released = []
        with self._condition:
            if task_id:
                tid = str(task_id).strip()
                row = self._active.pop(tid, None)
                if row:
                    released.append(row)
            else:
                released = list(self._active.values())
                self._active.clear()
            if released:
                self._persist_state()
                self._condition.notify_all()
        now = time.time()
        for row in released:
            tid = row['task_id']
            self.audit('queue.force_released', tid, {
                'kind': row.get('kind'),
                'elapsed_seconds': round(now - row.get('started_at', now), 1)
            }, actor=actor)
        return len(released)

    # ── 临界区 ────────────────────────────────────────────

    @contextlib.contextmanager
    def slot(self, task_id, kind, cancel_check=None, priority=0, account_pin=None):
        """排队进入浏览器临界区。

        同一执行上下文里嵌套调用直接放行（例如生成任务里触发的积分探测）——
        重新排一次队会自锁：外层已经占着唯一的 active 名额。
        """
        task_id = str(task_id)
        kind = str(kind or 'fx')
        if holds_fx_slot():
            self.audit('queue.reentrant', task_id, {'kind': kind, 'outer': current_fx_task_id()})
            depth = _SLOT_DEPTH.set(_SLOT_DEPTH.get() + 1)
            try:
                yield
            finally:
                _SLOT_DEPTH.reset(depth)
            return

        with self._condition:
            self._sequence += 1
            entry = {
                'task_id': task_id,
                'kind': kind,
                'priority': max(-100, min(100, int(priority))),
                'sequence': self._sequence,
                'enqueued_at': time.time(),
                'account_pin': str(account_pin).strip() if account_pin else None,
            }
            self._waiting.append(entry)
            self._persist_state()
            self._condition.notify_all()

            wait_timeout = self._limits['queue_wait_timeout_seconds']
            while True:
                if cancel_check and cancel_check():
                    self._waiting = [row for row in self._waiting if row is not entry]
                    self._persist_state()
                    self._condition.notify_all()
                    self.audit('queue.cancelled_before_start', task_id, {'kind': kind})
                    raise FxQueueCancelled('任务在等待 Google FX 队列时被取消')
                if wait_timeout and (time.time() - entry['enqueued_at']) > wait_timeout:
                    self._waiting = [row for row in self._waiting if row is not entry]
                    self._persist_state()
                    self._condition.notify_all()
                    self.audit('queue.wait_timeout', task_id,
                               {'kind': kind, 'waited_seconds': round(time.time() - entry['enqueued_at'], 1)})
                    raise FxQueueTimeout(
                        f'任务等待 Google FX 队列超过 {wait_timeout}s 仍未获得浏览器，已放弃')
                if self._next_startable() is entry:
                    self._waiting.remove(entry)
                    entry = {**entry, 'started_at': time.time()}
                    self._active[task_id] = entry
                    self._persist_state()
                    break
                self._condition.wait(timeout=0.25)

        task_token = _CURRENT_TASK_ID.set(task_id)
        depth_token = _SLOT_DEPTH.set(1)
        pin_token = _ACCOUNT_PIN.set(entry.get('account_pin'))
        self.audit('queue.started', task_id, {
            'kind': kind,
            'priority': entry['priority'],
            'waited_seconds': round(entry['started_at'] - entry['enqueued_at'], 1),
            'account_pin': entry.get('account_pin'),
        })
        try:
            yield
        finally:
            _ACCOUNT_PIN.reset(pin_token)
            _SLOT_DEPTH.reset(depth_token)
            _CURRENT_TASK_ID.reset(task_token)
            with self._condition:
                row = self._active.pop(task_id, None)
                duration = round(time.time() - row['started_at'], 2) if row else None
                self._persist_state()
                self._condition.notify_all()
            self.audit('queue.released', task_id, {'duration_seconds': duration})


PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
FX_CONTROL = FxControlPlane(
    os.path.join(PROJECT_ROOT, 'runtime', 'fx_control_state.json'),
    os.path.join(PROJECT_ROOT, 'runtime', 'fx_audit.jsonl'),
)
