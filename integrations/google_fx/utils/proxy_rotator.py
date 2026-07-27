# -*- coding: utf-8 -*-
"""
🔄 Miya IP 动态住宅代理轮换管理器
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
每次 Google FX 自动化任务启动前，自动更换 AdsPower 浏览器 Profile 的代理为
新的 Miya IP 动态住宅节点，降低被 Google 安全检测拦截的概率。

支持三种换 IP 模式:
  - session: 用户名加随机 session 后缀，每次连接分配新 IP（最常用）
  - gateway: 固定网关地址，每次新建连接自动轮换 IP
  - api:     调用 API 提取链接获取新的 IP:Port
"""

import os
import time
import uuid
import random
import string
import json
from typing import Optional, Dict
import requests
from .logger import log

from ..config import (
    get_runtime_default_user_id,
    get_runtime_default_port,
    runtime_env_or_default,
    AI_DIR,
)


class ProxyRotator:
    """Miya IP 动态住宅代理轮换管理器"""

    def __init__(self):
        self._load_config()

    # ──────────────────────────────────────────────
    # 配置加载
    # ──────────────────────────────────────────────

    def _load_config(self):
        """从 .env / 环境变量读取 Miya 代理配置（每次初始化重新读取，支持热更新）"""
        self.host = runtime_env_or_default("MIYA_PROXY_HOST", "")
        self.port = runtime_env_or_default("MIYA_PROXY_PORT", "")
        self.user = runtime_env_or_default("MIYA_PROXY_USER", "")
        self.password = runtime_env_or_default("MIYA_PROXY_PASSWORD", "")
        self.proxy_type = runtime_env_or_default("MIYA_PROXY_TYPE", "http")
        self.rotate_mode = runtime_env_or_default("MIYA_PROXY_ROTATE_MODE", "session")
        self.country = runtime_env_or_default("MIYA_PROXY_COUNTRY", "us")
        self.api_url = runtime_env_or_default("MIYA_PROXY_API_URL", "")
        self.auto_rotate = runtime_env_or_default("MIYA_AUTO_ROTATE", "1") == "1"
        self.session_prefix = runtime_env_or_default("MIYA_SESSION_PREFIX", "session")
        try:
            self.rotate_threshold = int(runtime_env_or_default("MIYA_ROTATE_THRESHOLD", "5"))
        except ValueError:
            self.rotate_threshold = 5

    @property
    def is_configured(self) -> bool:
        """检查是否已配置 Miya 代理（或者在 list 模式下已填充 IP 列表）"""
        if self.rotate_mode == "list":
            pool_file = AI_DIR / "runtime" / "proxy_pool.txt"
            if pool_file.exists():
                try:
                    with open(pool_file, "r") as f:
                        lines = [line.strip() for line in f if line.strip() and not line.strip().startswith("#")]
                        if lines:
                            return True
                except Exception:
                    pass
            return False
        return bool(self.host and self.port)

    # ──────────────────────────────────────────────
    # 代理配置生成
    # ──────────────────────────────────────────────

    def _generate_session_id(self) -> str:
        """生成随机 session ID，用于 session 模式换 IP"""
        return ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))

    def get_proxy_config(self) -> dict:
        """
        生成 AdsPower userProxyConfig 格式的代理配置。
        根据 rotate_mode 生成不同的用户名/IP端口：
          - session: username-session-{random}
          - gateway: username（固定，每次连接自动换 IP）
          - api:     从 API 提取新 IP:Port（覆盖 host/port）
          - list:    从 runtime/proxy_pool.txt 轮换读取 IP:Port:User:Pass
        """
        if not self.is_configured:
            log("⚠️ Miya 代理未配置（MIYA_PROXY_HOST / MIYA_PROXY_PORT 为空），跳过", "代理轮换")
            return {}

        proxy_host = self.host
        proxy_port = self.port
        proxy_user = self.user
        proxy_password = self.password
        proxy_type = self.proxy_type

        if self.rotate_mode == "list":
            # 列表模式：从 runtime/proxy_pool.txt 读取 IP 列表
            pool_file = AI_DIR / "runtime" / "proxy_pool.txt"
            proxies = []
            if pool_file.exists():
                try:
                    with open(pool_file, "r") as f:
                        proxies = [line.strip() for line in f if line.strip() and not line.strip().startswith("#")]
                except Exception as e:
                    log(f"⚠️ 读取 proxy_pool.txt 失败: {e}", "代理轮换")

            if not proxies:
                log("⚠️ 列表模式但未找到有效代理，将使用默认配置", "代理轮换")
            else:
                state = self._read_counter_info()
                proxy_index = state.get("proxy_index", 0)
                selected = proxies[proxy_index % len(proxies)]
                log(f"🔄 列表模式换 IP (index={proxy_index}): {selected.split(':', 2)[0]}", "代理轮换")
                parts = selected.split(":")
                if len(parts) >= 2:
                    proxy_host = parts[0]
                    proxy_port = parts[1]
                    if len(parts) >= 4:
                        proxy_user = parts[2]
                        proxy_password = parts[3]

        elif self.rotate_mode == "session":
            # Session 模式：用户名加随机后缀
            session_id = self._generate_session_id()
            if proxy_user:
                # 常见格式: username-session-xxxx 或 username_session-xxxx
                proxy_user = f"{self.user}-{self.session_prefix}-{session_id}"
            log(f"🔄 Session 模式换 IP: session_id={session_id}", "代理轮换")

        elif self.rotate_mode == "api":
            # API 提取模式：调 API 获取新 IP
            api_result = self._fetch_ip_from_api()
            if api_result:
                proxy_host = api_result.get("host", proxy_host)
                proxy_port = str(api_result.get("port", proxy_port))
                log(f"🔄 API 模式换 IP: {proxy_host}:{proxy_port}", "代理轮换")
            else:
                log("⚠️ API 提取 IP 失败，使用默认配置", "代理轮换")

        elif self.rotate_mode == "gateway":
            # 网关模式：固定配置，每次新连接自动换 IP
            log("🔄 网关模式: 新连接将自动分配新 IP", "代理轮换")

        else:
            log(f"⚠️ 未知换 IP 模式: {self.rotate_mode}，按 session 模式处理", "代理轮换")
            session_id = self._generate_session_id()
            if proxy_user:
                proxy_user = f"{self.user}-{self.session_prefix}-{session_id}"

        # 构造 AdsPower userProxyConfig 格式
        config = {
            "proxy_soft": "other",
            "proxy_type": proxy_type,
            "proxy_host": proxy_host,
            "proxy_port": proxy_port,
        }
        if proxy_user:
            config["proxy_user"] = proxy_user
        if proxy_password:
            config["proxy_password"] = proxy_password

        return config

    def _fetch_ip_from_api(self) -> Optional[dict]:
        """通过 API 提取链接获取新的代理 IP:Port"""
        if not self.api_url:
            log("⚠️ MIYA_PROXY_API_URL 未配置", "代理轮换")
            return None
        try:
            resp = requests.get(self.api_url, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            # 兼容常见 API 返回格式
            if isinstance(data, dict):
                # 格式: {"ip": "1.2.3.4", "port": 8080}
                if "ip" in data:
                    return {"host": data["ip"], "port": data.get("port", self.port)}
                # 格式: {"host": "1.2.3.4", "port": 8080}
                if "host" in data:
                    return data
                # 格式: {"data": [{"ip": "...", "port": ...}]}
                if "data" in data and isinstance(data["data"], list) and data["data"]:
                    item = data["data"][0]
                    return {"host": item.get("ip", ""), "port": item.get("port", "")}
            elif isinstance(data, list) and data:
                item = data[0]
                if isinstance(item, dict):
                    return {"host": item.get("ip", ""), "port": item.get("port", "")}
                # 格式: ["1.2.3.4:8080"]
                if isinstance(item, str) and ":" in item:
                    parts = item.rsplit(":", 1)
                    return {"host": parts[0], "port": parts[1]}
            # 纯文本格式: "1.2.3.4:8080"
            text = resp.text.strip()
            if ":" in text and len(text) < 50:
                parts = text.rsplit(":", 1)
                return {"host": parts[0], "port": parts[1]}
            log(f"⚠️ API 返回格式无法解析: {text[:100]}", "代理轮换")
            return None
        except Exception as e:
            log(f"❌ API 提取 IP 失败: {type(e).__name__}: {e}", "代理轮换")
            return None

    # ──────────────────────────────────────────────
    # 代理轮换核心逻辑
    # ──────────────────────────────────────────────

    def _read_counter_info(self) -> dict:
        counter_file = AI_DIR / "runtime" / "generation_counter.json"
        if not counter_file.parent.exists():
            counter_file.parent.mkdir(parents=True, exist_ok=True)
        state = {"counter": 0, "proxy_index": 0}
        if counter_file.exists():
            try:
                with open(counter_file, "r") as f:
                    data = json.load(f)
                    state["counter"] = int(data.get("counter", 0))
                    state["proxy_index"] = int(data.get("proxy_index", 0))
            except Exception:
                pass
        return state

    def _write_counter_info(self, counter: int, proxy_index: int):
        counter_file = AI_DIR / "runtime" / "generation_counter.json"
        try:
            with open(counter_file, "w") as f:
                json.dump({"counter": counter, "proxy_index": proxy_index}, f)
        except Exception as e:
            log(f"⚠️ 写入计数器失败: {e}", "代理轮换")

    def increment_request_counter(self, amount=1):
        """递增发送的生成请求计数器"""
        state = self._read_counter_info()
        counter = state["counter"] + amount
        proxy_index = state["proxy_index"]
        self._write_counter_info(counter, proxy_index)
        log(f"📈 发送请求 +{amount}，当前累计发送数: {counter}/{self.rotate_threshold}", "代理轮换")

    def increment_success_counter(self, amount=1):
        """兼容旧调用的空操作"""
        pass

    def rotate_proxy(self, user_id=None, port=None, force=False) -> dict:
        """
        执行完整的代理轮换流程：
        1. 关闭当前 AdsPower 浏览器（如果正在运行）
        2. 通过 AdsPower API 更新 Profile 的代理配置
        3. 返回新代理配置信息

        注意：调用此方法后，需要重新启动浏览器（由 get_ads_ws_url 自动完成）
        """
        if not self.is_configured:
            log("⚠️ Miya 代理未配置，跳过代理轮换", "代理轮换")
            return {}

        if not self.auto_rotate:
            log("ℹ️ 自动换 IP 已关闭 (MIYA_AUTO_ROTATE=0)", "代理轮换")
            return {}

        state = self._read_counter_info()
        counter = state["counter"]
        proxy_index = state["proxy_index"]

        should_rotate = False
        if force:
            log("🚨 收到强制/紧急换 IP 请求，将立即轮换并重置计数器", "代理轮换")
            proxy_index += 1
            counter = 0
            self._write_counter_info(counter, proxy_index)
            should_rotate = True
        else:
            if counter >= self.rotate_threshold:
                proxy_index += 1
                log(f"🔄 累计发送请求数已达 {counter} 次，开始轮换 IP (新 index={proxy_index}) 并重置计数器", "代理轮换")
                counter = 0
                self._write_counter_info(counter, proxy_index)
                should_rotate = True
            else:
                log(f"ℹ️ 累计发送请求数: {counter}/{self.rotate_threshold}，未达{self.rotate_threshold}次，跳过代理轮换，直接连接浏览器", "代理轮换")
                return {}

        ads_user_id = user_id or get_runtime_default_user_id()
        ads_port = port or get_runtime_default_port()

        log(f"🔄 开始代理轮换 (profile={ads_user_id}, mode={self.rotate_mode})...", "代理轮换")

        # Step 1: 关闭当前浏览器（忽略"已关闭"的错误）
        self._stop_browser(ads_user_id, ads_port)
        time.sleep(3)  # 给云端和本地进程留出缓冲时间

        # Step 2: 生成新代理配置
        proxy_config = self.get_proxy_config()
        if not proxy_config:
            return {}

        # Step 3: 通过 AdsPower API 更新 Profile 代理
        update_ok = self._update_profile_proxy(ads_user_id, ads_port, proxy_config)
        if update_ok:
            log(
                f"✅ 代理已轮换: {proxy_config.get('proxy_host')}:{proxy_config.get('proxy_port')}"
                f" (user={proxy_config.get('proxy_user', 'N/A')[:30]})",
                "代理轮换",
            )
        else:
            log("⚠️ 代理配置更新失败，将使用上一次的代理继续", "代理轮换")

        # Step 4: 等待 AdsPower 内部状态同步
        time.sleep(3)  # 增加状态同步的等待时间到 3 秒

        return proxy_config

    def _stop_browser(self, user_id: str, ads_port: str):
        """通过 AdsPower API 关闭浏览器"""
        try:
            url = f"http://127.0.0.1:{ads_port}/api/v1/browser/stop?user_id={user_id}"
            resp = requests.get(url, timeout=10)
            data = resp.json()
            if data.get("code") == 0:
                log(f"🛑 浏览器已关闭 (user_id={user_id})", "代理轮换")
                time.sleep(2)  # 等待浏览器进程完全退出
            elif "not running" in str(data.get("msg", "")).lower() or data.get("code") == -1:
                log(f"ℹ️ 浏览器未在运行 (user_id={user_id})", "代理轮换")
            else:
                log(f"⚠️ 关闭浏览器返回: {data}", "代理轮换")
        except Exception as e:
            log(f"⚠️ 关闭浏览器失败: {type(e).__name__}: {e}", "代理轮换")

    def _update_profile_proxy(self, user_id: str, ads_port: str, proxy_config: dict) -> bool:
        """通过 AdsPower API 更新浏览器 Profile 的代理配置"""
        try:
            url = f"http://127.0.0.1:{ads_port}/api/v1/user/update"
            payload = {
                "user_id": user_id,
                "user_proxy_config": proxy_config,
            }
            resp = requests.post(url, json=payload, timeout=15)
            data = resp.json()
            if data.get("code") == 0:
                return True
            else:
                log(f"⚠️ 更新 Profile 代理失败: {data}", "代理轮换")
                return False
        except Exception as e:
            log(f"❌ 更新 Profile 代理异常: {type(e).__name__}: {e}", "代理轮换")
            return False

    # ──────────────────────────────────────────────
    # IP 验证（可选，用于调试/日志）
    # ──────────────────────────────────────────────

    def verify_ip_via_api(self, ads_port=None) -> Optional[str]:
        """
        通过 AdsPower 打开的浏览器直接访问 ipinfo.io 验证当前出口 IP。
        返回 IP 地址字符串，失败返回 None。
        注意：此方法需要浏览器已启动。
        """
        try:
            from .browser import get_ads_ws_url
            from playwright.sync_api import sync_playwright

            with sync_playwright() as p:
                ws_url = get_ads_ws_url(auto_rotate_proxy=False)
                browser = p.chromium.connect_over_cdp(ws_url, timeout=15000)
                context = browser.contexts[0]
                page = context.new_page()
                page.goto("https://ipinfo.io/json", timeout=15000)
                ip_data = page.evaluate("() => JSON.parse(document.body.innerText)")
                page.close()
                browser.close()
                ip_addr = ip_data.get("ip", "unknown")
                log(f"🌐 当前出口 IP: {ip_addr} ({ip_data.get('city', '')}, {ip_data.get('country', '')})", "代理轮换")
                return ip_addr
        except Exception as e:
            log(f"⚠️ IP 验证失败: {type(e).__name__}: {e}", "代理轮换")
            return None
