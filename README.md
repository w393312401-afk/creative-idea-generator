# 创意点子工坊 (Creative Idea Generator)

这是一个集成了**三个视图（前端集成视图） + 一个图像子服务**的创意点子生成与管理系统。

---

## 📂 项目结构与三视图

本项目采用扁平化的前端资源结构，配合后端服务进行相对路径的分发。

### 1. 两大集成视图

| 视图入口 | 核心文件 | 说明 |
| :--- | :--- | :--- |
| **创意工坊** (`/`) | [index.html](file:///c:/Users/video/Desktop/creative-idea-generator/index.html)<br>[app.js](file:///c:/Users/video/Desktop/creative-idea-generator/app.js)<br>[js/image_studio.js](file:///c:/Users/video/Desktop/creative-idea-generator/js/image_studio.js)<br>[style.css](file:///c:/Users/video/Desktop/creative-idea-generator/style.css)<br>[image_studio.css](file:///c:/Users/video/Desktop/creative-idea-generator/image_studio.css)<br>[tokens.css](file:///c:/Users/video/Desktop/creative-idea-generator/tokens.css) | 主应用界面，含「激发维度／激发结果／图像工坊」三个顶部标签页，用于点子的生成、展示、交互，以及独立的文生图/图生图创作。 |
| **控制台** (`/console.html`) | [console.html](file:///c:/Users/video/Desktop/creative-idea-generator/console.html)<br>[console.js](file:///c:/Users/video/Desktop/creative-idea-generator/console.js)<br>[console.css](file:///c:/Users/video/Desktop/creative-idea-generator/console.css)<br>[tokens.css](file:///c:/Users/video/Desktop/creative-idea-generator/tokens.css) | 与后端深度协同的第二视图，用于系统状态监控与管理。 |

> ⚠️ **开发注意事项**：根目录下的 `index.html`, `app.js`, `style.css`, `tokens.css` 以及 `console.*` 属于核心前端资产，**请勿移动到子文件夹**，否则后端 `SimpleHTTPRequestHandler` 的静态路由与页面引用将会失效。

---

## 🏁 新机器上从零开始（Windows / macOS）

克隆下来直接双击启动脚本即可，首次运行会自动完成引导：

```
git clone https://github.com/w393312401-afk/creative-idea-generator.git
cd creative-idea-generator
```

*   **Windows**：双击 `run.bat`
*   **macOS**：双击 `run.command`

首次运行脚本会依次做四件事，之后每次启动都会跳过：

1.  **定位 Python**（Windows 依次找 `py` 启动器 → PATH 上的 `python` → 默认安装路径）。
    找不到会直接告诉你去装，而不是留一个看不懂的报错。
    需要 **Python 3.11+**，Windows 安装时务必勾选 *Add Python to PATH*。
2.  **创建 `.venv` 并安装依赖**（`pip install -r requirements.txt`，首次约 1–3 分钟）。
3.  **生成 `server_config.json`**（由 `tools/bootstrap_config.py` 从模板生成，
    会把模板里那几项中文占位说明清成空值——直接拷模板会让 `accessCode` 变成
    一句非空的说明文字，等于给界面上了一把没人知道口令的锁）。
4.  **启动前自检**：先用带控制台的 `python.exe` 试跑一次导入，依赖损坏 / 语法错误
    在这里就会打印出人话；通过后才用 `pythonw.exe` 后台起服务。

跑完这一步，`http://127.0.0.1:8085/` 已经可以打开，界面、画廊、台账、已有 `outputs/`
素材都能正常浏览。

**还需要你自己补的两样**（不补也能启动，只是对应功能不可用）：

| 要补的东西 | 不补的后果 | 怎么补 |
|---|---|---|
| `server_config.json` 里的 `apiKey`（以及 `baseUrl` 指向你的 LLM 网关） | 无法「激发创意」与合成提示词 | 记事本打开填好，重启服务 |
| 技能包 `gemini-veo-restoration-composer` | 提示词合成按空契约降级（创意维度变窄、一致性约束消失），启动日志与前端横幅会喊出缺失清单 | 见下方「技能包路径（skillDir）」 |

Google FX 的视频生成还需要本机装好 AdsPower，见下方说明；不装不影响前面所有功能。

---

## 🚀 服务管理

根目录下提供了统一的批处理脚本来运行与管理服务：

*   **启动与管理服务**：双击运行 [run.bat](file:///c:/Users/video/Desktop/creative-idea-generator/run.bat)
    *   **服务未启动时**：自动创建 `outputs/` 目录，在后台以 `8085` 端口启动 Python 服务，并自动拉起浏览器访问。
    *   **服务已运行时**：自动弹出交互菜单，提供以下选项：
        1.  **停止服务**（安全终止后台的 `server.py` 进程）
        2.  **重启服务**
        3.  **打开网页**（重新拉起浏览器页面）
        4.  **退出**

*   **停止服务**：双击运行 [stop.bat](file:///c:/Users/video/Desktop/creative-idea-generator/stop.bat)（一次清掉 8085 与所有残留 8086 监听进程）。

*   **macOS**：双击 [run.command](run.command)（不要双击 `run.sh`——Finder 对 `.sh` 后缀默认关联文本编辑器而不是终端，双击只会打开编辑器；`run.command` 是转调 `run.sh` 的薄封装，同一套启动/停止/重启交互菜单）。也可以在终端里直接 `./run.sh` 运行。

*   📌 **端口永久固定（2026-07-04）**：服务入口只有一个 —— **`http://127.0.0.1:8085/`**。
    *   图像服务站已完全并入创意工坊单页应用（顶部「🎨 图像工坊」标签页），不再是独立路由/独立目录，`run.bat` 也不再拉起任何 8086 子服务。
    *   LLM 代理固定 `8046`（`server_config.json` 的 `baseUrl` 为准）；`gpt-5.5` 由服务端 `resolve_gateway` 固定路由到 codex 代理，前端无需也无法再切换端口（设置面板的「GPT 代理端口」选择器已移除）。

---

## ⚙️ 配置说明

*   **Python 依赖**：`run.bat` / `run.sh` 首次运行会自动建 `.venv` 并安装；手动装是 `pip install -r requirements.txt`（包含 Pillow、Playwright、Pydantic 等）。
*   **配置文件**：`server_config.json`（实际运行配置，已加入 `.gitignore` 避免密钥泄露）
*   **配置模板**：[server_config.example.json](file:///c:/Users/video/Desktop/creative-idea-generator/server_config.example.json)
    *   包含 API 密钥、访问密码以及各类服务端参数配置。首次运行时由 `tools/bootstrap_config.py` 自动生成一份（占位说明会被清成空值），你只需要补 `apiKey`。
    *   `skillProfile` / `skillProfiles`：做哪个模型的提示词就读哪个技能包（两个包已随仓库内置，通常不用配；见下方「技能包与 profile」）。`skillDir` 是 base 包路径的历史别名。
    *   `adsPowerPort`：AdsPower 本地 API 端口（默认 `50325`）。Google FX 运行时已内置在 `integrations/google_fx/`，换机不再需要配置外部源码路径。
    *   `accessCode` 设置后，除 `/api/mode` 外的全部 API（含任务/日志/清单读取）都需要访问码；静态路由永不吐出配置、日志、任务与服务端源码。

### 技能包与 profile（做哪个模型的提示词，就读哪个包）

两个技能包已随代码放进仓库的 [skills/](skills/)，不配任何东西也能跑：

| profile | 技能包 | 面向的视频模型 | 契约文件 |
| --- | --- | --- | --- |
| `base` | `skills/gemini-veo-restoration-composer` | Veo 系列 / 通用 | 8 个 |
| `omni` | `skills/gemini-omni-restoration-composer` | Gemini Omni / Omni Flash（强制多镜头组接） | 12 个 |

激发创意与提示词合成都要**现读**这些契约文件（`SKILL.md` 与 `references/` 下的形态矩阵、提示词模板、一致性协议、空间工序表等）。**这些文件缺失不会报错**，只会让合成按空契约降级——创意维度变窄、模板与一致性约束整段消失。所以启动日志与前端横幅都会把缺失清单喊出来，**两个 profile 一次报全**（只报当前激活的那个，等于把「另一个包没装好」留到切模型那一刻才炸）。

**选哪个包**：UI 入口在**激发按钮上方的「提示词链路」芯片组**（排在 LLM 模型之前——链路决定提示词写成什么样，模型只决定谁来写），三选一：`自动` / `Veo · 单镜延时`(base) / `Omni · 主镜加特写`(omni)。停在「自动」时徽标会直接显示当前实际走哪条（规则表由 `/api/mode` 的 `skill_profile_rules` 下发，前端只按表匹配、不自带一份 `omni` 判断）。

`skillProfile` 默认 `auto`，按 `videoModel` 推断——模型名里含 `omni` 走 omni 包，其余走 base 包。也可以在配置文件里钉死：

```json
{ "skillProfile": "omni" }
```

钉死的用处是把两件事解耦：只想换渲染档位的人不该被顺手改掉提示词语法，反过来只想换提示词语法的人也不该被迫去改视频模型下拉框。取值优先级：环境变量 `SKILL_PROFILE` > `skillProfile` > 按 `videoModel` 推断。

**两条按 profile 分 / 不分的线**：形态矩阵 `idea-engine.md` 按 profile 走（两个包各带一份）；历史选题台账**全局共享一份**，落在 `runtime/used-topic-ledger.md`（首次使用从技能包里的种子整份拷贝，历史不丢）。台账不按 profile 分裂是刻意的——同一个选题换个分镜语法重做一遍不是新选题，去重记忆劈成两半等于没有。技能包 `references/` 里那份从此只是**只读种子**，运行时不再往包内追加（否则技能包进了 git 就会每合成一次脏一次）。

**改包路径**（换机部署、或想指回自己在 `~/.codex` 下那份工作副本时）：

```json
{ "skillProfiles": { "base": "~/.codex/skills/gemini-veo-restoration-composer" } }
```

*   支持 `~`、环境变量，以及相对本项目根目录的相对路径。`skillDir` 是 base 的历史别名，等价于 `skillProfiles.base`。
*   **改完不用重启**：服务会在配置文件 mtime 变化时重算，下一次「激发创意」/合成即按新路径读取。
*   取值优先级：环境变量（`SKILL_DIR` / `SKILL_DIR_OMNI`）> `skillProfiles.<profile>` / `skillDir` > 仓库内置 `skills/<包名>` > 旧默认 `~/.codex/skills/<包名>` > 自动探测。
*   自动探测只在前面几项都没有契约文件时兜底：扫 `~/.codex/skills`、`~/.claude/skills`、`~/.agents/skills`、`<项目>/skills` 的一级子目录，挑该 profile 契约命中最多（≥2 个）的那个包。
*   显式配了路径就绝不再自动探测：路径写错要在启动日志里看得见地报缺失，而不是被悄悄换成另一个技能包。
*   omni 包里没有 base 的 `prompt-templates.md` 这类文件，切到 omni 时这些读取会**回落到 base 包**（并在日志里说明），避免切个视频模型就把现有合成链路整段读空。

### 合成实现按 profile 分派（composer）

提示词合成是两段式：`compose_anchor_and_packet`（Phase 1：brief / 工序梯 / Drift Lock 包 / IMAGE 1）+ `compose_remaining_beats`（Phase 2：逐拍撰写与装配），中间插一道首帧验收门。**只有 Phase 2 按 profile 分派**，实现在 [prompt_pipeline/composers/](prompt_pipeline/composers/)：`get_composer(profile)` 给出实现，未知 profile 一律回落 base。

| | base | omni |
| --- | --- | --- |
| VIDEO | 一条连续的施工延时 | 剪辑过的多镜头序列：一条贯穿全段的主工作镜，被特写插入切开，再切回同一机位收尾（8/10s=四镜，含两个插入；4/6s=三镜，含一个插入），正文带一句把切点钉到秒的**时间线句**，默认 UGC 手机拍摄质感（可用光、轻微过曝、压缩噪点、不稳定构图），**无例外**禁止 one-take / oner / single continuous take 一类措辞 |
| IMAGE、Phase 1、槽位格式、断点续传 | —— 全 profile 同一套，omni 一律委托 base，不存在第二份实现 —— | |

omni 侧只覆写四类钩子：撰写指令（在 base 那份 system prompt 之后追加一段 OMNI VIDEO OVERRIDE，IMAGE 规则原样保留）、VIDEO 的确定性修复（按镜头数缩放的字数预算、多镜头版节奏声明、切点时间线的确定性注入与覆写、数字折成英文单词）、审计（主镜/插入/切回镜缺失或乱序、写出旧轮换梯的景别名、时间线缺失或与片长不符、一镜到底措辞算**结构性硬伤**，接进既有的「校验 → 定向回炉一轮 → 审核面板留痕」通路；记号类瑕疵只留痕不回炉）、占位符兜底稿的收尾。必读契约按 `SKILL.md §Required Reference Loading` 声明的 7 个走 `load_reference_file(name, 'omni')`，过门契约按需加载。

**主镜 + 特写插入，切点写进提示词**（2026-08-01 起，2026-08-09 改成现在这套）。此前是一条五到六级的景别轮换梯，两个问题：六个景别塞进 4 秒等于每镜 0.67 秒，观感是闪帧；而且尺度一路换下去，首尾帧锚的连续性只能靠文字反复申明来兜。现在按实拍剪法来：

*   **一条贯穿全段的主工作镜，被一到两个特写插入切开，再切回同一机位收尾**（8/10s 四镜 = 主镜 + 近景插入 + 特写插入 + 切回；4/6s 三镜 = 主镜 + 近景插入 + 切回。约束是主镜与切回镜各 ≥1.3 秒、插入镜 ≥0.9 秒）。片长变长买到的是第二个插入和更长的主镜，不是又一级景别。**第一镜与最后一镜是同一个机位**，于是首帧锚与尾帧锚天然落在同一构图上。推进量全部由主镜携带，插入镜零推进，剩余重复动作在切回那个剪辑点做 same-way 压缩。旧梯的 establishing long / full / medium / wide outro 一律按梯外景别报错。过门桥拍与最终兑现拍仍走各自的三工位梯。
*   **切点用一句时间线钉在秒上**（`Cut this ten-second clip on these marks and hold no other cuts — a wide working shot from 0.0 to 3.2, ...`），由 composer 确定性注入并覆写模型自编的版本。这是正文里**唯一**允许出现阿拉伯数字的地方（技能包 §Notation Ban 为此开了一条 Timecode exemption），其余计数一律折成英文单词。
*   **时长不再允许"沿用面板当前时长"**：提示词按 N 秒排切点、生成时却用了面板上残留的另一个时长，切点表当场作废。合成端与生成端统一走 `server_common.resolve_video_duration()`，默认 10s（主镜够长、且排得下第二个特写插入的长度）。
*   顺带清掉两处与 base 契约的硬冲突：base 的 even-rate 句（"每一刻都在推进、不许把改动推迟后一次兑现"）与镜头级进度锁直接对撞，换成约束到**镜内**的连续性声明；base 的 out-and-in 兜底会塞进按 8 秒写死的时间戳与 Grid 记号，omni 下整条跳过。

**断点续传指纹含 profile**：`get_brief_fingerprint(dimensions, profile)` 把 profile 一起哈希（`packet_cache_key` 由它派生，自动跟着分家）。一单在 base 下合成到一半、把视频模型切成 Omni Flash 再点合成，会**重排而不是续传**——否则命中旧断点续出来的是半 base 半 omni 的混合提示词集。代价是这次改动让**存量断点存档整体失效**，未完成的单子重试时从 Phase 1 重跑；已交付的整单不受影响（交付即清档）。

### 合成节拍输入

`/api/compose` 的 `dimensions` 支持 `beat_count_mode`：

- `adaptive`（默认）：`beats_count` 是施工里程碑上限；规划器会删除局部填充拍，实际拍数以返回槽位数为准。
- `fixed`：`beats_count` 保持精确施工拍数；仍须通过显著里程碑门禁，不会用弱变化静默凑数。

每个普通施工拍都生成一个完整、可命名的阶段成果，并在 VIDEO 中同时声明主体增长与物料/库存变化两条进度线。

### Google FX 内置运行时

Google FX 的图片、视频、积分探测、浏览器控制与号池运行时代码已收进
`integrations/google_fx/`。项目不再依赖 `N8N-main/Adspower/AI/core`，也不再通过
`adspowerPath` 修改 Python 导入路径。AdsPower 桌面应用、浏览器 profile 和登录会话仍然
保持外置；换机时安装 AdsPower，并在 `server_config.json` 确认 `adsPowerPort` 即可。

底层 FX 环境变量及号池状态放在 Git 忽略的 `runtime/` 目录。代码边界和维护规则详见
`integrations/google_fx/README.md`。

控制台的「Google FX 管理」页提供运行时/AdsPower 健康状态、模型摘要、账号池维护、
积分刷新、冷却解除、任务取消与选择器漂移诊断；它不会显示或编辑密码、Cookie 和 `.env`。
所有帧、视频、分步渲染和自动管线统一进入可观测的浏览器队列，并支持调整等待任务优先级：

- `暂停`：拒绝新任务，同时暂停等待队列调度；当前已运行任务不被强杀。
- `排空`：拒绝新任务，但继续执行现有等待任务，适合维护前收尾。
- `恢复`：重新接收并调度任务。

页面中的「运行配置」只开放 AdsPower 端口、图片/视频模型、Omni 时长、换号节拍和最低
积分等非敏感白名单字段。修改会原子写入 `server_config.json`、立即热生效并记录到
`runtime/fx_audit.jsonl`，也可一键回滚最近一次修改。

---

## 📄 项目文档

所有辅助及说明文档均已收纳至 [docs/](file:///c:/Users/video/Desktop/creative-idea-generator/docs/) 文件夹中：

1.  [部署指南 (external_deploy_guide.md)](file:///c:/Users/video/Desktop/creative-idea-generator/docs/external_deploy_guide.md) — 介绍如何将服务部署到外部服务器。
2.  [外网访问指南 (public_access_guide.md)](file:///c:/Users/video/Desktop/creative-idea-generator/docs/public_access_guide.md) — 介绍如何配置内网穿透或外网访问。
3.  [图像生成指南 (image_generation_guide.md)](file:///c:/Users/video/Desktop/creative-idea-generator/docs/image_generation_guide.md) — 图像服务站及 AI 绘图相关的参数与使用说明。
