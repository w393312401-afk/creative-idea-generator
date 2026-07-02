import os
import sys
import json
import socket
import urllib.request
import urllib.error
import urllib.parse
import re
import base64
import time
import threading
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

# Setup local log file
_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'server.log')
try:
    _log_file = open(_LOG_PATH, 'a', encoding='utf-8', buffering=1)
    _log_file.write(f"\n===== Image Service Station server log opened {datetime.now().isoformat()} =====\n")
    _log_file.flush()
except Exception:
    _log_file = None

class DummyWriter:
    def write(self, *args, **kwargs): pass
    def flush(self, *args, **kwargs): pass

class _Tee:
    def __init__(self, *streams):
        self.streams = [s for s in streams if s is not None]
    def write(self, data):
        for s in self.streams:
            try:
                s.write(data)
                s.flush()
            except Exception: pass
        return len(data) if isinstance(data, str) else 0
    def flush(self):
        for s in self.streams:
            try:
                s.flush()
            except Exception: pass

sys.stdout = _Tee(sys.stdout, _log_file) if (sys.stdout or _log_file) else DummyWriter()
sys.stderr = _Tee(sys.stderr, _log_file) if (sys.stderr or _log_file) else DummyWriter()

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    address_family = socket.AF_INET6
    allow_reuse_address = True
    daemon_threads = True

    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()

PORT = int(os.environ.get('PORT', '8086'))

# Read global server config from the parent directory to share API key
GLOBAL_CONFIG_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'server_config.json')

def load_global_config():
    cfg = {}
    if os.path.exists(GLOBAL_CONFIG_FILE):
        try:
            with open(GLOBAL_CONFIG_FILE, 'r', encoding='utf-8') as f:
                cfg = json.load(f) or {}
        except Exception as e:
            print(f"Warning: could not read global server_config.json ({e})")
    
    # Also check env variables
    env_map = {
        'baseUrl': 'SPARK_BASE_URL', 'apiKey': 'SPARK_API_KEY', 
        'imageModel': 'SPARK_IMAGE_MODEL', 'accessCode': 'SPARK_ACCESS_CODE',
        'geminiDirectApiKey': 'GEMINI_API_KEY', 'geminiDirectImageModel': 'GEMINI_IMAGE_MODEL',
    }
    for k, env in env_map.items():
        v = os.environ.get(env)
        if v:
            cfg[k] = v
    return cfg

SERVER_CONFIG = load_global_config()
SERVER_MANAGED = bool(SERVER_CONFIG.get('apiKey'))
ACCESS_CODE = (SERVER_CONFIG.get('accessCode') or '').strip()


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
        img.save(buf, format='JPEG', quality=90)
        return 'image/jpeg', base64.b64encode(buf.getvalue()).decode('ascii')
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


def effective_config(client_config):
    """Resolve the config. Managed mode prioritizes server secrets."""
    client_config = client_config or {}
    if not SERVER_MANAGED:
        return client_config
    return {
        'baseUrl': SERVER_CONFIG.get('baseUrl') or 'http://127.0.0.1:8046/v1',
        'apiKey': SERVER_CONFIG.get('apiKey') or '',
        'imageModel': SERVER_CONFIG.get('imageModel') or 'gemini-3.1-flash-image',
        'geminiDirectApiKey': SERVER_CONFIG.get('geminiDirectApiKey') or SERVER_CONFIG.get('geminiApiKey') or '',
        'geminiDirectImageModel': SERVER_CONFIG.get('geminiDirectImageModel') or '',
    }

def access_ok(handler):
    if not ACCESS_CODE:
        return True
    auth = handler.headers.get('Authorization') or ''
    if auth.startswith('Bearer '):
        return auth[7:].strip() == ACCESS_CODE
    return False


def normalize_image_quality_label(quality):
    q = (quality or '2K').strip().lower()
    if q in ('4k', 'hd'):
        return '4K'
    if q in ('2k', 'medium'):
        return '2K'
    return '1K'


def image_generation_model_name(model, size, quality):
    if model == 'gpt-image-2':
        return model
    if re.search(r'-\d+-\d+(?:-\d+k)?$', model.lower()):
        return model

    aspect_ratio = (size or '1:1').replace(':', '-')
    quality_label = normalize_image_quality_label(quality)
    if quality_label == '4K':
        return f"{model}-{aspect_ratio}-4k"
    if quality_label == '2K':
        return f"{model}-{aspect_ratio}-2k"
    return f"{model}-{aspect_ratio}"

# Thread-safe store for background image generation/edit tasks
IMAGE_TASKS = {}
IMAGE_TASKS_LOCK = threading.Lock()

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


def _run_async_image_edit(task_id, fields, files, config):
    try:
        import urllib.request
        import json
        import base64
        import re
        import sys
        
        base_url = (config.get('baseUrl') or 'http://127.0.0.1:8046/v1').rstrip('/')
        api_key = config.get('apiKey') or ''

        model = fields.get('model') or config.get('imageModel') or 'gemini-3.1-flash-image'
        if 'nano-banana-2' in model:
            model = model.replace('nano-banana-2', 'gemini-3.1-flash-image')

        if model != 'gpt-image-2':
            clean_model = re.sub(r'-\d+-\d+(?:-\d+k)?$', '', model, flags=re.IGNORECASE)
            clean_model = re.sub(r'-(?:2k|4k)(?:-\d+x\d+)?$', '', clean_model, flags=re.IGNORECASE)
            prompt = fields.get('prompt') or ''
            if fields.get('style'):
                prompt = f"{prompt}\n\nStyle: {fields.get('style')}".strip()

            aspect_ratio = fields.get('aspect_ratio') or fields.get('size') or '1:1'
            if aspect_ratio and aspect_ratio.lower() == 'auto':
                aspect_ratio = None
            image_size = normalize_image_quality_label(fields.get('image_size') or fields.get('quality') or '1K')

            if _gemini_direct_api_key(config):
                try:
                    native_resp = _gemini_native_image_edit(
                        config,
                        clean_model,
                        prompt,
                        files,
                        aspect_ratio,
                        image_size,
                      )
                    if native_resp:
                        with IMAGE_TASKS_LOCK:
                            IMAGE_TASKS[task_id] = {'status': 'completed', 'result': native_resp, 'error': None}
                        return
                except Exception as e:
                    with IMAGE_TASKS_LOCK:
                        IMAGE_TASKS[task_id] = {'status': 'failed', 'result': None, 'error': f'Gemini direct image edit failed: {e}'}
                    return

            final_model = image_generation_model_name(clean_model, aspect_ratio, image_size)
            payload = {
                'model': final_model,
                'prompt': (
                    'IMAGE EDITING MODE. Use the attached reference image as the authoritative source canvas. '
                    'Preserve its composition, camera angle, objects, identity, materials, and layout unless the '
                    'user explicitly asks to change them. Do not create an unrelated text-to-image scene.\n\n'
                    f'{prompt}'
                ),
                'response_format': 'b64_json',
            }
            if aspect_ratio:
                payload['aspect_ratio'] = aspect_ratio
                payload['size'] = aspect_ratio
            if image_size:
                payload['image_size'] = image_size
                payload['quality'] = image_size

            processed_images = []
            for _name, _filename, p_content_type, file_data in files:
                try:
                    import io
                    from PIL import Image

                    img = Image.open(io.BytesIO(file_data)).convert('RGB')
                    if aspect_ratio:
                        img = _crop_to_aspect_ratio(img, aspect_ratio)
                    img.thumbnail((1024, 1024))
                    buf = io.BytesIO()
                    img.save(buf, format='JPEG', quality=88)
                    upload_bytes = buf.getvalue()
                    upload_mime = 'image/jpeg'
                except Exception:
                    upload_bytes = file_data
                    upload_mime = p_content_type or 'image/png'

                image_b64 = base64.b64encode(upload_bytes).decode('ascii')
                processed_images.append({
                    'mime_type': upload_mime,
                    'data': image_b64
                })

            if len(processed_images) == 1:
                payload['image'] = processed_images[0]['data']
            elif len(processed_images) > 1:
                payload['images'] = processed_images

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
            with IMAGE_TASKS_LOCK:
                IMAGE_TASKS[task_id] = {'status': 'completed', 'result': resp_data, 'error': None}
            return

        # Standard OpenAI edits proxy logic
        import uuid
        boundary = f"Boundary-{uuid.uuid4().hex}"
        body_data = bytearray()

        for k, v in fields.items():
            if k == 'model':
                if 'nano-banana-2' in v:
                    v = v.replace('nano-banana-2', 'gemini-3.1-flash-image')
                v = image_generation_model_name(
                    v,
                    fields.get('aspect_ratio') or fields.get('size'),
                    fields.get('image_size') or fields.get('quality')
                )
            elif k == 'image_size':
                v = normalize_image_quality_label(v)
            elif k == 'aspect_ratio' and v.lower() == 'auto':
                continue
            body_data.extend(f"--{boundary}\r\n".encode('utf-8'))
            body_data.extend(f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode('utf-8'))
            body_data.extend(f"{v}\r\n".encode('utf-8'))

        if 'model' not in fields:
            model = config.get('imageModel') or 'gemini-3.1-flash-image'
            if 'nano-banana-2' in model:
                model = model.replace('nano-banana-2', 'gemini-3.1-flash-image')
            model_with_suffix = image_generation_model_name(
                model,
                fields.get('aspect_ratio') or fields.get('size'),
                fields.get('image_size') or fields.get('quality')
            )
            body_data.extend(f"--{boundary}\r\n".encode('utf-8'))
            body_data.extend(f'Content-Disposition: form-data; name="model"\r\n\r\n'.encode('utf-8'))
            body_data.extend(f"{model_with_suffix}\r\n".encode('utf-8'))

        for index, (name, filename, p_content_type, file_data) in enumerate(files):
            upstream_name = 'image' if index == 0 else 'image[]'
            body_data.extend(f"--{boundary}\r\n".encode('utf-8'))
            body_data.extend(f'Content-Disposition: form-data; name="{upstream_name}"; filename="{filename}"\r\n'.encode('utf-8'))
            body_data.extend(f'Content-Type: {p_content_type}\r\n\r\n'.encode('utf-8'))
            body_data.extend(file_data)
            body_data.extend(b"\r\n")

        body_data.extend(f"--{boundary}--\r\n".encode('utf-8'))

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
        with opener.open(req, timeout=180) as resp:
            resp_data = json.loads(resp.read().decode('utf-8'))
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
        with IMAGE_TASKS_LOCK:
            IMAGE_TASKS[task_id] = {'status': 'failed', 'result': None, 'error': str(e)}

class ImageStationRequestHandler(SimpleHTTPRequestHandler):
    
    def _send_json(self, data, status=200):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Headers', '*')
        self.send_header('Access-Control-Allow-Methods', '*')
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode('utf-8'))

    def _read_json_body(self):
        content_length = int(self.headers.get('content-length', 0))
        if content_length == 0:
            return {}
        body = self.rfile.read(content_length)
        return json.loads(body.decode('utf-8'))

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Headers', '*')
        self.send_header('Access-Control-Allow-Methods', '*')
        self.end_headers()


class ImageStationRequestHandler(SimpleHTTPRequestHandler):
    
    def _send_json(self, data, status=200):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Headers', '*')
        self.send_header('Access-Control-Allow-Methods', '*')
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode('utf-8'))

    def _read_json_body(self):
        content_length = int(self.headers.get('content-length', 0))
        if content_length == 0:
            return {}
        body = self.rfile.read(content_length)
        return json.loads(body.decode('utf-8'))

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Headers', '*')
        self.send_header('Access-Control-Allow-Methods', '*')
        self.end_headers()

    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == '/api/image/task/status':
            task_id = query.get('task_id', [None])[0]
            if not task_id:
                self._send_json({'error': 'Missing task_id'}, status=400)
                return
            with IMAGE_TASKS_LOCK:
                task = IMAGE_TASKS.get(task_id)
            if not task:
                self._send_json({'status': 'not_found'})
            else:
                self._send_json(task)
        else:
            super().do_GET()

    def do_POST(self):
        path = self.path.split('?')[0]

        if path == '/api/ping':
            try:
                body = self._read_json_body()
                config = effective_config(body.get('config', {}))
                base_url = (config.get('baseUrl') or 'http://127.0.0.1:8046/v1').rstrip('/')
                api_key = config.get('apiKey') or ''
                
                # Check if we can reach the proxy by making a simple request
                req = urllib.request.Request(
                    f'{base_url}/models',
                    headers={'Authorization': f'Bearer {api_key}'},
                    method='GET'
                )
                try:
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    with opener.open(req, timeout=5) as resp:
                        # Successful response or simple 200/401/404 means the endpoint is reachable
                        self._send_json({'online': True})
                except Exception as e:
                    # If we got a HTTPError it still means server is online but maybe key is wrong
                    if isinstance(e, urllib.error.HTTPError):
                        self._send_json({'online': True})
                    else:
                        self._send_json({'online': False, 'message': str(e)})
            except Exception as e:
                self._send_json({'online': False, 'message': str(e)})

        elif path == '/api/image/generations':
            try:
                if not access_ok(self):
                    self._send_json({'error': '访问码无效或缺失'}, status=401)
                    return
                body = self._read_json_body()
                config = effective_config(body.get('config'))
                base_url = (config.get('baseUrl') or 'http://127.0.0.1:8046/v1').rstrip('/')
                api_key = config.get('apiKey') or ''

                model = body.get('model') or config.get('imageModel') or 'gemini-3.1-flash-image'
                if 'nano-banana-2' in model:
                    model = model.replace('nano-banana-2', 'gemini-3.1-flash-image')
                prompt = body.get('prompt', '')
                size = body.get('size') or body.get('aspect_ratio') or '1:1'
                quality = normalize_image_quality_label(body.get('image_size') or body.get('quality') or '2K')
                response_format = body.get('response_format') or 'b64_json'

                final_model = image_generation_model_name(model, size, quality)

                payload = {
                    'model': final_model,
                    'prompt': prompt,
                    'size': size,
                    'quality': quality,
                    'response_format': response_format
                }

                import uuid
                task_id = f"img_task_{uuid.uuid4().hex}"
                
                with IMAGE_TASKS_LOCK:
                    IMAGE_TASKS[task_id] = {'status': 'pending', 'result': None, 'error': None}
                
                threading.Thread(
                    target=_run_async_image_generation,
                    args=(task_id, base_url, api_key, payload),
                    daemon=True
                ).start()
                
                self._send_json({'task_id': task_id, 'status': 'pending'})
            except Exception as e:
                self._send_json({'error': str(e)}, status=500)

        elif path == '/api/image/edits':
            try:
                if not access_ok(self):
                    self._send_json({'error': '访问码无效或缺失'}, status=401)
                    return

                content_type = self.headers.get('content-type')
                if not content_type or 'multipart/form-data' not in content_type:
                    self._send_json({'error': 'Content-Type must be multipart/form-data'}, status=400)
                    return

                content_length = int(self.headers.get('content-length', 0))
                body_bytes = self.rfile.read(content_length)

                # Parse the multipart form data
                msg_bytes = f"Content-Type: {content_type}\r\n\r\n".encode('utf-8') + body_bytes
                from email.parser import BytesParser
                from email.policy import default
                msg = BytesParser(policy=default).parsebytes(msg_bytes)

                fields = {}
                files = []
                client_config_str = "{}"

                for part in msg.walk():
                    if part.get_content_disposition() != 'form-data':
                        continue
                    name = part.get_param('name', header='content-disposition')
                    if not name:
                        continue
                    filename = part.get_filename()
                    payload = part.get_payload(decode=True) or b''
                    if filename is not None:
                        if payload:
                            files.append((name, filename, part.get_content_type(), payload))
                    else:
                        value = payload.decode('utf-8', errors='replace').strip()
                        if name == 'config':
                            client_config_str = value
                        else:
                            fields[name] = value

                # Resolve config
                client_config = {}
                if client_config_str:
                    try:
                        client_config = json.loads(client_config_str)
                    except: pass
                config = effective_config(client_config)

                import uuid
                task_id = f"img_task_{uuid.uuid4().hex}"
                
                with IMAGE_TASKS_LOCK:
                    IMAGE_TASKS[task_id] = {'status': 'pending', 'result': None, 'error': None}
                
                threading.Thread(
                    target=_run_async_image_edit,
                    args=(task_id, fields, files, config),
                    daemon=True
                ).start()
                
                self._send_json({'task_id': task_id, 'status': 'pending'})
            except Exception as e:
                self._send_json({'error': str(e)}, status=500)
        else:
            self.send_response(404)
            self.end_headers()

def run():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    if sys.stdout:
        print(f"Starting SPARK Studio (Image Service Station) on port {PORT}...")
        print(f"Sharing parent configuration from: {GLOBAL_CONFIG_FILE}")
    server_address = ('', PORT)
    httpd = ThreadedHTTPServer(server_address, ImageStationRequestHandler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    if sys.stdout:
        print("Stopping Image Service Station server...")

if __name__ == '__main__':
    run()
