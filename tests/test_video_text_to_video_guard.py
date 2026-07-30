# -*- coding: utf-8 -*-
"""回归护栏：视频链绝不允许退化成「没有首尾帧的文生视频」。

背景（2026-07-30 盐湖水罐车单复盘，server.log 14:23–14:32）：一批 12 段视频里
只有 1 段过了锚点校验，其余是

    🧭 任务 1 帧映射核对: 首帧=img_001.webp→13408d9d | 尾帧=img_002.webp→13408d9d
    ✅ 参考卡片挂载完成 (2/2)
    ❌ 下载的视频内容与槽位 1 的首尾锚点帧不符 (MAD first=57.1, last=62.7)

两张不同的帧图拿到了同一个画布 UUID，挂载却"成功"了 2/2。链路上有三处会让
一个本该 i2v 的片段安静地变成 t2v（Flow 收到没有参考图的请求不报错，照样按纯
文本生成一段无关片段，白烧额度、等到下载后锚点比对才被拒收）：

1. _upload_image_to_canvas 猜 UUID —— "去重候选"无条件采信、DOM 新增多张时
   next(iter(set)) 掷骰子；
2. _upload_to_slot_directly 只要 set_input_files 不抛异常就报成功，从不验证
   图片真的落进 Start/End 槽位；
3. 挂载成功到点 Generate 之间没有任何复核，chip 中途掉了也照提交。

本文件把这三处的新契约钉死。
"""

import os

import pytest

from integrations.google_fx.services import google_fx_helpers as H
from integrations.google_fx.services import google_fx_video as V


UUID_A = "13408d9d-5fbf-4531-8823-8baf9cccde76"
UUID_B = "363878e9-216a-4c49-9411-10f09f95cc66"
UUID_C = "a3bce4ab-14e1-4197-93bd-e80e7eb9aaed"


# ── 1. 上传归属：宁可不给 UUID，也不给错的 UUID ──────────────────────────

class _FakeUploadPage:
    """只实现 _upload_image_to_canvas 用到的那几个 Page 方法。

    panel_states: 每次 _get_panel_uuid_order 调用依次返回的画布 UUID 列表
                  （第一次调用是 known_uuids 基线，最后一个状态之后保持不变）。
    captured:     网络监听会"捕获"到的 URL（上传开始后立即可见）。
    """

    def __init__(self, panel_states, captured=()):
        self.panel_states = list(panel_states)
        self.captured = list(captured)
        self.escaped = False

    # -- _find_add2_btn / file input 走 locator --
    def locator(self, selector):
        return _FakeLocator(self)

    def evaluate(self, script, *args):
        srcs = []
        state = self.panel_states[0] if len(self.panel_states) == 1 else self.panel_states.pop(0)
        for uid in state:
            srcs.append(f"/fx/api/trpc/media.getMediaUrlRedirect?name={uid}")
        return srcs

    def on(self, event, handler):
        self._handler = handler

    def remove_listener(self, event, handler):
        pass


class _FakeLocator:
    def __init__(self, page):
        self.page = page

    @property
    def first(self):
        return self

    def count(self):
        return 1

    def is_visible(self, timeout=None):
        return True

    def click(self, **kwargs):
        return None

    def set_input_files(self, paths):
        # 上传一发起，监听器就"收到"了这些响应
        for url in self.page.captured:
            self.page._handler(_FakeResponse(url))


class _FakeResponse:
    def __init__(self, url):
        self.url = url
        self.status = 200
        self.request = self

    @property
    def resource_type(self):
        return "xhr"


@pytest.fixture
def _patch_upload_deps(monkeypatch, tmp_path):
    monkeypatch.setattr(V, "_find_add2_btn", lambda page: _FakeLocator(page))
    monkeypatch.setattr(V, "_safe_press_escape", lambda *a, **k: None)
    monkeypatch.setattr(V, "random_sleep", lambda *a, **k: None)
    monkeypatch.setattr(V.time, "sleep", lambda *_a: None)
    monkeypatch.setattr(V, "_make_response_handler",
                        lambda captured, mode=None: (lambda resp: captured.append((0, resp.url))))
    img = tmp_path / "img_002.webp"
    img.write_bytes(b"x")
    return str(img)


def test_upload_never_reuses_a_uuid_already_owned_by_another_frame(_patch_upload_deps, monkeypatch):
    """复盘现场：img_002 上传期间只在网络里看到 img_001 已有的 UUID。

    那是画布缩略图被重新拉了一次，不是本次上传的结果。旧实现把它当"去重上传"
    直接采信，于是 img_001 / img_002 指向同一张图。新实现必须拒绝，返回 None。
    """
    page = _FakeUploadPage(panel_states=[[UUID_A]],
                           captured=[f"/fx/api/trpc/media.getMediaUrlRedirect?name={UUID_A}"])
    # timeout 调小，避免测试真的等满宽限期
    got = V._upload_image_to_canvas(page, _patch_upload_deps, timeout=2,
                                    extra_known_uuids={UUID_A})
    assert got is None


def test_upload_accepts_dedup_uuid_only_after_grace_when_unowned(_patch_upload_deps, monkeypatch):
    """同一张图重传（Flow 按内容去重返回已有 media id）仍要能用——
    前提是这个 UUID 没被本轮别的文件占用，且等够了宽限期。"""
    monkeypatch.setattr(V, "_DEDUP_ATTRIBUTION_GRACE_SECONDS", 0)
    page = _FakeUploadPage(panel_states=[[UUID_A]],
                           captured=[f"/fx/api/trpc/media.getMediaUrlRedirect?name={UUID_A}"])
    got = V._upload_image_to_canvas(page, _patch_upload_deps, timeout=5, extra_known_uuids=None)
    assert got == UUID_A


def test_upload_refuses_to_guess_when_multiple_new_cards_appear(_patch_upload_deps):
    """上一张图的卡片渲染迟到，本次上传期间画布同时冒出两张新图：
    旧实现 next(iter(set)) 随机挑一张（错了就整条映射错位），新实现拒绝猜。"""
    page = _FakeUploadPage(panel_states=[[UUID_A], [UUID_A, UUID_B, UUID_C]], captured=[])
    got = V._upload_image_to_canvas(page, _patch_upload_deps, timeout=2,
                                    extra_known_uuids={UUID_A})
    assert got is None


def test_upload_takes_the_single_new_card(_patch_upload_deps):
    page = _FakeUploadPage(panel_states=[[UUID_A], [UUID_A, UUID_B]], captured=[])
    got = V._upload_image_to_canvas(page, _patch_upload_deps, timeout=5,
                                    extra_known_uuids={UUID_A})
    assert got == UUID_B


def test_panel_uuid_order_is_document_order_not_hash_order():
    class _P:
        def evaluate(self, *_a):
            return [
                f"/x?name={UUID_C}",
                f"/x?name={UUID_A}",
                f"/x?name={UUID_C}",  # 重复只保留首次位置
                f"/x?name={UUID_B}",
            ]

    assert H._get_panel_uuid_order(_P()) == [UUID_C, UUID_A, UUID_B]


# ── 2. 映射撞车：两张图指向同一 UUID 时整体作废 ──────────────────────────

def _runner():
    return V._ChunkRunner(total_reqs=1, chunk_start=0, chunk=[], all_slices={},
                          on_progress=None, cancel_check=None)


def test_colliding_uuid_mappings_are_dropped_from_both_maps():
    runner = _runner()
    runner.path_to_uuid = {"a.webp": UUID_A, "b.webp": UUID_A, "c.webp": UUID_B}
    mapping = dict(runner.path_to_uuid)

    runner._drop_colliding_uuid_mappings(mapping)

    assert mapping == {"c.webp": UUID_B}
    # 跨轮缓存也必须清掉，否则下一轮 _verify_cached_uploads 会把错误映射复用回来
    assert runner.path_to_uuid == {"c.webp": UUID_B}


def test_distinct_uuid_mappings_survive_untouched():
    runner = _runner()
    mapping = {"a.webp": UUID_A, "b.webp": UUID_B}
    runner._drop_colliding_uuid_mappings(mapping)
    assert mapping == {"a.webp": UUID_A, "b.webp": UUID_B}


# ── 3. 提交闸门：锚点没就绪就不提交 ──────────────────────────────────────

class _Req:
    def __init__(self, image, end_image, prompt="p"):
        self.image = image
        self.end_image = end_image
        self.prompt = prompt
        self.model = "Veo 3.1"
        self.ratio = "9:16"


def _frame(tmp_path, name):
    """落一个真实存在的帧文件：clean_path 对不存在的路径会去 outputs/ 里找同名文件，
    测试不该被本机历史项目的残留数据影响。"""
    p = tmp_path / name
    p.write_bytes(b"frame")
    return str(p)


def _submit_with(monkeypatch, path_to_uuid, req):
    """跑 _submit_tasks，返回 (结果列表, 是否真的调用了提交)。"""
    runner = V._ChunkRunner(total_reqs=1, chunk_start=0, chunk=[req], all_slices={},
                            on_progress=None, cancel_check=None)
    submitted_calls = []

    def _boom(*a, **k):
        submitted_calls.append(a)
        return {"tile_id": "fe_id_x", "click_time": 0.0}

    monkeypatch.setattr(V, "_submit_video_to_canvas", _boom)
    monkeypatch.setattr(V, "_inspect_all_pending_tiles", lambda *a, **k: {})
    monkeypatch.setattr(V, "random_sleep", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_ensure_video_config", lambda *a, **k: None)

    class _P:
        def evaluate(self, *_a):
            return []

    out = runner._submit_tasks(_P(), [(0, req)], [], path_to_uuid)
    return out, submitted_calls


def test_missing_end_frame_uuid_fails_the_slot_instead_of_submitting(monkeypatch, tmp_path):
    start = _frame(tmp_path, "img_001.webp")
    end = _frame(tmp_path, "img_002.webp")
    req = _Req(start, end)

    out, calls = _submit_with(monkeypatch, {start: UUID_A}, req)

    assert calls == [], "锚点帧缺失时绝不能提交——那就是一段文生视频"
    assert out[0]["status"] == "failed"
    assert "文生视频" in out[0]["message"]


def test_two_distinct_frames_sharing_one_uuid_fails_the_slot(monkeypatch, tmp_path):
    start = _frame(tmp_path, "img_001.webp")
    end = _frame(tmp_path, "img_002.webp")
    req = _Req(start, end)

    out, calls = _submit_with(monkeypatch, {start: UUID_A, end: UUID_A}, req)

    assert calls == []
    assert out[0]["status"] == "failed"
    assert "同一个画布 UUID" in out[0]["message"]


def test_complete_mapping_still_submits(monkeypatch, tmp_path):
    start = _frame(tmp_path, "img_001.webp")
    end = _frame(tmp_path, "img_002.webp")
    req = _Req(start, end)

    out, calls = _submit_with(monkeypatch, {start: UUID_A, end: UUID_B}, req)

    assert len(calls) == 1
    assert out[0]["status"] == "generating"


def test_hero_single_frame_slot_is_not_blocked(monkeypatch, tmp_path):
    """HERO 展示视频只有首帧、没有结束锚点，闸门不能误伤。"""
    start = _frame(tmp_path, "img_012.webp")
    req = _Req(start, "")

    out, calls = _submit_with(monkeypatch, {start: UUID_A}, req)

    assert len(calls) == 1
    assert out[0]["status"] == "generating"


# ── 4. 槽位直接上传：验不到缩略图就是失败 ────────────────────────────────

class _SlotContainer:
    def __init__(self, text, thumb_after_calls=None):
        self.text = text
        self._calls = 0
        self._thumb_after = thumb_after_calls

    def inner_text(self):
        return self.text

    def click(self, **kwargs):
        return None

    def locator(self, selector):
        self._calls += 1
        has = self._thumb_after is not None and self._calls >= self._thumb_after
        return _CountLocator(1 if (has and selector == "img") else 0)


class _CountLocator:
    def __init__(self, n):
        self._n = n

    def count(self):
        return self._n

    @property
    def first(self):
        return self

    def is_visible(self, timeout=None):
        return self._n > 0

    def click(self, **kwargs):
        return None

    def set_input_files(self, paths):
        return None


class _SlotPage:
    def __init__(self, container, prompt_refs=()):
        self.container = container
        self.prompt_refs = list(prompt_refs)

    def locator(self, selector):
        if "haspopup" in selector or "jekiem" in selector:
            return _ContainerList([self.container])
        return _CountLocator(1)


class _ContainerList:
    def __init__(self, items):
        self.items = items

    def count(self):
        return len(self.items)

    def nth(self, i):
        return self.items[i]

    @property
    def first(self):
        return self.items[0]


@pytest.fixture
def _patch_slot_deps(monkeypatch):
    monkeypatch.setattr(H, "random_sleep", lambda *a, **k: None)
    monkeypatch.setattr(H.time, "sleep", lambda *_a: None)


def test_slot_upload_without_thumbnail_reports_failure(_patch_slot_deps, monkeypatch, tmp_path):
    """set_input_files 没抛异常 ≠ 图片进了槽位。验不到就必须返回 False，
    否则调用方会拿着空首尾帧报"挂载完成 (2/2)"。"""
    monkeypatch.setattr(H, "_get_prompt_reference_uuids", lambda *a, **k: [])
    f = tmp_path / "img_001.webp"
    f.write_bytes(b"x")
    page = _SlotPage(_SlotContainer("Start"))

    assert H._upload_to_slot_directly(page, "Start", str(f), verify_timeout=1) is False


def test_slot_upload_with_thumbnail_reports_success(_patch_slot_deps, monkeypatch, tmp_path):
    monkeypatch.setattr(H, "_get_prompt_reference_uuids", lambda *a, **k: [])
    f = tmp_path / "img_001.webp"
    f.write_bytes(b"x")
    page = _SlotPage(_SlotContainer("Start", thumb_after_calls=1))

    assert H._upload_to_slot_directly(page, "Start", str(f), verify_timeout=5) is True


def test_slot_upload_accepts_prompt_ref_count_growth(_patch_slot_deps, monkeypatch, tmp_path):
    """槽位缩略图不在容器里时，提示词区参考图数量变多同样算落地。"""
    monkeypatch.setattr(H, "_get_prompt_reference_uuids", lambda *a, **k: [UUID_A])
    f = tmp_path / "img_001.webp"
    f.write_bytes(b"x")
    page = _SlotPage(_SlotContainer("Start"))

    assert H._upload_to_slot_directly(page, "Start", str(f),
                                      refs_before=[], verify_timeout=5) is True


def test_partial_slot_fallback_is_reported_as_mount_failure(monkeypatch, tmp_path):
    """Start 传上了、End 没传上：只有一端锚点的请求同样不能提交。"""
    start = tmp_path / "img_001.webp"
    end = tmp_path / "img_002.webp"
    for p in (start, end):
        p.write_bytes(b"x")

    monkeypatch.setattr(H, "_clear_prompt_reference_chips_video", lambda *a, **k: 0)
    monkeypatch.setattr(H, "_mount_flow_images_to_prompt", lambda *a, **k: [])
    monkeypatch.setattr(H, "_get_prompt_reference_uuids", lambda *a, **k: [])
    monkeypatch.setattr(H, "_upload_to_slot_directly",
                        lambda page, label, path_, **k: label == "Start")

    meta = {}
    mounted = H._mount_video_prompt_refs(object(), start_ref=UUID_A, end_ref=UUID_B,
                                         start_path=str(start), end_path=str(end),
                                         result_meta=meta)

    assert mounted == []
    assert meta["strategy"] == ""


# ── 5. 点 Generate 前的最后复核 ─────────────────────────────────────────

def test_refs_lost_before_generate_is_detected(monkeypatch):
    monkeypatch.setattr(H, "_get_prompt_reference_uuids", lambda *a, **k: [])
    meta = {"strategy": "prompt_chips", "expected": [UUID_A, UUID_B], "refs": [UUID_A, UUID_B]}
    assert H._video_refs_still_attached(object(), meta) is False


def test_refs_reordered_before_generate_is_detected(monkeypatch):
    """顺序错了就是首尾帧对调，生成的是一段倒放的过渡。"""
    monkeypatch.setattr(H, "_get_prompt_reference_uuids", lambda *a, **k: [UUID_B, UUID_A])
    meta = {"strategy": "prompt_chips", "expected": [UUID_A, UUID_B], "refs": [UUID_A, UUID_B]}
    assert H._video_refs_still_attached(object(), meta) is False


def test_refs_intact_before_generate_passes(monkeypatch):
    monkeypatch.setattr(H, "_get_prompt_reference_uuids", lambda *a, **k: [UUID_A, UUID_B])
    meta = {"strategy": "prompt_chips", "expected": [UUID_A, UUID_B], "refs": [UUID_A, UUID_B]}
    assert H._video_refs_still_attached(object(), meta) is True


def test_frame_slot_strategy_checks_reference_count(monkeypatch):
    monkeypatch.setattr(H, "_get_prompt_reference_uuids", lambda *a, **k: [UUID_A])
    meta = {"strategy": "frame_slots", "expected": [], "refs": [UUID_A, UUID_B]}
    assert H._video_refs_still_attached(object(), meta) is False


def test_prompt_reference_scan_is_structural_when_long_prompt_expands_upward():
    """长提示词会把输入条顶到视口上方，不能再用 bottom-420 坐标门槛判丢图。"""

    class _P:
        def __init__(self):
            self.script = ""

        def evaluate(self, script):
            self.script = script
            return [UUID_A, UUID_B]

    page = _P()
    assert H._get_prompt_reference_uuids(page, limit=2) == [UUID_A, UUID_B]
    assert "arrow_forward" in page.script
    assert "bar.querySelectorAll" in page.script
    assert "innerHeight" not in page.script


def test_long_prompt_reference_scan_does_not_block_submit_guard():
    class _P:
        def evaluate(self, _script):
            # 模拟输入条已经因长提示词扩展到页面较高位置，但结构扫描仍能读到 refs。
            return [UUID_A, UUID_B]

    meta = {
        "strategy": "prompt_chips",
        "expected": [UUID_A, UUID_B],
        "refs": [UUID_A, UUID_B],
    }
    assert H._video_refs_still_attached(_P(), meta) is True


def test_declared_anchor_without_canvas_ref_never_submits(monkeypatch):
    """最后一道防线：req 声明了锚点帧但 image/end_image 是空 UUID。"""

    class _R:
        image = ""
        end_image = ""
        prompt = "p"
        original_image = "C:/frames/img_001.webp"
        original_end_image = "C:/frames/img_002.webp"

    with pytest.raises(RuntimeError) as err:
        H._submit_video_to_canvas(object(), _R(), [])
    assert "CANVAS_MOUNT_FAILED" in str(err.value)
    assert "文生视频" in str(err.value)


# ── 6. 单段视频链不再用 Ctrl+A 清空把 chip 一起删掉 ──────────────────────

def test_single_video_path_appends_prompt_instead_of_clearing():
    import inspect

    source = inspect.getsource(V._generate_video_google_fx)
    fill_at = source.index("_fill_prompt_text(")
    mount_at = source.index("_mount_video_prompt_refs(")
    assert mount_at < fill_at
    # 挂载之后不允许再出现全选删除——那会连参考图 chip 一起删掉
    assert "ControlOrMeta+a" not in source[mount_at:]


# ── 7. 路径纠偏不许跨项目张冠李戴 ────────────────────────────────────────

def test_clean_path_refuses_cross_project_same_name_substitution(tmp_path, monkeypatch):
    """每个项目的帧都叫 img_001.webp。丢帧时按 basename 在整个 outputs/ 里随手
    挑一张，等于把别的项目的画面当成本段的首尾锚点传上去——生成的片段跟本片
    毫无关系，还会在重试轮反复抓同一张，永远修不好。"""
    from integrations.google_fx.utils import browser as B

    root = tmp_path / "outputs"
    for proj in ("run_A_title", "run_B_title"):
        d = root / proj / "frames"
        d.mkdir(parents=True)
        (d / "img_001.webp").write_bytes(b"x")
    monkeypatch.setattr(B, "OUTPUT_DIR", str(root))

    missing = str(root / "run_C_title" / "frames" / "img_001.webp")
    assert B.clean_path(missing) == os.path.normpath(missing)


def test_clean_path_still_fixes_a_moved_project_root(tmp_path, monkeypatch):
    """项目目录整体搬过位置（盘符/根目录变了）仍然要能纠偏——
    判据是「项目目录/子目录/文件名」三段尾巴唯一命中。"""
    from integrations.google_fx.utils import browser as B

    root = tmp_path / "outputs"
    d = root / "run_A_title" / "frames"
    d.mkdir(parents=True)
    (d / "img_001.webp").write_bytes(b"x")
    (root / "run_B_title" / "frames").mkdir(parents=True)
    (root / "run_B_title" / "frames" / "img_001.webp").write_bytes(b"x")
    monkeypatch.setattr(B, "OUTPUT_DIR", str(root))

    stale = os.path.join("D:", os.sep, "old", "run_A_title", "frames", "img_001.webp")
    assert B.clean_path(stale) == str(d / "img_001.webp")


# ── 8. 被拒收的废片不许在重试轮被重新认领 ────────────────────────────────

def test_rejected_tile_is_not_re_adopted_on_retry_round(monkeypatch):
    """锚点校验拒收的卡片还留在画布上，提示词切片照样匹配。不排除它，
    重试轮就会把同一段废片重新认领→重新下载→重新被拒，整轮重试空转。"""
    req = _Req("", "")
    runner = V._ChunkRunner(total_reqs=1, chunk_start=0, chunk=[req], all_slices={0: "slice"},
                            on_progress=None, cancel_check=None)
    runner.gen_retry_used = 1
    runner.rejected_tile_ids = {"fe_id_bad"}

    monkeypatch.setattr(V, "_scan_canvas_tiles", lambda page: [
        {"tileId": "fe_id_bad", "originalTileId": "fe_id_bad",
         "textClean": "slice", "videoSrc": "http://v/bad.mp4", "failed": False},
    ])

    adopted, still = runner._adopt_completed_tiles(object(), [(0, req)])
    assert adopted == []
    assert still == [(0, req)]


def test_untainted_tile_is_still_adopted_on_retry_round(monkeypatch):
    req = _Req("", "")
    runner = V._ChunkRunner(total_reqs=1, chunk_start=0, chunk=[req], all_slices={0: "slice"},
                            on_progress=None, cancel_check=None)
    runner.gen_retry_used = 1

    monkeypatch.setattr(V, "_scan_canvas_tiles", lambda page: [
        {"tileId": "fe_id_ok", "originalTileId": "fe_id_ok",
         "textClean": "slice", "videoSrc": "http://v/ok.mp4", "failed": False},
    ])

    adopted, still = runner._adopt_completed_tiles(object(), [(0, req)])
    assert len(adopted) == 1 and adopted[0]["tile_id"] == "fe_id_ok"
    assert still == []
