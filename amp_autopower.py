#!/usr/bin/env python3
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QLockFile, QStandardPaths, QThread, Signal
from PySide6.QtGui import QAction, QCloseEvent, QIcon
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFileDialog, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
    QPushButton, QProgressDialog, QSpinBox, QSystemTrayIcon, QTabWidget,
    QTimeEdit, QVBoxLayout, QWidget,
)
from PySide6.QtCore import QTime

APP_NAME = "AMP AutoPower"
APP_ID = "amp-autopower"
APP_VERSION = "1.1.1"
IPC_NAME = "amp-autopower-ipc-v1"
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
    "test": "Solo aviso (prueba)",
}


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
    time: str = "23:30"
    weekdays: list = field(default_factory=lambda: [0, 1, 2, 3, 4, 5, 6])
    action: str = "poweroff"
    warning_minutes: list = field(default_factory=lambda: [30, 15, 5, 1])
    final_countdown_seconds: int = 60


DEFAULT_CONFIG = {
    "start_minimized": True,
    "close_to_tray": True,
    "notifications": True,
    "sound": True,
    "auto_check_updates": True,
    "update_interval_hours": 48,
    "update_manifest_url": CANONICAL_UPDATE_MANIFEST_URL,
    "notify_updates": True,
    "schedules": [asdict(Schedule())],
}

DEFAULT_STATE = {
    "last_runs": {},
    "snoozes": {},
    "skipped_targets": {},
    "last_update_check": None,
    "available_update": None,
}


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
                with urllib.request.urlopen(req, timeout=15) as r:
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
            with urllib.request.urlopen(req, timeout=30) as r, out.open("wb") as f:
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


class CountdownDialog(QDialog):
    def __init__(self, parent, schedule: Schedule, seconds: int):
        super().__init__(parent)
        self.schedule = schedule
        self.remaining = seconds
        self.result_action = "execute"
        self.setWindowTitle(f"{APP_NAME} — acción inminente")
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        self.setModal(False)
        self.resize(520, 230)

        lay = QVBoxLayout(self)
        self.title = QLabel()
        self.title.setAlignment(Qt.AlignCenter)
        self.title.setStyleSheet("font-size: 22px; font-weight: 700;")
        lay.addWidget(self.title)

        desc = QLabel(
            f"Acción programada: <b>{ACTIONS.get(schedule.action, schedule.action)}</b><br>"
            f"Horario: <b>{schedule.time}</b><br><br>"
            "Puedes cancelarla o posponerla antes de que termine la cuenta regresiva."
        )
        desc.setAlignment(Qt.AlignCenter)
        desc.setWordWrap(True)
        lay.addWidget(desc)

        row = QHBoxLayout()
        cancel = QPushButton("Cancelar esta vez")
        snooze10 = QPushButton("Posponer 10 min")
        snooze30 = QPushButton("Posponer 30 min")
        cancel.clicked.connect(lambda: self.finish("cancel"))
        snooze10.clicked.connect(lambda: self.finish("snooze10"))
        snooze30.clicked.connect(lambda: self.finish("snooze30"))
        row.addWidget(cancel)
        row.addWidget(snooze10)
        row.addWidget(snooze30)
        lay.addLayout(row)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(1000)
        self.update_text()

    def update_text(self):
        self.title.setText(f"{ACTIONS.get(self.schedule.action, self.schedule.action)} en {self.remaining} s")

    def tick(self):
        self.remaining -= 1
        if self.remaining <= 0:
            self.timer.stop()
            self.finish("execute")
        else:
            self.update_text()

    def finish(self, action):
        self.result_action = action
        self.timer.stop()
        self.done(QDialog.Accepted)

    def closeEvent(self, event):
        self.result_action = "cancel"
        self.timer.stop()
        event.accept()


class ScheduleEditor(QDialog):
    def __init__(self, parent=None, schedule=None):
        super().__init__(parent)
        self.setWindowTitle("Editar programación")
        self.resize(520, 460)
        self.original = schedule
        s = schedule or Schedule()

        root = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit(s.name)
        self.enabled = QCheckBox("Activa")
        self.enabled.setChecked(s.enabled)
        self.time = QTimeEdit(QTime.fromString(s.time, "HH:mm"))
        self.time.setDisplayFormat("HH:mm")
        self.action = QComboBox()
        for key, text in ACTIONS.items():
            self.action.addItem(text, key)
        idx = self.action.findData(s.action)
        self.action.setCurrentIndex(max(0, idx))
        self.countdown = QSpinBox()
        self.countdown.setRange(15, 600)
        self.countdown.setSuffix(" s")
        self.countdown.setValue(s.final_countdown_seconds)

        form.addRow("Nombre:", self.name)
        form.addRow("Estado:", self.enabled)
        form.addRow("Hora:", self.time)
        form.addRow("Acción:", self.action)
        form.addRow("Cuenta regresiva final:", self.countdown)
        root.addLayout(form)

        days_box = QGroupBox("Días de la semana")
        days_layout = QGridLayout(days_box)
        self.days = []
        for i, d in enumerate(WEEKDAYS):
            c = QCheckBox(d)
            c.setChecked(i in s.weekdays)
            self.days.append(c)
            days_layout.addWidget(c, i // 4, i % 4)
        root.addWidget(days_box)

        warn_box = QGroupBox("Avisos previos")
        warn_layout = QHBoxLayout(warn_box)
        self.warn30 = QCheckBox("30 min")
        self.warn15 = QCheckBox("15 min")
        self.warn5 = QCheckBox("5 min")
        self.warn1 = QCheckBox("1 min")
        for w, val in [(self.warn30, 30), (self.warn15, 15), (self.warn5, 5), (self.warn1, 1)]:
            w.setChecked(val in s.warning_minutes)
            warn_layout.addWidget(w)
        root.addWidget(warn_box)

        note = QLabel("El aviso final abre una ventana por encima de las demás con opciones para cancelar o posponer.")
        note.setWordWrap(True)
        root.addWidget(note)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def get_schedule(self):
        warns = []
        for w, val in [(self.warn30, 30), (self.warn15, 15), (self.warn5, 5), (self.warn1, 1)]:
            if w.isChecked():
                warns.append(val)
        weekdays = [i for i, c in enumerate(self.days) if c.isChecked()]
        return Schedule(
            id=self.original.id if self.original else str(uuid.uuid4()),
            name=self.name.text().strip() or "Programación",
            enabled=self.enabled.isChecked(),
            time=self.time.time().toString("HH:mm"),
            weekdays=weekdays,
            action=self.action.currentData(),
            warning_minutes=sorted(warns, reverse=True),
            final_countdown_seconds=self.countdown.value(),
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
        self.available_update = self.state.get("available_update")
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
        cancel_action = QAction("Cancelar próxima ejecución", self)
        cancel_action.triggered.connect(self.cancel_next_run)
        update_action = QAction("Buscar actualizaciones", self)
        update_action.triggered.connect(lambda: self.check_updates(manual=True))
        quit_action = QAction("Salir", self)
        quit_action.triggered.connect(self.quit_app)
        menu.addAction(show_action)
        menu.addAction(cancel_action)
        menu.addAction(update_action)
        menu.addSeparator()
        menu.addAction(quit_action)
        self.tray.show()

        tabs = QTabWidget()
        self.setCentralWidget(tabs)

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
        tabs.addTab(sched_tab, "Programaciones")

        settings_tab = QWidget()
        settings_layout = QVBoxLayout(settings_tab)
        self.start_min = QCheckBox("Iniciar minimizada en la bandeja")
        self.close_tray = QCheckBox("Cerrar la ventana = minimizar a bandeja")
        self.notifications = QCheckBox("Mostrar notificaciones previas")
        self.sound = QCheckBox("Reproducir sonido de aviso cuando sea posible")
        self.start_min.setChecked(self.config.get("start_minimized", True))
        self.close_tray.setChecked(self.config.get("close_to_tray", True))
        self.notifications.setChecked(self.config.get("notifications", True))
        self.sound.setChecked(self.config.get("sound", True))
        for w in (self.start_min, self.close_tray, self.notifications, self.sound):
            settings_layout.addWidget(w)
            w.toggled.connect(self.save_settings)
        settings_layout.addStretch()
        tabs.addTab(settings_tab, "Ajustes")

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
        tabs.addTab(update_tab, "Actualizaciones")

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
        tabs.addTab(info_tab, "Información")

        self.refresh_list()
        self.refresh_update_ui()
        self.scheduler = QTimer(self)
        self.scheduler.timeout.connect(self.scheduler_tick)
        self.scheduler.start(1000)
        self.scheduler_tick()
        QTimer.singleShot(6000, self.maybe_auto_check_updates)

    def schedules(self):
        out = []
        for raw in self.config.get("schedules", []):
            try:
                out.append(Schedule(**raw))
            except Exception as e:
                log(f"Programación inválida ignorada: {e}")
        return out

    def set_schedules(self, schedules):
        self.config["schedules"] = [asdict(s) for s in schedules]
        save_json(CONFIG_FILE, self.config)
        self.refresh_list()

    def save_settings(self):
        self.config["start_minimized"] = self.start_min.isChecked()
        self.config["close_to_tray"] = self.close_tray.isChecked()
        self.config["notifications"] = self.notifications.isChecked()
        self.config["sound"] = self.sound.isChecked()
        if hasattr(self, "auto_updates"):
            self.config["auto_check_updates"] = self.auto_updates.isChecked()
            self.config["notify_updates"] = self.notify_updates.isChecked()
            self.config["update_manifest_url"] = self.manifest_url.text().strip()
        save_json(CONFIG_FILE, self.config)

    def refresh_list(self):
        self.list.clear()
        for s in self.schedules():
            days = "Todos" if len(s.weekdays) == 7 else ", ".join(WEEKDAYS[i] for i in s.weekdays)
            status = "✓" if s.enabled else "✗"
            text = f"{status}  {s.time} — {s.name} — {ACTIONS.get(s.action, s.action)} — {days}"
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
            ss.append(dlg.get_schedule())
            self.set_schedules(ss)

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
            self.set_schedules(ss)

    def delete_schedule(self):
        sid = self.get_selected_id()
        if not sid:
            return
        if QMessageBox.question(self, "Eliminar", "¿Eliminar esta programación?") == QMessageBox.Yes:
            self.set_schedules([s for s in self.schedules() if s.id != sid])

    def next_occurrence(self, s: Schedule, now=None):
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
        for delta in range(0, 8):
            day = now.date() + timedelta(days=delta)
            candidate = datetime.combine(day, datetime.min.time()).replace(hour=hh, minute=mm)
            if candidate.weekday() not in s.weekdays or candidate < now:
                continue
            if skipped_iso == candidate.isoformat():
                continue
            return candidate
        return None

    def update_next_label(self):
        now = datetime.now()
        candidates = []
        for s in self.schedules():
            if not s.enabled:
                continue
            nxt = self.next_occurrence(s, now)
            if nxt:
                candidates.append((nxt, s))
        if not candidates:
            self.status_label.setText("No hay ninguna acción activa programada.")
            return
        nxt, s = min(candidates, key=lambda x: x[0])
        delta = nxt - now
        sec = max(0, int(delta.total_seconds()))
        h, rem = divmod(sec, 3600)
        m, _ = divmod(rem, 60)
        self.status_label.setText(
            f"Próxima acción: <b>{ACTIONS.get(s.action, s.action)}</b> — "
            f"<b>{nxt.strftime('%a %d/%m %H:%M')}</b> — faltan {h} h {m} min"
        )

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

    def scheduler_tick(self):
        now = datetime.now()
        dayprefix = now.strftime("%Y-%m-%d")
        self.warned = {x for x in self.warned if dayprefix in x}

        for s in self.schedules():
            if not s.enabled or not s.weekdays:
                continue
            target = self.next_occurrence(s, now - timedelta(seconds=2))
            if not target:
                continue
            remaining = (target - now).total_seconds()
            for mins in s.warning_minutes:
                if 0 < remaining <= mins * 60 and remaining > mins * 60 - 2.5:
                    key = f"{target.date()}:{s.id}:warn:{mins}"
                    if key not in self.warned:
                        self.warned.add(key)
                        self.notify(
                            f"{ACTIONS.get(s.action, s.action)} programado",
                            f"La PC ejecutará «{ACTIONS.get(s.action, s.action)}» en {mins} minuto(s), a las {target.strftime('%H:%M')}. Abre {APP_NAME} para cancelar o cambiarlo.",
                            critical=mins <= 5,
                        )
            if 0 < remaining <= s.final_countdown_seconds:
                key = f"{target.isoformat()}:{s.id}"
                if key not in self.active_dialogs:
                    self.start_final_countdown(s, target, int(max(1, remaining)), key)
        self.update_next_label()

    def start_final_countdown(self, s, target, seconds, key):
        self.notify(
            "Acción inminente",
            f"{ACTIONS.get(s.action, s.action)} en {seconds} segundos. Puedes cancelar o posponer.",
            critical=True,
        )
        dlg = CountdownDialog(self, s, seconds)
        self.active_dialogs[key] = dlg
        dlg.finished.connect(lambda _=None, k=key, d=dlg, sc=s, tg=target: self.on_countdown_finished(k, d, sc, tg))
        self.show_normal()
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def on_countdown_finished(self, key, dlg, s, target):
        self.active_dialogs.pop(key, None)
        result = dlg.result_action
        if result == "cancel":
            self.mark_skipped(s, target)
            self.notify("Acción cancelada", f"«{s.name}» fue cancelada para esta ocasión.")
        elif result == "snooze10":
            self.snooze(s, 10)
        elif result == "snooze30":
            self.snooze(s, 30)
        else:
            self.execute_action(s, target)

    def mark_skipped(self, s, target):
        self.state.setdefault("last_runs", {})[s.id] = target.isoformat() + ":skipped"
        self.state.setdefault("skipped_targets", {})[s.id] = target.isoformat()
        self.state.get("snoozes", {}).pop(s.id, None)
        save_json(STATE_FILE, self.state)

    def snooze(self, s, minutes):
        dt = datetime.now() + timedelta(minutes=minutes)
        self.state.setdefault("snoozes", {})[s.id] = dt.isoformat()
        save_json(STATE_FILE, self.state)
        self.notify("Acción pospuesta", f"«{s.name}» se ejecutará a las {dt.strftime('%H:%M')}, en {minutes} minutos.")

    def execute_action(self, s, target):
        self.state.setdefault("last_runs", {})[s.id] = target.isoformat()
        self.state.get("snoozes", {}).pop(s.id, None)
        save_json(STATE_FILE, self.state)
        if s.action == "test":
            self.notify("Prueba completada", "El aviso y la cuenta regresiva funcionan correctamente.")
            return
        command_map = {
            "poweroff": ["systemctl", "poweroff"],
            "reboot": ["systemctl", "reboot"],
            "suspend": ["systemctl", "suspend"],
            "hibernate": ["systemctl", "hibernate"],
        }
        cmd = command_map.get(s.action)
        if not cmd:
            self.notify("Error", f"Acción desconocida: {s.action}", True)
            return
        self.notify("Ejecutando", ACTIONS.get(s.action, s.action), True)
        result = run_cmd(cmd)
        if result.returncode != 0:
            self.notify("No se pudo ejecutar la acción", result.stderr.strip() or "Error desconocido", True)

    def cancel_next_run(self):
        now = datetime.now()
        candidates = []
        for s in self.schedules():
            if s.enabled:
                nxt = self.next_occurrence(s, now)
                if nxt:
                    candidates.append((nxt, s))
        if not candidates:
            self.notify("Nada que cancelar", "No hay acciones activas próximas.")
            return
        target, s = min(candidates, key=lambda x: x[0])
        self.mark_skipped(s, target)
        self.notify("Próxima acción cancelada", f"{s.name} ({target.strftime('%d/%m %H:%M')}) no se ejecutará esta vez.")

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
                self.last_check_label.setText(f"Última comprobación: {dt.strftime('%d/%m/%Y %H:%M')}")
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
            QMessageBox.warning(self, "Actualización", "Hay una cuenta regresiva de energía activa. Cancélala o espera a que termine antes de actualizar.")
            return
        if not path.exists():
            QMessageBox.warning(self, "Actualización", "El paquete seleccionado ya no existe.")
            return
        ver = package_version(path)
        if not ver:
            QMessageBox.critical(self, "Actualización", "El paquete no contiene un archivo VERSION válido.")
            return
        if version_tuple(ver) <= version_tuple(APP_VERSION):
            ans = QMessageBox.question(
                self,
                "Versión no más reciente",
                f"El paquete es v{ver} y tienes v{APP_VERSION}. ¿Quieres reinstalarlo de todos modos?",
            )
            if ans != QMessageBox.Yes:
                return
        else:
            ans = QMessageBox.question(
                self,
                "Instalar actualización",
                f"Se instalará AMP AutoPower v{ver}.\n\n"
                "Tus horarios y ajustes se conservarán. Se creará una copia de seguridad de la versión actual y el servicio se reiniciará.\n\n"
                "¿Continuar?",
            )
            if ans != QMessageBox.Yes:
                return

        try:
            stage = Path(tempfile.mkdtemp(prefix="amp-autopower-update-", dir=str(UPDATE_CACHE_DIR)))
            with tarfile.open(path, "r:*") as tf:
                safe_extract_tar(tf, stage)
            installers = []
            for installer in stage.rglob("install.sh"):
                parent = installer.parent
                if (parent / "amp_autopower.py").exists() and (parent / "VERSION").exists():
                    installers.append(installer)
            if not installers:
                raise ValueError("No se encontró install.sh junto al programa dentro del paquete.")
            installer = min(installers, key=lambda p: len(p.parts))
            update_log = LOG_DIR / "update.log"
            ensure_dirs()
            lf = update_log.open("a", encoding="utf-8")
            log(f"Iniciando actualización desde {path} a v{ver}")
            subprocess.Popen(
                ["bash", str(installer), "--update"],
                cwd=str(installer.parent),
                stdout=lf,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            QMessageBox.information(
                self,
                "Actualización iniciada",
                f"Se está instalando AMP AutoPower v{ver}.\n\n"
                "La aplicación se reiniciará automáticamente. Si tarda, puedes abrirla de nuevo desde el menú de KDE."
            )
        except Exception as e:
            log(f"Error preparando actualización: {e}")
            QMessageBox.critical(self, "Actualización", f"No se pudo preparar la actualización:\n\n{e}")

    def on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.Trigger:
            self.show_normal()

    def show_normal(self):
        self.show()
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event: QCloseEvent):
        if self.config.get("close_to_tray", True):
            event.ignore()
            self.hide()
            self.tray.showMessage(APP_NAME, "Sigue funcionando en la bandeja.", QSystemTrayIcon.Information, 3000)
        else:
            event.accept()

    def quit_app(self):
        self.config["start_minimized"] = self.start_min.isChecked()
        save_json(CONFIG_FILE, self.config)
        self.tray.hide()
        QApplication.quit()


class IpcServer:
    def __init__(self, window):
        self.window = window
        self.server = QLocalServer(window)
        QLocalServer.removeServer(IPC_NAME)
        if self.server.listen(IPC_NAME):
            self.server.newConnection.connect(self.handle_connection)
        else:
            log(f"No se pudo iniciar IPC: {self.server.errorString()}")

    def handle_connection(self):
        sock = self.server.nextPendingConnection()
        if not sock:
            return
        if sock.waitForReadyRead(300):
            cmd = bytes(sock.readAll()).decode("utf-8", "replace").strip()
            if cmd == "show":
                self.window.show_normal()
            elif cmd == "check-update":
                self.window.check_updates(manual=True)
        sock.disconnectFromServer()


def send_ipc(command: str):
    sock = QLocalSocket()
    sock.connectToServer(IPC_NAME)
    if not sock.waitForConnected(300):
        return False
    sock.write(command.encode("utf-8"))
    sock.flush()
    sock.waitForBytesWritten(300)
    sock.disconnectFromServer()
    return True


def main():
    ensure_dirs()
    if "--version" in sys.argv:
        print(APP_VERSION)
        return 0

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("Local")
    app.setQuitOnLastWindowClosed(False)

    command = "check-update" if "--check-update" in sys.argv else "show"
    lock_path = str(Path(QStandardPaths.writableLocation(QStandardPaths.TempLocation)) / "amp-autopower.lock")
    lock = QLockFile(lock_path)
    lock.setStaleLockTime(0)
    if not lock.tryLock(50):
        if send_ipc(command):
            return 0
        QMessageBox.information(None, APP_NAME, "AMP AutoPower ya está ejecutándose, pero no se pudo contactar con su ventana. Prueba a reiniciar el servicio.")
        return 1

    window = MainWindow(app)
    ipc = IpcServer(window)
    window._ipc = ipc
    if not window.config.get("start_minimized", True) or "--show" in sys.argv:
        window.show()
    else:
        window.hide()
    if "--check-update" in sys.argv:
        QTimer.singleShot(1000, lambda: window.check_updates(manual=True))
    log(f"Aplicación iniciada v{APP_VERSION}")
    rc = app.exec()
    log("Aplicación cerrada")
    return rc


if __name__ == "__main__":
    sys.exit(main())
