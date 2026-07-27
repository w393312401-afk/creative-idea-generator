"""取消信号必须真正送进内置 Google FX 运行时的行为契约（2026-07-26）。

修复前的问题链：
1. SPARK 这侧唯一的跨进程取消信号是 builtins.google_fx_cancelled，而 AdsPower 侧
   2026-07 的重构已经把进程级全局旗标彻底删掉（请求隔离缺陷），换成 per-request 的
   CancelState + contextvar（utils/cancel_flag.py）；
2. SPARK 在自己的 worker 线程里同步调用 FX 运行时，原来没有建立
   那个上下文——于是脚本里那一整排 _check_cancelled() 读到的永远是 False；
3. 用户点「取消」后 /api/compose-cancel 立刻把 AdsPower 浏览器关掉，脚本却还在按
   2 秒一轮扫 DOM 等图片 URL，满屏刷 "路径B DOM扫描: TargetClosedError" +
   "等待图片 URL... 剩余 Ns"，直到每张图各自的 MAX_WAIT_SECONDS 走完才收摊。

修复后的契约：
- server_common.fx_cancel_context 在调用线程建好 CancelState（脚本同线程同步跑，
  contextvar 可见）并起守卫线程把 SPARK 的取消谓词同步过去，脚本的轮询循环立刻停；
- 取消端点先向 FX 运行时发取消（cancel_flag.cancel_all）再关浏览器，顺序不能反；
- 「因取消而收摊」的返回被单独摘出来当取消处理，不会被批次级重试当成可重试失败而
  再开一次浏览器重跑整批。
"""
import contextvars
import os
import threading
import time

import pytest


# ── 外部 utils.cancel_flag 的忠实替身 ──────────────────────────────────────────
# 语义逐条对齐 AdsPower 侧 utils/cancel_flag.py：取消状态只存在于 per-request 的
# CancelState 里、经 contextvar 传递；无上下文时 is_cancelled 恒为 False。测试不依赖
# 外部仓库是否在本机存在。

class _CancelState:
    def __init__(self, request_id=None, deadline=None):
        self.val = False
        self.request_id = request_id
        self.deadline = deadline


class _FakeCancelFlag:
    def __init__(self):
        self._var = contextvars.ContextVar('fake_cancel_ctx', default=None)
        self._active = {}
        self.cancel_all_calls = 0

    @property
    def is_cancelled(self):
        state = self._var.get()
        return bool(state.val) if state is not None else False

    def init_context(self, request_id=None, deadline=None):
        state = _CancelState(request_id, deadline)
        self._var.set(state)
        return state

    def deadline_exceeded(self):
        return False

    def register(self, request_id, state):
        self._active[request_id] = state

    def unregister(self, request_id):
        self._active.pop(request_id, None)

    def cancel_all(self):
        self.cancel_all_calls += 1
        for state in list(self._active.values()):
            state.val = True
        return len(self._active)

    def cancel_request(self, request_id):
        state = self._active.get(request_id)
        if state is None:
            return False
        self.cancel_all_calls += 1
        state.val = True
        return True

    def active_count(self):
        return len(self._active)


@pytest.fixture
def fake_flag(monkeypatch):
    """把内置运行时的 cancel_flag 入口替换成忠实替身。"""
    import server_common

    flag = _FakeCancelFlag()
    monkeypatch.setattr(server_common, 'get_fx_cancel_flag', lambda: flag)
    return flag


class TestFxCancelContext:
    def test_script_polling_loop_stops_when_spark_cancels(self, fake_flag):
        """回归本体：模拟脚本"等图片 URL"的轮询循环，取消后必须立刻停，
        而不是空转到自己的超时。"""
        from server_common import fx_cancel_context

        cancelled = threading.Event()
        polls = []

        with fx_cancel_context(lambda: cancelled.is_set(), poll_interval=0.01):
            # 脚本侧的循环：每轮先 _check_cancelled()（读 contextvar），再干活
            deadline = time.time() + 5.0
            while time.time() < deadline:
                if fake_flag.is_cancelled:
                    break
                polls.append(1)
                if len(polls) == 3:
                    cancelled.set()   # 用户点了取消
                time.sleep(0.02)
            else:
                pytest.fail('轮询循环空转到了超时——取消信号没进到脚本上下文')

        # 取消后最多再多跑几轮（守卫线程的 poll_interval 粒度），不是几百轮
        assert 3 <= len(polls) <= 12

    def test_already_cancelled_at_entry_is_visible_immediately(self, fake_flag):
        """进门就已取消：脚本第一次 _check_cancelled() 就该看到，不用等一个
        poll_interval——否则白开一次浏览器。"""
        from server_common import fx_cancel_context

        with fx_cancel_context(lambda: True, poll_interval=30.0):
            assert fake_flag.is_cancelled is True

    def test_no_context_means_flag_is_always_false(self, fake_flag):
        """修复前的状态留档：没有 CancelState 上下文时，脚本读到的永远是 False
        （所以那一整排 _check_cancelled() 全是空转）。"""
        assert fake_flag.is_cancelled is False

    def test_state_is_registered_for_endpoint_and_released_after(self, fake_flag):
        from server_common import fx_cancel_context

        with fx_cancel_context(lambda: False, poll_interval=0.01):
            # 无上下文入口（/api/compose-cancel）要能按注册表精确/全量取消
            assert fake_flag.active_count() == 1
        assert fake_flag.active_count() == 0

    def test_endpoint_cancel_all_is_visible_to_the_running_script(self, fake_flag):
        """取消端点走的是注册表，不是 contextvar——它在别的线程里，必须也能命中。"""
        from server_common import fx_cancel_context

        with fx_cancel_context(lambda: False, poll_interval=5.0):
            t = threading.Thread(target=fake_flag.cancel_all)
            t.start()
            t.join()
            assert fake_flag.is_cancelled is True

    def test_predicate_raising_is_treated_as_cancelled(self, fake_flag):
        """谓词自己炸了（SSE 连接已断等）按取消处理：宁可早停也不要空转到超时。"""
        from server_common import fx_cancel_context

        def _boom():
            raise ConnectionError('client gone')

        with fx_cancel_context(_boom, poll_interval=0.01):
            deadline = time.time() + 2.0
            while time.time() < deadline and not fake_flag.is_cancelled:
                time.sleep(0.01)
            assert fake_flag.is_cancelled is True

    def test_missing_runtime_dependency_degrades_to_noop(self, monkeypatch):
        """纯 API 后端缺少 FX 依赖时安静降级，不许把调用方带崩。"""
        import server_common

        def _missing():
            raise ImportError('playwright')

        monkeypatch.setattr(server_common, 'get_fx_cancel_flag', _missing)
        with server_common.fx_cancel_context(lambda: False) as state:
            assert state is None

    def test_none_predicate_is_noop(self, fake_flag):
        """没有取消谓词的调用路径（脚本/单测直调）不建上下文，也不起守卫线程。"""
        from server_common import fx_cancel_context

        before = threading.active_count()
        with fx_cancel_context(None) as state:
            assert state is None
            assert threading.active_count() == before
        assert fake_flag.active_count() == 0


# ── 批次级：取消不是可重试的失败 ───────────────────────────────────────────────

class _FakeModels:
    class ImageBatchRequest:
        def __init__(self, **kw):
            self.__dict__.update(kw)


class _FakeGoogleFx:
    """脚本替身：被调用时按当前取消状态返回，与真脚本同形（取消 → 非 success +
    message='任务已取消'，见 google_fx_image 的 except 分支）。"""

    def __init__(self, flag, cancelled_from_call=1):
        self.flag = flag
        self.calls = 0
        self.cancelled_from_call = cancelled_from_call

    def _generate_images_batch_google_fx(self, req):
        self.calls += 1
        if self.calls >= self.cancelled_from_call:
            return {'status': 'failed', 'image_urls': [], 'message': '任务已取消'}
        return {'status': 'failed', 'image_urls': [], 'message': '未找到底部配置按钮'}


class TestBatchCancelIsNotRetried:
    def test_cancelled_result_raises_cancellation_not_retryable_failure(self, fake_flag):
        import frame_generator as fg

        fx = _FakeGoogleFx(fake_flag)
        with pytest.raises(fg.ImageTaskCancelled):
            fg._fx_generate_batch(fx, _FakeModels(), {}, ['p1', 'p2'], None,
                                  cancel_fn=lambda: True)
        assert fx.calls == 1

    def test_cancellation_is_a_connection_error_so_callers_reraise(self):
        """帧序列的批次重试是 `except ConnectionError: raise` / `except Exception: 重试一次`。
        取消必须落在前一支上，否则会再开一次浏览器把整批重跑。"""
        import frame_generator as fg

        assert issubclass(fg.ImageTaskCancelled, ConnectionError)

    def test_plain_failure_still_reports_as_failure(self, fake_flag):
        import frame_generator as fg

        fx = _FakeGoogleFx(fake_flag, cancelled_from_call=99)
        with pytest.raises(RuntimeError) as e:
            fg._fx_generate_batch(fx, _FakeModels(), {}, ['p1'], None, cancel_fn=lambda: False)
        assert not isinstance(e.value, ConnectionError)
        assert '未找到底部配置按钮' in str(e.value)

    def test_temp_dir_is_cleaned_up_on_cancel(self, fake_flag):
        import frame_generator as fg

        seen = {}
        orig = fg.tempfile.mkdtemp

        def _spy(*a, **kw):
            seen['path'] = orig(*a, **kw)
            return seen['path']

        fg.tempfile.mkdtemp = _spy
        try:
            with pytest.raises(fg.ImageTaskCancelled):
                fg._fx_generate_batch(_FakeGoogleFx(fake_flag), _FakeModels(), {}, ['p1'],
                                      None, cancel_fn=lambda: True)
        finally:
            fg.tempfile.mkdtemp = orig
        assert not os.path.exists(seen['path'])


class TestCancelPredicateSource:
    def test_prefers_thread_local_cancel_sink(self):
        """worker 注册的 sink（cancel_event.is_set）是纯谓词，可安全跨线程轮询；
        它优先于会写事件/广播的 on_progress。"""
        import frame_generator as fg

        evt = threading.Event()
        fg.set_cancel_check_sink(evt.is_set)
        try:
            fn = fg._fx_batch_cancel_fn(on_progress=lambda *a: pytest.fail('不该走 on_progress'))
            assert fn() is False
            evt.set()
            assert fn() is True
        finally:
            fg.set_cancel_check_sink(None)

    def test_falls_back_to_on_progress_cancel_check(self):
        import frame_generator as fg

        fg.set_cancel_check_sink(None)
        stages = []

        def on_progress(stage, details):
            stages.append(stage)
            return True

        fn = fg._fx_batch_cancel_fn(on_progress)
        assert fn() is True
        assert stages == ['cancel_check']

    def test_no_predicate_available(self):
        import frame_generator as fg

        fg.set_cancel_check_sink(None)
        assert fg._fx_batch_cancel_fn(None) is None


# ── 取消端点：先通知脚本，再关浏览器 ──────────────────────────────────────────

class TestCancelEndpointDoesNotKillTheBrowser:
    """取消 = 只发信号。关浏览器会清空 Flow 画布、打松登录 token，还会让正在等图的
    脚本每一轮 DOM 扫描都抛 TargetClosedError 却照旧空转到 MAX_WAIT_SECONDS。"""

    def _cancel(self, monkeypatch, tmp_path):
        import server
        import server_common

        monkeypatch.chdir(tmp_path)
        os.makedirs('tasks', exist_ok=True)
        server_common.ACTIVE_TASKS.clear()

        order = []
        flag = _FakeCancelFlag()
        real_cancel_all = flag.cancel_all

        def _cancel_all():
            order.append('cancel_script')
            return real_cancel_all()

        flag.cancel_all = _cancel_all
        flag.cancel_request = lambda request_id: (order.append('cancel_script') or True)
        monkeypatch.setattr(server, 'get_fx_cancel_flag', lambda: flag)

        server_common.ACTIVE_TASKS['frames_abc'] = {
            'id': 'frames_abc',
            'status': 'running',
            'events': [],
            'listeners': set(),
            'cancel_event': threading.Event(),
            'dimensions': {'type': 'frames'},
            'result': None,
            'error': None,
            'last_active': 1.0,
        }
        try:
            h = object.__new__(server.SparkRequestHandler)
            h.path = '/api/compose-cancel'
            h._gate = lambda *a, **k: True
            h._read_json_body = lambda: {'task_id': 'frames_abc'}
            sent = []
            h._send_json = lambda obj, status=200: sent.append((obj, status))
            server.SparkRequestHandler.do_POST(h)
        finally:
            server_common.ACTIVE_TASKS.clear()
        return sent, order

    def test_cancel_signals_the_script_and_leaves_the_browser_alone(self, monkeypatch, tmp_path):
        sent, order = self._cancel(monkeypatch, tmp_path)
        assert sent == [({'status': 'ok'}, 200)]
        assert order == ['cancel_script']
        assert 'stop_browser' not in order

    def test_no_worker_path_kills_the_browser_either(self):
        """帧/视频 worker 的取消收尾原来也各自 stop_ads_browser 了一次（注释里提它没关系）。"""
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'server.py')
        with open(path, encoding='utf-8') as f:
            code_lines = [ln for ln in f if not ln.lstrip().startswith('#')]
        assert not [ln for ln in code_lines if 'stop_ads_browser' in ln]
