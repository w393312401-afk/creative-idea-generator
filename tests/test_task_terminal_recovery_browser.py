"""Restore cached terminal tasks without generation, even when SSE stays open."""
import json
from urllib.parse import parse_qs, urlparse

import pytest

pytest.importorskip('playwright.sync_api')
from playwright.sync_api import sync_playwright

from test_refresh_generation_progress import GenerationApi, IDEA, PARENT_ID, CHILD_ID, _seed_browser
from test_slot_grid_render import static_server


class TerminalApi(GenerationApi):
    def __init__(self, page, terminal_on_start, child_status):
        super().__init__(page)
        self.terminal = terminal_on_start
        self.child_status = child_status

    def tasks(self):
        return []

    def route(self, route):
        parsed = urlparse(route.request.url)
        if parsed.path != '/api/compose-status':
            return super().route(route)
        query = parse_qs(parsed.query)
        self.requests.append((route.request.method, parsed.path, query))
        task_id = query.get('task_id', [''])[0]
        if not self.terminal:
            data = {'status': 'running'}
        elif task_id == PARENT_ID:
            data = {'status': 'failed', 'error': '保存自动视频清单失败'}
        else:
            manifest = self.manifest()
            manifest['auto_video'].update(
                status='completed' if self.child_status == 'completed' else 'blocked',
                message='后台视频任务已结束')
            data = {'status': self.child_status, 'result': manifest,
                    'auto_video': manifest['auto_video'], 'error': '后台视频任务已停止'}
        route.fulfill(status=200, content_type='application/json', body=json.dumps(data))


@pytest.mark.parametrize('terminal_on_start,child_status', [(True, 'failed'), (False, 'completed')],
                         ids=['refresh-already-failed', 'heartbeat-task-becomes-terminal'])
def test_cached_terminal_tasks_clear_busy_and_restore_actual_media(terminal_on_start, child_status):
    with static_server() as base, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={'width': 1440, 'height': 1100})
        page_errors = []
        page.on('pageerror', lambda error: page_errors.append(str(error)))
        api = TerminalApi(page, terminal_on_start, child_status)
        _seed_browser(page, True)
        page.add_init_script('''
            // Hold the HTTP body open: no terminal event and no EOF will arrive.
            const terminalRecoveryFetch = window.fetch.bind(window);
            window.heldTaskStreams = [];
            window.cancelledTaskStreams = [];
            window.fetch = async (...args) => {
                const url = String(args[0]);
                if (!url.includes('/api/compose-stream')) return terminalRecoveryFetch(...args);
                const taskId = new URL(url, location.href).searchParams.get('task_id');
                window.heldTaskStreams.push(taskId);
                return new Response(new ReadableStream({
                    start(controller) { controller.enqueue(new TextEncoder().encode(': keepalive\\n\\n')); },
                    cancel() { window.cancelledTaskStreams.push(taskId); }
                }), {headers: {'Content-Type': 'text/event-stream'}});
            };
            document.addEventListener('DOMContentLoaded', () => {
                const originalWatch = window.watchTaskUntilTerminal;
                window.watchTaskUntilTerminal = (id, options) => originalWatch(id,
                    {...options, statusPollIntervalMs: 30});
            }, {once: true});
        ''')
        page.goto(base + '/index.html', wait_until='load')
        if not terminal_on_start:
            page.wait_for_function('''id => getIdeaTaskRecord(id, 'frames')
                && getIdeaTaskRecord(id, 'videos') && window.heldTaskStreams.length >= 2''', arg=IDEA['id'])
            assert page.locator('#generate-frames-btn').is_disabled()
            assert page.locator('#generate-videos-btn').is_disabled()
            api.terminal = True
        expected_auto_status = 'completed' if child_status == 'completed' else 'blocked'
        page.wait_for_function('''expected => {
            const run = currentIdea && currentIdea.frameRun;
                return run && !getIdeaTaskRecord(expected.id, 'frames') && !getIdeaTaskRecord(expected.id, 'videos')
                && run.frames.length === 2 && run.videos.length === 1;
        }''', arg={'id': IDEA['id'], 'autoStatus': expected_auto_status}, timeout=10000)
        assert page.evaluate('() => currentIdea.frameRun.auto_video.status') == expected_auto_status
        assert not page.locator('#generate-frames-btn').is_disabled()
        assert not page.locator('#generate-frames-selection-btn').is_disabled()
        assert not page.locator('#generate-videos-btn').is_disabled()
        assert not page.locator('#frames-progress').is_visible()
        assert not page.locator('#videos-progress').is_visible()
        assert '生成中' not in page.locator('#pipeline-next-btn').inner_text()
        assert page.locator('#frame-slot-1').get_attribute('data-kind') == 'ready'
        assert page.locator('#video-slot-1').get_attribute('data-kind') == 'ready'
        assert page.evaluate('''() => JSON.parse(localStorage.getItem('spark_active_background_tasks')).tasks''') == []
        assert set(page.evaluate('() => window.cancelledTaskStreams')) == {PARENT_ID, CHILD_ID}
        assert any(endpoint == '/api/get_manifest' for _, endpoint, _ in api.requests)
        assert not api.generation_posts
        assert not any(endpoint == '/api/compose-cancel' for _, endpoint, _ in api.requests)
        assert not page_errors, 'Serialized feed timestamps must not throw before watchers start'
        browser.close()
