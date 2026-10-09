"""beat_pack.generator：设计 → 分批逐段 → 整包校验 → 审阅，以及每条失败与修复路径。"""
import copy
import json

import pytest

import beat_pack_fixtures as fx
from beat_pack import compose, generator, schema


class Harness:
    """把 Generator 的回调收集起来，便于断言「每一批都落盘了」。"""

    def __init__(self, model, *, review=True, bible=None, rows=None, cancelled=None, **kwargs):
        self.model = model
        self.events, self.progress, self.row_snapshots, self.bibles, self.sleeps = [], [], [], [], []
        self.gen = generator.Generator(
            model, {'theme': '巨型干松果改成的雪林住所', 'segments': 28, 'review': review, 'constraints': '', 'framework': ''},
            bible=bible, rows=rows, cancelled=cancelled or (lambda: False), sleep=self.sleeps.append,
            on_event=lambda level, message: self.events.append((level, message)),
            on_bible=lambda value: self.bibles.append(copy.deepcopy(value)),
            on_rows=lambda value: self.row_snapshots.append(copy.deepcopy(value)),
            on_progress=lambda stage, message, fraction: self.progress.append((stage, fraction)), **kwargs)

    def run(self):
        return self.gen.run()

    def messages(self, level):
        return [m for lv, m in self.events if lv == level]


@pytest.fixture
def bible():
    return fx.example_bible()


@pytest.fixture
def model(bible):
    return fx.FakeModel(bible)


class HttpFailure(Exception):
    def __init__(self, code):
        super().__init__(f'HTTP Error {code}')
        self.code = code


def test_happy_path_makes_one_design_six_row_batches_and_one_review(model):
    harness = Harness(model)
    result = harness.run()
    assert [k for k, _ in model.calls] == ['design'] + ['rows'] * 6 + ['review']
    assert len(result.rows) == 28 and result.stats['repairs'] == 0
    assert [len(s) for s in harness.row_snapshots] == [5, 10, 15, 20, 25, 28]          # 每批通过校验就落盘
    assert len(harness.bibles) == 1 and harness.bibles[0]['title'] == result.bible['title']
    assert result.notes == ['Claude 一致性审阅：未发现需要修改的矛盾。']
    assert schema.validate_theme(result.bible, result.rows, 28) == []


def test_the_result_composes_into_a_valid_package(model):
    result = Harness(model).run()
    theme = result.theme
    assert theme['review_notes'] == result.notes
    pkg = compose.compose_package(theme)
    assert compose.validate_package(pkg, '.')['errors'] == []
    assert len(pkg['production_segments']) == 28


def test_progress_never_goes_backwards_and_finishes_at_one(model):
    harness = Harness(model)
    harness.run()
    fractions = [f for _, f in harness.progress]
    assert fractions == sorted(fractions) and fractions[-1] == 1.0
    assert {stage for stage, _ in harness.progress} >= {'design', 'rows', 'assemble', 'review'}


def test_design_prompt_carries_theme_requirements_and_framework(model):
    gen = Harness(model)
    gen.gen.framework = '先立门，再铺地，最后点灯。'
    gen.run()
    kind, system, user = model.prompts[0]
    assert kind == 'design'
    assert '巨型干松果改成的雪林住所' in user and '睡眠、起居、烹饪、如厕、洗浴' in user      # 缺省成品要求
    assert 'REFERENCE FRAMEWORK' in user and '先立门，再铺地，最后点灯。' in user
    assert 'N = 28 clips (29 state images)' in user
    assert 'FORMAT EXAMPLE' in system and 'unoccupied' in system.lower()


def test_no_framework_section_when_none_given(model):
    Harness(model).run()
    assert 'REFERENCE FRAMEWORK' not in model.prompts[0][2]


def test_row_prompts_carry_the_running_state_and_the_summary_of_earlier_rows(model):
    Harness(model).run()
    rows_prompts = [user for kind, _, user in model.prompts if kind == 'rows']
    assert 'COMPONENT STATES AT THE END OF CLIP 0' in rows_prompts[0] and 'CLIPS ALREADY WRITTEN' not in rows_prompts[0]
    assert 'COMPONENT STATES AT THE END OF CLIP 5' in rows_prompts[1]
    assert 'State of shell after step 5' in rows_prompts[1]          # 上一批写下的状态已经累计进来
    assert 'CLIPS ALREADY WRITTEN' in rows_prompts[1] and 'LAST WRITTEN CLIP IN FULL' in rows_prompts[1]


def test_unparseable_reply_is_retried_with_the_parse_error_in_the_prompt(model):
    model.queue('design', 'Sorry — here is the bible: {"name": "unclosed')
    harness = Harness(model)
    harness.run()
    designs = [u for k, _, u in model.prompts if k == 'design']
    assert len(designs) == 2 and 'could not be parsed' in designs[1] and 'could not be parsed' not in designs[0]
    assert any('不是可解析的 JSON' in m for m in harness.messages('warn'))


def test_transient_errors_back_off_and_retry(model):
    model.queue('design', RuntimeError('connection reset'))
    harness = Harness(model)
    harness.run()
    assert model.count('design') == 2 and harness.sleeps == [5]


def test_three_failures_in_a_row_fail_the_stage(model):
    for _ in range(3):
        model.queue('design', RuntimeError('still down'))
    with pytest.raises(generator.GenerationFailed) as caught:
        Harness(model).run()
    assert caught.value.stage == 'design' and 'still down' in caught.value.message and model.count('design') == 3


@pytest.mark.parametrize('code', [400, 401, 403, 404, 422])
def test_client_errors_fail_fast_with_a_gateway_hint(model, code):
    model.queue('design', HttpFailure(code))
    harness = Harness(model)
    with pytest.raises(generator.GenerationFailed) as caught:
        harness.run()
    assert model.count('design') == 1 and harness.sleeps == []
    assert f'HTTP {code}' in caught.value.message and 'claudeBaseUrl' in caught.value.message


def test_server_errors_are_retried_not_fatal(model):
    model.queue('design', HttpFailure(503))
    Harness(model).run()
    assert model.count('design') == 2


def test_a_bible_wrapped_under_a_bible_key_is_accepted(model, bible):
    model.queue('design', {'bible': bible})
    assert len(Harness(model).run().rows) == 28


def test_design_problems_are_repaired_with_a_patch(model, bible):
    broken = copy.deepcopy(bible)
    broken['geometry'] = bible['geometry'] + ' 中文混入。'
    model.queue('design', broken)
    model.queue('design-repair', {'geometry': bible['geometry'], 'ignored_field': 'x'})
    harness = Harness(model)
    result = harness.run()
    assert result.bible['geometry'] == bible['geometry'] and 'ignored_field' not in result.bible
    assert result.stats['repairs'] == 1
    repair_prompt = next(u for k, _, u in model.prompts if k == 'design-repair')
    assert 'geometry' in repair_prompt and 'failed automatic validation' in repair_prompt
    assert harness.bibles and harness.bibles[0]['geometry'] == bible['geometry']       # 落盘的是修复后的版本


def test_design_that_never_validates_fails_without_saving_a_bible(model, bible):
    broken = copy.deepcopy(bible)
    broken['cameras'].pop('work')
    model.queue('design', broken)
    model.queue('design-repair', {'cameras': broken['cameras']})
    model.queue('design-repair', {'cameras': broken['cameras']})
    harness = Harness(model)
    with pytest.raises(generator.GenerationFailed) as caught:
        harness.run()
    assert caught.value.stage == 'design' and caught.value.issues and harness.bibles == []
    assert 'camera_required' in {i['code'] for i in caught.value.issues}


def test_a_bad_row_is_repaired_without_rewriting_the_rest_of_the_batch(model, bible):
    bad = {'rows': [fx.content_row(bible, i) for i in range(6, 11)]}
    bad['rows'][1]['act'] += ' 工人'
    model.queue('rows', {'rows': [fx.content_row(bible, i) for i in range(1, 6)]})
    model.queue('rows', bad)
    harness = Harness(model)
    result = harness.run()
    assert result.stats['repairs'] == 1 and model.count('rows-repair') == 1
    repair = next(u for k, _, u in model.prompts if k == 'rows-repair')
    assert '[clip 7]' in repair and '[clip 6]' not in repair and '[clip 8]' not in repair
    assert '工人' not in result.rows[6]['act'] and len(result.rows) == 28


def test_rows_missing_from_a_reply_are_requested_again(model, bible):
    short = {'rows': [fx.content_row(bible, i) for i in range(1, 5)]}               # 要 1–5，只给 1–4
    model.queue('rows', short)
    harness = Harness(model)
    result = harness.run()
    assert model.count('rows-repair') == 1 and len(result.rows) == 28
    assert any('缺少这一段' in m for m in harness.messages('warn'))


def test_rows_without_index_fields_are_aligned_by_position(model, bible):
    rows = [fx.content_row(bible, i) for i in range(1, 6)]
    for row in rows:
        row.pop('index')
    model.queue('rows', {'rows': rows})
    assert Harness(model).run().stats['repairs'] == 0


def test_exhausted_row_repairs_fail_the_stage_and_keep_the_earlier_batches(model, bible):
    bad = {'rows': [fx.content_row(bible, i) for i in range(6, 11)]}
    bad['rows'][0]['act'] = 'too short'
    model.queue('rows', {'rows': [fx.content_row(bible, i) for i in range(1, 6)]})
    model.queue('rows', bad)
    model.queue('rows-repair', {'rows': [bad['rows'][0]]})
    model.queue('rows-repair', {'rows': [bad['rows'][0]]})
    harness = Harness(model)
    with pytest.raises(generator.GenerationFailed) as caught:
        harness.run()
    assert caught.value.stage == 'rows' and caught.value.issues
    assert [len(s) for s in harness.row_snapshots] == [5]                              # 第一批已经落盘，可以续跑


def test_cancel_is_checked_between_calls_and_the_saved_bible_survives(model):
    state = {'stop': False}
    model.on_call = lambda kind, label: state.update(stop=True) if kind == 'design' else None
    harness = Harness(model, cancelled=lambda: state['stop'])
    with pytest.raises(generator.Cancelled):
        harness.run()
    assert model.count('rows') == 0 and len(harness.bibles) == 1


def test_resume_reuses_the_saved_bible_and_rows(bible):
    first = fx.FakeModel(bible)
    state = {'batches': 0}

    def count_row_batches(kind, label):
        if kind == 'rows':
            state['batches'] += 1

    first.on_call = count_row_batches
    harness = Harness(first, cancelled=lambda: state['batches'] >= 2)
    with pytest.raises(generator.Cancelled):
        harness.run()
    saved_bible, saved_rows = harness.bibles[-1], harness.row_snapshots[-1]
    assert len(saved_rows) == 10

    second = fx.FakeModel(bible)
    resumed = Harness(second, bible=saved_bible, rows=saved_rows)
    result = resumed.run()
    assert second.count('design') == 0 and second.count('rows') == 4                   # 11–15、16–20、21–25、26–28
    assert result.rows[:10] == saved_rows and len(result.rows) == 28
    first_prompt = next(u for k, _, u in second.prompts if k == 'rows')
    assert 'COMPONENT STATES AT THE END OF CLIP 10' in first_prompt


def test_review_can_be_switched_off(model):
    harness = Harness(model, review=False)
    result = harness.run()
    assert model.count('review') == 0 and result.notes == []
    assert harness.progress[-1][1] == 1.0


def review_issue(bible, row, quote_from=None, **overrides):
    text = fx.content_row(bible, row)['act']
    item = {'row': row, 'field': 'act', 'severity': 'error', 'quote': quote_from or text[:60],
            'problem': 'The board is both installed and lying on the ground.', 'fix': 'Say the board is carried up from the stock.'}
    item.update(overrides)
    return item


def test_a_verified_review_error_rewrites_only_that_clip(model, bible):
    model.review_reply = {'issues': [review_issue(bible, 12)], 'summary': '发现 1 处板材状态矛盾，已修。'}
    fixed = fx.content_row(bible, 12)
    fixed['act'] = fixed['act'] + ' The board is carried up from the staged stock first. FIXED'
    model.queue('rows-repair', {'rows': [fixed]})
    result = Harness(model).run()
    assert result.stats['review_fixed'] == 1 and result.stats['review_issues'] == 1
    assert result.rows[11]['act'].endswith('FIXED') and result.rows[10]['act'] == fx.content_row(bible, 11)['act']
    assert any('发现 1 处板材状态矛盾' in n for n in result.notes)
    fix_prompt = next(u for k, _, u in model.prompts if k == 'rows-repair')
    assert 'Suggested fix: Say the board is carried up from the stock.' in fix_prompt and '[clip 12]' in fix_prompt


def test_a_quote_that_is_not_in_the_cited_text_is_discarded(model, bible):
    model.review_reply = {'issues': [review_issue(bible, 12, quote_from='this sentence was never written anywhere in the package')],
                          'summary': '总评'}
    harness = Harness(model)
    result = harness.run()
    assert model.count('rows-repair') == 0 and result.stats['review_issues'] == 0
    assert any('找不到所引用的文字' in m for m in harness.messages('info'))
    assert not any('审阅遗留' in n for n in result.notes)


@pytest.mark.parametrize('item', [
    {'row': 99, 'field': 'act', 'quote': 'x' * 20},
    {'row': 5, 'field': 'sound', 'quote': 'x' * 20},
    {'row': 5, 'field': 'act', 'quote': 'short'},
    {'row': None, 'field': 'act', 'quote': 'x' * 20},
    {'row': None, 'field': 'bible.nothing', 'quote': 'x' * 20},
    'not even an object',
])
def test_malformed_review_items_never_reach_a_repair(model, item):
    model.review_reply = {'issues': [item], 'summary': ''}
    result = Harness(model).run()
    assert model.count('rows-repair') == 0 and result.stats['review_issues'] == 0


def test_a_review_fix_that_breaks_validation_is_rolled_back(model, bible):
    model.review_reply = {'issues': [review_issue(bible, 12)], 'summary': '总评'}
    broken = fx.content_row(bible, 12)
    broken['act'] = 'The builder fixes it. ' + '工人' * 5
    model.queue('rows-repair', {'rows': [broken]})
    harness = Harness(model)
    result = harness.run()
    assert result.stats['review_fixed'] == 0
    assert result.rows[11]['act'] == fx.content_row(bible, 12)['act']
    assert any('已退回原文' in m for m in harness.messages('warn'))
    assert any(n.startswith('审阅遗留（error）· 第 12 段') for n in result.notes)


def test_a_mixed_review_batch_keeps_the_good_fix_and_rolls_back_only_the_bad_one(model, bible):
    model.review_reply = {'issues': [review_issue(bible, 12), review_issue(bible, 14)], 'summary': '总评'}
    good = fx.content_row(bible, 12)
    good['act'] += ' The board is carried up from the staged stock first. FIXED'
    bad = fx.content_row(bible, 14)
    bad['act'] = 'The builder fixes it. ' + '工人' * 5
    model.queue('rows-repair', {'rows': [good, bad]})
    result = Harness(model).run()
    assert model.count('rows-repair') == 1
    assert result.stats['review_fixed'] == 1
    assert result.rows[11]['act'].endswith('FIXED')
    assert result.rows[13]['act'] == fx.content_row(bible, 14)['act']
    assert [n for n in result.notes if n.startswith('审阅遗留')] == [
        n for n in result.notes if '第 14 段' in n and n.startswith('审阅遗留')]


def test_bible_level_review_findings_are_recorded_not_applied(model, bible):
    quote = bible['geometry'][:50]
    model.review_reply = {'issues': [{'row': None, 'field': 'bible.geometry', 'severity': 'error', 'quote': quote,
                                      'problem': 'The door is narrower than the tank halves.', 'fix': 'Widen the door or resize the tank.'}],
                          'summary': '总评'}
    result = Harness(model).run()
    assert model.count('rows-repair') == 0 and model.count('design-repair') == 0
    assert any('圣经 · bible.geometry' in n and 'narrower' in n for n in result.notes)


def test_a_failed_review_is_not_fatal_and_is_recorded(model):
    for _ in range(3):
        model.queue('review', 'I cannot comply.')
    harness = Harness(model)
    result = harness.run()
    assert len(result.rows) == 28 and any(n.startswith('一致性审阅：未完成') for n in result.notes)
    assert any('一致性审阅未完成' in m for m in harness.messages('warn'))


def test_assemble_repairs_a_late_row_problem(model, bible):
    rows = [schema.merge_outline(bible, [r], i)[0] for i, r in enumerate(fx.full_rows(bible), 1)]
    rows[8]['act'] = rows[8]['act'] + ' 中文'
    harness = Harness(model, bible=bible, rows=rows, review=False)
    harness.gen.assemble()
    assert model.count('rows-repair') == 1 and '中文' not in harness.gen.rows[8]['act']
    assert harness.row_snapshots                                                       # 修复结果落了盘


def test_assemble_repairs_a_bible_problem(model, bible):
    broken = copy.deepcopy(bible)
    broken['ledger']['stair']['rise_m'] = 3.6
    rows = [schema.merge_outline(bible, [r], i)[0] for i, r in enumerate(fx.full_rows(bible), 1)]
    model.queue('design-repair', {'ledger': bible['ledger']})
    harness = Harness(model, bible=broken, rows=rows, review=False)
    harness.gen.assemble()
    assert harness.gen.bible['ledger']['stair']['rise_m'] == 2.9 and harness.bibles


def test_assemble_gives_up_after_the_repair_budget(model, bible):
    broken = copy.deepcopy(bible)
    broken['ledger']['stair']['rise_m'] = 3.6
    rows = [schema.merge_outline(bible, [r], i)[0] for i, r in enumerate(fx.full_rows(bible), 1)]
    model.queue('design-repair', {'ledger': broken['ledger']})
    model.queue('design-repair', {'ledger': broken['ledger']})
    with pytest.raises(generator.GenerationFailed) as caught:
        Harness(model, bible=broken, rows=rows, review=False).gen.assemble()
    assert caught.value.stage == 'assemble' and 'stair_arithmetic' in {i['code'] for i in caught.value.issues}


def test_the_json_the_model_returns_is_never_trusted_to_set_structure(model, bible):
    rows = [fx.content_row(bible, i) for i in range(1, 6)]
    rows[2].update(cam='open', kind='reveal', stage='乱写', actor=False, imgcam='open')
    model.queue('rows', {'rows': rows})
    result = Harness(model).run()
    entry = bible['outline'][2]
    assert (result.rows[2]['cam'], result.rows[2]['kind'], result.rows[2]['stage']) == (entry['cam'], entry['kind'], entry['stage'])
    assert 'actor' not in result.rows[2]


def test_theme_notes_json_serialises(model):
    json.dumps(Harness(model).run().theme, ensure_ascii=False)
