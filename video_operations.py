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


def safe_native_submission_diagnostic(value):
    """Retain native request evidence without browser messages or page content."""
    if not isinstance(value, dict):
        return None
    result = {}
    for key in ('stage', 'reason', 'failure_kind'):
        item = value.get(key)
        if isinstance(item, str) and re.fullmatch(r'[a-z][a-z0-9_]{0,63}', item):
            result[key] = item
    error_kind = value.get('error_kind')
    if isinstance(error_kind, str) and error_kind in {'TimeoutError', 'Error', 'TargetClosedError', 'BrowserCaptchaError', 'RuntimeError', 'TypeError', 'ValueError', 'CancelledError', 'other', 'timeout', 'browser_operation_failed'}:
        result['error_kind'] = error_kind
    for key in ('armed', 'request_admitted', 'forwarding_started', 'request_forwarded', 'page_closed'):
        if type(value.get(key)) is bool:
            result[key] = value[key]
    for key in ('submit_attempts', 'elapsed_ms'):
        if type(value.get(key)) is int and 0 <= value[key] <= 3600000:
            result[key] = value[key]
    return result or None


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
                   'submission_id', 'project_key', 'provider', 'request_id', 'fixed_video_account',
                   'api_model', 'operation_id', 'upstream_task_id', 'upstream_account_id',
                   'client_submission_id', 'upstream_accepted', 'upstream_error_code', 'upstream_reason',
                   'recovery_state', 'recovered_at')
        payload = {k: details[k] for k in allowed if k in details}
        diagnostic = safe_native_submission_diagnostic(details.get('native_submission_diagnostic'))
        if diagnostic:
            payload['native_submission_diagnostic'] = diagnostic
        if 'slot' not in payload and 'index' in details:
            payload['slot'] = details['index']
        identity = {k: payload.get(k) for k in ('slot', 'account_id', 'project_url', 'tile_id', 'media_id', 'prompt_hash', 'submission_id')}
        if payload.get('submission_id') and (payload.get('fixed_video_account') is True
                                            or payload.get('provider') == 'flow2api'):
            # Native receipts gain tile/media IDs after clicking. Their stable
            # attempt ID must update the same pending record through resolution.
            identity = {k: payload.get(k) for k in ('slot', 'submission_id')}
        receipt_id = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        with self.connect() as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            # Flow gains its upstream identity after dispatch. Updating that
            # identity must resolve the original row, rather than leave behind
            # an earlier pending row with fewer fields.
            previous_rows = []
            if payload.get('submission_id'):
                previous_rows = conn.execute(
                    "SELECT receipt_id,payload FROM submissions WHERE task_id=? "
                    "AND json_extract(payload,'$.submission_id')=? "
                    "AND json_extract(payload,'$.slot')=? ORDER BY created_at",
                    (task_id, payload['submission_id'], payload.get('slot'))).fetchall()
            previous = previous_rows[-1] if previous_rows else conn.execute(
                'SELECT payload FROM submissions WHERE task_id=? AND receipt_id=?',
                (task_id, receipt_id)).fetchone()
            if previous:
                # A resolution from an older caller must not erase durable scope.
                old = json.loads(previous['payload'])
                for key in allowed:
                    if key not in payload and key in old:
                        payload[key] = old[key]
                if 'native_submission_diagnostic' not in payload:
                    diagnostic = safe_native_submission_diagnostic(old.get('native_submission_diagnostic'))
                    if diagnostic:
                        payload['native_submission_diagnostic'] = diagnostic
                if old.get('submission_pending') is False and payload.get('submission_pending') is True:
                    # Late transport/progress events cannot undo definitive
                    # settlement of this exact paid submission.
                    payload['submission_pending'] = False
                    payload['confirmed'] = old.get('confirmed', False)
                if payload.get('provider') == 'flow2api' or payload.get('fixed_video_account') is True:
                    receipt_id = hashlib.sha256(json.dumps(
                        {k: payload.get(k) for k in ('slot', 'submission_id')}, sort_keys=True).encode()).hexdigest()
            for row in previous_rows:
                conn.execute('DELETE FROM submissions WHERE task_id=? AND receipt_id=?',
                             (task_id, row['receipt_id']))
            conn.execute('INSERT OR REPLACE INTO submissions VALUES (?,?,?,?)',
                         (task_id, receipt_id, json.dumps(payload, ensure_ascii=False), time.time()))
        return payload

    def submissions(self, task_id):
        with self.connect() as conn:
            return [json.loads(r['payload']) for r in conn.execute(
                'SELECT payload FROM submissions WHERE task_id=? ORDER BY created_at', (task_id,))]

    def project_submissions(self, project_key):
        """Receipts survive task cleanup; scope is separate from receipt identity."""
        with self.connect() as conn:
            return [(row['task_id'], json.loads(row['payload'])) for row in conn.execute(
                "SELECT task_id,payload FROM submissions WHERE json_extract(payload,'$.project_key')=? "
                'ORDER BY created_at', (project_key,))]
