#!/usr/bin/python3
"""Bounded boot activation using the installed distro and Windows interop paths."""

import fcntl
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time

import gsa


CANCEL = Path("/run/wsl-gsa-autostart.cancelled")


def prepare_environment(config):
    os.environ["WSL_DISTRO_NAME"] = config["distro"]
    os.environ["PATH"] = os.pathsep.join([
        "/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin", "/sbin", "/bin",
        *config["windows_paths"],
    ])
    sockets = [path for path in Path("/run/WSL").glob("*_interop")
               if stat.S_ISSOCK(path.stat().st_mode)]
    if not sockets:
        raise RuntimeError("Windows interop socket is not ready; open a WSL terminal with interop enabled.")
    os.environ["WSL_INTEROP"] = str(max(sockets, key=lambda path: path.stat().st_mtime_ns))


def activate():
    with (gsa.LIB / "command.lock").open("r") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if (not gsa.system("autostart-status")["enabled"]
                or CANCEL.exists()):
            print("Autostart disabled or cancelled for this boot; skipping activation.", flush=True)
            return
        gsa.validate_wsl()
        linux = gsa.system("status")
        # A Windows relay can survive WSL shutdown with an obsolete NAT address.
        if not any(linux[key] for key in ("rules", "tcp", "dns")):
            gsa.windows("stop")
        result = gsa.enable()
        gsa.display(result, True)
        if result["state"] != "ON":
            raise RuntimeError("Autostart did not reach ON; inspect gsa status.")


def main():
    config = json.loads(gsa.CONFIG.read_text())
    if not config.get("windows_paths"):
        raise RuntimeError("Missing Windows executable paths. Reinstall the bridge.")
    for attempt in range(12):
        if CANCEL.exists():
            print("Autostart cancelled for this boot; skipping activation.", flush=True)
            return
        try:
            prepare_environment(config)
            activate()
            return
        except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
            print(f"Autostart attempt {attempt + 1}/12 failed: {error}", file=sys.stderr, flush=True)
            if attempt == 11:
                raise
            time.sleep(10)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"gsa autostart: {error}", file=sys.stderr)
        sys.exit(1)
