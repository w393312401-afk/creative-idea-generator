"""Run pytest in a disposable checkout with fail-closed I/O guards.

Used by check_project.py, not a replacement for an OS security sandbox.
Browser/native subprocess integration tests require a separately reviewed harness.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def make_guard(root, on_external=None):
    root = Path(root).resolve()

    def writable(value):
        if isinstance(value, int) or value is None:
            return
        path = Path(os.fsdecode(value)).resolve()
        if path == Path(os.devnull) or path == root or root in path.parents:
            return
        raise PermissionError(f'OFFLINE_GUARD: write outside disposable checkout: {path}')

    def audit(event, args):
        if event in {'socket.connect', 'socket.connect_ex', 'socket.sendto', 'subprocess.Popen', 'os.system', 'os.posix_spawn'}:
            if on_external is not None:
                on_external(event)
            raise PermissionError(f'OFFLINE_GUARD: {event} disabled in offline baseline')
        if event == 'open':
            path, mode, flags = args
            if (isinstance(mode, str) and any(c in mode for c in 'wax+')) or (
                flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
            ):
                writable(path)
        elif event in {'os.remove', 'os.rmdir', 'os.mkdir', 'os.chmod', 'os.utime', 'os.truncate'}:
            writable(args[0])
        elif event in {'os.rename', 'os.link', 'os.symlink'}:
            writable(args[0])
            writable(args[1])

    return audit


def install_guard(root, on_external=None):
    sys.addaudithook(make_guard(root, on_external))


if __name__ == '__main__':
    import pytest

    root = Path.cwd()
    sys.path.insert(0, str(root))
    # pytest's skip exception derives from BaseException, so application code
    # cannot swallow it in `except Exception` and report a misleading business
    # failure (e.g. ffmpeg returning None). Skips remain explicit in -ra/JUnit.
    # Writes outside the checkout are still hard failures, never skips.
    class OfflinePolicy:
        executing_test = False

        @pytest.hookimpl(hookwrapper=True)
        def pytest_runtest_protocol(self, item, nextitem):
            self.executing_test = True
            try:
                yield
            finally:
                self.executing_test = False

        def external(self, event):
            # Optional import-time capability probes may catch PermissionError;
            # never skip an entire module just because one probe is unavailable.
            if self.executing_test:
                pytest.skip(f'OFFLINE_INTEGRATION_REQUIRED: {event}; '
                            'needs a separately reviewed native/browser harness')

    policy = OfflinePolicy()
    install_guard(root, policy.external)
    # Match first-run installation without importing the user's private config.
    # Use the existing placeholder-scrubbing bootstrap inside the guarded copy.
    import runpy
    bootstrap = runpy.run_path(str(root / 'tools' / 'bootstrap_config.py'))
    bootstrap['main']()
    # conftest imports these in autouse fixtures. Load them with collection-time
    # (PermissionError) semantics even for a tiny focused test selection, so an
    # optional platform probe cannot skip every selected test during setup.
    import importlib
    for module in ('server_common', 'prompt_pipeline'):
        importlib.import_module(module)
    raise SystemExit(pytest.main(sys.argv[1:], plugins=[policy]))
