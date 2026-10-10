"""把「主题数据」组装成 video-beat-ladder 的 beat_package，并落成可导入的交付文件。

这里只做确定性的拼装，不调用模型：主题数据（几何、状态账本、机位、逐段行）进，
`beat_package.json`、说明文档和 `完整提示词.txt` 出。模型负责写数据，拼装规则在这里固定，
这样同一份数据永远得到同一份提示词，校验器也只需要校验这一条产线。

主题数据（theme）形状见 schema.py；本模块只假设它已经通过 schema.validate_theme。
"""
import copy
import importlib.util
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

MIN_SEGMENTS = 20
MAX_SEGMENTS = 40

ACTOR_ID = 'builder_A'
ACTOR = ('The builder is one adult with a square face, medium olive-tan skin, short dark brown hair swept to the side and a short trimmed beard, '
         'lean-athletic build, designed at 1.75 m world height. Baseline clothing is a mustard-yellow canvas work shirt with sleeves rolled to the forearms, '
         'dark grey work trousers, brown leather work boots and bare hands without gloves or hat; any later change is named in the segment. ')
ACTOR_STATE = 'Mustard-yellow canvas shirt with sleeves rolled to forearms, dark grey trousers, brown leather boots, bare hands; clean and dry at start.'
DEFAULT_WEAR = 'The mustard-yellow shirt, dark grey trousers, brown boots and bare hands persist from earlier segments. '
SCALE_SENTENCE = ('Keep the 1.75 m design height stable against the declared door, counters and bed at comparable depth; '
                  'projection changes from crouching or camera distance are natural. ')
HEADER = ('Photorealistic vertical 9:16 original process-story clip, ten seconds. Bind IMAGE {a} as the starting state and IMAGE {b} as the ending state; '
          'both are unoccupied environment references and the video itself continues smoothly between them without dissolves, teleportation, '
          'scene scaling or hidden bulk completion. ')
UNOCCUPIED_FOOTAGE = 'Entirely unoccupied footage with no people, hands, silhouettes or body reflections. '
HIDDEN_PARTS = ' Parts of the structure outside this view keep their previously completed state and are not redesigned.'

SKILL_NAME = 'video-beat-ladder'
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_VALIDATOR_REL = Path('scripts') / 'validate_beat_package.py'


class ThemeError(ValueError):
    """主题数据无法拼装（形状不对、引用了不存在的机位/组件、段数越界）。"""


# ── 主题数据形状归一 ──────────────────────────────────────────────

def _as_component(item):
    if isinstance(item, dict):
        return item['key'], item['scope'], item['text']
    key, scope, text = item
    return key, scope, text


def _as_camera(item):
    if isinstance(item, dict):
        return item['scope'], item['text']
    scope, text = item
    return scope, text


def normalize_theme(theme):
    """深拷贝并把组件/机位统一成 (key, scope, text) / (scope, text) 元组。

    模型产出的是 JSON（对象），手写主题文件里是元组——两种形状都认，下游只见一种。"""
    if not isinstance(theme, dict):
        raise ThemeError('主题数据必须是对象')
    out = copy.deepcopy(theme)
    try:
        out['components'] = [_as_component(c) for c in out.get('components') or []]
        cams = out.get('cameras') or {}
        out['cameras'] = {k: _as_camera(v) for k, v in cams.items()}
    except (KeyError, TypeError, ValueError) as exc:
        raise ThemeError(f'组件或机位形状不对：{exc!r}') from exc
    out['rows'] = [dict(r) for r in out.get('rows') or []]
    # 模型只写创意本身；包标题和变体条目由数据推出，手写主题文件里已有的值不覆盖。
    title = out.get('title') or (out.get('variant') or {}).get('title') or ''
    out.setdefault('title_long', f"{title} · {len(out['rows'])}段 Omni 完整文字包")
    out.setdefault('variant', {'id': 'N01', 'title': title, 'origin': 'creative_design'})
    return out


def visible_state(components, state, scope):
    """某个机位（scope）看得见的组件状态拼成一段。both 组件处处可见。"""
    return ' '.join(state[key] for key, sc, _ in components
                    if sc == 'both' or sc == scope or scope == 'both')


# ── beat_package ────────────────────────────────────────────────

def compose_package(theme):
    """主题数据 → beat_package（dict）。不写盘。

    线性链：N 段视频对应 N+1 张状态图，第 i 段绑定图 i → 图 i+1。状态图默认无人。
    组件账本逐段累计：每段的 `upd` 覆盖对应组件的整段文字，之后所有机位看到的都是更新后的状态。
    """
    T = normalize_theme(theme)
    comps, cams, rows = T['components'], T['cameras'], T['rows']
    count = len(rows)
    if not MIN_SEGMENTS <= count <= MAX_SEGMENTS:
        raise ThemeError(f'段数 {count} 超出 {MIN_SEGMENTS}–{MAX_SEGMENTS}')
    comp_keys = {key for key, _, _ in comps}
    for i, row in enumerate(rows, 1):
        for field in ('cam', 'imgcam', 'cam_end'):
            if row.get(field) is not None and row[field] not in cams:
                raise ThemeError(f'第 {i} 段 {field}={row[field]!r} 不在机位表里')
        unknown = [k for k in list(row.get('upd', {})) + list(row.get('show') or []) if k not in comp_keys]
        if unknown:
            raise ThemeError(f'第 {i} 段引用了不存在的组件：{sorted(set(unknown))}')
    space_id = T['space_id']
    state = {key: text for key, _, text in comps}

    def image_prompt(st, cam_key):
        scope, text = cams[cam_key]
        hidden = '' if scope == 'both' else HIDDEN_PARTS
        return ('Photorealistic vertical 9:16 still of an original fictional dwelling conversion. Entirely unoccupied: no people, hands, body parts, '
                'reflections or silhouettes. ' + text + ' ' + T['geometry'] + ' Current static state: ' + visible_state(comps, st, scope) + hidden
                + ' ' + T['weather'] + ' This is one still state, not a montage or action sequence. No captions, grid, labels or engineering marks.')

    def still_cam(row):
        return row.get('imgcam') or row['cam']

    images = [{'id': 1, 'actor_ids': [], 'people_allowed': False, 'space_id': space_id,
               'camera_family': still_cam(rows[0]), 'prompt': image_prompt(state, still_cam(rows[0]))}]
    segments = []
    wear = DEFAULT_WEAR
    for i, row in enumerate(rows, 1):
        before = dict(state)
        state.update(row.get('upd', {}))
        scope, cam_text = cams[row['cam']]
        has_actor = row.get('actor', True)
        if has_actor:
            # 服装状态一旦被某段声明就继续沿用，直到下一段改写：漏写 wear 不能让外套悄悄脱掉。
            wear = row.get('wear') or wear
            actor_text = ACTOR + wear + SCALE_SENTENCE
        else:
            actor_text = UNOCCUPIED_FOOTAGE
        shown = row.get('show')
        if shown is None:
            shown = list(row.get('upd', {}))
        ending = ' '.join(state[k] for k in shown)
        prompt = (HEADER.format(a=i, b=i + 1) + cam_text + ' ' + T['geometry'] + ' ' + actor_text
                  + 'Opening state: ' + visible_state(comps, before, scope) + ' Action and visible continuity: ' + row['act']
                  + ' Required ending state: ' + (ending or 'unchanged environment with the declared camera result.')
                  + ' Sound design: ' + row.get('sound', T['ambience'])
                  + ' No narration, music, captions or labels. Preserve all previously installed layers, sources and fixed objects except the explicitly changed state.')
        segments.append({
            'id': i, 'source_beat_ids': [], 'origin': 'creative_design', 'ladder_stage': row['stage'], 'operation': row['label'],
            'duration_sec': 10, 'start_image_id': i, 'end_image_id': i + 1,
            'actor_ids': [ACTOR_ID] if has_actor else [], 'space_id': space_id, 'camera_family': row['cam'],
            'kind': row.get('kind', 'construction'), 'prompt': prompt,
            'visible_change': {'region': row['label'], 'before': visible_state(comps, before, scope),
                               'after': visible_state(comps, state, scope), 'proof': row['vis']},
            'design_reason': row.get('why', row['stage'])})
        end_cam = row.get('cam_end', row['cam']) if i == count else still_cam(rows[i])
        images.append({'id': i + 1, 'actor_ids': [], 'people_allowed': False, 'space_id': space_id,
                       'camera_family': end_cam, 'prompt': image_prompt(state, end_cam)})

    actors = [{'id': ACTOR_ID, 'reference_source': 'planned: original full-body, face and hand/cuff references; none generated, reviewed, uploaded or bound',
               'appearance': ACTOR, 'height_m': 1.75, 'height_provenance': 'designed: story-design value 1.75 m, not measured from video evidence',
               'state_baseline': ACTOR_STATE}]
    checks = copy.deepcopy(T['checks'])
    if not any(c.get('id') == 'render_identity_scale_space' for c in checks):
        checks.append({'id': 'render_identity_scale_space', 'scope': 'design', 'status': 'unverified',
                       'reason': '尚未渲染，人物参考与空间图均为 planned；渲染前不得宣称身份、尺度与空间连续性通过。'})
    return {
        'version': 1, 'title': T['title_long'], 'mode': 'prompts', 'source': None, 'evidence_file': None, 'observed_beats': [],
        'variants': [T['variant']], 'production_segments': segments, 'images': images,
        'spatial_contract': {
            'status': 'planned', 'reference_height_m': 1.75, 'reference_height_provenance': '原创人物设计值，不是源视频实测。',
            'actors': actors, 'reference_actor_id': ACTOR_ID, 'spaces': T['spaces'], 'geometry_text': T['geometry'],
            'cameras': {k: v[1] for k, v in cams.items()}, 'checks': checks, 'openings': T['openings'], 'furniture_ledger': T['ledger']},
        'render_review': {'status': 'not_run'},
        'reference_bindings': {'status': 'planned', 'submitted_assets': [],
                               'note': f'无实际生成或绑定；{count + 1}张无人状态图保持独立。入口若只收首尾帧，需另做人物动作锚点再核验。'},
        'visual_plan': {'hook': T['hook'], 'work_scope': T['work_scope'], 'payoff': T['payoff']},
        'production_budget': {'segments': count, 'framework_base_segments': 25, 'duration_per_segment_sec': 10, 'raw_material_sec': count * 10,
                              'note': '素材预算不是成片时长；入口时长与参考能力未核验。'},
        'provenance': {'framework': T.get('framework_label') or '用户提供的节拍阶梯推进框架', 'classification': '原创设计；未审阅源视频帧，observed_beats 为空。'},
        'editorial_review': {'status': 'design_text_reviewed', 'pixel_review': 'not_run'},
    }


# ── 说明文档 ────────────────────────────────────────────────────

def _wear_changes(rows):
    """服装状态改写的段号与内容（审核记录里逐项列出，避免"换装"只藏在 act 里）。"""
    changes, last = [], None
    for i, row in enumerate(rows, 1):
        if row.get('actor', True) is False:
            continue
        wear = (row.get('wear') or '').strip()
        if wear and wear != last:
            changes.append((i, wear))
            last = wear
    return changes


def derive_concept_md(T):
    hook, scope, payoff = T['hook'], T['work_scope'], T['payoff']
    title = T.get('title') or T['variant']['title']
    return (f"# {title}\n\n原创设计，不是源片观察。\n\n**首屏**：{hook['first_conflict']}\n\n**主要改造范围**：{scope['boundary']}\n\n"
            f"**回报**：{hook['payoff']}\n\n**终景**：{payoff.get('reason', '')}\n")


def derive_spatial_md(T):
    lines = ['# 空间与比例（原创设计值）', '', '- 人物设计身高 H=1.75 m（设计值，不是实测）。']
    for space in T['spaces']:
        dims = ', '.join(f'{k}={v} m' for k, v in (space.get('dimensions_m') or {}).items() if v is not None)
        lines.append(f"- 空间 {space.get('id')}：{space.get('shape', '')}；{dims}")
        for item in space.get('relative_constraints') or []:
            lines.append(f'  - {item}')
    for opening in T['openings']:
        parts = ', '.join(f'{k}={v}' for k, v in opening.items() if k != 'id')
        lines.append(f"- 开口 {opening.get('id')}：{parts}")
    for key, value in (T['ledger'] or {}).items():
        parts = ', '.join(f'{k}={v}' for k, v in value.items()) if isinstance(value, dict) else str(value)
        lines.append(f'- 账本 {key}：{parts}')
    for note in T.get('spatial_notes') or []:
        lines.append(f'- {note}')
    lines.append('- 未验证：渲染比例；制作前必须先画平面+剖面。')
    return '\n'.join(lines) + '\n'


def derive_review_md(T):
    lines = ['# 审核记录', '', '- 观察：source=null，无源片帧审阅。', '- 设计检查（spatial_contract.checks）：']
    for check in T['checks']:
        lines.append(f"  - `{check.get('id')}`｜{check.get('status')}｜{check.get('reason', '')}")
    changes = _wear_changes(T['rows'])
    if changes:
        lines.append('- 人物服装状态（第 N 段起沿用，直到下一次改写）：')
        for index, wear in changes:
            lines.append(f'  - 第 {index} 段起：{wear.strip()}')
    lines += ['- 渲染：not_run；人物参考 planned。']
    for note in T.get('review_notes') or []:
        lines.append(f'- {note}')
    return '\n'.join(lines) + '\n'


def render_documents(theme, pkg):
    """说明文档：文件名 → 内容。创意方案/空间与比例/审核记录优先用模型写的，缺了就由数据推出。"""
    T = normalize_theme(theme)
    rows, segs = T['rows'], pkg['production_segments']
    count = len(segs)
    ladder = '\n'.join(
        f"| {s['id']:02d} | {s['ladder_stage']} | {s['operation']} | 图{s['start_image_id']}→图{s['end_image_id']} | {s['kind']} | {rows[s['id'] - 1]['vis']} |"
        for s in segs)
    return {
        '创意方案.md': T.get('concept') or derive_concept_md(T),
        '空间与比例.md': T.get('spatial_md') or derive_spatial_md(T),
        '审核记录.md': T.get('review_md') or derive_review_md(T),
        '生成阶梯.md': ('# 生成阶梯（原创设计，source=null）\n\n| 段 | 框架阶段 | 内容 | 锚点 | 类型 | 可见变化 |\n|---|---|---|---|---|---|\n' + ladder
                     + f'\n\n{count} 段 × 10 秒 = {count * 10} 秒素材预算，不是成片时长；框架基础预算为 25 段，本包按住所功能展开。\n'),
        '人物参考计划.md': ('# 人物参考计划（planned，均未生成/绑定）\n\n- id: ' + ACTOR_ID + '\n- 外观: ' + ACTOR + '\n- 初始状态: ' + ACTOR_STATE
                       + '\n- 需要的参考: 全身、脸部、手与袖口。\n- 状态: 第' + str(count) + '段为无人揭晓，actor_ids 为 []。\n'),
    }


# ── 校验器桥接与落盘 ──────────────────────────────────────────────

def validator_path():
    """video-beat-ladder 校验器：仓库内置 skills/ > 环境变量 BEAT_LADDER_SKILL_DIR > ~/.claude/skills。

    与项目对其它技能包的取值顺序一致：随代码版本化的仓库内置包优先，机器本地目录只在缺失时兜底。"""
    candidates = [_PROJECT_ROOT / 'skills' / SKILL_NAME]
    env = os.environ.get('BEAT_LADDER_SKILL_DIR', '').strip()
    if env:
        candidates.append(Path(os.path.expanduser(env)))
    candidates.append(Path.home() / '.claude' / 'skills' / SKILL_NAME)
    for root in candidates:
        path = root / _VALIDATOR_REL
        if path.is_file():
            return path
    raise FileNotFoundError('找不到 video-beat-ladder 校验器（skills/video-beat-ladder/scripts/validate_beat_package.py）')


_VALIDATOR_MODULE = None


def load_validator():
    """按路径载入校验器模块（它是独立脚本，不在包路径上）。进程内只载一次。"""
    global _VALIDATOR_MODULE
    if _VALIDATOR_MODULE is None:
        path = validator_path()
        spec = importlib.util.spec_from_file_location('beat_ladder_validator', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _VALIDATOR_MODULE = module
    return _VALIDATOR_MODULE


def validate_package(pkg, base='.'):
    """跑结构校验，返回校验器的报告 dict（errors/warnings 都是 {code, path, message}）。"""
    return load_validator().validate_package(pkg, base)


def write_package_files(theme, pkg, out_dir, *, replace=True):
    """写 beat_package.json、说明文档、validation.json，并在无结构错误时导出 delivery-v1/。

    返回 (report, delivery_dir 或 None)。校验有错误时不导出，调用方据 report['errors'] 决定怎么办。
    replace=True 会先清掉旧的 delivery-v1——导出器自己拒绝覆盖，而同一任务重跑要能原地重出。"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / 'beat_package.json').write_text(json.dumps(pkg, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    for name, text in render_documents(theme, pkg).items():
        (out / name).write_text(text, encoding='utf-8')
    report = validate_package(pkg, out)
    delivery = out / 'delivery-v1'
    if replace:
        shutil.rmtree(delivery, ignore_errors=True)
    if report['errors']:
        (out / 'validation.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        return report, None
    load_validator().export_package(pkg, delivery)
    report = dict(report, export_dir=str(delivery.resolve()))
    (out / 'validation.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return report, delivery


def complete_prompt_text(pkg):
    """不落盘地得到「完整提示词.txt」的内容（用于校验/预览）。"""
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / 'delivery'
        load_validator().export_package(pkg, target)
        return (target / '完整提示词.txt').read_text(encoding='utf-8')
