import inspect

from integrations.google_fx.services import google_fx_diagnostics as diagnostics
from integrations.google_fx.services import google_fx_helpers as helpers
from integrations.google_fx.services import google_fx_image
from integrations.google_fx.utils import account_binding
from integrations.google_fx import models


class _Page:
    def __init__(self, url):
        self.url = url


def test_l1_creates_project_before_looking_for_prompt(monkeypatch):
    page = _Page("https://labs.google/fx/tools/flow")
    events = []

    def create_project(target):
        events.append("create")
        target.url = "https://labs.google/fx/tools/flow/project/selftest-1"
        return True

    monkeypatch.setattr(helpers, "_click_new_project_button", create_project)
    monkeypatch.setattr(helpers, "_find_fx_prompt_input", lambda _target, announce=False: None)
    monkeypatch.setattr(
        helpers, "_wait_for_fx_toolbar",
        lambda target, timeout: events.append(("wait_toolbar", timeout)),
    )

    result = diagnostics._ensure_flow_project_open(page, toolbar_timeout=12)

    assert result.endswith("/project/selftest-1")
    assert events == ["create", ("wait_toolbar", 12)]


def test_l1_reuses_an_open_project(monkeypatch):
    page = _Page("https://labs.google/fx/tools/flow/project/existing")
    events = []
    monkeypatch.setattr(
        helpers, "_click_new_project_button",
        lambda _target: events.append("unexpected_create"),
    )
    monkeypatch.setattr(
        helpers, "_wait_for_fx_toolbar",
        lambda target, timeout: events.append(("wait_toolbar", timeout)),
    )

    diagnostics._ensure_flow_project_open(page)

    assert events == [("wait_toolbar", 30)]


def test_l1_reuses_generation_workspace_without_project_url(monkeypatch):
    """Flow 会恢复到无 /project/ 路由的生成页；有输入框时不应强行新建项目。"""
    page = _Page("https://labs.google/fx/tools/flow")
    events = []
    monkeypatch.setattr(helpers, "_find_fx_prompt_input",
                        lambda _target, announce=False: object())
    monkeypatch.setattr(
        helpers, "_click_new_project_button",
        lambda _target: events.append("unexpected_create"),
    )
    monkeypatch.setattr(
        helpers, "_wait_for_fx_toolbar",
        lambda target, timeout: events.append(("wait_toolbar", timeout)),
    )

    result = diagnostics._ensure_flow_project_open(page, toolbar_timeout=9)

    assert result == "https://labs.google/fx/tools/flow"
    assert events == [("wait_toolbar", 9)]


class _Locator:
    def __init__(self, hit):
        self._hit = hit

    def count(self):
        return 1 if self._hit else 0

    @property
    def first(self):
        return self

    def is_visible(self, timeout=0):
        return True


class _ProbePage:
    """只让 hits 里的选择器命中，其余一律 count=0。"""

    url = "https://labs.google/fx/tools/flow/project/probe"

    def __init__(self, hits):
        self._hits = set(hits)

    def locator(self, selector):
        return _Locator(selector in self._hits)


# 每条都是对应族的第 0 层，凑齐 _REQUIRED_WORKSPACE_FAMILIES 的主选择器命中。
_WORKSPACE_HITS = {
    "textarea",                                                    # prompt_input
    "button[aria-haspopup='dialog']:has(span:text('Create'))",     # add_media_btn
    "button:has(img[alt='User profile image'])",                   # account_menu_trigger
}


def test_probe_skips_keyword_tables():
    """config_btn_keywords 存的是模型名文本，不是选择器。

    交给 page.locator() 会被当元素名解析，永远 count=0，每次探针都白报一条失效。
    """
    probe = diagnostics.probe_selectors(_ProbePage(_WORKSPACE_HITS))
    probed = {row["family"] for row in probe["families"]}

    assert "config_btn_keywords" not in probed
    assert probed & diagnostics._REQUIRED_WORKSPACE_FAMILIES


def test_probe_reports_contextual_families_as_conditional():
    """弹窗/菜单/落地页的族在工作台页缺失是正常的，不该算失效。"""
    probe = diagnostics.probe_selectors(_ProbePage(_WORKSPACE_HITS))

    assert probe["summary"]["missing"] == 0
    assert probe["summary"]["conditional"] > 0
    assert probe["summary"]["primary"] == len(_WORKSPACE_HITS)

    states = {row["family"]: row["state"] for row in probe["families"]}
    assert states["flow_entry_btn"] == "conditional"


def test_login_page_selectors_never_count_as_missing_on_the_workspace():
    """google_login 组是 accounts.google.com 上的登录表单（utils/auto_login.py 用）。
    探针跑在 Flow 工作台页，这一整组必然全缺——报成 missing 会让探针从此永远
    亮红灯，"探针全绿"这个信号就废了。"""
    probe = diagnostics.probe_selectors(_ProbePage(_WORKSPACE_HITS))

    login_rows = [row for row in probe["families"] if row["group"] == "google_login"]
    assert login_rows, "登录页选择器应当仍然进探测报告（只是不算故障）"
    assert all(row["state"] == "conditional" for row in login_rows)


def test_deep_probe_is_opt_in_and_default_stays_read_only(monkeypatch):
    """默认探针不许点任何东西——它会对着生产页面跑。"""
    called = []
    monkeypatch.setattr(diagnostics, "_run_deep_probe",
                        lambda *a, **k: called.append("deep") or [])

    probe = diagnostics.probe_selectors(_ProbePage(_WORKSPACE_HITS))
    assert called == []
    assert probe["deep"] is False
    assert "deep_scenarios" not in probe


def test_deep_probe_upgrades_a_family_once_its_scenario_is_open(monkeypatch):
    """场景打开后重探到了，就不该再挂着 conditional。"""
    page = _ProbePage(_WORKSPACE_HITS)

    def fake_open(target):
        target._hits.add("div[role='dialog']")  # account_menu_surface 第 1 层
        return True

    monkeypatch.setattr(
        diagnostics, "_DEEP_PROBE_SCENARIOS",
        (("账号菜单", fake_open, ("account_menu_surface",)),))
    monkeypatch.setattr(diagnostics, "_safe_press_escape", lambda *a, **k: None,
                        raising=False)

    probe = diagnostics.probe_selectors(page, deep=True)
    row = next(r for r in probe["families"] if r["family"] == "account_menu_surface")

    assert row["state"] == "primary"
    assert row["probed_via"] == "账号菜单"
    assert probe["deep_scenarios"][0]["opened"] is True


def test_deep_probe_survives_a_scenario_that_will_not_open(monkeypatch):
    """一个场景打不开，不能带塌整个探针，且必须留下痕迹。"""
    def boom(_target):
        raise RuntimeError("触发器点不动")

    monkeypatch.setattr(
        diagnostics, "_DEEP_PROBE_SCENARIOS",
        (("账号菜单", boom, ("account_menu_surface",)),))

    probe = diagnostics.probe_selectors(_ProbePage(_WORKSPACE_HITS), deep=True)
    entry = probe["deep_scenarios"][0]

    assert entry["opened"] is False
    assert "触发器点不动" in entry["error"]
    assert probe["summary"]["total"] > 0


def test_open_config_panel_clicks_locator_from_helper_pair(monkeypatch):
    """配置按钮查找器返回 (locator, text)，深探针不能对整个 tuple 调 click。"""
    events = []

    class Button:
        def click(self):
            events.append("click")

    monkeypatch.setattr(helpers, "find_fx_config_button",
                        lambda _page: (Button(), "Nano Banana 2 · x1"))
    monkeypatch.setattr(diagnostics.time, "sleep", lambda _seconds: None)

    assert diagnostics._open_config_panel(object()) is True
    assert events == ["click"]


def test_probe_still_flags_a_broken_workspace_family():
    """工作台必需族真的全层未命中时，仍然要报 missing——否则探针就白做了。"""
    hits = _WORKSPACE_HITS - {"textarea"}
    probe = diagnostics.probe_selectors(_ProbePage(hits))

    states = {row["family"]: row["state"] for row in probe["families"]}
    assert states["prompt_input"] == "missing"
    assert probe["summary"]["missing"] == 1


def test_image_selftest_request_cannot_embed_browser_account():
    """L2 must select its account through task context, not the locked model."""
    request = models.ImageBatchRequest(prompts=["selftest"])

    assert request.prompts == ["selftest"]
    assert "user_id" not in request.model_dump()


def test_l2_binds_account_outside_the_locked_request(monkeypatch):
    captured = {}

    def generate(request):
        captured["request"] = request
        captured["account"] = account_binding.current_task_account()
        return {"status": "success", "image_urls": ["one.webp"]}

    monkeypatch.setattr(
        google_fx_image, "_generate_images_batch_google_fx_unlocked", generate)

    result = diagnostics._submit_minimal_image("diagnostic-account")

    assert result == {"status": "success", "results": 1}
    assert captured["account"] == "diagnostic-account"
    assert "user_id" not in captured["request"].model_dump()
    assert account_binding.current_task_account() is None


def test_config_count_accepts_current_flow_x_prefix():
    checks = helpers.check_fx_config(
        "🍌 Nano Banana 2 crop_9_16 x1",
        model="Nano Banana 2",
        orientation="crop_9_16",
        count="1x",
    )

    assert checks["count"] is True


def test_config_count_does_not_confuse_x1_with_x2():
    checks = helpers.check_fx_config(
        "🍌 Nano Banana 2 crop_9_16 x2",
        model="Nano Banana 2",
        orientation="crop_9_16",
        count="1x",
    )

    assert checks["count"] is False


def test_current_flow_count_control_uses_x_prefix_and_stable_aria_suffix():
    """Current Flow labels the tab x1 even though callers request legacy 1x."""
    source = inspect.getsource(helpers.fix_fx_config)
    assert "aria-controls$='-content-{_count_number}'" in source
    assert 'f"x{_count_number}"' in source


def test_l1_probe_config_functions():
    """验证 7 项 L1 自测配置与上传检测探针函数。"""
    page = _ProbePage({
        "button.flow_tab_slider_trigger[aria-controls$='-IMAGE']",
        "button.flow_tab_slider_trigger[aria-controls$='-VIDEO']",
        "button.flow_tab_slider_trigger[aria-controls$='-content-1']",
        "button.flow_tab_slider_trigger[aria-controls$='-content-PORTRAIT']",
        "button[role='tab']",
        "button[aria-haspopup='dialog']:has(span:text('Create'))",
        "input[type='file']",
    })

    assert "图片" in diagnostics.probe_image_config(page)
    assert "视频" in diagnostics.probe_video_config(page)
    assert "数量配置" in diagnostics.probe_count_config(page)
    assert "时长" in diagnostics.probe_duration_config(page)
    assert "比例" in diagnostics.probe_orientation_config(page)
    assert "参考模式" in diagnostics.probe_ref_mode_config(page)
    assert "上传配置" in diagnostics.probe_upload_config(page)
