const fs = require('fs');
const path = require('path');
const assert = require('assert');

const rootDir = path.resolve(__dirname, '..');
const htmlContent = fs.readFileSync(path.join(rootDir, 'index.html'), 'utf8');
const cssContent = fs.readFileSync(path.join(rootDir, 'css/image_studio.css'), 'utf8');
const jsContent = fs.readFileSync(path.join(rootDir, 'js/image_studio.js'), 'utf8');

console.log('--- Checking HTML markup ---');
assert(htmlContent.includes('id="imgstudio-mobile-nav"'), 'HTML should contain #imgstudio-mobile-nav');
assert(htmlContent.includes('id="imgstudio-mobtab-create"'), 'HTML should contain #imgstudio-mobtab-create');
assert(htmlContent.includes('id="imgstudio-mobtab-result"'), 'HTML should contain #imgstudio-mobtab-result');
assert(htmlContent.includes('id="imgstudio-mobile-active-badge"'), 'HTML should contain #imgstudio-mobile-active-badge');
assert(htmlContent.includes('class="tab-en"'), 'HTML should contain class="tab-en"');
assert(htmlContent.includes('class="ratio-hint"'), 'HTML should contain class="ratio-hint"');
assert(htmlContent.includes('class="upload-text-desktop"'), 'HTML should contain class="upload-text-desktop"');
assert(htmlContent.includes('class="upload-text-mobile"'), 'HTML should contain class="upload-text-mobile"');
assert(!htmlContent.includes('style="display: block; font-size: 11px; font-weight: normal; opacity: 0.8; margin-top: 2px;"'), 'Inline styles on shortcut hint should be removed');
assert(htmlContent.includes('id="t2i-model"') && htmlContent.includes('<option value="gpt-image-2">'), 't2i-model should include gpt-image-2 option');
console.log('✔ HTML markup checks passed');

console.log('--- Checking CSS rules ---');
assert(cssContent.includes('@media (hover: none), (pointer: coarse)'), 'CSS should have hover: none, pointer: coarse');
assert(cssContent.includes('@media (max-width: 1024px)'), 'CSS should have max-width: 1024px');
assert(cssContent.includes('@media (max-width: 768px)'), 'CSS should have max-width: 768px');
assert(cssContent.includes('@media (max-width: 480px)'), 'CSS should have max-width: 480px');
assert(cssContent.includes('font-size: 16px;'), 'CSS should enforce 16px font size on inputs in mobile');
assert(cssContent.includes('position: sticky;'), 'CSS should enable position: sticky for mobile footer');
assert(cssContent.includes('safe-area-inset-bottom'), 'CSS should support safe-area-inset-bottom');
assert(cssContent.includes('data-mobile-view="result"'), 'CSS should control data-mobile-view display');
assert(!cssContent.includes('@media (max-width: 992px)'), 'CSS should not have obsolete 992px breakpoint');
assert(cssContent.includes('background: var(--bg-card);'), 'textarea:focus should use var(--bg-card)');
console.log('✔ CSS rule checks passed');

console.log('--- Checking JS logic ---');
assert(jsContent.includes('switchImageStudioMobileView'), 'JS should define switchImageStudioMobileView');
assert(jsContent.includes('initImageStudioMobileView'), 'JS should define initImageStudioMobileView');
assert(jsContent.includes('imgstudio-mobile-active-badge'), 'JS should update mobile badge in render tasks');
assert(jsContent.includes("switchImageStudioMobileView('result')"), 'JS should switch to result on generation trigger');
assert(jsContent.includes("initImageStudioMobileView();"), 'JS should init mobile view on DOMContentLoaded');
console.log('✔ JS logic checks passed');

console.log('All Image Studio Mobile Adaptation tests PASSED successfully!');
