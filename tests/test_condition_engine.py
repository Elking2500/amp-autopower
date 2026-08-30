import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

from condition_engine import (
    ConditionContext,
    ConditionEngine,
    ScheduledOccurrence,
)


MONDAY_TARGET = datetime(2026, 8, 31, 23, 30)


def schedule(**overrides):
    values = {
        "id": "schedule-1",
        "use_time": True,
        "require_idle": False,
        "idle_minutes": 30,
        "final_countdown_seconds": 60,
        "weekdays": [0],
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def occurrence(target=MONDAY_TARGET, **overrides):
    item = ScheduledOccurrence.create(
        "schedule-1",
        target,
        60,
        created_at=target - timedelta(minutes=1),
    )
    values = item.__dict__.copy()
    values.update(overrides)
    return ScheduledOccurrence(**values)


def context(now, item=None, **overrides):
    values = {
        "now": now,
        "occurrence": item or occurrence(),
        "occurrence_pending": False,
        "idle_seconds": 0,
        "idle_reliable": False,
    }
    values.update(overrides)
    return ConditionContext(**values)


class ConditionEngineTests(unittest.TestCase):
    def setUp(self):
        self.engine = ConditionEngine()

    def test_countdown_can_start_before_time_condition_is_satisfied(self):
        result = self.engine.evaluate(
            schedule(),
            context(MONDAY_TARGET - timedelta(seconds=60)),
        )

        self.assertTrue(result.countdown_due)
        self.assertTrue(result.ready_for_countdown)
        self.assertFalse(result.trigger_reached)
        self.assertFalse(result.for_type("time").satisfied)
        self.assertIsNone(result.for_type("time").satisfied_since)
        self.assertEqual(result.scheduled_target, MONDAY_TARGET)
        self.assertEqual(
            result.countdown_start,
            MONDAY_TARGET - timedelta(seconds=60),
        )

    def test_time_condition_is_satisfied_at_scheduled_target(self):
        before = self.engine.evaluate(
            schedule(),
            context(MONDAY_TARGET - timedelta(microseconds=1)),
        )
        reached = self.engine.evaluate(
            schedule(),
            context(MONDAY_TARGET),
        )

        self.assertFalse(before.for_type("time").satisfied)
        self.assertTrue(reached.for_type("time").satisfied)
        self.assertEqual(
            reached.for_type("time").satisfied_since,
            MONDAY_TARGET,
        )

    def test_pending_time_and_idle_keeps_original_scheduled_target(self):
        item = occurrence()
        waiting = self.engine.evaluate(
            schedule(require_idle=True),
            context(
                MONDAY_TARGET,
                item=item,
                occurrence_pending=True,
                idle_seconds=10 * 60,
                idle_reliable=True,
            ),
        )

        self.assertTrue(waiting.trigger_reached)
        self.assertTrue(waiting.armed)
        self.assertTrue(waiting.pending)
        self.assertFalse(waiting.ready_for_countdown)
        self.assertEqual(waiting.scheduled_target, MONDAY_TARGET)

        persisted = item.mark_armed().with_next_check(
            MONDAY_TARGET + timedelta(minutes=20)
        )
        restored = ScheduledOccurrence.from_state(
            "schedule-1",
            persisted.to_state(),
        )
        ready = self.engine.evaluate(
            schedule(require_idle=True),
            context(
                MONDAY_TARGET + timedelta(minutes=20),
                item=restored,
                occurrence_pending=True,
                idle_seconds=30 * 60,
                idle_reliable=True,
            ),
        )

        self.assertTrue(ready.ready_for_countdown)
        self.assertTrue(ready.armed)
        self.assertFalse(ready.pending)
        self.assertEqual(ready.scheduled_target, MONDAY_TARGET)
        self.assertNotEqual(
            ready.scheduled_target,
            MONDAY_TARGET + timedelta(minutes=20),
        )

    def test_pending_friday_occurrence_remains_friday_after_midnight(self):
        friday_target = datetime(2026, 9, 4, 23, 30)
        saturday_ready = datetime(2026, 9, 5, 0, 10)
        item = occurrence(
            friday_target,
            armed=True,
            next_check_at=saturday_ready,
        )
        restored = ScheduledOccurrence.from_state(
            "schedule-1",
            item.to_state(),
        )
        result = self.engine.evaluate(
            schedule(require_idle=True, weekdays=[4]),
            context(
                saturday_ready,
                item=restored,
                occurrence_pending=True,
                idle_seconds=30 * 60,
                idle_reliable=True,
            ),
        )

        self.assertTrue(result.weekday_allowed)
        self.assertTrue(result.ready_for_countdown)
        self.assertEqual(result.scheduled_target, friday_target)
        self.assertEqual(result.scheduled_target.weekday(), 4)
        self.assertNotEqual(result.scheduled_target, saturday_ready)

    def test_pending_occurrence_remains_qualified_after_weekday_edit(self):
        item = occurrence(armed=True)
        result = self.engine.evaluate(
            schedule(require_idle=True, weekdays=[1]),
            context(
                MONDAY_TARGET + timedelta(minutes=20),
                item=item,
                occurrence_pending=True,
                idle_seconds=30 * 60,
                idle_reliable=True,
            ),
        )

        self.assertTrue(result.weekday_allowed)
        self.assertTrue(result.ready_for_countdown)
        self.assertEqual(result.scheduled_target, MONDAY_TARGET)

    def test_pending_occurrence_round_trip_preserves_all_timing_fields(self):
        next_check = MONDAY_TARGET + timedelta(minutes=20)
        original = occurrence(armed=True, next_check_at=next_check)

        restored = ScheduledOccurrence.from_state(
            original.schedule_id,
            original.to_state(),
        )

        self.assertEqual(restored, original)

    def test_idle_only_ignores_time_and_requires_reliable_monitor(self):
        item = schedule(use_time=False, require_idle=False, idle_minutes=1)
        unavailable = self.engine.evaluate(
            item,
            ConditionContext(
                now=MONDAY_TARGET,
                idle_seconds=120,
            ),
        )
        ready = self.engine.evaluate(
            item,
            ConditionContext(
                now=MONDAY_TARGET,
                idle_seconds=60,
                idle_reliable=True,
            ),
        )

        self.assertFalse(unavailable.ready_for_countdown)
        self.assertEqual(
            unavailable.for_type("idle").reason,
            "idle_monitor_unavailable",
        )
        self.assertTrue(ready.ready_for_countdown)
        self.assertFalse(ready.for_type("time").enabled)
        self.assertTrue(ready.for_type("idle").enabled)

    def test_weekday_blocks_idle_only_on_unselected_day(self):
        tuesday = MONDAY_TARGET + timedelta(days=1)
        result = self.engine.evaluate(
            schedule(use_time=False, require_idle=True),
            ConditionContext(
                now=tuesday,
                idle_seconds=30 * 60,
                idle_reliable=True,
            ),
        )

        self.assertFalse(result.weekday_allowed)
        self.assertFalse(result.ready_for_countdown)

    def test_idle_cycle_rearms_only_after_new_activity(self):
        runtime = self.engine.runtime

        self.assertFalse(runtime.idle_cycle_was_triggered("schedule-1"))
        runtime.mark_idle_cycle_triggered("schedule-1")
        self.assertTrue(runtime.idle_cycle_was_triggered("schedule-1"))

        runtime.record_activity()
        self.assertFalse(runtime.idle_cycle_was_triggered("schedule-1"))

    def test_idle_snooze_is_invalidated_by_new_activity(self):
        runtime = self.engine.runtime
        runtime.mark_idle_snoozed("schedule-1")

        self.assertFalse(runtime.idle_snooze_was_invalidated("schedule-1"))
        runtime.record_activity()
        self.assertTrue(runtime.idle_snooze_was_invalidated("schedule-1"))


if __name__ == "__main__":
    unittest.main()
