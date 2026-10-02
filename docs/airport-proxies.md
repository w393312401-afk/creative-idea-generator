# 机场代理接入

代理池选路顺序为：静态代理 → 美国机场节点 → 其它机场节点。同一层轮转；禁用或最近检测失败的条目不参与选路。异常活动触发的换 IP 会逐条验证出口，并在本次视频任务内按账号排除已经被平台拦截的出口 IP；即使这些代理仍能联网，也不会循环复用。当前层没有可用的新出口就进入下一层；全部候选耗尽或达到重试上限后停止，保留已完成视频。任务进度会显示正在换 IP、验证后的旧新 IP 或停止原因。

机场 VLESS 等节点经独立 Mihomo 进程转成本机 HTTP 代理。每个节点有固定的 `127.0.0.1` 端口，互不切换；AdsPower 可以分别绑定。桥接不会接管系统代理、TUN 或 Clash Verge 当前节点。实现采用 [Mihomo Listener 的固定 proxy 字段](https://wiki.metacubex.one/en/config/inbound/listeners/)。

## 导入或更新订阅

在项目根目录运行（先安装 requirements.txt 中的依赖）：

```sh
.venv/bin/python tools/import_airport_proxies.py "/完整路径/机场配置.yaml"
```

默认使用 Clash Verge 内置的 Mihomo，入口从 17901 开始。可用 `--core` 指定其它内核，`--base-port` 指定其它起始端口。导入会先验证配置和全部本地入口，再写入代理池；启动失败恢复之前的配置。请在生成任务结束后更新订阅，重新导入会短暂重启该订阅的桥接进程。

这是一份本地配置快照。Clash 订阅更新后重新运行导入命令；相同名称保留代理 ID 和端口、人工禁用状态、备注和绑定关系。配置中消失的节点会被禁用，旧端口不会分配给其它节点。重新出现的节点需要人工启用。

## 运行与检查

- 代理池和连通性结果保存在项目的 `runtime/proxy_pool.json`，控制台可检测、启用、禁用和下发。
- 桥接配置及日志位于 `~/Library/Application Support/SPARK/airport_bridge/<bridge_id>/`。上游凭据仅存在于本机私有配置中，不进入代理列表接口或仓库。
- `~/Library/LaunchAgents/local.spark.airport-<bridge_id>.plist` 负责登录时启动和异常退出后的恢复。关闭 Clash Verge 窗口不影响独立桥接，但不要卸载它提供的内核。
- 导入结果会返回 bridge_id 和 LaunchAgent 路径。停用桥接前先在控制台禁用该订阅的节点，再对该 LaunchAgent 执行 `launchctl bootout`；需要停止登录启动时移除对应 plist。

连通性检测只证明该出口可联网并返回真实 IP，不代表 Google Flow 一定接受该地区或出口。代理失效后可在控制台重新检测，成功后会重新参与轮换。

视频任务检测到异常活动后，最多等待 30 秒收取其余在途结果。到期仍未确认的片段保留原账号、画布和提交记录，不重复生成；其他明确失败或未提交片段继续执行换 IP 流程。SOCKS 出口探测需要 `requests[socks]`，并由远端代理解析检测域名，避免本地 DNS 的假 IP 影响验证。
