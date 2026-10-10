"""Exercise the installed SOCKS transport, not only mocked rotation results."""

from types import SimpleNamespace

import pytest
import requests

from integrations.google_fx.utils.proxy_rotator import ProxyRotator


def test_installed_requests_can_construct_socks_transport():
    # Construction does not connect, but uses the same optional dependency as
    # Session.get. Plain requests without the socks extra fails here.
    adapter = requests.adapters.HTTPAdapter()
    try:
        manager = adapter.proxy_manager_for("socks5h://127.0.0.1:1")
        assert manager.connection_from_url("https://ipinfo.io/json") is not None
    finally:
        adapter.close()


def test_missing_socks_dependency_reports_actionable_error_without_credentials(monkeypatch):
    config = {
        "proxy_type": "socks5", "proxy_host": "proxy.invalid", "proxy_port": "1080",
        "proxy_user": "private-user", "proxy_password": "private-password",
    }

    def fail(*args, **kwargs):
        raise requests.exceptions.InvalidSchema(
            "Missing dependencies for SOCKS support: socks5://private-user:private-password@proxy.invalid"
        )

    monkeypatch.setattr(requests.Session, "get", fail)
    with pytest.raises(RuntimeError, match="PySocks") as caught:
        ProxyRotator._probe_proxy_ip(config)
    assert "private-user" not in str(caught.value)
    assert "private-password" not in str(caught.value)

    rotator = ProxyRotator()
    monkeypatch.setattr(rotator, "_read_profile_proxy", lambda *args: config)
    result = rotator.rotate_proxy_verified("test-profile")
    assert not result["success"]
    assert "PySocks" in result["message"]
    assert "private-user" not in str(result)
    assert "private-password" not in str(result)


def test_probe_checks_explicit_socks_route_without_environment_proxy(monkeypatch):
    observed = {}

    def get(session, url, **kwargs):
        observed.update(trust_env=session.trust_env, **kwargs)
        return SimpleNamespace(raise_for_status=lambda: None,
                               json=lambda: {"ip": "203.0.113.7"})

    monkeypatch.setattr(requests.Session, "get", get)
    config = {"proxy_type": "socks5", "proxy_host": "127.0.0.1", "proxy_port": "1080"}
    assert ProxyRotator._probe_proxy_ip(config) == "203.0.113.7"
    assert observed["trust_env"] is False
    assert observed["proxies"] == {
        "http": "socks5h://127.0.0.1:1080", "https": "socks5h://127.0.0.1:1080",
    }
    assert config["proxy_type"] == "socks5"
