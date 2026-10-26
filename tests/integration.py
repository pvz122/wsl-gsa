#!/usr/bin/env python3
"""Opt-in real WSL/GSA smoke test. Temporarily enables forwarding; always stops it."""

import argparse
import json
import os
from pathlib import Path
import socket
import socketserver
import subprocess
import sys
import threading
import uuid


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import gsa


def command(args, timeout=120):
    result = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True,
                            text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"{args[0]} failed: {result.stderr.strip()}")
    return result.stdout


def root(*args):
    return command([gsa.executable("wsl.exe"), "--distribution", os.environ["WSL_DISTRO_NAME"],
                    "--user", "root", "--exec", *args])


def check(condition, message):
    if not condition:
        raise RuntimeError(message)
    print(f"PASS {message}", flush=True)


def normalize(value):
    if isinstance(value, dict):
        return {key: ({"packets": 0, "bytes": 0} if key == "counter" else normalize(item))
                for key, item in value.items() if key not in ("metainfo", "handle")}
    if isinstance(value, list):
        return [normalize(item) for item in value]
    return value


def firewall():
    return normalize(json.loads(root("/usr/sbin/nft", "-j", "list", "ruleset")))


def dns_query(tcp=False):
    query = b"\x47\x53\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00" + b"\x03www\x06google\x03com\0\0\1\0\1"
    kind = socket.SOCK_STREAM if tcp else socket.SOCK_DGRAM
    with socket.socket(socket.AF_INET, kind) as sock:
        sock.settimeout(10)
        sock.connect(("8.8.8.8", 53))
        if tcp:
            from dns_bridge import read_exact
            import struct
            sock.sendall(struct.pack("!H", len(query)) + query)
            size = struct.unpack("!H", read_exact(sock, 2))[0]
            answer = read_exact(sock, size)
        else:
            sock.send(query)
            answer = sock.recv(65535)
    check(len(answer) >= 12 and answer[:2] == query[:2] and answer[3] & 15 == 0,
          f"explicit DNS over {'TCP' if tcp else 'connected UDP'}")


class EchoTCP(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.sendall(self.request.recv(1024))


class EchoUDP(socketserver.BaseRequestHandler):
    def handle(self):
        data, sock = self.request
        sock.sendto(data, self.client_address)


def local_echo(host):
    for server_type, handler, kind in (
        (socketserver.TCPServer, EchoTCP, socket.SOCK_STREAM),
        (socketserver.UDPServer, EchoUDP, socket.SOCK_DGRAM),
    ):
        with server_type((host, 0), handler) as server:
            server.timeout = 3
            thread = threading.Thread(target=server.handle_request)
            thread.start()
            try:
                with socket.socket(socket.AF_INET, kind) as client:
                    client.settimeout(3)
                    client.connect(server.server_address)
                    client.sendall(b"direct-local")
                    check(client.recv(1024) == b"direct-local",
                          f"local {'TCP' if kind == socket.SOCK_STREAM else 'non-DNS UDP'} bypass")
            finally:
                thread.join(timeout=4)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docker-bridge", action="store_true",
                        help="Also create and remove an isolated temporary br-* network namespace")
    args = parser.parse_args()
    gsa.validate_wsl()
    check(json.loads(command(["gsa", "status", "--json"]))["state"] == "OFF",
          "bridge initially OFF (refuse to interrupt an active bridge)")
    baseline = firewall()
    resolver = Path("/etc/resolv.conf")
    resolver_before = (os.readlink(resolver) if resolver.is_symlink() else None, resolver.read_bytes())
    ident = uuid.uuid4().hex[:7]
    namespace, bridge, veth, peer = f"gsa-{ident}", f"br-{ident}", f"ga{ident}", f"gb{ident}"
    namespace_created = bridge_created = veth_created = False
    try:
        if args.docker_bridge:
            root("/usr/sbin/ip", "netns", "add", namespace)
            namespace_created = True
            root("/usr/sbin/ip", "link", "add", bridge, "type", "bridge")
            bridge_created = True
            root("/usr/sbin/ip", "addr", "add", "192.0.2.1/30", "dev", bridge)
            root("/usr/sbin/ip", "link", "set", bridge, "up")
            root("/usr/sbin/ip", "link", "add", veth, "type", "veth", "peer", "name", peer)
            veth_created = True
            root("/usr/sbin/ip", "link", "set", veth, "master", bridge)
            root("/usr/sbin/ip", "link", "set", veth, "up")
            root("/usr/sbin/ip", "link", "set", peer, "netns", namespace)
            for operation in (["link", "set", "lo", "up"], ["link", "set", peer, "up"],
                              ["addr", "add", "192.0.2.2/30", "dev", peer],
                              ["route", "add", "default", "via", "192.0.2.1"]):
                root("/usr/sbin/ip", "netns", "exec", namespace, "/usr/sbin/ip", *operation)
        first = json.loads(command(["gsa", "on", "--json"]))
        check(first["state"] == "ON", "activation reports ON")
        second = json.loads(command(["gsa", "on", "--json"]))
        check(first["windows"]["pid"] == second["windows"]["pid"], "repeated on preserves Windows PID")
        status = json.loads(command(["gsa", "status", "--probe", "--json"]))
        check(status["probe"]["ok"], "built-in HTTPS probe without application proxy")
        curl = ["curl", "-q", "-4", "--noproxy", "*", "-sS", "--connect-timeout", "7",
                "--max-time", "15", "-o", "/dev/null", "-w", "%{http_code}"]
        for site in ("https://www.google.com", "https://github.com", "https://www.bing.com"):
            code = command([*curl, site]).strip()
            check(code.isdigit() and 200 <= int(code) < 400, "public HTTPS with TLS verification")
        dns_query()
        dns_query(tcp=True)
        local_echo("127.0.0.1")
        routes = json.loads(command(["ip", "-j", "-4", "route", "get", gsa.gateway()]))
        local_echo(routes[0]["prefsrc"])
        root("/usr/bin/systemd-run", "--quiet", "--wait", "--pipe", "--collect",
             "/usr/bin/env", "-i", "/usr/bin/curl", *curl[1:], "https://www.google.com")
        print("PASS root background service with empty environment", flush=True)
        if args.docker_bridge:
            code = root("/usr/sbin/ip", "netns", "exec", namespace, "/usr/bin/env", "-i",
                        "/usr/bin/curl", *curl[1:], "https://www.google.com").strip()
            check(code == "200", "Docker-style forwarded DNS and HTTPS")
            local_echo("192.0.2.1")
        gsa.windows("stop")
        result = subprocess.run(["gsa", "status", "--json"], capture_output=True, text=True, timeout=45)
        check(result.returncode == 2 and json.loads(result.stdout)["state"] == "DEGRADED",
              "bridge failure detected as DEGRADED")
        recovered = json.loads(command(["gsa", "on", "--json"]))
        check(recovered["state"] == "ON", "on recovers a failed Windows bridge")
        root("/usr/bin/systemctl", "stop", "wsl-gsa-dns.service")
        result = subprocess.run(["gsa", "on", "--json"], capture_output=True, text=True, timeout=45)
        check(result.returncode == 1 and "Partial" in result.stderr,
              "partial Linux state rejected without replacing live rules")
    finally:
        # Remove interception before deleting any test network or stopping the listeners.
        command(["gsa", "off", "--json"])
        if namespace_created:
            root("/usr/sbin/ip", "netns", "delete", namespace)
        elif veth_created:
            root("/usr/sbin/ip", "link", "delete", veth)
        if bridge_created:
            root("/usr/sbin/ip", "link", "delete", bridge)
    check(json.loads(command(["gsa", "off", "--json"]))["state"] == "OFF", "repeated off is idempotent")
    check(firewall() == baseline, "unrelated firewall structure unchanged")
    check((os.readlink(resolver) if resolver.is_symlink() else None, resolver.read_bytes()) == resolver_before,
          "resolver contents and symlink unchanged")
    enabled = root("/usr/bin/systemctl", "show", "-p", "UnitFileState",
                   "wsl-gsa-tcp.service", "wsl-gsa-dns.service")
    check(enabled.count("UnitFileState=static") == 2, "no service autostart")
    print("Integration complete; forwarding is OFF.", flush=True)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        print(f"integration: {error}", file=sys.stderr)
        sys.exit(1)
