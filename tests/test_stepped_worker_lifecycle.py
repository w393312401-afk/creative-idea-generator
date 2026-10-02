"""The server worker must launch the current stepped pipeline and pause for review."""
from contextlib import nullcontext

import server
import stepped_pipeline


def test_start_worker_composes_anchor_and_pauses_for_review(monkeypatch, tmp_path):
    project_dir = str(tmp_path / 'project')
    calls = []

    def compose(config, dimensions, on_progress=None):
        calls.append(('compose', dimensions['theme']))
        return {
            'title': 'Garden workshop',
            'image_1_prompt': 'A disused garden shed',
            'packet': {},
            'parsed_brief': {},
            'beat_ladder': [{'index': 1}, {'index': 2}],
        }

    def render(config, title, sequence, prompt, on_progress=None):
        calls.append(('render', title, sequence, prompt))
        assert config['allowTextOnlyAnchor'] is True
        return {'image_path': str(tmp_path / 'anchor.webp'), 'project_dir': project_dir}

    monkeypatch.setattr(server, '_fx_browser_slot', lambda *args: nullcontext())
    monkeypatch.setattr(stepped_pipeline, '_get_project_dir', lambda title: project_dir)
    monkeypatch.setattr(stepped_pipeline, 'compose_anchor_and_packet', compose)
    monkeypatch.setattr(stepped_pipeline, 'render_single_frame', render)
    monkeypatch.setattr(stepped_pipeline, 'refine_packet_from_accepted_anchor',
                        lambda config, image_path, packet, brief: packet)

    task_id = 'stepped_worker_lifecycle'
    server.stepped_pipeline_start_worker(task_id, {}, {'theme': 'Garden workshop'})

    task = server.ACTIVE_TASKS[task_id]
    assert task['status'] == 'paused', task.get('error')
    assert calls == [('compose', 'Garden workshop'),
                     ('render', 'Garden workshop', 1, 'A disused garden shed')]
    assert task['result']['stage'] == 'review_anchor'
    assert task['result']['pipeline_state']['batches'][0]['sequences'] == [2]
    assert stepped_pipeline._load_state('Garden workshop')['stage'] == 'review_anchor'
    assert task['events'][-1][0] == 'stepped_paused'
