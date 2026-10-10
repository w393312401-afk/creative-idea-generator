// 配置中心「成片引导」：Codex 精剪完成后自动把透明引导动画烧进成片最后几秒。
// 设置存在服务端（cta_burn.py）而不是 localStorage 的 config——烧录发生在服务端的精剪 worker 里。
// 每个控件改动立即提交；头部「已保存」提示与其余分区共用 setSettingsSaveState。
(() => {
    'use strict';
    const API = '/api/engagement-cta';
    const BUILTIN_HINT = '关注、点赞、评论、收藏、分享依次点亮；按 TikTok 右侧操作栏定位，无文字，背景全透明。';
    const CUSTOM_HINT = '需要带透明通道：ProRes 4444（含 Alpha）MOV 或 VP9 透明 WebM，60 秒、300 MB 以内。从片尾时长的起点开始播放，超出成片结尾的部分不显示。';
    const $ = name => document.getElementById('settings-cta-' + name);
    let state = null, loading = null, revision = 0, busy = false, bound = false;

    const flag = (kind, text) => { if (typeof setSettingsSaveState === 'function') setSettingsSaveState(kind, text); };

    function setStatus(text, kind = '') {
        const node = $('status');
        if (!node) return;
        node.textContent = text;
        node.dataset.state = kind;
        node.hidden = !text;
    }

    async function call(path, options = {}) {
        const response = await fetch(API + path, options);
        const data = await response.json().catch(() => ({}));
        if (!response.ok || data.status !== 'ok') throw new Error(data.message || `请求失败（HTTP ${response.status}）`);
        return data;
    }

    const formatSize = bytes => bytes >= 1048576 ? `${(bytes / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`;

    function setDisabled(disabled) {
        ['enabled', 'seconds', 'source', 'upload-btn', 'remove-btn'].forEach(name => { if ($(name)) $(name).disabled = disabled; });
    }

    function renderPreview() {
        const frame = $('preview-frame');
        const custom = state.settings.source === 'custom' ? state.custom : null;
        const key = custom ? `video:${custom.preview_url || ''}` : `frame:${state.builtin_preview_url}`;
        $('preview').classList.toggle('is-off', !state.settings.enabled);
        $('preview-caption').textContent = (custom ? '自定义视频预览' : `内置动画预览 · ${state.settings.seconds} 秒`)
            + '。格子表示透明区域。' + (state.settings.enabled ? '' : '（自动烧录已关闭）');
        if (frame.dataset.key === key) return;
        frame.dataset.key = key;
        frame.replaceChildren();
        if (custom && custom.preview_url) {
            const video = document.createElement('video');
            // 静音属性要在设置 src 之前写上，否则浏览器按「有声自动播放」拦下，停在全透明的第一帧
            video.muted = video.defaultMuted = true;
            video.setAttribute('muted', '');
            Object.assign(video, { loop: true, autoplay: true, playsInline: true, src: custom.preview_url });
            video.setAttribute('aria-label', '自定义引导视频预览');
            frame.appendChild(video);
            video.play().catch(() => {});
        } else if (custom) {
            const note = document.createElement('p');
            note.className = 'cta-preview-empty';
            note.textContent = '这个格式无法在网页预览，烧录不受影响。';
            frame.appendChild(note);
        } else {
            const iframe = document.createElement('iframe');
            iframe.src = state.builtin_preview_url;
            iframe.title = '内置引导动画预览';
            iframe.tabIndex = -1;
            frame.appendChild(iframe);
        }
    }

    function render() {
        if (!state) return;
        const { settings, custom } = state;
        $('enabled').checked = settings.enabled;
        const seconds = $('seconds');
        const choices = state.seconds_choices || [settings.seconds];
        if (seconds.dataset.choices !== choices.join(',')) {
            seconds.replaceChildren(...choices.map(value => new Option(`最后 ${value} 秒`, String(value))));
            seconds.dataset.choices = choices.join(',');
        }
        seconds.value = String(settings.seconds);
        $('source').value = settings.source;
        $('source-hint').textContent = settings.source === 'custom' ? '烧录下方上传的自定义透明视频。' : BUILTIN_HINT;
        if (custom) {
            const parts = [custom.name, `${Number(custom.duration).toFixed(1)} 秒`, `${custom.width}×${custom.height}`, formatSize(custom.size_bytes)];
            let note = '';
            if (custom.duration > settings.seconds + 0.05) note = `；长于片尾时长，只显示前 ${settings.seconds} 秒`;
            else if (custom.duration < settings.seconds - 0.05) note = `；短于片尾时长，会在结尾前 ${(settings.seconds - custom.duration).toFixed(1)} 秒播完`;
            $('custom-info').textContent = `已上传：${parts.join(' · ')}${note}。`;
        } else {
            $('custom-info').textContent = CUSTOM_HINT;
        }
        $('upload-btn').textContent = custom ? '更换视频' : '上传透明视频';
        $('remove-btn').hidden = !custom;
        setDisabled(busy);
        renderPreview();
    }

    function load() {
        if (loading) return loading;
        if (!state) setStatus('正在读取成片引导设置…');
        loading = call('/settings')
            .then(data => {
                state = data;
                setStatus(data.custom_missing ? '之前上传的自定义视频已丢失，当前使用内置动画。' : '', data.custom_missing ? 'warn' : '');
                render();
            })
            .catch(error => setStatus(`无法读取成片引导设置：${error.message}`, 'error'))
            .finally(() => { loading = null; });
        return loading;
    }

    async function save(patch) {
        const mine = ++revision;
        flag('saving', '正在保存…');
        try {
            const data = await call('/settings', {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ patch }),
            });
            if (mine !== revision) return;
            state = data;
            setStatus('');
            render();
            flag('saved', '✓ 已保存');
        } catch (error) {
            if (mine !== revision) return;
            render(); // 回到服务端确认过的值
            setStatus(error.message, 'error');
            flag('error', '保存失败');
        }
    }

    async function upload(file) {
        const limits = state?.limits || {};
        const suffix = (file.name.match(/\.[^.]+$/) || [''])[0].toLowerCase();
        if (limits.suffixes && !limits.suffixes.includes(suffix)) {
            setStatus('请上传带透明通道的 .mov 或 .webm 视频。', 'error');
            return;
        }
        if (limits.max_bytes && file.size > limits.max_bytes) {
            setStatus(`视频超过 ${Math.round(limits.max_bytes / 1048576)} MB 上限。`, 'error');
            return;
        }
        busy = true;
        ++revision;
        setDisabled(true);
        setStatus(`正在上传并检查「${file.name}」的透明通道…`);
        flag('saving', '正在上传…');
        try {
            state = await call('/custom', {
                method: 'POST',
                headers: { 'Content-Type': 'application/octet-stream', 'X-Filename': encodeURIComponent(file.name) },
                body: file,
            });
            setStatus('已改用自定义透明视频。', 'ok');
            flag('saved', '✓ 已保存');
        } catch (error) {
            setStatus(error.message, 'error');
            flag('error', '上传失败');
        } finally {
            busy = false;
            $('file').value = '';
            render();
            setDisabled(false);
        }
    }

    async function removeCustom() {
        busy = true;
        ++revision;
        setDisabled(true);
        try {
            state = await call('/custom/delete', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
            setStatus('已移除自定义视频，改回内置动画。', 'ok');
            flag('saved', '✓ 已保存');
        } catch (error) {
            setStatus(error.message, 'error');
            flag('error', '保存失败');
        } finally {
            busy = false;
            render();
            setDisabled(false);
        }
    }

    function bind() {
        if (bound || !$('enabled')) return;
        bound = true;
        $('enabled').addEventListener('change', event => save({ enabled: event.target.checked }));
        $('seconds').addEventListener('change', event => save({ seconds: Number(event.target.value) }));
        $('source').addEventListener('change', event => {
            if (event.target.value === 'custom' && !state?.custom) {
                event.target.value = 'builtin'; // 上传成功后服务端会切过去
                $('file').click();
                return;
            }
            save({ source: event.target.value });
        });
        $('upload-btn').addEventListener('click', () => $('file').click());
        $('file').addEventListener('change', event => {
            const file = event.target.files && event.target.files[0];
            if (file) upload(file);
        });
        $('remove-btn').addEventListener('click', removeCustom);
    }

    window.EngagementCtaSettings = {
        load() {
            bind();
            return load();
        },
    };
})();
