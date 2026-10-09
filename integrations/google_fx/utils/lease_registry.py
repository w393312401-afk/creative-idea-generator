# -*- coding: utf-8 -*-
"""
🔖 浏览器租约登记簿（并发方案 P1，2026-10-02）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
包内要回答两个问题，但包不能反向依赖宿主的控制面：
  1. 现在有哪些 AdsPower 环境被"别的任务"租着？—— 选号、关浏览器时必须绕开它们（R3/R4/R7）；
  2. 当前任务还活着吗？—— 每条日志都是一次心跳，用来标出"疑似卡死"（R9）。

和 browser_gate / account_binding 同一个套路：宿主启动时 install 一个 provider，
没装就安静降级为"没有任何租约"，独立跑脚本时行为不变。

provider 需要实现：
    leased_user_ids(exclude_current=True) -> set[str]   他人持有租约的 user_id（不含当前任务自己）
    touch() -> None                                      刷新当前任务租约的心跳
可选（P2 并发）：
    claim_account(user_id) -> bool       当前租约改占某账号；与别的租约撞号/撞出口返回 False
    current_claim() / restore_claim(u)   选号失败时把租约账号恢复成选号前的样子
    max_concurrent() -> int              当前并发上限
    protected_from_cleanup(user_id) -> bool  账号维护系统管理或未知的环境不得自动清理
这些方法都必须是廉价的纯内存操作（claim_account 在 N>1 时可能查一次出口，但不持锁），
且绝不抛错（这里仍会兜底吞掉异常）。没有 provider / 方法缺失一律退回"没有并发"的旧行为。
"""

_PROVIDER = None


class AccountLeaseConflict(RuntimeError):
    """A requested browser cannot be exclusively claimed by this task."""


def install(provider):
    """宿主注入；传 None 卸载。"""
    global _PROVIDER
    _PROVIDER = provider


def is_installed():
    return _PROVIDER is not None


def leased_by_others():
    """被其它任务租着的 user_id 集合。未安装/出错一律返回空集合（= 旧行为）。"""
    if _PROVIDER is None:
        return set()
    try:
        return {str(uid) for uid in (_PROVIDER.leased_user_ids(exclude_current=True) or ()) if uid}
    except Exception:
        return set()


def protected_from_cleanup(user_id):
    """Protect managed/unknown profiles while preserving standalone behavior."""
    if _PROVIDER is None:
        return False
    try:
        query = getattr(_PROVIDER, 'protected_from_cleanup', None)
        if not callable(query):
            return getattr(_PROVIDER, 'account_admission_factory', None) is not None
        protected = query(user_id)
        return protected if type(protected) is bool else True
    except Exception:
        return True


def touch():
    """当前任务心跳。日志每一行都会调，必须极轻。"""
    if _PROVIDER is None:
        return
    try:
        _PROVIDER.touch()
    except Exception:
        pass


def claim(user_id):
    """选号的原子步骤：让当前任务的租约改占 user_id。

    返回 False 表示这个账号（或它的出口）已被别的租约占着，调用方应换下一个候选。
    没有 provider / 当前不在租约里 / 出错：一律返回 True（= 旧行为，不阻止选号）。
    """
    if _PROVIDER is None or not hasattr(_PROVIDER, 'claim_account'):
        return True
    try:
        return bool(_PROVIDER.claim_account(user_id))
    except Exception:
        return True


def require_claim(user_id):
    """Claim before binding/connecting; uncertainty must not open another task's profile.

    Standalone clients without a host retain their existing behavior. Hosted
    parallel generation must use a successful atomic claim, including manual
    accounts and empty-pool fallback accounts that bypass account selection.
    """
    if not user_id or _PROVIDER is None:
        return
    claim_account = getattr(_PROVIDER, 'claim_account', None)
    if not callable(claim_account):
        if concurrency() > 1:
            raise AccountLeaseConflict('无法确认浏览器独占租约，已停止连接')
        return
    try:
        claimed = bool(claim_account(user_id))
    except Exception as error:
        raise AccountLeaseConflict('浏览器租约核对失败，已停止连接') from error
    if not claimed:
        raise AccountLeaseConflict('浏览器环境或其出口正被其他任务占用，已停止连接')


def current_claim():
    if _PROVIDER is None or not hasattr(_PROVIDER, 'current_claim'):
        return None
    try:
        return _PROVIDER.current_claim()
    except Exception:
        return None


def restore_claim(user_id):
    if _PROVIDER is None or not hasattr(_PROVIDER, 'restore_claim'):
        return
    try:
        _PROVIDER.restore_claim(user_id)
    except Exception:
        pass


def concurrency():
    """当前并发上限。没有 provider 时是 1。"""
    if _PROVIDER is None or not hasattr(_PROVIDER, 'max_concurrent'):
        return 1
    try:
        return max(1, int(_PROVIDER.max_concurrent()))
    except Exception:
        return 1
