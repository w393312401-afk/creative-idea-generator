"""GPT Image variants retain official names and use native image API parameters."""
import json
from email.parser import BytesParser
from email.policy import default
from unittest.mock import patch

import pytest
from PIL import Image

from frame_generator import (
    _generate_image_edit, _generate_text_image, _image_edit_model,
    _image_generation_model, _image_generation_model_for_request,
    _image_size_to_api_size, _quality_to_images_api, call_image_llm,
)
from server_common import (
    gpt_image_pixel_size, gpt_image_render_quality, is_gpt_image_model,
    resolve_chat_model, resolve_gateway, resolve_image_model, effective_config,
)
import server_common


VARIANTS = (
    ('gpt-image-2.5-sunburst', 'max'),
    ('gpt-image-2.5-flare', 'auto'),
    ('gpt-image-2.5-sunburst-2026-09-30', 'max'),
    ('gpt-image-2.5-flare-2026-09-30', 'auto'),
)


@pytest.mark.parametrize('model,quality', VARIANTS)
def test_variant_identity_and_gateway(model, quality):
    assert is_gpt_image_model(model)
    assert resolve_chat_model(model) == model
    config = {'imageModel': model, 'imageAspectRatio': '9:16', 'imageQuality': '4K',
              'codexBaseUrl': 'http://codex.test/v1', 'codexApiKey': 'test-key'}
    assert _image_generation_model(config) == model
    assert _image_edit_model(config) == model
    assert _image_generation_model_for_request(model, '9:16', '4K') == model
    assert _image_size_to_api_size('9:16', model) == '1024x1536'
    assert resolve_gateway(model, config) == ('http://codex.test/v1', 'test-key')
    assert _quality_to_images_api('4K', model) == quality


@pytest.mark.parametrize('model', ('gpt-image-2', 'gpt-image-2.5', 'gpt-image-2-2026-04-21'))
def test_legacy_image_names_and_resolution_labels(model):
    assert is_gpt_image_model(model)
    expected = 'gpt-image-2.5' if model.startswith('gpt-image-2-') or model == 'gpt-image-2' else model
    assert _image_generation_model_for_request(model, '1:1', '2K') == expected
    assert _quality_to_images_api('1K', model) == 'low'
    assert _quality_to_images_api('2K', model) == 'medium'
    assert _quality_to_images_api('4K', model) == 'high'


@pytest.mark.parametrize('old_model', ('gpt-image-2', 'gpt-image-2-2026-04-21'))
def test_retired_image_model_requests_upgrade_in_all_helpers(old_model):
    assert resolve_image_model(old_model) == 'gpt-image-2.5'
    config = {'imageModel': old_model, 'imageAspectRatio': '9:16', 'imageQuality': '4K'}
    assert _image_generation_model(config) == 'gpt-image-2.5'
    assert _image_edit_model(config) == 'gpt-image-2.5'
    assert _image_generation_model_for_request(old_model, '9:16', '4K') == 'gpt-image-2.5'
    assert config['imageModel'] == old_model


@pytest.mark.parametrize('model', ('gpt-image-2.5', 'gpt-image-2.5-sunburst',
                                  'gpt-image-2.5-flare-2026-09-30', 'gpt-image-20',
                                  'gpt-image-2-custom', 'my-gpt-image-2', 'gemini-3.1-flash-image'))
def test_only_exact_retired_image_names_are_upgraded(model):
    assert resolve_image_model(model) == model


@pytest.mark.parametrize('managed', (True, False))
def test_effective_config_upgrades_retired_client_image_model(managed):
    config = {'imageModel': 'gpt-image-2-2026-04-21'}
    with patch.object(server_common, 'SERVER_MANAGED', managed), \
         patch.object(server_common, 'ALLOW_CLIENT_MODEL', True), \
         patch.object(server_common, 'SERVER_CONFIG', {}):
        assert effective_config(config)['imageModel'] == 'gpt-image-2.5'
    assert config['imageModel'] == 'gpt-image-2-2026-04-21'


def test_effective_config_upgrades_authoritative_server_image_model():
    with patch.object(server_common, 'SERVER_MANAGED', True), \
         patch.object(server_common, 'ALLOW_CLIENT_MODEL', False), \
         patch.object(server_common, 'SERVER_CONFIG', {'imageModel': 'gpt-image-2'}):
        assert effective_config({'imageModel': 'gemini-3.1-flash-image'})['imageModel'] == 'gpt-image-2.5'


@pytest.mark.parametrize('model', ('gemini-3.1-flash-image', 'my-gpt-image-2.5',
                                  'gpt-image-2.5-high', 'gpt-image-2.5-sunburst-9-16-4k'))
def test_unverified_names_are_not_native_gpt_variants(model):
    assert not is_gpt_image_model(model)


@pytest.mark.parametrize('model,default_quality', VARIANTS)
@pytest.mark.parametrize('resolution', ('1K', '2K', '4K', 'hd'))
def test_variant_render_quality_is_independent_of_resolution(model, default_quality, resolution):
    assert _quality_to_images_api(resolution, model) == default_quality


@pytest.mark.parametrize('quality', ('low', 'medium', 'high', 'xhigh', 'max', 'auto'))
@pytest.mark.parametrize('model,_default_quality', VARIANTS[:2])
def test_explicit_native_quality_is_preserved(model, _default_quality, quality):
    assert gpt_image_render_quality(model, quality) == quality
    assert _quality_to_images_api(quality, model) == quality


@pytest.mark.parametrize('model,_default_quality', VARIANTS)
@pytest.mark.parametrize('ratio,resolution,expected', (
    ('9:16', '1K', '1024x1536'), ('9:16', '2K', '1152x2048'),
    ('16:9', '2K', '2048x1152'), ('1:1', '2K', '2048x2048'),
    ('9:16', '4K', '2160x3840'), ('16:9', '4K', '3840x2160'),
    ('1:1', '4K', '2880x2880'), ('1:3', '4K', '1280x3840'),
    ('1024x1536', '4K', '1024x1536'), ('auto', '4K', 'auto'),
))
def test_variant_size_applies_resolution_without_exceeding_api_limits(model, _default_quality,
                                                                     ratio, resolution, expected):
    assert gpt_image_pixel_size(ratio, resolution, model) == expected
    assert _image_size_to_api_size(ratio, model, resolution) == expected


@pytest.mark.parametrize('ratio', ('9:16', '16:9', '1:1', '4:3', '2:3', '21:9', '1:9'))
@pytest.mark.parametrize('resolution', ('2K', '4K'))
def test_scaled_dimensions_meet_pixel_constraints(ratio, resolution):
    size = gpt_image_pixel_size(ratio, resolution, 'gpt-image-2.5-sunburst')
    width, height = map(int, size.split('x'))
    assert width % 16 == height % 16 == 0
    assert max(width, height) <= 3840
    assert max(width, height) <= 3 * min(width, height)
    assert 655360 <= width * height <= 8294400


def test_legacy_base_sizes_keep_the_existing_resolution_policy():
    assert gpt_image_pixel_size('9:16', '4K', 'gpt-image-2.5') == '1024x1536'
    assert gpt_image_pixel_size('9:16', '2K', 'gpt-image-2') == '1024x1536'


@pytest.mark.parametrize('size', ('auto', '1024x1024', '1024x1536', '1536x1024',
                                 '720x1280', '2048x2048', '3840x2160'))
def test_explicit_supported_pixel_sizes_are_preserved(size):
    assert gpt_image_pixel_size(size) == size


@pytest.mark.parametrize('size', ('0x1024', '721x1280', '4000x2000', '4096x4096',
                                 '512x512', '1024x3840'))
def test_unsupported_pixel_sizes_use_the_square_default(size):
    assert gpt_image_pixel_size(size) == '1024x1024'


@pytest.mark.parametrize('model,quality', VARIANTS)
def test_generation_payload_preserves_variant_and_uses_native_fields(model, quality, tmp_path):
    config = {'imageModel': model, 'imageAspectRatio': '9:16', 'imageQuality': '4K'}
    with patch('frame_generator.resolve_gateway', return_value=('http://codex.test/v1', 'test-key')), \
         patch('frame_generator._post_json', return_value={'data': [{'b64_json': 'ignored'}]}) as post, \
         patch('frame_generator._decode_or_download_image'):
        _generate_text_image(config, 'render a forest', str(tmp_path / 'render.webp'))
    payload = post.call_args.args[3]
    assert payload['model'] == model
    assert payload['size'] == '2160x3840'
    assert payload['quality'] == quality
    assert not {'image_size', 'aspect_ratio', 'response_format'} & payload.keys()


@pytest.mark.parametrize('allow_text', (False, True))
def test_cover_can_request_text_while_frames_keep_the_no_text_control(allow_text, tmp_path):
    prompt = 'Overlay the English hook SECRET BACKYARD CINEMA in bold white text.'
    with patch('frame_generator.resolve_gateway', return_value=('http://codex.test/v1', 'test-key')), \
         patch('frame_generator._post_json', return_value={'data': [{'b64_json': 'ignored'}]}) as post, \
         patch('frame_generator._decode_or_download_image'):
        _generate_text_image({'imageModel': 'gpt-image-2.5'}, prompt,
                             str(tmp_path / 'cover.webp'), **({'allow_text': True} if allow_text else {}))
    sent_prompt = post.call_args.args[3]['prompt']
    if allow_text:
        assert sent_prompt == prompt
    else:
        assert 'no visible text' in sent_prompt
        assert sent_prompt.endswith(prompt)


@pytest.mark.parametrize('model,quality', VARIANTS)
def test_edit_payload_preserves_variant_date_and_uses_native_fields(model, quality, tmp_path):
    reference = tmp_path / 'reference.png'
    Image.new('RGB', (64, 64), 'green').save(reference)
    config = {'imageModel': model, 'imageAspectRatio': '9:16', 'imageQuality': '4K'}
    with patch('frame_generator.resolve_gateway', return_value=('http://codex.test/v1', 'test-key')), \
         patch('frame_generator._execute_request_with_retry',
               return_value=json.dumps({'data': [{'b64_json': 'ignored'}]}).encode()) as execute, \
         patch('frame_generator._decode_or_download_image'):
        _generate_image_edit(config, 'render a forest', str(reference), str(tmp_path / 'edit.webp'))
    request = execute.call_args.args[0]
    assert request.full_url == 'http://codex.test/v1/images/edits'
    message = BytesParser(policy=default).parsebytes(
        f'Content-Type: {request.get_header("Content-type")}\r\n\r\n'.encode() + request.data)
    parts = {part.get_param('name', header='content-disposition'): part
             for part in message.iter_parts()}
    assert parts['model'].get_content().strip() == model
    assert parts['size'].get_content().strip() == '2160x3840'
    assert parts['quality'].get_content().strip() == quality
    assert 'image' in parts
    assert not {'image_size', 'aspect_ratio', 'response_format'} & parts.keys()


def test_legacy_chat_helper_does_not_append_suffix_to_gpt_variant():
    model = 'gpt-image-2.5-sunburst-2026-09-30'
    response = json.dumps({'choices': [{'message': {'content': 'result'}}]}).encode()
    with patch('frame_generator.resolve_gateway', return_value=('http://codex.test/v1', 'test-key')), \
         patch('frame_generator._execute_request_with_retry', return_value=response) as execute:
        call_image_llm({'imageModel': model, 'imageAspectRatio': '9:16', 'imageQuality': '4K'}, 'render')
    payload = json.loads(execute.call_args.args[0].data)
    assert payload['model'] == model
