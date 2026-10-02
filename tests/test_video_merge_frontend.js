// Manual merge results must keep their owner across project navigation.
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const app = fs.readFileSync(path.join(__dirname, '../app.js'), 'utf8');
const merge = app.slice(app.indexOf('async function mergeVideos('),
    app.indexOf('// 合成被门禁拦截时'));
const media = fs.readFileSync(path.join(__dirname, '../js/media_renderer.js'), 'utf8');
const cache = media.slice(0, media.indexOf('function safeSetImageSrc('));

function setup() {
    let resolve, reject, clock = 1000;
    const owner = { id: 'owner', title: 'Owner', frameRun: { videos: [] } };
    const savedOwner = { id: 'owner', title: 'Owner', frameRun: { videos: [] } };
    const other = { id: 'other', title: 'Other', frameRun: {
        videos: [], merged_video: { url: '/outputs/other/old.mp4' },
    } };
    const button = { innerHTML: '<span class="step-stat">未合并</span>', disabled: false };
    const meta = { textContent: '', innerHTML: '' };
    const calls = { requests: [], persisted: [], rendered: [], saved: [], blocked: [], toasts: [] };
    const ctx = {
        console: { error() {} }, URLSearchParams, Date: { now: () => ++clock },
        currentIdea: owner, savedIdeas: [savedOwner, other], config: {}, mergeInFlight: false,
        document: { getElementById: id => id === 'merge-videos-btn' ? button : meta },
        getMergeSpeed: () => 2, mergeSpeedLabel: rate => `${rate}倍速`, getCoverBurn: () => 'frame',
        getIdeaSaveTitle: idea => idea.id, coverRoleUrl: idea => `/outputs/${idea.id}/cover.webp`,
        escapeHtml: value => String(value), updatePipelineBar() {},
        saveCurrentIdeaState: () => calls.saved.push(ctx.currentIdea.id),
        persistIdeaItem: async idea => calls.persisted.push(idea.id),
        renderVideosForIdea: idea => calls.rendered.push({ id: idea.id,
            url: ctx.cacheBustedUrl(idea.frameRun.merged_video.url) }),
        renderMergeBlocked: data => calls.blocked.push(data),
        showToast: (...args) => calls.toasts.push(args),
        fetch: (url, init) => {
            calls.requests.push({ url, body: JSON.parse(init.body) });
            return new Promise((done, failed) => { resolve = done; reject = failed; });
        },
    };
    vm.createContext(ctx);
    vm.runInContext(cache + '\n' + merge, ctx);
    return { ctx, owner, savedOwner, other, button, meta, calls,
        reply: (status, data) => resolve({ ok: status < 400, status, json: async () => data }),
        fail: error => reject(error) };
}

const merged = { status: 'success', url: '/outputs/owner/Owner_2x.mp4', speed: 2 };
const success = { status: 'ok', merged_video: merged };

(async () => {
    {
        const s = setup();
        const pending = s.ctx.mergeVideos();
        s.ctx.currentIdea = s.other;
        s.meta.textContent = 'Other project';
        s.reply(200, success);
        await pending;
        assert.strictEqual(s.owner.frameRun.merged_video, merged);
        assert.strictEqual(s.savedOwner.frameRun.merged_video, merged);
        assert.strictEqual(s.other.frameRun.merged_video.url, '/outputs/other/old.mp4');
        assert.deepStrictEqual(s.calls.persisted, ['owner']);
        assert.deepStrictEqual(s.calls.saved, []);
        assert.deepStrictEqual(s.calls.rendered, []);
        assert.strictEqual(s.meta.textContent, 'Other project');
        assert.strictEqual(s.calls.requests[0].body.title, 'owner');
        assert.strictEqual(s.calls.requests[0].body.cover, '/outputs/owner/cover.webp');
        assert.strictEqual(s.button.disabled, false);
        assert.strictEqual(s.ctx.mergeInFlight, false);
    }
    {
        const s = setup();
        s.ctx.persistIdeaItem = async idea => {
            s.calls.persisted.push(idea.id);
            s.ctx.currentIdea = s.other;
            s.meta.textContent = 'Other project';
        };
        const pending = s.ctx.mergeVideos();
        s.reply(200, success);
        await pending;
        assert.deepStrictEqual(s.calls.saved, ['owner']);
        assert.deepStrictEqual(s.calls.rendered, [], 'navigation during persistence must still suppress old UI delivery');
        assert.strictEqual(s.meta.textContent, 'Other project');
    }
    {
        const s = setup();
        s.owner.frameRun.merged_video = { ...merged };
        const oldUrl = s.ctx.cacheBustedUrl(merged.url);
        const pending = s.ctx.mergeVideos();
        s.reply(200, success);
        await pending;
        const newUrl = s.calls.rendered[0].url;
        assert.notStrictEqual(newUrl, oldUrl, 'overwriting the same finished file must invalidate its playback cache');
        assert.match(newUrl, /\?v=\d+$/);
        assert.strictEqual(s.ctx.cacheBustedUrl(merged.url), newUrl, 'passive renders must retain the version');
        assert.deepStrictEqual(s.calls.saved, ['owner']);
        assert.match(s.meta.textContent, /视频合并已完成/);
        const next = s.ctx.mergeVideos();
        s.reply(200, success);
        await next;
        assert.notStrictEqual(s.calls.rendered[1].url, newUrl, 'each successful overwrite must refresh the player');
    }
    for (const outcome of ['blocked', 'failure']) {
        const s = setup();
        const pending = s.ctx.mergeVideos();
        s.ctx.currentIdea = s.other;
        s.meta.textContent = 'Other project';
        s.meta.innerHTML = 'Other controls';
        if (outcome === 'blocked') s.reply(409, { status: 'blocked', missing: [2] });
        else s.fail(new Error('encoding failed'));
        await pending;
        assert.strictEqual(s.meta.textContent, 'Other project');
        assert.strictEqual(s.meta.innerHTML, 'Other controls');
        assert.deepStrictEqual(s.calls.blocked, []);
        assert.deepStrictEqual(s.calls.persisted, []);
        assert.strictEqual(s.ctx.mergeInFlight, false);
    }
    {
        const s = setup();
        const pending = s.ctx.mergeVideos();
        s.ctx.currentIdea = null;
        s.reply(200, success);
        await pending;
        assert.strictEqual(s.owner.frameRun.merged_video, merged);
        assert.deepStrictEqual(s.calls.persisted, ['owner']);
        assert.deepStrictEqual(s.calls.rendered, []);
    }
    {
        const s = setup();
        const pending = s.ctx.mergeVideos();
        s.ctx.currentIdea = s.other;
        await s.ctx.mergeVideos(true);
        assert.strictEqual(s.calls.requests.length, 1, 'duplicate invocation must not create a competing merge');
        assert.strictEqual(s.ctx.mergeInFlight, true, 'rejected duplicate must retain the first operation state');
        s.reply(200, success);
        await pending;
    }
    {
        const s = setup();
        const pending = s.ctx.mergeVideos(true);
        s.reply(200, { status: 'ok', merged_video: { ...merged, partial: true, skipped_slots: [2] } });
        await pending;
        assert.strictEqual(s.calls.requests[0].body.force, true);
        assert.match(s.meta.innerHTML, /槽位 <b>2<\/b>/);
    }
    console.log('Manual video merge ownership and cache tests passed');
})().catch(error => { console.error(error); process.exit(1); });
