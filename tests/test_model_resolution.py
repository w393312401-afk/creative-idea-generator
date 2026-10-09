import unittest
import json
from pathlib import Path
from unittest.mock import patch
import server_common
from server_common import resolve_chat_model, resolve_gateway, effective_config
from prompt_pipeline import _aux_model

class TestModelResolution(unittest.TestCase):
    def test_resolve_chat_model_preserves_gemini_3_8(self):
        self.assertEqual(resolve_chat_model('gemini-3.8-flash-high'), 'gemini-3.8-flash-high')
        self.assertEqual(resolve_chat_model('  gemini-3.8-flash-high  '), 'gemini-3.8-flash-high')

    def test_resolve_chat_model_redirects_obsolete_models_to_gemini_3_8(self):
        for model in ('gemini-3-flash', 'gemini-3-flash-agent',
                      'gemini-3.5-flash', 'gemini-3.5-flash-low',
                      'gemini-3.5-flash-extra-low', 'gemini-3.6-flash-high',
                      'gemini-3.6-flash-low', 'gemini-3.7-flash-high',
                      'gemini-3.7-flash-low', 'gemini-3.1-pro-high',
                      'gemini-3.1-pro-low'):
            with self.subTest(model=model):
                self.assertEqual(resolve_chat_model(model), 'gemini-3.8-flash-high')

    def test_resolve_chat_model_redirects_old_gpt(self):
        for model in ('gpt-3.5-turbo', 'gpt-4', 'gpt-4-turbo', 'gpt-4o',
                      'gpt-4o-mini', 'gpt-4.1', 'gpt-4.5-preview', 'gpt-5',
                      'gpt-5.4', 'gpt-5.5', 'gpt-5.5-codex'):
            with self.subTest(model=model):
                self.assertEqual(resolve_chat_model(model), 'gpt-6.1-sol')

    def test_resolve_chat_model_preserves_claude_models(self):
        # Claude 已重新接入：用户选了什么就发什么，由 claudeBaseUrl 网关决定是否可用。
        for model in ('claude-opus-5-5', 'claude-sonnet-5-5', 'claude-fable-5-1',
                      'claude-haiku-5-5', 'claude-sonnet-4-6', 'claude-opus-4-6-thinking'):
            with self.subTest(model=model):
                self.assertEqual(resolve_chat_model(model), model)
        self.assertEqual(resolve_chat_model('  Claude-Sonnet-5-5  '), 'Claude-Sonnet-5-5')

    def test_resolve_chat_model_preserves_other_models(self):
        for model in ('custom-model', 'my-gpt-5-model', 'gpt-50-custom',
                      'gemini-3.8-flash-high', 'gemini-3.1-flash-image',
                      'gemini-3-flash-image', 'gpt-image-2.5'):
            with self.subTest(model=model):
                self.assertEqual(resolve_chat_model(model), model)

    def test_new_models_use_codex_gateway(self):
        config = {'baseUrl': 'http://gemini.test/v1', 'apiKey': 'gemini-key',
                  'codexBaseUrl': 'http://codex.test/v1', 'codexApiKey': 'codex-key'}
        for model in ('gpt-6.1-sol', 'gpt-6-astra', 'gpt-6-sol', 'gpt-6-luna',
                      'gpt-image-2.5'):
            with self.subTest(model=model):
                self.assertEqual(resolve_chat_model(model), model)
                self.assertEqual(resolve_gateway(model, config),
                                 ('http://codex.test/v1', 'codex-key'))

    def test_legacy_gpt_routes_to_gpt_gateway_after_migration(self):
        config = {'baseUrl': 'http://gemini.test/v1', 'apiKey': 'gemini-key',
                  'codexBaseUrl': 'http://codex.test/v1/', 'codexApiKey': 'codex-key'}
        for model in ('gpt-4o', 'gpt-5.5-codex'):
            with self.subTest(model=model):
                self.assertEqual(resolve_gateway(model, config),
                                 ('http://codex.test/v1', 'codex-key'))

    def test_migrated_gpt_gateway_uses_server_codex_credentials(self):
        with patch.object(server_common, 'SERVER_CONFIG', {
                'codexBaseUrl': 'http://server-codex.test/v1',
                'codexApiKey': 'server-codex-key'}):
            self.assertEqual(resolve_gateway('gpt-5.5-codex', {
                'baseUrl': 'http://gemini.test/v1', 'apiKey': 'gemini-key'}),
                ('http://server-codex.test/v1', 'server-codex-key'))

    def test_claude_models_use_dedicated_claude_gateway(self):
        config = {'baseUrl': 'http://gemini.test/v1', 'apiKey': 'gemini-key',
                  'codexBaseUrl': 'http://codex.test/v1', 'codexApiKey': 'codex-key',
                  'claudeBaseUrl': 'http://claude.test/v1/', 'claudeApiKey': 'claude-key'}
        for model in ('claude-opus-5-5', 'claude-sonnet-5-5', 'claude-fable-5-1',
                      'claude-haiku-5-5', 'Claude-Opus-5-5'):
            with self.subTest(model=model):
                self.assertEqual(resolve_gateway(model, config),
                                 ('http://claude.test/v1', 'claude-key'))

    def test_dedicated_claude_gateway_never_receives_the_main_api_key(self):
        config = {'baseUrl': 'http://gemini.test/v1', 'apiKey': 'gemini-key',
                  'claudeBaseUrl': 'http://claude.test/v1'}
        with patch.object(server_common, 'SERVER_CONFIG', {}):
            self.assertEqual(resolve_gateway('claude-opus-5-5', config),
                             ('http://claude.test/v1', ''))

    def test_claude_without_dedicated_gateway_uses_the_main_gateway(self):
        config = {'baseUrl': 'http://main.test/v1/', 'apiKey': 'main-key'}
        with patch.object(server_common, 'SERVER_CONFIG', {}):
            self.assertEqual(resolve_gateway('claude-sonnet-5-5', config),
                             ('http://main.test/v1', 'main-key'))
            self.assertEqual(resolve_gateway('claude-sonnet-5-5', {**config, 'claudeApiKey': 'claude-only-key'}),
                             ('http://main.test/v1', 'claude-only-key'))

    def test_claude_gateway_falls_back_to_server_config(self):
        with patch.object(server_common, 'SERVER_CONFIG', {
                'claudeBaseUrl': 'http://server-claude.test/v1',
                'claudeApiKey': 'server-claude-key'}):
            self.assertEqual(resolve_gateway('claude-opus-5-5', {
                'baseUrl': 'http://gemini.test/v1', 'apiKey': 'gemini-key'}),
                ('http://server-claude.test/v1', 'server-claude-key'))

    def test_other_models_ignore_claude_gateway_settings(self):
        config = {'baseUrl': 'http://gemini.test/v1', 'apiKey': 'gemini-key',
                  'codexBaseUrl': 'http://codex.test/v1', 'codexApiKey': 'codex-key',
                  'claudeBaseUrl': 'http://claude.test/v1', 'claudeApiKey': 'claude-key'}
        self.assertEqual(resolve_gateway('gemini-3.8-flash-high', config),
                         ('http://gemini.test/v1', 'gemini-key'))
        self.assertEqual(resolve_gateway('gpt-6.1-sol', config),
                         ('http://codex.test/v1', 'codex-key'))

    def test_shape_chat_payload_strips_sampling_and_caps_tokens_for_claude(self):
        from server_common import shape_chat_payload, claude_max_output_tokens
        payload = shape_chat_payload('claude-opus-5-5', {
            'model': 'claude-opus-5-5', 'temperature': 0.85, 'top_p': 0.9, 'top_k': 40,
            'max_tokens': 200000, 'messages': []})
        self.assertNotIn('temperature', payload)
        self.assertNotIn('top_p', payload)
        self.assertNotIn('top_k', payload)
        self.assertEqual(payload['max_tokens'], 128000)
        self.assertEqual(claude_max_output_tokens('claude-haiku-4-5'), 64000)
        self.assertEqual(claude_max_output_tokens('claude-sonnet-4-6'), 128000)
        small = shape_chat_payload('claude-sonnet-5-5', {'max_tokens': 5, 'temperature': 0.1})
        self.assertEqual(small, {'max_tokens': 5})

    def test_shape_chat_payload_leaves_other_models_untouched(self):
        from server_common import shape_chat_payload
        original = {'model': 'gpt-6.1-sol', 'temperature': 0.85, 'max_tokens': 65536}
        self.assertEqual(shape_chat_payload('gpt-6.1-sol', dict(original)), original)
        self.assertEqual(shape_chat_payload('gemini-3.8-flash-high', dict(original)), original)

    def test_new_image_model_request_parameters(self):
        from frame_generator import (_image_generation_model, _image_edit_model,
                                     _image_generation_model_for_request, _image_size_to_api_size)
        config = {'imageModel': 'gpt-image-2.5', 'imageAspectRatio': '9:16', 'imageQuality': '4K'}
        self.assertEqual(_image_generation_model(config), 'gpt-image-2.5')
        self.assertEqual(_image_edit_model(config), 'gpt-image-2.5')
        self.assertEqual(_image_generation_model_for_request('gpt-image-2.5', '9:16', '4K'), 'gpt-image-2.5')
        self.assertEqual(_image_size_to_api_size('9:16', 'gpt-image-2.5'), '1024x1536')

    def test_default_model_in_effective_config(self):
        # Default injection belongs to managed mode; do not depend on a real key.
        with patch.object(server_common, 'SERVER_MANAGED', True), \
             patch.object(server_common, 'SERVER_CONFIG', {}):
            cfg = effective_config({})
        self.assertEqual(cfg.get('model'), 'gemini-3.8-flash-high')

    def test_effective_config_upgrades_obsolete_models(self):
        with patch.object(server_common, 'SERVER_MANAGED', True), \
             patch.object(server_common, 'ALLOW_CLIENT_MODEL', True), \
             patch.object(server_common, 'SERVER_CONFIG', {}):
            self._assert_managed_model_upgrades()

    def _assert_managed_model_upgrades(self):
        self.assertEqual(effective_config({'model': 'gemini-3-flash'}).get('model'), 'gemini-3.8-flash-high')
        self.assertEqual(effective_config({'model': 'gemini-3.5-flash'}).get('model'), 'gemini-3.8-flash-high')
        self.assertEqual(effective_config({'model': 'gemini-3.6-flash-high'}).get('model'), 'gemini-3.8-flash-high')
        self.assertEqual(effective_config({'model': 'gemini-3.1-pro-high'}).get('model'), 'gemini-3.8-flash-high')
        self.assertEqual(effective_config({'cheapModel': 'gemini-3.5-flash-low'}).get('cheapModel'), 'gemini-3.8-flash-high')

    def test_managed_server_models_are_normalized_after_merge(self):
        with patch.object(server_common, 'SERVER_MANAGED', True), \
             patch.object(server_common, 'ALLOW_CLIENT_MODEL', False), \
             patch.object(server_common, 'SERVER_CONFIG', {
                 'model': 'gpt-4o', 'cheapModel': 'gpt-5.5',
                 'auxModel': 'gemini-3.7-flash-high',
                 'reviewModel': 'gpt-4.1',
                 'imageModel': 'gpt-image-2.5'}):
            cfg = effective_config({})
        self.assertEqual(cfg['model'], 'gpt-6.1-sol')
        self.assertEqual(cfg['cheapModel'], 'gpt-6.1-sol')
        self.assertEqual(cfg['auxModel'], 'gemini-3.8-flash-high')
        self.assertEqual(cfg['reviewModel'], 'gpt-6.1-sol')
        self.assertEqual(cfg['imageModel'], 'gpt-image-2.5')

    def test_managed_client_models_are_normalized_after_merge(self):
        client_config = {'model': 'gpt-4o', 'cheapModel': 'gpt-5.5',
                         'auxModel': 'gemini-3.7-flash-high',
                         'reviewModel': 'gpt-4.1',
                         'imageModel': 'gemini-3.1-flash-image'}
        with patch.object(server_common, 'SERVER_MANAGED', True), \
             patch.object(server_common, 'ALLOW_CLIENT_MODEL', True), \
             patch.object(server_common, 'SERVER_CONFIG', {'model': 'gemini-3.8-flash-high'}):
            cfg = effective_config(client_config)
        self.assertEqual(cfg['model'], 'gpt-6.1-sol')
        self.assertEqual(cfg['cheapModel'], 'gpt-6.1-sol')
        self.assertEqual(cfg['auxModel'], 'gemini-3.8-flash-high')
        self.assertEqual(cfg['reviewModel'], 'gpt-6.1-sol')
        self.assertEqual(cfg['imageModel'], 'gemini-3.1-flash-image')
        self.assertEqual(client_config['model'], 'gpt-4o')

    def test_managed_config_rejects_disabled_client_model_overrides(self):
        with patch.object(server_common, 'SERVER_MANAGED', True), \
             patch.object(server_common, 'ALLOW_CLIENT_MODEL', False), \
             patch.object(server_common, 'SERVER_CONFIG', {
                 'model': 'gpt-5.5', 'cheapModel': 'custom-cheap-model',
                 'auxModel': 'gpt-6-luna', 'imageModel': 'gpt-image-2.5'}):
            cfg = effective_config({'model': 'gemini-3.7-flash-high',
                                    'cheapModel': 'gpt-6-astra',
                                    'auxModel': 'gpt-4.1',
                                    'imageModel': 'gemini-3.1-flash-image'})
        self.assertEqual(cfg['model'], 'gpt-6.1-sol')
        self.assertEqual(cfg['cheapModel'], 'custom-cheap-model')
        self.assertEqual(cfg['auxModel'], 'gpt-6-luna')
        self.assertEqual(cfg['imageModel'], 'gpt-image-2.5')

    def test_unmanaged_config_preserves_client_model_contract(self):
        with patch.object(server_common, 'SERVER_MANAGED', False), \
             patch.object(server_common, 'SERVER_CONFIG', {}):
            self.assertNotIn('model', effective_config({}))
            self.assertEqual(effective_config({'model': 'custom-model'})['model'], 'custom-model')
            self.assertEqual(effective_config({'model': 'gemini-3-flash'})['model'], 'gemini-3.8-flash-high')

    def test_unmanaged_config_migrates_auxiliary_models_and_preserves_images(self):
        with patch.object(server_common, 'SERVER_MANAGED', False), \
             patch.object(server_common, 'SERVER_CONFIG', {}):
            cfg = effective_config({'model': 'gpt-4o',
                                    'cheapModel': 'gpt-5.5',
                                    'auxModel': 'gemini-3.7-flash-high',
                                    'reviewModel': 'gpt-4.1',
                                    'imageModel': 'gemini-3.1-flash-image'})
        self.assertEqual(cfg['model'], 'gpt-6.1-sol')
        self.assertEqual(cfg['cheapModel'], 'gpt-6.1-sol')
        self.assertEqual(cfg['auxModel'], 'gemini-3.8-flash-high')
        self.assertEqual(cfg['reviewModel'], 'gpt-6.1-sol')
        self.assertEqual(cfg['imageModel'], 'gemini-3.1-flash-image')

    def test_explicit_aux_model_uses_migrated_effective_config(self):
        with patch.object(server_common, 'SERVER_MANAGED', False), \
             patch.object(server_common, 'SERVER_CONFIG', {}):
            cfg = effective_config({'auxModel': 'gpt-4o'})
        self.assertEqual(_aux_model(cfg), 'gpt-6.1-sol')

    def test_claude_models_survive_effective_config(self):
        client_config = {'model': 'claude-opus-5-5', 'cheapModel': 'claude-haiku-5-5',
                         'auxModel': 'claude-sonnet-5-5', 'reviewModel': 'claude-fable-5-1'}
        for managed in (False, True):
            with self.subTest(managed=managed), \
                 patch.object(server_common, 'SERVER_MANAGED', managed), \
                 patch.object(server_common, 'ALLOW_CLIENT_MODEL', True), \
                 patch.object(server_common, 'SERVER_CONFIG', {}):
                cfg = effective_config(client_config)
            self.assertEqual(cfg['model'], 'claude-opus-5-5')
            self.assertEqual(cfg['auxModel'], 'claude-sonnet-5-5')
            self.assertEqual(cfg['cheapModel'], 'claude-haiku-5-5')
            # cheapModel 优先于 auxModel（见 prompt_pipeline._aux_model）
            self.assertEqual(_aux_model(cfg), 'claude-haiku-5-5')
            if not managed:
                self.assertEqual(cfg['reviewModel'], 'claude-fable-5-1')

    def test_aux_model_defaults_to_gemini_3_8(self):
        self.assertEqual(_aux_model({}), 'gemini-3.8-flash-high')
        self.assertEqual(_aux_model({'model': 'gemini-3-flash-agent'}), 'gemini-3.8-flash-high')

    def test_resolve_gateway_for_gemini_3_8(self):
        base_url, api_key = resolve_gateway('gemini-3.8-flash-high', {})
        self.assertIn('8046', base_url)

    def test_state_js_models_removed_and_added(self):
        with open('js/state.js', 'r', encoding='utf-8') as f:
            content = f.read()
        self.assertIn('gemini-3.8-flash-high', content)
        self.assertNotIn('gemini-3.6-flash-high', content)
        self.assertNotIn('gemini-3.1-pro-high', content)

    def test_public_config_template_uses_gemini_3_8(self):
        with (Path(__file__).resolve().parents[1] / 'server_config.example.json').open(encoding='utf-8') as f:
            cfg = json.load(f)
        self.assertEqual(cfg.get('model'), 'gemini-3.8-flash-high')
        self.assertEqual(cfg.get('cheapModel'), 'gemini-3.8-flash-high')

if __name__ == '__main__':
    unittest.main()
