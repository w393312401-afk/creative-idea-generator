# SPARK 图像生成与图生图接口说明书

本文档对应当前本机已验证配置，适用于 `gemini-3.1-flash-image`（界面别名 `nano-banana-2`）的文生图与参考图编辑。

## 一、当前服务架构

| 服务 | 地址 | 用途 |
| --- | --- | --- |
| Antigravity Tools 代理 | `http://127.0.0.1:8046` | 底层 OpenAI 兼容接口及 Gemini 图像路由 |
| SPARK 应用服务 | `http://127.0.0.1:8085` | 网页、任务管理和应用层图像接口 |
| SPARK 图像工坊 | `http://127.0.0.1:8085/`（顶部「🎨 图像工坊」标签页） | 文生图、图生图操作界面 |

当前 `server_config.json` 的推荐配置：

```json
{
  "baseUrl": "http://127.0.0.1:8046/v1",
  "imageModel": "gemini-3.1-flash-image",
  "imageEditTransport": "auto"
}
```

注意：

- `8045` 是旧代理端口，当前机器上该端口可能被残留监听占用，请改用 `8046`。
- `8082` 是旧 SPARK 配置，不应继续使用。该端口可能残留监听但请求超时。
- 开发网页和脚本时优先调用 SPARK 的 `8085` 应用接口；只有第三方客户端必须绕过 SPARK 时，才直接调用 Antigravity 的 `8046/v1`。

## 二、模型与路由要求

- 界面模型：`nano-banana-2`
- 实际模型：`gemini-3.1-flash-image`
- 支持比例：`1:1`、`9:16`、`16:9`、`3:2`、`2:3`、`21:9` 等。
- 支持清晰度：`1K`、`2K`、`4K`。

**当前实测基线（2026-07-29，Windows，Antigravity Tools 端口 8046）：**

- 底层 `/v1/images/edits` multipart 图生图连续两次返回 `200 OK`，返回 `data[0].b64_json`；方图和 9:16 竖图均已验证成功。
- SPARK 应用层仍统一暴露 `/api/image/edits`，请求格式是 multipart 表单；服务端会根据配置转发到底层图像接口。
- 底层 `/v1/images/generations` 只用于文生图。不要通过给 generations JSON 增加 `image` 字段来模拟图生图；该字段可能被静默忽略。
- 当前 Windows 网关的 `/v1/chat/completions` 图生图不可用：裸模型名和 `-1-1` 后缀模型名均返回上游 `404 Requested entity was not found`，随后被账号池包装成 `429 All accounts exhausted`。macOS 网关已确认 chat + 裸模型名 + `image_url` 有效，不能将 Windows 结论外推到 macOS。
- 直接调用底层 `/v1/images/edits` 时，模型名使用干净名 `gemini-3.1-flash-image`，不要带 `-2k`、`-9-16`、`-1-1` 等魔法后缀。
- Windows 当前推荐 `imageEditTransport: "auto"`：优先使用已验证成功的 edits；只有网关以后再次出现 edits 号池故障时才尝试回退。macOS 若 `/images/edits` 仍命中 Pro 图片池硬编码问题，则使用 `imageEditTransport: "chat"`。

## 三、调用 SPARK 应用接口

### 1. 文生图

请求：

```text
POST http://127.0.0.1:8085/api/image/generations
Content-Type: application/json
```

JSON 字段：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `prompt` | 是 | 文生图提示词 |
| `model` | 否 | 推荐 `nano-banana-2` 或 `gemini-3.1-flash-image` |
| `size` | 否 | 宽高比，如 `1:1`、`9:16`、`16:9`；默认 `1:1` |
| `quality` | 否 | `1K`、`2K`、`4K`；默认 `2K` |
| `response_format` | 否 | 推荐 `b64_json` |
| `config` | 否 | 本地模式下可传 `{ "baseUrl": "...", "apiKey": "..." }`；托管模式下密钥由服务端配置 |

Python 示例：

```python
import base64
import requests

response = requests.post(
    "http://127.0.0.1:8085/api/image/generations",
    json={
        "model": "nano-banana-2",
        "prompt": "清晨薄雾中的现代玻璃住宅，真实摄影",
        "size": "16:9",
        "quality": "2K",
        "response_format": "b64_json"
    },
    timeout=300
)
response.raise_for_status()

image = base64.b64decode(response.json()["data"][0]["b64_json"])
with open("text_to_image.png", "wb") as file:
    file.write(image)
```

### 2. 单参考图图生图

请求：

```text
POST http://127.0.0.1:8085/api/image/edits
Content-Type: multipart/form-data
```

表单字段：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `image` | 是 | 第一张主参考图 |
| `prompt` | 是 | 编辑指令 |
| `model` | 否 | 推荐 `nano-banana-2`；服务端会映射为 `gemini-3.1-flash-image` |
| `aspect_ratio` | 否 | 如 `1:1`、`9:16`、`16:9`；传 `auto` 表示不强制 |
| `image_size` | 否 | `1K`、`2K` 或 `4K`；默认通常为 `1K` |
| `response_format` | 否 | 推荐 `b64_json` |
| `style` | 否 | 可选风格，会追加到提示词 |
| `config` | 否 | JSON 字符串；本地模式可携带 `baseUrl/apiKey` |

Python 示例：

```python
import base64
import requests

with open("reference.png", "rb") as reference:
    response = requests.post(
        "http://127.0.0.1:8085/api/image/edits",
        files={
            "image": ("reference.png", reference, "image/png")
        },
        data={
            "model": "nano-banana-2",
            "prompt": "将场景光线改为清晨，只修改光线，保持参考图的内容、人物、物体、视角和构图不变。",
            "aspect_ratio": "9:16",
            "image_size": "2K",
            "response_format": "b64_json"
        },
        timeout=300
    )

response.raise_for_status()
image = base64.b64decode(response.json()["data"][0]["b64_json"])
with open("image_edit.png", "wb") as file:
    file.write(image)
```

### 3. 多参考图图生图

第一张图片使用 `image`，其余图片重复使用 `image[]`：

```python
import base64
import requests

files = [
    ("image", ("main.jpg", open("main.jpg", "rb"), "image/jpeg")),
    ("image[]", ("material.webp", open("material.webp", "rb"), "image/webp")),
    ("image[]", ("style.png", open("style.png", "rb"), "image/png"))
]

response = requests.post(
    "http://127.0.0.1:8085/api/image/edits",
    files=files,
    data={
        "model": "nano-banana-2",
        "prompt": "以第一张图为主画布，保持构图；参考第二张材质和第三张配色。",
        "aspect_ratio": "9:16",
        "image_size": "2K",
        "response_format": "b64_json"
    },
    timeout=300
)
response.raise_for_status()

image = base64.b64decode(response.json()["data"][0]["b64_json"])
with open("multi_reference_edit.png", "wb") as file:
    file.write(image)
```

服务端也兼容旧字段 `image1`、`image2`，但新代码应使用 `image` 和 `image[]`。

## 四、直接调用 Antigravity 底层接口

只有第三方客户端必须绕过 SPARK 时才建议直接调用。当前本机底层端口是 `8046`。

### 1. 直接文生图

```python
import base64
import requests

response = requests.post(
    "http://127.0.0.1:8046/v1/images/generations",
    headers={
        "Authorization": "Bearer YOUR_API_KEY",
        "Content-Type": "application/json"
    },
    json={
        "model": "gemini-3.1-flash-image",
        "prompt": "清晨薄雾中的现代玻璃住宅，真实摄影",
        "size": "16:9",
        "quality": "2K",
        "response_format": "b64_json"
    },
    timeout=300,
)
response.raise_for_status()

with open("direct_text_to_image.png", "wb") as file:
    file.write(base64.b64decode(response.json()["data"][0]["b64_json"]))
```

### 2. 直接图生图

```python
import base64
import requests

with open("reference.png", "rb") as reference:
    response = requests.post(
        "http://127.0.0.1:8046/v1/images/edits",
        headers={
            "Authorization": "Bearer YOUR_API_KEY"
        },
        files={
            "image": ("reference.png", reference, "image/png")
        },
        data={
            "model": "gemini-3.1-flash-image",
            "prompt": "改为雨夜霓虹氛围，保持参考图的主体、视角、透视和建筑布局；向上下自然扩展为竖版构图，不要文字。",
            "size": "720x1280",
            "response_format": "b64_json"
        },
        timeout=300,
    )
response.raise_for_status()

with open("direct_edit.png", "wb") as file:
    file.write(base64.b64decode(response.json()["data"][0]["b64_json"]))
```

直接图生图参数要点：

- `model` 使用 `gemini-3.1-flash-image` 干净名。
- `image` 是 multipart 文件字段；多图可追加 `image[]`。
- 底层接口当前已验证 `size="1024x1024"` 和 `size="720x1280"`。其中 `720x1280` 会输出网关原生 1K 竖图 `768x1376`，这是正常的尺寸归一化。
- SPARK 应用层仍可使用 `aspect_ratio` 和 `image_size`；直接调用 8046 时优先使用本节已验证的 `size` 写法。
- 不要手动设置 `Content-Type: multipart/form-data`；让 HTTP 客户端自动生成 boundary。

## 五、参考图上传规则

- 支持 PNG、JPEG/JPG、WEBP。
- 单文件前端限制为 10 MB。
- 服务端会尽量保留原始内容；SPARK 兼容路径可能会压缩到 JPEG 并裁剪到指定比例，以提高稳定性。
- 第一张图是主画布，后续图片作为物体、材质或风格参考。
- 文件名支持中文。
- 选择文件后可立即点击生成，不需要等待缩略图解码完成。

## 六、提示词建议

图生图指令应明确“修改什么”和“保持什么”：

```text
将场景光线改为清晨。严格保持原图中的人物、物体、背景、相机位置、
透视关系和画面构图不变，只调整天空颜色、光照方向和整体色温。
```

避免只写宽泛主题，例如“清晨城市”或“梦幻风景”。这类文字容易让模型重新构图。

## 七、宽高比与清晰度

| 比例 | 1K | 2K | 4K |
| --- | --- | --- | --- |
| `1:1` | 1024x1024 | 2048x2048 | 4096x4096 |
| `9:16` | 768x1376 | 1536x2752 | 3072x5504 |
| `16:9` | 1376x768 | 2752x1536 | 5504x3072 |
| `3:2` | 1264x848 | 2528x1696 | 5056x3392 |
| `2:3` | 848x1264 | 1696x2528 | 3392x5056 |
| `21:9` | 1584x672 | 3168x1344 | 6336x2688 |

对于图生图，若希望最大程度保持构图，建议选择与参考图一致的宽高比。

2026-07-29 竖版实测：请求 `size="720x1280"`，实际返回 JPEG `768x1376`。实际比例约为 1:1.792，与标准 9:16 的 1:1.778 接近；这是网关的原生输出尺寸，不应在请求成功后误判为比例失败。

## 八、排错清单

1. 检查 `server_config.json`：

   ```json
   "baseUrl": "http://127.0.0.1:8046/v1"
   ```

2. 检查服务：

   ```text
   http://127.0.0.1:8046/health
   http://127.0.0.1:8085/api/ping
   ```

3. 若请求超时：确认没有仍指向旧端口 `8045` 或 `8082`。

4. 若返回图与参考图完全无关：确认使用的是 `/api/image/edits` 或底层 `/v1/images/edits`。注意 `/v1/images/generations` 会**静默丢掉** `image` 字段——照样返回 200 和一张全分辨率的图，但那是纯文生图的产物，与参考图毫无关系（2026-07-25 实测），拿它做图生图等于制造断链帧。2026-07-29 当前 Windows 网关的 `/v1/chat/completions` 图片输入路径会返回 404/429，不能作为 Windows 兜底；macOS 上该 chat 路径有效。

5. 若返回 `503 No accounts available with quota` 或 `429 All accounts exhausted`，这是账号配额 / 限流问题，不是上传参数问题，稍后重试。

6. 若底层 `/v1/images/edits` 返回 `502/404`：确认软件版本为 `antigravity_tools v4.2.9`，并确认模型名没有带 `-2k`、`-9-16` 等后缀。

7. 若图生图（Edits）请求返回 `502`，且错误信息为 `No accounts available with quota for model: gemini-3-pro-image`（但实际请求模型为 `gemini-3.1-flash-image` 或 `nano-banana-2`）：
   - **根本原因**：Antigravity Tools 代理端（8046 端口）的 Rust 源码 `src-tauri/src/proxy/handlers/openai.rs` 在处理 edits 接口时存在硬编码 `gemini-3-pro-image` 模型的 bug。本地虽已进行修复（已提交 `commit eeba7129` 改为动态获取请求中的模型），但每次 **Antigravity 应用自动更新** 时，官方新包都会覆盖本地已编译的补丁 `antigravity_tools.exe`，导致此问题间歇性复发。
   - **复发解决方案**：
     1. 打开终端，切换至代理程序 Rust 源码目录：`cd D:\Antigravity-Manager\src-tauri`
     2. 确认本地 git 处于正确的分支且已合入 `commit eeba7129` 补丁。
     3. 重新进行 release 编译：`cargo build --release`
     4. 关闭当前正在运行的 app，并强杀残留的 8046 端口进程，确保端口释放。
     5. 将新编译生成的 `src-tauri/target/release/antigravity_tools.exe` 覆盖复制到 `D:\Antigravity-Manager\antigravity_tools.exe`。
     6. 重启服务。*注意：若启动后 cloudflared 隧道未正常拉起，需在命令行手动运行 `cloudflared tunnel run --token <gui_config.cloudflared.token> --protocol http2` 重新建立外网映射通道。*
   - **macOS 机器上没有这条修复路径**：网关是官方包 `/Applications/Antigravity Tools.app`，本机没有 `src-tauri` 源码树可重编，上面那套 `D:\Antigravity-Manager` 的流程只适用于原 Windows 机器。所以这台机器上 edits 接口的硬编码一直是未修补状态。
   - **应用侧的跨平台兜底（2026-07-25 起接入）**：帧序列的图生图撞上这堵墙时会用**同一个模型**改走 `/chat/completions`（参考图内联成 data URL）。macOS 已确认该路径有效；2026-07-29 当前 Windows 网关上 chat 图片路径实测失败。因此它是平台相关兜底，不能假设所有机器都可用；两条通道都失败时应如实结束任务并保留原始错误。其代价与留痕如下：
     - 这条通道只有**顶层 `size` 字段**能控出图比例；`aspect_ratio`、`image_size` 和写在提示词里的 "9:16" 全部无效（会出 1024x1024 方图）。
     - 分辨率控不了，固定出 **1K 档**（9:16 为 `768x1376`）。注意这与 `/images/edits` 的 `image_size=1K` 逐像素同档——**本单请求本来就是 1K 时画质没有任何损失**，只有请求 2K（`1536x2752`）/4K 才是真降档。
     - 走过这条通道的帧一律在 manifest 里记 `transport: "chat_completions"` 和 `actual_pixels`；只有确实降档（请求 2K/4K）时才额外记 `degraded_reason`，动态流播报也只在真降档时标警示色（`transport_fallback` 事件带 `degraded` 布尔）。号池额度恢复后对降档帧定向重渲即可换回全分辨率。
     - **不重复发那一枪**：撞墙一次后本进程就记住这个网关的 edits 已死（`_EDITS_POOL_DRY`），后续帧直接走 chat 通道——那一枪实测 ~1010ms，还要把整张参考图（~830KB）上传一遍，每帧白烧一次没有意义。熔断只活在进程内，网关补好补丁后**重启服务**即重新探路，不需要改配置。
     - 通道开关 `server_config.json` → `imageEditTransport`：`auto`（默认，先探 edits 再尝试 chat）/ `chat`（从不发 edits）/ `edits`（只发 edits）。**2026-07-29 当前 Windows 机器推荐 `auto`**；固定为 `chat` 会绕开已经恢复的 `/images/edits`。**macOS 若 edits 仍受硬编码号池问题影响，则推荐 `chat`**，因为该平台的 chat + `image_url` 已确认有效。
     - 这个键从前只写在配置文件里、实际从未生效：托管模式（`server_config.json` 配了 `apiKey`）下 `server_common.effective_config` 用白名单重建 config，`imageEditTransport` 不在名单里被整个丢掉，于是配了 `chat` 的机器每个进程照样先打一枪必挂的 edits（2026-07-26 修）。改这份白名单时记得同批加新键，这是同一个口子第二次漏（第一次是 `qaGateLevel`）。
     - 探路那一枪撞墙**不再播报成上游报错**：auto 模式下它后面还有 chat 通道接着渲，把它推进度流会被前端渲成「⚠️ 上游报错…此路终止，任务即将报错结束」——一句吓人的假话。换通道由 `transport_fallback` 事件如实播报，配额耗尽仍写 WARN 日志；真正走投无路（没有等价 chat 模型、或 `transport=edits`）的那一枪照报（`_execute_request_with_retry(emit_quota_failure=...)`）。
     - 图像站（`/api/image/edits`）走的是原样透传，**不带**这条兜底，撞墙时仍会直接报错。

## 九、已验证基线

### 2026-07-29 Windows 最新实测

| 请求 | 结果 |
| --- | --- |
| `/v1/images/generations` + `gemini-3.1-flash-image-1-1` | `200`，成功生成 1024x1024 PNG 文生图参考图 |
| `/v1/images/edits` + `gemini-3.1-flash-image` + multipart `image` + `size=1024x1024` | 连续两轮 `200`；成功保留城市布局并完成雨夜霓虹改绘，输出 1024x1024 JPEG |
| `/v1/images/edits` + `gemini-3.1-flash-image` + multipart `image` + `size=720x1280` | `200`；成功生成竖版延展构图，实际输出 768x1376 JPEG |
| `/v1/chat/completions` + 裸模型名 + `image_url` | 上游 `404 Requested entity was not found`，被包装成 `429 All accounts exhausted` |
| `/v1/chat/completions` + `gemini-3.1-flash-image-1-1` + `image_url` | 同样返回上游 404/包装后 429；模型后缀不能修复当前 chat 路由 |
| `/v1/chat/completions` + `gemini-3-pro-image` | `503 No accounts available with quota for model` |

本轮测试文件：

- 文生图参考图：`outputs/img2img_source.png`
- 方形图生图：`outputs/img2img_result.jpg`
- 9:16 竖版图生图：`outputs/img2img_result_9x16.jpg`

Windows 结论：当前可靠图生图通道是标准 multipart `/v1/images/edits`，推荐 `imageEditTransport: "auto"`；当前 Windows chat + `image_url` 不可用。

### macOS 平台基线

- macOS 机器上 `/v1/images/edits` 可能被硬编码路由到 `gemini-3-pro-image` 无额度池，而 `/v1/chat/completions` + 裸模型名 + `image_url` 已确认有效。
- macOS 推荐 `imageEditTransport: "chat"`；chat 通道使用顶层 `size` 控制比例，9:16 输出为 1K 档 `768x1376`。
- Windows 与 macOS 使用不同网关构建和账号路由，两套结论并存，不能互相覆盖。

### 更早历史记录

2026-06-30 历史验证：

- Antigravity Tools `4.2.9`
- 底层端口 `8046`
- `/v1/models` 能列出 `gemini-3.1-flash-image`
- `/v1/images/edits` + `gemini-3.1-flash-image` + multipart `image` 返回 `200 OK`
- 测试输出：`C:\Users\video\.antigravity_tools\model_availability_tests\upgrade_429_edit_output.png`

历史记录：

- 2026-06-30 曾用真实 1536x2752 PNG 参考图，经 SPARK `/api/image/edits` 验证单参考图、多参考图和帧序列可用。
- 2026-06-30 早些时候在 `4.2.7/4.2.8` 与旧端口 `8045` 下出现过 `/v1/images/edits` 502/404；该结论已被 `4.2.9 + 8046` 的新实测取代。
- 2026-06-30 下午已解决 **“从第二张图开始图生图不参考参考图且比例始终为 1:1”** 的问题：
  - **根本原因**：服务器此前将 Gemini 模型的图生图请求重定向到了 JSON 格式的 `/v1/images/generations`，且由于带有了 `-2k` 等质量后缀，导致网关无法正确匹配而退化路由至 `dall-e-3`（纯文生图模型）。DALL-E 3 静默忽略了 `image` 字段并退化为 1:1 比例。
  - **修复逻辑**：将 `server.py` 的手动图生图代理接口（`/api/image/edits`）与后台帧序列生成器（`_generate_image_edit`）中的 Gemini 图生图逻辑全部路由至标准的 **multipart `/v1/images/edits`** 接口；同时对于以 `gemini` 开头的模型名，**完全停用追加 `-2k`、`-4k` 质量后缀的逻辑**，以确保网关精准识别干净的 `gemini-3.1-flash-image-9-16` 等模型名。
  - **测试验证**：通过 `scratch/test_i2i.py` 本地自动生成红色正方形参考图，并调用图生图将其成功编辑为同等位置与尺寸的蓝色圆形，证明参考图识别与比例控制均已完美恢复。
