# 项目瘦身与日常维护

从项目根目录运行以下命令。Python 工具仅使用标准库；回归测试需要原有开发依赖。

## 缓存清理

```sh
python3 tools/clean_caches.py
python3 tools/clean_caches.py --apply
```

默认只预览；`--apply` 删除未跟踪的 Python 字节码、pytest 缓存和 Finder 元数据。
输出包含文件清单和逻辑字节数。虚拟环境、Git、作品、运行状态、日志、恢复备份、
创意库、任务、人工分析产物和草稿目录都被排除；符号链接也不会被跟随。
缓存会随着开发重新生成，属于正常行为。工具不会删除模型断点或业务缓存 JSON。

## 资源与回归检查

```sh
python3 tools/check_resources.py
.venv/bin/python tools/check_project.py --timeout 600
```

Windows 将 `python3` 替换成 `py -3`，将 `.venv/bin/python` 替换成
`.venv\Scripts\python.exe`。

资源检查验证页面中的本地脚本和样式存在、路径没有越出项目，并检查原始字节数、
gzip 传输字节数及脚本请求数。预算在 `tools/resource_budgets.json`，保留约 5% 字节余量。
完整检查入口会先执行资源检查，失败即退出；增加预算前先核对新增资源是否必要。
统计包含入口 HTML，不包含外部字体、动态媒体、请求头和浏览器缓存命中。

## 静态传输

`web_runtime/` 独立管理静态文件发送。支持 gzip 的客户端请求 1 KiB–2 MiB 的
HTML、CSS、JS、SVG 时，服务端在压缩确实更小时发送 gzip。小文件、媒体和 Range
请求继续发送原始字节。HEAD、条件请求和前端原有缓存控制继续生效。

压缩结果仅保存在进程内，缓存上限 8 MiB，不产生 `.gz` 构建副本。文件身份、大小与
纳秒时间戳参与缓存键，更新文件后会重新计算。无需增加 Python 依赖或前端构建工具。
正在运行的服务需在下一次正常重启后加载代码；维护工具不会擅自重启生成任务。

## 数据与 Git

`outputs/` 中的成片、`.venv/` 中的依赖，以及 `runtime/`、`.recovery/` 中的运行与
恢复资料占据主要空间。不要按文件年龄直接删除，也不要把静态调用扫描结果当成
业务代码可以删除的证明。

本次 Git 整理仅重新打包对象，未过期清理 reflog、未改写提交历史。macOS 锁定的
对象保持原样，文件锁定标记未被修改。完整结果见 [本轮记录](../reports/project-slimming-2026-09-12.md)。
