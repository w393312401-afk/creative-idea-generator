"""创意台账（topic_ledger.json）读写与迁移解析的行为契约。

关注点：
- 与 library.json 同一套整库清零防护：空列表拒绝覆盖非空（409）、写前 .bak 轮换、
  原子替换；
- 读取损坏文件必须返回 None（由调用方决定报 500），不能静默降级为 [] ——
  那正是 library.json 历史事故的触发路径；
- used-topic-ledger.md 的迁移解析：表格在文件中途被一段标题打断、随后又继续
  出现数据行，解析器必须按"是否是 | 开头的数据行"逐行扫描全文而不是假设单一
  连续表格；解析器本身不去重（去重是迁移脚本的职责，不是解析器的）；
- 按 id 批量删除（delete_ledger_entries）不吃 write_ledger 的空列表覆盖防护——
  显式 id 列表本身就是"确有意图"的证据，全选删空也必须能做到。
"""
import json
import os

import pytest

from server_common import (
    read_ledger, write_ledger, parse_legacy_ledger_md, delete_ledger_entries,
    register_ledger_candidates,
)


@pytest.fixture
def ledger_path(tmp_path):
    return os.path.join(str(tmp_path), 'topic_ledger.json')


def _entry(topic_dna='natural / refuge-den / self-material-window', **overrides):
    e = {
        'id': 'abc-123',
        'date': '2026-07-01',
        'topic_dna': topic_dna,
        'one_line': '测试选题',
        'source': 'GUI Generation',
        'avoid_notes': '',
        'status': 'used',
        'llm_score': 23,
        'user_score': None,
        'performance_note': '',
    }
    e.update(overrides)
    return e


class TestReadLedger:
    def test_missing_file_returns_empty_list(self, ledger_path):
        assert read_ledger(ledger_path) == []

    def test_corrupt_file_returns_none(self, ledger_path):
        with open(ledger_path, 'w', encoding='utf-8') as f:
            f.write('{not valid json')
        assert read_ledger(ledger_path) is None

    def test_non_list_json_returns_none(self, ledger_path):
        with open(ledger_path, 'w', encoding='utf-8') as f:
            json.dump({'oops': 'not a list'}, f)
        assert read_ledger(ledger_path) is None


class TestWriteLedger:
    def test_write_then_read_roundtrip(self, ledger_path):
        entries = [_entry(), _entry(topic_dna='vehicle / micro-home / porthole-lighting')]
        ok, msg = write_ledger(entries, ledger_path)
        assert ok is True and msg is None
        assert read_ledger(ledger_path) == entries

    def test_first_write_of_empty_list_is_allowed(self, ledger_path):
        ok, msg = write_ledger([], ledger_path)
        assert ok is True
        assert read_ledger(ledger_path) == []

    def test_rejects_empty_overwrite_of_nonempty(self, ledger_path):
        write_ledger([_entry()], ledger_path)
        ok, msg = write_ledger([], ledger_path)
        assert ok is False
        assert '拒绝' in msg
        # 拒绝时原文件必须原封不动
        assert read_ledger(ledger_path) == [_entry()]

    def test_rejects_undeclared_shrink_of_nonempty(self, ledger_path):
        """2026-07-30 收紧：非空 → 非空的缩量也拦。此前只堵"覆盖成空"这一头，中间
        那一大片"少一条"完全没人管——而整表回写只用于编辑（状态/评分/备注），删除
        一律走 /api/ledger/delete 的按 id 删，所以这条路径上的缩量都是状态错乱。

        真实触发路径：合成流程用 register_ledger_candidates 在服务端追加候选，一个在
        那之前打开的页面手里是短一截的表，此后任何一次改评分都会把新登记的候选整批
        抹掉——与 library.json 那两次事故同一个机制。"""
        write_ledger([_entry(id='a'), _entry(id='b', topic_dna='b')], ledger_path)
        ok, msg = write_ledger([_entry(id='a')], ledger_path)
        assert ok is False
        assert '已阻止' in msg and '创意台账' in msg
        # 拒绝时原文件必须原封不动
        assert len(read_ledger(ledger_path)) == 2

    def test_allows_edits_that_keep_every_row(self, ledger_path):
        """编辑（改状态/评分/备注）才是整表回写的正当用途，不能被这道防护误伤。"""
        write_ledger([_entry(id='a'), _entry(id='b', topic_dna='b')], ledger_path)
        ok, msg = write_ledger(
            [_entry(id='a', user_score=9), _entry(id='b', topic_dna='b')], ledger_path)
        assert ok is True and msg is None
        assert read_ledger(ledger_path)[0]['user_score'] == 9

    def test_delete_by_id_still_bypasses_the_guard(self, ledger_path):
        """按 id 删除天然带着"确有意图"的证据，不吃这道防护——否则台账就没法删了。"""
        write_ledger([_entry(id='a'), _entry(id='b', topic_dna='b')], ledger_path)
        result = delete_ledger_entries(['b'], ledger_path)
        assert result['deleted'] == 1
        assert [e['id'] for e in read_ledger(ledger_path)] == ['a']

    def test_creates_bak_before_overwriting_nonempty(self, ledger_path):
        write_ledger([_entry()], ledger_path)
        assert not os.path.exists(ledger_path + '.bak')
        write_ledger([_entry(topic_dna='b')], ledger_path)
        assert os.path.exists(ledger_path + '.bak')
        with open(ledger_path + '.bak', 'r', encoding='utf-8') as f:
            backed_up = json.load(f)
        assert backed_up == [_entry()]

    def test_rejects_non_list_payload(self, ledger_path):
        ok, msg = write_ledger({'not': 'a list'}, ledger_path)
        assert ok is False
        assert not os.path.exists(ledger_path)


class TestRegisterLedgerCandidates:
    def test_atomically_adds_generated_ideas_as_candidates(self, ledger_path):
        result = register_ledger_candidates([
            {'dna': 'natural / refuge / window', 'title': '冰洞住宅', 'score': 24},
            {'dna': 'vehicle / micro-home / brass', 'title': '潜艇小屋', 'score': 22},
        ], ledger_path)

        assert result['added'] == 2
        assert result['duplicates'] == 0
        rows = read_ledger(ledger_path)
        assert [row['status'] for row in rows] == ['candidate', 'candidate']
        assert [row['one_line'] for row in rows] == ['冰洞住宅', '潜艇小屋']

    def test_dedupes_existing_and_same_batch_dna_case_insensitively(self, ledger_path):
        write_ledger([_entry(topic_dna='Natural / Refuge / Window')], ledger_path)
        result = register_ledger_candidates([
            {'dna': ' natural / refuge / window ', 'title': '重复一'},
            {'dna': 'VEHICLE / HOME / BRASS', 'title': '新创意'},
            {'dna': 'vehicle / home / brass', 'title': '批内重复'},
        ], ledger_path)

        assert result['added'] == 1
        assert result['duplicates'] == 2
        assert len(read_ledger(ledger_path)) == 2

    def test_preserves_whitelisted_creative_seed_for_remix(self, ledger_path):
        register_ledger_candidates([{
            'dna': 'silo / refuge / brass',
            'title': '谷仓黄铜隐居屋',
            'creative_seed': {
                'input_str': '把废弃谷仓改成黄铜隐居屋',
                'carrier': 'grain silo',
                'twist_zh': '黄铜机械夹层',
                'unexpected': '不应持久化',
            },
        }], ledger_path)

        row = read_ledger(ledger_path)[0]
        assert row['creative_seed'] == {
            'input_str': '把废弃谷仓改成黄铜隐居屋',
            'carrier': 'grain silo',
            'twist_zh': '黄铜机械夹层',
        }

    def test_corrupt_ledger_fails_closed(self, ledger_path):
        with open(ledger_path, 'w', encoding='utf-8') as f:
            f.write('{broken')

        with pytest.raises(RuntimeError, match='创意台账读取失败'):
            register_ledger_candidates([{'dna': 'a / b / c', 'title': '不会写入'}], ledger_path)


class TestParseLegacyLedgerMd:
    def test_skips_header_and_separator_rows(self):
        content = (
            "| Date | Topic DNA (carrier / destiny / twist) | 一句话选题 | Source | Avoid Notes |\n"
            "|---|---|---|---|---|\n"
            "| 2026-06-22 | natural / refuge-den / self-material-window | 测试选题A | GUI Generation | note A |\n"
        )
        entries = parse_legacy_ledger_md(content)
        assert len(entries) == 1
        assert entries[0]['topic_dna'] == 'natural / refuge-den / self-material-window'
        assert entries[0]['one_line'] == '测试选题A'
        assert entries[0]['status'] == 'used'
        assert entries[0]['llm_score'] is None and entries[0]['user_score'] is None

    def test_resumes_parsing_data_rows_after_mid_file_heading(self):
        # 真实文件里出现过的坑：表格中途被 "## Avoid List" 标题打断，随后又继续
        # 出现数据行——两侧的数据行都必须被解析进来
        content = (
            "| Date | Topic DNA | 一句话选题 | Source | Avoid Notes |\n"
            "|---|---|---|---|---|\n"
            "| 2026-06-22 | natural / refuge-den / twist-a | 选题A | GUI Generation | note a |\n"
            "\n"
            "## Avoid List\n"
            "- some free-form bullet, not a table row\n"
            "\n"
            "| 2026-07-01 | vehicle / micro-home / twist-b | 选题B | GUI Generation | note b |\n"
        )
        entries = parse_legacy_ledger_md(content)
        dnas = [e['topic_dna'] for e in entries]
        assert dnas == ['natural / refuge-den / twist-a', 'vehicle / micro-home / twist-b']

    def test_does_not_dedupe_identical_topic_dna(self):
        # 解析器本身只负责把每一行数据行转成结构化条目；去重是迁移脚本的职责
        content = (
            "| 2026-06-22 | natural / dup / twist | 选题A | src | note |\n"
            "| 2026-07-01 | natural / dup / twist | 选题A重复 | src | note |\n"
        )
        entries = parse_legacy_ledger_md(content)
        assert len(entries) == 2
        assert entries[0]['id'] != entries[1]['id']

    def test_ignores_non_table_lines(self):
        content = "# Used Topic Ledger\n\nSome prose paragraph.\n\n| 2026-06-22 | a / b / c | 选题 | src | note |\n"
        entries = parse_legacy_ledger_md(content)
        assert len(entries) == 1

    def test_empty_content_returns_empty_list(self):
        assert parse_legacy_ledger_md('') == []


class TestDeleteLedgerEntries:
    def test_deletes_matching_ids_only(self, ledger_path):
        a, b, c = _entry(topic_dna='a'), _entry(topic_dna='b'), _entry(topic_dna='c')
        a['id'], b['id'], c['id'] = 'id-a', 'id-b', 'id-c'
        write_ledger([a, b, c], ledger_path)

        result = delete_ledger_entries(['id-b'], ledger_path)
        assert result['deleted'] == 1
        assert [e['id'] for e in result['remaining']] == ['id-a', 'id-c']
        assert [e['id'] for e in read_ledger(ledger_path)] == ['id-a', 'id-c']

    def test_can_delete_all_entries_bypassing_empty_overwrite_guard(self, ledger_path):
        # 这是与 write_ledger([]) 的关键差异：write_ledger 会 409 拒绝，
        # delete_ledger_entries 按 id 删必须能把台账真正清空
        a, b = _entry(topic_dna='a'), _entry(topic_dna='b')
        a['id'], b['id'] = 'id-a', 'id-b'
        write_ledger([a, b], ledger_path)

        ok, msg = write_ledger([], ledger_path)
        assert ok is False  # 佐证：整表回写确实还是被挡的

        result = delete_ledger_entries(['id-a', 'id-b'], ledger_path)
        assert result['deleted'] == 2
        assert result['remaining'] == []
        assert read_ledger(ledger_path) == []

    def test_ignores_unknown_ids(self, ledger_path):
        a = _entry(topic_dna='a')
        a['id'] = 'id-a'
        write_ledger([a], ledger_path)
        result = delete_ledger_entries(['no-such-id'], ledger_path)
        assert result['deleted'] == 0
        assert result['remaining'] == [a]
        assert read_ledger(ledger_path) == [a]

    def test_empty_id_list_is_a_no_op(self, ledger_path):
        a = _entry(topic_dna='a')
        write_ledger([a], ledger_path)
        result = delete_ledger_entries([], ledger_path)
        assert result == {'deleted': 0, 'remaining': []}
        assert read_ledger(ledger_path) == [a]  # 文件未被触碰

    def test_missing_file_is_a_no_op(self, ledger_path):
        result = delete_ledger_entries(['whatever'], ledger_path)
        assert result == {'deleted': 0, 'remaining': []}

    def test_corrupt_file_reports_remaining_none(self, ledger_path):
        with open(ledger_path, 'w', encoding='utf-8') as f:
            f.write('{not valid json')
        result = delete_ledger_entries(['whatever'], ledger_path)
        assert result['deleted'] == 0
        assert result['remaining'] is None

    def test_creates_bak_before_deleting(self, ledger_path):
        a, b = _entry(topic_dna='a'), _entry(topic_dna='b')
        a['id'], b['id'] = 'id-a', 'id-b'
        write_ledger([a, b], ledger_path)
        assert not os.path.exists(ledger_path + '.bak')
        delete_ledger_entries(['id-a'], ledger_path)
        assert os.path.exists(ledger_path + '.bak')
        with open(ledger_path + '.bak', 'r', encoding='utf-8') as f:
            backed_up = json.load(f)
        assert [e['id'] for e in backed_up] == ['id-a', 'id-b']
