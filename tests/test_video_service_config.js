const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../js/config.js'), 'utf8');
const section = source.slice(source.indexOf('// /api/mode supplies only'), source.indexOf('/** auto 模式'));
const fields = {
    'settings-video-provider': { value: 'google_fx' },
    'settings-fx-video-model': { value: 'Veo 3.1 - Fast', options: [
        { value: 'Omni Flash' }, { value: 'Veo 3.1 - Fast' },
    ] },
    'settings-fx-video-duration': { value: '4' },
    'settings-fx-video-resolution': { value: '720p' },
    'settings-fx-video-ref-mode': { value: 'VIDEO_REFERENCES', options: [
        { value: 'VIDEO_FRAMES' }, { value: 'VIDEO_REFERENCES' },
    ] },
    'settings-image-ratio': { value: '9:16' },
    'video-provider-status': { textContent: '', dataset: {} },
};
let saved = null;
const config = {
    videoProvider: 'google_fx', videoModel: 'Veo 3.1 - Fast', videoDuration: '4',
    videoResolution: '720p', videoRefMode: 'VIDEO_REFERENCES', imageAspectRatio: '9:16',
    baseUrl: 'http://127.0.0.1:8046/v1', flow2apiApiKey: 'stale-private',
    flow2apiBaseUrl: 'http://stale.invalid',
};
const ctx = vm.createContext({
    config, DEFAULT_CONFIG: {},
    document: { getElementById: id => fields[id] || null },
    localStorage: { setItem(key, value) { saved = JSON.parse(value); } },
    updateFxVideoDurationVisibility() {}, syncIdeationSkillProfilePicker() {},
});
vm.runInContext(section, ctx);

ctx.applyServerVideoConfig({
    videoProvider: 'flow2api', videoModel: 'Omni Flash', videoDuration: '10',
    videoResolution: '360p', videoRefMode: 'VIDEO_FRAMES', flow2apiConfigured: true,
    flow2apiApiKey: 'must-not-copy', flow2apiBaseUrl: 'http://must-not-copy.invalid',
    baseUrl: 'http://must-not-copy.invalid', imageAspectRatio: '16:9',
});
assert.equal(config.videoProvider, 'flow2api');
assert.equal(config.videoDuration, '10');
assert.equal(config.videoResolution, '360p');
assert.equal(config.videoRefMode, 'VIDEO_FRAMES');
assert.equal(config.imageAspectRatio, '9:16', 'server video sync must preserve project/browser aspect');
assert.equal(config.baseUrl, 'http://127.0.0.1:8046/v1', 'text/image gateway stays separate');
assert.equal(fields['settings-fx-video-model'].value, 'Omni Flash');
assert.equal(saved.flow2apiApiKey, undefined);
assert.equal(saved.flow2apiBaseUrl, undefined);

ctx.updateVideoProviderStatus();
assert.match(fields['video-provider-status'].textContent, /服务端连接已配置/);
assert.equal(fields['settings-fx-video-model'].options[1].disabled, true);
assert.equal(fields['settings-fx-video-ref-mode'].options[1].disabled, true);
fields['settings-fx-video-ref-mode'].value = 'VIDEO_REFERENCES';
ctx.updateVideoProviderStatus();
assert.equal(fields['settings-fx-video-ref-mode'].value, 'VIDEO_REFERENCES');
assert.equal(fields['video-provider-status'].dataset.state, 'error');
assert.match(fields['video-provider-status'].textContent, /仅支持「帧/);
fields['settings-fx-video-ref-mode'].value = 'VIDEO_FRAMES';
fields['settings-image-ratio'].value = '1:1';
ctx.updateVideoProviderStatus();
assert.equal(fields['settings-image-ratio'].value, '1:1', 'unsupported aspect must not silently change');
assert.equal(fields['video-provider-status'].dataset.state, 'error');
assert.match(fields['video-provider-status'].textContent, /仅支持 16:9 或 9:16/);
fields['settings-image-ratio'].value = '9:16';
fields['settings-fx-video-model'].value = 'Veo 3.1 - Fast';
ctx.updateVideoProviderStatus();
assert.equal(fields['settings-fx-video-model'].value, 'Veo 3.1 - Fast', 'unsupported choice must not silently change');
assert.equal(fields['video-provider-status'].dataset.state, 'error');
assert.match(fields['video-provider-status'].textContent, /不受 Flow2API 支持/);
fields['settings-video-provider'].value = 'google_fx';
ctx.updateVideoProviderStatus();
assert.equal(fields['settings-fx-video-model'].options[1].disabled, false);
assert.equal(fields['settings-fx-video-ref-mode'].options[1].disabled, false);
assert.match(fields['video-provider-status'].textContent, /AdsPower/);

ctx.applyServerVideoConfig({ videoProvider: 'flow2api', videoModel: 'Omni Flash', flow2apiConfigured: false });
ctx.updateVideoProviderStatus();
assert.equal(fields['video-provider-status'].dataset.state, 'error');
assert.match(fields['video-provider-status'].textContent, /尚未配置/);
assert.match(source, /const _FX_SERVER_SYNC_KEYS = \['videoProvider'/);
console.log('video service config: public sync, credential boundary, provider availability passed');
