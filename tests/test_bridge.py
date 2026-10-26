import concurrent.futures
import importlib.util
import io
import json
from pathlib import Path
import socket
import struct
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch


SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
import gsa
import autostart
import dns_bridge
import install

spec = importlib.util.spec_from_file_location("system_bridge", SRC / "system.py")
system = importlib.util.module_from_spec(spec)
spec.loader.exec_module(system)


def linux_state(rules=False, tcp=False, dns=False, state=None):
    return dict(rules=rules, tcp=tcp, dns=dns, state=state, autostart=False)


class RuleTests(unittest.TestCase):
    def test_only_owned_table_and_ipv4(self):
        rules = system.ruleset("192.0.2.1", ["127.0.0.0/8", "172.18.0.0/16"])
        self.assertIn("table ip wsl_gsa", rules)
        self.assertNotIn("flush", rules)
        self.assertNotIn("inet ", rules)
        self.assertNotIn("ip6", rules)
        self.assertIn('iifname "docker0"', rules)
        self.assertIn('iifname "br-*"', rules)
        self.assertIn("ct status dnat return", rules)
        self.assertIn("priority -110", rules)

    def test_dns_then_local_bypass_then_tcp(self):
        rules = system.ruleset("192.0.2.1", ["127.0.0.0/8"])
        self.assertLess(rules.index("dport 53"), rules.index("fib daddr"))
        self.assertLess(rules.index("fib daddr"), rules.index("redirect to :12345"))
        self.assertLess(rules.index("192.0.2.1 tcp dport 18080 return"),
                        rules.index("redirect to :12345"))
        self.assertNotIn("meta l4proto udp counter redirect to :12345", rules)

    def test_untrusted_address_rejected(self):
        for host in ("::1", "1.2.3.4; flush ruleset", "hostname"):
            with self.subTest(host=host), self.assertRaises(ValueError):
                system.ruleset(host, ["127.0.0.0/8"])

    def test_only_connected_routes_bypassed(self):
        routes = [{"dst": "172.18.0.0/16", "scope": "link"},
                  {"dst": "default", "gateway": "192.0.2.1"},
                  {"dst": "198.51.100.0/24", "gateway": "192.0.2.1"}]
        local = system.local_networks(routes)
        self.assertIn("172.18.0.0/16", local)
        self.assertIn("127.0.0.0/8", local)
        self.assertNotIn("198.51.100.0/24", local)

    @patch.object(system, "command")
    @patch.object(system, "table_exists", return_value=True)
    def test_stop_deletes_table_before_services(self, exists, command):
        events = []
        command.side_effect = lambda args, **kw: events.append(args) or ""
        with tempfile.TemporaryDirectory() as folder, patch.object(system, "RUN", Path(folder)):
            system.stop()
        self.assertEqual(events[0], [system.NFT, "delete", "table", "ip", "wsl_gsa"])
        self.assertEqual(len(events), 3)

    @patch.object(system, "command", side_effect=RuntimeError("delete failed"))
    @patch.object(system, "table_exists", return_value=True)
    def test_stop_keeps_services_if_rule_removal_fails(self, exists, command):
        with self.assertRaisesRegex(RuntimeError, "delete failed"):
            system.stop()
        self.assertEqual(command.call_count, 1)

    @patch.object(system, "status")
    def test_start_is_idempotent(self, status):
        status.return_value = linux_state(True, True, True, {"host": "192.0.2.1"})
        self.assertEqual(system.start("192.0.2.1"), status.return_value)

    @patch.object(system, "status")
    def test_start_rejects_partial_state(self, status):
        status.return_value = linux_state(True)
        with self.assertRaisesRegex(RuntimeError, "Partial"):
            system.start("192.0.2.1")

    def test_units_are_manual_and_unprivileged(self):
        for unit in (SRC.parent / "systemd").glob("*.service"):
            if unit.name == system.AUTOSTART:
                continue
            text = unit.read_text()
            self.assertNotIn("[Install]", text)
            self.assertIn("DynamicUser=yes", text)
            self.assertIn("NoNewPrivileges=yes", text)


class AutostartTests(unittest.TestCase):
    def test_boot_unit_is_opt_in_and_bounded(self):
        text = (SRC.parent / "systemd" / system.AUTOSTART).read_text()
        self.assertIn("WantedBy=multi-user.target", text)
        self.assertIn("After=network-online.target", text)
        self.assertIn("TimeoutStartSec=15min", text)
        self.assertNotIn("Restart=always", text)
        self.assertIn(system.AUTOSTART, install.UNITS)

    @patch.object(system, "command")
    def test_enabled_status_and_inspection_errors(self, command):
        for state, enabled in (("enabled\n", True), ("disabled\n", False), ("masked\n", False)):
            command.return_value = state
            self.assertEqual(system.autostart_status(), {"enabled": enabled})
        command.return_value = ""
        with self.assertRaisesRegex(RuntimeError, "Reinstall"):
            system.autostart_status()
        command.side_effect = RuntimeError("inspection failed")
        with self.assertRaisesRegex(RuntimeError, "inspection failed"):
            system.autostart_status()

    @patch.object(system, "autostart_status", return_value={"enabled": True})
    @patch.object(system, "command")
    def test_enable_does_not_activate_forwarding(self, command, status):
        with patch.object(Path, "read_text", return_value='{"windows_paths":["/windows"]}'):
            self.assertEqual(system.autostart(True), {"enabled": True})
        command.assert_called_once_with(["/usr/bin/systemctl", "enable", system.AUTOSTART])

    @patch.object(system, "command", side_effect=RuntimeError("enable failed"))
    def test_enable_failure_is_reported(self, command):
        with patch.object(Path, "read_text", return_value='{"windows_paths":["/windows"]}'), \
                self.assertRaisesRegex(RuntimeError, "enable failed"):
            system.autostart(True)

    @patch.object(system, "autostart_status", return_value={"enabled": False})
    @patch.object(system, "command")
    def test_disable_cancels_pending_boot_without_stopping_forwarding(self, command, status):
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(system, "AUTOSTART_CANCEL", Path(folder) / "cancelled"):
            system.autostart(False)
            self.assertTrue(system.AUTOSTART_CANCEL.exists())
        command.assert_called_once_with(["/usr/bin/systemctl", "disable", system.AUTOSTART])

    @patch.object(autostart, "activate")
    @patch.object(autostart, "prepare_environment")
    @patch.object(autostart.time, "sleep")
    def test_boot_retries_then_succeeds(self, sleep, prepare, activate):
        activate.side_effect = [RuntimeError("GSA not ready"), None]
        with patch.object(Path, "read_text", return_value='{"windows_paths":["/windows"]}'), \
                patch("sys.stderr", new_callable=io.StringIO) as output:
            autostart.main()
        self.assertEqual(activate.call_count, 2)
        sleep.assert_called_once_with(10)
        self.assertIn("GSA not ready", output.getvalue())

    @patch.object(autostart, "activate")
    @patch.object(autostart, "prepare_environment", side_effect=RuntimeError("interop unavailable"))
    @patch.object(autostart.time, "sleep")
    def test_boot_eventually_fails_explicitly(self, sleep, prepare, activate):
        with patch.object(Path, "read_text", return_value='{"windows_paths":["/windows"]}'), \
                patch("sys.stderr", new_callable=io.StringIO), \
                self.assertRaisesRegex(RuntimeError, "interop unavailable"):
            autostart.main()
        self.assertEqual(prepare.call_count, 12)
        self.assertEqual(sleep.call_count, 11)
        activate.assert_not_called()

    def test_boot_restores_environment_from_saved_paths_and_socket(self):
        with tempfile.TemporaryDirectory() as folder, \
                socket.socket(socket.AF_UNIX) as listener:
            path = Path(folder) / "42_interop"
            listener.bind(str(path))
            with patch.object(Path, "glob", return_value=[path]), \
                    patch.dict(gsa.os.environ, {}, clear=True):
                autostart.prepare_environment({"distro": "TestDistro", "windows_paths": ["/windows"]})
                self.assertEqual(gsa.os.environ["WSL_DISTRO_NAME"], "TestDistro")
                self.assertEqual(gsa.os.environ["WSL_INTEROP"], str(path))
                self.assertIn("/windows", gsa.os.environ["PATH"].split(":"))

    @patch.object(gsa, "validate_wsl")
    @patch.object(gsa, "enable", return_value={"state": "ON"})
    @patch.object(gsa, "display")
    @patch.object(gsa, "windows")
    @patch.object(gsa, "system")
    def test_boot_replaces_windows_relay_left_by_previous_wsl_session(
            self, system_call, windows, display, enable, validate):
        system_call.side_effect = [{"enabled": True}, linux_state()]
        with tempfile.TemporaryDirectory() as folder, patch.object(gsa, "LIB", Path(folder)), \
                patch.object(Path, "exists", return_value=False):
            (gsa.LIB / "command.lock").touch()
            autostart.activate()
        windows.assert_called_once_with("stop")
        enable.assert_called_once_with()

    @patch.object(gsa, "enable")
    @patch.object(gsa, "system", return_value={"enabled": True})
    def test_manual_off_cancels_pending_boot(self, system_call, enable):
        with tempfile.TemporaryDirectory() as folder, patch.object(gsa, "LIB", Path(folder)), \
                patch.object(Path, "exists", return_value=True), \
                patch("sys.stdout", new_callable=io.StringIO):
            (gsa.LIB / "command.lock").touch()
            autostart.activate()
        enable.assert_not_called()

    @patch.object(gsa.os, "geteuid", return_value=0)
    @patch.object(gsa, "run", return_value='{"enabled":false}')
    def test_root_service_does_not_reenter_wsl(self, run, uid):
        self.assertEqual(gsa.system("autostart-status"), {"enabled": False})
        self.assertEqual(run.call_args.args[0][0], "/usr/bin/python3")

    @patch.object(gsa.os, "geteuid", return_value=1000)
    @patch.object(gsa, "executable", return_value="/windows/wsl.exe")
    @patch.object(gsa, "run", return_value='{"enabled":false}')
    def test_normal_user_keeps_wsl_root_elevation(self, run, executable, uid):
        with patch.dict(gsa.os.environ, {"WSL_DISTRO_NAME": "TestDistro"}):
            gsa.system("autostart-status")
        self.assertEqual(run.call_args.args[0][:6],
                         ["/windows/wsl.exe", "--distribution", "TestDistro",
                          "--user", "root", "--exec"])

    @patch.object(gsa, "validate_wsl")
    @patch.object(gsa, "system", return_value={"enabled": True})
    @patch.object(gsa, "windows")
    def test_cli_autostart_json_does_not_start_windows(self, windows, system_call, validate):
        with tempfile.TemporaryDirectory() as folder, patch.object(gsa, "LIB", Path(folder)), \
                patch.object(gsa, "CONFIG", Path(folder) / "config.json"), \
                patch.dict(gsa.os.environ, {"WSL_DISTRO_NAME": "TestDistro"}), \
                patch.object(sys, "argv", ["gsa", "autostart", "on", "--json"]), \
                patch("sys.stdout", new_callable=io.StringIO) as output:
            gsa.CONFIG.write_text('{"distro":"TestDistro"}')
            (gsa.LIB / "command.lock").touch()
            self.assertEqual(gsa.main(), 0)
            self.assertEqual(json.loads(output.getvalue()), {"enabled": True})
        system_call.assert_called_once_with("autostart-enable")
        windows.assert_not_called()

    @patch.object(gsa, "validate_wsl")
    @patch.object(gsa, "get_status", return_value={"state": "OFF"})
    @patch.object(gsa, "system")
    @patch.object(gsa, "windows")
    def test_cli_off_cancels_startup_before_removing_rules(self, windows, system_call, status, validate):
        events = []
        system_call.side_effect = lambda action: events.append(action)
        windows.side_effect = lambda action: events.append("windows-" + action)
        with tempfile.TemporaryDirectory() as folder, patch.object(gsa, "LIB", Path(folder)), \
                patch.object(gsa, "CONFIG", Path(folder) / "config.json"), \
                patch.dict(gsa.os.environ, {"WSL_DISTRO_NAME": "TestDistro"}), \
                patch.object(sys, "argv", ["gsa", "off", "--json"]), \
                patch("sys.stdout", new_callable=io.StringIO):
            gsa.CONFIG.write_text('{"distro":"TestDistro"}')
            (gsa.LIB / "command.lock").touch()
            self.assertEqual(gsa.main(), 0)
        self.assertEqual(events, ["autostart-cancel", "stop", "windows-stop"])

    @patch.object(gsa, "validate_wsl")
    @patch.object(gsa, "enable", return_value={"state": "ON"})
    @patch.object(gsa, "display")
    @patch.object(gsa, "windows")
    @patch.object(gsa, "system")
    def test_boot_does_not_stop_an_already_active_bridge(
            self, system_call, windows, display, enable, validate):
        system_call.side_effect = [{"enabled": True}, linux_state(True, True, True)]
        with tempfile.TemporaryDirectory() as folder, patch.object(gsa, "LIB", Path(folder)), \
                patch.object(Path, "exists", return_value=False):
            (gsa.LIB / "command.lock").touch()
            autostart.activate()
        windows.assert_not_called()
        enable.assert_called_once_with()

    @patch.object(system, "autostart")
    @patch.object(system, "stop")
    @patch.object(system, "command")
    def test_uninstall_disables_and_removes_boot_files(self, command, stop, auto):
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(system, "LIB", Path(folder) / "lib"), \
                patch.object(system, "RUN", Path(folder) / "run"), \
                patch.object(system, "AUTOSTART_CANCEL", Path(folder) / "cancelled"), \
                patch.object(Path, "unlink", autospec=True) as unlink:
            system.LIB.mkdir()
            boot_script = system.LIB / "autostart.py"
            result = system.uninstall()
        self.assertTrue(result["uninstalled"])
        auto.assert_called_once_with(False)
        removed = [call.args[0] for call in unlink.call_args_list]
        self.assertIn(boot_script, removed)
        self.assertIn(Path("/etc/systemd/system") / system.AUTOSTART, removed)


class CLITests(unittest.TestCase):
    @patch.object(gsa, "windows")
    @patch.object(gsa, "system", return_value=linux_state())
    def test_off_state(self, system_call, windows):
        windows.side_effect = [{"running": False}, {"ready": True}]
        self.assertEqual(gsa.get_status()["state"], "OFF")

    @patch.object(gsa, "health")
    @patch.object(gsa, "windows")
    @patch.object(gsa, "system", return_value=linux_state(True, True, True))
    def test_on_state(self, system_call, windows, health):
        windows.side_effect = [{"running": True, "host": "192.0.2.1", "pid": 42}, {"ready": True}]
        self.assertEqual(gsa.get_status()["state"], "ON")

    @patch.object(gsa, "health", side_effect=RuntimeError("unreachable"))
    @patch.object(gsa, "windows")
    @patch.object(gsa, "system", return_value=linux_state(True, True, True))
    def test_bridge_failure_is_degraded(self, system_call, windows, health):
        windows.side_effect = [{"running": True}, {"ready": True}]
        self.assertEqual(gsa.get_status()["state"], "DEGRADED")

    @patch.object(gsa, "health")
    @patch.object(gsa, "windows")
    @patch.object(gsa, "system", return_value=linux_state(True, True, True))
    def test_gsa_failure_is_degraded(self, system_call, windows, health):
        windows.side_effect = [{"running": True}, RuntimeError("GSA stopped")]
        result = gsa.get_status()
        self.assertEqual(result["state"], "DEGRADED")
        self.assertIn("GSA stopped", result["errors"])

    @patch.object(gsa, "system", return_value=linux_state(True, False, True))
    @patch.object(gsa, "windows", return_value={"running": True})
    def test_partial_state_not_replaced(self, windows, system_call):
        with self.assertRaisesRegex(RuntimeError, "Partial"):
            gsa.enable()
        self.assertEqual(system_call.call_args_list[0].args, ("status",))
        self.assertEqual(system_call.call_count, 1)

    @patch.object(gsa, "gateway", return_value="192.0.2.1")
    @patch.object(gsa, "health", side_effect=RuntimeError("bad identity"))
    @patch.object(gsa.time, "sleep")
    @patch.object(gsa, "windows")
    @patch.object(gsa, "system")
    def test_start_failure_rolls_back_new_components(self, system_call, windows, sleep, health, gateway):
        system_call.return_value = linux_state()
        windows.side_effect = [{"running": False},
                               {"running": True, "host": "192.0.2.1", "pid": 42}, {"running": False}]
        with self.assertRaisesRegex(RuntimeError, "unreachable"):
            gsa.enable()
        self.assertEqual(system_call.call_args_list[-1].args, ("stop",))
        self.assertEqual(windows.call_args_list[-1].args, ("stop",))

    def test_json_is_plain_and_parseable(self):
        result = {"state": "OFF", "errors": []}
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            gsa.display(result, True)
            self.assertEqual(json.loads(output.getvalue()), result)
            self.assertNotIn("\033", output.getvalue())

    def test_powershell_literal_escaping(self):
        self.assertEqual(gsa.ps_quote("a'b"), "'a''b'")

    @patch.object(gsa, "run", return_value='[{"gateway":"192.0.2.1"}]')
    def test_gateway_discovery(self, run):
        self.assertEqual(gsa.gateway(), "192.0.2.1")

    @patch.object(gsa, "run", return_value="[]")
    def test_missing_gateway_is_actionable(self, run):
        with self.assertRaisesRegex(RuntimeError, "NAT gateway"):
            gsa.gateway()

    @patch.object(gsa.shutil, "which", return_value="/usr/bin/curl")
    @patch.object(gsa.subprocess, "run")
    def test_probe_bypasses_application_proxies(self, run, which):
        run.return_value = Mock(returncode=0, stdout=b"200", stderr=b"")
        result = {"state": "ON"}
        gsa.probe(result)
        args = run.call_args.args[0]
        self.assertEqual(args[args.index("--noproxy") + 1], "*")
        self.assertIn("-4", args)
        self.assertTrue(result["probe"]["ok"])


class DNSTests(unittest.TestCase):
    QUERY = b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00" + b"\x07example\x03com\0\0\1\0\1"

    def test_servfail_preserves_question_and_id(self):
        answer = dns_bridge.failure(self.QUERY)
        self.assertEqual(answer[:2], self.QUERY[:2])
        self.assertEqual(answer[3] & 15, 2)
        self.assertEqual(answer[12:], self.QUERY[12:])
        self.assertEqual(answer[6:12], b"\0" * 6)
        self.assertEqual(dns_bridge.failure(b"short"), b"")

    def test_read_exact_handles_fragments(self):
        sock = Mock()
        sock.recv.side_effect = [b"a", b"bc"]
        self.assertEqual(dns_bridge.read_exact(sock, 3), b"abc")

    def test_read_exact_eof(self):
        sock = Mock()
        sock.recv.return_value = b""
        with self.assertRaises(EOFError):
            dns_bridge.read_exact(sock, 2)

    @patch.object(dns_bridge.http.client, "HTTPConnection")
    def test_invalid_reply_becomes_servfail(self, connection):
        response = Mock(status=200)
        response.read.return_value = b"\0" * 12
        connection.return_value.getresponse.return_value = response
        with self.assertLogs(level="ERROR"):
            self.assertEqual(dns_bridge.answer(self.QUERY), dns_bridge.failure(self.QUERY))
        connection.return_value.close.assert_called_once()

    @unittest.skipUnless(sys.platform == "linux", "Linux IP_PKTINFO")
    def test_udp_preserves_queried_local_address(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool, \
                patch.object(dns_bridge, "POOL", pool), \
                patch.object(dns_bridge, "answer", side_effect=lambda data: data), \
                dns_bridge.UDPServer(("0.0.0.0", 0), dns_bridge.UDPHandler) as server, \
                socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
            thread = threading.Thread(target=server.handle_request)
            thread.start()
            client.settimeout(2)
            # A plain sendto() would reply from 127.0.0.1, not the connected peer.
            client.connect(("127.0.0.2", server.server_address[1]))
            client.send(self.QUERY)
            self.assertEqual(client.recv(4096), self.QUERY)
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())


class InstallerTests(unittest.TestCase):
    @patch.object(install.gsa, "ps_script")
    @patch.object(install.gsa, "run", return_value="C:\\source\\setup.ps1")
    @patch.object(install.gsa, "gateway", return_value="192.0.2.1")
    @patch.object(install.gsa, "validate_wsl", return_value="TestDistro")
    @patch.object(Path, "read_text", return_value="ID=ubuntu\n")
    @patch.object(install.subprocess, "run")
    def test_check_never_installs(self, run, read, wsl, gateway, command, ps):
        with patch.object(sys, "argv", ["install.py", "--check"]), \
                patch("sys.stdout", new_callable=io.StringIO):
            install.main()
        run.assert_not_called()
        self.assertEqual(ps.call_count, 2)
        self.assertIn("-Action check", ps.call_args_list[0].args[0])

    @patch.object(install.gsa, "validate_wsl", side_effect=RuntimeError("Not WSL"))
    @patch.object(install.gsa, "ps_script")
    def test_wsl_checked_before_powershell(self, ps, wsl):
        with patch.object(sys, "argv", ["install.py"]), self.assertRaisesRegex(RuntimeError, "Not WSL"):
            install.main()
        ps.assert_not_called()

    @patch.object(install.gsa, "ps_script", side_effect=RuntimeError("GSA missing"))
    @patch.object(install.gsa, "run", return_value="C:\\source\\setup.ps1")
    @patch.object(install.gsa, "gateway", return_value="192.0.2.1")
    @patch.object(install.gsa, "validate_wsl", return_value="TestDistro")
    @patch.object(Path, "read_text", return_value="ID=debian\n")
    @patch.object(install.subprocess, "run")
    def test_gsa_failure_prevents_installation(self, run, read, wsl, gateway, command, ps):
        with patch.object(sys, "argv", ["install.py"]), self.assertRaisesRegex(RuntimeError, "GSA missing"):
            install.main()
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
