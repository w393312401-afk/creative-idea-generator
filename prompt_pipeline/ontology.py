"""施工角色本体与材质包 —— 把变异从「换词」升级为「换本体」的那一层。

## 为什么需要这一层

`mutate.apply_slot_replacement` 的做法是**在已渲染好的散文上跑正则**，词典还是母本
专用硬编码（集装箱 / 河岸 / 鲟鱼 / 木棚）。它有三个必然后果，2026-09-03 那批海蚀洞
变体把三个都撞齐了：

  1. 字典没覆盖的母本词原样留下 —— railcar / carriage / scrapyard / blast doors /
     camper / mountain skyline 全是这么漏出来的；
  2. 不在 key 里的字段（beat 名）停在母本材质，body 却被 LLM 换成了新材质 ——
     `erect timber portal` 底下站着三道 Corten 钢拱；
  3. 同一个构件在不同拍解析出不同材料 —— 同一句里既 slate 又 basalt。

根因是**替换发生在散文层，而散文层没有「这是同一个构件」这个概念**。

## 这一层怎么解

构件先归到**角色**（role）——它在建造逻辑里承担的职能，与具体材料无关：垫层就是垫层，
不管它是碎石、夯土还是泡沫玻璃。角色是母本和变体之间**唯一被继承的东西**；具体材料
在渲染时才由材质包（MaterialPack）解析出来。

于是：
  - 同一 role 在整条链上只会解析出**同一个**材料（矛盾 3 消失）；
  - beat 名和 body 从同一次解析渲染（矛盾 2 消失）；
  - 母本的材料名词根本不参与变体渲染，没有可漏的东西（矛盾 1 消失）。

材质包按**类目**组织而不是按预置场景，所以换一个母本不需要改任何词典——这正是
`apply_slot_replacement` 做不到、必须整表重写的那件事。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

# ── 施工角色词表 ─────────────────────────────────────────────────────────────
#
# 顺序即工序拓扑的默认秩（topology.py 的 ROLE_RANK 直接读它），所以**新增角色必须
# 插到正确位置**，不能追加到末尾：秩错了，依赖倒置检查会把正常梯子判死。
CONSTRUCTION_ROLES: Dict[str, Dict[str, str]] = {
    'site':       {'zh': '场地与地表', 'en': 'site ground'},
    'demolition': {'zh': '破拆清场',   'en': 'strip-out'},
    'subfloor':   {'zh': '垫层找平',   'en': 'sub-base'},
    'structure':  {'zh': '主体骨架',   'en': 'structural frame'},
    'enclosure':  {'zh': '围护外板',   'en': 'exterior envelope'},
    'opening':    {'zh': '门窗洞口',   'en': 'opening'},
    'window':     {'zh': '窗',         'en': 'window'},
    'door':       {'zh': '门扇',       'en': 'door leaf'},
    'membrane':   {'zh': '防潮防水层', 'en': 'vapour barrier'},
    'service':    {'zh': '水电管线',   'en': 'services rough-in'},
    'batten':     {'zh': '龙骨',       'en': 'batten grid'},
    'insulation': {'zh': '保温层',     'en': 'insulation'},
    'sheathing':  {'zh': '基层板',     'en': 'sheathing board'},
    'flooring':   {'zh': '成品地面',   'en': 'finish floor'},
    'cladding':   {'zh': '室内饰面',   'en': 'interior lining'},
    'coating':    {'zh': '涂层保护',   'en': 'protective coating'},
    'paving':     {'zh': '室外铺装',   'en': 'exterior paving'},
    'heating':    {'zh': '取暖炉具',   'en': 'stove and flue'},
    'cabinetry':  {'zh': '固定家具',   'en': 'fitted joinery'},
    'furnishing': {'zh': '软装陈设',   'en': 'furnishing'},
    'hero':       {'zh': '终极揭示',   'en': 'hero reveal'},
}

ROLE_ORDER: List[str] = list(CONSTRUCTION_ROLES.keys())
ROLE_RANK: Dict[str, int] = {r: i for i, r in enumerate(ROLE_ORDER)}


# ── 角色识别 ─────────────────────────────────────────────────────────────────
#
# 从母本一拍的 operation / stage / 散文里读出它的角色。先匹配到的先赢，所以**具体的
# 排在笼统的前面**：`floor batten` 必须在 `floor` 之前，否则龙骨会被读成成品地面，
# 依赖检查会认为地面比保温层还早，整条梯子判死。
_ROLE_CUES: List[tuple] = [
    ('demolition', r'demolit|strip[- ]out|tear[- ]out|clear(?:ing)?\s+debris|dewater|de-water|rubble|dismantl|remov(?:e|es|ed|al)|lifts?\s+away|clears?\s+away|破拆|清场|拆除|排水'),
    ('batten',     r'\b(?:floor\s+)?(?:batten|sleeper|noggin|furring|joist\s+grid)s?\b|龙骨|木楞|搁栅'),
    ('subfloor',   r'sub-?floor|sub-?base|aggregate|hardcore|screed|levelling\s+layer|leveling\s+layer|ballast|垫层|找平|碎石|级配'),
    ('membrane',   r'vapou?r\s+barrier|damp[- ]proof|waterproof|membrane|防潮|防水卷材|隔汽'),
    ('insulation', r'insulat|rockwool|mineral\s+wool|batt\b|aerogel|保温|岩棉'),
    ('sheathing',  r'sheathing|plywood|osb|backing\s+(?:board|panel)|基层板|衬板'),
    ('flooring',   r'floorboard|tongue[- ]and[- ]groove\s+floor|finish\s+floor|hardwood\s+floor|地板铺装|成品地面'),
    ('cladding',   r'interior\s+lining|slat\s+(?:wall|clad)|wainscot|panel(?:ling|ing)|饰面|内衬|板条'),
    ('enclosure',  r'exterior\s+(?:clad|panel|skin|envelope)|siding|shell\s+panel|外墙|围护|外挂板'),
    ('window',     r'\bwindows?\b|glazing|glazed|窗'),
    ('door',       r'\bdoors?\b|door\s+lea(?:f|ves)|门扇|挂门'),
    ('opening',    r'\bopenings?\b|apertures?|cut-?outs?|洞口|开洞'),
    ('structure',  r'\bframe|framing|portal|arch|stud|rafter|truss|beam|column|upright|骨架|框架|立柱|梁|拱'),
    ('service',    r'conduit|wiring|electric|plumb|duct|rough-?in|junction\s+box|管线|布线|水电'),
    ('coating',    r'passivat|sealer|clear\s+coat|varnish|paint|stain|oil\s+finish|涂层|上漆|封闭剂'),
    ('paving',     r'flagstone|paver|patio|landing|walkway|apron|铺装|石板|露台'),
    ('heating',    r'stove|hearth|flue|chimney|fireplace|炉|烟囱|壁炉'),
    ('cabinetry',  r'cabinet|joinery|worktop|countertop|kitchenette|shelving|bench|橱柜|台面|操作台'),
    ('furnishing', r'furnish|mattress|bed\s+platform|textile|styling|soft\s+goods|软装|床|陈设'),
    ('hero',       r'\breveal\b|hero|final\s+shot|终极|揭示'),
    ('site',       r'\bsite\b|ground|terrain|excavat|场地|地表|开挖'),
]

_ROLE_PATTERNS = [(role, re.compile(pat, re.I)) for role, pat in _ROLE_CUES]

# stage/operation_type 是结构化字段，比散文可靠，先用它兜一层。
_STAGE_TO_ROLE = {
    'demolition': 'demolition', 'clearing': 'demolition', 'excavation': 'site',
    'structural': 'structure', 'framing': 'structure',
    'rough_in': 'service', 'rough-in': 'service', 'wiring': 'service', 'plumbing': 'service',
    'enclosure': 'enclosure', 'insulation': 'insulation',
    'surface': 'cladding', 'paneling': 'sheathing', 'drywall': 'sheathing',
    'floor': 'flooring', 'flooring': 'flooring',
    'fixtures': 'cabinetry', 'furnishing': 'furnishing',
    'reveal': 'hero', 'lighting': 'service',
}


# 「这一拍是干什么的」字段，优先级高于「这一拍长什么样」的铺陈散文。
_ROLE_HEADLINE_FIELDS = ('operation', 'milestone_name', 'visual_subject', 'visible_subject')
_ROLE_BODY_FIELDS = ('visible_action', 'visible_result', 'state_after', 'description')


def infer_role(beat: Dict[str, Any]) -> str:
    """读出一拍的施工角色。

    分两趟读，是因为 `re.search` 认的是**哪条模式先匹配**，不是哪个字段先出现：
    一条保温拍的铺陈里只要提一句「window assemblies remain undisturbed」，window
    那条模式就会先命中，整拍被判成装窗。所以先只拿「这一拍是干什么的」那几个字段
    过一遍，命中了就结束；命中不了才退到全文。

    stage 排最后 —— 它的粒度太粗（`surface` 同时盖着涂层、饰面和铺装三种角色）。
    """
    if not isinstance(beat, dict):
        return 'structure'
    explicit = str(beat.get('role') or '').strip().lower()
    if explicit in CONSTRUCTION_ROLES:
        return explicit

    headline = ' '.join(str(beat.get(k) or '') for k in _ROLE_HEADLINE_FIELDS)
    if headline.strip():
        for role, pattern in _ROLE_PATTERNS:
            if pattern.search(headline):
                return role

    # stage 排在铺陈散文**之前**：它是 Pass A 落下来的结构化字段，比在整段描写里
    # 捞关键词可靠得多。反过来的代价很具体 —— 一条 stage='demolition' 的拆除拍，
    # 描写里一句 "lifts away the shack from soil ground" 里的 ground 会让它被判成
    # 场地平整，兜底渲染于是写出一段和拆除毫无关系的话。
    stage = str(beat.get('stage') or beat.get('operation_type') or '').strip().lower()
    if stage in _STAGE_TO_ROLE:
        return _STAGE_TO_ROLE[stage]

    body = ' '.join(str(beat.get(k) or '') for k in _ROLE_HEADLINE_FIELDS + _ROLE_BODY_FIELDS)
    if body.strip():
        for role, pattern in _ROLE_PATTERNS:
            if pattern.search(body):
                return role

    return 'structure'


# ── 材质包 ───────────────────────────────────────────────────────────────────
#
# 按**类目**组织，不按预置场景。类目从轴 2 的自由文本里判，判不出走 composite。
# 每个包是 role → 具体材料名词短语；渲染时按 role 取，全链同一个 role 只会取到
# 同一个值。
_MATERIAL_CATEGORY_CUES = [
    ('metal',    r'钢|铁|铝|铜|金属|steel|iron|alumin|brass|copper|corten|metal|zinc'),
    ('stone',    r'石|岩|玄武|花岗|大理|混凝土|水泥|砖|stone|basalt|granite|marble|concrete|masonry|brick|slate'),
    ('timber',   r'木|竹|原木|松|柚|橡|落叶松|timber|wood|log|pine|oak|larch|teak|bamboo|plywood'),
    ('earth',    r'夯土|生土|土坯|草泥|earth|rammed|adobe|cob|clay|turf|sod'),
    ('textile',  r'帆布|织物|毛毡|皮革|canvas|felt|textile|leather|hide|tarp'),
]
_MATERIAL_CATEGORY_PATTERNS = [(k, re.compile(p, re.I)) for k, p in _MATERIAL_CATEGORY_CUES]

# 每个类目一张 role → 材料模板表。`{m}` 是轴 2 原文里择出的主材短语。
_PACK_TEMPLATES: Dict[str, Dict[str, str]] = {
    'metal': {
        'subfloor': 'compacted crushed aggregate sub-base', 'structure': '{m} portal frame',
        'enclosure': 'riveted {m} panel', 'door': 'insulated {m} door leaf',
        'window': 'double-glazed steel-framed window', 'membrane': 'taped polymer vapour barrier',
        'batten': 'galvanised top-hat batten', 'insulation': 'mineral wool batt',
        'sheathing': 'birch plywood backing panel', 'flooring': 'engineered timber floorboard',
        'cladding': 'powder-coated {m} lining panel', 'coating': 'matte passivating sealer',
        'paving': 'cut stone flagstone', 'heating': 'cast-iron stove with twin-wall flue',
        'cabinetry': 'welded {m} carcass with timber worktop', 'furnishing': 'wool and leather soft goods',
        'service': 'surface-run steel conduit', 'opening': 'framed steel aperture',
        'site': 'graded working platform', 'demolition': 'stripped steel shell',
        'hero': 'final revealed interior',
    },
    'stone': {
        'subfloor': 'compacted graded hardcore', 'structure': 'dressed {m} pier and lintel',
        'enclosure': 'coursed {m} wall', 'door': 'braced hardwood door leaf',
        'window': 'deep-reveal timber-framed window', 'membrane': 'bituminous damp-proof membrane',
        'batten': 'treated softwood batten', 'insulation': 'wood-fibre insulation board',
        'sheathing': 'lime-plaster backing coat', 'flooring': 'honed {m} floor slab',
        'cladding': 'lime-washed plaster lining', 'coating': 'breathable mineral silicate finish',
        'paving': 'cleft {m} paving slab', 'heating': 'masonry heater with clay flue',
        'cabinetry': 'stone-topped timber joinery', 'furnishing': 'linen and sheepskin soft goods',
        'service': 'chased copper and conduit run', 'opening': 'dressed stone reveal',
        'site': 'levelled stone terrace', 'demolition': 'cleared stone shell',
        'hero': 'final revealed interior',
    },
    'timber': {
        'subfloor': 'compacted gravel bed on ground beams', 'structure': '{m} post-and-beam frame',
        'enclosure': 'shiplap {m} board cladding', 'door': 'ledged and braced {m} door',
        'window': 'timber casement with double glazing', 'membrane': 'breather membrane with taped laps',
        'batten': 'sawn {m} counter-batten', 'insulation': 'sheep wool insulation batt',
        'sheathing': 'tongue-and-groove board sheathing', 'flooring': 'wide-plank {m} floorboard',
        'cladding': 'planed {m} slat lining', 'coating': 'penetrating hardwax oil',
        'paving': 'timber sleeper and gravel apron', 'heating': 'steel stove on stone hearth',
        'cabinetry': 'solid {m} carcass joinery', 'furnishing': 'wool and canvas soft goods',
        'service': 'notched conduit run in studwork', 'opening': 'trimmed timber opening',
        'site': 'levelled bearer platform', 'demolition': 'stripped timber carcass',
        'hero': 'final revealed interior',
    },
    'earth': {
        'subfloor': 'rammed hardcore and lime screed', 'structure': '{m} monolithic wall',
        'enclosure': 'lime-rendered {m} skin', 'door': 'braced plank door',
        'window': 'splayed reveal timber window', 'membrane': 'clay slip and reed damp layer',
        'batten': 'roundwood batten', 'insulation': 'straw-light clay infill',
        'sheathing': 'reed mat backing', 'flooring': 'sealed earthen floor',
        'cladding': 'burnished clay plaster', 'coating': 'limewash finish',
        'paving': 'rammed earth and stone apron', 'heating': 'clay rocket mass heater',
        'cabinetry': 'built-in earthen niche and timber shelf', 'furnishing': 'handwoven textile soft goods',
        'service': 'chased conduit set in clay plaster', 'opening': 'splayed earthen reveal',
        'site': 'levelled earth pad', 'demolition': 'cleared earthen shell',
        'hero': 'final revealed interior',
    },
    'textile': {
        'subfloor': 'timber deck platform', 'structure': 'tensioned {m} frame',
        'enclosure': 'stretched {m} panel', 'door': 'laced {m} entry flap',
        'window': 'clear vinyl glazing panel', 'membrane': 'waterproof groundsheet',
        'batten': 'lashed pole batten', 'insulation': 'quilted felt liner',
        'sheathing': 'inner {m} liner', 'flooring': 'layered rug over deck',
        'cladding': 'padded {m} lining', 'coating': 'proofing wax treatment',
        'paving': 'gravel and board walkway', 'heating': 'portable stove with flashing kit',
        'cabinetry': 'folding timber campaign furniture', 'furnishing': 'wool blanket and cushion soft goods',
        'service': 'surface-clipped low-voltage run', 'opening': 'reinforced fabric aperture',
        'site': 'cleared pitching ground', 'demolition': 'struck original covering',
        'hero': 'final revealed interior',
    },
    'composite': {
        'subfloor': 'compacted aggregate sub-base', 'structure': '{m} structural frame',
        'enclosure': '{m} exterior panel', 'door': 'insulated {m} door leaf',
        'window': 'double-glazed framed window', 'membrane': 'taped vapour barrier',
        'batten': 'treated timber batten', 'insulation': 'mineral wool batt',
        'sheathing': 'plywood backing panel', 'flooring': 'engineered floorboard',
        'cladding': '{m} interior lining', 'coating': 'matte protective sealer',
        'paving': 'stone paving slab', 'heating': 'cast-iron stove with insulated flue',
        'cabinetry': 'fitted {m} joinery', 'furnishing': 'natural-fibre soft goods',
        'service': 'clipped conduit run', 'opening': 'framed aperture',
        'site': 'levelled working platform', 'demolition': 'stripped original shell',
        'hero': 'final revealed interior',
    },
}

# 类目 → 工具 / 紧固 / ASMR。视频差量约束（object_ledger.allowed_video_entities）
# 拿它做工具白名单：工具不是建造产物，不该被判成「凭空出现的构件」。
_PACK_TOOLING: Dict[str, Dict[str, List[str]]] = {
    'metal':   {'tools': ['angle grinder', 'impact wrench', 'pneumatic rivet gun', 'welding set', 'magnetic level'],
                'fastening': ['high-tensile bolt', 'structural rivet'],
                'asmr': ['metallic clang', 'rivet snap', 'grinder whine', 'ratchet click']},
    'stone':   {'tools': ['stone chisel', 'club hammer', 'rubber mallet', 'stone saw', 'pointing trowel'],
                'fastening': ['resin anchor', 'lime mortar bed'],
                'asmr': ['stone chink', 'mallet thud', 'trowel scrape', 'grit sweep']},
    'timber':  {'tools': ['cordless impact driver', 'circular saw', 'block plane', 'chisel', 'framing square'],
                'fastening': ['countersunk wood screw', 'concealed brad'],
                'asmr': ['saw rasp', 'driver chatter', 'timber thud', 'plane shaving hiss']},
    'earth':   {'tools': ['tamping rammer', 'wooden float', 'shuttering board', 'spray bottle'],
                'fastening': ['timber dowel pin', 'reed tie'],
                'asmr': ['rammer thump', 'wet clay slap', 'float sweep']},
    'textile': {'tools': ['sailmaker needle', 'tensioning strap', 'grommet punch', 'mallet'],
                'fastening': ['brass grommet', 'lashing cord'],
                'asmr': ['fabric rustle', 'strap ratchet', 'grommet punch snap']},
    'composite': {'tools': ['cordless drill', 'spirit level', 'utility knife', 'sealant gun'],
                  'fastening': ['stainless screw', 'structural adhesive'],
                  'asmr': ['drill whir', 'panel knock', 'sealant squeeze']},
}


def detect_material_category(material_text: str) -> str:
    """轴 2 自由文本 → 材质类目。判不出走 composite（而不是抛错）：轴文本是用户
    自由填的，一个判不出的词不该让整条变体挂掉。"""
    text = str(material_text or '')
    for key, pattern in _MATERIAL_CATEGORY_PATTERNS:
        if pattern.search(text):
            return key
    return 'composite'


def _primary_material_phrase(material_text: str) -> str:
    """从轴 2 原文里择出一个能当定语用的主材短语。

    轴文本常常是「粗犷芬兰松木原木 + 气凝胶保温层 + 侘寂微水泥 + 黑色碳化防腐木」
    这种加号串；直接整串塞进 `{m}` 会得到一个 40 字的定语。取第一段，并砍掉明显的
    工序后缀。"""
    text = str(material_text or '').strip()
    if not text:
        return 'composite'
    head = re.split(r'[+＋/、,，;；]', text)[0].strip()
    head = re.sub(r'(结构|材料|体系|工艺|层|包|系统)$', '', head).strip()
    return head or text[:24]


class MaterialPack:
    """一次变体里，role → 具体材料的**唯一**权威解析。

    构造一次、全链复用。`resolve` 是纯函数式查表，没有随机——同一个 role 问一百次
    得到同一个答案，这正是「同句既 slate 又 basalt」不可能再发生的原因。
    """

    def __init__(self, category: str, material_text: str = '',
                 overrides: Optional[Dict[str, str]] = None):
        self.category = category if category in _PACK_TEMPLATES else 'composite'
        self.material_text = str(material_text or '')
        self.primary = _primary_material_phrase(self.material_text)
        self._templates = dict(_PACK_TEMPLATES[self.category])
        self._overrides = {k: str(v) for k, v in (overrides or {}).items() if v}
        tooling = _PACK_TOOLING.get(self.category) or _PACK_TOOLING['composite']
        self.tools = list(tooling['tools'])
        self.fastening = list(tooling['fastening'])
        self.asmr = list(tooling['asmr'])

    def resolve(self, role: str) -> str:
        """role → 材料名词短语。未知 role 回落到主材本身，不抛错。"""
        role = str(role or '').strip().lower()
        if role in self._overrides:
            return self._overrides[role]
        template = self._templates.get(role)
        if not template:
            return self.primary
        return template.replace('{m}', self.primary)

    def as_dict(self) -> Dict[str, Any]:
        return {
            'category': self.category,
            'primary': self.primary,
            'roles': {r: self.resolve(r) for r in ROLE_ORDER if r in self._templates},
            'tools': list(self.tools),
            'fastening': list(self.fastening),
            'asmr': list(self.asmr),
        }


def build_pack(mutation_axes: Dict[str, Any],
               overrides: Optional[Dict[str, str]] = None) -> MaterialPack:
    """从四轴里造出这次变体的材质包。只读轴 2。"""
    material_text = ''
    if isinstance(mutation_axes, dict):
        material_text = str(mutation_axes.get('material') or '')
    return MaterialPack(detect_material_category(material_text), material_text, overrides)


# ── 标题渲染 ─────────────────────────────────────────────────────────────────
#
# beat 名过去是**从母本原样继承**的字符串，body 却被 LLM 换成了新材质，于是
# `erect timber portal` 底下站着三道 Corten 钢拱。名字必须和 body 从同一次解析出来。
_ROLE_VERB = {
    'site': 'prepare', 'demolition': 'strip out', 'subfloor': 'lay', 'structure': 'erect',
    'enclosure': 'sheathe', 'opening': 'cut', 'window': 'glaze', 'door': 'hang',
    'membrane': 'seal', 'service': 'rough in', 'batten': 'fix', 'insulation': 'pack',
    'sheathing': 'board', 'flooring': 'lay', 'cladding': 'line', 'coating': 'coat',
    'paving': 'pave', 'heating': 'install', 'cabinetry': 'fit', 'furnishing': 'dress',
    'hero': 'reveal',
}


def render_beat_title(role: str, pack: MaterialPack) -> str:
    """`erect Corten steel portal frame` —— 动词来自 role，名词来自材质包。"""
    role = str(role or 'structure').strip().lower()
    verb = _ROLE_VERB.get(role, 'install')
    noun = pack.resolve(role)
    return f'{verb} {noun}'.strip()
