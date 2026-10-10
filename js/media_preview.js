// Public connections default to counts only. Keep URLs out of src/poster until
// previews are enabled or the user explicitly opens one in the lightbox.
(function () {
    'use strict';
    const KEY = 'spark_media_preview_mode';
    const SELECTOR = '[data-media-preview-src], [data-media-preview-poster]';
    const placeholders = new WeakMap();

    function isLocalHost(host) {
        const name = String(host || '').toLowerCase().replace(/^\[|\]$/g, '').replace(/\.$/, '');
        if (!name || name === 'localhost' || name.endsWith('.localhost') || name.endsWith('.local')
                || name === '::1' || /^(?:fc|fd)[0-9a-f]{2}:|^fe[89ab][0-9a-f]:/.test(name)) return true;
        const ip = name.split('.').map(Number);
        if (ip.length !== 4 || !/^\d+\.\d+\.\d+\.\d+$/.test(name) || ip.some(n => n > 255)) return false;
        return ip[0] === 127 || ip[0] === 10
            || (ip[0] === 192 && ip[1] === 168)
            || (ip[0] === 172 && ip[1] >= 16 && ip[1] <= 31)
            || (ip[0] === 169 && ip[1] === 254);
    }

    let counts = !isLocalHost(window.location.hostname);
    try {
        const saved = localStorage.getItem(KEY);
        if (saved === 'counts' || saved === 'previews') counts = saved === 'counts';
    } catch (_) { /* The automatic default also works without browser storage. */ }

    const escape = value => String(value || '').replace(/[&<>"']/g, char => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[char]));

    function attrs(url, attribute = 'src') {
        if (!url || !['src', 'poster'].includes(attribute)) return '';
        const encoded = escape(url);
        return `data-media-preview-${attribute}="${encoded}"` + (counts ? '' : ` ${attribute}="${encoded}"`);
    }

    function syncPlaceholder(element) {
        if (!element.parentNode) return;
        const url = element.getAttribute('data-media-preview-src');
        let button = placeholders.get(element);
        if (!url) {
            if (button) button.remove();
            placeholders.delete(element);
            return;
        }
        if (!button) {
            button = document.createElement('button');
            button.type = 'button';
            button.className = 'media-count-placeholder';
            button.addEventListener('click', event => {
                event.preventDefault();
                event.stopPropagation();
                const source = element.getAttribute('data-media-preview-src');
                if (source && typeof window.openLightbox === 'function') {
                    window.openLightbox([{ type: element.tagName === 'VIDEO' ? 'video' : 'image',
                        url: source, caption: '' }], 0);
                }
            });
            placeholders.set(element, button);
        }
        // innerHTML updates may detach the button while retaining a fixed player.
        if (button.previousSibling !== element) element.after(button);
        button.textContent = (element.tagName === 'VIDEO' ? '视频' : '图片') + ' ×1 · 预览';
        button.hidden = !counts;
    }

    function apply(element) {
        let releasedVideo = false;
        for (const attribute of ['src', 'poster']) {
            const source = element.getAttribute(`data-media-preview-${attribute}`);
            if (source === null) continue;
            if (counts || !source) {
                if (element.hasAttribute(attribute)) {
                    if (element.tagName === 'VIDEO' && attribute === 'src') {
                        element.pause();
                        releasedVideo = true;
                    }
                    element.removeAttribute(attribute);
                }
            } else if (element.getAttribute(attribute) !== source) {
                element.setAttribute(attribute, source);
            }
        }
        if (releasedVideo) element.load();
        syncPlaceholder(element);
    }

    function setSource(element, url, attribute = 'src') {
        if (!element || !['src', 'poster'].includes(attribute)) return;
        // Retaining an empty data attribute prevents a removed project's source
        // from returning when the user later enables previews.
        element.setAttribute(`data-media-preview-${attribute}`, url || '');
        apply(element);
    }

    function syncToggle() {
        document.documentElement.dataset.mediaPreview = counts ? 'counts' : 'previews';
        const toggle = document.getElementById('media-preview-toggle');
        if (toggle) {
            toggle.setAttribute('aria-pressed', String(counts));
            toggle.textContent = counts ? '仅计数' : '显示预览';
            toggle.title = counts ? '已停止自动加载图片和视频；点击显示全部预览' : '点击仅显示媒体计数，节省带宽';
        }
    }

    function setCountsOnly(enabled) {
        counts = !!enabled;
        try { localStorage.setItem(KEY, counts ? 'counts' : 'previews'); } catch (_) {}
        syncToggle();
        document.querySelectorAll(SELECTOR).forEach(apply);
        // A lightbox is an explicit request, but changing to counts only also
        // stops an already-open video from consuming bandwidth.
        if (counts && typeof window.closeLightbox === 'function') window.closeLightbox();
    }

    window.MediaPreview = { countsOnly: () => counts, attrs, setSource, setCountsOnly, isLocalHost };
    syncToggle();
    document.addEventListener('DOMContentLoaded', () => {
        document.getElementById('media-preview-toggle')?.addEventListener('click', () => setCountsOnly(!counts));
        document.querySelectorAll(SELECTOR).forEach(apply);
        // Only decorate new markup. Network suppression happens synchronously in
        // attrs/setSource; an observer alone would run too late to prevent fetches.
        new MutationObserver(records => {
            const pending = new Set();
            records.forEach(record => record.addedNodes.forEach(node => {
                if (node.nodeType !== 1) return;
                if (node.matches(SELECTOR)) pending.add(node);
                node.querySelectorAll(SELECTOR).forEach(element => pending.add(element));
            }));
            pending.forEach(element => { if (element.isConnected) apply(element); });
        }).observe(document.body, { childList: true, subtree: true });
    });
})();
