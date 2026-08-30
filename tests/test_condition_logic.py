import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

from condition_engine import (
    ConditionContext,
    ConditionEngine,
    ConditionResult,
    ScheduledOccurrence,
    combine_condition_results,
)


TARGET = datetime(2026, 8, 31, 23, 30)


def schedule(**overrides):
    values = {
        "id": "logic",
        "use_time": True,
        "trigger_mode": "time",
        "weekdays": [0],
        "final_countdown_seconds": 60,
        "condition_logic": "AND",
        "require_idle": False,
        "idle_minutes": 30,
        "require_cpu": False,
        "cpu_threshold": 10,
        "cpu_duration_seconds": 0,
        "cpu_use_average": False,
        "require_network": False,
        "network_interface": "enp1s0",
        "network_direction": "both",
        "network_comparison": "less",
        "network_threshold": 50,
        "network_unit": "KB/s",
        "network_duration_seconds": 0,
        "network_use_average": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def timed_occurrence():
    return ScheduledOccurrence.create("logic", TARGET, 60)


def interval_occurrence():
    return ScheduledOccurrence.create_interval(
        "logic",
        TARGET - timedelta(minutes=30),
        30,
        60,
    )


def context(now, occurrence=None, **overrides):
    values = {
        "now": now,
        "occurrence": occurrence,
        "occurrence_pending": False,
        "idle_seconds": 0,
        "idle_reliable": False,
        "cpu_usage": None,
        "cpu_reliable": False,
        "cpu_status": "cpu_monitor_unavailable",
        "network_speed_bytes_per_second": None,
        "network_reliable": False,
        "network_status": "network_monitor_unavailable",
    }
    values.update(overrides)
    return ConditionContext(**values)


class ConditionLogicTests(unittest.TestCase):
    def setUp(self):
        self.engine = ConditionEngine()

    def test_and_cpu_network_requires_both(self):
        item = schedule(require_cpu=True, require_network=True)
        now = TARGET - timedelta(seconds=60)
        waiting = self.engine.evaluate(
            item,
            context(
                now,
                timed_occurrence(),
                cpu_usage=5,
                cpu_reliable=True,
                network_speed_bytes_per_second=60 * 1024,
                network_reliable=True,
            ),
        )
        ready = self.engine.evaluate(
            item,
            context(
                now,
                timed_occurrence(),
                cpu_usage=5,
                cpu_reliable=True,
                network_speed_bytes_per_second=20 * 1024,
                network_reliable=True,
            ),
        )

        self.assertFalse(waiting.ready_for_countdown)
        self.assertTrue(ready.ready_for_countdown)

    def test_or_cpu_network_accepts_either(self):
        item = schedule(
            condition_logic="OR",
            require_cpu=True,
            require_network=True,
        )
        result = self.engine.evaluate(
            item,
            context(
                TARGET - timedelta(minutes=10),
                timed_occurrence(),
                cpu_usage=5,
                cpu_reliable=True,
                network_speed_bytes_per_second=60 * 1024,
                network_reliable=True,
            ),
        )

        self.assertTrue(result.ready_for_countdown)
        self.assertTrue(result.for_type("cpu").satisfied)
        self.assertFalse(result.for_type("network").satisfied)

    def test_time_and_cpu_requires_cpu(self):
        item = schedule(require_cpu=True)
        result = self.engine.evaluate(
            item,
            context(
                TARGET,
                timed_occurrence(),
                cpu_usage=50,
                cpu_reliable=True,
            ),
        )

        self.assertTrue(result.for_type("time").satisfied)
        self.assertFalse(result.ready_for_countdown)

    def test_time_or_cpu_can_trigger_before_target(self):
        item = schedule(condition_logic="OR", require_cpu=True)
        result = self.engine.evaluate(
            item,
            context(
                TARGET - timedelta(minutes=10),
                timed_occurrence(),
                cpu_usage=5,
                cpu_reliable=True,
            ),
        )

        self.assertTrue(result.ready_for_countdown)
        self.assertFalse(result.countdown_due)
        self.assertFalse(result.for_type("time").satisfied)
        self.assertEqual(result.scheduled_target, TARGET)

    def test_time_or_cpu_is_ready_after_target_via_time(self):
        item = schedule(condition_logic="OR", require_cpu=True)
        result = self.engine.evaluate(
            item,
            context(
                TARGET,
                timed_occurrence(),
                cpu_usage=50,
                cpu_reliable=True,
            ),
        )

        self.assertTrue(result.for_type("time").satisfied)
        self.assertFalse(result.for_type("cpu").satisfied)
        self.assertTrue(result.ready_for_countdown)

    def test_interval_and_cpu_requires_cpu(self):
        item = schedule(
            use_time=False,
            trigger_mode="interval",
            weekdays=[],
            require_cpu=True,
        )
        result = self.engine.evaluate(
            item,
            context(
                TARGET - timedelta(seconds=60),
                interval_occurrence(),
                cpu_usage=50,
                cpu_reliable=True,
            ),
        )

        self.assertTrue(result.countdown_due)
        self.assertFalse(result.ready_for_countdown)

    def test_interval_or_cpu_can_trigger_before_target(self):
        item = schedule(
            use_time=False,
            trigger_mode="interval",
            weekdays=[],
            condition_logic="OR",
            require_cpu=True,
        )
        occurrence = interval_occurrence()
        result = self.engine.evaluate(
            item,
            context(
                TARGET - timedelta(minutes=10),
                occurrence,
                cpu_usage=5,
                cpu_reliable=True,
            ),
        )

        self.assertTrue(result.ready_for_countdown)
        self.assertFalse(result.countdown_due)
        self.assertFalse(result.for_type("interval").satisfied)
        self.assertEqual(result.scheduled_target, TARGET)
        self.assertEqual(occurrence.start_at, TARGET - timedelta(minutes=30))

    def test_idle_and_network_requires_both(self):
        item = schedule(
            use_time=False,
            trigger_mode="idle",
            require_idle=True,
            require_network=True,
        )
        result = self.engine.evaluate(
            item,
            context(
                TARGET,
                idle_seconds=10 * 60,
                idle_reliable=True,
                network_speed_bytes_per_second=20 * 1024,
                network_reliable=True,
            ),
        )

        self.assertFalse(result.ready_for_countdown)

    def test_idle_or_network_accepts_network(self):
        item = schedule(
            use_time=False,
            trigger_mode="idle",
            require_idle=True,
            require_network=True,
            condition_logic="OR",
        )
        result = self.engine.evaluate(
            item,
            context(
                TARGET,
                idle_seconds=0,
                idle_reliable=False,
                network_speed_bytes_per_second=20 * 1024,
                network_reliable=True,
            ),
        )

        self.assertFalse(result.for_type("idle").satisfied)
        self.assertTrue(result.for_type("network").satisfied)
        self.assertTrue(result.ready_for_countdown)

    def test_cpu_idle_network_and_requires_all(self):
        item = schedule(
            use_time=False,
            trigger_mode="idle",
            require_idle=True,
            require_cpu=True,
            require_network=True,
        )
        result = self.engine.evaluate(
            item,
            context(
                TARGET,
                idle_seconds=30 * 60,
                idle_reliable=True,
                cpu_usage=5,
                cpu_reliable=True,
                network_speed_bytes_per_second=60 * 1024,
                network_reliable=True,
            ),
        )

        self.assertFalse(result.ready_for_countdown)

    def test_cpu_idle_network_or_accepts_one(self):
        item = schedule(
            use_time=False,
            trigger_mode="idle",
            require_idle=True,
            require_cpu=True,
            require_network=True,
            condition_logic="OR",
        )
        result = self.engine.evaluate(
            item,
            context(
                TARGET,
                idle_reliable=False,
                cpu_usage=50,
                cpu_reliable=True,
                network_speed_bytes_per_second=20 * 1024,
                network_reliable=True,
            ),
        )

        self.assertTrue(result.ready_for_countdown)

    def test_disabled_condition_does_not_participate(self):
        item = schedule(condition_logic="AND", require_cpu=False)
        result = self.engine.evaluate(
            item,
            context(TARGET - timedelta(seconds=60), timed_occurrence()),
        )

        self.assertFalse(result.for_type("cpu").enabled)
        self.assertTrue(result.ready_for_countdown)

    def test_single_condition_and_or_are_equivalent(self):
        now = TARGET - timedelta(seconds=60)
        and_result = ConditionEngine().evaluate(
            schedule(condition_logic="AND"),
            context(now, timed_occurrence()),
        )
        or_result = ConditionEngine().evaluate(
            schedule(condition_logic="OR"),
            context(now, timed_occurrence()),
        )

        self.assertEqual(
            and_result.ready_for_countdown,
            or_result.ready_for_countdown,
        )

    def test_zero_enabled_conditions_is_never_ready(self):
        disabled = ConditionResult("cpu", False, False)

        self.assertFalse(combine_condition_results((), "AND"))
        self.assertFalse(combine_condition_results((disabled,), "OR"))

    def test_cpu_and_network_durations_remain_independent_under_or(self):
        item = schedule(
            condition_logic="OR",
            require_cpu=True,
            cpu_duration_seconds=300,
            require_network=True,
            network_duration_seconds=120,
        )
        started = TARGET - timedelta(hours=1)
        result = None
        for seconds in range(121):
            result = self.engine.evaluate(
                item,
                context(
                    started + timedelta(seconds=seconds),
                    timed_occurrence(),
                    cpu_usage=5,
                    cpu_reliable=True,
                    network_speed_bytes_per_second=20 * 1024,
                    network_reliable=True,
                ),
            )

        self.assertFalse(result.for_type("cpu").satisfied)
        self.assertTrue(result.for_type("network").satisfied)
        self.assertTrue(result.ready_for_countdown)

    def test_pending_or_keeps_original_target(self):
        item = schedule(condition_logic="OR", require_cpu=True)
        occurrence = timed_occurrence().mark_armed()
        result = self.engine.evaluate(
            item,
            context(
                TARGET + timedelta(minutes=10),
                occurrence,
                occurrence_pending=True,
                cpu_usage=50,
                cpu_reliable=True,
            ),
        )

        self.assertTrue(result.ready_for_countdown)
        self.assertEqual(result.scheduled_target, TARGET)


if __name__ == "__main__":
    unittest.main()
