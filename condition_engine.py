from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple


CONDITION_TYPES = ("time", "date", "interval", "idle", "cpu", "network")


@dataclass(frozen=True)
class ScheduledOccurrence:
    schedule_id: str
    scheduled_target: datetime
    countdown_start: datetime
    armed: bool = False
    next_check_at: Optional[datetime] = None
    created_at: Optional[datetime] = None

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
        if occurrence is None:
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


class ConditionRuntimeState:
    """Estado temporal separado de la configuración persistente."""

    def __init__(self):
        self.activity_generation = 0
        self._idle_triggered_generation = {}
        self._idle_snooze_generation = {}

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


class ConditionEngine:
    def __init__(self):
        self.runtime = ConditionRuntimeState()

    def conditions_for(self, schedule) -> Tuple[Condition, ...]:
        use_time = bool(getattr(schedule, "use_time", True))
        idle_enabled = bool(getattr(schedule, "require_idle", False)) or not use_time
        idle_minutes = int(getattr(schedule, "idle_minutes", 30))

        # El modo solo-inactividad de v1.3.0 siempre aplica un mínimo seguro
        # de un minuto. El modo con hora conserva literalmente su umbral.
        idle_seconds = idle_minutes * 60
        if not use_time:
            idle_seconds = max(60, idle_seconds)

        return (
            TimeCondition(enabled=use_time),
            IdleCondition(idle_seconds, enabled=idle_enabled),
        )

    def evaluate(
        self,
        schedule,
        context: ConditionContext,
    ) -> ScheduleConditionResult:
        results = tuple(
            condition.evaluate(context)
            for condition in self.conditions_for(schedule)
        )
        non_time_results = tuple(
            result
            for result in results
            if result.enabled and result.condition_type != "time"
        )
        logic = str(getattr(schedule, "condition_logic", "AND")).upper()

        if logic not in ("AND", "OR"):
            logic = "AND"

        if not non_time_results:
            other_conditions_ready = True
        elif logic == "OR":
            other_conditions_ready = any(
                result.satisfied for result in non_time_results
            )
        else:
            other_conditions_ready = all(
                result.satisfied for result in non_time_results
            )

        use_time = bool(getattr(schedule, "use_time", True))
        weekdays = getattr(schedule, "weekdays", ())
        occurrence = context.occurrence

        if use_time:
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
                    context.occurrence_pending
                    or scheduled_target.weekday() in weekdays
                )
            )
            countdown_due = bool(
                countdown_start and context.now >= countdown_start
            )
            time_result = next(
                result
                for result in results
                if result.condition_type == "time"
            )
            trigger_reached = time_result.satisfied
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
