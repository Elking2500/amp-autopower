import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import amp_autopower as amp


class VersionSourceTests(unittest.TestCase):
    def setUp(self):
        self.source_dir = Path(amp.__file__).resolve().parent

    def _runtime_copy(self, version_marker="present", version="2.0.0-rc1"):
        temporary = tempfile.TemporaryDirectory()
        target = Path(temporary.name)
        for name in (
            "amp_autopower.py",
            "condition_engine.py",
            "compact_display.py",
        ):
            shutil.copy2(self.source_dir / name, target / name)
        if version_marker == "present":
            (target / "VERSION").write_text(version + "\n", encoding="utf-8")
        return temporary, target

    def _environment(self, root):
        home = Path(root) / "home"
        runtime = Path(root) / "runtime"
        runtime.mkdir(mode=0o700)
        environment = os.environ.copy()
        environment.update(
            {
                "HOME": str(home),
                "XDG_CONFIG_HOME": str(home / ".config"),
                "XDG_CACHE_HOME": str(home / ".cache"),
                "XDG_DATA_HOME": str(home / ".local/share"),
                "XDG_STATE_HOME": str(home / ".local/state"),
                "XDG_RUNTIME_DIR": str(runtime),
                "QT_QPA_PLATFORM": "offscreen",
            }
        )
        environment.pop("PYTHONPATH", None)
        return environment

    def test_source_runtime_uses_adjacent_version(self):
        expected = (self.source_dir / "VERSION").read_text(
            encoding="utf-8"
        ).strip()
        self.assertEqual(amp.APP_VERSION, expected)
        self.assertEqual(amp.load_app_version(), expected)

    def test_temporary_runtime_reports_version_independent_of_cwd(self):
        temporary, target = self._runtime_copy()
        self.addCleanup(temporary.cleanup)
        result = subprocess.run(
            [sys.executable, str(target / "amp_autopower.py"), "--version"],
            cwd="/",
            env=self._environment(target),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "2.0.0-rc1")

    def test_cli_status_uses_adjacent_version(self):
        temporary, target = self._runtime_copy()
        self.addCleanup(temporary.cleanup)
        result = subprocess.run(
            [sys.executable, str(target / "amp_autopower.py"), "--status"],
            cwd="/",
            env=self._environment(target),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Versión: 2.0.0-rc1", result.stdout)

    def test_missing_empty_or_invalid_version_is_safe(self):
        for marker, value in (
            ("missing", ""),
            ("present", ""),
            ("present", "not a version"),
        ):
            with self.subTest(marker=marker, value=value):
                temporary, target = self._runtime_copy(marker, value)
                try:
                    result = subprocess.run(
                        [
                            sys.executable,
                            str(target / "amp_autopower.py"),
                            "--version",
                        ],
                        cwd="/",
                        env=self._environment(target),
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                finally:
                    temporary.cleanup()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), "unknown")

    def test_unknown_version_disables_update_comparison(self):
        self.assertFalse(amp.is_newer_version("2.0.0", "unknown"))
        self.assertFalse(amp.is_newer_version("invalid", "1.3.0"))
        self.assertTrue(amp.is_newer_version("2.0.0", "1.3.0"))

    def test_install_and_uninstall_manage_runtime_version(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            mock_bin = root / "bin"
            mock_bin.mkdir()
            for command in ("systemctl", "sudo"):
                executable = mock_bin / command
                executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                executable.chmod(0o755)
            environment = self._environment(root)
            environment["PATH"] = f"{mock_bin}:/usr/bin:/bin"

            install = subprocess.run(
                ["bash", str(self.source_dir / "install.sh"), "--update-no-restart"],
                cwd=self.source_dir,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(install.returncode, 0, install.stderr)
            installed_version = (
                home / ".local/share/amp-autopower/VERSION"
            )
            self.assertTrue(installed_version.is_file())
            self.assertEqual(
                installed_version.read_text(encoding="utf-8").strip(),
                amp.APP_VERSION,
            )

            config_dir = home / ".config/amp-autopower"
            config_dir.mkdir(parents=True, exist_ok=True)
            sentinel = config_dir / "keep.json"
            sentinel.write_text("{}\n", encoding="utf-8")
            uninstall = subprocess.run(
                ["bash", str(self.source_dir / "uninstall.sh")],
                cwd=self.source_dir,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(uninstall.returncode, 0, uninstall.stderr)
            self.assertFalse(installed_version.exists())
            self.assertTrue(sentinel.exists())

    def test_runtime_has_no_hardcoded_release_version(self):
        source = (self.source_dir / "amp_autopower.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn('APP_VERSION = "1.3.0"', source)


if __name__ == "__main__":
    unittest.main()
