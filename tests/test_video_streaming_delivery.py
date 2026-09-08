# -*- coding: utf-8 -*-
"""
验证批量视频生成中的流式即时交付（做好一个立即下载并上报）与单任务超时收敛机制。
"""
import time
import pytest
from unittest.mock import MagicMock, patch

from integrations.google_fx.models import VideoRequest
from integrations.google_fx.services.google_fx_video import _ChunkRunner


class FakePage:
    def __init__(self):
        self.evaluate_calls = []

    def evaluate(self, script, *args):
        self.evaluate_calls.append(script)
        # 模拟 download_video_via_browser 里的 base64 返回
        import base64
        return base64.b64encode(b"fake_video_bytes").decode("utf-8")


def test_submission_snapshot_delivers_once_before_batch_wait(monkeypatch, tmp_path):
    req = VideoRequest(prompt='one', output_path=str(tmp_path))
    notifications = []
    runner = _ChunkRunner(2, 0, [req, req], {},
                          lambda *args: notifications.append(args), None)
    monkeypatch.setattr(
        'integrations.google_fx.services.google_fx_video.download_video_via_browser',
        lambda *args: str(tmp_path / 'one.mp4'))
    submitted = [dict(sub_idx=0, idx=0, req=req, tile_id='one', status='generating')]
    states = {'one': {'status': 'done', 'videoSrc': 'https://flow-content.google/video/one'},
              'two': {'status': 'failed', 'isCreditExhausted': True}}
    runner._deliver_ready_tasks(FakePage(), submitted, states)
    runner._deliver_ready_tasks(FakePage(), submitted, states)
    assert [n[1] for n in notifications] == ['video_done']
    assert runner.completed == {0}


def test_retry_recognizes_submitted_tile_without_prompt_or_reference_images(monkeypatch):
    req = VideoRequest(prompt='long original prompt')
    runner = _ChunkRunner(1, 0, [req], {}, None, None)
    runner.project_url = 'project-a'
    runner.gen_retry_used = 1
    runner._submitted_tiles[('project-a', 0)] = 'known'
    monkeypatch.setattr(
        'integrations.google_fx.services.google_fx_video._scan_canvas_tiles',
        lambda page: [dict(tileId='known', textClean='playcircle360p', imageUuids=[],
                           videoSrc='https://flow-content.google/video/result', failed=False)])
    adopted, remaining = runner._adopt_completed_tiles(FakePage(), [(0, req)])
    assert len(adopted) == 1 and not remaining
    runner.project_url = 'project-b'
    assert runner._adopt_completed_tiles(FakePage(), [(0, req)])[0] == []


def test_await_generation_delivers_done_task_immediately(monkeypatch, tmp_path):
    """
    当任务2先完成（done）时，_await_generation 必须在轮询中立即就地下载并触发 video_done 上报，
    绝不能等任务1完成才统一交付。
    """
    downloaded = []

    def fake_download(page, url, output_dir, prefix):
        p = str(tmp_path / f"{prefix}.mp4")
        downloaded.append((prefix, url, p))
        return p

    monkeypatch.setattr(
        "integrations.google_fx.services.google_fx_video.download_video_via_browser",
        fake_download,
    )

    notifications = []

    def fake_notify(idx, stage, payload):
        notifications.append((idx, stage, payload))
        return None

    req1 = VideoRequest(prompt="prompt 1", output_path=str(tmp_path))
    req2 = VideoRequest(prompt="prompt 2", output_path=str(tmp_path))

    runner = _ChunkRunner(
        total_reqs=2,
        chunk_start=0,
        chunk=[req1, req2],
        all_slices={0: "p1", 1: "p2"},
        on_progress=fake_notify,
        cancel_check=None,
    )

    submitted = [
        {
            "sub_idx": 0,
            "idx": 0,
            "req": req1,
            "tile_id": "tile_1",
            "click_time": time.time(),
            "status": "generating",
            "video_url": None,
        },
        {
            "sub_idx": 1,
            "idx": 1,
            "req": req2,
            "tile_id": "tile_2",
            "click_time": time.time(),
            "status": "generating",
            "video_url": None,
        },
    ]

    # 模拟轮询状态序列：
    # 第1次轮询：tile_2 完成，tile_1 仍在 generating；
    # 第2次轮询：tile_1 完成。
    poll_round = [0]

    def fake_inspect(page, tile_ids, prompts_map=None, slices_map=None, *args, **kwargs):
        poll_round[0] += 1
        if poll_round[0] == 1:
            return {
                "tile_1": {"status": "generating", "progress": 30},
                "tile_2": {"status": "done", "videoSrc": "https://flow.google/video/2.mp4"},
            }
        else:
            return {
                "tile_1": {"status": "done", "videoSrc": "https://flow.google/video/1.mp4"},
            }

    monkeypatch.setattr(
        "integrations.google_fx.services.google_fx_video._inspect_all_pending_tiles",
        fake_inspect,
    )
    monkeypatch.setattr("time.sleep", lambda s: None)

    fake_page = FakePage()
    runner._await_generation(fake_page, submitted)

    # 验证两次下载均发生
    assert len(downloaded) == 2
    # 验证任务2在第1轮就已交付（先于任务1交付）
    assert downloaded[0][0] == "veo3_1"  # sub_idx 1 (任务2)
    assert downloaded[1][0] == "veo3_0"  # sub_idx 0 (任务1)

    # 验证上报通知包含 video_done
    stages = [n[1] for n in notifications]
    assert stages == ["video_done", "video_done"]
    assert notifications[0][0] == 1  # 任务2先上报
    assert notifications[1][0] == 0  # 任务1后上报

    # 验证最终全部记录进 completed
    assert runner.completed == {0, 1}


def test_await_generation_per_task_timeout_releases_batch(monkeypatch, tmp_path):
    """
    当任务1异常卡死且已超时（task_wait > per_task_timeout），而任务2早已完成时，
    任务1必须被判定为单任务超时失败并立即上报，使批次快速收敛，不再死等整批超时。
    """
    downloaded = []

    def fake_download(page, url, output_dir, prefix):
        p = str(tmp_path / f"{prefix}.mp4")
        downloaded.append((prefix, url, p))
        return p

    monkeypatch.setattr(
        "integrations.google_fx.services.google_fx_video.download_video_via_browser",
        fake_download,
    )

    notifications = []

    def fake_notify(idx, stage, payload):
        notifications.append((idx, stage, payload))
        return None

    req1 = VideoRequest(prompt="prompt 1", output_path=str(tmp_path))
    req2 = VideoRequest(prompt="prompt 2", output_path=str(tmp_path))

    runner = _ChunkRunner(
        total_reqs=2,
        chunk_start=0,
        chunk=[req1, req2],
        all_slices={0: "p1", 1: "p2"},
        on_progress=fake_notify,
        cancel_check=None,
    )

    now = time.time()
    submitted = [
        {
            "sub_idx": 0,
            "idx": 0,
            "req": req1,
            "tile_id": "tile_stuck",
            "click_time": now - 400,  # 已经等待了400秒，超过300秒
            "status": "generating",
            "video_url": None,
        },
        {
            "sub_idx": 1,
            "idx": 1,
            "req": req2,
            "tile_id": "tile_done",
            "click_time": now,
            "status": "generating",
            "video_url": None,
        },
    ]

    def fake_inspect(page, tile_ids, prompts_map=None, slices_map=None, *args, **kwargs):
        return {
            "tile_stuck": {"status": "generating", "progress": 10},
            "tile_done": {"status": "done", "videoSrc": "https://flow.google/video/done.mp4"},
        }

    monkeypatch.setattr(
        "integrations.google_fx.services.google_fx_video._inspect_all_pending_tiles",
        fake_inspect,
    )
    monkeypatch.setattr("time.sleep", lambda s: None)

    fake_page = FakePage()
    runner._await_generation(fake_page, submitted)

    # 任务2应成功下载，任务1因单任务超时被标记失败
    assert len(downloaded) == 1
    assert downloaded[0][0] == "veo3_1"

    # 检查通知：任务2 video_done，任务1 video_error
    done_notifications = [n for n in notifications if n[1] == "video_done"]
    error_notifications = [n for n in notifications if n[1] == "video_error"]

    assert len(done_notifications) == 1
    assert done_notifications[0][0] == 1

    assert len(error_notifications) == 1
    assert error_notifications[0][0] == 0
    assert "单任务生成超时" in error_notifications[0][2]["message"]

    # 批次应正常收尾，任务2完成，任务1未完成
    assert runner.completed == {1}
    assert runner.results[0]["status"] == "failed"
    assert runner.results[1]["status"] == "success"


def test_distinct_slices_handles_long_boilerplate():
    """验证长公共前缀不能被当成身份；切片必须能区分不同段落。"""
    from integrations.google_fx.services.google_fx_helpers import _distinct_slices
    boilerplate = (
        "Use the provided first frame and last frame as exact composition anchors. "
        "Use IMAGE 1 as the actual first-frame image and IMAGE 2 as the actual last-frame image; "
        "every visible action must interpolate between those two frame images without inventing a third layout. "
    )
    p1 = boilerplate + "Craftsman sweeps trimmer head in wide low arcs against brush."
    p2 = boilerplate + "Craftsman drives wrecking bars behind weathered fence rails."
    prompts_map = {0: p1, 1: p2}
    slices = _distinct_slices(prompts_map)
    assert 0 in slices and 1 in slices
    # 切片非空且互相排斥；DOM 截断时由项目资产身份恢复。
    assert len(slices[0]) >= 20
    assert slices[0] != slices[1]
    from integrations.google_fx.services.google_fx_helpers import _clean_alnum
    assert slices[0] not in _clean_alnum(p2)
    assert slices[1] not in _clean_alnum(p1)


def test_adopt_completed_tiles_uuid_anchoring(monkeypatch):
    """
    验证重试轮认领画布上已完成视频时，即使提示词带有超长公共模板，
    只要首尾帧画布 UUID 匹配，即可通过 UUID 双向锚定 100% 精准认领并交付。
    """
    from integrations.google_fx.services.google_fx_video import _ChunkRunner, VideoRequest

    req1 = VideoRequest(
        prompt="Use the provided first frame... slot 1 action",
        image="img_001.webp",
        end_image="img_002.webp",
        image_uuid="13408d9d-5fbf-4531-8823-8baf9cccde76",
        end_image_uuid="363878e9-216a-4c49-9411-10f09f95cc66",
    )
    req2 = VideoRequest(
        prompt="Use the provided first frame... slot 2 action",
        image="img_002.webp",
        end_image="img_003.webp",
        image_uuid="363878e9-216a-4c49-9411-10f09f95cc66",
        end_image_uuid="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    )

    runner = _ChunkRunner(
        total_reqs=2,
        chunk_start=0,
        chunk=[req1, req2],
        all_slices={0: "p1", 1: "p2"},
        on_progress=lambda *args: None,
        cancel_check=None,
    )
    # 模拟为重试轮
    runner.ip_retry = 1

    # 画布上扫描到了 Slot 1 的已生成视频卡片，带有首尾帧 UUID 和 videoSrc
    fake_canvas_tiles = [
        {
            "tileId": "tile_slot_1_done",
            "textClean": "usetheprovidedfirstframe",
            "videoSrc": "https://flow-content.google/video/slot1_done.mp4",
            "failed": False,
            "imageUuids": [
                "13408d9d-5fbf-4531-8823-8baf9cccde76",
                "363878e9-216a-4c49-9411-10f09f95cc66",
            ],
        }
    ]

    monkeypatch.setattr(
        "integrations.google_fx.services.google_fx_video._scan_canvas_tiles",
        lambda page: fake_canvas_tiles,
    )

    fake_page = FakePage()
    adopted, remaining = runner._adopt_completed_tiles(fake_page, [(0, req1), (1, req2)])

    # 任务 1 必须成功认领，任务 2 保持 remaining
    assert len(adopted) == 1
    assert adopted[0]["sub_idx"] == 0
    assert adopted[0]["video_url"] == "https://flow-content.google/video/slot1_done.mp4"
    assert adopted[0]["tile_id"] == "tile_slot_1_done"

    assert len(remaining) == 1
    assert remaining[0][0] == 1
