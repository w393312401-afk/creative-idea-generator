# Google FX 内置运行时

这里保存 SPARK 实际使用的 Google Labs Flow / AdsPower 浏览器自动化代码。它从原
`N8N-main/Adspower/AI/core` 项目按依赖闭包迁入，并已改成
`integrations.google_fx` 命名空间，主项目不再修改 `sys.path` 或读取外部源码目录。

## 边界

内置内容：

- Flow 图片、视频和积分探测服务；
- AdsPower CDP 连接、UI 选择器、取消传播和节奏控制；
- 账号池及失败换号逻辑；
- FX 请求模型和运行时配置。

仍然外置：

- AdsPower 桌面应用、浏览器 profile 与登录会话；
- N8N 工作流、独立 Web 服务/dashboard；
- Notion、云雾生图、FFmpeg 等 SPARK 未调用的服务。

## 配置与状态

- AdsPower 本地 API 端口由 `server_config.json` 的 `adsPowerPort` 控制，默认 `50325`。
- UI 账号由 SPARK 的 `googleFxUserId` 或号池选择结果控制。
- 可选的底层环境变量保存在 `runtime/google_fx.env`；该目录已被 Git 忽略。
- 号池、选择器统计等可变状态统一保存在项目根目录的 `runtime/`。

## 维护约定

FX 代码应直接在本目录维护，不要再向旧项目双写。包内导入必须使用相对导入，避免产生
`config`、`models`、`utils`、`services` 等顶层模块冲突。相关纯逻辑回归测试使用
`tests/test_google_fx_*.py`。
