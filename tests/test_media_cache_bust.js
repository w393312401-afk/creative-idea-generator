// 媒体缓存版本号（js/media_renderer.js 里的 bustImageCache / cacheBustedUrl /
// safeSetImageSrc）的行为契约。
//
// 为什么需要它：手动上传、拖拽换位、单帧重试改的都是同名文件（img_NNN.webp、
// vid_NNN.mp4）——路径不变、内容变了。浏览器凭路径命中缓存，重渲出来的卡片会
// 原样显示旧图/旧片，界面看着像"拖了没反应"。版本号是唯一能让它回源的东西。
//
// 反过来也要保住：没被改动过的 URL 必须保持稳定，否则每次重渲都是全新 URL，
// 整格帧序列会被反复重新下载（这正是当初把 `?t=Date.now()` 换成按 URL 记版本的原因）。
//
// media_renderer.js 是浏览器端的 classic script（没有 module.exports），因此用 vm
// 装进一个上下文里取函数，而不是 require。
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ctx = { console };
vm.createContext(ctx);
vm.runInContext(
    fs.readFileSync(path.join(__dirname, '..', 'js', 'media_renderer.js'), 'utf8'), ctx);

const { bustImageCache, cacheBustedUrl, safeSetImageSrc } = ctx;

// 1. 没动过的文件：URL 原样返回，重渲能命中浏览器缓存
assert.strictEqual(cacheBustedUrl('/outputs/p/frames/img_001.webp'),
                   '/outputs/p/frames/img_001.webp');

// 2. 作废之后带上版本号
bustImageCache('/outputs/p/frames/img_003.webp');
const busted = cacheBustedUrl('/outputs/p/frames/img_003.webp');
assert.match(busted, /^\/outputs\/p\/frames\/img_003\.webp\?v=\d+$/);

// 3. manifest 里同一个文件有 url('/outputs/…') 与 file('outputs/…') 两种写法，
//    必须共用同一个版本号——否则拿 file 渲染的路径永远看不到那次作废
assert.match(cacheBustedUrl('outputs/p/frames/img_003.webp'), /\?v=\d+$/);

// 4. 版本号在没有新的作废时保持稳定（同一次渲染/多次渲染拿到同一个 URL）
assert.strictEqual(cacheBustedUrl('/outputs/p/frames/img_003.webp'), busted);
assert.strictEqual(cacheBustedUrl('/outputs/p/frames/img_003.webp'), busted);

// 5. 邻居不受牵连：只有被改过的那一格回源
assert.strictEqual(cacheBustedUrl('/outputs/p/frames/img_004.webp'),
                   '/outputs/p/frames/img_004.webp');

// 6. 视频与图片走同一套（拖拽换位后两个槽位的 mp4 都要回源）
bustImageCache('outputs/p/videos/vid_002.mp4');
assert.match(cacheBustedUrl('/outputs/p/videos/vid_002.mp4'), /\?v=\d+$/);

// 7. 已经带查询串的 URL 用 & 续接，不会拼出第二个 '?'
bustImageCache('/outputs/p/frames/img_005.webp');
const withQuery = cacheBustedUrl('/outputs/p/frames/img_005.webp?x=1');
assert.strictEqual((withQuery.match(/\?/g) || []).length, 1);
assert.match(withQuery, /\?x=1&v=\d+$/);

// 8. 远程与 data: URI 是不可变/不透明的，永远不加版本号
assert.strictEqual(cacheBustedUrl('https://cdn.example.com/a.png'),
                   'https://cdn.example.com/a.png');
assert.strictEqual(cacheBustedUrl('data:image/png;base64,AAAA'),
                   'data:image/png;base64,AAAA');

// 9. safeSetImageSrc：bust=true 就地作废并立刻带上新版本号
const img = { removeAttribute() { this.src = undefined; } };
safeSetImageSrc(img, '/outputs/p/frames/img_006.webp');
assert.strictEqual(img.src, '/outputs/p/frames/img_006.webp');
safeSetImageSrc(img, '/outputs/p/frames/img_006.webp', true);
assert.match(img.src, /^\/outputs\/p\/frames\/img_006\.webp\?v=\d+$/);

// 10. 不安全的 URL 仍然被挡在门外（缓存改动不能顺手放宽这条）
safeSetImageSrc(img, 'javascript:alert(1)');
assert.strictEqual(img.src, undefined);

console.log('media cache-bust tests passed');
