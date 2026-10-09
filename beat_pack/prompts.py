"""提示词包生成器给模型的指令。

四种调用：design（设计圣经 + 逐段大纲）、rows（一批逐段行）、repair（按校验问题重写）、review（整包一致性审阅）。
指令是英文（模型读规则最稳），字段里的中文部分在规则里逐项点名。few-shot 样例来自人工做完并通过校验器的
松果屋包（examples/*.json），只示范格式与细致程度——规则里明确禁止照抄内容。
"""
import json
from pathlib import Path

from .schema import MIN_ACT_CHARS

_EXAMPLES = Path(__file__).resolve().parent / 'examples'

DEFAULT_CONSTRAINTS = ('成品必须是完整可居住的住所：睡眠、起居、烹饪、如厕、洗浴五项功能齐全，'
                       '并在施工阶梯里交代各自的来源（水、电、排水、通风、取暖）。')

STAGE_SKELETON = """\
STAGE SKELETON (stage names are fixed Chinese strings; reuse them exactly unless a REFERENCE FRAMEWORK defines other stages)
  0 奇观与目标        — 1 clip: arrival; scale and the opening conflict; no construction yet.
  1 入口从轮廓到可开合  — 3-5 clips: clear and mark the door line, cut the opening, frame it, hang a door that opens and closes.
  2 外部支持与住宅前景  — 3-5 clips: access platform and stair, power, water, hoist, material stock, ground and lights.
  3 入室与引光        — 4-6 clips: THE bridge (exterior -> interior through the new door), then clear the interior and let daylight in.
  4 地面基层分层       — 2-3 clips: floor build-up (services under the floor, insulation, boards).
  5 墙顶包覆与三面填充   — 4 clips: pre-laid services, insulation, membranes, battens, roof.
  6 三面木饰面         — 3 clips: wall and roof cladding, trim, window fittings.
  7 生活布置与点亮      — 5 clips: kitchen, bathroom and toilet, bed, living furniture, lights and heating on.
  8 无人回报          — 1 clip: unoccupied reveal that pays off the opening."""

COMMON_RULES = """\
You are the lead designer of a series of vertical 9:16 "process-story" videos. In each video an unusual, oversized, ORIGINAL FICTIONAL carrier
(a giant husk, shell, vessel, structure...) is converted step by step into a complete, livable dwelling. The story is told as N ten-second clips
chained by N+1 still "state images": clip i starts at IMAGE i and ends at IMAGE i+1. A text-to-video model renders each clip and a text-to-image
model renders each state image. Code assembles your JSON into those prompts, so every sentence you write is read literally by an image/video model.

HOW YOUR DATA BECOMES PROMPTS
- `geometry` (English) is pasted into EVERY image prompt and EVERY clip prompt. It is the single source of truth for dimensions; every number
  used anywhere else must agree with it.
- `components` is a cumulative STATE LEDGER. Each component has a key, a scope (ext = visible from exterior cameras, int = visible from interior
  cameras, both = visible from both) and the text of its state AT THE START (usually "No X exists..." or a raw/unfinished description). A clip's
  `upd` REPLACES the full text of the listed components from that clip onward. A camera only prints the components whose scope it can see.
- `cameras`: each has a scope and text; prompts open with the camera text. Fixed cameras keep the same lens and framing whenever reused so the model
  sees the same place. A camera with travelling:true describes a moving route; it is used for clips only, never as the composition of a still image.
- State images are UNOCCUPIED: no people, hands, silhouettes or reflections. Scale is shown with objects (door, chair, wheelbarrow, ladder, woodpile).
  Never use the words man, men, woman, women, person, people, worker, workers, builder, human, figure or silhouette in geometry, weather, component
  texts, camera texts or `upd` texts (the only exception is a negation such as "no people"). The builder appears only inside a clip's `act`.
- The one character is "the builder": a 1.75 m adult whose look is fixed by the system. Never describe the builder's face or build; clothing changes
  go in `wear`.

LANGUAGE
- English (natural prose, no Chinese characters or full-width punctuation): geometry, weather, ambience, every components[].text, every cameras{}.text,
  every clip `act`, `upd`, `sound`, `wear`.
- Chinese: name, subtitle, title, concept, hook/work_scope/payoff values, outline labels and stages, `vis`, checks[].reason, spaces[].provenance,
  spaces[].relative_constraints, spatial_notes.

OUTPUT
Reply with ONE JSON object and nothing else: no markdown fence, no commentary before or after. Use double quotes, no trailing commas, no comments."""

DESIGN_RULES = """\
DESIGN PRINCIPLES
1. Carrier and scale. Pick a believable real-world size so a 1.75 m person can work on it. Treat the carrier as a hard designed building shell (give
   wall/husk thickness). State height, width, base/footing, wall thickness and what the shell is made of.
2. One entrance. A single door is cut into the carrier: 0.90 m wide x 1.95 m high (door leaf 0.85 m x 1.90 m) with its sill at a stated height above the
   footing/ground; say what lies below the sill until the floor is built. Everything big (tanks, panels, plywood, furniture) must be able to pass this
   door, be pre-cut or split in halves, or be built in place; never smuggle an item through an opening it cannot fit.
3. Do the arithmetic in `ledger` and `checks`. Stairs: risers x riser height = floor rise (keep the field rise_m and make the three numbers agree within
   0.06 m). Standing headroom >= 2.0 m wherever someone walks. Corridors/rings >= 1.2 m clear. Counter 0.6 m deep x 0.9 m high, bed about 1.9 x 1.4 m.
   A square room cannot be bigger than the circle that holds it. Check floors against the sill height so the finished floor meets the sill.
4. Complete dwelling. Follow the user's REQUIREMENTS for the finished product. If they ask for a dwelling, give a place for sleep, living, cooking,
   toilet and bathing, each with a source and route for water, power, drainage, ventilation and heat, laid BEFORE the layer that hides them.
   Lamps are lit only after power exists; a stove is lit only after its flue exists.
5. Material logistics. A `stock` component lists what is staged on site; consumed stock shrinks as it is used; waste leaves or is reused. Nothing is
   installed that was never delivered.
6. Components: 16-24 entries covering at least shell, door, access (landing/stair), power, stock, ground, sky (scope both), window, floor, walls, ceiling,
   inner circulation, utilities, bath, kitchen, bedroom and soft furnishings/lights. Starting states describe what is absent or raw ("No window exists...").
   Use keys in lowercase letters, digits, underscore. At least 3 components with scope ext and at least 3 with scope int.
7. Cameras: `open` (exterior, low and wide, near an object that gives scale, NOT a person), `work` (fixed exterior three-quarter wide that shows the whole
   work area), `bridge` (travelling: exterior approach -> doorway -> settles at the sill looking in), `reveal` (travelling, unattended interior route), plus
   a fixed exterior camera aimed at the work face and at least three fixed interior cameras suited to your plan. Each camera text gives height, distance,
   direction, what must be legible, and for fixed cameras "keep the same lens and framing on every return".
8. Hook. The first image must contain a concrete visual conflict (what is wrong/impossible/unfinished and where), and the final reveal must pay it off.
9. Outline: exactly N entries following the stage skeleton. Exactly one `bridge` clip (the exterior-to-interior crossing) and the last clip is the
   unoccupied `reveal`. Each entry: label (Chinese task name), stage (Chinese stage name), cam (a key in cameras), kind (construction|bridge|reveal) and,
   where needed, imgcam (a FIXED camera key used for the still image that starts a clip whose camera is travelling; every clip that uses a travelling
   camera needs one, including the last clip). The last entry also needs cam_end (a fixed camera for the final still, N+1) when its cam is travelling.
   Every clip must change something its camera can see.
10. Originality. The carrier is an original fictional design; do not name real brands, real people or real buildings.
The example below shows the FORMAT and LEVEL OF PRECISION only. It is a different carrier; invent every fact fresh for the user's carrier and do not
copy its carrier, materials, climate or numbers."""

BIBLE_SHAPE = """\
JSON SHAPE (all keys required unless marked optional)
{
 "name": "榴莲屋", "subtitle": "雨林住所", "title": "巨型干榴莲壳 → 雨林住所",
 "space_id": "durian_dwelling",
 "geometry": "English, all fixed dimensions, entrance, finished levels...",
 "weather": "English", "ambience": "English",
 "components": [{"key": "shell", "scope": "ext|int|both", "text": "English starting state"}],
 "cameras": {"open": {"scope": "ext", "text": "English"}, "bridge": {"scope": "ext", "text": "English", "travelling": true}},
 "spaces": [{"id": "...", "shape": "...", "dimensions_m": {"width": 0, "length": 0, "height": 0}, "provenance": "中文", "relative_constraints": ["中文"]}],
 "openings": [{"id": "south_door", "width_m": 0.9, "height_m": 1.95, "fixed": true}],
 "ledger": {"stair": {"risers": 15, "riser_m": 0.193, "rise_m": 2.9}},
 "checks": [{"id": "stair_rise", "scope": "design", "status": "pass|unverified", "reason": "中文，含数值核对"}],
 "hook": {"first_conflict": "中文", "focus": "中文", "payoff": "中文"},
 "work_scope": {"boundary": "中文", "main_camera_visible": "中文", "phases": "中文"},
 "payoff": {"people_present": false, "reason": "中文"},
 "concept": "中文 Markdown 创意方案",
 "spatial_notes": ["中文，可选"],
 "outline": [{"label": "中文", "stage": "中文", "cam": "key", "kind": "construction", "imgcam": "optional key", "cam_end": "optional key"}]
}"""

ROWS_RULES = f"""\
PER-CLIP CONTRACT
- Each clip has ONE clear visible result, located where the clip's own camera can see it (the outline fixes camera, stage and kind; do not change them).
  `upd` lists the components that change, each with the COMPLETE new state text (it replaces the old text, so repeat everything that is still true).
- `act` (English, one paragraph, {MIN_ACT_CHARS}-900 characters): the builder's actual work in the order main working view -> contact close-up -> return
  to the same fixed view ("a contact view shows ..."), naming the tool, the contact point, what the builder stands on, where each material comes from
  (it must already exist in the ledger/stock) and where it ends up. Close-ups show only local contact changes; they never finish a whole wall behind the
  viewer's back. One coherent job per ten seconds: do not cut three windows or fit a whole run of shelves in one clip.
- Accumulation: a covered wall never reverts to bare; a board is never both installed and lying on the ground; fixed objects, doors, pipes under floors
  and furniture outside the camera frame all persist. Services (conduit, water sleeves, drain, vent, flue) are laid before the layer that hides them.
  Lamps only after power, stove only after its flue.
- `vis` (Chinese, one sentence): what a viewer can verify at the end of the clip.
- `show` (optional list of component keys): the states to restate as the clip's required ending state; defaults to the keys in `upd`.
- `sound` (optional English): contact sounds that match the visible tools and materials.
- `wear` (optional English): write the builder's complete clothing/stain sentence ONLY in a clip where it changes (jacket on/off, gloves, harness,
  dust, resin); it carries on automatically into later clips. Never describe the face or build.
- Clip 1 establishes the scene: the builder arrives and the low camera shows scale; the ledger's starting states are the picture, so `upd` may be empty
  but `show` must list the components to make legible.
- The bridge clip: approach -> threshold -> settle inside; keep the door box and one landmark in view throughout; show how the builder gets across the
  height difference at the sill; describe the first interior view consistently with the int components.
- The final reveal clip: nobody appears. `act` starts with "No person appears.", moves the camera through the finished home along the plan, ends at a window,
  and ends with "Nothing is added, moved or replaced." `upd` may change only light/sky; `show` lists the components that prove the dwelling functions."""

REVIEW_RULES = """\
You audit a finished text package before it is released. Report only REAL problems that you can prove by quoting the package. Do not invent
problems, do not restyle, do not report taste. Check:
 1. Numbers: the same dimension stated differently in two places; an item that cannot pass the door/opening it is said to pass; stair or headroom arithmetic.
 2. Surfaces: stains, damp, rot, temporary supports present at the start that are never cleaned, covered or kept in the final state, or a clean surface in
    the final state that was never cleaned.
 3. Sources and order: a material, tool, power or water source used before it exists; services hidden before they were laid; lamp lit before power; stove lit
    before its flue; a board both installed and still lying on the ground.
 4. Action load: one ten-second clip finishing more than one coherent job.
 5. Clothing: `wear` contradicting a later `act` (for example a jacket put on in clip 5 but described as off in clip 8 without a clip that removes it).
 6. Requirements: a required dwelling function with no place, source or route.
 7. People: any person noun in a state text or camera text (state images must be unoccupied).
Return {"issues":[{"row": 12 or null, "field": "act | upd.<key> | wear | bible.<field>", "severity": "error|note", "quote": "an EXACT substring
copied from the cited field", "problem": "...", "fix": "..."}], "summary": "中文 3-6 句总评，如实写：查了什么、没发现什么、残留风险"}.
Use severity "error" only for contradictions that would make the finished clips wrong. A package with no real problem returns "issues": []."""


def _json(value, indent=None):
    return json.dumps(value, ensure_ascii=False, indent=indent)


def _example(name):
    return json.loads((_EXAMPLES / name).read_text(encoding='utf-8'))


def _requirements(constraints):
    return (constraints or '').strip() or DEFAULT_CONSTRAINTS


def design_messages(theme, framework, constraints, segments):
    system = '\n\n'.join([
        COMMON_RULES, DESIGN_RULES, STAGE_SKELETON, BIBLE_SHAPE,
        'FORMAT EXAMPLE (different carrier; copy structure and precision, never content):\n' + _json(_example('bible_example.json')),
    ])
    parts = [f'THEME / CARRIER (from the user):\n{theme.strip()}',
             f'REQUIREMENTS for the finished product (from the user):\n{_requirements(constraints)}',
             f'N = {segments} clips ({segments + 1} state images). The outline must have exactly {segments} entries.']
    if (framework or '').strip():
        parts.append('REFERENCE FRAMEWORK (from the user; derive beat order, stage names and pacing from it where it differs from the skeleton, '
                     'keeping exactly one bridge and an unoccupied reveal; do NOT copy its carrier, only its structure):\n' + framework.strip())
    parts.append('Write the design bible JSON now.')
    return system, '\n\n'.join(parts)


def _bible_digest(bible):
    """逐段调用用的圣经摘要：几何、账本初态、机位全文与结构数据；不带 concept/checks 这类只给人看的字段。"""
    keep = ('name', 'subtitle', 'title', 'space_id', 'geometry', 'weather', 'ambience', 'components', 'cameras',
            'spaces', 'openings', 'ledger', 'hook', 'work_scope', 'payoff')
    return {k: bible[k] for k in keep if k in bible}


def _row_digest(rows):
    lines = []
    for i, row in enumerate(rows, 1):
        keys = ','.join((row.get('upd') or {}).keys()) or '-'
        lines.append(f"{i}. [{row.get('stage')}|{row.get('cam')}|{row.get('kind')}] {row.get('label')} — {row.get('vis')} (upd: {keys})")
    return '\n'.join(lines)


def rows_messages(bible, state, done_rows, first, last, segments, constraints=''):
    """写第 first..last 段（含）。state 是前面各段累计后的组件状态。"""
    system = '\n\n'.join([
        COMMON_RULES, ROWS_RULES,
        'ROW SHAPE: {"rows":[{"index": 12, "label": "中文", "act": "English", "vis": "中文", "upd": {"component_key": "complete English state"}, '
        '"show": ["component_key"], "sound": "English", "wear": "English"}]}  (show, sound, wear optional)',
        'FORMAT EXAMPLE (different carrier; copy structure and precision, never content):\n' + _json(_example('rows_example.json')),
    ])
    outline = bible['outline']
    wanted = [dict(index=i, **outline[i - 1]) for i in range(first, last + 1)]
    parts = [
        f'REQUIREMENTS for the finished product:\n{_requirements(constraints)}',
        'DESIGN BIBLE (fixed; every number and name below is binding):\n' + _json(_bible_digest(bible)),
        'FULL OUTLINE:\n' + _json(outline),
        f'COMPONENT STATES AT THE END OF CLIP {first - 1} (what exists now):\n' + _json(state),
    ]
    if done_rows:
        parts.append('CLIPS ALREADY WRITTEN (summary):\n' + _row_digest(done_rows))
        parts.append('LAST WRITTEN CLIP IN FULL:\n' + _json({k: done_rows[-1].get(k) for k in ('label', 'act', 'upd', 'wear') if done_rows[-1].get(k)}))
    parts.append(f'Write clips {first}-{last} of {segments} for these outline entries (stage, cam and kind are fixed):\n{_json(wanted)}\n'
                 f'Return {{"rows": [...]}} with exactly {last - first + 1} rows, indexes {first}..{last}, in order.')
    return system, '\n\n'.join(parts)


def format_issues(issues):
    lines = []
    for item in issues:
        where = f"[clip {item['row']}] " if item.get('row') else ''
        lines.append(f"- {where}{item['path']}: {item['message']}")
    return '\n'.join(lines)


def repair_bible_messages(bible, issues, segments):
    system = '\n\n'.join([COMMON_RULES, DESIGN_RULES, STAGE_SKELETON, BIBLE_SHAPE])
    user = (f'This design bible failed automatic validation (N = {segments} clips).\n\nISSUES:\n{format_issues(issues)}\n\n'
            f'CURRENT BIBLE:\n{_json(bible)}\n\n'
            'Return a JSON object containing ONLY the top-level fields that must change, each as its complete corrected value '
            '(if you return components, cameras or outline, return them in full). Do not change anything that is not implicated by the issues.')
    return system, user


def repair_rows_messages(bible, state, done_rows, bad_rows, issues, segments, constraints=''):
    """bad_rows：[(index, 合并后的完整行)]；state：第一条坏行之前累计的组件状态。"""
    system = '\n\n'.join([
        COMMON_RULES, ROWS_RULES,
        'ROW SHAPE: {"rows":[{"index": 12, "label": "中文", "act": "English", "vis": "中文", "upd": {"component_key": "complete English state"}, '
        '"show": ["component_key"], "sound": "English", "wear": "English"}]}  (show, sound, wear optional)',
    ])
    first = bad_rows[0][0]
    content = [{k: v for k, v in dict(row, index=index).items() if k in ('index', 'label', 'act', 'vis', 'upd', 'show', 'sound', 'wear')}
               for index, row in bad_rows]
    parts = [
        f'REQUIREMENTS for the finished product:\n{_requirements(constraints)}',
        'DESIGN BIBLE (fixed):\n' + _json(_bible_digest(bible)),
        'FULL OUTLINE:\n' + _json(bible['outline']),
        f'COMPONENT STATES AT THE END OF CLIP {first - 1}:\n' + _json(state),
    ]
    if done_rows:
        parts.append('CLIPS BEFORE THE FIRST BROKEN ONE (summary):\n' + _row_digest(done_rows))
    parts.append(f'These clips failed validation (N = {segments}).\n\nISSUES:\n{format_issues(issues)}\n\nCURRENT CLIPS:\n{_json(content)}\n\n'
                 'Return {"rows": [...]} with the corrected version of exactly these clips (same indexes, in order), changing only what the issues require '
                 'and keeping every other fact intact.')
    return system, '\n\n'.join(parts)


def review_messages(bible, rows, constraints=''):
    system = '\n\n'.join([COMMON_RULES, REVIEW_RULES])
    content = [{k: v for k, v in dict(row, index=i).items() if k in ('index', 'label', 'act', 'upd', 'wear', 'kind')}
               for i, row in enumerate(rows, 1)]
    user = (f'REQUIREMENTS for the finished product:\n{_requirements(constraints)}\n\n'
            'DESIGN BIBLE:\n' + _json(_bible_digest(bible)) + '\n\nCLIPS:\n' + _json(content) + '\n\nAudit the package now.')
    return system, user
