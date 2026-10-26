# WSL GSA

**让 WSL 通过 Windows 原生连接使用现有 GSA 策略。**

这是一个非官方、MIT 开源的小工具，不是微软原生 WSL 支持，也不是 VPN。
它不会关闭或修改 GSA、组织策略、证书、默认路由或 `resolv.conf`。
请先确认你的组织允许使用此类桥接。

## GSA 是什么

**GSA** 是 **Microsoft Entra Global Secure Access（全局安全访问）** 的简称，
是微软围绕身份和访问策略提供的网络访问服务，主要包含：

| 服务 | 作用 |
| --- | --- |
| Microsoft Entra Internet Access | 按组织配置，对受支持的互联网和 Microsoft 365 流量应用访问控制 |
| Microsoft Entra Private Access | 基于身份访问组织配置的私有应用和资源，无需将它们直接暴露到公网 |

组织管理员配置流量转发配置文件、目标应用和访问策略；Windows 上的 GSA 客户端
负责获取符合条件的流量并送往服务。**安装了客户端不代表所有流量都会被接管，
更不代表所有目标都已授权访问**。具体覆盖范围和权限仍由组织决定。

### 为什么 Windows 能访问，WSL 却不行

WSL 2 的 Linux 运行在轻量虚拟机中，来宾网络流量不等同于 Windows 应用原生发起的流量。
微软列出的已知限制包括：Windows GSA 客户端不获取 WSL 来宾流量。
因此，同一台电脑上的 Windows 浏览器或 Git 能访问某个目标，而 WSL 中的
`curl`、Git、包管理器或开发服务却可能失败。

DNS 也必须配套考虑：GSA 可能为配置的目标返回合成地址。仅转发 TCP、却继续使用
不匹配的 DNS 路径，可能导致解析结果与连接路径不一致。

本工具对**新建 IPv4 TCP 连接和普通 53 端口 DNS**进行透明桥接：
Linux 侧截获流量，Windows 进程使用原生 socket 重新发起连接，再由现有 GSA
决定如何获取流量、应用何种策略。它不是 Linux 版 GSA 客户端，也不会让 WSL
获得微软原生 GSA 支持；所需的许可、管理员配置、登录和应用授权仍须事先具备。

### 什么时候有用

- Windows 已能通过组织的 Private Access 访问私有 Git、包仓库或 TCP 开发端点，
  但 WSL 中的相同工具无法访问。
- 希望受支持的 WSL 开发流量走组织已有的 Internet Access 路径，
  不想为每个命令行工具分别配置代理变量。
- 后台任务、systemd 服务或普通 Docker 桥接容器也需要相同的 TCP/DNS 路径，
  而仅在交互式 shell 设置代理无法覆盖它们。

使用前先确认目标在 Windows 上正常、组织允许此类桥接。它不能解决 Windows
GSA 未登录、应用未授权或策略拒绝的问题。如果需要 IPv6、QUIC/其他 UDP、
全协议 VPN 或防泄漏保证，它并不适用；如果 WSL 已通过组织批准的网络方案正常工作，
也不一定需要安装。

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
首次安装不会自动开启转发，autostart 默认关闭；更新安装保留已有的 autostart 设置。

## 日常使用

| 命令 | 用途 |
| --- | --- |
| `gsa on` | 开启新建 IPv4 TCP 和普通 DNS 转发 |
| `gsa off` | 先删除专属规则，再停止 Linux 和 Windows 组件 |
| `gsa status` | 显示 ON / OFF / DEGRADED 和组件状态 |
| `gsa status --probe` | 额外验证一个公共 HTTPS 地址，明确禁用应用代理 |
| `gsa status --json` | 脚本可解析 JSON，不带颜色 |
| `gsa autostart on` | 设置下次 WSL 发行版启动时自动开启转发 |
| `gsa autostart off` | 取消后续自动启动，不改变当前转发状态 |
| `gsa autostart status` | 查看自动启动设置，也支持 `--json` |
| `gsa uninstall` | 关闭并移除工具文件，保留通用依赖 |

`on/off` 可重复调用。状态异常时先 `gsa off` 再 `gsa on`。
返回码 0 表示 ON 或 OFF，2 表示 DEGRADED，1 表示命令/检查错误。
不带子命令不会默认打开。支持 TTY 颜色、`NO_COLOR`，不会改写 shell 环境变量。

## autostart：随 WSL 发行版启动

autostart 是**显式开启、按发行版生效**的模式，不是 Windows 登录启动项：

```sh
gsa autostart on
gsa on                    # 如需现在也开启，单独执行
gsa autostart status
gsa status --json         # linux.autostart 表示启动设置，不代表当前转发健康
```

启用后，`wsl-gsa-autostart.service` 随该发行版的 systemd 启动执行，
不会每开一个 shell 就执行一次。它**不会在 Windows 登录时拉起 WSL**，
不创建 Windows 计划任务，也不保证空闲 WSL 一直运行；WSL 自身的空闲关闭和
进程生命周期机制仍然适用。只支持一个活动桥接，请勿在多个发行版同时启用。

启动助手以 root 身份读取安装时保存的发行版名和 Windows 可执行文件路径，
寻找 WSL 互操作 socket，并复用 `gsa on` 的命令锁、健康检查和失败回滚。
它在网络启动后尝试激活；互操作或 GSA 尚未就绪时，最多尝试 **12 次**，
尝试之间等待 **10 秒**，整个 systemd 启动过程上限为 **15 分钟**。
Linux 尚无转发组件运行时，会先停止上次 WSL 退出后残留的受管 Windows 桥接，
再使用当前 NAT 网关重新建立连接。

Windows 上仍须预先运行并登录 GSA；助手不会替你登录、改策略或绕过执行策略。
无人值守启动时互操作 socket 可能尚不可用；超过重试范围后服务会明确失败并记录日志，
不会把未就绪状态当成成功：

```sh
journalctl -u wsl-gsa-autostart.service -b
gsa status
gsa on                    # 修复登录/互操作等问题后手动重试
```

启动成功后没有持续轮询或自动恢复。网络变化后仍需 `gsa off && gsa on`。
`gsa off` 会取消**本次发行版启动中**尚未完成的自动启动尝试，但保留下次启动设置。
如果要回到手动模式并立即停止：

```sh
gsa autostart off          # 同时取消尚未完成的启动尝试
gsa off
```

仅修改 autostart 设置不会立即开关当前转发。卸载会禁用并移除启动单元；
安装和启用都不会修改 shell 启动文件。

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

## 官方参考

- [Global Secure Access 概述](https://learn.microsoft.com/en-us/entra/global-secure-access/overview-what-is-global-secure-access)
- [GSA 当前已知限制](https://learn.microsoft.com/en-us/entra/global-secure-access/reference-current-known-limitations)
