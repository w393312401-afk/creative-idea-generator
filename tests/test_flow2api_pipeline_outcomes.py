"""A stopped Flow pipeline must distinguish full delivery from unresolved slots."""
import copy

import pytest

import pipeline_orchestrator as orchestrator
import stepped_pipeline as stepped
import server


PROMPT = '图片 1:\nstart\n图片 2:\nend\n视频 1:\nmove'
CONFIG = {'videoProvider': 'flow2api', 'imageBackend': 'api'}


@pytest.mark.parametrize('mode', ['auto', 'staged', 'stepped'])
@pytest.mark.parametrize('video_result', [
    {'videos': [{'slot': 1, 'status': 'failed'}]},
    {'videos': [{'slot': 1, 'status': 'cancelled'}]},
    {'videos': []},
    {'videos': [{'slot': 1, 'status': 'success', 'last_attempt': {'submission_pending': True}}]},
    {'videos': [{'slot': 1, 'status': 'success'}],
     'video_generation_stats': {'last_run': {'failed_slots': [1]}}},
    {'videos': [{'slot': 1, 'status': 'success', 'retained_previous': True,
                 'last_attempt': {'status': 'failed', 'submission_pending': False}}]},
])
def test_flow_pipeline_reports_partial_failure_without_another_submission(monkeypatch, tmp_path, mode, video_result):
    monkeypatch.setattr(orchestrator, 'compose_anchor_and_packet', lambda *a, **k: {
        'title': 'project', 'image_1_prompt': 'start', 'packet': {}, 'parsed_brief': {}, 'beat_ladder': []})
    monkeypatch.setattr(orchestrator, 'render_single_frame', lambda *a, **k: {
        'image_path': str(tmp_path / 'first.webp'), 'project_dir': str(tmp_path)})
    monkeypatch.setattr(orchestrator, 'refine_packet_from_accepted_anchor', lambda *a, **k: {})
    monkeypatch.setattr(orchestrator, 'compose_remaining_beats', lambda *a, **k: PROMPT)
    monkeypatch.setattr(orchestrator, 'prompt_block_from_output', lambda value: value)
    monkeypatch.setattr(orchestrator, 'persist_outline_delivery_ledger', lambda *a, **k: None)
    monkeypatch.setattr(orchestrator, 'generate_frame_sequence', lambda *a, **k: None)
    monkeypatch.setattr(orchestrator, 'optimize_video_prompts_for_sequence', lambda *a, **k: PROMPT)
    monkeypatch.setattr(orchestrator, '_get_project_dir', lambda *a: str(tmp_path))
    submissions = []

    def generate(*args, **kwargs):
        submissions.append(1)
        return copy.deepcopy(video_result)

    monkeypatch.setattr(orchestrator, 'generate_video_sequence', generate)
    events = []
    if mode == 'auto':
        result = orchestrator.run_autonomous_pipeline(CONFIG, {})
    elif mode == 'staged':
        result = orchestrator.run_staged_frame_rendering(CONFIG, 'project', PROMPT)
    else:
        state = {'stage': 'final_review', 'title': 'project', 'prompt_block': PROMPT}
        monkeypatch.setattr(stepped, '_load_state', lambda *a: state)
        monkeypatch.setattr(stepped, '_save_state', lambda *a: None)
        monkeypatch.setattr(stepped, '_enrich_state_with_refs', lambda state: state)
        monkeypatch.setattr(stepped, '_get_project_dir', lambda *a: str(tmp_path))
        monkeypatch.setattr(stepped, 'optimize_video_prompts_for_sequence', lambda *a, **k: PROMPT)
        result = stepped.advance_stepped_pipeline('project', config=CONFIG,
                                                on_progress=lambda *event: events.append(event))
        assert events[-1][1]['completion_state'] == 'partial_failed'
        assert '全部完成' not in events[-1][1]['message']
    assert result['completion_state'] == 'partial_failed'
    assert result['has_failures'] is True
    assert len(submissions) == 1


@pytest.mark.parametrize('status,expected', [('success', 'completed'), ('skipped_cut', 'completed')])
def test_expected_flow_clip_can_finish_successfully(status, expected):
    outcome = orchestrator._flow_video_outcome(CONFIG, {'videos': [{'slot': 1, 'status': status}]}, PROMPT)
    assert outcome['completion_state'] == expected and outcome['has_failures'] is False


def test_legacy_pipeline_outcome_is_unchanged():
    assert orchestrator._flow_video_outcome({'videoProvider': 'google_fx'}, {'videos': []}, PROMPT) == {}


@pytest.mark.parametrize('kind', ['auto', 'staged', 'stepped'])
def test_worker_forwards_partial_failure_as_terminal_business_outcome(monkeypatch, kind):
    result = {'title': 'project', 'stage': 'completed', 'completion_state': 'partial_failed',
              'has_failures': True, 'has_quality_warnings': False}
    monkeypatch.setattr(orchestrator, 'run_autonomous_pipeline', lambda *a, **k: dict(result))
    monkeypatch.setattr(orchestrator, 'run_staged_frame_rendering', lambda *a, **k: dict(result))
    monkeypatch.setattr(stepped, 'advance_stepped_pipeline', lambda *a, **k: dict(result))
    task_id = 'flow-outcome-' + kind
    if kind == 'auto':
        server.auto_run_worker(task_id, dict(CONFIG), {'project_key': 'project'})
    elif kind == 'staged':
        server.render_staged_worker(task_id, dict(CONFIG), 'project', PROMPT)
    else:
        server.stepped_pipeline_advance_worker(task_id, dict(CONFIG), 'project', 'approve')
    task = server.ACTIVE_TASKS[task_id]
    assert task['status'] == 'completed' and task['outcome'] == 'partial_failed'
    assert task['result']['has_failures'] is True
