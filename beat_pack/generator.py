"""用模型写出一整包提示词的流水线：设计圣经 → 分批逐段 → 整包校验修复 → 一致性审阅。

模型只负责写数据（schema.py 规定的 JSON）；拼装、校验、导出都是确定性的。每一步的产物
立即交给回调落盘（on_bible / on_rows），因此中途失败、取消或服务重启后能从已接受的段继续，
不会把已经花过钱的结果丢掉。

chat 是注入进来的调用函数：chat(system, user, *, max_tokens, label) -> str。生产里它包的是
prompt_pipeline._chat（走 OpenAI 兼容网关，Claude 型号经 claudeBaseUrl），测试里是脚本化的假模型。
"""
import json
import re
import time

from . import compose, prompts, schema

BATCH_SIZE = 5
MAX_REPAIRS = 2
LLM_ATTEMPTS = 3
REPAIR_ROWS_PER_CALL = 8
DESIGN_MAX_TOKENS = 32000
ROWS_MAX_TOKENS = 16000
REVIEW_MAX_TOKENS = 12000
FATAL_HTTP = (400, 401, 403, 404, 422)

BIBLE_KEYS = ('name', 'subtitle', 'title', 'space_id', 'geometry', 'weather', 'ambience', 'components', 'cameras', 'spaces',
              'openings', 'ledger', 'checks', 'hook', 'work_scope', 'payoff', 'concept', 'spatial_notes', 'outline')


class Cancelled(Exception):
    """用户取消。由 chat 包装在收到取消信号时抛出，流水线不吞它。"""


class GenerationFailed(Exception):
    """流水线在某一阶段无法继续。issues 是该阶段最后一次校验的问题列表（可能为空）。"""

    def __init__(self, stage, message, issues=None):
        super().__init__(message)
        self.stage = stage
        self.message = message
        self.issues = issues or []


class GenResult:
    def __init__(self, bible, rows, notes, stats):
        self.bible, self.rows, self.notes, self.stats = bible, rows, notes, stats

    @property
    def theme(self):
        bible = dict(self.bible)
        if self.notes:
            bible['review_notes'] = list(self.notes)
        return schema.to_theme(bible, self.rows)


def _norm(text):
    return re.sub(r'\s+', ' ', str(text or '')).strip()


def _chunks(items, size):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _summarize(issues, limit=6):
    shown = [f"{i['path']}: {i['message']}" for i in issues[:limit]]
    more = f'（另有 {len(issues) - limit} 处）' if len(issues) > limit else ''
    return '；'.join(shown) + more


class Generator:
    def __init__(self, chat, request, *, bible=None, rows=None, on_event=None, on_bible=None, on_rows=None,
                 on_progress=None, cancelled=None, sleep=time.sleep, batch_size=BATCH_SIZE, max_repairs=MAX_REPAIRS):
        self.chat = chat
        self.theme_text = request['theme']
        self.framework = request.get('framework') or ''
        self.constraints = request.get('constraints') or ''
        self.segments = int(request['segments'])
        self.review = bool(request.get('review', True))
        self.bible = bible
        self.rows = list(rows or [])
        self.on_event = on_event or (lambda level, message: None)
        self.on_bible = on_bible or (lambda bible: None)
        self.on_rows = on_rows or (lambda rows: None)
        self.on_progress = on_progress or (lambda stage, message, fraction: None)
        self.cancelled = cancelled or (lambda: False)
        self.sleep = sleep
        self.batch_size = batch_size
        self.max_repairs = max_repairs
        self.notes = []
        self.stats = {'calls': 0, 'repairs': 0, 'review_issues': 0, 'review_fixed': 0}
        batches = -(-self.segments // self.batch_size)
        self._units = 1 + batches + 1 + (1 if self.review else 0)
        self._done = 0

    # ── 小工具 ──────────────────────────────────────────────────

    def _event(self, level, message):
        self.on_event(level, message)

    def _check_cancel(self):
        if self.cancelled():
            raise Cancelled()

    def _advance(self, stage, message):
        self._done += 1
        self.on_progress(stage, message, min(1.0, self._done / self._units))

    def _ask_json(self, label, system, user, max_tokens):
        """调一次模型并解析成 JSON 对象。网络类错误和解析失败各自重试，4xx 直接判死。"""
        prompt, last = user, '未知错误'
        for attempt in range(1, LLM_ATTEMPTS + 1):
            self._check_cancel()
            self.stats['calls'] += 1
            try:
                reply = self.chat(system, prompt, max_tokens=max_tokens, label=label)
            except Cancelled:
                raise
            except Exception as exc:
                code = getattr(exc, 'code', None)
                if code in FATAL_HTTP:
                    raise GenerationFailed(label, f'网关返回 HTTP {code}：请检查 claudeBaseUrl / claudeApiKey 与模型名（{exc}）') from exc
                last = f'模型调用失败：{exc}'
                self._event('warn', f'{label}：{last}（第 {attempt}/{LLM_ATTEMPTS} 次）')
                if attempt < LLM_ATTEMPTS:
                    self.sleep(min(30, 5 * attempt))
                continue
            try:
                return schema.parse_json_reply(reply)
            except ValueError as exc:
                last = f'回复不是可解析的 JSON：{exc}'
                self._event('warn', f'{label}：{last}（第 {attempt}/{LLM_ATTEMPTS} 次）')
                prompt = (user + f'\n\nYour previous reply could not be parsed ({exc}). '
                          'Reply again with ONLY the complete JSON object, no fence, no commentary.')
        raise GenerationFailed(label, last)

    def _state_before(self, rows, index):
        state = schema.initial_state(self.bible)
        for row in rows[:index - 1]:
            state = schema.apply_row(state, row)
        return state

    # ── 阶段 1：设计圣经 ──────────────────────────────────────────

    def design(self):
        self._check_cancel()
        self.on_progress('design', '正在设计载体、尺寸、状态账本、机位与逐段大纲…', self._done / self._units)
        system, user = prompts.design_messages(self.theme_text, self.framework, self.constraints, self.segments)
        data = self._ask_json('design', system, user, DESIGN_MAX_TOKENS)
        bible = data['bible'] if isinstance(data.get('bible'), dict) else data
        bible = self._valid_bible(bible)
        self.bible = bible
        self.on_bible(bible)
        self._event('info', f"设计圣经完成：{bible.get('title')}，{len(bible['components'])} 个状态组件，{len(bible['cameras'])} 个机位。")
        self._advance('design', '设计圣经完成')

    def _patch_bible(self, bible, patch):
        merged = dict(bible)
        for key, value in patch.items():
            if key in BIBLE_KEYS:
                merged[key] = value
        return merged

    def _valid_bible(self, bible):
        for attempt in range(self.max_repairs + 1):
            issues = schema.validate_bible(bible, self.segments)
            if not issues:
                return bible
            if attempt == self.max_repairs:
                raise GenerationFailed('design', '设计圣经多次修复后仍未通过校验：' + _summarize(issues), issues)
            self._event('warn', f'设计圣经有 {len(issues)} 处问题，请求修复（{attempt + 1}/{self.max_repairs}）：' + _summarize(issues, 3))
            self.stats['repairs'] += 1
            system, user = prompts.repair_bible_messages(bible, issues, self.segments)
            bible = self._patch_bible(bible, self._ask_json('design-repair', system, user, DESIGN_MAX_TOKENS))
        return bible

    # ── 阶段 2：分批逐段 ──────────────────────────────────────────

    def write_rows(self):
        while len(self.rows) < self.segments:
            self._check_cancel()
            first = len(self.rows) + 1
            last = min(self.segments, first + self.batch_size - 1)
            self.on_progress('rows', f'正在写第 {first}–{last} 段（共 {self.segments} 段）…', self._done / self._units)
            batch = self._write_batch(first, last)
            self.rows.extend(batch)
            self.on_rows(self.rows)
            self._event('info', f'第 {first}–{last} 段已通过校验并保存。')
            self._advance('rows', f'已完成 {len(self.rows)}/{self.segments} 段')

    def _collect(self, data, indexes):
        """把模型返回的 rows 对齐到要写的段号；对不上的段号缺席（由校验报 rows_shape）。"""
        got = data.get('rows')
        if not isinstance(got, list):
            return {}
        by_index = {}
        for position, item in enumerate(got):
            if not isinstance(item, dict):
                continue
            index = item.get('index')
            if not isinstance(index, int) or isinstance(index, bool):
                index = indexes[position] if len(got) == len(indexes) else None
            if index in indexes:
                by_index.setdefault(index, item)
        return by_index

    def _write_batch(self, first, last):
        indexes = list(range(first, last + 1))
        state = self._state_before(self.rows, first)
        system, user = prompts.rows_messages(self.bible, state, self.rows, first, last, self.segments, self.constraints)
        data = self._ask_json(f'rows {first}-{last}', system, user, ROWS_MAX_TOKENS)
        content = self._collect(data, indexes)
        work = list(self.rows) + [None] * len(indexes)
        for offset, index in enumerate(indexes):
            work[first - 1 + offset] = self._merge(index, content.get(index))
        for attempt in range(self.max_repairs + 1):
            issues = []
            for index in indexes:
                if content.get(index) is None and not (work[index - 1] or {}).get('act'):
                    issues.append(schema.issue('rows_shape', f'rows[{index - 1}]', '回复里缺少这一段（需要 index 对应的完整行）。', index))
            issues += schema.validate_rows(self.bible, [work[i - 1] for i in indexes], first)
            if not issues:
                return [work[i - 1] for i in indexes]
            if attempt == self.max_repairs:
                raise GenerationFailed('rows', f'第 {first}–{last} 段多次修复后仍未通过校验：' + _summarize(issues), issues)
            self._event('warn', f'第 {first}–{last} 段有 {len(issues)} 处问题，请求修复（{attempt + 1}/{self.max_repairs}）：' + _summarize(issues, 3))
            self.stats['repairs'] += 1
            self._repair_rows(work, issues, f'rows-repair {first}-{last}')
            content = {i: work[i - 1] for i in indexes}
        return [work[i - 1] for i in indexes]

    def _merge(self, index, item):
        if item is None:
            return {}
        return schema.merge_outline(self.bible, [item], index)[0]

    def _repair_rows(self, work, issues, label):
        """按问题里的段号，把出错的行分组发给模型重写，原地替换 work 里对应的行。"""
        by_row = {}
        for item in issues:
            if item.get('row'):
                by_row.setdefault(item['row'], []).append(item)
        for chunk in _chunks(sorted(by_row), REPAIR_ROWS_PER_CALL):
            first = chunk[0]
            state = self._state_before(work, first)
            done = [r for r in work[:first - 1] if r]
            system, user = prompts.repair_rows_messages(
                self.bible, state, done, [(i, work[i - 1] or {}) for i in chunk],
                [it for i in chunk for it in by_row[i]], self.segments, self.constraints)
            data = self._ask_json(label, system, user, ROWS_MAX_TOKENS)
            fixed = self._collect(data, chunk)
            for index in chunk:
                if index in fixed:
                    work[index - 1] = self._merge(index, fixed[index])

    # ── 阶段 3：整包校验 + 拼装校验 ─────────────────────────────────

    def assemble(self):
        """整包校验（圣经 + 全部行），再拼装 package 过一遍校验器。发现问题就定点修复，最多 max_repairs 轮。"""
        self._check_cancel()
        self.on_progress('assemble', '正在拼装并校验整包…', self._done / self._units)
        for attempt in range(self.max_repairs + 1):
            issues = schema.validate_theme(self.bible, self.rows, self.segments)
            if not issues:
                issues = self._package_issues()
            if not issues:
                self._advance('assemble', '整包校验通过')
                return
            if attempt == self.max_repairs:
                raise GenerationFailed('assemble', '整包多次修复后仍未通过校验：' + _summarize(issues), issues)
            self._event('warn', f'整包校验发现 {len(issues)} 处问题，请求修复（{attempt + 1}/{self.max_repairs}）：' + _summarize(issues, 3))
            self.stats['repairs'] += 1
            row_issues = [i for i in issues if i.get('row')]
            bible_issues = [i for i in issues if not i.get('row')]
            if bible_issues:
                system, user = prompts.repair_bible_messages(self.bible, bible_issues, self.segments)
                self.bible = self._patch_bible(self.bible, self._ask_json('assemble-repair', system, user, DESIGN_MAX_TOKENS))
                self.on_bible(self.bible)
            if row_issues:
                self._repair_rows(self.rows, row_issues, 'assemble-rows-repair')
                self.on_rows(self.rows)

    def _package_issues(self):
        """拼装 package 并跑校验器：结构错误当错误，状态图里的人物名词（校验器只给警告）也当错误。"""
        pkg = compose.compose_package(schema.to_theme(self.bible, self.rows))
        report = compose.validate_package(pkg, '.')
        out = [schema.issue(f"validator_{e['code']}", e['path'], e['message']) for e in report['errors']]
        out += [schema.issue('validator_people', w['path'], w['message']) for w in report['warnings'] if w['code'] == 'image_prompt_people']
        return out

    # ── 阶段 4：一致性审阅 ─────────────────────────────────────────

    def _verified(self, item):
        """审阅意见必须能在被引用的字段里原文找到 quote，否则丢弃——不让模型凭印象改包。"""
        if not isinstance(item, dict):
            return None
        quote, field, row = _norm(item.get('quote')), str(item.get('field') or ''), item.get('row')
        if len(quote) < 8:
            return None
        if isinstance(row, int) and not isinstance(row, bool):
            if not 1 <= row <= len(self.rows):
                return None
            cur = self.rows[row - 1]
            if field == 'act':
                text = cur.get('act', '')
            elif field == 'wear':
                text = cur.get('wear', '')
            elif field.startswith('upd.'):
                text = (cur.get('upd') or {}).get(field[4:], '')
            else:
                return None
        elif field.startswith('bible.') and field[6:] in self.bible:
            value = self.bible[field[6:]]
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        else:
            return None
        return item if quote in _norm(text) else None

    def audit(self):
        self._check_cancel()
        if not self.review:
            return
        self.on_progress('review', '正在让模型通读整包做一致性审阅…', self._done / self._units)
        try:
            system, user = prompts.review_messages(self.bible, self.rows, self.constraints)
            data = self._ask_json('review', system, user, REVIEW_MAX_TOKENS)
        except GenerationFailed as exc:
            # 审阅是加分项：失败不拖垮已经通过确定性校验的包，但要在审核记录里如实写明没审成。
            self._event('warn', f'一致性审阅未完成：{exc.message}')
            self.notes.append(f'一致性审阅：未完成（{exc.message}），本包只通过了确定性校验。')
            self._advance('review', '审阅未完成')
            return
        raw = [i for i in data.get('issues') or [] if isinstance(i, dict)]
        verified = [i for i in raw if self._verified(i)]
        self.stats['review_issues'] = len(verified)
        dropped = len(raw) - len(verified)
        if dropped:
            self._event('info', f'审阅提出 {len(raw)} 条意见，其中 {dropped} 条在原文里找不到所引用的文字，已丢弃。')
        errors = [i for i in verified if i.get('severity') == 'error' and isinstance(i.get('row'), int)]
        unresolved = [i for i in verified if i not in errors or not isinstance(i.get('row'), int)]
        if errors:
            unresolved += self._apply_review_fixes(errors)
        summary = _norm(data.get('summary'))
        if summary:
            self.notes.append('Claude 一致性审阅：' + summary)
        for item in unresolved:
            where = f"第 {item['row']} 段" if isinstance(item.get('row'), int) else '圣经'
            self.notes.append(f"审阅遗留（{item.get('severity') or 'note'}）· {where} · {item.get('field')}：{_norm(item.get('problem'))}")
        self._advance('review', f'审阅完成：{len(verified)} 条有效意见，{self.stats["review_fixed"]} 条已修复')

    def _apply_review_fixes(self, errors):
        """把审阅里的 error 级意见交给模型改写对应段；改后若破坏了校验就整段退回，意见留作遗留项。"""
        issues = [schema.issue('review', f"rows[{i['row'] - 1}].{i.get('field')}",
                               f"{_norm(i.get('problem'))} Suggested fix: {_norm(i.get('fix'))} (quoted: \"{_norm(i.get('quote'))}\")", i['row'])
                  for i in errors]
        before = {i['row']: self.rows[i['row'] - 1] for i in errors}
        work = list(self.rows)
        try:
            self._repair_rows(work, issues, 'review-fix')
        except GenerationFailed as exc:
            self._event('warn', f'按审阅意见修复失败：{exc.message}')
            return errors
        left = []
        for item in errors:
            index = item['row']
            problems = schema.validate_rows(self.bible, [work[index - 1]], index)
            if problems:
                work[index - 1] = before[index]
                left.append(item)
                self._event('warn', f'第 {index} 段按审阅意见改写后未通过校验，已退回原文。')
            else:
                self.stats['review_fixed'] += 1
        if len(left) < len(errors):
            self.rows = work
            self.on_rows(self.rows)
        return left

    # ── 总入口 ────────────────────────────────────────────────────

    def run(self):
        if self.bible is None:
            self.design()
        else:
            self._done += 1                                   # 续跑：设计阶段早已完成
        self._done += len(self.rows) // self.batch_size
        self.write_rows()
        self.assemble()
        self.audit()
        if self.stats['review_fixed']:
            # 审阅改写了段落：交付前再过一遍确定性校验，保证交付的就是校验过的版本。
            issues = schema.validate_theme(self.bible, self.rows, self.segments) or self._package_issues()
            if issues:
                raise GenerationFailed('assemble', '审阅修复后的整包未通过校验：' + _summarize(issues), issues)
        return GenResult(self.bible, self.rows, self.notes, self.stats)
