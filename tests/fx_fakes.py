# -*- coding: utf-8 -*-
"""
🧪 离线 FX 复现夹具（清单 D6 / 测试第 8 项）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
这套夹具存在的唯一理由：**让"卡点 / 选择器漂移"这类问题变成可复现的测试**，
从而能交给 agent 修，而不是只能在有真账号 + 真 AdsPower + 真额度的机器上手工复现。

提供三样东西：

1. `FakeAdsPower` —— 本地 HTTP 服务，实现 AdsPower 本地 API 的三个端点
   （`/api/v1/user/list`、`/browser/start`、`/browser/stop`）。可以注入限频、
   超时挂死、返回坏 ws 地址等故障。

2. `flow_page_html(...)` —— 一份复刻 Flow 关键 DOM 的静态 HTML（prompt 输入框、
   Generate 按钮、底部配置按钮、账号头像菜单、积分文案）。可以按需"改版"：
   去掉某个元素、换掉 class、把积分文案换成定价宣传文案，用来复现选择器失效。

3. `FakePage` —— 只实现 probe_selectors / forensics 真正会用到的那一小片
   Playwright Page 协议（locator/count/is_visible/inner_text/url/is_closed/
   screenshot/content），配合 flow_page_html 就能离线跑选择器探针。
   它是个**极简 CSS 选择器匹配器**，只支持探针用到的语法，不是浏览器。

⚠️ 边界：FakePage 不执行 JS、不做布局，所以它只能验证"选择器还认不认得这个 DOM"，
不能替代 L1/L2 自检。真要验证交互，用 FakeAdsPower + 真 Playwright 指向
flow_page_html 起的本地页面。
"""

import json
import re
import socket
import threading
import time
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, HTTPServer


# ── 1. 假 AdsPower 本地 API ────────────────────────────────────────────────

class FakeAdsPower:
    """AdsPower 本地 API 的可注入故障替身。

    behaviors:
      rate_limit_times —— 前 N 次 user/list 返回限频错误
      hang_seconds     —— 每个请求先睡这么久（复现"AdsPower 卡住把控制台拖死"）
      dead_ws          —— browser/start 返回一个没人监听的 ws 端口
      start_error      —— browser/start 直接返回错误码
      profiles         —— user/list 要返回的环境列表
    """

    def __init__(self, profiles=None, rate_limit_times=0, hang_seconds=0.0,
                 dead_ws=False, start_error=None, page_size=100):
        self.profiles = profiles if profiles is not None else [
            {'user_id': f'u{i}', 'name': f'acct{i}@example.com', 'group_name': 'g'}
            for i in range(3)
        ]
        self.rate_limit_times = rate_limit_times
        self.hang_seconds = hang_seconds
        self.dead_ws = dead_ws
        self.start_error = start_error
        self.page_size = page_size
        self.calls = []
        self._list_calls = 0
        self._server = None
        self._thread = None
        self.port = None

    # -- 生命周期 --

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()

    def start(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass  # 测试输出不要被访问日志淹没

            def do_GET(self):
                owner.calls.append(self.path)
                if owner.hang_seconds:
                    time.sleep(owner.hang_seconds)
                if self.path.startswith('/api/v1/user/list'):
                    self._json(owner._user_list(self.path))
                elif self.path.startswith('/api/v1/browser/start'):
                    self._json(owner._browser_start())
                elif self.path.startswith('/api/v1/browser/stop'):
                    self._json({'code': 0, 'msg': 'success'})
                else:
                    self._json({'code': -1, 'msg': 'not found'}, status=404)

            def _json(self, payload, status=200):
                body = json.dumps(payload).encode('utf-8')
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = HTTPServer(('127.0.0.1', 0), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    # -- 端点行为 --

    def _user_list(self, path):
        self._list_calls += 1
        if self._list_calls <= self.rate_limit_times:
            return {'code': -1, 'msg': 'Too many request per second'}
        match = re.search(r'page=(\d+)', path)
        page = int(match.group(1)) if match else 1
        start = (page - 1) * self.page_size
        return {'code': 0, 'data': {'list': self.profiles[start:start + self.page_size]}}

    def _browser_start(self):
        if self.start_error:
            return {'code': -1, 'msg': self.start_error}
        if self.dead_ws:
            # 一个几乎肯定没人监听的端口：复现"AdsPower 说启动成功但调试端口没起来"
            return {'code': 0, 'data': {'ws': {'puppeteer': 'ws://127.0.0.1:1/devtools'}}}
        return {'code': 0, 'data': {'ws': {'puppeteer': f'ws://127.0.0.1:{self.port}/devtools'}}}


def unused_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class DeadListener:
    """只 accept 不响应的 TCP 服务：复现"端口通但请求永远不返回"。

    S3 的回归测试需要它——快照里那次 `socket.create_connection` 会成功，
    但任何走 HTTP 的调用都会挂死。
    """

    def __init__(self):
        self._sock = socket.socket()
        self._sock.bind(('127.0.0.1', 0))
        self._sock.listen(8)
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._accepted = []
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
                self._accepted.append(conn)  # 握手完就晾着，不回任何字节
            except OSError:
                return

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass
        for conn in self._accepted:
            try:
                conn.close()
            except OSError:
                pass


# ── 2. 复刻 Flow 关键 DOM ──────────────────────────────────────────────────

def flow_page_html(prompt_input=True, generate_button=True, config_button=True,
                   account_menu=True, credit_text='1050 Google Flow credits',
                   pricing_noise=True, new_project_button=True):
    """生成一份能被 UI_SELECTORS 命中的最小 Flow 页面。

    每个开关关掉一个元素，用来复现"Flow 改版后某个选择器族失效"。
    pricing_noise 默认开着：页面上同时存在定价宣传数字
    （"1,000 monthly Google Flow credits"），这正是积分探测绝不能扫全页文字的原因，
    留在这里让相关回归测试有真实的干扰源。
    """
    parts = ['<div id="app">']
    if new_project_button:
        parts.append(
            "<button aria-label='New project'>"
            "<i class='google-symbols'>add_2</i>New project</button>")
    if pricing_noise:
        parts.append("<div class='marketing'>Pro plan: 1,000 monthly Google Flow credits</div>")
    if account_menu:
        parts.append("<button aria-label='Account'><img class='avatar'/></button>")
        if credit_text:
            parts.append(f"<div role='dialog'><span class='credits'>{credit_text}</span></div>")
    if config_button:
        parts.append("<button aria-haspopup='dialog'>Nano Banana 2 · Portrait · 1x</button>")
    if prompt_input:
        parts.append("<div contenteditable='true' data-slate-editor='true'></div>")
        parts.append("<textarea placeholder='What do you want to create?'></textarea>")
    if generate_button:
        parts.append('<button>Generate</button>')
    parts.append('</div>')
    return '<html><body>' + ''.join(parts) + '</body></html>'


# ── 3. 极简 Page 替身 ─────────────────────────────────────────────────────

class _FakeLocator:
    def __init__(self, elements):
        self._elements = elements

    def count(self):
        return len(self._elements)

    @property
    def first(self):
        return _FakeLocator(self._elements[:1])

    @property
    def last(self):
        return _FakeLocator(self._elements[-1:])

    def is_visible(self, timeout=None):
        return bool(self._elements)

    def inner_text(self, timeout=None):
        if not self._elements:
            raise RuntimeError('locator 未命中任何元素')
        return self._elements[0]['text']

    def click(self, timeout=None):
        if not self._elements:
            raise RuntimeError('locator 未命中任何元素')
        self._elements[0].setdefault('clicks', 0)
        self._elements[0]['clicks'] += 1

    def scroll_into_view_if_needed(self):
        pass

    def hover(self):
        pass

    def focus(self):
        pass

    def press(self, key):
        pass

    def filter(self, has_text=None):
        if has_text is None:
            return self
        pattern = has_text if hasattr(has_text, 'search') else re.compile(re.escape(str(has_text)))
        return _FakeLocator([el for el in self._elements if pattern.search(el['text'])])


class FakePage:
    """只实现选择器探针 / 取证真正会用到的那一小片 Page 协议。

    支持的选择器语法（探针里出现的全部形态）：
      tag、[attr]、[attr='v']、[attr*='v']、:has-text('x')、:text-is('x')、
      #id、.class，以及用空格连接的后代关系（只按"后代里存在"近似匹配）。
    不支持的语法一律视为未命中——这会让探针把它报成 missing，
    所以夹具里的选择器写法要贴着真实 UI_SELECTORS 来，否则测的是夹具而不是代码。
    """

    def __init__(self, html=None, url='https://labs.google/fx/tools/flow/project/x',
                 closed=False):
        self.set_content(html if html is not None else flow_page_html())
        self.url = url
        self._closed = closed
        self.keyboard = _FakeKeyboard()
        self.screenshots = 0

    def set_content(self, html):
        self._html = html
        self._elements = _parse_elements(html)

    # -- Page 协议 --

    def locator(self, selector):
        if self._closed:
            raise RuntimeError('TargetClosedError: page has been closed')
        return _FakeLocator(_match(self._elements, selector))

    def is_closed(self):
        return self._closed

    def close(self):
        self._closed = True

    def content(self):
        if self._closed:
            raise RuntimeError('TargetClosedError')
        return self._html

    def inner_text(self, selector):
        if self._closed:
            raise RuntimeError('TargetClosedError')
        return re.sub(r'<[^>]+>', ' ', self._html)

    def screenshot(self, timeout=None, full_page=False):
        if self._closed:
            raise RuntimeError('TargetClosedError')
        self.screenshots += 1
        return b'\x89PNG\r\n\x1a\n' + b'fake'

    def bring_to_front(self):
        pass

    def evaluate(self, script, arg=None):
        return None

    def wait_for_selector(self, selector, state=None, timeout=None):
        if not _match(self._elements, selector):
            raise RuntimeError(f'wait_for_selector 超时: {selector}')
        return True

    def fill(self, selector, text, timeout=None):
        pass


class _FakeKeyboard:
    def __init__(self):
        self.presses = []

    def press(self, key):
        self.presses.append(key)

    def type(self, text):
        pass


_VOID_TAGS = {'img', 'br', 'input', 'hr', 'meta', 'link'}


class _TreeBuilder(HTMLParser):
    """用 stdlib 的 HTMLParser 建一棵浅树，再拍平成元素列表。

    早先这里用正则，但 `<tag ...>(.*?)</tag>` 会从最外层的 <html> 起就把整份文档
    吃掉，后面一个元素都匹配不到——扁平化必须建立在真正的嵌套解析之上。
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.elements = []
        self._stack = []

    def handle_starttag(self, tag, attrs):
        node = {'tag': tag.lower(), 'attrs': {k: (v or '') for k, v in attrs},
                'children': [], 'text_parts': []}
        if self._stack:
            self._stack[-1]['children'].append(node)
        self.elements.append(node)
        if tag.lower() not in _VOID_TAGS:
            self._stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if self._stack and self._stack[-1]['tag'] == tag.lower():
            self._stack.pop()

    def handle_endtag(self, tag):
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index]['tag'] == tag.lower():
                del self._stack[index:]
                return

    def handle_data(self, data):
        if self._stack and data.strip():
            self._stack[-1]['text_parts'].append(data)


def _node_text(node):
    parts = list(node['text_parts'])
    for child in node['children']:
        parts.append(_node_text(child))
    return ' '.join(part.strip() for part in parts if part.strip())


def _parse_elements(html):
    """把 HTML 解析成 [{tag, attrs, text, children}]（含嵌套信息，供 :has() 用）。"""
    builder = _TreeBuilder()
    builder.feed(html)
    builder.close()
    for node in builder.elements:
        node['text'] = _node_text(node)
    return builder.elements


def _tokenize_compound(selector):
    """把一个复合选择器拆成 (tag, [(kind, value), ...])。

    手写而不是用一条正则：`:has(i.x:text-is('a'))` 里的括号是嵌套的，
    `:has\\([^)]*\\)` 这种写法会停在第一个 `)` 上，把尾括号漏在外面导致整条
    选择器被判为"不支持"（表现为该族被误报成 missing）。
    返回 None 表示含有不支持的语法。
    """
    index, length = 0, len(selector)
    tag_chars = []
    while index < length and (selector[index].isalnum() or selector[index] in '-_*'):
        tag_chars.append(selector[index])
        index += 1
    tag = ''.join(tag_chars)
    conditions = []
    while index < length:
        char = selector[index]
        if char == '#' or char == '.':
            index += 1
            start = index
            while index < length and (selector[index].isalnum() or selector[index] in '-_'):
                index += 1
            if start == index:
                return None
            conditions.append(('id' if char == '#' else 'class', selector[start:index]))
        elif char == '[':
            end = selector.find(']', index)
            if end == -1:
                return None
            conditions.append(('attr', selector[index + 1:end]))
            index = end + 1
        elif char == ':':
            name_end = selector.find('(', index)
            if name_end == -1:
                return None
            name = selector[index + 1:name_end]
            body, index = _read_balanced(selector, name_end)
            if body is None:
                return None
            if name in ('has-text', 'text-is'):
                conditions.append((name, body.strip().strip("'\"")))
            elif name == 'has':
                conditions.append(('has', body.strip()))
            else:
                return None  # not/nth-child 等一律视为不支持
        else:
            return None
    return tag, conditions


def _read_balanced(text, open_index):
    """从 text[open_index] == '(' 读到配对的 ')'，返回 (内容, 右括号之后的下标)。"""
    depth, index, quote = 0, open_index, None
    start = open_index + 1
    while index < len(text):
        char = text[index]
        if quote:
            if char == quote:
                quote = None
        elif char in "'\"":
            quote = char
        elif char == '(':
            depth += 1
        elif char == ')':
            depth -= 1
            if depth == 0:
                return text[start:index], index + 1
        index += 1
    return None, len(text)


def _split_descendants(selector):
    """按**顶层**空白切分后代选择器。

    不能简单 partition(' ')：`[aria-label*='New project']` 和 `:has-text('a b')`
    里的空格属于选择器内部，切在那里会把选择器劈成两半（早先就是这么错的，
    表现为一堆本该命中的选择器全被报成 missing）。
    """
    parts, buffer, depth, quote = [], [], 0, None
    for char in selector:
        if quote:
            buffer.append(char)
            if char == quote:
                quote = None
            continue
        if char in "'\"":
            quote = char
            buffer.append(char)
        elif char in '[(':
            depth += 1
            buffer.append(char)
        elif char in '])':
            depth -= 1
            buffer.append(char)
        elif char.isspace() and depth == 0:
            if buffer:
                parts.append(''.join(buffer))
                buffer = []
        else:
            buffer.append(char)
    if buffer:
        parts.append(''.join(buffer))
    return parts


def _match(elements, selector):
    """极简选择器匹配。不支持的语法返回空（探针会如实报 missing）。"""
    selector = selector.strip()
    if not selector:
        return []
    # 后代选择器：只要求"祖先命中 且 页面里存在满足后代条件的元素"，
    # 对探针的"存在性"语义足够。
    segments = _split_descendants(selector)
    if len(segments) > 1:
        if not _match(elements, segments[0]):
            return []
        return _match(elements, ' '.join(segments[1:]))
    parsed = _tokenize_compound(selector)
    if parsed is None:
        return []
    tag, conditions = parsed
    result = []
    for element in elements:
        if tag and tag != '*' and element['tag'] != tag.lower():
            continue
        if all(_condition_ok(element, kind, value) for kind, value in conditions):
            result.append(element)
    return result


def _condition_ok(element, kind, value):
    if kind == 'id':
        return element['attrs'].get('id') == value
    if kind == 'class':
        return value in (element['attrs'].get('class') or '').split()
    if kind == 'attr':
        return _attr_ok(element, value)
    if kind == 'has-text':
        return value.lower() in element['text'].lower()
    if kind == 'text-is':
        return element['text'].strip() == value
    if kind == 'has':
        # :has(...) = 后代里存在匹配该子选择器的元素
        return bool(_match(_descendants(element), value))
    return False


def _descendants(node):
    out = []
    for child in node['children']:
        out.append(child)
        out.extend(_descendants(child))
    return out


def _attr_ok(element, expression):
    match = re.match(r"^([\w-]+)(?:(\*?=)'([^']*)')?$", expression.strip())
    if not match:
        return False
    name, operator, value = match.groups()
    if name not in element['attrs']:
        return False
    if operator is None:
        return True
    actual = element['attrs'][name]
    return value in actual if operator == '*=' else actual == value
