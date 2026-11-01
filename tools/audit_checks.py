"""Offline regression checks; no account, network, application imports or real config writes.

Assertions cover the intended behavior after the October 2026 audit fixes.
Definitions are compiled unchanged from the source AST to avoid logger.py's
import-time truncation of the user's logs and optional desktop dependencies.
Run with Python 3.10+; the frontend probes additionally require Node.js.
"""
from __future__ import annotations

import ast
import contextlib
import copy
import http.client
import io
import json
import logging
import math
import os
from pathlib import Path
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types
from html import unescape
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
RESULTS = []


def definitions(filename, names, **values):
    tree = ast.parse((ROOT / filename).read_text(encoding="utf-8-sig"))
    selected = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names:
            selected.append(node)
        elif isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in names for target in node.targets
        ):
            selected.append(node)
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    module = ast.fix_missing_locations(ast.Module(body=[future, *selected], type_ignores=[]))
    namespace = dict(values)
    exec(compile(module, str(ROOT / filename), "exec"), namespace)
    return namespace


def probe(name, fn):
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            evidence = fn()
        RESULTS.append({"name": name, "passed": True, "evidence": evidence})
    except Exception as exc:
        RESULTS.append({"name": name, "passed": False, "error": f"{type(exc).__name__}: {exc}"})


clock = types.SimpleNamespace(sleep=Mock(), time=time.time)
hw = definitions(
    "homework.py",
    {"pick", "html_to_text", "parse_questions", "question_key", "answer_is_valid", "_answer_detail", "solve_one", "AnswerEngine", "SliderRequired", "chat_endpoint", "needs_work"},
    re=re, json=json, unescape=unescape, time=clock, random=random,
    logger=Mock(), TYPE_NAMES={1: "single", 2: "multi", 3: "fill"},
    LETTERS="ABCDEFGHIJ", Q_FILL=3, Q_MULTI=2,
    PLACEHOLDER_KEYS={"", "sk-", "sk-xxx", "your-api-key", "changeme"},
)
browser = definitions(
    "homework_browser.py",
    {"run_one", "run_flow", "wait_for_question", "current_option_ids", "CURRENT_OPTIONS_JS", "VISIBLE_CHECKED_JS", "BrowserGone", "BrowserTimeout", "COURSE_MATCH_JS", "HOVER_COURSE_JS", "CLICK_EXAM_JS"},
    json=json, time=clock, random=random, pick=hw["pick"],
    TYPE_NAMES=hw["TYPE_NAMES"], Q_FILL=3, Path=Path,
    question_key=hw["question_key"], answer_is_valid=hw["answer_is_valid"],
)


def partial_api_submit():
    client = Mock()
    client.open_paper.return_value = [
        {"eid": str(i), "id": i, "questionType": 1, "name": f"question-{i}",
         "questionOptions": [{"id": 101, "content": "A"}, {"id": 102, "content": "B"}]}
        for i in (1, 2)
    ]
    client.check.return_value = []
    engine = Mock()
    engine.answer.side_effect = [(101, "A"), ValueError("model failed")]
    hw["solve_one"](client, engine, {"examName": "test", "problemNum": 2}, "r", "s", "c", rounds=0)
    assert client.submit.call_count == 0
    return {"questions": 2, "saved": client.save_answer.call_count, "submit_calls": 0}


def fill_counted_without_input():
    page = Mock()
    page.eval.return_value = "[]"
    browser.update(
        build_paper=Mock(return_value=[{"name": "fill", "type": 3, "options": []}]),
        open_exam_page=Mock(return_value=page), wait_for_captcha=Mock(return_value=True),
        click_value=Mock(return_value=True), click_button=Mock(return_value=True),
        click_dialog_confirm=Mock(return_value=True),
    )
    engine = Mock()
    engine.answer.return_value = ("textual answer", "fill 1")
    result = browser["run_one"](Mock(), Mock(), engine,
        {"examName": "fill test", "problemNum": 1}, {"name": "course"}, submit=True)
    assert result["filled"] == 0 and not result["complete"] and not result["submitted"]
    assert browser["click_value"].call_count == 0
    assert browser["click_button"].call_count == 0
    assert browser["open_exam_page"].call_count == 0
    return {"result": result, "option_clicks": 0, "submit_clicked": False}


def empty_paper_submit():
    browser["build_paper"].return_value = []
    browser["click_button"].reset_mock()
    result = browser["run_one"](Mock(), Mock(), Mock(), {"examName": "empty"}, {"name": "course"}, submit=True)
    assert result["filled"] == 0 and not result["complete"] and not result["submitted"]
    assert browser["click_button"].call_count == 0
    return result


def worker_false_success():
    with tempfile.TemporaryDirectory(prefix="zhs_audit_") as tmp:
        base = Path(tmp)
        cookies = base / "cookies.json"
        cookies.write_text("[]", encoding="utf-8")
        fake_browser = types.ModuleType("homework_browser")
        fake_browser.collect_pending = Mock(return_value=(None, None, [{"key": "one"}]))
        fake_browser.make_key = lambda item: item["key"]
        fake_browser.run_flow = Mock(return_value=[{"name": "test", "filled": 0, "status": "打不开答题页"}])
        report = Mock()
        worker = definitions("app_worker.py", {"run_homework"}, COOKIES_PATH=cookies, RUNTIME_DIR=base, report=report)
        with patch.dict(sys.modules, {"homework_browser": fake_browser}):
            worker["run_homework"]({}, {"keys": ["one"]})
        last = report.call_args
        assert last.args[0] == "failed" and last.kwargs["progress"] < 100
        return {"status": last.args[0], "progress": last.kwargs["progress"], "message": last.args[1]}


def manual_browser_closed():
    with tempfile.TemporaryDirectory(prefix="zhs_audit_") as tmp:
        profile = Path(tmp) / "profile"
        profile.mkdir()
        proc = Mock()
        browser.update(
            collect_pending=Mock(return_value=(None, Mock(), [])),
            AnswerEngine=Mock(), kill_orphan_browsers=Mock(),
            tempfile=types.SimpleNamespace(mkdtemp=lambda **kwargs: str(profile)),
            launch_edge=Mock(return_value=proc),
            wait_for_target=Mock(return_value={"webSocketDebuggerUrl": "local-mock"}),
            Cdp=Mock(), inject_cookies=Mock(return_value=0), shutil=shutil,
        )
        original = browser["run_one"]
        browser["run_one"] = Mock(return_value={"name": "test", "filled": 1, "status": "已填写未提交", "complete": True, "requires_manual": True})
        try:
            browser["run_flow"]([{"examName": "test", "_course": {}}], submit=False)
        finally:
            browser["run_one"] = original
        assert proc.terminate.call_count == 0
        assert browser["kill_orphan_browsers"].call_count == 0
        return {"submit": False, "browser_terminated": False, "previous_review_browsers_killed": False}


fucker = definitions("fucker.py", {"Fucker", "ExamCtx"}, logger=Mock(), os=os, time=clock,
                     random=lambda: 0, progressBar=Mock(), VIDEO_KEY=b"dummy", EXAM_KEY=b"dummy", math=math, shutil=shutil)


def hike_never_finishes():
    class BoundReached(Exception):
        pass
    calls = []
    def progress(*args, **kwargs):
        calls.append(args[0])
        if len(calls) >= 5:
            raise BoundReached()
    f = types.SimpleNamespace(
        _checkCookies=Mock(), _checkTimeLimit=Mock(),
        getHikeContext=Mock(return_value=types.SimpleNamespace(fucked_time=0)),
        _sessionReady=Mock(), stuViewFile=Mock(return_value=types.SimpleNamespace(dataId="d", totalTime=10)),
        watchVideo=Mock(), speed=10, end_thre=1,
        saveStuStudyRecord=Mock(return_value=10), _notify=Mock(), progressbar_view=False,
    )
    fucker["progressBar"] = progress
    try:
        fucker["Fucker"].fuckHikeVideo(f, "c", "v")
    except BoundReached:
        pass
    assert calls == [10]
    assert f._checkTimeLimit.call_count >= 2
    return {"reported_positions": calls, "time_limit_checks": f._checkTimeLimit.call_count, "returned_normally": True}


def ai_without_console():
    f = types.SimpleNamespace(
        tree_view=False, prefix="|",
        getAiKnowlegePoints=Mock(return_value=types.SimpleNamespace(courseName="AI", cakeThemeList=[])),
        _notify=Mock(),
    )
    with patch.object(os, "get_terminal_size", side_effect=OSError("no console")):
        fucker["Fucker"].fuckAiCourse(f, 1, 2, {}, no_exam=True)
    assert f._notify.call_count >= 1
    return {"tree_view": False, "no_console": True, "completed": True}


def ai_empty_exam_submitted():
    ctx = types.SimpleNamespace(
        examTestId=1, readAnswerCache=Mock(), openExam=Mock(),
        getSheetContent=Mock(return_value=[{"questionId": 1}, {"questionId": 2}]),
        getQuestionContent=Mock(return_value=None), submitExam=Mock(),
        progress_view=False,
    )
    try:
        start_ai_exam(ctx)
    except RuntimeError as exc:
        assert "拒绝提交" in str(exc)
    else:
        raise AssertionError("incomplete exam did not fail")
    assert ctx.submitExam.call_count == 0
    assert ctx.examStopped
    return {"questions": 2, "answers_saved": 0, "submit_calls": 0}


def start_ai_exam(ctx):
    ctx._start_exam = types.MethodType(fucker["ExamCtx"]._start_exam, ctx)
    return fucker["ExamCtx"].startFuck(ctx, [])


def clear_key_resets_endpoint():
    config = {"llm": {"provider": "custom", "base_url": "https://example.test/v4", "model": "test-model", "api_key": "dummy-only"}}
    writer = Mock()
    console = definitions("web_console.py", {"save_llm"}, re=re,
        load_config=lambda: copy.deepcopy(config), write_json_atomic=writer,
        CONFIG_PATH=Path("mock-only"), current_llm=lambda: {})
    console["save_llm"]({"clear_key": True})
    saved = writer.call_args.args[1]["llm"]
    assert saved == {"provider": "custom", "base_url": "https://example.test/v4", "model": "test-model", "api_key": ""}
    return {"saved": saved}


def avoid_combo_wrong_index():
    engine = hw["AnswerEngine"].__new__(hw["AnswerEngine"])
    engine._chat = Mock(side_effect=['{"answers":["B","C"]}', '{"answers":["A","C"]}'])
    answer, _ = engine.answer("course", "exam", "question", 2,
        [{"id": i + 1, "content": text} for i, text in enumerate(["opt-A", "opt-B", "opt-C"])],
        avoid=[["opt-B", "opt-C"]])
    assert answer == "1,3" and engine._chat.call_count == 2
    return {"new_combo_returned": answer, "model_calls": 2}


def repeated_question_text_corrupts_cache():
    client = Mock()
    def question(name, option_id):
        return {"eid": name, "questionType": 1, "name": "same question stem",
                "questionOptions": [{"id": option_id, "content": "correct"}, {"id": option_id + 1, "content": "other"}]}
    client.open_paper.side_effect = [
        [question("q1", 101), question("q2", 201)],
        [question("q1", 101), question("q2", 201)],
    ]
    client.check.side_effect = [
        [{"eid": "q1", "name": "same question stem", "is_correct": "0", "answer": 101}, {"eid": "q2", "name": "same question stem", "is_correct": "1", "answer": 201}],
        [{"eid": "q1", "name": "same question stem", "is_correct": "1", "answer": 102}, {"eid": "q2", "name": "same question stem", "is_correct": "1", "answer": 201}],
    ]
    engine = Mock()
    engine.answer.side_effect = [(101, "A"), (201, "A"), (102, "B")]
    hw["solve_one"](client, engine, {"examName": "redo", "problemNum": 2, "backNum": 1}, "r", "s", "c", rounds=1)
    answers = [call.args[2] for call in client.save_answer.call_args_list]
    assert answers == [101, 201, 102, 201]
    return {"saved_answer_ids": answers, "round2_q1_valid_ids": [101, 102]}


def logger_truncates_old_records():
    with tempfile.TemporaryDirectory(prefix="zhs_audit_") as tmp:
        path = Path(tmp) / "info.log"
        path.write_text("previous run marker", encoding="utf-8")
        namespace = definitions("logger.py", {"MonoLogger"}, logging=logging, os=os, getRealPath=lambda value: value)
        instance = namespace["MonoLogger"](name="offline-audit", level="DEBUG", path=tmp, to_console=False)
        assert path.read_text(encoding="utf-8") == "previous run marker"
        for attr in ("_debug_hdlr", "_info_hdlr", "_warning_hdlr", "_error_hdlr", "_critical_hdlr"):
            getattr(instance, attr).close()
        return {"previous_record_preserved": True}


def browser_closed_swallowed_by_captcha_wait():
    elapsed = [0]
    fake_time = types.SimpleNamespace(time=lambda: elapsed[0], sleep=lambda value: elapsed.__setitem__(0, elapsed[0] + value))
    code = definitions("homework_browser.py", {"wait_for_captcha", "BrowserGone", "CAPTCHA_STATE_JS"}, time=fake_time)
    page = Mock()
    page.eval.side_effect = code["BrowserGone"]("browser closed")
    code["BrowserTimeout"] = type("BrowserTimeout", (RuntimeError,), {})
    try:
        code["wait_for_captcha"](page, 5)
    except code["BrowserGone"]:
        pass
    else:
        raise AssertionError("browser closure was swallowed")
    assert page.eval.call_count == 1 and elapsed[0] == 0
    return {"browser_gone_propagated": True, "wait_seconds": elapsed[0]}


def cookies_logged_unmasked():
    log = Mock()
    isolated = definitions("fucker.py", {"Fucker"}, logger=log,
        VIDEO_KEY=b"dummy", cookiejar_from_dict=lambda value: value,
        json=json, unquote=lambda value: value)
    instance = isolated["Fucker"].__new__(isolated["Fucker"])
    instance.cookies = {"CASLOGC": '{"uuid":"audit-user"}', "SESSION": "dummy-session-only"}
    messages = [call.args[0] for call in log.debug.call_args_list]
    assert not any("dummy-session-only" in message or "audit-user" in message for message in messages)
    return {"dummy_cookie_value_present_in_debug_log": False, "real_credentials_read": False}


def full_api_paper_still_submits():
    raw = [
        {"eid": "q1", "name": "single", "questionType": 1,
         "questionOptions": [{"id": 101, "content": "a"}, {"id": 102, "content": "b"}]},
        {"eid": "q2", "name": "multi", "questionType": 2,
         "questionOptions": [{"id": 201, "content": "a"}, {"id": 202, "content": "b"}]},
        {"eid": "q3", "name": "fill", "questionType": 3, "questionOptions": []},
    ]
    for dry_run in (False, True):
        client = Mock()
        client.open_paper.return_value = raw
        client.check.return_value = []
        engine = Mock()
        engine.answer.side_effect = [(101, "A"), ("201,202", "A+B"), ("answer", "fill")]
        hw["solve_one"](client, engine, {"problemNum": 3}, "r", "s", "c", rounds=0, dry_run=dry_run)
        assert client.submit.call_count == (0 if dry_run else 1)
        assert client.save_answer.call_count == (0 if dry_run else 3)
    return {"single_multi_fill_saved": 3, "submit_calls": 1, "dry_run_writes": 0}


def api_save_failure_blocks_submission():
    client = Mock()
    client.open_paper.return_value = [
        {"eid": f"q{i}", "name": f"q{i}", "questionType": 1,
         "questionOptions": [{"id": i, "content": "answer"}]} for i in (1, 2)
    ]
    client.save_answer.side_effect = [None, RuntimeError("save failed")]
    engine = Mock()
    engine.answer.side_effect = [(1, "A"), (2, "A")]
    result = hw["solve_one"](client, engine, {"problemNum": 2}, "r", "s", "c", rounds=0)
    assert not result["complete"] and client.submit.call_count == 0
    return {"submit_calls": 0, "saved": result["answered"]}


def browser_choice_flow():
    evidence = []
    for submit, checked_ok, confirmed in ((False, True, False), (True, True, True), (True, True, False), (True, False, False)):
        questions = [
            {"id": 1, "name": "single", "type": 1, "options": [{"id": 101, "content": "a"}, {"id": 102, "content": "b"}]},
            {"id": 2, "name": "multi", "type": 2, "options": [{"id": 201, "content": "a"}, {"id": 202, "content": "b"}, {"id": 203, "content": "c"}]},
        ]
        current = [0]
        checked = [set(), set()]
        def evaluate(script):
            if script == browser["CURRENT_OPTIONS_JS"]:
                return json.dumps([str(o["id"]) for o in questions[current[0]]["options"]])
            if script == browser["VISIBLE_CHECKED_JS"]:
                return json.dumps(sorted(checked[current[0]]))
            raise AssertionError("unexpected page evaluation")
        def click_option(page, value):
            if checked_ok:
                checked[current[0]].add(value)
            return True
        buttons = []
        def click(page, text):
            buttons.append(text)
            if text == "下一题":
                current[0] += 1
            return True
        page = types.SimpleNamespace(eval=evaluate)
        engine = Mock()
        engine.answer.side_effect = [(101, "A"), ("201,203", "A+C")]
        with patch.dict(browser, {
            "build_paper": Mock(return_value=questions), "open_exam_page": Mock(return_value=page),
            "wait_for_captcha": Mock(return_value=True), "click_value": click_option,
            "click_button": click, "click_dialog_confirm": Mock(return_value=True),
            "wait_for_submission": Mock(return_value=confirmed),
        }):
            result = browser["run_one"](Mock(), Mock(), engine,
                {"examName": "test", "problemNum": 2}, {"name": "course"}, submit=submit)
        assert result["complete"] == checked_ok
        assert result["submitted"] == (submit and checked_ok and confirmed)
        assert ("提交作业" in buttons) == (submit and checked_ok)
        if not submit or not confirmed:
            assert result["requires_manual"]
        evidence.append({"auto_submit": submit, "readback_ok": checked_ok, "confirmed": confirmed,
                         "filled": result["filled"], "submitted": result["submitted"]})
    return evidence


def ai_save_and_submit_guards():
    class Record(dict):
        __getattr__ = dict.__getitem__
        __setattr__ = dict.__setitem__
    evidence = []
    for save_ok in (True, False):
        rows = [Record(id=i, questionType=1, content=f"q{i}", optionVos=[{"id": i * 100, "content": "a", "isCorrect": 1}],
                       userAnswerVo=[{"isCorrect": 1}]) for i in (1, 2)]
        ctx = types.SimpleNamespace(
            examTestId=1, readAnswerCache=Mock(), openExam=Mock(),
            getSheetContent=Mock(return_value=[{"questionId": 1}, {"questionId": 2}]),
            getQuestionContent=Mock(side_effect=rows + rows),
            getQuestionAnswer=Mock(side_effect=[([100], "test"), ([200], "test")]),
            saveAnswer=Mock(side_effect=[True, save_ok]), submitExam=Mock(),
            fucker=types.SimpleNamespace(_notify=Mock()), setAnswer=Mock(), progress_view=False,
        )
        if save_ok:
            assert start_ai_exam(ctx) == (True, 2, 2)
            assert ctx.fucker._notify.call_args_list[-1].args[1:] == (2, 2)
        else:
            try:
                start_ai_exam(ctx)
            except RuntimeError as exc:
                assert "拒绝提交" in str(exc)
            else:
                raise AssertionError("exam with a failed save was submitted")
        assert ctx.submitExam.call_count == int(save_ok)
        assert ctx.examStopped
        evidence.append({"all_saved": save_ok, "submit_calls": ctx.submitExam.call_count})
    return evidence


def hike_fraction_stall_and_limit():
    def make_f(confirmed):
        return types.SimpleNamespace(
            _checkCookies=Mock(), _checkTimeLimit=Mock(),
            getHikeContext=Mock(return_value=types.SimpleNamespace(fucked_time=0)),
            _sessionReady=Mock(), stuViewFile=Mock(return_value=types.SimpleNamespace(dataId="d", totalTime=100)),
            watchVideo=Mock(), speed=100, end_thre=.915,
            saveStuStudyRecord=Mock(return_value=confirmed), _notify=Mock(), progressbar_view=False,
        )
    with patch.dict(fucker, {"progressBar": Mock()}):
        f = make_f(92)
        fucker["Fucker"].fuckHikeVideo(f, "c", "v")
        assert f.saveStuStudyRecord.call_count == 1 and f.saveStuStudyRecord.call_args.args[2] == 92
        stalled = make_f(0)
        try:
            fucker["Fucker"].fuckHikeVideo(stalled, "c", "v")
        except RuntimeError as exc:
            assert "连续三次" in str(exc)
        else:
            raise AssertionError("stalled progress did not stop")
        assert stalled.saveStuStudyRecord.call_count == 3
        limited = make_f(92)
        limited._checkTimeLimit.side_effect = [None, TimeoutError("limit")]
        try:
            fucker["Fucker"].fuckHikeVideo(limited, "c", "v")
        except TimeoutError:
            pass
        else:
            raise AssertionError("time limit did not stop video")
        assert limited.saveStuStudyRecord.call_count == 0
    return {"fractional_target_rounded_to": 92, "stalled_reports_before_stop": 3, "limit_checked_in_loop": True}


def worker_terminal_states():
    evidence = []
    for expected, result in (
        ("manual", {"complete": True, "requires_manual": True, "submitted": False, "filled": 2}),
        ("done", {"complete": True, "requires_manual": False, "submitted": True, "filled": 2}),
        ("failed", {"complete": False, "status": "save failed", "filled": 1}),
    ):
        with tempfile.TemporaryDirectory(prefix="zhs_audit_") as tmp:
            base = Path(tmp)
            cookies = base / "cookies.json"
            cookies.write_text("[]", encoding="utf-8")
            fake_browser = types.ModuleType("homework_browser")
            fake_browser.collect_pending = Mock(return_value=(None, None, [{"key": "one"}]))
            fake_browser.make_key = lambda item: item["key"]
            fake_browser.run_flow = Mock(return_value=[result])
            report = Mock()
            worker = definitions("app_worker.py", {"run_homework"}, COOKIES_PATH=cookies, RUNTIME_DIR=base, report=report, logger=Mock())
            with patch.dict(sys.modules, {"homework_browser": fake_browser}):
                worker["run_homework"]({}, {"keys": ["one"]})
            assert report.call_args.args[0] == expected
            evidence.append(expected)
    return {"terminal_states_verified": evidence}


def request_logs_exclude_credentials():
    log = Mock()
    isolated = definitions("fucker.py", {"Fucker"}, logger=log,
        VIDEO_KEY=b"dummy", json=json, urlsplit=urlsplit, ObjDict=dict)
    session = types.SimpleNamespace(post=Mock(return_value=types.SimpleNamespace(json=lambda: {"status": 200, "token": "dummy-response-token"})),
                                    headers={}, cookies={"SESSION": "dummy-session"}, proxies={})
    f = types.SimpleNamespace(session=session, proxies={}, headers={"Authorization": "dummy-bearer"})
    isolated["Fucker"]._apiQuery(f, "https://example.test/path?ticket=dummy-ticket", {"password": "dummy-password"})
    messages = "\n".join(call.args[0] for call in log.debug.call_args_list)
    assert "dummy-" not in messages
    return {"request_body_headers_cookie_query_response_values_logged": False}


def actual_http_endpoints():
    import app_config
    import web_console
    with tempfile.TemporaryDirectory(prefix="zhs_http_audit_") as tmp, contextlib.ExitStack() as stack:
        base = Path(tmp)
        paths = {"APP_ROOT": base, "CONFIG_PATH": base / "config.json", "RUNTIME_DIR": base / "runtime",
                 "COOKIES_PATH": base / "cookies.json", "EXECUTION_PATH": base / "execution.json"}
        for module in (app_config, web_console):
            for name, value in paths.items():
                stack.enter_context(patch.object(module, name, value))
        web_assets = base / "web"
        shutil.copytree(ROOT / "web", web_assets)
        (web_assets / "private.txt").write_text("must not be served", encoding="utf-8")
        stack.enter_context(patch.object(web_console, "WEB_ROOT", web_assets))
        stack.enter_context(patch.object(web_console, "env_var", return_value=""))
        stack.enter_context(patch.object(web_console, "HOMEWORK_CACHE", {"at": 1, "items": [{"key": "keep"}]}))
        app_config.ensure_config()
        config = app_config.load_config()
        config["llm"] = {"provider": "custom", "base_url": "https://example.test/v4", "model": "test-model", "api_key": "dummy-audit-key-123456"}
        app_config.write_json_atomic(paths["CONFIG_PATH"], config)
        manager = web_console.ActionManager()
        server = web_console.ConsoleServer(("127.0.0.1", 0), web_console.ConsoleRequestHandler, manager)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def request(method, path, body=None):
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            try:
                conn.request(method, path, body=json.dumps(body) if body is not None else None,
                             headers={"Content-Type": "application/json"})
                response = conn.getresponse()
                payload = response.read()
                return response.status, payload
            finally:
                conn.close()
        try:
            status, payload = request("GET", "/api/settings")
            assert status == 200 and b"dummy-audit-key-123456" not in payload
            assert json.loads(payload)["llm"]["has_key"]
            assert request("POST", "/api/settings", {"clear_key": True})[0] == 200
            saved = app_config.load_config()["llm"]
            assert saved["model"] == "test-model" and saved["api_key"] == ""
            assert request("POST", "/api/settings", {"base_url": "invalid"})[0] == 400
            assert request("GET", "/../config.json")[0] == 403
            assert request("GET", "/%2e%2e/config.json")[0] == 403
            assert request("GET", "/private.txt")[0] == 404
            assert request("GET", "/app.js%0d%0aInjected:value")[0] == 404
            for name in ("index.html", "app.js", "styles.css", "favicon.svg"):
                status, payload = request("GET", "/" + name)
                assert status == 200 and payload == (web_assets / name).read_bytes()
            assert request("HEAD", "/styles.css") == (200, b"")
            manager._process = types.SimpleNamespace(poll=lambda: None)
            assert request("POST", "/api/action/sync", {"force_login": True})[0] == 409
            assert web_console.HOMEWORK_CACHE["items"] == [{"key": "keep"}]
            manager._process = None
            with patch.object(web_console.subprocess, "Popen", side_effect=OSError("mock spawn failure")):
                assert request("POST", "/api/action/sync", {"force_login": True})[0] == 409
            assert web_console.HOMEWORK_CACHE["items"] == [{"key": "keep"}]
            app_config.write_json_atomic(manager.status_path, {"state": "manual", "message": "review"})
            web_console.reset_stale_status()
            assert web_console.read_json(manager.status_path, {})["state"] == "manual"
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
    return {"settings_masked": True, "clear_preserves_model": True, "invalid_url_rejected": True,
            "path_traversal_blocked": True, "fixed_static_allowlist": True,
            "rejected_sync_preserves_cache": True, "manual_status_survives": True}


def tls_runtime_self_check():
    ssl_module = types.ModuleType("ssl")
    ssl_module.CERT_REQUIRED = 2
    ssl_module.PROTOCOL_TLS_CLIENT = 16
    ssl_module.SSLContext = Mock(return_value=types.SimpleNamespace(verify_mode=2))
    ssl_util = types.ModuleType("urllib3.util.ssl_")
    ssl_util.create_urllib3_context = Mock(return_value=types.SimpleNamespace(verify_mode=2))
    requests_module = types.ModuleType("requests")
    namespace = definitions("zhs_app.py", {"runtime_self_check"})
    with tempfile.TemporaryDirectory(prefix="zhs_tls_check_") as tmp:
        base = Path(tmp)
        ca_file = base / "test-ca.pem"
        ca_file.write_text("test-only", encoding="utf-8")
        requests_module.certs = types.SimpleNamespace(where=lambda: str(ca_file))
        modules = {"ssl": ssl_module, "requests": requests_module,
                   "urllib3": types.ModuleType("urllib3"), "urllib3.util": types.ModuleType("urllib3.util"),
                   "urllib3.util.ssl_": ssl_util}
        with patch.dict(sys.modules, modules):
            output = base / "result.json"
            assert namespace["runtime_self_check"](str(output))
            assert json.loads(output.read_text(encoding="utf-8"))["certificate_verification"]
            ssl_module.SSLContext.side_effect = FileNotFoundError("missing TLS resource")
            assert not namespace["runtime_self_check"](str(output))
            assert not json.loads(output.read_text(encoding="utf-8"))["ok"]
    return {"valid_tls_accepted": True, "missing_resource_detected": True, "verification_required": True}


def syntax_controls():
    files = list(ROOT.glob("*.py")) + list((ROOT / "decrypt").glob("*.py")) + list((ROOT / "tools").glob("*.py"))
    for path in files:
        compile(path.read_text(encoding="utf-8-sig"), str(path), "exec")
    assert hw["chat_endpoint"]("https://open.bigmodel.cn/api/paas/v4") == "https://open.bigmodel.cn/api/paas/v4/chat/completions"
    assert "checkbox" in browser["CURRENT_OPTIONS_JS"]
    return {"python_files_compiled_in_memory": len(files), "multi_selector_present": True, "v4_endpoint": "ok"}


FRONTEND_PROBE = r'''
const fs = require('fs');
const vm = require('vm');
const data = JSON.parse(fs.readFileSync(process.argv[1], 'utf8'));
const nodes = new Map();
function node(selector) {
  if (!nodes.has(selector)) nodes.set(selector, {
    value: '', textContent: '', innerHTML: '', disabled: false, hidden: false,
    style: {}, dataset: {}, listeners: {},
    classList: {add(){}, remove(){}, toggle(){}},
    addEventListener(event, fn) {this.listeners[event] = fn},
    querySelector(q) {
      if(q.includes('taskMode') && q.includes(':checked')) return {value:node('selectedMode').checked?'selected':'all'};
      if(q.includes('taskMode') && q.includes('selected')) return node('selectedMode');
      return node(selector+q);
    },
    querySelectorAll() {return []},
  });
  return nodes.get(selector);
}
const context = {
  document: {querySelector: node, querySelectorAll:()=>[], addEventListener(){}, body:node('body')},
  window: {clearTimeout(){},setTimeout(){}}, console,
};
let source = fs.readFileSync(data.app, 'utf8');
source = source.slice(0, source.indexOf('const observer ='));
vm.runInNewContext(source + `
state.courses = [{id:'101', name:'course', label:'test'}];
elements.courseList.listeners.change({target:{matches:()=>true,checked:true,value:'101'}});
const sent = compactAction();
if (sent.course_ids.join() !== '101' || state.selected.size !== 1) throw Error('selection probe failed');
console.log(JSON.stringify({name:'frontend_selection_scope',passed:true,evidence:{selected:[...state.selected],sent:sent.course_ids,summary:elements.selectedSummary.querySelector('span').textContent}}));
setTaskControls(true);
if(!elements.switchAccountButton.disabled) throw Error('switch unexpectedly enabled');
state.action = {active:true};
homeworkState.items = [{key:'keep'}];
elements.switchAccountButton.listeners.click();
if(homeworkState.items.length!==1) throw Error('busy switch cleared homework');
console.log(JSON.stringify({name:'switch_account_blocked_during_task',passed:true,evidence:{sync_disabled:elements.syncButton.disabled,switch_disabled:elements.switchAccountButton.disabled}}));
`, context);
const cards = [
  {textContent:'course one',querySelector:()=>({textContent:'course one'}),dispatchEvent(){}},
  {textContent:'course two extra',querySelector:()=>({textContent:'course two extra'}),dispatchEvent(){}},
];
let picked = -1;
const menus = [0,1].map(i=>({textContent:'作业考试', click(){picked=i}}));
const dom = {document:{querySelectorAll:s=>s==='.hoverList'?cards:menus},window:{},MouseEvent:class{}};
const hovered=vm.runInNewContext(data.hover.replace('__COURSE_NAME__', JSON.stringify('course two')), dom);
const clicked=vm.runInNewContext(data.click.replace('__COURSE_NAME__', JSON.stringify('course two')), dom);
if(hovered!=='ok'||clicked!=='ok'||picked!==1) throw Error('course menu probe failed');
console.log(JSON.stringify({name:'course_prefix_clicks_correct_course',passed:true,evidence:{expected_course_index:1,actual_course_index:picked}}));
picked=-1; menus.pop();
const missing=vm.runInNewContext(data.click.replace('__COURSE_NAME__', JSON.stringify('course two')), dom);
if(missing!=='ambiguous-menu'||picked!==-1) throw Error('missing menu must not fall back');
console.log(JSON.stringify({name:'missing_course_menu_stops',passed:true}));
'''


if __name__ == "__main__":
    for name, fn in [
        ("partial_api_submission_blocked", partial_api_submit),
        ("unsupported_browser_question_blocked", fill_counted_without_input),
        ("empty_browser_paper_blocked", empty_paper_submit),
        ("worker_failure_preserved", worker_false_success),
        ("manual_review_browser_preserved", manual_browser_closed),
        ("hike_exits_at_end", hike_never_finishes),
        ("ai_runs_without_console", ai_without_console),
        ("empty_ai_exam_blocked", ai_empty_exam_submitted),
        ("clear_key_preserves_settings", clear_key_resets_endpoint),
        ("wrong_combo_retried", avoid_combo_wrong_index),
        ("repeated_question_cache_isolated", repeated_question_text_corrupts_cache),
        ("old_logs_preserved", logger_truncates_old_records),
        ("closed_browser_propagates_immediately", browser_closed_swallowed_by_captcha_wait),
        ("cookies_not_logged", cookies_logged_unmasked),
        ("complete_api_paper_and_dry_run", full_api_paper_still_submits),
        ("failed_api_save_blocks_submit", api_save_failure_blocks_submission),
        ("browser_single_multi_readback_and_submission", browser_choice_flow),
        ("ai_complete_exam_and_failed_save", ai_save_and_submit_guards),
        ("hike_fraction_stall_and_time_limit", hike_fraction_stall_and_limit),
        ("worker_manual_done_failed_states", worker_terminal_states),
        ("request_logs_redacted", request_logs_exclude_credentials),
        ("actual_local_http_endpoints", actual_http_endpoints),
        ("tls_runtime_resources", tls_runtime_self_check),
        ("positive_controls", syntax_controls),
    ]:
        probe(name, fn)
    with tempfile.TemporaryDirectory(prefix="zhs_audit_") as tmp:
        payload = Path(tmp) / "frontend.json"
        payload.write_text(json.dumps({"app": str(ROOT / "web/app.js"), "hover": browser["HOVER_COURSE_JS"], "click": browser["CLICK_EXAM_JS"]}), encoding="utf-8")
        run = subprocess.run(["node", "-e", FRONTEND_PROBE, str(payload)], capture_output=True, text=True, encoding="utf-8")
        if run.returncode:
            RESULTS.append({"name": "frontend_probes", "passed": False, "error": run.stderr})
        else:
            RESULTS.extend(json.loads(line) for line in run.stdout.splitlines() if line.strip())
    # JSON remains readable by machines even on Windows terminals using a legacy code page.
    print(json.dumps(RESULTS, ensure_ascii=True, indent=2))
    sys.exit(0 if all(item["passed"] for item in RESULTS) else 1)
