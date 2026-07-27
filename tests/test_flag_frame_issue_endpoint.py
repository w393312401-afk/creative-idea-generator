"""/api/flag_frame_issue（人工主动描述帧序列某一帧的问题）的端点契约。

覆盖点：
- 正常标记：描述写进 manifest 对应帧的 manual_issue，quality_gate 变 manual_flagged，
  响应里回带更新后的帧条目（前端据此立刻刷新帧网格）。
- 描述留空＝撤销标记，quality_gate 回到被标记前的值。
- sequence 非整数 400；manifest 里没有这一帧 500（描述落不了盘必须当场报错，
  不能静默丢弃人写的东西）。
- 同项目有渲染/修复 worker 在跑时 409 拒绝——那些 worker 会拿自己的 manifest 快照
  整体回写，此刻插进去的描述会被覆盖掉。
"""
import io
import json
import os
from email.message import Message

import pytest

import pipeline_orchestrator as po
import server
import server_common


_TITLE = 'flag_frame_issue_test'


@pytest.fixture(autouse=True)
def _no_access_gate(monkeypatch):
    monkeypatch.setattr(server, 'ACCESS_CODE', '', raising=False)


@pytest.fixture
def project(tmp_path, monkeypatch):
    project_dir = str(tmp_path / 'proj')
    os.makedirs(os.path.join(project_dir, 'frames'), exist_ok=True)
    manifest = {'title': _TITLE, 'frames': [
        {'slot': 1, 'sequence': 1, 'quality_gate': 'sequence_reviewed_pass'},
        {'slot': 2, 'sequence': 2, 'quality_gate': 'sequence_reviewed_pass'},
    ]}
    with open(os.path.join(project_dir, 'manifest.json'), 'w', encoding='utf-8') as f:
        json.dump(manifest, f)
    # server 与 pipeline_orchestrator 都是 `from server_common import _get_project_dir`
    # 的按名导入，各自持有独立绑定——只打 server_common 那份，落盘会跑到真实的
    # outputs/ 下去（是否生效取决于 po 在打桩前有没有被导入过，即测试执行顺序）。
    monkeypatch.setattr(server, '_get_project_dir', lambda title: project_dir)
    monkeypatch.setattr(server_common, '_get_project_dir', lambda title: project_dir)
    monkeypatch.setattr(po, '_get_project_dir', lambda title: project_dir)
    return project_dir


def _handler(payload):
    body = json.dumps(payload).encode('utf-8')
    h = object.__new__(server.SparkRequestHandler)
    h.path = '/api/flag_frame_issue'
    headers = Message()
    headers['Content-Type'] = 'application/json'
    headers['Content-Length'] = str(len(body))
    h.headers = headers
    h.rfile = io.BytesIO(body)
    sent = []
    h._send_json = lambda obj, status=200: sent.append((obj, status))
    return h, sent


def _post(payload):
    h, sent = _handler(payload)
    server.SparkRequestHandler.do_POST(h)
    assert len(sent) == 1
    return sent[0]


def _frames(project_dir):
    with open(os.path.join(project_dir, 'manifest.json'), encoding='utf-8') as f:
        return {fr['sequence']: fr for fr in json.load(f)['frames']}


def test_flag_records_description_into_manifest(project):
    body, status = _post({'title': _TITLE, 'sequence': 2, 'description': '塔吊凭空消失了'})

    assert status == 200, body
    assert body['status'] == 'ok'
    assert body['frame']['manual_issue'] == '塔吊凭空消失了'
    assert body['frame']['quality_gate'] == 'manual_flagged'

    frames = _frames(project)
    assert frames[2]['manual_issue'] == '塔吊凭空消失了'
    assert frames[2]['quality_gate'] == 'manual_flagged'
    # 其余帧一律不许被这次标记碰到
    assert frames[1]['quality_gate'] == 'sequence_reviewed_pass'
    assert 'manual_issue' not in frames[1]


def test_empty_description_clears_the_flag(project):
    _post({'title': _TITLE, 'sequence': 2, 'description': '塔吊凭空消失了'})
    body, status = _post({'title': _TITLE, 'sequence': 2, 'description': '  '})

    assert status == 200, body
    frames = _frames(project)
    assert 'manual_issue' not in frames[2]
    assert frames[2]['quality_gate'] == 'sequence_reviewed_pass'


def test_non_integer_sequence_rejected(project):
    body, status = _post({'title': _TITLE, 'sequence': '2', 'description': 'x'})
    assert status == 400
    assert body['status'] == 'error'


def test_unknown_frame_reports_error_instead_of_silently_dropping(project):
    body, status = _post({'title': _TITLE, 'sequence': 99, 'description': '这一帧不存在'})
    assert status == 500
    assert body['status'] == 'error'
    assert '99' in body['message']


def test_rejected_while_a_frame_worker_holds_the_project(project, monkeypatch):
    """渲染/修复 worker 在跑时不许插队写 manifest——它们会整体回写自己的快照，
    这时候标进去的描述会被无声覆盖。"""
    monkeypatch.setitem(server.ACTIVE_TASKS, 'frames_running',
                        {'status': 'running', 'events': [], 'type': 'frames'})
    assert server_common.claim_frame_run(project, 'frames_running') is None
    try:
        body, status = _post({'title': _TITLE, 'sequence': 2, 'description': '塔吊凭空消失了'})
    finally:
        server_common.release_frame_run(project, 'frames_running')

    assert status == 409
    assert body['status'] == 'error'
    assert 'manual_issue' not in _frames(project)[2]

    # 占位释放后同一请求正常通过，且占位已经还回去（不会把项目锁死）
    body, status = _post({'title': _TITLE, 'sequence': 2, 'description': '塔吊凭空消失了'})
    assert status == 200, body
    assert server_common.claim_frame_run(project, 'someone_else') is None
    server_common.release_frame_run(project, 'someone_else')
