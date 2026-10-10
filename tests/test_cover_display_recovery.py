"""Cover previews recover cached empty responses without generating new images."""
import pytest

pytest.importorskip('playwright.sync_api')

from test_media_preview_browser import fixture_page, PNG


def test_cover_recovers_empty_response_then_survives_reopen_and_reload():
    with fixture_page(host='localhost') as (page, _, api_requests, errors):
        page.evaluate("() => { switchMainTab('results'); setResultLeftColumnCollapsed(false, true); }")
        requests = []
        url = '/outputs/counts-fixture/recovered-cover.png'

        def cover_response(route):
            requests.append(route.request.url)
            route.fulfill(content_type='image/png',
                          body=PNG if '?v=' in route.request.url else b'')

        page.route('**/recovered-cover.png*', cover_response)
        page.evaluate('''url => {
            currentIdea.covers = [url];
            currentIdea.activeCoverUrl = url;
            renderCoversForIdea(currentIdea);
        }''', url)
        page.wait_for_function('''() => {
            const img = document.getElementById('cover-img-display');
            return img.naturalWidth > 0 && img.style.display === 'block';
        }''')
        assert any('?v=' in request for request in requests)
        assert page.locator('#cover-image-placeholder').is_hidden()
        assert page.locator('#cover-history-thumbnails .cover-thumb').count() == 1

        # MediaPreview skips assigning the same src. Reopening the panel must
        # restore an already-decoded image without waiting for another load.
        page.evaluate('''() => {
            document.getElementById('cover-img-display').style.display = 'none';
            document.getElementById('cover-image-placeholder').style.display = 'flex';
            renderCoversForIdea(currentIdea);
            saveCurrentIdeaState();
        }''')
        page.locator('#cover-img-display').wait_for(state='visible')
        assert page.locator('#cover-image-placeholder').is_hidden()
        page.reload(wait_until='networkidle')
        page.wait_for_function("() => document.getElementById('cover-img-display').naturalWidth > 0")
        assert page.locator('#cover-img-display').is_visible()
        assert not any(endpoint == '/api/generate_cover' for _, endpoint in api_requests)
        assert not errors, errors


def test_cover_bad_response_retries_once_and_counts_mode_suppresses_requests():
    with fixture_page(host='localhost') as (page, _, _, errors):
        page.evaluate("() => { switchMainTab('results'); setResultLeftColumnCollapsed(false, true); }")
        requests = []
        url = '/outputs/counts-fixture/broken-cover.png'

        def broken_response(route):
            requests.append(route.request.url)
            route.fulfill(content_type='image/png', body=b'')

        page.route('**/broken-cover.png*', broken_response)
        # Use the real cover loader on the fixed preview; thumbnails share the
        # same helper but would introduce unrelated coalesced image requests.
        page.evaluate('''url => {
            window.coverFailureCount = 0;
            setCoverImageSource(document.getElementById('cover-img-display'), url,
                () => {}, () => { window.coverFailureCount++; });
        }''', url)
        page.wait_for_function('() => window.coverFailureCount === 1')
        page.wait_for_timeout(100)
        assert len(requests) == 2

        requests.clear()
        page.evaluate('''url => {
            MediaPreview.setCountsOnly(true);
            currentIdea.covers = [url];
            currentIdea.activeCoverUrl = url;
            renderCoversForIdea(currentIdea);
        }''', url)
        page.wait_for_timeout(100)
        assert not requests
        assert page.locator('#cover-img-display').get_attribute('src') is None
        assert page.locator('.cover-phone-frame .media-count-placeholder').is_visible()
        assert not errors, errors


def test_rerender_restores_loaded_cover_without_another_load_event():
    with fixture_page(host='localhost') as (page, _, _, errors):
        page.evaluate("() => { switchMainTab('results'); setResultLeftColumnCollapsed(false, true); }")
        page.wait_for_function("() => document.getElementById('cover-img-display').naturalWidth > 0")
        restored = page.evaluate('''() => {
            const img = document.getElementById('cover-img-display');
            const source = img.getAttribute('src');
            img.style.display = 'none';
            document.getElementById('cover-image-placeholder').style.display = 'flex';
            renderCoversForIdea(currentIdea);
            return img.getAttribute('src') === source && img.style.display === 'block';
        }''')
        assert restored
        assert page.locator('#cover-img-display').is_visible()
        assert page.locator('#cover-image-placeholder').is_hidden()
        assert not errors, errors
