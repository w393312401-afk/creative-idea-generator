#!/usr/bin/env python3
"""把一份「完整提示词.txt」导入本机项目工作台（http://127.0.0.1:8085/ 项目）。

与前端「📥 上传提示词集」同一路径：先过 /api/edit_prompts 的槽位契约校验，
再用 /api/library/item 新建一条创意（= 工作台上多出一行项目）。
从不改写已有项目；同名标题直接拒绝。

用法:
  python3 tools/import_prompt_set.py --file <完整提示词.txt> --title <项目标题> [--base http://127.0.0.1:8085] [--dry-run]
"""
import argparse
import json
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone


def call(base, path, body=None, access=None):
    headers = {"Content-Type": "application/json"}
    if access:
        headers["X-Access-Code"] = access
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(base + path, data=data, headers=headers, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {"error": str(e)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True)
    ap.add_argument("--title", required=True)
    ap.add_argument("--base", default="http://127.0.0.1:8085")
    ap.add_argument("--access", default=None, help="服务端启用访问码时填写")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    text = open(a.file, encoding="utf-8").read()
    title = a.title.strip()
    n_img = len(re.findall(r"^图片 (\d+):", text, re.M))
    n_vid = len(re.findall(r"^视频 (\d+)(?: \[BRIDGE\])?:", text, re.M))
    if not title or n_img == 0:
        sys.exit("标题为空或解析不到「图片 N:」槽位，未导入。")

    code, idx = call(a.base, "/api/library/index", access=a.access)
    entries = idx.get("items") or idx.get("entries") or idx.get("library") or []
    if any(isinstance(e, dict) and e.get("title") == title for e in entries):
        sys.exit(f"点子库里已有同名创意「{title}」，请换标题。")

    stamp = int(time.time() * 1000)
    key = f"run_import_{stamp}__{title}"
    code, data = call(a.base, "/api/edit_prompts",
                      {"title": key, "prompt_block": text, "prev_prompt_block": ""}, a.access)
    if code != 200:
        sys.exit(f"服务端槽位校验未通过，未导入：{data}")
    if a.dry_run:
        print(f"校验通过（图片 {n_img}，视频 {n_vid}），dry-run 未写入。")
        return

    idea = {
        "id": f"import_{stamp}", "title": title, "project_key": key, "theme": title,
        "creativity": "手动导入", "prompt_block": text,
        "timestamp": datetime.now().strftime("%Y/%m/%d %H:%M:%S"), "timings": {},
        "image_count": n_img, "video_count": n_vid, "collage_url": "", "covers": [], "frameRun": None,
        "english_title": "", "social_title_en": "", "social_title_cn": "",
        "audit_md": "由 video-beat-ladder 生成并自动导入；未经本机质量门与工序一致性二次校验，也未渲染。",
        "repair_md": "", "imported_at": datetime.now(timezone.utc).isoformat(),
        "imported_source_text": text, "imported_source_label": a.file,
    }
    if isinstance(data, dict) and data.get("prompt_slots"):
        idea["prompt_slots"] = data["prompt_slots"]
    code, res = call(a.base, "/api/library/item", {"item": idea}, a.access)
    if code != 200 or res.get("status") != "success":
        sys.exit(f"写入点子库失败：{res}")
    print(f"已导入「{title}」：图片 {n_img}，视频 {n_vid}，project_key={key}")


if __name__ == "__main__":
    main()
