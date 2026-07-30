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
try:
    from PIL import Image
except ImportError:
    print("[FATAL] 缺少 Pillow 依赖，请运行: pip install -r requirements.txt")
    raise

from server_common import (
    SERVER_CONFIG, SERVER_MANAGED, resolve_gateway, effective_config,
    OUTPUT_ROOT, SKILL_DIR, _get_project_dir, _safe_project_name,
    IMG2IMG_CONTROL_PROMPT, IMG2IMG_BRIDGE_CONTROL_PROMPT, IMG2IMG_BRIDGE_TURN_CONTROL_PROMPT,
    IMG2IMG_RAW_STATE_CONTROL_PROMPT,
    resolve_cover_reference,
    IMAGE_TASKS, IMAGE_TASKS_LOCK,
    apply_google_fx_runtime_overrides, fx_cancel_context,
    read_manifest, write_manifest, GenerationCancelled, log,
    gpt_image_pixel_size, drop_stale_review_verdicts, stamp_manifest_capabilities,
    # 号池轮转口径（帧序列与视频序列共用，见 server_common 的「换 IP 已全局关停」注释）
    _get_account_pool_service, _select_pool_account,
    _account_switch_interval, _account_rotation_ring,
)



class QuotaExhaustedError(RuntimeError):
    """Raised when the upstream API quota is exhausted; retrying is pointless."""
    pass


class ImageTaskCancelled(GenerationCancelled):
    """图像任务被用户取消（在重试间隙检测到）。继承 GenerationCancelled（即
    ConnectionError）以兼容 worker 收尾逻辑既有的 `except ConnectionError → cancelled`
    分支，取消后任务会被正确终态化成"已取消"而不是"失败"。"""


_UPSTREAM_SINK = threading.local()
_CANCEL_SINK = threading.local()


def set_upstream_event_sink(fn):
    """注册（传 None=清除）当前线程的上游失败即时广播回调。
    _execute_request_with_retry 每次尝试失败都会立刻回调一次——帧序列 worker 把它
    转成 SSE 'upstream_retry' 事件、图像站 worker 把它写进任务 stage——前端因此在
    上游报错的瞬间就能看到真相，而不是等整条重试退避链（最长分钟级）烧完才知道。"""
    _UPSTREAM_SINK.fn = fn


def set_cancel_check_sink(fn):
    """注册（传 None=清除）当前线程的默认取消检测回调。_execute_request_with_retry
    的多数调用点（call_image_llm/_post_json/_generate_image_edit 等）此前根本没
    传 cancel_check 参数——用户点「取消」只会中断前端的 SSE 读取，后端 worker 线程
    完全无感知，会继续把整条重试退避链（含上游限流的多轮重试）跑完，导致取消按钮
    "点了没用"。这里用线程局部变量兜底：worker 注册一次，之后所有嵌套多层调用到
    _execute_request_with_retry 的地方无需逐层传参也能在下一次尝试前感知取消。"""
    _CANCEL_SINK.fn = fn


def current_thread_sinks():
    """当前线程上注册的 (upstream_sink, cancel_sink)。

    两个 sink 都是 threading.local——把工作扔进线程池时子线程看不到它们，后果是
    退避链里的取消检测失灵（取消按钮又变成"点了没用"）、上游报错也不再实时广播。
    并发执行器（见 prompt_pipeline._map_parallel）必须先用这个函数把父线程的上下文
    取出来，再在每个子线程里 set 回去。"""
    return getattr(_UPSTREAM_SINK, 'fn', None), getattr(_CANCEL_SINK, 'fn', None)


def _emit_upstream_failure(attempt, max_attempts, error_text, retry_in=None):
    fn = getattr(_UPSTREAM_SINK, 'fn', None)
    if not fn:
        return
    try:
        fn({
            'attempt': attempt,
            'max_attempts': max_attempts,
            'error': str(error_text)[:200],
            'retry_in': round(retry_in, 1) if retry_in else None,
        })
    except Exception:
        pass  # 广播失败绝不能影响请求重试本身


def _interruptible_sleep(seconds, cancel_fn=None):
    """可中断的分段 sleep：按 0.5 秒粒度休眠，每段之间检查 cancel_fn。
    cancel_fn 返回 True 时立刻 raise ImageTaskCancelled，使退避期间也能
    响应取消——不再在 time.sleep 里傻等几分钟。cancel_fn 为 None 时退化为
    普通 time.sleep。"""
    if cancel_fn is None:
        # 没有取消回调，退回线程局部默认值
        cancel_fn = getattr(_CANCEL_SINK, 'fn', None)
    if cancel_fn is None:
        time.sleep(seconds)
        return
    remaining = seconds
    while remaining > 0:
        chunk = min(remaining, 0.5)
        time.sleep(chunk)
        remaining -= chunk
        if cancel_fn():
            raise ImageTaskCancelled("任务已被用户取消（退避期间检测到取消）")


def _execute_request_with_retry(req, opener=None, timeout=None, max_attempts=2, initial_delay=2.0, cancel_check=None, on_attempt=None, emit_quota_failure=True):
    """emit_quota_failure=False：配额耗尽照常抛 QuotaExhaustedError，但不往进度流
    广播「上游报错」。只给「调用方撞到这堵墙时有等价的路可换、换完这一帧照渲」的
    探路请求用（见 _generate_image_edit 的 edits→chat 换通道）——那种情况下广播出去
    的是一句吓人的假话：前端会把它渲成「此路终止，任务即将报错结束」，而任务其实
    好好地渲完了。真正走投无路的那一枪仍然照报。"""
    import random
    if opener is None:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    # 显式传入的 cancel_check 优先；否则退回 worker 通过 set_cancel_check_sink
    # 注册的线程局部默认值，见该函数的说明。
    check_cancel = cancel_check or getattr(_CANCEL_SINK, 'fn', None)

    last_exception = None
    delay = initial_delay

    for attempt in range(max_attempts):
        # 每次（重）试前检查取消：已取消的任务不再烧上游配额
        if check_cancel and check_cancel():
            raise ImageTaskCancelled("任务已被用户取消")
        if on_attempt:
            # 图像站实时动态用：报告当前是第几次尝试（回调异常不许影响请求本身）
            try:
                on_attempt(attempt + 1, max_attempts)
            except Exception:
                pass
        try:
            url_str = req.full_url if hasattr(req, 'full_url') else str(req)
            log('DEBUG', 'HTTP', f"发送请求 {url_str}", attempt=f"{attempt+1}/{max_attempts}")

            with opener.open(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            last_exception = e
            detail = ''
            try:
                detail = e.read().decode('utf-8')[:800]
            except Exception:
                pass

            log('WARN', 'HTTP', f"尝试 {attempt+1}/{max_attempts} 失败 HTTP {e.code}: {detail[:200]}")
            
            # Detect quota exhaustion (direct 429 or 502 wrapping upstream 429)
            # and fail immediately – retrying only wastes quota.
            # The account-pool token broker in front of the gateway also fails this
            # way when it has no account left for the model at all (not a JSON body,
            # e.g. "Max retries exhausted. Last error: Token error: No accounts
            # available with quota for model: ...") – treat that the same as a
            # quota error so it surfaces as a clean QuotaExhaustedError instead of
            # burning the remaining retries on a wall that will not move.
            quota_signal = (
                "QUOTA_EXHAUSTED" in detail
                or "RESOURCE_EXHAUSTED" in detail
                or "capacity on this model" in detail
                or "quotaResetDelay" in detail
                or "no accounts available" in detail.lower()
            )
            if quota_signal:
                err_msg = "您的图片生成配额已耗尽 (QUOTA_EXHAUSTED)。"
                msg = ""
                try:
                    # The 502 body wraps the upstream 429 JSON, find "message"
                    err_json = json.loads(detail)
                    inner = err_json.get('error', {})
                    msg = inner.get('message') or ""
                except Exception:
                    pass
                if not msg:
                    # Sometimes the real 429 JSON is nested deeper, or (token
                    # broker case) the body is plain text with no JSON at all.
                    m = re.search(r'"message":\s*"([^"]+)"', detail)
                    if m:
                        msg = m.group(1)
                if msg:
                    err_msg += f" {msg}"
                elif detail.strip():
                    err_msg += f" {detail.strip()[:300]}"
                # Extract quota reset delay for user-friendly message
                delay_m = re.search(r'quotaResetDelay[":\s]+([^,\}"]+)', detail)
                if delay_m:
                    err_msg += f" Quota resets in: {delay_m.group(1).strip()}"
                if emit_quota_failure:
                    _emit_upstream_failure(attempt + 1, max_attempts, f'HTTP {e.code} 配额耗尽: {detail.strip()[:120]}')
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

                    _emit_upstream_failure(attempt + 1, max_attempts, f'HTTP {e.code}: {detail.strip()[:120]}', retry_in=sleep_time)
                    log('WARN', 'HTTP', f"限流/服务端错误，{sleep_time:.2f}s 后重试")
                    _interruptible_sleep(sleep_time, check_cancel)
                    continue
            _emit_upstream_failure(attempt + 1, max_attempts, f'HTTP {e.code}: {detail.strip()[:120]}')
            raise e
        except urllib.error.URLError as e:
            last_exception = e
            log('WARN', 'HTTP', f"尝试 {attempt+1}/{max_attempts} 失败 URLError: {e.reason}")
            if attempt < max_attempts - 1:
                sleep_time = delay * (1.5 ** attempt) + random.uniform(0.5, 1.5)
                _emit_upstream_failure(attempt + 1, max_attempts, f'连接失败: {e.reason}', retry_in=sleep_time)
                _interruptible_sleep(sleep_time, check_cancel)
                continue
            _emit_upstream_failure(attempt + 1, max_attempts, f'连接失败: {e.reason}')
            raise e
        except socket.timeout as e:
            last_exception = e
            log('WARN', 'HTTP', f"尝试 {attempt+1}/{max_attempts} 失败：socket timeout")
            if attempt < max_attempts - 1:
                sleep_time = delay * (1.5 ** attempt) + random.uniform(0.5, 1.5)
                _emit_upstream_failure(attempt + 1, max_attempts, '请求超时 (socket timeout)', retry_in=sleep_time)
                _interruptible_sleep(sleep_time, check_cancel)
                continue
            _emit_upstream_failure(attempt + 1, max_attempts, '请求超时 (socket timeout)')
            raise e
        except Exception as e:
            last_exception = e
            log('WARN', 'HTTP', f"尝试 {attempt+1}/{max_attempts} 出现未预期错误: {e}")
            if attempt < max_attempts - 1:
                sleep_time = delay * (1.5 ** attempt) + random.uniform(0.5, 1.5)
                _emit_upstream_failure(attempt + 1, max_attempts, str(e), retry_in=sleep_time)
                _interruptible_sleep(sleep_time, check_cancel)
                continue
            _emit_upstream_failure(attempt + 1, max_attempts, str(e))
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


def update_manifest_stale_status(manifest, project_dir, regenerated_sequences=None, finalize=False):
    """帧内容变了 → 已合并视频/视频清单作废（旧行为，任何调用都执行）。

    finalize=True 时（帧生成整轮成功收尾处调用）额外维护 i2i 链的血统标记：
    部分重生（regenerated_sequences 为槽位子集）后，位于最早重生帧之后、又没被本轮
    重生的帧，其画面仍派生自旧链——标记 stale_lineage=True，供视频配对门禁与前端
    识别；被本轮重生的帧清除标记。整单全量重生（regenerated_sequences=None）时链条
    重新连续，清空全部标记。

    finalize 时还会顺手做两件同属"收尾"的事——这是所有渲染路径的共同收尾点，放在这里
    才能保证单帧重试/定向修复/整单重渲都不漏：
      1. 作废"所看帧图已经变过"的一致性审查结论（server_common.drop_stale_review_verdicts），
         否则 manifest 上会留下过期的 sequence_reviewed_pass；
      2. 盖上运行时能力印章（server_common.stamp_manifest_capabilities）：numpy/ffmpeg/
         技能契约缺失时本地视觉探针整套静默跳过，"这单压根没做内容级校验"必须是清单上
         的一行，而不是靠人回忆当时的环境。"""
    if 'merged_video' in manifest:
        del manifest['merged_video']
    if 'videos' in manifest:
        manifest['videos'] = []
    if not finalize:
        return
    stamp_manifest_capabilities(manifest, 'frames')
    dropped = drop_stale_review_verdicts(manifest, project_dir)
    if dropped and sys.stdout:
        print(f"[REVIEW] 帧内容已变化，IMG {dropped} 的一致性审查结论已作废")
    frames = manifest.get('frames') or []
    if regenerated_sequences is None:
        for fr in frames:
            if isinstance(fr, dict):
                fr.pop('stale_lineage', None)
        return
    regen = set()
    for s in regenerated_sequences:
        try:
            regen.add(int(s))
        except (TypeError, ValueError):
            continue
    if not regen:
        return
    # Every frame belongs to one continuous i2i lineage, including CUT beats.
    regen_start = min(regen)
    for fr in frames:
        if not isinstance(fr, dict):
            continue
        seq = fr.get('sequence')
        if not isinstance(seq, int):
            continue
        if seq in regen:
            fr.pop('stale_lineage', None)
            continue
        if seq > regen_start:
            fr['stale_lineage'] = True


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
    resp_bytes = _execute_request_with_retry(req, opener=opener, timeout=180)
    body = json.loads(resp_bytes.decode('utf-8'))
    return body['choices'][0]['message'].get('content') or ''


def _quality_to_images_api(quality):
    return _image_quality_to_label(quality)


def _image_size_to_api_size(aspect_ratio, model=None):
    if model == 'gpt-image-2':
        return gpt_image_pixel_size(aspect_ratio)
    return aspect_ratio or '9:16'


def _image_edit_api_size(aspect_ratio):
    """Windows 8046 `/images/edits` 已验证的顶层 size 参数。

    generations/chat 接口可接受比例字符串，但当前 Windows edits 路由用像素尺寸最稳定：
    2026-07-29 实测 720x1280 + image_size=2K 返回 1536x2752。未知比例保留原值，
    避免把调用方已经传入的网关扩展比例静默改成方图。
    """
    value = str(aspect_ratio or '9:16').strip().lower()
    if re.fullmatch(r'\d+x\d+', value):
        return value
    return {
        '1:1': '1024x1024',
        '16:9': '1280x720',
        '9:16': '720x1280',
        '4:3': '1216x896',
        '3:4': '896x1216',
        '3:2': '1264x848',
        '2:3': '848x1264',
        '21:9': '1584x672',
    }.get(value, value)


def _measure_image_pixels(path):
    """返回落盘图的真实像素尺寸字符串（如 '768x1376'）；读不出来就返回空串。"""
    try:
        from PIL import Image
        with Image.open(path) as img:
            return f'{img.size[0]}x{img.size[1]}'
    except Exception:
        return ''


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
            # 网关对部分 t2i 模型（实测 gpt-image-2）无视 size/aspect_ratio 固定出
            # ~1254x1254 方图，闭源改不了——落盘前按配置比例居中裁剪兜底，
            # 否则方帧混进 9:16 链，i2v 配对/合成必然构图跳变。比例已一致时是 no-op。
            img = _crop_to_aspect_ratio(img, config.get('imageAspectRatio') or '9:16')
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
        'size': _image_size_to_api_size(config.get('imageAspectRatio'), model),
        'quality': _quality_to_images_api(config.get('imageQuality')),
        'image_size': _image_quality_to_label(config.get('imageQuality')),
        'response_format': 'b64_json',
    }
    data = _post_json(base_url, api_key, '/images/generations', payload, timeout=300)
    if not data.get('data'):
        raise RuntimeError('text-to-image response contained no image data')
    _decode_or_download_image(data['data'][0], target_path, config)


CHAT_TRANSPORT = 'chat_completions'

# 号池墙的进程级熔断：一旦某个网关的 /images/edits 报出「pro-image 号池无额度」，
# 这条路在本进程剩下的时间里就是死的（号池自己已经把名下账号轮完了，不会自愈）。
# 记下来之后同一网关的图生图直接走 chat 通道，不再为每一帧都白发一次必挂的请求——
# 实测那一枪要 1010ms，还得把整张参考图（~830KB）上传一遍。
# 故意只活在进程内：网关补好补丁后重启服务即重新探路，不需要改配置。
_EDITS_POOL_DRY = set()
_EDITS_POOL_DRY_LOCK = threading.Lock()


def _mark_edits_pool_dry(base_url):
    with _EDITS_POOL_DRY_LOCK:
        _EDITS_POOL_DRY.add(base_url)


def _edits_pool_is_dry(base_url):
    with _EDITS_POOL_DRY_LOCK:
        return base_url in _EDITS_POOL_DRY


def reset_edits_pool_state():
    """测试/手工重新探路用：清掉熔断记忆。"""
    with _EDITS_POOL_DRY_LOCK:
        _EDITS_POOL_DRY.clear()


def _chat_transport_is_full_quality(config):
    """chat 通道固定出 1K 档（实测 2026-07-25：9:16 出 768x1376，与 /images/edits
    的 image_size=1K 逐像素同档；2K 档是 1536x2752）。所以只有请求 2K/4K 时它才
    算降档——本单本来就是 1K 的话画质没有任何损失。"""
    return _image_quality_to_label(config.get('imageQuality')) == '1K'


def chat_transport_note(config):
    """这一帧走 chat 通道的如实说明（进度流播报 + manifest 留痕共用一份文案）。"""
    note = ('chat 通道续渲：网关 /images/edits 的 gemini-3-pro-image 号池无额度，'
            '同模型改走 /chat/completions 完成图生图')
    if _chat_transport_is_full_quality(config):
        return note + '；该通道固定出 1K 档（768x1376），与本单请求的档位一致，画质无损失'
    tier = _image_quality_to_label(config.get('imageQuality'))
    return (note + f'；该通道只出 1K 档（768x1376），本单请求的是 {tier} 档——'
            '分辨率降档，补额度后建议对本帧定向重渲')


def _chat_transport_model(model):
    """返回可以通过网关 /chat/completions 通道渲同一个模型的裸模型名；不支持则 None。

    只放行 gemini 系图像模型：它们与 /images/edits 是同一个网关(8046)，chat 通道
    带上参考图就是同一个模型在做图生图，链上一致性不受影响。gpt-image-2 走的是另一个
    网关(codex 65038)，没有这条等价通道，不在此列。
    比例/画质魔法后缀必须先还原成裸名——网关的模型表里 flash-image 系没有后缀变体，
    带后缀会被上游判 404（实测 2026-07-25：gemini-3.1-flash-image-9-16 →
    "Requested entity was not found"，且被号池包装成 429「All accounts exhausted」）。
    """
    bare = re.sub(r'-\d+-\d+(?:-\d+k)?$', '', (model or '').strip(), flags=re.IGNORECASE)
    bare = re.sub(r'-(?:2k|4k)(?:-\d+x\d+)?$', '', bare, flags=re.IGNORECASE)
    lowered = bare.lower()
    if not lowered.startswith('gemini') or 'image' not in lowered:
        return None
    return bare


def _generate_image_edit_via_chat(config, model, prompt, ref_bytes, ref_mime, target_path,
                                  timeout=360):
    """图生图的备用传输通道：同一个网关、同一个模型，只把请求从 /images/edits
    （multipart）换成 /chat/completions（参考图内联成 data URL）。

    存在的原因：网关的 /images/edits 被写死路由到 gemini-3-pro-image 号池——请求里
    写的是哪个图像模型都一样，池子没额度就一律 502
      "Max retries exhausted. Last error: Token error: No accounts available with
       quota for model: gemini-3-pro-image"
    首帧走 /images/generations（flash-image 号池，有额度）没事、第 2 帧起必挂，就是
    这个原因。同一个网关的 chat 通道用同一个模型名带参考图能正常图生图，且实测出的
    是忠实续帧（同场景/同材质/同光照），所以撞这堵墙时不换模型、只换通道。

    已知代价（调用方必须留痕，不许假装是正常帧）：这条通道只有顶层 size 字段能控出图
    比例（aspect_ratio / image_size / 在提示词里写 "9:16" 全部无效，会出 1024x1024
    方图），分辨率完全控不了，实测固定 768x1376。
    """
    base_url, api_key = resolve_gateway(model, config)
    data_url = f"data:{ref_mime};base64,{base64.b64encode(ref_bytes).decode('ascii')}"
    payload = {
        'model': model,
        'size': config.get('imageAspectRatio') or '9:16',
        'messages': [{
            'role': 'user',
            'content': [
                {'type': 'text', 'text': prompt},
                {'type': 'image_url', 'image_url': {'url': data_url}},
            ],
        }],
    }
    data = _post_json(base_url, api_key, '/chat/completions', payload, timeout=timeout)
    choices = data.get('choices') or []
    message = (choices[0].get('message') or {}) if choices else {}
    content = message.get('content')
    if isinstance(content, list):
        # 数组式多模态回包：图可能在 image_url 块里，也可能在文本块的 markdown 里
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get('type') == 'image_url':
                url = (block.get('image_url') or {}).get('url')
                if url:
                    parts.append(url)
            elif block.get('text'):
                parts.append(block['text'])
        content = '\n'.join(parts)
    image_url = _extract_image_url_from_text(content or '')
    if not image_url:
        raise RuntimeError('chat 通道图生图响应里没有图片')
    _decode_or_download_image(image_url, target_path, config)


def _generate_image_edit(config, prompt, reference_path, target_path, control_prompt=None):
    """图生图渲一帧。返回实际用的传输通道：走 /images/edits 返回 None，走 chat 通道
    返回 CHAT_TRANSPORT（调用方据此在 manifest/进度流里留痕，见 chat_transport_note）。

    通道选择由 config['imageEditTransport'] 决定：
      'auto'（默认）—— 先走 /images/edits；撞上 pro-image 号池墙就换 chat 通道，
                        并记住这个网关已死，本进程后续帧直接走 chat 不再白发那一枪；
      'chat'        —— 从不发 /images/edits（网关补丁缺失的机器上省掉这次必挂请求）；
      'edits'       —— 只走 /images/edits，撞墙就地报错，绝不换通道。
    """
    if control_prompt is None:
        control_prompt = IMG2IMG_CONTROL_PROMPT
    model = _image_edit_model(config)
    base_url, api_key = resolve_gateway(model, config)

    aspect_ratio = config.get('imageAspectRatio') or '9:16'
    ref_mime = 'image/png'
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
        ref_mime = _detect_image_mime_from_path(reference_path)

    # Ensure clean model name (no suffixes like -9-16-2k)
    clean_model = re.sub(r'-\d+-\d+(?:-\d+k)?$', '', model, flags=re.IGNORECASE)
    clean_model = re.sub(r'-(?:2k|4k)(?:-\d+x\d+)?$', '', clean_model, flags=re.IGNORECASE)

    transport_mode = (config.get('imageEditTransport') or 'auto').strip().lower()
    chat_model = _chat_transport_model(clean_model)
    # auto 模式下第一枪 edits 只是探路：撞了号池墙也有 chat 通道接着渲，所以那次
    # 配额耗尽不该被当成"任务要挂了"广播出去（前端会渲成「此路终止，任务即将报错
    # 结束」）。真正换不了路的情况（没有等价 chat 模型 / transport=edits）照报。
    edits_wall_is_recoverable = bool(chat_model) and transport_mode != 'edits'
    full_prompt = f'{control_prompt}\n\n{prompt}'.strip()

    max_attempts = 2
    attempts_left = max_attempts
    while attempts_left > 0:
        attempt_no = max_attempts - attempts_left + 1
        # 已知这个网关的 edits 号池是干的（或用户直接指定 chat 通道）：不再发那一枪。
        # 每一枪都是 ~1s + 整张参考图上传，且结果必然是同一堵 502 墙。
        use_chat = bool(chat_model) and (
            transport_mode == 'chat'
            or (transport_mode == 'auto' and _edits_pool_is_dry(base_url)))
        try:
            if use_chat:
                if sys.stdout:
                    print(f"[FRAME SEQUENCE] Image-to-Image edit via /chat/completions "
                          f"(attempt {attempt_no}/{max_attempts}): "
                          f"{os.path.basename(reference_path)} ({len(ref_bytes)} bytes) -> {chat_model}"
                          f"（跳过 /images/edits："
                          f"{'配置指定' if transport_mode == 'chat' else '本进程已确认该网关号池无额度'}）")
                _generate_image_edit_via_chat(config, chat_model, full_prompt,
                                              ref_bytes, ref_mime, target_path)
                return CHAT_TRANSPORT

            # 一律走网关的 /images/edits（multipart/form-data）。
            # 曾经这里还有一条「配了 Google AI Studio 的 Gemini API Key 就直连
            # generativelanguage.googleapis.com」的分支，已随 key 入口一并删除：
            # 生图只有网关(8046) 和 UI 自动化(google_fx) 两条明路，不再有第三条
            # 凭一个环境变量/配置键就静默改道的暗路。
            import uuid
            boundary = f"Boundary-{uuid.uuid4().hex}"
            body_data = bytearray()

            fields = {
                'model': clean_model,
                'prompt': full_prompt,
                # Windows 8046 的 multipart edits 路由实测认顶层像素 size；继续发旧的
                # aspect_ratio 会让比例控制依赖网关版本。image_size 独立控制 1K/2K/4K：
                # size=720x1280 + image_size=2K 实际返回 1536x2752。
                'size': _image_edit_api_size(aspect_ratio),
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
                    f"[FRAME SEQUENCE] Image-to-Image edit via /images/edits (multipart) (attempt {attempt_no}/{max_attempts}): "
                    f"{os.path.basename(reference_path)} ({len(ref_bytes)} bytes) -> {clean_model} "
                    f"size={fields['size']} image_size={fields['image_size']}"
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
            resp_bytes = _execute_request_with_retry(
                req, opener=opener, timeout=360,
                emit_quota_failure=not edits_wall_is_recoverable)
            data = json.loads(resp_bytes.decode('utf-8'))

            if not data.get('data'):
                raise RuntimeError('image-to-image response contained no image data')
            _decode_or_download_image(data['data'][0], target_path, config)
            return None  # 走的是 /images/edits 正常通道

        except QuotaExhaustedError as quota_err:
            if use_chat:
                # chat 通道自己也没额度了：没有第三条路，就地停在配额耗尽这个真因上
                # （已完成的帧由断点续传保住，补了额度点重试即可接着渲）。
                raise
            # /images/edits 的号池没额度了。重试多少次都是同一堵墙（号池自己已经把
            # 名下账号轮完了），但换传输通道有救：同一个模型经 chat 通道能继续图生图，
            # 见 _generate_image_edit_via_chat 的说明。模型不变 = 不会出现"换模型认不出
            # 自己刚渲的东西"那类断链坏帧。
            if not chat_model or transport_mode == 'edits':
                # 没有等价通道可换（如 gpt-image-2 走的 codex 网关）或用户要求只走
                # edits：熔断记忆无处可用，不记，直接把真因抛出去。
                raise
            # 记下这个网关的 edits 已死，本帧下一圈和后续帧都直接走 chat，不再白发这一枪。
            _mark_edits_pool_dry(base_url)
            # 换通道这件事由调用方的 transport_fallback 事件如实播报（带 IMG 序号、
            # 说明降没降档）。这里曾经还额外推一条 upstream_failure，前端把它渲成
            # 「⚠️ 上游报错：…（第 1/1 次，此路终止，任务即将报错结束）」——一句吓人
            # 的假话：这一帧接着就在 chat 通道上渲成了。留日志，不再推进度流。
            log('WARN', 'FRAME_SEQ',
                f"/images/edits 号池无额度（{quota_err}），改用同模型 {chat_model} 的 chat 通道续渲")
            # 换通道重来，不算消耗一次重试机会：这一枪撞的是号池墙，不是链路抖动，
            # 换条路本身就该有和原来一样多的尝试次数。
            continue
        except GenerationCancelled:
            # 取消信号必须原样穿透：这是这个函数自己的外层重试循环（3 次，每次
            # 内部还套一层 _execute_request_with_retry 的 2 次退避重试）——不加
            # 这层专门捕获，取消会被下面 except Exception 当成一次普通失败，
            # 在 sleep 后又 continue 到下一次尝试，用户点了取消也拦不住它。
            raise
        except Exception as e:
            channel = '/chat/completions' if use_chat else '/images/edits'
            log('WARN', 'FRAME_SEQ',
                f"图生图（{channel}）外层尝试 {attempt_no}/{max_attempts} 失败: {e}")
            attempts_left -= 1
            if attempts_left > 0:
                import time
                time.sleep(2.0 + (attempt_no - 1) * 2.0)
            else:
                # All attempts failed, fail fast
                raise RuntimeError(f"All image-to-image edit attempts failed. Last error: {e}")


# ════════════════════════════════════════════════════════════════════
# Google FX UI 自动化帧序列生成（2026-07-04 新增，config.imageBackend == 'google_fx'）
# ════════════════════════════════════════════════════════════════════
# 使用内置浏览器自动化运行时 integrations.google_fx（labs.google Flow 画布）：
#   · 单次批量 ≤5 张，批内自动链式图生图（第 N+1 张自动挂第 N 张为参考）；
#   · 跨批次/单帧重试的续链：FX 运行时挂参考只认「文件名里的画布 UUID」，
#     所以每帧除 webp 外把原始 jpg（文件名含 UUID）留档到 frames/fx_src/；
#   · 每次调用用唯一临时 output_path，避免命中运行时的 dedupe 结果缓存；
#   · 与 API 路径一致：逐帧 VLM QA，失败改写提示词单帧重生（≤2 次）。
# 改动 FX 运行时或本文件都需重启 SPARK 进程。

_FX_CHUNK_SIZE = 5  # FX 运行时单次批量上限（google_fx_image 内部 prompts[:5]）

_FX_UUID_RE = re.compile(r'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})')


def _get_google_fx_image_service():
    """导入内置 Google FX 运行时；失败时把 ImportError 翻译成能直接照做的话。

    裸 ImportError 在前端只显示模块名，看不出是哪个运行时依赖没装；这里把它翻译成
    可直接执行的修复命令。
    """
    try:
        from integrations.google_fx.services import google_fx
        from integrations.google_fx import models
    except ImportError as e:
        missing = (getattr(e, 'name', '') or '').split('.')[0]
        if missing:
            hint = (
                f"内置 Google FX 运行时的依赖 {missing} 在当前 Python 环境里不可用。"
                f"请执行 pip install -r requirements.txt，并用 run.sh 重启 SPARK。"
            )
        else:
            hint = "内置 Google FX 运行时导入失败。"
        raise RuntimeError(f"帧序列 UI 自动化后端 (Google FX) 不可用：{hint} 原始错误: {e}") from e
    return google_fx, models


def _fx_image_model(config):
    # 内置 _normalize_model_name 只认当前 Flow 图片模型目录（含 Nano Banana 2 Lite）
    # 及其别名；未知名称会静默落到默认值，所以这里给一个确定的合法默认。
    from integrations.google_fx.model_catalog import normalize_google_fx_image_model
    return normalize_google_fx_image_model(config.get('googleFxImageModel'))


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


def plan_frame_chunk_accounts(chunks, ring, switch_interval):
    """给每个链式批次分配号池账号（纯函数，可单测）。

    返回与 chunks 等长的 [{'user_id': ...}, ...]。所有批次都跑在同一个 IP 上——换 IP
    已全局关停，见 server_common 的「换 IP 已全局关停」注释。

    与视频序列（video_generator.plan_generation_legs 直接按请求数硬切）的差别在于帧的
    批次边界是既定的：一个 chunk 就是外部脚本一次开浏览器、批内按提交顺序链式续图，
    中途换不了号，所以只能整批整批地分配。「每 switch_interval 个请求换一个号」这个
    节拍照旧，只是落到最近的批次边界上——累计够 switch_interval 帧才轮到下一个账号。

    可换的账号 ≤1 个（含手动指定账号/空号池）时全部返回 user_id=None，沿用调用方已经
    设好的账号。
    """
    if len(ring) <= 1:
        return [{'user_id': None} for _ in chunks]
    interval = max(1, switch_interval)
    plans = []
    leg_idx = 0
    leg_frames = 0
    for chunk in chunks:
        if leg_frames >= interval and plans:
            leg_idx += 1
            leg_frames = 0
        plans.append({'user_id': ring[leg_idx % len(ring)]})
        leg_frames += len(chunk)
    return plans


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


def _fx_cover_ref_jpg(cover_path, frames_dir):
    """Convert the selected cover to the JPEG reference format required by Flow."""
    target = os.path.join(_fx_src_dir(frames_dir), 'cover_ref.jpg')
    with Image.open(cover_path) as img:
        img.convert('RGB').save(target, format='JPEG', quality=92)
    return target


def _fx_local_frame_ref_jpg(frame_path, frames_dir, seq):
    """Convert a rendered frame into a Flow-uploadable i2i reference."""
    target = os.path.join(_fx_src_dir(frames_dir), f'chain_ref_{seq:03d}.jpg')
    with Image.open(frame_path) as img:
        img.convert('RGB').save(target, format='JPEG', quality=92)
    return target


def _fx_clear_frame_reference(frames_dir, seq):
    """Remove every cached FX reference for one replaced frame slot.

    A manual upload replaces ``img_NNN.webp`` but has no Flow canvas UUID.  Keeping the old
    ``img_NNN_<uuid>.jpg`` would make the next frame silently mount the pre-upload image.
    The local ``chain_ref_NNN.jpg`` conversion is equally stale and must be rebuilt too.
    """
    src_dir = os.path.join(frames_dir, 'fx_src')
    if not os.path.isdir(src_dir):
        return []
    prefixes = (f'img_{int(seq):03d}_', f'chain_ref_{int(seq):03d}.jpg')
    removed = []
    for name in os.listdir(src_dir):
        if not (name.startswith(prefixes[0]) or name == prefixes[1]):
            continue
        path = os.path.join(src_dir, name)
        if not os.path.isfile(path):
            continue
        os.remove(path)
        removed.append(path)
    return removed


def _fx_slot_reference_files(src_dir, seq):
    """该槽位现存的 UUID 留档文件名（正常只有一个，多了也按序返回）。"""
    prefix = f'img_{int(seq):03d}_'
    return [n for n in sorted(os.listdir(src_dir))
            if n.startswith(prefix) and n.lower().endswith('.jpg')]


def _fx_relocate_frame_reference(frames_dir, from_seq, to_seq, mode='swap'):
    """图换格之后，把 fx_src 里的 UUID 留档跟着搬到新格。

    ``_fx_find_ref_for`` 是靠 ``fx_src/img_NNN_<uuid>.jpg`` 给第 NNN+1 帧挂参考的。
    img_NNN.webp 换了格而留档留在原位，下一次生成就会照着换位前的那张老图续链。

    mode='copy' 时目标格拿到源格留档的副本（两格是同一张图，UUID 也该是同一个），
    源格原样保留；swap / 搬运则两格互换留档（空的一边把另一边也换空）。
    返回 {槽位号: 该格现在的留档绝对路径或 None}。
    """
    from_seq, to_seq = int(from_seq), int(to_seq)
    result = {from_seq: None, to_seq: None}
    src_dir = os.path.join(frames_dir, 'fx_src')
    if not os.path.isdir(src_dir):
        return result

    def _renamed(name, seq):
        m = re.match(r'^img_\d+_(.+)$', name)
        return f'img_{seq:03d}_{m.group(1)}' if m else name

    src_names = _fx_slot_reference_files(src_dir, from_seq)
    dst_names = _fx_slot_reference_files(src_dir, to_seq)

    if mode == 'copy':
        for name in dst_names:
            os.remove(os.path.join(src_dir, name))
        for name in src_names:
            shutil.copyfile(os.path.join(src_dir, name),
                            os.path.join(src_dir, _renamed(name, to_seq)))
    else:
        # 两格互换：先全部挪进临时名，避免 img_002_x → img_003_x 覆盖掉还没挪走的对家
        staged = []
        for name, seq in ([(n, to_seq) for n in src_names]
                          + [(n, from_seq) for n in dst_names]):
            tmp = os.path.join(src_dir, name + '.relocate.tmp')
            os.replace(os.path.join(src_dir, name), tmp)
            staged.append((tmp, os.path.join(src_dir, _renamed(name, seq))))
        for tmp, final in staged:
            os.replace(tmp, final)

    # chain_ref_NNN.jpg 只是该格 webp 的本地转档缓存，画面换了就作废（用时按新图重建）
    for seq in ((to_seq,) if mode == 'copy' else (from_seq, to_seq)):
        cached = os.path.join(src_dir, f'chain_ref_{seq:03d}.jpg')
        if os.path.isfile(cached):
            os.remove(cached)

    for seq in (from_seq, to_seq):
        found = _fx_slot_reference_files(src_dir, seq)
        result[seq] = os.path.join(src_dir, found[0]) if found else None
    return result


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


def _fx_batch_cancel_fn(on_progress):
    """给 fx_cancel_context 用的取消谓词（在守卫线程里被轮询调用，必须是纯谓词）。

    首选 worker 通过 set_cancel_check_sink 注册的回调（就是 `cancel_event.is_set`，
    读一个 threading.Event，跨线程安全）。这个 sink 是线程局部的，所以必须在 worker
    线程里现取——本函数只在生成入口调用一次，取到的是可调用对象本身。
    没注册 sink 的调用路径退回 on_progress('cancel_check')：各 worker 的这一分支同样
    只读 cancel_event，不写事件、不碰 ACTIVE_TASKS 之外的东西。"""
    _, cancel_sink = current_thread_sinks()
    if cancel_sink:
        return cancel_sink
    if on_progress:
        return lambda: bool(on_progress('cancel_check', None))
    return None


def _fx_cancelled_result(result):
    """外部批量生图脚本的返回是不是"因取消而收摊"。

    脚本内部把取消（_check_cancelled 抛的 RuntimeError("任务已取消")）在 except 里
    转成 message='任务已取消' 的非 success 结果返回，与"生成失败"同形。调用方必须把
    这一种单独摘出来当取消处理——否则会被批次级重试逻辑当成可重试的失败，再开一次
    浏览器把整批重跑一遍。"""
    return '任务已取消' in str((result or {}).get('message') or '')


def _fx_generate_batch(google_fx, models, config, prompt_texts, ref_path, cancel_fn=None,
                       excluded_media_uuids=None, excluded_image_paths=None):
    """调用外部批量生图脚本一次，返回 (本地文件路径列表, 临时目录)。

    ref_path：上一帧留档 jpg（首帧/无续链传 None）。
    cancel_fn：SPARK 侧的取消谓词，经 fx_cancel_context 桥到外部脚本的 per-request
    取消状态上，让脚本在等图片 URL 的轮询循环里就能停下来（原理与修复前的空转见
    server_common.fx_cancel_context）。
    外部脚本对失败的单张会静默跳过导致列表变短，而批内链式参考使得
    prompt↔图片 的对应关系无法事后修复，所以数量不齐一律按失败抛出。
    调用方负责在把图片转存后 shutil.rmtree 临时目录。
    """
    temp_out = tempfile.mkdtemp(prefix='spark_fx_img_')
    req = models.ImageBatchRequest(
        prompts=list(prompt_texts),
        images=[ref_path] if ref_path else [],
        excluded_media_uuids=list(excluded_media_uuids or []),
        excluded_image_paths=list(excluded_image_paths or []),
        ratio=config.get('imageAspectRatio') or '9:16',
        model=_fx_image_model(config),
        output_path=temp_out,  # 每次唯一，绕开外部 dedupe 缓存（同提示词重试不会拿到旧图）
    )
    with fx_cancel_context(cancel_fn):
        result = google_fx._generate_images_batch_google_fx(req)
    if not isinstance(result, dict) or result.get('status') != 'success':
        shutil.rmtree(temp_out, ignore_errors=True)
        if _fx_cancelled_result(result) or (cancel_fn and cancel_fn()):
            raise ImageTaskCancelled("帧序列生成已被用户取消（外部批量生图脚本已停）")
        raise RuntimeError(f"Google FX 批量生图失败: {(result or {}).get('message') or '未知错误'}")
    paths = [p for p in (result.get('image_urls') or []) if isinstance(p, str) and os.path.exists(p)]
    if len(paths) < len(prompt_texts):
        shutil.rmtree(temp_out, ignore_errors=True)
        raise RuntimeError(
            f"Google FX 批量生图不完整: 期望 {len(prompt_texts)} 张，实际落盘 {len(paths)} 张，"
            f"已放弃本批结果（批内链式对应关系无法修复）"
        )
    return paths, temp_out


def _generate_frame_sequence_google_fx(config, title, prompt_block, on_progress=None, target_sequences=None):
    import builtins
    # 纯 SPARK 内部旗标：AdsPower 侧早已删掉这个进程级全局标志，外部脚本不再读它
    # （真正送进脚本的取消信号见 _fx_batch_cancel_fn / fx_cancel_context）。
    builtins.google_fx_cancelled = False
    apply_google_fx_runtime_overrides(config)
    from prompt_pipeline import _parse_prompt_slots
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

    cancel_fn = _fx_batch_cancel_fn(on_progress)

    project_dir = _get_project_dir(title)
    frames_dir = os.path.join(project_dir, 'frames')
    os.makedirs(frames_dir, exist_ok=True)

    google_fx, fx_models = _get_google_fx_image_service()
    fx_model = _fx_image_model(config)

    # ── 换号（不换 IP，机制说明见 server_common 的「换 IP 已全局关停」注释）──
    # 号池服务本身取不到（老装机没有 utils/account_pool）不该把整条帧链带崩：退回
    # 单账号模式。选号失败（池子全欠费/禁用）是另一回事——那是明确的用户侧问题，
    # 照旧抛出来。
    try:
        account_pool = _get_account_pool_service()
    except Exception as e:
        print(f"Warning: 号池服务不可用，帧序列沿用当前账号 ({e})")
        account_pool = None
    pool_account_id = _select_pool_account(config, account_pool) if account_pool else None
    if pool_account_id:
        apply_google_fx_runtime_overrides(config)

    def _run_chunk_batch(chunk_prompts, ref_path, leg):
        """跑一批：先绑这一批的号池账号，再交给外部批量脚本。"""
        user_id = (leg or {}).get('user_id') or pool_account_id or config.get('googleFxUserId')
        if user_id:
            config['googleFxUserId'] = user_id
            apply_google_fx_runtime_overrides(config)
        from integrations.google_fx.utils import account_binding
        excluded_media_uuids = {
            str(frame.get('fx_uuid')).lower()
            for frame in manifest_frames_by_seq.values()
            if isinstance(frame, dict) and frame.get('fx_uuid')
        }
        excluded_image_paths = [
            _webp_path(seq)
            for seq in all_seqs
            if _frame_exists(seq)
        ]
        with account_binding.bound_task_account(user_id):
            return _fx_generate_batch(google_fx, fx_models, config, chunk_prompts, ref_path,
                                      cancel_fn=cancel_fn,
                                      excluded_media_uuids=excluded_media_uuids,
                                      excluded_image_paths=excluded_image_paths)

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
                # 未知键保留：检查点重锚定/校准记录（reanchors、anchor_recalibrations）等
                # 是在分段渲染的间隙写入的，重建 manifest 时丢掉它们会让下一段渲染的
                # 逐帧落盘把这些记录抹掉（videos/merged_video 仍由 stale 逻辑显式清理）
                for _k, _v in existing_manifest.items():
                    if _k not in manifest:
                        manifest[_k] = _v
        except Exception:
            pass

    manifest_frames_by_seq = {f['sequence']: f for f in manifest['frames']}
    # 按提示词块里的真实槽位号建索引（与 API 路径同一修复）：
    # 枚举位置在子集渲染时与槽位号错位，目标帧会静默漏渲
    prompts_by_seq = {int(item['index']): item for item in prompts}
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
        write_manifest(project_dir, manifest)

    generated_count = 0

    def _emit_frame(frame_info):
        nonlocal generated_count
        generated_count += 1
        if on_progress:
            on_progress('frame', {'frame': frame_info, 'current': generated_count, 'total': total_to_generate})

    def _run_vlm_qa(seq, item, is_bridge, ref_path):
        """不再逐帧质检——一致性审查移到整套序列渲染完成后，对着真实画面统一跑一次
        （见 pipeline_orchestrator._sequence_consistency_review）。'pending_manual_review'
        是 manifest 里已有的合法值，前端对它没有特殊徽标，渲染成普通帧。"""
        return 'pending_manual_review', None

    def _record_frame(seq, item, fx_src_path, fx_uuid, quality_gate, vlm_reason,
                      cover_reference=None):
        webp = _webp_path(seq)
        rel_path = os.path.relpath(webp, os.path.dirname(os.path.abspath(__file__))).replace('\\', '/')
        prev_path = _webp_path(seq - 1) if seq > 1 else None
        reference = prev_path if (prev_path and os.path.exists(prev_path)) else None
        if cover_reference:
            reference = cover_reference
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
            'parent_hash': "" if cover_reference else (_get_file_hash(reference) if reference else ""),
        }
        if cover_reference:
            frame_info['anchor_reference'] = 'cover'
        manifest_frames_by_seq[seq] = frame_info
        _save_manifest()  # 浏览器批量任务动辄数分钟，逐帧落盘保证进度可恢复
        _emit_frame(frame_info)

    chunks = plan_fx_chunks(gen_seqs)
    # 声明式硬切（[CUT] 视频槽）另起一批，便于让提示词主导场景变化；新批仍会显式
    # 挂载上一帧作为参考，因此不会退化成文生图或断开血统。
    cut_heads = set()
    for _v_idx, _v in (videos or {}).items():
        _vm = str(_v.get('meta', '') if isinstance(_v, dict) else '').upper()
        if 'CUT' in _vm and 'BRIDGE' not in _vm:
            cut_heads.add(int(_v_idx) + 1)
    if cut_heads:
        _split = []
        for _c in chunks:
            _cur = []
            for _s in _c:
                if _s in cut_heads and _cur:
                    _split.append(_cur)
                    _cur = []
                _cur.append(_s)
            if _cur:
                _split.append(_cur)
        chunks = _split
    chunk_by_start = {c[0]: c for c in chunks}
    # 批次边界定下来之后才能分账号：按换号节拍，每批绑号池里的下一个号（IP 始终不变）
    ring = _account_rotation_ring(config, account_pool, pool_account_id) if pool_account_id else []
    leg_by_chunk_start = {
        c[0]: leg for c, leg in zip(chunks, plan_frame_chunk_accounts(
            chunks, ring, _account_switch_interval(config)))
    }
    current_account_id = pool_account_id
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

        cover_ref_src = None
        ref_path = _fx_find_ref_for(frames_dir, chunk[0])

        chunk_prompts = [prompts_by_seq[s]['prompt'] for s in chunk]
        if chunk[0] == 1:
            cover_src = resolve_cover_reference(config, title)
            if not cover_src:
                raise RuntimeError('生成帧序列前必须先生成或选择封面图；第一帧只能以封面图进行图生图')
            ref_path = _fx_cover_ref_jpg(cover_src, frames_dir)
            cover_ref_src = cover_src
            # The cover is the image reference only. Send the parsed IMAGE 1 prompt verbatim.
        elif not ref_path:
            previous_webp = _webp_path(chunk[0] - 1)
            if not _frame_exists(chunk[0] - 1):
                raise RuntimeError(
                    f'无法生成第 {chunk[0]} 帧：缺少上一帧参考图，帧序列禁止退回文生图'
                )
            ref_path = _fx_local_frame_ref_jpg(previous_webp, frames_dir, chunk[0] - 1)
        leg = leg_by_chunk_start.get(chunk[0])
        if leg and leg.get('user_id') and leg['user_id'] != current_account_id:
            current_account_id = leg['user_id']
            if on_progress:
                on_progress('account_switch', {
                    'user_id': leg['user_id'],
                    'message': (f"换号继续：帧 {chunk[0]}~{chunk[-1]} 改用号池账号 "
                                f"{leg['user_id']}（保持当前 IP，不换 IP）"),
                })
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
            local_paths, temp_out = _run_chunk_batch(chunk_prompts, ref_path, leg)
        except ConnectionError:
            raise
        except Exception as e:
            if sys.stdout:
                print(f"[FRAME SEQUENCE][FX] 批量生图失败，3 秒后整批重试一次: {e}")
            _check_cancel()
            time.sleep(3.0)
            local_paths, temp_out = _run_chunk_batch(chunk_prompts, ref_path, leg)

        try:
            for offset, s in enumerate(chunk):
                item = prompts_by_seq[s]
                # Only VIDEO slots carry [BRIDGE] tags per the delivery contract; the
                # incoming transition (VIDEO s-1) is the real signal for IMAGE s.
                incoming_video = videos.get(s - 1)
                is_bridge = (
                    'BRIDGE' in item.get('meta', '').upper()
                    or 'BRIDGE' in (incoming_video.get('meta', '') if isinstance(incoming_video, dict) else '').upper()
                )
                _, fx_src_path, fx_uuid = _fx_store_frame(local_paths[offset], frames_dir, s)
                if s > 1:
                    first_frame_path = _webp_path(1)
                    target_path = _webp_path(s)
                    if os.path.exists(first_frame_path) and os.path.exists(target_path):
                        _match_color_lab(target_path, first_frame_path, target_path)
                prev_ref = _fx_find_ref_for(frames_dir, s)
                quality_gate, vlm_reason = _run_vlm_qa(s, item, is_bridge, prev_ref)
                # P1 换族锚点惯性检测（FX 链路只留痕不自动重渲：本批后续帧已链在该帧上）。
                if is_bridge and s > 1 and not vlm_reason:
                    _stuck, _inertia_mad = detect_anchor_inertia(_webp_path(s), _webp_path(s - 1))
                    if _stuck:
                        vlm_reason = (f"anchor_inertia: 桥接帧与参考帧近乎相同"
                                      f"（MAD={_inertia_mad:.2f}），i2i 惯性疑似未执行换族，"
                                      f"建议人工重渲该帧及其后续族段")
                        if sys.stdout:
                            print(f"[ANCHOR INERTIA][FX] Frame {s} {vlm_reason}")
                        if on_progress:
                            on_progress('anchor_inertia', {
                                'sequence': s, 'mad': round(_inertia_mad, 2),
                                'message': f"⚠️ IMG {s:03d} 桥接帧疑似被 i2i 惯性卡死（MAD={_inertia_mad:.2f}），已留痕",
                            })
                # QA 重生会替换留档，重新定位当前帧的 fx_src
                cur_ref = _fx_find_ref_for(frames_dir, s + 1)
                if cur_ref:
                    fx_src_path, fx_uuid = cur_ref, _fx_extract_uuid(cur_ref)
                _record_frame(s, item, fx_src_path, fx_uuid, quality_gate, vlm_reason,
                              cover_reference=(cover_ref_src if s == 1 else None))
                done_seqs.add(s)
        finally:
            shutil.rmtree(temp_out, ignore_errors=True)

    update_manifest_stale_status(manifest, project_dir,
                                 regenerated_sequences=target_sequences, finalize=True)
    _save_manifest()
    manifest['manifest'] = '/' + os.path.relpath(manifest_path, os.path.dirname(os.path.abspath(__file__))).replace('\\', '/')
    manifest['project_dir'] = os.path.abspath(project_dir)
    return manifest


# 已经报过的调色失败原因（见 _match_color_lab 末尾的去重）。进程级，不用加锁：
# 最坏情况是两个线程同时撞上第一次失败、各报一行，比漏报安全得多。
_COLOR_MATCH_WARNED = set()


def _match_color_lab(source_path, reference_path, output_path):
    """
    Adjusts the color statistics of source to match reference in LAB color space.
    L channel (lightness): 15% blend (allows progressive lighting changes).
    A & B color channels: 85% blend (suppresses pink/magenta color drift).

    OpenCV's 8-bit LAB representation stores every channel in the 0..255 range:
    L is scaled from 0..100 and a/b are offset by 128.  Keep that representation
    throughout this function; clipping it as floating-point CIE LAB corrupts the
    white balance (especially warm yellows) before converting back to BGR.
    """
    try:
        import cv2
        import numpy as np

        def _imread_unicode(path):
            # cv2.imread() 在 Windows 上用 fopen() 打开路径，本项目固定用中文
            # 标题命名母文件夹（outputs/<中文标题>/frames/...），遇到非 ASCII
            # 路径会静默失败返回 None——2026-07-22 实测确诊：色彩锚定对本项目
            # 里的每一帧、每一单都从未真正生效过，server.log 里"Aligning frame
            # N color to baseline"后面全是 cv::findDecoder 的静默失败警告。改用
            # np.fromfile 读字节 + cv2.imdecode 解码，绕开 fopen 的路径编码限制。
            data = np.fromfile(path, dtype=np.uint8)
            if data.size == 0:
                return None
            return cv2.imdecode(data, cv2.IMREAD_COLOR)

        def _imwrite_unicode(path, img):
            ext = os.path.splitext(path)[1] or '.webp'
            ok, buf = cv2.imencode(ext, img)
            if not ok:
                return False
            buf.tofile(path)
            return True

        src = _imread_unicode(source_path)
        ref = _imread_unicode(reference_path)
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
        # src_lab/ref_lab came from uint8 BGR, so cvtColor returned OpenCV's
        # uint8-encoded LAB (L: 0..255, a/b: 0..255 with 128 as neutral).
        # The previous CIE-LAB limits (L 0..100, a/b -127..127) clipped most
        # ordinary pixels and shifted the rendered sequence toward blue/cyan.
        merged = np.clip(merged, 0, 255)
        
        result_bgr = cv2.cvtColor(merged.astype(np.uint8), cv2.COLOR_LAB2BGR)
        _imwrite_unicode(output_path, result_bgr)
    except Exception as e:
        # 缺 cv2/numpy 是进程级的环境问题，不是"这一帧"的问题：原样每帧报一次，
        # 一次生成就刷 41 条一模一样的告警（实测），把日志冲满却只承载一个事实。
        # 按消息去重，同一种失败原因一个进程只报第一次。
        key = f"{type(e).__name__}:{e}"
        if key not in _COLOR_MATCH_WARNED:
            _COLOR_MATCH_WARNED.add(key)
            log('WARN', 'FRAMES', f"帧间调色不可用，已跳过（本进程仅提示一次）: {e}")


# P0 门框清除兜底的最大额外推进次数：换族室内侧帧渲出后若门框仍在画面里，
# 以该帧为参考用推进版控制指令"再往里推一步"，最多推这么多次。
_DOOR_CLEARANCE_MAX_PUSHES = 2


def _door_clearance_push_prompt(dc_reason, final_attempt=False):
    """定向门框清除推进指令：把上一轮 VLM 判定的具体残留位置（dc_reason）写回
    控制指令，而不是重复原样的 IMG2IMG_BRIDGE_CONTROL_PROMPT。根因是通用推进指令
    对每一轮都下发同一句话，i2i 编辑模型给出同样保守的结果——2026-07-16 岩湖贝壳
    单 img_005 连续两推、每次都换了措辞的失败原因，画面仍残留门框，印证"泛化推进"
    对已经推不动的模型无效，必须把失败点明确点名让模型针对性纠正。"""
    reason_text = dc_reason.split(':', 1)[-1].strip() if dc_reason else ''
    prompt = (
        IMG2IMG_BRIDGE_CONTROL_PROMPT +
        "\n\nDOOR CLEARANCE CORRECTION (mandatory, overrides any timid edit): an automated visual "
        "audit of the attached source image just found it still shows doorway/threshold remnants"
        + (f" — specifically: {reason_text}." if reason_text else ".") +
        " Push the camera decisively further past the threshold than the source image shows: "
        "every door frame, door leaf, jamb, and threshold/sill edge named above must be pushed "
        "completely out of frame this time. Do not repeat a small, partial, or timid advance — "
        "interior walls, ceiling, and floor must fill the frame edge to edge with zero doorway "
        "silhouette remaining anywhere in the shot."
    )
    if final_attempt:
        prompt += (
            " This is the last correction attempt budgeted for this frame: push further than "
            "feels natural rather than risk leaving any sliver of the doorway visible."
        )
    return prompt


# 过门帧「原始度」兜底的最大修正次数：室内首现帧渲出后若仍带人工痕迹/过于整洁，
# 以该帧自身为参考做定向状态修正，最多修这么多次。比门框清除少一次——这一步不改
# 构图只改内容，改不动通常是模型不肯加脏，多刷一轮的边际收益很低。
_RAW_STATE_MAX_FIXES = 1


def _raw_state_fix_prompt(rs_reason):
    """定向「回退到未被触碰状态」指令：把上一轮 VLM 判定的具体问题（rs_reason，例如
    "地面被扫干净/角落码着整齐的木料"）写回控制指令，而不是只下发通用的
    IMG2IMG_RAW_STATE_CONTROL_PROMPT——与门框清除加推同一条经验：泛化指令对已经渲成
    这样的模型没有纠正力，必须点名失败点。"""
    reason_text = rs_reason.split(':', 1)[-1].strip() if rs_reason else ''
    return (
        IMG2IMG_RAW_STATE_CONTROL_PROMPT +
        "\n\nRAW STATE CORRECTION (mandatory, overrides any timid edit): an automated visual audit "
        "of the attached source image just found it still reads as touched or tidied"
        + (f" — specifically: {reason_text}." if reason_text else ".") +
        " Fix exactly that, decisively: whatever was named above must be gone or undone in the "
        "returned image, and the space must end up visibly filthier and more derelict than the "
        "source frame, never cleaner. Keep the camera and composition identical."
    )


# 换族/桥接锚点帧的 i2i 惯性判据（2026-07-15 盐湖贝壳单标定）：桥接帧与其参考帧的
# 64px 灰度缩略 MAD——被参考惯性卡死渲成复制帧的 img_005/img_006 为 1.56/2.17，
# 正常施工推进对最低 4.8，真实换族 47。i2i 参考惯性压过"进入新空间"文本指令时，
# Near-copy detection triggers another i2i push; frame rendering never drops its reference.
_ANCHOR_INERTIA_MAD = 3.0


def detect_anchor_inertia(rendered_path, reference_path):
    """桥接/换族帧渲后与参考帧比对：近乎相同 = i2i 惯性未执行换族。
    返回 (stuck: bool, mad: float|None)；文件缺失/环境异常返回 (False, None) 不拦流程。"""
    try:
        if not (rendered_path and reference_path
                and os.path.exists(rendered_path) and os.path.exists(reference_path)):
            return False, None
        from PIL import Image
        import numpy as np

        def _gray(p):
            with Image.open(p) as im:
                return np.asarray(im.convert('L').resize((64, 114)), dtype=np.float32)

        mad = float(np.abs(_gray(rendered_path) - _gray(reference_path)).mean())
        return mad < _ANCHOR_INERTIA_MAD, mad
    except Exception:
        return False, None


def generate_frame_sequence(config, title, prompt_block, on_progress=None, target_sequences=None):
    if (config.get('imageBackend') or 'api').strip().lower() == 'google_fx':
        return _generate_frame_sequence_google_fx(
            config, title, prompt_block,
            on_progress=on_progress, target_sequences=target_sequences,
        )
    from prompt_pipeline import (
        _parse_prompt_slots, image_space_family, check_door_clearance_frame,
        check_first_interior_reveal_raw_state,
    )
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
                    # 未知键保留（与 FX 路径同款）：分段渲染间隙写入的 reanchors/
                    # anchor_recalibrations 等记录不能被逐帧落盘的重建 manifest 抹掉
                    for _k, _v in existing_manifest.items():
                        if _k not in manifest:
                            manifest[_k] = _v
        except Exception:
            pass

    if on_progress:
        total_to_generate = len(target_sequences) if target_sequences is not None else len(prompts)
        on_progress('start', {'total': total_to_generate})

    manifest_frames_by_seq = {f['sequence']: f for f in manifest['frames']}

    previous_path = None
    generated_count = 0
    for item in prompts:
        # 用提示词块里的真实槽位号，绝不能用枚举位置：单帧/子集渲染时
        # prompt_block 可能只含目标槽位，枚举位置永远从 1 开始，
        # 会导致 `seq in target_sequences` 永假 → 一帧不渲染，
        # 下游锚点门禁又因 fail-open 而自动放行——静默漏帧
        seq = int(item['index'])
        _check_cancel()
        filename = f'img_{seq:03d}.webp'
        target_path = os.path.join(frames_dir, filename)

        should_generate = True
        if target_sequences is not None:
            should_generate = seq in target_sequences

        if not should_generate:
            if os.path.exists(target_path):
                previous_path = target_path
            continue

        # If the file already exists on disk and we are not doing a specific target retry/regeneration,
        # we can skip the external API call and use the existing file immediately.
        already_exists = os.path.exists(target_path) and os.path.getsize(target_path) > 0
        skip_api_call = already_exists and (target_sequences is None)

        model = ""
        retries = 0
        vlm_qa_reason = None
        transport = None  # 非 None = 本帧不是走 /images/edits 渲的，见 chat_transport_note
        cover_anchor = False
        # Only VIDEO slots carry [BRIDGE]/[BRIDGE TURN]/[CUT] tags per the delivery contract;
        # the incoming transition (VIDEO seq-1) is the real signal for IMAGE seq, not the
        # image's own tag.
        incoming_video = videos.get(seq - 1)
        incoming_meta = (incoming_video.get('meta', '') if isinstance(incoming_video, dict) else '').upper()
        is_bridge = (
            'BRIDGE' in item.get('meta', '').upper()
            or 'BRIDGE' in incoming_meta
        )
        # pan 变体的单一过门拍：合并镜头（推进+转向）用合并版控制指令
        is_turn = 'TURN' in incoming_meta
        # CUT changes the edit instruction, but does not break the image-reference chain.
        is_cut_head = ('CUT' in incoming_meta) and ('BRIDGE' not in incoming_meta)
        cover_ref = (resolve_cover_reference(config, title)
                     if seq == 1 and not skip_api_call else None)
        if not skip_api_call:
            cover_anchor = bool(cover_ref)
            if seq == 1 and not cover_ref:
                raise RuntimeError('生成帧序列前必须先生成或选择封面图；第一帧只能以封面图进行图生图')
            reference = cover_ref if cover_anchor else previous_path
            if not reference or not os.path.exists(reference):
                raise RuntimeError(f'无法生成第 {seq} 帧：缺少上一帧参考图，帧序列禁止退回文生图')
            model = _image_edit_model(config)
            if on_progress:
                on_progress('frame_start', {
                    'slot': item['index'],
                    'sequence': seq,
                    'total': total_to_generate,
                })

            ctrl_prompt = IMG2IMG_CONTROL_PROMPT
            try:
                if cover_anchor:
                    # No generic prefix or label: send the parsed IMAGE 1 prompt verbatim.
                    ctrl_prompt = ''
                elif is_turn:
                    ctrl_prompt = IMG2IMG_BRIDGE_TURN_CONTROL_PROMPT
                elif is_bridge or is_cut_head:
                    ctrl_prompt = IMG2IMG_BRIDGE_CONTROL_PROMPT
                else:
                    ctrl_prompt = IMG2IMG_CONTROL_PROMPT
                transport = _generate_image_edit(config, item['prompt'], reference,
                                                 target_path, control_prompt=ctrl_prompt)
                if transport == CHAT_TRANSPORT and on_progress:
                    # This is a transport fallback; the request remains image-to-image.
                    on_progress('transport_fallback', {
                        'sequence': seq, 'transport': transport,
                        'degraded': not _chat_transport_is_full_quality(config),
                        'message': f"IMG {seq:03d} {chat_transport_note(config)}",
                    })
            except QuotaExhaustedError:
                # 主模型图片配额耗尽 = 明确失败，直接抛给上层。
                # 曾经这里会自动切到 imageEditFallbackModel（实配 gpt-image-2）继续渲：
                # 换模型意味着渲这张图的模型认不出"自己刚渲出来的东西"，实测会把纪实
                # 做旧材质渲成干净 CGI，还凭空发明提示词里没有的结构（2026-07-24 硅化
                # 巨木睡眠屋：一整扇玻璃门），并因链式编辑被下一帧当"已确认事实"继承。
                # 与其产出一串需要人工逐帧挑错的坏帧，不如就地停在配额耗尽这个真因上——
                # 已完成的帧由断点续传保住，补了额度点重试即可接着渲。
                raise
            except Exception as gen_err:
                # 同上：取消信号必须原样穿透，不能被"首帧重试一次 / 包装成 RuntimeError"
                # 的逻辑吞掉——否则用户点了取消，这里却把它当成一次普通生成失败去重试。
                if isinstance(gen_err, GenerationCancelled):
                    raise
                log('ERROR', 'FRAME_SEQ', f"第 {seq} 帧图生图失败（禁止回退文生图）: {gen_err}")
                raise RuntimeError(f"第 {seq} 帧图生图失败: {gen_err}")

            # Apply LAB color matching to prevent pink drift
            if seq > 1 and os.path.exists(target_path):
                first_frame_path = os.path.join(frames_dir, 'img_001.webp')
                if os.path.exists(first_frame_path):
                    if sys.stdout:
                        print(f"[COLOR MATCH] Aligning frame {seq} color to baseline first frame.")
                    _match_color_lab(target_path, first_frame_path, target_path)

            # P1 换族锚点惯性兜底（2026-07-15 盐湖贝壳单）：桥接帧由 i2i 生成时，
            # 参考惯性可能压过"进入新空间"的文本指令，渲出上一帧的近似复制帧——
            # 规划好的过门没执行，新空间在下一帧硬现身造成空间断裂。渲后与参考帧
            # 本地比对，近乎相同 = 惯性卡死，再以同一参考图做一次更强的 i2i 推进。
            if is_bridge:
                stuck, inertia_mad = detect_anchor_inertia(target_path, previous_path)
                if stuck:
                    if sys.stdout:
                        print(f"[ANCHOR INERTIA] Frame {seq} 与参考帧近乎相同（MAD={inertia_mad:.2f} < "
                              f"{_ANCHOR_INERTIA_MAD}），i2i 惯性未执行换族——加强图生图指令重渲")
                    if on_progress:
                        on_progress('anchor_inertia', {
                            'sequence': seq, 'mad': round(inertia_mad, 2),
                            'message': (f"IMG {seq:03d} 桥接帧被 i2i 参考惯性卡死"
                                        f"（与参考帧 MAD={inertia_mad:.2f}），正加强图生图指令重渲…"),
                        })
                    try:
                        transport = _generate_image_edit(
                            config, item['prompt'], previous_path, target_path,
                            control_prompt=IMG2IMG_BRIDGE_CONTROL_PROMPT)
                        model = _image_edit_model(config)
                        retries += 1
                        reference = previous_path
                        first_frame_path = os.path.join(frames_dir, 'img_001.webp')
                        if os.path.exists(first_frame_path):
                            _match_color_lab(target_path, first_frame_path, target_path)
                    except QuotaExhaustedError:
                        # 配额耗尽原样上抛，与主渲染路径同一套处置：不再切兜底模型，
                        # 也不吞掉失败后继续往下渲——下一帧照样会
                        # 撞同一堵墙，不如就地停在真因上。
                        raise
                    except Exception as inertia_err:
                        vlm_qa_reason = (f"anchor_inertia: 与参考帧近乎相同（MAD={inertia_mad:.2f}）"
                                         f"且 i2i 加强重试失败（{inertia_err}），保留原帧待人工重试")
                        if sys.stdout:
                            print(f"[ANCHOR INERTIA] Frame {seq} i2i 加强重试失败（{inertia_err}），保留原帧并留痕")

            # P0 门框清除兜底：单一过门拍产出的室内定格帧由 i2i 保守编辑生成——上一张
            # 外部参考帧门框占满画面时，编辑模型经常只做保守裁切，门框残留导致室内
            # 占比过小。渲出后立即对真实像素做单项 VLM 判定，未通过则以刚渲出的帧为
            # 参考、用推进版控制指令再推一步（把"过门"拆成两次连续推进），最多
            # _DOOR_CLEARANCE_MAX_PUSHES 次；推完仍不过只留痕，绝不拦渲染（终审交给
            # 整套序列一致性审查）。
            if (is_bridge
                    and image_space_family(videos, seq) == 'interior'):
                for _push in range(_DOOR_CLEARANCE_MAX_PUSHES + 1):
                    dc_passed, dc_reason = check_door_clearance_frame(config, target_path)
                    if on_progress:
                        _verdict = '通过' if dc_passed else '未通过'
                        _detail = f"（{dc_reason}）" if dc_reason and dc_reason != 'PASS' else ''
                        on_progress('door_clearance', {
                            'sequence': seq, 'passed': bool(dc_passed),
                            'reason': dc_reason, 'push': _push,
                            'message': f"门框清除检查 IMG {seq:03d}：{_verdict}{_detail}",
                        })
                    if dc_passed:
                        break
                    if _push >= _DOOR_CLEARANCE_MAX_PUSHES:
                        vlm_qa_reason = dc_reason
                        if sys.stdout:
                            print(f"[DOOR CLEARANCE] Frame {seq} still shows the door frame after "
                                  f"{_DOOR_CLEARANCE_MAX_PUSHES} extra push(es); keeping the frame "
                                  f"and recording the reason for the sequence review.")
                        break
                    push_ref = target_path + '.doorpush.webp'
                    is_final_push = (_push == _DOOR_CLEARANCE_MAX_PUSHES - 1)
                    try:
                        shutil.copyfile(target_path, push_ref)
                        if is_final_push:
                            # 前一轮 i2i 加推已经证实推不动——它仍然是拿同一张已经卡死
                            # 构图的参考帧去编辑，模型只会给出同样保守的结果（2026-07-22
                            # 喀斯特洞穴/沙漠花岗岩两单实测：2/2 次门框清除加推全部失败，
                            # 画面构图几乎原地不动）。最后一次仍使用当前画面作为参考，
                            # 但换成更强的 i2i 指令推进。
                            if sys.stdout:
                                print(f"[DOOR CLEARANCE] Frame {seq} failed door clearance twice "
                                      f"({dc_reason}); i2i pushes from the same stuck reference can't "
                                      f"break the locked composition — final attempt uses a "
                                      f"stronger i2i edit instruction.")
                            push_transport = _generate_image_edit(
                                config, item['prompt'], push_ref, target_path,
                                control_prompt=_door_clearance_push_prompt(
                                    dc_reason, final_attempt=True))
                            if push_transport == CHAT_TRANSPORT:
                                transport = push_transport
                            model = _image_edit_model(config)
                            # Keep the durable chain parent in the manifest; push_ref is temporary.
                            reference = previous_path
                        else:
                            if sys.stdout:
                                print(f"[DOOR CLEARANCE] Frame {seq} failed door clearance "
                                      f"({dc_reason}); pushing one more step past the threshold.")
                            push_transport = _generate_image_edit(
                                config, item['prompt'], push_ref, target_path,
                                control_prompt=_door_clearance_push_prompt(
                                    dc_reason, final_attempt=is_final_push))
                            # 加推这一步落进降档通道，最终落盘的就是降档帧——照样留痕
                            if push_transport == CHAT_TRANSPORT:
                                transport = push_transport
                        retries += 1
                        first_frame_path = os.path.join(frames_dir, 'img_001.webp')
                        if os.path.exists(first_frame_path):
                            _match_color_lab(target_path, first_frame_path, target_path)
                    except Exception as push_err:
                        # 取消信号必须原样穿透，不能被"留痕后继续"吞掉——否则用户点了
                        # 取消，这个门框清除的加推步骤却把它当成一次普通失败吸收掉，
                        # 循环会继续渲下一帧，取消形同没生效。
                        if isinstance(push_err, GenerationCancelled):
                            raise
                        # 再推失败：保留当前帧（已通过正常生成路径），留痕后继续
                        vlm_qa_reason = dc_reason
                        if sys.stdout:
                            print(f"[DOOR CLEARANCE] Extra push for frame {seq} failed "
                                  f"({push_err}); keeping the current frame.")
                        break
                    finally:
                        try:
                            if os.path.exists(push_ref):
                                os.remove(push_ref)
                        except OSError:
                            pass

            # P0 过门帧原始度兜底（2026-07-26 用户实测："过门帧有人工痕迹，不够原始"）：
            # 门框已经出画不代表这一帧对了——i2i 编辑模型进到室内后普遍把空间渲得像被
            # 布景过（地面扫净、杂物码整齐、表面看着刚修过），而按契约这一帧必须是没人
            # 进来过的废墟（下一拍的清理工序才动它）。文字契约与事后文本校验都只能管到
            # 提示词，这里对真实像素把关：未通过则以该帧自身为参考做定向状态修正（镜头
            # 不动，只改内容），最多 _RAW_STATE_MAX_FIXES 次；修完仍不过只留痕，绝不拦
            # 渲染（终审交给整套序列一致性审查）。
            if (is_bridge
                    and image_space_family(videos, seq) == 'interior'):
                for _fix in range(_RAW_STATE_MAX_FIXES + 1):
                    rs_passed, rs_reason = check_first_interior_reveal_raw_state(config, target_path)
                    if on_progress:
                        _verdict = '通过' if rs_passed else '未通过'
                        _detail = f"（{rs_reason}）" if rs_reason and rs_reason != 'PASS' else ''
                        on_progress('raw_state', {
                            'sequence': seq, 'passed': bool(rs_passed),
                            'reason': rs_reason, 'fix': _fix,
                            'message': f"过门帧原始度检查 IMG {seq:03d}：{_verdict}{_detail}",
                        })
                    if rs_passed:
                        break
                    if _fix >= _RAW_STATE_MAX_FIXES:
                        # 门框清除若也失败过，它的原因已经写进 vlm_qa_reason——两条都留，
                        # 序列审查/帧网格要看到的是这一帧全部未通过的项，不是最后一项。
                        vlm_qa_reason = '; '.join(x for x in (vlm_qa_reason, rs_reason) if x)
                        if sys.stdout:
                            print(f"[RAW STATE] Frame {seq} still reads as touched/tidied after "
                                  f"{_RAW_STATE_MAX_FIXES} correction(s); keeping the frame and "
                                  f"recording the reason for the sequence review.")
                        break
                    fix_ref = target_path + '.rawstate.webp'
                    try:
                        shutil.copyfile(target_path, fix_ref)
                        if sys.stdout:
                            print(f"[RAW STATE] Frame {seq} failed the raw-state audit "
                                  f"({rs_reason}); re-editing it back to an untouched ruin.")
                        fix_transport = _generate_image_edit(
                            config, item['prompt'], fix_ref, target_path,
                            control_prompt=_raw_state_fix_prompt(rs_reason))
                        # 这一步落进降档通道，最终落盘的就是降档帧——照样留痕
                        if fix_transport == CHAT_TRANSPORT:
                            transport = fix_transport
                        retries += 1
                        first_frame_path = os.path.join(frames_dir, 'img_001.webp')
                        if os.path.exists(first_frame_path):
                            _match_color_lab(target_path, first_frame_path, target_path)
                    except Exception as fix_err:
                        # 取消信号必须原样穿透（同门框清除加推：不能被"留痕后继续"吞掉）
                        if isinstance(fix_err, GenerationCancelled):
                            raise
                        vlm_qa_reason = '; '.join(x for x in (vlm_qa_reason, rs_reason) if x)
                        if sys.stdout:
                            print(f"[RAW STATE] Raw-state correction for frame {seq} failed "
                                  f"({fix_err}); keeping the current frame.")
                        break
                    finally:
                        try:
                            if os.path.exists(fix_ref):
                                os.remove(fix_ref)
                        except OSError:
                            pass

            # 不再逐帧质检——一致性审查移到整套序列渲染完成后统一跑一次，对着真实
            # 画面判断（见 pipeline_orchestrator._sequence_consistency_review）。
            current_quality_gate = 'pending_manual_review'
        else:
            reference = previous_path if seq > 1 else None
            existing_frame = manifest_frames_by_seq.get(seq)
            model = existing_frame.get('model', '') if existing_frame else _image_generation_model(config)
            retries = existing_frame.get('retry_count', 0) if existing_frame else 0
            
            current_quality_gate = existing_frame.get('quality_gate', 'pending_manual_review') if existing_frame else 'pending_manual_review'
            vlm_qa_reason = existing_frame.get('vlm_qa_reason') if existing_frame else None
            # 断点续传复用盘上这一帧时，降档留痕要跟着一起沿用——这一帧还是上一轮那张
            # 降档图，重放一次 manifest 不能把它洗成"正常帧"
            if existing_frame and existing_frame.get('transport') == CHAT_TRANSPORT:
                transport = CHAT_TRANSPORT
            if seq == 1 and existing_frame and existing_frame.get('anchor_reference') == 'cover':
                cover_anchor = True
                existing_ref = existing_frame.get('reference')
                if existing_ref:
                    reference = os.path.join(os.path.dirname(os.path.abspath(__file__)), existing_ref)

        rel_path = os.path.relpath(target_path, os.path.dirname(os.path.abspath(__file__))).replace('\\', '/')
        
        p_hash = ""
        if seq > 1:
            if skip_api_call and existing_frame and existing_frame.get('parent_hash'):
                p_hash = existing_frame['parent_hash']
            elif reference and previous_path and os.path.exists(previous_path):
                # parent_hash records the durable previous-frame link in the i2i chain.
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
        if cover_anchor:
            frame_info['anchor_reference'] = 'cover'
        if transport == CHAT_TRANSPORT:
            # 换过通道的帧如实标注。image_size 记的是"请求的档位"，这里再记一份真实
            # 像素——请求 2K/4K 时 chat 通道只给 1K，不记就会有 1K 帧混进后续挑帧/合成
            # 却看着像 2K 帧；请求本就是 1K 时两者一致，只是 degraded_reason 不再乱扣帽子。
            frame_info['transport'] = transport
            frame_info['actual_pixels'] = _measure_image_pixels(target_path)
            if not _chat_transport_is_full_quality(config):
                frame_info['degraded_reason'] = chat_transport_note(config)
        # 锚点门写入的提示词指纹要在断点续传/整轮重放时保留，
        # 否则下次 staged 调用会把已验过的首帧当作未验重新过门
        if skip_api_call and existing_frame and existing_frame.get('anchor_prompt_sha256'):
            frame_info['anchor_prompt_sha256'] = existing_frame['anchor_prompt_sha256']
        # 人工写下的问题描述（manual_issue，见 pipeline_orchestrator.set_manual_frame_issue）
        # 同理：这一帧压根没重渲、只是沿用盘上那张图，人指出的问题当然还在。不带过来
        # 的话 quality_gate 会留在 manual_flagged 却没有描述，帧网格显示"人工标记"但
        # 点开是空的。真正重渲过的帧不带——那张图已经换了，旧描述不再成立。
        if skip_api_call and existing_frame and existing_frame.get('manual_issue'):
            frame_info['manual_issue'] = existing_frame['manual_issue']
            if existing_frame.get('manual_flag_prev_gate') is not None:
                frame_info['manual_flag_prev_gate'] = existing_frame['manual_flag_prev_gate']
        # 一致性审查的留痕同理：这一帧没重渲，结论与它绑定的帧内容指纹都还成立。
        # 不带过来的话指纹会丢，drop_stale_review_verdicts 之后就再也无法判断这条
        # 结论是否过期（等于永久停在"看着像审过"的状态）。
        if skip_api_call and existing_frame:
            for key in ('review_frames_sha256', 'reviewed_at', 'review_issues'):
                if existing_frame.get(key) is not None:
                    frame_info[key] = existing_frame[key]

        manifest_frames_by_seq[seq] = frame_info
        previous_path = target_path
        generated_count += 1

        # 逐帧落盘（与 FX 路径一致）：旧行为只在整轮结束时写一次 manifest，
        # 中途崩溃会丢掉所有逐帧质检门禁记录，劣化帧拦截随之失效
        manifest['frames'] = [manifest_frames_by_seq[s] for s in sorted(manifest_frames_by_seq.keys())]
        update_manifest_stale_status(manifest, project_dir)
        write_manifest(project_dir, manifest)

        if on_progress:
            on_progress('frame', {'frame': frame_info, 'current': generated_count, 'total': total_to_generate})

    manifest['frames'] = [manifest_frames_by_seq[s] for s in sorted(manifest_frames_by_seq.keys())]
    update_manifest_stale_status(manifest, project_dir,
                                 regenerated_sequences=target_sequences, finalize=True)

    manifest_path = os.path.join(project_dir, 'manifest.json')
    write_manifest(project_dir, manifest)
    manifest['manifest'] = '/' + os.path.relpath(manifest_path, os.path.dirname(os.path.abspath(__file__))).replace('\\', '/')
    manifest['project_dir'] = os.path.abspath(project_dir)
    return manifest


def _image_task_cancelled(task_id):
    with IMAGE_TASKS_LOCK:
        t = IMAGE_TASKS.get(task_id)
        return bool(t and t.get('status') == 'cancelled')


def _finish_image_task(task_id, entry):
    """写终态，但绝不覆盖用户已取消的状态；保留 created_at 供前端算总用时。"""
    with IMAGE_TASKS_LOCK:
        current = IMAGE_TASKS.get(task_id)
        if current and current.get('status') == 'cancelled':
            return
        if current and current.get('created_at') and 'created_at' not in entry:
            entry = dict(entry, created_at=current['created_at'])
        IMAGE_TASKS[task_id] = entry


def _set_image_task_stage(task_id, stage):
    """更新图像站任务的当前阶段文案（前端实时生成动态轮询展示）。
    只在任务仍 pending 时写入——终态/已取消不回退。"""
    with IMAGE_TASKS_LOCK:
        t = IMAGE_TASKS.get(task_id)
        if t and t.get('status') == 'pending':
            t['stage'] = stage
            t['stage_at'] = time.time()


def _image_task_upstream_sink(task_id):
    """图像站任务的上游失败即时广播：每次尝试失败立刻写进 stage（含剩余重试信息），
    状态接口的长轮询会在 ~50ms 内把它推给前端——上游秒报错，前端秒可见。"""
    def _sink(ev):
        if ev.get('retry_in'):
            tail = f"，{ev['retry_in']}s 后自动重试（第 {ev['attempt']}/{ev['max_attempts']} 次）"
        else:
            tail = f"（第 {ev['attempt']}/{ev['max_attempts']} 次，已放弃重试）"
        _set_image_task_stage(task_id, f"⚠️ 上游报错：{ev.get('error', '未知错误')}{tail}"[:300])
    return _sink


def _run_async_image_generation(task_id, base_url, api_key, payload):
    # 每任务一条独立 daemon 线程，线程随任务结束销毁——sink 不需要显式清除
    set_upstream_event_sink(_image_task_upstream_sink(task_id))
    try:
        import urllib.request
        import json
        if _image_task_cancelled(task_id):
            return
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
        resp_bytes = _execute_request_with_retry(
            req, opener=opener, timeout=180,
            cancel_check=lambda: _image_task_cancelled(task_id),
            on_attempt=lambda a, m: _set_image_task_stage(
                task_id, '上游模型渲染中' if a == 1 else f'上游重试中（第 {a}/{m} 次尝试）'))
        _set_image_task_stage(task_id, '渲染完成，图像落盘中')
        resp_data = json.loads(resp_bytes.decode('utf-8'))
        resp_data = _save_image_station_result(resp_data)
        _finish_image_task(task_id, {'status': 'completed', 'result': resp_data, 'error': None})
    except ImageTaskCancelled:
        pass
    except urllib.error.HTTPError as e:
        detail = ''
        try:
            detail = e.read().decode('utf-8')[:500]
        except Exception: pass
        _finish_image_task(task_id, {'status': 'failed', 'result': None, 'error': f'Image API HTTP {e.code}: {detail}'})
    except Exception as e:
        _finish_image_task(task_id, {'status': 'failed', 'result': None, 'error': str(e)})


def _run_async_image_edit(task_id, base_url, api_key, body_data, boundary):
    # 每任务一条独立 daemon 线程，线程随任务结束销毁——sink 不需要显式清除
    set_upstream_event_sink(_image_task_upstream_sink(task_id))
    try:
        import urllib.request
        import json
        if _image_task_cancelled(task_id):
            return
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
        resp_bytes = _execute_request_with_retry(
            req, opener=opener, timeout=180,
            cancel_check=lambda: _image_task_cancelled(task_id),
            on_attempt=lambda a, m: _set_image_task_stage(
                task_id, '上游模型渲染中（图生图）' if a == 1 else f'上游重试中（第 {a}/{m} 次尝试）'))
        _set_image_task_stage(task_id, '渲染完成，图像落盘中')
        resp_data = json.loads(resp_bytes.decode('utf-8'))
        resp_data = _save_image_station_result(resp_data)
        _finish_image_task(task_id, {'status': 'completed', 'result': resp_data, 'error': None})
    except ImageTaskCancelled:
        pass
    except urllib.error.HTTPError as e:
        detail = ''
        try:
            detail = e.read().decode('utf-8')[:500]
        except Exception: pass
        _finish_image_task(task_id, {'status': 'failed', 'result': None, 'error': f'Image API HTTP {e.code}: {detail}'})
    except Exception as e:
        _finish_image_task(task_id, {'status': 'failed', 'result': None, 'error': str(e)})
