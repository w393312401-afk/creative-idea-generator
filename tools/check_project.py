"""Offline regression baseline in a disposable source copy (Python + JS).

The copy and reports are retained under the printed temporary directory for
inspection. No production credentials, outputs, runtime state, or git history
are copied. No original worktree files are changed by test execution.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from project_audit import ROOT, inventory, source_files
from check_resources import check as check_resources


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--python-only', action='store_true')
    parser.add_argument('--js-only', action='store_true')
    parser.add_argument('--timeout', type=int, default=600)
    parser.add_argument('--maxfail', type=int, default=0,
                        help='Stop Python after N failures; 0 runs the entire suite')
    parser.add_argument('tests', nargs='*', help='Optional relative Python test paths')
    args = parser.parse_args()
    if args.python_only and args.js_only:
        parser.error('choose only one test language')
    if args.timeout <= 0 or args.maxfail < 0:
        parser.error('timeout must be positive and maxfail must be non-negative')
    resource_report = check_resources(ROOT)
    if resource_report['errors']:
        print(json.dumps(resource_report, ensure_ascii=False, indent=2))
        return 1
    selected_tests = []
    for name in args.tests:
        target = (ROOT / name).resolve()
        if ROOT / 'tests' not in target.parents or target.suffix != '.py':
            parser.error('test paths must be Python files under tests/')
        selected_tests.append(str(target.relative_to(ROOT)))
    # macOS /var is a symlink to /private/var. Node's permission model needs
    # the canonical path, otherwise module resolution fails before tests run.
    snapshot = Path(tempfile.mkdtemp(prefix='spark-offline-')).resolve()
    checkout = snapshot / 'source'
    checkout.mkdir()
    baseline = inventory()
    (snapshot / 'baseline.json').write_text(json.dumps(baseline, ensure_ascii=False, indent=2))
    for rel in source_files(ROOT):
        dest = checkout / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, dest)
    temp = checkout / '.test-tmp'
    temp.mkdir()
    # Do not inherit model credentials, proxy settings, or runtime overrides.
    env = {key: value for key, value in os.environ.items() if key in {
        'PATH', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT', 'LANG', 'LC_ALL',
    }}
    env.update({'TMPDIR': str(temp), 'TEMP': str(temp), 'TMP': str(temp),
                'PYTHONDONTWRITEBYTECODE': '1', 'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1',
                'PYTHONIOENCODING': 'utf-8'})
    results = []
    commands = []
    if not args.js_only:
        commands.append(('python', [sys.executable, 'tools/offline_pytest.py', '-q',
                                   '--tb=short', '-ra', '--maxfail=' + str(args.maxfail),
                                   '--junitxml=' + str(checkout / 'python-results.xml'),
                                   '--basetemp=' + str(temp / 'pytest'),
                                   *(selected_tests or ['tests'])]))
    if not args.python_only:
        node = shutil.which('node')
        if node:
            for path in sorted((checkout / 'tests').glob('test_*.js')):
                commands.append((path.name, [node, '--permission', '--allow-fs-read=' + str(checkout),
                                              '--require', str(checkout / 'tools/offline_node.js'),
                                              str(path)]))
        else:
            results.append({'name': 'javascript', 'exit_code': 127, 'error': 'node unavailable'})
    print(f'Isolated checkout and reports: {snapshot}', flush=True)
    for name, command in commands:
        started = time.monotonic()
        log = snapshot / (name + '.log')
        with log.open('w', encoding='utf-8') as output:
            try:
                result = subprocess.run(command, cwd=checkout, env=env, stdout=output,
                                        stderr=subprocess.STDOUT, timeout=args.timeout)
                code = result.returncode
            except subprocess.TimeoutExpired:
                code = 124
                output.write('\nTimed out; baseline is incomplete.\n')
        results.append({'name': name, 'exit_code': code, 'seconds': round(time.monotonic() - started, 3),
                        'log': str(log)})
        if name == 'python':
            junit = checkout / 'python-results.xml'
            if junit.exists():
                suites = list(ET.parse(junit).getroot().iter('testsuite'))
                counts = {key: sum(int(suite.get(key, '0')) for suite in suites)
                          for key in ('tests', 'failures', 'errors', 'skipped')}
                results[-1]['junit_counts'] = counts
                print(f'Python JUnit counts (including subtests): {counts}', flush=True)
                if code == 0 and counts['tests'] <= counts['skipped']:
                    code = 3
                    results[-1].update(exit_code=code, error='No non-skipped tests executed')
        print(f'{name}: exit={code} ({results[-1]["seconds"]}s)', flush=True)
    (snapshot / 'results.json').write_text(json.dumps(results, indent=2))
    return int(any(item['exit_code'] for item in results))


if __name__ == '__main__':
    raise SystemExit(main())
