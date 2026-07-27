# -*- coding: utf-8 -*-
"""生成成功后的账号任务计数必须归到本批实际绑定的 AdsPower 账号。"""

from types import SimpleNamespace

from integrations.google_fx.services import google_fx_video as video
from integrations.google_fx.utils import account_binding
from integrations.google_fx.utils import account_pool as ap


class _SuccessfulRunner:
    def __init__(self, **_kwargs):
        pass

    def run(self):
        return [{"status": "success", "video_url": "https://example.test/video.mp4"}]


def _counts():
    return {
        row["user_id"]: row["video_task_count"]
        for row in ap.AccountPool().list_accounts(heal=False)
    }


def test_video_count_prefers_actual_task_binding_over_default(tmp_path, monkeypatch):
    monkeypatch.setattr(ap, "_STATE_FILE", tmp_path / "accounts.json")
    monkeypatch.setattr(ap.AccountPool, "_profile_name_map", lambda self: {})
    monkeypatch.setattr(video, "_ChunkRunner", _SuccessfulRunner)
    monkeypatch.setattr(video, "get_runtime_default_user_id", lambda: "default")
    ap.AccountPool().add_account("default")
    ap.AccountPool().add_account("actual")

    with account_binding.bound_task_account("actual"):
        video.generate_videos_batch_google_fx([
            SimpleNamespace(prompt="one", model="veo-3.1")
        ])

    assert _counts() == {"default": 0, "actual": 1}


def test_video_count_falls_back_to_runtime_default_when_unbound(tmp_path, monkeypatch):
    monkeypatch.setattr(ap, "_STATE_FILE", tmp_path / "accounts.json")
    monkeypatch.setattr(ap.AccountPool, "_profile_name_map", lambda self: {})
    monkeypatch.setattr(video, "_ChunkRunner", _SuccessfulRunner)
    monkeypatch.setattr(video, "get_runtime_default_user_id", lambda: "default")
    ap.AccountPool().add_account("default")

    with account_binding.bound_task_account(None):
        video.generate_videos_batch_google_fx([
            SimpleNamespace(prompt="one", model="veo-3.1")
        ])

    assert _counts() == {"default": 1}
