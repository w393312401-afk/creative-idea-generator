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

*   📌 **端口永久固定（2026-07-04）**：服务入口只有一个 —— **`http://127.0.0.1:8085/`**。
    *   图像服务站已完全并入创意工坊单页应用（顶部「🎨 图像工坊」标签页），不再是独立路由/独立目录，`run.bat` 也不再拉起任何 8086 子服务。
    *   LLM 代理固定 `8046`（`server_config.json` 的 `baseUrl` 为准）；`gpt-5.5` 由服务端 `resolve_gateway` 固定路由到 codex 代理，前端无需也无法再切换端口（设置面板的「GPT 代理端口」选择器已移除）。

---

## ⚙️ 配置说明

*   **Python 依赖**：首次部署请先执行 `pip install -r requirements.txt`（Pillow / requests）。
*   **配置文件**：`server_config.json`（实际运行配置，已加入 `.gitignore` 避免密钥泄露）
*   **配置模板**：[server_config.example.json](file:///c:/Users/video/Desktop/creative-idea-generator/server_config.example.json)
    *   包含 API 密钥、访问密码以及各类服务端参数配置。首次部署时请参考模板新建 `server_config.json` / [server_config.json](file:///c:/Users/video/Desktop/creative-idea-generator/server_config.json)。
    *   `adspowerPath`：AdsPower 自动化脚本目录（google_fx 帧序列/视频生成依赖），换机部署时必须改成本机路径。
    *   `accessCode` 设置后，除 `/api/mode` 外的全部 API（含任务/日志/清单读取）都需要访问码；静态路由永不吐出配置、日志、任务与服务端源码。

---

## 📄 项目文档

所有辅助及说明文档均已收纳至 [docs/](file:///c:/Users/video/Desktop/creative-idea-generator/docs/) 文件夹中：

1.  [部署指南 (external_deploy_guide.md)](file:///c:/Users/video/Desktop/creative-idea-generator/docs/external_deploy_guide.md) — 介绍如何将服务部署到外部服务器。
2.  [外网访问指南 (public_access_guide.md)](file:///c:/Users/video/Desktop/creative-idea-generator/docs/public_access_guide.md) — 介绍如何配置内网穿透或外网访问。
3.  [图像生成指南 (image_generation_guide.md)](file:///c:/Users/video/Desktop/creative-idea-generator/docs/image_generation_guide.md) — 图像服务站及 AI 绘图相关的参数与使用说明。
