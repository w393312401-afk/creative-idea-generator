// 任务提醒中心（js/notification_center.js）的行为契约。
//
// 为什么需要它：这套强提醒只有在「人已经切到别的软件」时才有意义，而那正是最难
// 手动验证的时刻——出问题时的表现永远是"点了没反应"，看不出是哪一路断了。
// 这里把三条最容易静默失效的链路钉死：
//   1. 闪烁请求必须带访问码（裸 fetch 会被服务端 _gate() 挡成 401，前端一声不吭）；
//   2. 停止闪烁必须带上同一个 title_hint（不带的话服务端退回一串通用关键词，
//      会把别的软件正在闪的任务栏也一并按停）；
//   3. 音效要在被浏览器挂起 / 音量为 0 时报出原因，而不是静默返回。
//
// notification_center.js 是浏览器端的 classic script，用 vm 装进带 DOM 桩的上下文里。
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const fetchCalls = [];

function makeCtx(overrides = {}) {
    const listeners = {};
    const ctx = {
        console,
        document: {
            title: 'SPARK - 创意点子激发中心',
            hidden: true,
            readyState: 'loading',
            hasFocus: () => false,
            addEventListener: () => {},
        },
        navigator: {},
        localStorage: { getItem: () => null },
        setInterval: () => 1,
        clearInterval: () => {},
        setTimeout: (fn) => fn && 0,
        fetch: (url, init) => {
            fetchCalls.push({ url, init: init || {}, body: JSON.parse((init && init.body) || '{}') });
            return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ status: 'ok', flashed: true, matched: 1, foreground: false }) });
        },
        config: { soundNotificationEnabled: true, notificationVolume: 80, taskbarFlashEnabled: true, desktopNotificationEnabled: true },
        ACCESS_CODE: 'secret-code',
        Headers: class {},
        addEventListener: (name, fn) => { listeners[name] = fn; },
        removeEventListener: () => {},
    };
    Object.assign(ctx, overrides);
    ctx.window = ctx;
    vm.createContext(ctx);
    vm.runInContext(
        fs.readFileSync(path.join(__dirname, '..', 'js', 'notification_center.js'), 'utf8'), ctx);
    return ctx;
}

(async () => {
    // 1. 闪烁请求带访问码，并且 hint 取自页面真实标题（而不是写死的 'SPARK'）
    fetchCalls.length = 0;
    const ctx = makeCtx();
    const info = await ctx.NotificationCenter.flashTaskbar();
    const flashCall = fetchCalls.find(c => c.body.stop === false);
    assert.strictEqual(flashCall.url, '/api/notify/flash_taskbar');
    assert.strictEqual(flashCall.init.headers['X-Access-Code'], 'secret-code');
    assert.strictEqual(flashCall.body.title_hint, 'SPARK');
    assert.strictEqual(info.ok, true);

    // 2. 停止闪烁必须带同一个 hint，否则会误停别的软件的任务栏闪烁
    fetchCalls.length = 0;
    await ctx.NotificationCenter.stopTaskbarFlash();
    const stopCall = fetchCalls.find(c => c.body.stop === true);
    assert.strictEqual(stopCall.body.title_hint, 'SPARK');

    // 3. 服务端说没闪成，前端要如实回传原因，不能当成成功
    fetchCalls.length = 0;
    const failing = makeCtx({
        fetch: () => Promise.resolve({
            ok: true, status: 200,
            json: () => Promise.resolve({ status: 'ok', flashed: false, reason: '没有找到可闪烁的窗口' })
        })
    });
    const bad = await failing.NotificationCenter.flashTaskbar();
    assert.strictEqual(bad.ok, false);
    assert.strictEqual(bad.reason, '没有找到可闪烁的窗口');

    // 4. 关掉开关就不该发请求
    fetchCalls.length = 0;
    const off = makeCtx({ config: { taskbarFlashEnabled: false } });
    const skipped = await off.NotificationCenter.flashTaskbar();
    assert.strictEqual(skipped.ok, false);
    assert.strictEqual(fetchCalls.length, 0);

    // 5. 音效在没有 Web Audio 时报原因，而不是静默返回 undefined
    const noAudio = makeCtx();
    assert.strictEqual(await noAudio.NotificationCenter.playSuccessSound(), 'unsupported');

    // 6. AudioContext 被浏览器挂起时也要报出来（自动播放策略的经典静默失败）
    const suspended = makeCtx({
        AudioContext: class { constructor() { this.state = 'suspended'; } resume() { return Promise.resolve(); } }
    });
    assert.strictEqual(await suspended.NotificationCenter.playErrorSound(), 'suspended');

    // 7. 音量为 0 也要能区分出来
    const muted = makeCtx({
        AudioContext: class { constructor() { this.state = 'running'; } resume() { return Promise.resolve(); } },
        config: { notificationVolume: 0 }
    });
    assert.strictEqual(await muted.NotificationCenter.playActionRequiredSound(), 'muted');

    // 8. 自检把每一路的失败原因说清楚，供设置面板直接展示
    const diag = await muted.NotificationCenter.diagnose();
    assert.strictEqual(diag.sound.ok, false);
    assert.match(diag.sound.reason, /音量/);
    assert.strictEqual(diag.desktop.ok, false);   // 桩环境里没有 Notification

    // 9. mac / Linux：服务端说这个平台没有任务栏闪烁，自检要判成"不适用"而不是失败
    const mac = makeCtx({
        AudioContext: class { constructor() { this.state = 'running'; } resume() { return Promise.resolve(); } },
        fetch: () => Promise.resolve({
            ok: true, status: 200,
            json: () => Promise.resolve({
                status: 'ok', flashed: false, supported: false,
                reason: '当前服务端不是 Windows：任务栏闪烁是 Win32 专有能力'
            })
        })
    });
    const macDiag = await mac.NotificationCenter.diagnose();
    assert.strictEqual(macDiag.taskbar.ok, true);
    assert.match(macDiag.taskbar.note, /Windows/);
    assert.strictEqual(macDiag.sound.ok, true);   // 声音在 mac 上照常工作

    console.log('test_notification_center.js: all assertions passed');
})().catch(err => { console.error(err); process.exit(1); });
