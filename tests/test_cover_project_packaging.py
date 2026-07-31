# -*- coding: utf-8 -*-
"""封面图跟项目打包在一起：outputs/<项目目录>/cover_*.webp。

封面以前落在全局池 outputs/covers/ 里，靠文件名前缀 `<安全标题>_cover_` 反查归属。
这组用例锁住新契约：

- 新封面写进项目目录，并且顺手把项目目录建出来（封面通常是项目的第一个产物）；
- 参考图的路径边界从 outputs/covers 放宽到整个 outputs/，但仍然只限 outputs/；
- 回落查找先看项目目录，再看历史封面池（迁移前的老数据不能失联）；
- 画廊里封面并进项目组，仍带 kind='cover' 与「在用」标注；
- 删项目 = 封面跟着走（rmtree 项目目录即可，不必再单独删一遍）;
- tools/migrate_covers.py 只搬认得出归属的，其余原样保留。
"""
import json
import os
import sys

import pytest

import server_common as sc
from server_common import (
    _get_project_dir,
    _is_cover_filename,
    delete_idea_output_files,
    gallery_collect_references,
    project_cover_path,
    resolve_cover_reference,
    scan_gallery,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _touch(path, mtime=None, content=b'x'):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(content)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


@pytest.fixture
def outputs(tmp_path, monkeypatch):
    """把 OUTPUT_ROOT 指到 tmp（绝对路径），与 test_anchor_from_cover 同款隔离。"""
    out = tmp_path / 'outputs'
    out.mkdir()
    monkeypatch.setattr(sc, 'OUTPUT_ROOT', str(out))
    monkeypatch.setattr(sc, '_PROJECT_KEY_CONTEXT', sc.threading.local())
    return str(out)


class TestCoverPath:
    def test_cover_lands_in_project_dir_and_creates_it(self, outputs):
        path = project_cover_path('run_123__废弃水塔改造')

        assert os.path.dirname(path) == _get_project_dir('run_123__废弃水塔改造')
        assert os.path.isdir(os.path.dirname(path))
        assert _is_cover_filename(os.path.basename(path))
        assert os.path.basename(path).startswith('cover_')
        assert path.endswith('.webp')
        # 不在全局封面池里
        assert os.sep + 'covers' + os.sep not in path

    def test_two_covers_of_one_project_are_siblings(self, outputs):
        first = project_cover_path('run_123__塔')
        _touch(first)
        second = project_cover_path('run_123__塔')
        assert os.path.dirname(first) == os.path.dirname(second)

    def test_is_cover_filename_only_matches_cover_images(self):
        assert _is_cover_filename('cover_1785382198077.webp')
        assert _is_cover_filename('cover_1.png')
        assert not _is_cover_filename('img_001.webp')       # 帧
        assert not _is_cover_filename('cover_1.mp4')        # 视频不是封面
        assert not _is_cover_filename('manifest.json')
        assert not _is_cover_filename(None)


class TestResolveCoverReference:
    def test_falls_back_to_newest_cover_in_project_dir(self, outputs):
        pdir = os.path.join(outputs, 'run_9__小屋')
        _touch(os.path.join(pdir, 'cover_100.webp'), mtime=1000)
        newest = _touch(os.path.join(pdir, 'cover_200.webp'), mtime=2000)

        assert resolve_cover_reference({}, 'run_9__小屋') == newest

    def test_project_key_argument_selects_the_namespace(self, outputs):
        pdir = os.path.join(outputs, 'run_9__小屋')
        cover = _touch(os.path.join(pdir, 'cover_100.webp'))
        # 显示标题撞不上目录名，project_key 才是磁盘命名空间
        assert resolve_cover_reference({}, '小屋', 'run_9__小屋') == cover

    def test_legacy_cover_pool_still_resolves(self, outputs):
        legacy = _touch(os.path.join(outputs, 'covers', '小屋_cover_1784.webp'))
        assert resolve_cover_reference({}, '小屋') == legacy

    def test_project_dir_cover_wins_over_older_pool_cover(self, outputs):
        _touch(os.path.join(outputs, 'covers', '小屋_cover_1784.webp'), mtime=1000)
        fresh = _touch(os.path.join(outputs, '小屋', 'cover_2000.webp'), mtime=2000)
        assert resolve_cover_reference({}, '小屋') == fresh

    def test_named_cover_inside_project_dir_is_accepted(self, outputs):
        cover = _touch(os.path.join(outputs, 'run_9__小屋', 'cover_100.webp'))
        config = {'coverReferencePath': cover}
        # 老实现把请求路径限制在 outputs/covers 下，项目目录里的封面会被拒
        assert resolve_cover_reference(config, 'run_9__小屋') == cover

    def test_named_cover_outside_outputs_is_rejected(self, outputs, tmp_path):
        outside = _touch(str(tmp_path / 'stolen' / 'secret.webp'))
        _touch(os.path.join(outputs, 'run_9__小屋', 'cover_100.webp'))
        config = {'coverReferencePath': outside}
        resolved = resolve_cover_reference(config, 'run_9__小屋')
        assert resolved != outside
        assert os.path.basename(resolved) == 'cover_100.webp'

    def test_no_cover_anywhere_returns_none(self, outputs):
        assert resolve_cover_reference({}, '从未跑过的项目') is None

    def test_empty_cover_file_is_not_a_cover(self, outputs):
        _touch(os.path.join(outputs, 'run_9__小屋', 'cover_100.webp'), content=b'')
        assert resolve_cover_reference({}, 'run_9__小屋') is None


class TestGalleryGrouping:
    def _tree(self, base):
        out = os.path.join(base, 'outputs')
        proj = os.path.join(out, 'run_9__小屋')
        _touch(os.path.join(proj, 'cover_2000.webp'), mtime=2000)
        _touch(os.path.join(proj, 'frames', 'img_001.webp'), mtime=2100)
        _touch(os.path.join(proj, 'videos', 'vid_001.mp4'), mtime=2200)
        return out, proj

    def test_cover_is_inside_the_project_group(self, tmp_path):
        self._tree(str(tmp_path))
        data = scan_gallery(base_dir=str(tmp_path))

        assert [g['key'] for g in data['groups']] == ['run_9__小屋']   # 没有独立封面组
        kinds = {it['name']: it['kind'] for it in data['groups'][0]['items']}
        assert kinds == {
            'cover_2000.webp': 'cover',
            'img_001.webp': 'frame',
            'vid_001.mp4': 'video',
        }

    def test_cover_in_use_flag_follows_library_reference(self, tmp_path):
        self._tree(str(tmp_path))
        refs = gallery_collect_references(library_items=[{
            'id': '1', 'title': '小屋', 'project_key': 'run_9__小屋',
            'covers': ['/outputs/run_9__小屋/cover_2000.webp'],
        }], tasks=[])

        data = scan_gallery(base_dir=str(tmp_path), refs=refs)
        cover = next(it for it in data['groups'][0]['items'] if it['kind'] == 'cover')
        assert cover['in_use'] is True

    def test_unreferenced_project_cover_does_not_flag_in_use(self, tmp_path):
        self._tree(str(tmp_path))
        refs = gallery_collect_references(library_items=[], tasks=[])
        data = scan_gallery(base_dir=str(tmp_path), refs=refs)
        cover = next(it for it in data['groups'][0]['items'] if it['kind'] == 'cover')
        assert cover['in_use'] is False


class TestDeleteTakesCoverAlong:
    def test_removing_the_project_dir_removes_its_cover(self, outputs, monkeypatch):
        monkeypatch.chdir(os.path.dirname(outputs))
        cover = _touch(os.path.join(outputs, '小屋', 'cover_100.webp'))
        _touch(os.path.join(outputs, '小屋', 'frames', 'img_001.webp'))

        deleted = delete_idea_output_files('小屋')

        assert deleted['project_dir'] is not None
        assert not os.path.exists(cover)
        # 老布局要靠 covers 参数二次清理，新布局一次 rmtree 就干净了
        assert deleted['covers'] == []


@pytest.fixture
def migration_tree(tmp_path, monkeypatch):
    """一棵能跑 tools/migrate_covers.py 的最小仓库：outputs/ + library/ + tasks/。"""
    import tools.migrate_covers as mc

    base = tmp_path
    monkeypatch.chdir(base)
    monkeypatch.setattr(mc, 'ROOT', str(base))
    monkeypatch.setattr(sc, 'OUTPUT_ROOT', 'outputs')
    monkeypatch.setattr(sc, 'LIBRARY_DIR', 'library')
    monkeypatch.setattr(sc, 'TASKS_DIR', 'tasks')
    monkeypatch.setattr(sc, '_PROJECT_KEY_CONTEXT', sc.threading.local())

    _touch(str(base / 'outputs' / 'covers' / '小屋_cover_1000.webp'))
    _touch(str(base / 'outputs' / 'covers' / '孤儿封面_cover_2000.webp'))
    _touch(str(base / 'outputs' / 'run_9_小屋' / 'frames' / 'img_001.webp'))

    item = {
        'id': '9', 'title': '小屋', 'project_key': 'run_9__小屋',
        'covers': ['/outputs/covers/小屋_cover_1000.webp'],
        'activeCoverUrl': '/outputs/covers/小屋_cover_1000.webp',
        'frameRun': {'frames': [{'sequence': 1,
                                 'reference': 'outputs/covers/小屋_cover_1000.webp'}]},
    }
    os.makedirs(str(base / 'library' / 'items'), exist_ok=True)
    with open(str(base / 'library' / 'items' / '9.json'), 'w', encoding='utf-8') as f:
        json.dump(item, f, ensure_ascii=False)
    with open(str(base / 'library' / 'index.json'), 'w', encoding='utf-8') as f:
        json.dump([sc.library_index_entry(item)], f, ensure_ascii=False)

    os.makedirs(str(base / 'tasks' / 'results'), exist_ok=True)
    with open(str(base / 'tasks' / 'results' / '9.json'), 'w', encoding='utf-8') as f:
        json.dump({'title': '小屋', 'project_key': 'run_9__小屋',
                   'covers': ['/outputs/covers/小屋_cover_1000.webp']}, f, ensure_ascii=False)

    return mc, base


class TestMigrateCovers:
    def test_dry_run_changes_nothing(self, migration_tree, capsys):
        mc, base = migration_tree
        assert mc.main.__call__ is not None
        sys.argv = ['migrate_covers.py', '--dry-run']
        mc.main()
        assert (base / 'outputs' / 'covers' / '小屋_cover_1000.webp').exists()
        assert not list((base / 'outputs' / 'run_9_小屋').glob('cover_*'))

    def test_owned_cover_moves_and_references_are_rewritten(self, migration_tree):
        mc, base = migration_tree
        sys.argv = ['migrate_covers.py']
        mc.main()

        moved = list((base / 'outputs' / 'run_9_小屋').glob('cover_*.webp'))
        assert len(moved) == 1
        assert moved[0].name == 'cover_1000.webp'
        assert not (base / 'outputs' / 'covers' / '小屋_cover_1000.webp').exists()

        new_url = '/outputs/run_9_小屋/cover_1000.webp'
        item = json.load(open(str(base / 'library' / 'items' / '9.json'), encoding='utf-8'))
        assert item['covers'] == [new_url]
        assert item['activeCoverUrl'] == new_url
        # 前导 '/' 的写法各处不同，改写时必须原样保留
        assert item['frameRun']['frames'][0]['reference'] == new_url.lstrip('/')

        result = json.load(open(str(base / 'tasks' / 'results' / '9.json'), encoding='utf-8'))
        assert result['covers'] == [new_url]

    def test_orphan_cover_stays_in_the_pool(self, migration_tree):
        mc, base = migration_tree
        sys.argv = ['migrate_covers.py']
        mc.main()
        assert (base / 'outputs' / 'covers' / '孤儿封面_cover_2000.webp').exists()

    def test_migration_is_idempotent(self, migration_tree):
        mc, base = migration_tree
        sys.argv = ['migrate_covers.py']
        mc.main()
        mc.main()
        assert len(list((base / 'outputs' / 'run_9_小屋').glob('cover_*.webp'))) == 1
