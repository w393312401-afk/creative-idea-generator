"""/api/project/rename 的端点契约——重点是「改名」与「搬目录」两件事的分离。

磁盘那一侧由 tests/test_project_rename.py 守；这里守的是端点回给前端的那份改名
清单，因为前端就是照着它决定「标题能不能改、project_key 钉成什么」的：

- 空闲时：目录整体搬走，回**新键**，前端把新键钉进条目；
- 这一单还有作业在跑时：目录**不搬**（worker 手里攥着旧目录路径），但改名照样
  成功，回**旧键**——前端把旧键钉成 project_key，磁盘命名空间从此不跟标题走，
  标题随便改而帧/视频/封面一张不丢。
  这条以前是 409：用户一边看着视频在跑一边点 ✨，主题换了、项目名一直不动，
  是个静默失效的坑；
- 目标目录已存在仍是 409（宁可不改名，也不能把两单资产混进一个目录）。
"""
import io
import json
import os
from email.message import Message

import pytest

import server
import server_common


OLD_KEY = 'run_import_1786251495795__旧名字'
NEW_TITLE = '新名字'


@pytest.fixture(autouse=True)
def _no_access_gate(monkeypatch):
    monkeypatch.setattr(server, 'ACCESS_CODE', '', raising=False)


@pytest.fixture
def outputs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    os.makedirs('outputs', exist_ok=True)
    monkeypatch.setattr(server_common, 'OUTPUT_ROOT', 'outputs', raising=False)
    monkeypatch.setattr(server, 'OUTPUT_ROOT', 'outputs', raising=False)
    name = server_common._safe_project_name(OLD_KEY)
    os.makedirs(os.path.join('outputs', name, 'frames'))
    with open(os.path.join('outputs', name, 'manifest.json'), 'w', encoding='utf-8') as f:
        json.dump({'frames': [{'file': f'outputs/{name}/frames/img_001.webp'}]}, f)
    return tmp_path / 'outputs'


@pytest.fixture(autouse=True)
def _no_tasks(monkeypatch):
    monkeypatch.setattr(server, 'ACTIVE_TASKS', {}, raising=False)
    monkeypatch.setattr(server, 'save_task_to_disk', lambda tid: None, raising=False)


def _post(payload):
    body = json.dumps(payload).encode('utf-8')
    h = object.__new__(server.SparkRequestHandler)
    h.path = '/api/project/rename'
    headers = Message()
    headers['Content-Type'] = 'application/json'
    headers['Content-Length'] = str(len(body))
    h.headers = headers
    h.rfile = io.BytesIO(body)
    sent = []
    h._send_json = lambda obj, status=200: sent.append((obj, status))
    server.SparkRequestHandler.do_POST(h)
    assert len(sent) == 1
    return sent[0]


def _running(project_key):
    return {'videos_x': {'id': 'videos_x', 'status': 'running',
                         'dimensions': {'project_key': project_key}, 'result': {}}}


class TestIdle:
    def test_moves_the_directory_and_returns_the_new_key(self, outputs):
        data, status = _post({'project_key': OLD_KEY, 'new_title': NEW_TITLE})
        assert status == 200
        assert data['moved'] is True
        assert data['project_key'] == 'run_import_1786251495795__新名字'
        assert (outputs / data['new_dir_name'] / 'manifest.json').exists()
        assert not (outputs / data['old_dir_name']).exists()

    def test_existing_target_directory_still_refuses(self, outputs):
        (outputs / server_common._safe_project_name(
            server_common.rekey_project_title(OLD_KEY, NEW_TITLE))).mkdir()
        data, status = _post({'project_key': OLD_KEY, 'new_title': NEW_TITLE})
        assert status == 409
        assert 'error' in data


class TestBusy:
    """作业在跑：名字照改，目录留在原地，命名空间钉回旧键。"""

    def test_rename_succeeds_without_moving_the_directory(self, outputs, monkeypatch):
        monkeypatch.setattr(server, 'ACTIVE_TASKS', _running(OLD_KEY), raising=False)
        data, status = _post({'project_key': OLD_KEY, 'new_title': NEW_TITLE})
        assert status == 200
        assert data['moved'] is False
        assert data['reason'] == 'busy'
        assert data['busy'] == ['videos_x']
        # 回的是旧键：前端拿它钉住 project_key，改完标题照样找得到旧目录
        assert data['project_key'] == OLD_KEY
        assert data['old_dir_name'] == data['new_dir_name'] == server_common._safe_project_name(OLD_KEY)
        assert (outputs / server_common._safe_project_name(OLD_KEY) / 'manifest.json').exists()

    def test_running_job_keys_are_left_alone(self, outputs, monkeypatch):
        tasks = _running(OLD_KEY)
        monkeypatch.setattr(server, 'ACTIVE_TASKS', tasks, raising=False)
        _post({'project_key': OLD_KEY, 'new_title': NEW_TITLE})
        # 目录没搬，worker 还在往旧键的目录里写——键跟着改就成了两边都残缺
        assert tasks['videos_x']['dimensions']['project_key'] == OLD_KEY

    def test_another_projects_running_job_does_not_defer_the_move(self, outputs, monkeypatch):
        monkeypatch.setattr(server, 'ACTIVE_TASKS', _running('别的项目'), raising=False)
        data, status = _post({'project_key': OLD_KEY, 'new_title': NEW_TITLE})
        assert status == 200
        assert data['moved'] is True


class TestStaleReferences:
    """目录搬走之后，**目录之外**还留着旧路径的地方也得跟着改。

    任务记录是最要命的那处：结果页的「跟进 / 查看已完成任务」整页照着 task.result
    渲染（app.js loadCompletedTask），只换 project_key 不换路径，打开就是满屏 404，
    那份带死链的对象还会被存成 currentIdea 再写回点子库。"""

    def _completed_task(self, project_key, old_dir):
        return {'frames_x': {
            'id': 'frames_x', 'status': 'completed',
            'dimensions': {'project_key': project_key, 'theme': '旧名字'},
            'result': {
                'project_key': project_key,
                'covers': [f'/outputs/{old_dir}/cover_1.webp'],
                'frameRun': {'frames': [{'url': f'/outputs/{old_dir}/frames/img_001.webp',
                                         'file': f'outputs/{old_dir}/frames/img_001.webp'}]},
            },
        }}

    def test_task_records_follow_the_moved_directory(self, outputs, monkeypatch):
        old_dir = server_common._safe_project_name(OLD_KEY)
        tasks = self._completed_task(OLD_KEY, old_dir)
        saved = []
        monkeypatch.setattr(server, 'ACTIVE_TASKS', tasks, raising=False)
        monkeypatch.setattr(server, 'save_task_to_disk', saved.append, raising=False)

        data, status = _post({'project_key': OLD_KEY, 'new_title': NEW_TITLE})
        assert status == 200
        result = tasks['frames_x']['result']
        assert result['project_key'] == data['project_key']
        assert result['covers'] == [f"/outputs/{data['new_dir_name']}/cover_1.webp"]
        assert result['frameRun']['frames'][0]['url'] == \
            f"/outputs/{data['new_dir_name']}/frames/img_001.webp"
        assert result['frameRun']['frames'][0]['file'] == \
            f"outputs/{data['new_dir_name']}/frames/img_001.webp"
        assert saved == ['frames_x']       # 改完要落盘，不然重启就丢

    def test_ledger_rows_follow_the_new_key(self, outputs, monkeypatch, tmp_path):
        """台账不跟着改，合流索引按 project_key 就找不到这一行的选题/评分/投放状态。"""
        ledger = tmp_path / 'topic_ledger.json'
        ledger.write_text(json.dumps([
            {'id': 'a', 'project_key': OLD_KEY, 'status': 'used'},
            {'id': 'b', 'project_key': '别的项目', 'status': 'candidate'},
        ], ensure_ascii=False), encoding='utf-8')
        monkeypatch.setattr(server_common, 'LEDGER_FILE', str(ledger), raising=False)

        data, status = _post({'project_key': OLD_KEY, 'new_title': NEW_TITLE})
        assert status == 200
        assert data['ledger_updated'] == 1
        rows = json.loads(ledger.read_text(encoding='utf-8'))
        assert rows[0]['project_key'] == data['project_key']
        assert rows[1]['project_key'] == '别的项目'      # 别人的行一个字都不许动
        assert rows[0]['status'] == 'used'               # 只改键，其余原样

    def test_manifest_title_follows_the_new_name(self, outputs):
        """合并成片的文件名是从 manifest['title'] 抽中文推出来的
        （video_generator.merge_project_videos），不改的话下次合出来的成片又叫回旧名。"""
        pdir = outputs / server_common._safe_project_name(OLD_KEY)
        manifest = json.loads((pdir / 'manifest.json').read_text(encoding='utf-8'))
        manifest['title'] = '旧名字'
        (pdir / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False),
                                            encoding='utf-8')

        data, status = _post({'project_key': OLD_KEY, 'new_title': NEW_TITLE})
        assert status == 200
        after = json.loads((outputs / data['new_dir_name'] / 'manifest.json')
                           .read_text(encoding='utf-8'))
        assert after['title'] == NEW_TITLE
        # 路径改写照旧生效，没被这一步覆盖掉
        assert after['frames'][0]['file'].startswith(f"outputs/{data['new_dir_name']}/")

    def test_busy_deferral_leaves_task_paths_alone(self, outputs, monkeypatch):
        """目录没搬，路径就一个字都不能改——改了才是死链。"""
        old_dir = server_common._safe_project_name(OLD_KEY)
        tasks = self._completed_task(OLD_KEY, old_dir)
        tasks.update(_running(OLD_KEY))
        monkeypatch.setattr(server, 'ACTIVE_TASKS', tasks, raising=False)

        data, status = _post({'project_key': OLD_KEY, 'new_title': NEW_TITLE})
        assert status == 200 and data['reason'] == 'busy'
        assert tasks['frames_x']['result']['covers'] == [f'/outputs/{old_dir}/cover_1.webp']
        assert tasks['frames_x']['result']['project_key'] == OLD_KEY
