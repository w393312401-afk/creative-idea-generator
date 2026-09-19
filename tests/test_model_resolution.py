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
        self.assertEqual(resolve_chat_model('gemini-3-flash'), 'gemini-3.8-flash-high')
        self.assertEqual(resolve_chat_model('gemini-3-flash-agent'), 'gemini-3.8-flash-high')
        self.assertEqual(resolve_chat_model('gemini-3.5-flash'), 'gemini-3.8-flash-high')
        self.assertEqual(resolve_chat_model('gemini-3.6-flash-high'), 'gemini-3.8-flash-high')
        self.assertEqual(resolve_chat_model('gemini-3.1-pro-high'), 'gemini-3.8-flash-high')

    def test_resolve_chat_model_preserves_other_models(self):
        self.assertEqual(resolve_chat_model('gemini-3.7-flash-high'), 'gemini-3.7-flash-high')
        self.assertEqual(resolve_chat_model('gpt-5.5'), 'gpt-5.5')
        self.assertEqual(resolve_chat_model('claude-sonnet-4-6'), 'claude-sonnet-4-6')

    def test_new_models_use_codex_gateway(self):
        config = {'baseUrl': 'http://gemini.test/v1', 'apiKey': 'gemini-key',
                  'codexBaseUrl': 'http://codex.test/v1', 'codexApiKey': 'codex-key'}
        for model in ('gpt-6-astra', 'gpt-image-2.5'):
            with self.subTest(model=model):
                self.assertEqual(resolve_chat_model(model), model)
                self.assertEqual(resolve_gateway(model, config),
                                 ('http://codex.test/v1', 'codex-key'))

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

    def test_unmanaged_config_preserves_client_model_contract(self):
        with patch.object(server_common, 'SERVER_MANAGED', False), \
             patch.object(server_common, 'SERVER_CONFIG', {}):
            self.assertNotIn('model', effective_config({}))
            self.assertEqual(effective_config({'model': 'custom-model'})['model'], 'custom-model')
            self.assertEqual(effective_config({'model': 'gemini-3-flash'})['model'], 'gemini-3-flash')

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
