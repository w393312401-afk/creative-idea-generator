"""Recover the proven, already-paid slot 21 without submitting generation."""
import json
import shutil
import subprocess
from pathlib import Path
from playwright.sync_api import sync_playwright
from integrations.google_fx.services.flow_video_identity import recover_project_videos
from integrations.google_fx.utils.browser import download_video_via_browser
from video_generator import _ManifestWriter

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT / 'outputs/run_import_1788960344600_夹缝废弃破木棚爆改极窄现代住宅'

def main():
    manifest_path = PROJECT / 'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    entry = next(v for v in manifest['videos'] if v['slot'] == 21)
    dest = PROJECT / 'videos/vid_021.mp4'
    if entry['status'] == 'success' or dest.exists():
        raise RuntimeError('Slot already exists; refusing overwrite')
    backup = ROOT / 'runtime/flow_recovery_0909/manifest_before_slot21.json'
    if not backup.exists():
        shutil.copy2(manifest_path, backup)
    request = dict(tile_id='recover_slot21', prompt=entry['prompt'],
                   refs=['365e1c63-e773-4bca-9f5d-4d146b956460',
                         '925784a5-daa9-45f9-9de9-717029d331ea'],
                   click_time=1788963929)
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp('http://127.0.0.1:52980')
        page = next(x for x in browser.contexts[0].pages if x.url == manifest['google_fx_project_url'])
        states = recover_project_videos(page, [request])
        state = states.get(request['tile_id'])
        if not state or state.get('mediaId') != '0f6a76b2-faec-46bd-ae55-11b0617574d3':
            raise RuntimeError('No unique verified result; will not guess')
        downloaded = Path(download_video_via_browser(page, state['videoSrc'],
                          str(ROOT / 'runtime/flow_recovery_0909'), 'slot21'))
        probe = json.loads(subprocess.check_output(['ffprobe', '-v', 'error',
            '-show_streams', '-show_format', '-of', 'json', str(downloaded)]))
        assert any(s['codec_type'] == 'video' and s.get('width', 0) > 0 for s in probe['streams'])
        assert float(probe['format']['duration']) > 1
        shutil.copy2(downloaded, dest)
        fixed = dict(entry, status='success', file=str(dest.relative_to(ROOT)),
                     url='/' + str(dest.relative_to(ROOT)),
                     recovered_media_id=state['mediaId'], source='project_media_identity')
        fixed.pop('error', None)
        _ManifestWriter(str(manifest_path), manifest,
                        [v['slot'] for v in manifest['videos']]).record(fixed)
        print('Recovered slot 21:', probe['format']['duration'], 'seconds')

if __name__ == '__main__':
    main()
