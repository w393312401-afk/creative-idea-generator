"""Decode real media: packet time bases must never determine merged timing."""
import array
import json
import math
import shutil
import subprocess
from pathlib import Path

import pytest

import video_generator as vg


pytestmark = pytest.mark.skipif(
    not shutil.which('ffmpeg') or not shutil.which('ffprobe'), reason='FFmpeg is required')


def _run(command):
    return subprocess.run(command, capture_output=True, check=True, timeout=30)


def _make_clip(path, color, *, size='160x120', fps=24, rate=None,
               timescale=12288, offset=0, audio_duration=2):
    command = ['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i',
               f'color=c={color}:s={size}:r={fps}:d=2']
    if rate:
        command += ['-f', 'lavfi', '-i',
                    f'sine=frequency=440:sample_rate={rate}:duration={audio_duration}']
    if offset:
        command += ['-vf', f'setpts=PTS+{offset}/TB']
        if rate:
            command += ['-af', f'asetpts=PTS+{offset}/TB']
    command += ['-c:v', 'libx264', '-threads', '1', '-pix_fmt', 'yuv420p',
                '-video_track_timescale', str(timescale)]
    if rate:
        command += ['-c:a', 'aac', '-ar', str(rate)]
    command.append(str(path))
    _run(command)


def _probe(path):
    result = _run(['ffprobe', '-v', 'error', '-show_streams', '-show_format',
                   '-of', 'json', str(path)])
    return json.loads(result.stdout)


def _frame_rgb(path, seconds):
    return _run(['ffmpeg', '-v', 'error', '-ss', str(seconds), '-i', str(path),
                 '-frames:v', '1', '-vf', 'crop=2:2:(iw-2)/2:(ih-2)/2',
                 '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-']).stdout[:3]


@pytest.mark.parametrize('speed', [1, 4])
def test_mixed_streams_keep_picture_audio_and_pacing_in_sync(tmp_path, speed):
    folder = tmp_path / 'videos'
    folder.mkdir()
    clips = [folder / f'vid_{index:03}.mp4' for index in range(1, 4)]
    _make_clip(clips[0], 'red')  # Later clips' audio must survive a silent first clip.
    _make_clip(clips[1], 'lime', size='120x160', fps=30, rate=44100,
               timescale=90000, offset=5, audio_duration=3)
    _make_clip(clips[2], 'blue', rate=48000, audio_duration=.5)
    manifest = {'title': 'mixed-media', 'videos': [
        {'slot': index + 1, 'status': 'success', 'file': str(path), 'clip_speed': pace}
        for index, (path, pace) in enumerate(zip(clips, (1, 1.25, .75)))]}
    (tmp_path / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')

    result = vg.merge_project_videos(
        str(tmp_path), speed=speed, cover_burn=False,
        config={'reviewsDisabled': True, 'videoMergeTimeoutSeconds': 30})
    output = Path(vg._BASE_DIR) / result['file']
    info = _probe(output)
    streams = {item['codec_type']: item for item in info['streams']}
    video, audio = streams['video'], streams['audio']
    expected = 6 / speed
    assert float(video['duration']) == pytest.approx(expected, abs=1 / 24)
    assert float(audio['duration']) == pytest.approx(expected, abs=.05)
    assert float(info['format']['duration']) == pytest.approx(expected, abs=.05)
    assert (video['width'], video['height'], video['r_frame_rate']) == (160, 120, '24/1')
    assert (audio['sample_rate'], audio['channels']) == ('48000', 2)
    assert int(video['nb_frames']) == round(expected * 24)

    # Check all three segments' actual decoded centers, including the shifted
    # input with a different picture size. No black clip or held prior frame.
    for seconds, channel in ((.5 / speed, 0), (3 / speed, 1), (5 / speed, 2)):
        pixel = _frame_rgb(output, seconds)
        assert len(pixel) == 3 and pixel[channel] > 180
        assert max(pixel[index] for index in range(3) if index != channel) < 40

    # The missing first audio is silence; the middle clip's tone is retained.
    amplitudes = []
    for seconds in (.3 / speed, 3 / speed):
        samples = array.array('h', _run([
            'ffmpeg', '-v', 'error', '-ss', str(seconds), '-i', str(output),
            '-t', str(.2 / speed), '-vn', '-ac', '1', '-f', 's16le', '-']).stdout)
        amplitudes.append(math.sqrt(sum(value * value for value in samples) / len(samples)))
    assert amplitudes[0] < 5
    assert amplitudes[1] > 100


def test_abnormal_audio_tail_is_rejected_before_publish(tmp_path, monkeypatch):
    folder = tmp_path / 'videos'
    folder.mkdir()
    source = folder / 'vid_001.mp4'
    _make_clip(source, 'red', rate=48000, audio_duration=3)
    previous = tmp_path / 'audio-tail_1x.mp4'
    previous.write_bytes(b'previous export')
    (tmp_path / 'manifest.json').write_text(json.dumps({
        'title': 'audio-tail', 'videos': [{'slot': 1, 'status': 'success', 'file': str(source)}]}))
    run_media = vg._run_media_command

    def produce_bad_output(command, **kwargs):
        if command[0] == 'ffmpeg':
            shutil.copyfile(source, command[-1])
            return subprocess.CompletedProcess(command, 0, '', '')
        return run_media(command, **kwargs)

    monkeypatch.setattr(vg, '_run_media_command', produce_bad_output)
    with pytest.raises(RuntimeError, match='音画时长不一致'):
        vg.merge_project_videos(str(tmp_path), speed=1, cover_burn=False,
                                config={'reviewsDisabled': True})
    assert previous.read_bytes() == b'previous export'
    assert not list(tmp_path.glob('.merge-*'))


def test_video_probe_uses_picture_duration_without_audio_tail(tmp_path):
    source = tmp_path / 'source.mp4'
    _make_clip(source, 'red', rate=44100, audio_duration=3)
    assert vg._ffprobe_video_params(str(source))['duration'] == pytest.approx(2)
    assert float(_probe(source)['format']['duration']) == pytest.approx(3)


def test_invalid_source_preserves_previous_export(tmp_path):
    folder = tmp_path / 'videos'
    folder.mkdir()
    source = folder / 'vid_001.mp4'
    source.write_bytes(b'not a video')
    previous = tmp_path / 'invalid_4x.mp4'
    previous.write_bytes(b'previous export')
    (tmp_path / 'manifest.json').write_text(json.dumps({
        'title': 'invalid', 'videos': [{'slot': 1, 'status': 'success', 'file': str(source)}]}))
    with pytest.raises(RuntimeError, match='无法读取视频片段'):
        vg.merge_project_videos(str(tmp_path), config={'reviewsDisabled': True})
    assert previous.read_bytes() == b'previous export'
    assert not list(tmp_path.glob('.merge-*'))
