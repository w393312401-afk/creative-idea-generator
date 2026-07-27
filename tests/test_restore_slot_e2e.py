"""端到端：起一个真实 server 实例，在真实页面上点卡片的「删除」，
再点 toast 上的「撤销」，确认磁盘与清单逐字节回到删除前。

服务端往返的细节契约在 tests/test_restore_slot.py；这里验的是整条用户路径
真的接通了（卡片按钮 → deleteSlotBeat → 撤销 toast → restoreSlotSnapshot →
两个网格重渲）。

**创意库隔离**：本用例通过 SPARK_DB_FILE / SPARK_LEDGER_FILE 把 server 指向
临时文件。页面的 saveLibrary() 会把它当时的 savedIdeas 整份 POST 回
/api/library，指向真库就是一次整库覆盖（2026-07-27 实际发生过一次）。
除此之外还在页面里把 saveLibrary 也短路掉，两道保险。
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

pytest.importorskip("playwright.sync_api")
from PIL import Image
from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TITLE = "e2e_restore_demo"


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _prompt_block():
    lines = ["图片提示词"]
    for i in range(1, 5):
        lines += ["图片 %d:" % i, "image prompt %d" % i, ""]
    lines.append("视频提示词")
    for i in range(1, 4):
        lines += ["视频 %d:" % i, "video prompt %d" % i, ""]
    return "\n".join(lines).strip()


def _build_run(run_dir):
    frames = os.path.join(run_dir, "frames")
    videos = os.path.join(run_dir, "videos")
    os.makedirs(frames)
    os.makedirs(videos)
    colors = {1: (220, 40, 40), 2: (40, 200, 80), 3: (60, 90, 240), 4: (240, 200, 40)}
    for s, c in colors.items():
        Image.new("RGB", (180, 320), color=c).save(
            os.path.join(frames, "img_%03d.webp" % s), format="WEBP")
    for s in (1, 2, 3):
        with open(os.path.join(videos, "vid_%03d.mp4" % s), "wb") as f:
            f.write(("VIDEO-%d" % s).encode())
    manifest = {
        "title": TITLE,
        "frames": [{"slot": s, "sequence": s,
                    "file": "outputs/%s/frames/img_%03d.webp" % (TITLE, s),
                    "url": "/outputs/%s/frames/img_%03d.webp" % (TITLE, s),
                    "prompt": "image prompt %d" % s,
                    "quality_gate": "auto_approved"} for s in colors],
        "videos": [{"slot": s, "sequence": s,
                    "file": "outputs/%s/videos/vid_%03d.mp4" % (TITLE, s),
                    "url": "/outputs/%s/videos/vid_%03d.mp4" % (TITLE, s),
                    "prompt": "video prompt %d" % s, "status": "success"}
                   for s in (1, 2, 3)],
    }
    with open(os.path.join(run_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False)


def _disk_state(run_dir):
    out = {}
    for sub in ("frames", "videos"):
        d = os.path.join(run_dir, sub)
        for name in sorted(os.listdir(d)):
            with open(os.path.join(d, name), "rb") as f:
                out["%s/%s" % (sub, name)] = f.read()
    with open(os.path.join(run_dir, "manifest.json"), "rb") as f:
        out["manifest.json"] = f.read()
    return out


@pytest.fixture
def live_server(tmp_path):
    run_dir = os.path.join(ROOT, "outputs", TITLE)
    shutil.rmtree(run_dir, ignore_errors=True)
    _build_run(run_dir)

    port = _free_port()
    env = dict(os.environ,
               PORT=str(port),
               SPARK_DB_FILE=str(tmp_path / "library.json"),
               SPARK_LEDGER_FILE=str(tmp_path / "topic_ledger.json"))
    proc = subprocess.Popen([sys.executable, "server.py"], cwd=ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = "http://127.0.0.1:%d" % port
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(base + "/index.html", timeout=2)
                break
            except Exception:
                time.sleep(0.5)
        else:
            raise RuntimeError("server 起不来")
        yield {"base": base, "run_dir": run_dir}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(run_dir, ignore_errors=True)


def test_delete_then_undo_from_the_ui(live_server):
    run_dir = live_server["run_dir"]
    before = _disk_state(run_dir)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 1000})
        page_errors = []
        page.on("pageerror", lambda e: page_errors.append(str(e)))
        page.goto(live_server["base"] + "/index.html", wait_until="load")
        page.wait_for_function(
            "() => typeof renderFramesForIdea === 'function'"
            " && typeof deleteSlotBeat === 'function'"
            " && typeof restoreSlotSnapshot === 'function'", timeout=20000)

        page.evaluate("""({title, pb}) => {
            currentIdea = { id: 'e2e', title, prompt_block: pb, frameRun: null };
            savedIdeas = [currentIdea];
            switchMainTab('results'); switchTab('overview');
            // 确认框在这条路径上只是二次确认，自动放行
            window.customConfirm = () => Promise.resolve(true);
            // 第二道保险：即便服务端没被隔离，也不让页面写创意库
            window.saveLibrary = () => Promise.resolve();
        }""", {"title": TITLE, "pb": _prompt_block()})
        page.evaluate(
            "() => fetch('/api/get_manifest?title=' + encodeURIComponent(currentIdea.title))"
            ".then(r => r.json()).then(m => { currentIdea.frameRun = m;"
            " renderFramesForIdea(currentIdea); renderVideosForIdea(currentIdea); })")
        page.wait_for_function(
            "() => document.querySelectorAll('#frames-grid .slot-card').length === 4",
            timeout=10000)

        # —— 删除第 2 拍 ——
        page.eval_on_selector('#frame-slot-2 [data-act="delete-slot"]', "el => el.click()")
        page.wait_for_function(
            "() => document.querySelectorAll('#frames-grid .slot-card').length === 3",
            timeout=20000)
        assert _disk_state(run_dir) != before, "删除应真的改变磁盘状态"
        assert page.evaluate("() => !!document.querySelector('.toast-action')"), \
            "删除后应立刻出现「撤销」出口"

        # —— 撤销 ——
        page.click(".toast-action")
        page.wait_for_function(
            "() => document.querySelectorAll('#frames-grid .slot-card').length === 4",
            timeout=20000)
        page.wait_for_timeout(800)

        after = _disk_state(run_dir)
        assert set(after) == set(before), \
            "恢复后文件集合应一致；缺=%s 多=%s" % (set(before) - set(after), set(after) - set(before))
        diff = [k for k in before if k != "manifest.json" and after[k] != before[k]]
        assert not diff, "恢复后媒体文件应逐字节一致，差异: %s" % diff
        assert json.loads(after["manifest.json"]) == json.loads(before["manifest.json"])

        labels = page.evaluate("""() => Array.from(
            document.querySelectorAll('#frames-grid .slot-card')).map(
                c => (c.querySelector('.slot-label')||{}).textContent)""")
        assert labels == ["IMG 001", "IMG 002", "IMG 003", "IMG 004"]
        assert "image prompt 2" in page.evaluate("() => currentIdea.prompt_block"), \
            "提示词块应把第 2 拍还原回去"

        listed = page.evaluate(
            "() => fetch('/api/deleted_slots?title=' + encodeURIComponent(currentIdea.title))"
            ".then(r => r.json())")
        assert not [s for s in listed["snapshots"] if not s["restored_at"]], \
            "恢复过的快照不应再列为可撤销"

        assert not page_errors, "页面不应有未捕获异常: %s" % page_errors[:3]
        browser.close()
