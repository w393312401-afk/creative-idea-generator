"""技能 profile（做哪个模型的提示词 → 读哪个技能包）的回归测试（2026-08-01）。

背景：此前只有一个 skillDir + 一份硬编码的契约清单，那份清单是 base 包
（restoration-prompt-composer）专属的。两个包的 references/ 文件名几乎不重叠，
于是：
  · 把 skillDir 指向 omni 包 → 报"缺 8 个契约文件"，包明明是全的；
  · omni 包又恰好躺在 _autodetect 会扫的技能根目录形态里，差一点就被误采纳；
  · 两个包都只存在于开发机的 ~/.codex 下，clone 下来的机器行为随机器而变，
    而失败是无声的（契约读空 → 合成照跑、质量悄悄劣化）。
本次把包放进仓库 skills/ 并按 profile 拆开解析，这里钉住六组行为：
  1. 契约清单按 profile 分开，不互相误报；
  2. 「videoModel → profile」的映射，以及 skillProfile/SKILL_PROFILE 显式覆写；
  3. 解析优先级里新增的"仓库内置"一层，以及 per-profile 的路径覆写；
  4. 非 base 包读不到某个契约时回落到 base（切个视频模型不该把合成链路读空）；
  5. 历史选题台账写到 runtime/ 而不是技能包，且两个 profile 共用同一份；
  6. 契约状态一次报全部 profile，而不是只报当前激活的那个。
"""
import json
import os

import pytest

import prompt_pipeline as pp
import server_common


@pytest.fixture
def cfg_file(tmp_path, monkeypatch):
    """把 server_config.json 指到临时文件，并复位所有 profile 的解析状态。"""
    path = tmp_path / 'server_config.json'
    path.write_text('{}', encoding='utf-8')
    monkeypatch.setattr(server_common, 'SERVER_CONFIG_FILE', str(path))
    for env in ('SKILL_DIR', 'SKILL_DIR_OMNI', 'SKILL_PROFILE'):
        monkeypatch.delenv(env, raising=False)
    for key in ('skillDir', 'skillProfiles', 'skillProfile', 'videoModel'):
        monkeypatch.delitem(server_common.SERVER_CONFIG, key, raising=False)
    monkeypatch.setattr(server_common, '_SKILL_ROOT_CANDIDATES', ())
    monkeypatch.setattr(server_common, '_VENDORED_SKILL_ROOT', str(tmp_path / 'vendored'))
    monkeypatch.setattr(server_common, '_DEFAULT_SKILL_DIR', str(tmp_path / 'legacy' / 'restoration-prompt-composer'))
    monkeypatch.setattr(server_common, 'SKILL_DIR', str(tmp_path / 'unset'))
    monkeypatch.setattr(server_common, 'SKILL_DIR_SOURCE', 'default')
    monkeypatch.setattr(server_common, '_SKILL_DIRS', dict(server_common._SKILL_DIRS))
    monkeypatch.setattr(server_common, '_SKILL_CONFIG_MTIME',
                        server_common._skill_config_mtime())
    return path


def _set_config(cfg_file, **keys):
    """写配置并保证 mtime 一定变化（同秒内多次写在低精度文件系统上可能同值）。"""
    cfg_file.write_text(json.dumps(keys), encoding='utf-8')
    stat = cfg_file.stat()
    os.utime(str(cfg_file), (stat.st_atime + 10, stat.st_mtime + 10))


def _write_package(directory, profile):
    """按某个 profile 的契约清单铺一个完整的技能包。"""
    for rel in server_common.skill_contract_files(profile):
        p = os.path.join(str(directory), *rel.split('/'))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, 'w', encoding='utf-8') as f:
            f.write(f'# {profile}:{rel}')
    return str(directory)


def _write_vendored(tmp_path, *profiles):
    """在被 patch 过的仓库内置 skills/ 下铺这些 profile 的包。"""
    for profile in profiles:
        _write_package(
            os.path.join(str(tmp_path), 'vendored',
                         server_common.SKILL_PROFILES[profile]['package']),
            profile)


class TestContractListsArePerProfile:
    def test_the_two_packages_declare_different_contracts(self):
        """两份清单必须真的不同——相同就说明有一边照抄了另一边，误报会回来。"""
        base = set(server_common.skill_contract_files('base'))
        omni = set(server_common.skill_contract_files('omni'))
        assert base != omni
        # 只共享"每个包都得有"的那几个：SKILL.md、形态矩阵、台账种子
        assert base & omni == {
            'SKILL.md', 'references/idea-engine.md', 'references/used-topic-ledger.md'}

    def test_base_contract_list_stays_the_legacy_eight(self):
        """SKILL_CONTRACT_FILES 是外部（frame_generator、旧测试）引用的名字，
        必须继续等于 base 的清单，不能因为多 profile 就换了含义。"""
        assert server_common.SKILL_CONTRACT_FILES == server_common.skill_contract_files('base')
        assert len(server_common.SKILL_CONTRACT_FILES) == 8

    def test_a_complete_omni_package_is_not_reported_as_missing(self, cfg_file, tmp_path):
        """这就是拆分前的那个 bug：拿 base 的清单去查 omni 包，报缺 8 个文件。"""
        omni = _write_package(tmp_path / 'omni-pkg', 'omni')
        _set_config(cfg_file, skillProfiles={'omni': omni})

        assert server_common.missing_skill_contract_files('omni') == []
        # 反过来：同一个目录按 base 的清单查，就该缺掉不重叠的那 5 个——这正是
        # 拆分前用户会看到的那份假告警。
        base_view = [rel for rel in server_common.skill_contract_files('base')
                     if not os.path.exists(os.path.join(omni, *rel.split('/')))]
        assert len(base_view) == 5

    def test_unknown_profile_falls_back_to_base_instead_of_raising(self):
        """profile 名有一部分来自前端配置；拼错一个字母不该让整条链路 500。"""
        assert server_common.skill_contract_files('nope') == server_common.skill_contract_files('base')
        assert server_common.skill_contract_report('nope')['profile'] == 'base'


class TestVideoModelToProfile:
    @pytest.mark.parametrize('model,expected', [
        ('Omni Flash', 'omni'),
        ('omni flash', 'omni'),
        ('Gemini Omni', 'omni'),
        ('Veo 3.1 - Lite', 'base'),
        ('Veo 3.1 - Lite [Lower Priority]', 'base'),
        ('', 'base'),
        (None, 'base'),
        ('某个还没出现的模型', 'base'),
    ])
    def test_mapping(self, model, expected):
        assert server_common.profile_for_video_model(model) == expected

    def test_active_profile_follows_the_selected_video_model(self, cfg_file):
        _set_config(cfg_file, videoModel='Omni Flash')
        assert server_common.active_skill_profile() == 'omni'

        _set_config(cfg_file, videoModel='Veo 3.1 - Lite')
        assert server_common.active_skill_profile() == 'base'

    def test_request_config_wins_over_server_config(self, cfg_file):
        """合成/激发收到的是本次请求带上来的配置，不是服务端那份。"""
        _set_config(cfg_file, videoModel='Veo 3.1 - Lite')
        assert server_common.active_skill_profile({'videoModel': 'Omni Flash'}) == 'omni'

    def test_explicit_profile_wins_over_video_model(self, cfg_file):
        """只想换渲染档位的人不该被顺手改掉提示词语法，反之亦然。"""
        _set_config(cfg_file, videoModel='Omni Flash', skillProfile='base')
        assert server_common.active_skill_profile() == 'base'

    def test_env_wins_over_config(self, cfg_file, monkeypatch):
        _set_config(cfg_file, videoModel='Veo 3.1 - Lite', skillProfile='base')
        monkeypatch.setenv('SKILL_PROFILE', 'omni')
        assert server_common.active_skill_profile() == 'omni'

    def test_unknown_override_falls_back_to_inference(self, cfg_file):
        _set_config(cfg_file, videoModel='Omni Flash', skillProfile='0mni')
        assert server_common.active_skill_profile() == 'omni'


class TestResolutionPerProfile:
    def test_vendored_packages_are_used_when_nothing_is_configured(self, cfg_file, tmp_path):
        """契约随代码版本化的意义：不配任何东西也有确定行为。"""
        _write_vendored(tmp_path, 'base', 'omni')
        _set_config(cfg_file, debug=True)

        for profile in ('base', 'omni'):
            report = server_common.skill_contract_report(profile)
            assert report['source'] == 'vendored'
            assert report['dir'] == os.path.join(
                str(tmp_path), 'vendored', server_common.SKILL_PROFILES[profile]['package'])
            assert report['missing'] == []

    def test_config_overrides_only_the_named_profile(self, cfg_file, tmp_path):
        _write_vendored(tmp_path, 'base', 'omni')
        elsewhere = _write_package(tmp_path / 'my-omni', 'omni')
        _set_config(cfg_file, skillProfiles={'omni': elsewhere})

        assert server_common.skill_dir('omni') == elsewhere
        assert server_common.skill_dir_source('omni') == 'config'
        # base 不受影响，仍用内置那份
        assert server_common.skill_dir_source('base') == 'vendored'

    def test_legacy_skill_dir_key_still_configures_base(self, cfg_file, tmp_path):
        """已经配好 skillDir 的机器升级后不能静默回到默认路径。"""
        _write_vendored(tmp_path, 'base')
        legacy = _write_package(tmp_path / 'legacy-base', 'base')
        _set_config(cfg_file, skillDir=legacy)

        assert server_common.skill_dir('base') == legacy
        assert server_common.skill_dir_source('base') == 'config'

    def test_skill_profiles_entry_wins_over_legacy_key(self, cfg_file, tmp_path):
        precise = _write_package(tmp_path / 'precise', 'base')
        _set_config(cfg_file, skillDir=str(tmp_path / 'legacy'),
                    skillProfiles={'base': precise})
        assert server_common.skill_dir('base') == precise

    def test_env_var_per_profile(self, cfg_file, tmp_path, monkeypatch):
        _write_vendored(tmp_path, 'omni')
        from_env = _write_package(tmp_path / 'env-omni', 'omni')
        _set_config(cfg_file, skillProfiles={'omni': str(tmp_path / 'config-omni')})
        monkeypatch.setenv('SKILL_DIR_OMNI', from_env)

        assert server_common.skill_dir('omni') == from_env
        assert server_common.skill_dir_source('omni') == 'env'

    def test_vendored_loses_to_explicit_config(self, cfg_file, tmp_path):
        """显式配错了要看得见地报缺失，不能被内置那份悄悄顶掉。"""
        _write_vendored(tmp_path, 'base')
        _set_config(cfg_file, skillDir=str(tmp_path / 'typo'))

        report = server_common.skill_contract_report('base')
        assert report['dir'] == str(tmp_path / 'typo')
        assert len(report['missing']) == report['total']

    def test_autodetect_matches_the_profiles_own_contracts(self, cfg_file, tmp_path, monkeypatch):
        """探测 omni 时要按 omni 的清单打分，否则会挑中隔壁的 base 包。"""
        base_pkg = _write_package(tmp_path / 'roots' / 'restoration-prompt-composer', 'base')
        omni_pkg = _write_package(tmp_path / 'roots' / 'some-omni-fork', 'omni')
        monkeypatch.setattr(server_common, '_SKILL_ROOT_CANDIDATES', (str(tmp_path / 'roots'),))
        _set_config(cfg_file, debug=True)

        assert server_common.skill_dir('omni') == omni_pkg
        assert server_common.skill_dir('base') == base_pkg

    def test_hot_reload_applies_to_every_profile(self, cfg_file, tmp_path):
        first = _write_package(tmp_path / 'omni-a', 'omni')
        _set_config(cfg_file, skillProfiles={'omni': first})
        assert server_common.skill_dir('omni') == first

        second = _write_package(tmp_path / 'omni-b', 'omni')
        _set_config(cfg_file, skillProfiles={'omni': second})
        assert server_common.skill_dir('omni') == second


class TestReferenceLoading:
    def test_reads_the_active_profiles_package(self, cfg_file, tmp_path, monkeypatch):
        monkeypatch.setattr(pp, '_REFERENCE_MISS_LOGGED', set())
        monkeypatch.setattr(pp, '_REFERENCE_FALLBACK_LOGGED', set())
        _write_vendored(tmp_path, 'base', 'omni')
        _set_config(cfg_file, debug=True)

        assert pp.load_reference_file('idea-engine.md', 'omni') == '# omni:references/idea-engine.md'
        assert pp.load_reference_file('idea-engine.md', 'base') == '# base:references/idea-engine.md'

    def test_missing_in_omni_falls_back_to_base(self, cfg_file, tmp_path, monkeypatch, capsys):
        """omni 包里没有 prompt-templates.md 这类文件。不兜底的话，把视频模型切到
        Omni Flash 就会让现有合成链路整段读空——一次静默的大降级。"""
        monkeypatch.setattr(pp, '_REFERENCE_MISS_LOGGED', set())
        monkeypatch.setattr(pp, '_REFERENCE_FALLBACK_LOGGED', set())
        _write_vendored(tmp_path, 'base', 'omni')
        _set_config(cfg_file, debug=True)

        assert pp.load_reference_file('prompt-templates.md', 'omni') == \
            '# base:references/prompt-templates.md'
        out = capsys.readouterr().out
        assert '回落到 base' in out

        # 回落只提示一次，不逐拍刷屏
        assert pp.load_reference_file('prompt-templates.md', 'omni') != ''
        assert capsys.readouterr().out == ''

    def test_missing_in_both_is_still_reported(self, cfg_file, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(pp, '_REFERENCE_MISS_LOGGED', set())
        monkeypatch.setattr(pp, '_REFERENCE_FALLBACK_LOGGED', set())
        _set_config(cfg_file, skillProfiles={'omni': str(tmp_path / 'empty-omni')})

        assert pp.load_reference_file('omni-multishot-language.md', 'omni') == ''
        assert '技能契约文件缺失' in capsys.readouterr().out


class TestUsedTopicLedgerIsGlobalAndWritable:
    @pytest.fixture(autouse=True)
    def ledger_at_tmp(self, tmp_path, monkeypatch):
        monkeypatch.setattr(server_common, 'USED_TOPIC_LEDGER_FILE',
                            str(tmp_path / 'runtime' / 'used-topic-ledger.md'))

    def test_seeded_from_the_skill_package_on_first_use(self, cfg_file, tmp_path):
        _write_vendored(tmp_path, 'base', 'omni')
        _set_config(cfg_file, debug=True)

        path = server_common.ensure_used_topic_ledger('base')
        assert path == str(tmp_path / 'runtime' / 'used-topic-ledger.md')
        with open(path, encoding='utf-8') as f:
            assert f.read() == '# base:references/used-topic-ledger.md'

    def test_both_profiles_share_one_ledger(self, cfg_file, tmp_path):
        """同一个选题换个分镜语法重做一遍不是新选题：去重记忆不能按 profile 劈成两半。"""
        _write_vendored(tmp_path, 'base', 'omni')
        _set_config(cfg_file, debug=True)

        assert server_common.ensure_used_topic_ledger('base') == \
            server_common.ensure_used_topic_ledger('omni')

    def test_writes_never_touch_the_skill_package(self, cfg_file, tmp_path):
        """包内那份是只读种子：技能包已经进了 git，往包里追加会让它每合成一次脏一次。"""
        _write_vendored(tmp_path, 'base')
        _set_config(cfg_file, debug=True)
        seed = os.path.join(server_common.skill_dir('base'), 'references', 'used-topic-ledger.md')
        before = open(seed, encoding='utf-8').read()

        pp.append_to_used_topic_ledger(
            {'carrier_slug': 'stone-hut', 'destiny': 'studio'},
            {'topic_dna': 'stone-hut / studio / skylight', 'theme': '测试选题'})

        assert open(seed, encoding='utf-8').read() == before
        written = open(server_common.used_topic_ledger_path(), encoding='utf-8').read()
        assert 'stone-hut / studio / skylight' in written

    def test_existing_ledger_is_never_reseeded(self, cfg_file, tmp_path):
        """播种只发生一次：否则每次重启都会把运行时攒下的去重记忆冲掉。"""
        _write_vendored(tmp_path, 'base')
        _set_config(cfg_file, debug=True)
        path = server_common.ensure_used_topic_ledger('base')
        with open(path, 'a', encoding='utf-8') as f:
            f.write('\n| 2026-08-01 | a / b / c | 运行时攒下的一行 | GUI | . |\n')

        assert '运行时攒下的一行' in open(server_common.ensure_used_topic_ledger('base'), encoding='utf-8').read()


class TestReportingCoversEveryProfile:
    def test_reports_include_all_profiles(self, cfg_file, tmp_path):
        """只报当前激活的那个，等于把"另一个包没装好"留到切模型那一刻才炸。"""
        _write_vendored(tmp_path, 'base')  # omni 故意不铺
        _set_config(cfg_file, videoModel='Veo 3.1 - Lite')

        reports = server_common.skill_contract_reports()
        by_profile = {r['profile']: r for r in reports}
        assert set(by_profile) == set(server_common.SKILL_PROFILES)
        assert by_profile['base']['missing'] == []
        assert len(by_profile['omni']['missing']) == by_profile['omni']['total']

    def test_report_carries_label_and_package(self, cfg_file, tmp_path):
        _set_config(cfg_file, debug=True)
        report = server_common.skill_contract_report('omni')
        assert report['package'] == 'gemini-omni-restoration-composer'
        assert report['label']

    def test_capability_stamp_checks_the_profile_the_order_actually_used(self, cfg_file, tmp_path):
        """每一单的能力印章要按本单实际用的包查契约。固定查 base 的话，一单 omni
        成片的记录就是错的——而这层存在的全部理由就是三天后还能知道当初缺没缺契约。"""
        _write_vendored(tmp_path, 'base')  # omni 故意不铺
        _set_config(cfg_file, videoModel='Veo 3.1 - Lite')
        assert server_common.runtime_capability_report()['skill_contract_missing'] == []

        _set_config(cfg_file, videoModel='Omni Flash')
        report = server_common.runtime_capability_report()
        assert report['skill_profile'] == 'omni'
        assert report['skill_contract_missing']
        assert any('技能契约' in t and 'omni' in t for t in report['degraded'])

    def test_vendored_packages_in_the_repo_are_complete(self):
        """仓库里实际躺着的那两个包必须契约齐全——这条不 patch 任何路径，
        直接查 skills/ 下的真实文件：搬包时漏掉一个 references/ 就会在这里挂。"""
        root = server_common._VENDORED_SKILL_ROOT
        for profile, spec in server_common.SKILL_PROFILES.items():
            pkg = os.path.join(root, spec['package'])
            missing = [rel for rel in spec['contracts']
                       if not os.path.exists(os.path.join(pkg, *rel.split('/')))]
            assert missing == [], f'{profile} 包缺文件: {missing}'


class TestFrontendSkillProfileSelection:
    """激发页脚的「提示词链路」选择器把 skillProfile 随请求 config 送上来
    （2026-08-01）。链路一旦能在前端选，这一路的每一环都必须真的通到底。"""

    def test_effective_config_passes_skill_profile_through_in_managed_mode(self, cfg_file):
        """托管模式的白名单是唯一透传口，漏掉 skillProfile 就是『选了但从未生效』
        的静默失效——qaGateLevel / imageEditTransport 都在这个口子上栽过。"""
        import unittest.mock as mock
        with mock.patch.object(server_common, 'SERVER_MANAGED', True):
            merged = server_common.effective_config({'skillProfile': 'omni'})
        assert merged.get('skillProfile') == 'omni'
        assert server_common.active_skill_profile(merged) == 'omni'

    def test_request_config_selection_beats_the_video_model(self, cfg_file):
        """选了链路就该盖过视频模型：只想换渲染档位的人不该被顺手改掉提示词语法。"""
        cfg = {'skillProfile': 'base', 'videoModel': 'Omni Flash'}
        assert server_common.active_skill_profile(cfg) == 'base'
        cfg = {'skillProfile': 'omni', 'videoModel': 'Veo 3.1 - Lite'}
        assert server_common.active_skill_profile(cfg) == 'omni'

    def test_auto_falls_back_to_the_video_model(self, cfg_file):
        """选择器停在「自动」时传的就是 'auto'，必须退回按 videoModel 推断。"""
        assert server_common.active_skill_profile(
            {'skillProfile': 'auto', 'videoModel': 'Omni Flash'}) == 'omni'
        assert server_common.active_skill_profile(
            {'skillProfile': 'auto', 'videoModel': 'Veo 3.1 - Quality'}) == 'base'

    @pytest.mark.parametrize('model', [
        'Veo 3.1 - Lite', 'Veo 3.1 - Fast', 'Veo 3.1 - Quality',
        'Veo 3.1 - Lite [Lower Priority]', 'Omni Flash',
    ])
    def test_every_frontend_video_model_option_maps_somewhere(self, cfg_file, model):
        """index.html 的 FX 视频模型下拉框里的每一项都必须落到一个已注册 profile 上。"""
        assert server_common.profile_for_video_model(model) in server_common.SKILL_PROFILES

    def test_api_mode_rules_are_shaped_for_the_frontend(self):
        """/api/mode 下发的规则表就是服务端这一份原样序列化——前端照它显示 auto
        实际走哪条，不自己硬编码 'omni' 判断（那会是第二个会漂移的真相源）。"""
        rules = [list(r) for r in server_common.SKILL_PROFILE_VIDEO_MODEL_RULES]
        assert rules and all(len(r) == 2 for r in rules)
        for needle, profile in rules:
            assert isinstance(needle, str) and needle == needle.lower()
            assert profile in server_common.SKILL_PROFILES
        # 前端 resolveAutoSkillProfile 的等价实现：按表匹配 → 都不中回 default。
        def resolve(video_model):
            hay = str(video_model or '').lower()
            for needle, profile in rules:
                if needle in hay:
                    return profile
            return server_common.DEFAULT_SKILL_PROFILE
        for model in ('Omni Flash', 'Veo 3.1 - Lite', '', None):
            assert resolve(model) == server_common.profile_for_video_model(model)
