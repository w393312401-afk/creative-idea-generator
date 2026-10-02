"""Cockpit's explicitly available image tools adapt without losing edit inputs.

All capability discovery and HTTP transports are mocked. These tests never call
an image service, load local account settings, or spend image-generation quota.
"""
import base64
import io
import json
import urllib.request
from unittest.mock import Mock

import pytest

import cockpit_image_responses as adapter


FLARE = 'gpt-image-2.5-flare'
BASE_URL = 'http://127.0.0.1:12345/v1'
API_KEY = 'synthetic-cockpit-key'


@pytest.fixture(autouse=True)
def offline_transport(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail('Responses adapter test attempted an unmocked HTTP request')

    monkeypatch.setattr(urllib.request, 'urlopen', denied)
    monkeypatch.setattr(urllib.request, 'build_opener', denied)


@pytest.fixture
def available_tool(monkeypatch):
    capability = Mock(return_value=[FLARE])
    monkeypatch.setattr(adapter, 'cockpit_image_responses_models', capability)
    return capability


def json_request(payload, *, path='/images/generations', base_url=BASE_URL,
                 method='POST'):
    return urllib.request.Request(
        base_url + path, json.dumps(payload).encode(),
        headers={'Authorization': f'Bearer {API_KEY}',
                 'Content-Type': 'application/json', 'X-Request-Id': 'offline-test'},
        method=method,
    )


def multipart_request(fields, files):
    boundary = 'offline-cockpit-boundary'
    parts = []
    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"'
            f'\r\n\r\n{value}\r\n'.encode()
        )
    for name, filename, content_type, data in files:
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
            f'filename="{filename}"\r\nContent-Type: {content_type}\r\n\r\n'.encode()
            + data + b'\r\n'
        )
    parts.append(f'--{boundary}--\r\n'.encode())
    return urllib.request.Request(
        BASE_URL + '/images/edits', b''.join(parts),
        headers={'Authorization': f'Bearer {API_KEY}',
                 'Content-Type': f'multipart/form-data; boundary={boundary}'},
        method='POST',
    )


def response_payload(*outputs, status='completed', **extra):
    return json.dumps({'status': status, 'output': list(outputs), **extra}).encode()


def image_output(result=None, **extra):
    if result is None:
        result = base64.b64encode(b'offline-generated-image').decode()
    return {'type': 'image_generation_call', 'status': 'completed',
            'result': result, **extra}


def converted_payload(request):
    converted, normalize = adapter.adapt_cockpit_image_request(request)
    assert converted.full_url == BASE_URL + '/responses'
    assert converted.get_method() == 'POST'
    assert converted.get_header('Authorization') == f'Bearer {API_KEY}'
    assert converted.get_header('Content-type') == 'application/json'
    assert callable(normalize)
    return json.loads(converted.data), normalize


def test_generation_uses_exact_tool_model_and_native_parameters(available_tool):
    original = json_request({
        'model': FLARE, 'prompt': '把花园改为夜景', 'n': 1,
        'size': '1152x2048', 'quality': 'high', 'background': 'transparent',
        'output_format': 'webp', 'output_compression': 75,
        'moderation': 'low', 'response_format': 'b64_json',
    })
    original_body = original.data
    payload, _ = converted_payload(original)
    available_tool.assert_called_once_with(BASE_URL, API_KEY)
    assert payload['model'] == 'gpt-5.5'
    assert payload['stream'] is False
    assert payload['tool_choice'] == {'type': 'image_generation'}
    assert payload['input'] == [{
        'role': 'user', 'content': [{'type': 'input_text', 'text': '把花园改为夜景'}],
    }]
    assert payload['tools'] == [{
        'type': 'image_generation', 'model': FLARE, 'action': 'generate',
        'size': '1152x2048', 'quality': 'high', 'background': 'transparent',
        'output_format': 'webp', 'output_compression': 75, 'moderation': 'low',
    }]
    assert original.data == original_body
    assert original.full_url == BASE_URL + '/images/generations'


def test_date_variant_is_not_replaced_by_undated_flare(available_tool):
    model = FLARE + '-2026-09-08'
    available_tool.return_value = [model]
    payload, _ = converted_payload(json_request({'model': model, 'prompt': 'A lake'}))
    assert payload['tools'][0]['model'] == model


@pytest.mark.parametrize('mask', [
    'https://example.test/mask.png',
    {'image_url': 'https://example.test/mask.png'},
    {'url': 'https://example.test/mask.png'},
])
def test_json_edit_preserves_all_references_and_mask(available_tool, mask):
    references = [
        'data:image/png;base64,' + base64.b64encode(b'first-reference').decode(),
        'https://example.test/second-reference.webp',
    ]
    payload, _ = converted_payload(json_request({
        'model': FLARE, 'prompt': 'Keep both subjects and change the sky',
        'image': references, 'mask': mask, 'quality': 'auto', 'size': '1536x1024',
    }, path='/images/edits'))
    assert payload['input'][0]['content'] == [
        {'type': 'input_text', 'text': 'Keep both subjects and change the sky'},
        *[{'type': 'input_image', 'image_url': image} for image in references],
    ]
    assert payload['tools'][0] == {
        'type': 'image_generation', 'model': FLARE, 'action': 'edit',
        'quality': 'auto', 'size': '1536x1024',
        'input_image_mask': {'image_url': 'https://example.test/mask.png'},
    }


def test_json_edit_accepts_images_objects_and_file_references(available_tool):
    reference = 'data:image/png;base64,' + base64.b64encode(b'reference').decode()
    payload, _ = converted_payload(json_request({
        'model': FLARE, 'prompt': 'Keep both subjects',
        'images': [{'image_url': reference}, {'file_id': 'file-offline-reference'}],
        'mask': {'file_id': 'file-offline-mask'},
    }, path='/images/edits'))
    assert payload['input'][0]['content'] == [
        {'type': 'input_text', 'text': 'Keep both subjects'},
        {'type': 'input_image', 'image_url': reference},
        {'type': 'input_image', 'file_id': 'file-offline-reference'},
    ]
    assert payload['tools'][0]['input_image_mask'] == {'file_id': 'file-offline-mask'}


def test_multipart_edit_preserves_binary_references_and_mask(available_tool):
    first = b'\x89PNG\r\n\x1a\n\x00\xfffirst\r\n'
    second = b'RIFF\x00\xfeWEBPsecond\x00'
    mask = b'\x89PNG\r\nmask\x00\xff\r\n'
    original = multipart_request({
        'model': FLARE, 'prompt': 'Change only the masked area',
        'n': '1', 'size': '1536x1024', 'quality': 'auto',
    }, [
        ('image', 'first.png', 'image/png', first),
        ('image[]', 'second.webp', 'image/webp', second),
        ('mask', 'mask.png', 'image/png', mask),
    ])
    original_body = original.data
    payload, _ = converted_payload(original)
    assert payload['input'][0]['content'] == [
        {'type': 'input_text', 'text': 'Change only the masked area'},
        {'type': 'input_image', 'image_url': 'data:image/png;base64,'
         + base64.b64encode(first).decode()},
        {'type': 'input_image', 'image_url': 'data:image/webp;base64,'
         + base64.b64encode(second).decode()},
    ]
    assert payload['tools'][0]['input_image_mask'] == {
        'image_url': 'data:image/png;base64,' + base64.b64encode(mask).decode(),
    }
    assert payload['tools'][0]['action'] == 'edit'
    assert original.data == original_body


@pytest.mark.parametrize('unsupported', [
    {'n': 2}, {'response_format': 'url'}, {'user': 'external-user'},
    {'seed': 42}, {'image_size': '4K'}, {'style': 'vivid'},
])
def test_unsupported_generation_parameters_fail_before_transport(available_tool, unsupported):
    with pytest.raises(ValueError):
        adapter.adapt_cockpit_image_request(json_request({
            'model': FLARE, 'prompt': 'A lake', **unsupported,
        }))


def test_unsupported_multipart_fields_are_not_silently_dropped(available_tool):
    request = multipart_request({
        'model': FLARE, 'prompt': 'Change the sky', 'unsupported_option': 'value',
    }, [('image', 'ref.png', 'image/png', b'offline-reference')])
    with pytest.raises(ValueError):
        adapter.adapt_cockpit_image_request(request)


@pytest.mark.parametrize('available_models,model', [
    ([], FLARE),
    (['gpt-image-2.5-sunburst'], FLARE),
    ([FLARE], 'gpt-image-2.5'),
    ([FLARE], 'gpt-image-2.5-sunburst'),
    ([FLARE], 'gemini-3.1-flash-image'),
])
def test_unknown_or_native_model_passes_through_without_changes(
        available_tool, available_models, model):
    available_tool.return_value = available_models
    request = json_request({'model': model, 'prompt': 'A lake', 'native_option': True})
    original_body = request.data
    adapted, normalize = adapter.adapt_cockpit_image_request(request)
    assert adapted is request
    assert normalize is None
    assert adapted.data == original_body


@pytest.mark.parametrize('model,content_type,image', [
    ('gpt-image-2.5', 'application/octet-stream', b'native-image-data'),
    ('gpt-image-2.5-sunburst', 'image/png', b''),
])
def test_native_multipart_parts_are_not_validated_by_responses_adapter(
        available_tool, model, content_type, image):
    request = multipart_request({'model': model, 'prompt': 'A lake'}, [
        ('image', 'reference.png', content_type, image),
    ])
    adapted, normalize = adapter.adapt_cockpit_image_request(request)
    assert adapted is request
    assert normalize is None


@pytest.mark.parametrize('path,method', [
    ('/chat/completions', 'POST'), ('/responses', 'POST'),
    ('/images/generations', 'GET'),
])
def test_non_image_post_requests_pass_through(available_tool, path, method):
    request = json_request({'model': FLARE, 'prompt': 'A lake'}, path=path, method=method)
    adapted, normalize = adapter.adapt_cockpit_image_request(request)
    assert adapted is request
    assert normalize is None


def test_remote_gateway_without_capability_passes_through(available_tool):
    available_tool.return_value = []
    request = json_request({'model': FLARE, 'prompt': 'A lake'},
                           base_url='https://remote.example.test/v1')
    adapted, normalize = adapter.adapt_cockpit_image_request(request)
    assert adapted is request
    assert normalize is None


def test_normalization_preserves_images_revised_prompts_and_tool_metadata(available_tool):
    _, normalize = converted_payload(json_request({'model': FLARE, 'prompt': 'A lake'}))
    first = image_output(revised_prompt='A lake at dusk', model=FLARE,
                         size='1024x1536', quality='high', output_format='png')
    second = image_output(base64.b64encode(b'second-generated-image').decode())
    result = json.loads(normalize(response_payload(
        {'type': 'message', 'content': []}, first, second,
    )))
    assert [row['b64_json'] for row in result['data']] == [first['result'], second['result']]
    assert result['data'][0]['revised_prompt'] == first['revised_prompt']
    field_sources = [result, result.get('metadata', {}), result['data'][0]]
    for field in ('model', 'size', 'quality', 'output_format'):
        assert any(source.get(field) == first[field] for source in field_sources)


@pytest.mark.parametrize('raw_response', [
    b'not-json',
    b'[]',
    response_payload(),
    response_payload({'type': 'message', 'content': []}),
    response_payload(image_output('')),
    response_payload(image_output('not valid base64 !')),
    response_payload(image_output('AAAAA')),
    response_payload(image_output('非图片数据')),
    response_payload(image_output(), status='incomplete'),
    response_payload(image_output(), status='failed', error={'message': 'Tool failed'}),
    response_payload(image_output(), error={'message': 'Tool failed'}),
])
def test_invalid_or_incomplete_outputs_raise_instead_of_creating_empty_images(
        available_tool, raw_response):
    _, normalize = converted_payload(json_request({'model': FLARE, 'prompt': 'A lake'}))
    with pytest.raises(ValueError):
        normalize(raw_response)


def test_executor_does_not_retry_a_completed_paid_request_with_invalid_output(
        available_tool, monkeypatch):
    import frame_generator

    opener = Mock()
    opener.open.side_effect = lambda *args, **kwargs: io.BytesIO(response_payload())
    monkeypatch.setattr(frame_generator, 'log', Mock())
    monkeypatch.setattr(frame_generator, '_emit_upstream_failure', Mock())
    monkeypatch.setattr(frame_generator, '_interruptible_sleep',
                        Mock(side_effect=AssertionError('paid request was retried')))
    with pytest.raises(ValueError):
        frame_generator._execute_request_with_retry(
            json_request({'model': FLARE, 'prompt': 'A lake'}),
            opener=opener, max_attempts=3,
        )
    assert opener.open.call_count == 1
    assert opener.open.call_args.args[0].full_url == BASE_URL + '/responses'


@pytest.mark.parametrize('actual_model', [
    'gpt-image-2.5-sunburst', 'gpt-image-2-codex',
])
def test_executor_rejects_reported_tool_model_mismatch_without_retry(
        available_tool, monkeypatch, actual_model):
    import frame_generator

    opener = Mock()
    opener.open.side_effect = lambda *args, **kwargs: io.BytesIO(
        response_payload(image_output(model=actual_model)))
    monkeypatch.setattr(frame_generator, 'log', Mock())
    monkeypatch.setattr(frame_generator, '_emit_upstream_failure', Mock())
    monkeypatch.setattr(frame_generator, '_interruptible_sleep',
                        Mock(side_effect=AssertionError('model mismatch was retried')))
    with pytest.raises(adapter.ImageResponsesOutputError):
        frame_generator._execute_request_with_retry(
            json_request({'model': FLARE, 'prompt': 'A lake'}),
            opener=opener, max_attempts=3,
        )
    assert opener.open.call_count == 1
    assert opener.open.call_args.args[0].full_url == BASE_URL + '/responses'


def test_completed_image_without_reported_tool_model_is_accepted(available_tool):
    _, normalize = converted_payload(json_request({'model': FLARE, 'prompt': 'A lake'}))
    output = image_output()
    assert 'model' not in output
    result = json.loads(normalize(response_payload(output)))
    assert result['data'] == [{'b64_json': output['result']}]


def test_executor_validates_unsupported_parameters_before_sending(available_tool, monkeypatch):
    import frame_generator

    opener = Mock()
    monkeypatch.setattr(frame_generator, 'log', Mock())
    with pytest.raises(ValueError):
        frame_generator._execute_request_with_retry(
            json_request({'model': FLARE, 'prompt': 'A lake', 'n': 2}),
            opener=opener, max_attempts=3,
        )
    opener.open.assert_not_called()


def test_frame_edit_does_not_rerender_after_a_paid_request_returns_no_image(
        available_tool, monkeypatch, tmp_path):
    import frame_generator
    from PIL import Image

    reference = tmp_path / 'reference.png'
    Image.new('RGB', (64, 64), 'green').save(reference)
    opener = Mock()
    opener.open.side_effect = lambda *args, **kwargs: io.BytesIO(response_payload())
    monkeypatch.setattr(frame_generator, 'resolve_gateway', lambda *args: (BASE_URL, API_KEY))
    monkeypatch.setattr(urllib.request, 'build_opener', Mock(return_value=opener))
    monkeypatch.setattr(frame_generator, 'log', Mock())
    monkeypatch.setattr(frame_generator, '_emit_upstream_failure', Mock())
    monkeypatch.setattr(frame_generator.time, 'sleep', Mock())
    with pytest.raises(adapter.ImageResponsesOutputError):
        frame_generator._generate_image_edit(
            {'imageModel': FLARE, 'imageAspectRatio': '9:16', 'imageQuality': '2K'},
            'Change the sky', str(reference), str(tmp_path / 'edit.webp'),
        )
    assert opener.open.call_count == 1
    assert opener.open.call_args.args[0].full_url == BASE_URL + '/responses'


def test_frame_edit_does_not_retry_unrepresentable_responses_parameters(
        available_tool, monkeypatch, tmp_path):
    import frame_generator
    from PIL import Image

    reference = tmp_path / 'reference.png'
    Image.new('RGB', (64, 64), 'green').save(reference)
    execute = Mock(side_effect=adapter.ImageResponsesParameterError('Unsupported image parameter'))
    monkeypatch.setattr(frame_generator, 'resolve_gateway', lambda *args: (BASE_URL, API_KEY))
    monkeypatch.setattr(urllib.request, 'build_opener', Mock())
    monkeypatch.setattr(frame_generator, '_execute_request_with_retry', execute)
    monkeypatch.setattr(frame_generator, 'log', Mock())
    monkeypatch.setattr(frame_generator.time, 'sleep', Mock())
    with pytest.raises(adapter.ImageResponsesParameterError):
        frame_generator._generate_image_edit(
            {'imageModel': FLARE, 'imageAspectRatio': '9:16'},
            'Change the sky', str(reference), str(tmp_path / 'edit.webp'),
        )
    assert execute.call_count == 1
