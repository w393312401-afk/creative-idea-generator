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
    assert "tile.querySelectorAll('img[src*=\"getMediaUrlRedirect\"]')" in source
    assert "if (!tileId || before.has(tileId)) continue" in source
    assert "路径B新结果tile捕获" in source
    assert "_MIN_GENERATED_RESULT_AGE_SECONDS" in source
    assert "blocked_media_uuids.add(media_uuid)" in source
    assert "getattr(req, \"excluded_media_uuids\"" in source
    assert "getattr(req, \"excluded_image_paths\"" in source
