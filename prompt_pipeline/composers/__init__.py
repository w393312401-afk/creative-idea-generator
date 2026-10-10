"""Phase 2（逐拍 VIDEO/IMAGE 撰写与装配）的 composer 策略层。

合成是两段式：compose_anchor_and_packet（Phase 1：brief/工序梯/Drift Lock 包/IMAGE 1）
+ compose_remaining_beats（Phase 2：第 2..N+1 拍的文本生成与最终装配）。Phase 1 与
IMAGE 段对所有技能包是同一套契约（帧渲染、创意库、断点续传都吃这一套），真正随
「做哪个模型的提示词」而变的只有 VIDEO 的镜头语法——所以按 profile 分派的只有 Phase 2，
且子类只覆写 VIDEO 相关的几个钩子，IMAGE/Phase 1 一律走 base 的实现，不复制第二份。

profile 的判定不在这一层：视频模型名 → profile 的映射唯一地长在
server_common.SKILL_PROFILE_VIDEO_MODEL_RULES / active_skill_profile()，这里只接收
结果。合成层再造一份判断，就等于给同一件事留了两个会各自漂移的真相源。
"""

from .base import BaseComposer
from .miniature import MiniatureComposer
from .omni import OmniComposer

# profile 名 → composer 类。键必须与 server_common.SKILL_PROFILES 的键一致。
COMPOSERS = {
    'base': BaseComposer,
    'omni': OmniComposer,
    'miniature': MiniatureComposer,
}

DEFAULT_COMPOSER = BaseComposer


def get_composer(profile=None):
    """按 profile 取 composer 实例；未知/缺省的 profile 一律回落到 base。

    回落而不是抛错：profile 有一部分来自前端配置（skillProfile、videoModel），
    与 server_common._normalize_skill_profile 同一个理由——拼错一个字母不该让整条
    合成链路 500。composer 是无状态的，每次新建一个即可。"""
    name = str(profile or '').strip().lower()
    return COMPOSERS.get(name, DEFAULT_COMPOSER)()


__all__ = ['BaseComposer', 'MiniatureComposer', 'OmniComposer', 'COMPOSERS', 'get_composer']
