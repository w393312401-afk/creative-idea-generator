# -*- coding: utf-8 -*-
"""
📊 选择器命中层级统计
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
多级 fallback 选择器每次命中时记录"第几层命中"。主选择器开始失效时
（命中层级整体后移、miss 增多），可以在流程还没坏的时候提前发现 UI 变化，
而不是等所有兜底都挂了才整体失败。

数据落盘 runtime/selector_stats.json，由烟雾测试与人工排查读取。
统计写入绝不允许影响主流程：所有异常一律吞掉。
"""

import json
import os
import threading
import time

from ..config import AI_DIR

_LOCK = threading.Lock()
_STATS = None
_STATS_MTIME = None
_STATS_PATH = None


def stats_file_path():
    override = os.environ.get("ADSPWR_SELECTOR_STATS_FILE", "").strip()
    if override:
        return override
    return os.path.join(AI_DIR, "runtime", "selector_stats.json")


def _file_mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def _load():
    """读统计缓存。文件被外部改过（mtime 变化）或路径变了就重新加载。

    历史：`if _STATS is not None: return _STATS` 一旦加载就永不失效。于是
    "在控制台清空统计"或换 ADSPWR_SELECTOR_STATS_FILE 之后，本进程还在用旧内存
    数据，控制台的选择器漂移警告修好了也永远挂着。
    """
    global _STATS, _STATS_MTIME, _STATS_PATH
    path = stats_file_path()
    mtime = _file_mtime(path)
    if _STATS is not None and path == _STATS_PATH and mtime == _STATS_MTIME:
        return _STATS
    try:
        with open(path, "r", encoding="utf-8") as f:
            _STATS = json.load(f)
        if not isinstance(_STATS, dict):
            _STATS = {}
    except Exception:
        _STATS = {}
    _STATS_PATH = path
    _STATS_MTIME = mtime
    return _STATS


def _persist(stats):
    global _STATS, _STATS_MTIME, _STATS_PATH
    path = stats_file_path()
    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    # 自己写的这一版就是权威版本，同步 mtime 免得下次读白重载一遍
    _STATS = stats
    _STATS_PATH = path
    _STATS_MTIME = _file_mtime(path)


def reset(family=None):
    """清空选择器统计。family 为空 = 全清，否则只清这一族。

    没有这个入口时，修好选择器之后控制台的漂移警告会一直挂着——统计里永远留着
    历史上那些 miss/兜底命中，没办法确认"现在已经好了"。返回被清掉的族数。
    """
    with _LOCK:
        stats = _load()
        if family:
            removed = 1 if str(family) in stats else 0
            stats.pop(str(family), None)
        else:
            removed = len(stats)
            stats = {}
        _persist(stats)
    return removed


def record_hit(family, index, selector="", total=0):
    """记录一次选择器族的命中情况。

    family:   选择器族名（如 'orientation_tab'），未提供时调用方一般用首个选择器
    index:    命中的层级序号，0 = 主选择器；-1 = 全部未命中
    selector: 实际命中的选择器字符串
    total:    该族的总层数
    """
    try:
        with _LOCK:
            stats = _load()
            entry = stats.setdefault(str(family)[:120], {"hits": {}, "miss": 0})
            if index < 0:
                entry["miss"] = entry.get("miss", 0) + 1
            else:
                key = str(index)
                entry.setdefault("hits", {})[key] = entry["hits"].get(key, 0) + 1
                if selector:
                    entry["last_selector"] = str(selector)[:200]
            if total:
                entry["total_layers"] = total
            entry["last_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            _persist(stats)
    except Exception:
        pass


def summarize():
    """返回统计摘要：主选择器命中率下降或 miss 偏高的族排在前面。"""
    try:
        with _LOCK:
            stats = json.loads(json.dumps(_load()))
    except Exception:
        return []
    rows = []
    for family, entry in stats.items():
        hits = entry.get("hits", {}) or {}
        total_hits = sum(hits.values())
        primary = hits.get("0", 0)
        miss = entry.get("miss", 0)
        rows.append({
            "family": family,
            "total_hits": total_hits,
            "primary_ratio": (primary / total_hits) if total_hits else 0.0,
            "miss": miss,
            "last_at": entry.get("last_at", ""),
            "last_selector": entry.get("last_selector", ""),
        })
    rows.sort(key=lambda r: (r["primary_ratio"], -r["miss"]))
    return rows
