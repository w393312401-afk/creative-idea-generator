# 创意点子工坊 (Creative Idea Generator)

这是一个集成了**三个视图（前端集成视图） + 一个图像子服务**的创意点子生成与管理系统。

---

## 📂 项目结构与三视图

本项目采用扁平化的前端资源结构，配合后端服务进行相对路径的分发。

### 1. 三大集成视图

| 视图入口 | 核心文件 | 说明 |
| :--- | :--- | :--- |
| **创意工坊** (`/`) | [index.html](file:///c:/Users/video/Desktop/creative-idea-generator/index.html)<br>[app.js](file:///c:/Users/video/Desktop/creative-idea-generator/app.js)<br>[style.css](file:///c:/Users/video/Desktop/creative-idea-generator/style.css)<br>[tokens.css](file:///c:/Users/video/Desktop/creative-idea-generator/tokens.css) | 主应用界面，用于点子的生成、展示与交互。 |
| **控制台** (`/console.html`) | [console.html](file:///c:/Users/video/Desktop/creative-idea-generator/console.html)<br>[console.js](file:///c:/Users/video/Desktop/creative-idea-generator/console.js)<br>[console.css](file:///c:/Users/video/Desktop/creative-idea-generator/console.css)<br>[tokens.css](file:///c:/Users/video/Desktop/creative-idea-generator/tokens.css) | 与后端深度协同的第二视图，用于系统状态监控与管理。 |
| **图像服务站** (`/image-service-station/`) | [image-service-station/](file:///c:/Users/video/Desktop/creative-idea-generator/image-service-station/) 内的网页资产 | 独立的图像子服务，已整合至主系统中。 |

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

*   ⚠️ **图像子服务说明（勿单独启动）**：图像服务站已完美整合进主服务（通过 `http://127.0.0.1:8085/image-service-station/` 访问）。**平时请勿单独运行 `image-service-station/run.bat`（**勿单独启动**）**。为了避免端口冲突，该独立子服务的默认端口已修改为 `8086`，仅作为备用开发/调试使用。

---

## ⚙️ 配置说明

*   **配置文件**：`server_config.json`（实际运行配置，已加入 `.gitignore` 避免密钥泄露）
*   **配置模板**：[server_config.example.json](file:///c:/Users/video/Desktop/creative-idea-generator/server_config.example.json)
    *   包含 API 密钥、访问密码以及各类服务端参数配置。首次部署时请参考模板新建 `server_config.json` / [server_config.json](file:///c:/Users/video/Desktop/creative-idea-generator/server_config.json)。

---

## 📄 项目文档

所有辅助及说明文档均已收纳至 [docs/](file:///c:/Users/video/Desktop/creative-idea-generator/docs/) 文件夹中：

1.  [部署指南 (external_deploy_guide.md)](file:///c:/Users/video/Desktop/creative-idea-generator/docs/external_deploy_guide.md) — 介绍如何将服务部署到外部服务器。
2.  [外网访问指南 (public_access_guide.md)](file:///c:/Users/video/Desktop/creative-idea-generator/docs/public_access_guide.md) — 介绍如何配置内网穿透或外网访问。
3.  [图像生成指南 (image_generation_guide.md)](file:///c:/Users/video/Desktop/creative-idea-generator/docs/image_generation_guide.md) — 图像服务站及 AI 绘图相关的参数与使用说明。
