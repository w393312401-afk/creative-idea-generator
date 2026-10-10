# -*- coding: utf-8 -*-
"""把全局封面池 outputs/covers/ 里的历史封面搬进各自的项目目录。

封面图现在跟项目打包在一起：`outputs/<项目目录>/cover_<毫秒时间戳>.webp`
（见 server_common.project_cover_path）。搬家之前它们住在一个全局池里，靠文件名
前缀 `<安全标题>_cover_` 反查归属，于是：

  · 删项目删不掉封面（要拿着 URL 再单独删一遍，见 delete_idea_output_files）；
  · 标题一改就认不回来，画廊里还得单开一个「封面图片」组；
  · 项目的资产统计（build_projects_index 的 assets）少算了封面这一份。

本脚本只搬**认得出归属**的那些：归属来自点子库条目 / 任务结果里记的 covers 列表，
这是硬证据（不靠文件名猜）。认不出归属的（母项目早已删掉的孤儿封面）**原样保留**
在 outputs/covers/ 里，画廊照旧把它们显示在「封面图片」组，你可以在那里手动清理。

搬家的同时会把引用一起改写：点子库正文（covers / activeCoverUrl / frameRun 里
第一帧的 reference）、tasks/results/*.json、tasks/events/*.jsonl。

用法：
    python tools/migrate_covers.py --dry-run   # 只报告会发生什么，一个字节都不动
    python tools/migrate_covers.py             # 真搬
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# 与 tools/migrate_library.py 同因：import server_common 会把 sys.stdout 换成
# 一个只在连着终端时才回显的 _Tee，CLI 报告必须写真正的 stdout。
_REAL_STDOUT = sys.stdout

import server_common as sc  # noqa: E402


def emit(line=''):
    try:
        _REAL_STDOUT.write(f'{line}\n')
        _REAL_STDOUT.flush()
    except Exception:
        pass


def _norm_rel(url):
    """封面 URL → 相对仓库根的 posix 路径（去掉前导 '/'）；不是 outputs/ 下的返回 None。"""
    if not isinstance(url, str) or not url.strip():
        return None
    rel = url.strip().split('?', 1)[0].replace('\\', '/').lstrip('/')
    return rel if rel.startswith(sc.OUTPUT_ROOT + '/') else None


def _is_legacy_cover(rel):
    return bool(rel) and rel.startswith(f"{sc.OUTPUT_ROOT}/{sc.LEGACY_COVERS_DIRNAME}/")


def _record_covers(rec):
    """一条记录（点子库条目 / 任务结果）里出现过的全部封面路径。"""
    out = []
    if not isinstance(rec, dict):
        return out
    candidates = list(rec.get('covers') or [])
    candidates.append(rec.get('activeCoverUrl'))
    frame_run = rec.get('frameRun')
    if isinstance(frame_run, dict):
        for frame in (frame_run.get('frames') or []):
            if isinstance(frame, dict):
                candidates.append(frame.get('reference'))
    for url in candidates:
        rel = _norm_rel(url)
        if _is_legacy_cover(rel):
            out.append(rel)
    return list(dict.fromkeys(out))


def _remap_strings(obj, mapping):
    """递归把任何等于旧封面路径的字符串换成新路径，保留原来的前导 '/' 写法。"""
    if isinstance(obj, str):
        stripped = obj.split('?', 1)[0].replace('\\', '/')
        lead = '/' if stripped.startswith('/') else ''
        new = mapping.get(stripped.lstrip('/'))
        return lead + new if new else obj
    if isinstance(obj, list):
        return [_remap_strings(v, mapping) for v in obj]
    if isinstance(obj, dict):
        return {k: _remap_strings(v, mapping) for k, v in obj.items()}
    return obj


def _target_cover_name(src_name, existing):
    """旧文件名 → 项目目录里的 cover_<毫秒时间戳>.<ext>。

    时间戳优先沿用旧名字尾部的那串数字（`..._cover_1785382198077.webp`），它就是
    当初生成时的毫秒时间戳；没有就退回文件 mtime，保证画廊按时间排序不错位。
    """
    stem, ext = os.path.splitext(os.path.basename(src_name))
    ext = (ext or '.webp').lower()
    stamp = None
    for pattern in (r'_cover_(\d+)$', r'_(\d{6,})$'):
        match = re.search(pattern, stem)
        if match:
            stamp = match.group(1)
            break
    if not stamp:
        try:
            stamp = str(int(os.path.getmtime(src_name) * 1000))
        except OSError:
            stamp = '0'
    name = f"{sc.COVER_FILENAME_PREFIX}{stamp}{ext}"
    n = 1
    while name in existing:
        name = f"{sc.COVER_FILENAME_PREFIX}{stamp}_{n}{ext}"
        n += 1
    existing.add(name)
    return name


def _cover_name_title(name):
    """`<安全标题>_cover_<时间戳>.webp` → `<安全标题>`；更老的 `..._<数字>.webp` 也认。"""
    stem = os.path.splitext(name)[0]
    for pattern in (r'^(.+)_cover_\d+$', r'^(.+)_\d{6,}$'):
        match = re.match(pattern, stem)
        if match:
            return match.group(1)
    return None


def _dir_title_part(dirname):
    """项目目录名里的标题部分：run_<任务id>_<标题> → <标题>。"""
    match = re.match(r'^run_[A-Za-z0-9-]+_(.*)$', dirname)
    return match.group(1) if match else dirname


def _project_dirs():
    out_dir = os.path.join(ROOT, sc.OUTPUT_ROOT)
    if not os.path.isdir(out_dir):
        return []
    return [n for n in sorted(os.listdir(out_dir))
            if n not in sc.GALLERY_SPECIAL_DIRS
            and os.path.isdir(os.path.join(out_dir, n))]


def _owner_from_filename(name, dirs):
    """没有任何记录引用它时的回落：拿文件名里的安全标题去撞现存的项目目录。

    只有目录**确实存在**才算撞上——这是"能配上项目就搬进去"的判据，撞不上的
    （母项目连目录都删了）原样留在封面池里。目录名里的标题被 _safe_project_name
    截到 60 字符，所以允许它是文件名标题的前缀。
    """
    safe_title = _cover_name_title(name)
    if not safe_title:
        return None
    matched = []
    for dirname in dirs:
        part = _dir_title_part(dirname)
        if part == safe_title or (len(part) >= 16 and safe_title.startswith(part)):
            matched.append(dirname)
    if not matched:
        return None
    # 同一标题跑过多次时目录会有好几个。这类封面没有任何活引用，搬错兄弟目录也
    # 不会弄坏什么，取最近产出的那个即可（并在报告里标明这是按文件名认的）。
    matched.sort(key=lambda d: os.path.getmtime(os.path.join(ROOT, sc.OUTPUT_ROOT, d)),
                 reverse=True)
    return {'source': 'filename', 'project_key': '', 'title': '',
            'project_dir': os.path.join(sc.OUTPUT_ROOT, matched[0])}


def _load_json(path):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def collect_owners():
    """封面相对路径 → 归属信息。点子库先行（活记录优先），任务结果补齐。"""
    owners = {}

    library_items = [i for i in (sc.read_library() or []) if isinstance(i, dict)]
    for item in library_items:
        for rel in _record_covers(item):
            owners.setdefault(rel, {
                'source': 'library',
                'project_key': item.get('project_key') or '',
                'title': item.get('title') or '',
            })

    results_dir = os.path.join(ROOT, sc.TASKS_DIR, 'results')
    if os.path.isdir(results_dir):
        for name in sorted(os.listdir(results_dir)):
            if not name.endswith('.json'):
                continue
            result = _load_json(os.path.join(results_dir, name))
            for rel in _record_covers(result):
                owners.setdefault(rel, {
                    'source': 'task',
                    'project_key': (result or {}).get('project_key') or '',
                    'title': (result or {}).get('title') or '',
                })

    return owners, library_items


def plan_moves(owners):
    """返回 (moves, skipped)。moves 是 [(旧相对路径, 新相对路径, 归属)]。"""
    covers_dir = os.path.join(ROOT, sc.OUTPUT_ROOT, sc.LEGACY_COVERS_DIRNAME)
    moves = []
    skipped = []
    if not os.path.isdir(covers_dir):
        return moves, skipped

    dirs = _project_dirs()
    taken = {}          # 目标目录 → 已占用的文件名，避免同项目多张封面撞名
    for name in sorted(os.listdir(covers_dir)):
        src_abs = os.path.join(covers_dir, name)
        if not os.path.isfile(src_abs) or sc._gallery_media_type(name) != 'image':
            continue
        rel = f"{sc.OUTPUT_ROOT}/{sc.LEGACY_COVERS_DIRNAME}/{name}"
        owner = owners.get(rel) or _owner_from_filename(name, dirs)
        if not owner:
            skipped.append((rel, '没有记录引用它，文件名也撞不上任何现存项目目录'))
            continue

        pdir = owner.get('project_dir') or sc._get_project_dir(
            owner['project_key'] or owner['title'])
        pdir_abs = pdir if os.path.isabs(pdir) else os.path.join(ROOT, pdir)
        if not os.path.isdir(pdir_abs) and owner['source'] != 'library':
            # 只有任务结果记着它、项目目录也早没了 —— 为一张孤儿封面凭空造一个
            # 项目目录，只会在画廊里多出一个空壳组。留在封面池里更诚实。
            skipped.append((rel, '项目目录已不存在，且只有历史任务记录引用它'))
            continue

        existing = taken.setdefault(pdir_abs, set(
            os.listdir(pdir_abs) if os.path.isdir(pdir_abs) else []))
        new_name = _target_cover_name(src_abs, existing)
        new_rel = os.path.relpath(os.path.join(pdir_abs, new_name), ROOT).replace('\\', '/')
        moves.append((rel, new_rel, owner))

    return moves, skipped


def rewrite_references(mapping, library_items, dry_run):
    """把旧封面路径的全部引用改写成新路径。返回改动过的文件数。"""
    touched = 0

    for item in library_items:
        updated = _remap_strings(item, mapping)
        if updated != item:
            touched += 1
            if not dry_run:
                sc.write_library_item(updated)

    tasks_root = os.path.join(ROOT, sc.TASKS_DIR)
    results_dir = os.path.join(tasks_root, 'results')
    if os.path.isdir(results_dir):
        for name in sorted(os.listdir(results_dir)):
            if not name.endswith('.json'):
                continue
            path = os.path.join(results_dir, name)
            data = _load_json(path)
            if data is None:
                continue
            updated = _remap_strings(data, mapping)
            if updated != data:
                touched += 1
                if not dry_run:
                    sc.write_json_atomic(path, updated)

    events_dir = os.path.join(tasks_root, 'events')
    if os.path.isdir(events_dir):
        for name in sorted(os.listdir(events_dir)):
            if not name.endswith('.jsonl'):
                continue
            path = os.path.join(events_dir, name)
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    lines = f.read().splitlines()
            except OSError:
                continue
            out = []
            changed = False
            for line in lines:
                try:
                    parsed = json.loads(line)
                except Exception:
                    out.append(line)       # 截断/损坏的行原样留着，不是本脚本该管的
                    continue
                updated = _remap_strings(parsed, mapping)
                if updated != parsed:
                    changed = True
                out.append(json.dumps(updated, ensure_ascii=False))
            if changed:
                touched += 1
                if not dry_run:
                    with open(path, 'w', encoding='utf-8') as f:
                        f.write('\n'.join(out) + ('\n' if out else ''))

    return touched


def main():
    parser = argparse.ArgumentParser(description='把历史封面搬进各自的项目目录')
    parser.add_argument('--dry-run', action='store_true', help='只报告，不改任何文件')
    args = parser.parse_args()

    owners, library_items = collect_owners()
    moves, skipped = plan_moves(owners)

    emit('=' * 72)
    emit('封面迁移：outputs/covers/ → outputs/<项目目录>/cover_*')
    emit('=' * 72)

    if not moves and not skipped:
        emit('outputs/covers/ 是空的（或不存在），无事可做。')
        return 0

    emit(f'\n可搬迁 {len(moves)} 张：')
    for old, new, owner in moves:
        emit(f'  {os.path.basename(old)}')
        emit(f'    → {new}   [{owner["source"]}] {owner["title"] or owner["project_key"]}')

    if skipped:
        emit(f'\n保留在封面池里 {len(skipped)} 张：')
        for rel, why in skipped:
            emit(f'  {os.path.basename(rel)}  —— {why}')

    if not moves:
        emit('\n没有可搬迁的封面。')
        return 0

    mapping = {old: new for old, new, _ in moves}

    if args.dry_run:
        touched = rewrite_references(mapping, library_items, dry_run=True)
        emit(f'\n[dry-run] 将移动 {len(moves)} 个文件，改写 {touched} 份记录。未做任何改动。')
        return 0

    # 先搬文件再改引用：反过来的话中途失败会留下一批指向不存在文件的 URL。
    # 搬家用 move，源文件不会有第二份副本残留在封面池里。
    moved = 0
    for old, new, _ in moves:
        src = os.path.join(ROOT, old)
        dst = os.path.join(ROOT, new)
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.move(src, dst)
            moved += 1
        except Exception as e:
            emit(f'  [失败] {old} → {new}: {e}')

    touched = rewrite_references(mapping, library_items, dry_run=False)
    emit(f'\n完成：移动 {moved} 个文件，改写 {touched} 份记录。')

    covers_dir = os.path.join(ROOT, sc.OUTPUT_ROOT, sc.LEGACY_COVERS_DIRNAME)
    left = [n for n in os.listdir(covers_dir)] if os.path.isdir(covers_dir) else []
    if left:
        emit(f'outputs/covers/ 还剩 {len(left)} 个文件（无归属的历史封面，画廊里仍可见）。')
    else:
        emit('outputs/covers/ 已清空，画廊里的「封面图片」组随之消失。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
