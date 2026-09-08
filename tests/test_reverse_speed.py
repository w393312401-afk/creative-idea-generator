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
from prompt_pipeline import reverse
from test_reverse_beats import _SheetJob


def test_peak_cache_reuses_success_and_invalidates_image_model_and_prompt():
    job = _SheetJob(count=3)
    job.overview['change_events'] = [{
        'event_id': 'E01', 'start': 1, 'peak': 2, 'end': 3,
        'evidence_frames': ['review_001.png', 'review_002.png', 'review_003.png']}]
    Path(job.dir, 'video_overview.json').write_text(json.dumps(job.overview))
    reply = json.dumps([{'frame': 'review_002.png', 'subject': 'wall', 'confidence': .9}])
    def invoke(model='m'):
        return reverse.verify_peak_frames({'model': model}, job.dir,
                                          {'facts': job.facts()})
    with patch.object(pp, '_multimodal_chat', return_value=reply) as call:
        invoke()
        assert invoke()['peak_verified'] == 1
        assert call.call_count == 1
        invoke('other')
        assert call.call_count == 2
        path = reverse._frames_by_name(job.overview)['review_002.png']['frame_path']
        Image.new('RGB', (80, 80), 'red').save(path)
        invoke()
        assert call.call_count == 3
        with patch.object(reverse, 'PASS_A_PROMPT_VERSION', 'next'):
            invoke()
        assert call.call_count == 4


def test_failed_peak_read_is_not_cached():
    job = _SheetJob(count=3)
    job.overview['change_events'] = [{'peak': 2,
        'evidence_frames': ['review_001.png', 'review_002.png', 'review_003.png']}]
    Path(job.dir, 'video_overview.json').write_text(json.dumps(job.overview))
    with patch.object(pp, '_multimodal_chat', side_effect=RuntimeError('offline')) as call:
        for _ in range(2):
            reverse.verify_peak_frames({'model': 'm'}, job.dir, {'facts': job.facts()})
        assert call.call_count == 2


def test_truncated_batch_splits_without_repeating_identical_request():
    job = _SheetJob(count=4)
    job.overview['analysis_plan'] = {'required_frames':
        [f'review_{i:03}.png' for i in range(1, 5)]}
    Path(job.dir, 'video_overview.json').write_text(json.dumps(job.overview))
    sizes = []
    def answer(config, system, text, paths, **kwargs):
        sizes.append(len(paths))
        if len(paths) > 2:
            raise pp.ResponseTruncated('length')
        import re
        names = re.findall(r'\d+\. (review_\d+\.png)', text)
        return json.dumps([{'frame': n, 'subject': 'wall', 'confidence': .9} for n in names])
    with patch.object(pp, '_multimodal_chat', side_effect=answer):
        result = reverse.extract_frame_facts({}, job.dir)
    assert sizes == [4, 2, 2]
    assert all(f['confidence'] > 0 for f in result['facts'])


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
