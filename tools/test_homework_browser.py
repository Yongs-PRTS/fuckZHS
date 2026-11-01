"""Offline startup regressions and optional real Edge smoke (blank pages only)."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

import requests
import websocket

from audit_checks import definitions


def browser_code():
    return definitions(
        "homework_browser.py",
        {"EDGE_CANDIDATES", "BrowserGone", "BrowserTimeout", "Cdp", "launch_edge",
         "debug_endpoint", "wait_for_target", "list_targets", "wait_new_target",
         "stop_edge", "run_flow"},
        os=os, Path=Path, json=json, requests=requests, websocket=websocket,
        subprocess=subprocess, tempfile=tempfile, shutil=shutil, time=time,
        urlsplit=urlsplit,
    )


class StartupTests(unittest.TestCase):
    def test_exited_launcher_with_live_socket(self):
        code = browser_code()
        ws = Mock(connected=True)
        ws.recv.return_value = '{"id":1,"result":{"enabled":true}}'
        with patch.object(websocket, "create_connection", return_value=ws) as connect:
            cdp = code["Cdp"]("ws://127.0.0.1:12345/devtools/page/test", proc=Mock(poll=lambda: 0))
            self.assertEqual(cdp.send("Page.enable"), {"enabled": True})
            self.assertTrue(connect.call_args.kwargs["suppress_origin"])
            self.assertIn("127.0.0.1", connect.call_args.kwargs["http_no_proxy"])
            ws.connected = False
            with self.assertRaises(code["BrowserGone"]):
                cdp.send("Page.enable")

    def test_connection_closure_detected_during_receive(self):
        code = browser_code()
        ws = Mock(connected=True)
        ws.recv.return_value = ""
        with patch.object(websocket, "create_connection", return_value=ws):
            with self.assertRaises(code["BrowserGone"]):
                code["Cdp"]("ws://127.0.0.1:12345/test").send("Page.enable")

    def test_each_profile_has_its_own_endpoint(self):
        code = browser_code()
        with tempfile.TemporaryDirectory() as folder:
            for port in (12345, 23456):
                profile = Path(folder) / str(port)
                profile.mkdir()
                (profile / "DevToolsActivePort").write_text(f"{port}\n/devtools/browser/test\n")
                code["list_targets"] = Mock(return_value=[{"webSocketDebuggerUrl": "test"}])
                code["wait_for_target"](profile, proc=Mock(poll=lambda: 0))
                code["list_targets"].assert_called_once_with(port)

    def test_missing_endpoint_has_diagnostic(self):
        code = browser_code()
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(RuntimeError, "退出码 9"):
                code["wait_for_target"](Path(folder), timeout=0, proc=Mock(poll=lambda: 9))

    def test_local_http_ignores_proxy_environment(self):
        code = browser_code()
        with patch.object(requests, "Session") as factory:
            session = factory.return_value.__enter__.return_value
            session.get.return_value.json.return_value = [{"type": "page", "id": "ours"}]
            self.assertEqual(code["list_targets"](12345)[0]["id"], "ours")
            self.assertFalse(session.trust_env)

    def test_failed_startup_is_cleaned_and_not_preserved(self):
        for stage in ("launch_edge", "wait_for_target", "Cdp", "inject_cookies"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as folder:
                code = browser_code()
                profile = Path(folder) / "profile"
                profile.mkdir()
                code.update(
                    collect_pending=Mock(return_value=(None, Mock(), [])), AnswerEngine=Mock(),
                    tempfile=Mock(mkdtemp=Mock(return_value=str(profile))),
                    launch_edge=Mock(return_value=Mock()),
                    wait_for_target=Mock(return_value={"webSocketDebuggerUrl": "test"}),
                    Cdp=Mock(), inject_cookies=Mock(return_value=0), stop_edge=Mock(),
                )
                code[stage].side_effect = RuntimeError("startup failed")
                output = io.StringIO()
                with contextlib.redirect_stdout(output), self.assertRaisesRegex(RuntimeError, "startup failed"):
                    code["run_flow"]([], keep_browser=True)
                code["stop_edge"].assert_called_once()
                self.assertNotIn("浏览器已保留", output.getvalue())

    def test_cleanup_closes_browser_after_launcher_exit(self):
        code = browser_code()
        with tempfile.TemporaryDirectory() as folder:
            profile = Path(folder) / "profile"
            profile.mkdir()
            (profile / "DevToolsActivePort").write_text("12345\n/devtools/browser/test\n")
            code["Cdp"] = Mock()
            proc = Mock(poll=lambda: 0)
            code["stop_edge"](proc, profile)
            code["Cdp"].return_value.send.assert_called_once_with("Browser.close", timeout=5)
            proc.terminate.assert_not_called()
            self.assertFalse(profile.exists())


def smoke(headless):
    code = browser_code()
    sessions = []
    try:
        for index in range(2):
            profile = Path(tempfile.mkdtemp(prefix="zhs_edge_smoke_"))
            entry = [None, profile, None]
            sessions.append(entry)
            entry[0] = code["launch_edge"](profile, headless=headless)
            page = code["wait_for_target"](profile, proc=entry[0])
            entry[2] = code["Cdp"](page["webSocketDebuggerUrl"], proc=entry[0])
            entry[2].send("Page.enable")
            self_check = entry[2].eval(f"window.startupSmoke = {index}; 6 * 7")
            assert self_check == 42
            print(json.dumps({"launch": index + 1, "port": entry[2].port,
                              "launcher_exit": entry[0].poll(), "cdp_result": self_check}))
        first, second = sessions[0][2], sessions[1][2]
        assert first.port != second.port
        assert first.eval("window.startupSmoke") == 0
        assert second.eval("window.startupSmoke") == 1
        click_value = definitions("homework_browser.py", {"click_value"}, json=json)["click_value"]
        first.eval("""document.body.innerHTML = `
          <div style="display:none"><label class="fl"><input type="checkbox" value="choice-x"></label></div>
          <label class="fl"><input type="checkbox" value="choice-x" id="active">Visible</label>
          <label class="fl"><input type="radio" value="choice-y" id="radio">Radio</label>
          <label class="fl"><input type="checkbox" value="blocked" id="blocked">Disabled by app</label>`;
          document.getElementById('blocked').addEventListener('click', e => e.preventDefault());
        """)
        assert click_value(first, "choice-x")
        assert click_value(first, "choice-y")
        assert first.eval("document.getElementById('active').checked") is True
        assert first.eval("document.querySelector('input').checked") is False
        assert not click_value(first, "blocked")
        assert first.eval("document.getElementById('blocked').checked") is False
        print("PASS: real Edge option clicks; hidden duplicate skipped; canceled selection stays unselected")
        second_entry = sessions.pop()
        code["stop_edge"](*second_entry)
        assert first.eval("window.startupSmoke") == 0
        print("PASS: two independent browsers; closing the second preserves the first")
    finally:
        for entry in reversed(sessions):
            code["stop_edge"](*entry)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()
    if args.smoke:
        smoke(args.headless)
    else:
        unittest.main(argv=[__file__], verbosity=2)
