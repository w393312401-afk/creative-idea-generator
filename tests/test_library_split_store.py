"""点子库拆分存储（library/index.json + library/items/<id>.json）的行为契约。

老形态是单个 library.json + "客户端始终持有完整数组、整份 POST 回来覆盖"。实测
2 条创意 208KB（单条 164KB），改一个字段要重写全库；而"整表覆盖"这个动作引发过
两次数据事故，逼出三道防线（空库拒写 / 缩量闸门 409 / .bak 轮换）。

拆开之后的核心义务，也就是这里要钉住的：
- 写一条**只**碰它自己的正文文件，绝不重写别的记录（这才是那些洞消失的原因）；
- 索引只装轻量字段，正文（prompt_block / frameRun / audit_md…）绝不进索引；
- 迁移是惰性且不可丢数据的：原文件备份成 .pre-split，损坏的老库宁可报错也不拆；
- 正文文件丢了时按索引残条返回，而不是让那条创意从库里消失；
- 整表兼容层仍然完整保留三道防护——老前端还在按那个契约工作。
"""
import json
import os

import pytest

import server_common as sc


@pytest.fixture
def store(tmp_path, monkeypatch):
    """把 DB_FILE 与 LIBRARY_DIR 都重定向到临时目录。

    这个隔离是硬要求，不是洁癖：2026-07-27 就发生过自动化测试驱动真实页面
    把开发者的真库整份覆盖（见 server_common._path_setting 的注释）。
    """
    db = str(tmp_path / 'library.json')
    lib = str(tmp_path / 'library')
    monkeypatch.setattr(sc, 'DB_FILE', db)
    monkeypatch.setattr(sc, 'LIBRARY_DIR', lib)
    return {'db': db, 'dir': lib, 'index': os.path.join(lib, 'index.json'),
            'items': os.path.join(lib, 'items')}


def _idea(ident, title='某个创意', frames=0, big=False):
    idea = {
        'id': ident,
        'title': title,
        'theme': f'做一个{title}',
        'project_key': f'run_{ident}__{title}',
        'timestamp': '2026-07-29 10:41:06',
        'covers': [f'outputs/covers/{ident}.webp'],
        'image_count': frames or None,
        'video_count': frames or None,
        # 正文字段：正是让单条膨胀到 164KB 的那些
        'prompt_block': ('X' * 4000) if big else 'prompt',
        'prompt_slots': {'images': [{'index': i + 1} for i in range(frames)]},
        'audit_md': 'PASS',
        'repair_md': '',
    }
    if frames:
        idea['frameRun'] = {'frames': [{'sequence': i + 1} for i in range(frames)]}
    return idea


def _write_legacy(store, ideas):
    with open(store['db'], 'w', encoding='utf-8') as f:
        json.dump(ideas, f, ensure_ascii=False)


# ── 迁移 ──────────────────────────────────────────────────────────────────

def test_lazy_migration_splits_and_backs_up(store):
    _write_legacy(store, [_idea('a'), _idea('b', '灯塔改造', frames=3)])

    index = sc.read_library_index()

    assert [r['id'] for r in index] == ['a', 'b']
    assert os.path.exists(store['index'])
    assert sorted(os.listdir(store['items'])) == ['a.json', 'b.json']
    # 原文件备份而不是删除：出问题时这是唯一一份完整的老数据
    assert os.path.exists(store['db'] + '.pre-split')
    assert os.path.exists(store['db'])


def test_index_carries_no_body_fields(store):
    """索引是用来渲染列表的，正文进了索引就等于什么都没拆。"""
    _write_legacy(store, [_idea('a', frames=3, big=True)])

    index = sc.read_library_index()
    entry = index[0]

    for field in ('prompt_block', 'prompt_slots', 'audit_md', 'repair_md', 'frameRun', 'covers'):
        assert field not in entry
    # 但列表卡片要用的信息一个不能少
    assert entry['title'] == '某个创意'
    assert entry['cover'] == 'outputs/covers/a.webp'
    assert entry['frame_count'] == 3
    assert entry['project_key'] == 'run_a__某个创意'
    # 索引必须显著小于正文，否则这次拆分没有意义
    body = os.path.getsize(os.path.join(store['items'], 'a.json'))
    assert os.path.getsize(store['index']) < body / 2


def test_migration_refuses_to_split_a_corrupt_legacy_file(store):
    """把读不出来的库"当成空库"拆一遍，等于把一次读取失败固化成整库清零。"""
    with open(store['db'], 'w', encoding='utf-8') as f:
        f.write('{ this is not json')

    with pytest.raises(RuntimeError, match='已停止迁移'):
        sc.read_library_index()

    assert not os.path.exists(store['index'])


def test_migration_is_idempotent(store):
    _write_legacy(store, [_idea('a')])
    sc.read_library_index()
    sc.write_library_item(_idea('b', '后加的'))

    # 再次访问不能把后加的那条冲掉（拆分库已建立 → 不再看 library.json）
    index = sc.read_library_index()
    assert {r['id'] for r in index} == {'a', 'b'}


def test_legacy_item_without_id_gets_one(store):
    """历史遗留的无 id 记录（library_shrink_verdict 的注释里提到确实存在）
    没法落成文件，迁移时要补一个稳定 id，而不是把它丢掉。"""
    _write_legacy(store, [{'title': '没有 id 的老记录', 'prompt_block': 'x'}])

    index = sc.read_library_index()
    assert len(index) == 1
    assert index[0]['id'].startswith('legacy-')


# ── 单条读写 ──────────────────────────────────────────────────────────────

def test_write_one_item_does_not_touch_the_others(store):
    """这是整个拆分的目的：一次保存只碰一个文件。"""
    _write_legacy(store, [_idea('a'), _idea('b'), _idea('c')])
    sc.read_library_index()

    others = {}
    for name in ('a.json', 'c.json'):
        path = os.path.join(store['items'], name)
        others[name] = (os.path.getmtime(path), open(path, encoding='utf-8').read())

    sc.write_library_item(_idea('b', '改过标题的 b'))

    for name, (mtime, content) in others.items():
        path = os.path.join(store['items'], name)
        assert open(path, encoding='utf-8').read() == content
        assert os.path.getmtime(path) == mtime


def test_write_new_item_lands_at_the_front(store):
    """与前端 savedIdeas.unshift 同序：最近收藏的排最前。"""
    _write_legacy(store, [_idea('a')])
    sc.read_library_index()

    sc.write_library_item(_idea('new', '刚收藏的'))

    assert [r['id'] for r in sc.read_library_index()] == ['new', 'a']


def test_write_existing_item_updates_in_place(store):
    _write_legacy(store, [_idea('a'), _idea('b')])
    sc.read_library_index()

    sc.write_library_item(_idea('b', '改过的 b'))

    index = sc.read_library_index()
    assert [r['id'] for r in index] == ['a', 'b']          # 位置不变
    assert index[1]['title'] == '改过的 b'
    assert sc.read_library_item('b')['title'] == '改过的 b'


def test_read_item_returns_full_body(store):
    _write_legacy(store, [_idea('a', frames=3, big=True)])
    sc.read_library_index()

    item = sc.read_library_item('a')
    assert len(item['prompt_block']) == 4000
    assert len(item['frameRun']['frames']) == 3


def test_read_missing_item_returns_none(store):
    _write_legacy(store, [_idea('a')])
    sc.read_library_index()
    assert sc.read_library_item('nope') is None


def test_delete_removes_index_row_and_body(store):
    _write_legacy(store, [_idea('a'), _idea('b')])
    sc.read_library_index()

    assert sc.delete_library_item('a') is True

    assert [r['id'] for r in sc.read_library_index()] == ['b']
    assert not os.path.exists(os.path.join(store['items'], 'a.json'))
    assert os.path.exists(os.path.join(store['items'], 'b.json'))


def test_delete_last_item_is_allowed(store):
    """老形态下"删掉库里最后一条"会撞上空列表覆盖防护被 409 拒绝。按 id 删
    天然带着确有意图的证据，不吃那道闸门——与台账 delete_ledger_entries 同理。"""
    _write_legacy(store, [_idea('only')])
    sc.read_library_index()

    assert sc.delete_library_item('only') is True
    assert sc.read_library_index() == []
    assert sc.read_library() == []


def test_delete_unknown_id_is_a_noop(store):
    _write_legacy(store, [_idea('a')])
    sc.read_library_index()
    assert sc.delete_library_item('nope') is False
    assert len(sc.read_library_index()) == 1


def test_ids_with_filesystem_hostile_characters(store):
    """importLibrary 生成的 id 是 Date.now()+Math.random()（带小数点），
    导入的库还可能带任意字符串 id。它们都要能安全落成文件名。"""
    for ident in ('1784684845170.123', 'a/b\\c', '中文 id', 'x' * 200):
        sc.write_library_item(_idea(ident, '边界 id'))

    index = sc.read_library_index()
    assert len(index) == 4
    for row in index:
        assert sc.read_library_item(row['id']) is not None


def test_empty_id_is_rejected(store):
    with pytest.raises(ValueError):
        sc.write_library_item({'id': '', 'title': 'x'})


# ── 整表重组（兼容层的读路径）────────────────────────────────────────────

def test_read_library_reassembles_full_array(store):
    _write_legacy(store, [_idea('a', frames=2, big=True), _idea('b')])
    sc.read_library_index()

    full = sc.read_library()
    assert [i['id'] for i in full] == ['a', 'b']
    assert len(full[0]['prompt_block']) == 4000
    assert len(full[0]['frameRun']['frames']) == 2


def test_missing_body_degrades_to_index_stub_not_a_vanished_idea(store):
    """正文文件丢了时跳过那条，等于在整表回写路径上凭空缩量——会被缩量闸门
    当成客户端状态错乱 409 掉，用户看到一句莫名其妙的报错。残条至少让那条
    创意还在库里、标题还看得见。"""
    _write_legacy(store, [_idea('a'), _idea('b')])
    sc.read_library_index()
    os.remove(os.path.join(store['items'], 'a.json'))

    full = sc.read_library()
    assert [i['id'] for i in full] == ['a', 'b']
    assert full[0]['title'] == '某个创意'


def test_read_library_with_explicit_path_still_reads_a_single_file(store):
    """迁移工具与测试要能直接读老单文件，不受当前存储形态影响。"""
    _write_legacy(store, [_idea('a')])
    sc.read_library_index()                     # 建立拆分库
    sc.write_library_item(_idea('b'))           # 只进拆分库

    assert [i['id'] for i in sc.read_library(path=store['db'])] == ['a']
    assert {i['id'] for i in sc.read_library()} == {'a', 'b'}


# ── 与项目工作台合流索引的衔接 ────────────────────────────────────────────

def test_projects_index_reads_through_the_split_store(store):
    """build_projects_index 用 read_library()，拆分之后必须照样拿得到条目——
    否则工作台上所有"已收藏"的项目会集体消失。"""
    _write_legacy(store, [_idea('1785458877351', '废弃越野救护车改造')])
    sc.read_library_index()

    rows = sc.build_projects_index(tasks=[], ledger_rows=[], with_assets=False)

    assert len(rows) == 1
    assert rows[0]['saved'] is True
    assert rows[0]['state'] == 'saved'
