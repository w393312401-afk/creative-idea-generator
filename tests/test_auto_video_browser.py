"""Automatic image-to-video UI smoke tests; all API requests are mocked."""
import copy
import json
from urllib.parse import parse_qs, urlparse

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright

from test_slot_grid_render import static_server


IDEA = {
    "id": "auto-video-ui", "title": "Automatic video smoke", "prompt_block": "smoke prompts",
    "prompt_slots": {
        "images": [{"index": index, "body": "image"} for index in (2, 5, 8)],
        "videos": [{"index": index, "body": "video"} for index in (4, 9)],
    },
}


def _mock_api(page):
    ideas = {IDEA["id"]: copy.deepcopy(IDEA)}
    posts = []
    video_config = {"videoRetryCount": 5}

    def route_api(route):
        request = route.request
        parsed = urlparse(request.url)
        endpoint = parsed.path
        data = {}
        if endpoint == "/api/mode":
            data = {"video_config": video_config}
        elif endpoint == "/api/google-fx/config":
            if request.method == "POST":
                video_config.update(request.post_data_json["patch"])
                data = {"status": "ok"}
            else:
                data = {"config": video_config}
        elif endpoint == "/api/library":
            data = list(ideas.values())
        elif endpoint == "/api/library/item":
            if request.method == "POST":
                item = request.post_data_json["item"]
                ideas[item["id"]] = item
                data = {"status": "success"}
            else:
                data = ideas.get(parse_qs(parsed.query).get("id", [""])[0], {})
        elif endpoint == "/api/get_manifest":
            data = {"frames": [], "videos": []}
        elif endpoint in ("/api/generate_frames", "/api/generate_frames_selection"):
            posts.append((endpoint, request.post_data_json))
            data = {"task_id": "mock-parent-%s" % len(posts)}
        elif endpoint == "/api/tasks":
            data = {"tasks": []}
        elif endpoint == "/api/projects":
            data = {"projects": []}
        elif endpoint == "/api/ping":
            data = {"online": True}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(data))

    page.route("**/api/**", route_api)
    return posts


def _open_idea(page, idea):
    page.evaluate("""idea => {
        currentIdea = idea; savedIdeas = [idea];
        switchMainTab('results');
        document.getElementById('output-placeholder-view').classList.remove('active');
        document.getElementById('output-content-view').classList.add('active');
        switchTab('overview'); hydrateFramesPanel(idea);
    }""", idea)


def test_auto_video_toggle_visibility_persistence_and_task_lock():
    with static_server() as base, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        _mock_api(page)
        page.goto(base + "/index.html", wait_until="load")
        page.wait_for_function("() => typeof initAutoVideoControl === 'function'")
        _open_idea(page, IDEA)
        toggle = page.locator("#frames-auto-video-toggle")
        assert toggle.is_visible()
        assert toggle.is_checked(), "New ideas enable automatic video generation by default"
        assert not toggle.is_disabled()
        assert "无需等待全部图片" in page.locator(".frames-auto-video-toggle").get_attribute("title")
        assert page.locator("#frames-settings-pop").get_attribute("hidden") is not None
        toggle.uncheck()
        assert page.evaluate("() => currentIdea.auto_generate_videos") is False

        other = {**IDEA, "id": "other-auto-video-ui"}
        _open_idea(page, other)
        assert toggle.is_checked(), "Another idea must keep its own enabled default"
        legacy_off = {**IDEA, "id": "legacy-default-off", "auto_generate_videos": False,
                      "frameRun": {"auto_generate_videos": False}}
        _open_idea(page, legacy_off)
        assert toggle.is_checked(), "The previous implicit disabled default migrates to enabled"
        explicit_off = {**legacy_off, "id": "explicit-default-off",
                        "auto_generate_videos_preference_explicit": True}
        _open_idea(page, explicit_off)
        assert not toggle.is_checked(), "An explicit server-side opt-out remains disabled"
        _open_idea(page, IDEA)
        assert not toggle.is_checked(), "An explicit opt-out survives a fresh idea object"
        toggle.check()
        page.evaluate("() => saveCurrentIdeaState()")

        page.reload(wait_until="load")
        page.wait_for_function("() => currentIdea && currentIdea.id === 'auto-video-ui' && document.getElementById('frames-auto-video-toggle').checked")
        assert toggle.is_checked()
        assert not toggle.is_disabled()

        page.evaluate("""() => {
            beginIdeaTask(currentIdea.id, 'frames', 'mock-running', new AbortController());
            hydrateFramesPanel(currentIdea);
        }""")
        assert toggle.is_disabled(), "The active frame request has already captured the preference"
        _open_idea(page, other)
        assert not toggle.is_disabled(), "A different idea is not locked by the owner's task"
        assert toggle.is_checked()
        _open_idea(page, IDEA)
        assert toggle.is_disabled()
        assert toggle.is_checked()
        page.evaluate("() => { endIdeaTask(currentIdea.id, 'frames'); hydrateFramesPanel(currentIdea); }")
        assert not toggle.is_disabled()
        browser.close()


def test_video_retry_setting_defaults_zero_persistence_and_request_snapshot():
    with static_server() as base, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        posts = _mock_api(page)
        page.goto(base + "/index.html", wait_until="load")
        page.wait_for_function("() => typeof autoSaveConfig === 'function'")
        page.locator("#open-settings-btn").click()
        page.evaluate("() => switchSettingsSection('backend')")
        retries = page.locator("#settings-video-retry-count")
        retries.wait_for(state="visible")
        assert retries.is_visible()
        assert retries.input_value() == "5"
        with page.expect_request("**/api/google-fx/config") as request:
            retries.select_option("0")
        assert request.value.post_data_json["patch"]["videoRetryCount"] == 0
        page.wait_for_function("() => config.videoRetryCount === 0 && document.getElementById('settings-saved-flag').dataset.state === 'saved'")
        assert page.evaluate("() => JSON.parse(localStorage.getItem('spark_config')).videoRetryCount") == 0
        page.reload(wait_until="load")
        page.wait_for_function("() => config.videoRetryCount === 0")
        page.locator("#open-settings-btn").click()
        page.evaluate("() => switchSettingsSection('backend')")
        retries.wait_for(state="visible")
        assert retries.input_value() == "0", "Zero must survive server readback and reload"
        with page.expect_request("**/api/google-fx/config") as request:
            retries.select_option("5")
        assert request.value.post_data_json["patch"]["videoRetryCount"] == 5
        page.wait_for_function("() => document.getElementById('settings-saved-flag').dataset.state === 'saved'")
        page.evaluate("() => document.getElementById('settings-modal').classList.remove('active')")
        _open_idea(page, IDEA)
        page.evaluate("""() => {
            document.getElementById('frames-skip-cover-toggle').checked = true;
            markCandidateSelectionMode(false);
            ensureFreshPromptBlock = async () => true;
            watchTaskUntilTerminal = () => new Promise(() => {});
        }""")
        page.evaluate("() => generateFrames()")
        assert posts[0][1]["config"]["videoRetryCount"] == 5
        browser.close()


def test_both_frame_actions_send_auto_video_option_and_video_debug_targets():
    with static_server() as base, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        posts = _mock_api(page)
        page.goto(base + "/index.html", wait_until="load")
        page.wait_for_function("() => typeof generateFramesSelection === 'function'")
        _open_idea(page, IDEA)
        page.locator("#frames-auto-video-toggle").check()
        page.evaluate("""() => {
            document.getElementById('frames-skip-cover-toggle').checked = true;
            document.getElementById('videos-debug-enabled').checked = true;
            document.getElementById('videos-debug-count').value = '1';
            markCandidateSelectionMode(false);
            ensureFreshPromptBlock = async () => true;
            watchTaskUntilTerminal = () => new Promise(() => {});
        }""")
        page.evaluate("() => generateFrames()")
        page.wait_for_function("() => !!getIdeaTaskRecord(currentIdea.id, 'frames')")
        assert page.locator("#frames-auto-video-toggle").is_disabled()
        page.evaluate("() => { endIdeaTask(currentIdea.id, 'frames'); hydrateFramesPanel(currentIdea); }")
        page.evaluate("() => generateFramesSelection()")
        assert [endpoint for endpoint, _ in posts] == ["/api/generate_frames", "/api/generate_frames_selection"]
        for _, body in posts:
            assert body["auto_generate_videos"] is True
            assert body["config"]["videoFramePairing"] == "auto"
            assert body["video_target_slots"] == [4]
            assert body["merge_speed"] == 4
        browser.close()


def test_progressive_frame_events_update_same_video_watcher_and_visible_status():
    with static_server() as base, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        _mock_api(page)
        page.goto(base + "/index.html", wait_until="load")
        page.wait_for_function("() => typeof handleAutoVideoHandoff === 'function'")
        _open_idea(page, IDEA)
        page.evaluate("""() => {
            window.autoVideoConnections = [];
            streamVideosProgress = (taskId, idea, targets, metadata) => {
                autoVideoConnections.push({ taskId, ideaId: idea.id });
                const rec = beginIdeaTask(idea.id, 'videos', taskId, new AbortController());
                rec.requestId = metadata.requestId;
                rec.targetSlots = targets;
                return new Promise(() => {});
            };
            const base = { target_slots: [4, 9], frame_pairs: [
                { slot: 4, start_anchor_slot: 2, end_anchor_slot: 5 },
                { slot: 9, start_anchor_slot: 5, end_anchor_slot: 8 }
            ] };
            watchTaskUntilTerminal = async (_taskId, options) => {
                const first = { ...base, status: 'started', task_id: 'progressive-child', request_id: 'progressive-request',
                    ready_slots: [4], queued_slots: [4], pending_slots: [9] };
                options.onEvent('auto_video_started', first);
                window.statusAfterFirstPair = document.getElementById('frames-auto-video-status').textContent;
                const second = { ...first, ready_slots: [4, 9], queued_slots: [4, 9], pending_slots: [] };
                options.onEvent('auto_video_updated', second);
                options.onEvent('auto_video_updated', second);
                return { status: 'completed', result: { frames: [], videos: [], auto_video: second } };
            };
        }""")
        page.evaluate("() => streamFramesProgress('progressive-parent', currentIdea)")
        assert "已调度 1/2 段" in page.evaluate("() => statusAfterFirstPair")
        assert "1 段等待首尾帧" in page.evaluate("() => statusAfterFirstPair")
        status = page.locator("#frames-auto-video-status").inner_text()
        assert "已调度 2/2 段" in status
        assert "VID 004：IMG 002 → IMG 005" in status
        assert page.evaluate("() => autoVideoConnections.length") == 1
        assert page.evaluate("() => getIdeaTaskRecord(currentIdea.id, 'videos').total") == 2
        assert page.evaluate("() => getIdeaTaskRecord(currentIdea.id, 'videos').targetSlots") == [4, 9]
        browser.close()
