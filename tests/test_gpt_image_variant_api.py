"""Image Studio dispatch keeps GPT variants and their native API parameters."""
import io
import json
from email.message import Message
from email.parser import BytesParser
from email.policy import default

import pytest

import server


@pytest.fixture
def dispatch(monkeypatch):
    calls = []

    class RecordedThread:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def start(self):
            calls.append(self.kwargs)

    monkeypatch.setattr(server.threading, 'Thread', RecordedThread)
    monkeypatch.setattr(server, 'IMAGE_TASKS', {})
    monkeypatch.setattr(server, 'access_ok', lambda handler: True)
    monkeypatch.setattr(server, 'effective_config', lambda config: dict(config or {}))
    monkeypatch.setattr(server, 'resolve_gateway', lambda model, config: ('http://gateway.test/v1', 'test-key'))
    return calls


def post(path, data, content_type='application/json'):
    handler = object.__new__(server.SparkRequestHandler)
    handler.path, handler.command = path, 'POST'
    handler.headers = Message()
    handler.headers['Content-Type'] = content_type
    handler.headers['Content-Length'] = str(len(data))
    handler.rfile = io.BytesIO(data)
    responses = []
    handler._send_json = lambda payload, status=200: responses.append((status, payload))
    handler.do_POST()
    assert responses[-1][0] == 200, responses[-1]
    return responses[-1][1]


@pytest.mark.parametrize('model,quality', [
    ('gpt-image-2.5-sunburst', 'max'),
    ('gpt-image-2.5-flare', 'auto'),
    ('gpt-image-2.5-sunburst-2026-09-08', 'max'),
    ('gpt-image-2.5-flare-2026-09-08', 'auto'),
])
def test_generation_preserves_variant_with_native_parameters(dispatch, model, quality):
    response = post('/api/image/generations', json.dumps({
        'model': model, 'prompt': 'A quiet garden', 'size': '9:16',
        'quality': '4K', 'image_size': '4K', 'response_format': 'b64_json',
    }).encode())
    assert response['status'] == 'pending'
    assert len(dispatch) == 1
    assert dispatch[0]['args'][3] == {
        'model': model, 'prompt': 'A quiet garden',
        'size': '2160x3840', 'quality': quality,
    }


def test_generation_preserves_explicit_render_quality_and_pixel_size(dispatch):
    post('/api/image/generations', json.dumps({
        'model': 'gpt-image-2.5-sunburst', 'prompt': 'A quiet garden',
        'size': '1536x864', 'quality': 'xhigh', 'image_size': '4K',
    }).encode())
    payload = dispatch[0]['args'][3]
    assert payload['size'] == '1536x864'
    assert payload['quality'] == 'xhigh'


def edit_request(fields, images=1):
    boundary = 'test-image-boundary'
    body = bytearray()
    for key, value in fields.items():
        body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
    for index in range(images):
        body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="ref-{index}.png"\r\nContent-Type: image/png\r\n\r\n'.encode())
        body.extend(b'reference-image-bytes\r\n')
    body.extend(f'--{boundary}--\r\n'.encode())
    return post('/api/image/edits', bytes(body), f'multipart/form-data; boundary={boundary}')


def upstream_edit(call):
    _, _, _, body, boundary = call['args']
    msg = BytesParser(policy=default).parsebytes(
        f'Content-Type: multipart/form-data; boundary={boundary}\r\n\r\n'.encode() + body
    )
    fields, images = {}, []
    for part in msg.iter_parts():
        name = part.get_param('name', header='content-disposition')
        if part.get_filename():
            images.append((name, part.get_payload(decode=True)))
        else:
            fields[name] = part.get_payload(decode=True).decode()
    return fields, images


@pytest.mark.parametrize('model,quality', [
    ('gpt-image-2.5-sunburst', 'max'),
    ('gpt-image-2.5-flare', 'auto'),
])
def test_edit_converts_studio_resolution_and_preserves_references(dispatch, model, quality):
    edit_request({
        'model': model, 'prompt': 'Change the sky', 'aspect_ratio': '16:9',
        'image_size': '4K', 'response_format': 'b64_json', 'style': 'cinematic',
    }, images=2)
    fields, images = upstream_edit(dispatch[0])
    assert fields == {
        'model': model, 'prompt': 'Change the sky\nDesired visual style: cinematic.',
        'size': '3840x2160', 'quality': quality,
    }
    assert images == [('image', b'reference-image-bytes'), ('image[]', b'reference-image-bytes')]


def test_edit_uses_configured_variant_and_automatic_size(dispatch):
    edit_request({
        'config': json.dumps({'imageModel': 'gpt-image-2.5-sunburst'}),
        'prompt': 'Change the sky', 'image_size': '2K',
    })
    fields, _ = upstream_edit(dispatch[0])
    assert fields['model'] == 'gpt-image-2.5-sunburst'
    assert fields['size'] == 'auto'
    assert fields['quality'] == 'max'


def test_edit_preserves_native_quality_and_size(dispatch):
    edit_request({
        'model': 'gpt-image-2.5-sunburst-2026-09-08', 'prompt': 'Change the sky',
        'size': '1536x864', 'quality': 'high', 'image_size': '4K',
    })
    fields, _ = upstream_edit(dispatch[0])
    assert fields['model'] == 'gpt-image-2.5-sunburst-2026-09-08'
    assert fields['size'] == '1536x864'
    assert fields['quality'] == 'high'


def test_gemini_generation_keeps_gateway_parameters(dispatch):
    post('/api/image/generations', json.dumps({
        'model': 'nano-banana-2', 'prompt': 'A garden', 'size': '9:16',
        'image_size': '4K', 'response_format': 'b64_json',
    }).encode())
    assert dispatch[0]['args'][3] == {
        'model': 'gemini-3.1-flash-image-9-16', 'prompt': 'A garden',
        'size': '9:16', 'quality': '4K', 'response_format': 'b64_json',
    }


def test_gemini_edit_keeps_gateway_parameters(dispatch):
    expected = {
        'model': 'nano-banana-2', 'prompt': 'Change the sky',
        'aspect_ratio': '16:9', 'image_size': '4K',
        'response_format': 'b64_json', 'style': 'cinematic',
    }
    edit_request(expected)
    fields, _ = upstream_edit(dispatch[0])
    assert fields == {**expected, 'model': 'gemini-3.1-flash-image'}
