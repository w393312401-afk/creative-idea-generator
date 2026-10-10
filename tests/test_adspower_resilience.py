# -*- coding: utf-8 -*-
import pytest
import requests

from integrations.google_fx.utils import browser


def test_get_ads_ws_url_reuses_running_browser_on_conn_error(monkeypatch, tmp_path, ads_inventory_reader):
    """清单确认仅一个环境后，active API故障仍可从缓存复用目标。"""
    user_id = "test_user_active"
    cache_dir = tmp_path / f"{user_id}_profile"
    cache_dir.mkdir(parents=True)
    port_file = cache_dir / "DevToolsActivePort"
    port_file.write_text("9222\n/devtools/browser/abc-123\n")

    # The cache root must also be synthetic: mocking glob alone still depends
    # on a real ~/.ADSPOWER_GLOBAL/cache directory existing on the developer's
    # machine. Never inspect or manipulate a real browser in this unit test.
    monkeypatch.setattr(browser.os.path, "expanduser", lambda path: str(tmp_path))
    monkeypatch.setattr(browser.glob, "glob", lambda pat: [str(port_file)])
    monkeypatch.setattr(browser, "_is_ws_port_open", lambda ws, *args, **kwargs: True)
    monkeypatch.setattr(browser, "_macos_frontmost_app", lambda: "")
    monkeypatch.setattr(browser, "suppress_browser_window", lambda *args, **kwargs: False)

    def mock_get(url, *args, **kwargs):
        if url.endswith('/browser/local-active'):
            from types import SimpleNamespace
            return SimpleNamespace(json=lambda: {'code': 0, 'data': {'list': [{'user_id': user_id}]}})
        raise requests.exceptions.ConnectionError("Connection refused")

    monkeypatch.setattr("requests.get", mock_get)

    ws = browser.get_ads_ws_url(user_id=user_id, port=50325, auto_rotate_proxy=False)
    assert ws == "ws://127.0.0.1:9222/devtools/browser/abc-123"


def test_get_ads_ws_url_provides_clear_error_when_adspower_down(monkeypatch, ads_inventory_reader):
    """当 AdsPower 未启动且无法自愈拉起时，应抛出包含明确指引的异常。"""
    monkeypatch.setattr(browser, "_find_running_browser_ws", lambda uid: None)
    monkeypatch.setattr(browser, "_try_revive_adspower", lambda port: False)
    monkeypatch.setattr(browser, "_macos_frontmost_app", lambda: "")
    monkeypatch.setattr(browser, "suppress_browser_window", lambda *args, **kwargs: False)

    def mock_get(url, *args, **kwargs):
        raise requests.exceptions.ConnectionError("Connection refused")

    monkeypatch.setattr("requests.get", mock_get)

    with pytest.raises(Exception) as exc_info:
        browser.get_ads_ws_url(user_id="offline_user", port=50325, auto_rotate_proxy=False)

    msg = str(exc_info.value)
    assert "50325" in msg
