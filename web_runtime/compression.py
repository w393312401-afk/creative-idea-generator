"""Negotiated gzip with a byte-bounded, process-local cache (no build artifacts)."""
from collections import OrderedDict
import gzip
import threading


_CACHE = OrderedDict()
_CACHE_LOCK = threading.Lock()
_CACHE_BYTES = 0
_MAX_CACHE_BYTES = 8 * 1024 * 1024


def accepts_gzip(header):
    preferences = {}
    for part in str(header or '').lower().split(','):
        coding, *params = part.strip().split(';')
        quality = 1.0
        for param in params:
            key, sep, value = param.strip().partition('=')
            if key == 'q':
                try:
                    quality = float(value) if sep else 0.0
                except ValueError:
                    quality = 0.0
        preferences[coding.strip()] = quality if 0 <= quality <= 1 else 0.0
    return preferences.get('gzip', preferences.get('*', 0.0)) > 0


def compress_response(raw, accept_encoding):
    """Negotiate bounded responses; streams and media use their own delivery paths."""
    if len(raw) < 1024 or not accepts_gzip(accept_encoding):
        return raw, None
    encoded = gzip.compress(raw, compresslevel=6, mtime=0)
    return (encoded, 'gzip') if len(encoded) < len(raw) else (raw, None)


def gzip_body(stream, path, stat):
    """Use the opened file's identity; atomic replacement invalidates old entries."""
    global _CACHE_BYTES
    key = (str(path), stat.st_dev, stat.st_ino, stat.st_mtime_ns,
           stat.st_ctime_ns, stat.st_size)
    with _CACHE_LOCK:
        if key in _CACHE:
            _CACHE.move_to_end(key)
            return _CACHE[key]
    raw = stream.read()
    stream.seek(0)
    encoded = gzip.compress(raw, compresslevel=6, mtime=0)
    if len(encoded) >= len(raw):
        return None
    with _CACHE_LOCK:
        # Another request may have populated the same entry while we compressed.
        if key not in _CACHE:
            while _CACHE and _CACHE_BYTES + len(encoded) > _MAX_CACHE_BYTES:
                _, removed = _CACHE.popitem(last=False)
                _CACHE_BYTES -= len(removed)
            if len(encoded) <= _MAX_CACHE_BYTES:
                _CACHE[key] = encoded
                _CACHE_BYTES += len(encoded)
    return encoded
