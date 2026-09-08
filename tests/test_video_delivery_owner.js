const assert = require('assert');
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync(require('path').join(__dirname, '../js/api_client.js'), 'utf8');
const fn = source.slice(source.indexOf('function renderVideoSlotDone('), source.indexOf('// busy=true：'));
const owner = { id: 'owner', frameRun: { videos: [] } };
const other = { id: 'other', frameRun: { videos: [] } };
const rendered = [];
const context = { currentIdea: other, isIdeaTaskActive: () => true,
    isViewingIdea: id => id === context.currentIdea.id,
    renderSlotById: (...args) => rendered.push(args), videoSlotState: v => v };
vm.createContext(context); vm.runInContext(fn, context);
context.renderVideoSlotDone(2, { slot: 2, url: '/two.mp4' }, owner);
assert.strictEqual(owner.frameRun.videos.length, 1);
assert.strictEqual(other.frameRun.videos.length, 0);
assert.strictEqual(rendered.length, 0);
context.currentIdea = owner;
context.renderVideoSlotDone(2, { slot: 2, url: '/updated.mp4' }, owner);
assert.strictEqual(owner.frameRun.videos.length, 1);
assert.strictEqual(owner.frameRun.videos[0].url, '/updated.mp4');
assert.strictEqual(rendered.length, 1);
console.log('Video delivery ownership tests passed');
