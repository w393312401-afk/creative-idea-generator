"""槽位网格渲染冒烟测试：把一份合成的 manifest 灌进真实页面，检查两个网格
画出来的卡片、徽标、按钮、事件委托是否都对。

覆盖 js/slot_model.js（状态）→ js/slot_card.js（渲染 + 委托）→
media_renderer/api_client 各调用点这一整条链路，方案见
docs/spark_result_slots_plan.md。纯逻辑部分另见 tests/test_slot_model.js。

跑法（会自起一个静态服务，无需后端）：
    pytest tests/test_slot_grid_render.py
"""
import contextlib
import functools
import http.server
import json
import os
import socketserver
import threading

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _Threaded(socketserver.ThreadingTCPServer):
    # 单线程 SimpleHTTPServer 会在并发拉 ~20 个脚本时丢连接，表现为随机某个
    # defer 脚本没跑到（误报成回归）。除了多线程，还要把 listen backlog 提上来
    # ——默认只有 5，Chromium 一次开六条以上连接就会撞出 ERR_SOCKET_NOT_CONNECTED。
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 128


class _Handler(http.server.SimpleHTTPRequestHandler):
    # HTTP/1.1 + keep-alive，少建连接就少一分被重置的机会
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


IDEA = {
    "id": "smoke1",
    "title": "冰川洞穴",
    "prompt_block": "x",
    "prompt_slots": {
        "images": [{"index": i, "body": "img %d" % i} for i in range(1, 6)],
        "videos": [{"index": i, "body": "vid %d" % i} for i in range(1, 5)],
    },
    "frameRun": {
        "title": "冰川洞穴",
        "project_dir": "outputs/smoke",
        "frames": [
            {"sequence": 1, "url": "/outputs/smoke/frames/img_001.webp"},
            {"sequence": 2, "url": "/outputs/smoke/frames/img_002.webp",
             "quality_gate": "i2i_fallback_degraded"},
            {"sequence": 3, "url": "/outputs/smoke/frames/img_003.webp",
             "quality_gate": "sequence_review_flagged", "vlm_qa_reason": "空间跳变",
             "manual_issue": "门开反了", "stale_lineage": True},
            # 4 缺失
            {"sequence": 5, "url": "/outputs/smoke/frames/img_005.webp",
             "source": "manual_upload", "swapped_from_sequence": 2},
        ],
        "videos": [
            {"slot": 1, "url": "/outputs/smoke/videos/vid_001.mp4"},
            {"slot": 2, "status": "failed", "error": "FX 队列超时"},
            {"slot": 3, "status": "skipped_cut"},
            # 4 缺失
        ],
    },
}



def test_slot_grid_renders_states_badges_and_delegated_actions():
    errors = []

    def check(cond, msg):
        if not cond:
            errors.append(msg)

    with static_server() as base, sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        console_errors = []
        page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: console_errors.append("PAGEERROR: %s" % e))
        page.goto(base + "/index.html", wait_until="load")
        # 静态服务偶发丢连接会让某个 defer 脚本没跑到；等到渲染链路真正就绪再开测，
        # 免得把"脚本没加载"误报成回归。
        try:
            page.wait_for_function(
                "() => ['renderSlotCard','frameSlotState','resolvePromptSlots',"
                "'renderFramesForIdea','renderVideosForIdea','switchMainTab']"
                ".every(n => typeof window[n] === 'function')", timeout=15000)
        except Exception:
            missing = page.evaluate(
                "() => ['renderSlotCard','frameSlotState','resolvePromptSlots',"
                "'renderFramesForIdea','renderVideosForIdea','switchMainTab']"
                ".filter(n => typeof window[n] !== 'function')")
            print("未就绪的全局:", missing)
            print("控制台:", console_errors[:20])
            raise

        # 图片实际不存在 -> onerror 会把卡片降级成占位；冒烟只关心渲染管线，
        # 因此拦截图片/视频请求返回 1x1 像素，让 ready 态真正成立。
        PNG = bytes.fromhex(
            "89504e470d0a1a0a0000000d494844520000000100000001080600000"
            "01f15c4890000000a49444154789c6300010000050001"
            "0d0a2db40000000049454e44ae426082")
        page.route("**/outputs/**", lambda route: route.fulfill(
            status=200, content_type="image/png", body=PNG))

        # currentIdea / savedIdeas 是 state.js 里的 let（词法全局，不挂 window），
        # 所以这里必须用无前缀赋值才能命中真正那个绑定。
        page.evaluate(
            "idea => { currentIdea = idea; savedIdeas = [idea];"
            " switchMainTab('results'); switchTab('overview');"
            " renderFramesForIdea(idea); renderVideosForIdea(idea); }", IDEA)
        page.wait_for_timeout(600)

        frames = page.evaluate("""() => Array.from(
            document.querySelectorAll('#frames-grid .slot-card')).map(c => ({
                id: c.id, kind: c.dataset.kind, draggable: c.draggable,
                url: c.dataset.url,
                label: (c.querySelector('.slot-label')||{}).textContent,
                badges: Array.from(c.querySelectorAll('.slot-badge')).map(b => b.textContent),
                acts: Array.from(c.querySelectorAll('.slot-action-btn')).map(b => b.dataset.act),
                title: c.title,
            }))""")
        videos = page.evaluate("""() => Array.from(
            document.querySelectorAll('#videos-grid .slot-card')).map(c => ({
                id: c.id, kind: c.dataset.kind,
                label: (c.querySelector('.slot-label')||{}).textContent,
                badges: Array.from(c.querySelectorAll('.slot-badge')).map(b => b.textContent),
                acts: Array.from(c.querySelectorAll('.slot-action-btn')).map(b => b.dataset.act),
            }))""")

        print(json.dumps({"frames": frames, "videos": videos},
                         ensure_ascii=False, indent=1))

        check(len(frames) == 5, "帧网格应按 prompt_slots 画 5 格，实得 %d" % len(frames))
        check(len(videos) == 4, "视频网格应画 4 格，实得 %d" % len(videos))

        check(frames[0]["kind"] == "ready" and frames[0]["draggable"], "IMG 001 应为可拖的出图卡")
        check(frames[1]["badges"] == ["降级"], "IMG 002 应只有降级徽标")
        check(frames[2]["badges"] == ["人工标记", "Stale"],
              "IMG 003 应同时挂人工标记与 Stale（旧实现第 3 枚没地方放）")
        check("fix-frame" in frames[2]["acts"], "IMG 003 应给出修复入口")
        check(frames[3]["kind"] == "missing"
              and frames[3]["acts"] == ["retry-frame", "upload-frame", "delete-slot"],
              "IMG 004 缺失应给生成/上传/删除三个出口，实得 %s" % frames[3]["acts"])
        check("人工从 IMG 002 拖过来" in frames[4]["title"], "IMG 005 的换位来源应进 hover")

        check(videos[0]["kind"] == "ready", "VID 001 应为出片卡")
        check(videos[0]["label"] == "VID 001 (IMG 001 ➔ IMG 002)", "VID 001 标签错: %s" % videos[0]["label"])
        check(videos[1]["kind"] == "failed", "VID 002 应为失败卡")
        check(videos[2]["kind"] == "cut" and videos[2]["acts"] == [],
              "VID 003 硬切应画中性卡且无重试按钮，实得 kind=%s acts=%s" % (videos[2]["kind"], videos[2]["acts"]))
        check(videos[3]["kind"] == "missing", "VID 004 应为未生成卡")

        # 徽标不重叠。静态服务下 API 全 404，结果面板不会被应用自身激活（整条祖先链
        # display:none，量出来全是 0），所以先把祖先链强行放出来再量真实布局。
        boxes = page.evaluate("""() => {
            const c = document.getElementById('frame-slot-3');
            for (let el = c; el; el = el.parentElement) {
                if (getComputedStyle(el).display === 'none') el.style.display = 'block';
            }
            const bs = Array.from(c.querySelectorAll('.slot-badge'));
            return { pos: bs.map(b => getComputedStyle(b).position),
                     wrap: getComputedStyle(c.querySelector('.slot-badges')).display,
                     rects: bs.map(b => {
                         const r = b.getBoundingClientRect();
                         return { l: Math.round(r.left), t: Math.round(r.top),
                                  w: Math.round(r.width), h: Math.round(r.height) };
                     }) };
        }""")
        check(boxes["wrap"] == "flex", "徽标容器应为 flex，实得 %s" % boxes["wrap"])
        check(all(p == "static" for p in boxes["pos"]),
              "徽标在容器内应回到流式布局（旧实现每枚都 position:absolute 叠在 left:5px），实得 %s"
              % boxes["pos"])
        # 真正要保证的是"不叠在一起"：窄卡片上 flex-wrap 会让第二枚换到下一行，
        # 宽卡片上则并排——两种都对，重叠才是错的（旧实现每枚都定位在 left:5px）。
        a, b = boxes["rects"]
        overlap = (a["l"] < b["l"] + b["w"] and b["l"] < a["l"] + a["w"]
                   and a["t"] < b["t"] + b["h"] and b["t"] < a["t"] + a["h"])
        check(all(r["w"] > 0 and r["h"] > 0 for r in boxes["rects"]) and not overlap,
              "两枚徽标不得重叠，实得 %s" % boxes["rects"])

        # 操作按钮必须待在自己的卡片里。S 档一列只有 88px，出图卡上最多五枚按钮：
        # 旧规则不换行也不限左边界，「修复此帧问题」「描述问题」会被压成 24px 宽的
        # 竖排汉字并整枚落到隔壁格上——看着是按钮，实际点到的是别人（2026-07-28 实机）
        page.evaluate("() => applySlotSize('S')")
        btn_box = page.evaluate("""() => {
            const c = document.getElementById('frame-slot-3');
            c.querySelector('.slot-actions').style.opacity = 1;
            const r = c.getBoundingClientRect();
            return { card: { l: r.left, w: r.width },
                     btns: Array.from(c.querySelectorAll('.slot-action-btn')).map(b => {
                         const q = b.getBoundingClientRect();
                         return { act: b.dataset.act, l: q.left, w: q.width };
                     }) };
        }""")
        outside = [b["act"] for b in btn_box["btns"]
                   if b["l"] < btn_box["card"]["l"] - 1
                   or b["l"] + b["w"] > btn_box["card"]["l"] + btn_box["card"]["w"] + 1]
        check(len(btn_box["btns"]) == 5, "IMG 003 应有五枚操作按钮，实得 %d" % len(btn_box["btns"]))
        check(not outside, "操作按钮不得溢出卡片，越界的有 %s" % outside)

        # 事件委托：点按钮应触发对应处理函数，且不触发 lightbox
        page.evaluate("""() => {
            window.__calls = [];
            window.retrySingleFrame = s => window.__calls.push(['retry-frame', s]);
            window.deleteSlotBeat = s => window.__calls.push(['delete-slot', s]);
            window.retrySingleVideo = s => window.__calls.push(['retry-video', s]);
            window.openLightbox = () => window.__calls.push(['lightbox']);
        }""")
        page.eval_on_selector('#frame-slot-4 [data-act="retry-frame"]', "el => el.click()")
        page.eval_on_selector('#video-slot-2 [data-act="retry-video"]', "el => el.click()")
        page.eval_on_selector("#frame-slot-1 img", "el => el.click()")
        calls = page.evaluate("() => window.__calls")
        check(["retry-frame", 4] in calls, "委托应把 IMG 004 的生成点击派给 retrySingleFrame，实得 %s" % calls)
        check(["retry-video", 2] in calls, "委托应把 VID 002 的重试点击派给 retrySingleVideo，实得 %s" % calls)
        check(["lightbox"] in calls, "点出图卡应开 lightbox，实得 %s" % calls)

        # busy：只影响 disabled，不改变按钮存在与否，且 disabled 后点击不派发
        before = page.evaluate(
            "() => document.querySelectorAll('#frames-grid .slot-action-btn').length")
        page.evaluate("() => { window.__calls = []; setFrameGridButtonsBusy(true); }")
        after = page.evaluate(
            "() => document.querySelectorAll('#frames-grid .slot-action-btn').length")
        check(before == after, "busy 不应增删按钮 (%d -> %d)" % (before, after))
        disabled = page.evaluate(
            "() => Array.from(document.querySelectorAll('#frames-grid .slot-action-btn'))"
            ".every(b => b.disabled)")
        check(disabled, "busy 时帧网格按钮应全部 disabled")
        page.eval_on_selector('#frame-slot-4 [data-act="retry-frame"]', "el => el.click()")
        check(page.evaluate("() => window.__calls.length") == 0, "disabled 的按钮不该派发点击")

        # 解除 busy 后按钮必须立刻恢复可点（旧实现在这里退化成死按钮）
        page.evaluate("() => setFrameGridButtonsBusy(false)")
        page.eval_on_selector('#frame-slot-4 [data-act="retry-frame"]', "el => el.click()")
        check(page.evaluate("() => window.__calls.length") == 1,
              "解除 busy 后按钮必须恢复可点（旧实现此处是死按钮）")

        # 解除 busy 还要把"不忙时该说什么"还回去：旧实现一律 title=''，跑完一轮之后
        # 每枚按钮的悬浮说明就永久消失了
        tip = page.evaluate(
            "() => document.querySelector('#frame-slot-4 [data-act=\"upload-frame\"]').title")
        check("手动上传本地图片" in tip, "解除 busy 后应恢复空闲提示，实得 %r" % tip)

        # 合并视图下的忙态往返：卡片画在 #beats-grid 里，旧实现却按 #frames-grid
        # 硬选按钮——解禁整个是空操作，任务跑完网格永远停在禁用态
        # （2026-07-28 实机：一致性审查跑完后整格帧按钮全点不动）
        page.evaluate("() => setSlotMergedView(true)")
        page.wait_for_timeout(300)
        merged = page.evaluate("""() => {
            setFrameGridButtonsBusy(true);
            const st = t => Array.from(document.querySelectorAll(
                `#beats-grid .slot-card[data-type="${t}"] .slot-action-btn`));
            const busy = { img: st('image').every(b => b.disabled),
                           vid: st('video').some(b => b.disabled),
                           imgN: st('image').length, vidN: st('video').length };
            setFrameGridButtonsBusy(false);
            busy.freed = st('image').every(b => !b.disabled);
            busy.tip = (st('image').find(b => b.dataset.act === 'upload-frame') || {}).title;
            return busy;
        }""")
        check(merged["imgN"] > 0 and merged["vidN"] > 0,
              "合并视图应把两类卡片都画进 #beats-grid，实得 img=%s vid=%s"
              % (merged["imgN"], merged["vidN"]))
        check(merged["img"], "合并视图下 busy 必须禁用帧卡片按钮")
        check(not merged["vid"], "帧网格的忙态不该顺手禁掉视频卡片的按钮")
        check(merged["freed"], "合并视图下解除 busy 必须真的解禁（旧实现是空操作，死按钮）")
        check("手动上传本地图片" in (merged["tip"] or ""),
              "合并视图下也要还回空闲提示，实得 %r" % merged["tip"])

        # 实机那次事故的完整复现：任务在跑时整格重渲（slot_model 按 busy 画出一整格
        # 禁用按钮）→ 任务结束、登记清掉 → 收尾只做 DOM 级解禁。合并视图下这一步
        # 选不中任何按钮，网格就永远停在禁用态。
        stuck = page.evaluate("""() => {
            const btns = () => Array.from(document.querySelectorAll(
                '#beats-grid .slot-card[data-type="image"] .slot-action-btn'));
            ideaTasksById['smoke1'] = { frames: { taskId: 't1' }, videos: null, cover: null };
            renderFramesForIdea(currentIdea);
            const during = btns().every(b => b.disabled);
            ideaTasksById = {};
            refreshSlotGridBusy('image');
            return { during, after: btns().every(b => !b.disabled) };
        }""")
        check(stuck["during"], "任务在跑时整格重渲应画成禁用态")
        check(stuck["after"],
              "任务结束后收尾解禁必须在合并视图下也生效（实机：审查跑完整格点不动）")

        # 收尾用的 refreshSlotGridBusy：忙不忙按"当前创意此刻还有没有同类任务在跑"
        # 现算，而不是由调用方硬写 false——网格 DOM 是跨创意共用的
        page.evaluate("() => setSlotMergedView(false)")
        page.wait_for_timeout(300)
        refreshed = page.evaluate("""() => {
            const btns = () => Array.from(
                document.querySelectorAll('#frames-grid .slot-action-btn'));
            ideaTasksById['smoke1'] = { frames: { taskId: 't1' }, videos: null, cover: null };
            refreshSlotGridBusy('image');
            const during = btns().every(b => b.disabled);
            ideaTasksById = {};
            refreshSlotGridBusy('image');
            return { during, after: btns().every(b => !b.disabled) };
        }""")
        check(refreshed["during"], "还有帧任务在跑时 refreshSlotGridBusy 必须保持禁用")
        check(refreshed["after"], "任务登记清空后 refreshSlotGridBusy 必须解禁")

        # 实时回调路径：renderVideoSlotDone 画出的卡片必须与整格重渲一致
        page.evaluate("""() => renderVideoSlotDone(4,
            { slot: 4, url: '/outputs/smoke/videos/vid_004.mp4' })""")
        live = page.evaluate("""() => { const c = document.getElementById('video-slot-4'); return {
            kind: c.dataset.kind, draggable: c.draggable,
            label: (c.querySelector('.slot-label')||{}).textContent,
            acts: Array.from(c.querySelectorAll('.slot-action-btn')).map(b => b.dataset.act) }; }""")
        check(live["kind"] == "ready", "renderVideoSlotDone 应画出 ready 卡")
        check(live["label"] == "VID 004 (IMG 004 ➔ IMG 005)",
              "实时回调也要带槽位契约标签，实得 %s" % live["label"])
        check(live["acts"] == ["retry-video", "upload-video", "delete-slot"],
              "实时回调画出的卡片必须带全部操作出口（旧实现一个都没有），实得 %s" % live["acts"])
        check(live["draggable"], "实时回调画出的卡片必须可拖（旧实现不可拖）")

        browser.close()

    # 静态服务没有后端，/api/* 全 404/501——那是预期噪音，不该淹没真问题。
    # 只报脚本加载失败与未捕获异常。
    real = [e for e in console_errors
            if "PAGEERROR" in e or "脚本加载失败" in e or "ERR_" in e]
    check(not real, "页面有脚本加载/运行错误: %s" % real[:5])

    assert not errors, "\n".join(errors)
