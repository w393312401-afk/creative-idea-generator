"""Import-time recovery must not read or audit the running service's leases."""
import subprocess
import sys
from pathlib import Path


def test_first_fx_import_ignores_real_stale_lease_and_keeps_audit_isolated():
    root = Path(__file__).resolve().parents[1]
    script = r'''
import builtins, io, json, os, runpy
from pathlib import Path
from unittest.mock import patch

namespace = runpy.run_path('tests/conftest.py')
real_state = str(Path('runtime/fx_control_state.json').resolve())
real_audit = str(Path('runtime/fx_audit.jsonl').resolve())
attempts = []
original_open = builtins.open

def offer_stale_lease(path, *args, **kwargs):
    name = str(Path(path).resolve()) if not isinstance(path, int) else ''
    if name == real_state:
        attempts.append('read_real_state')
        # The old implementation consumed this lease and tried to audit it.
        return io.StringIO(json.dumps({'schema': 1, 'pid': -1, 'leases': [
            {'task_id': 'production-task', 'user_id': 'production-account'}]}))
    if name == real_audit:
        attempts.append('write_real_audit')
        raise AssertionError('must not touch the service audit')
    return original_open(path, *args, **kwargs)

with patch('builtins.open', offer_stale_lease):
    namespace['pytest_sessionstart'](None)
    import fx_control
    control = fx_control.FX_CONTROL
    assert control.recovered is None
    control.audit('test.import_isolated')
    assert Path(control.audit_path).is_file()
    assert namespace['_real_state_path'](control.state_path) is None
    assert namespace['_real_state_path'](control.audit_path) is None
assert attempts == [], attempts
assert namespace['_state_write_violations'] == []
# Redirection must not weaken the write guard for any other runtime path.
try:
    original_open('runtime/test-isolation-must-not-write.json', 'w')
except PermissionError:
    pass
else:
    raise AssertionError('real-state write guard was bypassed')
assert len(namespace['_state_write_violations']) == 1
'''
    result = subprocess.run([sys.executable, '-c', script], cwd=root,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
