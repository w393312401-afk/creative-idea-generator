r"""「定位本地文件」在 Windows 上的两处坑（都表现为"点了没反应/不弹窗"）。

1. 命令行拼法：`explorer /select,<路径>` 必须走**字符串**命令行，路径单独加引号。
   传 list 时 subprocess.list2cmdline 会把 `/select,C:\a b\c.mp4` 整个套上引号
   （用户名含空格时必然触发），explorer 认不出这种写法，窗口根本不开。
2. 前台锁：服务端是后台进程（pythonw），新开的资源管理器窗口默认抢不到前台，
   只在任务栏闪一下。拉起后要按窗口标题找到它再强制置前——标题是
   「videos - 文件资源管理器」这种带后缀的形式，只能前缀匹配。
"""
import os

import pytest

import server_common


class TestWindowsRevealCommand:
    @pytest.fixture
    def media(self, tmp_path):
        p = tmp_path / 'outputs' / 'a b 项目' / 'videos'
        p.mkdir(parents=True)
        f = p / 'vid_001.mp4'
        f.write_bytes(b'x')
        return str(tmp_path), 'outputs/a b 项目/videos/vid_001.mp4'

    def test_uses_string_cmdline_with_only_the_path_quoted(self, media, monkeypatch):
        base_dir, rel = media
        launched = []

        monkeypatch.setattr(server_common.os, 'name', 'nt')
        monkeypatch.setattr(server_common.sys, 'platform', 'win32')
        monkeypatch.setattr(server_common, '_win_find_explorer_windows', lambda folder=None: [1])
        focused = []
        monkeypatch.setattr(
            server_common, '_win_focus_revealed_window',
            lambda folder, known=(), **kw: focused.append((folder, tuple(known))))

        import subprocess
        monkeypatch.setattr(subprocess, 'Popen', lambda cmd, **kw: launched.append(cmd))

        abs_p = server_common.reveal_media_in_file_manager(rel, base_dir=base_dir)

        assert len(launched) == 1
        cmd = launched[0]
        # 字符串命令行，不是 list——list 会被 list2cmdline 整体加引号
        assert isinstance(cmd, str)
        assert cmd.lower().endswith('/select,"%s"' % abs_p.lower())
        assert 'explorer.exe' in cmd.lower()

    def test_schedules_foreground_grab_with_pre_launch_snapshot(self, media, monkeypatch):
        base_dir, rel = media
        monkeypatch.setattr(server_common.os, 'name', 'nt')
        monkeypatch.setattr(server_common.sys, 'platform', 'win32')
        monkeypatch.setattr(server_common, '_win_find_explorer_windows', lambda folder=None: [11, 22])
        seen = []
        monkeypatch.setattr(
            server_common, '_win_focus_revealed_window',
            lambda folder, known=(), **kw: seen.append((folder, list(known))))

        import subprocess
        monkeypatch.setattr(subprocess, 'Popen', lambda cmd, **kw: None)

        started = []

        class _SyncThread:
            def __init__(self, target=None, daemon=None, **kw):
                self._target = target

            def start(self):
                started.append(1)
                self._target()

        # reveal_media_in_file_manager 里是函数内 import threading，patch 模块即可
        import threading
        monkeypatch.setattr(threading, 'Thread', _SyncThread)

        abs_p = server_common.reveal_media_in_file_manager(rel, base_dir=base_dir)

        assert started == [1]
        # 抢前台盯的是文件所在目录，且带着"拉起前就存在的窗口"快照，
        # 好把新开的那个从复用的旧窗口里区分出来
        assert seen == [(os.path.dirname(abs_p), [11, 22])]


class TestExplorerTitleMatch:
    @pytest.mark.parametrize('title', [
        'videos',                       # 纯目录名（部分系统/语言）
        'videos - 文件资源管理器',        # 中文 Windows 11 的实际标题
        'videos - File Explorer',       # 英文系统
        r'C:\out\proj\videos - 文件资源管理器',  # 开了"标题栏显示完整路径"
    ])
    def test_matches_real_explorer_titles(self, title):
        assert server_common._win_explorer_title_matches(title, r'C:\out\proj\videos')

    @pytest.mark.parametrize('title', [
        '',
        'videos2 - 文件资源管理器',   # 同前缀的别的目录，不能误认
        'frames - 文件资源管理器',
        'downloads',
    ])
    def test_rejects_other_windows(self, title):
        assert not server_common._win_explorer_title_matches(title, r'C:\out\proj\videos')
