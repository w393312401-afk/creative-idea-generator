/* ============================================================
   NotificationCenter · 任务完成/失败全方位强提醒中心
   
   提供四维立体通知反馈：
   1. 🔊 Web Audio 纯前端和弦合成音效（成功/失败/待审核，零外挂音频文件，低延迟）
   2. ⚡ Windows 任务栏高亮闪烁（Win32 FlashWindowEx + Web Notification 联动）
   3. 🔴 任务栏应用图标右上角数字徽标（navigator.setAppBadge）
   4. 🖥️ 操作系统原生桌面 Toast 弹窗（Notification API，点击回跳聚焦网页）
   5. 📑 浏览器后台标签页标题交替闪烁（Tab Flasher，切回自动复原）
   ============================================================ */

(function (global) {
    'use strict';

    let audioCtx = null;
    let tabFlashTimer = null;
    let originalDocumentTitle = document.title || 'Creative Idea Generator';
    let isTabFlashing = false;
    let lastSoundPlayedAt = 0;
    const openNotifications = new Set();

    function getAudioContext() {
        if (!audioCtx) {
            const AudioContextClass = window.AudioContext || window.webkitAudioContext;
            if (AudioContextClass) {
                audioCtx = new AudioContextClass();
            }
        }
        return audioCtx;
    }

    async function ensureAudioContext() {
        const ctx = getAudioContext();
        if (!ctx) return null;
        if (ctx.state === 'suspended') {
            try {
                await ctx.resume();
            } catch (e) {
                console.debug('AudioContext resume failed:', e);
            }
        }
        return ctx;
    }

    // 绑定首次用户手势以激活 AudioContext
    function initAudioOnUserGesture() {
        const unlock = () => {
            ensureAudioContext();
            window.removeEventListener('click', unlock);
            window.removeEventListener('keydown', unlock);
            window.removeEventListener('touchstart', unlock);
        };
        window.addEventListener('click', unlock, { once: true });
        window.addEventListener('keydown', unlock, { once: true });
        window.addEventListener('touchstart', unlock, { once: true });
    }
    if (typeof window !== 'undefined') {
        initAudioOnUserGesture();
    }

    /* ── 1. Web Audio 音效引擎 ──────────────────────────────────── */

    function getMasterVolume() {
        if (typeof config !== 'undefined' && config.soundNotificationEnabled === false) {
            return 0;
        }
        const vol = (typeof config !== 'undefined' && typeof config.notificationVolume === 'number')
            ? config.notificationVolume
            : 80;
        // 标定增益：保证在普通扬声器/耳机下清晰可闻
        return Math.max(0, Math.min(100, vol)) / 100 * 0.85;
    }

    async function playTone(freq, startOffset, duration, gainVal, type = 'sine') {
        const ctx = await ensureAudioContext();
        if (!ctx || gainVal <= 0) return;

        try {
            const startTime = ctx.currentTime + Math.max(0, startOffset) + 0.01;
            const osc = ctx.createOscillator();
            const gain = ctx.createGain();

            osc.type = type;
            osc.frequency.setValueAtTime(freq, startTime);

            // ADSR 包络：线性平滑过渡，绝不出现 click 破音或 DOMException
            gain.gain.setValueAtTime(0.0001, startTime);
            gain.gain.linearRampToValueAtTime(gainVal, startTime + 0.02);
            gain.gain.linearRampToValueAtTime(0.0001, startTime + duration);

            osc.connect(gain);
            gain.connect(ctx.destination);

            osc.start(startTime);
            osc.stop(startTime + duration + 0.05);
        } catch (err) {
            console.warn('playTone error:', err);
        }
    }

    // 🎯 成功完成音效：上扬大三和弦琶音（C5 -> E5 -> G5 -> C6）
    async function playSuccessSound(customVol = null) {
        const ctx = await ensureAudioContext();
        if (!ctx) return 'unsupported';
        // 自动播放策略下 AudioContext 会停在 suspended，声音会被静默丢掉——
        // 返回原因，让上层能提示用户「先在页面里点一下」。
        if (ctx.state !== 'running') return 'suspended';
        const baseVol = customVol !== null ? customVol : getMasterVolume();
        if (baseVol <= 0) return 'muted';

        const notes = [
            { f: 523.25, d: 0.20, offset: 0.00 }, // C5
            { f: 659.25, d: 0.20, offset: 0.09 }, // E5
            { f: 783.99, d: 0.25, offset: 0.18 }, // G5
            { f: 1046.50, d: 0.50, offset: 0.27 } // C6 (高音延音)
        ];

        notes.forEach(n => {
            playTone(n.f, n.offset, n.d, baseVol * 0.9, 'sine');
            playTone(n.f, n.offset, n.d * 0.8, baseVol * 0.35, 'triangle');
        });

        return 'ok';
    }

    // ❌ 失败/异常音效：短促低频双音警示（A4 -> F4）
    async function playErrorSound(customVol = null) {
        const ctx = await ensureAudioContext();
        if (!ctx) return 'unsupported';
        if (ctx.state !== 'running') return 'suspended';
        const baseVol = customVol !== null ? customVol : getMasterVolume();
        if (baseVol <= 0) return 'muted';

        const notes = [
            { f: 440.00, d: 0.22, offset: 0.00 }, // A4
            { f: 349.23, d: 0.40, offset: 0.14 }  // F4 (低音顿挫)
        ];

        notes.forEach(n => {
            playTone(n.f, n.offset, n.d, baseVol * 0.9, 'triangle');
            playTone(n.f, n.offset, n.d, baseVol * 0.5, 'sine');
        });

        return 'ok';
    }

    // 🛑 待审核/人工干预提示音：清脆双音门铃（E5 -> A5）
    async function playActionRequiredSound(customVol = null) {
        const ctx = await ensureAudioContext();
        if (!ctx) return 'unsupported';
        if (ctx.state !== 'running') return 'suspended';
        const baseVol = customVol !== null ? customVol : getMasterVolume();
        if (baseVol <= 0) return 'muted';

        const notes = [
            { f: 659.25, d: 0.20, offset: 0.00 }, // E5
            { f: 880.00, d: 0.42, offset: 0.12 }  // A5
        ];

        notes.forEach(n => {
            playTone(n.f, n.offset, n.d, baseVol * 0.85, 'sine');
            playTone(n.f, n.offset, n.d, baseVol * 0.45, 'triangle');
        });

        return 'ok';
    }

    /* ── 2. Windows 任务栏高亮闪烁 (Win32 FlashWindowEx) ─────────── */

    // 访问码模式下裸 fetch 会被 _gate() 挡成 401——闪烁请求和别的接口一样要带上访问码，
    // 否则远程/加了访问码的部署里任务栏永远不闪，前端还一声不吭。
    function notifyHeaders() {
        const headers = { 'Content-Type': 'application/json' };
        let code = '';
        try {
            code = (typeof ACCESS_CODE !== 'undefined' && ACCESS_CODE)
                ? ACCESS_CODE
                : (localStorage.getItem('spark_access_code') || '');
        } catch (e) {}
        if (code) headers['X-Access-Code'] = code;
        return headers;
    }

    // 服务端靠窗口标题命中浏览器窗口，所以 hint 必须来自页面真实标题，不能写死 'SPARK'
    // （改了 <title> 就再也命中不了）。标题闪烁期间 document.title 是临时值，取原标题。
    function taskbarHint(titleHint) {
        if (titleHint) return titleHint;
        const base = (isTabFlashing ? originalDocumentTitle : (document.title || originalDocumentTitle)) || 'SPARK';
        return (base.split(/[-–—|·]/)[0] || '').trim() || base;
    }

    let lastFlashInfo = null;

    async function flashTaskbar(titleHint = '', stop = false) {
        if (!stop && typeof config !== 'undefined' && config.taskbarFlashEnabled === false) {
            lastFlashInfo = { ok: false, reason: '任务栏闪烁已在设置中关闭' };
            return lastFlashInfo;
        }
        try {
            const res = await fetch('/api/notify/flash_taskbar', {
                method: 'POST',
                headers: notifyHeaders(),
                body: JSON.stringify({ title_hint: taskbarHint(titleHint), stop: !!stop })
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok || data.status !== 'ok') {
                lastFlashInfo = { ok: false, reason: (data && data.message) || ('HTTP ' + res.status) };
                console.warn('[NotificationCenter] 任务栏闪烁请求失败:', lastFlashInfo.reason);
                return lastFlashInfo;
            }
            lastFlashInfo = Object.assign({ ok: !!data.flashed }, data);
            if (!data.flashed && data.reason && data.supported !== false) {
                console.warn('[NotificationCenter] 任务栏未闪烁:', data.reason);
            }
            return lastFlashInfo;
        } catch (e) {
            lastFlashInfo = { ok: false, reason: (e && e.message) || '服务端不可达' };
            console.warn('[NotificationCenter] 任务栏闪烁请求异常:', lastFlashInfo.reason);
            return lastFlashInfo;
        }
    }

    // 登记「本页面所在的浏览器窗口」：页面拿到焦点的那一刻，操作系统的前台窗口
    // 必然就是承载本页面的浏览器窗口，服务端记下它的句柄。之后即便用户切到别的
    // 标签页（浏览器窗口标题会跟着变，标题匹配必然落空），任务栏照样闪得到。
    let lastRegisterAt = 0;
    function registerFlashWindow() {
        if (typeof document !== 'undefined' && !document.hasFocus()) return;
        const now = Date.now();
        if (now - lastRegisterAt < 5000) return;   // 焦点事件很密，节流一下
        lastRegisterAt = now;
        fetch('/api/notify/flash_taskbar', {
            method: 'POST',
            headers: notifyHeaders(),
            body: JSON.stringify({ register: true })
        }).catch(() => {});
    }

    // 停止闪烁必须带上同一个 hint：不带的话服务端会退回一串通用关键词（chrome/edge…），
    // 把别的软件正在闪的任务栏也一并按停。
    function stopTaskbarFlash(titleHint = '') {
        return flashTaskbar(titleHint, true);
    }

    /* ── 3. 任务栏应用图标数字徽标 (App Badge) ───────────────────── */

    function setAppBadge(count = 1) {
        if (typeof navigator !== 'undefined' && 'setAppBadge' in navigator) {
            navigator.setAppBadge(count).catch(() => {});
        }
    }

    function clearAppBadge() {
        if (typeof navigator !== 'undefined' && 'clearAppBadge' in navigator) {
            navigator.clearAppBadge().catch(() => {});
        }
    }

    /* ── 4. 浏览器后台标签页标题交替闪烁 ─────────────────────────── */

    function startTabFlash(message, icon = '🔔') {
        stopTabFlash(); // 先清理旧定时器
        originalDocumentTitle = document.title || 'SPARK - 创意点子激发中心';
        isTabFlashing = true;

        let state = false;
        const cleanTitle = (originalDocumentTitle || 'SPARK').replace(/^[🔔✅❌🛑⚠️ℹ️\s【】[\]\w\u4e00-\u9fa5]+?·\s*/, '').replace(/^【.+?】\s*/, '');
        const flashTitle = `${icon} 【${message}】 ${cleanTitle || originalDocumentTitle}`;

        tabFlashTimer = setInterval(() => {
            if (!isTabFlashing) {
                clearInterval(tabFlashTimer);
                return;
            }
            document.title = state ? flashTitle : originalDocumentTitle;
            state = !state;
        }, 600);
    }

    function stopTabFlash() {
        if (tabFlashTimer) {
            clearInterval(tabFlashTimer);
            tabFlashTimer = null;
        }
        isTabFlashing = false;
        if (originalDocumentTitle) {
            document.title = originalDocumentTitle;
        }
    }

    /* ── 5. 操作系统原生桌面弹窗 (Desktop Notification) ───────────── */

    async function requestDesktopPermission() {
        if (typeof window === 'undefined' || !('Notification' in window)) {
            return 'unsupported';
        }
        if (Notification.permission === 'granted') {
            return 'granted';
        }
        if (Notification.permission !== 'denied') {
            const res = await Notification.requestPermission();
            return res;
        }
        return 'denied';
    }

    async function showDesktopNotification(title, message, type = 'info') {
        if (typeof config !== 'undefined' && config.desktopNotificationEnabled === false) {
            return 'disabled';
        }
        if (typeof window === 'undefined' || !('Notification' in window)) {
            // http://192.168.x.x 这种非安全上下文里 Notification 直接不存在，
            // 页面看起来一切正常却永远弹不出卡片——这里明说原因，别静默吞掉。
            return window.isSecureContext === false ? 'insecure_context' : 'unsupported';
        }

        // 若权限仍是 default，自动申请一次
        if (Notification.permission === 'default') {
            try {
                await Notification.requestPermission();
            } catch (e) {}
        }

        if (Notification.permission !== 'granted') {
            return 'denied';
        }

        const icons = {
            success: '✅',
            error: '❌',
            action_required: '🛑',
            warning: '⚠️',
            info: 'ℹ️'
        };

        const prefix = icons[type] || '🔔';
        const opts = {
            body: message || '',
            tag: 'creative_studio_task_' + type,
            renotify: true,
            // Windows 的系统 toast 默认几秒后自动收进「通知中心」，人还没切回来就没了。
            // requireInteraction 让卡片挂在屏幕上直到用户点掉——强提醒的意义就在这。
            requireInteraction: true,
            silent: true // 声音由 Web Audio 统一精确控制
        };

        try {
            const notif = new Notification(`${prefix} ${title}`, opts);
            openNotifications.add(notif);
            notif.onclose = () => openNotifications.delete(notif);
            notif.onclick = function () {
                window.focus();
                notif.close();
                openNotifications.delete(notif);
                onWindowActivated();
            };
            return 'ok';
        } catch (e) {
            // 部分平台（含装成 PWA 的场景）禁止 new Notification()，只能走 Service Worker
            try {
                if (navigator.serviceWorker && navigator.serviceWorker.ready) {
                    const reg = await navigator.serviceWorker.ready;
                    await reg.showNotification(`${prefix} ${title}`, opts);
                    return 'ok';
                }
            } catch (e2) {}
            console.debug('Desktop notification failed:', e);
            return 'error:' + ((e && e.message) || 'unknown');
        }
    }

    /* ── 6. 窗口激活/切回前台事件监听 ───────────────────────────── */

    function onWindowActivated() {
        stopTabFlash();
        clearAppBadge();
        stopTaskbarFlash();
        // requireInteraction 的卡片不会自己消失，人已经回来了就替他收掉
        openNotifications.forEach(n => {
            try { n.close(); } catch (e) {}
        });
        openNotifications.clear();
    }

    if (typeof window !== 'undefined') {
        window.addEventListener('focus', () => {
            onWindowActivated();
            registerFlashWindow();
        });
        document.addEventListener('visibilitychange', () => {
            if (!document.hidden) {
                onWindowActivated();
                registerFlashWindow();
            }
        });
        // 页面本来就在前台打开的场景（刷新、首次进入）也要登记一次
        if (document.readyState === 'complete') {
            registerFlashWindow();
        } else {
            window.addEventListener('load', registerFlashWindow, { once: true });
        }
    }

    /* ── 7. 统一派发入口 (NotificationCenter.notify) ─────────────── */

    const NotificationCenter = {
        /**
         * 触发全套多模态强提醒
         */
        notify(options = {}) {
            const {
                type = 'info',
                title = '任务通知',
                message = '',
                sound = true,
                flashTaskbar: shouldFlash = true,
                desktop = true,
                badge = true,
                force = false
            } = options;

            const result = { sound: 'skipped', desktop: 'skipped', taskbar: 'skipped', tabFlash: false };

            // 1. 播放声音 (提供 300ms 防抖)
            const now = Date.now();
            if (sound && now - lastSoundPlayedAt > 300) {
                lastSoundPlayedAt = now;
                let p = null;
                if (type === 'success') {
                    p = playSuccessSound();
                } else if (type === 'error') {
                    p = playErrorSound();
                } else if (type === 'action_required' || type === 'warning') {
                    p = playActionRequiredSound();
                }
                if (p) {
                    result.sound = 'pending';
                    result.soundPromise = p.then(r => (result.sound = r || 'ok'));
                }
            }

            const isBackground = document.hidden || !document.hasFocus();

            // 2. 桌面系统通知
            if (desktop && (isBackground || force)) {
                result.desktop = 'pending';
                result.desktopPromise = showDesktopNotification(title, message, type)
                    .then(r => (result.desktop = r));
            }

            // 3. Windows 任务栏闪烁
            if (shouldFlash && (isBackground || force)) {
                result.taskbar = 'pending';
                result.taskbarPromise = flashTaskbar().then(r => (result.taskbar = r));
            }

            // 4. 任务栏徽标
            if (badge && (isBackground || force)) {
                setAppBadge(1);
            }

            // 5. 标签页标题闪烁
            if (isBackground || force) {
                const iconMap = { success: '✅', error: '❌', action_required: '🛑', warning: '⚠️', info: '🔔' };
                startTabFlash(title, iconMap[type] || '🔔');
                result.tabFlash = true;
            }

            return result;
        },

        /**
         * 逐通道自检：把「提醒没响」拆成具体是哪一路没通，供测试按钮直接展示给用户。
         */
        async diagnose() {
            const out = {};

            const soundOff = (typeof config !== 'undefined' && config.soundNotificationEnabled === false);
            if (soundOff) {
                out.sound = { ok: false, reason: '声音提醒已在设置中关闭' };
            } else {
                const ctx = await ensureAudioContext();
                if (!ctx) {
                    out.sound = { ok: false, reason: '浏览器不支持 Web Audio' };
                } else if (ctx.state !== 'running') {
                    out.sound = { ok: false, reason: `音频被浏览器挂起（${ctx.state}），请先在页面里点一下任意位置` };
                } else if (getMasterVolume() <= 0) {
                    out.sound = { ok: false, reason: '提示音量为 0%' };
                } else {
                    out.sound = { ok: true };
                }
            }

            if (typeof config !== 'undefined' && config.desktopNotificationEnabled === false) {
                out.desktop = { ok: false, reason: '桌面通知已在设置中关闭' };
            } else if (!('Notification' in window)) {
                out.desktop = {
                    ok: false,
                    reason: window.isSecureContext === false
                        ? '当前是非安全上下文（用 IP 直连），浏览器禁用了桌面通知，请改用 localhost 或 https 访问'
                        : '浏览器不支持桌面通知'
                };
            } else if (Notification.permission !== 'granted') {
                out.desktop = { ok: false, reason: `桌面通知未授权（${Notification.permission}）` };
            } else {
                out.desktop = { ok: true };
            }

            if (typeof config !== 'undefined' && config.taskbarFlashEnabled === false) {
                out.taskbar = { ok: false, reason: '任务栏闪烁已在设置中关闭' };
            } else {
                const info = await flashTaskbar();
                if (info && info.supported === false) {
                    // mac / Linux：任务栏闪烁是 Win32 专有能力，这一路是"不适用"
                    // 而不是"坏了"，别拿它去吓用户。
                    out.taskbar = { ok: true, note: info.reason || '当前系统没有任务栏闪烁，强提醒由声音与桌面通知承担' };
                } else if (!info || info.ok === false) {
                    out.taskbar = { ok: false, reason: (info && info.reason) || '服务端未响应' };
                } else if (info.foreground) {
                    // Windows 规定：窗口已经在前台时 FlashWindowEx 不产生任何可见效果。
                    // 「立即测试」正好就是这种情况，看不到闪烁是正常的，得说清楚。
                    out.taskbar = { ok: true, note: '窗口当前在前台，Windows 不会闪烁；请用「3 秒延时测试」切走后再看' };
                } else {
                    out.taskbar = { ok: true, note: `已命中 ${info.matched || 1} 个窗口` };
                }
            }

            return out;
        },

        playSuccessSound,
        playErrorSound,
        playActionRequiredSound,
        ensureAudioContext,

        flashTaskbar,
        stopTaskbarFlash,
        registerFlashWindow,
        setAppBadge,
        clearAppBadge,
        requestDesktopPermission,
        showDesktopNotification,
        startTabFlash,
        stopTabFlash,
        onWindowActivated,

        /**
         * 立即触发测试
         */
        async testInstantNotification(type = 'success') {
            await ensureAudioContext();

            // 若桌面通知未授权，主动申请
            if ('Notification' in window && Notification.permission === 'default') {
                await Notification.requestPermission();
                if (typeof updateDesktopPermissionStatus === 'function') {
                    updateDesktopPermissionStatus();
                }
            }

            const sampleData = {
                success: { title: '强提醒测试：任务完成', message: '所有关键帧与视频序列已就绪，耗时 42s！' },
                error: { title: '强提醒测试：任务异常', message: '第 3 拍渲染超时，请检查网络或后端配置。' },
                action_required: { title: '强提醒测试：等待审核', message: '第 1 批关键帧已就绪，请在工作台确认是否通过。' }
            };
            const item = sampleData[type] || sampleData.success;
            NotificationCenter.notify({
                type,
                title: item.title,
                message: item.message,
                force: true // 测试模式下强制触发桌面弹窗与闪烁
            });

            // 自检结果直接摆给用户：哪一路没通、为什么，不再只有「点了没反应」
            const diag = await NotificationCenter.diagnose();
            const label = { sound: '声音', desktop: '桌面通知', taskbar: '任务栏闪烁' };
            const bad = Object.keys(diag).filter(k => !diag[k].ok);
            if (typeof showToast === 'function') {
                if (bad.length) {
                    showToast(
                        '⚠️ 强提醒自检：' + bad.map(k => `${label[k]}未生效（${diag[k].reason}）`).join('；'),
                        'warning', 9000
                    );
                } else {
                    const notes = Object.keys(diag).map(k => diag[k].note).filter(Boolean);
                    showToast(
                        '✅ 强提醒三通道自检全部通过' + (notes.length ? `：${notes.join('；')}` : ''),
                        'success', 6000
                    );
                }
            }
            return diag;
        },

        /**
         * 延时 3 秒测试：留出时间切到其他软件体验任务栏闪烁与弹窗
         */
        startCountdownTest(seconds = 3, type = 'success') {
            let remain = seconds;
            if (typeof showToast === 'function') {
                showToast(`⏱️ 已启动 ${remain} 秒倒计时！请立即切到其他软件（如微信/剪映），测试任务栏闪烁与桌面通知`, 'info', remain * 1000 + 1000);
            }
            setTimeout(async () => {
                await NotificationCenter.testInstantNotification(type);
            }, remain * 1000);
        }
    };

    global.NotificationCenter = NotificationCenter;

})(typeof window !== 'undefined' ? window : this);
