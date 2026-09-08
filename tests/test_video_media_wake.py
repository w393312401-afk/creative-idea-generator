# -*- coding: utf-8 -*-
"""
新版 Flow 成片卡片的识别与「视频地址唤醒」。

2026-09-06 的整批事故：Flow 改版后，视频生成完成的卡片长这样——

    flow-video-tile > div.container
        img.thumbnail alt="Generated video thumbnail"
        div.pre-hover-overlay > mat-icon(play_circle) + span.resolution-badge("360p")

整张卡没有 <video>，页面里也搜不到任何 .../video/<uuid>——视频地址压根不在 DOM 里，
要交互一下 Flow 才会把 <video src> 挂上来。于是纯读 DOM 的轮询一路读不到东西，
每一段都哑到单任务 300s 超时判失败，而视频其实早就生成好、积分也早就扣了；
「做好一个就往前端交付」的流式规则自然也一次都没触发过（压根没有"做好"的时刻）。

这里钉两件事：
  1. 识别层能把「生成中 / 成片但没挂 video / 成片且挂了 video / 参考图」四种卡片分清；
  2. 轮询遇到第 2 种会主动唤醒，唤到地址就**当场**下载上报，不等整批。
"""
import os
import pytest
from unittest.mock import MagicMock

from integrations.google_fx.models import VideoRequest
from integrations.google_fx.services.google_fx_video import _ChunkRunner
from integrations.google_fx.services import google_fx_helpers as H

FIXTURE = os.path.join(os.path.dirname(__file__), 'fixtures', 'flow_new_tile_dom.html')


# ── 识别层：拿真实卡片骨架跑真正的 JS ──

@pytest.fixture(scope='module')
def flow_dom_page():
    """把现场裁下来的卡片骨架灌进真实浏览器——这段判定全在 JS 里，
    用假 page 测等于什么都没测。没装 chromium 就跳过。"""
    sync_api = pytest.importorskip('playwright.sync_api')
    with open(FIXTURE, encoding='utf-8') as f:
        html = f.read()
    try:
        pw = sync_api.sync_playwright().start()
        browser = pw.chromium.launch()
    except Exception as e:  # 没装浏览器 / 环境不可用
        pytest.skip("chromium 不可用: {}: {}".format(type(e).__name__, e))
    page = browser.new_page()
    page.set_content(html, wait_until='domcontentloaded')
    yield page
    browser.close()
    pw.stop()


def test_inspect_separates_pending_from_finished_thumbnail_tile(flow_dom_page):
    res = H._inspect_all_pending_tiles(
        flow_dom_page, ['tile_pending', 'tile_thumb', 'tile_video', 'tile_image'])

    pending = res['tile_pending']
    assert pending['isPending'] is True and pending['progress'] == 18
    # play_circle 图标在生成中的卡片上也有，不能靠它判完成
    assert pending['isVisiblyFinished'] is False
    assert pending['needsMediaWake'] is False

    thumb = res['tile_thumb']
    assert thumb['isVisiblyFinished'] is True and thumb['videoSrc'] is None
    assert thumb['needsMediaWake'] is True, "成片缩略图卡片必须被要求唤醒，不能一路哑到超时"

    done = res['tile_video']
    assert done['status'] == 'done'
    assert done['videoSrc'].startswith('https://flow-content.google/video/')
    assert done['needsMediaWake'] is False

    # 参考图卡片带 img，但绝不能被当成成片（否则会被当视频认领/交付）
    assert res['tile_image']['isVisiblyFinished'] is False
    assert res['tile_image']['needsMediaWake'] is False


def test_canvas_scan_marks_thumbnail_tile_claimable(flow_dom_page):
    by_id = {t['tileId']: t for t in H._scan_canvas_tiles(flow_dom_page)}
    assert by_id['tile_thumb']['visiblyFinished'] is True
    assert by_id['tile_thumb']['videoSrc'] is None
    assert by_id['tile_pending']['visiblyFinished'] is False
    assert by_id['tile_image']['visiblyFinished'] is False


def test_read_tile_video_src_never_leaks_across_tiles(flow_dom_page):
    """只读这张卡自己的 <video>。跨卡取地址就是静默串片，比读不到严重得多。"""
    assert H.read_flow_tile_video_src(flow_dom_page, 'tile_video')
    assert H.read_flow_tile_video_src(flow_dom_page, 'tile_thumb') == ''
    assert H.read_flow_tile_video_src(flow_dom_page, 'tile_image') == ''


# ── 轮询层：唤到地址就当场交付 ──

def _runner(tmp_path, notifications, count=1):
    reqs = [VideoRequest(prompt='p{}'.format(i), output_path=str(tmp_path))
            for i in range(count)]
    runner = _ChunkRunner(
        count, 0, reqs, {},
        lambda idx, stage, payload: notifications.append((idx, stage, payload)), None)
    return runner, reqs


def test_wake_flips_silent_tile_to_done_and_streams_immediately(monkeypatch, tmp_path):
    notifications = []
    runner, reqs = _runner(tmp_path, notifications)
    monkeypatch.setattr(
        'integrations.google_fx.services.google_fx_video.download_video_via_browser',
        lambda page, url, out_dir, prefix: str(tmp_path / (prefix + '.mp4')))
    woke_url = 'https://flow-content.google/video/abc'
    calls = []

    def fake_wake(page, tile_id, uuid='', allow_click=False, **kw):
        calls.append((tile_id, allow_click))
        return woke_url

    monkeypatch.setattr(
        'integrations.google_fx.services.google_fx_video.wake_flow_tile_video', fake_wake)

    submitted = [dict(sub_idx=0, idx=0, req=reqs[0], tile_id='t0', status='generating')]
    states = {'t0': {'status': 'generating', 'videoSrc': None, 'needsMediaWake': True,
                     'isVisiblyFinished': True}}
    page = MagicMock()
    runner._wake_ready_tiles(page, submitted, states)

    assert calls == [('t0', False)], "第一次只 hover，不该上来就点开播放器"
    assert states['t0']['status'] == 'done'
    assert states['t0']['videoSrc'] == woke_url

    runner._deliver_ready_tasks(page, submitted, states)
    assert [n[1] for n in notifications] == ['video_done']
    assert runner.completed == {0}


def test_wake_escalates_to_click_then_stops_at_cap(monkeypatch, tmp_path):
    notifications = []
    runner, reqs = _runner(tmp_path, notifications)
    calls = []

    def fake_wake(page, tile_id, uuid='', allow_click=False, **kw):
        calls.append(allow_click)
        return ''  # 一直唤不出来

    monkeypatch.setattr(
        'integrations.google_fx.services.google_fx_video.wake_flow_tile_video', fake_wake)
    submitted = [dict(sub_idx=0, idx=0, req=reqs[0], tile_id='t0', status='generating')]
    page = MagicMock()
    for _ in range(runner._MEDIA_WAKE_MAX_ATTEMPTS + 4):
        states = {'t0': {'status': 'generating', 'videoSrc': None,
                         'needsMediaWake': True, 'isVisiblyFinished': True}}
        runner._wake_ready_tiles(page, submitted, states)
        assert states['t0']['status'] == 'generating'

    assert len(calls) == runner._MEDIA_WAKE_MAX_ATTEMPTS, "唤不醒的卡片不能无限碰下去"
    assert calls[:runner._MEDIA_WAKE_CLICK_AFTER] == [False] * runner._MEDIA_WAKE_CLICK_AFTER
    assert calls[runner._MEDIA_WAKE_CLICK_AFTER] is True
    assert not notifications


def test_wake_skips_pending_and_already_reported_tiles(monkeypatch, tmp_path):
    runner, reqs = _runner(tmp_path, [], count=2)
    calls = []

    def fake_wake(page, tile_id, **kw):
        calls.append(tile_id)
        return ''

    monkeypatch.setattr(
        'integrations.google_fx.services.google_fx_video.wake_flow_tile_video', fake_wake)
    submitted = [
        dict(sub_idx=0, idx=0, req=reqs[0], tile_id='pending', status='generating'),
        dict(sub_idx=1, idx=1, req=reqs[1], tile_id='done', status='success', reported=True),
    ]
    states = {
        'pending': {'status': 'generating', 'needsMediaWake': False, 'isPending': True},
        'done': {'status': 'done', 'needsMediaWake': False},
    }
    runner._wake_ready_tiles(MagicMock(), submitted, states)
    assert calls == [], "生成中的卡片和已交付的任务都不该去碰"


# ── 认领层：重试轮别把已经做好的段重新提交一遍 ──

def _retry_runner(tmp_path, scan_tile, woken, monkeypatch):
    req = VideoRequest(prompt='long original prompt for slot one', output_path=str(tmp_path))
    runner = _ChunkRunner(1, 0, [req], {}, None, None)
    runner.project_url = 'project-a'
    runner.gen_retry_used = 1
    runner._submitted_tiles[('project-a', 0)] = 'known'
    monkeypatch.setattr(
        'integrations.google_fx.services.google_fx_video._scan_canvas_tiles',
        lambda page: [scan_tile])
    monkeypatch.setattr(
        'integrations.google_fx.services.google_fx_video.wake_flow_tile_video',
        lambda page, tile_id, **kw: woken)
    return runner, req


THUMB_TILE = dict(tileId='known', originalTileId='known', textClean='playcircle360p',
                  imageUuids=[], videoSrc=None, visiblyFinished=True, failed=False)


def test_retry_claims_thumbnail_tile_after_waking_it(monkeypatch, tmp_path):
    runner, req = _retry_runner(tmp_path, dict(THUMB_TILE),
                                'https://flow-content.google/video/woken', monkeypatch)
    adopted, remaining = runner._adopt_completed_tiles(MagicMock(), [(0, req)])
    assert len(adopted) == 1 and not remaining
    assert adopted[0]['video_url'] == 'https://flow-content.google/video/woken'


def test_retry_resubmits_when_thumbnail_tile_cannot_be_woken(monkeypatch, tmp_path):
    """唤不出地址的"成片形态"卡片不算数：认领它只会白占一个槽位，照常重新提交。"""
    runner, req = _retry_runner(tmp_path, dict(THUMB_TILE), '', monkeypatch)
    adopted, remaining = runner._adopt_completed_tiles(MagicMock(), [(0, req)])
    assert adopted == []
    assert remaining == [(0, req)]
