#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""知到作业（章节 / 单元测试 / 见面课测验）自动答题

和视频弹题、AI 课程考试都不是一回事：
    弹题 = 播放视频时弹出的小题（fucker.fuckZhidaoVideo 里处理）
    AI 课程考试 = kg-ai-run 那一套（fucker.ExamCtx）
    作业 = studentexam-api 的 getStudentHomework / doHomework 那一套（本文件）

接口流程
    CAS SSO  →  getStudentHomework（列表）  →  doHomework（取题）
             →  saveStudentAnswer（逐题保存）  →  submit（提交）
    AES-128-CBC + EXAM_KEY，无 dateFormate，校验 status == "200"

用法
    python homework.py --list                       # 只读：列出所有课程的作业
    python homework.py --run --limit 1              # 只做第一份
    python homework.py --run --only 绪论单元测试     # 按名称筛选
    python homework.py --run --dry-run              # 取题 + 生成答案，但不保存不提交

答题引擎
    需要一个 OpenAI 兼容接口。优先使用显式参数、本机配置，再读环境变量：
        DEEPSEEK_API_KEY   -> https://api.deepseek.com  / deepseek-chat
        OPENAI_API_KEY     -> https://api.openai.com    / gpt-4o-mini
        也可用 HOMEWORK_LLM_BASE / HOMEWORK_LLM_MODEL / HOMEWORK_LLM_KEY 覆盖
    没有可用 Key 时拒绝运行（不会用随机答案糊弄）。
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import time
from html import unescape

import requests

from app_config import env_var
from fucker import Fucker
from logger import logger
from zd_utils import Cipher, EXAM_KEY

HOMEWORK_HOST = "https://studentexam-api.zhihuishu.com"
HOMEWORK_BASE = f"{HOMEWORK_HOST}/studentExam/gateway/t/v1"
SSO_URL = f"{HOMEWORK_BASE}/student/getStudentHomework"

Q_SINGLE, Q_MULTI, Q_FILL, Q_JUDGE = 1, 2, 3, 14
TYPE_NAMES = {Q_SINGLE: "单选", Q_MULTI: "多选", Q_FILL: "填空", Q_JUDGE: "判断"}
LETTERS = "ABCDEFGHIJ"
PLACEHOLDER_KEYS = {"", "sk-", "sk-xxx", "your-api-key", "changeme"}
CAPTCHA_HINTS = ("滑块", "验证码", "验证未通过", "人机验证")


class SliderRequired(RuntimeError):
    """作答时触发了滑块验证：必须人工在浏览器里通过一次，否则所有保存都会被拒"""





# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def html_to_text(raw: str) -> str:
    """把题目 / 选项里的 HTML 压成纯文本"""
    if not raw:
        return ""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.S | re.I)
    text = re.sub(r"<img[^>]*>", " [图片] ", text, flags=re.I)
    text = re.sub(r"<br\s*/?>|</p>|</div>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = unescape(text).replace("\xa0", " ")
    return re.sub(r"[ \t]+", " ", text).strip()


def pick(mapping: dict, *keys, default=None):
    """按顺序取第一个非空字段，兼容接口字段名漂移"""
    for key in keys:
        if isinstance(mapping, dict) and mapping.get(key) not in (None, ""):
            return mapping[key]
    return default


# --------------------------------------------------------------------------- #
# 答题引擎（OpenAI 兼容）
# --------------------------------------------------------------------------- #
def chat_endpoint(base_url: str) -> str:
    """把用户填写的地址补全为 .../chat/completions

    兼容两种写法：只填域名（自动补 /v1）或已经带了版本号/完整路径（原样使用）。
    """
    base = (base_url or "https://api.deepseek.com").strip().rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    tail = base.rsplit("/", 1)[-1]
    if re.fullmatch(r"v\d+[a-z0-9.\-]*", tail):
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"


class AnswerEngine:
    """用 LLM 生成作业答案；没有可用 Key 直接抛错，绝不随机糊弄"""

    def __init__(self, base_url: str = "", api_key: str = "", model: str = "") -> None:
        self.base_url, self.api_key, self.model = self._resolve(base_url, api_key, model)
        self.session = requests.Session()
        self.calls = 0

    @staticmethod
    def _resolve(base_url: str, api_key: str, model: str):
        base_url = (base_url or "").strip()
        api_key = (api_key or "").strip()
        model = (model or "").strip()

        # 1) 界面里保存的本机配置（下载后由用户自己填）
        try:
            from app_config import load_config

            section = load_config().get("llm") or {}
        except Exception:
            section = {}
        saved_key = str(section.get("api_key") or "").strip()
        if api_key in PLACEHOLDER_KEYS:
            api_key = ""
        if api_key or saved_key not in PLACEHOLDER_KEYS:
            api_key = api_key or saved_key
            base_url = base_url or str(section.get("base_url") or "").strip()
            model = model or str(section.get("model") or "").strip()

        # 2) 环境变量（便于脚本 / 调试）
        if not api_key:
            api_key = env_var("HOMEWORK_LLM_KEY")
            if api_key in PLACEHOLDER_KEYS:
                api_key = ""
            if api_key:
                base_url = base_url or env_var("HOMEWORK_LLM_BASE")
                model = model or env_var("HOMEWORK_LLM_MODEL")

        # 3) 只剩官方环境变量时按服务商给默认地址
        if not api_key:
            deepseek = env_var("DEEPSEEK_API_KEY")
            openai_key = env_var("OPENAI_API_KEY")
            if deepseek and deepseek not in PLACEHOLDER_KEYS:
                api_key, base_url = deepseek, base_url or env_var("HOMEWORK_LLM_BASE") or "https://api.deepseek.com"
                model = model or env_var("HOMEWORK_LLM_MODEL") or "deepseek-chat"
            elif openai_key and openai_key not in PLACEHOLDER_KEYS:
                api_key, base_url = openai_key, base_url or env_var("HOMEWORK_LLM_BASE") or "https://api.openai.com"
                model = model or env_var("HOMEWORK_LLM_MODEL") or "gpt-4o-mini"

        if not api_key:
            raise RuntimeError(
                "还没有配置答题模型。请在界面「作业答题 → 答题模型」里填写 API Key，"
                "或设置环境变量 DEEPSEEK_API_KEY / OPENAI_API_KEY。"
            )
        return (base_url or "https://api.deepseek.com").rstrip("/"), api_key, model or "deepseek-chat"

    @property
    def endpoint(self) -> str:
        return chat_endpoint(self.base_url)

    def ping(self) -> str:
        """连通性测试：发一句最短的请求，返回模型回复"""
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": "只回复两个字：可用"}],
            "temperature": 0,
            "stream": False,
            "max_tokens": 16,
        }
        resp = self.session.post(
            self.endpoint,
            json=body,
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=30,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        payload = resp.json()
        return (payload["choices"][0]["message"].get("content") or "").strip()

    def _chat(self, prompt: str, max_retry: int = 3) -> str:
        url = self.endpoint
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": "你是严谨的答题助手，只输出 JSON，不要解释。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
            "stream": False,
        }
        last = None
        for attempt in range(max_retry):
            try:
                resp = self.session.post(
                    url,
                    json=body,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    timeout=120,
                )
                if resp.status_code != 200:
                    raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
                payload = resp.json()
                self.calls += 1
                message = payload["choices"][0]["message"]
                return message.get("content") or message.get("reasoning_content") or ""
            except Exception as exc:  # 网络抖动重试
                last = exc
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"LLM 调用失败：{last}")

    def answer(self, course: str, exam: str, question: str, qtype: int,
               options: list[dict], avoid: list[list[str]] = None) -> tuple[object, str]:
        """返回 (可直接提交的答案, 说明)

        avoid: 已确认错误的组合（用选项正文表示，跨次重做时选项顺序会变）
        """
        if qtype not in (1, 2, 3, 14):
            raise ValueError(f"尚不支持题型 {qtype}")
        if "[图片]" in question or any("[图片]" in option["content"] for option in options):
            raise ValueError("题目含图片，当前文本模型无法可靠读取，请手动作答")
        if qtype == Q_FILL:
            prompt = (
                f"课程《{course}》，作业《{exam}》。\n"
                f"下面是填空题：\n{question}\n\n"
                "如果有多个空，按空的顺序给答案。只输出 JSON："
                '{"fills": ["第一个空的答案", "第二个空的答案"]}'
            )
            raw = self._chat(prompt)
            data = self._extract_json(raw)
            fills = data.get("fills")
            if (not isinstance(fills, list) or not fills
                    or any(not isinstance(item, str) or not item.strip() for item in fills)):
                raise ValueError("填空答案必须是非空字符串数组，且不能漏空")
            fills = [item.strip() for item in fills]
            return "/".join(fills), f"填空 {len(fills)} 空"

        if not options or len({str(o['id']) for o in options}) != len(options):
            raise ValueError("选项为空或 ID 重复")
        labels = []
        lines = []
        for index, option in enumerate(options):
            label = LETTERS[index] if index < len(LETTERS) else str(index + 1)
            labels.append(label)
            lines.append(f"{label}. {option['content']}")
        type_name = TYPE_NAMES.get(qtype, "选择题")
        multi = qtype == Q_MULTI
        prompt = (
            f"课程《{course}》，作业《{exam}》。\n"
            f"下面是{type_name}题：\n{question}\n\n" + "\n".join(lines) + "\n\n"
            + ("选出所有正确选项。" if multi else "选出唯一正确答案。")
            + '只输出 JSON：{"answers": ["A"]}' + ("（多选要列出全部字母）" if multi else "")
        )
        avoid = avoid or []
        avoid_sets = [set(combo) for combo in avoid if combo]
        raw = self._chat(prompt)
        data = self._extract_json(raw)
        def parse_labels(data):
            picked = data.get("answers")
            if not isinstance(picked, list) or not picked or not all(isinstance(v, str) for v in picked):
                raise ValueError("选择题答案必须是非空标签数组")
            chosen = [label.strip().upper() for label in picked]
            if (any(label not in labels for label in chosen)
                    or len(chosen) != len(set(chosen)) or (not multi and len(chosen) != 1)):
                raise ValueError("模型返回未知、重复或数量不符的选项")
            return chosen

        chosen = parse_labels(data)

        # 多选题：命中「已确认错误」的组合时，把错误组合写进提示词再问一次
        def contents_of(labels_):
            return [options[labels.index(label)]["content"] for label in labels_ if label in labels]

        if multi and avoid_sets and set(contents_of(chosen)) in avoid_sets:
            retry_prompt = prompt + (
                "\n\n注意：以下选项组合已经作答过并被判定为错误，必须给出不同的组合：\n"
                + "\n".join("- " + "；".join(combo) for combo in avoid)
                + "\n请重新判断，只输出 JSON。"
            )
            try:
                data = self._extract_json(self._chat(retry_prompt))
                retry_labels = parse_labels(data)
                if retry_labels and set(contents_of(retry_labels)) not in avoid_sets:
                    chosen = retry_labels
                else:
                    raise ValueError("模型仍返回已确认错误的组合，停止作答")
            except Exception as exc:
                raise ValueError(f"未能生成新的有效多选答案：{exc}") from exc

        ids = [options[labels.index(label)]["id"] for label in chosen]
        text = ",".join(str(i) for i in ids) if multi else ids[0]
        return text, "+".join(chosen)

    @staticmethod
    def _extract_json(raw: str) -> dict:
        match = re.search(r"\{[\s\S]*\}", raw or "")
        if not match:
            raise ValueError(f"模型没有返回 JSON：{raw[:120]}")
        data = json.loads(match.group(0))
        if not isinstance(data, dict):
            raise ValueError("模型 JSON 必须是对象")
        return data


# --------------------------------------------------------------------------- #
# 作业接口
# --------------------------------------------------------------------------- #
class HomeworkClient:
    """studentexam-api 的作业接口封装（复用 Fucker 的 requests.Session）"""

    def __init__(self, fucker: Fucker) -> None:
        self.fucker = fucker
        self.session = fucker.session
        self._sso_done = False

    # -- 底层 ------------------------------------------------------------- //
    def _query(self, path: str, data: dict, timeout: int = 20) -> dict:
        cipher = Cipher(EXAM_KEY)
        form = {"secretStr": cipher.encrypt(json.dumps(data))}
        resp = self.session.post(f"{HOMEWORK_BASE}/{path}", data=form, timeout=timeout)
        try:
            payload = resp.json()
        except ValueError as exc:
            raise RuntimeError(f"作业接口返回非 JSON（HTTP {resp.status_code}）：{resp.text[:160]}") from exc
        status = str(payload.get("status"))
        if status != "200":
            message = str(payload.get("msg") or payload.get("message") or "")
            if any(hint in message for hint in CAPTCHA_HINTS):
                raise SliderRequired(message or "需要滑块验证")
            raise RuntimeError(f"作业接口失败 status={status} msg={message}")
        return payload

    def sso(self, force: bool = False) -> None:
        """通过 CAS 拿到 studentexam-api 的会话（没有这一步会一直 401）"""
        if self._sso_done and not force:
            return
        resp = self.session.get(
            f"https://passport.zhihuishu.com/cas/login?service={SSO_URL}",
            allow_redirects=False,
            timeout=15,
        )
        location = resp.headers.get("location", "")
        if "ticket=" not in location:
            raise RuntimeError("CAS SSO 失败：登录态可能已过期，请重新扫码登录")
        self.session.get(location, allow_redirects=True, timeout=15)
        self._sso_done = True
        logger.info("studentexam-api SSO 成功")

    # -- 业务 ------------------------------------------------------------- //
    def list_homework(self, recruit_id: str, course_id: str, flag: int = 1) -> list[dict]:
        self.sso()
        payload = self._query(
            "student/getStudentHomework",
            {"recruitId": recruit_id, "courseId": course_id, "flag": flag, "pageSize": 100, "pageNum": 0},
        )
        rt = payload.get("rt") or {}
        return rt.get("studentHomeworkList") or []

    def open_paper(self, item: dict, recruit_id: str, school_id: str, course_id: str) -> list[dict]:
        """doHomework：开始做作业，返回扁平化后的题目列表"""
        payload = self._query(
            "student/doHomework",
            {
                "recruitId": recruit_id,
                "examId": pick(item, "examId", "exam_id"),
                "studentExamId": pick(item, "id", "studentExamId"),
                "schoolId": school_id,
                "courseId": course_id,
            },
        )
        rt = payload.get("rt") or {}
        exam_base = rt.get("examBase") or rt
        questions = []
        for part in exam_base.get("workExamParts") or []:
            questions.extend(part.get("questionDtos") or [])
        return questions

    def save_answer(self, item: dict, question: dict, answer, recruit_id: str, school_id: str) -> None:
        qtype = pick(question, "questionType", default=0)
        if isinstance(qtype, dict):
            qtype = qtype.get("id", 0)
        answer_item = {
            "examId": pick(item, "examId", "exam_id"),
            "recruitId": recruit_id,
            "stuExamId": pick(item, "id", "studentExamId"),
            "eid": question.get("eid"),
            "schoolId": school_id,
            "deviceId": "",
            "examType": "",
            "fromType": 3,
            "answer": answer,
            "dataIds": "",
            "questionType": int(qtype or 0),
        }
        self._query(
            "answer/saveStudentAnswer",
            {"stuExamAnswer": json.dumps([answer_item]), "recruitId": recruit_id},
        )

    def submit(self, item: dict, recruit_id: str, answer_count: int) -> str:
        payload = self._query(
            "answer/submit",
            {
                "recruitId": recruit_id,
                "examId": pick(item, "examId", "exam_id"),
                "stuExamId": pick(item, "id", "studentExamId"),
                "achieveCount": str(answer_count),
            },
        )
        return str((payload.get("rt") or {}).get("score"))

    def redo(self, item: dict, recruit_id: str, course_id: str) -> None:
        """saveCourseTran：把已提交(state=4)的作业重置成可重做"""
        self._query(
            "student/saveCourseTran",
            {
                "recruitId": recruit_id,
                "examId": pick(item, "examId", "exam_id"),
                "description": "",
                "courseId": course_id,
            },
        )

    def look_paper(self, item: dict, recruit_id: str, school_id: str, course_id: str) -> list[dict]:
        """lookHomework：查看已提交作业（题目带数字型 id，可配合 answer_info 查对错）"""
        payload = self._query(
            "student/lookHomework",
            {
                "recruitId": recruit_id,
                "studentExamId": pick(item, "id", "studentExamId"),
                "examId": pick(item, "examId", "exam_id"),
                "schoolId": school_id,
                "courseId": course_id,
            },
        )
        rt = payload.get("rt") or {}
        exam_base = rt.get("examBase") or rt
        found = []
        for part in exam_base.get("workExamParts") or []:
            found.extend(part.get("questionDtos") or [])
        return found

    def check(self, item: dict, recruit_id: str, school_id: str, course_id: str) -> list[dict]:
        """提交后逐题核对（需作业已提交）"""
        questions = self.look_paper(item, recruit_id, school_id, course_id)
        ids = [q.get("id") for q in questions if q.get("id") is not None]
        info = self.answer_info(item, recruit_id, school_id, course_id, ids) if ids else {}
        rows = []
        for question in questions:
            key = str(question.get("id"))
            detail = info.get(key) or {}
            rows.append({
                "name": html_to_text(question.get("name", "")),
                "eid": question.get("eid"),
                "id": question.get("id"),
                "is_correct": str(detail.get("isCurrent", "")),
                "answer": detail.get("answer", ""),
                "score": detail.get("score", ""),
                "type": (question.get("questionType") or {}).get("id") if isinstance(question.get("questionType"), dict) else question.get("questionType"),
            })
        return rows

    def answer_info(self, item: dict, recruit_id: str, school_id: str,
                    course_id: str, question_ids: list) -> dict:
        """提交后查每题对错（questionIds 来自 lookHomework 的数字 id）"""
        payload = self._query(
            "answer/getStuAnswerInfo",
            {
                "recruitId": recruit_id,
                "stuExamId": pick(item, "id", "studentExamId"),
                "examId": pick(item, "examId", "exam_id"),
                "schoolId": school_id,
                "courseId": course_id,
                "questionIds": ",".join(str(q) for q in question_ids),
            },
        )
        rt = payload.get("rt")
        # 实测返回是以 questionId 为键的字典，这里同时兼容列表形态
        if isinstance(rt, dict):
            return {str(key): value for key, value in rt.items() if isinstance(value, dict)}
        if isinstance(rt, list):
            return {str(entry.get("questionId")): entry for entry in rt if isinstance(entry, dict)}
        return {}


# --------------------------------------------------------------------------- #
# 编排
# --------------------------------------------------------------------------- #
def parse_questions(raw_questions: list[dict]) -> list[dict]:
    """把接口题目整理成统一结构"""
    parsed = []
    for raw in raw_questions:
        qtype = pick(raw, "questionType", default=0)
        if isinstance(qtype, dict):
            qtype = qtype.get("id", 0)
        options = []
        for option in raw.get("questionOptions") or []:
            if option.get("id") is None:
                continue
            options.append({"id": option["id"], "content": html_to_text(option.get("content", ""))})
        parsed.append({
            "eid": raw.get("eid"),
            "id": raw.get("id"),
            "type": int(qtype or 0),
            "name": html_to_text(raw.get("name", "")),
            "options": options,
            "score": str(raw.get("questionScore", "")),
        })
    return parsed


def _answer_detail(answer, options: list[dict], qtype: int) -> str:
    """把答案 id 还原成「A. 选项内容」便于人眼检查"""
    if not options or qtype == Q_FILL:
        return ""
    raw = str(answer)
    parts = raw.split(",") if "," in raw else [raw]
    contents = []
    for part in parts:
        for opt_index, option in enumerate(options):
            if str(option["id"]) == part.strip():
                label = LETTERS[opt_index] if opt_index < len(LETTERS) else str(opt_index + 1)
                contents.append(f"{label}. {option['content'][:40]}")
    return "  => " + " | ".join(contents) if contents else ""


def question_key(question: dict) -> str:
    """Use question identity, including the option set when no ID is available."""
    for field in ("id", "eid"):
        if question.get(field) not in (None, ""):
            return f"{field}:{question[field]}"
    return "body:" + json.dumps([
        question.get("type"), question.get("name"),
        sorted((str(option["id"]), option["content"]) for option in question.get("options", [])),
    ], ensure_ascii=False)


def answer_is_valid(question: dict, answer) -> bool:
    text = str(answer if answer is not None else "").strip()
    if question["type"] == Q_FILL:
        return bool(text) and all(part.strip() for part in text.split("/"))
    if question["type"] not in (1, 2, 14):
        return False
    chosen = [part.strip() for part in text.split(",")]
    allowed = {str(option["id"]) for option in question["options"]}
    return (bool(allowed) and bool(chosen) and all(part in allowed for part in chosen)
            and len(chosen) == len(set(chosen))
            and (question["type"] == Q_MULTI or len(chosen) == 1))


def solve_one(client: HomeworkClient, engine: AnswerEngine, item: dict,
              recruit_id: str, school_id: str, course_id: str,
              course_name: str = "", dry_run: bool = False, rounds: int = 1) -> dict:
    """做一份作业：作答 → 提交 → 逐题核对 → 错题重做（最多 rounds 轮）"""
    name = pick(item, "examName", default="未命名作业")
    total_score = float(pick(item, "totalScore", default=0) or 0)
    back_num = int(float(pick(item, "backNum", default=0) or 0))
    remaining = max(0, back_num - int(float(pick(item, "isMarking", default=0) or 0)))
    initially_submitted = int(pick(item, "state", default=1)) == 4
    max_rounds = 0 if dry_run else max(0, min(rounds, remaining - int(initially_submitted)))

    print(f"\n=== {name} ===")
    print(f"  题量 {pick(item, 'problemNum')} · 总分 {pick(item, 'totalScore')} · 截止 {pick(item, 'endDate')}")
    print(f"  可重做 {back_num} 次 · 本次最多重做 {max_rounds} 轮")

    if initially_submitted and not dry_run:
        if not needs_work(item):
            return {"name": name, "answered": 0, "score": None, "complete": False,
                    "submitted": False, "status": "作业已完成或无剩余重做次数"}
        print("  该作业已提交过，先重置为可重做")
        client.redo(item, recruit_id, course_id)
        item["state"] = 5  # 本地同步，避免重试逻辑再重置一次

    correct_answers: dict[str, object] = {}
    wrong_combos: dict[str, list[list[str]]] = {}
    score = None
    answered = 0
    submitted = False

    for round_index in range(max_rounds + 1):
        if round_index:
            print(f"  —— 第 {round_index + 1} 轮：重做错题 ——")
            try:
                client.redo(item, recruit_id, course_id)
                submitted = False
                answered = 0
            except Exception as exc:
                print(f"  ! 重置作业失败：{exc}")
                break

        read = client.look_paper if dry_run and initially_submitted else client.open_paper
        questions = parse_questions(read(item, recruit_id, school_id, course_id))
        if not questions:
            print("  没有取到题目")
            break
        expected_total = int(pick(item, "problemNum", default=len(questions)) or len(questions))
        if len(questions) != expected_total or len({question_key(q) for q in questions}) != len(questions):
            return {"name": name, "answered": 0, "score": score, "complete": False,
                    "submitted": False, "status": "题量不符或题目重复，未提交"}
        print(f"  取到 {len(questions)} 道题")

        pending_answers: dict[str, dict] = {}
        answered = 0
        for index, question in enumerate(questions, start=1):
            qtype = question["type"]
            key = question_key(question)
            print(f"  [{index}/{len(questions)}] {TYPE_NAMES.get(qtype, f'题型{qtype}')} {question['name']}")
            for opt_index, option in enumerate(question["options"]):
                label = LETTERS[opt_index] if opt_index < len(LETTERS) else str(opt_index + 1)
                print(f"        {label}. {option['content'][:70]}")

            cached = correct_answers.get(key)
            if (cached and cached["options"] == question["options"]
                    and answer_is_valid(question, cached["answer"])):
                answer, note = cached["answer"], "沿用上次正确的答案"
            else:
                try:
                    answer, note = engine.answer(
                        course_name, name, question["name"], qtype, question["options"],
                        avoid=wrong_combos.get(key),
                    )
                except Exception as exc:
                    print(f"      ! 生成失败：{exc}")
                    continue
            if not answer_is_valid(question, answer):
                print("      ! 答案为空、题型不支持或答案与当前选项不符")
                continue
            print(f"      -> {note} = {answer}{_answer_detail(answer, question['options'], qtype)}")

            if dry_run:
                answered += 1
                continue
            try:
                client.save_answer(item, {"eid": question["eid"], "questionType": qtype},
                                   answer, recruit_id, school_id)
                pending_answers[key] = {"answer": answer, "options": question["options"],
                                        "type": qtype, "question": question}
                answered += 1
                time.sleep(random.uniform(1.5, 2.8))
            except SliderRequired:
                raise
            except Exception as exc:
                print(f"      ! 保存失败：{exc}")

        if dry_run:
            print(f"  [dry-run] 生成了 {answered} 个答案，未保存、未提交")
            return {"name": name, "answered": answered, "score": None,
                    "complete": answered == expected_total, "submitted": False, "status": "仅生成答案"}

        if not (answered == len(pending_answers) == len(questions) == expected_total and answered > 0):
            print(f"  只保存了 {answered}/{expected_total} 题，拒绝提交不完整试卷")
            return {"name": name, "answered": answered, "score": score, "complete": False,
                    "submitted": False, "status": "不完整，未提交"}

        try:
            score = client.submit(item, recruit_id, answered)
            submitted = True
        except Exception as exc:
            print(f"  ! 提交失败：{exc}")
            return {"name": name, "answered": answered, "score": score,
                    "complete": True, "submitted": False, "status": "提交失败，请核对平台状态"}

        # 提交后逐题核对，把对错信息沉淀下来
        try:
            rows = client.check(item, recruit_id, school_id, course_id)
        except Exception as exc:
            print(f"  已提交：{answered} 题，得分 {score}/{total_score:.0f}（核对失败：{exc}）")
            return {"name": name, "answered": answered, "score": score,
                    "complete": True, "submitted": True, "status": "已提交，核对失败"}

        correct_count = sum(1 for row in rows if row["is_correct"] == "1")
        unanswered = sum(1 for row in rows if row["is_correct"] not in ("0", "1"))
        wrong_count = len(rows) - correct_count
        for row in rows:
            key = question_key(row)
            record = pending_answers.get(key)
            if record is None:
                # Some API versions expose only eid on one endpoint and id on the other.
                matches = [(k, r) for k, r in pending_answers.items() if any(
                    row.get(field) not in (None, "")
                    and str(row[field]) == str(r["question"].get(field)) for field in ("id", "eid"))]
                if not matches:
                    matches = [(k, r) for k, r in pending_answers.items()
                               if r["question"]["name"] == row["name"]]
                if len(matches) == 1:
                    key, record = matches[0]
            if row["is_correct"] == "1":
                if record:
                    correct_answers[key] = record
            elif row["is_correct"] == "0":
                correct_answers.pop(key, None)
                if record and record["options"]:
                    ids = str(row["answer"]).split(",")
                    combo = [
                        option["content"]
                        for cid in ids
                        for option in record["options"]
                        if str(option["id"]) == cid.strip()
                    ]
                    if combo:
                        wrong_combos.setdefault(key, []).append(combo)
                print(f"      × 错题：{row['name'][:50]}")
        detail = f"对 {correct_count}/{len(rows)}"
        if unanswered:
            detail += f"，未答 {unanswered}"
        print(f"  已提交：{answered} 题，得分 {score}/{total_score:.0f}（{detail}）")

        if len(rows) == expected_total and correct_count == len(rows):
            print("  全部正确")
            break
        if round_index >= max_rounds:
            print("  已达本轮重做上限")
            break

    return {"name": name, "answered": answered, "score": score,
            "complete": submitted, "submitted": submitted,
            "status": "已提交" if submitted else "未提交"}


def needs_work(item: dict) -> bool:
    """未交（state=1/5）直接做；已交（state=4）只有在未满分且还有重做次数时才做"""
    state = int(str(pick(item, "state", default=1)))
    if state != 4:
        return state in (1, 5)
    score = float(pick(item, "score", default=0) or 0)
    total = float(pick(item, "totalScore", default=0) or 0)
    back = int(float(pick(item, "backNum", default=0) or 0))
    marking = int(float(pick(item, "isMarking", default=0) or 0))
    return score < total and (back - marking) > 0


def collect_courses(fucker: Fucker) -> list[dict]:
    """把「课程 + recruitId + schoolId + courseId」凑齐"""
    courses = []
    for entry in fucker.getZhidaoList():
        course_id = str(entry.courseId)
        info = {"name": str(entry.courseName), "courseId": course_id, "secret": str(entry.secret)}
        try:
            detail = fucker.queryCourse(entry.secret)
            info["recruitId"] = str(detail.get("recruitId"))
            info["schoolId"] = str(detail.get("schoolId"))
        except Exception as exc:
            info["error"] = str(exc)
        courses.append(info)
    return courses


def main() -> None:
    parser = argparse.ArgumentParser(description="知到作业自动答题")
    parser.add_argument("--list", action="store_true", help="只列出作业，不做题")
    parser.add_argument("--check", action="store_true", help="核对已提交作业的对错（配合 --only 使用）")
    parser.add_argument("--run", action="store_true", help="开始做题")
    parser.add_argument("--course", help="按课程名筛选（包含匹配）")
    parser.add_argument("--only", help="按作业名筛选（包含匹配）")
    parser.add_argument("--limit", type=int, default=0, help="最多做几份（0=不限）")
    parser.add_argument("--dry-run", action="store_true", help="取题并生成答案，但不保存不提交")
    parser.add_argument("--rounds", type=int, default=1, help="未满分时最多重做几轮（默认 1，受剩余重做次数限制）")
    parser.add_argument("--wait-captcha", type=float, default=5.0,
                        help="遇到滑块验证时等待多少分钟再重试（默认 5，0 表示直接中止）")
    parser.add_argument("--captcha-retries", type=int, default=2,
                        help="滑块验证最多重试几次（默认 2）")
    parser.add_argument("--cookies", default="cookies.json", help="cookies 文件路径")
    parser.add_argument("--model", default="", help="覆盖模型名")
    parser.add_argument("--base-url", default="", help="覆盖 API base")
    parser.add_argument("--api-key", default="", help="覆盖 API key")
    args = parser.parse_args()

    from utils import getRealPath
    import os.path

    cookies_path = getRealPath(args.cookies)
    if not os.path.isfile(cookies_path):
        raise SystemExit(f"找不到 cookies 文件：{cookies_path}，请先扫码登录")

    fucker = Fucker()
    with open(cookies_path, "r", encoding="utf-8") as stream:
        fucker.cookies = json.load(stream)
    fucker._sessionReady()

    client = HomeworkClient(fucker)

    # 1) 扫一遍所有课程的作业
    pending: list[dict] = []
    for course in collect_courses(fucker):
        if course.get("error"):
            print(f"[跳过] {course['name']}：{course['error']}")
            continue
        # --check / --run 需要已完成作业（flag=2）；--run 还要求它未满分且还有重做次数
        if args.check:
            flags = (2,)
        elif args.run and not args.dry_run:
            flags = (1, 2)
        else:
            flags = (1,)
        for flag in flags:
            try:
                items = client.list_homework(course["recruitId"], course["courseId"], flag=flag)
            except Exception as exc:
                print(f"[跳过] {course['name']}：{exc}")
                continue
            for item in items:
                item["_course"] = course
            pending.extend(items)

    if not pending:
        print("没有查到未完成的作业")
        return

    print(f"共查到 {len(pending)} 个未完成作业：")
    for item in pending:
        print(
            f"  - [{pick(item, 'courseName', default=item['_course']['name'])}] "
            f"{pick(item, 'examName')} · {pick(item, 'problemNum')} 题 · "
            f"截止 {pick(item, 'endDate')} · state={pick(item, 'state')} "
            f"· 可重做 {pick(item, 'backNum', default=0)} 次"
        )
    if args.check:
        for item in pending:
            if args.only and args.only not in str(pick(item, "examName", default="")):
                continue
            course = item["_course"]
            try:
                rows = client.check(item, course["recruitId"], course["schoolId"], course["courseId"])
            except Exception as exc:
                print(f"[核对失败] {pick(item, 'examName')}：{exc}")
                continue
            print(f"\n=== 核对 {pick(item, 'examName')} ===")
            for row in rows:
                mark = {"1": "对", "0": "错", "": "未答"}.get(row["is_correct"], "?")
                print(f"  [{mark}] {row['name']}  已答={row['answer']} 得分={row['score']}")
        return

    if args.list or not args.run:
        return

    # 2) 过滤 + 做题
    selected = pending
    if args.course:
        selected = [i for i in selected if args.course in str(pick(i, "courseName", default=""))]
    if args.only:
        selected = [i for i in selected if args.only in str(pick(i, "examName", default=""))]
    selected = [item for item in selected if needs_work(item)]
    if args.limit:
        selected = selected[: args.limit]
    if not selected:
        print("筛选后没有要做的作业")
        return

    engine = AnswerEngine(args.base_url, args.api_key, args.model)
    print(f"\n答题引擎：{engine.model} @ {engine.base_url}")
    print(f"本轮处理 {len(selected)} 份作业" + ("（dry-run）" if args.dry_run else ""))

    results = []
    captcha_used = 0
    for position, item in enumerate(selected, start=1):
        course = item["_course"]
        try:
            results.append(solve_one(
                client, engine, item,
                course["recruitId"], course["schoolId"], course["courseId"],
                course_name=course["name"], dry_run=args.dry_run, rounds=args.rounds,
            ))
        except SliderRequired as exc:
            captcha_used += 1
            if captcha_used > args.captcha_retries or args.wait_captcha <= 0:
                print(f"\n!! 仍被滑块验证拦截（{exc}），已中止且未提交空卷")
                print("   处理办法：用浏览器登录智慧树，打开任意一份作业通过一次滑块，隔一段时间再重跑；")
                print("   或加 --wait-captcha 10 --captcha-retries 3 让程序自己等风控冷却。")
                break
            print(f"\n!! 触发滑块验证（{exc}）")
            print(f"   本次未提交任何空卷；等待 {args.wait_captcha:.0f} 分钟后重试 "
                  f"({captcha_used}/{args.captcha_retries})，期间请勿反复请求")
            time.sleep(args.wait_captcha * 60)
            try:
                client.sso(force=True)  # 换一个新会话再试
            except Exception:
                pass
            # 同一份作业重新来一遍（已保存的答案会被覆盖）
            try:
                results.append(solve_one(
                    client, engine, item,
                    course["recruitId"], course["schoolId"], course["courseId"],
                    course_name=course["name"], dry_run=args.dry_run, rounds=args.rounds,
                ))
            except SliderRequired as again:
                print(f"   重试后仍被拦截（{again}），停止本批次")
                break
            except Exception as exc2:
                logger.exception(exc2)
                print(f"  ! 重试失败：{exc2}")
            continue
        except Exception as exc:
            logger.exception(exc)
            print(f"  ! 作业处理失败：{exc}")

        # 连续高频请求最容易触发风控，两份作业之间歇一会儿
        if not args.dry_run and position < len(selected):
            time.sleep(random.uniform(15, 30))

    print("\n=== 汇总 ===")
    for result in results:
        print(f"  {result['name']}：答 {result['answered']} 题，得分 {result['score']}")


if __name__ == "__main__":
    main()
