# -*- coding: utf-8 -*-
"""把反推阶段逐帧读到的画面记录，接回提示词合成的前后两端。

合成器从头到尾看不到任何一帧原片：80 秒视频最终只经由约七千字文本抵达写手，而那七千
字是 550 条逐帧读数被压成 17 拍之后的残余。2026-08-30 复盘（replica_cf9a445bc52b 微缩
草原庄园）：IMG 001 的名词全对（微缩沙盘/泥屋/图纸/人偶），机位、景别、三区布局、前景
植被全错——错的不是写手，是它压根没有画面可依。

这里提供两道，前后各一道，两条链路（极速直通 / 标准深度）共用同一份实现：

  · 前置（文本，零额外视觉调用）：`build_observed_digests` 把每一拍 `coverage_frames`
    对应的逐帧读数压成一张「原片实拍事实卡」，随合成输入一起发给写手。依据是反推阶段
    已经落盘的 `frame_facts.json`，不重新抽帧、不重新读图。

  · 后置（视觉）：`ground_prompt_block_on_observed` 逐拍把该拍的 `evidence_frames`
    真图发给多模态模型，拿写好的 IMAGE/VIDEO 提示词跟画面对一遍，就地订正对不上的
    地方。改不动就软退并留告警，绝不阻塞合成。

**后置那道只准动画面，不准动施工进度。** 提示词正文同时承载两件事：这一拍长什么样
（可以照原片订正），和这一拍干完了什么（由已核验的拍表说了算）。让视觉模型改后者等于
把用户逐拍核对过的阶梯推翻重来，而且它只看得见三张图、看不见前后拍的因果。系统提示词
里那条 SCOPE 是硬约束，不是客套。
"""

import json
import os
import re
import sys

import prompt_pipeline as pp


# 逐拍送审的取证帧上限。反推阶段每拍存了 3 张 evidence_frames、约 10 张 coverage_frames；
# 全发进去只会稀释注意力（2026-07-23 那次「一次塞满全部帧图」的教训写在
# _local_beat_review_system_prompt 的注释里），而 3 张足够判机位/景别/材质/人物。
_MAX_FRAMES_PER_CALL = 3

# 订正稿短于原稿这个比例就判定为模型截断/敷衍，弃用并保留原稿。
_MIN_REWRITE_RATIO = 0.5

# 单行上限。逐帧读数里个别栏（材质规格、微痕）能写到七八百字符，逐槽位铺开就是几 KB
# 的边际收益极低的长尾。
_MAX_LINE_CHARS = 400


# --------------------------------------------------------------------------
# 逐帧读数
# --------------------------------------------------------------------------

def load_frame_facts(job_dir):
    """读这条 job 的 frame_facts.json，返回 {帧文件名: 该帧读数}。

    读不到返回 {}——前置那道随即退化为「只用拍级字段」，不报错：事实卡是增量信息，
    缺了应该让合成继续跑，而不是把整单卡死。
    """
    if not job_dir:
        return {}
    path = os.path.join(job_dir, 'frame_facts.json')
    try:
        with open(path, 'r', encoding='utf-8') as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return {}
    facts = raw.get('facts') if isinstance(raw, dict) else raw
    if not isinstance(facts, list):
        return {}
    out = {}
    for item in facts:
        if isinstance(item, dict) and item.get('frame'):
            out[str(item['frame'])] = item
    return out


def _frame_path(job_dir, frame_name):
    if not (job_dir and frame_name):
        return None
    path = os.path.join(job_dir, 'review_frames', str(frame_name))
    return path if os.path.exists(path) else None


def _coverage_names(beat):
    """该拍按时间排好的 coverage 帧名。没有 coverage_frames 时退回 evidence_frames。"""
    cov = beat.get('coverage_frames')
    if isinstance(cov, list) and cov:
        rows = []
        for c in cov:
            if isinstance(c, dict) and c.get('frame'):
                rows.append((pp._num(c.get('timestamp'), default=0.0) if hasattr(pp, '_num')
                             else float(c.get('timestamp') or 0.0), str(c['frame'])))
            elif isinstance(c, str):
                rows.append((0.0, c))
        rows.sort(key=lambda r: r[0])
        if rows:
            return [name for _, name in rows]
    ev = beat.get('evidence_frames')
    if isinstance(ev, list):
        return [str(x) for x in ev if x]
    return []


def _evidence_names(beat):
    """逐拍送审用的取证帧：优先 evidence_frames（Pass A 挑过的关键帧），
    否则从 coverage 里取首/中/尾三张。"""
    ev = [str(x) for x in (beat.get('evidence_frames') or []) if x]
    if ev:
        return ev[:_MAX_FRAMES_PER_CALL]
    cov = _coverage_names(beat)
    if not cov:
        return []
    if len(cov) <= _MAX_FRAMES_PER_CALL:
        return cov
    return [cov[0], cov[len(cov) // 2], cov[-1]]


# --------------------------------------------------------------------------
# 事实卡
# --------------------------------------------------------------------------

def _clean_items(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        items = value
    else:
        items = [value]
    out = []
    for x in items:
        s = str(x).strip()
        if s and s not in out:
            out.append(s)
    return out


def _merge_items(facts, key, limit=6):
    """把若干帧读数里同一栏合并去重，保留出现顺序。"""
    out = []
    for fact in facts:
        for s in _clean_items(fact.get(key)):
            if s not in out:
                out.append(s)
            if len(out) >= limit:
                return out
    return out


def _clip_lines(lines):
    """逐行截到 _MAX_LINE_CHARS。截断处补省略号，让读的人知道这行被截过。"""
    out = []
    for ln in lines:
        if len(ln) > _MAX_LINE_CHARS:
            ln = ln[:_MAX_LINE_CHARS].rstrip(' ;,') + '…'
        out.append(ln)
    return out


def _zones_digest(fact):
    zones = fact.get('spatial_zones')
    if not isinstance(zones, dict):
        return []
    order = ('overhead', 'facade_and_walls', 'floor', 'peripherals_and_spoil')
    lines = []
    for key in list(order) + [k for k in zones if k not in order]:
        val = str(zones.get(key) or '').strip()
        if val:
            lines.append(f'{key}: {val}')
    return lines


def _image_digest(fact, include_micro=True):
    """一张静帧的事实卡：这张 IMAGE 该长什么样。

    include_micro：见 build_observed_digests。微观痕迹只发给微缩通道。"""
    if not fact:
        return ''
    lines = []
    subject = str(fact.get('subject') or '').strip()
    if subject:
        lines.append(f'Observed subject: {subject}')
    cam = [f'{label}: {str(fact.get(key) or "").strip()}'
           for key, label in (('shot_scale', 'shot scale'), ('camera_angle', 'camera angle'),
                              ('camera_bearing', 'camera bearing'), ('lens_feel', 'lens feel'))
           if str(fact.get(key) or '').strip()]
    if cam:
        lines.append('Observed camera: ' + '; '.join(cam))
    placement = str(fact.get('subject_placement') or '').strip()
    if placement:
        lines.append(f'Observed framing / subject placement: {placement}')
    for line in _zones_digest(fact):
        lines.append(f'Observed zone — {line}')
    # cast_appearance 不进静帧事实卡：帧序列一律净帧（frame_state.PERSON_FREE_IMAGE_FRAMES），
    # 这张 IMAGE 里根本不该有人，把「他长什么样」发给写手就是在请他写进去。人物外形仍然
    # 完整送达 VIDEO 一侧——cast_identity 是单独一栏发的（见 reverse.attach_scene_constants），
    # 那才是全片口径的人物识别项，逐帧读数这一份本来就是它的原料。
    for key, label in (('materials', 'Observed materials'),
                       ('material_specs', 'Observed material specs'),
                       ('micro_traces', 'Observed micro traces')):
        if key == 'micro_traces' and not include_micro:
            continue
        items = _clean_items(fact.get(key))
        if items:
            lines.append(f'{label}: ' + '; '.join(items[:6]))
    extent = str(fact.get('completion_extent') or '').strip()
    if extent:
        lines.append(f'Observed completion extent: {extent}')
    return '\n'.join(_clip_lines(lines))


def _video_digest(beat, facts, include_micro=True):
    """一拍运动的事实卡：这条 VIDEO 该怎么动。

    include_micro：见 build_observed_digests。微观痕迹只发给微缩通道。"""
    lines = []
    for key, label in (('camera_move', 'Observed camera move'),
                       ('shot_scale', 'Observed shot scale'),
                       ('camera_angle', 'Observed camera angle'),
                       ('camera_bearing', 'Observed camera bearing'),
                       ('lens_feel', 'Observed lens feel'),
                       ('subject_placement', 'Observed framing'),
                       ('light_state', 'Observed light'),
                       ('material_flow', 'Observed material flow'),
                       ('cast_action', 'Observed living cast action'),
                       ('tool', 'Observed tool'),
                       ('tool_specifics', 'Observed tool specifics')):
        value = str(beat.get(key) or '').strip()
        if value:
            lines.append(f'{label}: {value}')
    shot_count = beat.get('observed_shot_count')
    if isinstance(shot_count, int) and shot_count > 0:
        lines.append(f'Observed shot count in this beat: {shot_count}')
    scales = _clean_items(beat.get('observed_shot_scales'))
    if scales:
        lines.append('Observed per-shot scale ladder: ' + ' -> '.join(scales))
    cuts = beat.get('observed_cuts')
    if isinstance(cuts, (list, tuple)) and cuts:
        lines.append(f'Observed cut count inside this beat: {len(cuts)}')
    for key, label in (('materials', 'Observed materials across this beat'),
                       ('micro_traces', 'Observed micro traces across this beat')):
        if key == 'micro_traces' and not include_micro:
            continue
        items = _merge_items(facts, key)
        if items:
            lines.append(f'{label}: ' + '; '.join(items))
    # 这里曾经再合并一遍 cast_appearance。它是整份事实卡最大的一栏（实测 8.8KB / 17 行），
    # 而同样的内容 cast_identity 已经单独发过一遍、每张 IMAGE 事实卡里也带着——运动这一侧
    # 真正要的是 cast_action（人偶这一拍在做什么），不是再抄一遍他们长什么样。
    if facts:
        first_extent = str(facts[0].get('completion_extent') or '').strip()
        last_extent = str(facts[-1].get('completion_extent') or '').strip()
        if first_extent or last_extent:
            lines.append(f'Observed progress across this beat: {first_extent or "?"} -> {last_extent or "?"}')
        workers = [f.get('workers_present') for f in facts if f.get('workers_present') is not None]
        if workers:
            lines.append('Observed workers present in frame: '
                         + ('yes' if any(workers) else 'no'))
    return '\n'.join(_clip_lines(lines))


def build_observed_digests(beats_doc, job_dir, include_micro_traces=True):
    """把逐帧读数压成逐槽位的「原片实拍事实卡」。

    槽位对应关系：IMAGE 1 = 第 1 拍的起点画面；IMAGE k+1 = 第 k 拍的终点画面；
    VIDEO k = 第 k 拍的运动。这与 `_parse_prompt_slots` 的编号完全一致。

    返回 {'images': {n: 卡片文本}, 'videos': {n: 卡片文本},
          'image_frames': {n: [帧绝对路径]}, 'video_frames': {n: [...]}}。
    没有任何可用素材时返回空壳，调用方按「没有事实卡」正常继续。

    include_micro_traces（2026-08-30）：微观痕迹（锯末、粉笔弹线、油漆飞溅这类只有
    微距镜头才看得见的痕迹）只发给 miniature 通道。base / omni 交付的是全尺寸实景，
    这一栏在它们的景别下根本渲染不出来，进了提示词只会挤掉真正能看见的画面依据——
    与 build_outline_plan_block 的 micro_traces 门控、apply_observed_craft_fields 的
    include_micro 是同一条口径的三个注入点，判据统一是 active_skill_profile()。
    """
    # 局部 import：本模块与 reverse 都在 prompt_pipeline 包内、且都在模块级 import pp，
    # 顶层互相 import 会成环。
    from prompt_pipeline import reverse

    empty = {'images': {}, 'videos': {}, 'image_frames': {}, 'video_frames': {}}
    beats = (beats_doc or {}).get('beats') or []
    if not beats:
        return empty
    facts_by_name = load_frame_facts(job_dir)

    images, videos, image_frames, video_frames = {}, {}, {}, {}
    for i, beat in enumerate(beats):
        idx = i + 1
        names = _coverage_names(beat)
        beat_facts = [facts_by_name[n] for n in names if n in facts_by_name]

        # IMAGE 1 只由第一拍的起点帧决定。取哪一张不是 names[0] 说了算：
        # 1. 优先认第一拍已核准的证据帧 Triad 起始锚点（evidence_frames[0] 是模型/人工卡点确定的起步帧）。
        # 2. 原片开头可能是完工/半成品先导钩子闪帧，照直取就是把成品画面钉成整条 i2i 链的地基。
        #    判据与组稿期锚点对齐、渲染期对标帧共用一份闪帧判据（reverse.select_opening_anchor）。
        if idx == 1 and (names or beat.get('evidence_frames')):
            evi = [str(x) for x in (beat.get('evidence_frames') or []) if x]
            cand_names = (evi + [n for n in names if n not in evi]) if evi else names
            head_name = reverse.select_opening_anchor(cand_names, facts_by_name) or cand_names[0]
            head_fact = facts_by_name.get(head_name)
            digest = _image_digest(head_fact, include_micro=include_micro_traces)
            if digest:
                images[1] = digest
            head_path = _frame_path(job_dir, head_name)
            if head_path:
                image_frames[1] = [head_path]

        # IMAGE idx+1 = 本拍终点帧
        if names:
            tail_fact = facts_by_name.get(names[-1])
            digest = _image_digest(tail_fact, include_micro=include_micro_traces)
            if digest:
                images[idx + 1] = digest
            tail_path = _frame_path(job_dir, names[-1])
            if tail_path:
                image_frames[idx + 1] = [tail_path]

        v_digest = _video_digest(beat, beat_facts, include_micro=include_micro_traces)
        if v_digest:
            videos[idx] = v_digest
        paths = [p for p in (_frame_path(job_dir, n) for n in _evidence_names(beat)) if p]
        if paths:
            video_frames[idx] = paths

    return {'images': images, 'videos': videos,
            'image_frames': image_frames, 'video_frames': video_frames}


# 全量事实卡进单轮直出时的字符上限。原始 18 张静帧 + 17 段运动逐条铺开约 65KB；去掉
# 重复的人偶外观栏、提走全片恒定的两栏、再截单行长尾之后落在 40KB 出头（≈1 万 token）。
# 极速通道本来就只发一次调用，这个量装得下，也没到会稀释注意力的程度。上限只作最后兜底：
# 真撞上了宁可显式说明「后段未列出」，也不要静默截断——静默截断的后果是后几拍没有画面
# 依据，而写手看不出这一点。
_FULL_BLOCK_CEILING = 48000


def _compact_slot_digests(slot_digests, threshold=0.6):
    """把在多数槽位里重复出现的行提出来，返回 (恒定项, {槽位: 该槽位独有的行})。

    事实卡逐槽位铺开时，"Observed materials: 干草/木枝/泥浆…" 这类行会原样重复十几遍。
    它们描述的是这部片子本身，不是某一拍——提到开头说一次，既省上下文，也让每一拍剩下
    的行全是真正的差异（机位、景别、构图、完成度），写手一眼能看出这拍跟上一拍差在哪。
    """
    if not slot_digests:
        return [], {}
    per_slot = {n: [ln for ln in (text or '').split('\n') if ln.strip()]
                for n, text in slot_digests.items()}
    # 按**栏目**提取，不按整行。逐帧读数是逐帧独立写出来的，同一件事每帧措辞都略有出入，
    # 整行比对几乎匹配不上任何一条（实测提取率 0，整块随即撞上限被截断）。
    #
    # 只提材质规格这一栏：它描述的是「这部片子由什么构成」，全片近乎不变，说一次就够。
    # 机位/构图/景别/完成度/材质本身即便重复也必须逐拍留着——它们正是写手用来分辨这一拍
    # 跟上一拍差在哪的依据，提走就等于把每一拍写成同一拍。
    # 'Observed living cast:' 曾经也在这里。净帧策略之后静帧事实卡不再发这一栏（见
    # _image_digest），提不到任何东西，留着只是空跑一遍——一并撤掉。
    hoistable = ('Observed material specs:',)
    hits = {}
    for n in sorted(per_slot):
        for ln in per_slot[n]:
            for prefix in hoistable:
                if ln.strip().startswith(prefix):
                    # 同栏留最长的那条：逐帧读数详略不一，最长的那条信息最全。
                    if len(ln) > len(hits.get(prefix, '')):
                        hits[prefix] = ln
    total = len(per_slot)
    if total < max(2, int(total * threshold)) or not hits:
        return [], per_slot
    ordered_constant = [hits[p] for p in hoistable if p in hits]
    rest = {n: [ln for ln in lines
                if not any(ln.strip().startswith(p) for p in hoistable)]
            for n, lines in per_slot.items()}
    return ordered_constant, rest


# 撞上限时按栏目逐级降级的顺序（先丢的排前面）。丢栏目而不是砍尾巴：字符截断会让后
# 几拍一行画面依据都没有，而写手看不出这一点——他只会照常凭空想，跟改动之前一模一样。
# 丢栏目是全片均摊的损失，每一拍都还留着机位/构图/完成度这几条最要紧的。
_DEGRADE_ORDER = (
    'Observed micro traces',
    'Observed materials across this beat',
    'Observed zone — peripherals_and_spoil',
    'Observed progress across this beat',
    'Observed zone — overhead',
    'Observed material flow',
    'Observed subject',
    'Observed tool specifics',
    'Observed light',
    'Observed materials',
    'Observed zone —',
    'Observed completion extent',
)

# 降级栏目用尽后的最后一档：每个槽位只留最要紧的这几条。宁可每一拍都只剩机位和构图，
# 也不要有几拍一行都没有——前者是全片均摊的信息损失，后者是那几拍完全回到凭空想。
_MINIMAL_KEEP = ('Observed camera', 'Observed framing', 'Observed shot scale',
                 'Observed per-shot scale ladder')


def _render_full_block(header, constant, img_rest, vid_rest, total_beats, dropped):
    lines = list(header)
    if constant:
        lines.append('\n[CONSTANT ACROSS THE WHOLE FILM — carry into every slot, do not restate as news]')
        lines.extend(constant)
    for n in range(1, total_beats + 2):
        rows = [r for r in (img_rest.get(n) or []) if r.strip()]
        if rows:
            lines.append(f'\n[IMAGE {n} — observed reference state]')
            lines.extend(rows)
        if n <= total_beats:
            rows = [r for r in (vid_rest.get(n) or []) if r.strip()]
            if rows:
                lines.append(f'\n[VIDEO {n} — observed reference motion]')
                lines.extend(rows)
    if dropped:
        lines.append('\n[NOTE] These observation columns were omitted for length across all slots: '
                     + ', '.join(dropped)
                     + '. Every slot above still carries its camera, framing and completion state.')
    return '\n'.join(lines)


def observed_digest_block(digests, total_beats):
    """前置那道注进合成输入的正文（全量版，给单轮直出的极速通道用）。

    空事实卡返回空串，调用方据此决定加不加这一段。逐拍那份走 `beat_digest_block`。
    """
    if not digests:
        return ''
    images = digests.get('images') or {}
    videos = digests.get('videos') or {}
    if not images and not videos:
        return ''

    img_const, img_rest = _compact_slot_digests(images)
    vid_const, vid_rest = _compact_slot_digests(videos)
    constant = img_const + [ln for ln in vid_const if ln not in img_const]

    header = [
        'OBSERVED ORIGINAL FOOTAGE (read frame-by-frame off the reference film during reverse'
        ' engineering — this is what the camera ACTUALLY saw, not a suggestion):',
        'Write every slot below so its camera, framing, shot scale, spatial layout, materials,'
        ' weathering and living cast match these observations. Where an observation conflicts'
        ' with your instinct for a "nicer" composition, the observation wins. These describe how'
        ' each shot LOOKS — what work happens in it is still the approved beat ladder above.',
    ]

    dropped = []
    text = _render_full_block(header, constant, img_rest, vid_rest, total_beats, dropped)
    for prefix in _DEGRADE_ORDER:
        if len(text) <= _FULL_BLOCK_CEILING:
            break
        img_rest = {n: [ln for ln in rows if not ln.strip().startswith(prefix)]
                    for n, rows in img_rest.items()}
        vid_rest = {n: [ln for ln in rows if not ln.strip().startswith(prefix)]
                    for n, rows in vid_rest.items()}
        dropped.append(prefix)
        text = _render_full_block(header, constant, img_rest, vid_rest, total_beats, dropped)

    if len(text) > _FULL_BLOCK_CEILING:
        # 全部可降级栏目都丢完还超顶（拍数极多的长片）：退到最小档——每槽位只留机位与构图。
        img_rest = {n: [ln for ln in rows if ln.strip().startswith(_MINIMAL_KEEP)]
                    for n, rows in img_rest.items()}
        vid_rest = {n: [ln for ln in rows if ln.strip().startswith(_MINIMAL_KEEP)]
                    for n, rows in vid_rest.items()}
        dropped = ['everything except camera and framing']
        text = _render_full_block(header, constant, img_rest, vid_rest, total_beats, dropped)

    if len(text) > _FULL_BLOCK_CEILING:
        # 最小档都装不下（极端长片）。这时才真的少发槽位——但**按槽位整条丢并点名**，
        # 不做字符截断。字符截断会在半行处断掉，读的人无从知道后面还有几拍没列；点名之后
        # 写手至少知道「这几拍我没有画面依据」，这跟悄悄少给是两回事。
        omitted = []
        for n in range(total_beats + 1, 0, -1):
            if len(text) <= _FULL_BLOCK_CEILING:
                break
            if n <= total_beats and vid_rest.get(n):
                vid_rest[n] = []
                omitted.append(f'VIDEO {n}')
            if img_rest.get(n + 1) if n + 1 in img_rest else None:
                img_rest[n + 1] = []
                omitted.append(f'IMAGE {n + 1}')
            text = _render_full_block(header, constant, img_rest, vid_rest, total_beats, dropped)
        if omitted:
            text += ('\n[NOTE] No observations are supplied for these slots: '
                     + ', '.join(reversed(omitted))
                     + '. Write them from the beat ladder and the CONSTANT block alone.')
    return text



def beat_digest_block(digests, beat_index):
    """单拍注入用：第 i 拍写的是 VIDEO i 与 IMAGE i+1，所以事实卡也只发这两张。

    深度链路是逐拍/逐窗把提示词发出去的，整份 observed_digest_block 按拍数乘一遍会把
    上下文撑爆，还会让写手在写第 3 拍时读到第 14 拍的画面。极速链路是单轮直出全量，
    才用得上整份的 observed_digest_block。
    """
    if not digests:
        return ''
    images = digests.get('images') or {}
    videos = digests.get('videos') or {}
    v = videos.get(beat_index)
    img = images.get(beat_index + 1)
    if not v and not img:
        return ''
    lines = [
        'OBSERVED ORIGINAL FOOTAGE FOR THIS BEAT (read frame-by-frame off the reference film;'
        ' this is what the camera actually saw). Match its camera, framing, shot scale, spatial'
        ' layout, materials, weathering and living cast. Where it conflicts with your instinct'
        ' for a nicer composition, the observation wins. It describes how the shot LOOKS — the'
        ' construction work to perform is still the beat contract above.',
    ]
    if v:
        lines.append(f'\n[VIDEO {beat_index} — observed reference motion]\n{v}')
    if img:
        lines.append(f'\n[IMAGE {beat_index + 1} — observed reference state]\n{img}')
    return '\n'.join(lines)


# --------------------------------------------------------------------------
# 人偶物理比例
# --------------------------------------------------------------------------

# 人偶身高（真人 1.7m 按比例缩）低于这个厘米数就不再单独换算——低于 2cm 的读数多半是
# 反推把某个道具错认成人偶，写进锁里会把人偶钉成一粒沙。
_MIN_CAST_CM = 2.0

_RATIO_RE = re.compile(r'\b1\s*[:：]\s*(\d{1,3})\b')

# 只从「活物」那两栏里找比例。整份读数里 1:24 也可能出现在建筑模型的描述上，那是
# 另一件事——把建筑比例当人偶比例锁下去，正是这次要修的那种张冠李戴。
_CAST_FIELDS = ('cast_appearance',)


def _ratio_votes(texts):
    votes = {}
    for t in texts:
        for m in _RATIO_RE.finditer(str(t or '')):
            try:
                denom = int(m.group(1))
            except ValueError:
                continue
            if 4 <= denom <= 200:      # 常见微缩比例区间；越界的多半是尺寸而非比例
                votes[denom] = votes.get(denom, 0) + 1
    return votes


def measure_cast_scale(beats_doc, job_dir, cast_identity=None):
    """从原片逐帧读数里量出人偶的物理比例，返回一句可直接进 packet 的锁文本。

    量不到返回 None——调用方据此**不写这个键**。凭空造一个比例比没有更糟：下游的注入
    和校验都会拿它当权威，而它是编的。

    为什么锁物理比例而不是画幅占比：微缩线的镜头矩阵明令机位要在微距特写和广角英雄
    镜头之间大幅变化，画幅占比会随机位合法地变；恒定的是人偶相对建筑的**物理**大小。
    2026-08-30 实测（replica_cf9a445bc52b）那九帧漂掉的正是后者。

    投票而不是取第一条：550 帧里 `1:24` 出现 195 次、`1:50` 只有 2 次，后者是逐帧读数
    偶发的误读。少数派直接淘汰。
    """
    texts = []
    for c in (cast_identity or []):
        texts.append(c)
    facts_by_name = load_frame_facts(job_dir)
    for fact in facts_by_name.values():
        for key in _CAST_FIELDS:
            texts.extend(_clean_items(fact.get(key)))

    votes = _ratio_votes(texts)
    if not votes:
        return None
    denom = max(votes, key=lambda d: votes[d])
    total = sum(votes.values())
    # 得票不过半说明原片读数自己就没读准，锁下去等于把噪声钉死。
    if votes[denom] * 2 <= total:
        if sys.stdout:
            print(f'[OBSERVED] 人偶比例读数分歧过大（{votes}），本单不下比例锁')
        return None

    height_cm = round(170.0 / denom, 1)
    if height_cm < _MIN_CAST_CM:
        if sys.stdout:
            print(f'[OBSERVED] 人偶比例 1:{denom} 折算身高仅 {height_cm}cm，判为误读，本单不下比例锁')
        return None
    return (f'1:{denom} scale — each standing person is about {height_cm}cm tall, '
            f'roughly one {denom}th of a real adult')


def cast_scale_clause(packet):
    """packet 里的比例锁渲成一句可以直接缀在 IMAGE 正文后面的话。

    刻意同时给出比例、绝对身高和一条**相对参照**：图像模型对 "1:24" 这种记号不敏感，
    但对「成年人的手比他们大得多」这类相对关系敏感得多。2026-08-30 那九帧里 IMG 006 的
    人偶压过整个地基，正是没有任何相对参照可依。

    参照句只用由比例本身必然成立的两条（真人手 vs 人偶、一层楼 = 若干个人偶高）。不写
    「比两块砖高一点」这种——砖多大并不知道，编一条具体参照就是把这次要修的张冠李戴
    换个地方再犯一遍。
    """
    text = str((packet or {}).get('cast_scale') or '').strip()
    if not text:
        return ''
    return (f'Human scale lock: {text}, the same physical size in every frame. '
            'An adult human hand entering the frame dwarfs them, and a single storey of the '
            'build stands several of their heights tall — they never grow to full size '
            'against the miniature structure, and never shrink to specks. They are real '
            'living people at that size, never dolls or resin figures.')


# 判定「这一槽位里有活物」用的词。与 __init__._LIVING_SUBJECT_WORDS 同源，但这里只需要
# 判在不在，不需要判是不是锚点。
_CAST_WORDS = ('figurine', 'figurines', 'couple', 'resident', 'residents',
               'man', 'woman', 'villager', 'villagers', 'family', 'child', 'children',
               # 活物一律真人（human_cast）改写之后正文里剩下的就是这些词。不进表的话
               # 改写反而会让「这条提到活物」判成否，比例锁从此一条都注入不进去。
               'person', 'people', 'men', 'women', 'worker', 'workers', 'builder',
               'builders', 'craftsman', 'craftsmen', 'crew')

# 去重记号：重跑一次不能把锁句叠加两遍。
# 新旧两套记号都留着：老任务的 prompt_block 上写的是 2026-08-30 之前那句 figurine 版
# （见 cast_scale_clause 的改动），只认新记号的话，重跑一次会在同一段里叠出两句互相
# 矛盾的比例声明——图像模型只会挑一句听。
_CAST_SCALE_MARKERS = ('human scale lock:', 'figurine scale lock:',
                       'the same physical size in every frame',
                       'never grow to full size', 'never grow to human proportions',
                       'shrink to specks')


def _mentions_cast(text):
    low = ' ' + re.sub(r'[^a-z]+', ' ', str(text or '').lower()) + ' '
    return any(f' {w} ' in low for w in _CAST_WORDS)


def _strip_cast_scale(text):
    """删掉此前写进去的锁句（本函数的旧产物，或写手自己free-write的同义句）。

    不删就会累积：重跑一次多一句，几轮之后同一段里三四句互相矛盾的比例声明，
    图像模型只会挑一句听。
    """
    sentences = re.split(r'(?<=[.!?])\s+', str(text or '').strip())
    kept = [x for x in sentences
            if not any(m in x.lower() for m in _CAST_SCALE_MARKERS)]
    return ' '.join(x for x in kept if x).strip()


def enforce_cast_scale_lock(prompt_block, packet, beats_doc=None):
    """把 packet 的人偶物理比例锁，确定性地钉进每一条提到活物的 IMAGE 提示词。

    **为什么在 prompt_block 这一层做**，而不是在合成器内部：
      · `frame_state.compile_delta_image_prompt` 会把写手正文整段丢掉、只按白名单字段
        重拼（2026-08-23 人偶姿态句就是在那里消失的）。在它之前注入等于白注入；
        block 层面天然在它之后。
      · 极速线和深度线在这一层汇合，共用一个调用点——这正是 2026-08-30 连查两个坑
        （首帧对齐、比例锁）的共同教训：同一件事有两个口径，就一定会有一条线是坏的。

    只动 IMAGE。VIDEO 侧的活物大小由 IMAGE 的首尾锚点决定，另写一句只会和既有的
    worker_scale/进退场模板打架。

    返回 (prompt_block, report)。没有锁、解析不出槽位、或一条都没提到活物时原样返回。
    """
    report = {'locked': [], 'skipped': None, 'scale': None}
    clause = cast_scale_clause(packet)
    if not prompt_block or not clause:
        report['skipped'] = '没有可用的人偶比例锁（原片读数里量不出比例）'
        return prompt_block, report
    report['scale'] = str((packet or {}).get('cast_scale') or '')

    parsed_images, parsed_videos = pp._parse_prompt_slots(prompt_block)
    if not parsed_images:
        report['skipped'] = 'prompt_block 解析不出图片槽位'
        return prompt_block, report

    changed = False
    for n in sorted(parsed_images):
        item = parsed_images[n]
        body = item.get('body') if isinstance(item, dict) else str(item or '')
        if not body or not _mentions_cast(body):
            continue
        stripped = _strip_cast_scale(body)
        new_body = (stripped.rstrip() + ' ' + clause).strip()
        if new_body == body:
            continue
        if isinstance(item, dict):
            item['body'] = new_body
        else:
            parsed_images[n] = new_body
        report['locked'].append(n)
        changed = True

    if not changed:
        report['skipped'] = '没有一条 IMAGE 提到活物，无处可锁'
        return prompt_block, report

    # 重拼装走 _format_prompt_block：正文收口（_delivery_scrub）只在那里。
    return pp._format_prompt_block(parsed_images, parsed_videos), report


def check_cast_scale_lock(image_prompt, packet):
    """这一条 IMAGE 自己写了一个和锁不同的比例。

    与 `pp.check_anchor_scale_lock` 同形，但比的是物理比例记号而不是画幅占比桶。
    写手偶尔会在正文里 free-write 一句 "1:12 scale figurines"——注入的锁句在后面，
    同一段里两个比例，图像模型只会挑一句听。
    """
    errors = []
    locked = str((packet or {}).get('cast_scale') or '')
    if not locked or not image_prompt:
        return errors
    m = _RATIO_RE.search(locked)
    if not m:
        return errors
    expected = int(m.group(1))
    body = _strip_cast_scale(image_prompt)
    for found in _RATIO_RE.finditer(body):
        denom = int(found.group(1))
        if denom != expected:
            errors.append(
                f"IMAGE states figurine scale 1:{denom} but the packet locks 1:{expected} — "
                f"restate the locked ratio or drop the free-written one.")
            break
    return errors


# --------------------------------------------------------------------------
# 后置：逐拍对着真图订正
# --------------------------------------------------------------------------

_VERIFY_SYSTEM = """You are auditing two prompts of a shot-for-shot reconstruction sequence against the ACTUAL frames of the reference film they reproduce. You are shown the real reference frames for ONE beat.

You will receive:
  - IMAGE <n> prompt: the still frame that ends this beat (or, for beat 1, also the opening anchor).
  - VIDEO <n> prompt: the clip that moves through this beat.
  - The frame-by-frame observations already read off these same frames.

Your job: make the prompt text describe what the reference frames actually show.

[SCOPE — read this twice]
You may ONLY correct how the shot LOOKS and how the camera BEHAVES:
  - camera angle, pitch, bearing, height, shot scale, lens feel, depth of field
  - framing and composition: which third of the frame each element occupies, off-centre placement, horizon height, how much environmental margin surrounds the subject
  - the subject's on-screen SIZE relative to the frame, and the size relation between subject and its surroundings
  - spatial layout: what is in the left / centre / right zone, foreground / midground / background
  - materials, surface finish, weathering, grime, cracking, debris
  - vegetation, ground cover, clutter, terrain and sky actually present in frame
  - the living cast's appearance, clothing condition and where they physically stand (VIDEO only)
  - camera movement, shot count and cut rhythm inside the clip (VIDEO only)

You may NOT touch:
  - WHAT construction work happens in this beat, its operation, its tools, or its finished stage product
  - the progression of construction state (never move work earlier or later, never regress a finished state, never add or remove a milestone)
  - anything the prompt says about earlier or later beats
  - banned elements, sound/ASMR clauses, volume settings, or delivery boilerplate already present
The beat ladder was verified by a human. You are correcting the photography, not re-planning the build.

[RULES]
1. Keep the prompt in the same language it is written in (English descriptive prose). Keep roughly the same length and the same structure. Do not add commentary, headings, code fences, or bullet lists.
2. Change only what is actually wrong. If a slot already matches the frames, return it unchanged.
3. An IMAGE slot is a PERSON-FREE still anchor: never describe the cast, transient workers, floating tools, or human hands in it, and never add a sentence saying nobody is there either. If an IMAGE slot already names a person, remove them and describe what the empty space looks like instead. The cast belongs to the VIDEO slot, where they enter from off-frame and leave before the final moment.
4. Be concrete. "wider framing" is useless; "the hut occupies roughly one third of the frame height with open savannah margin on both sides" is a correction.
5. If the reference frames are too dark, blurred, or ambiguous to judge, leave the slot unchanged rather than guessing.

Respond with ONLY a JSON object, no code fence:
{"image_prompt": "<corrected or unchanged IMAGE text>", "video_prompt": "<corrected or unchanged VIDEO text>", "changed": ["short note per real correction"]}
Omit a key entirely if that slot was not supplied."""


def _extract_json(text):
    raw = (text or '').strip()
    raw = re.sub(r'^```(?:json)?\s*', '', raw)
    raw = re.sub(r'\s*```$', '', raw)
    try:
        return json.loads(raw)
    except ValueError:
        pass
    m = re.search(r'\{.*\}', raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except ValueError:
            return None
    return None


def _accept_rewrite(original, candidate):
    """订正稿的收口：空、过短（模型截断/敷衍）、或混进标记段的一律弃用，保留原稿。"""
    text = str(candidate or '').strip()
    if not text:
        return None
    if '===' in text:
        text = pp.prompt_block_from_output(text).strip() if hasattr(pp, 'prompt_block_from_output') else text
        if not text:
            return None
    original = str(original or '')
    if original and len(text) < len(original) * _MIN_REWRITE_RATIO:
        return None
    if text == original.strip():
        return None
    return text


def verify_beat_against_frames(config, beat_index, image_slot, image_prompt,
                               video_prompt, digests, frame_paths):
    """一次调用把这一拍的 IMAGE + VIDEO 一起对完。

    返回 (new_image_prompt|None, new_video_prompt|None, changed_notes)。
    调用失败或模型没给出可用订正时返回 (None, None, [])——调用方保留原稿。
    """
    if not frame_paths:
        return None, None, []
    images = (digests or {}).get('images') or {}
    videos = (digests or {}).get('videos') or {}

    parts = [f'Beat {beat_index} of the reference film. The attached frames are the real footage.']
    if image_prompt:
        parts.append(f'\n--- IMAGE {image_slot} prompt (the still that ends this beat) ---\n{image_prompt}')
        if images.get(image_slot):
            parts.append(f'\nFrame-by-frame observations for IMAGE {image_slot}:\n{images[image_slot]}')
    if video_prompt:
        parts.append(f'\n--- VIDEO {beat_index} prompt (the clip through this beat) ---\n{video_prompt}')
        if videos.get(beat_index):
            parts.append(f'\nFrame-by-frame observations for VIDEO {beat_index}:\n{videos[beat_index]}')
    parts.append('\nCorrect the photography only. Return the JSON object.')

    try:
        raw = pp._multimodal_chat(config, _VERIFY_SYSTEM, '\n'.join(parts),
                                  frame_paths[:_MAX_FRAMES_PER_CALL], max_tokens=2200)
    except Exception as exc:
        if sys.stdout:
            print(f'[OBSERVED] 第 {beat_index} 拍对帧订正调用失败（软退，保留原稿）: {exc}')
        return None, None, []

    data = _extract_json(raw)
    if not isinstance(data, dict):
        if sys.stdout:
            print(f'[OBSERVED] 第 {beat_index} 拍对帧订正返回无法解析（软退，保留原稿）')
        return None, None, []

    new_image = _accept_rewrite(image_prompt, data.get('image_prompt')) if image_prompt else None
    new_video = _accept_rewrite(video_prompt, data.get('video_prompt')) if video_prompt else None
    notes = [str(x).strip() for x in (data.get('changed') or []) if str(x).strip()]
    return new_image, new_video, notes


def ground_prompt_block_on_observed(config, prompt_block, beats_doc, job_dir,
                                    on_progress=None, digests=None):
    """后置那道：逐拍拿真图把整份 prompt_block 对一遍，就地订正。

    两条链路（极速 / 深度）都在合成收尾处调它，所以订正口径只有一份。任何一拍失败都
    只影响那一拍（保留原稿并记一条告警），绝不阻塞交付。

    返回 (prompt_block, report)；report 形如
    {'checked': n, 'corrected_images': [...], 'corrected_videos': [...],
     'notes': [...], 'skipped': [...]}
    """
    report = {'checked': 0, 'corrected_images': [], 'corrected_videos': [],
              'notes': [], 'skipped': []}
    beats = (beats_doc or {}).get('beats') or []
    if not prompt_block or not beats:
        return prompt_block, report

    if config.get('skipFootageGrounding'):
        report['skipped'].append('已配置 skipFootageGrounding，跳过后置实拍校对')
        if sys.stdout:
            print('[OBSERVED] ⚡ 已开启 skipFootageGrounding，跳过后置实拍校对以提速')
        return prompt_block, report

    if digests is None:
        digests = build_observed_digests(beats_doc, job_dir)
    video_frames = (digests or {}).get('video_frames') or {}
    if not video_frames:
        report['skipped'].append('没有可用的原片取证帧，后置对帧订正整体跳过')
        if sys.stdout:
            print('[OBSERVED] ⚠️ 没有可用的原片取证帧，后置对帧订正整体跳过')
        return prompt_block, report

    parsed_images, parsed_videos = pp._parse_prompt_slots(prompt_block)
    if not parsed_images and not parsed_videos:
        report['skipped'].append('prompt_block 解析不出槽位，后置对帧订正整体跳过')
        return prompt_block, report

    def _body(slots, n):
        item = slots.get(n)
        if isinstance(item, dict):
            return item.get('body') or ''
        return str(item or '')

    def _set_body(slots, n, text):
        item = slots.get(n)
        if isinstance(item, dict):
            item['body'] = text
        else:
            slots[n] = text

    total = len(beats)
    changed_any = False

    # 第 1 拍的取证帧同时管着 IMAGE 1（开场锚点）与 IMAGE 2（本拍终点）。锚点那张
    # 单独对一次：它的依据是起点帧，跟本拍终点不是同一个画面。
    if 1 in parsed_images:
        head_frames = ((digests or {}).get('image_frames') or {}).get(1)
        if head_frames:
            if on_progress:
                on_progress('replica_stage', {
                    'stage': 'compose',
                    'message': '正在拿原片起始帧对齐开场锚点（IMAGE 1）…',
                })
            new_img, _unused, notes = verify_beat_against_frames(
                config, 1, 1, _body(parsed_images, 1), '', digests, head_frames)
            report['checked'] += 1
            if new_img:
                _set_body(parsed_images, 1, new_img)
                report['corrected_images'].append(1)
                report['notes'].extend(f'IMAGE 1: {n}' for n in notes)
                changed_any = True

    tasks = []
    for i in range(1, total + 1):
        frames = video_frames.get(i)
        if not frames:
            continue
        image_slot = i + 1
        img_body = _body(parsed_images, image_slot) if image_slot in parsed_images else ''
        vid_body = _body(parsed_videos, i) if i in parsed_videos else ''
        if not img_body and not vid_body:
            continue
        tasks.append((i, image_slot, img_body, vid_body, frames))

    if tasks:
        import concurrent.futures
        max_workers = max(1, min(len(tasks), int(config.get('observedGroundingConcurrency', 3))))
        if max_workers <= 1:
            for i, image_slot, img_body, vid_body, frames in tasks:
                if on_progress:
                    on_progress('replica_stage', {
                        'stage': 'compose',
                        'message': f'正在拿原片实拍画面校对第 {i}/{total} 拍的提示词…',
                    })
                new_img, new_vid, notes = verify_beat_against_frames(
                    config, i, image_slot, img_body, vid_body, digests, frames)
                report['checked'] += 1
                if new_img:
                    _set_body(parsed_images, image_slot, new_img)
                    report['corrected_images'].append(image_slot)
                    report['notes'].extend(f'IMAGE {image_slot}: {n}' for n in notes)
                    changed_any = True
                if new_vid:
                    _set_body(parsed_videos, i, new_vid)
                    report['corrected_videos'].append(i)
                    report['notes'].extend(f'VIDEO {i}: {n}' for n in notes)
                    changed_any = True
        else:
            def _worker(item):
                i, image_slot, img_body, vid_body, frames = item
                new_img, new_vid, notes = verify_beat_against_frames(
                    config, i, image_slot, img_body, vid_body, digests, frames)
                return i, image_slot, new_img, new_vid, notes

            results = []
            completed = 0
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_item = {executor.submit(_worker, item): item for item in tasks}
                for future in concurrent.futures.as_completed(future_to_item):
                    item = future_to_item[future]
                    try:
                        res = future.result()
                        results.append(res)
                    except Exception as e:
                        results.append((item[0], item[1], None, None, []))
                    completed += 1
                    if on_progress:
                        on_progress('replica_stage', {
                            'stage': 'compose',
                            'message': f'正在拿原片实拍画面校对第 {completed}/{total} 拍的提示词（并发加速中）…',
                        })

            results.sort(key=lambda r: r[0])
            for i, image_slot, new_img, new_vid, notes in results:
                report['checked'] += 1
                if new_img:
                    _set_body(parsed_images, image_slot, new_img)
                    report['corrected_images'].append(image_slot)
                    report['notes'].extend(f'IMAGE {image_slot}: {n}' for n in notes)
                    changed_any = True
                if new_vid:
                    _set_body(parsed_videos, i, new_vid)
                    report['corrected_videos'].append(i)
                    report['notes'].extend(f'VIDEO {i}: {n}' for n in notes)
                    changed_any = True

    if not changed_any:
        return prompt_block, report

    # 重新拼装走 _format_prompt_block：正文收口（_delivery_scrub）在那里，绕过去就等于
    # 让模型的订正稿直接落进交付提示词（见 project_prompt_text_scrub_boundary）。
    rebuilt = pp._format_prompt_block(parsed_images, parsed_videos)
    if on_progress:
        on_progress('replica_stage', {
            'stage': 'compose',
            'message': (f'原片实拍校对完成：订正 {len(report["corrected_images"])} 条图片提示词、'
                        f'{len(report["corrected_videos"])} 条视频提示词'),
        })
    return rebuilt, report
