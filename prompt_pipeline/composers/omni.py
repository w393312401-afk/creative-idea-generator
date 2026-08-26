"""omni profile（gemini-omni-restoration-composer）的 Phase 2 composer。

与 base 的唯一区别在 VIDEO 一侧：Gemini Omni 的视频提示词不是一条一镜到底的片段，
而是一段**剪辑过的多镜头序列**，默认拍摄质感是 UGC 手机随手拍而不是院线感。IMAGE 段、
Phase 1（brief/工序梯/Drift Lock 包/IMAGE 1）、断点续传、槽位格式全部沿用 base——
它们是下游帧渲染/创意库/续传共同的契约，与「做哪个视频模型的提示词」无关。

2026-08-09「主镜 + 特写插入」改造。此前单段是一条五到六级的景别轮换梯
（远景→全景→中景→近景→特写→结果远景）。实拍的改造延时不是这么剪的：一个作业面
上真正成立的是**一条贯穿全段的主工作镜**，中间被一到两个特写插入切开，再切回同一
机位收尾。景别轮换梯把每一镜都换一次机位与尺度，短片长下每镜不足一秒，观感是闪帧；
更要命的是尺度一路换下去，锚点连续性只能靠文字反复申明来兜。

现在的施工梯只有两档：

  · 短片长（4s / 6s）三镜：主镜 wide working shot → 特写插入 close-up insert →
    切回 returning wide shot
  · 长片长（8s / 10s）四镜：主镜 → close-up insert → extreme close-up insert →
    切回 returning wide shot

要点：**主镜与切回镜是同一个机位**（returning wide shot 逐字要求 "the same camera
setup as the opening wide working shot"），所以首帧锚与尾帧锚天然落在同一构图上，
锚点连续性不再依赖跨尺度的文字兜底。推进量全部由主镜携带；插入镜按契约零推进，
只交代工具接触点的材料物理与本次操作特有的持久痕迹；剩余重复动作在切回那个剪辑点
上做 same-way 压缩，落到结果 IMAGE。

镜长约束随之改成两档：主镜与切回镜各不低于 1.3 秒，插入镜不低于 0.9 秒（插入本来
就是短镜，1 秒的插入读作插入，1 秒的**景别**才读作闪帧）。反向检查保留并更重要了：
正文里写出梯外的景别——尤其是旧语法的 establishing long shot / full shot /
medium shot / wide outro shot——一律按硬伤报（_extra_shot_rungs），否则模型会照着
旧习惯把景别轮换梯悄悄写回来。

  2. **切点用时间线句显式钉在秒上**。这是本次唯一被允许出现阿拉伯数字的地方
     （omni-output-templates.md §Notation Ban 的 Timecode exemption），正文其余部分
     的数字一律折成英文单词。时间线句由 _inject_timeline 确定性注入并覆写——模型
     自己编的时间线不作数。

  3. **过门桥拍与最终兑现拍走各自的梯**。此前它们被同一套施工梯审计，等于要求
     一段穿门镜头也写出"工具接触点"和"重复作业循环"。

同时清掉两处与 base 契约的硬冲突：
  · base 的 even-rate 句（"每一刻都在推进、不许把改动推迟后一次兑现"）与镜头级进度锁
    直接对撞——远景/全景推进量为零、特写不产生推进量、结果远景正是在剪辑点上做
    same-way 压缩。改用 OMNI_INSHOT_PHRASE：连续性约束到**镜内**，压缩只允许发生在
    声明过的切点上。
  · base 的 out-and-in 兜底会往多镜头包里塞绝对时间戳与固定入画口（"At t=0s ... from
    the Grid C1 edge ... by t=7.5s"）：时间戳按 8 秒写死、Grid 记号本身违反记号禁用、
    且与"镜一零工人 / 镜二从命名路径入画"冲突。omni 下整条跳过，工人进出由镜头梯承载，
    真缺进出仍由 base 的 check_out_and_in 报错并触发回炉。

契约文件一律经 pp.load_reference_file(name, self.profile) 读取（omni 包缺的文件由它
回落到 base），不在这里拼任何路径。

2026-08-23 起本文件不只服务 omni：MiniatureComposer 也继承 OmniComposer，复用整套镜头梯
机制（切点表、一镜到底禁令、镜头名审计、定向回炉），只换皮不换机制。**改这里等于同时改两条线。**
可换皮的接缝就这五处，子类覆写它们、不要改模块级实现：
  · ladder_for_kind —— 拍型 → 镜头梯（miniature 有自己的镜头名，且三套梯收敛成一种形状）
  · ensure_pacing + pacing_phrase / inshot_phrase 四个类属性 —— 节奏与镜内连续性文案
  · ensure_actor_engagement —— 零秒作业主体句（omni 是实景工人，miniature 是巨人手）
  · fallback_ladder_clause —— 占位兜底稿的镜头梯声明与拍摄质感
  · multishot_rework_system —— 定向回炉的 system prompt
类内取梯一律走 self.ladder_for_kind，不要直接调模块级的 ladder_for。
"""

import re
import sys
from collections import namedtuple

import prompt_pipeline as pp
import server_common

from .base import BaseComposer


# SKILL.md §Required Reference Loading 的「Always load, every composition run」清单。
# 顺序即 SKILL.md 的声明顺序。
OMNI_ALWAYS_LOAD_REFERENCES = (
    'omni-scene-skeleton.md',
    'omni-multishot-language.md',
    'omni-restoration-continuity.md',
    'omni-beat-skeleton.md',
    'omni-damage-vocabulary.md',
    'omni-lighting-environment-audio.md',
    'omni-output-templates.md',
)

# 条件加载：故事真的要从室外穿过一个开口进到室内时才读（SKILL.md 的 Load conditionally）。
OMNI_THRESHOLD_REFERENCE = 'omni-threshold-bridge.md'


# ── 镜头梯 ──────────────────────────────────────────────────────────────────
# variants 一律写成 _normalized() 之后的形态（小写、无连字符、单空格），判定按**出现
# 顺序**做，不只是"都出现过"——乱序的几个词不是轮换，是把词堆在一句里骗过检查。
# weight 是时长分配权重：主镜要装下"起始状态 + 第一次动作完整可见 + 重复循环"，配额必须最高。
Rung = namedtuple('Rung', 'key variants label phrase weight role')

_R_MAIN = Rung(
    'main', ('wide working shot',), '主镜 wide working shot',
    'a wide working shot', 1.5,
    '贯穿本段的主工作镜，画面开在起始 IMAGE 上、工人已经在作业面，0 秒立即发生第一次'
    '有效工具接触；第一次动作完整可见后转入重复循环，本拍改动在这一镜内推进到约四分之三，'
    '全程用 -ing / partially / growing 这类进行态措辞，不出现完成态描述')
_R_CLOSE = Rung(
    'close', ('close up',), '特写插入 close-up insert', 'a close-up insert', 1.0,
    '从主镜切进来的特写插入：工具接触点与材料物理（形变、碎屑、粉尘、纤维、飞溅），'
    '不产生新的推进量，切回主镜时完成度与切走那一刻一致')
_R_XCLOSE = Rung(
    'xclose', ('extreme close up',), '第二处特写插入 extreme close-up insert',
    'an extreme close-up insert', 0.9,
    '第二个插入镜，至少两处本次操作特有的持久痕迹与微观质感，同样不产生推进量')
_R_RETURN = Rung(
    'return', ('returning wide shot',), '切回主镜 returning wide shot',
    'a returning wide shot', 1.3,
    '切回**与主镜完全相同的机位与构图**（正文要写明 the same camera setup as the opening '
    'wide working shot），剩余重复动作在这个剪辑点上做 same-way 压缩，工人继续施工至镜头'
    '结束，画面落到这一拍的结果 IMAGE，不安排退场或空镜尾巴，只有这一镜可以用完成态措辞')

# 旧景别轮换梯的四级。已不在任何施工梯里，保留定义只为 _extra_shot_rungs 认得出来——
# 模型照旧习惯写回 establishing / full / medium / wide outro 时要按硬伤报，而不是
# 被当成"没写在梯里的无害措辞"放过去。
_R_ESTABLISHING = Rung(
    'establishing', ('establishing long shot',), '远景 establishing long shot',
    'an establishing long shot', 1.0, '（旧语法，已废弃）')
_R_FULL = Rung(
    'full', ('full shot',), '全景 full shot', 'a full shot', 1.0, '（旧语法，已废弃）')
_R_MEDIUM = Rung(
    'medium', ('medium shot',), '中景 medium shot', 'a medium shot', 1.4, '（旧语法，已废弃）')
_R_OUTRO = Rung(
    'outro', ('wide outro shot', 'wide outro'), '结果远景 wide outro shot',
    'a wide outro shot', 1.1, '（旧语法，已废弃）')

_R_APPROACH = Rung(
    'approach', ('wide approach shot',), '逼近远景 wide approach shot',
    'a wide approach shot', 1.0,
    '画面等同起始 IMAGE，镜头在开口外侧逼近，开口与两处被窥见的室内地标已可辨，全程零施工')
_R_THRESHOLD = Rung(
    'threshold', ('threshold shot',), '门槛 threshold shot', 'a threshold shot', 1.1,
    '推进到门槛处，门框在画面里滑出，曝光与白平衡开始从室外滚向室内，被窥见的地标占比放大')
_R_ARRIVAL = Rung(
    'arrival', ('interior wide shot',), '落定 interior wide shot',
    'an interior wide shot', 1.2,
    '完全落定在室内，画面精确等同结果 IMAGE，被窥见的地标已成为室内主地标，无工人无工具')

_R_DETAIL = Rung(
    'detail', ('detail shot',), '细部 detail shot', 'a detail shot', 1.0,
    '从已完工的签名锚点细部起手，实际的物理动作（机构行程、灯光亮起、使用者动作）在这一镜内发生')
_R_PULLBACK = Rung(
    'pullback', ('pull back shot',), '拉开 pull-back shot', 'a pull-back shot', 1.2,
    '镜头拉开，把签名锚点放回整个空间里，动作继续完成')
_R_FINAL_WIDE = Rung(
    'final_wide', ('final wide shot',), '终局远景 final wide shot', 'a final wide shot', 1.3,
    '稳定在略微收紧的终局远景上，画面等同结果 IMAGE，无工人无工具无材料，这一镜本身就是收尾欣赏')

# 施工梯：主镜 + 一到两个特写插入 + 切回主镜。三镜是下限（主镜/插入/切回，任何长度
# 下都不可裁），长片长多加一个特写插入而不是多加一级景别。
_CONSTRUCTION_LADDERS = {
    3: (_R_MAIN, _R_CLOSE, _R_RETURN),
    4: (_R_MAIN, _R_CLOSE, _R_XCLOSE, _R_RETURN),
}
_DEFAULT_CONSTRUCTION_LADDER = _CONSTRUCTION_LADDERS[4]

# ── 原片景别 → 镜头梯改写 ───────────────────────────────────────────────────
#
# 施工梯的主镜与切回镜此前写死是**远景**。复刻线上这就是一条凭空的改写：原片整拍拍在
# 中景或特写上时，切点表、逐镜职责、镜头名审计、定向回炉四处一致地要求写「wide working
# shot」，于是交付片把一条特写工序拍拉成了远景——而观测到的 shot_scale 只以一句劝导
# 文字下发（observed_craft_directive 的 SHOT），软的必然输给硬的。
#
# 这里按观测景别改写主镜/切回镜这一对（它们是同一个机位的两次出现，必须同步改，否则
# 「切回与主镜完全相同的机位与构图」当场自相矛盾）。key 一律不动：切点表、职责文案、
# 缺镜头审计、越界景别审计、兜底稿全部按 rung.key 取值，改 key 才会散架。
_SCALE_WORDS = {
    'extreme_wide': 'extreme wide',
    'wide': 'wide',
    'medium': 'medium',
    'close': 'close',
    'extreme_close': 'extreme close',
}
# 主镜已经很紧时，插入镜必须比主镜更紧——否则「插入」读不出插入，模型会把两镜写成
# 同一个画面，四镜梯当场塌成两镜。
_TIGHT_MAIN_SCALES = ('close', 'extreme_close')


def _article(phrase):
    return 'an' if phrase[:1].lower() in 'aeiou' else 'a'


def _rescaled(rung, phrase, label_prefix):
    """把一级镜头换成另一个景别的同一级镜头。key 一律不动。"""
    return rung._replace(variants=(phrase,), label=f'{label_prefix} {phrase}',
                         phrase=f'{_article(phrase)} {phrase}')


def apply_observed_scale(ladder, shot_scale, shot_scales=None):
    """按原片观测到的景别改写施工梯。

    两个入参是两个不同的读数，缺一不可：
      · shot_scale  —— 这一拍的**主景别**（原片这一拍出现最多的那个）。它决定主镜与
        切回镜。这两级必须同时改、且必须相同：切回镜的职责就是「切回与主镜完全相同的
        机位与构图」，也是这一拍首尾两张锚点 IMAGE 的景别。
      · shot_scales —— 这一拍**逐个镜头**的景别序列（reverse.attach_shot_scales）。它只
        决定中间那几个插入镜。原片在一拍里远/全/中/近/特怎么切，这里就怎么切。

    为什么首尾镜不跟着逐镜序列走：两拍之间的那张 IMAGE **同时属于两拍**（它既是上一拍的
    落点，也是下一拍的起点），景别只能有一个。让每拍的首尾镜都回到本拍主景别，这张共享
    锚点就有唯一解，首尾帧锚不松——中间镜照样在变，画面该有的丰富度在那里。

    shot_scale 为 None/'wide' 且没有序列时**原样返回**，非复刻线一个字节都不受影响。
    过门梯与兑现梯整段不参与：它们的三个工位是由职责定的（逼近/门槛/落定、细部/拉开/
    终局），景别是那份职责的一部分，按观测改写等于把过门拍改成不过门。
    """
    keys = {rung.key for rung in ladder}
    if 'main' not in keys or 'return' not in keys:
        return ladder
    main_word = _SCALE_WORDS.get(str(shot_scale or '').strip().lower())
    sequence = [str(x or '').strip().lower() for x in (shot_scales or [])]
    if (not main_word or main_word == 'wide') and not any(sequence):
        return ladder

    # 中间插入镜的景别按下标从序列里取。序列长度与梯子长度对不上时（原片切了六刀、
    # 梯子只排四镜，或反过来）掐掉首尾之后按下标取，取不到的那一级保持默认——宁可少改
    # 一级，也不能把第三镜的景别贴到第二镜上。
    middles = sequence[1:-1] if len(sequence) >= 3 else []
    tight_main = str(shot_scale or '').strip().lower() in _TIGHT_MAIN_SCALES

    out, middle_cursor = [], 0
    for rung in ladder:
        if rung.key == 'main' and main_word and main_word != 'wide':
            rung = _rescaled(rung, f'{main_word} working shot', '主镜')
        elif rung.key == 'return' and main_word and main_word != 'wide':
            phrase = f'returning {main_word} shot'
            rung = _rescaled(rung, phrase, '切回主镜')._replace(
                role=rung.role.replace('opening wide working shot',
                                       f'opening {main_word} working shot'))
        elif rung.key in ('close', 'xclose'):
            observed = _SCALE_WORDS.get(
                middles[middle_cursor] if middle_cursor < len(middles) else '')
            middle_cursor += 1
            # 插入镜与主镜同景别时保持默认的更紧一级：这一镜的职责是把工具接触点与
            # 材料物理放大，与主镜同框等于把两镜写成同一个画面，四镜梯当场塌成两镜。
            if observed and observed != main_word:
                rung = _rescaled(rung, f'{observed} insert',
                                 '特写插入' if rung.key == 'close' else '第二处特写插入')
            elif tight_main and rung.key == 'close':
                rung = _rescaled(rung, 'macro detail insert', '特写插入')
            elif tight_main:
                rung = _rescaled(rung, 'extreme macro insert', '第二处特写插入')
        out.append(rung)
    return tuple(out)


# 过门桥拍与兑现拍：三个自然工位，与时长无关（一次穿越就是逼近/门槛/落定，再切只是把
# 同一件事切碎）。它们免除节奏声明——traverse/reveal 不压缩劳动。
_TRAVERSAL_LADDER = (_R_APPROACH, _R_THRESHOLD, _R_ARRIVAL)
_REWARD_LADDER = (_R_DETAIL, _R_PULLBACK, _R_FINAL_WIDE)

# 时长 → 施工镜头数：短片长一个特写插入（三镜），长片长两个（四镜）。约束是主镜与切回镜
# 各 ≥1.3 秒、插入镜 ≥0.9 秒——插入本来就是短镜，读作插入；1 秒的**景别**才读作闪帧。
_SHOT_COUNT_BY_DURATION = {4: 3, 6: 3, 8: 4, 10: 4}
_TWO_INSERT_MIN_SECONDS = 7

_DURATION_WORDS = {4: 'four', 6: 'six', 8: 'eight', 10: 'ten'}
_COUNT_WORDS = {3: 'three', 4: 'four', 5: 'five', 6: 'six'}

# 兜底稿里每一级镜头的一句话职责（占位稿仍计入 fallback_count 门禁，但至少不违反镜头语法）。
_FALLBACK_SHOT_FRAGMENT = {
    'main': ('a wide working shot matching the first frame, with the worker already making '
             'effective tool contact and carrying the whole visible advance of this beat'),
    'close': 'a close-up insert on the tool contact',
    'xclose': 'an extreme close-up insert on the traces left behind',
    'return': ('a returning wide shot from the same camera setup, matching the last frame '
               'while visible work continues'),
    'approach': 'a wide approach shot matching the first frame',
    'threshold': 'a threshold shot at the opening itself as the door frame slides out of view',
    'arrival': 'an interior wide shot settling on the space beyond, matching the last frame',
    'detail': 'a detail shot on the finished signature anchor',
    'pullback': 'a pull-back shot opening out from it into the whole room',
    'final_wide': 'a final wide shot matching the last frame',
}


# ── 契约文案 ────────────────────────────────────────────────────────────────
# omni-multishot-language.md §Pacing Declaration：多镜头包里不能用 continuous，
# 那个词会被读成"拍一条一镜到底"。过门拍与最终兑现拍免除这句。
OMNI_PACING_PHRASE = (
    "edited construction time-lapse assembled from multiple camera setups, not real-time footage."
)
OMNI_PACING_MARKER = 'edited construction time-lapse assembled from multiple camera setups'

# base 的 _EVEN_RATE_PHRASE 在这里的替代品。原句把**推进量**和**画面运动**混成一件事：
# 它要求"每一刻都有可见推进"，而镜头级进度锁恰恰规定远景/全景推进量为零、特写不产生
# 推进量、结果远景在剪辑点做 same-way 压缩。这句把连续性约束到镜内，把压缩限定在切点上。
OMNI_INSHOT_PHRASE = (
    "Inside every shot the frame keeps moving from its first to its last moment — handheld "
    "drift, ambient motion, and the subject's own action never freeze — while this beat's "
    "change advances only during the work shots. The only compressions in the clip fall "
    "exactly on the listed cut marks; no shot contains a hold, a stall, or a deferred step "
    "that is then delivered all at once."
)
OMNI_INSHOT_MARKER = 'the only compressions in the clip fall exactly on the listed cut marks'

# 本 composer 自己产出的违规项前缀。
# ERROR = 结构性硬伤（split_structural_video_errors 靠它认出该回炉的那一类）；
# STYLE = 只留痕不回炉（记号类瑕疵回炉一轮也未必修得掉，还要多烧一次调用）。
OMNI_VIDEO_ERROR_PREFIX = 'OMNI VIDEO CONTRACT: '
OMNI_VIDEO_STYLE_PREFIX = 'OMNI VIDEO STYLE: '
# IMAGE 侧同样只留痕：IMAGE 走的是 base 的合成链路，回炉会把 base 的 IMAGE 契约
# 一起重跑，代价远大于一处记号瑕疵。
OMNI_IMAGE_STYLE_PREFIX = 'OMNI IMAGE STYLE: '

# base 专属、在 omni 下不再成立的校验项。只按精确文案过滤，不做模糊匹配——否则会
# 顺手吃掉真正的瑕疵。
_BASE_ONLY_ERROR_SNIPPETS = (
    # omni 有自己的节奏声明（OMNI_PACING_PHRASE）
    "VIDEO missing pacing control 'continuous construction time-lapse",
    # omni 有自己的镜内连续性声明（OMNI_INSHOT_PHRASE），见上面的对撞说明
    "VIDEO missing the even-rate clause",
)

# 用户明确要院线感/商业感时，才关掉 UGC 手机拍摄的默认档
# （omni-scene-skeleton.md §1 "Optional cinematic terms, only when useful or requested"）。
# 判据本体搬到了 pp.wants_cinematic_style —— Phase 1 的 IMAGE 1 要用同一套口径（见
# OmniComposer.wants_cinematic）。这里保留别名，旧的模块级引用不必跟着改。
_CINEMATIC_REQUEST_PATTERN = pp._CINEMATIC_REQUEST_PATTERN

_ONE_TAKE_PATTERNS = (
    (re.compile(r'\bone takes?\b'), 'one-take'),
    (re.compile(r'\boners?\b'), 'oner'),
    (re.compile(r'\bone shot\b'), 'one-shot'),
    (re.compile(r'\bsingle shot\b'), 'single-shot'),
    (re.compile(r'\b(?:one|single) (?:continuous|unbroken) (?:take|shot)\b'), 'single continuous take'),
    (re.compile(r'\bsingle take\b'), 'single take'),
    (re.compile(r'\b(?:unbroken|continuous) take\b'), 'continuous take'),
)

# 一镜到底措辞的确定性改写。按顺序套用在**原文**上（因此模式要同时容忍连字符与空格）。
_ONE_TAKE_SUBSTITUTIONS = (
    # base 兜底稿里的整句："One unbroken take at a steady speed: no cut, no fade, ..."
    # 一句里既宣告一镜到底又禁止剪辑点，改词改不干净，整句删。
    (re.compile(r'(?:(?<=^)|(?<=[.!?]))\s*[^.!?]*\bone\s+unbroken\s+take\b[^.!?]*[.!?]', re.I), ' '),
    (re.compile(r'\bin\s+one\s+continuous\s+(?:shot|take)\b', re.I), 'across the cut shot cycle'),
    (re.compile(r'\bone\s+continuous\s+coaxial\s+move\b', re.I),
     'a coaxial push carried across consecutive shots'),
    (re.compile(r'\b(?:a|one|single)\s+(?:single\s+)?(?:unbroken|continuous)\s+take\b', re.I),
     'an edited multi-shot sequence'),
    (re.compile(r'\b(?:unbroken|continuous)\s+take\b', re.I), 'edited multi-shot sequence'),
    (re.compile(r'\bone[\s\-]+takes?\b', re.I), 'edited multi-shot sequence'),
    (re.compile(r'\boners?\b', re.I), 'edited multi-shot sequence'),
    (re.compile(r'\b(?:one|single)\s+continuous\s+shot\b', re.I), 'edited multi-shot sequence'),
    (re.compile(r'\b(?:one|single)[\s\-]+shots?\b', re.I), 'cut coverage'),
    (re.compile(r'\bsingle[\s\-]+take\b', re.I), 'edited multi-shot sequence'),
)

# 时间线句。非贪婪抓到第一个 "seconds."——句子内部有小数点，按句号切会把它切碎。
_TIMELINE_RE = re.compile(r'\bCut this\b[^\n]*?\bseconds\.', re.IGNORECASE)

# 记号禁用（omni-output-templates.md §Notation Ban）的确定性修复：一到二十的独立整数
# 折成英文单词。IMAGE 编号与时间线句里的秒数不在此列（前者是锚点引用，后者是本次新增的
# Timecode exemption）。
_SMALL_INTEGER_WORDS = {
    1: 'one', 2: 'two', 3: 'three', 4: 'four', 5: 'five', 6: 'six', 7: 'seven',
    8: 'eight', 9: 'nine', 10: 'ten', 11: 'eleven', 12: 'twelve', 13: 'thirteen',
    14: 'fourteen', 15: 'fifteen', 16: 'sixteen', 17: 'seventeen', 18: 'eighteen',
    19: 'nineteen', 20: 'twenty',
}
_TENS_WORDS = {
    2: 'twenty', 3: 'thirty', 4: 'forty', 5: 'fifty',
    6: 'sixty', 7: 'seventy', 8: 'eighty', 9: 'ninety',
}
# 三位数才够覆盖画高比例（`45 percent of frame height`）。此前只认两位，于是二十以上
# 的计数与**全部**比例数字都从确定性改写里漏了过去，只在 _stray_digits 里留一条记号
# 瑕疵——记号禁用因此在二十以上的数字上形同虚设。
_DIGIT_COUNT_RE = re.compile(r'(?<![\d.])\b(\d{1,3})\b(?=\s+[A-Za-z])')


def _integer_to_words(n):
    """0-100 的整数折成英文单词；超出范围返回 None（不硬改，交给门禁报）。

    21-99 写成 `forty-five` 这种带连字符的形态是**有意的**：base 的
    _PERCENT_NEAR_PATTERN 正是按 `(?:forty)(?:[-\\s](?:five))?[-\\s]?percent` 认的，
    _parse_percent_token 也接受词形。所以把比例数字拼写出来之后，SCUP 的
    check_anchor_scale_lock / check_worker_scale_lock 依然解析得到同一个数——
    记号禁用与漂移门禁不必二选一。改成别的写法（`forty five`、`45`）会让其中一边失效。"""
    if n in _SMALL_INTEGER_WORDS:
        return _SMALL_INTEGER_WORDS[n]
    if n == 0:
        return 'zero'
    if n == 100:
        return 'one hundred'
    if 21 <= n <= 99:
        tens, unit = divmod(n, 10)
        word = _TENS_WORDS[tens]
        return word if unit == 0 else f"{word}-{_SMALL_INTEGER_WORDS[unit]}"
    return None


def _normalized(text):
    """判定用的归一化文本：小写、连字符/下划线/多空格一律折成单空格。
    'close-up' / 'close up' / 'closeup' 是同一个词，写法差异不该变成违规。"""
    low = (text or '').lower().replace('closeup', 'close up')
    return re.sub(r'[\s\-–—_]+', ' ', low)


def _one_take_hits(text):
    """文本里出现的一镜到底措辞（去重，保持声明顺序）。"""
    low = _normalized(text)
    return [label for pattern, label in _ONE_TAKE_PATTERNS if pattern.search(low)]


# ── 梯的选取与切点分配 ──────────────────────────────────────────────────────

def construction_shot_count(duration):
    """这个时长排几个施工镜头：短片长三镜（一个特写插入），长片长四镜（两个）。

    表外时长按同一条分界（≥7 秒才排得下第二个插入）判，只会返回 3 或 4——景别轮换梯
    时代那种"时长越长镜头越多"的线性反推已经作废，多出来的时间归主镜。"""
    try:
        seconds = int(round(float(duration)))
    except (TypeError, ValueError):
        seconds = server_common.OMNI_DEFAULT_VIDEO_DURATION
    if seconds in _SHOT_COUNT_BY_DURATION:
        return _SHOT_COUNT_BY_DURATION[seconds]
    return 4 if seconds >= _TWO_INSERT_MIN_SECONDS else 3


def is_expanded_transition_stage_beat(beat):
    """这一拍是不是 expand_spatial_transition_beats 展开出来的原子级过门/空间重置子拍。

    2026-08-06：这类子拍每个只装得下一个镜头动作，不该被套任何镜头梯（既不该被要求
    写出整套 traversal 3 镜，也不该落回 construction 默认梯）。ladder_kind 与
    omni_video_violations 的调用方都要认这同一个信号，否则镜头梯选型改对了、违规检查
    那边的默认兜底又会把同一个要求悄悄塞回来。

    'camera_reframe' 排除在外：那是同一个展开函数为长内景每三拍插的纯运镜换角度拍
    （operation == 'reframe'，不是过门），跟 prompt_pipeline.beat_is_crossing_clip
    判定"是不是过门跨越镜头"时排除它是同一个理由。"""
    return bool(isinstance(beat, dict)
                and beat.get('transition_stage') not in (None, '', 'none', 'camera_reframe'))


def ladder_kind(beat=None, is_threshold_or_reveal=None, is_crossing=False):
    """这一拍走哪套镜头梯：'construction' / 'traversal' / 'reward'。

    返回 None 表示**拍型不明**（回炉通路里只拿到一段文本），或本拍不该套任何镜头梯。
    此时调用方只做与拍型无关的清洗，不注入时间线——猜错等于给一段穿门镜头硬塞一张
    施工切点表。

    transition_stage 豁免：expand_spatial_transition_beats 会把规划期唯一的
    bridge_stage=1 过门标记展开成 3~5 个原子级子拍（见 prompt_pipeline __init__.py），
    每个子拍只装得下一个镜头动作。展开后的子拍仍带着 operation == 'threshold'，如果
    照旧走 'traversal' 分支，就是要求每个子拍单独交付「逼近远景+门槛+落定」整套 3
    镜——不可能完成，首稿必炸。只有还没被展开、仍是规划期原始标记的整段过门拍
    （bridge_stage/hard_cut 但没有 transition_stage）才需要整套 traversal 镜头梯。"""
    if beat:
        if is_expanded_transition_stage_beat(beat):
            return None
        operation = str(beat.get('operation') or '').strip().lower()
        if operation == 'reward':
            return 'reward'
        if operation == 'threshold' or beat.get('bridge_stage') or is_crossing:
            return 'traversal'
        return 'construction'
    if is_threshold_or_reveal is None:
        return None
    if not is_threshold_or_reveal:
        return 'construction'
    return 'traversal' if is_crossing else 'reward'


def ladder_for(duration, kind='construction'):
    """(时长, 拍型) → 镜头梯。"""
    if kind == 'traversal':
        return _TRAVERSAL_LADDER
    if kind == 'reward':
        return _REWARD_LADDER
    return _CONSTRUCTION_LADDERS[construction_shot_count(duration)]


def shot_marks(duration, ladder):
    """按权重把时长分给每一镜。返回 [(start, end, rung), ...]。

    切点由**累计权重**算出（而不是逐镜相加后取整），避免 0.05 级的取整误差逐镜累积；
    末镜的 end 直接写成时长本身，把余数一次吸收掉。"""
    seconds = float(duration)
    total = sum(r.weight for r in ladder) or 1.0
    marks, accumulated, cursor = [], 0.0, 0.0
    for index, rung in enumerate(ladder):
        accumulated += rung.weight
        if index == len(ladder) - 1:
            end = seconds
        else:
            end = max(round(seconds * accumulated / total, 1), round(cursor + 0.1, 1))
        marks.append((round(cursor, 1), round(end, 1), rung))
        cursor = end
    return marks


def timeline_sentence(duration, ladder):
    """时间线句：本条片子唯一允许出现阿拉伯数字的地方。"""
    marks = shot_marks(duration, ladder)
    segments = [f"{rung.phrase} from {start:.1f} to {end:.1f}" for start, end, rung in marks]
    if len(segments) > 1:
        body = ', '.join(segments[:-1]) + f", and {segments[-1]}"
    else:
        body = segments[0]
    seconds = int(round(float(duration)))
    word = _DURATION_WORDS.get(seconds, str(seconds))
    return (f"Cut this {word}-second clip on these marks and hold no other cuts — "
            f"{body} seconds.")


def ladder_roles(ladder, insert_subject=None):
    """逐镜职责文案。只有一个特写插入时，第二个插入的职责（持久痕迹）并进它——否则
    "至少两处本次操作特有的持久痕迹"会随着那一镜一起消失，那才是真正的内容损失。

    insert_subject（复刻线）：原片这一拍自己的插入镜拍的是什么。给了就钉在第一个插入镜
    上——通用职责（工具接触点 / 持久痕迹）是这条片子里**任何一拍**都能写的话，而这一句
    是**这一拍**的画面。没给（原创单，或原片这一拍本来就是一镜到底）时逐字不变。"""
    keys = {rung.key for rung in ladder}
    subject = str(insert_subject or '').strip()
    lines = []
    for index, rung in enumerate(ladder, start=1):
        role = rung.role
        if rung.key == 'close' and 'xclose' not in keys:
            role = role + ('；本片长只有这一个插入镜，因此至少两处本次操作特有的持久痕迹'
                           '也在同一镜里给到')
        if rung.key == 'close' and subject:
            role = role + (f'；**本拍原片的插入镜拍的就是：{subject}** —— 这一镜要拍的是它，'
                           f'不是一个泛泛的工具接触点')
        lines.append(f"{index}. {rung.phrase}（{rung.label.split()[0]}）——{role}；")
    return '\n'.join(lines)


# 确定性注入的结构句合计约 130 词：锚定开场句由 fix_video_opening 补（44）、切点表
# （30~55，随镜头数变）、节奏声明（13）、镜内连续性声明（62）。预算表按这个数拉开。
_STRUCTURAL_INJECTION_WORDS = 130


def video_word_targets(shot_count):
    """整条 VIDEO 的 (目标字数, 硬顶)，**含**下面确定性注入的那约 130 词结构句。"""
    return 55 * shot_count + 175, 55 * shot_count + 235


def video_draft_budget(shot_count):
    """模型初稿的预压缩预算：目标字数减去还没注入的结构句，再留 40 词余量。

    这里修的是一处旧账：此前的 460 是**硬顶**，却被 fix 链路当成预压缩预算用，压完再
    追加节奏句/音效句，结果必然超顶且不再复裁。反过来把硬顶直接减 130 也不行——那会
    让预算低于目标字数，一份**完全合规**的初稿照样被裁，而 _local_trim_to_budget 丢的
    是中间整句，也就是恰好丢掉中景/近景/特写这几个唯一携带推进量的镜头。
    所以预算必须 ≥「目标字数 − 结构句」，硬顶必须 ≥「预算 + 结构句」。"""
    target, _ceiling = video_word_targets(shot_count)
    return target - _STRUCTURAL_INJECTION_WORDS + 40


def _missing_shot_rungs(text, ladder=None):
    """镜头梯里缺失（或顺序不对）的级别。全部按出现顺序前向扫描。"""
    ladder = ladder or _DEFAULT_CONSTRUCTION_LADDER
    low = _normalized(text)
    missing = []
    cursor = 0
    for rung in ladder:
        hit = -1
        for variant in rung.variants:
            found = low.find(variant, cursor)
            if found != -1 and (hit == -1 or found < hit):
                hit = found
        if hit == -1:
            missing.append(rung.label)
        else:
            cursor = hit + 1
    return missing


_ALL_RUNGS = (_R_MAIN, _R_CLOSE, _R_XCLOSE, _R_RETURN,
              _R_ESTABLISHING, _R_FULL, _R_MEDIUM, _R_OUTRO,
              _R_APPROACH, _R_THRESHOLD, _R_ARRIVAL, _R_DETAIL, _R_PULLBACK, _R_FINAL_WIDE)


def _extra_shot_rungs(text, ladder):
    """正文里出现了**不属于本梯**的景别。

    2026-08-09 之后这条比缺镜头更要紧：模型的训练先验和本技能自己的旧稿都在写景别轮换梯
    （establishing long / full / medium / wide outro），而现在的施工梯只有主镜 + 插入 +
    切回。多写出来的景别既会把每镜压到一秒以下（闪帧），又会在一段本该同机位收尾的片子里
    换掉机位，首尾帧锚跟着一起松掉。"""
    low = _normalized(text)
    in_ladder = {rung.key for rung in ladder}
    extras = []
    for rung in _ALL_RUNGS:
        if rung.key in in_ladder:
            continue
        if any(variant in low for variant in rung.variants):
            extras.append(rung.label)
    return extras


def _body_without_timeline(text):
    """去掉切点表之后的正文。镜头梯审计**必须**在这上面做：切点表本身就按顺序列出了
    每一级镜头名，拿它去过镜头轮换检查等于自证——正文一个镜头都没写也能通过。"""
    return re.sub(r'\s{2,}', ' ', _TIMELINE_RE.sub(' ', text or '')).strip()


# 一个数字记号：可选小数部分 + 紧贴其后的字母（单位）。必须把单位一起吃进来再判断，
# 不能靠 `\d+(?:\.\d+)?(?![A-Za-z])` 这种"后面不许跟字母"的否定预查——正则会回溯：
# "14mm" 先试 "14"（后面是 m，预查失败），退成 "1"（后面是 4，不是字母，预查通过），
# 于是照样报出一个根本不存在的残留数字 "1"。实测 35/35 条真实 IMAGE 提示词都因为
# camera_dna 里的 "14mm"/"18mm" 被判违规（2026-08-06）。
_NUMBER_TOKEN_RE = re.compile(r'(?<![A-Za-z0-9.])(\d+(?:\.\d+)*)([A-Za-z]*)')


def _bare_numbers(probe):
    """probe 里没有紧贴单位的阿拉伯数字。贴单位的（14mm / 1.6m）按 _digits_to_words
    的口径豁免——它刻意不折这类写法，检查器必须放行同一批，否则修复器认定合规的文本
    会被检查器原样打回，形成无解的回炉死循环。"""
    return sorted({m.group(1) for m in _NUMBER_TOKEN_RE.finditer(probe or '') if not m.group(2)})


def _stray_digits(text):
    """时间线句与 IMAGE 编号之外的阿拉伯数字（记号禁用的残留）。"""
    probe = _TIMELINE_RE.sub(' ', text or '')
    probe = re.sub(r'\bimage\s+\d+', ' ', probe, flags=re.IGNORECASE)
    return _bare_numbers(probe)


# Grid 单元格里的那位数字（`Grid B2` 的 2）不算记号违规——omni 的记号禁用确实把 Grid
# 记法也列为违规，但 base 的 primary-landmark-restatement / anchor-scale-lock 要求 IMAGE
# 按 packet 逐字重述 Grid 单元格，那是 SCUP 漂移门禁唯一的解析锚点。这条冲突登记在
# contract-registry.json 的 omni-grid-notation-ban（enforcer 为 null），此处只是不把它
# 重复报成"数字违规"，免得真正可修的数字被淹掉。
_GRID_CELL_RE = re.compile(r'\bgrid\s+[a-z]\d\b', re.IGNORECASE)


def _stray_digits_image(text):
    """IMAGE 正文里的阿拉伯数字，扣除 IMAGE 编号、Grid 单元格与贴单位数字（14mm /
    1.6m —— 与 _stray_digits 同理，_digits_to_words 刻意不碰这类写法，检查器必须
    对齐同一条放行规则，否则 camera_dna 里正常的 "camera height 1.6m" 会被每拍打回）。"""
    probe = _GRID_CELL_RE.sub(' ', text or '')
    probe = re.sub(r'\bimage\s+\d+', ' ', probe, flags=re.IGNORECASE)
    return _bare_numbers(probe)


def omni_image_violations(image_prompt):
    """IMAGE 正文对 omni 记号禁用的违规项。空列表 = 合规。

    此前记号禁用只在 VIDEO 上有门禁（_stray_digits），IMAGE 一侧既不改写也不校验，
    而 base 的锚点重述句恰恰会往 IMAGE 里写 `holding 45 percent of frame height`——
    契约在文档里写着"适用于 prompt bodies"，实现上却有一半没人管。"""
    stray = _stray_digits_image(image_prompt)
    if not stray:
        return []
    return [
        OMNI_IMAGE_STYLE_PREFIX
        + "IMAGE 正文出现阿拉伯数字（" + ', '.join(stray)
        + "）——记号禁用同样适用于 IMAGE，计数与画高比例一律写成英文单词"
    ]


def omni_video_violations(video_prompt, ladder=None, duration=None, skip_shot_list=False):
    """VIDEO 正文对 omni 镜头语法的违规项。空列表 = 合规。

    ladder 缺省按长片长四镜施工梯判（模块级调用方的口径）。duration 给了才查
    时间线——回炉通路拿不到拍型时不该凭空要求一张切点表。
    节奏声明不在这里查：它由 OmniComposer.ensure_pacing 确定性注入，查了也只会是死代码。

    skip_shot_list（2026-08-06）：调用方已经用 is_expanded_transition_stage_beat 判定
    这一拍是展开后的原子级过门/空间重置子拍、传了 ladder=None 时才该置 True——这类拍
    根本不该套任何镜头梯。不加这个开关的话，下面 `ladder or _DEFAULT_CONSTRUCTION_LADDER`
    这行会在 ladder=None 时悄悄换成四镜施工梯，一样保证首稿必炸，只是换了个错误的
    期望镜头梯而已。"""
    if skip_shot_list:
        missing = extras = []
        ladder = ladder or _DEFAULT_CONSTRUCTION_LADDER
    else:
        ladder = ladder or _DEFAULT_CONSTRUCTION_LADDER
        body = _body_without_timeline(video_prompt)
        expected = ' / '.join(rung.label.split()[0] for rung in ladder)
        missing = _missing_shot_rungs(body, ladder)
        extras = _extra_shot_rungs(body, ladder)
    errors = []

    if missing:
        errors.append(
            OMNI_VIDEO_ERROR_PREFIX
            + "VIDEO is not an edited multi-shot sequence — missing (or out of order): "
            + '、'.join(missing)
            + f"。必须按 {expected} 的顺序写成 {len(ladder)} 个镜头，镜头之间用 clean cut / match cut 衔接"
        )

    if extras:
        errors.append(
            OMNI_VIDEO_ERROR_PREFIX
            + "VIDEO 写了本片长排不下的额外景别（" + '、'.join(extras)
            + f"）——本条片子只有 {len(ladder)} 个镜头：{expected}。"
            + "多出来的镜头会把每镜压到一秒以下，观感是闪帧；被裁掉那一级的职责并进相邻镜头，"
            + "不是另起一镜"
        )

    hits = _one_take_hits(video_prompt)
    if hits:
        errors.append(
            OMNI_VIDEO_ERROR_PREFIX
            + "VIDEO uses banned one-take wording (" + ', '.join(hits)
            + ") — omni 的多镜头契约没有例外，包括过门拍与最终兑现拍"
        )

    stray = _stray_digits(video_prompt)
    if stray:
        errors.append(
            OMNI_VIDEO_STYLE_PREFIX
            + "正文出现阿拉伯数字（" + ', '.join(stray)
            + "）——必须使用纯自然语言，计数一律写成英文单词。"
            + "IMAGE 编号（锚点引用）与紧贴单位的数字（14mm / 1.6m）不在此列，"
            + "报出来的这几个不含那两类"
        )

    return errors


class OmniComposer(BaseComposer):
    """Gemini Omni 的 Phase 2：VIDEO 走弹性镜头梯 + 时间线切点，其余一切沿用 base。"""

    profile = 'omni'

    # 下面四项是**可按 profile 换皮**的确定性注入文案。镜头梯机制（切点表、一镜到底禁令、
    # 镜头名审计）对所有多镜头 profile 是同一套，但节奏声明的措辞随题材而变——微缩线的
    # 施工不是 construction time-lapse 而是 craft time-lapse。marker 是**已注入判定**用的
    # 小写子串，必须是 phrase 的真子串，否则 ensure_pacing 会每过一次就再追加一句。
    pacing_phrase = OMNI_PACING_PHRASE
    pacing_marker = OMNI_PACING_MARKER
    inshot_phrase = OMNI_INSHOT_PHRASE
    inshot_marker = OMNI_INSHOT_MARKER
    # 镜头语法定向回炉时读的那份契约（子类换成自己包里的同类文档）。
    multishot_reference = 'omni-multishot-language.md'

    def __init__(self):
        super().__init__()
        self._reference_cache = {}

    # ── references ──────────────────────────────────────────────────────────

    def reference(self, name):
        """读一个契约文件（本次运行内缓存）。路径解析全在 load_reference_file 里，
        omni 包缺的文件由它回落到 base。"""
        if name not in self._reference_cache:
            self._reference_cache[name] = pp.load_reference_file(name, self.profile)
        return self._reference_cache[name]

    def required_references_block(self, include_threshold=False):
        """SKILL.md 声明的每次必读 references，拼成一段可直接进 system prompt 的文本。
        批量直出时这段只发一次（每拍共享），这正是批量通路存在的意义。"""
        names = list(OMNI_ALWAYS_LOAD_REFERENCES)
        if include_threshold:
            names.append(OMNI_THRESHOLD_REFERENCE)
        parts = []
        for name in names:
            body = self.reference(name)
            if not body:
                continue
            parts.append(f"---------- {name} ----------\n{body}")
        if not parts:
            # 契约整段读空时不静默降级：镜头梯仍由下面的 override 正文与审计兜住，
            # 但这件事必须在日志里看得见（load_reference_file 只按文件名提示一次）。
            if sys.stdout:
                print("[WARN] omni composer 一个必读契约都没读到，VIDEO 只能靠内置的镜头"
                      "规则兜底；检查 skills/gemini-omni-restoration-composer/references/")
            return ''
        return '\n\n'.join(parts)

    # ── 时长与镜头梯 ────────────────────────────────────────────────────────

    def clip_duration(self):
        """本单单段视频的时长（秒）。时间线把切点钉在秒上，所以这个数必须与生成端
        送给 Flow 面板的那个一致——两边都走 server_common.resolve_video_duration。"""
        return server_common.resolve_video_duration(self.config)

    def ladder_for_kind(self, duration, kind='construction', observed_shots=None,
                        observed_scale=None, observed_scales=None):
        """(时长, 拍型) → 镜头梯。**类内所有取梯的地方都必须走这里，且必须把
        observed_shots 一起传**——不要直接调模块级的 ladder_for。子 profile 可能有自己的
        一套镜头名与拍型映射（见 miniature），而复刻单的梯还随原片切点变；漏掉任何一处，
        取梯与审计就会各用一套梯：注入的是四镜切点表、审计要的是三镜，每一拍都判违规、
        每一拍都烧一轮回炉，报出来的还是「缺镜头」，看不出真因在取梯。

        observed_shots（复刻线）：原片这一拍由几个镜头组成。给了就按它排施工梯——原片
        切得碎（≥3 镜）排四镜，切得少或没切排三镜下限；给 None（原创单、老 job、二创
        变体、抽帧异常）时按片长排，与改造前完全一致。过门梯与兑现梯不受影响：它们的
        三个工位是由职责定的，原片多切几刀也只是把同一件事切碎。"""
        if kind == 'construction' and observed_shots is not None:
            # 夹进合法区间 [3, 4] 之后，再按片长压一次上限：4/6 秒排不下第二个
            # 插入镜（construction_shot_count 的分界），硬排等于每镜不足一秒的闪帧。
            capped = min(max(observed_shots, min(pp._MULTISHOT_LEGAL_SHOT_COUNTS)),
                         construction_shot_count(duration))
            return apply_observed_scale(_CONSTRUCTION_LADDERS[capped], observed_scale,
                                        shot_scales=observed_scales)
        if kind == 'construction':
            return apply_observed_scale(ladder_for(duration, kind), observed_scale,
                                        shot_scales=observed_scales)
        return ladder_for(duration, kind)

    def ladder_for_beat(self, beat=None, is_threshold_or_reveal=None, is_crossing=None):
        """这一拍的镜头梯。拍型不明时返回 None（见 ladder_kind）。"""
        if is_crossing is None:
            is_crossing = bool(beat) and bool(pp.beat_is_crossing_clip(beat))
        kind = ladder_kind(beat, is_threshold_or_reveal, is_crossing)
        if kind is None:
            return None
        return self.ladder_for_kind(self.clip_duration(), kind,
                                    observed_shots=pp.observed_shot_count_of(beat),
                                    observed_scale=pp.observed_shot_scale_of(beat),
                                    observed_scales=pp.observed_shot_scale_sequence_of(beat))

    # ── 风格分支 ────────────────────────────────────────────────────────────

    def wants_cinematic(self):
        """用户是否明确要了院线感/商业感。默认 False = 走 UGC 手机拍摄的真实感。

        判据本体在 pp.wants_cinematic_style：Phase 1 的 IMAGE 1（模块级、profile 无关的
        代码）要用同一套口径给首帧补拍摄质感子句，不能反向 import 本包。"""
        state = self.state or {}
        return pp.wants_cinematic_style(state.get('parsed_brief') or {}, state.get('theme', ''))

    def capture_style_rule(self):
        if self.wants_cinematic():
            return (
                "- CAPTURE STYLE: the brief explicitly asked for a cinematic/commercial finish, so "
                "the optional cinematic vocabulary is allowed (film-stock look, shallow depth of "
                "field, deliberate push-ins, rack focus). Keep the cut shot structure and the "
                "one-take ban regardless — a polished look is still cut coverage, never a oner."
            )
        return (
            "- CAPTURE STYLE (default, no cinematic finish was requested): every shot reads as "
            "casual UGC phone footage of a real worksite — recorded on a recent smartphone rear "
            "camera in real available light, with slight overexposure and small blown highlights "
            "near windows/lamps/sky, compression artifacts and sensor noise in the darker corners, "
            "mild wide-angle edge distortion, unsteady handheld framing with a few degrees of tilt, "
            "small off-centre composition, and brief autofocus breathing that resolves. Two to four "
            "such capture artifacts per prompt. Do NOT write polished studio/cinematic lighting, "
            "colour grading, or empty quality words. Landmarks stay locked even though the framing "
            "is loose: no primary landmark may leave the frame in the opening, staging, or closing shots."
        )

    def ensure_pacing(self, video_prompt):
        """普通施工拍补齐节奏声明与镜内连续性声明（缺了才补，不重复注入）。

        文案取自类属性（pacing_phrase / inshot_phrase），子 profile 换皮即可——注入点、
        判定口径与「过门拍/兑现拍免除」的分流仍然只有这一处实现。"""
        text = video_prompt or ''
        if self.pacing_marker not in text.lower():
            if text and not text.endswith(('.', '!', '?')):
                text += '.'
            text = f"{text} {self.pacing_phrase}".strip()
        if self.inshot_marker not in text.lower():
            if text and not text.endswith(('.', '!', '?')):
                text += '.'
            text = f"{text} {self.inshot_phrase}".strip()
        return text

    def fallback_ladder_clause(self, ladder):
        """占位兜底稿补的镜头梯声明。默认是 omni 的 UGC 手机质感版本。"""
        return fallback_ladder_clause(ladder)

    def ensure_actor_engagement(self, video_prompt, ladder, packet=None, beat=None,
                                is_threshold_or_reveal=False):
        """确保正文里的施工主体从零秒起就在作业面上（并清掉进出场措辞）。

        默认是 omni 的实景工人口径。世界观不同的子 profile（微缩线的施工主体是从画幅
        边缘伸入的巨人手，进出画本身就是契约要求）必须覆写这一处，否则会被塞进一句
        "the same lone worker is already positioned at the active work face"。"""
        return ensure_ladder_out_and_in(
            video_prompt, ladder, packet=packet, beat=beat,
            is_threshold_or_reveal=is_threshold_or_reveal)

    def video_override_block(self, include_threshold=False, ladder=None, insert_subject=None):
        """追加在 base system prompt 之后的 OMNI VIDEO OVERRIDE 段。

        只覆盖 VIDEO：上面那份 base 契约里关于 IMAGE 的每一条（干净帧、无人称词、
        里程碑骨架、痕迹、包络覆盖……）继续照旧生效，一个字都不重复。

        ladder 缺省用本单时长下的施工梯——批量直出的这一段是**每拍共享**的，而同一批
        里可能混着过门拍与兑现拍，它们各自的切点表由 _inject_timeline 逐拍确定性覆写，
        这里只需要把三套梯的规则讲清楚。"""
        duration = self.clip_duration()
        ladder = ladder or self.ladder_for_kind(duration, 'construction')
        target, ceiling = video_word_targets(len(ladder))
        references = self.required_references_block(include_threshold=include_threshold)
        references_section = (
            f"\n==================== OMNI REQUIRED REFERENCES ====================\n{references}"
            if references else '')
        return f"""

==================== OMNI VIDEO OVERRIDE (读到这里为止的 VIDEO 规则以本段为准) ====================
本次输出的目标模型是 Gemini Omni，单段片长 {duration} 秒。上面所有关于 IMAGE 的规则**继续完全
生效，不做任何修改**；唯独 VIDEO 的镜头语法整体改写为下面这套，与上文冲突处一律以本段为准。

MANDATORY SHOT STRUCTURE — 每一条普通施工 VIDEO 都是**一条贯穿全段的主工作镜，中间被
{'两个' if len(ladder) == 4 else '一个'}特写插入切开，最后切回同一机位收尾**。不是景别轮换：不要写
establishing long shot / full shot / medium shot / wide outro shot 这一类旧梯的景别名，
一个都不要。{duration} 秒对应 {len(ladder)} 个镜头，按这个顺序写满，镜头之间用 clean cut / match cut
衔接（禁止 cross-dissolve、fade、magical transition、instant transformation、teleport、
跳过物理过程的快剪）：
{ladder_roles(ladder, insert_subject)}

SAME CAMERA SETUP（本次改造的核心）——第一镜与最后一镜是**同一个机位、同一个构图、同一个焦段**，
只有施工完成度不同。正文在最后一镜里要写明它切回的是 the same camera setup as the opening wide
working shot。插入镜是从这个机位切进去的细部，切回来时完成度必须与切走那一刻一致。

CINEMATIC NARRATIVE FLOW (纯自然语言多镜头因果流)——严禁使用任何机械时间戳或数字切点表。正文必须使用流畅的电影分镜叙事连词（例如 "The sequence opens with...", "Cutting in closer to a close-up insert...", "An extreme close-up insert reveals...", "Cutting back to a returning wide shot from the same camera setup..."）来自然串联各个镜头。正文所有计数和尺寸一律写成英文单词（three roof beams，不是 3 roof beams；ten seconds，不是 10s）。

拍型分流：过门桥拍走 逼近远景 / 门槛 / 落定室内远景 三镜，最终兑现拍走 细部 / 拉开 / 终局远景
三镜，两者都免除下面的节奏声明（它们是穿越与揭示，不压缩劳动），但**同样不许写成一镜到底**。

ONE-TAKE BAN（无例外，过门拍与最终兑现拍也一样）：禁止写 oner、one-shot、one-take、
single continuous take、one continuous take、single take、unbroken take 或任何等义措辞。
推镜、揭示、穿门这些动作是**镜头内部的运动**，不是"一条不间断的长镜头"。
{self.capture_style_rule()}
- PACING DECLARATION：普通施工拍在正文里声明一次时间基准，用这句原话——
  "{OMNI_PACING_PHRASE}"（过门拍与最终兑现拍免除这句）。不要用 continuous 描述整条片段的拍法。
- IN-SHOT CONTINUITY：上文那条 EVEN RATE 指令（"每一刻都在推进 / 不许把改动推迟后一次兑现"）
  在多镜头包里**作废**，改用这句原话——
  "{OMNI_INSHOT_PHRASE}"
  理由：推进量全部集中在主镜，特写插入按契约不产生新的推进量，而切回镜恰恰是在剪辑点上
  做 same-way 压缩。要求"每一刻都在推进"等于要求模型违反自己的镜头级进度锁。
- PROGRESS ACROSS CUTS：每个镜头都从上一镜结束时的完成度开始；剪辑点只允许压缩"已经完整演示过一次"的重复动作，
  且必须在正文里说明（例如 after the remaining boards come loose the same way），不得跳过某类改动的第一次发生、
  不得凭空出现新物件、不得在剪辑点上让数量变化。
- PHRASING VARIATION：镜头梯是固定骨架，因此逐拍复读是本技能的头号失败模式。锚定开场句、
  镜头名、工人造型短语、节奏声明这几项**必须逐字保留**；除此之外，相邻两拍的句式模板、镜头内的从句顺序、
  动词选择、转场措辞、形容词搭配都必须换过。
- 长度：整条 VIDEO 目标 {target} 词上下，硬顶 {ceiling} 词。主镜与切回镜各 60–90 词
  （它们承载起始状态、推进过程与结果状态），每个特写插入 30–50 词。
{references_section}"""

    # ── 覆写钩子 ────────────────────────────────────────────────────────────

    def batch_system_prompt(self, config, packet, scup_ref, tbcp_ref):
        include_threshold = bool(tbcp_ref)
        return (super().batch_system_prompt(config, packet, scup_ref, tbcp_ref)
                + self.video_override_block(include_threshold=include_threshold))

    def single_beat_system_prompt(self, config, i, contract, packet, compiled_images,
                                  compiled_videos, scup_ref, tbcp_ref_i):
        is_crossing = bool(contract.get('is_bridge') or contract.get('is_cut'))
        ladder = self.ladder_for_beat(
            contract.get('beat'), contract.get('is_threshold_or_reveal'), is_crossing=is_crossing)
        return (super().single_beat_system_prompt(
            config, i, contract, packet, compiled_images, compiled_videos, scup_ref, tbcp_ref_i)
            + self.video_override_block(
                include_threshold=is_crossing, ladder=ladder,
                insert_subject=(contract.get('beat') or {}).get('insert_subject')))

    def apply_proactive_fixes(self, i, video_prompt, image_prompt, packet, mode, is_last,
                              is_threshold_or_reveal, beat=None, config=None, family=None,
                              beat_ladder=None):
        """IMAGE 完全委托 base（下游帧渲染吃的是同一套契约）；VIDEO 走 omni 自己的链路。

        VIDEO 不能借道 base：base 会把正文压到 270 词（多镜头文本会被腰斩）、注入含
        continuous 的节奏声明（在多镜头包里等于叫模型去拍一镜到底）、并塞进按 8 秒写死
        的工人进出时间戳。"""
        _discarded_video, fixed_image = pp.apply_proactive_fixes(
            i, video_prompt, image_prompt, packet, mode, is_last, is_threshold_or_reveal,
            beat=beat, config=config, family=family, beat_ladder=beat_ladder)
        # 记号禁用适用于两侧的 prompt body，不只 VIDEO。base 的锚点重述句会写进
        # `holding 45 percent of frame height`，词形化之后 SCUP 的比例门禁照样解析
        # 得到同一个数（_integer_to_words 的连字符形态就是为此选的），所以这里可以
        # 直接折数字，不必给 IMAGE 开一个例外。
        fixed_image = _digits_to_words(fixed_image)
        fixed_video = self.fix_omni_video(
            i, video_prompt, packet, is_threshold_or_reveal, beat=beat, config=config, family=family)
        # 末帧的镜面地收窄对 VIDEO 同样成立，但 omni 的 VIDEO 不走 base（见上），所以这里
        # 单独补一次：IMAGE 不再要求镜面地时，VIDEO 也不该留着「倒环氧做镜面」那道工序。
        if is_last and pp.ladder_gloss_floor_milestone(beat_ladder, i) == '':
            fixed_video = pp.strip_unearned_gloss_floor(fixed_video)
        return fixed_video, fixed_image

    def validate_beat_prompts(self, i, video_prompt, image_prompt, packet, mode, is_last,
                              is_threshold_or_reveal, prev_video=None, prev_image=None,
                              beat=None, family=None, is_pre_bridge=False,
                              is_post_reveal_cleanup=False):
        ladder = self.ladder_for_beat(beat, is_threshold_or_reveal)
        # 字数硬顶按本 profile 的镜头梯算，不用 base 的一镜到底档 380
        # （见 pp.validate_beat_prompts 的 video_word_limit 说明）。拍型不明时按施工梯。
        _ceiling_ladder = ladder or self.ladder_for_kind(self.clip_duration(), 'construction')
        errs = super().validate_beat_prompts(
            i, video_prompt, image_prompt, packet, mode, is_last, is_threshold_or_reveal,
            prev_video, prev_image, beat=beat, family=family, is_pre_bridge=is_pre_bridge,
            is_post_reveal_cleanup=is_post_reveal_cleanup,
            video_word_limit=video_word_targets(len(_ceiling_ladder))[1])
        errs = [e for e in (errs or [])
                if not any(snippet in e for snippet in _BASE_ONLY_ERROR_SNIPPETS)]
        return (errs
                + omni_video_violations(
                    video_prompt, ladder=ladder,
                    duration=self.clip_duration() if ladder else None,
                    skip_shot_list=is_expanded_transition_stage_beat(beat))
                + omni_image_violations(image_prompt))

    def split_structural_video_errors(self, errs):
        """omni 的镜头语法违规算结构性硬伤：镜头梯缺失时 Omni 会退回一条平淡的长镜头，
        和"VIDEO 无动作正文"一样属于 i2v 无画面可拍的那一类，必须回炉而不是仅留痕。
        记号类瑕疵（OMNI_VIDEO_STYLE_PREFIX）不在此列，自动落进 remainder 只留痕。"""
        structural, rest = super().split_structural_video_errors(errs)
        omni_errs = [e for e in rest if e.startswith(OMNI_VIDEO_ERROR_PREFIX)]
        remainder = [e for e in rest if not e.startswith(OMNI_VIDEO_ERROR_PREFIX)]
        return structural + omni_errs, remainder

    def rework_structural_video_beat(self, config, i, video_prompt, structural_errs, packet, beat=None):
        """先让 base 处理它自己那些硬伤，再对 omni 的镜头语法违规回炉一轮。"""
        omni_errs = [e for e in (structural_errs or []) if e.startswith(OMNI_VIDEO_ERROR_PREFIX)]
        base_errs = [e for e in (structural_errs or []) if not e.startswith(OMNI_VIDEO_ERROR_PREFIX)]
        ladder = self.ladder_for_beat(beat) if beat else None

        reworked = None
        if base_errs:
            video_prompt, reworked = super().rework_structural_video_beat(
                config, i, video_prompt, base_errs, packet, beat=beat)
            # base 的重写稿同样要过 omni 的镜头语法（它是照一镜到底的契约写的）。
            video_prompt = self.normalize_omni_video(video_prompt, beat=beat)

        residual = omni_video_violations(
            video_prompt, ladder=ladder,
            duration=self.clip_duration() if ladder else None,
            skip_shot_list=is_expanded_transition_stage_beat(beat))
        if omni_errs or [e for e in residual if e.startswith(OMNI_VIDEO_ERROR_PREFIX)]:
            video_prompt, omni_reworked = self.rework_omni_multishot(
                config, i, video_prompt, packet, beat=beat)
            reworked = omni_reworked if reworked is None else (reworked or omni_reworked)
        return video_prompt, reworked

    def normalize_reworked_video(self, video_prompt, beat=None):
        """里程碑成对回炉是照一镜到底骨架写的：先洗掉 one-take 措辞、补回切点表与节奏
        声明，再交回上游复验——否则每修一次里程碑就把镜头梯拆一次。"""
        return self.normalize_omni_video(video_prompt, beat=beat)

    def video_profile_violations(self, video_prompt, beat=None):
        """omni 的镜头语法硬伤（记号类瑕疵不算）。"""
        ladder = self.ladder_for_beat(beat) if beat else None
        residual = omni_video_violations(
            video_prompt, ladder=ladder,
            duration=self.clip_duration() if ladder else None,
            skip_shot_list=is_expanded_transition_stage_beat(beat))
        return [e for e in residual if e.startswith(OMNI_VIDEO_ERROR_PREFIX)]

    def finalize_fallback_video(self, video_prompt, contract):
        """占位符兜底稿：base 的兜底文案是照一镜到底写的（"One unbroken take..."、
        "one continuous coaxial move"），在 omni 下必须先清干净，再补一句镜头梯声明。"""
        is_crossing = bool(contract.get('is_bridge') or contract.get('is_cut'))
        beat = contract.get('beat')
        is_threshold_or_reveal = contract.get('is_threshold_or_reveal')
        text = self.normalize_omni_video(
            video_prompt, is_threshold_or_reveal=is_threshold_or_reveal,
            beat=beat, is_crossing=is_crossing)
        ladder = self.ladder_for_beat(beat, is_threshold_or_reveal, is_crossing=is_crossing)
        if ladder and _missing_shot_rungs(_body_without_timeline(text), ladder):
            if not text.endswith(('.', '!', '?')):
                text += '.'
            text = f"{text} {self.fallback_ladder_clause(ladder)}"
        return text

    # ── omni 自己的 VIDEO 处理 ───────────────────────────────────────────────

    def fix_omni_video(self, i, video_prompt, packet, is_threshold_or_reveal,
                       beat=None, config=None, family=None):
        """omni 版的 VIDEO 确定性修复链。与 base 的差异有四处：字数预算按镜头数缩放
        并在注入后复裁一次、节奏声明换成多镜头版、时间线切点确定性注入、不走 base 的
        out-and-in 兜底（那句会塞进按 8 秒写死的时间戳与 Grid 记号，见模块 docstring）。"""
        ladder = self.ladder_for_beat(beat, is_threshold_or_reveal)
        shot_count = len(ladder or _DEFAULT_CONSTRUCTION_LADDER)
        _target, ceiling = video_word_targets(shot_count)
        text = pp.clean_prompt_text(video_prompt)
        # 预压缩显式给下面的结构句注入让出余量，注入完再按硬顶复裁（见 video_draft_budget）。
        text = pp.compress_prompt_to_budget(text, video_draft_budget(shot_count), config,
                                            is_video=True)
        text = pp.fix_video_opening(i, text, profile='omni')
        text = pp.fix_sound_design(text, family=family or 'exterior')
        text = self.ensure_actor_engagement(text, ladder, packet=packet, beat=beat,
                                            is_threshold_or_reveal=is_threshold_or_reveal)
        text = self.normalize_omni_video(
            text, is_threshold_or_reveal=is_threshold_or_reveal, beat=beat)
        return pp.compress_prompt_to_budget(text, ceiling, config, is_video=True)

    def normalize_omni_video(self, video_prompt, is_threshold_or_reveal=None, beat=None,
                             is_crossing=None):
        """确定性归一：清一镜到底措辞、清 base 的 even-rate 句、折数字；拍型已知时再注入
        时间线，普通施工拍额外补齐节奏声明与镜内连续性声明。

        拍型未知（回炉通路只拿到一段文本）时只做前三步：过门拍/兑现拍本来就免除节奏声明，
        切点表也完全不同，猜错等于给它硬塞一份违约文案。"""
        text = strip_one_take_language(video_prompt)
        text = _strip_base_even_rate(text)
        text = _digits_to_words(text)

        if is_crossing is None:
            is_crossing = bool(beat) and bool(pp.beat_is_crossing_clip(beat))
        kind = ladder_kind(beat, is_threshold_or_reveal, is_crossing)
        if kind is None:
            return text

        duration = self.clip_duration()
        # observed_shots 必须跟着传：切点表是在这里确定性注入的，而镜头名审计走的是
        # ladder_for_beat。两边取梯的口径一旦不一致，注入的是四镜切点表、审计要的是
        # 三镜，每一拍都必然判违规并烧掉一轮定向回炉——而且报的是「缺镜头」，
        # 看不出真因在取梯。
        ladder = self.ladder_for_kind(duration, kind,
                                      observed_shots=pp.observed_shot_count_of(beat),
                                      observed_scale=pp.observed_shot_scale_of(beat),
                                      observed_scales=pp.observed_shot_scale_sequence_of(beat))
        text = _inject_timeline(text)
        if kind == 'construction':
            text = self.ensure_pacing(text)
        return text

    def multishot_rework_system(self, ladder, duration):
        """镜头语法定向回炉用的 system prompt。子 profile 覆写它来换掉世界观措辞
        （施工主体、镜头名、读哪份镜头语法契约），回炉的调用/复验流程不必复制第二份。"""
        multishot_ref = self.reference(self.multishot_reference)
        scales = ', '.join(rung.phrase for rung in ladder)
        return f"""You are rewriting ONE video prompt so it obeys the Gemini Omni multi-shot contract.

{multishot_ref}

Rewrite rules (additive — do not lose content):
- Keep the opening anchor sentence ("Use the provided image as the exact starting composition and environment anchor. ...") VERBATIM as the first sentence.
- Express the multi-shot sequence using pure natural language cinematic transitions (e.g. 'The sequence opens with...', 'Cutting in closer to...', 'An extreme close-up reveals...', 'Cutting back to...'). Do NOT output numeric timestamps, seconds marks, or robotic cut mark tables.
- Keep every concrete detail already in the draft: the same single worker and costume, the same tool, the same operation, the same persistent traces, the same audio description, the same lighting progression. Redistribute them across the shots instead of inventing new ones.
- Restructure the body into exactly {len(ladder)} shots IN THIS ORDER, naming each one in prose exactly as written here: {scales}. Join them with clean cuts or match cuts.
- This is NOT a shot-scale rotation. Do not write "establishing long shot", "full shot", "medium shot", or "wide outro shot" anywhere — those names belong to the retired grammar and count as a contract violation.
- The first and the last shot are the SAME camera setup, framing, and focal length, differing only in how far the work has progressed; say so explicitly in the last shot ("the same camera setup as the opening wide working shot"). The insert(s) cut into that setup and cut back at the same completion level.
- The worker is already at the active work face in the opening wide working shot and makes the first effective tool contact from the opening instant; that shot carries this beat's whole visible advance up to roughly three quarters; the insert(s) carry tool contact, material physics, and the persistent traces without advancing the state; the returning wide shot compresses the remaining repetitions the same way and reaches the beat's resulting state while visible work continues. Never show or describe a worker entrance or exit.
- Preserve the visible stage-milestone skeleton VERBATIM in meaning: the declared visible start state, the declared resulting state, both declared progress lines (primary and secondary material/stock), the first effective tool contact at the opening moment, the material source/container and the movement path, and repeated work cycles. Use the words "repeated"/"repeatedly"/"cycle by cycle"/"course by course" literally — "repetitions" alone does not read as repeated cycles.
- All numbers and counts must be written in English words. Never include arabic digits.
- NEVER write oner, one-shot, one-take, single continuous take, one continuous take, single take, or unbroken take — there is no exemption.
- Output ONLY the rewritten video prompt body. No headings, no labels, no commentary."""

    def rework_omni_multishot(self, config, i, video_prompt, packet, beat=None):
        """镜头语法定向回炉一轮：只重写 VIDEO，把正文改写成纯自然语言多镜头序列。

        与 base 的结构性回炉同款契约——加法式修改、锚定开场句逐字保留、重写稿必须真的
        通过 omni_video_violations 复验，否则保留原稿（只留痕）。返回
        (video_prompt, 是否采用重写稿)。"""
        duration = self.clip_duration()
        ladder = self.ladder_for_beat(beat) or self.ladder_for_kind(duration, 'construction')
        system = self.multishot_rework_system(ladder, duration)
        user = f"Beat {i} video prompt draft to restructure:\n\n{video_prompt}"

        try:
            resp = pp._chat(config, system, user, temperature=0.7, timeout=90)
        except pp.GenerationCancelled:
            raise
        except Exception as e:
            if sys.stdout:
                print(f"[OMNI] Beat {i} 多镜头回炉调用失败，保留原稿: {e}")
            return video_prompt, False

        candidate = pp._strip_leading_label_line((resp or '').strip())
        if not candidate:
            return video_prompt, False
        candidate = pp.fix_video_opening(i, candidate, profile='omni')
        candidate = self.normalize_omni_video(candidate, beat=beat)
        residual = omni_video_violations(
            candidate, ladder=ladder, duration=duration if beat else None,
            skip_shot_list=is_expanded_transition_stage_beat(beat))
        if [e for e in residual if e.startswith(OMNI_VIDEO_ERROR_PREFIX)]:
            if sys.stdout:
                print(f"[OMNI] Beat {i} 多镜头回炉稿复验未通过，保留原稿（仅留痕）")
            return video_prompt, False
        if beat:
            before = set(pp.check_milestone_video_prompt(video_prompt, beat))
            introduced = [e for e in pp.check_milestone_video_prompt(candidate, beat)
                          if e not in before]
            if introduced:
                if sys.stdout:
                    print(f"[OMNI] Beat {i} 多镜头回炉稿洗掉了里程碑骨架，保留原稿: {introduced}")
                return video_prompt, False
        return candidate, True


def ensure_ladder_out_and_in(video_prompt, ladder, packet=None, beat=None,
                             is_threshold_or_reveal=False):
    """Compatibility-named Omni fixer for direct work from the first instant.

    The worker is already at the active work face from the opening instant. Any old entrance/exit
    language is removed and the clip uses every shot for the operation through the outro.
    """
    text = video_prompt or ''
    if is_threshold_or_reveal or not ladder or ladder in (_TRAVERSAL_LADDER, _REWARD_LADDER):
        return text
    low = text.lower()
    sterile_phrases = ('sterile of workers', 'sterile of active workers', 'sterile of any human',
                       'no workers', 'no human presence', 'completely sterile of', 'without any human')
    if any(p in low for p in sterile_phrases):
        return text
    if not any(re.search(rf'\b{w}s?\b', low) for w in ('worker', 'crew', 'person', 'builder', 'laborer')):
        return text
    agent = r'(?:the\s+)?(?:same\s+)?(?:one\s+lone\s+)?(?:workers?|crew|persons?|builders?|laborers?)'
    text = re.sub(
        rf'(?i)\b(?:in\s+the\s+(?:opening|wide\s+working|returning\s+wide)\s+shot|at\s+(?:zero\s+seconds?|the\s+(?:start|beginning)))[,;:]?\s*'
        rf'{agent}[^.;]*?\b(?:enters?|walks?\s+in|steps?\s+in)(?:[^.;]*[.;])?', '', text)
    text = re.sub(
        rf'(?i)(?:,?\s*(?:and|then)?\s*)?{agent}[^.;]*?\b(?:exits?|walks?\s+out|steps?\s+out|leaves?\s+the\s+(?:frame|scene))(?:[^.;]*[.;])?',
        '', text)
    text = re.sub(r'\s{2,}', ' ', text).strip()
    low = text.lower()
    if ('zero seconds' in low or 't=0' in low or 'opening frame' in low or 'opening instant' in low) and any(
            p in low for p in ('already at', 'already positioned', 'first effective tool contact')):
        return text
    costume = pp._worker_costume_from_packet(packet)
    if text and not text.rstrip().endswith(('.', '!', '?')):
        text = text.rstrip() + '.'
    clause = (
        f" In the opening frame the same lone worker{costume} is already positioned at the active "
        "work face and makes the first effective tool contact immediately; every following shot "
        "continues that visible operation through the last shot of the clip."
    )
    return (text.rstrip() + clause).strip()


def fallback_ladder_clause(ladder):
    """占位兜底稿补的镜头梯声明：一句话里按顺序带齐每一级镜头名与 UGC 拍摄质感，
    让占位稿至少不违反 omni 的镜头语法（占位稿本身仍计入 fallback_count 门禁）。"""
    fragments = []
    for rung in ladder:
        fragment = _FALLBACK_SHOT_FRAGMENT[rung.key]
        if rung.key == 'main':
            fragment = (f'{rung.phrase} matching the first frame, with the worker already making '
                        f'effective tool contact and carrying the whole visible advance of this beat')
        elif rung.key == 'return':
            fragment = (f'{rung.phrase} from the same camera setup, matching the last frame '
                        f'while visible work continues')
        fragments.append(fragment)
    joined = ', '.join(fragments[:-1]) + f", and {fragments[-1]}"
    count = _COUNT_WORDS.get(len(ladder), str(len(ladder)))
    return (f"The clip is cut as {count} shots in order — {joined} — joined by clean cuts and "
            f"recorded like casual smartphone footage in available light, with slight "
            f"overexposure near the bright sources, compression noise in the shadows, and "
            f"unsteady handheld framing.")


def strip_one_take_language(video_prompt):
    """确定性清除一镜到底措辞：先按词改写，仍然命中的整句丢弃。"""
    text = video_prompt or ''
    for pattern, replacement in _ONE_TAKE_SUBSTITUTIONS:
        text = pattern.sub(replacement, text)
    if _one_take_hits(text):
        kept = [s for s in re.split(r'(?<=[.!?])\s+', text) if s.strip() and not _one_take_hits(s)]
        text = ' '.join(kept)
    return re.sub(r'\s{2,}', ' ', text).strip()


def _strip_base_even_rate(text):
    """清掉 base 的 even-rate 句。它与镜头级进度锁直接对撞，见 OMNI_INSHOT_PHRASE。"""
    if pp._EVEN_RATE_MARKER not in (text or '').lower():
        return text
    kept = [s for s in re.split(r'(?<=[.!?])\s+', text or '')
            if s.strip() and pp._EVEN_RATE_MARKER not in s.lower()]
    return re.sub(r'\s{2,}', ' ', ' '.join(kept)).strip()


def _digits_to_words(text):
    """一到一百的独立整数折成英文单词（纯自然语言记号禁用的确定性修复）。

    两处不动：IMAGE 编号（锚点引用）以及紧贴单位的数字（14mm / 1.6m）。"""
    source = text or ''
    source = _TIMELINE_RE.sub(' ', source)

    def replace(match):
        prefix = source[max(0, match.start() - 6):match.start()].lower()
        if prefix.endswith('image '):
            return match.group(0)
        return _integer_to_words(int(match.group(1))) or match.group(0)

    res = _DIGIT_COUNT_RE.sub(replace, source)
    return re.sub(r'\s{2,}', ' ', res).strip()


def _inject_timeline(text, sentence=None):
    """在纯自然语言体系下，清除任何机械时间线句（Cut this...）。"""
    body = _TIMELINE_RE.sub(' ', text or '')
    return re.sub(r'\s{2,}', ' ', body).strip()
