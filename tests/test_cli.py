import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import amp_autopower as amp
from PySide6.QtNetwork import QLocalServer
from amp_autopower import (
    IpcServer,
    MainWindow,
    Schedule,
    build_cli_parser,
    cli_command_from_args,
    format_schedules_cli,
    format_status_cli,
    resolve_schedule,
    schedule_to_dict,
    schedules_from_config,
)


class FakeSignal:
    def __init__(self):
        self.callbacks = []

    def connect(self, callback):
        self.callbacks.append(callback)


class FakeClientSocket:
    def __init__(self, connected=True, response=b""):
        self.connected = connected
        self.response = response
        self.written = b""
        self.server_name = None
        self.disconnected = False

    def connectToServer(self, server_name):
        self.server_name = server_name

    def waitForConnected(self, _timeout):
        return self.connected

    def write(self, payload):
        self.written += bytes(payload)
        return len(payload)

    def flush(self):
        return True

    def waitForBytesWritten(self, _timeout):
        return True

    def waitForReadyRead(self, _timeout):
        return bool(self.response)

    def bytesAvailable(self):
        return len(self.response)

    def readAll(self):
        response = self.response
        self.response = b""
        return response

    def disconnectFromServer(self):
        self.disconnected = True


class FragmentedClientSocket(FakeClientSocket):
    def __init__(self, chunks):
        super().__init__(response=b"")
        self.chunks = list(chunks)

    def waitForReadyRead(self, _timeout):
        return bool(self.chunks)

    def bytesAvailable(self):
        return len(self.chunks[0]) if self.chunks else 0

    def readAll(self):
        return self.chunks.pop(0)


class FakeServerSocket:
    def __init__(self, request):
        self.request = request
        self.written = b""
        self.disconnected = False
        self.readyRead = FakeSignal()
        self.disconnected_signal = FakeSignal()
        self.disconnected = self.disconnected_signal
        self.was_disconnected = False

    def bytesAvailable(self):
        return len(self.request)

    def waitForReadyRead(self, _timeout):
        return True

    def readAll(self):
        request = self.request
        self.request = b""
        return request

    def write(self, payload):
        self.written += bytes(payload)
        return len(payload)

    def flush(self):
        return True

    def waitForBytesWritten(self, _timeout):
        return True

    def disconnectFromServer(self):
        self.was_disconnected = True

    def deleteLater(self):
        pass


class CliWindowHarness:
    handle_cli_command = MainWindow.handle_cli_command
    set_schedule_enabled = MainWindow.set_schedule_enabled

    def __init__(self, schedules=None):
        self.config = deepcopy(amp.DEFAULT_CONFIG)
        self.config["schedules"] = [
            schedule_to_dict(schedule) for schedule in (schedules or [])
        ]
        self.state = deepcopy(amp.DEFAULT_STATE)
        self.compact_display = SimpleNamespace(isVisible=lambda: True)
        self.tray = SimpleNamespace(isVisible=lambda: False)
        self.show_normal = MagicMock()
        self.hide = MagicMock()
        self.toggle_main_window = MagicMock()
        self.scheduler = MagicMock()
        self.set_calls = []

    def schedules(self):
        return schedules_from_config(self.config)

    def set_schedules(self, schedules, restart_interval_ids=None, now=None):
        self.set_calls.append((schedules, restart_interval_ids, now))
        self.config["schedules"] = [schedule_to_dict(item) for item in schedules]


class CliParserTests(unittest.TestCase):
    def test_commands_parse_to_stable_internal_names(self):
        cases = {
            ("--show",): ("show", None),
            ("--hide",): ("hide", None),
            ("--toggle",): ("toggle", None),
            ("--status",): ("status", None),
            ("--list-schedules",): ("list-schedules", None),
            ("--enable", "schedule-id"): ("enable", "schedule-id"),
            ("--disable", "Night"): ("disable", "Night"),
        }
        parser = build_cli_parser()
        for arguments, expected in cases.items():
            with self.subTest(arguments=arguments):
                self.assertEqual(
                    cli_command_from_args(parser.parse_args(arguments)),
                    expected,
                )

    def test_help_is_short_and_has_no_direct_action_options(self):
        help_text = build_cli_parser().format_help()

        for option in (
            "--show",
            "--hide",
            "--toggle",
            "--status",
            "--list-schedules",
            "--enable",
            "--disable",
        ):
            self.assertIn(option, help_text)
        for forbidden in (
            "--poweroff",
            "--reboot",
            "--suspend",
            "--hibernate",
            "--logout",
            "--lock",
            "--run-schedule",
            "--check-update",
        ):
            self.assertNotIn(forbidden, help_text)

    def test_invalid_or_conflicting_arguments_exit_with_code_two(self):
        parser = build_cli_parser()
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as missing:
                parser.parse_args(["--enable"])
            with self.assertRaises(SystemExit) as conflicting:
                parser.parse_args(["--show", "--hide"])
            with self.assertRaises(SystemExit) as forbidden:
                parser.parse_args(["--poweroff"])

        self.assertEqual(missing.exception.code, 2)
        self.assertEqual(conflicting.exception.code, 2)
        self.assertEqual(forbidden.exception.code, 2)

    def test_help_and_version_exit_successfully(self):
        parser = build_cli_parser()
        with redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as help_result:
                parser.parse_args(["--help"])
            with self.assertRaises(SystemExit) as version_result:
                parser.parse_args(["--version"])

        self.assertEqual(help_result.exception.code, 0)
        self.assertEqual(version_result.exception.code, 0)


class CliFormattingTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 2, 12, 0)
        self.active = Schedule(
            id="active-id",
            name="Night task",
            enabled=True,
            action="test",
            time="13:30",
            weekdays=[2],
        )
        self.disabled = Schedule(
            id="disabled-id",
            name="Disabled task",
            enabled=False,
            action="reboot",
            trigger_mode="idle",
            use_time=False,
            idle_minutes=20,
        )

    def test_status_contains_required_compact_fields(self):
        output = format_status_cli(
            amp.DEFAULT_CONFIG,
            amp.DEFAULT_STATE,
            [self.active, self.disabled],
            ipc_available=True,
            display_visible=True,
            tray_visible=False,
            now=self.now,
        )

        self.assertIn(f"Versión: {amp.APP_VERSION}", output)
        self.assertIn("IPC accesible: sí", output)
        self.assertIn("2 total, 1 activas", output)
        self.assertIn("Solo aviso (prueba) | Night task", output)
        self.assertIn("Display compacto: visible", output)
        self.assertIn("Bandeja: oculta", output)

    def test_schedule_list_is_stable_and_not_json(self):
        output = format_schedules_cli(
            [self.active, self.disabled],
            amp.DEFAULT_STATE,
            now=self.now,
        )

        self.assertEqual(
            output.splitlines()[0],
            "ID=active-id | Estado=activa | Nombre=Night task | "
            "Acción=Solo aviso (prueba) | Modo=hora 13:30 | "
            "Próximo=2026-09-02T13:30",
        )
        self.assertIn("ID=disabled-id | Estado=inactiva", output)
        self.assertNotIn("{", output)

    def test_old_config_loads_for_cli_queries(self):
        config = {"schedules": [{"id": "legacy", "name": "Legacy"}]}

        schedules = schedules_from_config(config)

        self.assertEqual(len(schedules), 1)
        self.assertTrue(schedules[0].enabled)
        self.assertEqual(schedules[0].action, "poweroff")

    def test_offline_status_reads_real_legacy_files(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = os.path.join(directory, "config.json")
            state_path = os.path.join(directory, "state.json")
            with open(config_path, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "schedules": [
                            {
                                "id": "legacy",
                                "name": "Legacy",
                                "enabled": True,
                                "action": "test",
                                "time": "13:30",
                                "weekdays": [2],
                            }
                        ]
                    },
                    handle,
                )
            with open(state_path, "w", encoding="utf-8") as handle:
                json.dump({}, handle)
            with (
                patch("amp_autopower.CONFIG_FILE", amp.Path(config_path)),
                patch("amp_autopower.STATE_FILE", amp.Path(state_path)),
            ):
                response = amp._offline_query("status")

        self.assertTrue(response["ok"])
        self.assertIn("IPC accesible: no", response["output"])
        self.assertIn("1 total, 1 activas", response["output"])


class CliScheduleMutationTests(unittest.TestCase):
    def test_enable_and_disable_by_exact_id(self):
        schedule = Schedule(id="one", name="Night", enabled=False)
        window = CliWindowHarness([schedule])

        enabled = window.handle_cli_command("enable", "one")
        self.assertTrue(window.schedules()[0].enabled)
        disabled = window.handle_cli_command("disable", "one")

        self.assertTrue(enabled["ok"])
        self.assertTrue(disabled["ok"])
        self.assertFalse(window.schedules()[0].enabled)

    def test_enable_by_unique_exact_name(self):
        window = CliWindowHarness(
            [Schedule(id="one", name="Exact name", enabled=False)]
        )

        response = window.handle_cli_command("enable", "Exact name")

        self.assertTrue(response["ok"])
        self.assertTrue(window.schedules()[0].enabled)

    def test_duplicate_name_requires_an_id(self):
        schedules = [
            Schedule(id="one", name="Duplicate"),
            Schedule(id="two", name="Duplicate"),
        ]

        target, error, code = resolve_schedule(schedules, "Duplicate")

        self.assertIsNone(target)
        self.assertEqual(code, 2)
        self.assertIn("ambiguo", error)
        self.assertIn("one", error)
        self.assertIn("two", error)

    def test_unknown_id_or_name_is_an_error(self):
        window = CliWindowHarness([Schedule(id="one", name="Known")])

        response = window.handle_cli_command("disable", "missing")

        self.assertFalse(response["ok"])
        self.assertNotEqual(response["code"], 0)
        self.assertIn("No existe", response["output"])

    def test_show_hide_and_toggle_dispatch_without_stopping_any_scheduler(self):
        window = CliWindowHarness()

        for command in ("show", "hide", "toggle"):
            response = window.handle_cli_command(command)
            self.assertTrue(response["ok"])

        window.show_normal.assert_called_once_with()
        window.hide.assert_called_once_with()
        window.toggle_main_window.assert_called_once_with()
        window.scheduler.stop.assert_not_called()

    def test_status_and_list_dispatch_return_text(self):
        window = CliWindowHarness([Schedule(id="one", name="Night")])

        status = window.handle_cli_command("status")
        schedules = window.handle_cli_command("list-schedules")

        self.assertTrue(status["ok"])
        self.assertIn("IPC accesible: sí", status["output"])
        self.assertTrue(schedules["ok"])
        self.assertIn("ID=one", schedules["output"])


class IpcProtocolTests(unittest.TestCase):
    def server(self, socket, window):
        server = IpcServer.__new__(IpcServer)
        server.window = window
        server._buffers = {}
        server._connection_timers = {}
        server.server = SimpleNamespace(nextPendingConnection=lambda: socket)
        return server

    def test_existing_plain_show_message_remains_compatible(self):
        socket = FakeServerSocket(b"show")
        window = SimpleNamespace(show_normal=MagicMock())

        self.server(socket, window).handle_connection()

        window.show_normal.assert_called_once_with()
        self.assertEqual(socket.written, b"")
        self.assertTrue(socket.was_disconnected)

    def test_structured_request_returns_structured_response(self):
        request = json.dumps(
            {"version": 1, "command": "disable", "target": "schedule-id"}
        ).encode("utf-8") + b"\n"
        socket = FakeServerSocket(request)
        window = SimpleNamespace(
            handle_cli_command=MagicMock(
                return_value={"ok": True, "code": 0, "output": "disabled"}
            )
        )

        self.server(socket, window).handle_connection()

        window.handle_cli_command.assert_called_once_with("disable", "schedule-id")
        self.assertEqual(
            json.loads(socket.written.decode("utf-8")),
            {"ok": True, "code": 0, "output": "disabled"},
        )

    def test_fragmented_structured_request_waits_for_newline(self):
        request = json.dumps(
            {"version": 1, "command": "status", "target": None}
        ).encode("utf-8") + b"\n"
        socket = FakeServerSocket(request[:10])
        window = SimpleNamespace(
            handle_cli_command=MagicMock(
                return_value={"ok": True, "code": 0, "output": "ready"}
            )
        )
        server = self.server(socket, window)

        server.handle_connection()
        self.assertEqual(socket.written, b"")
        socket.request = request[10:]
        for callback in socket.readyRead.callbacks:
            callback()

        self.assertEqual(json.loads(socket.written), {
            "ok": True,
            "code": 0,
            "output": "ready",
        })

    def test_invalid_structured_request_is_rejected(self):
        socket = FakeServerSocket(b'{"version":1,"command":42}\n')

        self.server(socket, SimpleNamespace()).handle_connection()

        response = json.loads(socket.written.decode("utf-8"))
        self.assertFalse(response["ok"])
        self.assertEqual(response["code"], 2)

    def test_invalid_protocol_variants_are_rejected(self):
        requests = (
            b"{not-json}\n",
            b'{"version":2,"command":"status"}\n',
            b'{"version":1,"command":"enable","target":42}\n',
            b'{"version":1,"command":"status"}\n{}\n',
            b"x" * (amp.IPC_MAX_REQUEST_BYTES + 1),
        )

        for request in requests:
            with self.subTest(request=request[:30]):
                socket = FakeServerSocket(request)
                self.server(socket, SimpleNamespace()).handle_connection()
                response = json.loads(socket.written)
                self.assertFalse(response["ok"])
                self.assertNotEqual(response["code"], 0)

    def test_send_ipc_waits_for_and_validates_response(self):
        expected = {"ok": True, "code": 0, "output": "status"}
        socket = FakeClientSocket(
            response=json.dumps(expected).encode("utf-8") + b"\n"
        )

        with patch("amp_autopower.QLocalSocket", return_value=socket):
            response = amp.send_ipc("status", expect_response=True)

        self.assertEqual(response, expected)
        request = json.loads(socket.written.decode("utf-8"))
        self.assertEqual(request["command"], "status")
        self.assertEqual(request["version"], 1)

    def test_send_ipc_accepts_fragmented_response(self):
        payload = b'{"ok":true,"code":0,"output":"ready"}\n'
        socket = FragmentedClientSocket([payload[:8], payload[8:21], payload[21:]])

        with patch("amp_autopower.QLocalSocket", return_value=socket):
            response = amp.send_ipc("status", expect_response=True)

        self.assertEqual(response, {"ok": True, "code": 0, "output": "ready"})

    def test_send_ipc_rejects_boolean_exit_code(self):
        socket = FakeClientSocket(
            response=b'{"ok":true,"code":true,"output":"bad"}\n'
        )

        with patch("amp_autopower.QLocalSocket", return_value=socket):
            response = amp.send_ipc("status", expect_response=True)

        self.assertFalse(response["ok"])
        self.assertEqual(response["code"], 1)

    def test_send_ipc_timeout_reports_unknown_result(self):
        socket = FakeClientSocket(connected=True, response=b"")

        with (
            patch("amp_autopower.QLocalSocket", return_value=socket),
            patch("amp_autopower.IPC_TIMEOUT_MS", 1),
        ):
            response = amp.send_ipc(
                "disable",
                "schedule-id",
                expect_response=True,
            )

        self.assertFalse(response["ok"])
        self.assertIn("desconocido", response["output"])

    def test_real_local_socket_round_trip(self):
        socket_name = f"amp-autopower-test-{uuid.uuid4().hex}"
        server_code = """
import os
import sys
from PySide6.QtCore import QCoreApplication, QObject, QTimer
import amp_autopower as amp

amp.IPC_NAME = os.environ["AMP_TEST_IPC_NAME"]
app = QCoreApplication([])

class Window(QObject):
    def handle_cli_command(self, command, target=None):
        QTimer.singleShot(100, app.quit)
        return {
            "ok": command == "disable" and target == "schedule-id",
            "code": 0,
            "output": f"{command}:{target}",
        }

window = Window()
server = amp.IpcServer(window)
if not server.server.isListening():
    print("unavailable", flush=True)
    sys.exit(77)
print("ready", flush=True)
QTimer.singleShot(5000, app.quit)
app.exec()
"""
        environment = os.environ.copy()
        environment["AMP_TEST_IPC_NAME"] = socket_name
        server = subprocess.Popen(
            [sys.executable, "-c", server_code],
            cwd=os.path.dirname(amp.__file__),
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            readiness = server.stdout.readline().strip()
            if readiness == "unavailable":
                server.communicate(timeout=3)
                self.skipTest("QLocalServer no puede escuchar en este entorno")
            self.assertEqual(readiness, "ready")
            with patch("amp_autopower.IPC_NAME", socket_name):
                response = amp.send_ipc(
                    "disable",
                    "schedule-id",
                    expect_response=True,
                )
            _stdout, stderr = server.communicate(timeout=3)
        finally:
            if server.poll() is None:
                server.terminate()
                server.wait(timeout=3)
            QLocalServer.removeServer(socket_name)

        self.assertEqual(stderr, "")
        self.assertEqual(
            response,
            {"ok": True, "code": 0, "output": "disable:schedule-id"},
        )

    def test_daemon_inaccessible_fails_cleanly(self):
        socket = FakeClientSocket(connected=False)

        with patch("amp_autopower.QLocalSocket", return_value=socket):
            self.assertIsNone(amp.send_ipc("status", expect_response=True))
            self.assertFalse(amp.send_ipc("show"))


class MainCliRoutingTests(unittest.TestCase):
    def test_show_keeps_existing_single_instance_behavior(self):
        app = MagicMock()
        lock = MagicMock()
        lock.tryLock.return_value = False
        with (
            patch("amp_autopower.ensure_dirs"),
            patch("amp_autopower.configure_qt_system_theme"),
            patch("amp_autopower.QApplication", return_value=app),
            patch("amp_autopower.apply_system_palette_fallback"),
            patch("amp_autopower.QLockFile", return_value=lock),
            patch("amp_autopower.send_ipc_with_retry", return_value=True) as send,
            patch("amp_autopower.MainWindow") as window,
        ):
            result = amp.main(["--show"])

        self.assertEqual(result, 0)
        send.assert_called_once_with("show")
        window.assert_not_called()

    def test_offline_status_reads_without_starting_a_gui(self):
        output = io.StringIO()
        with (
            patch("amp_autopower.send_ipc_with_retry", return_value=None),
            patch(
                "amp_autopower._offline_query",
                return_value={"ok": True, "code": 0, "output": "offline"},
            ),
            patch("amp_autopower.QApplication") as application,
            redirect_stdout(output),
        ):
            result = amp.main(["--status"])

        self.assertEqual(result, 0)
        self.assertEqual(output.getvalue().strip(), "offline")
        application.assert_not_called()

    def test_offline_list_reads_without_starting_a_gui(self):
        output = io.StringIO()
        with (
            patch("amp_autopower.send_ipc_with_retry", return_value=None),
            patch(
                "amp_autopower._offline_query",
                return_value={"ok": True, "code": 0, "output": "list"},
            ),
            patch("amp_autopower.QApplication") as application,
            redirect_stdout(output),
        ):
            result = amp.main(["--list-schedules"])

        self.assertEqual(result, 0)
        self.assertEqual(output.getvalue().strip(), "list")
        application.assert_not_called()

    def test_offline_mutation_returns_nonzero_on_stderr(self):
        error = io.StringIO()
        with (
            patch("amp_autopower.send_ipc_with_retry", return_value=None),
            patch("amp_autopower.QApplication") as application,
            redirect_stderr(error),
        ):
            result = amp.main(["--disable", "Night"])

        self.assertEqual(result, 1)
        self.assertIn("debe estar ejecutándose", error.getvalue())
        application.assert_not_called()

    def test_daemon_error_exit_code_and_stderr_are_preserved(self):
        error = io.StringIO()
        response = {"ok": False, "code": 2, "output": "ambiguous"}
        with (
            patch("amp_autopower.send_ipc_with_retry", return_value=response),
            redirect_stderr(error),
        ):
            result = amp.main(["--enable", "Duplicate"])

        self.assertEqual(result, 2)
        self.assertEqual(error.getvalue().strip(), "ambiguous")

    def test_real_process_help_version_and_invalid_exit_codes(self):
        script = os.path.join(os.path.dirname(amp.__file__), "amp_autopower.py")
        with tempfile.TemporaryDirectory() as directory:
            environment = os.environ.copy()
            environment["HOME"] = directory
            environment["QT_QPA_PLATFORM"] = "offscreen"
            help_result = subprocess.run(
                [sys.executable, script, "--help"],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            version_result = subprocess.run(
                [sys.executable, script, "--version"],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            invalid_result = subprocess.run(
                [sys.executable, script, "--poweroff"],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(help_result.returncode, 0)
        self.assertIn("--list-schedules", help_result.stdout)
        self.assertEqual(version_result.returncode, 0)
        self.assertEqual(version_result.stdout.strip(), amp.APP_VERSION)
        self.assertEqual(invalid_result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
