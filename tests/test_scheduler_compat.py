import unittest
from dataclasses import asdict
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from amp_autopower import MainWindow, Schedule
from condition_engine import ConditionEngine, ScheduledOccurrence


class SchedulerHarness:
    pending_occurrence = MainWindow.pending_occurrence
    save_pending_occurrence = MainWindow.save_pending_occurrence
    pending_action_time = MainWindow.pending_action_time
    _prune_pending_occurrences = MainWindow._prune_pending_occurrences
    evaluate_conditions = MainWindow.evaluate_conditions
    defer_for_idle = MainWindow.defer_for_idle
    _has_active_dialog_for_schedule = MainWindow._has_active_dialog_for_schedule
    _pending_occurrence_tick = MainWindow._pending_occurrence_tick

    def __init__(self, state, idle_seconds=0, reliable=True):
        self.state = state
        self.condition_engine = ConditionEngine()
        self.active_dialogs = {}
        self.config = {"overlay_all_schedule_warnings": False}
        self._idle_seconds = idle_seconds
        self._reliable = reliable
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

        self.assertEqual(asdict(loaded), raw)

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
