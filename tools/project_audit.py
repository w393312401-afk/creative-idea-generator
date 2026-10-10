"""Read-only, dependency-free inventory for the slimming programme."""
from __future__ import annotations

import ast
import hashlib
import json
import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
SOURCE_SUFFIXES = {'.py', '.js', '.css', '.html', '.md', '.txt', '.json', '.toml', '.sh', '.bat', '.command'}
SOURCE_DIRS = {'tests', 'tools', 'js', 'css', 'prompt_pipeline', 'integrations', 'skills', 'docs', 'examples', 'web_runtime', '.agents'}


def git_output(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True)


def source_files(root):
    """Include in-flight source edits, never ignored user data or credentials."""
    names = git_output(root, 'ls-files', '--cached', '--others', '--exclude-standard', '-z').split('\0')
    for name in sorted(set(filter(None, names))):
        rel = Path(name)
        path = root / rel
        if path.is_symlink() or not path.is_file() or path.suffix not in SOURCE_SUFFIXES:
            continue
        if any(parent.is_symlink() for parent in path.parents if parent != root):
            continue
        if len(rel.parts) > 1 and rel.parts[0] not in SOURCE_DIRS:
            continue
        if len(rel.parts) == 1 and path.suffix == '.json' and name != 'server_config.example.json':
            continue
        yield rel


def tree_bytes(path):
    if path.is_symlink():
        return 0
    if path.is_file():
        return path.stat().st_size
    if not path.is_dir():
        return 0  # Git fsmonitor sockets and other special files are not data.
    total = 0
    for child in path.iterdir():
        total += tree_bytes(child)
    return total


def inventory(root=ROOT):
    imports, routes, modules, contracts, polls = {}, [], {}, {}, {}
    for rel in source_files(root):
        path = root / rel
        content = path.read_text(encoding='utf-8', errors='replace')
        if path.suffix in {'.py', '.js', '.css', '.html'}:
            modules[str(rel)] = {'bytes': path.stat().st_size, 'lines': len(content.splitlines()),
                                 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
        if path.suffix == '.py':
            tree = ast.parse(content, filename=str(rel))
            imports[str(rel)] = sorted({
                ('.' * node.level + (node.module or '') + ':' + ','.join(n.name for n in node.names))
                if isinstance(node, ast.ImportFrom) else ','.join(n.name for n in node.names)
                for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))
            })
            routes.extend({'file': str(rel), 'path': route} for route in sorted(set(
                re.findall(r"['\"](/api/[^'\"\s]+)['\"]", content))))
        if path.suffix == '.js':
            hits = [i for i, line in enumerate(content.splitlines(), 1) if 'setInterval(' in line]
            if hits:
                polls[str(rel)] = hits
        if path.name == 'contract-registry.json':
            contracts[str(rel)] = sorted(set(re.findall(r'[\w.]+:[\w]+', content)))
    assets = {}
    for page in ('index.html', 'console.html'):
        content = (root / page).read_text()
        refs = re.findall(r'<script\b[^>]*\bsrc="([^"]+)"|<link\b[^>]*\bhref="([^"]+)"', content)
        entries = []
        for script, link in refs:
            url = urlsplit(script or link)
            if url.scheme or url.netloc:
                continue
            target = root / url.path.lstrip('/')
            entries.append({'path': url.path, 'kind': 'script' if script else 'link',
                            'bytes': target.stat().st_size if target.is_file() else None})
        assets[page] = {'resources': entries, 'local_bytes': sum(e['bytes'] or 0 for e in entries),
                        'script_requests': sum(e['kind'] == 'script' for e in entries)}
    return {
        'head': git_output(root, 'rev-parse', 'HEAD').strip(),
        'protected_worktree_status': git_output(root, 'status', '--short').splitlines(),
        'modules': modules, 'imports': imports, 'api_literal_inventory': routes,
        'dynamic_contract_references': contracts, 'interval_locations': polls, 'assets': assets,
        'disk_bytes': {name: tree_bytes(root / name) for name in
                       ('outputs', 'runtime', 'logs', '.venv', '.git', '.recovery') if (root / name).exists()},
        'notes': ['API literals are candidates, not a complete resolved router.',
                  'Source locations do not measure live latency or prove dead code.'],
    }


if __name__ == '__main__':
    print(json.dumps(inventory(), ensure_ascii=False, indent=2))
