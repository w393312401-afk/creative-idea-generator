import os
import sys
import json
import io
import time
import socket
import urllib.request
import urllib.error
import urllib.parse
import base64
import threading
import re
from PIL import Image

from server_common import (
    SERVER_CONFIG, SERVER_MANAGED, resolve_gateway, effective_config,
    OUTPUT_ROOT, SKILL_DIR, _get_project_dir, _safe_project_name,
    IMG2IMG_CONTROL_PROMPT, IMG2IMG_BRIDGE_CONTROL_PROMPT
)


def call_image_llm(config, prompt_content):
    base_model = config.get('imageModel') or 'gemini-3.1-flash-image'
    if 'nano-banana-2' in base_model:
        base_model = base_model.replace('nano-banana-2', 'gemini-3.1-flash-image')

    # Build magic suffix model name (Method 3)
    model = base_model
    
    # 1. Aspect Ratio suffix
    aspect_ratio = config.get('imageAspectRatio')
    if aspect_ratio:
        # replace ':' with '-' to convert '9:16' to '9-16'
        ratio_suffix = aspect_ratio.replace(':', '-')
        model = f"{model}-{ratio_suffix}"

    # 2. Quality suffix
    quality = config.get('imageQuality')
    if quality:
        q_lower = quality.lower()
        if q_lower in ('2k', 'medium'):
            model = f"{model}-2k"
        elif q_lower in ('4k', 'hd'):
            model = f"{model}-4k"
        # '1k' or 'standard' gets no suffix

    if sys.stdout:
        print(f"[DEBUG] call_image_llm (magic suffix): original_model='{base_model}', constructed_model='{model}'")

    base_url, api_key = resolve_gateway(model, config)
    payload = {
        'model': model,
        'messages': [
            {'role': 'user', 'content': prompt_content},
        ]
    }

    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(
        f'{base_url}/chat/completions',
        data=data,
        headers={
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
        },
        method='POST',
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=180) as resp:
        body = json.loads(resp.read().decode('utf-8'))
    return body['choices'][0]['message'].get('content') or ''


def _quality_to_images_api(quality):
    return _image_quality_to_label(quality)


def _image_size_to_api_size(aspect_ratio):
    return aspect_ratio or '9:16'


def _image_quality_to_label(quality):
    q = (quality or '2K').lower()
    if q in ('4k', 'hd'):
        return '4K'
    if q in ('2k', 'medium'):
        return '2K'
    return '1K'


def _image_generation_model(config):
    model = config.get('imageModel') or 'gemini-3.1-flash-image'
    if 'nano-banana-2' in model:
        model = model.replace('nano-banana-2', 'gemini-3.1-flash-image')
    if model == 'gpt-image-2':
        return model
    if re.search(r'-\d+-\d+(?:-\d+k)?$', model.lower()):
        return model

    aspect_ratio = (config.get('imageAspectRatio') or '9:16').replace(':', '-')
    if model.lower().startswith('gemini'):
        return f'{model}-{aspect_ratio}'

    quality = _image_quality_to_label(config.get('imageQuality'))
    if quality == '4K':
        return f'{model}-{aspect_ratio}-4k'
    if quality == '2K':
        return f'{model}-{aspect_ratio}-2k'
    return f'{model}-{aspect_ratio}'


def _image_generation_model_for_request(model, size, quality):
    if model == 'gpt-image-2':
        return model
    if re.search(r'-\d+-\d+(?:-\d+k)?$', model.lower()):
        return model

    aspect_ratio = (size or '1:1').replace(':', '-')
    if model.lower().startswith('gemini'):
        return f'{model}-{aspect_ratio}'

    quality_label = _image_quality_to_label(quality)
    if quality_label == '4K':
        return f'{model}-{aspect_ratio}-4k'
    if quality_label == '2K':
        return f'{model}-{aspect_ratio}-2k'
    return f'{model}-{aspect_ratio}'


def _image_edit_model(config):
    model = config.get('imageModel') or 'gemini-3.1-flash-image'
    if 'nano-banana-2' in model:
        model = model.replace('nano-banana-2', 'gemini-3.1-flash-image')
    if model == 'gpt-image-2':
        return model
    if re.search(r'-\d+-\d+(?:-\d+k)?$', model.lower()):
        return model

    aspect_ratio = (config.get('imageAspectRatio') or '9:16').replace(':', '-')
    if model.lower().startswith('gemini'):
        return f'{model}-{aspect_ratio}'

    quality = _image_quality_to_label(config.get('imageQuality'))
    if quality == '4K':
        return f'{model}-{aspect_ratio}-4k'
    if quality == '2K':
        return f'{model}-{aspect_ratio}-2k'
    return f'{model}-{aspect_ratio}'


def _extract_image_url_from_text(content):
    if not content:
        return None
    markdown = re.search(r'!\[.*?\]\((.*?)\)', content, re.DOTALL)
    if markdown:
        return markdown.group(1).strip()
    raw_url = re.search(r'(https?://[^\s)]+|data:image/[^\s)]+)', content, re.DOTALL)
    if raw_url:
        return raw_url.group(1).strip()
    trimmed = content.strip()
    if trimmed.startswith(('http://', 'https://', 'data:image/')):
        return trimmed
    return None


def _clean_gemini_image_model(model):
    model = model or 'gemini-3.1-flash-image'
    if 'nano-banana-2' in model:
        model = model.replace('nano-banana-2', 'gemini-3.1-flash-image')
    model = re.sub(r'-\d+-\d+(?:-\d+k)?$', '', model, flags=re.IGNORECASE)
    model = re.sub(r'-(?:2k|4k)(?:-\d+x\d+)?$', '', model, flags=re.IGNORECASE)
    return model


def _gemini_direct_api_key(config):
    return (
        (config or {}).get('geminiDirectApiKey')
        or (config or {}).get('geminiApiKey')
        or SERVER_CONFIG.get('geminiDirectApiKey')
        or SERVER_CONFIG.get('geminiApiKey')
        or os.environ.get('GEMINI_API_KEY')
        or ''
    ).strip()


def _prepare_gemini_inline_image(file_data, content_type=None, max_side=1536):
    try:
        import io
        from PIL import Image

        img = Image.open(io.BytesIO(file_data)).convert('RGB')
        img.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        return 'image/png', base64.b64encode(buf.getvalue()).decode('ascii')
    except Exception:
        return content_type or 'image/png', base64.b64encode(file_data).decode('ascii')


def _crop_to_aspect_ratio(img, aspect_ratio_str):
    if not aspect_ratio_str or aspect_ratio_str.lower() == 'auto':
        return img
    try:
        aspect_ratio_str = aspect_ratio_str.replace('-', ':')
        if ':' in aspect_ratio_str:
            w_ratio, h_ratio = map(float, aspect_ratio_str.split(':'))
        else:
            return img
            
        target_ratio = w_ratio / h_ratio
        width, height = img.size
        current_ratio = width / height
        
        if abs(current_ratio - target_ratio) < 0.01:
            return img
            
        if current_ratio > target_ratio:
            new_width = int(height * target_ratio)
            left = (width - new_width) // 2
            right = left + new_width
            img = img.crop((left, 0, right, height))
        else:
            new_height = int(width / target_ratio)
            top = (height - new_height) // 2
            bottom = top + new_height
            img = img.crop((0, top, width, bottom))
            
        return img
    except Exception:
        return img


def _extract_b64_from_gemini_native_response(obj):
    if isinstance(obj, list):
        for item in obj:
            found = _extract_b64_from_gemini_native_response(item)
            if found:
                return found
        return None
    if not isinstance(obj, dict):
        if isinstance(obj, str) and obj.startswith('data:image/') and ',' in obj:
            return obj.split(',', 1)[1]
        return None

    for key in ('inlineData', 'inline_data', 'image', 'output_image', 'outputImage'):
        value = obj.get(key)
        if isinstance(value, dict):
            data = value.get('data') or value.get('b64_json') or value.get('base64')
            if isinstance(data, str) and len(data) > 100:
                return data.split(',', 1)[1] if data.startswith('data:image/') and ',' in data else data
            url = value.get('url')
            if isinstance(url, str) and url.startswith('data:image/') and ',' in url:
                return url.split(',', 1)[1]

    for key in ('b64_json', 'base64', 'data'):
        value = obj.get(key)
        if isinstance(value, str) and len(value) > 100:
            if key == 'data':
                is_image = (
                    obj.get('type') == 'image' or
                    'image' in str(obj.get('mime_type') or obj.get('mimeType') or '') or
                    not any(c.isspace() for c in value[:100])
                )
                if not is_image:
                    continue
            return value.split(',', 1)[1] if value.startswith('data:image/') and ',' in value else value

    for value in obj.values():
        found = _extract_b64_from_gemini_native_response(value)
        if found:
            return found
    return None


def _post_gemini_direct_json(url, api_key, payload, timeout=240):
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            'Content-Type': 'application/json',
            'x-goog-api-key': api_key,
        },
        method='POST',
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8'))


def _gemini_native_image_edit(config, model, prompt, file_items, aspect_ratio, image_size):
    api_key = _gemini_direct_api_key(config)
    if not api_key:
        return None

    clean_model = _clean_gemini_image_model(model)
    direct_model = (config or {}).get('geminiDirectImageModel') or SERVER_CONFIG.get('geminiDirectImageModel') or clean_model
    instruction = (
        'IMAGE EDITING MODE. Use the attached reference image as the authoritative source canvas. '
        'Preserve its composition, camera angle, objects, identity, materials, and layout unless the '
        'user explicitly asks to change them. Do not create an unrelated text-to-image scene. '
        'Return one edited image only.\n\n'
        f'{prompt or ""}'
    )
    inline_images = []
    for _name, _filename, content_type, file_data in file_items:
        mime, encoded = _prepare_gemini_inline_image(file_data, content_type)
        inline_images.append((mime, encoded))

    endpoint_model = urllib.parse.quote(direct_model, safe='')
    last_error = None

    interactions_input = [{'type': 'text', 'text': instruction}]
    for mime, encoded in inline_images:
        interactions_input.append({
            'type': 'image',
            'image': {'mime_type': mime, 'mimeType': mime, 'data': encoded},
            'mime_type': mime,
            'mimeType': mime,
            'data': encoded,
        })
    image_generation_config = {}
    if aspect_ratio:
        image_generation_config['aspectRatio'] = aspect_ratio
    if image_size:
        image_generation_config['imageSize'] = image_size

    interactions_payload = {
        'model': direct_model,
        'input': interactions_input,
        'responseModalities': ['IMAGE'],
        'response_format': {
            'type': 'image',
            'mime_type': 'image/jpeg',
            'mimeType': 'image/jpeg',
        }
    }
    if image_generation_config:
        interactions_payload['imageGenerationConfig'] = image_generation_config

    generate_parts = [{'text': instruction}]
    for mime, encoded in inline_images:
        generate_parts.append({'inlineData': {'mimeType': mime, 'data': encoded}})
    generate_payload = {
        'contents': [{'role': 'user', 'parts': generate_parts}],
        'generationConfig': {'responseModalities': ['IMAGE']},
    }
    if aspect_ratio or image_size:
        generate_payload['generationConfig']['imageConfig'] = {}
        if aspect_ratio:
            generate_payload['generationConfig']['imageConfig']['aspectRatio'] = aspect_ratio
        if image_size:
            generate_payload['generationConfig']['imageConfig']['imageSize'] = image_size

    for label, url, payload in (
        ('interactions', 'https://generativelanguage.googleapis.com/v1beta/interactions', interactions_payload),
        ('generateContent', f'https://generativelanguage.googleapis.com/v1beta/models/{endpoint_model}:generateContent', generate_payload),
    ):
        try:
            if sys.stdout:
                print(f"[GEMINI DIRECT] I2I via {label}, model={direct_model}, images={len(file_items)}")
            resp_data = _post_gemini_direct_json(url, api_key, payload, timeout=300)
            b64_json = _extract_b64_from_gemini_native_response(resp_data)
            if b64_json:
                return {'created': int(time.time()), 'data': [{'b64_json': b64_json}]}
            last_error = f'{label} response contained no image data'
        except urllib.error.HTTPError as e:
            detail = ''
            try:
                detail = e.read().decode('utf-8')[:800]
            except Exception:
                pass
            last_error = f'{label} HTTP {e.code}: {detail}'
            if sys.stdout:
                print(f"[GEMINI DIRECT] {last_error}")
        except Exception as e:
            last_error = f'{label}: {e}'
            if sys.stdout:
                print(f"[GEMINI DIRECT] {last_error}")

    raise RuntimeError(f'Gemini direct image edit failed: {last_error}')


def _detect_image_mime_from_path(path):
    with open(path, 'rb') as image_file:
        signature = image_file.read(16)
    if signature.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if signature.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if signature.startswith((b'GIF87a', b'GIF89a')):
        return 'image/gif'
    if signature.startswith(b'RIFF') and signature[8:12] == b'WEBP':
        return 'image/webp'
    return 'application/octet-stream'


def _persist_data_url_image(data_url, title, prefix='cover'):
    if not data_url or not data_url.startswith('data:image/'):
        return data_url

    header, encoded = data_url.split(',', 1)
    ext_match = re.match(r'data:image/([a-zA-Z0-9.+-]+);base64', header)
    ext = (ext_match.group(1) if ext_match else 'png').lower()
    if ext == 'jpeg':
        ext = 'jpg'

    out_dir = os.path.join(OUTPUT_ROOT, 'covers')
    os.makedirs(out_dir, exist_ok=True)
    filename = f"{_safe_project_name(title)}_{prefix}_{int(time.time() * 1000)}.{ext}"
    target_path = os.path.join(out_dir, filename)
    with open(target_path, 'wb') as f:
        f.write(base64.b64decode(encoded))

    rel_path = os.path.relpath(target_path, os.path.dirname(os.path.abspath(__file__))).replace('\\', '/')
    return '/' + rel_path


def _extract_image_prompts(block):
    images, _ = _parse_prompt_slots(block)
    return [{'index': idx, 'prompt': images[idx]} for idx in sorted(images)]


def _decode_or_download_image(data_item, target_path, config):
    b64 = None
    url = None
    if isinstance(data_item, dict):
        b64 = data_item.get('b64_json')
        url = data_item.get('url')
    elif isinstance(data_item, str):
        url = data_item

    image_bytes = None
    if b64:
        image_bytes = base64.b64decode(b64)
    elif url and url.startswith('data:image/'):
        _, encoded = url.split(',', 1)
        image_bytes = base64.b64decode(encoded)
    elif url and (url.startswith('http://') or url.startswith('https://')):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=180) as resp:
            image_bytes = resp.read()

    if not image_bytes:
        raise RuntimeError('image response did not include b64_json, data URL, or downloadable URL')

    if target_path.lower().endswith('.webp'):
        from PIL import Image
        import io
        try:
            img = Image.open(io.BytesIO(image_bytes))
            img.save(target_path, format='WEBP', quality=80)
        except Exception as e:
            print(f"Failed to convert image to WebP: {e}. Saving raw bytes instead.")
            with open(target_path, 'wb') as f:
                f.write(image_bytes)
    else:
        with open(target_path, 'wb') as f:
            f.write(image_bytes)


def _save_image_station_result(resp_data):
    """
    Given the response from /api/image/generations or /api/image/edits,
    if it contains b64_json or a remote URL, download/decode it,
    save it to outputs/image-station/ as a WebP file,
    and update resp_data to point to the local WebP URL.
    This prevents sending megabytes of base64 to the frontend.
    """
    if not isinstance(resp_data, dict) or 'data' not in resp_data:
        return resp_data
    
    out_dir = os.path.join(OUTPUT_ROOT, 'image-station')
    os.makedirs(out_dir, exist_ok=True)
    
    import base64
    import time
    import uuid
    import urllib.request
    
    for item in resp_data.get('data', []):
        b64 = item.get('b64_json')
        url = item.get('url')
        
        image_bytes = None
        if b64:
            image_bytes = base64.b64decode(b64)
        elif url and url.startswith('data:image/'):
            try:
                _, encoded = url.split(',', 1)
                image_bytes = base64.b64decode(encoded)
            except Exception: pass
        elif url and (url.startswith('http://') or url.startswith('https://')):
            try:
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(url, timeout=180) as resp:
                    image_bytes = resp.read()
            except Exception as e:
                print(f"[IMAGE STATION] Failed to download remote image {url}: {e}")
                
        if image_bytes:
            filename = f"img_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}.webp"
            target_path = os.path.join(out_dir, filename)
            
            try:
                from PIL import Image
                import io
                img = Image.open(io.BytesIO(image_bytes))
                img.save(target_path, format='WEBP', quality=80)
                
                # Update the item in resp_data
                rel_path = os.path.relpath(target_path, os.path.dirname(os.path.abspath(__file__))).replace('\\', '/')
                if 'b64_json' in item:
                    del item['b64_json']
                item['url'] = '/' + rel_path
            except Exception as e:
                print(f"[IMAGE STATION] Failed to convert/save image as WebP: {e}")
                
    return resp_data


def _post_json(base_url, api_key, path, payload, timeout=240):
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(
        f'{base_url.rstrip("/")}{path}',
        data=data,
        headers={
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
        },
        method='POST',
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8'))


def _post_multipart(base_url, api_key, path, fields, file_field, file_path, timeout=300):
    boundary = f'----SparkFrameBoundary{int(time.time() * 1000)}'
    chunks = []
    for name, value in fields.items():
        chunks.append(f'--{boundary}\r\n'.encode('utf-8'))
        chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode('utf-8'))
        chunks.append(str(value).encode('utf-8'))
        chunks.append(b'\r\n')
    filename = os.path.basename(file_path)
    chunks.append(f'--{boundary}\r\n'.encode('utf-8'))
    chunks.append(
        f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
        f'Content-Type: image/png\r\n\r\n'.encode('utf-8')
    )
    with open(file_path, 'rb') as f:
        chunks.append(f.read())
    chunks.append(b'\r\n')
    chunks.append(f'--{boundary}--\r\n'.encode('utf-8'))
    body = b''.join(chunks)

    req = urllib.request.Request(
        f'{base_url.rstrip("/")}{path}',
        data=body,
        headers={
            'Authorization': f'Bearer {api_key}',
            'Content-Type': f'multipart/form-data; boundary={boundary}',
        },
        method='POST',
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8'))


def _generate_text_image(config, prompt, target_path):
    model = _image_generation_model(config)
    base_url, api_key = resolve_gateway(model, config)
    clean_render_prompt = (
        "Render a clean photorealistic image with no visible text, captions, labels, grid lines, "
        "measurement guides, letters, numbers, or percentages. Any terms such as Grid A2, Grid B1, "
        "coordinates, or percentage heights in the scene description are invisible composition "
        "instructions only and must never appear in the image.\n\n"
        f"{prompt}"
    )
    payload = {
        'model': _image_generation_model(config),
        'prompt': clean_render_prompt,
        'size': _image_size_to_api_size(config.get('imageAspectRatio')),
        'quality': _quality_to_images_api(config.get('imageQuality')),
        'image_size': _image_quality_to_label(config.get('imageQuality')),
        'response_format': 'b64_json',
    }
    data = _post_json(base_url, api_key, '/images/generations', payload, timeout=300)
    if not data.get('data'):
        raise RuntimeError('text-to-image response contained no image data')
    _decode_or_download_image(data['data'][0], target_path, config)


def _generate_image_edit(config, prompt, reference_path, target_path, control_prompt=None):
    if control_prompt is None:
        control_prompt = IMG2IMG_CONTROL_PROMPT
    model = _image_edit_model(config)
    base_url, api_key = resolve_gateway(model, config)

    aspect_ratio = config.get('imageAspectRatio') or '9:16'
    try:
        from PIL import Image
        img = Image.open(reference_path).convert('RGB')
        img = _crop_to_aspect_ratio(img, aspect_ratio)
        import io
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        ref_bytes = buf.getvalue()
    except Exception:
        with open(reference_path, 'rb') as f:
            ref_bytes = f.read()

    # Ensure clean model name (no suffixes like -9-16-2k)
    clean_model = re.sub(r'-\d+-\d+(?:-\d+k)?$', '', model, flags=re.IGNORECASE)
    clean_model = re.sub(r'-(?:2k|4k)(?:-\d+x\d+)?$', '', clean_model, flags=re.IGNORECASE)

    max_attempts = 3
    for attempt in range(max_attempts):
        try:
            # 1. If using Gemini Direct API, route to the native Gemini image edit method
            if _gemini_direct_api_key(config):
                file_items = [('image', 'reference.png', 'image/png', ref_bytes)]
                image_size = _image_quality_to_label(config.get('imageQuality'))
                
                native_resp = _gemini_native_image_edit(
                    config,
                    clean_model,
                    prompt,
                    file_items,
                    aspect_ratio,
                    image_size,
                )
                if native_resp and native_resp.get('data'):
                    _decode_or_download_image(native_resp['data'][0], target_path, config)
                    return False  # Success, not a fallback
                else:
                    raise RuntimeError('Gemini direct image edit returned no data')

            # 2. Use the standard OpenAI /images/edits endpoint via multipart/form-data.
            import uuid
            boundary = f"Boundary-{uuid.uuid4().hex}"
            body_data = bytearray()

            fields = {
                'model': clean_model,
                'prompt': f'{control_prompt}\n\n{prompt}'.strip(),
                'aspect_ratio': aspect_ratio,
                'image_size': _image_quality_to_label(config.get('imageQuality')),
                'response_format': 'b64_json',
            }

            for k, v in fields.items():
                body_data.extend(f"--{boundary}\r\n".encode('utf-8'))
                body_data.extend(f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode('utf-8'))
                body_data.extend(f"{v}\r\n".encode('utf-8'))

            # Add reference image file
            body_data.extend(f"--{boundary}\r\n".encode('utf-8'))
            body_data.extend(f'Content-Disposition: form-data; name="image"; filename="reference.png"\r\n'.encode('utf-8'))
            body_data.extend(b'Content-Type: image/png\r\n\r\n')
            body_data.extend(ref_bytes)
            body_data.extend(b"\r\n")

            body_data.extend(f"--{boundary}--\r\n".encode('utf-8'))

            if sys.stdout:
                print(
                    f"[FRAME SEQUENCE] Image-to-Image edit via /images/edits (multipart) (attempt {attempt+1}/{max_attempts}): "
                    f"{os.path.basename(reference_path)} ({len(ref_bytes)} bytes) -> {clean_model}"
                )

            req = urllib.request.Request(
                f'{base_url}/images/edits',
                data=bytes(body_data),
                headers={
                    'Authorization': f'Bearer {api_key}',
                    'Content-Type': f'multipart/form-data; boundary={boundary}',
                },
                method='POST',
            )
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=360) as resp:
                data = json.loads(resp.read().decode('utf-8'))

            if not data.get('data'):
                raise RuntimeError('image-to-image response contained no image data')
            _decode_or_download_image(data['data'][0], target_path, config)
            return False  # Success, not a fallback

        except Exception as e:
            if sys.stdout:
                print(f"[FRAME SEQUENCE] Image edit attempt {attempt+1}/{max_attempts} failed: {e}")
            if attempt < max_attempts - 1:
                import time
                time.sleep(2.0 + attempt * 2.0)
            else:
                # All attempts failed, fail fast
                raise RuntimeError(f"All image-to-image edit attempts failed. Last error: {e}")


def generate_frame_sequence(config, title, prompt_block, on_progress=None, target_sequences=None):
    images, videos = _parse_prompt_slots(prompt_block)
    prompts = [{'index': idx, 'prompt': images[idx]} for idx in sorted(images)]
    if not prompts:
        raise RuntimeError('未在 prompt_block 中找到任何 图片 N: 提示词')

    project_dir = _get_project_dir(title)
    frames_dir = os.path.join(project_dir, 'frames')
    os.makedirs(frames_dir, exist_ok=True)

    manifest_path = os.path.join(project_dir, 'manifest.json')
    manifest = {
        'title': title,
        'created_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'method': 'A_single_chain',
        'aspect_ratio': config.get('imageAspectRatio') or '9:16',
        'image_size': _image_quality_to_label(config.get('imageQuality')),
        'control_prompt': IMG2IMG_CONTROL_PROMPT,
        'frames': [],
    }

    if os.path.exists(manifest_path):
        try:
            with open(manifest_path, 'r', encoding='utf-8') as f:
                existing_manifest = json.load(f)
                if isinstance(existing_manifest, dict) and 'frames' in existing_manifest:
                    manifest['frames'] = existing_manifest['frames']
                    manifest['created_at'] = existing_manifest.get('created_at', manifest['created_at'])
        except Exception:
            pass

    if on_progress:
        total_to_generate = len(target_sequences) if target_sequences is not None else len(prompts)
        on_progress('start', {'total': total_to_generate})

    manifest_frames_by_seq = {f['sequence']: f for f in manifest['frames']}

    previous_path = None
    generated_count = 0
    lineage_degraded = False

    for seq, item in enumerate(prompts, start=1):
        filename = f'img_{seq:03d}.webp'
        target_path = os.path.join(frames_dir, filename)
        
        should_generate = True
        if target_sequences is not None:
            should_generate = seq in target_sequences

        if not should_generate:
            if os.path.exists(target_path):
                previous_path = target_path
                existing_frame = manifest_frames_by_seq.get(seq)
                if existing_frame and existing_frame.get('quality_gate') == 'i2i_fallback_degraded':
                    lineage_degraded = True
            continue

        # If the file already exists on disk and we are not doing a specific target retry/regeneration,
        # we can skip the external API call and use the existing file immediately.
        already_exists = os.path.exists(target_path) and os.path.getsize(target_path) > 0
        skip_api_call = already_exists and (target_sequences is None)

        model = ""
        retries = 0
        vlm_qa_failed = False
        vlm_qa_reason = None

        if not skip_api_call:
            use_text_generation = (seq == 1 or not previous_path or not os.path.exists(previous_path))
            model = _image_generation_model(config) if use_text_generation else _image_edit_model(config)
            reference = previous_path if not use_text_generation else None
            
            is_fallback = False
            ctrl_prompt = IMG2IMG_CONTROL_PROMPT
            try:
                if use_text_generation:
                    _generate_text_image(config, item['prompt'], target_path)
                    lineage_degraded = False
                else:
                    prompt_text = item['prompt'].lower()
                    is_camera_moving = any(kw in prompt_text for kw in [
                        'push-in', 'push in', 'forward-pushing', 'forward pushing', 'dolly',
                        'camera moves', 'camera relocates', 'crosses the sill', 'sill-handoff',
                        'camera advances', 'zoom-in', 'zoom in', 'closer view'
                    ])
                    ctrl_prompt = IMG2IMG_BRIDGE_CONTROL_PROMPT if is_camera_moving else IMG2IMG_CONTROL_PROMPT
                    is_fallback = _generate_image_edit(config, item['prompt'], previous_path, target_path, control_prompt=ctrl_prompt)
                    if is_fallback:
                        lineage_degraded = True
            except Exception:
                retries += 1
                if use_text_generation:
                    _generate_text_image(config, item['prompt'], target_path)
                    lineage_degraded = False
                else:
                    prompt_text = item['prompt'].lower()
                    is_camera_moving = any(kw in prompt_text for kw in [
                        'push-in', 'push in', 'forward-pushing', 'forward pushing', 'dolly',
                        'camera moves', 'camera relocates', 'crosses the sill', 'sill-handoff',
                        'camera advances', 'zoom-in', 'zoom in', 'closer view'
                    ])
                    ctrl_prompt = IMG2IMG_BRIDGE_CONTROL_PROMPT if is_camera_moving else IMG2IMG_CONTROL_PROMPT
                    is_fallback = _generate_image_edit(config, item['prompt'], previous_path, target_path, control_prompt=ctrl_prompt)
                    if is_fallback:
                        lineage_degraded = True

            # Run VLM visual QA check on successful edits
            if seq > 1 and not use_text_generation and os.path.exists(previous_path) and os.path.exists(target_path):
                video_prompt = videos.get(seq - 1, "")
                if video_prompt:
                    vlm_pass, vlm_reason = run_vlm_qa_check(config, previous_path, target_path, video_prompt)
                    if not vlm_pass:
                        if sys.stdout:
                            print(f"[VLM QA] Beat {seq-1} (IMAGE {seq}) failed VLM check: {vlm_reason}. Retrying generation...")
                        # Retry generation up to 2 times
                        for retry_attempt in range(2):
                            try:
                                is_fallback = _generate_image_edit(config, item['prompt'], previous_path, target_path, control_prompt=ctrl_prompt)
                                if is_fallback:
                                    lineage_degraded = True
                                vlm_pass, vlm_reason = run_vlm_qa_check(config, previous_path, target_path, video_prompt)
                                if vlm_pass:
                                    if sys.stdout:
                                        print(f"[VLM QA] Beat {seq-1} passed VLM check on retry {retry_attempt+1}!")
                                    break
                                else:
                                    if sys.stdout:
                                        print(f"[VLM QA] Beat {seq-1} failed VLM check on retry {retry_attempt+1}: {vlm_reason}")
                            except Exception as retry_err:
                                if sys.stdout:
                                    print(f"[VLM QA] Retry {retry_attempt+1} hit exception: {retry_err}")
                        if not vlm_pass:
                            vlm_qa_failed = True
                            vlm_qa_reason = vlm_reason
                        
            if vlm_qa_failed:
                current_quality_gate = 'vlm_qa_failed'
            else:
                current_quality_gate = 'i2i_fallback_degraded' if lineage_degraded else 'pending_manual_review'
        else:
            reference = previous_path if seq > 1 else None
            existing_frame = manifest_frames_by_seq.get(seq)
            model = existing_frame.get('model', '') if existing_frame else _image_generation_model(config)
            retries = existing_frame.get('retry_count', 0) if existing_frame else 0
            
            current_quality_gate = existing_frame.get('quality_gate', 'pending_manual_review') if existing_frame else 'pending_manual_review'
            vlm_qa_reason = existing_frame.get('vlm_qa_reason') if existing_frame else None
            if current_quality_gate == 'i2i_fallback_degraded':
                lineage_degraded = True

        rel_path = os.path.relpath(target_path, os.path.dirname(os.path.abspath(__file__))).replace('\\', '/')
        
        p_hash = ""
        if seq > 1:
            if skip_api_call and existing_frame and existing_frame.get('parent_hash'):
                p_hash = existing_frame['parent_hash']
            elif previous_path and os.path.exists(previous_path):
                p_hash = _get_file_hash(previous_path)

        frame_info = {
            'slot': item['index'],
            'sequence': seq,
            'file': rel_path,
            'url': '/' + rel_path,
            'prompt': item['prompt'],
            'reference': os.path.relpath(reference, os.path.dirname(os.path.abspath(__file__))).replace('\\', '/') if reference else None,
            'model': model,
            'aspect_ratio': config.get('imageAspectRatio') or '9:16',
            'image_size': _image_quality_to_label(config.get('imageQuality')),
            'retry_count': retries,
            'quality_gate': current_quality_gate,
            'vlm_qa_reason': vlm_qa_reason,
            'parent_hash': p_hash,
        }
        
        manifest_frames_by_seq[seq] = frame_info
        previous_path = target_path
        generated_count += 1

        if on_progress:
            on_progress('frame', {'frame': frame_info, 'current': generated_count, 'total': total_to_generate})

    manifest['frames'] = [manifest_frames_by_seq[s] for s in sorted(manifest_frames_by_seq.keys())]
    update_manifest_stale_status(manifest, project_dir)

    manifest_path = os.path.join(project_dir, 'manifest.json')
    with open(manifest_path, 'w', encoding='utf-8') as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    manifest['manifest'] = '/' + os.path.relpath(manifest_path, os.path.dirname(os.path.abspath(__file__))).replace('\\', '/')
    manifest['project_dir'] = os.path.abspath(project_dir)
    return manifest


def _run_async_image_generation(task_id, base_url, api_key, payload):
    try:
        import urllib.request
        import json
        data = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(
            f'{base_url}/images/generations',
            data=data,
            headers={
                'Authorization': f'Bearer {api_key}',
                'Content-Type': 'application/json',
            },
            method='POST',
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=180) as resp:
            resp_data = json.loads(resp.read().decode('utf-8'))
        resp_data = _save_image_station_result(resp_data)
        with IMAGE_TASKS_LOCK:
            IMAGE_TASKS[task_id] = {'status': 'completed', 'result': resp_data, 'error': None}
    except urllib.error.HTTPError as e:
        detail = ''
        try:
            detail = e.read().decode('utf-8')[:500]
        except Exception: pass
        error_msg = f'Image API HTTP {e.code}: {detail}'
        with IMAGE_TASKS_LOCK:
            IMAGE_TASKS[task_id] = {'status': 'failed', 'result': None, 'error': error_msg}
    except Exception as e:
        error_msg = str(e)
        with IMAGE_TASKS_LOCK:
            IMAGE_TASKS[task_id] = {'status': 'failed', 'result': None, 'error': error_msg}


def _run_async_image_edit(task_id, base_url, api_key, body_data, boundary):
    try:
        import urllib.request
        import json
        req = urllib.request.Request(
            f'{base_url}/images/edits',
            data=body_data,
            headers={
                'Authorization': f'Bearer {api_key}',
                'Content-Type': f'multipart/form-data; boundary={boundary}',
            },
            method='POST',
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=180) as resp:
            resp_data = json.loads(resp.read().decode('utf-8'))
        resp_data = _save_image_station_result(resp_data)
        with IMAGE_TASKS_LOCK:
            IMAGE_TASKS[task_id] = {'status': 'completed', 'result': resp_data, 'error': None}
    except urllib.error.HTTPError as e:
        detail = ''
        try:
            detail = e.read().decode('utf-8')[:500]
        except Exception: pass
        error_msg = f'Image API HTTP {e.code}: {detail}'
        with IMAGE_TASKS_LOCK:
            IMAGE_TASKS[task_id] = {'status': 'failed', 'result': None, 'error': error_msg}
    except Exception as e:
        error_msg = str(e)
        with IMAGE_TASKS_LOCK:
            IMAGE_TASKS[task_id] = {'status': 'failed', 'result': None, 'error': error_msg}


