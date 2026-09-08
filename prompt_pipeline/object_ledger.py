"""物件账本 —— 复刻/变体线唯一的 spec 层硬闸。

## 为什么不复用 frame_state.validate_frame_state_contract

那道闸是**母本线**（outline / milestone 阶梯）的，判据吃的是 `before_state` /
`after_state` / `package_operations` / `changed_grid_cells`。复刻线的 beats 是另一套
schema（`state_before` / `state_after` / `visible_action`），而且根本没有
`package_operations` —— 直接接上去的话每一拍都会挨一条 "declares 0 operations"，
一百条假报错淹掉真问题。所以这里另起一道**只判物件账**的闸。

## 它判什么（三条规则，两档 severity）

2026-09-03 那批海蚀洞变体（20 图 + 19 视频）里，下面三类问题一路走到了成片，
`chain_guard` 一条都没拦住 —— 它只比对相邻两帧像素，看不见跨十几拍的账目缺口：

  1. **凭空出现 (phantom)**：窗户在 IMAGE 9 第一次作为 locked anchor 出现，此后
     被 10~20 共 12 张图当锚点继承，但**没有任何一拍建造过它**；外墙包覆那一拍
     的范围还明写着只有左墙和前舱壁，右墙自始至终不存在。同类的还有门框企口、
     水槽、厨房岛台。
  2. **回归 (regression)**：VIDEO 9 让工人「用铝耙找平碎石」，而碎石层在 BEAT 3
     就已经是压实找平的终态了。所有图片提示词都写着 "no regression of any
     previously completed feature"，那句话是写给模型看的，没有人在代码里判过。
  3. **依赖倒置 (inversion)**：烟囱穿顶领圈在 IMAGE 9 已经存在，IMAGE 12 才
     "roughed in"，IMAGE 18 才密封。

三条都在 spec 层判 —— 这时候一张图都还没抽，改的代价是零。

判死还是记诊断，看的是**这条问题是不是变异造出来的**：`validate_object_ledger` 收下
母本的阶梯（`baseline_beats=`）之后，母本自身就有的缺口一律降级为 warning，只有变体
新引入的才拦单。另外，规则 3 前半那张角色依赖表写的是常规工序顺序而不是物理定律，
它恒为 warning——细节见 `ROLE_DEPENDENCIES` 与 `validate_object_ledger` 的注释。

## 判据的克制

物件识别走**受控词表**，不做通用名词短语抽取：后者在建造散文里的假阳性率高到没法
当硬闸用（"the worker's shoulder"、"a moment of stillness" 都会被抽成物件）。词表
只收**建造产物**，工具与耗材单列白名单——工具本来就该反复出现，把它算进账里会让
每一把锤子都变成一次「凭空出现」。
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from .ontology import CONSTRUCTION_ROLES, UNKNOWN_ROLE, infer_role

# ── 建造产物词表 ─────────────────────────────────────────────────────────────
#
# canonical_id → (匹配正则, role)。一个 id 收齐它在散文里的所有变体写法，这样
# "vapor barrier" / "vapour barrier membrane" / "black poly sheeting" 是同一个物件，
# 不会被读成三次凭空出现。
#
# **只收建造产物**。工具、耗材、人、天气、光线一律不进这张表 —— 见模块 docstring
# 末段。新增条目时先问一句：它是不是「一旦装上就必须在后续每一帧里继续存在」的东西？
# 不是的话它不属于这里。
# 复数一律显式写出 `(?:s|es)?`。少写一次的代价很具体：`portal\s+arch\b` 匹配不到
# "portal arches"，于是主体骨架的 introduced_at 从第 4 拍漂到第 12 拍，围护件跟着被
# 判成依赖倒置——一条假报错，而真正的窗户凭空出现反而淹在里面（2026-09-03 首轮自测）。
_P = r'(?:e?s)?'

_OBJECT_VOCAB: List[Tuple[str, str, str]] = [
    # (canonical_id, pattern, role)
    ('sub_base',      rf'\b(?:sub-?base{_P}|sub-?floor\s+(?:bed|layer){_P}|aggregate\s+(?:bed|layer|sub-?base){_P}|hardcore|ballast\s+bed{_P}|crushed\s+\w+\s+(?:bed|layer){_P})\b', 'subfloor'),
    # `screed` 在建造散文里多半是**动词**（"distribute, screed, and tamp the gravel"），
    # 收裸词会让每一条摊铺拍都凭空长出一层找平层。只收它作名词的写法。
    ('screed',        rf'\b(?:screed\s+layer{_P}|screed\s+bed{_P}|levell?ing\s+layer{_P}|floor\s+screed{_P})\b', 'subfloor'),
    ('structural_frame', rf'\b(?:portal\s+(?:arch|frame){_P}|structural\s+frame{_P}|post-?and-?beam|stud\s+wall{_P}|wall\s+framing|framing\s+cage{_P}|roof\s+truss{_P}|rafter{_P}|wall\s+upright{_P})\b', 'structure'),
    ('ceiling_rib',   rf'\b(?:ceiling\s+(?:rib|arch|batten){_P}|curved\s+rib{_P}|barrel\s+arch{_P})\b', 'structure'),
    ('exterior_cladding', rf'\b(?:exterior\s+cladding|exterior\s+panel{_P}|siding|shell\s+plate{_P}|cladding\s+plate{_P}|riveted\s+\w+\s+(?:panel|plate){_P})\b', 'enclosure'),
    ('opening',       rf'\b(?:door\s+opening{_P}|portal\s+opening{_P}|aperture{_P}|wall\s+cut-?out{_P}|window\s+cut-?out{_P}|rebate{_P})\b', 'opening'),
    ('window',        rf'\b(?:window{_P}|glazing\s+unit{_P}|glazed\s+(?:panel|unit){_P}|casement{_P})\b', 'window'),
    ('door_leaf',     rf'\b(?:door\s+lea(?:f|ves)|swinging\s+door{_P}|cargo\s+door{_P}|entry\s+door{_P}|double\s+door{_P}|door\s+panel{_P})\b', 'door'),
    ('door_hardware', rf'\b(?:strap\s+hinge{_P}|cam-?latch{_P}|locking\s+bar{_P}|espagnolette{_P}|door\s+handle{_P}|weatherseal{_P}|compression\s+seal{_P})\b', 'door'),
    ('sill_plate',    rf'\b(?:sill\s+plate{_P}|threshold\s+plate{_P}|door\s+sill{_P})\b', 'door'),
    ('vapour_barrier', rf'\b(?:vapou?r\s+barrier{_P}|damp-?proof\s+membrane{_P}|breather\s+membrane{_P}|poly\s+sheeting|barrier\s+membrane{_P})\b', 'membrane'),
    ('seam_tape',     rf'\b(?:seam\s+tape|foil\s+tape|sealing\s+tape)\b', 'membrane'),
    ('conduit',       rf'\b(?:conduit{_P}|wiring\s+run{_P}|cable\s+run{_P}|junction\s+box{_P}|electrical\s+rough-?in)\b', 'service'),
    ('power_source',  rf'\b(?:solar\s+panel{_P}|battery\s+bank{_P}|generator{_P}|shore\s+power|mains\s+hook-?up|charge\s+controller{_P})\b', 'service'),
    ('plumbing',      rf'\b(?:water\s+tank{_P}|freshwater\s+tank{_P}|shut-?off\s+valve{_P}|supply\s+line{_P}|waste\s+pipe{_P}|greywater)\b', 'service'),
    # 裸 `basin` 不收：母本的「rock basin」是地貌，收了会让每一条排水拍都凭空长出一个水槽。
    ('sink',          rf'\b(?:sink{_P}|wash\s*basin{_P})\b', 'cabinetry'),
    ('flue_collar',   rf'\b(?:flue\s+(?:collar|sleeve|penetration){_P}|chimney\s+(?:collar|penetration){_P}|roof\s+penetration{_P})\b', 'heating'),
    ('flue_pipe',     rf'\b(?:flue\s+pipe{_P}|chimney\s+pipe{_P}|twin-?wall\s+flue{_P}|stove\s+pipe{_P})\b', 'heating'),
    ('stove',         rf'\b(?:wood-?burning\s+stove{_P}|cast-?iron\s+stove{_P}|stove{_P}|hearth{_P}|firebox{_P}|masonry\s+heater{_P})\b', 'heating'),
    ('floor_batten',  rf'\b(?:floor\s+(?:batten|sleeper){_P}|sleeper\s+(?:grid|bay){_P}|noggin{_P}|counter-?batten{_P}|joist\s+grid{_P})\b', 'batten'),
    ('insulation',    rf'\b(?:rockwool|mineral\s+wool|insulation\s+batt{_P}|wool\s+batt{_P}|aerogel\s+(?:layer|blanket){_P}|wood-?fibre\s+board{_P})\b', 'insulation'),
    ('sheathing',     rf'\b(?:plywood\s+(?:sheathing|backing|panel){_P}|birch\s+plywood|osb\s+board{_P}|backing\s+panel{_P}|structural\s+backing)\b', 'sheathing'),
    ('finish_floor',  rf'\b(?:floorboard{_P}|finish(?:ed)?\s+floor{_P}|tongue-?and-?groove\s+floor{_P}|hardwood\s+floor{_P}|floor\s+plank{_P})\b', 'flooring'),
    ('interior_lining', rf'\b(?:slat\s+(?:lining|cladding)|interior\s+lining|wall\s+lining|panel(?:ling|ing)|wainscot{_P}|fluted\s+\w+\s+slat{_P})\b', 'cladding'),
    ('protective_coating', rf'\b(?:passivator{_P}|clear\s+coat{_P}|protective\s+coating{_P}|sealer\s+coat{_P}|hardwax\s+oil|limewash|varnish{_P})\b', 'coating'),
    ('paving',        rf'\b(?:flagstone{_P}|paving\s+slab{_P}|paver{_P}|patio\s+landing{_P}|entry\s+landing{_P}|walkway\s+slab{_P})\b', 'paving'),
    ('scrape_grate',  rf'\b(?:scrape\s+grate{_P}|entry\s+grate{_P}|perforated\s+\w+\s+grate{_P}|boot\s+grate{_P})\b', 'paving'),
    ('cabinetry',     rf'\b(?:base\s+cabinet{_P}|cabinet\s+carcass{_P}|kitchenette{_P}|cabinetry|worktop{_P}|countertop{_P}|joinery\s+unit{_P})\b', 'cabinetry'),
    ('kitchen_island', rf'\b(?:kitchen\s+island{_P}|island\s+unit{_P})\b', 'cabinetry'),
    ('workbench',     rf'\b(?:workbench{_P}|fold-?down\s+bench{_P}|butcher-?block\s+bench{_P})\b', 'cabinetry'),
    ('bed_platform',  rf'\b(?:platform\s+bed{_P}|bed\s+frame{_P}|sleeping\s+platform{_P}|berth{_P})\b', 'furnishing'),
    ('lighting',      rf'\b(?:led\s+(?:strip|cove){_P}|cove\s+lighting|under-?cabinet\s+light{_P}|light\s+fitting{_P}|luminaire{_P})\b', 'service'),
]

_OBJECT_PATTERNS: List[Tuple[str, re.Pattern, str]] = [
    (oid, re.compile(pat, re.I), role) for oid, pat, role in _OBJECT_VOCAB
]

OBJECT_ROLE: Dict[str, str] = {oid: role for oid, _pat, role in _OBJECT_VOCAB}

# role → 该角色下的全部 canonical id。申报值归一要用它。
#
# 为什么需要：申报字段里写的可能是 role 名（'structure'），而账本比对的是 canonical id
# （'structural_frame'），两个命名空间几乎不相交。不打通的话，「申报式账本可以精确
# 拦单」这句话是假的——申报的东西一个也对不上，比对退化成纯散文推断，却按 blocking 判。
ROLE_OBJECTS: Dict[str, Tuple[str, ...]] = {}
for _oid, _pat, _role in _OBJECT_VOCAB:
    ROLE_OBJECTS[_role] = ROLE_OBJECTS.get(_role, ()) + (_oid,)

# 工具 / 耗材 / 活物 / 天候 —— 反复出现是正常的，不进账。
_NON_PRODUCT = re.compile(
    r'\b(?:rake|mallet|hammer|driver|drill|wrench|grinder|saw|chisel|trowel|brush|'
    r'squeegee|pry\s*bar|level|square|tape\s+measure|nailer|rivet\s+gun|stapler|'
    r'tacker|roller|bucket|drum|crate|tool\s*box|worker|builder|hand|boot|jacket|'
    r'daylight|sunlight|overcast|horizon|ocean|surf|wind|dust|sawdust)\b', re.I)


def extract_objects(*texts: Any) -> Set[str]:
    """一段（或几段）散文里出现的建造产物 canonical id 集合。"""
    blob = ' '.join(_flatten_text(t) for t in texts)
    if not blob.strip():
        return set()
    found: Set[str] = set()
    for oid, pattern, _role in _OBJECT_PATTERNS:
        if pattern.search(blob):
            found.add(oid)
    return found


def _flatten_text(value: Any) -> str:
    if value is None:
        return ''
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return ' '.join(_flatten_text(v) for v in value.values())
    if isinstance(value, (list, tuple, set)):
        return ' '.join(_flatten_text(v) for v in value)
    return str(value)


# ── 一拍的三种文本面 ────────────────────────────────────────────────────────
#
# 账本的判据全靠把一拍的文字分成三面。混在一起读的话，「本拍新建的」和「本拍继承的」
# 就分不开，凭空出现这条根本无从判起。
def _produced_text(beat: Dict[str, Any]) -> str:
    """本拍**产出**的东西：终态、可见成果、留存痕迹。"""
    return ' '.join(_flatten_text(beat.get(k)) for k in (
        'state_after', 'after_state', 'visible_result', 'visible_details',
        'persistent_traces', 'milestone_name', 'completion_extent',
    ))


def _inherited_text(beat: Dict[str, Any]) -> str:
    """本拍**继承**的东西：起始态、锁定锚点、继承声明。"""
    return ' '.join(_flatten_text(beat.get(k)) for k in (
        'state_before', 'before_state', 'inherited_state', 'locked_anchors',
        'preserve_state', 'anchors',
    ))


def _action_text(beat: Dict[str, Any]) -> str:
    """本拍**动作**触及的东西。"""
    return ' '.join(_flatten_text(beat.get(k)) for k in (
        'visible_action', 'description', 'operation',
    ))


def _beat_index(beat: Dict[str, Any], position: int) -> int:
    try:
        return int(beat.get('index') or beat.get('beat') or position)
    except (TypeError, ValueError):
        return position


# ── 账本 ─────────────────────────────────────────────────────────────────────
def build_object_ledger(beats: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """物件 → {'role', 'introduced_at', 'completed_at', 'referenced_at', 'acted_at'}。

    `introduced_at` 只认**产出面**：一个物件是在它第一次出现在某拍的终态/成果里时
    才算被建造出来的。出现在起始态或锚点里不算 —— 那正是「凭空出现」要抓的东西。
    """
    ledger: Dict[str, Dict[str, Any]] = {}
    for position, beat in enumerate(beats or [], start=1):
        if not isinstance(beat, dict):
            continue
        idx = _beat_index(beat, position)
        produced = extract_objects(_produced_text(beat))
        inherited = extract_objects(_inherited_text(beat))
        acted = extract_objects(_action_text(beat))

        for oid in produced | inherited | acted:
            entry = ledger.setdefault(oid, {
                'role': OBJECT_ROLE.get(oid, 'structure'),
                'introduced_at': None,
                'completed_at': None,
                'referenced_at': [],
                'acted_at': [],
            })
            entry['referenced_at'].append(idx)

        for oid in produced:
            entry = ledger[oid]
            if entry['introduced_at'] is None:
                entry['introduced_at'] = idx
            entry['completed_at'] = idx

        for oid in acted:
            ledger[oid]['acted_at'].append(idx)

    return ledger


# ── 角色依赖图 ───────────────────────────────────────────────────────────────
#
# role → 它必须排在**之后**的那些 role。只有当两个 role 在同一条梯子里都出现过时
# 才判 —— 缺席不算倒置（不同材质的工序数天然不同）。
#
# **这张表判出来的是 warning，不是 blocking。** 它写的是一套常规工序顺序，而不是
# 物理定律：先立架后做排水垫层的井屋、先挂舱门后开窗洞的舱体，都真实存在且成立。
# 拿它拦交付的代价实测过——仓库里唯一那份已交付母本
# （`outputs/replica_jobs/replica_1ef74020a25b`，17 拍）照它判死三条，没有一条是
# 复盘里那三类硬伤。判死的是「这条梯子不标准」，不是「变异弄坏了什么」。
ROLE_DEPENDENCIES: Dict[str, Tuple[str, ...]] = {
    'subfloor':   ('demolition',),
    'structure':  ('subfloor',),
    'enclosure':  ('structure',),
    'roof':       ('structure',),
    'opening':    ('enclosure',),
    'window':     ('opening',),
    'door':       ('opening',),
    'membrane':   ('enclosure',),
    'batten':     ('membrane',),
    'insulation': ('batten',),
    'sheathing':  ('insulation',),
    'flooring':   ('batten',),
    'cladding':   ('sheathing',),
    'coating':    ('enclosure',),
    'heating':    ('flooring',),
    'cabinetry':  ('flooring',),
    'furnishing': ('cabinetry',),
    'hero':       ('furnishing',),
}

# 物件级硬依赖：「没有前者，后者物理上不可能存在」的对子，缺席也要报。
#
# 语义是**任选其一**（any-of），不是全部：一道门洞可以由显式的 opening 交付，也可以
# 由主体骨架本身带出来（母本那三道 portal arch 就自带门洞）。写成 all-of 的话，
# 凡是没单独给洞口开一拍的正常梯子都会挨一条假报错——而真正的窗户凭空出现由规则 1
# 抓，本来就不依赖这里。保持极小，每加一条都是一次可能的误伤。
HARD_OBJECT_DEPENDENCIES: Dict[str, Tuple[str, ...]] = {
    'window':      ('opening', 'structural_frame'),
    'door_leaf':   ('opening', 'structural_frame'),
    'lighting':    ('conduit', 'power_source'),
    'sink':        ('plumbing',),
}
# 这里曾经还有一条 `flue_pipe: (flue_collar,)`。删掉了：一段烟囱管当然穿过某个领圈，
# 但领圈**被单独描写过**不是物理必然，它多半只是没被反推捞出来。实测里它拦下的是
# 「材质包给了 twin-wall flue、却没有哪句话提到 collar」这种纯词表缺口。复盘里真正
# 的那条烟囱缺陷（第 9 拍已存在、第 12 拍才 roughed in）是**顺序**问题，由规则 1
# 的凭空出现抓，从来不依赖这条。

_REGRESSION_VERBS = re.compile(
    r'\b(?:re-?level|re-?levels|levell?ing|re-?grade|re-?spread|re-?rake|rakes?|'
    r'sweeps?\s+out|clears?\s+out|strips?\s+(?:back|off)|dismantles?|removes?|'
    r'tears?\s+(?:out|down)|excavates?|re-?laying|re-?lays?)\b', re.I)


def validate_object_ledger(beats: List[Dict[str, Any]],
                          *,
                          baseline_beats: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """物件账三条规则。返回 [{'rule','severity','beat','object','message'}, ...]。

    ``baseline_beats``：母本的节拍阶梯。给了它，闸判的就从「这条梯子合不合规」变成
    **「变异弄坏了什么」**——母本自己就有的那些缺口降级为 warning 并标 ``inherited``，
    只有变体新引入的才判死。

    这个区别不是宽严之争，是判据对不对的问题。母本是原片的反推记录，它的账不平通常
    意味着反推漏了一拍（原片里做过的事没被聚出来），那是母本线要修的东西；变体 1:1
    继承母本的骨架，必然把同一个缺口原样带下来。拿它拦变体，拦下的是母本的旧账，而
    真正该拦的「变异新造出来的窗户」反而淹在里面。实测：仓库里唯一那份已交付母本
    （17 拍）生成变体时被判死 3 条，全部是母本自带、且全部不是复盘里那三类硬伤。

    severity 分两档：

      · ``blocking`` —— 证据自相矛盾：凭空继承（规则 1）、完工回归（规则 2）、
        物件级硬依赖缺位（规则 3 后半）。
      · ``warning``  —— 顺序惯例：角色依赖倒置（规则 3 前半，见 ROLE_DEPENDENCIES
        的注释），以及一切从母本继承下来的问题。

    软信号走 :func:`diagnose_object_ledger`，不拦单。
    """
    beats = [b for b in (beats or []) if isinstance(b, dict)]
    if not beats:
        return []
    ledger = build_object_ledger(beats)
    violations: List[Dict[str, Any]] = []

    # ── 规则 1：凭空出现 ──────────────────────────────────────────────────
    # 一个物件在第 N 拍被当成**已经存在**（起始态 / 锁定锚点）来引用，却没有任何
    # 一拍 M < N 把它建造出来。
    for position, beat in enumerate(beats, start=1):
        idx = _beat_index(beat, position)
        inherited = extract_objects(_inherited_text(beat))
        for oid in sorted(inherited):
            intro = ledger.get(oid, {}).get('introduced_at')
            if intro is None:
                violations.append({
                    'rule': 'phantom', 'severity': 'blocking', 'beat': idx, 'object': oid,
                    'message': (f'第 {idx} 拍把「{oid}」当作已存在的锚点继承，但整条阶梯'
                                f'没有任何一拍建造过它。'),
                })
            elif intro > idx:
                violations.append({
                    'rule': 'phantom', 'severity': 'blocking', 'beat': idx, 'object': oid,
                    'message': (f'第 {idx} 拍继承了「{oid}」，但它要到第 {intro} 拍才被'
                                f'建造出来。'),
                })

    # ── 规则 2：已完成回归 ────────────────────────────────────────────────
    # 一个物件在第 M 拍完工之后，又在第 N > M 拍被动作重新加工。
    for position, beat in enumerate(beats, start=1):
        idx = _beat_index(beat, position)
        action = _action_text(beat)
        if not _REGRESSION_VERBS.search(action):
            continue
        for oid in sorted(extract_objects(action)):
            entry = ledger.get(oid) or {}
            intro = entry.get('introduced_at')
            # 本拍自己就是它的产出拍 → 这是它的建造过程，不是回归。
            if intro is None or intro >= idx:
                continue
            # 破拆/清场拍天然要动已有的东西。
            if infer_role(beat) in ('demolition', 'site'):
                continue
            violations.append({
                'rule': 'regression', 'severity': 'blocking', 'beat': idx, 'object': oid,
                'message': (f'第 {idx} 拍的动作又去加工「{oid}」，而它在第 {intro} 拍'
                            f'已经是完工终态了。'),
            })

    # ── 规则 3：依赖倒置 ──────────────────────────────────────────────────
    role_first: Dict[str, int] = {}
    for oid, entry in ledger.items():
        intro = entry.get('introduced_at')
        if intro is None:
            continue
        role = entry.get('role') or 'structure'
        if role not in role_first or intro < role_first[role]:
            role_first[role] = intro

    for role, requires in ROLE_DEPENDENCIES.items():
        here = role_first.get(role)
        if here is None:
            continue
        for need in requires:
            there = role_first.get(need)
            if there is None:
                continue  # 缺席不算倒置，见 ROLE_DEPENDENCIES 的注释
            if there > here:
                # 顺序惯例，不是物理定律 —— warning，见 ROLE_DEPENDENCIES 的注释。
                violations.append({
                    'rule': 'inversion', 'severity': 'warning', 'beat': here, 'object': role,
                    'message': (f'角色「{role}」在第 {here} 拍就出现，但常规工序里它排在'
                                f'「{need}」之后，而后者要到第 {there} 拍才出现。'),
                })

    for oid, alternatives in HARD_OBJECT_DEPENDENCIES.items():
        here = (ledger.get(oid) or {}).get('introduced_at')
        if here is None:
            continue
        intros = [(ledger.get(need) or {}).get('introduced_at') for need in alternatives]
        satisfied = [n for n in intros if n is not None and n <= here]
        if satisfied:
            continue
        present = [n for n in intros if n is not None]
        names = '/'.join(alternatives)
        if present:
            violations.append({
                'rule': 'inversion', 'severity': 'blocking', 'beat': here, 'object': oid,
                'message': (f'第 {here} 拍装了「{oid}」，但它必需的「{names}」要到'
                            f'第 {min(present)} 拍才出现。'),
            })
        else:
            violations.append({
                'rule': 'inversion', 'severity': 'blocking', 'beat': here, 'object': oid,
                'message': f'第 {here} 拍装了「{oid}」，但它必需的「{names}」从未被建造。',
            })

    return _dedupe(_demote_inherited(violations, baseline_beats))


def _demote_inherited(violations: List[Dict[str, Any]],
                      baseline_beats: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """母本自己就有的问题降级为 warning。

    按 (rule, object) 配对而不是 (rule, beat, object)：变体的拍号会漂（同一个构件可能
    落在相邻的另一拍上），拿拍号配对等于配不上，降级就永远不会发生。
    """
    if not baseline_beats:
        return violations
    inherited = {(v.get('rule'), v.get('object'))
                 for v in validate_object_ledger(baseline_beats)}
    if not inherited:
        return violations
    out = []
    for item in violations:
        if (item.get('rule'), item.get('object')) in inherited:
            item = dict(item)
            item['severity'] = 'warning'
            item['inherited'] = True
            item['message'] = item.get('message', '') + '（母本自身就有这条缺口，变体只是原样继承，不拦单。）'
        out.append(item)
    return out


def diagnose_object_ledger(beats: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """软信号 —— 记诊断不拦单。

    典型是「建完就再也没被提过」：合法（被覆盖了），但也可能是这一拍白建了
    （母本那条 12mm 桦木胶合板被下一拍的板条 100% 盖死，成片里零可见度）。
    """
    beats = [b for b in (beats or []) if isinstance(b, dict)]
    ledger = build_object_ledger(beats)
    notes: List[Dict[str, Any]] = []
    total = len(beats)
    for oid, entry in sorted(ledger.items()):
        intro = entry.get('introduced_at')
        if intro is None:
            continue
        refs = [r for r in entry.get('referenced_at') or [] if r > intro]
        if not refs and intro < total:
            notes.append({
                'rule': 'orphan', 'severity': 'warning', 'beat': intro, 'object': oid,
                'message': (f'「{oid}」在第 {intro} 拍建成后再没被任何一拍提及，'
                            f'确认它是被后续工序覆盖了，而不是这一拍白建。'),
            })
    return notes


def annotate_beats(beats: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """给每一拍补上 `role` / `produced_objects` / `inherited_objects`，返回诊断。

    在 Pass B 落盘前跑（`reverse.normalize_beat_keys`），母本与变体一视同仁。补的是
    **缺失**的字段，已经有值的一律不动——模型填的、人在节拍阶梯上手改的，都比这里
    推出来的可信。

    为什么要写进文档而不是每次现推：`role` 是母本与变体之间唯一被继承的东西，靠正则
    现推意味着同一拍在不同调用点可能推出不同的角色，而它决定了变体这一拍要建什么。
    写下来之后它只推一次，人能在阶梯上看见它、也能直接改它，变体那侧读的是显式值。

    补出来的物件申报会标 ``objects_source='inferred'``：它是正则从散文里捞的，不是
    作者填的。`beats_declare_objects` 认这个标记——推断出来的账不能拿去拦交付。
    """
    notes: List[Dict[str, Any]] = []
    for position, beat in enumerate(beats or [], start=1):
        if not isinstance(beat, dict):
            continue
        idx = _beat_index(beat, position)

        role = str(beat.get('role') or '').strip().lower()
        if role not in CONSTRUCTION_ROLES:
            role = infer_role(beat)
            beat['role'] = role
        if role == UNKNOWN_ROLE:
            notes.append({
                'rule': 'unknown_role', 'severity': 'warning', 'beat': idx, 'object': '',
                'message': (f'第 {idx} 拍读不出施工角色（operation / stage / 散文里都没有'
                            f'可辨认的工序词）。变体会照着角色从零写这一拍，角色空着就只能'
                            f'写成一段通用施工——在节拍阶梯里给它补一个 role 更省事。'),
            })

        stamped = False
        if 'produced_objects' not in beat:
            beat['produced_objects'] = sorted(extract_objects(_produced_text(beat)))
            stamped = True
        if 'inherited_objects' not in beat:
            beat['inherited_objects'] = sorted(extract_objects(_inherited_text(beat)))
            stamped = True
        if stamped and not beat.get('objects_source'):
            beat['objects_source'] = 'inferred'
    return notes


def _dedupe(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen: Set[Tuple] = set()
    out: List[Dict[str, Any]] = []
    for item in items:
        key = (item.get('rule'), item.get('beat'), item.get('object'))
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return sorted(out, key=lambda x: (x.get('beat') or 0, x.get('rule') or ''))


# ── 视频差量约束 ─────────────────────────────────────────────────────────────
#
# 规范 2.3 的 Zero Phantom Changes 铁律此前只是**写给模型看的一句自然语言**
# （composers/base.py 的 FULL-FIELD DELTA CONSERVATION）。VIDEO 1 因此能凭空铺一层
# 木地板：它的末帧 IMAGE 2 明写着「完全排空、刮擦干净」，视频却把工人写成在铺
# 「dark, weathered timber planks」，还宣称那就是 IMAGE 2。
#
# 把它变成集合运算：视频里能出现的建造产物 ⊆ 相邻两帧的差量 ∪ 已建成的东西。
def _declared_objects(beat: Dict[str, Any], *keys: str) -> Set[str]:
    """一拍**显式申报**的物件集合。

    变体线的每一拍现在带 `produced_objects` / `inherited_objects`（由 mutate 的输出
    契约要求模型填写）。申报值可能是 canonical id、role 名，也可能是一句构件名 ——
    三种都收，能对上词表的归一化，对不上的按原样留着当自定义 id。
    """
    out: Set[str] = set()
    for key in keys:
        for item in (beat.get(key) or []):
            token = str(item or '').strip().lower()
            if not token:
                continue
            out.add(token)
            out |= extract_objects(token)
            # role 名要展开成它名下的全部 canonical id：申报「structure」说的是这一拍
            # 交付了骨架类构件，而账本比对用的是 'structural_frame' / 'ceiling_rib'。
            # 不展开的话两个命名空间永不相交，申报等于白填。
            out |= set(ROLE_OBJECTS.get(token) or ())
    return out


def beats_declare_objects(beats: List[Dict[str, Any]]) -> bool:
    """这条阶梯的物件账是**声明式**的，还是账本自己从散文里推出来的。

    这个区别决定了视频差量检查能不能当硬闸：作者（模型或人）填的申报是精确的，
    正则从散文里捞出来的不是。实测（2026-09-03，仓库里 6 份已交付提示词包共 86 段
    视频）推断模式的误报率约两成，全部来自同一个构件在图与视频里叫法不同（视频说
    "subfloor joist grid"，图说 "engineered floor deck"）。拿那样的判据去拦交付，
    拦掉的多半是好片子。

    判据是**申报的来源**，不是字段在不在。改动前这里只数键在不在，而 `annotate_beats`
    如今给每一份阶梯都补了这两个字段——只数键的话，母本线会因为被补过字段就"升级"成
    申报式，用推断出来的东西去拦单，等于把那两成误报直接变成拦单理由。
    `objects_source == 'inferred'` 的那些拍不算申报。

    `produced_objects: []` 是一次合法申报（这一拍没有新建成任何东西，比如过门拍），
    和字段整个缺席是两回事，所以判键在不在、不判值真不真。
    """
    beats = [b for b in (beats or []) if isinstance(b, dict)]
    if not beats:
        return False
    declared = sum(1 for b in beats
                   if ('produced_objects' in b or 'inherited_objects' in b)
                   and str(b.get('objects_source') or '') != 'inferred')
    return declared * 2 >= len(beats)


def allowed_video_objects(beats: List[Dict[str, Any]], video_index: int) -> Set[str]:
    """第 ``video_index`` 段视频（IMAGE i → IMAGE i+1）里允许出现的建造产物。

    = 目标帧新增的（差量） ∪ 此刻**已经建成**的（工人可以站在上面、靠着它干活）。
    不在这个集合里的建造产物 = 幽灵。
    """
    beats = [b for b in (beats or []) if isinstance(b, dict)]
    if not beats:
        return set()
    i = max(1, int(video_index))
    target = beats[i] if i < len(beats) else beats[-1]
    delta = extract_objects(_produced_text(target), _action_text(target))
    delta |= _declared_objects(target, 'produced_objects', 'inherited_objects')
    built: Set[str] = set()
    for beat in beats[:i]:
        built |= extract_objects(_produced_text(beat))
        built |= _declared_objects(beat, 'produced_objects')
    return delta | built


def _target_roles(beats: List[Dict[str, Any]], video_index: int) -> Set[str]:
    """这一段视频的目标拍所属的角色（含它之前所有拍的角色）。

    推断模式下的放宽判据：同一个构件在图与视频里叫法不同是常态，但**角色**是稳定的。
    角色对得上就放行，对不上才报——VIDEO 1 在一条 role=demolition 的拍上铺
    role=flooring 的地板，照样抓得住。
    """
    beats = [b for b in (beats or []) if isinstance(b, dict)]
    i = max(1, int(video_index))
    return {infer_role(b) for b in beats[:i + 1]}


def validate_video_objects(beats: List[Dict[str, Any]],
                           video_prompts: Dict[int, str],
                           *,
                           strict: Optional[bool] = None) -> List[Dict[str, Any]]:
    """逐段视频做差量集合检查。

    ``video_prompts``: {视频序号(1 起) → 提示词正文}。
    ``strict``: ``None``（默认）按 :func:`beats_declare_objects` 自动判定 ——
    申报式账本走精确比对并判 ``blocking``，推断式账本放宽到角色层并只判 ``warning``。
    显式传 True/False 可以覆盖。
    """
    beats = [b for b in (beats or []) if isinstance(b, dict)]
    exact = beats_declare_objects(beats) if strict is None else bool(strict)
    violations: List[Dict[str, Any]] = []
    for index in sorted(video_prompts or {}):
        text = str(video_prompts.get(index) or '')
        if not text.strip():
            continue
        allowed = allowed_video_objects(beats, index)
        # 角色层兜底在**两种模式下都算**。改动前它只在推断模式下生效，于是申报模式
        # 一遇到叫法不同（视频写 "clear coat"，这一拍的申报里是 protective_coating
        # 之外的写法）就直接判死——那正是推断模式测出两成误报的同一件事，只是换了个
        # 地方发生。现在它降一档而不是放行：角色对得上仍然记 warning，对不上才拦单。
        roles = _target_roles(beats, index)
        for oid in sorted(extract_objects(text)):
            if oid in allowed:
                continue
            role_ok = OBJECT_ROLE.get(oid) in roles
            if role_ok and not exact:
                continue
            severity = 'blocking' if (exact and not role_ok) else 'warning'
            note = ('，不过它的角色与这一段已经进行到的工序对得上，可能只是叫法不同。'
                    if role_ok else '。')
            violations.append({
                'rule': 'phantom_motion', 'severity': severity, 'beat': index, 'object': oid,
                'message': (f'第 {index} 段视频里出现了「{oid}」，它既不在这一段的首尾帧'
                            f'差量里，也不是此前已经建成的东西{note}'),
            })
    return _dedupe(violations)


def format_violations(violations: List[Dict[str, Any]], limit: int = 12) -> str:
    """给用户看的中文清单。"""
    if not violations:
        return ''
    label = {'phantom': '凭空出现', 'regression': '已完成回归',
             'inversion': '依赖倒置', 'phantom_motion': '视频幽灵变化',
             'orphan': '建成后失联'}
    lines = []
    for item in violations[:limit]:
        tag = label.get(item.get('rule') or '', item.get('rule') or '')
        lines.append(f'· [{tag}] {item.get("message")}')
    if len(violations) > limit:
        lines.append(f'· …另有 {len(violations) - limit} 条同类问题')
    return '\n'.join(lines)
