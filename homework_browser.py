#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""半自动作业答题：验证码你点，答题程序做

为什么不走接口：studentexam-api 触发风控后只回 `滑块验证未通过`，既不给图也不给票据，
接口侧无处可解；而网页端会弹**易盾点选**，人在浏览器里点一下就能通过，之后由页面自己
带上验证票据保存。

流程
    1. 用接口拿作业列表 + 题目（这一步不会被拦）
    2. LLM 逐题生成答案（本地算，不碰风控）
    3. 启动一个**可见的** Edge 窗口，注入 cookies，打开答题页
    4. 等你在弹窗里点完验证码（脚本会一直等）
    5. 自动逐题点选、点「下一题」保存
    6. 默认**不自动提交**，你检查完自己点；加 --submit 才自动提交

用法
    python homework_browser.py --list
    python homework_browser.py --run --only "第七章"
    python homework_browser.py --run --only "第七章" --submit
"""
from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests
import websocket

from fucker import Fucker
from homework import (
    AnswerEngine,
    answer_is_valid,
    HomeworkClient,
    Q_FILL,
    TYPE_NAMES,
    needs_work,
    parse_questions,
    pick,
    question_key,
)

EXAM_HOST = "https://onlineexamh5new.zhihuishu.com/stuExamWeb.html#/webExamList/dohomework"
EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
]


def kill_orphan_browsers() -> None:
    """清掉上一次被强杀后残留的 Edge（命令行里带我们临时 profile 前缀的）"""
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | "
             "Where-Object { $_.CommandLine -like '*zhs_edge_*' } | "
             "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"],
            capture_output=True, timeout=20,
        )
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# 极简 CDP 客户端
# --------------------------------------------------------------------------- #
class BrowserGone(RuntimeError):
    """浏览器窗口被关掉 / 进程没了"""


class BrowserTimeout(RuntimeError):
    """命令发出去了，但浏览器迟迟不回话"""


class Cdp:
    # 大部分命令 20 秒足够；Page.navigate 这种给 40 秒
    DEFAULT_TIMEOUT = 20.0

    def __init__(self, ws_url: str, proc=None) -> None:
        self.port = urlsplit(ws_url).port
        self.ws = websocket.create_connection(
            ws_url, timeout=self.DEFAULT_TIMEOUT, suppress_origin=True,
            http_no_proxy=["127.0.0.1", "localhost", "::1"],
        )
        self._id = 0
        self.proc = proc

    def _check_alive(self) -> None:
        # Edge can relaunch itself and exit the original Popen process while the
        # window and CDP socket remain alive. The socket is the source of truth.
        if not self.ws.connected:
            raise BrowserGone("浏览器窗口已经关闭")

    def send(self, method: str, params: dict | None = None, timeout: float | None = None) -> dict:
        self._check_alive()
        self._id += 1
        message_id = self._id
        limit = timeout or self.DEFAULT_TIMEOUT
        try:
            self.ws.send(json.dumps({"id": message_id, "method": method, "params": params or {}}))
        except Exception as exc:
            raise BrowserGone(f"和浏览器的连接断了（{exc.__class__.__name__}）") from exc

        deadline = time.time() + limit
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise BrowserTimeout(f"浏览器 {limit:.0f} 秒内没有响应（{method}）")
            try:
                self.ws.settimeout(remaining)
                raw = self.ws.recv()
            except websocket.WebSocketTimeoutException as exc:
                raise BrowserTimeout(f"浏览器 {limit:.0f} 秒内没有响应（{method}）") from exc
            except Exception as exc:
                raise BrowserGone(f"浏览器连接已断开（{exc.__class__.__name__}）") from exc
            if not raw:
                raise BrowserGone("浏览器连接被对方关闭")

            message = json.loads(raw)
            # 页面弹 alert/confirm 时渲染进程会卡住，CDP 也就永远等不到回包 —— 自动点掉
            if message.get("method") == "Page.javascriptDialogOpening":
                self._accept_dialog()
                continue
            if message.get("id") != message_id:
                continue  # 其它命令的响应 / 事件，跳过
            if "error" in message:
                raise RuntimeError(f"CDP {method} 失败: {message['error']}")
            return message.get("result", {})

    def _accept_dialog(self) -> None:
        self._id += 1
        try:
            self.ws.send(json.dumps({
                "id": self._id,
                "method": "Page.handleJavaScriptDialog",
                "params": {"accept": True},
            }))
        except Exception:
            pass

    def eval(self, expression: str, await_promise: bool = False):
        result = self.send("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": await_promise,
        })
        if "exceptionDetails" in result:
            raise RuntimeError(f"页面脚本出错: {result['exceptionDetails'].get('text')}")
        return result.get("result", {}).get("value")

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass


def launch_edge(profile_dir: Path, headless: bool = False) -> subprocess.Popen:
    for path in EDGE_CANDIDATES:
        if path and Path(path).exists():
            args = [
                path,
                "--remote-debugging-port=0",
                "--remote-debugging-address=127.0.0.1",
                f"--user-data-dir={profile_dir.resolve()}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-popup-blocking",
                "--disable-background-mode",
                "--window-size=1440,1300",
            ]
            if headless:
                args += ["--headless=new", "--disable-gpu"]
            args.append("about:blank")
            return subprocess.Popen(args)
    raise RuntimeError("没找到 Edge，请确认已安装 Microsoft Edge")


def debug_endpoint(profile_dir: Path) -> tuple[int, str]:
    """Read the endpoint belonging to this launch, never a previous review window."""
    lines = (profile_dir / "DevToolsActivePort").read_text(encoding="utf-8").splitlines()
    port = int(lines[0])
    if not 0 < port < 65536 or not lines[1].startswith("/devtools/browser/"):
        raise ValueError("Invalid Edge debugging endpoint")
    return port, f"ws://127.0.0.1:{port}{lines[1]}"


def wait_for_target(profile_dir: Path, timeout: float = 30, proc=None) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            port, _ = debug_endpoint(profile_dir)
            for page in list_targets(port):
                if page.get("webSocketDebuggerUrl"):
                    return page
        except (OSError, ValueError, IndexError):
            pass
        time.sleep(0.5)
    code = proc.poll() if proc is not None else None
    detail = f"；启动进程退出码 {code}" if code is not None else ""
    raise RuntimeError(f"Edge 在 {timeout:g} 秒内未提供可用调试页面{detail}。请检查 Edge 是否被系统策略阻止远程调试。")


def stop_edge(proc, profile_dir: Path, cdp=None) -> None:
    """Close our actual browser even when its launcher process has already exited."""
    connection = cdp
    try:
        if connection is None:
            _, endpoint = debug_endpoint(profile_dir)
            connection = Cdp(endpoint)
        connection.send("Browser.close", timeout=5)
    except Exception:
        pass
    finally:
        if connection is not None:
            connection.close()
    if proc is not None and proc.poll() is None:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
    for _ in range(4):
        shutil.rmtree(profile_dir, ignore_errors=True)
        if not profile_dir.exists():
            break
        time.sleep(0.6)


def inject_cookies(cdp: Cdp, cookies_path: Path) -> int:
    raw = json.loads(cookies_path.read_text(encoding="utf-8"))
    cookies = []
    for item in raw:
        domain = str(item.get("domain") or "")
        if "zhihuishu" not in domain:
            continue
        cookies.append({
            "name": item["name"],
            "value": item["value"],
            "domain": domain if domain.startswith(".") else "." + domain.lstrip("."),
            "path": item.get("path") or "/",
            "secure": bool(item.get("secure")),
            "httpOnly": False,
            "expires": int(item["expires"]) if item.get("expires") else -1,
        })
    cdp.send("Network.setCookies", {"cookies": cookies})
    return len(cookies)


# --------------------------------------------------------------------------- #
# 页面操作
# --------------------------------------------------------------------------- #
CAPTCHA_STATE_JS = """(() => {
  const el = document.querySelector('.yidun_popup');
  if (!el) return 'none';
  const style = getComputedStyle(el);
  return (style.display === 'none' || style.visibility === 'hidden') ? 'hidden' : 'visible';
})()"""

# 当前题目的选项 id：radio 本身 display:none，但它的父 div 可见，用它判断"现在停在第几题"
CURRENT_OPTIONS_JS = """(() => {
  const boxes = [...document.querySelectorAll('input[type=radio], input[type=checkbox]')];
  return JSON.stringify(
    boxes.filter(b => {
      const node = b.parentElement;
      return node && (node.offsetWidth || node.offsetHeight);
    }).map(b => b.value)
  );
})()"""


VISIBLE_CHECKED_JS = """(() => {
  const boxes = [...document.querySelectorAll('input[type=radio], input[type=checkbox]')];
  const visible = boxes.filter(b => {
    const node = b.parentElement;
    return node && (node.offsetWidth || node.offsetHeight);
  });
  return JSON.stringify(visible.filter(b => b.checked).map(b => b.value));
})()"""


def current_option_ids(cdp: Cdp) -> set:
    try:
        return set(json.loads(cdp.eval(CURRENT_OPTIONS_JS) or "[]"))
    except (BrowserGone, BrowserTimeout):
        raise
    except Exception:
        return set()


def wait_for_question(cdp: Cdp, expected: set, timeout: float = 10.0) -> bool:
    """等页面真正切到目标题目（选项集合与接口一致），避免点「下一题」后抢跑"""
    if not expected:
        return False
    deadline = time.time() + timeout
    while time.time() < deadline:
        if current_option_ids(cdp) == expected:
            return True
        time.sleep(0.4)
    return False


def wait_for_captcha(cdp: Cdp, timeout: float) -> bool:
    """等人工过验证码；返回是否已通过"""
    print("\n>>> 浏览器已打开答题页。")
    print(">>> 如果弹出「为保障您的学习安全，请进行验证」，请在窗口里点选完成；脚本会自动继续。")
    print(f">>> 最长等待 {timeout:.0f} 秒...")
    start = time.time()
    last_notice = 0
    while time.time() - start < timeout:
        try:
            state = cdp.eval(CAPTCHA_STATE_JS)
        except (BrowserGone, BrowserTimeout):
            raise
        except Exception:
            time.sleep(1)
            continue
        if state in ("none", "hidden"):
            print(">>> 验证已通过，开始答题")
            return True
        elapsed = time.time() - start
        if int(elapsed) - last_notice >= 15:
            last_notice = int(elapsed)
            print(f"    ...仍在等待验证码（已等 {int(elapsed)} 秒）")
        time.sleep(1.5)
    print(">>> 等待超时")
    return False


CENTER_URL = "https://onlineweb.zhihuishu.com/onlinestuh5"


def list_targets(port: int) -> list[dict]:
    try:
        with requests.Session() as session:
            session.trust_env = False  # Local CDP must never go through a proxy.
            response = session.get(f"http://127.0.0.1:{port}/json/list", timeout=2)
            response.raise_for_status()
            return [t for t in response.json() if t.get("type") == "page"]
    except (requests.RequestException, ValueError):
        return []


def wait_new_target(known: set, keyword: str, port: int, timeout: float = 25) -> dict | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        for target in list_targets(port):
            if target["id"] not in known and keyword in (target.get("url") or ""):
                return target
        time.sleep(0.5)
    return None


COURSE_MATCH_JS = r"""
  const strip = (t) => [...(t || '')].filter((ch) => ch.charCodeAt(0) > 32 && ch.charCodeAt(0) !== 12288).join('');
  const target = strip(__COURSE_NAME__);
  if (!target) return 'no-card';
  const cards = [...document.querySelectorAll('.hoverList')];
  const nameOf = (c) => strip(c.querySelector('.courseName')?.textContent || c.textContent);
  let matches = [];
  for (const test of [(s) => s === target, (s) => s.startsWith(target), (s) => s.includes(target)]) {
    matches = cards.filter((c) => test(nameOf(c)));
    if (matches.length) break;
  }
  if (matches.length !== 1) return matches.length ? 'ambiguous-card' : 'no-card';
  const card = matches[0];
  const index = cards.indexOf(card);
"""

HOVER_COURSE_JS = "(() => {" + COURSE_MATCH_JS + r"""
  for (const type of ['mouseenter', 'mouseover', 'mousemove']) {
    card.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, view: window }));
  }
  return 'ok';
})()
"""

CLICK_EXAM_JS = "(() => {" + COURSE_MATCH_JS + r"""
  const menus = [...document.querySelectorAll('.course-menu-w')]
    .filter((m) => strip(m.textContent) === '作业考试');
  if (menus.length !== cards.length || !menus[index]) return 'ambiguous-menu';
  const picked = menus[index];
  picked.click();
  return 'ok';
})()
"""

SAMPLE_TEXT_JS = r"""
(() => {
  const strip = (t) => [...(t || '')].filter((ch) => ch.charCodeAt(0) > 32 && ch.charCodeAt(0) !== 12288).join('');
  const seen = new Set();
  const out = [];
  for (const el of document.querySelectorAll('p, span, strong, h3, h4, a, div')) {
    if (el.children.length > 2) continue;
    const t = strip(el.textContent);
    if (!t || t.length > 24 || seen.has(t)) continue;
    seen.add(t);
    out.push(t);
    if (out.length >= 40) break;
  }
  return out.join(' / ');
})()
"""


def open_exam_page(main: Cdp, course_name: str, exam_name: str) -> Cdp | None:
    """真实点击链路：学生中心 → 该课的「作业考试」→ 该作业的「开始答题」

    答题页是 SPA 深链，直接跳转会是空白页（缺上下文），必须点进去。
    """
    print("    打开学生中心…")
    try:
        main.send("Page.navigate", {"url": CENTER_URL}, timeout=45)
    except BrowserTimeout as exc:
        raise RuntimeError(
            "打开学生中心超时：可能是浏览器窗口被关掉，或者页面弹了对话框。"
            "请重试一次。"
        ) from exc
    try:
        main.send("Page.bringToFront")
    except Exception:
        pass
    time.sleep(2)

    before = {t["id"] for t in list_targets(main.port)}
    # 允许 window.open 真正开窗（页面自己会写 location）
    main.eval("(() => { if (!window.__origOpen) { window.__origOpen = window.open.bind(window);"
              " window.open = function(){ return window.__origOpen.apply(window, arguments); }; } return 1; })()")

    clicked = "no-card"
    deadline = time.time() + 45
    while time.time() < deadline:
        try:
            hovered = main.eval(HOVER_COURSE_JS.replace("__COURSE_NAME__", json.dumps(course_name)))
            if hovered == "ok":
                time.sleep(0.9)  # 等悬停菜单渲染出来
                clicked = main.eval(CLICK_EXAM_JS.replace("__COURSE_NAME__", json.dumps(course_name)))
            else:
                clicked = hovered
        except (BrowserGone, BrowserTimeout):
            raise
        except Exception as exc:
            clicked = f"error:{exc}"
        if clicked == "ok":
            break
        time.sleep(1.5)
    print(f"    点击「作业考试」：{clicked}")
    if clicked != "ok":
        # 把页面上的真实文本打出来，方便按新结构改选择器
        try:
            sample = main.eval(SAMPLE_TEXT_JS)
            print(f"    页面上可见的文本样本：{sample}")
        except Exception:
            pass
        return None

    list_target = wait_new_target(before, "onlineexamh5new", main.port)
    if not list_target:
        print("    ! 没等到作业列表页")
        return None

    list_page = Cdp(list_target["webSocketDebuggerUrl"], proc=main.proc)
    list_page.send("Page.enable")
    list_page.send("Runtime.enable")
    try:
        list_page.send("Page.bringToFront")
    except Exception:
        pass

    # 等作业列表真正渲染出来（SPA 需要几秒）
    card_count = 0
    for _ in range(30):
        time.sleep(1)
        try:
            card_count = int(list_page.eval("document.querySelectorAll('.examItemWrap').length") or 0)
        except (BrowserGone, BrowserTimeout):
            raise
        except Exception:
            card_count = 0
        if card_count:
            break
    print(f"    列表页作业数：{card_count}")
    if not card_count:
        try:
            print(f"    列表页标题：{list_page.eval('document.title')}")
        except Exception:
            pass
        return None

    between = {t["id"] for t in list_targets(main.port)}
    # 页面用 window.open() 开新标签，headless 下会被拦；这里换成可用的直通版本
    list_page.eval("(() => { if (!window.__ownOpen) { window.__ownOpen = window.open.bind(window);"
                   " window.open = function(){ return window.__ownOpen.apply(window, arguments); }; } return 1; })()")

    def click_card() -> str:
        return str(list_page.eval(
            "(() => {"
            "  const buttons = ['开始答题', '重做', '再次作答', '继续答题'];"
            "  const all = [...document.querySelectorAll('.examItemWrap')];"
            f"  const cards = all.filter(c => (c.textContent || '').includes({json.dumps(exam_name)}));"
            "  if (!cards.length) return 'no-card|' + all.map(c => (c.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 30)).join(' // ');"
            "  for (const card of cards) {"
            "    const hit = [...card.querySelectorAll('button, a, div, span')]"
            "      .filter(b => buttons.includes((b.textContent || '').replace(/\\s+/g, '')));"
            "    if (hit.length) { hit[hit.length - 1].click(); return 'ok'; }"
            "  }"
            "  return 'no-btn';"
            "})()"
        ))

    started = click_card()
    if started != "ok":
        # 已提交但还能重做的作业在「已提交」标签下，按钮是「重做」
        print(f"    未提交页没找到（{started}），切到「已提交」")
        list_page.eval(
            "(() => { const tab = [...document.querySelectorAll('.el-tabs__item, li, span, div')]"
            ".find(el => (el.textContent || '').trim() === '已提交' && el.children.length === 0);"
            " if (tab) tab.click(); return !!tab; })()"
        )
        time.sleep(2.5)
        between = {t["id"] for t in list_targets(main.port)}
        started = click_card()
    print(f"    点击「开始答题」：{started}")
    if started != "ok":
        return None

    exam_target = wait_new_target(between, "dohomework", main.port)
    if not exam_target:
        print("    ! 没等到答题页，当前标签页：")
        for target in list_targets(main.port):
            print(f"      - {(target.get('url') or '')[:110]}")
        return None

    print(f"    答题页：{exam_target['url'][-60:]}")
    exam_page = Cdp(exam_target["webSocketDebuggerUrl"], proc=main.proc)
    exam_page.send("Page.enable")
    exam_page.send("Runtime.enable")
    try:
        exam_page.send("Page.bringToFront")
    except Exception:
        pass
    time.sleep(6)
    return exam_page


def click_value(cdp: Cdp, option_id: str) -> bool:
    """点选项：input 是隐藏的，真正响应点击的是它的父 div"""
    expr = "(() => {" + f"""
      const value = {json.dumps(str(option_id))};
      const input = [...document.querySelectorAll('input[type=radio], input[type=checkbox]')]
        .find(el => el.value === value && el.parentElement && el.parentElement.getClientRects().length);
      if (!input) return 'no-input';
      if (input.checked) return 'ok';
      let target = input;
      for (let i = 0; i < 3 && target.parentElement; i++) {{
        target = target.parentElement;
        if (target.className && String(target.className).includes('fl')) break;
      }}
      target.click();
      if (!input.checked) {{
        input.click();
      }}
      return input.checked ? 'ok' : 'not-checked';
    """ + "})()"
    return str(cdp.eval(expr)) == "ok"


def click_button(cdp: Cdp, text: str) -> bool:
    expr = "(() => {" + f"""
      const target = [...document.querySelectorAll('button')].find(b => (b.innerText || '').trim() === '{text}' && !b.disabled);
      if (!target) return 'not-found';
      target.click();
      return 'ok';
    """ + "})()"
    return str(cdp.eval(expr)) == "ok"


def click_dialog_confirm(cdp: Cdp) -> bool:
    expr = """(() => {
      const boxes = [...document.querySelectorAll('.el-message-box, .el-dialog, .el-message-box__wrapper')];
      for (const box of boxes) {
        const btn = [...box.querySelectorAll('button')].find(b => /确定|确认|提交/.test(b.innerText || ''));
        if (btn) { btn.click(); return 'ok'; }
      }
      return 'no-dialog';
    })()"""
    return str(cdp.eval(expr)) == "ok"


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def build_paper(client: HomeworkClient, item: dict, course: dict) -> list[dict]:
    read = client.look_paper if int(pick(item, "state", default=1)) == 4 else client.open_paper
    raw = read(item, course["recruitId"], course["schoolId"], course["courseId"])
    return parse_questions(raw)


def wait_for_submission(cdp: Cdp, timeout: float = 10) -> bool:
    """Only report a submission as successful when the page acknowledges it."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cdp.eval("/提交成功|交卷成功|作业已提交|已完成提交/.test(document.body.innerText || '')") is True:
            return True
        time.sleep(0.5)
    return False


def run_one(cdp: Cdp, client: HomeworkClient, engine: AnswerEngine,
            item: dict, course: dict, *, submit: bool = False, wait_seconds: float = 300,
            on_progress=None) -> dict:
    name = pick(item, "examName", default="未命名作业")
    print(f"\n=== {name} ===")

    def result(status, filled=0, *, complete=False, submitted=False, requires_manual=False):
        return {"name": name, "filled": filled, "status": status, "complete": complete,
                "submitted": submitted, "requires_manual": requires_manual,
                "ok": complete and (not submit or submitted)}

    def emit(message: str, current=None, total=None) -> None:
        print(f"  {message}")
        if on_progress:
            try:
                on_progress(message, current, total)
            except Exception:
                pass

    questions = build_paper(client, item, course)
    print(f"  取到 {len(questions)} 道题")
    expected_total = int(pick(item, "problemNum", default=len(questions)) or 0)
    if (not questions or expected_total != len(questions)
            or len({question_key(q) for q in questions}) != len(questions)):
        return result("题量不符、试卷为空或题目重复，未提交")
    if any(q["type"] not in (1, 2, 14) or not q["options"] for q in questions):
        return result("包含填空题或尚不支持的题型，请在智慧树网页手动作答；未自动提交")

    answers: list[tuple[str, object, str]] = []
    for index, question in enumerate(questions, start=1):
        label = TYPE_NAMES.get(question["type"], f"题型{question['type']}")
        try:
            answer, note = engine.answer(course["name"], name, question["name"],
                                         question["type"], question["options"])
        except Exception as exc:
            print(f"  [{index}] 生成失败：{exc}")
            continue
        detail = ""
        if question["options"] and question["type"] != Q_FILL:
            chosen = str(answer).split(",")
            parts = []
            for cid in chosen:
                for option in question["options"]:
                    if str(option["id"]) == cid.strip():
                        parts.append(option["content"][:30])
            detail = "  => " + " | ".join(parts)
        print(f"  [{index}/{len(questions)}] {label} {note} = {answer}{detail}")
        answers.append((str(answer), question, note))

    if len(answers) != len(questions) or any(not answer_is_valid(q, answer) for answer, q, _ in answers):
        return result("答案生成不完整或与选项不符，未打开重做、未提交")

    # 已提交但还有重做次数的作业：先重置，网页端才会在「未提交」页显示「开始答题」
    if int(str(pick(item, "state", default=1))) == 4:
        if not needs_work(item):
            return result("作业已完成或没有剩余重做次数，未重置")
        emit("该作业已提交，先重置为可重做")
        client.redo(item, course["recruitId"], course["courseId"])
        item["state"] = 5
        # Redo may replace question/option IDs or shuffle the paper. Never fill the old paper.
        if build_paper(client, item, course) != questions:
            emit("重做后的试卷已变化，重新读取题目并生成答案")
            return run_one(cdp, client, engine, item, course, submit=submit,
                           wait_seconds=wait_seconds, on_progress=on_progress)

    page = open_exam_page(cdp, course["name"], name)
    if page is None:
        print("  ! 没能打开答题页")
        return result("打不开答题页")

    # 页面没加载出题目时不要误判"验证已通过"
    visible: list = []
    for _ in range(20):
        try:
            visible = json.loads(page.eval(CURRENT_OPTIONS_JS) or "[]")
        except (BrowserGone, BrowserTimeout):
            raise
        except Exception:
            visible = []
        if visible:
            break
        time.sleep(1)
    print(f"  页面可见选项：{len(visible)} 个")

    if not wait_for_captcha(page, wait_seconds):
        return result("验证码未通过，浏览器已保留", requires_manual=True)

    filled = 0
    failed = False
    for index, (answer, question, note) in enumerate(answers):
        expected_ids = {str(option["id"]) for option in question["options"]}
        if not wait_for_question(page, expected_ids, timeout=12):
            visible = sorted(current_option_ids(page))
            emit(f"[{index + 1}/{len(answers)}] 等不到第 {index + 1} 题（页面 {visible} / 预期 {sorted(expected_ids)}）")
            failed = True
            break

        try:
            already = json.loads(page.eval(VISIBLE_CHECKED_JS) or "[]")
        except (BrowserGone, BrowserTimeout):
            raise
        except Exception:
            already = []
        if already:
            if set(already) != {part.strip() for part in str(answer).split(",")}:
                emit(f"[{index + 1}/{len(answers)}] 已选答案与生成答案不同，请在浏览器核对")
                failed = True
                break
            filled += 1
            emit(f"[{index + 1}/{len(answers)}] 已作答，跳过")
            if index < len(answers) - 1:
                if not click_button(page, "下一题"):
                    failed = True
                    break
                time.sleep(random.uniform(0.8, 1.4))
            continue

        ids = [part.strip() for part in str(answer).split(",") if part.strip()]
        if not ids or not all(click_value(page, option_id) for option_id in ids):
            emit(f"[{index + 1}/{len(answers)}] 点选失败，选项 {ids}")
            failed = True
            break
        checked = set(json.loads(page.eval(VISIBLE_CHECKED_JS) or "[]"))
        if checked != set(ids):
            emit("页面选中结果与答案不符，停止填写")
            failed = True
            break
        filled += 1
        emit(f"[{index + 1}/{len(answers)}] 已选 {note}")

        if index < len(answers) - 1:
            if not click_button(page, "下一题"):
                emit(f"[{index + 1}/{len(answers)}] 找不到「下一题」按钮")
                failed = True
                break
            time.sleep(random.uniform(1.0, 1.8))

    total_expected = max(expected_total, len(answers))
    complete = (not failed) and filled > 0 and filled == len(answers) == total_expected
    emit(f"已填 {filled}/{total_expected} 题" + ("" if complete else "（不完整）"))

    if not complete:
        print(f"  ⚠ 只完成了 {filled}/{total_expected} 题，拒绝提交（避免再次出现空卷）")
        return result("不完整，未提交；浏览器已保留供手动补齐", filled, requires_manual=True)

    if submit:
        print("  点击提交作业…")
        if click_button(page, "提交作业"):
            time.sleep(1.5)
            click_dialog_confirm(page)
            time.sleep(2)
            if wait_for_submission(page):
                return result("已提交", filled, complete=True, submitted=True)
            return result("已发起提交，请在保留的浏览器中确认结果", filled,
                          complete=True, requires_manual=True)
        else:
            return result("找不到提交按钮，浏览器已保留", filled, requires_manual=True)
    else:
        print("  ⚠ 未自动提交：请检查页面后自己点「提交作业」，或加 --submit 自动提交")
        return result("已填写未提交，请在保留的浏览器检查并提交", filled,
                      complete=True, requires_manual=True)


def make_key(item: dict) -> str:
    """作业的稳定标识：课程 + examId + stuExamId"""
    return "|".join([
        str(pick(item, "_courseId", default="")),
        str(pick(item, "examId", "exam_id", default="")),
        str(pick(item, "id", "studentExamId", default="")),
    ])


def collect_pending(cookies_path: Path) -> tuple[Fucker, HomeworkClient, list[dict]]:
    """扫描所有课程里"还值得做"的作业（不触发验证码）"""
    fucker = Fucker()
    fucker.cookies = json.loads(cookies_path.read_text(encoding="utf-8"))
    fucker._sessionReady()
    client = HomeworkClient(fucker)

    pending: list[dict] = []
    errors: list[str] = []
    for course in _courses(fucker):
        if course.get("error"):
            continue
        for flag in (1, 2):
            try:
                items = client.list_homework(course["recruitId"], course["courseId"], flag=flag)
            except Exception as exc:
                errors.append(str(exc))
                continue
            for item in items:
                item["_course"] = course
                item["_courseId"] = course["courseId"]
            pending.extend(items)

    todo = list({make_key(item): item for item in pending if needs_work(item)}.values())
    if not todo and errors:
        # 登录态过期是最常见的情况，别让调用方以为"没作业"
        raise RuntimeError(errors[0])
    return fucker, client, todo


def describe(item: dict) -> dict:
    """给前端用的作业摘要"""
    return {
        "key": make_key(item),
        "course": str(pick(item, "courseName", default="")),
        "name": str(pick(item, "examName", default="")),
        "questions": int(pick(item, "problemNum", default=0) or 0),
        "total_score": str(pick(item, "totalScore", default="")),
        "deadline": str(pick(item, "endDate", default="")),
        "state": int(str(pick(item, "state", default=1))),
        "redo_left": max(0, int(pick(item, "backNum", default=0) or 0)
                         - int(pick(item, "isMarking", default=0) or 0)),
        "score": str(pick(item, "score", default="") or ""),
    }


def run_flow(items: list[dict], *, submit: bool = False, wait_seconds: float = 300,
             on_progress=None, headless: bool = False, keep_browser: bool = False,
             cookies_path: Path | None = None, engine_args: dict | None = None) -> list[dict]:
    """跑一批作业：答题页由真实浏览器打开，验证码人工过"""
    def emit(message: str, current=None, total=None) -> None:
        print(message)
        if on_progress:
            try:
                on_progress(message, current, total)
            except Exception:
                pass

    cookies_path = cookies_path or Path("cookies.json")
    _, client, _ = collect_pending(cookies_path)

    engine_options = engine_args or {}
    engine = AnswerEngine(engine_options.get("base_url", ""), engine_options.get("api_key", ""),
                          engine_options.get("model", ""))
    emit(f"答题引擎：{engine.model} @ {engine.base_url}")

    # A previous visible browser may still be waiting for the user's manual submission.
    profile_dir = Path(tempfile.mkdtemp(prefix="zhs_edge_"))
    browser = None
    cdp = None
    results: list[dict] = []
    preserve_browser = keep_browser or (not submit and not headless)
    browser_ready = False
    try:
        emit("正在启动 Edge 并连接答题窗口…")
        browser = launch_edge(profile_dir, headless=headless)
        page = wait_for_target(profile_dir, proc=browser)
        cdp = Cdp(page["webSocketDebuggerUrl"], proc=browser)
        cdp.send("Page.enable")
        cdp.send("Network.enable")
        cdp.send("Runtime.enable")
        emit(f"注入 cookies {inject_cookies(cdp, cookies_path)} 条")
        browser_ready = True

        for index, item in enumerate(items, start=1):
            emit(f"开始 {index}/{len(items)}：{pick(item, 'examName', default='')}", index - 1, len(items))
            result = run_one(
                cdp, client, engine, item, item["_course"],
                submit=submit, wait_seconds=wait_seconds,
                on_progress=on_progress,
            )
            results.append(result)
            preserve_browser = preserve_browser or (not headless and result.get("requires_manual", False))
            filled = int(result.get("filled") or 0)
            status = str(result.get("status") or "")
            if result.get("complete"):
                emit(f"已填写 {index}/{len(items)}：{result.get('name')} · {status}", index, len(items))
            else:
                emit(
                    f"{index}/{len(items)}「{result.get('name')}」未完成（已填 {filled} 题）"
                    + (f"（{status}）" if status else ""),
                    index,
                    len(items),
                )
    except (BrowserGone, BrowserTimeout) as exc:
        raise RuntimeError(
            f"浏览器那边出问题了：{exc}。请关掉弹出的 Edge 窗口后重新开始。"
        ) from exc
    finally:
        if preserve_browser and browser_ready:
            if cdp is not None:
                cdp.close()
            emit("浏览器已保留，请检查并手动提交，完成后自行关闭窗口。")
        else:
            stop_edge(browser, profile_dir, cdp)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="半自动作业答题（验证码人工过）")
    parser.add_argument("--list", action="store_true", help="只列出可做的作业")
    parser.add_argument("--run", action="store_true", help="开始半自动答题")
    parser.add_argument("--course", help="按课程名筛选")
    parser.add_argument("--only", help="按作业名筛选")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--submit", action="store_true", help="填完后自动点「提交作业」")
    parser.add_argument("--wait", type=float, default=300, help="等验证码的秒数，默认 300")
    parser.add_argument("--cookies", default="cookies.json")
    parser.add_argument("--model", default="")
    parser.add_argument("--base-url", default="")
    parser.add_argument("--api-key", default="")
    parser.add_argument("--keep-browser", action="store_true", help="结束后不关闭浏览器")
    parser.add_argument("--headless", action="store_true", help="无窗口自检用（验证码无法人工点选）")
    args = parser.parse_args()

    from utils import getRealPath

    cookies_path = Path(getRealPath(args.cookies))
    if not cookies_path.is_file():
        raise SystemExit(f"找不到 cookies：{cookies_path}")

    _, _, pending = collect_pending(cookies_path)
    if args.course:
        pending = [i for i in pending if args.course in str(pick(i, "courseName", default=""))]
    if args.only:
        pending = [i for i in pending if args.only in str(pick(i, "examName", default=""))]

    if not pending:
        print("没有需要做的作业")
        return

    print(f"可做作业 {len(pending)} 份：")
    for item in pending:
        print(f"  - [{pick(item, 'courseName')}] {pick(item, 'examName')} "
              f"· {pick(item, 'problemNum')} 题 · 截止 {pick(item, 'endDate')}")
    if args.list or not args.run:
        return

    if args.limit:
        pending = pending[: args.limit]

    run_flow(
        pending,
        submit=args.submit,
        wait_seconds=args.wait,
        headless=args.headless,
        keep_browser=args.keep_browser,
        cookies_path=cookies_path,
        engine_args={"base_url": args.base_url, "api_key": args.api_key, "model": args.model},
    )


def _courses(fucker: Fucker) -> list[dict]:
    from homework import collect_courses
    return collect_courses(fucker)


if __name__ == "__main__":
    main()
