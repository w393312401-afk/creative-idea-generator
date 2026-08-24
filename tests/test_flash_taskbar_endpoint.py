import os
import unittest
import server_common


class TestFlashTaskbar(unittest.TestCase):
    def test_win_flash_taskbar_function_exists(self):
        self.assertTrue(hasattr(server_common, 'win_flash_taskbar'))

    def test_win_flash_taskbar_invocation_safe(self):
        # Should return boolean (True on Windows, False on non-Windows) and never throw exception
        res = server_common.win_flash_taskbar(title_hint="test", stop=True)
        self.assertIsInstance(res, bool)

    def test_detail_mode_returns_diagnosis(self):
        # detail=True 回诊断字典：前端拿它告诉用户「没闪」到底卡在哪一步
        res = server_common.win_flash_taskbar(title_hint="test", stop=True, detail=True)
        self.assertIsInstance(res, dict)
        for key in ('flashed', 'matched', 'foreground', 'reason'):
            self.assertIn(key, res)
        if os.name != 'nt':
            self.assertFalse(res['flashed'])
            self.assertTrue(res['reason'])

    def test_register_flash_target_is_safe(self):
        # 页面拿到焦点时登记承载它的浏览器窗口；非 Windows 上只是空转，不能抛
        self.assertTrue(hasattr(server_common, 'win_register_flash_target'))
        title = server_common.win_register_flash_target()
        self.assertIsInstance(title, str)

    @unittest.skipUnless(os.name == 'nt', 'FlashWindowEx 只在 Windows 上有意义')
    def test_registered_window_is_used_when_title_no_longer_matches(self):
        # 浏览器窗口标题跟的是当前活动标签页：用户切走标签页后，标题匹配必然落空。
        # 这时必须回落到登记过的窗口句柄，而不是「当前前台窗口」——对前台窗口调
        # FlashWindowEx 不产生任何可见效果，等于白闪。
        import ctypes
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        if not hwnd:
            self.skipTest('拿不到前台窗口')
        server_common.win_register_flash_target(hwnd)
        res = server_common.win_flash_taskbar(
            title_hint='zzz-title-that-matches-nothing', stop=True, detail=True)
        self.assertTrue(res['flashed'])
        self.assertEqual(res['matched'], 1)
        self.assertIn('登记', res['reason'])


if __name__ == '__main__':
    unittest.main()
