"""Opt-in Cockpit image transport with the Images API result contract preserved."""
import base64
import binascii
import json
import urllib.parse
import urllib.request
from email.parser import BytesParser
from email.policy import default

from server_common import cockpit_image_responses_models


class ImageResponsesParameterError(ValueError):
    """The requested Images parameter cannot be carried by this adapter."""


class ImageResponsesOutputError(ValueError):
    """A completed HTTP exchange did not produce a usable image; do not rerender."""


_TOOL_FIELDS = frozenset((
    'size', 'quality', 'background', 'output_format', 'output_compression',
    'moderation', 'input_fidelity',
))
_IMAGE_FIELDS = frozenset(('image', 'image[]', 'images'))
_ALLOWED_FIELDS = _TOOL_FIELDS | _IMAGE_FIELDS | {
    'model', 'prompt', 'mask', 'n', 'response_format', 'stream',
}


def _image_reference(value):
    if isinstance(value, str) and value.strip():
        return {'image_url': value}
    if isinstance(value, dict):
        if set(value) == {'url'} and isinstance(value['url'], str) and value['url']:
            return {'image_url': value['url']}
        if set(value) in ({'image_url'}, {'file_id'}):
            key = next(iter(value))
            if isinstance(value[key], str) and value[key]:
                return dict(value)
    raise ImageResponsesParameterError('Responses 图片通路不支持该参考图或遮罩格式。')


def _multipart_fields(req, allowed):
    header = req.get_header('Content-type') or ''
    message = BytesParser(policy=default).parsebytes(
        f'Content-Type: {header}\r\nMIME-Version: 1.0\r\n\r\n'.encode() + req.data)
    if not message.is_multipart():
        return None
    parts = list(message.iter_parts())
    models = [part.get_payload(decode=True) for part in parts
              if part.get_content_disposition() == 'form-data'
              and part.get_param('name', header='content-disposition') == 'model'
              and part.get_filename() is None]
    if not any(model == enabled.encode('utf-8') for model in models for enabled in allowed):
        return None
    if len(models) != 1 or message.defects:
        raise ImageResponsesParameterError('Responses 图片通路收到无效 multipart 请求。')
    fields = {}
    images = []
    mask = None
    for part in parts:
        if part.get_content_disposition() != 'form-data':
            raise ImageResponsesParameterError('Responses 图片通路不支持该 multipart 部分。')
        name = part.get_param('name', header='content-disposition')
        if not name:
            raise ImageResponsesParameterError('Responses 图片通路收到无名称的 multipart 参数。')
        content = part.get_payload(decode=True)
        if content is None:
            raise ImageResponsesParameterError('Responses 图片通路收到无效 multipart 内容。')
        if part.get_filename() is not None:
            if name not in _IMAGE_FIELDS and name != 'mask':
                raise ImageResponsesParameterError('Responses 图片通路不支持该上传文件参数。')
            if not content:
                raise ImageResponsesParameterError('Responses 图片通路收到空参考图或遮罩。')
            mime = part.get_content_type()
            if not mime.startswith('image/'):
                raise ImageResponsesParameterError('Responses 图片通路只接受图片参考和遮罩。')
            reference = {'image_url': f'data:{mime};base64,{base64.b64encode(content).decode("ascii")}'}
            if name == 'mask':
                if mask is not None:
                    raise ImageResponsesParameterError('Responses 图片通路只支持一个遮罩。')
                mask = reference
            else:
                images.append(reference)
        else:
            if name in fields:
                raise ImageResponsesParameterError('Responses 图片通路不支持重复的文本参数。')
            try:
                fields[name] = content.decode('utf-8')
            except UnicodeDecodeError:
                raise ImageResponsesParameterError('Responses 图片通路收到无效文本编码。') from None
    if images:
        if any(name in fields for name in _IMAGE_FIELDS):
            raise ImageResponsesParameterError('Responses 图片通路不支持混合文本与文件参考图。')
        fields['image'] = images
    if mask is not None:
        if 'mask' in fields:
            raise ImageResponsesParameterError('Responses 图片通路只支持一个遮罩。')
        fields['mask'] = mask
    return fields


def _responses_payload(fields, operation):
    if set(fields) - _ALLOWED_FIELDS:
        # Do not echo arbitrary field names or values: they may contain credentials.
        raise ImageResponsesParameterError('Responses 图片通路含不支持的参数，请移除后重试。')
    count = fields.get('n', 1)
    if type(count) is bool or count not in (1, '1'):
        raise ImageResponsesParameterError('Responses 图片通路每个任务仅支持 n=1。')
    if fields.get('response_format', 'b64_json') != 'b64_json':
        raise ImageResponsesParameterError('Responses 图片通路仅支持 b64_json 返回格式。')
    if fields.get('stream', False) not in (False, 'false', 'False', '0', 0):
        raise ImageResponsesParameterError('Responses 图片通路当前不支持流式图片请求。')
    prompt = fields.get('prompt')
    if not isinstance(prompt, str) or not prompt.strip():
        raise ImageResponsesParameterError('Responses 图片通路需要非空 prompt。')
    tool = {'type': 'image_generation', 'model': fields['model'],
            'action': 'edit' if operation == 'edits' else 'generate'}
    for name in _TOOL_FIELDS:
        if name in fields:
            value = fields[name]
            if name == 'output_compression':
                try:
                    converted = int(value)
                except (TypeError, ValueError):
                    raise ImageResponsesParameterError('output_compression 必须为 0 至 100 的整数。') from None
                if type(value) is bool or str(converted) != str(value) or not 0 <= converted <= 100:
                    raise ImageResponsesParameterError('output_compression 必须为 0 至 100 的整数。')
                value = converted
            elif not isinstance(value, str) or not value.strip():
                raise ImageResponsesParameterError('Responses 图片通路收到无效工具参数。')
            tool[name] = value
    content = [{'type': 'input_text', 'text': prompt}]
    for name, value in fields.items():
        if name in _IMAGE_FIELDS:
            for reference in value if isinstance(value, list) else [value]:
                content.append(dict(type='input_image', **_image_reference(reference)))
    has_images = len(content) > 1
    if operation == 'edits' and not has_images:
        raise ImageResponsesParameterError('Responses 图片编辑需要至少一张参考图。')
    if operation == 'generations' and (has_images or 'mask' in fields):
        raise ImageResponsesParameterError('生成请求含参考图或遮罩，请使用图片编辑接口。')
    if 'mask' in fields:
        tool['input_image_mask'] = _image_reference(fields['mask'])
    return {'model': 'gpt-5.5', 'input': [{'role': 'user', 'content': content}],
            'tools': [tool], 'tool_choice': {'type': 'image_generation'}, 'stream': False}


def _images_response(raw, requested_model=None):
    try:
        response = json.loads(raw)
    except (ValueError, TypeError):
        raise ImageResponsesOutputError('Responses 图片通路返回了无效 JSON。') from None
    if (not isinstance(response, dict) or response.get('error')
            or response.get('status') not in (None, 'completed')):
        raise ImageResponsesOutputError('Responses 图片请求未完成或上游报告失败，未返回可用图片。')
    output = response.get('output')
    if not isinstance(output, list):
        raise ImageResponsesOutputError('Responses 图片通路未返回 image_generation_call 图片数据。')
    images = []
    reported = {}
    for item in output:
        if not isinstance(item, dict) or item.get('type') != 'image_generation_call':
            continue
        result = item.get('result')
        if (item.get('status') not in (None, 'completed')
                or not isinstance(result, str) or not result.strip()):
            raise ImageResponsesOutputError('Responses 图片工具未完成或返回空图片数据。')
        reported_model = item.get('model')
        if (requested_model and isinstance(reported_model, str) and reported_model.strip()
                and reported_model != requested_model):
            raise ImageResponsesOutputError('Responses 图片工具报告的实际型号与请求型号不一致，已停止保存。')
        try:
            if not base64.b64decode(result, validate=True):
                raise ValueError('empty image')
        except (binascii.Error, ValueError):
            raise ImageResponsesOutputError('Responses 图片工具返回了无效 base64 图片数据。') from None
        image = {'b64_json': result}
        if isinstance(item.get('revised_prompt'), str):
            image['revised_prompt'] = item['revised_prompt']
        images.append(image)
        for name in ('model', 'size', 'quality', 'background', 'output_format'):
            if isinstance(item.get(name), str):
                reported[name] = item[name]
    if not images:
        raise ImageResponsesOutputError('Responses 图片通路未返回 image_generation_call 图片数据。')
    normalized = dict(data=images, **reported)
    if isinstance(response.get('created_at'), (int, float)):
        normalized['created'] = response['created_at']
    if isinstance(response.get('usage'), dict):
        normalized['usage'] = response['usage']
    return json.dumps(normalized, ensure_ascii=False).encode('utf-8')


def adapt_cockpit_image_request(req):
    """Return (request, result adapter); unrelated and unverified gateways pass through."""
    if not isinstance(req, urllib.request.Request) or req.get_method() != 'POST':
        return req, None
    url = urllib.parse.urlsplit(req.full_url)
    if url.path not in ('/v1/images/generations', '/v1/images/edits') or url.query or url.fragment:
        return req, None
    authorization = req.get_header('Authorization') or ''
    if not authorization.startswith('Bearer '):
        return req, None
    base_url = urllib.parse.urlunsplit((url.scheme, url.netloc, '/v1', '', ''))
    allowed = cockpit_image_responses_models(base_url, authorization[7:])
    if not allowed:
        return req, None
    content_type = (req.get_header('Content-type') or '').lower()
    if content_type.startswith('application/json'):
        try:
            fields = json.loads(req.data)
        except (ValueError, TypeError):
            return req, None
        if not isinstance(fields, dict):
            return req, None
    elif content_type.startswith('multipart/form-data'):
        fields = _multipart_fields(req, allowed)
        if fields is None:
            return req, None
    else:
        return req, None
    if fields.get('model') not in allowed:
        return req, None
    payload = _responses_payload(fields, url.path.rsplit('/', 1)[-1])
    headers = {name: value for name, value in req.header_items()
               if name.lower() not in ('content-type', 'content-length', 'transfer-encoding')}
    headers['Content-Type'] = 'application/json'
    adapted = urllib.request.Request(f'{base_url}/responses',
                                     data=json.dumps(payload).encode('utf-8'),
                                     headers=headers, method='POST')
    requested_model = fields['model']
    return adapted, lambda raw: _images_response(raw, requested_model)
