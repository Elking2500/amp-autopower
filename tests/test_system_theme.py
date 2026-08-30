import os
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QImage, QPalette
from PySide6.QtWidgets import QApplication

from amp_autopower import (
    DEFAULT_CONFIG,
    DEFAULT_STATE,
    MainWindow,
    Schedule,
    ScheduleEditor,
    apply_system_palette_fallback,
    configure_qt_system_theme,
    palette_is_dark,
)
from compact_display import CompactDisplayWindow, DisplayOptions


class SystemThemeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(
            ["amp-autopower-theme-test"]
        )

    def test_kde_platform_theme_is_selected_when_unset(self):
        environment = {"XDG_CURRENT_DESKTOP": "KDE"}

        configure_qt_system_theme(environment)

        self.assertEqual(environment["QT_QPA_PLATFORMTHEME"], "kde")

    def test_explicit_platform_theme_is_not_overridden(self):
        environment = {
            "XDG_CURRENT_DESKTOP": "KDE",
            "QT_QPA_PLATFORMTHEME": "qt6ct",
        }

        configure_qt_system_theme(environment)

        self.assertEqual(environment["QT_QPA_PLATFORMTHEME"], "qt6ct")

    def test_non_kde_session_is_not_forced(self):
        environment = {"XDG_CURRENT_DESKTOP": "GNOME"}

        configure_qt_system_theme(environment)

        self.assertNotIn("QT_QPA_PLATFORMTHEME", environment)

    def test_dark_kde_colors_replace_incorrect_light_qt_palette(self):
        palette = QPalette()
        palette.setColor(QPalette.Window, QColor("#f0f0f0"))
        palette.setColor(QPalette.WindowText, QColor("#202020"))
        app = PaletteApp(palette)
        colors = kde_colors(dark=True)

        changed = apply_system_palette_fallback(app, kde_colors=colors)

        self.assertTrue(changed)
        self.assertEqual(app.set_palette_calls, 1)
        self.assertTrue(palette_is_dark(app.palette()))
        self.assertEqual(
            app.palette().color(QPalette.Window),
            QColor(45, 50, 70),
        )

    def test_temporary_home_uses_kdeglobals_colors_without_scheme_files(self):
        palette = QPalette()
        palette.setColor(QPalette.Window, QColor("#f0f0f0"))
        palette.setColor(QPalette.WindowText, QColor("#202020"))
        app = PaletteApp(palette)
        with tempfile.TemporaryDirectory() as home:
            config_dir = Path(home) / ".config"
            config_dir.mkdir()
            (config_dir / "kdeglobals").write_text(
                "[Colors:Window]\n"
                "BackgroundNormal=45,50,70\n"
                "ForegroundNormal=222,222,222\n",
                encoding="utf-8",
            )

            changed = apply_system_palette_fallback(
                app,
                environment={"HOME": home},
            )

        self.assertTrue(changed)
        self.assertTrue(palette_is_dark(app.palette()))

    def test_light_kde_colors_replace_incorrect_dark_qt_palette(self):
        palette = QPalette()
        palette.setColor(QPalette.Window, QColor("#202020"))
        palette.setColor(QPalette.WindowText, QColor("#f0f0f0"))
        app = PaletteApp(palette)
        colors = kde_colors(dark=False)

        changed = apply_system_palette_fallback(app, kde_colors=colors)

        self.assertTrue(changed)
        self.assertEqual(app.set_palette_calls, 1)
        self.assertFalse(palette_is_dark(app.palette()))

    def test_valid_kde_palette_is_not_overridden(self):
        palette = QPalette()
        palette.setColor(QPalette.Window, QColor(45, 50, 70))
        palette.setColor(QPalette.WindowText, QColor(222, 222, 222))
        app = PaletteApp(palette)

        changed = apply_system_palette_fallback(
            app,
            kde_colors=kde_colors(dark=True),
        )

        self.assertFalse(changed)
        self.assertEqual(app.set_palette_calls, 0)

    def test_schedule_editor_inherits_application_palette(self):
        original = self.app.palette()
        palette = QPalette(original)
        palette.setColor(QPalette.Window, QColor("#161616"))
        palette.setColor(QPalette.WindowText, QColor("#f0f0f0"))
        palette.setColor(QPalette.Base, QColor("#202020"))
        palette.setColor(QPalette.Button, QColor("#282828"))
        self.app.setPalette(palette)
        editor = None
        try:
            editor = ScheduleEditor(schedule=Schedule())
            self.assertEqual(
                editor.palette().color(QPalette.Window),
                QColor("#161616"),
            )
            self.assertEqual(
                editor.time.palette().color(QPalette.Base),
                QColor("#202020"),
            )
            self.assertEqual(
                editor.mode.palette().color(QPalette.Button),
                QColor("#282828"),
            )
            self.assertEqual(editor.styleSheet(), "")
        finally:
            if editor is not None:
                editor.hide()
                editor.deleteLater()
            self.app.setPalette(original)
            self.app.processEvents()

    def test_main_window_inherits_application_palette(self):
        original = self.app.palette()
        palette = QPalette(original)
        palette.setColor(QPalette.Window, QColor("#161616"))
        palette.setColor(QPalette.Base, QColor("#202020"))
        palette.setColor(QPalette.Button, QColor("#282828"))
        self.app.setPalette(palette)
        config = deepcopy(DEFAULT_CONFIG)
        config.update(
            {
                "display_enabled": False,
                "input_monitor_enabled": False,
                "notifications": False,
            }
        )
        state = deepcopy(DEFAULT_STATE)
        window = None
        try:
            with patch(
                "amp_autopower.load_json",
                side_effect=[config, state],
            ), patch("amp_autopower.save_json"):
                window = MainWindow(self.app)
            self.assertEqual(
                window.palette().color(QPalette.Window),
                QColor("#161616"),
            )
            self.assertEqual(
                window.list.palette().color(QPalette.Base),
                QColor("#202020"),
            )
            self.assertEqual(window.styleSheet(), "")
        finally:
            if window is not None:
                window.scheduler.stop()
                window.activity_ui_timer.stop()
                window.compact_display.hide()
                window.tray.hide()
                window.hide()
                window.deleteLater()
            self.app.setPalette(original)
            self.app.processEvents()

    def test_compact_display_keeps_its_black_green_style(self):
        original = self.app.palette()
        palette = QPalette(original)
        palette.setColor(QPalette.Window, QColor("#ffffff"))
        palette.setColor(QPalette.WindowText, QColor("#000000"))
        self.app.setPalette(palette)
        display = None
        try:
            display = CompactDisplayWindow()
            display.apply_options(DisplayOptions(transparency_percent=25))
            display.show()
            self.app.processEvents()
            image = QImage(display.size(), QImage.Format_ARGB32)
            image.fill(0)
            display.render(image)
            background = image.pixelColor(2, 50)
            self.assertEqual(
                (background.red(), background.green(), background.blue()),
                (0, 0, 0),
            )
            self.assertEqual(background.alpha(), 191)
            self.assertIn("#5cff5c", display.styleSheet())
        finally:
            if display is not None:
                display.hide()
                display.deleteLater()
            self.app.setPalette(original)
            self.app.processEvents()


class PaletteApp:
    def __init__(self, palette):
        self._palette = palette
        self.set_palette_calls = 0

    def palette(self):
        return self._palette

    def setPalette(self, palette):
        self._palette = palette
        self.set_palette_calls += 1


def kde_colors(dark):
    if dark:
        window = (45, 50, 70)
        text = (222, 222, 222)
        view = (35, 40, 55)
        button = (25, 30, 45)
    else:
        window = (239, 240, 241)
        text = (35, 38, 41)
        view = (255, 255, 255)
        button = (239, 240, 241)
    return {
        "Colors:Window": {
            "BackgroundNormal": ",".join(map(str, window)),
            "ForegroundNormal": ",".join(map(str, text)),
        },
        "Colors:View": {
            "BackgroundNormal": ",".join(map(str, view)),
            "BackgroundAlternate": ",".join(map(str, window)),
            "ForegroundNormal": ",".join(map(str, text)),
        },
        "Colors:Button": {
            "BackgroundNormal": ",".join(map(str, button)),
            "ForegroundNormal": ",".join(map(str, text)),
        },
    }


if __name__ == "__main__":
    unittest.main()
