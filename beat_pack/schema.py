"""主题数据（设计圣经 + 逐段行）的形状、语言与一致性校验。

模型写出的 JSON 先过这里再进拼装：能确定性判断的错误（缺字段、引用了不存在的机位/组件、
英文提示词字段里混进中文、状态图文字里出现人物名词、机位看不见本段改动……）全部在这里拦住，
并带上段号，交给修复环节只重写出错的那几段，而不是整包重来。

问题（issue）统一为 dict：{'code', 'path', 'message', 'row'}，row 是 1 起的段号，整体性问题为 None。
"""
import json
import re

SCOPES = ('ext', 'int', 'both')
KINDS = ('construction', 'bridge', 'reveal')
REQUIRED_CAMERAS = ('open', 'work', 'bridge', 'reveal')
# 这两个机位按约定是"运动路线"，不能直接当静态状态图的机位。
DEFAULT_TRAVELLING = ('bridge', 'reveal')

KEY_RE = re.compile(r'^[a-z][a-z0-9_]{1,23}$')
SPACE_ID_RE = re.compile(r'^[a-z][a-z0-9_]{2,47}$')
CJK_RE = re.compile(r'[㐀-鿿豈-﫿　-〿＀-￯]')

# 与 video-beat-ladder 校验器的 image_prompt_people 同一份名词表与否定写法：
# 状态图默认无人，尺度用无人物件表达。校验器只给警告，这里当错误处理——否则它必然在交付里留着。
_PERSON_NOUNS = r'(?:man|men|woman|women|person|people|worker|workers|builder|human|figure|silhouette)'
_NEGATED_PERSON = (r'\b(?:no|without|zero|free of|free from|empty of|devoid of|never|not any|absence of)\s+(?:\w+\s+){0,2}'
                   + _PERSON_NOUNS + r'\b')

MIN_ACT_CHARS = 300
MIN_GEOMETRY_CHARS = 500
STAIR_TOLERANCE_M = 0.06


def issue(code, path, message, row=None):
    return {'code': code, 'path': path, 'message': message, 'row': row}


def has_cjk(text):
    return bool(CJK_RE.search(text or ''))


def person_nouns(text):
    """文字里（否定写法之外）出现的人物名词，小写去重排序。"""
    remaining = re.sub(_NEGATED_PERSON, ' ', text or '', flags=re.I)
    return sorted({m.group(0).lower() for m in re.finditer(r'\b' + _PERSON_NOUNS + r'\b', remaining, re.I)})


def _is_text(value, minimum=1):
    return isinstance(value, str) and len(value.strip()) >= minimum


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == value and abs(value) != float('inf')


def english_field(path, value, row=None, minimum=1, people=False):
    """英文提示词字段：非空、无中文；feeds_image=True 的字段还不得出现人物名词。"""
    if not _is_text(value, minimum):
        return [issue('required_text', path, f'需要至少 {minimum} 个字符的英文文本。', row)]
    out = []
    if has_cjk(value):
        out.append(issue('english_only', path, '这个字段会直接进入生成提示词，必须是英文，不能含中文字符或中文标点。', row))
    if people:
        nouns = person_nouns(value)
        if nouns:
            out.append(issue('person_in_state_text', path,
                             f'出现人物名词 {", ".join(nouns)}：状态图默认无人，尺度请改用无人物件（门、椅子、手推车、梯子）；'
                             '人物只在 act 里出现。', row))
    return out


# ── 模型回复 → JSON ─────────────────────────────────────────────

def parse_json_reply(text):
    """从模型回复里取出 JSON 对象：容忍代码围栏和前后多余的说明文字。失败抛 ValueError。"""
    if not isinstance(text, str) or not text.strip():
        raise ValueError('模型回复为空')
    body = text.strip()
    fence = re.search(r'```(?:json|JSON)?\s*\n(.*?)\n```', body, re.S)
    if fence and '{' in fence.group(1):
        body = fence.group(1).strip()
    start = body.find('{')
    if start < 0:
        raise ValueError('回复里没有 JSON 对象')
    depth, in_string, escaped = 0, False, False
    for pos in range(start, len(body)):
        char = body[pos]
        if in_string:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == '{':
            depth += 1
        elif char == '}':
            depth -= 1
            if depth == 0:
                value = json.loads(body[start:pos + 1])
                if not isinstance(value, dict):
                    raise ValueError('回复的 JSON 不是对象')
                return value
    raise ValueError('JSON 对象没有闭合（回复可能被截断）')


# ── 设计圣经 ────────────────────────────────────────────────────

def travelling_cameras(cameras):
    """运动路线机位的键集合：显式 travelling 标记优先，否则按约定 bridge/reveal。"""
    out = set()
    for key, cam in (cameras or {}).items():
        flag = cam.get('travelling') if isinstance(cam, dict) else None
        if flag is True or (flag is None and key in DEFAULT_TRAVELLING):
            out.add(key)
    return out


def _check_components(bible):
    out = []
    comps = bible.get('components')
    if not isinstance(comps, list) or not 10 <= len(comps) <= 32:
        return [issue('components_count', 'components', '需要 10–32 个状态组件（对象数组：key/scope/text）。')]
    seen = set()
    for i, comp in enumerate(comps):
        where = f'components[{i}]'
        if not isinstance(comp, dict):
            out.append(issue('component_shape', where, '组件必须是 {key, scope, text} 对象。'))
            continue
        key = comp.get('key')
        if not isinstance(key, str) or not KEY_RE.match(key):
            out.append(issue('component_key', f'{where}.key', 'key 需为小写字母开头的 2–24 位 [a-z0-9_]。'))
        elif key in seen:
            out.append(issue('component_key_duplicate', f'{where}.key', f'重复的组件 key：{key}'))
        else:
            seen.add(key)
        if comp.get('scope') not in SCOPES:
            out.append(issue('component_scope', f'{where}.scope', "scope 必须是 'ext'、'int' 或 'both'。"))
        out += english_field(f'{where}.text', comp.get('text'), minimum=10, people=True)
    scopes = [c.get('scope') for c in comps if isinstance(c, dict)]
    for needed in ('ext', 'int'):
        if scopes.count(needed) < 3:
            out.append(issue('component_scope_coverage', 'components', f"至少需要 3 个 scope='{needed}' 的组件，外景与室内才能分别累计状态。"))
    return out


def _check_cameras(bible):
    out = []
    cams = bible.get('cameras')
    if not isinstance(cams, dict) or not cams:
        return [issue('cameras_missing', 'cameras', '需要机位表（对象：键 → {scope, text}）。')]
    for key in REQUIRED_CAMERAS:
        if key not in cams:
            out.append(issue('camera_required', f'cameras.{key}', f'缺少必备机位 {key}。'))
    for key, cam in cams.items():
        where = f'cameras.{key}'
        if not isinstance(key, str) or not KEY_RE.match(key):
            out.append(issue('camera_key', where, '机位键需为小写字母开头的 2–24 位 [a-z0-9_]。'))
        if not isinstance(cam, dict):
            out.append(issue('camera_shape', where, '机位必须是 {scope, text} 对象。'))
            continue
        if cam.get('scope') not in SCOPES:
            out.append(issue('camera_scope', f'{where}.scope', "scope 必须是 'ext'、'int' 或 'both'。"))
        out += english_field(f'{where}.text', cam.get('text'), minimum=60, people=True)
    fixed_interior = [k for k, c in cams.items() if isinstance(c, dict) and c.get('scope') == 'int'
                      and k not in travelling_cameras(cams)]
    if len(fixed_interior) < 2:
        out.append(issue('camera_interior', 'cameras', '至少需要 2 个固定的室内机位（scope=int，非运动路线）。'))
    return out


def _check_spaces(bible):
    out = []
    spaces = bible.get('spaces')
    if not isinstance(spaces, list) or not spaces:
        return [issue('spaces_missing', 'spaces', '需要至少一个空间定义。')]
    seen = set()
    for i, space in enumerate(spaces):
        where = f'spaces[{i}]'
        if not isinstance(space, dict):
            out.append(issue('space_shape', where, '空间必须是对象。'))
            continue
        ident = space.get('id')
        if not _is_text(ident):
            out.append(issue('space_id', f'{where}.id', '空间需要 id。'))
        elif ident in seen:
            out.append(issue('space_id_duplicate', f'{where}.id', f'重复的空间 id：{ident}'))
        else:
            seen.add(ident)
        for field in ('shape', 'provenance'):
            if not _is_text(space.get(field)):
                out.append(issue('required_text', f'{where}.{field}', '需要非空文本。'))
        dims = space.get('dimensions_m')
        if not isinstance(dims, dict):
            out.append(issue('dimensions_object', f'{where}.dimensions_m', '需要 dimensions_m 对象。'))
            continue
        for field in ('width', 'length', 'height'):
            if field not in dims:
                out.append(issue('dimension_missing', f'{where}.dimensions_m.{field}', '必需尺寸键 width/length/height；未知填 null。'))
        for field, value in dims.items():
            if value is not None and (not _is_number(value) or value <= 0):
                out.append(issue('dimension_value', f'{where}.dimensions_m.{field}', '尺寸必须是正的米数或 null。'))
    return out


def _check_checks(bible):
    out = []
    checks = bible.get('checks')
    if not isinstance(checks, list) or len(checks) < 4:
        return [issue('checks_count', 'checks', '至少需要 4 条设计检查（id/scope/status/reason）。')]
    seen = set()
    for i, check in enumerate(checks):
        where = f'checks[{i}]'
        if not isinstance(check, dict):
            out.append(issue('check_shape', where, '检查必须是对象。'))
            continue
        ident = check.get('id')
        if not _is_text(ident):
            out.append(issue('check_id', f'{where}.id', '检查需要 id。'))
        elif ident in seen:
            out.append(issue('check_id_duplicate', f'{where}.id', f'重复的检查 id：{ident}'))
        else:
            seen.add(ident)
        if check.get('scope') != 'design':
            out.append(issue('check_scope', f'{where}.scope', "文本包的检查 scope 一律为 'design'。"))
        if check.get('status') not in ('pass', 'unverified'):
            out.append(issue('check_status', f'{where}.status',
                             "status 只能是 'pass' 或 'unverified'；设计有失败项就先修设计，不要交付 fail。"))
        if not _is_text(check.get('reason'), 10):
            out.append(issue('required_text', f'{where}.reason', '需要具体的检查理由（含数值核对过程）。'))
    return out


def _check_ledger(bible):
    out = []
    ledger = bible.get('ledger')
    if not isinstance(ledger, dict) or not ledger:
        return [issue('ledger_missing', 'ledger', '需要家具/设施账本（对象，键为实体名，值为带米数的尺寸）。')]
    for name, entry in ledger.items():
        if not isinstance(entry, dict):
            continue
        risers, riser, rise = entry.get('risers'), entry.get('riser_m'), entry.get('rise_m')
        if _is_number(risers) and _is_number(riser) and _is_number(rise):
            if abs(risers * riser - rise) > STAIR_TOLERANCE_M:
                out.append(issue('stair_arithmetic', f'ledger.{name}',
                                 f'踏步数×级高 = {risers}×{riser} = {risers * riser:.3f} m，与 rise_m={rise} 相差超过 {STAIR_TOLERANCE_M} m；'
                                 '请改级数、级高或层高，使三者自洽。'))
    return out


def _check_outline(bible, segments):
    out = []
    outline = bible.get('outline')
    cams = bible.get('cameras') if isinstance(bible.get('cameras'), dict) else {}
    if not isinstance(outline, list) or len(outline) != segments:
        got = len(outline) if isinstance(outline, list) else 'N/A'
        return [issue('outline_count', 'outline', f'outline 需要恰好 {segments} 项（当前 {got}）。')]
    moving = travelling_cameras(cams)
    bridges = [i for i, e in enumerate(outline, 1) if isinstance(e, dict) and e.get('kind') == 'bridge']
    for i, entry in enumerate(outline, 1):
        where = f'outline[{i - 1}]'
        if not isinstance(entry, dict):
            out.append(issue('outline_shape', where, 'outline 项必须是对象。', i))
            continue
        for field in ('label', 'stage'):
            if not _is_text(entry.get(field)):
                out.append(issue('required_text', f'{where}.{field}', '需要非空中文文本。', i))
        if entry.get('cam') not in cams:
            out.append(issue('outline_cam', f'{where}.cam', f"机位 {entry.get('cam')!r} 不在 cameras 里。", i))
        if entry.get('kind') not in KINDS:
            out.append(issue('outline_kind', f'{where}.kind', f'kind 必须是 {"/".join(KINDS)}。', i))
        for field in ('imgcam', 'cam_end'):
            if entry.get(field) is not None and entry[field] not in cams:
                out.append(issue('outline_cam', f'{where}.{field}', f'机位 {entry[field]!r} 不在 cameras 里。', i))
            elif entry.get(field) in moving:
                out.append(issue('outline_still_cam', f'{where}.{field}', '状态图机位不能是运动路线机位。', i))
        if entry.get('cam') in moving and not entry.get('imgcam'):
            out.append(issue('outline_imgcam_required', f'{where}.imgcam',
                             f"这一段用运动机位 {entry.get('cam')}，必须另给一个固定机位 imgcam 作为状态图构图。", i))
    if len(bridges) != 1:
        out.append(issue('outline_bridge', 'outline', f'必须恰好一段 kind=bridge（外到内的过门段），当前 {len(bridges)} 段。'))
    last = outline[-1] if isinstance(outline[-1], dict) else {}
    if last.get('kind') != 'reveal':
        out.append(issue('outline_reveal', f'outline[{segments - 1}].kind', '最后一段必须是 kind=reveal（无人终景）。', segments))
    elif last.get('cam') in moving and not last.get('cam_end'):
        out.append(issue('outline_cam_end', f'outline[{segments - 1}].cam_end',
                         '终景段用了运动机位：除了 imgcam（它第一帧的固定机位），还必须给 cam_end（固定机位）作为最后一张状态图的构图。', segments))
    if sum(1 for e in outline if isinstance(e, dict) and e.get('kind') == 'reveal') != 1:
        out.append(issue('outline_reveal', 'outline', '只能有一段 kind=reveal。'))
    return out


def validate_bible(bible, segments):
    """设计圣经的形状与语言校验。通过时返回 []。"""
    if not isinstance(bible, dict):
        return [issue('bible_shape', 'bible', '设计圣经必须是 JSON 对象。')]
    out = []
    for field in ('name', 'subtitle', 'title'):
        if not _is_text(bible.get(field)):
            out.append(issue('required_text', field, '需要非空中文文本。'))
    if _is_text(bible.get('name')) and len(bible['name'].strip()) > 12:
        out.append(issue('name_length', 'name', 'name 是项目名的前半（如“榴莲屋”），不超过 12 个字。'))
    if not isinstance(bible.get('space_id'), str) or not SPACE_ID_RE.match(bible.get('space_id') or ''):
        out.append(issue('space_id', 'space_id', 'space_id 需为小写字母开头的 3–48 位 [a-z0-9_]。'))
    out += english_field('geometry', bible.get('geometry'), minimum=MIN_GEOMETRY_CHARS, people=True)
    if _is_text(bible.get('geometry')) and not re.search(r'\d(?:\.\d+)?\s*m\b', bible['geometry']):
        out.append(issue('geometry_units', 'geometry', 'geometry 里需要带 m 单位的具体尺寸（外形、门洞、层高、净空等）。'))
    if _is_text(bible.get('geometry')) and not re.search(r'\bdoor\b', bible['geometry'], re.I):
        out.append(issue('geometry_door', 'geometry', 'geometry 需要写明唯一入口（door）的尺寸与门槛高度。'))
    out += english_field('weather', bible.get('weather'), minimum=10, people=True)
    out += english_field('ambience', bible.get('ambience'), minimum=10)
    out += _check_components(bible)
    out += _check_cameras(bible)
    out += _check_spaces(bible)
    openings = bible.get('openings')
    if not isinstance(openings, list) or not openings or not all(isinstance(o, dict) and _is_text(o.get('id')) for o in openings):
        out.append(issue('openings', 'openings', '需要开口清单（对象数组，每项有 id 与米数尺寸）。'))
    out += _check_ledger(bible)
    out += _check_checks(bible)
    for field, subfields in (('hook', ('first_conflict', 'focus', 'payoff')), ('work_scope', ('boundary', 'main_camera_visible', 'phases'))):
        value = bible.get(field)
        if not isinstance(value, dict):
            out.append(issue('required_object', field, f'需要对象，字段：{", ".join(subfields)}。'))
            continue
        for sub in subfields:
            if not _is_text(value.get(sub)):
                out.append(issue('required_text', f'{field}.{sub}', '需要非空中文文本。'))
    payoff = bible.get('payoff')
    if not isinstance(payoff, dict) or not _is_text(payoff.get('reason')):
        out.append(issue('required_object', 'payoff', '需要对象：{people_present: false, reason}。'))
    elif payoff.get('people_present') is not False:
        out.append(issue('payoff_people', 'payoff.people_present', '终景默认无人：people_present 必须为 false。'))
    if not _is_text(bible.get('concept'), 40):
        out.append(issue('required_text', 'concept', '需要创意方案说明（中文 Markdown，至少 40 字）。'))
    out += _check_outline(bible, segments)
    return out


# ── 逐段行 ──────────────────────────────────────────────────────

def _components_by_key(bible):
    return {c['key']: c for c in bible.get('components') or [] if isinstance(c, dict) and 'key' in c}


def validate_row(bible, row, index):
    """单段行的形状与语言校验（row 已合并 outline 里的 stage/cam/kind 等）。"""
    out = []
    comps = _components_by_key(bible)
    cams = bible.get('cameras') or {}
    if not isinstance(row, dict):
        return [issue('row_shape', f'rows[{index - 1}]', '行必须是对象。', index)]
    where = f'rows[{index - 1}]'
    for field in ('label', 'stage', 'vis'):
        if not _is_text(row.get(field)):
            out.append(issue('required_text', f'{where}.{field}', '需要非空中文文本。', index))
    if row.get('cam') not in cams:
        out.append(issue('row_cam', f'{where}.cam', f"机位 {row.get('cam')!r} 不在 cameras 里。", index))
    if row.get('kind') not in KINDS:
        out.append(issue('row_kind', f'{where}.kind', f'kind 必须是 {"/".join(KINDS)}。', index))
    unoccupied = row.get('actor') is False
    out += english_field(f'{where}.act', row.get('act'), row=index, minimum=MIN_ACT_CHARS)
    act = row.get('act') if isinstance(row.get('act'), str) else ''
    if act and not unoccupied and not re.search(r'\bbuilder\b', act, re.I):
        out.append(issue('act_actor', f'{where}.act', '有人的段落必须在 act 里写明 the builder 的动作。', index))
    if act and unoccupied:
        nouns = person_nouns(act)
        if nouns:
            out.append(issue('person_in_reveal', f'{where}.act',
                             f'无人终景的 act 出现人物名词 {", ".join(nouns)}；终景全程无人，只写空间与镜头的运动。', index))
    for field in ('sound', 'wear'):
        if row.get(field) is not None:
            out += english_field(f'{where}.{field}', row[field], row=index)
    upd = row.get('upd')
    if not isinstance(upd, dict):
        upd = {}
    if not upd:
        # 第 1 段是建立场景的开场：状态账本的初态就是它的画面，可以不更新组件，但要用 show 指出要看清的组件。
        if index > 1:
            out.append(issue('row_upd', f'{where}.upd', '每段至少更新一个组件（本段的可见结果）。', index))
        elif not row.get('show'):
            out.append(issue('row_show', f'{where}.show', '开场段不更新组件时，需要用 show 列出画面要看清的组件。', index))
    visible_hit = False
    cam_scope = (cams.get(row.get('cam')) or {}).get('scope') if isinstance(cams.get(row.get('cam')), dict) else None
    for key, text in upd.items():
        if key not in comps:
            out.append(issue('upd_key', f'{where}.upd.{key}', f'组件 {key!r} 不存在。', index))
            continue
        out += english_field(f'{where}.upd.{key}', text, row=index, minimum=15, people=True)
        comp_scope = comps[key].get('scope')
        if cam_scope in (None, 'both') or comp_scope == 'both' or comp_scope == cam_scope:
            visible_hit = True
    if upd and not visible_hit:
        out.append(issue('no_visible_change', f'{where}.upd',
                         f"本段机位 {row.get('cam')} 只看得见 {cam_scope}/both 范围的组件，但 upd 只改了别的范围的组件——"
                         '镜头里看不到任何变化。请改 upd 或让 outline 换一个看得见的机位。', index))
    show = row.get('show')
    if show is not None:
        if not isinstance(show, list) or any(k not in comps for k in show):
            bad = [k for k in show if k not in comps] if isinstance(show, list) else show
            out.append(issue('show_key', f'{where}.show', f'show 里有不存在的组件：{bad}', index))
    if row.get('kind') == 'reveal' and not unoccupied:
        out.append(issue('reveal_actor', f'{where}.actor', '终景段必须无人（actor=false）。', index))
    return out


def merge_outline(bible, content_rows, start):
    """把模型写的内容行（label/act/vis/upd/show/sound/wear）和 outline 里的结构字段合成完整行。

    stage/cam/imgcam/cam_end/kind 以 outline 为准，模型不需要也不允许在逐段阶段改写它们。"""
    outline = bible['outline']
    merged = []
    for offset, content in enumerate(content_rows):
        index = start + offset
        entry = outline[index - 1]
        row = dict(content) if isinstance(content, dict) else {}
        row.pop('index', None)
        row['stage'], row['cam'], row['kind'] = entry['stage'], entry['cam'], entry['kind']
        if not _is_text(row.get('label')):
            row['label'] = entry['label']
        for field in ('imgcam', 'cam_end'):
            if entry.get(field):
                row[field] = entry[field]
            else:
                row.pop(field, None)
        if entry['kind'] == 'reveal':
            row['actor'] = False
        else:
            row.pop('actor', None)
        merged.append(row)
    return merged


def validate_rows(bible, rows, first_index=1):
    """一批行的校验，段号从 first_index 起。"""
    out = []
    for offset, row in enumerate(rows):
        out += validate_row(bible, row, first_index + offset)
    return out


def apply_row(state, row):
    """把一段的 upd 累计进组件状态（返回新 dict）。"""
    new = dict(state)
    new.update(row.get('upd') or {})
    return new


def initial_state(bible):
    return {c['key']: c['text'] for c in bible.get('components') or []}


def to_theme(bible, rows):
    """设计圣经 + 行 → 拼装用的主题数据（compose.compose_package 的输入）。"""
    theme = {k: bible[k] for k in (
        'title', 'name', 'subtitle', 'space_id', 'geometry', 'weather', 'ambience', 'components', 'cameras',
        'spaces', 'openings', 'ledger', 'checks', 'hook', 'work_scope', 'payoff', 'concept') if k in bible}
    for optional in ('spatial_notes', 'framework_label', 'review_notes'):
        if bible.get(optional):
            theme[optional] = bible[optional]
    theme['rows'] = [dict(r) for r in rows]
    return theme


def validate_theme(bible, rows, segments):
    """整包校验：圣经 + 全部行 + 跨行一致性。通过时返回 []。"""
    out = validate_bible(bible, segments)
    if not isinstance(bible, dict) or any(i['code'] in ('outline_count',) for i in out):
        return out
    if len(rows) != segments:
        out.append(issue('rows_count', 'rows', f'需要 {segments} 段，当前 {len(rows)} 段。'))
    out += validate_rows(bible, rows)
    return out
