"""Offline answer regressions: fake models/platforms, no credentials or submissions."""
from __future__ import annotations

import contextlib
import io
import json
import re
import sys
import types
import unittest
from unittest.mock import Mock, patch

from audit_checks import definitions, hw, browser, fucker


class AnswerTests(unittest.TestCase):
    def engine(self, response):
        engine = hw["AnswerEngine"].__new__(hw["AnswerEngine"])
        engine._chat = Mock(return_value=json.dumps(response))
        return engine

    def test_malformed_choices_are_not_silently_truncated(self):
        options = [{"id": 10, "content": "a"}, {"id": 20, "content": "b"}]
        for value in ("AB", ["A", "B"], ["Apple"], ["A", "Z"], ["A", "A"], [], [1]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.engine({"answers": value}).answer("c", "e", "q", 1, options)

    def test_multi_unknown_or_duplicate_choice_rejected(self):
        for value in (["A", "Z"], ["A", "A"]):
            with self.assertRaises(ValueError):
                self.engine({"answers": value}).answer("c", "e", "q", 2, [{"id": 1, "content": "a"}])

    def test_labels_after_tenth_and_string_ids(self):
        options = [{"id": f"opt-{i}", "content": str(i)} for i in range(12)]
        answer, _ = self.engine({"answers": ["11"]}).answer("c", "e", "q", 1, options)
        self.assertEqual(answer, "opt-10")

    def test_fills_do_not_drop_missing_slots_or_split_strings(self):
        for fills in ("hello", ["first", ""], ["first", None], [1], []):
            with self.subTest(fills=fills), self.assertRaises(ValueError):
                self.engine({"fills": fills}).answer("c", "e", "q", 3, [])

    def test_image_and_unsupported_question_do_not_call_text_model(self):
        for qtype, name in ((1, "请看 [图片]"), (99, "essay")):
            engine = self.engine({"answers": ["A"]})
            with self.assertRaises(ValueError):
                engine.answer("c", "e", name, qtype, [{"id": 1, "content": "a"}])
            engine._chat.assert_not_called()

    def test_environment_key_not_sent_to_stale_saved_provider(self):
        config = types.ModuleType("app_config")
        config.load_config = lambda: {"llm": {"base_url": "https://old.test", "model": "old", "api_key": ""}}
        env = {"OPENAI_API_KEY": "test-key"}
        with patch.dict(sys.modules, {"app_config": config}), patch.dict(hw, {"env_var": lambda k: env.get(k, "")}):
            base, key, model = hw["AnswerEngine"]._resolve("", "", "")
            self.assertEqual((base, key, model), ("https://api.openai.com", "test-key", "gpt-4o-mini"))
            env["HOMEWORK_LLM_BASE"] = "https://custom.test/v4"
            env["HOMEWORK_LLM_MODEL"] = "custom"
            base, _, model = hw["AnswerEngine"]._resolve("", "", "")
            self.assertEqual((base, model), ("https://custom.test/v4", "custom"))

    def test_placeholder_key_is_unconfigured(self):
        config = types.ModuleType("app_config")
        config.load_config = lambda: {}
        with patch.dict(sys.modules, {"app_config": config}), patch.dict(hw, {"env_var": lambda k: "sk-"}):
            with self.assertRaises(RuntimeError):
                hw["AnswerEngine"]._resolve("", "sk-xxx", "")


class AiExamTests(unittest.TestCase):
    def test_course_retry_is_bounded_and_success_exits(self):
        point = types.SimpleNamespace(knowledgeName="test", knowledgeId=1, studyProgress=100)
        theme = types.SimpleNamespace(themeName="test", knowledgeList=[point])
        for success in (False, True):
            f = types.SimpleNamespace(tree_view=False, prefix="|", progressbar_view=False,
                getAiKnowlegePoints=Mock(return_value=types.SimpleNamespace(courseName="test", cakeThemeList=[theme])),
                _notify=Mock(), _checkTimeLimit=Mock(),
                queryAiExam=Mock(return_value={"paperId": 1, "examTestId": 1, "masteryScore": "50"}))
            factory = Mock()
            factory.return_value.startFuck.return_value = (success, int(success), 1)
            with patch.dict(fucker, {"ExamCtx": factory, "uniform": lambda a, b: 0}):
                if success:
                    fucker["Fucker"].fuckAiCourse(f, 1, 1, {"max_exam_attempts": 2})
                else:
                    with self.assertRaisesRegex(RuntimeError, "已尝试 2 次"):
                        fucker["Fucker"].fuckAiCourse(f, 1, 1, {"max_exam_attempts": 2})
            self.assertEqual(factory.return_value.startFuck.call_count, 1 if success else 2)

    def ctx(self):
        ctx = fucker["ExamCtx"].__new__(fucker["ExamCtx"])
        ctx.fucker = Mock()
        ctx.courseId = ctx.examTestId = ctx.examPaperId = 1
        ctx.sheetContent = None
        ctx.getAnswer = Mock(return_value=None)
        ctx.referenceMaterials = []
        ctx.op = Mock()
        return ctx

    def question(self, **extra):
        return {"id": 1, "questionType": 1, "content": "q",
                "optionVos": [{"id": 10, "content": "a"}, {"id": 20, "content": "b"}], **extra}

    def test_all_sheet_sections_are_loaded(self):
        ctx = self.ctx()
        ctx.fucker.zhidaoAiExamQuery.return_value = {"data": {"partSheetVos": [
            {"questionSheetVos": [{"questionId": 1}]}, {"questionSheetVos": [{"questionId": 2}]}]}}
        self.assertEqual([q["questionId"] for q in ctx.getSheetContent()], [1, 2])

    def test_failed_model_never_falls_back_to_random(self):
        for qtype in (1, 2, 14):
            ctx = self.ctx()
            ctx.op.generateAnswer.side_effect = RuntimeError("offline")
            with self.subTest(qtype=qtype), self.assertRaisesRegex(RuntimeError, "已停止作答"):
                ctx.getQuestionAnswer(self.question(questionType=qtype))

    def test_stale_cache_generates_new_answer(self):
        for cached in ("", "999", "10#@#20", "10#@#10"):
            ctx = self.ctx()
            ctx.getAnswer.return_value = {"answer": cached}
            ctx.op.generateAnswer.return_value = [20]
            self.assertEqual(ctx.getQuestionAnswer(self.question()), ([20], "AI generated"))

    def test_ai_image_question_requires_manual_work(self):
        ctx = self.ctx()
        with self.assertRaisesRegex(ValueError, "图片"):
            ctx.getQuestionAnswer(self.question(content='<p>see <img src="example.png"></p>'))
        ctx.op.generateAnswer.assert_not_called()

    def test_long_reference_prompt_truncates_with_integer_slice(self):
        code = definitions("fucker.py", {"Openai"}, json=json, re=re, logger=Mock())
        op = code["Openai"].__new__(code["Openai"])
        op.encoder = Mock()
        op.encoder.encode.return_value = list(range(30000))
        op.encoder.decode.return_value = "shortened"
        op.useZhidao = False
        op.openaiCompletion = Mock(return_value='```answer\r\n[{"id":10,"content":"a"}]\r\n```')
        self.assertEqual(op.generateAnswer("long document"), [10])
        self.assertEqual(len(op.encoder.decode.call_args.args[0]), 27900)
        op.openaiCompletion.assert_called_once_with("shortened")

    def test_invalid_answer_cannot_be_saved_or_submitted(self):
        for answer in ([10, 20], [10, 10], "10", [999]):
            ctx = self.ctx()
            ctx.readAnswerCache = Mock()
            ctx.openExam = Mock()
            ctx.getSheetContent = Mock(return_value=[{"questionId": 1}])
            ctx.getQuestionContent = Mock(return_value=self.question())
            ctx.getQuestionAnswer = Mock(return_value=(answer, "mock"))
            ctx.saveAnswer = Mock()
            ctx.submitExam = Mock()
            ctx.progress_view = False
            with self.subTest(answer=answer), self.assertRaisesRegex(RuntimeError, "拒绝提交"):
                ctx.startFuck([])
            ctx.saveAnswer.assert_not_called()
            ctx.submitExam.assert_not_called()
            self.assertTrue(ctx.examStopped)

    def test_plain_dict_question_and_hidden_grading(self):
        ctx = self.ctx()
        ctx.readAnswerCache = Mock()
        ctx.openExam = Mock()
        ctx.getSheetContent = Mock(return_value=[{"questionId": 1}])
        ctx.getQuestionContent = Mock(return_value=self.question(userAnswerVo=[]))
        ctx.getQuestionAnswer = Mock(return_value=([10], "mock"))
        ctx.saveAnswer = Mock(return_value=True)
        ctx.submitExam = Mock()
        ctx.setAnswer = Mock()
        ctx.progress_view = False
        self.assertEqual(ctx.startFuck([]), (False, 0, 1))
        ctx.submitExam.assert_called_once()
        ctx.setAnswer.assert_not_called()

    def test_ai_completion_normalizes_versioned_endpoint(self):
        requests = Mock()
        requests.post.return_value.json.return_value = {"choices": [{"message": {"content": "ok"}}]}
        code = definitions("fucker.py", {"Openai"}, requests=requests)
        fake_hw = types.ModuleType("homework")
        fake_hw.chat_endpoint = hw["chat_endpoint"]
        op = code["Openai"].__new__(code["Openai"])
        op.apiKey, op.modelName, op.stream = "test", "test", False
        with patch.dict(sys.modules, {"homework": fake_hw}):
            for base in ("https://api.test", "https://api.test/v1", "https://api.test/v1/chat/completions"):
                op.baseUrl = base
                self.assertEqual(op.openaiCompletion("q"), "ok")
                self.assertEqual(requests.post.call_args.args[0], "https://api.test/v1/chat/completions")


class BrowserAnswerTests(unittest.TestCase):
    def test_partial_existing_multi_answer_is_not_counted_or_submitted(self):
        question = {"id": 1, "name": "q", "type": 2,
                    "options": [{"id": 10, "content": "a"}, {"id": 20, "content": "b"}]}
        page = Mock()
        page.eval.side_effect = ['["10", "20"]', '["10"]']
        engine = Mock()
        engine.answer.return_value = ("10,20", "A+B")
        replacements = dict(build_paper=Mock(return_value=[question]), open_exam_page=Mock(return_value=page),
                            wait_for_captcha=Mock(return_value=True), wait_for_question=Mock(return_value=True),
                            click_button=Mock(), click_value=Mock())
        with patch.dict(browser, replacements), contextlib.redirect_stdout(io.StringIO()):
            result = browser["run_one"](Mock(), Mock(), engine, {"problemNum": 1}, {"name": "c"}, submit=True)
        self.assertEqual(result["filled"], 0)
        self.assertFalse(result["submitted"])
        self.assertTrue(result["requires_manual"])
        replacements["click_button"].assert_not_called()

    def test_redo_regenerates_answer_for_new_option_ids(self):
        old = {"id": 1, "name": "q", "type": 1, "options": [{"id": 10, "content": "a"}]}
        new = {**old, "options": [{"id": 20, "content": "a"}]}
        client, engine = Mock(), Mock()
        engine.answer.side_effect = [(10, "A"), (20, "A")]
        item = {"state": 4, "problemNum": 1, "backNum": 1, "score": 0, "totalScore": 1}
        replacements = dict(build_paper=Mock(side_effect=[[old], [new], [new]]),
                            open_exam_page=Mock(return_value=None), needs_work=hw["needs_work"])
        with patch.dict(browser, replacements), contextlib.redirect_stdout(io.StringIO()):
            browser["run_one"](Mock(), client, engine, item,
                               {"name": "c", "recruitId": "r", "courseId": "c"})
        client.redo.assert_called_once()
        self.assertEqual(engine.answer.call_count, 2)
        self.assertEqual(engine.answer.call_args.args[-1], new["options"])

    def test_completed_paper_is_read_without_reopening(self):
        code = definitions("homework_browser.py", {"build_paper"}, pick=hw["pick"], parse_questions=lambda q: q)
        client = Mock()
        client.look_paper.return_value = ["old"]
        self.assertEqual(code["build_paper"](client, {"state": 4},
                         {"recruitId": "r", "schoolId": "s", "courseId": "c"}), ["old"])
        client.open_paper.assert_not_called()


class ApiRedoTests(unittest.TestCase):
    def test_last_redo_is_not_spent_twice(self):
        client, engine = Mock(), Mock()
        item = {"state": 4, "backNum": 3, "isMarking": 2, "score": 0,
                "totalScore": 1, "problemNum": 1}
        client.open_paper.return_value = [{"id": 1, "eid": "e", "name": "q", "questionType": 1,
                                          "questionOptions": [{"id": 10, "content": "a"}]}]
        client.submit.return_value = "0"
        client.check.return_value = [{"id": 1, "name": "q", "is_correct": "0", "answer": "10"}]
        engine.answer.return_value = (10, "A")
        with contextlib.redirect_stdout(io.StringIO()):
            result = hw["solve_one"](client, engine, item, "r", "s", "c", rounds=5)
        client.redo.assert_called_once()
        client.submit.assert_called_once()
        self.assertTrue(result["submitted"])

    def test_exhausted_redo_does_not_mutate_exam(self):
        client = Mock()
        with contextlib.redirect_stdout(io.StringIO()):
            result = hw["solve_one"](client, Mock(),
                {"state": 4, "backNum": 1, "isMarking": 1, "totalScore": 1}, "r", "s", "c")
        client.redo.assert_not_called()
        client.open_paper.assert_not_called()
        self.assertFalse(result["submitted"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
