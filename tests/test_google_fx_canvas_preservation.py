# -*- coding: utf-8 -*-
"""Regression coverage: media generation may clean only the Prompt bar."""

import inspect

from integrations.google_fx.services import google_fx_helpers as H
from integrations.google_fx.services import google_fx_image as I


class _NoCanvasMutationPage:
    def evaluate(self, *_args, **_kwargs):
        raise AssertionError("canvas cleanup attempted to evaluate page-wide deletion JS")


def test_legacy_failed_card_cleanup_is_a_safe_noop():
    page = _NoCanvasMutationPage()
    assert H._delete_failed_cards(page) == 0
    assert H._clear_existing_uploaded_frame(page, "Start") is False


def test_prepare_canvas_never_calls_failed_card_cleanup(monkeypatch):
    monkeypatch.setattr(H, "_find_fx_prompt_input", lambda *_a, **_k: object())
    monkeypatch.setattr(H, "_wait_for_fx_toolbar", lambda *_a, **_k: None)
    monkeypatch.setattr(
        H,
        "_delete_failed_cards",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("canvas card deletion attempted")),
    )

    H._prepare_fx_canvas(object(), has_refs=False)


def test_image_batch_loop_has_no_canvas_card_deletion_call():
    source = inspect.getsource(I._generate_images_batch_google_fx_single_attempt)
    assert "_delete_failed_cards(" not in source


def test_video_cleanup_reuses_prompt_bar_scoped_cleaner(monkeypatch):
    page = object()
    seen = []
    monkeypatch.setattr(
        H,
        "_clear_prompt_reference_chips_image",
        lambda actual: seen.append(actual) or 2,
    )

    assert H._clear_prompt_reference_chips_video(page) == 2
    assert seen == [page]
