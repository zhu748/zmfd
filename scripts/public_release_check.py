#!/usr/bin/env python3
"""Fail closed when a glm2api release candidate contains local or secret data."""

from __future__ import annotations

import argparse
import ast
import json
import locale
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
TEXT_EXTS = {
    ".cmd", ".css", ".env", ".html", ".js", ".json", ".md", ".mjs",
    ".ps1", ".py", ".toml", ".txt", ".yaml", ".yml",
}
FORBIDDEN_NAMES = {
    ".env",
    ".env.local",
    "apikey.local.json",
    "history.local.json",
    "pending_deletes.local.json",
    "profiles.local.json",
    "settings.local.json",
}
FORBIDDEN_DIRS = {"__pycache__", "history.local.json.d", "logs", "node_modules"}
FORBIDDEN_SUFFIXES = {".db", ".har", ".key", ".log", ".pcap", ".pcapng", ".pfx", ".p12", ".saz", ".sqlite", ".sqlite3"}
SECRET_PATTERNS = {
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    "OpenAI-style key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "JWT": re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    "Windows user path": re.compile(r"\b[A-Za-z]:\\Users\\[^\\\s]+", re.I),
    "escaped Windows user path": re.compile(r"\b[A-Za-z]:\\\\Users\\\\[^\\\s]+", re.I),
    "Unix user path": re.compile(r"/(?:home|Users)/[^/\s]+"),
}
CREDENTIAL_LITERAL_RE = re.compile(
    r'''(?im)^\s*["']?(?:password|passwd|api[_-]?key|token|secret)["']?\s*[:=]\s*(["'])([^"'\r\n]+)\1'''
)
PLACEHOLDER_MARKERS = {
    "<redacted>", "change-me", "changeme", "dummy", "example", "placeholder",
    "test", "your-", "your_",
}


def decode_git_output(value: bytes) -> str:
    """Decode Git-for-Windows paths without assuming its output code page."""
    encodings = (locale.getpreferredencoding(False), sys.getfilesystemencoding(), "utf-8")
    for encoding in dict.fromkeys(item for item in encodings if item):
        try:
            return value.decode(encoding)
        except UnicodeDecodeError:
            continue
    return value.decode("utf-8", errors="surrogateescape")


def git_candidates(staged: bool) -> tuple[list[Path], list[str]]:
    command = (
        ["git", "-C", str(ROOT), "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"]
        if staged
        else ["git", "-C", str(ROOT), "ls-files", "--cached", "--others", "--exclude-standard", "-z"]
    )
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        if isinstance(detail, bytes):
            detail = decode_git_output(detail)
        return [], [f"git candidate discovery failed: {str(detail).strip()[:300]}"]
    files: list[Path] = []
    failures: list[str] = []
    # NUL 分隔避免 Git 对中文、空格、引号及换行文件名做 C 风格转义，
    # 否则构造出的路径可能不存在并被静默漏扫。
    for value in decode_git_output(result.stdout).split("\0"):
        if not value:
            continue
        path = (ROOT / value).resolve()
        try:
            path.relative_to(ROOT.resolve())
        except ValueError:
            failures.append(f"candidate escapes repository root: {value}")
            continue
        if path.is_file():
            files.append(path)
    return files, failures


def is_placeholder(value: str) -> bool:
    lowered = value.strip().lower()
    return len(lowered) < 12 or any(marker in lowered for marker in PLACEHOLDER_MARKERS)


def scan_file(path: Path) -> list[str]:
    rel = path.relative_to(ROOT)
    rel_text = rel.as_posix()
    lowered_parts = {part.lower() for part in rel.parts}
    failures: list[str] = []
    if rel.name.lower() in FORBIDDEN_NAMES:
        failures.append(f"local state file must not be public: {rel_text}")
    if lowered_parts & FORBIDDEN_DIRS:
        failures.append(f"local runtime directory must not be public: {rel_text}")
    if path.suffix.lower() in FORBIDDEN_SUFFIXES:
        failures.append(f"capture/log artifact must not be public: {rel_text}")
    if failures or (path.suffix.lower() not in TEXT_EXTS and path.name not in {"LICENSE"}):
        return failures

    text = path.read_text(encoding="utf-8", errors="replace")
    for label, pattern in SECRET_PATTERNS.items():
        if pattern.search(text):
            failures.append(f"{label}: {rel_text}")
    for _quote, value in CREDENTIAL_LITERAL_RE.findall(text):
        if not is_placeholder(value):
            failures.append(f"non-placeholder credential literal: {rel_text}")
            break
    if path.suffix.lower() == ".py":
        try:
            ast.parse(text, filename=rel_text)
        except SyntaxError as exc:
            failures.append(f"python syntax: {rel_text}:{exc.lineno}: {exc.msg}")
    if path.suffix.lower() == ".json":
        try:
            json.loads(text)
        except Exception as exc:
            failures.append(f"json parse: {rel_text}: {exc}")
    return failures


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staged", action="store_true", help="scan only staged release candidates")
    parser.add_argument("paths", nargs="*", type=Path, help="explicit repository-relative candidate paths")
    args = parser.parse_args(argv)
    if args.staged and args.paths:
        parser.error("--staged cannot be combined with explicit paths")
    if args.paths:
        files = []
        failures = []
        for value in args.paths:
            path = (ROOT / value).resolve() if not value.is_absolute() else value.resolve()
            try:
                path.relative_to(ROOT.resolve())
            except ValueError:
                failures.append(f"candidate escapes repository root: {value}")
                continue
            if not path.is_file():
                failures.append(f"candidate is not a file: {value}")
                continue
            files.append(path)
    else:
        files, failures = git_candidates(args.staged)
    if args.staged and not files:
        failures.append("release candidate is empty")
    for path in files:
        failures.extend(scan_file(path))
    payload: dict[str, Any] = {
        "overall": "PASS" if not failures else "FAIL",
        "files_scanned": len(files),
        "failures": failures,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
