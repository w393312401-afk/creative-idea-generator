"""技能包路径配置项 skillDir 的回归测试（2026-07-26）。

背景：技能契约缺失的告警一直写着「用环境变量 SKILL_DIR / server_config.json 指向技能
所在位置」，但代码只读环境变量——server_config.json 里根本没有这个配置项，照着提示改
配置完全无效，用户只能改环境变量重启。本次补上 skillDir 配置项，并钉住四条行为：
  1. 取值优先级 env > skillDir > 默认路径 > 自动探测；
  2. 显式配置绝不被自动探测顶掉（路径写错要看得见地报缺失，而不是悄悄换一个包）；
  3. 改完 server_config.json 不用重启：下一次激发/合成按新路径读契约文件；
  4. 激发链路（load_reference_file / run_ideate 读的形态矩阵与台账）真的跟着走。
"""
import json
import os

import pytest

import prompt_pipeline as pp
import server_common


def _write_contract(directory, rels=server_common.SKILL_CONTRACT_FILES):
    for rel in rels:
        p = os.path.join(str(directory), *rel.split('/'))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, 'w', encoding='utf-8') as f:
            f.write(f'# {rel}')
    return str(directory)


@pytest.fixture
def cfg_file(tmp_path, monkeypatch):
    """把 server_config.json 指到临时文件，并复位 skillDir 的解析状态。

    monkeypatch 会在测试结束时把 SKILL_DIR / _SKILL_CONFIG_MTIME 等模块全局恢复原值，
    所以这些测试不会把开发机的真实技能路径污染给同批次的其他测试。"""
    path = tmp_path / 'server_config.json'
    path.write_text('{}', encoding='utf-8')
    monkeypatch.setattr(server_common, 'SERVER_CONFIG_FILE', str(path))
    monkeypatch.delenv('SKILL_DIR', raising=False)
    monkeypatch.delitem(server_common.SERVER_CONFIG, 'skillDir', raising=False)
    # 自动探测不许扫到开发机上真实存在的技能目录，否则断言随机器而变
    monkeypatch.setattr(server_common, '_SKILL_ROOT_CANDIDATES', ())
    monkeypatch.setattr(server_common, '_DEFAULT_SKILL_DIR', str(tmp_path / 'default-skill'))
    monkeypatch.setattr(server_common, 'SKILL_DIR', str(tmp_path / 'default-skill'))
    monkeypatch.setattr(server_common, 'SKILL_DIR_SOURCE', 'default')
    monkeypatch.setattr(server_common, '_SKILL_CONFIG_MTIME',
                        server_common._skill_config_mtime())
    return path


def _set_config(cfg_file, **keys):
    """写配置并保证 mtime 一定变化（同秒内多次写在低精度文件系统上可能同值）。"""
    cfg_file.write_text(json.dumps(keys), encoding='utf-8')
    stat = cfg_file.stat()
    os.utime(str(cfg_file), (stat.st_atime + 10, stat.st_mtime + 10))


class TestResolutionPriority:
    def test_config_skill_dir_is_used(self, cfg_file, tmp_path):
        target = _write_contract(tmp_path / 'my-skill')
        _set_config(cfg_file, skillDir=target)

        assert server_common.skill_dir() == target
        assert server_common.skill_contract_report()['source'] == 'config'
        assert server_common.missing_skill_contract_files() == []

    def test_env_wins_over_config(self, cfg_file, tmp_path, monkeypatch):
        from_env = _write_contract(tmp_path / 'env-skill')
        _set_config(cfg_file, skillDir=str(tmp_path / 'config-skill'))
        monkeypatch.setenv('SKILL_DIR', from_env)

        assert server_common.skill_dir() == from_env
        assert server_common.skill_contract_report()['source'] == 'env'

    def test_user_path_and_relative_path_are_expanded(self, cfg_file, monkeypatch, tmp_path):
        monkeypatch.setenv('HOME', str(tmp_path))
        monkeypatch.setattr(server_common, '_PROJECT_ROOT', str(tmp_path / 'proj'))

        _set_config(cfg_file, skillDir='~/skills/composer')
        assert server_common.skill_dir() == os.path.join(str(tmp_path), 'skills', 'composer')

        _set_config(cfg_file, skillDir='skills/composer')
        assert server_common.skill_dir() == os.path.join(str(tmp_path), 'proj', 'skills', 'composer')

    def test_blank_config_falls_back_to_default(self, cfg_file, tmp_path):
        _set_config(cfg_file, skillDir='   ')
        assert server_common.skill_dir() == str(tmp_path / 'default-skill')
        assert server_common.skill_contract_report()['source'] == 'default'

    def test_wrong_explicit_path_is_reported_not_silently_replaced(self, cfg_file, tmp_path, monkeypatch):
        """显式配错了，就该在缺失清单上看见——绝不能被自动探测悄悄换成别的包。"""
        real = _write_contract(tmp_path / 'roots' / 'renamed-composer')
        monkeypatch.setattr(server_common, '_SKILL_ROOT_CANDIDATES', (str(tmp_path / 'roots'),))
        _set_config(cfg_file, skillDir=str(tmp_path / 'typo'))

        report = server_common.skill_contract_report()
        assert report['dir'] == str(tmp_path / 'typo')
        assert report['dir'] != real
        assert len(report['missing']) == report['total']


class TestAutodetect:
    def test_renamed_package_is_found_when_nothing_is_configured(self, cfg_file, tmp_path, monkeypatch):
        renamed = _write_contract(tmp_path / 'roots' / 'gemini-omni-restoration-composer')
        monkeypatch.setattr(server_common, '_SKILL_ROOT_CANDIDATES', (str(tmp_path / 'roots'),))
        _set_config(cfg_file, debug=True)

        assert server_common.skill_dir() == renamed
        assert server_common.skill_contract_report()['source'] == 'autodetect'

    def test_unrelated_skill_package_is_not_adopted(self, cfg_file, tmp_path, monkeypatch):
        """只有一个 SKILL.md 的无关技能包不算数（否则随便一个技能都会被误认）。"""
        _write_contract(tmp_path / 'roots' / 'n8n-code-python', rels=('SKILL.md',))
        monkeypatch.setattr(server_common, '_SKILL_ROOT_CANDIDATES', (str(tmp_path / 'roots'),))
        _set_config(cfg_file, debug=True)

        assert server_common.skill_dir() == str(tmp_path / 'default-skill')
        assert server_common.skill_contract_report()['source'] == 'default'


class TestHotReload:
    def test_editing_config_takes_effect_without_restart(self, cfg_file, tmp_path):
        first = _write_contract(tmp_path / 'skill-a')
        _set_config(cfg_file, skillDir=first)
        assert server_common.skill_dir() == first

        second = _write_contract(tmp_path / 'skill-b')
        _set_config(cfg_file, skillDir=second)
        assert server_common.skill_dir() == second

    def test_removing_the_key_falls_back_again(self, cfg_file, tmp_path):
        _set_config(cfg_file, skillDir=_write_contract(tmp_path / 'skill-a'))
        server_common.skill_dir()

        _set_config(cfg_file, debug=True)
        assert server_common.skill_dir() == str(tmp_path / 'default-skill')


class TestIdeationReadsTheConfiguredPackage:
    def test_reference_files_follow_the_configured_dir(self, cfg_file, tmp_path, monkeypatch):
        monkeypatch.setattr(pp, '_REFERENCE_MISS_LOGGED', set())
        skill = tmp_path / 'skill'
        os.makedirs(str(skill / 'references'))
        (skill / 'references' / 'idea-engine.md').write_text('形态矩阵内容', encoding='utf-8')
        _set_config(cfg_file, skillDir=str(skill))

        assert pp.load_reference_file('idea-engine.md') == '形态矩阵内容'

    def test_switching_dir_re_warns_about_the_new_dirs_missing_files(self, cfg_file, tmp_path, capsys, monkeypatch):
        """「缺失只提示一次」按完整路径记：换了技能目录必须重新提示一次，
        否则新目录的缺失会被旧目录的记录吃掉，等于换错路径后毫无反馈。"""
        monkeypatch.setattr(pp, '_REFERENCE_MISS_LOGGED', set())
        _set_config(cfg_file, skillDir=str(tmp_path / 'skill-a'))
        assert pp.load_reference_file('idea-engine.md') == ''
        assert 'skill-a' in capsys.readouterr().out

        assert pp.load_reference_file('idea-engine.md') == ''
        assert capsys.readouterr().out == ''

        _set_config(cfg_file, skillDir=str(tmp_path / 'skill-b'))
        assert pp.load_reference_file('idea-engine.md') == ''
        assert 'skill-b' in capsys.readouterr().out
