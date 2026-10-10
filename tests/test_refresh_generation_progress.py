"""Actual page reload recovery with mocked HTTP task/manifest/SSE responses."""
import copy
import json
from urllib.parse import parse_qs, urlparse

import pytest

pytest.importorskip('playwright.sync_api')
from playwright.sync_api import sync_playwright

from test_slot_grid_render import static_server


PROMPT = ('图片 1:\nempty room\n图片 2:\nframing\n图片 3:\nfinishing\n图片 4:\nfinished room\n'
          '视频 1 (图片 1 → 图片 2):\nBuild the walls.\n'
          '视频 2 (图片 2 → 图片 3):\nFinish the walls.\n'
          '视频 3 (图片 3 → 图片 4):\nDecorate the room.\n')
IDEA = {
    'id': 'refresh-generation-owner', 'project_key': 'refresh-generation-project',
    'title': 'Refresh ongoing image and video generation', 'prompt_block': PROMPT,
    'prompt_slots': {
        'images': [{'index': index, 'body': 'image'} for index in range(1, 5)],
        'videos': [{'index': index, 'body': 'video'} for index in range(1, 4)],
    },
    'frameRun': {'frames': [], 'videos': []},
}
PARENT_ID, CHILD_ID = 'frames-refresh-parent', 'videos-refresh-child'
PNG = bytes.fromhex(
    '89504e470d0a1a0a0000000d494844520000000100000001080600000'
    '01f15c4890000000a49444154789c6300010000050001'
    '0d0a2db40000000049454e44ae426082')


def _frame(index):
    url = f'/outputs/refresh-generation-project/frames/img_{index:03d}.webp'
    return {'sequence': index, 'slot': index, 'file': url.lstrip('/'), 'url': url,
            'quality_gate': 'pending_manual_review'}


def _video(index):
    url = f'/outputs/refresh-generation-project/videos/vid_{index:03d}.mp4'
    return {'slot': index, 'sequence': index, 'status': 'success',
            'file': url.lstrip('/'), 'url': url,
            'start_anchor_slot': index, 'end_anchor_slot': index + 1}


def _handoff(done):
    return {'status': 'started', 'task_id': CHILD_ID, 'request_id': 'auto_video:' + PARENT_ID,
            'target_slots': [1, 2, 3], 'ready_slots': list(range(1, done + 1)),
            'queued_slots': list(range(1, done + 1)), 'pending_slots': list(range(done + 1, 4)),
            'frame_pairs': [{'slot': index, 'start_anchor_slot': index, 'end_anchor_slot': index + 1}
                            for index in range(1, 4)]}


class GenerationApi:
    """The backend continues independently while the page is replaced."""
    def __init__(self, page):
        self.frame_count, self.video_count = 2, 1
        self.requests, self.generation_posts, self.stream_requests = [], [], []
        page.route('**/api/**', self.route)
        page.route('**/outputs/**', lambda route: route.fulfill(
            status=200, content_type='image/png', body=PNG))

    def manifest(self):
        return {'title': IDEA['title'], 'project_dir': 'outputs/' + IDEA['project_key'],
                'prompt_block': PROMPT, 'prompt_slots': copy.deepcopy(IDEA['prompt_slots']),
                'frames': [_frame(index) for index in range(1, self.frame_count + 1)],
                'videos': [_video(index) for index in range(1, self.video_count + 1)],
                'auto_generate_videos': True, 'video_frame_pairing': 'auto',
                'auto_video': _handoff(self.video_count)}

    def events(self, task_id):
        if task_id == PARENT_ID:
            events = [('start', {'total': 4})]
            events += [('frame', {'frame': _frame(index), 'current': index, 'total': 4})
                       for index in range(1, 3)]
            events.append(('auto_video_started', _handoff(1)))
            for index in range(3, self.frame_count + 1):
                events.append(('frame', {'frame': _frame(index), 'current': index, 'total': 4}))
                events.append(('auto_video_updated', _handoff(index - 1)))
            return events
        return [('start', {'total': 3, 'slots': [1, 2, 3]})] + [
            ('video_done', {'index': index, 'current': index, 'total': 3, 'video': _video(index)})
            for index in range(1, self.video_count + 1)]

    def tasks(self):
        return [{'id': task_id, 'status': 'running', 'error': None, 'result': None,
                 'last_active': 1000, 'events': self.events(task_id),
                 'dimensions': {'type': kind, 'project_key': IDEA['project_key'],
                                'theme': IDEA['title'], 'auto_generate_videos': True,
                                **({'progressive': True, 'parent_frame_task_id': PARENT_ID,
                                    'target_slots': [1, 2, 3], 'request_id': 'auto_video:' + PARENT_ID}
                                   if kind == 'videos' else {})}}
                for task_id, kind in ((PARENT_ID, 'frames'), (CHILD_ID, 'videos'))]

    def route(self, route):
        request = route.request
        parsed = urlparse(request.url)
        endpoint, query = parsed.path, parse_qs(parsed.query)
        self.requests.append((request.method, endpoint, query))
        data = {}
        if endpoint == '/api/mode':
            data = {'video_config': {'videoProvider': 'flow2api'}}
        elif endpoint == '/api/library':
            data = [copy.deepcopy(IDEA)]
        elif endpoint == '/api/library/item':
            # The library remains an old snapshot; physical results live in manifest.
            data = {'status': 'success'} if request.method == 'POST' else copy.deepcopy(IDEA)
        elif endpoint == '/api/get_manifest':
            assert query.get('title') == [IDEA['project_key']]
            data = self.manifest()
        elif endpoint == '/api/tasks':
            data = {'tasks': self.tasks(), 'total_count': 2}
        elif endpoint == '/api/projects':
            tasks = self.tasks()
            data = {'projects': [{'project_key': IDEA['project_key'], 'title': IDEA['title'],
                                  'kind': 'project', 'state': 'running', 'saved': True,
                                  'library': {'id': IDEA['id']}, 'task': tasks[0] if tasks else {},
                                  'sub_jobs': tasks, 'assets': {'dir': 'outputs/' + IDEA['project_key']}}]}
        elif endpoint == '/api/compose-status':
            data = {'status': 'running', 'result': self.manifest(),
                    'dimensions': next((task['dimensions'] for task in self.tasks()
                                        if task['id'] == query.get('task_id', [''])[0]), {})}
        elif endpoint == '/api/compose-stream':
            task_id = query.get('task_id', [''])[0]
            self.stream_requests.append(task_id)
            body = ''.join(f'id: {index}\ndata: {json.dumps({"type": kind, "data": value})}\n\n'
                           for index, (kind, value) in enumerate(self.events(task_id), 1))
            route.fulfill(status=200, content_type='text/event-stream', body=body)
            return
        elif endpoint.startswith('/api/generate_'):
            self.generation_posts.append((endpoint, request.post_data_json))
            data = {'task_id': 'unexpected-new-generation'}
        elif endpoint == '/api/ping':
            data = {'online': True}
        route.fulfill(status=200, content_type='application/json', body=json.dumps(data))


def _seed_browser(page, cached_tasks):
    tasks = ([{'ideaId': IDEA['id'], 'type': 'frames', 'taskId': PARENT_ID},
              {'ideaId': IDEA['id'], 'type': 'videos', 'taskId': CHILD_ID,
               'targetSlots': [1, 2, 3], 'requestId': 'auto_video:' + PARENT_ID}]
             if cached_tasks else [])
    if tasks:
        # Real task persistence JSON-serializes Date objects into ISO strings.
        tasks[0].update(meta='正在生成帧序列: 2/4...', current=2, total=4, feedLines=[
            {'text': '缓存中的已交付图片', 'cls': 'ok', 'time': '2026-10-05T01:02:03.000Z'},
            {'text': '毫秒时间戳', 'time': 1791162123000},
            {'text': '秒时间戳', 'time': 1791162123},
            {'text': '数字字符串时间戳', 'time': '1791162123000'},
            {'text': '旧缓存无效时间', 'time': 'invalid-time'},
        ])
    page.add_init_script('''const seed = %s;
        localStorage.setItem('spark_current_idea', JSON.stringify(seed.idea));
        localStorage.setItem('spark_current_idea_id', seed.idea.id);
        localStorage.setItem('spark_active_tab', 'overview');
        localStorage.removeItem('spark_active_task_id');
        if (seed.tasks.length) localStorage.setItem('spark_active_background_tasks', JSON.stringify({tasks: seed.tasks}));
        else localStorage.removeItem('spark_active_background_tasks');
        // Delay successful identity/library reads to expose startup recovery races.
        const realFetch = window.fetch.bind(window);
        window.fetch = async (...args) => {
            const response = await realFetch(...args);
            const url = String(args[0]);
            if (url.includes('/api/library') && (!args[1] || !args[1].method || args[1].method === 'GET'))
                await new Promise(resolve => setTimeout(resolve, 250));
            return response;
        };
    ''' % json.dumps({'idea': IDEA, 'tasks': tasks}))


def _assert_progress(page, frame_count, video_count):
    page.wait_for_function('''counts => {
        if (!currentIdea || currentIdea.id !== counts.id) return false;
        const run = currentIdea.frameRun || {};
        const frames = getIdeaTaskRecord(counts.id, 'frames');
        const videos = getIdeaTaskRecord(counts.id, 'videos');
        return (run.frames || []).length === counts.frames && (run.videos || []).length === counts.videos
            && frames && frames.taskId === counts.parent && videos && videos.taskId === counts.child
            && frames.progressInfo && frames.progressInfo.percent > 0
            && videos.progressInfo && videos.progressInfo.percent > 0
            && document.getElementById('frames-progress-label').textContent.includes(`${counts.frames}/4`)
            && document.getElementById('videos-progress-label').textContent.includes(`${counts.videos}/3`);
    }''', arg={'id': IDEA['id'], 'frames': frame_count, 'videos': video_count,
              'parent': PARENT_ID, 'child': CHILD_ID}, timeout=12000)
    assert page.locator('#main-tab-results').evaluate("element => element.classList.contains('active')")
    assert page.locator('#frames-progress').is_visible()
    assert page.locator('#videos-progress').is_visible()
    assert page.locator('#frames-progress-label').inner_text().find(f'{frame_count}/4') >= 0
    assert page.locator('#videos-progress-label').inner_text().find(f'{video_count}/3') >= 0
    assert page.locator('#frames-auto-video-status').is_visible()
    assert f'已调度 {video_count}/3 段' in page.locator('#frames-auto-video-status').inner_text()
    assert page.locator('#generate-frames-btn').is_disabled()
    assert page.locator('#generate-videos-btn').is_disabled()
    for index in range(1, frame_count + 1):
        assert page.locator(f'#frame-slot-{index}').get_attribute('data-kind') == 'ready'
    for index in range(1, video_count + 1):
        assert page.locator(f'#video-slot-{index}').get_attribute('data-kind') == 'ready'


@pytest.mark.parametrize('cached_tasks', [True, False], ids=['cached-registry', 'discover-from-server'])
def test_refresh_restores_project_manifest_and_both_live_tasks_without_new_generation(cached_tasks):
    with static_server() as base, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={'width': 1440, 'height': 1100})
        api = GenerationApi(page)
        _seed_browser(page, cached_tasks)
        page.goto(base + '/index.html', wait_until='load')
        _assert_progress(page, 2, 1)
        streams_before_refresh = len(api.stream_requests)
        page.reload(wait_until='load')
        _assert_progress(page, 2, 1)
        assert len(api.stream_requests) > streams_before_refresh
        assert PARENT_ID in api.stream_requests and CHILD_ID in api.stream_requests
        assert sum(endpoint == '/api/get_manifest' for _, endpoint, _ in api.requests) >= 2
        if not cached_tasks:
            assert any(endpoint == '/api/tasks' for _, endpoint, _ in api.requests)
        api.frame_count, api.video_count = 3, 2
        _assert_progress(page, 3, 2)
        assert not api.generation_posts, 'Restoring watchers must never submit a fresh generation request'
        browser.close()


def test_late_manifest_read_preserves_deliveries_when_task_started_and_completed_during_read():
    with static_server() as base, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={'width': 1440, 'height': 1100})
        api = GenerationApi(page)
        api.tasks = lambda: []
        _seed_browser(page, False)
        page.goto(base + '/index.html', wait_until='load')
        page.wait_for_function("() => currentIdea && currentIdea.frameRun && currentIdea.frameRun.frames.length === 2")
        page.evaluate('''() => {
            const realFetch = window.fetch.bind(window);
            let holdNextManifest = true;
            const gate = new Promise(resolve => { window.releaseLateManifest = resolve; });
            window.fetch = async (...args) => {
                const response = await realFetch(...args);
                if (holdNextManifest && String(args[0]).includes('/api/get_manifest')) {
                    holdNextManifest = false;
                    window.lateManifestReceived = true;
                    await gate;
                }
                return response;
            };
            window.lateManifestRead = reloadManifestIntoIdea(currentIdea);
        }''')
        page.wait_for_function('() => window.lateManifestReceived === true')
        api.frame_count, api.video_count = 3, 2
        page.evaluate('''delivery => {
            // Discovery and terminal events can both happen while an earlier GET is in flight.
            beginIdeaTask(currentIdea.id, 'frames', 'finishing-parent', new AbortController());
            beginIdeaTask(currentIdea.id, 'videos', 'finishing-child', new AbortController());
            applyFrameEventToIdea(delivery.frame, currentIdea);
            renderVideoSlotDone(delivery.video.slot, delivery.video, currentIdea);
            currentIdea.frameRun.auto_video = delivery.handoff;
            currentIdea.prompt_block = delivery.prompt_block;
            currentIdea.prompt_slots = delivery.prompt_slots;
            currentIdea.frameRun.prompt_block = delivery.prompt_block;
            currentIdea.frameRun.prompt_slots = delivery.prompt_slots;
            currentIdea.frameRun.merged_video = delivery.merged_video;
            currentIdea.frameRun.video_generation_stats = delivery.video_generation_stats;
            endIdeaTask(currentIdea.id, 'frames');
            endIdeaTask(currentIdea.id, 'videos');
            window.releaseLateManifest();
        }''', {'frame': _frame(3), 'video': _video(2), 'handoff': {**_handoff(2), 'status': 'completed'},
               'prompt_block': PROMPT.replace('Build the walls.', 'Optimized accepted construction motion.'),
               'prompt_slots': {**IDEA['prompt_slots'], 'videos': [
                   {'index': index, 'body': 'optimized accepted motion'} for index in range(1, 4)]},
               'merged_video': {'status': 'success', 'url': '/outputs/refresh-generation-project/final.mp4'},
               'video_generation_stats': {'last_run': {'attempt_id': 'just-completed', 'accepted_results': 2}}})
        page.evaluate('() => window.lateManifestRead')
        current = page.evaluate('() => currentIdea.frameRun')
        assert [frame['sequence'] for frame in current['frames']] == [1, 2, 3]
        assert [video['slot'] for video in current['videos']] == [1, 2]
        assert current['auto_video']['status'] == 'completed'
        assert 'Optimized accepted construction motion.' in page.evaluate('() => currentIdea.prompt_block')
        assert 'Optimized accepted construction motion.' in current['prompt_block']
        assert current['prompt_slots']['videos'][0]['body'] == 'optimized accepted motion'
        assert current['merged_video']['url'].endswith('/final.mp4')
        assert current['video_generation_stats']['last_run']['attempt_id'] == 'just-completed'
        assert not api.generation_posts
        browser.close()
