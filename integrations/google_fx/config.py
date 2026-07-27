# -*- coding: utf-8 -*-
"""
⚙️ 全局配置区域
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
集中管理所有超时时间、路径和默认参数。
所有可变配置优先读取进程环境；可选读取项目根目录的 .env 文件。
"""

import os
import sys
import threading
import warnings
import urllib3
from pathlib import Path

from .model_catalog import (
    DEFAULT_GOOGLE_FX_IMAGE_MODEL as CATALOG_DEFAULT_GOOGLE_FX_IMAGE_MODEL,
    normalize_google_fx_image_model,
)

# 禁用 requests 的 SSL 警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

warnings.simplefilter("ignore")

# 🖥️ 跨平台编码兼容性处理
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except:
        pass

# ==============================================================================
# 📄 加载 .env (唯一配置源)
# ==============================================================================

import platform

# 🖥️ 平台检测
PLATFORM = platform.system()  # "Darwin" (Mac) / "Windows" / "Linux"
IS_WINDOWS = PLATFORM == "Windows"
IS_MAC = PLATFORM == "Darwin"

CORE_DIR = Path(__file__).resolve().parent
# 内置运行时位于 <project>/integrations/google_fx。账号池、日志和默认输出必须继续
# 落在主项目下，不能随着包层级变化跑到 integrations/ 目录里。
PROJECT_ROOT_DIR = CORE_DIR.parents[1]
AI_DIR = PROJECT_ROOT_DIR
DEFAULT_PROJECT_ROOT = PROJECT_ROOT_DIR
ENV_FILE = Path(
    os.environ.get("GOOGLE_FX_ENV_FILE", str(PROJECT_ROOT_DIR / "runtime" / "google_fx.env"))
).expanduser()

# 尝试加载 python-dotenv；包不存在时也能正常 fallback
try:
    from dotenv import load_dotenv
    load_dotenv(ENV_FILE, override=False)  # 不覆盖已有环境变量
except ImportError:
    # python-dotenv 未安装时手动解析 .env 文件
    if ENV_FILE.exists():
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip()
                    # 不覆盖已有环境变量
                    if k and k not in os.environ:
                        os.environ[k] = v

# ==============================================================================
# ⚙️ 全局配置常量 (全部从环境变量读取，带 fallback)
# ==============================================================================


def _env_or_default(env_name: str, fallback: str) -> str:
    value = os.environ.get(env_name, "").strip()
    return value if value else fallback


def _read_env_file_values() -> dict[str, str]:
    values: dict[str, str] = {}
    if not ENV_FILE.exists():
        return values

    with open(ENV_FILE, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            if not key:
                continue

            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                value = value[1:-1]
            values[key] = value

    return values


def runtime_env_or_default(env_name: str, fallback: str) -> str:
    if env_name in os.environ and os.environ[env_name].strip():
        return os.environ[env_name].strip()
    file_values = _read_env_file_values()
    if env_name in file_values and file_values[env_name].strip():
        return file_values[env_name].strip()
    return _env_or_default(env_name, fallback)


# 💡 服务端口
SERVER_HOST = _env_or_default("SERVER_HOST", "127.0.0.1")
SERVER_PORT = int(_env_or_default("SERVER_PORT", "8000"))

# 💡 AdsPower 配置
DEFAULT_PORT = _env_or_default("ADSPOWER_PORT", "50325")
DEFAULT_USER_ID = _env_or_default("ADSPOWER_DEFAULT_USER_ID", "")

# 💡 路径
# 独立 AdsPower 项目过去按平台写到桌面或 N8N-main/AI_video；内置后统一回到
# SPARK 的 outputs/。实际生成调用通常会传 output_path，这里只是安全兜底。
_default_output_dir = str(DEFAULT_PROJECT_ROOT / "outputs")

PROJECT_ROOT = _env_or_default("ADSPWR_PROJECT_ROOT", str(DEFAULT_PROJECT_ROOT))
OUTPUT_DIR = _env_or_default("ADSPWR_OUTPUT_DIR", _default_output_dir)

# 💡 生成任务的最大等待时间（秒）
MAX_WAIT_SECONDS = int(_env_or_default("GOOGLE_FX_MAX_WAIT_SECONDS", "120"))

# 💡 单个请求的总时间预算（秒）：超出后运行锁等待与长循环会主动放弃，避免请求假死
GOOGLE_FX_REQUEST_BUDGET_SECONDS = int(_env_or_default("GOOGLE_FX_REQUEST_BUDGET_SECONDS", "3600"))

# 💡 页面加载超时（毫秒）
PAGE_LOAD_TIMEOUT = 60000

# 💡 默认 AI 模型配置
DEFAULT_GOOGLE_FX_VIDEO_MODEL = _env_or_default("GOOGLE_FX_VIDEO_MODEL", "Veo 3.1 - Fast")
DEFAULT_GOOGLE_FX_IMAGE_MODEL = normalize_google_fx_image_model(
    _env_or_default("GOOGLE_FX_IMAGE_MODEL", CATALOG_DEFAULT_GOOGLE_FX_IMAGE_MODEL)
)




def get_runtime_default_port() -> str:
    return runtime_env_or_default("ADSPOWER_PORT", DEFAULT_PORT)


def get_runtime_default_user_id() -> str:
    return runtime_env_or_default("ADSPOWER_DEFAULT_USER_ID", DEFAULT_USER_ID)


def get_runtime_google_fx_video_model() -> str:
    return runtime_env_or_default("GOOGLE_FX_VIDEO_MODEL", DEFAULT_GOOGLE_FX_VIDEO_MODEL)


def get_runtime_google_fx_image_model() -> str:
    return normalize_google_fx_image_model(
        runtime_env_or_default("GOOGLE_FX_IMAGE_MODEL", DEFAULT_GOOGLE_FX_IMAGE_MODEL)
    )


def _runtime_int(env_name: str, fallback: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(runtime_env_or_default(env_name, str(fallback))))
    except (TypeError, ValueError):
        return fallback


def get_runtime_max_wait_seconds() -> int:
    """单张图/单条视频的最长等待（秒），每次调用现读。

    MAX_WAIT_SECONDS 是模块级常量（import 那一刻冻结），SPARK 在同一进程里改
    os.environ 后立刻发起 FX 调用，用常量会读到旧值——所以需要等待超时能热调的
    地方一律走这个函数。
    """
    return _runtime_int("GOOGLE_FX_MAX_WAIT_SECONDS", MAX_WAIT_SECONDS, minimum=10)


def get_runtime_request_budget_seconds() -> int:
    """单个请求的总时间预算（秒），每次调用现读。"""
    return _runtime_int("GOOGLE_FX_REQUEST_BUDGET_SECONDS",
                        GOOGLE_FX_REQUEST_BUDGET_SECONDS, minimum=60)


# ==============================================================================
# 🌐 Miya IP 动态住宅代理配置
# ==============================================================================

MIYA_PROXY_HOST = _env_or_default("MIYA_PROXY_HOST", "")
MIYA_PROXY_PORT = _env_or_default("MIYA_PROXY_PORT", "")
MIYA_PROXY_USER = _env_or_default("MIYA_PROXY_USER", "")
MIYA_PROXY_PASSWORD = _env_or_default("MIYA_PROXY_PASSWORD", "")
MIYA_PROXY_TYPE = _env_or_default("MIYA_PROXY_TYPE", "http")
MIYA_PROXY_ROTATE_MODE = _env_or_default("MIYA_PROXY_ROTATE_MODE", "session")
MIYA_PROXY_COUNTRY = _env_or_default("MIYA_PROXY_COUNTRY", "us")
MIYA_PROXY_API_URL = _env_or_default("MIYA_PROXY_API_URL", "")
MIYA_SESSION_PREFIX = _env_or_default("MIYA_SESSION_PREFIX", "session")
MIYA_AUTO_ROTATE = _env_or_default("MIYA_AUTO_ROTATE", "1") == "1"
