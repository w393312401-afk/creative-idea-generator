"""miniature profile (gemini-miniature-restoration-composer) 的 Phase 2 composer.

微缩模型与巨人手建造专属 Composer:
- 施工主体: Oversized Human Hands / Macro Fingers 从画幅边缘伸入，使用微型工具进行精密装配。
- 居住与观察: Two cast-resin miniature figurines (1:24 dollhouse scale) 处于微缩场景中。
- 光学摄影: 50mm-85mm macro lens feel, shallow depth of field, creamy background bokeh, model eye-level height.
- 室内机位: Open-front Cutaway Dollhouse View (娃娃屋敞开立面/剖面), 彻底废除走入式进门 (Walk-in Threshold).
- 负向过滤: 白名单放行 miniature / dollhouse，针对性压制 full-size human room 与全尺寸建筑广角.

2026-08-23 多镜头改造。此前本 profile 继承 BaseComposer，交付的是一条锁死机位的
一镜到底片段；SKILL.md 的 description 却一直写着「微距多镜头 VIDEO 提示词」——散文与
实现分叉，多镜头那半句全包只有那一处，模板、门禁、composer 三边都是单镜。现在改成继承
OmniComposer，镜头梯机制（切点表、一镜到底禁令、镜头名审计、定向回炉）整套复用，
**只换皮不换机制**：

  · 镜头名换成微距口径：主镜 macro working shot / 切回 returning macro shot，插入镜沿用
    close-up / extreme close-up（它们本来就是微距词汇）。
  · 三套梯全部收敛到「主镜 + 插入 + 切回同一机位」这一种形状。omni 的过门梯
    （wide approach → threshold → interior wide）与兑现梯（detail → pull-back →
    final wide）在这里一条都不能用：前者是走入式穿门的镜头名，正是本包 P0 禁止的东西；
    后者的 pull-back 会在一条靠「机位不动」立住锚点连续性的片子里换掉机位。剖面揭示拍与
    最终兑现拍因此走同名的三镜梯，只是逐镜职责不同。
  · UGC 手机质感 → 锁死微距三脚架质感。omni 的默认拍摄质感里有 handheld drift 与
    wide-angle edge distortion，两条都与本包的微距契约直接对撞。
  · 节奏声明与镜内连续性声明换成微缩口径（见 MINIATURE_PACING_PHRASE / *_INSHOT_PHRASE），
    marker 仍是 phrase 的真子串，保证 ensure_pacing 幂等。

镜头语法违规仍带 OMNI_VIDEO_CONTRACT 前缀：那个前缀是「违规 → 结构性分类 → 定向回炉」
这条链的接头，按 profile 各起一个名字就要在三处各留一份分支，换来的只是日志里好看一点。
"""

import re
import sys

import prompt_pipeline as pp

from .omni import (
    OmniComposer,
    Rung,
    construction_shot_count,
    ladder_roles,
    timeline_sentence,
    video_word_targets,
    OMNI_INSHOT_MARKER,
)

# SKILL.md 声明的每次必读 references。在这份清单存在之前，这几份文档全仓库没有任何
# 读取代码——它们只出现在 server_common._MINIATURE_CONTRACT_FILES 的存在性清单里，
# 于是「契约完整」与「契约生效」是两件不相干的事：missing_skill_contract_files() 报
# 空，而 system prompt 里这几份文档一行都没有，整个微缩世界观只活在本文件硬编码的
# override 段里。
#
# 与 omni 的区别：omni 把过门协议做成条件加载（它的 references 合计约 100KB，按需
# 省的是真金白银）。这里合计约 20KB，条件加载省不下什么，却会踩中 base 那条
# TBCP 注释记录过的坑——过门拍常落在第 5/6/12 拍，按窗口判定会让某些窗口的
# system prompt 里恒无该协议正文，是个不报错的质量洞。所以一律全量加载。
MINIATURE_MULTISHOT_REFERENCE = 'miniature-multishot-language.md'
MINIATURE_ALWAYS_LOAD_REFERENCES = (
    'miniature-scene-skeleton.md',
    MINIATURE_MULTISHOT_REFERENCE,
    'miniature-macro-language.md',
    'miniature-materials-and-tools.md',
    'miniature-cutaway-architecture.md',
    'miniature-output-templates.md',
)

# base 的共享 system prompt 首句写死了 `gemini-veo-restoration-composer`。合成 miniature
# 时那句自称必须改掉，否则模型在同一份提示词里先被告知自己是 Veo 作曲家、再被告知
# 一切以微缩覆盖段为准。批量与逐拍两条通路都要改——批量才是主通路（composeBatchSize
# 默认 5，逐拍只在整窗请求失败时兜底），只改逐拍等于没改。
#
# 必须钉住**整句角色声明**，不能只匹配那个包名：scup_ref / tbcp_ref 的正文是被内插
# 进同一份 system prompt 的，而本包的 references 里正当地提到过 base 包的名字
# （"本文件取代 base 包 `gemini-veo-restoration-composer` 的同名文件"）。裸替换包名
# 会把那句话改成"取代 base 包 `gemini-miniature-restoration-composer`"——自指的胡话。
_VEO_ROLE_SENTENCE_PATTERN = re.compile(
    r'(You are a professional prompt composer operating under the )'
    r'`gemini-veo-restoration-composer`'
)

# base 的**逐拍**通路（composers/base.py 的 single_beat_system_prompt）里硬编码了一段
# 四条的 HUMAN-SPATIAL METRIC CONSERVATION 规则：公制房间尺寸、紧凑家具、24mm 广角
# 1.3m 胸高机位、以及"VIDEO 必须声明工人身高 1.78m 占画高约 35%"。它不来自任何
# reference 文件，所以关掉 reference 回退动不到它；批量通路没有这一段（实测批量里
# 这些字面量为零，逐拍里为一）。逐拍是整窗批量请求失败时的兜底通路，会真的跑到。
#
# 整块换成微缩口径，而不是逐条删——这四条要解决的问题（空间别膨胀、尺度要可判读）
# 在微缩题材下同样存在，只是判据完全不同。
_VEO_METRIC_BLOCK_PATTERN = re.compile(
    r'- HUMAN-SPATIAL METRIC CONSERVATION & ERGONOMIC SCALE \(P0\):.*?'
    r'(?=- Output the prompts in the following format:)',
    re.DOTALL
)

_MINIATURE_METRIC_BLOCK = """- MINIATURE SCALE CONSERVATION & MACRO ERGONOMICS (P0):
  1. Tabletop Envelope: state the model's size with centimetres or an everyday comparison (about the width of a spread hand, roughly two palms tall). NEVER declare architectural metric dimensions such as ceiling clearance or room diameter — they make the model render as a real building.
  2. Craft Prop Scale: every fitting inside the shell is a miniature made from craft stock (basswood, card, resin, brass rod). Forbid full-size furniture vocabulary; name the miniature version instead.
  3. Camera Normalization: default to a macro lens feel at model eye-level (just above the tabletop) or a thirty-to-forty-five degree oblique, with shallow depth of field and creamy background bokeh. NEVER a wide-angle architectural lens at human chest height. The close-up inserts go closer on the same model with the same macro optics; they never become another place.
  4. Video Actor Scale Figure: the opening macro working shot of VIDEO {i} must declare the giant hand's scale against the model (e.g. 'one oversized human hand reaching in from the upper frame margin, its palm spanning roughly the height of one storey of the model'), plus the resident figurines' scale (roughly a thumb tall). NEVER declare a human worker's body height.
"""


# ── 镜头梯 ──────────────────────────────────────────────────────────────────
# 结构与 omni 完全一致（主镜 + 一到两个特写插入 + 切回同一机位），换的只有镜头名与
# 逐镜职责。variants 一律写成 omni._normalized() 之后的形态（小写、无连字符、单空格）。
_M_MAIN = Rung(
    'main', ('macro working shot',), '主镜 macro working shot',
    'a macro working shot', 1.5,
    '贯穿本段的主工作镜，画面开在起始 IMAGE 上、巨人手已从画幅某条边缘伸入作业面，'
    '0 秒立即发生第一次有效工具接触；第一次动作完整可见后转入重复循环，本拍改动在这一镜内'
    '推进到约四分之三，全程用 -ing / partially / growing 这类进行态措辞，不出现完成态描述')
_M_CLOSE = Rung(
    'close', ('close up',), '特写插入 close-up insert', 'a close-up insert', 1.0,
    '从主镜切进去的微距特写：同一台相机再推近到工具接触点，只交代材料物理（胶液铺开、'
    '砂浆挤出、木屑翻卷、瓦片压合），不产生新的推进量，切回主镜时完成度与切走那一刻一致')
_M_XCLOSE = Rung(
    'xclose', ('extreme close up',), '第二处特写插入 extreme close-up insert',
    'an extreme close-up insert', 0.9,
    '第二个插入镜，至少两处本拍特有的持久手工艺痕迹（胶痕、砂浆边、锯末、切口毛刺、笔触）'
    '与微观质感，同样不产生推进量')
_M_RETURN = Rung(
    'return', ('returning macro shot',), '切回主镜 returning macro shot',
    'a returning macro shot', 1.3,
    '切回**与主镜完全相同的机位、构图与焦段**（正文要写明 the same locked macro setup as '
    'the opening macro working shot），剩余重复动作在这个剪辑点上做 same-way 压缩，'
    '巨人手完成最后一次操作后退出画幅，画面落到这一拍的结果 IMAGE（净帧），'
    '只有这一镜可以用完成态措辞')

# 剖面揭示拍：镜头名与施工梯同名（同一台机位，同一套微距光学），职责整套不同——
# 本拍零施工，交付的只是「外立面板被揭走、内部毛坯完整可见」这一件事。
_M_REVEAL_MAIN = Rung(
    'main', ('macro working shot',), '主镜 macro working shot',
    'a macro working shot', 1.5,
    '揭示主镜：机位一动不动，始终在模型外侧。巨人手从画幅边缘伸入扣住外立面板（或整片屋顶），'
    '匀速把它抬离卡槽并搬走，**本镜零施工**：不清理、不安装、不上色，不写任何 time-lapse 措辞')
_M_REVEAL_CLOSE = Rung(
    'close', ('close up',), '特写插入 close-up insert', 'a close-up insert', 1.0,
    '插入镜：面板脱离卡槽的接缝特写，以及面板最终被靠放/搬离的**去处**（靠在树桩侧面、'
    '出画到左上角），指尖与卡槽的接触细节可见，内部仍然一点没被动过')
_M_REVEAL_RETURN = Rung(
    'return', ('returning macro shot',), '切回主镜 returning macro shot',
    'a returning macro shot', 1.3,
    '切回同一机位：整个内部在敞开的剖面里一次看全，毛坯状态原封不动，外部成果全部继承'
    '（屋顶内侧完好、无漏光），画面里没有手、工具与材料，落到这一拍的结果 IMAGE')

# 最终兑现拍：仍是同一机位，交付「住进去了」。
_M_REWARD_MAIN = Rung(
    'main', ('macro working shot',), '主镜 macro working shot',
    'a macro working shot', 1.5,
    '收尾主镜：同一台锁死的微距机位上呈现完工全貌，巨人手做最后一件收尾动作'
    '（把立面板装回卡槽 / 拨亮微型 LED / 把人偶放上门廊）')
_M_REWARD_CLOSE = Rung(
    'close', ('close up',), '特写插入 close-up insert', 'a close-up insert', 1.0,
    '插入镜：签名细部——灯珠在窗格后亮起、门牌、人偶落座的那一下，微距浅景深，'
    '不新增任何工序')
_M_REWARD_RETURN = Rung(
    'return', ('returning macro shot',), '切回主镜 returning macro shot',
    'a returning macro shot', 1.3,
    '切回同一机位收尾：完工全貌 + 暖光 + 人偶从旁观转为入住 + 环境布景，双手已退出画幅，'
    '画面落到最终 IMAGE，这一镜本身就是收尾欣赏')

_MINIATURE_CONSTRUCTION_LADDERS = {
    3: (_M_MAIN, _M_CLOSE, _M_RETURN),
    4: (_M_MAIN, _M_CLOSE, _M_XCLOSE, _M_RETURN),
}
_MINIATURE_REVEAL_LADDER = (_M_REVEAL_MAIN, _M_REVEAL_CLOSE, _M_REVEAL_RETURN)
_MINIATURE_REWARD_LADDER = (_M_REWARD_MAIN, _M_REWARD_CLOSE, _M_REWARD_RETURN)

# 兜底稿里每一级镜头的一句话职责（占位稿仍计入 fallback_count 门禁，但至少不违反镜头语法）。
_MINIATURE_FALLBACK_FRAGMENT = {
    'main': ('a macro working shot matching the first frame, with an oversized human hand '
             'already reaching in from the frame margin and carrying the whole visible advance '
             'of this beat'),
    'close': 'a close-up insert on the micro-tool contact point',
    'xclose': 'an extreme close-up insert on the craft traces left behind',
    'return': ('a returning macro shot from the same locked setup, matching the last frame '
               'after the hand withdraws'),
}
_COUNT_WORDS = {3: 'three', 4: 'four'}


# ── 契约文案 ────────────────────────────────────────────────────────────────
# 多镜头包里不能用 continuous 描述整条片段的拍法（那个词会被读成"拍一条一镜到底"）。
# 揭示拍与最终兑现拍免除这一句。marker 必须是 phrase 的真子串，见 OmniComposer.ensure_pacing。
MINIATURE_PACING_PHRASE = (
    "edited miniature craft time-lapse assembled from multiple macro camera setups, "
    "not real-time footage, with oversized human hands entering and withdrawing between passes."
)
MINIATURE_PACING_MARKER = 'edited miniature craft time-lapse assembled from multiple macro camera setups'

# omni 的镜内连续性声明里写着 handheld drift——本包的机位锁死在三脚架上，那句直接对撞。
# 尾句（OMNI_INSHOT_MARKER）逐字保留：它既是判定已注入的 marker，也是"压缩只发生在切点上"
# 这条规则本身。
MINIATURE_INSHOT_PHRASE = (
    "Inside every shot the frame keeps living from its first to its last moment — the giant "
    "hand's own motion, drifting dust, and the settling of craft debris never freeze, while "
    "the camera itself stays locked — and this beat's change advances only during the working "
    "shots. The only compressions in the clip fall exactly on the listed cut marks; no shot "
    "contains a hold, a stall, or a deferred step that is then delivered all at once."
)

# 锁死机位的微距光学声明。数字一律词形——NLVTR 门禁禁止 `50-85mm` 这类数值区间，而
# 这句是会被确定性注入进兜底稿与 system prompt 的，写错一次就是每条片子都错。
MINIATURE_OPTICS_PHRASE = (
    "a locked macro diorama setup at model eye-level, fifty to eighty-five millimetre macro "
    "lens feel with shallow depth of field and creamy background bokeh"
)

MINIATURE_VIDEO_ERROR_PREFIX = 'MINIATURE VIDEO CONTRACT: '
MINIATURE_IMAGE_ERROR_PREFIX = 'MINIATURE IMAGE CONTRACT: '

# 违规词匹配：严禁在微缩建造中出现 1.78m 工人、全尺寸施工队或反光背心安全帽
_FULL_SCALE_WORKER_PATTERN = re.compile(
    r'\b(?:1\.78\s*m|1\.8\s*m|one\s+point\s+seven\s+eight\s+meters?|'
    r'(?:a\s+|one\s+)?lone\s+(?:male\s+)?workers?|construction\s+workers?|'
    r'workm[ae]n(?:\s+in\s+safety\s+vest)?|hard\s*hats?|safety\s+vests?|'
    r'workers?\s+in\s+a\s+[^.;]*shirt)\b',
    re.IGNORECASE
)

# 违规词匹配：严禁走入式穿门或全尺寸建筑过门
_WALK_IN_THRESHOLD_PATTERN = re.compile(
    r'\b(?:walks?\s+(?:across|through)\s+the\s+threshold|'
    r'steps?\s+inside\s+(?:the\s+)?(?:room|interior)|'
    r'pushes?\s+through\s+(?:the\s+)?(?:doorway|door|threshold)\s+(?:and\s+settles?\s+inside|into)|'
    r'camera\s+(?:walks|enters|steps)\s+(?:into|across|through))\b',
    re.IGNORECASE
)

# 违规词匹配：严禁使用全尺寸建筑 24mm 广角镜头
_FULL_SCALE_OPTICS_PATTERN = re.compile(
    r'\b(?:24\s*mm|twenty[- ]four\s+millimeter)\s+wide[- ]?angle\b',
    re.IGNORECASE
)

# 微距与景深正向标记词
_MACRO_OPTICS_POSITIVE_KEYWORDS = (
    'macro', 'shallow depth of field', 'shallow dof', 'bokeh',
    'model eye-level', 'diorama', 'tabletop', 'cutaway', 'soft background blur'
)

# 旧的一镜到底节奏声明。它们在多镜头包里必须被换掉而不是留着：`continuous ... time-lapse`
# 会被读成"拍一条不间断的长镜头"，与切点表直接矛盾。三种来源各写一条——base 注入的、
# 本包改造前自己注入的、以及模型照旧稿抄出来的。
_LEGACY_PACING_PATTERNS = (
    re.compile(r'(?i)\bcontinuous\s+construction\s+time-lapse,\s*not\s+real-time\s+footage\.?'),
    re.compile(r'(?i)\bcontinuous\s+miniature\s+craft\s+time-lapse\s*\(not\s+real-time\)'
               r'(?:,\s*with\s+oversized\s+human\s+hands\s+entering\s+to\s+assemble\s+components)?\.?'),
    re.compile(r'(?i)\bcontinuous\s+miniature\s+(?:craft|construction)\s+time-lapse,\s*'
               r'not\s+real-time\s+footage\.?'),
)


def check_miniature_macro_optics(prompt_text):
    """检查提示词是否包含微距摄影或微缩沙盘相关光学声明。"""
    text = str(prompt_text or '')
    if not text:
        return []
    errors = []
    # 否定检查：检查是否误写了 24mm 超广角全尺寸建筑镜头（含词形数字与空格变体）
    if _FULL_SCALE_OPTICS_PATTERN.search(text):
        errors.append(f"{MINIATURE_VIDEO_ERROR_PREFIX}Found full-scale '24mm wide-angle' instead of macro lens.")
    # 正向检查：提示词必须声明微距摄影/浅景深/模型眼平/背景虚化
    low = text.lower()
    if not any(kw in low for kw in _MACRO_OPTICS_POSITIVE_KEYWORDS):
        errors.append(
            f"{MINIATURE_VIDEO_ERROR_PREFIX}Prompt must explicitly declare macro lens / "
            "shallow depth of field / model eye-level optics."
        )
    return errors


def check_miniature_actor_violations(prompt_text):
    """检查提示词是否违规包含了 1.78m 真人工人或真人尺寸施工队。"""
    text = str(prompt_text or '')
    if not text:
        return []
    errors = []
    match = _FULL_SCALE_WORKER_PATTERN.search(text)
    if match:
        errors.append(
            f"{MINIATURE_VIDEO_ERROR_PREFIX}Banned full-scale worker '{match.group(0)}' found. "
            "Miniature builds must use oversized human hands."
        )
    return errors


def check_miniature_cutaway_framing(prompt_text):
    """检查室内提示词是否违规使用了走入式进门，而非娃娃屋剖面（支持 VIDEO 与 IMAGE）。"""
    text = str(prompt_text or '')
    if not text:
        return []
    errors = []
    match = _WALK_IN_THRESHOLD_PATTERN.search(text)
    if match:
        errors.append(
            f"{MINIATURE_IMAGE_ERROR_PREFIX}Walk-in threshold movement '{match.group(0)}' is forbidden. "
            "Use cutaway dollhouse framing."
        )
    return errors


class MiniatureComposer(OmniComposer):
    """Phase 2 Composer for Miniature & Giant Hand Diorama builds（多镜头组接）。"""

    profile = 'miniature'

    pacing_phrase = MINIATURE_PACING_PHRASE
    pacing_marker = MINIATURE_PACING_MARKER
    inshot_phrase = MINIATURE_INSHOT_PHRASE
    inshot_marker = OMNI_INSHOT_MARKER
    multishot_reference = MINIATURE_MULTISHOT_REFERENCE

    def __init__(self):
        super().__init__()
        self._reference_cache = {}

    # ── references ──────────────────────────────────────────────────────────

    def required_references_block(self, include_threshold=False):
        """把每次必读的 references 拼成一段可直接进 system prompt 的文本。
        批量直出时这段只发一次（每拍共享），这正是批量通路存在的意义。

        include_threshold 在这里不分流：本包的剖面揭示协议只有一份
        （threshold-bridge-consistency-protocol.md，由 tbcp_ref 走 base 的通路内插），
        参数保留只为与 OmniComposer 的签名对齐。"""
        parts = []
        for name in MINIATURE_ALWAYS_LOAD_REFERENCES:
            body = self.reference(name)
            if not body:
                continue
            parts.append(f"---------- {name} ----------\n{body}")
        if not parts:
            # 契约整段读空时不静默降级：微缩规则仍由下面的 override 正文与门禁兜住，
            # 但这件事必须在日志里看得见（load_reference_file 只按文件名提示一次）。
            if sys.stdout:
                print("[WARN] miniature composer 一个必读契约都没读到，微缩世界观只能靠"
                      "内置的 override 正文兜底；检查 "
                      "skills/gemini-miniature-restoration-composer/references/")
            return ''
        return '\n\n'.join(parts)

    def rebrand_identity(self, text):
        """把 base 共享段里那句角色声明的自称改成微缩包自称。

        只动那一句——references 正文里对 base 包名的正当指代必须原样留着。"""
        return _VEO_ROLE_SENTENCE_PATTERN.sub(
            r'\1`gemini-miniature-restoration-composer`', text)

    # ── 镜头梯 ──────────────────────────────────────────────────────────────

    def ladder_for_kind(self, duration, kind='construction', observed_shots=None,
                        observed_scale=None, observed_scales=None):
        """(时长, 拍型) → 微缩镜头梯。三套梯共用同一组镜头名与同一台锁死机位，
        差别只在逐镜职责——omni 的过门梯/兑现梯在这里一条都不能用，理由见模块 docstring。

        observed_shots 的口径与 omni 完全一致（复刻线按原片切点排梯，见
        pp.apply_observed_shot_counts）；揭示拍与兑现拍恒为三镜。

        observed_scale / observed_scales 在这里**故意不生效**（收下只为与 omni 同签名，让共用的
        ladder_for_beat 一处取梯）。本包的镜头名全部锚在 P0 微距契约上，按原片景别改写
        主镜会写出 "wide working shot" 这类整包禁写的词，capture_style_rule 与镜头名审计
        当场对撞。微缩线要吃原片景别，得先有一套自己的景别词表。"""
        if kind == 'traversal':
            return _MINIATURE_REVEAL_LADDER
        if kind == 'reward':
            return _MINIATURE_REWARD_LADDER
        if observed_shots is not None:
            # 夹进合法区间 [3, 4] 之后，再按片长压一次上限：4/6 秒排不下第二个
            # 插入镜（construction_shot_count 的分界），硬排等于每镜不足一秒的闪帧。
            capped = min(max(observed_shots, min(pp._MULTISHOT_LEGAL_SHOT_COUNTS)),
                         construction_shot_count(duration))
            return _MINIATURE_CONSTRUCTION_LADDERS[capped]
        return _MINIATURE_CONSTRUCTION_LADDERS[construction_shot_count(duration)]

    # ── 风格分支 ────────────────────────────────────────────────────────────

    def capture_style_rule(self):
        """拍摄质感。omni 默认的 UGC 手机档在这里整条作废：handheld drift 与
        wide-angle edge distortion 与本包的 P0 微距契约直接对撞。"""
        return (
            "- CAPTURE STYLE: every shot is locked macro diorama photography on a tripod — "
            f"{MINIATURE_OPTICS_PHRASE}, real tabletop workshop light, and the real sandbox "
            "ground (leaf litter, grit, wood grain) held at the frame edge as scale evidence. "
            "The close-up inserts move closer on the SAME model with the SAME macro optics; "
            "they never become a different place, a different scale of world, or a full-size "
            "room. NEVER write handheld drift, unsteady framing, wide-angle edge distortion, "
            "chest-height architectural framing, or phone-camera artifacts."
        )

    def video_override_block(self, include_threshold=False, ladder=None, insert_subject=None):
        """追加在 base system prompt 之后的 MINIATURE MULTI-SHOT VIDEO OVERRIDE 段。

        只覆盖 VIDEO 的镜头语法：上面那份 base 契约里关于 IMAGE 的每一条（干净帧、
        无人称词、里程碑骨架、痕迹、包络覆盖……）继续照旧生效，一个字都不重复。

        ladder 缺省用本单时长下的施工梯——批量直出的这一段是**每拍共享**的，而同一批
        里可能混着揭示拍与兑现拍，它们各自的切点表由 normalize 逐拍确定性覆写，
        这里只需要把三套梯的规则讲清楚。"""
        duration = self.clip_duration()
        ladder = ladder or self.ladder_for_kind(duration, 'construction')
        target, ceiling = video_word_targets(len(ladder))
        insert_count = '两个' if len(ladder) == 4 else '一个'
        return f"""

==================== MINIATURE MULTI-SHOT VIDEO OVERRIDE (读到这里为止的 VIDEO 规则以本段为准) ====================
本单片长 {duration} 秒。上面所有关于 IMAGE 的规则**继续完全生效，不做任何修改**；唯独 VIDEO 的
镜头语法整体改写为下面这套，与上文冲突处一律以本段为准。

MANDATORY SHOT STRUCTURE — 每一条 VIDEO 都是**一条贯穿全段的微距主工作镜，中间被{insert_count}特写
插入切开，最后切回同一机位收尾**。{duration} 秒对应 {len(ladder)} 个镜头，按这个顺序写满，镜头之间用
clean cut / match cut 衔接（禁止 cross-dissolve、fade、instant transformation、跳过物理过程的快剪）：
{ladder_roles(ladder, insert_subject)}

SAME LOCKED SETUP（本包的锚点连续性就靠它）——第一镜与最后一镜是**同一台锁死的微距机位、同一构图、
同一焦段**，只有施工完成度不同；正文在最后一镜里要写明它切回的是 the same locked macro setup as the
opening macro working shot。插入镜是从这台机位推近的细部，切回来时完成度必须与切走那一刻一致。
CINEMATIC NARRATIVE FLOW (纯自然语言多镜头因果流)——严禁使用任何机械时间戳或数字切点表。正文必须使用流畅的电影分镜叙事连词（例如 "The scene opens with...", "The camera then cuts into a tight close-up insert on...", "Shifting to an extreme close-up insert...", "Finally, the camera cuts back to the primary locked diorama setup..."）来自然串联各个镜头。正文所有计数和尺寸一律写成英文单词（three roof tiles，不是 3 roof tiles；eight seconds，不是 8s）。

拍型分流：剖面揭示拍（巨人手揭走外立面板/屋顶）与最终兑现拍走各自的三镜梯，镜头名相同、逐镜职责不同，
两者都免除下面的节奏声明（它们是揭示与收尾，不压缩劳动），但**同样不许写成一镜到底**。
揭示拍另外还是零施工拍：不清理、不安装、不上色，不写任何 time-lapse 措辞。

ONE-TAKE BAN（无例外，揭示拍与兑现拍也一样）：禁止写 oner、one-shot、one-take、
single continuous take、one continuous take、single take、unbroken take 或任何等义措辞。
本包过去那句 "continuous miniature craft time-lapse" 也一并作废——用下面的节奏声明。

ACTOR ACROSS CUTS：施工主体自始至终是从画幅边缘伸入的超大真人手。主镜零秒就已经有一只手在作业面上
并发生第一次有效工具接触（不要为"手入画"单独安排一个镜头）；插入镜里只有指尖与工具接触点；
切回镜里手完成最后一次操作后**退出画幅**，末帧是净帧。

CAST IN FRAME（活物即时应激与动作-反应咬合）：人偶是画面中唯一的活物，绝不能只在结尾贴一句站位，
必须将其微动作与工匠施工动作形成【因果时序咬合】：
- 手从边缘入画时：人偶立即产生生理/视线应激（抬头仰望、转头注视）；
- 手持工具作业推进时：人偶视线跟随工具移动或微调站姿（上前半步、探头、指引）；
- 手收尾撤出画面时：人偶身体定格在交付成果前观望。
严禁写「人偶保持原样/站位不变/remain/stay」；当上游给出了 CAST IN FRAME 时，必须完整还原这一因果链。
{self.capture_style_rule()}
- PACING DECLARATION：普通工序拍在正文里声明一次时间基准，用这句原话——
  "{MINIATURE_PACING_PHRASE}"（揭示拍与兑现拍免除这句）。不要用 continuous 描述整条片段的拍法。
- IN-SHOT CONTINUITY：上文那条 EVEN RATE 指令（"每一刻都在推进 / 不许把改动推迟后一次兑现"）
  在多镜头包里**作废**，改用这句原话——
  "{MINIATURE_INSHOT_PHRASE}"
  理由：推进量全部集中在主镜，特写插入按契约不产生新的推进量，而切回镜恰恰是在剪辑点上
  做 same-way 压缩。要求"每一刻都在推进"等于要求模型违反自己的镜头级进度锁。
- PROGRESS ACROSS CUTS：每个镜头都从上一镜结束时的完成度开始；剪辑点只允许压缩"已经完整演示过一次"的
  重复动作，且必须在正文里说明（例如 after the remaining tiles are lapped the same way），
  不得跳过某类改动的第一次发生、不得凭空出现新物件、不得在剪辑点上让数量变化。
- PHRASING VARIATION：镜头梯是固定骨架，因此逐拍复读是本技能的头号失败模式。锚定开场句、切点表、
  镜头名、节奏声明这几项**必须逐字保留**；除此之外，相邻两拍的句式模板、镜头内的从句顺序、
  动词选择、转场措辞、工具与痕迹的写法都必须换过。
- 音效：微观工艺音（刀刃刮擦 / 镊子轻叩 / 石片轻碰 / 点胶针挤压 / 细砂纸摩擦）+ 稳定室内底噪；
  **绝不写脚步声**。切点本身不配音效。
- 长度：整条 VIDEO 目标 {target} 词上下，硬顶 {ceiling} 词。主镜与切回镜各 60–90 词
  （它们承载起始状态、推进过程与结果状态），每个特写插入 30–50 词。
==================== MINIATURE REQUIRED REFERENCES (本段是上面覆盖声明的详细规范) ====================
{self.required_references_block(include_threshold=include_threshold)}"""

    # ── 覆写钩子 ────────────────────────────────────────────────────────────

    def batch_system_prompt(self, config, packet, scup_ref, tbcp_ref):
        """生成微缩专项的批量共享 system prompt。

        顺序是有意的：base 共享段 → 微缩世界观覆盖 → 多镜头镜头语法覆盖。
        世界观（谁在施工、用什么光学、室内怎么拍）先立住，镜头语法是 VIDEO 的最后一道口径。"""
        base_prompt = self.rebrand_identity(
            pp._batch_shared_system_prompt(packet, scup_ref, tbcp_ref))
        return (base_prompt + _MINIATURE_WORLDVIEW_OVERRIDE
                + self.video_override_block(include_threshold=bool(tbcp_ref))
                + self.banned_elements_block() + self.scene_constants_block())

    def single_beat_system_prompt(self, config, i, contract, packet, compiled_images,
                                  compiled_videos, scup_ref, tbcp_ref_i):
        """单拍生成的 system prompt（含身份、微缩世界观与本拍镜头梯覆盖）。

        super() 是 OmniComposer：它会在 base 正文之后追加 self.video_override_block(...)，
        而那个方法已被本类覆写，所以这里拿到的是**微缩版**的镜头语法段，不是 omni 版。"""
        base_prompt = self.rebrand_identity(super().single_beat_system_prompt(
            config, i, contract, packet, compiled_images, compiled_videos, scup_ref, tbcp_ref_i
        ))
        # 逐拍通路独有的那段实景公制规则，整块换成微缩口径（见常量处的说明）。
        base_prompt = _VEO_METRIC_BLOCK_PATTERN.sub(
            _MINIATURE_METRIC_BLOCK.replace('{i}', str(i)), base_prompt)
        miniature_note = """

==================== MINIATURE OVERRIDE FOR THIS BEAT ====================
- ACTOR: Action must be executed by OVERSIZED HUMAN HANDS entering from frame margins with micro-tools.
- DO NOT generate full-scale 1.78m workers, safety vests, or people inside the model.
- OPTICS: Maintain macro diorama framing (fifty to eighty-five millimetre macro lens feel) and shallow depth of field with creamy background bokeh, on ONE locked camera setup for the whole clip.
- SHOTS: This clip is cut, not a oner — write the shot ladder using fluent cinematic narrative transitions without numeric timestamps or robotic cut mark tables.
- INTERIORS: If this is an interior stage, use open-front dollhouse cutaway framing filmed from the exterior tabletop perspective.
- PACING & AUDIO: Use the edited miniature craft time-lapse pacing declaration and micro-tool contact sound effects (no human footsteps).
"""
        return base_prompt + miniature_note

    # ── 确定性注入的微缩口径 ────────────────────────────────────────────────

    def ensure_actor_engagement(self, video_prompt, ladder, packet=None, beat=None,
                                is_threshold_or_reveal=False):
        """保证正文里的施工主体是从画幅边缘伸入的巨人手，且零秒就在作业面上。

        omni 的实现会注入一句 "the same lone worker{costume} is already positioned at the
        active work face"——在本包里那是 P0 违规（门禁会拦、还要烧一轮回炉）。这里换成
        巨人手口径，并且**只在正文完全没写到手时**才补：手从哪条边缘伸入是逐拍的创作，
        写死一条会让每拍都从同一个角落伸手。"""
        text = video_prompt or ''
        if not ladder or is_threshold_or_reveal:
            return text
        low = text.lower()
        if 'hand' in low or 'finger' in low:
            return text
        if text and not text.rstrip().endswith(('.', '!', '?')):
            text = text.rstrip() + '.'
        return (text.rstrip() + " In the opening macro working shot one oversized human hand is "
                "already reaching in from the upper frame margin with its micro-tool and makes "
                "the first effective tool contact from the opening instant; the hands withdraw clear of "
                "the frame before the final moment.").strip()

    def fallback_ladder_clause(self, ladder):
        """占位兜底稿补的镜头梯声明：一句话里按顺序带齐每一级镜头名与锁死的微距质感，
        让占位稿至少不违反镜头语法（占位稿本身仍计入 fallback_count 门禁）。"""
        fragments = [_MINIATURE_FALLBACK_FRAGMENT[rung.key] for rung in ladder]
        joined = ', '.join(fragments[:-1]) + f", and {fragments[-1]}"
        count = _COUNT_WORDS.get(len(ladder), str(len(ladder)))
        return (f"The clip is cut as {count} shots in order — {joined} — joined by clean cuts and "
                f"shot entirely on {MINIATURE_OPTICS_PHRASE}, with the tabletop sandbox ground "
                f"held at the frame edge.")

    def multishot_rework_system(self, ladder, duration):
        """镜头语法定向回炉的 system prompt（微缩口径）。

        与 omni 版的差别全在世界观：施工主体是巨人手不是工人、机位锁死不许换、
        读的是本包的 miniature-multishot-language.md。回炉的调用与复验流程仍走 omni 的实现。"""
        multishot_ref = self.reference(self.multishot_reference)
        scales = ', '.join(rung.phrase for rung in ladder)
        return f"""You are rewriting ONE video prompt so it obeys the miniature diorama multi-shot contract.

{multishot_ref}

Rewrite rules (additive — do not lose content):
- Keep the opening anchor sentence ("Use the provided image as the exact starting composition and environment anchor. ...") VERBATIM as the first sentence.
- Express the multi-shot sequence using pure natural language cinematic transitions (e.g. 'The scene opens with...', 'The camera then cuts into a tight close-up insert on...', 'Shifting to an extreme close-up insert...', 'Finally, the camera cuts back to the primary locked diorama setup...'). Do NOT output numeric timestamps, seconds marks, or robotic cut mark tables.
- Keep every concrete detail already in the draft: the same giant hands and their entry margin, the same named micro-tool, the same operation, the same craft traces, the same figurines, the same audio description, the same lighting. Redistribute them across the shots instead of inventing new ones.
- Restructure the body into exactly {len(ladder)} shots IN THIS ORDER, naming each one in prose exactly as written here: {scales}. Join them with clean cuts or match cuts.
- The first and the last shot are the SAME locked macro camera setup, framing, and focal length, differing only in how far the build has progressed; say so explicitly in the last shot ("the same locked macro setup as the opening macro working shot"). The insert(s) move closer on that same model and cut back at the same completion level. The camera itself never pans, tracks, pushes, or pulls back, and it never enters the model.
- The builder is an OVERSIZED REAL HUMAN HAND (giant fingers) reaching in from a frame margin with a precision micro-tool — never a full-scale worker, never a person inside the model. One hand is already making effective tool contact from the opening instant in the opening macro working shot; that shot carries this beat's whole visible advance up to roughly three quarters; the insert(s) carry tool contact, material physics, and the persistent craft traces without advancing the state; the returning macro shot compresses the remaining repetitions the same way, and the hands withdraw clear of frame before the last moment so the final frame is clean.
- Interiors are filmed from OUTSIDE through an open-front dollhouse cutaway. Never write a walk-in threshold, a camera pushing through a doorway, or a full-size room.
- All numbers, counts, and dimensions must be written in English words or everyday comparisons.
- NEVER write oner, one-shot, one-take, single continuous take, one continuous take, single take, or unbroken take — there is no exemption.
- Output ONLY the rewritten video prompt body. No headings, no labels, no commentary."""

    def ensure_living_cast_reaction(self, video_prompt, beat=None, packet=None, is_threshold_or_reveal=False):
        """确保微缩场景中常驻的人偶具备动态三段式因果时序咬合，杜绝死人/假人现象。"""
        text = video_prompt or ''
        if is_threshold_or_reveal:
            return text
        low = text.lower()

        # 检查是否已包含人偶/居民描述
        has_cast = any(k in low for k in ('figurin', 'couple', 'bystander', 'resident', 'cast in frame'))
        if has_cast:
            return text

        # 若正文未提及人偶，自动在末段切回镜前或结尾注入微缩人偶的动态响应
        cast_phrase = (
            "Down by the diorama edge, the two miniature couple figurines tilt their heads up in awe "
            "to track the giant hand as it works, shifting their stance and nodding approvingly as the stage settles."
        )

        if 'finally, the camera cuts back' in low:
            text = re.sub(
                r'(?i)(finally,\s+the\s+camera\s+cuts\s+back\s+to\s+[^.;]+?[.;])',
                r'\1 Down by the diorama edge, the miniature couple figurines lean in with curiosity, their gaze actively tracking the final micro-adjustments before standing side by side to admire the finished work.',
                text,
                count=1
            )
        elif not text.rstrip().endswith(('.', '!', '?')):
            text = text.rstrip() + f". {cast_phrase}"
        else:
            text = text.rstrip() + f" {cast_phrase}"
        return text

    # ── 微缩专属的确定性清洗 ────────────────────────────────────────────────

    def fix_miniature_video(self, text):
        """清洗视频提示词中的全尺寸工人、工匠、假人静态词、广角镜头与旧的一镜到底节奏声明。"""
        res = str(text or '')

        # 1. 彻底清除 base fix_out_and_in / omni 注入的实景工人起手句与工匠句式
        res = re.sub(
            r'(?i)\b(?:at\s+t\s*=\s*0s?|at\s+zero\s+seconds?)[,;:]?\s*(?:the\s+same\s+)?'
            r'(?:one\s+lone\s+|the\s+|a\s+)?(?:male\s+)?(?:workers?|craftsm[ae]n)\b[^.;]*?'
            r'\b(?:is\s+already\s+positioned|is\s+already\s+stationed|is\s+already\s+at\s+the\s+work\s+face|'
            r'makes?\s+(?:the\s+)?first\s+contact|enters?|begins?)[^.;]*[.;]?',
            '',
            res
        )
        res = re.sub(
            r'(?i)\b(?:one|the|a)\s+(?:lone\s+)?(?:male\s+)?(?:workers?|craftsm[ae]n)\s+(?:with\s+bare\s+hands\s+and\s+forearms[^.;]*?)?(?:is\s+already\s+)?(?:stationed\s+)?at\s+the\s+work\s+face\b[^.;]*?[.;]?',
            '',
            res
        )
        res = re.sub(
            r'(?i)\ba\s+single\s+worker\'?s?\s+bare\s+hands\s+and\s+human\s+fingers\b',
            'oversized human hands and fingers',
            res
        )

        # 2. 替换各类实景工人/工匠称谓为超大真人手
        res = re.sub(
            r'(?i)\b(?:a|one)\s+(?:lone\s+)?(?:male\s+)?(?:workers?|craftsm[ae]n)\s*(?:\([^)]*\))?',
            'an oversized human hand',
            res
        )
        res = re.sub(
            r'(?i)\bthe\s+(?:same\s+)?(?:lone\s+)?(?:male\s+)?(?:workers?|craftsm[ae]n)\b',
            'the giant hand',
            res
        )
        res = re.sub(
            r'(?i)\b(?:lone\s+)?workers?\b',
            'oversized hands',
            res
        )
        res = re.sub(
            r'(?i)\b(?:the\s+)?craftsm[ae]n\b',
            'the giant hand',
            res
        )
        res = re.sub(
            r'(?i)\bhuman\s+fingers\b',
            'giant fingers',
            res
        )

        # 3. 强力清洗假人/静止人偶词汇并转化为动态应激
        res = re.sub(
            r'(?i)\b(?:unmoving|motionless|static|frozen|still)\s+miniature\s+(?:bystander\s+)?figurines?\s+stand\s+watching\s+quietly\b',
            'the curious miniature figurines tilt their heads and shift stance, attentively tracking the micro-tool movements',
            res
        )
        res = re.sub(
            r'(?i)\b(?:unmoving|motionless|static|frozen)\s+miniature\s+figurines?\b',
            'miniature figurines',
            res
        )
        res = re.sub(
            r'(?i)\bminiature\s+(?:couple\s+)?figurines?\s+(?:remain|stay\s+put|stand\s+still|stand\s+quietly|are\s+unchanged)\b',
            'miniature figurines shift their posture and turn their gaze',
            res
        )

        # 4. 平滑化特写切镜语义，消除导致时序冻结的 zero advancement 措辞
        res = re.sub(
            r'(?i)\bwith\s+zero\s+(?:state\s+advancement|progress\s+advance)\b',
            'capturing the continuous microscopic material shearing without skipping stages',
            res
        )
        res = re.sub(
            r'(?i)\bwithout\s+advancing\s+the\s+overall\s+build\s+(?:state|stage)\b',
            'showcasing the fine material physics and tactile contact without skipping ahead',
            res
        )

        # 5. 替换 24mm 广角机位为微距眼平机位
        res = re.sub(
            r'(?i)\b(?:\d+\s*mm|twenty[- ]four\s+millimeter)\s+wide-?angle\s+(?:tripod\s+shot|lens|view|framing)\b',
            'macro diorama eye-level tripod shot',
            res
        )

        # 6. 替换脚步声音效
        res = re.sub(
            r'(?i)\b(?:and\s+)?footsteps\s+of\s+this\s+beat\b',
            'and fine tool clicks of this beat',
            res
        )
        res = re.sub(r'(?i)\bfootsteps\b', 'craft contact sounds', res)

        # 7. 旧的一镜到底节奏声明 → 多镜头节奏声明
        for pattern in _LEGACY_PACING_PATTERNS:
            if not pattern.search(res):
                continue
            replacement = '' if MINIATURE_PACING_MARKER in res.lower() else MINIATURE_PACING_PHRASE
            res = pattern.sub(replacement, res)

        # 8. 终极去重
        res = self.deduplicate_boilerplate_phrases(res)
        return re.sub(r'\s{2,}', ' ', res).strip()

    def fix_miniature_image(self, text):
        """清洗图片提示词中的镜头与反微缩描述，注入微缩背景虚化。"""
        res = str(text or '')
        # 替换 24mm 广角机位
        res = re.sub(
            r'(?i)\b(?:\d+\s*mm|twenty[- ]four\s+millimeter)\s+wide-?angle\s+(?:tripod\s+shot|lens|view|framing)\b',
            'macro diorama eye-level tripod shot',
            res
        )
        # 清洗 Base 注入的反微缩语句
        res = re.sub(
            r'(?i)\bnever\s+reading\s+as\s+a\s+distant\s+miniature\s+in\s+a\s+(?:landscape\s+)?panorama\.?',
            '',
            res
        )
        res = re.sub(r'(?i)\bnot\s+a\s+miniature\b', '', res)
        # 替换 Base 注入的地平线句为浅景深焦外描述
        res = re.sub(
            r'(?i)The\s+horizon\s+line\s+remains\s+perfectlys?\s+level\s+at\s+exactly\s+half\s+the\s+frame\s+height\.?',
            'The background features soft creamy blur with shallow depth of field.',
            res
        )
        return re.sub(r'\s{2,}', ' ', res).strip()

    def apply_proactive_fixes(self, i, video_prompt, image_prompt, packet, mode, is_last,
                              is_threshold_or_reveal, beat=None, config=None, family=None,
                              beat_ladder=None):
        """先走 omni 的多镜头修复链（切点表、一镜到底清洗、字数预算），再做微缩清洗与活物动态保底。"""
        fixed_video, fixed_image = super().apply_proactive_fixes(
            i, video_prompt, image_prompt, packet, mode, is_last, is_threshold_or_reveal,
            beat=beat, config=config, family=family, beat_ladder=beat_ladder
        )
        fixed_video = self.fix_miniature_video(fixed_video)
        fixed_video = self.ensure_living_cast_reaction(
            fixed_video, beat=beat, packet=packet, is_threshold_or_reveal=is_threshold_or_reveal
        )
        fixed_image = self.fix_miniature_image(fixed_image)
        return fixed_video, fixed_image

    def validate_beat_prompts(self, i, video_prompt, image_prompt, packet, mode, is_last,
                              is_threshold_or_reveal, prev_video=None, prev_image=None,
                              beat=None, family=None, is_pre_bridge=False,
                              is_post_reveal_cleanup=False):
        """omni 的镜头语法门禁 + 微缩契约门禁。"""
        errors = list(super().validate_beat_prompts(
            i, video_prompt, image_prompt, packet, mode, is_last, is_threshold_or_reveal,
            prev_video=prev_video, prev_image=prev_image, beat=beat, family=family,
            is_pre_bridge=is_pre_bridge, is_post_reveal_cleanup=is_post_reveal_cleanup
        ) or [])
        errors.extend(check_miniature_macro_optics(video_prompt))
        errors.extend(check_miniature_macro_optics(image_prompt))
        errors.extend(check_miniature_actor_violations(video_prompt))
        errors.extend(check_miniature_actor_violations(image_prompt))
        errors.extend(check_miniature_cutaway_framing(video_prompt))
        errors.extend(check_miniature_cutaway_framing(image_prompt))
        return errors

    def split_structural_video_errors(self, errs):
        """微缩模式下的结构性硬伤分类：镜头语法违规照 omni 的口径回炉（super 已归入
        structural），只把 ghost work 从结构性硬伤降级为留痕瑕疵，避免误触发回炉——
        微缩线的插入镜按契约就可能整镜没有手在画面里。"""
        structural, rest = super().split_structural_video_errors(errs)
        ghosts = [e for e in structural if 'ghost work' in e]
        cleaned_structural = [e for e in structural if 'ghost work' not in e]
        cleaned_rest = list(rest) + ghosts
        return cleaned_structural, cleaned_rest


# 世界观覆盖段。放在 base 共享段之后、镜头语法段之前——它回答"谁在施工、多大、用什么光学、
# 室内怎么拍"，镜头语法段回答"这条片子怎么剪"，两段不重叠。
_MINIATURE_WORLDVIEW_OVERRIDE = """

==================== MINIATURE & GIANT HAND DIORAMA OVERRIDE (读到这里为止的 VIDEO/IMAGE 规则以本段为准) ====================
You are a professional prompt composer operating under the `gemini-miniature-restoration-composer` profile.
This is a MINIATURE DIORAMA / GIANT HAND DIY BUILD.

PRIORITY & WORLDVIEW OVERRIDE:
- 上文所有关于 1.78m 工人、全景 24mm 建筑广角走入式机位、走入式穿门（TBCP walk-in threshold bridge）以及反微缩负向词（miniature furniture, dollhouse scale 等）的规则全部作废，与上文冲突处一律以本段为准。

1. THE ACTOR (Oversized Human Hands):
   - Construction is executed 100% by OVERSIZED REAL HUMAN HANDS (Giant Hands / Macro Fingers) entering from frame margins (upper edge or lateral edges) wielding precision micro-tools (miniature pointing trowels, fine-tip tweezers, hobby craft blades, syringe glue applicators).
   - NEVER describe a 1.78m human worker, neon safety vests, hard hats, or people walking inside the miniature structure.

2. THE RESIDENTS (Tiny Figurines) — they are LIVING CAST, not static props:
   - Two cast-resin painted miniature figurines (one to twenty-four dollhouse scale couple, roughly a thumb tall) inhabit or observe the model from foreground edges. They watch; they never build.
   - ACTION-REACTION CAUSAL INTERLOCK: They are ALIVE in every beat and actively respond to the craftsman:
     * When hands/tools enter from margins, figurines immediately react with head/gaze shifts (tilting heads up in awe, turning to look).
     * During active craft work, they track the tool motions with slight shifts of weight, steps closer, or pointing gestures.
     * When work completes and hands withdraw, they settle into their final observing stance facing the new work.
    - What is LOCKED is their identity, costume and scale (same two figurines, same attire, thumb-tall height, never touching a tool, never leaving the frame). What is FREE is their pose, orientation and micro-actions dynamically reacting to the work.
   - NEVER write them as unchanged ("the two figurines remain where they were") or isolate their motion as a passive afterthought at the very end of the clip.

3. MACRO OPTICS & DEPTH OF FIELD:
   - Macro lens feel (fifty to eighty-five millimetres equivalent), shallow depth of field with creamy background bokeh.
   - Camera is locked at model eye-level (just above the tabletop) or a thirty to forty-five degree oblique tabletop angle, and it stays on that one setup for the whole clip.

4. CUTAWAY DOLLHOUSE ARCHITECTURE FOR INTERIORS:
   - All interior stages are filmed from OUTSIDE through an open-front dollhouse cutaway or open-roof view. Giant hands reach directly into the cutaway rooms to place cabinets, wire micro LEDs, or stage miniature fixtures.
   - NEVER describe walk-in threshold passage into an enclosed room.

5. CRAFT MATERIALS & MICRO TRACES:
   - Use miniature materials: basswood slats, thumbnail-sized miniature blocks, craft mortar slurry, balsa studs, micro LED wire.
   - Traces: tiny glue fillets, miniature cement beads, fine sawdust at cut edges.

6. SOUND DESIGN:
   - Fine tactile micro-craft noises (blade scoring, tweezer taps, tiny stone clicks, glue syringe clicks, delicate sandpaper friction) and steady quiet indoor workshop tone. No human footsteps.
"""
