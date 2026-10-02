"""The FX console must keep the reason beneath a multi-line failure header."""

import pytest

import server


FAILED_VIDEO = [
    '[09:25:52] │ ℹ️ │ GoogleFX-Video │ [videos_a] ❌ 任务 1 生成失败: warning',
    'Failed',
    'We noticed some unusual activity. Please visit the Help Center for more information.',
    'You have not been charged for this generation.',
    'refresh',
    'undo',
    'delete_forever',
]


@pytest.fixture
def log_file(tmp_path, monkeypatch):
    path = tmp_path / 'fx.log'
    monkeypatch.setattr(server, '_fx_log_path', lambda: str(path))

    def write(lines):
        path.write_text('\n'.join(lines) + '\n', encoding='utf-8')

    return write


def test_task_filter_keeps_complete_failure_record(log_file):
    log_file(FAILED_VIDEO + [
        '[09:25:53] │ ℹ️ │ GoogleFX-Video │ [videos_b] 另一个任务',
        '另一个任务的详情',
    ])

    assert server._fx_log_tail(task_id='videos_a') == FAILED_VIDEO


@pytest.mark.parametrize('keyword', ['UNUSUAL ACTIVITY', 'not been charged', 'warning'])
def test_keyword_matches_whole_record_and_keeps_its_context(log_file, keyword):
    log_file(FAILED_VIDEO)

    assert server._fx_log_tail(task_id='videos_a', keyword=keyword) == FAILED_VIDEO


@pytest.mark.parametrize('boundary', [
    '09:25:53.001 [INFO ] [COMPOSE] [task=text_b] another module',
    '[LLM] another module',
    'LOG: "GET /api/status HTTP/1.1" 200 -',
    '127.0.0.1 - - [25/Sep/2026 09:25:53] "GET / HTTP/1.1" 200 -',
    'Warning: unrelated stdout warning',
    '===== SPARK server log opened 2026-09-25T09:25:53 =====',
])
def test_mixed_stdout_cannot_be_inherited_by_previous_fx_record(log_file, boundary):
    log_file(FAILED_VIDEO + [boundary, 'unrelated continuation',
                           '[09:25:54] │ ℹ️ │ GoogleFX │ [videos_b] next task'])

    assert server._fx_log_tail(task_id='videos_a') == FAILED_VIDEO
    assert server._fx_log_tail(task_id='videos_a', keyword='unrelated') == []


def test_structured_stdout_uses_exact_task_and_module_ownership(log_file):
    target = [
        '09:25:53.001 [ERROR] [VIDEOS] [task=videos_a] failed',
        'detail mentioning a second task videos_b',
    ]
    other_module = [
        '09:25:54.001 [INFO ] [COMPOSE] [task=videos_a] mentioning GoogleFX',
        'other module continuation',
    ]
    log_file(target + other_module + [
        '[09:25:55] │ ℹ️ │ GoogleFX │ [videos_ab] mentioning videos_a',
        'videos_a appears in another task body',
    ])

    assert server._fx_log_tail(task_id='videos_a') == target
    assert server._fx_log_tail(task_id='videos_a', fx_only=False) == target + other_module
    assert server._fx_log_tail(task_id='videos_b', fx_only=False) == []


def test_legacy_stdout_module_and_task_remain_filterable(log_file):
    record = ['[VIDEOS] [task=videos_a] legacy stdout', 'legacy detail']
    log_file(record + ['[LLM] [task=text_a] non-FX', 'detail mentioning VIDEOS'])

    assert server._fx_log_tail(task_id='videos_a') == record
    assert server._fx_log_tail() == record


def test_limit_selects_latest_complete_records_in_chronological_order(log_file):
    newest = ['[09:25:54] │ ℹ️ │ GoogleFX │ [videos_a] latest', 'latest detail']
    log_file(['[09:25:50] │ ℹ️ │ GoogleFX │ [videos_a] oldest'] + FAILED_VIDEO + newest)

    assert server._fx_log_tail(task_id='videos_a', limit=1) == newest
    assert server._fx_log_tail(task_id='videos_a', limit=2) == FAILED_VIDEO + newest
    assert server._fx_log_tail(task_id='videos_a', keyword='unusual', limit=1) == FAILED_VIDEO
    assert server._fx_log_tail(task_id='videos_a', limit=0) == newest


def test_limit_is_still_capped_at_800_records(log_file):
    lines = [f'[09:25:50] │ ℹ️ │ GoogleFX │ [videos_a] message {idx}' for idx in range(805)]
    log_file(lines)

    assert server._fx_log_tail(task_id='videos_a', limit=900) == lines[-800:]


def test_read_window_orphan_continuations_have_no_task_or_module(log_file, monkeypatch):
    newest = ['[09:25:54] │ ℹ️ │ GoogleFX │ [videos_b] latest']
    log_file(FAILED_VIDEO + ['orphan continuation'] * 60 + newest)
    monkeypatch.setattr(server, '_LOG_TAIL_BYTES', 100)

    assert server._fx_log_tail() == newest
    assert server._fx_log_tail(task_id='videos_a') == []


def test_ansi_stdout_headers_still_split_records(log_file):
    record = ['\x1b[92m[09:25:52] │ ℹ️ │ GoogleFX │ [videos_a] failure',
              'failure detail\x1b[0m']
    log_file(record + ['\x1b[91m[LLM] unrelated\x1b[0m', 'unrelated detail'])

    assert server._fx_log_tail(task_id='videos_a') == record


def test_level_filter_keeps_multiline_error_and_warning_context(log_file):
    warning = ['[09:25:53] │ ℹ️ │ GoogleFX-Video │ [videos_a] ⚠️ 下载重试', 'retry detail']
    recovered = ['[09:25:54] │ ℹ️ │ GoogleFX-Video │ [videos_a] ✅ 失败后已恢复',
                 '上一次 ❌ 生成失败，当前结果正常']
    log_file(FAILED_VIDEO + warning + recovered)

    assert server._fx_log_tail(level='error') == FAILED_VIDEO
    assert server._fx_log_tail(level='warning') == FAILED_VIDEO + warning
    assert server._fx_log_tail(level='all') == FAILED_VIDEO + warning + recovered
    assert server._fx_log_tail(level='error', task_id='videos_a', keyword='NOT BEEN CHARGED') == FAILED_VIDEO
    assert server._fx_log_tail(level='warning', limit=1) == warning
    assert server._fx_log_records(level='all') == [
        {'level': 'error', 'lines': FAILED_VIDEO},
        {'level': 'warning', 'lines': warning},
        {'level': 'info', 'lines': recovered},
    ]


@pytest.mark.parametrize('header,expected', [
    ('[09:25:52] │ ❌ │ Error │ [videos_a] 人工处理等待超时', 'error'),
    ('[09:25:52] │ ⚠️ │ Warning │ [videos_a] 即将重试', 'warning'),
    ('[09:25:52] │ ℹ️ │ GoogleFX │ [videos_a] ⛔ 账号已耗尽', 'error'),
    ('[09:25:52] │ ℹ️ │ GoogleFX │ [videos_a] 🚨 致命问题', 'error'),
    ('09:25:52.001 [ERROR] [VIDEOS] [task=videos_a] failure', 'error'),
    ('09:25:52.001 [CRITICAL] [VIDEOS] [task=videos_a] failure', 'error'),
    ('09:25:52.001 [WARN ] [FRAMES] [task=videos_a] retry', 'warning'),
    ('09:25:52.001 [DEBUG] [VIDEOS] [task=videos_a] diagnostic', 'debug'),
    ('09:25:52.001 [INFO ] [VIDEOS] [task=videos_a] 失败后已恢复', 'info'),
    ('[VIDEOS] [task=videos_a] ⚠️ legacy warning', 'warning'),
])
def test_record_levels_follow_explicit_headers_and_leading_icons(log_file, header, expected):
    lines = [header, 'full diagnostic context']
    log_file(lines)

    assert server._fx_log_records(task_id='videos_a') == [{'level': expected, 'lines': lines}]
    assert server._fx_log_tail(level='error') == (lines if expected == 'error' else [])
    assert server._fx_log_tail(level='warning') == (lines if expected in ('error', 'warning') else [])


def test_generic_non_fx_error_module_still_requires_all_modules(log_file):
    unrelated = ['09:25:52.001 [ERROR] [COMPOSE] [task=videos_a] model failed', 'detail']
    log_file(unrelated)
    assert server._fx_log_tail(level='error') == []
    assert server._fx_log_tail(level='error', fx_only=False) == unrelated


def test_severity_filter_reads_a_wider_tail(log_file, monkeypatch):
    monkeypatch.setattr(server, '_LOG_TAIL_BYTES', 140)
    recent_info = ['[09:25:53] │ ℹ️ │ GoogleFX │ [videos_a] ' + 'ordinary progress ' * 12]
    log_file(FAILED_VIDEO + recent_info)
    assert server._fx_log_tail(level='error') == FAILED_VIDEO


def test_logs_api_preserves_legacy_lines_and_adds_records(log_file):
    log_file(FAILED_VIDEO + ['[09:25:53] │ ℹ️ │ GoogleFX │ [videos_b] other task'])
    handler = object.__new__(server.SparkRequestHandler)
    handler.path = '/api/google-fx/logs?task_id=videos_a&level=error&q=unusual&limit=1'
    handler._gate = lambda: True
    responses = []
    handler._send_json = lambda payload, status=200: responses.append((status, payload))
    handler.do_GET()
    assert responses == [(200, {'status': 'ok', 'lines': FAILED_VIDEO,
                               'records': [{'level': 'error', 'lines': FAILED_VIDEO}]})]


def test_logs_api_rejects_unknown_level(log_file):
    log_file(FAILED_VIDEO)
    handler = object.__new__(server.SparkRequestHandler)
    handler.path = '/api/google-fx/logs?level=not-a-level'
    handler._gate = lambda: True
    responses = []
    handler._send_json = lambda payload, status=200: responses.append((status, payload))
    handler.do_GET()
    assert responses[0][0] == 400
    assert responses[0][1]['status'] == 'error'


def test_missing_log_is_empty_but_read_failure_is_not_an_empty_result(tmp_path, log_file, monkeypatch):
    monkeypatch.setattr(server, '_fx_log_path', lambda: str(tmp_path / 'missing.log'))
    assert server._fx_log_records() == []
    path = tmp_path / 'unreadable.log'
    path.write_text('fixture', encoding='utf-8')
    monkeypatch.setattr(server, '_fx_log_path', lambda: str(path))

    def denied(*args, **kwargs):
        raise PermissionError('private path should not be exposed')

    monkeypatch.setattr(server, 'open', denied, raising=False)
    with pytest.raises(OSError, match='日志读取失败'):
        server._fx_log_records()
    handler = object.__new__(server.SparkRequestHandler)
    handler.path = '/api/google-fx/logs?level=error'
    handler._gate = lambda: True
    responses = []
    handler._send_json = lambda payload, status=200: responses.append((status, payload))
    handler.do_GET()
    assert responses == [(500, {'status': 'error', 'message': '日志读取失败，请稍后重试'})]
