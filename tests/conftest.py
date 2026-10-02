"""Pytest 引导:把仓库根钉到 sys.path。

`python -m pytest` 只会把「调用时的工作目录」放进 sys.path,因此从非仓库根目录运行时,
`import prompt_pipeline` / `import server_common` 等会 ModuleNotFoundError。这里显式把仓库根
(本文件的上一级目录)插到 sys.path 最前,确保导入与工作目录无关。

与 pyproject.toml 的 `pythonpath = "."` 互为双保险(conftest 不依赖 pytest 版本、也能在
直接运行单个测试文件时生效),并为后续把巨石文件拆成包(prompt_pipeline/ 等)保驾护航。
"""
import os
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


# ── 真实运行数据写保护（fail-closed） ──────────────────────────────────────
# 下面那些 _isolate_* fixture 是"已知路径逐个重定向"，漏一个就是一次静默污染
# （2026-10-01 实测：代理轮换计数、选择器统计、fx_debug、Errors、packet 缓存都被测试写过）。
# 这里在进程级审计钩子上兜底：任何测试往仓库的运行数据目录或根目录状态文件里写、删、
# 改名，一律当场 PermissionError 拦下，并在该测试 teardown 时判失败——业务代码即使
# except Exception 吞掉了异常，也逃不过 teardown 的检查。只读不受影响。
_PROTECTED_DIRS = ('runtime', 'logs', 'outputs', 'tasks', 'library')
_PROTECTED_ROOT_SUFFIXES = ('.json', '.bak', '.log', '.pid')
# 有意写进真实 outputs 的用例：真服务只从仓库 outputs/ 出图，fixture 自带清理。
_ALLOWED_STATE_PATHS = ('outputs/e2e_restore_demo',)
_REAL_ROOT = os.path.normcase(os.path.realpath(_REPO_ROOT))
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
_state_write_violations = []
_state_write_reported = 0


def _real_state_path(value):
    """value 落在受保护的真实运行数据里时返回仓库相对路径，否则 None。"""
    if value is None or isinstance(value, int):
        return None
    try:
        path = os.path.normcase(os.path.realpath(os.fsdecode(value)))
        rel = os.path.relpath(path, _REAL_ROOT).replace(os.sep, '/')
    except (TypeError, ValueError):  # Windows 跨盘符 relpath 会抛 ValueError
        return None
    if rel == '.' or rel.startswith('../'):
        return None
    if any(rel == allowed or rel.startswith(allowed + '/') for allowed in _ALLOWED_STATE_PATHS):
        return None
    parts = rel.split('/')
    if parts[0] in _PROTECTED_DIRS or (len(parts) == 1 and rel.endswith(_PROTECTED_ROOT_SUFFIXES)):
        return rel
    return None


def _guard_real_state(event, args):
    if event == 'open':
        path, mode, flags = args
        if not ((isinstance(mode, str) and any(c in mode for c in 'wax+')) or flags & _WRITE_FLAGS):
            return
        targets = (path,)
    elif event in ('os.remove', 'os.rmdir'):
        # (path, dir_fd)，未传 dir_fd 时审计参数是 -1。shutil.rmtree / TemporaryDirectory
        # 清理时按目录 fd 删裸文件名，那不是相对 cwd 的路径；整棵删受保护目录会先触发
        # 带完整路径的 shutil.rmtree 事件。
        targets = (args[0],) if args[1] in (None, -1) else ()
    elif event in ('os.truncate', 'shutil.rmtree'):
        targets = (args[0],)
    elif event == 'os.rename':  # (src, dst, src_dir_fd, dst_dir_fd)；os.replace 也走这个事件
        targets = tuple(path for path, fd in zip(args[:2], args[2:4]) if fd in (None, -1))
    else:
        return
    for target in targets:
        rel = _real_state_path(target)
        if rel:
            import threading
            _state_write_violations.append(
                f'{event} {rel}（线程 {threading.current_thread().name}）')
            raise PermissionError(f'TEST_ISOLATION: 测试不得改写真实运行数据 {rel}（{event}）')


sys.addaudithook(_guard_real_state)


@pytest.fixture(autouse=True)
def _forbid_real_state_writes():
    yield
    global _state_write_reported
    leaked = _state_write_violations[_state_write_reported:]
    _state_write_reported = len(_state_write_violations)
    if leaked:
        pytest.fail('测试试图改写真实运行数据（已拦截，请把路径重定向到 tmp_path）：\n  '
                    + '\n  '.join(leaked), pytrace=False)


@pytest.fixture(autouse=True)
def _isolate_macos_window_io(monkeypatch):
    """窗口策略单测不得通过 AppleScript 改变开发机的前台应用。"""
    from integrations.google_fx.utils import macos_window
    monkeypatch.setattr(macos_window, '_osascript',
                        lambda script: (False, '', 'window IO disabled in tests'))
    monkeypatch.setattr(macos_window, '_jxa',
                        lambda script: (False, '', 'window IO disabled in tests'), raising=False)


@pytest.fixture(autouse=True)
def _default_fx_concurrency(monkeypatch):
    """并发数/出口策略默认走"串行 + hard"：串行语义的用例不能被开发机 shell 里的同名环境变量
    或别的用例通过 apply_direct_env 留下的值改掉。需要并发的用例自己 setenv。"""
    monkeypatch.delenv('SPARK_FX_MAX_CONCURRENT', raising=False)
    monkeypatch.delenv('SPARK_FX_EGRESS_POLICY', raising=False)


@pytest.fixture(autouse=True)
def _isolate_open_adspower_inventory(monkeypatch):
    """选号单测不得发现或关掉开发机当前打开的真实浏览器。"""
    from integrations.google_fx.utils import browser
    original = browser.list_running_ads_browsers
    monkeypatch.setattr(browser, 'list_running_ads_browsers', lambda port=None, strict=False: [])
    monkeypatch.setattr(browser, 'stop_ads_browser', lambda user_id=None, port=None: True)
    return original


@pytest.fixture
def ads_inventory_reader(monkeypatch, _isolate_open_adspower_inventory):
    """Use real inventory parsing against an explicitly mocked/local test API."""
    from integrations.google_fx.utils import browser
    monkeypatch.setattr(browser, 'list_running_ads_browsers', _isolate_open_adspower_inventory)


@pytest.fixture
def offline_fx_video_io(monkeypatch):
    """Opt-in guard for video unit tests; integration tests keep their own IO.

    Every account/proxy action must be an explicit test double. pytest.fail
    raises outside normal Exception handling, so retry loops cannot swallow a
    missing mock and silently continue. The transport guards also catch aliases
    imported before this fixture was installed.
    """
    import requests
    from playwright.sync_api import BrowserType
    from integrations.google_fx.services import google_fx_credit
    from integrations.google_fx.utils import account_pool, proxy_rotator

    def denied(*args, **kwargs):
        pytest.fail("video unit test attempted live account/proxy/browser IO; add an explicit mock")

    monkeypatch.setattr(account_pool, 'switch_to_next_account', denied)
    monkeypatch.setattr(google_fx_credit, 'probe_flow_credit', denied)
    monkeypatch.setattr(proxy_rotator, 'ProxyRotator', denied)
    monkeypatch.setattr(requests.sessions.Session, 'send', denied)
    monkeypatch.setattr(BrowserType, 'connect', denied)
    monkeypatch.setattr(BrowserType, 'connect_over_cdp', denied)


@pytest.fixture(autouse=True)
def _isolate_qa_gate_level(monkeypatch):
    """把 qaGateLevel 的两个环境来源从所有测试里剥离：开发机 server_config.json
    配了 lenient/off、或导出了 SPARK_QA_GATE_LEVEL 时，视觉门会提前短路返回，
    大量以 config={} 调门的既有测试（strictGates fail-closed、锚点门 PASS/FAIL
    契约等）会静默反转。测试要什么档位，一律在自己的 config dict 里显式声明。"""
    import server_common
    monkeypatch.delenv('SPARK_QA_GATE_LEVEL', raising=False)
    monkeypatch.delitem(server_common.SERVER_CONFIG, 'qaGateLevel', raising=False)
    # 用户的一键关闭偏好也不能改变未显式指定配置的测试语义。
    monkeypatch.delenv('SPARK_REVIEWS_DISABLED', raising=False)
    monkeypatch.delitem(server_common.SERVER_CONFIG, 'reviewsDisabled', raising=False)


@pytest.fixture(autouse=True)
def _isolate_repo_root_state_files(tmp_path_factory, monkeypatch):
    """把仓库根的三个「本机运行时状态文件」在所有测试里重定向到临时目录。

    这些常量是模块导入期就算好的绝对路径（基于 _REPO_ROOT），因此 monkeypatch.chdir
    之类的隔离手法穿不透它们：
      - search_snippet_cache.json —— 联网摘要 6 小时缓存
      - trend_refs.json / trend_refs.archive.json —— 联网参考案例库
      - compose_checkpoints.json —— 提示词合成断点续传存档

    不隔离会双向出事：真实缓存/案例库被喂进测试（2026-07-25 实测
    test_trend_ref_compose_usage 里"联网通道应当拿不到东西"的前提被真实缓存击穿），
    以及测试反过来把桩数据写进开发者的真实案例库。多数测试各自 patch 过其中一两个，
    但那是逐个测试的自觉，漏一个就是一个隐蔽 flake —— 这里统一兜底。
    需要断言具体路径行为的测试仍可自行 patch 覆盖本 fixture。"""
    import prompt_pipeline
    state_dir = tmp_path_factory.mktemp('repo_state')
    for attr, filename in (
        ('SEARCH_SNIPPET_CACHE_PATH', 'search_snippet_cache.json'),
        ('TREND_REFS_PATH', 'trend_refs.json'),
        ('TREND_REFS_ARCHIVE_PATH', 'trend_refs.archive.json'),
        ('COMPOSE_CHECKPOINT_PATH', 'compose_checkpoints.json'),
        # packet 缓存同理：测试写进去的桩 camera_dna 等条目会被真实生成按指纹命中复用。
        ('CACHE_PATH', 'packet_cache.json'),
    ):
        monkeypatch.setattr(prompt_pipeline, attr, str(state_dir / filename), raising=True)


@pytest.fixture(autouse=True)
def _isolate_library_dir(tmp_path_factory, monkeypatch):
    """创意库目录也重定向到临时目录。

    与上面两条同一个理由，但这一条是被真事故教出来的（2026-08-09）：复刻流水线在
    run_compose 末尾开始把提示词包写进创意库，于是每个跑到那一步的测试都往开发者的
    真实创意库里塞一条桩记录——工作台上凭空多出几行 title='t' 的项目，而且没人会想到
    是测试干的。写路径一旦存在，隔离就必须是默认行为，不能靠每个测试自觉。"""
    import server_common
    monkeypatch.setattr(server_common, 'LIBRARY_DIR',
                        str(tmp_path_factory.mktemp('library')), raising=True)


@pytest.fixture(autouse=True)
def _isolate_used_topic_ledger(tmp_path_factory, monkeypatch):
    """把可写的历史选题台账（runtime/used-topic-ledger.md）也重定向到临时目录。

    同上一条 fixture 的理由，只是方向更危险：这份台账是**会被追加写**的去重记忆
    （每选中一个选题落一行）。不隔离的话，任何一个跑到 append_to_used_topic_ledger
    的测试都会往开发者的真实台账里塞桩数据，之后真实激发会把这些桩当成"已用选题"
    永久回避掉——一次静默的创意面缩窄，而且没人会想到去台账里翻。
    需要断言台账路径行为的测试（test_skill_profiles.py）自行 patch 覆盖本 fixture。"""
    import server_common
    path = tmp_path_factory.mktemp('ledger') / 'used-topic-ledger.md'
    # 先建成空文件：否则首次读取会从真实技能包里整份播种，把开发机上几百行真实
    # 选题喂进 run_ideate 的 system prompt——大量 ideate 测试（此前靠 patch
    # load_reference_file 拿到空台账）会因为这些真实内容而断言反转。
    path.write_text('', encoding='utf-8')
    monkeypatch.setattr(server_common, 'USED_TOPIC_LEDGER_FILE', str(path))


@pytest.fixture(autouse=True)
def _isolate_fx_runtime_state(tmp_path_factory, monkeypatch):
    """FX 运行时状态在所有测试里重定向到临时目录。

    2026-10-01 排查：在仓库里直接跑 pytest 会改写开发机的真实状态——
      - runtime/generation_counter.json：click_fx_send 递增代理轮换计数，真实换 IP 提前触发
      - runtime/selector_stats.json：假页面的命中/未命中混进控制台的选择器健康统计
      - runtime/fx_debug/、outputs/Errors/：假页面的现场快照（<html></html>）冒充真实故障
      - runtime/fx_audit.jsonl、fx_control_state.json：actor=test 的审计行与暂停状态
    需要断言具体路径行为的测试仍可自行 patch 覆盖本 fixture。"""
    import fx_control
    from integrations.google_fx.utils import forensics, proxy_rotator, ui_helpers
    root = tmp_path_factory.mktemp('fx_runtime')
    monkeypatch.setattr(proxy_rotator, 'AI_DIR', root)
    monkeypatch.setattr(forensics, 'DEBUG_ROOT', root / 'fx_debug')
    monkeypatch.setattr(ui_helpers, 'OUTPUT_DIR', str(root / 'outputs'))
    monkeypatch.setenv('ADSPWR_SELECTOR_STATS_FILE', str(root / 'selector_stats.json'))
    monkeypatch.setattr(fx_control.FX_CONTROL, 'state_path', str(root / 'fx_control_state.json'))
    monkeypatch.setattr(fx_control.FX_CONTROL, 'audit_path', str(root / 'fx_audit.jsonl'))


@pytest.fixture(autouse=True)
def _isolate_tasks_dir(tmp_path_factory, monkeypatch):
    """把 tasks 目录以及内存任务表在所有 pytest 测试中重定向到临时目录。

    与 _isolate_library_dir 同一个理由（2026-09-04 真实事故）：测试直接调用
    server.get_or_create_task 触发 save_task_to_disk，往真实的 tasks/ 目录下写下
    test_cover_* 与 test_vid_task_* 等桩任务，导致工作台加载到一堆「未命名项目」
    与「孤立作业」。写路径一旦存在，隔离就必须是默认行为，不能靠每个测试自觉。
    """
    import server_common
    tasks_dir = str(tmp_path_factory.mktemp('tasks'))
    monkeypatch.setattr(server_common, 'TASKS_DIR', tasks_dir, raising=True)
    if 'server' in sys.modules:
        import server
        monkeypatch.setattr(server, 'TASKS_DIR', tasks_dir, raising=False)
    orig_tasks = dict(server_common.ACTIVE_TASKS)
    server_common.ACTIVE_TASKS.clear()
    monkeypatch.setattr(server_common, 'TASKS_LOADED_FROM_DISK', False)
    monkeypatch.setattr(server_common, '_TASK_FLUSHED_EVENTS', {})
    yield
    server_common.ACTIVE_TASKS.clear()
    server_common.ACTIVE_TASKS.update(orig_tasks)
