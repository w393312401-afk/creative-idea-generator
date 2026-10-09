# -*- coding: utf-8 -*-
"""
🔗 FX 账号绑定（2026-07-26）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
历史问题：失败换号靠改 `os.environ["ADSPOWER_DEFAULT_USER_ID"]` 实现（见
utils/account_pool.switch_to_next_account 的旧注释）。这是进程级副作用：
- 换完不还原，**后续所有任务**（包括本来显式指定了账号的）都跟着漂到新账号；
- 两条链路（帧/视频）同时换号会互相覆盖；
- 控制台"当前环境"显示的值会被悄悄改掉，用户看不出账号已经漂了。

现在换号写进 per-task 的 contextvar，只影响当前执行上下文。contextvar 只对
设置它的线程/上下文可见，而 FX 运行时正是在各 worker 线程里同步跑的，语义正好
对上（和 utils/cancel_flag 的 per-request CancelState 同一套思路）。

解析优先级（get_ads_ws_url 等调用方统一走 resolve_account）：
1. 调用方显式传入的 user_id；
2. 本上下文的换号结果（set_task_account）；
3. 宿主安装的定向绑定解析器（SPARK 控制台给排队任务钉的账号）；
4. 当前已独占的租约环境（防止并行任务改变进程默认值后串号）；
5. 进程默认值（ADSPOWER_DEFAULT_USER_ID / config.DEFAULT_USER_ID）。
"""

import contextlib
import contextvars

from . import lease_registry

_TASK_ACCOUNT = contextvars.ContextVar("google_fx_task_account", default=None)
_FIXED_TASK_ACCOUNT = contextvars.ContextVar("google_fx_fixed_task_account", default=None)
_PIN_RESOLVER = None
_ACCOUNT_OBSERVER = None


class FixedAccountStopError(RuntimeError):
    """A strict video task must stop instead of changing its account/session."""


def current_fixed_task_account():
    return _FIXED_TASK_ACCOUNT.get()


def install_account_observer(observer):
    """宿主注入"某上下文绑定了账号"的只读观察回调（控制台作战板用）；传 None 卸载。

    observer(user_id) 只能做记录，异常会被吞掉，绝不能影响账号解析本身。
    """
    global _ACCOUNT_OBSERVER
    _ACCOUNT_OBSERVER = observer


def _observe(user_id):
    if _ACCOUNT_OBSERVER is None or not user_id:
        return
    try:
        _ACCOUNT_OBSERVER(user_id)
    except Exception:
        pass


def set_task_account(user_id):
    """把当前执行上下文绑定到某个 AdsPower user_id，返回可用于还原的 token。"""
    value = str(user_id).strip() if user_id else ""
    fixed = current_fixed_task_account()
    if fixed and value != fixed:
        raise FixedAccountStopError("固定视频账号任务禁止切换账号，已停止提交")
    lease_registry.require_claim(value)
    _observe(value)
    return _TASK_ACCOUNT.set(value or None)


def reset_task_account(token):
    try:
        _TASK_ACCOUNT.reset(token)
    except (ValueError, LookupError):
        # token 来自别的上下文（跨线程传递）时忽略：绑定本身随上下文一起消失。
        pass


@contextlib.contextmanager
def bound_task_account(user_id):
    """在一个生成批次内绑定实际 AdsPower 账号，并在结束后可靠还原。

    视频/图片链会把一项大任务拆成多个账号腿；只修改进程环境变量既会串任务，
    也无法让批次结束时的计数知道本腿实际用了谁。这个作用域把账号归属限制在
    当前同步执行上下文中，异常、取消或正常返回都会还原上一层绑定。
    """
    token = set_task_account(user_id)
    try:
        yield current_task_account()
    finally:
        reset_task_account(token)


def current_task_account():
    return _TASK_ACCOUNT.get()


@contextlib.contextmanager
def bound_fixed_task_account(user_id):
    """Pin one video task without changing the process default or other tasks."""
    value = str(user_id).strip() if user_id else ""
    if not value:
        raise ValueError("固定视频账号不能为空")
    if current_fixed_task_account() not in (None, value):
        raise FixedAccountStopError("固定视频账号任务禁止嵌套切换账号")
    fixed_token = _FIXED_TASK_ACCOUNT.set(value)
    token = None
    try:
        token = set_task_account(value)
        yield value
    finally:
        if token is not None:
            reset_task_account(token)
        _FIXED_TASK_ACCOUNT.reset(fixed_token)


def install_pin_resolver(resolver):
    """宿主注入"读取队列定向绑定"的函数；传 None 卸载。

    resolver() -> user_id 或 None，必须是无副作用的纯读取。
    """
    global _PIN_RESOLVER
    _PIN_RESOLVER = resolver


def pinned_account():
    if _PIN_RESOLVER is None:
        return None
    try:
        value = _PIN_RESOLVER()
    except Exception:
        return None
    return str(value).strip() or None if value else None


def resolve_account(explicit=None, fallback=None):
    """按优先级解析本次要用的 AdsPower user_id。"""
    fixed = current_fixed_task_account()
    if fixed:
        if explicit and str(explicit).strip() != fixed:
            raise FixedAccountStopError("固定视频账号任务禁止连接其他账号")
        lease_registry.require_claim(fixed)
        _observe(fixed)
        return fixed
    # A leg that used the process default already claimed its real profile.
    # Keep that lease binding when a parallel task changes the global default.
    for candidate in (explicit, current_task_account(), pinned_account(), lease_registry.current_claim(), fallback):
        if candidate:
            value = str(candidate).strip()
            if value:
                # 显式传入的账号可能是嵌套探针探别的号，不代表本任务占用的账号，不上报。
                if not explicit:
                    lease_registry.require_claim(value)
                    _observe(value)
                return value
    return ""
