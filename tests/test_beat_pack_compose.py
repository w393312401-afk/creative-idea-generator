"""beat_pack.compose：主题数据 → beat_package → 交付文件。"""
import copy
import json
import re

import pytest

import beat_pack_fixtures as fx
from beat_pack import compose, schema


@pytest.fixture
def bible():
    return fx.example_bible()


@pytest.fixture
def theme(bible):
    return fx.theme_from(bible, fx.full_rows(bible))


def test_chain_has_n_segments_and_n_plus_one_unoccupied_images(theme):
    pkg = compose.compose_package(theme)
    segments, images = pkg['production_segments'], pkg['images']
    assert (len(segments), len(images)) == (28, 29)
    for i, seg in enumerate(segments, 1):
        assert (seg['start_image_id'], seg['end_image_id']) == (i, i + 1)
        assert seg['duration_sec'] == 10 and seg['origin'] == 'creative_design'
        assert f'Bind IMAGE {i} as the starting state and IMAGE {i + 1} as the ending state' in seg['prompt']
    for image in images:
        assert image['actor_ids'] == [] and image['people_allowed'] is False
        assert image['prompt'].startswith('Photorealistic vertical 9:16 still of an original fictional dwelling conversion. Entirely unoccupied')


def test_only_the_last_clip_is_unoccupied_and_exactly_one_is_a_bridge(theme):
    segments = compose.compose_package(theme)['production_segments']
    assert [s['kind'] for s in segments].count('bridge') == 1
    assert segments[-1]['kind'] == 'reveal' and segments[-1]['actor_ids'] == []
    assert 'Entirely unoccupied footage' in segments[-1]['prompt']
    assert all(s['actor_ids'] == ['builder_A'] for s in segments[:-1])
    assert all('The builder is one adult' in s['prompt'] for s in segments[:-1])


def test_state_images_never_use_a_travelling_camera(theme, bible):
    moving = schema.travelling_cameras(bible['cameras'])
    assert moving == {'bridge', 'reveal'}
    pkg = compose.compose_package(theme)
    assert {image['camera_family'] for image in pkg['images']}.isdisjoint(moving)
    # 终景是运动机位：最后一张状态图用 cam_end 指定的固定机位，而不是一条运动路线。
    assert pkg['images'][-1]['camera_family'] == 'int_u'
    assert 'starting 1.4 m high at the door' not in pkg['images'][-1]['prompt']


def test_camera_scope_filters_the_visible_state(theme, bible):
    segments = compose.compose_package(theme)['production_segments']
    # walls 是室内组件（scope=int），假模型从不更新它：外景机位的开场状态里不该出现它，室内机位里必须出现。
    walls = next(c['text'] for c in bible['components'] if c['key'] == 'walls')
    assert next(c['scope'] for c in bible['components'] if c['key'] == 'walls') == 'int'
    seen = set()
    for seg in segments:
        scope = bible['cameras'][seg['camera_family']]['scope']
        opening = seg['prompt'].split('Opening state:')[1].split('Action and visible continuity')[0]
        assert (walls in opening) == (scope in ('int', 'both')), seg['id']
        seen.add(scope)
    assert seen == {'ext', 'int'}


def test_wear_sentence_carries_forward_until_it_changes(bible):
    rows = fx.full_rows(bible)
    rows[9]['wear'] = 'The jacket and gloves hang on a peg inside the door; the builder works in the mustard-yellow shirt and snow boots. '
    pkg = compose.compose_package(fx.theme_from(bible, rows))
    prompts = [s['prompt'] for s in pkg['production_segments']]
    jacket = 'A dark green insulated work jacket is worn over the shirt'
    assert all(jacket in prompts[i] for i in range(0, 9))             # 第 1 段声明，之后沿用
    assert all(jacket not in prompts[i] for i in range(9, 27))        # 第 10 段改写后不再沿用旧的
    assert all('hang on a peg inside the door' in prompts[i] for i in range(9, 27))
    assert 'hang on a peg' not in prompts[27]                         # 无人终景不描述人物服装


def test_default_wear_applies_until_a_row_declares_one(bible):
    rows = fx.full_rows(bible)
    rows[0].pop('wear')
    pkg = compose.compose_package(fx.theme_from(bible, rows))
    assert compose.DEFAULT_WEAR in pkg['production_segments'][0]['prompt']


def test_updates_accumulate_into_later_opening_states(theme, bible):
    segments = compose.compose_package(theme)['production_segments']
    ext = [s for s in segments if bible['cameras'][s['camera_family']]['scope'] == 'ext']
    previous, current = ext[3], ext[4]
    # 假模型每个外景段都更新 shell：下一个外景段的“之前”就是上一段写下的状态，“之后”是本段的新状态。
    assert f"State of shell after step {previous['id']}" in current['visible_change']['before']
    assert f"State of shell after step {current['id']}" in current['visible_change']['after']
    assert f"State of shell after step {current['id']}" not in current['visible_change']['before']


def test_validator_accepts_the_composed_package(theme):
    report = compose.validate_package(compose.compose_package(theme), '.')
    assert report['errors'] == []
    assert report['structure_status'] == 'pass'
    assert report['render_review_status'] == 'not_run'


def test_unverified_render_check_is_always_present(theme):
    theme = copy.deepcopy(theme)
    theme['checks'] = [c for c in theme['checks'] if c['id'] != 'render_identity_scale_space']
    pkg = compose.compose_package(theme)
    ids = {c['id']: c for c in pkg['spatial_contract']['checks']}
    assert ids['render_identity_scale_space']['status'] == 'unverified'


@pytest.mark.parametrize('mutate, message', [
    (lambda t: t['rows'][3]['upd'].update({'nope': 'x' * 40}), '不存在的组件'),
    (lambda t: t['rows'][3].update({'cam': 'ghost'}), 'ghost'),
    (lambda t: t['rows'][3].update({'imgcam': 'ghost'}), 'imgcam'),
    (lambda t: t['rows'].__delitem__(slice(19, None)), '段数'),
    (lambda t: t['rows'].extend(copy.deepcopy(t['rows'][:20])), '段数'),
])
def test_composer_rejects_dangling_references_and_bad_counts(theme, mutate, message):
    theme = copy.deepcopy(theme)
    mutate(theme)
    with pytest.raises(compose.ThemeError, match=message):
        compose.compose_package(theme)


def test_tuple_and_dict_theme_shapes_compose_identically(theme):
    legacy = copy.deepcopy(theme)
    legacy['components'] = [(c['key'], c['scope'], c['text']) for c in legacy['components']]
    legacy['cameras'] = {k: (v['scope'], v['text']) for k, v in legacy['cameras'].items()}
    assert compose.compose_package(legacy) == compose.compose_package(theme)


def test_write_package_files_creates_a_complete_delivery(theme, tmp_path):
    pkg = compose.compose_package(theme)
    out = tmp_path / 'pack'
    report, delivery = compose.write_package_files(theme, pkg, out)
    assert report['errors'] == [] and delivery == out / 'delivery-v1'
    for name in ('beat_package.json', 'validation.json', '创意方案.md', '空间与比例.md', '审核记录.md', '生成阶梯.md', '人物参考计划.md'):
        assert (out / name).is_file(), name
    text = (delivery / '完整提示词.txt').read_text(encoding='utf-8')
    assert len(re.findall(r'^图片 \d+:$', text, re.M)) == 29
    assert len(re.findall(r'^视频 \d+(?: \[BRIDGE\])?:$', text, re.M)) == 28
    assert len(re.findall(r'^视频 \d+ \[BRIDGE\]:$', text, re.M)) == 1
    assert (delivery / '逐段复制' / '图片_029.txt').is_file() and (delivery / '时间索引.tsv').is_file()
    assert compose.complete_prompt_text(pkg) == text
    # 同一目录原地重出：导出器自己拒绝覆盖，写盘层负责先清掉旧的交付目录。
    report_again, delivery_again = compose.write_package_files(theme, pkg, out)
    assert report_again['errors'] == [] and delivery_again.is_dir()


def test_invalid_package_is_reported_and_not_exported(theme, tmp_path):
    pkg = compose.compose_package(theme)
    pkg['production_segments'][4]['end_image_id'] = 9
    report, delivery = compose.write_package_files(theme, pkg, tmp_path / 'pack')
    assert delivery is None and report['errors']
    assert not (tmp_path / 'pack' / 'delivery-v1').exists()
    assert json.loads((tmp_path / 'pack' / 'validation.json').read_text(encoding='utf-8'))['structure_status'] == 'fail'


def test_documents_are_derived_from_data_when_the_model_did_not_write_them(theme):
    theme = copy.deepcopy(theme)
    for key in ('concept', 'spatial_md', 'review_md'):
        theme.pop(key, None)
    theme['concept'] = ''
    pkg = compose.compose_package(theme)
    docs = compose.render_documents(theme, pkg)
    assert theme['hook']['first_conflict'] in docs['创意方案.md']
    assert '空间 pinecone_dwelling' in docs['空间与比例.md'] and 'stair' in docs['空间与比例.md']
    assert 'dwelling_functions' in docs['审核记录.md'] and '第 1 段起' in docs['审核记录.md']
    assert docs['生成阶梯.md'].count('\n| ') >= 28
    assert '28 段 × 10 秒 = 280 秒' in docs['生成阶梯.md']


def test_review_notes_are_written_into_the_review_record(bible):
    theme = fx.theme_from(bible, fx.full_rows(bible))
    theme['review_notes'] = ['Claude 一致性审阅：未发现矛盾。']
    docs = compose.render_documents(theme, compose.compose_package(theme))
    assert 'Claude 一致性审阅：未发现矛盾。' in docs['审核记录.md']


def test_validator_prefers_the_vendored_skill(monkeypatch, tmp_path):
    assert compose.validator_path() == compose._PROJECT_ROOT / 'skills' / 'video-beat-ladder' / 'scripts' / 'validate_beat_package.py'
    # 仓库内置包缺失时才退到环境变量，再退到用户目录。
    fake = tmp_path / 'skill'
    (fake / 'scripts').mkdir(parents=True)
    (fake / 'scripts' / 'validate_beat_package.py').write_text('# stub\n', encoding='utf-8')
    monkeypatch.setattr(compose, '_PROJECT_ROOT', tmp_path / 'empty-repo')
    monkeypatch.setenv('BEAT_LADDER_SKILL_DIR', str(fake))
    assert compose.validator_path() == fake / 'scripts' / 'validate_beat_package.py'
    monkeypatch.setenv('BEAT_LADDER_SKILL_DIR', str(tmp_path / 'missing'))
    monkeypatch.setattr(compose.Path, 'home', classmethod(lambda cls: tmp_path / 'nobody'))
    with pytest.raises(FileNotFoundError):
        compose.validator_path()
