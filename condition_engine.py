from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


CONDITION_TYPES = ("time", "date", "interval", "idle", "cpu", "network")
TRIGGER_MODES = ("time", "interval", "idle")


def interpolate_counters(samples, cutoff: float, maximum_bracket: float):
    before = [sample for sample in samples if sample[0] <= cutoff]
    after = [sample for sample in samples if sample[0] >= cutoff]
    if not before or not after:
        return None
    before_at, before_counters = before[-1]
    after_at, after_counters = after[0]
    if after_at == before_at:
        return before_counters
    bracket = after_at - before_at
    if bracket > maximum_bracket:
        return None
    position = (cutoff - before_at) / bracket
    return tuple(
        before_value + (after_value - before_value) * position
        for before_value, after_value in zip(before_counters, after_counters)
    )


def schedule_trigger_mode(schedule) -> str:
    if isinstance(schedule, dict):
        mode = str(schedule.get("trigger_mode", "")).lower()
        use_time = schedule.get("use_time", True)
    else:
        mode = str(getattr(schedule, "trigger_mode", "")).lower()
        use_time = getattr(schedule, "use_time", True)

    if mode in TRIGGER_MODES:
        return mode
    return "time" if bool(use_time) else "idle"


@dataclass(frozen=True)
class ScheduledOccurrence:
    schedule_id: str
    scheduled_target: datetime
    countdown_start: datetime
    armed: bool = False
    next_check_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    trigger_type: str = "time"
    start_at: Optional[datetime] = None
    duration_seconds: Optional[int] = None

    @classmethod
    def create(
        cls,
        schedule_id: str,
        scheduled_target: datetime,
        countdown_seconds: int,
        created_at: Optional[datetime] = None,
    ):
        return cls(
            schedule_id=schedule_id,
            scheduled_target=scheduled_target,
            countdown_start=scheduled_target - timedelta(
                seconds=max(0, int(countdown_seconds))
            ),
            created_at=created_at,
            trigger_type="time",
        )

    @classmethod
    def create_interval(
        cls,
        schedule_id: str,
        start_at: datetime,
        duration_minutes: int,
        countdown_seconds: int,
    ):
        duration_seconds = max(60, int(duration_minutes) * 60)
        scheduled_target = start_at + timedelta(seconds=duration_seconds)
        return cls(
            schedule_id=schedule_id,
            scheduled_target=scheduled_target,
            countdown_start=scheduled_target - timedelta(
                seconds=max(0, int(countdown_seconds))
            ),
            created_at=start_at,
            trigger_type="interval",
            start_at=start_at,
            duration_seconds=duration_seconds,
        )

    @classmethod
    def from_state(cls, schedule_id: str, data: Dict[str, Any]):
        scheduled_target = datetime.fromisoformat(data["scheduled_target"])
        countdown_start = datetime.fromisoformat(data["countdown_start"])
        next_check_raw = data.get("next_check_at")
        created_raw = data.get("created_at")
        return cls(
            schedule_id=schedule_id,
            scheduled_target=scheduled_target,
            countdown_start=countdown_start,
            armed=bool(data.get("armed", False)),
            next_check_at=(
                datetime.fromisoformat(next_check_raw)
                if next_check_raw
                else None
            ),
            created_at=(
                datetime.fromisoformat(created_raw)
                if created_raw
                else None
            ),
            trigger_type=str(data.get("trigger_type", "time")),
            start_at=(
                datetime.fromisoformat(data["start_at"])
                if data.get("start_at")
                else None
            ),
            duration_seconds=(
                int(data["duration_seconds"])
                if data.get("duration_seconds") is not None
                else None
            ),
        )

    def to_state(self) -> Dict[str, Any]:
        return {
            "scheduled_target": self.scheduled_target.isoformat(),
            "countdown_start": self.countdown_start.isoformat(),
            "armed": self.armed,
            "next_check_at": (
                self.next_check_at.isoformat()
                if self.next_check_at
                else None
            ),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "trigger_type": self.trigger_type,
            "start_at": self.start_at.isoformat() if self.start_at else None,
            "duration_seconds": self.duration_seconds,
        }

    def mark_armed(self):
        return replace(self, armed=True)

    def with_next_check(self, next_check_at: Optional[datetime]):
        return replace(self, next_check_at=next_check_at)


@dataclass(frozen=True)
class ConditionContext:
    now: datetime
    occurrence: Optional[ScheduledOccurrence] = None
    occurrence_pending: bool = False
    idle_seconds: float = 0.0
    idle_reliable: bool = False
    cpu_usage: Optional[float] = None
    cpu_average: Optional[float] = None
    cpu_reliable: bool = False
    cpu_status: str = "cpu_monitor_unavailable"
    network_rx_bytes_per_second: Optional[float] = None
    network_tx_bytes_per_second: Optional[float] = None
    network_speed_bytes_per_second: Optional[float] = None
    network_average_bytes_per_second: Optional[float] = None
    network_reliable: bool = False
    network_status: str = "network_monitor_unavailable"


@dataclass(frozen=True)
class ConditionResult:
    condition_type: str
    enabled: bool
    satisfied: bool
    info: Dict[str, Any] = field(default_factory=dict)
    satisfied_since: Optional[datetime] = None
    satisfied_for_seconds: Optional[float] = None
    reason: str = ""


@dataclass(frozen=True)
class ScheduleConditionResult:
    ready: bool
    ready_for_countdown: bool
    logic: str
    weekday_allowed: bool
    scheduled_target: Optional[datetime]
    countdown_start: Optional[datetime]
    countdown_due: bool
    trigger_reached: bool
    armed: bool
    pending: bool
    other_conditions_ready: bool
    conditions: Tuple[ConditionResult, ...]

    def for_type(self, condition_type: str) -> Optional[ConditionResult]:
        return next(
            (
                result
                for result in self.conditions
                if result.condition_type == condition_type
            ),
            None,
        )


class Condition:
    condition_type = ""

    def __init__(self, enabled: bool = True):
        self.enabled = bool(enabled)

    def evaluate(self, context: ConditionContext) -> ConditionResult:
        raise NotImplementedError

    def disabled_result(self) -> ConditionResult:
        return ConditionResult(
            condition_type=self.condition_type,
            enabled=False,
            satisfied=False,
            reason="disabled",
        )


class TimeCondition(Condition):
    condition_type = "time"

    def evaluate(self, context: ConditionContext) -> ConditionResult:
        if not self.enabled:
            return self.disabled_result()

        occurrence = context.occurrence
        if occurrence is None or occurrence.trigger_type != "time":
            return ConditionResult(
                condition_type=self.condition_type,
                enabled=True,
                satisfied=False,
                reason="missing_scheduled_target",
            )

        scheduled_target = occurrence.scheduled_target
        satisfied = context.now >= scheduled_target
        satisfied_for = None

        if satisfied:
            satisfied_for = max(
                0.0,
                (context.now - scheduled_target).total_seconds(),
            )

        return ConditionResult(
            condition_type=self.condition_type,
            enabled=True,
            satisfied=satisfied,
            info={
                "scheduled_target": scheduled_target,
                "scheduled_time": scheduled_target.strftime("%H:%M"),
            },
            satisfied_since=scheduled_target if satisfied else None,
            satisfied_for_seconds=satisfied_for,
            reason="trigger_reached" if satisfied else "waiting_for_time",
        )


class IntervalCondition(Condition):
    condition_type = "interval"

    def evaluate(self, context: ConditionContext) -> ConditionResult:
        if not self.enabled:
            return self.disabled_result()

        occurrence = context.occurrence
        if (
            occurrence is None
            or occurrence.trigger_type != "interval"
            or occurrence.start_at is None
            or occurrence.duration_seconds is None
        ):
            return ConditionResult(
                condition_type=self.condition_type,
                enabled=True,
                satisfied=False,
                reason="missing_interval_occurrence",
            )

        scheduled_target = occurrence.scheduled_target
        satisfied = context.now >= scheduled_target
        satisfied_for = None

        if satisfied:
            satisfied_for = max(
                0.0,
                (context.now - scheduled_target).total_seconds(),
            )

        return ConditionResult(
            condition_type=self.condition_type,
            enabled=True,
            satisfied=satisfied,
            info={
                "start_at": occurrence.start_at,
                "duration_seconds": occurrence.duration_seconds,
                "scheduled_target": scheduled_target,
            },
            satisfied_since=scheduled_target if satisfied else None,
            satisfied_for_seconds=satisfied_for,
            reason="interval_elapsed" if satisfied else "waiting_for_interval",
        )


class IdleCondition(Condition):
    condition_type = "idle"

    def __init__(self, minimum_seconds: int, enabled: bool = True):
        super().__init__(enabled)
        self.minimum_seconds = max(0, int(minimum_seconds))

    def evaluate(self, context: ConditionContext) -> ConditionResult:
        if not self.enabled:
            return self.disabled_result()

        idle_seconds = max(0.0, float(context.idle_seconds))
        reliable = bool(context.idle_reliable)
        satisfied = reliable and idle_seconds >= self.minimum_seconds
        satisfied_for = None
        satisfied_since = None

        if satisfied:
            satisfied_for = idle_seconds - self.minimum_seconds
            satisfied_since = context.now - timedelta(seconds=satisfied_for)

        if not reliable:
            reason = "idle_monitor_unavailable"
        elif satisfied:
            reason = "idle_threshold_reached"
        else:
            reason = "waiting_for_idle"

        return ConditionResult(
            condition_type=self.condition_type,
            enabled=True,
            satisfied=satisfied,
            info={
                "idle_seconds": idle_seconds,
                "minimum_seconds": self.minimum_seconds,
                "reliable": reliable,
            },
            satisfied_since=satisfied_since,
            satisfied_for_seconds=satisfied_for,
            reason=reason,
        )


class CPUCondition(Condition):
    condition_type = "cpu"

    def __init__(
        self,
        comparison: str,
        threshold: float,
        minimum_seconds: int,
        use_average: bool = False,
        enabled: bool = True,
    ):
        super().__init__(enabled)
        self.comparison = (
            "greater" if str(comparison).lower() == "greater" else "less"
        )
        self.threshold = min(100.0, max(0.0, float(threshold)))
        self.minimum_seconds = max(0, int(minimum_seconds))
        self.use_average = bool(use_average)

    def evaluate(self, context: ConditionContext) -> ConditionResult:
        if not self.enabled:
            return self.disabled_result()

        value = context.cpu_average if self.use_average else context.cpu_usage
        reliable = bool(context.cpu_reliable) and value is not None
        threshold_met = False
        if reliable:
            if self.comparison == "greater":
                threshold_met = value > self.threshold
            else:
                threshold_met = value < self.threshold

        if not reliable:
            reason = context.cpu_status or "cpu_monitor_unavailable"
        elif threshold_met:
            reason = "cpu_threshold_met"
        else:
            reason = "waiting_for_cpu_threshold"

        return ConditionResult(
            condition_type=self.condition_type,
            enabled=True,
            satisfied=threshold_met,
            info={
                "usage_percent": context.cpu_usage,
                "average_percent": context.cpu_average,
                "value_percent": value,
                "threshold": self.threshold,
                "comparison": self.comparison,
                "minimum_seconds": self.minimum_seconds,
                "use_average": self.use_average,
                "reliable": reliable,
                "threshold_met": threshold_met,
            },
            reason=reason,
        )


class NetworkCondition(Condition):
    condition_type = "network"

    def __init__(
        self,
        interface: str,
        direction: str,
        comparison: str,
        threshold_bytes_per_second: float,
        minimum_seconds: int,
        use_average: bool = False,
        enabled: bool = True,
    ):
        super().__init__(enabled)
        self.interface = str(interface)
        self.direction = (
            direction if direction in ("rx", "tx", "both") else "both"
        )
        self.comparison = (
            "greater" if str(comparison).lower() == "greater" else "less"
        )
        self.threshold = max(0.0, float(threshold_bytes_per_second))
        self.minimum_seconds = max(0, int(minimum_seconds))
        self.use_average = bool(use_average)

    def evaluate(self, context: ConditionContext) -> ConditionResult:
        if not self.enabled:
            return self.disabled_result()

        value = (
            context.network_average_bytes_per_second
            if self.use_average
            else context.network_speed_bytes_per_second
        )
        reliable = bool(context.network_reliable) and value is not None
        threshold_met = False
        if reliable:
            if self.comparison == "greater":
                threshold_met = value > self.threshold
            else:
                threshold_met = value < self.threshold

        if not reliable:
            reason = context.network_status or "network_monitor_unavailable"
        elif threshold_met:
            reason = "network_threshold_met"
        else:
            reason = "waiting_for_network_threshold"

        return ConditionResult(
            condition_type=self.condition_type,
            enabled=True,
            satisfied=threshold_met,
            info={
                "interface": self.interface,
                "direction": self.direction,
                "rx_bytes_per_second": context.network_rx_bytes_per_second,
                "tx_bytes_per_second": context.network_tx_bytes_per_second,
                "speed_bytes_per_second": (
                    context.network_speed_bytes_per_second
                ),
                "average_bytes_per_second": (
                    context.network_average_bytes_per_second
                ),
                "value_bytes_per_second": value,
                "threshold": self.threshold,
                "comparison": self.comparison,
                "minimum_seconds": self.minimum_seconds,
                "use_average": self.use_average,
                "reliable": reliable,
                "threshold_met": threshold_met,
            },
            reason=reason,
        )


@dataclass(frozen=True)
class CPUReading:
    usage_percent: Optional[float]
    average_percent: Optional[float]
    reliable: bool
    reason: str


@dataclass(frozen=True)
class NetworkReading:
    rx_bytes_per_second: Optional[float]
    tx_bytes_per_second: Optional[float]
    speed_bytes_per_second: Optional[float]
    average_bytes_per_second: Optional[float]
    reliable: bool
    reason: str


@dataclass(frozen=True)
class NetworkMonitorStatus:
    reliable: bool
    reason: str
    interfaces: Tuple[str, ...]


class CPUMonitor:
    """Muestrea el agregado de /proc/stat sin temporizadores ni dependencias Qt."""

    def __init__(
        self,
        sample_interval_seconds: float = 1.0,
        retention_seconds: float = 86400.0,
        reader=None,
        clock=None,
    ):
        self.sample_interval_seconds = max(0.1, float(sample_interval_seconds))
        self.retention_seconds = max(60.0, float(retention_seconds))
        self.reader = reader or self._read_proc_stat
        self.clock = clock or time.monotonic
        self._last_attempt_at = None
        self._previous_counters = None
        self._previous_sample_at = None
        self._usage_percent = None
        self._counter_samples = []
        self._reason = "cpu_monitor_warming_up"

    @staticmethod
    def _read_proc_stat():
        with open("/proc/stat", "r", encoding="ascii") as proc_stat:
            return proc_stat.readline()

    @staticmethod
    def parse_counters(raw: str) -> Tuple[int, int]:
        line = str(raw).splitlines()[0] if str(raw).splitlines() else ""
        fields = line.split()
        if not fields or fields[0] != "cpu" or len(fields) < 5:
            raise ValueError("línea agregada de CPU inválida")
        values = [int(value) for value in fields[1:]]
        user = values[0]
        nice = values[1] if len(values) > 1 else 0
        system = values[2] if len(values) > 2 else 0
        idle = values[3] if len(values) > 3 else 0
        iowait = values[4] if len(values) > 4 else 0
        irq = values[5] if len(values) > 5 else 0
        softirq = values[6] if len(values) > 6 else 0
        steal = values[7] if len(values) > 7 else 0
        idle_total = idle + iowait
        total = idle_total + user + nice + system + irq + softirq + steal
        return total, idle_total

    @staticmethod
    def calculate_usage(previous: Tuple[int, int], current: Tuple[int, int]):
        total_delta = current[0] - previous[0]
        idle_delta = current[1] - previous[1]
        if total_delta <= 0 or idle_delta < 0:
            raise ValueError("contadores de CPU no avanzaron")
        busy_delta = min(total_delta, max(0, total_delta - idle_delta))
        return 100.0 * busy_delta / total_delta

    def record(self, raw: str, sampled_at: Optional[float] = None):
        sampled_at = self.clock() if sampled_at is None else float(sampled_at)
        counters = self.parse_counters(raw)
        previous = self._previous_counters
        previous_at = self._previous_sample_at
        self._previous_counters = counters
        self._previous_sample_at = sampled_at
        if (
            previous is None
            or previous_at is None
            or sampled_at - previous_at > self.sample_interval_seconds * 3
        ):
            self._usage_percent = None
            self._counter_samples = [(sampled_at, counters)]
            self._reason = "cpu_monitor_warming_up"
            return self.reading()

        try:
            usage = self.calculate_usage(previous, counters)
        except ValueError as e:
            self._usage_percent = None
            self._counter_samples = [(sampled_at, counters)]
            self._reason = f"cpu_monitor_error: {e}"
            return self.reading()

        self._usage_percent = usage
        self._reason = "cpu_sample_available"
        self._counter_samples.append((sampled_at, counters))
        cutoff = sampled_at - self.retention_seconds
        recent = [
            sample for sample in self._counter_samples if sample[0] >= cutoff
        ]
        older = [
            sample for sample in self._counter_samples if sample[0] < cutoff
        ]
        self._counter_samples = older[-1:] + recent
        return self.reading()

    def sample(self, force: bool = False):
        now = self.clock()
        if (
            not force
            and self._last_attempt_at is not None
            and now - self._last_attempt_at < self.sample_interval_seconds
        ):
            return self.reading()
        self._last_attempt_at = now
        try:
            return self.record(self.reader(), sampled_at=now)
        except (OSError, ValueError, IndexError) as e:
            self._usage_percent = None
            self._previous_counters = None
            self._previous_sample_at = None
            self._counter_samples = []
            self._reason = f"cpu_monitor_error: {e}"
            return self.reading()

    def reading(self, average_window_seconds: int = 0) -> CPUReading:
        if self._usage_percent is None:
            return CPUReading(None, None, False, self._reason)

        window = max(0, int(average_window_seconds))
        if not window:
            return CPUReading(
                self._usage_percent,
                None,
                True,
                self._reason,
            )

        if not self._counter_samples:
            return CPUReading(
                self._usage_percent,
                None,
                False,
                "cpu_average_warming_up",
            )
        newest_at, newest_counters = self._counter_samples[-1]
        cutoff = newest_at - window
        baseline_counters = interpolate_counters(
            self._counter_samples,
            cutoff,
            self.sample_interval_seconds * 1.5,
        )
        if baseline_counters is None:
            return CPUReading(
                self._usage_percent,
                None,
                False,
                "cpu_average_warming_up",
            )
        try:
            average = self.calculate_usage(baseline_counters, newest_counters)
        except ValueError:
            return CPUReading(
                self._usage_percent,
                None,
                False,
                "cpu_average_warming_up",
            )
        return CPUReading(
            self._usage_percent,
            average,
            True,
            "cpu_average_available",
        )


class NetworkMonitor:
    """Muestrea RX/TX por interfaz desde /proc/net/dev."""

    def __init__(
        self,
        sample_interval_seconds: float = 1.0,
        retention_seconds: float = 3600.0,
        reader=None,
        identity_reader=None,
        clock=None,
    ):
        self.sample_interval_seconds = max(0.1, float(sample_interval_seconds))
        self.retention_seconds = min(3600.0, max(60.0, float(retention_seconds)))
        self.reader = reader or self._read_proc_net_dev
        self.identity_reader = (
            identity_reader or self._read_interface_identities
        )
        self.clock = clock or time.monotonic
        self._last_attempt_at = None
        self._previous_at = None
        self._previous_counters = {}
        self._current_counters = {}
        self._current_rates = {}
        self._counter_samples = {}
        self._interface_identities = {}
        self._reason = "network_monitor_warming_up"

    @staticmethod
    def _read_proc_net_dev():
        with open("/proc/net/dev", "r", encoding="ascii") as proc_net_dev:
            return proc_net_dev.read()

    @staticmethod
    def parse_counters(raw: str) -> Dict[str, Tuple[int, int]]:
        counters = {}
        for line in str(raw).splitlines():
            if ":" not in line:
                continue
            interface, values_raw = line.split(":", 1)
            values = values_raw.split()
            if len(values) < 16:
                continue
            counters[interface.strip()] = (int(values[0]), int(values[8]))
        if not counters:
            raise ValueError("/proc/net/dev no contiene interfaces")
        return counters

    @classmethod
    def discover_interfaces(cls, sys_path=Path("/sys/class/net")):
        interfaces = set()
        try:
            interfaces.update(entry.name for entry in Path(sys_path).iterdir())
        except OSError:
            pass
        try:
            interfaces.update(cls.parse_counters(cls._read_proc_net_dev()))
        except (OSError, ValueError):
            pass
        return tuple(sorted(interfaces))

    @staticmethod
    def _read_interface_identities(interfaces):
        identities = {}
        for interface in interfaces:
            try:
                value = (
                    Path("/sys/class/net") / interface / "ifindex"
                ).read_text(encoding="ascii")
                identities[interface] = int(value.strip())
            except (OSError, ValueError):
                pass
        return identities

    @staticmethod
    def calculate_rates(
        previous: Tuple[int, int],
        current: Tuple[int, int],
        elapsed_seconds: float,
    ) -> Tuple[float, float]:
        elapsed = float(elapsed_seconds)
        rx_delta = current[0] - previous[0]
        tx_delta = current[1] - previous[1]
        if elapsed <= 0 or rx_delta < 0 or tx_delta < 0:
            raise ValueError("contadores de red no avanzaron")
        return rx_delta / elapsed, tx_delta / elapsed

    @staticmethod
    def select_direction(rates: Tuple[float, float], direction: str) -> float:
        if direction == "rx":
            return rates[0]
        if direction == "tx":
            return rates[1]
        return rates[0] + rates[1]

    def _reset(self, reason: str):
        self._previous_at = None
        self._previous_counters = {}
        self._current_counters = {}
        self._current_rates = {}
        self._counter_samples = {}
        self._interface_identities = {}
        self._reason = reason

    def record(self, raw: str, sampled_at: Optional[float] = None):
        sampled_at = self.clock() if sampled_at is None else float(sampled_at)
        counters = self.parse_counters(raw)
        identities = self.identity_reader(tuple(counters))
        previous_at = self._previous_at
        previous = self._previous_counters
        previous_identities = self._interface_identities
        gap = None if previous_at is None else sampled_at - previous_at
        if gap is None or gap > self.sample_interval_seconds * 3:
            self._current_rates = {}
            self._counter_samples = {
                interface: [(sampled_at, values)]
                for interface, values in counters.items()
            }
            self._reason = "network_monitor_warming_up"
        else:
            rates = {}
            histories = {}
            cutoff = sampled_at - self.retention_seconds
            for interface, values in counters.items():
                history = self._counter_samples.get(interface, [])
                old_values = previous.get(interface)
                identity_changed = (
                    interface in previous_identities
                    and interface in identities
                    and previous_identities[interface] != identities[interface]
                )
                if identity_changed:
                    old_values = None
                    history = []
                if old_values is not None:
                    try:
                        rates[interface] = self.calculate_rates(
                            old_values,
                            values,
                            gap,
                        )
                    except ValueError:
                        history = []
                history.append((sampled_at, values))
                recent = [sample for sample in history if sample[0] >= cutoff]
                older = [sample for sample in history if sample[0] < cutoff]
                histories[interface] = older[-1:] + recent
            self._current_rates = rates
            self._counter_samples = histories
            self._reason = "network_sample_available"
        self._previous_at = sampled_at
        self._previous_counters = counters
        self._current_counters = counters
        self._interface_identities = identities
        return self.status()

    def sample(self, force: bool = False):
        now = self.clock()
        if (
            not force
            and self._last_attempt_at is not None
            and now - self._last_attempt_at < self.sample_interval_seconds
        ):
            return self.status()
        self._last_attempt_at = now
        try:
            return self.record(self.reader(), sampled_at=now)
        except (OSError, ValueError, IndexError) as e:
            self._reset(f"network_monitor_error: {e}")
            return self.status()

    def status(self) -> NetworkMonitorStatus:
        return NetworkMonitorStatus(
            reliable=bool(self._current_rates),
            reason=self._reason,
            interfaces=tuple(sorted(self._current_counters)),
        )

    def available_interfaces(self) -> Tuple[str, ...]:
        interfaces = set(self._current_counters)
        interfaces.update(self.discover_interfaces())
        return tuple(sorted(interfaces))

    def reading(
        self,
        interface: str,
        direction: str,
        average_window_seconds: int = 0,
    ) -> NetworkReading:
        if interface not in self._current_counters:
            return NetworkReading(
                None,
                None,
                None,
                None,
                False,
                "network_interface_unavailable",
            )
        rates = self._current_rates.get(interface)
        if rates is None:
            return NetworkReading(
                None,
                None,
                None,
                None,
                False,
                self._reason,
            )
        direction = direction if direction in ("rx", "tx", "both") else "both"
        speed = self.select_direction(rates, direction)
        requested_window = max(0, int(average_window_seconds))
        window = max(2, requested_window) if requested_window else 0
        if not window:
            return NetworkReading(
                rates[0],
                rates[1],
                speed,
                None,
                True,
                "network_sample_available",
            )

        history = self._counter_samples.get(interface, [])
        newest_at, newest_counters = history[-1]
        cutoff = newest_at - window
        baseline = interpolate_counters(
            history,
            cutoff,
            min(
                self.sample_interval_seconds * 3,
                max(self.sample_interval_seconds * 1.5, window),
            ),
        )
        if baseline is None:
            return NetworkReading(
                rates[0],
                rates[1],
                speed,
                None,
                False,
                "network_average_warming_up",
            )
        try:
            average_rates = self.calculate_rates(
                baseline,
                newest_counters,
                window,
            )
        except ValueError:
            return NetworkReading(
                rates[0],
                rates[1],
                speed,
                None,
                False,
                "network_average_warming_up",
            )
        return NetworkReading(
            rates[0],
            rates[1],
            speed,
            self.select_direction(average_rates, direction),
            True,
            "network_average_available",
        )


class ConditionRuntimeState:
    """Estado temporal separado de la configuración persistente."""

    def __init__(self):
        self.activity_generation = 0
        self._idle_triggered_generation = {}
        self._idle_snooze_generation = {}
        self._condition_satisfied_since = {}
        self._condition_last_evaluated_at = {}

    def record_activity(self):
        self.activity_generation += 1

    def idle_cycle_was_triggered(self, schedule_id: str) -> bool:
        return (
            self._idle_triggered_generation.get(schedule_id)
            == self.activity_generation
        )

    def mark_idle_cycle_triggered(self, schedule_id: str):
        self._idle_triggered_generation[schedule_id] = self.activity_generation

    def clear_idle_cycle_triggered(self, schedule_id: str):
        self._idle_triggered_generation.pop(schedule_id, None)

    def mark_idle_snoozed(self, schedule_id: str):
        self._idle_snooze_generation[schedule_id] = self.activity_generation

    def idle_snooze_was_invalidated(self, schedule_id: str) -> bool:
        generation = self._idle_snooze_generation.get(schedule_id)
        return generation is not None and generation != self.activity_generation

    def clear_idle_snooze(self, schedule_id: str):
        self._idle_snooze_generation.pop(schedule_id, None)

    def cpu_satisfied_since(self, schedule_id: str) -> Optional[datetime]:
        return self.condition_satisfied_since(schedule_id, "cpu")

    def mark_cpu_satisfied(self, schedule_id: str, now: datetime) -> datetime:
        return self.mark_condition_satisfied(schedule_id, "cpu", now)

    def clear_cpu_satisfied(self, schedule_id: str):
        self.clear_condition_satisfied(schedule_id, "cpu")

    def record_cpu_evaluation(self, schedule_id: str, now: datetime) -> bool:
        return self.record_condition_evaluation(schedule_id, "cpu", now)

    def clear_cpu_runtime(self, schedule_id: str):
        self.clear_condition_runtime(schedule_id, "cpu")

    def condition_satisfied_since(
        self,
        schedule_id: str,
        condition_type: str,
    ) -> Optional[datetime]:
        return self._condition_satisfied_since.get(
            (schedule_id, condition_type)
        )

    def mark_condition_satisfied(
        self,
        schedule_id: str,
        condition_type: str,
        now: datetime,
    ) -> datetime:
        return self._condition_satisfied_since.setdefault(
            (schedule_id, condition_type),
            now,
        )

    def clear_condition_satisfied(self, schedule_id: str, condition_type: str):
        self._condition_satisfied_since.pop((schedule_id, condition_type), None)

    def record_condition_evaluation(
        self,
        schedule_id: str,
        condition_type: str,
        now: datetime,
    ) -> bool:
        key = (schedule_id, condition_type)
        previous = self._condition_last_evaluated_at.get(key)
        self._condition_last_evaluated_at[key] = now
        if previous is None:
            return False
        gap = (now - previous).total_seconds()
        return 0 <= gap <= 5

    def clear_condition_runtime(self, schedule_id: str, condition_type: str):
        key = (schedule_id, condition_type)
        self.clear_condition_satisfied(schedule_id, condition_type)
        self._condition_last_evaluated_at.pop(key, None)


class ConditionEngine:
    def __init__(self):
        self.runtime = ConditionRuntimeState()

    def conditions_for(self, schedule) -> Tuple[Condition, ...]:
        trigger_mode = schedule_trigger_mode(schedule)
        idle_enabled = (
            bool(getattr(schedule, "require_idle", False))
            or trigger_mode == "idle"
        )
        idle_minutes = int(getattr(schedule, "idle_minutes", 30))

        # El modo solo-inactividad de v1.3.0 siempre aplica un mínimo seguro
        # de un minuto. El modo con hora conserva literalmente su umbral.
        idle_seconds = idle_minutes * 60
        if trigger_mode == "idle":
            idle_seconds = max(60, idle_seconds)

        return (
            TimeCondition(enabled=trigger_mode == "time"),
            IntervalCondition(enabled=trigger_mode == "interval"),
            IdleCondition(idle_seconds, enabled=idle_enabled),
            CPUCondition(
                getattr(schedule, "cpu_comparison", "less"),
                getattr(schedule, "cpu_threshold", 10),
                getattr(schedule, "cpu_duration_seconds", 300),
                use_average=getattr(schedule, "cpu_use_average", False),
                enabled=getattr(schedule, "require_cpu", False),
            ),
            NetworkCondition(
                getattr(schedule, "network_interface", ""),
                getattr(schedule, "network_direction", "both"),
                getattr(schedule, "network_comparison", "less"),
                float(getattr(schedule, "network_threshold", 50))
                * (
                    1024 * 1024
                    if getattr(schedule, "network_unit", "KB/s") == "MB/s"
                    else 1024
                ),
                getattr(schedule, "network_duration_seconds", 300),
                use_average=getattr(
                    schedule,
                    "network_use_average",
                    False,
                ),
                enabled=getattr(schedule, "require_network", False),
            ),
        )

    def evaluate(
        self,
        schedule,
        context: ConditionContext,
    ) -> ScheduleConditionResult:
        results = []
        for condition in self.conditions_for(schedule):
            result = condition.evaluate(context)
            if condition.condition_type in ("cpu", "network"):
                result = self._apply_continuous_duration(
                    str(getattr(schedule, "id", "")),
                    condition,
                    result,
                    context.now,
                )
            results.append(result)
        results = tuple(results)
        non_temporal_results = tuple(
            result
            for result in results
            if (
                result.enabled
                and result.condition_type not in ("time", "interval")
            )
        )
        logic = str(getattr(schedule, "condition_logic", "AND")).upper()

        if logic not in ("AND", "OR"):
            logic = "AND"

        if not non_temporal_results:
            other_conditions_ready = True
        elif logic == "OR":
            other_conditions_ready = any(
                result.satisfied for result in non_temporal_results
            )
        else:
            other_conditions_ready = all(
                result.satisfied for result in non_temporal_results
            )

        trigger_mode = schedule_trigger_mode(schedule)
        weekdays = getattr(schedule, "weekdays", ())
        occurrence = context.occurrence

        if trigger_mode in ("time", "interval"):
            scheduled_target = (
                occurrence.scheduled_target if occurrence else None
            )
            countdown_start = occurrence.countdown_start if occurrence else None
            # Una ocurrencia normal se califica contra el calendario actual.
            # Una pendiente ya fue calificada al crearse y conserva esa
            # identidad aunque continúe otro día.
            weekday_allowed = bool(
                occurrence
                and (
                    trigger_mode == "interval"
                    or
                    context.occurrence_pending
                    or scheduled_target.weekday() in weekdays
                )
            )
            countdown_due = bool(
                countdown_start and context.now >= countdown_start
            )
            temporal_result = next(
                result
                for result in results
                if result.condition_type == trigger_mode
            )
            trigger_reached = temporal_result.satisfied
            armed = bool(occurrence and occurrence.armed) or trigger_reached
            ready_for_countdown = (
                weekday_allowed
                and countdown_due
                and other_conditions_ready
            )
            pending = (
                context.occurrence_pending
                and armed
                and not other_conditions_ready
            )
        else:
            scheduled_target = None
            countdown_start = None
            weekday_allowed = context.now.weekday() in weekdays
            countdown_due = True
            trigger_reached = True
            armed = False
            pending = False
            ready_for_countdown = (
                weekday_allowed and other_conditions_ready
            )

        return ScheduleConditionResult(
            ready=ready_for_countdown,
            ready_for_countdown=ready_for_countdown,
            logic=logic,
            weekday_allowed=weekday_allowed,
            scheduled_target=scheduled_target,
            countdown_start=countdown_start,
            countdown_due=countdown_due,
            trigger_reached=trigger_reached,
            armed=armed,
            pending=pending,
            other_conditions_ready=other_conditions_ready,
            conditions=results,
        )

    def _apply_continuous_duration(
        self,
        schedule_id: str,
        condition,
        result: ConditionResult,
        now: datetime,
    ) -> ConditionResult:
        condition_type = condition.condition_type
        if not result.enabled:
            self.runtime.clear_condition_runtime(schedule_id, condition_type)
            return result

        continuous_observation = self.runtime.record_condition_evaluation(
            schedule_id,
            condition_type,
            now,
        )
        if not continuous_observation:
            self.runtime.clear_condition_satisfied(schedule_id, condition_type)

        if not result.satisfied:
            self.runtime.clear_condition_satisfied(schedule_id, condition_type)
            return result

        satisfied_since = self.runtime.mark_condition_satisfied(
            schedule_id,
            condition_type,
            now,
        )
        elapsed = max(0.0, (now - satisfied_since).total_seconds())
        satisfied = elapsed >= condition.minimum_seconds
        info = dict(result.info)
        info["threshold_met"] = True
        if satisfied:
            reason = f"{condition_type}_duration_reached"
        else:
            reason = f"waiting_for_{condition_type}_duration"
        return replace(
            result,
            satisfied=satisfied,
            info=info,
            satisfied_since=satisfied_since,
            satisfied_for_seconds=elapsed,
            reason=reason,
        )
