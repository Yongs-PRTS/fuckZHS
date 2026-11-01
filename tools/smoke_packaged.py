"""Check a packaged console and worker in a temporary copy with dummy configuration."""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=Path, default=ROOT / "dist/ZHS-Desktop.exe")
    parser.add_argument("--network", action="store_true", help="Also verify HTTPS to the public login host without account cookies")
    args = parser.parse_args()
    build_dir = ROOT / "build"
    build_dir.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="packaged-smoke-", dir=build_dir) as folder:
        work = Path(folder)
        executable = work / "ZHS-Desktop.exe"
        shutil.copy2(args.exe, executable)
        config = {"llm": {"provider": "custom", "base_url": "https://example.test/v1",
                          "model": "audit-model", "api_key": "dummy-key-for-local-smoke-only"}}
        (work / "config.json").write_text(json.dumps(config), encoding="utf-8")
        environment = dict(os.environ)
        for name in ("HOMEWORK_LLM_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
            environment[name] = "dummy-environment-key"
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]

        def request(method, path, body=None):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            try:
                conn.request(method, path, body=json.dumps(body) if body is not None else None,
                             headers={"Content-Type": "application/json"})
                response = conn.getresponse()
                return response.status, response.read()
            finally:
                conn.close()

        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        tls_path = work / "tls-check.json"
        tls_command = [str(executable), "--self-check", str(tls_path)]
        if args.network:
            tls_command.append("--https-probe")
        with (work / "tls-smoke.log").open("w", encoding="utf-8") as stream:
            tls_run = subprocess.run(tls_command, cwd=work, env=environment, stdout=stream,
                                     stderr=subprocess.STDOUT, creationflags=creationflags, timeout=60)
        tls = json.loads(tls_path.read_text(encoding="utf-8"))
        assert tls_run.returncode == 0 and tls.get("ok"), tls
        with (work / "console-smoke.log").open("w", encoding="utf-8") as stream:
            process = subprocess.Popen([str(executable), "--browser", "--host", "127.0.0.1",
                                        "--port", str(port), "--no-browser"],
                                       cwd=work, env=environment, stdout=stream, stderr=subprocess.STDOUT,
                                       creationflags=creationflags)
            try:
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError(f"packaged console exited with code {process.returncode}")
                    try:
                        status, body = request("GET", "/api/health")
                        if status == 200 and json.loads(body).get("ok"):
                            break
                    except OSError:
                        pass
                    time.sleep(.25)
                else:
                    raise RuntimeError("packaged console did not become healthy")
                started = time.monotonic()
                status, payload = request("GET", "/api/settings")
                settings_seconds = round(time.monotonic() - started, 3)
                assert status == 200 and json.loads(payload)["llm"]["has_key"]
                assert b"dummy-key-for-local-smoke-only" not in payload
                for name in ("app.js", "styles.css"):
                    status, body = request("GET", f"/{name}")
                    assert status == 200 and body == (ROOT / "web" / name).read_bytes(), name
                assert request("POST", "/api/quit", {})[0] == 200
                process.wait(timeout=10)
                assert process.returncode == 0
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)

        runtime = work / "runtime"
        runtime.mkdir(exist_ok=True)
        request_path = runtime / "smoke-request.json"
        request_path.write_text('{"action":"__offline_audit_invalid_action__"}', encoding="utf-8")
        with (work / "worker-smoke.log").open("w", encoding="utf-8") as stream:
            run = subprocess.run([str(executable), "--worker", "--request", str(request_path)],
                                 cwd=work, env=environment, stdout=stream, stderr=subprocess.STDOUT,
                                 creationflags=creationflags, timeout=60)
        status = json.loads((runtime / "action.json").read_text(encoding="utf-8"))
        assert run.returncode == 1 and status["state"] == "failed"
        assert "__offline_audit_invalid_action__" in status["error"]
        result = {"console_health": True, "settings_masked": True, "settings_seconds": settings_seconds,
                  "tls": tls,
                  "packaged_frontend_matches_source": True, "clean_console_shutdown": True,
                  "worker_imports_and_reports_error": True, "real_account_used": False,
                  "sha256": hashlib.sha256(args.exe.read_bytes()).hexdigest()}
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
