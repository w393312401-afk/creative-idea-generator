"""List or remove regenerable project caches; user data and environments are excluded."""
from pathlib import Path
import argparse
import json
import os
import subprocess

ROOT = Path(__file__).resolve().parents[1]
PROTECTED = {'.git', '.venv', 'venv', 'node_modules', 'outputs', 'runtime', 'logs',
             '.recovery', 'artifacts', 'library', 'tasks', 'scratch'}


def disposable(rel):
    return (rel.name == '.DS_Store'
            or ('__pycache__' in rel.parent.parts and rel.suffix == '.pyc')
            or '.pytest_cache' in rel.parent.parts)


def candidates(root, tracked=()):
    root = root.resolve()
    tracked = set(tracked)
    for directory, dirs, files in os.walk(root, followlinks=False):
        parent = Path(directory)
        dirs[:] = [name for name in dirs if name not in PROTECTED
                   and not (parent / name).is_symlink()]
        for name in files:
            path = parent / name
            rel = path.relative_to(root)
            if disposable(rel) and rel.as_posix() not in tracked and not path.is_symlink() and path.is_file():
                yield path


def clean(root, paths, tracked=()):
    """Recheck ancestry before each unlink; never follow links outside the project."""
    root = root.resolve()
    tracked = set(tracked)
    removed = 0
    for path in paths:
        if path.is_symlink() or any(p.is_symlink() for p in path.parents if p != root):
            continue
        resolved = path.resolve()
        if root not in resolved.parents or any(part in PROTECTED for part in resolved.relative_to(root).parts):
            continue
        rel = resolved.relative_to(root)
        if not disposable(rel) or rel.as_posix() in tracked:
            continue
        try:
            size = path.stat().st_size
            path.unlink()
            removed += size
        except FileNotFoundError:
            pass
    return removed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='Remove listed regenerable caches')
    args = parser.parse_args()
    tracked = subprocess.check_output(['git', '-C', str(ROOT), 'ls-files', '-z'], text=True).split('\0')
    paths = list(candidates(ROOT, tracked))
    report = {'mode': 'apply' if args.apply else 'preview', 'files': len(paths),
              'candidate_bytes': sum(path.stat().st_size for path in paths),
              'paths': [str(path.relative_to(ROOT)) for path in paths]}
    if args.apply:
        report['removed_bytes'] = clean(ROOT, paths, tracked)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
