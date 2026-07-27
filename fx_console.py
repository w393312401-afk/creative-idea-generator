"""Google FX 服务管理中心的后端支撑：配置白名单 + 版本栈。

从 server.py 拆出来的原因：配置项从 6 个扩到二十来个之后，"哪些能热改、
哪些要重启、改了写哪个环境变量、怎么回滚" 这套规则本身就有足够的体量和测试面，
塞在 HTTP 路由旁边会淹没在 4000 行里。

版本栈（替代原来的单步回滚）：每次成功保存都往 runtime/fx_config_versions.jsonl
追加一条完整快照，于是可以列版本、看 diff、回滚到任意版本、以及回滚之后重做。
原实现是"在审计里找最近一条 config.update 然后套用它的 before"——只能退一步，
第二次点击会重复套用同一份 before（看起来像又退了一步，实际是空操作），
而且没有任何前进的路。
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
        'type': 'enum', 'options': ['', '4', '6', '8', '10'], 'default': '', 'hot': True,
        'group': '模型', 'label': 'Omni 视频时长（秒）',
    },

    # ── 号池与换号 ──────────────────────────────────────
    'googleFxIpRotateRequests': {
        'type': 'integer', 'min': 1, 'max': 100, 'default': 5, 'hot': True,
        'group': '号池', 'label': '换号节拍（每 N 个请求换一个号）',
    },
    'videoAccountPoolMinCredit': {
        'type': 'integer', 'min': 0, 'max': 100000, 'default': 1, 'hot': True,
        'group': '号池', 'label': '选号最低积分',
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
        elif value not in spec['options']:
            raise ValueError(f'{key} 不是允许的选项')
        clean[key] = value
    # 节奏区间必须是正向的，否则 random.uniform 会拿到反向区间
    low = clean.get('googleFxPacingMinSeconds')
    high = clean.get('googleFxPacingMaxSeconds')
    if low is not None and high is not None and low > high:
        raise ValueError('提交最小间隔不能大于最大间隔')
    return clean


class FxConfigStore:
    """FX 运行配置的读写与版本栈。

    宿主注入四个协作对象，避免本模块反向依赖 server：
      config          —— 活的 SERVER_CONFIG dict（原地更新，其它模块持有同一引用）
      config_file     —— server_config.json 路径
      apply_overrides —— server_common.apply_google_fx_runtime_overrides
      audit           —— FX_CONTROL.audit
    """

    def __init__(self, config, config_file, versions_file, apply_overrides, audit):
        self._config = config
        self._config_file = str(config_file)
        self._versions_file = str(versions_file)
        self._cursor_file = str(versions_file) + '.cursor'
        self._apply_overrides = apply_overrides
        self._audit = audit
        self._lock = threading.Lock()

    # ── 游标 ──────────────────────────────────────────
    # 回滚/重做需要知道"当前停在版本栈的哪一格"。光比对"配置内容和当前不同的最近
    # 一条"是不够的：回滚本身也会追加一条版本记录，于是第二次回滚会把刚被退掉的
    # 那一版当成"最近的不同版本"又装回去（来回跳，永远退不到第三格）。

    def _read_cursor(self):
        try:
            with open(self._cursor_file, 'r', encoding='utf-8') as handle:
                return json.load(handle).get('version_id')
        except Exception:
            return None

    def _write_cursor(self, version_id):
        try:
            os.makedirs(os.path.dirname(self._cursor_file), exist_ok=True)
            tmp = self._cursor_file + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as handle:
                json.dump({'version_id': version_id}, handle)
            os.replace(tmp, self._cursor_file)
        except Exception:
            pass

    def _chronological(self):
        rows = self.versions(500)
        rows.reverse()
        return rows

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
        rows = []
        try:
            with open(self._versions_file, 'r', encoding='utf-8') as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        continue
        except FileNotFoundError:
            return []
        except Exception:
            return []
        rows.reverse()
        return rows[:max(1, min(int(limit), 200))]

    def diff_against_current(self, version_id):
        target = next((row for row in self.versions(200) if row.get('id') == version_id), None)
        if target is None:
            raise KeyError('找不到这个配置版本')
        current = self.current()
        changes = {}
        for key, value in (target.get('config') or {}).items():
            if key in FX_CONFIG_SPEC and current.get(key) != value:
                changes[key] = {'current': current.get(key), 'target': value}
        return {'version': target, 'changes': changes}

    # ── 写 ────────────────────────────────────────────

    def _append_version(self, config, action, actor, note=''):
        row = {
            'id': datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'),
            'at': datetime.now(timezone.utc).astimezone().isoformat(),
            'action': action,
            'actor': actor,
            'note': note,
            'config': dict(config),
        }
        try:
            os.makedirs(os.path.dirname(self._versions_file), exist_ok=True)
            with open(self._versions_file, 'a', encoding='utf-8') as handle:
                handle.write(json.dumps(row, ensure_ascii=False, default=str) + '\n')
        except Exception:
            pass
        return row

    def ensure_baseline(self, actor='system'):
        """版本文件为空时先记一条基线，否则第一次"回滚"没有落脚点。"""
        if self.versions(1):
            return None
        return self._append_version(self.current(), 'baseline', actor,
                                    note='服务启动时记录的基线配置')

    def save(self, patch, actor='local', action='config.update', note='', cursor_id=None):
        clean = validate_patch(patch)
        with self._lock:
            before = {key: self._config.get(key, FX_CONFIG_SPEC[key]['default'])
                      for key in clean}
            if all(before[key] == clean[key] for key in clean):
                # 没有实际变化就不要污染版本栈——否则"回滚一步"会退到一个同样的版本。
                if cursor_id:
                    self._write_cursor(cursor_id)
                return {'config': self.current(), 'changed': {}, 'version': None,
                        'cursor': cursor_id or self._read_cursor()}
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
            version = self._append_version(self.current(), action, actor, note)
            # 普通保存把游标推到新版本；回滚/重做由调用方指定游标落在目标版本上，
            # 这样连续回滚才能一格一格往前走。
            self._write_cursor(cursor_id or version['id'])
        self._audit(action, details={'before': before, 'after': clean,
                                    'version_id': version['id']}, actor=actor)
        return {'config': self.current(), 'changed': clean, 'version': version,
                'cursor': cursor_id or version['id']}

    def restore(self, version_id=None, actor='local', direction='back'):
        """回滚/重做到某个版本。

        version_id 给定时直接跳到那一版。否则按 direction 沿版本栈移动一格：
          back    —— 游标往前（更早）找最近一个配置内容不同的版本
          forward —— 游标往后（更晚）找最近一个配置内容不同的版本
        """
        history = self._chronological()
        if not history:
            raise ValueError('还没有任何配置版本记录')
        current = self.current()

        if version_id:
            target = next((row for row in history if row.get('id') == version_id), None)
            if target is None:
                raise KeyError('找不到这个配置版本')
        else:
            cursor = self._read_cursor()
            index = next((i for i, row in enumerate(history) if row.get('id') == cursor), None)
            if index is None:
                # 没有游标（老数据/刚升级）：从最后一条与当前等价的版本起步
                index = next((i for i in range(len(history) - 1, -1, -1)
                              if (history[i].get('config') or {}) == current), len(history) - 1)
            step = 1 if direction == 'forward' else -1
            target = None
            probe = index + step
            while 0 <= probe < len(history):
                if (history[probe].get('config') or {}) != current:
                    target = history[probe]
                    break
                probe += step
            if target is None:
                raise ValueError(
                    '没有可回滚的更早版本' if direction != 'forward'
                    else '没有可重做的更新版本')

        patch = {key: value for key, value in (target.get('config') or {}).items()
                 if key in FX_CONFIG_SPEC}
        if not patch:
            raise ValueError('目标版本没有可应用的字段')
        return self.save(patch, actor=actor, action='config.restore',
                         note=f'恢复到版本 {target["id"]}（{target.get("action")}）',
                         cursor_id=target['id'])
