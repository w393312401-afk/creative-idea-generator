"""Validate page assets and enforce checked-in raw/transfer-size budgets."""
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit, unquote
import gzip
import json

ROOT = Path(__file__).resolve().parents[1]


class AssetParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.assets = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'script' and attrs.get('src'):
            self.assets.append(attrs['src'])
        elif tag == 'link' and 'stylesheet' in attrs.get('rel', '').split() and attrs.get('href'):
            self.assets.append(attrs['href'])


def measure_page(root, page):
    root = root.resolve()
    parser = AssetParser()
    parser.feed((root / page).read_text())
    files = [root / page]
    scripts = 0
    for value in parser.assets:
        url = urlsplit(value)
        if url.scheme or url.netloc:
            continue
        path = ((root if url.path.startswith('/') else (root / page).parent)
                / unquote(url.path).lstrip('/')).resolve()
        if root not in path.parents or not path.is_file():
            raise ValueError(f'{page}: missing or out-of-project asset: {value}')
        files.append(path)
        scripts += path.suffix == '.js'
    raw = transfer = 0
    for path in files:
        body = path.read_bytes()
        raw += len(body)
        if path.suffix in {'.html', '.css', '.js', '.svg'} and 1024 <= len(body) <= 2 * 1024 * 1024:
            transfer += min(len(body), len(gzip.compress(body, compresslevel=6, mtime=0)))
        else:
            transfer += len(body)
    return {'raw_bytes': raw, 'gzip_bytes': transfer, 'scripts': scripts}


def check(root=ROOT):
    budgets = json.loads((root / 'tools/resource_budgets.json').read_text())
    pages, errors = {}, []
    for page, limits in budgets['pages'].items():
        try:
            pages[page] = measure_page(root, page)
        except (OSError, ValueError) as exc:
            errors.append(str(exc))
            continue
        for key, limit in limits.items():
            if pages[page][key] > limit:
                errors.append(f'{page} {key}: {pages[page][key]} exceeds {limit}')
    return {'pages': pages, 'errors': errors}


if __name__ == '__main__':
    result = check()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(bool(result['errors']))
