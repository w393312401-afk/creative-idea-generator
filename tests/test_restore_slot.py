"""整拍恢复接口 (/api/restore_slot、/api/deleted_slots) 的行为契约。

删除整拍会给其后每一格重新编号，所以「撤销」不是把那一格插回去，而是整单回滚：
当前编号整体后移一位、归档文件放回原位、manifest 与 prompt_block 还原成删除前
那一份。判据只有一条——**删除→恢复之后，磁盘与清单必须与删除前逐字节一致**。

背景：删除侧一直在写 .deleted_slots/<id>/ 快照，但读回那一半从来没有实现，
一次误删只能靠手写一次性脚本捞回来（tools/recover_ice_cave_slot3.py）。
"""
import io
import json
import os
from email.message import Message

import pytest
from PIL import Image

import server


@pytest.fixture(autouse=True)
def _no_access_gate(monkeypatch):
    monkeypatch.setattr(server, 'ACCESS_CODE', '', raising=False)


def _prompt_block(image_count=4, video_count=3, hero_at=None):
    lines = ['图片提示词']
    for i in range(1, image_count + 1):
        lines += [f'图片 {i}:', f'image prompt {i}', '']
    lines.append('视频提示词')
    for i in range(1, video_count + 1):
        tag = ' [HERO]' if hero_at == i else ''
        lines += [f'视频 {i}{tag}:', f'video prompt {i}', '']
    return '\n'.join(lines).strip()


@pytest.fixture
def project(tmp_path):
    """4 张图 + 3 段视频，每个文件内容各不相同，便于断言哪个文件回到了哪一格。"""
    project_dir = str(tmp_path / 'proj')
    frames_dir = os.path.join(project_dir, 'frames')
    videos_dir = os.path.join(project_dir, 'videos')
    fx_src_dir = os.path.join(frames_dir, 'fx_src')
    os.makedirs(fx_src_dir, exist_ok=True)
    os.makedirs(videos_dir, exist_ok=True)

    colors = {1: (255, 0, 0), 2: (0, 255, 0), 3: (0, 0, 255), 4: (255, 255, 0)}
    for seq, color in colors.items():
        Image.new('RGB', (90, 160), color=color).save(
            os.path.join(frames_dir, f'img_{seq:03d}.webp'), format='WEBP')
        # FX 路径给每帧留档的原始 jpg，删除/恢复时必须跟着一起改名
        with open(os.path.join(fx_src_dir, f'img_{seq:03d}_uuid{seq}.jpg'), 'wb') as f:
            f.write(f'FXSRC-{seq}'.encode('utf-8'))
    for slot in (1, 2, 3):
        with open(os.path.join(videos_dir, f'vid_{slot:03d}.mp4'), 'wb') as f:
            f.write(f'VIDEO-{slot}'.encode('utf-8'))

    manifest = {
        'title': 'restore_slot_test',
        'aspect_ratio': '9:16',
        'frames': [
            {'slot': s, 'sequence': s, 'file': f'frames/img_{s:03d}.webp',
             'url': f'/frames/img_{s:03d}.webp', 'prompt': f'image prompt {s}',
             'quality_gate': 'auto_approved'}
            for s in (1, 2, 3, 4)
        ],
        'videos': [
            {'slot': s, 'sequence': s, 'file': f'videos/vid_{s:03d}.mp4',
             'url': f'/videos/vid_{s:03d}.mp4', 'prompt': f'video prompt {s}',
             'status': 'success', 'anchor_check': f'ok-{s}'}
            for s in (1, 2, 3)
        ],
        'chain_drift': [{'family_anchor': 1, 'tail': 4, 'passed': True}],
        'merged_video': {'file': 'outputs/x/merged.mp4', 'status': 'success'},
    }
    with open(os.path.join(project_dir, 'manifest.json'), 'w', encoding='utf-8') as f:
        json.dump(manifest, f)

    return {'dir': project_dir, 'frames_dir': frames_dir, 'videos_dir': videos_dir,
            'fx_src_dir': fx_src_dir, 'colors': colors}


def _post(path, payload):
    body = json.dumps(payload).encode('utf-8')
    h = object.__new__(server.SparkRequestHandler)
    h.path = path
    headers = Message()
    headers['Content-Type'] = 'application/json'
    headers['Content-Length'] = str(len(body))
    h.headers = headers
    h.rfile = io.BytesIO(body)
    sent = []
    h._send_json = lambda obj, status=200: sent.append((obj, status))
    server.SparkRequestHandler.do_POST(h)
    return sent[0]


def _get(path):
    h = object.__new__(server.SparkRequestHandler)
    h.path = path
    h.headers = Message()
    sent = []
    h._send_json = lambda obj, status=200: sent.append((obj, status))
    server.SparkRequestHandler.do_GET(h)
    return sent[0]


def _snapshot_disk(project):
    """整单当前状态的可比较快照：文件名 → 内容，外加 manifest。"""
    state = {'manifest': _manifest(project), 'files': {}}
    for d, prefix in ((project['frames_dir'], 'frames'),
                      (project['videos_dir'], 'videos'),
                      (project['fx_src_dir'], 'fx_src')):
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            fp = os.path.join(d, name)
            if os.path.isfile(fp):
                with open(fp, 'rb') as f:
                    state['files'][f'{prefix}/{name}'] = f.read()
    return state


def _manifest(project):
    with open(os.path.join(project['dir'], 'manifest.json'), encoding='utf-8') as f:
        return json.load(f)


def _delete(project, sequence, prompt_block):
    return _post('/api/delete_slot', {'title': 'restore_slot_test',
                                      'sequence': sequence,
                                      'prompt_block': prompt_block})


def _restore(project, snapshot_id, **extra):
    payload = {'title': 'restore_slot_test', 'snapshot_id': snapshot_id}
    payload.update(extra)
    return _post('/api/restore_slot', payload)


def _snapshot_id(delete_body):
    return os.path.basename(delete_body['recovery_snapshot'])


@pytest.fixture(autouse=True)
def _project_dir(project, monkeypatch):
    monkeypatch.setattr(server, '_get_project_dir', lambda title: project['dir'])


class TestRoundTrip:
    """删除 → 恢复必须回到逐字节相同的状态。"""

    @pytest.mark.parametrize('sequence', [1, 2, 3, 4])
    def test_delete_then_restore_is_byte_identical(self, project, sequence):
        prompt = _prompt_block()
        before = _snapshot_disk(project)

        body, status = _delete(project, sequence, prompt)
        assert status == 200, body
        assert _snapshot_disk(project) != before, "删除本身必须真的改变了状态"

        rbody, rstatus = _restore(project, _snapshot_id(body))
        assert rstatus == 200, rbody

        after = _snapshot_disk(project)
        assert after['files'] == before['files'], (
            f"删第 {sequence} 拍再恢复后，磁盘文件应逐字节还原；"
            f"差异: {set(after['files']) ^ set(before['files'])}")
        assert after['manifest'] == before['manifest'], "清单应还原成删除前那一份"
        assert rbody['prompt_block'] == prompt, "提示词块应还原成删除前那一份"
        assert rbody['image_count'] == 4

    def test_restore_of_last_beat_recovers_the_dropped_tail_video(self, project):
        """删最后一拍时，跨过删除点的那一段视频（VID 003）会因为超出
        "视频数=图片数-1" 被一并删除。它此前不在快照里，那种情况下的删除
        实际不可逆——现在归档覆盖全部被物理删除的文件。"""
        prompt = _prompt_block()
        body, status = _delete(project, 4, prompt)
        assert status == 200, body
        assert not os.path.exists(os.path.join(project['videos_dir'], 'vid_003.mp4'))

        snapshot = body['recovery_snapshot']
        assert os.path.isfile(os.path.join(snapshot, 'vid_003.mp4')), \
            "被前移挤掉的尾段视频也必须进快照，否则删最后一拍不可逆"

        rbody, rstatus = _restore(project, _snapshot_id(body))
        assert rstatus == 200, rbody
        with open(os.path.join(project['videos_dir'], 'vid_003.mp4'), 'rb') as f:
            assert f.read() == b'VIDEO-3'

    def test_restore_with_hero_video(self, project):
        """英雄展示段槽位号恒等于最后一张图，删除时会被改挂到新的最后一张图上；
        恢复必须把它挂回原位。"""
        prompt = _prompt_block(image_count=4, video_count=4, hero_at=4)
        with open(os.path.join(project['videos_dir'], 'vid_004.mp4'), 'wb') as f:
            f.write(b'VIDEO-HERO')
        mdata = _manifest(project)
        mdata['videos'].append({'slot': 4, 'sequence': 4, 'file': 'videos/vid_004.mp4',
                                'url': '/videos/vid_004.mp4', 'prompt': 'video prompt 4',
                                'status': 'success', 'meta': 'HERO'})
        with open(os.path.join(project['dir'], 'manifest.json'), 'w', encoding='utf-8') as f:
            json.dump(mdata, f)

        before = _snapshot_disk(project)
        body, status = _delete(project, 2, prompt)
        assert status == 200, body
        with open(os.path.join(project['videos_dir'], 'vid_003.mp4'), 'rb') as f:
            assert f.read() == b'VIDEO-HERO', "英雄段应已改挂到新的最后一张图"

        rbody, rstatus = _restore(project, _snapshot_id(body))
        assert rstatus == 200, rbody
        assert _snapshot_disk(project)['files'] == before['files']
        assert rbody['prompt_block'] == prompt


class TestGuards:
    def test_second_restore_of_the_same_snapshot_is_refused(self, project):
        """同一份快照恢复两次的换号前提已经不成立，会把好好的一单推乱。"""
        body, _ = _delete(project, 2, _prompt_block())
        sid = _snapshot_id(body)
        assert _restore(project, sid)[1] == 200

        second, status = _restore(project, sid)
        assert status == 409
        assert '已经恢复过' in second['error']

    def test_diverged_run_needs_explicit_confirmation(self, project):
        """删除之后这单又被改动过时，恢复会丢掉新记录——必须先问过用户。"""
        body, _ = _delete(project, 2, _prompt_block())
        sid = _snapshot_id(body)

        mdata = _manifest(project)
        mdata['frames'][0]['quality_gate'] = 'sequence_review_flagged'
        with open(os.path.join(project['dir'], 'manifest.json'), 'w', encoding='utf-8') as f:
            json.dump(mdata, f)

        blocked, status = _restore(project, sid)
        assert status == 409
        assert blocked['status'] == 'diverged'

        forced, fstatus = _restore(project, sid, force=True)
        assert fstatus == 200, forced
        assert forced['forced'] is True

    def test_unrelated_manifest_churn_does_not_count_as_divergence(self, project):
        """指纹只看"这单有哪些内容"，不看每次写 manifest 都会变的耗时/计数字段，
        否则撤销永远会被判成"已被改动过"。"""
        body, _ = _delete(project, 2, _prompt_block())
        mdata = _manifest(project)
        mdata['frames'][0]['render_ms'] = 1234
        mdata['generated_at'] = '2026-07-27T00:00:00'
        with open(os.path.join(project['dir'], 'manifest.json'), 'w', encoding='utf-8') as f:
            json.dump(mdata, f)

        rbody, status = _restore(project, _snapshot_id(body))
        assert status == 200, rbody
        assert rbody['forced'] is False

    @pytest.mark.parametrize('bad', ['../../etc', 'x/../../y', '20260101_000000_1_slot_1',
                                     '', 'not_a_snapshot'])
    def test_snapshot_id_must_match_the_archive_shape(self, project, bad):
        """snapshot_id 直接来自请求体又要拼进路径，形状不对一律拒绝。"""
        body, status = _restore(project, bad)
        assert status == 400
        assert 'snapshot_id' in body['error']

    def test_missing_snapshot_is_404(self, project):
        body, status = _restore(project, '20260101_000000_000000_slot_002')
        assert status == 404

    def test_restore_point_is_saved_before_rolling_back(self, project):
        """恢复本身也要可回头：被覆盖的 manifest 与提示词块先存一份。"""
        body, _ = _delete(project, 2, _prompt_block())
        after_delete_manifest = _manifest(project)

        rbody, status = _restore(project, _snapshot_id(body),
                                 prompt_block='当前提示词块')
        assert status == 200, rbody
        point = rbody['restore_point']
        assert os.path.isdir(point)
        with open(os.path.join(point, 'manifest.before.json'), encoding='utf-8') as f:
            assert json.load(f) == after_delete_manifest
        with open(os.path.join(point, 'prompt_block.before.txt'), encoding='utf-8') as f:
            assert f.read() == '当前提示词块'


class TestListing:
    def test_lists_snapshots_newest_first_with_restorability(self, project):
        prompt = _prompt_block()
        first, _ = _delete(project, 4, prompt)
        second_prompt = first[0]['prompt_block'] if isinstance(first, tuple) else first['prompt_block']
        _delete(project, 1, second_prompt)

        body, status = _get('/api/deleted_slots?title=restore_slot_test')
        assert status == 200, body
        snaps = body['snapshots']
        assert len(snaps) == 2
        assert snaps[0]['id'] > snaps[1]['id'], "最新的排在最前"
        assert snaps[0]['sequence'] == 1
        assert snaps[0]['diverged'] is False, "最近一次删除后没动过，应可直接撤销"
        assert snaps[1]['diverged'] is True, "更早那次的前提已被后一次删除推翻"
        assert snaps[1]['image_prompt'] == 'image prompt 4'
        assert all(s['restored_at'] is None for s in snaps)

    def test_restored_snapshots_are_marked(self, project):
        body, _ = _delete(project, 2, _prompt_block())
        _restore(project, _snapshot_id(body))

        listed, status = _get('/api/deleted_slots?title=restore_slot_test')
        assert status == 200, listed
        assert listed['snapshots'][0]['restored_at'] is not None

    def test_restore_points_are_not_listed_as_undoable_deletes(self, project):
        body, _ = _delete(project, 2, _prompt_block())
        _restore(project, _snapshot_id(body))
        listed, _ = _get('/api/deleted_slots?title=restore_slot_test')
        assert len(listed['snapshots']) == 1, "恢复自己留的底不是可撤销的删除"
