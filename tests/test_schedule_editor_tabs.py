import os
import unittest
from dataclasses import asdict
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QWidget

from amp_autopower import Schedule, ScheduleEditor


TAB_NAMES = [
    "General",
    "Programación",
    "Inactividad",
    "CPU",
    "Red",
    "Acción",
]


class ScheduleEditorTabsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(
            ["amp-autopower-schedule-editor-test"]
        )

    def setUp(self):
        self.parent = QWidget()
        self.parent.config = {"display_use_24_hour": True}
        self.parent.network_monitor = SimpleNamespace(
            available_interfaces=lambda: ("enp1s0", "wlan0"),
        )
        self.editors = []

    def tearDown(self):
        for editor in self.editors:
            editor.hide()
            editor.deleteLater()
        self.parent.hide()
        self.parent.deleteLater()
        self.app.processEvents()

    def editor(self, schedule):
        editor = ScheduleEditor(self.parent, schedule)
        self.editors.append(editor)
        return editor

    def test_has_six_navigable_tabs(self):
        editor = self.editor(Schedule())

        self.assertEqual(editor.tabs.count(), 6)
        self.assertEqual(
            [editor.tabs.tabText(index) for index in range(6)],
            TAB_NAMES,
        )
        for index in range(editor.tabs.count()):
            editor.tabs.setCurrentIndex(index)
            self.assertEqual(editor.tabs.currentIndex(), index)

    def test_cpu_and_network_tabs_remain_enabled_when_conditions_are_off(self):
        editor = self.editor(
            Schedule(require_cpu=False, require_network=False)
        )

        self.assertTrue(editor.tabs.isTabEnabled(TAB_NAMES.index("CPU")))
        self.assertTrue(editor.tabs.isTabEnabled(TAB_NAMES.index("Red")))
        self.assertFalse(editor.cpu_threshold.isEnabled())
        self.assertFalse(editor.network_threshold.isEnabled())

    def test_time_mode_controls_and_round_trip(self):
        source = Schedule(
            id="time",
            name="Hora",
            use_time=True,
            trigger_mode="time",
            time="23:30",
            weekdays=[0, 2, 4],
            warning_minutes=[30, 5, 1],
            final_countdown_seconds=45,
            network_interface="enp1s0",
        )
        editor = self.editor(source)

        self.assertTrue(editor.time.isEnabled())
        self.assertFalse(editor.interval_hours.isEnabled())
        self.assertTrue(editor.days_box.isEnabled())
        self.assertTrue(editor.warn_box.isEnabled())
        self.assertEqual(asdict(editor.get_schedule()), asdict(source))

    def test_interval_mode_controls_and_round_trip(self):
        source = Schedule(
            id="interval",
            name="Intervalo",
            use_time=False,
            trigger_mode="interval",
            interval_minutes=125,
            weekdays=[],
            warning_minutes=[15],
            network_interface="enp1s0",
        )
        editor = self.editor(source)

        self.assertFalse(editor.time.isEnabled())
        self.assertTrue(editor.interval_hours.isEnabled())
        self.assertFalse(editor.days_box.isEnabled())
        self.assertFalse(editor.warn_box.isEnabled())
        self.assertEqual(asdict(editor.get_schedule()), asdict(source))

    def test_idle_mode_controls_and_round_trip(self):
        source = Schedule(
            id="idle",
            name="Inactividad",
            use_time=False,
            trigger_mode="idle",
            weekdays=[0, 1, 2, 3, 4, 5, 6],
            require_idle=True,
            idle_minutes=27,
            network_interface="enp1s0",
        )
        editor = self.editor(source)

        self.assertFalse(editor.time.isEnabled())
        self.assertFalse(editor.interval_hours.isEnabled())
        self.assertTrue(editor.days_box.isEnabled())
        self.assertTrue(editor.require_idle.isChecked())
        self.assertFalse(editor.require_idle.isEnabled())
        self.assertEqual(asdict(editor.get_schedule()), asdict(source))

    def test_switching_modes_keeps_enablement_coherent(self):
        editor = self.editor(Schedule())

        editor.mode.setCurrentIndex(editor.mode.findData("interval"))
        self.assertFalse(editor.time.isEnabled())
        self.assertTrue(editor.interval_minutes.isEnabled())
        self.assertFalse(editor.days_box.isEnabled())

        editor.mode.setCurrentIndex(editor.mode.findData("idle"))
        self.assertFalse(editor.time.isEnabled())
        self.assertFalse(editor.interval_minutes.isEnabled())
        self.assertTrue(editor.days_box.isEnabled())
        self.assertFalse(editor.require_idle.isEnabled())

        editor.mode.setCurrentIndex(editor.mode.findData("time"))
        self.assertTrue(editor.time.isEnabled())
        self.assertFalse(editor.interval_minutes.isEnabled())
        self.assertTrue(editor.days_box.isEnabled())

    def test_cpu_network_and_or_full_round_trip(self):
        source = Schedule(
            id="all",
            name="Todas las condiciones",
            enabled=False,
            use_time=True,
            trigger_mode="time",
            time="13:05",
            weekdays=[1, 3, 5],
            action="poweroff",
            warning_minutes=[30, 15, 5, 1],
            final_countdown_seconds=90,
            condition_logic="OR",
            require_idle=True,
            idle_minutes=18,
            require_cpu=True,
            cpu_comparison="greater",
            cpu_threshold=72,
            cpu_duration_seconds=125,
            cpu_use_average=True,
            cpu_average_seconds=45,
            require_network=True,
            network_interface="enp1s0",
            network_direction="tx",
            network_comparison="greater",
            network_threshold=75,
            network_unit="MB/s",
            network_duration_seconds=180,
            network_use_average=True,
            network_average_seconds=30,
            close_apps_first=True,
        )
        editor = self.editor(source)

        self.assertFalse(editor.condition_logic.isHidden())
        self.assertTrue(editor.cpu_threshold.isEnabled())
        self.assertTrue(editor.network_threshold.isEnabled())
        self.assertEqual(asdict(editor.get_schedule()), asdict(source))

    def test_legacy_schedule_defaults_round_trip(self):
        legacy = Schedule(
            id="legacy",
            name="Legacy",
            use_time=True,
            time="22:10",
            weekdays=[0, 1, 2, 3, 4],
        )
        editor = self.editor(legacy)

        saved = editor.get_schedule()
        self.assertEqual(saved.id, legacy.id)
        self.assertEqual(saved.name, legacy.name)
        self.assertEqual(saved.time, legacy.time)
        self.assertEqual(saved.weekdays, legacy.weekdays)
        self.assertEqual(saved.trigger_mode, "time")

    def test_tabs_follow_application_palette(self):
        original = self.app.palette()
        palette = QPalette(original)
        palette.setColor(QPalette.Window, QColor("#161616"))
        self.app.setPalette(palette)
        try:
            editor = self.editor(Schedule())
            self.assertEqual(
                editor.tabs.palette().color(QPalette.Window),
                QColor("#161616"),
            )
            self.assertEqual(editor.tabs.styleSheet(), "")
        finally:
            self.app.setPalette(original)


if __name__ == "__main__":
    unittest.main()
