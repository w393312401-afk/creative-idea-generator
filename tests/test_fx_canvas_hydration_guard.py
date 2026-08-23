# -*- coding: utf-8 -*-
"""提交后头几秒抓回来的图不是本次结果，是画布重排捞上来的旧图。

2026-08-22 事故：一条正在跑的帧序列突然把别的拍的图放进了槽位。
日志把整条链写得很清楚（全部发生在同一秒）：

    [19:56:38] ✅ 已点击发送 (arrow icon)
    [19:56:38] 📡 捕获图片重定向 ×11        ← Flow 重挂历史 tile，签名 URL 全被重拉一遍
    [19:56:39] 🎯 捕获候选图 (1/4) … (4/4)   ← 4 张全是旧图
    [19:56:39] 🎉 4x 批量生成完成

抓到的 4 张里，1b0e8cb1 与 c091693e 是 5 分钟前 IMG 013 的候选图，779efad8 是
IMG 011 的候选图；AI 从这堆旧图里选了 1b0e8cb1，于是 **IMG 012 的槽位上放的是
IMG 013 的画面**（manifest 里 img_012 的 fx_uuid 就是它）。

四道防线当时全部失守，原因各不相同：

  1. `_is_generated_candidate_stable` 的最小年龄闸判的是**判定时刻**而不是
     **观测时刻**——多轮询几次就自动放行，0 秒抓到的历史图熬到 8s 照样被收下；
  2. 「新 tile」「提交后新增 DOM 媒体」这两条佐证恰恰被画布重排伪装：虚拟化卸载
     过的历史 tile 重新挂载时，tileId 不在提交前基线里，看着就像本次新结果；
  3. 候选线（4选1）从来不往台账里记账，所以昨天刚下载过的 UUID 今天依旧"清白"；
  4. 候选线也从不传 excluded_media_uuids / excluded_image_paths，本任务自己的
     历史帧对 FX 侧完全不可见。

真结果最快多久？全量日志给了两条线各自的分布：

  · 单图 1x（660 次捕获）：0/1/2s 各有若干（全是本类事故），3–6s 一次没有，
    真结果集中在 10–14s；
  · 候选 x4（85 批）：真结果最快 15s，主群 20–44s；异常批则整齐地落在
    0/1/4/7/9s —— 一批四张同一秒到齐，物理上不可能是刚下单的生成。

水化窗口就落在两簇之间，两条线各取各的。
"""

import inspect
import json
import os

from integrations.google_fx.services import google_fx_image as image_service

import candidate_selection_pipeline as csp


SUBMIT_TS = 1_000.0


def _candidate_branch_source():
    """只取 4选1 那一段（候选等待函数 + 候选提交分支），避免被单图线的同名代码蒙混。"""
    source = inspect.getsource(
        image_service._generate_images_batch_google_fx_single_attempt
    )
    start = source.index("def _wait_for_candidate_urls(")
    end = source.index("for idx, prompt_text in enumerate(prompts):", start)
    return source[start:end]


# ── 判据本身 ────────────────────────────────────────────────────────────────

def test_single_image_window_separates_history_from_real_results():
    # 事故当天的观测：提交后 0.3s / 1.0s / 2.0s 一次涌进来 11 条重定向
    for early in (0.0, 0.3, 1.0, 2.0):
        assert image_service._is_canvas_hydration_capture(
            SUBMIT_TS + early, SUBMIT_TS
        ), f"提交后 {early}s 的捕获必须判为历史媒体"

    # 单图线真结果集中在 10–14s，7/8s 各有一例，都不能拦
    for real in (7.0, 10.0, 14.0, 26.0):
        assert not image_service._is_canvas_hydration_capture(
            SUBMIT_TS + real, SUBMIT_TS
        ), f"提交后 {real}s 的捕获是真结果，不能拦"


def test_candidate_window_is_wider_than_the_single_image_one():
    """x4 批量慢一倍：真结果 15s 起，而回收来的旧图在 0–9s 整批到齐。"""
    for recycled in (0.0, 1.0, 4.0, 7.0, 9.0):
        assert image_service._is_canvas_hydration_capture(
            SUBMIT_TS + recycled, SUBMIT_TS, candidate_mode=True
        ), f"x4 模式下提交后 {recycled}s 到齐的整批必须判为旧图"
        if recycled >= 7.0:
            # 同一个时刻在单图线上是真结果——两条线不能共用一个阈值
            assert not image_service._is_canvas_hydration_capture(
                SUBMIT_TS + recycled, SUBMIT_TS
            )

    for real in (15.0, 20.0, 44.0):
        assert not image_service._is_canvas_hydration_capture(
            SUBMIT_TS + real, SUBMIT_TS, candidate_mode=True
        ), f"x4 模式下 {real}s 才到的是真结果"


def test_guard_is_inert_without_a_submit_timestamp():
    assert not image_service._is_canvas_hydration_capture(None, SUBMIT_TS)
    assert not image_service._is_canvas_hydration_capture(SUBMIT_TS, None)


def test_window_is_tunable_and_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("SPARK_FX_HYDRATION_WINDOW", "20")
    assert image_service._canvas_hydration_window_seconds() == 20.0
    assert image_service._canvas_hydration_window_seconds(True) == 20.0
    assert image_service._is_canvas_hydration_capture(SUBMIT_TS + 18, SUBMIT_TS)

    # 0 = 整条判据关掉（排障用，与台账的 SPARK_FX_MEDIA_LEDGER 同一套口径）
    monkeypatch.setenv("SPARK_FX_HYDRATION_WINDOW", "0")
    assert not image_service._is_canvas_hydration_capture(SUBMIT_TS + 0.1, SUBMIT_TS)

    # 写坏了退回默认值，别把生成任务带崩
    monkeypatch.setenv("SPARK_FX_HYDRATION_WINDOW", "十二秒")
    assert (image_service._canvas_hydration_window_seconds()
            == image_service._CANVAS_HYDRATION_WINDOW_SECONDS)


def test_windows_sit_between_the_two_observed_clusters():
    single = image_service._CANVAS_HYDRATION_WINDOW_SECONDS
    batch = image_service._CANDIDATE_HYDRATION_WINDOW_SECONDS
    assert 2.0 < single < 7.0, "单图线：夹在「重排涌入 0–2s」与「最快真结果 7s」之间"
    assert 9.0 < batch < 15.0, "候选线：夹在「整批回收 ≤9s」与「最快真结果 15s」之间"
    assert single < batch


# ── 4选1 分支的接线 ─────────────────────────────────────────────────────────

def test_candidate_capture_blacklists_the_hydration_burst():
    branch = _candidate_branch_source()
    assert "_is_canvas_hydration_capture(\n                                    ts, submit_ts_this, candidate_mode=True)" in branch, \
        "网络候选必须按**捕获时刻**判水化（且用 x4 那条更宽的窗口），而不是判定时刻"
    # 只 continue 不拉黑是不够的：同一个 UUID 熬过最小年龄闸后会被下一轮收下
    hydration_at = branch.index("_is_canvas_hydration_capture(")
    stable_at = branch.index("_is_generated_candidate_stable(")
    assert hydration_at < stable_at, "水化判据必须挡在最小年龄闸之前"
    assert "blocked_media_uuids.add(m_uuid)" in branch


def test_candidate_dom_scan_is_muted_inside_the_window():
    branch = _candidate_branch_source()
    assert "in_hydration_window = _is_canvas_hydration_capture(" in branch
    assert "time.time(), submit_ts_this, candidate_mode=True)" in branch
    assert "(() if in_hydration_window else new_tile_rows)" in branch, \
        "水化窗口内的「新 tile」多半是旧图重挂，不能采"


def test_candidate_downloads_are_ledgered_and_deduplicated():
    branch = _candidate_branch_source()
    assert "_images_are_near_duplicates(" in branch, "候选线也要有像素级近重复兜底"
    assert "media_ledger.record_consumed(" in branch, \
        "候选图落盘就是被消费掉了，不记账等于下一拍还能再抓一次"
    # 顺序与单图线一致：先近重复校验，通过了才记账
    assert branch.index("_images_are_near_duplicates(") < \
        branch.index("media_ledger.record_consumed(")
    assert "consumed_media_uuids.add(consumed_uuid)" in branch, \
        "本次提交内的后续轮询也要立刻拉黑，不能只写盘"


def test_single_image_path_still_judges_on_capture_time():
    source = inspect.getsource(
        image_service._generate_images_batch_google_fx_single_attempt
    )
    assert "capture_ts=ts," in source, "路径A 必须把捕获时刻交给判据"
    assert "elif _is_canvas_hydration_capture(" in source, \
        "路径B/C 没有捕获时刻，只能按「现在还太早」延后一轮（且不许拉黑）"
    assert "candidate_mode=is_candidate_request)" in source, \
        "单图线要按本次请求的模式取窗口，不能写死"


# ── 提交前黑名单：本任务自己的历史必须递到 FX 侧 ────────────────────────────

def test_fx_media_exclusions_reads_manifest_and_disk(tmp_path):
    project_dir = tmp_path / "run_demo"
    frames_dir = project_dir / "frames"
    (frames_dir / "fx_src").mkdir(parents=True)
    (frames_dir / "candidates" / "frame_011").mkdir(parents=True)

    frame_uuid = "1b0e8cb1-345f-410b-b723-cd6c540019c0"
    cand_uuid = "c091693e-d454-4500-9c16-3c7be2bdbc1f"
    src_uuid = "779efad8-f0dc-4d27-ae38-2685799fdcfc"
    partial_uuid = "766de8d0-d4c2-46e1-b541-065bdacbcdf9"

    (project_dir / "manifest.json").write_text(json.dumps({
        "frames": [
            {"sequence": 12, "fx_uuid": frame_uuid,
             "candidates": [{"index": 1, "fx_uuid": cand_uuid}]},
            {"sequence": 13, "fx_uuid": None},        # 有的路径没记 fx_uuid
        ]
    }), encoding="utf-8")
    (frames_dir / "img_012.webp").write_bytes(b"webp-bytes")
    (frames_dir / "img_013.webp").write_bytes(b"")     # 空文件不算槽位图
    (frames_dir / "fx_src" / f"img_012_{src_uuid}.jpg").write_bytes(b"jpg")
    # partial 批次：图已经下载落盘，manifest 还没来得及记——它一样已经被消费掉了
    (frames_dir / "candidates" / "frame_011"
     / f"candidate_1_{partial_uuid}.jpg").write_bytes(b"jpg")

    uuids, paths = csp.fx_media_exclusions(str(project_dir), str(frames_dir))

    assert set(uuids) == {frame_uuid, cand_uuid, src_uuid, partial_uuid}
    assert paths == [str(frames_dir / "img_012.webp")]


def test_near_duplicate_reference_list_stays_bounded(tmp_path):
    """像素比对的参照表要封顶：每张候选都要跟表上每张图跑一次解码+差分。"""
    project_dir = tmp_path / "run_long"
    frames_dir = project_dir / "frames"
    frames_dir.mkdir(parents=True)
    for seq in range(1, 21):
        (frames_dir / f"img_{seq:03d}.webp").write_bytes(b"webp-bytes")

    _uuids, paths = csp.fx_media_exclusions(str(project_dir), str(frames_dir))

    assert len(paths) == csp._NEAR_DUP_REFERENCE_FRAMES
    # 留下的必须是最近那几张：画布重排捞回来的历史 tile 几乎总是刚生成不久的
    assert paths[-1].endswith("img_020.webp")
    assert paths[0].endswith("img_015.webp")


def test_fx_media_exclusions_survives_a_missing_project(tmp_path):
    uuids, paths = csp.fx_media_exclusions(str(tmp_path / "nope"), None)
    assert uuids == [] and paths == []


def test_candidate_request_carries_the_exclusions():
    source = inspect.getsource(csp.generate_frame_candidates)
    assert "fx_media_exclusions(project_dir, frames_dir)" in source
    assert "excluded_media_uuids=fx_excluded_uuids," in source
    assert "excluded_image_paths=fx_excluded_paths," in source
    # 必须在建请求之前算好
    assert source.index("fx_media_exclusions(") < source.index("req = ImageBatchRequest(")
