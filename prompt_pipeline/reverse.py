"""反推模式（SKILL.md Tier 4）的可编程实现。

对话里的 Claude 手工跑 Tier 4 时，Stage 1「只看像素」靠的是纪律；产品化之后没有人
在回路里，纪律必须变成结构。这个模块承担三件事：

  Pass A  extract_frame_facts   逐帧客观事实提取（多模态）
  Pass B  cluster_beats         事实 + change_events → timelapse_beats.json
  校验    validate_beats        事件覆盖、证据帧、施工依赖硬否决
  二创    mutate_beats          沿受控变异轴做同构映射，骨架不动

反注入（anti-priming）是本模块最重要的不变量：Pass A 的调用链上**不允许**出现任何
主题、简报、项目标题。这不是靠注释约定的——`extract_frame_facts` 的签名里没有可以
装下它们的形参，且 config 在进多模态调用之前会被 `_scrub_config_for_pass_a` 剥成只剩
网关字段。破坏这一点的代价很具体：模型会开始「补全」它认为该有的施工步骤，于是
banned_elements 里本该出现的东西反而被写进了 beats。

模块级函数一律通过 `pp.xxx` 访问 prompt_pipeline（与 composers/base.py 同一约定）——
全套测试都用 `patch.object(pp, ...)` 打桩，import 绑定会把桩打空。
"""

import json
import math
import os
import re
import sys
import tempfile
import time

import prompt_pipeline as pp


# Pass A 的提示词版本号。改了 _PASS_A_SYSTEM 就必须改它，否则旧缓存会被当成新结果复用。
PASS_A_PROMPT_VERSION = 'v3'

# ── 实体规范化与消歧词典 ───────────────────────────────────────────────────
#
# 解决多模态模型在不同帧中对同一物理材质/器具用词漂移的问题（如上一帧叫 poly sheeting，
# 下一帧叫 black vapor barrier），确保定长窗与阶梯统计不会产生虚假的出现与消失。
_CANONICAL_ENTITY_PATTERNS = [
    # 动力工具与气动设备（优先于紧固件和板材）
    #
    # 钻这一条必须排在冲击起子之前，且不许再被删掉：v3 重写时整条 drill 规则丢了一次，
    # 于是 drill / power drill / hammer drill 在相邻帧里成了三个不同实体，逐窗统计
    # （见 _WINDOW_FACT_FIELDS）把它读成"一个消失、一个出现"，节拍边界跟着错位。
    # 排在前面是因为 "cordless drill driver" 这类合写必须归钻，不能被下面的 driver 吃掉。
    (re.compile(r'\b(?:cordless|corded|power|electric|hammer|rotary)?\s*drill(?:\s*driver)?\b', re.I), 'cordless drill'),
    # `screw gun` / `screwdriver` 归起子，不归射钉枪——所以射钉枪那条的前缀里不许有
    # `screw`。它们同为 first-match-wins，射钉枪排在后面只是双保险。
    (re.compile(r'\b(?:cordless|brushless|impact)?\s*(?:impact\s*driver|driver)\b|\b(?:screw\s*gun|screwdriver)\b', re.I), 'cordless impact driver'),
    (re.compile(r'\b(?:pneumatic|framing|brad|finish|nail)\s*(?:gun|nailer)\b', re.I), 'framing nailer'),
    (re.compile(r'\b(?:circular|skil)\s*saw\b', re.I), 'circular saw'),
    (re.compile(r'\b(?:reciprocating\s*saw|sawzall|sabre\s*saw)\b', re.I), 'reciprocating saw'),
    (re.compile(r'\b(?:orbital|sheet|belt|disc)\s*sander\b', re.I), 'orbital sander'),
    (re.compile(r'\b(?:airless|paint|high-?pressure)\s*sprayer\b', re.I), 'paint sprayer'),
    (re.compile(r'\b(?:caulk(?:ing)?\s*gun|sealant\s*gun)\b', re.I), 'caulking gun'),

    # 紧固件与辅料（优先于板材与基础材料）
    (re.compile(r'\b(?:drywall|phosphate|coarse-?thread|fine-?thread)\s+screws?\b|\bblack\s+screws?\b', re.I), 'drywall screws'),
    (re.compile(r'\b(?:wood|timber|decking|countersunk)\s+screws?\b', re.I), 'countersunk wood screws'),
    (re.compile(r'\b(?:framing|collated|strip|pneumatic|galvanized)\s+nails?\b', re.I), 'framing nails'),
    (re.compile(r'\b(?:construction\s+adhesive|heavy-?duty\s+adhesive|liquid\s+nails?)\b', re.I), 'construction adhesive'),

    # 防潮与防水卷材与胶带
    (re.compile(r'\b(?:vapor|vapour)\s+barrier\s*(?:membrane|sheeting|sheet|film)?\b|\bpoly(?:ethylene)?\s+(?:sheeting|sheet|film)\b|\bblack\s+(?:poly|plastic\s+sheeting|tarp|membrane)\b', re.I), 'vapor barrier membrane'),
    (re.compile(r'\bwaterproof(?:ing)?\s+(?:membrane|sheet|sheeting|film|layer)\b', re.I), 'waterproof membrane'),
    (re.compile(r'\b(?:seam|tuck|sheathing|flashing|acrylic|vapor|foil)\s+tape\b', re.I), 'seam sealing tape'),

    # 保温材料
    (re.compile(r'\b(?:fiberglass|fibreglass|glass\s*wool|mineral\s*wool|rockwool)\s*(?:insulation|batts?|rolls?)?\b|\byellow\s+(?:insulation|fibreglass\s+batts?)\b', re.I), 'insulation batts'),
    (re.compile(r'\b(?:rigid\s+foam|xps|eps|foam\s*board|polyiso(?:cyanurate)?)\s*(?:insulation|panel|sheet)?\b', re.I), 'rigid foam board'),
    (re.compile(r'\b(?:spray\s+foam|polyurethane\s+foam|expanding\s+foam|pu\s+foam)\b', re.I), 'expanding PU foam sealant'),

    # 龙骨与结构木料
    (re.compile(r'\b(?:timber|wood|wooden)\s*(?:studs?|framing|rafters?|joists?)\b|\b2x4\s*(?:studs?|framing)?\b', re.I), 'timber framing studs'),

    # 板材
    (re.compile(r'\b(?:gypsum\s*board|sheetrock|plasterboard|drywall\s*(?:sheet|panel|board)?)\b', re.I), 'drywall panels'),
    (re.compile(r'\b(?:osb|oriented\s*strand\s*board)\s*(?:sheet|sheeting|panel|board)?s?\b', re.I), 'OSB sheathing'),
    (re.compile(r'\b(?:plywood)\s*(?:sheet|sheeting|panel|board|subfloor)?s?\b', re.I), 'plywood sheathing'),

    # 地基碎石、水泥与自流平
    (re.compile(r'\b(?:crushed\s*(?:gravel|stone|rock)|aggregate|gravel\s*sub-?base)\b', re.I), 'crushed gravel'),
    (re.compile(r'\b(?:concrete|cement)\s*(?:slab|screed|mortar|patch)\b', re.I), 'concrete surface'),
    (re.compile(r'\b(?:self-?level(?:ing)?\s*(?:compound|underlayment|cement|screed))\b', re.I), 'self-leveling underlayment'),

    # 常见手工具
    # 前缀不能全可选：裸 `level` 一并吃掉了 "floor level" / "eye level" 这类根本不是
    # 器具的短语。留一条"整条短语就是 level"的分支来接住 tools 里单写 level 的情况。
    (re.compile(r'\b(?:spirit|bubble|laser|torpedo)\s*levels?\b|^\s*levels?\s*$', re.I), 'spirit level'),
    (re.compile(r'\b(?:plastering|hand|masonry|smoothing|notched|taping)?\s*trowel\b|\bputty\s*knife\b', re.I), 'trowel'),
    (re.compile(r'\b(?:tape\s*measure|measuring\s*tape)\b', re.I), 'measuring tape'),
    (re.compile(r'\b(?:utility|craft|stanley|box|snap-off)\s*knife\b', re.I), 'utility knife'),
    (re.compile(r'\b(?:chalk\s*line|snap\s*line)\b', re.I), 'chalk snap line'),
    (re.compile(r'\b(?:spiked\s*roller)\b', re.I), 'spiked roller'),
]


def canonicalize_entity_phrase(phrase):
    """把短语中的近义词归一到标准术语。"""
    text = str(phrase or '').strip()
    if not text:
        return ''
    for pattern, canonical in _CANONICAL_ENTITY_PATTERNS:
        if pattern.search(text):
            return canonical
    return text


def extract_roi_patches(frame_path, patch_size=512, max_patches=2, output_dir=None):
    """从原生高分辨率帧中提取关键作业工区/接触点的 100% 原生分辨率局部特写切片。

    解决整图缩小后微小工具刀头、紧固件、板材接缝像素被池化抹平的问题。
    返回局部切片的图片路径列表；若图片过小或裁剪失败则返回原图路径列表。
    """
    try:
        from PIL import Image
    except ImportError:
        return [frame_path]

    if not frame_path or not os.path.exists(frame_path):
        return []

    try:
        with Image.open(frame_path) as im:
            w, h = im.size
            if w <= patch_size or h <= patch_size:
                return [frame_path]

            target_dir = output_dir or tempfile.mkdtemp(prefix='roi_patch_')
            os.makedirs(target_dir, exist_ok=True)
            base_name = os.path.splitext(os.path.basename(frame_path))[0]
            patches = []

            left = max(0, min(w - patch_size, int(w * 0.5) - patch_size // 2))

            def _save_patch(top, suffix):
                box = (left, top, left + patch_size, top + patch_size)
                path = os.path.join(target_dir, f'{base_name}_roi_{suffix}.jpg')
                im.crop(box).convert('RGB').save(path, 'JPEG', quality=85)
                return path

            # 两块 ROI 的纵向位置必须一起算，不能各自独立贴边内推。
            #
            # 名义中心是 0.55H（作业接触区）与 0.78H（材料交接/底面）。各自独立 clamp 的话，
            #16:9 上两块会重叠 55%：第一块占到 338–850，底下只剩 230px，第二块被贴底推回
            # 来。多花的载荷买回两张近似重复的图，还让"哪张图属于哪一帧"更难对位。
            #
            # 所以这里先把第一块上限压到"给第二块留出 min_sep 的位置"，再让第二块尽量待在
            # 它的名义位置上。留不出 min_sep（画幅太矮）就只出一块——第二块此时几乎是第一
            # 块的副本，白搭载荷。
            min_sep = int(patch_size * 0.75)          # 容许最多 25% 重叠
            room = h - patch_size                     # top 的合法上界
            two_fit = room >= min_sep

            top1 = int(h * 0.55) - patch_size // 2
            top1 = max(0, min(room - min_sep if two_fit else room, top1))
            patches.append(_save_patch(top1, 'action'))

            if max_patches >= 2 and two_fit:
                top2 = max(int(h * 0.78) - patch_size // 2, top1 + min_sep)
                top2 = max(0, min(room, top2))
                patches.append(_save_patch(top2, 'seam'))

            return patches if patches else [frame_path]
    except Exception as e:
        if sys.stdout:
            print(f"[REVERSE] ROI 特写裁剪异常，回退使用原帧: {e}")
        return [frame_path]


# 单次多模态调用塞多少帧。压到 768px JPEG 之后单帧约 100–200KB，10 帧一批的 base64
# 载荷在 2MB 量级——再大就开始撞网关的请求体上限和超时。
_PASS_A_BATCH_SIZE = 10

# Pass B 的视觉输入：送审帧按 digest 顺序拼成的分页拼图。
#
# 为什么必须有：Pass B 原本只拿到 `_facts_digest` 那几十行文本去聚类节拍。跨帧的东西
# 恰恰只活在像素里——同一面墙完成度的推进、工具箱有没有挪、上一拍的痕迹是不是还在，
# 在 `extent=左三分之二已涂` 这种一行摘要里全丢了，模型只能靠文本相邻性猜哪几帧属于
# 同一个里程碑。给它整页拼图，节拍边界就是一次感知，不必从文字里重建。
#
# 5 列 × 4 行 = 20 帧一页：再密单帧就小到读不出完成范围，再稀页数上去、载荷和钱一起涨。
_PASS_B_SHEET_COLUMNS = 5
_PASS_B_SHEET_PAGE_SIZE = 20
_PASS_B_SHEET_DIRNAME = '.pass_b_sheets'

_FRAME_FACTS_FILENAME = 'frame_facts.json'
_BEATS_FILENAME = 'timelapse_beats.json'
_SCHEMA_FILENAME = 'timelapse-beats.schema.json'

# 模型回复解析失败时的重试次数。与「校验未过的定向回炉」是两笔独立预算，理由见
# cluster_beats 里的注释。
_PARSE_RETRY_BUDGET = 2


# ── 施工阶段与硬否决 ─────────────────────────────────────────────────────────
#
# 取自 references/omni-restoration-continuity.md 的「Construction Sequence
# Dependencies」。这里只编码**机械可判定**的那部分：阶段序号的单调性，以及三条
# 能用阶段号表达的硬否决。体积守恒、封闭空间成因这类需要看画面才能判的，留给
# 人工卡点（review_beats），不在这里假装能自动判。

_STAGE_RANK = {
    'demolition': 1,    # 拆除清运
    'structural': 2,    # 结构修复：地基、框架、承重墙、屋面结构
    'rough_in': 3,      # 隐蔽工程：电线、水管、风管
    'enclosure': 4,     # 封闭：吊顶板、墙板
    'surface': 5,       # 面层：底漆、面漆、湿作业
    'floor': 6,         # 地面收尾
    'fixtures': 7,      # 灯具设备
    'furnishing': 8,    # 家具软装
    'reveal': 9,        # 最终奖励揭示
}

# 会把隐蔽工程盖住的阶段。做完其中任何一样再去走线，都意味着要拆开刚做完的东西。
_COVERING_STAGES = ('enclosure', 'surface', 'floor')

_STAGE_LABELS_ZH = {
    'transition': '空间过门/镜头穿越',
    'demolition': '拆除清运',
    'structural': '结构修复',
    'rough_in': '隐蔽工程',
    'enclosure': '封板封顶',
    'surface': '面层处理',
    'floor': '地面收尾',
    'fixtures': '灯具设备',
    'furnishing': '家具软装',
    'reveal': '成果揭示',
}


def stage_label(stage):
    return _STAGE_LABELS_ZH.get(stage, stage or '未分类')


# ── Pass A：逐帧客观事实提取 ─────────────────────────────────────────────────

_PASS_A_SYSTEM = """You are a forensic engineering frame reader. You are given still frames extracted from a video.

Your ONLY job is to record exact, physically visible facts in each frame at high forensic fidelity. You have no knowledge of what this video is about, what it is for, or what usually happens in videos like this.

HARD RULES
- Record ONLY what the pixels show. If you cannot see it, it does not exist.
- Never infer a tool, material, worker, or operation from context, common sense, or industry habit. "There is fresh paint, so there must be a brush" is a forbidden inference.
- Never describe intent, purpose, story, or what will happen next.
- FORBIDDEN VAGUE WORDS: In 'tool_specifics', 'material_specs', 'fastening_and_bonding', and 'micro_traces', NEVER write single generic words like "tool", "drill", "machine", "wood", "board", "paint", "metal", "plastic", "renovation work", "partially done". ALWAYS specify the concrete physical type, driving mechanism, finish, or cross-section.
- Four-Zone Spatial Fact Decomposition: Break down physical state across 4 spatial domains:
  1. overhead: roof, rafters, ceiling, overhead lights/ducts, upper structure.
  2. facade_and_walls: wall framing, studs, sheathing, panels, doors/windows, wiring.
  3. floor: ground, dirt, gravel, insulation, subfloor, joists, flooring finish.
  4. peripherals_and_spoil: debris piles, waste bags, toolboxes, raw materials.
- Micro-Engineering Forensic Extraction:
  - material_specs: Visible material layers, nominal thickness/grade, surface texture, sheen/reflectivity (e.g. "9mm OSB sheathing with raw matte texture", "2x4 SPF timber studs (38x89mm)", "black polyethylene vapor barrier membrane with red taped seams").
  - tool_specifics: Specific equipment/tool model, power source, and active bit/blade (e.g. "18V cordless brushless impact driver with magnetic bit", "pneumatic framing nailer", "stainless steel notched trowel", "airless paint spray gun").
  - fastening_and_bonding: Observable mechanical fasteners or chemical bonds (e.g. "countersunk black drywall screws", "expanding PU foam sealant along gap", "heavy-duty construction adhesive bead", "staples").
  - micro_traces: Visible microscopic physical marks left by the work (e.g. "fine wood sawdust along pencil cut-lines", "fresh drywall dust on floor edges", "chalk snap line", "paint overspray splatter").
- Describe completion extent spatially and concretely: "left two-thirds of the wall is coated, right third is bare" — never "partially done".
- If a frame is too blurry, dark, or occluded to read, say so and give it a low confidence.

OUTPUT
Return a JSON array, one object per frame given, in the same order:
[{
  "frame": "<the exact filename you were given>",
  "subject": "<what occupies the frame, one sentence>",
  "spatial_zones": {
    "overhead": "<concrete physical state or 'none'>",
    "facade_and_walls": "<concrete physical state or 'none'>",
    "floor": "<concrete physical state or 'none'>",
    "peripherals_and_spoil": "<concrete physical state or 'none'>"
  },
  "materials": ["<visible material surfaces>"],
  "material_specs": ["<specific material thickness, texture, grade>"],
  "tools": ["<visible tools, machines, equipment>"],
  "tool_specifics": ["<exact tool model/type, drive mechanism, bit/blade>"],
  "fastening_and_bonding": ["<countersunk screws, expanding foam, staples, adhesive>"],
  "workers_present": true|false,
  "completion_extent": "<concrete spatial state of the work visible in this frame>",
  "traces": ["<visible marks left by work: dust, drips, cut lines, offcuts, stains>"],
  "micro_traces": ["<fine dust, pencil layout marks, offcuts, splatter>"],
  "confidence": 0.0-1.0
}]

Return the JSON array and nothing else. No prose, no code fences."""


_PASS_A_CONFIG_KEYS = (
    # 只放网关与调用参数。任何可能携带主题的键（theme / _project_key / dimensions /
    # title / outline…）都不在这里，这就是反注入的结构性保证。
    'baseUrl', 'apiKey', 'codexBaseUrl', 'codexApiKey',
    'model', 'reviewModel', 'reviewConcurrency',
    'frameFactsModel', 'peakVerifyModel',
)


def _scrub_config_for_pass_a(config):
    """把 config 剥成只剩网关字段。见模块 docstring 的反注入说明。"""
    src = config or {}
    return {k: src[k] for k in _PASS_A_CONFIG_KEYS if k in src}


def _pass_a_model(config):
    """Pass A 默认走审查用的便宜模型；`framFactsModel` 可覆盖。

    逐帧读「材料标签 / 工具类型 / 完成范围」这类细节，flash 会糊。方案里的建议是
    flash 打底 + peak 帧强模型复核（见 verify_peak_frames）。"""
    cfg = config or {}
    return (cfg.get('frameFactsModel') or cfg.get('reviewModel')
            or cfg.get('model') or 'gemini-3.7-flash-high')


def _facts_cache_path(job_dir):
    return os.path.join(job_dir, '.frame_facts_cache.json')


def _read_facts_cache_file(job_dir):
    path = _facts_cache_path(job_dir)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if data.get('prompt_version') != PASS_A_PROMPT_VERSION:
        # 提示词换了版，旧结果不能复用——这正是版本号存在的理由。
        return {}
    return data


def _migrated_legacy_cache(job_dir, data):
    """老格式（一个扁平的 frames 表，没记模型）→ 按模型分桶的新格式。

    老格式没有记是哪个模型读的。硬扔掉会让升级当天正在跑的任务白付一次 Pass A，
    所以从磁盘上的 frame_facts.json 认领那个模型名——那份产物就是这批缓存生出来的。
    认不出来才丢。
    """
    frames = data.get('frames') or {}
    if not frames:
        return {}
    try:
        with open(os.path.join(job_dir, _FRAME_FACTS_FILENAME), 'r', encoding='utf-8') as f:
            model = (json.load(f) or {}).get('model')
    except (OSError, ValueError):
        return {}
    return {model: frames} if model else {}


def _load_facts_cache(job_dir, model):
    """这一模型读过的帧事实。

    缓存必须按模型分桶：键只有帧名的话，把「逐帧识别」换成强模型之后，Pass A 会
    原样命中弱模型留下的读数——用户付了强模型的价，拿到的还是上一轮的结论，而且
    页面上没有任何迹象。分桶之后换模型是真的重读，换回来则仍然免费。
    """
    data = _read_facts_cache_file(job_dir)
    if not data:
        return {}
    by_model = data.get('by_model')
    if by_model is None:
        by_model = _migrated_legacy_cache(job_dir, data)
    return dict((by_model or {}).get(model) or {})


def _save_facts_cache(job_dir, model, frames):
    data = _read_facts_cache_file(job_dir)
    by_model = data.get('by_model')
    if by_model is None:
        by_model = _migrated_legacy_cache(job_dir, data)
    by_model = dict(by_model or {})
    by_model[model] = frames
    path = _facts_cache_path(job_dir)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump({'prompt_version': PASS_A_PROMPT_VERSION, 'by_model': by_model},
                  f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _planned_frames(overview):
    """analysis_plan 指定的帧（文件名 → 完整条目）。

    SKILL.md 把 analysis_plan 定成硬下界：送审少于它列出的帧，节拍阶梯即失效。所以
    这里不做任何「聪明的裁剪」，plan 说哪些就是哪些。降级模式的收窄在 plan 之外单独
    表达（见 degraded_plan_frames），并且会被明确标注到产物里。"""
    entries = ((overview.get('review_sampling') or {}).get('frames') or [])
    by_name = {os.path.basename(e['frame_path']): e for e in entries if e.get('frame_path')}
    plan = overview.get('analysis_plan') or {}
    names = plan.get('required_frames') or [os.path.basename(e['frame_path']) for e in entries]
    return [by_name[n] for n in names if n in by_name]


def degraded_plan_frames(overview):
    """降级模式：只取 change_events 的 start/peak/end 证据帧 + 首尾帧。

    产出的 beats 精度更低，调用方必须把这一点标注到 UI 上——省下的是视觉调用钱，
    代价是「事件之间发生了什么」全部靠推断，而推断正是本模块要禁的东西。"""
    entries = ((overview.get('review_sampling') or {}).get('frames') or [])
    by_name = {os.path.basename(e['frame_path']): e for e in entries if e.get('frame_path')}
    keep = set()
    for event in (overview.get('change_events') or []):
        keep.update(event.get('evidence_frames') or [])
    if entries:
        keep.add(os.path.basename(entries[0]['frame_path']))
        keep.add(os.path.basename(entries[-1]['frame_path']))
    ordered = [e for e in entries if os.path.basename(e['frame_path']) in keep]
    return ordered


def all_review_frames(overview):
    """抽帧抽出来的**全部**送审帧，不做任何裁剪。

    analysis_plan 是硬下界，不是上界：长片走 adaptive 时它只挑「四成 + 每秒一张」，
    于是一条三分钟的视频抽出四百多帧、真正送进模型的只有一百来张，事件之间的推进
    全靠 Pass B 在文本里补——用户反馈的「图片识别数量太少」就是这一档。要更密，
    就在这里把上界打开，代价是视觉调用次数按比例上涨（预估里照实报）。
    """
    return [e for e in ((overview.get('review_sampling') or {}).get('frames') or [])
            if e.get('frame_path')]


# 送审档位。三档都只决定「送多少帧给多模态模型」，与抽帧密度（--base-fps，见
# replica_pipeline.run_extract）是两个独立的旋钮：抽帧决定总共有多少帧可选，
# 这里决定选多少张去花钱。
REVIEW_SCOPES = ('degraded', 'plan', 'all')
DEFAULT_REVIEW_SCOPE = 'plan'


def normalize_review_scope(scope=None, degraded=False):
    """把「档位字符串 / 老的 degraded 布尔」统一成一个档位名。

    `degraded` 是这三档存在之前的唯一开关，磁盘上的老 job 状态和老前端都还在传它，
    所以它继续作为 scope 缺省时的回退，而不是被删掉。
    """
    text = str(scope or '').strip().lower()
    if text in REVIEW_SCOPES:
        return text
    return 'degraded' if degraded else DEFAULT_REVIEW_SCOPE


def scope_frames(overview, scope):
    """档位 → 送审帧列表。"""
    scope = normalize_review_scope(scope)
    if scope == 'degraded':
        return degraded_plan_frames(overview)
    if scope == 'all':
        return all_review_frames(overview)
    return _planned_frames(overview)


def _frames_by_name(overview):
    entries = ((overview.get('review_sampling') or {}).get('frames') or [])
    return {os.path.basename(e['frame_path']): e for e in entries if e.get('frame_path')}


def peak_frame_names(overview):
    """每个 change_event 的峰值帧（evidence_frames 的 start/peak/end 里中间那张）。

    `verify_peak_frames` 与 `estimate_pass_a_cost` 共用它——复核要花的钱必须和预估里
    报出来的是同一笔，两处各算一遍迟早对不上。
    """
    by_name = _frames_by_name(overview)
    names = []
    for event in (overview.get('change_events') or []):
        for name in (event.get('evidence_frames') or [])[1:2]:
            if name in by_name and name not in names:
                names.append(name)
    return names


def estimate_pass_a_cost(overview, degraded=False, batch_size=_PASS_A_BATCH_SIZE,
                         scope=None):
    """给 UI 的开跑前预估。extract 完成后必须先把它摆给用户确认再烧钱。

    峰值帧复核现在默认开（见 `_peak_verify_model`），所以它那几次调用也必须出现在这里。
    默认加钱却不进预估，等于绕开了「先确认再烧钱」这道卡点本身。
    """
    scope = normalize_review_scope(scope, degraded)
    frames = scope_frames(overview, scope)
    n = len(frames)
    batches = (n + batch_size - 1) // batch_size if n else 0
    peaks = peak_frame_names(overview)
    peak_batches = (len(peaks) + _PEAK_BATCH_SIZE - 1) // _PEAK_BATCH_SIZE if peaks else 0
    return {
        'frame_count': n,
        'total_extracted': len(((overview.get('review_sampling') or {}).get('frames') or [])),
        'batch_count': batches,
        'batch_size': batch_size,
        'scope': scope,
        'degraded': scope == 'degraded',
        'plan_mode': (overview.get('analysis_plan') or {}).get('mode'),
        'peak_frame_count': len(peaks),
        'peak_batch_count': peak_batches,
    }


# ── 模型回复的 JSON 解析 ────────────────────────────────────────────────────
#
# 这一层不是洁癖，是本模块踩过的实际故障（2026-08-09，两次连跑都死在 Pass B）：
# 让模型写 "the left two-thirds of the wall" 这类描述性字段，它迟早会在字符串里
# 写进一个没转义的引号或裸换行，于是整批回复报
# `Expecting ',' delimiter`，几十次视觉调用的成果一起作废。
#
# 修复只做三件保守的事，都不改变合法 JSON 的语义：去掉尾逗号、把字符串里的裸控制字符
# 转义、把字符串里的内层引号转义。真正结构性坏掉的回复修不回来——那种情况交给调用方
# 重试，见 _chat_json。

def _repair_json_text(text):
    """扫一遍文本，转义字符串内的裸控制字符与内层引号，并去掉尾逗号。

    判定引号是"闭合"还是"内层"的依据：闭合引号后面（跳过空白）只可能是 , : } ] 或结尾。
    其余位置的引号必然是字符串内容的一部分，模型忘了转义。
    """
    out = []
    in_string = False
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if not in_string:
            if ch == '"':
                in_string = True
                out.append(ch)
            elif ch == ',':
                # 尾逗号：下一个非空白字符是 } 或 ] 就丢掉这个逗号。
                j = i + 1
                while j < n and text[j] in ' \t\r\n':
                    j += 1
                if j < n and text[j] in '}]':
                    i += 1
                    continue
                out.append(ch)
            else:
                out.append(ch)
            i += 1
            continue

        # 字符串内部
        if ch == '\\':
            out.append(text[i:i + 2])
            i += 2
            continue
        if ch == '"':
            j = i + 1
            while j < n and text[j] in ' \t\r\n':
                j += 1
            if j >= n or text[j] in ',:}]':
                in_string = False
                out.append(ch)
            else:
                out.append('\\"')
            i += 1
            continue
        if ch in '\n\r\t':
            out.append({'\n': '\\n', '\r': '\\r', '\t': '\\t'}[ch])
            i += 1
            continue
        out.append(ch)
        i += 1
    return ''.join(out)


class TruncatedReply(ValueError):
    """回复在写完之前就被 max_tokens 截断了。

    与「写错了一个引号」是两种病，药也不同：截断要让模型写短一点，修复器再聪明也补不出
    没写出来的内容。分开是因为第一版把两者混在一起，日志里只看得到一句
    `Expecting ',' delimiter`，看上去像格式问题，实际是输出太长。
    """


def _looks_truncated(text):
    """括号/引号没配平 = 在半路断掉。字符串内部的括号不计数。"""
    depth = 0
    in_string = False
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if in_string:
            if ch == '\\':
                i += 2
                continue
            if ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch in '{[':
            depth += 1
        elif ch in '}]':
            depth -= 1
        i += 1
    return depth > 0 or in_string


def parse_json_reply(raw):
    """模型回复 → Python 对象。先原样解析，失败再走一次保守修复。

    两次都失败就把原始回复带进异常消息（截断到 600 字）——上一次线上故障里，日志只有
    一句 `Expecting ',' delimiter: line 156 column 6`，没人能知道模型到底回了什么。
    """
    cleaned = pp._strip_code_fences(raw)
    try:
        return json.loads(cleaned)
    except ValueError as first:
        try:
            return json.loads(_repair_json_text(cleaned))
        except ValueError:
            snippet = (cleaned or '')[:600]
            if _looks_truncated(cleaned):
                # 截断看尾部比看开头有用：断在哪里一眼就知道。
                raise TruncatedReply(
                    f'模型回复在写完之前被截断（{len(cleaned)} 字，{first}）。'
                    f'这不是格式错误，是输出太长——拍数太多或每拍写得太啰嗦。'
                    f'\n断点前 200 字：\n{(cleaned or "")[-200:]}')
            raise ValueError(f'模型回复不是合法 JSON（{first}）。原始回复前 600 字：\n{snippet}')


def _dump_bad_reply(job_dir, stage, raw, error):
    """把解析不了的原始回复落到 job 目录，供事后排查。

    上一次线上故障（2026-08-09）里日志只有一句 `Expecting ',' delimiter: line 156
    column 6`，回复本身没留下来——连"模型到底写错了什么"都无从判断。落盘失败一律吞掉：
    这是排查辅助，不能反过来盖住真正的错误。
    """
    try:
        path = os.path.join(job_dir, f'.bad_reply_{stage}_{int(time.time())}.txt')
        with open(path, 'w', encoding='utf-8') as f:
            f.write(f'# {error}\n\n{raw}')
        if sys.stdout:
            print(f'[REVERSE] {stage} 回复解析失败，原始回复已存到 {path}')
    except Exception:
        pass


def _parse_facts_array(raw, expected_names, strict=False):
    """把模型回复解析成 {frame_name: fact}。名字对不上的按顺序兜底。

    `strict=True` 关掉按位兜底，只认名字对得上的对象。峰值复核必须用它：那一路给模型
    的图片数量多于帧数量（整帧 + 原生特写切片），一旦模型按图片数而不是帧数逐个作答，
    按位兜底就会把第 1 帧的特写读数挂到第 2、3 帧上——正好挂在节拍边界帧上，还会覆盖
    Pass A 已经读对的结论。宁可这一帧不复核。
    """
    data = parse_json_reply(raw)
    if isinstance(data, dict):
        data = data.get('frames') or data.get('results') or []
    if not isinstance(data, list):
        raise ValueError('Pass A 回复不是数组')

    out = {}
    for idx, item in enumerate(data):
        if not isinstance(item, dict):
            continue
        name = str(item.get('frame') or '').strip()
        if name not in expected_names:
            if strict:
                continue
            # 模型偶尔会回 "frame 3" 或省略字段。按位置兜底，位置也超界就丢弃——
            # 丢一帧比把事实挂到错误的帧上安全得多。
            if idx < len(expected_names):
                name = expected_names[idx]
            else:
                continue

        zones_raw = item.get('spatial_zones')
        zones = {}
        if isinstance(zones_raw, dict):
            for k in ('overhead', 'facade_and_walls', 'floor', 'peripherals_and_spoil'):
                val = str(zones_raw.get(k) or '').strip()
                if val:
                    zones[k] = val

        materials = [canonicalize_entity_phrase(x) for x in (item.get('materials') or []) if str(x).strip()]
        tools = [canonicalize_entity_phrase(x) for x in (item.get('tools') or []) if str(x).strip()]
        material_specs = [str(x).strip() for x in (item.get('material_specs') or []) if str(x).strip()]
        tool_specifics = [str(x).strip() for x in (item.get('tool_specifics') or []) if str(x).strip()]
        fastening = [canonicalize_entity_phrase(x) for x in (item.get('fastening_and_bonding') or []) if str(x).strip()]
        traces = [str(x).strip() for x in (item.get('traces') or []) if str(x).strip()]
        micro_traces = [str(x).strip() for x in (item.get('micro_traces') or []) if str(x).strip()]

        out[name] = {
            'frame': name,
            'subject': str(item.get('subject') or '').strip(),
            'spatial_zones': zones,
            'materials': materials,
            'material_specs': material_specs,
            'tools': tools,
            'tool_specifics': tool_specifics,
            'fastening_and_bonding': fastening,
            'workers_present': bool(item.get('workers_present')),
            'completion_extent': str(item.get('completion_extent') or '').strip(),
            'traces': traces,
            'micro_traces': micro_traces,
            'confidence': _clamp01(item.get('confidence'), default=0.5),
        }
    return out


def _clamp01(value, default=0.5):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, v))


def extract_frame_facts(config, job_dir, on_progress=None, degraded=False,
                        batch_size=_PASS_A_BATCH_SIZE, scope=None):
    """Pass A。读 job_dir/video_overview.json，产出 job_dir/frame_facts.json。

    签名里没有 dimensions / brief / title / theme，且不接受 **kwargs——反注入靠这个
    形状来保证，别为了「顺手传个主题过去让它理解得更好」而破坏它。`scope` 是档位
    枚举（见 REVIEW_SCOPES），装不下主题，加它不破坏这条不变量。
    """
    overview_path = os.path.join(job_dir, 'video_overview.json')
    with open(overview_path, 'r', encoding='utf-8') as f:
        overview = json.load(f)

    scope = normalize_review_scope(scope, degraded)
    entries = scope_frames(overview, scope)
    if not entries:
        raise ValueError('analysis_plan 为空：抽帧阶段没有产出任何可送审的帧')

    clean_config = _scrub_config_for_pass_a(config)
    model = _pass_a_model(config)
    cache = _load_facts_cache(job_dir, model)

    pending = [e for e in entries if os.path.basename(e['frame_path']) not in cache]
    if on_progress:
        on_progress('replica_stage', {
            'stage': 'review_frames',
            'message': f'逐帧事实提取：共 {len(entries)} 帧，命中缓存 {len(entries) - len(pending)} 帧，'
                       f'待提取 {len(pending)} 帧',
            'total': len(entries),
            'cached': len(entries) - len(pending),
        })

    batches = [pending[i:i + batch_size] for i in range(0, len(pending), batch_size)]
    done_counter = {'n': 0}

    def _ask(batch, max_tokens, timeout, max_side=768):
        """一次视觉调用：喂这批帧，返回 {frame_name: fact}。解析失败抛 ValueError。"""
        paths = [e['frame_path'] for e in batch]
        names = [os.path.basename(p) for p in paths]
        # 先压再送：全尺寸 PNG 的 base64 会把请求体撑到十几 MB，这是超时的主要来源。
        small = pp._compress_frames_for_review(paths, max_side=max_side, quality=78 if max_side > 768 else 72)
        listing = '\n'.join(
            f'{i + 1}. {n}  (t={e["timestamp"]}s)'
            for i, (n, e) in enumerate(zip(names, batch))
        )
        user_text = (
            f'Read these {len(batch)} frames. They are given in this order:\n{listing}\n\n'
            'Return one JSON object per frame, in the same order, using the exact filenames above.'
        )
        raw = pp._multimodal_chat(
            clean_config, _PASS_A_SYSTEM, user_text, small,
            model=model, max_tokens=max_tokens, timeout=timeout,
        )
        try:
            return _parse_facts_array(raw, names)
        except ValueError as e:
            _dump_bad_reply(job_dir, 'frame_facts', raw, e)
            raise

    def _run_batch(batch):
        pp._raise_if_cancelled(on_progress)
        names = [os.path.basename(e['frame_path']) for e in batch]
        # 一批是十帧的视觉调用。解析炸了就整批作废重来，比丢掉十帧的观察便宜得多。
        # 截断（ResponseTruncated）也走这条路：十帧的观察写不进 4096 tokens 是常事，
        # 而它的解药和坏 JSON 一样是把这批拆小，不是原样再要一次。
        for remaining in range(_PARSE_RETRY_BUDGET, -1, -1):
            pp._raise_if_cancelled(on_progress)
            try:
                return _ask(batch, max_tokens=4096, timeout=180)
            except (ValueError, pp.ResponseTruncated):
                if remaining == 0:
                    break

        if len(batch) == 1:
            if sys.stdout:
                print(f'[REVERSE] 单帧连续解析失败，放弃: {names[0]}')
            return {}

        # 二分梯队重试：若批次大于 2 帧，先尝试二分为两个半批（例如 10 -> 5 + 5），
        # 并提升压缩分辨率至 1024px 提高微小工具与工序接缝的识别精度。
        if len(batch) > 2:
            mid = len(batch) // 2
            sub_batches = [batch[:mid], batch[mid:]]
            if sys.stdout:
                print(f'[REVERSE] 这批 {len(batch)} 帧解析失败，二分拆为 {len(sub_batches[0])} + {len(sub_batches[1])} 帧（升至 1024px）重试…')
            if on_progress:
                on_progress('replica_stage', {
                    'stage': 'review_frames',
                    'message': f'一批 {len(batch)} 帧解析失败，正在二分拆为 {len(sub_batches[0])} + {len(sub_batches[1])} 帧高清重试…',
                })
            bisection_salvaged = {}
            for sub_batch in sub_batches:
                pp._raise_if_cancelled(on_progress)
                sub_names = [os.path.basename(e['frame_path']) for e in sub_batch]
                sub_ok = False
                for sub_rem in range(_PARSE_RETRY_BUDGET, -1, -1):
                    pp._raise_if_cancelled(on_progress)
                    try:
                        sub_res = _ask(sub_batch, max_tokens=2048, timeout=120, max_side=1024)
                        bisection_salvaged.update(sub_res)
                        sub_ok = True
                        break
                    except (ValueError, pp.ResponseTruncated):
                        if sub_rem == 0:
                            break
                if not sub_ok:
                    # 半批仍失败，降级逐帧重试（同样使用 1024px）
                    for entry in sub_batch:
                        pp._raise_if_cancelled(on_progress)
                        try:
                            bisection_salvaged.update(_ask([entry], max_tokens=1024, timeout=90, max_side=1024))
                        except (ValueError, pp.ResponseTruncated):
                            if sys.stdout:
                                print(f'[REVERSE] 逐帧重试仍失败，丢弃这一帧: '
                                      f'{os.path.basename(entry["frame_path"])}')
            return bisection_salvaged

        if sys.stdout:
            print(f'[REVERSE] 这批帧连续 {_PARSE_RETRY_BUDGET + 1} 次解析失败，'
                  f'改为逐帧重试: {names}')
        if on_progress:
            on_progress('replica_stage', {
                'stage': 'review_frames',
                'message': f'一批 {len(batch)} 帧解析失败，正在逐帧重试以免整批丢失…',
            })
        salvaged = {}
        for entry in batch:
            pp._raise_if_cancelled(on_progress)
            try:
                salvaged.update(_ask([entry], max_tokens=1024, timeout=90, max_side=1024))
            except (ValueError, pp.ResponseTruncated):
                if sys.stdout:
                    print(f'[REVERSE] 逐帧重试仍失败，丢弃这一帧: '
                          f'{os.path.basename(entry["frame_path"])}')
        if sys.stdout:
            print(f'[REVERSE] 逐帧重试救回 {len(salvaged)}/{len(batch)} 帧')
        return salvaged

    def _on_done(_key, result):
        if result:
            cache.update(result)
            _save_facts_cache(job_dir, model, cache)
        done_counter['n'] += len(result or {})
        if on_progress:
            on_progress('replica_stage', {
                'stage': 'review_frames',
                'message': f'逐帧事实提取 {done_counter["n"]}/{len(pending)}',
                'done': done_counter['n'],
                'total': len(pending),
            })

    if batches:
        try:
            results = pp._map_parallel(
                _run_batch,
                [(i, b) for i, b in enumerate(batches)],
                pp.review_concurrency(config),
                on_done=_on_done,
            )
            for chunk in results.values():
                cache.update(chunk or {})
        finally:
            _save_facts_cache(job_dir, model, cache)

    facts = []
    for entry in entries:
        name = os.path.basename(entry['frame_path'])
        fact = dict(cache.get(name) or {'frame': name, 'confidence': 0.0})
        fact['timestamp'] = entry['timestamp']
        facts.append(fact)

    payload = {
        'prompt_version': PASS_A_PROMPT_VERSION,
        'model': model,
        'scope': scope,
        'degraded': scope == 'degraded',
        'frame_count': len(facts),
        'generated_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'facts': facts,
    }
    out_path = os.path.join(job_dir, _FRAME_FACTS_FILENAME)
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return payload


# 峰值帧复核一批塞几张。比 Pass A 的十帧小：这几张是要「放大了看」的，一次问太多张
# 就退回成粗读，复核也就白做了。
_PEAK_BATCH_SIZE = 6

# `peakVerifyModel` 的显式关闭值。默认开之后必须留一个关得掉的开关。
_PEAK_VERIFY_OFF = ('off', 'none', 'no', 'false', '0', 'skip')


def _peak_verify_model(config):
    """峰值帧复核用哪个模型；返回 None 表示不复核。

    默认开（2026-08-10 改）。原先默认关，省下的是几次调用的钱，代价是节拍边界——
    边界恰好落在 peak 帧上，那几帧被 flash 读糊，整条阶梯就整体错位，后面所有合成
    调用都建在错的骨架上。这笔账不对等，所以默认改成开。

    选型顺序：`peakVerifyModel` 显式指定 → 主模型 `model`（通常比 Pass A 的
    `reviewModel` 强一档）。即便解析下来和 Pass A 同一个模型也照跑：复核这一遍换的
    不只是模型，还有 1024px 而非 768px 的输入、更小的批次和「放大了看」的指令。
    要关就把 `peakVerifyModel` 设成 off/none/false。
    """
    cfg = config or {}
    raw = cfg.get('peakVerifyModel')
    if raw is False:
        return None
    if isinstance(raw, str):
        text = raw.strip()
        if text.lower() in _PEAK_VERIFY_OFF:
            return None
        if text:
            return text
    return cfg.get('model') or 'gemini-3.7-flash-high'


# 复核结果里"这一项模型没答"和"这一项模型看清了是空的"必须分开。空串/空表/空 dict 一律
# 算没答，落回 Pass A 的读数；bool 与数值不算——`workers_present: false` 正是复核要给的
# 结论，把它当"没答"就等于永远推翻不了 Pass A 说有人。
def _is_blank_fact_value(value):
    if value is None:
        return True
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return False
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, dict, set)):
        return len(value) == 0
    return False


def _merge_verified_fact(base, refined, model):
    """把峰值复核的读数按字段合进 Pass A 的事实，而不是整条替换。

    整条替换埋过一个很贵的坑：复核提示词的重心在 material_specs / tool_specifics 这些
    新字段上，模型漏答 `materials`/`tools` 是常事，而这两个字段正是 `_WINDOW_FACT_FIELDS`
    的成员——一旦被清成空表，逐窗统计就在 peak 帧上看到实体集体消失，而 peak 帧恰恰是
    节拍边界。「复核是增强，不是门禁」这条既有约定得落到字段一级才算数。

    timestamp 一律以 Pass A 为准（复核提示词里没有时间轴，模型无从知道）。
    """
    merged = dict(base or {})
    for key, value in (refined or {}).items():
        if key in ('frame', 'timestamp'):
            continue
        if _is_blank_fact_value(value) and not _is_blank_fact_value(merged.get(key)):
            continue
        merged[key] = value
    merged['timestamp'] = (base or {}).get('timestamp')
    merged['verified_by'] = model
    return merged


def verify_peak_frames(config, job_dir, facts_payload, on_progress=None):
    """用强模型复核 change_events 的 peak 帧。

    Pass A 用 flash 打底是为了成本，但节拍边界恰恰落在 peak 上——那几帧读错，整条
    阶梯就错位。这里只对 peak 帧重跑一次，命中的改写会覆盖 flash 的结论并把
    confidence 抬到复核值。开关与选型见 `_peak_verify_model`。
    """
    model = _peak_verify_model(config)
    if not model:
        return facts_payload

    overview_path = os.path.join(job_dir, 'video_overview.json')
    with open(overview_path, 'r', encoding='utf-8') as f:
        overview = json.load(f)

    by_name = _frames_by_name(overview)
    peak_names = peak_frame_names(overview)
    if not peak_names:
        return facts_payload

    if on_progress:
        on_progress('replica_stage', {
            'stage': 'review_frames',
            'message': f'强模型复核 {len(peak_names)} 张事件峰值帧（{model}）',
        })

    clean_config = _scrub_config_for_pass_a(config)
    roi_dir = os.path.join(job_dir, 'roi_patches')
    os.makedirs(roi_dir, exist_ok=True)
    batches = [peak_names[i:i + _PEAK_BATCH_SIZE]
               for i in range(0, len(peak_names), _PEAK_BATCH_SIZE)]

    def _run_peak_batch(batch):
        pp._raise_if_cancelled(on_progress)
        full_paths = [by_name[n]['frame_path'] for n in batch]
        compressed_full = pp._compress_frames_for_review(full_paths, max_side=1024, quality=82)

        # 为这批峰值关键帧提取 100% 原生尺寸局部特写切片并持久化到 job_dir/roi_patches。
        #
        # 送进去的图片数量从此不再等于帧数量（一帧 = 1 张整帧 + 最多 2 张原生特写）。
        # 模型只能靠文字知道哪张图属于哪一帧，所以这里必须逐张编号列清单——早先只写
        # 「Detail Crop for X is attached」不给序号，模型无从对位，而它一旦按图片数逐个
        # 作答，下游按位兜底就会把特写的读数挂到别的帧上。配合 strict=True 双保险。
        detail_images = []
        image_manifest = []
        batch_roi_map = {}
        for idx, (n, fp) in enumerate(zip(batch, full_paths)):
            detail_images.append(compressed_full[idx])
            image_manifest.append(f'- Image {len(detail_images)}: FULL FRAME of {n}')
            patches = extract_roi_patches(fp, patch_size=512, max_patches=2, output_dir=roi_dir)
            rel_patches = []
            for p_idx, patch in enumerate(patches):
                if patch != fp and os.path.exists(patch):
                    detail_images.append(patch)
                    rel_patches.append(os.path.relpath(patch, job_dir))
                    image_manifest.append(
                        f'- Image {len(detail_images)}: native-scale DETAIL CROP #{p_idx + 1} '
                        f'of {n} (same frame as its full frame above, not a separate frame)'
                    )
            batch_roi_map[n] = rel_patches

        listing = '\n'.join(f'{i + 1}. {n}' for i, n in enumerate(batch))
        manifest_text = '\n'.join(image_manifest)

        user_text = (
            f'You are given {len(detail_images)} images that together show only '
            f'{len(batch)} distinct frames. Detail crops are native-scale magnifications of a '
            f'frame you already have — they let you read microscopic fastener types, tool '
            f'blade/bit models, and material cross-sections. They are NOT additional frames.\n\n'
            f'Image manifest (in the order attached):\n{manifest_text}\n\n'
            f'Frames to analyze:\n{listing}\n\n'
            f'Read these {len(batch)} peak frames with forensic scrutiny, folding each frame\'s '
            f'detail crops into that frame\'s reading. Inspect material surfaces, fasteners, '
            f'tool mechanisms, and boundaries.\n'
            f'Return a JSON array of EXACTLY {len(batch)} objects — one per frame, in the exact '
            f'order of the {len(batch)} frame names listed above, never one per image. Set each '
            f'object\'s "frame" field to the exact frame name from the list above.'
        )

        try:
            raw = pp._multimodal_chat(
                clean_config, _PASS_A_SYSTEM,
                user_text,
                detail_images, model=model, max_tokens=4096, timeout=240,
            )
            parsed = _parse_facts_array(raw, batch, strict=True)
            for n, fact in parsed.items():
                if n in batch_roi_map:
                    fact['roi_patches'] = batch_roi_map[n]
            return parsed
        except pp.GenerationCancelled:
            raise
        except Exception as e:
            if sys.stdout:
                print(f'[REVERSE] 峰值帧复核失败，这批沿用 Pass A 的读数 {batch}: {e}')
            return {}

    refined = {}
    if batches:
        results = pp._map_parallel(
            _run_peak_batch,
            [(i, b) for i, b in enumerate(batches)],
            pp.review_concurrency(config),
        )
        for chunk in results.values():
            refined.update(chunk or {})

    by_frame = {f['frame']: f for f in facts_payload.get('facts') or []}
    for name, fact in refined.items():
        if name in by_frame:
            by_frame[name] = _merge_verified_fact(by_frame[name], fact, model)
    facts_payload['facts'] = [by_frame[f['frame']] for f in facts_payload.get('facts') or []]
    facts_payload['peak_verified'] = len(refined)
    # 复核用了哪个模型要落进产物：UI 上「峰值复核模型」是可选的，选了什么、有没有
    # 真的生效，只有这里说了算。
    facts_payload['peak_verify_model'] = model

    with open(os.path.join(job_dir, _FRAME_FACTS_FILENAME), 'w', encoding='utf-8') as f:
        json.dump(facts_payload, f, ensure_ascii=False, indent=2)
    return facts_payload


# ── Pass B 的视觉输入 ────────────────────────────────────────────────────────

def _digest_frame_paths(overview, facts):
    """facts 顺序对应的帧全路径。缺路径的帧留 None 占位，序号不许错位。

    拼图与 digest 的对齐全靠这个顺序：模型是按「第几格」去认「第几条事实」的，
    这里悄悄跳过一帧，后面每一格的对应关系就整体平移一位。
    """
    by_name = _frames_by_name(overview)
    out = []
    for fact in facts:
        entry = by_name.get(fact.get('frame'))
        out.append(entry.get('frame_path') if entry else None)
    return out


def build_pass_b_sheets(job_dir, overview, facts,
                        columns=_PASS_B_SHEET_COLUMNS, page_size=_PASS_B_SHEET_PAGE_SIZE):
    """把送审帧按 facts 顺序拼成分页拼图。

    返回 `[{'path': 拼图路径, 'start': 首格在 facts 里的下标, 'facts': 该页的事实切片}]`。
    `start` 必须显式带着走，不能靠累加前几页的长度反推——中间跳过缺失帧之后，累加值
    就不再等于真实下标，而这个偏移恰恰是「模型把 A 帧的画面读成 B 帧的事实」的成因。

    尽力而为：ffmpeg 缺失、帧文件不在、拼接失败一律返回 []，调用方退回纯文本 Pass B。
    拼图是让节拍聚类看得见画面的增强，不该反过来变成一道能卡死反推的门禁。

    只拼**路径齐全的连续段**：中间缺一帧就换页，而不是把它跳过去接上下一帧——跳过去
    等于让后面每一格都错位一位，模型会把 A 帧的画面当成 B 帧的事实来读，这比没有拼图
    坏得多。
    """
    try:
        from pathlib import Path
        from tools.collage import build_keyframe_collage
    except ImportError:
        return []

    paths = _digest_frame_paths(overview, facts)
    sheet_dir = os.path.join(job_dir, _PASS_B_SHEET_DIRNAME)

    # 连续可用段：(起点下标, [帧路径…])
    runs, start, run = [], 0, []
    for idx, path in enumerate(paths):
        if path and os.path.exists(path):
            if not run:
                start = idx
            run.append(path)
            continue
        if run:
            runs.append((start, run))
            run = []
    if run:
        runs.append((start, run))

    sheets = []
    try:
        os.makedirs(sheet_dir, exist_ok=True)
        for run_start, run_paths in runs:
            for offset in range(0, len(run_paths), page_size):
                chunk = run_paths[offset:offset + page_size]
                lo = run_start + offset
                out_path = os.path.join(sheet_dir, f'sheet_{lo + 1:03d}_{lo + len(chunk):03d}.jpg')
                # max_frames=0 关掉降采样：build_keyframe_collage 默认会把超过 25 张的
                # 输入抽稀成 25 张（那是给整片总览拼图用的），而这里的拼图是**带下标
                # 契约**的——第 k 格必须恰好是 FRAME FACTS 第 lo+k 条，_sheet_layout_block
                # 就是照这个对应关系写给模型的。抽掉任何一张，后面每一格都错位一位，
                # 模型会把 A 帧的画面当成 B 帧的事实来读，正是本函数 docstring 说的
                # 「比没有拼图坏得多」。page_size 现在是 20，侥幸没撞上默认值 25；
                # 但那是巧合不是保证，把它显式钉死。
                made = build_keyframe_collage([Path(p) for p in chunk], Path(out_path),
                                              columns=columns, max_frames=0)
                if not made:
                    return []   # ffmpeg 不可用：别交出半套拼图，那比没有更难对齐
                sheets.append({'path': str(made), 'start': lo,
                               'facts': facts[lo:lo + len(chunk)]})
    except Exception as e:
        if sys.stdout:
            print(f'[REVERSE] Pass B 拼图生成失败，本次退回纯文本聚类: {e}')
        return []
    return sheets


def _sheet_layout_block(sheets, columns=_PASS_B_SHEET_COLUMNS):
    """告诉模型每张拼图的第几格对应 FRAME FACTS 的哪一条。

    不写这段，拼图就只是一堆没有身份的缩略图——模型看得见画面，却没法把它挂回带
    时间戳的事实上，`evidence_frames` 会开始写幻觉文件名。
    """
    lines = []
    for i, sheet in enumerate(sheets, start=1):
        chunk = sheet['facts']
        if not chunk:
            continue
        lo = sheet['start'] + 1
        lines.append(f'SHEET {i}: FRAME FACTS #{lo}–#{lo + len(chunk) - 1}  '
                     f'({chunk[0].get("frame")} … {chunk[-1].get("frame")})')
    return (
        f'\n==================== CONTACT SHEETS ====================\n'
        f'{len(sheets)} tiled sheet image(s) are attached, {columns} tiles per row, '
        f'read left to right then top to bottom. The tiles are the SAME frames as FRAME '
        f'FACTS above, in the SAME order:\n' + '\n'.join(lines) + '\n\n'
        'Use the sheets to judge what the text cannot carry: how far one surface advances '
        'across consecutive tiles, whether an object stayed put, whether a trace from an '
        'earlier tile is still visible later. Beat boundaries are where the sheet shows the '
        'work changing SYSTEM, not merely advancing.\n'
        'The frame facts remain the authority on WHAT is present. Never name a tool, '
        'material, or operation that no frame fact records, even if you think you see it.\n'
    )


# ── Pass B：节拍聚类 ─────────────────────────────────────────────────────────

_PASS_B_SYSTEM = """You cluster observed frame facts into a production beat ladder for a construction / renovation / restoration time-lapse.

You are given:
- FRAME FACTS: per-frame observations with timestamps. This is the only evidence that exists.
- CHANGE EVENTS: detected state jumps, each with a start, peak, end, and evidence frames.
- TIME WINDOWS: the same footage summarised again on a fixed five-second grid — what entered the frame, what left it, and the concrete state at the end of each window. Computed mechanically from the frame facts, so it is evidence, not opinion.

RULES
- Every five-second window must fall inside some beat. A window whose NEW/GONE items appear in no beat's narration is work you have skipped — go back and widen a beat or claim it. The windows exist to catch exactly that.
- Every change event must be claimed by exactly one beat, via source_event_ids. Zero unclaimed, zero double-claimed. A single beat may and often should claim SEVERAL adjacent events — events are detected state jumps, not production beats.
- A beat is a PRODUCTION MILESTONE, not a state jump. It must declare TWO OR THREE tightly coupled operations in package_operations that share ONE terminal product (for example ["cut", "fit", "fasten"] all producing one boarded ceiling). A beat carrying only one operation is under-scoped — widen its window and merge the adjacent events that belong to the same milestone.
- Never mix two different physical SYSTEMS in one beat (wiring and wall panels are separate milestones), but the two-to-three operations that jointly produce one milestone belong together in one beat.
- A beat must produce a full visible milestone, not a token patch. If two adjacent windows continue the same milestone, merge them.
- Four-Zone Spatial Scanning: In every beat, scan four spatial domains for physical delta: 1. Top/Overhead (roof, beams, ceiling, fixtures, skylights), 2. Middle (walls, framing, openings, wiring), 3. Bottom (floor, sub-base, insulation, finish), 4. Peripherals & Spoil (debris removal, material balance). Material & Spoil Balance: Demolition/clearing must account for where debris goes (e.g. bundled or hauled out); installation must account for raw material consumption. Zero Phantom Changes: Never describe action in only one zone while letting another zone's damage or debris disappear without worker action.
- Monotonic Chronological Inheritance across Spaces: When passing through a doorway or opening into a new space (space label changes), the first beat in the new space must physically inherit all completed exterior/prior work. Never regress already-finished elements (e.g. never describe a roof leaking or ground full of dead leaves if previous beats cleared/repaired them).
- macro_environment: ONE to THREE items describing the macro terrain, geology, climate/lighting, and spatial metric envelope visible in this beat — "arid desert sandstone cliff with natural ambient sunlight", "loose reddish-tan desert sand ground with ripples".
- visible_details: THREE to SIX items. Each names a material with its colour, texture or condition and where it sits — "yellow fibreglass batts in the left wall bays", not "insulation". These items are the only place the reference film's actual look survives into the prompt; a bare noun brings back a generic version of this work, not this film.
- persistent_traces: AT LEAST TWO visible marks this beat leaves behind, each naming the mark AND the surface it sits on.
- evidence_frames: list AT MOST THREE frames per beat — the one that best shows the start, the one that best shows the work, and the one that best shows the result. Do not echo every frame in the window; a long frame list is the single biggest cause of a reply that gets cut off before it finishes.
- Keep the SENTENCE fields (visual_subject, visible_action, visible_result, state_before, state_after) under thirty words each. Length belongs in visible_details and persistent_traces, where it buys concrete look; in the sentence fields it only buys narration.
- Do not restate scene_signature in every beat. It is written once, at the top, and applies throughout.
- space: name the physical space the camera is filming FROM, as a short lowercase label ("wooded slope outside", "entrance tunnel", "main room", "sleeping alcove"). Reuse the SAME label verbatim for every beat shot in that space. Start a NEW label ONLY when the camera has physically moved into a different enclosed space through an opening — never for a reframe, a closer angle, or a pan inside one space, and never because the work changed. Every beat filmed outside the structure shares ONE outdoor label. This field is the only record of how many times the film walks through a doorway; a film that enters a corridor and later enters a room off it has THREE labels, and collapsing them into one deletes that second entry from the reproduction.
- Every claim must trace to a frame. Never write a tool, material, worker, or operation that no frame fact mentions.
- state_before / state_after must be concrete spatial completion extent, never "partially done".
- banned_elements: things a renovation of this type would plausibly involve but that appear in NO frame fact. Be generous here — this list is what stops the prompt writer from hallucinating later.
- stage: classify each beat as exactly one of the ten below. Read these definitions — the stage drives a construction-dependency check, and a beat filed under the wrong one gets the ladder rejected for a violation it does not actually have.
  - transition: camera moving through an opening or doorway into a new enclosed space (pure camera move across the threshold, sterile of workers, carrying NO construction tasks; operation is "threshold" or "过门", package_operations is ["threshold"] or []).
  - demolition: tearing out, clearing, hauling away debris.
  - structural: foundations, framing, load-bearing walls, roof structure, cutting an opening through a structural wall.
  - rough_in: services that later get covered up — electrical cable, plumbing pipe, ductwork. ONLY the services themselves.
  - enclosure: closing a cavity up — wall studs, insulation batts, sheathing, wall boards, ceiling boards. Wall framing plus insulation belongs HERE, not in structural and never in fixtures.
  - surface: finish layers — primer, paint, plaster, cladding, wall panelling and trim, wet trades.
  - floor: floor build-up and finishing — sub-base, joists, floorboards, floor coverings.
  - fixtures: powered or plumbed equipment being installed — light fittings, sockets, switches, taps, appliances. A beat with no powered or plumbed device in it is NEVER fixtures.
  - furnishing: loose furniture, textiles, decor being moved in and arranged.
  - reveal: the final walk-through / hero shot of the finished result.
- Threshold Beat Purity (过门镜头独立与纯净零施工规则):
  - When the camera moves into a new enclosed space through an opening (the space label changes), the crossing must be recorded as an independent "transition" beat.
  - Never combine a doorway/threshold crossing with construction operations (e.g. clearing or framing) into a single beat.
  - The crossing beat is sterile of workers and carries no construction work; the subsequent construction work begins in the following beat.
- 2X Speed Timelapse Model & Strict Duration Breakdown (按秒数拆拍与 2 倍速硬规则):
  - The reference video is a ~2x fast-forward timelapse. In source footage timestamps, the ideal duration of each trade milestone is strictly 2.5s to 5.5s (which represents 5.0s~11.0s of actual physical trade work).
  - HARD SPLIT ON OVER-LONG BEATS (>6.0s): NEVER allow any single beat to span longer than 6.0s. If a construction stage spans 6.0s or longer (e.g. 7.3s of structural work or 10.0s of final walkthrough), you MUST SPLIT it into 2 or more distinct sub-milestones (e.g. split structural into rough framing vs joist reinforcement/boarding; split reveal into final soft-furnishing/lighting closeout vs camera pull-back hero reveal).
  - HARD MERGE ON FLEETING MICRO-BEATS (<2.0s): Avoid micro-beats under 2.0s. If an action lasts under 2.0s (e.g. 1.2s placing loose items), MERGE it into the preceding or following trade milestone (NEVER merge across space boundaries or merge a transition beat with construction work).
  - TARGET BEAT DENSITY: For a 30s~40s video, aim for approximately 8 to 11 concise, evenly-spaced production beats (each 2.5s~5.0s). Never collapse the narrative into 5~7 bloated over-long beats!
- Action-Tool-SFX Triad Binding: When given change events with triad evidence (pre_state, action_peak, post_state) and audio_sfx pulses, declare the exact physical action verb and geometric tool shown at the action peak, matching the acoustic strike/cutting/drilling impact.
- Construction Sequence Monotonicity: Strictly adhere to the 9-stage sequence (demolition -> structural -> rough_in -> enclosure -> surface -> floor -> fixtures -> furnishing -> reveal). Rough-in services must precede cavity enclosure/boarding; structural sub-base must precede floor finishing.
- Boundary Precision: Beat start and end timestamps must strictly align with actual visible state transitions shown in the contact sheets and frame facts.
- Beats are ordered by time and must not overlap.

- scene_signature: ONE sentence naming the venue and how it looks throughout — its structure, its materials, its weathering, its surroundings, its standing light. Write what is true in the first frame and still true in the last. No work, no progress, no beat content. This is the one line that says why the reference film looks like itself; a generic version of it ("an interior space under renovation") is worse than none.

OUTPUT
Return one JSON object, no prose, no code fences:
{
  "video_duration_sec": <number>,
  "scene_signature": "<one sentence, under thirty words>",
  "banned_elements": ["..."],
  "beats": [{
    "id": "B01",
    "start": <sec>, "end": <sec>,
    "stage": "<one of the ten>",
    "space": "<short label of the space this beat is filmed in, reused verbatim across beats>",
    "macro_environment": ["..."],
    "operation": "<the dominant physical operation naming this milestone>",
    "package_operations": ["<two or three tightly coupled operations sharing one terminal product; or [\"threshold\"] for transition>"],
    "visual_subject": "...",
    "visible_details": ["..."],
    "visible_action": "...",
    "visible_result": "...",
    "state_before": "...",
    "state_after": "...",
    "persistent_traces": ["..."],
    "workers_present": true|false,
    "source_event_ids": ["E01"],
    "evidence_frames": ["review_007.png"],
    "confidence": 0.0-1.0
  }]
}"""


def _facts_digest(facts):
    """事实摘要。行首的 #N 是拼图对齐用的：拼图里的格子没有文件名，模型只能靠序号
    把「第几格的画面」挂回「第几条事实」。改这里的编号就必须同步改 `_sheet_layout_block`。"""
    lines = []
    for i, f in enumerate(facts, start=1):
        parts = [f'#{i} [{f.get("timestamp")}s] {f.get("frame")}']
        if f.get('subject'):
            parts.append(f'subject={f["subject"]}')
        zones = f.get('spatial_zones') or {}
        if isinstance(zones, dict) and any(zones.values()):
            z_parts = []
            if zones.get('overhead') and zones['overhead'].lower() != 'none':
                z_parts.append(f"top:{zones['overhead']}")
            if zones.get('facade_and_walls') and zones['facade_and_walls'].lower() != 'none':
                z_parts.append(f"wall:{zones['facade_and_walls']}")
            if zones.get('floor') and zones['floor'].lower() != 'none':
                z_parts.append(f"floor:{zones['floor']}")
            if zones.get('peripherals_and_spoil') and zones['peripherals_and_spoil'].lower() != 'none':
                z_parts.append(f"spoil:{zones['peripherals_and_spoil']}")
            if z_parts:
                parts.append(f'zones=[{", ".join(z_parts)}]')
        if f.get('completion_extent'):
            parts.append(f'extent={f["completion_extent"]}')
        if f.get('materials'):
            parts.append(f'materials={"/".join(f["materials"])}')
        if f.get('material_specs'):
            parts.append(f'mat_specs={"/".join(f["material_specs"])}')
        if f.get('tools'):
            parts.append(f'tools={"/".join(f["tools"])}')
        if f.get('tool_specifics'):
            parts.append(f'tool_specs={"/".join(f["tool_specifics"])}')
        if f.get('fastening_and_bonding'):
            parts.append(f'fasteners={"/".join(f["fastening_and_bonding"])}')
        if f.get('traces'):
            parts.append(f'traces={"/".join(f["traces"])}')
        if f.get('micro_traces'):
            parts.append(f'micro_traces={"/".join(f["micro_traces"])}')
        parts.append(f'workers={"yes" if f.get("workers_present") else "no"}')
        parts.append(f'conf={f.get("confidence")}')
        lines.append(' | '.join(parts))
    return '\n'.join(lines)


def _events_digest(events):
    lines = []
    for e in events:
        part = (
            f'{e.get("event_id")}: start={e.get("start")}s peak={e.get("peak")}s end={e.get("end")}s '
            f'evidence={",".join(e.get("evidence_frames") or [])}'
        )
        triad = e.get("triad_frames")
        if isinstance(triad, dict):
            pre = triad.get("pre_state") or "none"
            act = triad.get("action_peak") or "none"
            post = triad.get("post_state") or "none"
            part += f' | triad=[pre:{pre}, action:{act}, post:{post}]'
        cue = e.get("audio_cue")
        if isinstance(cue, dict) and cue.get("has_acoustic_spike"):
            part += f' | audio_sfx=[spike@{cue.get("transient_time")}s, +{cue.get("delta_db")}dB]'
        lines.append(part)
    return '\n'.join(lines)


def cluster_beats(config, job_dir, facts_payload=None, on_progress=None, max_rework=1):
    """Pass B。产出并写入 job_dir/timelapse_beats.json。

    校验不过就定向回炉，形态与 composer 的「校验 → 回炉一轮 → 留痕」一致：把违规项
    原样回喂给模型，而不是自己去改 beats——自动修 beats 等于把观察结果篡改成合规的
    样子，那恰恰是反推最不能干的事。
    """
    with open(os.path.join(job_dir, 'video_overview.json'), 'r', encoding='utf-8') as f:
        overview = json.load(f)
    if facts_payload is None:
        with open(os.path.join(job_dir, _FRAME_FACTS_FILENAME), 'r', encoding='utf-8') as f:
            facts_payload = json.load(f)

    facts = facts_payload.get('facts') or []
    events = overview.get('change_events') or []
    duration = (overview.get('media_metadata') or {}).get('duration_sec') or 0

    # 定长窗与帧事实同源，但换了一根轴：事实是逐帧的、密到读不完，窗是逐五秒的、
    # 直接说「这五秒里画面多了什么少了什么」。给模型两套读法，是为了让「某一段没被
    # 任何一拍认领」这件事在输入里就显形，而不是等成片出来才发现少了一道工序。
    windows = analyze_time_windows(facts, duration)
    user = (
        f'VIDEO DURATION: {duration} seconds\n\n'
        f'==================== FRAME FACTS ====================\n{_facts_digest(facts)}\n\n'
        f'==================== CHANGE EVENTS ====================\n{_events_digest(events)}\n\n'
        f'==================== TIME WINDOWS (fixed {WINDOW_SECONDS:g}s grid) ===================='
        f'\n{_windows_digest(windows)}\n'
    )

    # 拼图拿得到就走多模态。拿不到（ffmpeg 缺失、帧文件不在）退回纯文本，产出会粗一档
    # 但不该因此跑不完——所以这里是分支，不是断言。
    sheets = build_pass_b_sheets(job_dir, overview, facts)
    sheet_paths = [s['path'] for s in sheets]
    if sheets:
        user += _sheet_layout_block(sheets)
        if on_progress:
            on_progress('replica_stage', {
                'stage': 'cluster_beats',
                'message': f'已生成 {len(sheets)} 张送审拼图，节拍聚类将看着画面做',
                'sheets': sheet_paths,
            })
    elif sys.stdout:
        print('[REVERSE] 无可用拼图，Pass B 退回纯文本聚类（节拍边界精度会下降）')

    def _ask_pass_b(prompt):
        # 预估输出 token 预算：基线 32768，确保长片多拍（20+ 拍）与结构回炉提示词不撞硬上限
        target_tokens = 32768
        if sheet_paths:
            # `_multimodal_chat` 的 temperature 固定 0.1，比纯文本路径的 0.2 还低——
            # 聚类要的是稳定复现，不是花样，低一点正合适。
            return pp._multimodal_chat(
                config, _PASS_B_SYSTEM, prompt, sheet_paths,
                model=(config or {}).get('model'), max_tokens=target_tokens, timeout=360,
            )
        return pp._chat(config, _PASS_B_SYSTEM, prompt, temperature=0.2,
                        max_tokens=target_tokens, timeout=240)

    violations = []
    beats_doc = None
    # 解析失败与校验未过是两类不同的失败，各有各的预算。共用一个计数器的话，一次
    # 「模型忘了转义引号」就会吃掉本该留给结构回炉的那一轮。
    parse_budget = _PARSE_RETRY_BUDGET
    truncated_once = False
    attempt = 0
    while attempt <= max_rework:
        pp._raise_if_cancelled(on_progress)
        if on_progress:
            on_progress('replica_stage', {
                'stage': 'cluster_beats',
                'message': '正在把帧事实聚类成节拍阶梯…' if attempt == 0 else '节拍阶梯校验未过，正在定向回炉…',
            })
        prompt = user
        if violations:
            prompt = user + (
                '\n==================== VALIDATION FAILURES (FIX THESE) ====================\n'
                + '\n'.join(f'- {v["message"]}' for v in violations)
                + '\nRe-inspect the frame facts before changing beat order. A misread frame is far '
                  'more likely than an impossible build order.\n'
            )
        if truncated_once:
            # 不说清楚它还会再写一份一样长的。截断的解药是写短，不是重试。
            prompt += (
                '\n==================== YOUR PREVIOUS REPLY WAS CUT OFF ====================\n'
                'It ran past the output limit before it finished. Produce FEWER, WIDER beats by '
                'merging adjacent events that belong to the same milestone, list at most two '
                'evidence_frames per beat, and keep every prose field under twenty words.\n'
            )
        try:
            raw = _ask_pass_b(prompt)
        except pp.ResponseTruncated as e:
            # 多模态路径把截断当异常抛（finish_reason=length），纯文本路径只能靠
            # parse_json_reply 从半截 JSON 里认出来。两条路必须汇进同一个「写短一点再来」
            # 的分支——否则上了拼图之后，一次本来能回炉的截断会变成整单失败。
            parse_budget -= 1
            truncated_once = True
            if parse_budget < 0:
                raise
            if on_progress:
                on_progress('replica_stage', {
                    'stage': 'cluster_beats',
                    'message': f'模型回复太长被截断（{e}），要求它合并节拍、写短一点后重试…',
                })
            continue
        try:
            beats_doc = parse_json_reply(raw)
        except TruncatedReply as e:
            parse_budget -= 1
            truncated_once = True
            _dump_bad_reply(job_dir, 'cluster_beats_truncated', raw, e)
            if parse_budget < 0:
                raise
            if on_progress:
                on_progress('replica_stage', {
                    'stage': 'cluster_beats',
                    'message': '模型回复太长被截断，要求它合并节拍、写短一点后重试…',
                })
            continue
        except ValueError as e:
            # 既有的 beat ladder 生成循环就是这么处理的：解析炸了就再要一次，别让
            # 几十次视觉调用的成果陪葬。
            parse_budget -= 1
            _dump_bad_reply(job_dir, 'cluster_beats', raw, e)
            if parse_budget < 0:
                raise
            if on_progress:
                on_progress('replica_stage', {
                    'stage': 'cluster_beats',
                    'message': f'模型回复不是合法 JSON，重新要一次（剩余 {parse_budget + 1} 次）…',
                })
            continue
        if not isinstance(beats_doc, dict):
            raise ValueError(f'Pass B 回复应当是一个 JSON 对象，实际是 {type(beats_doc).__name__}')

        beats_doc.setdefault('video_duration_sec', duration)
        beats_doc.setdefault('banned_elements', [])
        beats_doc['source_video'] = overview.get('source_video')
        beats_doc['variant_of'] = None
        beats_doc['mutation_axes'] = []
        # 归一必须在校验之前：键名漂移会伪装成十几条「缺少字段」，回炉预算只有一轮，
        # 拿去修一个纯搬运就能解决的问题，等于把那一轮扔了。
        normalize_beat_keys(beats_doc)
        _renumber_beats(beats_doc)
        normalize_beat_spaces(beats_doc)
        reconcile_event_coverage(beats_doc, overview)
        attach_coverage_frames(beats_doc, overview)
        # 定长窗随文档一起落盘：卡点上要拿它跟节拍对照，回炉时也要用同一份，不能
        # 每次重算一遍——那样两次看到的「画面变化」可能不是同一份。
        beats_doc['time_windows'] = windows
        attach_scene_constants(beats_doc, facts)

        # 注入 2x 倍速与双轴时序计算元数据
        speed_multiplier = float(beats_doc.get('speed_multiplier') or 2.0)
        beats_doc['speed_multiplier'] = speed_multiplier
        try:
            from .duration_engine import calculate_beat_word_quota
            for b in beats_doc.get('beats') or []:
                start, end = _num(b.get('start')), _num(b.get('end'))
                if end > start:
                    s_dur = round(end - start, 1)
                    b['screen_duration_sec'] = s_dur
                    b['action_duration_sec'] = round(s_dur * speed_multiplier, 1)
                    b['speed_factor'] = speed_multiplier
                    b['voiceover_quota'] = calculate_beat_word_quota(s_dur, lang='zh')
        except Exception:
            pass

        violations = validate_beats(beats_doc, overview)
        if not [v for v in violations if v['level'] == 'error']:
            break
        attempt += 1

    beats_doc['validation'] = violations
    out_path = os.path.join(job_dir, _BEATS_FILENAME)
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(beats_doc, f, ensure_ascii=False, indent=2)
    return beats_doc


# ── 中文对照 ─────────────────────────────────────────────────────────────────

# 要翻译的字段，与人工卡点上那张卡展示/可编辑的字段一一对应。
# operation / package_operations 是**工序名**：合成器按它们做相位判定，翻译只作提示，
# 不参与任何判据。
TRANSLATE_FIELDS = (
    'visual_subject', 'operation', 'visible_action', 'visible_result',
    'state_before', 'state_after', 'visible_details', 'persistent_traces',
    'package_operations', 'macro_environment',
)

_TRANSLATE_SYSTEM = """You translate a construction time-lapse beat ladder from English into Simplified Chinese for a human reviewer.

RULES
- Translate only. Never add, drop, merge, soften or "fix" any fact — the reviewer is checking these lines against real video frames, so an improved translation destroys the only thing this step is for.
- Keep it terse and concrete, in the register a Chinese site foreman would use (「把黄色保温棉塞满地梁格栅」, not 「进行保温施工作业」).
- Array fields translate item by item, same length, same order.
- Keep proper nouns, brand names and measurements as they are.

OUTPUT
Return one JSON object, no prose, no code fences, mapping each beat id to its translated fields:
{"B01": {"visual_subject": "...", "visible_details": ["..."]}, "B02": {...}}
Include only the fields you were given for that beat."""


def prune_stale_translations(previous_doc, beats_doc):
    """用户在卡点上改过的英文字段，其中文对照立刻作废。

    保存时不重跑翻译（那是一次要等的模型调用，而保存必须是即时的）；作废掉的字段在
    界面上退回只显示英文，用户按「重译中文」才会补回来。留着旧译文是最坏的选择——
    核对的人会照着中文点头，而实际送去合成的是他刚改过的英文。
    """
    old = {b.get('id'): b for b in (previous_doc or {}).get('beats') or [] if isinstance(b, dict)}
    for beat in (beats_doc or {}).get('beats') or []:
        if not isinstance(beat, dict) or not isinstance(beat.get('zh'), dict):
            continue
        before = old.get(beat.get('id')) or {}
        beat['zh'] = {key: value for key, value in beat['zh'].items()
                      if key in before and before.get(key) == beat.get(key)}
        if not beat['zh']:
            beat.pop('zh', None)
    return beats_doc


def _beat_translation_payload(beats):
    """送去翻译的最小载荷：只有 id 和需要翻译的字段。"""
    payload = []
    for beat in beats:
        item = {'id': beat.get('id')}
        for key in TRANSLATE_FIELDS:
            value = beat.get(key)
            if isinstance(value, (list, tuple)):
                items = [str(x).strip() for x in value if str(x).strip()]
                if items:
                    item[key] = items
            elif str(value or '').strip():
                item[key] = str(value).strip()
        if len(item) > 1:
            payload.append(item)
    return payload


def translate_beats(config, beats_doc, on_progress=None):
    """给每一拍补一份中文对照，写在 `beat['zh']` 里。返回翻译过的拍数。

    英文仍然是**唯一事实源**：下游提示词、合成器的相位判定、banned 门禁读的全是英文
    原文，`zh` 只喂给人工卡点的界面。所以这里不允许把翻译写回英文字段，也不允许因为
    翻译失败而让整单失败——看不懂是体验问题，翻错了写回去才是事故。

    纯文本调用，不带任何帧图：Pass A 的反注入不变量与它无关（此时 beats 已经产出，
    主题信息也不会倒流回帧事实提取）。
    """
    beats = [b for b in (beats_doc or {}).get('beats') or [] if isinstance(b, dict)]
    payload = _beat_translation_payload(beats)
    if not payload:
        return 0
    if on_progress:
        on_progress('replica_stage', {
            'stage': 'cluster_beats',
            'message': f'正在把 {len(payload)} 拍的英文观察译成中文对照…',
        })
    try:
        raw = pp._chat(
            config, _TRANSLATE_SYSTEM,
            json.dumps(payload, ensure_ascii=False),
            temperature=0.1, max_tokens=8192, timeout=180)
        data = parse_json_reply(raw)
    except Exception as e:
        # 翻译只是可读性增强，炸了就保持英文原样。
        if sys.stdout:
            print(f'[REVERSE] 中文对照生成失败（非致命，卡点仍显示英文原文）: {e}')
        return 0
    if not isinstance(data, dict):
        return 0

    translated = 0
    for beat in beats:
        item = data.get(beat.get('id'))
        if not isinstance(item, dict):
            continue
        zh = {}
        for key in TRANSLATE_FIELDS:
            value = item.get(key)
            source = beat.get(key)
            if isinstance(source, (list, tuple)):
                if isinstance(value, (list, tuple)):
                    # 条数对不上就整项丢弃：错位的对照比没有对照更容易误导核对的人。
                    items = [str(x).strip() for x in value if str(x).strip()]
                    if len(items) == len([x for x in source if str(x).strip()]):
                        zh[key] = items
            elif isinstance(value, str) and value.strip():
                zh[key] = value.strip()
        if zh:
            beat['zh'] = zh
            translated += 1
    return translated


def _renumber_beats(beats_doc):
    """按 start 排序并重排 id。模型偶尔会回 B1 / beat_1 / 乱序，schema 要求 B01 顺序不跳号。"""
    beats = [b for b in (beats_doc.get('beats') or []) if isinstance(b, dict)]
    beats.sort(key=lambda b: (_num(b.get('start')), _num(b.get('end'))))
    for i, beat in enumerate(beats, start=1):
        beat['id'] = f'B{i:02d}'
    beats_doc['beats'] = beats
    return beats_doc


def _num(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# ── 观察到的空间序列 ─────────────────────────────────────────────────────────
#
# 一条片子进过几次门，是**看出来的**，不是骨架规定的。Pass B 逐拍写下 `space`（这一拍
# 的机位在哪个空间里），这里把它归一成一条序列；序列每变一次值，就是原片里的一次进门。
#
# 在此之前，反推层根本不产出空间信息：beats_to_dimensions 只交出工序与状态，合成期再由
# 叙事骨架决定过门次数——linear_milestone 写死一次、nested_space_payoff 写死两次。于是
# 原片走廊尽头那道门无论开几次，复刻出来都只进一次门（2026-08-14 用户复盘：地堡单
# IMG 006-013 全在同一机位族里，第二个空间只是画面深处一扇永远没被推开的门）。
_DEFAULT_SPACE_LABEL = 'main space'


def normalize_space_label(value):
    """空间名归一：折叠空白、转小写、去尾部标点。空值返回空串（由调用方决定继承谁）。

    归一只为**判等**服务——判等错了后果是两个方向的：把同一个空间的两种写法当成两个
    空间，会凭空插进一次不存在的过门；反过来把两个空间当成一个，就是本函数存在的理由。
    """
    text = ' '.join(str(value or '').split()).strip().strip('.,;:!'
                                                            '。，；：').lower()
    return text


def normalize_beat_spaces(beats_doc):
    """把每拍的 `space` 归一并回填，返回归一后的标签序列。

    缺字段/写空的拍**继承上一拍**（而不是各自新开一个空间）：漏写是模型最常见的失误，
    按「未知即新空间」处理会在阶梯中间插进一串假过门。首拍缺失才退到通用名。
    老文档（2026-08-14 之前跑的、压根没有这个字段）因此整条序列只有一个空间，
    行为与改动前完全一致。
    """
    labels = []
    current = ''
    for beat in (beats_doc.get('beats') or []):
        if not isinstance(beat, dict):
            continue
        label = normalize_space_label(beat.get('space'))
        current = label or current or _DEFAULT_SPACE_LABEL
        beat['space'] = current
        labels.append(current)
    return labels


def space_sequence(beats_doc):
    """这份阶梯逐拍的空间标签序列（不改写文档）。"""
    labels, current = [], ''
    for beat in (beats_doc.get('beats') or []):
        if not isinstance(beat, dict):
            continue
        label = normalize_space_label(beat.get('space'))
        current = label or current or _DEFAULT_SPACE_LABEL
        labels.append(current)
    return labels


def space_crossings(labels):
    """空间序列 → 过门位置的 1-based 拍号列表。值一变就是一次过门，几次都算。"""
    crossings = []
    for position, label in enumerate(labels or [], 1):
        if position > 1 and label != labels[position - 2]:
            crossings.append(position)
    return crossings


# ── 定长时间窗：画面变化的第二套读法 ─────────────────────────────────────────
#
# 节拍是**语义**切分：一拍 = 一个生产里程碑，因此拍窗宽窄不一（3.5s 到 10.9s 都有），
# 而且它由模型划定。这套切法有个结构性盲点：一段时间里发生的事若没被模型认成里程碑，
# 它就不在任何一拍的叙述里，而没有任何机制会喊出来——「漏了」和「这段确实没事发生」
# 在产物里长得一模一样。
#
# 定长窗是独立的第二套读法：不问语义，只按固定 5 秒切开，逐窗统计画面里**新出现**和
# **消失**了什么。它与节拍互为对照——节拍说「这一拍在铺地板」，定长窗说「45–50s 之间
# 画面里多了碎石、少了积叶」。两者对不上的地方，就是要人去看帧的地方。
#
# 全部本地统计，不花一次模型调用：Pass A 已经把每一帧读完了，这里只是换一个轴去汇总。
WINDOW_SECONDS = 5.0

# 一个词要在窗内多少比例的帧里出现，才算这一窗的显著特征。太低会被模型的一次性措辞
# 带偏（同一样东西每帧说法都不同），太高会漏掉只在半个窗里露面的东西。
_WINDOW_SALIENT_RATIO = 0.4
# 判「新出现」时，此前各窗的出现比例必须低于这个值。留出余地是因为 Pass A 偶尔会在
# 一两帧里提前提到某样东西（反光、半遮挡），那不算它已经在画面里了。
_WINDOW_NOVEL_RATIO = 0.15

_WINDOW_STOPWORDS = frozenset("""
a an the and or of in on at to for from with without by over under near into onto
is are was were be been being this that these those it its there here some several
many few more most other another same such as than then when while during after
before across along around behind between through visible seen shows showing
partially fully mostly slightly very quite still also just only both each any all
left right front back top bottom side upper lower middle center central
""".split())


def _content_words(value):
    """一段文本里的实词集合。用词而不是整串短语做判据：同一样东西每帧的措辞都不同
    （"black ceiling sheeting" / "dark ceiling surface"），按整串统计会把一个恒常物
    拆成几十个「只出现一次」的条目，于是什么都像是刚出现的。"""
    words = set()
    raw_str = str(value or '').strip()
    canon = canonicalize_entity_phrase(raw_str)
    tokens = list(re.split(r'[^a-zA-Z一-鿿]+', raw_str.lower()))
    if canon and canon.lower() != raw_str.lower():
        tokens.extend(re.split(r'[^a-zA-Z一-鿿]+', canon.lower()))
    for token in tokens:
        if len(token) > 2 and token not in _WINDOW_STOPWORDS:
            words.add(token)
    return words


_WINDOW_FACT_FIELDS = ('materials', 'tools', 'traces')


def _fact_phrases(fact):
    """一条帧事实里所有可见物的短语（材质 / 器具 / 痕迹）。包含空间四域提取实体。"""
    out = []
    for field in _WINDOW_FACT_FIELDS:
        for item in (fact.get(field) or []):
            text = str(item).strip()
            if text:
                out.append(text)
    zones = fact.get('spatial_zones') or {}
    if isinstance(zones, dict):
        for val in zones.values():
            if val and str(val).lower() != 'none':
                text = str(val).strip()
                if text:
                    out.append(text)
    return out


def analyze_time_windows(facts, duration, window_seconds=WINDOW_SECONDS):
    """按定长窗汇总画面变化，返回逐窗的 dict 列表。

    每一窗给出：窗内帧数、有工人的帧占比、窗首窗尾的完成范围原文，以及这一窗里
    **新出现**和**消失**的可见物（用代表性短语表述，判据在词一级）。
    """
    rows = []
    for fact in (facts or []):
        ts = fact.get('timestamp')
        if ts is None:
            continue
        try:
            rows.append((float(ts), fact))
        except (TypeError, ValueError):
            continue
    if not rows:
        return []
    rows.sort(key=lambda r: r[0])

    span = _num(duration) or rows[-1][0]
    step = max(0.5, float(window_seconds))
    count = max(1, int(math.ceil(span / step - 1e-9)))

    buckets = [[] for _ in range(count)]
    for ts, fact in rows:
        buckets[min(count - 1, int(ts // step))].append((ts, fact))

    # 每窗每词的出现比例，以及这个词在本窗最常见的完整短语（判据在词一级，展示在
    # 短语一级——只报一个词，人看不出「多了什么」）。
    ratios, phrases = [], []
    for bucket in buckets:
        hits, best = {}, {}
        for _ts, fact in bucket:
            seen_here = set()
            for phrase in _fact_phrases(fact):
                for word in _content_words(phrase):
                    best.setdefault(word, {})
                    best[word][phrase] = best[word].get(phrase, 0) + 1
                    seen_here.add(word)
            for word in seen_here:
                hits[word] = hits.get(word, 0) + 1
        n = len(bucket)
        ratios.append({w: k / n for w, k in hits.items()} if n else {})
        phrases.append({w: max(v.items(), key=lambda kv: (kv[1], -len(kv[0])))[0]
                        for w, v in best.items()})

    def _elsewhere_max(word, indices):
        return max((ratios[j].get(word, 0.0) for j in indices), default=0.0)

    out = []
    for i, bucket in enumerate(buckets):
        salient = [w for w, r in ratios[i].items() if r >= _WINDOW_SALIENT_RATIO]
        novel = {w for w in salient if _elsewhere_max(w, range(i)) < _WINDOW_NOVEL_RATIO}
        # 最后一窗不判「消失」：它后面没有窗，于是窗里的一切都满足「此后再没出现」。
        # 那是片子结束了，不是东西消失了——真实数据里这一条会把片尾成品的木墙板、
        # 床品、灯串全部报成消失，正好是最不该出错的一窗（成品揭示）。
        gone = set() if i == count - 1 else {
            w for w in salient
            if _elsewhere_max(w, range(i + 1, count)) < _WINDOW_NOVEL_RATIO}
        # 三分而不是两分。只在这一窗露过面的东西（前面没有、后面也没有）同时满足
        # 「新出现」和「消失」，两栏都报就成了自相矛盾的一行——而它其实是最值得看的
        # 一类：某样东西只在这五秒里出现过，多半是一次一闪而过的工序或一件临时道具。
        brief = novel & gone
        appeared = novel - brief
        vanished = gone - brief

        # 去重是跨栏的，不是每栏各去各的：一条短语含好几个词（"wide landscape rake"
        # 里 rake 只在本窗出现、landscape 之后还有），不同的词会把同一条短语同时拉进
        # 两栏，读起来就是「它既是新出现的又是只出现一次的」。先到先得，顺序按信息量
        # 从高到低：只此一窗 > 消失 > 新出现 > 起始就有。
        claimed = set()

        def _phrases(words):
            picked = []
            for word in sorted(words, key=lambda w: -ratios[i].get(w, 0.0)):
                phrase = phrases[i].get(word)
                key = phrase.lower() if phrase else None
                if key and key not in claimed:
                    claimed.add(key)
                    picked.append(phrase)
            return picked[:6]

        brief_p, vanished_p, appeared_p = (_phrases(brief), _phrases(vanished),
                                           _phrases(appeared))

        first, last = (bucket[0][1], bucket[-1][1]) if bucket else ({}, {})
        workers = [bool(f.get('workers_present')) for _ts, f in bucket]
        row = {
            'start': round(i * step, 3),
            'end': round(min(span, (i + 1) * step), 3),
            'frame_count': len(bucket),
            'workers_present_ratio': round(sum(workers) / len(workers), 2) if workers else 0.0,
            # 第一窗没有「此前」，它的显著特征全都算新出现，那不是变化而是起始画面。
            # 混进 appeared 会让整条时间线的第一行永远是一大串噪音。
            'baseline': appeared_p if i == 0 else [],
            'appeared': [] if i == 0 else appeared_p,
            'vanished': vanished_p,
            'brief': brief_p,
            'extent_start': str(first.get('completion_extent') or '').strip(),
            'extent_end': str(last.get('completion_extent') or '').strip(),
        }
        out.append(row)
    return out


def _windows_digest(windows, max_extent_lines=48):
    """定长窗摘要，喂给 Pass B。

    完成范围原文是整份摘要里最长的部分，窗一多就会把提示词撑爆；超过阈值只保留
    每窗的增减清单。宁可让长视频少带一层细节，也不能让这块把节拍聚类挤掉。
    """
    verbose = len(windows) <= max_extent_lines
    lines = []
    for w in windows:
        head = (f'{w["start"]:.1f}–{w["end"]:.1f}s | frames={w["frame_count"]} '
                f'| workers={w["workers_present_ratio"]:.0%}')
        for label, key in (('PRESENT AT START', 'baseline'), ('NEW', 'appeared'),
                           ('GONE', 'vanished'), ('ONLY HERE', 'brief')):
            if w.get(key):
                head += f' | {label}: ' + '; '.join(w[key])
        lines.append(head)
        if verbose and w['extent_end']:
            lines.append(f'    state at {w["end"]:.1f}s: {w["extent_end"]}')
    return '\n'.join(lines)


# ── 场景恒常特征 ─────────────────────────────────────────────────────────────
#
# 整条管线是围绕**变化**建的：change_events → beats → 每拍的 delta。恒常的东西不产生
# delta，于是在数据结构里没有落脚点——一段实测（2026-08-13）：`stains`（墙上的污渍）
# 出现在 57% 的帧里，青苔、裸露树根、那盏挂着的灯泡各占一到两成，而它们在整条节拍
# 阶梯里一个字都没有。40 万字的帧事实压成 2 万字的阶梯，被压掉的正是「这条片子长
# 什么样」。
#
# 于是复刻出来的提示词有正确的工序和骨架，质感却是重新想象的：干净的混凝土而不是
# 长着绿霉污渍的，没有落叶青苔，没有那盏灯。
#
# 这个字段就是给它们的落脚点，和 `banned_elements` 正好对称：一个记「原片里永远没有
# 的东西」，一个记「原片里一直都在的东西」。同样是本地统计，不花一次模型调用。
SCENE_CONSTANT_RATIO = 0.35
_SCENE_CONSTANT_LIMIT = 8


def analyze_scene_constants(facts, min_ratio=SCENE_CONSTANT_RATIO,
                            limit=_SCENE_CONSTANT_LIMIT):
    """出现在超过 `min_ratio` 比例的帧里的可见物，按材质 / 痕迹 / 常驻器具分栏。

    判据在词一级、展示在短语一级，与 `analyze_time_windows` 同一套方法（同一样东西
    每帧措辞不同，按整串统计会把一个恒常物拆成几十个「只出现一次」的条目）。
    """
    rows = [f for f in (facts or []) if isinstance(f, dict)]
    if not rows:
        return {}
    n = len(rows)

    out = {}
    for field, key in (('materials', 'materials'), ('traces', 'traces'),
                       ('tools', 'fixtures_in_shot')):
        hits, best = {}, {}
        for fact in rows:
            seen_here = set()
            for item in (fact.get(field) or []):
                phrase = str(item).strip()
                if not phrase:
                    continue
                for word in _content_words(phrase):
                    best.setdefault(word, {})
                    best[word][phrase] = best[word].get(phrase, 0) + 1
                    seen_here.add(word)
            for word in seen_here:
                hits[word] = hits.get(word, 0) + 1

        picked, claimed = [], set()
        for word, k in sorted(hits.items(), key=lambda kv: -kv[1]):
            if k / n < min_ratio:
                break
            phrase = max(best[word].items(), key=lambda kv: (kv[1], -len(kv[0])))[0]
            if phrase.lower() in claimed:
                continue
            claimed.add(phrase.lower())
            picked.append(phrase)
            if len(picked) >= limit:
                break
        if picked:
            out[key] = picked
    return out


def attach_scene_constants(beats_doc, facts):
    """算出场景恒常特征并挂到文档上。只在字段**不存在**时算一次。

    判「键在不在」而不是「值真不真」：它进合成提示词、影响每一条产物，而统计难免会
    把工人的手套、一次性道具算进来。用户在卡点上把三栏全删空之后，值是空的、键还在——
    按真值判定的话，下一次读状态就会把它们原样加回去，用户永远删不掉。
    """
    if 'scene_constants' in beats_doc:
        return beats_doc['scene_constants']
    # 一个都没统计出来也要落键，否则每次读状态都会重算一遍（几百 KB 的事实全扫）。
    beats_doc['scene_constants'] = analyze_scene_constants(facts)
    return beats_doc['scene_constants']


def anchor_reference_frame(beats_doc, overview):
    """起始锚点该照着哪一张真实帧写：原片最早的那一张送审帧。

    锚点图（IMAGE 1）是整条序列的地基——后面每一拍的画面都从它继承材质与光线。它写歪
    了，后面全歪，而组稿阶段它恰恰是凭文字空想出来的。
    """
    entries = [e for e in ((overview or {}).get('review_sampling') or {}).get('frames') or []
               if e.get('frame_path') and e.get('timestamp') is not None]
    if not entries:
        return None
    path = min(entries, key=lambda e: _num(e['timestamp'], default=1e9))['frame_path']
    return path if os.path.exists(path) else None


def ground_anchor_on_reference(config, anchor_prompt, frame_path, on_progress=None):
    """拿原片真实首帧重写锚点图提示词的画面描述。失败原样返回，绝不阻塞合成。

    形状照搬 `pp.ground_threshold_reveal_prompt`（2026-08-07 那次「废弃桥墩过门后渲成
    生锈金属格栅」的解药）：不做事后视觉判定、不重渲，只是把输入源从「没见过任何像素
    的文字猜测」换成「已经在磁盘上的真实画面」。

    对反推复刻来说这一步的收益比原场景还大：合成器从头到尾看不到任何一帧原片，80 秒
    视频最终只经由约七千字文本抵达写手。给它看一眼真正的起点，是最省的一次纠偏——
    整单只多一次调用，而锚点图的材质与光线会被后面每一拍继承。
    """
    if not anchor_prompt or not frame_path:
        return anchor_prompt
    system = (
        "You are rewriting ONE photoreal image prompt: the opening anchor frame of a "
        "reconstruction sequence. You are shown the ACTUAL first frame of the reference film "
        "this sequence reproduces.\n\n"
        "Rewrite the prompt you are given so that its structure, materials, weathering, "
        "vegetation, clutter and light match what you can see in the reference frame. Keep its "
        "subject, its camera framing and its intent exactly as written — you are correcting "
        "what the place is made of and what it looks like, not what the shot is of.\n\n"
        "Rules:\n"
        "1. Never introduce a material, structure or object the reference frame gives no basis for.\n"
        "2. Keep every decay, stain, moss, debris and vegetation detail the reference frame "
        "actually shows — those are why the reference film looks like itself.\n"
        "3. Do not describe the reference frame's transient contents: people, tools being held, "
        "or equipment mid-use. Standing equipment that never leaves the shot is fine.\n"
        "4. Plain visual prose only — no percentages, labels, on-screen text, or commentary.\n"
        "Respond with ONLY the rewritten prompt, nothing else."
    )
    try:
        text = pp._multimodal_chat(
            config, system,
            f'The prompt to correct:\n\n{anchor_prompt}', [frame_path], max_tokens=900)
    except Exception as exc:
        if sys.stdout:
            print(f'[REVERSE] 锚点图对齐真实首帧失败，沿用组稿阶段的提示词: {exc}')
        return anchor_prompt
    text = (text or '').strip()
    if not text:
        return anchor_prompt
    if on_progress:
        on_progress('replica_stage', {
            'stage': 'compose',
            'message': '锚点图已照着原片真实首帧校正过材质与光线',
        })
    return text


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
    labels = (('environment', 'always-present macro environment & biome'),
              ('materials', 'always-present materials and surfaces'),
              ('traces', 'always-present marks and weathering'),
              ('fixtures_in_shot', 'equipment permanently in shot'))
    for key, label in labels:
        items = [str(x).strip() for x in ((constants or {}).get(key) or []) if str(x).strip()]
        if items:
            lines.append(f'{label}: ' + '; '.join(items))
    return lines


# ── 键名漂移 ─────────────────────────────────────────────────────────────────
#
# 模型会在长回复的后半段把字段名写成近义词：2026-08-13 的那一单，前五拍老实写
# `visible_action` / `visible_result`，从第六拍起整段漂成 `visual_action` /
# `visual_result`。内容一个字不缺，全部躺在错名字底下，而校验器报的是十二条
# 「缺少字段」——把用户挡在合成门外，去修一个根本不存在的缺失。
#
# 这不该由模型来修。内容已经在文档里了：重跑一次要重付一次调用，措辞会被重新生成
# （可能偏离画面），而且极可能在别处再漂一次。这里做的是纯搬运。
#
# 纪律：**只搬运，不生成，绝不覆盖**。目标键已经有值时一律不动，连错名的那份也
# 留着——两个都有值意味着模型写了两份不同的说法，那是需要人看画面裁决的事，静默
# 挑一份等于替用户篡改观察结果。这条与「不自动修 beats」的纪律不冲突：搬运不改变
# 任何一个字的内容。
_BEAT_KEY_ALIASES = {
    'space_id': 'space',
    'room': 'space',
    'location': 'space',
    'visual_action': 'visible_action',
    'visual_result': 'visible_result',
    'visual_details': 'visible_details',
    'details': 'visible_details',
    'visible_subject': 'visual_subject',
    'persistent_trace': 'persistent_traces',
    'package_operation': 'package_operations',
    'source_event_id': 'source_event_ids',
    'evidence_frame': 'evidence_frames',
    'reference_frame': 'reference_frames',
    'environment': 'macro_environment',
    'macro_env': 'macro_environment',
    'macro_environment_specs': 'macro_environment',
}


def normalize_beat_keys(beats_doc):
    """把漂掉的字段名搬回契约名，返回搬运记录 [{'beat_id','from','to'}, …]。

    记录会写进 `beats_doc['key_normalizations']`，由 validate_beats 转成一条 warn。
    修得静默是不行的：用户得知道这一拍的动作描述是从一个错名字底下捡回来的，
    值得对着帧多看一眼。
    """
    moved = []
    for beat in (beats_doc.get('beats') or []):
        if not isinstance(beat, dict):
            continue
        for alias, canonical in _BEAT_KEY_ALIASES.items():
            if alias not in beat:
                continue
            if beat.get(canonical) or beat[alias] in (None, '', [], {}):
                continue
            beat[canonical] = beat.pop(alias)
            moved.append({'beat_id': beat.get('id'), 'from': alias, 'to': canonical})
    if moved:
        # 累加而不是覆盖，且没搬到东西时绝不清空：这条记录是这份文档的历史事实，
        # 不是一次调用的临时值。归一会在每次读状态、每次保存时重跑，第二次跑必然
        # 一处也搬不到——那时清空，等于这条 warn 只在用户看不见的那一瞬间存在过。
        beats_doc['key_normalizations'] = (beats_doc.get('key_normalizations') or []) + moved
    return moved


# ── 覆盖帧 ───────────────────────────────────────────────────────────────────
#
# `evidence_frames` 最多三张（见 _PASS_B_SYSTEM：帧列表一长，Pass B 的回复就会被
# 截断）。三张对一拍 3s 的窗够看，对一拍 10s 的窗就是每 3.4s 才有一张——同一份阶梯
# 里，长拍的画面密度只有短拍的三分之一，人工核对时长拍中段发生过什么根本看不见。
#
# 覆盖帧补的就是这一段：从**已经抽好的**帧里按时间均匀取一排，铺满整个拍窗。它是
# 纯本地计算，不多抽一帧、不多花一次模型调用，也不参与任何判据——合成器读的仍然是
# evidence_frames。它只负责让人工卡点上「这一拍的 10 秒里到底发生了什么」看得见。
COVERAGE_MIN_FRAMES = 6
COVERAGE_MAX_FRAMES = 10


def _review_frame_timeline(overview):
    """[(timestamp, 文件名), …]，按时间升序。抽帧脚本产出的全部送审帧。

    注意取的是 review_sampling 的**全集**，不是 analysis_plan 那个子集：plan 是
    「哪些帧值得花钱送给模型看」，覆盖帧是「哪些帧值得给人看」，后者不花钱，没有
    理由跟着前者一起稀疏。
    """
    rows = []
    for entry in ((overview.get('review_sampling') or {}).get('frames') or []):
        path, ts = entry.get('frame_path'), entry.get('timestamp')
        if not path or ts is None:
            continue
        try:
            rows.append((float(ts), os.path.basename(path)))
        except (TypeError, ValueError):
            continue
    rows.sort()
    return rows


def coverage_frames_for_window(timeline, start, end,
                               min_frames=COVERAGE_MIN_FRAMES,
                               max_frames=COVERAGE_MAX_FRAMES):
    """拍窗 [start, end] 内按时间均分取帧，返回 [{'frame', 'timestamp'}, …]。

    张数自适应：窗内帧不够 `max_frames` 就全给（抽帧密度是上限，这里造不出帧来），
    多了就按等时距目标点就近取 `max_frames` 张。`min_frames` 只在窗内帧数超过上限
    时作为下界兜底，正常档位的抽帧密度下每拍会落在 6~10 张。
    """
    if not timeline:
        return []
    lo, hi = sorted((_num(start), _num(end)))
    inside = [row for row in timeline if lo - 1e-6 <= row[0] <= hi + 1e-6]
    if not inside:
        # 拍窗比抽帧步长还短（用户拆拍能拆出这种窗）。给最近的一张，别让这拍空着——
        # 空着会被误读成「这一段没有画面」，而事实是这一段没被单独抽到帧。
        nearest = min(timeline, key=lambda row: min(abs(row[0] - lo), abs(row[0] - hi)))
        return [{'frame': nearest[1], 'timestamp': round(nearest[0], 3)}]

    count = max(min_frames, min(max_frames, len(inside)))
    if len(inside) <= count:
        picked = list(inside)
    else:
        span = hi - lo
        picked, used = [], set()
        for i in range(count):
            target = lo + (span * i / (count - 1) if count > 1 and span > 0 else 0.0)
            # 每个目标点消耗一张帧，所以 count 张一定取得满，也一定不重复。
            best = min((row for row in inside if row[1] not in used),
                       key=lambda row: abs(row[0] - target))
            used.add(best[1])
            picked.append(best)
        picked.sort()
    return [{'frame': name, 'timestamp': round(ts, 3)} for ts, name in picked]


def attach_coverage_frames(beats_doc, overview):
    """给每一拍挂上 `coverage_frames`。派生数据，每次都重算。

    重算而不是「缺了才补」：用户在卡点上拆拍/并拍会改动时间窗，留着上一版的覆盖帧
    等于让人对着**别的拍窗**的画面核对这一拍——比没有覆盖帧更坏。
    """
    timeline = _review_frame_timeline(overview)
    beats = beats_doc.get('beats') or []
    if not timeline:
        for beat in beats:
            if isinstance(beat, dict):
                beat.pop('coverage_frames', None)
        return beats_doc
    for beat in beats:
        if isinstance(beat, dict):
            beat['coverage_frames'] = coverage_frames_for_window(
                timeline, beat.get('start'), beat.get('end'))
    return beats_doc


# ── 校验器 ───────────────────────────────────────────────────────────────────

def _schema_path():
    from server_common import skill_reference_path
    return skill_reference_path(_SCHEMA_FILENAME, profile='omni')


def _load_schema():
    """契约唯一真源。校验器从 schema 文件读 required 清单，而不是在这里再抄一份。

    读不到返回 None，字段校验跳过（事件覆盖、证据帧、施工顺序那几组不依赖 schema，
    照跑）。降级本身由 `_load_schema_or_reason` 报到 validation 列表里——见那里的说明。
    """
    return _load_schema_or_reason()[0]


def _load_schema_or_reason():
    """(schema, 读不到的原因)。

    降级必须喊出来。原先读不到只 print 一行 stdout 就 return None：字段校验静默失效，
    而 UI 那边照样显示「节拍阶梯已通过全部机械校验」——一句在这种情况下完全错误的话，
    用户据此以为阶梯是干净的。外部配置的技能包版本旧、少了这个文件时最容易踩到。
    """
    try:
        with open(_schema_path(), 'r', encoding='utf-8') as f:
            return json.load(f), None
    except (OSError, ValueError, ImportError) as e:
        if sys.stdout:
            print(f'[REVERSE] 读不到 {_SCHEMA_FILENAME}，本次跳过 beats 字段校验: {e}')
        return None, str(e)


def _err(code, message, beat_id=None):
    return {'level': 'error', 'code': code, 'message': message, 'beat_id': beat_id}


def _warn(code, message, beat_id=None):
    return {'level': 'warn', 'code': code, 'message': message, 'beat_id': beat_id}


def is_variant_doc(beats_doc):
    """这份阶梯是不是二创变体。

    变体没有 `evidence_frames`——`_merge_variant` 有意把它改名成 `reference_frames`：
    变体不再对原片的事实负责，那些帧只剩机位与构图参考。校验器不知道这件事的话，
    每一拍都会同时报「缺少字段 evidence_frames」和「没有证据帧」，一条本来干净的
    变体阶梯在合成卡点上被判死（2026-08-12 的整单失败就是这样来的）。
    """
    return bool(beats_doc.get('variant_of') or beats_doc.get('mutation_axes'))


def validate_beats(beats_doc, overview, schema=None):
    """返回 violations 列表；error 级别会触发回炉，warn 级别只在人工卡点上高亮。

    自动校验只覆盖机械可判定的部分。体积守恒、封闭空间成因这类必须看画面才知道的，
    在这里判不了，也不该假装能判——它们是 review_beats 人工卡点存在的理由。
    """
    out = []
    schema_error = None
    if schema is None:
        schema, schema_error = _load_schema_or_reason()
    beats = beats_doc.get('beats') or []

    if not beats:
        return [_err('no_beats', '节拍阶梯为空')]

    if schema_error:
        # 静默降级会让 UI 显示「已通过全部机械校验」，而字段校验其实压根没跑。
        out.append(_warn(
            'schema_unavailable',
            f'读不到契约文件 {_SCHEMA_FILENAME}，本次**跳过了字段完整性校验**'
            f'（事件覆盖、证据帧、施工顺序等其余校验照常跑）。'
            f'技能包可能版本过旧或路径配错：{schema_error}'))
    # 键名搬运过就说出来。静默修复会让人以为模型本来就写对了，而这几拍的动作描述
    # 是从一个错名字底下捡回来的，值得对着帧多看一眼。
    moved = beats_doc.get('key_normalizations') or []
    if moved:
        ids = sorted({str(m.get('beat_id')) for m in moved})
        pairs = sorted({f'{m.get("from")}→{m.get("to")}' for m in moved})
        out.append(_warn(
            'beat_keys_normalized',
            f'{len(ids)} 拍（{"、".join(ids)}）的字段名写漂了，已按契约名归位：'
            f'{"，".join(pairs)}。内容原样搬运、未作改动，但请顺带核对这几拍的措辞。'))

    variant = is_variant_doc(beats_doc)
    out.extend(_validate_required_fields(beats_doc, beats, schema, variant=variant))
    out.extend(_validate_event_coverage(beats, overview))
    out.extend(_validate_evidence_frames(beats, overview, variant=variant))
    out.extend(_validate_time_axis(
        beats,
        beats_doc.get('video_duration_sec'),
        speed_multiplier=beats_doc.get('speed_multiplier') or beats_doc.get('source_speed_multiplier')
    ))
    out.extend(_validate_construction_order(beats, banned_elements=beats_doc.get('banned_elements')))
    out.extend(_validate_space_monotonicity(beats))
    out.extend(_validate_temporary_objects(beats))
    out.extend(_validate_composer_frame_contract(beats))
    return out


def _package_operation_bounds():
    """合成器对「一拍申报几道工序」的上下界。

    从 `validate_frame_state_contract` 的**函数签名默认值**读，而不是在这里写两个数字。
    frame_state.py 的注释里已经记着这个口径曾经在三处各写各的（schema 说 1~3、里程碑闸
    判 1~3、硬闸判 1~2），一条合法阶梯因此被判死。绝不再添第四处。
    """
    try:
        import inspect
        from .frame_state import validate_frame_state_contract as fn
        params = inspect.signature(fn).parameters
        return (params['min_package_operations'].default,
                params['max_package_operations'].default)
    except Exception:
        return None, None


def _validate_composer_frame_contract(beats):
    """预判合成器的 frame-state 硬闸，在人工卡点上就报出来。

    这条校验的由来是一个真实的设计错误（2026-08-09）：Pass B 的提示词曾经要求
    「一拍恰好一道工序」，而合成器的硬闸要求「一拍 2~3 道紧密耦合工序」——生产端系统性地
    产出消费端必须拒收的东西，五次合成尝试无一成功。规则口径在 frame_state.py，
    这里只做提前预警，不复制它。
    """
    out = []
    lo, hi = _package_operation_bounds()
    if lo is None:
        return out

    for beat in beats:
        bid = beat.get('id')
        op = str(beat.get('operation') or '').lower()
        stage = str(beat.get('stage') or '').lower()
        is_transition = (
            op in ('threshold', 'reward', 'reframe')
            or stage in ('transition', 'threshold', 'reveal')
            or bool(beat.get('bridge_stage'))
            or bool(beat.get('hard_cut'))
        )
        if is_transition:
            continue
        package = [p for p in (beat.get('package_operations') or []) if str(p).strip()]
        if not lo <= len(package) <= hi:
            out.append(_err(
                'package_operations_out_of_range',
                f'{bid} 申报了 {len(package)} 道工序，合成器要求每拍 {lo}~{hi} 道紧密耦合、'
                f'共同产出同一成果的工序。只有一道说明这拍粒度过细——把它和相邻的同一里程碑'
                f'合并成一拍，或补齐共同产出该成果的其余工序。', bid))
        if len(beat.get('persistent_traces') or []) < 2:
            out.append(_err(
                'too_few_traces',
                f'{bid} 只声明了 {len(beat.get("persistent_traces") or [])} 条遗留痕迹，'
                f'合成器要求至少两条可见痕迹。', bid))
    return out


def _validate_temporary_objects(beats):
    """临时施工物必须在进入软装之前清场——否则合成器的场景状态预检会打回整条阶梯。

    这条规则不是这里发明的，是 `prompt_pipeline.scene_state` 的既有产线契约（搬家具进场
    时画面里不该还立着脚手架、工具箱、地面防护布）。判据直接 import 它的 cue 表，不另抄
    一份——抄一份就是第二份契约，迟早和本体漂移。

    为什么是 warn 不是 error：原片里那块防护布是**真实观察**，不是错误。真正的冲突在于
    「照实复刻」与「合成器的产线规则」不兼容，该由人来裁决——补一条清场动作，还是把它从
    可见细节里去掉。判成 error 会把一个合法观察堵死；何况这里是文本启发式，与合成器自己
    的物体台账口径不完全一致，误判会挡住本来能跑通的单。
    """
    try:
        from .scene_state import TEMPORARY_OBJECT_CUES, _matches_cue
    except ImportError:
        return []

    out = []
    lingering = {}   # 命中的 cue（tarp/scaffold…） → 原文短语
    for beat in beats:
        text_fields = [beat.get('visual_subject'), beat.get('visible_action'),
                       beat.get('visible_result'), beat.get('state_after')]
        text_fields += list(beat.get('visible_details') or [])
        text_fields += list(beat.get('persistent_traces') or [])

        for phrase in [t for t in text_fields if t]:
            for cue in _matched_cues(phrase, TEMPORARY_OBJECT_CUES):
                lingering.setdefault(cue, _shorten(phrase))

        # 这一拍把它清掉了就销账。按 cue 本身销账，不按整词交集——后者会被 "a"、"the"
        # 这类虚词撞上，静默地把该报的问题销掉。
        removal_text = f'{beat.get("visible_action") or ""} {beat.get("visible_result") or ""}'.lower()
        if _REMOVAL_VERBS.search(removal_text):
            for cue in list(lingering):
                if cue in removal_text:
                    lingering.pop(cue, None)

        if beat.get('stage') in ('furnishing', 'reveal') and lingering:
            names = '、'.join(sorted(lingering.values()))
            out.append(_warn(
                'temporary_object_lingering',
                f'{beat.get("id")}（{stage_label(beat.get("stage"))}）开始时，'
                f'这些临时施工物在原片里仍然在场且没有清场动作：{names}。'
                f'复刻会照实保留它们——原片就是这么拍的，合成不会因此失败。'
                f'只有当你认为这是读帧读错了、或者不想在成片里看到它们时才需要动：'
                f'在进入软装的拍之前补一条清场动作（改那一拍的「可见动作」），'
                f'或者把它从相关拍的「可见细节 / 遗留痕迹」里去掉。',
                beat.get('id')))
            break   # 一次说清就够，不必逐拍重复同一件事
    return out


# 清场动作的措辞。按词干写，不写死形态——第一版写了 `roll up`，遇到 "rolls up" 就漏判，
# 于是一条本来已经清过场的阶梯照样被标黄。
_REMOVAL_VERBS = re.compile(
    r'remov\w*|clear\w*|fold\w*|strip\w*|dismantl\w*|'
    r'roll\w*\s+(?:up|away)|carr\w*\s+(?:\w+\s+)?(?:out|away)|'
    r'haul\w*\s+(?:\w+\s+)?(?:out|away)|hoist\w*\s+(?:\w+\s+)?away|'
    r'lift\w*\s+(?:\w+\s+)?(?:out|away)|tak\w*\s+(?:\w+\s+)?away|took\s+(?:\w+\s+)?away|'
    r'unrig\w*|derig\w*|de-rig\w*|unhook\w*|detach\w*|'
    r'clean\w*\s+(?:out|away)|'
    r'撤走|撤下|收起|卷起|清走|清出|移走|搬走|拆除|拆掉',
    re.I)


def _matched_cues(phrase, cues):
    """短语里命中的临时物 cue。销账按 cue 走，比按整词交集稳。"""
    lower = ' '.join(str(phrase or '').split()).lower()
    return [cue for cue in cues if cue in lower]


def _shorten(phrase, limit=40):
    text = ' '.join(str(phrase or '').split())
    return text if len(text) <= limit else text[:limit] + '…'


def _missing_field_reason(beat, key, prop):
    """字段是否算「缺失」，严格度由 schema 自己声明，不在这里一刀切。

    一刀切「空列表 = 缺失」是错的：`source_event_ids: []` 完全合法——一拍可以不认领
    任何变化事件（安静窗口，或整条视频压根没检出事件）。真正的覆盖不变量由
    `_validate_event_coverage` 管。这里只认 schema 写明的 minItems / minLength。
    """
    if key not in beat or beat[key] is None:
        return f'缺少字段 `{key}`'
    value = beat[key]
    types = prop.get('type')
    types = [types] if isinstance(types, str) else (types or [])

    if 'boolean' in types:
        # False 是合法值，也是 IMAGE 锚点候选的判据，不能被空值判定吃掉。
        return None if isinstance(value, bool) else f'字段 `{key}` 应为布尔值'
    if 'array' in types:
        if not isinstance(value, list):
            return f'字段 `{key}` 应为数组'
        if len(value) < (prop.get('minItems') or 0):
            return f'字段 `{key}` 至少要有 {prop["minItems"]} 项'
        return None
    if 'string' in types:
        if not isinstance(value, str):
            return f'字段 `{key}` 应为字符串'
        if len(value.strip()) < (prop.get('minLength') or 0):
            return f'字段 `{key}` 不能为空'
        return None
    return None


def _validate_required_fields(beats_doc, beats, schema, variant=False):
    out = []
    if not schema:
        return out
    # schema 的 required 是按原片阶梯写的。变体那一份的对应字段叫 reference_frames，
    # 由 `_validate_evidence_frames` 按变体口径校验，不在这里当缺失字段报。
    exempt = {'evidence_frames'} if variant else set()
    root_required = schema.get('required') or []
    for key in root_required:
        if key not in beats_doc:
            out.append(_err('missing_root_field', f'缺少根字段 `{key}`'))

    beat_schema = ((schema.get('definitions') or {}).get('beat') or {})
    beat_required = beat_schema.get('required') or []
    beat_props = beat_schema.get('properties') or {}
    for beat in beats:
        bid = beat.get('id')
        op = str(beat.get('operation') or '').lower()
        stage = str(beat.get('stage') or '').lower()
        is_transition = (
            op in ('threshold', 'reward', 'reframe')
            or stage in ('transition', 'threshold', 'reveal')
            or bool(beat.get('bridge_stage'))
            or bool(beat.get('hard_cut'))
        )
        for key in beat_required:
            if key in exempt:
                continue
            if is_transition and key == 'package_operations':
                # 过门/穿越/揭示拍允许 0~1 道工序（例如 ["threshold"] 或 []），不适用 2~3 道施工工序限制
                continue
            problem = _missing_field_reason(beat, key, beat_props.get(key) or {})
            if problem:
                out.append(_err('missing_beat_field', f'{bid} {problem}', bid))
        for key in ('state_before', 'state_after'):
            text = str(beat.get(key) or '')
            if re.search(r'partial|部分完成|some of|一些', text, re.I):
                out.append(_warn('vague_state',
                                 f'{bid} 的 `{key}` 用了含糊表述，必须写具体空间完成范围：{text}', bid))
        # 细节条数：判 warn 不判 error，schema 的 minItems 也照旧留在 1。
        # 这是**质量**下限不是**契约**下限——把它提成硬伤，会让所有存量阶梯在合成门口
        # 集体判死，而它们并没有变坏。visible_details 是原片长相在提示词里唯一的落脚点，
        # 少于三条基本等于只剩工序名（见 _PASS_B_SYSTEM 里对这个字段的要求）。
        details = [x for x in (beat.get('visible_details') or []) if str(x).strip()]
        if 0 < len(details) < 3:
            out.append(_warn('thin_details',
                             f'{bid} 只写了 {len(details)} 条可见细节。这是原片质感在提示词里'
                             f'唯一的落脚点，对着覆盖帧补到三条以上，复刻出来才像这条片子。', bid))
    return out


def _get_event_timestamp(ev):
    for k in ('peak', 'timestamp', 'time'):
        if ev.get(k) is not None:
            return _num(ev.get(k))
    if ev.get('start') is not None and ev.get('end') is not None:
        return (_num(ev.get('start')) + _num(ev.get('end'))) / 2.0
    if ev.get('start') is not None:
        return _num(ev.get('start'))
    return None


def _find_best_matching_beat(beats, ev, ev_time=None):
    if ev_time is None:
        ev_time = _get_event_timestamp(ev)
    best_beat = None
    best_overlap = -1.0
    for b in beats:
        b_start = _num(b.get('start'))
        b_end = _num(b.get('end'))
        if ev_time is not None and (b_start <= ev_time <= b_end or (b_start - 0.1 <= ev_time <= b_end + 0.1)):
            return b
        e_start = _num(ev.get('start', ev_time or 0))
        e_end = _num(ev.get('end', ev_time or 0))
        overlap = max(0.0, min(b_end, e_end) - max(b_start, e_start))
        if overlap > best_overlap:
            best_overlap = overlap
            best_beat = b
    if best_beat is None and beats:
        min_dist = float('inf')
        target_t = ev_time if ev_time is not None else 0.0
        for b in beats:
            b_mid = (_num(b.get('start')) + _num(b.get('end'))) / 2.0
            dist = abs(b_mid - target_t)
            if dist < min_dist:
                min_dist = dist
                best_beat = b
    return best_beat


def reconcile_event_coverage(beats_doc, overview=None, reconcile_unbound=True):
    """自动解决 event_double_bound (多拍认领同一事件) 以及可选的 event_unbound (漏认领事件)。

    规则：
    1. 每个 change_event 必须且只能分配给时间窗口最匹配（包含 peak/中点）的唯一一拍。
    2. 拆拍后派生的子拍绝不重复声明父拍的 event_id。
    3. 没有任何 change_event 的子拍保持 source_event_ids 为空列表 []，完全合规。
    """
    overview = overview or {}
    events = overview.get('change_events') or []
    beats = beats_doc.get('beats') or []
    if not beats:
        return beats_doc

    if not events:
        seen = set()
        for b in beats:
            eids = b.get('source_event_ids') or []
            unique_eids = []
            for eid in eids:
                if eid not in seen:
                    unique_eids.append(eid)
                    seen.add(eid)
            b['source_event_ids'] = unique_eids
        return beats_doc

    event_by_id = {e.get('event_id'): e for e in events if e.get('event_id')}

    if reconcile_unbound:
        for b in beats:
            b['source_event_ids'] = []
        for eid, ev in event_by_id.items():
            best_beat = _find_best_matching_beat(beats, ev)
            if best_beat is not None:
                if eid not in best_beat['source_event_ids']:
                    best_beat['source_event_ids'].append(eid)
    else:
        claimed_map = {}
        for b in beats:
            for eid in (b.get('source_event_ids') or []):
                claimed_map.setdefault(eid, []).append(b)
        for eid, beat_list in claimed_map.items():
            if len(beat_list) > 1:
                ev = event_by_id.get(eid, {})
                best_b = _find_best_matching_beat(beat_list, ev) or beat_list[0]
                for b in beat_list:
                    if b is not best_b:
                        b['source_event_ids'] = [x for x in (b.get('source_event_ids') or []) if x != eid]

    return beats_doc


def _validate_event_coverage(beats, overview):
    """每个 change_event 必须且只能被一拍认领。

    漏 = 原片里一次真实可见的变化被丢掉了；重 = 节拍窗口重叠。两者都会让阶梯与原片
    对不上，而这正是 1:1 复刻唯一要保证的东西。"""
    out = []
    all_ids = [e.get('event_id') for e in (overview.get('change_events') or []) if e.get('event_id')]
    claimed = {}
    for beat in beats:
        for eid in (beat.get('source_event_ids') or []):
            claimed.setdefault(eid, []).append(beat.get('id'))

    for eid in all_ids:
        holders = claimed.get(eid) or []
        if not holders:
            out.append(_err('event_unbound',
                            f'变化事件 {eid} 没有被任何一拍认领（原片里的真实变化被丢掉了）'))
        elif len(holders) > 1:
            out.append(_err('event_double_bound',
                            f'变化事件 {eid} 被多拍同时认领：{"、".join(holders)}'))
    for eid, holders in claimed.items():
        if eid not in all_ids:
            out.append(_err('event_unknown',
                            f'{holders[0]} 认领了不存在的事件 {eid}', holders[0]))
    return out


def _validate_evidence_frames(beats, overview, variant=False):
    """证据帧必须真实存在，且时间戳落在本拍窗口内。

    变体读 `reference_frames`，而且整档降级为 warn：那些帧不再是「这拍确实发生过」的
    证据，只是原片的构图参考，缺一张不该拦住合成。见 `is_variant_doc`。
    """
    out = []
    frame_key = 'reference_frames' if variant else 'evidence_frames'
    label = '参考帧' if variant else '证据帧'
    flag = _warn if variant else _err
    entries = ((overview.get('review_sampling') or {}).get('frames') or [])
    ts_by_name = {os.path.basename(e['frame_path']): e['timestamp']
                  for e in entries if e.get('frame_path')}
    for scene in (overview.get('scenes') or []):
        for key in ('frame_path', 'frame_path_start', 'frame_path_mid', 'frame_path_end'):
            path = scene.get(key)
            if path:
                ts_by_name.setdefault(os.path.basename(path), scene.get('midpoint'))

    for beat in beats:
        bid = beat.get('id')
        frames = beat.get(frame_key) or []
        if not frames:
            if not variant:
                out.append(_err('no_evidence', f'{bid} 没有证据帧，但它断言了动作与结果', bid))
            continue
        start, end = _num(beat.get('start')), _num(beat.get('end'))
        for name in frames:
            if name not in ts_by_name:
                out.append(flag('evidence_missing',
                                f'{bid} 引用了不存在的{label} {name}', bid))
                continue
            ts = _num(ts_by_name[name], default=None) if ts_by_name[name] is not None else None
            if ts is not None and not (start - 0.5 <= ts <= end + 0.5):
                out.append(_warn('evidence_out_of_window',
                                 f'{bid} 的{label} {name}（t={ts}s）落在拍窗 [{start}, {end}] 之外', bid))
    return out


_UNCOVERED_TOLERANCE_SEC = 0.5


def _validate_space_grouping(source_doc, variant_doc):
    """二创：空间的**分组**必须与原骨架逐拍一致，名字可以随载体改。

    「哪几拍在同一个空间、哪一拍换空间」和拍数、施工序一样属于被复用的节奏骨架——
    它就是这条片子进几次门、什么时候进。名字换成新载体的叫法是应该的（"main room"
    → "bus saloon"），把两个空间并成一个、或者凭空多出一个，改的就是骨架本身。
    判 error：合成期按这份序列标过门（apply_observed_space_sequence），并错了就是
    变体比原片少走进一个房间，而这件事在成片之前看不出来。
    """
    source = space_sequence(source_doc)
    variant = space_sequence(variant_doc)
    if not source or len(source) != len(variant):
        return []
    src_crossings, var_crossings = space_crossings(source), space_crossings(variant)
    if src_crossings == var_crossings:
        return []
    beats = variant_doc.get('beats') or []
    bid = None
    for position in sorted(set(src_crossings) ^ set(var_crossings)):
        if 1 <= position <= len(beats):
            bid = (beats[position - 1] or {}).get('id')
            break
    return [_err('space_grouping_changed',
                 f'空间分组被改了：原骨架在第 {src_crossings or "—"} 拍过门，变体成了第 '
                 f'{var_crossings or "—"} 拍。换空间的名字可以，改哪几拍共用一个空间不行——'
                 f'那等于改掉了这条片子进几次门。', bid)]


def _validate_time_axis(beats, duration, speed_multiplier=None):
    """时间轴：非法窗、超长、重叠，以及**没有任何一拍覆盖的时间段**。

    空洞这一条是 2026-08-13 补的。此前只查重叠不查空洞，于是「这一段被漏掉了」与
    「这一段确实没事发生」在产物里长得一模一样——而前者正是复刻会整段丢掉一道工序的
    成因。判 warn 不判 error：安静段落（比如镜头空摇）确实可以不属于任何里程碑，
    该由人看一眼定夺，不该直接拦死合成。
    
    2x 倍速适配（2026-08-16）：参考视频通常为 2x 倍速，单拍屏幕时长 span 对应的
    真实物理动作为 span * speed_multiplier。
    """
    out = []
    prev_end = None
    first_start = None
    speed = float(speed_multiplier) if speed_multiplier and speed_multiplier > 0 else 2.0
    for beat in beats:
        bid = beat.get('id')
        start, end = _num(beat.get('start')), _num(beat.get('end'))
        if first_start is None:
            first_start = start
        if end <= start:
            out.append(_err('bad_window', f'{bid} 的时间窗非法：start={start} end={end}', bid))
        span = end - start
        if span < 2.0:
            action_sec = span * speed
            out.append(_warn('beat_too_short',
                             f'{bid} 屏幕时长仅 {span:.1f}s（等效 1.0x 物理动作约 {action_sec:.1f}s，低于 2.0s 阈值），'
                             f'粒度过细，建议与相邻工序合并', bid))
        elif span > 6.0:
            action_sec = span * speed
            out.append(_warn('beat_too_long',
                             f'{bid} 屏幕时长达 {span:.1f}s（等效 1.0x 物理动作长达 {action_sec:.1f}s，超过 6.0s 建议上限），'
                             f'包含过多复合工序，建议拆分为两个具体子工序里程碑', bid))
        if duration and end > _num(duration) + 0.5:
            out.append(_warn('window_overruns',
                             f'{bid} 的 end={end}s 超出视频时长 {duration}s', bid))
        if prev_end is not None and start + 0.01 < prev_end:
            out.append(_err('window_overlap',
                            f'{bid} 与上一拍时间窗重叠（start={start} < 上一拍 end={prev_end}）', bid))
        elif prev_end is not None and start - prev_end > _UNCOVERED_TOLERANCE_SEC:
            out.append(_warn('window_uncovered',
                             f'{prev_end}s – {start}s（{start - prev_end:.1f}s）不属于任何一拍。'
                             f'对着这段的覆盖帧看一眼：若有工序发生，把它并进 {bid} 或前一拍。',
                             bid))
        prev_end = end

    if first_start is not None and first_start > _UNCOVERED_TOLERANCE_SEC:
        out.append(_warn('window_uncovered',
                         f'0s – {first_start}s（{first_start:.1f}s）不属于任何一拍。',
                         beats[0].get('id') if beats else None))
    if duration and prev_end is not None and _num(duration) - prev_end > _UNCOVERED_TOLERANCE_SEC:
        out.append(_warn('window_uncovered',
                         f'{prev_end}s – {duration}s（{_num(duration) - prev_end:.1f}s）'
                         f'不属于任何一拍，片尾这段被漏掉了。',
                         beats[-1].get('id') if beats else None))
    return out


def _validate_construction_order(beats, banned_elements=None):
    """施工依赖顺序 + 三条可机械判定的硬否决。

    其余硬否决（体积守恒、封闭空间成因、承重件临时支撑）必须看画面，不在这里判。
    按 space（空间分区）分别维护阶段单调性与遮盖层，避免室外完工转入室内时产生假阳性逆行误报。
    """
    out = []
    staged = [(b, _STAGE_RANK.get(b.get('stage'))) for b in beats]

    # 阶段逆行：按空间隔离判定。允许同级和小幅回退（真实改造确实会来回穿插），跨三级以上的倒挂才判。
    max_rank_per_space = {}
    max_rank_beat_per_space = {}
    for beat, rank in staged:
        if beat.get('stage') in ('transition', 'threshold') or str(beat.get('operation') or '').lower() in ('threshold', 'reward', 'reframe') or beat.get('bridge_stage'):
            continue
        if rank is None:
            out.append(_warn('stage_unknown',
                             f'{beat.get("id")} 的 stage 无法识别：{beat.get("stage")}', beat.get('id')))
            continue
        space = normalize_space_label(beat.get('space')) or '_default_space_'
        max_rank_so_far = max_rank_per_space.get(space, 0)
        max_rank_beat = max_rank_beat_per_space.get(space)
        if rank + 2 < max_rank_so_far:
            space_hint = f'（空间「{space}」）' if space != '_default_space_' else ''
            out.append(_err('stage_regression',
                            f'{beat.get("id")}（{stage_label(beat.get("stage"))}{space_hint}）出现在 '
                            f'{max_rank_beat}（{stage_label(_rank_to_stage(max_rank_so_far))}）之后，'
                            f'违反施工依赖顺序。先回去核对帧，误读帧远比不可能的施工顺序更常见。',
                            beat.get('id')))
        if rank > max_rank_so_far:
            max_rank_per_space[space] = rank
            max_rank_beat_per_space[space] = beat.get('id')

    stages = {b.get('stage') for b, _ in staged}

    # 「布线/水管在遮盖它的东西之后」：同一空间内遮盖层先于隐蔽工程才算违规。
    first_cover_per_space = {}
    for beat, _rank in staged:
        if beat.get('stage') in ('transition', 'threshold') or str(beat.get('operation') or '').lower() in ('threshold', 'reward', 'reframe') or beat.get('bridge_stage'):
            continue
        space = normalize_space_label(beat.get('space')) or '_default_space_'
        if beat.get('stage') in _COVERING_STAGES and space not in first_cover_per_space:
            first_cover_per_space[space] = beat

    for beat, _rank in staged:
        if beat.get('stage') == 'rough_in':
            space = normalize_space_label(beat.get('space')) or '_default_space_'
            first_cover = first_cover_per_space.get(space)
            if first_cover is not None and _num(beat.get('start')) > _num(first_cover.get('start')):
                space_hint = f'在同一空间「{space}」的 ' if space != '_default_space_' else '在 '
                out.append(_err('rough_in_after_enclosure',
                                f'{beat.get("id")} {space_hint}{first_cover.get("id")}'
                                f'（{stage_label(first_cover.get("stage"))}）之后才做隐蔽工程，'
                                f'违反硬否决「布线/水管不得晚于遮盖它的板材/面层」。先回去核对帧。',
                                beat.get('id')))

    # 供电链：有灯具/通电设备的拍，前面必须有 rough_in。
    # 豁免情况：
    # 1. banned_elements 明确声明了无暗管布线 / 禁止隐蔽布线（说明原片确实未拍摄隐蔽走线）
    # 2. 所有 fixtures 拍均为太阳能（solar/photovoltaic）、电池（battery）、发电机或明线插头设备
    if 'fixtures' in stages and 'rough_in' not in stages:
        banned_list = banned_elements or []
        has_banned_wiring = any(
            re.search(r'wiring|electrical|conduit|cable|rough.?in|走线|布线|隐蔽工程', str(x), re.I)
            for x in banned_list
        )
        if not has_banned_wiring:
            fixture_beats = [b for b in beats if b.get('stage') == 'fixtures']
            standalone_pattern = re.compile(
                r'solar|photovoltaic|panel|battery|generator|off.?grid|cord|plug|'
                r'太阳能|光伏|电池|发电机|插头|明线|便携',
                re.I
            )
            all_standalone = True
            for fb in fixture_beats:
                desc = f"{fb.get('operation') or ''} {fb.get('visual_subject') or ''} {fb.get('visible_action') or ''} {' '.join(fb.get('visible_details') or [])}"
                if not standalone_pattern.search(desc):
                    all_standalone = False
                    break
            if not all_standalone:
                lit = next((b.get('id') for b in fixture_beats), None)
                out.append(_err('power_chain_broken',
                                f'{lit} 安装了灯具/通电设备，但整条阶梯里没有任何隐蔽布线拍。'
                                f'若原片确实没拍布线，应把该拍降级为非通电设备，或把布线写进 banned_elements。',
                                lit))

    # 底漆先于面漆：同一 surface 段内的文本启发式。
    primer_seen = False
    for beat in beats:
        text = f'{beat.get("operation") or ""} {beat.get("visible_action") or ""}'.lower()
        if re.search(r'primer|底漆|打底', text):
            primer_seen = True
        elif re.search(r'finish coat|topcoat|面漆|罩面', text) and not primer_seen:
            out.append(_warn('finish_before_primer',
                             f'{beat.get("id")} 出现面漆但此前没有底漆拍。先核对帧再改顺序。',
                             beat.get('id')))
    return out


_SPACE_REGRESSION_KEYWORDS = re.compile(
    r'leaking|water drip|decayed rafter|rotted wood|collapsed ceiling|'
    r'piles of leaves|dead branches|broken rubble|bare mud|'
    r'落叶堆积|残枝枯木|天花板漏水|破损塌陷|泥泞积水|地面杂物',
    re.I
)


def _validate_space_monotonicity(beats):
    """过门时空单调继承与零状态回退校验：跨空间过门后，不得在 state_before 或 visible_details 中描写已被修复的退化状态。"""
    out = []
    if not beats:
        return out
    crossings = space_crossings([b.get('space', '') for b in beats if isinstance(b, dict)])
    for pos in crossings:
        idx = pos - 1
        if 0 <= idx < len(beats):
            beat = beats[idx]
            bid = beat.get('id')
            text = f"{beat.get('state_before') or ''} {' '.join(beat.get('visible_details') or [])}"
            matched = _SPACE_REGRESSION_KEYWORDS.findall(text)
            if matched:
                out.append(_warn(
                    'space_state_regression',
                    f'{bid}（过门进入新空间第一拍）描述了疑似已被前序工序修复的退化状态（{"、".join(set(matched))}）。'
                    f'请核对证据帧：进门首帧必须物理继承已完工的室外/前序工序，严禁状态倒退。',
                    bid
                ))
    return out


def _rank_to_stage(rank):
    for stage, r in _STAGE_RANK.items():
        if r == rank:
            return stage
    return None


# ── 二创：受控变异 ───────────────────────────────────────────────────────────

MUTATION_AXES = {
    'carrier': '载体替换',
    'environment': '地域环境',
    'material': '材质风格',
    'pacing': '节奏拍数',
    'reward': '结局奖励',
}

_MAX_AXES = 2

_MUTATE_SYSTEM = """You rewrite a beat ladder onto a new subject while preserving its rhythm skeleton exactly.

WHAT YOU MUST NOT CHANGE
- The number of beats, their order, and their id / start / end / source_event_ids. The timing skeleton is the asset being reused.
- The magnitude of progress per beat. If beat 3 originally took a wall from bare to fully boarded, the new beat 3 must take its equivalent surface equally far.
- The construction dependency order and each beat's stage.
- The trace inheritance chain: whatever a beat leaves behind must still be inherited by the beats after it.
- The SPACE structure: which beats share a space and where the label changes. That pattern is how many times the film walks through a doorway and when — part of the rhythm skeleton, not of the subject. Rename each space to fit the new carrier ("main room" -> "bus saloon"), but keep the same beats sharing a label and the same beats starting a new one; never merge two spaces into one or invent a third.

WHAT YOU REWRITE
Only along the requested mutation axes: visual_subject, visible_details, visible_action, visible_result, state_before, state_after, persistent_traces, operation, and the space NAMES (never their grouping).

RULES
- Do not carry the old carrier's construction habits onto the new one. A bus does not get brick footings; a boat hull does not get drywall screwed to studs. Re-derive the operation that achieves the SAME stage milestone on the NEW carrier.
- state_before / state_after stay concrete and spatial.
- Recompute banned_elements from scratch for the new carrier. Do not inherit the old list.
- Write a scene_signature for the NEW venue: one sentence naming what the place is, what it is made of, how it is weathered, what surrounds it, how it is lit — true from the first frame to the last. Never carry the old venue's over; a bus in a snowfield shares nothing with a mossy concrete bunker.
- Never write digits or percent symbols; spell counts as words.

OUTPUT
Return one JSON object, no prose, no code fences:
{ "scene_signature": "<one sentence, under thirty words>", "banned_elements": ["..."], "beats": [ { same fields as input, rewritten } ] }"""


def mutate_beats(config, beats_doc, axis_spec, on_progress=None, max_rework=1):
    """二创。沿受控变异轴做同构映射，时间骨架原样保留。

    axis_spec: {'axes': ['carrier', ...], 'brief': '换成废弃巴士，北欧雪地'}

    变体最容易坏在「换了载体却抄了旧载体的施工顺序」，所以映射完要重跑同一套
    施工依赖校验——事件覆盖与证据帧校验则不适用（变体不再对原片负责，证据帧降级为
    构图参考）。
    """
    axes = [a for a in (axis_spec.get('axes') or []) if a in MUTATION_AXES]
    if not axes:
        raise ValueError('至少要指定一条变异轴')
    if len(axes) > _MAX_AXES:
        # 三轴以上同时变，产出的已经不是「参考爆款」而是一个新选题——那条路走选题
        # 发动机更合适，硬走这里只会得到一个骨架对不上内容的四不像。
        raise ValueError(f'最多同时变 {_MAX_AXES} 条轴，收到 {len(axes)} 条：'
                         f'{"、".join(MUTATION_AXES[a] for a in axes)}。'
                         f'要大改请直接用选题发动机出新选题。')

    source_beats = beats_doc.get('beats') or []
    skeleton = json.dumps([
        {k: b.get(k) for k in ('id', 'start', 'end', 'stage', 'operation',
                               'visual_subject', 'visible_details', 'visible_action',
                               'visible_result', 'state_before', 'state_after',
                               'persistent_traces', 'workers_present')}
        for b in source_beats
    ], ensure_ascii=False, indent=2)

    user = (
        f'MUTATION AXES: {"、".join(MUTATION_AXES[a] for a in axes)}\n'
        f'USER DIRECTION: {axis_spec.get("brief") or "(none given — infer a natural target)"}\n\n'
        f'==================== SOURCE BEAT LADDER ====================\n{skeleton}\n'
    )

    violations = []
    variant = None
    parse_budget = _PARSE_RETRY_BUDGET
    attempt = 0
    while attempt <= max_rework:
        pp._raise_if_cancelled(on_progress)
        if on_progress:
            on_progress('replica_stage', {
                'stage': 'mutate_beats',
                'message': '正在沿变异轴改写节拍…' if attempt == 0 else '变体校验未过，正在回炉…',
            })
        prompt = user
        if violations:
            prompt = user + (
                '\n==================== VALIDATION FAILURES (FIX THESE) ====================\n'
                + '\n'.join(f'- {v["message"]}' for v in violations) + '\n'
            )
        raw = pp._chat(config, _MUTATE_SYSTEM, prompt, temperature=0.6,
                       max_tokens=32768, timeout=240)
        try:
            data = parse_json_reply(raw)
        except ValueError as e:
            # 同 cluster_beats：解析失败与校验未过是两笔预算。
            parse_budget -= 1
            if parse_budget < 0:
                raise
            if on_progress:
                on_progress('replica_stage', {
                    'stage': 'mutate_beats',
                    'message': f'模型回复不是合法 JSON，重新要一次（剩余 {parse_budget + 1} 次）…',
                })
            continue
        if not isinstance(data, dict):
            raise ValueError(f'二创回复应当是一个 JSON 对象，实际是 {type(data).__name__}')
        variant = _merge_variant(beats_doc, data, axes)
        violations = [v for v in _validate_construction_order(variant['beats'])]
        violations.extend(_validate_time_axis(
            variant['beats'],
            variant.get('video_duration_sec'),
            speed_multiplier=variant.get('speed_multiplier') or beats_doc.get('speed_multiplier')
        ))
        violations.extend(_validate_space_grouping(beats_doc, variant))
        if not [v for v in violations if v['level'] == 'error']:
            break
        attempt += 1

    variant['validation'] = violations
    return variant


def _merge_variant(beats_doc, data, axes):
    """把模型改写的内容并回原骨架。时间骨架以原文档为准，模型改不动它。"""
    rewritten = {b.get('id'): b for b in (data.get('beats') or []) if isinstance(b, dict)}
    out_beats = []
    for src in (beats_doc.get('beats') or []):
        new = dict(src)
        patch = rewritten.get(src.get('id')) or {}
        for key in ('visual_subject', 'visible_details', 'visible_action', 'visible_result',
                    'state_before', 'state_after', 'persistent_traces', 'operation', 'space'):
            if patch.get(key):
                new[key] = patch[key]
                # 中文对照是上一版英文的译文，改了英文就必须作废对应那条——留着它，
                # 卡点上会出现「中文写着旧载体、英文已经换成新载体」的错位。
                if isinstance(new.get('zh'), dict):
                    new['zh'] = {k: v for k, v in new['zh'].items() if k != key}
        # 证据帧降级为构图参考：变体不再对原片的事实负责，那些帧只提供机位与构图。
        new['reference_frames'] = list(src.get('evidence_frames') or [])
        new.pop('evidence_frames', None)
        out_beats.append(new)

    return {
        'video_duration_sec': beats_doc.get('video_duration_sec'),
        'source_video': beats_doc.get('source_video'),
        'variant_of': beats_doc.get('pipeline_id') or beats_doc.get('variant_of') or 'source',
        'mutation_axes': axes,
        # 场景恒常特征与场景签名都**不继承**，理由和 banned_elements 一样：它们描述的是
        # 原片那个地方——青苔、混凝土污渍、那盏三脚架灯。换了载体还带着它们，写手会把
        # 废弃巴士写成长着青苔的混凝土掩体。统计那份没有源事实可算（变体 job 目录下没有
        # frame_facts.json），签名则由模型按新载体重写。
        'scene_signature': str(data.get('scene_signature') or '').strip(),
        # banned 按新载体重算，绝不继承旧列表——旧载体的「不存在物」对新载体毫无意义。
        'banned_elements': [str(x).strip() for x in (data.get('banned_elements') or []) if str(x).strip()],
        'beats': out_beats,
    }


# ── AI 定向修复节拍硬伤 ───────────────────────────────────────────────────────

_AUTOFIX_SYSTEM = """You fix validation errors and construction order violations in a renovation/restoration time-lapse beat ladder (timelapse_beats.json).

You are given:
- CURRENT BEAT LADDER: list of beats with timestamps, stages, spaces, operations, actions, traces.
- VALIDATION FAILURES: the exact mechanical validation errors that MUST be fixed (e.g. stage regressions, rough_in after covering/enclosure, package operations out of range, missing fields).

THE 9 STANDARDIZED STAGES (IN STRICT CHRONOLOGICAL SEQUENCE):
1. demolition: tearing out, clearing, digging, hauling away waste debris.
2. structural: foundations, framing, load-bearing walls, subfloors, roof structure, structural opening cuts.
3. rough_in: MEP services that get covered up — electrical wiring, plumbing pipes, conduits, ductwork, insulation.
4. enclosure: closing cavities — wall studs, insulation batts/foam, wallboards, drywall, ceiling boards, timber cladding.
5. surface: finish layers — joint taping, sanding, primer, paint, plaster, tiles, trim, wet trades.
6. floor: floor finishing — subfloor, joists, teak/wood planking, tiles, floor coverings.
7. fixtures: powered/plumbed equipment installed — light fittings, sockets, switches, sconces, taps, built-in cabinets/furniture.
8. furnishing: loose furniture, mattresses, textiles, decor staging, tea table.
9. reveal: final walkthrough / hero shot showcasing completed space.

CRITICAL RULES:
- CHRONOLOGICAL STAGE MONOTONICITY:
  - Stages across the beat ladder must follow the construction sequence without invalid regressions (demolition -> structural -> rough_in -> enclosure -> surface -> floor -> fixtures -> furnishing -> reveal).
  - NEVER mark a beat as rough_in if it occurs AFTER enclosure, surface, or floor. If it installs lights, switches, or outlets on finished walls, change stage to fixtures.
  - NEVER mark a beat as structural if it occurs during interior finish/carpentry after surfaces or flooring (reclassify to surface, fixtures, or enclosure as appropriate).
  - Beats occurring after or at the very end of reveal must be furnishing or reveal (e.g. hero shots, lighting ambiance, final staging).
  - If a prior beat was misclassified (e.g. an early framing beat was mislabelled enclosure before rough_in, or rough_in was mislabelled surface), reclassify the stage to the logically accurate one.
- PRESERVE INTEGRITY:
  - Keep all beat `id`s intact in their existing order (B01, B02, ...).
  - Keep timestamps (`start`, `end`), `space`, and frame bindings (`evidence_frames` / `reference_frames`) unchanged.
  - Fix `package_operations` if out of range: ensure each beat has 1 to 3 concise, tightly coupled operations.
  - Fix `persistent_traces`: ensure at least 2 visible physical marks left on surfaces.
  - Keep descriptions faithful to the work shown, only adjusting wording where necessary to match the corrected stage or fix vague states.

OUTPUT:
Return ONE JSON object, no commentary, no code fences:
{
  "beats": [
    {
      "id": "B01",
      "stage": "<one of the 9 stages>",
      "operation": "...",
      "package_operations": ["..."],
      "visual_subject": "...",
      "visible_details": ["..."],
      "visible_action": "...",
      "visible_result": "...",
      "state_before": "...",
      "state_after": "...",
      "persistent_traces": ["..."]
    }
  ]
}"""


def _merge_fixed_beats(beats_doc, data):
    """把 AI 修复后的字段并回原 beats_doc，保护 timestamps、evidence_frames 等不被抹除。"""
    raw_list = data.get('beats') if isinstance(data, dict) else data
    if not isinstance(raw_list, list):
        return beats_doc

    by_id = {}
    for idx, b in enumerate(raw_list):
        if isinstance(b, dict):
            bid = b.get('id')
            if bid:
                by_id[bid] = b
            by_id[idx] = b

    out_beats = []
    for idx, src in enumerate(beats_doc.get('beats') or []):
        new = dict(src)
        patch = by_id.get(src.get('id')) or by_id.get(idx) or {}
        for key in ('stage', 'operation', 'package_operations', 'persistent_traces',
                    'visual_subject', 'visible_details', 'visible_action', 'visible_result',
                    'state_before', 'state_after', 'space', 'workers_present', 'source_event_ids'):
            if key in patch and patch[key] not in (None, '', []):
                new[key] = patch[key]
                if isinstance(new.get('zh'), dict):
                    new['zh'] = {k: v for k, v in new['zh'].items() if k != key}
        out_beats.append(new)

    beats_doc['beats'] = out_beats
    beats_doc.setdefault('banned_elements', [])
    beats_doc.setdefault('scene_signature', '')
    if isinstance(data, dict):
        if 'scene_signature' in data and data['scene_signature']:
            beats_doc['scene_signature'] = str(data['scene_signature']).strip()
        if 'banned_elements' in data and isinstance(data['banned_elements'], list):
            beats_doc['banned_elements'] = [str(x).strip() for x in data['banned_elements'] if str(x).strip()]
    return beats_doc


def autofix_beats(config, beats_doc, overview=None, on_progress=None, max_rework=2):
    """AI 定向修复节拍阶梯中的硬伤与违规项。

    针对校验器指出的 stage_regression, rough_in_after_enclosure, package_operations,
    event_double_bound 等违规，机械层优先自动调解事件覆盖冲突，剩余逻辑回喂给 LLM 做定向修正，
    修复后重新执行机械校验。返回 (fixed_beats_doc, fixed_errors_count)。
    """
    overview = overview or {}
    
    raw_violations = validate_beats(beats_doc, overview)
    raw_errors = [v for v in raw_violations if v.get('level') == 'error']
    if not raw_errors:
        return beats_doc, 0

    # 1. 机械层确定性预修复：解决 event_double_bound (多拍认领) 与 event_unbound (漏认领)
    reconcile_event_coverage(beats_doc, overview)
    
    initial_violations = validate_beats(beats_doc, overview)
    initial_errors = [v for v in initial_violations if v.get('level') == 'error']
    if not initial_errors:
        beats_doc['validation'] = initial_violations
        return beats_doc, len(raw_errors)

    source_beats = beats_doc.get('beats') or []
    skeleton = json.dumps([
        {k: b.get(k) for k in ('id', 'start', 'end', 'space', 'stage', 'operation',
                               'package_operations', 'visual_subject', 'visible_details',
                               'visible_action', 'visible_result', 'state_before',
                               'state_after', 'persistent_traces', 'workers_present', 'source_event_ids')
         if b.get(k) is not None}
        for b in source_beats
    ], ensure_ascii=False, indent=2)

    parse_budget = _PARSE_RETRY_BUDGET
    violations = list(initial_violations)
    attempt = 0

    while attempt <= max_rework:
        pp._raise_if_cancelled(on_progress)
        err_lines = [f"- [{v.get('level', 'error').upper()}] ({v.get('code')}) "
                     f"{v.get('beat_id') or 'GLOBAL'}: {v.get('message')}" for v in violations]
        user_prompt = (
            f"==================== CURRENT BEAT LADDER ====================\n{skeleton}\n\n"
            f"==================== VALIDATION FAILURES (FIX THESE) ====================\n"
            + "\n".join(err_lines) + "\n\n"
            "Please fix the errors above by correcting the stage classification, package_operations, "
            "persistent_traces, source_event_ids, or descriptions. Preserve the beat IDs and real construction observations."
        )

        if on_progress:
            on_progress('replica_stage', {
                'stage': 'review_beats',
                'message': ('AI 正在分析硬伤并定向修复节拍阶梯…' if attempt == 0
                            else f'正在进行第 {attempt + 1} 轮定向修复…'),
            })

        raw = pp._chat(config, _AUTOFIX_SYSTEM, user_prompt, temperature=0.1,
                       max_tokens=32768, timeout=240)
        try:
            data = parse_json_reply(raw)
        except ValueError as e:
            parse_budget -= 1
            if parse_budget < 0:
                raise
            if on_progress:
                on_progress('replica_stage', {
                    'stage': 'review_beats',
                    'message': f'模型回复不是合法 JSON，正在重试（剩余 {parse_budget + 1} 次）…',
                })
            continue

        if not isinstance(data, (dict, list)):
            raise ValueError(f'AI 修复回复应当是一个 JSON 对象或数组，实际是 {type(data).__name__}')

        _merge_fixed_beats(beats_doc, data)
        normalize_beat_keys(beats_doc)
        _renumber_beats(beats_doc)
        normalize_beat_spaces(beats_doc)
        reconcile_event_coverage(beats_doc, overview)
        attach_coverage_frames(beats_doc, overview)

        violations = validate_beats(beats_doc, overview)
        remaining_errors = [v for v in violations if v.get('level') == 'error']
        if not remaining_errors:
            break
        attempt += 1

    beats_doc['validation'] = violations
    remaining_errors = [v for v in violations if v.get('level') == 'error']
    fixed_count = max(0, len(initial_errors) - len(remaining_errors))
    return beats_doc, max(1, fixed_count)


def autobalance_beats(beats_doc, overview=None, max_duration=6.0, min_duration=2.0, speed_multiplier=2.0):
    """根据秒数与 2x 倍速模型自动拆解超长拍（>6.0s）并合并微拍（<2.0s）。"""
    overview = overview or {}
    source_beats = [dict(b) for b in (beats_doc.get('beats') or []) if isinstance(b, dict)]
    if not source_beats:
        return beats_doc, 0

    speed = float(beats_doc.get('speed_multiplier') or speed_multiplier or 2.0)
    beats_doc['speed_multiplier'] = speed

    changed_count = 0
    current_beats = source_beats

    for _ in range(3):
        # 1. 拆解超长拍 (> max_duration)
        split_beats = []
        split_occurred = False
        for b in current_beats:
            start = _num(b.get('start'))
            end = _num(b.get('end'))
            span = round(end - start, 2)
            if span > max_duration:
                num_parts = max(2, int(math.ceil(span / max_duration)))
                part_duration = round(span / num_parts, 1)
                cur_start = start
                pkg = list(b.get('package_operations') or [])

                for part_idx in range(num_parts):
                    cur_end = end if part_idx == num_parts - 1 else round(cur_start + part_duration, 1)
                    sub_b = dict(b)
                    sub_b['start'] = cur_start
                    sub_b['end'] = cur_end

                    if len(pkg) >= num_parts:
                        chunk_sz = max(1, len(pkg) // num_parts)
                        sub_pkg = pkg[part_idx * chunk_sz:(part_idx + 1) * chunk_sz] or [pkg[-1]]
                        sub_b['package_operations'] = sub_pkg

                    if b.get('stage') == 'reveal' or b.get('operation') in ('reward', 'reveal'):
                        if part_idx == 0:
                            sub_b['operation'] = '软装点缀与设备点亮 (Phase 1)'
                            sub_b['stage'] = 'furnishing'
                        else:
                            sub_b['operation'] = '终极完工全景赏析 (Phase 2)'
                            sub_b['stage'] = 'reveal'
                    else:
                        op_name = b.get('operation') or '施工推进'
                        sub_b['operation'] = f'{op_name} ({part_idx + 1}/{num_parts})'

                    split_beats.append(sub_b)
                    cur_start = cur_end
                changed_count += (num_parts - 1)
                split_occurred = True
            else:
                split_beats.append(b)

        current_beats = split_beats

        # 2. 合并过短微拍 (< min_duration)（严禁跨空间合并，严禁将过门/运镜拍与施工拍合并）
        merged_beats = []
        i = 0
        merge_occurred = False
        while i < len(current_beats):
            curr = dict(current_beats[i])
            c_start = _num(curr.get('start'))
            c_end = _num(curr.get('end'))
            c_span = round(c_end - c_start, 2)
            c_is_trans = (
                curr.get('stage') in ('transition', 'threshold', 'reveal')
                or str(curr.get('operation') or '').lower() in ('threshold', 'reward', 'reframe')
                or bool(curr.get('bridge_stage'))
                or bool(curr.get('hard_cut'))
            )
            if c_span < min_duration and len(current_beats) > 1 and not c_is_trans:
                if merged_beats:
                    prev = merged_beats[-1]
                    p_is_trans = (
                        prev.get('stage') in ('transition', 'threshold', 'reveal')
                        or str(prev.get('operation') or '').lower() in ('threshold', 'reward', 'reframe')
                        or bool(prev.get('bridge_stage'))
                        or bool(prev.get('hard_cut'))
                    )
                    if prev.get('space') == curr.get('space') and not p_is_trans:
                        prev['end'] = c_end
                        p_pkg = list(prev.get('package_operations') or [])
                        c_pkg = list(curr.get('package_operations') or [])
                        prev['package_operations'] = list(dict.fromkeys(p_pkg + c_pkg))[:3]
                        changed_count += 1
                        merge_occurred = True
                        i += 1
                        continue
                if i + 1 < len(current_beats):
                    nxt = dict(current_beats[i + 1])
                    n_is_trans = (
                        nxt.get('stage') in ('transition', 'threshold', 'reveal')
                        or str(nxt.get('operation') or '').lower() in ('threshold', 'reward', 'reframe')
                        or bool(nxt.get('bridge_stage'))
                        or bool(nxt.get('hard_cut'))
                    )
                    if nxt.get('space') == curr.get('space') and not n_is_trans:
                        nxt['start'] = c_start
                        n_pkg = list(nxt.get('package_operations') or [])
                        c_pkg = list(curr.get('package_operations') or [])
                        nxt['package_operations'] = list(dict.fromkeys(c_pkg + n_pkg))[:3]
                        current_beats[i + 1] = nxt
                        changed_count += 1
                        merge_occurred = True
                        i += 1
                        continue
            merged_beats.append(curr)
            i += 1

        current_beats = merged_beats
        if not split_occurred and not merge_occurred:
            break

    beats_doc['beats'] = current_beats
    normalize_beat_keys(beats_doc)
    _renumber_beats(beats_doc)
    normalize_beat_spaces(beats_doc)
    reconcile_event_coverage(beats_doc, overview)
    attach_coverage_frames(beats_doc, overview)

    try:
        from .duration_engine import calculate_beat_word_quota
        for b in beats_doc.get('beats') or []:
            start, end = _num(b.get('start')), _num(b.get('end'))
            if end > start:
                s_dur = round(end - start, 1)
                b['screen_duration_sec'] = s_dur
                b['action_duration_sec'] = round(s_dur * speed, 1)
                b['speed_factor'] = speed
                b['voiceover_quota'] = calculate_beat_word_quota(s_dur, lang='zh')
    except Exception:
        pass

    violations = validate_beats(beats_doc, overview)
    beats_doc['validation'] = violations
    return beats_doc, changed_count


# ── 交给合成器 ───────────────────────────────────────────────────────────────

def beats_to_dimensions(beats_doc, base_dimensions=None):
    """把 beats 转成 compose_anchor_and_packet 吃的 dimensions（Tier 3 绑定简报）。

    关键在 `beat_outline`：compose_anchor_and_packet 见到非空清单就切进「清单一比一
    还原」——拍数锁死为清单长度，不再走自适应密度公式去猜。这正是 1:1 复刻要的语义，
    所以这里不需要另造一条绑定通路。
    """
    beats = beats_doc.get('beats') or []
    dimensions = dict(base_dimensions or {})

    outline = []
    for beat in beats:
        text = f'{beat.get("visible_action") or ""} → {beat.get("visible_result") or ""}'.strip(' →')
        # 把耦合工序写进条目正文。合成器会照这份清单重新规划 ladder 并自己填
        # package_operations——不给它工序，它只能从一句动作描述里猜，猜出一道工序的
        # 拍就会被自己的硬闸判死。
        package = [str(p).strip() for p in (beat.get('package_operations') or []) if str(p).strip()]
        if package:
            text = f'{text}（工序：{"、".join(package)}）'
        entry = {'text': text or (beat.get('visual_subject') or ''),
                 'op': beat.get('operation') or beat.get('stage')}
        # 观察到的机位所在空间。合成期据此确定过门发生在第几拍、发生几次
        # （见 __init__.apply_observed_space_sequence）——不透传这一个键，
        # 复刻单的过门次数就仍由叙事骨架写死，与原片无关。
        space = normalize_space_label(beat.get('space'))
        if space:
            entry['space'] = space
        # 结构化再发一遍。正文里那段「（工序：…）」是给规划模型读的，合成器的确定性
        # 通路（compile_outline_fallback_ladder / backfill_package_operations）读的是
        # 这个键——2026-08-11 的整单失败就是它缺席造成的：兜底梯子最后一拍拿不到本拍
        # 真实工序，只继承到一个单元素的占位，随即被合成器自己的硬闸判死。
        if package:
            entry['package_operations'] = package

        # 富字段绑定（2026-08-10）。此前这里只发 text + op，于是人工卡点上逐拍精修的
        # state_before / state_after / persistent_traces / visible_details 全部止步于
        # 本函数——用户核对的东西根本没进过模型上下文，"1:1 复刻"实际只锁住了拍数。
        # 这几个键走 build_outline_plan_block 既有的富字段通路（MATERIALS / LEAVES /
        # STATE），不新造第二条绑定链路。
        details = [str(x).strip() for x in (beat.get('visible_details') or []) if str(x).strip()]
        if details:
            entry['mat'] = details
        macro_env = [str(x).strip() for x in ((beat.get('macro_environment') if isinstance(beat.get('macro_environment'), (list, tuple)) else [beat.get('macro_environment')]) if beat.get('macro_environment') else []) if str(x).strip()]
        if macro_env:
            entry['macro_environment'] = macro_env
        traces = [str(x).strip() for x in (beat.get('persistent_traces') or []) if str(x).strip()]
        if traces:
            # trace 在契约里是单串（zone/scope/trace 同组），列表要先合并。
            entry['trace'] = '; '.join(traces)
        for key in ('state_before', 'state_after'):
            value = str(beat.get(key) or '').strip()
            if value:
                entry[key] = value

        # 中文简介与结构化对照透传（2026-08-16）：卡点上核对过的 zh 字段（包含 operation/headline/
        # visible_action/visible_result 等）必须透传给合成器。此前这里遗漏了 zh，导致合成出来的
        # prompt_block 标题只有「图片 1:」而没有「图片 1（中文简介）:」，前端折叠栏缺失简介。
        zh = beat.get('zh')
        if isinstance(zh, dict) and zh:
            entry['zh'] = dict(zh)
            summary = zh.get('operation') or zh.get('headline') or zh.get('visible_result') or zh.get('visible_action')
            if isinstance(summary, (list, tuple)):
                summary = '、'.join(str(s).strip() for s in summary if str(s).strip())
            if summary:
                entry['summary'] = str(summary).strip()
                entry['operation_zh'] = str(summary).strip()
        elif beat.get('operation_zh') or beat.get('headline_zh') or beat.get('summary'):
            summary = beat.get('operation_zh') or beat.get('headline_zh') or beat.get('summary')
            entry['summary'] = str(summary).strip()
            entry['operation_zh'] = str(summary).strip()

        outline.append(entry)
    dimensions['beat_outline'] = outline

    if not dimensions.get('theme'):
        # 起点主体 → 终点主体，各截短。原来直接拼 state_before / state_after 两整句，
        # 反推场景下那是两段完整英文描述，拼出来两百多字：合成器把它整段当标题前缀，
        # 项目列表里就是一行认不出是哪一单的乱码。
        first, last = (beats[0] if beats else {}), (beats[-1] if beats else {})
        start = _short_subject(first.get('visual_subject') or first.get('state_before'), '改造对象')
        end = _short_subject(last.get('visible_result') or last.get('state_after'), '成品')
        dimensions['theme'] = f'{start} 改造为 {end}'

    # banned_elements 走 P0 门禁：任一 banned 元素出现在提示词里 = 交付前必须重写。
    dimensions['banned_elements'] = list(beats_doc.get('banned_elements') or [])
    # 场景恒常特征：与 banned 对称的另一半——一个说「永远没有」，一个说「一直都在」。
    # 只有它进了提示词，复刻出来的才是这条片子的质感，而不是同一道工序的通用想象。
    constants = beats_doc.get('scene_constants') or {}
    if constants:
        dimensions['scene_constants'] = {k: list(v) for k, v in constants.items() if v}
    signature = str(beats_doc.get('scene_signature') or '').strip()
    if signature:
        dimensions['scene_signature'] = signature
    dimensions['reverse_engineered'] = True
    # 逐拍空间序列 + 过门拍号。清单条目上已经带了 space，这里再给一份整体视图：
    # 合成期的确定性收口（apply_observed_space_sequence）读它，人工审阅也看得见
    # 「这条片子进了几次门、在第几拍」。
    _spaces = space_sequence(beats_doc)
    if _spaces:
        dimensions['space_sequence'] = _spaces
        dimensions['space_crossings'] = space_crossings(_spaces)
    dimensions['mutation_axes'] = list(beats_doc.get('mutation_axes') or [])
    return dimensions


def _short_subject(text, fallback, limit=60):
    """取第一个分句并截短，供自动主题使用。"""
    raw = ' '.join(str(text or '').split())
    if not raw:
        return fallback
    raw = re.split(r'[.;,，。；]', raw)[0].strip() or raw
    return raw if len(raw) <= limit else raw[:limit].rstrip() + '…'


def banned_element_hits(prompt_block, banned_elements):
    """P0 门禁用：提示词整块里命中的 banned 元素。命中即交付前必须重写。"""
    text = (prompt_block or '').lower()
    hits = []
    for item in (banned_elements or []):
        needle = str(item).strip().lower()
        if needle and needle in text:
            hits.append(item)
    return hits
