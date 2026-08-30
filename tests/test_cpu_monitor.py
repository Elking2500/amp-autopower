import unittest

from condition_engine import CPUMonitor


class CPUMonitorTests(unittest.TestCase):
    def test_calculates_usage_from_known_proc_stat_samples(self):
        previous = CPUMonitor.parse_counters("cpu 100 0 100 800 0 0 0 0")
        current = CPUMonitor.parse_counters("cpu 150 0 150 900 0 0 0 0")

        self.assertEqual(CPUMonitor.calculate_usage(previous, current), 50.0)

    def test_moving_average_uses_configured_window(self):
        monitor = CPUMonitor()
        monitor.record("cpu 100 0 100 800 0 0 0 0", sampled_at=0)
        monitor.record("cpu 105 0 105 890 0 0 0 0", sampled_at=1)
        monitor.record("cpu 115 0 115 970 0 0 0 0", sampled_at=2)
        monitor.record("cpu 130 0 130 1040 0 0 0 0", sampled_at=3)

        reading = monitor.reading(average_window_seconds=2)

        self.assertTrue(reading.reliable)
        self.assertAlmostEqual(reading.usage_percent, 30.0)
        self.assertAlmostEqual(reading.average_percent, 25.0)

    def test_average_waits_for_a_complete_window(self):
        monitor = CPUMonitor()
        monitor.record("cpu 100 0 100 800 0 0 0 0", sampled_at=0)
        monitor.record("cpu 110 0 110 880 0 0 0 0", sampled_at=1)

        reading = monitor.reading(average_window_seconds=60)

        self.assertFalse(reading.reliable)
        self.assertIsNone(reading.average_percent)
        self.assertEqual(reading.reason, "cpu_average_warming_up")

    def test_sample_is_throttled(self):
        samples = iter((
            "cpu 100 0 100 800 0 0 0 0",
            "cpu 110 0 110 880 0 0 0 0",
        ))
        reads = []
        now = [0.0]

        def reader():
            reads.append(True)
            return next(samples)

        monitor = CPUMonitor(reader=reader, clock=lambda: now[0])
        monitor.sample()
        now[0] = 0.5
        monitor.sample()
        now[0] = 1.0
        monitor.sample()

        self.assertEqual(len(reads), 2)

    def test_sampling_gap_restarts_monitor_warmup(self):
        monitor = CPUMonitor()
        monitor.record("cpu 100 0 100 800 0 0 0 0", sampled_at=0)
        monitor.record("cpu 110 0 110 880 0 0 0 0", sampled_at=1)

        reading = monitor.record(
            "cpu 210 0 210 1780 0 0 0 0",
            sampled_at=10,
        )

        self.assertFalse(reading.reliable)
        self.assertEqual(reading.reason, "cpu_monitor_warming_up")

    def test_average_rejects_baseline_too_far_before_window(self):
        monitor = CPUMonitor()
        monitor.record("cpu 100 0 100 800 0 0 0 0", sampled_at=0)
        monitor.record("cpu 130 0 130 1040 0 0 0 0", sampled_at=2.9)

        reading = monitor.reading(average_window_seconds=1)

        self.assertFalse(reading.reliable)
        self.assertEqual(reading.reason, "cpu_average_warming_up")

    def test_average_interpolates_between_nearby_samples(self):
        monitor = CPUMonitor()
        monitor.record("cpu 100 0 100 800 0 0 0 0", sampled_at=0)
        monitor.record("cpu 111 0 111 888 0 0 0 0", sampled_at=1.1)
        monitor.record("cpu 121 0 121 968 0 0 0 0", sampled_at=2.1)

        reading = monitor.reading(average_window_seconds=1)

        self.assertTrue(reading.reliable)
        self.assertAlmostEqual(reading.average_percent, 20.0)


if __name__ == "__main__":
    unittest.main()
