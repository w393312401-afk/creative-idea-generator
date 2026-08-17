# -*- coding: utf-8 -*-
"""
🧪 离线 FX 复现夹具的自身回归 + 选择器探针 / 取证 / dry-run 的离线验证
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
清单 D3 / D6 / 测试第 8 项。这些用例不需要真账号、真 AdsPower、真额度，
所以"选择器漂移导致的失败"可以直接写成可复现的测试交给 agent 修。
"""

import json
import os

import pytest

from tests.fx_fakes import (
    DeadListener, FakeAdsPower, FakePage, flow_page_html, unused_port,
)

from integrations.google_fx.services.google_fx_diagnostics import probe_selectors
from integrations.google_fx.utils import account_pool as ap
from integrations.google_fx.utils import forensics


# ── 夹具自身：选择器匹配器是否可信 ────────────────────────────────────────────

def test_fake_page_matches_the_selector_syntax_the_probe_uses():
    page = FakePage()
    assert page.locator('textarea').count() == 1
    assert page.locator("[contenteditable='true']").count() == 1
    assert page.locator("button[aria-haspopup='dialog']").count() == 1
    assert page.locator("button[aria-label*='New project']").count() == 1
    assert page.locator("button:has-text('Generate')").count() == 1
    assert page.locator("button:has(i.google-symbols:text-is('add_2'))").count() == 1
    assert page.locator("div.credits").count() == 0, 'class 在 span 上，不该匹配到 div'
    assert page.locator("span.credits").count() == 1


def test_fake_page_reports_closed_like_playwright():
    page = FakePage()
    page.close()
    assert page.is_closed() is True
    with pytest.raises(RuntimeError):
        page.locator('textarea')


# ── D3：选择器探针能否发现"改版" ──────────────────────────────────────────────

def test_selector_probe_reports_healthy_page():
    probe = probe_selectors(FakePage())
    assert probe['summary']['total'] > 0
    prompt = next(row for row in probe['families'] if row['family'] == 'prompt_input')
    assert prompt['state'] == 'primary', '完整页面上主选择器应该直接命中'
    assert probe['selector_version']


def test_selector_probe_detects_missing_prompt_input():
    """复现"Flow 改版把输入框换掉"——探针必须把它报成 missing 而不是静默通过。"""
    page = FakePage(flow_page_html(prompt_input=False))
    probe = probe_selectors(page)
    prompt = next(row for row in probe['families'] if row['family'] == 'prompt_input')
    assert prompt['state'] == 'missing'
    assert probe['summary']['missing'] >= 1


def test_selector_probe_marks_conditional_families_as_not_broken():
    """只在弹窗打开后才存在的族，在静止页面上缺失是正常的，不能报成故障。"""
    probe = probe_selectors(FakePage(flow_page_html(account_menu=False)))
    conditional = [row for row in probe['families'] if row['state'] == 'conditional']
    assert conditional, 'credit_display 这类条件性元素应被标为 conditional'
    assert all(row['family'] != 'prompt_input' for row in conditional)


def test_selector_probe_does_not_fail_idle_page_for_dialog_only_controls():
    """配置面板/落地页等只在别的页面状态存在的族，缺失不应让 L1 静止页自检失败。"""
    probe = probe_selectors(FakePage())
    contextual = {
        row['family']: row['state'] for row in probe['families']
        if row['family'] in {'config_panel_root', 'flow_entry_btn', 'credit_display'}
    }
    assert contextual == {
        'config_panel_root': 'conditional',
        'flow_entry_btn': 'conditional',
        'credit_display': 'conditional',
    }
    assert probe['summary']['missing'] == 0


def test_probed_families_are_all_consumed_by_production_code():
    """探针只该探生产代码真读的族。

    历史上 UI_SELECTORS 攒了一堆没有任何消费者的族（旧上传路径、旧错误横幅、
    甚至几张被当成选择器探测的文本关键词表），探针把它们一并报成"失效"，把
    面板变成了噪音。这条用例把"字典里的族"和"代码里读的族"钉在一起，防止再漂。
    """
    import re
    from pathlib import Path
    from integrations.google_fx.ui_selectors import UI_SELECTORS
    from integrations.google_fx.services.google_fx_diagnostics import _NON_SELECTOR_FAMILIES

    root = Path(__file__).resolve().parent.parent / 'integrations' / 'google_fx'
    sources = [p for p in root.rglob('*.py')
               if p.name not in {'ui_selectors.py', 'google_fx_diagnostics.py'}]
    blob = '\n'.join(p.read_text(encoding='utf-8', errors='ignore') for p in sources)

    probed = [f for f, v in UI_SELECTORS['google_fx'].items()
              if isinstance(v, (list, tuple)) and f not in _NON_SELECTOR_FAMILIES]
    orphans = [f for f in probed if not re.search(r'\b%s\b' % re.escape(f), blob)]
    assert not orphans, f'这些族没有任何生产消费者，应该删掉或接上：{orphans}'


# ── D2：失败取证 ─────────────────────────────────────────────────────────────

def test_capture_writes_screenshot_dom_and_meta(tmp_path, monkeypatch):
    monkeypatch.setattr(forensics, 'DEBUG_ROOT', tmp_path / 'fx_debug')
    page = FakePage()
    slot = forensics.capture(page, 'unit_test', '人为触发', bucket='task_x')
    assert slot is not None
    files = set(os.listdir(slot))
    assert {'screenshot.png', 'page.html', 'page.txt', 'meta.json'} <= files
    meta = json.loads((tmp_path / 'fx_debug' / 'task_x'
                       / os.path.basename(slot) / 'meta.json').read_text(encoding='utf-8'))
    assert meta['tag'] == 'unit_test' and meta['page'] == 'open'


def test_capture_survives_a_dead_page(tmp_path, monkeypatch):
    """浏览器已经关掉时取证不能抛异常——它是失败路径上的收尾动作。"""
    monkeypatch.setattr(forensics, 'DEBUG_ROOT', tmp_path / 'fx_debug')
    page = FakePage()
    page.close()
    slot = forensics.capture(page, 'dead_page', '浏览器已关闭', bucket='task_y')
    assert slot is not None
    assert 'screenshot.png' not in os.listdir(slot)


def test_capture_can_be_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(forensics, 'DEBUG_ROOT', tmp_path / 'fx_debug')
    monkeypatch.setenv('GOOGLE_FX_DEBUG_CAPTURE', '0')
    assert forensics.capture(FakePage(), 'off', '关闭时不该落盘') is None


def test_capture_file_resolution_blocks_path_traversal(tmp_path, monkeypatch):
    monkeypatch.setattr(forensics, 'DEBUG_ROOT', tmp_path / 'fx_debug')
    forensics.capture(FakePage(), 'trav', '', bucket='task_z')
    row = forensics.list_captures()[0]
    assert forensics.resolve_capture_file(row['id'], 'meta.json')
    assert forensics.resolve_capture_file(row['id'], '../../../../etc/passwd') is None
    assert forensics.resolve_capture_file('../../etc', 'passwd') is None


def test_capture_prunes_old_buckets(tmp_path, monkeypatch):
    monkeypatch.setattr(forensics, 'DEBUG_ROOT', tmp_path / 'fx_debug')
    monkeypatch.setattr(forensics, 'MAX_BUCKETS', 2)
    for index in range(4):
        forensics.capture(FakePage(), f'c{index}', '', bucket=f'task_{index}')
    assert len(os.listdir(tmp_path / 'fx_debug')) <= 2


def test_clear_captures_removes_all_directories(tmp_path, monkeypatch):
    debug_dir = tmp_path / 'fx_debug'
    monkeypatch.setattr(forensics, 'DEBUG_ROOT', debug_dir)
    forensics.capture(FakePage(), 'cap1', '', bucket='task_1')
    forensics.capture(FakePage(), 'cap2', '', bucket='task_2')
    assert len(forensics.list_captures()) == 2
    assert len(os.listdir(debug_dir)) == 2

    cleared = forensics.clear_captures()
    assert cleared == 2
    assert len(forensics.list_captures()) == 0
    assert len(os.listdir(debug_dir)) == 0


# ── D6：dry-run 不提交 ───────────────────────────────────────────────────────

def test_dry_run_short_circuits_the_single_submit_choke_point(monkeypatch):
    from integrations.google_fx.services import google_fx_helpers as helpers
    monkeypatch.setenv('FX_DRY_RUN', '1')
    page = FakePage()
    assert helpers.click_fx_send_button(page) is True
    generate = page.locator("button:has-text('Generate')")
    assert generate.count() == 1
    assert page._elements  # 页面没被改动
    clicked = [el for el in page._elements if el.get('clicks')]
    assert not clicked, 'dry-run 下不能真的点到任何按钮'


def test_dry_run_off_means_real_click_path_is_attempted(monkeypatch):
    from integrations.google_fx.services import google_fx_helpers as helpers
    monkeypatch.delenv('FX_DRY_RUN', raising=False)
    page = FakePage()
    helpers.click_fx_send_button(page)
    clicked = [el for el in page._elements if el.get('clicks')]
    assert clicked and 'Generate' in clicked[0]['text']


# ── 假 AdsPower：限频、坏端口、挂死 ──────────────────────────────────────────

def test_account_pool_paginates_against_fake_adspower():
    profiles = [{'user_id': f'u{i}', 'name': f'a{i}@x.com', 'group_name': 'g'}
                for i in range(250)]
    with FakeAdsPower(profiles=profiles) as fake:
        rows = ap.AccountPool().list_adspower_profiles(port=fake.port)
    assert len(rows) == 250, '必须翻完所有页，不能只取第一页'


def test_account_pool_retries_rate_limited_pages(monkeypatch):
    monkeypatch.setattr(ap.time, 'sleep', lambda _s: None)
    with FakeAdsPower(rate_limit_times=2) as fake:
        rows = ap.AccountPool().list_adspower_profiles(port=fake.port)
    assert len(rows) == 3, '限频是瞬时状态，退避重试后应拿到完整结果'


def test_get_ads_ws_url_fails_fast_on_dead_debug_port(monkeypatch):
    """AdsPower 说"启动成功"但调试端口没起来：必须报错，不能返回一个连不上的 ws。"""
    from integrations.google_fx.utils import browser
    monkeypatch.setattr(browser.time, 'sleep', lambda _s: None)
    with FakeAdsPower(dead_ws=True) as fake:
        with pytest.raises(Exception) as excinfo:
            browser.get_ads_ws_url(user_id='u0', port=str(fake.port),
                                   auto_rotate_proxy=False)
    assert 'AdsPower' in str(excinfo.value)


def test_dead_listener_accepts_but_never_answers():
    """S3 回归要用它：端口 connect 得通，但 HTTP 请求永远不返回。"""
    import socket as socket_module
    with DeadListener() as dead:
        with socket_module.create_connection(('127.0.0.1', dead.port), timeout=1):
            pass  # connect 成功 = 状态快照里那次端口探测会判"在线"
        sock = socket_module.create_connection(('127.0.0.1', dead.port), timeout=1)
        sock.sendall(b'GET / HTTP/1.0\r\n\r\n')
        sock.settimeout(0.4)
        with pytest.raises((socket_module.timeout, TimeoutError)):
            sock.recv(16)
        sock.close()
