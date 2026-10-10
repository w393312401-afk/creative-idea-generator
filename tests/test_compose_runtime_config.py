import pytest
import server_common as sc


@pytest.mark.parametrize('managed', [True, False])
def test_server_compose_budget_overrides_stale_browser_defaults(monkeypatch, managed):
    monkeypatch.setattr(sc, 'SERVER_MANAGED', managed)
    monkeypatch.setattr(sc, 'SERVER_CONFIG', {
        'composeRequestTimeoutSeconds': 120, 'composeBatchSize': 3,
        'composeBatchRetryCount': 0,
    })
    client = {'composeRequestTimeoutSeconds': 45, 'composeBatchSize': 5,
              'composeBatchRetryCount': 1}
    config = sc.effective_config(client)
    assert config['composeRequestTimeoutSeconds'] == 120
    assert config['composeBatchSize'] == 3
    assert config['composeBatchRetryCount'] == 0
    assert client['composeRequestTimeoutSeconds'] == 45


@pytest.mark.parametrize('managed', [True, False])
def test_client_compose_budget_used_without_server_override(monkeypatch, managed):
    monkeypatch.setattr(sc, 'SERVER_MANAGED', managed)
    monkeypatch.setattr(sc, 'SERVER_CONFIG', {})
    config = sc.effective_config({'composeRequestTimeoutSeconds': 90})
    assert config['composeRequestTimeoutSeconds'] == 90


@pytest.mark.parametrize('managed', [True, False])
def test_server_compose_budget_used_without_client_config(monkeypatch, managed):
    monkeypatch.setattr(sc, 'SERVER_MANAGED', managed)
    monkeypatch.setattr(sc, 'SERVER_CONFIG', {'composeRequestTimeoutSeconds': 120})
    assert sc.effective_config(None)['composeRequestTimeoutSeconds'] == 120
