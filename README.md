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
| 技能包 `restoration-prompt-composer` | 提示词合成按空契约降级（创意维度变窄、一致性约束消失），启动日志与前端横幅会喊出缺失清单 | 见下方「技能包路径（skillDir）」 |

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
    *   `skillDir`：技能包（`restoration-prompt-composer`）的本地目录，换机部署时最容易漏配的一项（见下方「技能包路径」）。
    *   `adsPowerPort`：AdsPower 本地 API 端口（默认 `50325`）。Google FX 运行时已内置在 `integrations/google_fx/`，换机不再需要配置外部源码路径。
    *   `accessCode` 设置后，除 `/api/mode` 外的全部 API（含任务/日志/清单读取）都需要访问码；静态路由永不吐出配置、日志、任务与服务端源码。

### 技能包路径（skillDir）

激发创意与提示词合成都要现读技能包里的 8 个契约文件（`SKILL.md` 与 `references/` 下的形态矩阵 `idea-engine.md`、提示词模板 `prompt-templates.md`、历史选题台账 `used-topic-ledger.md`、三份一致性协议、空间工序表）。**这些文件缺失不会报错**，只会让合成按空契约降级——创意维度变窄、模板与一致性约束整段消失。所以启动日志与前端横幅都会把缺失清单喊出来。

在 `server_config.json` 里指定路径即可：

```json
{ "skillDir": "~/.codex/skills/restoration-prompt-composer" }
```

*   支持 `~`、环境变量，以及相对本项目根目录的相对路径（如 `skills/restoration-prompt-composer`）。
*   **改完不用重启**：服务会在配置文件 mtime 变化时重算，下一次「激发创意」/合成即按新路径读取。
*   取值优先级：环境变量 `SKILL_DIR` > `skillDir` > 内置默认 `~/.codex/skills/restoration-prompt-composer` > 自动探测。
*   自动探测只在前三项都没有契约文件时兜底：扫 `~/.codex/skills`、`~/.claude/skills`、`~/.agents/skills`、`<项目>/skills` 的一级子目录，挑契约命中最多（≥2 个）的那个包——技能包被改名或装到别的 agent 目录时能自动捡回来。
*   显式配了 `skillDir`/`SKILL_DIR` 就绝不再自动探测：路径写错要在启动日志里看得见地报缺失，而不是被悄悄换成另一个技能包。

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
