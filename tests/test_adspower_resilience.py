# -*- coding: utf-8 -*-
import pytest
import requests

from integrations.google_fx.utils import browser


def test_get_ads_ws_url_reuses_running_browser_on_conn_error(monkeypatch, tmp_path):
    """当 AdsPower Local API 端口完全连不上 (ConnectionError) 时，如果有正在运行的浏览器，应直接复用。"""
    user_id = "test_user_active"
    cache_dir = tmp_path / f"{user_id}_profile"
    cache_dir.mkdir(parents=True)
    port_file = cache_dir / "DevToolsActivePort"
    port_file.write_text("9222\n/devtools/browser/abc-123\n")

    monkeypatch.setattr(browser.glob, "glob", lambda pat: [str(port_file)])
    monkeypatch.setattr(browser, "_is_ws_port_open", lambda ws, *args, **kwargs: True)

    def mock_get(url, *args, **kwargs):
        raise requests.exceptions.ConnectionError("Connection refused")

    monkeypatch.setattr("requests.get", mock_get)

    ws = browser.get_ads_ws_url(user_id=user_id, port=50325, auto_rotate_proxy=False)
    assert ws == "ws://127.0.0.1:9222/devtools/browser/abc-123"


def test_get_ads_ws_url_provides_clear_error_when_adspower_down(monkeypatch):
    """当 AdsPower 未启动且无法自愈拉起时，应抛出包含明确指引的异常。"""
    monkeypatch.setattr(browser, "_find_running_browser_ws", lambda uid: None)
    monkeypatch.setattr(browser, "_try_revive_adspower", lambda port: False)

    def mock_get(url, *args, **kwargs):
        raise requests.exceptions.ConnectionError("Connection refused")

    monkeypatch.setattr("requests.get", mock_get)

    with pytest.raises(Exception) as exc_info:
        browser.get_ads_ws_url(user_id="offline_user", port=50325, auto_rotate_proxy=False)

    msg = str(exc_info.value)
    assert "50325" in msg
