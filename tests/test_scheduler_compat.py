import unittest
from dataclasses import asdict
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from amp_autopower import MainWindow, Schedule, schedule_to_dict
from condition_engine import (
    CPUReading,
    ConditionEngine,
    ScheduledOccurrence,
    schedule_trigger_mode,
)


class SchedulerHarness:
    pending_occurrence = MainWindow.pending_occurrence
    save_pending_occurrence = MainWindow.save_pending_occurrence
    pending_action_time = MainWindow.pending_action_time
    _prune_pending_occurrences = MainWindow._prune_pending_occurrences
    _reconcile_schedule_occurrences = MainWindow._reconcile_schedule_occurrences
    _start_interval_occurrence = MainWindow._start_interval_occurrence
    _complete_interval_schedule = MainWindow._complete_interval_schedule
    _record_action_completion = MainWindow._record_action_completion
    _guard_interval_execution = MainWindow._guard_interval_execution
    _restore_failed_interval_execution = MainWindow._restore_failed_interval_execution
    execute_action = MainWindow.execute_action
    evaluate_conditions = MainWindow.evaluate_conditions
    defer_for_idle = MainWindow.defer_for_idle
    _defer_for_conditions = MainWindow._defer_for_conditions
    _has_active_dialog_for_schedule = MainWindow._has_active_dialog_for_schedule
    _pending_occurrence_tick = MainWindow._pending_occurrence_tick
    set_schedules = MainWindow.set_schedules
    mark_skipped = MainWindow.mark_skipped

    def __init__(
        self,
        state,
        idle_seconds=0,
        reliable=True,
        cpu_reading=None,
    ):
        self.state = state
        self.condition_engine = ConditionEngine()
        self.active_dialogs = {}
        self.config = {
            "overlay_all_schedule_warnings": False,
            "schedules": [],
        }
        self._idle_seconds = idle_seconds
        self._reliable = reliable
        self._cpu_reading = cpu_reading or CPUReading(
            None,
            None,
            False,
            "cpu_monitor_warming_up",
        )
        self.cpu_monitor = SimpleNamespace(
            reading=lambda _window=0: self._cpu_reading,
        )
        self.started = []

    def idle_seconds(self):
        return self._idle_seconds

    def input_monitor_reliable(self):
        return self._reliable

    def notify(self, *_args, **_kwargs):
        pass

    def show_warning_banner(self, *_args, **_kwargs):
        pass

    def start_final_countdown(self, schedule, target, seconds, key):
        self.started.append((schedule.id, target, seconds, key))

    def schedules(self):
        out = []
        cpu_settings = self.config.get("cpu_settings", {})
        for raw in self.config.get("schedules", []):
            data = dict(raw)
            preset = cpu_settings.get(data.get("id"), {})
            if isinstance(preset, dict):
                for key, value in preset.items():
                    data.setdefault(key, value)
            out.append(Schedule(**data))
        return out

    def refresh_list(self):
        pass


class SchedulerCompatibilityTests(unittest.TestCase):
    def test_v130_schedule_fields_load_without_migration(self):
        raw = {
            "id": "legacy",
            "name": "Legacy schedule",
            "enabled": True,
            "use_time": True,
            "time": "23:30",
            "weekdays": [0, 1, 2, 3, 4],
            "action": "test",
            "warning_minutes": [30, 5],
            "final_countdown_seconds": 60,
            "require_idle": True,
            "idle_minutes": 15,
            "close_apps_first": False,
        }

        loaded = Schedule(**raw)

        loaded_data = asdict(loaded)
        for key, value in raw.items():
            self.assertEqual(loaded_data[key], value)
        self.assertEqual(schedule_trigger_mode(loaded), "time")
        self.assertEqual(loaded.interval_minutes, 60)

    def test_legacy_modes_serialize_without_interval_fields(self):
        timed = Schedule(id="timed", use_time=True, trigger_mode="time")
        idle = Schedule(id="idle", use_time=False, trigger_mode="idle")

        for item in (timed, idle):
            serialized = schedule_to_dict(item)
            self.assertNotIn("trigger_mode", serialized)
            self.assertNotIn("interval_minutes", serialized)
            self.assertNotIn("require_cpu", serialized)
            self.assertNotIn("cpu_threshold", serialized)

    def test_disabled_cpu_preserves_custom_configuration(self):
        item = Schedule(
            id="cpu",
            require_cpu=False,
            cpu_comparison="greater",
            cpu_threshold=80,
            cpu_duration_seconds=30,
            cpu_use_average=True,
            cpu_average_seconds=15,
        )

        state = {
            "last_runs": {},
            "snoozes": {},
            "skipped_targets": {},
            "pending_occurrences": {},
            "completed_intervals": {},
        }
        harness = SchedulerHarness(state)
        with patch("amp_autopower.save_json"):
            harness.set_schedules([item])
        serialized = harness.config["schedules"][0]
        restored = harness.schedules()[0]

        self.assertNotIn("require_cpu", serialized)
        self.assertNotIn("cpu_threshold", serialized)
        self.assertFalse(restored.require_cpu)
        self.assertEqual(restored.cpu_comparison, "greater")
        self.assertEqual(restored.cpu_threshold, 80)
        self.assertEqual(restored.cpu_duration_seconds, 30)
        self.assertTrue(restored.cpu_use_average)
        self.assertEqual(restored.cpu_average_seconds, 15)

    def test_switching_interval_to_time_discards_interval_occurrence(self):
        interval = Schedule(
            id="schedule",
            use_time=False,
            trigger_mode="interval",
            weekdays=[],
        )
        timed = Schedule(
            id="schedule",
            use_time=True,
            trigger_mode="time",
            weekdays=[0],
        )
        state = {
            "last_runs": {},
            "snoozes": {},
            "skipped_targets": {},
            "pending_occurrences": {
                interval.id: ScheduledOccurrence.create_interval(
                    interval.id,
                    datetime(2026, 8, 31, 10, 0),
                    30,
                    60,
                ).to_state(),
            },
            "completed_intervals": {},
        }
        harness = SchedulerHarness(state)

        with patch("amp_autopower.save_json"):
            harness._prune_pending_occurrences([timed])

        self.assertNotIn(interval.id, state["pending_occurrences"])

    def test_editing_active_interval_cancels_old_countdown_before_restart(self):
        saved_at = datetime(2026, 8, 31, 10, 15)
        target = datetime(2026, 8, 31, 10, 30)
        original = Schedule(
            id="interval",
            use_time=False,
            trigger_mode="interval",
            interval_minutes=30,
            weekdays=[],
            action="reboot",
        )
        updated = Schedule(
            id="interval",
            use_time=False,
            trigger_mode="interval",
            interval_minutes=90,
            weekdays=[],
            action="test",
        )
        state = {
            "last_runs": {},
            "snoozes": {},
            "skipped_targets": {},
            "pending_occurrences": {
                original.id: ScheduledOccurrence.create_interval(
                    original.id,
                    datetime(2026, 8, 31, 10, 0),
                    30,
                    60,
                ).to_state(),
            },
            "completed_intervals": {},
        }
        harness = SchedulerHarness(state)
        harness.config["schedules"] = [schedule_to_dict(original)]

        class Dialog:
            schedule = original
            remaining = 30
            finished_with = None

            def finish(self, action):
                self.finished_with = action
                harness.mark_skipped(original, target)

        dialog = Dialog()
        harness.active_dialogs["old"] = dialog

        with patch("amp_autopower.save_json"):
            harness.set_schedules(
                [updated],
                restart_interval_ids={updated.id},
                now=saved_at,
            )

        restarted = harness.pending_occurrence(updated)
        self.assertEqual(dialog.finished_with, "cancel")
        self.assertEqual(restarted.start_at, saved_at)
        self.assertEqual(
            restarted.scheduled_target,
            saved_at + timedelta(minutes=90),
        )
        self.assertTrue(harness.schedules()[0].enabled)
        self.assertEqual(harness.schedules()[0].action, "test")

    def test_interval_reconcile_persists_start_and_target(self):
        start_at = datetime(2026, 8, 31, 10, 0)
        state = {
            "last_runs": {},
            "snoozes": {},
            "skipped_targets": {},
            "pending_occurrences": {},
            "completed_intervals": {},
        }
        item = Schedule(
            id="interval",
            use_time=False,
            trigger_mode="interval",
            interval_minutes=30,
            weekdays=[],
        )
        harness = SchedulerHarness(state)

        with patch("amp_autopower.save_json"):
            harness._reconcile_schedule_occurrences(
                [item],
                restart_interval_ids={item.id},
                now=start_at,
            )

        occurrence = harness.pending_occurrence(item)
        self.assertEqual(occurrence.start_at, start_at)
        self.assertEqual(
            occurrence.scheduled_target,
            datetime(2026, 8, 31, 10, 30),
        )

    def test_interval_user_snooze_does_not_replace_original_target(self):
        start_at = datetime(2026, 8, 31, 10, 0)
        target = datetime(2026, 8, 31, 10, 30)
        snooze = datetime(2026, 8, 31, 10, 40)
        occurrence = ScheduledOccurrence.create_interval(
            "interval",
            start_at,
            30,
            60,
        ).mark_armed()
        state = {
            "last_runs": {},
            "snoozes": {"interval": snooze.isoformat()},
            "skipped_targets": {},
            "pending_occurrences": {
                "interval": occurrence.to_state(),
            },
            "completed_intervals": {},
        }
        item = Schedule(
            id="interval",
            use_time=False,
            trigger_mode="interval",
            interval_minutes=30,
            weekdays=[],
        )
        harness = SchedulerHarness(state)

        with patch("amp_autopower.save_json"):
            harness._pending_occurrence_tick(
                item,
                occurrence,
                snooze - timedelta(seconds=60),
            )

        restored = harness.pending_occurrence(item)
        self.assertEqual(restored.start_at, start_at)
        self.assertEqual(restored.scheduled_target, target)
        self.assertEqual(harness.started[0][1], target)

    def test_disabling_interval_removes_active_occurrence(self):
        start_at = datetime(2026, 8, 31, 10, 0)
        active = Schedule(
            id="interval",
            use_time=False,
            trigger_mode="interval",
            interval_minutes=30,
            weekdays=[],
        )
        disabled = Schedule(**{**asdict(active), "enabled": False})
        state = {
            "last_runs": {},
            "snoozes": {},
            "skipped_targets": {},
            "pending_occurrences": {
                active.id: ScheduledOccurrence.create_interval(
                    active.id,
                    start_at,
                    30,
                    60,
                ).to_state(),
            },
            "completed_intervals": {},
        }
        harness = SchedulerHarness(state)

        with patch("amp_autopower.save_json"):
            harness._reconcile_schedule_occurrences([disabled], now=start_at)

        self.assertNotIn(active.id, state["pending_occurrences"])

    def test_reactivating_interval_creates_new_start(self):
        reactivated_at = datetime(2026, 8, 31, 12, 0)
        item = Schedule(
            id="interval",
            use_time=False,
            trigger_mode="interval",
            interval_minutes=45,
            weekdays=[],
        )
        state = {
            "last_runs": {},
            "snoozes": {},
            "skipped_targets": {},
            "pending_occurrences": {},
            "completed_intervals": {item.id: "completed"},
        }
        harness = SchedulerHarness(state)

        with patch("amp_autopower.save_json"):
            harness._reconcile_schedule_occurrences(
                [item],
                restart_interval_ids={item.id},
                now=reactivated_at,
            )

        occurrence = harness.pending_occurrence(item)
        self.assertEqual(occurrence.start_at, reactivated_at)
        self.assertEqual(
            occurrence.scheduled_target,
            reactivated_at + timedelta(minutes=45),
        )
        self.assertNotIn(item.id, state["completed_intervals"])

    def test_editing_interval_duration_restarts_from_save_time(self):
        original_start = datetime(2026, 8, 31, 10, 0)
        saved_at = datetime(2026, 8, 31, 10, 30)
        item = Schedule(
            id="interval",
            use_time=False,
            trigger_mode="interval",
            interval_minutes=180,
            weekdays=[],
        )
        old_occurrence = ScheduledOccurrence.create_interval(
            item.id,
            original_start,
            120,
            60,
        )
        state = {
            "last_runs": {},
            "snoozes": {},
            "skipped_targets": {},
            "pending_occurrences": {
                item.id: old_occurrence.to_state(),
            },
            "completed_intervals": {},
        }
        harness = SchedulerHarness(state)

        with patch("amp_autopower.save_json"):
            harness._reconcile_schedule_occurrences(
                [item],
                restart_interval_ids={item.id},
                now=saved_at,
            )

        occurrence = harness.pending_occurrence(item)
        self.assertEqual(occurrence.start_at, saved_at)
        self.assertEqual(
            occurrence.scheduled_target,
            saved_at + timedelta(hours=3),
        )

    def test_deleting_interval_cleans_all_associated_state(self):
        item = Schedule(
            id="interval",
            use_time=False,
            trigger_mode="interval",
            weekdays=[],
        )
        target = datetime(2026, 8, 31, 11, 0)
        state = {
            "last_runs": {item.id: "run"},
            "snoozes": {item.id: "snooze"},
            "skipped_targets": {item.id: "skipped"},
            "pending_occurrences": {
                item.id: ScheduledOccurrence.create_interval(
                    item.id,
                    datetime(2026, 8, 31, 10, 0),
                    60,
                    60,
                ).to_state(),
            },
            "completed_intervals": {item.id: target.isoformat()},
        }
        harness = SchedulerHarness(state)
        harness.config["schedules"] = [asdict(item)]

        with patch("amp_autopower.save_json"):
            harness.set_schedules([], now=target)

        for state_key in (
            "last_runs",
            "snoozes",
            "skipped_targets",
            "pending_occurrences",
            "completed_intervals",
        ):
            self.assertNotIn(item.id, state[state_key])

    def test_completed_interval_is_disabled_and_not_restarted(self):
        item = Schedule(
            id="interval",
            use_time=False,
            trigger_mode="interval",
            weekdays=[],
        )
        target = datetime(2026, 8, 31, 11, 0)
        state = {
            "last_runs": {},
            "snoozes": {},
            "skipped_targets": {},
            "pending_occurrences": {
                item.id: ScheduledOccurrence.create_interval(
                    item.id,
                    datetime(2026, 8, 31, 10, 0),
                    60,
                    60,
                ).to_state(),
            },
            "completed_intervals": {},
        }
        harness = SchedulerHarness(state)
        harness.config["schedules"] = [asdict(item)]

        with patch("amp_autopower.save_json"):
            harness.mark_skipped(item, target)
            configured = harness.schedules()[0]
            harness._reconcile_schedule_occurrences([configured], now=target)

        self.assertFalse(configured.enabled)
        self.assertNotIn(item.id, state["pending_occurrences"])
        self.assertIn(item.id, state["completed_intervals"])

    def test_successful_interval_action_completes_one_shot(self):
        item = Schedule(
            id="interval",
            use_time=False,
            trigger_mode="interval",
            interval_minutes=60,
            weekdays=[],
            action="suspend",
            close_apps_first=False,
        )
        target = datetime(2026, 8, 31, 11, 0)
        state = {
            "last_runs": {},
            "snoozes": {},
            "skipped_targets": {},
            "pending_occurrences": {
                item.id: ScheduledOccurrence.create_interval(
                    item.id,
                    datetime(2026, 8, 31, 10, 0),
                    60,
                    60,
                ).to_state(),
            },
            "completed_intervals": {},
        }
        harness = SchedulerHarness(state)
        harness.config["schedules"] = [schedule_to_dict(item)]
        success = SimpleNamespace(returncode=0, stderr="", stdout="")

        def successful_command(_cmd):
            self.assertFalse(harness.schedules()[0].enabled)
            self.assertIn(item.id, state["completed_intervals"])
            return success

        with (
            patch("amp_autopower.save_json"),
            patch("amp_autopower.run_cmd", side_effect=successful_command),
        ):
            harness.execute_action(item, target)

        self.assertFalse(harness.schedules()[0].enabled)
        self.assertEqual(state["last_runs"][item.id], target.isoformat())
        self.assertIn(item.id, state["completed_intervals"])
        self.assertNotIn(item.id, state["pending_occurrences"])

    def test_failed_interval_action_remains_active(self):
        item = Schedule(
            id="interval",
            use_time=False,
            trigger_mode="interval",
            interval_minutes=60,
            weekdays=[],
            action="suspend",
            close_apps_first=False,
        )
        target = datetime(2026, 8, 31, 11, 0)
        occurrence = ScheduledOccurrence.create_interval(
            item.id,
            datetime(2026, 8, 31, 10, 0),
            60,
            60,
        )
        state = {
            "last_runs": {},
            "snoozes": {},
            "skipped_targets": {},
            "pending_occurrences": {item.id: occurrence.to_state()},
            "completed_intervals": {},
        }
        harness = SchedulerHarness(state)
        harness.config["schedules"] = [schedule_to_dict(item)]
        failure = SimpleNamespace(
            returncode=1,
            stderr="simulated failure",
            stdout="",
        )

        with (
            patch("amp_autopower.save_json"),
            patch("amp_autopower.run_cmd", return_value=failure),
        ):
            harness.execute_action(item, target)

        self.assertTrue(harness.schedules()[0].enabled)
        self.assertNotIn(item.id, state["last_runs"])
        self.assertNotIn(item.id, state["completed_intervals"])
        self.assertIn(item.id, state["pending_occurrences"])

    def test_interval_command_exception_restores_active_occurrence(self):
        item = Schedule(
            id="interval",
            use_time=False,
            trigger_mode="interval",
            interval_minutes=60,
            weekdays=[],
            action="suspend",
            close_apps_first=False,
        )
        target = datetime(2026, 8, 31, 11, 0)
        occurrence = ScheduledOccurrence.create_interval(
            item.id,
            datetime(2026, 8, 31, 10, 0),
            60,
            60,
        )
        state = {
            "last_runs": {},
            "snoozes": {},
            "skipped_targets": {},
            "pending_occurrences": {item.id: occurrence.to_state()},
            "completed_intervals": {},
        }
        harness = SchedulerHarness(state)
        harness.config["schedules"] = [schedule_to_dict(item)]

        with (
            patch("amp_autopower.save_json"),
            patch("amp_autopower.run_cmd", side_effect=OSError("simulated")),
        ):
            harness.execute_action(item, target)

        self.assertTrue(harness.schedules()[0].enabled)
        self.assertNotIn(item.id, state["completed_intervals"])
        self.assertIn(item.id, state["pending_occurrences"])

    def test_multiple_intervals_keep_independent_occurrences(self):
        start_at = datetime(2026, 8, 31, 10, 0)
        first = Schedule(
            id="first",
            use_time=False,
            trigger_mode="interval",
            interval_minutes=30,
            weekdays=[],
        )
        second = Schedule(
            id="second",
            use_time=False,
            trigger_mode="interval",
            interval_minutes=90,
            weekdays=[],
        )
        state = {
            "last_runs": {},
            "snoozes": {},
            "skipped_targets": {},
            "pending_occurrences": {},
            "completed_intervals": {},
        }
        harness = SchedulerHarness(state)

        with patch("amp_autopower.save_json"):
            harness._reconcile_schedule_occurrences(
                [first, second],
                restart_interval_ids={first.id, second.id},
                now=start_at,
            )

        first_occurrence = harness.pending_occurrence(first)
        second_occurrence = harness.pending_occurrence(second)
        self.assertEqual(
            first_occurrence.scheduled_target,
            start_at + timedelta(minutes=30),
        )
        self.assertEqual(
            second_occurrence.scheduled_target,
            start_at + timedelta(minutes=90),
        )
        self.assertNotEqual(first_occurrence, second_occurrence)

    def test_time_and_cpu_pending_keeps_original_target(self):
        target = datetime(2026, 8, 31, 23, 30)
        occurrence = ScheduledOccurrence.create("timed", target, 60)
        state = {
            "last_runs": {},
            "snoozes": {},
            "skipped_targets": {},
            "pending_occurrences": {"timed": occurrence.to_state()},
        }
        item = Schedule(
            id="timed",
            time="23:30",
            weekdays=[0],
            require_cpu=True,
            cpu_threshold=10,
            cpu_duration_seconds=300,
        )
        harness = SchedulerHarness(
            state,
            cpu_reading=CPUReading(50.0, None, True, "cpu_sample_available"),
        )

        with patch("amp_autopower.save_json"):
            harness._pending_occurrence_tick(item, occurrence, target)

        persisted = harness.pending_occurrence(item)
        self.assertTrue(persisted.armed)
        self.assertEqual(persisted.scheduled_target, target)
        self.assertEqual(harness.started, [])

    def test_editing_cpu_condition_resets_continuous_duration(self):
        started = datetime(2026, 8, 31, 22, 0)
        state = {
            "last_runs": {},
            "snoozes": {},
            "skipped_targets": {},
            "pending_occurrences": {},
            "completed_intervals": {},
        }
        item = Schedule(
            id="timed",
            time="23:30",
            weekdays=[0],
            require_cpu=True,
            cpu_threshold=10,
            cpu_duration_seconds=300,
        )
        harness = SchedulerHarness(
            state,
            cpu_reading=CPUReading(5.0, None, True, "cpu_sample_available"),
        )
        occurrence = ScheduledOccurrence.create(
            item.id,
            datetime(2026, 8, 31, 23, 30),
            60,
        )
        harness.evaluate_conditions(item, started, occurrence)
        self.assertEqual(
            harness.condition_engine.runtime.cpu_satisfied_since(item.id),
            started,
        )

        updated = Schedule(**{**asdict(item), "cpu_threshold": 15})
        with patch("amp_autopower.save_json"):
            harness.set_schedules([updated], now=started + timedelta(minutes=1))

        self.assertIsNone(
            harness.condition_engine.runtime.cpu_satisfied_since(item.id)
        )

    def test_editing_another_schedule_keeps_cpu_continuous_duration(self):
        started = datetime(2026, 8, 31, 22, 0)
        state = {
            "last_runs": {},
            "snoozes": {},
            "skipped_targets": {},
            "pending_occurrences": {},
            "completed_intervals": {},
        }
        cpu_item = Schedule(
            id="cpu",
            require_cpu=True,
            cpu_threshold=10,
            cpu_duration_seconds=300,
        )
        other = Schedule(id="other", name="Original")
        harness = SchedulerHarness(
            state,
            cpu_reading=CPUReading(5.0, None, True, "cpu_sample_available"),
        )
        harness.config["schedules"] = [
            schedule_to_dict(cpu_item),
            schedule_to_dict(other),
        ]
        occurrence = ScheduledOccurrence.create(
            cpu_item.id,
            datetime(2026, 8, 31, 23, 30),
            60,
        )
        harness.evaluate_conditions(cpu_item, started, occurrence)
        updated_other = Schedule(**{**asdict(other), "name": "Updated"})

        with patch("amp_autopower.save_json"):
            harness.set_schedules(
                [cpu_item, updated_other],
                now=started + timedelta(minutes=1),
            )

        self.assertEqual(
            harness.condition_engine.runtime.cpu_satisfied_since(cpu_item.id),
            started,
        )

    def test_interval_and_cpu_pending_keeps_original_target(self):
        start = datetime(2026, 8, 31, 22, 30)
        occurrence = ScheduledOccurrence.create_interval(
            "interval",
            start,
            60,
            60,
        )
        target = occurrence.scheduled_target
        state = {
            "last_runs": {},
            "snoozes": {},
            "skipped_targets": {},
            "pending_occurrences": {"interval": occurrence.to_state()},
            "completed_intervals": {},
        }
        item = Schedule(
            id="interval",
            use_time=False,
            trigger_mode="interval",
            interval_minutes=60,
            weekdays=[],
            require_cpu=True,
            cpu_threshold=10,
            cpu_duration_seconds=300,
        )
        harness = SchedulerHarness(
            state,
            cpu_reading=CPUReading(50.0, None, True, "cpu_sample_available"),
        )

        with patch("amp_autopower.save_json"):
            harness._pending_occurrence_tick(item, occurrence, target)

        persisted = harness.pending_occurrence(item)
        self.assertTrue(persisted.armed)
        self.assertEqual(persisted.start_at, start)
        self.assertEqual(persisted.scheduled_target, target)
        self.assertEqual(harness.started, [])

    def test_cpu_snooze_keeps_original_interval_and_target(self):
        start = datetime(2026, 8, 31, 22, 30)
        occurrence = ScheduledOccurrence.create_interval(
            "interval",
            start,
            60,
            60,
        ).mark_armed()
        target = occurrence.scheduled_target
        snooze = target + timedelta(minutes=10)
        state = {
            "last_runs": {},
            "snoozes": {"interval": snooze.isoformat()},
            "skipped_targets": {},
            "pending_occurrences": {"interval": occurrence.to_state()},
            "completed_intervals": {},
        }
        item = Schedule(
            id="interval",
            use_time=False,
            trigger_mode="interval",
            interval_minutes=60,
            weekdays=[],
            require_cpu=True,
            cpu_threshold=10,
            cpu_duration_seconds=300,
        )
        harness = SchedulerHarness(
            state,
            cpu_reading=CPUReading(50.0, None, True, "cpu_sample_available"),
        )

        with patch("amp_autopower.save_json"):
            harness._pending_occurrence_tick(
                item,
                occurrence,
                snooze - timedelta(seconds=60),
            )

        persisted = harness.pending_occurrence(item)
        self.assertEqual(persisted.start_at, start)
        self.assertEqual(persisted.scheduled_target, target)
        self.assertEqual(item.cpu_duration_seconds, 300)
        self.assertEqual(harness.started, [])

    def test_cpu_pending_can_cross_midnight_without_changing_target(self):
        target = datetime(2026, 9, 4, 23, 30)
        occurrence = ScheduledOccurrence.create(
            "timed",
            target,
            60,
        ).mark_armed()
        state = {
            "last_runs": {},
            "snoozes": {},
            "skipped_targets": {},
            "pending_occurrences": {"timed": occurrence.to_state()},
        }
        item = Schedule(
            id="timed",
            time="23:30",
            weekdays=[4],
            require_cpu=True,
            cpu_threshold=10,
            cpu_duration_seconds=60,
        )
        harness = SchedulerHarness(
            state,
            cpu_reading=CPUReading(5.0, None, True, "cpu_sample_available"),
        )

        with patch("amp_autopower.save_json"):
            harness._pending_occurrence_tick(item, occurrence, target)
            persisted = harness.pending_occurrence(item)
            for seconds in range(1, 40 * 60 + 1):
                harness.evaluate_conditions(
                    item,
                    target + timedelta(seconds=seconds),
                    persisted,
                    pending=True,
                )
            harness._pending_occurrence_tick(
                item,
                persisted,
                target + timedelta(minutes=40),
            )

        self.assertEqual(len(harness.started), 1)
        self.assertEqual(harness.started[0][1], target)
        self.assertEqual(harness.pending_occurrence(item).scheduled_target, target)

    def test_pending_occurrence_survives_restart_and_uses_original_target(self):
        target = datetime(2026, 9, 4, 23, 30)
        ready_at = datetime(2026, 9, 5, 0, 10)
        occurrence = ScheduledOccurrence.create(
            "timed",
            target,
            60,
            created_at=target - timedelta(minutes=1),
        ).mark_armed().with_next_check(ready_at)
        state = {
            "last_runs": {},
            "snoozes": {},
            "skipped_targets": {},
            "pending_occurrences": {
                "timed": occurrence.to_state(),
            },
        }
        item = Schedule(
            id="timed",
            time="23:30",
            weekdays=[4],
            final_countdown_seconds=60,
            require_idle=True,
            idle_minutes=30,
        )

        restarted = SchedulerHarness(state, idle_seconds=30 * 60)
        with patch("amp_autopower.save_json"):
            restored = restarted.pending_occurrence(item)
            restarted._pending_occurrence_tick(item, restored, ready_at)

        self.assertEqual(restored.scheduled_target, target)
        self.assertEqual(len(restarted.started), 1)
        self.assertEqual(restarted.started[0][1], target)
        self.assertEqual(restarted.started[0][2], 60)
        self.assertNotEqual(restarted.started[0][1], ready_at)

    def test_unmet_idle_persists_pending_original_instead_of_snooze_target(self):
        target = datetime(2026, 8, 31, 23, 30)
        occurrence = ScheduledOccurrence.create(
            "timed",
            target,
            60,
            created_at=target - timedelta(minutes=1),
        )
        state = {
            "last_runs": {},
            "snoozes": {},
            "skipped_targets": {},
            "pending_occurrences": {},
        }
        item = Schedule(
            id="timed",
            time="23:30",
            weekdays=[0],
            require_idle=True,
            idle_minutes=30,
        )
        harness = SchedulerHarness(state, idle_seconds=10 * 60)

        with patch("amp_autopower.save_json"):
            harness.defer_for_idle(
                item,
                occurrence,
                target,
            )

        persisted = harness.pending_occurrence(item)
        self.assertEqual(persisted.scheduled_target, target)
        self.assertTrue(persisted.armed)
        self.assertGreater(persisted.next_check_at, target)
        self.assertNotIn(item.id, harness.state["snoozes"])

    def test_completed_pending_occurrence_does_not_start_again(self):
        target = datetime(2026, 8, 31, 23, 30)
        occurrence = ScheduledOccurrence.create(
            "timed",
            target,
            60,
            created_at=target - timedelta(minutes=1),
        ).mark_armed()
        state = {
            "last_runs": {"timed": target.isoformat()},
            "snoozes": {},
            "skipped_targets": {},
            "pending_occurrences": {
                "timed": occurrence.to_state(),
            },
        }
        item = Schedule(id="timed", time="23:30", weekdays=[0])
        harness = SchedulerHarness(state)

        with patch("amp_autopower.save_json"):
            harness._pending_occurrence_tick(
                item,
                occurrence,
                target + timedelta(minutes=1),
            )

        self.assertEqual(harness.started, [])
        self.assertNotIn(item.id, state["pending_occurrences"])

    def test_user_snooze_starts_countdown_before_snooze_deadline(self):
        target = datetime(2026, 8, 31, 23, 30)
        snooze = target + timedelta(minutes=10)
        occurrence = ScheduledOccurrence.create(
            "timed",
            target,
            60,
            created_at=target - timedelta(minutes=1),
        ).mark_armed()
        state = {
            "last_runs": {},
            "snoozes": {"timed": snooze.isoformat()},
            "skipped_targets": {},
            "pending_occurrences": {
                "timed": occurrence.to_state(),
            },
        }
        item = Schedule(id="timed", time="23:30", weekdays=[0])
        harness = SchedulerHarness(state)

        with patch("amp_autopower.save_json"):
            harness._pending_occurrence_tick(
                item,
                occurrence,
                snooze - timedelta(seconds=60),
            )

        self.assertEqual(len(harness.started), 1)
        self.assertEqual(harness.started[0][1], target)
        self.assertEqual(harness.started[0][2], 60)

    def test_pending_action_order_uses_user_snooze_not_original_target(self):
        now = datetime(2026, 8, 31, 22, 20)
        original = now.replace(hour=22, minute=0)
        snooze = now.replace(hour=23, minute=0)
        occurrence = ScheduledOccurrence.create(
            "timed",
            original,
            60,
            created_at=original - timedelta(minutes=1),
        ).mark_armed()
        state = {
            "last_runs": {},
            "snoozes": {"timed": snooze.isoformat()},
            "skipped_targets": {},
            "pending_occurrences": {
                "timed": occurrence.to_state(),
            },
        }
        item = Schedule(id="timed", time="22:00", weekdays=[0])
        harness = SchedulerHarness(state)

        due = harness.pending_action_time(item, occurrence, now)

        self.assertEqual(due, snooze)
        self.assertNotEqual(due, original)

    def test_retry_before_original_target_still_finishes_at_original_time(self):
        target = datetime(2026, 8, 31, 23, 30)
        occurrence = ScheduledOccurrence.create(
            "timed",
            target,
            600,
            created_at=target - timedelta(minutes=10),
        ).with_next_check(target - timedelta(minutes=8))
        state = {
            "last_runs": {},
            "snoozes": {},
            "skipped_targets": {},
            "pending_occurrences": {
                "timed": occurrence.to_state(),
            },
        }
        item = Schedule(
            id="timed",
            time="23:30",
            weekdays=[0],
            final_countdown_seconds=600,
        )
        harness = SchedulerHarness(state)

        due = harness.pending_action_time(
            item,
            occurrence,
            target - timedelta(minutes=9),
        )

        self.assertEqual(due, target)

    def test_retry_at_original_target_still_finishes_at_original_time(self):
        target = datetime(2026, 8, 31, 23, 30)
        occurrence = ScheduledOccurrence.create(
            "timed",
            target,
            120,
            created_at=target - timedelta(minutes=2),
        ).with_next_check(target)
        state = {
            "last_runs": {},
            "snoozes": {},
            "skipped_targets": {},
            "pending_occurrences": {
                "timed": occurrence.to_state(),
            },
        }
        item = Schedule(
            id="timed",
            time="23:30",
            weekdays=[0],
            final_countdown_seconds=120,
        )
        harness = SchedulerHarness(state)

        due = harness.pending_action_time(
            item,
            occurrence,
            target - timedelta(seconds=2),
        )

        self.assertEqual(due, target)

    def test_switching_to_idle_only_prunes_timed_pending_occurrence(self):
        target = datetime(2026, 8, 31, 23, 30)
        occurrence = ScheduledOccurrence.create("timed", target, 60)
        state = {
            "last_runs": {},
            "snoozes": {},
            "skipped_targets": {},
            "pending_occurrences": {
                "timed": occurrence.to_state(),
            },
        }
        harness = SchedulerHarness(state)
        item = Schedule(
            id="timed",
            use_time=False,
            require_idle=True,
            weekdays=[0],
        )

        with patch("amp_autopower.save_json"):
            harness._prune_pending_occurrences([item])

        self.assertNotIn(item.id, state["pending_occurrences"])

    def test_malformed_schedule_does_not_break_pending_pruning(self):
        state = {
            "last_runs": {},
            "snoozes": {},
            "skipped_targets": {},
            "pending_occurrences": {},
        }
        harness = SchedulerHarness(state)
        harness.config["schedules"] = [None]

        harness._prune_pending_occurrences()

        self.assertEqual(state["pending_occurrences"], {})

    def test_last_run_prevents_resolving_same_occurrence_again(self):
        target = datetime(2026, 8, 31, 23, 30)
        item = Schedule(id="timed", time="23:30", weekdays=[0])
        owner = SimpleNamespace(
            state={
                "last_runs": {item.id: target.isoformat()},
                "snoozes": {},
                "skipped_targets": {},
            }
        )

        resolved = MainWindow.next_occurrence(
            owner,
            item,
            target - timedelta(seconds=1),
        )

        self.assertEqual(resolved, target + timedelta(days=7))

    def test_legacy_future_snooze_remains_supported(self):
        now = datetime(2026, 8, 31, 23, 0)
        snooze = now + timedelta(minutes=10)
        item = Schedule(id="timed", time="23:30", weekdays=[0])
        owner = SimpleNamespace(
            state={
                "last_runs": {},
                "snoozes": {item.id: snooze.isoformat()},
                "skipped_targets": {},
            }
        )

        target = MainWindow.next_occurrence(owner, item, now)

        self.assertEqual(target, snooze)

    def test_skipped_target_advances_to_next_selected_weekday(self):
        now = datetime(2026, 8, 31, 23, 0)
        skipped = now.replace(hour=23, minute=30)
        item = Schedule(id="timed", time="23:30", weekdays=[0])
        owner = SimpleNamespace(
            state={
                "last_runs": {},
                "snoozes": {},
                "skipped_targets": {item.id: skipped.isoformat()},
            }
        )

        target = MainWindow.next_occurrence(owner, item, now)

        self.assertEqual(target, skipped + timedelta(days=7))


if __name__ == "__main__":
    unittest.main()
