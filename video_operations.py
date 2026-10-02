"""Durable request identities and remote submission receipts for video generation."""
import contextlib
import hashlib
import json
import os
import re
import sqlite3
import time
import uuid


class VideoOperationConflict(ValueError):
    pass


def validate_request_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{7,127}', value):
        raise ValueError('request_id 格式无效')
    return value


def request_fingerprint(body, route):
    snapshot = {k: v for k, v in body.items() if k != 'request_id'}
    encoded = json.dumps([route, snapshot], ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest()


class VideoOperationStore:
    def __init__(self, path=None):
        self.path = path

    @contextlib.contextmanager
    def connect(self):
        if self.path is None:
            import server_common
            path = os.path.join(server_common.TASKS_DIR, 'video_operations.sqlite3')
        else:
            path = self.path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        conn = sqlite3.connect(path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS operations (
                    request_id TEXT PRIMARY KEY, task_id TEXT UNIQUE NOT NULL,
                    fingerprint TEXT NOT NULL, created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS cancellations (
                    request_id TEXT PRIMARY KEY, created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS submissions (
                    task_id TEXT NOT NULL, receipt_id TEXT NOT NULL, payload TEXT NOT NULL,
                    created_at REAL NOT NULL, PRIMARY KEY(task_id, receipt_id));
            ''')
            yield conn
        finally:
            conn.close()

    def lookup(self, request_id):
        validate_request_id(request_id)
        with self.connect() as conn:
            row = conn.execute('SELECT * FROM operations WHERE request_id=?', (request_id,)).fetchone()
            cancelled = conn.execute('SELECT 1 FROM cancellations WHERE request_id=?', (request_id,)).fetchone()
            if row:
                return {**dict(row), 'cancel_requested': bool(cancelled)}
            if cancelled:
                return {'request_id': request_id, 'task_id': None, 'cancel_requested': True}
            return None

    def reserve(self, request_id, fingerprint, prefix='videos'):
        validate_request_id(request_id)
        with self.connect() as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM operations WHERE request_id=?', (request_id,)).fetchone()
            cancelled = conn.execute('SELECT 1 FROM cancellations WHERE request_id=?', (request_id,)).fetchone()
            if row:
                if row['fingerprint'] != fingerprint:
                    raise VideoOperationConflict('同一 request_id 不能用于不同的视频生成请求')
                return {**dict(row), 'cancel_requested': bool(cancelled)}, False
            if cancelled:
                return {'request_id': request_id, 'task_id': None, 'cancel_requested': True}, False
            row = {'request_id': request_id, 'task_id': f'{prefix}_{uuid.uuid4().hex}',
                   'fingerprint': fingerprint, 'created_at': time.time()}
            conn.execute('INSERT INTO operations VALUES (:request_id,:task_id,:fingerprint,:created_at)', row)
            return row, True

    def cancel(self, request_id):
        validate_request_id(request_id)
        with self.connect() as conn, conn:
            conn.execute('INSERT OR IGNORE INTO cancellations VALUES (?,?)', (request_id, time.time()))
        return self.lookup(request_id)

    def record_submission(self, task_id, details):
        # Keep identity/provenance only, never credentials or full prompt text.
        allowed = ('slot', 'current', 'total', 'account_id', 'project_url', 'tile_id', 'media_id',
                   'prompt_hash', 'start_uuid', 'end_uuid', 'refs', 'confirmed', 'submission_pending',
                   'submission_id')
        payload = {k: details[k] for k in allowed if k in details}
        if 'slot' not in payload and 'index' in details:
            payload['slot'] = details['index']
        identity = {k: payload.get(k) for k in ('slot', 'account_id', 'project_url', 'tile_id', 'media_id', 'prompt_hash', 'submission_id')}
        receipt_id = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        with self.connect() as conn, conn:
            conn.execute('INSERT OR REPLACE INTO submissions VALUES (?,?,?,?)',
                         (task_id, receipt_id, json.dumps(payload, ensure_ascii=False), time.time()))
        return payload

    def submissions(self, task_id):
        with self.connect() as conn:
            return [json.loads(r['payload']) for r in conn.execute(
                'SELECT payload FROM submissions WHERE task_id=? ORDER BY created_at', (task_id,))]
