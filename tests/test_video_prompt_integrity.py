from unittest.mock import MagicMock
import pytest

from integrations.google_fx.models import VideoRequest
from integrations.google_fx.services import google_fx_helpers as H


@pytest.mark.parametrize('word,corrupt', [
    ('and', 'a nd'), ('beds', 'be ds'), ('boots', 'boot s'),
    ('square', 's quare'), ('blue-hour', 'blue-ho ur'), ('central', 'centra l'),
])
def test_historical_midword_corruption_is_rejected(word, corrupt):
    editor = MagicMock()
    editor.evaluate.return_value = 'The worker uses ' + corrupt + '.'
    with pytest.raises(RuntimeError, match='PROMPT_TEXT_MISMATCH'):
        H._verify_video_prompt_before_send(editor, 'The worker uses ' + word + '.')


@pytest.mark.parametrize('actual', ['same prefix here truncated', 'same prefix here complete extra',
                                  'SAME prefix here complete', ''])
def test_full_text_not_prefix_is_required(actual):
    editor = MagicMock()
    editor.evaluate.return_value = actual
    assert not H._editor_prompt_matches(editor, 'same prefix here complete')


def test_layout_whitespace_is_allowed():
    editor = MagicMock()
    editor.evaluate.return_value = '\nfirst line\nsecond\u00a0line\n'
    H._verify_video_prompt_before_send(editor, 'first line second line')


def test_partial_text_is_not_appended_again():
    editor, page = MagicMock(), MagicMock()
    editor.evaluate.return_value = 'partial text'
    assert not H._fill_prompt_text(page, editor, 'complete prompt', has_refs=True)
    page.keyboard.insert_text.assert_not_called()
    page.keyboard.type.assert_not_called()


@pytest.mark.parametrize('corrupt', [True, False])
def test_submission_checks_text_after_pacing_without_extra_keystrokes(monkeypatch, corrupt):
    page, editor = MagicMock(), MagicMock()
    prompt = 'The worker uses concrete and pipes.'
    editor.evaluate.return_value = prompt
    monkeypatch.setattr(H, 'random_sleep', lambda *a: None)
    monkeypatch.setattr(H, '_clear_prompt_reference_chips_video', lambda *a: None)
    monkeypatch.setattr(H, '_find_fx_prompt_input', lambda *a, **k: editor)
    monkeypatch.setattr(H, '_fill_prompt_text', lambda *a, **k: True)
    def pacing(*args):
        if corrupt:
            editor.evaluate.return_value = prompt.replace('pipes', 'pi pes')
    monkeypatch.setattr(H, 'fx_pacing_wait', pacing)
    monkeypatch.setattr(H, 'fx_pacing_bounds', lambda: (0, 0))
    send = MagicMock()
    monkeypatch.setattr(H, 'click_fx_send_button', send)
    monkeypatch.setattr(H, 'note_fx_submit', lambda: None)
    monkeypatch.setattr(H, '_wait_for_new_tile_id', lambda *a, **k: 'new-tile')
    req = VideoRequest(prompt=prompt)
    if corrupt:
        with pytest.raises(RuntimeError, match='PROMPT_TEXT_MISMATCH'):
            H._submit_video_to_canvas(page, req, [])
        send.assert_not_called()
    else:
        assert H._submit_video_to_canvas(page, req, [])['tile_id'] == 'new-tile'
        send.assert_called_once()
    page.keyboard.type.assert_not_called()
    page.keyboard.press.assert_not_called()


def test_media_identity_is_kept_in_manifest_record(monkeypatch, tmp_path):
    from video_generator import _BatchBridge
    generated = tmp_path / 'generated.mp4'
    generated.write_bytes(b'test-video')
    writer = MagicMock()
    plan = dict(slot=8, seq=1, prompt='p', dest_path=str(tmp_path / 'vid_008.mp4'),
                start_frame='first.webp', end_frame='last.webp')
    bridge = _BatchBridge([{'plan': plan}], 1, 'omni', writer, None)
    monkeypatch.setattr('video_generator.verify_video_anchors', lambda *a, **k: (True, 'ok'))
    bridge(0, 'video_done', dict(video_url=str(generated),
                               flow_media_id='verified-output', flow_project_url='original-project'))
    record = writer.record.call_args.args[0]
    assert record['flow_media_id'] == 'verified-output'
    assert record['flow_project_url'] == 'original-project'
