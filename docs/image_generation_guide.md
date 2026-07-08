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
  "imageModel": "gemini-3.1-flash-image"
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

**当前实测基线（2026-06-30，antigravity_tools v4.2.9）：**

- 底层 `/v1/images/edits` multipart 图生图已恢复可用，实测 `200 OK`，返回 `data[0].b64_json`。
- SPARK 应用层仍统一暴露 `/api/image/edits`，请求格式是 multipart 表单；服务端会根据配置转发到底层图像接口。
- 底层 `/v1/images/generations` 仍用于文生图；SPARK 内部部分帧序列流程也会用 JSON `image` 字段做兼容图生图。
- 不建议用 `/v1/chat/completions` 做图生图；多模态聊天路径容易丢失参考图或变成纯文生图。
- 直接调用底层 `/v1/images/edits` 时，模型名使用干净名 `gemini-3.1-flash-image`，不要带 `-2k`、`-9-16` 等魔法后缀；比例和清晰度通过 `aspect_ratio`、`image_size` 字段传递。

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
            "prompt": "将场景光线改为清晨，只修改光线，保持参考图的内容、人物、物体、视角和构图不变。",
            "aspect_ratio": "9:16",
            "image_size": "2K",
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
- `aspect_ratio` 和 `image_size` 使用独立表单字段，不要写进模型名后缀。
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

4. 若返回图与参考图完全无关：确认使用的是 `/api/image/edits` 或底层 `/v1/images/edits`，不要用 `/v1/chat/completions` 做图生图。

5. 若返回 `503 No accounts available with quota` 或 `429 All accounts exhausted`，这是账号配额 / 限流问题，不是上传参数问题，稍后重试。

6. 若底层 `/v1/images/edits` 返回 `502/404`：确认软件版本为 `antigravity_tools v4.2.9`，并确认模型名没有带 `-2k`、`-9-16` 等后缀。

## 九、已验证基线

2026-06-30 当前机器验证：

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
