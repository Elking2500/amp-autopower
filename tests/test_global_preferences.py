import json
import os
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QKeySequence, QPalette
from PySide6.QtWidgets import QApplication

from amp_autopower import (
    APP_ID,
    DEFAULT_CONFIG,
    DEFAULT_STATE,
    GlobalShortcutManager,
    IpcServer,
    MainWindow,
    display_options_from_config,
    load_json,
)


class FakeSocket:
    def __init__(self, command):
        self.command = command
        self.disconnected = False

    def waitForReadyRead(self, _timeout):
        return True

    def readAll(self):
        return self.command.encode("utf-8")

    def disconnectFromServer(self):
        self.disconnected = True


class FakeSignal:
    def __init__(self):
        self.callbacks = []

    def connect(self, callback):
        self.callbacks.append(callback)

    def emit(self, *args):
        for callback in list(self.callbacks):
            callback(*args)


class GlobalPreferencesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(
            ["amp-autopower-global-preferences-test"]
        )

    def setUp(self):
        self.windows = []

    def tearDown(self):
        for window in self.windows:
            window.scheduler.stop()
            window.activity_ui_timer.stop()
            disable_shortcut = getattr(
                window.global_shortcut,
                "disable",
                None,
            )
            if disable_shortcut is not None:
                disable_shortcut()
            window.compact_display.hide()
            window.tray.hide()
            window.hide()
            window.deleteLater()
        self.app.processEvents()

    def window(self, **updates):
        config = deepcopy(DEFAULT_CONFIG)
        config.update(
            {
                "schedules": [],
                "display_enabled": False,
                "input_monitor_enabled": False,
                "notifications": False,
                "sound": False,
                "auto_check_updates": False,
            }
        )
        config.update(updates)
        state = deepcopy(DEFAULT_STATE)
        with (
            patch(
                "amp_autopower.load_json",
                side_effect=[config, state],
            ),
            patch("amp_autopower.save_json"),
        ):
            window = MainWindow(self.app)
        self.windows.append(window)
        return window

    def test_old_config_gets_safe_global_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps({"start_minimized": False}),
                encoding="utf-8",
            )
            loaded = load_json(path, DEFAULT_CONFIG)

        self.assertTrue(loaded["tray_visible"])
        self.assertEqual(loaded["global_hotkey"], "")

    def test_preferences_group_existing_display_settings_without_duplication(self):
        window = self.window(
            display_always_on_top=False,
            display_show_title=False,
            display_show_action=False,
            display_transparency_percent=63,
            display_use_24_hour=False,
        )

        self.assertEqual(
            [
                window.preferences_tabs.tabText(index)
                for index in range(window.preferences_tabs.count())
            ],
            ["General", "Display", "Atajos", "Comportamiento"],
        )
        self.assertEqual(
            window.current_display_options(),
            display_options_from_config(window.config),
        )

    def test_preferences_follow_application_palette(self):
        original = self.app.palette()
        palette = QPalette(original)
        palette.setColor(QPalette.Window, QColor("#171717"))
        self.app.setPalette(palette)
        try:
            window = self.window()
            self.assertEqual(
                window.preferences_tabs.palette().color(QPalette.Window),
                QColor("#171717"),
            )
        finally:
            self.app.setPalette(original)

    def test_hiding_tray_does_not_stop_scheduler(self):
        window = self.window(tray_visible=True)
        window._ipc = SimpleNamespace(
            server=SimpleNamespace(isListening=lambda: True)
        )

        with patch("amp_autopower.save_json"):
            window.set_tray_visible(False)

        self.assertFalse(window.config["tray_visible"])
        self.assertFalse(window.tray.isVisible())
        self.assertTrue(window.scheduler.isActive())

    def test_tray_stays_visible_without_window_recovery_method(self):
        window = self.window(tray_visible=True)

        with (
            patch("amp_autopower.save_json"),
            patch("amp_autopower.QMessageBox.warning") as warning,
        ):
            hidden = window.set_tray_visible(False)

        self.assertFalse(hidden)
        self.assertTrue(window.config["tray_visible"])
        warning.assert_called_once()

    def test_show_ipc_recovers_main_window(self):
        socket = FakeSocket("show")
        window = SimpleNamespace(show_normal=MagicMock())
        ipc = IpcServer.__new__(IpcServer)
        ipc.window = window
        ipc.server = SimpleNamespace(
            nextPendingConnection=lambda: socket,
        )

        ipc.handle_connection()

        window.show_normal.assert_called_once_with()
        self.assertTrue(socket.disconnected)

    def test_disabled_hotkey_does_not_attempt_registration(self):
        window = self.window(global_hotkey="")
        window.global_shortcut.disable()
        manager = SimpleNamespace(
            set_shortcut=MagicMock(return_value=True),
            last_error="",
        )
        window.global_shortcut = manager

        self.assertTrue(window.apply_global_hotkey(notify_failure=False))

        manager.set_shortcut.assert_called_once_with("")
        self.assertIn("desactivado", window.global_hotkey_status.text())

    def test_hotkey_registration_failure_does_not_break_window(self):
        window = self.window(global_hotkey="")
        window.global_shortcut.disable()
        manager = SimpleNamespace(
            set_shortcut=MagicMock(return_value=False),
            last_error="simulated registration failure",
        )
        window.global_shortcut = manager
        window.config["global_hotkey"] = "Ctrl+Alt+F12"

        with patch("amp_autopower.QMessageBox.warning"):
            registered = window.apply_global_hotkey()

        self.assertFalse(registered)
        self.assertTrue(window.scheduler.isActive())
        self.assertIn("simulated", window.global_hotkey_status.text())

    def test_hotkey_can_be_activated_and_deactivated(self):
        window = self.window(global_hotkey="")
        window.global_shortcut.disable()
        manager = SimpleNamespace(
            set_shortcut=MagicMock(return_value=True),
            last_error="",
        )
        window.global_shortcut = manager
        window.global_hotkey_edit.setKeySequence(
            QKeySequence("Ctrl+Alt+F12")
        )

        with patch("amp_autopower.save_json"):
            window.save_global_hotkey()
            window.clear_global_hotkey()

        self.assertEqual(
            [call.args[0] for call in manager.set_shortcut.call_args_list],
            ["Ctrl+Alt+F12", ""],
        )
        self.assertEqual(window.config["global_hotkey"], "")

    def test_global_shortcut_backend_degrades_without_qtdbus(self):
        manager = GlobalShortcutManager()

        with patch("amp_autopower.QDBusInterface", None):
            self.assertFalse(manager.set_shortcut("Ctrl+Alt+F12"))

        self.assertIn("QtDBus", manager.last_error)

    def test_kde_shortcut_backend_registers_and_receives_activation(self):
        root = MagicMock()
        root.isValid.return_value = True
        replies = {
            "doRegister": SimpleNamespace(arguments=lambda: []),
            "getComponent": SimpleNamespace(
                arguments=lambda: [
                    SimpleNamespace(path=lambda: "/component/amp_autopower")
                ]
            ),
            "setInactive": SimpleNamespace(arguments=lambda: []),
        }
        root.call.side_effect = lambda method, *_args: replies[method]
        component = MagicMock()
        component.isValid.return_value = True
        component.globalShortcutPressed = FakeSignal()
        manager = GlobalShortcutManager()
        manager._message_error = lambda _reply: ""
        activated = MagicMock()
        manager.activated.connect(activated)

        with (
            patch(
                "amp_autopower.QDBusConnection.sessionBus",
                return_value=object(),
            ),
            patch(
                "amp_autopower.QDBusInterface",
                side_effect=[root, component],
            ),
            patch(
                "amp_autopower.QMetaObject.invokeMethod",
                return_value=[123],
            ) as invoke,
            patch(
                "amp_autopower.Q_RETURN_ARG",
                side_effect=lambda type_name: ("return", type_name),
            ),
            patch(
                "amp_autopower.Q_ARG",
                side_effect=lambda type_name, value: (type_name, value),
            ),
        ):
            self.assertTrue(manager.set_shortcut("Ctrl+Alt+F12"))
            component.globalShortcutPressed.emit(
                APP_ID,
                "show-hide-window",
                123456,
            )
            manager.disable()

        activated.assert_called_once_with()
        self.assertEqual(invoke.call_args.args[1], "setShortcut")
        self.assertEqual(invoke.call_args.args[-1], ("uint", 6))
        self.assertFalse(manager.is_registered)
        self.assertEqual(root.call.call_args_list[-1].args[0], "setInactive")

    def test_kde_shortcut_backend_retries_after_service_restart(self):
        manager = GlobalShortcutManager()
        manager._shortcut_text = "Ctrl+Alt+F12"
        manager._registered = True
        manager._component = MagicMock()

        manager._on_service_unregistered("org.kde.kglobalaccel")

        self.assertFalse(manager.is_registered)
        register = MagicMock(return_value=True)
        manager.set_shortcut = register
        with patch("amp_autopower.QTimer.singleShot") as single_shot:
            manager._on_service_registered("org.kde.kglobalaccel")
        callback = single_shot.call_args.args[1]
        callback()
        register.assert_called_once_with("Ctrl+Alt+F12")

        register.reset_mock()
        manager._shortcut_text = "Ctrl+Alt+F12"
        with patch("amp_autopower.QTimer.singleShot") as single_shot:
            manager._on_service_registered("org.kde.kglobalaccel")
        manager._shortcut_text = ""
        single_shot.call_args.args[1]()
        register.assert_not_called()

    def test_hotkey_action_toggles_main_window(self):
        window = self.window()
        window.show()
        self.app.processEvents()

        window.toggle_main_window()
        self.assertFalse(window.isVisible())
        window.toggle_main_window()
        self.assertTrue(window.isVisible())


if __name__ == "__main__":
    unittest.main()
