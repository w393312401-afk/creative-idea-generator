"""Replica lifecycle regressions; all assets and library writes are isolated."""
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

import replica_pipeline as rp


@pytest.fixture
def job(tmp_path, monkeypatch):
    monkeypatch.setattr(rp.server_common, 'OUTPUT_ROOT', str(tmp_path))
    monkeypatch.setattr(rp, '_probe', lambda path: {'duration_sec': 5})
    return rp.ingest_video(b'source-video', 'source.mp4')


def test_status_refresh_cannot_bypass_delta_audit(job):
    job.update(stage='audit_failed', prompt_block='VIDEO 1: install a floor',
               beats={'beats': [{'id': 'B01'}]})
    rp._save_state(job)
    issues = [{'severity': 'blocking', 'message': 'unaccounted floor'}]
    with patch('prompt_pipeline.object_ledger.validate_video_objects', return_value=issues), \
         patch('prompt_pipeline.object_ledger.format_violations', return_value='floor'), \
         patch.object(rp, '_publish_to_library') as publish:
        state = rp.get_replica_status(job['job_id'])
    assert state['stage'] == 'audit_failed'
    assert state['video_delta_issues'] == issues
    publish.assert_not_called()


def test_archived_video_upload_creates_usable_job(job):
    rp.archive_replica_job(job['job_id'])
    new = rp.ingest_video(b'source-video', 'source.mp4')
    assert new['job_id'] != job['job_id']
    assert os.path.isfile(new['video_path'])


def test_missing_video_is_not_reused(job):
    os.remove(job['video_path'])
    new = rp.ingest_video(b'source-video', 'source.mp4')
    assert new['job_id'] != job['job_id']


@pytest.mark.parametrize('operation', [rp.archive_replica_job, rp.delete_replica_job])
@pytest.mark.parametrize('parent_field', ['parent_baseline_id', 'variant_of'])
def test_dependent_assets_are_preserved(job, operation, parent_field):
    child = rp.ingest_video(b'child', 'child.mp4')
    child[parent_field] = job['job_id']
    rp._save_state(child)
    with pytest.raises(ValueError, match='依赖'):
        operation(job['job_id'])
    assert os.path.isfile(job['video_path'])


def test_lock_preserves_original_exception(job):
    with pytest.raises(ValueError, match='original failure'):
        with rp.state_lock(job['job_id']):
            raise ValueError('original failure')


def test_lock_serializes_windows_threads(job, monkeypatch):
    monkeypatch.setattr(rp, 'fcntl', None)
    attempted = threading.Event()
    acquired = threading.Event()

    def contender():
        attempted.set()
        with rp.state_lock(job['job_id']):
            acquired.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with rp.state_lock(job['job_id']):
            future = pool.submit(contender)
            assert attempted.wait(2)
            assert not acquired.wait(.1)
        future.result(timeout=2)
    assert acquired.is_set()


def test_slot_bodies_accepts_string_lists():
    assert rp._slot_bodies(['first', {'index': 3, 'body': 'third'}]) == {1: 'first', 3: 'third'}
