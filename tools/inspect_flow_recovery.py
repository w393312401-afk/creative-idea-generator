"""Read existing Flow project media for a local recovery investigation."""
import json
from pathlib import Path
from playwright.sync_api import sync_playwright
from integrations.google_fx.services.flow_video_identity import parse_project_media
from integrations.google_fx.utils.browser import flow_project_id

def main():
    out = Path('runtime/flow_recovery_0909')
    out.mkdir(exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp('http://127.0.0.1:52980')
        context = browser.contexts[0]
        projects = {page.url for page in context.pages if flow_project_id(page.url)}
        print('projects', sorted(projects))
        for url in sorted(projects):
            project = flow_project_id(url)
            reader = context.new_page()
            bodies = []
            def received(response):
                if '/data/batchexecute' in response.url:
                    try:
                        bodies.append(response.text())
                    except Exception:
                        pass
            reader.on('response', received)
            try:
                reader.goto(url, wait_until='domcontentloaded')
                reader.wait_for_timeout(12000)
                rows = {}
                for body in bodies:
                    for row in parse_project_media(body, project):
                        rows[row['media_id']] = row
                (out / (project + '.json')).write_text(json.dumps(list(rows.values()), ensure_ascii=False, indent=2))
                (out / (project + '.responses.json')).write_text(json.dumps(bodies))
                print(project, 'responses', len(bodies), 'videos', len(rows))
                print(reader.locator('flow-grid-tile-container').evaluate_all('(els)=>els.map(e=>({title:e.getAttribute("aria-label"),type:e.querySelector("flow-video-tile")?"video":"image"}))'))
            finally:
                reader.close()

if __name__ == '__main__':
    main()
