"""Public-link media acceptance with every request fulfilled by local fixtures.

Loads the complete index and its real scripts. No running server, credentials,
generation service, or media files are needed.
"""
import base64
import copy
import json
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


ROOT = Path(__file__).resolve().parents[1]
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
)
IDEA = {
    "id": "media-counts-ui", "title": "Counts acceptance", "project_key": "counts-fixture",
    "prompt_block": "Fixture prompts", "audit_md": "PASS", "repair_md": "PASS",
    "collage_url": "/outputs/counts-fixture/collage.png",
    "covers": ["/outputs/counts-fixture/cover.png", "/outputs/counts-fixture/cover-history.png"],
    "prompt_slots": {
        "images": [{"index": i, "body": "image"} for i in (1, 2, 3)],
        "videos": [{"index": i, "body": "video"} for i in (1, 2)],
    },
    "frameRun": {
        "frames": [
            {"sequence": 1, "url": "/outputs/counts-fixture/frame-1.png"},
            {"sequence": 2, "url": "/outputs/counts-fixture/frame-2.png",
             "quality_gate": "pending_manual_review"},
        ],
        "videos": [
            {"slot": 1, "status": "success", "url": "/outputs/counts-fixture/clip-1.mp4"},
            {"slot": 2, "status": "failed", "error": "fixture video failure"},
        ],
        "merged_video": {"status": "success", "url": "/outputs/counts-fixture/merged.mp4",
                         "duration_seconds": 8, "size_bytes": 1024, "speed": 1},
    },
}
PROJECT = {
    "project_key": "counts-fixture", "kind": "library", "title": IDEA["title"],
    "state": "completed", "cover": IDEA["covers"][0], "library": IDEA,
    "image_count": 3, "video_count": 2,
    "progress": {"image_ready": 2, "image_total": 3, "video_ready": 1, "video_total": 2},
    "assets": {"file_count": 7, "bytes": 2048, "cover": IDEA["covers"][0]},
    "sub_jobs": [],
}
GALLERY = {
    "totals": {"images": 2, "videos": 1, "bytes": 3072},
    "groups": [{"key": "counts-fixture", "kind": "project", "title": IDEA["title"],
                "items": [
                    {"path": "counts-fixture/gallery-1.png", "url": "/outputs/counts-fixture/gallery-1.png",
                     "name": "gallery-1.png", "type": "image", "kind": "frame", "size": 1024, "mtime": 1},
                    {"path": "counts-fixture/gallery-2.png", "url": "/outputs/counts-fixture/gallery-2.png",
                     "name": "gallery-2.png", "type": "image", "kind": "cover", "size": 1024, "mtime": 1},
                    {"path": "counts-fixture/gallery-clip.mp4", "url": "/outputs/counts-fixture/gallery-clip.mp4",
                     "name": "gallery-clip.mp4", "type": "video", "kind": "video", "size": 1024, "mtime": 1},
                ]}],
}
ARCHIVE = {
    "archived": True,
    "archive": {
        "cover_url": "/outputs/counts-fixture/archive-poster.png",
        "final_videos": [{"url": "/outputs/counts-fixture/archive.mp4", "name": "archive.mp4"}],
    },
}


@contextmanager
def fixture_page(host="spark.test", restore_idea=True, gallery_data=None):
    from playwright.sync_api import sync_playwright

    media_requests, api_requests, errors = [], [], []
    idea = copy.deepcopy(IDEA)
    gallery = copy.deepcopy(gallery_data if gallery_data is not None else GALLERY)

    def gallery_response(path, query):
        filter = query.get("filter", ["all"])[0]
        search = query.get("q", [""])[0].strip().lower()
        counts = dict.fromkeys(("all", "frame", "cover", "video", "merged", "studio", "orphan"), 0)
        groups = []
        for group in gallery["groups"]:
            matching = []
            for item in group["items"]:
                orphan = group.get("orphan") is True if group["kind"] == "project" else item.get("in_use") is False
                counts["all"] += 1
                if item["kind"] in counts:
                    counts[item["kind"]] += 1
                counts["orphan"] += int(orphan)
                if filter != "all" and not (orphan if filter == "orphan" else item["kind"] == filter):
                    continue
                if search and not any(search in str(value).lower() for value in (
                        group["title"], group.get("idea_title", ""), item["name"], item["path"])):
                    continue
                matching.append(item)
            if matching:
                summary = {key: value for key, value in group.items() if key != "items"}
                groups.append(summary | {"total_count": len(group["items"]), "filtered_count": len(matching),
                                         "count": len(matching), "bytes": sum(item["size"] for item in matching),
                                         "oldest_mtime": min(item["mtime"] for item in matching),
                                         "latest_mtime": max(item["mtime"] for item in matching), "items": matching})
        if path == "api/gallery/index":
            return {"revision": "gallery-fixture", "totals": gallery["totals"], "filter_counts": counts,
                    "groups": [{key: value for key, value in group.items() if key != "items"} for group in groups]}
        group = next(group for group in groups if group["key"] == query["group"][0])
        offset, limit = int(query.get("offset", ["0"])[0]), int(query.get("limit", ["12"])[0])
        return {"revision": "gallery-fixture", "group_key": group["key"], "total_count": group["total_count"],
                "filtered_count": group["filtered_count"], "offset": offset,
                "has_more": offset + limit < len(group["items"]), "items": group["items"][offset:offset + limit]}

    def route_request(route):
        request = route.request
        url = urlsplit(request.url)
        if url.hostname != host:
            route.abort()
            return
        path = url.path.lstrip("/") or "index.html"
        if path.startswith("outputs/"):
            media_requests.append(url.path)
            # Video requests only need to be observed; decoding is irrelevant to
            # testing source assignment, pausing, and release.
            route.fulfill(content_type="image/png" if path.endswith(".png") else "video/mp4",
                          body=PNG if path.endswith(".png") else b"")
            return
        if path.startswith("api/"):
            api_requests.append((request.method, url.path))
            payload = {}
            if path == "api/mode":
                payload = {"server_managed": False, "needs_access_code": False, "gate_settings": []}
            elif path == "api/library":
                payload = [idea]
            elif path == "api/library/index":
                payload = {"items": [{key: idea[key] for key in ("id", "title", "project_key") if key in idea}], "count": 1}
            elif path == "api/library/item":
                payload = idea if request.method == "GET" else {"status": "success"}
            elif path == "api/get_manifest":
                payload = idea["frameRun"]
            elif path == "api/tasks":
                payload = {"tasks": []}
            elif path == "api/tasks/summary":
                payload = {"tasks": [], "counts": {"running": 0, "ideation_running": 0}, "total_count": 0}
            elif path == "api/projects":
                payload = {"projects": [PROJECT], "counts": {"active": 1, "completed": 1}, "total_count": 1}
            elif path == "api/gallery":
                payload = gallery
            elif path in ("api/gallery/index", "api/gallery/items"):
                payload = gallery_response(path, parse_qs(url.query))
            elif path == "api/project/references":
                payload = {"status": "ok"}
            elif path == "api/google-fx/config":
                payload = {"config": {}}
            elif path == "api/ping":
                payload = {"online": True}
            elif path == "api/codex-video-editor/jobs":
                payload = {"jobs": [{"id": "fixture-edit", "source": idea["frameRun"]["merged_video"]["url"],
                                     "status": "completed", "mode": "trim", "created_at": 1,
                                     "output": {"url": "/outputs/counts-fixture/refined.mp4",
                                                "duration_seconds": 6, "size_bytes": 2048}}]}
            elif path == "api/codex-video-editor/capabilities":
                payload = {"available": True}
            route.fulfill(content_type="application/json", body=json.dumps(payload))
            return
        target = (ROOT / path).resolve()
        if ROOT not in target.parents or target.suffix not in {".html", ".js", ".css"} or not target.is_file():
            route.abort()
            return
        mime = {".html": "text/html", ".js": "application/javascript", ".css": "text/css"}[target.suffix]
        route.fulfill(content_type=mime, body=target.read_text())

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            context = browser.new_context(service_workers="block", viewport={"width": 1440, "height": 1000})
            context.route("**/*", route_request)
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            if restore_idea:
                page.add_init_script("localStorage.setItem('spark_current_idea', " + json.dumps(json.dumps(idea)) + ");")
                history = [{"id": f"history-{i}", "image": f"/outputs/counts-fixture/history-{i}.png",
                            "prompt": "fixture", "model": "fixture", "ratio": "9:16", "quality": "2K"}
                           for i in range(30)]
                page.add_init_script("localStorage.setItem('spark_image_history', " + json.dumps(json.dumps(history)) + ");")
            page.goto(f"http://{host}/index.html", wait_until="networkidle")
            page.wait_for_function("() => typeof MediaPreview !== 'undefined' && typeof renderGallery === 'function'")
            yield page, media_requests, api_requests, errors
        finally:
            browser.close()


def assert_sources_released(page):
    assert page.locator("[data-media-preview-src][src], [data-media-preview-poster][poster]").count() == 0
    assert page.locator("video[data-media-preview-src]").evaluate_all("els => els.every(el => el.paused)")


def test_public_counts_no_media_requests_and_single_preview_then_toggle():
    with fixture_page() as (page, requests, api_requests, errors):
        page.wait_for_function("() => currentIdea && currentIdea.id === 'media-counts-ui' && document.getElementById('frame-slot-1')")
        assert not errors, errors
        assert ("GET", "/api/library/index") in api_requests
        assert ("GET", "/api/library/item") in api_requests
        assert ("GET", "/api/library") not in api_requests
        assert ("GET", "/api/tasks/summary") in api_requests
        assert page.locator("#media-preview-toggle").get_attribute("aria-pressed") == "true"
        assert "2/3" in page.locator("#frames-meta").inner_text()
        assert "1/2" in page.locator("#videos-meta").inner_text()
        assert page.locator("#frame-slot-3").get_attribute("data-kind") == "missing"
        assert page.locator("#video-slot-2").get_attribute("data-kind") == "failed"
        assert page.locator("#codex-edit-status").inner_text() == "精剪完成"

        page.evaluate("async () => { switchMainTab('projects'); await refreshProjects(); }")
        page.locator("#projects-list .project-row").wait_for()
        assert "共 1" in page.locator("#projects-stats").inner_text()
        assert "图片 2/3" in page.locator("#projects-list").inner_text()
        page.evaluate("async () => { switchMainTab('gallery'); await refreshGallery(); }")
        assert "2 张图片 · 1 个视频" in page.locator("#gallery-stats").inner_text()
        assert page.locator("#gallery-groups .gallery-card").count() == 0
        assert ("GET", "/api/gallery/index") in api_requests
        assert ("GET", "/api/gallery/items") not in api_requests
        assert ("GET", "/api/gallery") not in api_requests
        page.locator("#gallery-groups .g-group-title").click()
        page.wait_for_function("() => document.querySelectorAll('#gallery-groups .gallery-card').length === 3")
        assert ("GET", "/api/gallery/items") in api_requests
        assert page.locator("#gallery-groups .gallery-card").count() == 3
        assert page.locator("#gallery-groups .gallery-play-badge").is_hidden()
        assert page.locator("#history-count").inner_text() == "30"
        # Exercise the real archive renderer, whose poster could otherwise still
        # transfer an image even when the video's source is withheld.
        page.evaluate("archive => { const panel = document.createElement('div'); panel.id = 'fixture-archive'; panel.innerHTML = projectsArchiveFilesHtml(archive); document.body.append(panel); }", ARCHIVE)
        page.wait_for_function("() => document.querySelector('#fixture-archive .media-count-placeholder')")
        page.wait_for_timeout(100)
        assert requests == [], requests
        assert_sources_released(page)
        assert page.locator("#cover-img-display").get_attribute("data-media-preview-src")
        assert page.locator("#collage-preview-img").get_attribute("data-media-preview-src")
        assert page.locator("#merged-video-player").get_attribute("data-media-preview-src")
        assert page.locator("#codex-edit-player").get_attribute("data-media-preview-src")
        assert page.locator("#fixture-archive video").get_attribute("data-media-preview-poster")

        page.locator(".gallery-card[data-path='counts-fixture/gallery-1.png'] .media-count-placeholder").click()
        page.wait_for_function("() => document.getElementById('lightbox-img').naturalWidth > 0")
        assert requests == ["/outputs/counts-fixture/gallery-1.png"], requests
        assert page.locator("#lightbox-modal").is_visible()
        assert page.locator("#media-preview-toggle").get_attribute("aria-pressed") == "true"
        page.locator("#close-lightbox-btn").click()
        assert page.locator("#lightbox-img").get_attribute("src") in (None, "")
        assert page.locator("#lightbox-video").get_attribute("src") in (None, "")

        page.locator("#media-preview-toggle").click()
        page.wait_for_function("() => !MediaPreview.countsOnly() && document.querySelector('#fixture-archive video[src][poster]')")
        page.wait_for_timeout(100)
        assert page.locator("#media-preview-toggle").get_attribute("aria-pressed") == "false"
        assert "/outputs/counts-fixture/archive-poster.png" in requests
        assert "/outputs/counts-fixture/merged.mp4" in requests
        assert "/outputs/counts-fixture/refined.mp4" in requests
        assert page.locator("#frame-slot-3").get_attribute("data-kind") == "missing"
        page.reload(wait_until="networkidle")
        assert page.locator("#media-preview-toggle").get_attribute("aria-pressed") == "false", "Explicit preview choice survives reload"
        page.locator("#media-preview-toggle").click()
        assert_sources_released(page)
        requests.clear()
        page.reload(wait_until="networkidle")
        page.wait_for_function("() => document.getElementById('frame-slot-1')")
        assert page.locator("#media-preview-toggle").get_attribute("aria-pressed") == "true"
        assert requests == [], requests
        assert_sources_released(page)
        # Clearing a project's finished media must also discard the deferred URL,
        # otherwise switching previews back on would fetch the previous project.
        page.evaluate("() => { syncCodexVideoEditor(null, {id: 'empty-fixture'}); imgStudioResetSpotlightUI(); }")
        assert page.locator("#codex-edit-player").get_attribute("data-media-preview-src") in (None, "")
        page.evaluate("() => MediaPreview.setCountsOnly(false)")
        assert page.locator("#codex-edit-player").get_attribute("src") in (None, "")
        assert not errors, errors
        assert not [path for method, path in api_requests if method != "GET" and any(
            token in path for token in ("generate", "compose", "restart", "archive", "delete"))]


def test_local_default_and_public_private_host_classification():
    with fixture_page(host="localhost", restore_idea=False) as (page, requests, _, errors):
        assert page.locator("#media-preview-toggle").get_attribute("aria-pressed") == "false"
        assert page.evaluate("() => MediaPreview.countsOnly()") is False
        for host in ("localhost", "127.0.0.1", "192.168.1.25", "10.12.4.1", "172.16.1.2", "172.31.255.1"):
            assert page.evaluate("host => MediaPreview.isLocalHost(host)", host), host
        for host in ("spark.test", "example.com", "8.8.8.8", "172.15.1.1", "172.32.0.1"):
            assert not page.evaluate("host => MediaPreview.isLocalHost(host)", host), host
        page.set_viewport_size({"width": 390, "height": 844})
        toggle = page.locator("#media-preview-toggle")
        assert toggle.is_visible()
        toggle.click()
        assert toggle.get_attribute("aria-pressed") == "true"
        assert_sources_released(page)
        assert not errors, errors


def test_gallery_real_requests_are_lazy_and_selection_covers_unloaded_pages():
    gallery = copy.deepcopy(GALLERY)
    gallery["groups"][0]["items"] = [
        {"path": f"outputs/counts-fixture/frame-{index}.png", "url": f"/outputs/counts-fixture/frame-{index}.png",
         "name": f"frame-{index}.png", "type": "image", "kind": "frame", "size": 10, "mtime": 1}
        for index in range(30)
    ]
    gallery["groups"].append({"key": "second-fixture", "kind": "project", "title": "Second",
                               "items": [{"path": "outputs/second-fixture/one.png", "url": "/outputs/second-fixture/one.png",
                                          "name": "one.png", "type": "image", "kind": "frame", "size": 10, "mtime": 1}]})
    gallery["totals"] = {"images": 31, "videos": 0, "bytes": 310}
    with fixture_page(restore_idea=False, gallery_data=gallery) as (page, requests, api_requests, errors):
        page.evaluate("async () => { switchMainTab('gallery'); await refreshGallery(); }")
        assert page.locator("#gallery-groups .gallery-group").count() == 2
        assert page.locator("#gallery-groups .gallery-card").count() == 0
        assert ("GET", "/api/gallery/items") not in api_requests
        group = page.locator(".gallery-group[data-group='counts-fixture']")
        group.locator(".g-group-title").click()
        page.wait_for_function("() => document.querySelectorAll('.gallery-card').length === 12")
        assert len([entry for entry in api_requests if entry == ("GET", "/api/gallery/items")]) == 1
        group.locator(".gallery-expand-btn").click()
        page.wait_for_function("() => document.querySelectorAll('.gallery-card').length === 24")
        assert len([entry for entry in api_requests if entry == ("GET", "/api/gallery/items")]) == 2
        group.locator(".g-group-select").click()
        page.wait_for_function("() => gallerySelectedPaths().length === 30 && !galleryBulkLoading")
        assert page.locator(".gallery-card").count() == 24
        assert "已选 30 项" in page.locator("#gallery-selection-note").inner_text()
        page.locator("#gallery-select-all-btn").click()
        page.wait_for_function("() => gallerySelectedPaths().length === 31 && !galleryBulkLoading")
        page.locator("#gallery-filters [data-filter='video']").click()
        page.wait_for_function("() => galleryData.queryKey === galleryQueryKey() && galleryVisibleGroups().length === 0")
        assert page.evaluate("() => gallerySelectedPaths().length") == 0
        page.locator("#gallery-filters [data-filter='frame']").click()
        page.wait_for_function("() => galleryData.queryKey === galleryQueryKey() && galleryVisibleGroups().length === 2")
        assert page.evaluate("() => gallerySelectedPaths().length") == 0
        assert ("GET", "/api/gallery") not in api_requests
        assert requests == [], requests
        assert not errors, errors


if __name__ == "__main__":
    test_public_counts_no_media_requests_and_single_preview_then_toggle()
    test_local_default_and_public_private_host_classification()
    test_gallery_real_requests_are_lazy_and_selection_covers_unloaded_pages()
    print("media preview browser acceptance passed (all requests intercepted)")
