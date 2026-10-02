"""Google FX control plane: lightweight browser mutex lock and execution status.

FX_CONTROL 是驱动 AdsPower/Flow 浏览器的互斥锁入口。所有需要浏览器的动作
（帧/视频/分步渲染/自治管线、积分探针、自检、选择器探针）都经过 `slot()`，
保证同一时间仅有 1 个任务操作 Flow 画布，防止多请求互相踩踏。

设计要点：
- 单一互斥（Mutex Lock）：同时间仅允许 1 个任务进入浏览器临界区。
- 上下文可重入：同一执行上下文（线程/Task）中嵌套请求 slot 时直接放行，防止自锁。
- 轻量无依赖：移除复杂的优先级排队、多重超时与排空状态机，轻量稳定。
"""

import collections
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

# 作战板（并发可视化 P0）：只读观测，不参与任何加锁/放行决策。
# 历史环形缓冲只存内存——泳道回看 30 分钟足够，重启后清空是可接受的。
BOARD_HISTORY_MAX = int(os.environ.get('SPARK_FX_BOARD_HISTORY_MAX', '300'))
BOARD_HISTORY_WINDOW_SECONDS = 30 * 60

# 租约心跳超过这个秒数没刷新，就在作战板上标成"疑似卡死"（只标记，绝不自动处理）。
# 心跳 = 该任务的每一条日志（utils/logger.log → lease_registry.touch）。默认 300s：
# 视频提交后的轮询等待里日志可能稀疏，120s 会把正常等待误报成卡死。
LEASE_STALL_SECONDS = int(os.environ.get('SPARK_FX_LEASE_STALL_SECONDS', '300'))
LEASE_STATE_SCHEMA = 1

# 并发上限的硬天花板：每个 AdsPower 环境都是一个完整的 Chromium，内存是真实约束。
MAX_CONCURRENT_HARD_LIMIT = 4


def current_fx_task_id():
    return _CURRENT_TASK_ID.get()


def holds_fx_slot():
    """当前执行上下文是否已经在浏览器临界区内。"""
    return _SLOT_DEPTH.get() > 0


def current_account_pin():
    """当前上下文被定向绑定的 AdsPower user_id（未绑定返回 None）。"""
    return _ACCOUNT_PIN.get()


class FxControlPlane:
    """Google FX 浏览器的租约控制面。

    并发方案 P2：同一时刻最多 N 份租约（N = SPARK_FX_MAX_CONCURRENT，默认 1、硬上限 4），
    每份租约独占一个 AdsPower 环境。放行不再靠一把锁抢，而是由准入条件决定：
      capacity  已用名额 < N
      account   想用的账号没被别的租约占着（R1）
      ip        想用的账号的出口与别的租约相同（R5，策略 hard 时）
      project   同一项目同时只有一个租约（R2/R12：同项目的帧与视频不会并行写 manifest）
    排队按（优先级 → 先到先得）顺序，被挡住的不阻塞后面能放行的。
    N=1 时容量条件等价于旧的互斥锁，行为只多了"按序放行"。
    """

    def __init__(self, state_path=None, audit_path=None):
        self.state_path = str(state_path or '')
        self.audit_path = str(audit_path or '')
        self._active_lock = threading.Lock()
        self._cond = threading.Condition(self._active_lock)
        self._audit_lock = threading.Lock()
        # 状态文件的写入必须串行：N>1 时多个任务会同时授予/释放租约，共用一个临时文件名会写坏 JSON。
        self._persist_lock = threading.Lock()
        # 全部受 _active_lock 保护。
        self._leases = {}        # seg_id -> lease dict（含被强制释放、线程尚未退出的"僵尸"租约）
        self._waiting = {}
        self._history = collections.deque(maxlen=max(10, BOARD_HISTORY_MAX))
        self._seq = 0
        self._stall_reported = set()
        # 宿主注入：user_id -> 出口身份字符串（None=未知）。只在 N>1 时才会被调用。
        self.egress_resolver = None
        # 上一个进程退出时还占着浏览器的租约（崩溃/被杀）；只在启动时从状态文件读一次。
        self.recovered = self._load_recovered_lease()

    # ── 兼容层 ────────────────────────────────────────────

    @property
    def _active(self):
        """旧接口：最早的、未被强制释放的租约。调用方须持有 _active_lock 或接受弱一致。"""
        live = [row for row in self._leases.values() if not row.get('force_released')]
        return min(live, key=lambda row: row['seg_id']) if live else None

    # ── 配置 ──────────────────────────────────────────────

    @staticmethod
    def _max_concurrent():
        try:
            value = int(os.environ.get('SPARK_FX_MAX_CONCURRENT', '1'))
        except (TypeError, ValueError):
            value = 1
        return max(1, min(MAX_CONCURRENT_HARD_LIMIT, value))

    @staticmethod
    def _egress_policy():
        return 'warn' if os.environ.get('SPARK_FX_EGRESS_POLICY', 'hard').strip().lower() == 'warn' else 'hard'

    def max_concurrent(self):
        return self._max_concurrent()

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
            'max_concurrent': self._max_concurrent(),
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
            live = sorted((row for row in self._leases.values() if not row.get('force_released')),
                          key=lambda row: row['seg_id'])
            active_copy = dict(live[0]) if live else None
            waiting = self._waiting_rows_locked()
        return {
            'mode': 'accepting',
            'active': active_copy,
            'active_count': len(live),
            # 注意：不要在这里加 active_list。server.py 的积分刷新/测试登录接口读到
            # 非空 active_list 会改成对生成类任务回 409（FX_BUSY），那是行为变化；
            # 作战板用 board() 自己的 leases 字段，不走这条路径。
            'waiting': waiting,
            'waiting_count': len(waiting),
            'busy': bool(live),
            'limits': self.limits(),
            'kind_limits': {},
            'orphaned': [],
            'orphaned_count': 0,
        }

    def overdue_active(self):
        return []

    def _force_release_locked(self, task_id):
        """把匹配的租约标成"已强制释放"。名额**不**释放：底层线程还在跑，放行下一个任务
        会造成并发污染（与旧版"槽位仍保持占用"一致）。线程真正退出时才回收名额。"""
        freed = []
        for row in self._leases.values():
            if row.get('force_released'):
                continue
            if task_id is None or row.get('task_id') == str(task_id):
                row['force_released'] = True
                self._record_history_locked(row, 'force_released')
                freed.append(dict(row))
        return freed

    def release_stuck(self, task_id=None):
        """紧急释放卡死的 active 状态。"""
        with self._active_lock:
            freed = self._force_release_locked(task_id)
        if freed:
            self._persist_lease()
        return freed

    def force_release(self, task_id=None):
        return self.release_stuck(task_id)

    def force_release_active(self, task_id=None, actor='local'):
        with self._active_lock:
            freed = self._force_release_locked(task_id)
        if freed:
            self.audit('control.force_release', task_id=task_id, actor=actor)
            self._persist_lease()
            return len(freed)
        return 0

    def clear_orphaned(self, actor='local'):
        return self.snapshot()

    # ── 准入判定（调用方须持有 _active_lock）──────────────────

    @staticmethod
    def _holder_of(lease):
        return lease.get('user_id') or lease.get('account_pin') or None

    def _blockers_locked(self, waiter):
        """列出 waiter 当前被什么挡住；空列表 = 可以放行。"""
        blockers = []
        leases = sorted(self._leases.values(), key=lambda row: row['seg_id'])
        capacity = self._max_concurrent()
        if len(leases) >= capacity and leases:
            blockers.append({'type': 'capacity', 'holder_task': leases[0]['task_id']})
        want = waiter.get('want_account')
        if want:
            holder = next((row for row in leases if self._holder_of(row) == want), None)
            if holder:
                blockers.append({'type': 'account', 'holder_task': holder['task_id'], 'rule': 'R1'})
            elif self._egress_policy() == 'hard' and waiter.get('want_egress'):
                clash = next((row for row in leases if row.get('egress_id') == waiter['want_egress']), None)
                if clash:
                    blockers.append({'type': 'ip', 'holder_task': clash['task_id'], 'rule': 'R5'})
        project = waiter.get('project_key')
        if project:
            clash = next((row for row in leases if row.get('project_key') == project), None)
            if clash:
                blockers.append({'type': 'project', 'holder_task': clash['task_id'], 'rule': 'R2',
                                 'enforced': True})
        return blockers

    def _ordered_waiting_locked(self):
        """放行顺序：优先级高者先 → 先到先得。（旁路探针自带更高优先级：它们很短，选号要靠结果。）"""
        return sorted(self._waiting.values(), key=lambda row: (
            -(row.get('priority') or 0), row['since_ts'], row['seq']))

    def _next_admissible_locked(self):
        for row in self._ordered_waiting_locked():
            if not self._blockers_locked(row):
                return row['task_id']
        return None

    # ── 核心入口 ──────────────────────────────────────────

    @contextlib.contextmanager
    def slot(self, task_id, kind='task', account_pin=None, cancel_check=None, wait_timeout=None, priority=0,
             project_key=None, stage=None, want_account=None, **kwargs):
        """排队申请一份浏览器租约。支持同上下文嵌套重入（嵌套的旁路动作复用外层租约）。

        project_key/stage：归属标注，同时用于项目互斥（R2）。
        want_account：任务明确要用的 AdsPower 环境（用户指定/探针目标）；它被别的租约占着，
        或出口与别的租约相同时，本任务继续排队。没指定（自动选号）的任务不受此限，
        选号时由 claim_account 原子占账号。
        """
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

        want_account = str(want_account).strip() if want_account else None
        want_egress = self._egress_of(want_account) if want_account else None
        start_wait = time.time()
        self._register_waiting(task_id, kind, account_pin, priority, project_key, stage,
                               want_account, want_egress)
        lease = None
        try:
            while lease is None:
                if cancel_check and cancel_check():
                    raise FxQueueCancelled(f'任务 {task_id} 在等待浏览器时被取消')
                if wait_timeout and (time.time() - start_wait) > wait_timeout:
                    raise FxQueueTimeout(f'任务 {task_id} 等待浏览器超时（{wait_timeout}s）')
                with self._cond:
                    if self._next_admissible_locked() == task_id:
                        lease = self._grant_locked(task_id, kind, account_pin, project_key, stage,
                                                   want_account, want_egress)
                    else:
                        self._cond.wait(timeout=0.1)
        except BaseException:
            self._unregister_waiting(task_id)
            raise

        token_id = _CURRENT_TASK_ID.set(task_id)
        token_depth = _SLOT_DEPTH.set(1)
        token_pin = _ACCOUNT_PIN.set(account_pin or None)
        self._persist_lease()
        self.audit('lease.grant', task_id=task_id, details={
            'lease_id': lease['lease_id'], 'kind': str(kind or 'task'),
            'project_key': project_key or '', 'stage': stage or '',
            'waited_seconds': round(time.time() - start_wait, 1)})
        outcome = 'ok'
        try:
            yield
        except BaseException as exc:
            # GenerationCancelled 等取消信号都带 Cancel 字样；其余一律记为 error。
            outcome = 'cancelled' if 'Cancel' in type(exc).__name__ else 'error'
            raise
        finally:
            with self._cond:
                row = self._leases.pop(lease['seg_id'], None)
                if row is not None and not row.get('force_released'):
                    self._record_history_locked(row, outcome)
                self._stall_reported.discard(lease['seg_id'])
                self._cond.notify_all()
            if row is not None:
                self._persist_lease()
                self.audit('lease.release', task_id=task_id, details={
                    'lease_id': row.get('lease_id'), 'outcome': outcome,
                    'user_id': row.get('user_id'), 'force_released': bool(row.get('force_released')),
                    'held_seconds': round(time.time() - row['started_ts'], 1)})
            _ACCOUNT_PIN.reset(token_pin)
            _SLOT_DEPTH.reset(token_depth)
            _CURRENT_TASK_ID.reset(token_id)

    def _grant_locked(self, task_id, kind, account_pin, project_key, stage, want_account, want_egress):
        self._waiting.pop(task_id, None)
        self._seq += 1
        started_ts = time.time()
        lease = {
            'task_id': task_id,
            'kind': str(kind or 'task'),
            'account_pin': account_pin,
            'started_at': datetime.now(timezone.utc).astimezone().isoformat(),
            'started_ts': started_ts,
            'seg_id': self._seq,
            'lease_id': f'L_{self._seq}',
            # 明确要用的账号在授予的一刻就写进租约，别的任务选号时立刻看得见。
            'user_id': account_pin or want_account or None,
            'egress_id': want_egress,
            'project_key': project_key or '',
            'stage': stage or '',
            'heartbeat_ts': started_ts,
        }
        self._leases[self._seq] = lease
        return lease

    # ── 账号占用（选号的原子步骤）────────────────────────────

    def _own_lease_locked(self, task_id):
        return next((row for row in self._leases.values()
                     if row['task_id'] == task_id and not row.get('force_released')), None)

    def _egress_of(self, user_id):
        """账号的出口身份。只在并发（N>1）时查，且在任何锁之外调用（可能走 AdsPower 接口）。"""
        if not user_id or self._max_concurrent() <= 1 or self.egress_resolver is None:
            return None
        try:
            return self.egress_resolver(str(user_id)) or None
        except Exception:
            return None

    def claim_account(self, user_id):
        """当前租约改占 user_id；与别的租约冲突（同账号 R1 / 同出口 R5）返回 False。

        选号 + 占用在同一个临界区里完成，两个任务不可能同时拿到同一个账号。
        N=1 时没有"别的租约"，永远返回 True，与旧行为一致。
        """
        user_id = str(user_id or '').strip()
        task_id = _CURRENT_TASK_ID.get()
        if not user_id or not task_id:
            return True
        egress = self._egress_of(user_id)
        reason = None
        with self._cond:
            mine = self._own_lease_locked(task_id)
            if mine is None:
                return True
            for other in self._leases.values():
                if other is mine:
                    continue
                if self._holder_of(other) == user_id:
                    reason = ('account', other['task_id'])
                    break
                if (self._egress_policy() == 'hard' and egress and other.get('egress_id') == egress
                        and self._holder_of(other) != user_id):
                    reason = ('ip', other['task_id'])
                    break
            if reason is None:
                mine['user_id'] = user_id
                mine['egress_id'] = egress or mine.get('egress_id')
        if reason:
            self.audit('lease.claim_denied', task_id=task_id, details={
                'user_id': user_id, 'reason': reason[0], 'holder_task': reason[1]})
            return False
        if egress is None and self._max_concurrent() > 1 and self.egress_resolver is not None:
            self.audit('egress.unknown', task_id=task_id, details={'user_id': user_id})
        return True

    def current_claim(self):
        task_id = _CURRENT_TASK_ID.get()
        with self._active_lock:
            mine = self._own_lease_locked(task_id) if task_id else None
            return mine.get('user_id') if mine else None

    def restore_claim(self, user_id):
        """选号失败时把租约的账号恢复成选号前的样子（不做冲突判断）。"""
        task_id = _CURRENT_TASK_ID.get()
        if not task_id:
            return
        with self._active_lock:
            mine = self._own_lease_locked(task_id)
            if mine is not None:
                mine['user_id'] = user_id or mine.get('account_pin') or None

    # ── 作战板观测（只读）──────────────────────────────────

    def _register_waiting(self, task_id, kind, account_pin, priority, project_key=None, stage=None,
                          want_account=None, want_egress=None):
        with self._cond:
            self._seq += 1
            self._waiting[task_id] = {
                'task_id': task_id,
                'kind': str(kind or 'task'),
                'account_pin': account_pin,
                'priority': priority or 0,
                'project_key': project_key or '',
                'stage': stage or '',
                'want_account': want_account,
                'want_egress': want_egress,
                'since_ts': time.time(),
                'seq': self._seq,
            }

    def _unregister_waiting(self, task_id):
        with self._cond:
            self._waiting.pop(task_id, None)
            self._cond.notify_all()

    def _waiting_rows_locked(self):
        """调用方须持有 _active_lock。按放行顺序。"""
        return [dict(row, since=datetime.fromtimestamp(row['since_ts']).astimezone().isoformat())
                for row in self._ordered_waiting_locked()]

    def _record_history_locked(self, row, outcome):
        """调用方须持有 _active_lock。"""
        self._history.append({
            'seg_id': row['seg_id'],
            'lease_id': row.get('lease_id'),
            'task_id': row['task_id'],
            'kind': row['kind'],
            'user_id': row.get('user_id'),
            'project_key': row.get('project_key') or '',
            'stage': row.get('stage') or '',
            'started_ts': row['started_ts'],
            'ended_ts': time.time(),
            'outcome': outcome,
        })

    # ── 租约持久化与崩溃恢复 ──────────────────────────────

    _PERSIST_KEYS = ('lease_id', 'task_id', 'kind', 'user_id', 'project_key', 'stage', 'started_at')

    def _persist_lease(self):
        """把当前全部租约落到状态文件。只为"进程被杀后下次启动能知道谁占着浏览器"，
        写失败不能影响任务（吞掉）。无状态文件路径（单测里的临时控制面）时不写。"""
        if not self.state_path:
            return
        # 先拿写锁再取快照：后到的写入一定带着不早于先到者的状态，最终落盘的就是最新的。
        with self._persist_lock:
            with self._active_lock:
                rows = [{k: row.get(k) for k in self._PERSIST_KEYS}
                        for row in sorted(self._leases.values(), key=lambda r: r['seg_id'])
                        if not row.get('force_released')]
            payload = {
                'schema': LEASE_STATE_SCHEMA,
                'pid': os.getpid(),
                'updated_at': datetime.now(timezone.utc).astimezone().isoformat(),
                'lease': rows[0] if rows else None,   # 旧字段：保持与 P1 的状态文件兼容
                'leases': rows,
            }
            try:
                os.makedirs(os.path.dirname(self.state_path) or '.', exist_ok=True)
                tmp = f'{self.state_path}.{os.getpid()}.tmp'
                with open(tmp, 'w', encoding='utf-8') as handle:
                    json.dump(payload, handle, ensure_ascii=False)
                os.replace(tmp, self.state_path)
            except Exception:
                pass

    def _load_recovered_lease(self):
        """启动时读上一个进程留下的状态：若它退出时还有租约，说明那次任务被打断了。

        只读状态文件并写审计，不碰 AdsPower、不关任何浏览器——是否清理由人决定。
        """
        if not self.state_path:
            return None
        recovered = None
        try:
            with open(self.state_path, 'r', encoding='utf-8') as handle:
                previous = json.load(handle)
            if isinstance(previous, dict) and previous.get('schema') == LEASE_STATE_SCHEMA \
                    and previous.get('pid') != os.getpid():
                rows = previous.get('leases')
                if not isinstance(rows, list):
                    rows = [previous['lease']] if previous.get('lease') else []
                rows = [row for row in rows if isinstance(row, dict)]
                if rows:
                    recovered = dict(rows[0], previous_pid=previous.get('pid'),
                                     recovered_ts=time.time(), others=len(rows) - 1,
                                     all=rows)
        except Exception:
            recovered = None
        if recovered:
            for row in recovered['all']:
                self.audit('lease.recovered', task_id=row.get('task_id'), details=row)
        # 导入模块不得有写盘副作用（单测里会 import server）。遗留租约留在文件里，
        # 由宿主启动流程调 acknowledge_recovery() 清掉，或被下一次授予/释放覆盖。
        return recovered

    def acknowledge_recovery(self):
        """宿主启动时调用：把状态文件改写成当前进程的空租约，避免下次重启重复报告同一次中断。"""
        if self.recovered:
            self._persist_lease()

    # ── 包内租约登记簿（utils/lease_registry）的 provider 接口 ──

    def leased_user_ids(self, exclude_current=True):
        """被租着的 user_id（含被强制释放但线程未退出的）。exclude_current 时不含当前上下文自己的。"""
        current = _CURRENT_TASK_ID.get() if exclude_current else None
        with self._active_lock:
            return {str(self._holder_of(row)) for row in self._leases.values()
                    if self._holder_of(row) and not (current and row['task_id'] == current)}

    def touch(self):
        """当前任务的心跳（每条日志调一次）。只刷新"自己的"租约。"""
        task_id = _CURRENT_TASK_ID.get()
        if not task_id:
            return
        with self._active_lock:
            mine = self._own_lease_locked(task_id)
            if mine is not None:
                mine['heartbeat_ts'] = time.time()

    def note_account(self, user_id):
        """account_binding 的观察钩子：当前执行上下文绑定了某个账号。

        只记到当前租约上（按 contextvar 里的 task_id 对号入座），泳道据此知道占用落在哪个账号。
        这是被动记录，不做冲突判断（真正的互斥在 claim_account）；若发现与别的租约撞号，写审计。
        """
        user_id = str(user_id or '').strip()
        task_id = _CURRENT_TASK_ID.get()
        if not user_id or not task_id:
            return
        clash = None
        with self._active_lock:
            mine = self._own_lease_locked(task_id)
            if mine is not None:
                mine['user_id'] = user_id
                clash = next((row['task_id'] for row in self._leases.values()
                              if row is not mine and self._holder_of(row) == user_id), None)
        if clash:
            self.audit('lease.account_shared', task_id=task_id,
                       details={'user_id': user_id, 'other_task': clash})

    def board(self, now=None):
        """作战板数据：容量、在用租约、等待（含阻塞原因）、近 30 分钟历史。"""
        now = float(now if now is not None else time.time())
        stalled_new = []
        with self._active_lock:
            rows = sorted(self._leases.values(), key=lambda row: row['seg_id'])
            waiting = self._waiting_rows_locked()
            for row in waiting:
                row['blocked_by'] = self._blockers_locked(row)
            history = [dict(row) for row in self._history
                       if now - row['ended_ts'] <= BOARD_HISTORY_WINDOW_SECONDS]
            leases = []
            for active in rows:
                heartbeat_age = max(0.0, now - (active.get('heartbeat_ts') or active['started_ts']))
                stalled = (LEASE_STALL_SECONDS > 0 and heartbeat_age > LEASE_STALL_SECONDS
                           and not active.get('force_released'))
                state = 'force_released' if active.get('force_released') else ('stalled' if stalled else 'running')
                leases.append({
                    'lease_id': active.get('lease_id') or f"L_{active['seg_id']}",
                    'task_id': active['task_id'],
                    'kind': active['kind'],
                    'state': state,
                    'user_id': active.get('user_id'),
                    'account_pin': active.get('account_pin'),
                    'egress_id': active.get('egress_id'),
                    'project_key': active.get('project_key') or '',
                    'stage': active.get('stage') or '',
                    'started_ts': active['started_ts'],
                    'elapsed_seconds': round(max(0.0, now - active['started_ts']), 1),
                    'heartbeat_age_seconds': round(heartbeat_age, 1),
                })
                if stalled and active['seg_id'] not in self._stall_reported:
                    self._stall_reported.add(active['seg_id'])
                    stalled_new.append((active['task_id'], round(heartbeat_age, 1)))
        for task_id, age in stalled_new:
            self.audit('lease.stalled', task_id=task_id, details={'heartbeat_age_seconds': age})
        for index, row in enumerate(waiting):
            row['position'] = index + 1
            row['waited_seconds'] = round(max(0.0, now - row['since_ts']), 1)
        return {
            'capacity': {'max': self._max_concurrent(), 'used': len(leases),
                         'per_project_max': 1, 'egress_policy': self._egress_policy()},
            'leases': leases,
            'waiting': waiting,
            'history': history,
            'recovered': self._recovered_for_board(now),
            'server_ts': now,
        }

    def _recovered_for_board(self, now):
        """上次进程被打断时留下的租约，启动后 30 分钟内显示在作战板上。"""
        recovered = self.recovered
        if recovered and now - recovered.get('recovered_ts', 0) <= BOARD_HISTORY_WINDOW_SECONDS:
            return {k: v for k, v in recovered.items() if k != 'all'}
        return None

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
