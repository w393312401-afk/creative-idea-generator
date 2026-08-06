"""omni profile（gemini-omni-restoration-composer）的 Phase 2 composer。

与 base 的唯一区别在 VIDEO 一侧：Gemini Omni 的视频提示词不是一条一镜到底的片段，
而是一段**剪辑过的多镜头序列**，默认拍摄质感是 UGC 手机随手拍而不是院线感。IMAGE 段、
Phase 1（brief/工序梯/Drift Lock 包/IMAGE 1）、断点续传、槽位格式全部沿用 base——
它们是下游帧渲染/创意库/续传共同的契约，与「做哪个视频模型的提示词」无关。

2026-08-01「弹性镜头梯 + 时间线」改造，三件事：

  1. **镜头数不再是常量六**。Omni Flash 的 Flow 面板提供 4/6/8/10 秒四档，六镜头塞进
     4 秒等于每镜 0.67 秒——模型只能丢镜头或整体加速，两者都表现为观感上的跳变。
     现在镜头梯由 server_common.resolve_video_duration() 决定（见 _SHOT_COUNT_BY_DURATION），
     约束是「平均每镜不低于 1.3 秒、主工作镜不低于 1.9 秒」。裁镜头不是随便丢：
     远景（首帧锚）/ 中景（唯一携带推进量的镜头）/ 结果远景（尾帧锚）三镜任何长度下
     都不可裁，回补优先级是 近景 → 全景 → 特写，被裁那一级的职责并进相邻镜头
     （ladder_roles）。反向也要查：写出梯外的额外景别同样是硬伤（_extra_shot_rungs）——
     四秒里塞六个镜头会把每镜压到一秒以下，观感是闪帧。

  2. **切点用时间线句显式钉在秒上**。这是本次唯一被允许出现阿拉伯数字的地方
     （omni-output-templates.md §Notation Ban 的 Timecode exemption），正文其余部分
     的数字一律折成英文单词。时间线句由 _inject_timeline 确定性注入并覆写——模型
     自己编的时间线不作数。

  3. **过门桥拍与最终兑现拍走各自的梯**。此前它们被同一套六镜头施工梯审计，等于要求
     一段穿门镜头也写出"工具接触点"和"重复作业循环"。

同时清掉两处与 base 契约的硬冲突：
  · base 的 even-rate 句（"每一刻都在推进、不许把改动推迟后一次兑现"）与镜头级进度锁
    直接对撞——远景/全景推进量为零、特写不产生推进量、结果远景正是在剪辑点上做
    same-way 压缩。改用 OMNI_INSHOT_PHRASE：连续性约束到**镜内**，压缩只允许发生在
    声明过的切点上。
  · base 的 out-and-in 兜底会往六镜头包里塞绝对时间戳与固定入画口（"At t=0s ... from
    the Grid C1 edge ... by t=7.5s"）：时间戳按 8 秒写死、Grid 记号本身违反记号禁用、
    且与"镜一零工人 / 镜二从命名路径入画"冲突。omni 下整条跳过，工人进出由镜头梯承载，
    真缺进出仍由 base 的 check_out_and_in 报错并触发回炉。

契约文件一律经 pp.load_reference_file(name, self.profile) 读取（omni 包缺的文件由它
回落到 base），不在这里拼任何路径。
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
# weight 是时长分配权重：中景要装下"第一次动作完整可见 + 重复循环"，配额必须最高。
Rung = namedtuple('Rung', 'key variants label phrase weight role')

_R_ESTABLISHING = Rung(
    'establishing', ('establishing long shot',), '远景 establishing long shot',
    'an establishing long shot', 1.0,
    '画面等同这一拍的起始 IMAGE，本拍的改动量为零，零工人')
_R_FULL = Rung(
    'full', ('full shot',), '全景 full shot', 'a full shot', 1.0,
    '工人从明确命名的边缘/路径入画，已带着工具与材料走向作业区，仅做准备，作业区仍等同起始 IMAGE')
_R_MEDIUM = Rung(
    'medium', ('medium shot',), '中景 medium shot', 'a medium shot', 1.4,
    '主操作推进，第一次动作完整可见，随后重复循环，本拍改动推进到约四分之三，'
    '用 -ing / partially / growing 这类进行态措辞')
_R_CLOSE = Rung(
    'close', ('close up',), '近景 close-up', 'a close-up', 1.0,
    '工具接触点与材料物理（形变、碎屑、粉尘、纤维、飞溅），不产生新的推进量')
_R_XCLOSE = Rung(
    'xclose', ('extreme close up',), '特写 extreme close-up', 'an extreme close-up', 0.9,
    '至少两处本次操作特有的持久痕迹与微观质感，同样不产生推进量')
_R_OUTRO = Rung(
    'outro', ('wide outro shot', 'wide outro'), '结果远景 wide outro shot',
    'a wide outro shot', 1.1,
    '工人从明确命名的路径出画、临时工具随之离场，画面精确等同这一拍的结果 IMAGE，'
    '只有这一镜可以用完成态措辞')

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

# 施工梯：按时长伸缩。三镜不可裁 = 远景（首帧锚）/ 中景（唯一携带推进量的镜头）/
# 结果远景（尾帧锚）。回补优先级 近景 → 全景 → 特写：镜头数被压到四时，工人入画可以
# 并进中景首句（干净帧契约照样成立），而因果痕迹没有别的镜头能承载。
_CONSTRUCTION_LADDERS = {
    3: (_R_ESTABLISHING, _R_MEDIUM, _R_OUTRO),
    4: (_R_ESTABLISHING, _R_MEDIUM, _R_CLOSE, _R_OUTRO),
    5: (_R_ESTABLISHING, _R_FULL, _R_MEDIUM, _R_CLOSE, _R_OUTRO),
    6: (_R_ESTABLISHING, _R_FULL, _R_MEDIUM, _R_CLOSE, _R_XCLOSE, _R_OUTRO),
}

# 过门桥拍与兑现拍：三个自然工位，与时长无关（一次穿越就是逼近/门槛/落定，再切只是把
# 同一件事切碎）。它们免除节奏声明——traverse/reveal 不压缩劳动。
_TRAVERSAL_LADDER = (_R_APPROACH, _R_THRESHOLD, _R_ARRIVAL)
_REWARD_LADDER = (_R_DETAIL, _R_PULLBACK, _R_FINAL_WIDE)

# 时长 → 施工镜头数。约束：平均每镜 ≥1.3 秒（更短读作闪帧而非镜头），主工作镜 ≥1.9 秒。
_SHOT_COUNT_BY_DURATION = {4: 3, 6: 4, 8: 5, 10: 6}
_MIN_SHOT_SECONDS = 1.3

_DURATION_WORDS = {4: 'four', 6: 'six', 8: 'eight', 10: 'ten'}
_COUNT_WORDS = {3: 'three', 4: 'four', 5: 'five', 6: 'six'}

# 兜底稿里每一级镜头的一句话职责（占位稿仍计入 fallback_count 门禁，但至少不违反镜头语法）。
_FALLBACK_SHOT_FRAGMENT = {
    'establishing': 'an establishing long shot matching the first frame',
    'full': 'a full shot as the worker enters carrying the tool',
    'medium': 'a medium shot of the repeated work cycle',
    'close': 'a close-up on the tool contact',
    'xclose': 'an extreme close-up on the traces left behind',
    'outro': 'a wide outro shot matching the last frame with the worker already gone',
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
    """这个时长排几个施工镜头。表外时长按「平均每镜不低于 1.3 秒」反推，钳在 3–6。"""
    try:
        seconds = int(round(float(duration)))
    except (TypeError, ValueError):
        seconds = server_common.OMNI_DEFAULT_VIDEO_DURATION
    if seconds in _SHOT_COUNT_BY_DURATION:
        return _SHOT_COUNT_BY_DURATION[seconds]
    return max(3, min(6, int(seconds // _MIN_SHOT_SECONDS)))


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


def ladder_roles(ladder):
    """逐镜职责文案。镜头数被压缩时，被裁掉那一级的职责并进相邻镜头——否则"工人从命名
    路径入画"和"至少两处持久痕迹"会随着镜头一起消失，那才是真正的内容损失。"""
    keys = {rung.key for rung in ladder}
    lines = []
    for index, rung in enumerate(ladder, start=1):
        role = rung.role
        if rung.key == 'medium' and 'full' not in keys:
            role = ('工人从明确命名的边缘/路径带着工具入画后立即开始作业'
                    '（本长度没有独立全景，入画并进本镜首句）；') + role
        if rung.key == 'close' and 'xclose' not in keys:
            role = role + '；并在同一镜里给到至少两处本次操作特有的持久痕迹（本长度没有独立特写）'
        if rung.key == 'medium' and 'close' not in keys:
            role = role + ('；工具接触点的材料物理与至少两处本次操作特有的持久痕迹'
                           '也在本镜内交代（本长度没有独立近景与特写）')
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
    ladder = ladder or _CONSTRUCTION_LADDERS[6]
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


_ALL_RUNGS = (_R_ESTABLISHING, _R_FULL, _R_MEDIUM, _R_CLOSE, _R_XCLOSE, _R_OUTRO,
              _R_APPROACH, _R_THRESHOLD, _R_ARRIVAL, _R_DETAIL, _R_PULLBACK, _R_FINAL_WIDE)


def _extra_shot_rungs(text, ladder):
    """正文里出现了**不属于本梯**的景别。

    这是短片长下的头号失败模式：模型照着满六镜的习惯写，把六个镜头塞进四秒。缺镜头有人
    查（_missing_shot_rungs），多镜头此前完全没人查——而多出来的镜头恰恰会把每镜压到
    一秒以下，观感就是闪帧。"""
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

    ladder 缺省按满六镜施工梯判（模块级调用方与旧测试的口径）。duration 给了才查
    时间线——回炉通路拿不到拍型时不该凭空要求一张切点表。
    节奏声明不在这里查：它由 _ensure_pacing 确定性注入，查了也只会是死代码。

    skip_shot_list（2026-08-06）：调用方已经用 is_expanded_transition_stage_beat 判定
    这一拍是展开后的原子级过门/空间重置子拍、传了 ladder=None 时才该置 True——这类拍
    根本不该套任何镜头梯。不加这个开关的话，下面 `ladder or _CONSTRUCTION_LADDERS[6]`
    这行会在 ladder=None 时悄悄换成六镜施工梯，一样保证首稿必炸，只是换了个错误的
    期望镜头梯而已。"""
    if skip_shot_list:
        missing = extras = []
        ladder = ladder or _CONSTRUCTION_LADDERS[6]
    else:
        ladder = ladder or _CONSTRUCTION_LADDERS[6]
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

    if duration is not None:
        expected_sentence = timeline_sentence(duration, ladder)
        found = _TIMELINE_RE.search(video_prompt or '')
        if not found:
            errors.append(
                OMNI_VIDEO_ERROR_PREFIX
                + "VIDEO 缺少时间线句，切点没有被钉在秒上——必须原样带上："
                + expected_sentence
            )
        elif _normalized(found.group(0)) != _normalized(expected_sentence):
            errors.append(
                OMNI_VIDEO_ERROR_PREFIX
                + "VIDEO 的时间线句与本条片子的时长/镜头梯不符，必须原样改成："
                + expected_sentence
            )

    stray = _stray_digits(video_prompt)
    if stray:
        errors.append(
            OMNI_VIDEO_STYLE_PREFIX
            + "正文出现时间码以外的阿拉伯数字（" + ', '.join(stray)
            + "）——记号禁用只对时间线句开了口子，其余计数一律写成英文单词"
        )

    return errors


class OmniComposer(BaseComposer):
    """Gemini Omni 的 Phase 2：VIDEO 走弹性镜头梯 + 时间线切点，其余一切沿用 base。"""

    profile = 'omni'

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

    def ladder_for_beat(self, beat=None, is_threshold_or_reveal=None, is_crossing=None):
        """这一拍的镜头梯。拍型不明时返回 None（见 ladder_kind）。"""
        if is_crossing is None:
            is_crossing = bool(beat) and bool(pp.beat_is_crossing_clip(beat))
        kind = ladder_kind(beat, is_threshold_or_reveal, is_crossing)
        if kind is None:
            return None
        return ladder_for(self.clip_duration(), kind)

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

    def video_override_block(self, include_threshold=False, ladder=None):
        """追加在 base system prompt 之后的 OMNI VIDEO OVERRIDE 段。

        只覆盖 VIDEO：上面那份 base 契约里关于 IMAGE 的每一条（干净帧、无人称词、
        里程碑骨架、痕迹、包络覆盖……）继续照旧生效，一个字都不重复。

        ladder 缺省用本单时长下的施工梯——批量直出的这一段是**每拍共享**的，而同一批
        里可能混着过门拍与兑现拍，它们各自的切点表由 _inject_timeline 逐拍确定性覆写，
        这里只需要把三套梯的规则讲清楚。"""
        duration = self.clip_duration()
        ladder = ladder or ladder_for(duration, 'construction')
        target, ceiling = video_word_targets(len(ladder))
        references = self.required_references_block(include_threshold=include_threshold)
        references_section = (
            f"\n==================== OMNI REQUIRED REFERENCES ====================\n{references}"
            if references else '')
        return f"""

==================== OMNI VIDEO OVERRIDE (读到这里为止的 VIDEO 规则以本段为准) ====================
本次输出的目标模型是 Gemini Omni，单段片长 {duration} 秒。上面所有关于 IMAGE 的规则**继续完全
生效，不做任何修改**；唯独 VIDEO 的镜头语法整体改写为下面这套，与上文冲突处一律以本段为准。

MANDATORY SHOT LADDER — 每一条普通施工 VIDEO 都是一段剪辑过的多镜头序列。{duration} 秒对应
{len(ladder)} 个镜头，按这个顺序写满，镜头之间用 clean cut / match cut 衔接（禁止 cross-dissolve、
fade、magical transition、instant transformation、teleport、跳过物理过程的快剪）：
{ladder_roles(ladder)}

SHOT TIMELINE（本段最重要的一条）——正文里必须原样带上这句切点表，位置紧跟锚定开场句之后：
  "{timeline_sentence(duration, ladder)}"
后面每个镜头的首句再用**英文单词**复述一次自己的入点（例如 "A clean cut at the one-and-a-half-second
mark moves to a full shot ..."），与切点表形成冗余绑定。这句切点表是**唯一**允许出现阿拉伯数字的
地方；正文其余部分的计数一律写成英文单词（three roof beams，不是 3 roof beams）。

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
  理由：远景与全景的推进量按契约就是零，近景/特写不产生新的推进量，而结果远景恰恰是在剪辑点上
  做 same-way 压缩。要求"每一刻都在推进"等于要求模型违反自己的镜头级进度锁。
- PROGRESS ACROSS CUTS：每个镜头都从上一镜结束时的完成度开始；剪辑点只允许压缩"已经完整演示过一次"的重复动作，
  且必须在正文里说明（例如 after the remaining boards come loose the same way），不得跳过某类改动的第一次发生、
  不得凭空出现新物件、不得在剪辑点上让数量变化。
- PHRASING VARIATION：镜头梯是固定骨架，因此逐拍复读是本技能的头号失败模式。锚定开场句、切点表、
  镜头名、工人造型短语、节奏声明这几项**必须逐字保留**；除此之外，相邻两拍的句式模板、镜头内的从句顺序、
  动词选择、转场措辞、形容词搭配都必须换过。
- 长度：整条 VIDEO 目标 {target} 词上下，硬顶 {ceiling} 词；单个镜头 45–70 词。
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
            + self.video_override_block(include_threshold=is_crossing, ladder=ladder))

    def apply_proactive_fixes(self, i, video_prompt, image_prompt, packet, mode, is_last,
                              is_threshold_or_reveal, beat=None, config=None, family=None):
        """IMAGE 完全委托 base（下游帧渲染吃的是同一套契约）；VIDEO 走 omni 自己的链路。

        VIDEO 不能借道 base：base 会把正文压到 270 词（多镜头文本会被腰斩）、注入含
        continuous 的节奏声明（在多镜头包里等于叫模型去拍一镜到底）、并塞进按 8 秒写死
        的工人进出时间戳。"""
        _discarded_video, fixed_image = pp.apply_proactive_fixes(
            i, video_prompt, image_prompt, packet, mode, is_last, is_threshold_or_reveal,
            beat=beat, config=config, family=family)
        # 记号禁用适用于两侧的 prompt body，不只 VIDEO。base 的锚点重述句会写进
        # `holding 45 percent of frame height`，词形化之后 SCUP 的比例门禁照样解析
        # 得到同一个数（_integer_to_words 的连字符形态就是为此选的），所以这里可以
        # 直接折数字，不必给 IMAGE 开一个例外。
        fixed_image = _digits_to_words(fixed_image)
        fixed_video = self.fix_omni_video(
            i, video_prompt, packet, is_threshold_or_reveal, beat=beat, config=config, family=family)
        return fixed_video, fixed_image

    def validate_beat_prompts(self, i, video_prompt, image_prompt, packet, mode, is_last,
                              is_threshold_or_reveal, prev_video=None, prev_image=None,
                              beat=None, family=None, is_pre_bridge=False,
                              is_post_reveal_cleanup=False):
        ladder = self.ladder_for_beat(beat, is_threshold_or_reveal)
        # 字数硬顶按本 profile 的镜头梯算，不用 base 的一镜到底档 380
        # （见 pp.validate_beat_prompts 的 video_word_limit 说明）。拍型不明时按施工梯。
        _ceiling_ladder = ladder or ladder_for(self.clip_duration(), 'construction')
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
            text = f"{text} {fallback_ladder_clause(ladder)}"
        return text

    # ── omni 自己的 VIDEO 处理 ───────────────────────────────────────────────

    def fix_omni_video(self, i, video_prompt, packet, is_threshold_or_reveal,
                       beat=None, config=None, family=None):
        """omni 版的 VIDEO 确定性修复链。与 base 的差异有四处：字数预算按镜头数缩放
        并在注入后复裁一次、节奏声明换成多镜头版、时间线切点确定性注入、不走 base 的
        out-and-in 兜底（那句会塞进按 8 秒写死的时间戳与 Grid 记号，见模块 docstring）。"""
        ladder = self.ladder_for_beat(beat, is_threshold_or_reveal)
        shot_count = len(ladder or _CONSTRUCTION_LADDERS[6])
        _target, ceiling = video_word_targets(shot_count)
        text = pp.clean_prompt_text(video_prompt)
        # 预压缩显式给下面的结构句注入让出余量，注入完再按硬顶复裁（见 video_draft_budget）。
        text = pp.compress_prompt_to_budget(text, video_draft_budget(shot_count), config,
                                            is_video=True)
        text = pp.fix_video_opening(i, text)
        text = pp.fix_sound_design(text, family=family or 'exterior')
        text = ensure_ladder_out_and_in(text, ladder, packet=packet, beat=beat,
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
        ladder = ladder_for(duration, kind)
        text = _inject_timeline(text, timeline_sentence(duration, ladder))
        if kind == 'construction':
            text = _ensure_pacing(text)
        return text

    def rework_omni_multishot(self, config, i, video_prompt, packet, beat=None):
        """镜头语法定向回炉一轮：只重写 VIDEO，把正文改写成按切点表剪辑的多镜头序列。

        与 base 的结构性回炉同款契约——加法式修改、锚定开场句逐字保留、重写稿必须真的
        通过 omni_video_violations 复验，否则保留原稿（只留痕）。返回
        (video_prompt, 是否采用重写稿)。"""
        multishot_ref = self.reference('omni-multishot-language.md')
        duration = self.clip_duration()
        ladder = self.ladder_for_beat(beat) or ladder_for(duration, 'construction')
        scales = ', '.join(rung.phrase for rung in ladder)
        system = f"""You are rewriting ONE video prompt so it obeys the Gemini Omni multi-shot contract.

{multishot_ref}

Rewrite rules (additive — do not lose content):
- Keep the opening anchor sentence ("Use the provided first frame and last frame as exact composition anchors. ...") VERBATIM as the first sentence.
- Immediately after it, place this shot timeline VERBATIM: "{timeline_sentence(duration, ladder)}"
- Keep every concrete detail already in the draft: the same single worker and costume, the same tool, the same operation, the same persistent traces, the same audio description, the same lighting progression. Redistribute them across the shots instead of inventing new ones.
- Restructure the body into exactly {len(ladder)} shots IN THIS ORDER, naming each scale in prose: {scales}. Join them with clean cuts or match cuts, and open each shot's sentence by restating its cut mark in English words (for example "A clean cut at the one-and-a-half-second mark ...").
- The opening shot equals the beat's starting IMAGE with zero workers and zero progress; the worker enters from a named path; the middle shots carry the work and its physics; the closing shot equals the beat's resulting IMAGE with the worker already gone through a named exit path.
- The shot timeline is the ONLY place arabic digits may appear. Every other count is written in English words.
- NEVER write oner, one-shot, one-take, single continuous take, one continuous take, single take, or unbroken take — there is no exemption.
- Output ONLY the rewritten video prompt body. No headings, no labels, no commentary."""
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
        candidate = pp.fix_video_opening(i, candidate)
        # 拍型从 beat 反推（ladder_kind 的 beat 分支），这样回炉稿也能拿到该有的切点表与
        # 节奏声明；beat 缺失时拍型不明，只做清洗，不硬塞一份可能违约的切点表。
        candidate = self.normalize_omni_video(candidate, beat=beat)
        residual = omni_video_violations(
            candidate, ladder=ladder, duration=duration if beat else None,
            skip_shot_list=is_expanded_transition_stage_beat(beat))
        if [e for e in residual if e.startswith(OMNI_VIDEO_ERROR_PREFIX)]:
            if sys.stdout:
                print(f"[OMNI] Beat {i} 多镜头回炉稿复验未通过，保留原稿（仅留痕）")
            return video_prompt, False
        return candidate, True


def ensure_ladder_out_and_in(video_prompt, ladder, packet=None, beat=None,
                             is_threshold_or_reveal=False):
    """镜头梯版的 worker out-and-in 兜底（约束前移，取代 base 的时间戳版）。

    base 的 fix_out_and_in 在 omni 下整条被跳过（它会塞进按 8 秒写死的 't=0s ... by
    t=7.5s' 和 'Grid C1 edge' —— 两者都违反本档的记号禁用与弹性时长）。跳过之后
    进出这件事就完全没有确定性兜底了，全靠模型自觉：2026-08-02 实测一单 13/16 拍
    在 base 的 check_out_and_in 上报"缺 worker out-and-in"，全部只留痕不回炉。

    这里按镜头梯自己的语义补：工人在**全景/中景**入画、在**结果远景**出画，用镜头梯
    自己的措辞（named path，无时间戳、无 Grid 记号），且必须能被 base 的
    check_out_and_in 认出（它查的是 enter / exits / leaves the frame 这些词）。

    以下情况原样返回：过门拍/兑现拍（契约上就没有工人）、正文已声明画面无人、
    正文里根本没有工人、进出两侧都已经写全、梯里没有结果远景可挂。"""
    text = video_prompt or ''
    if is_threshold_or_reveal or not ladder:
        return text
    low = text.lower()
    sterile_phrases = ('sterile of workers', 'sterile of active workers', 'sterile of any human',
                       'no workers', 'no human presence', 'completely sterile of', 'without any human')
    if any(p in low for p in sterile_phrases):
        return text
    if not any(re.search(rf'\b{w}s?\b', low) for w in ('worker', 'crew', 'person', 'builder', 'laborer')):
        return text
    # 出画镜是结果远景；梯里没有它（过门/兑现梯）就不该由这里补
    if not any(rung.key in ('outro',) for rung in ladder):
        return text

    has_entry = re.search(r'\benters?\b|\bwalks in\b|\bsteps in\b|\benters the frame\b', low) is not None
    has_exit = re.search(r'\bexits?\b|\bwalks out\b|\bleaves? the frame\b|\bsteps out\b', low) is not None
    if has_entry and has_exit:
        return text

    costume = pp._worker_costume_from_packet(packet)
    entry_rung = 'full shot' if any(r.key == 'full' for r in ladder) else 'medium shot'
    clauses = []
    if not has_entry:
        clauses.append(
            f"In the {entry_rung} the same lone worker{costume} enters the frame along a named "
            f"approach path already carrying the tool and material for this beat")
    if not has_exit:
        clauses.append(
            "in the wide outro shot that worker exits the frame back along the same named path "
            "with the tool and the emptied container, leaving the frame completely empty of "
            "workers before the clip ends")
    if not clauses:
        return text
    if text and not text.rstrip().endswith(('.', '!', '?')):
        text = text.rstrip() + '.'
    return (text.rstrip() + ' ' + ', and '.join(clauses) + '.').strip()


def fallback_ladder_clause(ladder):
    """占位兜底稿补的镜头梯声明：一句话里按顺序带齐每一级镜头名与 UGC 拍摄质感，
    让占位稿至少不违反 omni 的镜头语法（占位稿本身仍计入 fallback_count 门禁）。"""
    fragments = [_FALLBACK_SHOT_FRAGMENT[rung.key] for rung in ladder]
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
    """一到二十的独立整数折成英文单词（记号禁用的确定性修复）。

    三处不动：时间线句里的秒数（本次新增的 Timecode exemption）、IMAGE 编号（锚点引用）、
    以及紧贴单位的数字（14mm / 1.6m —— 没有空格，正则本来就不匹配）。"""
    source = text or ''
    timeline = _TIMELINE_RE.search(source)
    if timeline:
        head, tail = source[:timeline.start()], source[timeline.end():]
        return f"{_digits_to_words(head)}{timeline.group(0)}{_digits_to_words(tail)}"

    def replace(match):
        prefix = source[max(0, match.start() - 6):match.start()].lower()
        if prefix.endswith('image '):
            return match.group(0)
        return _integer_to_words(int(match.group(1))) or match.group(0)

    return _DIGIT_COUNT_RE.sub(replace, source)


def _inject_timeline(text, sentence):
    """把切点表钉在锚定开场句之后。模型自己编的时间线一律先清掉——切点表是确定性契约，
    不是模型的创作空间。"""
    body = _TIMELINE_RE.sub(' ', text or '')
    body = re.sub(r'\s{2,}', ' ', body).strip()

    marker = 'third layout.'
    index = body.lower().find(marker)
    if index != -1:
        head = body[:index + len(marker)]
        tail = body[index + len(marker):].lstrip()
        return f"{head} {sentence} {tail}".strip()
    # 锚定句还没注入（回炉稿/兜底稿在 fix_video_opening 之前）：先放最前面，
    # 后续的 fix_video_opening 会把锚定句补到它之前。
    return f"{sentence} {body}".strip()


def _ensure_pacing(video_prompt):
    """普通施工拍补齐 omni 的节奏声明与镜内连续性声明（缺了才补，不重复注入）。"""
    text = video_prompt or ''
    if OMNI_PACING_MARKER not in text.lower():
        if text and not text.endswith(('.', '!', '?')):
            text += '.'
        text = f"{text} {OMNI_PACING_PHRASE}".strip()
    if OMNI_INSHOT_MARKER not in text.lower():
        if text and not text.endswith(('.', '!', '?')):
            text += '.'
        text = f"{text} {OMNI_INSHOT_PHRASE}".strip()
    return text
