import unittest
import json
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

    def test_default_model_in_effective_config(self):
        cfg = effective_config({})
        self.assertEqual(cfg.get('model'), 'gemini-3.8-flash-high')

    def test_effective_config_upgrades_obsolete_models(self):
        self.assertEqual(effective_config({'model': 'gemini-3-flash'}).get('model'), 'gemini-3.8-flash-high')
        self.assertEqual(effective_config({'model': 'gemini-3.5-flash'}).get('model'), 'gemini-3.8-flash-high')
        self.assertEqual(effective_config({'model': 'gemini-3.6-flash-high'}).get('model'), 'gemini-3.8-flash-high')
        self.assertEqual(effective_config({'model': 'gemini-3.1-pro-high'}).get('model'), 'gemini-3.8-flash-high')
        self.assertEqual(effective_config({'cheapModel': 'gemini-3.5-flash-low'}).get('cheapModel'), 'gemini-3.8-flash-high')

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

    def test_server_config_uses_gemini_3_8(self):
        with open('server_config.json', 'r', encoding='utf-8') as f:
            cfg = json.load(f)
        self.assertEqual(cfg.get('model'), 'gemini-3.8-flash-high')
        self.assertEqual(cfg.get('cheapModel'), 'gemini-3.8-flash-high')

if __name__ == '__main__':
    unittest.main()
