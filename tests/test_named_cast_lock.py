"""具名人物一致性锁（Named Cast Lock）的门禁回归。

背景：跨段无记忆的流水线里，"人物一致"100% 来自每一段逐字重述同一组锚点词。那是一条
字符串相等性质，人眼复读十四条提示词是错的工具——一个 `khaki jacket` 就在那一段生成了
另一个人，而通读散文恰好是这种事故的滑出通道。

这份测试钉住的是三件此前**静默失效**的事：

  1. `action_onset_markers` 出参：判据"身份块必须前置于动作起点标记之前"此前把标记写死成
     模块常量 `PACING_PHRASE = 'continuous construction time-lapse'`，于是任何非施工题材
     上这道门从不执行——报告干净、退出码 0，缺陷完全看不见。静默跳过比没有这道门更危险，
     所以"判不了"现在自己就是一条发现（warn 级）。
  2. Gate 8 两锁互斥：Hero Agent Lock 与 Named Cast Lock 互斥是 P0，但此前没有任何执行点。
  3. 服务端接入：此前唯一执行点是一个要人记得跑的 CLI。记不住就等于没有。

以及一条不变量：这些消息**不得**被 `split_structural_video_errors` 判成结构性硬伤——
那一档会为该拍烧掉一轮定向回炉，而"外套写错色"不该抢走"这拍没有可拍的画面"的修复预算。
"""
import importlib.util
import json
import os

import pytest

import prompt_pipeline as pp
from prompt_pipeline import cast_lock


_CORE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'skills', 'gemini-veo-restoration-composer', 'scripts', 'cast_lock_core.py')


def _load_core():
    spec = importlib.util.spec_from_file_location('_cast_lock_core_test', _CORE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


core = _load_core()


@pytest.fixture
def registry():
    """真实的 cast-registry.json —— 刻意不用夹具替身。

    这份注册表就是被测契约的正文（身份块、T1 词表、常驻负面项、两锁互斥标记）。用一份
    手写替身来测，测到的是替身，而注册表本身漂了不会有任何信号。"""
    path = cast_lock._registry_path()
    assert path, "cast-registry.json 应当能被解析到"
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


@pytest.fixture
def jake(registry):
    cast = core.find_cast(registry, 'jake-miller')
    assert cast is not None
    return cast


def _locked_video(jake, tail=""):
    """一条本应完全合法的 VIDEO 提示词：身份块前置、常驻负面项齐全。"""
    return (
        f"At the first instant, {jake['identity_blocks']['short']} kneels at the work face "
        f"and begins scraping the surface, a continuous construction time-lapse, not "
        f"real-time footage. "
        f"No readable text or logos on clothing, no change of jacket colour, "
        f"no second person resembling him.{tail}"
    )


class TestBaselineStillPasses:
    def test_a_fully_locked_prompt_has_no_findings(self, jake, registry):
        fnd = core.Findings()
        globals_ = core.registry_globals(registry, jake)
        assert core.audit_prompt('VIDEO 3', _locked_video(jake), jake, globals_, fnd) is True
        assert fnd.rows == []

    def test_delivered_reference_set_still_passes(self, jake, registry):
        """已交付的 saguaro 提示词集是这条锁的活基线；抽取器或门禁改动把它判红即是回归。"""
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            'veo_petrified_saguaro_prompt_set.md')
        if not os.path.exists(path):
            pytest.skip('参考提示词集不在仓库里')
        with open(path, 'r', encoding='utf-8') as f:
            prompts = core.split_prompts(f.read())
        fnd, stats = core.audit_prompt_set(prompts, jake, core.registry_globals(registry, jake))
        assert stats['judged'] > 0, '基线集里应当有被判的含人物槽位'
        assert fnd.errors == [], [r[:3] for r in fnd.errors]


class TestActionOnsetMarkers:
    """P0-1：动作起点标记出参，且"判不了"要出声。"""

    def test_marker_list_comes_from_the_registry(self, registry, jake):
        globals_ = core.registry_globals(registry, jake)
        assert globals_['action_onset_markers'] == registry['action_onset_markers']
        assert globals_['action_onset_markers'], '注册表必须登记至少一条标记'

    def test_per_cast_override_wins(self, registry, jake):
        cast = dict(jake, action_onset_markers=['the kettle starts to whistle'])
        globals_ = core.registry_globals(registry, cast)
        assert globals_['action_onset_markers'] == ['the kettle starts to whistle']

    def test_placement_is_judged_against_a_custom_marker(self, registry, jake):
        """换题材后这道门必须真的执行——这是此前静默失效的那一半。"""
        cast = dict(jake, action_onset_markers=['a continuous kitchen time-lapse'])
        globals_ = core.registry_globals(registry, cast)
        body = (
            "A continuous kitchen time-lapse, not real-time footage, as the dough is folded "
            f"again and again by {jake['identity_blocks']['short']} working steadily. "
            "No readable text or logos on clothing, no change of jacket colour, "
            "no second person resembling him."
        )
        fnd = core.Findings()
        core.audit_prompt('VIDEO 4', body, cast, globals_, fnd)
        gates = [r[0] for r in fnd.rows]
        assert 'identity-block-placement' in gates, gates

    def test_no_marker_warns_instead_of_passing_silently(self, registry, jake):
        """一条标记都不命中：此前 return 掉、报告干净；现在必须留下一条 warn。"""
        body = (
            f"{jake['identity_blocks']['short']} lifts the panel into place. "
            "No readable text or logos on clothing, no change of jacket colour, "
            "no second person resembling him."
        )
        fnd = core.Findings()
        core.audit_prompt('VIDEO 5', body, jake, core.registry_globals(registry, jake), fnd)
        gates = [r[0] for r in fnd.rows]
        assert gates == ['identity-block-placement-unverifiable'], gates
        assert fnd.errors == []
        assert len(fnd.warnings) == 1

    def test_unverifiable_placement_does_not_fail_the_run_but_strict_does(self, registry, jake):
        """warn 的语义：默认不判死（否则新题材第一天全红，逼人编一个假标记来消音），
        --strict 下可提级。"""
        body = (
            f"{jake['identity_blocks']['short']} lifts the panel into place. "
            "No readable text or logos on clothing, no change of jacket colour, "
            "no second person resembling him."
        )
        fnd = core.Findings()
        core.audit_prompt('VIDEO 5', body, jake, core.registry_globals(registry, jake), fnd)
        assert not fnd.errors and fnd.warnings


class TestLockExclusivity:
    """P0-2：Gate 8，此前完全没有执行点的那条 P0。"""

    def test_hero_agent_lock_residue_is_caught(self, registry, jake):
        globals_ = core.registry_globals(registry, jake)
        body = _locked_video(jake, tail=" A second figure in a white hardhat waits nearby.")
        fnd = core.Findings()
        core.audit_prompt('VIDEO 6', body, jake, globals_, fnd)
        assert 'hero-agent-lock-residue' in [r[0] for r in fnd.rows]

    def test_negated_marker_is_not_residue(self, registry, jake):
        """负面清单靠点名来禁止——`no white hardhat` 是执行契约的那句话本身。
        不豁免它，唯一的干净跑法就是删掉负面清单。"""
        globals_ = core.registry_globals(registry, jake)
        body = _locked_video(jake, tail=" No white hardhat, no hi-vis vest.")
        fnd = core.Findings()
        core.audit_prompt('VIDEO 7', body, jake, globals_, fnd)
        assert 'hero-agent-lock-residue' not in [r[0] for r in fnd.rows]

    def test_gate_runs_on_slots_that_declare_themselves_person_free(self, registry, jake):
        """自称无人却还挂着背心句的槽位，正是这道门要抓的残留；contains_person 会跳过它。"""
        globals_ = core.registry_globals(registry, jake)
        body = ("The frame is empty of workers, though a solid bright-neon-yellow safety vest "
                "hangs on the scaffold.")
        fnd = core.Findings()
        assert core.audit_prompt('VIDEO 8', body, jake, globals_, fnd) is False
        assert [r[0] for r in fnd.rows] == ['hero-agent-lock-residue']


class TestVocabularyDriftStillWorks:
    def test_banned_synonym_outside_the_identity_block_is_caught(self, registry, jake):
        globals_ = core.registry_globals(registry, jake)
        body = _locked_video(jake, tail=" His khaki jacket catches the low sun.")
        fnd = core.Findings()
        core.audit_prompt('VIDEO 9', body, jake, globals_, fnd)
        assert 'vocabulary-drift' in [r[0] for r in fnd.rows]

    def test_required_term_containing_a_banned_substring_is_not_a_hit(self, registry, jake):
        """`dark-brown` 含 `brown boots`、`faded olive-green work jacket` 含
        `green work jacket`——不先屏蔽合法载体，每条合法提示词都会假红。"""
        globals_ = core.registry_globals(registry, jake)
        fnd = core.Findings()
        core.audit_prompt('VIDEO 10', _locked_video(jake), jake, globals_, fnd)
        assert 'vocabulary-drift' not in [r[0] for r in fnd.rows]


class TestPipelineModeGating:
    def test_mode_a_judges_video_only(self, jake):
        assert core.judged_kinds_for(jake) == ('VIDEO',)

    def test_mode_b_judges_images_too(self, jake):
        assert core.judged_kinds_for(dict(jake, pipeline_mode='B')) == ('VIDEO', 'IMAGE')


class TestServerSideWiring:
    """P0-4：锁不再依赖"有人记得跑 CLI"。"""

    def test_off_by_default(self, jake):
        """未显式选具名锁的单子必须整段 no-op —— Hero Agent Lock 仍是本包默认。"""
        body = "one lone worker in a solid bright-neon-yellow safety vest sweeps the floor."
        assert pp.named_cast_beat_violations(body, body, {'camera_dna': 'whatever'}) == []

    def test_fires_when_the_packet_selects_named_cast(self, jake):
        packet = {'agent_lock_mode': 'named_cast', 'cast_id': 'jake-miller'}
        bad = ("one worker in a khaki jacket keeps sweeping, a continuous construction "
               "time-lapse, not real-time footage.")
        out = pp.named_cast_beat_violations('an empty room, no people', bad, packet)
        assert out, '选了具名锁却没有任何发现'
        assert all(m.startswith('Named Cast Lock [') for m in out), out

    def test_a_locked_beat_produces_nothing(self, jake):
        packet = {'agent_lock_mode': 'named_cast', 'cast_id': 'jake-miller'}
        out = pp.named_cast_beat_violations('an empty room, no people at all',
                                            _locked_video(jake), packet)
        assert out == [], out

    def test_unknown_cast_id_degrades_to_no_enforcement(self):
        packet = {'agent_lock_mode': 'named_cast', 'cast_id': 'nobody-registered'}
        assert pp.named_cast_beat_violations('x', 'one worker sweeps', packet) == []

    def test_mode_a_image_prompt_is_not_judged(self, jake):
        """模式 A 下 IMAGE 锚点按契约无人，且已由 Clean Frame Boundary 拥有——
        在这里再判一遍只会把同一帧用两个名字报两次。"""
        packet = {'agent_lock_mode': 'named_cast', 'cast_id': 'jake-miller'}
        image = "one worker in a khaki jacket stands in the doorway."
        assert pp.named_cast_beat_violations(image, '', packet) == []

    def test_packet_is_stamped_from_config(self):
        packet = pp.apply_named_cast_settings(
            {'camera_dna': 'x'}, {'agentLockMode': 'named_cast', 'castId': 'jake-miller'})
        assert packet['agent_lock_mode'] == 'named_cast'
        assert packet['cast_id'] == 'jake-miller'

    def test_packet_untouched_without_the_config_key(self):
        packet = pp.apply_named_cast_settings({'camera_dna': 'x'}, {})
        assert 'agent_lock_mode' not in packet


class TestFindingsAreNotStructuralHardFailures:
    """不变量：外套写错色不该抢走"这拍没有可拍画面"的定向回炉预算。"""

    def test_no_cast_lock_message_is_classified_structural(self, registry, jake):
        globals_ = core.registry_globals(registry, jake)
        fnd = core.Findings()
        # 一条把每道门都踩一遍的提示词。
        body = ("one worker in a khaki jacket and a white hardhat; the same worker returns, "
                "two workers finish up, a continuous construction time-lapse.")
        core.audit_prompt('VIDEO 11', body, jake, globals_, fnd)
        messages = fnd.messages()
        assert len(messages) >= 5, messages
        structural, rest = pp.split_structural_video_errors(messages)
        assert structural == [], structural
        assert rest == messages
