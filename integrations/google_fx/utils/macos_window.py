# -*- coding: utf-8 -*-
"""
🍎 macOS 下的 AdsPower 浏览器窗口抑制
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
为什么需要这个模块：browser.build_adspower_launch_args 的"静默后台模式"靠
`--window-position=-10000,-10000` 把窗口丢到屏幕外，那是照 Windows 写的方案
（同一批参数里的 CalculateNativeWinOcclusion 就是 Windows 专属 flag）。macOS 的
窗口服务器不允许窗口完全离开可见区域，会把负坐标 clamp 回屏幕内，而且 Chromium
启动时会 activate 自己——两件事叠加，任务一开浏览器就抢走最前端焦点，正在打字
的人被打断。实测（server.log 287 次静默启动）参数确实传进去了，窗口照样弹到最前。

macOS 使用 AppKit 的 NSRunningApplication.hide 做 app 级隐藏；不可用时才尝试
System Events 的 `set visible of process ... to false` 兼容路径。
它跟"最小化"不是一回事——隐藏后窗口仍然存在、仍然参与渲染，CDP 连接和 Playwright
的 page 对象全程有效，所以人工介入时只要取消隐藏就能看见，不需要重启浏览器重连。

三种模式（ADSPOWER_MACOS_WINDOW_MODE）：
  hide  : 隐藏浏览器 app + 把焦点还给原前台应用（默认，效果最彻底）
  focus : 只尝试把焦点还给原前台应用，窗口留在屏幕上
  off   : 什么都不做，沿用旧行为

AppKit 路径不访问辅助功能 UI 元素。旧 System Events 兼容路径依赖"辅助功能"权限；
权限拒绝后只停用该隐藏兼容路径，后续仍尝试 AppKit。所有失败都不阻塞生成任务。
macOS 的应用激活是系统请求，不能保证焦点一定恢复。
"""

import json
import os
import shutil
import subprocess
from urllib.parse import urlparse

from .logger import log

# 辅助功能权限缺失只提示一次，避免每次启动浏览器都刷屏
_ACCESSIBILITY_WARNED = False
# 只停用已确认无权限的 System Events 隐藏路径，不影响 AppKit 的后续尝试。
_HIDE_DEGRADED = False
# 被本模块隐藏过的浏览器 PID。人工接管时要把窗口翻出来给人看，但那条链路上只拿得到
# Playwright 的 page 对象、拿不到 CDP 端口（Playwright 不暴露 ws_url），所以这里自己
# 记账。用集合而不是单个值：号池切换时同一批任务可能先后开过好几个 profile。
_HIDDEN_PIDS = set()

_OSASCRIPT_TIMEOUT = 5


def _osascript(script: str):
    """跑一段 AppleScript，返回 (ok, stdout, stderr)。osascript 缺失时静默失败。"""
    binary = shutil.which("osascript")
    if not binary:
        return False, "", "osascript not found"
    try:
        proc = subprocess.run(
            [binary, "-e", script],
            capture_output=True, text=True, timeout=_OSASCRIPT_TIMEOUT,
        )
        return proc.returncode == 0, proc.stdout.strip(), proc.stderr.strip()
    except Exception as e:
        return False, "", f"{type(e).__name__}: {e}"


def _jxa(script: str):
    """运行内置 JavaScript Objective-C 桥，返回 (ok, stdout, stderr)。"""
    binary = shutil.which("osascript")
    if not binary:
        return False, "", "osascript not found"
    try:
        proc = subprocess.run(
            [binary, "-l", "JavaScript", "-e", script],
            capture_output=True, text=True, timeout=_OSASCRIPT_TIMEOUT,
        )
        return proc.returncode == 0, proc.stdout.strip(), proc.stderr.strip()
    except Exception as e:
        return False, "", f"{type(e).__name__}: {e}"


def _jxa_boolean(script: str) -> tuple:
    """解析 API 的 BOOL；osascript 的退出码成功不能代替操作结果。"""
    try:
        ok, out, err = _jxa(script)
        if not ok:
            return False, out, err
        if json.loads(out) is True:
            return True, out, ""
        return False, out, "AppKit operation returned false"
    except (TypeError, ValueError) as e:
        return False, "", f"invalid AppKit result: {e}"


def _native_app_action(pid: int, action: str) -> tuple:
    """按 PID 调用公共 AppKit API。"""
    expressions = {"hide": "app.hide", "unhide": "app.unhide",
                   "activate": "app.activateWithOptions(0)"}
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False, "", "invalid AppKit PID"
    if pid <= 0 or action not in expressions:
        return False, "", "invalid AppKit action or PID"
    # JXA 的无参 Objective-C 方法用属性访问执行，不能写成 app.hide()。
    return _jxa_boolean(
        'ObjC.import("AppKit"); '
        f'var app = $.NSRunningApplication.runningApplicationWithProcessIdentifier({pid}); '
        f'JSON.stringify(app.isNil() ? false : Boolean({expressions[action]}));'
    )


def _is_permission_error(stderr: str) -> bool:
    """辅助功能权限缺失：osascript 报 -1719 / not allowed assistive access。"""
    text = (stderr or "").lower()
    return "-1719" in text or "assistive" in text or "not allowed" in text


def browser_pid_from_ws(ws_url: str):
    """从 CDP WebSocket URL 反查监听该调试端口的浏览器主进程 PID。

    AdsPower 可以同时开多个 profile（每个一个 SunBrowser 实例），所以不能按进程名
    一刀切地隐藏——那会把用户手动打开、正在看的另一个 profile 也一起藏掉。调试端口
    是每个实例唯一的，用它定位才精确。
    """
    try:
        port = urlparse(ws_url).port
    except Exception:
        return None
    if not port:
        return None
    lsof = shutil.which("lsof")
    if not lsof:
        return None
    try:
        proc = subprocess.run(
            [lsof, "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=_OSASCRIPT_TIMEOUT,
        )
    except Exception:
        return None
    for line in (proc.stdout or "").split():
        try:
            return int(line.strip())
        except ValueError:
            continue
    return None


def frontmost_app() -> str:
    """返回当前前台应用名，供启动浏览器后把焦点还回去。取不到时返回空串。

    优先读 NSWorkspace；仅当原生读取失败时使用 System Events 兼容路径。
    """
    ok, out, _ = _jxa(
        'ObjC.import("AppKit"); '
        'var app = $.NSWorkspace.sharedWorkspace.frontmostApplication; '
        'JSON.stringify(app.isNil() ? "" : ObjC.unwrap(app.localizedName));'
    )
    if ok:
        try:
            name = json.loads(out)
            if isinstance(name, str) and name:
                return name
        except (TypeError, ValueError):
            pass
    ok, out, _ = _osascript(
        'tell application "System Events" to get name of first process whose frontmost is true'
    )
    return out if ok else ""


def restore_focus(app_name: str = "") -> bool:
    """请求把焦点还给 app_name；名字为空时不切换应用。

    `activate` 不访问辅助功能 UI 元素，但 macOS 仍可能不接受激活请求。
    """
    if app_name:
        ok, _, _ = _jxa_boolean(
            'ObjC.import("AppKit"); '
            'var apps = $.NSWorkspace.sharedWorkspace.runningApplications; '
            'var activated = false; '
            'for (var i = 0; i < apps.count; i++) { '
            'var app = apps.objectAtIndex(i); '
            f'if (ObjC.unwrap(app.localizedName) === {json.dumps(app_name)}) {{ '
            'activated = Boolean(app.activateWithOptions(0)); break; } } '
            'JSON.stringify(activated);'
        )
        if ok:
            return True
        quoted_name = app_name.replace("\\", "\\\\").replace('"', '\\"')
        ok, _, _ = _osascript(f'tell application "{quoted_name}" to activate')
        if ok:
            return True
    # 没有目标应用名：让 Finder 之外的上一个应用回到前台不可靠，直接放弃而不是乱切，
    # 免得把用户切到一个他没在用的窗口。
    return False


def _set_visible(pid: int, visible: bool) -> tuple:
    native_result = _native_app_action(pid, "unhide" if visible else "hide")
    if native_result[0]:
        return native_result
    if not visible and _HIDE_DEGRADED:
        return native_result
    state = "true" if visible else "false"
    return _osascript(
        f'tell application "System Events" to set visible of '
        f'(first process whose unix id is {pid}) to {state}'
    )


def _activate_pid(pid: int) -> bool:
    """请求激活浏览器供人工接管；系统可能拒绝，不能保证窗口立即前置。"""
    if _native_app_action(pid, "activate")[0]:
        return True
    ok, _, _ = _osascript(
        f'tell application "System Events" to set frontmost of '
        f'(first process whose unix id is {pid}) to true'
    )
    return ok


def hide_browser(ws_url: str, previous_app: str = "") -> bool:
    """隐藏该 CDP 端口对应的浏览器窗口，并把焦点还给 previous_app。

    返回 True 表示窗口确实被隐藏了；返回 False 表示只做到了（或只尝试了）归还焦点。
    任何情况下都不抛异常——窗口没藏住顶多是碍眼，不该让生成任务失败。
    """
    global _ACCESSIBILITY_WARNED, _HIDE_DEGRADED

    hidden = False
    pid = browser_pid_from_ws(ws_url)
    if pid:
        ok, _, err = _set_visible(pid, False)
        if ok:
            hidden = True
            _HIDDEN_PIDS.add(pid)
        elif _is_permission_error(err):
            _HIDE_DEGRADED = True
            if not _ACCESSIBILITY_WARNED:
                _ACCESSIBILITY_WARNED = True
                log(
                    "⚠️ AppKit 未能隐藏浏览器，兼容隐藏方式缺少「辅助功能」权限。"
                    "可在 系统设置 → 隐私与安全性 → 辅助功能 中授权实际启动服务的程序"
                    "（如终端 / VSCode / Python）；后续仍会尝试 AppKit，"
                    "当前仅尝试归还焦点。",
                    "浏览器启动",
                )
        elif not _HIDE_DEGRADED:
            log(f"⚠️ 隐藏浏览器窗口失败（不影响任务）: {err}", "浏览器启动")

    restore_focus(previous_app)
    return hidden


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def reveal_hidden(activate: bool = True) -> int:
    """把本模块隐藏过、且进程还活着的浏览器窗口全部显示出来，返回显示成功的个数。

    用于"检测到需要人工登录/验证码"：这时候把窗口翻到最前是期望行为。隐藏记录保留，
    人工处理完可以调 rehide_revealed() 一键藏回去。
    """
    shown = 0
    for pid in sorted(_HIDDEN_PIDS):
        if not _process_alive(pid):
            continue
        ok, _, _ = _set_visible(pid, True)
        if ok:
            shown += 1
            if activate:
                _activate_pid(pid)
    return shown


def rehide_revealed(previous_app: str = "") -> int:
    """人工处理结束后把之前翻出来的窗口重新藏回去，返回重新隐藏成功的个数。"""
    hidden = 0
    for pid in sorted(_HIDDEN_PIDS):
        if not _process_alive(pid):
            _HIDDEN_PIDS.discard(pid)
            continue
        ok, _, _ = _set_visible(pid, False)
        if ok:
            hidden += 1
    restore_focus(previous_app)
    return hidden


def forget(pid: int = None):
    """清理隐藏记录（浏览器关闭时调用；不传 pid 则清空并顺带剔除已死进程）。"""
    if pid is not None:
        _HIDDEN_PIDS.discard(pid)
        return
    for dead in [p for p in _HIDDEN_PIDS if not _process_alive(p)]:
        _HIDDEN_PIDS.discard(dead)


def show_browser(ws_url: str) -> bool:
    """取消隐藏并把浏览器切到最前——只在需要人工处理登录/验证码时调用。

    这时候抢焦点是**期望行为**：人得看见那个窗口才能处理。
    """
    pid = browser_pid_from_ws(ws_url)
    if not pid:
        return False
    ok, _, err = _set_visible(pid, True)
    if not ok and not _is_permission_error(err):
        log(f"⚠️ 恢复浏览器窗口显示失败: {err}", "浏览器启动")
    # 隐藏状态解除后还要把它 activate 到最前，否则窗口可能仍压在其它应用下面。
    _activate_pid(pid)
    return ok
