const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

const events = {};
const notifications = [];
const panel = { innerHTML: '' };
let closes = 0;
const context = vm.createContext({
    console,
    document: { getElementById: id => id === 'stepped-panel-content' ? panel : null },
    NotificationCenter: { notify: value => notifications.push(value) },
    EventSource: class {
        addEventListener(name, callback) { events[name] = callback; }
        close() { closes++; }
    },
});
vm.runInContext(fs.readFileSync('js/stepped_pipeline.js', 'utf8'), context);
vm.runInContext("steppedState = { stage: 'render_videos' }; startSteppedSSE('test');", context);
for (const phase of ['querying', 'waiting', 'retrying']) {
    events.video_recovery({ data: JSON.stringify({ phase, slots: [25, 26, 27],
        completed_slots: Array.from({ length: 24 }, (_, index) => index + 1),
        retry_round: 2, next_retry_at: 2000000000 }) });
    assert.equal(vm.runInContext('steppedState.stage', context), 'render_videos');
    assert.equal(vm.runInContext('steppedState.status', context), 'running');
    assert(panel.innerHTML.includes('24/27'));
    assert(panel.innerHTML.includes(phase === 'querying' ? '自动核对' : phase === 'waiting' ? '等待自动恢复' : '自动补跑'));
    assert(!panel.innerHTML.includes('流程已完成'));
    assert.equal(closes, 0, '恢复等待不能关闭原任务事件流');
    assert(!notifications.some(item => item.type === 'success'));
}
events.stepped_stage({ data: JSON.stringify({ pipeline_state: { stage: 'render_videos', title: 'test' } }) });
assert(panel.innerHTML.includes('24/27'), '中间阶段快照保留恢复进度');
events.result({ data: JSON.stringify({ status: 'running', pipeline_state: {
    stage: 'render_videos', title: 'test', video_recovery: { phase: 'waiting', slots: [25, 26, 27],
        completed_slots: Array.from({ length: 24 }, (_, index) => index + 1) }
} }) });
assert.equal(vm.runInContext('steppedState.stage', context), 'render_videos');
assert.equal(closes, 0, '仍在生成的视频快照不触发提前完成');
events.video_recovery({ data: JSON.stringify({ phase: 'waiting', slots: [25], completed_slots: [], message: '<script>wait</script>' }) });
assert(!panel.innerHTML.includes('<script>'), '恢复文本作为普通文字显示');
assert(panel.innerHTML.includes('&lt;script&gt;'));
events.result({ data: JSON.stringify({
    completion_state: 'partial_failed', has_failures: true,
    pipeline_state: { stage: 'completed', title: 'test' },
}) });
assert(panel.innerHTML.includes('视频需要处理'));
assert(!panel.innerHTML.includes('均已生成并审核完毕'));
assert.equal(notifications.at(-1).type, 'action_required');
assert.equal(vm.runInContext('steppedState.completion_state', context), 'partial_failed');
assert.equal(closes, 1, '最终完成事件才关闭事件流');
events.video_recovery({ data: JSON.stringify({ phase: 'waiting', slots: [25] }) });
assert.equal(vm.runInContext('steppedState.stage', context), 'completed', '重放恢复事件不回退任务终态');

vm.runInContext("lastNotifiedStage = ''; updateSteppedUI({ stage: 'completed' });", context);
assert(panel.innerHTML.includes('流程已完成'));
assert.equal(notifications.at(-1).type, 'success');
console.log('Stepped partial outcome tests passed');
