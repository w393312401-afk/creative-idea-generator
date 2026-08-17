"""Google FX control plane: lightweight browser mutex lock and execution status.

FX_CONTROL 是驱动 AdsPower/Flow 浏览器的互斥锁入口。所有需要浏览器的动作
（帧/视频/分步渲染/自治管线、积分探针、自检、选择器探针）都经过 `slot()`，
保证同一时间仅有 1 个任务操作 Flow 画布，防止多请求互相踩踏。

设计要点：
- 单一互斥（Mutex Lock）：同时间仅允许 1 个任务进入浏览器临界区。
- 上下文可重入：同一执行上下文（线程/Task）中嵌套请求 slot 时直接放行，防止自锁。
- 轻量无依赖：移除复杂的优先级排队、多重超时与排空状态机，轻量稳定。
"""

import contextlib
import contextvars
import json
import os
import threading
import time
from datetime import datetime, timezone


class FxQueueCancelled(ConnectionError):
    """任务在等待浏览器时被取消。"""


class FxQueueTimeout(TimeoutError):
    """任务等待浏览器超过超时时间。"""


_CURRENT_TASK_ID = contextvars.ContextVar('spark_fx_task_id', default=None)
_SLOT_DEPTH = contextvars.ContextVar('spark_fx_slot_depth', default=0)
_ACCOUNT_PIN = contextvars.ContextVar('spark_fx_account_pin', default=None)

# 审计文件轮转参数
AUDIT_MAX_BYTES = int(os.environ.get('SPARK_FX_AUDIT_MAX_BYTES', str(4 * 1024 * 1024)))
AUDIT_BACKUP_COUNT = int(os.environ.get('SPARK_FX_AUDIT_BACKUP_COUNT', '3'))


def current_fx_task_id():
    return _CURRENT_TASK_ID.get()


def holds_fx_slot():
    """当前执行上下文是否已经在浏览器临界区内。"""
    return _SLOT_DEPTH.get() > 0


def current_account_pin():
    """当前上下文被定向绑定的 AdsPower user_id（未绑定返回 None）。"""
    return _ACCOUNT_PIN.get()


class FxControlPlane:
    """Google FX 浏览器的极简互斥锁与执行状态控制面。"""

    def __init__(self, state_path=None, audit_path=None):
        self.state_path = str(state_path or '')
        self.audit_path = str(audit_path or '')
        self._lock = threading.Lock()
        self._active_lock = threading.Lock()
        self._active = None
        self._audit_lock = threading.Lock()

    # ── 状态与准入 ────────────────────────────────────────

    def admission(self, kind=None):
        """极简准入控制：默认直接放行。"""
        return True, None

    def set_mode(self, action, actor='local'):
        return self.snapshot()

    def set_limits(self, patch, actor='local'):
        return self.snapshot()

    def limits(self):
        return {
            'max_concurrent': 1,
            'task_timeout_seconds': 0,
            'queue_wait_timeout_seconds': 0,
            'kind_limits': {},
        }

    def reprioritize(self, task_id, priority, actor='local'):
        return self.snapshot()

    def pin_account(self, task_id, user_id, actor='local'):
        return self.snapshot()

    def snapshot(self):
        """返回当前的执行状态快照。"""
        with self._active_lock:
            active_copy = dict(self._active) if self._active else None
        return {
            'mode': 'accepting',
            'active': active_copy,
            'active_count': 1 if active_copy else 0,
            'waiting': [],
            'waiting_count': 0,
            'busy': bool(active_copy),
            'limits': self.limits(),
            'kind_limits': {},
            'orphaned': [],
            'orphaned_count': 0,
        }

    def overdue_active(self):
        return []

    def release_stuck(self, task_id=None):
        """紧急释放卡死的 active 状态。"""
        with self._active_lock:
            if self._active and (task_id is None or self._active.get('task_id') == str(task_id)):
                freed = dict(self._active)
                self._active = None
                return [freed]
        return []

    def force_release(self, task_id=None):
        return self.release_stuck(task_id)

    def force_release_active(self, task_id=None, actor='local'):
        with self._active_lock:
            if self._active and (task_id is None or self._active.get('task_id') == str(task_id)):
                freed = dict(self._active)
                self._active = None
                self.audit('control.force_release', task_id=task_id, actor=actor)
                return 1
        return 0

    def clear_orphaned(self, actor='local'):
        return self.snapshot()

    # ── 核心锁入口 ────────────────────────────────────────

    @contextlib.contextmanager
    def slot(self, task_id, kind='task', account_pin=None, cancel_check=None, wait_timeout=None, priority=0, **kwargs):
        """排队进入浏览器临界区。支持同上下文嵌套重入。"""
        task_id = str(task_id or f'anon_{int(time.time() * 1000)}')
        depth = _SLOT_DEPTH.get()

        # 嵌套重入：同一上下文已经持有 slot 时直接放行，防止自锁
        if depth > 0:
            token_depth = _SLOT_DEPTH.set(depth + 1)
            try:
                yield
            finally:
                _SLOT_DEPTH.reset(token_depth)
            return

        # 等待获取互斥锁（支持 cancel_check 与 wait_timeout）
        start_wait = time.time()
        acquired = False
        while not acquired:
            if cancel_check and cancel_check():
                raise FxQueueCancelled(f'任务 {task_id} 在等待浏览器时被取消')
            if wait_timeout and (time.time() - start_wait) > wait_timeout:
                raise FxQueueTimeout(f'任务 {task_id} 等待浏览器超时（{wait_timeout}s）')
            acquired = self._lock.acquire(timeout=0.1)

        token_id = _CURRENT_TASK_ID.set(task_id)
        token_depth = _SLOT_DEPTH.set(1)
        token_pin = _ACCOUNT_PIN.set(account_pin or None)
        with self._active_lock:
            self._active = {
                'task_id': task_id,
                'kind': str(kind or 'task'),
                'account_pin': account_pin,
                'started_at': datetime.now(timezone.utc).astimezone().isoformat(),
            }
        try:
            yield
        finally:
            with self._active_lock:
                self._active = None
            _ACCOUNT_PIN.reset(token_pin)
            _SLOT_DEPTH.reset(token_depth)
            _CURRENT_TASK_ID.reset(token_id)
            self._lock.release()

    # ── 轻量审计 ──────────────────────────────────────────

    def _rotate_audit_if_needed(self):
        if not self.audit_path:
            return
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
        if not self.audit_path:
            return {}
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
        if not self.audit_path:
            return []
        limit = max(1, min(int(limit), 500))
        rows = []
        try:
            with open(self.audit_path, 'rb') as handle:
                handle.seek(0, os.SEEK_END)
                position = handle.tell()
                buffer = b''
                budget = 1 * 1024 * 1024
                chunk_size = 64 * 1024
                read_total = 0
                while position > 0 and len(rows) < limit and read_total < budget:
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
                        if len(rows) >= limit:
                            break
        except Exception:
            return []
        return rows[:limit]


PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
FX_CONTROL = FxControlPlane(
    os.path.join(PROJECT_ROOT, 'runtime', 'fx_control_state.json'),
    os.path.join(PROJECT_ROOT, 'runtime', 'fx_audit.jsonl'),
)
