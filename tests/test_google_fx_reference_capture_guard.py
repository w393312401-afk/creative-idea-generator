# -*- coding: utf-8 -*-
"""Regression coverage for prompt-reference URLs racing generated-image capture."""

import inspect

from PIL import Image

from integrations.google_fx.services import google_fx_image as image_service


REFERENCE_UUID = "3e72de93-07ae-4311-8625-3ada5279f4c6"
RESULT_UUID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def test_extract_media_uuid_from_flow_redirect_variants():
    assert image_service._extract_media_uuid(
        f"/fx/api/trpc/media.getMediaUrlRedirect?name={REFERENCE_UUID}"
    ) == REFERENCE_UUID
    assert image_service._extract_media_uuid(
        f"https://flow-content.google/image/{REFERENCE_UUID}?Expires=123"
    ) == REFERENCE_UUID
    assert image_service._extract_media_uuid(
        f"fx_batch_123_0_{REFERENCE_UUID}.jpg"
    ) == REFERENCE_UUID


def test_prompt_reference_uuid_is_never_an_output_candidate():
    blocked = {REFERENCE_UUID}
    assert image_service._is_blocked_media_candidate(
        f"/fx/api/trpc/media.getMediaUrlRedirect?name={REFERENCE_UUID}", blocked
    )
    assert image_service._is_blocked_media_candidate(
        f"https://flow-content.google/image/{REFERENCE_UUID}", blocked
    )
    assert not image_service._is_blocked_media_candidate(
        f"https://flow-content.google/image/{RESULT_UUID}", blocked
    )


def test_fast_result_in_new_submission_tile_bypasses_age_guard():
    submit_ts = 100.0
    assert not image_service._is_generated_candidate_stable(
        submit_ts, now=102.0, confirmed_new_tile=False
    )
    assert image_service._is_generated_candidate_stable(
        submit_ts, now=102.0, confirmed_new_tile=True
    )
    assert image_service._is_generated_candidate_stable(
        submit_ts, now=submit_ts + image_service._MIN_GENERATED_RESULT_AGE_SECONDS,
        confirmed_new_tile=False,
    )


def test_early_candidate_is_deferred_not_blacklisted():
    """The 8s age guard must never permanently reject a UUID.

    Nano Banana Lite returns in 6–10s, straddling the threshold: three
    consecutive generations (d5b9144b / 6dd6aa35 / f07d50ca) were captured on
    the network within 8s of Send, blacklisted as "history", and then waited out
    the full 120s timeout even though Flow had already put them on the canvas.
    """
    source = inspect.getsource(
        image_service._generate_images_batch_google_fx_single_attempt
    )
    early_branch = source[source.index("_is_generated_candidate_stable("):]
    early_branch = early_branch[:early_branch.index("def _scan_new_result_tiles")]
    assert "blocked_media_uuids.add(" not in early_branch
    assert "ignored_candidates.add(url)" not in early_branch
    # The blacklist check owns every permanent rejection, so it must run first.
    assert source.index("_is_blocked_media_candidate(url, blocked_media_uuids)") < \
        source.index("if not _is_generated_candidate_stable(")


def test_unconfirmed_candidate_is_accepted_after_grace_period():
    """A virtualized canvas may never mount the result tile; don't wait forever."""
    first_seen = 500.0
    grace = image_service._UNCONFIRMED_CANDIDATE_GRACE_SECONDS
    assert not image_service._unconfirmed_candidate_is_acceptable(
        first_seen, now=first_seen + grace - 1
    )
    assert image_service._unconfirmed_candidate_is_acceptable(
        first_seen, now=first_seen + grace
    )
    # The grace period must still leave room to download inside the wait budget.
    assert grace < image_service._MIN_GENERATED_RESULT_AGE_SECONDS * 10


def test_recompressed_reference_is_rejected_as_near_duplicate(tmp_path):
    reference = tmp_path / "chain_ref_003.jpg"
    candidate = tmp_path / "captured_result.jpg"
    distinct = tmp_path / "real_result.jpg"

    Image.new("RGB", (64, 96), (90, 120, 140)).save(reference, quality=96)
    Image.new("RGB", (64, 96), (91, 120, 140)).save(candidate, quality=82)
    Image.new("RGB", (64, 96), (180, 40, 20)).save(distinct, quality=90)

    duplicate, mad, matched = image_service._images_are_near_duplicates(
        str(candidate), [str(reference)]
    )
    assert duplicate is True
    assert mad <= 3.0
    assert matched == str(reference)

    duplicate, mad, matched = image_service._images_are_near_duplicates(
        str(distinct), [str(reference)]
    )
    assert (duplicate, mad, matched) == (False, None, None)


def test_capture_baselines_are_frozen_before_send():
    source = inspect.getsource(
        image_service._generate_images_batch_google_fx_single_attempt
    )
    freeze_at = source.index("pre_submit_known_net_urls =")
    send_at = source.index("click_fx_send_button(page, input_el)")
    assert freeze_at < send_at
    assert "blocked_media_uuids=pre_submit_media_uuids" in source
    assert "tile.querySelectorAll('img')" in source
    assert "confirmed_new_tile=True" in source
    assert "if (!tileId || before.has(tileId)) continue" in source
    assert "路径B新结果tile捕获" in source
    assert "_is_generated_candidate_stable(" in source
    assert "_unconfirmed_candidate_is_acceptable(" in source
    assert "getattr(req, \"excluded_media_uuids\"" in source
    assert "getattr(req, \"excluded_image_paths\"" in source


def test_reference_mount_failure_is_a_stable_hard_failure():
    error = image_service._reference_mount_error("frame 2")
    assert isinstance(error, image_service.ReferenceMountError)
    assert str(error).startswith("REFERENCE_MOUNT_FAILED:")


def test_i2i_paths_never_submit_after_reference_mount_failure():
    """挂载失败的三条路都必须在提交前停住，绝不降级成无参考生成。

    2026-08-02：第三处（循环内的链式续挂失败）改成经 _halt_batch_keeping_prefix
    停批，而不再直接 raise —— 直接 raise 会跳过 result["image_urls"] = local_paths，
    把本批已经生成、下载、扣过积分的前几张一并作废。「停在这一张、不继续提交」
    这个核心保证没变，变的只是失败结果怎么带回去。
    """
    source = inspect.getsource(
        image_service._generate_images_batch_google_fx_single_attempt
    )
    assert "所有参考图均未成功挂载，以无参考模式生成" not in source
    assert "本帧将脱链以无参考模式生成" not in source

    # 三处挂载失败点都要构造 ReferenceMountError：循环外的两处（参考图全部挂载
    # 失败 / 本地文件不存在）直接 raise，循环内的一处交给 _halt_batch_keeping_prefix。
    assert source.count("_reference_mount_error(") >= 3
    assert source.count("raise _reference_mount_error(") >= 2

    loop_body = source.split("for idx, prompt_text in enumerate(prompts):", 1)[1]
    chain_failure = loop_body.split("_reference_mount_error(", 1)[0]
    # 循环内那处必须走"保前缀后 break"，而不是继续往下走到 click_fx_send_button。
    assert "_halt_batch_keeping_prefix(" in chain_failure
    assert "break" in loop_body.split("_reference_mount_error(", 1)[1][:400]
