# -*- coding: utf-8 -*-
import unittest
from unittest.mock import MagicMock

from integrations.google_fx.models import VideoRequest
from integrations.google_fx.services.google_fx_helpers import (
    check_fx_config,
    _click_video_resolution_tab,
)


class TestGoogleFxResolutionGate(unittest.TestCase):
    def test_check_fx_config_detects_360p(self):
        status_text = 'Video · 360p · 8s crop_9_16 x1'
        checks = check_fx_config(
            status_text,
            model='Omni Flash',
            orientation='Portrait',
            count='1x',
            duration='8s',
            want_video=True,
            resolved_model_text='Omni Flash',
            resolution='360p'
        )
        self.assertTrue(checks.get('resolution'))
        self.assertTrue(checks.get('duration'))
        self.assertTrue(checks.get('count'))
        self.assertTrue(checks.get('mode'))

    def test_check_fx_config_detects_720p(self):
        status_text = 'Video · 720p · 8s crop_9_16 x1'
        checks = check_fx_config(
            status_text,
            model='Omni Flash',
            orientation='Portrait',
            count='1x',
            duration='8s',
            want_video=True,
            resolved_model_text='Omni Flash',
            resolution='720p'
        )
        self.assertTrue(checks.get('resolution'))

    def test_check_fx_config_fails_on_mismatch_resolution(self):
        status_text = 'Video · 360p · 8s crop_9_16 x1'
        checks = check_fx_config(
            status_text,
            model='Omni Flash',
            orientation='Portrait',
            count='1x',
            duration='8s',
            want_video=True,
            resolved_model_text='Omni Flash',
            resolution='720p'
        )
        self.assertFalse(checks.get('resolution'))

    def test_click_video_resolution_tab_selector(self):
        mock_page = MagicMock()
        mock_scope = MagicMock()
        mock_btn = MagicMock()
        mock_btn.is_visible.return_value = True
        mock_btn.get_attribute.side_effect = lambda attr: 'inactive' if attr == 'data-state' else 'false'
        mock_scope.locator.return_value.first = mock_btn

        result = _click_video_resolution_tab(mock_page, mock_scope, '360p')
        self.assertIn('VIDEO_RESOLUTION_360P', result)
        mock_btn.click.assert_called_once_with(force=True)

    def test_click_video_resolution_tab_already_active(self):
        mock_page = MagicMock()
        mock_scope = MagicMock()
        mock_btn = MagicMock()
        mock_btn.is_visible.return_value = True
        mock_btn.get_attribute.side_effect = lambda attr: 'active' if attr == 'data-state' else 'true'
        mock_scope.locator.return_value.first = mock_btn

        result = _click_video_resolution_tab(mock_page, mock_scope, '720p')
        self.assertIn('already active', result)
        mock_btn.click.assert_not_called()

    def test_video_request_model_accepts_resolution(self):
        req = VideoRequest(
            prompt='test prompt',
            model='Omni Flash',
            duration='8',
            resolution='360p',
            ratio='9:16'
        )
        self.assertEqual(req.resolution, '360p')
        self.assertEqual(req.duration, '8')

    def test_resolve_video_resolution_omni(self):
        import server_common
        from server_common import resolve_video_resolution
        self.assertEqual(resolve_video_resolution({'videoModel': 'Omni Flash', 'videoResolution': '360p'}), '360p')
        self.assertEqual(resolve_video_resolution({'videoModel': 'Omni Flash', 'videoResolution': '720p'}), '720p')
        # 配置里没写分辨率时先回落 SERVER_CONFIG，再回落 OMNI_DEFAULT_VIDEO_RESOLUTION。
        # 这里必须把 SERVER_CONFIG 里的值摘掉，否则断言测的是本机 server_config.json
        # 当前恰好存着什么（开发机存 '360p' 时这条就红），而不是代码的默认值。
        saved = server_common.SERVER_CONFIG.pop('videoResolution', None)
        try:
            self.assertEqual(resolve_video_resolution({'videoModel': 'Omni Flash'}),
                             server_common.OMNI_DEFAULT_VIDEO_RESOLUTION)
        finally:
            if saved is not None:
                server_common.SERVER_CONFIG['videoResolution'] = saved

    def test_resolve_video_resolution_non_omni(self):
        from server_common import resolve_video_resolution
        self.assertIsNone(resolve_video_resolution({'videoModel': 'Veo 3.1 - Fast', 'videoResolution': '360p'}))
        self.assertIsNone(resolve_video_resolution({'videoModel': 'Veo 3.1 - Quality', 'videoResolution': '720p'}))


if __name__ == '__main__':
    unittest.main()
