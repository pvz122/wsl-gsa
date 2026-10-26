#!/usr/bin/env python3
"""Explicit, repeatable installation; preflight never installs packages."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import gsa


ROOT = Path(__file__).resolve().parent.parent
LIB = Path("/usr/local/lib/wsl-gsa")
UNITS = ("wsl-gsa-tcp.service", "wsl-gsa-dns.service", "wsl-gsa-autostart.service")


def root_install(config):
    if os.geteuid() != 0:
        raise RuntimeError("Internal installation requires root.")
    for unit in UNITS:
        result = subprocess.run(["systemctl", "is-active", "--quiet", unit], check=False)
        if result.returncode == 0:
            raise RuntimeError("Existing bridge is running. Stop it before installing.")
    tables = json.loads(gsa.run(["/usr/sbin/nft", "-j", "list", "tables"])) if shutil.which("nft") else {}
    if any(t.get("table", {}).get("name") == "wsl_gsa" for t in tables.get("nftables", [])):
        raise RuntimeError("Existing bridge rules found. Stop the bridge before installing.")
    if Path("/usr/local/bin/gsa").exists() and not (LIB / "config.json").exists():
        raise RuntimeError("/usr/local/bin/gsa already exists and is not owned by this installer.")
    missing = [pkg for pkg, binary in (("redsocks", "/usr/sbin/redsocks"),
               ("nftables", "/usr/sbin/nft"), ("iproute2", "/usr/sbin/ip"), ("curl", "/usr/bin/curl"))
               if not Path(binary).is_file()]
    if missing:
        if not shutil.which("apt-get"):
            raise RuntimeError("Automatic dependency installation supports Debian/Ubuntu only.")
        stock_mask = Path("/run/systemd/system/redsocks.service")
        masked = False
        if "redsocks" in missing and not stock_mask.is_symlink() and not stock_mask.exists():
            subprocess.run(["systemctl", "mask", "--runtime", "redsocks.service"], check=True)
            masked = True
        try:
            subprocess.run(["apt-get", "update", "-qq"], check=True)
            subprocess.run(["apt-get", "install", "-y", "--no-install-recommends", *missing],
                           env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"}, check=True)
        finally:
            if masked:
                subprocess.run(["systemctl", "unmask", "--runtime", "redsocks.service"], check=True)
        if "redsocks" in missing:
            subprocess.run(["systemctl", "disable", "redsocks.service"], check=True)
    LIB.mkdir(mode=0o755, parents=True, exist_ok=True)
    targets = [(ROOT / "src" / name, LIB / name, 0o644)
               for name in ("gsa.py", "system.py", "dns_bridge.py", "autostart.py")]
    targets += [(ROOT / "systemd" / name, Path("/etc/systemd/system") / name, 0o644)
                for name in UNITS]
    targets.append((ROOT / "src/gsa.py", Path("/usr/local/bin/gsa"), 0o755))
    for source, target, mode in targets:
        if target.is_symlink():
            raise RuntimeError(f"Refusing to replace symlink: {target}")
        target.write_bytes(source.read_bytes().replace(b"\r\n", b"\n"))
        os.chown(target, 0, 0)
        target.chmod(mode)
    config_path = LIB / "config.json"
    if config_path.is_symlink():
        raise RuntimeError("Refusing to replace config symlink.")
    config_path.write_text(json.dumps(config) + "\n")
    os.chown(config_path, 0, 0)
    config_path.chmod(0o644)
    lock_path = LIB / "command.lock"
    if lock_path.is_symlink():
        raise RuntimeError("Refusing to replace command lock symlink.")
    lock_path.touch(mode=0o644, exist_ok=True)
    os.chown(lock_path, 0, 0)
    lock_path.chmod(0o644)
    subprocess.run(["systemctl", "daemon-reload"], check=True)


def main():
    parser = argparse.ArgumentParser(description="Install WSL GSA without enabling forwarding.")
    parser.add_argument("--check", action="store_true", help="Read-only WSL/GSA prerequisite checks")
    parser.add_argument("--root-config", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.root_config:
        root_install(json.loads(args.root_config))
        return
    distro = gsa.validate_wsl()
    host = gsa.gateway()
    os_release = Path("/etc/os-release").read_text()
    if not any(line in os_release.splitlines() for line in ("ID=ubuntu", "ID=debian")):
        raise RuntimeError("This installer currently supports Debian and Ubuntu WSL distributions.")
    setup = gsa.run(["wslpath", "-w", str(ROOT / "windows/setup.ps1")])
    key = hashlib.sha256(distro.encode()).hexdigest()[:16]
    script = f"& {gsa.ps_quote(setup)} -Action check -DistroKey {gsa.ps_quote(key)}"
    gsa.ps_script(script)
    # Verify NAT ownership before downloading or writing anything.
    adapter_check = (
        "$ErrorActionPreference='Stop'; "
        "$a=[Net.NetworkInformation.NetworkInterface]::GetAllNetworkInterfaces() | "
        "Where-Object { $_.Name -like '*WSL*' } | "
        "ForEach-Object { $_.GetIPProperties().UnicastAddresses } | "
        f"Where-Object {{ $_.Address.ToString() -eq '{host}' }}; "
        "if (-not $a) { throw 'A Windows WSL NAT adapter is required.' }; "
        "@{nat=$true}|ConvertTo-Json -Compress")
    gsa.ps_script(adapter_check)
    print("WSL + systemd + Windows interop + NAT + GSA runtime: ready.", flush=True)
    if args.check:
        print("Read-only check complete. GSA sign-in/policy reachability is not proven by runtime detection.")
        return
    print("Installing Windows bridge (Node.js LTS via winget only if missing)...", flush=True)
    installed = gsa.ps_script(
        f"& {gsa.ps_quote(setup)} -Action install -DistroKey {gsa.ps_quote(key)}")
    config = {"distro": distro, "version": gsa.VERSION,
              "windows_paths": sorted({str(Path(gsa.executable(name)).parent)
                                       for name in ("wsl.exe", "powershell.exe")}),
              "windows_control": installed["directory"] + "\\control.ps1"}
    print("Installing Linux dependencies and root-owned services...", flush=True)
    result = subprocess.run(
        [gsa.executable("wsl.exe"), "--distribution", distro, "--user", "root", "--exec",
         "/usr/bin/python3", str(ROOT / "src/install.py"), "--root-config", json.dumps(config)],
        stdin=subprocess.DEVNULL, timeout=900)
    if result.returncode:
        raise RuntimeError("Linux installation failed. Fix the reported error and rerun; Windows files remain installed.")
    print("Installed. Forwarding is OFF; existing autostart preference is preserved (default: disabled).\n\n"
          "  gsa on\n  gsa status --probe\n  gsa off\n\n"
          "Read README.md for scope, trust boundary and uninstall instructions.")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"install: {error}", file=sys.stderr)
        sys.exit(1)
