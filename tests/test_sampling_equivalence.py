"""Performance changes must preserve facts, cache identity and timestamps."""
import json
import importlib.util
import shutil
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from PIL import ImageChops
import pytest
import prompt_pipeline as pp








@pytest.mark.parametrize('variable_rate', [False, True])
def test_batch_frames_match_legacy_with_duplicates_and_fractional_times(tmp_path, variable_rate):
    if not shutil.which('ffmpeg'):
        pytest.skip('FFmpeg required for pixel equivalence test')
    script = Path(__file__).resolve().parents[1] / 'skills/gemini-omni-restoration-composer/scripts/analyze_timelapse_video.py'
    spec = importlib.util.spec_from_file_location('batch_analyzer', script)
    analyzer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(analyzer)
    video = tmp_path / 'test.mkv'
    command = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-f', 'lavfi',
               '-i', ('testsrc2=size=96x64:rate=10:duration=1' if variable_rate else
                      'testsrc2=size=96x64:rate=30:duration=61')]
    if variable_rate:
        command += ['-vf', "setpts='if(lt(N,5),N,2*N-5)/(10*TB)'", '-fps_mode', 'vfr']
    analyzer.run(command + ['-c:v', 'ffv1', str(video)])
    meta = analyzer.media_metadata(video)
    times = [0, .01, .05, .1, .101, .401, .7, meta['duration_sec']]
    if not variable_rate:
        times += [52.7, 53.2, 57.7, 60.2]
    requests = [(tmp_path / f'new{i}.png', t) for i, t in enumerate(times)]
    analyzer.extract_frames_batch(video, requests, meta['duration_sec'], meta['fps'])
    for i, (new, t) in enumerate(requests):
        old = tmp_path / f'old{i}.png'
        analyzer.extract_frame(video, old, t, meta['duration_sec'], meta['fps'])
        with Image.open(old) as a, Image.open(new) as b:
            assert ImageChops.difference(a.convert('RGB'), b.convert('RGB')).getbbox() is None
