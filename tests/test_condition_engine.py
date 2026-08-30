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


def interval_schedule(**overrides):
    return schedule(
        use_time=False,
        trigger_mode="interval",
        interval_minutes=30,
        weekdays=[],
        **overrides,
    )


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

    def test_interval_target_is_start_plus_duration(self):
        start_at = datetime(2026, 8, 31, 10, 0)
        item = ScheduledOccurrence.create_interval(
            "schedule-1",
            start_at,
            30,
            60,
        )

        self.assertEqual(item.start_at, start_at)
        self.assertEqual(item.duration_seconds, 30 * 60)
        self.assertEqual(
            item.scheduled_target,
            datetime(2026, 8, 31, 10, 30),
        )

    def test_interval_countdown_precedes_condition_satisfaction(self):
        start_at = datetime(2026, 8, 31, 10, 0)
        item = ScheduledOccurrence.create_interval(
            "schedule-1",
            start_at,
            30,
            60,
        )
        result = self.engine.evaluate(
            interval_schedule(),
            ConditionContext(
                now=datetime(2026, 8, 31, 10, 29),
                occurrence=item,
                occurrence_pending=True,
            ),
        )

        self.assertTrue(result.countdown_due)
        self.assertTrue(result.ready_for_countdown)
        self.assertFalse(result.trigger_reached)
        self.assertFalse(result.for_type("interval").satisfied)
        self.assertEqual(
            result.countdown_start,
            datetime(2026, 8, 31, 10, 29),
        )

    def test_time_condition_rejects_interval_occurrence(self):
        item = ScheduledOccurrence.create_interval(
            "schedule-1",
            datetime(2026, 8, 31, 10, 0),
            30,
            60,
        )
        result = self.engine.evaluate(
            schedule(),
            ConditionContext(
                now=datetime(2026, 8, 31, 10, 30),
                occurrence=item,
                occurrence_pending=True,
            ),
        )

        self.assertFalse(result.for_type("time").satisfied)
        self.assertFalse(result.trigger_reached)

    def test_interval_occurrence_round_trip_preserves_start_and_target(self):
        start_at = datetime(2026, 8, 31, 10, 0)
        original = ScheduledOccurrence.create_interval(
            "schedule-1",
            start_at,
            30,
            60,
        )

        restored = ScheduledOccurrence.from_state(
            original.schedule_id,
            original.to_state(),
        )

        self.assertEqual(restored, original)
        self.assertEqual(restored.start_at, start_at)
        self.assertEqual(
            restored.scheduled_target,
            datetime(2026, 8, 31, 10, 30),
        )

    def test_interval_and_idle_can_start_countdown_before_target(self):
        start_at = datetime(2026, 8, 31, 10, 0)
        item = ScheduledOccurrence.create_interval(
            "schedule-1",
            start_at,
            30,
            60,
        )
        result = self.engine.evaluate(
            interval_schedule(require_idle=True),
            ConditionContext(
                now=datetime(2026, 8, 31, 10, 29),
                occurrence=item,
                occurrence_pending=True,
                idle_seconds=30 * 60,
                idle_reliable=True,
            ),
        )

        self.assertTrue(result.ready_for_countdown)
        self.assertFalse(result.for_type("interval").satisfied)
        self.assertTrue(result.for_type("idle").satisfied)

    def test_interval_and_idle_pending_keeps_original_target(self):
        start_at = datetime(2026, 8, 31, 10, 0)
        target = datetime(2026, 8, 31, 10, 30)
        item = ScheduledOccurrence.create_interval(
            "schedule-1",
            start_at,
            30,
            60,
        ).mark_armed()
        waiting = self.engine.evaluate(
            interval_schedule(require_idle=True),
            ConditionContext(
                now=target,
                occurrence=item,
                occurrence_pending=True,
                idle_seconds=10 * 60,
                idle_reliable=True,
            ),
        )
        ready = self.engine.evaluate(
            interval_schedule(require_idle=True),
            ConditionContext(
                now=target + timedelta(minutes=20),
                occurrence=item,
                occurrence_pending=True,
                idle_seconds=30 * 60,
                idle_reliable=True,
            ),
        )

        self.assertTrue(waiting.pending)
        self.assertFalse(waiting.ready_for_countdown)
        self.assertEqual(waiting.scheduled_target, target)
        self.assertTrue(ready.ready_for_countdown)
        self.assertEqual(ready.scheduled_target, target)

    def test_interval_pending_can_cross_midnight(self):
        start_at = datetime(2026, 9, 4, 23, 0)
        target = datetime(2026, 9, 4, 23, 30)
        ready_at = datetime(2026, 9, 5, 0, 10)
        item = ScheduledOccurrence.create_interval(
            "schedule-1",
            start_at,
            30,
            60,
        ).mark_armed().with_next_check(ready_at)
        result = self.engine.evaluate(
            interval_schedule(require_idle=True),
            ConditionContext(
                now=ready_at,
                occurrence=item,
                occurrence_pending=True,
                idle_seconds=30 * 60,
                idle_reliable=True,
            ),
        )

        self.assertTrue(result.ready_for_countdown)
        self.assertEqual(result.scheduled_target, target)
        self.assertEqual(result.for_type("interval").info["start_at"], start_at)

    def test_cpu_less_than_threshold(self):
        result = self.engine.evaluate(
            schedule(
                require_cpu=True,
                cpu_comparison="less",
                cpu_threshold=10,
                cpu_duration_seconds=0,
            ),
            context(
                MONDAY_TARGET - timedelta(seconds=60),
                cpu_usage=9.0,
                cpu_reliable=True,
                cpu_status="cpu_sample_available",
            ),
        )

        self.assertTrue(result.for_type("cpu").satisfied)
        self.assertEqual(result.for_type("cpu").info["value_percent"], 9.0)

    def test_cpu_greater_than_threshold(self):
        result = self.engine.evaluate(
            schedule(
                require_cpu=True,
                cpu_comparison="greater",
                cpu_threshold=80,
                cpu_duration_seconds=0,
            ),
            context(
                MONDAY_TARGET - timedelta(seconds=60),
                cpu_usage=81.0,
                cpu_reliable=True,
                cpu_status="cpu_sample_available",
            ),
        )

        self.assertTrue(result.for_type("cpu").satisfied)

    def test_cpu_continuous_duration_and_reset(self):
        item = schedule(
            require_cpu=True,
            cpu_comparison="less",
            cpu_threshold=10,
            cpu_duration_seconds=30,
        )
        started = MONDAY_TARGET - timedelta(minutes=2)

        samples = []
        for seconds in range(31):
            samples.append(self.engine.evaluate(
                item,
                context(
                    started + timedelta(seconds=seconds),
                    cpu_usage=5.0,
                    cpu_reliable=True,
                    cpu_status="cpu_sample_available",
                ),
            ))
        first = samples[0]
        almost = samples[29]
        reached = samples[30]
        broken = self.engine.evaluate(
            item,
            context(
                started + timedelta(seconds=31),
                cpu_usage=50.0,
                cpu_reliable=True,
                cpu_status="cpu_sample_available",
            ),
        )
        restarted = self.engine.evaluate(
            item,
            context(
                started + timedelta(seconds=32),
                cpu_usage=5.0,
                cpu_reliable=True,
                cpu_status="cpu_sample_available",
            ),
        )

        self.assertFalse(first.for_type("cpu").satisfied)
        self.assertFalse(almost.for_type("cpu").satisfied)
        self.assertTrue(reached.for_type("cpu").satisfied)
        self.assertEqual(reached.for_type("cpu").satisfied_for_seconds, 30)
        self.assertFalse(broken.for_type("cpu").satisfied)
        self.assertFalse(restarted.for_type("cpu").satisfied)
        self.assertEqual(restarted.for_type("cpu").satisfied_for_seconds, 0)

    def test_cpu_duration_resets_after_evaluation_gap(self):
        item = schedule(
            require_cpu=True,
            cpu_threshold=10,
            cpu_duration_seconds=30,
        )
        started = MONDAY_TARGET - timedelta(minutes=2)
        first = self.engine.evaluate(
            item,
            context(
                started,
                cpu_usage=5.0,
                cpu_reliable=True,
                cpu_status="cpu_sample_available",
            ),
        )
        after_gap = self.engine.evaluate(
            item,
            context(
                started + timedelta(minutes=1),
                cpu_usage=5.0,
                cpu_reliable=True,
                cpu_status="cpu_sample_available",
            ),
        )

        self.assertFalse(first.for_type("cpu").satisfied)
        self.assertFalse(after_gap.for_type("cpu").satisfied)
        self.assertEqual(after_gap.for_type("cpu").satisfied_for_seconds, 0)

    def test_cpu_average_is_used_when_enabled(self):
        result = self.engine.evaluate(
            schedule(
                require_cpu=True,
                cpu_comparison="less",
                cpu_threshold=25,
                cpu_duration_seconds=0,
                cpu_use_average=True,
            ),
            context(
                MONDAY_TARGET - timedelta(seconds=60),
                cpu_usage=90.0,
                cpu_average=20.0,
                cpu_reliable=True,
                cpu_status="cpu_average_available",
            ),
        )

        self.assertTrue(result.for_type("cpu").satisfied)
        self.assertEqual(result.for_type("cpu").info["value_percent"], 20.0)

    def test_cpu_monitor_error_is_not_satisfied(self):
        result = self.engine.evaluate(
            schedule(require_cpu=True, cpu_duration_seconds=0),
            context(
                MONDAY_TARGET - timedelta(seconds=60),
                cpu_usage=None,
                cpu_reliable=False,
                cpu_status="cpu_monitor_error: simulated",
            ),
        )

        self.assertFalse(result.for_type("cpu").satisfied)
        self.assertEqual(
            result.for_type("cpu").reason,
            "cpu_monitor_error: simulated",
        )
        self.assertFalse(result.ready_for_countdown)

    def test_cpu_runtime_starts_empty_after_restart(self):
        item = schedule(
            require_cpu=True,
            cpu_threshold=10,
            cpu_duration_seconds=30,
        )
        restarted_engine = ConditionEngine()
        result = restarted_engine.evaluate(
            item,
            context(
                MONDAY_TARGET,
                cpu_usage=5.0,
                cpu_reliable=True,
                cpu_status="cpu_sample_available",
            ),
        )

        self.assertFalse(result.for_type("cpu").satisfied)
        self.assertEqual(result.for_type("cpu").satisfied_for_seconds, 0)

    def test_cpu_and_idle_use_and_semantics(self):
        item = schedule(
            require_idle=True,
            idle_minutes=30,
            require_cpu=True,
            cpu_threshold=10,
            cpu_duration_seconds=0,
        )
        now = MONDAY_TARGET - timedelta(seconds=60)
        waiting = self.engine.evaluate(
            item,
            context(
                now,
                idle_seconds=10 * 60,
                idle_reliable=True,
                cpu_usage=5.0,
                cpu_reliable=True,
                cpu_status="cpu_sample_available",
            ),
        )
        ready = self.engine.evaluate(
            item,
            context(
                now,
                idle_seconds=30 * 60,
                idle_reliable=True,
                cpu_usage=5.0,
                cpu_reliable=True,
                cpu_status="cpu_sample_available",
            ),
        )

        self.assertFalse(waiting.ready_for_countdown)
        self.assertTrue(waiting.for_type("cpu").satisfied)
        self.assertFalse(waiting.for_type("idle").satisfied)
        self.assertTrue(ready.ready_for_countdown)

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
