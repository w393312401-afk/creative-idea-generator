"""Resume only failed shack slots via the normal server task API."""
import json
from pathlib import Path
import requests
from prompt_pipeline import _parse_prompt_slots

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT / 'outputs/run_import_1788960344600_夹缝废弃破木棚爆改极窄现代住宅'

def main():
    report_path = ROOT / 'runtime/flow_recovery_0909/restarted_task.json'
    if report_path.exists():
        raise RuntimeError('Restart already dispatched; inspect saved task before resubmitting')
    item = json.loads((ROOT / 'library/items/import_1788960344600.json').read_text())
    manifest = json.loads((PROJECT / 'manifest.json').read_text())
    _, expected = _parse_prompt_slots(item['prompt_block'])
    preserved = {v['slot'] for v in manifest['videos'] if v['status'] == 'success'}
    slots = sorted(set(expected) - preserved)
    assert slots and 21 not in slots
    body = dict(title=item['project_key'], display_title=item['title'],
                prompt_block=item['prompt_block'], target_slots=slots)
    response = requests.post('http://127.0.0.1:8085/api/generate_videos', json=body, timeout=30)
    response.raise_for_status()
    result = response.json()
    assert result.get('task_id'), result
    report = dict(result, target_slots=slots, preserved_slots=sorted(preserved))
    report_path.write_text(json.dumps(report, indent=2))
    print(json.dumps(report))

if __name__ == '__main__':
    main()
