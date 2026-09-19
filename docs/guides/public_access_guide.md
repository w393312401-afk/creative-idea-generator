# Antigravity Tools 公网访问与接口请求指南

本指南介绍如何从公网（外网）请求你本地部署的 `Antigravity Tools` 代理服务（当前本机端口 `8046`）。

---

## 方式一：使用内置的 Cloudflare Tunnel（最推荐，免配置）

`Antigravity Tools` 已经内置集成了 Cloudflare Tunnel 穿透服务。它可以为你本地的 `http://localhost:8046` 服务自动生成一个安全的公网 HTTPS 链接（形如 `https://*.trycloudflare.com`），无需公网 IP，也无需配置路由器端口映射。

### 1. 开启公网隧道
1. 打开 `Antigravity Tools` 的主界面。
2. 进入**设置 (Settings)**，找到 **Cloudflared (Cloudflare Tunnel)** 配置板块。
3. 确保将其开启：
   * **模式 (Mode)**：选择 `quick`（快速隧道，无需注册账号）。
   * **端口 (Port)**：`8046`（与代理服务端口一致）。
   * **协议 (Protocol)**：`http2`。
4. 开启后，主界面或系统托盘菜单中会显示生成的公网 URL，例如：
   `https://random-subdomain.trycloudflare.com`

---

## 方式二：使用 Tailscale 等虚拟局域网（安全，仅限个人设备间）

如果你只需要在你自己的其他设备（如手机、外网笔记本）上访问本地服务，使用虚拟局域网是最安全的选择。

### 步骤
1. 在本地电脑和需要访问的客户端设备上安装并登录 [Tailscale](https://tailscale.com/)。
2. 此时本地电脑会获得一个虚拟局域网 IP，例如 `100.115.12.34`。
3. 在客户端设备上直接请求：`http://100.115.12.34:8046`。

---

## 方式三：路由器端口映射 + DDNS（适合有动态公网 IP 的用户）

如果你本地宽带有动态公网 IP（通过拨号获得）：
1. **路由器映射**：在路由器管理后台的“虚拟服务器”或“端口转发”中，将外网端口（如 `8046`）映射到你本地电脑的局域网静态 IP（如 `192.168.1.100`）的 `8046` 端口。
2. **DDNS 配置**：配置动态域名解析（如腾讯云、阿里域名或免费的 No-IP），用域名绑定你的公网 IP。
3. **公网请求地址**：`http://your-domain.ddns.com:8046`。

---

## 💻 客户端配置与 API 请求格式

不论你使用哪种穿透方式，获得公网 URL 后，就可以在任意客户端（如 Cursor、Cherry Studio、自定义脚本等）中请求你本地的服务了。

### 1. 基础配置参数
* **API Base URL / 接口地址**：`公网URL/v1`
  * *例如（Cloudflare）*：`https://random-subdomain.trycloudflare.com/v1`
  * *例如（Tailscale）*：`http://100.115.12.34:8046/v1`
* **API Key / 密钥**：使用你在 Antigravity Tools 中创建的 User Token。不要在说明书、前端代码或截图中记录真实密钥。

---

### 2. 代码请求示例 (curl / Python)

#### Python 示例 (使用 openai 库)
```python
from openai import OpenAI

client = OpenAI(
    # 将 base_url 替换为你的公网映射地址，必须带上 /v1
    base_url="https://random-subdomain.trycloudflare.com/v1",
    api_key="YOUR_API_KEY"
)

response = client.chat.completions.create(
    model="gpt-4o",  # 会自动映射为你本地配置的 Gemini 等对应模型
    messages=[
        {"role": "user", "content": "你好，请生成三个创意点子。"}
    ]
)

print(response.choices[0].message.content)
```

#### curl 命令行示例
```bash
curl https://random-subdomain.trycloudflare.com/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -d '{
    "model": "gpt-4o",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

---

## ⚠️ 安全注意事项
1. **保护好你的 API Key**：公网暴露后，任何知道你公网 URL 和 API Key 的人都可以调用你的接口消耗你的配额。
2. **IP 白名单与限流**：你可以在 `Antigravity Tools` 的“安全与监控”设置中配置 **IP 白名单 (IP Whitelist)** 或启用 **IP 黑名单**，并限制非信任 IP 的请求频率，防止接口被盗刷。
