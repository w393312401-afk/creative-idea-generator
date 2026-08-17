# -*- coding: utf-8 -*-
import json
import urllib.parse
import pytest
from unittest.mock import patch, MagicMock

from integrations.google_fx.config import (
    get_runtime_adspower_silent_mode,
    get_runtime_adspower_window_position,
    get_runtime_adspower_window_size,
    get_runtime_adspower_headless,
)
from integrations.google_fx.utils.browser import (
    build_adspower_launch_args,
    get_ads_ws_url,
)


def test_build_adspower_launch_args_silent():
    encoded, args = build_adspower_launch_args(silent=True)
    assert any("--window-position=" in a for a in args)
    assert any("--window-size=" in a for a in args)
    assert any("CalculateNativeWinOcclusion" in a for a in args)
    assert any("--disable-backgrounding-occluded-windows" in a for a in args)
    assert any("--disable-renderer-backgrounding" in a for a in args)
    assert "--mute-audio" in args
    assert "--no-first-run" in args

    # 验证 URL 编码能够被正确反解析为 JSON
    decoded = json.loads(urllib.parse.unquote(encoded))
    assert decoded == args


def test_build_adspower_launch_args_non_silent():
    encoded, args = build_adspower_launch_args(silent=False)
    assert not any("--window-position=" in a for a in args)
    assert not any("--window-size=" in a for a in args)
    assert "--mute-audio" in args


def test_runtime_config_env_overrides(monkeypatch):
    monkeypatch.setenv("ADSPOWER_SILENT_MODE", "0")
    monkeypatch.setenv("ADSPOWER_WINDOW_POSITION", "2000,2000")
    monkeypatch.setenv("ADSPOWER_WINDOW_SIZE", "1920,1080")
    monkeypatch.setenv("ADSPOWER_HEADLESS", "1")

    assert get_runtime_adspower_silent_mode() is False
    assert get_runtime_adspower_window_position() == "2000,2000"
    assert get_runtime_adspower_window_size() == "1920,1080"
    assert get_runtime_adspower_headless() is True


def test_get_ads_ws_url_injects_silent_launch_args(monkeypatch):
    captured_urls = []

    def mock_get(url, *args, **kwargs):
        captured_urls.append(url)
        resp = MagicMock()
        resp.json.return_value = {
            "code": 0,
            "data": {"ws": {"puppeteer": "ws://127.0.0.1:9222/devtools"}}
        }
        return resp

    monkeypatch.setattr("requests.get", mock_get)
    monkeypatch.setattr("integrations.google_fx.utils.browser._is_ws_port_open", lambda ws: True)
    monkeypatch.setenv("ADSPOWER_SILENT_MODE", "1")

    ws = get_ads_ws_url(user_id="test_user", port=50325, auto_rotate_proxy=False)
    assert ws == "ws://127.0.0.1:9222/devtools"
    assert len(captured_urls) == 1
    start_url = captured_urls[0]

    assert "launch_args=" in start_url
    # 解析 query string
    parsed = urllib.parse.urlparse(start_url)
    qs = urllib.parse.parse_qs(parsed.query)
    launch_args_list = json.loads(qs["launch_args"][0])
    assert any("--window-position=" in a for a in launch_args_list)
    assert any("--disable-backgrounding-occluded-windows" in a for a in launch_args_list)


def test_console_schema_and_direct_env_for_silent_mode(monkeypatch):
    import os
    import fx_console
    schema = fx_console.FX_CONFIG_SPEC
    assert "adsPowerSilentMode" in schema
    assert schema["adsPowerSilentMode"]["group"] == "连接"
    assert schema["adsPowerSilentMode"]["type"] == "bool"
    assert schema["adsPowerSilentMode"]["hot"] is True
    assert schema["adsPowerSilentMode"]["env"] == "ADSPOWER_SILENT_MODE"

    clean = fx_console.validate_patch({"adsPowerSilentMode": False})
    assert clean["adsPowerSilentMode"] is False

    fx_console.apply_direct_env(clean)
    assert os.environ["ADSPOWER_SILENT_MODE"] == "0"

    clean_true = fx_console.validate_patch({"adsPowerSilentMode": True})
    fx_console.apply_direct_env(clean_true)
    assert os.environ["ADSPOWER_SILENT_MODE"] == "1"

