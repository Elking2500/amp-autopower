import unittest
from unittest.mock import patch

from condition_engine import NetworkMonitor


def proc_net_dev(**interfaces):
    lines = [
        "Inter-| Receive | Transmit",
        " face |bytes packets errs drop fifo frame compressed multicast|bytes",
    ]
    for interface, (rx_bytes, tx_bytes) in interfaces.items():
        lines.append(
            f"{interface}: {rx_bytes} 0 0 0 0 0 0 0 "
            f"{tx_bytes} 0 0 0 0 0 0 0"
        )
    return "\n".join(lines)


class NetworkMonitorTests(unittest.TestCase):
    def test_calculates_rx_and_tx_rates(self):
        rates = NetworkMonitor.calculate_rates((1000, 500), (3048, 1524), 2)

        self.assertEqual(rates[0], 1024)
        self.assertEqual(rates[1], 512)

    def test_selects_rx_tx_and_combined(self):
        rates = (1024.0, 2048.0)

        self.assertEqual(NetworkMonitor.select_direction(rates, "rx"), 1024)
        self.assertEqual(NetworkMonitor.select_direction(rates, "tx"), 2048)
        self.assertEqual(
            NetworkMonitor.select_direction(rates, "both"),
            3072,
        )

    def test_reports_kibibytes_per_second(self):
        monitor = NetworkMonitor()
        monitor.record(proc_net_dev(enp1s0=(0, 0)), sampled_at=0)
        monitor.record(proc_net_dev(enp1s0=(2048, 0)), sampled_at=1)

        reading = monitor.reading("enp1s0", "rx")

        self.assertTrue(reading.reliable)
        self.assertEqual(reading.speed_bytes_per_second / 1024, 2)

    def test_first_sample_has_no_valid_rate(self):
        monitor = NetworkMonitor()

        monitor.record(proc_net_dev(wlan0=(100, 200)), sampled_at=0)
        reading = monitor.reading("wlan0", "both")

        self.assertFalse(reading.reliable)
        self.assertIsNone(reading.speed_bytes_per_second)
        self.assertEqual(reading.reason, "network_monitor_warming_up")

    def test_moving_average_uses_counter_window(self):
        monitor = NetworkMonitor()
        monitor.record(proc_net_dev(enp1s0=(0, 0)), sampled_at=0)
        monitor.record(proc_net_dev(enp1s0=(100, 200)), sampled_at=1)
        monitor.record(proc_net_dev(enp1s0=(300, 600)), sampled_at=2)

        reading = monitor.reading("enp1s0", "both", 2)

        self.assertTrue(reading.reliable)
        self.assertEqual(reading.speed_bytes_per_second, 600)
        self.assertEqual(reading.average_bytes_per_second, 450)

    def test_interface_disappearance_is_unavailable(self):
        monitor = NetworkMonitor()
        monitor.record(
            proc_net_dev(enp1s0=(0, 0), lo=(0, 0)),
            sampled_at=0,
        )
        monitor.record(proc_net_dev(lo=(100, 100)), sampled_at=1)

        reading = monitor.reading("enp1s0", "both")

        self.assertFalse(reading.reliable)
        self.assertEqual(reading.reason, "network_interface_unavailable")

    def test_new_interface_requires_its_own_second_sample(self):
        monitor = NetworkMonitor()
        monitor.record(proc_net_dev(enp1s0=(0, 0)), sampled_at=0)
        monitor.record(
            proc_net_dev(enp1s0=(100, 100), wlan0=(50, 25)),
            sampled_at=1,
        )

        warming = monitor.reading("wlan0", "both")
        monitor.record(
            proc_net_dev(enp1s0=(200, 200), wlan0=(150, 75)),
            sampled_at=2,
        )
        ready = monitor.reading("wlan0", "both")

        self.assertFalse(warming.reliable)
        self.assertTrue(ready.reliable)
        self.assertEqual(ready.speed_bytes_per_second, 150)

    def test_recreated_interface_with_new_ifindex_requires_second_sample(self):
        identities = [{"enp1s0": 2}, {"enp1s0": 2}, {"enp1s0": 7}]
        monitor = NetworkMonitor(
            identity_reader=lambda _interfaces: identities.pop(0)
        )
        monitor.record(proc_net_dev(enp1s0=(100, 100)), sampled_at=0)
        monitor.record(proc_net_dev(enp1s0=(200, 200)), sampled_at=1)
        monitor.record(proc_net_dev(enp1s0=(500, 500)), sampled_at=2)

        reading = monitor.reading("enp1s0", "both")

        self.assertFalse(reading.reliable)
        self.assertEqual(reading.reason, "network_sample_available")

    def test_counter_reset_only_invalidates_affected_interface(self):
        monitor = NetworkMonitor()
        monitor.record(
            proc_net_dev(enp1s0=(1000, 1000), lo=(1000, 1000)),
            sampled_at=0,
        )
        monitor.record(
            proc_net_dev(enp1s0=(1100, 1100), lo=(1100, 1100)),
            sampled_at=1,
        )
        monitor.record(
            proc_net_dev(enp1s0=(10, 10), lo=(1200, 1200)),
            sampled_at=2,
        )

        self.assertFalse(monitor.reading("enp1s0", "both").reliable)
        self.assertTrue(monitor.reading("lo", "both").reliable)

    def test_available_interfaces_combines_cache_and_current_discovery(self):
        monitor = NetworkMonitor()
        monitor.record(proc_net_dev(enp1s0=(0, 0)), sampled_at=0)

        with patch.object(
            NetworkMonitor,
            "discover_interfaces",
            return_value=("wlan0",),
        ):
            interfaces = monitor.available_interfaces()

        self.assertEqual(interfaces, ("enp1s0", "wlan0"))

    def test_wide_average_window_accepts_moderate_sampling_jitter(self):
        monitor = NetworkMonitor()
        monitor.record(proc_net_dev(enp1s0=(0, 0)), sampled_at=0)
        for index in range(1, 42):
            monitor.record(
                proc_net_dev(enp1s0=(index * 160, index * 80)),
                sampled_at=index * 1.6,
            )

        reading = monitor.reading("enp1s0", "both", 60)

        self.assertTrue(reading.reliable)
        self.assertAlmostEqual(reading.average_bytes_per_second, 150)

    def test_short_average_uses_two_second_minimum_with_jitter(self):
        monitor = NetworkMonitor()
        monitor.record(proc_net_dev(enp1s0=(0, 0)), sampled_at=0)
        monitor.record(proc_net_dev(enp1s0=(160, 80)), sampled_at=1.6)
        monitor.record(proc_net_dev(enp1s0=(320, 160)), sampled_at=3.2)

        reading = monitor.reading("enp1s0", "both", 1)

        self.assertTrue(reading.reliable)
        self.assertAlmostEqual(reading.average_bytes_per_second, 150)

    def test_recovers_safely_after_reader_error(self):
        values = [
            proc_net_dev(enp1s0=(0, 0)),
            OSError("simulated"),
            proc_net_dev(enp1s0=(200, 100)),
            proc_net_dev(enp1s0=(300, 150)),
        ]
        now = [0.0]

        def reader():
            value = values.pop(0)
            if isinstance(value, Exception):
                raise value
            return value

        monitor = NetworkMonitor(reader=reader, clock=lambda: now[0])
        first = monitor.sample()
        now[0] = 1
        failed = monitor.sample()
        now[0] = 2
        warming = monitor.sample()
        now[0] = 3
        recovered = monitor.sample()

        self.assertFalse(first.reliable)
        self.assertFalse(failed.reliable)
        self.assertTrue(failed.reason.startswith("network_monitor_error"))
        self.assertFalse(warming.reliable)
        self.assertTrue(recovered.reliable)


if __name__ == "__main__":
    unittest.main()
