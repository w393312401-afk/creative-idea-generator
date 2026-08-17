const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

// 1. 验证 CSS 文件中已经去除了 :not(.active) 并且规则是 display: none !important
const cssContent = fs.readFileSync(path.join(__dirname, '..', 'css', 'app', 'nav-customize.css'), 'utf8');
assert.ok(!cssContent.includes('.mobile-nav-btn.nav-hidden:not(.active)'), 'CSS 不应再包含 :not(.active) 过滤规则');
assert.ok(cssContent.includes('.mobile-nav-btn.nav-hidden'), 'CSS 必须包含 .mobile-nav-btn.nav-hidden 规则');
assert.match(cssContent, /\.mobile-nav-btn\.nav-hidden\s*\{[\s\S]*?display:\s*none\s*!important;/, '隐藏标签必须强制 display: none !important');

// 2. 模拟 DOM 与测试 nav_customize.js
class FakeClassList {
    constructor(classes = []) {
        this.classes = new Set(classes);
    }
    add(cls) { this.classes.add(cls); }
    remove(cls) { this.classes.delete(cls); }
    contains(cls) { return this.classes.has(cls); }
    toggle(cls, force) {
        if (typeof force === 'boolean') {
            if (force) this.classes.add(cls);
            else this.classes.delete(cls);
            return force;
        }
        if (this.classes.has(cls)) {
            this.classes.delete(cls);
            return false;
        }
        this.classes.add(cls);
        return true;
    }
}

class FakeElement {
    constructor(id, classes = [], text = '') {
        this.id = id;
        this.classList = new FakeClassList(classes);
        this.textContent = text;
        this.children = [];
        this.parentElement = null;
        this.listeners = {};
    }
    querySelector(selector) {
        if (selector === '.btn-text') {
            return { textContent: this.textContent };
        }
        if (selector === '.btn-icon') {
            return { textContent: '★' };
        }
        if (selector === '.nav-customize-btn') {
            return this.children.find(c => c.classList.contains('nav-customize-btn')) || null;
        }
        return null;
    }
    querySelectorAll(selector) {
        if (selector === '.mobile-nav-btn') {
            return this.children.filter(c => c.classList.contains('mobile-nav-btn'));
        }
        return [];
    }
    appendChild(child) {
        const idx = this.children.indexOf(child);
        if (idx !== -1) {
            this.children.splice(idx, 1);
        }
        this.children.push(child);
        child.parentElement = this;
        return child;
    }
    addEventListener(type, fn) {
        if (!this.listeners[type]) this.listeners[type] = [];
        this.listeners[type].push(fn);
    }
    setAttribute() {}
}

function createEnv(initialHidden = [], initialActive = 'main-tab-config') {
    const bar = new FakeElement('mobile-nav-bar', ['mobile-nav-tabs']);
    const tabs = [
        'main-tab-config',
        'main-tab-results',
        'main-tab-projects',
        'main-tab-gallery',
        'main-tab-ledger',
        'main-tab-replica',
    ];

    const buttonMap = {};
    tabs.forEach(id => {
        const classes = ['mobile-nav-btn'];
        if (id === initialActive) classes.push('active');
        const btn = new FakeElement(id, classes, id.replace('main-tab-', ''));
        buttonMap[id] = btn;
        bar.appendChild(btn);
    });

    let currentActiveTab = initialActive.replace('main-tab-', '');
    const switchedTabs = [];

    const store = {};
    if (initialHidden && initialHidden.length > 0) {
        store['spark_nav_prefs'] = JSON.stringify({ order: tabs, hidden: initialHidden });
    }

    const sandbox = {
        console,
        document: {
            readyState: 'complete',
            querySelector(sel) {
                if (sel === '.mobile-nav-tabs') return bar;
                return null;
            },
            createElement(tag) {
                return new FakeElement('', []);
            },
            addEventListener() {},
            removeEventListener() {},
        },
        localStorage: {
            getItem(k) { return store[k] || null; },
            setItem(k, v) { store[k] = String(v); },
            removeItem(k) { delete store[k]; },
        },
        switchMainTab(tabKey) {
            switchedTabs.push(tabKey);
            currentActiveTab = tabKey;
            tabs.forEach(id => {
                const btn = buttonMap[id];
                if (id === `main-tab-${tabKey}`) {
                    btn.classList.add('active');
                } else {
                    btn.classList.remove('active');
                }
            });
        },
        showToast() {},
    };

    sandbox.window = sandbox;
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);

    const source = fs.readFileSync(path.join(__dirname, '..', 'js', 'nav_customize.js'), 'utf8');
    vm.runInContext(source, sandbox, { filename: 'nav_customize.js' });

    return { sandbox, bar, buttonMap, switchedTabs, getActiveTab: () => currentActiveTab };
}

// 测试用例 1：默认状态所有标签可见
{
    const env = createEnv([], 'main-tab-config');
    assert.equal(env.buttonMap['main-tab-config'].classList.contains('nav-hidden'), false);
    assert.equal(env.buttonMap['main-tab-replica'].classList.contains('nav-hidden'), false);
}

// 测试用例 2：持久化隐藏的标签在加载时自动标记 nav-hidden，若当前 active 处于隐藏标签，则自动平滑切至首个可见标签
{
    const env = createEnv(['main-tab-config', 'main-tab-replica'], 'main-tab-config');
    assert.equal(env.buttonMap['main-tab-config'].classList.contains('nav-hidden'), true);
    assert.equal(env.buttonMap['main-tab-replica'].classList.contains('nav-hidden'), true);
    assert.equal(env.buttonMap['main-tab-results'].classList.contains('nav-hidden'), false);
    // 因为 main-tab-config 被隐藏了，applyNavPrefs 应该自动切到 results
    assert.equal(env.getActiveTab(), 'results');
    assert.ok(env.switchedTabs.includes('results'));
    assert.equal(env.buttonMap['main-tab-results'].classList.contains('active'), true);
    assert.equal(env.buttonMap['main-tab-config'].classList.contains('active'), false);
}

// 测试用例 3：外部调用 switchMainTab('replica') 后，被隐藏标签即便获得 active 属性，nav-hidden 仍旧保持，配合 CSS 彻底隐藏
{
    const env = createEnv(['main-tab-replica'], 'main-tab-config');
    assert.equal(env.buttonMap['main-tab-replica'].classList.contains('nav-hidden'), true);
    env.sandbox.switchMainTab('replica');
    assert.equal(env.buttonMap['main-tab-replica'].classList.contains('active'), true);
    // nav-hidden 必须依然存在
    assert.equal(env.buttonMap['main-tab-replica'].classList.contains('nav-hidden'), true);
}

// 测试用例 4：重置偏好后恢复所有标签显示
{
    const env = createEnv(['main-tab-replica', 'main-tab-ledger'], 'main-tab-config');
    assert.equal(env.buttonMap['main-tab-replica'].classList.contains('nav-hidden'), true);
    env.sandbox.resetNavPrefs();
    assert.equal(env.buttonMap['main-tab-replica'].classList.contains('nav-hidden'), false);
    assert.equal(env.buttonMap['main-tab-ledger'].classList.contains('nav-hidden'), false);
}

console.log('test_nav_customize.js: all assertions passed successfully!');
