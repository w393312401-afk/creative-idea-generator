# 项目工作台重构方案（任务列表 + 点子库合并）

> **落地状态（2026-07-31）：P0–P4 全部完成并上线。**
> 详见文末「九、落地记录」。

> 目标：把「激发任务列表」和「我的点子库」两个右侧抽屉，合并成一个以**创意项目**为
> 单位的主标签页；同时把点子库与任务的持久层从「整表覆盖写」改成「按条增量写」。
>
> 触发这次重构的三个真实痛点（用户口述）：
> 1. 两个抽屉太窄（380px）、互斥、还要和日志 dock 抢右侧位置，来回切换才能干活；
> 2. 同一个项目散在 **任务列表 / 点子库 / 创意台账 / 画廊** 四个地方，靠标题模糊匹配互相反查；
> 3. 存取慢、保存会失败（409）、怕丢数据。
>
> 关联文档：`spark_result_slots_plan.md`（结果槽位）、`spark_settings_center_layout_plan.md`
> （配置中心，本方案的布局语言沿用它）。

---

## 一、现状体检

### 1.1 四个界面，四份数据，零主键

| 界面 | 入口 | 数据源 | 写入方式 |
|---|---|---|---|
| 激发任务列表 | `#toggle-tasks-btn` 抽屉 | `GET /api/tasks`（内存 `ACTIVE_TASKS` + `tasks/<id>.json`） | 服务端写 |
| 我的点子库 | `#toggle-library-btn` 抽屉 | `GET /api/library`（`library.json`） | **整个数组 POST 覆盖** |
| 创意台账 | 主标签页 `#panel-ledger` | `GET /api/ledger`（`topic_ledger.json`） | **整表 POST 覆盖** + 按 id 删除 |
| 画廊 | 主标签页 `#panel-gallery` | 扫描 `outputs/` 目录 | 文件系统 |

四者描述的其实是**同一条创意的四个生命周期切面**：选题（台账）→ 激发任务（任务列表）
→ 结果收藏（点子库）→ 成片资产（画廊）。但它们之间没有主键，只有事后模糊反查：

- [app.js:3272](../app.js#L3272) `findSavedIdeaForSpark()` —— 按 `theme` 整串 / `title` 归一化匹配；
- [app.js:3289](../app.js#L3289) `findCompletedTaskForSpark()` —— 按目录名前缀 `run_<safe_id>_` 猜任务；
- [server_common.py:1241](../server_common.py#L1241) `gallery_collect_references()` —— 注释里明写
  「判定刻意从宽……宁可漏标孤儿」，因为它只能靠标题的各种命名变体去撞。

**根因**：`project_key` 其实已经是天然主键
（[server_common.py:1042](../server_common.py#L1042) `make_idea_project_key()` →
`run_<safe_task_id>__<title>`，也正是 `outputs/` 下的目录名），但它**只在结果阶段才被写出来**，
台账行里根本没有，所以四个界面谁也用不上它，只能退化成标题匹配。

### 1.2 点子库：2 条创意 = 208KB，每次改动整份重写

`library.json` 当前只有 **2 条**记录却 208KB，单条最大 **164KB**——因为一条创意条目里塞了
`prompt_block` / `prompt_slots` / `audit_md` / `repair_md` / `frameRun`（整份帧视频清单）
/ `covers` / `timings` 全部正文。

而 [app.js:541](../app.js#L541) `saveLibrary()` 的契约是「客户端始终持有完整数组、整份 POST
回来覆盖」（[server_common.py:1565](../server_common.py#L1565) 注释确认）。由此派生出的问题链：

- 每次收藏/删除/改一个字段 = 上传全库 + 全量落盘；
- 同时还 `localStorage.setItem('spark_library', JSON.stringify(savedIdeas))` 全量镜像一份
  ——`js/image_studio.js:365` 已经因为同类做法撞过 localStorage 配额；
- 「整表覆盖」天然会被状态错乱的客户端清空，于是服务端补了**三道事故防线**：
  空库拒写、缩量闸门 409（[server_common.py:1608](../server_common.py#L1608)
  `library_shrink_verdict`）、`.bak` 轮换；
- 前端因此必须在删除时**声明删除意图**（`intent.removed_ids` / `frame_shrink_ids`），
  否则 409 被拒——用户看到的就是「保存失败，请刷新页面后重试」。

这三道防线全都是**正确的补丁**，但它们防的是「整表覆盖」这个动作本身。换成按条读写，
这类事故在结构上就不存在了。

### 1.3 任务持久层：一次事件 = 整个 tasks/ 目录重写

[server_common.py:2142](../server_common.py#L2142) `save_tasks_to_disk()` 遍历
`ACTIVE_TASKS` **把每一个任务都整份重写一遍**，然后再扫一遍目录删孤儿文件。
而实测单个任务文件 **523KB**：`events` 375KB + `result` 132KB。

它在 `server.py` 里被调用了 **13 处**（任务创建、每个阶段落点、完成、失败、取消……）。
也就是说：跑 3 个任务时，一次状态变更 ≈ 1.5MB 的同步磁盘写。

同样地，「整目录重写 + 孤儿清理」这个危险动作已经引发过两次真实事故（注释里记了：
一次 `tasks/` 被整目录清空、一次测试建 1 条内存任务把 5 个真实任务删光），于是又补了
两段防呆（`active_ids` 空则跳过、`TASKS_LOADED_FROM_DISK` 未置位则跳过）。**同一个病根。**

另外 `task.result` 与点子库条目内容高度重复（132KB vs 164KB）——同一份提示词正文在磁盘上
至少存了两遍。

### 1.4 交互层

- 两个抽屉都是 `position: fixed; width: 380px`（[base.css:1175](../css/app/base.css#L1175)），
  互斥打开，且 `openLibraryDrawer()` 会顺手 `collapseLogDock()`——三者抢同一块地。
- [app.js:1411](../app.js#L1411) `renderTasks()` 每 2.5s 轮询 + 拼整块 HTML 字符串，
  靠 `html === _lastTasksRenderHtml` 字符串比对来跳过重绘；一旦有任何变化就整块
  `innerHTML` 重置（scroll 位置手工保存恢复，hover/焦点/展开态全丢）。
- `MEDIA_TASK_TYPES`（frames / staged_render / videos / cover）被**整类过滤掉**，
  失败的媒体任务在任务列表里完全不可见。
- 点子库只有「最新 / 最早」一个排序（主题筛选器已作为死代码删除），没有状态、标签、
  备注、批量操作——而隔壁创意台账早就有 chips 筛选 + 排序 + 全选 + 批量改状态 + 批量删除。
- 点子库条目 schema 里没有 `status` / `tags` / `note` / `updated_at`，想加也无处可加。

---

## 二、目标形态

### 2.1 一个实体：创意项目（Project）

```
Project  ← 主键 project_key
├─ seed      选题        （台账 topic_ledger.json 的行）
├─ run       激发任务    （tasks/<id>.json）
├─ result    激发结果    （tasks/results/<id>.json）
├─ saved     收藏记录    （library/items/<id>.json）
└─ assets    成片资产    （outputs/<project_key>/）
```

四个界面不再是四个并列的列表，而是**同一张项目表的四种视图**：

- **项目工作台**（新）= 项目表全景，取代任务列表抽屉 + 点子库抽屉；
- **创意台账** = 项目表的「选题与投放」列视图（保留，因为它管的是发布后的表现回填）；
- **画廊** = 项目表的「资产」列视图（保留，因为它管的是磁盘清理）；
- **激发结果** = 单个项目的详情页（保留）。

### 2.2 新主标签页「📁 项目」

与 `激发维度 / 激发结果 / 画廊 / 创意台账` 平级，插在「激发结果」之后：

```
⚙️ 激发维度   ☄️ 激发结果   📁 项目   🖼️ 画廊   📒 创意台账
```

header 上的两个图标按钮（`#toggle-tasks-btn` / `#toggle-library-btn`）**删除**，
换成一个「⚡ N 进行中」角标按钮：点击 = 跳到项目页并预选「运行中」筛选。
两个 `.drawer` 元素随之删除，右侧从此只剩日志 dock 一个停靠物。

### 2.3 布局：列表 + 详情双栏

```
┌─ 项目工作台 ────────────────────────────────────────────────────┐
│ [全部][运行中][已完成][已收藏][失败/取消]  🔍搜索  [排序▾] [🔄]   │  ← 复用 .ledger-toolbar
│ ☑️全选  已选 3 项       [批量收藏][批量删除]                      │  ← 复用 .ledger-bulk-bar
├──────────────────────────┬─────────────────────────────────────┤
│ ▸ [封面] 潜水艇微宅       │   ← 详情 pane                        │
│    候选·运行中 ███░░ 62%  │   标题 / 选题串 / 状态               │
│    frames ✓  videos ⚠     │   镜头数·耗时·模型·Tokens            │
│ ▸ [封面] 石砌烤烟房改造    │   [查看结果][再跑一遍][收藏][删除]   │
│    已收藏 · 12 镜 · 7-29  │   ─────────────────────────          │
│ ▸ [封面] 灯塔改造          │   🖼️ 去画廊看资产 (14 个文件)        │
│    已完成 · 未收藏         │   📒 去台账（已发布 · 打分 8）       │
└──────────────────────────┴─────────────────────────────────────┘
```

- 宽屏：左列表 + 右详情（详情 pane 常驻，取代覆盖式抽屉——**这直接解掉痛点 1**）；
- 窄屏：列表 → 详情两级页面（沿用 `.mobile-nav-tabs` 已有的单面板互斥策略）；
- 每行右上角挂**跨界面直达**：→ 画廊资产、→ 台账行、→ 激发结果——**解掉痛点 2**；
- 媒体子作业（frames / videos / cover / staged_render）不再被过滤丢弃，
  收敛成项目行下的一排小徽章，失败可见、可就地重试。

样式**不新造设计语言**：复用 `.ledger-toolbar` / `.ledger-filter-chip` /
`.ledger-bulk-bar` / `.gallery-*` 已有的类，只新增 `.project-row` 与 `.project-detail`。

---

## 三、数据层重构

### 3.1 点子库：索引 / 正文分离

```
library/
├─ index.json              ← 轻量索引数组，全量加载 < 10KB
└─ items/<id>.json         ← 单条正文（prompt_block / prompt_slots / audit_md
                              / repair_md / frameRun / covers / timings）
library.json.pre-split     ← 迁移前备份（仓库已有 library.json.pre-vid3-fix 的先例）
```

`index.json` 每条只留：

```jsonc
{
  "id": "1785381906330",
  "project_key": "run_1785381906330__废弃水泵房改造…",
  "task_id": "1785381906330",
  "title": "…", "theme": "…", "english_title": "…",
  "cover": "outputs/…/cover_01.png",
  "image_count": 12, "video_count": 12,
  "timestamp": "2026-07-28 10:41:06",
  "updated_at": 1785381906.3,
  "status": "saved",        // 新增：saved | archived
  "tags": [], "note": ""    // 新增：现在 schema 里无处可放的两个字段
}
```

新 API（老的 `GET/POST /api/library` 保留为兼容层）：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/library/index` | 只回索引数组 |
| GET | `/api/library/item/<id>` | 单条正文 |
| PUT | `/api/library/item/<id>` | 新建/整条覆盖（幂等，body 是单条） |
| PATCH | `/api/library/item/<id>` | 改 status/tags/note 等元字段 |
| DELETE | `/api/library/item/<id>` | 删除（含 `/api/library/delete_item` 的产物清理） |

**收益**：整表覆盖写从日常路径上消失 → 空库覆盖、缩量闸门 409、「保存失败请刷新」、
localStorage 配额四类问题一起消失（**解掉痛点 3**）。`library_shrink_verdict` 与
`.bak` 轮换在兼容层期间保留，作用范围缩到迁移路径。

localStorage 镜像同步瘦身：只镜像 `index.json`（几 KB），正文不再进浏览器存储。

### 3.2 任务持久层：单条写 + events 外置

```
tasks/
├─ <id>.json               ← 只留 meta：id/status/dimensions/result_summary/error/last_active
├─ events/<id>.jsonl       ← 事件追加写（append-only，不再整份重写）
└─ results/<id>.json       ← 结果全文（只在完成时写一次）
```

- 新增 `save_task_to_disk(tid)`：**只写一个文件**。`server.py` 里 13 处
  `save_tasks_to_disk()` 全部改为它。
- `save_tasks_to_disk()` 降级为「退出前 flush / 迁移」专用，孤儿清理从中剥离成
  显式的 `prune_orphan_task_files()`，只在启动加载完成后调用一次。
- `record_task_stage()` / 事件落点改成向 `.jsonl` 追加一行。
- 完成时把 `result` 写 `results/<id>.json`，`tasks/<id>.json` 里只留
  `/api/tasks` 列表已经在用的那份 `result_summary`
  （[server.py:2170](../server.py#L2170) 已经实现了瘦身逻辑，直接复用它的字段集）。

**收益**：一次状态变更的磁盘写从 ~1.5MB 降到 ~2KB；「整目录重写」这个危险动作消失，
两段防呆补丁不再是唯一防线（保留但不再吃重）。

### 3.3 project_key 贯通

1. **任务创建即定 key**：`get_or_create_task()` / `prepare_task_for_run()` 在
   `dimensions` 里写入 `project_key`（现在要等结果阶段），标题未定时先用 `task_label`。
2. **台账行加 `project_key`**：激发启动时回填到对应 ledger row
   （`ledger_activation_registration` 已有这条通路，见 `tests/test_ledger_activation_registration.py`）。
3. **画廊精确匹配**：`gallery_collect_references()` 优先用 `project_key` 精确匹配，
   标题变体匹配降为历史数据的回落分支，「宁可漏标孤儿」的宽判定得以收紧。
4. **删除三处模糊反查**：`findSavedIdeaForSpark` / `findCompletedTaskForSpark` /
   `sparkNormKey` 一系（[app.js:3257-3320](../app.js#L3257)）替换为按 `project_key` 直查。

### 3.4 合流接口 `GET /api/projects`

工作台一次请求吃满，避免前端并发拉三个源再自己 join：

```jsonc
{
  "projects": [{
    "project_key": "run_1785381906330__废弃水泵房改造…",
    "title": "…", "theme": "…", "cover": "outputs/…",
    "state": "running",              // running|completed|failed|cancelled|saved|archived
    "saved": true,                   // 是否已收藏
    "task": { "id": "…", "status": "running", "progress": 62,
              "stage": "渲染 VIDEO 链", "last_active": 1785… },
    "sub_jobs": [{ "type": "frames", "status": "completed" },
                 { "type": "videos", "status": "failed", "error": "…" }],
    "ledger": { "id": "b4bda…", "status": "published", "user_score": 8 },
    "assets": { "file_count": 14, "bytes": 20480000 },
    "image_count": 12, "video_count": 12,
    "timestamp": "2026-07-28 10:41:06"
  }],
  "total_count": 37
}
```

支持 `?state=&q=&sort=&limit=&offset=` 服务端筛选分页——点子库现在是**全量渲染无分页**，
项目多了必然卡。

---

## 四、前端实现要点

新文件 `js/projects.js`（参照 `js/ledger.js` 的模块组织：模块内状态 + 依赖宿主的
`escapeHtml` / `showToast`）。

1. **keyed 增量渲染**：以 `project_key` 为 key 做行级 diff，只更新变化的行，
   取代 `renderTasks()` 现在的「拼整串 HTML → 字符串比对 → 整块 innerHTML」。
   hover / 焦点 / 展开态 / 滚动位置自然保住。
2. **进度走 SSE，不轮询**：运行中的项目订阅已有的
   `/api/compose-stream?task_id=…`（[server.py:2221](../server.py#L2221)），
   进度条由 `ProgressModel` 驱动（已有，`renderTasks` 已在用）。
   列表整体只留 30s 兜底刷新，删掉 2.5s 全量轮询和 5s/30s 双轨角标轮询。
3. **筛选/排序/批量**：直接抄 `js/ledger.js` 的 `ledgerSelected: Set` + chips + 批量条实现。
4. **详情 pane**：复用 `renderIdea()` 的现有渲染，点开时才 `GET /api/library/item/<id>`
   或 `GET /api/tasks/<id>/result` 拉正文（**懒加载**，列表阶段不碰 164KB 正文）。

---

## 五、分阶段实施

每一阶段都能独立上线、独立回滚。

| 阶段 | 内容 | 风险 | 可回滚性 |
|---|---|---|---|
| **P0** ✅ | 只读合流接口 `GET /api/projects`（读现有三个源，不改任何存储）+ 新建「项目」主标签页与 `js/projects.js`；两个抽屉**保留**但 header 上已无入口 | 低 | 把 header 的两个按钮加回来即可 |
| **P1** ✅ | 点子库拆索引/正文 + 单条读写 API + `tools/migrate_library.py`；`GET/POST /api/library` 转为兼容层（读重组、写转发到逐条写） | 中 | 保留 `library.json.pre-split` |
| **P2** ✅ | 任务持久层 `save_task_to_disk()` + `events/*.jsonl` + `results/*.json`；启动时自动迁移旧格式 | 中 | 旧 `tasks/<id>.json` 格式仍可读 |
| **P3** ✅ | `project_key` 贯通（任务创建即定 / 台账回填 / 画廊精确匹配），删除三处标题模糊反查 | 中 | 保留标题匹配为回落分支 |
| **P4** ✅ | 删除 `#library-drawer` / `#tasks-drawer` 及其 CSS、`renderTasks` / `renderLibrary`、整表 `POST /api/library` 兼容层 | 低 | —— |

**建议先做 P0 + P1**：P0 直接解掉痛点 1 和 2（用户当天就有感），P1 解掉痛点 3 里最疼的
「保存失败 / 怕丢数据」。P2 是性能与事故面收敛，可以稍后。

---

## 六、测试

现有测试锁死了当前语义，改动时必须同步：

- `tests/test_library_shrink_guard.py` —— 锁的是整表覆盖的缩量闸门。P1 后这条路径
  只剩兼容层，测试保留但标注为「兼容层契约」，另加单条写路径的新测试。
- `tests/test_task_history_not_wiped.py` —— 锁的是 `save_tasks_to_disk()` 的两段防呆。
  P2 后 `save_task_to_disk()` 不再有整目录删除动作，需要新增
  「单条写不影响其他任务文件」的用例，旧用例转测 `prune_orphan_task_files()`。
- `tests/test_gallery_endpoints.py` —— 已覆盖「项目目录 → 点子库条目」反查。
  P3 后要加 `project_key` 精确匹配的用例，并保留一条旧格式（无 project_key）的回落用例。
- `tests/test_ledger_store.py` / `test_ledger_activation_registration.py` ——
  P3 加 `project_key` 回填后需补断言。

新增：

- `tests/test_library_split_migration.py` —— `library.json` → `library/` 迁移的幂等性与
  正文完整性（含 164KB 大条目）。
- `tests/test_projects_index.py` —— `/api/projects` 的合流正确性：
  同一 `project_key` 下 任务/收藏/台账/资产 四路数据合成一行；缺任意一路时不崩。
- `tests/test_task_single_write.py` —— 写任务 A 不触碰任务 B 的文件。

---

## 七、已知风险

1. **迁移期双写不一致**（最大风险）。约束：P1 上线后 `library/` 是唯一真相源，
   `library.json` 只读不写，兼容层的 POST 一律转发到单条写。
2. **`project_key` 含中文标题且长度受 `_safe_project_name` 60 字符截断**
   （[server_common.py:1045](../server_common.py#L1045) 注释已说明），作主键时要确认
   截断后仍唯一——`run_<task_id>__` 前缀已经保证了这一点，但新增台账回填时要走同一个
   `make_idea_project_key()`，不能在别处手拼。
3. **旧数据没有 `project_key`**（当前 2 条点子库记录里只有 1 条有）。
   合流接口必须允许 `project_key` 为空的行以 `id` 兜底成孤立项目，不能因为缺 key 就不显示。
4. **`app.js` 已 4669 行**。`js/projects.js` 必须是独立模块，不能再往 `app.js` 里堆——
   仓库已有把函数「moved to modular JS file」的迁移习惯，沿用它。

---

## 九、落地记录（2026-07-31）

### P0 已完成 —— 项目工作台

| 文件 | 变更 |
|---|---|
| `server_common.py` | 新增 `build_projects_index()` / `filter_projects()` 及一组 `_proj_*` 辅助函数：四路数据按 `project_key` 合流成项目表 |
| `server.py` | 新增 `GET /api/projects`（`state`/`q`/`sort`/`limit`/`offset`/`assets` 参数，chips 角标在完整表上统计） |
| `js/projects.js` | 新模块：列表 + 详情双栏、chips 筛选、**keyed 增量渲染**、按"有没有在跑"自适应轮询（4s / 30s） |
| `index.html` | 新增 `#panel-projects` 面板与「📁 项目」主标签；header 两个抽屉按钮合并成 `#open-projects-btn`（角标 id 不变） |
| `css/projects.css` | 新样式，作用域锚在 `#panel-projects`，视觉语言照抄创意台账 |
| `app.js` | `switchMainTab` 注册 `projects`；`Alt+L`/`Alt+T` 改为跳工作台的「已收藏」/「运行中」档；抽屉绑定补上存在性检查 |

实测合流效果（真实数据）：**9 行 → 6 行** —— 同一母项目下 3 次帧序列 + 1 次封面
原本会各占一行，现在按标题分组成 1 行；此前被 `MEDIA_TASK_TYPES` 整类过滤掉的
4 个失败媒体作业，现在在「失败/取消」档里看得见了。

### P1 已完成 —— 点子库拆分存储

| 文件 | 变更 |
|---|---|
| `server_common.py` | 新增拆分存储层：`read_library_index` / `read_library_item` / `write_library_item` / `delete_library_item` / `replace_library_table` / `migrate_library_to_split`；`LIBRARY_LOCK` 改 `RLock`（读-判定-写要在同一把锁里嵌套） |
| `server.py` | 新增 `GET /api/library/index`、`GET /api/library/item`、`POST /api/library/item`、`POST /api/library/item/delete`；`GET/POST /api/library` 降为兼容层（三道防护一字未改） |
| `tools/migrate_library.py` | 迁移 CLI（`--dry-run` / `--force`），输出绕开 `server_common` 对 `sys.stdout` 的 tee |
| `app.js` | 收藏 / 删除 / 导入改走单条写；新增 `persistIdeaItem()` / `deleteIdeaItem()` |
| `.gitignore` | 忽略 `library/` 与 `library.json.pre-split` |

**已对真实库执行迁移**：`library.json` 203.2 KB（2 条，单条最大 160.6 KB）
→ `library/index.json` **2.0 KB** + `library/items/` 2 个正文文件；迁移后逐条与
`library.json.pre-split` 做全字段比对，**两条完全一致，无任何丢失**。

用户可感知的变化：
- 列表渲染读 2 KB 而不是 203 KB；
- 收藏一条 = 写一个文件，不再上传全库；
- **删除库里最后一条不再被 409 拒绝**（老路径会撞上"空列表覆盖非空库"防护）；
- 删除不再需要前端"声明缩量意图"，也不再打两次请求。

### 测试

新增 4 个测试文件、**72 条用例**，全部通过：

- `tests/test_projects_index.py`（21）—— 四路合流、缺路不崩、孤立作业按标题分组、资产目录名解析
- `tests/test_projects_endpoint.py`（8）—— 参数接线、counts 不随筛选缩水、失败回 500 而非空表
- `tests/test_library_split_store.py`（23）—— 写一条不碰别的、索引不含正文、损坏老库拒绝迁移、正文丢失降级为残条
- `tests/test_library_endpoints.py`（20）—— 新旧两套接口，**含三道防护的完整回归**

既有的 `test_library_shrink_guard` / `test_task_history_not_wiped` /
`test_gallery_endpoints` / `test_ledger_store` / `test_restore_slot_e2e` 全部照常通过。

另在隔离目录起真实服务做过端到端冒烟：四个新接口 + 两道 409 防护均行为正确。

### P2 已完成 —— 任务持久层拆分

| 文件 | 变更 |
|---|---|
| `server_common.py` | 新增 `save_task_to_disk()`（单条写）/ `delete_task_files()`（显式删）/ `prune_orphan_task_files()`（目录扫描式清理，从落盘路径剥离）/ `_task_file_id()`（id 消毒）；`load_tasks_from_disk()` 认新旧两种格式并就地迁移；`cleanup_old_tasks` 改显式删文件 |
| `server.py` | 13 处 `save_tasks_to_disk()` → `save_task_to_disk(tid)`；`/api/tasks/delete`、`/api/tasks/clear` 改显式 `delete_task_files()`；`prune_orphan_task_files()` 只在启动加载完成后跑一次 |

存储布局：

```
tasks/<id>.json          meta（id/status/dimensions/error/last_active），平均 0.6 KB
tasks/events/<id>.jsonl  事件流，运行中**追加**新增的那几条，不重新序列化整份
tasks/results/<id>.json  结果全文，只在有结果时写
```

实测（真实的 21 条任务记录，合计 2.8 MB）：

| | 旧 | 新 |
|---|---|---|
| 一次状态变更的同步写量 | **2777.6 KB**（内存里所有任务整份重写） | **0.6 KB**（只写这一条的 meta） |

迁移验证：21 条任务 → 迁移 → 再模拟一次重启，**任务数、状态、事件条数、结果全部零漂移**；
唯一差异是既有行为（`running` 任务重启后转 `failed` 并补一条 error 事件）。

**新增第三段防呆**（`TASK_ORPHAN_GRACE_SECONDS = 24h`）：前两段防呆都假设"本进程的
内存 = 磁盘应有的全部"，但只要有第二个写者，这个前提就不成立。落地过程中真实撞到过：
一个指向同一 `tasks/` 的第二实例启动（`run()` 会 `os.chdir` 到仓库目录，绕开了本以为
生效的隔离），把 4 个正在跑的任务记录当孤儿删了——内存里还在，一重启就没。现在
`prune_orphan_task_files` 只删够旧的文件，刚写下来的永远安全。

> 顺带的教训：**验证用的第二个服务实例没法靠改 CWD 隔离**——`run()` 第一行就
> `os.chdir(os.path.dirname(__file__))`。要隔离任务目录，得让 `TASKS_DIR` 也可配置
> （目前只有 `DB_FILE`/`LEDGER_FILE`/`LIBRARY_DIR` 支持环境变量覆盖）。

测试：`tests/test_task_store.py`（23 条：三份文件布局、事件追加不重复、缩短时整份重写、
老格式迁移幂等、截断行不拖垮整条任务、id 路径穿越、删除后 flush 状态清零）；
`tests/test_task_history_not_wiped.py` 重写为 8 条（两段旧防呆转测
`prune_orphan_task_files`，新增"落盘绝不删文件"与新的新鲜度防呆）；
`tests/test_task_retry_overwrite.py` 跟随新布局更新。

### P3 已完成 —— project_key 贯通

`project_key` 不再是"结果阶段才生成"，而是**任务创建那一刻**就定下
（`server_common.ensure_task_project_key`），并写进三处：

| 位置 | 变更 |
|---|---|
| 激发任务 | `get_or_create_task` / `prepare_task_for_run` 落 `dimensions.project_key`；compose 结果不再用 LLM title 重算一遍（那会与运行中那个键不一致，把同一条创意拆成两份），自治管线与 compose 从此同键 |
| 媒体子作业 | frames / staged / videos / cover 四个创建点显式带上母项目的 key，`build_projects_index` 优先按它挂接，标题撞名降为老任务回落 |
| 创意台账 | `/api/compose` 登记候选时写入 `project_key`，台账「回到激发项目」变成直查 |
| 画廊 | `gallery_collect_references` 归属反查改两趟：第一趟只认 `project_key` 派生的目录名（硬证据），第二趟才用标题变体补齐没被认领的 |
| 前端 | `findSavedIdeaForSpark` / `findCompletedTaskForSpark` 先按主键直查；新增 `sparkProjectKeyMatches` 处理"目录名把 `__` 折成 `_` 且截断到 60 字符"这层归一化 |

**顺带修掉一个真 bug**：`gallery_collect_references` 还在直接读 `library.json`，
而 P1 之后真相源已经是 `library/` —— 画廊拿到的是迁移前的旧快照，收藏后新增的项目
会被误判成孤儿。改走 `read_library()`。

`TASKS_DIR` 也补上了环境变量开关（`SPARK_TASKS_DIR`），与 `DB_FILE` / `LEDGER_FILE` /
`LIBRARY_DIR` 一致 —— 这是被 P2 那次事故教出来的：`run()` 第一行就
`os.chdir(仓库目录)`，光靠切工作目录隔离不了第二个服务实例。

### P4 已完成 —— 删除旧路径

| 删除的东西 | 说明 |
|---|---|
| `#library-drawer` / `#tasks-drawer` | 73 行 DOM，及 base/efficiency/panels-tabs 三份 CSS 里 57 条独占规则、tokens.css 一条暗色阴影 |
| `openLibraryDrawer` / `closeLibraryDrawer` / `openTasksDrawer` / `closeTasksDrawer` / `setDrawerToggleOpenState` / `DRAWER_TOGGLE_LABELS` | 连同日志 dock 与两个抽屉"三者互斥"的协调逻辑 |
| `renderTasks` + 抽屉筛选状态 + `_lastTasksRenderHtml` + `taskModelOptions` + `formatTaskDuration` | 241 行 |
| `renderLibrary` + `showDeleteConfirm` | 90 行 |
| `startTasksPolling` / `stopTasksPolling` | 恒定 2.5s 全量轮询，被工作台的自适应轮询取代 |
| `saveLibrary`（整表回写） | 7 处调用点全部改走 `persistIdeaItem` 单条写 |
| `POST /api/library` + `parse_library_payload` + `replace_library_table` | 及它们的测试。`GET /api/library` 保留（全量导出还在用），`library_shrink_verdict` 保留（创意台账的整表回写仍需要） |

**没有丢功能**：抽屉里的「导入 JSON / 全部导出 / 清空已完成 / 清空失败」全部搬到工作台
工具栏；任务卡上的「换模型再跑」下拉搬到详情栏；拖放导入 JSON 的靶区从抽屉换成工作台
列表；`Alt+L` / `Alt+T` 改为跳工作台的「已收藏」/「运行中」两档。

### 尚未做

- `api_client.js` / `media_renderer.js` 的写入仍是"改完整条再整条 PUT"。真正的
  PATCH（只提交变动字段）留待需要时再做——单条写已经把代价从"全库 208KB"降到
  "这一条 164KB"，再往下收益递减。
- 点子库索引里预留的 `status` / `tags` / `note` 三个字段还没接 UI。

### 落地过程中踩到并修掉的问题

1. **第二个服务实例误删 4 条正在跑的任务记录**（P2）。`run()` 会 `os.chdir` 到仓库目录，
   我以为隔离了的验证实例其实直接操作了真实 `tasks/`。数据已从活服务内存恢复；
   `prune_orphan_task_files` 补了第三段防呆（24h 新鲜度宽限），并补上 `SPARK_TASKS_DIR`。
2. **e2e 测试往真实创意库写了一条垃圾记录**（P4）。`test_restore_slot_e2e.py` 只隔离了
   `SPARK_DB_FILE` / `SPARK_LEDGER_FILE`，没跟上 P1/P2 新增的 `SPARK_LIBRARY_DIR` /
   `SPARK_TASKS_DIR`；它的第二道保险 `window.saveLibrary` 也随 P4 失效。已补齐四个开关、
   保险改指 `persistIdeaItem`，并从 `library.json.pre-split` 逐字段校验恢复了创意库。
3. **`syncFrameRunToLibrary` 漏写 frameRun**（P4）。批量替换 `saveLibrary` 时少带了一行
   `savedIdeas[existingIdx].frameRun = manifestData`，删除一拍后库里那份不再更新。
   由 `test_restore_slot_e2e` 抓到。
4. **`window.renderTasks = renderTasks` 顶层抛错**（P4）。删函数没删导出，整个 `app.js`
   在那一行之后全部不执行。由 Playwright 的"页面不应有未捕获异常"断言抓到。

