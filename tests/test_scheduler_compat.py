import signal
import unittest
from dataclasses import asdict
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from amp_autopower import (
    CONFIG_FILE,
    STATE_FILE,
    MainWindow,
    Schedule,
    schedule_to_dict,
)
from condition_engine import (
    CPUReading,
    ConditionEngine,
    NetworkReading,
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
    _close_chrome_cleanly = MainWindow._close_chrome_cleanly
    _handle_pre_action_failure = MainWindow._handle_pre_action_failure
    _finish_pre_action = MainWindow._finish_pre_action
    _cancel_pre_action = MainWindow._cancel_pre_action
    _pre_action_timed_out = MainWindow._pre_action_timed_out
    _kill_timed_out_pre_action = MainWindow._kill_timed_out_pre_action
    _run_pre_action = MainWindow._run_pre_action
    execute_action = MainWindow.execute_action
    on_countdown_finished = MainWindow.on_countdown_finished
    _run_cancel_action_command = MainWindow._run_cancel_action_command
    _find_qdbus = MainWindow._find_qdbus
    _session_action_command = MainWindow._session_action_command
    _run_final_command = MainWindow._run_final_command
    _execute_final_action = MainWindow._execute_final_action
    evaluate_conditions = MainWindow.evaluate_conditions
    defer_for_idle = MainWindow.defer_for_idle
    _defer_for_conditions = MainWindow._defer_for_conditions
    _has_active_dialog_for_schedule = MainWindow._has_active_dialog_for_schedule
    _pending_occurrence_tick = MainWindow._pending_occurrence_tick
    _idle_only_tick = MainWindow._idle_only_tick
    _timed_schedule_tick = MainWindow._timed_schedule_tick
    next_occurrence = MainWindow.next_occurrence
    cancel_next_run = MainWindow.cancel_next_run
    snooze = MainWindow.snooze
    set_schedules = MainWindow.set_schedules
    set_schedule_enabled = MainWindow.set_schedule_enabled
    mark_skipped = MainWindow.mark_skipped

    def __init__(
        self,
        state,
        idle_seconds=0,
        reliable=True,
        cpu_reading=None,
        network_reading=None,
    ):
        self.state = state
        self.condition_engine = ConditionEngine()
        self.active_dialogs = {}
        self.config = {
            "overlay_all_schedule_warnings": False,
            "schedules": [],
        }
        self.warned = set()
        self._pre_action_processes = {}
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
        self._network_reading = network_reading or NetworkReading(
            None,
            None,
            None,
            None,
            False,
            "network_monitor_warming_up",
        )
        self.network_monitor = SimpleNamespace(
            reading=lambda _interface, _direction, _window=0: (
                self._network_reading
            ),
        )
        self.started = []
        self.notifications = []

    def idle_seconds(self):
        return self._idle_seconds

    def input_monitor_reliable(self):
        return self._reliable

    def notify(self, *args, **kwargs):
        self.notifications.append((args, kwargs))

    def show_warning_banner(self, *_args, **_kwargs):
        pass

    def start_final_countdown(self, schedule, target, seconds, key):
        self.started.append((schedule.id, target, seconds, key))

    def schedules(self):
        out = []
        cpu_settings = self.config.get("cpu_settings", {})
        network_settings = self.config.get("network_settings", {})
        logic_settings = self.config.get("condition_logic_settings", {})
        action_settings = self.config.get("action_settings", {})
        for raw in self.config.get("schedules", []):
            data = dict(raw)
            preset = cpu_settings.get(data.get("id"), {})
            if isinstance(preset, dict):
                for key, value in preset.items():
                    data.setdefault(key, value)
            network_preset = network_settings.get(data.get("id"), {})
            if isinstance(network_preset, dict):
                for key, value in network_preset.items():
                    data.setdefault(key, value)
            data.setdefault(
                "condition_logic",
                logic_settings.get(data.get("id"), "AND"),
            )
            action_preset = action_settings.get(data.get("id"), {})
            if isinstance(action_preset, dict):
                for key, value in action_preset.items():
                    data.setdefault(key, value)
            out.append(Schedule(**data))
        return out

    def refresh_list(self):
        pass


class FakeSignal:
    def __init__(self):
        self.callbacks = []

    def connect(self, callback):
        self.callbacks.append(callback)

    def emit(self, *args):
        for callback in list(self.callbacks):
            callback(*args)


class FakeTimer:
    instances = []

    def __init__(self, _parent=None):
        self.timeout = FakeSignal()
        self.interval = None
        self.stopped = False
        self.__class__.instances.append(self)

    def setSingleShot(self, _single_shot):
        pass

    def start(self, interval):
        self.interval = interval

    def stop(self):
        self.stopped = True


class FakeProcess:
    NotRunning = 0
    Running = 1
    instances = []

    def __init__(self, _parent=None):
        self.finished = FakeSignal()
        self.errorOccurred = FakeSignal()
        self.program = None
        self.arguments = None
        self.started = False
        self.terminated = False
        self.killed = False
        self.deleted = False
        self.process_state = self.NotRunning
        self.exit_code = 0
        self.stderr = b""
        self.stdout = b""
        self.error_text = ""
        self.__class__.instances.append(self)

    def setProgram(self, program):
        self.program = program

    def setArguments(self, arguments):
        self.arguments = arguments

    def start(self):
        self.started = True
        self.process_state = self.Running

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def state(self):
        return self.process_state

    def exitCode(self):
        return self.exit_code

    def readAllStandardError(self):
        return self.stderr

    def readAllStandardOutput(self):
        return self.stdout

    def errorString(self):
        return self.error_text

    def deleteLater(self):
        self.deleted = True


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
        self.assertEqual(loaded.condition_logic, "AND")

    def test_legacy_modes_serialize_without_interval_fields(self):
        timed = Schedule(id="timed", use_time=True, trigger_mode="time")
        idle = Schedule(id="idle", use_time=False, trigger_mode="idle")

        for item in (timed, idle):
            serialized = schedule_to_dict(item)
            self.assertNotIn("trigger_mode", serialized)
            self.assertNotIn("interval_minutes", serialized)
            self.assertNotIn("require_cpu", serialized)
            self.assertNotIn("cpu_threshold", serialized)
            self.assertNotIn("require_network", serialized)
            self.assertNotIn("network_threshold", serialized)
            self.assertNotIn("condition_logic", serialized)
            self.assertNotIn("pre_action_enabled", serialized)
            self.assertNotIn("pre_action_command", serialized)
            self.assertNotIn("cancel_action_behavior", serialized)
            self.assertNotIn("cancel_action_command", serialized)

        serialized_or = schedule_to_dict(Schedule(id="or", condition_logic="OR"))
        self.assertNotIn("condition_logic", serialized_or)

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

    def test_disabled_network_preserves_custom_configuration(self):
        item = Schedule(
            id="network",
            require_network=False,
            network_interface="tailscale0",
            network_direction="tx",
            network_comparison="greater",
            network_threshold=2,
            network_unit="MB/s",
            network_duration_seconds=30,
            network_use_average=True,
            network_average_seconds=15,
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
        self.assertNotIn("require_network", serialized)
        self.assertNotIn("network_interface", serialized)
        self.assertFalse(restored.require_network)
        self.assertEqual(restored.network_interface, "tailscale0")
        self.assertEqual(restored.network_direction, "tx")
        self.assertEqual(restored.network_comparison, "greater")
        self.assertEqual(restored.network_threshold, 2)
        self.assertEqual(restored.network_unit, "MB/s")
        self.assertEqual(restored.network_duration_seconds, 30)
        self.assertTrue(restored.network_use_average)
        self.assertEqual(restored.network_average_seconds, 15)

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

    def test_cli_disable_interval_uses_central_cleanup(self):
        now = datetime(2026, 8, 31, 12, 0)
        item = Schedule(
            id="cli-interval",
            name="CLI interval",
            use_time=False,
            trigger_mode="interval",
            interval_minutes=45,
            weekdays=[],
        )
        state = {
            "last_runs": {},
            "snoozes": {item.id: (now + timedelta(minutes=10)).isoformat()},
            "skipped_targets": {},
            "pending_occurrences": {
                item.id: ScheduledOccurrence.create_interval(
                    item.id,
                    now - timedelta(minutes=5),
                    45,
                    60,
                ).to_state(),
            },
            "completed_intervals": {},
        }
        harness = SchedulerHarness(state)
        harness.config["schedules"] = [schedule_to_dict(item)]

        with patch("amp_autopower.save_json") as save:
            ok, _message, code = harness.set_schedule_enabled(
                item.id,
                False,
                now=now,
            )

        self.assertTrue(ok)
        self.assertEqual(code, 0)
        self.assertFalse(harness.schedules()[0].enabled)
        self.assertNotIn(item.id, state["pending_occurrences"])
        self.assertNotIn(item.id, state["snoozes"])
        saved_paths = [call.args[0] for call in save.call_args_list]
        self.assertIn(CONFIG_FILE, saved_paths)
        self.assertIn(STATE_FILE, saved_paths)

    def test_cli_enable_interval_starts_fresh_occurrence(self):
        now = datetime(2026, 8, 31, 12, 0)
        item = Schedule(
            id="cli-interval",
            name="CLI interval",
            enabled=False,
            use_time=False,
            trigger_mode="interval",
            interval_minutes=45,
            weekdays=[],
        )
        state = {
            "last_runs": {item.id: "old-run"},
            "snoozes": {item.id: (now + timedelta(minutes=10)).isoformat()},
            "skipped_targets": {item.id: "old-target"},
            "pending_occurrences": {},
            "completed_intervals": {item.id: "completed"},
        }
        harness = SchedulerHarness(state)
        harness.config["schedules"] = [schedule_to_dict(item)]

        with patch("amp_autopower.save_json") as save:
            ok, _message, code = harness.set_schedule_enabled(
                item.name,
                True,
                now=now,
            )

        enabled = harness.schedules()[0]
        occurrence = harness.pending_occurrence(enabled)
        self.assertTrue(ok)
        self.assertEqual(code, 0)
        self.assertTrue(enabled.enabled)
        self.assertEqual(occurrence.start_at, now)
        self.assertEqual(
            occurrence.scheduled_target,
            now + timedelta(minutes=45),
        )
        for state_key in (
            "last_runs",
            "snoozes",
            "skipped_targets",
            "completed_intervals",
        ):
            self.assertNotIn(item.id, state[state_key])
        saved_paths = [call.args[0] for call in save.call_args_list]
        self.assertIn(CONFIG_FILE, saved_paths)
        self.assertIn(STATE_FILE, saved_paths)

    def test_cli_disable_timed_schedule_prunes_pending_and_snooze(self):
        now = datetime(2026, 8, 31, 12, 0)
        item = Schedule(id="cli-time", name="CLI time", time="13:00")
        occurrence = ScheduledOccurrence.create(
            item.id,
            now + timedelta(hours=1),
            60,
        )
        state = {
            "last_runs": {},
            "snoozes": {item.id: (now + timedelta(hours=2)).isoformat()},
            "skipped_targets": {},
            "pending_occurrences": {item.id: occurrence.to_state()},
            "completed_intervals": {},
        }
        harness = SchedulerHarness(state)
        harness.config["schedules"] = [schedule_to_dict(item)]

        with patch("amp_autopower.save_json"):
            ok, _message, code = harness.set_schedule_enabled(
                item.id,
                False,
                now=now,
            )

        self.assertTrue(ok)
        self.assertEqual(code, 0)
        self.assertFalse(harness.schedules()[0].enabled)
        self.assertNotIn(item.id, state["pending_occurrences"])
        self.assertNotIn(item.id, state["snoozes"])

    def test_cli_disable_idle_schedule_clears_snooze_and_runtime(self):
        now = datetime(2026, 8, 31, 12, 0)
        item = Schedule(
            id="cli-idle",
            name="CLI idle",
            use_time=False,
            trigger_mode="idle",
            idle_minutes=20,
        )
        state = {
            "last_runs": {},
            "snoozes": {item.id: (now + timedelta(minutes=10)).isoformat()},
            "skipped_targets": {},
            "pending_occurrences": {},
            "completed_intervals": {},
        }
        harness = SchedulerHarness(state)
        harness.config["schedules"] = [schedule_to_dict(item)]
        runtime = harness.condition_engine.runtime
        runtime.mark_idle_cycle_triggered(item.id)
        runtime.mark_idle_snoozed(item.id)

        with patch("amp_autopower.save_json"):
            ok, _message, code = harness.set_schedule_enabled(
                item.id,
                False,
                now=now,
            )

        self.assertTrue(ok)
        self.assertEqual(code, 0)
        self.assertNotIn(item.id, state["snoozes"])
        self.assertFalse(runtime.idle_cycle_was_triggered(item.id))
        self.assertNotIn(item.id, runtime._idle_snooze_generation)

    def test_cli_enable_active_interval_preserves_existing_occurrence(self):
        start = datetime(2026, 8, 31, 12, 0)
        now = start + timedelta(minutes=5)
        item = Schedule(
            id="cli-active-interval",
            name="CLI active interval",
            use_time=False,
            trigger_mode="interval",
            interval_minutes=45,
            weekdays=[],
        )
        occurrence = ScheduledOccurrence.create_interval(
            item.id,
            start,
            45,
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

        with patch("amp_autopower.save_json"):
            ok, message, code = harness.set_schedule_enabled(
                item.id,
                True,
                now=now,
            )

        preserved = harness.pending_occurrence(item)
        self.assertTrue(ok)
        self.assertEqual(code, 0)
        self.assertIn("sin cambios", message)
        self.assertEqual(preserved.start_at, start)
        self.assertEqual(preserved.scheduled_target, occurrence.scheduled_target)

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
        harness.config["cpu_settings"] = {item.id: {"cpu_threshold": 25}}
        harness.config["network_settings"] = {
            item.id: {"network_interface": "enp1s0"}
        }
        harness.config["condition_logic_settings"] = {item.id: "OR"}
        harness.config["action_settings"] = {
            item.id: {"pre_action_enabled": True}
        }

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
        self.assertNotIn(item.id, harness.config["cpu_settings"])
        self.assertNotIn(item.id, harness.config["network_settings"])
        self.assertNotIn(item.id, harness.config["condition_logic_settings"])
        self.assertNotIn(item.id, harness.config["action_settings"])

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

    def test_time_and_network_pending_keeps_original_target(self):
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
            require_network=True,
            network_interface="enp1s0",
            network_threshold=50,
            network_duration_seconds=300,
        )
        harness = SchedulerHarness(
            state,
            network_reading=NetworkReading(
                60 * 1024,
                10 * 1024,
                70 * 1024,
                None,
                True,
                "network_sample_available",
            ),
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

    def test_interval_and_network_pending_keeps_start_and_target(self):
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
            require_network=True,
            network_interface="wlan0",
            network_threshold=50,
            network_duration_seconds=300,
        )
        harness = SchedulerHarness(
            state,
            network_reading=NetworkReading(
                60 * 1024,
                10 * 1024,
                70 * 1024,
                None,
                True,
                "network_sample_available",
            ),
        )

        with patch("amp_autopower.save_json"):
            harness._pending_occurrence_tick(item, occurrence, target)

        persisted = harness.pending_occurrence(item)
        self.assertTrue(persisted.armed)
        self.assertEqual(persisted.start_at, start)
        self.assertEqual(persisted.scheduled_target, target)
        self.assertEqual(harness.started, [])

    def test_network_snooze_keeps_original_interval_and_configuration(self):
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
            require_network=True,
            network_interface="tailscale0",
            network_threshold=50,
            network_duration_seconds=300,
        )
        harness = SchedulerHarness(
            state,
            network_reading=NetworkReading(
                60 * 1024,
                10 * 1024,
                70 * 1024,
                None,
                True,
                "network_sample_available",
            ),
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
        self.assertEqual(item.network_interface, "tailscale0")
        self.assertEqual(item.network_duration_seconds, 300)
        self.assertEqual(harness.started, [])

    def test_missing_network_interface_never_starts_countdown(self):
        target = datetime(2026, 8, 31, 23, 30)
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
            weekdays=[0],
            require_network=True,
            network_interface="missing0",
            network_duration_seconds=0,
        )
        harness = SchedulerHarness(
            state,
            network_reading=NetworkReading(
                None,
                None,
                None,
                None,
                False,
                "network_interface_unavailable",
            ),
        )

        with patch("amp_autopower.save_json"):
            harness._pending_occurrence_tick(
                item,
                occurrence,
                target + timedelta(minutes=10),
            )

        self.assertEqual(harness.started, [])
        self.assertEqual(
            harness.pending_occurrence(item).scheduled_target,
            target,
        )

    def test_network_pending_can_cross_midnight_without_changing_target(self):
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
            require_network=True,
            network_interface="enp1s0",
            network_threshold=50,
            network_duration_seconds=60,
        )
        harness = SchedulerHarness(
            state,
            network_reading=NetworkReading(
                10 * 1024,
                5 * 1024,
                15 * 1024,
                None,
                True,
                "network_sample_available",
            ),
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

    def test_condition_logic_round_trip_uses_compatibility_map(self):
        state = {
            "last_runs": {},
            "snoozes": {},
            "skipped_targets": {},
            "pending_occurrences": {},
            "completed_intervals": {},
        }
        harness = SchedulerHarness(state)
        item = Schedule(id="logic", condition_logic="OR")

        with patch("amp_autopower.save_json"):
            harness.set_schedules(
                [item],
                now=datetime(2026, 8, 31, 12, 0),
            )

        self.assertNotIn(
            "condition_logic",
            harness.config["schedules"][0],
        )
        self.assertEqual(
            harness.config["condition_logic_settings"][item.id],
            "OR",
        )
        self.assertEqual(harness.schedules()[0].condition_logic, "OR")

        with patch("amp_autopower.save_json"):
            harness.set_schedules(
                [Schedule(id="logic", condition_logic="AND")],
                now=datetime(2026, 8, 31, 12, 1),
            )

        self.assertNotIn(
            item.id,
            harness.config["condition_logic_settings"],
        )

    def test_pre_action_round_trip_uses_compatibility_map(self):
        state = {
            "last_runs": {},
            "snoozes": {},
            "skipped_targets": {},
            "pending_occurrences": {},
            "completed_intervals": {},
        }
        harness = SchedulerHarness(state)
        item = Schedule(
            id="pre-action",
            pre_action_enabled=True,
            pre_action_command="/usr/bin/example --save",
            pre_action_wait=False,
            pre_action_timeout_seconds=90,
            pre_action_failure_policy="continue",
            cancel_action_behavior="command",
            cancel_action_command='"/tmp/Cancel Tool" --undo',
        )

        with patch("amp_autopower.save_json"):
            harness.set_schedules(
                [item],
                now=datetime(2026, 8, 31, 12, 0),
            )

        serialized = harness.config["schedules"][0]
        for field in (
            "pre_action_enabled",
            "pre_action_command",
            "pre_action_wait",
            "pre_action_timeout_seconds",
            "pre_action_failure_policy",
        ):
            self.assertNotIn(field, serialized)
        self.assertEqual(
            harness.config["action_settings"][item.id],
            {
                "pre_action_enabled": True,
                "pre_action_command": "/usr/bin/example --save",
                "pre_action_wait": False,
                "pre_action_timeout_seconds": 90,
                "pre_action_failure_policy": "continue",
                "cancel_action_behavior": "command",
                "cancel_action_command": '"/tmp/Cancel Tool" --undo',
            },
        )
        restored = harness.schedules()[0]
        self.assertTrue(restored.pre_action_enabled)
        self.assertEqual(restored.pre_action_command, item.pre_action_command)
        self.assertFalse(restored.pre_action_wait)
        self.assertEqual(restored.pre_action_timeout_seconds, 90)
        self.assertEqual(restored.pre_action_failure_policy, "continue")
        self.assertEqual(restored.cancel_action_behavior, "command")
        self.assertEqual(
            restored.cancel_action_command,
            '"/tmp/Cancel Tool" --undo',
        )

        with patch("amp_autopower.save_json"):
            harness.set_schedules(
                [Schedule(id=item.id)],
                now=datetime(2026, 8, 31, 12, 1),
            )

        self.assertNotIn(item.id, harness.config["action_settings"])

    def test_disabled_pre_action_runs_final_action_directly(self):
        state = {
            "last_runs": {},
            "snoozes": {},
            "skipped_targets": {},
            "pending_occurrences": {},
            "completed_intervals": {},
        }
        harness = SchedulerHarness(state)
        harness._run_pre_action = MagicMock()
        target = datetime(2026, 8, 31, 12, 0)
        item = Schedule(
            id="disabled-pre-action",
            action="test",
            pre_action_enabled=False,
            pre_action_command="/does/not/exist",
        )

        with patch("amp_autopower.save_json"):
            harness.execute_action(item, target)

        harness._run_pre_action.assert_not_called()
        self.assertEqual(state["last_runs"][item.id], target.isoformat())

    def test_waited_pre_action_parses_arguments_and_runs_final_action(self):
        state = {
            "last_runs": {},
            "snoozes": {},
            "skipped_targets": {},
            "pending_occurrences": {},
            "completed_intervals": {},
        }
        harness = SchedulerHarness(state)
        target = datetime(2026, 8, 31, 12, 0)
        item = Schedule(
            id="pre-action",
            action="test",
            pre_action_enabled=True,
            pre_action_command='"/tmp/My Tool" --flag "two words"',
            pre_action_timeout_seconds=17,
        )
        FakeProcess.instances = []
        FakeTimer.instances = []

        with (
            patch("amp_autopower.QProcess", FakeProcess),
            patch("amp_autopower.QTimer", FakeTimer),
            patch("amp_autopower.save_json") as save,
        ):
            harness.execute_action(item, target)
            process = FakeProcess.instances[0]
            self.assertEqual(process.program, "/tmp/My Tool")
            self.assertEqual(process.arguments, ["--flag", "two words"])
            self.assertTrue(process.started)
            self.assertEqual(FakeTimer.instances[0].interval, 17000)
            self.assertNotIn(item.id, state["last_runs"])

            process.process_state = FakeProcess.NotRunning
            process.finished.emit(0, None)
            process.finished.emit(0, None)

        self.assertEqual(state["last_runs"][item.id], target.isoformat())
        save.assert_called_once()
        self.assertTrue(process.deleted)
        self.assertEqual(harness._pre_action_processes, {})

    def test_running_pre_action_blocks_duplicate_countdown(self):
        target = datetime(2026, 8, 31, 12, 0)
        occurrence = ScheduledOccurrence.create_interval(
            "interval",
            target - timedelta(minutes=30),
            30,
            60,
        )
        state = {
            "last_runs": {},
            "snoozes": {},
            "skipped_targets": {},
            "pending_occurrences": {
                "interval": occurrence.to_state(),
            },
            "completed_intervals": {},
        }
        harness = SchedulerHarness(state)
        item = Schedule(
            id="interval",
            use_time=False,
            trigger_mode="interval",
            interval_minutes=30,
            weekdays=[],
            action="test",
            pre_action_enabled=True,
            pre_action_command="/usr/bin/example",
        )
        harness.config["schedules"] = [schedule_to_dict(item)]
        FakeProcess.instances = []
        FakeTimer.instances = []

        with (
            patch("amp_autopower.QProcess", FakeProcess),
            patch("amp_autopower.QTimer", FakeTimer),
            patch("amp_autopower.save_json"),
        ):
            harness.execute_action(item, target)
            self.assertTrue(harness._has_active_dialog_for_schedule(item.id))
            harness._pending_occurrence_tick(item, occurrence, target)
            self.assertEqual(harness.started, [])

            process = FakeProcess.instances[0]
            process.process_state = FakeProcess.NotRunning
            process.finished.emit(0, None)

        self.assertIn(item.id, state["completed_intervals"])

    def test_nonzero_pre_action_applies_cancel_or_continue_policy(self):
        target = datetime(2026, 8, 31, 12, 0)

        for policy in ("cancel", "continue"):
            with self.subTest(policy=policy):
                occurrence = ScheduledOccurrence.create(policy, target, 60)
                state = {
                    "last_runs": {},
                    "snoozes": {},
                    "skipped_targets": {},
                    "pending_occurrences": {
                        policy: occurrence.to_state(),
                    },
                    "completed_intervals": {},
                }
                harness = SchedulerHarness(state)
                item = Schedule(
                    id=policy,
                    action="test",
                    pre_action_enabled=True,
                    pre_action_command="/usr/bin/example",
                    pre_action_failure_policy=policy,
                )
                FakeProcess.instances = []
                FakeTimer.instances = []

                with (
                    patch("amp_autopower.QProcess", FakeProcess),
                    patch("amp_autopower.QTimer", FakeTimer),
                    patch("amp_autopower.save_json"),
                ):
                    harness.execute_action(item, target)
                    process = FakeProcess.instances[0]
                    process.stderr = b"simulated command failure"
                    process.process_state = FakeProcess.NotRunning
                    process.finished.emit(7, None)

                if policy == "cancel":
                    self.assertNotIn(item.id, state["last_runs"])
                    self.assertIn(item.id, state["pending_occurrences"])
                else:
                    self.assertEqual(
                        state["last_runs"][item.id],
                        target.isoformat(),
                    )
                self.assertTrue(
                    any(
                        "simulated command failure" in args[1]
                        for args, _kwargs in harness.notifications
                    )
                )

    def test_missing_pre_action_program_is_reported_and_cancelled(self):
        state = {
            "last_runs": {},
            "snoozes": {},
            "skipped_targets": {},
            "pending_occurrences": {},
            "completed_intervals": {},
        }
        harness = SchedulerHarness(state)
        target = datetime(2026, 8, 31, 12, 0)
        item = Schedule(
            id="missing",
            action="test",
            pre_action_enabled=True,
            pre_action_command="/does/not/exist",
        )
        FakeProcess.instances = []
        FakeTimer.instances = []

        with (
            patch("amp_autopower.QProcess", FakeProcess),
            patch("amp_autopower.QTimer", FakeTimer),
            patch("amp_autopower.save_json"),
        ):
            harness.execute_action(item, target)
            process = FakeProcess.instances[0]
            process.error_text = "No such file or directory"
            process.process_state = FakeProcess.NotRunning
            process.errorOccurred.emit("FailedToStart")

        self.assertNotIn(item.id, state["last_runs"])
        self.assertIn("No such file", harness.notifications[-1][0][1])

    def test_editing_schedule_cancels_stale_running_pre_action(self):
        state = {
            "last_runs": {},
            "snoozes": {},
            "skipped_targets": {},
            "pending_occurrences": {},
            "completed_intervals": {},
        }
        harness = SchedulerHarness(state)
        target = datetime(2026, 8, 31, 12, 0)
        item = Schedule(
            id="edited",
            name="Original",
            action="test",
            pre_action_enabled=True,
            pre_action_command="/usr/bin/example",
        )
        updated = Schedule(
            id=item.id,
            name="Updated",
            action="test",
            pre_action_enabled=True,
            pre_action_command="/usr/bin/example",
        )
        FakeProcess.instances = []
        FakeTimer.instances = []

        with (
            patch("amp_autopower.QProcess", FakeProcess),
            patch("amp_autopower.QTimer", FakeTimer),
            patch("amp_autopower.save_json"),
        ):
            harness.set_schedules([item], now=target)
            harness.execute_action(item, target)
            process = FakeProcess.instances[0]

            harness.set_schedules([updated], now=target)
            self.assertTrue(process.terminated)
            process.process_state = FakeProcess.NotRunning
            process.finished.emit(0, None)

        self.assertNotIn(item.id, state["last_runs"])
        self.assertEqual(harness._pre_action_processes, {})

    def test_cancel_next_run_stops_running_pre_action(self):
        target = datetime(2026, 8, 31, 12, 0)
        occurrence = ScheduledOccurrence.create_interval(
            "interval",
            target - timedelta(minutes=30),
            30,
            60,
        )
        state = {
            "last_runs": {},
            "snoozes": {},
            "skipped_targets": {},
            "pending_occurrences": {
                "interval": occurrence.to_state(),
            },
            "completed_intervals": {},
        }
        harness = SchedulerHarness(state)
        item = Schedule(
            id="interval",
            use_time=False,
            trigger_mode="interval",
            interval_minutes=30,
            weekdays=[],
            action="test",
            pre_action_enabled=True,
            pre_action_command="/usr/bin/example",
        )
        harness.config["schedules"] = [schedule_to_dict(item)]
        FakeProcess.instances = []
        FakeTimer.instances = []

        with (
            patch("amp_autopower.QProcess", FakeProcess),
            patch("amp_autopower.QTimer", FakeTimer),
            patch("amp_autopower.save_json"),
        ):
            harness.execute_action(item, target)
            process = FakeProcess.instances[0]
            harness.cancel_next_run()
            self.assertTrue(process.terminated)
            self.assertEqual(
                state["last_runs"][item.id],
                target.isoformat() + ":skipped",
            )

            process.process_state = FakeProcess.NotRunning
            process.finished.emit(0, None)

        self.assertEqual(
            state["last_runs"][item.id],
            target.isoformat() + ":skipped",
        )
        self.assertEqual(harness._pre_action_processes, {})

    def test_failed_pre_action_does_not_consume_interval(self):
        target = datetime(2026, 8, 31, 12, 0)
        occurrence = ScheduledOccurrence.create_interval(
            "interval",
            target - timedelta(minutes=30),
            30,
            60,
        )
        state = {
            "last_runs": {},
            "snoozes": {},
            "skipped_targets": {},
            "pending_occurrences": {
                "interval": occurrence.to_state(),
            },
            "completed_intervals": {},
        }
        harness = SchedulerHarness(state)
        item = Schedule(
            id="interval",
            use_time=False,
            trigger_mode="interval",
            interval_minutes=30,
            weekdays=[],
            action="test",
            pre_action_enabled=True,
            pre_action_command='"unterminated',
        )
        harness.config["schedules"] = [schedule_to_dict(item)]

        with patch("amp_autopower.save_json"):
            harness.execute_action(item, target)

        self.assertTrue(harness.schedules()[0].enabled)
        self.assertNotIn(item.id, state["last_runs"])
        self.assertNotIn(item.id, state["completed_intervals"])
        self.assertIn(item.id, state["pending_occurrences"])
        self.assertIn("comando no es válido", harness.notifications[-1][0][1])

    def test_pre_action_timeout_cancels_without_consuming_interval(self):
        target = datetime(2026, 8, 31, 12, 0)
        occurrence = ScheduledOccurrence.create_interval(
            "interval",
            target - timedelta(minutes=30),
            30,
            60,
        )
        state = {
            "last_runs": {},
            "snoozes": {},
            "skipped_targets": {},
            "pending_occurrences": {
                "interval": occurrence.to_state(),
            },
            "completed_intervals": {},
        }
        harness = SchedulerHarness(state)
        item = Schedule(
            id="interval",
            use_time=False,
            trigger_mode="interval",
            interval_minutes=30,
            weekdays=[],
            action="test",
            pre_action_enabled=True,
            pre_action_command="/usr/bin/example",
            pre_action_timeout_seconds=3,
        )
        harness.config["schedules"] = [schedule_to_dict(item)]
        FakeProcess.instances = []
        FakeTimer.instances = []

        with (
            patch("amp_autopower.QProcess", FakeProcess),
            patch("amp_autopower.QTimer", FakeTimer),
            patch("amp_autopower.save_json"),
        ):
            harness.execute_action(item, target)
            process = FakeProcess.instances[0]
            FakeTimer.instances[0].timeout.emit()
            self.assertTrue(process.terminated)
            self.assertEqual(FakeTimer.instances[1].interval, 2000)
            FakeTimer.instances[1].timeout.emit()
            self.assertTrue(process.killed)
            process.process_state = FakeProcess.NotRunning
            process.finished.emit(-15, None)

        self.assertTrue(harness.schedules()[0].enabled)
        self.assertNotIn(item.id, state["completed_intervals"])
        self.assertIn(item.id, state["pending_occurrences"])
        self.assertIn("timeout de 3 segundos", harness.notifications[-1][0][1])

    def test_pre_action_timeout_can_continue_to_final_action(self):
        state = {
            "last_runs": {},
            "snoozes": {},
            "skipped_targets": {},
            "pending_occurrences": {},
            "completed_intervals": {},
        }
        harness = SchedulerHarness(state)
        target = datetime(2026, 8, 31, 12, 0)
        item = Schedule(
            id="timeout-continue",
            action="test",
            pre_action_enabled=True,
            pre_action_command="/usr/bin/example",
            pre_action_timeout_seconds=2,
            pre_action_failure_policy="continue",
        )
        FakeProcess.instances = []
        FakeTimer.instances = []

        with (
            patch("amp_autopower.QProcess", FakeProcess),
            patch("amp_autopower.QTimer", FakeTimer),
            patch("amp_autopower.save_json"),
        ):
            harness.execute_action(item, target)
            process = FakeProcess.instances[0]
            FakeTimer.instances[0].timeout.emit()
            process.process_state = FakeProcess.NotRunning
            process.finished.emit(-15, None)

        self.assertEqual(state["last_runs"][item.id], target.isoformat())
        self.assertTrue(
            any(
                "timeout de 2 segundos" in args[1]
                for args, _kwargs in harness.notifications
            )
        )

    def test_pre_action_failure_can_continue_to_final_action(self):
        state = {
            "last_runs": {},
            "snoozes": {},
            "skipped_targets": {},
            "pending_occurrences": {},
            "completed_intervals": {},
        }
        harness = SchedulerHarness(state)
        target = datetime(2026, 8, 31, 12, 0)
        item = Schedule(
            id="continue",
            action="test",
            pre_action_enabled=True,
            pre_action_command="",
            pre_action_failure_policy="continue",
        )

        with patch("amp_autopower.save_json"):
            harness.execute_action(item, target)

        self.assertEqual(state["last_runs"][item.id], target.isoformat())
        self.assertEqual(harness.notifications[-1][0][0], "Prueba completada")

    def test_detached_pre_action_exception_cancels_final_action(self):
        state = {
            "last_runs": {},
            "snoozes": {},
            "skipped_targets": {},
            "pending_occurrences": {},
            "completed_intervals": {},
        }
        harness = SchedulerHarness(state)
        target = datetime(2026, 8, 31, 12, 0)
        item = Schedule(
            id="detached-error",
            action="test",
            pre_action_enabled=True,
            pre_action_command="/usr/bin/example",
            pre_action_wait=False,
        )

        with (
            patch(
                "amp_autopower.QProcess.startDetached",
                side_effect=OSError("simulated start failure"),
            ),
            patch("amp_autopower.save_json"),
        ):
            harness.execute_action(item, target)

        self.assertNotIn(item.id, state["last_runs"])
        self.assertNotIn(item.id, state["skipped_targets"])
        self.assertIn("simulated start failure", harness.notifications[-1][0][1])

    def test_session_action_falls_back_to_gdbus_then_loginctl(self):
        state = {
            "last_runs": {},
            "snoozes": {},
            "skipped_targets": {},
            "pending_occurrences": {},
            "completed_intervals": {},
        }
        harness = SchedulerHarness(state)
        harness._find_qdbus = lambda: None

        with patch(
            "amp_autopower.shutil.which",
            side_effect=lambda name: (
                "/usr/bin/gdbus" if name == "gdbus" else None
            ),
        ):
            logout = harness._session_action_command("logout")
        self.assertEqual(logout[0:3], ["/usr/bin/gdbus", "call", "--session"])
        self.assertEqual(logout[-1], "org.kde.Shutdown.logout")

        with patch(
            "amp_autopower.shutil.which",
            side_effect=lambda name: (
                "/usr/bin/loginctl" if name == "loginctl" else None
            ),
        ):
            lock = harness._session_action_command("lock")
        self.assertEqual(lock, ["/usr/bin/loginctl", "lock-session"])

    def test_detached_pre_action_uses_argument_vector(self):
        state = {
            "last_runs": {},
            "snoozes": {},
            "skipped_targets": {},
            "pending_occurrences": {},
            "completed_intervals": {},
        }
        harness = SchedulerHarness(state)
        target = datetime(2026, 8, 31, 12, 0)
        item = Schedule(
            id="detached",
            action="test",
            pre_action_enabled=True,
            pre_action_command="/usr/bin/example --save now",
            pre_action_wait=False,
        )

        with (
            patch(
                "amp_autopower.QProcess.startDetached",
                return_value=(True, 1234),
            ) as start_detached,
            patch("amp_autopower.save_json"),
        ):
            harness.execute_action(item, target)

        start_detached.assert_called_once_with(
            "/usr/bin/example",
            ["--save", "now"],
        )
        self.assertEqual(state["last_runs"][item.id], target.isoformat())

    def test_logout_and_lock_use_session_dbus_without_closing_chrome(self):
        state = {
            "last_runs": {},
            "snoozes": {},
            "skipped_targets": {},
            "pending_occurrences": {},
            "completed_intervals": {},
        }
        success = SimpleNamespace(returncode=0, stderr="", stdout="")

        for action, method in (
            ("logout", "org.kde.Shutdown.logout"),
            ("lock", "org.freedesktop.ScreenSaver.Lock"),
        ):
            with self.subTest(action=action):
                harness = SchedulerHarness(state)
                harness._close_chrome_cleanly = MagicMock(return_value=True)
                item = Schedule(
                    id=action,
                    action=action,
                    close_apps_first=True,
                )
                with (
                    patch(
                        "amp_autopower.shutil.which",
                        side_effect=lambda name: (
                            "/usr/bin/qdbus6" if name == "qdbus6" else None
                        ),
                    ),
                    patch(
                        "amp_autopower.run_cmd",
                        return_value=success,
                    ) as command,
                    patch("amp_autopower.save_json"),
                ):
                    harness.execute_action(item, datetime(2026, 8, 31, 12, 0))

                self.assertEqual(command.call_args.args[0][-1], method)
                harness._close_chrome_cleanly.assert_not_called()

    def test_safe_power_actions_keep_chrome_then_plasma_shutdown(self):
        success = SimpleNamespace(returncode=0, stderr="", stdout="")

        for action, method in (
            ("poweroff", "org.kde.Shutdown.logoutAndShutdown"),
            ("reboot", "org.kde.Shutdown.logoutAndReboot"),
        ):
            with self.subTest(action=action):
                state = {
                    "last_runs": {},
                    "snoozes": {},
                    "skipped_targets": {},
                    "pending_occurrences": {},
                    "completed_intervals": {},
                }
                harness = SchedulerHarness(state)
                harness._close_chrome_cleanly = MagicMock(return_value=True)
                item = Schedule(
                    id=action,
                    action=action,
                    close_apps_first=True,
                )

                with (
                    patch(
                        "amp_autopower.shutil.which",
                        side_effect=lambda name: (
                            "/usr/bin/qdbus6" if name == "qdbus6" else None
                        ),
                    ),
                    patch(
                        "amp_autopower.run_cmd",
                        return_value=success,
                    ) as command,
                    patch("amp_autopower.save_json"),
                ):
                    harness.execute_action(item, datetime(2026, 8, 31, 12, 0))

                harness._close_chrome_cleanly.assert_called_once_with()
                self.assertEqual(command.call_args.args[0][-1], method)

    def test_failed_timed_action_does_not_consume_occurrence(self):
        state = {
            "last_runs": {},
            "snoozes": {},
            "skipped_targets": {},
            "pending_occurrences": {},
            "completed_intervals": {},
        }
        harness = SchedulerHarness(state)
        item = Schedule(
            id="timed-failure",
            action="suspend",
            close_apps_first=False,
        )
        target = datetime(2026, 8, 31, 12, 0)
        failure = SimpleNamespace(
            returncode=1,
            stderr="simulated failure",
            stdout="",
        )

        with (
            patch("amp_autopower.run_cmd", return_value=failure),
            patch("amp_autopower.save_json"),
        ):
            harness.execute_action(item, target)

        self.assertNotIn(item.id, state["last_runs"])

    def test_failed_timed_session_and_chrome_do_not_consume_occurrence(self):
        target = datetime(2026, 8, 31, 12, 0)

        for action, chrome_ok in (("logout", True), ("poweroff", False)):
            with self.subTest(action=action):
                state = {
                    "last_runs": {},
                    "snoozes": {},
                    "skipped_targets": {},
                    "pending_occurrences": {},
                    "completed_intervals": {},
                }
                harness = SchedulerHarness(state)
                harness._session_action_command = MagicMock(
                    return_value=["/usr/bin/qdbus6", "mock-method"]
                )
                harness._close_chrome_cleanly = MagicMock(
                    return_value=chrome_ok
                )
                item = Schedule(
                    id=f"timed-{action}-failure",
                    action=action,
                    close_apps_first=True,
                )

                with (
                    patch(
                        "amp_autopower.run_cmd",
                        return_value=SimpleNamespace(
                            returncode=1,
                            stderr="simulated DBus failure",
                            stdout="",
                        ),
                    ),
                    patch("amp_autopower.save_json"),
                ):
                    harness.execute_action(item, target)

                self.assertNotIn(item.id, state["last_runs"])

    def test_early_or_failure_retries_with_original_scheduled_target(self):
        target = datetime(2026, 8, 31, 23, 30)
        state = {
            "last_runs": {},
            "snoozes": {},
            "skipped_targets": {},
            "pending_occurrences": {},
            "completed_intervals": {},
        }
        harness = SchedulerHarness(state)
        item = Schedule(
            id="timed-or-retry",
            action="suspend",
            condition_logic="OR",
            require_cpu=True,
        )
        failure = SimpleNamespace(returncode=1, stderr="failure", stdout="")
        success = SimpleNamespace(returncode=0, stderr="", stdout="")

        with (
            patch("amp_autopower.run_cmd", side_effect=(failure, success)),
            patch("amp_autopower.save_json"),
        ):
            harness.execute_action(item, target)
            self.assertNotIn(item.id, state["last_runs"])
            harness.execute_action(item, target)

        self.assertEqual(state["last_runs"][item.id], target.isoformat())

    def test_chrome_clean_close_sends_sighup_to_main_process(self):
        state = {
            "last_runs": {},
            "snoozes": {},
            "skipped_targets": {},
            "pending_occurrences": {},
            "completed_intervals": {},
        }
        harness = SchedulerHarness(state)
        harness._chrome_main_processes = lambda: [(1234, ["chrome"])]

        with (
            patch("amp_autopower.os.kill") as kill,
            patch("amp_autopower.Path.exists", return_value=False),
            patch("amp_autopower.time.sleep"),
        ):
            self.assertTrue(harness._close_chrome_cleanly())

        kill.assert_called_once_with(1234, signal.SIGHUP)

    def test_cancel_command_disabled_does_nothing(self):
        state = {
            "last_runs": {},
            "snoozes": {},
            "skipped_targets": {},
            "pending_occurrences": {},
            "completed_intervals": {},
        }
        harness = SchedulerHarness(state)
        target = datetime(2026, 8, 31, 12, 0)
        item = Schedule(
            id="cancel-disabled",
            action="test",
            cancel_action_behavior="none",
            cancel_action_command="/usr/bin/example",
        )
        dialog = SimpleNamespace(
            result_action="cancel",
            cancelled_by_user=True,
            cancel_command_executed=False,
        )

        with (
            patch("amp_autopower.QProcess.startDetached") as command,
            patch("amp_autopower.save_json"),
        ):
            harness.on_countdown_finished("cancel", dialog, item, target)

        command.assert_not_called()

    def test_explicit_cancel_runs_command_once_with_safe_arguments(self):
        state = {
            "last_runs": {},
            "snoozes": {},
            "skipped_targets": {},
            "pending_occurrences": {},
            "completed_intervals": {},
        }
        harness = SchedulerHarness(state)
        target = datetime(2026, 8, 31, 12, 0)
        item = Schedule(
            id="cancel-command",
            action="test",
            cancel_action_behavior="command",
            cancel_action_command='"/tmp/Cancel Tool" --undo "two words"',
        )
        dialog = SimpleNamespace(
            result_action="cancel",
            cancelled_by_user=True,
            cancel_command_executed=False,
        )

        with (
            patch(
                "amp_autopower.QProcess.startDetached",
                return_value=(True, 1234),
            ) as command,
            patch("amp_autopower.save_json"),
        ):
            harness.on_countdown_finished("cancel", dialog, item, target)
            harness.on_countdown_finished("cancel", dialog, item, target)

        command.assert_called_once_with(
            "/tmp/Cancel Tool",
            ["--undo", "two words"],
        )

    def test_snooze_does_not_run_cancel_command(self):
        state = {
            "last_runs": {},
            "snoozes": {},
            "skipped_targets": {},
            "pending_occurrences": {},
            "completed_intervals": {},
        }
        harness = SchedulerHarness(state)
        target = datetime(2026, 8, 31, 12, 0)
        item = Schedule(
            id="snooze",
            action="test",
            cancel_action_behavior="command",
            cancel_action_command="/usr/bin/example",
        )
        dialog = SimpleNamespace(
            result_action="snooze10",
            cancelled_by_user=False,
            cancel_command_executed=False,
        )

        with (
            patch("amp_autopower.QProcess.startDetached") as command,
            patch("amp_autopower.save_json"),
        ):
            harness.on_countdown_finished("snooze", dialog, item, target)

        command.assert_not_called()

    def test_internal_cancel_does_not_run_cancel_command(self):
        state = {
            "last_runs": {},
            "snoozes": {},
            "skipped_targets": {},
            "pending_occurrences": {},
            "completed_intervals": {},
        }
        harness = SchedulerHarness(state)
        target = datetime(2026, 8, 31, 12, 0)
        item = Schedule(
            id="internal-cancel",
            action="test",
            cancel_action_behavior="command",
            cancel_action_command="/usr/bin/example",
        )
        dialog = SimpleNamespace(
            result_action="cancel",
            cancelled_by_user=False,
            cancel_command_executed=False,
        )

        with (
            patch("amp_autopower.QProcess.startDetached") as command,
            patch("amp_autopower.save_json"),
        ):
            harness.on_countdown_finished("internal", dialog, item, target)

        command.assert_not_called()

    def test_cancel_command_failure_does_not_escape(self):
        state = {
            "last_runs": {},
            "snoozes": {},
            "skipped_targets": {},
            "pending_occurrences": {},
            "completed_intervals": {},
        }
        harness = SchedulerHarness(state)
        item = Schedule(
            cancel_action_behavior="command",
            cancel_action_command="/does/not/exist",
        )

        with patch(
            "amp_autopower.QProcess.startDetached",
            side_effect=OSError("simulated failure"),
        ):
            self.assertFalse(harness._run_cancel_action_command(item))

        self.assertIn("simulated failure", harness.notifications[-1][0][1])

    def test_pre_action_failure_does_not_run_cancel_command(self):
        state = {
            "last_runs": {},
            "snoozes": {},
            "skipped_targets": {},
            "pending_occurrences": {},
            "completed_intervals": {},
        }
        harness = SchedulerHarness(state)
        harness._run_cancel_action_command = MagicMock()
        item = Schedule(
            id="pre-failure",
            cancel_action_behavior="command",
            cancel_action_command="/usr/bin/example",
        )

        with patch("amp_autopower.save_json"):
            harness._handle_pre_action_failure(
                item,
                datetime(2026, 8, 31, 12, 0),
                "simulated pre-action failure",
            )

        harness._run_cancel_action_command.assert_not_called()

    def test_disabling_or_deleting_schedule_does_not_run_cancel_command(self):
        target = datetime(2026, 8, 31, 12, 0)

        for replacement in (
            [Schedule(id="scheduled", enabled=False)],
            [],
        ):
            with self.subTest(deleted=not replacement):
                state = {
                    "last_runs": {},
                    "snoozes": {},
                    "skipped_targets": {},
                    "pending_occurrences": {},
                    "completed_intervals": {},
                }
                harness = SchedulerHarness(state)
                item = Schedule(
                    id="scheduled",
                    action="test",
                    cancel_action_behavior="command",
                    cancel_action_command="/usr/bin/example",
                )
                harness.config["schedules"] = [schedule_to_dict(item)]

                class InternalDialog:
                    schedule = item

                    def finish(dialog_self, action):
                        dialog_self.result_action = action
                        dialog_self.cancelled_by_user = False
                        dialog_self.cancel_command_executed = False
                        harness.on_countdown_finished(
                            "active",
                            dialog_self,
                            item,
                            target,
                        )

                harness.active_dialogs["active"] = InternalDialog()
                with (
                    patch("amp_autopower.QProcess.startDetached") as command,
                    patch("amp_autopower.save_json"),
                ):
                    harness.set_schedules(replacement, now=target)

                command.assert_not_called()

    def test_timed_or_cpu_starts_countdown_before_time_window(self):
        now = datetime(2026, 8, 31, 23, 20)
        target = datetime(2026, 8, 31, 23, 30)
        state = {
            "last_runs": {},
            "snoozes": {},
            "skipped_targets": {},
            "pending_occurrences": {},
        }
        item = Schedule(
            id="timed-or",
            time="23:30",
            weekdays=[0],
            warning_minutes=[],
            final_countdown_seconds=60,
            condition_logic="OR",
            require_cpu=True,
            cpu_duration_seconds=0,
        )
        harness = SchedulerHarness(
            state,
            cpu_reading=CPUReading(
                5.0,
                None,
                True,
                "cpu_sample_available",
            ),
        )

        harness._timed_schedule_tick(item, now)

        self.assertEqual(
            harness.started,
            [(item.id, target, 60, f"{target.isoformat()}:{item.id}")],
        )

    def test_idle_or_network_does_not_require_idle_monitor(self):
        now = datetime(2026, 8, 31, 12, 0)
        state = {
            "last_runs": {},
            "snoozes": {},
            "skipped_targets": {},
            "pending_occurrences": {},
        }
        item = Schedule(
            id="idle-or",
            use_time=False,
            trigger_mode="idle",
            weekdays=[0],
            condition_logic="OR",
            require_idle=True,
            require_network=True,
            network_interface="enp1s0",
            network_duration_seconds=0,
        )
        harness = SchedulerHarness(
            state,
            reliable=False,
            network_reading=NetworkReading(
                20 * 1024,
                10 * 1024,
                30 * 1024,
                None,
                True,
                "network_sample_available",
            ),
        )

        harness._idle_only_tick(item, now)

        self.assertEqual(len(harness.started), 1)
        self.assertEqual(harness.started[0][1], now)

    def test_interval_or_early_execution_remains_one_shot(self):
        start = datetime(2026, 8, 31, 22, 30)
        target = start + timedelta(minutes=60)
        occurrence = ScheduledOccurrence.create_interval(
            "interval-or",
            start,
            60,
            60,
        )
        state = {
            "last_runs": {},
            "snoozes": {},
            "skipped_targets": {},
            "pending_occurrences": {
                occurrence.schedule_id: occurrence.to_state(),
            },
            "completed_intervals": {},
        }
        item = Schedule(
            id=occurrence.schedule_id,
            use_time=False,
            trigger_mode="interval",
            interval_minutes=60,
            weekdays=[],
            action="test",
            final_countdown_seconds=60,
            condition_logic="OR",
            require_cpu=True,
            cpu_duration_seconds=0,
        )
        harness = SchedulerHarness(
            state,
            cpu_reading=CPUReading(
                5.0,
                None,
                True,
                "cpu_sample_available",
            ),
        )
        harness.config["schedules"] = [schedule_to_dict(item)]
        harness.config["condition_logic_settings"] = {item.id: "OR"}

        with patch("amp_autopower.save_json"):
            harness._pending_occurrence_tick(
                item,
                occurrence,
                target - timedelta(minutes=10),
            )
            harness.execute_action(item, target)
            harness._pending_occurrence_tick(item, occurrence, target)

        self.assertEqual(len(harness.started), 1)
        self.assertEqual(harness.started[0][1], target)
        self.assertEqual(state["last_runs"][item.id], target.isoformat())
        self.assertFalse(harness.config["schedules"][0]["enabled"])

    def test_snooze_preserves_or_logic_and_original_target(self):
        target = datetime(2026, 8, 31, 23, 30)
        occurrence = ScheduledOccurrence.create("timed-or", target, 60)
        state = {
            "last_runs": {},
            "snoozes": {},
            "skipped_targets": {},
            "pending_occurrences": {
                occurrence.schedule_id: occurrence.to_state(),
            },
        }
        item = Schedule(
            id=occurrence.schedule_id,
            condition_logic="OR",
            require_cpu=True,
        )
        harness = SchedulerHarness(state)
        harness.config["schedules"] = [schedule_to_dict(item)]
        harness.config["condition_logic_settings"] = {item.id: "OR"}

        with patch("amp_autopower.save_json"):
            harness.snooze(item, 10, target)

        persisted = harness.pending_occurrence(item)
        self.assertEqual(persisted.scheduled_target, target)
        self.assertEqual(harness.schedules()[0].condition_logic, "OR")


if __name__ == "__main__":
    unittest.main()
