// Finished media uses the recorded rate and moves the existing player after the cover.
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

function node(id) {
    const classes = new Set();
    return {
        id, parentNode: null, children: [], style: {}, dataset: {}, textContent: '',
        classList: {
            toggle(name, enabled) { enabled ? classes.add(name) : classes.delete(name); },
            contains(name) { return classes.has(name); },
        },
        insertBefore(child, before) {
            if (child === before) return child;
            if (child.parentNode) {
                const siblings = child.parentNode.children;
                siblings.splice(siblings.indexOf(child), 1);
            }
            const index = before ? this.children.indexOf(before) : this.children.length;
            assert.ok(index >= 0, 'insertBefore reference must belong to the parent');
            this.children.splice(index, 0, child);
            child.parentNode = this;
            return child;
        },
        get nextSibling() {
            if (!this.parentNode) return null;
            const siblings = this.parentNode.children;
            return siblings[siblings.indexOf(this) + 1] || null;
        },
        addEventListener() {},
        removeAttribute(name) { delete this[name]; },
        load() {},
    };
}

const elements = Object.fromEntries([
    'videos-grid', 'videos-meta', 'merged-video-container', 'merged-video-player',
    'merged-video-info', 'merged-video-download', 'merged-video-heading',
    'merged-video-description', 'merged-video-reveal', 'cover-section', 'frames-section',
].map(id => [id, node(id)]));
const overview = node('overview');
const leftColumn = node('result-left-column');
const originalParent = node('video-wrapper');
overview.insertBefore(leftColumn, null);
leftColumn.insertBefore(elements['cover-section'], null);
overview.insertBefore(elements['frames-section'], null);
overview.insertBefore(originalParent, null);
originalParent.insertBefore(elements['merged-video-container'], null);
const editSyncs = [];
const leftColumnSyncs = [];
const ctx = {
    syncCodexVideoEditor: (merged, idea) => editSyncs.push({ merged, idea }),
    syncResultLeftColumnForMerged: ready => leftColumnSyncs.push(ready),
    console, URLSearchParams,
    document: { getElementById: id => elements[id] },
    slotRenderTarget: () => elements['videos-grid'],
    clearSlotGrid() {},
};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'js', 'media_renderer.js'), 'utf8'), ctx);

for (const rate of [1, 1.5, 2, 4, '4']) {
    assert.strictEqual(ctx.mergedVideoSpeed({ speed: rate }), Number(rate));
}
for (const rate of [undefined, null, '', 0, -1, 'unknown', Infinity]) {
    assert.strictEqual(ctx.mergedVideoSpeed({ speed: rate }), null);
}

const idea = { title: 'example', frameRun: { videos: [], merged_video: {
    status: 'success', url: '/outputs/example/videos/merged_4x.mp4', speed: 4,
    duration_seconds: 12, size_bytes: 1024 * 1024,
} } };
const container = elements['merged-video-container'];
const player = elements['merged-video-player'];
ctx.renderVideosForIdea(idea);
// 成品区是 HTML 里的固定位置，渲染只切换显示，不再搬 DOM。
assert.strictEqual(container.parentNode, originalParent);
assert.strictEqual(container.style.display, 'block');
assert.strictEqual(leftColumnSyncs.at(-1), true);
assert.match(elements['merged-video-info'].textContent, /4倍速/);
assert.match(elements['merged-video-description'].textContent, /4 倍速/);
assert.strictEqual(elements['merged-video-download'].download, 'example_merged_4x.mp4');
assert.strictEqual(player.src, idea.frameRun.merged_video.url);
assert.strictEqual(editSyncs.at(-1).merged, idea.frameRun.merged_video);

// Re-rendering preserves node identity and never moves the container.
ctx.renderVideosForIdea(idea);
assert.strictEqual(elements['merged-video-player'], player);
assert.strictEqual(container.parentNode, originalParent);
assert.strictEqual(originalParent.children.filter(child => child.id === 'merged-video-container').length, 1);

// Missing metadata must not invent a 2x claim or a 2x filename.
delete idea.frameRun.merged_video.speed;
ctx.renderVideosForIdea(idea);
assert.match(elements['merged-video-info'].textContent, /倍速未记录/);
assert.doesNotMatch(elements['merged-video-description'].textContent, /倍速/);
assert.strictEqual(elements['merged-video-download'].download, 'example_merged.mp4');

// Changing to a project without a finished result hides the block and releases the cover column.
ctx.renderVideosForIdea({ frameRun: { videos: [] } });
assert.strictEqual(container.parentNode, originalParent);
assert.strictEqual(container.style.display, 'none');
assert.strictEqual(leftColumnSyncs.at(-1), false);
assert.strictEqual(player.src, undefined);
assert.strictEqual(editSyncs.at(-1).merged, null);

console.log('result media UI tests passed');
