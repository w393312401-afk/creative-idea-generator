# -*- coding: utf-8 -*-
"""滚动视频批次只建一个执行器并保留每项自己的账号和画布。"""

from types import SimpleNamespace

from integrations.google_fx.services import google_fx_video as video


def test_batch_uses_one_runner_for_all_requests(monkeypatch):
    seen_initial_urls = []

    class _Runner:
        def __init__(self, chunk, **_kwargs):
            self.chunk = chunk

        def run(self):
            seen_initial_urls.append(getattr(self, "project_url", None))
            assert len(self.chunk) == 14
            if not self.project_url:
                self.project_url = "https://labs.google/fx/tools/flow/project/task-project"
            return [{"status": "failed", "video_url": None} for _ in self.chunk]

    monkeypatch.setattr(video, "_ChunkRunner", _Runner)

    reqs = [SimpleNamespace(prompt=f"prompt {i}", model="veo-3.1") for i in range(14)]
    video.generate_videos_batch_google_fx(reqs)

    assert seen_initial_urls == [None]


def test_batch_starts_from_local_project_canvas_and_returns_binding(monkeypatch):
    seen_initial_urls = []

    class _Runner:
        def __init__(self, chunk, **_kwargs):
            self.chunk = chunk

        def run(self):
            seen_initial_urls.append(self.project_url)
            assert self.bound_project_url == self.project_url
            return [{"status": "failed", "video_url": None,
                     "project_url": self.project_url, "account_id": "actual-account"}
                    for _ in self.chunk]

    monkeypatch.setattr(video, "_ChunkRunner", _Runner)
    bound = "https://labs.google/fx/tools/flow/project/local-project"
    reqs = [
        SimpleNamespace(prompt=f"prompt {i}", model="veo-3.1", project_url=bound)
        for i in range(7)
    ]

    results = video.generate_videos_batch_google_fx(reqs)

    assert seen_initial_urls == [bound]
    assert {item["project_url"] for item in results} == {bound}
    assert {item["account_id"] for item in results} == {"actual-account"}
