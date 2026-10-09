"""把一份「完整提示词.txt」作为新项目写进创意库（= 项目工作台上多出一行）。

与前端「📥 上传提示词集」、tools/import_prompt_set.py 同一条路径的进程内版本：先过服务端槽位契约
（图片从 1 连续到 N、视频落在 1..N），再用 write_library_item 新建一条创意。从不改写已有项目，
同名标题直接拒绝——重名由调用方换标题重试，而不是静默覆盖别人的项目。
"""
import re
import time
from datetime import datetime, timezone

CREATIVITY_LABEL = 'Claude 生成'


class ImportRefused(ValueError):
    """导入被拒绝。code 供界面区分：duplicate_title / bad_slots / empty_title。"""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def check_slots(text):
    """槽位契约校验，返回 (图片数, 视频数, prompt_slots)。不通过抛 ImportRefused。"""
    from prompt_pipeline import _parse_prompt_slots, prompt_slots_list
    images, videos = _parse_prompt_slots(text or '')
    if not images:
        raise ImportRefused('bad_slots', '提示词里解析不到任何「图片 N:」槽位')
    count = max(images)
    missing = [n for n in range(1, count + 1) if n not in images]
    if missing:
        raise ImportRefused('bad_slots', '图片槽位号必须从 1 连续编到 N，缺少：' + '、'.join(f'图片 {n}' for n in missing))
    stray = sorted(n for n in videos if n < 1 or n > count)
    if stray:
        raise ImportRefused('bad_slots', '视频槽位号必须落在 1–%d 内，越界的有：%s' % (count, '、'.join(f'视频 {n}' for n in stray)))
    return len(images), len(videos), prompt_slots_list(text)


def title_exists(title, library_dir=None):
    from server_common import read_library_index
    index = read_library_index(library_dir) or []
    return any(isinstance(row, dict) and row.get('title') == title for row in index)


def import_prompt_set(text, title, *, source_label, audit_md='', library_dir=None):
    """写入创意库。返回 {'status': 'imported', 'id', 'title', 'project_key', 'images', 'videos'}。"""
    from server_common import write_library_item
    title = (title or '').strip()
    if not title:
        raise ImportRefused('empty_title', '项目标题为空')
    images, videos, slots = check_slots(text)
    if title_exists(title, library_dir):
        raise ImportRefused('duplicate_title', f'点子库里已有同名创意「{title}」，请换一个标题')
    stamp = int(time.time() * 1000)
    key = f'run_import_{stamp}__{title}'
    idea = {
        'id': f'import_{stamp}', 'title': title, 'project_key': key, 'theme': title,
        'creativity': CREATIVITY_LABEL, 'prompt_block': text,
        'timestamp': datetime.now().strftime('%Y/%m/%d %H:%M:%S'), 'timings': {},
        'image_count': images, 'video_count': videos, 'collage_url': '', 'covers': [], 'frameRun': None,
        'english_title': '', 'social_title_en': '', 'social_title_cn': '',
        'audit_md': audit_md or '由 Claude 按 video-beat-ladder 生成并通过结构校验；未经本机质量门与工序一致性二次校验，也未渲染。',
        'repair_md': '', 'imported_at': datetime.now(timezone.utc).isoformat(),
        'imported_source_text': text, 'imported_source_label': source_label,
        'prompt_slots': slots,
    }
    write_library_item(idea, library_dir)
    return {'status': 'imported', 'id': idea['id'], 'title': title, 'project_key': key, 'images': images, 'videos': videos}


def default_title(bible):
    """项目标题：“榴莲屋_雨林住所”，与手工导入的命名一致；清掉文件名/标题里不宜出现的字符。"""
    raw = f"{bible.get('name', '').strip()}_{bible.get('subtitle', '').strip()}"
    return re.sub(r'[\\/:*?"<>|\s]+', '', raw).strip('_')
