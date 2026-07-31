"""创意台账只收录真正启动过激发/合成的创意。"""
from unittest.mock import Mock

import pytest

import server
import server_common


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    # 这些用例走的是真实 /api/compose 处理函数，会把任务记录落到 CWD 下的 tasks/。
    # 不切工作目录就是往项目根目录里写真实任务文件（并触发孤儿清理），见
    # test_task_history_not_wiped.py 记录的 2026-07-31 事故。
    monkeypatch.chdir(tmp_path)
    server_common.ACTIVE_TASKS.clear()
    monkeypatch.setattr(server, 'access_ok', lambda handler: True)
    monkeypatch.setattr(server, 'rate_ok', lambda ip, action='default': True)
    monkeypatch.setattr(server, '_client_ip', lambda handler: '127.0.0.1')
    monkeypatch.setattr(server, 'effective_config', lambda config: config or {})
    monkeypatch.setattr(server, 'background_worker', lambda *args, **kwargs: None)
    yield
    server_common.ACTIVE_TASKS.clear()


def _handler(path, body):
    handler = object.__new__(server.SparkRequestHandler)
    handler.path = path
    handler._read_json_body = lambda: body
    sent = []
    handler._send_json = lambda payload, status=200: sent.append((payload, status))
    return handler, sent


def test_ideation_batch_is_not_registered(monkeypatch):
    register = Mock()
    monkeypatch.setattr(server, 'register_ledger_candidates', register)
    monkeypatch.setattr(server, 'run_ideate', lambda *args, **kwargs: {
        'ideas': [
            {'dna': 'a / b / c', 'title': '未激发 A'},
            {'dna': 'd / e / f', 'title': '未激发 B'},
        ],
        'trend_refs': [],
    })
    handler, sent = _handler('/api/ideate', {'count': 2})

    server.SparkRequestHandler.do_POST(handler)

    assert sent == [({
        'status': 'ok',
        'ideas': [
            {'dna': 'a / b / c', 'title': '未激发 A'},
            {'dna': 'd / e / f', 'title': '未激发 B'},
        ],
        'trend_refs': [],
    }, 200)]
    register.assert_not_called()


def test_ideation_forwards_ledger_remix_seed(monkeypatch):
    run_ideate = Mock(return_value={'ideas': [], 'trend_refs': []})
    monkeypatch.setattr(server, 'run_ideate', run_ideate)
    seed = {'topic_dna': 'a / b / c', 'one_line': '母题'}
    handler, sent = _handler('/api/ideate', {'count': 4, 'remix_seed': seed})

    server.SparkRequestHandler.do_POST(handler)

    assert sent[0][0]['status'] == 'ok'
    run_ideate.assert_called_once_with(
        {}, 4, theme=None, theme_label=None, trend_ref_ids=[], remix_seed=seed,
        pacing_skeleton_ids=[])


def test_ideation_forwards_selected_pacing_skeletons(monkeypatch):
    run_ideate = Mock(return_value={'ideas': [], 'trend_refs': []})
    monkeypatch.setattr(server, 'run_ideate', run_ideate)
    handler, sent = _handler('/api/ideate', {
        'count': 4,
        'pacing_skeleton_ids': ['linear_milestone', 'dual_payoff'],
    })

    server.SparkRequestHandler.do_POST(handler)

    assert sent[0][0]['status'] == 'ok'
    run_ideate.assert_called_once_with(
        {}, 4, theme=None, theme_label=None, trend_ref_ids=[], remix_seed=None,
        pacing_skeleton_ids=['linear_milestone', 'dual_payoff'])


def test_compose_registers_only_the_activated_candidate(monkeypatch):
    register = Mock(return_value={'added': 1, 'duplicates': 0, 'entries': []})
    monkeypatch.setattr(server, 'register_ledger_candidates', register)
    candidate = {'dna': 'a / b / c', 'title': '真正激发的创意', 'score': 24}
    handler, sent = _handler('/api/compose', {
        'task_id': 'ledger-activation',
        'dimensions': {
            'theme': 'prompt input',
            'ledger_candidate': candidate,
        },
    })

    server.SparkRequestHandler.do_POST(handler)

    assert sent[0][0]['status'] == 'ok'
    register.assert_called_once()
    (registered,), kwargs = register.call_args
    assert kwargs == {'source': 'Creative Activation'}
    assert len(registered) == 1
    # 原候选字段一字不改地带过去
    assert {k: registered[0][k] for k in candidate} == candidate
    # 2026-07-31（P3）：登记时必须带上这条选题激发出来的项目主键。有了它，台账
    # 「回到激发项目」就是一次直查，不再靠 DNA/一句话选题去撞标题。
    assert registered[0]['project_key'] == 'run_ledger-activation__prompt input'
    # 登记发生在 prepare_task_for_run 之前，主键要在那之前就写进 dimensions
    assert candidate.get('project_key') is None, 'candidate 不该被就地改写'


def test_compose_without_ledger_candidate_does_not_register(monkeypatch):
    register = Mock()
    monkeypatch.setattr(server, 'register_ledger_candidates', register)
    handler, sent = _handler('/api/compose', {
        'task_id': 'non-idea-compose',
        'dimensions': {'theme': 'legacy API request'},
    })

    server.SparkRequestHandler.do_POST(handler)

    assert sent[0][0]['status'] == 'ok'
    register.assert_not_called()
