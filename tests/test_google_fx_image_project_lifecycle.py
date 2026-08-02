# -*- coding: utf-8 -*-
import os
import shutil
from types import SimpleNamespace

import frame_generator as frames
from integrations.google_fx.services import google_fx_image as image_service
from integrations.google_fx.utils import account_binding


def test_image_chunks_pass_the_same_flow_project_url_forward():
    seen_urls = []
    bound = "https://labs.google/fx/tools/flow/project/local-project"

    class _Models:
        @staticmethod
        def ImageBatchRequest(**kwargs):
            return SimpleNamespace(**kwargs)

    class _Fx:
        @staticmethod
        def _generate_images_batch_google_fx(req):
            seen_urls.append(req.project_url)
            path = os.path.join(req.output_path, "result.jpg")
            with open(path, "wb") as handle:
                handle.write(b"image")
            return {
                "status": "success",
                "image_urls": [path],
                "project_url": bound,
            }

    session = {}
    created_dirs = []
    try:
        for prompt in ("first", "second"):
            _paths, temp_dir = frames._fx_generate_batch(
                _Fx(), _Models(), {}, [prompt], None, canvas_session=session
            )
            created_dirs.append(temp_dir)
    finally:
        for temp_dir in created_dirs:
            shutil.rmtree(temp_dir, ignore_errors=True)

    assert seen_urls == [None, bound]
    assert session == {"project_url": bound}


def test_image_projects_are_isolated_per_bound_account():
    seen = []
    urls = {
        "acct-a": "https://labs.google/fx/tools/flow/project/a",
        "acct-b": "https://labs.google/fx/tools/flow/project/b",
    }

    class _Models:
        @staticmethod
        def ImageBatchRequest(**kwargs):
            return SimpleNamespace(**kwargs)

    class _Fx:
        @staticmethod
        def _generate_images_batch_google_fx(req):
            account = account_binding.current_task_account()
            seen.append((account, req.project_url))
            path = os.path.join(req.output_path, f"{account}.jpg")
            with open(path, "wb") as handle:
                handle.write(b"image")
            return {"status": "success", "image_urls": [path],
                    "project_url": urls[account], "attempts_used": 1}

    session = {}
    created = []
    try:
        for account in ("acct-a", "acct-b", "acct-a"):
            with account_binding.bound_task_account(account):
                _paths, temp_dir = frames._fx_generate_batch(
                    _Fx(), _Models(), {}, [account], None, canvas_session=session
                )
            created.append(temp_dir)
    finally:
        for temp_dir in created:
            shutil.rmtree(temp_dir, ignore_errors=True)

    assert seen == [("acct-a", None), ("acct-b", None), ("acct-a", urls["acct-a"])]
    assert session["projects_by_account"] == urls


def test_partial_image_batch_returns_durable_prefix_instead_of_discarding_it():
    class _Models:
        @staticmethod
        def ImageBatchRequest(**kwargs):
            return SimpleNamespace(**kwargs)

    class _Fx:
        @staticmethod
        def _generate_images_batch_google_fx(req):
            path = os.path.join(req.output_path, "prefix.jpg")
            with open(path, "wb") as handle:
                handle.write(b"prefix")
            return {
                "status": "partial",
                "image_urls": [path],
                "failed_index": 1,
                "attempts_used": 1,
                "message": "second image timed out",
            }

    paths, temp_dir = frames._fx_generate_batch(
        _Fx(), _Models(), {}, ["first", "second"], None
    )
    try:
        assert len(paths) == 1
        assert os.path.exists(paths[0])
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_targeted_iteration_splits_adjacent_frames_owned_by_different_canvases():
    manifest = {
        "google_fx_project_url": "https://labs.google/fx/tools/flow/project/legacy",
        "google_fx_project_account_id": "legacy-account",
    }
    existing = {
        2: {
            "fx_account_id": "acct-a",
            "fx_project_url": "https://labs.google/fx/tools/flow/project/a",
        },
        3: {
            "fx_account_id": "acct-b",
            "fx_project_url": "https://labs.google/fx/tools/flow/project/b",
        },
        4: {
            "fx_account_id": "acct-b",
            "fx_project_url": "https://labs.google/fx/tools/flow/project/b",
        },
    }

    chunks = frames.split_fx_chunks_by_canvas([[2, 3, 4]], existing, manifest)

    assert chunks == [[2], [3, 4]]


def test_legacy_targeted_iteration_uses_the_original_project_binding():
    manifest = {
        "google_fx_project_url": "https://labs.google/fx/tools/flow/project/original",
        "google_fx_project_account_id": "original-account",
    }

    assert frames._fx_frame_canvas_binding({}, manifest) == (
        "original-account",
        "https://labs.google/fx/tools/flow/project/original",
    )


def test_original_canvas_iteration_never_switches_to_another_account(monkeypatch):
    """A different account cannot open the target frame's account-scoped project URL."""
    switched = []
    monkeypatch.setattr(
        image_service,
        "_generate_images_batch_google_fx_single_attempt",
        lambda _req: (_ for _ in ()).throw(RuntimeError("quota exhausted")),
    )
    monkeypatch.setattr(
        image_service, "_classify_failure_for_switch", lambda _exc: (True, "quota")
    )
    monkeypatch.setattr(image_service, "_record_current_generation_failure", lambda _exc: None)
    monkeypatch.setattr(image_service, "_cancellable_sleep", lambda _seconds: None)
    monkeypatch.setattr(
        image_service,
        "_switch_account_on_failure",
        lambda **_kwargs: switched.append(True),
    )

    result = image_service._generate_images_batch_google_fx_unlocked(
        SimpleNamespace(max_attempts=2, allow_account_switch=False)
    )

    assert result["status"] == "failed"
    assert switched == []
