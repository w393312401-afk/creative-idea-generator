"""质量门禁配置总表（server_common.GATE_SETTINGS）的通路与语义。

这个文件存在的理由是一个反复复发的 bug 类别：门禁项此前散在四处手工同步
（消费点的 config.get / effective_config 白名单 / js/state.js 前端默认值 /
server_config.example.json 注释），漏掉任何一处就是一次「配置了但从未生效」的
静默失效。qaGateLevel、imageEditTransport、skillProfile 各栽过一次；到
2026-08-10 为止 videoProcessVlmReview 与 strictGates 仍漏在托管模式白名单外
（strictGates 还被 SERVER_CONFIG 兜底掩盖着，看起来「能用」，实际那条 config
分支永远走不到）。

因此这里的核心用例是**遍历式**的：对 GATE_SETTINGS 里的每一项都断言
「请求 config 带的值能一路走到消费点」，而不是逐个手写。新增门禁项时
不必改这个文件，漏接白名单则立刻红。
"""
import os
import unittest
from contextlib import contextmanager
from unittest.mock import patch

import server_common
from server_common import (
    GATE_SETTINGS, effective_config, gate_setting, gate_settings_report,
    qa_gate_level, strict_gates_enabled,
)


@contextmanager
def _isolated(server_cfg=None, env=None):
    """隔离门禁取值的三个来源，否则结果会随开发机 server_config.json 漂移。"""
    clean_env = {k: v for k, v in os.environ.items()
                 if not k.startswith('SPARK_')}
    clean_env.update(env or {})
    with patch.dict(os.environ, clean_env, clear=True), \
         patch.dict(server_common.SERVER_CONFIG, server_cfg or {}, clear=True):
        yield


def _other_value(spec):
    """给一项门禁造一个「和默认值不同」的合法值，用于验证覆盖确实生效。"""
    if spec['type'] == 'bool':
        return not spec['default']
    if spec['type'] == 'enum':
        return next(v for v in spec['options'] if v != spec['default'])
    return spec['default'] + 1 if spec['default'] < spec.get('max', 3) else spec['default'] - 1


class TestRegistryShape(unittest.TestCase):
    """表本身的自洽性——写错一项 spec 会让派生出来的白名单/面板一起错。"""

    def test_keys_are_unique(self):
        keys = [item['key'] for item in GATE_SETTINGS]
        self.assertEqual(len(keys), len(set(keys)))

    def test_every_spec_is_well_formed(self):
        for spec in GATE_SETTINGS:
            with self.subTest(key=spec['key']):
                self.assertIn(spec['type'], ('bool', 'enum', 'int'))
                self.assertIn('default', spec)
                self.assertTrue(spec.get('label'))
                self.assertTrue(spec.get('hint'))
                self.assertIn(spec.get('section'), ('prompt', 'frame', 'video', 'env'))
                if spec['type'] == 'enum':
                    self.assertIn(spec['default'], spec['options'])
                    # 选项都要有中文文案，否则面板上会露出裸键名
                    for value in spec['options']:
                        self.assertIn(value, spec.get('option_labels', {}))
                if spec['type'] == 'int':
                    self.assertLessEqual(spec['min'], spec['default'])
                    self.assertLessEqual(spec['default'], spec['max'])

    def test_derived_allowlist_covers_every_gate_key(self):
        """白名单由表派生，不是手抄的——这条断言就是本文件的立身之本。"""
        for spec in GATE_SETTINGS:
            with self.subTest(key=spec['key']):
                self.assertIn(spec['key'], server_common._PASSTHROUGH_CLIENT_KEYS)

    def test_unknown_key_raises_instead_of_silently_defaulting(self):
        """拼错键名要当场炸，不能悄悄返回 None——静默失效正是这套配置的老毛病。"""
        with self.assertRaises(KeyError):
            gate_setting('videoProcessVlmReviewX')


class TestPrecedence(unittest.TestCase):
    """统一优先级：请求 config > server_config.json > 环境变量 > 表里的 default。"""

    def test_default_when_nothing_configured(self):
        with _isolated():
            for spec in GATE_SETTINGS:
                with self.subTest(key=spec['key']):
                    self.assertEqual(gate_setting(spec['key'], {}), spec['default'])
                    self.assertEqual(gate_setting(spec['key'], None), spec['default'])

    def test_request_config_beats_server_config(self):
        for spec in GATE_SETTINGS:
            other = _other_value(spec)
            with self.subTest(key=spec['key']), \
                 _isolated(server_cfg={spec['key']: spec['default']}):
                self.assertEqual(gate_setting(spec['key'], {spec['key']: other}), other)

    def test_server_config_used_when_request_lacks_key(self):
        for spec in GATE_SETTINGS:
            other = _other_value(spec)
            with self.subTest(key=spec['key']), _isolated(server_cfg={spec['key']: other}):
                self.assertEqual(gate_setting(spec['key'], {}), other)

    def test_env_var_is_last_resort(self):
        for spec in GATE_SETTINGS:
            env_key = spec.get('env')
            if not env_key:
                continue
            with self.subTest(key=spec['key']), \
                 _isolated(env={env_key: str(_other_value(spec)).lower()}):
                self.assertEqual(gate_setting(spec['key'], {}), _other_value(spec))

    def test_explicit_false_is_not_treated_as_absent(self):
        """布尔项显式关掉必须生效。用 truthy 判断（`config.get(k) or ...`）会让
        『本次任务关掉 VLM 复审』这种请求永远关不掉——旧代码的原型正是这个形状。"""
        with _isolated(server_cfg={'videoProcessVlmReview': True}):
            self.assertFalse(gate_setting('videoProcessVlmReview',
                                          {'videoProcessVlmReview': False}))

    def test_string_false_forms_are_honoured(self):
        """浏览器/CLI 传过来的可能是字符串。"""
        with _isolated():
            for raw in ('0', 'false', 'FALSE', 'no', 'off', ''):
                self.assertFalse(gate_setting('videoProcessVlmReview',
                                              {'videoProcessVlmReview': raw}), raw)
            for raw in ('1', 'true', 'yes', 'on'):
                self.assertTrue(gate_setting('videoProcessVlmReview',
                                             {'videoProcessVlmReview': raw}), raw)

    def test_invalid_values_fall_back_to_default(self):
        """门禁配置写错不该让整条生成链崩掉，但也不该按用户以为的那个值跑。"""
        with _isolated():
            self.assertEqual(gate_setting('qaGateLevel', {'qaGateLevel': 'yolo'}), 'standard')
            self.assertEqual(gate_setting('frameContinuityMode',
                                          {'frameContinuityMode': 123}), 'balanced')
            self.assertEqual(gate_setting('frameContinuityMaxRetries',
                                          {'frameContinuityMaxRetries': 'x'}), 1)

    def test_int_values_are_clamped(self):
        with _isolated():
            self.assertEqual(gate_setting('frameContinuityMaxRetries',
                                          {'frameContinuityMaxRetries': 99}), 3)
            self.assertEqual(gate_setting('frameContinuityMaxRetries',
                                          {'frameContinuityMaxRetries': -5}), 0)

    def test_enum_is_case_and_space_insensitive(self):
        with _isolated():
            self.assertEqual(gate_setting('qaGateLevel', {'qaGateLevel': ' LENIENT '}),
                             'lenient')


class TestEffectiveConfigPassthrough(unittest.TestCase):
    """托管模式（配了 apiKey 即是）的白名单是唯一的透传口。"""

    def test_every_gate_key_survives_managed_mode(self):
        for spec in GATE_SETTINGS:
            other = _other_value(spec)
            with self.subTest(key=spec['key']), _isolated(), \
                 patch.object(server_common, 'SERVER_MANAGED', True):
                merged = effective_config({spec['key']: other})
                self.assertEqual(merged.get(spec['key']), other)
                # 关键的一步：透传之后消费点读到的也必须是这个值
                self.assertEqual(gate_setting(spec['key'], merged), other)

    def test_every_gate_key_survives_nonmanaged_mode(self):
        for spec in GATE_SETTINGS:
            other = _other_value(spec)
            with self.subTest(key=spec['key']), _isolated(), \
                 patch.object(server_common, 'SERVER_MANAGED', False):
                merged = effective_config({spec['key']: other})
                self.assertEqual(gate_setting(spec['key'], merged), other)

    def test_video_process_vlm_review_regression(self):
        """2026-08-10 修复的具体缺口：这一项此前不在白名单里，托管模式下
        请求带的值会被整个丢掉，只有 server_config.json 生效。"""
        with _isolated(), patch.object(server_common, 'SERVER_MANAGED', True):
            merged = effective_config({'videoProcessVlmReview': False})
            self.assertIn('videoProcessVlmReview', merged)
            self.assertFalse(gate_setting('videoProcessVlmReview', merged))

    def test_strict_gates_regression(self):
        """同一个缺口的另一半：strictGates 被 SERVER_CONFIG 兜底掩盖着，
        看起来能用，实际请求 config 那条分支永远走不到。"""
        with _isolated(), patch.object(server_common, 'SERVER_MANAGED', True):
            merged = effective_config({'strictGates': True})
            self.assertIn('strictGates', merged)
            self.assertTrue(strict_gates_enabled(merged))

    def test_client_explicit_value_wins_over_server_in_nonmanaged_mode(self):
        """非托管分支是「客户端没带才从服务端补」，别把客户端显式写的 false 顶掉。"""
        with _isolated(server_cfg={'videoProcessVlmReview': True}), \
             patch.object(server_common, 'SERVER_MANAGED', False):
            merged = effective_config({'videoProcessVlmReview': False})
            self.assertFalse(gate_setting('videoProcessVlmReview', merged))


class TestLegacyReadersAgree(unittest.TestCase):
    """两个历史入口改成薄封装后，语义必须与直接查表一致。"""

    def test_qa_gate_level_matches_registry(self):
        with _isolated(server_cfg={'qaGateLevel': 'lenient'}):
            self.assertEqual(qa_gate_level({}), gate_setting('qaGateLevel', {}))
            self.assertEqual(qa_gate_level({}), 'lenient')

    def test_strict_gates_matches_registry(self):
        with _isolated(server_cfg={'strictGates': True}):
            self.assertTrue(strict_gates_enabled({}))
            self.assertIs(strict_gates_enabled({}), bool(gate_setting('strictGates', {})))

    def test_qa_gate_levels_constant_still_exported(self):
        """外部有 `from server_common import QA_GATE_LEVELS` 的引用。"""
        self.assertEqual(tuple(server_common.QA_GATE_LEVELS),
                         ('standard', 'lenient', 'off'))


class TestGateSettingsReport(unittest.TestCase):
    """/api/mode 下发的形状：前端照它渲染开关面板，不再抄一份默认值。"""

    def test_report_covers_every_key_and_is_json_safe(self):
        import json
        with _isolated():
            report = gate_settings_report()
        self.assertEqual([r['key'] for r in report],
                         [s['key'] for s in GATE_SETTINGS])
        json.dumps(report)  # 不可 JSON 序列化就下发不出去（options 曾是 tuple）

    def test_report_carries_server_value_and_pin_flag(self):
        with _isolated(server_cfg={'qaGateLevel': 'off'}):
            report = {r['key']: r for r in gate_settings_report()}
        self.assertEqual(report['qaGateLevel']['server_value'], 'off')
        self.assertTrue(report['qaGateLevel']['server_pinned'])
        # 服务端没写的项：生效值是表里的 default，且不标「已钉死」
        self.assertFalse(report['videoProcessVlmReview']['server_pinned'])
        self.assertTrue(report['videoProcessVlmReview']['server_value'])

    def test_report_does_not_leak_env_var_names(self):
        """env 是服务端内部口径，下发给浏览器没有意义还徒增迷惑。"""
        with _isolated():
            for item in gate_settings_report():
                self.assertNotIn('env', item)


class TestFrontendHasNoSecondSourceOfTruth(unittest.TestCase):
    """前端不得再抄一份门禁默认值——那正是白名单漂移的同构问题。"""

    def test_state_js_declares_no_gate_defaults(self):
        import re
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, 'js', 'state.js'), encoding='utf-8') as fh:
            source = fh.read()
        # 注释里可以提到键名（解释为什么不在这里定义），但不能有 `key:` 赋值
        body = re.sub(r'//[^\n]*|/\*.*?\*/', '', source, flags=re.S)
        for spec in GATE_SETTINGS:
            with self.subTest(key=spec['key']):
                self.assertNotRegex(body, rf'\b{spec["key"]}\s*:')


if __name__ == '__main__':
    unittest.main()
