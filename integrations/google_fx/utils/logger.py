# -*- coding: utf-8 -*-
"""
🌟 精致化日志输出
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
带颜色和图标的日志函数，同时写入控制台和文件。

文件写入走标准 logging + RotatingFileHandler（2026-07 重构 / T3）：
- 线程安全：logging 自带锁，取代旧的"每行 open/append/close 且无锁"写法，
  消除多线程并发写日志时的行交错/丢行。
- 自动轮转：文件超过 ADSPWR_LOG_MAX_BYTES 自动切分，保留 ADSPWR_LOG_BACKUP_COUNT
  个备份，避免 server.log 无限增长写满磁盘。
"""

import os
import time
import threading
import contextvars
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

# 轮转参数（可通过环境变量覆盖）
_LOG_MAX_BYTES = int(os.environ.get("ADSPWR_LOG_MAX_BYTES", str(10 * 1024 * 1024)))  # 默认 10MB
_LOG_BACKUP_COUNT = int(os.environ.get("ADSPWR_LOG_BACKUP_COUNT", "5"))

_file_logger = None
_file_logger_lock = threading.Lock()

# 任务标签是 per-context 的（2026-07-26）。原来是模块级全局字符串：FX 任务本身串行，
# 但非 FX 任务、积分探针、自检线程是并发的，一个线程 set_task_label() 会把前缀盖到
# 其它线程的日志上，"按任务过滤日志"因此不可靠。contextvar 天然按线程/上下文隔离。
_task_label_var = contextvars.ContextVar("adspwr_task_label", default="")


def set_task_label(label):
    """标记当前上下文正在执行的任务 id，后续 log() 输出会带上这个前缀。

    传空字符串清除。返回值可传给 reset_task_label() 精确还原（嵌套场景用）。
    """
    return _task_label_var.set((label or "").strip())


def reset_task_label(token):
    try:
        _task_label_var.reset(token)
    except (ValueError, LookupError):
        _task_label_var.set("")


def current_task_label():
    return _task_label_var.get()


def _get_file_logger():
    """惰性、幂等地构建写文件用的 logger（reload/并发下不会重复挂 handler）。"""
    global _file_logger
    if _file_logger is not None:
        return _file_logger
    with _file_logger_lock:
        if _file_logger is not None:
            return _file_logger
        logger = logging.getLogger("adspwr_file")
        logger.setLevel(logging.INFO)
        logger.propagate = False
        if not logger.handlers:  # 幂等：避免重复注册导致重复写入
            handler = RotatingFileHandler(
                get_log_file_path(),
                maxBytes=_LOG_MAX_BYTES,
                backupCount=_LOG_BACKUP_COUNT,
                encoding="utf-8",
            )
            handler.setFormatter(logging.Formatter("%(message)s"))  # 行内容已自行排版
            logger.addHandler(handler)
        _file_logger = logger
        return logger


def get_log_file_path() -> str:
    """解析日志文件的绝对路径。

    ⚠️ 写日志（log()）与读日志（/api/logs）必须共用此函数，否则前端日志面板
    会读到一个从未被写入的文件而永远为空。
    路径规则：优先 $ADSPWR_LOG_DIR/server.log，否则回退到项目根目录 logs/server.log。
    """
    log_dir = os.environ.get("ADSPWR_LOG_DIR", "").strip()
    if not log_dir:
        # 兜底：始终写到项目根目录的 logs/，避免日志散落到 core/
        project_root = Path(__file__).resolve().parents[3]
        log_dir = os.path.join(project_root, "logs")
    os.makedirs(log_dir, exist_ok=True)
    return os.path.join(log_dir, "server.log")


def log(msg, prefix="System"):
    """ 🌟 精致化日志输出 """
    task_label = _task_label_var.get()
    if task_label:
        msg = f"[{task_label}] {msg}"
    # 颜色代码
    COLORS = {
        "GoogleFX": "\033[94m", # 蓝色
        "Worker":   "\033[96m", # 青色
        "System":   "\033[92m", # 绿色
        "Error":    "\033[91m", # 红色
        "Warning":  "\033[93m", # 黄色
        "RESET":    "\033[0m"
    }

    # 图标代码
    ICONS = {
        "GoogleFX": "🎨",
        "Worker":   "⚙️",
        "System":   "🚀",
        "Error":    "❌",
        "Warning":  "⚠️"
    }

    color = COLORS.get(prefix, COLORS["System"])
    icon = ICONS.get(prefix, "ℹ️")
    current_time = time.strftime("%H:%M:%S")

    # 统一列宽排版: 模块名称定长 10 字符
    module_name = f"{prefix:<10}"

    # 控制台彩色输出
    console_line = f"{color}[{current_time}] │ {icon} │ {module_name} │ {msg}{COLORS['RESET']}"
    print(console_line, flush=True)

    # 文件输出 (去除颜色代码)：线程安全 + 自动轮转
    file_line = f"[{current_time}] │ {icon} │ {module_name} │ {msg}"
    try:
        _get_file_logger().info(file_line)
    except Exception:
        pass
