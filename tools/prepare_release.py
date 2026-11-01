"""Create a source-only ZIP from an allowlist; never include runtime/account data."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import zipfile

ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = {".gitignore", "README.md", "LICENSE", "requirements.txt", "meta.json",
              "build_windows.ps1", "config.example.json"}
DIRECTORIES = {
    "web": {".html", ".css", ".js", ".svg"},
    "tools": {".py"},
    ".github": {".yml", ".yaml", ".md"},
    "packaging": {".txt"},
    "images": {".png", ".jpg", ".webp"},
    "docs": {".md"},
    "decrypt": {".py"},
}
SECRET = re.compile(r"sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")


def source_files():
    files = [ROOT / name for name in ROOT_FILES]
    files.extend(ROOT.glob("*.py"))
    for directory, suffixes in DIRECTORIES.items():
        files.extend(p for p in (ROOT / directory).rglob("*")
                     if p.is_file() and p.suffix.lower() in suffixes
                     and "__pycache__" not in p.parts)
    for path in sorted(set(files)):
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(ROOT):
            raise ValueError(f"Missing or unsafe source file: {path.relative_to(ROOT)}")
        yield path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "releases/ZHS-source.zip")
    args = parser.parse_args()
    files = list(source_files())
    manifest = {}
    for path in files:
        data = path.read_bytes()
        if path.suffix.lower() not in {".png", ".jpg", ".webp"}:
            if SECRET.search(data.decode("utf-8-sig")):
                raise ValueError(f"Possible credential in {path.relative_to(ROOT)} (value not displayed)")
        manifest[path.relative_to(ROOT).as_posix()] = hashlib.sha256(data).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(args.output, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, path.relative_to(ROOT).as_posix())
    args.output.with_suffix(".manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"archive": str(args.output), "files": len(files),
                      "bytes": args.output.stat().st_size,
                      "sha256": hashlib.sha256(args.output.read_bytes()).hexdigest()}))


if __name__ == "__main__":
    main()
