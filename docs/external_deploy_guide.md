# 对外开放部署指南（服务端托管模式）

让**外部用户**打开网页就能用,而**不需要**他们自己装 Antigravity、也**拿不到**你的密钥。

## 原理

```
外部用户浏览器
   └─(Cloudflare 隧道 https 链接)→ 你电脑上的 SPARK 服务(8085)
        └─(本机)→ Antigravity 反代(8046)→ Gemini
```

- 密钥只存在你本机的 `server_config.json`(已被 `.gitignore`,不进前端、不进浏览器)。
- 一旦 `server_config.json` 里填了 `apiKey`,服务自动进入**托管模式**:忽略浏览器传来的任何密钥、用服务端密钥、按访问码放行、按 IP 限流。

## 开启步骤

### 1. 填服务端配置
编辑 [`server_config.json`](server_config.json):
- `apiKey`:填你 Antigravity 的 API Key(**强烈建议在 Antigravity 里新建一个 User Token 专用于本服务**,见下方安全说明)。
- `accessCode`:设一个访问码(例如 `spark-xxxx`,用 ASCII 字符)。外部用户首次打开会被要求输入,输对才能用。**留空 = 不设门,不建议对外。**
- `rateMax` / `rateWindow`:每个 IP 在 `rateWindow` 秒内最多 `rateMax` 次生成(默认 1 小时 20 次)。

> 也可以用环境变量覆盖:`SPARK_API_KEY` / `SPARK_ACCESS_CODE` / `SPARK_MODEL` / `SPARK_IMAGE_MODEL` / `SPARK_RATE_MAX` / `SPARK_RATE_WINDOW`。

### 2. 重启本服务
双击 `stop.bat` 再 `run.bat`(或重跑 `python server.py`)。
自检:浏览器开 `http://127.0.0.1:8085/api/mode`,应返回 `{"server_managed": true, "needs_access_code": true}`。
此时前端会自动隐藏「⚙️ API 配置中心」,并在进入时要求输入访问码。

### 3. 开 Cloudflare 隧道（把 8085 暴露到公网）
Antigravity Tools 内置 Cloudflare Tunnel:
1. 打开 Antigravity Tools → 设置 → Cloudflared 板块。
2. 模式 `quick`,**端口填 `8085`（SPARK 专用端口）**,协议 `http2`,开启。
   - ⚠️ 不要填 `8046`:这是 Antigravity 底层 API 与管理界面，不是 SPARK 网页。
   - ⚠️ 不要再使用旧端口 `8082`:该端口可能存在残留监听或超时。
   - SPARK 现在固定运行在 **8085**，图像服务站已并入主页面的「🎨 图像工坊」标签页，不再有独立地址。
3. 拿到形如 `https://xxx.trycloudflare.com` 的公网链接。

把**公网链接 + 访问码**发给外部用户即可。

## ⚠️ 安全建议(重要)

1. **轮换密钥**：任何曾出现在前端、日志、截图或说明书里的旧 Key 都应视为已泄露。请到 Antigravity 里**作废旧 Key、新建一个**，只把新 Key 放进 `server_config.json`。
2. **务必设 accessCode**:公网链接一旦泄露,没有访问码任何人都能烧你的配额。
3. **限流已默认开启**,可按需调小 `rateMax`。
4. 你的电脑必须**保持开机 + Antigravity 运行 + 本服务运行**,外部用户才能访问(quick 隧道链接每次重启会变)。

## 本地自用不受影响

`server_config.json` 的 `apiKey` 留空时,服务回到原来的本地模式:用浏览器「配置中心」里你自己存的 key,行为和以前完全一样。
