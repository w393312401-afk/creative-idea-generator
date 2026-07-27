/* ============================================================
   Lightbox 控制器(双前端共享)
   —— 通用控制器原在 media_renderer.js(主 app,全局函数)与 console.js(控制台,
   闭包内局部)各存一份逐字节几乎相同的拷贝;此处合并为单一来源的全局函数,
   index.html 与 console.html 均加载。
   "页面上哪些元素点击后开灯箱"的专属绑定仍留在各自文件里(主 app 在 media_renderer.js,
   控制台在 console.js 里绑定 play-preview-*),调用本文件的全局 openLightbox。
   依赖 DOM:#lightbox-modal / #lightbox-img / #lightbox-video / #lightbox-caption /
   #close-lightbox-btn / #prev-lightbox-btn / #next-lightbox-btn(两个 HTML 都具备)。
   ============================================================ */
let lightboxItems = [];
let lightboxActiveIndex = -1;

function initLightbox() {
    const modal = document.getElementById('lightbox-modal');
    const closeBtn = document.getElementById('close-lightbox-btn');
    const prevBtn = document.getElementById('prev-lightbox-btn');
    const nextBtn = document.getElementById('next-lightbox-btn');

    if (!modal) return;

    closeBtn?.addEventListener('click', closeLightbox);
    prevBtn?.addEventListener('click', () => navigateLightbox(-1));
    nextBtn?.addEventListener('click', () => navigateLightbox(1));

    // Close on clicking outside the content
    modal.addEventListener('click', (e) => {
        if (e.target === modal) {
            closeLightbox();
        }
    });
}

function openLightbox(items, index) {
    lightboxItems = items;
    lightboxActiveIndex = index;

    const modal = document.getElementById('lightbox-modal');
    if (!modal) return;

    modal.style.display = 'flex';
    updateLightboxContent();

    document.addEventListener('keydown', handleLightboxKeydown);
}

function closeLightbox() {
    const modal = document.getElementById('lightbox-modal');
    if (!modal) return;
    modal.style.display = 'none';

    const video = document.getElementById('lightbox-video');
    if (video) {
        video.pause();
        video.src = '';
    }

    document.removeEventListener('keydown', handleLightboxKeydown);
}

function updateLightboxContent() {
    const img = document.getElementById('lightbox-img');
    const video = document.getElementById('lightbox-video');
    const caption = document.getElementById('lightbox-caption');
    const prevBtn = document.getElementById('prev-lightbox-btn');
    const nextBtn = document.getElementById('next-lightbox-btn');

    if (!img || !video || !caption) return;

    if (lightboxActiveIndex < 0 || lightboxActiveIndex >= lightboxItems.length) {
        closeLightbox();
        return;
    }

    const item = lightboxItems[lightboxActiveIndex];

    if (item.type === 'video') {
        img.style.display = 'none';
        video.src = item.url;
        video.style.display = 'block';
        video.play().catch(err => console.log("Auto-play prevented", err));
    } else {
        video.style.display = 'none';
        video.pause();
        video.src = '';
        // 直接赋 src 会命中浏览器对这个 URL 的既有缓存，重试/修复原地覆盖同名
        // 帧文件后点开还是老图。safeSetImageSrc（media_renderer.js）按 URL 维护
        // 缓存版本号，重渲发生时该 URL 已被 bump 过，这里用 bust=false 直接
        // 取新版本号即可，不需要在这里再 bump 一次。
        if (typeof safeSetImageSrc === 'function') {
            safeSetImageSrc(img, item.url, false);
        } else {
            img.src = item.url;
        }
        img.style.display = 'block';
    }

    if (item.caption) {
        caption.innerHTML = item.caption;
        caption.style.display = 'block';
    } else {
        caption.style.display = 'none';
    }

    if (lightboxItems.length <= 1) {
        if (prevBtn) prevBtn.style.display = 'none';
        if (nextBtn) nextBtn.style.display = 'none';
    } else {
        if (prevBtn) prevBtn.style.display = 'flex';
        if (nextBtn) nextBtn.style.display = 'flex';
    }
}

function navigateLightbox(direction) {
    if (lightboxItems.length <= 1) return;

    lightboxActiveIndex += direction;
    if (lightboxActiveIndex < 0) {
        lightboxActiveIndex = lightboxItems.length - 1;
    } else if (lightboxActiveIndex >= lightboxItems.length) {
        lightboxActiveIndex = 0;
    }

    updateLightboxContent();
}

function handleLightboxKeydown(e) {
    if (e.key === 'ArrowLeft') {
        navigateLightbox(-1);
    } else if (e.key === 'ArrowRight') {
        navigateLightbox(1);
    } else if (e.key === 'Escape') {
        closeLightbox();
    }
}

// 单次初始化;兼容 defer(index.html,DOMContentLoaded 前执行)与 body 末内联(console.html)
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initLightbox);
} else {
    initLightbox();
}
