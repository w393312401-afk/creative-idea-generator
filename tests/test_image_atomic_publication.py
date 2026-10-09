"""Readers should see the previous complete image until its replacement is ready."""
import base64
import io
from pathlib import Path

import pytest
from PIL import Image

import frame_generator


def _png_bytes(color):
    buffer = io.BytesIO()
    Image.new('RGB', (128, 128), color).save(buffer, format='PNG')
    return buffer.getvalue()


def _data_item(image_bytes):
    return {'b64_json': base64.b64encode(image_bytes).decode('ascii')}


def _assert_destination(target, previous):
    if previous is None:
        assert not target.exists()
    else:
        assert target.read_bytes() == previous


def _watch_publication(monkeypatch, target, previous, *, failure=None):
    """Observe the public destination while a private file is partly written."""
    original_tempfile = frame_generator.tempfile.NamedTemporaryFile
    original_replace = frame_generator.os.replace
    observations = []
    written = []

    class ObservedTemporary:
        def __init__(self, temporary):
            self.temporary = temporary
            self.name = temporary.name

        def __enter__(self):
            self.temporary.__enter__()
            return self

        def __exit__(self, *args):
            return self.temporary.__exit__(*args)

        def write(self, data):
            temporary_path = Path(self.name)
            assert temporary_path.parent == target.parent
            assert temporary_path != target
            halfway = len(data) // 2
            self.temporary.write(data[:halfway])
            self.temporary.flush()
            assert temporary_path.read_bytes() == data[:halfway]
            _assert_destination(target, previous)
            observations.append('partial write')
            if failure == 'write':
                raise OSError('simulated disk full')
            self.temporary.write(data[halfway:])
            self.temporary.flush()
            _assert_destination(target, previous)
            observations.append('complete private write')
            written.append(data)
            return len(data)

    def temporary_file(*args, **kwargs):
        return ObservedTemporary(original_tempfile(*args, **kwargs))

    def replace(source, destination):
        assert Path(destination) == target
        assert Path(source).parent == target.parent
        assert Path(source).read_bytes() == written[-1]
        _assert_destination(target, previous)
        observations.append('before publication')
        if failure == 'replace':
            raise OSError('simulated publication failure')
        original_replace(source, destination)
        assert target.read_bytes() == written[-1]
        observations.append('published')

    monkeypatch.setattr(frame_generator.tempfile, 'NamedTemporaryFile', temporary_file)
    monkeypatch.setattr(frame_generator.os, 'replace', replace)
    return observations


@pytest.mark.parametrize('existing', [False, True])
def test_webp_is_encoded_and_written_before_publication(tmp_path, monkeypatch, existing):
    target = tmp_path / 'cover.webp'
    previous = _png_bytes('blue') if existing else None
    if previous is not None:
        target.write_bytes(previous)
    source = _png_bytes('red')
    observations = _watch_publication(monkeypatch, target, previous)
    original_save = Image.Image.save

    def observed_encode(image, destination, *args, **kwargs):
        assert isinstance(destination, io.BytesIO)
        _assert_destination(target, previous)
        observations.append('before encoding')
        original_save(image, destination, *args, **kwargs)
        _assert_destination(target, previous)
        observations.append('after encoding')

    monkeypatch.setattr(Image.Image, 'save', observed_encode)
    frame_generator._decode_or_download_image(
        _data_item(source), str(target), {'imageAspectRatio': '9:16'})

    assert observations == [
        'before encoding', 'after encoding', 'partial write',
        'complete private write', 'before publication', 'published',
    ]
    with Image.open(target) as image:
        assert image.format == 'WEBP'
        assert image.size == (72, 128)
        image.load()
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize('failure', ['write', 'replace'])
def test_failed_publication_preserves_old_image_and_removes_temporary(
    tmp_path, monkeypatch, failure,
):
    target = tmp_path / 'cover.webp'
    previous = _png_bytes('blue')
    target.write_bytes(previous)
    observations = _watch_publication(monkeypatch, target, previous, failure=failure)

    with pytest.raises(OSError, match='simulated'):
        frame_generator._decode_or_download_image(
            _data_item(_png_bytes('red')), str(target), {'imageAspectRatio': 'auto'})

    assert observations[0] == 'partial write'
    assert 'published' not in observations
    assert target.read_bytes() == previous
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize('conversion', ['raw destination', 'failed webp conversion'])
def test_raw_bytes_are_published_atomically(tmp_path, monkeypatch, conversion):
    target = tmp_path / ('cover.png' if conversion == 'raw destination' else 'cover.webp')
    previous = _png_bytes('blue')
    target.write_bytes(previous)
    source = _png_bytes('red')
    observations = _watch_publication(monkeypatch, target, previous)
    if conversion == 'failed webp conversion':
        def failed_encode(image, destination, *args, **kwargs):
            assert isinstance(destination, io.BytesIO)
            destination.write(b'partially encoded webp')
            _assert_destination(target, previous)
            raise OSError('simulated encoder failure')

        monkeypatch.setattr(Image.Image, 'save', failed_encode)

    frame_generator._decode_or_download_image(
        _data_item(source), str(target), {'imageAspectRatio': 'auto'})

    assert observations == [
        'partial write', 'complete private write', 'before publication', 'published',
    ]
    assert target.read_bytes() == source
    assert list(tmp_path.iterdir()) == [target]
