"""Claude 提示词包生成：按 video-beat-ladder 的方法，从一个创意主题生成整包 Omni 图片/视频提示词。

对外接口见 jobs.py（capabilities / start / cancel / resume / list_jobs / get_job / import_job / prompt_text），
拼装与校验见 compose.py / schema.py，模型流水线见 generator.py。
"""
from .jobs import (BeatPackError, cancel, capabilities, get_job, import_job, list_jobs, prompt_text,
                   resume, running_job_ids, set_concurrency, start)

__all__ = ['BeatPackError', 'cancel', 'capabilities', 'get_job', 'import_job', 'list_jobs', 'prompt_text',
           'resume', 'running_job_ids', 'set_concurrency', 'start']
