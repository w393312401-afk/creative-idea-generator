# -*- coding: utf-8 -*-
import os
import shutil
from types import SimpleNamespace

import frame_generator as frames


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
