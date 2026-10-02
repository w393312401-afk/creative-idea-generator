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

  2. **切点用自然分镜叙事表达**。每个镜头边界实际写出 clean cut / match cut，
     不输出数字切点表。旧时间线函数只保留兼容入口，归一时删除机械时间线句。

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
    'omni-work-first-rhythm.md',
    'omni-restoration-continuity.md',
    'omni-beat-skeleton.md',
    'omni-damage-vocabulary.md',
    'omni-lighting-environment-audio.md',
    'omni-output-templates.md',
)

# 条件加载：真实过门，或同空间无施工换机位（SKILL.md 的 Load conditionally）。
OMNI_THRESHOLD_REFERENCE = 'omni-threshold-bridge.md'


# ── 镜头梯 ──────────────────────────────────────────────────────────────────
# variants 一律写成 _normalized() 之后的形态（小写、无连字符、单空格），判定按**出现
# 顺序**做，不只是"都出现过"——乱序的几个词不是轮换，是把词堆在一句里骗过检查。
# weight 是时长分配权重：主镜要装下"起始状态 + 第一次动作完整可见 + 重复循环"，配额必须最高。
Rung = namedtuple('Rung', 'key variants label phrase weight role')

_R_MAIN = Rung(
    'main', (
        'wide working shot', 'opening wide working shot', 'opening wide shot',
        'wide work shot', 'working wide shot', 'wide staging shot', 'wide shot',
        'master shot', 'wide action shot'
    ), '主镜 wide working shot',
    'a wide working shot', 1.5,
    '贯穿本段的主工作镜，画面开在起始 IMAGE 上、工人已经在作业面，0 秒立即发生第一次'
    '有效工具接触；第一次动作完整可见后转入重复循环，本拍改动在这一镜内推进到约四分之三，'
    '全程用 -ing / partially / growing 这类进行态措辞，不出现完成态描述')
_R_CLOSE = Rung(
    'close', (
        'close up', 'close up insert', 'macro insert', 'macro detail insert',
        'macro shot', 'tight insert', 'detail insert', 'insert shot',
        'medium close up', 'tight close up', 'first close up insert', 'first insert'
    ), '特写插入 close-up insert', 'a close-up insert', 1.0,
    '从主镜切进来的特写插入：工具接触点与材料物理（形变、碎屑、粉尘、纤维、飞溅），'
    '不产生新的推进量，切回主镜时完成度与切走那一刻一致')
_R_XCLOSE = Rung(
    'xclose', (
        'extreme close up', 'extreme close up insert', 'extreme macro insert',
        'second close up insert', 'second insert', 'extreme detail insert',
        'macro detail', 'extreme close', 'second macro insert'
    ), '第二处特写插入 extreme close-up insert',
    'an extreme close-up insert', 0.9,
    '第二个插入镜，至少两处本次操作特有的持久痕迹与微观质感，同样不产生推进量')
_R_RETURN = Rung(
    'return', (
        'returning wide shot', 'return to wide shot', 'returns to wide shot',
        'returning to the wide shot', 'returning wide working shot',
        'cutting back to wide shot', 'cut back to wide shot',
        'cutting back to the opening wide shot', 'cutting back to the same wide shot',
        'cutting back to the wide shot', 'returns to the wide shot',
        'returns to the opening wide shot', 'returning to wide shot',
        'final wide working shot', 'returning wide', 'returns to wide',
        'returns to the same wide shot', 'cutting back to wide',
        'cut back to wide', 'returning to the same camera setup',
        'cutting back to the same camera setup'
    ), '切回主镜 returning wide shot',
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
    'approach', ('wide approach shot', 'approach shot', 'exterior approach shot', 'wide approach'),
    '逼近远景 wide approach shot',
    'a wide approach shot', 1.0,
    '画面等同起始 IMAGE，镜头在开口外侧逼近，开口与两处被窥见的室内地标已可辨，全程零施工')
_R_THRESHOLD = Rung(
    'threshold', ('threshold shot', 'portal shot', 'entryway shot', 'crossing shot'),
    '门槛 threshold shot', 'a threshold shot', 1.1,
    '推进到门槛处，门框在画面里滑出，曝光与白平衡开始从室外滚向室内，被窥见的地标占比放大')
_R_ARRIVAL = Rung(
    'arrival', ('interior wide shot', 'arrival shot', 'interior settling shot', 'settling wide shot'),
    '落定 interior wide shot',
    'an interior wide shot', 1.2,
    '完全落定在室内，画面精确等同结果 IMAGE，被窥见的地标已成为室内主地标，无工人无工具')

_R_DETAIL = Rung(
    'detail', ('detail shot', 'signature detail shot', 'anchor detail shot', 'close up detail'),
    '细部 detail shot', 'a detail shot', 1.0,
    '从已完工的签名锚点细部起手，实际的物理动作（机构行程、灯光亮起、使用者动作）在这一镜内发生')
_R_PULLBACK = Rung(
    'pullback', ('pull back shot', 'pullback shot', 'pulling back shot', 'pulling back'),
    '拉开 pull-back shot', 'a pull-back shot', 1.2,
    '镜头拉开，把签名锚点放回整个空间里，动作继续完成')
_R_FINAL_WIDE = Rung(
    'final_wide', ('final wide shot', 'final wide', 'closing wide shot', 'finishing wide shot', 'concluding wide shot', 'final wide reveal'),
    '终局远景 final wide shot', 'a final wide shot', 1.3,
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
    variants = [phrase]
    if rung.key == 'return':
        for pfx in ('returning ', 'return to ', 'returns to ', 'cutting back to ', 'cut back to '):
            variants.append(phrase.replace('returning ', pfx))
    elif rung.key == 'main':
        variants.append(phrase.replace(' working shot', ' work shot'))
        variants.append(phrase.replace(' working shot', ' shot'))
    elif rung.key in ('close', 'xclose'):
        variants.extend(['close up', 'macro insert', 'detail insert', 'extreme close up', 'insert'])
    return rung._replace(variants=tuple(dict.fromkeys(variants)), label=f'{label_prefix} {phrase}',
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

# 展开后的过门子拍只完成自己的阶段，不能每拍重新走完整的逼近/门槛/落定。
# 硬件拍首尾相同机位，空间移动拍的首尾则分别来自两张不同位置的锚点。
_TRANSITION_HARDWARE_LADDER = (
    _R_MAIN._replace(role='从本拍起始 IMAGE 的入口机位开场，只开合原有硬件或移开已存在的入口杂物；不安装新件、不推进施工'),
    _R_CLOSE._replace(role='clean cut 到同一入口部件的接触细节；部件状态与切走时相同，插入不新增开合或空间推进'),
    _R_RETURN._replace(role='clean cut 返回与第一镜相同的机位、构图与焦段，完成本拍入口动作，精确匹配本拍结果 IMAGE；不跨过入口、不执行后续阶段'),
)
_R_TRANSITION_WORK = Rung(
    'transition_work', ('transition working shot', 'transition work shot'),
    '阶段主镜 transition working shot', 'a transition working shot', 1.4,
    '精确从本拍起始 IMAGE 的相机位置与朝向开场，连续显示本 transition_stage 的下降、转向、无施工换机位或局部揭示；只完成本阶段，不提前完成下一阶段')
_R_TRANSITION_DETAIL = Rung(
    'transition_detail', ('detail insert', 'transition detail insert'),
    '方向证据 detail insert', 'a detail insert', 1.0,
    'clean cut 到已经可见的门槛、梯档、舱肋或接缝方向证据；相机的空间位置与移动完成度不在插入期间推进，不增加新地标或施工')
_R_TRANSITION_LAND = Rung(
    'transition_land', ('landing shot', 'transition landing shot'),
    '阶段落点 landing shot', 'a landing shot', 1.3,
    'clean cut 继续刚才的空间移动或揭示，在本拍结果 IMAGE 的位置与朝向落定；不回到起始机位，不跳过尚未演示的穿越、下降或转向')
_TRANSITION_STAGE_LADDER = (_R_TRANSITION_WORK, _R_TRANSITION_DETAIL, _R_TRANSITION_LAND)
_HARDWARE_TRANSITION_STAGES = frozenset(('door_hardware_open', 'hatch_hardware_open', 'divider_open'))
_R_SINGLE_TAKE = Rung(
    'single_take', ('single take', 'one take', 'single shot', 'one shot'),
    '单镜 single take', 'a single take', 1.0,
    '用户明确指定的单镜覆盖：从起始 IMAGE 连续执行实际动作到结果 IMAGE，不插入剪辑、不给默认多镜检查冒充通过')
_SINGLE_TAKE_LADDER = (_R_SINGLE_TAKE,)

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
    "only at the described cuts; no shot contains a hold, a stall, or a deferred step "
    "that is then delivered all at once."
)
OMNI_INSHOT_MARKER = 'the only compressions in the clip fall only at the described cuts'

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
# 折成英文单词。IMAGE 编号作为锚点引用保留，旧时间线在折词前直接删除。
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

    子拍不套整套 traversal 或施工梯，使用只覆盖该阶段的三镜语法。

    'camera_reframe' 排除在外：那是同一个展开函数为长内景每三拍插的纯运镜换角度拍
    （operation == 'reframe'，不是过门），跟 prompt_pipeline.beat_is_crossing_clip
    判定"是不是过门跨越镜头"时排除它是同一个理由。"""
    return bool(isinstance(beat, dict)
                and beat.get('transition_stage') not in (None, '', 'none', 'camera_reframe'))


def ladder_kind(beat=None, is_threshold_or_reveal=None, is_crossing=False):
    """拍型分流；原子过门阶段有自己的三镜梯，未知拍型返回 None。"""
    if beat:
        operation = str(beat.get('operation') or '').strip().lower()
        if operation == 'reframe' or beat.get('transition_stage') == 'camera_reframe':
            # 纯换机位不算跨门，也不能误套要求施工和返回原机位的施工梯。
            return 'transition_stage'
        if is_expanded_transition_stage_beat(beat):
            return ('transition_hardware' if beat.get('transition_stage') in _HARDWARE_TRANSITION_STAGES
                    else 'transition_stage')
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
    if kind == 'single_take':
        return _SINGLE_TAKE_LADDER
    if kind == 'transition_hardware':
        return _TRANSITION_HARDWARE_LADDER
    if kind == 'transition_stage':
        return _TRANSITION_STAGE_LADDER
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
    """旧版兼容工具；当前生成不调用它，归一阶段会删除这种数字时间线。"""
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
        if rung.key == 'close' and 'xclose' not in keys and ladder != _TRANSITION_HARDWARE_LADDER:
            role = role + ('；本片长只有这一个插入镜，因此至少两处本次操作特有的持久痕迹'
                           '也在同一镜里给到')
        if rung.key == 'close' and subject:
            role = role + (f'；**本拍原片的插入镜拍的就是：{subject}** —— 这一镜要拍的是它，'
                           f'不是一个泛泛的工具接触点')
        lines.append(f"{index}. {rung.phrase}（{rung.label.split()[0]}）——{role}；")
    return '\n'.join(lines)


# 压缩预留的结构句预算；包括锚定开场、节奏和镜内连续性句，不再包含时间线。
_STRUCTURAL_INJECTION_WORDS = 130


def video_word_targets(shot_count):
    """整条 VIDEO 的 (目标字数, 硬顶)，**含**下面确定性注入的那约 130 词结构句。"""
    shot_count = max(3, shot_count)  # 用户单镜不扩大三镜的四百词额度。
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
              _R_APPROACH, _R_THRESHOLD, _R_ARRIVAL, _R_DETAIL, _R_PULLBACK, _R_FINAL_WIDE,
              _R_TRANSITION_WORK, _R_TRANSITION_DETAIL, _R_TRANSITION_LAND)


def _extra_shot_rungs(text, ladder):
    """正文里出现了**不属于本梯**的景别。

    2026-08-09 之后这条比缺镜头更要紧：模型的训练先验和本技能自己的旧稿都在写景别轮换梯
    （establishing long / full / medium / wide outro），而现在的施工梯只有主镜 + 插入 +
    切回。多写出来的景别既会把每镜压到一秒以下（闪帧），又会在一段本该同机位收尾的片子里
    换掉机位，首尾帧锚跟着一起松掉。"""
    low = _normalized(text)
    in_ladder = {rung.key for rung in ladder}
    # 空间/兑现的落点可用full-shot等词说明已选末镜的构图，不等于另开一镜。
    # 只豁免末镜内明确的落帧描述；出现额外cut仍按新增景别报错。
    _ordered_low, spans = _ordered_rung_spans(text, ladder)
    landing_end = (spans[-1][1] if len(spans) == len(ladder)
                   and spans[-1][2].key in ('transition_land', 'arrival', 'final_wide') else None)

    def landing_framing(start):
        if landing_end is None or start < landing_end:
            return False
        preceding = low[landing_end:start]
        clause = re.split(r'[.!?;]', preceding)[-1]
        return (not _CUT_RE.search(preceding) and bool(re.search(
            r'\b(?:settle(?:s)?|finish(?:es)?|end(?:s)?|land(?:s)?)\s+'
            r'(?:in|into|on)\s+(?:(?:the|a|an)\s+)?(?:[a-z]+\s+){0,3}$', clause)))

    extras = []
    for rung in _ALL_RUNGS:
        if rung.key in in_ladder:
            continue
        allowed_variants = [variant for allowed in ladder for variant in allowed.variants]
        if any(not any(variant in allowed for allowed in allowed_variants)
               and any(not landing_framing(match.start()) for match in re.finditer(
                   r'\b' + re.escape(variant) + r'\b', low))
               for variant in rung.variants):
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


def omni_image_violations(image_prompt, word_limit=None):
    """IMAGE 正文对 omni 记号禁用的违规项。空列表 = 合规。

    此前记号禁用只在 VIDEO 上有门禁（_stray_digits），IMAGE 一侧既不改写也不校验，
    而 base 的锚点重述句恰恰会往 IMAGE 里写 `holding 45 percent of frame height`——
    契约在文档里写着"适用于 prompt bodies"，实现上却有一半没人管。"""
    errors = []
    if word_limit is not None and len((image_prompt or '').split()) > word_limit:
        errors.append(f"IMAGE prompt word count ({len(image_prompt.split())}) exceeds limit of {word_limit} words")
    stray = _stray_digits_image(image_prompt)
    if stray:
        errors.append(
            OMNI_IMAGE_STYLE_PREFIX
            + "IMAGE 正文出现阿拉伯数字（" + ', '.join(stray)
            + "）——记号禁用同样适用于 IMAGE，计数与画高比例一律写成英文单词")
    return errors


def _ordered_rung_spans(text, ladder):
    """实际正文中的镜头位置；不把尾部概括或旧时间线当作实际分镜。"""
    low = _normalized(_body_without_timeline(text))
    cursor, spans = 0, []
    for rung in ladder:
        matches = [(low.find(v, cursor), v) for v in rung.variants]
        matches = [(start, variant) for start, variant in matches if start >= 0]
        if not matches:
            return low, []
        start, variant = min(matches, key=lambda item: (item[0], -len(item[1])))
        spans.append((start, start + len(variant), rung))
        cursor = start + len(variant)
    return low, spans


_CUT_RE = re.compile(
    r'\b(?:clean|match)\s+cuts?\b'
    r'|\bcut(?:s|ting)?\s+(?:to|into|in|back|away)\b'
    r'|\bcut\s+(?:is|becomes|returns?|enters?|reveals?|moves?|leads?|drops?)\b')
_NEGATED_CUT_RE = re.compile(r'\b(?:no|without|never|not|avoid(?:ing)?|forbid(?:den)?)\s+(?:(?:a|any|the)\s+)?(?:clean\s+|match\s+)?cuts?\b')


def _shot_structure_errors(text, ladder):
    """有限文本门禁：实际剪辑边界、可拍描述，以及施工返回机位的显式连续性。

    这不代替画面或语义审查；它防止镜头名列表与自相矛盾的返回机位冒充通过。
    """
    low, spans = _ordered_rung_spans(text, ladder)
    if len(spans) != len(ladder):
        return []  # 缺镜头的错误由主门禁报告，避免重复噪声。
    errors = []
    missing_cuts = []
    for index in range(1, len(spans)):
        previous_end, current_start = spans[index - 1][1], spans[index][0]
        boundary = _NEGATED_CUT_RE.sub(' ', low[previous_end:current_start])
        boundary = re.sub(r'\b(?:audio|sound|music|soundtrack)\b[^.;]*', ' ', boundary)
        if not _CUT_RE.search(boundary):
            missing_cuts.append(str(index + 1))
    if missing_cuts:
        errors.append(OMNI_VIDEO_ERROR_PREFIX + 'VIDEO lacks an actual clean cut / match cut into shot(s) '
                      + ', '.join(missing_cuts) + '; ordered shot names alone do not describe an edit')
    # 至少有一小段可拍内容，防止 "wide shot, clean cut close-up, ..." 自证。
    thin = []
    stop = {'a', 'an', 'the', 'and', 'then', 'with', 'to', 'of', 'in', 'on', 'from',
            'shot', 'shots', 'cut', 'cuts', 'cutting', 'clean', 'match', 'same', 'camera',
            'setup', 'opening', 'working', 'returning', 'wide', 'close', 'up', 'insert'}
    for index, (_start, end, rung) in enumerate(spans):
        next_start = spans[index + 1][0] if index + 1 < len(spans) else len(low)
        words = [word for word in re.findall(r'\b[a-z]+\b', low[end:next_start]) if word not in stop]
        if len(words) < 4:
            thin.append(rung.label)
    if thin:
        errors.append(OMNI_VIDEO_ERROR_PREFIX + 'VIDEO contains shot labels without enough visible action/detail: '
                      + ' / '.join(thin))
    return_span = next(((start, end) for start, end, rung in spans if rung.key == 'return'), None)
    if return_span:
        return_body = low[return_span[0]:]
        same_setup = bool(re.search(r'\b(?:same|identical|unchanged)\s+(?:locked\s+)?(?:camera\s+|macro\s+)?setup\b', return_body))
        same_position = bool(re.search(r'\b(?:same|identical|unchanged)\s+(?:opening\s+)?(?:camera\s+)?(?:position|viewpoint)\b', return_body))
        explicit_unchanged = ('opening camera position' in return_body
                              and re.search(r'\b(?:unchanged|identical|same)\b', return_body))
        same_trio = ((same_position or explicit_unchanged)
                     and re.search(r'\b(?:focal length|lens|focal setting)\b', return_body)
                     and re.search(r'\b(?:framing|composition)\b', return_body))
        contradictory_camera_re = re.compile(
            r'\b(?:new|different|changed|another)\s+(?:(?:camera|overhead|tighter|wider)\s+)?'
            r'(?:setup|position|viewpoint|framing|composition|focal length|lens|shot|view)\b'
            r'|\b(?:tighter|wider|changed|different)\s+framing\b'
            r'|\b(?:changes?|changing)\s+(?:the\s+)?(?:focal length|lens|framing|camera position)\b'
            r'|\bcamera\s+(?:is\s+)?(?:repositioned|relocated)\b'
            r'|\b(?:camera|returning\s+\w+\s+shot)(?:\s+\w+){0,6}\s+(?:new|different)\s+angle\b'
            r'|\b(?:new|different)\s+overhead\b',
        )
        contradiction = None
        for match in contradictory_camera_re.finditer(return_body):
            prefix = return_body[max(0, match.start() - 30):match.start()]
            if (re.search(r'\b(?:no|without|not|never)\b[^.;]*$', prefix)
                    and not re.search(r'\b(?:but|however|instead)\b', prefix)):
                continue
            contradiction = match
            break
        if not (same_setup or same_trio) or contradiction:
            errors.append(OMNI_VIDEO_ERROR_PREFIX + 'VIDEO returning shot must explicitly use the same camera setup '
                          '(position, framing and focal length) as the opening working shot; a new overhead angle '
                          'or different lens/framing is not a return to the anchor')
    return errors


def omni_video_violations(video_prompt, ladder=None, duration=None, skip_shot_list=False,
                          allow_single_take=False):
    """VIDEO 正文对 omni 镜头语法的违规项。空列表 = 合规。

    duration 用于选择默认施工梯。skip_shot_list 是旧调用方兼容参数；只有显式用户
    单镜授权 allow_single_take 才豁免多镜语法，原子过门阶段必须传它自己的 ladder。
    """
    if allow_single_take:
        missing = extras = []
        ladder = _SINGLE_TAKE_LADDER
    else:
        ladder = ladder or (ladder_for(duration) if duration is not None else _DEFAULT_CONSTRUCTION_LADDER)
        body = _body_without_timeline(video_prompt)
        expected = ' / '.join(rung.label.split()[0] for rung in ladder)
        missing = _missing_shot_rungs(body, ladder)
        extras = _extra_shot_rungs(body, ladder)
    errors = []
    ceiling = video_word_targets(max(3, len(ladder)))[1]
    word_count = len((video_prompt or '').split())
    if word_count > ceiling:
        errors.append(OMNI_VIDEO_ERROR_PREFIX + f'VIDEO prompt word count ({word_count}) exceeds limit of {ceiling} words')

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

    if not allow_single_take:
        errors.extend(_shot_structure_errors(video_prompt, ladder))
    hits = _one_take_hits(video_prompt)
    if hits and not allow_single_take:
        errors.append(
            OMNI_VIDEO_ERROR_PREFIX
            + "VIDEO uses banned one-take wording (" + ', '.join(hits)
            + ") — 默认多镜头契约适用于过门与兑现；只有用户明确指定单镜才能覆盖"
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
    """Gemini Omni 的 Phase 2：VIDEO 走分拍型自然分镜，其余一切沿用 base。"""

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
        """本单单段视频的时长（秒），决定施工三/四镜和相应预算，必须与生成端一致。
        两边都走 server_common.resolve_video_duration，
        并且带上同一个 self._duration_hint（begin_run 里按 beat_ladder 拍重算好的），
        保证合成阶段的片长和生成阶段实际请求不会走成两个数。"""
        return server_common.resolve_video_duration(self.config, fallback_hint=self._duration_hint)

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
        if self.allows_single_take(beat):
            return _SINGLE_TAKE_LADDER
        if is_crossing is None:
            is_crossing = bool(beat) and bool(pp.beat_is_crossing_clip(beat))
        kind = ladder_kind(beat, is_threshold_or_reveal, is_crossing)
        if kind is None:
            return None
        return self.ladder_for_kind(self.clip_duration(), kind,
                                    observed_shots=pp.observed_shot_count_of(beat),
                                    observed_scale=pp.observed_shot_scale_of(beat),
                                    observed_scales=pp.observed_shot_scale_sequence_of(beat))

    def allows_single_take(self, beat=None):
        """只接受明确用户输入标识；生成器自己写的 beat.video_shot_mode 不构成授权。"""
        if self.profile != 'omni':
            return False
        if isinstance(beat, dict) and beat.get('user_single_take') is True:
            return True
        state = self.state or {}
        brief = state.get('parsed_brief') or {}
        if 'video_shot_mode' in brief:
            return brief.get('video_shot_mode') == 'single_take'
        return pp.omni_user_shot_mode(theme=state.get('theme', '')) == 'single_take'

    def video_contract_errors(self, video_prompt, beat=None, ladder=None, duration=None):
        return omni_video_violations(
            video_prompt, ladder=ladder, duration=duration,
            allow_single_take=self.allows_single_take(beat))

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
                "field, deliberate push-ins, rack focus). Preserve the applicable shot structure; "
                "use a single take only when the user explicitly requests that override."
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
            "is loose: stationary working shots retain their primary landmarks; travelling transition "
            "shots instead preserve the registered spatial path and their own first/last anchor views."
        )

    def ensure_pacing(self, video_prompt):
        """普通施工拍补齐节奏声明与镜内连续性声明（缺了才补，不重复注入）。

        文案取自类属性（pacing_phrase / inshot_phrase），子 profile 换皮即可——注入点、
        判定口径与「过门拍/兑现拍免除」的分流仍然只有这一处实现。"""
        text = video_prompt or ''
        low = text.lower()

        # 1. 节奏声明检测（支持 marker 及常见变体正则）
        has_pacing = bool(
            self.pacing_marker in low
            or re.search(r'(?i)\bedited\s+(?:miniature\s+craft|construction)\s+time-lapse\s+assembled\s+from\s+multiple', text)
        )
        if not has_pacing:
            if text and not text.endswith(('.', '!', '?')):
                text += '.'
            text = f"{text} {self.pacing_phrase}".strip()

        # 2. 镜内连续性声明检测（支持 marker 及常见变体正则）
        has_inshot = bool(
            self.inshot_marker in low
            or re.search(r'(?i)\binside\s+every\s+shot\s+the\s+frame\s+keeps\s+(?:living|moving)', text)
            or re.search(r'(?i)\bthe\s+only\s+compressions\s+in\s+the\s+clip\s+fall', text)
        )
        if not has_inshot:
            if text and not text.endswith(('.', '!', '?')):
                text += '.'
            text = f"{text} {self.inshot_phrase}".strip()

        # 3. 终极去重清洗：若多次拼接产生重复的套话段落，保留第一处并清除多余副本
        text = self.deduplicate_boilerplate_phrases(text)
        return text

    @staticmethod
    def deduplicate_boilerplate_phrases(text):
        """清理提示词中因多次拼接或回炉产生的重复套话（如重复开场白、重复连续性声明、重复节奏声明）。"""
        if not text or not isinstance(text, str):
            return text

        # (1) 开场白去重 (Use the provided image / Use IMAGE N ...)
        anchor_pattern = re.compile(
            r'(Use the provided (?:first frame and last frame|image) as (?:the )?exact (?:starting )?composition and environment anchor\.[^.;]*?[.;])',
            re.IGNORECASE
        )
        anchor_matches = list(anchor_pattern.finditer(text))
        if len(anchor_matches) > 1:
            first_span = anchor_matches[0].span()
            head = text[:first_span[1]]
            tail = text[first_span[1]:]
            for m in anchor_matches[1:]:
                tail = tail.replace(m.group(0), '', 1)
            text = head + tail

        # (2) 镜内连续性声明去重 (Inside every shot the frame keeps living/moving...)
        inshot_pattern = re.compile(
            r'(Inside every shot the frame keeps (?:living|moving) from its first to its last moment — [^.;]+? — and this beat\'s change advances only during the (?:working|work) shots\.\s*The only compressions in the clip fall [^.;]+?; no shot contains a hold, a stall, or a deferred step that is then delivered all at once\.?)',
            re.IGNORECASE
        )
        inshot_matches = list(inshot_pattern.finditer(text))
        if len(inshot_matches) > 1:
            first_span = inshot_matches[0].span()
            head = text[:first_span[1]]
            tail = text[first_span[1]:]
            for m in inshot_matches[1:]:
                tail = tail.replace(m.group(0), '', 1)
            text = head + tail

        # (3) 节奏声明去重 (edited miniature craft time-lapse / edited construction time-lapse...)
        pacing_pattern = re.compile(
            r'(edited (?:miniature craft|construction) time-lapse assembled from multiple (?:macro )?camera setups, not real-time footage(?:, with oversized human hands entering and withdrawing between passes)?\.?)',
            re.IGNORECASE
        )
        pacing_matches = list(pacing_pattern.finditer(text))
        if len(pacing_matches) > 1:
            first_span = pacing_matches[0].span()
            head = text[:first_span[1]]
            tail = text[first_span[1]:]
            for m in pacing_matches[1:]:
                tail = tail.replace(m.group(0), '', 1)
            text = head + tail

        return re.sub(r'\s{2,}', ' ', text).strip()

    def fallback_ladder_clause(self, ladder):
        """占位兜底稿补的镜头梯声明。默认是 omni 的 UGC 手机质感版本。"""
        return fallback_ladder_clause(ladder)

    def ensure_actor_engagement(self, video_prompt, ladder, packet=None, beat=None,
                                is_threshold_or_reveal=False):
        """确保正文里的施工主体从画外入画、干完出画（首尾锚点帧一律无人）。

        默认是 omni 的实景工人口径。世界观不同的子 profile（微缩线的施工主体是从画幅
        边缘伸入的巨人手，本来就自带进出画）必须覆写这一处，否则会被塞进一句写实工人的
        入画/出画措辞。"""
        return ensure_ladder_out_and_in(
            video_prompt, ladder, packet=packet, beat=beat,
            is_threshold_or_reveal=is_threshold_or_reveal)

    def video_override_block(self, include_threshold=False, ladder=None, insert_subject=None):
        """追加在 base system prompt 之后的 OMNI VIDEO OVERRIDE 段。

        只覆盖 VIDEO：上面那份 base 契约里关于 IMAGE 的每一条（干净帧、无人称词、
        里程碑骨架、痕迹、包络覆盖……）继续照旧生效，一个字都不重复。

        批量段共享默认施工梯，同时声明过门/兑现/原子阶段的优先分流；单拍传入其实际梯。
        """
        duration = self.clip_duration()
        generic_batch = ladder is None
        ladder = ladder or (_SINGLE_TAKE_LADDER if self.allows_single_take()
                            else self.ladder_for_kind(duration, 'construction'))
        target, ceiling = video_word_targets(max(3, len(ladder)))
        references = self.required_references_block(
            include_threshold=include_threshold or ladder in (
                _TRANSITION_HARDWARE_LADDER, _TRANSITION_STAGE_LADDER))
        references_section = (
            f"\n==================== OMNI REQUIRED REFERENCES ====================\n{references}"
            if references else '')
        if ladder == _SINGLE_TAKE_LADDER:
            return f"""

==================== OMNI VIDEO OVERRIDE — EXPLICIT USER SINGLE TAKE ====================
用户明确指定单镜，单段片长 {duration} 秒。此覆盖优先于本提示与参考文件里的默认多镜头规则。
只写一个连续实拍镜头，从起始 IMAGE 的实际状态执行用户指定动作到结果 IMAGE；不添加特写插入、
不添加剪辑、不写 edited construction time-lapse、不检查默认 cut / return-camera 梯。
动作第一次发生完整可见，所有物件有既有来源，首尾锚点绑定、自然身体力学、材质与照明连续性继续生效。
保留用户指定的固定机位、焦段与构图；计数写英文单词，使用自然叙事而非机械时间线。
整条 VIDEO 硬顶 {ceiling} 词。审计写“用户单镜覆盖；默认多镜项不适用”，不得伪称这些项通过。
{self.capture_style_rule()}
{references_section}"""
        keys = {rung.key for rung in ladder}
        same_camera_rule = (
            '第一镜与最后一镜是同一个机位、同一个构图、同一个焦段；最后一镜明确写明 '
            'the same camera setup as the opening working shot。插入不改变完成度。'
            if 'main' in keys and 'return' in keys else
            '首镜与末镜分别绑定本拍自己的起始和结果 IMAGE，按该拍型的空间路径连接；'
            '不要求不同位置的首尾使用同一机位，插入不推进空间位置或完成度。')
        if generic_batch:
            shot_scope = ('下面的默认梯仅普通施工适用。混合批次先按每拍的 operation / '
                          'transition_stage 选择拍型：完整过门、兑现及展开子阶段各用自己的三镜，'
                          '用户明确单镜覆盖优先；不能把下面施工梯套到整批。')
            shot_count_rule = f'仅普通施工按下面 {len(ladder)} 镜默认梯的顺序写满'
            same_camera_rule = (
                '仅普通施工与入口硬件三镜要求首末同机位、构图、焦段并明确声明；'
                '其他过门、空间移动和兑现拍分别绑定自己的起始及结果 IMAGE，'
                '不要求不同位置的首尾回到同一机位。插入不推进完成度或空间位置。')
            length_rule = (
                f'普通施工参考梯目标 {target} 词上下；每拍按实际镜头数审计全文：'
                '三镜特殊拍及用户单镜硬顶四百词，四镜施工硬顶四百五十五词。')
        else:
            shot_scope = '本拍按下面已选定的实际梯逐镜写可拍的动作与细节。'
            shot_count_rule = f'本拍 {len(ladder)} 个镜头，按这个顺序写满'
            length_rule = f'整条 VIDEO 目标 {target} 词上下，硬顶 {ceiling} 词。'
        return f"""

==================== OMNI VIDEO OVERRIDE (读到这里为止的 VIDEO 规则以本段为准) ====================
本次输出的目标模型是 Gemini Omni，单段片长 {duration} 秒。上面所有关于 IMAGE 的规则**继续完全
生效，不做任何修改**；唯独 VIDEO 的镜头语法整体改写为下面这套，与上文冲突处一律以本段为准。

MANDATORY SHOT STRUCTURE — {shot_scope} 普通施工是主工作镜、
{'两个' if len(ladder) == 4 else '一个'}特写插入、切回同机位；过门、兑现和展开子阶段优先用各自梯。不要写
establishing long shot / full shot / medium shot / wide outro shot 这一类旧梯的景别名，
一个都不要。{shot_count_rule}，每个镜头边界实际用 clean cut / match cut
衔接（禁止 cross-dissolve、fade、magical transition、instant transformation、teleport、
跳过物理过程的快剪）：
{ladder_roles(ladder, insert_subject)}

ANCHOR CAMERA POLICY——{same_camera_rule}

CINEMATIC NARRATIVE FLOW (纯自然语言多镜头因果流)——严禁使用任何机械时间戳或数字切点表。正文必须使用流畅的电影分镜叙事连词（例如 "The sequence opens with...", "Cutting in closer to a close-up insert...", "An extreme close-up insert reveals...", "Cutting back to a returning wide shot from the same camera setup..."）来自然串联各个镜头。正文所有计数和尺寸一律写成英文单词（three roof beams，不是 3 roof beams；ten seconds，不是 10s）。

拍型分流：过门桥拍走 逼近远景 / 门槛 / 落定室内远景 三镜，最终兑现拍走 细部 / 拉开 / 终局远景
三镜，优先于十秒施工四镜。展开入口硬件子阶段走固定三镜（主镜 / 特写 / 同机位返回），
只开合原有入口或移走已有杂物，不安装新物件。展开的空间移动/转向/揭示子阶段走
transition working shot / detail insert / landing shot 三镜，只完成自己的 transition_stage；
insert不推进空间位置，首末分别绑定各自anchor，禁止每个子拍重演完整穿越。以上特殊拍型都免除施工节奏声明。

DEFAULT ONE-TAKE BAN（过门拍与兑现拍同样适用；用户明确单镜覆盖优先）：禁止写 oner、one-shot、one-take、
single continuous take、one continuous take、single take、unbroken take 或任何等义措辞。
推镜、揭示、穿门这些动作是**镜头内部的运动**，不是"一条不间断的长镜头"。
{self.capture_style_rule()}
- PACING DECLARATION：普通施工拍在正文里声明一次时间基准，用这句原话——
  "{OMNI_PACING_PHRASE}"（所有过门阶段与最终兑现拍免除这句）。不要用 continuous 描述默认多镜片段的拍法。
- IN-SHOT CONTINUITY（以下句子只用于普通施工；特殊过门与兑现不硬套施工推进量）：上文那条
  EVEN RATE 指令（"每一刻都在推进 / 不许把改动推迟后一次兑现"）在默认多镜头包里作废，普通施工改用——
  "{OMNI_INSHOT_PHRASE}"
  理由：推进量全部集中在主镜，特写插入按契约不产生新的推进量，而切回镜恰恰是在剪辑点上
  做 same-way 压缩。要求"每一刻都在推进"等于要求模型违反自己的镜头级进度锁。
- PROGRESS ACROSS CUTS：每个镜头都从上一镜结束时的完成度开始；剪辑点只允许压缩"已经完整演示过一次"的重复动作，
  且必须在正文里说明（例如 after the remaining boards come loose the same way），不得跳过某类改动的第一次发生、
  不得凭空出现新物件、不得在剪辑点上让数量变化。
- PHRASING VARIATION：镜头梯是固定骨架，因此逐拍复读是本技能的头号失败模式。锚定开场句、
  镜头名、工人造型短语、节奏声明这几项**必须逐字保留**；除此之外，相邻两拍的句式模板、镜头内的从句顺序、
  动词选择、转场措辞、形容词搭配都必须换过。
- 长度：{length_rule} 普通施工主镜与切回镜各 60–90 词
  （它们承载起始状态、推进过程与结果状态），每个特写插入 30–50 词。
{references_section}"""

    # ── 覆写钩子 ────────────────────────────────────────────────────────────

    def batch_system_prompt(self, config, packet, scup_ref, tbcp_ref):
        include_threshold = bool(tbcp_ref) or any(
            ladder_kind(beat) in ('transition_hardware', 'transition_stage')
            for beat in (self.state or {}).get('beat_ladder', []) if isinstance(beat, dict))
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
            video_word_limit=video_word_targets(max(3, len(_ceiling_ladder)))[1])
        errs = [e for e in (errs or [])
                if not any(snippet in e for snippet in _BASE_ONLY_ERROR_SNIPPETS)]
        return (errs
                + self.video_contract_errors(
                    video_prompt, beat=beat, ladder=ladder,
                    duration=self.clip_duration() if ladder else None)
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

        residual = self.video_contract_errors(
            video_prompt, beat=beat, ladder=ladder,
            duration=self.clip_duration() if ladder else None)
        if omni_errs or [e for e in residual if e.startswith(OMNI_VIDEO_ERROR_PREFIX)]:
            video_prompt, omni_reworked = self.rework_omni_multishot(
                config, i, video_prompt, packet, beat=beat)
            reworked = omni_reworked if reworked is None else (reworked or omni_reworked)
        return video_prompt, reworked

    def normalize_reworked_video(self, video_prompt, beat=None):
        """里程碑回炉后按实际用户模式归一并交回上游复验，默认多镜与用户单镜不互相改写。"""
        return self.normalize_omni_video(video_prompt, beat=beat)

    def video_profile_violations(self, video_prompt, beat=None):
        """omni 的镜头语法硬伤（记号类瑕疵不算）。"""
        ladder = self.ladder_for_beat(beat) if beat else None
        residual = self.video_contract_errors(
            video_prompt, beat=beat, ladder=ladder,
            duration=self.clip_duration() if ladder else None)
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
        if self.allows_single_take(beat):
            return text
        if ladder and _missing_shot_rungs(_body_without_timeline(text), ladder):
            if not text.endswith(('.', '!', '?')):
                text += '.'
            text = f"{text} {self.fallback_ladder_clause(ladder)}"
        return text

    # ── omni 自己的 VIDEO 处理 ───────────────────────────────────────────────

    def fix_omni_video(self, i, video_prompt, packet, is_threshold_or_reveal,
                       beat=None, config=None, family=None):
        """omni 版的 VIDEO 确定性修复链。字数预算按镜头数缩放并在注入后复裁，
        节奏声明换成多镜头版、删除机械时间线，不走 base 的
        out-and-in 兜底（那句会塞进按 8 秒写死的时间戳与 Grid 记号，见模块 docstring）。"""
        ladder = self.ladder_for_beat(beat, is_threshold_or_reveal)
        shot_count = max(3, len(ladder or _DEFAULT_CONSTRUCTION_LADDER))
        _target, ceiling = video_word_targets(shot_count)
        text = pp.clean_prompt_text(video_prompt)
        # 已在交付硬顶内的正文不为套话预算先裁一次；旧预裁会删掉真实insert/cut。
        # 超顶稿可以预压缩，但新损坏的镜头/机位结构不能成为后续复验的错误基线。
        if len(text.split()) > ceiling:
            before_draft_errors = set(self.video_contract_errors(
                text, beat=beat, ladder=ladder, duration=self.clip_duration()))
            draft = pp.compress_prompt_to_budget(text, video_draft_budget(shot_count), config,
                                                 is_video=True)
            after_draft_errors = self.video_contract_errors(
                draft, beat=beat, ladder=ladder, duration=self.clip_duration())
            if not [error for error in after_draft_errors
                    if error not in before_draft_errors and 'word count' not in error]:
                text = draft
        text = pp.fix_video_opening(i, text, profile='omni')
        text = pp.fix_sound_design(text, family=family or 'exterior')
        text = pp.fix_natural_body_mechanics(text)
        text = self.ensure_actor_engagement(text, ladder, packet=packet, beat=beat,
                                            is_threshold_or_reveal=is_threshold_or_reveal)
        text = self.normalize_omni_video(
            text, is_threshold_or_reveal=is_threshold_or_reveal, beat=beat)
        candidate = pp.compress_prompt_to_budget(text, ceiling, config, is_video=True)
        # 压缩会删整句，可能恰好删掉cut、insert或same-camera。不得把损坏的短稿冒充合规。
        before_errors = set(self.video_contract_errors(text, beat=beat, ladder=ladder,
                                                      duration=self.clip_duration()))
        after_errors = self.video_contract_errors(candidate, beat=beat, ladder=ladder,
                                                 duration=self.clip_duration())
        introduced = [error for error in after_errors
                      if error not in before_errors and 'word count' not in error]
        return text if introduced else candidate

    def normalize_omni_video(self, video_prompt, is_threshold_or_reveal=None, beat=None,
                             is_crossing=None):
        """确定性归一：默认清一镜到底措辞、清 base 的 even-rate 句、折数字与旧时间线；
        普通施工拍补齐节奏声明，用户明确单镜则保留连续实拍叙述。

        拍型未知（回炉通路只拿到一段文本）时只做前三步：过门拍/兑现拍本来就免除节奏声明，
        不猜未知拍型的结构。"""
        user_single = self.allows_single_take(beat)
        text = (video_prompt or '') if user_single else strip_one_take_language(video_prompt)
        text = _strip_base_even_rate(text)
        text = _digits_to_words(text)
        text = text.replace('exactly on the listed cut marks', 'only at the described cuts')
        if user_single:
            text = text.replace('through the multi-shot sequence', 'through the same continuous take')
            text = text.replace('subsequent shots continue the operation', 'the same shot continues the operation')
            kept = [sentence for sentence in re.split(r'(?<=[.!?])\s+', text)
                    if not any(marker in sentence.lower() for marker in (
                        self.pacing_marker, 'inside every shot the frame keeps',
                        'the only compressions in the clip fall'))]
            return ' '.join(kept).strip()

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
        if ladder == _SINGLE_TAKE_LADDER:
            return f"""You are rewriting ONE video prompt for an explicitly requested single continuous take.
The user's single-take request overrides the default Gemini Omni multi-shot grammar.
Keep the first/last IMAGE anchors, the same physical work, objects, material sources, traces,
body mechanics, sound and available light. Keep all actions inside one uninterrupted shot.
Do not add cuts, close-up inserts, edited time-lapse, or a multi-shot pacing declaration.
Use English words for counts and natural prose, without timestamp tables. Keep the whole
delivered prompt, including anchor instructions and sound, at or below four hundred words.
Output ONLY the rewritten VIDEO body."""
        if ladder in (_TRANSITION_HARDWARE_LADDER, _TRANSITION_STAGE_LADDER,
                       _TRAVERSAL_LADDER, _REWARD_LADDER):
            same_camera = ('Return to the identical opening camera position, framing and focal length; '
                           'say the same camera setup explicitly in the returning shot.'
                           if ladder == _TRANSITION_HARDWARE_LADDER else
                           'The first and last shots use their OWN respective anchor camera positions; '
                           'do not require them to be the same setup.')
            return f"""You are rewriting ONE video prompt so it obeys the Gemini Omni multi-shot contract.
{multishot_ref}
Keep the exact first/last IMAGE binding and every existing physical detail, object, source,
light and sound. Use exactly three shots in this order: {scales}. Write an actual clean cut
or match cut into EACH later shot, and describe visible action or detail in each shot.
{ladder_roles(ladder)}
{same_camera}
For an expanded transition, execute only the current stage: never redo the whole crossing,
skip ahead to a later stage, add construction, or advance camera position during the insert.
For a reward clip, retain the already finished state and the specified use action.
Do not add construction repetition, material progress quotas or construction time-lapse.
Write counts in English words and natural narrative prose, without numeric cut marks.
Keep the entire delivered VIDEO at or below four hundred words. Output ONLY the VIDEO body."""
        return f"""You are rewriting ONE video prompt so it obeys the Gemini Omni multi-shot contract.

{multishot_ref}

Rewrite rules (additive — do not lose content):
{pp.WORK_FIRST_VIDEO_RULES}
- Keep the opening anchor sentence ("Use the provided image as the exact starting composition and environment anchor. ...") VERBATIM as the first sentence.
- Express the multi-shot sequence using pure natural language cinematic transitions (e.g. 'The sequence opens with...', 'Cutting in closer to...', 'An extreme close-up reveals...', 'Cutting back to...'). Do NOT output numeric timestamps, seconds marks, or robotic cut mark tables.
- Keep every concrete detail already in the draft: the same single worker and costume, the same tool, the same operation, the same persistent traces, the same audio description, the same lighting progression. Redistribute them across the shots instead of inventing new ones.
- Restructure the body into exactly {len(ladder)} shots IN THIS ORDER, naming each one in prose exactly as written here: {scales}. Join them with clean cuts or match cuts.
- This is NOT a shot-scale rotation. Do not write "establishing long shot", "full shot", "medium shot", or "wide outro shot" anywhere — those names belong to the retired grammar and count as a contract violation.
- The first and the last shot are the SAME camera setup, framing, and focal length, differing only in how far the work has progressed; say so explicitly in the last shot ("the same camera setup as the opening wide working shot"). The insert(s) cut into that setup and cut back at the same completion level.
- Both anchor frames are person-free stills: immediately after the opening instant the worker reaches or steps in from the adjacent frame edge into first effective tool contact without pausing. The main shot carries the visible advance; the insert(s) prove this operation's contact, material physics and persistent traces without advancing completion; the returning wide shot compresses the remaining repetitions the same way and reaches the resulting state. The last working motion includes withdrawing all visible hands/body out of frame, matching the person-free closing anchor. No separate arrival, departure or empty hold; let the operation determine the opening and closing gesture.
- Preserve the visible stage-milestone skeleton VERBATIM in meaning: the declared visible start state, the declared resulting state, both declared progress lines (primary and secondary material/stock), the first effective tool contact at the opening moment, the material source/container and the movement path, and repeated work cycles. Use the words "repeated"/"repeatedly"/"cycle by cycle"/"course by course" literally — "repetitions" alone does not read as repeated cycles.
- All numbers and counts must be written in English words. Never include arabic digits.
- NEVER write oner, one-shot, one-take, single continuous take, one continuous take, single take, or unbroken take — there is no exemption.
- Keep the entire delivered VIDEO at or below {video_word_targets(len(ladder))[1]} words, including the anchors, sound and continuity sentences.
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

        timeout_sec = int(config.get('composeRequestTimeoutSeconds', 45))
        try:
            resp = pp._chat(config, system, user, temperature=0.7, timeout=timeout_sec)
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
        candidate = pp.fix_natural_body_mechanics(candidate)
        candidate = self.normalize_omni_video(candidate, beat=beat)
        residual = self.video_contract_errors(
            candidate, beat=beat, ladder=ladder, duration=duration if beat else None)
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
    """Keep person-free anchor instants with work-integrated edge entry/withdrawal."""
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
    # 净帧策略（frame_state.PERSON_FREE_IMAGE_FRAMES）之后这里整个翻了向：首尾锚点帧
    # 都是空的，所以要洗掉的是「开场就已经在作业面上」这类**旧策略**句子，进出画的句子
    # 一律留着。理由见 pp.fix_out_and_in 的策略注释。
    agent = pp._WORKER_AGENT_RE_SRC
    text = re.sub(
        rf'(?i)(?:^|(?<=[.;]))\s*(?:in\s+the\s+(?:opening|wide\s+working|returning\s+wide)\s+shot|'
        rf'at\s+(?:zero\s+seconds?|t\s*=\s*0s?|the\s+(?:start|beginning)))[,;:]?\s*'
        rf'{agent}[^.;]*?\b(?:already\s+(?:at|positioned|in\s+place))\b[^.;]*[.;]', ' ', text)
    text = re.sub(r'\s{2,}', ' ', text).strip()
    has_entry = bool(re.search(rf'(?i){agent}[^.;]*?{pp._WORKER_ENTRY_RE_SRC}', text))
    has_exit = bool(re.search(rf'(?i)(?:{agent}|they|he|she)[^.;]*?{pp._WORKER_EXIT_RE_SRC}', text))
    if has_entry and has_exit:
        return text
    costume = pp._worker_costume_from_packet(packet)
    if text and not text.rstrip().endswith(('.', '!', '?')):
        text = text.rstrip() + '.'
    clause = (
        f" The opening frame is empty of people; immediately afterwards the lone worker{costume} "
        "enters from off-frame with one short reach or step into first effective tool contact at "
        "the adjacent work face; subsequent shots continue the operation. The worker withdraws fully "
        "out of frame with the last working motion, leaving a person-free closing frame."
    )
    return (text.rstrip() + clause).strip()


def fallback_ladder_clause(ladder):
    """占位兜底稿补的镜头梯声明：一句话里按顺序带齐每一级镜头名与 UGC 拍摄质感，
    让占位稿至少不违反 omni 的镜头语法（占位稿本身仍计入 fallback_count 门禁）。"""
    fragments = []
    if ladder == _SINGLE_TAKE_LADDER:
        return ('One single continuous take matches the first frame, shows the specified physical '
                'action without cuts, and ends at the last-frame state in the same camera setup.')
    for rung in ladder:
        fragment = _FALLBACK_SHOT_FRAGMENT.get(rung.key)
        if rung.key == 'transition_work':
            fragment = 'a transition working shot matching the first frame and visibly continuing only the current spatial stage'
        elif rung.key == 'transition_detail':
            fragment = 'a detail insert showing the already visible orientation landmark without advancing the camera position'
        elif rung.key == 'transition_land':
            fragment = 'a landing shot resuming that same stage and settling at the last-frame position and orientation'
        if rung.key == 'main':
            fragment = (f'{rung.phrase} matching the person-free first frame, with the worker entering '
                        f'from the adjacent frame edge immediately afterwards into first effective tool contact '
                        f'and carrying the visible advance of this beat')
        elif rung.key == 'return':
            fragment = (f'{rung.phrase} from the same camera setup, matching the last frame '
                        f'as the last working motion ends with the worker withdrawing fully out of frame')
        if ladder == _TRANSITION_HARDWARE_LADDER:
            fragment = {
                'main': 'a wide working shot matching the first frame as the existing entrance hardware or loose obstruction is opened or moved on camera',
                'close': 'a close-up insert showing that same existing contact point at unchanged opening completion',
                'return': 'a returning wide shot from the same camera setup, framing and focal length as the opening working shot, completing only the current entrance action and matching the last frame',
            }[rung.key]
        fragments.append(fragment)
    joined = fragments[0] + ''.join(f'; a clean cut to {fragment}' for fragment in fragments[1:])
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
