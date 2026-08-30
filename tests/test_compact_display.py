import os
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication, QLabel, QListWidget, QWidget

from compact_display import (
    DISPLAY_HEIGHT,
    DISPLAY_WIDTH,
    CompactDisplayWindow,
    DisplayCondition,
    DisplayOptions,
    DisplayScreen,
    DisplaySnapshot,
    build_display_conditions,
    background_alpha_from_transparency,
    choose_display_snapshot,
    display_options_from_config,
    format_clock,
    format_cpu,
    format_duration,
    format_interval_remaining,
    format_logic,
    format_network,
    restore_display_position,
    select_condition_key,
    sort_display_snapshots,
    store_display_options,
)
from amp_autopower import (
    MainWindow,
    Schedule,
    ScheduleEditor,
    format_app_datetime,
    format_app_time,
)


NOW = datetime(2026, 8, 31, 20, 0)


def snapshot(schedule_id, priority, due=None, conditions=()):
    return DisplaySnapshot(
        schedule_id=schedule_id,
        title=schedule_id,
        action="Apagar",
        logic="AND",
        conditions=tuple(conditions),
        priority=priority,
        due=due,
    )


class CompactDisplayLogicTests(unittest.TestCase):
    def test_background_alpha_for_25_percent_transparency(self):
        self.assertEqual(background_alpha_from_transparency(25), 191)

    def test_background_alpha_for_1_percent_transparency(self):
        self.assertEqual(background_alpha_from_transparency(1), 252)

    def test_background_alpha_for_99_percent_transparency(self):
        self.assertEqual(background_alpha_from_transparency(99), 3)

    def test_background_alpha_for_50_and_75_percent_transparency(self):
        self.assertEqual(background_alpha_from_transparency(50), 128)
        self.assertEqual(background_alpha_from_transparency(75), 64)

    def test_global_time_format_helpers(self):
        value = datetime(2026, 8, 31, 23, 30)

        self.assertEqual(format_app_time({"display_use_24_hour": True}, value), "23:30")
        self.assertEqual(format_app_time({"display_use_24_hour": False}, value), "11:30 PM")
        self.assertEqual(format_app_time({"display_use_24_hour": False}, "13:05"), "1:05 PM")
        self.assertTrue(
            format_app_datetime(
                {"display_use_24_hour": False},
                value,
            ).endswith("31/08 11:30 PM")
        )

    def test_selects_active_then_pending_then_nearest_then_interval_then_idle(self):
        candidates = [
            snapshot("idle", 4),
            snapshot("interval", 3, NOW + timedelta(hours=1)),
            snapshot("next-late", 2, NOW + timedelta(hours=2)),
            snapshot("next-near", 2, NOW + timedelta(hours=1)),
            snapshot("pending", 1, NOW + timedelta(hours=3)),
            snapshot("active", 0, NOW + timedelta(seconds=30)),
        ]

        ordered = sort_display_snapshots(candidates)

        self.assertEqual(
            [item.schedule_id for item in ordered],
            [
                "active",
                "pending",
                "next-near",
                "next-late",
                "interval",
                "idle",
            ],
        )

    def test_explicit_schedule_selection_is_respected(self):
        candidates = [snapshot("near", 2), snapshot("idle", 4)]

        selected = choose_display_snapshot(candidates, "idle")

        self.assertEqual(selected.schedule_id, "idle")

    def test_formats_clock_in_12_and_24_hour_modes(self):
        value = datetime(2026, 8, 31, 23, 30)

        self.assertEqual(format_clock(value, True), "23:30")
        self.assertEqual(format_clock(value, False), "11:30 PM")

    def test_formats_interval_remaining(self):
        target = NOW + timedelta(hours=1, minutes=42, seconds=17)

        self.assertEqual(
            format_interval_remaining(target, NOW),
            "01:42:17",
        )

    def test_formats_interval_pending(self):
        self.assertEqual(
            format_interval_remaining(NOW, NOW + timedelta(seconds=1), True),
            "PEND",
        )

    def test_formats_idle_duration(self):
        self.assertEqual(format_duration(27 * 60 + 35), "27:35")

    def test_formats_cpu(self):
        self.assertEqual(format_cpu(8.2, True), "8 %")

    def test_formats_network_in_kb_and_mb(self):
        self.assertEqual(format_network(42 * 1024, True), "42 KB/s")
        self.assertEqual(format_network(1.5 * 1024 * 1024, True), "1.5 MB/s")

    def test_formats_unavailable_monitor(self):
        self.assertEqual(format_cpu(None, False), "--")
        self.assertEqual(format_network(None, False), "--")

    def test_shows_logic_only_for_multiple_conditions(self):
        self.assertEqual(format_logic("OR", 2), "OR")
        self.assertEqual(format_logic("and", 3), "AND")
        self.assertEqual(format_logic("OR", 1), "")

    def test_selects_only_available_condition(self):
        keys = ("time", "cpu", "network")

        self.assertEqual(select_condition_key(keys, "time", "cpu"), "cpu")
        self.assertEqual(select_condition_key(keys, "cpu", "idle"), "cpu")
        self.assertEqual(select_condition_key(("network",), "cpu"), "network")

    def test_display_options_round_trip(self):
        options = DisplayOptions(
            enabled=True,
            always_on_top=False,
            show_title=False,
            show_action=False,
            transparency_percent=40,
            use_24_hour=False,
        )
        config = {}

        store_display_options(config, options)

        self.assertEqual(display_options_from_config(config), options)

    def test_restores_valid_position(self):
        screens = (DisplayScreen("DP-1", 0, 0, 1920, 1080),)

        restored = restore_display_position(
            {"x": 100, "y": 200, "screen": "DP-1"},
            screens,
        )

        self.assertEqual(restored, (100, 200, "DP-1"))

    def test_repositions_offscreen_position(self):
        screens = (DisplayScreen("DP-1", 0, 0, 1920, 1080),)

        x, y, screen = restore_display_position(
            {"x": 9000, "y": 9000, "screen": "missing"},
            screens,
        )

        self.assertEqual(screen, "DP-1")
        self.assertGreaterEqual(x, 0)
        self.assertGreaterEqual(y, 0)
        self.assertLessEqual(x + DISPLAY_WIDTH, 1920)
        self.assertLessEqual(y + DISPLAY_HEIGHT, 1080)

    def test_screen_name_survives_monitor_rearrangement(self):
        screens = (
            DisplayScreen("HDMI-1", 0, 0, 1920, 1080),
            DisplayScreen("DP-1", 1920, 0, 1920, 1080),
        )

        x, y, screen = restore_display_position(
            {"x": 100, "y": 100, "screen": "DP-1"},
            screens,
        )

        self.assertEqual(screen, "DP-1")
        self.assertGreaterEqual(x, 1920)
        self.assertEqual(y, 100)

    def test_builds_indicators_for_all_active_conditions(self):
        conditions = build_display_conditions(
            mode="time",
            scheduled_target=NOW + timedelta(hours=1),
            now=NOW,
            use_24_hour=True,
            pending=False,
            idle_enabled=True,
            idle_seconds=60,
            idle_reliable=True,
            cpu_enabled=True,
            cpu_value=8,
            cpu_reliable=True,
            network_enabled=True,
            network_value=42 * 1024,
            network_reliable=True,
        )

        self.assertEqual(
            [condition.key for condition in conditions],
            ["time", "idle", "cpu", "network"],
        )


class CompactDisplayQtSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(
            ["amp-autopower-display-test"]
        )

    def setUp(self):
        self.window = CompactDisplayWindow()

    def tearDown(self):
        self.window.hide()
        self.window.deleteLater()
        self.app.processEvents()

    def test_offscreen_render_selection_and_close_hide(self):
        conditions = (
            DisplayCondition("time", "HRA", "Hora", "23:30"),
            DisplayCondition("cpu", "CPU", "CPU", "8 %"),
        )
        self.window.apply_options(DisplayOptions(enabled=True))
        self.window.set_snapshots(
            [snapshot("night", 2, NOW, conditions)]
        )
        self.window.show()
        self.app.processEvents()

        self.assertEqual(self.window.size().width(), DISPLAY_WIDTH)
        self.assertEqual(self.window.size().height(), DISPLAY_HEIGHT)
        self.assertEqual(self.window.value_label.text(), "23:30")
        self.assertEqual(self.window.meta_label.text(), "Apagar | AND")

        self.window.select_condition("cpu")
        self.assertEqual(self.window.value_label.text(), "8 %")

        self.window.hide_requested.connect(self.window.hide)
        self.window.close()
        self.app.processEvents()
        self.assertFalse(self.window.isVisible())

    def test_priority_transition_and_manual_schedule_selection(self):
        condition = (DisplayCondition("time", "HRA", "Hora", "23:30"),)
        normal = snapshot("normal", 2, NOW + timedelta(hours=1), condition)
        pending = snapshot("pending", 1, NOW + timedelta(hours=2), condition)
        active = snapshot("active", 0, NOW + timedelta(seconds=30), condition)

        self.window.set_snapshots([normal])
        self.window.set_snapshots([normal, pending])
        self.assertEqual(self.window.current_schedule_id, "pending")

        self.window.cycle_schedule()
        self.assertEqual(self.window.current_schedule_id, "normal")
        self.window.set_snapshots([normal, pending])
        self.assertEqual(self.window.current_schedule_id, "normal")

        self.window.set_snapshots([normal, pending, active])
        self.assertEqual(self.window.current_schedule_id, "active")

    def test_changing_window_options_preserves_position(self):
        self.window.move(30, 40)
        self.window.show()
        self.app.processEvents()

        self.window.apply_options(
            DisplayOptions(always_on_top=False, transparency_percent=40)
        )
        self.app.processEvents()

        self.assertEqual((self.window.x(), self.window.y()), (30, 40))

    def test_transparency_uses_rgba_without_window_opacity(self):
        with patch.object(
            CompactDisplayWindow,
            "setWindowOpacity",
            side_effect=AssertionError("setWindowOpacity must not be used"),
        ):
            self.window.apply_options(
                DisplayOptions(transparency_percent=25)
            )

        self.assertTrue(self.window.testAttribute(Qt.WA_TranslucentBackground))
        self.assertEqual(self.window.background_alpha, 191)

    def test_transparency_change_updates_painted_background_state(self):
        self.window.apply_options(DisplayOptions(transparency_percent=75))
        self.window.show()
        self.app.processEvents()
        image = QImage(self.window.size(), QImage.Format_ARGB32)
        image.fill(0)
        self.window.render(image)

        self.assertEqual(self.window.background_alpha, 64)
        self.assertEqual(image.pixelColor(2, 50).alpha(), 64)

    def test_main_status_and_schedule_row_follow_12_hour_format(self):
        target = datetime(2026, 8, 31, 23, 30)
        item = Schedule(time="23:30", weekdays=[0])
        owner = SimpleNamespace(
            config={"display_use_24_hour": False},
            list=QListWidget(),
            status_label=QLabel(),
            schedules=lambda: [item],
            pending_occurrence=lambda _schedule: None,
            pending_action_time=lambda *_args: target,
            next_occurrence=lambda _schedule, _now: target,
            update_next_label=lambda: None,
        )

        MainWindow.refresh_list(owner)
        self.assertIn("11:30 PM", owner.list.item(0).text())

        owner.update_next_label = MainWindow.update_next_label.__get__(owner)
        owner.update_next_label()
        self.assertIn("11:30 PM", owner.status_label.text())


class ScheduleEditorTimeFormatTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(
            ["amp-autopower-editor-test"]
        )

    def setUp(self):
        self.schedule = Schedule(time="13:05")

    def tearDown(self):
        for widget in QApplication.topLevelWidgets():
            if isinstance(widget, ScheduleEditor):
                widget.hide()
                widget.deleteLater()
        self.app.processEvents()

    def test_qtimeedit_uses_24_hour_format(self):
        editor = ScheduleEditor(schedule=self.schedule, use_24_hour=True)

        self.assertEqual(editor.time.displayFormat(), "HH:mm")
        self.assertEqual(editor.time.text(), "13:05")

    def test_qtimeedit_uses_12_hour_format(self):
        editor = ScheduleEditor(schedule=self.schedule, use_24_hour=False)

        self.assertEqual(editor.time.displayFormat(), "h:mm AP")

    def test_1305_is_displayed_as_105_pm(self):
        editor = ScheduleEditor(schedule=self.schedule, use_24_hour=False)

        self.assertEqual(editor.time.text(), "1:05 PM")

    def test_saving_from_12_hour_mode_keeps_internal_24_hour_time(self):
        editor = ScheduleEditor(schedule=self.schedule, use_24_hour=False)

        saved = editor.get_schedule()

        self.assertEqual(saved.time, "13:05")

    def test_editor_reads_current_global_preference_when_opened(self):
        parent = QWidget()
        parent.config = {"display_use_24_hour": False}
        editor = ScheduleEditor(parent, self.schedule)

        self.assertEqual(editor.time.text(), "1:05 PM")
        parent.deleteLater()


if __name__ == "__main__":
    unittest.main()
