"""MiniatureComposer 与 gemini-miniature-restoration-composer 技能包的单元测试。"""

import os
import pytest

import prompt_pipeline as pp
from prompt_pipeline.composers import get_composer, MiniatureComposer
from prompt_pipeline.composers.miniature import (
    check_miniature_macro_optics,
    check_miniature_actor_violations,
    check_miniature_cutaway_framing,
    MINIATURE_PACING_PHRASE,
    MINIATURE_PACING_MARKER,
    MINIATURE_OPTICS_PHRASE,
)
from prompt_pipeline.composers.omni import OmniComposer, omni_video_violations, video_word_targets

# 片长必须在测试里钉死。begin_run({}) 会让 clip_duration() 回落到开发机的
# server_config.json——那份配置用户在设置页随时改得动（实测：改成 Omni Flash / 10 秒
# 之后，三条按面板固定 8 秒写的用例当场变红）。test_omni_timeline.py 早就为同一件事
# 留过一条注释，这里补上同样的钉子。miniature 视频模型名不含 omni，片长恒为面板固定值。
MINIATURE_CONFIG = {'videoModel': 'miniature'}
import server_common


class TestMiniatureComposerRegistration:
    def test_composer_factory_returns_miniature_composer(self):
        composer = get_composer('miniature')
        assert isinstance(composer, MiniatureComposer)
        assert composer.profile == 'miniature'

    def test_skill_profile_is_registered(self):
        assert 'miniature' in server_common.SKILL_PROFILES
        spec = server_common.SKILL_PROFILES['miniature']
        assert spec['package'] == 'gemini-miniature-restoration-composer'
        assert spec['env'] == 'SKILL_DIR_MINIATURE'

    def test_contract_files_exist(self):
        missing = server_common.missing_skill_contract_files('miniature')
        assert missing == []

    def test_video_model_mapping(self):
        assert server_common.profile_for_video_model('miniature') == 'miniature'
        assert server_common.profile_for_video_model('Miniature Diorama Pro') == 'miniature'


class TestMiniatureCheckers:
    def test_actor_violations(self):
        # 违规用例 1：Base 范本原话
        errs1 = check_miniature_actor_violations("one lone worker in a solid pale shirt, dark pants, and dark cap")
        assert len(errs1) > 0
        assert "Banned full-scale worker" in errs1[0]

        # 违规用例 2：NLVTR 词形数字
        errs2 = check_miniature_actor_violations("one lone worker roughly one point seven eight meters tall")
        assert len(errs2) > 0

        # 违规用例 3：全尺寸施工工人与安全帽
        errs3 = check_miniature_actor_violations("A construction worker wearing a safety vest and hard hat")
        assert len(errs3) > 0

        # 合规用例：包含超大真人手与微型工具
        clean_errs = check_miniature_actor_violations("An oversized real human hand enters wielding fine-tip tweezers")
        assert clean_errs == []

    def test_macro_optics(self):
        # 违规用例 1：空格变体 24 mm wide angle
        errs1 = check_miniature_macro_optics("A locked 24 mm wide angle tripod shot of the model")
        assert len(errs1) > 0
        assert "24mm wide-angle" in errs1[0]

        # 违规用例 2：词形数字 twenty-four millimeter
        errs2 = check_miniature_macro_optics("A static twenty-four millimeter wide-angle tripod shot")
        assert len(errs2) > 0

        # 违规用例 3：未声明微距光学与景深
        errs3 = check_miniature_macro_optics("A plain eye-level photograph of the wooden structure")
        assert len(errs3) > 0
        assert "macro lens" in errs3[0]

        # 合规用例：完整微距景深声明
        clean_errs = check_miniature_macro_optics("A static macro diorama eye-level shot with shallow depth of field and soft background blur")
        assert clean_errs == []

    def test_cutaway_framing(self):
        # 违规用例 1：TBCP 标准过门措辞
        errs1 = check_miniature_cutaway_framing("The camera pushes through the doorway and settles inside the room")
        assert len(errs1) > 0
        assert "Walk-in threshold movement" in errs1[0]

        # 违规用例 2：跨过门槛走入
        errs2 = check_miniature_cutaway_framing("The camera walks across the threshold into the interior")
        assert len(errs2) > 0

        # 违规用例 3：步入室内
        errs3 = check_miniature_cutaway_framing("The view steps inside the room to frame the fireplace")
        assert len(errs3) > 0

        # 合规用例：敞开式娃娃屋剖面
        clean_errs = check_miniature_cutaway_framing("An open-front cutaway dollhouse view filmed from outside the model")
        assert clean_errs == []


class TestMiniatureProactiveFixes:
    def test_proactive_fixes_clean_worker_and_negative_terms(self):
        composer = MiniatureComposer()
        raw_video = (
            "At t=0s, one lone worker in a solid pale shirt is already positioned at the active work face. "
            "continuous construction time-lapse, not real-time footage. "
            "The 24mm wide-angle lens captures the worker placing bricks. "
            "Sound effects include tool contact, material movement, and footsteps of this beat."
        )
        raw_image = (
            "A static 24mm wide-angle tripod shot of the model. "
            "The horizon line remains perfectly level at exactly half the frame height. "
            "never reading as a distant miniature in a landscape panorama. "
            "Miniature furniture in dollhouse scale sits inside the cutaway room."
        )

        fixed_video, fixed_image = composer.apply_proactive_fixes(
            1, raw_video, raw_image, {'camera_dna': '24mm wide-angle tripod shot'}, 'direct', False, False
        )

        # 视频验证：
        # 1. 1.78m 工人与 lone worker 被清除/替换为超大真人手
        assert 'lone worker' not in fixed_video.lower()
        assert 'oversized' in fixed_video.lower() or 'giant' in fixed_video.lower()
        # 2. 24mm 广角被替换为微距
        assert '24mm' not in fixed_video
        assert 'macro diorama eye-level' in fixed_video
        # 3. 脚步声被替换为微缩工艺音效
        assert 'footsteps' not in fixed_video
        assert 'tool clicks' in fixed_video or 'craft contact' in fixed_video
        # 4. 节奏句替换为微缩专属
        assert MINIATURE_PACING_PHRASE in fixed_video

        # 图像验证：
        # 1. 24mm 广角被替换为微距
        assert '24mm' not in fixed_image
        assert 'macro diorama eye-level' in fixed_image
        # 2. 反微缩语句被剥离
        assert 'never reading as a distant miniature' not in fixed_image
        # 3. 正向微缩词汇完好保留（不破坏语法，不留双空格破损）
        assert 'Miniature furniture in dollhouse scale' in fixed_image
        # 4. 地平线句被替换为浅景深焦外虚化
        assert 'The horizon line remains' not in fixed_image
        assert 'creamy blur with shallow depth of field' in fixed_image


class TestMiniatureStructuralErrors:
    def test_ghost_work_is_not_flagged_as_structural(self):
        composer = MiniatureComposer()
        errs = [
            "VIDEO shows construction work with no visible agent (ghost work) — add the lone worker",
            "Stylistic repetition: beat mirrors phrasing",
        ]
        structural, rest = composer.split_structural_video_errors(errs)
        assert not any('ghost work' in e for e in structural)
        assert any('ghost work' in e for e in rest)


class TestMiniatureSystemPrompts:
    def test_batch_system_prompt_carries_miniature_override(self):
        composer = MiniatureComposer()
        prompt = composer.batch_system_prompt({}, {'camera_dna': ''}, '', '')
        assert "MINIATURE & GIANT HAND DIORAMA OVERRIDE" in prompt
        assert "gemini-miniature-restoration-composer" in prompt
        assert "PRIORITY & WORLDVIEW OVERRIDE" in prompt

    def test_single_beat_system_prompt_declares_miniature_identity(self):
        composer = MiniatureComposer()
        contract = {
            'beat': {'operation': 'build', 'description': 'tiny roof'},
            'img_i_lighting': 'ambient',
            'img_ip1_lighting': 'ambient',
            'family_contract': '',
            'templates_cropped': '',
            'anchor_rule': '',
            'stage_scope': '',
            'is_first_interior_reveal': False,
            'family': 'exterior',
        }
        prompt = composer.single_beat_system_prompt(
            {}, 1, contract, {}, {1: 'IM1'}, {}, '', ''
        )
        assert "gemini-miniature-restoration-composer" in prompt
        assert "gemini-veo-restoration-composer" not in prompt
        assert "MINIATURE OVERRIDE FOR THIS BEAT" in prompt



class TestMiniatureMultiShotLadder:
    """2026-08-23：本 profile 从「锁死机位的一镜到底」改成「同一台锁死机位上的多镜头组接」。

    钉住的是换皮而不是换机制：镜头梯、切点表、一镜到底禁令全部复用 omni 的实现，
    镜头名与逐镜职责换成微距口径，omni 的过门梯/兑现梯一条都不许漏进来。
    """

    def test_composer_inherits_the_multishot_machinery(self):
        assert isinstance(MiniatureComposer(), OmniComposer)

    def test_construction_ladder_is_four_shots_at_the_panel_duration(self):
        composer = MiniatureComposer()
        composer.begin_run(dict(MINIATURE_CONFIG), {})
        # 非 Omni 视频模型的片长由面板固定，miniature 走的就是这一档
        assert composer.clip_duration() == server_common.FIXED_VIDEO_DURATION
        ladder = composer.ladder_for_kind(composer.clip_duration(), 'construction')
        assert [rung.key for rung in ladder] == ['main', 'close', 'xclose', 'return']
        assert [rung.phrase for rung in ladder] == [
            'a macro working shot', 'a close-up insert',
            'an extreme close-up insert', 'a returning macro shot',
        ]

    def test_reveal_and_reward_reuse_the_same_shot_names(self):
        """omni 的 wide approach / threshold / interior wide 是走入式穿门的镜头名，
        pull-back / final wide 会换掉机位——两套在本包都是 P0 违规，必须都不出现。"""
        composer = MiniatureComposer()
        composer.begin_run(dict(MINIATURE_CONFIG), {})
        banned = {'approach', 'threshold', 'arrival', 'detail', 'pullback', 'final_wide'}
        for kind in ('traversal', 'reward'):
            ladder = composer.ladder_for_kind(8, kind)
            assert [rung.key for rung in ladder] == ['main', 'close', 'return']
            assert not banned & {rung.key for rung in ladder}
            # 镜头名与工序拍完全一致，差别只在逐镜职责
            assert [r.phrase for r in ladder] == [
                'a macro working shot', 'a close-up insert', 'a returning macro shot']
        assert (composer.ladder_for_kind(8, 'traversal')[0].role
                != composer.ladder_for_kind(8, 'reward')[0].role)

    def test_fix_chain_cleans_timeline_and_kills_one_take_wording(self):
        composer = MiniatureComposer()
        composer.begin_run(dict(MINIATURE_CONFIG), {})
        raw = (
            "The giant hand lays thumbnail-sized tiles along the eave with fine-tip tweezers. "
            "One unbroken take at a steady speed: no cut, no fade, no dissolve. "
            "continuous miniature craft time-lapse (not real-time), with oversized human hands "
            "entering to assemble components."
        )
        fixed, _img = composer.apply_proactive_fixes(
            3, raw, "A static macro diorama shot of the stump.", {'camera_dna': ''},
            'direct', False, False)
        low = fixed.lower()
        # 纯自然语言模式：不包含机器切点表
        assert 'cut this' not in low
        # 一镜到底措辞（含本包旧的 continuous 节奏句）一并作废
        assert 'unbroken take' not in low
        assert 'continuous miniature craft time-lapse' not in low
        # 新节奏声明只出现一次
        assert low.count(MINIATURE_PACING_MARKER) == 1

    def test_single_shot_body_is_a_structural_violation(self):
        """镜头梯缺失要能触发定向回炉，而不是只留一条痕。"""
        composer = MiniatureComposer()
        composer.begin_run(dict(MINIATURE_CONFIG), {})
        body = (
            "Use the provided first frame and last frame as exact composition anchors; every "
            "visible action must interpolate between those two frame images without inventing "
            "a third layout. A static macro diorama eye-level shot with shallow depth of field: "
            "an oversized human hand sets miniature blocks along the course with a trowel."
        )
        errs = composer.validate_beat_prompts(
            2, body, "A static macro diorama shot with shallow depth of field.", {},
            'direct', False, False)
        structural, _rest = composer.split_structural_video_errors(errs)
        assert any('multi-shot' in e for e in structural)

    def test_no_full_scale_worker_clause_is_injected(self):
        """omni 的 ensure_ladder_out_and_in 会补一句 "the same lone worker ... is already
        positioned"——那在本包是 P0 违规，必须被巨人手口径顶掉。"""
        composer = MiniatureComposer()
        composer.begin_run(dict(MINIATURE_CONFIG), {})
        raw = ("A macro working shot on the stump; the crew keeps setting blocks along the course "
               "with a trowel while the pile beside the stump empties.")
        fixed, _img = composer.apply_proactive_fixes(
            4, raw, "A static macro diorama shot.", {'camera_dna': ''}, 'direct', False, False)
        assert 'lone worker' not in fixed.lower()
        assert check_miniature_actor_violations(fixed) == []
        assert 'hand' in fixed.lower()

    def test_capture_style_is_locked_macro_not_ugc_phone(self):
        """omni 默认档是 UGC 手机随手拍（handheld drift + 广角边缘畸变），两条都与本包的
        P0 微距契约对撞，必须整条换成锁死的微距三脚架，并把那两条明确写成禁令。"""
        composer = MiniatureComposer()
        composer.begin_run(dict(MINIATURE_CONFIG), {})
        rule = composer.capture_style_rule()
        low = rule.lower()
        assert 'locked macro diorama photography on a tripod' in low
        assert MINIATURE_OPTICS_PHRASE in rule
        # UGC 档的正向措辞一个都不许留
        for banned in ('casual ugc phone footage', 'smartphone rear camera',
                       'autofocus breathing', 'compression artifacts'):
            assert banned not in low
        # 而且要把它们写成禁令
        never = low[low.index('never write'):]
        for required in ('handheld drift', 'wide-angle edge distortion', 'chest-height'):
            assert required in never

    def test_fallback_clause_keeps_the_macro_setup(self):
        composer = MiniatureComposer()
        composer.begin_run(dict(MINIATURE_CONFIG), {})
        clause = composer.fallback_ladder_clause(composer.ladder_for_kind(8, 'construction'))
        assert 'macro working shot' in clause
        assert 'returning macro shot' in clause
        assert MINIATURE_OPTICS_PHRASE in clause
        assert 'handheld' not in clause.lower()

    def test_optics_phrase_carries_no_numeric_range(self):
        """NLVTR 门禁禁止 `50-85mm` 这类数值区间，而这句是被确定性注入的。"""
        assert pp.check_nlvtr_violations(MINIATURE_OPTICS_PHRASE) == []


class TestMiniatureMultiShotSystemPrompts:
    def test_batch_prompt_carries_worldview_then_shot_grammar(self):
        composer = MiniatureComposer()
        composer.begin_run(dict(MINIATURE_CONFIG), {})
        prompt = composer.batch_system_prompt({}, {'camera_dna': ''}, '', '')
        assert 'MINIATURE & GIANT HAND DIORAMA OVERRIDE' in prompt
        assert 'MINIATURE MULTI-SHOT VIDEO OVERRIDE' in prompt
        # 世界观在前、镜头语法在后：VIDEO 的最后一道口径必须是镜头语法段
        assert (prompt.index('MINIATURE & GIANT HAND DIORAMA OVERRIDE')
                < prompt.index('MINIATURE MULTI-SHOT VIDEO OVERRIDE'))
        assert 'Cut this eight-second clip on these marks' in prompt
        assert 'ONE-TAKE BAN' in prompt
        # omni 的世界观措辞一个都不许漏进来
        assert 'Gemini Omni' not in prompt
        assert 'UGC phone footage' not in prompt

    def test_batch_prompt_loads_the_multishot_reference(self):
        composer = MiniatureComposer()
        composer.begin_run(dict(MINIATURE_CONFIG), {})
        prompt = composer.batch_system_prompt({}, {'camera_dna': ''}, '', '')
        assert 'miniature-multishot-language.md' in prompt
        # 契约正文真的被读进来了，而不只是文件名
        assert '一条主工作镜' in prompt

    def test_single_beat_prompt_keeps_identity_and_shot_ladder(self):
        composer = MiniatureComposer()
        composer.begin_run(dict(MINIATURE_CONFIG), {})
        contract = {
            'beat': {'operation': 'build', 'description': 'tiny roof'},
            'img_i_lighting': 'ambient',
            'img_ip1_lighting': 'ambient',
            'family_contract': '',
            'templates_cropped': '',
            'anchor_rule': '',
            'stage_scope': '',
            'is_first_interior_reveal': False,
            'family': 'exterior',
        }
        prompt = composer.single_beat_system_prompt({}, 1, contract, {}, {1: 'IM1'}, {}, '', '')
        assert 'gemini-miniature-restoration-composer' in prompt
        assert 'gemini-veo-restoration-composer' not in prompt
        assert 'MINIATURE MULTI-SHOT VIDEO OVERRIDE' in prompt
        assert 'MINIATURE OVERRIDE FOR THIS BEAT' in prompt
        # 逐拍通路独有的实景公制段被换成微缩口径
        assert 'HUMAN-SPATIAL METRIC CONSERVATION' not in prompt
        assert 'MINIATURE SCALE CONSERVATION' in prompt

    def test_rework_prompt_is_miniature_flavoured(self):
        composer = MiniatureComposer()
        composer.begin_run(dict(MINIATURE_CONFIG), {})
        ladder = composer.ladder_for_kind(8, 'construction')
        system = composer.multishot_rework_system(ladder, 8)
        assert 'OVERSIZED REAL HUMAN HAND' in system
        assert 'the same locked macro setup as the opening macro working shot' in system
        assert 'Gemini Omni' not in system


class TestShippedExemplarsPassTheirOwnGates:
    """技能包里每一条 VIDEO 范例都必须通过运行时门禁。

    2026-08-23 实测：改写成多镜头之后，范例把最后一镜写成了"The last cut returns to the
    same locked macro setup ..."——读起来完全正确，却一次都没有出现 `returning macro shot`
    这个镜头名，于是七条范例全部被镜头梯门禁判为缺镜头。写手照着抄的就是这些范例，
    范例违约等于每一拍首稿必炸、每一拍都要烧一轮定向回炉。散文与门禁的一致性只能靠
    这条测试守。
    """

    SECTIONS = (
        ('### Ordinary Construction VIDEO', 'construction'),
        ('### Threshold Bridge', 'traversal'),
        ('### Final Reward VIDEO N', 'reward'),
        ('## Fill-In Checklist', None),
    )

    def _exemplars(self):
        path = os.path.join(
            server_common.skill_dir('miniature'), 'references', 'prompt-templates.md')
        with open(path, encoding='utf-8') as f:
            text = f.read()
        for index, (heading, kind) in enumerate(self.SECTIONS[:-1]):
            start = text.index(heading)
            end = text.index(self.SECTIONS[index + 1][0])
            for part in text[start:end].split('#### ')[1:]:
                lines = part.split('\n')
                body = next(l for l in lines[1:] if l.strip() and not l.startswith('---'))
                yield lines[0].strip(), kind, body.strip()

    def test_every_video_exemplar_passes_every_gate(self):
        composer = MiniatureComposer()
        composer.begin_run(dict(MINIATURE_CONFIG), {})
        duration = composer.clip_duration()
        failures = []
        seen = 0
        for label, kind, body in self._exemplars():
            seen += 1
            ladder = composer.ladder_for_kind(duration, kind)
            errs = (omni_video_violations(body, ladder=ladder, duration=duration)
                    + pp.check_nlvtr_violations(body)
                    + check_miniature_macro_optics(body)
                    + check_miniature_actor_violations(body)
                    + check_miniature_cutaway_framing(body))
            if errs:
                failures.append(f'{label}: ' + ' | '.join(e[:120] for e in errs))
        assert seen >= 7, f'只解析到 {seen} 条范例，模板的 #### 结构可能被改坏了'
        assert not failures, '技能包范例违反自己的门禁：\n  ' + '\n  '.join(failures)

    def test_every_video_exemplar_fits_the_word_ceiling(self):
        composer = MiniatureComposer()
        composer.begin_run(dict(MINIATURE_CONFIG), {})
        duration = composer.clip_duration()
        over = []
        for label, kind, body in self._exemplars():
            ceiling = video_word_targets(len(composer.ladder_for_kind(duration, kind)))[1]
            words = len(body.split())
            if words > ceiling:
                over.append(f'{label}: {words} 词 > 硬顶 {ceiling}')
        assert not over, '范例超出本镜头梯的字数硬顶（抄它就会被裁掉中间的镜头）：\n  ' + '\n  '.join(over)
