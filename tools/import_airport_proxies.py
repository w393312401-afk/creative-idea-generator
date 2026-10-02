#!/usr/bin/env python3
"""Import a local Clash subscription into the proxy pool on macOS.

Usage: .venv/bin/python tools/import_airport_proxies.py /path/to/profile.yaml
Re-run after a subscription update; stable node names keep their local ports.
"""

import argparse
import json
import os
from pathlib import Path
import plistlib
import socket
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from integrations.google_fx.utils import proxy_pool
from integrations.google_fx.utils.airport_bridge import build_bridge, merge_entries, read_profile


def private_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        Path(temp).replace(path)
    finally:
        Path(temp).unlink(missing_ok=True)


def listening(port):
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=0.2):
            return True
    except OSError:
        return False


def import_profile(profile, core, base_port=17901):
    if sys.platform != "darwin":
        raise RuntimeError("自动启动本地桥接目前使用 macOS LaunchAgent")
    core = Path(core).expanduser().resolve()
    if not core.is_file():
        raise ValueError("找不到 Mihomo 内核，请用 --core 指定")
    nodes = read_profile(profile)
    with proxy_pool._LOCK:
        state = proxy_pool._read_state()
    bridge_id, config, entries = build_bridge(nodes, profile, state, base_port)
    # Background services cannot reliably read Desktop under macOS privacy rules.
    # Keep their private runtime under Application Support instead of the project.
    directory = Path.home() / "Library" / "Application Support" / "SPARK" / "airport_bridge" / bridge_id
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    label = f"local.spark.airport-{bridge_id}"
    target = f"gui/{os.getuid()}/{label}"
    plist = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    path = directory / "config.json"
    candidate = directory / "candidate.json"
    private_write(candidate, json.dumps(config, ensure_ascii=False, indent=2).encode())
    validation = subprocess.run([str(core), "-t", "-d", str(directory), "-f", str(candidate)],
                                capture_output=True, timeout=30)
    private_write(directory / "validation.log", validation.stdout + validation.stderr)
    if validation.returncode:
        raise RuntimeError(f"机场配置未通过内核校验，详情保存在 {directory / 'validation.log'}")
    for pid, entry in entries.items():
        if pid not in state["proxies"] and listening(entry["port"]):
            raise RuntimeError(f"本地端口 {entry['port']} 已占用，请用 --base-port 指定其他起始端口")
    old_config = path.read_bytes() if path.exists() else None
    old_plist = plist.read_bytes() if plist.exists() else None
    service = {
        "Label": label, "ProgramArguments": [str(core), "-d", str(directory), "-f", str(path)],
        "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 10,
        "WorkingDirectory": str(directory),
        "StandardOutPath": str(directory / "service.log"),
        "StandardErrorPath": str(directory / "service.log"),
    }
    # The pool is untouched until validation and listener startup both succeed.
    loaded = subprocess.run(["launchctl", "print", target], capture_output=True).returncode == 0
    try:
        if loaded:
            subprocess.run(["launchctl", "bootout", target], capture_output=True, check=True)
        if old_config:
            private_write(directory / "config.previous.json", old_config)
        private_write(path, candidate.read_bytes())
        private_write(plist, plistlib.dumps(service))
        private_write(directory / "service.log", b"")
        subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)],
                       capture_output=True, check=True)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if all(listening(e["port"]) for e in entries.values()):
                break
            time.sleep(0.2)
        else:
            raise RuntimeError("机场桥接未能启动全部本地入口，代理池未修改")
        with proxy_pool._LOCK:
            latest = proxy_pool._read_state()
            private_write(directory / "pool.before-import.json",
                          json.dumps(latest, ensure_ascii=False, indent=2).encode())
            proxy_pool._write_state(merge_entries(latest, bridge_id, entries))
            proxy_pool._STATE_FILE.chmod(0o600)
    except Exception:
        subprocess.run(["launchctl", "bootout", target], capture_output=True)
        if old_config is not None:
            private_write(path, old_config)
        else:
            path.unlink(missing_ok=True)
        if old_plist is not None:
            private_write(plist, old_plist)
            if loaded:
                subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)],
                               capture_output=True)
        else:
            plist.unlink(missing_ok=True)
        raise
    finally:
        candidate.unlink(missing_ok=True)
    return {"imported": len(entries), "us_nodes": sum(e["fallback_tier"] == 0 for e in entries.values()),
            "bridge_id": bridge_id, "ports": [min(int(e["port"]) for e in entries.values()),
                                                max(int(e["port"]) for e in entries.values())],
            "launch_agent": str(plist)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", type=Path)
    parser.add_argument("--core", default="/Applications/Clash Verge.app/Contents/MacOS/verge-mihomo")
    parser.add_argument("--base-port", type=int, default=17901)
    args = parser.parse_args()
    try:
        result = import_profile(args.profile, args.core, args.base_port)
    except Exception as exc:
        # YAML and subprocess errors can include credentials; print only safe errors.
        message = str(exc) if type(exc) in (RuntimeError, ValueError) else type(exc).__name__
        print(f"导入失败: {message}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
