import asyncio
import base64
import hashlib
import io
import json
from pathlib import Path
import tempfile
import types
import unittest
import uuid
from unittest.mock import AsyncMock, patch

import httpx
from PIL import Image

from flow2api_video import Flow2APIVideoError, Flow2APIVideoService


KEY = 'secret-test-key'
MEDIA = 'http://127.0.0.1:38000/tmp/synthetic.mp4'
MP4 = b'\x00\x00\x00\x18ftypisom' + b'original-video-and-audio-bytes' * 3


def sse(*payloads):
    return ''.join('data: ' + (p if isinstance(p, str) else json.dumps(p)) + '\n\n' for p in payloads).encode()


def done(url=MEDIA):
    return {'choices': [{'delta': {'content': f"<video src='{url}' controls></video>"}, 'finish_reason': 'stop'}]}


class BlockingStream(httpx.AsyncByteStream):
    def __init__(self, chunk, state):
        self.chunk, self.state, self.closed = chunk, state, False

    async def __aiter__(self):
        yield self.chunk
        self.state['cancel'] = True
        await asyncio.sleep(20)

    async def aclose(self):
        self.closed = True


class Flow2APIVideoTests(unittest.TestCase):
    def test_native_submission_diagnostic_is_sanitized_without_settling_receipt(self):
        prepared = {'submission': {'submission_id': 'same-submission', 'submission_pending': True}}
        Flow2APIVideoService._capture_submission(prepared, {
            'client_submission_id': 'same-submission', 'code': 'generation_failed',
            'native_submission_diagnostic': {
                'stage': 'native_submit', 'error_kind': 'TimeoutError', 'failure_kind': 'timeout',
                'forwarding_started': True, 'page_closed': True, 'submit_attempts': 1,
                'raw_error': 'https://private.example/?cookie=secret', 'cookie': 'secret',
            },
        })
        receipt = prepared['submission']
        self.assertIs(receipt['submission_pending'], True)
        self.assertEqual(receipt['native_submission_diagnostic']['error_kind'], 'TimeoutError')
        self.assertNotIn('secret', json.dumps(receipt['native_submission_diagnostic']))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.calls = []
        self.events = []
        self.config = {'flow2apiBaseUrl': 'http://127.0.0.1:38000/v1', 'flow2apiApiKey': KEY,
                       'flow2apiVideoTimeoutSeconds': 2, 'flow2apiVideoConcurrency': 1, 'videoRetryCount': 2}

    def request(self, **values):
        fields = dict(prompt='synthetic prompt', image='', end_image='', image_uuid='', end_image_uuid='',
                      ratio='16:9', model='Omni Flash', duration='10', resolution='360p', output_path=str(self.directory))
        fields.update(values)
        return types.SimpleNamespace(**fields)

    def service(self, handler, validate=True):
        with patch('flow2api_video.shutil.which', side_effect=lambda value: '/synthetic/' + value):
            service = Flow2APIVideoService(self.config)
        service._client_factory = lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)
        if validate:
            service._validate_video = AsyncMock(return_value={'duration': 10, 'width': 640, 'height': 360, 'has_audio': True})
        return service

    def callback(self, *args):
        self.events.append(args)

    def success_handler(self, request):
        self.calls.append(request)
        if request.method == 'POST':
            return httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=sse(done(), '[DONE]'))
        self.assertNotIn('authorization', request.headers)
        return httpx.Response(200, content=MP4, headers={'content-type': 'video/mp4'})

    def image(self, name, color):
        path = self.directory / name
        Image.new('RGB', (8, 8), color).save(path)
        return str(path)

    def test_explicit_omni_model_mapping_preserves_frame_semantics(self):
        for count in (0, 1, 2):
            for seconds in (4, 6, 8, 10):
                for ratio, orientation in (('16:9', 'landscape'), ('9:16', 'portrait')):
                    for resolution in ('360p', '720p'):
                        request = self.request(image='first' if count else '', end_image='last' if count == 2 else '',
                                               duration=str(seconds), ratio=ratio, resolution=resolution)
                        model, duration = Flow2APIVideoService.resolve_model(request)
                        self.assertEqual(model, f'omni-1.1-flash{"-frames" if count else ""}-{seconds}s-{orientation}-{resolution}')
                        self.assertEqual(duration, seconds)

    def test_non_frame_config_modes_fail_before_any_network_client_is_created(self):
        with patch('flow2api_video.httpx.AsyncClient') as client:
            for mode in ('VIDEO_REFERENCES', 'VIDEO_INGREDIENTS', '', None, False):
                with self.assertRaises(Flow2APIVideoError) as raised:
                    Flow2APIVideoService({**self.config, 'videoRefMode': mode})
                self.assertEqual(raised.exception.code, 'unsupported_reference_mode')
            Flow2APIVideoService(self.config)
            Flow2APIVideoService({**self.config, 'videoRefMode': 'VIDEO_FRAMES'})
            client.assert_not_called()

    def test_reference_lists_and_explicit_invalid_options_are_never_dropped(self):
        handler = unittest.mock.Mock(side_effect=AssertionError('must not submit'))
        for field in ('images', 'reference_images', 'references', 'refs'):
            result = self.service(handler).generate_videos_batch_google_fx([self.request(**{field: ['reference.png']})])[0]
            self.assertEqual(result['code'], 'unsupported_reference_mode')
        for field in ('duration', 'ratio', 'resolution'):
            for value in (0, False):
                result = self.service(handler).generate_videos_batch_google_fx([self.request(**{field: value})])[0]
                self.assertEqual(result['code'], 'unsupported_options')
        for field in ('image', 'end_image'):
            result = self.service(handler).generate_videos_batch_google_fx([self.request(**{field: 0})])[0]
            self.assertEqual(result['code'], 'frame_missing')
        handler.assert_not_called()

    def test_batch_envelope_defaults_are_inherited_without_overwriting_item_choices(self):
        service = self.service(self.success_handler)
        batch = types.SimpleNamespace(model='Omni Flash', ratio='9:16', duration='6', resolution='720p',
                                      output_path=str(self.directory), items=[
            self.request(model='', ratio=None, duration='', resolution=None, output_path=''),
            self.request(duration='8')])
        results = service.generate_videos_batch_google_fx(batch)
        self.assertTrue(all(result['status'] == 'success' for result in results))
        models = [json.loads(call.content)['model'] for call in self.calls if call.method == 'POST']
        self.assertEqual(models, ['omni-1.1-flash-6s-portrait-720p', 'omni-1.1-flash-8s-landscape-360p'])
        self.assertEqual([args.args[1] for args in service._validate_video.await_args_list], [6, 8])
        self.assertTrue(all(Path(result['video_url']).parent == self.directory for result in results))

    def test_unverified_veo_models_are_rejected_before_submission(self):
        handler = unittest.mock.Mock(side_effect=AssertionError('must not submit'))
        for model in ('Veo 3.1 - Fast', 'Veo 3.1 - Quality'):
            result = self.service(handler).generate_videos_batch_google_fx([
                self.request(model=model, image='first', end_image='last', duration='8', resolution=None)])[0]
            self.assertEqual(result['code'], 'unsupported_model')
        handler.assert_not_called()

    def test_frames_order_single_submission_and_original_audio_bytes(self):
        first, last = self.image('first.png', 'red'), self.image('last.png', 'blue')
        service = self.service(self.success_handler)
        result = service.generate_videos_batch_google_fx([self.request(image=first, end_image=last)], self.callback)[0]
        self.assertEqual(result['status'], 'success')
        self.assertTrue(result['has_audio'])
        self.assertEqual(Path(result['video_url']).read_bytes(), MP4)
        posts = [r for r in self.calls if r.method == 'POST']
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0].headers['authorization'], f'Bearer {KEY}')
        body = json.loads(posts[0].content)
        content = body['messages'][0]['content']
        self.assertEqual(base64.b64decode(content[1]['image_url']['url'].split(',', 1)[1]), Path(first).read_bytes())
        self.assertEqual(base64.b64decode(content[2]['image_url']['url'].split(',', 1)[1]), Path(last).read_bytes())
        self.assertEqual([event[1] for event in self.events], ['video_start', 'request_submitting', 'request_submitted', 'request_resolved', 'video_done'])
        submitting = self.events[1][2]
        self.assertEqual(submitting['acceptance'], 'submission_pending')
        self.assertTrue(submitting['submission_pending'])
        self.assertFalse(submitting['confirmed'])
        accepted = self.events[2][2]
        self.assertEqual(accepted['acceptance'], 'http_transport_only')
        self.assertFalse(accepted['upstream_accepted'])
        self.assertFalse(accepted['confirmed'])
        self.assertTrue(accepted['submission_pending'])
        uuid.UUID(accepted['submission_id'])
        self.assertEqual(accepted['prompt_hash'], hashlib.sha256(b'synthetic prompt').hexdigest())
        self.assertEqual(result['submission_id'], accepted['submission_id'])
        self.assertEqual(submitting['submission_id'], accepted['submission_id'])
        self.assertTrue(result['confirmed'])
        self.assertFalse(result['submission_pending'])
        resolved = self.events[-2][2]
        self.assertEqual(resolved['submission_id'], result['submission_id'])
        self.assertTrue(resolved['confirmed'])
        self.assertFalse(resolved['submission_pending'])
        self.assertNotIn('account_id', result)
        self.assertNotIn(KEY, repr(self.events))
        self.assertEqual(list(self.directory.glob('*.part.mp4')), [])

    def test_stream_business_error_after_video_link_stays_pending_without_resubmission(self):
        def handler(request):
            self.calls.append(request)
            return httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=sse(
                done(), {'error': {'message': 'UNUSUAL_ACTIVITY cookie=private https://secret.invalid/?key=' + KEY}}, '[DONE]'))
        result = self.service(handler).generate_videos_batch_google_fx([self.request()], self.callback)[0]
        self.assertEqual(result['code'], 'upstream_pending')
        self.assertTrue(result['submission_pending'])
        self.assertFalse(result['confirmed'])
        self.assertEqual(result['submission_id'], self.calls[-1].headers['X-SPARK-Submission-ID'])
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn(KEY, repr(result) + repr(self.events))
        self.assertNotIn('private', repr(result) + repr(self.events))
        self.assertEqual(self.events[-1][1], 'video_error')
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_explicit_no_available_token_refusal_clears_pending_receipt(self):
        error = {'message': 'no account', 'code': 'no_available_token', 'submission_pending': False}
        def handler(request):
            self.calls.append(request)
            return httpx.Response(200, headers={'content-type': 'text/event-stream'},
                                  content=sse({'error': error}, '[DONE]'))
        result = self.service(handler).generate_videos_batch_google_fx([self.request()], self.callback)[0]
        self.assertEqual(result['code'], 'upstream_unavailable')
        self.assertFalse(result['submission_pending'])
        self.assertFalse(result['confirmed'])
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.events[-1][1], 'video_error')
        self.assertFalse(self.events[-1][2]['submission_pending'])

    def test_no_available_token_claim_after_video_link_stays_pending(self):
        def handler(request):
            self.calls.append(request)
            return httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=sse(
                done(), {'error': {'code': 'no_available_token', 'submission_pending': False}}, '[DONE]'))
        result = self.service(handler).generate_videos_batch_google_fx([self.request()], self.callback)[0]
        self.assertEqual(result['code'], 'upstream_pending')
        self.assertTrue(result['submission_pending'])

    def test_structured_pre_submission_failure_retries_only_its_slot(self):
        posts = []
        def handler(request):
            if request.method == 'POST':
                posts.append(request)
                body = (sse({'error': {'code': 'submission_not_started', 'submission_pending': False,
                                      'message': 'native editor failure'}}) if len(posts) == 1 else sse(done(), '[DONE]'))
                return httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=body)
            return httpx.Response(200, content=MP4)
        results = self.service(handler).generate_videos_batch_google_fx([self.request(), self.request()], self.callback)
        self.assertEqual(results[0]['status'], 'success')
        self.assertFalse(results[0]['submission_pending'])
        self.assertTrue(results[0]['confirmed'])
        original = self.events[1][2]['submission_id']
        resolved = next(details for _, stage, details in self.events
                        if stage == 'request_resolved' and details['submission_id'] == original)
        self.assertFalse(resolved['submission_pending'])
        self.assertEqual(resolved['code'], 'submission_not_started')
        self.assertNotEqual(results[0]['submission_id'], original)
        self.assertEqual(results[1]['status'], 'success')
        self.assertEqual(len(posts), 3)

    def test_native_rejection_retries_then_stops_unusual_activity_batch(self):
        def handler(request):
            self.calls.append(request)
            return httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=sse({
                'error': {'code': 'submission_rejected', 'submission_pending': False,
                          'upstream_accepted': False, 'reason': 'unusual_activity',
                          'client_submission_id': request.headers['X-SPARK-Submission-ID']}}))
        results = self.service(handler).generate_videos_batch_google_fx(
            [self.request(), self.request()], self.callback)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(results[0]['code'], 'upstream_refused')
        self.assertFalse(results[0]['submission_pending'])
        self.assertEqual(results[0]['client_submission_id'], results[0]['submission_id'])
        self.assertEqual(results[1]['code'], 'batch_stopped')
        self.assertNotIn('submission_id', results[1])

    def test_accepted_operation_identity_survives_poll_auth_failure_and_stops_batch(self):
        def handler(request):
            self.calls.append(request)
            identity = {'client_submission_id': request.headers['X-SPARK-Submission-ID'],
                        'operation_id': 'original-google-operation', 'account_id': 2,
                        'upstream_accepted': True, 'submission_pending': True}
            return httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=sse(
                {'submission': {**identity, 'state': 'pending'}},
                {'error': {**identity, 'code': 'video_status_unknown', 'reason': 'poll_auth_failed'}}))
        results = self.service(handler).generate_videos_batch_google_fx(
            [self.request(), self.request()], self.callback)
        self.assertEqual(len(self.calls), 1)
        pending = results[0]
        self.assertEqual(pending['code'], 'upstream_auth_pending')
        self.assertTrue(pending['submission_pending'])
        self.assertTrue(pending['upstream_accepted'])
        self.assertEqual(pending['operation_id'], 'original-google-operation')
        self.assertEqual(pending['upstream_account_id'], 2)
        self.assertEqual(results[1]['code'], 'batch_stopped')
        self.assertEqual(results[1]['stopped_by'], 'upstream_auth_pending')
        self.assertEqual([event[1] for event in self.events].count('request_submitted'), 1)
        self.assertFalse(any(stage == 'request_resolved' for _, stage, _ in self.events))
        accepted = next(event[2] for event in self.events if event[1] == 'request_accepted')
        self.assertEqual(accepted['operation_id'], pending['operation_id'])

    def test_explicit_native_access_refusal_retries_then_stops_later_posts(self):
        def handler(request):
            self.calls.append(request)
            return httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=sse({
                'error': {'code': 'submission_rejected', 'submission_pending': False,
                          'upstream_accepted': False, 'reason': 'upstream_rejected'}}))
        results = self.service(handler).generate_videos_batch_google_fx([self.request(), self.request()])
        self.assertEqual(len(self.calls), 3)
        self.assertFalse(results[0]['submission_pending'])
        self.assertEqual(results[1]['code'], 'batch_stopped')
        self.assertNotIn('submission_id', results[1])

    def test_mismatched_submission_or_refusal_after_acceptance_cannot_unlock_receipt(self):
        for mismatch in (False, True):
            with self.subTest(mismatch=mismatch):
                def handler(request):
                    identity = request.headers['X-SPARK-Submission-ID']
                    return httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=sse(
                        {'submission': {'client_submission_id': identity, 'operation_id': 'paid-operation',
                                        'upstream_accepted': True, 'submission_pending': True}},
                        {'error': {'client_submission_id': str(uuid.uuid4()) if mismatch else identity,
                                   'code': 'submission_rejected', 'reason': 'unusual_activity',
                                   'upstream_accepted': False, 'submission_pending': False}}))
                result = self.service(handler).generate_videos_batch_google_fx([self.request()])[0]
                self.assertTrue(result['submission_pending'])
                self.assertTrue(result['upstream_accepted'])
                self.assertEqual(result['operation_id'], 'paid-operation')

    def test_terminal_operation_failure_settles_only_explicit_matched_evidence(self):
        for code, include_identity, pending in (
                ('video_generation_failed', True, False),
                ('generation_failed', True, True),
                ('video_generation_failed', False, True)):
            with self.subTest(code=code, include_identity=include_identity):
                def handler(request):
                    error = {'code': code, 'submission_pending': False, 'upstream_accepted': True,
                             'operation_id': 'original-failed-operation'}
                    if include_identity:
                        error['client_submission_id'] = request.headers['X-SPARK-Submission-ID']
                    return httpx.Response(200, headers={'content-type': 'text/event-stream'},
                                          content=sse({'error': error}))
                result = self.service(handler).generate_videos_batch_google_fx([self.request()])[0]
                self.assertEqual(result['submission_pending'], pending)
                self.assertEqual(result['code'], 'upstream_pending' if pending else 'upstream_failed')

    def test_wrong_operation_cannot_settle_original_accepted_submission(self):
        def handler(request):
            identity = request.headers['X-SPARK-Submission-ID']
            return httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=sse(
                {'submission': {'client_submission_id': identity, 'operation_id': 'original-operation',
                                'upstream_accepted': True, 'submission_pending': True}},
                {'error': {'client_submission_id': identity, 'operation_id': 'another-operation',
                           'code': 'video_generation_failed', 'submission_pending': False,
                           'upstream_accepted': True}}))
        result = self.service(handler).generate_videos_batch_google_fx([self.request()])[0]
        self.assertTrue(result['submission_pending'])
        self.assertEqual(result['operation_id'], 'original-operation')

    def test_untrusted_pre_submission_claims_or_prior_video_link_remain_pending(self):
        for error, prior_link in (
            ({'code': 'submission_not_started', 'submission_pending': 'false'}, False),
            ({'code': 'submission_not_started'}, False),
            ({'code': 'native_editor_input_failure', 'submission_pending': False}, False),
            ({'code': 500, 'submission_pending': False, 'message': 'native_editor_input/prompt_input_mismatch'}, False),
            ({'code': 'submission_not_started', 'submission_pending': False}, True),
        ):
            with self.subTest(error=error, prior_link=prior_link):
                calls = []
                def handler(request):
                    calls.append(request)
                    body = sse(*([done()] if prior_link else []), {'error': error})
                    return httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=body)
                result = self.service(handler).generate_videos_batch_google_fx([self.request()])[0]
                self.assertEqual(result['code'], 'upstream_pending')
                self.assertTrue(result['submission_pending'])
                self.assertFalse(result['confirmed'])
                self.assertEqual(len(calls), 1)

    def test_account_refusal_stops_later_posts_and_preserves_success_and_pending(self):
        posted = []
        def handler(request):
            self.calls.append(request)
            if request.method == 'GET':
                return httpx.Response(200, content=MP4)
            prompt = json.loads(request.content)['messages'][0]['content'][0]['text']
            posted.append(prompt)
            if prompt == 'success':
                body = sse(done(), '[DONE]')
            elif prompt == 'pending':
                body = sse({'error': {'code': 401, 'message': 'status poll failed'}})
            else:
                body = sse({'error': {'code': 'no_available_token', 'submission_pending': False}})
            return httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=body)
        results = self.service(handler).generate_videos_batch_google_fx(
            [self.request(prompt=p) for p in ('success', 'pending', 'no account', 'later 1', 'later 2')], self.callback)
        self.assertEqual(posted, ['success', 'pending'] + ['no account'] * 3)
        self.assertEqual(Path(results[0]['video_url']).read_bytes(), MP4)
        self.assertTrue(results[1]['submission_pending'])
        self.assertEqual(results[1]['code'], 'upstream_pending')
        self.assertEqual(results[2]['code'], 'upstream_unavailable')
        for result in results[3:]:
            self.assertEqual(result['code'], 'batch_stopped')
            self.assertEqual(result['stopped_by'], 'upstream_unavailable')
            self.assertFalse(result['submission_pending'])
            self.assertFalse(result['confirmed'])
            self.assertTrue(result['retry_after_recovery'])
            self.assertNotIn('submission_id', result)
        self.assertEqual([index for index, stage, _ in self.events if stage == 'request_submitting'], [0, 1, 2, 2, 2])
        self.assertEqual([index for index, stage, _ in self.events if stage == 'video_error'], [1, 2, 3, 4])

    def test_gateway_refusal_stops_chain_instance_but_not_a_new_manual_task(self):
        for status in (401, 403):
            with self.subTest(status=status):
                calls = []
                refusing = True
                def handler(request):
                    calls.append(request)
                    if refusing:
                        return httpx.Response(status)
                    if request.method == 'POST':
                        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=sse(done(), '[DONE]'))
                    return httpx.Response(200, content=MP4)
                service = self.service(handler)
                results = service.generate_videos_batch_google_fx([self.request(), self.request()])
                self.assertEqual(len(calls), 3)
                expected = 'authorization' if status == 401 else 'access_denied'
                self.assertEqual(results[0]['code'], expected)
                if status == 403:
                    self.assertIn('HTTP 403', results[0]['message'])
                    self.assertNotIn('账号过期', results[0]['message'])
                self.assertFalse(results[0]['submission_pending'])
                self.assertEqual(results[1]['code'], 'batch_stopped')
                self.assertEqual(results[1]['stopped_by'], expected)
                refusing = False
                stopped = service.generate_videos_batch_google_fx([self.request()])
                self.assertEqual(stopped[0]['code'], 'batch_stopped')
                self.assertEqual(len(calls), 3)
                recovered = self.service(handler).generate_videos_batch_google_fx([self.request()])
                self.assertEqual(recovered[0]['status'], 'success')
                self.assertEqual(len([call for call in calls if call.method == 'POST']), 4)

    def test_temporary_limit_or_individual_error_does_not_stop_other_slots(self):
        responses = [
            httpx.Response(429),
            httpx.Response(503),
            httpx.Response(200, headers={'content-type': 'text/event-stream'},
                           content=sse({'error': {'code': 'concurrency_limit', 'submission_pending': False}})),
            httpx.Response(200, headers={'content-type': 'text/event-stream'},
                           content=sse(done(), {'error': {'code': 'no_available_token', 'submission_pending': False}})),
        ]
        for response in responses:
            with self.subTest(status=response.status_code, body=response.content):
                posts = []
                def handler(request):
                    if request.method == 'POST':
                        posts.append(request)
                        if len(posts) == 1:
                            return response
                        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=sse(done(), '[DONE]'))
                    return httpx.Response(200, content=MP4)
                results = self.service(handler).generate_videos_batch_google_fx([self.request(), self.request()])
                settled = response.status_code == 429
                self.assertEqual(len(posts), 3 if settled else 2)
                self.assertEqual(results[0]['status'], 'success' if settled else 'failed')
                self.assertEqual(results[1]['status'], 'success')

    def test_unsupported_or_missing_frames_fail_before_post(self):
        handler = unittest.mock.Mock(side_effect=AssertionError('must not submit'))
        for request in (self.request(end_image='last'), self.request(image_uuid='only-canvas-uuid'),
                        self.request(image='missing.png'), self.request(ratio='1:1'), self.request(duration='12')):
            result = self.service(handler).generate_videos_batch_google_fx([request])[0]
            self.assertEqual(result['status'], 'failed')
        handler.assert_not_called()

    def test_unreadable_frame_diagnostics_do_not_expose_local_details(self):
        handler = unittest.mock.Mock(side_effect=AssertionError('must not submit'))
        with patch('flow2api_video.Path.is_file', side_effect=PermissionError('private/path/' + KEY)):
            result = self.service(handler).generate_videos_batch_google_fx([self.request(image='first.png')], self.callback)[0]
        self.assertEqual(result['code'], 'frame_invalid')
        self.assertNotIn(KEY, repr(result) + repr(self.events))
        self.assertNotIn('private/path', repr(result) + repr(self.events))
        handler.assert_not_called()

    def test_malformed_or_unfinished_sse_never_downloads(self):
        for body in (b'data: {bad}\n\n', sse({'choices': [{'delta': {'content': f"<video src='{MEDIA}'></video>"}}]})):
            calls = []
            def handler(request):
                calls.append(request)
                return httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=body)
            result = self.service(handler).generate_videos_batch_google_fx([self.request()])[0]
            self.assertEqual(result['code'], 'stream_invalid')
            self.assertEqual(len(calls), 1)

    def test_download_redirect_cannot_escape_allowlist_or_receive_api_auth(self):
        def handler(request):
            self.calls.append(request)
            if request.method == 'POST':
                return httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=sse(done(), '[DONE]'))
            self.assertNotIn('authorization', request.headers)
            return httpx.Response(302, headers={'location': 'https://flow-content.google.evil.invalid/stolen'})
        result = self.service(handler).generate_videos_batch_google_fx([self.request()])[0]
        self.assertEqual(result['code'], 'media_url')
        self.assertEqual(len(self.calls), 6)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_media_url_policy_rejects_userinfo_wrong_scheme_and_false_suffix(self):
        service = self.service(self.success_handler)
        for url in ('http://flow-content.google/video/id', 'https://flow-content.google.evil.invalid/video/id',
                    'https://user:pass@flow-content.google/video/id', 'http://127.0.0.1:22/secrets', 'file:///etc/passwd'):
            with self.assertRaises(Flow2APIVideoError):
                service._validate_media_url(url)
        self.assertEqual(service._validate_media_url('/tmp/a.mp4'), 'http://127.0.0.1:38000/tmp/a.mp4')
        self.assertEqual(service._validate_media_url('https://cdn.googleusercontent.com/v?id=private'), 'https://cdn.googleusercontent.com/v?id=private')

    def test_cancel_before_submit_makes_no_request(self):
        handler = unittest.mock.Mock()
        with self.assertRaises(ConnectionError):
            self.service(handler).generate_videos_batch_google_fx([self.request()], cancel_check=lambda: True)
        handler.assert_not_called()

    def test_cancel_sse_stops_batch_and_closes_response(self):
        state = {'cancel': False}
        stream = BlockingStream(sse({'choices': [{'delta': {'reasoning_content': 'not exposed'}}]}), state)
        def handler(request):
            self.calls.append(request)
            return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=stream)
        with self.assertRaises(ConnectionError) as raised:
            self.service(handler).generate_videos_batch_google_fx([self.request(), self.request()], cancel_check=lambda: state['cancel'])
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(stream.closed)
        self.assertTrue(raised.exception.submission_details['submission_pending'])
        self.assertFalse(raised.exception.submission_details['confirmed'])

    def test_cancel_download_removes_partial_file(self):
        state = {'cancel': False}
        stream = BlockingStream(MP4[:32], state)
        def handler(request):
            if request.method == 'POST':
                return httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=sse(done(), '[DONE]'))
            return httpx.Response(200, stream=stream)
        with self.assertRaises(ConnectionError):
            self.service(handler).generate_videos_batch_google_fx([self.request()], cancel_check=lambda: state['cancel'])
        self.assertTrue(stream.closed)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_deadline_cancels_pending_response_without_resubmission(self):
        self.config['flow2apiVideoTimeoutSeconds'] = 0.02
        stream = BlockingStream(b': heartbeat\n\n', {})
        def handler(request):
            self.calls.append(request)
            return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=stream)
        result = self.service(handler).generate_videos_batch_google_fx([self.request()])[0]
        self.assertEqual(result['code'], 'timeout')
        self.assertTrue(result['submission_pending'])
        self.assertTrue(stream.closed)
        self.assertEqual(len(self.calls), 1)

    def test_headers_disconnect_and_network_sse_errors_keep_pending_receipt(self):
        for mode in ('disconnect', 'sse_timeout', 'sse_network'):
            calls = []
            def handler(request):
                calls.append(request)
                if mode == 'disconnect':
                    raise httpx.ReadError('private ' + KEY)
                message = 'upstream timeout' if mode == 'sse_timeout' else 'connection reset'
                return httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=sse({'error': {'message': message}}))
            result = self.service(handler).generate_videos_batch_google_fx([self.request()], self.callback)[0]
            self.assertTrue(result['submission_pending'])
            self.assertFalse(result['confirmed'])
            uuid.UUID(result['submission_id'])
            self.assertEqual(result['prompt_hash'], hashlib.sha256(b'synthetic prompt').hexdigest())
            self.assertEqual(len(calls), 1)
            self.assertNotIn(KEY, repr(result) + repr(self.events))

    def test_only_definitive_rejections_resolve_pending_submission(self):
        for status, finish, pending in ((401, None, False), (403, None, False), (402, None, False),
                                        (429, None, False), (503, None, True), (408, None, True),
                                        (200, 'length', True), (200, 'content_filter', True)):
            def handler(request):
                body = sse({'choices': [{'delta': {}, 'finish_reason': finish}]})
                return httpx.Response(status, headers={'content-type': 'text/event-stream'}, content=body)
            result = self.service(handler).generate_videos_batch_google_fx([self.request()])[0]
            self.assertEqual(result['submission_pending'], pending, (status, finish))
            self.assertFalse(result['confirmed'])

    def test_sse_polling_auth_and_unknown_errors_never_claim_submission_rejected(self):
        errors = [
            {'message': '视频状态查询失败: Flow 登录认证失败... (rpc=jwpduf, code=401)', 'code': 401},
            {'message': 'polling status failed: authorization failed', 'code': 403},
            {'message': 'upstream internal server error', 'code': 500},
            {'message': 'unknown failure'},
            {'message': 'quota exceeded while querying submitted video', 'code': 429},
            {'message': 'UNUSUAL_ACTIVITY during status query'},
            {'message': 'query failed cookie=private https://secret.invalid/?key=' + KEY},
        ]
        for error in errors:
            with self.subTest(code=error.get('code')):
                self.calls.clear()
                self.events.clear()
                def handler(request):
                    self.calls.append(request)
                    return httpx.Response(200, headers={'content-type': 'text/event-stream'},
                                          content=sse({'error': error}, '[DONE]'))
                result = self.service(handler).generate_videos_batch_google_fx([self.request()], self.callback)[0]
                self.assertEqual(result['code'], 'upstream_pending')
                self.assertEqual(result['message'], 'Flow2API 视频结果待确认，已保留原提交收据，请核对原任务')
                self.assertTrue(result['submission_pending'])
                self.assertFalse(result['confirmed'])
                self.assertEqual(result['submission_id'], self.calls[-1].headers['X-SPARK-Submission-ID'])
                self.assertEqual(self.events[-1][1], 'video_error')
                self.assertTrue(self.events[-1][2]['submission_pending'])
                self.assertNotIn('request_resolved', [stage for _, stage, _ in self.events])
                self.assertEqual([request.method for request in self.calls], ['POST'])
                self.assertNotIn(KEY, repr(result) + repr(self.events))
                self.assertNotIn('secret.invalid', repr(result) + repr(self.events))

    def test_callback_cancellation_preserves_subclass_stops_batch_and_cleans_output(self):
        class GenerationCancelled(ConnectionError):
            pass
        for stage in ('request_submitted', 'video_done'):
            self.calls.clear()
            cancellation = GenerationCancelled('cancel')
            def callback(index, event, details):
                if event == stage:
                    raise cancellation
            with self.assertRaises(GenerationCancelled) as raised:
                self.service(self.success_handler).generate_videos_batch_google_fx([self.request(), self.request()], callback)
            self.assertIs(raised.exception, cancellation)
            self.assertEqual(len([r for r in self.calls if r.method == 'POST']), 1)
            self.assertEqual(list(self.directory.iterdir()), [])
            self.assertEqual(cancellation.submission_details['submission_pending'], stage == 'request_submitted')

    def test_submission_intent_is_persisted_before_post_and_callback_failure_prevents_send(self):
        handler = unittest.mock.Mock(side_effect=AssertionError('must not submit'))
        for callback_error in (ConnectionError('cancel'), RuntimeError('receipt unavailable')):
            events = []
            def callback(index, stage, details):
                events.append((stage, details))
                if stage == 'request_submitting':
                    handler.assert_not_called()
                    self.assertTrue(details['submission_pending'])
                    self.assertFalse(details['confirmed'])
                    self.assertFalse(details['upstream_accepted'])
                    uuid.UUID(details['submission_id'])
                    raise callback_error
            if isinstance(callback_error, ConnectionError):
                with self.assertRaises(ConnectionError):
                    self.service(handler).generate_videos_batch_google_fx([self.request()], callback)
            else:
                with self.assertRaises(Flow2APIVideoError) as raised:
                    self.service(handler).generate_videos_batch_google_fx([self.request()], callback)
                self.assertEqual(raised.exception.code, 'callback')
            self.assertNotIn('request_submitted', [stage for stage, _ in events])
            self.assertNotIn(KEY, repr(events))
        handler.assert_not_called()

    def test_journal_callback_failure_aborts_whole_batch_before_any_post(self):
        def callback(index, stage, details):
            self.events.append((index, stage, details))
            if stage == 'request_submitting':
                raise RuntimeError('private disk failure ' + KEY)
        with self.assertRaises(Flow2APIVideoError) as raised:
            self.service(self.success_handler).generate_videos_batch_google_fx([self.request(), self.request()], callback)
        self.assertEqual(raised.exception.code, 'callback')
        self.assertNotIn(KEY, str(raised.exception))
        self.assertEqual(self.calls, [])
        self.assertTrue(all(index == 0 for index, _, _ in self.events))
        self.assertNotIn('video_error', [stage for _, stage, _ in self.events])

    def test_accepted_callback_failure_aborts_whole_batch_after_only_first_post(self):
        def callback(index, stage, details):
            self.events.append((index, stage, details))
            if stage == 'request_submitted':
                raise RuntimeError('checkpoint failed')
        with self.assertRaises(Flow2APIVideoError) as raised:
            self.service(self.success_handler).generate_videos_batch_google_fx([self.request(), self.request()], callback)
        self.assertEqual(raised.exception.code, 'callback')
        self.assertEqual([request.method for request in self.calls], ['POST'])
        self.assertTrue(all(index == 0 for index, _, _ in self.events))
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_delivery_callback_failure_removes_download_and_aborts_whole_batch(self):
        def callback(index, stage, details):
            self.events.append((index, stage, details))
            if stage == 'video_done':
                raise RuntimeError('checkpoint failed')
        with self.assertRaises(Flow2APIVideoError):
            self.service(self.success_handler).generate_videos_batch_google_fx([self.request(), self.request()], callback)
        self.assertEqual(len([request for request in self.calls if request.method == 'POST']), 1)
        self.assertTrue(all(index == 0 for index, _, _ in self.events))
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_resolved_receipt_precedes_download_and_cancellation_makes_no_get(self):
        def callback(index, stage, details):
            self.events.append((index, stage, details))
            if stage == 'request_resolved':
                self.assertTrue(details['confirmed'])
                self.assertFalse(details['submission_pending'])
                self.assertTrue(details['upstream_accepted'])
                self.assertEqual([request.method for request in self.calls], ['POST'])
                raise ConnectionError('cancel after resolved receipt persisted')
        with self.assertRaises(ConnectionError) as raised:
            self.service(self.success_handler).generate_videos_batch_google_fx([self.request()], callback)
        self.assertFalse(raised.exception.submission_details['submission_pending'])
        self.assertEqual([request.method for request in self.calls], ['POST'])
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_sse_client_has_no_short_read_timeout_inside_total_deadline(self):
        service = self.service(self.success_handler)
        factory = service._client_factory
        configurations = []
        def client_factory(**kwargs):
            configurations.append(kwargs)
            return factory(**kwargs)
        service._client_factory = client_factory
        result = service.generate_videos_batch_google_fx([self.request()])[0]
        self.assertEqual(result['status'], 'success')
        self.assertIsNone(configurations[0]['timeout'].read)
        self.assertEqual(configurations[0]['timeout'].connect, 10)
        self.assertEqual(configurations[1]['timeout'].read, 60)

    def test_decode_failure_and_html_download_are_never_published(self):
        service = self.service(self.success_handler)
        service._validate_video = AsyncMock(side_effect=Flow2APIVideoError('video_invalid'))
        result = service.generate_videos_batch_google_fx([self.request()])[0]
        self.assertEqual(result['code'], 'video_invalid')
        self.assertEqual(list(self.directory.iterdir()), [])
        service = self.service(self.success_handler, validate=False)
        path = self.directory / 'error.part.mp4'
        path.write_bytes(b'<html>upstream failed</html>' * 5)
        service._media_command = AsyncMock()
        with self.assertRaises(Flow2APIVideoError):
            asyncio.run(service._validate_video(path, 10, (640, 360)))
        service._media_command.assert_not_awaited()

    def test_probe_checks_duration_dimensions_then_decodes_audio_and_video(self):
        service = self.service(self.success_handler, validate=False)
        path = self.directory / 'candidate.mp4'
        path.write_bytes(MP4)
        probe = {'streams': [{'codec_type': 'video', 'width': 640, 'height': 360, 'duration': '10'}, {'codec_type': 'audio'}]}
        service._media_command = AsyncMock(side_effect=[json.dumps(probe).encode(), b''])
        result = asyncio.run(service._validate_video(path, 10, (640, 360)))
        self.assertTrue(result['has_audio'])
        decode = service._media_command.await_args_list[-1].args[0]
        self.assertIn('0:v:0', decode)
        self.assertIn('0:a?', decode)
        self.assertIn('-xerror', decode)
        probe['streams'][0]['width'] = 0
        service._media_command = AsyncMock(return_value=json.dumps(probe).encode())
        with self.assertRaises(Flow2APIVideoError):
            asyncio.run(service._validate_video(path, 10, (640, 360)))

    def test_requested_orientation_resolution_and_pixel_tolerance_are_validated(self):
        service = self.service(self.success_handler, validate=False)
        path = self.directory / 'candidate.mp4'
        path.write_bytes(MP4)
        for ratio, resolution, expected in (('16:9', '360p', (640, 360)), ('9:16', '360p', (360, 640)),
                                             ('16:9', '720p', (1280, 720)), ('9:16', '720p', (720, 1280))):
            self.assertEqual(service._prepare(self.request(ratio=ratio, resolution=resolution))['expected_size'], expected)
        for actual, expected, valid in (((640, 360), (640, 360), True), ((638, 362), (640, 360), True),
                                       ((360, 640), (640, 360), False), ((640, 360), (1280, 720), False),
                                       ((800, 360), (640, 360), False), ((640, 356), (640, 360), False)):
            probe = {'streams': [{'codec_type': 'video', 'width': actual[0], 'height': actual[1], 'duration': '10'}]}
            service._media_command = AsyncMock(side_effect=[json.dumps(probe).encode(), b''])
            if valid:
                asyncio.run(service._validate_video(path, 10, expected))
                self.assertEqual(service._media_command.await_count, 2)
            else:
                with self.assertRaises(Flow2APIVideoError):
                    asyncio.run(service._validate_video(path, 10, expected))
                self.assertEqual(service._media_command.await_count, 1)

    def test_project_rejection_directly_retries_then_cleans_all_rejected_files(self):
        service = self.service(self.success_handler)
        result = service.generate_videos_batch_google_fx([self.request()], lambda index, stage, data: 'rejected' if stage == 'video_done' else None)[0]
        self.assertEqual(result['code'], 'rejected')
        self.assertEqual(len([r for r in self.calls if r.method == 'POST']), 3)
        self.assertEqual(list(self.directory.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
