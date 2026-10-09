"""beat_pack.jobs：排队、后台执行、取消、续跑、重启对账、入库与请求校验。"""
import json
import threading
import time
import types

import pytest

import beat_pack
import beat_pack_fixtures as fx
from beat_pack import jobs

CONFIG = {'model': 'gpt-6.1-sol', 'apiKey': 'SECRET-MAIN-KEY', 'claudeApiKey': 'SECRET-CLAUDE-KEY',
          'claudeBaseUrl': 'http://claude.test/v1'}
BODY = {'theme': '巨型干松果改成雪林里的完整住所', 'segments': 28}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, 'BASE_DIR', tmp_path / 'beat_packs')
    monkeypatch.setattr(jobs, '_RECONCILED', False)
    monkeypatch.setattr(jobs, '_JOBS', {})
    import server_common
    monkeypatch.setattr(server_common, 'SERVER_CONFIG', {})            # 不读开发机上真实的网关配置
    model = fx.FakeModel()
    monkeypatch.setattr(jobs, '_make_chat', lambda live, name, on_receive: model)
    return types.SimpleNamespace(model=model, root=tmp_path / 'beat_packs')


def wait(job_id, want=('completed', 'failed', 'cancelled'), timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        job = jobs.get_job(job_id)
        if job['status'] in want:
            return job
        time.sleep(0.02)
    raise AssertionError(f'任务 {job_id} 没有在 {timeout}s 内到达 {want}：{jobs.get_job(job_id)}')


def library_titles():
    from server_common import read_library_index
    return [row['title'] for row in read_library_index() or []]


def test_a_job_runs_to_completion_and_lands_in_the_library(env):
    job = jobs.start(BODY, CONFIG)
    assert job['status'] in ('queued', 'running') and job['model'] == 'claude-opus-5-5'     # 全局是 GPT：回落到默认 Claude
    done = wait(job['id'])
    assert done['status'] == 'completed' and done['progress'] == 1.0
    result = done['result']
    assert (result['images'], result['videos']) == (29, 28) and result['validation'] == 'needs_review'
    assert result['import']['status'] == 'imported' and result['import']['title'] == '松果屋_雪林住所'
    assert library_titles() == ['松果屋_雪林住所']
    folder = env.root / job['id']
    assert (folder / 'bible.json').is_file() and (folder / 'rows.json').is_file()
    delivery = folder / result['dir'] / 'delivery-v1'
    assert (delivery / '完整提示词.txt').is_file() and 'delivery-v1/完整提示词.txt' in result['files']
    assert done['events'][0]['message'].startswith('已创建任务') and done['events'][-1]['message'].startswith('完成')
    assert done['stats']['calls'] == 8 and done['title'] == '松果屋_雪林住所'


def test_the_imported_project_carries_the_generated_prompts(env):
    from server_common import read_library_item, read_library_index
    job = wait(jobs.start(BODY, CONFIG)['id'])
    entry = read_library_index()[0]
    item = read_library_item(entry['id'])
    assert item['creativity'] == 'Claude 生成' and item['image_count'] == 29 and item['video_count'] == 28
    assert item['project_key'].startswith('run_import_') and item['project_key'].endswith('__松果屋_雪林住所')
    assert item['prompt_block'] == jobs.prompt_text(job['id'])
    assert len(item['prompt_slots']['images']) == 29 and len(item['prompt_slots']['videos']) == 28
    assert item['imported_source_label'] == f"beat_pack:{job['id']}"


def test_no_secret_is_ever_written_to_disk(env):
    job = wait(jobs.start(BODY, CONFIG)['id'])
    leaked = [str(p) for p in env.root.rglob('*') if p.is_file() and any(
        secret.encode() in p.read_bytes() for secret in ('SECRET-MAIN-KEY', 'SECRET-CLAUDE-KEY', 'claude.test'))]
    assert leaked == [] and job['status'] == 'completed'


def test_auto_import_can_be_off_and_the_job_can_be_imported_later_once(env):
    job = wait(jobs.start({**BODY, 'auto_import': False}, CONFIG)['id'])
    assert job['result']['import'] is None and library_titles() == []
    imported = jobs.import_job(job['id'])
    assert imported['result']['import']['title'] == '松果屋_雪林住所' and library_titles() == ['松果屋_雪林住所']
    with pytest.raises(beat_pack.BeatPackError) as caught:
        jobs.import_job(job['id'])
    assert caught.value.code == 'duplicate_title' and caught.value.status == 409 and library_titles() == ['松果屋_雪林住所']


def test_a_taken_title_does_not_fail_the_job_and_can_be_renamed(env):
    from server_common import write_library_item
    write_library_item({'id': 'existing', 'title': '松果屋_雪林住所', 'project_key': 'k', 'prompt_block': ''})
    job = wait(jobs.start(BODY, CONFIG)['id'])
    assert job['status'] == 'completed' and job['result']['import']['status'] == 'refused'
    assert job['result']['import']['code'] == 'duplicate_title'
    assert any('换个标题' in e['message'] for e in job['events'])
    renamed = jobs.import_job(job['id'], title='松果屋 雪林住所(二)')
    assert renamed['result']['import']['title'] == '松果屋雪林住所(二)'
    assert library_titles().count('松果屋_雪林住所') == 1 and '松果屋雪林住所(二)' in library_titles()


def test_a_custom_title_overrides_the_generated_one(env):
    job = wait(jobs.start({**BODY, 'title': '我的 松果/屋'}, CONFIG)['id'])
    assert job['result']['import']['title'] == '我的松果屋' and library_titles() == ['我的松果屋']


def test_cancelling_a_running_job_keeps_progress_and_resume_finishes_it(env):
    holder = {}
    calls = {'rows': 0}

    def cancel_on_third_batch(kind, label):
        if kind == 'rows':
            calls['rows'] += 1
            if calls['rows'] == 3:
                while 'id' not in holder:
                    time.sleep(0.001)
                jobs.cancel(holder['id'])

    env.model.on_call = cancel_on_third_batch
    holder['id'] = jobs.start(BODY, CONFIG)['id']
    stopped = wait(holder['id'])
    assert stopped['status'] == 'cancelled' and stopped['resumable'] is True
    saved = json.loads((env.root / holder['id'] / 'rows.json').read_text(encoding='utf-8'))['rows']
    assert len(saved) == 15                              # 第 3 批那次调用已经发出并返回，其结果通过校验后保存
    env.model.on_call = None
    resumed = jobs.resume(holder['id'], CONFIG)
    assert resumed['status'] == 'queued'
    done = wait(holder['id'])
    assert done['status'] == 'completed' and env.model.count('design') == 1 and env.model.count('rows') == 6
    assert library_titles() == ['松果屋_雪林住所']


def test_a_queued_job_can_be_cancelled_before_it_starts(env):
    gate, entered = threading.Event(), threading.Event()

    def block(kind, label):
        entered.set()
        gate.wait(10)

    env.model.on_call = block
    first = jobs.start(BODY, CONFIG)
    assert entered.wait(10)
    second = jobs.start({**BODY, 'theme': '另一个主题：巨型海螺壳改成潮岸住所'}, CONFIG)
    assert jobs.get_job(second['id'])['status'] == 'queued'
    assert jobs.cancel(second['id'])['status'] == 'cancelled'
    env.model.on_call = None
    gate.set()
    assert wait(first['id'])['status'] == 'completed'
    assert jobs.get_job(second['id'])['status'] == 'cancelled'            # worker 取到它时发现已取消，什么也没做
    assert env.model.count('design') == 1


def test_a_failed_job_records_why_and_can_be_resumed(env):
    env.model.queue('rows', {'rows': [fx.content_row(fx.example_bible(), 1)]})            # 一批 5 段只回 1 段，修复也不给
    for _ in range(2):
        env.model.queue('rows-repair', {'rows': []})
    job = wait(jobs.start(BODY, CONFIG)['id'])
    assert job['status'] == 'failed' and job['error']['stage'] == 'rows' and job['error']['issues']
    assert job['resumable'] is True and '多次修复后仍未通过校验' in job['message']
    done = wait(jobs.resume(job['id'], CONFIG)['id'])
    assert done['status'] == 'completed' and done['error'] is None and env.model.count('design') == 1


def test_resume_is_refused_for_jobs_that_are_not_stopped(env):
    job = wait(jobs.start(BODY, CONFIG)['id'])
    with pytest.raises(beat_pack.BeatPackError) as caught:
        jobs.resume(job['id'], CONFIG)
    assert caught.value.code == 'not_resumable' and caught.value.status == 409


def test_a_job_left_running_by_a_dead_process_is_marked_interrupted(env):
    folder = env.root / 'bp_20261009_010203_abcdef'
    folder.mkdir(parents=True)
    request = jobs.normalize_request(BODY, CONFIG)
    (folder / 'state.json').write_text(json.dumps({
        'id': folder.name, 'status': 'running', 'stage': 'rows', 'message': '…', 'progress': 0.4, 'created_at': 1.0, 'request': request,
        'events': [], 'has_bible': True}), encoding='utf-8')
    listed = jobs.list_jobs()
    assert listed[0]['status'] == 'interrupted' and listed[0]['resumable'] is True and '服务重启' in listed[0]['message']
    assert jobs.running_job_ids() == []
    done = wait(jobs.resume(folder.name, CONFIG)['id'])
    assert done['status'] == 'completed'


def test_running_job_ids_covers_queued_and_running_jobs_for_the_restart_guard(env):
    gate, entered = threading.Event(), threading.Event()
    env.model.on_call = lambda kind, label: (entered.set(), gate.wait(10))
    first = jobs.start(BODY, CONFIG)
    assert entered.wait(10)
    second = jobs.start({**BODY, 'theme': '另一个主题：巨型海螺壳改成潮岸住所'}, CONFIG)
    assert jobs.running_job_ids() == sorted([first['id'], second['id']])
    env.model.on_call = None
    gate.set()
    wait(first['id']), wait(second['id'])
    assert jobs.running_job_ids() == []


def test_an_identical_active_request_returns_the_same_job(env):
    gate, entered = threading.Event(), threading.Event()
    env.model.on_call = lambda kind, label: (entered.set(), gate.wait(10))
    first = jobs.start(BODY, CONFIG, request_id='abc')
    assert entered.wait(10)
    assert jobs.start(BODY, CONFIG, request_id='abc')['id'] == first['id']
    other = jobs.start(BODY, CONFIG, request_id='other')
    assert other['id'] != first['id']
    env.model.on_call = None
    gate.set()
    wait(first['id']), wait(other['id'])


@pytest.mark.parametrize('body, message', [
    ('nope', '请求格式'),
    ({}, '请填写创意主题'),
    ({'theme': '短'}, '至少 4 个字'),
    ({'theme': 'x' * 6001}, '不能超过 6000'),
    ({'theme': '合法的主题文字', 'segments': 19}, '20–40'),
    ({'theme': '合法的主题文字', 'segments': 41}, '20–40'),
    ({'theme': '合法的主题文字', 'segments': 'abc'}, '20–40'),
    ({'theme': '合法的主题文字', 'segments': True}, '20–40'),
    ({'theme': '合法的主题文字', 'segments': 28.5}, '20–40'),
    ({'theme': '合法的主题文字', 'model': 'bad model;rm -rf'}, '模型名'),
    ({'theme': '合法的主题文字', 'framework': 5}, '必须是文本'),
    ({'theme': '合法的主题文字', 'review': 'yes'}, 'true/false'),
    ({'theme': '合法的主题文字', 'auto_import': 1}, 'true/false'),
    ({'theme': '合法的主题文字', 'constraints': 'x' * 3001}, '不能超过 3000'),
])
def test_request_validation(env, body, message):
    with pytest.raises(beat_pack.BeatPackError, match=message) as caught:
        jobs.start(body, CONFIG)
    assert caught.value.status == 400 and not env.root.exists()


def test_request_defaults_and_string_segments(env):
    request = jobs.normalize_request({'theme': '合法的主题文字', 'segments': '30'}, {'model': 'claude-sonnet-5-5'})
    assert request['segments'] == 30 and request['model'] == 'claude-sonnet-5-5'
    assert request['review'] is True and request['auto_import'] is True
    assert '睡眠、起居、烹饪、如厕、洗浴' in request['constraints'] and request['framework'] == ''
    assert jobs.normalize_request({'theme': '合法的主题文字'}, {'model': 'gemini-3.8-flash-high'})['model'] == 'claude-opus-5-5'
    assert jobs.normalize_request({'theme': '合法的主题文字', 'model': 'gpt-6.1-sol'}, {})['model'] == 'gpt-6.1-sol'


def test_job_ids_cannot_escape_the_job_directory(env):
    for bad in ('../etc', 'bp_x', '', None, 'bp_20261009_010203_abcdef/../..', 'bp_20261009_010203_ABCDEF'):
        with pytest.raises(beat_pack.BeatPackError) as caught:
            jobs.get_job(bad)
        assert caught.value.status == 400
    with pytest.raises(beat_pack.BeatPackError) as caught:
        jobs.get_job('bp_20261009_010203_abcdef')
    assert caught.value.status == 404


def test_only_the_three_prompt_files_can_be_read(env):
    job = wait(jobs.start(BODY, CONFIG)['id'])
    for name in jobs.PROMPT_FILES:
        assert jobs.prompt_text(job['id'], name).strip()
    for bad in ('../../state.json', 'beat_package.json', '逐段复制/图片_001.txt', ''):
        with pytest.raises(beat_pack.BeatPackError) as caught:
            jobs.prompt_text(job['id'], bad)
        assert caught.value.code == 'bad_file'


def test_prompt_text_needs_a_finished_job(env):
    gate, entered = threading.Event(), threading.Event()
    env.model.on_call = lambda kind, label: (entered.set(), gate.wait(10))
    job = jobs.start(BODY, CONFIG)
    assert entered.wait(10)
    with pytest.raises(beat_pack.BeatPackError) as caught:
        jobs.prompt_text(job['id'])
    assert caught.value.code == 'no_result'
    with pytest.raises(beat_pack.BeatPackError) as caught:
        jobs.import_job(job['id'])
    assert caught.value.code == 'not_completed'
    env.model.on_call = None
    gate.set()
    wait(job['id'])


def test_list_jobs_is_newest_first_and_limited(env):
    ids = []
    for index in range(3):
        ids.append(wait(jobs.start({**BODY, 'theme': f'第 {index} 个主题：巨型干松果改成住所'}, CONFIG)['id'])['id'])
    listed = [j['id'] for j in jobs.list_jobs()]
    assert listed == ids[::-1] and len(jobs.list_jobs(limit=2)) == 2
    assert all(len(j['events']) <= 8 for j in jobs.list_jobs()) and len(jobs.get_job(ids[0])['events']) > 8


def test_capabilities_report_the_route_without_leaking_it(env):
    caps = jobs.capabilities(CONFIG)
    assert caps['gateway'] == {'route': 'claudeBaseUrl', 'has_key': True} and caps['default_model'] == 'claude-opus-5-5'
    assert caps['models'][0] == 'claude-opus-5-5' and caps['segments'] == {'min': 20, 'max': 40, 'default': 28}
    assert caps['message'] == '' and 'SECRET' not in json.dumps(caps) and 'claude.test' not in json.dumps(caps)
    bare = jobs.capabilities({'model': 'claude-sonnet-5-5'})
    assert bare['default_model'] == 'claude-sonnet-5-5' and bare['gateway']['route'] == 'baseUrl'
    assert bare['gateway']['has_key'] is False and 'claudeApiKey' in bare['message']


def test_concurrency_is_clamped():
    try:
        for value, expected in ((0, 1), (2, 2), (9, 3), ('x', 1), (None, 1)):
            jobs.set_concurrency(value)
            assert jobs._MAX_WORKERS == expected
    finally:
        jobs.set_concurrency(1)


def test_unexpected_exceptions_become_a_failed_job(env, monkeypatch):
    job = wait(jobs.start(BODY, CONFIG)['id'])
    state = jobs._load_state(job['id'])
    state.update(status='failed')
    jobs._save_state(state)

    def boom(job_id):
        raise RuntimeError('disk exploded')

    monkeypatch.setattr(jobs, '_run', boom)
    assert jobs.resume(job['id'], CONFIG)['status'] == 'queued'
    result = wait(job['id'], want=('failed',))
    assert 'disk exploded' in result['message'] and result['error']['message'].startswith('内部错误')
