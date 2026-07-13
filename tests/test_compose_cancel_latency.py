"""激发创意（compose）任务取消延迟修复的行为契约。

修复前的问题链：
1. /api/compose-cancel 只 set cancel_event，任务记录要等 worker 在下一个
   进度回调点自己感知才切到 cancelled——而 worker 可能正卡在一个 90-240 秒
   的 LLM 请求里，前端"正在取消"一挂几分钟；
2. _chat 流式回调里的裸 except 会把取消信号逐 chunk 吞掉，外层 except 还会
   把取消当网络错误降级成一次全新的 240 秒非流式重试；
3. 各合成重试循环把取消当生成错误再重试 3-4 轮。

修复后的契约：
- /api/compose-cancel 对纯 LLM 合成任务立即终态化（cancelled + error 事件 +
  广播 + 落盘），FX 任务仍走原有的协作式取消（要正确收尾共享浏览器）；
- worker 凭 run 令牌（本轮的 cancel_event 对象身份）发现记录已被终态化或被
  重试换代接管后静默退出，绝不重复写事件、绝不把过期结果写进新一轮运行；
- GenerationCancelled 在 _chat 流式路径与重试循环中一路放行。
"""
import json
import os
import threading
import urllib.request

import pytest

import server
import server_common
from server_common import GenerationCancelled
from prompt_pipeline import _raise_if_cancelled, _chat


@pytest.fixture(autouse=True)
def _isolated_tasks(tmp_path, monkeypatch):
    """tasks/ 落盘目录指到临时目录；ACTIVE_TASKS 就地清空——server.py 经
    wildcard import 与 server_common 共享同一个 dict 对象，重绑定只会让两个
    命名空间指向不同的表。"""
    monkeypatch.chdir(tmp_path)
    os.makedirs("tasks", exist_ok=True)
    server_common.ACTIVE_TASKS.clear()
    yield
    server_common.ACTIVE_TASKS.clear()


def _seed_running(tid, dimensions=None):
    t = {
        "id": tid,
        "status": "running",
        "events": [
            ("progress", {"stage": "outline", "details": "…"}),
            ("text_chunk", "streaming…"),
        ],
        "listeners": set(),
        "cancel_event": threading.Event(),
        "dimensions": dimensions or {"theme": "树屋"},
        "result": None,
        "error": None,
        "last_active": 1.0,
    }
    server_common.ACTIVE_TASKS[tid] = t
    return t


def _cancel_handler(task_id):
    """构造一个只够跑 /api/compose-cancel 分支的 SparkRequestHandler。"""
    h = object.__new__(server.SparkRequestHandler)
    h.path = "/api/compose-cancel"
    h._gate = lambda *a, **k: True
    h._read_json_body = lambda: {"task_id": task_id}
    sent = []
    h._send_json = lambda obj, status=200: sent.append((obj, status))
    return h, sent


class TestCancelEndpointImmediateFinalize:
    def test_pure_llm_task_is_finalized_immediately(self):
        t = _seed_running("1720000000000")
        received = []
        stop_evt = threading.Event()
        t["listeners"] = {(lambda et, d: received.append((et, d)), stop_evt)}

        h, sent = _cancel_handler("1720000000000")
        server.SparkRequestHandler.do_POST(h)

        assert sent == [({"status": "ok"}, 200)]
        # 接口返回时任务已是终态——不等 worker 自己感知
        assert t["cancel_event"].is_set()
        assert t["status"] == "cancelled"
        assert t["error"] == "用户取消了生成任务"
        # text_chunk 被裁剪，追加了与 worker 收尾同形的 error 事件
        assert ("error", {"message": "用户取消了生成任务"}) in t["events"]
        assert not any(e[0] == "text_chunk" for e in t["events"])
        # 挂着的流立即收到终止事件
        assert ("error", {"message": "用户取消了生成任务"}) in received
        assert stop_evt.is_set()
        # 落盘同步
        with open(os.path.join("tasks", "1720000000000.json"), encoding="utf-8") as f:
            assert json.load(f)["status"] == "cancelled"

    def test_fx_capable_task_is_not_force_finalized(self, monkeypatch):
        import builtins
        monkeypatch.setattr(builtins, "google_fx_cancelled", False, raising=False)
        t = _seed_running("frames_123", {"type": "frames"})

        h, sent = _cancel_handler("frames_123")
        server.SparkRequestHandler.do_POST(h)

        assert sent == [({"status": "ok"}, 200)]
        assert t["cancel_event"].is_set()
        # FX 任务要正确收尾共享浏览器，仍由 worker 协作式退出
        assert t["status"] == "running"

    def test_terminal_task_is_left_untouched(self):
        t = _seed_running("1720000000001")
        t["status"] = "completed"
        t["result"] = {"title": "已完成"}
        events_before = list(t["events"])

        h, sent = _cancel_handler("1720000000001")
        server.SparkRequestHandler.do_POST(h)

        assert sent == [({"status": "ok"}, 200)]
        assert t["status"] == "completed"
        assert t["events"] == events_before


class TestWorkerRunTokenGuards:
    def test_worker_exits_silently_when_endpoint_already_finalized(self, monkeypatch):
        """worker 卡在 LLM 请求里时接口已终态化：worker 醒来后不得重复写事件。"""
        _seed_running("900")

        def fake_call_llm(config, dimensions, on_progress=None):
            t = server_common.ACTIVE_TASKS["900"]
            t["cancel_event"].set()
            t["status"] = "cancelled"
            t["error"] = "用户取消了生成任务"
            t["events"] = [e for e in t["events"] if e[0] != "text_chunk"]
            t["events"].append(("error", {"message": "用户取消了生成任务"}))
            raise GenerationCancelled("Generation cancelled by user")

        monkeypatch.setattr(server, "call_llm", fake_call_llm)
        server.background_worker("900", {}, {"theme": "x"})

        t = server_common.ACTIVE_TASKS["900"]
        assert t["status"] == "cancelled"
        errors = [e for e in t["events"] if e[0] == "error"]
        assert errors == [("error", {"message": "用户取消了生成任务"})]

    def test_completion_cannot_flip_a_cancelled_task(self, monkeypatch):
        """取消发生在 LLM 返回前的最后一刻：completed 不得覆盖 cancelled。"""
        _seed_running("901")

        def fake_call_llm(config, dimensions, on_progress=None):
            server_common.ACTIVE_TASKS["901"]["cancel_event"].set()
            return "content"

        monkeypatch.setattr(server, "call_llm", fake_call_llm)
        monkeypatch.setattr(server, "parse_sections", lambda c: {"prompt_block": ""})
        server.background_worker("901", {}, {"theme": "x"})

        t = server_common.ACTIVE_TASKS["901"]
        assert t["status"] == "cancelled"
        assert t["result"] is None
        assert not any(e[0] == "result" for e in t["events"])

    def test_zombie_worker_abandons_record_after_retry_takeover(self, monkeypatch):
        """取消立即终态化后用户马上重试（换新 cancel_event）：还没死透的旧
        线程不得把过期结果/事件写进新一轮运行的记录。"""
        started = threading.Event()
        release = threading.Event()

        def fake_call_llm(config, dimensions, on_progress=None):
            started.set()
            release.wait(5)
            return "content"

        monkeypatch.setattr(server, "call_llm", fake_call_llm)
        monkeypatch.setattr(server, "parse_sections", lambda c: {"prompt_block": ""})

        _seed_running("902")
        th = threading.Thread(target=server.background_worker, args=("902", {}, {"theme": "旧"}))
        th.start()
        assert started.wait(5)

        t = server_common.ACTIVE_TASKS["902"]
        # 用户取消：接口立即终态化
        t["cancel_event"].set()
        t["status"] = "cancelled"
        t["error"] = "用户取消了生成任务"
        # 用户马上重试：原地重置 + 换新 cancel_event（prepare_task_for_run 契约）
        t2, already_running = server_common.prepare_task_for_run("902", {"theme": "新"})
        assert t2 is t and already_running is False

        release.set()
        th.join(5)
        assert not th.is_alive()
        # 新一轮记录未被僵尸线程污染
        assert t["status"] == "running"
        assert t["result"] is None
        assert t["error"] is None
        assert t["events"] == []


class TestCancelPropagation:
    def test_probe_contract(self):
        _raise_if_cancelled(None)  # 无回调 → no-op

        seen = []

        def not_cancelled(stage, details):
            seen.append(stage)
            return False

        _raise_if_cancelled(not_cancelled)
        assert seen == ["cancel_check"]

        with pytest.raises(GenerationCancelled):
            _raise_if_cancelled(lambda stage, details: True)

    def test_background_worker_on_progress_raises_on_cancel_check(self, monkeypatch):
        """background_worker 的 on_progress 收到探针时：未取消返回 False 且不
        产生事件；已取消直接 raise GenerationCancelled。"""
        _seed_running("903")
        captured = {}

        def fake_call_llm(config, dimensions, on_progress=None):
            captured["probe"] = on_progress("cancel_check", None)
            events_after_probe = list(server_common.ACTIVE_TASKS["903"]["events"])
            server_common.ACTIVE_TASKS["903"]["cancel_event"].set()
            with pytest.raises(GenerationCancelled):
                on_progress("cancel_check", None)
            captured["no_probe_event"] = events_after_probe
            raise GenerationCancelled("Generation cancelled by user")

        monkeypatch.setattr(server, "call_llm", fake_call_llm)
        server.background_worker("903", {}, {"theme": "x"})

        assert captured["probe"] is False
        assert not any(
            e[0] == "progress" and e[1].get("stage") == "cancel_check"
            for e in captured["no_probe_event"]
        )
        assert server_common.ACTIVE_TASKS["903"]["status"] == "cancelled"

    def test_chat_streaming_propagates_cancellation_without_fallback(self, monkeypatch):
        """流式 _chat 的 on_chunk 抛出取消时必须立刻上抛：不逐 chunk 吞掉、
        不降级成第二次全新的非流式请求。"""
        open_calls = []

        class FakeStream:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def __iter__(self):
                return iter([
                    b'data: {"choices":[{"delta":{"content":"hello"}}]}\n',
                    b'data: {"choices":[{"delta":{"content":"world"}}]}\n',
                    b"data: [DONE]\n",
                ])

        class FakeOpener:
            def open(self, req, timeout=None):
                open_calls.append(timeout)
                return FakeStream()

        monkeypatch.setattr(urllib.request, "build_opener", lambda *a, **k: FakeOpener())

        def cancelling_chunk(chunk):
            raise GenerationCancelled("Generation cancelled by user")

        with pytest.raises(GenerationCancelled):
            _chat({"model": "m", "apiUrl": "http://localhost:1/v1", "apiKey": "k"},
                  "sys", "user", on_chunk=cancelling_chunk)
        assert len(open_calls) == 1  # 没有触发非流式降级重试

    def test_chat_streaming_still_falls_back_on_real_stream_errors(self, monkeypatch):
        """真实的流中断（非取消）仍保留原有的非流式降级韧性。"""
        open_calls = []

        class BrokenStream:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def __iter__(self):
                raise ConnectionResetError("stream dropped")

        class FakeBody:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps(
                    {"choices": [{"message": {"content": "fallback"}}]}
                ).encode("utf-8")

        class FakeOpener:
            def open(self, req, timeout=None):
                open_calls.append(timeout)
                if len(open_calls) == 1:
                    return BrokenStream()
                return FakeBody()

        monkeypatch.setattr(urllib.request, "build_opener", lambda *a, **k: FakeOpener())

        out = _chat({"model": "m", "apiUrl": "http://localhost:1/v1", "apiKey": "k"},
                    "sys", "user", on_chunk=lambda c: None)
        assert out == "fallback"
        assert len(open_calls) == 2
