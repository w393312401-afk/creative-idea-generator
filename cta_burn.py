"""精剪成片的片尾引导动画：配置中心「成片引导」的服务端设置、自定义透明素材、烧录前准备。

设置和素材都放在 outputs/engagement_cta/ 下：
  .settings.json        开关、片尾秒数、用内置动画还是自定义视频（点开头，静态路由不公开）
  .cache/<键>.mov       内置动画按片尾秒数渲染的透明 MOV 缓存（见 tools/engagement_cta.py）
  custom/<sha>.<ext>    上传的自定义透明视频；custom/<sha>.preview.webm 供配置中心预览

精剪 worker 在成片核验通过后调用 prepare() 读取**当时**的设置：关闭则不烧录；内置动画按片尾
秒数渲染（有缓存直接复用）；自定义视频直接使用。叠加命令由 tools.engagement_cta.apply_command
生成，worker 用自己可取消的进程管理执行。
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import threading
import time
from urllib.parse import quote
import uuid

ROOT = Path(__file__).resolve().parent / 'outputs' / 'engagement_cta'
SETTINGS_NAME = '.settings.json'
SECONDS_CHOICES = (5, 6, 7, 8, 10)
DEFAULTS = {'enabled': True, 'seconds': 5, 'source': 'builtin'}
CUSTOM_SUFFIXES = ('.mov', '.webm', '.mkv')
MAX_CUSTOM_BYTES = 300 * 1024 * 1024
MAX_CUSTOM_SECONDS = 60
BUILTIN_NAME = '内置无文字互动动画'
_LOCK = threading.Lock()


class CtaSettingsError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _root(root):
    return Path(root) if root else ROOT


def _read_raw(root):
    try:
        data = json.loads((root / SETTINGS_NAME).read_text('utf-8'))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_raw(root, data):
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / f'{SETTINGS_NAME}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp'
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), 'utf-8')
    os.replace(tmp, root / SETTINGS_NAME)


def _custom_path(root, relative):
    """custom 记录里的相对路径 → 绝对路径；越出 custom/ 目录的一律视为无效。"""
    if not isinstance(relative, str) or not relative:
        return None
    custom_dir = (root / 'custom').resolve()
    path = (root / relative).resolve()
    try:
        path.relative_to(custom_dir)
    except ValueError:
        return None
    return path


def load_settings(root=None):
    """当前生效的设置。自定义视频文件丢失时回落到内置动画，并标记 custom_missing。"""
    root = _root(root)
    raw = _read_raw(root)
    settings = dict(DEFAULTS, custom=None, custom_missing=False)
    if isinstance(raw.get('enabled'), bool):
        settings['enabled'] = raw['enabled']
    if raw.get('seconds') in SECONDS_CHOICES:
        settings['seconds'] = int(raw['seconds'])
    if raw.get('source') in ('builtin', 'custom'):
        settings['source'] = raw['source']
    custom = raw.get('custom')
    if isinstance(custom, dict):
        path = _custom_path(root, custom.get('file'))
        if path and path.is_file():
            settings['custom'] = custom
        else:
            settings['custom_missing'] = True
    if settings['source'] == 'custom' and not settings['custom']:
        settings['source'] = 'builtin'
    return settings


def _stored(settings):
    return {key: settings[key] for key in ('enabled', 'seconds', 'source', 'custom')}


def public_settings(root=None):
    root = _root(root)
    settings = load_settings(root)
    custom = settings['custom']
    public_custom = None
    if custom:
        def url(relative):
            return '/outputs/engagement_cta/' + quote(relative, safe='/') if relative else ''
        public_custom = {key: custom.get(key) for key in ('name', 'duration', 'width', 'height', 'size_bytes', 'uploaded_at')}
        public_custom.update(url=url(custom['file']), preview_url=url(custom.get('preview')))
    return {
        'settings': {key: settings[key] for key in ('enabled', 'seconds', 'source')},
        'custom': public_custom,
        'custom_missing': settings['custom_missing'],
        'seconds_choices': list(SECONDS_CHOICES),
        'builtin_preview_url': f"/tools/engagement_cta.html?embed=1&bg=checker&duration={settings['seconds']}",
        'limits': {'max_bytes': MAX_CUSTOM_BYTES, 'max_seconds': MAX_CUSTOM_SECONDS,
                   'suffixes': list(CUSTOM_SUFFIXES)},
    }


def save_settings(patch, root=None):
    root = _root(root)
    if not isinstance(patch, dict) or not patch:
        raise CtaSettingsError('设置内容不正确')
    unknown = set(patch) - {'enabled', 'seconds', 'source'}
    if unknown:
        raise CtaSettingsError(f'不支持的设置项：{", ".join(sorted(unknown))}')
    with _LOCK:
        settings = load_settings(root)
        if 'enabled' in patch:
            if not isinstance(patch['enabled'], bool):
                raise CtaSettingsError('开关值不正确')
            settings['enabled'] = patch['enabled']
        if 'seconds' in patch:
            seconds = patch['seconds']
            if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds not in SECONDS_CHOICES:
                raise CtaSettingsError(f'片尾时长只能选 {"、".join(map(str, SECONDS_CHOICES))} 秒')
            settings['seconds'] = int(seconds)
        if 'source' in patch:
            if patch['source'] not in ('builtin', 'custom'):
                raise CtaSettingsError('引导视频来源不正确')
            if patch['source'] == 'custom' and not settings['custom']:
                raise CtaSettingsError('请先上传自定义透明视频')
            settings['source'] = patch['source']
        _write_raw(root, _stored(settings))
    return public_settings(root)


def _remove_custom_files(root, custom):
    for key in ('file', 'preview'):
        path = _custom_path(root, (custom or {}).get(key))
        if path:
            path.unlink(missing_ok=True)


def _make_preview(source, info, preview):
    """转一份小尺寸 VP9 透明 WebM：ProRes MOV 在 Chrome 里放不了，配置中心靠它预览。失败不影响上传。"""
    from tools import engagement_cta
    try:
        subprocess.run([engagement_cta._ffmpeg(), '-y', '-v', 'error', '-nostdin', *info['decoder'],
                        '-i', str(source), '-t', str(MAX_CUSTOM_SECONDS), '-an',
                        '-vf', "scale=-2:'min(640,ih)':flags=lanczos,format=yuva420p",
                        '-c:v', 'libvpx-vp9', '-pix_fmt', 'yuva420p', '-b:v', '0', '-crf', '34',
                        '-row-mt', '1', '-auto-alt-ref', '0', str(preview)],
                       capture_output=True, timeout=180, check=True)
        return preview.is_file() and preview.stat().st_size > 0
    except (OSError, subprocess.SubprocessError, engagement_cta.CtaError):
        preview.unlink(missing_ok=True)
        return False


def store_custom(data, filename, root=None):
    """校验并保存一段自定义透明视频，保存成功即切换为使用它。"""
    from tools import engagement_cta
    root = _root(root)
    name = Path(str(filename or '')).name.strip()[:120] or 'custom'
    suffix = Path(name).suffix.lower()
    if suffix not in CUSTOM_SUFFIXES:
        raise CtaSettingsError('请上传带透明通道的 .mov 或 .webm 视频')
    if not data:
        raise CtaSettingsError('上传的文件是空的')
    if len(data) > MAX_CUSTOM_BYTES:
        raise CtaSettingsError(f'视频超过 {MAX_CUSTOM_BYTES // 1048576} MB 上限', status=413)
    custom_dir = root / 'custom'
    custom_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(data).hexdigest()[:16]
    upload = custom_dir / f'.upload-{uuid.uuid4().hex}{suffix}'
    try:
        upload.write_bytes(data)
        try:
            info = engagement_cta.probe(upload)
        except engagement_cta.CtaError:
            raise CtaSettingsError('无法读取这个视频文件，请确认它能正常播放') from None
        if not info['alpha']:
            raise CtaSettingsError('这个视频没有透明通道。请导出 ProRes 4444（含 Alpha）MOV 或 VP9 透明 WebM。')
        if not 0 < info['duration'] <= MAX_CUSTOM_SECONDS:
            raise CtaSettingsError(f'引导视频需要在 {MAX_CUSTOM_SECONDS} 秒以内')
        final = custom_dir / f'{digest}{suffix}'
        os.replace(upload, final)
        preview = custom_dir / f'{digest}.preview.webm'
        has_preview = _make_preview(final, info, preview)
    finally:
        upload.unlink(missing_ok=True)
    record = {
        'file': f'custom/{final.name}',
        'preview': f'custom/{preview.name}' if has_preview else '',
        'name': name,
        'duration': round(info['duration'], 3),
        'width': info['width'],
        'height': info['height'],
        'size_bytes': len(data),
        'uploaded_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
    }
    with _LOCK:
        settings = load_settings(root)
        previous = _read_raw(root).get('custom')
        settings.update(custom=record, source='custom')
        _write_raw(root, _stored(settings))
        if isinstance(previous, dict) and previous.get('file') != record['file']:
            _remove_custom_files(root, previous)
    return public_settings(root)


def remove_custom(root=None):
    root = _root(root)
    with _LOCK:
        settings = load_settings(root)
        _remove_custom_files(root, _read_raw(root).get('custom'))
        settings.update(custom=None, source='builtin')
        _write_raw(root, _stored(settings))
    return public_settings(root)


def prepare(root=None, fps=30):
    """精剪成片烧录前调用：返回 None（未开启）或 {'source', 'seconds', 'overlay', 'name'}。

    内置动画首次使用某个片尾秒数时会用无头 Chromium 渲染一次（约 10 秒），之后走缓存。"""
    root = _root(root)
    settings = load_settings(root)
    if not settings['enabled']:
        return None
    seconds = settings['seconds']
    if settings['source'] == 'custom':
        custom = settings['custom']
        return {'source': 'custom', 'seconds': seconds, 'name': custom.get('name') or '自定义视频',
                'overlay': str(_custom_path(root, custom['file']))}
    from tools import engagement_cta
    overlay = engagement_cta.cached_overlay({'duration': float(seconds)}, fps, root / '.cache')
    return {'source': 'builtin', 'seconds': seconds, 'name': BUILTIN_NAME, 'overlay': str(overlay)}
