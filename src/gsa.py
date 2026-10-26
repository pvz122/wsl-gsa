#!/usr/bin/python3
"""Manual WSL-to-Windows GSA bridge. No shell proxy configuration required."""

import argparse
import base64
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request


LIB = Path("/usr/local/lib/wsl-gsa")
CONFIG = LIB / "config.json"
VERSION = "0.1.0"


def run(args, timeout=30, **kwargs):
    result = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True,
                            timeout=timeout, **kwargs)
    if result.returncode:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(stderr or f"{args[0]} exited with code {result.returncode}")
    return result.stdout.decode("utf-8-sig").strip()


def executable(name):
    found = shutil.which(name)
    if not found:
        raise RuntimeError(f"{name} not found. Enable WSL Windows interop and appendWindowsPath.")
    return found


def validate_wsl():
    if sys.version_info < (3, 9):
        raise RuntimeError("Python 3.9 or newer is required.")
    if sys.platform != "linux" or "microsoft" not in Path("/proc/sys/kernel/osrelease").read_text().lower():
        raise RuntimeError("Run this tool inside WSL 2, not Windows or ordinary Linux.")
    distro = os.environ.get("WSL_DISTRO_NAME")
    if not distro:
        raise RuntimeError("WSL_DISTRO_NAME is missing; run from your WSL distribution.")
    if not Path("/run/systemd/system").is_dir():
        raise RuntimeError("A running systemd instance is required. See README prerequisites.")
    executable("wsl.exe")
    executable("powershell.exe")
    executable("wslpath")
    return distro


def ps_script(script):
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return json.loads(run([executable("powershell.exe"), "-NoProfile", "-NonInteractive",
                           "-EncodedCommand", encoded], timeout=300))


def ps_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def windows(action, host=None, control=None):
    if control is None:
        control = json.loads(CONFIG.read_text())["windows_control"]
    args = [executable("powershell.exe"), "-NoProfile", "-NonInteractive",
            "-File", control, "-Action", action]
    if host:
        args += ["-HostAddress", host]
    return json.loads(run(args, timeout=45))


def gateway():
    routes = json.loads(run(["ip", "-j", "-4", "route", "show", "default"]))
    host = next((r.get("gateway") for r in routes if r.get("gateway")), None)
    if not host:
        raise RuntimeError("No IPv4 NAT gateway found. Mirrored/virtioproxy networking is not supported.")
    return str(ipaddress.IPv4Address(host))


def system(action, host=None):
    args = [executable("wsl.exe"), "--distribution", os.environ["WSL_DISTRO_NAME"],
            "--user", "root", "--exec", "/usr/bin/python3", str(LIB / "system.py"), action]
    if host:
        args += ["--host", host]
    return json.loads(run(args, timeout=90))


def health(state):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"http://{state['host']}:18080/__wsl_gsa_health", timeout=3) as response:
        data = json.load(response)
    if (data.get("application") != "wsl-gsa-proxy" or data.get("pid") != state["pid"]
            or data.get("version") != 3
            or not {"tcp-connect", "dns-wire"}.issubset(data.get("capabilities", []))):
        raise RuntimeError("Unexpected bridge identity/version; run gsa off and reinstall.")


def enable():
    before = system("status")
    win_before = windows("status")
    if any(before[k] for k in ("rules", "tcp", "dns")) and not all(
            before[k] for k in ("rules", "tcp", "dns")):
        raise RuntimeError("Partial Linux state detected. Run gsa off, then gsa on.")
    try:
        state = windows("start", gateway())
        for attempt in range(10):
            try:
                health(state)
                break
            except (OSError, ValueError, RuntimeError, urllib.error.URLError):
                if attempt == 9:
                    raise RuntimeError("Windows bridge unreachable. Check firewall and gsa status.") from None
                time.sleep(0.3)
        system("start", state["host"])
    except Exception as error:
        errors = []
        if not before["rules"]:
            try:
                system("stop")
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as cleanup:
                errors.append(str(cleanup))
        if not win_before["running"] and not errors:
            try:
                windows("stop")
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as cleanup:
                errors.append(str(cleanup))
        if errors:
            raise RuntimeError(f"{error}; rollback failed: {'; '.join(errors)}") from error
        raise
    return get_status()


def get_status():
    linux = system("status")
    win = windows("status")
    healthy = False
    errors = []
    ready = False
    try:
        windows("check")
        ready = True
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as error:
        errors.append(str(error))
    if win["running"]:
        try:
            health(win)
            healthy = True
        except (OSError, RuntimeError, ValueError, urllib.error.URLError) as error:
            errors.append(str(error))
    on = all(linux[k] for k in ("rules", "tcp", "dns")) and healthy and ready
    off = not any(linux[k] for k in ("rules", "tcp", "dns")) and not win["running"]
    return {"state": "ON" if on else "OFF" if off else "DEGRADED",
            "linux": linux, "windows": win, "reachable": healthy,
            "gsa_ready": ready, "errors": errors}


def probe(result):
    if not shutil.which("curl"):
        raise RuntimeError("curl is required for --probe.")
    output = subprocess.run(
        ["curl", "-q", "-4", "-sS", "-I", "--noproxy", "*", "--connect-timeout", "5",
         "--max-time", "12", "-o", "/dev/null", "-w", "%{http_code}", "https://www.microsoft.com"],
        stdin=subprocess.DEVNULL, capture_output=True, timeout=15)
    code = output.stdout.decode().strip()
    ok = output.returncode == 0 and code.isdigit() and 200 <= int(code) < 400
    result["probe"] = {"ok": ok, "http_status": code,
                       "error": output.stderr.decode(errors="replace").strip()}
    if not ok and result["state"] == "ON":
        result["state"] = "DEGRADED"


def display(result, as_json):
    if as_json:
        print(json.dumps(result))
        return
    color = sys.stdout.isatty() and "NO_COLOR" not in os.environ
    state = result["state"]
    shade = {"ON": "32", "OFF": "36", "DEGRADED": "33"}[state]
    badge = f"\033[1;{shade}m{state}\033[0m" if color else state
    print(f"WSL GSA  /  {badge}\n")
    rows = [
        ("Windows bridge", "reachable" if result["reachable"] else
         "unreachable" if result["windows"]["running"] else "stopped"),
        ("GSA runtime", "detected" if result["gsa_ready"] else "not ready"),
        ("TCP relay", "active" if result["linux"]["tcp"] else "stopped"),
        ("DNS relay", "active" if result["linux"]["dns"] else "stopped"),
        ("IPv4 rules", "installed" if result["linux"]["rules"] else "absent"),
        ("Autostart", "never"),
    ]
    for label, value in rows:
        print(f"  {label:<18} {value}")
    for error in result["errors"]:
        print(f"\n  ! {error}")
    if "probe" in result:
        print(f"\n  HTTPS probe: {'passed' if result['probe']['ok'] else 'FAILED'} "
              f"(HTTP {result['probe']['http_status']}; application proxies disabled)")
    print("\nNew IPv4 TCP + UDP/TCP DNS :53; Docker bridges included.")
    print("Loopback/connected subnets direct for TCP; other UDP and IPv6 unchanged.")


def main():
    parser = argparse.ArgumentParser(
        prog="gsa", description="Bridge new WSL IPv4 TCP + DNS through Windows GSA.",
        epilog="Manual only. No default action, autostart, GSA policy changes or TLS interception.")
    parser.add_argument("--version", action="version", version=VERSION)
    sub = parser.add_subparsers(dest="action", required=True)
    for action, help_text in (("on", "Enable transparent forwarding"),
                              ("off", "Remove rules FIRST, then stop the bridge"),
                              ("status", "Inspect bridge health (exit 2 when degraded)")):
        item = sub.add_parser(action, help=help_text)
        item.add_argument("--json", action="store_true", help="Machine-readable output")
        if action == "status":
            item.add_argument("--probe", action="store_true", help="Also test one public HTTPS endpoint")
    sub.add_parser("uninstall", help="Stop and remove tool files; leave dependencies installed")
    args = parser.parse_args()
    validate_wsl()
    config = json.loads(CONFIG.read_text())
    if config["distro"] != os.environ["WSL_DISTRO_NAME"]:
        raise RuntimeError("Installation belongs to a different distro. Reinstall in this distro.")
    with (LIB / "command.lock").open("r") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if args.action == "uninstall":
            system("stop")
            windows("stop")
            win = windows("uninstall")
            linux = system("uninstall")
            print("Uninstalled. Dependencies and shell configuration were left unchanged.")
            for path in win.get("retained_paths", []) + linux.get("retained_paths", []):
                print(f"Kept directory containing unknown files: {path}")
            return 0
        if args.action == "on":
            result = enable()
        else:
            if args.action == "off":
                system("stop")
                windows("stop")
            result = get_status()
        if getattr(args, "probe", False):
            probe(result)
        display(result, args.json)
        return 2 if result["state"] == "DEGRADED" else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"gsa: {error}", file=sys.stderr)
        sys.exit(1)
