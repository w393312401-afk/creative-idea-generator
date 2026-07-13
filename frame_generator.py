import os
import sys
import json
import io
import time
import socket
import shutil
import tempfile
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
    IMG2IMG_CONTROL_PROMPT, IMG2IMG_BRIDGE_CONTROL_PROMPT,
    IMAGE_TASKS, IMAGE_TASKS_LOCK
)
from manifest_store import QualityGate



class QuotaExhaustedError(RuntimeError):
    """Raised when the upstream API quota is exhausted; retrying is pointless."""
    pass


def _execute_request_with_retry(req, opener=None, timeout=None, max_attempts=5, initial_delay=2.0):
    import random
    if opener is None:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    
    last_exception = None
    delay = initial_delay
    
    for attempt in range(max_attempts):
        try:
            url_str = req.full_url if hasattr(req, 'full_url') else str(req)
            if sys.stdout:
                print(f"[HTTP] Sending request to {url_str} (attempt {attempt+1}/{max_attempts})")
            
            with opener.open(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            last_exception = e
            detail = ''
            try:
                detail = e.read().decode('utf-8')[:800]
            except Exception:
                pass
            
            if sys.stdout:
                print(f"[HTTP] Attempt {attempt+1}/{max_attempts} failed with HTTP {e.code}: {detail}")
            
            # Detect quota exhaustion (direct 429 or 502 wrapping upstream 429)
            # and fail immediately – retrying only wastes quota.
            quota_signal = (
                "QUOTA_EXHAUSTED" in detail
                or "RESOURCE_EXHAUSTED" in detail
                or "capacity on this model" in detail
                or "quotaResetDelay" in detail
            )
            if quota_signal:
                err_msg = "您的图片生成配额已耗尽 (QUOTA_EXHAUSTED)。"
                try:
                    # The 502 body wraps the upstream 429 JSON, find "message"
                    err_json = json.loads(detail)
                    inner = err_json.get('error', {})
                    msg = inner.get('message') or ""
                    # Sometimes the real 429 JSON is nested deeper
                    if not msg:
                        m = re.search(r'"message":\s*"([^"]+)"', detail)
                        if m:
                            msg = m.group(1)
                    if msg:
                        err_msg += f" {msg}"
                    # Extract quota reset delay for user-friendly message
                    delay_m = re.search(r'quotaResetDelay[":\s]+([^,\}"]+)', detail)
                    if delay_m:
                        err_msg += f" Quota resets in: {delay_m.group(1).strip()}"
                except Exception:
                    m = re.search(r'"message":\s*"([^"]+)"', detail)
                    if m:
                        err_msg += f" {m.group(1)}"
                raise QuotaExhaustedError(err_msg)

            # Retry on 429 and 5xx errors
            if e.code in (429, 500, 502, 503, 504):
                if attempt < max_attempts - 1:
                    sleep_time = delay
                    retry_after = e.headers.get('Retry-After')
                    if retry_after:
                        try:
                            sleep_time = float(retry_after)
                        except ValueError:
                            pass
                    else:
                        sleep_time = delay * (1.5 ** attempt) + random.uniform(0.5, 1.5)
                    
                    if sys.stdout:
                        print(f"[HTTP] Rate-limited or server error. Retrying in {sleep_time:.2f} seconds...")
                    time.sleep(sleep_time)
                    continue
            raise e
        except urllib.error.URLError as e:
            last_exception = e
            if sys.stdout:
                print(f"[HTTP] Attempt {attempt+1}/{max_attempts} failed with URLError: {e.reason}")
            if attempt < max_attempts - 1:
                sleep_time = delay * (1.5 ** attempt) + random.uniform(0.5, 1.5)
                time.sleep(sleep_time)
                continue
            raise e
        except socket.timeout as e:
            last_exception = e
            if sys.stdout:
                print(f"[HTTP] Attempt {attempt+1}/{max_attempts} failed with socket timeout")
            if attempt < max_attempts - 1:
                sleep_time = delay * (1.5 ** attempt) + random.uniform(0.5, 1.5)
                time.sleep(sleep_time)
                continue
            raise e
        except Exception as e:
            last_exception = e
            if sys.stdout:
                print(f"[HTTP] Attempt {attempt+1}/{max_attempts} failed with unexpected error: {e}")
            if attempt < max_attempts - 1:
                sleep_time = delay * (1.5 ** attempt) + random.uniform(0.5, 1.5)
                time.sleep(sleep_time)
                continue
            raise e
            
    if last_exception:
        raise last_exception


def _get_file_hash(filepath):
    import hashlib
    h = hashlib.sha256()
    try:
        with open(filepath, 'rb') as f:
            for chunk in iter(lambda: f.read(8192), b''):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return ""


def update_manifest_stale_status(manifest, project_dir):
    if 'merged_video' in manifest:
        del manifest['merged_video']
    if 'videos' in manifest:
        manifest['videos'] = []


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
    resp_bytes = _execute_request_with_retry(req, opener=opener, timeout=timeout)
    return json.loads(resp_bytes.decode('utf-8'))


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
    from prompt_pipeline import _parse_prompt_slots
    images, _ = _parse_prompt_slots(block)
    # _parse_prompt_slots returns {'body','meta'} dicts; unwrap so 'prompt' is always the
    # plain prose (a dict here would leak its repr into the image-model request) and 'meta'
    # is at the top level where generate_frame_sequence reads the BRIDGE flag.
    items = []
    for idx in sorted(images):
        slot = images[idx]
        body = slot['body'] if isinstance(slot, dict) else slot
        meta = slot.get('meta', '') if isinstance(slot, dict) else ''
        items.append({'index': idx, 'prompt': body, 'meta': meta})
    return items


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
        image_bytes = _execute_request_with_retry(url, opener=opener, timeout=180)

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
                image_bytes = _execute_request_with_retry(url, opener=opener, timeout=180)
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
    resp_bytes = _execute_request_with_retry(req, opener=opener, timeout=timeout)
    return json.loads(resp_bytes.decode('utf-8'))


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
    resp_bytes = _execute_request_with_retry(req, opener=opener, timeout=timeout)
    return json.loads(resp_bytes.decode('utf-8'))


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
            resp_bytes = _execute_request_with_retry(req, opener=opener, timeout=360)
            data = json.loads(resp_bytes.decode('utf-8'))

            if not data.get('data'):
                raise RuntimeError('image-to-image response contained no image data')
            _decode_or_download_image(data['data'][0], target_path, config)
            return False  # Success, not a fallback

        except QuotaExhaustedError:
            # Quota is gone – no point retrying with any attempt count, re-raise immediately
            raise
        except Exception as e:
            if sys.stdout:
                print(f"[FRAME SEQUENCE] Image edit attempt {attempt+1}/{max_attempts} failed: {e}")
            if attempt < max_attempts - 1:
                import time
                time.sleep(2.0 + attempt * 2.0)
            else:
                # All attempts failed, fail fast
                raise RuntimeError(f"All image-to-image edit attempts failed. Last error: {e}")


# ════════════════════════════════════════════════════════════════════
# Google FX UI 自动化帧序列生成（2026-07-04 新增，config.imageBackend == 'google_fx'）
# ════════════════════════════════════════════════════════════════════
# 复用外部 AdsPower 浏览器自动化脚本 services/google_fx_image.py（labs.google Flow 画布）：
#   · 单次批量 ≤5 张，批内自动链式图生图（第 N+1 张自动挂第 N 张为参考）；
#   · 跨批次/单帧重试的续链：外部脚本挂参考只认「文件名里的画布 UUID」，
#     所以每帧除 webp 外把原始 jpg（文件名含 UUID）留档到 frames/fx_src/；
#   · 每次调用用唯一临时 output_path，避免命中外部脚本的 dedupe 结果缓存；
#   · 与 API 路径一致：逐帧 VLM QA，失败改写提示词单帧重生（≤2 次）。
# 改动外部脚本或本文件都需重启 SPARK 进程。

_FX_CHUNK_SIZE = 5  # 外部脚本单次批量上限（google_fx_image 内部 prompts[:5]）

_FX_UUID_RE = re.compile(r'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})')


def _get_google_fx_image_service():
    adspower_path = SERVER_CONFIG.get('adspowerPath') or 'C:\\Users\\video\\Desktop\\N8N-main\\Adspower\\AI\\core'
    if adspower_path not in sys.path:
        sys.path.append(adspower_path)
    import services.google_fx as google_fx
    import models
    return google_fx, models


def _fx_image_model(config):
    # 外部 _normalize_model_name 只认 "Nano Banana Pro" / "Nano Banana 2" / "Imagen 4"
    # 及其别名；未知名称会静默落到默认值，所以这里给一个确定的合法默认。
    return (config.get('googleFxImageModel') or 'Nano Banana 2').strip()


def _fx_extract_uuid(path_or_url):
    m = _FX_UUID_RE.search(os.path.basename(str(path_or_url or '')))
    return m.group(1) if m else None


def plan_fx_chunks(seqs, chunk_size=_FX_CHUNK_SIZE):
    """把待生成的帧序号切成可交给外部批量脚本的批次（纯函数，可单测）。

    只有「连续」的序号才能进同一批：批内链式参考是 外部脚本按提交顺序自动挂前一张,
    序号断开意味着中间帧已存在/不重生，链必须重新从 fx_src 留档接起。
    """
    chunks = []
    cur = []
    for s in sorted(seqs):
        if cur and (s != cur[-1] + 1 or len(cur) >= chunk_size):
            chunks.append(cur)
            cur = []
        cur.append(s)
    if cur:
        chunks.append(cur)
    return chunks


def _fx_src_dir(frames_dir):
    d = os.path.join(frames_dir, 'fx_src')
    os.makedirs(d, exist_ok=True)
    return d


def _fx_find_ref_for(frames_dir, seq):
    """返回第 seq-1 帧留档的 UUID jpg 路径（用于挂参考续链）；找不到返回 None。"""
    if seq <= 1:
        return None
    src_dir = os.path.join(frames_dir, 'fx_src')
    if not os.path.isdir(src_dir):
        return None
    prefix = f'img_{seq - 1:03d}_'
    for name in sorted(os.listdir(src_dir)):
        if name.startswith(prefix) and name.lower().endswith('.jpg') and _fx_extract_uuid(name):
            return os.path.join(src_dir, name)
    return None


def _fx_store_frame(src_path, frames_dir, seq):
    """外部脚本下载的原始 jpg → frames/img_NNN.webp，原始文件按
    img_NNN_<uuid>.jpg 留档到 frames/fx_src/（同槽位旧档先清掉，防止
    重试后按前缀找参考时命中旧 UUID）。返回 (webp_path, fx_src_path, uuid)。"""
    target_path = os.path.join(frames_dir, f'img_{seq:03d}.webp')
    with Image.open(src_path) as img:
        img.convert('RGB').save(target_path, format='WEBP', quality=80)

    uuid_str = _fx_extract_uuid(src_path)
    src_dir = _fx_src_dir(frames_dir)
    prefix = f'img_{seq:03d}_'
    for old in os.listdir(src_dir):
        if old.startswith(prefix):
            try:
                os.remove(os.path.join(src_dir, old))
            except Exception:
                pass
    fx_src_path = os.path.join(src_dir, f'{prefix}{uuid_str or "nouuid"}.jpg')
    shutil.copyfile(src_path, fx_src_path)
    return target_path, fx_src_path, uuid_str


def _fx_generate_batch(google_fx, models, config, prompt_texts, ref_path):
    """调用外部批量生图脚本一次，返回 (本地文件路径列表, 临时目录)。

    ref_path：上一帧留档 jpg（首帧/无续链传 None）。
    外部脚本对失败的单张会静默跳过导致列表变短，而批内链式参考使得
    prompt↔图片 的对应关系无法事后修复，所以数量不齐一律按失败抛出。
    调用方负责在把图片转存后 shutil.rmtree 临时目录。
    """
    temp_out = tempfile.mkdtemp(prefix='spark_fx_img_')
    req = models.ImageBatchRequest(
        prompts=list(prompt_texts),
        images=[ref_path] if ref_path else [],
        ratio=config.get('imageAspectRatio') or '9:16',
        model=_fx_image_model(config),
        output_path=temp_out,  # 每次唯一，绕开外部 dedupe 缓存（同提示词重试不会拿到旧图）
    )
    result = google_fx._generate_images_batch_google_fx(req)
    if not isinstance(result, dict) or result.get('status') != 'success':
        shutil.rmtree(temp_out, ignore_errors=True)
        raise RuntimeError(f"Google FX 批量生图失败: {(result or {}).get('message') or '未知错误'}")
    paths = [p for p in (result.get('image_urls') or []) if isinstance(p, str) and os.path.exists(p)]
    if len(paths) < len(prompt_texts):
        shutil.rmtree(temp_out, ignore_errors=True)
        raise RuntimeError(
            f"Google FX 批量生图不完整: 期望 {len(prompt_texts)} 张，实际落盘 {len(paths)} 张，"
            f"已放弃本批结果（批内链式对应关系无法修复）"
        )
    return paths, temp_out


def _fx_apply_vlm_fixes(config, orig_prompt, vlm_reason, is_bridge, is_last):
    """按 API 路径同款流程，用 VLM 反馈改写提示词并套用主动修复。"""
    from prompt_pipeline import (
        fix_image_prompt_with_vlm_feedback, clean_prompt_text,
        fix_image_clean_frame_proactive, fix_horizon_line,
        fix_camera_contradictions, fix_rhma_blur, fix_camera_dna,
    )
    corrected = fix_image_prompt_with_vlm_feedback(config, orig_prompt, vlm_reason)
    corrected = clean_prompt_text(corrected)
    corrected = fix_image_clean_frame_proactive(corrected)
    corrected = fix_horizon_line(corrected)
    corrected = fix_camera_contradictions(corrected, is_bridge=is_bridge)
    corrected = fix_rhma_blur(corrected, is_last=is_last)

    camera_dna = ""
    if ":" in orig_prompt:
        parts = orig_prompt.split(":", 1)
        if any(kw in parts[0].lower() for kw in ['tripod', 'camera', 'shot', 'height', 'view']):
            camera_dna = parts[0] + ":"
    if camera_dna:
        corrected = fix_camera_dna(corrected, camera_dna)
    return corrected


def _generate_frame_sequence_google_fx(config, title, prompt_block, on_progress=None, target_sequences=None):
    import builtins
    builtins.google_fx_cancelled = False
    rotate_requests = config.get('googleFxIpRotateRequests')
    if rotate_requests is not None:
        os.environ['MIYA_ROTATE_THRESHOLD'] = str(rotate_requests)
    from prompt_pipeline import _parse_prompt_slots, run_frame_qa_check
    images, videos = _parse_prompt_slots(prompt_block)

    prompts = []
    for idx in sorted(images):
        item = images[idx]
        body = item['body'] if isinstance(item, dict) else item
        meta = item.get('meta', '') if isinstance(item, dict) else ''
        prompts.append({'index': idx, 'prompt': body, 'meta': meta})
    if not prompts:
        raise RuntimeError('未在 prompt_block 中找到任何 图片 N: 提示词')

    def _check_cancel():
        if on_progress and on_progress('cancel_check', None):
            raise ConnectionError('用户取消了帧序列生成')

    project_dir = _get_project_dir(title)
    frames_dir = os.path.join(project_dir, 'frames')
    os.makedirs(frames_dir, exist_ok=True)

    google_fx, fx_models = _get_google_fx_image_service()
    fx_model = _fx_image_model(config)

    manifest_path = os.path.join(project_dir, 'manifest.json')
    manifest = {
        'title': title,
        'created_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'method': 'A_single_chain',
        'backend': 'google_fx',
        'aspect_ratio': config.get('imageAspectRatio') or '9:16',
        'image_size': _image_quality_to_label(config.get('imageQuality')),
        'control_prompt': '',  # Flow UI 链式参考不走 IMG2IMG_CONTROL_PROMPT
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

    manifest_frames_by_seq = {f['sequence']: f for f in manifest['frames']}
    prompts_by_seq = {seq: item for seq, item in enumerate(prompts, start=1)}
    all_seqs = sorted(prompts_by_seq.keys())

    def _webp_path(seq):
        return os.path.join(frames_dir, f'img_{seq:03d}.webp')

    def _frame_exists(seq):
        p = _webp_path(seq)
        return os.path.exists(p) and os.path.getsize(p) > 0

    # 任务分解：full run = 已有帧直接复用（断点续传），缺失帧成批生成；
    # target 模式 = 只重生指定序号，其余帧不动也不发事件（与 API 路径一致）。
    if target_sequences is not None:
        wanted = {int(s) for s in target_sequences}
        skip_seqs = []
        gen_seqs = [s for s in all_seqs if s in wanted]
    else:
        skip_seqs = [s for s in all_seqs if _frame_exists(s)]
        gen_seqs = [s for s in all_seqs if not _frame_exists(s)]

    total_to_generate = len(skip_seqs) + len(gen_seqs)
    if on_progress:
        on_progress('start', {'total': total_to_generate})

    def _save_manifest():
        manifest['frames'] = [manifest_frames_by_seq[s] for s in sorted(manifest_frames_by_seq.keys())]
        with open(manifest_path, 'w', encoding='utf-8') as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)

    generated_count = 0

    def _emit_frame(frame_info):
        nonlocal generated_count
        generated_count += 1
        if on_progress:
            on_progress('frame', {'frame': frame_info, 'current': generated_count, 'total': total_to_generate})

    def _run_vlm_qa(seq, item, is_bridge, ref_path):
        """逐帧 VLM 质检 + 失败改写重生（≤2 次）。返回 (quality_gate, vlm_reason)。
        quality_gate 为 'auto_approved' 表示 VLM 自动判定通过；'pending_manual_review'
        表示未跑过任何自动校验（如首帧或上一帧缺失）；'vlm_qa_failed' 表示重试后仍未通过。"""
        if seq <= 1 or not _frame_exists(seq - 1):
            return QualityGate.PENDING_MANUAL_REVIEW, None
        video_item = videos.get(seq - 1, "")
        video_prompt = video_item['body'] if isinstance(video_item, dict) else video_item
        if not video_prompt:
            return QualityGate.PENDING_MANUAL_REVIEW, None

        prev_path = _webp_path(seq - 1)
        target_path = _webp_path(seq)
        vlm_pass, vlm_reason = run_frame_qa_check(config, _webp_path(1), prev_path, target_path, video_prompt, seq, is_bridge=is_bridge)
        if vlm_pass:
            return QualityGate.AUTO_APPROVED, None

        if sys.stdout:
            print(f"[VLM QA][FX] Beat {seq-1} (IMAGE {seq}) failed VLM check: {vlm_reason}. Retrying via Google FX...")
        for retry_attempt in range(2):
            _check_cancel()
            try:
                if on_progress:
                    on_progress('frame_retry', {
                        'slot': item['index'],
                        'sequence': seq,
                        'attempt': retry_attempt + 1,
                        'reason': vlm_reason,
                    })
                corrected = _fx_apply_vlm_fixes(
                    config, item['prompt'], vlm_reason,
                    is_bridge=is_bridge, is_last=(seq == len(prompts)),
                )
                if sys.stdout:
                    print(f"[VLM QA][FX] Attempt {retry_attempt+1} corrected prompt: {corrected}")
                retry_paths, retry_tmp = _fx_generate_batch(google_fx, fx_models, config, [corrected], ref_path)
                try:
                    _fx_store_frame(retry_paths[0], frames_dir, seq)
                finally:
                    shutil.rmtree(retry_tmp, ignore_errors=True)
                vlm_pass, vlm_reason = run_frame_qa_check(config, _webp_path(1), prev_path, target_path, video_prompt, seq, is_bridge=is_bridge)
                if vlm_pass:
                    if sys.stdout:
                        print(f"[VLM QA][FX] Beat {seq-1} passed VLM check on retry {retry_attempt+1}!")
                    item['prompt'] = corrected  # 回写 manifest 用的提示词
                    return QualityGate.AUTO_APPROVED, None
                if sys.stdout:
                    print(f"[VLM QA][FX] Beat {seq-1} failed VLM check on retry {retry_attempt+1}: {vlm_reason}")
            except ConnectionError:
                raise
            except Exception as retry_err:
                if sys.stdout:
                    print(f"[VLM QA][FX] Retry {retry_attempt+1} hit exception: {retry_err}")
        return QualityGate.VLM_QA_FAILED, vlm_reason

    def _record_frame(seq, item, fx_src_path, fx_uuid, quality_gate, vlm_reason):
        webp = _webp_path(seq)
        rel_path = os.path.relpath(webp, os.path.dirname(os.path.abspath(__file__))).replace('\\', '/')
        prev_path = _webp_path(seq - 1) if seq > 1 else None
        reference = prev_path if (prev_path and os.path.exists(prev_path)) else None
        frame_info = {
            'slot': item['index'],
            'sequence': seq,
            'file': rel_path,
            'url': '/' + rel_path,
            'prompt': item['prompt'],
            'meta': item.get('meta', ''),
            'reference': os.path.relpath(reference, os.path.dirname(os.path.abspath(__file__))).replace('\\', '/') if reference else None,
            'model': fx_model,
            'backend': 'google_fx',
            'fx_uuid': fx_uuid,
            'fx_src': os.path.relpath(fx_src_path, os.path.dirname(os.path.abspath(__file__))).replace('\\', '/') if fx_src_path else None,
            'aspect_ratio': config.get('imageAspectRatio') or '9:16',
            'image_size': _image_quality_to_label(config.get('imageQuality')),
            'retry_count': 0,
            'quality_gate': quality_gate,
            'vlm_qa_reason': vlm_reason,
            'parent_hash': _get_file_hash(reference) if reference else "",
        }
        manifest_frames_by_seq[seq] = frame_info
        _save_manifest()  # 浏览器批量任务动辄数分钟，逐帧落盘保证进度可恢复
        _emit_frame(frame_info)

    chunks = plan_fx_chunks(gen_seqs)
    chunk_by_start = {c[0]: c for c in chunks}
    done_seqs = set()

    for seq in all_seqs:
        if seq in done_seqs:
            continue
        if seq in skip_seqs:
            # 断点续传：直接复用已有帧与其 manifest 条目
            existing = manifest_frames_by_seq.get(seq)
            if not existing:
                item = prompts_by_seq[seq]
                own_src = _fx_find_ref_for(frames_dir, seq + 1)  # 本帧自己的留档（img_{seq}_*）
                rel_path = os.path.relpath(_webp_path(seq), os.path.dirname(os.path.abspath(__file__))).replace('\\', '/')
                existing = {
                    'slot': item['index'], 'sequence': seq,
                    'file': rel_path, 'url': '/' + rel_path,
                    'prompt': item['prompt'], 'meta': item.get('meta', ''),
                    'reference': None, 'model': fx_model, 'backend': 'google_fx',
                    'fx_uuid': _fx_extract_uuid(own_src) if own_src else None,
                    'fx_src': os.path.relpath(own_src, os.path.dirname(os.path.abspath(__file__))).replace('\\', '/') if own_src else None,
                    'aspect_ratio': config.get('imageAspectRatio') or '9:16',
                    'image_size': _image_quality_to_label(config.get('imageQuality')),
                    'retry_count': 0, 'quality_gate': 'pending_manual_review',
                    'vlm_qa_reason': None, 'parent_hash': '',
                }
                manifest_frames_by_seq[seq] = existing
            _emit_frame(existing)
            done_seqs.add(seq)
            continue

        chunk = chunk_by_start.get(seq)
        if not chunk:
            continue  # target 模式下不在重生名单里的序号
        _check_cancel()

        ref_path = _fx_find_ref_for(frames_dir, chunk[0])
        if chunk[0] > 1 and not ref_path and sys.stdout:
            print(
                f"[FRAME SEQUENCE][FX] 第 {chunk[0]} 帧找不到上一帧的 UUID 留档 "
                f"(frames/fx_src/img_{chunk[0]-1:03d}_*.jpg)，本批将以无参考模式起链，画面连续性可能下降"
            )

        chunk_prompts = [prompts_by_seq[s]['prompt'] for s in chunk]
        if on_progress:
            for s in chunk:
                item = prompts_by_seq[s]
                on_progress('frame_start', {
                    'slot': item['index'],
                    'sequence': s,
                    'total': total_to_generate,
                })
        if sys.stdout:
            print(f"[FRAME SEQUENCE][FX] Google FX 批量生图: 帧 {chunk[0]}~{chunk[-1]} ({len(chunk)} 张), ref={'有' if ref_path else '无'}")
        try:
            local_paths, temp_out = _fx_generate_batch(google_fx, fx_models, config, chunk_prompts, ref_path)
        except ConnectionError:
            raise
        except Exception as e:
            if sys.stdout:
                print(f"[FRAME SEQUENCE][FX] 批量生图失败，3 秒后整批重试一次: {e}")
            _check_cancel()
            time.sleep(3.0)
            local_paths, temp_out = _fx_generate_batch(google_fx, fx_models, config, chunk_prompts, ref_path)

        try:
            for offset, s in enumerate(chunk):
                item = prompts_by_seq[s]
                is_bridge = ('BRIDGE' in item.get('meta', '').upper())
                _, fx_src_path, fx_uuid = _fx_store_frame(local_paths[offset], frames_dir, s)
                if s > 1:
                    first_frame_path = _webp_path(1)
                    target_path = _webp_path(s)
                    if os.path.exists(first_frame_path) and os.path.exists(target_path):
                        _match_color_lab(target_path, first_frame_path, target_path)
                prev_ref = _fx_find_ref_for(frames_dir, s)
                quality_gate, vlm_reason = _run_vlm_qa(s, item, is_bridge, prev_ref)
                # QA 重生会替换留档，重新定位当前帧的 fx_src
                cur_ref = _fx_find_ref_for(frames_dir, s + 1)
                if cur_ref:
                    fx_src_path, fx_uuid = cur_ref, _fx_extract_uuid(cur_ref)
                _record_frame(s, item, fx_src_path, fx_uuid, quality_gate, vlm_reason)
                done_seqs.add(s)
        finally:
            shutil.rmtree(temp_out, ignore_errors=True)

    update_manifest_stale_status(manifest, project_dir)
    _save_manifest()
    manifest['manifest'] = '/' + os.path.relpath(manifest_path, os.path.dirname(os.path.abspath(__file__))).replace('\\', '/')
    manifest['project_dir'] = os.path.abspath(project_dir)
    return manifest


def _match_color_lab(source_path, reference_path, output_path):
    """
    Adjusts the color statistics of source to match reference in LAB color space.
    L channel (lightness): 15% blend (allows progressive lighting changes).
    A & B color channels: 85% blend (suppresses pink/magenta color drift).
    """
    try:
        import cv2
        import numpy as np
        
        src = cv2.imread(source_path)
        ref = cv2.imread(reference_path)
        if src is None or ref is None:
            return

        # Convert BGR to LAB
        src_lab = cv2.cvtColor(src, cv2.COLOR_BGR2LAB).astype(np.float32)
        ref_lab = cv2.cvtColor(ref, cv2.COLOR_BGR2LAB).astype(np.float32)

        # Split channels
        s_l, s_a, s_b = cv2.split(src_lab)
        r_l, r_a, r_b = cv2.split(ref_lab)

        # Channel stats
        s_means = [np.mean(s_l), np.mean(s_a), np.mean(s_b)]
        s_stds = [np.std(s_l), np.std(s_a), np.std(s_b)]
        r_means = [np.mean(r_l), np.mean(r_a), np.mean(r_b)]
        r_stds = [np.std(r_l), np.std(r_a), np.std(r_b)]

        adjusted_channels = []
        for i, (s_ch, s_mean, s_std, r_mean, r_std) in enumerate(zip(
            [s_l, s_a, s_b], s_means, s_stds, r_means, r_stds
        )):
            if s_std < 1e-4:
                adjusted_channels.append(s_ch)
                continue
            
            # Reinhard color transfer
            adjusted = (s_ch - s_mean) * (r_std / s_std) + r_mean
            
            # Soft-blend to preserve local lighting transitions
            if i == 0:  # L channel (lightness) - keep most of original L variations
                blend = 0.15 * adjusted + 0.85 * s_ch
            else:       # A & B color channels - transfer color profile
                blend = 0.85 * adjusted + 0.15 * s_ch
            
            adjusted_channels.append(blend)

        # Merge back and convert
        merged = cv2.merge(adjusted_channels)
        merged[:, :, 0] = np.clip(merged[:, :, 0], 0, 100)
        merged[:, :, 1] = np.clip(merged[:, :, 1], -127, 127)
        merged[:, :, 2] = np.clip(merged[:, :, 2], -127, 127)
        
        result_bgr = cv2.cvtColor(merged.astype(np.uint8), cv2.COLOR_LAB2BGR)
        cv2.imwrite(output_path, result_bgr)
    except Exception as e:
        if sys.stdout:
            print(f"[COLOR MATCH] Warning: LAB color matching failed: {e}")


def generate_frame_sequence(config, title, prompt_block, on_progress=None, target_sequences=None):
    if (config.get('imageBackend') or 'api').strip().lower() == 'google_fx':
        return _generate_frame_sequence_google_fx(
            config, title, prompt_block,
            on_progress=on_progress, target_sequences=target_sequences,
        )
    from prompt_pipeline import _parse_prompt_slots, run_frame_qa_check
    images, videos = _parse_prompt_slots(prompt_block)
    
    prompts = []
    for idx in sorted(images):
        item = images[idx]
        body = item['body'] if isinstance(item, dict) else item
        meta = item.get('meta', '') if isinstance(item, dict) else ''
        prompts.append({
            'index': idx,
            'prompt': body,
            'meta': meta
        })
        
    if not prompts:
        raise RuntimeError('未在 prompt_block 中找到任何 图片 N: 提示词')

    def _check_cancel():
        if on_progress and on_progress('cancel_check', None):
            raise ConnectionError('用户取消了帧序列生成')

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
        _check_cancel()
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
        vlm_checked = False
        is_bridge = ('BRIDGE' in item.get('meta', '').upper())

        if not skip_api_call:
            use_text_generation = (seq == 1 or not previous_path or not os.path.exists(previous_path))
            model = _image_generation_model(config) if use_text_generation else _image_edit_model(config)
            reference = previous_path if not use_text_generation else None
            if on_progress:
                on_progress('frame_start', {
                    'slot': item['index'],
                    'sequence': seq,
                    'total': total_to_generate,
                })
            
            is_fallback = False
            ctrl_prompt = IMG2IMG_CONTROL_PROMPT
            try:
                if use_text_generation:
                    _generate_text_image(config, item['prompt'], target_path)
                    lineage_degraded = False
                else:
                    ctrl_prompt = IMG2IMG_BRIDGE_CONTROL_PROMPT if is_bridge else IMG2IMG_CONTROL_PROMPT
                    is_fallback = _generate_image_edit(config, item['prompt'], previous_path, target_path, control_prompt=ctrl_prompt)
                    if is_fallback:
                        lineage_degraded = True
            except QuotaExhaustedError as quota_err:
                # Primary model quota exhausted.
                # Try fallbackImageModel if configured, otherwise raise error.
                fallback_model = config.get('imageEditFallbackModel') or config.get('fallbackImageModel')
                if fallback_model:
                    if sys.stdout:
                        print(f"[FRAME SEQUENCE] Quota exhausted on primary model. "
                              f"Retrying frame {seq} with fallback model: {fallback_model}")
                    fallback_config = dict(config)
                    fallback_config['imageModel'] = fallback_model
                    try:
                        if use_text_generation:
                            _generate_text_image(fallback_config, item['prompt'], target_path)
                            lineage_degraded = False
                            model = _image_generation_model(fallback_config)
                        else:
                            is_fallback = _generate_image_edit(fallback_config, item['prompt'], previous_path, target_path, control_prompt=ctrl_prompt)
                            lineage_degraded = True  # degraded since we switched models mid-sequence
                            model = _image_edit_model(fallback_config)
                    except Exception as fb_err:
                        if not use_text_generation:
                            if sys.stdout:
                                print(f"[FRAME SEQUENCE] Fallback model failed on frame {seq}: {fb_err}. "
                                      f"Using last-resort text-to-image (fallback model) for frame {seq}.")
                            _generate_text_image(fallback_config, item['prompt'], target_path)
                            lineage_degraded = True
                            model = _image_generation_model(fallback_config)
                        else:
                            if sys.stdout:
                                print(f"[FRAME SEQUENCE] Fallback model failed on frame {seq}: {fb_err}.")
                            raise RuntimeError(f"第 {seq} 帧生成失败（主模型配额耗尽，且备用模型也失败）: {fb_err}")
                else:
                    raise quota_err
            except Exception as gen_err:
                if use_text_generation:
                    retries += 1
                    if sys.stdout:
                        print(f"[FRAME SEQUENCE] First frame generation failed: {gen_err}. Retrying text-to-image.")
                    _generate_text_image(config, item['prompt'], target_path)
                    lineage_degraded = False
                else:
                    if sys.stdout:
                        print(f"[FRAME SEQUENCE] Frame {seq} image-to-image generation failed: {gen_err}. Subsequent frames cannot fall back to text-to-image.")
                    raise RuntimeError(f"第 {seq} 帧图生图失败: {gen_err}")

            # Apply LAB color matching to prevent pink drift
            if seq > 1 and os.path.exists(target_path):
                first_frame_path = os.path.join(frames_dir, 'img_001.webp')
                if os.path.exists(first_frame_path):
                    if sys.stdout:
                        print(f"[COLOR MATCH] Aligning frame {seq} color to baseline first frame.")
                    _match_color_lab(target_path, first_frame_path, target_path)

            # Run VLM visual QA check on successful edits
            if seq > 1 and not use_text_generation and os.path.exists(previous_path) and os.path.exists(target_path):
                video_item = videos.get(seq - 1, "")
                video_prompt = video_item['body'] if isinstance(video_item, dict) else video_item
                if video_prompt:
                    vlm_checked = True
                    image_1_path = os.path.join(frames_dir, 'img_001.webp')
                    vlm_pass, vlm_reason = run_frame_qa_check(config, image_1_path, previous_path, target_path, video_prompt, seq, is_bridge=is_bridge)
                    if not vlm_pass:
                        if sys.stdout:
                            print(f"[VLM QA] Beat {seq-1} (IMAGE {seq}) failed VLM check: {vlm_reason}. Retrying generation with VLM feedback...")
                        # Retry generation up to 2 times
                        for retry_attempt in range(2):
                            try:
                                if on_progress:
                                    on_progress('frame_retry', {
                                        'slot': item['index'],
                                        'sequence': seq,
                                        'attempt': retry_attempt + 1,
                                        'reason': vlm_reason,
                                    })
                                # Get corrected prompt from auxModel using VLM feedback
                                from prompt_pipeline import fix_image_prompt_with_vlm_feedback, clean_prompt_text, fix_image_clean_frame_proactive, fix_horizon_line, fix_camera_contradictions, fix_rhma_blur, fix_camera_dna
                                
                                orig_prompt = item['prompt']
                                corrected_prompt = fix_image_prompt_with_vlm_feedback(config, orig_prompt, vlm_reason)
                                
                                # Apply proactive fixes to the corrected prompt
                                corrected_prompt = clean_prompt_text(corrected_prompt)
                                corrected_prompt = fix_image_clean_frame_proactive(corrected_prompt)
                                corrected_prompt = fix_horizon_line(corrected_prompt)
                                corrected_prompt = fix_camera_contradictions(corrected_prompt, is_bridge=is_bridge)
                                corrected_prompt = fix_rhma_blur(corrected_prompt, is_last=(seq == len(prompts)))
                                
                                # Extract camera DNA prefix from the original prompt to preserve it
                                camera_dna = ""
                                if ":" in orig_prompt:
                                    parts = orig_prompt.split(":", 1)
                                    if any(kw in parts[0].lower() for kw in ['tripod', 'camera', 'shot', 'height', 'view']):
                                        camera_dna = parts[0] + ":"
                                
                                if camera_dna:
                                    corrected_prompt = fix_camera_dna(corrected_prompt, camera_dna)
                                
                                if sys.stdout:
                                    print(f"[VLM QA] Attempt {retry_attempt+1} corrected prompt: {corrected_prompt}")
                                
                                is_fallback = _generate_image_edit(config, corrected_prompt, previous_path, target_path, control_prompt=ctrl_prompt)
                                if is_fallback:
                                    lineage_degraded = True
                                first_frame_path = os.path.join(frames_dir, 'img_001.webp')
                                if os.path.exists(first_frame_path) and os.path.exists(target_path):
                                    _match_color_lab(target_path, first_frame_path, target_path)
                                vlm_pass, vlm_reason = run_frame_qa_check(config, first_frame_path, previous_path, target_path, video_prompt, seq, is_bridge=is_bridge)
                                if vlm_pass:
                                    if sys.stdout:
                                        print(f"[VLM QA] Beat {seq-1} passed VLM check on retry {retry_attempt+1}!")
                                    # Update the prompt in our active prompts list so it writes back to manifest!
                                    item['prompt'] = corrected_prompt
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
            elif lineage_degraded:
                current_quality_gate = 'i2i_fallback_degraded'
            elif vlm_checked:
                current_quality_gate = 'auto_approved'
            else:
                current_quality_gate = 'pending_manual_review'
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
            'meta': item.get('meta', ''),
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
        resp_bytes = _execute_request_with_retry(req, opener=opener, timeout=180)
        resp_data = json.loads(resp_bytes.decode('utf-8'))
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
        resp_bytes = _execute_request_with_retry(req, opener=opener, timeout=180)
        resp_data = json.loads(resp_bytes.decode('utf-8'))
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


