# -*- coding: utf-8 -*-
"""Flow 改版的"早发现 + 早收手"防线。

2026-09-05 那次改版暴露的不是一个 bug，而是**整条反馈链是哑的**：
Flow 换了域名和前端，代码认不出画布卡片，于是每个任务都报同一句
"Generate 后未检测到新 tile"；系统照常重试，把 6 张参考图重新上传一遍、
又点了一轮 Generate 把积分烧掉，失败原因一个字没变。全过程没有任何一处
说出"这看起来像是 Flow 改版了"。

这个文件钉死补上的四道防线：
  ① 同因全灭熔断——一轮里所有任务以同一个自动化层原因失败，就停手并告警；
  ② 积分-产出对撞——余额在掉却拿不到成品，直接判定是识别层瞎了；
  ④ 失败现场能离线看懂 DOM（page.html 不再被内联 CSS 吃光 + 元素骨架）；
  ⑤ 选择器基线巡检——命中层级往后退就报，不必等真任务失败。
（③ 把画布卡片纳入 UI_SELECTORS/探针，钉在 test_flow_new_ui_migration.py。）
"""

import json
import os
import unittest
from unittest.mock import patch

from integrations.google_fx.services import google_fx_diagnostics as D
from integrations.google_fx.services import google_fx_helpers as H
from integrations.google_fx.services import google_fx_video as V
from integrations.google_fx.utils import forensics


# ── ① 同因全灭熔断 ────────────────────────────────────────────────────────

class _Req:
    prompt = "p"
    model = "Veo 3.1"
    ratio = "9:16"
    image = ""
    end_image = ""


def _runner(n=3):
    return V._ChunkRunner(total_reqs=n, chunk_start=0, chunk=[_Req() for _ in range(n)],
                          all_slices={}, on_progress=None, cancel_check=None)


class TestFailureMessageNormalisation(unittest.TestCase):
    def test_digits_uuids_and_frame_names_are_collapsed(self):
        a = V._normalize_failure_message("CANVAS_MOUNT_FAILED:画布卡片挂载失败 (0/2)")
        b = V._normalize_failure_message("CANVAS_MOUNT_FAILED:画布卡片挂载失败 (1/2)")
        assert a == b

    def test_genuinely_different_causes_stay_different(self):
        a = V._normalize_failure_message("生成失败")
        b = V._normalize_failure_message("下载失败: timeout")
        assert a != b


class TestUiBreakageBreaker(unittest.TestCase):
    def _attempt(self, runner, messages):
        """让 runner 看起来刚跑完一轮：每个槽位都有失败原因，且都没完成。"""
        for sub_idx, message in enumerate(messages):
            runner.results[sub_idx] = {"status": "failed", "video_url": None,
                                       "message": message}
        return [(i, runner.chunk[i]) for i in range(len(messages))]

    def test_trips_when_every_task_dies_of_the_same_ui_cause(self):
        runner = _runner()
        attempted = self._attempt(runner, ["Generate 后未检测到新 tile"] * 3)
        assert runner._check_ui_breakage(attempted)
        assert "共因" in runner.ui_breakage
        assert "已停止重试" in runner.ui_breakage

    def test_trips_on_the_canvas_blind_signal(self):
        runner = _runner(2)
        msg = (H.CANVAS_BLIND_PREFIX + ":提交成功但读不到画布结果，疑似 Flow 改版导致"
               "卡片选择器失效——账号余额在此期间从 891 掉到 884")
        attempted = self._attempt(runner, [msg, msg])
        assert runner._check_ui_breakage(attempted)

    def test_mount_failures_differing_only_in_counters_still_trip(self):
        runner = _runner(2)
        attempted = self._attempt(runner, [
            "CANVAS_MOUNT_FAILED:画布卡片挂载失败 (0/2)",
            "CANVAS_MOUNT_FAILED:画布卡片挂载失败 (1/2)",
        ])
        assert runner._check_ui_breakage(attempted)

    def test_does_not_trip_on_mixed_causes(self):
        """原因五花八门 = 内容/网络问题，重试是有意义的，不能熔断。"""
        runner = _runner(2)
        attempted = self._attempt(runner, ["Generate 后未检测到新 tile", "下载失败: timeout"])
        assert runner._check_ui_breakage(attempted) is None
        assert runner.ui_breakage is None

    def test_does_not_trip_when_something_succeeded(self):
        """有一个成了就说明链路是通的，剩下的该照常重试。"""
        runner = _runner(2)
        attempted = self._attempt(runner, ["Generate 后未检测到新 tile"] * 2)
        runner.completed.add(1)
        assert runner._check_ui_breakage(attempted) is None

    def test_does_not_trip_on_a_single_task(self):
        """单个任务失败可能只是抽风，样本太小不足以判"整条链坏了"。"""
        runner = _runner(1)
        attempted = self._attempt(runner, ["Generate 后未检测到新 tile"])
        assert runner._check_ui_breakage(attempted) is None

    def test_does_not_trip_on_flow_side_generation_failures(self):
        """Flow 自己生成失败（内容/审核）不是自动化层问题，必须照常重试。"""
        runner = _runner(3)
        attempted = self._attempt(runner, ["超时未检测到视频文件或生成失败"] * 3)
        assert runner._check_ui_breakage(attempted) is None

    def test_notifies_the_caller_so_the_ui_can_surface_it(self):
        seen = []
        runner = _runner(2)
        runner.on_progress = lambda idx, stage, payload: seen.append((stage, payload))
        attempted = self._attempt(runner, ["Generate 后未检测到新 tile"] * 2)
        runner._check_ui_breakage(attempted)
        assert any(stage == "ui_breakage" for stage, _ in seen), seen


class _StubRunner:
    """第一批就判定自动化层失效的 runner 替身。"""
    instances = []

    def __init__(self, total_reqs, chunk_start, chunk, all_slices,
                 on_progress, cancel_check):
        self.chunk = chunk
        self.chunk_start = chunk_start
        self.project_url = None
        self.bound_project_url = None
        self.submitted_count = 0
        self.ui_breakage = None
        _StubRunner.instances.append(self)

    def run(self):
        self.ui_breakage = "疑似 Flow 改版 / 自动化层失效：本轮 5 个任务全部失败"
        return [{"status": "failed", "video_url": None, "message": self.ui_breakage}
                for _ in self.chunk]


class TestBatchStopsAfterBreakage(unittest.TestCase):
    def test_remaining_chunks_are_not_even_attempted(self):
        """熔断之后还去跑后面几批 = 再白传几轮参考图、再白烧几轮积分。"""
        _StubRunner.instances = []
        reqs = [_Req() for _ in range(V.VIDEO_CHUNK_SIZE * 3)]
        with patch.object(V, "_ChunkRunner", _StubRunner), \
             patch.object(V, "_normalize_model_name", lambda m, is_video=False: m):
            results = V.generate_videos_batch_google_fx(reqs)

        assert len(_StubRunner.instances) == 1, "熔断后不该再构造后续分批的 runner"
        assert len(results) == len(reqs), "被跳过的任务也必须有结果，不能凭空少掉"
        assert all(r["status"] == "failed" for r in results)
        assert all("Flow 改版" in r["message"] for r in results)


# ── ② 积分-产出对撞 ───────────────────────────────────────────────────────

class _ScanPage:
    def __init__(self, layer, media_count):
        self._scan = {"layer": layer, "mediaCount": media_count}

    def evaluate(self, js, *a):
        return dict(self._scan)


class TestCreditOutputContradiction(unittest.TestCase):
    def test_dropping_balance_with_no_output_is_named_for_what_it_is(self):
        """余额在掉 = Flow 真的收下了请求；同时一段都拿不到 = 我们这边瞎了。"""
        with patch.object(H, "last_credit_reading", lambda: (884, 0.0)):
            msg = H._no_tile_error_message(_ScanPage(layer=0, media_count=3),
                                           credit_before=891)
        assert msg.startswith(H.CANVAS_BLIND_PREFIX)
        assert "891" in msg and "884" in msg

    def test_no_tile_selector_matching_is_also_enough_on_its_own(self):
        with patch.object(H, "last_credit_reading", lambda: (900, 0.0)):
            msg = H._no_tile_error_message(_ScanPage(layer=-1, media_count=7),
                                           credit_before=900)
        assert msg.startswith(H.CANVAS_BLIND_PREFIX)
        assert "没有任何一层卡片" in msg

    def test_stable_balance_and_healthy_canvas_keeps_the_old_vague_message(self):
        """没有证据就别乱扣帽子——那可能真的只是 Flow 这一次抽风。"""
        with patch.object(H, "last_credit_reading", lambda: (900, 0.0)):
            msg = H._no_tile_error_message(_ScanPage(layer=0, media_count=3),
                                           credit_before=900)
        assert msg == "Generate 后未检测到新 tile"

    def test_unknown_previous_balance_is_not_treated_as_a_drop(self):
        with patch.object(H, "last_credit_reading", lambda: (884, 0.0)):
            msg = H._no_tile_error_message(_ScanPage(layer=0, media_count=3),
                                           credit_before=None)
        assert msg == "Generate 后未检测到新 tile"

    def test_credit_readings_are_recorded_for_later_comparison(self):
        from integrations.google_fx.services import google_fx_credit as C
        C.note_credit_reading(777)
        assert C.last_credit_reading()[0] == 777
        C.note_credit_reading(None)          # 读不到就不该覆盖掉上一次的真读数
        assert C.last_credit_reading()[0] == 777


# ── ④ 失败现场要能离线看懂 DOM ────────────────────────────────────────────

class _CapturePage:
    """能跑 JS 的假页面：strip 返回去样式的 HTML，skeleton 返回骨架文本。"""

    def __init__(self, strip_result="<html><body><flow-grid-tile-container/></body></html>",
                 skeleton_result="body\n  flow-grid-tile-container", raise_on_strip=False):
        self.url = "https://flow.google.com/project/x"
        self._strip = strip_result
        self._skeleton = skeleton_result
        self._raise_on_strip = raise_on_strip
        self.keyboard = self

    def evaluate(self, js, arg=None):
        if "cloneNode" in js:
            if self._raise_on_strip:
                raise RuntimeError("evaluate failed")
            return self._strip
        if "maxNodes" in js or "rows.join" in js:
            return self._skeleton
        return None

    def is_closed(self):
        return False

    def content(self):
        return "<html><head><style>" + "x" * 500 + "</style></head><body>raw</body></html>"

    def inner_text(self, selector):
        return "text"

    def screenshot(self, timeout=None, full_page=False):
        return b"\x89PNG\r\n\x1a\nfake"

    def bring_to_front(self):
        pass

    def locator(self, selector):
        raise RuntimeError("not needed")


class TestForensicsCapturesUsableDom(unittest.TestCase):
    def _capture(self, tmp, page, tag="ui_test"):
        with patch.object(forensics, "DEBUG_ROOT", tmp / "fx_debug"):
            slot = forensics.capture(page, tag, "人为触发", bucket="b")
        assert slot is not None
        return slot

    def setUp(self):
        import tempfile
        from pathlib import Path
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_page_html_has_the_styles_stripped_out(self):
        """原来只存 page.content()[:80000]，Flow 的内联 CSS 把预算吃光，
        抓下来的现场里一个 body 标签都没有——改版当天等于没留证据。"""
        slot = self._capture(self.tmp, _CapturePage())
        html = open(os.path.join(slot, "page.html"), encoding="utf-8").read()
        assert "flow-grid-tile-container" in html
        assert "<style>" not in html

    def test_dom_skeleton_is_written(self):
        slot = self._capture(self.tmp, _CapturePage())
        skeleton = open(os.path.join(slot, "dom_skeleton.txt"), encoding="utf-8").read()
        assert "flow-grid-tile-container" in skeleton

    def test_falls_back_to_raw_content_when_the_strip_script_fails(self):
        """存得更好是加分项，把现场弄丢是事故——JS 挂了也必须留下 page.html。"""
        slot = self._capture(self.tmp, _CapturePage(raise_on_strip=True))
        html = open(os.path.join(slot, "page.html"), encoding="utf-8").read()
        assert "raw" in html
        meta = json.load(open(os.path.join(slot, "meta.json"), encoding="utf-8"))
        assert "html_strip_error" in meta


# ── ⑤ 选择器基线巡检 ──────────────────────────────────────────────────────

def _families(**states):
    return {"families": [
        {"group": "google_fx", "family": name, "state": state,
         "hit_index": index, "hit_selector": f"sel-{name}-{index}"}
        for name, (state, index) in states.items()
    ]}


class TestSelectorBaselineDrift(unittest.TestCase):
    def test_primary_falling_to_fallback_is_reported(self):
        old = _families(add_media_btn=("primary", 0))
        new = _families(add_media_btn=("fallback", 1))
        changes = D.diff_selector_baseline(new, old)
        assert len(changes) == 1
        assert "primary" in changes[0]["message"] and "fallback" in changes[0]["message"]

    def test_slipping_to_a_later_fallback_layer_is_reported(self):
        """还是 fallback，但接住它的层更靠后了——下一次改版就该整族失守了。"""
        old = _families(add_media_btn=("fallback", 1))
        new = _families(add_media_btn=("fallback", 4))
        assert len(D.diff_selector_baseline(new, old)) == 1

    def test_improvements_are_not_reported(self):
        """修好了不需要人管，混进来只会稀释信号。"""
        old = _families(add_media_btn=("fallback", 3))
        new = _families(add_media_btn=("primary", 0))
        assert D.diff_selector_baseline(new, old) == []

    def test_no_baseline_means_no_noise(self):
        assert D.diff_selector_baseline(_families(x=("primary", 0)), None) == []

    def test_baseline_round_trips_through_disk(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "baseline.json")
            with patch.dict(os.environ, {"GOOGLE_FX_SELECTOR_BASELINE_FILE": path}):
                probe = _families(prompt_input=("primary", 0))
                assert D.save_selector_baseline(probe)
                assert D.load_selector_baseline() == probe

    def test_required_family_on_fallback_raises_an_alert(self):
        alerts = D._collect_alerts(_families(add_media_btn=("fallback", 1))["families"])
        assert alerts and alerts[0]["level"] == "warning"

    def test_required_family_missing_is_critical(self):
        rows = _families(prompt_input=("missing", -1))["families"]
        rows[0]["total_layers"] = 2
        alerts = D._collect_alerts(rows)
        assert alerts and alerts[0]["level"] == "critical"

    def test_non_required_families_do_not_raise_alerts(self):
        """条件性元素（弹窗里的控件等）在静止页上本来就不在，报了就是噪音。"""
        assert D._collect_alerts(_families(config_panel_root=("missing", -1))["families"]) == []


if __name__ == "__main__":
    unittest.main()
