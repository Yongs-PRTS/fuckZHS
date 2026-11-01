"""CLI entry-point regressions without logging in or using real configuration."""
from __future__ import annotations

import argparse
import ast
import contextlib
import io
import sys
import types
import unittest
from unittest.mock import Mock, patch

from audit_checks import ROOT, definitions


class CliTests(unittest.TestCase):
    def setUp(self):
        self.code = definitions("main.py", {"validate_ai_config", "parse_bool"}, argparse=argparse)

    def test_platform_ai_does_not_require_external_credentials(self):
        self.code["validate_ai_config"]({"ai": {"enabled": True, "use_zhidao_ai": True}})

    def test_external_ai_reuses_shared_model_configuration(self):
        fake = types.ModuleType("homework")
        fake.AnswerEngine = Mock()
        fake.PLACEHOLDER_KEYS = {"", "sk-"}
        with patch.dict(sys.modules, {"homework": fake}):
            self.code["validate_ai_config"]({"ai": {"enabled": True, "use_zhidao_ai": False}})
        fake.AnswerEngine._resolve.assert_called_once_with("", "", "")

    def test_invalid_external_configuration_fails(self):
        fake = types.ModuleType("homework")
        fake.AnswerEngine = Mock()
        fake.PLACEHOLDER_KEYS = {"", "sk-"}
        fake.AnswerEngine._resolve.side_effect = RuntimeError("missing key")
        with patch.dict(sys.modules, {"homework": fake}):
            with self.assertRaises(RuntimeError):
                self.code["validate_ai_config"]({"ai": {"enabled": True, "use_zhidao_ai": False}})
            with self.assertRaises(ValueError):
                self.code["validate_ai_config"]({"ai": {"enabled": True, "openai": {"api_key": "test"}}})

    def test_skipping_exam_needs_no_ai_configuration(self):
        self.code["validate_ai_config"]({}, no_exam=True)
        with self.assertRaises(ValueError):
            self.code["validate_ai_config"]({"ai": {"enabled": False}})

    def test_noexam_flag_accepts_bare_and_legacy_boolean_values(self):
        # Execute the actual argument declaration, keeping it in sync with main.py.
        tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
        node = next(n for n in tree.body if isinstance(n, ast.Expr)
                    and isinstance(n.value, ast.Call) and n.value.args
                    and isinstance(n.value.args[0], ast.Constant) and n.value.args[0].value == "--noexam")
        parser = argparse.ArgumentParser()
        exec(compile(ast.Module(body=[node], type_ignores=[]), "main.py", "exec"),
             {"parser": parser, "parse_bool": self.code["parse_bool"]})
        for arguments, expected in (([], False), (["--noexam"], True), (["--noexam", "True"], True),
                                    (["--noexam", "false"], False)):
            self.assertIs(parser.parse_args(arguments).noexam, expected)

    def test_ai_runtime_failure_returns_failure_exit_code(self):
        tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
        blocks = [n for n in tree.body if isinstance(n, ast.If)
                  and isinstance(n.test, ast.Attribute) and n.test.attr == "aicourse"]
        block = blocks[-1]
        for error, expected in ((None, 0), (RuntimeError("failed"), 1)):
            runtime = Mock()
            runtime.fuckAiCourse.side_effect = error
            scope = {"args": types.SimpleNamespace(aicourse=["1", "2"], noexam=False),
                     "config": types.SimpleNamespace(ai={}), "fucker": runtime, "logger": Mock()}
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as caught:
                exec(compile(ast.Module(body=[block], type_ignores=[]), "main.py", "exec"), scope)
            self.assertEqual(caught.exception.code, expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
