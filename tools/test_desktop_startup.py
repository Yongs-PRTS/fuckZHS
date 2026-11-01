"""Offline regression checks for desktop startup, bridge reflection and crash recovery."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import desktop_app as desktop


class ImmediateThread:
    def __init__(self, target, **kwargs):
        self.target = target

    def start(self):
        self.target()


class StartupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        (ROOT / "build").mkdir(exist_ok=True)

    def test_bridge_only_exports_resize_and_does_not_inspect_native_window(self):
        from webview.util import inject_pywebview

        touched = []

        class NativeWindow:
            @property
            def native(self):
                touched.append("native")
                return None

        def exports(api):
            scripts = []
            window = SimpleNamespace(
                _js_api=api, _functions={}, _expose_lock=threading.Lock(),
                run_js=scripts.append,
                events=SimpleNamespace(before_load=threading.Event(),
                                       loaded=threading.Event(),
                                       _pywebviewready=threading.Event()),
            )
            with patch("webview.util.load_js_files", return_value=("bootstrap", "%(functions)s")), \
                 patch("webview.util.Thread", ImmediateThread):
                inject_pywebview("edgechromium", window)
            self.assertTrue(window.events.loaded.is_set())
            return json.loads(scripts[-1])

        # Control: the original public reference really is recursively visited.
        exports(SimpleNamespace(window=NativeWindow()))
        self.assertEqual(touched, ["native"])
        touched.clear()
        api = desktop.DesktopApi()
        api._window = NativeWindow()
        self.assertEqual(exports(api), [{"func": "resize", "params": ["width", "height"]}])
        self.assertEqual(touched, [])

    def test_instance_lock_releases_on_normal_exit_and_crash(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "build") as folder:
            code = (
                "from pathlib import Path; import sys; "
                "from desktop_app import DesktopInstance; "
                "lock=DesktopInstance(Path(sys.argv[1])); "
                "print(lock.acquire(), flush=True); "
                "command=sys.stdin.readline(); "
                "__import__('os')._exit(23) if command.strip() == 'crash' else lock.close()"
            )
            first = desktop.DesktopInstance(Path(folder))
            self.assertTrue(first.acquire())
            try:
                blocked = subprocess.run([sys.executable, "-c", code, folder], input="\n",
                                         capture_output=True, text=True, cwd=ROOT, timeout=10)
                self.assertEqual(blocked.stdout.strip(), "False", blocked.stderr)
            finally:
                first.close()
            child = subprocess.Popen([sys.executable, "-c", code, folder], cwd=ROOT,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(child.stdout.readline().strip(), "True")
                child.stdin.write("crash\n")
                child.stdin.flush()
                child.wait(timeout=10)
                self.assertEqual(child.returncode, 23)
                recovered = desktop.DesktopInstance(Path(folder))
                try:
                    self.assertTrue(recovered.acquire())
                finally:
                    recovered.close()
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=10)
                child.stdin.close()
                child.stdout.close()

    def test_unclean_profile_is_preserved_and_new_profile_is_reused_after_clean_exit(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "build") as folder, \
             patch.object(desktop, "RUNTIME_DIR", Path(folder)):
            previous = desktop._prepare_webview_storage()
            previous.mkdir()
            sentinel = previous / "possibly-in-use"
            sentinel.write_text("keep", encoding="utf-8")
            recovered = desktop._prepare_webview_storage()
            self.assertNotEqual(recovered, previous)
            self.assertTrue(recovered.is_dir())
            self.assertEqual(sentinel.read_text(), "keep")
            desktop._clear_storage_marker()
            self.assertEqual(desktop._prepare_webview_storage(), recovered)
            desktop._clear_storage_marker()

    def test_watchdog_keeps_checking_after_initial_healthy_probe(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "build") as folder:
            user32 = Mock()
            user32.IsHungAppWindow.side_effect = [0, 1, 1, 1, 1, 1]
            stopped = Mock()
            stopped.is_set.return_value = False
            stopped.wait.return_value = False
            with patch.object(desktop, "RUNTIME_DIR", Path(folder)), \
                 patch.object(desktop, "_find_window_for_pid", return_value=123), \
                 patch.object(desktop.ctypes, "windll", SimpleNamespace(user32=user32)), \
                 patch.object(desktop.threading, "Thread", ImmediateThread), \
                 patch("faulthandler.dump_traceback"), \
                 patch.object(desktop.os, "_exit", side_effect=RuntimeError("watchdog exit")):
                with self.assertRaisesRegex(RuntimeError, "watchdog exit"):
                    desktop._start_watchdog(os.getpid(), stopped)
            self.assertEqual(user32.IsHungAppWindow.call_count, 6)


if __name__ == "__main__":
    (ROOT / "build").mkdir(exist_ok=True)
    unittest.main(verbosity=2)
