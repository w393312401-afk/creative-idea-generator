"""契约注册表与门禁的一致性回归（2026-08-05）。

背景：运行时从不读 SKILL.md——它只参与"文件在不在"的存在性校验，内容一行都没被加载
过（全项目只有 server_common 的两份契约清单引用它）。skill 里那些 P0 契约，实际是由
prompt_pipeline 里四十多个 check_* 函数手写的一份平行实现。

于是有两个真相源：markdown 里的散文，和 Python 里的门禁。此前没有任何东西保证两边
一致——改了 markdown 而门禁没跟，或者门禁改了名而契约没跟，都不会有任何信号。本测试
钉住的就是这条链：

  1. 每个技能包都要带注册表，版本与运行时兼容；
  2. 注册表里每个 enforcer 都必须能 import 到一个真实可调用对象（改名/删除即红）；
  3. 没有执行者的契约必须显式写 gap 说明——缺口可以存在，但必须登记在案；
  4. 结论要一路送到 /api/mode，前端才看得见。

这张表的价值不在于"证明全都执行了"，而在于让**没有执行**的那几条无法再隐身。
"""
import importlib
import json

import pytest

import server_common


ALL_PROFILES = sorted(server_common.SKILL_PROFILES)


def _registry_path(profile):
    return server_common.os.path.join(
        server_common.skill_dir(profile), *server_common.SKILL_REGISTRY_REL.split('/'))


def _resolve(enforcer):
    """'模块:属性' -> 真实对象。解析不到就抛，由调用方转成断言失败。"""
    module_name, _, attr = str(enforcer).partition(':')
    module = importlib.import_module(module_name)
    return getattr(module, attr)


@pytest.fixture
def base_skill_dir(tmp_path, monkeypatch):
    """把 base 的技能目录指到临时目录。

    必须同时 patch 模块全局 SKILL_DIR：skill_dir() 对 base 一律读那个全局，只改
    _SKILL_DIRS 会被静默吃掉（server_common.skill_dir 的注释写的就是这件事）。
    顺带清掉注册表缓存——它按 (路径, mtime) 记，跨用例复用会串味。
    """
    monkeypatch.setattr(server_common, 'SKILL_DIR', str(tmp_path))
    monkeypatch.setitem(server_common._SKILL_DIRS, 'base', (str(tmp_path), 'test'))
    server_common._SKILL_REGISTRY_CACHE.clear()
    yield tmp_path
    server_common._SKILL_REGISTRY_CACHE.clear()


class TestRegistryShipsWithEveryPackage:
    @pytest.mark.parametrize('profile', ALL_PROFILES)
    def test_registry_is_present_and_parses(self, profile):
        with open(_registry_path(profile), 'r', encoding='utf-8') as f:
            data = json.load(f)
        assert isinstance(data.get('contracts'), list) and data['contracts'], \
            f'{profile} 的注册表没有任何契约条目'

    @pytest.mark.parametrize('profile', ALL_PROFILES)
    def test_version_is_compatible_with_the_runtime(self, profile):
        _data, status = server_common.skill_contract_registry(profile)
        assert status == 'ok', (
            f'{profile} 的契约注册表状态为 {status}——技能包与运行时脱节，'
            f'照跑会按错误的契约集合审计')

    @pytest.mark.parametrize('profile', ALL_PROFILES)
    def test_declared_profile_matches_the_package_it_ships_in(self, profile):
        data, _status = server_common.skill_contract_registry(profile)
        assert data['profile'] == profile
        assert data['package'] == server_common.SKILL_PROFILES[profile]['package']


class TestEveryEnforcerActuallyExists:
    """注册表声明的执行者必须解析得到一个可调用对象。

    这是整套机制的承重墙：门禁改名、挪走、删掉而契约没跟，在这里红。
    """

    @pytest.mark.parametrize('profile', ALL_PROFILES)
    def test_all_enforcers_resolve_to_callables(self, profile):
        data, _status = server_common.skill_contract_registry(profile)
        broken = []
        for contract in data['contracts']:
            enforcer = contract.get('enforcer')
            if not enforcer:
                continue
            try:
                target = _resolve(enforcer)
            except (ImportError, AttributeError, ValueError) as e:
                broken.append(f"{contract['id']} -> {enforcer}（{type(e).__name__}: {e}）")
                continue
            if not callable(target):
                broken.append(f"{contract['id']} -> {enforcer}（解析到了，但不可调用）")
        assert not broken, (
            f'{profile} 注册表里这些契约指向了不存在或不可调用的执行者：\n  '
            + '\n  '.join(broken))

    @pytest.mark.parametrize('profile', ALL_PROFILES)
    def test_contracts_without_an_enforcer_document_the_gap(self, profile):
        """没有执行者是允许的，装作有则不允许。

        缺口写清楚理由，下一个人才知道那是权衡的结果而不是漏掉的一条；空着不写，
        半年后没人分得清"没人管"和"忘了登记"。
        """
        data, _status = server_common.skill_contract_registry(profile)
        undocumented = [c['id'] for c in data['contracts']
                        if not c.get('enforcer') and not str(c.get('gap') or '').strip()]
        assert not undocumented, (
            f'{profile} 注册表里这些契约既没有执行者也没写 gap 说明：{undocumented}')

    @pytest.mark.parametrize('profile', ALL_PROFILES)
    def test_entries_carry_the_required_fields(self, profile):
        data, _status = server_common.skill_contract_registry(profile)
        required = ('id', 'level', 'scope', 'summary', 'source')
        bad = [(c.get('id') or '(无 id)', k)
               for c in data['contracts'] for k in required
               if not str(c.get(k) or '').strip()]
        assert not bad, f'{profile} 注册表条目缺字段：{bad}'

    @pytest.mark.parametrize('profile', ALL_PROFILES)
    def test_ids_are_unique(self, profile):
        data, _status = server_common.skill_contract_registry(profile)
        ids = [c.get('id') for c in data['contracts']]
        assert len(ids) == len(set(ids)), f'{profile} 注册表存在重复 id'

    @pytest.mark.parametrize('profile', ALL_PROFILES)
    def test_levels_and_scopes_use_the_declared_vocabulary(self, profile):
        data, _status = server_common.skill_contract_registry(profile)
        bad = [(c['id'], c.get('level'), c.get('scope')) for c in data['contracts']
               if c.get('level') not in ('P0', 'P1', 'P2')
               or c.get('scope') not in ('image', 'video', 'both', 'pipeline')]
        assert not bad, f'{profile} 注册表用了未定义的 level/scope：{bad}'


class TestKnownGapsStayVisible:
    """已知缺口的清单本身就是交付物：它变了要有人知道。"""

    def test_base_gaps_are_exactly_the_three_we_accepted(self):
        report = server_common.skill_contract_report('base')
        assert set(report['unenforced']) == {
            'anchor-frame-compliance',     # 2026-08-05 生成期视觉判定整体移除，只剩 agent 侧口头复核
            'staged-anchor-render-gate',   # agent 侧交付节奏，服务端链路不经过
            'used-topic-ledger-dedup',     # 去重全交给 LLM，产出侧无程序化查重
        }

    def test_omni_gaps_are_exactly_the_two_we_accepted(self):
        report = server_common.skill_contract_report('omni')
        assert set(report['unenforced']) == {
            'omni-grid-notation-ban',      # 与 base 的 SCUP 锚点解析直接冲突
            'omni-ugc-realism-layer',      # 纯风格取向，无确定性判定标准
        }


class TestReportCarriesRegistryStateToTheFrontend:
    @pytest.mark.parametrize('profile', ALL_PROFILES)
    def test_report_exposes_version_and_status(self, profile):
        report = server_common.skill_contract_report(profile)
        assert report['registry_status'] == 'ok'
        assert report['registry_expected'] == server_common.SUPPORTED_CONTRACT_VERSION
        assert report['contract_count'] > 0
        # 原有字段不能因为加了新键而丢
        assert {'profile', 'label', 'package', 'dir', 'source', 'missing', 'total'} <= set(report)

    def test_missing_registry_is_reported_not_swallowed(self, base_skill_dir):
        """包里没有注册表时状态必须是 missing——而不是"没有契约所以一切正常"。"""
        data, status = server_common.skill_contract_registry('base')
        assert (data, status) == (None, 'missing')
        assert server_common.skill_contract_report('base')['registry_status'] == 'missing'

    def test_corrupt_registry_is_reported_as_unreadable(self, base_skill_dir):
        refs = base_skill_dir / 'references'
        refs.mkdir()
        (refs / 'contract-registry.json').write_text('{ not json', encoding='utf-8')
        assert server_common.skill_contract_registry('base')[1] == 'unreadable'

    def test_major_version_drift_is_reported(self, base_skill_dir):
        """次版本兼容（增补条目），主版本不兼容（契约集合变了）。"""
        refs = base_skill_dir / 'references'
        refs.mkdir()
        target = refs / 'contract-registry.json'

        target.write_text(json.dumps({'contract_version': '1.9', 'contracts': [{'id': 'x'}]}),
                          encoding='utf-8')
        assert server_common.skill_contract_registry('base')[1] == 'ok'

        target.write_text(json.dumps({'contract_version': '2.0', 'contracts': [{'id': 'x'}]}),
                          encoding='utf-8')
        # 缓存按 (路径, mtime) 记；同一秒内重写 mtime 可能不变，显式清一次
        server_common._SKILL_REGISTRY_CACHE.clear()
        data, status = server_common.skill_contract_registry('base')
        assert status == 'version_mismatch'
        assert data['contract_version'] == '2.0', '版本不匹配时仍要带回包声明的版本供报错显示'


class TestStrictModeSwitch:
    def test_defaults_to_off(self, monkeypatch):
        monkeypatch.delenv('SKILL_CONTRACT_STRICT', raising=False)
        monkeypatch.setitem(server_common.SERVER_CONFIG, 'strictSkillContract', False)
        assert server_common.skill_contract_strict() is False

    @pytest.mark.parametrize('raw', ['1', 'true', 'TRUE', 'yes', 'on'])
    def test_env_var_turns_it_on(self, raw, monkeypatch):
        monkeypatch.setenv('SKILL_CONTRACT_STRICT', raw)
        assert server_common.skill_contract_strict() is True

    def test_config_key_turns_it_on(self, monkeypatch):
        monkeypatch.delenv('SKILL_CONTRACT_STRICT', raising=False)
        monkeypatch.setitem(server_common.SERVER_CONFIG, 'strictSkillContract', True)
        assert server_common.skill_contract_strict() is True

    def test_env_var_wins_over_config(self, monkeypatch):
        monkeypatch.setenv('SKILL_CONTRACT_STRICT', 'off')
        monkeypatch.setitem(server_common.SERVER_CONFIG, 'strictSkillContract', True)
        assert server_common.skill_contract_strict() is False


class TestStrictModeStopsSilentIdeationDowngrade:
    """形态矩阵缺失时，严格模式必须当场失败而不是降级产出。

    默认行为（打一条 WARN 照跑）保持不变——它是有意的：缺矩阵只是创意变窄，
    不至于让整台服务不可用。严格模式给的是"宁可炸也不要静默劣化"这个选项。
    """

    @pytest.fixture
    def missing_idea_engine(self, monkeypatch):
        import prompt_pipeline as pp
        monkeypatch.setattr(pp, 'load_reference_file', lambda *a, **kw: '')
        monkeypatch.setattr(pp, 'load_used_topic_ledger', lambda *a, **kw: '')

    def test_strict_mode_raises_instead_of_degrading(self, missing_idea_engine, monkeypatch):
        import prompt_pipeline as pp
        monkeypatch.setenv('SKILL_CONTRACT_STRICT', '1')
        with pytest.raises(RuntimeError, match='idea-engine.md'):
            pp.run_ideate({}, count=1)

    def test_default_mode_still_only_warns(self, missing_idea_engine, monkeypatch, capsys):
        """没开严格模式时不能因为这次改动而变成硬失败——那会是一次行为回退。"""
        import prompt_pipeline as pp
        monkeypatch.delenv('SKILL_CONTRACT_STRICT', raising=False)
        monkeypatch.setitem(server_common.SERVER_CONFIG, 'strictSkillContract', False)
        # 让激发在读完矩阵之后、真正打 LLM 之前停下：这里只关心"缺矩阵没抛异常"。
        monkeypatch.setattr(pp, 'read_ledger', lambda *a, **kw: None)
        with pytest.raises(RuntimeError, match='创意台账'):
            pp.run_ideate({}, count=1)
        assert 'idea-engine.md 缺失' in capsys.readouterr().out
