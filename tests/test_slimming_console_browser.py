"""Native-browser acceptance with every network request served by local fixtures.

No application server, AdsPower, credentials, or model API is used.
"""
from pathlib import Path
import json

ROOT = Path(__file__).resolve().parents[1]


def test_trimmed_console_boot_and_navigation():
    from playwright.sync_api import sync_playwright

    errors = []
    requested = []
    def route_request(route):
        from urllib.parse import urlsplit
        url = urlsplit(route.request.url)
        if url.netloc != 'spark.test':
            route.abort()
            return
        path = url.path.lstrip('/') or 'console.html'
        requested.append(url.path)
        if path.startswith('api/'):
            payload = {'status': 'success', 'accounts': [], 'tasks': []}
            if path == 'api/mode':
                payload = {'server_managed': False, 'needs_access_code': False}
            elif path == 'api/tasks':
                payload = []
            route.fulfill(content_type='application/json', body=json.dumps(payload))
            return
        target = (ROOT / path).resolve()
        if ROOT not in target.parents or target.suffix not in {'.html', '.js', '.css'} or not target.is_file():
            route.abort()
            return
        mime = {'.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css'}[target.suffix]
        route.fulfill(content_type=mime, body=target.read_text())

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            context = browser.new_context(service_workers='block')
            context.route('**/*', route_request)
            page = context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto('http://spark.test/console.html#models', wait_until='networkidle')
            assert not errors, errors
            assert page.locator('#tab-google-fx').evaluate("el => el.classList.contains('active')")
            assert page.locator('[data-tab="models"], [data-tab="playground"], #tab-models, #tab-playground').count() == 0
            page.locator('[data-tab="docs"]').click()
            assert page.locator('#tab-docs').evaluate("el => el.classList.contains('active')")
            page.locator('[data-doc="post-image-edit"]').click()
            assert not errors, errors
            assert page.locator('#code-image-edit-block').inner_text().strip() != '加载中...', page.locator('.doc-panel').evaluate_all("els => els.map(el => [el.id, el.getAttribute('style')])")
            page.set_viewport_size({'width': 390, 'height': 844})
            page.locator('#mobile-menu-toggle').click()
            page.locator('[data-tab="google-fx"]').click()
            assert page.locator('#tab-google-fx').evaluate("el => el.classList.contains('active')")
            assert not errors, errors
            assert '/api/mode' in requested
        finally:
            browser.close()


if __name__ == '__main__':
    test_trimmed_console_boot_and_navigation()
    print('trimmed console browser acceptance passed (all requests intercepted)')
