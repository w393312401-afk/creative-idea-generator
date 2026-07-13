"""画廊后端（scan_gallery / gallery_delete_files）的行为契约。

关注点：
- 扫描只认媒体扩展名白名单，manifest/检查点等永远不进画廊；
- 插帧中间产物目录（项目根下 frames/videos 以外的子目录）不进画廊；
- 删除接口的安全边界：越界路径、非媒体文件、目录一律拒绝；
- 项目内媒体删空后整个项目目录被清理，否则上报 affected_project_dirs
  让调用方重同步 manifest。
"""
import os
import time

import pytest

from server_common import (
    scan_gallery,
    gallery_delete_files,
    gallery_collect_references,
    _safe_project_name,
    _legacy_ascii_project_name,
)


def _touch(path, mtime=None, content=b'x'):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(content)
    if mtime is not None:
        os.utime(path, (mtime, mtime))


@pytest.fixture
def media_tree(tmp_path):
    """base_dir/outputs/ 下搭一棵有代表性的媒体树。"""
    base = str(tmp_path)
    out = os.path.join(base, 'outputs')

    _touch(os.path.join(out, 'covers', 'cover_a.webp'), mtime=1000)
    _touch(os.path.join(out, 'covers', 'not_media.txt'), mtime=1000)
    _touch(os.path.join(out, 'image-station', 'img_1.webp'), mtime=5000)

    proj = os.path.join(out, '树屋项目')
    _touch(os.path.join(proj, 'frames', 'img_001.webp'), mtime=2000)
    _touch(os.path.join(proj, 'frames', 'img_002.webp'), mtime=2100)
    _touch(os.path.join(proj, 'videos', 'vid_001.mp4'), mtime=3000)
    _touch(os.path.join(proj, '树屋_2x.mp4'), mtime=4000)
    _touch(os.path.join(proj, 'manifest.json'), mtime=2000, content=b'{}')
    # 插帧中间产物目录：不应进画廊
    _touch(os.path.join(proj, '树屋_2x_frames', 'frame_0001.jpg'), mtime=4000)

    return base


def _group(data, key):
    hits = [g for g in data['groups'] if g['key'] == key]
    return hits[0] if hits else None


class TestScanGallery:
    def test_groups_and_kinds(self, media_tree):
        data = scan_gallery(base_dir=media_tree)

        covers = _group(data, 'covers')
        assert covers['kind'] == 'covers'
        assert [it['name'] for it in covers['items']] == ['cover_a.webp']

        studio = _group(data, 'image-station')
        assert [it['name'] for it in studio['items']] == ['img_1.webp']

        proj = _group(data, '树屋项目')
        assert proj['kind'] == 'project'
        kinds = {it['name']: it['kind'] for it in proj['items']}
        assert kinds == {
            'img_001.webp': 'frame',
            'img_002.webp': 'frame',
            'vid_001.mp4': 'video',
            '树屋_2x.mp4': 'merged',
        }
        # manifest.json 与插帧中间产物目录都不在画廊里
        names = set(kinds)
        assert 'manifest.json' not in names and 'frame_0001.jpg' not in names

    def test_item_shape_and_urls(self, media_tree):
        data = scan_gallery(base_dir=media_tree)
        it = _group(data, '树屋项目')['items'][0]
        assert it['path'].startswith('outputs/')
        assert it['url'] == '/' + it['path']
        assert it['type'] in ('image', 'video')
        assert it['size'] > 0 and it['mtime'] > 0

    def test_totals_and_group_order(self, media_tree):
        data = scan_gallery(base_dir=media_tree)
        assert data['totals']['images'] == 4   # cover + studio + 2 frames
        assert data['totals']['videos'] == 2   # vid_001 + 合成成片
        assert data['totals']['bytes'] > 0
        # 组内按 mtime 倒序，组间按最新活动倒序
        assert [g['key'] for g in data['groups']] == ['image-station', '树屋项目', 'covers']
        proj_names = [it['name'] for it in _group(data, '树屋项目')['items']]
        assert proj_names[0] == '树屋_2x.mp4'

    def test_missing_outputs_dir(self, tmp_path):
        data = scan_gallery(base_dir=str(tmp_path))
        assert data == {'groups': [], 'totals': {'images': 0, 'videos': 0, 'bytes': 0}}


class TestGalleryDelete:
    def test_delete_file_keeps_project_and_reports_affected(self, media_tree):
        res = gallery_delete_files(['outputs/树屋项目/frames/img_001.webp'], base_dir=media_tree)
        assert res['deleted'] == ['outputs/树屋项目/frames/img_001.webp']
        assert res['failed'] == []
        assert not os.path.exists(os.path.join(media_tree, 'outputs', '树屋项目', 'frames', 'img_001.webp'))
        # 项目里还有别的媒体：目录保留，并要求重同步 manifest
        assert res['affected_project_dirs'] == [os.path.join(media_tree, 'outputs', '树屋项目')]
        assert res['removed_project_dirs'] == []

    def test_delete_all_media_removes_project_dir(self, media_tree):
        # 只删画廊里列出的 4 个文件——插帧中间产物（树屋_2x_frames/*.jpg）
        # 不在画廊清单里，但不能因此拦住文件夹清理
        paths = [
            'outputs/树屋项目/frames/img_001.webp',
            'outputs/树屋项目/frames/img_002.webp',
            'outputs/树屋项目/videos/vid_001.mp4',
            'outputs/树屋项目/树屋_2x.mp4',
        ]
        res = gallery_delete_files(paths, base_dir=media_tree)
        assert len(res['deleted']) == 4 and res['failed'] == []
        # 画廊可见媒体清零 → 整个项目文件夹（含 manifest 与中间产物）被清掉
        assert not os.path.exists(os.path.join(media_tree, 'outputs', '树屋项目'))
        assert res['affected_project_dirs'] == []
        assert len(res['removed_project_dirs']) == 1

    def test_special_dirs_never_removed(self, media_tree):
        res = gallery_delete_files(['outputs/covers/cover_a.webp'], base_dir=media_tree)
        assert res['deleted'] == ['outputs/covers/cover_a.webp']
        # covers 不是项目：既不进 affected 也不会被整目录清理
        assert res['affected_project_dirs'] == [] and res['removed_project_dirs'] == []
        assert os.path.isdir(os.path.join(media_tree, 'outputs', 'covers'))

    def test_rejects_unsafe_and_non_media_paths(self, media_tree):
        _touch(os.path.join(media_tree, 'secret.webp'), mtime=1)
        res = gallery_delete_files([
            '../secret.webp',                      # 相对越界
            'outputs/../secret.webp',              # 归一化后越界
            'outputs/树屋项目/manifest.json',       # 非媒体白名单
            'outputs/树屋项目/frames',              # 目录
            'outputs/covers/ghost.webp',           # 不存在
            '',                                    # 空
            123,                                   # 非字符串
        ], base_dir=media_tree)
        assert res['deleted'] == []
        assert len(res['failed']) == 7
        assert os.path.exists(os.path.join(media_tree, 'secret.webp'))
        assert os.path.exists(os.path.join(media_tree, 'outputs', '树屋项目', 'manifest.json'))

    def test_accepts_backslash_and_leading_slash(self, media_tree):
        res = gallery_delete_files(
            ['/outputs\\covers\\cover_a.webp'], base_dir=media_tree)
        assert res['deleted'] == ['outputs/covers/cover_a.webp']


class TestGalleryReferences:
    def test_collect_from_library_and_tasks(self):
        lib = [{
            'title': '树屋项目',
            'english_title': 'tree house retreat',
            'covers': ['/outputs/covers/cover_a.webp'],
            'collage_url': '/outputs/covers/collage_b.webp',
            'frameRun': {
                'title': '树屋项目',
                'frames': [{'url': '/outputs/别名目录/frames/img_001.webp'}],
                'videos': [{'file': 'outputs/别名目录/videos/vid_001.mp4'}],
            },
        }]
        tasks = [
            # staged_render 任务：theme 就是项目标题
            {'dimensions': {'type': 'staged_render', 'theme': '飞机残骸小屋'}, 'result': None},
            {'dimensions': {}, 'result': {'title': '灯塔改造', 'covers': ['outputs/covers/cover_c.webp']}},
        ]
        refs = gallery_collect_references(library_items=lib, tasks=tasks)

        assert {'outputs/covers/cover_a.webp', 'outputs/covers/collage_b.webp',
                'outputs/covers/cover_c.webp'} <= refs['cover_paths']
        # 三代命名方案都收：新方案（中文直用）与 legacy ascii+hash
        assert _safe_project_name('树屋项目') in refs['project_names']
        assert _legacy_ascii_project_name('tree house retreat') in refs['project_names']
        assert _safe_project_name('飞机残骸小屋') in refs['project_names']
        assert _safe_project_name('灯塔改造') in refs['project_names']
        # frameRun 里的文件路径直接贡献项目目录名
        assert '别名目录' in refs['project_names']

    def test_ignores_external_and_malformed(self):
        refs = gallery_collect_references(library_items=[{
            'title': '',
            'covers': ['https://cdn.example.com/x.webp', 'data:image/webp;base64,xxx', None, 123],
            'frameRun': {'frames': [{'url': '/elsewhere/a.webp'}, 'not-a-dict']},
        }], tasks=['not-a-dict', None])
        assert refs['cover_paths'] == set()
        assert refs['project_names'] == set()

    def test_scan_annotates_in_use_and_orphan(self, media_tree):
        # 树屋项目无引用、mtime 是远古值（1000-4000）→ 超过宽限期 → 孤儿
        refs = {'cover_paths': {'outputs/covers/cover_a.webp'}, 'project_names': set()}
        data = scan_gallery(base_dir=media_tree, refs=refs)
        assert _group(data, '树屋项目')['orphan'] is True
        assert _group(data, 'covers')['items'][0]['in_use'] is True
        # 图像工坊不参与引用判定：不带任何标注字段
        studio_item = _group(data, 'image-station')['items'][0]
        assert 'in_use' not in studio_item

        # 被引用的项目 / 未被引用的封面
        refs2 = {'cover_paths': set(), 'project_names': {'树屋项目'}}
        data2 = scan_gallery(base_dir=media_tree, refs=refs2)
        assert _group(data2, '树屋项目')['orphan'] is False
        assert _group(data2, 'covers')['items'][0]['in_use'] is False

    def test_recent_activity_grace_prevents_orphan(self, media_tree):
        # 24h 内有产出的项目即使无引用也不标孤儿（可能正在生成中）
        now = time.time()
        p = os.path.join(media_tree, 'outputs', '树屋项目', 'frames', 'img_001.webp')
        os.utime(p, (now, now))
        data = scan_gallery(base_dir=media_tree,
                            refs={'cover_paths': set(), 'project_names': set()})
        assert _group(data, '树屋项目')['orphan'] is False

    def test_no_refs_means_no_annotation(self, media_tree):
        data = scan_gallery(base_dir=media_tree)
        assert 'orphan' not in _group(data, '树屋项目')
        assert 'in_use' not in _group(data, 'covers')['items'][0]
