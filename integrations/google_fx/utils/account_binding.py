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
4. 进程默认值（ADSPOWER_DEFAULT_USER_ID / config.DEFAULT_USER_ID）。
"""

import contextlib
import contextvars

_TASK_ACCOUNT = contextvars.ContextVar("google_fx_task_account", default=None)
_PIN_RESOLVER = None


def set_task_account(user_id):
    """把当前执行上下文绑定到某个 AdsPower user_id，返回可用于还原的 token。"""
    value = str(user_id).strip() if user_id else ""
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
    for candidate in (explicit, current_task_account(), pinned_account(), fallback):
        if candidate:
            value = str(candidate).strip()
            if value:
                return value
    return ""
