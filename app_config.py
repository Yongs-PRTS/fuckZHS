"""Shared application paths and configuration defaults."""

from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
from pathlib import Path


APP_ROOT = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent
)
BUNDLE_ROOT = Path(getattr(sys, "_MEIPASS", APP_ROOT)).resolve()
RUNTIME_DIR = APP_ROOT / "runtime"
CONFIG_PATH = APP_ROOT / "config.json"
COOKIES_PATH = APP_ROOT / "cookies.json"
EXECUTION_PATH = APP_ROOT / "execution.json"


DEFAULT_CONFIG = {
    "username": "",
    "password": "",
    "qrlogin": True,
    "save_cookies": True,
    "proxies": {},
    "logLevel": "INFO",
    "tree_view": True,
    "progressbar_view": True,
    "qr_extra": {
        "show_in_terminal": None,
        "ensure_unicode": False,
    },
    "image_path": "",
    "pushplus": {
        "enable": False,
        "token": "",
    },
    "bark": {
        "enable": False,
        "token": "https://example.com/xxxxxxxxx",
    },
    "config_version": "1.4.0",
    "llm": {
        "provider": "",
        "base_url": "",
        "api_key": "",
        "model": "",
    },
    "ai": {
        "enabled": True,
        "use_zhidao_ai": True,
        "openai": {
            "api_base": "https://api.openai.com",
            "api_key": "",
            "model_name": "",
        },
        "ppt_processing": {
            "provide_to_ai": False,
            "moonShot": {
                "base_url": "https://api.moonshot.cn/v1",
                "api_key": "sk-",
                "delete_after_convert": True,
            },
        },
        "use_stream": True,
        "max_exam_attempts": 3,
    },
}


def env_var(name: str) -> str:
    """读环境变量；进程环境里没有时，回落到 Windows 用户级环境变量（setx 写入的值）"""
    value = (os.environ.get(name) or "").strip()
    if value or os.name != "nt":
        return value
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, name)
        return str(value).strip()
    except OSError:
        return ""


def merge_defaults(defaults, current):
    if not isinstance(current, dict):
        return copy.deepcopy(defaults)
    merged = copy.deepcopy(defaults)
    for key, value in current.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_defaults(merged[key], value)
        else:
            merged[key] = value
    return merged


def write_json_atomic(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temp_path = Path(stream.name)
            json.dump(payload, stream, ensure_ascii=False, indent=4)
            stream.flush()
        temp_path.replace(path)
    finally:
        if temp_path and temp_path.exists():
            temp_path.unlink(missing_ok=True)


def ensure_config() -> Path:
    if not CONFIG_PATH.exists():
        write_json_atomic(CONFIG_PATH, DEFAULT_CONFIG)
    return CONFIG_PATH


def load_config() -> dict:
    ensure_config()
    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as stream:
            current = json.load(stream)
    except (OSError, ValueError, TypeError):
        current = {}
    return merge_defaults(DEFAULT_CONFIG, current)


def resource_path(*parts: str) -> Path:
    return BUNDLE_ROOT.joinpath(*parts)
