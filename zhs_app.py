"""Single entry point for development and the packaged executable."""

from __future__ import annotations

import sys


def runtime_self_check(output_path: str, https_probe: bool = False) -> bool:
    """Check frozen TLS resources without login, cookies, or the desktop UI."""
    import json
    from pathlib import Path

    result = {"ok": False}
    try:
        import ssl
        import requests
        from urllib3.util.ssl_ import create_urllib3_context

        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        client_context = create_urllib3_context()
        if context.verify_mode != ssl.CERT_REQUIRED or client_context.verify_mode != ssl.CERT_REQUIRED:
            raise RuntimeError("TLS certificate verification is not enabled")
        if not Path(requests.certs.where()).is_file():
            raise RuntimeError("CA bundle is missing")
        result.update(ssl_context=True, http_client_tls_context=True,
                      certificate_verification=True, ca_bundle_available=True)
        if https_probe:
            with requests.Session() as session:
                response = session.get("https://passport.zhihuishu.com/", timeout=15)
                result.update(https_verified=True, https_status=response.status_code)
        result["ok"] = True
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    Path(output_path).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result["ok"]


def main() -> None:
    if "--self-check" in sys.argv:
        index = sys.argv.index("--self-check")
        if index + 1 >= len(sys.argv):
            raise SystemExit("--self-check requires an output JSON path")
        raise SystemExit(0 if runtime_self_check(sys.argv[index + 1], "--https-probe" in sys.argv) else 1)

    if "--worker" in sys.argv:
        sys.argv.remove("--worker")
        from app_worker import main as worker_main

        worker_main()
        return

    if "--browser" in sys.argv:
        sys.argv.remove("--browser")
        from web_console import main as console_main

        console_main()
        return

    from desktop_app import main as desktop_main

    desktop_main()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        if getattr(sys, "frozen", False):
            import ctypes

            ctypes.windll.user32.MessageBoxW(0, f"程序启动失败：\n{exc}", "ZHS 学习助手", 0x10)
        else:
            raise
