"""活物一律真人（Human Cast Policy）。

问题：整条链路的词汇表是从「微缩沙盘」那条线长出来的——反推的读数模板、两个合成器的
系统提示词、比例锁的锁句、4选1 的评分口径，全都管画面里的活人叫 figurine。于是即便
原片拍的是活生生的人（2026-08-30 实测 replica_af8db0d7a95f：scene_constants.cast 写的
是 "the lone builder: light-skinned Caucasian man…"），交付的提示词照样写成 "the lone
equipment figurine in a royal blue work jacket"——生图模型照着这个词渲，人就成了蜡像。

策略（用户 2026-08-30 定的）：**所有通道一律真人，微缩线也不例外**。微缩片里的人该多大
还是多大——尺寸由比例锁管（observed_grounding.cast_scale_clause）；这里管的是**材质与
身份**：他们是有皮肤、有布料、会自然发力的活人，不是树脂/塑料/蜡/玩偶。

两件事分开做，别混：
  · 尺寸  → 比例锁（1:24、7cm 高）。保留，照写。
  · 材质/身份 → 本模块。figurine/doll/mannequin/wax figure → 真人措辞。

为什么是确定性改写而不是只改系统提示词：写手是模型，改提示词只能降低概率。这一层落在
交付边界上（apply_proactive_fixes 末尾、极速通道出口、复刻线合成收尾），无论词是从模板、
从写手、还是从合成后的「对帧订正」进来的，都出不了这道门。

**否定句一律不碰**：正文里写 "never reads as a plastic doll" 是对的，把它改成
"never reads as a real person" 意思正好翻过来。见 _NEGATION_CUES。
"""
import re

# 材质/工艺定语：跟在活物名词前面时整段删掉（"cast-resin miniature figurine" → 真人）。
_MATERIALS = (
    r'cast[-\s]?resin|poly[-\s]?resin|resin|plastic|vinyl|pvc|porcelain|ceramic|'
    r'clay|polymer|silicone|wax(?:work)?|papier[-\s]?m[aâ]ch[eé]|'
    r'hand[-\s]?painted|painted|moulded|molded|sculpted|carved|'
    r'3d[-\s]?printed|toy|action'
)

# 一定是「假人」的名词。刻意不收 figure / model / statue：
#   · "figure" 单用多半是 "a figure in the doorway"（就是个人影），改了反而丢信息；
#   · "model" 满地都是（scale model of the house、image model），改它必然误伤；
#   · "statue" 是场景里真的雕像，不是活物。
# 带材质定语的 figure（"resin figure"）由 _MATERIAL_FIGURE_RE 单独收。
_DOLL_NOUNS = r'figurines?|dolls?|mannequins?|manikins?|puppets?|statuettes?|waxworks?'

# 否定线索：命中就整处跳过。窗口取匹配前 44 个字符——够覆盖 "never reads as a"、
# "rather than a"、"instead of a" 这类前缀，又不至于把上一句的 not 也算进来。
_NEGATION_CUES = (
    'never', 'not ', "n't", 'no ', 'avoid', 'without', 'rather than',
    'instead of', 'nor ', 'anti-', 'non-', 'forbid', 'ban ', 'banned',
)
_NEG_WINDOW = 44


def _is_negated(text, start):
    head = text[max(0, start - _NEG_WINDOW):start].lower()
    return any(cue in head for cue in _NEGATION_CUES)


def _plural(noun):
    n = noun.lower()
    return n.endswith('s') and not n.endswith('ss')


# 主模式：[1:24 scale] [cast-resin ...] [miniature] figurines
_CAST_RE = re.compile(
    r'(?P<scale>\b1\s*[:：]\s*\d{1,3}(?:[-\s]?scale)?[-\s]+)?'
    r'(?P<mats>(?:(?:' + _MATERIALS + r')[-\s]+)+)?'
    r'(?P<mini>(?:miniature|mini)[-\s]+)?'
    r'(?P<noun>' + _DOLL_NOUNS + r')\b(?![-\s]?like\b)',
    re.IGNORECASE,
)

# 带材质定语的 figure/figures（"resin figures"、"wax figure"）。单独一条，因为裸 figure
# 不能碰。
_MATERIAL_FIGURE_RE = re.compile(
    r'(?P<scale>\b1\s*[:：]\s*\d{1,3}(?:[-\s]?scale)?[-\s]+)?'
    r'(?P<mats>(?:(?:' + _MATERIALS + r')[-\s]+)+)'
    r'(?P<mini>(?:miniature|mini)[-\s]+)?'
    r'(?P<noun>figures?)\b',
    re.IGNORECASE,
)

# 「像人偶一样」的比喻定语。只收指人的那几个：plastic-like / resin-like 更多是在说
# 某个表面的质感（"plastic-like sheen on the wall"），改它就是误伤场景描述。
_DOLLLIKE_RE = re.compile(
    r'\b(?:doll|figurine|mannequin|waxwork)[-\s]?like\b', re.IGNORECASE)


def _match_case(sample, word):
    """把替换词的大小写对齐原词：句首大写要跟着大写，全大写同理。"""
    if sample.isupper():
        return word.upper()
    if sample[:1].isupper():
        return word[:1].upper() + word[1:]
    return word


def _replace(m):
    if _is_negated(m.string, m.start()):
        return m.group(0)
    noun = m.group('noun')
    scale = m.group('scale') or ''
    mini = m.group('mini') or ''
    # 尺度记号保留（那是尺寸，不是材质）；"miniature" 保留但改成明确的**尺度**说法，
    # 免得它继续兼职形容"是个模型"。已经有 "1:24 scale" 这种硬记号时就不再叠
    # "miniature-scale"——两句说的是同一件事，叠起来还读不通。
    mini_out = 'miniature-scale ' if (mini and not scale) else ''
    word = 'people' if _plural(noun) else 'person'
    out = f'{scale}{mini_out}{_match_case(m.group(0), word)}'
    return out


def humanize_cast_text(text):
    """把一段文本里的「假人」措辞就地改写成真人措辞。

    幂等：改写产物里不再含任何触发词，重跑一次字节不变。否定句原样保留。
    非字符串原样返回——调用方常常直接把 None / 列表灌进来。
    """
    if not isinstance(text, str) or not text:
        return text
    out = _CAST_RE.sub(_replace, text)
    out = _MATERIAL_FIGURE_RE.sub(_replace, out)
    out = _DOLLLIKE_RE.sub(
        lambda m: m.group(0) if _is_negated(m.string, m.start()) else 'lifelike', out)
    # 材质定语可能落在真人名词前面（"resin man"、"wax worker"），主模式够不着。
    stripped = re.sub(
        r'\b(?:' + _MATERIALS + r')[-\s]+(?=(?:miniature[-\s]+)?'
        r'(?:person|people|man|men|woman|women|worker|workers|builder|builders|'
        r'craftsman|craftsmen|resident|residents|couple|child|children|villagers?)\b)',
        lambda m: m.group(0) if _is_negated(m.string, m.start()) else '',
        out, flags=re.IGNORECASE)
    if stripped != out:
        # 只有真删掉了定语才收空格。无条件收的话，这个函数挂在交付正文的唯一出口上
        # （_delivery_scrub），会顺手把正文里本来就有的双空格一并改掉——那是另一件事，
        # 不该由"活物一律真人"这道口子代劳。
        stripped = re.sub(r'[ \t]{2,}', ' ', stripped)
    return stripped


# 真人身份前缀：给「识别出来就是个假人」的恒常项用（用户要的"自动优化为真人的形式"）。
_REAL_HUMAN_PREFIX = 'real living human'
_REAL_HUMAN_MARKERS = ('real living human', 'real human', 'living person', 'real people')


def humanize_cast_entry(text):
    """恒常项 / cast_identity 里的一条：改写措辞，并在原文确实是「假人」时补上
    一句真人身份声明。

    只给改写过的条目补前缀：本来就写着「一个留着络腮胡的男人」的条目不需要这句话，
    每条都补只会把恒常卡片撑长，也会在提示词里挤掉真正有信息量的字。
    """
    if not isinstance(text, str) or not text.strip():
        return text
    fixed = humanize_cast_text(text)
    if fixed == text:
        return text
    low = fixed.lower()
    if any(m in low for m in _REAL_HUMAN_MARKERS):
        return fixed
    return f'{_REAL_HUMAN_PREFIX} — {fixed}'


def humanize_cast_list(items):
    """一整栏恒常人物 / cast_identity。非列表原样返回。"""
    if not isinstance(items, list):
        return items
    return [humanize_cast_entry(x) if isinstance(x, str) else x for x in items]


def humanize_prompt_text(text):
    """交付边界上的那道：一条 IMAGE / VIDEO 正文。

    只做等长或更短的改写，**不追加任何句子**——IMAGE 有 180/220 词硬顶
    （prompt_pipeline.image_word_limit_for），在这道口子上追加一句会把本来合格的帧
    顶成超字数，然后被回炉轮当成写坏了重写一遍。真人这件事的正面表述放在系统提示词
    与恒常项里说，这里只负责把已经写出来的假人措辞掰回来。
    """
    return humanize_cast_text(text)


def humanize_prompt_block(block):
    """整份 prompt_block（极速通道单轮直出、复刻线合成收尾各调一次）。"""
    return humanize_cast_text(block)


# 给系统提示词用的一段硬规则。三个合成器共用同一份措辞，免得各写各的再各自漂移。
REAL_HUMAN_CAST_RULE = (
    "[LIVING CAST ARE REAL HUMAN BEINGS — NON-NEGOTIABLE]\n"
    "- Every living person in frame is a REAL HUMAN BEING: real skin with pores and "
    "sub-surface scattering, real hair, real woven fabric clothing that creases and hangs "
    "under gravity, and natural human body mechanics.\n"
    "- STRICTLY NEVER write them as figurines, dolls, mannequins, wax figures, resin or "
    "plastic models, action figures, or any painted/moulded toy - and never describe their "
    "skin, hair or clothing as resin, plastic, vinyl, porcelain, wax or painted.\n"
    "- This holds in EVERY channel, miniature sets included: in a miniature scene the people "
    "are small, but they are still photographed as living human beings, never as toy props. "
    "Their physical size is governed solely by the scale lock, never by their material."
)
