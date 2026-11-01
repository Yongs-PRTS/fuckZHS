#!/usr/bin/env python3
"""Local web application for the ZHS learning assistant."""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import shutil
import subprocess
import time
import sys
import threading
import webbrowser
from collections import deque
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit
from urllib.request import urlopen

from app_config import (
    APP_ROOT,
    CONFIG_PATH,
    COOKIES_PATH,
    EXECUTION_PATH,
    RUNTIME_DIR,
    ensure_config,
    env_var,
    load_config,
    resource_path,
    write_json_atomic,
)


WEB_ROOT = resource_path("web")
LOG_LEVELS = (("warning.log", "warn"), ("error.log", "error"), ("info.log", "info"))
# "stopping" counts as final too: otherwise a user-requested stop is later
# reported as a crash by the monitor thread.
FINAL_STATES = {"done", "failed", "stopped", "stopping", "captcha", "manual"}
MAX_REQUEST_BYTES = 1_000_000
MAX_PROCESS_LOG_LIMIT = 500
COURSE_TYPE_LABELS = {
    "zhidao": "知到共享课",
    "hike": "校内学分课",
    "ai": "AI 课程 · 自动答题",
}
AI_PLACEHOLDER_KEYS = {"", "sk-", "sk-xxx", "your-api-key", "changeme"}
# 日志行形如：2026-10-25 16:32:28,173:  消息正文
LOG_LINE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?:,\d{3})?:\s{0,2}(.*)$")
HOMEWORK_CACHE = {"at": 0.0, "items": []}
HOMEWORK_CACHE_TTL = 60.0


def mask_secret(value: str) -> str:
    """只露头尾，日志和界面都用它"""
    if not value:
        return ""
    if len(value) <= 10:
        return "•" * len(value)
    return f"{value[:6]}…{value[-4:]}"


def current_llm() -> dict:
    """当前生效的答题模型配置（Key 只给掩码）"""
    section = load_config().get("llm") or {}
    api_key = str(section.get("api_key") or "").strip()
    has_saved = bool(api_key and api_key not in AI_PLACEHOLDER_KEYS)
    env_key = ""
    for name in ("HOMEWORK_LLM_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
        value = env_var(name)
        if value and value not in AI_PLACEHOLDER_KEYS:
            env_key = name
            break
    return {
        "provider": str(section.get("provider") or "").strip(),
        "base_url": str(section.get("base_url") or "").strip(),
        "model": str(section.get("model") or "").strip(),
        "api_key_masked": mask_secret(api_key),
        "has_key": has_saved or bool(env_key),
        "key_source": "config" if has_saved else ("env" if env_key else ""),
        "env_var": env_key,
    }


def save_llm(payload: dict) -> dict:
    provider = str(payload.get("provider") or "").strip()[:40]
    base_url = str(payload.get("base_url") or "").strip()[:300]
    model = str(payload.get("model") or "").strip()[:120]
    api_key = payload.get("api_key")
    api_key = api_key.strip() if isinstance(api_key, str) else ""

    if base_url and not re.match(r"^https?://", base_url):
        raise ValueError("API 地址必须以 http:// 或 https:// 开头")
    if base_url and " " in base_url:
        raise ValueError("API 地址不能包含空格")
    if api_key and (" " in api_key or "\n" in api_key):
        raise ValueError("API Key 不能包含空格或换行")

    config = load_config()
    section = config.setdefault("llm", {})
    for field, value in (("provider", provider), ("base_url", base_url), ("model", model)):
        if field in payload:
            section[field] = value
    if api_key:
        section["api_key"] = api_key
    elif payload.get("clear_key"):
        section["api_key"] = ""
    write_json_atomic(CONFIG_PATH, config)
    return current_llm()


def test_llm(payload: dict) -> dict:
    """用界面里当前的（未保存）值试一次调用"""
    from homework import AnswerEngine

    base_url = str(payload.get("base_url") or "").strip()
    model = str(payload.get("model") or "").strip()
    api_key = payload.get("api_key")
    api_key = api_key.strip() if isinstance(api_key, str) else ""
    engine = AnswerEngine(base_url=base_url, api_key=api_key, model=model)
    started = time.time()
    reply = engine.ping()
    return {
        "ok": True,
        "reply": reply[:80],
        "endpoint": engine.endpoint,
        "model": engine.model,
        "seconds": round(time.time() - started, 2),
    }


def load_homework(force: bool = False) -> list[dict]:
    """扫描可做的作业（走接口，不触发验证码）；结果缓存 60 秒"""
    now = time.time()
    cached = HOMEWORK_CACHE["items"]
    if not force and cached and now - HOMEWORK_CACHE["at"] < HOMEWORK_CACHE_TTL:
        return cached
    if not COOKIES_PATH.is_file():
        raise RuntimeError("尚未登录，请先扫码登录")
    from homework_browser import collect_pending, describe

    _, _, pending = collect_pending(COOKIES_PATH)
    items = [describe(item) for item in pending]
    HOMEWORK_CACHE["at"] = now
    HOMEWORK_CACHE["items"] = items
    return items


def read_json(path: Path, fallback):
    try:
        with path.open("r", encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, ValueError, TypeError):
        return fallback


def file_timestamp(path: Path) -> str | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds")
    except OSError:
        return None


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def is_console_running(url: str) -> bool:
    try:
        with urlopen(f"{url}/api/health", timeout=1.5) as response:
            payload = json.load(response)
        return response.status == 200 and payload.get("ok") is True
    except (OSError, ValueError, TypeError):
        return False


def show_startup_error(message: str) -> None:
    if getattr(sys, "frozen", False):
        import ctypes

        ctypes.windll.user32.MessageBoxW(0, message, "ZHS 学习助手", 0x10)
    else:
        print(message, file=sys.stderr)


def load_courses() -> list[dict[str, str]]:
    raw_courses = read_json(EXECUTION_PATH, [])
    if not isinstance(raw_courses, list):
        return []

    courses = []
    seen = set()
    for item in raw_courses:
        if not isinstance(item, dict) or item.get("id") is None:
            continue
        course_id = str(item["id"]).strip()
        if not course_id or course_id in seen:
            continue
        seen.add(course_id)
        course_type = str(item.get("type") or "").strip()
        courses.append(
            {
                "name": str(item.get("name") or "未命名课程").strip(),
                "id": course_id,
                "type": course_type,
                "classId": str(item.get("classId") or "").strip(),
                "label": COURSE_TYPE_LABELS.get(course_type, "本地课程"),
            }
        )
    return courses


def ai_state(config: dict) -> dict:
    """Describes which answering engine will generate exam answers."""
    ai_config = config.get("ai") or {}
    enabled = bool(ai_config.get("enabled"))
    use_zhidao_ai = bool(ai_config.get("use_zhidao_ai", True))
    openai_config = ai_config.get("openai") or {}
    api_key = str(openai_config.get("api_key") or "").strip()
    key_ready = bool(api_key) and api_key not in AI_PLACEHOLDER_KEYS

    if not enabled:
        engine, ready = "已停用", False
    elif use_zhidao_ai:
        engine, ready = "知到 AI", True
    elif key_ready:
        model = str(openai_config.get("model_name") or "默认模型")
        engine, ready = f"OpenAI · {model}", True
    else:
        engine, ready = "OpenAI · 未配置 Key", False

    return {
        "ai_enabled": enabled,
        "ai_engine": engine,
        "ai_ready": ready,
        "ai_backend": "zhidao" if use_zhidao_ai else "openai",
        "ai_use_stream": bool(ai_config.get("use_stream", False)),
    }


def load_summary() -> dict:
    config = load_config()
    meta = read_json(resource_path("meta.json"), {})
    if not isinstance(meta, dict):
        meta = {}

    latest_log = max(
        (APP_ROOT / "logs" / name for name, _ in LOG_LEVELS),
        key=lambda path: path.stat().st_mtime if path.exists() else 0,
        default=APP_ROOT / "logs" / "info.log",
    )
    courses = load_courses()
    return {
        "app": "ZHS Study Console",
        "version": str(meta.get("version") or "unknown"),
        "author": str(meta.get("author") or ""),
        "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "course_count": len(courses),
        "ai_course_count": sum(1 for course in courses if course.get("type") == "ai"),
        **ai_state(config),
        "login_mode": "二维码登录" if config.get("qrlogin", True) else "账号登录",
        "tree_view": bool(config.get("tree_view", True)),
        "progressbar_view": bool(config.get("progressbar_view", True)),
        "config_ready": (APP_ROOT / "config.json").is_file(),
        "course_list_ready": EXECUTION_PATH.is_file(),
        "cookies_ready": COOKIES_PATH.is_file(),
        "homework_ready": COOKIES_PATH.is_file(),
        "courses_updated_at": file_timestamp(EXECUTION_PATH),
        "latest_activity_at": file_timestamp(latest_log),
        "packaged": bool(getattr(sys, "frozen", False)),
        "desktop": os.environ.get("ZHS_DESKTOP") == "1",
    }


def collect_log_entries(limit: int) -> list[dict[str, str]]:
    entries = []
    for filename, level in LOG_LEVELS:
        path = APP_ROOT / "logs" / filename
        if not path.is_file():
            continue
        try:
            with path.open("r", encoding="utf-8", errors="replace") as stream:
                lines = deque(stream, maxlen=max(limit * 2, limit))
        except OSError:
            continue
        current = None
        for line in lines:
            text = line.strip()
            match = LOG_LINE_RE.match(text) if len(text) > 19 else None
            if match:
                current = {
                    "time": match.group(1),
                    "message": match.group(2).strip(),
                    "level": level,
                }
                entries.append(current)
            elif current and level in {"warn", "error"}:
                current["message"] = f"{current['message']}\n{text}"[-6000:]

    entries.sort(key=lambda item: item["time"])
    return entries[-limit:]


def load_recent_logs(limit: int = 14) -> list[dict[str, str]]:
    return collect_log_entries(limit)[::-1]


def load_process_logs(limit: int = 300) -> list[dict[str, str]]:
    return collect_log_entries(limit)


def reset_stale_status() -> None:
    """程序启动时，把上次异常中断留下的"运行中"状态归位"""
    path = RUNTIME_DIR / "action.json"
    current = read_json(path, {})
    if not isinstance(current, dict):
        return
    state = current.get("state")
    if state in FINAL_STATES or state in (None, "", "idle"):
        return
    current.update({
        "state": "stopped",
        "message": "上次任务已中断（程序重启），进度已保存在服务端，可重新开始",
        "error": "",
        "updated_at": now_iso(),
    })
    write_json_atomic(path, current)


class ActionManager:
    def __init__(self) -> None:
        reset_stale_status()
        self._lock = threading.Lock()
        self._process: subprocess.Popen | None = None
        self._log_stream = None

    @property
    def status_path(self) -> Path:
        return RUNTIME_DIR / "action.json"

    @property
    def qr_path(self) -> Path:
        return RUNTIME_DIR / "qr.png"

    def _worker_command(self, request_path: Path) -> list[str]:
        if getattr(sys, "frozen", False):
            return [sys.executable, "--worker", "--request", str(request_path)]
        return [sys.executable, str(APP_ROOT / "app_worker.py"), "--request", str(request_path)]

    def start(self, action: str, options: dict | None = None) -> tuple[bool, str]:
        with self._lock:
            if self._process and self._process.poll() is None:
                return False, "已有任务正在运行"

            ensure_config()
            RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
            self.qr_path.unlink(missing_ok=True)
            request_path = RUNTIME_DIR / "request.json"
            write_json_atomic(
                request_path,
                {"action": action, "options": options or {}, "created_at": now_iso()},
            )
            self._write_state("starting", "正在启动任务")

            log_path = RUNTIME_DIR / "worker.log"
            self._log_stream = log_path.open("w", encoding="utf-8")
            creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            try:
                self._process = subprocess.Popen(
                    self._worker_command(request_path),
                    cwd=APP_ROOT,
                    stdin=subprocess.DEVNULL,
                    stdout=self._log_stream,
                    stderr=subprocess.STDOUT,
                    creationflags=creation_flags,
                )
            except Exception as exc:
                self._close_log()
                self._write_state("failed", f"无法启动任务：{exc}", error=str(exc))
                return False, str(exc)

            if action == "sync" and (options or {}).get("force_login"):
                HOMEWORK_CACHE["at"] = 0.0
                HOMEWORK_CACHE["items"] = []
            threading.Thread(target=self._monitor_process, args=(self._process,), daemon=True).start()
            return True, "正在获取登录二维码" if action == "sync" else "任务已启动"

    def _monitor_process(self, process: subprocess.Popen) -> None:
        exit_code = process.wait()
        with self._lock:
            if self._process is not process:
                return
            current = read_json(self.status_path, {})
            state = current.get("state") if isinstance(current, dict) else None
            if exit_code != 0 and state not in FINAL_STATES:
                self._write_state("failed", f"后台任务异常退出（代码 {exit_code}）")
            elif exit_code == 0 and state not in FINAL_STATES:
                self._write_state("done", "任务已完成", progress=100)
            self._close_log()

    def _close_log(self) -> None:
        if self._log_stream:
            try:
                self._log_stream.close()
            except OSError:
                pass
            self._log_stream = None

    def _write_state(self, state: str, message: str, *, progress: int = 0, error: str = "") -> None:
        write_json_atomic(
            self.status_path,
            {
                "state": state,
                "message": message,
                "progress": progress,
                "current": progress,
                "total": 0,
                "error": error,
                "updated_at": now_iso(),
            },
        )

    def stop(self) -> bool:
        with self._lock:
            process = self._process
            if not process or process.poll() is not None:
                return False
            self._write_state("stopping", "正在停止任务")
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
        self.qr_path.unlink(missing_ok=True)
        self._write_state("stopped", "任务已停止")
        return True

    def snapshot(self) -> dict:
        active = bool(self._process and self._process.poll() is None)
        status = read_json(self.status_path, {}) if self.status_path.is_file() else {}
        if not isinstance(status, dict) or not status:
            status = {
                "state": "idle",
                "message": "尚未开始任务",
                "progress": 0,
                "current": 0,
                "total": 0,
                "error": "",
                "updated_at": None,
            }
        status["active"] = active
        status["qr_available"] = (
            active and status.get("state") == "waiting_login" and self.qr_path.is_file()
        )
        return status

    def shutdown(self) -> None:
        try:
            self.stop()
        finally:
            self._close_log()


class ConsoleServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address, handler_class, action_manager: ActionManager):
        super().__init__(server_address, handler_class)
        self.action_manager = action_manager
        self.quit_callback = None
        self._shutdown_started = False
        self._shutdown_lock = threading.Lock()

    def request_shutdown(self) -> None:
        with self._shutdown_lock:
            if self._shutdown_started:
                return
            self._shutdown_started = True
        self.action_manager.shutdown()
        if self.quit_callback:
            try:
                self.quit_callback()
            except Exception:
                pass
        threading.Thread(target=self.shutdown, daemon=True).start()


class ConsoleRequestHandler(BaseHTTPRequestHandler):
    server_version = "ZHSConsole/2.0"

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/api/summary":
            self.send_json(load_summary())
            return
        if path == "/api/courses":
            self.send_json({"courses": load_courses()})
            return
        if path == "/api/logs":
            self.send_json({"entries": load_recent_logs()})
            return
        if path == "/api/process":
            requested = parse_qs(urlsplit(self.path).query).get("limit", ["300"])[0]
            try:
                limit = max(50, min(int(requested), MAX_PROCESS_LOG_LIMIT))
            except ValueError:
                limit = 300
            self.send_json({"entries": load_process_logs(limit), "limit": limit})
            return
        if path == "/api/homework":
            force = "force=1" in (urlsplit(self.path).query or "")
            try:
                items = load_homework(force=force)
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.SERVICE_UNAVAILABLE)
                return
            self.send_json({"ok": True, "items": items})
            return
        if path == "/api/settings":
            self.send_json({"ok": True, "llm": current_llm()})
            return
        if path == "/api/health":
            self.send_json({"ok": True})
            return
        if path == "/api/status":
            self.send_json(self.server.action_manager.snapshot())
            return
        if path == "/api/qr":
            self.serve_qr()
            return
        if path == "/README.md":
            self.send_file(resource_path("README.md"), "text/markdown; charset=utf-8")
            return
        self.serve_static(path)

    def do_HEAD(self) -> None:
        path = urlsplit(self.path).path
        if path.startswith("/api/"):
            self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
            self.send_header("Allow", "GET, POST")
            self.end_headers()
            return
        self.serve_static(path, include_body=False)

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if not path.startswith("/api/action/") and path not in {
            "/api/quit",
            "/api/settings",
            "/api/settings/test",
        }:
            self.send_json({"ok": False, "error": "接口不存在"}, HTTPStatus.NOT_FOUND)
            return
        if self.headers.get_content_type() != "application/json":
            self.send_json({"ok": False, "error": "请求必须使用 JSON"}, HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > MAX_REQUEST_BYTES:
                raise ValueError("请求内容过大")
            payload = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(payload, dict):
                raise ValueError("请求内容格式错误")
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return

        if path == "/api/action/sync":
            # 切换账号时强制重新扫码，并清掉上一个账号的作业缓存
            force_login = bool(payload.get("force_login", False))
            self.handle_start("sync", {"force_login": force_login})
        elif path == "/api/action/run":
            try:
                options = self.validate_run_options(payload)
            except ValueError as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self.handle_start("run", options)
        elif path == "/api/action/homework":
            try:
                options = self.validate_homework_options(payload)
            except ValueError as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            if not current_llm()["has_key"]:
                self.send_json(
                    {"ok": False, "error": "还没配置答题模型，请先在「答题模型」里填写 API Key"},
                    HTTPStatus.BAD_REQUEST,
                )
                return
            self.handle_start("homework", options)
        elif path == "/api/action/ai":
            try:
                options = self.validate_ai_options(payload)
            except ValueError as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self.handle_start("ai", options)
        elif path == "/api/settings":
            try:
                llm = save_llm(payload)
            except ValueError as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            except Exception as exc:
                self.send_json({"ok": False, "error": f"保存失败：{exc}"[:300]}, HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            self.send_json({"ok": True, "llm": llm})
        elif path == "/api/settings/test":
            try:
                result = test_llm(payload)
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)[:300]}, HTTPStatus.BAD_REQUEST)
                return
            self.send_json(result)
        elif path == "/api/action/stop":
            self.send_json({"ok": True, "stopped": self.server.action_manager.stop()})
        elif path == "/api/quit":
            self.send_json({"ok": True})
            self.server.request_shutdown()
        else:
            self.send_json({"ok": False, "error": "接口不存在"}, HTTPStatus.NOT_FOUND)

    def validate_run_options(self, payload: dict) -> dict:
        raw_ids = payload.get("course_ids") or []
        if not isinstance(raw_ids, list):
            raise ValueError("课程列表格式错误")
        course_ids = list(dict.fromkeys(str(item).strip() for item in raw_ids if str(item).strip()))
        if len(course_ids) > 500:
            raise ValueError("一次最多运行 500 门课程")

        def number(name: str, minimum: float, maximum: float, integer: bool = False):
            value = payload.get(name)
            if value in (None, ""):
                return None
            try:
                parsed = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{name} 必须是数字") from exc
            if parsed < minimum or parsed > maximum:
                raise ValueError(f"{name} 必须在 {minimum} 到 {maximum} 之间")
            return int(parsed) if integer else parsed

        speed = number("speed", 0.1, 8)
        threshold = number("threshold", 0.1, 2)
        limit = number("limit", 0, 1440, integer=True)
        return {
            "course_ids": course_ids,
            "speed": speed,
            "threshold": threshold,
            "limit": limit or 0,
        }

    def validate_homework_options(self, payload: dict) -> dict:
        """作业答题：勾选的作业 key + 是否自动提交 + 等验证码的秒数"""
        raw_keys = payload.get("keys") or []
        if not isinstance(raw_keys, list):
            raise ValueError("作业列表格式错误")
        keys = [str(key).strip() for key in raw_keys if str(key).strip()]
        if not keys:
            raise ValueError("请至少勾选一份作业")
        if len(keys) > 50:
            raise ValueError("一次最多处理 50 份作业")

        try:
            wait = int(float(payload.get("wait") or 300))
        except (TypeError, ValueError) as exc:
            raise ValueError("等待时长必须是数字") from exc
        if wait < 30 or wait > 1800:
            raise ValueError("等待验证码的时长需在 30 到 1800 秒之间")

        return {"keys": keys, "submit": bool(payload.get("submit", False)), "wait": wait}

    def validate_ai_options(self, payload: dict) -> dict:
        """Validate the AI course form: course id + class id + exam switch."""

        def read_digits(label: str, *keys: str) -> str:
            value = ""
            for key in keys:
                candidate = payload.get(key)
                if candidate not in (None, ""):
                    value = str(candidate).strip()
                    break
            if not value:
                raise ValueError(f"请填写{label}")
            if not value.isdigit():
                raise ValueError(f"{label}必须是纯数字")
            if len(value) > 20:
                raise ValueError(f"{label}长度不合法")
            return value

        course_id = read_digits("AI 课程 ID", "course_id", "courseId")
        class_id = read_digits("班级 ID", "class_id", "classId")
        no_exam = payload.get("no_exam", False)
        if not isinstance(no_exam, bool):
            no_exam = str(no_exam).lower() in {"1", "true", "yes", "on"}
        return {
            "course_id": course_id,
            "class_id": class_id,
            "no_exam": bool(no_exam),
        }

    def handle_start(self, action: str, options: dict) -> None:
        ok, message = self.server.action_manager.start(action, options)
        self.send_json(
            {"ok": ok, "message": message, "status": self.server.action_manager.snapshot()},
            HTTPStatus.ACCEPTED if ok else HTTPStatus.CONFLICT,
        )

    def serve_qr(self) -> None:
        target = self.server.action_manager.qr_path
        if not target.is_file():
            self.send_json({"ok": False, "error": "二维码尚未生成"}, HTTPStatus.NOT_FOUND)
            return
        self.send_file(target, "image/png", cache_control="no-store")

    def serve_static(self, url_path: str, include_body: bool = True) -> None:
        relative_path = unquote(url_path).lstrip("/") or "index.html"
        if relative_path.endswith("/"):
            relative_path += "index.html"
        try:
            target = (WEB_ROOT / relative_path).resolve()
            target.relative_to(WEB_ROOT.resolve())
        except (OSError, ValueError):
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        content_type, _ = mimetypes.guess_type(target.name)
        self.send_file(
            target,
            f"{content_type or 'application/octet-stream'}; charset=utf-8",
            include_body,
        )

    def send_file(
        self,
        target: Path,
        content_type: str,
        include_body: bool = True,
        cache_control: str = "no-cache",
    ) -> None:
        if not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(target.stat().st_size))
        self.send_header("Cache-Control", cache_control)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if include_body:
            with target.open("rb") as stream:
                shutil.copyfileobj(stream, self.wfile)

    def send_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, message: str, *args) -> None:
        if not getattr(sys, "frozen", False):
            print(f"[web] {self.address_string()} - {message % args}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the ZHS local application.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1)")
    parser.add_argument("--port", default=8765, type=int, help="Bind port (default: 8765)")
    parser.add_argument("--open", action="store_true", help="Open the browser after startup")
    parser.add_argument("--no-browser", action="store_true", help="Do not open the browser")
    args = parser.parse_args()

    if not (WEB_ROOT / "index.html").is_file():
        raise SystemExit(f"Frontend files are missing from {WEB_ROOT}")
    ensure_config()
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    action_manager = ActionManager()
    url = f"http://{args.host}:{args.port}"
    try:
        server = ConsoleServer((args.host, args.port), ConsoleRequestHandler, action_manager)
    except OSError as exc:
        if is_console_running(url):
            if not args.no_browser:
                webbrowser.open(url)
        else:
            show_startup_error(f"无法启动本地服务：{exc}")
        return
    print(f"ZHS console: {url}")

    if (args.open or getattr(sys, "frozen", False)) and not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        action_manager.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
