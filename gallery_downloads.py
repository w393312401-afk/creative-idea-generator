"""Short-lived gallery ZIP downloads; stream the archive without browser Blob copies."""
import atexit
import contextlib
import os
from pathlib import Path
import secrets
import shutil
import stat
import tempfile
import threading
import time
import zipfile

from server_common import scan_gallery

BASE_DIR = Path(__file__).resolve().parent
TTL_SECONDS = 600
MAX_FILES = 1000
MAX_BYTES = 8 * 1024**3
_BUILD_SLOT = threading.Lock()
_CACHE_LOCK = threading.RLock()
_ARCHIVES = {}


class GalleryDownloadError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _remove(info):
    with contextlib.suppress(OSError):
        Path(info['path']).unlink()


def _expire(token):
    with _CACHE_LOCK:
        info = _ARCHIVES.pop(token, None)
    if info:
        _remove(info)


def _cleanup():
    with _CACHE_LOCK:
        records = list(_ARCHIVES.values())
        _ARCHIVES.clear()
    for info in records:
        _remove(info)


atexit.register(_cleanup)


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def prepare_download(paths, base_dir=None):
    if not isinstance(paths, list) or not paths or len(paths) > MAX_FILES:
        raise GalleryDownloadError(f'请选择 1–{MAX_FILES} 个文件下载')
    if any(not isinstance(path, str) or not path or len(path) > 4096 for path in paths):
        raise GalleryDownloadError('所选文件路径不正确，请刷新画廊后重试')
    if not _BUILD_SLOT.acquire(blocking=False):
        raise GalleryDownloadError('正在准备另一份下载，请稍后重试', 409)
    temporary = None
    try:
        base = Path(base_dir or BASE_DIR).resolve()
        root = (base / 'outputs').resolve()
        allowed = {item['path'] for group in scan_gallery(base_dir=str(base)).get('groups', [])
                   for item in group.get('items', [])}
        files = []
        total = 0
        for raw in dict.fromkeys(paths):
            if raw not in allowed:
                raise GalleryDownloadError('部分文件已不在画廊中，请刷新后重新选择')
            candidate = base / raw
            try:
                resolved = candidate.resolve(strict=True)
                archive_name = resolved.relative_to(root).as_posix()
                info = candidate.stat()
            except (OSError, ValueError):
                raise GalleryDownloadError('部分文件不存在或路径越界，请刷新后重新选择') from None
            if not stat.S_ISREG(info.st_mode) or candidate.is_symlink() or any(part.startswith('.') for part in Path(archive_name).parts):
                raise GalleryDownloadError('所选内容不是可下载的画廊文件')
            total += info.st_size
            files.append((candidate, archive_name, _identity(info)))
        if total > MAX_BYTES:
            raise GalleryDownloadError('本次文件超过 8 GB，请分批下载')
        if shutil.disk_usage(tempfile.gettempdir()).free < total + 64 * 1024**2:
            raise GalleryDownloadError('临时磁盘空间不足，请减少所选文件后重试', 507)
        descriptor, temporary = tempfile.mkstemp(prefix='spark-gallery-', suffix='.zip')
        with os.fdopen(descriptor, 'w+b') as sink, zipfile.ZipFile(sink, 'w', compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
            for candidate, name, identity in files:
                # Check again at open: generation can replace a selected file while packing.
                if not candidate.resolve(strict=True).is_relative_to(root):
                    raise GalleryDownloadError('文件路径在打包期间发生变化，请刷新后重试', 409)
                fd = os.open(candidate, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
                with os.fdopen(fd, 'rb') as source:
                    before = os.fstat(source.fileno())
                    if not stat.S_ISREG(before.st_mode) or _identity(before) != identity:
                        raise GalleryDownloadError('文件正在更新，请稍后重新下载', 409)
                    timestamp = time.localtime(before.st_mtime)[:6]
                    if not 1980 <= timestamp[0] <= 2107:
                        timestamp = (1980, 1, 1, 0, 0, 0)
                    entry = zipfile.ZipInfo(name, timestamp)
                    entry.file_size = before.st_size
                    entry.external_attr = 0o100644 << 16
                    with archive.open(entry, 'w', force_zip64=True) as target:
                        shutil.copyfileobj(source, target, length=1024 * 1024)
                    if _identity(os.fstat(source.fileno())) != identity:
                        raise GalleryDownloadError('文件正在更新，请稍后重新下载', 409)
        token = secrets.token_urlsafe(24)
        filename = 'SPARK_selected_' + time.strftime('%Y%m%d_%H%M%S') + '.zip'
        info = {'path': temporary, 'filename': filename, 'count': len(files),
                'size_bytes': os.path.getsize(temporary), 'expires_at': time.time() + TTL_SECONDS}
        with _CACHE_LOCK:
            # Bounded scratch storage; links are only needed long enough to start a download.
            while len(_ARCHIVES) >= 4:
                _expire(next(iter(_ARCHIVES)))
            _ARCHIVES[token] = info
        timer = threading.Timer(TTL_SECONDS, _expire, args=(token,))
        timer.daemon = True
        timer.start()
        temporary = None
        return {'url': '/api/gallery/download-zip/' + token, 'filename': filename,
                'count': info['count'], 'size_bytes': info['size_bytes']}
    except GalleryDownloadError:
        raise
    except OSError as exc:
        raise GalleryDownloadError('文件暂时无法打包，请刷新画廊后重试', 409) from exc
    finally:
        if temporary:
            _remove({'path': temporary})
        _BUILD_SLOT.release()


@contextlib.contextmanager
def open_download(token):
    # The unguessable short-lived token authorizes only this prepared archive;
    # download navigation cannot attach the app's usual authorization header.
    with _CACHE_LOCK:
        info = _ARCHIVES.get(token)
        if not info or info['expires_at'] <= time.time():
            _expire(token)
            raise GalleryDownloadError('下载链接已过期，请重新点击下载所选', 410)
        try:
            stream = open(info['path'], 'rb')
        except OSError:
            _expire(token)
            raise GalleryDownloadError('下载包已失效，请重新点击下载所选', 410) from None
    try:
        yield stream, info
    finally:
        stream.close()
        if info['expires_at'] <= time.time() or token not in _ARCHIVES:
            _remove(info)
