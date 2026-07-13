# -*- coding: utf-8 -*-
"""从 outputs/*/manifest.json 重建创意库条目 → library.rebuilt.json（不覆盖 library.json）。

2026-07-12 整库清零事故的恢复工具：library.json 被状态错乱的客户端会话覆盖为空后，
帧/视频/manifest 资产仍完好地留在 outputs/ 里。本脚本把每个项目目录的 manifest 重组成
库条目（标题、由逐帧/逐段提示词重组的 prompt_block、frameRun 帧清单），产出
library.rebuilt.json 供人工确认后替换：

    python tools/rebuild_library.py          # 生成/刷新 library.rebuilt.json
    # 确认无误后手动：copy library.rebuilt.json library.json （或在浏览器里对比后再定）

注意：无法恢复的字段——创意维度(dimensions)、审计报告、封面归属（covers/ 里的图无法
可靠回挂到条目）。若你的常用浏览器 localStorage['spark_library'] 仍有完整备份，优先用
那份（页面加载时会自动提示恢复），本脚本产物仅作兜底。
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUTS = os.path.join(REPO, 'outputs')
SKIP_DIRS = {'covers', 'diag', 'image-station', 'import-check-only', 'test_single_frame'}


def _rebuild_prompt_block(frames, videos):
    lines = ['图片提示词']
    for fr in sorted(frames, key=lambda f: f.get('slot') or 0):
        meta = (fr.get('meta') or '').strip()
        tag = f" [{meta}]" if meta else ''
        lines.append(f"图片 {fr.get('slot')}{tag}:")
        lines.append((fr.get('prompt') or '').strip())
        lines.append('')
    if videos:
        lines.append('视频提示词')
        for vd in sorted(videos, key=lambda v: v.get('slot') or 0):
            meta = (vd.get('meta') or '').strip()
            tag = f" [{meta}]" if meta else ''
            lines.append(f"视频 {vd.get('slot')}{tag}:")
            lines.append((vd.get('prompt') or '').strip())
            lines.append('')
    return '\n'.join(lines).strip()


def main():
    ideas = []
    for name in sorted(os.listdir(OUTPUTS)):
        pdir = os.path.join(OUTPUTS, name)
        mpath = os.path.join(pdir, 'manifest.json')
        if name in SKIP_DIRS or not os.path.isdir(pdir) or not os.path.exists(mpath):
            continue
        try:
            with open(mpath, 'r', encoding='utf-8') as f:
                manifest = json.load(f)
        except Exception as e:
            print(f"[SKIP] {name}: manifest 读取失败 {e}")
            continue
        frames = [fr for fr in (manifest.get('frames') or []) if isinstance(fr, dict)]
        videos = [vd for vd in (manifest.get('videos') or []) if isinstance(vd, dict)]
        if not frames:
            print(f"[SKIP] {name}: 无帧记录")
            continue
        title = manifest.get('title') or name
        rel_manifest = '/' + os.path.relpath(mpath, REPO).replace('\\', '/')
        idea = {
            'id': f"rebuilt-{int(time.time()*1000)}-{len(ideas)}",
            'title': title,
            'theme': title,
            'prompt_block': _rebuild_prompt_block(frames, videos),
            'covers': [],
            'rebuilt_from_manifest': rel_manifest,
            'frameRun': {
                'title': title,
                'frames': frames,
                'manifest': rel_manifest,
                'project_dir': os.path.abspath(pdir),
            },
        }
        if videos:
            idea['videoRun'] = {'title': title, 'videos': videos}
        ideas.append(idea)
        print(f"[OK] {title}: {len(frames)} 帧 / {len(videos)} 段视频")

    out_path = os.path.join(REPO, 'library.rebuilt.json')
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(ideas, f, ensure_ascii=False, indent=2)
    print(f"\n共重建 {len(ideas)} 条 → {out_path}")
    print("确认无误后手动替换 library.json（或优先使用浏览器 localStorage 备份）。")


if __name__ == '__main__':
    main()
