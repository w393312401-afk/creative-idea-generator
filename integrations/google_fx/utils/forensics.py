# -*- coding: utf-8 -*-
"""
🔍 失败现场取证（2026-07-26）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
问题：包内已经有一堆诊断 dump 函数（`_dump_visible_button_texts`、
`_dump_prompt_bar_for_diagnosis`、`_get_flow_menu_debug_info`），但它们**只写日志**。
真出问题时要在 3MB+ 的 server.log 里翻，而且截图这种最有信息量的证据根本没留。

现在：失败点调一次 `capture(page, tag, why)`，把截图 + DOM 片段 + 页面文字 +
可见按钮清单 + URL 一起落到 `runtime/fx_debug/<bucket>/<序号>_<tag>/`，
SPARK 控制台按任务列出可下载。

硬性约束：取证**绝不能**影响主流程。所有异常一律吞掉；截图有独立超时；
单次取证的产物有大小上限；目录数量有上限，超了删最老的。
"""

import json
import os
import time
from pathlib import Path

from ..config import AI_DIR
from .logger import log, current_task_label

# 取证根目录。跟 runtime/ 下其它可变状态放一起（已被 Git 忽略）。
DEBUG_ROOT = Path(os.environ.get("GOOGLE_FX_DEBUG_DIR", str(AI_DIR / "runtime" / "fx_debug")))

# 保留多少个取证 bucket（一般 = 任务数）。超了删最老的，避免无限占盘。
MAX_BUCKETS = int(os.environ.get("GOOGLE_FX_DEBUG_MAX_BUCKETS", "40"))
# 单个 bucket 里最多留多少次取证
MAX_CAPTURES_PER_BUCKET = int(os.environ.get("GOOGLE_FX_DEBUG_MAX_CAPTURES", "20"))
# 文字类证据的截断长度
_TEXT_LIMIT = 20000


def is_enabled():
    """取证默认开启；设 GOOGLE_FX_DEBUG_CAPTURE=0 可关掉（例如磁盘紧张时）。"""
    return os.environ.get("GOOGLE_FX_DEBUG_CAPTURE", "1").strip().lower() not in ("0", "false", "no")


def _safe_name(value, fallback="unknown"):
    text = "".join(ch if (ch.isalnum() or ch in "-_.") else "_" for ch in str(value or ""))
    return text[:80] or fallback


def _bucket_dir(bucket):
    path = DEBUG_ROOT / _safe_name(bucket, "no_task")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _prune():
    """按 mtime 删掉超额的老 bucket。"""
    try:
        buckets = [p for p in DEBUG_ROOT.iterdir() if p.is_dir()]
    except FileNotFoundError:
        return
    if len(buckets) <= MAX_BUCKETS:
        return
    buckets.sort(key=lambda p: p.stat().st_mtime)
    import shutil
    for stale in buckets[: len(buckets) - MAX_BUCKETS]:
        try:
            shutil.rmtree(stale, ignore_errors=True)
        except Exception:
            pass


def _write(path, content, binary=False):
    try:
        if binary:
            with open(path, "wb") as f:
                f.write(content)
        else:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
        return True
    except Exception:
        return False


# 骨架里最多记多少个元素。8000 个节点足够覆盖 Flow 整个工作台，压出来也就几百 KB。
_SKELETON_MAX_NODES = int(os.environ.get("GOOGLE_FX_DEBUG_SKELETON_NODES", "8000"))

# ⚠️ 2026-09-05 教训：原来这里只存 `page.content()[:80000]`。Flow 把整套 CSS
# 内联在 <head><style> 里，80000 字符**全被 CSS 吃光**，抓下来的 page.html 里
# 一个 body 标签都没有。结果是 Flow 改版当天现场存了、却完全看不出新 DOM 长什么样，
# 只能连真浏览器去看。下面两件事解决它：
#   1) page.html 去掉 style/script/link/svg 再存 —— 同样的预算能装下真正的结构；
#   2) 另存一份 dom_skeleton.txt：每个元素一行「深度|标签|class|data-*/aria-*」，
#      不含文本和样式，几百 KB 就能完整还原"这一版 UI 的元素长什么样"，
#      离线就能写出新选择器。
_STRIP_HTML_JS = """() => {
    const doc = document.documentElement.cloneNode(true);
    doc.querySelectorAll('style, script, noscript, link, svg, template, iframe')
       .forEach((el) => el.remove());
    // 内联 style 属性同样是纯噪声（Angular 会往上面写几百字符的动画状态）
    doc.querySelectorAll('[style]').forEach((el) => el.removeAttribute('style'));
    return doc.outerHTML;
}"""

_SKELETON_JS = """(maxNodes) => {
    const rows = [];
    const KEEP = /^(id|class|role|type|href|src|alt|title|placeholder|contenteditable)$/;
    const walk = (el, depth) => {
        if (rows.length >= maxNodes) return;
        const attrs = [];
        for (const a of Array.from(el.attributes || [])) {
            const n = a.name;
            if (KEEP.test(n) || n.startsWith('data-') || n.startsWith('aria-')
                || n.startsWith('mat') || n.startsWith('_ng')) {
                let v = a.value || '';
                if (v.length > 120) v = v.slice(0, 120) + '…';
                attrs.push(n + '=' + JSON.stringify(v));
            }
        }
        rows.push('  '.repeat(Math.min(depth, 30)) + el.tagName.toLowerCase()
                  + (attrs.length ? ' ' + attrs.join(' ') : ''));
        for (const child of Array.from(el.children)) walk(child, depth + 1);
    };
    walk(document.body || document.documentElement, 0);
    if (rows.length >= maxNodes) rows.push('... (truncated at ' + maxNodes + ' nodes)');
    return rows.join('\\n');
}"""


def capture(page, tag, why="", bucket=None, extra=None, collectors=None):
    """把当前页面状态落盘。返回取证目录路径（失败或未开启返回 None）。

    page 可以是 None（连浏览器之前就失败的场景）：那就只落文字信息。
    bucket 默认取当前日志任务标签，也就是 SPARK 的 task_id。

    collectors: {文件名: callable(page) -> str}。调用方可以塞进额外的取证项，
    比如画布卡片结构 dump（见 google_fx_diagnostics.dump_canvas_tiles）。
    每个 collector 独立 try/except，任何一个挂了都不影响其余证据落盘——
    取证是失败路径上的收尾动作，本身绝不能变成新的故障源。
    """
    if not is_enabled():
        return None
    try:
        bucket = bucket or current_task_label() or "no_task"
        base = _bucket_dir(bucket)
        existing = sorted([p for p in base.iterdir() if p.is_dir()])
        if len(existing) >= MAX_CAPTURES_PER_BUCKET:
            import shutil
            shutil.rmtree(existing[0], ignore_errors=True)
        slot = base / f"{int(time.time())}_{_safe_name(tag, 'capture')}"
        slot.mkdir(parents=True, exist_ok=True)

        meta = {
            "tag": str(tag),
            "why": str(why)[:2000],
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "bucket": str(bucket),
            "extra": extra or {},
            "url": "",
            "page": "closed_or_missing",
        }

        if page is not None:
            try:
                meta["url"] = page.url
            except Exception:
                pass
            try:
                meta["page"] = "closed" if page.is_closed() else "open"
            except Exception:
                pass
            if meta["page"] == "open":
                try:
                    png = page.screenshot(timeout=5000, full_page=False)
                    _write(slot / "screenshot.png", png, binary=True)
                except Exception as e:
                    meta["screenshot_error"] = f"{type(e).__name__}: {e}"
                try:
                    # 先剥掉 style/script 再截断，否则预算全被内联 CSS 吃掉
                    # （见 _STRIP_HTML_JS 上方注释）。JS 没跑成（抛异常、或返回
                    # 了非字符串）就退回原始 content()——有总比没有强，绝不能因为
                    # 想存得更好反而把现场丢了。
                    html = None
                    try:
                        html = page.evaluate(_STRIP_HTML_JS)
                    except Exception as e:
                        meta["html_strip_error"] = f"{type(e).__name__}: {e}"
                    if not isinstance(html, str):
                        html = page.content()
                    _write(slot / "page.html", html[:_TEXT_LIMIT * 4])
                except Exception as e:
                    meta["html_error"] = f"{type(e).__name__}: {e}"
                try:
                    # 元素骨架：选择器失效时唯一真正能离线用的证据
                    skeleton = page.evaluate(_SKELETON_JS, _SKELETON_MAX_NODES)
                    if isinstance(skeleton, str) and skeleton.strip():
                        _write(slot / "dom_skeleton.txt", skeleton)
                    else:
                        meta["skeleton_error"] = "evaluate 未返回文本"
                except Exception as e:
                    meta["skeleton_error"] = f"{type(e).__name__}: {e}"
                try:
                    _write(slot / "page.txt", page.inner_text("body")[:_TEXT_LIMIT])
                except Exception as e:
                    meta["text_error"] = f"{type(e).__name__}: {e}"
                try:
                    from ..services.google_fx_helpers import _dump_visible_button_texts
                    buttons = _dump_visible_button_texts(page, limit=80)
                    _write(slot / "buttons.json",
                           json.dumps(buttons, ensure_ascii=False, indent=2, default=str))
                except Exception as e:
                    meta["buttons_error"] = f"{type(e).__name__}: {e}"
                try:
                    from ..services.google_fx_diagnostics import probe_selectors
                    _write(slot / "selectors.json",
                           json.dumps(probe_selectors(page), ensure_ascii=False, indent=2, default=str))
                except Exception as e:
                    meta["selectors_error"] = f"{type(e).__name__}: {e}"
                for filename, collector in (collectors or {}).items():
                    try:
                        payload = collector(page)
                        if not isinstance(payload, str):
                            payload = json.dumps(payload, ensure_ascii=False,
                                                 indent=2, default=str)
                        _write(slot / _safe_name(filename, "extra.txt"), payload)
                    except Exception as e:
                        meta[f"collector_error:{filename}"] = f"{type(e).__name__}: {e}"

        _write(slot / "meta.json", json.dumps(meta, ensure_ascii=False, indent=2, default=str))
        _prune()
        log(f"🔍 已保存失败现场: {slot}（{tag}）", "取证")
        return str(slot)
    except Exception as e:
        # 取证自己炸了绝不能影响主流程
        try:
            log(f"⚠️ 保存失败现场时出错（已忽略）: {type(e).__name__}: {e}", "取证")
        except Exception:
            pass
        return None


def list_captures(limit=100):
    """列出所有取证记录，最新的在前。供 SPARK 控制台展示/下载。"""
    rows = []
    try:
        buckets = [p for p in DEBUG_ROOT.iterdir() if p.is_dir()]
    except Exception:
        return rows
    for bucket in buckets:
        try:
            slots = [p for p in bucket.iterdir() if p.is_dir()]
        except Exception:
            continue
        for slot in slots:
            meta = {}
            try:
                with open(slot / "meta.json", "r", encoding="utf-8") as f:
                    meta = json.load(f)
            except Exception:
                pass
            try:
                files = sorted(p.name for p in slot.iterdir() if p.is_file())
            except Exception:
                files = []
            rows.append({
                "bucket": bucket.name,
                "id": f"{bucket.name}/{slot.name}",
                "tag": meta.get("tag", slot.name),
                "why": meta.get("why", ""),
                "at": meta.get("at", ""),
                "url": meta.get("url", ""),
                "page": meta.get("page", ""),
                "mtime": slot.stat().st_mtime if slot.exists() else 0,
                "files": files,
            })
    rows.sort(key=lambda row: row["mtime"], reverse=True)
    return rows[:limit]


def resolve_capture_file(capture_id, filename):
    """把 (capture_id, filename) 解析成绝对路径，越界返回 None。

    capture_id 来自 HTTP 请求，必须防目录穿越：解析后必须仍在 DEBUG_ROOT 之内。
    """
    try:
        parts = [p for p in str(capture_id).split("/") if p]
        if len(parts) != 2:
            return None
        target = (DEBUG_ROOT / _safe_name(parts[0]) / _safe_name(parts[1]) / _safe_name(filename)).resolve()
        root = DEBUG_ROOT.resolve()
        if root not in target.parents:
            return None
        return str(target) if target.is_file() else None
    except Exception:
        return None


def clear_captures():
    """清空所有失败现场取证文件与目录，同时物理删除本地文件。返回删除的顶级条目数。"""
    count = 0
    try:
        if not DEBUG_ROOT.exists():
            return 0
        import shutil
        for child in list(DEBUG_ROOT.iterdir()):
            try:
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                    count += 1
                elif child.is_file():
                    child.unlink(missing_ok=True)
                    count += 1
            except Exception:
                pass
        log(f"🧹 已清空失败现场本地数据（共删除 {count} 个任务/条目目录）", "取证")
    except Exception as e:
        log(f"⚠️ 清空失败现场时出错: {type(e).__name__}: {e}", "取证")
    return count

