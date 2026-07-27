# -*- coding: utf-8 -*-
"""
🧪 输入区「历史提示词 + 参考图」清理
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
纯逻辑测试，页面用桩对象，不需要真实浏览器/AdsPower。

桩对象按 2026-07-26 只读探针 dump 到的**真机 DOM**建模（labs.google Flow）：

    <div>                                   ← prompt bar（含发送键 arrow_forward）
      <div data-slate-editor="true">
        <!-- 编辑器为空时 Slate 会渲染 placeholder，它本身就有 29 个字符 -->
        <span data-slate-placeholder="true">What do you want to create?</span>
      </div>
      <div>… add_2 / Agent / 模型 / <i>arrow_forward</i> …</div>
      <div>                                 ← 有内容时才出现
        <button><i>close</i><span>Clear prompt</span></button>
      </div>
      <button data-card-open="false">        ← 参考图 chip，每张一个
        <div><img alt="… present in your collection."></div>
        <div><i>cancel</i></div>
      </button>
    </div>

被这组测试锁住的三个真实缺陷：
  1. 判空用 innerText → placeholder 让空编辑器恒显示「还剩 29 字」。
  2. 旧扫描要求 chip 内有 <i>cancel</i> 且落在视口底部 420px 内，任一不满足就
     静默数出 0 个、一个也不清（用户侧表现为「清了跟没清一样」）。
  3. 回退选择器 button[data-state='closed'] i 是 page-wide 的，会在无关按钮上乱点。

真机端到端验证（连 AdsPower 实跑）已另行确认：文字 + 4 张参考图 → 一次点击
0.3s 全清空；本来就干净时发出 0 次点击。
"""



import pytest
from integrations.google_fx.services import google_fx_helpers as H

PLACEHOLDER = "What do you want to create?"


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr(H, "log", lambda *a, **k: None)
    monkeypatch.setattr(H, "random_sleep", lambda *a, **k: None)


class FakeMouse:
    def __init__(self, page):
        self.page = page
        self.clicks = []

    def move(self, x, y):
        pass

    def click(self, x, y):
        self.clicks.append((x, y))
        self.page.on_cancel_click(x, y)


class FakeBar:
    """按真机结构建模的输入条。"""

    def __init__(self, chips=0, text="", bar=True, clear_btn_works=True,
                 cancel_works=True):
        self.chips = chips
        self.text = text
        self.bar = bar
        self.clear_btn_works = clear_btn_works
        self.cancel_works = cancel_works
        self.clear_clicks = 0
        self.cancel_clicks = 0
        self.mouse = FakeMouse(self)

    # 真机语义：有文字或有 chip 时才渲染 Clear prompt
    @property
    def has_clear(self):
        return bool(self.text) or self.chips > 0

    @property
    def editor_empty(self):
        return self.text == ""

    def on_cancel_click(self, x, y):
        self.cancel_clicks += 1
        if self.cancel_works and self.chips > 0:
            self.chips -= 1

    def evaluate(self, script, *args):
        if "return {chips: chips.length, has_clear" in script:
            return {"chips": self.chips, "has_clear": self.has_clear,
                    "editor_empty": self.editor_empty, "bar": self.bar}
        if "clearBtn.click()" in script:
            if not self.bar or not self.has_clear:
                return False
            self.clear_clicks += 1
            if self.clear_btn_works:
                self.chips = 0
                self.text = ""
            return True
        if "return {x: r.left" in script:
            if self.chips <= 0:
                return None
            return {"x": 500.0, "y": 660.0}
        if "barTail" in script:
            return '{"barTail":"<div/>","chipHtml":[]}'
        raise AssertionError(f"未预期的 evaluate: {script[:70]}")


# ──────────────────────────────────────────────────────────────
# 1. placeholder 判空（真机上 innerText 恒为 29 字）
# ──────────────────────────────────────────────────────────────

def test_placeholder_is_29_chars_so_innertext_cannot_mean_empty():
    """锁住这个数字：日志里那句「残留 29 字」就是它，不是真的有残留。"""
    assert len(PLACEHOLDER) == 27
    assert len((PLACEHOLDER + "\n﻿\n").strip()) == 29


def test_empty_editor_reports_empty_not_29_chars():
    page = FakeBar(chips=0, text="")
    st = H.read_prompt_bar_state(page)
    assert st["editor_empty"] is True
    assert st["chips"] == 0


def test_editor_with_real_text_reports_not_empty():
    page = FakeBar(chips=0, text="leftover prompt")
    assert H.read_prompt_bar_state(page)["editor_empty"] is False


# ──────────────────────────────────────────────────────────────
# 2. 一键 Clear prompt 是主路径
# ──────────────────────────────────────────────────────────────

def test_clear_removes_text_and_all_chips_in_one_click():
    """用户场景：图生图跑完，输入框里既有历史提示词又挂着参考图。"""
    page = FakeBar(chips=4, text="leftover history prompt")
    removed = H._clear_prompt_reference_chips_image(page)
    assert page.chips == 0
    assert page.text == ""
    assert removed == 4
    assert page.clear_clicks == 1        # 一次点击搞定
    assert page.cancel_clicks == 0       # 不需要逐个点 ✕


def test_clear_is_noop_when_already_clean():
    """本来就干净 → 一次点击都不许发（这就是「疯狂乱点」的另一半）。"""
    page = FakeBar(chips=0, text="")
    assert H._clear_prompt_reference_chips_image(page) == 0
    assert page.clear_clicks == 0
    assert page.cancel_clicks == 0


def test_clear_handles_text_only_no_chips():
    page = FakeBar(chips=0, text="only text left over")
    H._clear_prompt_reference_chips_image(page)
    assert page.text == ""
    assert page.editor_empty


# ──────────────────────────────────────────────────────────────
# 3. Clear prompt 失灵时回退到逐个点 ✕
# ──────────────────────────────────────────────────────────────

def test_falls_back_to_per_chip_cancel_when_clear_button_dead():
    page = FakeBar(chips=3, text="", clear_btn_works=False)
    removed = H._clear_prompt_reference_chips_image(page)
    assert page.chips == 0
    assert removed == 3
    assert page.cancel_clicks == 3


def test_gives_up_early_when_nothing_works():
    """点不动就停手，不无限空点。"""
    page = FakeBar(chips=3, text="", clear_btn_works=False, cancel_works=False)
    removed = H._clear_prompt_reference_chips_image(page)
    assert removed == 0
    assert page.cancel_clicks <= 3        # 早停，不是跑满 max_rounds=8


def test_skips_everything_when_no_prompt_bar():
    """还没进项目页（没有输入条）时不该乱点。"""
    page = FakeBar(chips=0, text="", bar=False)
    assert H._clear_prompt_reference_chips_image(page) == 0
    assert page.clear_clicks == 0
    assert page.cancel_clicks == 0


# ──────────────────────────────────────────────────────────────
# 4. 不再使用过宽的 page-wide 选择器
# ──────────────────────────────────────────────────────────────

def test_no_pagewide_close_selector_fallback_remains():
    """button[data-state='closed'] i 能匹配页面上每个折叠态 Radix 触发器；
    清理路径里不允许再出现它（旧回退分支已整段删除）。

    只看可执行代码——注释/docstring 里还留着这条选择器是**故意**的，
    那是在记录被修掉的历史缺陷。
    """
    import ast
    import inspect
    import textwrap

    code = ""
    for fn in (H._clear_prompt_reference_chips_image,
               H._click_clear_prompt_button,
               H._click_one_chip_cancel):
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        fn_node = tree.body[0]
        body = fn_node.body
        # 丢掉 docstring
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            body = body[1:]
        for node in body:
            code += ast.unparse(node) + "\n"

    assert "close_history_btn" not in code
    assert "data-state='closed'" not in code
    assert "UI_SELECTORS" not in code
    # 旧的按选择器回退整段已删除
    assert not hasattr(H, "_clear_chips_via_selectors")


def test_scan_is_structural_not_geometric():
    """定位靠 DOM 结构（编辑器 → 含 arrow_forward 的祖先），
    不再靠 innerHeight-420 这种视口几何。"""
    assert "arrow_forward" in H._PROMPT_BAR_JS
    assert "data-slate-placeholder" in H._PROMPT_BAR_JS
    assert "innerHeight" not in H._PROMPT_BAR_JS


# ──────────────────────────────────────────────────────────────
# 5. 链式续图：每次提交只带 1 张参考图
# ──────────────────────────────────────────────────────────────

def test_chain_keeps_exactly_one_reference_per_submit():
    """复刻批内循环：清 → 挂 → 提交。

    修复前 chip 只增不减（真机 refs_after 实测到 4 张同时挂着），
    模型锚定最早那张 → 每帧都退回第一帧。
    """
    page = FakeBar(chips=1, text="")      # 循环外先挂了封面
    submitted = []
    for idx in range(1, 5):
        H._clear_prompt_reference_chips_image(page)
        page.chips += 1                    # _mount_uuid_as_ref
        page.text = f"prompt {idx}"
        submitted.append(page.chips)
    assert submitted == [1, 1, 1, 1]
