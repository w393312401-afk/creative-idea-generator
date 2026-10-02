"""Explicit retries and adjacent chunks reuse only the correct canvas assets."""

import os
from types import SimpleNamespace

import pytest

from integrations.google_fx.services import google_fx_video as V
from integrations.google_fx.services.google_fx_reference_cache import (
    ReferenceUploadCache,
    reference_cache_key,
)


PROJECT = "https://labs.google/fx/tools/flow/project/retry-cache"


@pytest.fixture
def upload_env(tmp_path, monkeypatch):
    frames = []
    for i in range(3):
        path = tmp_path / f"frame_{i}.webp"
        path.write_bytes(f"frame-{i}".encode())
        frames.append(str(path))
    env = SimpleNamespace(frames=frames, canvas=set(), uploads=[], account="account-a", now=0)
    cache = ReferenceUploadCache()
    monkeypatch.setattr(V, "REFERENCE_UPLOAD_CACHE", cache)
    monkeypatch.setattr(V.account_binding, "resolve_account", lambda **kwargs: env.account)
    monkeypatch.setattr(V, "get_runtime_default_user_id", lambda: "")
    monkeypatch.setattr(V, "_get_panel_uuids", lambda page: set(env.canvas))
    monkeypatch.setattr(V, "_find_add2_btn", lambda page: object())
    monkeypatch.setattr(V, "random_sleep", lambda *args: None)
    monkeypatch.setattr(V.time, "time", lambda: env.now)
    monkeypatch.setattr(V.time, "sleep", lambda seconds: setattr(env, "now", env.now + seconds))

    def upload(page, path, **kwargs):
        env.uploads.append(path)
        uuid = f"00000000-0000-4000-8000-{len(env.uploads):012d}"
        env.canvas.add(uuid)
        return uuid

    monkeypatch.setattr(V, "_upload_image_to_canvas", upload)

    def runner(start=0, end=1, project=PROJECT):
        req = SimpleNamespace(image=frames[start], end_image=frames[end],
                              image_uuid="", end_image_uuid="", ratio="9:16")
        result = V._ChunkRunner(1, 0, [req], {}, None, None)
        result.project_url = project
        return result

    env.runner = runner
    env.run = lambda item: item._upload_references(object(), list(enumerate(item.chunk)))
    env.cache = cache
    return env


def test_explicit_retry_reuses_both_references_in_a_fresh_runner(upload_env):
    env = upload_env
    first = env.run(env.runner())
    second = env.run(env.runner())
    assert second == first
    assert env.uploads == env.frames[:2]
    assert env.now == 0  # UUIDs already visible: no readiness timeout spent.


def test_next_chunk_uploads_only_the_new_endpoint(upload_env):
    env = upload_env
    first = env.run(env.runner())
    second = env.run(env.runner(start=1, end=2))
    assert second[env.frames[1]] == first[env.frames[1]]
    assert env.uploads == env.frames


@pytest.mark.parametrize("same_runner", [False, True])
def test_replaced_file_is_uploaded_even_with_same_size_and_mtime(upload_env, same_runner):
    env = upload_env
    runner = env.runner()
    first = env.run(runner)
    before = os.stat(env.frames[0])
    with open(env.frames[0], "wb") as target:
        target.write(b"changed")  # Same byte length as frame-0.
    os.utime(env.frames[0], ns=(before.st_atime_ns, before.st_mtime_ns))
    second = env.run(runner if same_runner else env.runner())
    assert second[env.frames[0]] != first[env.frames[0]]
    assert second[env.frames[1]] == first[env.frames[1]]
    assert env.uploads == [*env.frames[:2], env.frames[0]]


def test_missing_canvas_asset_reuploads_only_that_file(upload_env):
    env = upload_env
    first = env.run(env.runner())
    env.canvas.remove(first[env.frames[1]])
    second = env.run(env.runner())
    assert second[env.frames[0]] == first[env.frames[0]]
    assert second[env.frames[1]] != first[env.frames[1]]
    assert env.uploads == [*env.frames[:2], env.frames[1]]
    assert env.run(env.runner()) == second
    assert len(env.uploads) == 3


def test_replaced_file_cannot_be_reseeded_from_an_old_manifest_uuid(upload_env):
    env = upload_env
    first = env.run(env.runner())
    with open(env.frames[0], "wb") as target:
        target.write(b"replacement")
    retry = env.runner()
    retry.canvas_is_bound = True
    retry.chunk[0].image_uuid = first[env.frames[0]]
    retry.chunk[0].end_image_uuid = first[env.frames[1]]
    second = env.run(retry)
    assert second[env.frames[0]] != first[env.frames[0]]
    assert second[env.frames[1]] == first[env.frames[1]]
    assert len(env.uploads) == 3


@pytest.mark.parametrize("same_runner", [False, True])
def test_account_change_cannot_reuse_uploads(upload_env, same_runner):
    env = upload_env
    runner = env.runner()
    first = env.run(runner)
    env.account = "account-b"
    second = env.run(runner if same_runner else env.runner())
    assert set(first.values()).isdisjoint(second.values())
    assert len(env.uploads) == 4


@pytest.mark.parametrize("same_runner", [False, True])
def test_project_change_cannot_reuse_uploads(upload_env, same_runner):
    env = upload_env
    runner = env.runner()
    first = env.run(runner)
    if same_runner:
        runner.project_url = PROJECT + "-other"
    else:
        runner = env.runner(project=PROJECT + "-other")
    second = env.run(runner)
    assert set(first.values()).isdisjoint(second.values())
    assert len(env.uploads) == 4


def test_unknown_account_does_not_share_reference_uploads(upload_env):
    env = upload_env
    env.account = ""
    env.run(env.runner())
    env.run(env.runner())
    assert len(env.uploads) == 4


def test_becoming_bound_to_an_account_discards_unscoped_local_uploads(upload_env):
    env = upload_env
    env.account = ""
    runner = env.runner()
    first = env.run(runner)
    env.account = "known-account"
    second = env.run(runner)
    assert set(first.values()).isdisjoint(second.values())
    assert len(env.uploads) == 4


def test_same_image_endpoints_keep_two_distinct_assets_on_retry(upload_env):
    env = upload_env
    first = env.run(env.runner(start=0, end=0))
    second = env.run(env.runner(start=0, end=0))
    assert second == first
    assert len(set(second.values())) == 2
    assert env.uploads == [env.frames[0], env.frames[0]]


def test_colliding_cached_uuids_are_evicted_and_never_returned(upload_env):
    env = upload_env
    first = env.run(env.runner())
    scope = (env.account, PROJECT)
    key = reference_cache_key(scope, env.frames[1])
    env.cache.put(key, first[env.frames[0]])
    assert env.run(env.runner()) == {}
    assert env.cache.get(key) is None
    mapping = env.run(env.runner())
    assert len(set(mapping.values())) == 2
    assert len(env.uploads) == 4


def test_file_changed_during_upload_is_not_cached(upload_env, monkeypatch):
    env = upload_env
    original_upload = V._upload_image_to_canvas

    def upload_and_replace(page, path, **kwargs):
        uuid = original_upload(page, path, **kwargs)
        with open(path, "ab") as target:
            target.write(b"changed-during-upload")
        return uuid

    monkeypatch.setattr(V, "_upload_image_to_canvas", upload_and_replace)
    env.run(env.runner())
    monkeypatch.setattr(V, "_upload_image_to_canvas", original_upload)
    env.run(env.runner())
    assert len(env.uploads) == 4


def test_cache_is_bounded_and_reading_refreshes_recency():
    cache = ReferenceUploadCache(max_entries=2)
    a, b, c = ("a", "hash"), ("b", "hash"), ("c", "hash")
    cache.put(a, "uuid-a")
    cache.put(b, "uuid-b")
    assert cache.get(a) == "uuid-a"
    cache.put(c, "uuid-c")
    assert cache.get(b) is None
    assert cache.get(a) == "uuid-a"
    cache.put(("a", "new-hash"), "uuid-new")
    assert cache.get(a) is None


def test_missing_file_has_no_cache_key(tmp_path):
    assert reference_cache_key(("account", PROJECT), str(tmp_path / "missing")) is None
