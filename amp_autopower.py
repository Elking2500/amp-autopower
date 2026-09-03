#!/usr/bin/env python3
import argparse
import configparser
import hashlib
import json
import os
import signal
import re
import select
import shlex
import socket
import threading
import time
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, asdict, field, replace
from datetime import datetime, timedelta
from pathlib import Path

from condition_engine import (
    CPUMonitor,
    ConditionContext,
    ConditionEngine,
    NetworkMonitor,
    ScheduledOccurrence,
    schedule_trigger_mode,
)
from compact_display import (
    CompactDisplayWindow,
    DisplayOptions,
    DisplayScreen,
    DisplaySnapshot,
    build_display_conditions,
    display_options_from_config,
    format_clock,
    format_schedule_time,
    format_ui_datetime,
    restore_display_position,
    store_display_options,
)
from PySide6.QtCore import (
    Q_ARG, Q_RETURN_ARG, QLocale, QMetaObject, QObject, QProcess, Qt,
    QTimer, QLockFile, QStandardPaths, QThread, Signal,
)
from PySide6.QtGui import (
    QAction, QColor, QCloseEvent, QIcon, QKeySequence, QPalette,
)
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFileDialog, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QLabel,
    QKeySequenceEdit, QLineEdit, QListWidget, QListWidgetItem, QMainWindow,
    QMessageBox, QPushButton, QProgressDialog, QSpinBox, QSystemTrayIcon,
    QTabWidget, QTimeEdit, QVBoxLayout, QWidget,
)
from PySide6.QtCore import QTime

try:
    from PySide6.QtDBus import (
        QDBusConnection, QDBusInterface, QDBusMessage,
        QDBusServiceWatcher,
    )
except ImportError:
    QDBusConnection = None
    QDBusInterface = None
    QDBusMessage = None
    QDBusServiceWatcher = None

try:
    import evdev
    from evdev import ecodes
except Exception:
    evdev = None
    ecodes = None

APP_NAME = "AMP AutoPower"
APP_ID = "amp-autopower"
APP_VERSION = "1.3.0"
IPC_NAME = "amp-autopower-ipc-v1"
IPC_TIMEOUT_MS = 2000
IPC_MAX_REQUEST_BYTES = 16 * 1024
IPC_MAX_RESPONSE_BYTES = 256 * 1024
CONFIG_DIR = Path.home() / ".config" / APP_ID
CONFIG_FILE = CONFIG_DIR / "config.json"
STATE_FILE = CONFIG_DIR / "state.json"
LOG_DIR = Path.home() / ".local" / "state" / APP_ID
LOG_FILE = LOG_DIR / "amp-autopower.log"
CACHE_DIR = Path.home() / ".cache" / APP_ID
UPDATE_CACHE_DIR = CACHE_DIR / "updates"
CANONICAL_UPDATE_MANIFEST_URL = "https://raw.githubusercontent.com/Elking2500/amp-autopower/main/manifest.json"

WEEKDAYS = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]
ACTIONS = {
    "poweroff": "Apagar",
    "reboot": "Reiniciar",
    "suspend": "Suspender",
    "hibernate": "Hibernar",
    "logout": "Cerrar sesión",
    "lock": "Bloquear sesión",
    "test": "Solo aviso (prueba)",
}


def configure_qt_system_theme(environment=None):
    environment = os.environ if environment is None else environment
    if environment.get("QT_QPA_PLATFORMTHEME"):
        return
    desktop = " ".join(
        str(environment.get(key, ""))
        for key in (
            "XDG_CURRENT_DESKTOP",
            "XDG_SESSION_DESKTOP",
            "DESKTOP_SESSION",
            "KDE_FULL_SESSION",
        )
    ).lower()
    if "kde" in desktop or "plasma" in desktop:
        environment["QT_QPA_PLATFORMTHEME"] = "kde"


def _read_kde_colors(environment=None, path=None):
    environment = os.environ if environment is None else environment
    if path is None:
        config_home = environment.get("XDG_CONFIG_HOME")
        if not config_home:
            home = environment.get("HOME") or str(Path.home())
            config_home = str(Path(home) / ".config")
        path = Path(config_home) / "kdeglobals"
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            parser.read_file(handle)
    except (OSError, configparser.Error):
        return {}
    return {
        section: dict(parser.items(section))
        for section in parser.sections()
        if section.startswith("Colors:")
    }


def _kde_color(groups, group, key):
    raw = groups.get(group, {}).get(key)
    if raw is None:
        return None
    try:
        values = [int(part.strip()) for part in raw.split(",")[:3]]
    except ValueError:
        return None
    if len(values) != 3 or any(value < 0 or value > 255 for value in values):
        return None
    return QColor(*values)


def _color_luminance(color):
    return (
        color.redF() * 0.2126
        + color.greenF() * 0.7152
        + color.blueF() * 0.0722
    )


def palette_is_dark(palette):
    background = palette.color(QPalette.Window)
    foreground = palette.color(QPalette.WindowText)
    return _color_luminance(background) < _color_luminance(foreground)


def kde_colors_are_dark(groups):
    background = _kde_color(
        groups,
        "Colors:Window",
        "BackgroundNormal",
    )
    foreground = _kde_color(
        groups,
        "Colors:Window",
        "ForegroundNormal",
    )
    if background is None or foreground is None:
        return None
    return _color_luminance(background) < _color_luminance(foreground)


def _style_hint_is_dark(app):
    try:
        scheme = app.styleHints().colorScheme()
    except (AttributeError, RuntimeError):
        return None
    if scheme == Qt.ColorScheme.Dark:
        return True
    if scheme == Qt.ColorScheme.Light:
        return False
    return None


def _fallback_palette(base, dark, groups):
    palette = QPalette(base)
    defaults = {
        "window": QColor(45, 50, 70) if dark else QColor(239, 240, 241),
        "window_text": QColor(222, 222, 222) if dark else QColor(35, 38, 41),
        "base": QColor(35, 40, 55) if dark else QColor(255, 255, 255),
        "alternate": QColor(45, 50, 65) if dark else QColor(247, 247, 247),
        "button": QColor(25, 30, 45) if dark else QColor(239, 240, 241),
        "button_text": QColor(250, 250, 250) if dark else QColor(35, 38, 41),
        "highlight": QColor(121, 141, 210) if dark else QColor(61, 174, 233),
        "highlight_text": QColor(255, 255, 255),
        "tooltip": QColor(50, 50, 50) if dark else QColor(255, 255, 220),
        "tooltip_text": QColor(222, 222, 222) if dark else QColor(35, 38, 41),
    }
    values = {
        "window": _kde_color(groups, "Colors:Window", "BackgroundNormal"),
        "window_text": _kde_color(groups, "Colors:Window", "ForegroundNormal"),
        "base": _kde_color(groups, "Colors:View", "BackgroundNormal"),
        "alternate": _kde_color(groups, "Colors:View", "BackgroundAlternate"),
        "button": _kde_color(groups, "Colors:Button", "BackgroundNormal"),
        "button_text": _kde_color(groups, "Colors:Button", "ForegroundNormal"),
        "highlight": _kde_color(groups, "Colors:Selection", "BackgroundNormal"),
        "highlight_text": _kde_color(groups, "Colors:Selection", "ForegroundNormal"),
        "tooltip": _kde_color(groups, "Colors:Tooltip", "BackgroundNormal"),
        "tooltip_text": _kde_color(groups, "Colors:Tooltip", "ForegroundNormal"),
    }
    values = {
        key: value if value is not None else defaults[key]
        for key, value in values.items()
    }
    for role, key in (
        (QPalette.Window, "window"),
        (QPalette.WindowText, "window_text"),
        (QPalette.Base, "base"),
        (QPalette.AlternateBase, "alternate"),
        (QPalette.Text, "window_text"),
        (QPalette.Button, "button"),
        (QPalette.ButtonText, "button_text"),
        (QPalette.Highlight, "highlight"),
        (QPalette.HighlightedText, "highlight_text"),
        (QPalette.ToolTipBase, "tooltip"),
        (QPalette.ToolTipText, "tooltip_text"),
    ):
        palette.setColor(role, values[key])
    return palette


def apply_system_palette_fallback(
    app,
    environment=None,
    kde_colors=None,
):
    groups = (
        _read_kde_colors(environment)
        if kde_colors is None
        else kde_colors
    )
    kde_dark = kde_colors_are_dark(groups)
    palette = app.palette()
    palette_dark = palette_is_dark(palette)
    desired_dark = kde_dark
    if desired_dark is None:
        desired_dark = _style_hint_is_dark(app)
    if desired_dark is None or desired_dark == palette_dark:
        return False
    app.setPalette(_fallback_palette(palette, desired_dark, groups))
    return True


def format_interval(minutes):
    hours, remaining = divmod(max(1, int(minutes)), 60)
    parts = []
    if hours:
        parts.append(f"{hours} h")
    if remaining:
        parts.append(f"{remaining} min")
    return " ".join(parts)


def use_24_hour_format(config):
    return bool(config.get("display_use_24_hour", True))


def format_app_time(config, value, include_seconds=False):
    if isinstance(value, str):
        return format_schedule_time(value, use_24_hour_format(config))
    return format_clock(
        value,
        use_24_hour_format(config),
        include_seconds,
    )


def format_app_datetime(
    config,
    value,
    date_format="%a %d/%m",
    include_seconds=False,
):
    return format_ui_datetime(
        value,
        use_24_hour_format(config),
        date_format,
        include_seconds,
    )


def ensure_dirs():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    UPDATE_CACHE_DIR.mkdir(parents=True, exist_ok=True)


def log(msg: str):
    ensure_dirs()
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(f"[{stamp}] {msg}\n")


def run_cmd(cmd):
    log("Ejecutando: " + " ".join(str(x) for x in cmd))
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


_URLLIB_IPV4_LOCK = threading.Lock()


def urlopen_ipv4(request, timeout=15):
    """Abre una URL por IPv4 sin modificar la configuración IPv6 del sistema."""
    with _URLLIB_IPV4_LOCK:
        original_getaddrinfo = socket.getaddrinfo

        def ipv4_only(host, port, family=0, type=0, proto=0, flags=0):
            return original_getaddrinfo(
                host,
                port,
                socket.AF_INET,
                type,
                proto,
                flags,
            )

        socket.getaddrinfo = ipv4_only
        try:
            return urllib.request.urlopen(request, timeout=timeout)
        finally:
            socket.getaddrinfo = original_getaddrinfo


def version_tuple(value: str):
    nums = re.findall(r"\d+", str(value))[:4]
    return tuple(int(x) for x in nums) + (0,) * (4 - len(nums))


def is_newer_version(candidate: str, current: str = APP_VERSION):
    return version_tuple(candidate) > version_tuple(current)


def package_version(path: Path):
    """Lee VERSION de un paquete sin extraerlo."""
    try:
        with tarfile.open(path, "r:*") as tf:
            members = [m for m in tf.getmembers() if m.isfile() and Path(m.name).name == "VERSION"]
            members.sort(key=lambda m: len(Path(m.name).parts))
            if not members:
                return None
            f = tf.extractfile(members[0])
            if not f:
                return None
            return f.read(64).decode("utf-8", "replace").strip()
    except Exception as e:
        log(f"No se pudo leer versión de {path}: {e}")
        return None


def local_update_candidates():
    dirs = []
    for name in ("Descargas", "Downloads"):
        p = Path.home() / name
        if p.exists() and p not in dirs:
            dirs.append(p)
    if UPDATE_CACHE_DIR.exists():
        dirs.append(UPDATE_CACHE_DIR)

    found = []
    for directory in dirs:
        try:
            for p in directory.glob("AMP-AutoPower*.tar.gz"):
                ver = package_version(p)
                if ver and is_newer_version(ver):
                    found.append({
                        "version": ver,
                        "source": "local",
                        "path": str(p),
                        "notes": f"Paquete encontrado en {p}",
                    })
        except Exception as e:
            log(f"Error buscando actualizaciones locales en {directory}: {e}")
    if not found:
        return None
    return max(found, key=lambda x: version_tuple(x["version"]))


def safe_extract_tar(tf: tarfile.TarFile, target: Path):
    target_resolved = target.resolve()
    for member in tf.getmembers():
        dest = (target / member.name).resolve()
        if target_resolved != dest and target_resolved not in dest.parents:
            raise ValueError(f"Ruta insegura en paquete: {member.name}")
        if member.issym() or member.islnk():
            raise ValueError(f"Enlace no permitido en paquete: {member.name}")
    tf.extractall(target)


@dataclass
class Schedule:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = "Apagado nocturno"
    enabled: bool = True
    use_time: bool = True
    trigger_mode: str = ""
    interval_minutes: int = 60
    time: str = "23:30"
    weekdays: list = field(default_factory=lambda: [0, 1, 2, 3, 4, 5, 6])
    action: str = "poweroff"
    warning_minutes: list = field(default_factory=lambda: [30, 15, 5, 1])
    final_countdown_seconds: int = 60
    condition_logic: str = "AND"
    require_idle: bool = False
    idle_minutes: int = 30
    require_cpu: bool = False
    cpu_comparison: str = "less"
    cpu_threshold: int = 10
    cpu_duration_seconds: int = 300
    cpu_use_average: bool = False
    cpu_average_seconds: int = 60
    require_network: bool = False
    network_interface: str = ""
    network_direction: str = "both"
    network_comparison: str = "less"
    network_threshold: int = 50
    network_unit: str = "KB/s"
    network_duration_seconds: int = 300
    network_use_average: bool = False
    network_average_seconds: int = 60
    close_apps_first: bool = True
    pre_action_enabled: bool = False
    pre_action_command: str = ""
    pre_action_wait: bool = True
    pre_action_timeout_seconds: int = 60
    pre_action_failure_policy: str = "cancel"
    cancel_action_behavior: str = "none"
    cancel_action_command: str = ""


def schedule_to_dict(schedule):
    data = asdict(schedule)
    data.pop("condition_logic", None)
    for key in (
        "pre_action_enabled",
        "pre_action_command",
        "pre_action_wait",
        "pre_action_timeout_seconds",
        "pre_action_failure_policy",
        "cancel_action_behavior",
        "cancel_action_command",
    ):
        data.pop(key, None)
    if schedule_trigger_mode(schedule) != "interval":
        data.pop("trigger_mode", None)
        data.pop("interval_minutes", None)
    if not schedule.require_cpu:
        for key in (
            "require_cpu",
            "cpu_comparison",
            "cpu_threshold",
            "cpu_duration_seconds",
            "cpu_use_average",
            "cpu_average_seconds",
        ):
            data.pop(key, None)
    if not schedule.require_network:
        for key in (
            "require_network",
            "network_interface",
            "network_direction",
            "network_comparison",
            "network_threshold",
            "network_unit",
            "network_duration_seconds",
            "network_use_average",
            "network_average_seconds",
        ):
            data.pop(key, None)
    return data


DEFAULT_CONFIG = {
    "start_minimized": True,
    "close_to_tray": True,
    "tray_visible": True,
    "global_hotkey": "",
    "notifications": True,
    "sound": True,
    "input_monitor_enabled": True,
    "fullscreen_overlay": True,
    "overlay_all_screens": True,
    "overlay_all_schedule_warnings": True,
    "auto_check_updates": True,
    "update_interval_hours": 48,
    "update_manifest_url": CANONICAL_UPDATE_MANIFEST_URL,
    "notify_updates": True,
    "cpu_settings": {},
    "network_settings": {},
    "condition_logic_settings": {},
    "action_settings": {},
    "display_enabled": False,
    "display_always_on_top": True,
    "display_show_title": True,
    "display_show_action": True,
    "display_transparency_percent": 25,
    "display_use_24_hour": True,
    "display_position": {},
    "schedules": [schedule_to_dict(Schedule())],
}

DEFAULT_STATE = {
    "last_runs": {},
    "snoozes": {},
    "skipped_targets": {},
    "pending_occurrences": {},
    "completed_intervals": {},
    "last_update_check": None,
    "available_update": None,
}


def schedules_from_config(config):
    schedules = []
    cpu_settings = config.get("cpu_settings", {})
    network_settings = config.get("network_settings", {})
    logic_settings = config.get("condition_logic_settings", {})
    action_settings = config.get("action_settings", {})
    for raw in config.get("schedules", []):
        try:
            data = dict(raw)
            schedule_id = data.get("id")
            for settings in (cpu_settings, network_settings, action_settings):
                preset = settings.get(schedule_id, {})
                if isinstance(preset, dict):
                    for key, value in preset.items():
                        data.setdefault(key, value)
            data.setdefault(
                "condition_logic",
                logic_settings.get(schedule_id, "AND"),
            )
            schedules.append(Schedule(**data))
        except Exception as exc:
            log(f"Programación inválida ignorada: {exc}")
    return schedules


def _pending_occurrence_from_state(schedule, state):
    raw = state.get("pending_occurrences", {}).get(schedule.id)
    if not raw:
        return None
    try:
        return ScheduledOccurrence.from_state(schedule.id, raw)
    except Exception:
        return None


def _pending_action_time_from_state(schedule, occurrence, state, now):
    internal_due = None
    if occurrence.next_check_at:
        if occurrence.next_check_at <= occurrence.scheduled_target:
            internal_due = occurrence.scheduled_target
        else:
            internal_due = occurrence.next_check_at + timedelta(
                seconds=int(schedule.final_countdown_seconds)
            )

    snooze_iso = state.get("snoozes", {}).get(schedule.id)
    if snooze_iso:
        try:
            snooze_due = datetime.fromisoformat(snooze_iso)
            return max(snooze_due, internal_due or snooze_due)
        except (TypeError, ValueError):
            pass
    if internal_due:
        return internal_due
    if occurrence.scheduled_target > now:
        return occurrence.scheduled_target
    return now + timedelta(seconds=int(schedule.final_countdown_seconds))


def schedule_next_target(schedule, state, now=None):
    if not schedule.enabled:
        return None
    now = now or datetime.now()
    occurrence = _pending_occurrence_from_state(schedule, state)
    if occurrence is not None:
        return _pending_action_time_from_state(schedule, occurrence, state, now)
    if schedule_trigger_mode(schedule) != "time":
        return None

    snooze_iso = state.get("snoozes", {}).get(schedule.id)
    if snooze_iso:
        try:
            snooze_due = datetime.fromisoformat(snooze_iso)
            if snooze_due >= now:
                return snooze_due
        except (TypeError, ValueError):
            pass
    try:
        hour, minute = map(int, schedule.time.split(":"))
    except (AttributeError, TypeError, ValueError):
        return None
    skipped_iso = state.get("skipped_targets", {}).get(schedule.id)
    last_run_iso = state.get("last_runs", {}).get(schedule.id)
    for delta in range(8):
        day = now.date() + timedelta(days=delta)
        candidate = datetime.combine(day, datetime.min.time()).replace(
            hour=hour,
            minute=minute,
        )
        if candidate.weekday() not in schedule.weekdays or candidate < now:
            continue
        if candidate.isoformat() in (skipped_iso, last_run_iso):
            continue
        return candidate
    return None


def _cli_text(value):
    return " ".join(str(value).split())


def _cli_mode_text(schedule):
    mode = schedule_trigger_mode(schedule)
    if mode == "time":
        return f"hora {schedule.time}"
    if mode == "interval":
        return f"intervalo {format_interval(schedule.interval_minutes)}"
    return f"inactividad {schedule.idle_minutes} min"


def format_schedules_cli(schedules, state, now=None):
    if not schedules:
        return "No hay programaciones."
    now = now or datetime.now()
    lines = []
    for schedule in schedules:
        target = schedule_next_target(schedule, state, now)
        target_text = target.isoformat(timespec="minutes") if target else "-"
        lines.append(
            f"ID={schedule.id} | "
            f"Estado={'activa' if schedule.enabled else 'inactiva'} | "
            f"Nombre={_cli_text(schedule.name)} | "
            f"Acción={ACTIONS.get(schedule.action, schedule.action)} | "
            f"Modo={_cli_mode_text(schedule)} | "
            f"Próximo={target_text}"
        )
    return "\n".join(lines)


def format_status_cli(
    config,
    state,
    schedules,
    ipc_available,
    display_visible=False,
    tray_visible=False,
    now=None,
):
    now = now or datetime.now()
    candidates = []
    for schedule in schedules:
        target = schedule_next_target(schedule, state, now)
        if target is not None:
            candidates.append((target, schedule))
    if candidates:
        target, schedule = min(candidates, key=lambda item: item[0])
        next_action = (
            f"{ACTIONS.get(schedule.action, schedule.action)} | "
            f"{_cli_text(schedule.name)} | "
            f"{target.isoformat(timespec='minutes')}"
        )
    else:
        next_action = "ninguna"
    active_count = sum(1 for schedule in schedules if schedule.enabled)
    return "\n".join(
        (
            f"Versión: {APP_VERSION}",
            f"IPC accesible: {'sí' if ipc_available else 'no'}",
            f"Programaciones: {len(schedules)} total, {active_count} activas",
            f"Próxima acción: {next_action}",
            f"Display compacto: {'visible' if display_visible else 'oculto'}",
            f"Bandeja: {'visible' if tray_visible else 'oculta'}",
        )
    )


def resolve_schedule(schedules, selector):
    selector = str(selector or "").strip()
    if not selector:
        return None, "Debes indicar un ID o nombre.", 2
    by_id = next((schedule for schedule in schedules if schedule.id == selector), None)
    if by_id is not None:
        return by_id, "", 0
    by_name = [schedule for schedule in schedules if schedule.name == selector]
    if len(by_name) == 1:
        return by_name[0], "", 0
    if len(by_name) > 1:
        ids = ", ".join(schedule.id for schedule in by_name)
        return None, f"Nombre ambiguo «{selector}». Usa un ID: {ids}", 2
    return None, f"No existe una programación con ID o nombre «{selector}».", 1


def build_cli_parser():
    parser = argparse.ArgumentParser(
        prog="amp-autopower",
        description="Administra la instancia local de AMP AutoPower.",
    )
    commands = parser.add_mutually_exclusive_group()
    commands.add_argument("--show", action="store_true", help="Muestra la ventana.")
    commands.add_argument("--hide", action="store_true", help="Oculta la ventana.")
    commands.add_argument(
        "--toggle",
        action="store_true",
        help="Alterna la visibilidad de la ventana.",
    )
    commands.add_argument(
        "--status",
        action="store_true",
        help="Muestra un resumen del estado.",
    )
    commands.add_argument(
        "--list-schedules",
        action="store_true",
        help="Lista las programaciones.",
    )
    commands.add_argument(
        "--enable",
        metavar="ID_O_NOMBRE",
        help="Activa una programación por ID o nombre exacto.",
    )
    commands.add_argument(
        "--disable",
        metavar="ID_O_NOMBRE",
        help="Desactiva una programación por ID o nombre exacto.",
    )
    commands.add_argument(
        "--check-update",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=APP_VERSION,
        help="Muestra la versión y termina.",
    )
    return parser


def cli_command_from_args(args):
    for option in ("show", "hide", "toggle", "status", "list_schedules"):
        if getattr(args, option):
            return option.replace("_", "-"), None
    if args.enable is not None:
        return "enable", args.enable
    if args.disable is not None:
        return "disable", args.disable
    if args.check_update:
        return "check-update", None
    return None, None


def load_json(path: Path, default):
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(default, dict) and isinstance(data, dict):
                merged = default.copy()
                merged.update(data)
                # Migración 1.1.1: versiones anteriores guardaban una URL vacía.
                # Si sigue vacía, conectar automáticamente al canal oficial.
                if path == CONFIG_FILE and not str(merged.get("update_manifest_url", "")).strip():
                    merged["update_manifest_url"] = CANONICAL_UPDATE_MANIFEST_URL
                return merged
            return data
    except Exception as e:
        log(f"Error leyendo {path}: {e}")
    return default.copy() if isinstance(default, dict) else default


def save_json(path: Path, data):
    ensure_dirs()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


class GlobalShortcutManager(QObject):
    activated = Signal()
    status_changed = Signal(bool, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.last_error = ""
        self._registered = False
        self._root = None
        self._component = None
        self._shortcut_text = ""
        self._action_id = [
            APP_ID,
            "show-hide-window",
            APP_NAME,
            "Mostrar/ocultar ventana principal",
        ]
        self._service_watcher = None
        if QDBusServiceWatcher is not None:
            watch_mode = (
                QDBusServiceWatcher.WatchForRegistration
                | QDBusServiceWatcher.WatchForUnregistration
            )
            self._service_watcher = QDBusServiceWatcher(
                "org.kde.kglobalaccel",
                QDBusConnection.sessionBus(),
                watch_mode,
                self,
            )
            self._service_watcher.serviceRegistered.connect(
                self._on_service_registered
            )
            self._service_watcher.serviceUnregistered.connect(
                self._on_service_unregistered
            )

    @property
    def is_registered(self):
        return self._registered

    def _message_error(self, reply):
        if reply.type() == QDBusMessage.ErrorMessage:
            return reply.errorMessage() or "Error DBus desconocido."
        return ""

    def set_shortcut(self, shortcut_text):
        self._deactivate()
        self.last_error = ""
        shortcut_text = str(shortcut_text or "").strip()
        self._shortcut_text = shortcut_text
        if not shortcut_text:
            self.status_changed.emit(False, "")
            return True
        if QDBusInterface is None:
            return self._fail("QtDBus no está disponible.")

        sequence = QKeySequence.fromString(
            shortcut_text,
            QKeySequence.PortableText,
        )
        if sequence.isEmpty() or sequence.count() != 1:
            return self._fail("El atajo global no es válido.")

        try:
            bus = QDBusConnection.sessionBus()
            root = QDBusInterface(
                "org.kde.kglobalaccel",
                "/kglobalaccel",
                "org.kde.KGlobalAccel",
                bus,
            )
            if not root.isValid():
                return self._fail(
                    "El servicio de atajos globales de KDE no está disponible."
                )

            reply = root.call("doRegister", self._action_id)
            error = self._message_error(reply)
            if error:
                return self._fail(error)
            self._root = root
            self._registered = True

            key = sequence[0].toCombined()
            assigned = QMetaObject.invokeMethod(
                root,
                "setShortcut",
                Qt.DirectConnection,
                Q_RETURN_ARG("QList<int>"),
                Q_ARG("QStringList", self._action_id),
                Q_ARG("QList<int>", [key]),
                Q_ARG("uint", 6),
            )
            if not assigned:
                return self._fail(
                    "El atajo está ocupado o KDE rechazó su registro."
                )

            reply = root.call("getComponent", APP_ID)
            error = self._message_error(reply)
            if error or not reply.arguments():
                return self._fail(
                    error or "KDE no devolvió el componente."
                )
            component_path = reply.arguments()[0]
            if hasattr(component_path, "path"):
                component_path = component_path.path()
            component = QDBusInterface(
                "org.kde.kglobalaccel",
                str(component_path),
                "org.kde.kglobalaccel.Component",
                bus,
            )
            if not component.isValid():
                return self._fail(
                    "No se pudo escuchar el atajo registrado."
                )
            component.globalShortcutPressed.connect(self._on_pressed)
            self._component = component
            self.status_changed.emit(True, "")
            return True
        except Exception as exc:
            return self._fail(str(exc))

    def disable(self):
        self._shortcut_text = ""
        self._deactivate()
        self.status_changed.emit(False, "")

    def _deactivate(self):
        if self._registered and self._root is not None:
            try:
                self._root.call("setInactive", self._action_id)
            except Exception:
                pass
        if self._component is not None:
            self._component.deleteLater()
        self._component = None
        self._root = None
        self._registered = False

    def _fail(self, error):
        self.last_error = error
        self._deactivate()
        self.status_changed.emit(False, error)
        return False

    def _on_service_unregistered(self, _service):
        if not self._shortcut_text:
            return
        self._registered = False
        self._root = None
        if self._component is not None:
            self._component.deleteLater()
            self._component = None
        self.status_changed.emit(
            False,
            "El servicio de atajos de KDE se reinició.",
        )

    def _on_service_registered(self, _service):
        if self._shortcut_text:
            shortcut = self._shortcut_text
            QTimer.singleShot(
                500,
                lambda: self._retry_shortcut(shortcut),
            )

    def _retry_shortcut(self, shortcut):
        if self._shortcut_text == shortcut:
            self.set_shortcut(shortcut)

    def _on_pressed(self, component, action, _timestamp):
        if component == APP_ID and action == self._action_id[1]:
            self.activated.emit()


class UpdateCheckThread(QThread):
    result_ready = Signal(object)

    def __init__(self, manifest_url: str):
        super().__init__()
        self.manifest_url = (manifest_url or "").strip()

    def run(self):
        result = {
            "available": None,
            "error": None,
            "checked_at": datetime.now().isoformat(timespec="seconds"),
        }
        try:
            candidates = []
            local = local_update_candidates()
            if local:
                candidates.append(local)

            if self.manifest_url:
                req = urllib.request.Request(
                    self.manifest_url,
                    headers={"User-Agent": f"AMP-AutoPower/{APP_VERSION}"},
                )
                with urlopen_ipv4(req, timeout=15) as r:
                    raw = r.read(256 * 1024)
                manifest = json.loads(raw.decode("utf-8"))
                version = str(manifest.get("version", "")).strip()
                package_url = str(manifest.get("package_url", "")).strip()
                sha256 = str(manifest.get("sha256", "")).strip().lower()
                if version and package_url and is_newer_version(version):
                    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
                        raise ValueError("El canal remoto no publicó un SHA-256 válido; por seguridad se ignoró la actualización.")
                    candidates.append({
                        "version": version,
                        "source": "remote",
                        "package_url": urllib.parse.urljoin(self.manifest_url, package_url),
                        "sha256": sha256,
                        "notes": str(manifest.get("notes", "")).strip(),
                    })

            if candidates:
                result["available"] = max(candidates, key=lambda x: version_tuple(x["version"]))
        except Exception as e:
            result["error"] = str(e)
        self.result_ready.emit(result)


class DownloadThread(QThread):
    progress = Signal(int)
    finished_download = Signal(object)

    def __init__(self, info: dict):
        super().__init__()
        self.info = info

    def run(self):
        try:
            url = self.info["package_url"]
            version = self.info["version"]
            out = UPDATE_CACHE_DIR / f"AMP-AutoPower-CachyOS-v{version}.tar.gz"
            req = urllib.request.Request(url, headers={"User-Agent": f"AMP-AutoPower/{APP_VERSION}"})
            with urlopen_ipv4(req, timeout=30) as r, out.open("wb") as f:
                total = int(r.headers.get("Content-Length") or 0)
                done = 0
                while True:
                    if self.isInterruptionRequested():
                        out.unlink(missing_ok=True)
                        raise RuntimeError("Descarga cancelada por el usuario.")
                    chunk = r.read(256 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if total > 0:
                        self.progress.emit(min(100, int(done * 100 / total)))
            expected = self.info.get("sha256", "")
            if expected:
                h = hashlib.sha256()
                with out.open("rb") as f:
                    for chunk in iter(lambda: f.read(1024 * 1024), b""):
                        h.update(chunk)
                actual = h.hexdigest().lower()
                if actual != expected:
                    out.unlink(missing_ok=True)
                    raise ValueError("La suma SHA-256 del paquete no coincide. Se canceló la actualización.")
            ver = package_version(out)
            if ver != version:
                out.unlink(missing_ok=True)
                raise ValueError(f"El paquete descargado declara versión {ver or 'desconocida'}, no {version}.")
            self.finished_download.emit({"ok": True, "path": str(out)})
        except Exception as e:
            self.finished_download.emit({"ok": False, "error": str(e)})


class InputActivityMonitor(QThread):
    activity = Signal(str)
    status = Signal(object)

    EXCLUDED_NAME_PARTS = (
        "power button", "sleep button", "lid switch", "video bus",
        "pc speaker", "hda", "hd-audio", "acpi", "gpio keys",
        "motion sensor", "motion sensors", "accelerometer", "gyroscope", "gyro",
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self._devices = {}
        self._axis_state = {}
        self._last_status = None

    def _looks_like_user_input(self, dev):
        name = (dev.name or "").lower()
        if any(x in name for x in self.EXCLUDED_NAME_PARTS):
            return False
        try:
            caps = dev.capabilities(absinfo=False)
        except Exception:
            return False
        return any(t in caps for t in (ecodes.EV_KEY, ecodes.EV_REL, ecodes.EV_ABS))

    def _report_status(self, denied=0, total=0):
        names = sorted({(d.name or Path(d.path).name) for d in self._devices.values()})
        data = {
            "backend": "evdev", "available": evdev is not None,
            "accessible": len(self._devices), "denied": denied,
            "total": total, "devices": names,
        }
        if data != self._last_status:
            self._last_status = data
            self.status.emit(data)

    def _rescan(self):
        if evdev is None:
            self._report_status()
            return
        try:
            paths = set(evdev.list_devices())
        except Exception:
            paths = set()
        for path in list(self._devices):
            if path not in paths:
                try: self._devices[path].close()
                except Exception: pass
                self._devices.pop(path, None)
        denied = 0
        for path in sorted(paths):
            if path in self._devices:
                continue
            try:
                dev = evdev.InputDevice(path)
                if not self._looks_like_user_input(dev):
                    dev.close(); continue
                os.set_blocking(dev.fd, False)
                self._devices[path] = dev
            except PermissionError:
                denied += 1
            except Exception:
                continue
        self._report_status(denied=denied, total=len(paths))

    def _abs_event_is_activity(self, dev, event):
        key = (dev.path, event.code)
        previous = self._axis_state.get(key)
        self._axis_state[key] = event.value
        if previous is None:
            return False
        try:
            info = dev.absinfo(event.code)
            span = max(1, int(info.max) - int(info.min))
            threshold = max(2, int(span * 0.025))
        except Exception:
            threshold = 4
        return abs(int(event.value) - int(previous)) >= threshold

    def _is_activity(self, dev, event):
        if event.type == ecodes.EV_KEY:
            return event.value == 1
        if event.type == ecodes.EV_REL:
            return event.value != 0
        if event.type == ecodes.EV_ABS:
            return self._abs_event_is_activity(dev, event)
        return False

    def run(self):
        if evdev is None:
            self._report_status(); return
        last_scan = 0.0
        while not self.isInterruptionRequested():
            now = time.monotonic()
            if now - last_scan >= 4.0:
                self._rescan(); last_scan = now
            devices = list(self._devices.values())
            if not devices:
                self.msleep(500); continue
            try:
                readable, _, _ = select.select(devices, [], [], 0.75)
            except (OSError, ValueError):
                self._rescan(); self.msleep(200); continue
            for dev in readable:
                try:
                    for event in dev.read():
                        if self._is_activity(dev, event):
                            self.activity.emit(dev.name or Path(dev.path).name)
                except BlockingIOError:
                    pass
                except OSError:
                    try: dev.close()
                    except Exception: pass
                    self._devices.pop(dev.path, None)
        for dev in list(self._devices.values()):
            try: dev.close()
            except Exception: pass
        self._devices.clear()


def _overlay_flags():
    flags = Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
    if QApplication.platformName().lower() == "xcb":
        flags |= Qt.X11BypassWindowManagerHint
    return flags


class WarningBanner(QWidget):
    closed = Signal(object)
    def __init__(self, screen, title, body, lifetime_ms=12000):
        super().__init__(None, _overlay_flags())
        self.screen = screen
        self.setWindowTitle("AMP AutoPower — aviso")
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        root = QVBoxLayout(self); root.setContentsMargins(18,18,18,18)
        card = QWidget()
        card.setStyleSheet("QWidget{background:rgba(20,20,20,235);border:2px solid white;border-radius:14px;color:white;} QLabel{border:none;background:transparent;color:white;}")
        lay = QVBoxLayout(card)
        t = QLabel(title); t.setAlignment(Qt.AlignCenter); t.setStyleSheet("font-size:23px;font-weight:800;")
        b = QLabel(body); b.setWordWrap(True); b.setAlignment(Qt.AlignCenter); b.setStyleSheet("font-size:16px;")
        lay.addWidget(t); lay.addWidget(b); root.addWidget(card)
        geo = screen.availableGeometry(); width = min(850, max(520, int(geo.width()*0.55)))
        self.resize(width,150); self.move(geo.x()+(geo.width()-width)//2, geo.y()+max(18,int(geo.height()*0.035)))
        self.keep_above = QTimer(self); self.keep_above.timeout.connect(self.raise_); self.keep_above.start(350)
        QTimer.singleShot(lifetime_ms, self.close)
    def closeEvent(self, event):
        self.keep_above.stop(); self.closed.emit(self); super().closeEvent(event)


class OverlayPage(QWidget):
    action_requested = Signal(str)
    def __init__(self, screen, schedule, remaining, use_24_hour=True):
        super().__init__(None, _overlay_flags())
        self.screen = screen; self.schedule = schedule
        self.setWindowTitle("AMP AutoPower — EMERGENCIA")
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setStyleSheet("background:rgba(0,0,0,190);color:white;")
        outer = QVBoxLayout(self); outer.setContentsMargins(40,40,40,40); outer.addStretch()
        card = QWidget(); card.setMaximumWidth(860)
        card.setStyleSheet("QWidget{background:rgba(18,18,18,245);border:3px solid white;border-radius:18px;color:white;} QLabel{border:none;background:transparent;color:white;} QPushButton{font-size:17px;padding:14px 18px;border-radius:10px;border:1px solid #aaa;background:#333;color:white;} QPushButton:hover{background:#555;}")
        lay = QVBoxLayout(card); lay.setContentsMargins(28,28,28,28)
        self.title = QLabel(); self.title.setAlignment(Qt.AlignCenter); self.title.setStyleSheet("font-size:34px;font-weight:900;")
        lay.addWidget(self.title)
        mode = schedule_trigger_mode(schedule)
        if mode == "time":
            trigger_text = (
                "Hora programada: "
                f"<b>{format_schedule_time(schedule.time, use_24_hour)}</b>"
            )
        elif mode == "interval":
            trigger_text = (
                f"Intervalo: <b>{format_interval(schedule.interval_minutes)}</b>"
            )
        else:
            trigger_text = f"Activada tras <b>{schedule.idle_minutes} min de inactividad</b>"
        desc = QLabel(
            f"Acción programada: <b>{ACTIONS.get(schedule.action, schedule.action)}</b><br>"
            f"{trigger_text}<br><br>"
            "Este aviso está diseñado para mostrarse sobre aplicaciones y juegos a pantalla completa."
        )
        desc.setAlignment(Qt.AlignCenter); desc.setWordWrap(True); desc.setStyleSheet("font-size:18px;"); lay.addWidget(desc)
        row = QHBoxLayout()
        for text, action in (("Cancelar esta vez","cancel"),("Posponer 10 min","snooze10"),("Posponer 30 min","snooze30")):
            btn = QPushButton(text); btn.clicked.connect(lambda _=False, a=action: self.action_requested.emit(a)); row.addWidget(btn)
        lay.addLayout(row)
        center = QHBoxLayout(); center.addStretch(); center.addWidget(card); center.addStretch(); outer.addLayout(center); outer.addStretch()
        self.update_remaining(remaining)
    def update_remaining(self, remaining):
        self.title.setText(f"{ACTIONS.get(self.schedule.action, self.schedule.action).upper()} EN {remaining} SEGUNDOS")
    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.action_requested.emit("cancel"); return
        super().keyPressEvent(event)


class CountdownDialog(QDialog):
    def __init__(self, parent, schedule: Schedule, seconds: int):
        super().__init__(parent)
        self.schedule = schedule; self.remaining = seconds; self.result_action = "execute"; self.pages = []
        self.cancelled_by_user = False
        self.cancel_command_executed = False
        self._finished = False
        self.setWindowTitle(f"{APP_NAME} — acción inminente"); self.setAttribute(Qt.WA_DontShowOnScreen, True)
        self.timer = QTimer(self); self.timer.timeout.connect(self.tick); self.timer.start(1000)
        self.keep_above = QTimer(self); self.keep_above.timeout.connect(self._raise_pages); self.keep_above.start(250)
    def _make_pages(self):
        if self.pages: return
        parent = self.parent(); all_screens = bool(getattr(parent,"config",{}).get("overlay_all_screens",True))
        use_24_hour = use_24_hour_format(getattr(parent, "config", {}))
        screens = QApplication.screens() if all_screens else [QApplication.primaryScreen()]
        for screen in screens:
            if screen is None: continue
            page = OverlayPage(screen,self.schedule,self.remaining,use_24_hour); page.action_requested.connect(self._handle_user_action); page.setGeometry(screen.geometry()); self.pages.append(page)
    def show(self):
        self._make_pages()
        for page in self.pages:
            page.setGeometry(page.screen.geometry()); page.show(); page.showFullScreen(); page.raise_()
        if self.pages: self.pages[0].activateWindow()
        super().show()
    def _raise_pages(self):
        for page in self.pages:
            if page.isVisible(): page.raise_()
    def tick(self):
        self.remaining -= 1
        if self.remaining <= 0:
            self.timer.stop(); self.finish("execute"); return
        for page in self.pages: page.update_remaining(self.remaining)
    def _handle_user_action(self, action):
        self.finish(action, user_requested=True)
    def finish(self, action, user_requested=False):
        if self._finished:
            return
        self._finished = True
        self.cancelled_by_user = bool(
            user_requested and action == "cancel"
        )
        self.result_action = action; self.timer.stop(); self.keep_above.stop()
        for page in self.pages: page.hide(); page.close()
        self.pages.clear(); self.done(QDialog.Accepted)
    def closeEvent(self, event):
        if self.timer.isActive(): self.result_action = "cancel"; self.timer.stop()
        self.keep_above.stop()
        for page in self.pages: page.close()
        self.pages.clear(); event.accept()


class ScheduleEditor(QDialog):
    def __init__(self, parent=None, schedule=None, use_24_hour=None):
        super().__init__(parent)
        self.setWindowTitle("Editar programación")
        self.resize(720, 610)
        self.setMinimumSize(660, 520)
        self.original = schedule
        s = schedule or Schedule()

        outer = QVBoxLayout(self)

        self.name = QLineEdit(s.name)

        self.enabled = QCheckBox("Activa")
        self.enabled.setChecked(s.enabled)

        self.mode = QComboBox()
        self.mode.addItem("Hora programada", "time")
        self.mode.addItem("Intervalo", "interval")
        self.mode.addItem("Solo inactividad", "idle")
        mode_index = self.mode.findData(schedule_trigger_mode(s))
        self.mode.setCurrentIndex(max(0, mode_index))

        self.time = QTimeEdit(QTime.fromString(s.time, "HH:mm"))
        if use_24_hour is None:
            parent_config = getattr(parent, "config", {})
            use_24_hour = parent_config.get("display_use_24_hour", True)
        if not use_24_hour:
            self.time.setLocale(QLocale(QLocale.English, QLocale.UnitedStates))
        self.time.setDisplayFormat("HH:mm" if use_24_hour else "h:mm AP")

        interval_minutes = max(1, int(getattr(s, "interval_minutes", 60)))
        interval_hours, interval_remainder = divmod(interval_minutes, 60)
        self.interval_hours = QSpinBox()
        self.interval_hours.setRange(0, 168)
        self.interval_hours.setSuffix(" h")
        self.interval_hours.setValue(interval_hours)
        self.interval_minutes = QSpinBox()
        self.interval_minutes.setRange(0, 59)
        self.interval_minutes.setSuffix(" min")
        self.interval_minutes.setValue(interval_remainder)
        self.interval_hours.valueChanged.connect(self._ensure_interval_duration)
        self.interval_minutes.valueChanged.connect(self._ensure_interval_duration)
        interval_row = QHBoxLayout()
        interval_row.addWidget(self.interval_hours)
        interval_row.addWidget(self.interval_minutes)

        self.action = QComboBox()
        for key, text in ACTIONS.items():
            self.action.addItem(text, key)
        idx = self.action.findData(s.action)
        self.action.setCurrentIndex(max(0, idx))

        self.countdown = QSpinBox()
        self.countdown.setRange(15, 600)
        self.countdown.setSuffix(" s")
        self.countdown.setValue(s.final_countdown_seconds)

        self.condition_logic = QComboBox()
        self.condition_logic.addItem("Todas deben cumplirse (AND)", "AND")
        self.condition_logic.addItem("Cualquiera puede cumplirse (OR)", "OR")
        logic_index = self.condition_logic.findData(
            str(getattr(s, "condition_logic", "AND")).upper()
        )
        self.condition_logic.setCurrentIndex(max(0, logic_index))
        self.condition_logic_label = QLabel("Combinar condiciones:")

        self.require_idle = QCheckBox("Solo ejecutar cuando no haya actividad")
        self.require_idle.setChecked(
            getattr(s, "require_idle", False)
            or schedule_trigger_mode(s) == "idle"
        )

        self.idle_minutes = QSpinBox()
        self.idle_minutes.setRange(1, 720)
        self.idle_minutes.setSuffix(" min")
        self.idle_minutes.setValue(
            max(1, int(getattr(s, "idle_minutes", 30)))
        )

        self.require_cpu = QCheckBox("Usar condición CPU")
        self.require_cpu.setChecked(getattr(s, "require_cpu", False))

        self.cpu_comparison = QComboBox()
        self.cpu_comparison.addItem("Menor que", "less")
        self.cpu_comparison.addItem("Mayor que", "greater")
        cpu_comparison_index = self.cpu_comparison.findData(
            getattr(s, "cpu_comparison", "less")
        )
        self.cpu_comparison.setCurrentIndex(max(0, cpu_comparison_index))

        self.cpu_threshold = QSpinBox()
        self.cpu_threshold.setRange(0, 100)
        self.cpu_threshold.setSuffix(" %")
        self.cpu_threshold.setValue(
            min(100, max(0, int(getattr(s, "cpu_threshold", 10))))
        )

        self.cpu_duration = QSpinBox()
        self.cpu_duration.setRange(1, 86400)
        self.cpu_duration.setSuffix(" s")
        self.cpu_duration.setValue(
            max(1, int(getattr(s, "cpu_duration_seconds", 300)))
        )

        self.cpu_use_average = QCheckBox("Usar promedio móvil")
        self.cpu_use_average.setChecked(
            getattr(s, "cpu_use_average", False)
        )

        self.cpu_average = QSpinBox()
        self.cpu_average.setRange(1, 3600)
        self.cpu_average.setSuffix(" s")
        self.cpu_average.setValue(
            max(1, int(getattr(s, "cpu_average_seconds", 60)))
        )

        self.require_network = QCheckBox("Usar condición de red")
        self.require_network.setChecked(
            getattr(s, "require_network", False)
        )

        self.network_interface = QComboBox()
        configured_interface = str(getattr(s, "network_interface", ""))
        monitor = getattr(parent, "network_monitor", None)
        interfaces = (
            monitor.available_interfaces()
            if monitor is not None
            else NetworkMonitor.discover_interfaces()
        )
        if configured_interface and configured_interface not in interfaces:
            self.network_interface.addItem(
                f"{configured_interface} (no disponible)",
                configured_interface,
            )
        for interface in interfaces:
            self.network_interface.addItem(interface, interface)
        if self.network_interface.count() == 0:
            self.network_interface.addItem("Sin interfaces disponibles", "")
        network_interface_index = self.network_interface.findData(
            configured_interface
        )
        if network_interface_index >= 0:
            self.network_interface.setCurrentIndex(network_interface_index)

        self.network_direction = QComboBox()
        self.network_direction.addItem("Entrada (RX)", "rx")
        self.network_direction.addItem("Salida (TX)", "tx")
        self.network_direction.addItem("Ambas (RX + TX)", "both")
        network_direction_index = self.network_direction.findData(
            getattr(s, "network_direction", "both")
        )
        self.network_direction.setCurrentIndex(max(0, network_direction_index))

        self.network_comparison = QComboBox()
        self.network_comparison.addItem("Menor que", "less")
        self.network_comparison.addItem("Mayor que", "greater")
        network_comparison_index = self.network_comparison.findData(
            getattr(s, "network_comparison", "less")
        )
        self.network_comparison.setCurrentIndex(
            max(0, network_comparison_index)
        )

        self.network_threshold = QSpinBox()
        self.network_threshold.setRange(0, 1000000)
        self.network_threshold.setValue(
            max(0, int(getattr(s, "network_threshold", 50)))
        )

        self.network_unit = QComboBox()
        self.network_unit.addItem("KB/s", "KB/s")
        self.network_unit.addItem("MB/s", "MB/s")
        network_unit_index = self.network_unit.findData(
            getattr(s, "network_unit", "KB/s")
        )
        self.network_unit.setCurrentIndex(max(0, network_unit_index))
        network_threshold_row = QHBoxLayout()
        network_threshold_row.addWidget(self.network_threshold)
        network_threshold_row.addWidget(self.network_unit)

        self.network_duration = QSpinBox()
        self.network_duration.setRange(1, 86400)
        self.network_duration.setSuffix(" s")
        self.network_duration.setValue(
            max(1, int(getattr(s, "network_duration_seconds", 300)))
        )

        self.network_use_average = QCheckBox("Usar promedio móvil")
        self.network_use_average.setChecked(
            getattr(s, "network_use_average", False)
        )

        self.network_average = QSpinBox()
        self.network_average.setRange(2, 3600)
        self.network_average.setSuffix(" s")
        self.network_average.setValue(
            max(2, int(getattr(s, "network_average_seconds", 60)))
        )

        self.close_apps = QCheckBox(
            "Cerrar aplicaciones correctamente antes de apagar/reiniciar"
        )
        self.close_apps.setChecked(
            getattr(s, "close_apps_first", True)
        )

        self.pre_action_enabled = QCheckBox(
            "Ejecutar programa/comando antes de la acción"
        )
        self.pre_action_enabled.setChecked(
            getattr(s, "pre_action_enabled", False)
        )
        self.pre_action_command = QLineEdit(
            getattr(s, "pre_action_command", "")
        )
        self.pre_action_browse = QPushButton("Examinar...")
        pre_action_command_row = QHBoxLayout()
        pre_action_command_row.addWidget(self.pre_action_command, 1)
        pre_action_command_row.addWidget(self.pre_action_browse)

        self.pre_action_wait = QCheckBox("Esperar a que termine")
        self.pre_action_wait.setChecked(
            getattr(s, "pre_action_wait", True)
        )
        self.pre_action_timeout = QSpinBox()
        self.pre_action_timeout.setRange(1, 3600)
        self.pre_action_timeout.setSuffix(" s")
        self.pre_action_timeout.setValue(
            max(
                1,
                min(
                    3600,
                    int(getattr(s, "pre_action_timeout_seconds", 60)),
                ),
            )
        )
        self.pre_action_failure_policy = QComboBox()
        self.pre_action_failure_policy.addItem(
            "Cancelar acción",
            "cancel",
        )
        self.pre_action_failure_policy.addItem(
            "Continuar de todos modos",
            "continue",
        )
        policy_index = self.pre_action_failure_policy.findData(
            getattr(s, "pre_action_failure_policy", "cancel")
        )
        self.pre_action_failure_policy.setCurrentIndex(max(0, policy_index))

        self.cancel_action_behavior = QComboBox()
        self.cancel_action_behavior.addItem("No hacer nada", "none")
        self.cancel_action_behavior.addItem("Ejecutar comando", "command")
        cancel_behavior_index = self.cancel_action_behavior.findData(
            getattr(s, "cancel_action_behavior", "none")
        )
        self.cancel_action_behavior.setCurrentIndex(
            max(0, cancel_behavior_index)
        )
        self.cancel_action_command = QLineEdit(
            getattr(s, "cancel_action_command", "")
        )
        self.cancel_action_browse = QPushButton("Examinar...")
        cancel_action_command_row = QHBoxLayout()
        cancel_action_command_row.addWidget(self.cancel_action_command, 1)
        cancel_action_command_row.addWidget(self.cancel_action_browse)

        days_box = QGroupBox("Días de la semana")
        days_layout = QGridLayout(days_box)

        self.days = []
        for i, d in enumerate(WEEKDAYS):
            c = QCheckBox(d)
            c.setChecked(i in s.weekdays)
            self.days.append(c)
            days_layout.addWidget(c, i // 4, i % 4)

        self.days_box = days_box

        self.warn_box = QGroupBox("Avisos previos para horario programado")
        warn_layout = QHBoxLayout(self.warn_box)

        self.warn30 = QCheckBox("30 min")
        self.warn15 = QCheckBox("15 min")
        self.warn5 = QCheckBox("5 min")
        self.warn1 = QCheckBox("1 min")

        for w, val in [
            (self.warn30, 30),
            (self.warn15, 15),
            (self.warn5, 5),
            (self.warn1, 1),
        ]:
            w.setChecked(val in s.warning_minutes)
            warn_layout.addWidget(w)

        scheduling_note = QLabel(
            "El modo Intervalo comienza al guardar y se ejecuta una sola vez. "
            "Al completarse queda desactivado; editarlo o reactivarlo inicia "
            "un intervalo nuevo. Los días no se aplican a Intervalo.\n\n"
            "En Solo inactividad, la acción puede ejecutarse a cualquier hora "
            "de los días seleccionados y la inactividad es obligatoria."
        )
        scheduling_note.setWordWrap(True)

        action_note = QLabel(
            "El cierre seguro de aplicaciones se usa para Apagar y Reiniciar "
            "mediante la sesión de Plasma, evitando matar los programas a la "
            "fuerza. Cerrar sesión usa el cierre normal de Plasma. Bloquear "
            "no cierra Chrome ni otras aplicaciones."
        )
        action_note.setWordWrap(True)

        self.tabs = QTabWidget()
        outer.addWidget(self.tabs, 1)

        general_tab = QWidget()
        general_form = QFormLayout(general_tab)
        general_form.addRow("Nombre:", self.name)
        general_form.addRow("Estado:", self.enabled)
        general_form.addRow("Acción:", self.action)
        general_form.addRow("Cuenta regresiva final:", self.countdown)
        general_form.addRow(self.condition_logic_label, self.condition_logic)
        general_form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.tabs.addTab(general_tab, "General")

        scheduling_tab = QWidget()
        scheduling_layout = QVBoxLayout(scheduling_tab)
        scheduling_form = QFormLayout()
        scheduling_form.addRow("Modo:", self.mode)
        scheduling_form.addRow("Hora:", self.time)
        scheduling_form.addRow("Duración:", interval_row)
        scheduling_form.setFieldGrowthPolicy(
            QFormLayout.AllNonFixedFieldsGrow
        )
        scheduling_layout.addLayout(scheduling_form)
        scheduling_layout.addWidget(self.days_box)
        scheduling_layout.addWidget(self.warn_box)
        scheduling_layout.addWidget(scheduling_note)
        scheduling_layout.addStretch()
        self.tabs.addTab(scheduling_tab, "Programación")

        idle_tab = QWidget()
        idle_form = QFormLayout(idle_tab)
        idle_form.addRow("Inactividad:", self.require_idle)
        idle_form.addRow("Tiempo mínimo inactivo:", self.idle_minutes)
        idle_form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.tabs.addTab(idle_tab, "Inactividad")

        cpu_tab = QWidget()
        cpu_form = QFormLayout(cpu_tab)
        cpu_form.addRow("CPU:", self.require_cpu)
        cpu_form.addRow("Comparación:", self.cpu_comparison)
        cpu_form.addRow("Umbral:", self.cpu_threshold)
        cpu_form.addRow("Duración continua:", self.cpu_duration)
        cpu_form.addRow("Promedio:", self.cpu_use_average)
        cpu_form.addRow("Ventana del promedio:", self.cpu_average)
        cpu_form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.tabs.addTab(cpu_tab, "CPU")

        network_tab = QWidget()
        network_form = QFormLayout(network_tab)
        network_form.addRow("Red:", self.require_network)
        network_form.addRow("Interfaz:", self.network_interface)
        network_form.addRow("Dirección:", self.network_direction)
        network_form.addRow("Comparación:", self.network_comparison)
        network_form.addRow("Umbral:", network_threshold_row)
        network_form.addRow("Duración continua:", self.network_duration)
        network_form.addRow("Promedio:", self.network_use_average)
        network_form.addRow("Ventana del promedio:", self.network_average)
        network_form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.tabs.addTab(network_tab, "Red")

        action_tab = QWidget()
        action_layout = QVBoxLayout(action_tab)
        safe_close_box = QGroupBox("Cierre seguro")
        safe_close_layout = QVBoxLayout(safe_close_box)
        safe_close_layout.addWidget(self.close_apps)
        action_layout.addWidget(safe_close_box)

        pre_action_box = QGroupBox("Programa previo")
        pre_action_form = QFormLayout(pre_action_box)
        pre_action_form.addRow(self.pre_action_enabled)
        pre_action_form.addRow("Comando/programa:", pre_action_command_row)
        pre_action_form.addRow(self.pre_action_wait)
        pre_action_form.addRow("Timeout:", self.pre_action_timeout)
        pre_action_form.addRow(
            "Si falla:",
            self.pre_action_failure_policy,
        )
        pre_action_form.setFieldGrowthPolicy(
            QFormLayout.AllNonFixedFieldsGrow
        )
        action_layout.addWidget(pre_action_box)

        cancel_action_box = QGroupBox("Al cancelar explícitamente")
        cancel_action_form = QFormLayout(cancel_action_box)
        cancel_action_form.addRow(
            "Comportamiento:",
            self.cancel_action_behavior,
        )
        cancel_action_form.addRow(
            "Comando:",
            cancel_action_command_row,
        )
        cancel_action_form.setFieldGrowthPolicy(
            QFormLayout.AllNonFixedFieldsGrow
        )
        action_layout.addWidget(cancel_action_box)
        action_layout.addWidget(action_note)
        action_layout.addStretch()
        self.tabs.addTab(action_tab, "Acción")

        buttons = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

        self.mode.currentIndexChanged.connect(self._refresh_mode_controls)
        self.require_idle.toggled.connect(self._refresh_mode_controls)
        self.require_cpu.toggled.connect(self._refresh_cpu_controls)
        self.cpu_use_average.toggled.connect(self._refresh_cpu_controls)
        self.require_network.toggled.connect(self._refresh_network_controls)
        self.network_use_average.toggled.connect(
            self._refresh_network_controls
        )
        self.action.currentIndexChanged.connect(self._refresh_action_controls)
        self.pre_action_enabled.toggled.connect(
            self._refresh_pre_action_controls
        )
        self.pre_action_wait.toggled.connect(
            self._refresh_pre_action_controls
        )
        self.pre_action_browse.clicked.connect(self._browse_pre_action)
        self.cancel_action_behavior.currentIndexChanged.connect(
            self._refresh_cancel_action_controls
        )
        self.cancel_action_browse.clicked.connect(
            self._browse_cancel_action
        )

        self._refresh_mode_controls()
        self._refresh_cpu_controls()
        self._refresh_network_controls()
        self._refresh_logic_controls()
        self._refresh_action_controls()
        self._refresh_pre_action_controls()
        self._refresh_cancel_action_controls()

    def _refresh_mode_controls(self):
        mode = self.mode.currentData()
        timed = mode == "time"
        interval = mode == "interval"

        self.time.setEnabled(timed)
        self.warn_box.setEnabled(timed)
        self.interval_hours.setEnabled(interval)
        self.interval_minutes.setEnabled(interval)
        self.days_box.setEnabled(not interval)

        if mode == "idle":
            self.require_idle.setChecked(True)
            self.require_idle.setEnabled(False)
            self.idle_minutes.setEnabled(True)
        else:
            self.require_idle.setEnabled(True)
            self.idle_minutes.setEnabled(self.require_idle.isChecked())
        self._refresh_logic_controls()

    def _ensure_interval_duration(self):
        if self.interval_hours.value() == 0 and self.interval_minutes.value() == 0:
            self.interval_minutes.setValue(1)

    def _refresh_cpu_controls(self):
        enabled = self.require_cpu.isChecked()
        self.cpu_comparison.setEnabled(enabled)
        self.cpu_threshold.setEnabled(enabled)
        self.cpu_duration.setEnabled(enabled)
        self.cpu_use_average.setEnabled(enabled)
        self.cpu_average.setEnabled(
            enabled and self.cpu_use_average.isChecked()
        )
        self._refresh_logic_controls()

    def _refresh_network_controls(self):
        enabled = self.require_network.isChecked()
        self.network_interface.setEnabled(enabled)
        self.network_direction.setEnabled(enabled)
        self.network_comparison.setEnabled(enabled)
        self.network_threshold.setEnabled(enabled)
        self.network_unit.setEnabled(enabled)
        self.network_duration.setEnabled(enabled)
        self.network_use_average.setEnabled(enabled)
        self.network_average.setEnabled(
            enabled and self.network_use_average.isChecked()
        )
        self._refresh_logic_controls()

    def _refresh_logic_controls(self):
        if not hasattr(self, "condition_logic"):
            return
        mode = self.mode.currentData()
        active_conditions = 1
        if mode != "idle" and self.require_idle.isChecked():
            active_conditions += 1
        if self.require_cpu.isChecked():
            active_conditions += 1
        if self.require_network.isChecked():
            active_conditions += 1
        visible = active_conditions >= 2
        self.condition_logic_label.setVisible(visible)
        self.condition_logic.setVisible(visible)

    def _refresh_action_controls(self):
        action = self.action.currentData()
        self.close_apps.setEnabled(action in ("poweroff", "reboot"))

    def _refresh_pre_action_controls(self):
        enabled = self.pre_action_enabled.isChecked()
        self.pre_action_command.setEnabled(enabled)
        self.pre_action_browse.setEnabled(enabled)
        self.pre_action_wait.setEnabled(enabled)
        self.pre_action_timeout.setEnabled(
            enabled and self.pre_action_wait.isChecked()
        )
        self.pre_action_failure_policy.setEnabled(enabled)

    def _browse_pre_action(self):
        path, _selected_filter = QFileDialog.getOpenFileName(
            self,
            "Seleccionar programa o comando",
        )
        if path:
            self.pre_action_command.setText(shlex.quote(path))

    def _refresh_cancel_action_controls(self):
        enabled = self.cancel_action_behavior.currentData() == "command"
        self.cancel_action_command.setEnabled(enabled)
        self.cancel_action_browse.setEnabled(enabled)

    def _browse_cancel_action(self):
        path, _selected_filter = QFileDialog.getOpenFileName(
            self,
            "Seleccionar comando al cancelar",
        )
        if path:
            self.cancel_action_command.setText(shlex.quote(path))

    def get_schedule(self):
        warns = []

        for w, val in [
            (self.warn30, 30),
            (self.warn15, 15),
            (self.warn5, 5),
            (self.warn1, 1),
        ]:
            if w.isChecked():
                warns.append(val)

        weekdays = [
            i for i, c in enumerate(self.days)
            if c.isChecked()
        ]

        mode = self.mode.currentData()
        use_time = mode == "time"
        interval_minutes = max(
            1,
            self.interval_hours.value() * 60
            + self.interval_minutes.value(),
        )
        action = self.action.currentData()

        return Schedule(
            id=self.original.id if self.original else str(uuid.uuid4()),
            name=self.name.text().strip() or "Programación",
            enabled=self.enabled.isChecked(),
            use_time=use_time,
            trigger_mode=mode,
            interval_minutes=interval_minutes,
            time=self.time.time().toString("HH:mm"),
            weekdays=weekdays,
            action=action,
            warning_minutes=sorted(warns, reverse=True),
            final_countdown_seconds=self.countdown.value(),
            condition_logic=self.condition_logic.currentData(),
            require_idle=(
                True if mode == "idle"
                else self.require_idle.isChecked()
            ),
            idle_minutes=self.idle_minutes.value(),
            require_cpu=self.require_cpu.isChecked(),
            cpu_comparison=self.cpu_comparison.currentData(),
            cpu_threshold=self.cpu_threshold.value(),
            cpu_duration_seconds=self.cpu_duration.value(),
            cpu_use_average=self.cpu_use_average.isChecked(),
            cpu_average_seconds=self.cpu_average.value(),
            require_network=self.require_network.isChecked(),
            network_interface=self.network_interface.currentData() or "",
            network_direction=self.network_direction.currentData(),
            network_comparison=self.network_comparison.currentData(),
            network_threshold=self.network_threshold.value(),
            network_unit=self.network_unit.currentData(),
            network_duration_seconds=self.network_duration.value(),
            network_use_average=self.network_use_average.isChecked(),
            network_average_seconds=self.network_average.value(),
            close_apps_first=(
                self.close_apps.isChecked()
                if action in ("poweroff", "reboot")
                else False
            ),
            pre_action_enabled=self.pre_action_enabled.isChecked(),
            pre_action_command=self.pre_action_command.text().strip(),
            pre_action_wait=self.pre_action_wait.isChecked(),
            pre_action_timeout_seconds=self.pre_action_timeout.value(),
            pre_action_failure_policy=(
                self.pre_action_failure_policy.currentData()
            ),
            cancel_action_behavior=self.cancel_action_behavior.currentData(),
            cancel_action_command=self.cancel_action_command.text().strip(),
        )


class MainWindow(QMainWindow):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.config = load_json(CONFIG_FILE, DEFAULT_CONFIG)
        self.state = load_json(STATE_FILE, DEFAULT_STATE)
        self.warned = set()
        self.active_dialogs = {}
        self.update_thread = None
        self.download_thread = None
        self.download_dialog = None
        self._pre_action_processes = {}
        self.available_update = self.state.get("available_update")

        # Control del instalador que espera al OK antes de reiniciar.
        self.install_process = None
        self.install_progress = None
        self.pending_update_version = None
        self.install_poll_timer = QTimer(self)
        self.install_poll_timer.timeout.connect(self._poll_update_install)

        self.condition_engine = ConditionEngine()
        self.cpu_monitor = CPUMonitor()
        self._last_cpu_monitor_error = None
        self.network_monitor = NetworkMonitor()
        self._last_network_monitor_error = None
        self._condition_wait_notified = {}
        self._reconcile_schedule_occurrences(
            self.schedules(),
            now=datetime.now(),
        )

        self.last_activity_monotonic = time.monotonic()
        self.last_activity_device = "inicio de AMP AutoPower"
        self.input_monitor_status = {"backend":"evdev","available":evdev is not None,"accessible":0,"denied":0,"total":0,"devices":[]}
        self.input_monitor = None
        self.banner_windows = []
        self.setWindowTitle(f"{APP_NAME} {APP_VERSION}")
        self.resize(800, 610)
        self.setMinimumSize(700, 500)

        self.tray = QSystemTrayIcon(self)
        icon = QIcon.fromTheme("system-shutdown")
        self.setWindowIcon(icon)
        self.tray.setIcon(icon)
        self.tray.setToolTip(f"{APP_NAME} {APP_VERSION}")
        self.tray.activated.connect(self.on_tray_activated)
        menu = self.tray.contextMenu()
        if menu is None:
            from PySide6.QtWidgets import QMenu
            menu = QMenu()
            self.tray.setContextMenu(menu)
        show_action = QAction("Abrir", self)
        show_action.triggered.connect(self.show_normal)
        self.display_tray_action = QAction("Mostrar display", self)
        self.display_tray_action.setCheckable(True)
        self.display_tray_action.setChecked(
            self.config.get("display_enabled", False)
        )
        self.display_tray_action.toggled.connect(self.set_display_enabled)
        cancel_action = QAction("Cancelar próxima ejecución", self)
        cancel_action.triggered.connect(self.cancel_next_run)
        update_action = QAction("Buscar actualizaciones", self)
        update_action.triggered.connect(lambda: self.check_updates(manual=True))
        quit_action = QAction("Salir", self)
        quit_action.triggered.connect(self.quit_app)
        menu.addAction(show_action)
        menu.addAction(self.display_tray_action)
        menu.addAction(cancel_action)
        menu.addAction(update_action)
        menu.addSeparator()
        menu.addAction(quit_action)
        self.tray.setVisible(self.config.get("tray_visible", True))

        self.main_tabs = QTabWidget()
        self.setCentralWidget(self.main_tabs)

        sched_tab = QWidget()
        sched_lay = QVBoxLayout(sched_tab)
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet("font-size: 15px; padding: 8px;")
        sched_lay.addWidget(self.status_label)

        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(lambda _: self.edit_schedule())
        sched_lay.addWidget(self.list)
        row = QHBoxLayout()
        add_btn = QPushButton("Añadir")
        edit_btn = QPushButton("Editar")
        del_btn = QPushButton("Eliminar")
        test_btn = QPushButton("Probar aviso")
        cancel_btn = QPushButton("Cancelar próxima vez")
        add_btn.clicked.connect(self.add_schedule)
        edit_btn.clicked.connect(self.edit_schedule)
        del_btn.clicked.connect(self.delete_schedule)
        test_btn.clicked.connect(self.test_warning)
        cancel_btn.clicked.connect(self.cancel_next_run)
        for b in (add_btn, edit_btn, del_btn, test_btn, cancel_btn):
            row.addWidget(b)
        sched_lay.addLayout(row)
        self.main_tabs.addTab(sched_tab, "Programaciones")

        settings_tab = QWidget()
        settings_layout = QVBoxLayout(settings_tab)
        self.preferences_tabs = QTabWidget()
        settings_layout.addWidget(self.preferences_tabs)

        general_tab = QWidget()
        general_layout = QVBoxLayout(general_tab)
        self.start_min = QCheckBox("Iniciar con la ventana oculta")
        self.tray_visible = QCheckBox("Mostrar icono en la bandeja del sistema")
        self.close_tray = QCheckBox(
            "Cerrar la ventana = ocultarla sin detener el scheduler"
        )
        self.notifications = QCheckBox("Mostrar notificaciones previas")
        self.sound = QCheckBox("Reproducir sonido de aviso cuando sea posible")
        self.start_min.setChecked(self.config.get("start_minimized", True))
        self.tray_visible.setChecked(self.config.get("tray_visible", True))
        self.close_tray.setChecked(self.config.get("close_to_tray", True))
        self.notifications.setChecked(self.config.get("notifications", True))
        self.sound.setChecked(self.config.get("sound", True))
        for w in (self.start_min, self.close_tray, self.notifications, self.sound):
            general_layout.addWidget(w)
            w.toggled.connect(self.save_settings)
        general_layout.insertWidget(1, self.tray_visible)
        self.tray_visible.toggled.connect(self.set_tray_visible)
        recovery_note = QLabel(
            "Si ocultas la bandeja puedes recuperar la ventana con el "
            "atajo global o ejecutando amp-autopower --show."
        )
        recovery_note.setWordWrap(True)
        general_layout.addWidget(recovery_note)
        general_layout.addStretch()
        self.preferences_tabs.addTab(general_tab, "General")

        behavior_tab = QWidget()
        behavior_layout = QVBoxLayout(behavior_tab)

        overlay_box = QGroupBox("Avisos sobre juegos y pantalla completa")
        overlay_lay = QVBoxLayout(overlay_box)
        self.fullscreen_overlay = QCheckBox("Forzar overlay de emergencia siempre encima")
        self.fullscreen_overlay.setChecked(self.config.get("fullscreen_overlay", True))
        self.overlay_all_screens = QCheckBox("Mostrar la cuenta regresiva en todas las pantallas")
        self.overlay_all_screens.setChecked(self.config.get("overlay_all_screens", True))
        self.overlay_all_schedule_warnings = QCheckBox("Mostrar también los avisos previos como bandas sobre el juego")
        self.overlay_all_schedule_warnings.setChecked(self.config.get("overlay_all_schedule_warnings", True))
        for w in (self.fullscreen_overlay, self.overlay_all_screens, self.overlay_all_schedule_warnings):
            overlay_lay.addWidget(w); w.toggled.connect(self.save_settings)
        behavior_layout.addWidget(overlay_box)

        display_box = QGroupBox("Display compacto")
        display_lay = QFormLayout(display_box)
        self.display_enabled = QCheckBox("Mostrar display")
        self.display_always_on_top = QCheckBox(
            "Siempre visible sobre otras ventanas"
        )
        self.display_show_title = QCheckBox("Mostrar título")
        self.display_show_action = QCheckBox("Mostrar tipo de acción")
        self.display_use_24_hour = QCheckBox("Usar formato de 24 horas")
        self.display_transparency = QSpinBox()
        self.display_transparency.setRange(1, 99)
        self.display_transparency.setSuffix(" %")
        display_options = display_options_from_config(self.config)
        self.display_enabled.setChecked(display_options.enabled)
        self.display_always_on_top.setChecked(
            display_options.always_on_top
        )
        self.display_show_title.setChecked(display_options.show_title)
        self.display_show_action.setChecked(display_options.show_action)
        self.display_use_24_hour.setChecked(display_options.use_24_hour)
        self.display_transparency.setValue(
            display_options.transparency_percent
        )
        display_lay.addRow(self.display_enabled)
        display_lay.addRow(self.display_always_on_top)
        display_lay.addRow(self.display_show_title)
        display_lay.addRow(self.display_show_action)
        display_lay.addRow("Transparencia:", self.display_transparency)
        display_lay.addRow(self.display_use_24_hour)
        display_tab = QWidget()
        display_tab_layout = QVBoxLayout(display_tab)
        display_tab_layout.addWidget(display_box)
        display_tab_layout.addStretch()
        self.preferences_tabs.addTab(display_tab, "Display")
        self.display_enabled.toggled.connect(self.set_display_enabled)
        for widget in (
            self.display_always_on_top,
            self.display_show_title,
            self.display_show_action,
            self.display_use_24_hour,
        ):
            widget.toggled.connect(self.save_display_settings)
        self.display_transparency.valueChanged.connect(
            self.save_display_settings
        )

        shortcuts_tab = QWidget()
        shortcuts_layout = QVBoxLayout(shortcuts_tab)
        shortcuts_form = QFormLayout()
        self.global_hotkey_edit = QKeySequenceEdit()
        self.global_hotkey_edit.setMaximumSequenceLength(1)
        self.global_hotkey_edit.setKeySequence(
            QKeySequence.fromString(
                self.config.get("global_hotkey", ""),
                QKeySequence.PortableText,
            )
        )
        self.clear_global_hotkey_btn = QPushButton("Desactivar atajo")
        shortcuts_form.addRow(
            "Mostrar/ocultar ventana:",
            self.global_hotkey_edit,
        )
        shortcuts_form.addRow(self.clear_global_hotkey_btn)
        shortcuts_layout.addLayout(shortcuts_form)
        self.global_hotkey_status = QLabel()
        self.global_hotkey_status.setWordWrap(True)
        shortcuts_layout.addWidget(self.global_hotkey_status)
        shortcuts_note = QLabel(
            "El atajo usa KGlobalAccel de KDE en Wayland. Déjalo vacío para "
            "desactivarlo; --show seguirá disponible como recuperación."
        )
        shortcuts_note.setWordWrap(True)
        shortcuts_layout.addWidget(shortcuts_note)
        shortcuts_layout.addStretch()
        self.preferences_tabs.addTab(shortcuts_tab, "Atajos")
        self.global_hotkey_edit.editingFinished.connect(
            self.save_global_hotkey
        )
        self.clear_global_hotkey_btn.clicked.connect(
            self.clear_global_hotkey
        )

        activity_box = QGroupBox("Detección global de inactividad")
        activity_lay = QVBoxLayout(activity_box)
        self.input_monitor_check = QCheckBox("Detectar mouse, teclado, touchpad, joystick y mandos mediante evdev")
        self.input_monitor_check.setChecked(self.config.get("input_monitor_enabled", True))
        self.activity_label = QLabel("Inicializando monitor de entrada…")
        self.activity_label.setWordWrap(True)
        activity_lay.addWidget(self.input_monitor_check); activity_lay.addWidget(self.activity_label)
        behavior_layout.addWidget(activity_box)
        self.input_monitor_check.toggled.connect(self.toggle_input_monitor)
        cancel_note = QLabel(
            "El comando opcional ante cancelación se configura por "
            "programación en su pestaña Acción. Posponer no lo ejecuta."
        )
        cancel_note.setWordWrap(True)
        behavior_layout.addWidget(cancel_note)
        behavior_layout.addStretch()
        self.preferences_tabs.addTab(behavior_tab, "Comportamiento")
        self.main_tabs.addTab(settings_tab, "Preferencias")

        update_tab = QWidget()
        update_layout = QVBoxLayout(update_tab)
        self.update_version = QLabel(f"<h3>Versión instalada: {APP_VERSION}</h3>")
        update_layout.addWidget(self.update_version)
        self.update_status = QLabel("Estado de actualizaciones: sin comprobar todavía.")
        self.update_status.setWordWrap(True)
        update_layout.addWidget(self.update_status)

        update_group = QGroupBox("Actualización automática")
        update_group_layout = QFormLayout(update_group)
        self.auto_updates = QCheckBox("Comprobar automáticamente cada 48 horas")
        self.auto_updates.setChecked(self.config.get("auto_check_updates", True))
        self.notify_updates = QCheckBox("Avisarme cuando haya una versión nueva")
        self.notify_updates.setChecked(self.config.get("notify_updates", True))
        self.manifest_url = QLineEdit(self.config.get("update_manifest_url", ""))
        self.manifest_url.setPlaceholderText("Opcional: URL del manifest.json del canal de actualizaciones")
        update_group_layout.addRow(self.auto_updates)
        update_group_layout.addRow(self.notify_updates)
        update_group_layout.addRow("Canal por Internet:", self.manifest_url)
        update_layout.addWidget(update_group)

        update_buttons = QHBoxLayout()
        self.check_update_btn = QPushButton("Buscar actualizaciones")
        self.install_available_btn = QPushButton("Instalar actualización disponible")
        self.install_package_btn = QPushButton("Instalar paquete .tar.gz…")
        self.check_update_btn.clicked.connect(lambda: self.check_updates(manual=True))
        self.install_available_btn.clicked.connect(self.install_available_update)
        self.install_package_btn.clicked.connect(self.choose_update_package)
        for b in (self.check_update_btn, self.install_available_btn, self.install_package_btn):
            update_buttons.addWidget(b)
        update_layout.addLayout(update_buttons)
        self.last_check_label = QLabel()
        update_layout.addWidget(self.last_check_label)
        tip = QLabel(
            "AMP AutoPower también busca paquetes <b>AMP-AutoPower*.tar.gz</b> en ~/Descargas y ~/Downloads. "
            "Así, cuando descargues una nueva versión, basta con pulsar <b>Buscar actualizaciones</b> y luego <b>Instalar</b>. "
            "La instalación conserva tus horarios y crea un respaldo de la versión anterior."
        )
        tip.setWordWrap(True)
        update_layout.addWidget(tip)
        update_layout.addStretch()
        self.main_tabs.addTab(update_tab, "Actualizaciones")

        self.auto_updates.toggled.connect(self.save_settings)
        self.notify_updates.toggled.connect(self.save_settings)
        self.manifest_url.editingFinished.connect(self.save_settings)

        info_tab = QWidget()
        info_layout = QVBoxLayout(info_tab)
        self.info = QLabel()
        self.info.setWordWrap(True)
        self.info.setText(
            f"<h3>{APP_NAME} {APP_VERSION}</h3>"
            "<p>Programa acciones de energía usando systemd. Mantiene un icono en la bandeja y avisa antes de ejecutar.</p>"
            f"<p><b>Configuración:</b> {CONFIG_FILE}<br>"
            f"<b>Registro:</b> {LOG_FILE}</p>"
            "<p>Las acciones disponibles son apagar, reiniciar, suspender, hibernar y una acción de prueba.</p>"
        )
        info_layout.addWidget(self.info)
        info_layout.addStretch()
        self.main_tabs.addTab(info_tab, "Información")

        self.refresh_list()
        self.refresh_update_ui()
        self.compact_display = CompactDisplayWindow()
        self.compact_display.open_requested.connect(self.show_normal)
        self.compact_display.hide_requested.connect(
            lambda: self.set_display_enabled(False)
        )
        self.compact_display.position_changed.connect(
            self.save_display_position
        )
        self.compact_display.apply_options(display_options)
        self.app.screenAdded.connect(lambda _screen: self.ensure_display_position())
        self.app.screenRemoved.connect(lambda _screen: self.ensure_display_position())
        if display_options.enabled:
            self.show_compact_display()
        self.global_shortcut = GlobalShortcutManager(self)
        self.global_shortcut.activated.connect(self.toggle_main_window)
        self.global_shortcut.status_changed.connect(
            self._on_global_shortcut_status
        )
        self.apply_global_hotkey(notify_failure=False)
        self._start_input_monitor()
        self.activity_ui_timer = QTimer(self)
        self.activity_ui_timer.timeout.connect(self.refresh_activity_label)
        self.activity_ui_timer.start(1000)
        self.refresh_activity_label()
        self.scheduler = QTimer(self)
        self.scheduler.timeout.connect(self.scheduler_tick)
        self.scheduler.start(1000)
        self.scheduler_tick()
        QTimer.singleShot(6000, self.maybe_auto_check_updates)

    def schedules(self):
        return schedules_from_config(self.config)

    def set_schedule_enabled(self, selector, enabled, now=None):
        schedules = self.schedules()
        target, error, code = resolve_schedule(schedules, selector)
        if target is None:
            return False, error, code

        enabled = bool(enabled)
        changed = target.enabled != enabled
        updated = replace(target, enabled=enabled)
        schedules = [updated if item.id == target.id else item for item in schedules]
        restart_ids = None
        if changed and enabled and schedule_trigger_mode(updated) == "interval":
            restart_ids = {updated.id}
        self.set_schedules(
            schedules,
            restart_interval_ids=restart_ids,
            now=now,
        )
        state = "activada" if enabled else "desactivada"
        qualifier = "" if changed else " (sin cambios)"
        return (
            True,
            f"Programación {state}: {_cli_text(updated.name)} "
            f"[{updated.id}]{qualifier}",
            0,
        )

    def handle_cli_command(self, command, target=None):
        if command == "show":
            self.show_normal()
            return {"ok": True, "code": 0, "output": "Ventana mostrada."}
        if command == "hide":
            self.hide()
            return {"ok": True, "code": 0, "output": "Ventana oculta."}
        if command == "toggle":
            self.toggle_main_window()
            return {"ok": True, "code": 0, "output": "Visibilidad alternada."}
        if command == "status":
            schedules = self.schedules()
            output = format_status_cli(
                self.config,
                self.state,
                schedules,
                ipc_available=True,
                display_visible=self.compact_display.isVisible(),
                tray_visible=self.tray.isVisible(),
            )
            return {"ok": True, "code": 0, "output": output}
        if command == "list-schedules":
            output = format_schedules_cli(self.schedules(), self.state)
            return {"ok": True, "code": 0, "output": output}
        if command in ("enable", "disable"):
            ok, output, code = self.set_schedule_enabled(
                target,
                command == "enable",
            )
            return {"ok": ok, "code": code, "output": output}
        return {
            "ok": False,
            "code": 2,
            "output": f"Comando IPC no válido: {_cli_text(command)}",
        }

    def set_schedules(self, schedules, restart_interval_ids=None, now=None):
        previous_by_id = {s.id: s for s in self.schedules()}
        previous_ids = set(previous_by_id)
        restart_ids = set(restart_interval_ids or ())
        schedules_by_id = {s.id: s for s in schedules}
        cpu_fields = (
            "enabled",
            "use_time",
            "trigger_mode",
            "weekdays",
            "require_cpu",
            "cpu_comparison",
            "cpu_threshold",
            "cpu_duration_seconds",
            "cpu_use_average",
            "cpu_average_seconds",
        )
        reset_cpu_ids = set(restart_ids) | (previous_ids - set(schedules_by_id))
        for schedule_id, schedule in schedules_by_id.items():
            previous = previous_by_id.get(schedule_id)
            if previous is None or any(
                getattr(previous, field_name) != getattr(schedule, field_name)
                for field_name in cpu_fields
            ):
                reset_cpu_ids.add(schedule_id)
        for schedule_id in reset_cpu_ids:
            self.condition_engine.runtime.clear_cpu_runtime(schedule_id)
        network_fields = (
            "enabled",
            "use_time",
            "trigger_mode",
            "weekdays",
            "require_network",
            "network_interface",
            "network_direction",
            "network_comparison",
            "network_threshold",
            "network_unit",
            "network_duration_seconds",
            "network_use_average",
            "network_average_seconds",
        )
        reset_network_ids = set(restart_ids) | (
            previous_ids - set(schedules_by_id)
        )
        for schedule_id, schedule in schedules_by_id.items():
            previous = previous_by_id.get(schedule_id)
            if previous is None or any(
                getattr(previous, field_name) != getattr(schedule, field_name)
                for field_name in network_fields
            ):
                reset_network_ids.add(schedule_id)
        for schedule_id in reset_network_ids:
            self.condition_engine.runtime.clear_condition_runtime(
                schedule_id,
                "network",
            )
        reset_idle_ids = set(restart_ids) | (previous_ids - set(schedules_by_id))
        for schedule_id, schedule in schedules_by_id.items():
            previous = previous_by_id.get(schedule_id)
            if previous is None or any(
                getattr(previous, field_name) != getattr(schedule, field_name)
                for field_name in (
                    "enabled",
                    "use_time",
                    "trigger_mode",
                    "weekdays",
                    "idle_minutes",
                    "require_idle",
                )
            ):
                reset_idle_ids.add(schedule_id)
        for schedule_id in reset_idle_ids:
            self.condition_engine.runtime.clear_idle_cycle_triggered(schedule_id)
            self.condition_engine.runtime.clear_idle_snooze(schedule_id)
        for dlg in list(self.active_dialogs.values()):
            dialog_schedule = getattr(dlg, "schedule", None)
            schedule_id = getattr(dialog_schedule, "id", None)
            updated = schedules_by_id.get(schedule_id)
            if schedule_id and (
                updated is None
                or not updated.enabled
                or (
                    schedule_trigger_mode(updated) != "interval"
                    and not updated.weekdays
                )
                or schedule_trigger_mode(updated)
                != schedule_trigger_mode(dialog_schedule)
                or schedule_id in restart_ids
            ):
                dlg.finish("cancel")
        for context in list(
            getattr(self, "_pre_action_processes", {}).values()
        ):
            process = context["process"]
            updated = schedules_by_id.get(context["schedule"].id)
            if updated is None or updated != context["schedule"]:
                self._cancel_pre_action(process)

        cpu_settings = self.config.setdefault("cpu_settings", {})
        cpu_defaults = {
            "cpu_comparison": "less",
            "cpu_threshold": 10,
            "cpu_duration_seconds": 300,
            "cpu_use_average": False,
            "cpu_average_seconds": 60,
        }
        for schedule in schedules:
            configured = {
                key: getattr(schedule, key)
                for key in cpu_defaults
            }
            if configured != cpu_defaults:
                cpu_settings[schedule.id] = configured
            else:
                cpu_settings.pop(schedule.id, None)
        network_settings = self.config.setdefault("network_settings", {})
        network_defaults = {
            "network_interface": "",
            "network_direction": "both",
            "network_comparison": "less",
            "network_threshold": 50,
            "network_unit": "KB/s",
            "network_duration_seconds": 300,
            "network_use_average": False,
            "network_average_seconds": 60,
        }
        for schedule in schedules:
            configured = {
                key: getattr(schedule, key)
                for key in network_defaults
            }
            if configured != network_defaults:
                network_settings[schedule.id] = configured
            else:
                network_settings.pop(schedule.id, None)
        logic_settings = self.config.setdefault(
            "condition_logic_settings",
            {},
        )
        for schedule in schedules:
            logic = str(schedule.condition_logic).upper()
            if logic == "OR":
                logic_settings[schedule.id] = "OR"
            else:
                logic_settings.pop(schedule.id, None)
        action_settings = self.config.setdefault("action_settings", {})
        action_defaults = {
            "pre_action_enabled": False,
            "pre_action_command": "",
            "pre_action_wait": True,
            "pre_action_timeout_seconds": 60,
            "pre_action_failure_policy": "cancel",
            "cancel_action_behavior": "none",
            "cancel_action_command": "",
        }
        for schedule in schedules:
            configured = {
                key: getattr(schedule, key)
                for key in action_defaults
            }
            if configured != action_defaults:
                action_settings[schedule.id] = configured
            else:
                action_settings.pop(schedule.id, None)
        removed_ids = previous_ids - set(schedules_by_id)
        for schedule_id in removed_ids:
            cpu_settings.pop(schedule_id, None)
            network_settings.pop(schedule_id, None)
            logic_settings.pop(schedule_id, None)
            action_settings.pop(schedule_id, None)
        self.config["schedules"] = [schedule_to_dict(s) for s in schedules]
        save_json(CONFIG_FILE, self.config)

        changed_enabled_ids = {
            schedule_id
            for schedule_id, schedule in schedules_by_id.items()
            if (
                schedule_id in previous_by_id
                and previous_by_id[schedule_id].enabled != schedule.enabled
            )
        }
        state_changed = False
        for schedule_id in changed_enabled_ids:
            if self.state.get("snoozes", {}).pop(schedule_id, None) is not None:
                state_changed = True
        for schedule_id in removed_ids:
            for state_key in (
                "last_runs",
                "snoozes",
                "skipped_targets",
                "pending_occurrences",
                "completed_intervals",
            ):
                if self.state.get(state_key, {}).pop(schedule_id, None) is not None:
                    state_changed = True

        self._reconcile_schedule_occurrences(
            schedules,
            restart_interval_ids=restart_interval_ids,
            now=now,
        )
        if state_changed:
            save_json(STATE_FILE, self.state)
        self.refresh_list()

    def _prune_pending_occurrences(self, schedules=None):
        if schedules is None:
            valid_modes = {
                raw.get("id"): schedule_trigger_mode(raw)
                for raw in self.config.get("schedules", [])
                if (
                    isinstance(raw, dict)
                    and raw.get("enabled", True)
                    and (
                        schedule_trigger_mode(raw) == "interval"
                        or (
                            schedule_trigger_mode(raw) == "time"
                            and raw.get("weekdays", [])
                        )
                    )
                )
            }
        else:
            valid_modes = {
                s.id: schedule_trigger_mode(s)
                for s in schedules
                if (
                    s.enabled
                    and (
                        schedule_trigger_mode(s) == "interval"
                        or (
                            schedule_trigger_mode(s) == "time"
                            and s.weekdays
                        )
                    )
                )
            }

        pending = self.state.setdefault("pending_occurrences", {})
        removed = [
            sid
            for sid, raw in pending.items()
            if (
                not isinstance(raw, dict)
                or sid not in valid_modes
                or str(raw.get("trigger_type", "time"))
                != valid_modes[sid]
            )
        ]
        for sid in removed:
            pending.pop(sid, None)
            self.state.get("snoozes", {}).pop(sid, None)
        if removed:
            save_json(STATE_FILE, self.state)

    def _reconcile_schedule_occurrences(
        self,
        schedules,
        restart_interval_ids=None,
        now=None,
    ):
        now = now or datetime.now()
        restart_ids = set(restart_interval_ids or ())
        self._prune_pending_occurrences(schedules)
        changed = False

        interval_ids = {
            s.id
            for s in schedules
            if schedule_trigger_mode(s) == "interval"
        }
        completed_intervals = self.state.setdefault("completed_intervals", {})
        for schedule_id in list(completed_intervals):
            if schedule_id not in interval_ids:
                completed_intervals.pop(schedule_id, None)
                changed = True

        for s in schedules:
            if not s.enabled or schedule_trigger_mode(s) != "interval":
                continue

            occurrence = self.pending_occurrence(s)
            completed = s.id in self.state.get("completed_intervals", {})
            should_restart = s.id in restart_ids

            if completed and not should_restart:
                continue
            if (
                not should_restart
                and occurrence is not None
                and occurrence.trigger_type == "interval"
            ):
                continue

            self._start_interval_occurrence(s, now, save=False)
            changed = True

        if changed:
            save_json(STATE_FILE, self.state)

    def _start_interval_occurrence(self, s, now, save=True):
        occurrence = ScheduledOccurrence.create_interval(
            s.id,
            now,
            int(s.interval_minutes),
            int(s.final_countdown_seconds),
        )
        self.state.setdefault("pending_occurrences", {})[
            s.id
        ] = occurrence.to_state()
        for state_key in (
            "last_runs",
            "snoozes",
            "skipped_targets",
            "completed_intervals",
        ):
            self.state.get(state_key, {}).pop(s.id, None)
        if save:
            save_json(STATE_FILE, self.state)
        return occurrence

    def save_settings(self):
        self.config["start_minimized"] = self.start_min.isChecked()
        self.config["close_to_tray"] = self.close_tray.isChecked()
        self.config["tray_visible"] = self.tray_visible.isChecked()
        self.config["notifications"] = self.notifications.isChecked()
        self.config["sound"] = self.sound.isChecked()
        if hasattr(self, "input_monitor_check"):
            self.config["input_monitor_enabled"] = self.input_monitor_check.isChecked()
            self.config["fullscreen_overlay"] = self.fullscreen_overlay.isChecked()
            self.config["overlay_all_screens"] = self.overlay_all_screens.isChecked()
            self.config["overlay_all_schedule_warnings"] = self.overlay_all_schedule_warnings.isChecked()
        if hasattr(self, "auto_updates"):
            self.config["auto_check_updates"] = self.auto_updates.isChecked()
            self.config["notify_updates"] = self.notify_updates.isChecked()
            self.config["update_manifest_url"] = self.manifest_url.text().strip()
        save_json(CONFIG_FILE, self.config)

    def set_tray_visible(self, visible):
        visible = bool(visible)
        if not visible and not self._window_recovery_available():
            self.tray_visible.blockSignals(True)
            self.tray_visible.setChecked(True)
            self.tray_visible.blockSignals(False)
            self.config["tray_visible"] = True
            self.tray.setVisible(True)
            save_json(CONFIG_FILE, self.config)
            QMessageBox.warning(
                self,
                "No se ocultó la bandeja",
                "No hay un atajo global activo y el canal de recuperación "
                "--show no está disponible. La bandeja permanecerá visible.",
            )
            return False
        if self.tray_visible.isChecked() != visible:
            self.tray_visible.blockSignals(True)
            self.tray_visible.setChecked(visible)
            self.tray_visible.blockSignals(False)
        self.config["tray_visible"] = visible
        self.tray.setVisible(visible)
        save_json(CONFIG_FILE, self.config)
        return True

    def _window_recovery_available(self):
        manager = getattr(self, "global_shortcut", None)
        if manager is not None and getattr(manager, "is_registered", False):
            return True
        ipc = getattr(self, "_ipc", None)
        server = getattr(ipc, "server", None)
        return bool(server is not None and server.isListening())

    def apply_global_hotkey(self, notify_failure=True):
        configured = str(self.config.get("global_hotkey", "") or "").strip()
        shortcut = QKeySequence.fromString(
            configured,
            QKeySequence.PortableText,
        ).toString(QKeySequence.PortableText)
        registration_value = shortcut or configured
        try:
            registered = self.global_shortcut.set_shortcut(registration_value)
        except Exception as exc:
            registered = False
            self.global_shortcut.last_error = str(exc)

        if not configured:
            self.global_hotkey_status.setText("Atajo global desactivado.")
            return True
        if registered:
            self.global_hotkey_status.setText(
                f"Atajo global activo: {shortcut}"
            )
            return True

        error = self.global_shortcut.last_error or "Error desconocido."
        self.global_hotkey_status.setText(
            f"No se pudo activar el atajo: {error}"
        )
        display_shortcut = shortcut or configured
        log(
            "No se pudo registrar el atajo global "
            f"{display_shortcut}: {error}"
        )
        if notify_failure:
            QMessageBox.warning(
                self,
                "Atajo global no disponible",
                f"No se pudo registrar {display_shortcut}.\n\n{error}\n\n"
                "Puedes seguir abriendo la ventana con "
                "amp-autopower --show.",
            )
        return False

    def _on_global_shortcut_status(self, active, error):
        configured = str(self.config.get("global_hotkey", "") or "").strip()
        if active:
            self.global_hotkey_status.setText(
                f"Atajo global activo: {configured}"
            )
        elif error:
            self.global_hotkey_status.setText(
                f"No se pudo activar el atajo: {error}"
            )
        else:
            self.global_hotkey_status.setText("Atajo global desactivado.")
        if (
            not active
            and hasattr(self, "_ipc")
            and not self.config.get("tray_visible", True)
            and not self._window_recovery_available()
        ):
            self.set_tray_visible(True)

    def save_global_hotkey(self):
        shortcut = self.global_hotkey_edit.keySequence().toString(
            QKeySequence.PortableText
        )
        self.config["global_hotkey"] = shortcut
        save_json(CONFIG_FILE, self.config)
        self.apply_global_hotkey()

    def clear_global_hotkey(self):
        self.global_hotkey_edit.clear()
        self.save_global_hotkey()

    def current_display_options(self):
        if not hasattr(self, "display_enabled"):
            return display_options_from_config(self.config)
        return DisplayOptions(
            enabled=self.display_enabled.isChecked(),
            always_on_top=self.display_always_on_top.isChecked(),
            show_title=self.display_show_title.isChecked(),
            show_action=self.display_show_action.isChecked(),
            transparency_percent=self.display_transparency.value(),
            use_24_hour=self.display_use_24_hour.isChecked(),
        )

    def save_display_settings(self, *_args):
        previous_use_24_hour = use_24_hour_format(self.config)
        options = self.current_display_options()
        store_display_options(self.config, options)
        if hasattr(self, "compact_display"):
            self.compact_display.apply_options(options)
        if (
            previous_use_24_hour != options.use_24_hour
            and hasattr(self, "list")
        ):
            self.refresh_list()
            self.refresh_compact_display(datetime.now())
        save_json(CONFIG_FILE, self.config)

    def set_display_enabled(self, enabled):
        enabled = bool(enabled)
        for control_name in ("display_enabled", "display_tray_action"):
            control = getattr(self, control_name, None)
            if control is not None and control.isChecked() != enabled:
                control.blockSignals(True)
                control.setChecked(enabled)
                control.blockSignals(False)
        options = self.current_display_options()
        options = DisplayOptions(
            enabled=enabled,
            always_on_top=options.always_on_top,
            show_title=options.show_title,
            show_action=options.show_action,
            transparency_percent=options.transparency_percent,
            use_24_hour=options.use_24_hour,
        )
        store_display_options(self.config, options)
        if hasattr(self, "compact_display"):
            self.compact_display.apply_options(options)
            if enabled:
                self.show_compact_display()
            else:
                self.compact_display.hide()
        save_json(CONFIG_FILE, self.config)

    def display_screens(self):
        screens = []
        for screen in QApplication.screens():
            geometry = screen.availableGeometry()
            screens.append(
                DisplayScreen(
                    screen.name(),
                    geometry.x(),
                    geometry.y(),
                    geometry.width(),
                    geometry.height(),
                )
            )
        return screens

    def ensure_display_position(self):
        if not hasattr(self, "compact_display"):
            return
        x, y, screen_name = restore_display_position(
            self.config.get("display_position"),
            self.display_screens(),
        )
        self.compact_display.move(x, y)
        position = {
            "x": x,
            "y": y,
            "screen": screen_name,
        }
        if self.config.get("display_position") != position:
            self.config["display_position"] = position
            save_json(CONFIG_FILE, self.config)

    def show_compact_display(self):
        self.ensure_display_position()
        self.compact_display.show()

    def save_display_position(self, position):
        if not isinstance(position, dict):
            return
        normalized = {
            "x": int(position.get("x", 0)),
            "y": int(position.get("y", 0)),
            "screen": str(position.get("screen", "")),
        }
        if self.config.get("display_position") == normalized:
            return
        self.config["display_position"] = normalized
        save_json(CONFIG_FILE, self.config)

    # ---------------- Actividad global ----------------
    def _start_input_monitor(self):
        if not self.config.get("input_monitor_enabled", True): return
        if self.input_monitor and self.input_monitor.isRunning(): return
        self.input_monitor = InputActivityMonitor(self)
        self.input_monitor.activity.connect(self.on_input_activity)
        self.input_monitor.status.connect(self.on_input_status)
        self.input_monitor.start()

    def toggle_input_monitor(self, checked):
        self.config["input_monitor_enabled"] = bool(checked); save_json(CONFIG_FILE, self.config)
        if checked:
            self.last_activity_monotonic = time.monotonic(); self.last_activity_device = "monitor activado"; self._start_input_monitor()
        elif self.input_monitor and self.input_monitor.isRunning():
            self.input_monitor.requestInterruption()
        self.refresh_activity_label()

    def on_input_activity(self, device_name):
        self.last_activity_monotonic = time.monotonic()
        self.last_activity_device = device_name or "dispositivo de entrada"
        self.condition_engine.runtime.record_activity()

    def on_input_status(self, status):
        self.input_monitor_status = status or {}; self.refresh_activity_label()

    def idle_seconds(self):
        return max(0.0, time.monotonic() - self.last_activity_monotonic)

    def input_monitor_reliable(self):
        return self.config.get("input_monitor_enabled", True) and bool(self.input_monitor_status.get("available")) and int(self.input_monitor_status.get("accessible",0)) > 0

    def evaluate_conditions(self, s, now, occurrence=None, pending=False):
        cpu_usage = None
        cpu_average = None
        cpu_reliable = False
        cpu_status = "cpu_monitor_unavailable"
        if getattr(s, "require_cpu", False):
            monitor = getattr(self, "cpu_monitor", None)
            if monitor is not None:
                average_window = (
                    int(getattr(s, "cpu_average_seconds", 60))
                    if getattr(s, "cpu_use_average", False)
                    else 0
                )
                reading = monitor.reading(average_window)
                cpu_usage = reading.usage_percent
                cpu_average = reading.average_percent
                cpu_reliable = reading.reliable
                cpu_status = reading.reason
        network_rx = None
        network_tx = None
        network_speed = None
        network_average = None
        network_reliable = False
        network_status = "network_monitor_unavailable"
        if getattr(s, "require_network", False):
            monitor = getattr(self, "network_monitor", None)
            if monitor is not None:
                average_window = (
                    int(getattr(s, "network_average_seconds", 60))
                    if getattr(s, "network_use_average", False)
                    else 0
                )
                reading = monitor.reading(
                    getattr(s, "network_interface", ""),
                    getattr(s, "network_direction", "both"),
                    average_window,
                )
                network_rx = reading.rx_bytes_per_second
                network_tx = reading.tx_bytes_per_second
                network_speed = reading.speed_bytes_per_second
                network_average = reading.average_bytes_per_second
                network_reliable = reading.reliable
                network_status = reading.reason
        context = ConditionContext(
            now=now,
            occurrence=occurrence,
            occurrence_pending=pending,
            idle_seconds=self.idle_seconds(),
            idle_reliable=self.input_monitor_reliable(),
            cpu_usage=cpu_usage,
            cpu_average=cpu_average,
            cpu_reliable=cpu_reliable,
            cpu_status=cpu_status,
            network_rx_bytes_per_second=network_rx,
            network_tx_bytes_per_second=network_tx,
            network_speed_bytes_per_second=network_speed,
            network_average_bytes_per_second=network_average,
            network_reliable=network_reliable,
            network_status=network_status,
        )
        return self.condition_engine.evaluate(s, context)

    def pending_occurrence(self, s):
        raw = self.state.get("pending_occurrences", {}).get(s.id)

        if not raw:
            return None

        try:
            return ScheduledOccurrence.from_state(s.id, raw)
        except Exception as e:
            log(f"Ocurrencia pendiente inválida para {s.id}: {e}")
            self.state.get("pending_occurrences", {}).pop(s.id, None)
            save_json(STATE_FILE, self.state)
            return None

    def pending_action_time(self, s, occurrence, now):
        internal_due = None
        if occurrence.next_check_at:
            if occurrence.next_check_at <= occurrence.scheduled_target:
                internal_due = occurrence.scheduled_target
            else:
                internal_due = occurrence.next_check_at + timedelta(
                    seconds=int(s.final_countdown_seconds)
                )

        snooze_iso = self.state.get("snoozes", {}).get(s.id)
        if snooze_iso:
            try:
                snooze_due = datetime.fromisoformat(snooze_iso)
                return max(snooze_due, internal_due or snooze_due)
            except Exception:
                pass

        if internal_due:
            return internal_due

        if occurrence.scheduled_target > now:
            return occurrence.scheduled_target

        return now + timedelta(seconds=int(s.final_countdown_seconds))

    def save_pending_occurrence(self, occurrence):
        self.state.setdefault("pending_occurrences", {})[
            occurrence.schedule_id
        ] = occurrence.to_state()
        save_json(STATE_FILE, self.state)

    def refresh_activity_label(self):
        if not hasattr(self, "activity_label"): return
        idle = int(self.idle_seconds()); mins, secs = divmod(idle, 60)
        accessible = int(self.input_monitor_status.get("accessible",0)); denied = int(self.input_monitor_status.get("denied",0))
        if evdev is None: status = "evdev no está instalado"
        elif accessible <= 0: status = "sin acceso a dispositivos /dev/input"
        else:
            status = f"{accessible} dispositivo(s) monitorizado(s)"
            if denied: status += f"; {denied} sin permiso"
        self.activity_label.setText(f"<b>Estado:</b> {status}<br><b>Inactividad actual:</b> {mins} min {secs} s<br><b>Última actividad:</b> {self.last_activity_device}")

    def _defer_for_conditions(self, s, occurrence, now, evaluation):
        delays = []
        reasons = []
        idle_result = evaluation.for_type("idle")
        if idle_result and idle_result.enabled and not idle_result.satisfied:
            threshold = max(60, int(s.idle_minutes) * 60)
            idle = int(self.idle_seconds())
            if self.input_monitor_reliable():
                delays.append(max(60, threshold - idle + 2))
                reasons.append(
                    f"Se detectó actividad; se requieren {s.idle_minutes} min "
                    "sin usar mouse, teclado o mando."
                )
            else:
                delays.append(300)
                reasons.append(
                    "No se puede comprobar la inactividad por falta de acceso "
                    "al monitor de entrada."
                )

        cpu_result = evaluation.for_type("cpu")
        if cpu_result and cpu_result.enabled and not cpu_result.satisfied:
            info = cpu_result.info
            if not info.get("reliable"):
                delays.append(30)
                reasons.append(
                    f"La condición CPU no está disponible ({cpu_result.reason})."
                )
            elif info.get("threshold_met"):
                elapsed = cpu_result.satisfied_for_seconds or 0
                missing = max(
                    1,
                    int(info.get("minimum_seconds", 0) - elapsed + 1),
                )
                delays.append(missing)
                reasons.append("La CPU aún no cumple la duración continua.")
            else:
                delays.append(30)
                comparison = (
                    "mayor que"
                    if info.get("comparison") == "greater"
                    else "menor que"
                )
                reasons.append(
                    f"La CPU debe ser {comparison} {info.get('threshold'):g} %."
                )

        network_result = evaluation.for_type("network")
        if (
            network_result
            and network_result.enabled
            and not network_result.satisfied
        ):
            info = network_result.info
            if not info.get("reliable"):
                delays.append(30)
                reasons.append(
                    "La condición de red no está disponible "
                    f"({network_result.reason})."
                )
            elif info.get("threshold_met"):
                elapsed = network_result.satisfied_for_seconds or 0
                missing = max(
                    1,
                    int(info.get("minimum_seconds", 0) - elapsed + 1),
                )
                delays.append(missing)
                reasons.append("La red aún no cumple la duración continua.")
            else:
                delays.append(30)
                comparison = (
                    "mayor que"
                    if info.get("comparison") == "greater"
                    else "menor que"
                )
                threshold_kib = float(info.get("threshold", 0)) / 1024
                reasons.append(
                    f"La red debe ser {comparison} {threshold_kib:g} KB/s."
                )

        if not reasons:
            return
        dt = now + timedelta(seconds=min(delays))
        if now >= occurrence.scheduled_target:
            occurrence = occurrence.mark_armed()
        occurrence = occurrence.with_next_check(dt)
        self.save_pending_occurrence(occurrence)
        reason = f"«{s.name}»: {' '.join(reasons)}"
        notified = getattr(self, "_condition_wait_notified", {})
        previous_notice = notified.get(s.id)
        should_notify = (
            previous_notice is None
            or previous_notice[0] != reason
            or (now - previous_notice[1]).total_seconds() >= 300
        )
        if should_notify:
            notified[s.id] = (reason, now)
            self._condition_wait_notified = notified
            self.notify(
                "Esperando condiciones",
                f"{reason} Próxima comprobación: "
                f"{format_app_time(self.config, dt, True)}.",
                True,
            )
            if self.config.get("overlay_all_schedule_warnings", True):
                self.show_warning_banner(
                    "AMP AutoPower — esperando condiciones",
                    reason,
                    10000,
                )

    def defer_for_idle(self, s, occurrence, now):
        evaluation = self.evaluate_conditions(
            s,
            now,
            occurrence,
            pending=True,
        )
        self._defer_for_conditions(s, occurrence, now, evaluation)

    def _has_active_dialog_for_schedule(self, schedule_id):
        for dlg in self.active_dialogs.values():
            if getattr(getattr(dlg, "schedule", None), "id", None) == schedule_id:
                return True
        for context in getattr(
            self,
            "_pre_action_processes",
            {},
        ).values():
            if context["schedule"].id == schedule_id:
                return True
        return False

    def _idle_only_tick(self, s, now):
        evaluation = self.evaluate_conditions(s, now)

        if not evaluation.weekday_allowed:
            return

        if self._has_active_dialog_for_schedule(s.id):
            return

        # Si había un aplazamiento y después hubo actividad real,
        # cancelar el aplazamiento y comenzar un ciclo de inactividad nuevo.
        snooze_iso = self.state.get("snoozes", {}).get(s.id)

        if snooze_iso:
            if self.condition_engine.runtime.idle_snooze_was_invalidated(s.id):
                self.state.get("snoozes", {}).pop(s.id, None)
                self.condition_engine.runtime.clear_idle_snooze(s.id)
                save_json(STATE_FILE, self.state)
                snooze_iso = None

        if snooze_iso:
            try:
                snooze_dt = datetime.fromisoformat(snooze_iso)

                if snooze_dt > now:
                    return

                self.state.get("snoozes", {}).pop(s.id, None)
                self.condition_engine.runtime.clear_idle_snooze(s.id)
                save_json(STATE_FILE, self.state)

            except Exception:
                self.state.get("snoozes", {}).pop(s.id, None)
                self.condition_engine.runtime.clear_idle_snooze(s.id)
                save_json(STATE_FILE, self.state)

        if not evaluation.ready:
            return

        # Ya se mostró/ejecutó durante este mismo ciclo de inactividad.
        if self.condition_engine.runtime.idle_cycle_was_triggered(s.id):
            return

        self.condition_engine.runtime.mark_idle_cycle_triggered(s.id)

        target = now
        key = (
            f"idle:{s.id}:"
            f"{self.condition_engine.runtime.activity_generation}"
        )

        self.start_final_countdown(
            s,
            target,
            int(s.final_countdown_seconds),
            key,
        )

    # ---------------- Overlays de aviso ----------------
    def show_warning_banner(self, title, body, lifetime_ms=12000):
        if not self.config.get("fullscreen_overlay", True): return
        screens = QApplication.screens() if self.config.get("overlay_all_screens", True) else [QApplication.primaryScreen()]
        for screen in screens:
            if screen is None: continue
            banner = WarningBanner(screen,title,body,lifetime_ms); self.banner_windows.append(banner); banner.closed.connect(self._remove_banner); banner.show(); banner.raise_()

    def _remove_banner(self, banner):
        try: self.banner_windows.remove(banner)
        except ValueError: pass
        banner.deleteLater()

    def refresh_list(self):
        self.list.clear()

        for s in self.schedules():
            mode = schedule_trigger_mode(s)
            if mode == "interval":
                days = "Una vez"
            else:
                days = (
                    "Todos"
                    if len(s.weekdays) == 7
                    else ", ".join(WEEKDAYS[i] for i in s.weekdays)
                )

            status = "✓" if s.enabled else "✗"

            if mode == "time":
                trigger = format_app_time(self.config, s.time)
            elif mode == "interval":
                trigger = f"Intervalo {format_interval(s.interval_minutes)}"
            else:
                trigger = f"Inactividad {s.idle_minutes} min"

            text = (
                f"{status}  {trigger} — {s.name} — "
                f"{ACTIONS.get(s.action, s.action)} — {days}"
            )

            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, s.id)
            self.list.addItem(item)

        self.update_next_label()

    def get_selected_id(self):
        item = self.list.currentItem()
        return item.data(Qt.UserRole) if item else None

    def add_schedule(self):
        dlg = ScheduleEditor(self)
        if dlg.exec() == QDialog.Accepted:
            ss = self.schedules()
            added = dlg.get_schedule()
            ss.append(added)
            restart_ids = (
                {added.id}
                if schedule_trigger_mode(added) == "interval"
                else None
            )
            self.set_schedules(ss, restart_interval_ids=restart_ids)

    def edit_schedule(self):
        sid = self.get_selected_id()
        if not sid:
            return
        ss = self.schedules()
        target = next((s for s in ss if s.id == sid), None)
        if not target:
            return
        dlg = ScheduleEditor(self, target)
        if dlg.exec() == QDialog.Accepted:
            updated = dlg.get_schedule()
            ss = [updated if s.id == sid else s for s in ss]
            restart_ids = (
                {sid}
                if updated.enabled
                and schedule_trigger_mode(updated) == "interval"
                else None
            )
            self.set_schedules(ss, restart_interval_ids=restart_ids)

    def delete_schedule(self):
        sid = self.get_selected_id()
        if not sid:
            return
        if QMessageBox.question(self, "Eliminar", "¿Eliminar esta programación?") == QMessageBox.Yes:
            self.set_schedules([s for s in self.schedules() if s.id != sid])

    def next_occurrence(self, s: Schedule, now=None):
        if schedule_trigger_mode(s) != "time":
            return None

        now = now or datetime.now()
        hh, mm = map(int, s.time.split(":"))
        snooze_iso = self.state.get("snoozes", {}).get(s.id)
        if snooze_iso:
            try:
                snooze_dt = datetime.fromisoformat(snooze_iso)
                if snooze_dt >= now:
                    return snooze_dt
                self.state.get("snoozes", {}).pop(s.id, None)
                save_json(STATE_FILE, self.state)
            except Exception:
                pass
        skipped_iso = self.state.get("skipped_targets", {}).get(s.id)
        last_run_iso = self.state.get("last_runs", {}).get(s.id)
        for delta in range(0, 8):
            day = now.date() + timedelta(days=delta)
            candidate = datetime.combine(day, datetime.min.time()).replace(hour=hh, minute=mm)
            if candidate.weekday() not in s.weekdays or candidate < now:
                continue
            if (
                skipped_iso == candidate.isoformat()
                or last_run_iso == candidate.isoformat()
            ):
                continue
            return candidate
        return None

    def update_next_label(self):
        now = datetime.now()
        candidates = []
        idle_modes = []

        for s in self.schedules():
            if not s.enabled:
                continue

            mode = schedule_trigger_mode(s)
            if mode == "idle":
                if now.weekday() in s.weekdays:
                    idle_modes.append(s)
                continue

            pending = self.pending_occurrence(s)
            if pending:
                nxt = self.pending_action_time(s, pending, now)
            else:
                nxt = self.next_occurrence(s, now)

            if nxt:
                candidates.append((nxt, s))

        parts = []

        if candidates:
            nxt, s = min(candidates, key=lambda x: x[0])
            delta = nxt - now
            sec = max(0, int(delta.total_seconds()))
            h, rem = divmod(sec, 3600)
            m, _ = divmod(rem, 60)

            parts.append(
                f"Próxima acción: "
                f"<b>{ACTIONS.get(s.action, s.action)}</b> — "
                f"<b>{format_app_datetime(self.config, nxt)}</b> — "
                f"faltan {h} h {m} min"
            )

        if idle_modes:
            details = "; ".join(
                f"{ACTIONS.get(s.action, s.action)} tras "
                f"{s.idle_minutes} min"
                for s in idle_modes[:3]
            )

            if len(idle_modes) > 3:
                details += f"; +{len(idle_modes) - 3} más"

            parts.append(
                f"Modo por inactividad activo: <b>{details}</b>"
            )

        if not parts:
            self.status_label.setText(
                "No hay ninguna acción activa programada."
            )
            return

        self.status_label.setText("<br>".join(parts))

    def notify(self, title, body, critical=False):
        log(f"Aviso: {title} | {body}")
        if self.config.get("notifications", True):
            self.tray.showMessage(title, body, QSystemTrayIcon.Warning if critical else QSystemTrayIcon.Information, 12000)
            if shutil.which("notify-send"):
                urgency = "critical" if critical else "normal"
                subprocess.Popen(["notify-send", "-u", urgency, "-a", APP_NAME, title, body])
        if self.config.get("sound", True):
            for player, sound in [
                ("canberra-gtk-play", ["-i", "dialog-warning"]),
                ("paplay", ["/usr/share/sounds/freedesktop/stereo/alarm-clock-elapsed.oga"]),
            ]:
                if shutil.which(player):
                    try:
                        subprocess.Popen([player] + sound, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    except Exception:
                        pass
                    break

    def _pending_occurrence_tick(self, s, occurrence, now):
        if self._has_active_dialog_for_schedule(s.id):
            return

        completed = self.state.get("last_runs", {}).get(s.id)
        if completed == occurrence.scheduled_target.isoformat():
            self.state.get("pending_occurrences", {}).pop(s.id, None)
            self.state.get("snoozes", {}).pop(s.id, None)
            if schedule_trigger_mode(s) == "interval":
                self._complete_interval_schedule(s, occurrence.scheduled_target)
            save_json(STATE_FILE, self.state)
            return

        snooze_dt = None
        snooze_expired = False
        snooze_iso = self.state.get("snoozes", {}).get(s.id)
        if snooze_iso:
            try:
                snooze_dt = datetime.fromisoformat(snooze_iso)
            except Exception:
                snooze_dt = None

            if snooze_dt is None or snooze_dt <= now:
                snooze_expired = True
                self.state.get("snoozes", {}).pop(s.id, None)
                save_json(STATE_FILE, self.state)

        if now >= occurrence.scheduled_target and not occurrence.armed:
            occurrence = occurrence.mark_armed()
            self.save_pending_occurrence(occurrence)

        evaluation = self.evaluate_conditions(
            s,
            now,
            occurrence,
            pending=True,
        )

        if snooze_dt and snooze_dt > now:
            snooze_countdown_start = snooze_dt - timedelta(
                seconds=int(s.final_countdown_seconds)
            )
            if now < snooze_countdown_start:
                return
            if evaluation.ready_for_countdown:
                remaining = (snooze_dt - now).total_seconds()
                key = f"{occurrence.scheduled_target.isoformat()}:{s.id}"
                self.start_final_countdown(
                    s,
                    occurrence.scheduled_target,
                    int(max(1, remaining)),
                    key,
                )
            elif (
                occurrence.next_check_at is None
                or now >= occurrence.next_check_at
            ):
                self._defer_for_conditions(s, occurrence, now, evaluation)
            return

        # Si una condición se completa durante la ventana previa, todavía
        # podemos terminar el countdown en la hora original.
        if now < occurrence.scheduled_target:
            if evaluation.ready_for_countdown:
                remaining = (occurrence.scheduled_target - now).total_seconds()
                countdown_seconds = (
                    int(max(1, remaining))
                    if evaluation.countdown_due
                    else int(s.final_countdown_seconds)
                )
                key = f"{occurrence.scheduled_target.isoformat()}:{s.id}"
                self.start_final_countdown(
                    s,
                    occurrence.scheduled_target,
                    countdown_seconds,
                    key,
                )
            return

        if evaluation.ready_for_countdown:
            getattr(self, "_condition_wait_notified", {}).pop(s.id, None)
            if occurrence.next_check_at:
                occurrence = occurrence.with_next_check(None)
                self.save_pending_occurrence(occurrence)
            key = f"{occurrence.scheduled_target.isoformat()}:{s.id}"
            self.start_final_countdown(
                s,
                occurrence.scheduled_target,
                int(s.final_countdown_seconds),
                key,
            )
            return

        if (
            occurrence.next_check_at
            and now < occurrence.next_check_at
            and not snooze_expired
        ):
            return

        self._defer_for_conditions(s, occurrence, now, evaluation)

    def _timed_schedule_tick(self, s, now):
        pending = self.pending_occurrence(s)
        if pending:
            self._pending_occurrence_tick(s, pending, now)
            return

        target = self.next_occurrence(
            s,
            now - timedelta(seconds=2),
        )
        if not target:
            return

        occurrence = ScheduledOccurrence.create(
            s.id,
            target,
            int(s.final_countdown_seconds),
            created_at=now,
        )
        remaining = (target - now).total_seconds()

        for mins in s.warning_minutes:
            if (
                0 < remaining <= mins * 60
                and remaining > mins * 60 - 2.5
            ):
                key = f"{target.date()}:{s.id}:warn:{mins}"

                if key not in self.warned:
                    self.warned.add(key)
                    warning_title = (
                        f"{ACTIONS.get(s.action, s.action)} programado"
                    )
                    warning_body = (
                        f"La PC ejecutará "
                        f"«{ACTIONS.get(s.action, s.action)}» "
                        f"en {mins} minuto(s), a las "
                        f"{format_app_time(self.config, target)}. "
                        f"Abre {APP_NAME} para cancelar o cambiarlo."
                    )
                    self.notify(
                        warning_title,
                        warning_body,
                        critical=mins <= 5,
                    )
                    if self.config.get(
                        "overlay_all_schedule_warnings",
                        True,
                    ):
                        self.show_warning_banner(
                            warning_title,
                            warning_body,
                            12000 if mins <= 5 else 9000,
                        )

        evaluation = self.evaluate_conditions(s, now, occurrence)

        if (
            evaluation.ready_for_countdown
            and not evaluation.countdown_due
        ):
            key = f"{target.isoformat()}:{s.id}"
            if key not in self.active_dialogs:
                self.start_final_countdown(
                    s,
                    target,
                    int(s.final_countdown_seconds),
                    key,
                )
            return

        if not 0 < remaining <= s.final_countdown_seconds:
            return

        key = f"{target.isoformat()}:{s.id}"
        if key in self.active_dialogs:
            return

        if not evaluation.ready_for_countdown:
            self._defer_for_conditions(s, occurrence, now, evaluation)
            return

        self.start_final_countdown(
            s,
            target,
            int(max(1, remaining)),
            key,
        )

    def _display_snapshot(
        self,
        schedule,
        now,
        occurrence,
        priority,
        due=None,
        pending=False,
    ):
        mode = schedule_trigger_mode(schedule)
        target = occurrence.scheduled_target if occurrence else None
        cpu_value = None
        cpu_reliable = False
        if schedule.require_cpu:
            average_window = (
                int(schedule.cpu_average_seconds)
                if schedule.cpu_use_average
                else 0
            )
            reading = self.cpu_monitor.reading(average_window)
            cpu_value = (
                reading.average_percent
                if schedule.cpu_use_average
                else reading.usage_percent
            )
            cpu_reliable = reading.reliable and cpu_value is not None

        network_value = None
        network_reliable = False
        if schedule.require_network:
            average_window = (
                int(schedule.network_average_seconds)
                if schedule.network_use_average
                else 0
            )
            reading = self.network_monitor.reading(
                schedule.network_interface,
                schedule.network_direction,
                average_window,
            )
            network_value = (
                reading.average_bytes_per_second
                if schedule.network_use_average
                else reading.speed_bytes_per_second
            )
            network_reliable = reading.reliable and network_value is not None

        options = self.current_display_options()
        conditions = build_display_conditions(
            mode=mode,
            scheduled_target=target,
            now=now,
            use_24_hour=options.use_24_hour,
            pending=pending,
            idle_enabled=(mode != "idle" and schedule.require_idle),
            idle_seconds=self.idle_seconds(),
            idle_reliable=self.input_monitor_reliable(),
            cpu_enabled=schedule.require_cpu,
            cpu_value=cpu_value,
            cpu_reliable=cpu_reliable,
            network_enabled=schedule.require_network,
            network_value=network_value,
            network_reliable=network_reliable,
        )
        return DisplaySnapshot(
            schedule_id=schedule.id,
            title=schedule.name,
            action=ACTIONS.get(schedule.action, schedule.action),
            logic=schedule.condition_logic,
            conditions=conditions,
            priority=priority,
            due=due,
        )

    def display_snapshots(self, now, schedules=None):
        schedules = schedules if schedules is not None else self.schedules()
        enabled_by_id = {schedule.id: schedule for schedule in schedules if schedule.enabled}
        snapshots = []
        active_ids = set()
        for dialog in self.active_dialogs.values():
            schedule = getattr(dialog, "schedule", None)
            target = getattr(dialog, "scheduled_target", None)
            if schedule is None or target is None:
                continue
            active_ids.add(schedule.id)
            occurrence = self.pending_occurrence(schedule)
            if occurrence is None:
                occurrence = ScheduledOccurrence.create(
                    schedule.id,
                    target,
                    int(schedule.final_countdown_seconds),
                    created_at=now,
                )
            snapshots.append(
                self._display_snapshot(
                    schedule,
                    now,
                    occurrence,
                    0,
                    due=now + timedelta(seconds=max(0, dialog.remaining)),
                    pending=now >= occurrence.scheduled_target,
                )
            )

        for schedule in enabled_by_id.values():
            if schedule.id in active_ids:
                continue
            mode = schedule_trigger_mode(schedule)
            if mode != "interval" and not schedule.weekdays:
                continue
            occurrence = self.pending_occurrence(schedule)
            if occurrence is not None:
                waiting = (
                    mode != "interval"
                    or occurrence.armed
                    or occurrence.next_check_at is not None
                    or now >= occurrence.scheduled_target
                    or schedule.id in self.state.get("snoozes", {})
                )
                priority = 1 if waiting else 3
                due = (
                    self.pending_action_time(schedule, occurrence, now)
                    if waiting
                    else occurrence.scheduled_target
                )
                snapshots.append(
                    self._display_snapshot(
                        schedule,
                        now,
                        occurrence,
                        priority,
                        due=due,
                        pending=waiting and now >= occurrence.scheduled_target,
                    )
                )
                continue
            if mode == "time":
                target = self.next_occurrence(schedule, now)
                if target is None:
                    continue
                occurrence = ScheduledOccurrence.create(
                    schedule.id,
                    target,
                    int(schedule.final_countdown_seconds),
                    created_at=now,
                )
                snapshots.append(
                    self._display_snapshot(
                        schedule,
                        now,
                        occurrence,
                        2,
                        due=target,
                    )
                )
                continue
            snapshots.append(
                self._display_snapshot(schedule, now, None, 4)
            )
        return snapshots

    def refresh_compact_display(self, now, schedules=None):
        if not hasattr(self, "compact_display"):
            return
        self.compact_display.set_snapshots(
            self.display_snapshots(now, schedules)
        )

    def scheduler_tick(self):
        now = datetime.now()
        dayprefix = now.strftime("%Y-%m-%d")

        self.warned = {
            x for x in self.warned
            if dayprefix in x
        }

        schedules = self.schedules()
        if any(s.enabled and s.require_cpu for s in schedules):
            cpu_reading = self.cpu_monitor.sample()
            if (
                not cpu_reading.reliable
                and cpu_reading.reason.startswith("cpu_monitor_error")
                and cpu_reading.reason != self._last_cpu_monitor_error
            ):
                log(f"Monitor CPU no disponible: {cpu_reading.reason}")
                self._last_cpu_monitor_error = cpu_reading.reason
            elif cpu_reading.reliable:
                self._last_cpu_monitor_error = None
        if any(s.enabled and s.require_network for s in schedules):
            network_status = self.network_monitor.sample()
            if (
                not network_status.reliable
                and network_status.reason.startswith("network_monitor_error")
                and network_status.reason != self._last_network_monitor_error
            ):
                log(f"Monitor de red no disponible: {network_status.reason}")
                self._last_network_monitor_error = network_status.reason
            elif network_status.reliable:
                self._last_network_monitor_error = None

        for s in schedules:
            if not s.enabled:
                continue

            mode = schedule_trigger_mode(s)
            if mode != "interval" and not s.weekdays:
                continue
            if mode == "idle":
                self._idle_only_tick(s, now)
                continue
            if mode == "interval":
                occurrence = self.pending_occurrence(s)
                if occurrence is None:
                    completed = s.id in self.state.get(
                        "completed_intervals",
                        {},
                    )
                    if not completed:
                        occurrence = self._start_interval_occurrence(s, now)
                if occurrence is not None:
                    self._pending_occurrence_tick(s, occurrence, now)
                continue

            self._timed_schedule_tick(s, now)

        self.update_next_label()
        self.refresh_compact_display(now, schedules)

    def start_final_countdown(self, s, target, seconds, key):
        self.notify(
            "Acción inminente",
            f"{ACTIONS.get(s.action, s.action)} en {seconds} segundos. Puedes cancelar o posponer.",
            critical=True,
        )
        dlg = CountdownDialog(self, s, seconds)
        dlg.scheduled_target = target
        self.active_dialogs[key] = dlg
        dlg.finished.connect(lambda _=None, k=key, d=dlg, sc=s, tg=target: self.on_countdown_finished(k, d, sc, tg))
        # El overlay independiente debe aparecer sobre el juego; no elevamos la ventana principal.
        dlg.show()

    def on_countdown_finished(self, key, dlg, s, target):
        self.active_dialogs.pop(key, None)
        result = dlg.result_action
        if result == "cancel":
            self.mark_skipped(s, target)
            self.notify("Acción cancelada", f"«{s.name}» fue cancelada para esta ocasión.")
            if (
                getattr(dlg, "cancelled_by_user", False)
                and not getattr(dlg, "cancel_command_executed", False)
            ):
                dlg.cancel_command_executed = True
                self._run_cancel_action_command(s)
        elif result == "snooze10":
            self.snooze(s, 10, target)
        elif result == "snooze30":
            self.snooze(s, 30, target)
        else:
            self.execute_action(s, target)

    def _run_cancel_action_command(self, s):
        if getattr(s, "cancel_action_behavior", "none") != "command":
            return False
        command = getattr(s, "cancel_action_command", "").strip()
        try:
            args = shlex.split(command)
        except ValueError as exc:
            error = f"El comando al cancelar no es válido: {exc}"
            log(error)
            self.notify("No se ejecutó el comando al cancelar", error, True)
            return False
        if not args:
            error = "No se configuró ningún comando al cancelar."
            log(error)
            self.notify("No se ejecutó el comando al cancelar", error, True)
            return False

        try:
            started = QProcess.startDetached(args[0], args[1:])
            if isinstance(started, tuple):
                started = started[0]
        except Exception as exc:
            log(f"No se pudo iniciar el comando al cancelar: {exc}")
            self.notify(
                "No se ejecutó el comando al cancelar",
                str(exc),
                True,
            )
            return False
        if not started:
            error = f"No se pudo iniciar «{args[0]}»."
            log(error)
            self.notify("No se ejecutó el comando al cancelar", error, True)
            return False
        log("Ejecutando comando al cancelar: " + " ".join(args))
        return True

    def mark_skipped(self, s, target):
        self.state.setdefault("last_runs", {})[s.id] = target.isoformat() + ":skipped"
        self.state.setdefault("skipped_targets", {})[s.id] = target.isoformat()
        self.state.get("snoozes", {}).pop(s.id, None)
        self.state.get("pending_occurrences", {}).pop(s.id, None)
        self._complete_interval_schedule(s, target)
        save_json(STATE_FILE, self.state)

    def _complete_interval_schedule(self, s, target):
        if schedule_trigger_mode(s) != "interval":
            return

        matching = next(
            (
                raw
                for raw in self.config.get("schedules", [])
                if (
                    isinstance(raw, dict)
                    and raw.get("id") == s.id
                    and schedule_trigger_mode(raw) == "interval"
                )
            ),
            None,
        )
        if matching is None:
            return

        self.state.setdefault("completed_intervals", {})[
            s.id
        ] = target.isoformat()
        changed = False
        if matching.get("enabled", True):
            matching["enabled"] = False
            changed = True
        if changed:
            save_json(CONFIG_FILE, self.config)
        if hasattr(self, "list"):
            self.refresh_list()

    def snooze(self, s, minutes, target=None):
        now = datetime.now()
        dt = now + timedelta(minutes=minutes)

        self.state.setdefault("snoozes", {})[s.id] = dt.isoformat()

        if schedule_trigger_mode(s) != "idle":
            occurrence = self.pending_occurrence(s)
            if occurrence is None and target is not None:
                if schedule_trigger_mode(s) == "interval":
                    start_at = target - timedelta(
                        minutes=max(1, int(s.interval_minutes))
                    )
                    occurrence = ScheduledOccurrence.create_interval(
                        s.id,
                        start_at,
                        int(s.interval_minutes),
                        int(s.final_countdown_seconds),
                    )
                else:
                    occurrence = ScheduledOccurrence.create(
                        s.id,
                        target,
                        int(s.final_countdown_seconds),
                        created_at=now,
                    )
                if now >= target:
                    occurrence = occurrence.mark_armed()
            if occurrence is not None:
                self.state.setdefault("pending_occurrences", {})[
                    s.id
                ] = occurrence.with_next_check(None).to_state()
        else:
            # Permitir que se vuelva a mostrar al terminar el aplazamiento,
            # siempre que el usuario no haya vuelto a usar la PC.
            self.condition_engine.runtime.clear_idle_cycle_triggered(s.id)
            self.condition_engine.runtime.mark_idle_snoozed(s.id)

        save_json(STATE_FILE, self.state)

        self.notify(
            "Acción pospuesta",
            f"«{s.name}» se ejecutará a las "
            f"{format_app_time(self.config, dt)}, "
            f"en {minutes} minutos.",
        )

    def _chrome_main_processes(self):
        """Devuelve solamente procesos principales de Google Chrome."""
        found = []
        nul = bytes((0,))

        try:
            entries = list(Path("/proc").iterdir())
        except OSError:
            return found

        for proc in entries:
            if not proc.name.isdigit():
                continue

            try:
                exe = os.path.realpath(proc / "exe")

                if exe != "/opt/google/chrome/chrome":
                    continue

                raw = (proc / "cmdline").read_bytes()

                if not raw:
                    continue

                fields = [
                    field
                    for field in raw.split(nul)
                    if field
                ]

                if not fields:
                    continue

                # Todo proceso secundario de Chromium contiene
                # --type= en su cmdline: renderer, zygote,
                # gpu-process, utility, etc.
                #
                # Comprobamos el cmdline bruto porque en algunas
                # ejecuciones no podemos depender de cómo queden
                # separados los argumentos en /proc/PID/cmdline.
                if b"--type=" in raw:
                    continue

                args = [
                    field.decode("utf-8", "replace")
                    for field in fields
                ]

                found.append((int(proc.name), args))

            except (
                OSError,
                PermissionError,
                ValueError,
            ):
                continue

        return found

    def _close_chrome_cleanly(self):
        """
        Pide a Chrome una salida normal mediante SIGHUP.

        Esto reproduce el comportamiento que comprobamos manualmente:
        Chrome guarda correctamente sus ventanas/pestañas antes de salir.

        Nunca se usa SIGKILL.
        """
        processes = self._chrome_main_processes()

        if not processes:
            return True

        pids = [pid for pid, _args in processes]

        self.notify(
            "Cerrando Chrome",
            (
                f"Solicitando a {len(pids)} proceso(s) principal(es) de "
                "Chrome que guarden sus ventanas y salgan correctamente."
            ),
        )

        failed = []

        for pid in pids:
            try:
                os.kill(pid, signal.SIGHUP)

            except ProcessLookupError:
                # Ya había terminado.
                continue

            except (PermissionError, OSError) as exc:
                failed.append((pid, str(exc)))

        if failed:
            details = ", ".join(
                f"PID {pid}: {error}"
                for pid, error in failed
            )

            self.notify(
                "No se ejecutó la acción",
                (
                    "No se pudo solicitar un cierre correcto de Chrome. "
                    "Por seguridad no se apagó ni reinició la PC.\n\n"
                    f"{details}"
                ),
                True,
            )
            return False

        # Chrome normalmente tarda muy poco, pero le damos hasta 15 s
        # para escribir su estado de sesión y terminar.
        deadline = time.monotonic() + 15.0
        remaining = set(pids)

        while remaining and time.monotonic() < deadline:
            still_running = set()

            for pid in remaining:
                try:
                    proc_dir = Path(f"/proc/{pid}")

                    if not proc_dir.exists():
                        continue

                    exe = os.path.realpath(proc_dir / "exe")

                    if (
                        exe == "/opt/google/chrome/chrome"
                        or Path(exe).name == "chrome"
                    ):
                        still_running.add(pid)

                except OSError:
                    continue

            remaining = still_running

            if remaining:
                time.sleep(0.20)

        if remaining:
            pid_text = ", ".join(
                str(pid)
                for pid in sorted(remaining)
            )

            self.notify(
                "No se ejecutó la acción",
                (
                    "Chrome no terminó correctamente después de 15 "
                    "segundos. No se forzó su cierre y tampoco se "
                    "apagó/reinició la PC para evitar perder el estado "
                    f"de las ventanas. PID pendientes: {pid_text}"
                ),
                True,
            )
            return False

        # Pequeño margen para que los últimos datos escritos lleguen
        # completamente al almacenamiento.
        time.sleep(0.5)

        self.notify(
            "Chrome cerrado correctamente",
            (
                "Chrome guardó su estado y terminó. "
                "Ahora Plasma puede cerrar el resto de la sesión."
            ),
        )

        return True

    def _record_action_completion(self, s, target):
        self.state.setdefault("last_runs", {})[s.id] = target.isoformat()
        self.state.get("snoozes", {}).pop(s.id, None)
        self.state.get("pending_occurrences", {}).pop(s.id, None)
        self._complete_interval_schedule(s, target)
        save_json(STATE_FILE, self.state)

    def _guard_interval_execution(self, s, target):
        if schedule_trigger_mode(s) != "interval":
            return
        self._complete_interval_schedule(s, target)
        save_json(STATE_FILE, self.state)

    def _restore_failed_interval_execution(self, s):
        if schedule_trigger_mode(s) != "interval":
            return
        self.state.get("completed_intervals", {}).pop(s.id, None)
        changed = False
        for raw in self.config.get("schedules", []):
            if (
                isinstance(raw, dict)
                and raw.get("id") == s.id
                and schedule_trigger_mode(raw) == "interval"
            ):
                if not raw.get("enabled", True):
                    raw["enabled"] = True
                    changed = True
                break
        if changed:
            save_json(CONFIG_FILE, self.config)
        save_json(STATE_FILE, self.state)
        if hasattr(self, "list"):
            self.refresh_list()

    def _handle_pre_action_failure(self, s, target, reason):
        log(f"Falló el programa previo de «{s.name}»: {reason}")
        if getattr(s, "pre_action_failure_policy", "cancel") == "continue":
            self.notify(
                "Falló el programa previo",
                f"{reason}\n\nLa acción programada continuará.",
                True,
            )
            self._execute_final_action(s, target)
            return

        if schedule_trigger_mode(s) != "interval":
            self.mark_skipped(s, target)
        self.notify(
            "Acción cancelada",
            f"El programa previo falló y la acción no se ejecutará.\n\n{reason}",
            True,
        )

    def _finish_pre_action(self, process, exit_code=None, error=None):
        contexts = getattr(self, "_pre_action_processes", {})
        context = contexts.pop(id(process), None)
        if context is None:
            return

        context["timer"].stop()
        if context.get("kill_timer") is not None:
            context["kill_timer"].stop()

        s = context["schedule"]
        target = context["target"]
        timed_out = context.get("timed_out", False)
        cancelled = context.get("cancelled", False)
        stderr = bytes(process.readAllStandardError()).decode(
            "utf-8",
            "replace",
        ).strip()
        stdout = bytes(process.readAllStandardOutput()).decode(
            "utf-8",
            "replace",
        ).strip()
        error_text = process.errorString() if error is not None else ""
        process.deleteLater()

        if cancelled:
            return
        if timed_out:
            reason = (
                "El programa previo superó el timeout de "
                f"{context['timeout_seconds']} segundos."
            )
        elif error is not None:
            reason = error_text or str(error)
        elif exit_code != 0:
            reason = stderr or stdout or f"Terminó con código {exit_code}."
        else:
            self._execute_final_action(s, target)
            return

        self._handle_pre_action_failure(s, target, reason)

    def _cancel_pre_action(self, process):
        context = getattr(self, "_pre_action_processes", {}).get(id(process))
        if context is None:
            return
        context["cancelled"] = True
        context["timer"].stop()
        if process.state() == QProcess.NotRunning:
            self._finish_pre_action(process, exit_code=process.exitCode())
            return
        process.terminate()

        kill_timer = QTimer(process)
        kill_timer.setSingleShot(True)
        kill_timer.timeout.connect(
            lambda p=process: self._kill_timed_out_pre_action(p)
        )
        context["kill_timer"] = kill_timer
        kill_timer.start(2000)

    def _pre_action_timed_out(self, process):
        context = getattr(self, "_pre_action_processes", {}).get(id(process))
        if context is None:
            return
        context["timed_out"] = True
        process.terminate()

        kill_timer = QTimer(process)
        kill_timer.setSingleShot(True)
        kill_timer.timeout.connect(
            lambda p=process: self._kill_timed_out_pre_action(p)
        )
        context["kill_timer"] = kill_timer
        kill_timer.start(2000)

    def _kill_timed_out_pre_action(self, process):
        context = getattr(self, "_pre_action_processes", {}).get(id(process))
        if context is None:
            return
        if process.state() != QProcess.NotRunning:
            process.kill()
        else:
            self._finish_pre_action(process, exit_code=process.exitCode())

    def _run_pre_action(self, s, target):
        command = getattr(s, "pre_action_command", "").strip()
        try:
            args = shlex.split(command)
        except ValueError as exc:
            self._handle_pre_action_failure(
                s,
                target,
                f"El comando no es válido: {exc}",
            )
            return

        if not args:
            self._handle_pre_action_failure(
                s,
                target,
                "No se configuró ningún comando.",
            )
            return

        program, program_args = args[0], args[1:]
        log("Ejecutando programa previo: " + " ".join(args))
        if not getattr(s, "pre_action_wait", True):
            try:
                started = QProcess.startDetached(program, program_args)
                if isinstance(started, tuple):
                    started = started[0]
            except Exception as exc:
                self._handle_pre_action_failure(s, target, str(exc))
                return
            if not started:
                self._handle_pre_action_failure(
                    s,
                    target,
                    f"No se pudo iniciar «{program}».",
                )
                return
            self._execute_final_action(s, target)
            return

        parent = self if isinstance(self, QObject) else None
        process = QProcess(parent)
        process.setProgram(program)
        process.setArguments(program_args)
        timeout_seconds = max(
            1,
            min(
                3600,
                int(getattr(s, "pre_action_timeout_seconds", 60)),
            ),
        )
        timer = QTimer(process)
        timer.setSingleShot(True)
        contexts = getattr(self, "_pre_action_processes", None)
        if contexts is None:
            contexts = {}
            self._pre_action_processes = contexts
        contexts[id(process)] = {
            "process": process,
            "schedule": s,
            "target": target,
            "timer": timer,
            "kill_timer": None,
            "timed_out": False,
            "cancelled": False,
            "cancel_action_executed": False,
            "timeout_seconds": timeout_seconds,
        }
        process.finished.connect(
            lambda exit_code, _status, p=process: self._finish_pre_action(
                p,
                exit_code=exit_code,
            )
        )
        process.errorOccurred.connect(
            lambda error, p=process: self._finish_pre_action(p, error=error)
        )
        timer.timeout.connect(
            lambda p=process: self._pre_action_timed_out(p)
        )
        timer.start(timeout_seconds * 1000)
        process.start()

    def execute_action(self, s, target):
        if getattr(s, "pre_action_enabled", False):
            self._run_pre_action(s, target)
            return
        self._execute_final_action(s, target)

    def _find_qdbus(self):
        for candidate in (
            "qdbus6",
            "qdbus",
            "qdbus-qt6",
            "qdbus-qt5",
        ):
            path = shutil.which(candidate)
            if path:
                return path
        return None

    def _session_action_command(self, action):
        methods = {
            "poweroff": (
                "org.kde.Shutdown",
                "/Shutdown",
                "org.kde.Shutdown.logoutAndShutdown",
            ),
            "reboot": (
                "org.kde.Shutdown",
                "/Shutdown",
                "org.kde.Shutdown.logoutAndReboot",
            ),
            "logout": (
                "org.kde.Shutdown",
                "/Shutdown",
                "org.kde.Shutdown.logout",
            ),
            "lock": (
                "org.freedesktop.ScreenSaver",
                "/ScreenSaver",
                "org.freedesktop.ScreenSaver.Lock",
            ),
        }
        service, object_path, method = methods[action]
        qdbus = self._find_qdbus()
        if qdbus:
            return [qdbus, service, object_path, method]

        gdbus = shutil.which("gdbus")
        if gdbus:
            return [
                gdbus,
                "call",
                "--session",
                "--dest",
                service,
                "--object-path",
                object_path,
                "--method",
                method,
            ]
        if action == "lock":
            loginctl = shutil.which("loginctl")
            if loginctl:
                return [loginctl, "lock-session"]
        return None

    def _run_final_command(self, s, target, cmd, error_title):
        is_interval = schedule_trigger_mode(s) == "interval"
        if is_interval:
            self._guard_interval_execution(s, target)
        try:
            result = run_cmd(cmd)
        except Exception as exc:
            if is_interval:
                self._restore_failed_interval_execution(s)
            self.notify(error_title, str(exc), True)
            return False

        if result.returncode != 0:
            if is_interval:
                self._restore_failed_interval_execution(s)
            self.notify(
                error_title,
                result.stderr.strip()
                or result.stdout.strip()
                or "El comando devolvió un error desconocido.",
                True,
            )
            return False
        if is_interval:
            self._record_action_completion(s, target)
        return True

    def _execute_final_action(self, s, target):
        is_interval = schedule_trigger_mode(s) == "interval"
        if not is_interval:
            self._record_action_completion(s, target)

        if s.action == "test":
            if is_interval:
                self._record_action_completion(s, target)
            self.notify(
                "Prueba completada",
                "El aviso y la cuenta regresiva funcionan correctamente.",
            )
            return

        if (
            getattr(s, "close_apps_first", True)
            and s.action in ("poweroff", "reboot")
        ):
            cmd = self._session_action_command(s.action)
            if not cmd:
                self.notify(
                    "No se ejecutó la acción",
                    "La programación pidió cerrar las aplicaciones "
                    "correctamente, pero no se encontró qdbus ni gdbus. "
                    "Por seguridad no se forzó la acción.",
                    True,
                )
                return
            # Chrome necesita una petición de salida propia antes del
            # cierre global de Plasma. SIGHUP fue probado manualmente
            # y conserva correctamente todas sus ventanas/pestañas.
            if not self._close_chrome_cleanly():
                return

            self.notify(
                "Cerrando aplicaciones",
                "Plasma cerrará la sesión y permitirá que las "
                "aplicaciones guarden correctamente su estado antes de "
                f"{ACTIONS.get(s.action, s.action).lower()}.",
                True,
            )

            self._run_final_command(
                s,
                target,
                cmd,
                "No se pudo iniciar el cierre seguro",
            )
            return

        if s.action in ("logout", "lock"):
            cmd = self._session_action_command(s.action)
            if not cmd:
                self.notify(
                    "No se ejecutó la acción",
                    "No se encontró qdbus ni gdbus para controlar la "
                    "sesión de Plasma.",
                    True,
                )
                return
            self.notify("Ejecutando", ACTIONS[s.action], True)
            self._run_final_command(
                s,
                target,
                cmd,
                f"No se pudo {ACTIONS[s.action].lower()}",
            )
            return

        command_map = {
            "poweroff": ["systemctl", "poweroff"],
            "reboot": ["systemctl", "reboot"],
            "suspend": ["systemctl", "suspend"],
            "hibernate": ["systemctl", "hibernate"],
        }

        cmd = command_map.get(s.action)

        if not cmd:
            self.notify(
                "Error",
                f"Acción desconocida: {s.action}",
                True,
            )
            return

        self.notify(
            "Ejecutando",
            ACTIONS.get(s.action, s.action),
            True,
        )

        self._run_final_command(
            s,
            target,
            cmd,
            "No se pudo ejecutar la acción",
        )

    def cancel_next_run(self):
        active = [
            dlg
            for dlg in self.active_dialogs.values()
            if getattr(dlg, "schedule", None) is not None
        ]
        if active:
            min(active, key=lambda dlg: dlg.remaining).finish(
                "cancel",
                user_requested=True,
            )
            return

        pre_actions = list(
            getattr(self, "_pre_action_processes", {}).values()
        )
        if pre_actions:
            context = min(pre_actions, key=lambda item: item["target"])
            s = context["schedule"]
            target = context["target"]
            self._cancel_pre_action(context["process"])
            self.mark_skipped(s, target)
            if not context.get("cancel_action_executed", False):
                context["cancel_action_executed"] = True
                self._run_cancel_action_command(s)
            self.notify(
                "Acción cancelada",
                f"«{s.name}» fue cancelada mientras se ejecutaba el "
                "programa previo.",
            )
            return

        now = datetime.now()
        candidates = []
        for s in self.schedules():
            if s.enabled:
                pending = self.pending_occurrence(s)
                if pending:
                    candidates.append((
                        self.pending_action_time(s, pending, now),
                        s,
                        pending.scheduled_target,
                    ))
                    continue
                nxt = self.next_occurrence(s, now)
                if nxt:
                    candidates.append((nxt, s, nxt))
        if not candidates:
            self.notify("Nada que cancelar", "No hay acciones activas próximas.")
            return
        due, s, target = min(candidates, key=lambda x: x[0])
        self.mark_skipped(s, target)
        self._run_cancel_action_command(s)
        self.notify(
            "Próxima acción cancelada",
            f"{s.name} "
            f"({format_app_datetime(self.config, due, '%d/%m')}) "
            "no se ejecutará esta vez.",
        )

    def test_warning(self):
        s = Schedule(name="Prueba de aviso", action="test", final_countdown_seconds=15)
        key = f"test:{uuid.uuid4()}"
        self.start_final_countdown(s, datetime.now() + timedelta(seconds=15), 15, key)

    # ---------------- Actualizaciones ----------------
    def refresh_update_ui(self):
        last = self.state.get("last_update_check")
        if last:
            try:
                dt = datetime.fromisoformat(last)
                self.last_check_label.setText(
                    "Última comprobación: "
                    f"{format_app_datetime(self.config, dt, '%d/%m/%Y')}"
                )
            except Exception:
                self.last_check_label.setText("Última comprobación: desconocida")
        else:
            self.last_check_label.setText("Última comprobación: todavía no realizada")

        info = self.available_update
        if info and is_newer_version(str(info.get("version", ""))):
            src = "paquete descargado" if info.get("source") == "local" else "Internet"
            self.update_status.setText(f"<b>Actualización disponible: v{info['version']}</b> ({src}).")
            self.install_available_btn.setEnabled(True)
        else:
            self.update_status.setText(f"AMP AutoPower {APP_VERSION} está listo para comprobar actualizaciones.")
            self.install_available_btn.setEnabled(False)

    def maybe_auto_check_updates(self):
        if not self.config.get("auto_check_updates", True):
            return
        last = self.state.get("last_update_check")
        interval = max(1, int(self.config.get("update_interval_hours", 48)))
        if last:
            try:
                if datetime.now() - datetime.fromisoformat(last) < timedelta(hours=interval):
                    return
            except Exception:
                pass
        self.check_updates(manual=False)

    def check_updates(self, manual=False):
        if self.update_thread and self.update_thread.isRunning():
            if manual:
                QMessageBox.information(self, "Actualizaciones", "Ya hay una comprobación en curso.")
            return
        self.save_settings()
        if manual:
            self.update_status.setText("Buscando actualizaciones…")
            self.check_update_btn.setEnabled(False)
        self.update_thread = UpdateCheckThread(self.config.get("update_manifest_url", ""))
        self.update_thread.result_ready.connect(lambda result, m=manual: self.on_update_check_finished(result, m))
        self.update_thread.start()

    def on_update_check_finished(self, result, manual):
        self.check_update_btn.setEnabled(True)
        self.state["last_update_check"] = result.get("checked_at") or datetime.now().isoformat(timespec="seconds")
        info = result.get("available")
        self.available_update = info
        self.state["available_update"] = info
        save_json(STATE_FILE, self.state)
        self.refresh_update_ui()

        if info:
            notes = info.get("notes") or "Hay una nueva versión disponible."
            if manual:
                QMessageBox.information(
                    self,
                    "Actualización disponible",
                    f"Está disponible AMP AutoPower v{info['version']}.\n\n{notes}\n\nPuedes instalarla desde la pestaña Actualizaciones.",
                )
            elif self.config.get("notify_updates", True):
                self.notify("Actualización disponible", f"AMP AutoPower v{info['version']} está disponible.")
        elif result.get("error"):
            self.update_status.setText(f"No se pudo completar la comprobación por Internet: {result['error']}")
            if manual:
                QMessageBox.warning(
                    self,
                    "Actualizaciones",
                    "No se pudo completar la comprobación por Internet.\n\n"
                    f"{result['error']}\n\n"
                    "La búsqueda de paquetes descargados se realiza también cuando es posible.",
                )
        elif manual:
            QMessageBox.information(self, "Actualizaciones", f"Ya tienes la versión más reciente instalada: {APP_VERSION}.")

    def choose_update_package(self):
        start = str(Path.home() / "Descargas") if (Path.home() / "Descargas").exists() else str(Path.home())
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Seleccionar actualización de AMP AutoPower",
            start,
            "Paquetes AMP AutoPower (*.tar.gz);;Todos los archivos (*)",
        )
        if path:
            self.prepare_install_package(Path(path))

    def install_available_update(self):
        info = self.available_update
        if not info:
            QMessageBox.information(self, "Actualizaciones", "No hay una actualización disponible.")
            return
        if info.get("source") == "local" and info.get("path"):
            self.prepare_install_package(Path(info["path"]))
            return
        if info.get("source") == "remote" and info.get("package_url"):
            self.download_update(info)
            return
        QMessageBox.warning(self, "Actualizaciones", "La información de la actualización está incompleta.")

    def download_update(self, info):
        if self.download_thread and self.download_thread.isRunning():
            return
        self.download_dialog = QProgressDialog("Descargando actualización…", "Cancelar", 0, 100, self)
        self.download_dialog.setWindowTitle("AMP AutoPower")
        self.download_dialog.setAutoClose(False)
        self.download_dialog.setValue(0)
        self.download_thread = DownloadThread(info)
        self.download_thread.progress.connect(self.download_dialog.setValue)
        self.download_thread.finished_download.connect(self.on_download_finished)
        self.download_dialog.canceled.connect(self.download_thread.requestInterruption)
        self.download_thread.start()
        self.download_dialog.show()

    def on_download_finished(self, result):
        if self.download_dialog:
            self.download_dialog.close()
            self.download_dialog = None
        if not result.get("ok"):
            QMessageBox.critical(self, "Actualización", f"No se pudo descargar la actualización:\n\n{result.get('error', 'Error desconocido')}")
            return
        self.prepare_install_package(Path(result["path"]))

    def prepare_install_package(self, path: Path):
        if self.active_dialogs:
            QMessageBox.warning(
                self,
                "Actualización",
                "Hay una cuenta regresiva de energía activa. "
                "Cancélala o espera a que termine antes de actualizar.",
            )
            return

        if self.install_process and self.install_process.poll() is None:
            QMessageBox.information(
                self,
                "Actualización",
                "Ya hay una instalación en curso.",
            )
            return

        if not path.exists():
            QMessageBox.warning(
                self,
                "Actualización",
                "El paquete seleccionado ya no existe.",
            )
            return

        ver = package_version(path)

        if not ver:
            QMessageBox.critical(
                self,
                "Actualización",
                "El paquete no contiene un archivo VERSION válido.",
            )
            return

        if version_tuple(ver) <= version_tuple(APP_VERSION):
            ans = QMessageBox.question(
                self,
                "Versión no más reciente",
                f"El paquete es v{ver} y tienes v{APP_VERSION}. "
                "¿Quieres reinstalarlo de todos modos?",
            )

            if ans != QMessageBox.Yes:
                return

        else:
            ans = QMessageBox.question(
                self,
                "Instalar actualización",
                f"Se instalará AMP AutoPower v{ver}.\n\n"
                "Tus horarios y ajustes se conservarán. "
                "Se creará una copia de seguridad de la versión actual.\n\n"
                "Cuando la instalación termine podrás pulsar OK y "
                "solo entonces AMP AutoPower se reiniciará.\n\n"
                "¿Continuar?",
            )

            if ans != QMessageBox.Yes:
                return

        try:
            stage = Path(
                tempfile.mkdtemp(
                    prefix="amp-autopower-update-",
                    dir=str(UPDATE_CACHE_DIR),
                )
            )

            with tarfile.open(path, "r:*") as tf:
                safe_extract_tar(tf, stage)

            installers = []

            for installer in stage.rglob("install.sh"):
                parent = installer.parent

                if (
                    (parent / "amp_autopower.py").exists()
                    and (parent / "condition_engine.py").exists()
                    and (parent / "compact_display.py").exists()
                    and (parent / "VERSION").exists()
                ):
                    installers.append(installer)

            if not installers:
                raise ValueError(
                    "No se encontró install.sh junto al programa "
                    "dentro del paquete."
                )

            installer = min(
                installers,
                key=lambda p: len(p.parts),
            )

            ensure_dirs()

            unit_name = (
                f"amp-autopower-update-install-"
                f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
            )

            log(
                f"Instalando actualización desde {path} "
                f"a v{ver} mediante {unit_name}; "
                "el reinicio esperará confirmación del usuario"
            )

            self.pending_update_version = ver

            self.install_progress = QProgressDialog(
                f"Instalando AMP AutoPower v{ver}…",
                "",
                0,
                0,
                self,
            )

            self.install_progress.setWindowTitle("AMP AutoPower")
            self.install_progress.setCancelButton(None)
            self.install_progress.setMinimumDuration(0)
            self.install_progress.setWindowModality(Qt.WindowModal)
            self.install_progress.show()

            self.install_process = subprocess.Popen(
                [
                    "systemd-run",
                    "--user",
                    "--quiet",
                    "--wait",
                    "--collect",
                    f"--unit={unit_name}",
                    "/usr/bin/bash",
                    str(installer),
                    "--update-no-restart",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )

            self.install_poll_timer.start(250)

        except Exception as e:
            log(f"Error preparando actualización: {e}")

            if self.install_progress:
                self.install_progress.close()
                self.install_progress = None

            QMessageBox.critical(
                self,
                "Actualización",
                f"No se pudo preparar la actualización:\n\n{e}",
            )

    def _poll_update_install(self):
        if not self.install_process:
            self.install_poll_timer.stop()
            return

        rc = self.install_process.poll()

        if rc is None:
            return

        self.install_poll_timer.stop()

        if self.install_progress:
            self.install_progress.close()
            self.install_progress = None

        ver = self.pending_update_version or "desconocida"

        self.install_process = None
        self.pending_update_version = None

        if rc != 0:
            QMessageBox.critical(
                self,
                "Actualización",
                f"No se pudo instalar AMP AutoPower v{ver}.\n\n"
                f"Revisa el registro:\n{LOG_DIR / 'update.log'}",
            )
            return

        log(
            f"AMP AutoPower v{ver} instalada correctamente; "
            "esperando OK antes de reiniciar"
        )

        QMessageBox.information(
            self,
            "Actualización instalada",
            f"AMP AutoPower v{ver} se instaló correctamente.\n\n"
            "Pulsa OK para reiniciar AMP AutoPower y usar la nueva versión.",
        )

        # Esta línea solo se alcanza DESPUÉS de pulsar OK.
        self._restart_after_update()

    def _restart_after_update(self):
        unit_name = (
            f"amp-autopower-update-restart-"
            f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
        )

        self.update_status.setText(
            "Actualización instalada. Reiniciando AMP AutoPower…"
        )

        log(
            "El usuario confirmó OK; reiniciando AMP AutoPower"
        )

        subprocess.Popen(
            [
                "systemd-run",
                "--user",
                "--quiet",
                "--collect",
                f"--unit={unit_name}",
                "/usr/bin/systemctl",
                "--user",
                "restart",
                "amp-autopower.service",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.Trigger:
            self.show_normal()

    def show_normal(self):
        self.show()
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def toggle_main_window(self):
        if self.isVisible() and not self.isMinimized():
            self.hide()
        else:
            self.show_normal()

    def closeEvent(self, event: QCloseEvent):
        if self.config.get("close_to_tray", True):
            event.ignore()
            self.hide()
            if self.config.get("tray_visible", True):
                self.tray.showMessage(APP_NAME, "Sigue funcionando en la bandeja.", QSystemTrayIcon.Information, 3000)
        else:
            event.accept()

    def quit_app(self):
        self.config["start_minimized"] = self.start_min.isChecked()
        save_json(CONFIG_FILE, self.config)
        if hasattr(self, "compact_display"):
            self.compact_display.hide()
        if self.input_monitor and self.input_monitor.isRunning():
            self.input_monitor.requestInterruption(); self.input_monitor.wait(1200)
        if hasattr(self, "global_shortcut"):
            self.global_shortcut.disable()
        self.tray.hide()
        QApplication.quit()


class IpcServer:
    def __init__(self, window):
        self.window = window
        self.server = QLocalServer(window)
        self.server.setSocketOptions(QLocalServer.UserAccessOption)
        self._buffers = {}
        self._connection_timers = {}
        QLocalServer.removeServer(IPC_NAME)
        if self.server.listen(IPC_NAME):
            self.server.newConnection.connect(self.handle_connection)
        else:
            log(f"No se pudo iniciar IPC: {self.server.errorString()}")

    def handle_connection(self):
        sock = self.server.nextPendingConnection()
        if not sock:
            return
        self._buffers[sock] = bytearray()
        sock.readyRead.connect(lambda current=sock: self._read_connection(current))
        sock.disconnected.connect(lambda current=sock: self._forget_connection(current))
        try:
            timer = QTimer(self.server)
            timer.setSingleShot(True)
            timer.timeout.connect(
                lambda current=sock: self._expire_connection(current)
            )
            timer.start(IPC_TIMEOUT_MS)
            self._connection_timers[sock] = timer
        except TypeError:
            pass
        self._read_connection(sock)

    def _read_connection(self, sock):
        buffer = self._buffers.get(sock)
        if buffer is None:
            return
        available = int(sock.bytesAvailable())
        if available <= 0:
            return
        if len(buffer) + available > IPC_MAX_REQUEST_BYTES:
            self._write_response(
                sock,
                {
                    "ok": False,
                    "code": 2,
                    "output": "Solicitud IPC demasiado grande.",
                },
            )
            self._finish_connection(sock)
            return
        buffer.extend(bytes(sock.readAll()))
        stripped = bytes(buffer).lstrip()
        if not stripped:
            return
        if stripped.startswith(b"{"):
            if b"\n" not in buffer:
                return
            raw, trailing = bytes(buffer).split(b"\n", 1)
            if trailing.strip():
                response = {
                    "ok": False,
                    "code": 2,
                    "output": "Solo se permite una solicitud por conexión.",
                }
            else:
                response = self._handle_structured_request(raw)
            self._write_response(sock, response)
            self._finish_connection(sock)
            return

        command = bytes(buffer).decode("utf-8", "replace").strip()
        if command == "show":
            self.window.show_normal()
        elif command == "check-update":
            self.window.check_updates(manual=True)
        else:
            return
        self._finish_connection(sock)

    def _finish_connection(self, sock):
        self._buffers.pop(sock, None)
        timer = self._connection_timers.pop(sock, None)
        if timer is not None:
            timer.stop()
            timer.deleteLater()
        sock.disconnectFromServer()

    def _forget_connection(self, sock):
        self._buffers.pop(sock, None)
        timer = self._connection_timers.pop(sock, None)
        if timer is not None:
            timer.stop()
            timer.deleteLater()
        try:
            sock.deleteLater()
        except RuntimeError:
            pass

    def _expire_connection(self, sock):
        if sock not in self._buffers:
            return
        self._write_response(
            sock,
            {
                "ok": False,
                "code": 1,
                "output": "La solicitud IPC no se completó a tiempo.",
            },
        )
        self._finish_connection(sock)


    def _handle_structured_request(self, raw):
        try:
            request = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {"ok": False, "code": 2, "output": "Solicitud IPC inválida."}
        if not isinstance(request, dict) or request.get("version") != 1:
            return {"ok": False, "code": 2, "output": "Protocolo IPC inválido."}
        command = request.get("command")
        target = request.get("target")
        if not isinstance(command, str) or (
            target is not None and not isinstance(target, str)
        ):
            return {"ok": False, "code": 2, "output": "Argumentos IPC inválidos."}
        return self.window.handle_cli_command(command, target)

    def _write_response(self, sock, response):
        payload = json.dumps(
            response,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        sock.write(payload)
        sock.flush()
        sock.waitForBytesWritten(100)


def _ipc_client_error(output):
    return {"ok": False, "code": 1, "output": output}


def send_ipc(command: str, target=None, expect_response=False):
    sock = QLocalSocket()
    sock.connectToServer(IPC_NAME)
    if not sock.waitForConnected(IPC_TIMEOUT_MS):
        return None if expect_response else False
    if expect_response:
        payload = json.dumps(
            {"version": 1, "command": command, "target": target},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
    else:
        payload = command.encode("utf-8")
    sock.write(payload)
    sock.flush()
    sock.waitForBytesWritten(IPC_TIMEOUT_MS)
    if expect_response:
        response_data = bytearray()
        deadline = time.monotonic() + (IPC_TIMEOUT_MS / 1000)
        while b"\n" not in response_data:
            available = int(sock.bytesAvailable())
            if available:
                if len(response_data) + available > IPC_MAX_RESPONSE_BYTES:
                    sock.disconnectFromServer()
                    return _ipc_client_error("Respuesta IPC demasiado grande.")
                response_data.extend(bytes(sock.readAll()))
                continue
            remaining_ms = int(max(0, deadline - time.monotonic()) * 1000)
            if remaining_ms <= 0:
                sock.disconnectFromServer()
                return _ipc_client_error(
                    "La instancia aceptó la conexión, pero no respondió; "
                    "el resultado de la operación es desconocido."
                )
            if not sock.waitForReadyRead(remaining_ms):
                if int(sock.bytesAvailable()) > 0:
                    continue
                sock.disconnectFromServer()
                return _ipc_client_error(
                    "La instancia aceptó la conexión, pero no respondió; "
                    "el resultado de la operación es desconocido."
                )
        frame, trailing = bytes(response_data).split(b"\n", 1)
        if trailing.strip():
            sock.disconnectFromServer()
            return _ipc_client_error("Respuesta IPC inválida.")
        try:
            response = json.loads(frame.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            sock.disconnectFromServer()
            return _ipc_client_error("Respuesta IPC inválida.")
        if (
            not isinstance(response, dict)
            or not isinstance(response.get("ok"), bool)
            or type(response.get("code")) is not int
            or not isinstance(response.get("output"), str)
        ):
            sock.disconnectFromServer()
            return _ipc_client_error("Respuesta IPC inválida.")
        sock.disconnectFromServer()
        return response
    sock.disconnectFromServer()
    return True


def send_ipc_with_retry(command, target=None, expect_response=False):
    unavailable = None if expect_response else False
    for attempt in range(8):
        response = send_ipc(command, target, expect_response)
        if response is not unavailable:
            return response
        if attempt < 7:
            time.sleep(0.2)
    return unavailable


def _print_cli_response(response):
    output = response.get("output", "")
    if output:
        stream = sys.stdout if response.get("ok") else sys.stderr
        print(output, file=stream)
    return int(response.get("code", 1))


def _offline_query(command):
    config = load_json(CONFIG_FILE, DEFAULT_CONFIG)
    state = load_json(STATE_FILE, DEFAULT_STATE)
    schedules = schedules_from_config(config)
    if command == "status":
        output = format_status_cli(
            config,
            state,
            schedules,
            ipc_available=False,
        )
    else:
        output = format_schedules_cli(schedules, state)
    return {"ok": True, "code": 0, "output": output}


def main(argv=None):
    cli_argv = sys.argv[1:] if argv is None else list(argv)
    args = build_cli_parser().parse_args(cli_argv)
    command, target = cli_command_from_args(args)

    administrative = {
        "hide",
        "toggle",
        "status",
        "list-schedules",
        "enable",
        "disable",
    }
    if command in administrative:
        response = send_ipc_with_retry(command, target, expect_response=True)
        if response is not None:
            return _print_cli_response(response)
        if command in ("status", "list-schedules"):
            return _print_cli_response(_offline_query(command))
        print(
            "AMP AutoPower debe estar ejecutándose y accesible por IPC.",
            file=sys.stderr,
        )
        return 1

    ensure_dirs()
    configure_qt_system_theme()
    qt_argv = sys.argv if argv is None else [sys.argv[0], *cli_argv]
    app = QApplication(qt_argv)
    app._system_palette_fallback = apply_system_palette_fallback(app)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("Local")
    app.setQuitOnLastWindowClosed(False)

    existing_command = command if command == "check-update" else "show"
    lock_path = str(Path(QStandardPaths.writableLocation(QStandardPaths.TempLocation)) / "amp-autopower.lock")
    lock = QLockFile(lock_path)
    lock.setStaleLockTime(0)
    if not lock.tryLock(50):
        if send_ipc_with_retry(existing_command):
            return 0
        QMessageBox.information(None, APP_NAME, "AMP AutoPower ya está ejecutándose, pero no se pudo contactar con su ventana. Prueba a reiniciar el servicio.")
        return 1

    window = MainWindow(app)
    ipc = IpcServer(window)
    window._ipc = ipc
    if not window.config.get("tray_visible", True):
        window.set_tray_visible(False)
    if not window.config.get("start_minimized", True) or command == "show":
        window.show()
    else:
        window.hide()
    if command == "check-update":
        QTimer.singleShot(1000, lambda: window.check_updates(manual=True))
    log(f"Aplicación iniciada v{APP_VERSION}")
    rc = app.exec()
    log("Aplicación cerrada")
    return rc


if __name__ == "__main__":
    sys.exit(main())
