<div align="center">

# WSL GSA

**Your WSL traffic. Windows-native sockets. The same GSA policy.**

[![Tests](https://github.com/pvz122/wsl-gsa/actions/workflows/tests.yml/badge.svg)](https://github.com/pvz122/wsl-gsa/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-38bdf8.svg)](LICENSE)

`gsa on` &nbsp; / &nbsp; `gsa off` &nbsp; / &nbsp; `gsa status`

Manual, transparent IPv4 TCP + DNS bridging for WSL 2.
No shell proxy variables. No TLS decryption. No autostart.

[中文说明](docs/README.zh-CN.md)

</div>

> **Unofficial community tool, not Microsoft-supported WSL integration.**
> Microsoft documents that the Windows GSA client does not acquire WSL guest traffic.
> This tool originates replacement connections from Windows instead. It neither
> disables GSA nor changes organization policy. Use only where your organization permits it.

## Quick start

Run **inside your WSL distribution**, as your normal Linux user:

```sh
git clone https://github.com/pvz122/wsl-gsa.git
cd wsl-gsa
sh install.sh --check   # read-only: WSL first, Windows GSA second
sh install.sh          # install components; forwarding remains OFF

gsa on
gsa status --probe
gsa off
```

The installer deliberately uses an inspectable checkout rather than `curl | sh`.
Review it before installing. Running the installer authorizes package installation;
it uses your Windows account's existing `wsl.exe --user root` access, not a saved
sudo password. Linux code is then copied to root-owned locations before normal use.
No restart is performed or needed on an **already compatible** system.

## Requirements and installation order

| Requirement | Support |
| --- | --- |
| WSL | WSL 2, **NAT networking**, working Windows executable interop |
| Linux | Ubuntu or Debian, Python **3.9+**, running systemd |
| Windows | Windows PowerShell 5.1, GSA installed, running and signed in |
| Dependencies | Windows Node.js **22+**; Linux redsocks, nftables, iproute2, curl |
| Privileges | Package/network administration in this WSL distro; ordinary Windows user |

1. Validate Linux/WSL, systemd, distribution, executable interop and IPv4 default gateway.
2. Use PowerShell to detect the GSA engine, tunneling process and running driver;
   verify the gateway belongs to a Windows WSL adapter.
3. Only then install Windows bridge files and, if missing, Node.js LTS via `winget`.
   An existing older Node runtime must be upgraded explicitly. Winget installation
   accepts its package/source agreements and may need Windows elevation; if this
   cannot run unattended, install official Node.js LTS yourself and retry.
4. Install missing Linux dependencies with apt, then root-owned helpers and **static**
   systemd units. A newly installed stock redsocks service is prevented from starting
   and disabled; an existing redsocks service is not stopped or reconfigured.

The tool does not install, repair, sign into, or reconfigure GSA. Runtime presence is
only a readiness signal, **not proof of authentication, tunnel health or policy coverage**.
No GSA policies, firewall permissions, adapters, routes, resolver files or shell
startup files are changed by installation.

If systemd or NAT is not already enabled, installation stops with an explanation.
Enabling these platform prerequisites yourself may require a WSL restart; the bridge
does not perform one. Mirrored networking, virtioproxy, WSL 1, non-Debian package
managers and multiple simultaneously active distro bridges are not supported.

## CLI

```text
gsa on                  Enable new IPv4 TCP and ordinary DNS forwarding
gsa off                 Remove redirect rules first, then stop Linux/Windows relays
gsa status              Show ON / OFF / DEGRADED and component health
gsa status --probe      Also test https://www.microsoft.com over IPv4, without env proxies
gsa status --json       Script-friendly JSON; no ANSI formatting
gsa on --json
gsa off --json
gsa uninstall           Stop forwarding and remove this distro's installed tool files
gsa --version
gsa --help
```

No command means help/error, **not activation**. Repeated on/off is safe; partial
Linux state requires `gsa off` before `gsa on`. An unhealthy Windows bridge can be
restarted by `gsa on`; unexpected PID identity is never killed. Status exit codes:
**0** = ON or OFF, **2** = DEGRADED, **1** = command/inspection error. Argument errors
use argparse's exit code 2. `NO_COLOR=1` disables color; redirected output has no ANSI.
Only `--probe` makes a public HTTPS request. One successful probe does not verify
every site/port or prove which GSA forwarding profile acquired a connection.

## Precisely what gets forwarded

| Traffic | Behavior while ON |
| --- | --- |
| New outbound IPv4 TCP | Transparent REDIRECT to redsocks, then Windows HTTP CONNECT |
| IPv4 UDP/TCP destination port 53 | DNS wire message via HTTP to Windows; Windows issues UDP DNS |
| WSL users and background/system services | Included, regardless of proxy environment variables |
| Forwarded traffic entering `docker0` / `br-*` | Included, including ordinary Docker bridge egress |
| Loopback, locally assigned addresses, connected scope-link subnets | Direct for **non-DNS TCP**, preserving local apps/databases |
| Link-local, multicast and limited broadcast addresses | Direct for non-DNS TCP |
| Other UDP, including QUIC | **Untouched**, not tunneled or blocked |
| IPv6 | **Untouched**, not tunneled or blocked |
| Existing connections | Not migrated; reconnect applications after toggling |

DNS interception precedes the local-network bypass deliberately: even an explicit
DNS server address is sent to Windows so GSA's DNS-to-TCP mapping stays consistent.
Consequently local port-53 resolvers, Docker service-discovery DNS, split DNS and
custom DNS servers can behave differently while ON. Normal local app/database TCP
connections remain direct. Domain-based public/private policy coverage remains GSA's
decision. Cached DNS answers can outlive a toggle; reconnect/re-resolve if necessary.

Only the main routing table's connected networks **at activation time** are bypassed.
After adding a Docker bridge or changing networks, run `gsa off && gsa on`. Container
host networking is included as host traffic; custom bridge names, macvlan, rootless
networking, other namespaces and nonstandard firewall/routing arrangements are not
guaranteed. Existing firewall rules can still block traffic; this tool does not
override them. Internet-bound IPv6/UDP may use a different path, so this is **not a
VPN, kill switch or leak-prevention tool**.

DNS is forwarded as wire data, including synthetic answers used by GSA. Both Linux
UDP and TCP clients are supported, but Windows upstream queries intentionally use
UDP: GSA documents limitations around Windows TCP DNS acquisition. Truncated DNS
replies are preserved; large answers/TCP fallback are not guaranteed. DoH/DoT and
application DNS caches are not converted into port-53 DNS; disable encrypted DNS
yourself if required by your approved GSA configuration.

## How it works

```text
WSL host processes / Docker bridge
         |
         +-- IPv4 TCP --> dedicated nft NAT --> redsocks ----+
         |                                                 |
         +-- DNS :53 --> bounded Python UDP/TCP relay -------+
                                                           |
                                  Windows WSL adapter :18080
                                                           |
                                   Node.js host-native TCP / UDP DNS
                                                           |
                                     Existing Windows GSA acquisition
                                                           |
                                            Existing organization policy
```

Linux owns only `table ip wsl_gsa`. NAT OUTPUT/PREROUTING priority **-110** runs
before ordinary destination NAT; a dedicated INPUT guard protects relay listeners
from direct, unredirected external access. Other nftables tables are not flushed,
and `/etc/resolv.conf`, routes and Docker rules are not rewritten. Rules are
syntax-checked before application. Startup failures roll back newly created
components. Shutdown removes the tool table **before** stopping listeners.
DNS uses Linux `IP_PKTINFO` to preserve UDP reply source addresses under reverse NAT.

### Trust boundary

The Windows listener binds **only** to the detected WSL IPv4 adapter and accepts
only sources in that adapter's IPv4 subnet. It does **not** bind to `0.0.0.0`, Wi-Fi
or Ethernet, add Windows firewall exceptions, or install certificates.

The protocol is unencrypted/unauthenticated inside the WSL virtual subnet. Any
process or distro able to reach that subnet can use the running bridge's Windows
network identity. Trust your local WSL workloads. Do not expose/port-forward the
listener or use this on an untrusted shared WSL host. TLS application traffic is
still end-to-end; the bridge itself never installs a CA or decrypts it.

Linux network changes require root. This tool uses the Windows account's normal
ability to invoke its own WSL distro as root; it does not create new Windows/admin
permissions or sudoers entries. PID, creation time and script identity protect
against stopping a reused Windows PID. A root-owned command lock serializes Linux
users; a per-Windows-user mutex serializes Windows controller operations.

## Files, troubleshooting and removal

| Location | Purpose |
| --- | --- |
| `/usr/local/bin/gsa` | Root-owned CLI |
| `/usr/local/lib/wsl-gsa/` | Root-owned helpers, distro/Windows path configuration and command lock |
| `/etc/systemd/system/wsl-gsa-{tcp,dns}.service` | Manual/static units, no boot enablement |
| `/run/wsl-gsa/` | Ephemeral relay configuration/state |
| `%LOCALAPPDATA%\WslGsaBridge\<distro-hash>\` | Windows scripts, Node path, PID identity and bounded `bridge.log` |

Runtime files contain local paths and network addresses. **Do not publish them.**
The Node/Python bridge logs error categories, not DNS questions, payloads or
destinations. Third-party redsocks/system journals can contain network metadata;
inspect locally and redact before sharing.

- **Preflight fails:** open GSA, sign in and verify it works on Windows. Check WSL
  systemd, NAT networking and Windows interop. No organization settings are changed.
- **PowerShell execution policy blocks scripts:** use your approved policy/signed
  script workflow. The tool does not bypass execution policy.
- **Bridge unreachable:** inspect the installed Windows `bridge.log`; review your
  firewall with IT. Firewall changes are deliberately not automated.
- **Port conflict:** bridge uses Windows TCP **18080**, Linux TCP **12345**, and
  Linux UDP/TCP **1053**. Stop only the known conflicting owner, or choose not to
  use the tool. Another distro/prototype using those ports must be OFF first.
- **DEGRADED:** run `gsa off`, inspect
  `journalctl -u wsl-gsa-tcp -u wsl-gsa-dns`, then `gsa on`. If the gateway changed,
  this off/on cycle refreshes it. A running process alone is not a health guarantee.
- **One site/port fails:** compare with the same site/port on Windows. The tool
  cannot unblock destinations denied by GSA, bypass policy, or guarantee all ports.
- **Proxy variables already set:** those applications can still use their own
  proxy. The bridge does not unset variables; use `curl -4 --noproxy '*' ...` to
  test transparent interception.
- **Linux install fails after Windows setup:** errors are surfaced; fix and rerun
  `sh install.sh`. A stopped per-distro Windows installation may remain. It can be
  removed with its `control.ps1 -Action uninstall`; no boot task was created.

Uninstall from WSL:

```sh
gsa off
gsa uninstall
```

Dependencies (Node.js, redsocks, nftables, iproute2, curl) remain installed to avoid
breaking other software. Remove those separately only when you know they are unused.
Directories containing unknown files are retained and reported rather than deleted.
The clone and MIT license remain yours; no shell configuration is modified.
Reinstall/update by fetching the desired code, running `gsa off`, then `sh install.sh`.
No background polling, scheduled task, enabled service or default activation is added.

If the CLI is damaged, remove the interception first:

```sh
sudo nft delete table ip wsl_gsa
sudo systemctl stop wsl-gsa-tcp.service wsl-gsa-dns.service
```

An absent table may report an error; do not flush the entire ruleset. Stop Windows
with the installed controller's `-Action stop`, not a blanket Node process kill.

## Development

No pip/npm dependencies or build step:

```sh
python3 -m unittest discover -s tests -v
node --test tests/proxy.test.cjs
sh -n install.sh
```

CI runs Linux behavior/unit tests, Node protocol tests on Linux/Windows, and
Windows PowerShell parsing. Real GSA integration requires a permitted Windows/WSL
machine; CI cannot verify authentication or policy.

On an authorized, installed WSL machine, the opt-in integration harness checks
transparent HTTPS, explicit UDP/TCP DNS, local TCP/UDP, an empty-environment root
service, idempotence, recovery, firewall/resolver preservation and no autostart:

```sh
python3 tests/integration.py --docker-bridge
```

It requires the bridge to be OFF initially, temporarily enables it, creates/removes
an isolated test bridge/namespace when requested, and leaves forwarding OFF.
Do not run it alongside other network changes.

## References and license

- [Microsoft: GSA current known limitations](https://learn.microsoft.com/en-us/entra/global-secure-access/reference-current-known-limitations)
- [Microsoft: WSL networking](https://learn.microsoft.com/en-us/windows/wsl/networking)
- [Microsoft: systemd in WSL](https://learn.microsoft.com/en-us/windows/wsl/systemd)

[MIT](LICENSE). Independent community project; not affiliated with Microsoft.
