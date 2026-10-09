"""提示词包生成器测试用的假模型与合法数据。

假模型按调用类型（设计 / 逐段 / 修复 / 审阅）返回合法 JSON；测试用 queue() 往某一类调用里塞一条
预先写好的回复（坏 JSON、带中文的行、编造的审阅意见……）来演练各条失败与修复路径。
"""
import copy
import json
import re
from pathlib import Path

EXAMPLES = Path(__file__).resolve().parent.parent / 'beat_pack' / 'examples'


def example_bible():
    """样例圣经（松果屋，28 段）——本身就是通过 schema.validate_bible 的合法数据。"""
    return json.loads((EXAMPLES / 'bible_example.json').read_text(encoding='utf-8'))


def content_row(bible, index, *, marker=''):
    """模型会写的内容行（index/label/act/vis/upd/show）。按 outline 选一个本段机位看得见的组件来更新。"""
    entry = bible['outline'][index - 1]
    scope = bible['cameras'][entry['cam']]['scope']
    comps = bible['components']
    if entry['kind'] == 'reveal':
        act = ('No person appears. The camera starts at the door looking round the finished home, follows the plan past the kitchen, the bathroom '
               'door and the stair, and ends at the window over the bed while the warm lamps glow steadily and the low sun crosses the sill. '
               'The stove ticks softly, the kitchen counter, the bath door and the bed stay exactly where they were built, and the stair rail '
               'catches the last light. Nothing is added, moved or replaced.')
        if marker:
            act += ' ' + marker
        return {
            'index': index, 'label': entry['label'], 'act': act, 'vis': '终景无人，沿平面读完所有生活功能。',
            'upd': {'sky': f'Low golden sun fills the windows while the lamps glow warm (reveal {index}).'}, 'show': ['sky'],
        }
    key = next(c['key'] for c in comps if scope == 'both' or c['scope'] in ('both', scope))
    act = (f'The builder works at step {index} on the supported work surface: the main view shows the tool, the material and the area being changed, '
           'a contact view shows the tool biting into the material at the exact joint, and the camera returns to the same fixed view where the finished '
           f'detail is now readable. Material comes from the staged stock and nothing else is added. {marker}').strip()
    return {
        'index': index, 'label': entry['label'], 'act': act, 'vis': f'第 {index} 段可见变化：{entry["label"]}。',
        'upd': {key: f'State of {key} after step {index}: the change is complete, tidy and still consistent with every earlier step.'},
        'show': [key],
        **({'wear': 'A dark green insulated work jacket is worn over the shirt with wool gloves and snow boots. '} if index == 1 else {}),
    }


class FakeModel:
    """chat(system, user, *, max_tokens, label) 的脚本化替身。"""

    def __init__(self, bible=None, review=None):
        self.bible = bible if bible is not None else example_bible()
        self.review_reply = review if review is not None else {'issues': [], 'summary': '未发现需要修改的矛盾。'}
        self.calls = []                 # 每次调用的 (kind, label)
        self.prompts = []               # 每次调用的 (kind, system, user)
        self.queued = {}                # kind -> [待消费的回复或异常]
        self.on_call = None             # 每次调用前执行（测试里用来触发取消）

    def queue(self, kind, reply):
        self.queued.setdefault(kind, []).append(reply)

    @staticmethod
    def kind_of(system, user):
        if 'You audit a finished text package' in system:
            return 'review'
        if user.startswith('THEME / CARRIER'):
            return 'design'
        if 'failed automatic validation' in user and 'CURRENT BIBLE' in user:
            return 'design-repair'
        if 'These clips failed validation' in user:
            return 'rows-repair'
        if 'Write clips ' in user:
            return 'rows'
        raise AssertionError('未识别的调用：' + user[:80])

    def __call__(self, system, user, *, max_tokens, label):
        kind = self.kind_of(system, user)
        self.calls.append((kind, label))
        self.prompts.append((kind, system, user))
        if self.on_call:
            self.on_call(kind, label)
        pending = self.queued.get(kind)
        if pending:
            reply = pending.pop(0)
            if isinstance(reply, BaseException):
                raise reply
            return reply if isinstance(reply, str) else json.dumps(reply, ensure_ascii=False)
        if kind == 'design':
            return json.dumps(self.bible, ensure_ascii=False)
        if kind == 'rows':
            first, last = (int(n) for n in re.search(r'Write clips (\d+)-(\d+) of', user).groups())
            return json.dumps({'rows': [content_row(self.bible, i) for i in range(first, last + 1)]}, ensure_ascii=False)
        if kind == 'rows-repair':
            wanted = sorted({int(n) for n in re.findall(r'\[clip (\d+)\]', user)})
            return json.dumps({'rows': [content_row(self.bible, i) for i in wanted]}, ensure_ascii=False)
        if kind == 'review':
            return json.dumps(self.review_reply, ensure_ascii=False)
        raise AssertionError(kind)

    def count(self, kind):
        return sum(1 for k, _ in self.calls if k == kind)


def theme_from(bible, rows):
    """生成器产出的主题数据（圣经 + 合并了 outline 的行），供拼装测试直接使用。"""
    from beat_pack import schema
    merged = [schema.merge_outline(bible, [row], i)[0] for i, row in enumerate(rows, 1)]
    return schema.to_theme(copy.deepcopy(bible), merged)


def full_rows(bible):
    return [content_row(bible, i) for i in range(1, len(bible['outline']) + 1)]
