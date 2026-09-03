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

import copy
import json
import math
import os
import re
import sys
import tempfile
import time

import prompt_pipeline as pp


# Pass A 的提示词版本号。改了 _PASS_A_SYSTEM 就必须改它，否则旧缓存会被当成新结果复用。
# v4（2026-08-24）：新增 cast_appearance —— 画面里活物的外形（人种/肤色/发型/
# 帽子/上下装颜色款式/鞋），供全局识别项 scene_constants.cast 统计。
# v5（2026-08-25）：新增 camera_angle / camera_bearing —— 逐帧的拍摄角度读数（俯仰 +
# 方位），Pass B 的逐拍角度分析据此有据可依，而不是凭工序想象机位。
# v6（2026-08-25）：新增 lens_feel（焦段感）与 subject_placement（主体在画面里的位置与
# 占比）——锚点系统的 grid / z_depth_scale 此前从没在原片上量过，全是 packet 那次调用
# 按主题编的。
# v7（2026-08-25）：新增逐帧 shot_scale（远/全/中/近/特）。此前景别只有**逐拍**一个读数，
# 而原片一拍是由几个镜头组成的——多镜头交付线的镜头梯因此只能拿一个景别去排三到四镜，
# 中间那两个插入镜的景别全是写死的。逐帧有了读数，才能按 observed_cuts 切出的每一个镜头
# 各自投一次票（attach_shot_scales），镜头梯才真的是照着原片的景别序列排的。
# v8（2026-08-27）：强化 cast_appearance 与 cast_identity —— 显式提取服装新旧/破损度（ragged/weathered/dusty/worn）
# 与人物初始神情/情绪状态（haggard/distressed/sorrowful/tearful），彻底避免破旧场景被脑补为精致新衣。
PASS_A_PROMPT_VERSION = 'v8'

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
- cast_appearance: WHO/WHAT IS ALIVE IN THIS FRAME AND WHAT THEY LOOK LIKE. One item per living thing visible — a person, a miniature figurine, or an animal. Describe the BODY, CLOTHING WEAR/CONDITION, AND FACIAL/EMOTIONAL DEMEANOR: apparent ethnicity / skin tone, build and apparent age band, hair (length, colour, tied or loose), facial hair, headwear, upper garment (type + colour + wear condition e.g. ragged, tattered, weathered, dusty, or clean), lower garment (type + colour + wear condition), footwear, gloves, always-worn accessory, and facial expression / emotional demeanor (e.g. haggard/distressed/sorrowful/tearful, cheerful/smiling, neutral). Example: "light-brown-skinned South Asian man, slim, short black hair, no beard, ragged faded red long-sleeve tee with dirt smudges, dusty dark blue jeans, brown leather boots, sorrowful and haggard expression"; "1:24 African male figurine: faded dusty blue short-sleeve shirt, worn brown trousers, distressed haggard expression"; "brown short-haired dog, medium build".
  - Ethnicity, skin tone, clothing wear, and facial demeanor are VISIBLE physical facts here and must be recorded like any other: they prevent clean new clothing or mismatched expressions from being hallucinated downstream. Read them off the pixels; if the face is turned away, obscured, or too small to read, write only what you can see ("person in a red tee, face not visible") and lower the confidence — never guess, and never omit the whole item because one attribute is unreadable.
  - If nobody and no animal is in the frame, return an empty list.
- camera_angle / camera_bearing: WHERE THE CAMERA IS, read off this frame's own geometry — not off what is being built.
  - camera_angle (vertical) is one of: bird_eye (straight or near-straight down, ground plane fills the frame), high_angle (above the subject looking down, top surfaces visible, horizon high or out of frame), eye_level (lens at standing height, frame reads level, horizon near the middle), low_angle (below the subject looking up, undersides visible, horizon low), worm_eye (lens on or near the ground looking sharply up), dutch_angle (the whole frame is rolled off horizontal — the horizon itself is tilted).
  - camera_bearing (horizontal) is one of: front, three_quarter (a front and a side face both visible), side (the flank alone), rear_three_quarter, back — which face of the subject the lens is looking at.
  - Read them from converging lines, which faces of objects are visible, and where the horizon sits. Write the exact token and nothing else. If the frame is too tight, too dark, or too featureless to tell, leave that one empty and lower the confidence — a guessed angle is worse than a blank one, because everything downstream will reproduce it.
- shot_scale: HOW MUCH OF THE SUBJECT THIS FRAME HOLDS — one of extreme_wide (the whole site/landscape, the subject small in it), wide (the whole subject and its immediate surroundings), medium (part of the subject, roughly a person from the waist up), close (one component, joint, or a face filling most of the frame), extreme_close (a detail smaller than a hand — a screw head, a bead of sealant, a blade edge). Read it off how much of the FRAME the subject occupies, nothing else. This is NOT lens_feel: a wide lens standing close gives a close shot. Every frame gets one — if the frame is unreadable, leave it empty and lower the confidence.
- lens_feel: how WIDE the lens is, read off the perspective itself — one of ultra_wide (strong barrel curvature at the edges, near objects looming, edges stretched), wide (noticeably expansive but not distorted), normal (perspective looks like unaided vision, no compression, no stretch), tele (background compressed and flattened onto the subject, shallow depth), macro (a subject smaller than a hand filling the frame at close focus). This is NOT the same reading as how much of the scene is in shot: a wide lens standing far back and a long lens standing close both give you "the whole wall". Leave it empty if the frame gives you nothing to judge by.
- subject_placement: WHERE THE MAIN SUBJECT SITS IN THIS FRAME AND HOW BIG IT IS. One short sentence carrying three things: its horizontal position (left / centre / right, in thirds), its vertical position, and how much of the frame HEIGHT it occupies, plus where the horizon sits if one is visible. Example: "the shell sits centred, filling about three fifths of frame height, horizon across the upper third"; "the trench runs along the lower left, taking the bottom quarter of the frame". Write fractions in words, never digits or percent signs. This is the one reading that says what the reference film's composition actually is — everything downstream currently guesses it.
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
  "camera_angle": "<bird_eye|high_angle|eye_level|low_angle|worm_eye|dutch_angle, or empty if unreadable>",
  "camera_bearing": "<front|three_quarter|side|rear_three_quarter|back, or empty if unreadable>",
  "shot_scale": "<extreme_wide|wide|medium|close|extreme_close, or empty if unreadable>",
  "lens_feel": "<ultra_wide|wide|normal|tele|macro, or empty if unreadable>",
  "subject_placement": "<one short sentence: the main subject's horizontal and vertical position and what fraction of frame height it fills, plus where the horizon sits; fractions in words>",
  "workers_present": true|false,
  "cast_appearance": ["<one per living thing in frame: ethnicity/skin tone, build, hair, headwear, upper garment + colour + wear/condition, lower garment + colour + wear/condition, footwear, facial expression/emotional demeanor; empty list if nobody is in frame>"],
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
            or cfg.get('model') or 'gemini-3.8-flash-high')


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
    """鲁棒的 JSON 修复引擎，专门应对大模型长输出中的语法瑕疵：
    1. 键值对/数组元素之间漏掉逗号（例如 "key1": "val1"\\n "key2": "val2" 或 "k": 123\\n "k2": ...）
    2. 幻觉多余/闭合错位的括号（例如标量字段后多写了 `],` 或多余的 `}`/`]`）
    3. 字符串内部未转义的双引号（智能向前探查区分闭合引号与内嵌引号）
    4. 字符串内部裸控制字符（\\n, \\r, \\t）转义
    5. 对象/数组末尾多余的尾逗号（trailing commas）
    6. Python 关键字（True, False, None）转为 JSON 标准值
    7. C/JS 风格及 Python 风格注释过滤
    8. 单引号字符串转双引号
    9. 自动闭合流末尾未闭合的括号
    """
    if not text:
        return text

    # 1. 过滤思考标签与 Markdown 代码块
    cleaned = text.strip()
    cleaned = re.sub(r'<think>.*?</think>', '', cleaned, flags=re.DOTALL).strip()
    if cleaned.startswith('```'):
        nl = cleaned.find('\n')
        cleaned = cleaned[nl + 1:] if nl != -1 else cleaned[3:]
    if cleaned.rstrip().endswith('```'):
        cleaned = cleaned[:cleaned.rstrip().rfind('```')]
    cleaned = cleaned.strip()

    # 2. 提取最外层 JSON 结构
    first_brace = cleaned.find('{')
    first_bracket = cleaned.find('[')
    if first_brace != -1 and (first_bracket == -1 or first_brace < first_bracket):
        start_idx = first_brace
        end_char = '}'
    elif first_bracket != -1:
        start_idx = first_bracket
        end_char = ']'
    else:
        start_idx = -1

    if start_idx != -1:
        last_end = cleaned.rfind(end_char)
        if last_end != -1 and last_end > start_idx:
            cleaned = cleaned[start_idx:last_end + 1]
        else:
            cleaned = cleaned[start_idx:]

    # 快速路径：已经是合法 JSON 则直接返回
    try:
        json.loads(cleaned)
        return cleaned
    except Exception:
        pass

    # 3. 状态机流式扫描与语法修复
    out = []
    stack = []  # 容器栈，记录 '{' 和 '['
    i = 0
    n = len(cleaned)
    in_str = False
    str_quote_char = '"'
    str_escape = False

    while i < n:
        ch = cleaned[i]

        # ── 字符串内部 ──
        if in_str:
            if str_escape:
                out.append(ch)
                str_escape = False
                i += 1
                continue
            if ch == '\\':
                out.append(ch)
                str_escape = True
                i += 1
                continue

            if ch == str_quote_char:
                # 探查此引号是合法结束引号还是未转义的内嵌引号
                j = i + 1
                while j < n and cleaned[j] in ' \t\r\n':
                    j += 1
                next_ch = cleaned[j] if j < n else ''

                is_closing = False
                if j >= n:
                    is_closing = True
                elif next_ch in ',:}]':
                    is_closing = True
                elif next_ch in '"\'':
                    # 漏逗号的连续字符串: "val1" "val2" 或 "val1" "key2":
                    is_closing = True
                elif next_ch.isalpha() or next_ch in '-0123456789':
                    # 检查本行后续是否还有引号和分隔符（若有，说明当前引号是 "he said "hello" today" 中的内嵌引号）
                    line_end = cleaned.find('\n', j)
                    if line_end == -1:
                        line_end = n
                    rest_of_line = cleaned[j:line_end]
                    if ('"' in rest_of_line or "'" in rest_of_line) and any(d in rest_of_line for d in (',', '}', ']', ':')):
                        is_closing = False
                    else:
                        is_closing = True
                elif next_ch in '{[':
                    is_closing = True

                if is_closing:
                    in_str = False
                    out.append('"')
                else:
                    out.append('\\"')
                i += 1
                continue

            if ch in '\n\r\t':
                ctrl_map = {'\n': '\\n', '\r': '\\r', '\t': '\\t'}
                out.append(ctrl_map[ch])
                i += 1
                continue

            out.append(ch)
            i += 1
            continue

        # ── 字符串外部 ──

        # 注释跳过
        if ch == '/' and i + 1 < n:
            if cleaned[i + 1] == '/':
                nl = cleaned.find('\n', i)
                i = nl if nl != -1 else n
                continue
            elif cleaned[i + 1] == '*':
                end_c = cleaned.find('*/', i + 2)
                i = end_c + 2 if end_c != -1 else n
                continue
        elif ch == '#':
            nl = cleaned.find('\n', i)
            i = nl if nl != -1 else n
            continue

        # 空白符
        if ch in ' \t\r\n':
            out.append(ch)
            i += 1
            continue

        # 引号开始新字符串
        if ch in ('"', "'"):
            # 补逗号检查：如果前一个有效字符是值终止符，自动补逗号
            k = len(out) - 1
            while k >= 0 and out[k] in ' \t\r\n':
                k -= 1
            if k >= 0:
                last_ch = out[k]
                if last_ch in '"}]' or (last_ch.isalnum() and last_ch not in ':,'):
                    if last_ch not in ':,' and last_ch not in '{[':
                        out.append(',')

            in_str = True
            str_quote_char = ch
            out.append('"')
            i += 1
            continue

        # 打开容器 '{'
        if ch == '{':
            k = len(out) - 1
            while k >= 0 and out[k] in ' \t\r\n':
                k -= 1
            if k >= 0 and (out[k] in '"}]' or out[k].isalnum()) and out[k] not in ':,{[':
                out.append(',')
            stack.append('{')
            out.append('{')
            i += 1
            continue

        # 打开容器 '['
        if ch == '[':
            k = len(out) - 1
            while k >= 0 and out[k] in ' \t\r\n':
                k -= 1
            if k >= 0 and (out[k] in '"}]' or out[k].isalnum()) and out[k] not in ':,{[':
                out.append(',')
            stack.append('[')
            out.append('[')
            i += 1
            continue

        # 闭合容器 '}'
        if ch == '}':
            if not stack:
                # 栈空时的孤立右大括号 -> 丢弃
                i += 1
                continue
            if stack[-1] == '{':
                k = len(out) - 1
                while k >= 0 and out[k] in ' \t\r\n':
                    k -= 1
                if k >= 0 and out[k] == ',':
                    out[k] = ' '
                stack.pop()
                out.append('}')
                i += 1
                continue
            elif stack[-1] == '[':
                if '{' in stack:
                    k = len(out) - 1
                    while k >= 0 and out[k] in ' \t\r\n':
                        k -= 1
                    if k >= 0 and out[k] == ',':
                        out[k] = ' '
                    stack.pop()
                    out.append(']')
                    continue
                else:
                    i += 1
                    continue

        # 闭合容器 ']'
        if ch == ']':
            if not stack:
                # 栈空时的孤立右中括号 -> 丢弃
                i += 1
                continue
            if stack[-1] == '[':
                k = len(out) - 1
                while k >= 0 and out[k] in ' \t\r\n':
                    k -= 1
                if k >= 0 and out[k] == ',':
                    out[k] = ' '
                stack.pop()
                out.append(']')
                i += 1
                continue
            elif stack[-1] == '{':
                # 在对象 `{}` 中意外遇到 `]`（例如大模型在字符串后多写了 `],`）
                j = i + 1
                while j < n and cleaned[j] in ' \t\r\n':
                    j += 1
                if j < n and cleaned[j] == ',':
                    i = j + 1
                else:
                    i += 1
                continue

        # 逗号
        if ch == ',':
            k = len(out) - 1
            while k >= 0 and out[k] in ' \t\r\n':
                k -= 1
            if k >= 0 and out[k] in ',:{[':
                # 重复或位置错误的逗号 -> 丢弃
                i += 1
                continue
            out.append(',')
            i += 1
            continue

        # Python 字面量
        if cleaned[i:i + 4] == 'True' and not (i + 4 < n and cleaned[i + 4].isalnum()):
            out.append('true')
            i += 4
            continue
        if cleaned[i:i + 5] == 'False' and not (i + 5 < n and cleaned[i + 5].isalnum()):
            out.append('false')
            i += 5
            continue
        if cleaned[i:i + 4] == 'None' and not (i + 4 < n and cleaned[i + 4].isalnum()):
            out.append('null')
            i += 4
            continue

        # 数字或布尔标识符开始前补逗号检查
        if (ch.isdigit() or ch in '-+') and (i == 0 or not cleaned[i - 1].isalnum()):
            k = len(out) - 1
            while k >= 0 and out[k] in ' \t\r\n':
                k -= 1
            if k >= 0 and (out[k] in '"}]' or out[k].isalnum()) and out[k] not in ':,{[':
                out.append(',')

        out.append(ch)
        i += 1

    # 清除尾逗号并自动闭合未匹配容器
    k = len(out) - 1
    while k >= 0 and out[k] in ' \t\r\n':
        k -= 1
    if k >= 0 and out[k] == ',':
        out[k] = ' '

    return ''.join(out)


class CraftRefineRolledBack(RuntimeError):
    """工艺精修产出了新的硬伤，整份已回滚。

    单独一个类型而不是普通 ValueError：调用方要能把「精修没跑成」和「精修跑了但把阶梯
    改坏了、已经退回原样」区分开——后者对用户是好消息（什么都没丢），措辞不该一样。
    """


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
    """模型回复 → Python 对象。先原样解析，失败再走多级鲁棒修复。

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
        # 活物外形。不过 canonicalize_entity_phrase：那份词典是给材质/器具用词漂移的，
        # 把人的衣着往材质词上归一只会毁掉这条读数。
        cast_appearance = [str(x).strip() for x in (item.get('cast_appearance') or []) if str(x).strip()]
        # 逐帧机位读数走和逐拍字段同一张近义词表：模型照样会写 "low angle shot"、
        # "from the side"，认不出来的一律归零——留一个歪值下去，等于让整条片子的
        # 角度分析建立在一句没人校对过的话上。
        camera_angle = _coerce_enum(item.get('camera_angle'), CAMERA_ANGLES,
                                    _CAMERA_ANGLE_SYNONYMS) or ''
        camera_bearing = _coerce_enum(item.get('camera_bearing'), CAMERA_BEARINGS,
                                      _CAMERA_BEARING_SYNONYMS) or ''
        lens_feel = _coerce_enum(item.get('lens_feel'), LENS_FEELS, _LENS_FEEL_SYNONYMS) or ''
        # 逐帧景别（v7）。走和逐拍 shot_scale 同一张近义词表：模型会写 "wide shot"、"CU"。
        shot_scale = _coerce_enum(item.get('shot_scale'), SHOT_SCALES,
                                  _SHOT_SCALE_SYNONYMS) or ''
        # 构图是自由文本（它要说位置、占比、地平线三件事，闭集装不下），只做去空白。
        subject_placement = ' '.join(str(item.get('subject_placement') or '').split())

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
            'cast_appearance': cast_appearance,
            'camera_angle': camera_angle,
            'camera_bearing': camera_bearing,
            'shot_scale': shot_scale,
            'lens_feel': lens_feel,
            'subject_placement': subject_placement,
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
    return cfg.get('model') or 'gemini-3.8-flash-high'


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

    # action 让前端把这一段挂到自己的进度区间上：它跟逐帧提取同属 review_frames，
    # 但发生在它之后——共用一个区间的话，"只增不减"的进度条会把它整段吃掉。
    if on_progress:
        on_progress('replica_stage', {
            'stage': 'review_frames',
            'action': 'peak_verify',
            'message': f'强模型复核 {len(peak_names)} 张事件峰值帧（{model}）',
            'done': 0,
            'total': len(peak_names),
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
    peak_done = {'n': 0}

    def _on_peak_done(_key, result):
        peak_done['n'] += len(result or {})
        if on_progress:
            on_progress('replica_stage', {
                'stage': 'review_frames',
                'action': 'peak_verify',
                'message': f'峰值帧复核 {peak_done["n"]}/{len(peak_names)}',
                'done': peak_done['n'],
                'total': len(peak_names),
            })

    if batches:
        results = pp._map_parallel(
            _run_peak_batch,
            [(i, b) for i, b in enumerate(batches)],
            pp.review_concurrency(config),
            on_done=_on_peak_done,
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
- macro_environment: ONLY declare for the opening anchor beat (B01) AND the first beat after entering a new enclosed space / threshold crossing (when space label changes or stage is transition). ONE to THREE items describing the macro terrain, geology, climate/lighting, and spatial metric envelope visible in this beat — e.g. "arid desert sandstone cliff with natural ambient sunlight", "loose reddish-tan desert sand ground with ripples". For intermediate beats within the same space, do NOT declare macro_environment (keep empty [] or omit) to avoid context clutter and reduce interference with trade actions.
  - This field is what the place LOOKS LIKE ON ITS OWN, before anyone worked on it. NEVER put a work product here — a trench that was dug, a wall that was built, a floor that was laid belong in state_before / state_after, never in macro_environment. Those one-to-three items are the film's only macro-environment budget; spending one on a result you already wrote in the state fields costs you a real environment fact.
- visible_details: THREE to FIVE items (each under ten words). Each names a material with its colour, texture or condition AND where it sits in frame — "yellow fibreglass batts in the left wall bays", not "insulation". These items are the only place the reference film's actual look survives into the prompt; a bare noun brings back a generic version of this work, not this film.
  - Never spend one of these slots restating something already written in macro_environment or persistent_traces. The budget is 3-5 lines for the whole beat; a line that repeats the autumn foliage you already declared as macro environment is a line that is not describing the subject.
  - Spend the slots on what makes THIS subject recognisable — for a vehicle: its glazing, window rubbers, front face, wheels, rust; for a room: its openings, its edges, its floor build-up. Not on the background.
- persistent_traces: EXACTLY TWO visible marks THIS beat leaves behind (each under ten words), each naming the mark AND the surface it sits on. A feature that was already there before this beat (fallen leaves, moss, old staining) is not a trace — it belongs in macro_environment or nowhere. Every item in this list is concatenated into one string downstream, so a pre-existing environment noun in here dilutes the marks that actually matter.
- evidence_frames: list EXACTLY THREE frames per beat (the Triad: 1. start/pre-state anchor, 2. peak work/tool action, 3. resulting completion). Never fewer than three when at least three frames exist in the window. Do not echo every frame in the window; a long frame list is the single biggest cause of a reply that gets cut off before it finishes.
- Keep the SENTENCE fields (visual_subject, visible_action, visible_result, state_before, state_after) under twenty words each. Length belongs in visible_details and persistent_traces, where it buys concrete look; in the sentence fields it only buys narration.
- visible_result and state_after have DIFFERENT jobs and must not be the same sentence written twice:
  - visible_result = what you SEE at the moment the action lands (the body settles, the sling goes slack, a clod breaks off the wall).
  - state_after = HOW FAR the work got — a completion extent, carrying a quantity: a fraction, a percentage, a flush/level relation, a height, a count of bays or metres. "roof flush with surrounding grade, whole body below grade" is an extent; "the bus is in the pit" is not.
  - The same discipline applies to state_before: give the starting extent a number or a spatial relation (how high it hangs, how much is already done), never a bare picture.
- operation is a CLEAN milestone-level operation token of one to three words ("seat bus", "board ceiling", "pour slab"). It drives a phase check downstream, so never write it as a full clause with objects and prepositions ("lowering the bus into the excavation pit"), and never make it a verbatim copy of one of the package_operations entries.
- Do not restate scene_signature in every beat. It is written once, at the top, and applies throughout.
- space: name the physical space the camera is filming FROM, as a short lowercase label ("wooded slope outside", "entrance tunnel", "main room", "sleeping alcove"). Reuse the SAME label verbatim for every beat shot in that space. Start a NEW label ONLY when the camera has physically moved into a different enclosed space through an opening — never for a reframe, a closer angle, or a pan inside one space, and never because the work changed. Every beat filmed outside the structure shares ONE outdoor label. This field is the only record of how many times the film walks through a doorway; a film that enters a corridor and later enters a room off it has THREE labels, and collapsing them into one deletes that second entry from the reproduction.
- Visual Grounding Gate (VGG) & Zero Residential Appliance Hallucination (mandatory):
  - NEVER invent or hallucinate residential appliances (kitchen cabinetry, ceramic sink, brass faucet, cooktop, oven, refrigerator, dining table, dining chairs, bathroom vanity) or extra functional rooms UNLESS they are explicitly observed and named in the FRAME FACTS / TIME WINDOWS.
  - In rustic/bushcraft/hobbit/shelter projects, if the space is a raw timber-framed foyer, bedroom, or alcove, describe ONLY the physical carpentry, membranes, insulation, and timber framing that actually exist in the footage. Do NOT default to "kitchen" or "dining room" just because it is an interior room.
- worker_attire: Extract and carry the authoritative attire from frame facts: e.g. "one lone craftsman in a casual grey work t-shirt, dark cargo pants, and backwards baseball cap (no neon safety vest, no plastic hardhat)". Never substitute a municipal safety vest or hardhat when the builder is wearing casual/craftsman workwear.
- cast_identity: ONE list for the WHOLE film (not per beat) — one item per living thing that recurs across the footage: each builder, each helper, each figurine, each animal. Each item is that individual's FIXED physical identity and clothing condition, head to toe: apparent ethnicity and skin tone, build and apparent age band, hair (length, colour, tied/loose), facial hair, headwear, upper garment with colour and wear condition (ragged, tattered, weathered, dusty, or clean), lower garment with colour and wear condition, footwear, gloves, always-worn accessory, and baseline/initial emotional demeanor. Example: "the resident couple: 1:24 miniature African male in distressed faded dusty blue shirt and worn brown trousers with sorrowful haggard expression; 1:24 miniature African female in weathered earth-toned patterned headwrap and wrap skirt with tearful distressed demeanor"; "the lone builder: light-brown-skinned South Asian man, early thirties, slim, short black hair, clean-shaven, faded red long-sleeve tee, dark blue jeans, brown leather boots, no hardhat and no hi-vis vest"; "the site dog: short-haired tan mongrel, medium build, red collar".
  - Lift it from the frame facts' cast_appearance field, which records this frame by frame. Where frames disagree on a detail, take the reading that most frames agree on — a single frame that read the shirt as orange does not license a new person.
  - This is IDENTITY, never ACTION: what they permanently look like and the physical state of their clothing, not transient micro-actions. The transient doing is each beat's own cast_action.
  - Ethnicity, skin tone, clothing wear/tear, and baseline facial demeanor belong in this list. They prevent clean new clothing or mismatched expressions from being hallucinated downstream. Record what the frames show; if faces are never readable, describe only what is visible and say so ("face never clearly visible") rather than inventing.
  - Order the list with the person who appears most first. If the footage genuinely never shows a living thing, return an empty list.
- PER-BEAT PRODUCTION FIELDS. Each of these is a separate channel downstream; do not smuggle them into the sentence fields and do not leave them out because "it is obvious from the action".
  - tool: the ONE geometric tool that delivers the action at its peak ("crawler crane on outriggers", "cordless impact driver", "rubber mallet"). This is the tool half of the Action-Tool-SFX triad. If machinery does the work, the machine IS the tool.
  - sfx: ONE to THREE physical sounds this beat actually makes, one sound source per item ("hydraulic crane whine", "soil clods sliding down the trench wall", "steel body thudding into the cut"). Match the audio_sfx spikes given to you in the CHANGE EVENTS where there are any. NEVER write music, score, mood beds, or narration — the delivery is 0% BGM, 100% physical sound.
  - shot_scale: one of extreme_wide | wide | medium | close | extreme_close — the framing this beat is filmed at.
  - camera_move: one of static | push_in | pull_out | pan | tilt | orbit | follow | handheld | crane — how the camera behaves during this beat. A transition beat crossing a threshold is normally push_in or follow. Use the exact token, never a phrase.
  - camera_angle: WHERE THE CAMERA IS RELATIVE TO THE SUBJECT, VERTICALLY. One of bird_eye | high_angle | eye_level | low_angle | worm_eye | dutch_angle. Read it off the frame's own geometry, not off the subject: which way the converging lines run, whether you can see the TOP faces of things (looking down) or their UNDERSIDES and the sky/ceiling behind them (looking up), and where the horizon sits in the frame.
    - bird_eye = straight or near-straight down, the ground plane fills the frame, the subject reads as a plan/footprint. high_angle = clearly above the subject looking down, top surfaces visible, horizon high in frame or out of it. eye_level = the lens sits at a standing person's height and the frame reads level, horizon near the middle. low_angle = clearly below the subject looking up, undersides visible, subject towering, horizon low in frame. worm_eye = lens on or near the ground, looking sharply up along the subject. dutch_angle = the whole frame is rolled off horizontal (the horizon itself is tilted) — use this ONLY for a genuinely canted frame, never for "the camera is off to one side".
  - camera_bearing: WHERE THE CAMERA IS RELATIVE TO THE SUBJECT, HORIZONTALLY. One of front | three_quarter | side | rear_three_quarter | back — which face of the subject the lens is looking at: its front, a front corner (both a front and a side face visible), its flank alone, a rear corner, or its back.
    - These two are INDEPENDENT axes and both must be declared: one beat can be low_angle AND side at the same time, and collapsing them into one reading throws away the half that makes this film look like itself — "worm's-eye from the flank" and "standing overhead from the front" are two completely different films of the same trade operation.
    - The FRAME FACTS you are given already carry both readings frame by frame, shown as `view=<angle>/<bearing>`. Lift the beat's pair from the frames inside that beat's own window — where they disagree, take the reading that most of that window's frames agree on. Use the exact tokens, never a phrase, and never guess to fill the slot: they must be readable from the frames you were given. Where the camera genuinely does not move between beats, repeat the SAME pair — a stable tripod film declaring five different angles is a misread, not variety.
  - lens_feel: how WIDE the lens is — one of ultra_wide | wide | normal | tele | macro. Read it off the perspective itself: edge curvature and looming near objects (ultra_wide), a compressed flattened background (tele), unaided-vision perspective (normal). The frame facts carry it per frame as `lens=`. This is NOT shot_scale: a wide lens standing far back and a long lens standing close both deliver "the whole wall", and they look nothing alike. Every beat filmed on the same camera in the same space normally repeats the SAME value — this is a production fact, not a per-beat choice.
  - subject_placement: WHERE THE MAIN SUBJECT SITS IN THE FRAME AND HOW BIG IT IS, in one short sentence: horizontal position in thirds, vertical position, what fraction of the frame HEIGHT it occupies, and where the horizon sits if visible ("the shell sits centred, filling about three fifths of frame height, horizon across the upper third"). The frame facts carry it per frame as `placement=`; take the reading that holds for most of this beat's window. Fractions in words, never digits or percent signs. Downstream this is the ONLY measurement of the reference film's composition — without it the anchors' screen positions and frame-height shares are invented from scratch.
  - time_treatment: how time runs in THIS beat — one of timelapse (work visibly racing, repeated cycles compressed, people moving unnaturally fast), real_time (a walk-through, a reveal, a single unhurried action at natural speed), slow_motion. Read it off how fast bodies and material move between the frames of this beat's own window, not off the film's overall speed_multiplier. The final reward/walk-through beat of these films is almost always real_time even though every construction beat around it is timelapse; declaring it timelapse is how a calm finished-home tour gets delivered as a frantic sped-up clip.
  - worker_count: how many people are visible in this beat, as an integer. 0 for a sterile beat, and it must agree with workers_present.
  - light_state: the light and time of day in this beat ("overcast midday, no cast shadows", "low golden side light from frame left"). The film spans days; a beat that does not declare its light gets a random one.
  - material_flow: where this beat's material went or came from ("excavated soil piled on the trench's north lip", "offcuts bundled and carried out through the doorway"). This is the Material & Spoil Balance rule's field — demolition must say where debris goes, installation must say what stock was consumed.
  - cast_action: what EVERY living thing in frame is DOING WITH ITS BODY this beat, apart from the work itself — structured as an ACTION-REACTION CAUSAL CHAIN: [Trigger Event in this beat] -> [Immediate Reflex / Bodily Reaction] -> [Engaged Tracking / Motion] -> [Settled Posture] ("As the giant hand descends from the canopy -> the two figurines tilt heads back looking up in awe -> as the shack is gripped -> swiftly stand up onto their feet and step back -> turn to face the cleared footprint as the blueprint is lowered"; "As the shovel strikes the dirt -> turns head to track the blade -> steps closer to inspect the deepening trench"). Living thing means a person, a miniature figurine, OR an animal (the site dog, a cat on the wall, a resin hen in the diorama) — cover each one that is in frame, not just the humans. NEVER write "remain", "stay", "unchanged", "in the same spot", "still standing where they were": that is a POSITION, not a move, and it is copied verbatim into every frame downstream, so the delivered film shows plastic dolls that never move once — the single most-reported failure of this pipeline. If a subject genuinely barely moved, write the smallest real change instead: a head turn, a shift of weight, an eye-tracking response. Never repeat visible_action here: that one is the operation, this one is the body's reaction to the operation. If nothing alive is in frame, leave it out.
  - material_specs: ONE to THREE engineering specs for the material this beat works with — nominal thickness, grade, section size, surface texture, sheen ("9mm OSB sheathing, raw matte face", "2x4 SPF studs (38x89mm)", "black polyethylene vapour barrier, red taped seams"). The FRAME FACTS you are given already carry a mat_specs field measured frame by frame; lift it from there. This is NOT visible_details: that one says what the material is, what colour it is and where it sits in frame; this one says how thick it is, what grade it is and what its face looks like. Copy nothing you cannot find in the frame facts — never fill in a plausible nominal size from trade habit.
  - tool_specifics: the specific type, drive mechanism and active bit/blade of the ONE tool named in "tool" ("18V cordless brushless impact driver with magnetic bit", "pneumatic framing nailer", "stainless steel notched trowel"). The frame facts carry this as tool_specs. "tool" says which tool; this says which KIND of that tool. Leave it out if the frames never show it clearly enough to say.
  - fastening_and_bonding: ONE to THREE visible fasteners or chemical bonds this beat uses ("countersunk black drywall screws", "expanding PU foam sealant along the gap", "construction adhesive bead", "staples"). The frame facts carry it as fasteners. This is what the beat's joints actually look like, and it is also what decides whether this beat's sfx is a driving whine, a nail crack, or a squeeze — keep the two consistent.
  - micro_traces: ONE to THREE FINE marks this beat leaves ("fine sawdust along the pencil cut-line", "chalk snap line on the subfloor", "paint overspray on the sill"). The frame facts carry it as micro_traces. This is NOT persistent_traces: that one is the two macro marks later beats must inherit; this one is the grain of detail that makes the beat read as real work, and nothing downstream demands it be carried forward. Never repeat a persistent_traces item here.
  - insert_subject: if the film CUTS to a closer framing inside this beat, name in a few words what that closer shot is ON ("the tweezer tip pressing a roof tile", "mortar squeezing out from under the block", "the two figurines watching from the moss"). Read it off the frames: a run of frames at a much closer framing inside one beat is that cut. Leave it out for a beat filmed as one uninterrupted shot — an invented insert is worse than none, because it will be reproduced verbatim.
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

- ambient_motion: ONE list for the WHOLE film (not per beat), naming what keeps moving in the frame the whole way through without anyone doing it: running water, drifting smoke, a flame, wind in the canopy, falling snow or rain, laundry swinging, dust motes, a flickering filament. These never appear in any beat — a beat records what CHANGED, and these change nothing, they just never stop. Leaving them out is what makes a delivered clip read as a photograph with one moving hand in it. Write only what you can actually see moving across frames; if the film's background genuinely holds still, return an empty list.
- ambient_sound, color_grade and cast_identity are written ONCE, at the top, exactly like scene_signature and ambient_motion — never restated inside a beat. Every beat downstream is rendered by an image model that has no memory of the previous frame; this list is the only thing that keeps one builder from becoming three different people over eleven shots.
- ambient_sound: ONE list for the WHOLE film (not per beat), naming the sound that is under EVERY shot without anyone making it: wind in the canopy, a stream, distant traffic, surf, rain on a roof, the hollow room tone of a bare concrete space, insects. It is the exact audio counterpart of ambient_motion — a beat's sfx records what this beat's work SOUNDS LIKE, and this records what the PLACE sounds like when nobody is working. Nothing downstream measures it, so leaving it out means every clip's ambient line is invented from scratch and the film's acoustic space changes shot to shot. Write only what the footage supports; empty list if the audio is genuinely dead or unreadable.
- color_grade: ONE sentence for the WHOLE film describing its LOOK as a photographic treatment, not as a mood: colour temperature bias, contrast, saturation, black level, and any film/digital character ("cool overcast neutral grade, gentle contrast, slightly lifted blacks, restrained saturation"; "warm golden bias with deep crushed shadows and punchy saturation"). This is the first thing a viewer registers about whether a copy looks like the original, and it is currently nowhere in this pipeline. Never write mood or genre words (cinematic, dramatic, moody, epic) — those are not gradings, and everything downstream will render its own idea of them.
- scene_signature: ONE sentence naming the venue and how it looks throughout — its structure, its materials, its weathering, its surroundings, its standing light. Write what is true in the first frame and still true in the last. No work, no progress, no beat content. This is the one line that says why the reference film looks like itself; a generic version of it ("an interior space under renovation") is worse than none.

OUTPUT
Return one JSON object, no prose, no code fences:
{
  "video_duration_sec": <number>,
  "scene_signature": "<one sentence, under thirty words>",
  "ambient_motion": ["<what keeps moving in frame all the way through, one per item; empty list if nothing does>"],
  "ambient_sound": ["<what is audible under every shot with nobody making it, one per item; empty list if none>"],
  "color_grade": "<one sentence: colour temperature bias, contrast, saturation, black level, film/digital character — never mood words>",
  "cast_identity": ["<one per recurring living thing: ethnicity/skin tone, build, age band, hair, facial hair, headwear, upper garment + colour, lower garment + colour, footwear; identity only, never action; empty list if nobody appears>"],
  "banned_elements": ["..."],
  "beats": [{
    "id": "B01",
    "start": <sec>, "end": <sec>,
    "stage": "<one of the ten>",
    "space": "<short label of the space this beat is filmed in, reused verbatim across beats>",
    "macro_environment": ["..."],
    "operation": "<one to three word milestone operation token>",
    "package_operations": ["<two or three tightly coupled operations sharing one terminal product; or [\"threshold\"] for transition>"],
    "visual_subject": "...",
    "visible_details": ["..."],
    "visible_action": "...",
    "visible_result": "<what you see at the moment the action lands>",
    "state_before": "...",
    "state_after": "<how far the work got, carrying a quantity>",
    "persistent_traces": ["..."],
    "material_specs": ["<one to three engineering specs lifted from the frame facts' mat_specs>"],
    "fastening_and_bonding": ["<one to three visible fasteners or bonds, from the frame facts' fasteners>"],
    "micro_traces": ["<one to three fine marks, from the frame facts' micro_traces; never repeat persistent_traces>"],
    "tool": "...",
    "tool_specifics": "<the type/drive/bit of that tool, from the frame facts' tool_specs; omit if not readable>",
    "sfx": ["..."],
    "shot_scale": "<extreme_wide|wide|medium|close|extreme_close>",
    "camera_angle": "<bird_eye|high_angle|eye_level|low_angle|worm_eye|dutch_angle>",
    "camera_bearing": "<front|three_quarter|side|rear_three_quarter|back>",
    "lens_feel": "<ultra_wide|wide|normal|tele|macro>",
    "subject_placement": "<one short sentence: the subject's position in thirds, what fraction of frame height it fills, where the horizon sits; fractions in words>",
    "time_treatment": "<timelapse|real_time|slow_motion>",
    "camera_move": "<static|push_in|pull_out|pan|tilt|orbit|follow|handheld|crane>",
    "worker_count": <integer>,
    "light_state": "...",
    "material_flow": "...",
    "cast_action": "<ACTION-REACTION CAUSAL CHAIN: Trigger Event -> Immediate Reflex -> Engaged Tracking -> Settled Posture; never 'remain/stay/unchanged'; omit if nobody is in frame>",
    "insert_subject": "<what the film's closer cut-in inside this beat is on; omit if this beat is one uninterrupted shot>",
    "workers_present": true|false,
    "source_event_ids": ["E01"],
    "evidence_frames": ["review_001.png", "review_004.png", "review_007.png"],
    "confidence": 0.95
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
        if f.get('cast_appearance'):
            parts.append(f'cast={"/".join(f["cast_appearance"])}')
        if f.get('camera_angle') or f.get('camera_bearing'):
            parts.append(f'view={f.get("camera_angle") or "?"}/{f.get("camera_bearing") or "?"}')
        if f.get('shot_scale'):
            parts.append(f'scale={f["shot_scale"]}')
        if f.get('lens_feel'):
            parts.append(f'lens={f["lens_feel"]}')
        if f.get('subject_placement'):
            parts.append(f'placement={f["subject_placement"]}')
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
        # 预估输出 token 预算：基线 65536，确保长片多拍（20+ 拍）与结构回炉提示词不撞硬上限
        target_tokens = 65536
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
    truncated_once = False
    attempt = 0
    while attempt <= max_rework:
        parse_budget = _PARSE_RETRY_BUDGET
        while True:
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
                      'more likely than an impossible build order. Keep each prose field strictly under twenty words.\n'
                )
            if truncated_once:
                # 不说清楚它还会再写一份一样长的。截断的解药是写短，不是重试。
                prompt += (
                    '\n==================== YOUR PREVIOUS REPLY WAS CUT OFF ====================\n'
                    'It ran past the output limit before it finished. Produce FEWER, WIDER beats by '
                    'merging adjacent events that belong to the same milestone, and keep every prose field under twenty words.\n'
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
            break

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
        ensure_three_evidence_frames(beats_doc, overview)
        attach_coverage_frames(beats_doc, overview)
        attach_shot_cuts(beats_doc, overview)
        # 逐镜景别序列（v7）。必须排在 attach_shot_cuts 之后：它切镜头窗靠的就是那一步
        # 挂上的 observed_cuts。
        attach_shot_scales(beats_doc, overview, facts=facts)
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
    # 2026-08-22：新增的制作字段里只有这四个是自由文本，需要中文对照。
    # shot_scale / camera_move / camera_angle / camera_bearing 是闭集枚举、
    # worker_count 是整数——它们的中文在界面上由 *_LABELS_ZH 直接渲染，翻译它们只会把
    # 闭集值译成一句中文，再被 _coerce_enum 丢掉。
    'tool', 'sfx', 'light_state', 'material_flow',
    # 2026-08-23：插入镜主体也是自由文本，且它是要被逐字抄进成片的一句，
    # 核对的人必须看得懂它写的是什么。
    'insert_subject', 'cast_action',
    # 2026-08-25：主体构图也是自由文本（位置、占比、地平线三件事），核对的人必须看得懂。
    # lens_feel / time_treatment 是闭集，不译（理由同上）。
    'subject_placement',
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


_TRANSIENT_TOOL_PATTERN = re.compile(
    r'\b(?:drill|impact\s*driver|driver|saw|sander|nailer|nail\s*gun|staple\s*gun|'
    r'trowel|putty\s*knife|tape\s*measure|measuring\s*tape|utility\s*knife|'
    r'caulk(?:ing)?\s*gun|paint(?:brush|\s*roller|brush)?|roller|hammer|mallet|'
    r'wrench|pliers|chisel|crowbar|pry\s*bar|level|spirit\s*level|'
    r'glove|gloves|mask|glasses|rag|sponge|bucket|screw|screws|nail|nails)\b',
    re.IGNORECASE
)

def _is_transient_tool(phrase):
    return bool(_TRANSIENT_TOOL_PATTERN.search(str(phrase or '')))


def analyze_scene_constants(facts, min_ratio=SCENE_CONSTANT_RATIO,
                            limit=_SCENE_CONSTANT_LIMIT):
    """出现在超过 `min_ratio` 比例的帧里的可见物，按材质 / 痕迹 / 常驻器具 / 常驻活物分栏。

    判据在词一级、展示在短语一级，与 `analyze_time_windows` 同一套方法（同一样东西
    每帧措辞不同，按整串统计会把一个恒常物拆成几十个「只出现一次」的条目）。
    过滤掉手持工具（电钻/锤子/锯子/抹刀等）与一次性消耗品，避免将瞬态施工工具误判为全片常驻器具。
    """
    rows = [f for f in (facts or []) if isinstance(f, dict)]
    if not rows:
        return {}
    n = len(rows)

    out = {}
    # cast_appearance → cast：全片同一个人/同一条狗的外形（人种、肤色、发型、衣着、鞋）。
    # 和材质/痕迹同一套「过半数帧里都在 = 恒常」的判据，因为它确实是恒常项：工序每拍都
    # 变，穿的那件红T恤不变。它不落在任何一拍的 delta 里，所以和污渍青苔一样，不单独送
    # 进提示词就会在每一帧被模型重新想象一次——同一条片子里换人种、换衣服、换发型。
    for field, key in (('materials', 'materials'), ('traces', 'traces'),
                       ('tools', 'fixtures_in_shot'), ('cast_appearance', 'cast')):
        hits, best = {}, {}
        for fact in rows:
            seen_here = set()
            for item in (fact.get(field) or []):
                phrase = str(item).strip()
                if not phrase:
                    continue
                if key == 'fixtures_in_shot' and _is_transient_tool(phrase):
                    continue
                for word in _content_words(phrase):
                    best.setdefault(word, {})
                    best[word][phrase] = best[word].get(phrase, 0) + 1
                    seen_here.add(word)
            for word in seen_here:
                hits[word] = hits.get(word, 0) + 1

        # cast 单独压低上限。其余各栏多一条只是多一句环境描述，人物栏多一条却是**多一个
        # 人**：同一个工人在不同帧里的措辞难免有出入（"red tee" / "faded red long-sleeve"），
        # 每一条都收进去，交付出去的就是一段互相打架的人物描述，模型只能自己挑一个。
        # 三条足够覆盖「工人 + 助手 + 一条狗」，再多几乎一定是同一个人的不同说法。
        key_limit = 3 if key == 'cast' else limit
        picked, claimed = [], set()
        for word, k in sorted(hits.items(), key=lambda kv: -kv[1]):
            if k / n < min_ratio:
                break
            phrase = max(best[word].items(), key=lambda kv: (kv[1], -len(kv[0])))[0]
            if key == 'fixtures_in_shot' and _is_transient_tool(phrase):
                continue
            if phrase.lower() in claimed:
                continue
            claimed.add(phrase.lower())
            picked.append(phrase)
            if len(picked) >= key_limit:
                break
        if picked:
            out[key] = picked
    return out


# ── 微缩题材判定（这一单该不该走微缩沙盘那套系统提示词）────────────────────────
# 2026-08-30 实测 replica_af8db0d7a95f：一单**真人实拍**的半挂车改造被判成微缩片，
# 交付正文里 39 处 miniature、33 处 giant hands、8 处 diorama，施工者成了"拇指高的
# miniature builder"，用户看到的还是一屋子假人。唯一的触发证据是识别项里的一个词：
#   "Caucasian male builder/craftsman in his 30s…"
# —— 当年的旧判据把 'craftsman' 当成"巨手工匠"的证据。可 craftsman 就是
# 「手艺人/工匠」，真人施工片里最常见的自称之一。一个中性职业词把整条通道拨到了另一
# 个题材上，而且全程没有任何提示。
#
# 现在只认真正指向微缩题材的证据：显式档位、题材词、或识别项里的比例/人偶记号。
# **绝不再拿职业词当证据。**
_MINIATURE_TOPIC_HINTS = (
    'miniature', 'diorama', 'tabletop', 'dollhouse', "doll's house", 'dolls house',
    '微缩', '沙盘', '手作模型', '微缩模型',
)
# 识别项里的微缩记号：人偶、拇指高、1:24 这类比例、以及真人化改写留下的
# "miniature-scale"（见 prompt_pipeline.human_cast——改写会把 figurine 换成真人措辞，
# 判据不能只认 figurine，否则归一之后证据就没了）。
_CAST_MINIATURE_RE = re.compile(
    r'(?i)\b(?:miniature|dollhouse|figurines?|thumb[-\s]tall|god[-\s]?hand)\b'
    r'|\b1\s*[:：]\s*\d{1,3}\b'
)


def detect_miniature_scale(beats_doc, title=None, config=None):
    """这一单是不是微缩沙盘题材。返回 (is_miniature, 证据说明)。

    判定顺序：doc 上已经定过的 render_scale > 显式 skillProfile > 题材词 > 识别项记号。
    落在 doc 上的那份是权威——判定必须**只做一次**并跟着任务走：识别项的措辞会被后续
    改写（真人化）、被人在卡点上编辑，每跑一趟重新从文本里猜一次，就会出现同一单前后
    两趟走两条通道。
    """
    doc = beats_doc if isinstance(beats_doc, dict) else {}

    # 显式设置排在已定档**之前**：定档是为了让自动判定稳定（识别项被改写、被人编辑
    # 都不该让同一单换通道），但它不该反过来压住用户当次的明确指定——那就成了"设置
    # 无效"，正是这一串问题的老毛病。
    if str((config or {}).get('skillProfile') or '').strip().lower() == 'miniature':
        return True, '配置显式指定 skillProfile=miniature'

    recorded = str(doc.get('render_scale') or '').strip().lower()
    if recorded in ('miniature', 'full'):
        return recorded == 'miniature', f'任务已定档：{recorded}'

    haystack = ' '.join(str(x or '') for x in (
        title, doc.get('carrier'), doc.get('scene_signature'), doc.get('video_name'))).lower()
    for hint in _MINIATURE_TOPIC_HINTS:
        if hint in haystack:
            return True, f'题材词命中「{hint}」'

    for c in (doc.get('cast_identity') or []):
        m = _CAST_MINIATURE_RE.search(str(c or ''))
        if m:
            return True, f'识别项里的微缩记号「{m.group(0)}」'

    return False, '没有任何微缩证据（题材词与识别项记号都没命中）'


def freeze_render_scale(beats_doc, title=None, config=None):
    """把微缩判定钉在 doc 上（render_scale: 'miniature' | 'full'），返回是否微缩。

    只在键不存在时定一次——与 attach_scene_constants 判「键在不在」同一条理由：
    之后识别项被改写或被人编辑，都不该让同一单换通道。
    """
    if not isinstance(beats_doc, dict):
        return False
    is_mini, why = detect_miniature_scale(beats_doc, title=title, config=config)
    if 'render_scale' not in beats_doc:
        beats_doc['render_scale'] = 'miniature' if is_mini else 'full'
        beats_doc['render_scale_reason'] = why
    return str(beats_doc.get('render_scale')).lower() == 'miniature'

def attach_scene_constants(beats_doc, facts):
    """算出场景恒常特征并挂到文档上。只在字段**不存在**时算一次。

    判「键在不在」而不是「值真不真」：它进合成提示词、影响每一条产物，而统计难免会
    把工人的手套、一次性道具算进来。用户在卡点上把三栏全删空之后，值是空的、键还在——
    按真值判定的话，下一次读状态就会把它们原样加回去，用户永远删不掉。
    """
    if 'scene_constants' in beats_doc:
        return beats_doc['scene_constants']
    # 一个都没统计出来也要落键，否则每次读状态都会重算一遍（几百 KB 的事实全扫）。
    constants = analyze_scene_constants(facts)
    # 「一直在动的东西」并不进本地统计：帧事实是一张张**静止**画面的清单，水在流、烟在飘
    # 这件事在任何单帧里都看不出来。它只能由 Pass B 从帧序列里读（顶层 ambient_motion），
    # 在这里并进同一个容器，之后全链路只认 scene_constants.motion 一处。
    motion = [str(x).strip() for x in (beats_doc.get('ambient_motion') or []) if str(x).strip()]
    if motion:
        constants['motion'] = motion
    # 人物外形（cast）两个来源都要：本地统计（analyze_scene_constants 的 cast 栏）绝不
    # 凭空捏造，但措辞是从单帧读数里挑的、偏碎；Pass B 的顶层 cast_identity 是看过整段
    # 帧序列之后写的一句整话（"the same lone craftsman throughout: …"），读起来像人话，
    # 也才写得出「全片同一个人」这层意思。有模型那份就用模型那份，没有就退回统计那份——
    # 与 scene_signature / scene_constants 并存是同一条理由（失效方式相反）。
    cast = [str(x).strip() for x in (beats_doc.get('cast_identity') or []) if str(x).strip()]
    if cast:
        constants['cast'] = cast
    # 通道判定必须排在真人化**之前**：真人化会把 figurine 换成真人措辞，那正是
    # detect_miniature_scale 认微缩用的证据之一，先归一再判就等于把证据擦掉再断案。
    freeze_render_scale(beats_doc)
    # 活物一律真人：读数照原片老实读（原片真是微缩沙盘就该读出人偶），但**恒常项一落地
    # 就归一成真人**——这一栏是全链路复述外形的唯一权威，写着 figurine 的话每一帧
    # IMAGE 都会跟着复述一遍，交付出来就是一屋子蜡像。尺寸不归这里管（比例锁另有其人）。
    # cast_identity 同步改写：两处不同步的话，合成器拿到的还是人偶那份。
    from .human_cast import humanize_cast_list
    if constants.get('cast'):
        constants['cast'] = humanize_cast_list(list(constants['cast']))
    if beats_doc.get('cast_identity'):
        beats_doc['cast_identity'] = humanize_cast_list(list(beats_doc['cast_identity']))
    # 环境底噪与 motion 严格对称：那一栏是「一直在动」，这一栏是「一直在响」。同样统计
    # 不出来（帧事实是一张张无声画面），只能由 Pass B 读，或人在卡点上补。不给它落脚点，
    # 每条 VIDEO 结尾那句 "Ambient noise:" 就是模型现编的，整片的声场一拍一个样。
    ambient_sound = [str(x).strip() for x in (beats_doc.get('ambient_sound') or []) if str(x).strip()]
    if ambient_sound:
        constants['ambient_sound'] = ambient_sound
    # 影调：一句话，但仍然装进列表——这个容器全链路按列表读（scene_constants_lines、
    # 卡片上的「、」分隔、beats_to_dimensions 的 list(v)）。为它单开一个字符串字段，
    # 就要在这四处各加一个类型分支。
    grade = ' '.join(str(beats_doc.get('color_grade') or '').split())
    if grade:
        constants['grade'] = [grade]
    beats_doc['scene_constants'] = constants
    return beats_doc['scene_constants']


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


# 「这一帧看着已经完工到什么程度」的词表。判据不是命中绝对数量，而是首帧与紧随其后
# 那几帧的**落差**——一支正常的施工片开头几帧分数都低，先导闪帧则是孤零零的一个高分。
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


# 首帧要比紧随其后那几帧「超前」这么多分，才算先导闪帧。1 分是噪音（一个 installed
# 就够了），2 分要求首帧至少有两处独立的完工证据、而后续帧一处都没有。
_TEASER_SCORE_GAP = 2
# 往后看几帧作为「真正的起点长什么样」的对照。
_TEASER_LOOKAHEAD = 5
# 先导钩子是**闪帧**：0.16s 一闪而过。跳过的那一段必须整个落在这个时间窗内，否则那不是
# 钩子，而是这支片子真的从一个成品状态开拍——旧房翻新片的「改造前」全景就是这样，它会
# 持续好几秒、后面紧跟着拆改。少了这一条，翻新题材会被整片砍掉第一拍。
_TEASER_FLASH_WINDOW_SECONDS = 0.6
# 最多跳这么多张。抽帧密度约 5fps 时 0.16s 的闪帧只占 1 张，留 2 张余量给更密的抽帧。
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


def anchor_reference_frame(beats_doc, overview, facts=None):
    """起始锚点该照着哪一张真实帧写：原片最早的那一张有效送审帧。

    锚点图（IMAGE 1）是整条序列的地基——后面每一拍的画面都从它继承材质与光线。它写歪
    了，后面全歪，而组稿阶段它恰恰是凭文字空想出来的。
    原片开头若是短视频常见的完工/半成品先导钩子闪帧（Teaser Flash），而后续几帧才是真正
    的施工放线/未开发原始地面，则自动跳过，锁定真实起始帧——判据见 opening_anchor_skip，
    与另外三个注入点共用同一份。
    """
    entries = [e for e in ((overview or {}).get('review_sampling') or {}).get('frames') or []
               if e.get('frame_path') and e.get('timestamp') is not None]
    if not entries:
        return None
    entries = [e for e in entries if os.path.exists(e['frame_path'])]
    if not entries:
        return None
    entries.sort(key=lambda e: _num(e['timestamp'], default=1e9))

    if facts is None:
        facts = (beats_doc or {}).get('facts') or (overview or {}).get('facts')
        if not facts and entries[0].get('frame_path'):
            job_dir_cand = os.path.dirname(os.path.dirname(entries[0]['frame_path']))
            ff_path = os.path.join(job_dir_cand, 'frame_facts.json')
            if os.path.exists(ff_path):
                try:
                    with open(ff_path, 'r', encoding='utf-8') as f:
                        facts = json.load(f)
                except Exception:
                    facts = None
    if isinstance(facts, dict):
        facts = facts.get('facts') or []
    facts_list = facts if isinstance(facts, list) else []

    # 读数优先按帧名对齐，对不上再退回位置对齐：review_sampling 与 frame_facts 是同一次
    # 抽帧的两份产物、顺序一致，但只有 frame_facts 那份带 `frame` 字段。
    facts_by_name = {str(f.get('frame')): f for f in facts_list
                     if isinstance(f, dict) and f.get('frame')}
    rows = []
    for i, e in enumerate(entries[:_TEASER_LOOKAHEAD + _TEASER_MAX_SKIP]):
        name = os.path.basename(e['frame_path'])
        fact = facts_by_name.get(name)
        if fact is None and not facts_by_name and i < len(facts_list):
            fact = facts_list[i]
        rows.append({'name': name, 'timestamp': _num(e.get('timestamp'), default=0.0),
                     'text': _stage_text(fact)})
    if not any(r['text'] for r in rows):
        return entries[0]['frame_path']
    return entries[opening_anchor_skip(rows)]['frame_path']


_WEIGHTED_NEGATIVE_RE = re.compile(r'\(\s*[^()]*?:\s*\d+(?:\.\d+)?\s*\)')
_NEGATIVE_LINE_RE = re.compile(r'(?im)^[ \t]*(?:negative(?:\s+prompt)?|anti-symmetry\s+negatives?)[ \t]*[:：].*$')


def strip_weighted_negatives(text):
    """剥掉 SD 风格的负向提示词与权重括号。

    出图端是 Gemini image（Nano Banana 2），整条链路没有负向通道，也不解析 `(...:1.6)`
    权重语法——这类括号会被当成画面描述照着画。2026-09-01 那次「首帧渲出暗角、望远镜
    镜头、对标帧根本没有的中心圆孔」就是这么来的：一句本意是禁止的
    `(centered composition, ..., telescope vignette, tunnel vision:1.6)` 被原样拼在正向
    提示词末尾，语义整个翻转。

    上游的 system prompt 已经明说不要写，但那是软约束；这里做硬兜底，顺带清掉历史缓存
    里可能已经带上的尾巴。
    """
    if not text:
        return text
    cleaned = _NEGATIVE_LINE_RE.sub('', text)
    cleaned = _WEIGHTED_NEGATIVE_RE.sub('', cleaned)
    # 剥掉留下的孤立标点与空行，别让提示词以 " ." 或连续空行收尾
    cleaned = re.sub(r'[ \t]+([.,;])', r'\1', cleaned)
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
    cleaned = re.sub(r'[ \t]{2,}', ' ', cleaned)
    return cleaned.strip() or text


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
        "You are rewriting ONE photoreal image prompt: the opening anchor frame (IMAGE 1) of a "
        "reconstruction sequence. You are shown the ACTUAL first frame of the reference film "
        "this sequence reproduces.\n\n"
        "Rewrite the prompt so that its camera perspective, viewpoint angle, spatial layout "
        "(left third vs center third vs right third), portal/opening placement, structure, materials, "
        "weathering, vegetation, clutter and light 100% faithfully match what you observe in the reference frame.\n\n"
        "Rules:\n"
        "1. [Strict Spatial Topology & Asymmetry Lock]:\n"
        "   - Read the exact camera angle, pitch, bearing, and framing from the reference frame.\n"
        "   - Describe what occupies the left third, the center third and the right third of the frame — "
        "solid boundary, rock or wall; floor substrate, pools, depressions, vanishing depth; portal opening or "
        "exterior view — written as flowing visual prose, NOT as a labelled list and NOT as grid coordinates.\n"
        "   - State the framing positively, exactly as the reference frame shows it. If the opening/portal is "
        "off-center (e.g. on the right side) and the viewpoint is 3/4 oblique, you MUST write that 3/4 oblique "
        "off-center framing explicitly, with the field of view reaching corner to corner and the scene filling the "
        "frame edge to edge. NEVER collapse an off-center opening into a centered circular vignette or symmetrical tunnel.\n"
        "2. [Clean Frame Anchor & Structural Balance]:\n"
        "   - Do NOT describe transient moving workers or floating tools.\n"
        "   - If the reference frame shows a person standing on one side of the frame (e.g. on a rock ledge or floor on the left), "
        "you MUST describe the permanent physical structure, rock shelf, or raised platform they occupy to preserve the authentic visual weight on that side, "
        "preventing the image generator from shifting openings to the center.\n"
        "3. [Material & Texture Fidelity]:\n"
        "   - Keep every decay, stain, water puddle, moss, debris and texture the reference frame actually shows.\n"
        "4. [Positive Description Only]:\n"
        "   - The renderer has no negative-prompt channel and no weight syntax. Every word you write gets drawn.\n"
        "   - NEVER append a negative prompt, a banned-element list, or weighted tokens such as `(...:1.6)`. "
        "Naming an unwanted effect (vignette, tunnel vision, circular portal, centered composition) makes the "
        "renderer draw that effect. Say what the frame DOES look like instead.\n"
        "5. Plain visual prose only — no commentary, no headings, no zone labels, no code fences. "
        "Match the wording and shape of an ordinary photoreal shot description, so this prompt reads like every "
        "other beat in the sequence.\n"
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
    text = strip_weighted_negatives((text or '').strip())
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
    # 2026-08-22 新增的六个制作字段。别名照旧只搬运不生成。`audio_asmr_cues` 是
    # mutate 的正交变异线一直在写、而全链路没有一处在读的键（写完就没人管），
    # 现在把它并进 `sfx` 这条真出口。
    'tools': 'tool',
    'primary_tool': 'tool',
    'sound': 'sfx',
    'sounds': 'sfx',
    'audio': 'sfx',
    'audio_sfx': 'sfx',
    'sfx_cues': 'sfx',
    'audio_asmr_cues': 'sfx',
    'cast': 'cast_action',
    'figurine_action': 'cast_action',
    'figurines': 'cast_action',
    'worker_action': 'cast_action',
    'people_action': 'cast_action',
    'posture': 'cast_action',
    'insert': 'insert_subject',
    'insert_shot': 'insert_subject',
    'cutaway_subject': 'insert_subject',
    'shot_size': 'shot_scale',
    'framing': 'shot_scale',
    'shot': 'shot_scale',
    'camera': 'camera_move',
    'camera_movement': 'camera_move',
    'camera_motion': 'camera_move',
    'angle': 'camera_angle',
    'shot_angle': 'camera_angle',
    'camera_height': 'camera_angle',
    'vertical_angle': 'camera_angle',
    'camera_pitch': 'camera_angle',
    'view_angle': 'camera_angle',
    'bearing': 'camera_bearing',
    'camera_side': 'camera_bearing',
    'facing': 'camera_bearing',
    'orientation': 'camera_bearing',
    'horizontal_angle': 'camera_bearing',
    'camera_azimuth': 'camera_bearing',
    'lens': 'lens_feel',
    'focal_length': 'lens_feel',
    'lens_type': 'lens_feel',
    'field_of_view': 'lens_feel',
    'placement': 'subject_placement',
    'composition': 'subject_placement',
    'framing_position': 'subject_placement',
    'subject_position': 'subject_placement',
    'subject_scale': 'subject_placement',
    'speed': 'time_treatment',
    'time_mode': 'time_treatment',
    'playback_speed': 'time_treatment',
    'motion_treatment': 'time_treatment',
    'crew_size': 'worker_count',
    'worker_num': 'worker_count',
    'lighting': 'light_state',
    'light': 'light_state',
    'light_source_state': 'light_state',
    'time_of_day': 'light_state',
    'spoil': 'material_flow',
    'material_balance': 'material_flow',
    'spoil_balance': 'material_flow',
    'timestamp_start': 'start',
    'timestamp_end': 'end',
    'start_time': 'start',
    'end_time': 'end',
    'time_start': 'start',
    'time_end': 'end',
    't_start': 'start',
    't_end': 'end',
}

# ── 制作字段的值域 ───────────────────────────────────────────────────────────
#
# 景别与运镜是**闭集**，不收自由文本：规划器读到 "slow dramatic push through the
# autumn canopy" 会把它当创作提示接着发挥，读到 `push_in` 才是照抄。模型仍然会写
# 自由文本，所以这里留一张够宽的近义词表，认不出来的一律丢弃——留一个歪值下去，
# 等于让那一拍的机位由一句没人校对过的话决定。
SHOT_SCALES = ('extreme_wide', 'wide', 'medium', 'close', 'extreme_close')
CAMERA_MOVES = ('static', 'push_in', 'pull_out', 'pan', 'tilt', 'orbit',
                'follow', 'handheld', 'crane')
# 拍摄角度拆成两栏，因为它们是**两根互相独立的轴**：同一拍完全可以既是低角度仰拍、
# 又是从侧面拍的。捏成一栏就得二选一，而被丢掉的那一半正是原片最像自己的地方——
# 「贴地仰拍」和「站着俯看」是同一道工序的两条完全不同的片子。
# 倾斜（荷兰角）严格说是第三根轴（滚转），但它罕见且一旦出现就是这一拍最抢眼的角度
# 事实，所以并进 camera_angle，占掉那一格。
CAMERA_ANGLES = ('bird_eye', 'high_angle', 'eye_level', 'low_angle', 'worm_eye', 'dutch_angle')
CAMERA_BEARINGS = ('front', 'three_quarter', 'side', 'rear_three_quarter', 'back')
# 焦段感（2026-08-25）。与景别是两件事：14mm 拍中景和 85mm 拍中景，透视、畸变、纵深
# 完全两回事，而 camera_dna 里那句 "ultra-wide 16mm lens feel" 此前是模型自己编的。
LENS_FEELS = ('ultra_wide', 'wide', 'normal', 'tele', 'macro')
# 这一拍的时间处理。此前每一拍都被写成 "continuous construction time-lapse"——包括最后
# 那个成品巡览拍，而原片的巡览基本都是实时的。没人报错，因为没有字段承接它。
TIME_TREATMENTS = ('timelapse', 'real_time', 'slow_motion')

SHOT_SCALE_LABELS_ZH = {
    'extreme_wide': '大远景', 'wide': '远景', 'medium': '中景',
    'close': '近景', 'extreme_close': '特写',
}
CAMERA_ANGLE_LABELS_ZH = {
    'bird_eye': '鸟瞰', 'high_angle': '高角度俯拍', 'eye_level': '平视',
    'low_angle': '低角度仰拍', 'worm_eye': '虫视', 'dutch_angle': '倾斜角',
}
CAMERA_BEARING_LABELS_ZH = {
    'front': '正面', 'three_quarter': '前侧四分之三', 'side': '侧面',
    'rear_three_quarter': '后侧四分之三', 'back': '背面',
}
LENS_FEEL_LABELS_ZH = {
    'ultra_wide': '超广', 'wide': '广角', 'normal': '标准', 'tele': '长焦', 'macro': '微距',
}
TIME_TREATMENT_LABELS_ZH = {
    'timelapse': '延时加速', 'real_time': '实时', 'slow_motion': '慢动作',
}
CAMERA_MOVE_LABELS_ZH = {
    'static': '固定', 'push_in': '缓推', 'pull_out': '缓拉', 'pan': '横摇',
    'tilt': '俯仰摇', 'orbit': '环绕', 'follow': '跟随', 'handheld': '手持',
    'crane': '升降',
}

_SHOT_SCALE_SYNONYMS = {
    'ews': 'extreme_wide', 'extreme_wide_shot': 'extreme_wide', 'establishing': 'extreme_wide',
    'establishing_shot': 'extreme_wide', 'very_wide': 'extreme_wide', 'xws': 'extreme_wide',
    'aerial': 'extreme_wide', 'drone': 'extreme_wide',
    'ws': 'wide', 'wide_shot': 'wide', 'long': 'wide', 'long_shot': 'wide', 'full': 'wide',
    'full_shot': 'wide',
    'ms': 'medium', 'medium_shot': 'medium', 'mid': 'medium', 'mid_shot': 'medium',
    'medium_wide': 'medium', 'medium_close': 'medium',
    'cu': 'close', 'close_up': 'close', 'closeup': 'close', 'close_shot': 'close',
    'ecu': 'extreme_close', 'extreme_close_up': 'extreme_close', 'macro': 'extreme_close',
    'detail': 'extreme_close', 'insert': 'extreme_close',
}
_CAMERA_ANGLE_SYNONYMS = {
    'birds_eye': 'bird_eye', 'bird_s_eye': 'bird_eye', 'birdseye': 'bird_eye',
    'top_down': 'bird_eye', 'topdown': 'bird_eye', 'overhead': 'bird_eye',
    'straight_down': 'bird_eye', 'aerial': 'bird_eye', 'drone': 'bird_eye',
    'plan_view': 'bird_eye', 'god_view': 'bird_eye',
    'high': 'high_angle', 'looking_down': 'high_angle', 'downward': 'high_angle',
    'elevated': 'high_angle', 'above': 'high_angle', 'raised': 'high_angle',
    'eye': 'eye_level', 'eyelevel': 'eye_level', 'level': 'eye_level',
    'neutral': 'eye_level', 'chest_height': 'eye_level', 'standing_height': 'eye_level',
    'straight_on': 'eye_level', 'horizontal': 'eye_level',
    'low': 'low_angle', 'looking_up': 'low_angle', 'upward': 'low_angle',
    'below': 'low_angle', 'hero_angle': 'low_angle', 'up_angle': 'low_angle',
    'worms_eye': 'worm_eye', 'worm_s_eye': 'worm_eye', 'wormseye': 'worm_eye',
    'ground_level': 'worm_eye', 'ground': 'worm_eye', 'floor_level': 'worm_eye',
    'straight_up': 'worm_eye',
    'dutch': 'dutch_angle', 'dutch_tilt': 'dutch_angle', 'canted': 'dutch_angle',
    'canted_angle': 'dutch_angle', 'tilted': 'dutch_angle', 'oblique': 'dutch_angle',
    'skewed': 'dutch_angle',
}
_CAMERA_BEARING_SYNONYMS = {
    'frontal': 'front', 'head_on': 'front', 'straight_on': 'front', 'face_on': 'front',
    'front_on': 'front', 'facade': 'front',
    'three_quarters': 'three_quarter', 'threequarter': 'three_quarter',
    'front_three_quarter': 'three_quarter', 'front_quarter': 'three_quarter',
    'corner': 'three_quarter', 'angled': 'three_quarter', 'diagonal': 'three_quarter',
    'profile': 'side', 'lateral': 'side', 'side_on': 'side', 'broadside': 'side',
    'flank': 'side', 'from_the_side': 'side',
    'back_three_quarter': 'rear_three_quarter', 'rear_quarter': 'rear_three_quarter',
    'over_the_shoulder': 'rear_three_quarter', 'ots': 'rear_three_quarter',
    'rear': 'back', 'behind': 'back', 'from_behind': 'back', 'reverse': 'back',
    'back_on': 'back', 'tail_on': 'back',
}
_LENS_FEEL_SYNONYMS = {
    'ultrawide': 'ultra_wide', 'ultra_wide_angle': 'ultra_wide', 'uwa': 'ultra_wide',
    'superwide': 'ultra_wide', 'super_wide': 'ultra_wide', 'fisheye': 'ultra_wide',
    'action_cam': 'ultra_wide', 'gopro': 'ultra_wide', 'drone_lens': 'ultra_wide',
    '10mm': 'ultra_wide', '12mm': 'ultra_wide', '14mm': 'ultra_wide', '16mm': 'ultra_wide',
    '18mm': 'ultra_wide', '20mm': 'ultra_wide',
    'wide_angle': 'wide', 'wideangle': 'wide', 'phone_wide': 'wide',
    '24mm': 'wide', '28mm': 'wide', '35mm': 'wide',
    'standard': 'normal', 'nifty_fifty': 'normal', 'natural_perspective': 'normal',
    'no_distortion': 'normal', '40mm': 'normal', '50mm': 'normal', '55mm': 'normal',
    'telephoto': 'tele', 'long_lens': 'tele', 'zoomed_in': 'tele', 'compressed': 'tele',
    'flattened_perspective': 'tele',
    '85mm': 'tele', '105mm': 'tele', '135mm': 'tele', '200mm': 'tele', '300mm': 'tele',
    'macro_lens': 'macro', 'extreme_macro': 'macro', 'micro': 'macro', 'probe_lens': 'macro',
}
_TIME_TREATMENT_SYNONYMS = {
    'time_lapse': 'timelapse', 'timelapsed': 'timelapse', 'hyperlapse': 'timelapse',
    'sped_up': 'timelapse', 'speed_up': 'timelapse', 'fast_forward': 'timelapse',
    'fastforward': 'timelapse', 'accelerated': 'timelapse', 'fast_motion': 'timelapse',
    'realtime': 'real_time', 'real': 'real_time', 'normal_speed': 'real_time',
    'actual_speed': 'real_time', 'one_x': 'real_time', '1x': 'real_time',
    'live_action': 'real_time', 'unaccelerated': 'real_time',
    'slowmo': 'slow_motion', 'slow_mo': 'slow_motion', 'slomo': 'slow_motion',
    'high_speed': 'slow_motion', 'overcranked': 'slow_motion', 'slowed': 'slow_motion',
}
_CAMERA_MOVE_SYNONYMS = {
    'fixed': 'static', 'locked': 'static', 'locked_off': 'static', 'lockedoff': 'static',
    'none': 'static', 'still': 'static', 'tripod': 'static',
    'push': 'push_in', 'dolly_in': 'push_in', 'zoom_in': 'push_in', 'truck_in': 'push_in',
    'move_in': 'push_in', 'forward': 'push_in', 'dolly_forward': 'push_in',
    'pull': 'pull_out', 'pull_back': 'pull_out', 'dolly_out': 'pull_out',
    'zoom_out': 'pull_out', 'reveal_pullback': 'pull_out', 'backward': 'pull_out',
    'pan_left': 'pan', 'pan_right': 'pan', 'whip_pan': 'pan', 'swivel': 'pan',
    'tilt_up': 'tilt', 'tilt_down': 'tilt',
    'arc': 'orbit', 'orbital': 'orbit', 'circle': 'orbit', 'around': 'orbit',
    'tracking': 'follow', 'track': 'follow', 'trailing': 'follow', 'walkthrough': 'follow',
    'steadicam': 'follow', 'gimbal': 'follow',
    'hand_held': 'handheld', 'shaky': 'handheld', 'pov': 'handheld',
    'jib': 'crane', 'boom': 'crane', 'crane_up': 'crane', 'crane_down': 'crane',
}


_NUMBER_WORDS = {
    'zero': 0, 'no': 0, 'none': 0, 'nobody': 0, '无': 0, '零': 0,
    'one': 1, 'a': 1, 'an': 1, 'single': 1, 'lone': 1, 'solo': 1, '一': 1, '独': 1,
    'two': 2, 'pair': 2, 'both': 2, '两': 2, '二': 2,
    'three': 3, '三': 3, 'four': 4, '四': 4, 'five': 5, '五': 5,
    'six': 6, '六': 6, 'seven': 7, '七': 7, 'eight': 8, '八': 8,
    'nine': 9, '九': 9, 'ten': 10, '十': 10,
}


def _coerce_count(value):
    """人数。数字优先，其次数词——Pass B 要的是整数，实际交回来的多半是
    "one lone craftsman"。两样都认不出就返回 None（未标注），绝不默认成 0：
    0 是「这一拍清场」这个真实断言，跟「没写」不是一回事。"""
    raw = str(value if value is not None else '').strip()
    if not raw:
        return None
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        pass
    digits = re.search(r'\d+', raw)
    if digits:
        return int(digits.group())
    for token in re.split(r'[^a-zA-Z一-鿿]+', raw.lower()):
        if token in _NUMBER_WORDS:
            return _NUMBER_WORDS[token]
    return None


def _coerce_enum(value, allowed, synonyms):
    """把模型写的自由文本收进闭集；收不进就返回 None（宁可空着，也不留歪值）。"""
    token = re.sub(r'[^a-z0-9]+', '_', str(value or '').strip().lower()).strip('_')
    if not token:
        return None
    if token in allowed:
        return token
    if token in synonyms:
        return synonyms[token]
    # "slow push in on the trench" 这类整句：从里面捞第一个认得的词。长词优先，
    # 否则 "push_in" 会先被 "push" 撞上（结果一样，但顺序一变就不一样了）。
    for key in sorted(list(allowed) + list(synonyms), key=len, reverse=True):
        if key in token:
            return synonyms.get(key, key) if key not in allowed else key
    return None


def normalize_beat_craft_fields(beats_doc):
    """制作字段的值域归一：枚举收进闭集、人数收成整数、单串/列表互转。

    跟着 `normalize_beat_keys` 一起跑（键名归位完才谈得上值域），因为调用点分散在
    replica_pipeline 与本模块的五处，单独开一个函数必然漏掉其中一两处——键名归一
    自己就是被漏出来的教训。
    """
    for beat in (beats_doc.get('beats') or []):
        if not isinstance(beat, dict):
            continue
        if 'sfx' in beat:
            raw = beat['sfx']
            items = raw if isinstance(raw, (list, tuple)) else [raw]
            beat['sfx'] = [str(x).strip() for x in items if str(x or '').strip()][:4]
        # 微观取证三栏（2026-08-24）：单串写成列表、列表截到契约上界。模型把它们写成
        # 一个逗号串是常事，不归一的话下游 `isinstance(raw, (list, tuple))` 那一道会
        # 整栏判空——和没写一模一样，且不报错。
        for key in ('material_specs', 'fastening_and_bonding', 'micro_traces'):
            if key in beat:
                raw = beat[key]
                items = raw if isinstance(raw, (list, tuple)) else [raw]
                beat[key] = [str(x).strip() for x in items if str(x or '').strip()][:3]
        for key in ('tool', 'tool_specifics', 'light_state', 'material_flow',
                    'subject_placement'):
            if key in beat:
                value = beat[key]
                if isinstance(value, (list, tuple)):
                    value = '; '.join(str(x).strip() for x in value if str(x or '').strip())
                beat[key] = str(value or '').strip()
        if 'shot_scale' in beat:
            beat['shot_scale'] = _coerce_enum(
                beat['shot_scale'], SHOT_SCALES, _SHOT_SCALE_SYNONYMS) or ''
        if 'camera_move' in beat:
            beat['camera_move'] = _coerce_enum(
                beat['camera_move'], CAMERA_MOVES, _CAMERA_MOVE_SYNONYMS) or ''
        if 'camera_angle' in beat:
            beat['camera_angle'] = _coerce_enum(
                beat['camera_angle'], CAMERA_ANGLES, _CAMERA_ANGLE_SYNONYMS) or ''
        if 'camera_bearing' in beat:
            beat['camera_bearing'] = _coerce_enum(
                beat['camera_bearing'], CAMERA_BEARINGS, _CAMERA_BEARING_SYNONYMS) or ''
        if 'lens_feel' in beat:
            beat['lens_feel'] = _coerce_enum(
                beat['lens_feel'], LENS_FEELS, _LENS_FEEL_SYNONYMS) or ''
        if 'time_treatment' in beat:
            beat['time_treatment'] = _coerce_enum(
                beat['time_treatment'], TIME_TREATMENTS, _TIME_TREATMENT_SYNONYMS) or ''
        if 'worker_count' in beat:
            count = _coerce_count(beat['worker_count'])
            if count is None:
                beat.pop('worker_count', None)
            else:
                beat['worker_count'] = max(0, min(12, count))
        # 人数与「有没有人」是同一件事的两种写法，两处都在时以人数为准——用户在卡片上
        # 改的是人数那一栏，布尔那枚芯片是渲染出来给他看的。
        if isinstance(beat.get('worker_count'), int):
            beat['workers_present'] = beat['worker_count'] > 0
    return beats_doc


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

        # 补全基础契约默认值，防止由于别名或模型省略导致整单报红
        if not beat.get('visual_subject'):
            beat['visual_subject'] = beat.get('visible_result') or beat.get('operation') or f"Milestone {beat.get('id', '')}"
        if not beat.get('state_before'):
            beat['state_before'] = f"Initial starting condition prior to {beat.get('operation', 'work')}"
        if not beat.get('state_after'):
            beat['state_after'] = beat.get('visible_result') or beat.get('visible_action') or "Delivered milestone outcome"
        if beat.get('workers_present') is None:
            beat['workers_present'] = beat.get('worker_count', 1) > 0 if isinstance(beat.get('worker_count'), int) else True
        if not beat.get('package_operations'):
            beat['package_operations'] = [beat.get('operation')] if beat.get('operation') else ['work_step']
        if not beat.get('visible_details'):
            beat['visible_details'] = [beat.get('operation') or 'construction materials']
        if not beat.get('persistent_traces'):
            beat['persistent_traces'] = [beat.get('visible_result') or 'completed physical structure']
        if beat.get('source_event_ids') is None:
            beat['source_event_ids'] = []

    if moved:
        # 累加而不是覆盖，且没搬到东西时绝不清空：这条记录是这份文档的历史事实，
        # 不是一次调用的临时值。归一会在每次读状态、每次保存时重跑，第二次跑必然
        # 一处也搬不到——那时清空，等于这条 warn 只在用户看不见的那一瞬间存在过。
        beats_doc['key_normalizations'] = (beats_doc.get('key_normalizations') or []) + moved
    normalize_beat_craft_fields(beats_doc)
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


# ── 观察到的镜头切点 ─────────────────────────────────────────────────────────
#
# 抽帧脚本（skills/…/scripts/analyze_timelapse_video.py）一直在算两组跳变：高阈值那组
# 是**剪辑切点**（cut_points），低阈值那组扣掉切点附近 0.3s 之后是**状态变化**。整条
# 复刻线只消费了后者与聚合出来的 pace_metrics（scene_count / cut_count 三个数），
# 逐条 cut_points 落在 video_overview.json 里没有任何读者——于是「原片这一拍是一个
# 镜头还是切了三刀」这件事，采集到了、写进盘了、没人接。
#
# 多镜头语法（omni / miniature）交付的每一拍本身就是剪辑过的序列，这个数是它唯一的
# 事实底座：镜头梯排三镜还是四镜、原片到底切没切，都由它回答。派生字段，每次重算。
_CUT_EDGE_EPSILON = 0.15


def overview_cut_points(overview):
    """video_overview.json 里的剪辑切点（升序、去重、非负）。

    读不到就返回空列表而不是抛：老 job 的 overview 里没有这个键，抽帧脚本环境异常时
    也可能整组缺失，两种情况都该降级成「这一拍的镜头数未知」，而不是让读状态失败。
    """
    raw = (overview or {}).get('cut_points')
    if not isinstance(raw, (list, tuple)):
        return []
    points = []
    for item in raw:
        value = _num(item, -1.0)
        if value >= 0:
            points.append(round(value, 3))
    return sorted(set(points))


def observed_cuts_for_window(cut_points, start, end, edge=_CUT_EDGE_EPSILON):
    """落在拍窗**内部**的剪辑切点。

    边界那一刀不算：拍与拍的分界处本来就常常压着一刀（Pass B 就是按变化聚的类），
    把它算进来会让每一拍都凭空多出一个镜头。edge 取 0.15s——与抽帧脚本自己对切点
    去重用的窗口同宽，两边对「同一刀」的容差保持一致。
    """
    if not cut_points:
        return []
    lo, hi = sorted((_num(start), _num(end)))
    return [t for t in cut_points if lo + edge < t < hi - edge]


def attach_shot_cuts(beats_doc, overview):
    """给每一拍挂上 `observed_cuts` 与 `observed_shot_count`。派生数据，每次都重算。

    重算而不是「缺了才补」，理由与 attach_coverage_frames 同源：用户在卡点上拆拍/并拍
    改的就是时间窗，留着上一版的镜头数等于让人按**别的拍窗**的剪辑节奏做判断。

    二创变体原样跳过：变体的时间窗继承自原片，但它自己的目录里没有 overview
    （`reference_frames` 那条线，见 is_variant_doc），按空切点重算只会把继承下来的
    镜头数一路抹成 1 —— 与 shot_scale / camera_move 被列为「节奏骨架，原样继承」
    是同一条纪律。
    """
    beats = beats_doc.get('beats') or []
    if is_variant_doc(beats_doc):
        return beats_doc
    cut_points = overview_cut_points(overview)
    for beat in beats:
        if not isinstance(beat, dict):
            continue
        if not cut_points:
            # 未知与「一镜到底」必须分得开：没有切点数据时把字段清掉，让下游据此降级，
            # 而不是留下一个看起来很确定的 1。
            beat.pop('observed_cuts', None)
            beat.pop('observed_shot_count', None)
            beat.pop('observed_shot_seconds', None)
            continue
        cuts = observed_cuts_for_window(cut_points, beat.get('start'), beat.get('end'))
        beat['observed_cuts'] = cuts
        beat['observed_shot_count'] = len(cuts) + 1
        # 每镜多长。镜头**数**不能直接跨拍长比较：原片一拍平均三秒半，交付一拍是固定
        # 片长（8 秒），"原片这拍是一镜"与"我们切了两刀"说的根本不是同一个节奏
        # ——按每镜时长比才比得上（实测一条 77 秒片：原片 0.259 刀/秒，交付三镜
        # 0.25 刀/秒，几乎一致；真正对不上的是原片把四镜压进三秒半的那种快切拍）。
        span = abs(_num(beat.get('end')) - _num(beat.get('start')))
        beat['observed_shot_seconds'] = (round(span / float(len(cuts) + 1), 2)
                                         if span > 0 else None)
    return beats_doc


def shot_windows_for_beat(beat):
    """按 `observed_cuts` 把这一拍的拍窗切成逐个镜头窗，返回 [(start, end), …]。

    没有切点数据时返回 []（**不是**「整拍算一镜」）：未知与一镜到底必须分得开，
    与 attach_shot_cuts 同一条纪律。
    """
    if not isinstance(beat, dict) or 'observed_cuts' not in beat:
        return []
    lo, hi = sorted((_num(beat.get('start')), _num(beat.get('end'))))
    if not (hi > lo):
        return []
    marks = [lo] + [t for t in (beat.get('observed_cuts') or []) if lo < t < hi] + [hi]
    return [(marks[i], marks[i + 1]) for i in range(len(marks) - 1)]


def attach_shot_scales(beats_doc, overview, facts=None, job_dir=None):
    """给每一拍挂上 `observed_shot_scales`：这一拍**逐个镜头**的景别。派生数据，每次重算。

    为什么需要它：景别此前只有逐拍一个读数，而原片一拍本来就是几个镜头。多镜头交付线的
    镜头梯拿一个景别去排三到四镜，中间那两个插入镜的景别只能写死——「原片是远景切特写再
    切中景」这件事整条链路一个字都接不住。有了逐帧 shot_scale（Pass A v7）与切点
    （observed_cuts），每个镜头窗各投一次票，序列就是量出来的。

    与 attach_shot_cuts / attach_coverage_frames 同一条纪律：读不到就把字段清掉，
    不留一个看起来很确定的默认值。二创变体原样跳过（它自己目录下没有 overview）。
    """
    beats = beats_doc.get('beats') or []
    if is_variant_doc(beats_doc):
        return beats_doc
    by_frame = _frame_facts_by_name(beats_doc, overview, facts, job_dir)
    timeline = _review_frame_timeline(overview or {})
    for beat in beats:
        if not isinstance(beat, dict):
            continue
        windows = shot_windows_for_beat(beat) if (by_frame and timeline) else []
        scales = []
        for lo, hi in windows:
            votes = {}
            for ts, name in timeline:
                if not (lo - 1e-6 <= ts <= hi + 1e-6):
                    continue
                scale = str((by_frame.get(name) or {}).get('shot_scale') or '').strip()
                if scale in SHOT_SCALES:
                    votes[scale] = votes.get(scale, 0) + 1
            # 这一镜一张有读数的帧都没有时写空串占位——位置必须留着，否则序列与镜头
            # 一一对应的关系就断了，下游只能按下标硬贴，贴错比不贴更坏。
            scales.append(max(votes.items(), key=lambda kv: kv[1])[0] if votes else '')
        if any(scales):
            beat['observed_shot_scales'] = scales
        else:
            beat.pop('observed_shot_scales', None)
    return beats_doc


def _frame_facts_by_name(beats_doc, overview, facts=None, job_dir=None):
    """{帧文件名: 帧事实}。读不到返回 {}。

    facts 的三种到达形态都吃：调用方直接传进来的 list、frame_facts.json 那个
    {'facts': [...]} 信封、以及只给了 job_dir 时自己去盘上读那一份（存量任务走这支：
    读一次状态就补上序列，不用重跑 Pass A）。
    """
    if facts is None:
        facts = (beats_doc or {}).get('facts') or (overview or {}).get('facts')
    if facts is None and job_dir:
        path = os.path.join(job_dir, _FRAME_FACTS_FILENAME)
        if os.path.exists(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    facts = json.load(f)
            except Exception:
                facts = None
    if isinstance(facts, dict):
        facts = facts.get('facts') or []
    if not isinstance(facts, list):
        return {}
    out = {}
    for row in facts:
        if isinstance(row, dict) and row.get('frame'):
            out[os.path.basename(str(row['frame']))] = row
    return out


def observed_shot_stats(beats_doc):
    """整条阶梯的镜头节奏概览：给卡点上那行提示与合成前的偏差告警用。

    返回 None 表示这条 job 没有切点数据（老 job / 变体 / 抽帧异常），调用方据此
    什么都不显示——显示一个「全部一镜」比不显示更误导。
    """
    beats = [b for b in (beats_doc.get('beats') or [])
             if isinstance(b, dict) and isinstance(b.get('observed_shot_count'), int)]
    if not beats:
        return None
    counts = [b['observed_shot_count'] for b in beats]
    lengths = [b['observed_shot_seconds'] for b in beats
               if isinstance(b.get('observed_shot_seconds'), (int, float))]
    span = sum(abs(_num(b.get('end')) - _num(b.get('start'))) for b in beats)
    cuts = sum(len(b.get('observed_cuts') or []) for b in beats)
    return {
        'beats': len(counts),
        'cuts': cuts,
        'single_shot_beats': sum(1 for c in counts if c <= 1),
        'max_shots': max(counts),
        'avg_shots': round(sum(counts) / float(len(counts)), 2),
        # 每秒切点率与平均镜长才是能跨拍长比较的两个数（见 attach_shot_cuts 的说明）。
        'cuts_per_second': round(cuts / span, 3) if span > 0 else None,
        'avg_shot_seconds': round(sum(lengths) / float(len(lengths)), 2) if lengths else None,
    }


def ensure_three_evidence_frames(beats_doc, overview):
    """确保每一拍都有且仅有三张代表性证据帧（Triad: 1. 起始帧, 2. 施工峰值帧, 3. 交付结果帧）。

    如果大模型返回少于 3 张（例如只返回了起止 2 张），从拍窗 [start, end] 内部的抽帧时间轴
    及绑定的 change_events 里自动补齐峰值作业帧（Peak Action），保证每拍都有严格的三态闭环。
    """
    if not isinstance(beats_doc, dict):
        return beats_doc
    timeline = _review_frame_timeline(overview or {})
    if not timeline:
        return beats_doc
    ts_by_name = {name: ts for ts, name in timeline}
    change_events = (overview or {}).get('change_events') or []
    events_by_id = {e.get('event_id'): e for e in change_events
                    if isinstance(e, dict) and e.get('event_id')}

    is_var = is_variant_doc(beats_doc)
    frame_key = 'reference_frames' if is_var else 'evidence_frames'

    for beat in beats_doc.get('beats') or []:
        if not isinstance(beat, dict):
            continue
        cur_frames = [f for f in (beat.get(frame_key) or [])
                      if isinstance(f, str) and f in ts_by_name]
        lo = _num(beat.get('start'))
        hi = _num(beat.get('end'))
        if lo > hi:
            lo, hi = hi, lo
        inside = [row for row in timeline if lo - 1e-6 <= row[0] <= hi + 1e-6]
        if not inside:
            if not cur_frames and timeline:
                nearest = min(timeline, key=lambda row: min(abs(row[0] - lo), abs(row[0] - hi)))
                beat[frame_key] = [nearest[1]]
            continue

        if len(inside) <= 3:
            beat[frame_key] = [row[1] for row in inside]
            continue

        # If already exactly 3 distinct frames and all within window [lo-0.5, hi+0.5], keep them
        if len(cur_frames) == 3 and len(set(cur_frames)) == 3:
            all_inside = all(lo - 0.5 <= ts_by_name[f] <= hi + 0.5 for f in cur_frames)
            if all_inside:
                cur_frames.sort(key=lambda f: ts_by_name.get(f, 0.0))
                beat[frame_key] = cur_frames
                continue

        # 1. Start frame: closest to lo
        p_start_row = min(inside, key=lambda r: abs(r[0] - lo))
        p_start = (cur_frames[0]
                   if (cur_frames and abs(ts_by_name[cur_frames[0]] - lo) <= 1.0)
                   else p_start_row[1])

        # 2. End frame: closest to hi
        p_end_row = min(inside, key=lambda r: abs(r[0] - hi))
        p_end = (cur_frames[-1]
                 if (len(cur_frames) >= 2 and abs(ts_by_name[cur_frames[-1]] - hi) <= 1.0 and cur_frames[-1] != p_start)
                 else p_end_row[1])

        # 3. Peak/Mid frame
        p_peak = None
        bound_ids = beat.get('source_event_ids') or []
        for eid in bound_ids:
            ev = events_by_id.get(eid)
            if not ev:
                continue
            triad = ev.get('triad_frames') or {}
            cand = triad.get('action_peak')
            if cand and cand in ts_by_name and cand not in (p_start, p_end) and any(r[1] == cand for r in inside):
                p_peak = cand
                break
            ev_frames = ev.get('evidence_frames') or []
            if (len(ev_frames) >= 2 and ev_frames[1] in ts_by_name
                    and ev_frames[1] not in (p_start, p_end)
                    and any(r[1] == ev_frames[1] for r in inside)):
                p_peak = ev_frames[1]
                break
            if ev.get('peak') is not None:
                p_ts = _num(ev.get('peak'))
                cand_row = min((r for r in inside if r[1] not in (p_start, p_end)),
                               key=lambda r: abs(r[0] - p_ts), default=None)
                if cand_row:
                    p_peak = cand_row[1]
                    break

        if not p_peak:
            mid_target = lo + (hi - lo) / 2.0
            avail = [r for r in inside if r[1] not in (p_start, p_end)]
            if avail:
                p_peak_row = min(avail, key=lambda r: abs(r[0] - mid_target))
                p_peak = p_peak_row[1]

        chosen = [p_start]
        if p_peak and p_peak not in chosen:
            chosen.append(p_peak)
        if p_end and p_end not in chosen:
            chosen.append(p_end)

        for target in (lo, (lo + hi) / 2.0, hi):
            if len(chosen) >= 3:
                break
            avail = [r for r in inside if r[1] not in chosen]
            if avail:
                best = min(avail, key=lambda r: abs(r[0] - target))
                chosen.append(best[1])

        chosen.sort(key=lambda f: ts_by_name.get(f, 0.0))
        beat[frame_key] = chosen

    return beats_doc


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


# ── 单拍内容体检 ─────────────────────────────────────────────────────────────
#
# 这一组判据全是 warn，一条 error 都不出。理由与 `thin_details` 那条同源：它们是
# **质量**下限不是**契约**下限，判成硬伤会让所有存量阶梯在合成门口集体判死，而它们
# 并没有变坏。另一条纪律是**逐条聚合**——同一种毛病十四拍各报一条，等于把人工卡点
# 变成一面红墙，用户读第三条就开始整片忽略。每种毛病只出一条，把拍号列进去。

# 施工产物词。macro_environment 只写「这地方本来长什么样」，出现这些词多半是把
# 本拍干出来的东西写进了大环境（那份配额整条片子只有一次）。
_WORK_PRODUCT_CUES = re.compile(
    r'\b(excavat\w*|dug|dig|trench\w*|install\w*|built|build\w*|construct\w*|framed|framing|'
    r'poured|laid|fitted|fastened|boarded|clad|painted|plaster\w*|insulat\w*|assembled|'
    r'mounted|erected|demolish\w*|cleared|stacked)\b'
    r'|挖|砌|铺|装|建|浇|封|刷|拆|码放', re.I)

# 痕迹词。一条 persistent_trace 至少要点名「留下的是什么」。
_TRACE_MARK_CUES = re.compile(
    r'\b(mark\w*|scar\w*|stain\w*|dust|debris|shaving\w*|sawdust|scratch\w*|scuff\w*|'
    r'print\w*|striation\w*|residue|seam\w*|dent\w*|track\w*|rut\w*|gouge\w*|imprint\w*|'
    r'crumb\w*|smear\w*|splatter\w*|drip\w*|offcut\w*|chip\w*|groove\w*|indentation\w*|'
    r'head\w*|hole\w*|line\w*|edge\w*)\b'
    r'|痕|印|屑|渍|沫|坑洼|划|斑', re.I)

# 静态站位的措辞。cast_action 要写「从什么姿态动到什么姿态」，写成这些词就是在写
# 「他们此刻在哪」——下游把它原样写进每一帧的图，逐帧姿态一致，人偶就不会动了。
_STATIC_CAST_RE = re.compile(
    r'\b(remain\w*|stay\w*|unchanged|unmoved|motionless|static|still (?:stand|sit|seat|kneel|'
    r'crouch|watch|observ)\w*|where they (?:were|are)|same (?:position|spot|place|pose|stance)|'
    r'in place|as before|hold(?:ing)? (?:their )?(?:position|pose|stance))\b'
    r'|保持原样|站位不变|不变|原地|一动不动|纹丝不动', re.I)

# 量词。状态字段要写「完成到哪儿」，不是「看起来怎样」。
_QUANTITY_CUES = re.compile(
    r'\d|\bhalf\b|\bthird\b|\bquarter\b|\bfull\w*\b|\bentire\w*\b|\bwhole\b|\bevery\b|'
    r'\ball\b|\bnone\b|\bflush\b|\blevel with\b|\bedge[- ]to[- ]edge\b|\bwall[- ]to[- ]wall\b|'
    r'\bpercent\b|%|\bmetre\w*\b|\bmeter\w*\b|\bmm\b|\bcm\b|\bbay\w*\b|\bcourse\w*\b|'
    r'\bup to\b|\bdown to\b|\bfrom .{1,20} to\b|'
    # 写成词的数与尺度。「悬空一个车身高」是量，跟「4.5 米」一样算数。
    r'\b(one|two|three|four|five|six|seven|eight|nine|ten)\b|'
    r'\b(height|depth|deep|thick\w*|span|clearance|gap)\b'
    r'|全|整|半|三分之|四分之|齐平|一半|每一|所有|米|厘米|成|高|深|厚', re.I)

# 位置锚。visible_details 每条要说清「它在画面的哪儿」。这里不能复用 _content_words，
# 它的停用词表把 left/right/top 这些方位词当虚词滤掉了。
_POSITION_CUES = re.compile(
    r'\b(left|right|top|bottom|upper|lower|middle|centre|center|front|back|rear|near|far|'
    r'foreground|background|overhead|underneath|beneath|above|below|along|across|behind|'
    r'inside|outside|beside|around|at the|on the|in the|against the)\b'
    r'|左|右|上|下|顶|底|中|前|后|侧|旁|里|外|沿|周围', re.I)

# 主导工序里不该出现的连接词。出现它 = 写成了带宾语的整句而不是工序词。
_OPERATION_CLAUSE_CUES = re.compile(r'\b(into|onto|through|across|with|from|over|under|while|and)\b', re.I)

# 环境物概念组。同一件东西在大环境、细节、痕迹三栏各换一种说法写一遍，是实词交集
# 抓不到的（"golden orange beech and oak leaf litter" 与 "golden yellow foliage"
# 只共用一个 golden，Jaccard 连 0.06 都不到），但它确实就是同一件东西。所以这里不判
# 措辞判概念，且只收**本来就该待在大环境栏里**的那几类——土、石、木这些既是环境也是
# 施工对象的词一律不收，收了会把「坑壁的分层土质」这种正当细节误判成重复。
_AMBIENT_CONCEPTS = {
    'leaves': ('leaf', 'leaves', 'litter', 'foliage', '落叶', '树叶', '枯叶'),
    'canopy': ('canopy', 'treetop', 'treetops', 'branches', 'boughs', '树冠', '枝叶'),
    'sky': ('sky', 'skies', 'cloud', 'clouds', 'overcast', '天空', '云'),
    'undergrowth': ('moss', 'mossy', 'grass', 'weeds', 'undergrowth', 'brambles', '青苔', '苔藓', '杂草'),
    'snow': ('snow', 'snowy', 'frost', 'ice', '积雪', '霜'),
    'water': ('rain', 'puddle', 'puddles', 'stream', 'drizzle', '雨', '水洼'),
    'daylight': ('sunlight', 'sunshine', 'daylight', 'shadows', 'dappled', '阳光', '日光'),
}


def _ambient_concepts(text):
    """一段文本命中的环境物概念。"""
    low = str(text or '').lower()
    tokens = set(re.split(r'[^a-zA-Z一-鿿]+', low))
    hits = set()
    for concept, words in _AMBIENT_CONCEPTS.items():
        for word in words:
            if (word in tokens) if word.isascii() else (word in low):
                hits.add(concept)
                break
    return hits


_CRAFT_FIELD_LABELS = (
    ('tool', '主导工具'), ('sfx', '本拍声音'), ('shot_scale', '景别'),
    ('camera_angle', '拍摄角度'), ('camera_bearing', '机位方位'),
    ('lens_feel', '焦段感'), ('subject_placement', '主体构图'),
    ('camera_move', '运镜'), ('time_treatment', '时间处理'), ('worker_count', '工人数'),
    ('light_state', '光照时段'), ('material_flow', '物料去向'),
)


def _is_transition_beat(beat):
    """过门/揭示/硬切拍。它们不干活，工序与物料类判据一律不适用。"""
    op = str(beat.get('operation') or '').lower()
    stage = str(beat.get('stage') or '').lower()
    return (op in ('threshold', 'reward', 'reframe')
            or stage in ('transition', 'threshold', 'reveal')
            or bool(beat.get('bridge_stage'))
            or bool(beat.get('hard_cut')))


def _text_overlap(a, b):
    """两段文本的实词 Jaccard。用来判「同一句话写了两遍」。"""
    wa, wb = _content_words(a), _content_words(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / float(len(wa | wb))


def _scan_beat_craft(beats):
    """单拍内容体检的**判据层**：返回 (症状码 → 拍号列表, 制作字段名 → 拍号列表)。

    与 `_validate_beat_craft` 的分工是判据 vs 措辞：那边把这里的结果聚合成人话 warn 挂到
    人工卡点上，`refine_beat_craft` 按这里的拍号决定哪几拍要送模型精修。两边共用同一份
    判据，绝不各写一份——判据一分叉，界面上报的毛病和精修实际修的毛病就不是同一件事，
    而用户判断「修好了没有」靠的正是界面上那几条。
    """
    macro_work, dup_detail, thin_trace = [], [], []
    vague_extent, echoed_state, wordy_op, no_anchor = [], [], [], []
    no_insert, no_cast, static_cast = [], [], []
    missing = {key: [] for key, _label in _CRAFT_FIELD_LABELS}

    for beat in beats:
        bid = beat.get('id')
        is_transition = _is_transition_beat(beat)

        macro = [str(x).strip() for x in (beat.get('macro_environment') or []) if str(x).strip()]
        details = [str(x).strip() for x in (beat.get('visible_details') or []) if str(x).strip()]
        traces = [str(x).strip() for x in (beat.get('persistent_traces') or []) if str(x).strip()]

        # 一① 大环境里混进了本拍施工产物
        if any(_WORK_PRODUCT_CUES.search(item) for item in macro):
            macro_work.append(bid)

        # 一② 可见细节复述了大环境/遗留痕迹已经写过的东西。两道判据并联：措辞几乎照抄
        # 的走实词交集，换了说法的走环境物概念。
        context = macro + traces
        context_ambient = set()
        for other in context:
            context_ambient |= _ambient_concepts(other)
        for item in details:
            if (any(_text_overlap(item, other) >= 0.34 for other in context)
                    or (_ambient_concepts(item) & context_ambient)):
                dup_detail.append(bid)
                break

        # 一③ 遗留痕迹没点名「留下的是什么」——多半是把原本就在的环境物写了进来
        if traces and not all(_TRACE_MARK_CUES.search(item) for item in traces):
            thin_trace.append(bid)

        # 二⓪ 画面里有人（或人偶），却没写他们的身体语言。延时片里人是唯一的活物，
        # 这一栏空着交付出来就是一动不动的塑料小人——2026-08-23 用户实测反馈的正是它。
        # 只对「画面里确实有人」的拍报：清场帧本来就没人可写。
        if (beat.get('workers_present') and not _is_transition_beat(beat)
                and not str(beat.get('cast_action') or '').strip()):
            no_cast.append(bid)

        # 二⓪b 写了，但写的是「位置」不是「动作」。2026-08-23 实测那条微缩片里，
        # 二十拍的 cast_action 多数长这样：'two miniature figurines remain standing at the
        # upper-left perimeter'——它交代的是人偶此刻站在哪，不是它从什么姿态动到什么姿态。
        # 下游把这句原样写进 IMAGE，逐帧姿态自然完全一致，交付出来就是不动的小人。
        cast_text = str(beat.get('cast_action') or '').strip()
        if cast_text and _STATIC_CAST_RE.search(cast_text):
            static_cast.append(bid)

        # 二① 原片这一拍切过刀，却没说那个插入镜拍的是什么。只对真的切过刀的拍报——
        # 一镜到底的拍本来就没有插入镜可抄，按缺字段报会把整片标红（实测一条 77 秒片
        # 22 拍里有 7 拍是一镜）。
        if (isinstance(beat.get('observed_shot_count'), int)
                and beat['observed_shot_count'] >= 2
                and not str(beat.get('insert_subject') or '').strip()):
            no_insert.append(bid)

        # 三① 状态字段没有量
        states = [str(beat.get(k) or '').strip() for k in ('state_before', 'state_after')]
        if any(text and not _QUANTITY_CUES.search(text) for text in states):
            vague_extent.append(bid)

        # 三② 可见结果与结束状态是同一句话
        if _text_overlap(beat.get('visible_result'), beat.get('state_after')) >= 0.6:
            echoed_state.append(bid)

        # 三③ 主导工序写成了带宾语的整句
        op = str(beat.get('operation') or '').strip()
        if op and not is_transition and (len(op.split()) > 4 or _OPERATION_CLAUSE_CUES.search(op)):
            wordy_op.append(bid)

        # 三④ 可见细节缺位置锚
        if details and sum(1 for item in details if not _POSITION_CUES.search(item)) > len(details) // 2:
            no_anchor.append(bid)

        # 二 制作字段缺不缺。过门拍不干活，工具/物料两栏本就该空。
        for key, _label in _CRAFT_FIELD_LABELS:
            if is_transition and key in ('tool', 'material_flow'):
                continue
            value = beat.get(key)
            if key == 'worker_count':
                filled = isinstance(value, int)
            elif isinstance(value, str):
                filled = bool(value.strip())
            else:
                filled = bool(value)
            if not filled:
                missing[key].append(bid)

    buckets = {
        'macro_env_work_product': macro_work,
        'detail_repeats_context': dup_detail,
        'trace_without_mark': thin_trace,
        'state_without_quantity': vague_extent,
        'result_echoes_state': echoed_state,
        'operation_not_a_token': wordy_op,
        'detail_without_position': no_anchor,
        'missing_insert_subject': no_insert,
        'missing_cast_action': no_cast,
        'static_cast_action': static_cast,
    }
    return buckets, missing


def _validate_beat_craft(beats):
    """单拍内容体检：写串栏的、写重复的、写含糊的、以及七个制作字段缺不缺。

    只出 warn，且每种毛病聚合成一条。判据都是文本启发式——它们指得出「这里值得再看
    一眼」，指不出「这里一定错了」，所以绝不该有权力挡住合成。判据本身在
    `_scan_beat_craft`，这里只负责把它翻成人话。
    """
    out = []
    buckets, missing = _scan_beat_craft(beats)
    macro_work = buckets['macro_env_work_product']
    dup_detail = buckets['detail_repeats_context']
    thin_trace = buckets['trace_without_mark']
    vague_extent = buckets['state_without_quantity']
    echoed_state = buckets['result_echoes_state']
    wordy_op = buckets['operation_not_a_token']
    no_anchor = buckets['detail_without_position']
    no_insert = buckets['missing_insert_subject']
    no_cast = buckets['missing_cast_action']
    static_cast = buckets['static_cast_action']

    def _ids(items):
        return '、'.join(x for x in dict.fromkeys(items) if x)

    if macro_work:
        out.append(_warn('macro_env_work_product',
                         f'{_ids(macro_work)} 的「大环境识别项」里写了本拍的施工产物（挖出来的坑、'
                         f'砌起来的墙…）。这一栏只写这地方本来长什么样：地貌、地质、气候光照、'
                         f'空间包络。施工产物属于起始/结束状态——整条片子只有首拍和过门拍能填这一栏，'
                         f'分一条给已经写过的结果，就少一条真环境。'))
    if dup_detail:
        out.append(_warn('detail_repeats_context',
                         f'{_ids(dup_detail)} 的「细节识别项」里有一条在复述大环境或遗留痕迹已经写过的'
                         f'东西。这一栏是原片长相在提示词里唯一的落脚点，配额只有 3~6 条，'
                         f'重复一条就少一条真信息——换成这一拍主体身上还没写过的特征。'))
    if thin_trace:
        out.append(_warn('trace_without_mark',
                         f'{_ids(thin_trace)} 的「遗留痕迹」里有一条没点名留下的是什么痕迹（斗痕、'
                         f'压印、木屑、螺钉头…）。原本就在的环境物（落叶、青苔）不是这一拍留下的痕迹；'
                         f'整栏会被拼成一个串下发，混进环境词会稀释真正的痕迹。'))
    if vague_extent:
        out.append(_warn('state_without_quantity',
                         f'{_ids(vague_extent)} 的起始/结束状态只写了「样子」没写「量」。'
                         f'完成范围要带一个量：比例、范围、齐平关系、高度差、几个开间都算'
                         f'（「车顶与地面齐平、车身全部入坑」是量，「校车在坑里」不是）。'))
    if echoed_state:
        out.append(_warn('result_echoes_state',
                         f'{_ids(echoed_state)} 的「可见结果」和「结束状态」几乎是同一句话。'
                         f'两栏走的是不同通路，分工写才有意义：可见结果＝这一下看见了什么'
                         f'（车体沉下去、吊索由紧转松），结束状态＝完成到哪儿（车顶与地面齐平）。'))
    if wordy_op:
        out.append(_warn('operation_not_a_token',
                         f'{_ids(wordy_op)} 的「主导工序」写成了带宾语的整句。合成器拿它做相位判定，'
                         f'长短语会把判定和中文翻译一起搞糊——写成 1~3 个词的里程碑工序词'
                         f'（吊装就位 / seat bus），宾语和过程留给可见动作那一栏。'))
    if no_anchor:
        out.append(_warn('detail_without_position',
                         f'{_ids(no_anchor)} 的「细节识别项」多数条目没说它在画面的哪儿。'
                         f'每条＝材料 + 颜色/质感/状态 + 位置；少了位置，图像模型会把它摆在'
                         f'自己顺手的地方，逐拍摆得还不一样。'))

    if no_cast:
        out.append(_warn('missing_cast_action',
                         f'{_ids(no_cast)} 的画面里有人（或人偶），但没写他们在这一拍的'
                         f'身体语言（姿态/朝向/视线/位移/手势）。这一栏空着，交付出来的人'
                         f'就是从头到尾一动不动的——延时片里人是唯一的活物，冻住它等于把'
                         f'片子做成静物展示。工序动作写在「可见动作」里，这一栏只写人本身。'))

    if static_cast:
        out.append(_warn('static_cast_action',
                         f'{_ids(static_cast)} 的「人物动作神情」写的是站位不是动作'
                         f'（remain / stay / unchanged / 还在原地这类写法）。这一栏要写的是'
                         f'**从上一拍的什么姿态、动到这一拍的什么姿态**；只写「他们还站在'
                         f'左上角」，下游会把这句原样写进每一帧的图，逐帧姿态一模一样，'
                         f'交付出来照样是一动不动的小人。真的几乎没动，就写那个最小的'
                         f'真实变化（转头、换重心、抬手指一下）。'))

    if no_insert:
        out.append(_warn('missing_insert_subject',
                         f'{_ids(no_insert)} 在原片里切过刀，但没写「插入镜拍的是什么」。'
                         f'多镜头交付会照排插入镜，这一栏空着就只能落回通用职责'
                         f'（工具接触点 / 持久痕迹）——那是这条片子里任何一拍都能写的话，'
                         f'不是**这一拍**的画面。'))

    # 缺字段聚成一条。七种各报一条、每条再列十四个拍号，人工卡点就成了一面红墙，
    # 读到第三条就开始整片忽略——真正的硬伤也一起被忽略掉。
    gaps = [(key, label) for key, label in _CRAFT_FIELD_LABELS if missing[key]]
    if gaps:
        lines = '；'.join(
            f'{label}（{_ids(missing[key])}）：{_CRAFT_FIELD_WHY[key].rstrip("。")}'
            for key, label in gaps)
        out.append(_warn('missing_craft_fields',
                         f'这几拍的拍摄与工艺栏还空着——{lines}'))
    return out


_CRAFT_FIELD_WHY = {
    'tool': '动作峰值上那件工具是「动作-工具-音效」三联里的一环，'
            '塞在可见动作那句话里合成器读不出来。',
    'sfx': '抽帧那边已经把音频瞬态算出来了，没有这一栏它就地蒸发，'
           '而交付口径是 ASMR 原声 60%、BGM 0%——空着就只能由模型自己编声音。',
    'shot_scale': '复刻线逐拍此前没有任何机位字段，空着等于这一拍的景别由合成器替原片决定。',
    'camera_move': '同上——运镜空着，原片的镜头语言这一格就没锁住。',
    'camera_angle': '俯仰角度（鸟瞰/俯拍/平视/仰拍/虫视/倾斜）是原片最像自己的一格：'
                    '同一道工序，贴地仰拍和站着俯看是两条完全不同的片子。空着就由合成器'
                    '默认成平视。',
    'camera_bearing': '方位（正面/侧面/背面）和俯仰是两根独立的轴，只标一根等于扔掉另一半。',
    'lens_feel': '焦段和景别是两件事：14mm 拍中景和 85mm 拍中景，透视、畸变、纵深完全两回事。'
                 '空着，机位句里那个「超广 16mm」就还是编的。',
    'subject_placement': '主体在画面哪儿、占多高——锚点的位置与占比此前从没在原片上量过，'
                         '全是合成器按主题编的。空着，角度标得再准，构图也还是另一条片子的。',
    'time_treatment': '空着，这一拍就会被默认写成延时加速——包括最后那个成品巡览拍，'
                      '而原片的巡览基本都是实时的。',
    'worker_count': '「有工人」那枚芯片只是个布尔，且它压根没进合成绑定，'
                    '十几拍下来人数会自己漂。0 是清场帧，空着是没标注，两者不一样。',
    'light_state': '延时片跨天跨时段，不逐拍声明光照，每拍的光就会自己跳。',
    'material_flow': '挖出来的土去哪了、耗掉的料从哪来。Material & Spoil Balance 规则'
                     '一直要求交代它，空着画面里就会出现凭空消失的渣土。',
}


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
    out.extend(_validate_event_coverage(beats, overview, variant=variant))
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
    out.extend(_validate_beat_craft(beats))
    out.extend(_validate_camera_angle_consistency(beats))
    return out


def _validate_camera_angle_consistency(beats):
    """同一个空间里的拍摄角度不一致时说出来。

    2026-08-25 之前这是一条**损失**告警：IMAGE 的机位句按空间发一句，一个空间只落得下
    一个角度，少数派那几拍的图会按多数派出。现在机位按 (空间, 角度, 方位, 焦段) 分组
    发句（`pp.observed_camera_setups`），少数派也拿得到自己的那一句，损失没有了。

    这条留着，是因为它换了一个理由：每多一个机位就多一把独立的几何锁。原片真换了机位，
    那本来就该是两把；**读错了**角度的那一拍，现在不再是「图按多数派出」这种安静的降级，
    而是图真的会凭空换一次机位——跨帧一致性从那一拍断开。所以仍然要在人工卡点上摊开，
    只是要用户核的东西反过来了：不是「要不要拆空间」，而是「这几拍原片到底换没换机位」。
    """
    by_space = {}
    for position, beat in enumerate(beats or [], start=1):
        if not isinstance(beat, dict):
            continue
        angle = str(beat.get('camera_angle') or '').strip()
        bearing = str(beat.get('camera_bearing') or '').strip()
        if not angle and not bearing:
            continue
        space = str(beat.get('space') or '').strip() or '（未标空间）'
        bid = str(beat.get('id') or f'B{position:02d}')
        by_space.setdefault(space, []).append(
            (bid, (angle, bearing), camera_setup_verified(beat)))

    out = []
    for space, rows in by_space.items():
        counts = {}
        for _bid, pair, _ok in rows:
            counts[pair] = counts.get(pair, 0) + 1
        if len(counts) < 2:
            continue
        # 这个空间里每一拍都按帧复核过，且读数自复核以来没被改过——问题已经回答了：
        # 原片确实换了机位。继续把它摆在待确认清单里，就是让用户第二遍回答同一个问题。
        # 「复核过」是逐拍的指纹戳，不是一个全局开关，用户手改任何一拍都会让它自动失效
        # （见 `camera_setup_verified`），这条 warn 也就随之回来。
        if all(ok for _bid, _pair, ok in rows):
            continue
        dominant = max(counts.items(), key=lambda kv: kv[1])[0]
        odd = [bid for bid, pair, _ok in rows if pair != dominant]
        done = sum(1 for _bid, _pair, ok in rows if ok)
        _label = lambda pair: ' / '.join(
            x for x in (CAMERA_ANGLE_LABELS_ZH.get(pair[0], pair[0]),
                        CAMERA_BEARING_LABELS_ZH.get(pair[1], pair[1])) if x)
        out.append(_warn(
            'mixed_camera_angle',
            f'空间「{space}」里有两种以上拍摄角度：多数拍是{_label(dominant)}，'
            f'而 {"、".join(odd)} 标的是'
            f'{"；".join(sorted({_label(pair) for _bid, pair, _ok in rows if pair != dominant}))}。'
            f'这几拍会各自生成一句自己的机位声明并按各自的角度出图（见 '
            f'pp.observed_camera_setups）——原片真换了机位就是对的，不用改。'
            f'但每多一个机位就多一把独立的几何锁，原片其实没换机位（读错了）时，'
            f'那几拍的图会凭空换一次机位、跨帧一致性从这里断开。'
            + (f'本空间 {len(rows)} 拍里已有 {done} 拍按帧复核过，'
               f'按「🎥 按帧复核机位」把剩下的核完。' if done else
               '按「🎥 按帧复核机位」让 Pass A 的逐帧读数替你核一遍。')))
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


def _validate_event_coverage(beats, overview, variant=False):
    """每个 change_event 必须且只能被一拍认领。

    漏 = 原片里一次真实可见的变化被丢掉了；重 = 节拍窗口重叠。两者都会让阶梯与原片
    对不上，而这正是 1:1 复刻唯一要保证的东西。

    变体整档降级为 warn，理由与 `_validate_evidence_frames` 同源（见 `is_variant_doc`）：
    `overview` 是从源 job 复制过来的**原片**事件名册，而变体不再对原片的事实负责。变体
    一改拍数或时间窗就报 `event_unbound`，而这条错**任何文字改写都修不掉**——只有机械
    层的 reconcile 能碰它。判成 error 的后果是「AI 修复硬伤」每次都跑满 rework 轮次、
    每轮重写全表，最后照样剩着，这正是「越修越坏」最主要的那台永动机。
    """
    out = []
    flag = _warn if variant else _err
    all_ids = [e.get('event_id') for e in (overview.get('change_events') or []) if e.get('event_id')]
    claimed = {}
    for beat in beats:
        for eid in (beat.get('source_event_ids') or []):
            claimed.setdefault(eid, []).append(beat.get('id'))

    for eid in all_ids:
        holders = claimed.get(eid) or []
        if not holders:
            out.append(flag('event_unbound',
                             f'变化事件 {eid} 没有被任何一拍认领（原片里的真实变化被丢掉了）'
                             + ('。变体不对原片事实负责，这条仅供核对。' if variant else '')))
        elif len(holders) > 1:
            out.append(flag('event_double_bound',
                             f'变化事件 {eid} 被多拍同时认领：{"、".join(holders)}'))
    for eid, holders in claimed.items():
        if eid not in all_ids:
            out.append(flag('event_unknown',
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
    # 2. 所有 fixtures 拍均为太阳能（solar/photovoltaic）、电池（battery）、发电机、马灯/燃油/蜡烛、免布线/灯串、插头或非通电设备（如纯橱柜、水槽、木作门架）
    if 'fixtures' in stages and 'rough_in' not in stages:
        banned_list = banned_elements or []
        has_banned_wiring = any(
            re.search(r'wiring|electrical|conduit|cable|rough.?in|power\s*cable|electric|走线|布线|隐蔽工程|强电|弱电|暗管|暗线', str(x), re.I)
            for x in banned_list
        )
        if not has_banned_wiring:
            fixture_beats = [b for b in beats if b.get('stage') == 'fixtures']
            electrical_pattern = re.compile(
                r'\b(?:light(?:ing|s)?|lamps?|luminaires?|pendants?|sconces?|chandeliers?|downlights?|spotlights?|leds?|bulbs?|sockets?|outlets?|switch(?:es)?|appliances?|heaters?|wir(?:ing|e|es)|powered|electric(?:al)?|illumination|conduits?)\b|'
                r'灯|插座|开关|通电|照明|电器|吊灯|壁灯|筒灯|射灯|暗线|强电|弱电',
                re.I
            )
            standalone_pattern = re.compile(
                r'solar|photovoltaic|panel|battery|generator|off.?grid|cord|plug|'
                r'lantern|hurricane|kerosene|oil\s*lamp|candle|torch|fairy\s*light|string\s*light|micro.?led|usb|cordless|rechargeable|portable|gravity|unpowered|'
                r'太阳能|光伏|电池|发电机|插头|明线|便携|马灯|煤油灯|油灯|蜡烛|火把|灯串|彩灯|离网|重力|免布线|充电',
                re.I
            )
            all_exempt = True
            for fb in fixture_beats:
                desc = f"{fb.get('operation') or ''} {fb.get('visual_subject') or ''} {fb.get('visible_action') or ''} {' '.join(fb.get('visible_details') or [])}"
                is_electrical = bool(electrical_pattern.search(desc))
                if is_electrical and not standalone_pattern.search(desc):
                    all_exempt = False
                    break
            if not all_exempt:
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
Only along the requested mutation axes: visual_subject, visible_details, visible_action, visible_result, state_before, state_after, persistent_traces, operation, tool, sfx, material_flow, and the space NAMES (never their grouping).
- tool / sfx / material_flow must be re-derived for the NEW carrier and its materials: the tool that achieves the same milestone on the new carrier, the sounds that tool and that material actually make, and where the new material's waste goes. A steel hull does not sound like a plaster wall.
- shot_scale, camera_angle, camera_bearing, lens_feel, subject_placement, camera_move, time_treatment and worker_count are the rhythm skeleton, not the subject. Leave them exactly as given; never restage the camera — the angle a shot is taken from belongs to the film being reproduced, not to what is being built in it.

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
                               'persistent_traces', 'workers_present',
                               # 工具/音效/物料去向随载体变（巴士不用瓦刀，也不响瓦刀的声）；
                               # 景别、运镜、人数属于节奏骨架，原样继承，不发给模型。
                               'tool', 'sfx', 'material_flow')}
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
                       max_tokens=65536, timeout=240)
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
                    'state_before', 'state_after', 'persistent_traces', 'operation', 'space',
                    'tool', 'sfx', 'material_flow'):
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
- VALIDATION FAILURES: the exact mechanical validation errors that MUST be fixed (e.g. stage regressions, rough_in after covering/enclosure, power_chain_broken, package operations out of range, missing fields).

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
- FIXING POWER CHAIN & FIXTURES (power_chain_broken):
  - If a beat is flagged with power_chain_broken (fixtures/equipment installed without prior rough_in wiring):
    1. If the beat is pure carpentry / joinery / plumbing (e.g., cabinets, cupboards, sink, faucet, door, shelves), keep it clearly described as unpowered woodwork/fixtures or reclassify to surface/furnishing.
    2. If the beat installs standalone, battery, solar, lantern, or plug-in lighting, explicitly state its standalone nature (e.g., "battery-powered LED", "solar stake lights", "hanging kerosene lantern", "off-grid lamp").
    3. If the video did not film concealed in-wall electrical wiring, include "concealed electrical wiring" in the root "banned_elements" array.
- PRESERVE INTEGRITY:
  - Keep all beat `id`s intact in their existing order (B01, B02, ...).
  - Keep timestamps (`start`, `end`), `space`, and frame bindings (`evidence_frames` / `reference_frames`) unchanged.
  - Fix `package_operations` if out of range: ensure each beat has 1 to 3 concise, tightly coupled operations.
  - Fix `persistent_traces`: ensure at least 2 visible physical marks left on surfaces.
  - Keep descriptions faithful to the work shown, only adjusting wording where necessary to match the corrected stage or fix vague states.

OUTPUT:
Return ONE JSON object, no commentary, no code fences:
{
  "banned_elements": ["concealed electrical wiring", "..."],
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


def _merge_fixed_beats(beats_doc, data, allowed_ids=None):
    """把 AI 修复后的字段并回原 beats_doc，保护 timestamps、evidence_frames 等不被抹除。

    `allowed_ids` 是本轮准许改写的拍号白名单（见 `autofix_beats` 的纪律 2）。给了就
    严格按它挡：白名单外的拍即便模型回了内容也一律丢弃——报 2 条错重写 14 拍，正是
    「越修越坏」里新错误的来源。传 None 表示不设限（纯全局错的回落路径）。

    白名单存在时，**按序号取 patch 的那条回退路径也必须关掉**：模型被要求只回白名单
    里的那几拍，此时 `by_id[0]` 是 B07 而不是 B01，按序号配对会把 B07 的修复内容糊到
    B01 头上。
    """
    raw_list = data.get('beats') if isinstance(data, dict) else data
    if not isinstance(raw_list, list):
        return beats_doc
    allow = {str(x) for x in allowed_ids} if allowed_ids else None

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
        if allow is not None and str(src.get('id')) not in allow:
            out_beats.append(new)
            continue
        patch = by_id.get(src.get('id')) or (by_id.get(idx) if allow is None else None) or {}
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
            existing_banned = list(beats_doc.get('banned_elements') or [])
            for x in data['banned_elements']:
                item_str = str(x).strip()
                if item_str and item_str not in existing_banned:
                    existing_banned.append(item_str)
            beats_doc['banned_elements'] = existing_banned
    return beats_doc


def _heal_power_chain_mechanically(beats_doc):
    """确定性自愈供电链缺失问题：无 rough_in 拍且含有通电/灯具设备时，自动将隐蔽布线补入 banned_elements。"""
    stages = {b.get('stage') for b in (beats_doc.get('beats') or []) if isinstance(b, dict)}
    if 'fixtures' in stages and 'rough_in' not in stages:
        banned = list(beats_doc.get('banned_elements') or [])
        has_banned_wiring = any(
            re.search(r'wiring|electrical|conduit|cable|rough.?in|power\s*cable|electric|走线|布线|隐蔽工程|强电|弱电|暗管|暗线', str(x), re.I)
            for x in banned
        )
        if not has_banned_wiring:
            banned.append('concealed electrical wiring')
            beats_doc['banned_elements'] = banned


def _autofix_skeleton(beats_doc):
    """喂给修复模型的阶梯正文。每轮都要按当前文档重算——此前它在循环外算一次，
    第二轮拿着**第一轮之前**的阶梯配上**第一轮之后**的报错单下发，模型改的是一份
    已经不存在的稿子。"""
    return json.dumps([
        {k: b.get(k) for k in ('id', 'start', 'end', 'space', 'stage', 'operation',
                               'package_operations', 'visual_subject', 'visible_details',
                               'visible_action', 'visible_result', 'state_before',
                               'state_after', 'persistent_traces', 'workers_present', 'source_event_ids')
         if b.get(k) is not None}
        for b in (beats_doc.get('beats') or [])
    ], ensure_ascii=False, indent=2)


def _variant_context_block(beats_doc):
    """变体阶梯的四轴上下文。

    修复器此前看不到这一段：它拿到的只有施工文本和报错单，于是按通用装修常识改写措辞，
    每修一轮就把「极地峡湾 + 芬兰松木 + 白鲸」往「通用毛坯房改造」拉回一点。四轴取值是
    这条变体存在的**全部理由**，必须随稿下发并声明冻结。
    """
    if not is_variant_doc(beats_doc):
        return ''
    # 存量文档里 `mutation_axes` 也可能是一个轴名列表（早期写法）。
    # 直接 `.items()` 会把一条本来只是想修阶段逆行的变体炸在修复入口上。
    axes = beats_doc.get('mutation_axes')
    if isinstance(axes, dict):
        lines = [f'- {k}: {v}' for k, v in axes.items() if str(v or '').strip()]
    elif isinstance(axes, (list, tuple)):
        lines = [f'- {x}' for x in axes if str(x or '').strip()]
    else:
        lines = []
    sig = str(beats_doc.get('scene_signature') or '').strip()
    brief = str(beats_doc.get('mutation_brief') or '').strip()
    body = ['==================== VARIANT AXES (FROZEN - DO NOT REWRITE) ====================']
    if sig:
        body.append(f'scene_signature: {sig}')
    if lines:
        body.append('mutation_axes:')
        body.extend(lines)
    if brief:
        body.append(f'user brief: {brief}')
    body.append(
        'This ladder is a derived variant. The environment, material, function and hero-reveal '
        'vocabulary above is FROZEN: never swap it back to generic renovation wording, never '
        'substitute a different material or biome. Fix only the flagged structural errors and '
        'keep every axis noun exactly as written.')
    return '\n'.join(body) + '\n\n'


def autofix_beats(config, beats_doc, overview=None, on_progress=None, max_rework=2):
    """AI 定向修复节拍阶梯中的硬伤与违规项。

    针对校验器指出的 stage_regression, rough_in_after_enclosure, power_chain_broken,
    package_operations, event_double_bound 等违规，机械层优先自动调解事件覆盖冲突与供电链暗线豁免，
    剩余逻辑回喂给 LLM 做定向修正，修复后重新执行机械校验。返回 (fixed_beats_doc, fixed_errors_count)。

    三条纪律（2026-08-25，起因是「越修越坏」）：
      1. **不许变坏**：每一轮的产物都要重新校验，只有 error 数**严格减少**才被采纳；
         否则整轮丢弃、回到上一份最好的稿子。最坏情况是「没修动」，不再是「修坏了」。
         此前没有这道闸门：每按一次按钮，更差的版本就永久落盘一次，连按三次就是三层
         复合漂移，而返回值 `max(1, fixed_count)` 还保证外层永远以为修好了。
      2. **定向**：报错点名了哪几拍，就只允许改哪几拍。此前报 2 条错会重写全部 14 拍，
         本来干净的那 12 拍陪着一起被改写——新错误就是这么长出来的。
      3. **不与机械层对拆**：循环里的事件调解改用 `reconcile_unbound=False`。默认的
         `True` 第一件事是清空所有拍的 `source_event_ids` 再按时间窗重排，会把模型刚
         改好的认领当场作废，两边每轮互相覆盖。
    """
    overview = overview or {}

    raw_violations = validate_beats(beats_doc, overview)
    raw_errors = [v for v in raw_violations if v.get('level') == 'error']
    if not raw_errors:
        return beats_doc, 0

    # 1. 机械层确定性预修复：解决 event_double_bound (多拍认领) 与 event_unbound (漏认领)
    reconcile_event_coverage(beats_doc, overview)
    # 2. 机械层确定性预修复：解决 power_chain_broken (原片无布线拍时的暗线豁免)
    _heal_power_chain_mechanically(beats_doc)

    initial_violations = validate_beats(beats_doc, overview)
    initial_errors = [v for v in initial_violations if v.get('level') == 'error']
    if not initial_errors:
        beats_doc['validation'] = initial_violations
        return beats_doc, len(raw_errors)

    # 机械层的产物是这一轮的地板：确定性、必然不坏，后面每一轮都要跟它比。
    best_doc = copy.deepcopy(beats_doc)
    best_violations = list(initial_violations)
    best_errors = len(initial_errors)

    variant_block = _variant_context_block(beats_doc)
    parse_budget = _PARSE_RETRY_BUDGET
    attempt = 0

    while attempt <= max_rework:
        pp._raise_if_cancelled(on_progress)
        round_errors = [v for v in best_violations if v.get('level') == 'error']

        # 纪律 2：白名单 = 报错点名的拍。全局错（event_unbound 之类）不点名任何一拍，
        # 也不该授权重写任何一拍；若一条点名的都没有，才回落到全表（老行为）。
        allowed_ids = sorted({str(v.get('beat_id')) for v in round_errors if v.get('beat_id')})
        scope_line = (
            f'ONLY these beats may be modified: {", ".join(allowed_ids)}. '
            'Return ONLY those beats in the "beats" array. Every other beat is frozen - '
            'do not return it, do not rewrite it.\n'
            if allowed_ids else
            'Return the full beats array.\n')

        err_lines = [f'- [{v.get("level", "error").upper()}] ({v.get("code")}) '
                     f'{v.get("beat_id") or "GLOBAL"}: {v.get("message")}' for v in round_errors]
        user_prompt = (
            variant_block
            + '==================== CURRENT BEAT LADDER ====================\n'
            + _autofix_skeleton(best_doc) + '\n\n'
            '==================== VALIDATION FAILURES (FIX THESE) ====================\n'
            + '\n'.join(err_lines) + '\n\n'
            + f'==================== SCOPE ====================\n{scope_line}\n'
            'Please fix the errors above by correcting the stage classification, package_operations, '
            'persistent_traces, source_event_ids, or descriptions. Preserve the beat IDs and real construction observations.'
        )

        if on_progress:
            on_progress('replica_stage', {
                'stage': 'review_beats',
                'message': ('AI 正在分析硬伤并定向修复节拍阶梯…' if attempt == 0
                            else f'正在进行第 {attempt + 1} 轮定向修复…'),
            })

        raw = pp._chat(config, _AUTOFIX_SYSTEM, user_prompt, temperature=0.1,
                       max_tokens=65536, timeout=240)
        try:
            data = parse_json_reply(raw)
        except ValueError:
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

        # 纪律 1：本轮在**副本**上落地。没变好就整轮丢弃，best_doc 一个字不动。
        candidate = copy.deepcopy(best_doc)
        _merge_fixed_beats(candidate, data, allowed_ids=allowed_ids or None)
        normalize_beat_keys(candidate)
        _renumber_beats(candidate)
        normalize_beat_spaces(candidate)
        # 纪律 3：只解重复认领，不清空重排。
        reconcile_event_coverage(candidate, overview, reconcile_unbound=False)
        _heal_power_chain_mechanically(candidate)
        attach_coverage_frames(candidate, overview)
        attach_shot_cuts(candidate, overview)

        violations = validate_beats(candidate, overview)
        remaining = len([v for v in violations if v.get('level') == 'error'])
        if remaining < best_errors:
            best_doc, best_violations, best_errors = candidate, violations, remaining
            if not remaining:
                break
        elif on_progress:
            on_progress('replica_stage', {
                'stage': 'review_beats',
                'message': (f'第 {attempt + 1} 轮修复没有减少硬伤'
                            f'（{best_errors} → {remaining}），已丢弃本轮改动。'),
            })
        attempt += 1

    best_doc['validation'] = best_violations
    return best_doc, max(0, len(initial_errors) - best_errors)


# ── 工艺精修 ─────────────────────────────────────────────────────────────────
#
# 与 autofix 的分界线是 1:1：
#   autofix   修的是**硬伤**——阶段倒退、供电链断、工序数越界。它有权改 stage / space /
#             package_operations，因为那些字段本身就是被判死的东西。
#   refine    修的是**措辞**——同一件事说得够不够准。画面上发生了什么一个字不动：拍号、
#             时间窗、空间序列、施工阶段、工序包、认领的事件、证据帧全部只读。
#
# 之所以要单独一条路径：`autofix_beats` 第一件事就是「没有 error 就原样返回」，而工艺
# 体检出的全是 warn。一条 0 硬伤、8 条工艺 warn 的阶梯按下「AI 修复硬伤」，模型一次都
# 不会被调用，外层却照样重跑翻译、清掉合成产物、把 stage 退回卡点，最后弹一句「已解决
# 全部硬伤」——用户看到的是修完了，实际一个字没改。

# 本函数覆盖的症状码。共同点：改的是**怎么说**，不是**发生了什么**。
# 不在其中的三条是有意留给别人的：evidence_out_of_window 与 beat_too_short 动的是时间窗
# （autobalance / 人工的活），temporary_object_lingering 是「照实复刻 vs 产线规则」的取舍，
# 只能由人裁决——把它交给模型，模型只会把用户真实观察到的那块防护布删掉。
CRAFT_REFINE_CODES = (
    'macro_env_work_product', 'detail_repeats_context', 'trace_without_mark',
    'state_without_quantity', 'result_echoes_state', 'operation_not_a_token',
    'detail_without_position', 'missing_cast_action', 'static_cast_action',
    'missing_insert_subject', 'missing_craft_fields',
)

# 允许模型重写的字段：措辞层。
_CRAFT_REWRITE_FIELDS = (
    'visual_subject', 'visible_details', 'visible_action', 'visible_result',
    'state_before', 'state_after', 'persistent_traces', 'operation', 'macro_environment',
)

# 只补空、绝不覆盖已有值的字段。用户在卡点上手填过的，模型不得改写——他看着帧填的，
# 模型看着同一批帧只是再猜一遍。
_CRAFT_FILL_FIELDS = (
    'tool', 'sfx', 'shot_scale', 'camera_angle', 'camera_bearing', 'lens_feel',
    'subject_placement', 'camera_move', 'time_treatment',
    'worker_count', 'light_state', 'material_flow', 'cast_action', 'insert_subject',
)

# 每种症状给模型的定向指令。措辞与 `_validate_beat_craft` 报给用户的那几条同源，
# 但这里是给模型的祈使句，且必须是英文——beats 正文是英文，中英混着下发会把改写
# 结果也带成中英混排。
_CRAFT_ISSUE_BRIEFS = {
    'macro_env_work_product':
        "macro_environment currently contains this beat's own work product. That column is only "
        "for what this place looked like to begin with (terrain, geology, climate/light, spatial "
        "envelope). Move any built/dug/installed result out of it into state_before/state_after.",
    'detail_repeats_context':
        "One visible_details entry restates something already covered by macro_environment or "
        "persistent_traces. Replace that entry with a feature of THIS beat's subject that has not "
        "been written yet. Keep 3-6 entries; aim for 5-6.",
    'trace_without_mark':
        "persistent_traces must name the mark itself AND the surface it landed on (bucket scars in "
        "the trench wall, screw heads along the batten, sawdust on the deck). Pre-existing scenery "
        "(fallen leaves, moss, old stains) is NOT a trace left by this beat - drop it.",
    'state_without_quantity':
        "state_before / state_after must carry a QUANTITY, not just an appearance: proportion, "
        "extent, flush relationship, height difference, number of bays. 'roof tiles on the left two "
        "thirds, right third still bare battens' is a quantity; 'roof partly tiled' is not.",
    'result_echoes_state':
        "visible_result and state_after are nearly the same sentence. Split them: visible_result = "
        "what you SEE happen in this beat (the bus body sinking, the sling going slack); state_after "
        "= how far the work has progressed (roof flush with grade).",
    'operation_not_a_token':
        "operation must be a 1-3 word milestone token ('seat bus', 'board ceiling'), not a clause "
        "with an object. Move the object and the process into visible_action.",
    'detail_without_position':
        "Most visible_details entries do not say WHERE in the frame they are. Each entry = material "
        "+ colour/texture/state + position ('yellow fibreglass batts in the left wall bays').",
    'missing_cast_action':
        "There are living subjects in frame (workers, miniature figures, animals) but cast_action is "
        "empty. Write their body language APART from the work: posture, facing, gaze, movement, "
        "gesture. The trade action itself belongs in visible_action, not here.",
    'static_cast_action':
        "cast_action names where the living subjects ARE, not how they MOVED. Rewrite it as a "
        "change: the pose they held last beat -> the pose they hold now ('get up off the stone "
        "and turn to face the new wall, the one in red now half a step closer'). Never 'remain', "
        "'stay', 'unchanged', 'still standing where they were' - a cast frozen across the ladder "
        "is delivered as dolls that never move. If a subject barely moved, write the smallest "
        "real change: a head turn, a shift of weight, a hand raised to point.",
    'missing_insert_subject':
        "The source cut to a close-up inside this beat but insert_subject is empty. Name what that "
        "insert shot is on ('the tweezer tip pressing a roof tile', 'mortar squeezing out from under "
        "the block') - something specific to THIS beat, not a generic tool-contact shot.",
}

_CRAFT_FILL_BRIEFS = {
    'tool': "tool: the one geometric tool at the action peak (crane / jamb saw / rubber mallet / trowel).",
    'sfx': "sfx: the physical sounds audible in this beat, one source per entry, max 4.",
    'shot_scale': "shot_scale: one of extreme_wide | wide | medium | close | extreme_close.",
    'camera_move': ("camera_move: one of static | push_in | pull_out | pan | tilt | orbit | follow | "
                    "handheld | crane. Closed set - do not write free text."),
    'camera_angle': ("camera_angle: where the camera is VERTICALLY - one of bird_eye | high_angle | "
                     "eye_level | low_angle | worm_eye | dutch_angle. Read it off the frame's own "
                     "geometry (converging lines, which faces of objects are visible, where the "
                     "horizon sits), not off the work. Closed set - do not write free text."),
    'camera_bearing': ("camera_bearing: where the camera is HORIZONTALLY - one of front | "
                       "three_quarter | side | rear_three_quarter | back, i.e. which face of the "
                       "subject the lens looks at. Independent of camera_angle; declare both. "
                       "Closed set - do not write free text."),
    'lens_feel': ("lens_feel: how wide the lens is - one of ultra_wide | wide | normal | tele | "
                  "macro. Read it off the perspective (edge curvature and looming near objects vs a "
                  "compressed flattened background), not off how much of the scene is in shot. "
                  "Closed set - do not write free text."),
    'subject_placement': ("subject_placement: one short sentence - where the main subject sits "
                          "(horizontal position in thirds, vertical position), what fraction of "
                          "frame HEIGHT it fills, and where the horizon sits if visible. Fractions "
                          "in words, never digits or percent signs."),
    'time_treatment': ("time_treatment: how time runs in this beat - one of timelapse | real_time | "
                       "slow_motion. Judge it by how fast bodies and material move between this "
                       "beat's own frames. A final walk-through/reveal is almost always real_time. "
                       "Closed set - do not write free text."),
    'worker_count': "worker_count: integer count of people visible in this beat's frames.",
    'light_state': "light_state: lighting and time of day ('overcast midday, no cast shadows').",
    'material_flow': ("material_flow: where the spoil went and where the stock came from "
                      "('excavated soil piled on the trench's north lip')."),
}

_CRAFT_REFINE_SYSTEM = """You are a prompt craft editor for a shot-for-shot (1:1) replica of a time-lapse construction video.

A beat ladder has already been reverse-engineered from the source video and APPROVED. Your job is NOT to re-interpret the video. Your job is to say the SAME facts more precisely, so an image/video model renders them consistently.

=========== HARD 1:1 CONTRACT (violating this ruins the whole job) ===========
- You may NOT change what happens on screen. Same work, same order, same extent, same people, same space.
- You may NOT invent anything that is not visible in the attached evidence frames or already written in the beat.
- The following are READ-ONLY CONTEXT. Never emit them, never propose changes to them:
  id, start, end, space, stage, package_operations, workers_present, source_event_ids,
  evidence_frames, coverage_frames, observed_cuts, observed_shot_count, confidence.
- Do NOT add cinematic or quality vocabulary (cinematic, 8k, dramatic lighting, masterpiece,
  award-winning, hyperrealistic). Those change what the source looked like. This is a replica.
- Do NOT introduce anything named in BANNED ELEMENTS.
- Keep the existing language and register: plain declarative English, present tense, no marketing adjectives.
- If the frames do not let you fix an issue honestly, LEAVE THAT FIELD OUT of your reply. A field you
  cannot ground is better left as it was. Never pad, never guess.

=========== WHAT YOU MAY EDIT ===========
Rewrite (wording only, same facts):
  visual_subject, visible_details, visible_action, visible_result,
  state_before, state_after, persistent_traces, operation, macro_environment
Fill in ONLY if listed as missing below (never overwrite a value that is already there):
  tool, sfx, shot_scale, camera_angle, camera_bearing, lens_feel, subject_placement,
  camera_move, time_treatment, worker_count, light_state, material_flow, cast_action,
  insert_subject

=========== OUTPUT ===========
Return ONE JSON object, no commentary, no code fences. Include ONLY the fields you actually changed:
{"id": "<the beat id>", "<field>": <new value>, ...}
If nothing can be honestly improved, return {"id": "<the beat id>"}."""


def _resolve_frame_paths(frames_dir, names, limit=3):
    """证据帧文件名 → 存在的绝对路径。找不到的静默跳过（老 job 的帧可能已清理）。"""
    if not frames_dir:
        return []
    roots = [frames_dir,
             os.path.join(frames_dir, 'review_frames'),
             os.path.join(frames_dir, 'storyboard')]
    out = []
    for name in (names or []):
        base = os.path.basename(str(name or '').strip())
        if not base:
            continue
        for root in roots:
            path = os.path.join(root, base)
            if os.path.isfile(path):
                out.append(path)
                break
        if len(out) >= limit:
            break
    return out


def _craft_todo(beats):
    """哪几拍要精修，各自要修什么。返回 {beat_id: [症状码…]}，按拍号顺序。"""
    buckets, missing = _scan_beat_craft(beats)
    todo = {}
    for code, ids in buckets.items():
        for bid in ids:
            todo.setdefault(bid, [])
            if code not in todo[bid]:
                todo[bid].append(code)
    for field, ids in missing.items():
        for bid in ids:
            todo.setdefault(bid, [])
            tag = f'missing:{field}'
            if tag not in todo[bid]:
                todo[bid].append(tag)
    order = {b.get('id'): i for i, b in enumerate(beats)}
    return dict(sorted(todo.items(), key=lambda kv: order.get(kv[0], 1 << 30)))


def _merge_refined_beat(beat, patch):
    """把一拍的精修结果并回去。返回实际改动的字段名列表。

    两条纪律写死在这里，不指望模型自觉：
    - `_CRAFT_REWRITE_FIELDS` 之外的键一律丢弃（模型改 stage / start 的冲动是真实存在的）；
    - `_CRAFT_FILL_FIELDS` 只在原值为空时才写——用户在卡点上手填过的，模型不得覆盖。
    """
    if not isinstance(patch, dict):
        return []
    changed = []
    for key, value in patch.items():
        if key not in _CRAFT_REWRITE_FIELDS and key not in _CRAFT_FILL_FIELDS:
            continue
        if value in (None, '', [], {}):
            continue
        if key in _CRAFT_FILL_FIELDS:
            existing = beat.get(key)
            if isinstance(existing, str) and existing.strip():
                continue
            if isinstance(existing, (list, tuple, dict)) and len(existing):
                continue
            if isinstance(existing, int) and not isinstance(existing, bool):
                continue
        if key == 'worker_count':
            # 人数与 workers_present 是同一件事的两种写法，而 normalize_beat_craft_fields
            # 会拿人数去改布尔。模型把有人的拍写成 0，就等于凭空把这一拍变成清场帧、
            # 让它成为 IMAGE 锚点候选——那是画面层面的改动，不是措辞。对不上就不要。
            count = _coerce_count(value)
            if count is None:
                continue
            present = beat.get('workers_present')
            if present is True and count < 1:
                continue
            if present is False and count > 0:
                continue
            value = count
        if beat.get(key) == value:
            continue
        beat[key] = value
        changed.append(key)
        if isinstance(beat.get('zh'), dict):
            beat['zh'] = {k: v for k, v in beat['zh'].items() if k != key}
            if not beat['zh']:
                beat.pop('zh', None)
    return changed


def refine_beat_craft(config, beats_doc, overview=None, frames_dir=None, on_progress=None):
    """看着证据帧逐拍精修措辞。返回 (beats_doc, 改动的拍数, 未覆盖的 warn 码列表)。

    只送有毛病的那几拍，一拍一次调用：批量送会让模型把 B07 的帧记到 B08 头上，而
    「位置锚」「量」「插入镜拍的是什么」这三类恰恰全靠看对帧。

    收尾有一道回滚闸：精修后若**硬伤变多了**，整份原样退回。措辞层的改写不该产出新的
    结构性违规；一旦产出了，说明模型越界改了它不该碰的东西，这时候留下半份比全退回更糟。
    """
    beats = beats_doc.get('beats') or []
    if not beats:
        return beats_doc, 0, []

    todo = _craft_todo(beats)
    if not todo:
        return beats_doc, 0, []

    before_violations = validate_beats(beats_doc, overview or {})
    before_errors = len([v for v in before_violations if v.get('level') == 'error'])
    snapshot = copy.deepcopy(beats_doc)

    banned = [str(x).strip() for x in (beats_doc.get('banned_elements') or []) if str(x).strip()]
    signature = str(beats_doc.get('scene_signature') or '').strip()
    by_id = {b.get('id'): b for b in beats if isinstance(b, dict)}

    refined = 0
    total = len(todo)
    for seq, (bid, codes) in enumerate(todo.items(), 1):
        pp._raise_if_cancelled(on_progress)
        beat = by_id.get(bid)
        if not isinstance(beat, dict):
            continue

        briefs = []
        for code in codes:
            if code.startswith('missing:'):
                field = code.split(':', 1)[1]
                brief = _CRAFT_FILL_BRIEFS.get(field)
                if brief:
                    briefs.append(f'- MISSING FIELD -> {brief}')
            elif code in _CRAFT_ISSUE_BRIEFS:
                briefs.append(f'- {code}: {_CRAFT_ISSUE_BRIEFS[code]}')
        if not briefs:
            continue

        readonly = {k: beat.get(k) for k in
                    ('id', 'start', 'end', 'space', 'stage', 'package_operations',
                     'workers_present', 'observed_shot_count')
                    if beat.get(k) is not None}
        editable = {k: beat.get(k) for k in (_CRAFT_REWRITE_FIELDS + _CRAFT_FILL_FIELDS)
                    if beat.get(k) not in (None, '', [], {})}

        frames = _resolve_frame_paths(frames_dir, beat.get('evidence_frames')
                                      or beat.get('reference_frames'))
        user_text = (
            (f'SCENE SIGNATURE (true for the whole video): {signature}\n\n' if signature else '')
            + (f'BANNED ELEMENTS (never mention these): {", ".join(banned)}\n\n' if banned else '')
            + f'READ-ONLY CONTEXT for beat {bid} (never emit these):\n'
            + json.dumps(readonly, ensure_ascii=False, indent=1)
            + f'\n\nEDITABLE FIELDS as they stand now:\n'
            + json.dumps(editable, ensure_ascii=False, indent=1)
            + '\n\nISSUES TO FIX IN THIS BEAT:\n' + '\n'.join(briefs)
            + ('\n\nThe evidence frames for this beat are attached in chronological order. '
               'Ground every edit in what you can actually see in them.'
               if frames else
               '\n\nNO EVIDENCE FRAMES ARE AVAILABLE for this beat. Do NOT invent visual specifics '
               '(positions, quantities, insert subjects) you cannot derive from the text above - '
               'restructure only what is already written, and leave the rest out of your reply.')
        )

        if on_progress:
            on_progress('replica_stage', {
                'stage': 'review_beats',
                'message': f'工艺精修 {seq}/{total}：{bid}（{len(briefs)} 项）…',
                # 分子/分母单独给字段，不只写进文案：进度条要拿它算段内百分比。
                # 这一段是逐拍多模态调用，十几拍就是几分钟——没有它进度条全程不动。
                'done': seq - 1,
                'total': total,
            })

        try:
            if frames:
                raw = pp._multimodal_chat(config, _CRAFT_REFINE_SYSTEM, user_text, frames,
                                          max_tokens=4096, timeout=120)
            else:
                raw = pp._chat(config, _CRAFT_REFINE_SYSTEM, user_text,
                               temperature=0.2, max_tokens=4096, timeout=120)
            patch = parse_json_reply(raw)
        except pp.GenerationCancelled:
            raise
        except Exception as e:
            # 单拍失败不该拖垮整轮：19 拍里第 7 拍网络抖一下，前 6 拍的成果不能跟着丢。
            if on_progress:
                on_progress('replica_stage', {
                    'stage': 'review_beats',
                    'message': f'{bid} 精修失败，跳过：{e}',
                })
            continue

        if isinstance(patch, list):
            patch = next((x for x in patch if isinstance(x, dict)), None)
        if _merge_refined_beat(beat, patch or {}):
            refined += 1

    normalize_beat_craft_fields(beats_doc)

    after_violations = validate_beats(beats_doc, overview or {})
    after_errors = len([v for v in after_violations if v.get('level') == 'error'])
    if after_errors > before_errors:
        beats_doc.clear()
        beats_doc.update(snapshot)
        beats_doc['validation'] = before_violations
        raise CraftRefineRolledBack(
            f'工艺精修后硬伤从 {before_errors} 项涨到 {after_errors} 项，已整份回滚——'
            f'措辞层的改写不该产出新的结构性违规，说明模型越界改了不该碰的字段。')

    beats_doc['validation'] = after_violations
    uncovered = sorted({v.get('code') for v in after_violations
                        if v.get('level') != 'error' and v.get('code') not in CRAFT_REFINE_CODES})
    return beats_doc, refined, uncovered


# ── 机位复核 ─────────────────────────────────────────────────────────────────
#
# `mixed_camera_angle` 是唯一一条工艺精修够不着的待确认项，而它的杠杆比别的 warn 都大：
# `pp.observed_camera_setups` 的分组键是 (空间, 角度, 方位, 焦段, 景别)，所以一个读错的
# camera_angle 不是「这拍标签写歪了」，是凭空开出一个 SETUP_n——带一整套自己的几何锁句，
# 那一拍的图会真的按另一个机位出，跨帧一致性从那里断开。
#
# 它此前没有 AI 通路，理由是「原片真换了机位就是对的，模型没法从多数/少数这个统计事实
# 推出该往哪边改」。这条理由只对「再问一次模型」成立。真正的出路在产地上：逐拍的
# camera_angle 是 **Pass B** 写的，而 Pass B 是文本模型读摘要（`_facts_digest` 里那句
# `view=high_angle/three_quarter`）；真正看过帧的是 **Pass A**，逐帧多模态读数，而且早
# 就落盘在 frame_facts.json 里。这一栏在盘上一直有一份一手读数，从来没人拿它去校二手
# 转写——所以第一步根本不该是新调用，是对账。
#
# 于是这一趟分三层，先免费后花钱：
#   · 帧票与梯子一致     → 盖复核戳，这条 warn 不再打扰人（零调用）
#   · 帧票与梯子不一致   → Pass B 转写丢了东西，按帧票改回去（零调用）
#   · 帧票自己就分裂／这一拍根本没有 Pass A 读数 → 这才是「原片可能真换了机位」，
#     升级成一次逐拍多模态复核（要钱，且只在这一层要）
#
# 只复核**有冲突的空间**里的拍。没冲突的空间即使读错了，全空间读成同一个错角度也只是
# 少一把锁、不会在空间内部撕开——不值得为它冒一次改写的险。

# (字段, 闭集, 同义词表, 中文标签)。同义词表跟着一起带，是因为第三层拿到的是模型
# 自由文本，必须走 `_coerce_enum` 收进闭集——收不进就当没读出来，绝不留歪值。
_CAMERA_RECHECK_AXES = (
    ('camera_angle', CAMERA_ANGLES, _CAMERA_ANGLE_SYNONYMS, CAMERA_ANGLE_LABELS_ZH),
    ('camera_bearing', CAMERA_BEARINGS, _CAMERA_BEARING_SYNONYMS, CAMERA_BEARING_LABELS_ZH),
)


def camera_reading_stamp(beat):
    """这一拍机位读数的指纹：读数本身，**加上它所依据的那个拍窗**。

    拍窗进指纹，是因为 `autobalance_beats` 拆拍与合拍都走 `dict(b)`——复核戳会被原样
    复制进每一个新拍。读数没变、戳看着还有效，但它当初是在另一个窗上投出来的：一条 6s
    的拍拆成两条 3s，两半各自继承了一张只在合起来的窗上数过票的证明；合拍更糟，幸存的
    那一拍拿着一张只覆盖新窗一半的证明。窗进了指纹，任何拆合都让戳自动失效，"先平衡还是
    先复核"就不再是个需要用户记住的坑——顺序错了只会让这条 warn 回来，不会让它带着一张
    过期的证明沉默下去。
    """
    return '{}/{}@{:.2f}-{:.2f}'.format(
        str((beat or {}).get('camera_angle') or '').strip(),
        str((beat or {}).get('camera_bearing') or '').strip(),
        _num((beat or {}).get('start')), _num((beat or {}).get('end')))


def camera_setup_verified(beat):
    """这一拍是不是已按帧复核过，且读数自那以后没被改过。

    戳里存的是**当时那一对读数**，不是一个布尔。用户在卡点上手改了角度，指纹就对不上，
    戳自动失效——不需要任何一处记得去清它。一个布尔戳会在用户改完之后继续宣称「已按帧
    复核」，那比没有戳更坏。
    """
    mark = (beat or {}).get('camera_setup_verified')
    if not isinstance(mark, dict):
        return False
    return str(mark.get('reading') or '') == camera_reading_stamp(beat)


# 复核戳的失效面：读数被手改、拍窗被拆合，两者都让 `camera_reading_stamp` 对不上。
# 唯一不失效的是「什么都没动」——那正是戳该继续有效的情形。


def camera_conflict_spaces(beats):
    """哪些空间里落了不止一对 (角度, 方位)。口径与 `_validate_camera_angle_consistency`
    完全一致——那条 warn 报的是哪几个空间，这里复核的就是哪几个空间。"""
    by_space = {}
    for beat in (beats or []):
        if not isinstance(beat, dict):
            continue
        angle = str(beat.get('camera_angle') or '').strip()
        bearing = str(beat.get('camera_bearing') or '').strip()
        if not angle and not bearing:
            continue
        space = str(beat.get('space') or '').strip() or '（未标空间）'
        by_space.setdefault(space, set()).add((angle, bearing))
    return {space for space, pairs in by_space.items() if len(pairs) > 1}


def _beat_window_frames(beat, timeline):
    """这一拍窗内的送审帧名，按时间序。"""
    lo, hi = sorted((_num(beat.get('start')), _num(beat.get('end'))))
    if not (hi > lo):
        return []
    return [name for ts, name in timeline if lo - 1e-6 <= ts <= hi + 1e-6]


def _camera_axis_vote(names, by_frame, field, allowed):
    """一根轴上的帧票。返回 (胜出值或 None, 得票, 总票)。

    要求**严格过半**才算读出来了：2/1/1 这种三分票不是一个读数，是「这一拍里镜头本来
    就动过或者帧读不准」，那正是该升级去看图的情形，不该在这里硬投出一个赢家。
    """
    votes = {}
    for name in names:
        value = str((by_frame.get(name) or {}).get(field) or '').strip()
        if value in allowed:
            votes[value] = votes.get(value, 0) + 1
    total = sum(votes.values())
    if not total:
        return None, 0, 0
    top, count = sorted(votes.items(), key=lambda kv: (-kv[1], kv[0]))[0]
    if count * 2 <= total:
        return None, count, total
    return top, count, total


_CAMERA_RECHECK_SYSTEM = """You are reading frames from one beat of a source video to determine WHERE THE CAMERA STANDS.

Report the camera, not the subject. Read it off the frame's own geometry: which way the converging lines run, whether you can see the TOP faces of things (looking down) or their UNDERSIDES and the sky/ceiling behind them (looking up), where the horizon sits, and which face of the subject the lens is pointed at.

camera_angle (VERTICAL) - exactly one of:
  bird_eye      straight or near-straight down, the ground plane fills the frame
  high_angle    above the subject looking down, top surfaces visible, horizon high or out of frame
  eye_level     lens at standing height, frame reads level, horizon near the middle
  low_angle     below the subject looking up, undersides visible, horizon low
  worm_eye      lens on or near the ground looking sharply up
  dutch_angle   the whole frame is rolled off horizontal - the horizon itself is tilted

camera_bearing (HORIZONTAL) - exactly one of:
  front, three_quarter, side, rear_three_quarter, back

The two axes are INDEPENDENT: a shot can be low_angle AND side at the same time. Judge each on its own.

The frames are in chronological order and all come from the SAME beat. If the camera clearly MOVES between them (a genuine setup change inside one beat), say so with "moved": true and report the setup that holds for most of the beat.

If an axis genuinely cannot be read from these frames, return an empty string for it. An empty string is a correct answer; a guess is not.

Return ONE JSON object, no commentary, no code fences:
{"camera_angle": "<value or empty>", "camera_bearing": "<value or empty>", "moved": <true|false>}"""


def recheck_camera_setups(beats_doc, overview=None, facts=None, job_dir=None,
                          config=None, frames_dir=None, on_progress=None):
    """按帧复核有冲突空间里的机位读数。返回 (beats_doc, report)。

    report = {'skipped': 原因或 None, 'spaces': [...], 'checked': n,
              'confirmed': n, 'corrected': [...], 'escalated': n, 'unresolved': [...]}

    只写 camera_angle / camera_bearing / camera_setup_verified 三个键，别的一个不碰。
    这两栏是闭集枚举、不在 TRANSLATE_FIELDS 里（中文由 *_LABELS_ZH 直接渲染），所以这
    一趟**不需要重跑翻译**——但合成产物照样作废，机位分组一变，SETUP 句就跟着变。
    """
    beats = (beats_doc or {}).get('beats') or []
    report = {'skipped': None, 'spaces': [], 'checked': 0, 'confirmed': 0,
              'corrected': [], 'escalated': 0, 'unresolved': []}
    if not beats:
        report['skipped'] = '还没有节拍阶梯'
        return beats_doc, report
    if is_variant_doc(beats_doc):
        # 变体没有自己的原片帧，`reference_frames` 只是机位参考。拿母本的帧去改变体的
        # 机位，等于用一份不对应的证据改写另一份文档。
        report['skipped'] = '二创变体没有自己的原片帧，机位复核只在母本上跑'
        return beats_doc, report

    spaces = camera_conflict_spaces(beats)
    if not spaces:
        report['skipped'] = '没有空间落下两个以上机位，无需复核'
        return beats_doc, report
    report['spaces'] = sorted(spaces)

    by_frame = _frame_facts_by_name(beats_doc, overview, facts, job_dir)
    timeline = _review_frame_timeline(overview or {})
    if not by_frame:
        report['skipped'] = ('盘上没有 Pass A 的逐帧读数（frame_facts.json），'
                             '这一趟没有可对账的一手数据')
        return beats_doc, report

    targets = [b for b in beats
               if isinstance(b, dict)
               and (str(b.get('space') or '').strip() or '（未标空间）') in spaces]
    report['checked'] = len(targets)

    # ── 第一、二层：对账。零调用。
    pending = []          # 帧票没能定案的 (beat, [定不下来的轴名…], 已定案轴的票数)
    for beat in targets:
        names = _beat_window_frames(beat, timeline)
        unresolved_axes, marks = [], {}
        for field, allowed, _syn, labels in _CAMERA_RECHECK_AXES:
            current = str(beat.get(field) or '').strip()
            winner, count, total = _camera_axis_vote(names, by_frame, field, allowed)
            if winner is None:
                # 一票没有 = 这一拍没有 Pass A 读数；有票但不过半 = 帧自己就分裂。
                # 两种都交给第三层，不在这里硬定。
                unresolved_axes.append(field)
                continue
            if winner == current:
                marks[field] = f'{count}/{total}'
                continue
            # 覆盖一个**已有**读数比补一个空栏险得多：空栏本来就没有下游后果，覆盖会
            # 把一把几何锁挪到别处。所以覆盖要两票起，一票只够升级去看图。
            if current and count < 2:
                unresolved_axes.append(field)
                continue
            beat[field] = winner
            marks[field] = f'{count}/{total}'
            report['corrected'].append({
                'beat_id': beat.get('id'), 'field': field, 'was': current,
                'now': winner, 'votes': f'{count}/{total}', 'by': 'frames',
                'was_zh': labels.get(current, current or '（空）'),
                'now_zh': labels.get(winner, winner),
            })
        if unresolved_axes:
            pending.append((beat, unresolved_axes, marks))
        else:
            beat['camera_setup_verified'] = {
                'reading': camera_reading_stamp(beat), 'by': 'frames', 'votes': marks,
            }
            report['confirmed'] += 1

    # ── 第三层：升级。只有帧票定不了案的那几拍才走到这里，且只在给了 config 时。
    if pending and config:
        total_pending = len(pending)
        for seq, (beat, axes, marks) in enumerate(pending, 1):
            pp._raise_if_cancelled(on_progress)
            bid = beat.get('id')
            paths = _resolve_frame_paths(frames_dir, beat.get('evidence_frames')
                                         or beat.get('reference_frames'))
            if not paths:
                report['unresolved'].append({
                    'beat_id': bid, 'axes': axes, 'reason': '找不到这一拍的证据帧文件'})
                continue
            if on_progress:
                on_progress('replica_stage', {
                    'stage': 'review_beats',
                    'message': f'机位复核 {seq}/{total_pending}：{bid} 帧读数不一致，正在看图…',
                    'done': seq - 1, 'total': total_pending,
                })
            try:
                raw = pp._multimodal_chat(
                    config, _CAMERA_RECHECK_SYSTEM,
                    f'Beat {bid}. Frames are in chronological order. '
                    f'Report camera_angle and camera_bearing.',
                    paths, max_tokens=512, timeout=90)
                patch = parse_json_reply(raw)
            except pp.GenerationCancelled:
                raise
            except Exception as e:
                # 单拍失败不拖垮整轮：前面几拍对账出来的订正不能跟着丢。
                report['unresolved'].append({
                    'beat_id': bid, 'axes': axes, 'reason': f'复核调用失败：{e}'})
                continue
            if isinstance(patch, list):
                patch = next((x for x in patch if isinstance(x, dict)), None)
            patch = patch if isinstance(patch, dict) else {}
            report['escalated'] += 1
            still = []
            for field in axes:
                allowed, syn, labels = next(
                    (a, s_, l) for f, a, s_, l in _CAMERA_RECHECK_AXES if f == field)
                value = _coerce_enum(patch.get(field), allowed, syn)
                if not value:
                    still.append(field)
                    continue
                current = str(beat.get(field) or '').strip()
                marks[field] = 'model'
                if value == current:
                    continue
                beat[field] = value
                report['corrected'].append({
                    'beat_id': bid, 'field': field, 'was': current, 'now': value,
                    'votes': 'model', 'by': 'model',
                    'was_zh': labels.get(current, current or '（空）'),
                    'now_zh': labels.get(value, value),
                })
            if still:
                report['unresolved'].append({
                    'beat_id': bid, 'axes': still, 'reason': '模型也读不出这两栏'})
            else:
                # 模型说这一拍中途换过机位时不盖戳：戳的含义是「这一对读数管住整拍」，
                # 而它自己刚说了管不住。留着这条 warn，让人去看这一拍该不该拆。
                if patch.get('moved') is True:
                    report['unresolved'].append({
                        'beat_id': bid, 'axes': [],
                        'reason': '模型判定这一拍内部换过机位，建议按切点拆拍'})
                else:
                    beat['camera_setup_verified'] = {
                        'reading': camera_reading_stamp(beat), 'by': 'model', 'votes': marks,
                    }
                    report['confirmed'] += 1
    elif pending:
        for beat, axes, _marks in pending:
            report['unresolved'].append({
                'beat_id': beat.get('id'), 'axes': axes,
                'reason': '帧读数不一致，需要看图复核'})

    normalize_beat_craft_fields(beats_doc)
    beats_doc['validation'] = validate_beats(beats_doc, overview or {})
    return beats_doc, report


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
    ensure_three_evidence_frames(beats_doc, overview)
    attach_coverage_frames(beats_doc, overview)
    attach_shot_cuts(beats_doc, overview)

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
    for i, beat in enumerate(beats):
        text = f'{beat.get("visible_action") or ""} → {beat.get("visible_result") or ""}'.strip(' →')
        # 把耦合工序写进条目正文。合成器会照这份清单重新规划 ladder 并自己填
        # package_operations——不给它工序，它只能从一句动作描述里猜，猜出一道工序的
        # 拍就会被自己的硬闸判死。
        package = [str(p).strip() for p in (beat.get('package_operations') or []) if str(p).strip()]
        if package:
            text = f'{text}（工序：{"、".join(package)}）'
        raw_op = str(beat.get('operation') or beat.get('stage') or '').strip().lower()
        stage = str(beat.get('stage') or '').strip().lower()
        if stage == 'reveal' or raw_op in ('reward', 'reveal') or 'reveal' in raw_op or 'reward' in raw_op:
            entry_op = 'reward'
        elif stage in ('transition', 'threshold') or raw_op in ('threshold', 'reframe'):
            entry_op = 'threshold'
        else:
            entry_op = beat.get('operation') or beat.get('stage')
        entry = {'text': text or (beat.get('visual_subject') or ''),
                 'op': entry_op}
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

        # 大环境识别项（2026-08-20）：仅在第一拍（锚点首帧）与过门拍/换空间首拍（新空间承接）透传，
        # 中间普通工序拍默认不透传，避免对大模型提示词造成上下文干扰与动作冲淡。
        is_first = (i == 0)
        prev_space = normalize_space_label(beats[i - 1].get('space')) if i > 0 else ''
        is_crossed = bool(i > 0 and prev_space and space and prev_space.lower() != space.lower())
        is_transition = (beat.get('stage') == 'transition')
        if is_first or is_crossed or is_transition:
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

        # 制作字段（2026-08-22）。此前这六件事在卡片上根本没有落脚点：工具与音效只能
        # 混在动作那句英文里、机位压根没有字段、光照与物料去向直接丢掉。它们各自都有
        # 一条已经在跑的下游规则在等（动作-工具-音效三联、ASMR 原声 60%、
        # Material & Spoil Balance），只是没人喂。键名在这里就收短，因为清单条目会被
        # 整段渲进规划提示词，长键名要按拍数乘一遍。
        tool = str(beat.get('tool') or '').strip()
        if tool:
            entry['tool'] = tool
        sfx = [str(x).strip() for x in (beat.get('sfx') or []) if str(x).strip()]
        if sfx:
            entry['sfx'] = sfx

        # 微观取证四栏（2026-08-24）。Pass A 逐帧量的规格/紧固/微痕在 2026-08-24 之前
        # 连 beats 都进不去（schema 里没有字段），现在既然进来了，就必须一路走到写手
        # 手上——否则只是把断点从 Pass B 挪到了这里。键名照例收短：清单条目会被整段
        # 渲进规划提示词，长键名要按拍数乘一遍。
        tool_specifics = str(beat.get('tool_specifics') or '').strip()
        if tool_specifics:
            entry['tool_specifics'] = tool_specifics
        for src, dst in (('material_specs', 'mat_specs'),
                         ('fastening_and_bonding', 'fasteners'),
                         ('micro_traces', 'micro')):
            items = [str(x).strip() for x in (beat.get(src) or []) if str(x).strip()]
            if items:
                entry[dst] = items
        # 拍摄角度两栏（2026-08-25）走同一条通路。键名保持全称：`angle` / `bearing`
        # 单看容易被读成「墙角」「方位角」，而这两个词会原样渲进规划提示词。
        for src, dst in (('shot_scale', 'shot_scale'), ('camera_move', 'camera_move'),
                         ('camera_angle', 'camera_angle'), ('camera_bearing', 'camera_bearing'),
                         ('lens_feel', 'lens_feel'), ('subject_placement', 'placement'),
                         ('time_treatment', 'time_treatment'),
                         ('light_state', 'light'), ('material_flow', 'flow')):
            value = str(beat.get(src) or '').strip()
            if value:
                entry[dst] = value
        if isinstance(beat.get('worker_count'), int):
            # 写成字符串而不是整数：清单条目的富字段通路全是字符串键，
            # 0 传下去还会被 `if value` 判成空。
            entry['crew'] = str(beat['worker_count'])

        # 观察到的镜头数（2026-08-23，多镜头兼容）。它**不进规划提示词**——镜头梯是
        # 合成期由 composer 确定性排的，规划器不参与；把这个数渲给它只会诱导它往
        # description 里写分镜。它走的是与 space 同一条通路：随清单条目落到
        # parsed_brief['beat_outline']，再由 apply_observed_shot_counts 按下标贴回梯子。
        if isinstance(beat.get('observed_shot_count'), int):
            entry['shot_count'] = str(beat['observed_shot_count'])
        if isinstance(beat.get('observed_shot_seconds'), (int, float)):
            entry['shot_seconds'] = str(beat['observed_shot_seconds'])
        # 逐镜景别序列（2026-08-25）。走 shot_count 同一条通路：随清单条目落到
        # parsed_brief['beat_outline']，再由 pp.apply_observed_craft_fields 按下标贴回
        # 梯子，最后由 composer 排梯时逐镜取用。它**不进规划提示词**——分镜是合成期
        # 确定性排的，渲给规划器只会诱导它往 description 里自己写分镜。
        scales = [str(x).strip() for x in (beat.get('observed_shot_scales') or [])]
        if any(scales):
            entry['shot_scales'] = '/'.join(x or '?' for x in scales)
        # 插入镜主体走的是另一条通路：它是**内容**，规划器要把它织进这一拍的描述里，
        # 所以它既进 parsed_brief（给合成期逐拍绑定）也进规划提示词（见
        # build_outline_plan_block 的 INSERT 规则，只在多镜头链路上渲染）。
        insert_subject = str(beat.get('insert_subject') or '').strip()
        if insert_subject:
            entry['insert'] = insert_subject
        cast_action = str(beat.get('cast_action') or '').strip()
        if cast_action:
            entry['cast'] = cast_action

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
        if isinstance(constants, dict):
            dimensions['scene_constants'] = {k: list(v) for k, v in constants.items() if v}
        elif isinstance(constants, list):
            dimensions['scene_constants'] = [str(x).strip() for x in constants if str(x).strip()]
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
    if beats:
        first_beat = beats[0]
        macro_env = first_beat.get('macro_environment')
        if macro_env:
            dimensions['initial_macro_environment'] = (
                macro_env if isinstance(macro_env, (list, tuple)) else [macro_env]
            )
        if first_beat.get('state_before'):
            dimensions['initial_state_before'] = str(first_beat.get('state_before')).strip()
        # worker_attire 是给合成侧那条「主题自适应着装」规则用的单句权威口径。
        # cast_identity（全片级、含人种/肤色）落在 scene_constants.cast 里，整段随
        # SCENE CONSTANTS 进每一条提示词；这里额外把第一位（出镜最多的那个）折成一句
        # 兜底 worker_attire，免得那条老规则在没有 worker_attire 的单子上回落成
        # 「通用工装」，跟 SCENE CONSTANTS 里的真人对撞。
        _sc = beats_doc.get('scene_constants')
        _sc_dict = _sc if isinstance(_sc, dict) else {}
        _cast = [str(x).strip()
                 for x in (_sc_dict.get('cast')
                           or beats_doc.get('cast_identity') or [])
                 if str(x).strip()]
        attire = str(beats_doc.get('worker_attire') or first_beat.get('worker_attire')
                     or (_cast[0] if _cast else '')).strip()
        if attire:
            dimensions['worker_attire'] = attire
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
        if re.search(regex, text, re.IGNORECASE):
            hits.append(item)
    return hits

