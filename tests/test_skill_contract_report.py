"""技能契约缺失的可见性回归测试（2026-07-25）。

背景：SKILL_DIR 下的 8 个契约文件（形态矩阵 idea-engine.md、提示词模板
prompt-templates.md、三份一致性协议等）缺失时不会报错——load_reference_file 返回空串、
run_ideate 拿到空矩阵，整条合成管线照样跑完并产出劣化结果。此前：
  1. 启动检查只查了 8 个里的 2 个，另外 6 个缺失时全程无声；
  2. load_reference_file 只在"读取报错"时打日志，"文件不存在"一行都不打；
  3. 结论只写进服务端日志，从浏览器用的人根本看不到。
本测试钉住修复后的三条：清单覆盖全集、缺失一次性告警、/api/mode 把结论带给前端。
"""
import os

import pytest

import prompt_pipeline as pp
import server_common


@pytest.fixture
def skill_dir(tmp_path, monkeypatch):
    """把 SKILL_DIR 指到临时目录。

    各模块都是 `from server_common import *` 拿到的**副本**，改 server_common 那一份
    不会传导过去，所以每个消费方都要单独 patch。这也正是 skill_contract_report() 存在
    的理由：dir 与 missing 都在 server_common 内部取值，调用方不再各持一份可能过期的
    SKILL_DIR。（此前 /api/mode 直接引用 server.SKILL_DIR，这条测试单独跑时因为
    `import server` 发生在 patch 之后而“碰巧”通过，全量跑就会挂。）"""
    import server

    d = tmp_path / 'skill'
    (d / 'references').mkdir(parents=True)
    monkeypatch.setattr(server_common, 'SKILL_DIR', str(d))
    monkeypatch.setattr(pp, 'SKILL_DIR', str(d))
    monkeypatch.setattr(server, 'SKILL_DIR', str(d), raising=False)
    return d


class TestMissingSkillContractFiles:
    def test_every_file_the_pipeline_actually_reads_is_covered(self):
        """清单必须覆盖合成真正会读的全部文件。少一个，就等于少一条可见性。"""
        assert set(server_common.SKILL_CONTRACT_FILES) == {
            'SKILL.md',
            'references/prompt-templates.md',
            'references/idea-engine.md',
            'references/used-topic-ledger.md',
            'references/space-workflows.md',
            'references/spatial-consistency-upgrade-protocol.md',
            'references/drift-lock-assembly-guide.md',
            'references/threshold-bridge-consistency-protocol.md',
        }

    def test_all_missing_when_dir_is_empty(self, skill_dir):
        missing = server_common.missing_skill_contract_files()
        assert len(missing) == len(server_common.SKILL_CONTRACT_FILES)

    def test_present_files_are_not_reported(self, skill_dir):
        (skill_dir / 'SKILL.md').write_text('x', encoding='utf-8')
        (skill_dir / 'references' / 'idea-engine.md').write_text('y', encoding='utf-8')

        missing = server_common.missing_skill_contract_files()
        assert 'SKILL.md' not in missing
        assert 'references/idea-engine.md' not in missing
        assert 'references/prompt-templates.md' in missing
        assert len(missing) == len(server_common.SKILL_CONTRACT_FILES) - 2

    def test_nothing_missing_when_contract_is_complete(self, skill_dir):
        for rel in server_common.SKILL_CONTRACT_FILES:
            p = skill_dir.joinpath(*rel.split('/'))
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text('content', encoding='utf-8')
        assert server_common.missing_skill_contract_files() == []


class TestLoadReferenceFileAnnouncesMisses:
    def test_missing_file_warns_once_then_stays_quiet(self, skill_dir, capsys, monkeypatch):
        """缺文件必须打一行 WARN（此前完全无声），但逐拍调用不能刷屏。"""
        monkeypatch.setattr(pp, '_REFERENCE_MISS_LOGGED', set())

        assert pp.load_reference_file('prompt-templates.md') == ''
        first = capsys.readouterr().out
        assert '技能契约文件缺失' in first
        assert 'prompt-templates.md' in first

        for _ in range(5):
            assert pp.load_reference_file('prompt-templates.md') == ''
        assert capsys.readouterr().out == ''

        # 另一个文件仍然各报一次
        pp.load_reference_file('space-workflows.md')
        assert 'space-workflows.md' in capsys.readouterr().out

    def test_existing_file_is_read_without_warning(self, skill_dir, capsys, monkeypatch):
        monkeypatch.setattr(pp, '_REFERENCE_MISS_LOGGED', set())
        (skill_dir / 'references' / 'space-workflows.md').write_text('真实内容', encoding='utf-8')

        assert pp.load_reference_file('space-workflows.md') == '真实内容'
        assert '缺失' not in capsys.readouterr().out


class TestModeEndpointReportsSkillContract:
    def test_report_does_not_depend_on_a_stale_wildcard_copy(self, skill_dir, monkeypatch):
        """skill_contract_report() 的 dir 必须跟随 server_common.SKILL_DIR，
        而不是任何调用方模块里导入时抓下来的副本。"""
        import server

        monkeypatch.setattr(server, 'SKILL_DIR', '/definitely/not/used', raising=False)
        assert server_common.skill_contract_report()['dir'] == str(skill_dir)

    def _mode_payload(self, monkeypatch):
        import server

        sent = []
        h = object.__new__(server.SparkRequestHandler)
        monkeypatch.setattr(server.SparkRequestHandler, '_send_json',
                            lambda self, body, status=200: sent.append((body, status)),
                            raising=False)
        h.path = '/api/mode'
        h.headers = {}
        server.SparkRequestHandler.do_GET(h)
        assert sent, 'no response captured'
        return sent[0][0]

    def test_missing_list_is_reported_to_the_frontend(self, skill_dir, monkeypatch):
        body = self._mode_payload(monkeypatch)
        assert body['skill_contract']['dir'] == str(skill_dir)
        assert body['skill_contract']['total'] == len(server_common.SKILL_CONTRACT_FILES)
        assert len(body['skill_contract']['missing']) == len(server_common.SKILL_CONTRACT_FILES)

    def test_complete_contract_reports_an_empty_missing_list(self, skill_dir, monkeypatch):
        for rel in server_common.SKILL_CONTRACT_FILES:
            p = skill_dir.joinpath(*rel.split('/'))
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text('content', encoding='utf-8')

        body = self._mode_payload(monkeypatch)
        assert body['skill_contract']['missing'] == []
        # 原有字段不能因为加了新键而丢
        assert 'server_managed' in body
        assert 'needs_access_code' in body
