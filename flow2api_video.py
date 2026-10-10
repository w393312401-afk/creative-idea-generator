"""SPARK's synchronous batch facade for the Flow2API video SSE API.

Confirmed generation failures are retried within the original slot using its
configured extra-attempt budget. Unknown results retain their original receipt.
Downloads retain the original MP4 bytes,
including audio, and become visible only after complete media validation.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
from html.parser import HTMLParser
import hashlib
import io
import json
import math
from pathlib import Path
import re
import shutil
import tempfile
import time
from urllib.parse import urljoin, urlsplit
import uuid

import httpx
from fx_console import normalize_video_retry_count
from video_operations import safe_native_submission_diagnostic
from PIL import Image


_MESSAGES = {
    'configuration': 'Flow2API 服务地址或密钥未配置正确',
    'unsupported_model': '当前 Flow2API 视频通道仅支持 Omni Flash',
    'unsupported_reference_mode': '当前 Flow2API 视频通道仅支持首尾帧模式，未提交多参考图请求',
    'unsupported_options': 'Flow2API 不支持所选时长、比例或分辨率组合',
    'frame_missing': '首尾帧文件缺失，已停止提交，未降级为文生视频',
    'frame_invalid': '首尾帧图片无效或超过大小限制，已停止提交',
    'prompt_missing': '视频提示词不能为空',
    'media_tools_missing': '缺少 ffmpeg 或 ffprobe，已停止提交',
    'authorization': 'Flow2API 身份验证失败，请检查服务端密钥',
    'access_denied': 'Flow2API 服务拒绝访问（HTTP 403），请检查接口权限或网络/代理',
    'upstream_rejected': 'Flow2API 拒绝了视频生成请求',
    'upstream_blocked': 'Flow 视频生成被平台拦截，请检查 Flow2API 状态',
    'upstream_quota': 'Flow2API 视频生成额度不足或请求受限',
    'upstream_pending': 'Flow2API 视频结果待确认，已保留原提交收据，请核对原任务',
    'upstream_auth_pending': 'Flow2API 状态查询认证失效，原视频结果待确认，请核对原任务',
    'upstream_refused': '平台明确拒绝了本次生成，未创建视频任务；请恢复账号状态后重试',
    'upstream_failed': '原视频任务已确认生成失败，可在恢复服务后重试',
    'submission_not_started': 'Flow2API 在提交生成前失败，此片段尚未提交；请检查服务状态后重试',
    'upstream_unavailable': 'Flow2API 当前没有可用账号（账号校验未通过），本次未提交生成，请检查 Flow2API 账号与代理',
    'batch_stopped': '本批次已停止后续提交，此片段尚未提交；恢复 Flow2API 账号、服务访问或认证后，可手动重试未提交片段',
    'transport': 'Flow2API 连接中断',
    'timeout': 'Flow2API 视频生成超过时间上限',
    'stream_invalid': 'Flow2API 返回了无效或未完成的视频响应',
    'media_url': 'Flow2API 返回的视频地址不在允许的下载范围内',
    'download': 'Flow2API 视频下载失败，未交付不完整文件',
    'video_invalid': '下载的视频未通过时长、尺寸或完整解码校验',
    'callback': '视频交付状态保存失败，本次未自动重试',
    'output': '视频输出目录不可用',
    'rejected': '视频未通过项目验收',
}


class Flow2APIVideoError(RuntimeError):
    def __init__(self, code):
        self.code = code if code in _MESSAGES else 'transport'
        super().__init__(_MESSAGES[self.code])


class _Cancelled(ConnectionError):
    def __init__(self):
        super().__init__('视频生成已取消，已保留原视频')


class _VideoHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.urls = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == 'video':
            self.urls.extend(value for key, value in attrs if key.lower() == 'src' and value)


def _get(request, name, default=None):
    return request.get(name, default) if isinstance(request, dict) else getattr(request, name, default)


def _option(request, name, default=''):
    value = _get(request, name)
    return default if value is None or value == '' else value


def _origin(parsed):
    return parsed.scheme.lower(), (parsed.hostname or '').lower(), parsed.port or (443 if parsed.scheme == 'https' else 80)


class Flow2APIVideoService:
    MAX_IMAGE_BYTES = 20 * 1024 * 1024
    MAX_VIDEO_BYTES = 256 * 1024 * 1024
    MAX_STREAM_BYTES = 4 * 1024 * 1024
    MAX_RETRIES = 5
    RETRYABLE_ERRORS = frozenset({
        'submission_not_started', 'upstream_failed', 'upstream_refused',
        'upstream_pending', 'upstream_auth_pending', 'transport', 'timeout',
        'stream_invalid', 'authorization', 'access_denied', 'upstream_unavailable',
        'upstream_rejected', 'upstream_blocked', 'upstream_quota',
        'media_url', 'download', 'video_invalid', 'rejected',
    })

    def __init__(self, config):
        config = config or {}
        self.max_retries = normalize_video_retry_count(config.get('videoRetryCount'))
        if config.get('videoRefMode', 'VIDEO_FRAMES') != 'VIDEO_FRAMES':
            raise Flow2APIVideoError('unsupported_reference_mode')
        self._api_key = str(config.get('flow2apiApiKey') or '').strip()
        base = str(config.get('flow2apiBaseUrl') or 'http://127.0.0.1:38000/v1').rstrip('/')
        try:
            parsed = urlsplit(base)
            if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError()
            self._origin = _origin(parsed)
            self._root_url = f'{parsed.scheme}://{parsed.netloc}/'
            self._endpoint = base + ('/chat/completions' if parsed.path.rstrip('/').endswith('/v1') else '/v1/chat/completions')
            self.timeout = float(config.get('flow2apiVideoTimeoutSeconds') or 1800)
            if not self._api_key or not 0 < self.timeout <= 7200:
                raise ValueError()
        except (TypeError, ValueError):
            raise Flow2APIVideoError('configuration') from None
        self._client_factory = httpx.AsyncClient
        # One facade is created per SPARK generation task; chains reuse it for
        # successive single-slot calls. Never carry this stop into a new task.
        self._stopped_by = None
        try:
            value = config.get('flow2apiVideoConcurrency', 3)
            if isinstance(value, bool) or isinstance(value, float) and not value.is_integer():
                raise ValueError()
            concurrency = int(value)
        except (TypeError, ValueError, OverflowError):
            concurrency = 3
        self.concurrency = max(1, min(10, concurrency))
        self._ffprobe = shutil.which('ffprobe')
        self._ffmpeg = shutil.which('ffmpeg')

    @staticmethod
    def resolve_model(request):
        model = str(_get(request, 'model', '') or '').strip().lower()
        if any(_get(request, field) for field in ('images', 'reference_images', 'references', 'refs')):
            raise Flow2APIVideoError('unsupported_reference_mode')
        start = str(_option(request, 'image')).strip()
        end = str(_option(request, 'end_image')).strip()
        if end and not start:
            raise Flow2APIVideoError('frame_missing')
        if (_get(request, 'image_uuid') and not start) or (_get(request, 'end_image_uuid') and not end):
            raise Flow2APIVideoError('frame_missing')
        ratio = str(_option(request, 'ratio', '16:9')).strip().lower()
        orientation = {'16:9': 'landscape', '9:16': 'portrait', 'landscape': 'landscape', 'portrait': 'portrait'}.get(ratio)
        if not orientation:
            raise Flow2APIVideoError('unsupported_options')
        omni = model in {'omni flash', 'omni 1.1 flash', 'omni-1.1-flash'}
        raw_duration = str(_option(request, 'duration', 10 if omni else 8)).strip().lower()
        match = re.fullmatch(r'(\d+)(?:s|秒)?', raw_duration)
        seconds = int(match[1]) if match else 0
        resolution = str(_option(request, 'resolution', '720p')).strip().lower()
        if omni:
            if seconds not in {4, 6, 8, 10} or resolution not in {'360p', '720p'}:
                raise Flow2APIVideoError('unsupported_options')
            frame_part = '-frames' if start else ''
            return f'omni-1.1-flash{frame_part}-{seconds}s-{orientation}-{resolution}', seconds
        raise Flow2APIVideoError('unsupported_model')

    def _prepare(self, request):
        api_model, seconds = self.resolve_model(request)
        prompt = str(_get(request, 'prompt') or '').strip()
        if not prompt:
            raise Flow2APIVideoError('prompt_missing')
        if not self._ffprobe or not self._ffmpeg:
            raise Flow2APIVideoError('media_tools_missing')
        content = [{'type': 'text', 'text': prompt}]
        # Order is an API contract: first frame then last frame, never references.
        for field in ('image', 'end_image'):
            raw_path = str(_option(request, field)).strip()
            if not raw_path:
                continue
            path = Path(raw_path)
            try:
                if not path.is_file():
                    raise Flow2APIVideoError('frame_missing')
                if not 0 < path.stat().st_size <= self.MAX_IMAGE_BYTES:
                    raise ValueError()
                data = path.read_bytes()
                with Image.open(io.BytesIO(data)) as image:
                    mime = {'PNG': 'image/png', 'JPEG': 'image/jpeg', 'WEBP': 'image/webp'}.get(image.format)
                    if not mime or min(image.size) <= 0:
                        raise ValueError()
                    image.verify()
            except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
                raise Flow2APIVideoError('frame_invalid') from None
            content.append({'type': 'image_url', 'image_url': {'url': f'data:{mime};base64,' + base64.b64encode(data).decode('ascii')}})
        output_dir = Path(_get(request, 'output_path') or (Path.cwd() / 'output' / 'videos'))
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise Flow2APIVideoError('output') from None
        orientation, resolution = api_model.rsplit('-', 2)[-2:]
        short_edge = int(resolution[:-1])
        expected_size = (short_edge * 16 // 9, short_edge)
        if orientation == 'portrait':
            expected_size = expected_size[::-1]
        return {'payload': {'model': api_model, 'stream': True, 'messages': [{'role': 'user', 'content': content}]},
                'output_dir': output_dir, 'model': api_model, 'duration': seconds, 'expected_size': expected_size,
                'submission': {'submission_id': str(uuid.uuid4()),
                               'prompt_hash': hashlib.sha256(prompt.encode('utf-8')).hexdigest(),
                               'confirmed': False, 'submission_pending': False}}

    @staticmethod
    def _notify(callback, index, stage, details):
        if callback is None:
            return None
        try:
            return callback(index, stage, details)
        except ConnectionError:
            # GenerationCancelled is a ConnectionError subclass. Preserve its
            # identity so the task layer records cancellation, not failure.
            raise
        except Exception:
            raise Flow2APIVideoError('callback') from None

    @staticmethod
    def _check_cancel(cancel_check):
        if cancel_check and cancel_check():
            raise _Cancelled()

    async def _bounded(self, operation, cancel_check):
        task = asyncio.create_task(operation)
        deadline = time.monotonic() + self.timeout
        try:
            while not task.done():
                self._check_cancel(cancel_check)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise Flow2APIVideoError('timeout')
                await asyncio.wait({task}, timeout=min(0.2, remaining))
            return await task
        except BaseException:
            # Cancellation can race a completed download before this waiter has
            # delivered its result to the project callback. Remove that orphan.
            if task.done() and not task.cancelled() and task.exception() is None:
                result = task.result()
                if isinstance(result, dict) and result.get('video_url'):
                    with contextlib.suppress(OSError):
                        Path(result['video_url']).unlink()
            raise
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    @staticmethod
    def _business_error(payload):
        # Flow2API's common SSE error envelope does not identify whether the
        # native generation was already accepted. Even 401/quota errors can be
        # failures of the later status poll. Text matching cannot settle a paid
        # submission receipt; retain it until an actual result is recovered.
        # Only explicit machine-readable refusals before account selection or
        # arming the native generation button can settle an unaccepted receipt.
        if isinstance(payload, dict) and payload.get('submission_pending') is False:
            if payload.get('code') == 'no_available_token':
                return Flow2APIVideoError('upstream_unavailable')
            if payload.get('code') == 'submission_not_started':
                return Flow2APIVideoError('submission_not_started')
            if (payload.get('code') == 'submission_rejected'
                    and payload.get('upstream_accepted') is False
                    and not payload.get('operation_id') and not payload.get('task_id')):
                return Flow2APIVideoError('upstream_refused')
            if (payload.get('code') == 'video_generation_failed'
                    and payload.get('upstream_accepted') is True
                    and payload.get('operation_id') and payload.get('client_submission_id')):
                return Flow2APIVideoError('upstream_failed')
        if (isinstance(payload, dict) and payload.get('code') == 'video_status_unknown'
                and payload.get('submission_pending') is True
                and payload.get('upstream_accepted') is True
                and payload.get('reason') == 'poll_auth_failed'):
            return Flow2APIVideoError('upstream_auth_pending')
        return Flow2APIVideoError('upstream_pending')

    @staticmethod
    def _capture_submission(prepared, payload):
        """Keep only upstream identity, never arbitrary error text or credentials."""
        if not isinstance(payload, dict):
            return
        receipt = prepared['submission']
        client_id = payload.get('client_submission_id')
        if client_id and client_id != receipt['submission_id']:
            raise Flow2APIVideoError('stream_invalid')
        operation_id = payload.get('operation_id')
        if payload.get('code') == 'video_status_unknown' and not operation_id:
            operation_id = payload.get('task_id')
        if (operation_id and receipt.get('operation_id')
                and operation_id != receipt['operation_id']):
            raise Flow2APIVideoError('stream_invalid')
        for source, destination in (
            ('client_submission_id', 'client_submission_id'),
            ('operation_id', 'operation_id'), ('account_id', 'upstream_account_id'),
            ('code', 'upstream_error_code'), ('reason', 'upstream_reason'),
        ):
            value = payload.get(source)
            if isinstance(value, (str, int)) and not isinstance(value, bool):
                receipt[destination] = value
        # Earlier Flow2API error envelopes use task_id for Google's operation.
        if payload.get('code') == 'video_status_unknown' and isinstance(payload.get('task_id'), str):
            receipt['operation_id'] = payload['task_id']
        if payload.get('upstream_accepted') is True:
            receipt['upstream_accepted'] = True
        diagnostic = safe_native_submission_diagnostic(payload.get('native_submission_diagnostic'))
        if diagnostic:
            receipt['native_submission_diagnostic'] = diagnostic

    async def _generate(self, prepared, index, callback):
        if self._stopped_by:
            raise Flow2APIVideoError('batch_stopped')
        texts, urls = [], []
        terminal = False
        stream_bytes = 0
        # Native frame upload can precede the first SSE heartbeat for minutes.
        # _bounded owns the wall-clock deadline and cooperative cancellation.
        async with self._client_factory(timeout=httpx.Timeout(120, connect=10, read=None), follow_redirects=False, trust_env=False) as client:
            # A sibling can stop the batch while this client's transport opens.
            if self._stopped_by:
                raise Flow2APIVideoError('batch_stopped')
            # Auth is scoped to this one API request, never a client default.
            # A lost response cannot prove the paid request was rejected.
            prepared['submission']['submission_pending'] = True
            # Persist intent before sending: a disconnect or cancellation can
            # happen before HTTP response headers arrive. This is not acceptance.
            self._notify(callback, index, 'request_submitting', {'provider': 'flow2api', 'api_model': prepared['model'],
                         'acceptance': 'submission_pending', 'upstream_accepted': False, **prepared['submission']})
            async with client.stream('POST', self._endpoint, json=prepared['payload'],
                                     headers={'Authorization': f'Bearer {self._api_key}', 'Accept': 'text/event-stream',
                                              'X-SPARK-Submission-ID': prepared['submission']['submission_id']}) as response:
                if response.status_code in {400, 401, 402, 403, 404, 405, 413, 415, 422, 429}:
                    prepared['submission']['submission_pending'] = False
                if response.status_code == 401:
                    raise Flow2APIVideoError('authorization')
                if response.status_code == 403:
                    raise Flow2APIVideoError('access_denied')
                if response.status_code in {402, 429}:
                    raise Flow2APIVideoError('upstream_quota')
                if response.status_code != 200:
                    raise Flow2APIVideoError('upstream_pending' if prepared['submission']['submission_pending']
                                            else 'upstream_rejected')
                self._notify(callback, index, 'request_submitted', {'provider': 'flow2api', 'api_model': prepared['model'],
                             'acceptance': 'http_transport_only', 'upstream_accepted': False, **prepared['submission']})
                if 'text/event-stream' not in response.headers.get('content-type', '').lower():
                    raise Flow2APIVideoError('stream_invalid')
                event_lines = []

                def consume_event():
                    nonlocal terminal
                    if not event_lines:
                        return
                    data = '\n'.join(event_lines)
                    event_lines.clear()
                    if data.strip() == '[DONE]':
                        terminal = True
                        return
                    try:
                        payload = json.loads(data)
                    except (ValueError, TypeError):
                        raise Flow2APIVideoError('stream_invalid') from None
                    if not isinstance(payload, dict):
                        raise Flow2APIVideoError('stream_invalid')
                    if isinstance(payload.get('submission'), dict):
                        self._capture_submission(prepared, payload['submission'])
                        self._notify(callback, index, 'request_accepted', {
                            'provider': 'flow2api', 'api_model': prepared['model'],
                            'acceptance': 'upstream_operation_available', **prepared['submission']})
                    if payload.get('error'):
                        self._capture_submission(prepared, payload['error'])
                        error = self._business_error(payload['error'])
                        # Never clear the receipt once a video link has been seen.
                        if error.code in {'upstream_unavailable', 'submission_not_started', 'upstream_refused', 'upstream_failed'}:
                            seen = _VideoHTML()
                            seen.feed(''.join(texts))
                            accepted_but_refused = (error.code != 'upstream_failed'
                                                   and prepared['submission'].get('upstream_accepted') is True)
                            if urls or seen.urls or accepted_but_refused:
                                error = Flow2APIVideoError('upstream_pending')
                            else:
                                prepared['submission']['submission_pending'] = False
                        raise error
                    for choice in payload.get('choices') or []:
                        if not isinstance(choice, dict):
                            raise Flow2APIVideoError('stream_invalid')
                        finish = choice.get('finish_reason')
                        if finish and finish != 'stop':
                            if finish == 'content_filter':
                                raise Flow2APIVideoError('upstream_pending')
                            raise Flow2APIVideoError('stream_invalid')
                        terminal = terminal or finish == 'stop'
                        delta = choice.get('delta') or choice.get('message') or {}
                        content = delta.get('content') if isinstance(delta, dict) else None
                        if isinstance(content, str):
                            texts.append(content)
                        elif isinstance(content, list):
                            for item in content:
                                if isinstance(item, dict) and item.get('type') == 'video_url':
                                    value = item.get('video_url') or {}
                                    if isinstance(value, dict) and isinstance(value.get('url'), str):
                                        urls.append(value['url'])

                async for line in response.aiter_lines():
                    stream_bytes += len(line.encode('utf-8'))
                    if stream_bytes > self.MAX_STREAM_BYTES:
                        raise Flow2APIVideoError('stream_invalid')
                    if not line:
                        consume_event()
                    elif line.startswith('data:'):
                        event_lines.append(line[5:].lstrip())
                consume_event()
        parser = _VideoHTML()
        parser.feed(''.join(texts))
        urls.extend(parser.urls)
        urls = list(dict.fromkeys(urls))
        if not terminal or len(urls) != 1:
            raise Flow2APIVideoError('stream_invalid')
        prepared['submission'].update(confirmed=True, submission_pending=False)
        # Journal Google's terminal result before local download/decoding, where
        # cancellation must not leave the last durable receipt pending forever.
        self._notify(callback, index, 'request_resolved', {'provider': 'flow2api', 'api_model': prepared['model'],
                     'acceptance': 'upstream_result_available', 'upstream_accepted': True, **prepared['submission']})
        return await self._download(urls[0], prepared)

    def _validate_media_url(self, value):
        try:
            absolute = urljoin(self._root_url, value)
            parsed = urlsplit(absolute)
            if parsed.username or parsed.password or parsed.fragment or parsed.scheme not in {'http', 'https'}:
                raise ValueError()
            host = (parsed.hostname or '').lower()
            same_origin = _origin(parsed) == self._origin
            google_media = parsed.scheme == 'https' and (parsed.port in (None, 443)) and (
                host == 'flow-content.google' or host.endswith('.flow-content.google') or
                host == 'googleusercontent.com' or host.endswith('.googleusercontent.com') or
                host == 'googlevideo.com' or host.endswith('.googlevideo.com'))
            if not (same_origin or google_media):
                raise ValueError()
            return absolute
        except (TypeError, ValueError):
            raise Flow2APIVideoError('media_url') from None

    async def _download(self, url, prepared):
        url = self._validate_media_url(url)
        fd, temporary = tempfile.mkstemp(prefix='.flow2api-', suffix='.part.mp4', dir=prepared['output_dir'])
        import os
        os.close(fd)
        temporary = Path(temporary)
        destination = prepared['output_dir'] / f'flow2api_{uuid.uuid4().hex}.mp4'
        try:
            # A separate unauthenticated client also avoids carrying API cookies.
            async with self._client_factory(timeout=httpx.Timeout(60, connect=10), follow_redirects=False, trust_env=False) as client:
                for hop in range(4):
                    async with client.stream('GET', url, headers={'Accept': 'video/mp4,application/octet-stream'}) as response:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            if hop == 3 or not response.headers.get('location'):
                                raise Flow2APIVideoError('download')
                            url = self._validate_media_url(urljoin(url, response.headers['location']))
                            continue
                        if response.status_code != 200:
                            raise Flow2APIVideoError('download')
                        size = 0
                        with temporary.open('wb') as output:
                            async for data in response.aiter_bytes():
                                size += len(data)
                                if size > self.MAX_VIDEO_BYTES:
                                    raise Flow2APIVideoError('video_invalid')
                                output.write(data)
                            output.flush()
                            os.fsync(output.fileno())
                        break
            metadata = await self._validate_video(temporary, prepared['duration'], prepared['expected_size'])
            os.replace(temporary, destination)
            return {'status': 'success', 'video_url': str(destination), 'provider': 'flow2api',
                    'model': prepared['model'], 'api_model': prepared['model'], **metadata, **prepared['submission']}
        finally:
            with contextlib.suppress(OSError):
                temporary.unlink()

    async def _media_command(self, args):
        process = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            stdout, _ = await process.communicate()
            if process.returncode:
                raise Flow2APIVideoError('video_invalid')
            return stdout
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=1)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()

    async def _validate_video(self, path, expected_duration, expected_size):
        try:
            if path.stat().st_size < 32:
                raise ValueError()
            with path.open('rb') as source:
                if b'ftyp' not in source.read(64):
                    raise ValueError()
            raw = await self._media_command([self._ffprobe, '-v', 'error', '-show_entries',
                'stream=codec_type,width,height,duration:format=duration,format_name', '-of', 'json', str(path)])
            info = json.loads(raw)
            stream = next(s for s in info.get('streams', []) if s.get('codec_type') == 'video')
            duration = float(stream.get('duration') or info.get('format', {}).get('duration') or 0)
            width, height = int(stream.get('width') or 0), int(stream.get('height') or 0)
            if not math.isfinite(duration) or duration <= 0 or width <= 0 or height <= 0:
                raise ValueError()
            if abs(width - expected_size[0]) > 2 or abs(height - expected_size[1]) > 2:
                raise ValueError()
            if abs(duration - expected_duration) > max(0.6, expected_duration * 0.1):
                raise ValueError()
            await self._media_command([self._ffmpeg, '-v', 'error', '-xerror', '-nostdin', '-threads', '1',
                '-i', str(path), '-map', '0:v:0', '-map', '0:a?', '-f', 'null', '-'])
            return {'duration': duration, 'width': width, 'height': height,
                    'has_audio': any(s.get('codec_type') == 'audio' for s in info.get('streams', []))}
        except (OSError, ValueError, StopIteration, KeyError):
            raise Flow2APIVideoError('video_invalid') from None

    def generate_videos_batch_google_fx(self, reqs, on_progress=None, cancel_check=None):
        if hasattr(reqs, 'items') and not isinstance(reqs, (list, tuple, dict)):
            batch = reqs
            fields = ('prompt', 'image', 'end_image', 'image_uuid', 'end_image_uuid', 'images',
                      'reference_images', 'references', 'refs', 'model', 'ratio', 'duration', 'resolution', 'output_path')
            reqs = [{field: _get(item, field) for field in fields} for item in (batch.items or [])]
            for request in reqs:
                for field in ('model', 'ratio', 'duration', 'resolution', 'output_path'):
                    request[field] = _option(request, field, _get(batch, field))
        reqs = list(reqs)
        prepared = []
        for request in reqs:
            self._check_cancel(cancel_check)
            try:
                prepared.append(self._prepare(request))
            except Flow2APIVideoError as error:
                prepared.append(error)

        async def run():
            results = [None] * len(prepared)
            next_index = 0
            fatal_error = None

            def notify(index, stage, details):
                # Callbacks remain serialized on this event loop. Latch failure
                # immediately so siblings cannot send a paid POST after a
                # receipt checkpoint or cancellation callback has failed.
                nonlocal fatal_error
                if fatal_error is not None:
                    raise fatal_error
                try:
                    return self._notify(on_progress, index, stage, details)
                except BaseException as error:
                    fatal_error = error
                    raise

            def stopped_result(item):
                failure = {'status': 'failed', 'video_url': None, 'provider': 'flow2api',
                           'message': _MESSAGES['batch_stopped'], 'code': 'batch_stopped',
                           'stopped_by': self._stopped_by, 'retry_after_recovery': True,
                           'submission_pending': False, 'confirmed': False}
                if isinstance(item, dict):
                    failure['api_model'] = item['model']
                return failure

            async def generate_once(item, index):
                result = None
                try:
                    result = await self._generate(item, index, notify)
                    self._check_cancel(cancel_check)
                    accepted = notify(index, 'video_done', result)
                    if accepted == 'rejected':
                        raise Flow2APIVideoError('rejected')
                    return result
                except BaseException:
                    if result and result.get('video_url'):
                        with contextlib.suppress(OSError):
                            Path(result['video_url']).unlink()
                    raise

            async def generate(item, index):
                for attempt in range(self.max_retries + 1):
                    self._check_cancel(cancel_check)
                    try:
                        # Each attempt gets a full deadline, including download.
                        return await self._bounded(generate_once(item, index), cancel_check)
                    except (ConnectionError, asyncio.CancelledError):
                        raise
                    except Exception as caught:
                        error = caught if isinstance(caught, Flow2APIVideoError) else Flow2APIVideoError('transport')
                        receipt = item['submission']
                        retryable = (error.code in self.RETRYABLE_ERRORS
                                     and receipt.get('submission_pending') is False)
                        if retryable and attempt < self.max_retries and not self._stopped_by:
                            # Only a terminal receipt permits another paid POST.
                            # Timeout, disconnect and unknown upstream status keep
                            # the original receipt pending without resubmission.
                            if receipt.get('submission_pending') is False:
                                notify(index, 'request_resolved', {
                                    'provider': 'flow2api', 'api_model': item['model'],
                                    'acceptance': 'upstream_failure_confirmed',
                                    'status': 'failed', 'code': error.code, **receipt})
                            notify(index, 'video_warning', {
                                'provider': 'flow2api', 'index': index,
                                'code': 'flow2api_auto_retry', 'retry': attempt + 1,
                                'max_retries': self.max_retries,
                                'message': f'Flow2API 视频生成失败，正在重试（{attempt + 1}/{self.max_retries}）',
                            })
                            await asyncio.sleep(0)
                            self._check_cancel(cancel_check)
                            if self._stopped_by:
                                raise error
                            # Re-read the request so an anchor rejection's adapted
                            # prompt reaches the next POST, with a fresh identity.
                            fresh = self._prepare(reqs[index])
                            item.clear()
                            item.update(fresh)
                            notify(index, 'video_start', {'provider': 'flow2api', 'api_model': item['model']})
                            continue
                        if error.code in {'upstream_unavailable', 'authorization', 'access_denied',
                                          'upstream_auth_pending', 'upstream_refused'}:
                            # Stop before waking another worker, while requests
                            # already sent finish and retain their receipts.
                            self._stopped_by = error.code
                        raise error

            async def process(index, item):
                self._check_cancel(cancel_check)
                if self._stopped_by:
                    # Only this task is paused. Preserve earlier success/pending
                    # receipts, and never invent a submission for an untouched slot.
                    failure = stopped_result(item)
                    results[index] = failure
                    notify(index, 'video_error', failure)
                    return
                result = None
                try:
                    if isinstance(item, Flow2APIVideoError):
                        raise item
                    notify(index, 'video_start', {'provider': 'flow2api', 'api_model': item['model']})
                    result = await generate(item, index)
                    self._check_cancel(cancel_check)
                    results[index] = result
                except (ConnectionError, asyncio.CancelledError) as error:
                    if result and result.get('video_url'):
                        with contextlib.suppress(OSError):
                            Path(result['video_url']).unlink()
                    if isinstance(item, dict) and not isinstance(error, asyncio.CancelledError):
                        error.submission_details = {'provider': 'flow2api', 'api_model': item['model'], **item['submission']}
                    raise
                except Exception as error:
                    if result and result.get('video_url'):
                        with contextlib.suppress(OSError):
                            Path(result['video_url']).unlink()
                    # The task layer uses progress callbacks to persist paid
                    # submission receipts. A failed checkpoint must stop this
                    # batch, never permit a later item to spend more credits.
                    if isinstance(error, Flow2APIVideoError) and error.code == 'callback':
                        raise
                    safe = error if isinstance(error, Flow2APIVideoError) else Flow2APIVideoError('transport')
                    failure = (stopped_result(item) if safe.code == 'batch_stopped' else
                               {'status': 'failed', 'video_url': None, 'provider': 'flow2api', 'message': str(safe), 'code': safe.code})
                    if isinstance(item, dict) and safe.code != 'batch_stopped':
                        failure.update(api_model=item['model'], **item['submission'])
                    # These are definitive service/account refusals before
                    # generation. Generic SSE errors may be later poll failures;
                    # HTTP 429/concurrency limits must not poison the whole batch.
                    if safe.code in {'upstream_unavailable', 'authorization', 'access_denied',
                                      'upstream_auth_pending', 'upstream_refused'}:
                        self._stopped_by = safe.code
                        failure['retry_after_recovery'] = True
                    results[index] = failure
                    notify(index, 'video_error', failure)

            async def worker():
                nonlocal next_index
                while next_index < len(prepared):
                    if fatal_error is not None:
                        raise fatal_error
                    self._check_cancel(cancel_check)
                    index = next_index
                    next_index += 1
                    await process(index, prepared[index])

            workers = [asyncio.create_task(worker()) for _ in range(min(self.concurrency, len(prepared)))]
            try:
                await asyncio.gather(*workers)
            finally:
                # Do not let gather's early exception leave another SSE stream,
                # download or ffmpeg process running after the facade returns.
                for task in workers:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*workers, return_exceptions=True)
            return results
        return asyncio.run(run())
