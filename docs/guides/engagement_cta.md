# 成片引导动画（关注、点赞、评论、收藏、分享）

9:16 透明叠加动画：手势依次点击关注 → 点赞 → 评论 → 收藏 → 分享并点亮图标（点赞变红、收藏变黄、关注徽标转成 ✓ 后收起）。默认没有图标下方文字、提示气泡或标题。头像框只有白色圆框，框内透明。默认时长 5 秒，首尾透明。

位置按 TikTok 预览截图中的视频区域校准（不含手机状态栏和底部发布区域）：1080×1920 画布上互动栏中线为 x=1000，头像中心 y=1128，点赞、评论、收藏、分享中心分别为 y=1278、1410、1542、1674。头像到点赞相距 150 像素，四个动作相距 132 像素。可选文字不影响图标的定位。

## 精剪成片自动烧录

默认开启：Codex 精剪完成并通过核验后，把引导动画烧录到成片**最后 5 秒**，再发布为精剪结果。精剪结果里会注明「已烧录片尾引导」，可直接下载带引导的成片，也可以点「下载无引导版」取回烧录前的版本。两个文件都在该任务的 `codex_edits/<任务>/work/` 目录下，带引导的文件名以 `_cta.mp4` 结尾。

在 **配置中心 → 成片引导** 调整：

| 设置 | 说明 |
| --- | --- |
| 自动烧录 | 关闭后精剪成片保持原样 |
| 片尾时长 | 最后 5 / 6 / 7 / 8 / 10 秒；成片比这更短时从开头播放 |
| 引导视频 | 内置无文字互动动画，或上传的自定义透明视频 |
| 自定义视频 | 上传带透明通道的 ProRes 4444（含 Alpha）MOV 或 VP9 透明 WebM，60 秒、300 MB 以内；没有透明通道的视频会被拒绝 |

设置保存在运行项目的电脑上（`outputs/engagement_cta/.settings.json`），在任何设备上修改都对之后**完成**的精剪生效；正在进行的精剪在烧录那一刻读取当时的设置。预览窗里的深色格子表示透明区域。

- 内置动画按片尾时长渲染，每种时长首次使用时需要约 10 秒（需要 Playwright 自带的 Chromium），之后复用 `outputs/engagement_cta/.cache/`。
- 自定义视频从片尾时长的起点开始播放：比片尾时长长的部分在成片结尾被截掉，短的会提前播完。素材等比缩放到能放进画面的最大尺寸，贴右侧、垂直居中；9:16 全屏素材在 9:16 成片上即原样铺满。
- 烧录只叠加画面：成片的帧数、时长、画幅和音轨都与烧录前一致，烧录后会逐项核对。烧录失败（素材损坏、渲染环境缺失等）不影响精剪：照常发布无引导版本，并在完成信息里写明原因。

## 手动导出与叠加

```bash
python tools/engagement_cta.py render
```

输出到 `outputs/engagement_cta/`，均带透明通道：

| 文件 | 用途 |
| --- | --- |
| `engagement_cta.mov` | ProRes 4444，剪映 / CapCut / Premiere / Final Cut 直接放到画中画轨 |
| `engagement_cta.webm` | VP9 透明，网页播放 |

另可用 `--formats mov,webm,green,png` 额外导出绿幕 MP4 或逐帧 PNG。

把动画叠加到任意一条视频：

```bash
python tools/engagement_cta.py apply outputs/某项目/成片.mp4
```

默认占成片最后 5 秒；`--start 3` 从第 3 秒开始，`--start -10` 从距片尾 10 秒开始，`--overlay 自定义.mov` 改用自己的透明视频。结果默认写到 `outputs/engagement_cta/applied/<原名>_cta.mp4`，原视频不改动。

## 可调参数

`render`、`apply`、`still` 都支持：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--labels` | 无 | 可选的 5 个点击提示；空项不生成气泡 |
| `--counts` | 无 | 可选的四个图标下方文字；填纯数字（如 `1280`）时点击后 +1 |
| `--avatar` | 透明圆框 | 账号头像图片 |
| `--headline` | 无 | 可选自定义大标题 |
| `--scale` | `1` | 互动栏整体缩放 |
| `--duration` | `5` | 动画总时长（秒），最短约 4.1；五次点击均分中间时间 |
| `--hold` | `0.3` | 全部点亮后停留（秒） |
| `--fps` | `30` | 帧率 |

例：

```bash
python tools/engagement_cta.py render --avatar ~/Pictures/logo.png --counts 43K,360,3576,1495 --duration 6 --name mz_cta
```

## 预览与检查

浏览器直接打开 `tools/engagement_cta.html` 可循环预览，拖动进度条逐帧查看，切换深色、浅色和透明格背景。URL 参数可临时覆盖同名配置，例如 `engagement_cta.html?duration=8&scale=1.2`；加 `embed=1` 去掉控制条（配置中心的预览窗即用此模式）。

`python tools/engagement_cta.py still --at 1.0,2.0,4.4` 输出指定时刻的透明 PNG。

## 实现

动画每一帧只由时间 `t` 决定（不使用 CSS 动画，粒子用固定种子），导出时无头 Chromium 逐帧调用 `renderAt(t)` 截透明 PNG，再交给 FFmpeg 编码。修改动画只需改 HTML；缓存键包含 HTML 内容，改动后会自动重新渲染。已烧录的旧成片需从无引导版本重新叠加，避免保留旧图标与文字。

- `tools/engagement_cta.html`：动画本体
- `tools/engagement_cta.py`：渲染、缓存、叠加命令
- `cta_burn.py`：配置中心设置、自定义视频校验与存储、精剪烧录前准备
- `codex_video_editor.py` 的 `_burn_cta`：精剪核验通过后执行烧录并核对结果
- 接口：`GET/POST /api/engagement-cta/settings`，`POST /api/engagement-cta/custom`（请求体为视频原始字节，文件名放在 URL 编码的 `X-Filename` 请求头），`POST /api/engagement-cta/custom/delete`
