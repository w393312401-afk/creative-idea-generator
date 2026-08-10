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
import os
import re
import sys
import time

import prompt_pipeline as pp


# Pass A 的提示词版本号。改了 _PASS_A_SYSTEM 就必须改它，否则旧缓存会被当成新结果复用。
PASS_A_PROMPT_VERSION = 'v1'

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

_STAGE_LABELS_ZH = {
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

_PASS_A_SYSTEM = """You are a forensic frame reader. You are given still frames extracted from a video.

Your ONLY job is to record what is physically visible in each frame. You have no knowledge of what this video is about, what it is for, or what usually happens in videos like this.

HARD RULES
- Record only what the pixels show. If you cannot see it, it does not exist.
- Never infer a tool, material, worker, or operation from context, common sense, or industry habit. "There is fresh paint, so there must be a brush" is a forbidden inference.
- Never describe intent, purpose, story, or what will happen next.
- Describe completion extent spatially and concretely: "left two-thirds of the wall is coated, right third is bare" — never "partially done".
- If a frame is too blurry, dark, or occluded to read, say so and give it a low confidence.

OUTPUT
Return a JSON array, one object per frame given, in the same order:
[{
  "frame": "<the exact filename you were given>",
  "subject": "<what occupies the frame, one sentence>",
  "materials": ["<visible material surfaces>"],
  "tools": ["<visible tools, machines, equipment>"],
  "workers_present": true|false,
  "completion_extent": "<concrete spatial state of the work visible in this frame>",
  "traces": ["<visible marks left by work: dust, drips, cut lines, offcuts, stains>"],
  "confidence": 0.0-1.0
}]

Return the JSON array and nothing else. No prose, no code fences."""


_PASS_A_CONFIG_KEYS = (
    # 只放网关与调用参数。任何可能携带主题的键（theme / _project_key / dimensions /
    # title / outline…）都不在这里，这就是反注入的结构性保证。
    'baseUrl', 'apiKey', 'codexBaseUrl', 'codexApiKey',
    'model', 'reviewModel', 'reviewConcurrency',
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
            or cfg.get('model') or 'gemini-3.6-flash-high')


def _facts_cache_path(job_dir):
    return os.path.join(job_dir, '.frame_facts_cache.json')


def _load_facts_cache(job_dir):
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
    return data.get('frames') or {}


def _save_facts_cache(job_dir, frames):
    path = _facts_cache_path(job_dir)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump({'prompt_version': PASS_A_PROMPT_VERSION, 'frames': frames},
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


def estimate_pass_a_cost(overview, degraded=False, batch_size=_PASS_A_BATCH_SIZE):
    """给 UI 的开跑前预估。extract 完成后必须先把它摆给用户确认再烧钱。

    峰值帧复核现在默认开（见 `_peak_verify_model`），所以它那几次调用也必须出现在这里。
    默认加钱却不进预估，等于绕开了「先确认再烧钱」这道卡点本身。
    """
    frames = degraded_plan_frames(overview) if degraded else _planned_frames(overview)
    n = len(frames)
    batches = (n + batch_size - 1) // batch_size if n else 0
    peaks = peak_frame_names(overview)
    peak_batches = (len(peaks) + _PEAK_BATCH_SIZE - 1) // _PEAK_BATCH_SIZE if peaks else 0
    return {
        'frame_count': n,
        'total_extracted': len(((overview.get('review_sampling') or {}).get('frames') or [])),
        'batch_count': batches,
        'batch_size': batch_size,
        'degraded': bool(degraded),
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


def _parse_facts_array(raw, expected_names):
    """把模型回复解析成 {frame_name: fact}。名字对不上的按顺序兜底。"""
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
            # 模型偶尔会回 "frame 3" 或省略字段。按位置兜底，位置也超界就丢弃——
            # 丢一帧比把事实挂到错误的帧上安全得多。
            if idx < len(expected_names):
                name = expected_names[idx]
            else:
                continue
        out[name] = {
            'frame': name,
            'subject': str(item.get('subject') or '').strip(),
            'materials': [str(x).strip() for x in (item.get('materials') or []) if str(x).strip()],
            'tools': [str(x).strip() for x in (item.get('tools') or []) if str(x).strip()],
            'workers_present': bool(item.get('workers_present')),
            'completion_extent': str(item.get('completion_extent') or '').strip(),
            'traces': [str(x).strip() for x in (item.get('traces') or []) if str(x).strip()],
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
                        batch_size=_PASS_A_BATCH_SIZE):
    """Pass A。读 job_dir/video_overview.json，产出 job_dir/frame_facts.json。

    签名里没有 dimensions / brief / title / theme，且不接受 **kwargs——反注入靠这个
    形状来保证，别为了「顺手传个主题过去让它理解得更好」而破坏它。
    """
    overview_path = os.path.join(job_dir, 'video_overview.json')
    with open(overview_path, 'r', encoding='utf-8') as f:
        overview = json.load(f)

    entries = degraded_plan_frames(overview) if degraded else _planned_frames(overview)
    if not entries:
        raise ValueError('analysis_plan 为空：抽帧阶段没有产出任何可送审的帧')

    clean_config = _scrub_config_for_pass_a(config)
    model = _pass_a_model(config)
    cache = _load_facts_cache(job_dir)

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

    def _ask(batch, max_tokens, timeout):
        """一次视觉调用：喂这批帧，返回 {frame_name: fact}。解析失败抛 ValueError。"""
        paths = [e['frame_path'] for e in batch]
        names = [os.path.basename(p) for p in paths]
        # 先压再送：全尺寸 PNG 的 base64 会把请求体撑到十几 MB，这是超时的主要来源。
        small = pp._compress_frames_for_review(paths, max_side=768, quality=72)
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

        # 整批预算用尽后不再直接丢掉这十帧。多半是其中一两帧让模型写出了坏 JSON，
        # 而整批作废会把另外八帧本来读得好好的观察一起赔进去——缺帧在 Pass B 里
        # 表现为该窗口证据不足，节拍边界就落错。逐帧重来一次，救回能救的：
        # 单帧回复只有一个对象，坏 JSON 的概率和影响面都小一个量级。
        if len(batch) == 1:
            if sys.stdout:
                print(f'[REVERSE] 单帧连续解析失败，放弃: {names[0]}')
            return {}
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
                salvaged.update(_ask([entry], max_tokens=1024, timeout=90))
            except (ValueError, pp.ResponseTruncated):
                if sys.stdout:
                    print(f'[REVERSE] 逐帧重试仍失败，丢弃这一帧: '
                          f'{os.path.basename(entry["frame_path"])}')
        if sys.stdout:
            print(f'[REVERSE] 逐帧重试救回 {len(salvaged)}/{len(batch)} 帧')
        return salvaged

    def _on_done(_key, result):
        done_counter['n'] += len(result or {})
        if on_progress:
            on_progress('replica_stage', {
                'stage': 'review_frames',
                'message': f'逐帧事实提取 {done_counter["n"]}/{len(pending)}',
                'done': done_counter['n'],
                'total': len(pending),
            })

    if batches:
        results = pp._map_parallel(
            _run_batch,
            [(i, b) for i, b in enumerate(batches)],
            pp.review_concurrency(config),
            on_done=_on_done,
        )
        for chunk in results.values():
            cache.update(chunk or {})
        _save_facts_cache(job_dir, cache)

    facts = []
    for entry in entries:
        name = os.path.basename(entry['frame_path'])
        fact = dict(cache.get(name) or {'frame': name, 'confidence': 0.0})
        fact['timestamp'] = entry['timestamp']
        facts.append(fact)

    payload = {
        'prompt_version': PASS_A_PROMPT_VERSION,
        'model': model,
        'degraded': bool(degraded),
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
    return cfg.get('model') or 'gemini-3.6-flash-high'


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
    batches = [peak_names[i:i + _PEAK_BATCH_SIZE]
               for i in range(0, len(peak_names), _PEAK_BATCH_SIZE)]
    refined = {}
    for batch in batches:
        pp._raise_if_cancelled(on_progress)
        paths = [by_name[n]['frame_path'] for n in batch]
        small = pp._compress_frames_for_review(paths, max_side=1024, quality=82)
        listing = '\n'.join(f'{i + 1}. {n}' for i, n in enumerate(batch))
        # 复核是增强，不是门禁：这一批炸了就留着 Pass A 的结论继续走。默认开之后这条
        # 尤其要紧——一次解析失败若能掀掉整单，等于把已经付过钱的 Pass A 一起赔进去，
        # 而「没复核」的代价只是这几帧沿用 flash 的读数。
        try:
            raw = pp._multimodal_chat(
                clean_config, _PASS_A_SYSTEM,
                f'Read these {len(batch)} frames carefully. Crop-zoom mentally onto material '
                f'surfaces, tool contact points, and completion boundaries before answering.\n'
                f'{listing}\n\nReturn one JSON object per frame in the same order.',
                small, model=model, max_tokens=4096, timeout=240,
            )
            refined.update(_parse_facts_array(raw, batch))
        except pp.GenerationCancelled:
            raise
        except Exception as e:
            if sys.stdout:
                print(f'[REVERSE] 峰值帧复核失败，这批沿用 Pass A 的读数 {batch}: {e}')

    by_frame = {f['frame']: f for f in facts_payload.get('facts') or []}
    for name, fact in refined.items():
        if name in by_frame:
            ts = by_frame[name].get('timestamp')
            fact['timestamp'] = ts
            fact['verified_by'] = model
            by_frame[name] = fact
    facts_payload['facts'] = [by_frame[f['frame']] for f in facts_payload.get('facts') or []]
    facts_payload['peak_verified'] = len(refined)

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
                made = build_keyframe_collage([Path(p) for p in chunk], Path(out_path),
                                              columns=columns)
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

RULES
- Every change event must be claimed by exactly one beat, via source_event_ids. Zero unclaimed, zero double-claimed. A single beat may and often should claim SEVERAL adjacent events — events are detected state jumps, not production beats.
- A beat is a PRODUCTION MILESTONE, not a state jump. It must declare TWO OR THREE tightly coupled operations in package_operations that share ONE terminal product (for example ["cut", "fit", "fasten"] all producing one boarded ceiling). A beat carrying only one operation is under-scoped — widen its window and merge the adjacent events that belong to the same milestone.
- Never mix two different physical SYSTEMS in one beat (wiring and wall panels are separate milestones), but the two-to-three operations that jointly produce one milestone belong together in one beat.
- A beat must produce a full visible milestone, not a token patch. If two adjacent windows continue the same milestone, merge them.
- persistent_traces must list AT LEAST TWO visible marks this beat leaves behind.
- evidence_frames: list AT MOST THREE frames per beat — the one that best shows the start, the one that best shows the work, and the one that best shows the result. Do not echo every frame in the window; a long frame list is the single biggest cause of a reply that gets cut off before it finishes.
- Keep every prose field under thirty words. This is a structural ladder, not a description.
- Every claim must trace to a frame. Never write a tool, material, worker, or operation that no frame fact mentions.
- state_before / state_after must be concrete spatial completion extent, never "partially done".
- banned_elements: things a renovation of this type would plausibly involve but that appear in NO frame fact. Be generous here — this list is what stops the prompt writer from hallucinating later.
- stage: classify each beat as one of demolition | structural | rough_in | enclosure | surface | floor | fixtures | furnishing | reveal.
- Beats are ordered by time and must not overlap.

OUTPUT
Return one JSON object, no prose, no code fences:
{
  "video_duration_sec": <number>,
  "banned_elements": ["..."],
  "beats": [{
    "id": "B01",
    "start": <sec>, "end": <sec>,
    "stage": "<one of the nine>",
    "operation": "<the dominant physical operation naming this milestone>",
    "package_operations": ["<two or three tightly coupled operations sharing one terminal product>"],
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
        if f.get('completion_extent'):
            parts.append(f'extent={f["completion_extent"]}')
        if f.get('materials'):
            parts.append(f'materials={"/".join(f["materials"])}')
        if f.get('tools'):
            parts.append(f'tools={"/".join(f["tools"])}')
        if f.get('traces'):
            parts.append(f'traces={"/".join(f["traces"])}')
        parts.append(f'workers={"yes" if f.get("workers_present") else "no"}')
        parts.append(f'conf={f.get("confidence")}')
        lines.append(' | '.join(parts))
    return '\n'.join(lines)


def _events_digest(events):
    return '\n'.join(
        f'{e.get("event_id")}: start={e.get("start")}s peak={e.get("peak")}s end={e.get("end")}s '
        f'evidence={",".join(e.get("evidence_frames") or [])}'
        for e in events
    )


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

    user = (
        f'VIDEO DURATION: {duration} seconds\n\n'
        f'==================== FRAME FACTS ====================\n{_facts_digest(facts)}\n\n'
        f'==================== CHANGE EVENTS ====================\n{_events_digest(events)}\n'
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
        if sheet_paths:
            # `_multimodal_chat` 的 temperature 固定 0.1，比纯文本路径的 0.2 还低——
            # 聚类要的是稳定复现，不是花样，低一点正合适。
            return pp._multimodal_chat(
                config, _PASS_B_SYSTEM, prompt, sheet_paths,
                model=(config or {}).get('model'), max_tokens=16384, timeout=360,
            )
        return pp._chat(config, _PASS_B_SYSTEM, prompt, temperature=0.2,
                        max_tokens=16384, timeout=240)

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
        _renumber_beats(beats_doc)

        violations = validate_beats(beats_doc, overview)
        if not [v for v in violations if v['level'] == 'error']:
            break
        attempt += 1

    beats_doc['validation'] = violations
    out_path = os.path.join(job_dir, _BEATS_FILENAME)
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(beats_doc, f, ensure_ascii=False, indent=2)
    return beats_doc


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
    out.extend(_validate_required_fields(beats_doc, beats, schema))
    out.extend(_validate_event_coverage(beats, overview))
    out.extend(_validate_evidence_frames(beats, overview))
    out.extend(_validate_time_axis(beats, beats_doc.get('video_duration_sec')))
    out.extend(_validate_construction_order(beats))
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


def _validate_required_fields(beats_doc, beats, schema):
    out = []
    if not schema:
        return out
    root_required = schema.get('required') or []
    for key in root_required:
        if key not in beats_doc:
            out.append(_err('missing_root_field', f'缺少根字段 `{key}`'))

    beat_schema = ((schema.get('definitions') or {}).get('beat') or {})
    beat_required = beat_schema.get('required') or []
    beat_props = beat_schema.get('properties') or {}
    for beat in beats:
        bid = beat.get('id')
        for key in beat_required:
            problem = _missing_field_reason(beat, key, beat_props.get(key) or {})
            if problem:
                out.append(_err('missing_beat_field', f'{bid} {problem}', bid))
        for key in ('state_before', 'state_after'):
            text = str(beat.get(key) or '')
            if re.search(r'partial|部分完成|some of|一些', text, re.I):
                out.append(_warn('vague_state',
                                 f'{bid} 的 `{key}` 用了含糊表述，必须写具体空间完成范围：{text}', bid))
    return out


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


def _validate_evidence_frames(beats, overview):
    """证据帧必须真实存在，且时间戳落在本拍窗口内。"""
    out = []
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
        frames = beat.get('evidence_frames') or []
        if not frames:
            out.append(_err('no_evidence', f'{bid} 没有证据帧，但它断言了动作与结果', bid))
            continue
        start, end = _num(beat.get('start')), _num(beat.get('end'))
        for name in frames:
            if name not in ts_by_name:
                out.append(_err('evidence_missing',
                                f'{bid} 引用了不存在的证据帧 {name}', bid))
                continue
            ts = _num(ts_by_name[name], default=None) if ts_by_name[name] is not None else None
            if ts is not None and not (start - 0.5 <= ts <= end + 0.5):
                out.append(_warn('evidence_out_of_window',
                                 f'{bid} 的证据帧 {name}（t={ts}s）落在拍窗 [{start}, {end}] 之外', bid))
    return out


def _validate_time_axis(beats, duration):
    out = []
    prev_end = None
    for beat in beats:
        bid = beat.get('id')
        start, end = _num(beat.get('start')), _num(beat.get('end'))
        if end <= start:
            out.append(_err('bad_window', f'{bid} 的时间窗非法：start={start} end={end}', bid))
        if duration and end > _num(duration) + 0.5:
            out.append(_warn('window_overruns',
                             f'{bid} 的 end={end}s 超出视频时长 {duration}s', bid))
        if prev_end is not None and start + 0.01 < prev_end:
            out.append(_err('window_overlap',
                            f'{bid} 与上一拍时间窗重叠（start={start} < 上一拍 end={prev_end}）', bid))
        prev_end = end
    return out


def _validate_construction_order(beats):
    """施工依赖顺序 + 三条可机械判定的硬否决。

    其余硬否决（体积守恒、封闭空间成因、承重件临时支撑）必须看画面，不在这里判。
    """
    out = []
    staged = [(b, _STAGE_RANK.get(b.get('stage'))) for b in beats]

    # 阶段逆行。允许同级和小幅回退（真实改造确实会来回穿插），跨两级以上的倒挂才判。
    max_rank_so_far = 0
    max_rank_beat = None
    for beat, rank in staged:
        if rank is None:
            out.append(_warn('stage_unknown',
                             f'{beat.get("id")} 的 stage 无法识别：{beat.get("stage")}', beat.get('id')))
            continue
        if rank + 1 < max_rank_so_far:
            out.append(_err('stage_regression',
                            f'{beat.get("id")}（{stage_label(beat.get("stage"))}）出现在 '
                            f'{max_rank_beat}（{stage_label(_rank_to_stage(max_rank_so_far))}）之后，'
                            f'违反施工依赖顺序。先回去核对帧，误读帧远比不可能的施工顺序更常见。',
                            beat.get('id')))
        if rank > max_rank_so_far:
            max_rank_so_far, max_rank_beat = rank, beat.get('id')

    stages = {b.get('stage') for b, _ in staged}

    # 「布线/水管在封板之后」是硬否决榜首，而它恰好只差一级（enclosure=4 → rough_in=3），
    # 会被上面那条 ±1 容差整条放过。所以单独判：任何 rough_in 拍出现在 enclosure 拍
    # 之后都是错，不吃容差。
    first_enclosure = next((b for b, r in staged if b.get('stage') == 'enclosure'), None)
    if first_enclosure is not None:
        enclosure_start = _num(first_enclosure.get('start'))
        for beat, _rank in staged:
            if beat.get('stage') == 'rough_in' and _num(beat.get('start')) > enclosure_start:
                out.append(_err('rough_in_after_enclosure',
                                f'{beat.get("id")} 在 {first_enclosure.get("id")} 封板之后才做隐蔽工程，'
                                f'违反硬否决「布线/水管不得晚于遮盖它的板材」。先回去核对帧。',
                                beat.get('id')))

    # 供电链：有灯具/通电设备的拍，前面必须有 rough_in。
    if 'fixtures' in stages and 'rough_in' not in stages:
        lit = next((b.get('id') for b in beats if b.get('stage') == 'fixtures'), None)
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

WHAT YOU REWRITE
Only along the requested mutation axes: visual_subject, visible_details, visible_action, visible_result, state_before, state_after, persistent_traces, operation.

RULES
- Do not carry the old carrier's construction habits onto the new one. A bus does not get brick footings; a boat hull does not get drywall screwed to studs. Re-derive the operation that achieves the SAME stage milestone on the NEW carrier.
- state_before / state_after stay concrete and spatial.
- Recompute banned_elements from scratch for the new carrier. Do not inherit the old list.
- Never write digits or percent symbols; spell counts as words.

OUTPUT
Return one JSON object, no prose, no code fences:
{ "banned_elements": ["..."], "beats": [ { same fields as input, rewritten } ] }"""


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
                       max_tokens=16384, timeout=240)
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
        violations.extend(_validate_time_axis(variant['beats'], variant.get('video_duration_sec')))
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
                    'state_before', 'state_after', 'persistent_traces', 'operation'):
            if patch.get(key):
                new[key] = patch[key]
        # 证据帧降级为构图参考：变体不再对原片的事实负责，那些帧只提供机位与构图。
        new['reference_frames'] = list(src.get('evidence_frames') or [])
        new.pop('evidence_frames', None)
        out_beats.append(new)

    return {
        'video_duration_sec': beats_doc.get('video_duration_sec'),
        'source_video': beats_doc.get('source_video'),
        'variant_of': beats_doc.get('pipeline_id') or beats_doc.get('variant_of') or 'source',
        'mutation_axes': axes,
        # banned 按新载体重算，绝不继承旧列表——旧载体的「不存在物」对新载体毫无意义。
        'banned_elements': [str(x).strip() for x in (data.get('banned_elements') or []) if str(x).strip()],
        'beats': out_beats,
    }


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

        # 富字段绑定（2026-08-10）。此前这里只发 text + op，于是人工卡点上逐拍精修的
        # state_before / state_after / persistent_traces / visible_details 全部止步于
        # 本函数——用户核对的东西根本没进过模型上下文，"1:1 复刻"实际只锁住了拍数。
        # 这几个键走 build_outline_plan_block 既有的富字段通路（MATERIALS / LEAVES /
        # STATE），不新造第二条绑定链路。
        details = [str(x).strip() for x in (beat.get('visible_details') or []) if str(x).strip()]
        if details:
            entry['mat'] = details
        traces = [str(x).strip() for x in (beat.get('persistent_traces') or []) if str(x).strip()]
        if traces:
            # trace 在契约里是单串（zone/scope/trace 同组），列表要先合并。
            entry['trace'] = '; '.join(traces)
        for key in ('state_before', 'state_after'):
            value = str(beat.get(key) or '').strip()
            if value:
                entry[key] = value
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
    dimensions['reverse_engineered'] = True
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
