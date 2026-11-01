"""Background task runner used by the packaged application."""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime
from pathlib import Path

from app_config import (
    COOKIES_PATH,
    EXECUTION_PATH,
    RUNTIME_DIR,
    load_config,
    write_json_atomic,
)
from fucker import CaptchaException, Fucker
from logger import logger
from utils import cookie_jar_to_list


STATUS_PATH = RUNTIME_DIR / "action.json"
QR_PATH = RUNTIME_DIR / "qr.png"

AI_ID_KEYS = ("courseId", "course_id", "scMapId", "courseID", "id")
AI_CLASS_KEYS = ("classId", "class_id", "clazzId", "classID")
AI_NAME_KEYS = ("courseName", "course_name", "name", "title")


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def report(state: str, message: str, *, progress: int = 0, total: int = 0, error: str = "") -> None:
    write_json_atomic(
        STATUS_PATH,
        {
            "state": state,
            "message": message,
            "progress": max(0, min(progress, 100)),
            "current": progress,
            "total": total,
            "error": error,
            "updated_at": now_iso(),
            "pid": os.getpid(),
        },
    )


def write_qr(image_bytes: bytes) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    temp_path = QR_PATH.with_suffix(".tmp")
    temp_path.write_bytes(image_bytes)
    temp_path.replace(QR_PATH)
    report("waiting_login", "请使用手机扫码登录")


def save_cookies(fucker: Fucker) -> None:
    write_json_atomic(COOKIES_PATH, cookie_jar_to_list(fucker.cookies))


class ProgressBridge:
    """Translate Fucker progress events into runtime/action.json updates."""

    def __init__(self, total_courses: int = 1, state: str = "running") -> None:
        self.total_courses = max(int(total_courses or 1), 1)
        self.state = state
        self.finished_courses = 0

    def _progress(self, ratio: float = 0.0) -> int:
        ratio = min(max(ratio, 0.0), 1.0)
        share = 100.0 / self.total_courses
        return int(self.finished_courses * share + ratio * share)

    def course_start(self, index: int, message: str) -> None:
        report(self.state, message, progress=self._progress(), total=self.total_courses)

    def course_done(self, index: int, message: str) -> None:
        self.finished_courses = max(self.finished_courses, index)
        report(self.state, message, progress=self._progress(), total=self.total_courses)

    def __call__(self, message: str, current=None, total=None) -> None:
        if not message:
            return
        ratio = 0.0
        if isinstance(current, int) and isinstance(total, int) and total > 0:
            ratio = current / total
        try:
            # 也写进日志，"完整运行过程"里能看到逐条进度
            logger.info(message)
        except Exception:
            pass
        report(self.state, message, progress=self._progress(ratio), total=self.total_courses)


def build_fucker(config: dict, options: dict, progress_callback=None) -> Fucker:
    push_config = config.get("pushplus") or {}
    bark_config = config.get("bark") or {}
    return Fucker(
        proxies=config.get("proxies") or {},
        speed=options.get("speed"),
        end_thre=options.get("threshold"),
        limit=options.get("limit") or 0,
        pushplus_token=push_config.get("token", "") if push_config.get("enable") else "",
        bark_token=bark_config.get("token", "") if bark_config.get("enable") else "",
        tree_view=False,
        progressbar_view=False,
        progress_callback=progress_callback,
    )


def _first_value(item: dict, keys):
    for key in keys:
        value = item.get(key)
        if value not in (None, ""):
            return value
    return None


def _iter_ai_items(payload):
    """Walk the loosely-typed AI course payload and yield candidate records."""
    if isinstance(payload, list):
        for item in payload:
            yield item
    elif isinstance(payload, dict):
        if any(key in payload for key in AI_ID_KEYS):
            yield payload
        for value in payload.values():
            if isinstance(value, list):
                for item in value:
                    yield item
            elif isinstance(value, dict):
                yield from _iter_ai_items(value)


def extract_ai_courses(payload) -> list[dict[str, str]]:
    """Normalise the AI course list into name/id/classId/type records."""
    courses: list[dict[str, str]] = []
    seen = set()
    for item in _iter_ai_items(payload):
        if not isinstance(item, dict):
            continue
        course_id = _first_value(item, AI_ID_KEYS)
        class_id = _first_value(item, AI_CLASS_KEYS)
        if course_id in (None, "") or class_id in (None, ""):
            continue
        key = str(course_id).strip()
        if not key or key in seen:
            continue
        seen.add(key)
        courses.append(
            {
                "name": str(_first_value(item, AI_NAME_KEYS) or "AI 课程"),
                "id": key,
                "classId": str(class_id).strip(),
                "type": "ai",
            }
        )
    return courses


def fetch_courses(fucker: Fucker) -> list[dict[str, str]]:
    errors = []
    courses = []
    try:
        courses.extend(
            {
                "name": str(course.courseName or "未命名课程"),
                "id": str(course.secret),
                "type": "zhidao",
            }
            for course in fucker.getZhidaoList()
        )
    except Exception as exc:
        errors.append(str(exc))
        logger.exception(exc)

    try:
        courses.extend(
            {
                "name": str(course.courseName or "未命名课程"),
                "id": str(course.courseId),
                "type": "hike",
            }
            for course in fucker.getHikeList()
        )
    except Exception as exc:
        errors.append(str(exc))
        logger.exception(exc)

    # AI courses can answer exams on their own, but the endpoint is optional:
    # never let it break a regular sync.
    try:
        courses.extend(extract_ai_courses(fucker.getZhidaoAiList()))
    except Exception as exc:
        logger.exception(exc)

    if errors and not courses:
        raise RuntimeError("；".join(errors))

    unique = {}
    for course in courses:
        unique[course["id"]] = course
    return list(unique.values())


def load_execution_catalog() -> dict[str, dict[str, str]]:
    """Read execution.json into an ordered id -> course mapping."""
    try:
        payload = json.loads(EXECUTION_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(payload, list):
        return {}

    catalog: dict[str, dict[str, str]] = {}
    for item in payload:
        if not isinstance(item, dict) or item.get("id") is None:
            continue
        course_id = str(item["id"]).strip()
        if not course_id:
            continue
        catalog[course_id] = {
            "name": str(item.get("name") or "未命名课程"),
            "id": course_id,
            "type": str(item.get("type") or ""),
            "classId": str(item.get("classId") or ""),
        }
    return catalog


def resolve_courses(fucker: Fucker, requested: list[str]) -> list[dict[str, str]]:
    """Decide which courses to run and attach their type metadata."""
    catalog = load_execution_catalog()
    if requested:
        resolved = []
        for course_id in requested:
            entry = catalog.get(course_id)
            resolved.append(
                dict(entry)
                if entry
                else {"name": course_id, "id": course_id, "type": "", "classId": ""}
            )
        return resolved
    if catalog:
        return [dict(entry) for entry in catalog.values()]
    return fetch_courses(fucker)


def run_one_course(fucker: Fucker, config: dict, course: dict[str, str]) -> None:
    """Run a single course, answering exams when it is an AI course."""
    course_id = str(course.get("id") or "").strip()
    if not course_id:
        raise RuntimeError("课程 ID 为空")
    if course.get("type") == "ai":
        class_id = str(course.get("classId") or "").strip()
        if not class_id:
            raise RuntimeError("AI 课程缺少班级 ID，请重新同步课程清单")
        ai_config = config.get("ai") or {}
        if not ai_config.get("enabled"):
            raise RuntimeError("AI 答题未启用：请在 config.json 中把 ai.enabled 设为 true")
        fucker.fuckAiCourse(int(course_id), int(class_id), aiConfig=ai_config)
        return
    fucker.fuckCourse(course_id)


def run_sync(config: dict, options: dict | None = None) -> None:
    """同步课程；options.force_login=True 时忽略已保存的登录态，强制重新扫码"""
    options = options or {}
    force_login = bool(options.get("force_login"))
    courses = None
    if force_login:
        report("waiting_login", "切换账号：正在生成登录二维码")
    elif COOKIES_PATH.is_file():
        try:
            with COOKIES_PATH.open("r", encoding="utf-8") as stream:
                saved_cookies = json.load(stream)
            fucker = build_fucker(config, {})
            fucker.cookies = saved_cookies
            report("syncing", "正在使用已保存的登录信息同步课程")
            courses = fetch_courses(fucker)
        except Exception as exc:
            logger.exception(exc)

    if courses is None:
        fucker = build_fucker(config, {})
        report("waiting_login", "正在获取登录二维码")
        fucker.login(use_qr=True, qr_callback=write_qr)
        save_cookies(fucker)
        report("syncing", "登录成功，正在同步课程")
        courses = fetch_courses(fucker)

    write_json_atomic(EXECUTION_PATH, courses)
    QR_PATH.unlink(missing_ok=True)
    report("done", f"同步完成，共找到 {len(courses)} 门课程", progress=100, total=len(courses))


def run_courses(config: dict, options: dict) -> None:
    if not COOKIES_PATH.is_file():
        raise RuntimeError("尚未登录，请先扫码登录并同步课程")
    with COOKIES_PATH.open("r", encoding="utf-8") as stream:
        saved_cookies = json.load(stream)

    requested = [str(item).strip() for item in options.get("course_ids", []) if str(item).strip()]
    bridge = ProgressBridge(total_courses=max(len(requested), 1))
    fucker = build_fucker(config, options, bridge)
    fucker.cookies = saved_cookies

    courses = resolve_courses(fucker, requested)
    if not courses:
        raise RuntimeError("没有找到可运行的课程，请先同步课程")

    total = len(courses)
    bridge.total_courses = total
    completed = 0
    failures = []
    for index, course in enumerate(courses, start=1):
        label = course.get("name") or course["id"]
        bridge.course_start(index, f"正在处理第 {index}/{total} 门 · {label}")
        try:
            run_one_course(fucker, config, course)
            completed += 1
        except CaptchaException as exc:
            # 验证码是账号级风控，继续跑只会一路失败，直接停在这
            location = str(exc) or label
            logger.warning(f"captcha required at {location}")
            report("captcha", f"需要验证码：{location}", progress=int((index - 1) / total * 100), total=total)
            return
        except Exception as exc:
            logger.exception(exc)
            failures.append(f"{course['id']}: {exc}")
        bridge.course_done(index, f"已完成 {completed}/{total} 门课程")

    if failures:
        raise RuntimeError(
            f"任务结束，成功 {completed} 门，失败 {len(failures)} 门。{failures[0]}"
        )
    report("done", f"全部完成，共处理 {completed} 门课程", progress=100, total=total)


def run_ai(config: dict, options: dict) -> None:
    """Run one AI course: watch its resources and answer every exam."""
    if not COOKIES_PATH.is_file():
        raise RuntimeError("尚未登录，请先扫码登录并同步课程")

    ai_config = config.get("ai") or {}
    if not ai_config.get("enabled"):
        raise RuntimeError("AI 答题未启用：请在 config.json 中把 ai.enabled 设为 true")

    try:
        course_id = int(str(options.get("course_id")).strip())
        class_id = int(str(options.get("class_id")).strip())
    except (TypeError, ValueError) as exc:
        raise RuntimeError("AI 课程 ID 与班级 ID 必须是数字") from exc

    with COOKIES_PATH.open("r", encoding="utf-8") as stream:
        saved_cookies = json.load(stream)

    bridge = ProgressBridge(total_courses=1)
    fucker = build_fucker(config, options, bridge)
    fucker.cookies = saved_cookies

    no_exam = bool(options.get("no_exam"))
    bridge.course_start(1, f"正在读取 AI 课程 {course_id}（班级 {class_id}）")
    fucker.fuckAiCourse(course_id, class_id, aiConfig=ai_config, no_exam=no_exam)
    bridge.course_done(1, "AI 课程资源已刷完，考试已跳过" if no_exam else "AI 课程与考试已全部完成")


def run_homework(config: dict, options: dict) -> None:
    """作业答题：真实浏览器打开答题页，人工过验证码，程序作答"""
    if not COOKIES_PATH.is_file():
        raise RuntimeError("尚未登录，请先扫码登录并同步课程")

    from homework_browser import collect_pending, make_key, run_flow

    report("fetching", "正在扫描作业清单")
    _, _, pending = collect_pending(COOKIES_PATH)

    keys = [str(key).strip() for key in (options.get("keys") or []) if str(key).strip()]
    if keys:
        wanted = set(keys)
        pending = [item for item in pending if make_key(item) in wanted]
    if not pending:
        raise RuntimeError("没有找到可做的作业（可能已完成或已提交）")

    submit = bool(options.get("submit"))
    wait_seconds = float(options.get("wait") or 300)
    total = len(pending)

    # 打包成窗口程序后 stdout 是空的，把过程写进文件方便排查
    log_path = RUNTIME_DIR / "homework.log"
    try:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        log_path.write_text("", encoding="utf-8")
    except OSError:
        pass

    def on_progress(message: str, current=None, total_hint=None) -> None:
        progress = 0
        if isinstance(current, int) and isinstance(total_hint, int) and total_hint > 0:
            progress = int(current / total_hint * 100)
        logger.info(message)
        report("running", message, progress=progress, total=total)

    report("running", f"准备处理 {total} 份作业：浏览器即将弹出，请按提示完成验证码", total=total)
    import contextlib

    with log_path.open("a", encoding="utf-8") as log_stream, contextlib.redirect_stdout(log_stream):
        results = run_flow(
            pending,
            submit=submit,
            wait_seconds=wait_seconds,
            on_progress=on_progress,
            cookies_path=COOKIES_PATH,
        )

    filled = sum(int(result.get("filled") or 0) for result in results)
    failures = [result for result in results if not result.get("complete")]
    completed = sum(bool(result.get("complete")) for result in results)
    if len(results) != total or failures:
        reason = failures[0].get("status", "填写失败") if failures else "部分作业未返回结果"
        report("failed", f"作业未全部完成：{completed}/{total} 份，共填 {filled} 题。{reason}",
               progress=min(99, int(completed / total * 100)), total=total, error=reason)
    elif any(result.get("requires_manual") for result in results):
        report("manual", f"已填完 {total} 份作业，共 {filled} 题；请在保留的浏览器检查并确认提交", progress=100, total=total)
    elif all(result.get("submitted") for result in results):
        report("done", f"作业已提交：{total} 份，共填 {filled} 题", progress=100, total=total)
    else:
        report("failed", "无法确认作业提交结果，请检查浏览器", total=total, error="提交结果缺失")


def main() -> None:
    parser = argparse.ArgumentParser(description="ZHS background worker")
    parser.add_argument("--request", required=True)
    args = parser.parse_args()

    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    QR_PATH.unlink(missing_ok=True)
    try:
        request = json.loads(Path(args.request).read_text(encoding="utf-8"))
        action = request.get("action")
        options = request.get("options") or {}
        config = load_config()
        logger.setLevel(config.get("logLevel") or "INFO")
        report("starting", "正在启动任务")

        if action == "sync":
            run_sync(config, options)
        elif action == "run":
            run_courses(config, options)
        elif action == "ai":
            run_ai(config, options)
        elif action == "homework":
            run_homework(config, options)
        else:
            raise ValueError(f"未知操作: {action}")
    except Exception as exc:
        logger.exception(exc)
        QR_PATH.unlink(missing_ok=True)
        report("failed", str(exc), error=str(exc))
        time.sleep(0.1)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
