import urllib.error
from unittest.mock import Mock, patch

import pytest
import prompt_pipeline as pp


def test_connection_error_identifies_actual_gateway():
    opener = Mock()
    opener.open.side_effect = urllib.error.URLError(ConnectionRefusedError(61, 'Connection refused'))
    with patch.object(pp, 'resolve_gateway', return_value=('http://127.0.0.1:52692/v1', 'test-key')), \
            patch.object(pp.urllib.request, 'build_opener', return_value=opener), \
            patch.object(pp, '_interruptible_sleep') as sleep:
        with pytest.raises(RuntimeError) as error:
            pp._chat({'model': 'gpt-6-astra'}, 'test', 'test')
    message = str(error.value)
    assert '52692' in message
    assert 'gpt-6-astra' in message
    assert 'codexBaseUrl' in message
    assert '8046' not in message
    assert 'test-key' not in message
    assert opener.open.call_count == 3
    assert [call.args[0] for call in sleep.call_args_list] == [5, 10]


def test_refused_connection_recovers_without_placeholder():
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.read.return_value = b'{"choices":[{"message":{"content":"real prompt"}}]}'
    opener = Mock()
    opener.open.side_effect = [urllib.error.URLError(ConnectionRefusedError(61, 'refused')), response]
    with patch.object(pp.urllib.request, 'build_opener', return_value=opener), \
            patch.object(pp, '_interruptible_sleep') as sleep:
        assert pp._chat({'model': 'test'}, 'test', 'test') == 'real prompt'
    sleep.assert_called_once_with(5)
    assert opener.open.call_count == 2


def test_timeout_is_not_replayed_by_transport():
    opener = Mock()
    opener.open.side_effect = TimeoutError('timed out')
    with patch.object(pp.urllib.request, 'build_opener', return_value=opener), \
            patch.object(pp, '_interruptible_sleep') as sleep:
        with pytest.raises(RuntimeError, match='timed out'):
            pp._chat({'model': 'test'}, 'test', 'test')
    assert opener.open.call_count == 1
    sleep.assert_not_called()
