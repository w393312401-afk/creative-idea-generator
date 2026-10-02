"""Image HTTP failures retain useful upstream details without exposing credentials."""
import io
import json
import urllib.error
import urllib.request
from unittest.mock import Mock, patch

import pytest

import frame_generator
from frame_generator import _execute_request_with_retry, QuotaExhaustedError


def _request_and_opener(body, code=400):
    request = urllib.request.Request(
        'http://image.test/v1/images/generations', data=b'{}',
        headers={'Authorization': 'Bearer synthetic-secret-123456'}, method='POST')
    error = urllib.error.HTTPError(request.full_url, code, 'Bad Request', {},
                                   io.BytesIO(body.encode()))
    opener = Mock()
    opener.open.side_effect = error
    return request, opener


def test_bad_request_preserves_error_type_message_and_readable_body():
    message = 'model gpt-image-2.5-sunburst is not supported. Use gpt-image-2.5.'
    request, opener = _request_and_opener(json.dumps({'error': {'message': message,
                                                              'code': 'invalid_request'}}))
    with pytest.raises(urllib.error.HTTPError) as caught:
        _execute_request_with_retry(request, opener=opener)
    error = caught.value
    assert error.code == 400
    assert message in str(error)
    assert json.loads(error.read())['error']['message'] == message
    assert message in error.upstream_detail
    assert opener.open.call_count == 1


def test_error_details_exclude_echoed_config_and_authorization_from_logs_and_exception():
    message = ('Unsupported model. Authorization: Bearer synthetic-secret-123456; '
               'config: {"private_setting": "do-not-display", "apiKey": "other-secret"}')
    body = {'error': {'message': message, 'code': 'invalid_request',
                      'config': {'private_setting': 'do-not-display'},
                      'headers': {'Authorization': 'Bearer another-private-token'}},
            'config': {'apiKey': 'unrelated-private-token'}}
    request, opener = _request_and_opener(json.dumps(body))
    with patch('frame_generator.log') as log, pytest.raises(urllib.error.HTTPError) as caught:
        _execute_request_with_retry(request, opener=opener)
    text = str(caught.value) + caught.value.read().decode() + str(log.call_args_list)
    assert 'Unsupported model' in text
    for private in ('synthetic-secret-123456', 'other-secret', 'do-not-display',
                    'another-private-token', 'unrelated-private-token', 'private_setting'):
        assert private not in text


def test_plain_text_error_redacts_credentials_before_preserving_body():
    request, opener = _request_and_opener('Unsupported model; apiKey=synthetic-secret-123456')
    with pytest.raises(urllib.error.HTTPError) as caught:
        _execute_request_with_retry(request, opener=opener)
    text = str(caught.value) + caught.value.read().decode()
    assert 'Unsupported model' in text
    assert 'synthetic-secret-123456' not in text


@pytest.mark.parametrize('worker', ('generation', 'edit'))
def test_image_studio_final_error_still_reads_the_upstream_reason(worker):
    message = 'This image model is not supported by the configured gateway.'
    _, opener = _request_and_opener(json.dumps({'error': {'message': message}}))
    task_id = 'test-image-upstream-detail'
    tasks = {task_id: {'status': 'pending'}}
    try:
        with patch('frame_generator.IMAGE_TASKS', tasks), \
             patch('frame_generator.urllib.request.build_opener', return_value=opener):
            if worker == 'generation':
                frame_generator._run_async_image_generation(
                    task_id, 'http://image.test/v1', 'test-key', {'model': 'gpt-image-2.5'})
            else:
                frame_generator._run_async_image_edit(
                    task_id, 'http://image.test/v1', 'test-key', b'', 'test-boundary')
    finally:
        frame_generator.set_upstream_event_sink(None)
    assert tasks[task_id]['status'] == 'failed'
    assert message in tasks[task_id]['error']


def test_json_quota_code_still_fails_on_the_first_attempt():
    request, opener = _request_and_opener(json.dumps({'error': {
        'message': 'No capacity left for this model.', 'code': 'RESOURCE_EXHAUSTED'}}), code=502)
    with pytest.raises(QuotaExhaustedError):
        _execute_request_with_retry(request, opener=opener)
    assert opener.open.call_count == 1
