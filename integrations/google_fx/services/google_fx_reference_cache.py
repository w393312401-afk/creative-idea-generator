"""Bounded, process-local cache of uploaded Flow reference assets.

Entries are proposals only: callers must verify their UUIDs on the current
canvas before mounting. Content hashes prevent reuse after replacing a file.
"""

import hashlib
import os
import threading
from collections import OrderedDict


def reference_cache_key(scope, path, role="primary"):
    if not scope:
        return None
    canonical_path = os.path.realpath(os.path.abspath(path))
    try:
        before = os.stat(canonical_path)
        digest = hashlib.sha256()
        with open(canonical_path, "rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        after = os.stat(canonical_path)
    except OSError:
        return None
    # Do not cache files being replaced while we read them.
    if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
        after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns
    ):
        return None
    return (*scope, canonical_path, role, digest.hexdigest())


class ReferenceUploadCache:
    def __init__(self, max_entries=512):
        self.max_entries = max_entries
        self._entries = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            value = self._entries.get(key)
            if value is not None:
                self._entries.move_to_end(key)
            return value

    def put(self, key, uuid):
        if key is None or not uuid:
            return
        with self._lock:
            # Keep only the latest content for this path/role in this canvas.
            for previous in list(self._entries):
                if previous[:-1] == key[:-1]:
                    del self._entries[previous]
            self._entries[key] = uuid
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)

    def content_changed(self, key):
        if key is None:
            return False
        with self._lock:
            return any(previous[:-1] == key[:-1] and previous[-1] != key[-1]
                       for previous in self._entries)

    def discard(self, key):
        with self._lock:
            self._entries.pop(key, None)


REFERENCE_UPLOAD_CACHE = ReferenceUploadCache()
