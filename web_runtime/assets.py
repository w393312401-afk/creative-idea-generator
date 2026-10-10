"""Content versions for public front-end assets, without a build step."""
import hashlib
import html
from html.parser import HTMLParser
import re
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit, urlunsplit


def content_version(raw):
    """Hash bytes, not timestamps: replacing files may preserve their mtime."""
    return hashlib.sha256(raw).hexdigest()


def is_frontend_asset(path):
    path = unquote(urlsplit(path).path)
    if '\\' in path or any(part.startswith('.') for part in path.split('/') if part):
        return False
    return ((path.startswith('/js/') and path.endswith('.js'))
            or (path.startswith('/css/') and path.endswith('.css'))
            or path in {'/app.js', '/console.js'})


_ATTR = re.compile(r'''([^\s/>=]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?''')
_TAG = re.compile(r'<\s*[^\s/>]+')


class _AssetTags(HTMLParser):
    def __init__(self, source):
        super().__init__(convert_charrefs=False)
        self.offsets = [0]
        # HTMLParser counts only LF as a new line, unlike str.splitlines(), which
        # also splits bare CR and Unicode line separators.
        for line in source.split('\n')[:-1]:
            self.offsets.append(self.offsets[-1] + len(line) + 1)
        self.tags = []
        self.base = None

    def handle_starttag(self, tag, attrs):
        values = {}
        for name, value in attrs:
            values.setdefault(name, value)
        if tag == 'base' and self.base is None and values.get('href'):
            self.base = values['href']
        name = 'src' if tag == 'script' else 'href' if tag == 'link' else None
        if not name or not values.get(name):
            return
        if tag == 'link':
            rel = str(values.get('rel') or '').lower().split()
            if 'stylesheet' not in rel and not ('preload' in rel and str(values.get('as') or '').lower() == 'style'):
                return
        raw = self.get_starttag_text()
        start = _TAG.match(raw)
        if not start:
            return
        line, column = self.getpos()
        offset = self.offsets[line - 1] + column
        for attr in _ATTR.finditer(raw, start.end()):
            if attr.group(1).lower() != name:
                continue
            group = next((n for n in (2, 3, 4) if attr.group(n) is not None), None)
            if group is not None:
                self.tags.append((offset + attr.start(group), offset + attr.end(group), values[name]))
            break

    handle_startendtag = handle_starttag


def rewrite_html_assets(raw, page_url, digest_for_path):
    """Change only real script/style attributes; preserve all other HTML bytes.

    The callback resolves allowed paths inside the static root and hashes their
    actual bytes. Missing, external, and private references remain untouched.
    """
    try:
        source = raw.decode('utf-8')
    except UnicodeDecodeError:
        return raw
    parser = _AssetTags(source)
    parser.feed(source)
    parser.close()
    page = urlsplit(page_url)
    base = urljoin(page_url, parser.base) if parser.base else page_url
    edits = []
    digests = {}
    for start, end, reference in parser.tags:
        resolved = urlsplit(urljoin(base, reference))
        if resolved.scheme != page.scheme or resolved.netloc != page.netloc or not is_frontend_asset(resolved.path):
            continue
        if resolved.path not in digests:
            digests[resolved.path] = digest_for_path(resolved.path)
        digest = digests[resolved.path]
        if not digest:
            continue
        original = urlsplit(reference)
        query = [(key, value) for key, value in parse_qsl(original.query, keep_blank_values=True) if key != 'v']
        query.append(('v', digest))
        versioned = urlunsplit(original._replace(query=urlencode(query)))
        edits.append((start, end, html.escape(versioned, quote=True)))
    for start, end, value in reversed(edits):
        source = source[:start] + value + source[end:]
    return source.encode('utf-8')
