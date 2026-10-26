#!/usr/bin/python3
"""Privileged manager: owns one nftables table and the bridge's systemd units."""

import argparse
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import sys
import time


NFT = "/usr/sbin/nft"
RUN = Path("/run/wsl-gsa")
LIB = Path("/usr/local/lib/wsl-gsa")
UNITS = ["wsl-gsa-tcp.service", "wsl-gsa-dns.service"]
AUTOSTART = "wsl-gsa-autostart.service"
AUTOSTART_CANCEL = Path("/run/wsl-gsa-autostart.cancelled")


def autostart_status():
    state = command(["/usr/bin/systemctl", "show", "--property=UnitFileState",
                     "--value", AUTOSTART]).strip()
    if state not in ("enabled", "enabled-runtime", "disabled", "masked", "masked-runtime"):
        raise RuntimeError(f"Unexpected autostart unit state: {state!r}. Reinstall the bridge.")
    return {"enabled": state in ("enabled", "enabled-runtime")}


def autostart(enabled):
    if enabled and not json.loads((LIB / "config.json").read_text()).get("windows_paths"):
        raise RuntimeError("Autostart requires saved Windows executable paths. Reinstall the bridge.")
    command(["/usr/bin/systemctl", "enable" if enabled else "disable", AUTOSTART])
    if not enabled:
        AUTOSTART_CANCEL.touch(mode=0o600, exist_ok=True)
    return autostart_status()


def command(args, **kwargs):
    result = subprocess.run(args, capture_output=True, text=True, timeout=30, **kwargs)
    if result.returncode:
        raise RuntimeError(f"{' '.join(args)}: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout


def table_exists():
    data = json.loads(command([NFT, "-j", "list", "tables"]))
    return any(item.get("table", {}).get("family") == "ip" and
               item.get("table", {}).get("name") == "wsl_gsa" for item in data["nftables"])


def unit_active(unit):
    result = subprocess.run(["/usr/bin/systemctl", "is-active", "--quiet", unit],
                            capture_output=True, text=True, timeout=10)
    if result.returncode not in (0, 3):
        raise RuntimeError(f"Cannot inspect {unit}: {result.stderr.strip()}")
    return result.returncode == 0


def status():
    saved = RUN / "state.json"
    return {"rules": table_exists(), "tcp": unit_active(UNITS[0]), "dns": unit_active(UNITS[1]),
            "autostart": autostart_status()["enabled"],
            "state": json.loads(saved.read_text()) if saved.exists() else None}


def stop():
    if table_exists():
        command([NFT, "delete", "table", "ip", "wsl_gsa"])
    errors = []
    for unit in UNITS:
        try:
            command(["/usr/bin/systemctl", "stop", unit])
        except RuntimeError as error:
            errors.append(str(error))
    if errors:
        raise RuntimeError("; ".join(errors))
    (RUN / "state.json").unlink(missing_ok=True)


def local_networks(routes):
    local = ["127.0.0.0/8", "169.254.0.0/16", "224.0.0.0/4", "255.255.255.255/32"]
    local += [str(ipaddress.IPv4Network(route["dst"], strict=False))
              for route in routes if route.get("scope") == "link" and
              route.get("dst") not in (None, "default")]
    return sorted(set(local))


def ruleset(host, local):
    host = str(ipaddress.IPv4Address(host))
    local = [str(ipaddress.IPv4Network(item)) for item in local]
    return f"""table ip wsl_gsa {{
 chain proxy_redirect {{
  meta l4proto {{ tcp, udp }} th dport 53 counter redirect to :1053
  fib daddr type local return
  ip daddr {host} tcp dport 18080 return
  ip daddr {{ {", ".join(local)} }} return
  meta l4proto tcp counter redirect to :12345
 }}
 chain output {{
  type nat hook output priority -110; policy accept;
  jump proxy_redirect
 }}
 chain prerouting {{
  type nat hook prerouting priority -110; policy accept;
  iifname "docker0" jump proxy_redirect
  iifname "br-*" jump proxy_redirect
 }}
 chain input {{
  type filter hook input priority -10; policy accept;
  iifname "lo" return
  ct status dnat return
  meta l4proto {{ tcp, udp }} th dport {{ 1053, 12345 }} counter drop
 }}
}}
"""


def start(host):
    host = str(ipaddress.IPv4Address(host))
    current = status()
    if current["state"] and current["state"]["host"] == host and all(
            current[name] for name in ("rules", "tcp", "dns")):
        return current
    if any(current[name] for name in ("rules", "tcp", "dns", "state")):
        raise RuntimeError("Partial/stale setup. Run gsa off before gsa on.")
    RUN.mkdir(mode=0o755, exist_ok=True)
    routes = json.loads(command(["/usr/sbin/ip", "-j", "-4", "route", "show", "table", "main"]))
    local = local_networks(routes)
    (RUN / "environment").write_text(f"GSA_HOST={host}\n")
    (RUN / "redsocks.conf").write_text(
        "base { log_debug = off; log_info = off; log = stderr; daemon = off; "
        "redirector = iptables; }\n"
        f"redsocks {{ local_ip = 0.0.0.0; local_port = 12345; ip = {host}; "
        "port = 18080; type = http-connect; }\n")
    command(["/usr/sbin/redsocks", "-t", "-c", str(RUN / "redsocks.conf")])
    rules = ruleset(host, local)
    command([NFT, "-c", "-f", "-"], input=rules)
    try:
        command(["/usr/bin/systemctl", "start", *UNITS])
        for _ in range(25):
            sockets = command(["/usr/bin/ss", "-H", "-lntu"])
            if all(unit_active(unit) for unit in UNITS) and ":12345 " in sockets and ":1053 " in sockets:
                break
            time.sleep(0.2)
        else:
            raise RuntimeError("Relay listeners unavailable; inspect journalctl -u wsl-gsa-tcp -u wsl-gsa-dns")
        command([NFT, "-f", "-"], input=rules)
        (RUN / "state.json").write_text(json.dumps({"host": host, "bypass": local}))
        return status()
    except Exception as error:
        try:
            stop()
        except Exception as cleanup:
            raise RuntimeError(f"{error}; rollback failed: {cleanup}") from error
        raise


def uninstall():
    autostart(False)
    command(["/usr/bin/systemctl", "stop", AUTOSTART])
    stop()
    for unit in [*UNITS, AUTOSTART]:
        (Path("/etc/systemd/system") / unit).unlink(missing_ok=True)
    command(["/usr/bin/systemctl", "daemon-reload"])
    Path("/usr/local/bin/gsa").unlink(missing_ok=True)
    for name in ("gsa.py", "system.py", "dns_bridge.py", "autostart.py", "config.json", "command.lock"):
        (LIB / name).unlink(missing_ok=True)
    retained = []
    if any(LIB.iterdir()):
        retained.append(str(LIB))
    else:
        LIB.rmdir()
    for name in ("state.json", "environment", "redsocks.conf"):
        (RUN / name).unlink(missing_ok=True)
    if RUN.exists():
        RUN.rmdir()
    AUTOSTART_CANCEL.unlink(missing_ok=True)
    return {"uninstalled": True, "retained_paths": retained}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("start", "stop", "status", "uninstall",
                                         "autostart-enable", "autostart-disable",
                                         "autostart-status", "autostart-cancel"))
    parser.add_argument("--host")
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise RuntimeError("This helper requires WSL root.")
    with open("/run/wsl-gsa.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if args.action == "autostart-cancel":
            AUTOSTART_CANCEL.touch(mode=0o600, exist_ok=True)
            result = {"cancelled": True}
        elif args.action == "autostart-status":
            result = autostart_status()
        elif args.action in ("autostart-enable", "autostart-disable"):
            result = autostart(args.action == "autostart-enable")
        elif args.action == "start":
            if not args.host:
                parser.error("start requires --host")
            result = start(args.host)
        elif args.action == "uninstall":
            result = uninstall()
        else:
            if args.action == "stop":
                stop()
            result = status()
        print(json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"gsa system: {error}", file=sys.stderr)
        sys.exit(1)
