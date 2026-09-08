# 视频成片识别与槽位恢复（2026-09-06）

现场任务 `videos_c8bd4f407ea34cc589ae99ee2074fdbf`：第 15–17 段为 missing，14、18 显示成片但归属错误。

## 已确认原因

- Angular 重绘整个画布后，`data-spark-tile-id` 和 `data-original-tile-id` 消失。
- 旧 `_distinct_slices` 将长公共模板前缀当成身份；兜底还会按新卡片位置选择，可能把其他段落盖上错误标签。
- 成片卡片没有原提示词和输入参考图，只有缩略图；hover 才挂载 video。
- 新加载项目的媒体 URL 是 `/asb/...=mm,...`，会重定向到 googlevideo。页面 fetch 可能被 CORS 拦截，且初挂载 video 时 src 仍可能是缩略图 URL。

## 修复

- 仅允许能区分完整任务集合的提示词切片。提交后没有唯一身份时生成待核对 ID，不猜位置。
- missing 时在同一浏览器会话的临时读取标签页加载原项目，解析页面自身收到的项目媒体响应：核对完整提示词、项目 ID、提交时间与已知有序参考图；多候选拒绝猜测。
- 根据响应中输出媒体 ID/缩略图路径定位结果卡片，hover 后等待播放器 metadata 就绪，再取实际地址。
- 每恢复一段即下载并调用已有 video_done 链路。读取标签页关闭，不重载生成标签页，不提交生成。
- 下载支持浏览器请求上下文重定向回退，验证 HTTP 状态和 MP4 文件头，拒绝把错误页/缩略图作为视频。
- 身份未确认而超时的任务保留画布并停止该批自动重生成，避免因识别故障重复扣点。

## 现场恢复

按完整提示词核实的第 14–18 段已经恢复到原项目。旧 manifest 与旧槽位文件在 `runtime/recovery_14_18_backup`。恢复清单见 `runtime/identity_recovery_report.json`：5 段均为 360×640、约 10 秒，含视频/音频，文件接口返回 HTTP 206。

测试覆盖解析真实响应骨架、重复提示词拒绝匹配、参考图顺序、旧输出排除、missing 当轮恢复与交付、重定向下载及非 MP4 拒绝。回归命令：

```powershell
python -m pytest tests/test_flow_video_identity.py tests/test_video_streaming_delivery.py tests/test_video_media_wake.py tests/test_google_fx_chain_bugfixes.py tests/test_google_fx_video_project_lifecycle.py tests/test_video_generator.py tests/test_flow_new_ui_migration.py tests/test_runtime_version.py -q
node tests/test_video_delivery_owner.js
```

边界：媒体响应结构来自本次实测。上游结构变化或多份完全相同提示词的输出无法唯一对应时，会明确失败并保留现场，不承诺永远适配未来页面变化。
