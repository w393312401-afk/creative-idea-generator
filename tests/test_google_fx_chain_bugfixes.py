# -*- coding: utf-8 -*-
"""Regression coverage for the 2026-08-02 Google FX generation-chain triage.

每个用例对应一个当时在线上会真实触发、但整套测试都没盖到的缺陷。
"""

import inspect

import pytest

from integrations.google_fx.services import google_fx_image as image_service
from integrations.google_fx.services import google_fx_video as video_service
from integrations.google_fx.utils import account_pool as account_pool_mod
from integrations.google_fx.utils import browser as browser_mod


# ── P0-1: Page.evaluate() 没有 timeout 参数，探活函数因此恒判"已死" ──────────

class _HealthyPage:
    """按 playwright sync 版 Page 的真实签名实现探活相关方法。

    关键点：`evaluate(self, expression, arg=None)` —— **没有** timeout 关键字参数。
    """

    def __init__(self, name="page"):
        self.name = name
        self.new_page_marker = False

    def is_closed(self):
        return False

    def evaluate(self, expression, arg=None):
        return 1

    def wait_for_function(self, expression, *, arg=None, timeout=None, polling=None):
        return 1


class _DeadPage(_HealthyPage):
    def evaluate(self, expression, arg=None):
        raise RuntimeError("Frame was detached")

    def wait_for_function(self, expression, *, arg=None, timeout=None, polling=None):
        raise RuntimeError("Frame was detached")


class _Context:
    def __init__(self, pages):
        self.pages = pages
        self.new_page_calls = 0

    def new_page(self):
        self.new_page_calls += 1
        fresh = _HealthyPage("brand-new")
        fresh.new_page_marker = True
        self.pages.append(fresh)
        return fresh


def test_probe_matches_the_real_playwright_page_signature():
    """守住这组用例赖以成立的前提：sync 版 Page.evaluate 确实没有 timeout 参数。

    哪天 playwright 给 Page.evaluate 加上了 timeout，这里会先红，提醒我们
    _HealthyPage 的替身签名（以及下面那些行为用例的判别力）需要重新评估。
    """
    from playwright.sync_api import Page

    assert "timeout" not in inspect.signature(Page.evaluate).parameters
    assert "timeout" in inspect.signature(Page.wait_for_function).parameters


def test_healthy_page_is_reported_alive():
    """行为守卫：替身的 evaluate 用的是真实签名（无 timeout 关键字）。

    一旦探活退回 `page.evaluate("1", timeout=2000)`，这里会抛 TypeError → 被
    _page_is_alive 自己的 except 吞掉 → 返回 False，本用例即刻转红。那正是
    2026-08-02 之前的线上行为：**任何**页面都被判成已死，_connect_fx_page 每次
    生成都新建标签页并重新导航，标签页逐次累积。
    """
    assert browser_mod._page_is_alive(_HealthyPage()) is True


def test_detached_page_is_reported_dead():
    assert browser_mod._page_is_alive(_DeadPage()) is False


def test_closed_page_is_reported_dead():
    class _Closed(_HealthyPage):
        def is_closed(self):
            return True

    assert browser_mod._page_is_alive(_Closed()) is False


def test_recover_reuses_a_healthy_tab_instead_of_opening_a_new_one():
    broken = _DeadPage("broken")
    healthy = _HealthyPage("healthy-flow-tab")
    ctx = _Context([broken, healthy])

    recovered = browser_mod._recover_valid_page(ctx, broken)

    assert recovered is healthy
    assert ctx.new_page_calls == 0, "有活页可用时不该新建标签页（会导致标签页累积）"


def test_recover_creates_a_page_only_when_every_tab_is_dead():
    broken = _DeadPage("broken")
    ctx = _Context([broken, _DeadPage("also-dead")])

    recovered = browser_mod._recover_valid_page(ctx, broken)

    assert recovered.new_page_marker is True
    assert ctx.new_page_calls == 1


# ── P1-4: tile 扫描接受 blob:/data: 后，绝对化拼接会拼出无法下载的垃圾 URL ────

def test_relative_flow_path_is_absolutized():
    assert image_service._absolute_media_url(
        "/fx/api/trpc/media.getMediaUrlRedirect?name=abc"
    ) == "https://labs.google/fx/api/trpc/media.getMediaUrlRedirect?name=abc"


def test_absolute_url_is_passed_through():
    url = "https://flow-content.google/image/abc?Expires=1"
    assert image_service._absolute_media_url(url) == url


@pytest.mark.parametrize("src", [
    "blob:https://labs.google/2b0e6f4a-1111-4222-8333-444455556666",
    "data:image/png;base64,iVBORw0KGgo=",
    "",
    None,
])
def test_undownloadable_srcs_are_rejected_not_mangled(src):
    """blob:/data: 取不到就得返回空，绝不能拼成 https://labs.googleblob:https://...

    tile 扫描的 looksLikeMedia 现在会放这两种 src 进来；老写法
    `src if src.startswith("http") else f"https://labs.google{src}"`
    会造出一个必然下载失败的地址，进而把整批按「已生成但下载落盘失败」截断。
    """
    result = image_service._absolute_media_url(src)
    assert result == ""
    assert "labs.googleblob" not in result
    assert "labs.googledata" not in result


# ── P0-3: 批内失败必须保留已成功的前缀，而不是连同积分一起丢掉 ───────────────

def test_every_in_loop_failure_path_preserves_the_successful_prefix():
    """参考图挂载失败 / 近重复判定不能再直接 raise 出提交循环。

    循环里的 raise 会跳过 `result["image_urls"] = local_paths`，让 except 分支返回
    image_urls=[]，把已经生成、下载、扣过积分的前几张图全部作废，上层重试再从头
    生成一遍。超时和下载失败两条路一直是 break 保前缀，这两条要对齐。
    """
    source = inspect.getsource(
        image_service._generate_images_batch_google_fx_single_attempt
    )
    loop_body = source.split("for idx, prompt_text in enumerate(prompts):", 1)[1]

    assert "_halt_batch_keeping_prefix" in loop_body
    # 提交循环体内不该再有裸 raise（_halt_batch_keeping_prefix 在没有前缀可保时
    # 才代为抛出原异常，那句 raise 在辅助函数里，不在循环体内）。
    assert "raise _reference_mount_error(" not in loop_body


def test_halt_helper_raises_when_there_is_no_prefix_to_keep():
    """一张都没成时没有前缀可保，必须把原异常原样抛出，保留它的分类前缀。"""
    source = inspect.getsource(image_service._generate_images_batch_google_fx_single_attempt)
    helper = source.split("def _halt_batch_keeping_prefix", 1)[1].split("\n            for idx,", 1)[0]
    assert "if not local_paths:" in helper
    assert "raise exc" in helper
    assert 'result["failed_index"] = idx' in helper


# ── P1-5: 视频参考模式必须进配置缓存键，否则热切模式不生效 ───────────────────

def test_video_config_cache_key_includes_the_reference_mode():
    """GOOGLE_FX_VIDEO_REF_MODE 是现读运行时配置，必须参与 _confirmed_config 比较。

    漏掉它的话，只要模型/比例/时长没变，在控制台把「帧 / 素材」切过去后整段配置
    校验都会被早退跳过，新模式永不生效。
    """
    source = inspect.getsource(video_service._ChunkRunner._ensure_video_config)
    head, _, tail = source.partition("if self._confirmed_config == wanted:")
    assert tail, "早退判断不见了，用例需要跟着更新"
    # 参考模式要在早退之前就读出来，并且出现在 wanted 元组里。
    assert "get_runtime_google_fx_video_ref_mode()" in head
    wanted_line = [ln for ln in head.splitlines() if "wanted = (" in ln][0]
    assert "_video_ref_mode" in wanted_line


# ── P2-6: 合并导入不能抹掉线上已有的备注 ────────────────────────────────────

def test_merge_import_keeps_existing_note(tmp_path, monkeypatch):
    """overwrite=False 且条目不带 note 时，原备注必须原样保留。

    add_account 对 name/serial 是"留空沿用旧值"，但对 note 是"留空即清空"。
    import_config 照搬了同一套 `else ""` 写法，于是平滑合并反而会清空所有备注。
    """
    state_file = tmp_path / "account_pool.json"
    monkeypatch.setattr(account_pool_mod, "_STATE_FILE", str(state_file), raising=False)

    pool = account_pool_mod.AccountPool()
    captured = {}

    def _fake_add_account(user_id, name="", note="", serial_number=""):
        captured[user_id] = {"name": name, "note": note, "serial_number": serial_number}
        return {"user_id": user_id}

    monkeypatch.setattr(pool, "add_account", _fake_add_account)
    monkeypatch.setattr(pool, "list_accounts", lambda *a, **k: [])
    monkeypatch.setattr(
        account_pool_mod, "_read_state",
        lambda: {"env-1": {"name": "老名字", "note": "重要备注：主力号"}},
    )

    pool.import_config({"accounts": [{"user_id": "env-1"}]}, overwrite=False)

    assert captured["env-1"]["note"] == "重要备注：主力号"


def test_overwrite_import_clears_note_when_absent(tmp_path, monkeypatch):
    """overwrite=True 是显式覆盖语义，缺失的 note 就该被清空。"""
    captured = {}
    pool = account_pool_mod.AccountPool()

    def _fake_add_account(user_id, name="", note="", serial_number=""):
        captured[user_id] = {"note": note}
        return {"user_id": user_id}

    monkeypatch.setattr(pool, "add_account", _fake_add_account)
    monkeypatch.setattr(pool, "list_accounts", lambda *a, **k: [])
    monkeypatch.setattr(
        account_pool_mod, "_read_state",
        lambda: {"env-1": {"note": "旧备注"}},
    )

    pool.import_config({"accounts": [{"user_id": "env-1"}]}, overwrite=True)

    assert captured["env-1"]["note"] == ""


def test_duplicate_user_id_in_one_payload_counts_as_added_once(monkeypatch):
    pool = account_pool_mod.AccountPool()
    monkeypatch.setattr(pool, "add_account", lambda **kw: {"user_id": kw["user_id"]})
    monkeypatch.setattr(pool, "list_accounts", lambda *a, **k: [])
    monkeypatch.setattr(account_pool_mod, "_read_state", lambda: {})

    outcome = pool.import_config(
        {"accounts": [{"user_id": "env-dup"}, {"user_id": "env-dup"}]},
        overwrite=False,
    )

    assert outcome["added"] == 1
    assert outcome["updated"] == 1
