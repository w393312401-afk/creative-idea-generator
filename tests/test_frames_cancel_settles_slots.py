"""取消帧序列后，界面必须停下来：没轮到的槽位不能继续画「等待中」转圈。

复现的实机故障（2026-07-28 截图）：用户点「取消」，服务端 4 秒后就把任务停了
（server.log 有「帧序列任务已被用户取消」），可界面上 IMG 002-013 一直转圈，
看起来像"取消了任务还在跑"。根因是收尾顺序——streamFramesProgress 的
catch 分支先重渲一次，此时 ideaTasksById 里的任务登记还在，
renderFramesForIdea 的 isFramePending 据它判定这些槽位"还没轮到"，于是继续画
pending 卡；随后 finally 才 endIdeaTask，但没有人再重渲一次。

这里用真实页面 + 打桩的 /api/compose-* 走完整条 streamFramesProgress，
断言收尾后帧网格里不再有 pending 卡。

跑法（自起静态服务，无需后端）：
    pytest tests/test_frames_cancel_settles_slots.py
"""
import contextlib
import functools
import http.server
import os
import socketserver
import threading

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _Threaded(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 128


class _Handler(http.server.SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass


@contextlib.contextmanager
def static_server():
    handler = functools.partial(_Handler, directory=ROOT)
    httpd = _Threaded(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield "http://127.0.0.1:%d" % httpd.server_address[1]
    finally:
        httpd.shutdown()


# 5 个图片槽位，只有第 1 帧出了图——正是截图里"首帧完成、其余全在转圈"的形状
IDEA = {
    "id": "cancel1",
    "title": "废弃发射井",
    "prompt_block": "x",
    "prompt_slots": {
        "images": [{"index": i, "body": "img %d" % i} for i in range(1, 6)],
        "videos": [],
    },
    "frameRun": {
        "title": "废弃发射井",
        "project_dir": "outputs/cancel1",
        "frames": [{"sequence": 1, "url": "/outputs/cancel1/frames/img_001.webp"}],
    },
}

READY_GLOBALS = [
    "streamFramesProgress", "renderFramesForIdea", "getIdeaTaskRecord",
    "switchMainTab", "switchTab",
]


def _slot_kinds(page):
    return page.evaluate("""() => Array.from(
        document.querySelectorAll('#frames-grid .slot-card'))
        .map(c => [c.id, c.dataset.kind])""")


def test_cancelled_frames_task_leaves_no_pending_spinners():
    with static_server() as base, sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        console_errors = []
        page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: console_errors.append("PAGEERROR: %s" % e))

        # 事件流打桩：任务不在内存里（404）→ watchTaskUntilTerminal 转去问状态接口，
        # 状态接口回 cancelled → 返回 {status:'cancelled'}，streamFramesProgress
        # 据此抛 AbortError 走"用户取消"收尾分支。这正是点了取消之后的真实时序。
        page.route("**/api/compose-stream*", lambda route: route.fulfill(
            status=404, content_type="text/plain", body="not found"))
        page.route("**/api/compose-status*", lambda route: route.fulfill(
            status=200, content_type="application/json", body='{"status": "cancelled"}'))
        PNG = bytes.fromhex(
            "89504e470d0a1a0a0000000d494844520000000100000001080600000"
            "01f15c4890000000a49444154789c6300010000050001"
            "0d0a2db40000000049454e44ae426082")
        page.route("**/outputs/**", lambda route: route.fulfill(
            status=200, content_type="image/png", body=PNG))

        page.goto(base + "/index.html", wait_until="load")
        try:
            page.wait_for_function(
                "names => names.every(n => typeof window[n] === 'function')",
                arg=READY_GLOBALS, timeout=15000)
        except Exception:
            print("未就绪的全局:", page.evaluate(
                "names => names.filter(n => typeof window[n] !== 'function')", READY_GLOBALS))
            print("控制台:", console_errors[:20])
            raise

        page.evaluate(
            "idea => { currentIdea = idea; savedIdeas = [idea];"
            " switchMainTab('results'); switchTab('overview'); }", IDEA)

        # 任务在跑期间：未出图的槽位就该是 pending（这个语义不能被修坏）
        page.evaluate("() => { beginIdeaTask(currentIdea.id, 'frames', 'probe', null);"
                      " renderFramesForIdea(currentIdea); }")
        during = dict(_slot_kinds(page))
        page.evaluate("() => endIdeaTask(currentIdea.id, 'frames')")

        # 完整跑一遍收尾链路
        page.evaluate("async () => { await streamFramesProgress('frames_x', currentIdea); }")
        page.wait_for_timeout(300)

        after = dict(_slot_kinds(page))
        rec_after = page.evaluate("() => !!getIdeaTaskRecord(currentIdea.id, 'frames')")
        btn_disabled = page.evaluate(
            "() => document.getElementById('generate-frames-btn').disabled")

        assert during.get("frame-slot-2") == "pending", \
            "任务在跑时未出图的槽位应画等待中，实际: %r" % (during,)

        pending_after = [sid for sid, kind in after.items() if kind == "pending"]
        assert not pending_after, \
            "取消收尾后仍有槽位在转圈（用户会以为任务还在跑）: %r" % (pending_after,)
        assert after.get("frame-slot-1") == "ready", \
            "已出图的首帧不能被收尾重渲抹掉，实际: %r" % (after,)
        assert after.get("frame-slot-2") == "missing", \
            "未出图的槽位收尾后应落到「未生成」态（带生成/上传出口），实际: %r" % (after,)
        assert rec_after is False, "收尾后不该再留着帧序列任务登记"
        assert btn_disabled is False, "收尾后「生成帧序列」按钮应恢复可点"

        browser.close()
