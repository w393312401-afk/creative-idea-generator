"""Google FX 服务管理中心的后端支撑：配置白名单与运行配置存储。

从 server.py 拆出来的原因：配置项从 6 个扩到二十来个之后，"哪些能热改、
哪些要重启、改了写哪个环境变量" 这套规则本身就有足够的体量和测试面，
塞在 HTTP 路由旁边会淹没在 4000 行里。
"""

import json
import os
import threading
from datetime import datetime, timezone

from integrations.google_fx.model_catalog import (
    GOOGLE_FX_IMAGE_MODELS,
    is_legacy_google_fx_image_model,
    normalize_google_fx_image_model,
)


# hot=True   → 写完环境变量当场生效（读取方每次调用现读）
# hot=False  → 需要重启服务才生效（读取方是 import 期求值的模块级常量）
#              前端会在这类字段上显示"需重启"，不要谎称热生效。
FX_CONFIG_SPEC = {
    # ── 连接与模型 ──────────────────────────────────────
    'adsPowerPort': {
        'type': 'integer', 'min': 1, 'max': 65535, 'default': 50325, 'hot': True,
        'group': '连接', 'label': 'AdsPower 本地 API 端口',
    },
    'adsPowerSilentMode': {
        'type': 'bool', 'default': True, 'hot': True, 'env': 'ADSPOWER_SILENT_MODE',
        'group': '连接', 'label': '后台静默运行（屏幕外运行，不弹窗抢焦点）',
    },
    'googleFxImageModel': {
        'type': 'enum', 'options': list(GOOGLE_FX_IMAGE_MODELS),
        'default': 'Nano Banana 2', 'hot': True, 'group': '模型', 'label': '图片模型',
    },
    'videoModel': {
        'type': 'enum',
        'options': ['Veo 3.1 - Lite', 'Veo 3.1 - Fast', 'Veo 3.1 - Quality', 'Omni Flash',
                    'Veo 3.1 - Lite [Lower Priority]'],
        'default': 'Veo 3.1 - Lite [Lower Priority]', 'hot': True,
        'group': '模型', 'label': '视频模型',
    },
    'videoDuration': {
        'type': 'enum', 'options': ['4', '6', '8', '10'], 'default': '10', 'hot': True,
        'group': '模型', 'label': 'Omni 视频时长（秒）',
    },
    'videoRefMode': {
        'type': 'enum', 'options': ['VIDEO_FRAMES', 'VIDEO_REFERENCES'],
        'default': 'VIDEO_FRAMES', 'hot': True,
        'group': '模型', 'label': '视频参考模式（帧 / 素材）',
    },

    # ── 号池与换号 ──────────────────────────────────────
    'googleFxIpRotateRequests': {
        'type': 'integer', 'min': 1, 'max': 100, 'default': 5, 'hot': True,
        'group': '号池', 'label': '换号节拍（每 N 个请求换一个号）',
    },
    'videoAccountPoolMinCredit': {
        'type': 'integer', 'min': 0, 'max': 100000, 'default': 15, 'hot': True,
        'group': '号池', 'label': '选号最低积分（低于此值自动禁用）',
    },
    'googleFxAccountStrategy': {
        'type': 'enum', 'options': ['credit_desc', 'expiration_asc', 'rotation'],
        'default': 'credit_desc', 'hot': True, 'group': '号池',
        'label': '选号调度策略（积分最多 / 重置日期最早 / 均衡轮替）',
    },
    'googleFxPriorityUserIds': {
        'type': 'account_list', 'default': [], 'hot': True, 'group': '号池',
        'label': '优先级浏览器实例（多选，留空=全池）',
    },
    # 'account' 类型的候选项来自号池（前端用 /api/account-pool 的结果渲染下拉），
    # 不写进 spec 的 options：号池是会变的运行时数据，固化进配置 schema 只会过期。
    'googleFxSequenceUserId': {
        'type': 'account', 'default': '', 'hot': True, 'group': '号池',
        'label': '序列生成默认浏览器环境（留空=按号池自动选）',
    },
    'googleFxSequenceUserLock': {
        'type': 'bool', 'default': False, 'hot': True, 'group': '号池',
        'label': '锁定默认环境：整条序列不按节拍换号',
    },

    # ── 超时与预算 ──────────────────────────────────────
    'googleFxMaxWaitSeconds': {
        'type': 'integer', 'min': 10, 'max': 3600, 'default': 120, 'hot': True,
        'env': 'GOOGLE_FX_MAX_WAIT_SECONDS', 'group': '超时',
        'label': '单张图等待上限（秒，视频按 ×5）',
    },
    'googleFxRequestBudgetSeconds': {
        'type': 'integer', 'min': 60, 'max': 86400, 'default': 3600, 'hot': True,
        'env': 'GOOGLE_FX_REQUEST_BUDGET_SECONDS', 'group': '超时',
        'label': '单请求总时间预算（秒）',
    },
    'googleFxManualWaitSeconds': {
        'type': 'integer', 'min': 60, 'max': 7200, 'default': 1200, 'hot': True,
        'env': 'GOOGLE_FX_MANUAL_WAIT_SECONDS', 'group': '超时',
        'label': '等待人工登录/验证码上限（秒）',
    },
    'googleFxRunLockWaitSeconds': {
        'type': 'integer', 'min': 60, 'max': 86400, 'default': 1800, 'hot': False,
        'env': 'GOOGLE_FX_RUN_LOCK_WAIT_SECONDS', 'group': '超时',
        'label': '库内运行锁等待上限（秒）',
    },

    # ── 提交节奏 ────────────────────────────────────────
    'googleFxPacingMinSeconds': {
        'type': 'integer', 'min': 0, 'max': 600, 'default': 15, 'hot': True,
        'env': 'GOOGLE_FX_PACING_MIN_SECONDS', 'group': '节奏',
        'label': '两次提交最小间隔（秒）',
    },
    'googleFxPacingMaxSeconds': {
        'type': 'integer', 'min': 0, 'max': 600, 'default': 25, 'hot': True,
        'env': 'GOOGLE_FX_PACING_MAX_SECONDS', 'group': '节奏',
        'label': '两次提交最大间隔（秒）',
    },

    # ── 去重与并发 ──────────────────────────────────────
    'googleFxDedupTtlSeconds': {
        'type': 'integer', 'min': 0, 'max': 86400, 'default': 600, 'hot': False,
        'env': 'GOOGLE_FX_DEDUP_TTL_SECONDS', 'group': '去重',
        'label': '重复请求缓存 TTL（秒，0=关闭复用）',
    },
    'googleFxVideoBatchForceSerial': {
        'type': 'bool', 'default': True, 'hot': False,
        'env': 'GOOGLE_FX_VIDEO_BATCH_FORCE_SERIAL', 'group': '去重',
        'label': '视频批量强制串行',
    },

    # ── 调试 ────────────────────────────────────────────
    'googleFxDryRun': {
        'type': 'bool', 'default': False, 'hot': True, 'env': 'FX_DRY_RUN',
        'group': '调试', 'label': 'Dry-run（走完定位与校验但不提交）',
    },
    'googleFxDebugCapture': {
        'type': 'bool', 'default': True, 'hot': True, 'env': 'GOOGLE_FX_DEBUG_CAPTURE',
        'group': '调试', 'label': '失败时自动保存现场（截图/DOM/选择器）',
    },
    'googleFxDebugMaxBuckets': {
        'type': 'integer', 'min': 1, 'max': 500, 'default': 40, 'hot': True,
        'env': 'GOOGLE_FX_DEBUG_MAX_BUCKETS', 'group': '调试',
        'label': '保留多少个任务的现场记录',
    },

    # ── 日志 ────────────────────────────────────────────
    'googleFxLogMaxBytes': {
        'type': 'integer', 'min': 1024 * 1024, 'max': 512 * 1024 * 1024,
        'default': 10 * 1024 * 1024, 'hot': False, 'env': 'ADSPWR_LOG_MAX_BYTES',
        'group': '日志', 'label': '日志单文件上限（字节）',
    },
    'googleFxLogBackupCount': {
        'type': 'integer', 'min': 0, 'max': 50, 'default': 5, 'hot': False,
        'env': 'ADSPWR_LOG_BACKUP_COUNT', 'group': '日志', 'label': '日志保留份数',
    },
}

# 这些键的环境变量落地由 server_common.apply_google_fx_runtime_overrides 处理
# （它在每次生成前都会重跑），此处只声明 env 映射给"直通型"配置用。
_DIRECT_ENV_KEYS = {key: spec['env'] for key, spec in FX_CONFIG_SPEC.items() if spec.get('env')}


def bool_to_env(value):
    return '1' if value else '0'


def apply_direct_env(config):
    """把带 env 映射的白名单字段写进 os.environ。

    读取方分两类：hot=True 的每次调用现读（写完即生效）；hot=False 的是 import
    期常量，写了也要等重启——前端据 spec 里的 hot 标记提示，不做虚假承诺。
    """
    for key, env_name in _DIRECT_ENV_KEYS.items():
        if key not in config:
            continue
        value = config[key]
        spec = FX_CONFIG_SPEC[key]
        if spec['type'] == 'bool':
            os.environ[env_name] = bool_to_env(value)
        else:
            os.environ[env_name] = str(value)


def validate_patch(patch):
    """校验并归一化一个配置补丁。返回清洗后的 dict。"""
    if not isinstance(patch, dict) or not patch:
        raise ValueError('patch 必须是非空对象')
    unknown = sorted(set(patch) - set(FX_CONFIG_SPEC))
    if unknown:
        raise ValueError(f'不允许修改字段: {", ".join(unknown)}')
    clean = {}
    for key, value in patch.items():
        spec = FX_CONFIG_SPEC[key]
        if key == 'googleFxImageModel' and is_legacy_google_fx_image_model(value):
            value = normalize_google_fx_image_model(value)
        if spec['type'] == 'integer':
            try:
                value = int(value)
            except (TypeError, ValueError):
                raise ValueError(f'{key} 必须是整数')
            if value < spec['min'] or value > spec['max']:
                raise ValueError(f'{key} 必须在 {spec["min"]}~{spec["max"]} 之间')
        elif spec['type'] == 'bool':
            if isinstance(value, str):
                value = value.strip().lower() in ('1', 'true', 'yes', 'on')
            value = bool(value)
        elif spec['type'] == 'account':
            # AdsPower user_id 是一串短标识；这里只做形态校验，不去号池里核对存在性——
            # 校验一次配置不该打 AdsPower 本地 API，而且账号可能是刚加进池子的。
            value = str(value or '').strip()
            if len(value) > 64:
                raise ValueError(f'{key} 不是合法的 AdsPower 环境编号')
        elif spec['type'] == 'account_list':
            if isinstance(value, str):
                items = [u.strip() for u in value.split(',') if u.strip()]
            elif isinstance(value, (list, tuple, set)):
                items = [str(u).strip() for u in value if str(u).strip()]
            else:
                items = []
            for item in items:
                if len(item) > 64:
                    raise ValueError(f'{key} 包含非法的 AdsPower 环境 ID: {item}')
            value = items
        elif value not in spec['options']:
            raise ValueError(f'{key} 不是允许的选项')
        clean[key] = value
    # 节奏区间必须是正向的，否则 random.uniform 会拿到反向区间
    low = clean.get('googleFxPacingMinSeconds')
    high = clean.get('googleFxPacingMaxSeconds')
    if low is not None and high is not None and low > high:
        raise ValueError('提交最小间隔不能大于最大间隔')
    # 「锁定默认环境」而没指定环境是个空承诺：保存后行为仍是自动选号，但界面上
    # 那个勾是打上的，用户会以为序列被钉住了。两个字段同时提交时直接拦下来。
    if (clean.get('googleFxSequenceUserLock')
            and 'googleFxSequenceUserId' in clean
            and not clean['googleFxSequenceUserId']):
        raise ValueError('勾选「锁定默认环境」前请先选择序列生成默认浏览器环境')
    return clean


class FxConfigStore:
    """FX 运行配置的读写存储。

    宿主注入协作对象，避免本模块反向依赖 server：
      config          —— 活的 SERVER_CONFIG dict（原地更新，其它模块持有同一引用）
      config_file     —— server_config.json 路径
      apply_overrides —— server_common.apply_google_fx_runtime_overrides
      audit           —— FX_CONTROL.audit
    """

    def __init__(self, config, config_file, versions_file=None, apply_overrides=None, audit=None):
        self._config = config
        self._config_file = str(config_file)
        self._apply_overrides = apply_overrides or (lambda _c: None)
        self._audit = audit or (lambda *a, **kw: None)
        self._lock = threading.Lock()

    # ── 读 ────────────────────────────────────────────

    def current(self):
        current = {key: self._config.get(key, spec['default'])
                   for key, spec in FX_CONFIG_SPEC.items()}
        current['googleFxImageModel'] = normalize_google_fx_image_model(
            current.get('googleFxImageModel'))
        return current

    def migrate_deprecated_values(self, actor='system'):
        raw = self._config.get('googleFxImageModel')
        if not is_legacy_google_fx_image_model(raw):
            return None
        return self.save(
            {'googleFxImageModel': normalize_google_fx_image_model(raw)},
            actor=actor, action='config.migrate',
            note=f'旧图片模型 {raw} 已迁移到当前模型目录',
        )

    def schema(self):
        """给前端渲染表单用：带 group/label/hot 标记，去掉内部 env 映射。"""
        return {key: {k: v for k, v in spec.items() if k != 'env'}
                for key, spec in FX_CONFIG_SPEC.items()}

    def versions(self, limit=30):
        return []

    def ensure_baseline(self, actor='system'):
        return None

    # ── 写 ────────────────────────────────────────────

    def save(self, patch, actor='local', action='config.update', note=''):
        clean = validate_patch(patch)
        with self._lock:
            before = {key: self._config.get(key, FX_CONFIG_SPEC[key]['default'])
                      for key in clean}
            if all(before[key] == clean[key] for key in clean):
                return {'config': self.current(), 'changed': {}, 'version': None, 'versions': []}
            updated = dict(self._config)
            updated.update(clean)
            tmp = self._config_file + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as handle:
                json.dump(updated, handle, ensure_ascii=False, indent=2)
            os.replace(tmp, self._config_file)
            self._config.clear()
            self._config.update(updated)
            self._apply_overrides(self._config)
            apply_direct_env(self.current())
        self._audit(action, details={'before': before, 'after': clean}, actor=actor)
        return {'config': self.current(), 'changed': clean, 'version': None, 'versions': []}

