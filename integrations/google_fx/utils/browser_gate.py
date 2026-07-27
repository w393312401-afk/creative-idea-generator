# -*- coding: utf-8 -*-
"""
🚦 浏览器互斥闸门（2026-07-26）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
包内需要浏览器的"旁路动作"（积分探针、选择器探针、自检）过去直接调
`get_ads_ws_url()` 启浏览器，完全绕开宿主的排队控制面。结果是生成任务跑着的时候
点一下"刷新积分"就会去抢同一个 AdsPower profile、`bring_to_front()`、按 Escape，
直接搅乱 Flow 页面状态。

本模块提供一个可被宿主替换的闸门：默认 no-op（独立跑脚本时行为不变），
SPARK 在启动时装上 FX_CONTROL.slot 的实现，于是探针也进同一条队列、
在控制台可见、可取消、可限流。嵌套调用由控制面自己的可重入判断处理。

包内规约：需要浏览器的旁路动作**必须**包在 `browser_slot(...)` 里。
"""

import contextlib

_GATE = None


def install(gate):
    """宿主注入闸门实现；传 None 恢复 no-op。

    gate(kind, cancel_check=None, priority=0, task_id=None) 必须返回一个上下文管理器。
    """
    global _GATE
    _GATE = gate


def is_installed():
    return _GATE is not None


@contextlib.contextmanager
def browser_slot(kind, cancel_check=None, priority=0, task_id=None):
    """进入浏览器临界区。未安装闸门时安静降级为直通。"""
    if _GATE is None:
        yield
        return
    with _GATE(kind, cancel_check=cancel_check, priority=priority, task_id=task_id):
        yield
