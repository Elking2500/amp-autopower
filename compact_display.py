from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Sequence, Tuple

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCloseEvent, QFont, QMouseEvent, QPainter
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


DISPLAY_WIDTH = 172
DISPLAY_HEIGHT = 110


@dataclass(frozen=True)
class DisplayOptions:
    enabled: bool = False
    always_on_top: bool = True
    show_title: bool = True
    show_action: bool = True
    transparency_percent: int = 25
    use_24_hour: bool = True


@dataclass(frozen=True)
class DisplayScreen:
    name: str
    x: int
    y: int
    width: int
    height: int


@dataclass(frozen=True)
class DisplayCondition:
    key: str
    indicator: str
    title: str
    value: str


@dataclass(frozen=True)
class DisplaySnapshot:
    schedule_id: str
    title: str
    action: str
    logic: str
    conditions: Tuple[DisplayCondition, ...]
    priority: int
    due: Optional[datetime] = None
    selected_condition: Optional[str] = None


def display_options_from_config(config) -> DisplayOptions:
    return DisplayOptions(
        enabled=bool(config.get("display_enabled", False)),
        always_on_top=bool(config.get("display_always_on_top", True)),
        show_title=bool(config.get("display_show_title", True)),
        show_action=bool(config.get("display_show_action", True)),
        transparency_percent=max(
            1,
            min(99, int(config.get("display_transparency_percent", 25))),
        ),
        use_24_hour=bool(config.get("display_use_24_hour", True)),
    )


def store_display_options(config, options: DisplayOptions):
    config.update(
        {
            "display_enabled": options.enabled,
            "display_always_on_top": options.always_on_top,
            "display_show_title": options.show_title,
            "display_show_action": options.show_action,
            "display_transparency_percent": options.transparency_percent,
            "display_use_24_hour": options.use_24_hour,
        }
    )


def opacity_from_transparency(transparency_percent: int) -> float:
    transparency = max(1, min(99, int(transparency_percent)))
    return (100 - transparency) / 100.0


def background_alpha_from_transparency(transparency_percent: int) -> int:
    return round(255 * opacity_from_transparency(transparency_percent))


def format_clock(
    value: datetime,
    use_24_hour: bool = True,
    include_seconds: bool = False,
) -> str:
    if use_24_hour:
        return value.strftime("%H:%M:%S" if include_seconds else "%H:%M")
    hour = value.hour % 12 or 12
    suffix = "AM" if value.hour < 12 else "PM"
    if include_seconds:
        return f"{hour}:{value.minute:02d}:{value.second:02d} {suffix}"
    return f"{hour}:{value.minute:02d} {suffix}"


def format_schedule_time(value: str, use_24_hour: bool = True) -> str:
    parsed = datetime.strptime(str(value), "%H:%M")
    return format_clock(parsed, use_24_hour)


def format_ui_datetime(
    value: datetime,
    use_24_hour: bool = True,
    date_format: str = "%a %d/%m",
    include_seconds: bool = False,
) -> str:
    return (
        f"{value.strftime(date_format)} "
        f"{format_clock(value, use_24_hour, include_seconds)}"
    )


def format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def format_interval_remaining(
    scheduled_target: datetime,
    now: datetime,
    pending: bool = False,
) -> str:
    if pending and now >= scheduled_target:
        return "PEND"
    return format_duration((scheduled_target - now).total_seconds())


def format_cpu(value: Optional[float], reliable: bool) -> str:
    if not reliable or value is None:
        return "--"
    return f"{value:.0f} %"


def format_network(value: Optional[float], reliable: bool) -> str:
    if not reliable or value is None:
        return "--"
    kibibytes = max(0.0, value) / 1024.0
    if kibibytes >= 1024:
        return f"{kibibytes / 1024.0:.1f} MB/s"
    return f"{kibibytes:.0f} KB/s"


def format_logic(logic: str, condition_count: int) -> str:
    if condition_count < 2:
        return ""
    return "OR" if str(logic).upper() == "OR" else "AND"


def select_condition_key(
    available_keys: Sequence[str],
    current_key: Optional[str] = None,
    requested_key: Optional[str] = None,
) -> Optional[str]:
    keys = tuple(available_keys)
    if requested_key in keys:
        return requested_key
    if current_key in keys:
        return current_key
    return keys[0] if keys else None


def sort_display_snapshots(snapshots: Sequence[DisplaySnapshot]):
    best_by_schedule = {}
    for index, snapshot in enumerate(snapshots):
        sort_key = (
            snapshot.priority,
            snapshot.due or datetime.max,
            index,
        )
        previous = best_by_schedule.get(snapshot.schedule_id)
        if previous is None or sort_key < previous[0]:
            best_by_schedule[snapshot.schedule_id] = (sort_key, snapshot)
    return tuple(
        item[1]
        for item in sorted(best_by_schedule.values(), key=lambda item: item[0])
    )


def choose_display_snapshot(
    snapshots: Sequence[DisplaySnapshot],
    selected_schedule_id: Optional[str] = None,
):
    ordered = sort_display_snapshots(snapshots)
    if not ordered:
        return None
    for snapshot in ordered:
        if snapshot.schedule_id == selected_schedule_id:
            return snapshot
    return ordered[0]


def restore_display_position(
    saved_position,
    screens: Sequence[DisplayScreen],
    width: int = DISPLAY_WIDTH,
    height: int = DISPLAY_HEIGHT,
):
    if not screens:
        return 0, 0, ""

    saved = saved_position if isinstance(saved_position, dict) else {}
    try:
        saved_x = int(saved["x"])
        saved_y = int(saved["y"])
    except (KeyError, TypeError, ValueError):
        saved_x = None
        saved_y = None

    screen = None
    saved_screen = str(saved.get("screen", ""))
    if saved_screen:
        screen = next(
            (item for item in screens if item.name == saved_screen),
            None,
        )
    if screen is None and saved_x is not None and saved_y is not None:
        screen = next(
            (
                item
                for item in screens
                if item.x <= saved_x < item.x + item.width
                and item.y <= saved_y < item.y + item.height
            ),
            None,
        )
    if screen is not None and (saved_x is None or saved_y is None):
        saved_x = screen.x + max(0, screen.width - width - 16)
        saved_y = screen.y + 16

    if screen is None:
        screen = screens[0]
        saved_x = screen.x + max(0, screen.width - width - 16)
        saved_y = screen.y + 16

    x = max(screen.x, min(saved_x, screen.x + max(0, screen.width - width)))
    y = max(screen.y, min(saved_y, screen.y + max(0, screen.height - height)))
    return x, y, screen.name


def build_display_conditions(
    mode: str,
    scheduled_target: Optional[datetime],
    now: datetime,
    use_24_hour: bool,
    pending: bool,
    idle_enabled: bool,
    idle_seconds: float,
    idle_reliable: bool,
    cpu_enabled: bool,
    cpu_value: Optional[float],
    cpu_reliable: bool,
    network_enabled: bool,
    network_value: Optional[float],
    network_reliable: bool,
):
    conditions = []
    if mode == "time" and scheduled_target is not None:
        conditions.append(
            DisplayCondition(
                "time",
                "HRA",
                "Hora",
                format_clock(scheduled_target, use_24_hour),
            )
        )
    elif mode == "interval" and scheduled_target is not None:
        conditions.append(
            DisplayCondition(
                "interval",
                "INT",
                "Intervalo",
                format_interval_remaining(scheduled_target, now, pending),
            )
        )
    if mode == "idle" or idle_enabled:
        conditions.append(
            DisplayCondition(
                "idle",
                "USR",
                "Usuario",
                format_duration(idle_seconds) if idle_reliable else "--",
            )
        )
    if cpu_enabled:
        conditions.append(
            DisplayCondition(
                "cpu",
                "CPU",
                "CPU",
                format_cpu(cpu_value, cpu_reliable),
            )
        )
    if network_enabled:
        conditions.append(
            DisplayCondition(
                "network",
                "RED",
                "Red",
                format_network(network_value, network_reliable),
            )
        )
    return tuple(conditions)


class ClickableLabel(QLabel):
    clicked = Signal()

    def mouseReleaseEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)


class CompactDisplayWindow(QWidget):
    open_requested = Signal()
    hide_requested = Signal()
    position_changed = Signal(object)

    def __init__(self):
        super().__init__(None)
        self._options = DisplayOptions()
        self._background_alpha = background_alpha_from_transparency(25)
        self._always_on_top = None
        self._snapshots = ()
        self._current_schedule_id = None
        self._manual_schedule_selection = False
        self._condition_by_schedule = {}
        self._drag_offset = None
        self._indicator_buttons = []
        self._position_timer = QTimer(self)
        self._position_timer.setSingleShot(True)
        self._position_timer.setInterval(350)
        self._position_timer.timeout.connect(self._emit_position)

        self.setObjectName("compactDisplay")
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFixedSize(DISPLAY_WIDTH, DISPLAY_HEIGHT)
        self.setWindowTitle("AMP AutoPower Display")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(5, 3, 5, 3)
        layout.setSpacing(1)

        self.title_label = ClickableLabel("AMP AutoPower")
        self.title_label.setAlignment(Qt.AlignCenter)
        self.title_label.clicked.connect(self.cycle_schedule)
        layout.addWidget(self.title_label)

        self.condition_label = ClickableLabel("Sin programaciones")
        self.condition_label.setAlignment(Qt.AlignCenter)
        self.condition_label.setToolTip("Clic para cambiar programacion")
        self.condition_label.clicked.connect(self.cycle_schedule)
        layout.addWidget(self.condition_label)

        self.value_label = QLabel("--")
        self.value_label.setAlignment(Qt.AlignCenter)
        value_font = QFont("monospace")
        value_font.setStyleHint(QFont.Monospace)
        value_font.setBold(True)
        value_font.setPixelSize(25)
        self.value_label.setFont(value_font)
        layout.addWidget(self.value_label, 1)

        self.meta_label = QLabel("")
        self.meta_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.meta_label)

        self.indicator_layout = QHBoxLayout()
        self.indicator_layout.setContentsMargins(0, 0, 0, 0)
        self.indicator_layout.setSpacing(2)
        self.indicator_layout.addStretch()
        layout.addLayout(self.indicator_layout)

        self.apply_options(self._options)

    @property
    def current_schedule_id(self):
        return self._current_schedule_id

    @property
    def current_condition_key(self):
        return self._condition_by_schedule.get(self._current_schedule_id)

    @property
    def background_alpha(self):
        return self._background_alpha

    def apply_options(self, options: DisplayOptions):
        flags_changed = self._always_on_top != options.always_on_top
        was_visible = self.isVisible() if flags_changed else False
        position = self.pos() if flags_changed else None
        self._options = options
        if flags_changed:
            flags = Qt.Tool | Qt.FramelessWindowHint
            if options.always_on_top:
                flags |= Qt.WindowStaysOnTopHint
            self.setWindowFlags(flags)
            self._always_on_top = options.always_on_top
        self._background_alpha = background_alpha_from_transparency(
            options.transparency_percent
        )
        self.setStyleSheet(
            "QLabel { color: #5cff5c; background: transparent; }"
            "QPushButton { color: #42ff42; background: transparent; "
            "border: 1px solid #238823; padding: 0 2px; font: 8px monospace; }"
            "QPushButton:checked { color: #001800; background: #5cff5c; }"
        )
        self.title_label.setVisible(options.show_title)
        self._render()
        self.update()
        if flags_changed and was_visible:
            self.move(position)
            self.show()

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.fillRect(
            self.rect(),
            QColor(0, 0, 0, self._background_alpha),
        )
        painter.setPen(QColor(66, 255, 66))
        painter.drawRect(self.rect().adjusted(0, 0, -1, -1))
        painter.end()

    def set_snapshots(self, snapshots: Sequence[DisplaySnapshot]):
        ordered = sort_display_snapshots(snapshots)
        previous = choose_display_snapshot(ordered, self._current_schedule_id)
        if ordered and previous is not None:
            best = ordered[0]
            if (
                not self._manual_schedule_selection
                or (best.priority == 0 and previous.priority != 0)
            ):
                previous = best
                self._manual_schedule_selection = False
        if previous is None:
            self._manual_schedule_selection = False
        self._snapshots = ordered
        self._current_schedule_id = (
            previous.schedule_id if previous is not None else None
        )
        self._render()

    def cycle_schedule(self):
        if len(self._snapshots) < 2:
            return
        ids = [snapshot.schedule_id for snapshot in self._snapshots]
        try:
            index = ids.index(self._current_schedule_id)
        except ValueError:
            index = -1
        self._current_schedule_id = ids[(index + 1) % len(ids)]
        self._manual_schedule_selection = True
        self._render()

    def select_condition(self, key: str):
        snapshot = choose_display_snapshot(
            self._snapshots,
            self._current_schedule_id,
        )
        if snapshot is None:
            return
        selected = select_condition_key(
            [condition.key for condition in snapshot.conditions],
            self.current_condition_key,
            key,
        )
        self._condition_by_schedule[snapshot.schedule_id] = selected
        self._render()

    def _render(self):
        snapshot = choose_display_snapshot(
            self._snapshots,
            self._current_schedule_id,
        )
        if snapshot is None:
            self.title_label.setText("AMP AutoPower")
            self.condition_label.setText("Sin programaciones")
            self.value_label.setText("--")
            self.meta_label.clear()
            self._set_indicators(())
            return

        keys = [condition.key for condition in snapshot.conditions]
        selected_key = select_condition_key(
            keys,
            self._condition_by_schedule.get(snapshot.schedule_id),
            snapshot.selected_condition,
        )
        self._condition_by_schedule[snapshot.schedule_id] = selected_key
        selected = next(
            (
                condition
                for condition in snapshot.conditions
                if condition.key == selected_key
            ),
            None,
        )
        self.title_label.setText(snapshot.title or "AMP AutoPower")
        self.condition_label.setText(selected.title if selected else "Estado")
        self.value_label.setText(selected.value if selected else "--")
        meta = []
        if self._options.show_action and snapshot.action:
            meta.append(snapshot.action)
        logic = format_logic(snapshot.logic, len(snapshot.conditions))
        if logic:
            meta.append(logic)
        self.meta_label.setText(" | ".join(meta))
        self.meta_label.setVisible(bool(meta))
        self._set_indicators(snapshot.conditions, selected_key)

    def _set_indicators(self, conditions, selected_key=None):
        existing_keys = [
            button.property("conditionKey")
            for button in self._indicator_buttons
        ]
        new_keys = [condition.key for condition in conditions]
        if existing_keys == new_keys:
            for button in self._indicator_buttons:
                button.setChecked(
                    button.property("conditionKey") == selected_key
                )
            return
        while self._indicator_buttons:
            button = self._indicator_buttons.pop()
            self.indicator_layout.removeWidget(button)
            button.deleteLater()
        for condition in conditions:
            button = QPushButton(condition.indicator)
            button.setCheckable(True)
            button.setChecked(condition.key == selected_key)
            button.setProperty("conditionKey", condition.key)
            button.setToolTip(condition.title)
            button.clicked.connect(
                lambda _checked=False, key=condition.key: self.select_condition(key)
            )
            self.indicator_layout.insertWidget(
                self.indicator_layout.count() - 1,
                button,
            )
            self._indicator_buttons.append(button)

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        open_action = menu.addAction("Abrir AMP AutoPower")
        hide_action = menu.addAction("Ocultar display")
        selected = menu.exec(event.globalPos())
        if selected == open_action:
            self.open_requested.emit()
        elif selected == hide_action:
            self.hide_requested.emit()

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            handle = self.windowHandle()
            if handle is not None and handle.startSystemMove():
                self._drag_offset = None
            else:
                self._drag_offset = event.globalPosition().toPoint() - self.pos()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent):
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            self._drag_offset = None
            self._emit_position()
        super().mouseReleaseEvent(event)

    def moveEvent(self, event):
        super().moveEvent(event)
        if self.isVisible():
            self._position_timer.start()

    def _emit_position(self):
        center = self.frameGeometry().center()
        screen = QApplication.screenAt(center) or self.screen()
        self.position_changed.emit(
            {
                "x": self.x(),
                "y": self.y(),
                "screen": screen.name() if screen is not None else "",
            }
        )

    def closeEvent(self, event: QCloseEvent):
        event.ignore()
        self.hide_requested.emit()
