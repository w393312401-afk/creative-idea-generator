"""项目改名的磁盘侧：rename_project_media / rekey_project_title。

改名是"库里的标题"与"盘上的目录"必须同时动的一件事——只动一边，这一单的帧/视频/
封面就在界面上凭空消失（_get_project_dir 按新名字找到的是空目录）。所以这里守的是：

- 目录整体搬走，内容一个不少；
- 目录里名字带旧项目名/旧主题的文件（拼图、合并成片）跟着改，且改出来的名字与
  下次重新生成时会用的名字一致，不留两份；
- 目录里所有 json 中写死的路径/URL 一并改写——前端就是照着它们取图的；
- 目标目录已存在时整件事都不做（宁可不改名，也不能把两单的资产混进一个目录）；
- run_<id>__ 前缀是那次 compose 的隔离命名空间，改名绝不能动它。
"""
import json
import os

import pytest

import server_common
from server_common import (
    rekey_project_title,
    rename_project_media,
    _safe_project_name,
)


@pytest.fixture
def outputs(tmp_path, monkeypatch):
    """把 OUTPUT_ROOT 指到临时目录（相对路径，与生产同形）。"""
    monkeypatch.chdir(tmp_path)
    os.makedirs('outputs', exist_ok=True)
    monkeypatch.setattr(server_common, 'OUTPUT_ROOT', 'outputs')
    return tmp_path / 'outputs'


OLD_KEY = 'run_import_1786251495795__悬崖巨石下海景卧室小屋：Veo 分段提示词'
NEW_TITLE = '悬崖巨石下海景卧室小屋建造'


def _make_project(outputs, key, *, with_media=True):
    name = _safe_project_name(key)
    pdir = outputs / name
    (pdir / 'frames').mkdir(parents=True)
    (pdir / 'frames' / 'img_001.webp').write_bytes(b'frame')
    (pdir / f'{name}_collage.jpg').write_bytes(b'collage')
    (pdir / '悬崖巨石下海景卧室小屋分段提示词_2x.mp4').write_bytes(b'video')
    (pdir / 'cover_1786251509045.webp').write_bytes(b'cover')
    manifest = {
        'title': '悬崖巨石下海景卧室小屋：Veo 分段提示词',
        'frames': [{'slot': 1,
                    'file': f'outputs/{name}/frames/img_001.webp',
                    'url': f'/outputs/{name}/frames/img_001.webp'}],
        'collage_url': f'/outputs/{name}/{name}_collage.jpg',
        'merged_video': {'url': f'/outputs/{name}/悬崖巨石下海景卧室小屋分段提示词_2x.mp4'},
    }
    (pdir / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf-8')
    # 删拍恢复快照也住在项目目录里，里面同样写着路径
    snap = pdir / '.deleted_slots' / 'snap1'
    snap.mkdir(parents=True)
    (snap / 'meta.json').write_text(
        json.dumps({'file': f'outputs/{name}/frames/img_009.webp'}, ensure_ascii=False),
        encoding='utf-8')
    return pdir


# ── project_key 改写 ──────────────────────────────────────────────────────

def test_rekey_keeps_the_run_prefix():
    assert rekey_project_title(OLD_KEY, NEW_TITLE) == f'run_import_1786251495795__{NEW_TITLE}'


def test_rekey_of_a_legacy_title_key_becomes_just_the_new_title():
    assert rekey_project_title('悬崖小屋', NEW_TITLE) == NEW_TITLE
    assert rekey_project_title('', NEW_TITLE) == NEW_TITLE


def test_rekey_of_blank_title_falls_back_to_a_placeholder():
    assert rekey_project_title(OLD_KEY, '   ') == 'run_import_1786251495795__未命名创意'


# ── 目录搬迁 ──────────────────────────────────────────────────────────────

def test_directory_moves_with_all_of_its_contents(outputs):
    _make_project(outputs, OLD_KEY)
    new_key = rekey_project_title(OLD_KEY, NEW_TITLE)

    result = rename_project_media(OLD_KEY, new_key, NEW_TITLE)

    assert result['moved'] is True
    new_dir = outputs / result['new_dir_name']
    assert not (outputs / result['old_dir_name']).exists()
    assert (new_dir / 'frames' / 'img_001.webp').read_bytes() == b'frame'
    assert (new_dir / 'cover_1786251509045.webp').exists()
    assert (new_dir / '.deleted_slots' / 'snap1' / 'meta.json').exists()


def test_collage_and_merged_video_are_renamed_to_match(outputs):
    _make_project(outputs, OLD_KEY)
    new_key = rekey_project_title(OLD_KEY, NEW_TITLE)

    result = rename_project_media(OLD_KEY, new_key, NEW_TITLE)

    new_dir = outputs / result['new_dir_name']
    assert (new_dir / f"{result['new_dir_name']}_collage.jpg").exists()
    # 合并成片的主干取新标题里的中文，与 video_generator 下次合并时的取法一致
    assert (new_dir / f'{NEW_TITLE}_2x.mp4').exists()
    assert result['file_map'] == {
        f"{result['old_dir_name']}_collage.jpg": f"{result['new_dir_name']}_collage.jpg",
        '悬崖巨石下海景卧室小屋分段提示词_2x.mp4': f'{NEW_TITLE}_2x.mp4',
    }


def test_json_paths_inside_are_rewritten(outputs):
    _make_project(outputs, OLD_KEY)
    new_key = rekey_project_title(OLD_KEY, NEW_TITLE)

    result = rename_project_media(OLD_KEY, new_key, NEW_TITLE)

    new_dir = outputs / result['new_dir_name']
    manifest = json.loads((new_dir / 'manifest.json').read_text(encoding='utf-8'))
    assert manifest['frames'][0]['url'] == f"/outputs/{result['new_dir_name']}/frames/img_001.webp"
    assert manifest['frames'][0]['file'] == f"outputs/{result['new_dir_name']}/frames/img_001.webp"
    assert manifest['collage_url'] == (
        f"/outputs/{result['new_dir_name']}/{result['new_dir_name']}_collage.jpg")
    assert manifest['merged_video']['url'] == (
        f"/outputs/{result['new_dir_name']}/{NEW_TITLE}_2x.mp4")
    assert result['old_dir_name'] not in json.dumps(manifest, ensure_ascii=False)
    # 删拍快照也要改：恢复时按的就是里面这条路径
    snap = json.loads((new_dir / '.deleted_slots' / 'snap1' / 'meta.json').read_text(encoding='utf-8'))
    assert snap['file'] == f"outputs/{result['new_dir_name']}/frames/img_009.webp"


def test_manifest_title_follows_the_new_name(outputs):
    """manifest.title 必须跟着改：合并成片的文件名就是从它抽中文推出来的
    （video_generator.merge_project_videos → _project_display_name），留着旧名字的话，
    改完名再合一次，成片又叫回旧名——而这一轮刚把旧名的成片改成新名，两边打架。

    （这条以前断言的是"不该改写"，理由是 title 属于正文而非路径。但它同时是文件名
    的来源，rename_project_media 自己的目标又写着"改完名的文件要与下次重新合并出来
    的同名"，两者不能同时成立。）"""
    _make_project(outputs, OLD_KEY)
    result = rename_project_media(OLD_KEY, rekey_project_title(OLD_KEY, NEW_TITLE), NEW_TITLE)

    manifest = json.loads((outputs / result['new_dir_name'] / 'manifest.json')
                          .read_text(encoding='utf-8'))
    assert manifest['title'] == NEW_TITLE
    # 只动 title 这一个字段，路径改写与其余正文照旧
    assert manifest['frames'][0]['file'].startswith(f"outputs/{result['new_dir_name']}/")


def test_manifest_title_is_left_alone_when_nothing_moved(outputs):
    """没有媒体目录可搬时，什么都不做（也没有 manifest 可写）。"""
    result = rename_project_media(OLD_KEY, rekey_project_title(OLD_KEY, NEW_TITLE), NEW_TITLE)
    assert result['moved'] is False and result['rewrite_failures'] == []


# ── 什么都不做的两种情况 ──────────────────────────────────────────────────

def test_project_without_any_media_reports_no_media_not_an_error(outputs):
    result = rename_project_media(OLD_KEY, rekey_project_title(OLD_KEY, NEW_TITLE), NEW_TITLE)

    assert result['moved'] is False
    assert result['reason'] == 'no_media'
    assert result['file_map'] == {}


def test_same_safe_dir_name_is_a_noop(outputs):
    """两个键落在同一个安全目录名上（只改了标点）时没有任何东西需要搬。"""
    key = 'run_1__小屋改造'
    _make_project(outputs, key)
    # '：' 会被 _safe_project_name 折成 '_'，与原名不同；这里构造真正同名的情形
    result = rename_project_media(key, key, '小屋改造')

    assert result['moved'] is False
    assert result['reason'] == 'same_dir'
    assert (outputs / _safe_project_name(key)).exists()


def test_existing_target_directory_aborts_the_whole_rename(outputs):
    """新名字下已经有另一单的资产：宁可不改名，也不能把两单混进一个目录。"""
    old_dir = _make_project(outputs, OLD_KEY)
    new_key = rekey_project_title(OLD_KEY, NEW_TITLE)
    (outputs / _safe_project_name(new_key)).mkdir()

    with pytest.raises(ValueError, match='已存在'):
        rename_project_media(OLD_KEY, new_key, NEW_TITLE)

    # 原目录纹丝不动
    assert (old_dir / 'frames' / 'img_001.webp').exists()
    assert (old_dir / 'manifest.json').exists()


def test_target_filename_collision_keeps_the_old_filename(outputs):
    """目录里已经有一个同名文件时不覆盖它——宁可留个旧名字，也不丢文件。"""
    pdir = _make_project(outputs, OLD_KEY)
    (pdir / f'{NEW_TITLE}_2x.mp4').write_bytes(b'other')

    result = rename_project_media(OLD_KEY, rekey_project_title(OLD_KEY, NEW_TITLE), NEW_TITLE)

    new_dir = outputs / result['new_dir_name']
    assert (new_dir / f'{NEW_TITLE}_2x.mp4').read_bytes() == b'other'
    assert (new_dir / '悬崖巨石下海景卧室小屋分段提示词_2x.mp4').read_bytes() == b'video'
    assert '悬崖巨石下海景卧室小屋分段提示词_2x.mp4' not in result['file_map']


def test_merged_video_stem_falls_back_when_the_new_title_has_no_chinese(outputs):
    _make_project(outputs, OLD_KEY)
    new_key = rekey_project_title(OLD_KEY, 'Cliff House Build')

    result = rename_project_media(OLD_KEY, new_key, 'Cliff House Build')

    new_dir = outputs / result['new_dir_name']
    assert (new_dir / f"{_safe_project_name('Cliff House Build')}_2x.mp4").exists()
