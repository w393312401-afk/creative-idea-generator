# 瘦身执行记录

## 范围与阶段门

以视频复刻与提示词导入为核心；保持 Windows/macOS、既有 API 与三种 profile
兼容。保护用户作品、源素材、账号状态、凭据、选题台账、断点和恢复快照。

执行顺序为 P0 安全基线 → P1 界面减法 → P2 模块拆解 → P3 兼容治理 →
P4 缓存治理 → P5 持续检查。前一阶段未验收，不叠加后一阶段。
未通过的既有断言与离线环境限制必须分别记录，不能通过删除测试或放开生产访问来变绿。

## P0 工具

- `tools/project_audit.py`：只读扫描，输出提交号、在途文件清单、源码规模与哈希、
  导入关系、API 字面量清单、动态契约引用、轮询位置、页面资源及目录字节数。
  API 字面量不是完整路由证明，静态调用关系不能单独用作死代码删除依据。
- `tools/check_project.py`：在临时源码副本运行离线测试；包含未提交源码，不复制
  `.git`、虚拟环境、生产配置、用户 JSON、作品、runtime、恢复备份或 artifacts。
  副本使用现有首次启动脚本从公开模板生成去除占位密钥的测试配置，不复制本机配置。
  每次打印独立报告目录，保留源码快照、`baseline.json`、逐项日志与 `results.json`。
  Python 另输出 JUnit XML；超时返回失败，不宣称全套通过。
- Python 禁止真实网络和原生子进程，并将依赖这些能力的测试显式标为
  `OFFLINE_INTEGRATION_REQUIRED`；副本外写入仍为硬错误。
- JavaScript 使用 Node 权限模式禁止写入、子进程和 worker，同时预加载网络拦截。
  这些是可信测试的防误操作措施，不是运行恶意代码的 OS 安全沙箱。
- `requirements-dev.txt` 单列 pytest。运行服务仍只需原来的运行依赖，Node 仅用于测试。

macOS/Linux（从项目根运行）：

```sh
.venv/bin/python tools/project_audit.py
.venv/bin/python tools/check_project.py --timeout 600
.venv/bin/python tools/check_project.py --js-only
.venv/bin/python tools/check_project.py --python-only --maxfail 1
.venv/bin/python tools/check_project.py --python-only tests/test_slimming_harness.py
```

Windows 使用 `.venv\Scripts\python.exe` 替换 `.venv/bin/python`。
JavaScript 检查需要支持 `--permission` 的 Node（本机验证为 24.11.0）。
未安装 Node 会将 JS 检查记为失败，而不是默默跳过。

不在检查过程中安装依赖、重启服务、修改账号或触发模型生成。
临时快照只含源码和测试产生的文件；本轮没有自动清理这些报告，以便复核。

## 初始规模（2026-09-12）

基准提交：`5504ce525caf32237eb057475bceb313f1c313b1`。
开始时已有多项视频身份恢复、媒体缓存和 Omni 改动，快照保留了这些在途源码。

| 项目 | 初始值 |
| --- | ---: |
| `prompt_pipeline/__init__.py` | 20,635 行 |
| `server.py` | 9,131 行 |
| `server_common.py` | 6,103 行 |
| `app.js` | 5,479 行 |
| `console.js` | 2,203 行 |
| 主页面本地链接资源 | 2,185,257 字节 / 30 个脚本请求 |
| 控制台本地链接资源 | 312,813 字节 / 5 个脚本请求 |
| outputs | 925,852,020 字节 |
| runtime | 219,020,210 字节 |
| logs | 33,988,769 字节 |
| 恢复快照 | 92,743,273 字节 |

资源字节数是未压缩的本地链接资源之和，不含 HTML、远程字体、动态媒体与浏览器缓存效应。
磁盘值是逻辑文件字节数，与 `du` 的分配块占用不同，不能混作清理收益。
运行接口延迟、真实浏览器资源加载和 Windows 验收尚未测量。

## 已修正的测试隔离问题

`test_get_ads_ws_url_reuses_running_browser_on_conn_error` 原先只模拟了 glob 结果，
仍依赖真实 AdsPower 缓存根存在；现改用临时根，并模拟 macOS 焦点/窗口操作。
未修改 AdsPower 的业务实现。

初版隔离运行发现原生 FFmpeg 被拦截后，业务代码吞掉异常，测试表现为拼图返回 None。
现使用 pytest 明确的集成验收跳过结果，防止把环境约束误报成业务回归。
macOS 临时路径已统一解析真实路径，避免 Node 因 `/var` 符号链接拒绝加载测试。

## 后续阶段（尚未实施）

- P1：移除独立原创 UI、模型展示页与沙盒，保留 API 文档、实际配置、联网参考和 FX。
- P2：按底层能力、领域逻辑、编排、HTTP 的顺序拆解；先厘清可变状态和测试替换点。
- P3：旧原创 API 作为兼容实现保留；契约注册与旧存档消费者逐项核对。
- P4：只读维护清单优先；缓存清理必须登记、过期、无活动引用且路径安全。
- P5：将现有检查入口扩展到资源引用与依赖边界，再建立体积和复杂度预算。

本轮 P0 不计算体积节省、不做 Git 压缩、不删除素材，不提交任何用户在途改动。

## 首轮阶段门结果：P0 未通过，P1–P5 暂停（历史记录）

截至本轮末，业务源码与本轮开始时的快照逐文件比较一致；唯一修改的既有文件是
上述 AdsPower 测试夹具。新增的是基线工具、安全测试、开发依赖清单和本记录。

验证结果：

- 原有 37 个 JavaScript 测试文件全部通过；新增网络保护测试也通过，共 38 个文件。
- Python 广域检查启用 `--maxfail 1`：1,815 passed、130 skipped、1 failed，另有
  128 个 subtest 通过；在约 47% 处停止，**不是完整套件通过**。
- 跳过项中 129 项触发了网络/原生子进程限制，1 项仅适用 Windows。跳过并不代表验收。
- 最终入口聚焦复测：11 项保护测试与 2 项 AdsPower 测试共 13 passed、0 skipped。
- `git diff --check` 通过。

确定性阻断项：

```text
tests/test_model_resolution.py::TestModelResolution::test_default_model_in_effective_config
AssertionError: None != 'gemini-3.8-flash-high'
```

公开模板经过原有 bootstrap 清除密钥占位符后，`SERVER_MANAGED` 为 false。
`effective_config` 的非托管分支只继承部分服务端字段，不补默认 `model`；
这个测试却无条件断言默认模型存在。独立运行同一文件也复现，排除了只由套件顺序引起的解释。
该测试文件在本轮开始时已有用户修改，因此本轮没有改断言、强行注入假密钥或修改业务语义。

下一步先区分并补齐托管/非托管两种配置模式的测试夹具与契约：如果默认模型只应在托管
模式补齐，现有测试须显式设置该模式；若两种模式都应补齐，则属于需要单独验证的配置
行为修复。处理后重新跑 P0，再进入已批准的 P1 功能精简。

最小复现：

```sh
.venv/bin/python tools/check_project.py --python-only --maxfail 1 tests/test_model_resolution.py
```

本机原始报告（临时目录，系统可能日后清理）：

- 最终工具复测（13 项 Python + 38 个 JS 文件）：`/private/var/folders/b7/xqdnyy5j4d1gc1xdlbv9d3p00000gn/T/spark-offline-hza9pm8e`
- Python 广域：`/private/var/folders/b7/xqdnyy5j4d1gc1xdlbv9d3p00000gn/T/spark-offline-gma4d5cr`
- Python 独立复现：`/private/var/folders/b7/xqdnyy5j4d1gc1xdlbv9d3p00000gn/T/spark-offline-0__pbm_0`

## 第二轮：配置模式隔离

继续执行时核对了 `effective_config` 的两条既有分支：托管模式注入默认模型并升级旧模型；
非托管模式保留客户端值，不注入默认模型。本轮保持这两种生产行为不变。
相关测试现在显式 patch 托管模式、客户端模型权限和内存配置，并新增非托管契约测试。
配置模板断言改为读取公开模板，不依赖本机 `server_config.json` 或实际密钥。
原有新模型与图像参数断言完整保留。聚焦验证：14 passed、0 skipped。

## 当前进度：P0 离线基线通过，P1 首批完成，P2 开始

任务重试测试另有两个夹具问题：仅 chdir 却未覆盖实际的 `TASKS_DIR`。
已显式绑定临时任务目录，保留存盘、重试覆盖与运行中拒绝重入断言。
随后完整离线回归：**3,799 passed、233 skipped、182 subtests passed**，无失败。
跳过的原生/联网/Windows 场景仍须独立验收，不等于端到端全量通过。

### P1 已完成

- 控制台移除模型展示、筛选、后缀生成器、请求沙盒、上传预览及对应入口。
  API 文档、模型运行配置和 FX 运维保留；共享移动导航、主题与文档样式保留。
- `console.js` 从 2,203 行降到 863 行；控制台本地链接资源从 312,813 降到
  237,181 字节，减少 75,632 字节（约 24.2%，未压缩，不含 HTML）。
- “激发结果”改为“创作结果”，没有当前作品、恢复 ID 或运行任务的启动进入项目工作台。
  已恢复内容与活跃任务继续走原有路径。
- 联网参考设置被复刻模块消费，因此保留键名与功能，只更新过时的随机激发专用文案。
  原创 UI 的主调用链此前已移除，本轮不恢复；后台 `/api/ideate` 不变。
- 控制台状态轮询避免重叠，隐藏页面不发新轮询。文档选中面板改按导航状态定位，
  修复 `display:none` 与 `display: none` 字符串差异造成的错误代码片段渲染。
- 新增启动/恢复、退役入口、防重复请求、后台轮询的 JS 契约检查；新增独立
  Playwright 验收，拦截所有请求、只返回本地源码与测试 JSON，不连接应用或 AdsPower。
  已通过桌面文档切换、旧 hash 回退、移动端导航和无 pageerror 验证。

### P2 首个兼容边界

新增无依赖的 `legacy_media_contract.py`，提示词与视频槽位规划共享旧硬切占位识别。
两侧原有公共/私有导入名保留；旧正文识别与 `[CUT]` 标签含义不变。
新增单一函数身份、叶子模块无导入及正反例测试。相关回归：161 passed、1 skipped，
另有 7 subtests passed；跳过项为原生子进程测试。

### 仍未完成

大模块拆解、composer 反向依赖解除、星号导入治理、旧原创后台隔离、
生命周期清理工具、持续预算和 Windows 实机验收尚未完成。
主页面资源尚未减少（当前约 2,185,590 字节）；本轮没有声称完成全项目瘦身。
没有删除作品、账号状态、恢复数据，也没有重启服务、提交用户在途修改或改写 Git 历史。

## 后续：全项目空间与传输优化（2026-09-12）

目录归档后继续完成 Git 对象重新打包、可重建缓存清理、静态 HTTP 模块拆出、
gzip 协商与有限内存缓存，并将页面资源与体积预算接入统一检查入口。
本轮清理量及验证证据见 [全项目瘦身结果](project-slimming-2026-09-12.md)，
日常操作见 [维护指南](../guides/project_maintenance.md)。

这覆盖 P2 的静态传输边界、P4 的可重建缓存和 P5 的前端资源预算。
大型领域模块与旧 API 的进一步拆解仍须按原有契约逐项进行；业务缓存和作品的
生命周期删除不在可重建缓存清理工具范围内。
