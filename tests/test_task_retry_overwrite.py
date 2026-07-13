"""prepare_task_for_run 的重试覆盖契约。

重试沿用旧 task_id 时，终态任务记录（failed/cancelled/completed）必须被
原地重置复用——重试覆盖被重试的那条任务记录，而不是留下失败记录再另开
一条新记录；运行中的记录则拒绝二次启动（already_running=True，调用方不得
再起第二个 worker）。
"""
import json
import os
import threading

import pytest

import server_common
from server_common import prepare_task_for_run


@pytest.fixture(autouse=True)
def _isolated_tasks(tmp_path, monkeypatch):
    """tasks/ 落盘目录指到临时目录，并隔离全局 ACTIVE_TASKS。"""
    monkeypatch.chdir(tmp_path)
    os.makedirs("tasks", exist_ok=True)
    monkeypatch.setattr(server_common, "ACTIVE_TASKS", {})


def _seed(tid, status="failed"):
    t = {
        "id": tid,
        "status": status,
        "events": [("progress", {"stage": "compose"}), ("error", {"message": "boom"})],
        "listeners": set(),
        "cancel_event": threading.Event(),
        "dimensions": {"theme": "旧主题"},
        "result": {"title": "旧结果"},
        "error": "boom",
        "last_active": 1.0,
    }
    t["cancel_event"].set()
    server_common.ACTIVE_TASKS[tid] = t
    return t


def test_creates_new_task_when_absent():
    t, already_running = prepare_task_for_run("100", {"theme": "新主题"})
    assert already_running is False
    assert t["status"] == "running"
    assert t["dimensions"] == {"theme": "新主题"}
    assert list(server_common.ACTIVE_TASKS) == ["100"]
    assert os.path.exists(os.path.join("tasks", "100.json"))


@pytest.mark.parametrize("status", ["failed", "cancelled", "completed"])
def test_terminal_record_is_overwritten_in_place(status):
    old = _seed("200", status)
    old_cancel = old["cancel_event"]
    sentinel_listener = object()
    old["listeners"].add(sentinel_listener)

    t, already_running = prepare_task_for_run("200", {"theme": "新主题"})

    assert already_running is False
    # 同一条记录被复用：id 不变、表里仍只有一条
    assert t is old
    assert list(server_common.ACTIVE_TASKS) == ["200"]
    # 上一轮的痕迹被清空
    assert t["status"] == "running"
    assert t["events"] == []
    assert t["error"] is None
    assert t["result"] is None
    assert t["dimensions"] == {"theme": "新主题"}
    # cancel_event 必须换新：旧事件已被 set，复用会让新 worker 一启动就自杀
    assert t["cancel_event"] is not old_cancel
    assert not t["cancel_event"].is_set()
    # 仍挂着的旁观流保留，直接收新一轮事件
    assert sentinel_listener in t["listeners"]


def test_terminal_reset_is_persisted_to_disk():
    _seed("300", "failed")
    prepare_task_for_run("300", {"theme": "新主题"})
    with open(os.path.join("tasks", "300.json"), encoding="utf-8") as f:
        on_disk = json.load(f)
    assert on_disk["status"] == "running"
    assert on_disk["events"] == []
    assert on_disk["error"] is None
    assert on_disk["dimensions"] == {"theme": "新主题"}


def test_running_task_is_not_reset():
    old = _seed("400", "running")
    old_events = list(old["events"])
    old_cancel = old["cancel_event"]

    t, already_running = prepare_task_for_run("400", {"theme": "新主题"})

    assert already_running is True
    assert t is old
    # 运行中的记录原样保留：事件、cancel_event、维度都不动
    assert t["events"] == old_events
    assert t["cancel_event"] is old_cancel
    assert t["dimensions"] == {"theme": "旧主题"}


def test_dimensions_kept_when_not_provided():
    _seed("500", "failed")
    t, _ = prepare_task_for_run("500")
    assert t["dimensions"] == {"theme": "旧主题"}
