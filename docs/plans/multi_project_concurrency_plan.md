# 多项目并发：可视化方案 + 防冲突规则

> 状态：**P0、P1、P2 已落地（2026-10-02，见文末「十」「十一」「十二」）；P3 仍是设计稿。** 配套原型：[`../reference/concurrency_board_mockup.html`](../reference/concurrency_board_mockup.html)（示意数据，浏览器直接打开）。
>
> 目标：多个创意项目同时跑；**视频生成可以并发，但每个并发任务独占一个 AdsPower 浏览器环境，绝不共用**；
> 并发状态一眼可见，冲突在发生前被规则挡住、发生后能被定位和处理。

---

## 一、结论速览

1. **把"一把全局浏览器锁"换成"账号租约（Account Lease）"。** 并发的最小单位不是任务，而是「一个任务腿 ↔ 一个 AdsPower 环境（user_id）」的独占租约。租约同时绑定出口代理、CDP 连接、页面，生命周期内别人碰不到。
2. **三层互斥，固定加锁顺序：项目声明 → 容量/账号租约 → 文件短锁。** 持有后一层时不许回头等前一层，从结构上消灭死锁。
3. **并发上限 N 是显式配置，默认 1（行为与今天完全一致），灰度到 2。** 每个浏览器环境很吃内存，N 必须可控、可一键回退。
4. **可视化 = 一块"并发作战板"，四个视图**：账号泳道（谁占着哪个浏览器）、项目×阶段矩阵（每个项目走到哪）、等待与冲突雷达（谁在等谁、为什么）、资源条（容量/代理/孤儿）。
5. **分四期落地，P0 只加只读可视化，零行为变化。**

---

## 二、现状体检：为什么今天只能串行

并发被**五个**独立的硬约束同时卡死，只改其中一个没有用：

| # | 约束 | 位置 | 说明 |
|---|---|---|---|
| 1 | 全局单把互斥锁 | [fx_control.py:60](../../fx_control.py#L60)、[:137](../../fx_control.py#L137) `slot()` | `self._lock` 一把锁、`self._active` 一个槽；`limits()` 写死 `max_concurrent: 1`（[:76](../../fx_control.py#L76)） |
| 2 | 兜底串行锁 | [server.py:1826](../../server.py#L1826) `_FX_SERIAL_LOCK` | 即便控制面放行，帧链路仍要再拿这把锁，超 10s 直接抛错 |
| 3 | **"单环境模式"主动关别人的浏览器** | [browser.py:325](../../integrations/google_fx/utils/browser.py#L325) `ensure_single_ads_browser` | 每次 `get_ads_ws_url`（[:578](../../integrations/google_fx/utils/browser.py#L578)）先把**除自己外所有** profile 停掉。并发时 A 一启动就把 B 的浏览器杀了 |
| 4 | 账号选取会"捡"别人的浏览器 | [account_pool.py:1200](../../integrations/google_fx/utils/account_pool.py#L1200) `pick_open_account` | 优先复用**任何**已打开且有额度的 profile，还会关掉"额度耗尽"的已开 profile——两个任务会选中同一个号，或关掉对方正在用的窗口 |
| 5 | 换号会停"当前"浏览器 | [account_pool.py:1533](../../integrations/google_fx/utils/account_pool.py#L1533) `switch_to_next_account` | `stop_current` 只认 `resolve_account()`，本身没错，但选新号时 `exclude` 里没有"别的任务正占着的号" |

还有两处**不是阻塞、但会在并发下出事**的全局副作用：

- [browser.py:511](../../integrations/google_fx/utils/browser.py#L511) `reveal_hidden_browser_windows` / `rehide_browser_windows` 只在人工接管（等登录/验证码）时调用，但它按"本进程隐藏过谁"**一次翻出全部**静默窗口——并发时会把别的任务的窗口一起翻出来抢焦点。普通任务的静默策略（[:463](../../integrations/google_fx/utils/browser.py#L463) `bring_page_to_front_if_allowed`）本身是安全的；
- 代理轮换计数 `runtime/generation_counter.json`（[proxy_rotator.py:261](../../integrations/google_fx/utils/proxy_rotator.py#L261)）是进程全局的单份计数；代理池 `pick_proxy(exclude_proxy_ids=…)`（[proxy_pool.py:451](../../integrations/google_fx/utils/proxy_pool.py#L451)）已有排除参数，但没人把"已被运行中环境占用的代理"传进去。

### 已经对的、可以直接复用的

- **账号绑定已是 per-task contextvar**（[account_binding.py](../../integrations/google_fx/utils/account_binding.py)），不改 `os.environ`；`bound_task_account()` 已按"账号腿"设计。
- **取消标志已是 per-request**（[cancel_flag.py](../../integrations/google_fx/utils/cancel_flag.py)），无进程级全局位。
- **项目级互斥已有雏形**：`claim_frame_run(project_dir, task_id)`（[server_common.py:4629](../../server_common.py#L4629) 一带）+ `manifest_lock(project_dir)`（[:4609](../../server_common.py#L4609)）。
- **"多活跃"的接口已经预留**：[server.py:977](../../server.py#L977)、[:4583](../../server.py#L4583)、[:4790](../../server.py#L4790) 与 [google_fx_console.js:155](../../js/google_fx_console.js#L155) 都在读 `queue.active_list`——但 `FxControlPlane.snapshot()` 从未产出它（只有单个 `active`，`waiting` 恒为 `[]`）。前端的排队列/优先级列（[google_fx_console.js:737](../../js/google_fx_console.js#L737)）同样没有数据可吃。**这是瘦身重构（ff57817）留下的断层，本方案顺手补上。**
- 测试替身 [tests/fx_fakes.py](../../tests/fx_fakes.py) 可以扩成"多 profile 假 AdsPower"。

---

## 三、概念模型

### 3.1 三类资源与加锁顺序

```
 ① 项目声明  Project Claim     key = (project_key, stage)         非阻塞，抢不到立刻返回 holder
        │
        ▼
 ② 租约      Lease             容量名额 + 账号(user_id) + 出口身份   阻塞排队，可取消，可超时
        │
        ▼
 ③ 文件短锁  manifest_lock / account_pool._LOCK / LIBRARY_LOCK …   毫秒级，锁内禁止网络 / 浏览器 I/O
```

**铁律：只许 ①→②→③ 单向获取。**

- 持有 ② 时**不得**去等 ①（否则 A 持账号 X 等项目 P，B 持项目 P 等账号 X → 死锁）；
- 持有 ③ 时**不得**发起 ②（文件锁里不排队）；
- **失败换号 = 先释放旧租约，再申请新租约**，任何时刻一个任务腿只持一份租约。

### 3.2 租约（Lease）是什么

一份租约 = 一个任务腿对一个浏览器环境的独占使用权：

| 字段 | 含义 |
|---|---|
| `lease_id` / `task_id` / `project_key` / `stage` | 谁、给哪个项目、做哪一步（frames / videos / probe / login …） |
| `user_id` | AdsPower 环境（**唯一键**，同一 user_id 同时最多一份租约） |
| `proxy_id` + `egress_id` | 出口代理条目 + 出口身份（见 R5），用来判"同 IP 冲突" |
| `ws_port` | 本租约浏览器的 CDP 端口，只有持有者连得上 |
| `state` | `waiting → granted → launching → running → releasing → released`；异常态 `stalled` / `orphaned` / `force_released` |
| `acquired_at` / `heartbeat_at` | 占用时长、心跳（判卡死用） |
| `blocked_by[]`（仅 waiting） | 等待原因：`capacity` / `account` / `project` / `ip`，各带持有者与起始时间 |

### 3.3 视频并发的两种粒度（**请你选**，默认 A）

| | A. 项目间并发（默认） | B. 项目内扇出 |
|---|---|---|
| 含义 | 不同项目的视频任务各占一个浏览器同时跑；同一项目同一时刻只有 1 个腿 | 同一项目的 N 个镜头拆给 K 个腿，K 个浏览器同时出片 |
| 复杂度 | 低，只需租约 + 项目声明 | 高，需要"镜头归属表"和逐镜头合并写 manifest |
| 风险 | 低 | 同镜头被两腿重复提交（双倍烧积分）、manifest 互相覆盖 |
| 建议 | **P2 上线** | **P3，开关默认关**，且单项目腿数上限默认 1 |

B 的专属规则见 R10。

---

## 四、防冲突规则

> 每条规则都写明：违反会怎样、在哪里落地。编号即 UI 里"冲突原因"的标签。

| 编号 | 规则 | 违反后果 | 落地点 |
|---|---|---|---|
| **R1** | **一个 AdsPower 环境同一时刻只属于一个租约。** 包括积分探针、选择器探针、自检、自动登录——这些旁路动作也必须先拿该账号的租约 | 两个任务操控同一页面、`bring_to_front`/Escape 互相搅乱 Flow 画布（`browser_gate.py` 注释里的老问题） | `FxControlPlane.lease()`；`browser_gate` 的 `kind` 改为携带 `user_id` |
| **R2** | **一个项目的同一阶段同一时刻只有一个写者；frames 与 videos 对同一项目互斥。** 抢不到不排队，立即返回 `already_running + holder`（沿用 `claim_frame_run` 语义） | 两个 worker 拿各自过期快照整体覆盖 manifest，帧"无故丢失"（[server_common.py:4625](../../server_common.py#L4625) 注释记录过的事故） | 把 `claim_frame_run` 泛化为 `claim_project_stage(project_key, stage, task_id)`；陈旧占位自动收回逻辑保留 |
| **R3** | **浏览器不共享：每个租约独立 profile、独立 CDP、独立 Playwright 连接。** 禁止"复用别人已打开的浏览器" | 页面状态串扰、一个任务关窗口另一个断连 | `pick_open_account` 只允许复用**本任务自己租约内**的 profile |
| **R4** | **只关"无主"的浏览器，永不关别人租约内的。** `ensure_single_ads_browser` 改为 `ensure_profile_exclusive(user_id, leased=set(...))`：关闭 = 已打开 ∖ 全部租约 ∖ 自己；并校验 `运行中数 < N` | A 启动杀了 B 的浏览器（现状约束 #3） | `browser.py`；`stop_ads_browser`/`close_ads_browser` 加 `owner_task` 校验 |
| **R5** | **出口 IP 互斥：同时运行的环境必须是不同出口。** `egress_id = proxy 条目身份`（沿用 [proxy_rotator.py:313](../../integrations/google_fx/utils/proxy_rotator.py#L313) `_proxy_identity`）；**直连（无代理）视为同一个出口 `local-direct`，最多同时 1 个** | 同 IP 多账号同时在线 → Google 关联风控、批量掉登录 | 授予租约时判定；把已占用的条目传给 `proxy_pool.pick_proxy(exclude_proxy_ids=…)`；P2 起启动后再用 `_probe_proxy_ip` 做一次真实 IP 复核（软告警） |
| **R6** | **账号-代理粘性，且代理轮换只作用于自己的环境。** 轮换前后租约里的 `egress_id` 同步更新，轮换流程不得 stop 别人的 profile | 轮换时误停他人浏览器；轮换后撞上另一环境的出口 | `proxy_rotator.rotate_*` 入口校验租约归属 |
| **R7** | **换号 = 释放 + 重新申请，`exclude` 必含"所有他人租约的账号"。** 选号与授予租约在同一临界区内原子完成 | 两个任务同时换号选中同一个"最优号"（check-then-act 竞态） | `switch_to_next_account` / `pick_account` 增加 `exclude_leased=True`，内部走 `FX_CONTROL.lease(pick=...)` |
| **R8** | **积分只由租约持有者写。** 乐观扣减/测量值写回走 `AccountPool` 既有锁，但调用方必须是该账号当前租约的 `task_id` | 并发下积分账错乱、同一账号被两腿双扣 | `record_video_submission` / `optimistic_deduct_credit` 增加 owner 断言（先告警，后拒绝） |
| **R9** | **取消/超时/崩溃只回收自己的东西；强制释放必须显式、二次确认、留审计。** 心跳超时（默认 120s）只标 `stalled`（琥珀色），**不自动杀**；无主浏览器（AdsPower 在跑但无租约）宽限 60s 后才可被"一键清理" | 取消 A 时误杀 B；卡死租约永久占住名额（现状里"只能重启服务"的老问题） | `force_release_active` 已有，扩成按 `lease_id`；落盘 `runtime/fx_leases.json`，重启时与 AdsPower `local-active` 对账 |
| **R10** | **项目内扇出（仅 P3）：每个镜头恰好归属一个腿；腿只写自己镜头的字段，合并写在 `manifest_lock` 内完成；腿失败 → 镜头回到待办池再分配，带重试计数，不自动重复提交。** 复用 `VideoOperationStore` 的 `request_id` 幂等 | 同镜头双提交烧积分；腿互相覆盖 manifest | 新增 `shot_ownership` 表；`manifest` 写入改为"按镜头键 merge" |
| **R11** | **并发模式强制静默窗口，人工接管只动"自己的"窗口。** N>1 时 `reveal_hidden_browser_windows`/`rehide_*` 改为只处理该租约的 `ws_port`（现有 `reveal_browser_window(ws_url)` 已支持按端口）；普通任务一律不 `bring_to_front` | 一个任务等人工登录时，别的任务的窗口被一起翻出、抢走用户焦点 | `browser.py` 窗口策略加 `concurrent_mode` 分支；人工接管链路改传 `ws_url` |
| **R12** | **容量与公平：总名额 N；队列按（优先级 → 项目轮转 → 先到先得）；单项目最多占 `per_project_max`（默认 1）个名额；旁路探针（积分刷新）只吃"空闲名额"，永不抢在生成任务前面。** | 一个大项目霸占全部浏览器；刷新积分把生成任务挤在后面 | `FxControlPlane` 调度器 |

### 配置项（全部可回退）

| 配置 | 默认 | 说明 |
|---|---|---|
| `SPARK_FX_MAX_CONCURRENT` | `1`（P2 起可设 2，硬上限 4） | **为 1 时，行为 = 今天**，其余规则只做审计不阻断 |
| `SPARK_FX_PER_PROJECT_MAX` | `1` | R12；P3 才可调大 |
| `SPARK_FX_LEASE_STALL_SECONDS` | `120` | 心跳超时标 `stalled` |
| `SPARK_FX_ORPHAN_GRACE_SECONDS` | `60` | 无主浏览器宽限期 |
| `SPARK_FX_EGRESS_POLICY` | `hard` | R5：`hard` 拒绝授予 / `warn` 只告警（调试期用） |

---

## 五、调度流程（一次视频任务腿）

```
提交视频任务
  │
  ├─① claim_project_stage(project, 'videos')  ── 失败 → 返回 already_running + holder（不排队）
  │
  ├─② FX_CONTROL.lease(task, kind='videos', want=pin/strategy/priority_ids)
  │      进入 waiting，blocked_by 实时更新：
  │        capacity  已满 N/N
  │        account   候选号全被占用 / 冷却 / 额度不足
  │        ip        候选号的出口与某运行环境相同
  │      授予时：原子地「选号 + 校验出口 + 占名额」→ state=granted
  │
  ├─ ensure_profile_exclusive → 启动/复用 本租约的 profile → state=running
  │      运行中：每个关键步骤 lease.touch() 刷新心跳
  │
  ├─③ 写 manifest / 积分 / 日志：仅文件短锁
  │
  └─ finally：释放租约 → 关闭自己的 profile（可配保留热启动）→ release_project_stage
```

失败换号：`release(旧租约)` → `lease(exclude=已试 ∪ 他人租约)` → 新租约；中途**不持双份**。

---

## 六、可视化方案：并发作战板

放在现有「Google FX 控制台」页（[google_fx_console.js](../../js/google_fx_console.js)）里替换 `renderSlotDisplay` 那一块单槽卡片，并在项目工作台的项目卡上放一个小徽标（当前阶段 + 是否被阻塞）。原型见 [concurrency_board_mockup.html](../reference/concurrency_board_mockup.html)。

### 6.1 四个视图（同一块面板，自上而下）

**A. 账号泳道（"谁占着哪个浏览器"）** —— 主视图
- 行 = AdsPower 环境（账号 #序号 + 积分 + 出口 IP 尾号 + 状态）；列 = 时间轴（最近 30 分钟，右端是"现在"）。
- 条 = 一段租约；**颜色 = 项目**（按 `project_key` 哈希到固定色相，全局稳定，换页面不变色），条内文字 = 阶段 + 进度（`视频 3/8`）。
- 空档 = 虚线"空闲 · 可接单"；右端的实线光标 = 现在。
- 顶部容量条：`浏览器 2 / 3`，满了变琥珀；有 `stalled` 变红点。

**B. 项目×阶段矩阵（"每个项目走到哪了"）**
- 行 = 项目；列 = 选题 → 合成 → 帧 → 视频 → 剪辑 → 交付。
- 单元格 = 状态徽标：`完成` / `运行中 5/8` / `排队·等账号` / `被占·项目锁` / `失败`；点击展开抽屉，联动过滤该任务日志（沿用 `task_id` 日志过滤）。

**C. 等待与冲突雷达（"谁在等谁、为什么"）**
- 每条等待 = 一句人话 + 规则编号：`视频任务 B 等待账号 #70 ← 被「校车改造·视频」占用 4m12s · R1`。
- 每条带动作：**取消** / **提优先级** / **改绑账号** / **强制释放**（仅对 `stalled`，二次确认并说明会关闭哪个浏览器）。
- 最底部"等待依赖图"：只有出现环（理论上规则 ①→②→③ 已杜绝）时才亮红色"检测到循环等待"，作为兜底自检。

**D. 资源条（"家底"）**
- 账号小胶囊：空闲 / 占用（项目色点）/ 冷却 / 禁用 / 积分不足；代理出口分组（同色 = 同出口，R5 一目了然）。
- 无主浏览器计数 + "一键清理"；心跳超时计数。

### 6.2 视觉语义（不只靠颜色）

| 语义 | 编码 |
|---|---|
| 项目身份 | 色相（固定哈希）+ 项目名缩写文字 |
| 运行中 | 实心条 + 末端脉冲点 |
| 排队/等待 | 空心虚线框 + 沙漏字形 |
| 被项目锁挡住 | 斜纹填充 + 锁字形 |
| 卡死 `stalled` | 琥珀描边 + 感叹号，**不自动处理** |
| 孤儿 `orphaned` | 灰底 + 问号 |
| 失败 | 红描边 + ✕ |

色值一律走 [tokens.css](../../css/tokens.css) 的变量，明暗主题自动跟随；项目色相只取 6–8 个预设档，保证与暖纸色底有足够对比度；按钮/字号用已有刻度（`--fs-*`、`--radius-*`）。窄屏（≤768）泳道退化成"按账号分组的卡片列表"，矩阵横向滚动。

### 6.3 数据契约（后端 → 前端）

新增 `GET /api/google-fx/board`，同一份数据也内嵌在 `/api/google-fx/status` 的 `board` 字段里（控制台只轮询后者，不多一路请求）：

```json
{
  "capacity": {"max": 3, "used": 2, "per_project_max": 1, "egress_policy": "hard"},
  "leases": [{
    "lease_id": "L_8f2a", "task_id": "videos_1790...", "project_key": "run_x__校车改造",
    "stage": "videos", "state": "running",
    "user_id": "k1f1hip2", "account_label": "#70", "credit": 988,
    "proxy_id": "p_03", "egress_id": "198.51.100.x", "ws_port": 53421,
    "acquired_at": "2026-10-02T15:02:11+08:00", "heartbeat_at": "2026-10-02T15:06:40+08:00",
    "progress": {"done": 3, "total": 8}
  }],
  "waiting": [{
    "task_id": "videos_1791...", "project_key": "run_y__荒岛", "stage": "videos",
    "priority": 0, "position": 1, "since": "...",
    "blocked_by": [{"type": "account", "holder_task": "videos_1790...", "rule": "R1", "since": "..."}]
  }],
  "project_stages": [{
    "project_key": "run_x__校车改造",
    "stages": {"frames": {"state": "done"}, "videos": {"state": "running", "done": 3, "total": 8, "task_id": "..."}}
  }],
  "history": [{"user_id": "k1f1hip2", "project_key": "...", "stage": "videos", "from": "...", "to": null}],
  "orphans": [{"user_id": "k1anmo58", "seen_since": "..."}],
  "server_time": "..."
}
```

- `history` 是泳道的时间段来源：控制面在授予/释放时 append，内存环形缓冲（30 分钟）即可，**不需要新表**；重启后从 `fx_audit.jsonl` 的 lease 事件回放。
- 刷新：板块可见时 1.5s 轮询（现有 `/api/tasks` 是 2.5s，沿用 `document.hidden` 暂停逻辑，[google_fx_console.js:2216](../../js/google_fx_console.js#L2216)）；租约状态变化时可复用现有 SSE 通道推一条 `lease_changed` 触发立即刷新。
- 持久化：`runtime/fx_leases.json`（原子写，与 `account_pool.json` 同风格），审计继续写 `runtime/fx_audit.jsonl`，新增 `lease.grant / lease.release / lease.stalled / lease.force_release / project.claim_denied`。

---

## 七、落地分期

| 期 | 内容 | 行为变化 | 主要文件 | 验收 |
|---|---|---|---|---|
| **P0 可视化先行** ✅ | 控制面补产 `waiting` / `history`；新增 `/api/google-fx/board`；作战板只读（单泳道也能用）；项目卡阻塞徽标 | **无** | `fx_control.py`、`server.py`、`google_fx_console.js`、`css/console/google-fx.css` | 单任务跑一遍，泳道出现一根条、结束后变历史；排队时 `waiting` 有数据 |
| **P1 租约内核（N=1）** ✅ | `lease()` 取代 `slot()`；R1/R3/R4/R7 落地；`claim_project_stage`；`_FX_SERIAL_LOCK` 退役为审计；`ensure_profile_exclusive`；`fx_leases.json` + 重启对账 | **无**（N 仍为 1，规则只多出审计与更清晰的报错） | `fx_control.py`、`browser.py`、`account_pool.py`、`server.py`、`server_common.py` | 现有测试全绿；故意起两个任务，第二个显示 `blocked_by: capacity`；kill -9 重启后无主浏览器被识别 |
| **P2 项目间视频并发** ✅ | `SPARK_FX_MAX_CONCURRENT=2`；R5 出口互斥 + 代理池排除；R6/R8/R11；探针只吃空闲名额；孤儿清理 | **有**：不同项目的视频任务可同时出片 | `proxy_pool.py`、`proxy_rotator.py`、`google_fx_credit.py`、`browser.py` 窗口策略 | 两项目同时出视频、两个独立浏览器、出口不同；取消 A 不影响 B；同出口账号被拒并在雷达里说明原因 |
| **P3 项目内扇出（可选）** | R10：镜头归属表 + 逐镜头合并写；`per_project_max>1` | 有，开关默认关 | `google_fx_video.py`、`server_common.py`（manifest 写入） | 8 镜头拆 2 腿，无重复提交、manifest 无丢字段；一腿失败镜头被另一腿接走且只提交一次 |

**回退**：任何一期出问题，把 `SPARK_FX_MAX_CONCURRENT` 设回 1 即回到串行语义；P1 的租约内核在 N=1 时与旧锁等价，所以 P2 的风险被隔离在配置开关后面。

---

## 八、测试计划

沿用 `tests/` 约定与 [tests/fx_fakes.py](../../tests/fx_fakes.py)（扩成多 profile 假 AdsPower：`local-active` 返回多个、`start/stop/active` 按 user_id 独立）。

1. **租约互斥**：同一 `user_id` 两线程并发申请，只有一个 granted，另一个 `blocked_by=account`。
2. **容量**：N=2 时第三个任务排队，释放后按（优先级→项目轮转→FIFO）出队。
3. **R4 不杀邻居**：A 持租约运行时，B 启动不触发对 A 的 `browser/stop`；无主 profile 才被关。
4. **R7 原子选号**：多线程同时 `pick_account`，不出现两个任务拿到同一 `user_id`（并发压测 ≥1000 次）。
5. **R2 项目声明**：同项目 frames 与 videos 互斥；陈旧占位（任务已终态）自动收回。
6. **加锁顺序/死锁**：构造 A→②等 ① 的反向路径应在开发期断言失败；soak 跑 10 分钟无死锁。
7. **取消/崩溃**：取消 A 只释放 A 的租约与 profile；模拟 worker 异常退出，租约被标 `stalled`，不自动杀；重启对账能识别孤儿。
8. **R5 出口**：两账号同代理 → `hard` 拒绝、`warn` 放行但告警；直连账号同时 ≥2 被拒。
9. **R10（P3）**：镜头归属唯一；腿失败镜头重分配只提交一次；manifest 并发写字段不丢（可复用 `manifest_lock` 竞争测试写法）。
10. **前端**：`/api/fx/board` 契约快照测试；泳道时间段计算、空档、`stalled` 渲染的纯函数用现有 `tests/*.js` 风格测。

---

## 九、风险与待决策

| 风险 | 缓解 |
|---|---|
| 并发开多个 AdsPower 浏览器内存暴涨 | N 默认 1、硬上限 4；P2 先从 2 起并实测内存；启动前做空闲内存检查，不足则保持 waiting 并在雷达里写明 `capacity(memory)` |
| Google 对多账号同时在线更敏感 | R5 出口互斥是硬规则；账号-代理粘性（R6）；P2 灰度期只开 2 路 |
| 现有 `server.py`/`fx_console.py` 有未提交改动，改控制面易冲突 | P0/P1 先在独立提交里完成，再叠 P2；控制面改动集中在 `fx_control.py` 一个文件 |
| 租约表与 AdsPower 实际状态漂移 | 30s 对账（`list_running_ads_browsers`）+ 重启对账；以 AdsPower 为事实、租约表为意图，冲突时标孤儿而不是猜 |

**需要你拍板的三件事**（我的默认建议在前）：

1. **视频并发粒度**：先做 *A 项目间并发*（建议），B 项目内扇出放 P3 观察后再定？
2. **并发上限起步值**：*2*（建议），还是你机器能承受更多？
3. **同出口冲突策略**：*hard 直接拒绝*（建议），还是调试期先 *warn*？

---

## 附：与既有文档的关系

- 任务/项目的数据模型沿用 [project_workbench_refactor_plan.md](./project_workbench_refactor_plan.md) 的 `project_key` 主键；
- 布局语言沿用 [spark_settings_center_layout_plan.md](./spark_settings_center_layout_plan.md)；
- `google_fx.py` 顶部"串行化归属"注释（说 `max_concurrent` 被 `LIMIT_SPEC` 钳在 1）在 P1 落地时需同步改写，现有 `fx_control.py` 已不含 `LIMIT_SPEC`，该注释已过期。

---

## 十、P0 落地记录（2026-10-02）

**零行为变化**：放行仍是 `FxControlPlane` 的一把锁，新增的都是只读观测。

### 做了什么

| 位置 | 内容 |
|---|---|
| [fx_control.py](../../fx_control.py) | `slot()` 在排队时登记 `waiting`、持有时记 `seg_id/user_id`、释放时写入 30 分钟内存环形历史（含 `ok/error/cancelled`）；新增 `board()`、`note_account()`；`snapshot()` 的 `waiting` 不再恒为空 |
| [account_binding.py](../../integrations/google_fx/utils/account_binding.py) | 新增只读观察钩子 `install_account_observer`：`set_task_account` 与"无显式账号"的 `resolve_account` 会上报，泳道据此知道占用落在哪个账号；观察者抛错会被吞掉 |
| [server.py](../../server.py) | `_build_fx_board()`（合并控制面、任务表、号池；`list_accounts(heal=False)`，不打 AdsPower）；`/api/google-fx/board`；状态快照内嵌 `board`；`/api/projects` 每行带 `fx_queue` |
| [js/fx_board.js](../../js/fx_board.js)、[css/console/fx-board.css](../../css/console/fx-board.css)、[console.html](../../console.html) | 作战板：账号泳道 / 项目×阶段 / 等待与冲突 / 账号条；点击条或单元格联动日志过滤 |
| [js/projects.js](../../js/projects.js) | 项目卡徽标：`⏳ 排队等浏览器`（悬停显示在等谁、已等多久）/ `🖥 占用浏览器` |
| 测试 | [tests/test_fx_board.py](../../tests/test_fx_board.py)（11 项）、[tests/test_fx_board.js](../../tests/test_fx_board.js) |

### 与设计稿的偏差（有意为之）

1. **没有给 `snapshot()` 加 `active_list`。** [server.py](../../server.py) 的 `/api/account-pool/refresh`、`/api/account-pool/login` 读到非空 `active_list` 会对生成类任务回 **409 FX_BUSY**（2026-07-27 的设计意图）。但瘦身重构后 `active_list` 一直为空，这两个接口实际是"排在生成任务后面等"。直接补上会让行为变化，所以 P0 只在 `board()` 里给 `leases`，并在测试里钉死 `snapshot()` 不含 `active_list`。**这个 409 要不要恢复，留到 P1 决定。**
2. **"第 N 位"只是参考。** 放行靠 `threading.Lock`，不保证 FIFO；P1 的租约调度器才会真正按（优先级→项目轮转→先到先得）出队。
3. **矩阵只有「帧 / 视频」两列。** 选题/合成/剪辑/交付不是浏览器任务，P0 数据源里没有。
4. **P0 没有的**：心跳/卡死标记（需要 `lease.touch()`，P1）、无主浏览器（需要打 AdsPower，轮询里不允许）、历史重启回放（只在内存，重启后泳道从空开始）。
5. 原型 HTML 里的 6 列矩阵、`stalled`、`orphaned` 是 P1 之后的形态。

---

## 十一、P1 落地记录（2026-10-02）

**N 仍为 1，放行行为不变**：规则 R3/R4/R7 在"没有他人租约"时与旧行为逐字等价，用替身 provider 才能触发，所以测试里专门把它们真正跑了一遍；没装登记簿时回退到旧行为也有测试钉住。

### 做了什么

| 位置 | 内容 |
|---|---|
| [lease_registry.py](../../integrations/google_fx/utils/lease_registry.py)（新） | 包内的"租约登记簿"：宿主安装 provider，包只问两件事——谁被别的任务租着、当前任务心跳。同 `browser_gate` 套路，未安装 = 空集合/空操作，任何异常都被吞掉 |
| [fx_control.py](../../fx_control.py) | `slot()` 即租约：带 `lease_id / project_key / stage / heartbeat`；`touch()` 心跳；`board()` 里租约 `state` 可为 `stalled`；`leased_user_ids()`；租约落盘到 `runtime/fx_control_state.json`；启动时识别上一进程遗留租约（`recovered`）；强制释放现在会写入历史（`force_released`）；新增审计 `lease.grant / release / stalled / recovered` |
| [logger.py](../../integrations/google_fx/utils/logger.py) | 每条 FX 日志 = 一次心跳（`lease_registry.touch()`） |
| [browser.py](../../integrations/google_fx/utils/browser.py) | `ensure_single_ads_browser` → **`ensure_profile_exclusive`**（旧名保留为别名）：只关"无主"环境，保留别的任务租用的 |
| [account_pool.py](../../integrations/google_fx/utils/account_pool.py) | `pick_account` 排除他人租着的账号；`pick_open_account` 不"捡"他人窗口、也不替他人关闭额度耗尽的窗口 |
| [server.py](../../server.py) | 把项目/阶段传入租约；启动时安装登记簿并确认恢复；**开着的浏览器对账线程**（30s，问 AdsPower，结果缓存）；board 增加 `open_browsers`（leased/warm/orphan）、R2 观察、`recovered`；`POST /api/google-fx/orphans/close` |
| 作战板 | 卡死条（琥珀描边 + !）；"疑似卡死/无主浏览器"徽标；等待项注明项目互斥；中断恢复提示；账号条标注浏览器状态；「关闭无主浏览器」按钮（二次确认） |
| 测试 | [tests/test_fx_lease.py](../../tests/test_fx_lease.py)（20 项），[tests/test_fx_board.js](../../tests/test_fx_board.js) 扩充 |

### 判定口径

- **无主浏览器** = AdsPower 里开着、**无租约**、**本进程近 30 分钟没用过**、且被观察到超过 60 秒宽限期。生成结束后留着复用的浏览器（保温）**不算**无主，否则会天天误报。
- **卡死** = 租约心跳（该任务最近一条日志）超过 300 秒。只标记，从不自动释放或关闭。
- 清理接口每次都用最新租约重新判定，被租用/保温/宽限期内的一律跳过并如实回报。

### 与设计稿的偏差（有意为之）

1. **`_FX_SERIAL_LOCK` 没有退役。** 在 N=1 下它是对"绕过队列的调用方"的真实兜底，退役没有收益只有风险；等 P2 放开并发、它会挡住第二个任务时再处理。
2. **R2 `claim_project_stage` 只做观察，没有强制。** 同项目 frames 与 videos 现在仍是排队等待（行为不变）；作战板会标注并写 `project.conflict_observed` 审计，真正"直接拒绝"留到 P2 与并发一起上，否则 N=1 下会把原本能排队的请求变成报错。
3. **卡死阈值 300s 而不是 120s。** 视频提交后的轮询等待日志稀疏，120s 会把正常等待误报成卡死。
4. **R4 的 `stop/close` 归属校验没做。** 控制台手动"关闭浏览器"仍可关任何环境（用户明确操作），只对自动路径（`ensure_profile_exclusive`、`pick_open_account`）加了保护。
5. **模块导入不写盘。** 一开始在 `FxControlPlane` 构造时就落盘，被测试的"禁止改写真实运行数据"守卫抓到；现在遗留租约保留在文件里，由 `bootstrap_fx_runtime` 调 `acknowledge_recovery()` 清理。
6. **仍未做**：`/api/account-pool/refresh` 的 409 是否恢复（见 P0 记录第 1 条）——P1 没有产出 `active_list`，保持现状。

### 给 P2 的备忘

- `leased_user_ids()` 现在只有单个 active，P2 改为遍历全部租约即可，登记簿接口不用动。
- 需要把 `_FX_SERIAL_LOCK`、`ensure_profile_exclusive` 里的"最多一个运行中"前提、`ACTIVE_TASKS` 的 `userId` 单值假设一并检查。
- 对账线程会在 N>1 时对 AdsPower 产生稳定的 30s 一次请求，已验证成本可忽略，但留意 AdsPower 的限频。

---

## 十二、P2 落地记录（2026-10-02）

**默认仍是串行（并发数 = 1）。** 在控制台「配置与维护 → 并发」把「浏览器并发数」改成 2 即热生效，不用重启；改回 1 立即回到串行语义。

### 做了什么

| 位置 | 内容 |
|---|---|
| [fx_control.py](../../fx_control.py) | **调度核心重写**：单把锁 → 多租约 + 准入条件。`SPARK_FX_MAX_CONCURRENT`（1–4，默认 1）；放行条件 = 容量 / 账号(R1) / 出口(R5) / 项目(R2)；排队按（优先级 → 先到先得），被挡住的不拖住后面能放行的；`claim_account()` 原子占号；强制释放的租约变"僵尸"，**线程不退出就不释放名额**；状态文件支持多租约，崩溃恢复支持多条 |
| [lease_registry.py](../../integrations/google_fx/utils/lease_registry.py) | 增加 `claim / current_claim / restore_claim / concurrency` |
| [egress.py](../../integrations/google_fx/utils/egress.py)（新） | 账号出口身份 = 代理（类型、主机、端口、用户名），**不含密码**；无代理统一为 `local-direct`（同时最多 1 个）；10 分钟缓存；查不到按"未知"处理，不当成冲突；N=1 时从不调用 |
| [account_pool.py](../../integrations/google_fx/utils/account_pool.py) | 选号**先占再探**：占不到（别的租约先到 / 同出口）就换下一个候选；选号落空时恢复原账号 |
| [browser_gate.py](../../integrations/google_fx/utils/browser_gate.py) 与 5 个旁路动作 | 旁路动作带上目标账号，目标账号被别的任务占用时排队，不再去碰它；老签名的闸门不受影响 |
| [browser.py](../../integrations/google_fx/utils/browser.py) | 人工接管（等登录/验证码）在并发时只翻出**当前任务自己**的窗口（R11） |
| [server.py](../../server.py) | 任务记录里用户指定的环境成为 `want_account`；N>1 时兜底串行锁让路；探针闸门解析默认账号；启动时安装出口解析器 |
| [fx_console.py](../../fx_console.py) | 新配置 `fxMaxConcurrent`（1–4）、`fxEgressPolicy`（hard/warn），热生效 |
| [server_common.py](../../server_common.py) | 不经过 `pick_account` 的选号路径同样遵守"别人占着的号不用"：**序列默认环境**（首选账号先占后用，被占就让出走自动选号）、切腿复核 `revalidate_leg_account`、轮转候选 `_next_unused_account` |
| 作战板 | 排队原因逐类说明（名额 / 账号被占 / 同出口 / 项目互斥）、僵尸租约提示、并发模式说明；代理变更时出口缓存失效 |
| 测试 | [tests/test_fx_concurrency.py](../../tests/test_fx_concurrency.py)（39 项，含"两个租约各开各的浏览器、互不关闭"的假 AdsPower 端到端、并发写状态文件的回归） |

### 与设计稿的偏差（有意为之）

1. **同项目冲突是"排队"，不是"直接拒绝"。** 设计稿写的是 `claim` 立即返回 `already_running`；但现在这类请求本来就是排队的，改成报错是新的失败路径，没有安全收益。排队 + 作战板说明原因，同样保证同一项目不会并行写 manifest。
2. **项目互斥范围是"同一项目任何阶段"**（设计稿只写了同阶段互斥 + 帧/视频互斥，合并为一条更简单）。
3. **旁路探针没有"殿后"。** 设计稿 R12 要探针只吃空闲名额，但现有代码把探针优先级设得比生成任务高（它们很短，选号依赖结果），按现有意图保留。
4. **R8（积分只由租约持有者写）没做断言。** 账号租约已经保证同一时刻只有一个任务在用同一个账号，积分写入天然只来自持有者，再加断言只是重复。
5. **启动前内存检查没做。** 需要引入系统内存读取（平台相关），而 N 有 4 的硬上限、且默认是 1；上线观察后再决定要不要。
6. **没有给"关闭浏览器"类的手动操作加归属校验**（同 P1）。
7. 项目内扇出（P3）仍未做，`per_project_max` 固定为 1。

### 并发语义要点

- **想用的账号**：任务记录里用户指定了环境（`userId` / `googleFxUserId`）就是 `want_account`，授予租约的那一刻就写进租约；**没指定**（自动选号）时，选号过程用 `claim_account` 原子占号，两个任务不可能拿到同一个账号。
- **嵌套的积分探测**（选号时对过期账号探测）复用外层租约，不额外占名额。
- **兜底串行锁** `_FX_SERIAL_LOCK`：N=1 保持原样；N>1 让路，互斥改由租约保证。
- 设置里的值会写 `SPARK_FX_MAX_CONCURRENT` 环境变量；控制台配置是唯一来源，会覆盖启动时手工导出的同名环境变量。

### 上线前自查

1. 号池里至少有 2 个**积分足够且出口不同**的账号（出口相同的账号不会同时跑）。
2. 先设 2，同时跑两个不同项目的视频，观察作战板与系统内存。
3. AdsPower 本地 API 有 1 次/秒的限频；并发 + 30 秒对账线程会让调用更密，若日志里出现"限频"重试变多，把对账间隔 `SPARK_FX_RECONCILE_SECONDS` 调大。

### 上线前自查结果（2026-10-02，只读）

- 号池启用 7 个账号，出口分成 6 组：**环境 #70 与 #66 共用同一个出口**，两者不会同时运行（hard 策略下后到的会选别的账号）。其余 5 个各自独立。
- 你配置了 `googleFxSequenceUserId`（序列默认环境）：并发时它只是"首选"，被别的任务占着会让出，不会两个任务抢同一个。
- 实际日志里已经频繁出现 Google 的 "We noticed some unusual activity" 风控提示。并发意味着更多账号同时在线，**这个提示若变多，先把并发数调回 1**。

### 实现中发现并修掉的问题

1. 多个任务同时授予/释放租约时，状态文件共用同一个临时文件名，会被写成坏 JSON（已加写锁并有回归测试）。
2. 测试里 `apply_direct_env` 直接写环境变量没被还原，污染了后面的串行语义用例（已在 `conftest` 加自动清理夹具）。
3. 序列默认环境的选号路径绕过了 `pick_account`，两个并发任务会同时落到同一个账号（已补占号）。
