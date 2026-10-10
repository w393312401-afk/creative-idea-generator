"""手动编辑提示词集接口 (/api/edit_prompts) 的行为契约。

这条路径只做三件事：校验槽位契约、把提示词改过但画面没跟上的帧/视频标脏
(prompt_dirty)、回一份权威 prompt_slots。它**不**动磁盘文件、**不**重排用户的
原文，也**不**负责删拍——删拍要连文件、编号与恢复快照一起处理，那是
/api/delete_slot 的活，这里遇到拍数变少一律拒绝。
"""
import io
import json
import os
from email.message import Message

import pytest

import server


@pytest.fixture(autouse=True)
def _no_access_gate(monkeypatch):
    monkeypatch.setattr(server, 'ACCESS_CODE', '', raising=False)


def _prompt_block(image_count=4, video_count=3, hero_at=None, bodies=None):
    bodies = bodies or {}
    lines = ['图片提示词']
    for i in range(1, image_count + 1):
        lines += [f'图片 {i}:', bodies.get(('image', i), f'image prompt {i}'), '']
    lines.append('视频提示词')
    for i in range(1, video_count + 1):
        tag = ' [HERO]' if hero_at == i else ''
        lines += [f'视频 {i}{tag}:', bodies.get(('video', i), f'video prompt {i}'), '']
    return '\n'.join(lines).strip()


@pytest.fixture
def project(tmp_path):
    """4 张图 + 3 段视频的既成事实（只需要 manifest，本接口不碰媒体文件）。"""
    project_dir = str(tmp_path / 'proj')
    os.makedirs(project_dir, exist_ok=True)
    manifest = {
        'title': 'edit_prompts_test',
        'frames': [
            {'slot': s, 'sequence': s, 'file': f'frames/img_{s:03d}.webp',
             'prompt': f'image prompt {s}', 'quality_gate': 'auto_approved'}
            for s in (1, 2, 3, 4)
        ],
        'videos': [
            {'slot': s, 'sequence': s, 'file': f'videos/vid_{s:03d}.mp4',
             'prompt': f'video prompt {s}', 'status': 'success'}
            for s in (1, 2, 3)
        ],
        'merged_video': {'file': 'outputs/x/merged.mp4', 'status': 'success'},
    }
    with open(os.path.join(project_dir, 'manifest.json'), 'w', encoding='utf-8') as f:
        json.dump(manifest, f)
    return {'dir': project_dir}


def _post(payload):
    body = json.dumps(payload).encode('utf-8')
    h = object.__new__(server.SparkRequestHandler)
    h.path = '/api/edit_prompts'
    headers = Message()
    headers['Content-Type'] = 'application/json'
    headers['Content-Length'] = str(len(body))
    h.headers = headers
    h.rfile = io.BytesIO(body)
    sent = []
    h._send_json = lambda obj, status=200: sent.append((obj, status))
    server.SparkRequestHandler.do_POST(h)
    return sent[0]


def _manifest(project):
    with open(os.path.join(project['dir'], 'manifest.json'), encoding='utf-8') as f:
        return json.load(f)


@pytest.fixture
def bound(project, monkeypatch):
    monkeypatch.setattr(server, '_get_project_dir', lambda title: project['dir'])
    return project


class TestSlotContract:
    """槽位号是契约不是标签：图片必须从 1 连续编到 N，视频只能落在 1..N。"""

    def test_hole_in_image_numbering_is_rejected(self, bound):
        block = _prompt_block().replace('图片 3:', '图片 5:')
        body, status = _post({'title': 't', 'prompt_block': block,
                              'prev_prompt_block': _prompt_block()})
        assert status == 400
        assert '连续' in body['error']

    def test_block_without_image_slots_is_rejected(self, bound):
        body, status = _post({'title': 't', 'prompt_block': '随便写点什么，没有槽位'})
        assert status == 400
        assert '图片 N' in body['error']

    def test_video_slot_out_of_range_is_rejected(self, bound):
        block = _prompt_block() + '\n\n视频 9:\nstray video\n'
        body, status = _post({'title': 't', 'prompt_block': block,
                              'prev_prompt_block': _prompt_block()})
        assert status == 400
        assert '视频 9' in body['error']

    def test_dropping_a_beat_is_refused_and_points_at_delete_slot(self, bound):
        """手动编辑不负责删拍——它不会动磁盘文件，也不写恢复快照。"""
        body, status = _post({'title': 't',
                              'prompt_block': _prompt_block(image_count=3, video_count=2),
                              'prev_prompt_block': _prompt_block()})
        assert status == 409
        assert body['status'] == 'rejected'
        assert '删除' in body['error']

    def test_shrinking_below_frames_already_on_disk_is_refused(self, bound):
        """连 prev 都没带（旧客户端）时，磁盘上的帧数就是那道下界。"""
        body, status = _post({'title': 't',
                              'prompt_block': _prompt_block(image_count=2, video_count=1)})
        assert status == 409
        assert body['refresh_required'] is True
        assert _manifest(bound)['frames'][0].get('prompt_dirty') is None, '拒绝的请求不许改 manifest'


class TestEditMarksChangedSlotsDirty:
    def test_changed_image_prompt_marks_only_that_frame(self, bound):
        new_block = _prompt_block(bodies={('image', 2): 'image prompt 2 —— 改成别的样子'})
        body, status = _post({'title': 't', 'prompt_block': new_block,
                              'prev_prompt_block': _prompt_block()})
        assert status == 200, body
        assert body['changed_images'] == [2]
        assert body['dirty_frames'] == [2]
        frames = {f['sequence']: f for f in _manifest(bound)['frames']}
        assert frames[2]['prompt_dirty'] is True
        assert frames[2]['prompt_dirty_at']
        assert all(frames[s].get('prompt_dirty') is None for s in (1, 3, 4))

    def test_changed_video_prompt_marks_that_slot(self, bound):
        new_block = _prompt_block(bodies={('video', 3): 'video prompt 3 —— 换个运镜'})
        body, status = _post({'title': 't', 'prompt_block': new_block,
                              'prev_prompt_block': _prompt_block()})
        assert status == 200, body
        assert body['dirty_videos'] == [3]
        videos = {v['slot']: v for v in _manifest(bound)['videos']}
        assert videos[3]['prompt_dirty'] is True
        assert videos[1].get('prompt_dirty') is None

    def test_meta_only_change_counts_as_changed(self, bound):
        """[BRIDGE] 这类标注决定的是渲染契约本身，改了它画面同样不再对得上。"""
        new_block = _prompt_block().replace('图片 3:', '图片 3 [BRIDGE]:')
        body, status = _post({'title': 't', 'prompt_block': new_block,
                              'prev_prompt_block': _prompt_block()})
        assert status == 200, body
        assert body['changed_images'] == [3]

    def test_merged_video_is_dropped_when_something_changed(self, bound):
        """成片是按旧提示词渲的素材拼的，提示词一改它就不再代表这一单。"""
        new_block = _prompt_block(bodies={('image', 1): 'brand new opening'})
        body, status = _post({'title': 't', 'prompt_block': new_block,
                              'prev_prompt_block': _prompt_block()})
        assert status == 200, body
        assert 'merged_video' not in _manifest(bound)

    def test_untouched_edit_marks_nothing(self, bound):
        body, status = _post({'title': 't', 'prompt_block': _prompt_block(),
                              'prev_prompt_block': _prompt_block()})
        assert status == 200, body
        assert body['dirty_frames'] == [] and body['dirty_videos'] == []
        assert 'merged_video' in _manifest(bound), '什么都没改就别作废成片'


class TestAppendBeat:
    def test_appending_a_beat_is_allowed_and_touches_no_existing_frame(self, bound):
        new_block = _prompt_block(image_count=5, video_count=4)
        body, status = _post({'title': 't', 'prompt_block': new_block,
                              'prev_prompt_block': _prompt_block()})
        assert status == 200, body
        assert body['image_count'] == 5
        assert body['added_beats'] == [5]
        assert body['dirty_frames'] == [], '新增的拍没有画面，标脏无从谈起'
        assert [f['sequence'] for f in _manifest(bound)['frames']] == [1, 2, 3, 4], \
            '本接口不凭空造帧记录：新拍以空槽位出现在前端，等着真正生成'

    def test_new_slot_shows_up_in_prompt_slots_contract(self, bound):
        new_block = _prompt_block(image_count=5, video_count=4)
        body, status = _post({'title': 't', 'prompt_block': new_block,
                              'prev_prompt_block': _prompt_block()})
        assert status == 200, body
        assert [s['index'] for s in body['prompt_slots']['images']] == [1, 2, 3, 4, 5]
        assert [s['index'] for s in body['prompt_slots']['videos']] == [1, 2, 3, 4]


class TestUserTextIsPreservedVerbatim:
    def test_block_is_returned_byte_for_byte(self, bound):
        """手动编辑要的就是"我写成什么样，存下来就是什么样"——不重排、不补分节。"""
        raw = ('# 我自己加的一段备注\n\n'
               + _prompt_block()
               + '\n\n<!-- 收尾备注：这一单走 Veo 单镜延时 -->')
        body, status = _post({'title': 't', 'prompt_block': raw,
                              'prev_prompt_block': _prompt_block()})
        assert status == 200, body
        assert body['prompt_block'] == raw


class TestWithoutProjectDir:
    """刚合成完、还没渲过任何帧：项目目录不存在，改提示词照样要能存。"""

    def test_edit_works_before_any_frame_exists(self, tmp_path, monkeypatch):
        monkeypatch.setattr(server, '_get_project_dir',
                            lambda title: str(tmp_path / 'never_rendered'))
        body, status = _post({'title': 't',
                              'prompt_block': _prompt_block(image_count=5, video_count=4),
                              'prev_prompt_block': _prompt_block()})
        assert status == 200, body
        assert body['image_count'] == 5
        assert body['frames'] == [] and body['videos'] == []
