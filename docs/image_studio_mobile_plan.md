# 图像工坊移动端适配方案（单模块全局优化）

> 目标：把 `#panel-image-studio` 从「桌面深色页面的等比缩小版」改造成一个真能用手指
> 操作的模块。范围**严格锁在这一个模块**：`css/image_studio.css` +
> `index.html` 里 `#panel-image-studio` 那一段 + `js/image_studio.js`，不动宿主的
> `base.css` / `panels-tabs.css` / `tokens.css`。
>
> 关联文档：`spark_result_minimal_layout_plan.md`（结果页移动端精简，本方案沿用它的
> 断点与「首屏不被说明文字吃掉」的原则）、`replica_ui_layout_plan.md`。

---

## 一、现状体检

### 1.0 根因：这是一个没落地的移植

`css/image_studio.css` 开头自己写着：

> Ported from the former standalone `/image-service-station/style.css`.

移植时只做了一件事——给每条选择器加 `#panel-image-studio` 前缀防止类名串味。**主题层和
触屏层都没有跟着迁移**：

- 原页面是纯深色的，所以里面直接写死了 `#020308`、`rgba(7, 9, 19, 0.9)`、`color: #fff`。
  而本应用的**默认主题是浅色**（`css/tokens.css:7` `--bg-dark: #F5F1E8` 暖奶油色），深色
  是 `html[data-theme="dark"]` 才开的。也就是说这些硬编码在**默认主题下就是错的**。
- 原页面是鼠标页面，所有次级操作都挂在 `:hover` 上。触屏没有 hover。

三个响应式断点 `1200 / 992 / 768`（`css/image_studio.css:1110,1116,1130`）和全站的
`1024 / 768 / 480`（`css/tokens.css:433,446,511`、`css/app/base.css:1524`）**完全错位**，
768–992 之间是死区：模块已经把双栏堆叠了，宿主还按桌面对待。

### 1.1 P0 · 触屏上点不到的功能

| # | 问题 | 位置 | 后果 |
|---|---|---|---|
| 1 | **spotlight 的 5 个操作全是 hover-only** | `image_studio.css:646` `opacity: 0` → `:651` `.spotlight-image-wrapper:hover` | 放大 / 下载 / 复制 / 填回提示词 / 送去图生图 —— 手机上**一个都点不到**。生成完的图只能看，不能用 |
| 2 | **历史卡片操作条同样 hover-only** | `:837` `opacity: 0` → `:840` `.history-card:hover` | 创作历史画廊在手机上是一堆纯装饰的方块 |
| 3 | **参考图删除按钮 16×16px** | `:545-547` `width/height: 16px` | 传错图删不掉。WCAG 2.5.8 最低 24px，HIG 建议 44px |
| 4 | **生成按钮不 sticky，埋在长表单最底** | `:457` `position: static` | 文生图表单是「模型 → 提示词 → 7 个风格标签 → 6 张比例卡 → 3 张清晰度卡」，手机上要划完整屏才能按到「开始生成」 |

第 4 条尤其值得说：宿主在 `panels-tabs.css:1833` 的 @768 里**已经**把 `.panel-footer`
做成了 `position: sticky; bottom: 0`，是本模块 `:452-457` 主动写 `position: static` 把它
关掉的。注释说是为了修「和上面的 tab 条重叠」——但那个修法是把整条规则无差别关死，
连手机上想要的粘底也一起关了。

### 1.2 P1 · 输入体验

| # | 问题 | 位置 | 后果 |
|---|---|---|---|
| 5 | **输入控件 14px，低于 iOS 的 16px 阈值** | `:151` `textarea{font-size:.875rem}`、`:199` `.styled-select` 同 | iOS Safari 聚焦 <16px 的输入框会**自动放大视口且不回弹**。本页 `viewport-fit=cover` 且没设 `maximum-scale`，点一次提示词框整页就歪了 |
| 6 | **触控目标普遍 <44px** | `.action-icon-btn` 36×36 (`:692`)、`.history-card-btn` 28×28 (`:847`)、`.task-btn-icon` 28×28 (`:1073`) | 误触率高 |
| 7 | **四层嵌套滚动容器** | grid `overflow-y:auto`(`:1119`) → `.panel-body`/`.gallery-body` `overflow-y:auto`(`:85,568`) → `.live-feed-container` `max-height:340px`(`:1169`) → `.feed-stage-lines` `max-height:110px`(`:1255`) | 手指落在实时动态区上划不动页面，只滚了里面那 340px 的小窗。这是手机上最典型的「滑不动」体感 |

### 1.3 P2 · 窄屏布局

| # | 问题 | 位置 |
|---|---|---|
| 8 | `.imgstudio-grid { height: 100% }`（`:16`）在 992 块里**没重置**，只改了列数和 overflow | `:1116-1120` |
| 9 | 清晰度卡在 768 下仍是 3 列（`:1134`），而卡内是「1K (Standard)」+「标准画质 (1024px)」两行文字，390px 屏上每列约 100px，必然撑爆 |
| 10 | 比例卡 2 列，但文案是「9:16 (手机壁纸/抖音)」，0.7rem 下折 2–3 行，六张卡高度参差 |
| 11 | `.panel-tab-btn` 保持 `padding:1rem; font-size:.85rem`（`:56-64`），「文生图 (Text-to-Image)」在半个卡片宽里必折行。宿主 @768 收窄的是 `.tab-btn`，**类名不同，管不着这里** |
| 12 | `.history-header-row` / `.tasks-header-row` 是 `space-between` 且**不换行**（`:783,884`），「实时生成动态 (0 渲染中 / 共 0 条)」+ 两个文字按钮在 358px 里顶出横向滚动 |
| 13 | `.history-grid-container` `minmax(130px,1fr)`（`:797`）在手机上只排 2 列 |

### 1.4 P3 · 主题与观感

| # | 问题 | 位置 |
|---|---|---|
| 14 | 硬编码深色：`.spotlight-image-wrapper{background:#020308}`(`:625`)、overlay 渐变 `rgba(7,9,19,…)`(`:640`)、`.history-card-overlay{rgba(7,9,19,.8)}`(`:831`)、`.info-prompt{color:#fff}`(`:681`)、`textarea:focus{background:rgba(0,0,0,.35)}`(`:162`) | 默认浅色主题下是一块突兀的黑，且 focus 时输入框由浅变黑 |
| 15 | 触屏 hover 粘滞：`.history-card:hover{transform:scale(1.03)}`(`:813`)、`.style-tag:hover{translateY(-1px)}`(`:245`) 等，点完卡片视觉上「卡住」不复原 |
| 16 | 文案在手机上无意义：「拖拽图片到此处」（`index.html:1163`）、生成按钮上的「Ctrl + Enter」（`index.html:1243`） |
| 17 | `.feed-entry-prompt` / `.task-prompt` 用 `white-space: nowrap` + 省略号（`:1242,1015`），手机上只能看到约 20 个字 |

---

## 二、方案总纲

三条原则：

1. **触屏能力用 `@media (hover: none), (pointer: coarse)` 判定，不用宽度判定。** hover 有没有、
   手指粗不粗，和视口多宽是两件事（触屏笔记本、iPad 外接键盘都会打脸宽度判定）。
2. **手机上只保留一个滚动容器**：`.imgstudio-grid`。卡片内部一律 `overflow: visible`、
   `max-height: none`。
3. **断点对齐全站**：`1024`（堆叠）/ `768`（手机）/ `480`（小屏），废掉 `992`。
   1200 那档保留但并到 1024。

所有新规则集中写在 `css/image_studio.css` 末尾一个带标题的段落里，**不要散插**，
并把现有的 `992 / 768` 两块删掉重写。

---

## 三、P0 · 触屏可达性（先做这一批，改完手机就能用了）

### 3.1 spotlight 操作条：hover 浮层 → 常驻工具条

浮层现在是绝对定位盖在图上的。触屏上改成图**下方**的静态一行，避免遮挡画面：

```css
@media (hover: none), (pointer: coarse) {
    /* 图 + 工具条竖排 */
    #panel-image-studio .spotlight-image-wrapper {
        flex-direction: column;
    }

    #panel-image-studio .spotlight-info-overlay {
        position: static;
        opacity: 1;
        transform: none;
        background: rgba(var(--panel-rgb), 0.92);
        border-top: 1px solid var(--border-color);
        flex-direction: column;
        align-items: stretch;
        gap: 10px;
        padding: 12px;
        width: 100%;
    }

    #panel-image-studio .info-prompt {
        color: var(--text-main);       /* 原 #fff：静态条背景是主题色了 */
        max-height: none;
        display: -webkit-box;
        -webkit-line-clamp: 2;
        -webkit-box-orient: vertical;
        overflow: hidden;
    }

    /* 5 个操作平分一行，每个都是完整触控目标 */
    #panel-image-studio .info-actions {
        display: grid;
        grid-template-columns: repeat(5, 1fr);
        gap: 8px;
    }

    #panel-image-studio .action-icon-btn {
        width: auto;
        height: 44px;
        background: rgba(var(--ink-rgb), 0.06);
        border-color: var(--border-color);
        color: var(--text-main);
    }
}
```

### 3.2 历史卡片操作条：常驻

```css
@media (hover: none), (pointer: coarse) {
    #panel-image-studio .history-card-overlay {
        opacity: 1;
        background: linear-gradient(to top, rgba(0, 0, 0, 0.66) 0%, transparent 55%);
        align-items: flex-end;
        justify-content: flex-end;
        padding: 6px;
        gap: 6px;
    }

    #panel-image-studio .history-card-btn {
        width: 36px;
        height: 36px;
        font-size: 0.9rem;
        background: rgba(0, 0, 0, 0.45);
    }
}
```

> 这里保留深色遮罩是对的：它压在缩略图**照片**上，不是压在主题背景上。

### 3.3 触控目标统一到 ≥40px

```css
@media (pointer: coarse) {
    #panel-image-studio .task-btn-icon   { width: 40px; height: 40px; }
    #panel-image-studio .upload-preview-card   { width: 76px; height: 76px; }
    #panel-image-studio .upload-preview-delete {
        width: 26px; height: 26px;
        top: 4px; right: 4px;
        font-size: 0.85rem;
    }
    #panel-image-studio .text-action-btn { min-height: 36px; padding: 6px 4px; }
    #panel-image-studio .style-tag       { padding: 0.55rem 0.85rem; font-size: 0.8rem; }
}
```

### 3.4 生成按钮粘底

前提：`.imgstudio-card` 的 `overflow: hidden`（`:28`）必须在窄屏放开。
**`position: sticky` 在 `overflow: hidden` 的祖先里不生效**——那个祖先会被当成不可滚动的
滚动容器，sticky 等于 static。这正是当初「粘不住只好改 static」的真实原因。

```css
@media (max-width: 1024px) {
    #panel-image-studio .imgstudio-card { overflow: visible; }
}

@media (max-width: 768px) {
    #panel-image-studio .panel-footer {
        position: sticky;          /* 撤掉 :457 的 static */
        bottom: 0;
        z-index: 5;
        padding: 10px 12px calc(10px + env(safe-area-inset-bottom, 0px)) 12px;
        box-shadow: 0 -4px 14px rgba(0, 0, 0, 0.12);
    }
    #panel-image-studio .gradient-submit-btn { min-height: 48px; padding: 0.85rem 1rem; }
    #panel-image-studio .btn-shortcut-hint   { display: none !important; }
}
```

`:452-457` 那条 `position: static` 的用意（挡住宿主 `.panel-footer` 泄漏）要保留，
但**收窄到桌面**：把它包进 `@media (min-width: 769px)`，并在注释里写清楚为什么手机要反过来。

---

## 四、P1 · 输入与滚动

### 4.1 干掉 iOS 聚焦缩放

```css
@media (max-width: 768px) {
    /* iOS Safari 对 <16px 的输入框聚焦即放大视口，且不回弹 */
    #panel-image-studio textarea,
    #panel-image-studio .styled-select {
        font-size: 16px;
    }
}
```

> 不要用 `maximum-scale=1` 去堵——那会一并禁掉用户主动缩放，是无障碍倒退。

### 4.2 收敛成单一滚动容器

注意宿主 `panels-tabs.css:1258` 的 `@768` 里写的是
`.panel-body, .content-scroll-area { overflow-y: auto !important }`，**带 `!important`**，
所以这里必须同样 `!important` 才压得住（`!important` 胜过特异性）：

```css
@media (max-width: 1024px) {
    /* 唯一滚动容器 */
    #panel-image-studio .imgstudio-grid {
        grid-template-columns: 1fr;
        gap: 16px;
        height: 100%;
        min-height: 0;
        overflow-y: auto;
        overscroll-behavior: contain;
        -webkit-overflow-scrolling: touch;
        padding-bottom: calc(8px + env(safe-area-inset-bottom, 0px));
    }

    #panel-image-studio .imgstudio-card {
        height: auto;
        max-height: none;
        overflow: visible;
    }

    /* 卡片内部一律不再自己滚 */
    #panel-image-studio .panel-body,
    #panel-image-studio .gallery-body {
        overflow: visible !important;
        flex: 0 0 auto;
    }

    #panel-image-studio .live-feed-container,
    #panel-image-studio .tasks-list-container,
    #panel-image-studio .feed-stage-lines {
        max-height: none;
        overflow: visible;
    }
}
```

---

## 五、P2 · 窄屏排布

需要**两处 HTML 微调**来支持文案降级（把括号说明包成可隐藏的 span）：

```html
<!-- index.html · 标签 -->
<button class="panel-tab-btn active" id="imgstudio-tab-t2i" onclick="switchImageStudioTab('t2i')">
    文生图<span class="tab-en"> (Text-to-Image)</span>
</button>

<!-- index.html · 比例卡，六张同理 -->
<span class="ratio-text">9:16<span class="ratio-hint"> (手机壁纸/抖音)</span></span>
```

```css
@media (max-width: 768px) {
    #panel-image-studio .panel-header   { padding: 14px 16px 10px !important; }
    #panel-image-studio .panel-header h2 { font-size: 1.05rem !important; }
    #panel-image-studio .panel-header p  { font-size: 0.75rem; }

    #panel-image-studio .panel-tab-btn  { padding: 0.75rem 0.5rem; font-size: 0.85rem; }
    #panel-image-studio .panel-tab-btn .tab-en { display: none; }

    /* 比例：三列小卡，靠图形辨识，不靠文字 */
    #panel-image-studio .aspect-ratio-grid { grid-template-columns: repeat(3, 1fr); gap: 0.4rem; }
    #panel-image-studio .ratio-card { padding: 0.5rem 0.25rem; gap: 0.35rem; }
    #panel-image-studio .ratio-text { font-size: 0.66rem; }
    #panel-image-studio .ratio-text .ratio-hint { display: none; }

    /* 清晰度：留三列，砍掉第二行说明 */
    #panel-image-studio .quality-grid  { grid-template-columns: repeat(3, 1fr); }
    #panel-image-studio .quality-title { font-size: 0.78rem; }
    #panel-image-studio .quality-desc  { display: none; }

    /* 标题行允许换行，杜绝横向溢出 */
    #panel-image-studio .history-header-row,
    #panel-image-studio .tasks-header-row {
        flex-wrap: wrap;
        gap: 8px;
    }
    #panel-image-studio .history-header-row h3,
    #panel-image-studio .tasks-header-row h3 { font-size: 0.95rem; }

    /* 历史网格：3 列缩略图比 2 列大图好扫 */
    #panel-image-studio .history-grid-container {
        grid-template-columns: repeat(auto-fill, minmax(96px, 1fr));
        gap: 0.5rem;
    }

    /* spotlight 别把首屏吃光 */
    #panel-image-studio .spotlight-container { min-height: 180px; }
    #panel-image-studio .spotlight-image-wrapper img { max-height: 46vh; }

    /* 提示词单行省略 → 两行 clamp */
    #panel-image-studio .feed-entry-prompt,
    #panel-image-studio .task-prompt {
        white-space: normal;
        display: -webkit-box;
        -webkit-line-clamp: 2;
        -webkit-box-orient: vertical;
        overflow: hidden;
    }
}

@media (max-width: 480px) {
    #panel-image-studio .aspect-ratio-grid { grid-template-columns: repeat(2, 1fr); }
    #panel-image-studio .info-actions      { grid-template-columns: repeat(3, 1fr); }
    #panel-image-studio .history-grid-container {
        grid-template-columns: repeat(auto-fill, minmax(84px, 1fr));
    }
}
```

---

## 六、P3 · 主题归位与文案

### 6.1 硬编码深色换成 token（这条不分屏幕大小，全局生效）

| 现状 | 改为 |
|---|---|
| `.spotlight-card { background: rgba(0,0,0,.3) }` `:580` | `background: rgba(var(--ink-rgb), 0.04)` |
| `.spotlight-image-wrapper { background: #020308 }` `:625` | `background: rgba(var(--ink-rgb), 0.06)` |
| `.spotlight-info-overlay` 渐变 `rgba(7,9,19,…)` `:640` | `linear-gradient(to top, rgba(var(--panel-rgb), .94), rgba(var(--panel-rgb), .6) 70%, transparent)` |
| `.info-prompt { color: #fff }` `:681` | `color: var(--text-main)` |
| `.info-model { color: #dfb2ff }` `:673` | `color: var(--primary)` |
| `textarea:focus { background: rgba(0,0,0,.35) }` `:162` | `background: var(--bg-card)`（即不变色，只描边发光） |
| `.action-icon-btn` 的 `rgba(255,255,255,.1)` `:690` | `rgba(var(--ink-rgb), 0.06)` + `color: var(--text-main)` |

`.history-card-overlay`（`:831`）**保持深色**——它压在照片上。

### 6.2 关掉触屏 hover 粘滞

```css
@media (hover: none) {
    #panel-image-studio .history-card:hover,
    #panel-image-studio .style-tag:hover,
    #panel-image-studio .task-card:hover,
    #panel-image-studio .gradient-submit-btn:hover,
    #panel-image-studio .action-icon-btn:hover,
    #panel-image-studio .feed-entry-result img:hover,
    #panel-image-studio .random-prompt-btn:hover {
        transform: none;
    }
    /* 用 :active 补回按下反馈 */
    #panel-image-studio .history-card:active,
    #panel-image-studio .style-tag:active { opacity: 0.75; }
}
```

### 6.3 文案降级（`index.html` + 一小段 JS）

- 上传区：触屏下把「拖拽图片到此处，或**点击上传**」换成「**点击选择图片**或拍照」，
  并隐藏 `.upload-hint` 里的格式清单（改成 title 提示）。纯 CSS 做法是两份文案各挂
  一个 span，用 `@media (hover:none)` 互换 `display`。
- 生成按钮的 `.btn-shortcut-hint`（`index.html:1243` 那段内联样式）在 @768 隐藏——
  顺手把内联 `style` 挪进 CSS，否则 `display:block` 内联优先级压不掉，必须 `!important`。

---

## 七、P4 · 移动视图模型（可选，收益最大但要动 JS）

前面 P0–P3 做完，手机上是「一根长列：表单卡 → 结果卡」。**仍然有一个结构性问题**：
生成完图之后，结果在页面下半段，用户得自己往下划才知道跑完了没有。

建议把手机上的两张卡改成**两个互斥子视图**，顶部一个分段控件切换：

```
┌─────────────────────┐
│  ✏️ 创作   │  🖼️ 结果 ③ │   ← 分段控件，右侧角标 = 渲染中数量
├─────────────────────┤
│                     │
│   （当前子视图）      │
│                     │
├─────────────────────┤
│   ⚡ 开始生成         │   ← 仅「创作」视图显示，粘底
└─────────────────────┘
```

实现要点（`js/image_studio.js`）：

1. 只在 `matchMedia('(max-width: 768px)')` 命中时启用，桌面完全不受影响；
2. 切换靠给 `.imgstudio-grid` 加 `data-mobile-view="create" | "result"`，CSS 用
   `[data-mobile-view="result"] #imgstudio-controls { display: none }` 控制，**不动 DOM 顺序**；
3. `imgStudioTriggerGeneration()` 成功入队后自动切到「结果」；
4. 角标复用已有的 `#tasks-active-count`；
5. 桌面 → 手机的 `matchMedia` change 事件里把属性清掉，避免旋转屏后卡片消失。

这一步和 P0–P3 解耦，可以单独排期。

---

## 八、落地顺序与验收

| 阶段 | 内容 | 改动面 | 验收 |
|---|---|---|---|
| **P0** | 三、触屏可达性 | 纯 CSS + `:452` 那条规则加断点 | iPhone 上生成一张图，5 个操作**全部可点**；历史卡片可点；参考图删得掉；表单任意位置都能按到生成 |
| **P1** | 四、输入与滚动 | 纯 CSS | 点提示词框**页面不放大**；手指落在实时动态区上能带动整页滚动 |
| **P2** | 五、窄屏排布 | CSS + 2 处 HTML span | 390px 下**无横向滚动**；比例/清晰度卡文字不折行；标签一行放下 |
| **P3** | 六、主题与文案 | CSS + HTML 文案 | 浅色主题下 spotlight 区域不再是黑块；点完卡片不留残影 |
| **P4** | 七、移动视图模型 | JS + CSS | 生成后自动跳到结果视图 |

**回归要看的三处**（这个模块和宿主的 CSS 有历史纠葛，容易改坏别处）：

1. `.panel-footer` —— 桌面的图像工坊页脚**不能**变粘性（`:452` 注释记录的原始 bug：
   宿主规则会让它盖住上面的 tab 条）；
2. `.task-card` —— `:910` 显式写 `flex-direction: row` 是在防宿主同名类泄漏，改动时别删；
3. `.panel-body` / `.control-group` / `.group-title` 都是和宿主重名的通用类，任何新规则
   **必须**带 `#panel-image-studio` 前缀。

**建议的验证矩阵**：iPhone SE（375×667，最窄）、iPhone 15 Pro（393×852，有刘海，验
safe-area）、iPad Air 竖屏（820×1180，验 1024 堆叠断点），各跑浅色 + 深色两遍。

---

## 九、落地记录

### 9.1 改动范围与文件明细

| 文件 | 变更内容 |
|---|---|
| `index.html` | 1. 在 `#panel-image-studio` 顶部新增移动子视图分段导航 `#imgstudio-mobile-nav`（`#imgstudio-mobtab-create` 与 `#imgstudio-mobtab-result`，含动态角标 `#imgstudio-mobile-active-badge`）；<br>2. 文生图/图生图 Tab 英文说明包裹 `<span class="tab-en">`；<br>3. 生图比例卡与参考图自适应提示包裹 `<span class="ratio-hint">`；<br>4. 上传区域文案拆分为 `.upload-text-desktop` 与 `.upload-text-mobile`；<br>5. 移除 `.btn-shortcut-hint` 内联样式。 |
| `css/image_studio.css` | 1. **P3 主题 Token 化**：清除硬编码深色值（`.spotlight-card`、`.spotlight-image-wrapper`、`.spotlight-info-overlay` 渐变、`info-model`、`info-prompt`、`textarea:focus` 背景、`action-icon-btn` 边框与背景），全局接入 CSS 变量与透明度通道；<br>2. **P0/P1 桌面隔离与移动粘底**：将 `.panel-footer` 的桌面防泄漏隔离规则限制在 `@media (min-width: 769px)`，在 `@media (max-width: 768px)` 开启 `position: sticky; bottom: 0` + `safe-area-inset-bottom` 支撑；<br>3. **P0/P3 触屏能力判定**（`@media (hover: none), (pointer: coarse)`）：Spotlight 悬浮条转为图片下方静态工具条、历史卡片遮罩常驻、触控目标规范 $\ge 40\text{px}$、上传文案触屏切换、消除 hover 粘滞；<br>4. **P1/P2 单一滚动容器与断点整合**（废除 `1200 / 992`，统一定位 `1024 / 768 / 480`）：解除卡片与动态列表内部嵌套滚动，统一交由 `.imgstudio-grid` 处理；iOS Safari 输入框 16px 阈值防缩放；比例网格与清晰度网格自动折叠说明文本；<br>5. **P4 移动子视图样式**：`.imgstudio-mobile-nav` 吸顶分段控件样式，基于 `[data-mobile-view="create" | "result"]` 互斥控制控制栏与画廊显示。 |
| `js/image_studio.js` | 1. 新增 `imgStudioMobileView` 状态与 `switchImageStudioMobileView(view)` 分段切换函数；<br>2. `initImageStudioMobileView()` 监听 `(max-width: 768px)` 媒体查询，桌面端自动移除 `data-mobile-view` 属性无缝恢复双栏；<br>3. `imgStudioTriggerGeneration()` 生成入队后自动调用 `switchImageStudioMobileView('result')` 引导查看渲染；<br>4. `imgStudioRenderTaskListUI()` 实时同步活跃任务数到移动端分段导航角标；<br>5. `imgStudioReusePrompt()` 与 `imgStudioSendToImageToImage()` 触发时自动切回 `create` 视图。 |

### 9.2 验证与回归检查

1. **桌面双栏回归（> 1024px）**：控制面板固定宽度与画廊分栏平铺，生成按钮非粘性（不遮挡上方 tab），鼠标 hover 渐变与放大特效完整。
2. **平板响应（769px–1024px）**：单列堆叠布局，`.imgstudio-grid` 统一滚动，卡片内部无多层冲突滚动条。
3. **手机端触控（$\le 768\text{px}$，如 390px / 375px）**：
   - 顶部提供「✏️ 创作参数」与「🖼️ 渲染结果」互斥切换，角标实时显示渲染中数量；
   - 输入框聚焦无 iOS Safari 视口异常放大；
   - 底部生成按钮吸附粘底且自动适配底部安全区域；
   - 触发生成后自动滑向渲染结果；
   - 聚光灯与历史卡片操作栏常驻显示，触控面积 $\ge 44\text{px}$ 且删除/放大/复用/图生图等均可一触即达；
   - 比例与清晰度小卡片文案不折行、不撑出横向滚动条。
4. **浅色/深色主题兼容**：在默认 Warm Light Paper 浅色主题及 Dark 主题下，聚光灯与工具栏均自然继承主题色阶，无突兀黑块。
