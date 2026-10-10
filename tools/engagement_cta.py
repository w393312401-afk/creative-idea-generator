"""9:16 关注、点赞、评论、收藏、分享引导动画（默认无文字）：导出透明素材、叠加到成片。

动画本体在 ``tools/engagement_cta.html``（浏览器直接打开即可预览、拖进度）。本脚本用无头
Chromium 逐帧调用页面里的 ``renderAt(t)`` 截透明 PNG，再交给 FFmpeg：

    # 导出纯透明素材 → outputs/engagement_cta/
    #   engagement_cta.mov    ProRes 4444 带透明通道（剪映 / CapCut / Premiere / FCP 直接叠）
    #   engagement_cta.webm   VP9 透明（网页播放）
    python tools/engagement_cta.py render
    python tools/engagement_cta.py render --formats mov,webm,green   # 额外要绿幕版

    # 叠加到任意成片：默认占成片最后 5 秒；按成片尺寸等比缩放、贴右侧
    python tools/engagement_cta.py apply outputs/某项目/final.mp4 --start 3

    # 抽几帧静态图检查画面
    python tools/engagement_cta.py still --at 1.0,2.0,4.6

文字、头像、计数、时长都可调，见 ``--help``。同一套参数渲染过的叠加层会缓存在
``outputs/engagement_cta/.cache/``，重复 apply 不会重新逐帧渲染。Codex 精剪完成后的
自动烧录（配置中心「成片引导」）也走这里的 ``cached_overlay`` / ``apply_command``，见 cta_burn.py。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.collage import resolve_binary  # noqa: E402

TEMPLATE = Path(__file__).with_suffix(".html")
DEFAULT_OUT_DIR = PROJECT_ROOT / "outputs" / "engagement_cta"
CACHE_DIR = DEFAULT_OUT_DIR / ".cache"
WIDTH, HEIGHT = 1080, 1920
FORMATS = ("mov", "webm", "green", "png")
# 与 engagement_cta.html 的 DEFAULTS 一致。页面时间线：首次点击 0.65s，末次点击后 0.7s 手势退场、
# 停留 hold、0.37s 淡出、0.1s 透明尾帧，五次点击均分剩余时间 → 点击间隔 = (时长 - 1.82 - hold) / 4。
DEFAULT_DURATION, DEFAULT_HOLD = 5.0, 0.3
MIN_STEP, MAX_DURATION = 0.5, 30.0
# 带透明通道的像素格式。VP8/VP9 的透明层另存在 alpha_mode 标签里，像素格式仍报 yuv420p。
_ALPHA_PIX_FMT = re.compile(r"^(yuva|rgba|bgra|argb|abgr|gbrap|ya\d)")


class CtaError(RuntimeError):
    pass


# ── 配置 ────────────────────────────────────────────────────────────────


def _split(value: str | None, expected: int, name: str) -> list[str] | None:
    if value is None:
        return None
    items = [part.strip() for part in value.replace("，", ",").split(",")]
    if len(items) != expected:
        raise CtaError(f"{name} 需要 {expected} 项（逗号分隔），收到 {len(items)} 项：{value}")
    return items


def _avatar_data_url(path: str | None) -> str:
    if not path:
        return ""
    file = Path(path).expanduser()
    if not file.is_file():
        raise CtaError(f"头像文件不存在：{file}")
    mime = mimetypes.guess_type(file.name)[0] or "image/png"
    if not mime.startswith("image/"):
        raise CtaError(f"头像需要是图片文件：{file}")
    return f"data:{mime};base64,{base64.b64encode(file.read_bytes()).decode()}"


def check_timing(duration: float, hold: float = DEFAULT_HOLD) -> None:
    if not 0 <= hold <= 3:
        raise CtaError("停留时长需要在 0–3 秒之间")
    if duration > MAX_DURATION:
        raise CtaError(f"引导动画最长 {MAX_DURATION:g} 秒")
    if (duration - 1.82 - hold) / 4 < MIN_STEP:
        raise CtaError(f"引导动画 {duration:g} 秒太短，至少需要 {1.82 + hold + 4 * MIN_STEP:.2f} 秒")


def build_config(args: argparse.Namespace) -> dict:
    """命令行参数 → 注入页面的 window.CTA_CONFIG（只放显式给出的字段，其余用页面默认值）。"""
    config: dict = {}
    labels = _split(args.labels, 5, "--labels")
    counts = _split(args.counts, 4, "--counts")
    if labels:
        config["labels"] = labels
    if counts:
        config["counts"] = counts
    if args.avatar:
        config["avatar"] = _avatar_data_url(args.avatar)
    if args.headline:
        config["headline"] = args.headline
    if args.scale is not None:
        if args.scale <= 0:
            raise CtaError("--scale 必须大于 0")
        config["scale"] = args.scale
    for key in ("duration", "hold"):
        if getattr(args, key) is not None:
            config[key] = float(getattr(args, key))
    check_timing(config.get("duration", DEFAULT_DURATION), config.get("hold", DEFAULT_HOLD))
    return config


def config_key(config: dict, fps: int) -> str:
    # 显式写出默认值与省略它渲染出的是同一段动画，键也应相同
    normalized = {"duration": DEFAULT_DURATION, "hold": DEFAULT_HOLD, **config}
    normalized = {key: float(value) if isinstance(value, int) and not isinstance(value, bool) else value
                  for key, value in normalized.items()}
    digest = hashlib.sha1()
    digest.update(TEMPLATE.read_bytes())
    digest.update(json.dumps(normalized, sort_keys=True, ensure_ascii=False).encode())
    digest.update(str(fps).encode())
    return digest.hexdigest()[:12]


# ── 逐帧渲染 ────────────────────────────────────────────────────────────


class _Page:
    """持有一个已注入配置、可按时间渲染的无头页面。"""

    def __init__(self, config: dict):
        self.config = config

    def __enter__(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - 依赖缺失时的提示
            raise CtaError("缺少 playwright：pip install -r requirements.txt") from exc
        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.launch()
        except Exception as exc:
            self._pw.stop()
            raise CtaError(f"无法启动 Chromium（可运行 python -m playwright install chromium）：{exc}") from exc
        try:
            self.page = self._browser.new_page(
                viewport={"width": WIDTH, "height": HEIGHT}, device_scale_factor=1
            )
            payload = json.dumps(self.config, ensure_ascii=False)
            self.page.add_init_script(f"window.CTA_EXPORT = true; window.CTA_CONFIG = {payload};")
            self.page.goto(TEMPLATE.as_uri())
            self.page.wait_for_function("window.CTA_READY === true || !!window.CTA_ERROR", timeout=15000)
            error = self.page.evaluate("window.CTA_ERROR")
            if error:
                raise CtaError(f"动画页面初始化失败：{error}")
            self.duration = float(self.page.evaluate("window.CTA_DURATION"))
        except BaseException:
            self.__exit__()
            raise
        return self

    def shot(self, t: float, path: Path) -> None:
        self.page.evaluate("t => window.renderAt(t)", t)
        self.page.screenshot(path=str(path), omit_background=True, type="png")

    def __exit__(self, *exc):
        self._browser.close()
        self._pw.stop()


def render_frames(config: dict, fps: int, frame_dir: Path) -> tuple[int, float]:
    frame_dir.mkdir(parents=True, exist_ok=True)
    with _Page(config) as page:
        total = round(page.duration * fps)
        for i in range(total):
            page.shot(i / fps, frame_dir / f"{i:04d}.png")
            if (i + 1) % fps == 0 or i + 1 == total:
                print(f"  渲染帧 {i + 1}/{total}", file=sys.stderr, flush=True)
    return total, page.duration


def _ffmpeg() -> str:
    binary = resolve_binary("ffmpeg")
    if not binary:
        raise CtaError("未找到 ffmpeg")
    return binary


def _run(cmd: list[str]) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        tail = "\n".join(result.stderr.strip().splitlines()[-8:])
        raise CtaError(f"ffmpeg 失败（{result.returncode}）：\n{tail}")


def encode(frame_dir: Path, fps: int, out_dir: Path, stem: str, formats: list[str]) -> dict[str, Path]:
    ffmpeg = _ffmpeg()
    out_dir.mkdir(parents=True, exist_ok=True)
    frames = ["-framerate", str(fps), "-i", str(frame_dir / "%04d.png")]
    written: dict[str, Path] = {}
    if "mov" in formats:
        path = out_dir / f"{stem}.mov"
        _run([ffmpeg, "-y", "-v", "error", "-nostdin", *frames,
              "-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le",
              "-alpha_bits", "16", "-vendor", "apl0", "-an", str(path)])
        written["mov"] = path
    if "webm" in formats:
        path = out_dir / f"{stem}.webm"
        _run([ffmpeg, "-y", "-v", "error", "-nostdin", *frames,
              "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-b:v", "0", "-crf", "28",
              "-row-mt", "1", "-auto-alt-ref", "0", "-an", str(path)])
        written["webm"] = path
    if "green" in formats:
        path = out_dir / f"{stem}_green.mp4"
        _run([ffmpeg, "-y", "-v", "error", "-nostdin",
              "-f", "lavfi", "-i", f"color=c=0x00FF00:s={WIDTH}x{HEIGHT}:r={fps}", *frames,
              "-filter_complex", "[0:v][1:v]overlay=shortest=1,format=yuv420p",
              "-c:v", "libx264", "-crf", "16", "-preset", "slow", "-movflags", "+faststart",
              "-an", str(path)])
        written["green"] = path
    if "png" in formats:
        path = out_dir / f"{stem}_png"
        if path.exists():
            shutil.rmtree(path)
        shutil.copytree(frame_dir, path)
        written["png"] = path
    return written


def render(config: dict, fps: int, out_dir: Path, stem: str, formats: list[str]) -> dict[str, Path]:
    with tempfile.TemporaryDirectory(prefix="engagement_cta_") as tmp:
        total, duration = render_frames(config, fps, Path(tmp))
        print(f"  编码 {', '.join(formats)}（{total} 帧，{duration:.2f}s）", file=sys.stderr, flush=True)
        return encode(Path(tmp), fps, out_dir, stem, formats)


def cached_overlay(config: dict, fps: int = 30, cache_dir: Path | str = CACHE_DIR) -> Path:
    """返回这组参数对应的透明 MOV；没有就渲染一份。

    多个精剪任务可能同时要同一份缓存：各自写临时文件再原子改名，谁先写完都不会留下半截文件。"""
    cache_dir = Path(cache_dir)
    path = cache_dir / f"{config_key(config, fps)}.mov"
    if path.is_file() and path.stat().st_size > 0:
        return path
    print("  首次使用这组参数，渲染叠加层…", file=sys.stderr, flush=True)
    written = render(config, fps, cache_dir, f"{path.stem}.tmp-{os.getpid()}", ["mov"])["mov"]
    os.replace(written, path)
    return path


# ── 叠加到成片 ──────────────────────────────────────────────────────────


def probe(path: Path) -> dict:
    ffprobe = resolve_binary("ffprobe")
    if not ffprobe:
        raise CtaError("未找到 ffprobe")
    result = subprocess.run(
        [ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise CtaError(f"无法读取视频：{Path(path).name}")
    data = json.loads(result.stdout or "{}")
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    if not video:
        raise CtaError(f"没有视频轨：{Path(path).name}")
    duration = float(data.get("format", {}).get("duration") or video.get("duration") or 0)
    codec = video.get("codec_name") or ""
    tags = {str(key).lower(): str(value) for key, value in (video.get("tags") or {}).items()}
    decoder: list[str] = []
    if codec in ("vp8", "vp9") and tags.get("alpha_mode") == "1":
        # FFmpeg 自带的 VP8/VP9 解码器会丢掉透明层，必须显式用 libvpx 解码
        alpha, decoder = True, ["-c:v", "libvpx-vp9" if codec == "vp9" else "libvpx"]
    else:
        alpha = bool(_ALPHA_PIX_FMT.match(video.get("pix_fmt") or ""))
    return {
        "width": int(video["width"]),
        "height": int(video["height"]),
        "duration": duration,
        "codec": codec,
        "alpha": alpha,
        "decoder": decoder,
        "has_audio": any(s.get("codec_type") == "audio" for s in data.get("streams", [])),
    }


def resolve_start(start: str, video_duration: float, overlay_duration: float) -> float:
    """'end' = 片尾最后一段；数字 = 第几秒开始；负数 = 距片尾几秒开始。"""
    if start == "end":
        value = video_duration - overlay_duration
    else:
        try:
            value = float(start)
        except ValueError as exc:
            raise CtaError(f"--start 需要是秒数或 end，收到：{start}") from exc
        if value < 0:
            value = video_duration + value
    return round(max(0.0, min(value, max(0.0, video_duration - 0.1))), 3)


def overlay_geometry(video_w: int, video_h: int, overlay_w: int = WIDTH,
                     overlay_h: int = HEIGHT) -> tuple[int, int, int, int]:
    """叠加层等比缩放到能放进成片的最大尺寸，贴右侧、垂直居中。返回 (w, h, x, y)。"""
    k = min(video_w / overlay_w, video_h / overlay_h)
    w = max(2, round(overlay_w * k / 2) * 2)
    h = max(2, round(overlay_h * k / 2) * 2)
    return w, h, video_w - w, (video_h - h) // 2


def apply_command(video: Path, overlay: Path, start: str, output: Path) -> tuple[list[str], float, float]:
    """生成叠加命令（不执行），返回 (命令, 开始秒, 结束秒)。精剪任务用自己的可取消进程管理来跑它。"""
    if not Path(video).is_file():
        raise CtaError(f"视频不存在：{Path(video).name}")
    if Path(output).resolve() == Path(video).resolve():
        raise CtaError("输出路径不能覆盖原视频")
    info = probe(video)
    over = probe(overlay)
    if not over["alpha"]:
        raise CtaError("引导视频没有透明通道（需要 ProRes 4444 MOV 或 VP9 透明 WebM）")
    begin = resolve_start(start, info["duration"], over["duration"])
    end = round(min(info["duration"], begin + over["duration"]), 3)
    w, h, x, y = overlay_geometry(info["width"], info["height"], over["width"], over["height"])
    graph = (
        f"[1:v]setpts=PTS-STARTPTS+{begin}/TB,scale={w}:{h}:flags=lanczos,format=yuva444p[cta];"
        f"[0:v][cta]overlay=x={x}:y={y}:eof_action=pass:repeatlast=0,format=yuv420p[v]"
    )
    cmd = [_ffmpeg(), "-y", "-v", "error", "-nostdin", "-i", str(video), *over["decoder"], "-i", str(overlay),
           "-filter_complex", graph, "-map", "[v]"]
    if info["has_audio"]:
        cmd += ["-map", "0:a", "-c:a", "copy"]
    # -t 钉住原时长：叠加层比剩余片长更长时，overlay 默认会用最后一帧把成片拖长
    cmd += ["-c:v", "libx264", "-crf", "18", "-preset", "medium", "-movflags", "+faststart",
            "-t", f"{info['duration']:.3f}", str(output)]
    return cmd, begin, end


def apply(video: Path, overlay: Path, start: str, output: Path) -> Path:
    cmd, begin, end = apply_command(video, overlay, start, output)
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    _run(cmd)
    print(f"  引导动画位于 {begin:.2f}s – {end:.2f}s", file=sys.stderr)
    return output


# ── 命令行 ──────────────────────────────────────────────────────────────


def _add_style_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("动画参数（全部可选）")
    group.add_argument("--labels", help="点击时弹出的 5 个提示，逗号分隔（默认不显示提示）")
    group.add_argument("--counts", help="四个图标下的文字，逗号分隔（默认不显示；纯数字点击后 +1）")
    group.add_argument("--avatar", help="头像图片路径（默认：透明圆框）")
    group.add_argument("--headline", help="可选自定义大标题（默认不显示）")
    group.add_argument("--scale", type=float, help="互动栏整体缩放（默认 1）")
    group.add_argument("--duration", type=float, help=f"动画总时长秒数（默认 {DEFAULT_DURATION:g}，最短约 4.1）")
    group.add_argument("--hold", type=float, help=f"五项都点亮后停留的秒数（默认 {DEFAULT_HOLD:g}）")
    group.add_argument("--fps", type=int, default=30, help="帧率（默认 30）")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="9:16 无文字互动引导动画")
    sub = parser.add_subparsers(dest="command", required=True)

    p_render = sub.add_parser("render", help="导出透明叠加素材")
    _add_style_args(p_render)
    p_render.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR, help="输出目录")
    p_render.add_argument("--name", default="engagement_cta", help="输出文件名前缀")
    p_render.add_argument("--formats", default="mov,webm",
                          help=f"逗号分隔，可选 {', '.join(FORMATS)}（默认 mov,webm，均带透明通道）")

    p_apply = sub.add_parser("apply", help="把引导动画叠加到一条成片上")
    p_apply.add_argument("video", type=Path, help="成片 MP4")
    _add_style_args(p_apply)
    p_apply.add_argument("--start", default="end",
                         help="开始时间：end=片尾最后一段（默认）；3=第 3 秒；-8=距片尾 8 秒")
    p_apply.add_argument("--overlay", type=Path, help="改用已有的透明 MOV/WebM（忽略动画参数）")
    p_apply.add_argument("-o", "--output", type=Path,
                         help="输出路径（默认 outputs/engagement_cta/applied/<原名>_cta.mp4）")

    p_still = sub.add_parser("still", help="渲染指定时刻的透明 PNG 用于检查")
    _add_style_args(p_still)
    p_still.add_argument("--at", default="1.0,2.0,4.4", help="秒数，逗号分隔")
    p_still.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR / "stills")

    args = parser.parse_args(argv)
    try:
        if args.fps <= 0 or args.fps > 120:
            raise CtaError("--fps 需要在 1–120 之间")
        config = build_config(args)
        if args.command == "render":
            formats = [f.strip() for f in args.formats.split(",") if f.strip()]
            unknown = sorted(set(formats) - set(FORMATS))
            if unknown or not formats:
                raise CtaError(f"不支持的格式：{', '.join(unknown) or '(空)'}；可选 {', '.join(FORMATS)}")
            for kind, path in render(config, args.fps, args.out_dir, args.name, formats).items():
                print(f"{kind}\t{path}")
        elif args.command == "apply":
            overlay = args.overlay or cached_overlay(config, args.fps)
            if not overlay.is_file():
                raise CtaError(f"叠加层不存在：{overlay}")
            output = args.output or DEFAULT_OUT_DIR / "applied" / f"{args.video.stem}_cta.mp4"
            print(apply(args.video, overlay, args.start, output))
        else:
            times = [float(v) for v in args.at.replace("，", ",").split(",") if v.strip()]
            args.out_dir.mkdir(parents=True, exist_ok=True)
            with _Page(config) as page:
                for t in times:
                    path = args.out_dir / f"cta_{t:05.2f}s.png"
                    page.shot(min(t, page.duration), path)
                    print(path)
    except CtaError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
