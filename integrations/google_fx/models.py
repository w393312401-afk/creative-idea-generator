# -*- coding: utf-8 -*-
"""
📦 数据模型定义
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
所有 API 请求的 Pydantic 数据模型。
默认值统一引用 config.py 中的常量。
"""

from typing import List, Optional
from pydantic import BaseModel, Field, root_validator
from .config import (
    get_runtime_google_fx_video_model,
    get_runtime_google_fx_image_model,

)


_BROWSER_ENV_LOCKED_MESSAGE = (
    "请求体不允许覆盖 AdsPower 浏览器环境或端口。"
    "请只修改 Adspower/AI/.env 中的 ADSPOWER_DEFAULT_USER_ID 和 ADSPOWER_PORT。"
)


class BrowserEnvLockedRequest(BaseModel):
    """浏览器环境只允许从 .env 读取，禁止请求体覆盖。"""

    @root_validator(pre=True)
    def reject_browser_env_overrides(cls, values):
        if isinstance(values, dict):
            blocked_keys = [key for key in ("user_id", "port") if key in values]
            if blocked_keys:
                blocked = ", ".join(blocked_keys)
                raise ValueError(f"{_BROWSER_ENV_LOCKED_MESSAGE} 检测到字段: {blocked}")
        return values


class VideoRequest(BrowserEnvLockedRequest):
    prompt: str = ""
    image: str = ""       # 首帧 (Start frame) 本地路径
    end_image: str = ""   # 尾帧 (End frame) 本地路径，可选
    # 首/尾帧在 Flow 画布上的媒体 UUID。帧序列本来就是在 project_url 那个画布上
    # 生成的（manifest.frames[].fx_uuid），带上 UUID 就能直接挂载画布已有资产，
    # 不必把同一张图再上传一轮。留空 = 未知，照旧上传本地文件。
    image_uuid: str = ""
    end_image_uuid: str = ""
    ratio: Optional[str] = None
    # 可选: "Veo 3.1 - Fast" | "Veo 3.1 - Quality"
    model: str = Field(default_factory=get_runtime_google_fx_video_model)
    duration: Optional[str] = None
    output_path: str = ""
    project_url: Optional[str] = None  # Bound Flow canvas for the local project.






class VideoBatchItem(BaseModel):
    """批量视频生成中的单条任务"""
    prompt: str = ""
    image: str = ""       # 首帧路径
    end_image: str = ""   # 尾帧路径，可选
    ratio: Optional[str] = None
    model: str = ""       # 为空时继承批量请求级别的 model
    duration: Optional[str] = None
    output_path: str = "" # 为空时继承批量请求级别的 output_path


class VideoBatchRequest(BrowserEnvLockedRequest):
    """批量视频生成请求 — 同一画布先批量提交、再统一监听"""
    items: List[VideoBatchItem] = []
    ratio: Optional[str] = None
    model: str = Field(default_factory=get_runtime_google_fx_video_model)
    duration: Optional[str] = None
    output_path: str = ""
    concurrent: bool = True       # 并行提交模式 (同一画布快速连续提交后统一监听)
    max_concurrent: int = 5       # 单次批量最大任务数 (画布同时生成上限)


class MergeRequest(BaseModel):
    video_paths: List[str] = []
    output_filename: str = "merged_video.mp4"
    output_dir: str = ""  # 自定义输出目录，为空时使用默认 OUTPUT_DIR
    speed: float = 1.0    # 视频播放速度乘数 (如 2.0 表示两倍速)


class ImageBatchRequest(BrowserEnvLockedRequest):
    prompts: List[str] = []
    images: List[str] = []  # 参考图路径列表 (图生图 / 多图参考)
    excluded_media_uuids: List[str] = []  # 当前项目已用过的 Flow 媒体，不得当作新结果
    excluded_image_paths: List[str] = []  # 已有槽位图，用于下载后的近重复硬校验
    ratio: Optional[str] = None
    # 可选: "Nano Banana Pro" | "Nano Banana 2" | "Nano Banana 2 Lite"
    model: str = Field(default_factory=get_runtime_google_fx_image_model)
    output_path: str = ""
    project_url: Optional[str] = None  # Reuse across five-item submission chunks.
    # 本任务还没有自己的画布：浏览器里停着的那块属于上一个任务，必须新建而不是沿用
    # （2026-08-05 串图事故）。同一任务的后续 chunk 不再置位，继续复用已建立的画布。
    require_fresh_canvas: bool = False
    # One orchestration owner, one retry budget.  Callers that resume a partial
    # prefix pass the remaining budget instead of each layer inventing its own
    # four-attempt loop.
    max_attempts: int = Field(default=3, ge=1, le=6)
    # Targeted frame iteration must stay with the account that owns project_url.
    # A quota/risk retry on another account cannot open that account-scoped canvas.
    allow_account_switch: bool = True
    is_candidate_mode: bool = False
    generation_count: Optional[str] = None


class GoogleFxRunRequest(BrowserEnvLockedRequest):
    """n8n-friendly Google FX task wrapper."""
    task_id: str = ""
    kind: str = "image"  # image | video
    prompt: str = ""
    prompts: List[str] = []
    image: str = ""
    end_image: str = ""
    images: List[str] = []
    ratio: Optional[str] = None
    model: str = ""
    duration: Optional[str] = None
    output_path: str = ""


# 2026-08-01 清理：这里原有 UploadNotionVideoRequest（page_id / video_path / status），
# 全 repo 零引用——Notion 上传端点早已不在本服务里。
