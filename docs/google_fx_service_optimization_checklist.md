# Google FX 服务管理优化清单

审阅范围：`fx_control.py`、`integrations/google_fx/**`、`server.py` 的 FX/号池端点、
`js/google_fx_console.js` + `console.html` 的控制台面板、`tests/test_*fx*`。

目标：**更灵活可控、更好管理**，并补齐**调试能力**，让稳定性/卡点/bug 类问题能变成
可复现的任务交给 agent 修。

优先级：P0 = 会导致服务卡死或数据/展示失真；P1 = 明显限制可控性或排查效率；P2 = 体验与长期维护。

---

## 一、卡点与稳定性（P0）

### S1. 三层串行锁重叠，中间一层不可取消、不带超时
- 位置：`fx_control.py:137`（`FX_CONTROL.slot`）→ `server.py:949,956`（`_FX_SERIAL_LOCK`）
  → `integrations/google_fx/services/google_fx.py:92,127`（`_GOOGLE_FX_RUN_LOCK`）
- 问题：同一个浏览器资源被三把锁保护。`_fx_serial_lock_for` 在 `task_id`/`cancel_event`
  缺省时直接返回裸 `_FX_SERIAL_LOCK`（`server.py:966`），该路径不排队、不响应取消。
  一旦裸锁路径持有它，已经拿到队列 active slot 的任务会无限期阻塞在 `server.py:956`——
  此时 `FX_CONTROL` 认为"有任务在执行"，整个队列停滞，而控制台的取消只在 slot **等待阶段**
  生效（`fx_control.py:151`），对已进入临界区的任务无效。
- 建议：以 `FX_CONTROL` 为唯一 admission 入口；`_FX_SERIAL_LOCK` 改为带 timeout +
  取消轮询的获取，或在确认无遗漏调用点后删除。`_GOOGLE_FX_RUN_LOCK` 保留为库内自保，
  但超时要上报为可识别的错误码而不是裸 `RuntimeError`。

### S2. 积分探测绕过队列，会和正在跑的生成任务抢同一浏览器
- 位置：`server.py:2190`（`/api/account-pool/refresh`）、
  `integrations/google_fx/utils/account_pool.py:384`（`pick_account` 里的 stale 刷新）→
  `services/google_fx_credit.py:108`（`get_ads_ws_url` 真的启浏览器）
- 问题：两条路径都完全不经过 `FX_CONTROL` / `_FX_SERIAL_LOCK`。生成任务运行中点"刷新积分"
  会启动/复用同一 AdsPower profile、`bring_to_front`、按 Escape，直接干扰 Flow 页面状态。
- 建议：探针也走 `FX_CONTROL.slot(kind='probe', priority=高, 短 deadline)`；或在
  `lock_busy` 时返回 409 并在控制台提示"生成中，稍后再探测"。

### S3. 状态快照会被 AdsPower 的 HTTP 调用拖住
- 位置：`server.py:496`（快照里 `list_accounts()`）→ `account_pool.py:100`（未命名账号触发
  `_profile_name_map`）→ `account_pool.py:197`（4 次退避重试，最坏 ~12s/页，每页再 sleep 1.1s）
- 问题：前端每 6s 轮询一次（`js/google_fx_console.js:422`）。AdsPower 卡住时轮询请求会堆叠，
  控制台表现为整体"假死"，而它本来的定位是"只读状态、不碰浏览器"。
- 建议：快照只读状态文件（给 `list_accounts` 加 `heal=False`），命名自愈只在显式
  导入/编辑时做；快照结果加 1~2s TTL 缓存；前端改成"上一次未返回就跳过本轮"。

### S4. 换号是进程级 env 副作用，不还原、会互相覆盖
- 位置：`account_pool.py:449`（`os.environ["ADSPOWER_DEFAULT_USER_ID"] = ...`）、
  读取方 `utils/browser.py:156`
- 问题：一次失败换号会永久改变**后续所有任务**的默认账号（包括本来不该漂移的任务）；
  两条链路同时换号互相覆盖；控制台"当前环境"显示的值也会被悄悄改掉。
- 建议：账号绑定改成 per-task 上下文（contextvar，和 `fx_control.current_fx_task_id` 同层），
  env 只作为进程兜底默认值；换号事件写审计。

### S5. 视频链没有提交节奏闸门
- 位置：`services/google_fx_helpers.py:4013`（`fx_pacing_wait`）
- 问题：`note_fx_submit()` 两条链都记（`helpers.py:1547`、`google_fx_image.py:649`），
  但 `fx_pacing_wait()` 只有图片链调用（`google_fx_image.py:726`）。视频批量连续提交
  没有最小间隔约束。
- 建议：视频提交前统一调用；min/max 间隔纳入配置白名单。

### S6. 号池状态写失败被静默吞掉
- 位置：`account_pool.py:86`
- 问题：磁盘写失败只留一行日志，禁用/冷却标记回退到旧值，选号会重新选中刚被拉黑的账号。
- 建议：写失败要抛给调用方或至少进 `diagnostics`，控制台红条提示。

### S7. `set_task_label` 是模块全局
- 位置：`integrations/google_fx/utils/logger.py:27,30`
- 问题：FX 任务串行，但非 FX 任务与探针线程并发时，日志前缀会串到别的任务上，
  按 task 过滤日志不可靠。
- 建议：改 contextvar。

---

## 二、Bug / 展示失真（P0–P1）

### B1. 控制台"IP 轮换：已关闭"是硬编码，可能与实际行为相反
- 位置：`console.html:312`、`server.py:594`（`'ip_rotation_enabled': False`）
- 实际：`utils/browser.py:161-168`，`get_ads_ws_url` 默认 `auto_rotate_proxy=True`，
  只要 `MIYA_PROXY_*` 配好、`MIYA_AUTO_ROTATE=1`（默认值就是 1，`config.py:172`）就真的换 IP。
- 建议：读 `ProxyRotator.is_configured / auto_rotate` 真实上报，并在配置面板给显式开关
  和 `MIYA_ROTATE_THRESHOLD` 输入。

### B2. `DEFAULT_CREDIT = 1000` 是编造值，被当成真实积分展示
- 位置：`account_pool.py:33,148`
- 问题：与模块自己的原则（"积分数字只信真实探测"）矛盾。从未探测过的账号在控制台显示
  "积分 1000 / 可用"；`pick_account` 的按积分降序（`account_pool.py:378`）在新号之间也失去意义。
- 建议：新增账号 `credit=None`；控制台显示"未探测"；排序把 `None` 排在已探测可用号之后。

### B3. 配置回滚只能回退一步，且第二次点击是空操作
- 位置：`server.py:422`（只找最近一条 `action == 'config.update'`，自身写 `config.rollback`）
- 问题：无法连续往前回退，也没有 redo；用户点两次会以为"又回滚了一步"。
- 建议：维护配置版本栈（`runtime/fx_config_versions.jsonl`），支持列出版本、看 diff、
  回滚到任意版本、重做。

### B4. 前端丢掉了 `cooldown_reason`
- 位置：`js/google_fx_console.js:23-33`（判定顺序：disabled → cooldown → credit<=0）
- 问题：后端区分了 `cooldown_reason='login_required'`（`account_pool.py:346`，2h 冷却、不清零积分）
  和额度耗尽（24h、清零），前端一律显示"冷却中"。用户不知道该去人工登录还是等额度。
- 建议：拆成"待人工登录"/"额度耗尽"两种状态，前者提供"打开该浏览器登录"按钮。

### B5. 审计日志写了但控制台基本看不到
- 位置：`server.py:1866` 返回 `audit`，但前端只用了 config 审计的第一条（`google_fx_console.js:237`）
- 问题：`queue.started / released / cancelled_before_start / reprioritize / task.cancel /
  control.*` 全部写进了 `runtime/fx_audit.jsonl`，UI 零展示。
- 建议：控制台加"操作与队列时间线"面板，支持按 action/task_id 过滤。

### B6. 审计文件只追加不轮转，读取方式是全量解析
- 位置：`fx_control.py:70`（append）、`fx_control.py:76`（每次全文件读 + 全行 `json.loads`）
- 问题：长期运行文件无上限增长，且状态接口每次刷新都会全量解析，越跑越慢。
- 建议：按大小轮转（复用 logger 的 `RotatingFileHandler` 思路）；`recent_audit` 从文件尾部反向读。

### B7. 去重缓存会静默返回历史结果
- 位置：`google_fx.py:142,171-175`（TTL 默认 600s，`google_fx.py:91`）
- 问题：同指纹请求在 10 分钟内直接 `deepcopy` 上次结果。用户"重跑同一个分镜"会拿到上次文件，
  而任务流里看不出"这次没真跑"。
- 建议：命中缓存要写进 task events + 审计并在控制台标注；默认只对"同一 task_id 的重复提交"生效。

### B8. 选择器统计只进不出
- 位置：`utils/selector_stats.py:31-42`（`_STATS` 内存缓存永不失效）、无 reset 接口
- 问题：修好选择器后控制台的漂移警告（`server.py:533`）会永远挂着；`record_hit` 只覆盖 14 处
  调用点，`UI_SELECTORS` 里的族远多于此，覆盖不全导致漂移信号有盲区。
- 建议：加 `reset(family=None)` 与"标记基线"接口 + 控制台按钮；`_load` 支持按文件 mtime 重载；
  把 `record_hit` 补到所有多级 fallback 命中点。

---

## 三、可控性与灵活性（P1）

### C1. 配置白名单太窄，关键参数只能改 env + 重启
- 位置：`server.py:369-376`（`_FX_CONFIG_SPEC` 仅 6 个字段）
- 建议纳入（标注 hot / 需重启）：
  - `MAX_WAIT_SECONDS`、`GOOGLE_FX_REQUEST_BUDGET_SECONDS`（`config.py:128,131`）
  - `GOOGLE_FX_RUN_LOCK_WAIT_SECONDS`、`GOOGLE_FX_DEDUP_TTL_SECONDS`、
    `GOOGLE_FX_VIDEO_BATCH_FORCE_SERIAL`（`google_fx.py:89-91`）
  - pacing 的 min/max 间隔（`helpers.py:4013`）
  - `_manual_intervention_max_wait`（`helpers.py:2714`）
  - MIYA 轮换开关与阈值（`config.py:163-173`）
  - 日志轮转参数（`logger.py:22-23`）

### C2. 队列调度维度太少
- 位置：`fx_control.py`（只有全局 pause/drain/resume + ±1 优先级）
- 缺：
  - 并发度上限可配（现在硬编码"单活跃"，`fx_control.py:157`）
  - 按 kind 分类限流/配额（frames / videos / probe / selftest）
  - 单任务执行超时 + 自动踢出（现在长跑任务只能人工取消）
  - 定向调度（"这个任务只用账号 X"）
  - 优先级直接设值/拖拽，而不是只能 ±1（`google_fx_console.js:392`）

### C3. 队列状态纯内存，重启即丢
- 位置：`fx_control.py:34-36`（只有 accepting/paused 落盘）
- 建议：waiting/active 落盘，启动时把上次的 active 回放成 `orphaned` 记录，
  控制台能回答"上次为什么中断"。

### C4. 控制台不能对单个任务做 override 重跑
- 缺：指定账号/模型/时长后重跑某个失败任务。现在只能改全局配置再从业务侧重新发起。

---

## 四、调试功能（新增，P1）

### D1. 分级自检端点 `POST /api/google-fx/selftest`
- L0 只读：运行时 import、AdsPower 端口、号池状态文件、`runtime/` 可写性、配置一致性
- L1 连浏览器不提交：CDP 连接、打开 Flow、定位 prompt 输入 / 发送按钮 / 配置按钮、读积分
- L2 真实最小提交：一张最小图片，验证端到端
- 要求：走 `FX_CONTROL` 队列、带 deadline、返回**逐步骤耗时**和失败步骤名，
  写审计。这是"测试服务稳定性"最直接的抓手。

### D2. 失败现场取证落盘
- 现状：`_dump_visible_button_texts`（`helpers.py:2473`）、`_dump_prompt_bar_for_diagnosis`
  （`helpers.py:3658`）、`_get_flow_menu_debug_info`（`helpers.py:476`）都只写日志，
  排查要翻 3MB+ 的 `server.log`。
- 建议：失败时自动 `page.screenshot` + DOM 片段 + 上述 dump 结果，落
  `runtime/fx_debug/<task_id>/<stage>/`，控制台按任务列出可下载。

### D3. 选择器探针端点
- 对 `UI_SELECTORS` 每个族在当前页面 `count()/is_visible()` 一次，输出"哪个族失效、
  命中第几层"，并显示 `SELECTOR_VERSION`（`ui_selectors.py:15`）。
- Flow 改版是这套自动化最常见的故障源，现在只能靠事后统计反推。

### D4. 阶段计时与卡点定位
- 现状：`_fx_task_stage`（`server.py:442`）只取最后一个 stage，没有耗时。
- 建议：记录 stage 时间线（进入时刻 + 驻留时长），控制台把超阈值阶段标红。
  像"等工具栏空转 180s"（`helpers.py` 里 `_page_is_gone` 注释记录的实测 case）
  这类问题就能一眼看到，而不是靠读日志推断。

### D5. FX 专属日志流
- `/api/logs` 是全量。加 `?scope=fx&task_id=`，控制台内嵌实时 tail。

### D6. Dry-run 与 DOM 回放
- `FX_DRY_RUN=1`：走完所有 DOM 定位与配置校验，但不点发送。
- 把一次真实运行的 DOM 快照存档，供离线回放。
- **这是让 agent 在没有真账号/AdsPower 的环境里复现并修 bug 的前提条件。**

### D7. 手动接管可视化
- `wait_out_manual_intervention`（`helpers.py:2741`）期间控制台应显示"等待人工登录/验证码，
  剩余 Xs"以及一键"打开该浏览器"。现在只有日志，用户不知道服务在等自己。

---

## 五、测试补齐（P1）

现有 9 个 FX 测试全是纯逻辑测试。缺口：

1. 状态快照不被阻塞（用一个只 accept 不响应的假 AdsPower socket）
2. S1 的双锁卡死回归（裸锁路径 + 队列路径并发）
3. 探针与生成互斥（S2）
4. 视频链 pacing（S5）
5. 配置多步回滚 / redo（B3）
6. 审计轮转与尾部读取（B6）
7. dedupe 缓存命中的语义与可见性（B7）
8. **fake-AdsPower + fake-Flow-DOM fixture**：本地 HTTP 服务模拟 AdsPower 的
   `/api/v1/user/list`、`/browser/start|stop`，加一份静态 HTML 复刻 Flow 关键 DOM。
   有了它，"卡点/选择器漂移"类问题才能写成可复现测试。

---

## 建议的执行批次（适合分派给 agent）

| 批次 | 内容 | 交付判据 |
|---|---|---|
| 批次 1（P0 卡点） | S1、S2、S3 | 新增死锁/阻塞回归测试全绿；生成中点"刷新积分"不再抢浏览器；状态接口在 AdsPower 挂死时仍 < 200ms 返回 |
| 批次 2（P0 失真） | B1、B2、B4、S6 | 控制台不再显示任何未探测/硬编码的假值；`ip_rotation_enabled` 反映真实配置 |
| 批次 3（调试基建） | D1、D2、D4 + 测试第 8 项 fixture | selftest 三级可跑并返回步骤耗时；失败任务能在控制台下载现场；fixture 能离线跑通 L1 |
| 批次 4（可控性） | C1、C2、C3、B3、B6 | 配置项可热改并可多步回滚；队列支持并发度/分类限流/超时踢出且重启可恢复 |
| 批次 5（长尾） | S4、S5、S7、B5、B7、B8、C4、D3、D5、D6、D7 | 逐项带测试 |

批次 1 和 2 内部相互独立，可并行；批次 3 的 fixture 是批次 4、5 的验证基础，建议先落。
