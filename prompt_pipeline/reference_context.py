"""Shared scene context, local reference selection, and banned-element checks."""

import re
import sys


def _num(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default

def _stage_text(fact):
    """一条逐帧读数里能反映「施工到哪一步」的那几栏。

    `completion_extent` 是 Pass A 专门为这件事写的一栏（"Natural cavern basin intact
    with pool undisturbed" / "Complete room fit-out with all structural, mechanical..."），
    比 `subject`（写的是「人在干什么」）稳得多；两栏都收，缺一栏不影响判读。
    """
    if isinstance(fact, str):
        return fact
    if not isinstance(fact, dict):
        return ''
    parts = [str(fact.get('completion_extent') or ''),
             str(fact.get('subject') or fact.get('description') or '')]
    return ' '.join(p for p in parts if p)

_LATE_STAGE_RE = re.compile(
    r'\b(?:finished|completed|complete|fully\s+(?:finished|built|installed|fitted|furnished|clad)|'
    r'furnished|furniture|cabinetry|fit-?out|fitted\s+out|move-?in|habitable|staged|decorated|'
    r'installed|cladding|clad|panell?ed|painted|glazed|shingled?|roof\s+deck|flooring\s+laid|'
    r'lighting\s+(?:installed|fixtures?)|light\s+fixtures?|final\s+state)\b',
    re.IGNORECASE)

_EARLY_STAGE_RE = re.compile(
    r'\b(?:untouched|undisturbed|intact|natural|wild|overgrown|pristine|virgin|derelict|abandoned|'
    r'ruined|empty|vacant|unworked|uncleared|before\s+any\s+work|'
    r'no\s+(?:work|construction|equipment|structure)|'
    r'bare\s+(?:ground|earth|soil|rock|site|floor|terrain)|raw\s+(?:ground|earth|site)|'
    r'debris|rubble|spoil|mud|silt|standing\s+water|stagnant|puddle|weeds?|brush|'
    r'clearing|excavat\w*|marking|layout\s+line|staking|survey\w*|demoli\w*|stripping|'
    r'foundation|footing|framing|rough-?in)\b',
    re.IGNORECASE)

def completion_score(text):
    """一段读数的「完工度」粗分：晚期词计正、早期词计负。

    绝对值没有意义（词表长度本身就带偏），只用来跟同一支片子里的另一帧比大小。
    """
    if not text:
        return 0
    return len(_LATE_STAGE_RE.findall(text)) - len(_EARLY_STAGE_RE.findall(text))

_TEASER_SCORE_GAP = 2

_TEASER_LOOKAHEAD = 5

_TEASER_FLASH_WINDOW_SECONDS = 2.0

_TEASER_MAX_SKIP = 2

def is_teaser_flash_frame(f0_text, f_subsequent_texts):
    """首帧是不是短视频常见的完工/半成品先导钩子闪帧 (Teaser Flash)。

    两条判据取或：

      · 旧的词表判据（原样保留）：首帧命中完工词表 **且** 后续帧命中放线/未开发词表。
        两张表是 2026-08-21 那一单现拧出来的英文关键词，命中即算数——这里只做加法，
        不动它，免得当初钉住的那一单又漏回去。
      · 新的阶段落差判据：首帧的完工度分数比后续几帧高出 `_TEASER_SCORE_GAP`。词表判据
        要求两张窄表**同时**命中，一支海蚀洞抽水清淤的片子（后续帧读数是 pump /
        suction hose / squeegee，一个放线词都没有）就算首帧真是完工闪帧也判不出来；
        落差判据只问「首帧是不是明显比后面几帧完工」，不依赖题材词汇。
    """
    if not f0_text or not f_subsequent_texts:
        return False

    adv_patterns = [
        r'\b(shelter|shingle|window|retaining|shoring|plank shoring|clad in|paneled|roof deck|bunker|cabin|suite|completed|furnished|welded|aquarium)\b'
    ]
    layout_patterns = [
        r'\b(marking can|spraying|spray paint|marking line|mineral powder|powder from a bag|layout line|aerosol|dispensing white|running downward|walks across a grassy field|clearing grass|cutting sod|stripping turf|natural wild|undisturbed|bare ground)\b'
    ]

    f0_has_adv = any(re.search(p, f0_text, re.I) for p in adv_patterns)
    subs_have_layout = any(re.search(p, text, re.I) for text in f_subsequent_texts for p in layout_patterns)
    if f0_has_adv and subs_have_layout:
        return True

    lead = completion_score(f0_text)
    if lead < 1:
        return False
    follow = max((completion_score(t) for t in f_subsequent_texts[:_TEASER_LOOKAHEAD]), default=0)
    return (lead - follow) >= _TEASER_SCORE_GAP

def opening_anchor_skip(entries, max_skip=_TEASER_MAX_SKIP):
    """开场锚点该跳过开头的几张先导闪帧。0 = 首帧就是真起点。

    这是全仓唯一一份先导帧判据。四个注入点（组稿锚点对齐、组稿收尾对帧订正、渲染期
    链路守卫对标帧、4选1 打分基准）过去各写各的取帧，只有第一个装了这道护栏，于是它
    前脚跳过的闪帧，后脚被另外三个原样喂回 IMG 001——用户看到的就是「帧序列第一帧又
    开始读爆款视频的首帧」。口径收在这里，四处一起走。

    entries: [{'name': 帧名, 'timestamp': 秒, 'text': 读数文本}, …]，按时间升序。
    """
    rows = [e for e in (entries or []) if isinstance(e, dict)]
    if len(rows) < 2:
        return 0

    skip = 0
    while skip < min(max_skip, len(rows) - 1):
        nxt = rows[skip + 1]
        # 闪帧窗：跳完之后落脚的那一帧仍必须在片头一瞬之内。
        if _num(nxt.get('timestamp'), default=1e9) > _TEASER_FLASH_WINDOW_SECONDS:
            break
        f0_text = rows[skip].get('text') or ''
        f_subs = [r.get('text') or '' for r in rows[skip + 1:skip + 1 + _TEASER_LOOKAHEAD]]
        if not is_teaser_flash_frame(f0_text, f_subs):
            break
        if sys.stdout:
            print(f'[REVERSE] 检测到片头先导钩子帧 (Teaser Flash: {rows[skip].get("name")})，'
                  f'跳过并顺延到 {nxt.get("name")}')
        skip += 1
    return skip

def select_opening_anchor(names, facts_by_name, timestamps=None):
    """一串按时间排好的候选帧名 → 真正的开场锚点帧名。

    `names` 为空返回 None。读不到任何读数时一律返回 names[0]：判不出是不是闪帧就别跳，
    宁可读原片首帧，也不能凭空往后挪一帧。

    timestamps: {帧名: 秒}。不给就从 facts_by_name 里取；两处都没有时按 0 处理——闪帧窗
    于是恒为真，判据完全落在阶段落差上（老单的 coverage 不带时间戳）。
    """
    names = [str(n) for n in (names or []) if n]
    if not names:
        return None
    facts_by_name = facts_by_name or {}
    timestamps = timestamps or {}
    entries = []
    for n in names:
        fact = facts_by_name.get(n)
        ts = timestamps.get(n)
        if ts is None and isinstance(fact, dict):
            ts = fact.get('timestamp')
        entries.append({'name': n, 'timestamp': _num(ts, default=0.0),
                        'text': _stage_text(fact)})
    if not any(e['text'] for e in entries):
        return names[0]
    return names[opening_anchor_skip(entries)]

def scene_constants_lines(constants, signature=None):
    """把场景恒常信息摊平成给提示词用的行。

    两个来源有意并存，因为它们的失效方式相反：`scene_constants` 是本地统计，绝不会
    凭空捏造，但措辞是从帧事实里挑的、偏碎（"dark ceiling surface"）；`signature` 是
    模型写的整体基调，读起来像人话，但它可能润色。碎而可靠的那份负责兜底，整体那份
    负责让写手知道这是个什么地方。
    """
    lines = []
    text = str(signature or '').strip()
    if text:
        lines.append(f'the place itself: {text}')
    if isinstance(constants, list):
        for item in constants:
            s = str(item).strip()
            if s:
                lines.append(f'always-present scene landmark: {s}')
        return lines
    if isinstance(constants, dict):
        labels = (('environment', 'always-present macro environment & biome'),
                  ('materials', 'always-present materials and surfaces'),
                  ('traces', 'always-present marks and weathering'),
                  ('fixtures_in_shot', 'equipment permanently in shot'),
                  # 这一栏是「同一个人从头到尾」，不是「有个人在」。措辞里的 never re-cast
                  # 是给写手的：合成侧据此要求每一条 IMAGE/VIDEO 复述同一份外形
                  # （见 BaseComposer.scene_constants_block 的 cast_rule）。
                  ('cast', 'the same living cast in every shot, never re-cast — '
                           'appearance is fixed, only the pose changes'),
                  ('grade', 'the film\'s photographic grade, identical in every frame'),
                  # 与 motion 那一栏同构：那条说「一直在动」，这条说「一直在响」。合成侧据此
                  # 要求每一条 VIDEO 的环境声都落在这上面（见 BaseComposer.scene_constants_block）。
                  ('ambient_sound', 'audible under every shot with nobody making it'),
                  # 这一栏与上面四栏的动词不同：它们是「在」，这一栏是「在动」。合成侧据此
                  # 要求每一条 VIDEO 都让它继续动（见 BaseComposer.scene_constants_block）。
                  ('motion', 'never stops moving anywhere in the film'))
        from .human_cast import humanize_cast_list
        for key, label in labels:
            items = [str(x).strip() for x in (constants.get(key) or []) if str(x).strip()]
            # 活物一律真人。attach_scene_constants 落地时已经归一过一次，这里再来一道
            # 是因为这一栏之后还会被人在卡点上手改、也可能来自本次改动之前存下的旧任务；
            # 送进提示词的那一份必须是真人措辞，不能指望上游都过过手。幂等，重复调无害。
            if key == 'cast':
                items = humanize_cast_list(items)
            if items:
                lines.append(f'{label}: ' + '; '.join(items))
    return lines

def banned_element_hits(prompt_block, banned_elements):
    """P0 门禁用：提示词整块里命中的 banned 元素。命中即交付前必须重写。

    使用字词边界正则匹配，防止英文短词做子串包含时的假阳性误判
    （如 'woven' 误判为 'oven'、'embedded' 误判为 'bed'、'carpet' 误判为 'car'、
    'bark' 误判为 'bar'、'spot' 误判为 'pot' 等）。中文字符保留自然子串匹配。
    """
    if not prompt_block or not banned_elements:
        return []
    text = str(prompt_block)
    hits = []
    for item in banned_elements:
        needle = str(item).strip()
        if not needle:
            continue
        pattern = re.escape(needle)
        prefix = r'\b' if re.match(r'^[a-zA-Z0-9_]', needle) else ''
        suffix = r'\b' if re.search(r'[a-zA-Z0-9_]$', needle) else ''
        regex = f'{prefix}{pattern}{suffix}'

        # 特例防假阳性：
        # 'oven' 旨在拦截现代厨房家用电器（residential appliances），
        # 但在荒野庇护所/泥工搭建场景下，原生态手工泥塑烤炉/火塘（domed oven / cob oven / clay oven / earthen oven / wood-fired oven）
        # 属于真实的土工/砌筑工序，并非幻觉出的现代厨房电器。
        if needle.lower() == 'oven':
            matches = list(re.finditer(regex, text, re.IGNORECASE))
            real_hits = []
            for m in matches:
                before = text[max(0, m.start() - 30):m.start()].lower()
                if re.search(r'\b(cob|clay|earth|earthen|mud|brick|stone|wood-fired|wood fired|domed)\s*$', before):
                    continue
                real_hits.append(m)
            if real_hits:
                hits.append(item)
            continue

        if re.search(regex, text, re.IGNORECASE):
            hits.append(item)
    return hits
