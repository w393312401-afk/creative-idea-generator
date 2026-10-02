"""Build isolated Mihomo listeners for airport nodes; never change Clash settings.

The pool stores local HTTP endpoints, while outbound credentials stay in the
private bridge config. Listener ports and pool IDs survive subscription reordering.
"""

import copy
import hashlib
import json
import re
from pathlib import Path

from .proxy_pool import _now_iso


def read_profile(path):
    import yaml

    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("proxies"), list):
        raise ValueError("配置中没有 proxies 节点列表")
    nodes = data["proxies"]
    if not nodes:
        raise ValueError("配置中的节点列表为空")
    names = set()
    for node in nodes:
        if not isinstance(node, dict) or not node.get("name") or not node.get("server"):
            raise ValueError("配置含缺少名称或服务器地址的节点")
        if node["name"] in names:
            raise ValueError("配置含重复节点名称，无法生成独立入口")
        names.add(node["name"])
    return nodes


def is_us_node(name):
    return bool(re.search(r"🇺🇸|美国|美國|\b(?:US|USA|United States)\b", str(name), re.I))


def build_bridge(nodes, source_profile, state, base_port=17901):
    """Return config + entries without mutating the input pool or writing files."""
    source_profile = str(Path(source_profile).expanduser().resolve())
    bridge_id = hashlib.sha256(source_profile.encode()).hexdigest()[:12]
    proxies = state.get("proxies", {})
    existing = {entry.get("node_name"): (pid, entry)
                for pid, entry in proxies.items() if entry.get("bridge_id") == bridge_id}
    # Reserve retired ports too: an AdsPower profile may still refer to one.
    reserved = {int(entry["port"]) for entry in proxies.values()
                if entry.get("host") in ("127.0.0.1", "localhost", "::1")
                and str(entry.get("port", "")).isdigit()}
    next_port = int(base_port)
    listeners, entries = [], {}
    for node in nodes:
        name = node["name"]
        old_id, old = existing.get(name, (None, {}))
        pid = old_id or "airport_" + hashlib.sha256(
            f"{bridge_id}\0{name}".encode()).hexdigest()[:16]
        if old:
            port = int(old["port"])
        else:
            while next_port in reserved:
                next_port += 1
            port = next_port
            reserved.add(port)
            next_port += 1
        if not 1 <= port <= 65535:
            raise ValueError("本地代理端口超出 1~65535 范围")
        fingerprint = hashlib.sha256(json.dumps(
            node, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        us = is_us_node(name)
        entry = {
            **old, "label": old.get("label") or f"机场 · {name}",
            "host": "127.0.0.1", "port": str(port), "proxy_type": "http",
            "user": "", "password": "", "source": "airport",
            "fallback_tier": 0 if us else 1,
            "bridge_id": bridge_id, "node_name": name,
            "source_profile": source_profile, "node_fingerprint": fingerprint,
            "note": old.get("note") or ("静态代理之后使用 · 美国优先" if us
                                           else "静态及美国机场之后备用"),
            "disabled": bool(old.get("disabled", False)),
            "bound_user_id": old.get("bound_user_id") or "",
            "use_count": int(old.get("use_count") or 0),
            "created_at": old.get("created_at") or _now_iso(),
        }
        if fingerprint != old.get("node_fingerprint"):
            entry.update(last_check_at=None, last_check_status=None,
                         last_check_error=None, exit_ip="", exit_location="", latency_ms=None)
        entries[pid] = entry
        listeners.append({"name": pid, "type": "http", "listen": "127.0.0.1",
                          "port": port, "proxy": name})
    config = {
        "allow-lan": False, "mode": "rule", "log-level": "warning",
        "ipv6": False, "dns": {"enable": False}, "tun": {"enable": False},
        "listeners": listeners, "proxies": copy.deepcopy(nodes),
        "rules": ["MATCH,REJECT"],
    }
    return bridge_id, config, entries


def merge_entries(state, bridge_id, entries):
    """Preserve static proxies and retire missing nodes without reusing their ports."""
    merged = copy.deepcopy(state)
    pool = merged.setdefault("proxies", {})
    for pid, old in pool.items():
        if old.get("bridge_id") == bridge_id and pid not in entries:
            old.update(disabled=True, last_check_status="failed",
                       last_check_error="节点已从机场配置移除，请重新导入后检查")
    for pid, entry in entries.items():
        # A probe, edit, or binding made while the core starts should survive import.
        latest = pool.get(pid, {})
        entry = {**entry, **{k: latest[k] for k in (
            "disabled", "bound_user_id", "applied_at", "use_count", "label", "note")
            if k in latest}}
        if latest.get("node_fingerprint") == entry.get("node_fingerprint"):
            entry = {**entry, **{k: latest[k] for k in (
                "last_check_at", "last_check_status", "last_check_error", "exit_ip",
                "exit_location", "latency_ms") if k in latest}}
        pool[pid] = dict(entry)
    return merged
