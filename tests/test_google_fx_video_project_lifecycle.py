# -*- coding: utf-8 -*-
"""视频任务分 chunk 时必须复用同一个 Flow 项目。"""

from types import SimpleNamespace

from integrations.google_fx.services import google_fx_video as video


def test_batch_reuses_first_chunk_project_for_all_later_chunks(monkeypatch):
    seen_initial_urls = []

    class _Runner:
        def __init__(self, chunk, **_kwargs):
            self.chunk = chunk

        def run(self):
            seen_initial_urls.append(getattr(self, "project_url", None))
            if not self.project_url:
                self.project_url = "https://labs.google/fx/tools/flow/project/task-project"
            return [{"status": "failed", "video_url": None} for _ in self.chunk]

    monkeypatch.setattr(video, "_ChunkRunner", _Runner)

    reqs = [SimpleNamespace(prompt=f"prompt {i}", model="veo-3.1") for i in range(14)]
    video.generate_videos_batch_google_fx(reqs)

    assert seen_initial_urls == [
        None,
        "https://labs.google/fx/tools/flow/project/task-project",
        "https://labs.google/fx/tools/flow/project/task-project",
    ]
