# 「激发结果」图片/视频槽位优化方案

目标：把结果页的两个槽位网格从"六套各写各的 innerHTML"改成
**一个状态模型 + 一个卡片渲染器 + 一套事务化操作**，让以后加一种徽标、加一个
按钮、改一次布局都只改一个地方。

红线（与 `docs/spark_dimension_minimal_layout_plan.md` 同）：
**不改任何槽位契约**——`VID N ≡ IMG N → IMG N+1`、视频数 ≡ 图片数 − 1、
槽位号是契约不是标签、删除必须整体前移。`#frames-grid` / `#videos-grid` /
`#frames-meta` / `#videos-meta` / `frame-slot-N` / `video-slot-N` 这些 id 全部保留，
`app.js`、`js/api_client.js` 里按 id 取元素的路径一行不用动。

对照现状：[js/media_renderer.js:523-1068](../js/media_renderer.js#L523-L1068)、
[js/api_client.js:273-344](../js/api_client.js#L273-L344)、
[css/app/skill-output.css:368-500](../css/app/skill-output.css#L368-L500)。

---

## 一、现在为什么难迭代

| 问题 | 具体证据 |
|---|---|
| **同一张卡片有 10 处独立的 innerHTML** | `media_renderer.js` 里 6 处（已出图 [671](../js/media_renderer.js#L671)、等待中 [761](../js/media_renderer.js#L761)、未生成 [527](../js/media_renderer.js#L527)、视频成功 [993](../js/media_renderer.js#L993)、视频失败 [958](../js/media_renderer.js#L958)、视频未生成 [799](../js/media_renderer.js#L799)），`api_client.js` 里另有 4 处（`renderVideoSlotPending/Done/Failed/SkippedCut`，[273-344](../js/api_client.js#L273-L344)）。改一个按钮要改 10 遍，漏一遍就是一处行为分叉。 |
| **两套渲染已经真的分叉了** | [`renderVideoSlotDone`](../js/api_client.js#L285) 画出来的卡片**没有重试/上传/删除按钮**、**没有 `IMG N ➔ IMG N+1` 标签**、**不认英雄展示**、**没有重设 `draggable`**——所以刚生成完的那一格，在下一次整格重渲之前既拖不动也没有操作出口，与 `renderVideosForIdea` 画的同状态卡片不是一回事。 |
| **没有"槽位状态"这个概念** | 状态是每次渲染现算的 7 个布尔（[626-645](../js/media_renderer.js#L626-L645)），且同一语义有多个别名：过期 = `stale_lineage` \|\| `quality_gate==='stale'` \|\| `stale`；英雄 = `is_hero` \|\| `meta` 含 `HERO`。没有一个"输入一条 frame 记录、输出它是什么状态"的函数，于是既不能单测，也不能在别处（拍轨、批量筛选、总览）复用。 |
| **徽标位置是硬编码的** | 第二枚徽标靠 `style="left: 45px;"` 手动挪位（[680](../js/media_renderer.js#L680)），第三枚就没地方放了。而可能同时成立的徽标有降级/审查未过/人工标记/未核验/未审查/留痕/过期/手动/换位共 9 种。 |
| **行内样式压过 CSS，已经开始靠 `!important` 打补丁** | `.frame-card-actions` 在 [skill-output.css:425](../css/app/skill-output.css#L425) 有完整样式，但 JS 每次都把 `background/border/padding/font-size/opacity` 写成行内（[681-685](../js/media_renderer.js#L681-L685)），CSS 那份等于死代码。移动端要让按钮常驻，只能整段 `!important` 压回来——[panels-tabs.css:1697-1712](../css/app/panels-tabs.css#L1697-L1712) 的注释原话："行内 style 写死了 opacity:0，只有 !important 压得住"。 |
| **每卡绑监听，已经因此出过两次实机事故** | 代码注释记录在案：[media_renderer.js:546-549](../js/media_renderer.js#L546-L549)（2026-07-22 连续生成帧序列，第 2 帧起点击无响应）、[973-974](../js/media_renderer.js#L973-L974)。根因都是"busy 时跳过 `addEventListener`，之后只摘 `disabled` 属性"，形成看着能点、点了没反应的死按钮。只要还是每卡绑监听，这个坑就永远开着。 |
| **六份几乎一样的操作流程** | `uploadVideoToSlot` / `swapVideoSlots` / `swapFrameSlots` / `uploadFrameToSlot` / `uploadFramesFromDrop` / `deleteSlotBeat` 各自手写同一套：抢闸门 → 画 spinner → 请求 → 乐观 patch → 重渲 → `reloadManifestIntoIdea` → 再重渲 → toast → `finally` 里放闸门 + 解 busy。任何一处忘了 `endSlotMutation()` 或忘了 `isViewingIdea` 判断，就是一次静默卡死或串页面渲染。 |
| **删除有快照、却没有回读的那一半** | 服务端在动文件之前已经完整落盘 `.deleted_slots/<ts>_slot_NNN/`：`manifest.before.json` + `prompt_block.before.txt` + `removed.json` + 媒体文件（[server.py:4450-4480](../server.py#L4450-L4480)）。但全仓唯一的 restore 端点是 `/api/trend-refs/archive/restore`——**槽位快照没有任何读回路径**。代价是现成的：`tools/recover_ice_cave_slot3.py` 与 `.recovery/ice_cave_slot3_before_restore_20260727_221105/`，为撤销一次误删手写的一次性脚本。 |
| **管理粒度只有"一格一格 hover"** | 没有多选、没有"只看有问题的"、没有"跳到第一个问题"、没有批量重试入口（`retryMissingVideos` 存在于 [api_client.js:2156](../js/api_client.js#L2156)，但只有合并被拦截时的横幅能触到它）。12 拍以上的单子只能靠肉眼扫两个网格。 |
| **网格尺寸与配对关系都读不出来** | `minmax(70px, 1fr)`（[skill-output.css:368](../css/app/skill-output.css#L368)）在 1600px 桌面上排出 ~20 列、9:16 缩略图仅 124px 高，看不清画面；而真正要判断的"IMG N 与 VID N 是否接得上"，需要在两个相隔一屏的 section 之间来回对号。 |

---

## 二、目标模型：先有状态，再有像素

新增 `js/slot_model.js`（**纯函数、无 DOM、无全局**，可直接单测）：

```js
// 一拍（beat）= 图片槽位 N ＋ 视频槽位 N，两者共用一个序号
buildBeats(idea) -> {
  beats: [{ seq, image: SlotState, video: SlotState | null }],
  counts: { images: 12, videos: 11, imagesReady: 12, videosReady: 9, flagged: 2 }
}

// SlotState —— 这一格"是什么"的唯一真相
{
  kind: 'ready' | 'pending' | 'missing' | 'failed' | 'cut',  // 决定画哪种壳
  seq, url, label,          // label: 'IMG 003' / 'VID 003 (IMG 003 ➔ IMG 004)' / 'VID 011 (英雄展示 · 完工全景)'
  badges: [{ id, text, tone, tip }],      // 表驱动，见下
  issues: [ '…' ],                        // 汇总进 hover title 与"只看有问题的"筛选
  actions: [ 'retry','fix','describe','upload','delete' ],  // 该状态允许的操作
  flags: { degraded, reviewFailed, manualFlagged, unverified, reviewSkipped,
           warned, stale, hero, manualUpload, swappedFrom }
}
```

徽标改成表驱动，别名收口在 `test` 里，新增一种徽标 = 在表里加一行：

```js
const FRAME_BADGES = [
  { id:'degraded',  text:'降级',   tone:'warn',
    test: f => f.quality_gate === 'i2i_fallback_degraded',
    tip:  f => '降级为文生图' },
  { id:'stale',     text:'Stale',  tone:'warn',
    // 三个别名是历史 manifest 的兼容写法，语义只有一个，收口在这里
    test: f => f.stale_lineage || f.quality_gate === 'stale' || f.stale,
    tip:  () => '此帧派生自已被替换的旧帧，建议重新生成' },
  …
];
```

`summarizeRunQuality`（[media_renderer.js:63](../js/media_renderer.js#L63)）现在自己数了一遍同样的东西，改成消费 `counts`，两处口径从此不会再对不上。

---

## 三、六处改动

### A. 单一卡片渲染器（先做，其余全部依赖它）

```js
renderSlotCard(cardEl, state)   // 唯一一处写卡片 innerHTML 的地方
```

* `media_renderer.js` 的 6 个分支与 `api_client.js` 的 4 个 `renderVideoSlot*` 全部改成
  调用它。`renderVideoSlotDone(idx, video)` 变成
  `renderSlotCard(el, videoSlotState(video, ctx))`——**丢按钮/丢标签/丢 draggable 的分叉自动消失**。
* 四个 `renderVideoSlot*` 的函数名与签名保留（生成过程中的实时回调点很多），只是内部转调。
* 卡片壳固定为：
  ```
  .slot-card[data-kind][data-seq][data-type]
    ├─ .slot-media      （img / video / spinner / 失败图标）
    ├─ .slot-badges     （flex 容器，徽标随便加几枚都自动排，不再有 left:45px）
    ├─ .slot-actions    （按 state.actions 生成，全部带 data-act）
    └─ .slot-label
  ```

### B. 事件委托，一次性关掉"死按钮"这类 bug

网格上各挂**一个** click 监听，按 `data-act` 分发：

```js
grid.addEventListener('click', (e) => {
  const btn = e.target.closest('[data-act]');
  const card = e.target.closest('.slot-card');
  if (!card) return;
  const seq = Number(card.dataset.seq);
  if (!btn) { openSlotLightbox(card.dataset.type, seq); return; }
  SLOT_ACTIONS[btn.dataset.act](seq);       // retry / fix / describe / upload / delete
});
```

于是 busy 只影响 `disabled` 属性（浏览器天然不派发 disabled 元素的 click），
**结构上不可能再出现"摘掉 disabled 却没补绑监听"**。`setFrameGridButtonsBusy` /
`setVideoGridButtonsBusy` 简化为在 grid 上切一个 `.is-busy` class + 批量写 `disabled`。

### C. 行内样式清零

`.slot-*` 全部进 `css/app/skill-output.css`，JS 只写 class 与 `data-*`。
连带收益：[panels-tabs.css:1697-1712](../css/app/panels-tabs.css#L1697-L1712) 那 7 行
`!important` 补丁可以退役，移动端"按钮常驻"改成一条正常的 `@media` 规则。

### D. 事务化的槽位操作

把六份重复流程抽成一个包装器：

```js
await mutateSlot({
  what: '上传视频',
  ideaId: ownerIdea.id,
  optimistic: () => renderSlotCard(card, pendingState(slot, '上传中...')),
  run: async () => postUploadVideo(slot, file),        // 只剩这一行是各自不同的
  patch: (data) => ({ video: data.video, dropMerged: true }),
  bust:  (data) => [data.video],
});
```

包装器统一负责：抢/放 `_slotMutationBusy` 闸门、busy 态、乐观 patch、
`bustImageCache`、`reloadManifestIntoIdea`、两次重渲的 `isViewingIdea` 判断、
成功/失败 toast、`finally` 兜底。六处调用点各自只保留自己的请求与 patch 形状。

### E. 删除可撤销（价值最高的单项）

快照已经是完整的，缺的只是回读的那一半。补三样：

| 位置 | 改动 |
|---|---|
| `server.py` | `GET /api/deleted_slots?title=` 列出该单的快照（时间、槽位号、缩略图）；`POST /api/restore_slot {title, snapshot_id}` 按 `manifest.before.json` + `prompt_block.before.txt` 整单回滚——因为删除会重编号，逐格回填不可靠，**整单回滚才是正确语义**（快照里存整份 manifest 就是为这个）。回滚前同样先写一份当前状态的快照。 |
| `js/api_client.js` | `restoreDeletedSlot(snapshotId)`，走 §D 的 `mutateSlot`。 |
| UI | 删除成功的 toast 带一枚「撤销」按钮（限 30s）；`#frames-settings-pop` 里加一行「已删除的拍（N）」，展开是快照列表 + 每条一个「恢复」。 |

做完这一项，`tools/recover_ice_cave_slot3.py` 这类一次性脚本就不用再写第二遍。

### F. 布局与管理粒度

网格头新增一条 `.slot-toolbar`（两个 section 共用同一组件）：

```
IMG 12/12 · VID 9/11 · ⚠2      [全部▾ 只看有问题 未生成]   [◧ 合并视图]  [尺寸 S M L]   ⟳ 重试所选(3)
```

* **筛选**：`全部 / 只看有问题（badges 非空）/ 只看未生成`，纯前端过滤 `beats`，
  不发请求。附「跳到第一个问题」。
* **多选**：卡片左上角 checkbox（按住 Shift 连选），选中后工具条右侧出现
  「重试所选 / 删除所选」。批量重试直接复用现成的 `retryMissingVideos(slots)`
  与 `retrySingleFrame`，串行执行、共用一次闸门。
* **尺寸档**：`minmax(70px)` 换成 CSS 变量 `--slot-min`，S/M/L = 88/120/168px，
  存 `localStorage`（键 `slotGridSize`，写法同 `sparkDrawerOpen`）。
* **合并视图（可选，第 3 期）**：一拍一列，IMG N 在上、VID N 在下，列头写 `#N`
  与该拍的徽标汇总——把"这一段接不接得上"从跨屏对号变成一眼可见。
  实现上是给 `#frames-section` / `#videos-section` 的公共父节点加一个
  `.beats-merged` class，两个 grid 的卡片按 `seq` 重排进同一个 CSS grid 的两行；
  **DOM 节点、id、渲染器全不变**，只换容器。默认仍是现在的双网格视图。

```
合并视图（桌面）
┌─────┬─────┬─────┬─────┐
│ #1  │ #2  │ #3⚠ │ #4  │  ← 列头：拍号 + 该拍徽标汇总
├─────┼─────┼─────┼─────┤
│IMG  │IMG  │IMG  │IMG  │
│001  │002  │003  │004  │
├─────┼─────┼─────┼─────┤
│VID  │VID  │VID  │ ⊘   │  ← VID N 恒接 IMG N → IMG N+1，空位一眼可见
│001  │002  │003  │未生成│
└─────┴─────┴─────┴─────┘
```

---

## 四、分期落地

| 期 | 内容 | 行为变化 | 验收 |
|---|---|---|---|
| **P0** ✅ 已完成 | §A 渲染器 + §B 事件委托 + §C 样式收口 + `js/slot_model.js` | 见下「P0 落地记录」 | `tests/test_slot_model.js`、`tests/test_slot_grid_render.py` 全绿；1020 项既有 pytest 与 5 项 JS 测试无回归 |
| **P1** ✅ 已完成 | §E 删除可撤销 | 新增撤销入口，见下「P1 落地记录」 | `tests/test_restore_slot.py`（19 项）与 `tests/test_restore_slot_e2e.py` 全绿 |
| **P2** ✅ 已完成 | §D 事务包装 | 零 | 六个调用点全部转调 `mutateSlot`，闸门只剩一处收发 |
| **P3** ✅ 已完成 | §F 工具条：筛选 + 多选 + 尺寸档 | 新增 | `tests/test_slot_toolbar.py`（19 项）全绿 |
| **P4** ✅ 已完成 | §F 合并视图 | 新增（默认关） | `tests/test_slot_merged_view.py`（13 项）全绿 |

P0 是其余各期的地基，可以单独合入验证。

### P0 落地记录（2026-07-27）

新增 [js/slot_model.js](../js/slot_model.js)（纯函数状态模型）、
[js/slot_card.js](../js/slot_card.js)（唯一渲染器 + 网格事件委托）、
[tests/test_slot_model.js](../tests/test_slot_model.js)、
[tests/test_slot_grid_render.py](../tests/test_slot_grid_render.py)。

**收口的写卡片位置：19 → 1。** `media_renderer.js` 6 处、`api_client.js` 4 处渲染
函数 + 6 处转圈占位、`app.js` 3 处，全部转调 `renderSlotCard`。
`markFrameCardMissing` / `markVideoCardMissing` / `bindDeleteSlotButton` 三个
只为拼模板存在的辅助函数随之删除；`renderVideoSlotPending/Done/Failed/SkippedCut`
四个函数名与签名保留（事件流按名调用），内部转调。

**行为变化**（都是修复既有缺陷，无预期外改动）：

1. `renderVideoSlotDone` 画出的卡片此前没有重试/上传/删除按钮、没有
   `IMG N ➔ IMG N+1` 标签、不认英雄展示、不设 `draggable`——一段视频刚生成完，
   在下次整格重渲前既拖不动也没有操作出口。现在与整格重渲完全一致。
2. 生成期间新建的视频占位卡（app.js 事件流 `start` 分支）漏调
   `enableVideoSlotDnd`，整单跑完前接不住拖拽上传/换位。已补。
3. 声明式硬切槽位（`status='skipped_cut'` / `skipped_bridge_hold'`）重渲清单时
   没有 url，会被当成 `status==='failed'` 画成带重试按钮的失败卡——与服务端
   合并门禁的判定（[server.py:1395](../server.py#L1395) 视其为预期缺失）相反。
   现在画中性卡、无重试出口，两端口径一致。
4. 徽标改 flex 容器后不再互相遮挡（此前第二枚靠硬编码 `left:45px`，第三枚起
   直接叠在一起）。窄卡上整枚换行，不逐字折行。
5. 「删除」按钮的 hover 说明统一为提到恢复快照的那一版——占位卡上此前那句
   "文件一并删除"没提快照，比实际行为更吓人。

**结构性收益**：卡片上不再绑任何 click 监听（全部走网格级委托），
`busy` 只能影响 `disabled` 属性，而浏览器本就不派发 `disabled` 元素的 click——
"摘掉 disabled 却没补绑监听器"造成的死按钮（2026-07-21 / 07-22 两次实机事故）
在结构上不可能再发生。测试里有一条专门守这个：置忙 → 点击不派发 → 解除忙 →
点击立即恢复。

用户文本（问题描述、失败原因、徽标提示）改为 `textContent` / `title` 属性赋值，
不再手工 `.replace(/"/g, '&quot;')` 拼进 HTML。

行内样式清零后，[panels-tabs.css:1690](../css/app/panels-tabs.css#L1690) 那段
移动端 `!important` 补丁已退役为普通规则。

### P1 落地记录（2026-07-27）

**服务端**：新增 `POST /api/restore_slot` 与 `GET /api/deleted_slots`
（[server.py](../server.py)）。恢复是整单回滚——当前编号整体后移一位、归档文件
放回原位、manifest 与 prompt_block 还原成删除前那一份。

顺带补上删除侧两处缺口：

1. **快照此前不完整。** 删最后一拍时，跨过删除点的那一段视频（老槽位
   `image_count-1`）前移后会超出「视频数＝图片数-1」而被一并物理删除，但它
   不在快照里——那种情况下的删除实际不可逆。现在归档覆盖**全部**会被删除的
   文件（帧、视频、`fx_src` 留档 jpg）。
2. **删除后写状态指纹**（`state.json`）。撤销时据此判断这单在删除之后有没有
   被继续改动过；对不上时返回 `409 status='diverged'`，由用户确认后带
   `force=true` 才覆盖。指纹只取「有哪些内容」（帧号/文件/质检结论、视频槽位/
   文件/状态、有无成片），不含耗时计数一类每次写 manifest 都会变的字段——
   否则撤销永远会被判成"已被改动过"（`manifest_fingerprint`，
   [server_common.py](../server_common.py)）。

其它护栏：`snapshot_id` 形状白名单（挡路径穿越）、同一份快照只能恢复一次、
换号搬移先整体预检再执行（目标被占用就整次拒绝，不留半成品状态）、恢复前把
当前 manifest 与提示词块也存一份「恢复点」。

**前端**：`showToast` 增加可选行动按钮，删除成功后挂 30 秒「撤销」；
`⚙ → ↩️ 已删除的拍` 里可以在窗口过后恢复，并标出哪些会覆盖后续改动
（`restoreSlotSnapshot` / `openDeletedSlotsPanel`，[js/api_client.js](../js/api_client.js)）。

**测试隔离（重要）**：`DB_FILE` / `LEDGER_FILE` 改为可被
`SPARK_DB_FILE` / `SPARK_LEDGER_FILE` 覆盖。端到端测试要驱动真实页面，而页面的
`saveLibrary()` 会把当时的 `savedIdeas` 整份 POST 回 `/api/library`——指向真库
就是一次整库覆盖。落地过程中真的发生过一次（已用 `.recovery` 快照与
`tools/recover_ice_cave_all.py` 的确定性重建完整捞回），因此把隔离做成了配置项
而不是只靠测试自觉。

---

## 五、不变量（改动全程不得触碰）

1. `VID N ≡ IMG N → IMG N+1`，视频数 ≡ 图片数 − 1；删除必须整体前移，不留空洞。
2. 槽位总数以 `resolvePromptSlots(idea)` 为准（后端 `prompt_slots` 优先、正则兜底），
   **不以"已生成几个"为准**——[media_renderer.js:883-886](../js/media_renderer.js#L883-L886) 的教训。
3. 用槽位号本身配对，绝不用数组下标（[media_renderer.js:596-599](../js/media_renderer.js#L596-L599) 记录的历史事故前提）。
4. 覆盖同名文件后必须 `bustImageCache`，否则浏览器拿缓存里的旧图/旧片。
5. 异步回调回来先判 `isViewingIdea(ownerIdea.id)` 再写 DOM。
6. 槽位改动全程单闸门 `_slotMutationBusy`，拖拽路径同样受闸。
7. 删除必须先写快照、快照失败即整次删除失败，不允许降级成不可恢复删除。

---

## 六、测试口子

`js/slot_model.js` 是纯函数，可以直接补 `tests/test_slot_model.js`（同
`tests/test_frame_start_preserves_completed.js` 的跑法）：

* 每种 `quality_gate` → 期望的 `kind` / `badges` / `actions`；
* 过期三别名、英雄两别名 → 同一个 flag；
* `prompt_slots` 有 12 条 / manifest 只有 9 条 → `beats` 仍是 12 拍、后 3 拍 `kind==='missing'`；
* 单帧重试任务（`targetSequences=[3]`）进行中 → 只有第 3 拍是 `pending`，其余不是
  （[media_renderer.js:750-758](../js/media_renderer.js#L750-L758) 记录的 2026-07-20 实机事故）。

服务端 `/api/restore_slot` 补进 `tests/test_delete_slot.py`：删除 → 恢复 → 与删除前逐字节比对。

### P2–P4 落地记录（2026-07-28）

**P2 · 事务外壳。** 新增 `mutateSlot`（[js/api_client.js](../js/api_client.js)）：
前置检查 → 抢闸门 → 乐观占位 → 请求 → 就地 patch → 重渲 → 拉权威清单 →
再重渲 → toast → `finally` 放闸门与忙态，全部收进一处。六个入口（上传帧 /
上传视频 / 帧换位 / 视频换位 / 删除整拍 / 恢复整拍）各自只剩自己的 `request`
与 `patch`。`beginSlotMutation` / `endSlotMutation` 现在只有这一个调用者——
「漏掉 endSlotMutation 导致静默卡死」和「漏掉 isViewingIdea 把 A 创意的结果
画进 B 创意」这两类错误没有地方再犯。两个 409-确认-重来的分支（上传视频锚点
不符、恢复时发现分歧）用 `SLOT_MUTATION_HANDLED` 交回外壳，只收尾不报错。
随之删掉已无调用点的 `bustSlotMedia`。

**P3 · 工具条。** 新增 [js/slot_toolbar.js](../js/slot_toolbar.js)：

* **计数与筛选**（全部 / 只看有问题 / 未生成）只读卡片上的
  `data-kind` / `data-badges`——那是 `renderSlotCard` 按 `slot_model` 的判定
  写下的，工具条再算一遍就会有第二套口径。筛选是纯视觉的（只加 class），
  所以「筛完再选、清掉筛选后选中仍在」是自然行为。
* **多选批量重试/删除**。批量删除**从后往前**执行：删除会把其后所有槽位整体
  前移一位，按升序删的话，删完第 3 拍之后原来的第 5 拍已经变成第 4 拍，接着
  去删「第 5 拍」打到的是另一拍。测试里专门守着这个顺序。
* **尺寸档 S/M/L**（88/120/168px，存 `localStorage`）。默认从 70px 提到 88px：
  70px 在 1600px 桌面上会排出约 20 列、缩略图只有 124px 高，看不清画面。
* **跳到第一个问题**，只在真有带徽标的槽位时出现。

**P4 · 合并视图（一拍一列）。** 第 N 列＝第 N 拍，上行 IMG N、下行 VID N，
哪一段接不上一眼可见。实现上不搬 DOM、不改渲染器输出：两个渲染器照旧各画各的
卡片，只是改往 `#beats-grid` 里 append 并按槽位号写 `grid-column` / `grid-row`
（CSS 读不到 `data-seq` 的数值，这是唯一必须由脚本写的定位）。因此卡片 id、
拖拽、事件委托、勾选全都原样成立——测试里有一条直接比对两种视图下的卡片描述，
要求一字不差。

配套的两处收口：容器一律经 `slotRenderTarget(type)` 取（原先散在 app.js /
api_client.js / media_renderer.js 的 8 处 `getElementById('…-grid')`），
整格清空一律经 `clearSlotGrid(grid, type)`（合并视图下两个渲染器共用一个容器，
`innerHTML = ''` 会把对方刚画好的那一行也抹掉）；事件委托与勾选绑定改为从
`card.dataset.type` 现取类型而不是闭包，并同时挂在三个容器上。

**与方案原稿的一处出入**：合并视图没有做「列头 `#N` + 该拍徽标汇总」那一行。
拍号已经写在每张卡片的 IMG/VID 标签里，再加一行列头需要第三个渲染入口去维护，
和它带来的信息量不相称。

**未做**：`summarizeRunQuality` 仍是自己数一遍 manifest，没有改成消费
`summarizeSlotStates`（§二 提到过）。两处口径目前一致，但它仍是第二套计数，
以后新增徽标时要记得两边都改。
