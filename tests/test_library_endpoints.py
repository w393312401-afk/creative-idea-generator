"""创意库 HTTP 接口的行为契约（拆分存储落地后）。

分两组：

**新契约**（/api/library/index、/api/library/item、/api/library/item/delete）——
写一条只碰它自己的文件，因此这条路径上不该有、也不需要那三道整表防护。

**读兼容层**（GET /api/library）—— 全量导出与外部脚本还在用它，必须继续能重组出
完整数组。写路径（POST /api/library）已于 2026-07-31（P4）删除：它的三道防护
（空库拒写 / 槽位自洽 / 未声明缩量 409）防的是"整份覆盖"这个动作本身，所有写入
改走单条 API 之后，这个动作不存在了。
"""
from email.message import Message
import io
import json
import os

import pytest

import server
import server_common as sc


@pytest.fixture
def store(tmp_path, monkeypatch):
    db = str(tmp_path / 'library.json')
    lib = str(tmp_path / 'library')
    for mod in (sc, server):
        monkeypatch.setattr(mod, 'DB_FILE', db, raising=False)
        monkeypatch.setattr(mod, 'LIBRARY_DIR', lib, raising=False)
    # outputs 清理不该在这些用例里真的动文件系统
    monkeypatch.setattr(server, 'delete_idea_output_files',
                        lambda title, covers=None: {'project_dir': title, 'covers': list(covers or [])})
    return {'db': db, 'dir': lib, 'items': os.path.join(lib, 'items')}


def _get(path):
    h = object.__new__(server.SparkRequestHandler)
    h.path = path
    h._gate = lambda *a, **k: True
    sent = []
    h._send_json = lambda obj, status=200: sent.append((obj, status))
    return h, sent


def _post(path, payload):
    raw = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    h = object.__new__(server.SparkRequestHandler)
    h.path = path
    h.headers = Message()
    h.headers['Content-Length'] = str(len(raw))
    h.rfile = io.BytesIO(raw)
    h._gate = lambda *a, **k: True
    sent = []
    h._send_json = lambda obj, status=200: sent.append((obj, status))
    return h, sent


def _idea(ident, title='某个创意', frames=0):
    idea = {
        'id': ident, 'title': title, 'theme': f'做一个{title}',
        'project_key': f'run_{ident}__{title}',
        'covers': [f'outputs/covers/{ident}.webp'],
        'prompt_block': 'X' * 2000,
        'prompt_slots': {'images': [{'index': i + 1} for i in range(frames)]},
    }
    if frames:
        idea['frameRun'] = {'frames': [{'sequence': i + 1} for i in range(frames)]}
    return idea


def _seed(store, ideas):
    with open(store['db'], 'w', encoding='utf-8') as f:
        json.dump(ideas, f, ensure_ascii=False)


# ── 新契约 ────────────────────────────────────────────────────────────────

class TestSplitEndpoints:
    def test_index_returns_light_rows_only(self, store):
        _seed(store, [_idea('a', frames=3)])

        h, sent = _get('/api/library/index')
        h.do_GET()

        body, status = sent[0]
        assert status == 200
        assert body['count'] == 1
        row = body['items'][0]
        assert 'prompt_block' not in row and 'frameRun' not in row
        assert row['frame_count'] == 3

    def test_item_returns_full_body(self, store):
        _seed(store, [_idea('a', frames=3)])

        h, sent = _get('/api/library/item?id=a')
        h.do_GET()

        body, status = sent[0]
        assert status == 200
        assert len(body['prompt_block']) == 2000
        assert len(body['frameRun']['frames']) == 3

    def test_item_missing_id_is_400_and_unknown_id_is_404(self, store):
        _seed(store, [_idea('a')])

        h, sent = _get('/api/library/item')
        h.do_GET()
        assert sent[0][1] == 400

        h, sent = _get('/api/library/item?id=nope')
        h.do_GET()
        assert sent[0][1] == 404

    def test_post_item_upserts_a_single_record(self, store):
        _seed(store, [_idea('a'), _idea('b')])
        sc.read_library_index()
        untouched = open(os.path.join(store['items'], 'a.json'), encoding='utf-8').read()

        h, sent = _post('/api/library/item', {'item': _idea('b', '改过的 b')})
        h.do_POST()

        body, status = sent[0]
        assert status == 200 and body['status'] == 'success'
        assert body['entry']['title'] == '改过的 b'
        assert open(os.path.join(store['items'], 'a.json'), encoding='utf-8').read() == untouched

    def test_post_item_accepts_a_bare_object_too(self, store):
        h, sent = _post('/api/library/item', _idea('solo'))
        h.do_POST()

        assert sent[0][0]['status'] == 'success'
        assert sc.read_library_item('solo')['title'] == '某个创意'

    def test_post_item_without_id_is_400(self, store):
        h, sent = _post('/api/library/item', {'item': {'title': '无 id'}})
        h.do_POST()
        assert sent[0][1] == 400

    def test_delete_item_removes_record_and_output_files(self, store):
        _seed(store, [_idea('a'), _idea('b')])
        sc.read_library_index()

        h, sent = _post('/api/library/item/delete', {'id': 'a'})
        h.do_POST()

        body, status = sent[0]
        assert status == 200 and body['removed'] is True
        # 产物清理要按 project_key（产物落在 outputs/<project_key>/ 下），
        # 传展示标题会删不掉、留下孤儿目录
        assert body['deleted']['project_dir'] == 'run_a__某个创意'
        assert [r['id'] for r in sc.read_library_index()] == ['b']

    def test_delete_the_last_item_is_allowed(self, store):
        """老形态下删掉最后一条会撞上"空列表覆盖非空库"防护被 409 拒绝。"""
        _seed(store, [_idea('only')])
        sc.read_library_index()

        h, sent = _post('/api/library/item/delete', {'id': 'only'})
        h.do_POST()

        assert sent[0][1] == 200
        assert sc.read_library_index() == []

    def test_delete_without_id_is_400(self, store):
        h, sent = _post('/api/library/item/delete', {})
        h.do_POST()
        assert sent[0][1] == 400


# ── 整表兼容层 ────────────────────────────────────────────────────────────

class TestWholeTableCompatibility:
    def test_get_returns_the_full_array(self, store):
        _seed(store, [_idea('a', frames=2)])

        h, sent = _get('/api/library')
        h.do_GET()

        body, status = sent[0]
        assert status == 200
        assert [i['id'] for i in body] == ['a']
        assert len(body[0]['prompt_block']) == 2000

    def test_get_reflects_writes_made_through_the_new_api(self, store):
        """两套接口必须看到同一份数据，否则迁移期会出现"存了但看不见"。"""
        _seed(store, [_idea('a')])
        sc.write_library_item(_idea('new', '新收藏的'))

        h, sent = _get('/api/library')
        h.do_GET()

        assert {i['id'] for i in sent[0][0]} == {'a', 'new'}

    def test_corrupt_store_returns_500_not_an_empty_array(self, store):
        """回空数组 = 前端下一次保存把整库覆盖成空。这是真实发生过的清零路径。"""
        with open(store['db'], 'w', encoding='utf-8') as f:
            f.write('{ broken')

        h, sent = _get('/api/library')
        h.do_GET()

        assert sent[0][1] == 500

    def test_whole_table_post_is_gone(self, store):
        """POST /api/library 已于 2026-07-31（P4）删除。它的三道防护（空库拒写 /
        槽位自洽 / 未声明缩量 409）防的是"整份覆盖"这个动作本身，而所有写入都改走
        单条 API 之后，这个动作不存在了。读路径保留（全量导出还在用）。"""
        _seed(store, [_idea('a')])

        h, sent = _post('/api/library', [_idea('a'), _idea('b')])
        h.do_POST()

        assert sent == [] or sent[0][1] == 404
        # 数据没被动过
        assert [r['id'] for r in sc.read_library_index()] == ['a']
