# WSL GSA

**让 WSL 通过 Windows 原生连接使用现有 GSA 策略。**

这是一个非官方、MIT 开源的小工具，不是微软原生 WSL 支持，也不是 VPN。
它不会关闭或修改 GSA、组织策略、证书、默认路由或 `resolv.conf`。
请先确认你的组织允许使用此类桥接。

## 安装

要求：**WSL 2 NAT 模式**、Ubuntu/Debian、Python 3.9+、已运行的 systemd、
Windows 可执行文件互操作，以及 Windows 上已安装、启动并登录的 GSA。
已满足这些前提时，安装和开关不需要重启 Windows 或 WSL。

在 WSL 普通用户终端中执行：

```sh
git clone https://github.com/pvz122/wsl-gsa.git
cd wsl-gsa
sh install.sh --check
sh install.sh
gsa on
gsa status --probe
gsa off
```

安装器先检查 WSL，再通过 PowerShell 检查 GSA 引擎、隧道进程和驱动，
确认 Windows 网关对应 WSL 网卡后，才安装组件。运行时检测不能证明
GSA 已正确登录或每条策略都生效，需要你先在 Windows 上确认。

Windows 需要 Node.js 22+；缺失时通过 winget 安装 LTS，接受其安装协议，
可能需要 Windows 授权。如果不能无人值守安装，请先自行安装官方 Node.js LTS。
Linux 缺少的 redsocks、nftables、iproute2、curl 通过 apt 安装。
使用 Windows 用户本来就有的 `wsl.exe --user root` 能力进行 Linux 安装，
不会保存 sudo 密码、添加 sudoers 条目或修改 PowerShell 执行策略。
安装不会自动开启转发，也不添加任何开机启动。

## 日常使用

| 命令 | 用途 |
| --- | --- |
| `gsa on` | 开启新建 IPv4 TCP 和普通 DNS 转发 |
| `gsa off` | 先删除专属规则，再停止 Linux 和 Windows 组件 |
| `gsa status` | 显示 ON / OFF / DEGRADED 和组件状态 |
| `gsa status --probe` | 额外验证一个公共 HTTPS 地址，明确禁用应用代理 |
| `gsa status --json` | 脚本可解析 JSON，不带颜色 |
| `gsa uninstall` | 关闭并移除工具文件，保留通用依赖 |

`on/off` 可重复调用。状态异常时先 `gsa off` 再 `gsa on`。
返回码 0 表示 ON 或 OFF，2 表示 DEGRADED，1 表示命令/检查错误。
不带子命令不会默认打开。支持 TTY 颜色、`NO_COLOR`，不会改写 shell 环境变量。

## 覆盖范围与边界

- 系统级截获**新建 IPv4 TCP** 和 **IPv4 UDP/TCP 53 端口 DNS**，
  包括后台服务和进入 `docker0` / `br-*` 的 Docker 桥接流量。
- 非 DNS 的本地回环、当前直连子网 TCP 保持直连，保护本地应用与数据库。
- **DNS 优先于本地绕过**：即便指定 DNS 服务器或本地 53 端口，也转交 Windows。
  因此自建 DNS、Docker 服务发现和分流 DNS 可能受影响。
- 其他 UDP、QUIC 和 IPv6 **原样保留，不代理、不阻断**。这不是防泄漏或全协议 VPN。
- 已建立连接不会迁移。新建 Docker 网络或更换网络后需要关闭再开启，以刷新直连网段。
- Windows 上仍由 GSA 决定策略和可达性；工具不能绕过封锁，也不能保证所有网站/端口。
- Linux TCP DNS 通过桥接后仍以 Windows UDP DNS 发出；大响应/截断后的 TCP 回退
  不保证可用。DoH/DoT 不会自动转换为普通 DNS。
- 仅支持同时开启一个发行版；不支持 mirrored/virtioproxy 网络和任意第三方网络布局。

Windows 监听器只绑定检测到的 WSL IPv4 网卡，只允许同子网来源；
不会开放 Wi-Fi/以太网监听或自动放行防火墙。但 WSL 子网内协议没有身份认证，
同一主机上能够访问该子网的其他发行版/进程也能使用它。请仅用于可信 WSL 环境，
不要对外转发端口。TLS 仍为端到端，不安装 CA，不解密流量。

## 故障与卸载

先检查 Windows 本身是否能访问目标，再检查 `gsa status`。
Linux 日志：`journalctl -u wsl-gsa-tcp -u wsl-gsa-dns`；
Windows 日志位于 `%LOCALAPPDATA%\WslGsaBridge\<发行版哈希>\bridge.log`。
日志、运行时配置和状态可能包含机器信息，请勿直接上传到公共仓库。

```sh
gsa off
gsa uninstall
```

Node.js、redsocks 等依赖不会自动卸载，以免影响其他软件。
完整架构、文件位置、故障恢复、安全边界、开发测试说明见[英文 README](../README.md)。
