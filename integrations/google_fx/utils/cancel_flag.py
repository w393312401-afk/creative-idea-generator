# -*- coding: utf-8 -*-
"""
🛑 每请求作用域的取消标志（2026-07 重构 / T2）

设计要点：
- 取消状态只存在于 per-request 的 CancelState 中，通过 contextvar 传递。
  asyncio.create_task 与 anyio.to_thread.run_sync 都会复制当前 context，
  因此同一请求的「端点协程 / 断连监控 / 工作线程」共享同一个 CancelState 实例，
  在其中一个地方置位即可被其它地方读到。
- 不再使用任何进程级全局标志（旧的 builtins.google_fx_cancelled 会让一个请求
  的取消/重置影响到其它并发请求，是严重的请求隔离缺陷，已彻底移除）。
- 对于没有请求上下文的调用方（/cancel_task、/close_browser），通过「活跃请求
  注册表」按 request_id 精确取消，或用 cancel_all() 取消全部在跑请求。
"""
import sys
import time
import threading
import contextvars


class CancelState:
    def __init__(self, request_id: str = None, deadline: float = None):
        self.val = False
        self.request_id = request_id
        self.deadline = deadline   # 绝对超时时刻（time.time() 秒）；None 表示不限时


# 每个请求上下文持有一个 CancelState 实例
_cancel_context_var = contextvars.ContextVar("google_fx_cancel_context", default=None)


class CancelFlag:
    def __init__(self):
        # request_id -> CancelState，供无请求上下文的取消入口精确定位
        self._active = {}
        self._lock = threading.Lock()

    @property
    def is_cancelled(self) -> bool:
        state = _cancel_context_var.get()
        return bool(state.val) if state is not None else False

    @is_cancelled.setter
    def is_cancelled(self, value: bool):
        state = _cancel_context_var.get()
        if state is not None:
            state.val = value
        else:
            # 无请求上下文时不再写任何全局标志（会污染其它并发请求）。
            from .logger import log
            log(
                "⚠️ 在无请求上下文时设置 cancel_flag.is_cancelled 已被忽略；"
                "请改用 cancel_request(request_id) 或 cancel_all()",
                "Warning",
            )

    def init_context(self, request_id: str = None, deadline: float = None) -> "CancelState":
        """为当前请求上下文建立独立的取消状态，并返回该状态对象。"""
        state = CancelState(request_id=request_id, deadline=deadline)
        _cancel_context_var.set(state)
        return state

    def clear_context(self) -> None:
        """丢弃当前上下文的 CancelState（恢复成"没有取消上下文"）。

        给在**复用线程**上临时建上下文的短任务收尾用（积分探针就跑在 HTTP handler
        线程上，而 keep-alive 连接的后续请求复用同一个线程）：不清掉的话，这条带
        deadline 的旧状态会一直留在线程上，下一个复用该线程、自己又没建上下文的
        调用一读 deadline_exceeded() 就是 True，凭空炸出"请求超时：超出时间预算"。
        长任务的 worker 线程不要用它——那边每批都会 init_context 覆盖，而且故意留着
        已置位的旧状态让收尾路径继续短路。
        """
        _cancel_context_var.set(None)

    def current_request_id(self):
        """返回当前请求上下文的 request_id（无上下文时返回 None）。

        工作线程通过它识别"自己是谁"，用于浏览器占用权归属判断。
        """
        state = _cancel_context_var.get()
        return state.request_id if state is not None else None

    def remaining_seconds(self):
        """返回本请求剩余时间预算（秒）；无上下文或未设 deadline 时返回 None。"""
        state = _cancel_context_var.get()
        if state is None or state.deadline is None:
            return None
        return state.deadline - time.time()

    def deadline_exceeded(self) -> bool:
        """本请求是否已超出时间预算。"""
        remaining = self.remaining_seconds()
        return remaining is not None and remaining <= 0

    # ── 活跃请求注册表（供 /cancel_task、/close_browser 等无上下文入口使用）──

    def register(self, request_id: str, state: "CancelState") -> None:
        with self._lock:
            self._active[request_id] = state

    def unregister(self, request_id: str) -> None:
        with self._lock:
            self._active.pop(request_id, None)

    def cancel_request(self, request_id: str) -> bool:
        """按 request_id 取消单个请求，返回是否命中。"""
        with self._lock:
            state = self._active.get(request_id)
        if state is None:
            return False
        state.val = True
        return True

    def cancel_all(self) -> int:
        """取消所有活跃请求，返回被取消的数量。"""
        with self._lock:
            states = list(self._active.values())
        for state in states:
            state.val = True
        return len(states)

    def active_count(self) -> int:
        with self._lock:
            return len(self._active)


sys.modules[__name__] = CancelFlag()
