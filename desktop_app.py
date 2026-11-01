"""Native desktop host for the ZHS learning assistant."""

from __future__ import annotations

import ctypes
import logging
import os
import tempfile
import threading
import time
from ctypes import wintypes
from pathlib import Path

from app_config import RUNTIME_DIR, ensure_config

WINDOW_TITLE = "ZHS 学习助手"
log = logging.getLogger("zhs.desktop")


class DesktopInstance:
    """Hold an OS file lock for the entire UI/server lifetime, including shutdown."""

    def __init__(self, runtime: Path) -> None:
        self.path = runtime / "desktop.lock"
        self.stream = None

    def acquire(self) -> bool:
        import msvcrt

        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = os.fdopen(os.open(self.path, os.O_RDWR | os.O_CREAT), "r+b")
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            stream.close()
            return False
        self.stream = stream
        stream.write(b"1" + str(os.getpid()).encode("ascii"))
        stream.truncate()
        stream.flush()
        return True

    def activate_existing(self) -> None:
        try:
            # The PID sits outside the locked byte so other processes can read it.
            with self.path.open("rb") as stream:
                stream.seek(1)
                pid = int(stream.read(32))
            handle = _find_window_for_pid(pid)
            if handle:
                user32 = ctypes.windll.user32
                user32.ShowWindowAsync(wintypes.HWND(handle), 9)  # SW_RESTORE
                user32.SetForegroundWindow(wintypes.HWND(handle))
        except (OSError, ValueError):
            pass  # First instance may still be starting.

    def close(self) -> None:
        if self.stream is not None:
            self.stream.close()  # Windows releases the lock on exit/crash as well.
            self.stream = None


def _find_window_for_pid(pid: int) -> int:
    """按进程号找可见的顶层窗口（窗口标题会被网页 <title> 覆盖，不能靠标题找）"""
    user32 = ctypes.windll.user32
    found: list[int] = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def callback(hwnd, _):
        window_pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid))
        if window_pid.value == pid and user32.IsWindowVisible(hwnd):
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            if rect.right - rect.left > 200 and rect.bottom - rect.top > 200:
                found.append(hwnd)
        return True

    user32.EnumWindows(callback_type(callback), 0)
    return found[0] if found else 0


def _prepare_webview_storage() -> Path:
    """Use a fresh profile after a crash; never delete a live/locked WebView profile."""
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    storage = RUNTIME_DIR / "webview"
    marker = RUNTIME_DIR / "webview.dirty"
    selection = RUNTIME_DIR / "webview-profile.txt"
    if selection.exists():
        name = selection.read_text(encoding="utf-8").strip()
        if name.startswith("webview-recovery-") and Path(name).name == name:
            storage = RUNTIME_DIR / name
    if marker.exists():
        storage = Path(tempfile.mkdtemp(prefix="webview-recovery-", dir=RUNTIME_DIR))
        selection.write_text(storage.name, encoding="utf-8")
        log.warning("Previous exit was unclean; using fresh profile %s", storage.name)
    marker.write_text("1", encoding="utf-8")
    return storage


def _clear_storage_marker() -> None:
    try:
        (RUNTIME_DIR / "webview.dirty").unlink(missing_ok=True)
    except OSError:
        pass


def _start_watchdog(pid: int, stopped: threading.Event) -> None:
    """Monitor the full startup period, even if the first probe is responsive."""

    def run() -> None:
        handle = 0
        for _ in range(40):
            if stopped.is_set():
                return
            handle = _find_window_for_pid(pid)
            if handle:
                break
            stopped.wait(0.5)
        if not handle:
            return
        user32 = ctypes.windll.user32
        strikes = 0
        for _ in range(40):
            if stopped.wait(3) or not user32.IsWindow(wintypes.HWND(handle)):
                return
            try:
                hung = user32.IsHungAppWindow(wintypes.HWND(handle))
            except Exception:
                return
            if hung:
                strikes += 1
                if strikes >= 5:
                    log.error("Native window remained unresponsive during startup")
                    import faulthandler

                    with (RUNTIME_DIR / "stacks.txt").open("a", encoding="utf-8") as stream:
                        faulthandler.dump_traceback(file=stream, all_threads=True)
                    user32.MessageBoxW(
                        None,
                        "启动没有完成，界面一直无响应。\n\n程序会退出，请重新双击打开。",
                        "ZHS 学习助手",
                        0x30,
                    )
                    os._exit(3)
            else:
                strikes = 0

    threading.Thread(target=run, daemon=True).start()


def _blank_icon_path() -> str:
    """生成一张全透明图标：WinForms 在创建窗口时自己设置，不跨线程发消息"""
    path = RUNTIME_DIR / "blank.ico"
    if path.is_file():
        return str(path)
    try:
        from PIL import Image

        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        Image.new("RGBA", (32, 32), (0, 0, 0, 0)).save(
            path, format="ICO", sizes=[(16, 16), (32, 32), (48, 48)]
        )
        return str(path)
    except Exception:
        return ""


def _polish_native_window(pid: int) -> None:
    """Win11：雾面标题栏 + 圆角，并去掉标题栏的图标与文字（失败时静默跳过）

    只用 Win32 消息：改 WinForms 属性会重建窗口句柄，而句柄里挂着 WebView2，
    重建会让 UI 线程卡死（表现为程序无响应）。
    """
    window_handle = 0
    for _ in range(60):
        window_handle = _find_window_for_pid(pid)
        if window_handle:
            break
        time.sleep(0.2)
    if not window_handle:
        return

    dwm = ctypes.windll.dwmapi
    handle = wintypes.HWND(window_handle)

    # 等 WebView2 把窗口画完，别在初始化阶段抢消息循环
    time.sleep(2.5)
    try:  # DWMWA_WINDOW_CORNER_PREFERENCE = DWMWCP_ROUND
        preference = ctypes.c_int(2)
        dwm.DwmSetWindowAttribute(handle, 33, ctypes.byref(preference), ctypes.sizeof(preference))
    except Exception:
        pass
    try:  # DWMWA_SYSTEMBACKDROP_TYPE = Mica（标题栏雾面透明）
        backdrop = ctypes.c_int(2)
        dwm.DwmSetWindowAttribute(handle, 38, ctypes.byref(backdrop), ctypes.sizeof(backdrop))
    except Exception:
        pass


class DesktopApi:
    """暴露给前端的少量窗口操作（缩放把手用）"""

    def __init__(self) -> None:
        # pywebview recursively reflects every public attribute of js_api.
        # Exposing Window here traverses native WinForms/WebView2 properties.
        self._window = None

    def resize(self, width: int, height: int) -> None:
        if self._window is None:
            return
        try:
            self._window.resize(max(960, int(width)), max(680, int(height)))
        except Exception:
            pass


def _install_stack_dump() -> None:
    """调试用：每 10 秒把所有线程栈写进 runtime/stacks.txt"""
    if os.environ.get("ZHS_STACK_DUMP") != "1":
        return
    import faulthandler

    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    stream = open(RUNTIME_DIR / "stacks.txt", "a", buffering=1, encoding="utf-8")
    faulthandler.dump_traceback_later(10, repeat=True, file=stream)


def main() -> None:
    instance = DesktopInstance(RUNTIME_DIR)
    if not instance.acquire():
        instance.activate_existing()
        return
    try:
        from logging.handlers import RotatingFileHandler

        handler = RotatingFileHandler(RUNTIME_DIR / "desktop.log", maxBytes=512_000,
                                      backupCount=2, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        log.setLevel(logging.INFO)
        log.addHandler(handler)
        logging.getLogger("pywebview").addHandler(handler)
        log.info("Starting desktop pid=%s", os.getpid())
        _run_desktop()
    except Exception:
        log.exception("Desktop startup failed")
        raise
    finally:
        instance.close()


def _run_desktop() -> None:
    os.environ["ZHS_DESKTOP"] = "1"
    _install_stack_dump()
    ensure_config()
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)

    from web_console import ActionManager, ConsoleRequestHandler, ConsoleServer
    import webview

    action_manager = ActionManager()
    server = ConsoleServer(("127.0.0.1", 0), ConsoleRequestHandler, action_manager)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    url = f"http://127.0.0.1:{server.server_port}"

    api = DesktopApi()
    window = webview.create_window(
        WINDOW_TITLE,
        url,
        width=1320,
        height=900,
        min_size=(960, 680),
        background_color="#f3f1e9",
        text_select=True,
        zoomable=True,
        resizable=True,
        js_api=api,
    )
    api._window = window
    stopped = threading.Event()
    window.events.closed += stopped.set

    def on_loaded() -> None:
        log.info("Desktop page and JS bridge loaded")
        # 页面加载完再动标题栏：WebView2 初始化期间不要碰窗口消息
        try:
            window.title = ""  # 走 pywebview 自己的接口，内部会切到 UI 线程
        except Exception:
            pass
        if os.environ.get("ZHS_NO_POLISH") == "1":
            return
        threading.Thread(target=_polish_native_window, args=(os.getpid(),), daemon=True).start()

    window.events.loaded += on_loaded

    def close_window() -> None:
        try:
            window.destroy()
        except Exception:
            pass

    server.quit_callback = close_window
    storage_dir = _prepare_webview_storage()
    _start_watchdog(os.getpid(), stopped)
    try:
        webview.start(
            icon=_blank_icon_path() or None,
            gui="edgechromium",
            debug=False,
            private_mode=False,
            storage_path=str(storage_dir),
        )
        _clear_storage_marker()
    finally:
        stopped.set()
        action_manager.shutdown()
        server.shutdown()
        server.server_close()
        log.info("Desktop shutdown complete")
        time.sleep(0.05)


if __name__ == "__main__":
    main()
