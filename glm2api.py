#!/usr/bin/env python3
"""
Z.ai GLM-5.2 HAR-based client / OpenAI-compatible local proxy.

This script is case-local. It reads browser state from a user-provided HAR,
reconstructs the Z.ai front-end signature algorithm, and can either:

1. send one prompt directly to `glm-5.3`;
2. serve a minimal `/v1/chat/completions` endpoint locally.

Secrets are never printed by default. Use only with authorized CTF/lab traffic.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import logging
import copy
import csv
import email.utils
import functools
import gc
import hashlib
import html
import hmac
import http.client
import ipaddress
import json
import mimetypes
import os
import queue
import re
import secrets
import shutil
import string
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import webbrowser
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit
from urllib.request import Request, urlopen
from xml.etree import ElementTree


# ---------------------------------------------------------------------------
# 运行日志基础设施：文件轮转 + stderr + 内存环形缓冲（供 /api/logs 与面板查看）。
# 安全约定：token / apikey / captcha 等敏感值一律只记 sha16 指纹，不落全文。
# ---------------------------------------------------------------------------
LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_FILE_PATH = LOG_DIR / "glm2api.log"
LOG_MAX_BYTES = 8 * 1024 * 1024
LOG_BACKUP_COUNT = 5
LOG_RING_CAPACITY = 1500
LOG_FORMAT = "%(asctime)s.%(msecs)03d [%(levelname)s] [%(threadName)s] %(message)s"
LOG = logging.getLogger("glm2api")

_REQ_CONTEXT = threading.local()


class RingBufferHandler(logging.Handler):
    """Keep structured log entries in memory while preserving plain-line output."""

    def __init__(self, capacity: int = LOG_RING_CAPACITY) -> None:
        super().__init__(level=logging.DEBUG)
        self.capacity = max(1, int(capacity))
        self._lock = threading.Lock()
        self._records: deque[dict[str, Any]] = deque(maxlen=self.capacity)
        self._sequence = 0
        self.dropped_by_filter = 0

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
        except Exception:
            return
        message = record.getMessage()
        state = ""
        rid = ""
        kind = "system"
        if message.startswith("{"):
            try:
                payload = json.loads(message)
            except Exception:
                payload = None
            if isinstance(payload, dict) and payload.get("state"):
                state = str(payload.get("state") or "")[:120]
                rid = str(payload.get("rid") or "")[:32]
                kind = "event"
        if kind == "system":
            access = re.match(r"\[([0-9a-f]{8})\]\s+(REQ|RES)\b", message, re.I)
            if access:
                rid = access.group(1)
                kind = "access"
            elif record.levelno >= logging.ERROR:
                kind = "error"
        with self._lock:
            self._sequence += 1
            self._records.append(
                {
                    "seq": self._sequence,
                    "timestamp_ms": int(record.created * 1000),
                    "level": record.levelname,
                    "levelno": record.levelno,
                    "thread": str(record.threadName or "")[:80],
                    "kind": kind,
                    "state": state,
                    "rid": rid,
                    "message": message,
                    "line": line,
                }
            )

    def query(
        self,
        limit: int = 300,
        min_level: int = logging.DEBUG,
        contains: str = "",
        state: str = "",
        kind: str = "",
        rid: str = "",
        after_seq: int = 0,
    ) -> tuple[list[dict[str, Any]], int, dict[str, Any]]:
        """Return filtered entries, match count, and a ring-aware incremental cursor."""
        with self._lock:
            records = list(self._records)
        first_seq = int(records[0]["seq"]) if records else 0
        last_seq = int(records[-1]["seq"]) if records else 0
        after_seq = max(0, int(after_seq))
        reset_required = bool(
            after_seq
            and records
            and (after_seq < first_seq - 1 or after_seq > last_seq)
        )
        if min_level > logging.DEBUG:
            records = [item for item in records if int(item["levelno"]) >= min_level]
        if contains:
            needle = contains.lower()
            records = [item for item in records if needle in str(item["line"]).lower()]
        if state:
            state_needle = state.lower()
            records = [item for item in records if state_needle in str(item["state"]).lower()]
        if kind:
            records = [item for item in records if str(item["kind"]).lower() == kind.lower()]
        if rid:
            rid_needle = rid.lower()
            records = [item for item in records if rid_needle in str(item["rid"]).lower()]
        matched = len(records)
        if after_seq and not reset_required:
            records = [item for item in records if int(item["seq"]) > after_seq]
        cursor = {
            "first_seq": first_seq,
            "last_seq": last_seq,
            "reset_required": reset_required,
        }
        return [dict(item) for item in records[-max(1, limit) :]], matched, cursor

    def snapshot(
        self,
        limit: int = 300,
        min_level: int = logging.DEBUG,
        contains: str = "",
    ) -> list[str]:
        records, _matched, _cursor = self.query(limit=limit, min_level=min_level, contains=contains)
        return [str(item["line"]) for item in records]

    def stats(self, matched_count: int | None = None) -> dict[str, Any]:
        with self._lock:
            records = list(self._records)
        level_counts = Counter(str(item["level"]) for item in records)
        kind_counts = Counter(str(item["kind"]) for item in records)
        state_counts = Counter(str(item["state"]) for item in records if item.get("state"))
        last_error_at = max(
            (int(item["timestamp_ms"]) for item in records if int(item["levelno"]) >= logging.ERROR),
            default=0,
        )
        return {
            "total": len(records),
            "matched": len(records) if matched_count is None else max(0, int(matched_count)),
            "capacity": self.capacity,
            "levels": {name: int(level_counts.get(name, 0)) for name in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")},
            "kinds": dict(kind_counts),
            "top_states": [{"state": name, "count": count} for name, count in state_counts.most_common(16)],
            "last_error_at": last_error_at,
            "oldest_at": int(records[0]["timestamp_ms"]) if records else 0,
            "newest_at": int(records[-1]["timestamp_ms"]) if records else 0,
        }

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)


def setup_logging(level: str = "INFO", console: bool = True) -> Path:
    """Install file + stderr + ring handlers on the glm2api logger (idempotent)."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(LOG_FORMAT, datefmt="%Y-%m-%d %H:%M:%S")
    if not any(isinstance(handler, RingBufferHandler) for handler in LOG.handlers):
        ring_handler = RingBufferHandler()
        ring_handler.setFormatter(formatter)
        LOG.addHandler(ring_handler)
    if not any(isinstance(handler, RotatingFileHandler) for handler in LOG.handlers):
        file_handler = RotatingFileHandler(
            LOG_FILE_PATH,
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(logging.DEBUG)
        LOG.addHandler(file_handler)
    if console and not any(
        isinstance(handler, logging.StreamHandler) and not isinstance(handler, (RingBufferHandler, RotatingFileHandler))
        for handler in LOG.handlers
    ):
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setFormatter(formatter)
        LOG.addHandler(console_handler)
    LOG.propagate = False
    LOG.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    return LOG_FILE_PATH


def log_file_label() -> str:
    """Return a non-identifying display label even when tests relocate the log file."""
    try:
        return LOG_FILE_PATH.relative_to(Path(__file__).resolve().parent).as_posix()
    except ValueError:
        return LOG_FILE_PATH.name


def log_ring() -> RingBufferHandler:
    """Return the process-wide ring handler, installing logging on first use."""
    if not any(isinstance(handler, RingBufferHandler) for handler in LOG.handlers):
        setup_logging("INFO", console=False)
    for handler in LOG.handlers:
        if isinstance(handler, RingBufferHandler):
            return handler
    raise RuntimeError("ring log handler missing")  # pragma: no cover - unreachable


def set_current_request_id(rid: str) -> None:
    _REQ_CONTEXT.request_id = rid


def current_request_id() -> str:
    return str(getattr(_REQ_CONTEXT, "request_id", "") or "")


def log_event(state: str, level: int = logging.INFO, **fields: Any) -> None:
    """Emit one structured JSON event line (keeps the historical {"state": ...} shape)."""
    payload: dict[str, Any] = {"state": state, **fields}
    rid = current_request_id()
    if rid:
        payload = {"rid": rid, **payload}
    LOG.log(level, json.dumps(payload, ensure_ascii=False, default=str))


def _percentile(values: Iterable[int | float], percentile: float) -> int:
    ordered = sorted(max(0, int(value)) for value in values)
    if not ordered:
        return 0
    position = max(0, min(len(ordered) - 1, round((len(ordered) - 1) * percentile)))
    return ordered[position]


def _metric_path(path: str) -> str:
    """Normalize dynamic local paths so metrics never expose response identifiers."""
    value = str(path or "/")
    for prefix in ("/v1/responses/", "/responses/"):
        if value.startswith(prefix):
            return prefix + ":id"
    return value


class RuntimeMetrics:
    """Small in-memory operational counters; content and account identifiers are excluded."""

    def __init__(self) -> None:
        self.started_at_ms = int(time.time() * 1000)
        self.started_mono = time.monotonic()
        self._lock = threading.Lock()
        self._requests_total = 0
        self._status = Counter()
        self._methods = Counter()
        self._paths = Counter()
        self._samples: deque[tuple[float, int, int]] = deque(maxlen=4096)

    def record_http(self, method: str, path: str, status: int, duration_ms: int) -> None:
        status = max(0, int(status or 0))
        duration_ms = max(0, int(duration_ms or 0))
        with self._lock:
            self._requests_total += 1
            self._status[str(status)] += 1
            self._methods[str(method or "UNKNOWN").upper()] += 1
            self._paths[_metric_path(path)] += 1
            self._samples.append((time.monotonic(), status, duration_ms))

    def snapshot(self) -> dict[str, Any]:
        now_mono = time.monotonic()
        with self._lock:
            requests_total = self._requests_total
            status = Counter(self._status)
            methods = Counter(self._methods)
            paths = Counter(self._paths)
            samples = list(self._samples)
        durations = [item[2] for item in samples]
        recent = [item for item in samples if now_mono - item[0] <= 300]
        status_4xx = sum(count for code, count in status.items() if code.startswith("4"))
        status_5xx = sum(count for code, count in status.items() if code.startswith("5"))
        recent_errors = sum(1 for _at, code, _duration in recent if code >= 500)
        return {
            "started_at": self.started_at_ms,
            "uptime_seconds": max(0, int(now_mono - self.started_mono)),
            "requests_total": requests_total,
            "status_4xx": status_4xx,
            "status_5xx": status_5xx,
            "error_rate": round(status_5xx / requests_total, 4) if requests_total else 0.0,
            "avg_duration_ms": round(sum(durations) / len(durations)) if durations else 0,
            "p50_duration_ms": _percentile(durations, 0.50),
            "p95_duration_ms": _percentile(durations, 0.95),
            "requests_5m": len(recent),
            "errors_5m": recent_errors,
            "methods": dict(methods),
            "top_paths": [{"path": name, "count": count} for name, count in paths.most_common(8)],
        }


RUNTIME_METRICS = RuntimeMetrics()


BASE_URL = "https://chat.z.ai"
DEFAULT_MODEL = "glm-5.3"
# x-preview-l is the upstream model ID behind the GLM-5.3-Flash UI entry.
FLASH_MODEL = "x-preview-l"
# GLM-5-Turbo is the official daily/agent model that succeeded GLM-5.1 on the
# site picker (2026-08 official list: GLM-5.3-Flash / GLM-5.3 / GLM-5.2 / GLM-5-Turbo).
# Upstream IDs are case-sensitive here: "GLM-5-Turbo" works, "glm-5-turbo" 500s.
TURBO_MODEL = "GLM-5-Turbo"
SUPPORTED_MODELS = ("glm-5.3", "x-preview-l", "GLM-5-Turbo", "glm-5.2")
# Only these IDs are advertised to local model discovery/UI clients.  The
# suffix is a local routing hint and is removed before the request reaches Z.ai.
FORCE_HISTORY_MODEL_SUFFIX = "-forcehistory"
NO_THINKING_MODEL_SUFFIX = "-nothinking"
ADVERTISED_MODELS = (
    "glm-5.3",
    "x-preview-l",
    "GLM-5-Turbo",
    "glm-5.2",
    "glm-5.3-forcehistory",
    "x-preview-l-forcehistory",
    "GLM-5-Turbo-forcehistory",
    "glm-5.2-forcehistory",
)
MODEL = DEFAULT_MODEL
FE_VERSION = "prod-fe-1.1.92"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)
# Page <title> observed on every chat/completions request in both 2026-08 HARs;
# it mirrors the site banner (GLM-5.3-Flash branding) regardless of routed model.
PAGE_TITLE = "Z.ai - Advanced AI Chatbot & Agent powered by GLM-5.3-Flash"
REGION = "overseas"
WEB_INDEX_PATH = Path(__file__).with_name("web") / "index.html"
PROFILE_STORE_PATH = Path(__file__).with_name("profiles.local.json")
SETTINGS_STORE_PATH = Path(__file__).with_name("settings.local.json")
DEFAULT_AUTO_WEB_SEARCH = False
DEFAULT_ENABLE_THINKING = True
DEFAULT_REASONING_EFFORT = "max"
DEFAULT_DELETE_CHAT_AFTER_COMPLETION = True
UPSTREAM_STREAM_TIMEOUT_SEC = 300  # 上游流式响应“无数据间隔”超时（秒）
# 上游繁忙（HAR 实测 MODEL_CONCURRENCY_LIMIT）默认等待后重试；可在面板设置调整。
DEFAULT_UPSTREAM_RETRY_WAIT_SEC = 3.0
DEFAULT_UPSTREAM_RETRY_ATTEMPTS = 3
# 瞬时类上游错误：回答尚未开始时可安全换会话重试（内容/鉴权/验证码类错误不在此列）。
TRANSIENT_UPSTREAM_ERROR_PATTERNS = (
    "MODEL_CONCURRENCY_LIMIT",  # HAR 实测：{"code":"MODEL_CONCURRENCY_LIMIT","detail":"当前模型使用人数较多..."}
    "MODEL_BUSY",
    "RATE_LIMIT",
    "SERVER_BUSY",
    "当前模型使用人数较多",
    "请稍后再试",
    "上游繁忙",
    "系统繁忙",
)
SUPPORTED_REASONING_EFFORTS = ("low", "high", "max")
MAX_HAR_UPLOAD_BYTES = 512 * 1024 * 1024
MAX_CHAT_FILE_UPLOAD_BYTES = 128 * 1024 * 1024
MAX_JSON_BODY_BYTES = 2 * 1024 * 1024
MAX_SSE_BUFFER_BYTES = 2 * 1024 * 1024
# Buffered tool/reasoning streams can be quiet from the client's perspective
# even while upstream is still producing hidden semantic deltas.  SSE comments
# are protocol-safe and keep SDK/proxy idle timers from tearing down the turn.
SSE_KEEPALIVE_INTERVAL_SECONDS = 10.0
UPLOAD_STREAM_CHUNK_BYTES = 1024 * 1024
MAX_CONTEXT_FILE_BYTES = 4 * 1024 * 1024
# Per-account concurrency cap for in-flight upstream generations (Z.ai 429s beyond this).
MAX_CONCURRENT_GENERATIONS_PER_PROFILE = 3
RESPONSE_STORE_TTL_SECONDS = 60 * 60
MAX_RESPONSE_STORE_ITEMS = 128
PROFILE_STORE_LOCK = threading.RLock()
SETTINGS_STORE_LOCK = threading.RLock()
API_KEY_STORE_LOCK = threading.RLock()
API_KEY_ENV_NAME = "GLM2API_API_KEY"
API_KEY_STORE_PATH = Path(__file__).with_name("apikey.local.json")

# Recovered from the production front-end bundle in chat.z.ai.har.
SIGNING_SEED = "key-@@@@)))()((9))-xxxx&&&%%%%%"


def sha16(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def load_har(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8", errors="replace") as f:
        return json.load(f)


def file_sha16(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def is_loopback_host(host: str) -> bool:
    value = str(host or "").strip().strip("[]")
    if value.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def validate_server_bind(host: str, allow_remote: bool, api_key: str) -> None:
    """Reject accidental or unauthenticated exposure of the account-backed proxy."""
    if is_loopback_host(host):
        return
    if not allow_remote:
        raise RuntimeError("--host 不是本机回环地址；如确实需要局域网访问，请同时显式传入 --allow-remote")
    if not str(api_key or "").strip():
        raise RuntimeError("非回环地址必须配置 API Key；请使用 --api-key 或 GLM2API_API_KEY")


def atomic_write_text(path: Path, text: str, *, durable: bool = True) -> None:
    """Write a small local-state file without exposing a partial JSON document."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as f:
            tmp_name = f.name
            f.write(text)
            f.flush()
            if durable:
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
        os.replace(tmp_name, path)
        tmp_name = ""
    finally:
        if tmp_name:
            Path(tmp_name).unlink(missing_ok=True)


def decode_har_text(content: dict[str, Any]) -> str:
    text = content.get("text", "") or ""
    if content.get("encoding") == "base64":
        try:
            return base64.b64decode(text).decode("utf-8", errors="replace")
        except Exception:
            return text
    return text


def json_or_none(text: str) -> Any:
    try:
        return json.loads(text)
    except Exception:
        return None


def entry_url_path(entry: dict[str, Any]) -> str:
    return urlsplit(entry.get("request", {}).get("url", "")).path


def header_value(headers: list[dict[str, str]], name: str, default: str = "") -> str:
    lname = name.lower()
    for item in headers:
        if item.get("name", "").lower() == lname:
            return item.get("value", "")
    return default


def response_json(entry: dict[str, Any]) -> Any:
    text = decode_har_text(entry.get("response", {}).get("content", {}))
    return json_or_none(text)


def request_json(entry: dict[str, Any]) -> Any:
    text = (entry.get("request", {}).get("postData") or {}).get("text", "") or ""
    return json_or_none(text)


def now_ms() -> str:
    return str(int(time.time() * 1000))


def browser_variables(user_name: str = "user", language: str = "zh-CN") -> dict[str, str]:
    local = datetime.now().astimezone()
    return {
        "{{USER_NAME}}": user_name or "user",
        "{{USER_LOCATION}}": "Unknown",
        "{{CURRENT_DATETIME}}": local.strftime("%Y-%m-%d %H:%M:%S"),
        "{{CURRENT_DATE}}": local.strftime("%Y-%m-%d"),
        "{{CURRENT_TIME}}": local.strftime("%H:%M:%S"),
        "{{CURRENT_WEEKDAY}}": local.strftime("%A"),
        "{{CURRENT_TIMEZONE}}": local.tzname() or "Asia/Shanghai",
        "{{USER_LANGUAGE}}": language,
    }


def prompt_b64(prompt: str) -> str:
    return base64.b64encode(prompt.encode("utf-8")).decode("ascii")


def sorted_payload(timestamp: str, request_id: str, user_id: str) -> str:
    # JS: Object.entries({timestamp, requestId, user_id})
    #       .sort((a,b)=>a[0].localeCompare(b[0])).join(",")
    items = {
        "timestamp": timestamp,
        "requestId": request_id,
        "user_id": user_id,
    }
    return ",".join(f"{key},{value}" for key, value in sorted(items.items()))


def z_sign(prompt: str, timestamp: str, request_id: str, user_id: str) -> str:
    payload = sorted_payload(timestamp, request_id, user_id)
    message = f"{payload}|{prompt_b64(prompt)}|{timestamp}"
    bucket = str(int(int(timestamp) / (5 * 60 * 1000)))
    derived = hmac.new(SIGNING_SEED.encode(), bucket.encode(), hashlib.sha256).hexdigest()
    return hmac.new(derived.encode(), message.encode(), hashlib.sha256).hexdigest()


def default_chrome_path() -> str | None:
    candidates = [
        os.environ.get("CHROME_PATH"),
        os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        shutil.which("chrome"),
        shutil.which("chrome.exe"),
        shutil.which("msedge"),
        shutil.which("msedge.exe"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return str(candidate)
    return None


def describe_captcha_verify_param(value: str) -> dict[str, Any]:
    info: dict[str, Any] = {
        "captcha_len": len(value),
        "captcha_fp": sha16(value) if value else "",
    }
    if not value:
        return info
    try:
        raw = base64.b64decode(value).decode("utf-8", errors="replace")
        obj = json.loads(raw)
        if isinstance(obj, dict):
            info.update(
                {
                    "decoded_keys": sorted(obj.keys()),
                    "sceneId": obj.get("sceneId"),
                    "certifyId_len": len(str(obj.get("certifyId", ""))),
                    "securityToken_len": len(str(obj.get("securityToken", ""))),
                    "isSign": obj.get("isSign"),
                }
            )
    except Exception as exc:
        info["decode_error"] = str(exc)[:120]
    return info


_CAPTCHA_PLAYWRIGHT: Callable[..., Any] | None = None


def _captcha_playwright() -> Callable[..., Any]:
    """Lazily import the Playwright sync API (heavy import; browser paths only)."""
    global _CAPTCHA_PLAYWRIGHT
    if _CAPTCHA_PLAYWRIGHT is None:
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:  # pragma: no cover - depends on local toolchain
            raise RuntimeError("Playwright Python package is unavailable; install/playwright-enable it first") from exc
        _CAPTCHA_PLAYWRIGHT = sync_playwright
    return _CAPTCHA_PLAYWRIGHT


CAPTCHA_EVALUATE_SCRIPT = """async (timeoutMs) => {
                  const scriptUrl = "https://o.alicdn.com/captcha-frontend/aliyunCaptcha/AliyunCaptcha.js";
                  const sceneId = location.hostname === "chat.z.ai" ? "didk33e0" : "xswyjefn";
                  window.AliyunCaptchaConfig = {region: "sgp", prefix: "no8xfe"};
                  if (!window.initAliyunCaptcha) {
                    await new Promise((resolve, reject) => {
                      let s = document.querySelector(`script[src="${scriptUrl}"]`);
                      if (s) {
                        s.addEventListener("load", resolve, {once: true});
                        s.addEventListener("error", reject, {once: true});
                        return;
                      }
                      s = document.createElement("script");
                      s.src = scriptUrl;
                      s.onload = resolve;
                      s.onerror = reject;
                      document.head.appendChild(s);
                    });
                  }

                  let element = document.getElementById("codex-chat-captcha-element");
                  if (!element) {
                    element = document.createElement("div");
                    element.id = "codex-chat-captcha-element";
                    element.style.cssText = "position:absolute;left:-99999px;top:-99999px;width:0;height:0;overflow:hidden;pointer-events:none;";
                    document.body.appendChild(element);
                  }
                  let button = document.getElementById("codex-chat-captcha-trigger");
                  if (!button) {
                    button = document.createElement("button");
                    button.id = "codex-chat-captcha-trigger";
                    button.type = "button";
                    button.setAttribute("aria-hidden", "true");
                    button.tabIndex = -1;
                    button.style.cssText = "position:absolute;left:-99999px;top:-99999px;width:1px;height:1px;opacity:0;";
                    document.body.appendChild(button);
                  }

                  const lang = {
                    cn: {
                      START_VERIFY: "点击开始验证",
                      POPUP_TITLE: "请完成安全验证",
                      SLIDE_TIP: "请按住滑块，拖动到最右边",
                      CHECK_BOX_TIP: "确认您不是机器人",
                      PUZZLE_TIP: "请拖动滑块完成拼图",
                      INPAINTING_TIP: "请拖动滑块还原完整图片",
                      VERIFYING: "验证中...",
                      SUCCESS: "滑动成功!",
                      SLIDE_FAIL: "验证失败，请刷新重试",
                      CAPTCHA_FAIL: "验证失败，请重试!",
                      CONGESTION: "前方拥堵，请刷新重试",
                      CAPTCHA_COMPLETED: "滑动完成",
                      FINISH_CAPTCHA: "请先完成验证！"
                    }
                  };

                  return await new Promise((resolve, reject) => {
                    let inst = null;
                    const timer = setTimeout(() => reject(new Error("captcha timeout")), timeoutMs);
                    window.initAliyunCaptcha({
                      SceneId: sceneId,
                      mode: "popup",
                      element: "#codex-chat-captcha-element",
                      button: "#codex-chat-captcha-trigger",
                      captchaLogoImg: "https://z-cdn.chatglm.cn/z-ai/static/logo.svg",
                      upLang: lang,
                      language: "cn",
                      timeout: 10000,
                      delayBeforeSuccess: false,
                      success: (v) => {
                        clearTimeout(timer);
                        try { inst && inst.refresh && inst.refresh(); } catch (e) {}
                        resolve(v);
                      },
                      fail: () => setTimeout(() => button.click(), 300),
                      onError: (e) => {
                        clearTimeout(timer);
                        reject(new Error(typeof e === "string" ? e : JSON.stringify(e || {})));
                      },
                      onClose: () => {
                        clearTimeout(timer);
                        reject(new Error("captcha closed"));
                      },
                      getInstance: (i) => {
                        inst = i;
                        setTimeout(() => button.click(), 50);
                      }
                    });
                  });
                }"""


def _launch_captcha_page(
    pw: Any,
    state: "HarState",
    headless: bool,
    chrome_path: str | None,
    timeout_ms: int,
    selected_model: str,
) -> tuple[Any, Any, Any]:
    """Launch a preloaded chat.z.ai page with the account token and telemetry."""
    executable_path = chrome_path or default_chrome_path()
    launch_args = ["--disable-blink-features=AutomationControlled", "--no-sandbox"]
    user_agent = state.user_agent or DEFAULT_USER_AGENT
    launch_kwargs: dict[str, Any] = {"headless": headless, "args": launch_args}
    if executable_path:
        launch_kwargs["executable_path"] = executable_path
    browser = pw.chromium.launch(**launch_kwargs)
    try:
        context = browser.new_context(
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            user_agent=user_agent,
            viewport={"width": int(state.viewport_width or "1152"), "height": int(state.viewport_height or "932")},
            device_scale_factor=float(state.pixel_ratio or "1.5"),
        )
        page = context.new_page()
        page.goto(BASE_URL + "/", wait_until="domcontentloaded", timeout=timeout_ms)
        page.evaluate(
            """(args) => {
              localStorage.setItem("token", args.token);
              if (args.device) localStorage.setItem("_arms_uid", args.device);
              localStorage.setItem("selectedModels", JSON.stringify([args.model]));
              localStorage.setItem("last_mode", "default");
            }""",
            {"token": state.token, "device": state.device_id, "model": selected_model},
        )
        page.reload(wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(1_500)
        return browser, context, page
    except Exception:
        try:
            browser.close()
        except Exception:
            pass
        raise


def solve_captcha_on_page(page: Any, timeout_ms: int) -> str:
    """Run AliyunCaptcha on an existing preloaded page; return captcha_verify_param."""
    result = page.evaluate(CAPTCHA_EVALUATE_SCRIPT, timeout_ms)
    if not isinstance(result, str) or not result:
        raise RuntimeError(f"captcha success returned unexpected value: {type(result).__name__}")
    return result


def get_browser_captcha(
    state: "HarState",
    chrome_path: str | None = None,
    headless: bool = True,
    timeout_ms: int = 75_000,
    selected_model: str = DEFAULT_MODEL,
) -> str:
    """Single-shot fallback: open a browser, solve once, close it immediately."""
    pw = _captcha_playwright()()
    try:
        # sync_playwright() returns a context manager; start() returns the
        # actual Playwright instance that owns .chromium / .stop().
        pw = pw.start()
        browser, _context, page = _launch_captcha_page(
            pw,
            state,
            headless=headless,
            chrome_path=chrome_path,
            timeout_ms=timeout_ms,
            selected_model=selected_model,
        )
        try:
            return solve_captcha_on_page(page, timeout_ms)
        finally:
            browser.close()
    finally:
        pw.stop()


class CaptchaWorker:
    """Reuse one headless browser/page for AliyunCaptcha across completions.

    Playwright's sync API is thread-affine, so a single daemon thread owns the
    browser. Completion threads enqueue (request_id, state, model) and wait for
    the matching result. On failure the page/browser is rebuilt once and the
    request is retried before giving up. After `idle_timeout_sec` without work
    the browser shuts down to free memory; the next request lazily restarts it.
    """

    def __init__(
        self,
        chrome_path: str | None = None,
        headless: bool = True,
        default_timeout_ms: int = 75_000,
        idle_timeout_sec: float = 900.0,
    ) -> None:
        self.chrome_path = chrome_path
        self.headless = headless
        self.default_timeout_ms = default_timeout_ms
        self.idle_timeout_sec = idle_timeout_sec
        self._requests: queue.Queue[tuple[str, "HarState", str]] = queue.Queue()
        self._results: queue.Queue[tuple[str, object]] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()
        self._closed = False
        self._last_used = time.time()

    def start(self) -> None:
        with self._start_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            while True:  # 丢弃上次会话残留的过期请求
                try:
                    self._requests.get_nowait()
                except queue.Empty:
                    break
            self._closed = False
            self._last_used = time.time()
            self._thread = threading.Thread(
                target=self._run,
                name="glm2api-captcha-worker",
                daemon=True,
            )
            self._thread.start()

    def solve(self, state: "HarState", selected_model: str, timeout_ms: int | None = None) -> str:
        self.start()
        request_id = uuid.uuid4().hex
        timeout_ms = max(10_000, int(timeout_ms or self.default_timeout_ms))
        self._requests.put((request_id, state, selected_model))
        self._last_used = time.time()
        deadline = time.time() + timeout_ms / 1000
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise TimeoutError(f"captcha worker timed out after {timeout_ms / 1000:.1f}s")
            try:
                result_id, value = self._results.get(timeout=min(0.5, max(0.05, remaining)))
            except queue.Empty:
                continue
            if result_id != request_id:
                continue
            if isinstance(value, Exception):
                raise value
            return str(value)

    def close(self) -> None:
        with self._start_lock:
            self._closed = True
            self._requests.put(("__close__", None, ""))  # type: ignore[arg-type]
            thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=8)

    def _run(self) -> None:
        browser = None
        page = None
        current_token = ""
        current_device = ""
        pw = None
        try:
            pw = _captcha_playwright()().start()
            while not self._closed:
                try:
                    request_id, state, selected_model = self._requests.get(timeout=1.0)
                except queue.Empty:
                    if self.idle_timeout_sec and time.time() - self._last_used > self.idle_timeout_sec:
                        break
                    continue
                if request_id == "__close__":
                    break
                if state is None:
                    continue
                self._last_used = time.time()
                try:
                    browser, page = self._ensure_page(pw, browser, page, state, current_token, current_device, selected_model)
                    current_token = state.token
                    current_device = state.device_id
                    value = solve_captcha_on_page(page, self.default_timeout_ms)
                    self._results.put((request_id, value))
                except Exception as exc:
                    self._teardown_browser(browser)
                    browser = None
                    page = None
                    current_token = ""
                    current_device = ""
                    try:
                        browser, page = self._ensure_page(pw, browser, page, state, current_token, current_device, selected_model)
                        current_token = state.token
                        current_device = state.device_id
                        value = solve_captcha_on_page(page, self.default_timeout_ms)
                        self._results.put((request_id, value))
                    except Exception as retry_exc:
                        self._teardown_browser(browser)
                        browser = None
                        page = None
                        current_token = ""
                        current_device = ""
                        self._results.put((request_id, retry_exc))
        finally:
            self._teardown_browser(browser)
            if pw is not None:
                try:
                    pw.stop()
                except Exception:
                    pass

    def _ensure_page(
        self,
        pw: Any,
        browser: Any,
        page: Any,
        state: "HarState",
        current_token: str,
        current_device: str,
        selected_model: str,
    ) -> tuple[Any, Any]:
        if page is not None and state.token == current_token and state.device_id == current_device:
            return browser, page
        if browser is not None:
            self._teardown_browser(browser)
        new_browser, _context, new_page = _launch_captcha_page(
            pw,
            state,
            headless=self.headless,
            chrome_path=self.chrome_path,
            timeout_ms=self.default_timeout_ms,
            selected_model=selected_model,
        )
        return new_browser, new_page

    @staticmethod
    def _teardown_browser(browser: Any) -> None:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass


# 全局常驻验证码浏览器；serve 模式由 main() 创建并负责关闭，CLI 单发/测试保持 None。
_CAPTCHA_WORKER: CaptchaWorker | None = None
_CAPTCHA_DEGRADED_UNTIL = 0.0
_CAPTCHA_DEGRADED_LOCK = threading.RLock()
CAPTCHA_DEGRADED_COOLDOWN_SECONDS = 1800
CAPTCHA_RETRY_BACKOFF_SECONDS = 20

# happy-dom (Node) AliyunCaptcha solver: an in-process DOM mock that solves the
# TRACELESS challenge in ~5-10s without a real browser. Headless Chromium gets
# challenged/looped by upstream far more often, so auto mode prefers this path
# and keeps the Playwright worker as fallback.
HAPPYDOM_CAPTCHA_SCRIPT = Path(__file__).with_name("captcha_happy.mjs")
_HAPPYDOM_AVAILABLE: bool | None = None
_CAPTCHA_MODE = "auto"


def happydom_captcha_available() -> bool:
    global _HAPPYDOM_AVAILABLE
    if _HAPPYDOM_AVAILABLE is None:
        _HAPPYDOM_AVAILABLE = HAPPYDOM_CAPTCHA_SCRIPT.exists() and shutil.which("node") is not None
    return _HAPPYDOM_AVAILABLE


def get_happydom_captcha(timeout_ms: int = 75_000) -> str:
    """Solve the AliyunCaptcha challenge via the local happy-dom Node script.

    Returns "" when Node/the script is missing or the solve fails; callers fall
    back to the Playwright browser worker.
    """
    node = shutil.which("node")
    if not node or not HAPPYDOM_CAPTCHA_SCRIPT.exists():
        return ""
    try:
        proc = subprocess.run(
            [node, str(HAPPYDOM_CAPTCHA_SCRIPT), "--timeout-ms", str(max(timeout_ms, 30_000))],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(timeout_ms / 1000 + 15, 30),
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    lines = (proc.stdout or "").strip().splitlines()
    payload = json_or_none(lines[-1]) if lines else None
    if isinstance(payload, dict) and payload.get("ok") and payload.get("captcha"):
        return str(payload["captcha"])
    return ""


def _set_captcha_degraded(seconds: float) -> None:
    """After a fresh-captcha failure, skip the slow browser retry for a while.

    ``seconds <= 0`` clears the cooldown (used by tests and after a manual
    re-authorization so the next request can try the browser flow again).
    """
    global _CAPTCHA_DEGRADED_UNTIL
    with _CAPTCHA_DEGRADED_LOCK:
        if seconds <= 0:
            _CAPTCHA_DEGRADED_UNTIL = 0.0
        else:
            _CAPTCHA_DEGRADED_UNTIL = max(_CAPTCHA_DEGRADED_UNTIL, time.time() + seconds)


def resolve_fresh_captcha(
    state: "HarState",
    selected_model: str,
    worker: CaptchaWorker | None,
    timeout_ms: int,
    chrome_path: str | None = None,
    headless: bool = True,
    force_fresh: bool = False,
) -> str:
    """Return a captcha_verify_param for fresh-captcha mode with fallback.

    The headless AliyunCaptcha flow can fail (page without a solvable
    challenge, browser issue, network) and each attempt blocks the request for
    up to ``timeout_ms``. On failure we record a cooldown and fall back to the
    captcha stored in the account profile (e.g. obtained through the manual
    browser login in the web panel). That keeps requests working right after a
    re-authorization instead of timing out on every turn.

    ``force_fresh``：上游刚以 F018/F019 拒绝过验证码时使用。池中的预热码与
    降级路径的 profile 存码大概率同样超龄，必须绕开一切缓存捷径现场重解。
    """
    if not force_fresh:
        pooled = _captcha_pool_take()
        if pooled:
            log_event("fresh_captcha_pool_hit")
            _schedule_captcha_prefetch(timeout_ms)  # 取走即补货，维持池深度
            return pooled
    with _CAPTCHA_DEGRADED_LOCK:
        degraded = time.time() < _CAPTCHA_DEGRADED_UNTIL
    stored = state.captcha_verify_param
    if force_fresh:
        degraded = False
        stored = ""
    if degraded and stored:
        log_event("fresh_captcha_degraded_skip", has_stored_captcha=bool(stored))
        return stored
    if degraded and not stored:
        # Without a stored captcha the request would fail upstream with
        # FRONTEND_CAPTCHA_REQUIRED anyway, so the cooldown must not suppress
        # the browser attempt; challenge issuance is intermittent and a spaced
        # retry typically succeeds.
        log_event("fresh_captcha_cooldown_retry")
        _set_captcha_degraded(0)
    last_exc: Exception | None = None
    started = time.time()
    want_happydom = _CAPTCHA_MODE in ("auto", "happydom") and happydom_captcha_available()
    want_browser = _CAPTCHA_MODE in ("auto", "browser")
    for attempt in range(2):
        if attempt:
            log_event("fresh_captcha_retry", after_sec=CAPTCHA_RETRY_BACKOFF_SECONDS)
            time.sleep(CAPTCHA_RETRY_BACKOFF_SECONDS)
        captcha = ""
        if want_happydom:
            captcha = get_happydom_captcha(timeout_ms)
            backend = "happydom"
        if not captcha and want_browser:
            try:
                captcha = (
                    worker.solve(state, selected_model, timeout_ms=timeout_ms)
                    if worker is not None
                    else get_browser_captcha(
                        state,
                        chrome_path=chrome_path,
                        headless=headless,
                        timeout_ms=timeout_ms,
                        selected_model=selected_model,
                    )
                )
                backend = "browser"
            except Exception as exc:
                last_exc = exc
        if captcha:
            _set_captcha_degraded(0)
            _schedule_captcha_prefetch(timeout_ms)  # 为下一次请求预解，摊薄求解延迟
            log_event(
                "fresh_captcha_solved",
                backend=backend,
                attempt=attempt + 1,
                elapsed_ms=int((time.time() - started) * 1000),
            )
            return captcha
        if _CAPTCHA_MODE == "happydom":
            break
    fallback = state.captcha_verify_param
    _set_captcha_degraded(CAPTCHA_DEGRADED_COOLDOWN_SECONDS)
    log_event(
        "fresh_captcha_fallback",
        error_type=type(last_exc).__name__ if last_exc else "empty",
        error=str(last_exc)[:300] if last_exc else "captcha worker returned empty result",
        has_stored_captcha=bool(fallback),
    )
    if fallback:
        return fallback
    raise RuntimeError(
        "验证码获取失败：请先在网页面板重新完成浏览器授权（登录并切换账号），保存后再重试。"
    ) from last_exc


def get_browser_login_state(
    chrome_path: str | None = None,
    timeout_ms: int = 300_000,
    progress_cb: Callable[[str], None] | None = None,
) -> HarState:
    """Open a real browser for manual login and collect post-login state.

    The local web UI never receives the password. The user completes login and
    any official CAPTCHA in the opened chat.z.ai browser window. After a
    non-guest token is observed, we read only the session token and browser
    telemetry required by the existing completion client.
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # pragma: no cover - depends on local toolchain
        raise RuntimeError("Playwright Python package is unavailable; install/playwright-enable it first") from exc

    executable_path = chrome_path or default_chrome_path()
    launch_args = ["--disable-blink-features=AutomationControlled", "--no-sandbox"]
    deadline = time.time() + timeout_ms / 1000

    def report(stage: str) -> None:
        log_event("browser_login_progress", stage=stage)
        if progress_cb is not None:
            try:
                progress_cb(stage)
            except Exception:
                pass

    with sync_playwright() as p:
        launch_kwargs: dict[str, Any] = {"headless": False, "args": launch_args}
        if executable_path:
            launch_kwargs["executable_path"] = executable_path
        browser = p.chromium.launch(**launch_kwargs)
        try:
            context = browser.new_context(locale="zh-CN", timezone_id="Asia/Shanghai")
            page = context.new_page()
            page.goto(BASE_URL + "/auth", wait_until="domcontentloaded", timeout=60_000)
            report("授权浏览器已打开，请在窗口中完成登录")

            captured_token: dict[str, str] = {"value": ""}
            initial_token = ""
            try:
                initial_token = str(page.evaluate("() => localStorage.getItem('token') || ''") or "")
            except Exception:
                pass

            def _jwt_claims(tok: str) -> dict[str, Any]:
                try:
                    part = tok.split(".")[1]
                    raw = base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))
                    data = json.loads(raw)
                    return data if isinstance(data, dict) else {}
                except Exception:
                    return {}

            def _is_guest_token(tok: str) -> bool:
                claims = _jwt_claims(tok)
                email = str(claims.get("email") or "")
                role = str(claims.get("role") or "")
                return bool(role == "guest" or email.lower().endswith("@guest.com"))

            def _looks_real(tok: str) -> bool:
                return bool(tok) and tok != initial_token and not _is_guest_token(tok)

            def log_login_event(kind: str, detail: str) -> None:
                try:
                    log_event("browser_login_debug", event=kind, detail=str(detail)[:300])
                except Exception:
                    pass

            def on_request(request: Any) -> None:
                try:
                    url = str(request.url)
                    if "chat.z.ai" in url and "/api/" in url:
                        auth = str(request.headers.get("authorization") or "")
                        tok = ""
                        if auth.lower().startswith("bearer "):
                            tok = auth[7:].strip()
                        log_login_event(
                            "request",
                            f"{request.method} {url[:140]} bearer={bool(tok)} fp={sha16(tok)[:8] if tok else '-'}",
                        )
                        if _looks_real(tok):
                            captured_token["value"] = tok
                            log_login_event("token_captured", f"from request fp={sha16(tok)[:8]}")
                except Exception:
                    pass

            def on_response(response: Any) -> None:
                try:
                    url = str(response.url)
                    if url.rstrip("/").endswith("/api/v1/auths/signin"):
                        try:
                            data = response.json()
                        except Exception:
                            data = None
                        if isinstance(data, dict):
                            tok = str(data.get("token") or "")
                            if _looks_real(tok):
                                captured_token["value"] = tok
                                log_login_event("token_captured", f"from signin response fp={sha16(tok)[:8]}")
                            if not response.ok:
                                detail = str(data.get("detail") or data.get("message") or "")
                                log_login_event("signin_failed", f"status={response.status} detail={detail[:160]}")
                                if detail:
                                    report("登录未成功：" + detail + "（可修改后重试，窗口保持打开）")
                except Exception:
                    pass

            def attach_page_hooks(pg: Any) -> None:
                try:
                    pg.on("close", lambda: log_login_event("page_closed", "page target closed"))
                    pg.on("framenavigated", lambda fr: log_login_event("navigated", fr.url[:220]))
                except Exception:
                    pass

            attach_page_hooks(page)
            try:
                context.on("page", lambda pg: (attach_page_hooks(pg), log_login_event("page_opened", pg.url[:220])))
            except Exception:
                pass
            context.on("request", on_request)
            context.on("response", on_response)

            last_error = ""
            last_capture: dict[str, Any] = {}
            closed_since: float | None = None

            def _finish_login(real_token: str, real_auth: dict[str, Any], capture: dict[str, Any]) -> HarState:
                telemetry = capture.get("telemetry") or {}
                report("登录成功，正在保存账号")
                return HarState(
                    token=real_token,
                    user_id=str(real_auth.get("id") or "unknown"),
                    user_name=str(real_auth.get("name") or real_auth.get("email") or "browser-user"),
                    device_id=str(capture.get("device") or ""),
                    captcha_verify_param="",
                    user_agent=str(telemetry.get("user_agent") or "Mozilla/5.0"),
                    language=str(telemetry.get("language") or "zh-CN"),
                    languages=str(telemetry.get("languages") or "zh-CN,zh"),
                    screen_width=str(telemetry.get("screen_width") or "1707"),
                    screen_height=str(telemetry.get("screen_height") or "1067"),
                    viewport_width=str(telemetry.get("viewport_width") or "1152"),
                    viewport_height=str(telemetry.get("viewport_height") or "932"),
                    pixel_ratio=str(telemetry.get("pixel_ratio") or "1.5"),
                    color_depth=str(telemetry.get("color_depth") or "24"),
                    browser_name=str(telemetry.get("browser_name") or "Chrome"),
                    os_name=str(telemetry.get("os_name") or "Windows"),
                    chat_id="",
                    fe_version=FE_VERSION,
                    region=REGION,
                    sec_ch_ua=str(telemetry.get("sec_ch_ua") or ""),
                    sec_ch_ua_mobile=str(telemetry.get("sec_ch_ua_mobile") or "?0"),
                    sec_ch_ua_platform=str(telemetry.get("sec_ch_ua_platform") or '"Windows"'),
                )

            while time.time() < deadline:
                # 网络层抓到的真实 token 优先：页面跳转/替换/关闭都不影响已捕获结果
                if captured_token["value"]:
                    claims = _jwt_claims(captured_token["value"])
                    real_auth = {
                        "id": str(claims.get("id") or claims.get("sub") or claims.get("user_id") or ""),
                        "name": str(claims.get("name") or claims.get("email") or "browser-user"),
                        "email": str(claims.get("email") or ""),
                    }
                    log_login_event("login_detected", f"network token fp={sha16(captured_token['value'])[:8]}")
                    return _finish_login(captured_token["value"], real_auth, last_capture)

                try:
                    if page.is_closed():
                        alive = [p for p in context.pages if not p.is_closed()]
                        if alive:
                            page = alive[-1]
                            closed_since = None
                            attach_page_hooks(page)
                            report("登录窗口已切换页面，继续等待登录")
                        else:
                            if closed_since is None:
                                closed_since = time.time()
                            if time.time() - closed_since >= 5:
                                raise RuntimeError("授权浏览器窗口已关闭，登录流程未完成，请重新点击“浏览器登录”。")
                            last_error = "window closed; waiting for replacement page"
                            try:
                                page.wait_for_timeout(1_000)
                            except Exception:
                                pass
                            continue
                except RuntimeError:
                    raise
                except Exception:
                    pass
                try:
                    capture = page.evaluate(
                        """async () => {
                          const token = localStorage.getItem("token") || "";
                          let device = localStorage.getItem("_arms_uid") || "";
                          if (!device || !/^uid_[A-Za-z0-9]{7,}$/.test(device)) {
                            device = "uid_" + Math.random().toString(36).slice(2, 15);
                            localStorage.setItem("_arms_uid", device);
                          }
                          const telemetry = {
                            user_agent: navigator.userAgent || "Mozilla/5.0",
                            language: navigator.language || "zh-CN",
                            languages: (navigator.languages || [navigator.language || "zh-CN"]).join(","),
                            screen_width: String(screen.width || 1707),
                            screen_height: String(screen.height || 1067),
                            viewport_width: String(window.innerWidth || 1152),
                            viewport_height: String(window.innerHeight || 932),
                            pixel_ratio: String(window.devicePixelRatio || 1.5),
                            color_depth: String(screen.colorDepth || 24),
                            browser_name: "Chrome",
                            os_name: navigator.platform || "Windows",
                            sec_ch_ua: navigator.userAgentData && navigator.userAgentData.brands
                              ? navigator.userAgentData.brands.map(b => `"${b.brand}";v="${b.version}"`).join(", ")
                              : "",
                            sec_ch_ua_mobile: navigator.userAgentData
                              ? (navigator.userAgentData.mobile ? "?1" : "?0")
                              : "?0",
                            sec_ch_ua_platform: navigator.userAgentData && navigator.userAgentData.platform
                              ? `"${navigator.userAgentData.platform}"`
                              : "\"Windows\""
                          };
                          let auth = null;
                          let auth_error = "";
                          if (token) {
                            try {
                              const resp = await fetch("/api/v1/auths/", {
                                method: "GET",
                                credentials: "include",
                                headers: {
                                  "Accept": "application/json",
                                  "Content-Type": "application/json",
                                  "Authorization": `Bearer ${token}`
                                }
                              });
                              auth = await resp.json().catch(() => null);
                              if (!resp.ok) auth_error = `auths status ${resp.status}`;
                            } catch (e) {
                              auth_error = String(e && e.message || e);
                            }
                          }
                          const bodyText = (document.body ? document.body.innerText : "").slice(0, 200);
                          return {token, device, telemetry, auth, auth_error, href: location.href, bodyText};
                        }"""
                    )
                    last_capture = capture or {}
                    token = str((capture or {}).get("token") or "")
                    auth = (capture or {}).get("auth") or {}
                    real_token = ""
                    real_auth: dict[str, Any] = {}
                    if token and isinstance(auth, dict) and auth.get("id") and auth.get("role") != "guest":
                        real_token = token
                        real_auth = auth
                        log_login_event("login_detected", f"localStorage token fp={sha16(real_token)[:8]} role={auth.get('role')}")
                    if real_token and isinstance(real_auth, dict):
                        return _finish_login(real_token, real_auth, capture or {})
                    page_state = str((capture or {}).get("href") or "?")
                    page_text = str((capture or {}).get("bodyText") or "").replace("\n", " ")[:120]
                    last_error = str((capture or {}).get("auth_error") or f"waiting for non-guest token @ {page_state} [{page_text}]")
                except Exception as exc:
                    last_error = str(exc)
                try:
                    page.wait_for_timeout(1_000)
                except Exception as exc:
                    last_error = str(exc)
            raise RuntimeError(f"browser login timeout; last state: {last_error}")
        finally:
            browser.close()


@dataclass
class HarState:
    token: str
    user_id: str
    user_name: str
    device_id: str
    captcha_verify_param: str
    user_agent: str
    language: str
    languages: str
    screen_width: str
    screen_height: str
    viewport_width: str
    viewport_height: str
    pixel_ratio: str
    color_depth: str
    browser_name: str
    os_name: str
    chat_id: str
    fe_version: str = FE_VERSION
    region: str = REGION
    sec_ch_ua: str = ""
    sec_ch_ua_mobile: str = "?0"
    sec_ch_ua_platform: str = '"Windows"'


@dataclass
class AccountProfile:
    id: str
    label: str
    source: str
    har_fp: str
    loaded_at: str
    state: HarState


@dataclass
class ChatOptions:
    model: str = DEFAULT_MODEL
    auto_web_search: bool = DEFAULT_AUTO_WEB_SEARCH
    enable_thinking: bool = DEFAULT_ENABLE_THINKING
    reasoning_effort: str = DEFAULT_REASONING_EFFORT
    include_thinking: bool = False
    delete_chat_after_completion: bool = DEFAULT_DELETE_CHAT_AFTER_COMPLETION
    mode: str = "new"
    chat_id: str = ""
    user_msg_id: str = ""


@dataclass
class ToolChoice:
    """Protocol-neutral client tool selection policy."""

    mode: str = "auto"
    forced_name: str = ""
    disable_parallel: bool = False
    allowed_names: tuple[str, ...] = ()


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ProtocolRequest:
    """Normalized external API request consumed by the single GLM execution path."""

    surface: str
    response_model: str
    options: ChatOptions
    stream: bool
    messages: list[dict[str, Any]]
    context_text: str
    execution_prompt: str
    files: list[dict[str, Any]]
    tools: list[dict[str, Any]]
    tool_choice: ToolChoice
    context_as_file: bool
    store: bool = True
    previous_response_id: str = ""


@dataclass
class ProtocolTurn:
    text: str
    thinking: str
    tool_calls: list[ToolCall]
    input_tokens: int
    output_tokens: int
    upstream_chat_id: str = ""
    upstream_chat_deleted: bool = False
    upstream_chat_delete_error: str = ""


@dataclass
class StoredResponse:
    payload: dict[str, Any]
    messages: list[dict[str, Any]]
    expires_at: float


_MISSING = object()


def _first_body_value(body: dict[str, Any], names: tuple[str, ...], default: Any = _MISSING) -> Any:
    for name in names:
        if name in body:
            return body[name]
    for container_name in ("zai", "zai_options", "extra_body", "options"):
        container = body.get(container_name)
        if not isinstance(container, dict):
            continue
        for name in names:
            if name in container:
                return container[name]
    return default


def coerce_bool(value: Any, default: bool = False) -> bool:
    if value is _MISSING or value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "y", "联网", "开启", "开"}:
        return True
    if text in {"0", "false", "no", "off", "n", "关闭", "关"}:
        return False
    return default


def model_suffix_flags(value: Any) -> tuple[str, bool, bool]:
    """Return the normalized base ID plus local force-history/no-thinking flags.

    Suffixes are intentionally parsed only at the end of the model ID, and may
    be stacked in either order.  They are local routing controls rather than
    Z.ai model names.
    """
    model = str(value or "").strip().lower()
    # Claude Code appends context-window markers to model IDs (glm-5.2[1M]);
    # they are display hints, never part of the upstream model name.
    model = re.sub(r"\[[^\]]*\]$", "", model).strip()
    force_history = False
    no_thinking = False
    while model:
        if model.endswith(FORCE_HISTORY_MODEL_SUFFIX):
            force_history = True
            model = model[: -len(FORCE_HISTORY_MODEL_SUFFIX)]
            continue
        if model.endswith(NO_THINKING_MODEL_SUFFIX):
            no_thinking = True
            model = model[: -len(NO_THINKING_MODEL_SUFFIX)]
            continue
        break
    return model.rstrip(), force_history, no_thinking


def is_force_history_model(value: Any) -> bool:
    _base, force_history, _no_thinking = model_suffix_flags(value)
    return force_history


def is_no_thinking_model(value: Any) -> bool:
    _base, _force_history, no_thinking = model_suffix_flags(value)
    return no_thinking


def normalize_model(value: Any = _MISSING) -> str:
    if value is _MISSING or value is None or not str(value).strip():
        return DEFAULT_MODEL
    text = str(value).strip()
    lower, _force_history, _no_thinking = model_suffix_flags(text)
    if not lower:
        raise ValueError("model must include a base model name before local suffixes")
    aliases = {
        "5.3": "glm-5.3",
        "glm5.3": "glm-5.3",
        "glm-5.3": "glm-5.3",
        # GLM-5.3-Flash ships upstream as x-preview-l.
        "flash": FLASH_MODEL,
        "glm-flash": FLASH_MODEL,
        "glm5.3flash": FLASH_MODEL,
        "glm-5.3-flash": FLASH_MODEL,
        "x-preview-l": FLASH_MODEL,
        "5.2": "glm-5.2",
        "glm5.2": "glm-5.2",
        "glm-5.2": "glm-5.2",
        # GLM-5.1 已从官网下架（上游现已 500），旧 ID 重映射到继任旗舰 glm-5.2。
        "5.1": "glm-5.2",
        "glm5.1": "glm-5.2",
        "glm-5.1": "glm-5.2",
        "glm-5-turbo": TURBO_MODEL,
        "glm5turbo": TURBO_MODEL,
        "5-turbo": TURBO_MODEL,
        "turbo": TURBO_MODEL,
        # Common OpenAI-compatible model IDs. They remain response aliases;
        # only the selected GLM model reaches chat.z.ai upstream.
        "gpt-5": "glm-5.3",
        "gpt-5.1": "glm-5.3",
        "gpt-5.2": "glm-5.3",
        "gpt-5.3": "glm-5.3",
        "gpt-5-codex": "glm-5.3",
        "gpt-5.1-codex": "glm-5.3",
        "gpt-5.2-codex": "glm-5.3",
        "gpt-5.3-codex": "glm-5.3",
        "gpt-4.1": "glm-5.3",
        "gpt-4o": "glm-5.2",
        "o1": "glm-5.3",
        "o3": "glm-5.3",
        "o4-mini": "glm-5.2",
        "claude-sonnet-4-6": "glm-5.3",
        "claude-opus-4-6": "glm-5.3",
        "claude-haiku-4-5": "glm-5.2",
        "claude-3-5-sonnet-latest": "glm-5.3",
        "claude-3-5-haiku-latest": "glm-5.2",
    }
    if lower in aliases:
        return aliases[lower]
    if lower.startswith("claude-"):
        return "glm-5.2" if "haiku" in lower else DEFAULT_MODEL
    if lower.startswith(("gpt-", "chatgpt-", "codex-", "o1", "o3", "o4")):
        return DEFAULT_MODEL
    for model in SUPPORTED_MODELS:
        if lower == model.lower():
            return model
    raise ValueError(f"unsupported model: {text}; supported: {', '.join(ADVERTISED_MODELS)}")


def normalize_reasoning_effort(value: Any = _MISSING) -> str:
    if value is _MISSING or value is None or not str(value).strip():
        return DEFAULT_REASONING_EFFORT
    text = str(value).strip().lower()
    aliases = {
        "l": "low",
        "low": "low",
        "低": "low",
        "h": "high",
        "high": "high",
        "高": "high",
        "m": "max",
        "max": "max",
        "maximum": "max",
        "super": "max",
        "super-high": "max",
        "ultra": "max",
        "超高": "max",
        "最大": "max",
    }
    if text in aliases:
        return aliases[text]
    raise ValueError(f"unsupported reasoning_effort: {value}; supported: low, high, max (availability varies by model)")


def coerce_reasoning_effort_for_model(model: str, effort: str) -> str:
    """Clamp the requested effort to what the selected model supports (2026-08 UI):

    glm-5.3 / x-preview-l: low/high/max; glm-5.2: high/max (no low tier);
    GLM-5-Turbo: thinking toggle only -- effort is never sent in features, so
    the stored value only mirrors the official UI's chat-level field.
    """
    normalized = normalize_reasoning_effort(effort)
    if normalized == "low" and model not in (DEFAULT_MODEL, FLASH_MODEL):
        return "high"
    return normalized


def chat_options_from_body(body: dict[str, Any], include_thinking_default: bool = False) -> ChatOptions:
    mode = str(_first_body_value(body, ("mode", "conversation_mode"), "new") or "new").strip().lower()
    if mode not in {"new", "continue", "edit", "reuse"}:
        raise ValueError("mode must be one of: new, continue, edit, reuse")
    selected_model = normalize_model(_first_body_value(body, ("model",), DEFAULT_MODEL))
    return ChatOptions(
        model=selected_model,
        auto_web_search=coerce_bool(
            _first_body_value(body, ("auto_web_search", "web_search", "search"), DEFAULT_AUTO_WEB_SEARCH),
            DEFAULT_AUTO_WEB_SEARCH,
        ),
        enable_thinking=coerce_bool(
            _first_body_value(body, ("enable_thinking", "thinking", "deep_thinking"), DEFAULT_ENABLE_THINKING),
            DEFAULT_ENABLE_THINKING,
        ),
        reasoning_effort=coerce_reasoning_effort_for_model(
            selected_model,
            normalize_reasoning_effort(
                _first_body_value(body, ("reasoning_effort", "thinking_effort"), DEFAULT_REASONING_EFFORT)
            ),
        ),
        include_thinking=coerce_bool(
            _first_body_value(body, ("include_thinking",), include_thinking_default),
            include_thinking_default,
        ),
        delete_chat_after_completion=coerce_bool(
            _first_body_value(
                body,
                ("delete_chat_after_completion", "delete_after_completion", "auto_delete"),
                DEFAULT_DELETE_CHAT_AFTER_COMPLETION,
            ),
            DEFAULT_DELETE_CHAT_AFTER_COMPLETION,
        ),
        mode=mode,
        chat_id=str(_first_body_value(body, ("chat_id", "conversation_id"), "") or "").strip(),
        user_msg_id=str(
            _first_body_value(body, ("user_msg_id", "current_user_message_id", "message_id"), "") or ""
        ).strip(),
    )


def chat_options_public(options: ChatOptions) -> dict[str, Any]:
    return {
        "model": options.model,
        "auto_web_search": options.auto_web_search,
        "enable_thinking": options.enable_thinking,
        "reasoning_effort": options.reasoning_effort,
        "include_thinking": options.include_thinking,
        "delete_chat_after_completion": options.delete_chat_after_completion,
        "mode": options.mode,
        "chat_id": options.chat_id,
    }


def safe_profile_label(label: str, fallback: str = "HAR profile") -> str:
    cleaned = "".join(ch for ch in label.strip() if ch.isprintable())
    cleaned = cleaned.replace("\r", " ").replace("\n", " ")
    return (cleaned[:80] or fallback).strip()


def local_settings_defaults() -> dict[str, Any]:
    return {
        "model": DEFAULT_MODEL,
        "auto_web_search": DEFAULT_AUTO_WEB_SEARCH,
        "enable_thinking": DEFAULT_ENABLE_THINKING,
        "reasoning_effort": DEFAULT_REASONING_EFFORT,
        "include_thinking": False,
        "delete_chat_after_completion": DEFAULT_DELETE_CHAT_AFTER_COMPLETION,
        "upstream_timeout_sec": UPSTREAM_STREAM_TIMEOUT_SEC,
        "upstream_retry_wait_sec": DEFAULT_UPSTREAM_RETRY_WAIT_SEC,
        "upstream_retry_max_attempts": DEFAULT_UPSTREAM_RETRY_ATTEMPTS,
        "history_max_records": 300,
    }


def normalize_local_settings(body: dict[str, Any], defaults: dict[str, Any] | None = None) -> dict[str, Any]:
    base = dict(defaults or local_settings_defaults())
    merged = dict(base)
    for key in ("model", "auto_web_search", "enable_thinking", "reasoning_effort", "include_thinking", "delete_chat_after_completion"):
        if key in body:
            merged[key] = body[key]
    options = chat_options_from_body(merged, include_thinking_default=bool(base.get("include_thinking", False)))
    try:
        upstream_timeout_sec = int(body.get("upstream_timeout_sec", base.get("upstream_timeout_sec", UPSTREAM_STREAM_TIMEOUT_SEC)))
        upstream_timeout_sec = max(60, min(3600, upstream_timeout_sec))
    except (TypeError, ValueError):
        upstream_timeout_sec = int(base.get("upstream_timeout_sec", UPSTREAM_STREAM_TIMEOUT_SEC))
    try:
        upstream_retry_wait_sec = float(
            body.get("upstream_retry_wait_sec", base.get("upstream_retry_wait_sec", DEFAULT_UPSTREAM_RETRY_WAIT_SEC))
        )
        upstream_retry_wait_sec = max(0.0, min(120.0, upstream_retry_wait_sec))
    except (TypeError, ValueError):
        upstream_retry_wait_sec = float(base.get("upstream_retry_wait_sec", DEFAULT_UPSTREAM_RETRY_WAIT_SEC))
    try:
        upstream_retry_max_attempts = int(
            body.get(
                "upstream_retry_max_attempts",
                base.get("upstream_retry_max_attempts", DEFAULT_UPSTREAM_RETRY_ATTEMPTS),
            )
        )
        upstream_retry_max_attempts = max(1, min(6, upstream_retry_max_attempts))
    except (TypeError, ValueError):
        upstream_retry_max_attempts = int(base.get("upstream_retry_max_attempts", DEFAULT_UPSTREAM_RETRY_ATTEMPTS))
    try:
        history_max_records = int(body.get("history_max_records", base.get("history_max_records", 300)))
        history_max_records = max(50, min(2000, history_max_records))
    except (TypeError, ValueError):
        history_max_records = int(base.get("history_max_records", 300))
    return {
        "model": options.model,
        "auto_web_search": options.auto_web_search,
        "enable_thinking": options.enable_thinking,
        "reasoning_effort": options.reasoning_effort,
        "include_thinking": options.include_thinking,
        "delete_chat_after_completion": options.delete_chat_after_completion,
        "upstream_timeout_sec": upstream_timeout_sec,
        "upstream_retry_wait_sec": upstream_retry_wait_sec,
        "upstream_retry_max_attempts": upstream_retry_max_attempts,
        "history_max_records": history_max_records,
    }


def load_local_settings(path: Path = SETTINGS_STORE_PATH) -> tuple[dict[str, Any], str, str]:
    with SETTINGS_STORE_LOCK:
        if not path.exists():
            return local_settings_defaults(), "", ""
        try:
            raw = json.loads(path.read_text(encoding="utf-8", errors="replace"))
            if not isinstance(raw, dict):
                raise ValueError("settings store must be a JSON object")
            source = raw.get("settings") if isinstance(raw.get("settings"), dict) else raw
            settings = normalize_local_settings(source)
            return settings, str(raw.get("saved_at") or ""), ""
        except Exception as exc:
            return local_settings_defaults(), "", f"settings load failed: {exc}"


def save_local_settings(settings: dict[str, Any], path: Path = SETTINGS_STORE_PATH) -> str:
    with SETTINGS_STORE_LOCK:
        saved_at = datetime.now().astimezone().isoformat(timespec="seconds")
        payload = {
            "schema": "glm2api.settings.v1",
            "saved_at": saved_at,
            "settings": normalize_local_settings(settings),
        }
        atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        return saved_at


def load_api_key_store(path: Path = API_KEY_STORE_PATH) -> tuple[str, str, str]:
    with API_KEY_STORE_LOCK:
        if not path.exists():
            return "", "", ""
        try:
            store = json.loads(path.read_text(encoding="utf-8", errors="replace"))
            if not isinstance(store, dict) or store.get("encryption") != "windows-dpapi-current-user":
                raise ValueError("unsupported api key store encryption")
            encrypted = base64.b64decode(str(store.get("payload") or ""))
            payload = json.loads(dpapi_unprotect(encrypted).decode("utf-8", errors="replace"))
            if not isinstance(payload, dict):
                raise ValueError("api key store payload must be an object")
            return str(payload.get("api_key") or ""), str(store.get("saved_at") or ""), ""
        except Exception as exc:
            return "", "", f"api key load failed: {exc}"


def save_api_key_store(api_key: str, path: Path = API_KEY_STORE_PATH) -> str:
    with API_KEY_STORE_LOCK:
        saved_at = datetime.now().astimezone().isoformat(timespec="seconds")
        payload = {"api_key": str(api_key or "").strip()}
        encrypted = dpapi_protect(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        store = {
            "schema": "glm2api.api_key_store.v1",
            "encryption": "windows-dpapi-current-user",
            "saved_at": saved_at,
            "payload": base64.b64encode(encrypted).decode("ascii"),
        }
        atomic_write_text(path, json.dumps(store, ensure_ascii=False, indent=2) + "\n")
        return saved_at


def profile_source_display(source: str) -> tuple[str, str]:
    text = safe_profile_label(str(source or ""), fallback="unknown")
    lower = text.lower()
    if lower == "browser login":
        return "浏览器登录", "browser"
    if lower.startswith("preloaded har:"):
        return "预加载 HAR: " + Path(text.split(":", 1)[1].strip()).name, "har"
    if lower.startswith("web upload"):
        return "网页上传 HAR", "har"
    if re.match(r"^[A-Za-z]:[\\/]", text) or "\\" in text or "/" in text:
        return "本地 HAR: " + Path(text.replace("\\", "/")).name, "har"
    if lower.startswith("token"):
        return "token 直连", "token"
    return text, "har" if "har" in lower else "profile"


def jwt_payload_claims(token: str) -> dict[str, Any]:
    """Decode the payload segment of a chat.z.ai session JWT (no signature check)."""
    parts = str(token or "").strip().split(".")
    if len(parts) != 3 or not all(parts):
        raise ValueError("token 必须是 chat.z.ai 的三段式 JWT（形如 eyJ....xxx....xxx）")
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = json.loads(
            base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8", errors="replace")
        )
    except Exception as exc:
        raise ValueError(f"token payload 解析失败: {exc}")
    if not isinstance(claims, dict):
        raise ValueError("token payload 不是 JSON 对象")
    return claims


def synthesize_device_id() -> str:
    # Mirrors the official telemetry shape uid_<16 base36 chars> (localStorage _arms_uid),
    # e.g. uid_bc0but6i8yc2rl45 in the 2026-08 upload HAR.
    alphabet = string.ascii_lowercase + string.digits
    return "uid_" + "".join(secrets.choice(alphabet) for _ in range(16))


def refresh_state_browser_version(state: HarState) -> None:
    """Align logins captured months ago with the current front-end fingerprint.

    chat.z.ai ships a new bundle (x-fe-version) and users upgrade Chrome over
    time; replaying an old UA/fe-version pair from a stored profile stands out,
    so stale values are refreshed in memory to the current constants.
    """
    match = re.search(r"Chrome/(\d+)", str(state.user_agent or ""))
    if not match or int(match.group(1)) < 151:
        state.user_agent = DEFAULT_USER_AGENT
        state.sec_ch_ua = default_sec_ch_ua(DEFAULT_USER_AGENT)
    if str(state.fe_version or "") != FE_VERSION:
        state.fe_version = FE_VERSION
    if not str(state.device_id or "").strip():
        state.device_id = synthesize_device_id()


def state_from_token(token: str) -> HarState:
    """Build a runnable HarState from a bare session token (no HAR required).

    The token payload already carries the user id; device id and browser
    telemetry are synthesized to match the current production fingerprint.
    """
    token = str(token or "").strip()
    claims = jwt_payload_claims(token)
    user_id = str(claims.get("id") or claims.get("sub") or claims.get("user_id") or "")
    if not user_id:
        raise ValueError("token payload 缺少用户 id，请确认复制的是 chat.z.ai 的会话 token")
    return HarState(
        token=token,
        user_id=user_id,
        user_name=str(claims.get("name") or claims.get("email") or "token-user"),
        device_id=synthesize_device_id(),
        captcha_verify_param="",
        user_agent=DEFAULT_USER_AGENT,
        language="zh-CN",
        languages="zh-CN,zh",
        screen_width="1707",
        screen_height="1067",
        viewport_width="1152",
        viewport_height="932",
        pixel_ratio="1.5",
        color_depth="24",
        browser_name="Chrome",
        os_name="Windows",
        chat_id="",
        fe_version=FE_VERSION,
        region=REGION,
        sec_ch_ua=default_sec_ch_ua(DEFAULT_USER_AGENT),
        sec_ch_ua_mobile="?0",
        sec_ch_ua_platform='"Windows"',
    )


def profile_summary(
    profile: AccountProfile,
    active: bool = False,
    same_user_count: int = 1,
) -> dict[str, Any]:
    state = profile.state
    source_display, source_type = profile_source_display(profile.source)
    return {
        "id": profile.id,
        "label": profile.label,
        "source": profile.source,
        "source_display": source_display,
        "source_type": source_type,
        "har_fp": profile.har_fp,
        "loaded_at": profile.loaded_at,
        "active": active,
        "user_id": state.user_id,
        "user_id_fp": sha16(state.user_id) if state.user_id else "",
        "user_name": state.user_name,
        "token_fp": sha16(state.token),
        "device_id_fp": sha16(state.device_id) if state.device_id else "",
        "captcha_fp": sha16(state.captcha_verify_param) if state.captcha_verify_param else "",
        "chat_id": state.chat_id,
        "same_user_count": same_user_count,
        "duplicate_user": same_user_count > 1,
        "default_model": DEFAULT_MODEL,
        "supported_models": list(ADVERTISED_MODELS),
    }


def make_profile(state: HarState, label: str, source: str, har_text: str = "", har_fp: str = "") -> AccountProfile:
    if not har_fp and har_text:
        har_fp = hashlib.sha256(har_text.encode("utf-8", errors="replace")).hexdigest()[:16]
    return AccountProfile(
        id="profile_" + uuid.uuid4().hex[:12],
        label=safe_profile_label(label),
        source=safe_profile_label(source, fallback="uploaded HAR"),
        har_fp=har_fp,
        loaded_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        state=state,
    )


def har_state_from_dict(data: dict[str, Any]) -> HarState:
    fields = HarState.__dataclass_fields__
    values = {name: str(data.get(name) or "") for name in fields}
    values["fe_version"] = values.get("fe_version") or FE_VERSION
    values["region"] = values.get("region") or REGION
    return HarState(**values)


def profile_to_dict(profile: AccountProfile) -> dict[str, Any]:
    return {
        "id": profile.id,
        "label": profile.label,
        "source": profile.source,
        "har_fp": profile.har_fp,
        "loaded_at": profile.loaded_at,
        "state": asdict(profile.state),
    }


def profile_from_dict(data: dict[str, Any]) -> AccountProfile:
    return AccountProfile(
        id=str(data.get("id") or "profile_" + uuid.uuid4().hex[:12]),
        label=safe_profile_label(str(data.get("label") or "saved profile")),
        source=safe_profile_label(str(data.get("source") or "profile store")),
        har_fp=str(data.get("har_fp") or ""),
        loaded_at=str(data.get("loaded_at") or datetime.now().astimezone().isoformat(timespec="seconds")),
        state=har_state_from_dict(data.get("state") or {}),
    )


class DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def dpapi_protect(data: bytes) -> bytes:
    if os.name != "nt":
        raise RuntimeError("DPAPI profile persistence is only available on Windows")
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(DataBlob),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    in_buffer = ctypes.create_string_buffer(data)
    in_blob = DataBlob(len(data), ctypes.cast(in_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    out_blob = DataBlob()
    if not crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(out_blob),
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def dpapi_unprotect(data: bytes) -> bytes:
    if os.name != "nt":
        raise RuntimeError("DPAPI profile persistence is only available on Windows")
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(DataBlob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    in_buffer = ctypes.create_string_buffer(data)
    in_blob = DataBlob(len(data), ctypes.cast(in_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    out_blob = DataBlob()
    if not crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(out_blob),
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def save_profile_store(
    profiles: dict[str, AccountProfile],
    active_profile_id: str,
    path: Path = PROFILE_STORE_PATH,
) -> None:
    with PROFILE_STORE_LOCK:
        payload = {
            "active_profile_id": active_profile_id,
            "profiles": [profile_to_dict(profile) for profile in profiles.values()],
            "saved_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        encrypted = dpapi_protect(raw)
        store = {
            "schema": "glm2api.profile_store.v1",
            "encryption": "windows-dpapi-current-user",
            "payload": base64.b64encode(encrypted).decode("ascii"),
            "profile_count": len(profiles),
            "active_profile_id": active_profile_id,
            "saved_at": payload["saved_at"],
        }
        atomic_write_text(path, json.dumps(store, ensure_ascii=False, indent=2) + "\n")


def load_profile_store(path: Path = PROFILE_STORE_PATH) -> tuple[dict[str, AccountProfile], str, str]:
    with PROFILE_STORE_LOCK:
        if not path.exists():
            return {}, "", ""
        store = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        if store.get("encryption") != "windows-dpapi-current-user":
            raise RuntimeError("unsupported profile store encryption")
        encrypted = base64.b64decode(str(store.get("payload") or ""))
        payload = json.loads(dpapi_unprotect(encrypted).decode("utf-8", errors="replace"))
        profiles: dict[str, AccountProfile] = {}
        for item in payload.get("profiles") or []:
            if isinstance(item, dict):
                profile = profile_from_dict(item)
                refresh_state_browser_version(profile.state)
                profiles[profile.id] = profile
        active = str(payload.get("active_profile_id") or "")
        if active not in profiles:
            active = next(iter(profiles.keys()), "")
        return profiles, active, str(store.get("saved_at") or "")


def merge_profile(profiles: dict[str, AccountProfile], profile: AccountProfile) -> str:
    token_fp = sha16(profile.state.token)
    for existing_id, existing in profiles.items():
        if sha16(existing.state.token) == token_fp:
            profile.id = existing_id
            profiles[existing_id] = profile
            return existing_id
    if profile.state.user_id:
        for existing_id, existing in profiles.items():
            if existing.state.user_id and existing.state.user_id == profile.state.user_id:
                profile.id = existing_id
                profiles[existing_id] = profile
                return existing_id
    profiles[profile.id] = profile
    return profile.id


def profile_user_counts(profiles: dict[str, AccountProfile]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for profile in profiles.values():
        key = profile.state.user_id
        if key:
            counts[key] = counts.get(key, 0) + 1
    return counts


def profile_duplicate_stats(profiles: dict[str, AccountProfile]) -> dict[str, int]:
    counts = profile_user_counts(profiles)
    duplicate_groups = [count for count in counts.values() if count > 1]
    return {
        "duplicate_user_groups": len(duplicate_groups),
        "duplicate_profile_count": sum(count - 1 for count in duplicate_groups),
    }


def profile_loaded_timestamp(profile: AccountProfile) -> str:
    return str(profile.loaded_at or "")


def compact_duplicate_profiles(
    profiles: dict[str, AccountProfile],
    active_profile_id: str,
) -> tuple[str, list[AccountProfile]]:
    by_user: dict[str, list[tuple[str, AccountProfile]]] = {}
    for profile_id, profile in profiles.items():
        user_id = profile.state.user_id
        if user_id:
            by_user.setdefault(user_id, []).append((profile_id, profile))

    removed: list[AccountProfile] = []
    for items in by_user.values():
        if len(items) <= 1:
            continue
        if any(profile_id == active_profile_id for profile_id, _profile in items):
            keep_id = active_profile_id
        else:
            keep_id, _keep_profile = max(items, key=lambda item: profile_loaded_timestamp(item[1]))
        for profile_id, profile in items:
            if profile_id == keep_id:
                continue
            removed.append(profile)
            profiles.pop(profile_id, None)
    if active_profile_id not in profiles:
        active_profile_id = next(iter(profiles.keys()), "")
    return active_profile_id, removed


def extract_state(har: dict[str, Any]) -> HarState:
    entries = har.get("log", {}).get("entries", [])

    signin_user: dict[str, Any] | None = None
    auth_user: dict[str, Any] | None = None
    latest_completion: dict[str, Any] | None = None
    fallback_completion: dict[str, Any] | None = None

    for entry in entries:
        path = entry_url_path(entry)
        status = entry.get("response", {}).get("status")
        if status != 200:
            continue
        if path == "/api/v1/auths/signin":
            obj = response_json(entry)
            if isinstance(obj, dict) and obj.get("token"):
                signin_user = obj
        elif path == "/api/v1/auths/":
            obj = response_json(entry)
            if isinstance(obj, dict) and obj.get("token"):
                auth_user = obj
        elif path == "/api/v2/chat/completions":
            body = request_json(entry)
            if isinstance(body, dict):
                try:
                    model = normalize_model(body.get("model"))
                except ValueError:
                    continue
                fallback_completion = entry
                if model == DEFAULT_MODEL:
                    latest_completion = entry

    latest_completion = latest_completion or fallback_completion
    if not latest_completion:
        raise RuntimeError("HAR 中没有找到支持模型的 /api/v2/chat/completions 成功请求")

    req = latest_completion.get("request", {})
    headers = req.get("headers", [])
    q = dict(parse_qsl(urlsplit(req.get("url", "")).query, keep_blank_values=True))
    body = request_json(latest_completion) or {}
    variables = body.get("variables") if isinstance(body.get("variables"), dict) else {}

    user = signin_user or auth_user or {}

    token = str(user.get("token") or q.get("token") or "")
    user_id = str(user.get("id") or q.get("user_id") or "")
    if not token or not user_id:
        raise RuntimeError("无法从 HAR 提取 token / user_id")

    return HarState(
        token=token,
        user_id=user_id,
        user_name=str(user.get("name") or variables.get("{{USER_NAME}}") or "user"),
        device_id=header_value(headers, "x-device-id"),
        captcha_verify_param=str(os.environ.get("ZAI_CAPTCHA_VERIFY_PARAM") or body.get("captcha_verify_param") or ""),
        user_agent=q.get("user_agent") or header_value(headers, "user-agent") or "Mozilla/5.0",
        language=q.get("language") or header_value(headers, "accept-language") or "zh-CN",
        languages=q.get("languages") or "zh-CN,zh",
        screen_width=q.get("screen_width") or "1707",
        screen_height=q.get("screen_height") or "1067",
        viewport_width=q.get("viewport_width") or "1152",
        viewport_height=q.get("viewport_height") or "932",
        pixel_ratio=q.get("pixel_ratio") or "1.5",
        color_depth=q.get("color_depth") or "24",
        browser_name=q.get("browser_name") or "Chrome",
        os_name=q.get("os_name") or "Windows",
        chat_id=str(body.get("chat_id") or ""),
        fe_version=header_value(headers, "x-fe-version", FE_VERSION),
        region=header_value(headers, "x-region", REGION),
        sec_ch_ua=header_value(headers, "sec-ch-ua"),
        sec_ch_ua_mobile=header_value(headers, "sec-ch-ua-mobile", "?0"),
        sec_ch_ua_platform=header_value(headers, "sec-ch-ua-platform", '"Windows"'),
    )


def extract_state_from_har_path(path: Path) -> tuple[HarState, str]:
    har_fp = file_sha16(path)
    har = load_har(path)
    try:
        return extract_state(har), har_fp
    finally:
        har.clear()


def extract_state_via_worker(path: Path, timeout_sec: int = 120) -> tuple[HarState, str]:
    script_path = Path(__file__).resolve()
    completed = subprocess.run(
        [sys.executable, str(script_path), "--extract-state-json", "--har", str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_sec,
        check=False,
    )
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        raise RuntimeError(f"HAR 状态提取 worker 失败，退出码 {completed.returncode}: {stderr[-500:]}")
    payload = json.loads(completed.stdout)
    return har_state_from_dict(payload.get("state") or {}), str(payload.get("har_fp") or "")


def extract_state_from_uploaded_bytes(raw: bytes) -> tuple[HarState, str]:
    tmp_name = ""
    try:
        with tempfile.NamedTemporaryFile(prefix="glm2api-upload-", suffix=".har", delete=False) as f:
            tmp_name = f.name
            f.write(raw)
        return extract_state_via_worker(Path(tmp_name))
    finally:
        if tmp_name:
            try:
                Path(tmp_name).unlink(missing_ok=True)
            except Exception:
                pass


def query_params(state: HarState, prompt: str, chat_id: str) -> tuple[str, str]:
    timestamp = now_ms()
    request_id = str(uuid.uuid4())
    local = datetime.now().astimezone()
    utc_now = datetime.now(timezone.utc)
    params = {
        "timestamp": timestamp,
        "requestId": request_id,
        "user_id": state.user_id,
        "version": "0.0.1",
        "platform": "web",
        "token": state.token,
        "user_agent": state.user_agent,
        "language": state.language,
        "languages": state.languages,
        "timezone": "Asia/Shanghai",
        "cookie_enabled": "true",
        "screen_width": state.screen_width,
        "screen_height": state.screen_height,
        "screen_resolution": f"{state.screen_width}x{state.screen_height}",
        "viewport_height": state.viewport_height,
        "viewport_width": state.viewport_width,
        "viewport_size": f"{state.viewport_width}x{state.viewport_height}",
        "color_depth": state.color_depth,
        "pixel_ratio": state.pixel_ratio,
        "current_url": f"{BASE_URL}/c/{chat_id}" if chat_id else BASE_URL + "/",
        "pathname": f"/c/{chat_id}" if chat_id else "/",
        "search": "",
        "hash": "",
        "host": "chat.z.ai",
        "hostname": "chat.z.ai",
        "protocol": "https:",
        "referrer": "",
        "title": PAGE_TITLE,
        "timezone_offset": str(-int(local.utcoffset().total_seconds() / 60)) if local.utcoffset() else "-480",
        "local_time": utc_now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "utc_time": email.utils.format_datetime(utc_now, usegmt=True),
        "is_mobile": "false",
        "is_touch": "false",
        "max_touch_points": "10",
        "browser_name": state.browser_name,
        "os_name": state.os_name,
    }
    signature = z_sign(prompt, timestamp, request_id, state.user_id)
    params["signature_timestamp"] = timestamp
    return urlencode(params), signature


def chrome_major_from_user_agent(user_agent: str) -> str:
    match = re.search(r"(?:Chrome|Chromium)/(\d+)", user_agent or "")
    return match.group(1) if match else "139"


def default_sec_ch_ua(user_agent: str) -> str:
    major = chrome_major_from_user_agent(user_agent)
    return f'"Not=A?Brand";v="99", "Google Chrome";v="{major}", "Chromium";v="{major}"'


def default_sec_ch_ua_platform(state: HarState) -> str:
    probe = f"{state.sec_ch_ua_platform} {state.os_name} {state.user_agent}".lower()
    if "mac" in probe:
        platform = "macOS"
    elif "linux" in probe:
        platform = "Linux"
    elif "android" in probe:
        platform = "Android"
    else:
        platform = "Windows"
    return f'"{platform}"'


def control_accept_language(state: HarState) -> str:
    # Upload/delete/control XHRs send a q-graded pair ("zh-CN,zh;q=0.9") while
    # chats/new and completions send the bare primary language; match both.
    primary = str(state.language or "zh-CN").split(",")[0].strip() or "zh-CN"
    secondary = primary.split("-")[0] or primary
    if secondary == primary:
        secondary = "zh" if primary.startswith("zh") else primary
    return f"{primary},{secondary};q=0.9"


def browser_client_hint_headers(state: HarState) -> dict[str, str]:
    """Fetch metadata and Client Hints observed on chat.z.ai same-origin XHRs.

    These headers are not used for local CORS.  They make server-to-server
    control requests look like the browser HAR; missing `Sec-Fetch-*` caused
    upstream 403 on the chat deletion endpoint in the captured flow.
    """
    sec_ch_ua_platform = str(getattr(state, "sec_ch_ua_platform", "") or "").strip()
    if sec_ch_ua_platform and not (sec_ch_ua_platform.startswith('"') and sec_ch_ua_platform.endswith('"')):
        sec_ch_ua_platform = f'"{sec_ch_ua_platform.strip(chr(34))}"'
    return {
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "sec-ch-ua": str(getattr(state, "sec_ch_ua", "") or default_sec_ch_ua(state.user_agent)),
        "sec-ch-ua-mobile": str(getattr(state, "sec_ch_ua_mobile", "") or "?0"),
        "sec-ch-ua-platform": sec_ch_ua_platform or default_sec_ch_ua_platform(state),
    }


def request_headers(state: HarState, signature: str, accept: str = "*/*") -> dict[str, str]:
    headers = {
        "Accept": accept,
        "Accept-Language": state.language,
        "Authorization": f"Bearer {state.token}",
        "Content-Type": "application/json",
        "Origin": BASE_URL,
        "User-Agent": state.user_agent,
        "X-FE-Version": state.fe_version,
        "X-Region": state.region,
        "X-Signature": signature,
    }
    headers.update(browser_client_hint_headers(state))
    if state.device_id:
        headers["X-Device-ID"] = state.device_id
    return headers


def http_json(method: str, url: str, headers: dict[str, str], payload: dict[str, Any] | None = None) -> Any:
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    req = Request(url, data=data, headers=headers, method=method)
    with urlopen(req, timeout=60) as resp:
        raw = resp.read()
        text = raw.decode("utf-8", errors="replace")
        parsed = json_or_none(text)
        return parsed if parsed is not None else text


def require_uuid(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    try:
        return str(uuid.UUID(text))
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a UUID") from exc


def chat_control_headers(state: HarState, with_auth: bool = False) -> dict[str, str]:
    """Headers observed for chat deletion and task stopping in the browser HAR."""
    headers = {
        "Accept": "application/json",
        "Accept-Language": control_accept_language(state),
        "Content-Type": "application/json",
        "Origin": BASE_URL,
        "User-Agent": state.user_agent,
        "X-Region": state.region,
    }
    headers.update(browser_client_hint_headers(state))
    if with_auth:
        headers["Authorization"] = f"Bearer {state.token}"
        headers["X-FE-Version"] = state.fe_version
        if state.device_id:
            headers["X-Device-ID"] = state.device_id
    return headers


def http_error_summary(exc: HTTPError) -> str:
    body = ""
    try:
        body = exc.read().decode("utf-8", errors="replace").strip()
    except Exception:
        body = ""
    finally:
        try:
            exc.close()
        except Exception:
            pass
    reason = str(getattr(exc, "reason", "") or getattr(exc, "msg", "") or "").strip()
    summary = f"HTTP Error {exc.code}"
    if reason:
        summary += f": {reason}"
    if body:
        summary += f": {body[:300]}"
    return summary


def delete_zai_chat(state: HarState, chat_id: str) -> bool:
    chat_id = require_uuid(chat_id, "chat_id")
    last_error = ""
    delete_started = time.time()
    # Occasional upstream TLS resets ("UNEXPECTED_EOF_WHILE_READING") hit DELETE;
    # one spaced retry keeps auto-delete reliable.
    for attempt in range(2):
        if attempt:
            time.sleep(0.8)
        try:
            for with_auth in (False, True):
                try:
                    result = http_json(
                        "DELETE",
                        f"{BASE_URL}/api/v1/chats/{chat_id}",
                        chat_control_headers(state, with_auth=with_auth),
                    )
                except HTTPError as exc:
                    last_error = http_error_summary(exc)
                    if exc.code == 404:
                        # 会话已不存在（如创建即失败、或已被删除）视作删除成功。
                        log_event("upstream_chat_deleted", chat_id_fp=sha16(chat_id), note="already_gone")
                        return True
                    if exc.code in {401, 403} and not with_auth:
                        continue
                    raise RuntimeError(f"删除对话失败: {last_error}") from exc
                if result is True:
                    log_event(
                        "upstream_chat_deleted",
                        chat_id_fp=sha16(chat_id),
                        elapsed_ms=int((time.time() - delete_started) * 1000),
                    )
                    return True
                last_error = str(result)[:300]
                break
        except URLError as exc:
            last_error = f"urlopen error: {getattr(exc, 'reason', exc)}"
            continue
        break
    raise RuntimeError(f"删除对话失败: {last_error or 'unknown error'}")


# ---------------------------------------------------------------------------
# 上游历史对话浏览（面板「历史」页数据源）。
# HAR 实测接口与报文结构：
#   GET /api/v1/chats/?page=N&type=default  -> 裸数组 [{id,title,updated_at,created_at,type}]
#   GET /api/v1/chats/{chat_id}             -> {id,title,chat:{models,history:{messages:{id:{...}}},
#                                              currentId},updated_at,created_at,...}
# 消息按 id 存字典、parentId 链定序，从 currentId 回溯到根后反转即时间正序。
# ---------------------------------------------------------------------------

def _chat_control_get(state: HarState, url: str, action: str) -> Any:
    """GET 类 chat 控制接口：先匿名（与浏览器删除行为一致），401/403 再带鉴权重试。"""
    last_error = ""
    for with_auth in (False, True):
        try:
            return http_json("GET", url, chat_control_headers(state, with_auth=with_auth))
        except HTTPError as exc:
            last_error = http_error_summary(exc)
            if exc.code in {401, 403} and not with_auth:
                continue
            if exc.code == 404:
                raise RuntimeError(f"{action}: 对话不存在或已被删除") from exc
            raise RuntimeError(f"{action}失败: {last_error}") from exc
    raise RuntimeError(f"{action}失败: {last_error or 'unknown error'}")


def list_zai_chats(state: HarState, page: int = 1) -> list[dict[str, Any]]:
    """拉取上游账号的历史对话列表（单页，page 从 1 起）。"""
    page = max(1, int(page))
    result = _chat_control_get(state, f"{BASE_URL}/api/v1/chats/?page={page}&type=default", "获取对话列表")
    if isinstance(result, list):
        return [item for item in result if isinstance(item, dict)]
    raise RuntimeError(f"获取对话列表失败: unexpected payload {str(result)[:200]}")


def get_zai_chat_detail(state: HarState, chat_id: str) -> dict[str, Any]:
    chat_id = require_uuid(chat_id, "chat_id")
    result = _chat_control_get(state, f"{BASE_URL}/api/v1/chats/{chat_id}", "获取对话详情")
    if isinstance(result, dict) and isinstance(result.get("chat"), dict):
        return result
    raise RuntimeError(f"获取对话详情失败: unexpected payload {str(result)[:200]}")


def normalize_history_message_content(value: Any) -> str:
    """防御性展开历史消息 content。

    实测代理默认模型不落助手历史（-forcehistory 的由来），content 可能缺失；
    不同版本上游又可能是纯文本、JSON 字符串或 [{type,text}] 数组，这里统一归一成文本。
    """
    if value is None:
        return ""
    if isinstance(value, str):
        text = value.strip()
        if text[:1] in "{[":
            parsed = json_or_none(text)
            if parsed is None:
                return text
            return normalize_history_message_content(parsed)
        return text
    if isinstance(value, dict):
        for key in ("content", "text", "message"):
            inner = value.get(key)
            if isinstance(inner, str) and inner.strip():
                return normalize_history_message_content(inner)
        if isinstance(value.get("content"), list):
            return normalize_history_message_content(value["content"])
        return json.dumps(value, ensure_ascii=False, indent=2)
    if isinstance(value, list):
        parts = [
            item["text"] if isinstance(item, dict) and isinstance(item.get("text"), str) else item
            for item in value
            if isinstance(item, str) or (isinstance(item, dict) and isinstance(item.get("text"), str))
        ]
        if parts:
            return "\n".join(parts)
        return json.dumps(value, ensure_ascii=False, indent=2)
    return str(value)


def extract_chat_history_messages(detail: dict[str, Any]) -> list[dict[str, Any]]:
    """从上游 chat 详情还原时间正序的消息链；缺 currentId 时按时间戳排序兜底。"""
    chat = detail.get("chat") if isinstance(detail.get("chat"), dict) else {}
    history = chat.get("history") if isinstance(chat.get("history"), dict) else {}
    raw = history.get("messages")
    if not isinstance(raw, dict):
        return []
    chain: list[dict[str, Any]] = []
    visited: set[str] = set()
    node = history.get("currentId")
    node = str(node) if isinstance(node, str) else ""
    while node and node not in visited and isinstance(raw.get(node), dict):
        visited.add(node)
        item = raw[node]
        chain.append(item)
        parent = item.get("parentId")
        node = str(parent) if isinstance(parent, str) and parent else ""
    if chain:
        chain.reverse()  # 回溯得到的是新→旧，反转为时间正序
    else:
        chain = [item for item in raw.values() if isinstance(item, dict)]
        chain.sort(key=lambda item: item.get("timestamp") or 0)  # 已是时间正序
    messages: list[dict[str, Any]] = []
    for item in chain:
        role = str(item.get("role") or "").strip().lower()
        if not role:
            continue
        messages.append(
            {
                "role": role,
                "content": normalize_history_message_content(item.get("content")),
                "timestamp": item.get("timestamp"),
            }
        )
    return messages


# ---------------------------------------------------------------------------
# 本地请求镜像（对齐 ds2api 的 chathistory）：每条 API/面板请求在开始流式前写入
# 一条 "streaming" 记录（含完整出站消息、文件清单、上下文、最终 prompt），结束时
# 更新为 success/stopped/error + 回复 + thinking + 耗时。上游不保存 API 会话的
# 助手回复，历史页完全依赖这份镜像回看；文件只记元数据（文件名/大小），绝不落内容。
# history.local.json 在 gitignore 中，绝不入库。
# ---------------------------------------------------------------------------
HISTORY_STORE_PATH = Path(__file__).resolve().parent / "history.local.json"
# v3 存储（对齐 ds2api store.go）：索引文件只存记录 id 顺序，完整记录在
# history.local.json.d/<id>.json；start/finish 只重写单条 detail + 小索引，
# 避免 v2 单文件全量重写（重度使用可达 ~10MB/次）的写放大。
HISTORY_DETAIL_DIR = HISTORY_STORE_PATH.parent / (HISTORY_STORE_PATH.name + ".d")
HISTORY_SCHEMA = "glm2api.history.v4"
# 运行时可经设置页调整（settings: history_max_records, 50-2000）
_HISTORY_CONF = {"max_records": 300}
# 兼容旧测试/外部引用的只读别名
HISTORY_MAX_RECORDS = 300
HISTORY_PROMPT_CHARS = 8_000
HISTORY_MSG_CHARS = 6_000
HISTORY_MESSAGES_MAX = 30
HISTORY_CONTEXT_CHARS = 16_000
HISTORY_CONTEXT_FILES_MAX = 4
HISTORY_CONTEXT_FILE_CHARS = 80_000
HISTORY_FINAL_CHARS = 40_000
HISTORY_ANSWER_CHARS = 30_000
HISTORY_THINKING_CHARS = 20_000
HISTORY_PREVIEW_CHARS = 160
HISTORY_FILES_MAX = 20
# 详情页每 750ms 轮询一次；按同频率持久化即可保持实时感，同时避免此前
# 250ms 频率造成 detail + 完整索引每秒约 8 次原子替换。
HISTORY_PROGRESS_INTERVAL_SECONDS = 0.75
_HISTORY_LOCK = threading.Lock()
_HISTORY_CACHE: list[dict[str, Any]] | None = None
_HISTORY_DIRTY: set[str] = set()
_HISTORY_DELETED: set[str] = set()


def history_display_content(content: Any) -> str:
    """把请求消息内容拍平成展示文本；非文本块（图片/文件/音频）保留占位标记。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        parts = [history_display_content(item) for item in content]
        return "\n".join(part for part in parts if part)
    if not isinstance(content, dict):
        return protocol_content_text(content)
    item_type = str(content.get("type") or "").strip().lower()
    if item_type in {"image_url", "input_image", "image", "img_url"}:
        return "[图片]"
    if item_type in {"file", "input_file", "document"}:
        name = str(content.get("name") or content.get("filename") or content.get("file_id") or "").strip()
        return f"[文件: {name}]" if name else "[文件]"
    if item_type in {"input_audio", "audio"}:
        return "[音频]"
    return protocol_content_text(content)


def history_messages_snapshot(messages: Any) -> list[dict[str, str]]:
    """出站消息快照：保留最后 N 条、逐条截断，作为历史详情的「对话框发了什么」。"""
    snapshot: list[dict[str, str]] = []
    for item in list(messages or [])[-HISTORY_MESSAGES_MAX:]:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip().lower()
        text = history_display_content(item.get("content")).strip()
        if not role or not text:
            continue
        snapshot.append({"role": role, "content": text[:HISTORY_MSG_CHARS]})
    return snapshot


def history_files_snapshot(files: Any) -> list[dict[str, Any]]:
    """随请求发出的文件清单（仅元数据：文件名/大小/类型）。"""
    snapshot: list[dict[str, Any]] = []
    for item in list(files or [])[:HISTORY_FILES_MAX]:
        if not isinstance(item, dict):
            continue
        file_obj = item.get("file") if isinstance(item.get("file"), dict) else item
        meta = file_obj.get("meta") if isinstance(file_obj.get("meta"), dict) else {}
        name = str(
            item.get("name")
            or item.get("filename")
            or item.get("file_name")
            or file_obj.get("name")
            or file_obj.get("filename")
            or meta.get("name")
            or ""
        ).strip()
        if not name:
            continue
        try:
            size = int(float(item.get("size") or file_obj.get("size") or meta.get("size") or 0))
        except (TypeError, ValueError):
            size = 0
        snapshot.append(
            {
                "name": name[:200],
                "size": max(0, size),
                "content_type": str(
                    item.get("content_type")
                    or item.get("mime")
                    or file_obj.get("content_type")
                    or file_obj.get("mime")
                    or meta.get("content_type")
                    or ""
                )[:100],
            }
        )
    return snapshot


def history_context_files_snapshot(files: Any) -> list[dict[str, Any]]:
    """内部历史拆分附件快照：保留名称、用途和可审计正文，并明确标记截断。"""
    snapshot: list[dict[str, Any]] = []
    for item in list(files or [])[:HISTORY_CONTEXT_FILES_MAX]:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "")
        original_chars = len(content)
        try:
            size = int(item.get("size") or len(content.encode("utf-8")))
        except (TypeError, ValueError):
            size = len(content.encode("utf-8"))
        snapshot.append(
            {
                "kind": str(item.get("kind") or "context")[:40],
                "name": str(item.get("name") or "context.txt")[:200],
                "size": max(0, size),
                "content_type": str(item.get("content_type") or "text/plain; charset=utf-8")[:100],
                "content": content[:HISTORY_CONTEXT_FILE_CHARS],
                "original_chars": original_chars,
                "truncated": original_chars > HISTORY_CONTEXT_FILE_CHARS,
            }
        )
    return snapshot


def history_delivery_mode(record: dict[str, Any]) -> str:
    """返回实际实发模式；旧记录通过已有字段推断，不触发存储迁移。"""
    explicit = str(record.get("delivery_mode") or "").strip().lower()
    if explicit in {"inline", "file"}:
        return explicit
    if record.get("context_files"):
        return "file"
    final_prompt = str(record.get("final_prompt") or "").lower()
    if record.get("history_text") and "attached file holds the earlier conversation" in final_prompt:
        return "file"
    return "inline"


def _history_record_title(user_input: str) -> str:
    return (str(user_input or "").strip().splitlines() or ["本地会话"])[0][:40] or "本地会话"


def _history_detail_path(record_id: str) -> Path:
    return HISTORY_DETAIL_DIR / f"{record_id}.json"


def _history_read_detail_locked(record_id: str) -> dict[str, Any] | None:
    try:
        env = json.loads(_history_detail_path(record_id).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception as exc:
        log_event("history_store_detail_read_error", record_id_fp=sha16(record_id), error=str(exc)[:200])
        return None
    if isinstance(env, dict) and isinstance(env.get("record"), dict):
        return env["record"]
    return None


def _history_load_locked() -> None:
    global _HISTORY_CACHE
    try:
        HISTORY_DETAIL_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        log_event("history_store_dir_error", error=str(exc)[:200])
    raw = None
    try:
        raw = json.loads(HISTORY_STORE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        pass
    except Exception as exc:
        log_event("history_store_index_read_error", error=str(exc)[:200])

    if isinstance(raw, dict) and str(raw.get("schema") or "") in {HISTORY_SCHEMA, "glm2api.history.v3"}:
        # v3/v4：索引只存摘要（v3 仅 id 顺序），正文读 detail 目录。
        if str(raw.get("schema") or "") == HISTORY_SCHEMA:
            order = [
                str(item.get("id") or "")
                for item in (raw.get("items") or [])
                if isinstance(item, dict)
            ]
        else:
            order = [str(rid) for rid in (raw.get("ids") or []) if isinstance(rid, str)]
        records: list[dict[str, Any]] = []
        for rid in order:
            if not rid:
                continue
            detail = _history_read_detail_locked(rid)
            if detail is not None:
                records.append(detail)
        _HISTORY_CACHE = records
        if str(raw.get("schema") or "") != HISTORY_SCHEMA and records:
            # 旧 v3 ids 索引一次性升级为 v4 摘要索引。
            _HISTORY_DIRTY.update(r["id"] for r in records)
            _history_persist_locked()
            log_event("history_store_migrated", count=len(records))
        return

    # 旧格式（v2 单文件 / v1 单文件）整体迁移到 v3。
    items: list[dict[str, Any]] = []
    if isinstance(raw, dict) and isinstance(raw.get("records"), list):
        items = [item for item in raw["records"] if isinstance(item, dict)]
        if str(raw.get("schema") or "") != "glm2api.history.v2":
            items = [_adapt_v1_history_record(item) for item in items]
    _HISTORY_CACHE = items
    if items:
        _HISTORY_DIRTY.update(str(item.get("id") or "") for item in items)
        _history_persist_locked()
        log_event("history_store_migrated", count=len(items))


def _history_records_locked() -> list[dict[str, Any]]:
    global _HISTORY_CACHE
    if _HISTORY_CACHE is None:
        _HISTORY_CACHE = []
        try:
            _history_load_locked()
        except Exception as exc:
            log_event("history_store_load_error", error=str(exc)[:200])
    return _HISTORY_CACHE


def _history_find_locked(record_id: str) -> dict[str, Any] | None:
    """Find a record newest-first; streaming updates almost always target the tail."""
    record_id = str(record_id or "")
    if not record_id:
        return None
    return next(
        (record for record in reversed(_history_records_locked()) if str(record.get("id") or "") == record_id),
        None,
    )


def _adapt_v1_history_record(record: dict[str, Any]) -> dict[str, Any]:
    """v1（单轮 prompt/answer/thinking）→ v2 请求记录，老镜像数据不丢。"""
    answer = str(record.get("answer") or "")
    prompt = str(record.get("prompt") or "")
    ts = int(record.get("timestamp") or 0) * 1000
    return {
        "id": "req_" + uuid.uuid4().hex[:24],
        "chat_id": str(record.get("chat_id") or ""),
        "status": "success" if answer.strip() else "error",
        "surface": "",
        "model": str(record.get("model") or ""),
        "stream": True,
        "created_at": ts,
        "updated_at": ts,
        "completed_at": ts,
        "elapsed_ms": 0,
        "status_code": 200 if answer.strip() else 0,
        "title": str(record.get("title") or _history_record_title(prompt)),
        "user_input": prompt,
        "messages": [
            {"role": "user", "content": prompt[:HISTORY_MSG_CHARS]},
            {"role": "assistant", "content": answer[:HISTORY_MSG_CHARS]},
        ],
        "files": [],
        "delivery_mode": "inline",
        "context_file_requested": False,
        "context_file_fallback": "",
        "context_files": [],
        "history_text": "",
        "final_prompt": prompt[:HISTORY_FINAL_CHARS],
        "reasoning": str(record.get("thinking") or "")[:HISTORY_THINKING_CHARS],
        "content": answer[:HISTORY_ANSWER_CHARS],
        "error": "",
        "finish_reason": "stop" if answer.strip() else "",
    }


def _history_summary_locked(record: dict[str, Any]) -> dict[str, Any]:
    """索引内嵌的摘要条目（ds2api SummaryEntry 同构）：列表展示不必读 detail 文件。"""
    return {
        "id": str(record.get("id") or ""),
        "chat_id": str(record.get("chat_id") or ""),
        "status": str(record.get("status") or ""),
        "surface": str(record.get("surface") or ""),
        "model": str(record.get("model") or ""),
        "account": str(record.get("account") or ""),
        "caller": str(record.get("caller") or ""),
        "stream": bool(record.get("stream")),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "completed_at": record.get("completed_at") or 0,
        "elapsed_ms": record.get("elapsed_ms") or 0,
        "status_code": record.get("status_code") or 0,
        "title": str(record.get("title") or ""),
        "user_input": str(record.get("user_input") or ""),
        "preview": history_preview(record),
        "error": str(record.get("error") or "")[:200],
        "files": len(record.get("files") or []),
        "delivery_mode": history_delivery_mode(record),
        "context_file_requested": bool(record.get("context_file_requested")),
        "context_file_fallback": str(record.get("context_file_fallback") or ""),
        "context_files": len(record.get("context_files") or []),
        "finish_reason": str(record.get("finish_reason") or ""),
    }


def _history_write_atomic_locked(path: Path, body: str) -> None:
    # Progress snapshots are frequent and terminal state is written again at
    # stream end. Atomic replace prevents torn JSON; skipping fsync here avoids
    # two forced disk flushes per progress tick. Settings/credentials keep the
    # durable default.
    atomic_write_text(path, body, durable=False)


def _history_persist_locked() -> None:
    """脏 detail 单条重写 + 小索引；删除的 detail 文件一并清理。"""
    try:
        HISTORY_DETAIL_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        log_event("history_store_dir_error", error=str(exc)[:200])
    deleted_done: set[str] = set()
    dirty_done: set[str] = set()
    for rid in sorted(_HISTORY_DELETED):
        try:
            _history_detail_path(rid).unlink(missing_ok=True)
        except Exception as exc:
            log_event("history_store_detail_delete_error", record_id_fp=sha16(rid), error=str(exc)[:200])
        else:
            deleted_done.add(rid)
    dirty_ids = sorted(_HISTORY_DIRTY)
    # Progress writes usually touch the newest record only; migrations can dirty
    # hundreds at once. Build an index only for the latter to avoid O(n²).
    records_by_id = (
        {str(record.get("id") or ""): record for record in _HISTORY_CACHE}
        if len(dirty_ids) > 1
        else None
    )
    dirty_failed = False
    for rid in dirty_ids:
        record = records_by_id.get(rid) if records_by_id is not None else _history_find_locked(rid)
        if record is None:
            dirty_done.add(rid)
            continue
        try:
            payload = json.dumps({"schema": HISTORY_SCHEMA, "record": record}, ensure_ascii=False)
            _history_write_atomic_locked(_history_detail_path(rid), payload)
        except Exception as exc:
            dirty_failed = True
            log_event("history_store_detail_write_error", record_id_fp=sha16(rid), error=str(exc)[:200])
        else:
            dirty_done.add(rid)
    # Never publish an index whose referenced detail write failed. Keep every
    # pending marker so the next history mutation retries the complete batch.
    if dirty_failed:
        return
    index = json.dumps(
        {
            "schema": HISTORY_SCHEMA,
            "limit": max(1, int(_HISTORY_CONF.get("max_records") or 300)),
            "updated": int(time.time() * 1000),
            "items": [_history_summary_locked(r) for r in _HISTORY_CACHE],
        },
        ensure_ascii=False,
    )
    try:
        _history_write_atomic_locked(HISTORY_STORE_PATH, index)
    except Exception as exc:
        log_event("history_store_index_write_error", error=str(exc)[:200])
        return
    _HISTORY_DIRTY.difference_update(dirty_done)
    _HISTORY_DELETED.difference_update(deleted_done)


def _history_upsert_locked(record: dict[str, Any]) -> None:
    records = _history_records_locked()
    records.append(record)
    cap = max(1, int(_HISTORY_CONF.get("max_records") or 300))
    if len(records) > cap:
        for old in records[: len(records) - cap]:
            _HISTORY_DELETED.add(str(old.get("id") or ""))
        del records[: len(records) - cap]


def start_history_record(
    *,
    surface: str = "",
    model: str = "",
    stream: bool = True,
    user_input: str = "",
    messages: Any = None,
    files: Any = None,
    context_text: str = "",
    final_prompt: str = "",
    delivery_mode: str = "inline",
    context_file_requested: bool = False,
    context_file_fallback: str = "",
    context_files: Any = None,
    chat_id: str = "",
    account: str = "",
) -> str:
    """请求开始时落一条 streaming 记录，返回记录 id；任何失败只记日志不影响主流程。"""
    user_input = str(user_input or "").strip()
    now_ms = int(time.time() * 1000)
    # ds2api caller_id 同位：记录请求从哪个入口进来（面板 / CLI / API Key 接入）。
    caller = {"panel_chat": "panel", "cli_direct": "cli"}.get(str(surface or ""), "api")
    record = {
        "id": "req_" + uuid.uuid4().hex[:24],
        "chat_id": str(chat_id or ""),
        "status": "streaming",
        "surface": str(surface or ""),
        "model": str(model or ""),
        "account": str(account or ""),
        "caller": caller,
        "stream": bool(stream),
        "created_at": now_ms,
        "updated_at": now_ms,
        "completed_at": 0,
        "elapsed_ms": 0,
        "status_code": 0,
        "title": _history_record_title(user_input),
        "user_input": user_input[:HISTORY_PROMPT_CHARS] if user_input else "",
        "messages": history_messages_snapshot(messages),
        "files": history_files_snapshot(files),
        "delivery_mode": "file" if str(delivery_mode).lower() == "file" else "inline",
        "context_file_requested": bool(context_file_requested),
        "context_file_fallback": str(context_file_fallback or "")[:120],
        "context_files": history_context_files_snapshot(context_files),
        "history_text": str(context_text or "")[:HISTORY_CONTEXT_CHARS],
        "final_prompt": str(final_prompt or "")[:HISTORY_FINAL_CHARS],
        "reasoning": "",
        "content": "",
        "error": "",
        "finish_reason": "",
    }
    try:
        with _HISTORY_LOCK:
            _history_upsert_locked(record)
            _HISTORY_DIRTY.add(str(record["id"]))
            _history_persist_locked()
        return str(record["id"])
    except Exception as exc:
        log_event("history_store_write_error", stage="start", error=str(exc)[:200])
        return ""


def update_history_progress(
    record_id: str,
    *,
    content: str = "",
    reasoning: str = "",
    status_code: int = 0,
    elapsed_ms: int = 0,
) -> None:
    """流式进行中把已读到的思维链/回复节流落盘（ds2api progress 同款），
    让"历史"页打开生成中的记录即可实时看到内容；失败只记日志不影响主流程。"""
    if not record_id:
        return
    now_ms = int(time.time() * 1000)
    try:
        with _HISTORY_LOCK:
            record = _history_find_locked(record_id)
            if record is None:
                return
            record["content"] = str(content or "")[:HISTORY_ANSWER_CHARS]
            record["reasoning"] = str(reasoning or "")[:HISTORY_THINKING_CHARS]
            if status_code:
                record["status_code"] = int(status_code)
            record["elapsed_ms"] = max(0, int(elapsed_ms or 0))
            record["updated_at"] = now_ms
            _HISTORY_DIRTY.add(record_id)
            _history_persist_locked()
    except Exception as exc:
        log_event("history_store_write_error", stage="progress", error=str(exc)[:200])


def restart_history_record(record_id: str, final_prompt: str) -> None:
    """Reuse one request mirror when a semantic tool-format retry is sampled."""
    if not record_id:
        return
    try:
        with _HISTORY_LOCK:
            record = _history_find_locked(record_id)
            if record is None:
                return
            record["status"] = "streaming"
            record["final_prompt"] = str(final_prompt or "")[:HISTORY_FINAL_CHARS]
            record["reasoning"] = ""
            record["content"] = ""
            record["error"] = ""
            record["finish_reason"] = ""
            record["updated_at"] = int(time.time() * 1000)
            _HISTORY_DIRTY.add(record_id)
            _history_persist_locked()
    except Exception as exc:
        log_event("history_store_write_error", stage="restart", error=str(exc)[:200])


def finish_history_record(
    record_id: str,
    *,
    status: str = "success",
    content: str = "",
    reasoning: str = "",
    error: str = "",
    elapsed_ms: int = 0,
    chat_id: str = "",
    status_code: int = 0,
    finish_reason: str = "",
) -> None:
    """结束时更新记录为终态；找不到记录（如被淘汰出上限）时静默放弃。"""
    if not record_id:
        return
    now_ms = int(time.time() * 1000)
    try:
        with _HISTORY_LOCK:
            record = _history_find_locked(record_id)
            if record is None:
                return
            if chat_id:
                record["chat_id"] = str(chat_id)
            record["status"] = str(status or "success")
            record["content"] = str(content or "")[:HISTORY_ANSWER_CHARS]
            record["reasoning"] = str(reasoning or "")[:HISTORY_THINKING_CHARS]
            record["error"] = str(error or "")[:500]
            record["elapsed_ms"] = max(0, int(elapsed_ms or 0))
            record["status_code"] = int(status_code or (200 if status in {"success", "stopped"} else 0))
            record["finish_reason"] = str(finish_reason or ("stop" if status == "success" else ""))
            prompt_basis = str(record.get("final_prompt") or record.get("user_input") or "")
            for context_file in record.get("context_files") or []:
                if isinstance(context_file, dict):
                    prompt_basis += "\n" + str(context_file.get("content") or "")
            prompt_tokens = estimate_protocol_tokens(prompt_basis)
            reasoning_tokens = estimate_protocol_tokens(record.get("reasoning"))
            completion_tokens = estimate_protocol_tokens(record.get("content"))
            record["usage"] = {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "reasoning_tokens": reasoning_tokens,
                "total_tokens": prompt_tokens + completion_tokens + reasoning_tokens,
            }
            record["updated_at"] = now_ms
            if status in {"success", "error", "stopped"}:
                record["completed_at"] = now_ms
            _HISTORY_DIRTY.add(record_id)
            _history_persist_locked()
        log_event(
            "history_recorded",
            record_id_fp=sha16(record_id),
            chat_id_fp=sha16(str(record.get("chat_id") or "")) if record.get("chat_id") else "",
            status=str(status),
            content_chars=len(str(content or "")),
        )
    except Exception as exc:
        log_event("history_store_write_error", stage="finish", error=str(exc)[:200])


def history_preview(record: dict[str, Any]) -> str:
    """列表预览：回复 → 思维链 → 错误 → 用户输入，与 ds2api buildPreview 同序。"""
    for key in ("content", "reasoning", "error", "user_input"):
        text = str(record.get(key) or "").strip()
        if text:
            return text[:HISTORY_PREVIEW_CHARS] + ("…" if len(text) > HISTORY_PREVIEW_CHARS else "")
    return ""


def local_history_records() -> list[dict[str, Any]]:
    """只读快照，按写入顺序（旧→新）返回。"""
    with _HISTORY_LOCK:
        return list(_history_records_locked())


def local_history_metrics(hours: int = 24, now_ms: int | None = None) -> dict[str, Any]:
    """Aggregate retained request mirrors without copying prompt/reply content."""
    hours = max(1, min(int(hours or 24), 24 * 30))
    current_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    window_ms = hours * 60 * 60 * 1000
    if hours <= 2:
        bucket_ms = 5 * 60 * 1000
    elif hours <= 8:
        bucket_ms = 15 * 60 * 1000
    elif hours <= 48:
        bucket_ms = 60 * 60 * 1000
    elif hours <= 24 * 7:
        bucket_ms = 6 * 60 * 60 * 1000
    else:
        bucket_ms = 24 * 60 * 60 * 1000
    bucket_count = max(1, min(48, (window_ms + bucket_ms - 1) // bucket_ms))
    end_bucket_ms = (current_ms // bucket_ms) * bucket_ms
    start_bucket_ms = end_bucket_ms - (bucket_count - 1) * bucket_ms
    bucket_rows: list[dict[str, Any]] = []
    bucket_by_start: dict[int, dict[str, Any]] = {}
    for index in range(bucket_count):
        started = start_bucket_ms + index * bucket_ms
        row = {
            "start_ms": started,
            "start": datetime.fromtimestamp(started / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
            "total": 0,
            "success": 0,
            "error": 0,
            "stopped": 0,
            "streaming": 0,
            "elapsed_total_ms": 0,
            "elapsed_samples": 0,
        }
        bucket_rows.append(row)
        bucket_by_start[started] = row

    with _HISTORY_LOCK:
        records = _history_records_locked()
        retained_total = len(records)
        rows = [
            {
                "status": str(record.get("status") or ""),
                "surface": str(record.get("surface") or "unknown"),
                "caller": str(record.get("caller") or "unknown"),
                "model": str(record.get("model") or "unknown"),
                "created_at": int(record.get("created_at") or 0),
                "elapsed_ms": max(0, int(record.get("elapsed_ms") or 0)),
                "status_code": max(0, int(record.get("status_code") or 0)),
                "delivery_mode": history_delivery_mode(record),
                "fallback": bool(record.get("context_file_fallback")),
                "usage": dict(record.get("usage") or {}) if isinstance(record.get("usage"), dict) else {},
            }
            for record in records
        ]

    status_counts: Counter[str] = Counter()
    surface_counts: Counter[str] = Counter()
    caller_counts: Counter[str] = Counter()
    model_rows: dict[str, dict[str, Any]] = {}
    durations: list[int] = []
    token_totals = {"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0, "total_tokens": 0}
    file_delivery_requests = 0
    fallback_requests = 0
    latest_at = 0
    window_rows = 0
    for row in rows:
        created_at = int(row["created_at"])
        if created_at < start_bucket_ms or created_at > current_ms + bucket_ms:
            continue
        bucket_start = (created_at // bucket_ms) * bucket_ms
        bucket = bucket_by_start.get(bucket_start)
        if bucket is None:
            continue
        window_rows += 1
        latest_at = max(latest_at, created_at)
        status = str(row["status"] or "unknown")
        status_counts[status] += 1
        surface_counts[str(row["surface"])] += 1
        caller_counts[str(row["caller"])] += 1
        bucket["total"] += 1
        if status in {"success", "error", "stopped", "streaming"}:
            bucket[status] += 1
        elapsed_ms = int(row["elapsed_ms"])
        if elapsed_ms > 0:
            durations.append(elapsed_ms)
            bucket["elapsed_total_ms"] += elapsed_ms
            bucket["elapsed_samples"] += 1
        if row["delivery_mode"] == "file":
            file_delivery_requests += 1
        if row["fallback"]:
            fallback_requests += 1
        usage = row["usage"]
        for key in token_totals:
            token_totals[key] += max(0, int(usage.get(key) or 0))
        model = str(row["model"])
        model_item = model_rows.setdefault(
            model,
            {"model": model, "count": 0, "success": 0, "error": 0, "stopped": 0, "tokens": 0, "elapsed_total_ms": 0, "elapsed_samples": 0},
        )
        model_item["count"] += 1
        if status in {"success", "error", "stopped"}:
            model_item[status] += 1
        model_item["tokens"] += max(0, int(usage.get("total_tokens") or 0))
        if elapsed_ms > 0:
            model_item["elapsed_total_ms"] += elapsed_ms
            model_item["elapsed_samples"] += 1

    for bucket in bucket_rows:
        samples = int(bucket.pop("elapsed_samples"))
        total_elapsed = int(bucket.pop("elapsed_total_ms"))
        bucket["avg_elapsed_ms"] = round(total_elapsed / samples) if samples else 0
    models = []
    for item in model_rows.values():
        samples = int(item.pop("elapsed_samples"))
        total_elapsed = int(item.pop("elapsed_total_ms"))
        item["avg_elapsed_ms"] = round(total_elapsed / samples) if samples else 0
        models.append(item)
    models.sort(key=lambda item: (-int(item["count"]), str(item["model"]).lower()))
    outcomes = int(status_counts.get("success", 0) + status_counts.get("error", 0))
    return {
        "hours": hours,
        "bucket_minutes": bucket_ms // 60_000,
        "retained_total": retained_total,
        "requests": window_rows,
        "latest_at": latest_at,
        "statuses": {name: int(status_counts.get(name, 0)) for name in ("success", "error", "stopped", "streaming")},
        "success_rate": round(status_counts.get("success", 0) / outcomes, 4) if outcomes else 0.0,
        "avg_elapsed_ms": round(sum(durations) / len(durations)) if durations else 0,
        "p50_elapsed_ms": _percentile(durations, 0.50),
        "p95_elapsed_ms": _percentile(durations, 0.95),
        "tokens": token_totals,
        "file_delivery_requests": file_delivery_requests,
        "file_delivery_rate": round(file_delivery_requests / window_rows, 4) if window_rows else 0.0,
        "fallback_requests": fallback_requests,
        "models": models[:12],
        "surfaces": [{"surface": name, "count": count} for name, count in surface_counts.most_common(12)],
        "callers": dict(caller_counts),
        "timeline": bucket_rows,
    }


def local_history_summary(text: str = "", status: str = "") -> list[dict[str, Any]]:
    """列表用摘要（新→旧），不含大字段；text 匹配标题/预览/模型/账号，status 精确匹配。"""
    needle = str(text or "").strip().lower()
    status_filter = str(status or "").strip().lower()
    records = local_history_records()
    summaries = []
    for record in reversed(records):
        summary = {
            "id": str(record.get("id") or ""),
            "chat_id": str(record.get("chat_id") or ""),
            "status": str(record.get("status") or ""),
            "surface": str(record.get("surface") or ""),
            "model": str(record.get("model") or ""),
            "account": str(record.get("account") or ""),
            "caller": str(record.get("caller") or ""),
            "stream": bool(record.get("stream")),
            "created_at": record.get("created_at"),
            "updated_at": record.get("updated_at"),
            "elapsed_ms": record.get("elapsed_ms") or 0,
            "status_code": record.get("status_code") or 0,
            "title": str(record.get("title") or ""),
            "preview": history_preview(record),
            "error": str(record.get("error") or "")[:200],
            "files": len(record.get("files") or []),
            "delivery_mode": history_delivery_mode(record),
            "context_file_requested": bool(record.get("context_file_requested")),
            "context_file_fallback": str(record.get("context_file_fallback") or ""),
            "context_files": len(record.get("context_files") or []),
            "finish_reason": str(record.get("finish_reason") or ""),
        }
        if status_filter and summary["status"] != status_filter:
            continue
        if needle:
            haystack = " ".join(
                (
                    summary["title"],
                    summary["preview"],
                    summary["model"],
                    summary["account"],
                    summary["surface"],
                )
            ).lower()
            if needle not in haystack:
                continue
        summaries.append(summary)
    return summaries


def local_history_summary_page(
    text: str = "",
    status: str = "",
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[dict[str, Any]], int]:
    """Return one newest-first summary page without materializing every summary.

    The history UI refreshes unfiltered page 1 frequently. In that common path
    only the requested records are converted; filtered searches still scan all
    records so their total remains exact, but retain at most one page of output.
    """
    needle = str(text or "").strip().lower()
    status_filter = str(status or "").strip().lower()
    page = max(1, int(page or 1))
    page_size = max(1, int(page_size or 1))
    start = (page - 1) * page_size
    records = local_history_records()
    if not needle and not status_filter:
        total = len(records)
        end_from_tail = min(total, start + page_size)
        if start >= total:
            return [], total
        selected = records[total - end_from_tail : total - start]
        return [_history_summary_locked(record) for record in reversed(selected)], total

    items: list[dict[str, Any]] = []
    total = 0
    for record in reversed(records):
        summary = _history_summary_locked(record)
        if status_filter and summary["status"] != status_filter:
            continue
        if needle:
            haystack = " ".join(
                (
                    summary["title"],
                    summary["preview"],
                    summary["model"],
                    summary["account"],
                    summary["surface"],
                )
            ).lower()
            if needle not in haystack:
                continue
        if total >= start and len(items) < page_size:
            items.append(summary)
        total += 1
    return items, total


def get_local_history_record(record_id: str) -> dict[str, Any] | None:
    record_id = str(record_id or "")
    with _HISTORY_LOCK:
        record = _history_find_locked(record_id)
        if record is not None:
            return json.loads(json.dumps(record, ensure_ascii=False))
    return None


def purge_local_history(chat_id: str) -> int:
    """删除指定会话的全部本地镜像记录，返回移除条数。"""
    chat_id = str(chat_id or "")
    try:
        with _HISTORY_LOCK:
            records = _history_records_locked()
            kept = [r for r in records if str(r.get("chat_id") or "") != chat_id]
            removed = len(records) - len(kept)
            if removed:
                _HISTORY_DELETED.update(str(r.get("id") or "") for r in records if str(r.get("chat_id") or "") == chat_id)
                _HISTORY_CACHE[:] = kept
                _history_persist_locked()
            return removed
    except Exception as exc:
        log_event("history_store_write_error", error=str(exc)[:200])
        return 0


def purge_history_record(record_id: str) -> int:
    """删除单条请求镜像记录，返回移除条数（0/1）。"""
    record_id = str(record_id or "")
    try:
        with _HISTORY_LOCK:
            records = _history_records_locked()
            kept = [r for r in records if str(r.get("id") or "") != record_id]
            removed = len(records) - len(kept)
            if removed:
                _HISTORY_DELETED.add(record_id)
                _HISTORY_CACHE[:] = kept
                _history_persist_locked()
            return removed
    except Exception as exc:
        log_event("history_store_write_error", error=str(exc)[:200])
        return 0


def clear_local_history() -> int:
    """清空全部请求镜像记录，返回移除条数。"""
    try:
        with _HISTORY_LOCK:
            records = _history_records_locked()
            removed = len(records)
            _HISTORY_DELETED.update(str(r.get("id") or "") for r in records)
            _HISTORY_CACHE.clear()
            _history_persist_locked()
            return removed
    except Exception as exc:
        log_event("history_store_write_error", error=str(exc)[:200])
        return 0


def delete_zai_file(state: HarState, file_id: str) -> bool:
    """Best-effort removal of an uploaded file that never reached a chat.

    The upstream file deletion endpoint is optional: 404 means the file is
    already gone and 405 means the deployment has no deletion support; both
    are treated as a completed no-op. Returns False only for a 405 no-op,
    True when the file is gone or deleted.
    """
    file_id = str(file_id or "").strip()
    if not file_id or any(ch.isspace() for ch in file_id) or "/" in file_id or "\\" in file_id:
        raise ValueError("invalid file id")
    last_error = ""
    for with_auth in (False, True):
        try:
            result = http_json(
                "DELETE",
                f"{BASE_URL}/api/v1/files/{file_id}",
                chat_control_headers(state, with_auth=with_auth),
            )
        except HTTPError as exc:
            if exc.code in {404, 405}:
                code = exc.code
                try:
                    exc.close()
                except Exception:
                    pass
                return code != 405
            last_error = http_error_summary(exc)
            if exc.code in {401, 403} and not with_auth:
                continue
            raise RuntimeError(f"删除附件失败: {last_error}") from exc
        if result is None or result is True or isinstance(result, dict):
            return True
        last_error = str(result)[:300]
        break
    raise RuntimeError(f"删除附件失败: {last_error or 'unknown error'}")


def stop_zai_task(state: HarState, assistant_message_id: str) -> dict[str, Any]:
    assistant_message_id = require_uuid(assistant_message_id, "assistant_message_id")
    last_error = ""
    for with_auth in (False, True):
        try:
            result = http_json(
                "POST",
                f"{BASE_URL}/api/tasks/stop/{assistant_message_id}",
                chat_control_headers(state, with_auth=with_auth),
                {},
            )
        except HTTPError as exc:
            last_error = http_error_summary(exc)
            if exc.code in {401, 403} and not with_auth:
                continue
            raise RuntimeError(f"停止生成失败: {last_error}") from exc
        if isinstance(result, dict) and coerce_bool(result.get("status"), False):
            return result
        last_error = str(result)[:300]
        break
    raise RuntimeError(f"停止生成失败: {last_error or 'unknown error'}")


def safe_filename(name: str, fallback: str = "upload.bin") -> str:
    cleaned = Path(str(name or fallback).replace("\\", "/")).name
    cleaned = "".join(ch for ch in cleaned if ch.isprintable() and ch not in {'"', "\r", "\n"})
    cleaned = cleaned.strip(" .")
    return cleaned[:160] or fallback


def guess_content_type(filename: str, content_type: str = "") -> str:
    content_type = str(content_type or "").split(";", 1)[0].strip()
    if content_type and content_type.lower() not in {"application/octet-stream", "binary/octet-stream"}:
        return content_type
    guessed, _encoding = mimetypes.guess_type(filename)
    return guessed or content_type or "application/octet-stream"


def multipart_file_parts(filename: str, content_type: str) -> tuple[str, bytes, bytes]:
    # Official front-end uses the WebKit boundary style: ----WebKitFormBoundary + 16
    # mixed-case alphanumerics (e.g. ----WebKitFormBoundaryccAYOxFe7ba9DaJ2).
    alphabet = string.ascii_letters + string.digits
    boundary = "----WebKitFormBoundary" + "".join(secrets.choice(alphabet) for _ in range(16))
    disposition = f'Content-Disposition: form-data; name="file"; filename="{filename}"'
    head = (
        f"--{boundary}\r\n"
        f"{disposition}\r\n"
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode("utf-8")
    tail = f"\r\n--{boundary}--\r\n".encode("ascii")
    return boundary, head, tail


def multipart_file_body(filename: str, content_type: str, raw: bytes) -> tuple[str, bytes]:
    boundary, head, tail = multipart_file_parts(filename, content_type)
    return boundary, head + raw + tail


def upload_file_headers(state: HarState, boundary: str, with_auth: bool = False) -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "Accept-Language": control_accept_language(state),
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Origin": BASE_URL,
        "User-Agent": state.user_agent,
        "X-Region": state.region,
    }
    headers.update(browser_client_hint_headers(state))
    if with_auth:
        headers["Authorization"] = f"Bearer {state.token}"
        headers["X-FE-Version"] = state.fe_version
        if state.device_id:
            headers["X-Device-ID"] = state.device_id
    return headers


def _upload_file_stream_to_zai(
    state: HarState,
    filename: str,
    content_type: str,
    content_length: int,
    chunk_factory: Callable[[], Iterable[bytes]],
) -> dict[str, Any]:
    """Send multipart data in chunks so a local upload is never duplicated in RAM."""
    filename = safe_filename(filename)
    content_type = guess_content_type(filename, content_type)
    boundary, head, tail = multipart_file_parts(filename, content_type)
    parsed = urlsplit(BASE_URL)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise RuntimeError(f"unsupported BASE_URL for file upload: {BASE_URL}")
    target = f"{parsed.path.rstrip('/')}/api/v1/files/" if parsed.path else "/api/v1/files/"
    total_length = len(head) + content_length + len(tail)
    connection_type = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    last_error = ""
    for with_auth in (False, True):
        conn: http.client.HTTPConnection | None = None
        try:
            conn = connection_type(parsed.hostname, port=port, timeout=180)
            conn.putrequest("POST", target)
            headers = upload_file_headers(state, boundary, with_auth=with_auth)
            headers["Content-Length"] = str(total_length)
            for key, value in headers.items():
                conn.putheader(key, value)
            conn.endheaders()
            conn.send(head)
            sent = 0
            for chunk in chunk_factory():
                if not chunk:
                    continue
                sent += len(chunk)
                conn.send(chunk)
            if sent != content_length:
                raise RuntimeError(f"附件读取长度异常: expected {content_length}, sent {sent}")
            conn.send(tail)
            response = conn.getresponse()
            text = response.read(1024 * 1024).decode("utf-8", errors="replace")
            if 200 <= response.status < 300:
                obj = json_or_none(text)
                if isinstance(obj, dict) and obj.get("id"):
                    return obj
                last_error = f"HTTP {response.status}: 上传文件返回异常: {text[:300]}"
            else:
                last_error = f"HTTP {response.status}: {text[:500]}"
            if response.status in {401, 403} and not with_auth:
                continue
            break
        except (OSError, TimeoutError, http.client.HTTPException) as exc:
            last_error = f"上传连接失败: {exc}"
            break
        finally:
            if conn is not None:
                conn.close()
    raise RuntimeError(f"上传文件失败: {last_error or 'unknown error'}")


def upload_file_to_zai(state: HarState, filename: str, raw: bytes, content_type: str = "") -> dict[str, Any]:
    def byte_chunks() -> Iterable[bytes]:
        for offset in range(0, len(raw), UPLOAD_STREAM_CHUNK_BYTES):
            yield raw[offset : offset + UPLOAD_STREAM_CHUNK_BYTES]

    return _upload_file_stream_to_zai(state, filename, content_type, len(raw), byte_chunks)


def upload_file_path_to_zai(
    state: HarState,
    path: Path,
    filename: str = "",
    content_type: str = "",
) -> dict[str, Any]:
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"附件临时文件不存在: {path}")
    filename = safe_filename(filename or path.name)
    size = path.stat().st_size

    def file_chunks() -> Iterable[bytes]:
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(UPLOAD_STREAM_CHUNK_BYTES), b""):
                yield chunk

    return _upload_file_stream_to_zai(state, filename, content_type, size, file_chunks)


def completion_file_ref(item: dict[str, Any], user_msg_id: str) -> dict[str, Any]:
    file_obj = item.get("file") if isinstance(item.get("file"), dict) else item
    file_id = str(file_obj.get("id") or item.get("id") or "")
    if not file_id:
        raise ValueError("uploaded file object missing id")
    meta = file_obj.get("meta") if isinstance(file_obj.get("meta"), dict) else {}
    name = str(item.get("name") or file_obj.get("filename") or meta.get("name") or file_id)
    size = int(item.get("size") or meta.get("size") or 0)
    return {
        "type": "file",
        "file": file_obj,
        "id": file_id,
        "url": str(item.get("url") or f"/api/v1/files/{file_id}"),
        "name": name,
        "status": str(item.get("status") or "uploaded"),
        "size": size,
        "error": str(item.get("error") or ""),
        "itemId": str(item.get("itemId") or uuid.uuid4()),
        "media": str(item.get("media") or "file"),
        "uploadedAt": int(item.get("uploadedAt") or int(time.time() * 1000)),
        "ref_user_msg_id": user_msg_id,
    }


def chat_files_from_body(body: dict[str, Any]) -> list[dict[str, Any]]:
    value = _first_body_value(body, ("files", "attachments"), [])
    if value is _MISSING or value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("files must be a list")
    files: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, dict):
            files.append(item)
    return files


WEB_HISTORY_MAX_MESSAGES = 20
WEB_HISTORY_MAX_CHARS = 8000


def web_history_from_body(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize web-panel multi-turn history for the upstream messages array."""
    value = _first_body_value(body, ("history",), _MISSING)
    if value is _MISSING or value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("history must be a list")
    cleaned: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip().lower()
        if role not in {"user", "assistant"}:
            continue
        content = strip_parsed_tool_markup(protocol_content_text(item.get("content")))
        if not content:
            continue
        if len(content) > WEB_HISTORY_MAX_CHARS:
            content = content[:WEB_HISTORY_MAX_CHARS] + "…[内容过长已截断]"
        cleaned.append({"role": role, "content": content})
    return cleaned[-WEB_HISTORY_MAX_MESSAGES:]


def new_chat(state: HarState, prompt: str, options: ChatOptions | None = None) -> tuple[str, str]:
    options = options or ChatOptions()
    user_msg_id = str(uuid.uuid4())
    timestamp_sec = int(time.time())
    timestamp_ms = int(time.time() * 1000)
    chat = {
        "id": "",
        "title": "新聊天",
        "models": [options.model],
        "params": {},
        "history": {
            "messages": {
                user_msg_id: {
                    "id": user_msg_id,
                    "parentId": None,
                    "childrenIds": [],
                    "role": "user",
                    "content": prompt,
                    "timestamp": timestamp_sec,
                    "models": [options.model],
                }
            },
            "currentId": user_msg_id,
        },
        "tags": [],
        "flags": [],
        "features": [{"server": "tool_selector_h", "status": "hidden", "type": "tool_selector"}],
        "mcp_servers": [],
        "enable_thinking": options.enable_thinking,
        "reasoning_effort": options.reasoning_effort,
        "auto_web_search": options.auto_web_search,
        "message_version": 1,
        "extra": {},
        "timestamp": timestamp_ms,
        "type": "default",
    }
    headers = {
        "Accept": "application/json",
        "Accept-Language": state.language,
        "Authorization": f"Bearer {state.token}",
        "Content-Type": "application/json",
        "Origin": BASE_URL,
        "User-Agent": state.user_agent,
        "X-FE-Version": state.fe_version,
        "X-Region": state.region,
    }
    headers.update(browser_client_hint_headers(state))
    if state.device_id:
        headers["X-Device-ID"] = state.device_id
    resp = http_json("POST", f"{BASE_URL}/api/v1/chats/new", headers, {"chat": chat})
    if not isinstance(resp, dict) or not resp.get("id"):
        raise RuntimeError(f"创建 chat 失败: {str(resp)[:300]}")
    return str(resp["id"]), user_msg_id


def completion_payload(
    state: HarState,
    prompt: str,
    chat_id: str,
    user_msg_id: str,
    assistant_msg_id: str | None = None,
    captcha_verify_param: str | None = None,
    options: ChatOptions | None = None,
    files: list[dict[str, Any]] | None = None,
    history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    options = options or ChatOptions()
    features = {
        "image_generation": False,
        "web_search": False,
        "auto_web_search": options.auto_web_search,
        "preview_mode": True,
        "flags": [],
        "vlm_tools_enable": False,
        "vlm_web_search_enable": False,
        "vlm_website_mode": False,
        "enable_thinking": options.enable_thinking,
    }
    # 新版前端对 glm-5.3 与 x-preview-l 都在 features 里携带 reasoning_effort。
    if options.enable_thinking and options.model in (DEFAULT_MODEL, FLASH_MODEL):
        features["reasoning_effort"] = options.reasoning_effort

    messages: list[dict[str, Any]] = []
    for item in history or []:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "user").strip().lower()
        if role not in {"user", "assistant"}:
            continue
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        if len(content) > WEB_HISTORY_MAX_CHARS:
            content = content[:WEB_HISTORY_MAX_CHARS] + "…[内容过长已截断]"
        messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": prompt})
    payload = {
        "stream": True,
        "model": options.model,
        "messages": messages,
        "signature_prompt": prompt,
        "params": {},
        "extra": {},
        "features": features,
        "variables": browser_variables(state.user_name, state.language),
        "chat_id": chat_id,
        "id": assistant_msg_id or str(uuid.uuid4()),
        "current_user_message_id": user_msg_id,
        "current_user_message_parent_id": None,
        "background_tasks": {"title_generation": True, "tags_generation": True},
    }
    captcha = captcha_verify_param or state.captcha_verify_param
    if captcha:
        payload["captcha_verify_param"] = captcha
    if files:
        payload["files"] = [completion_file_ref(item, user_msg_id) for item in files]
    return payload


def stream_zai_completion(
    state: HarState,
    prompt: str,
    create_chat: bool = True,
    chat_id: str | None = None,
    user_msg_id: str | None = None,
    assistant_msg_id: str | None = None,
    captcha_verify_param: str | None = None,
    fresh_captcha_browser: bool = False,
    chrome_path: str | None = None,
    captcha_headless: bool = True,
    captcha_timeout_ms: int = 75_000,
    upstream_timeout_sec: int | None = None,
    options: ChatOptions | None = None,
    context_out: dict[str, Any] | None = None,
    files: list[dict[str, Any]] | None = None,
    history: list[dict[str, Any]] | None = None,
    history_ctx: dict[str, Any] | None = None,
    retry_wait_sec: float = DEFAULT_UPSTREAM_RETRY_WAIT_SEC,
    retry_attempts: int = DEFAULT_UPSTREAM_RETRY_ATTEMPTS,
) -> Any:
    options = options or ChatOptions()
    assistant_msg_id = require_uuid(assistant_msg_id, "assistant_message_id") if assistant_msg_id else str(uuid.uuid4())
    retry_attempts = max(1, int(retry_attempts))
    retry_wait_sec = max(0.0, float(retry_wait_sec))
    # 仅当本次调用自建会话时才内部重试：复用会话（continue/reuse）的 chat_id 由调用方持有，
    # 换会话会破坏“保持当前会话”语义，此类错误仍原样透传。
    own_chat = bool(create_chat or not chat_id)
    content_emitted = False
    attempt = 0
    force_fresh_captcha = False  # 上游刚拒绝过验证码时，下一 attempt 绕开池码现场重解
    # 本地请求镜像积累：phase=thinking 进 reasoning，其余进 content；重试换会话时清零。
    answer_parts: list[str] = []
    thinking_parts: list[str] = []
    # ds2api 同款两阶段镜像：流开始前落 streaming 记录（含出站消息/文件/最终 prompt），
    # 结束时更新为 success/stopped/error；客户端断开与上游失败也保留已读内容。
    # 同一 history_ctx 复用同一条记录（面板降级重试不会产生两条记录）。
    hist_id = ""
    if history_ctx:
        hist_id = str(history_ctx.get("_record_id") or "")
        if not hist_id:
            hist_id = start_history_record(
                surface=str(history_ctx.get("surface") or ""),
                model=options.model,
                stream=coerce_bool(history_ctx.get("stream"), True),
                user_input=str(history_ctx.get("user_input") or ""),
                messages=history_ctx.get("messages") or [],
                files=files or [],
                context_text=str(history_ctx.get("context_text") or ""),
                final_prompt=prompt,
                delivery_mode=str(history_ctx.get("delivery_mode") or "inline"),
                context_file_requested=coerce_bool(history_ctx.get("context_file_requested"), False),
                context_file_fallback=str(history_ctx.get("context_file_fallback") or ""),
                context_files=history_ctx.get("context_files") or [],
                account=str(history_ctx.get("account") or ""),
            )
            if hist_id:
                history_ctx["_record_id"] = hist_id
        else:
            restart_history_record(hist_id, prompt)
    if hist_id and context_out is not None:
        context_out["_history_record_id"] = hist_id
    hist_started = time.monotonic()
    hist_status = "success"
    hist_error = ""
    # 流式过程中按历史详情页的读取频率节流落盘，让生成中内容保持实时，
    # 同时避免比消费者更高频地重写 detail 与完整索引。
    hist_last_progress = 0.0

    def _hist_progress_tick() -> None:
        nonlocal hist_last_progress
        if not hist_id:
            return
        now = time.monotonic()
        if now - hist_last_progress < HISTORY_PROGRESS_INTERVAL_SECONDS:
            return
        hist_last_progress = now
        update_history_progress(
            hist_id,
            content="".join(answer_parts),
            reasoning="".join(thinking_parts),
            status_code=200,
            elapsed_ms=int((now - hist_started) * 1000),
        )

    try:
        while True:
            attempt += 1
            last_attempt = attempt >= retry_attempts
            if own_chat:
                try:
                    chat_id, user_msg_id = new_chat(state, prompt, options=options)
                except Exception as exc:
                    # 创建会话阶段同样可能撞上瞬时繁忙/限流/验证码失效。
                    if not last_attempt and is_retryable_upstream_error(str(exc)):
                        wait_sec = 0.0 if is_captcha_upstream_error(str(exc)) else retry_wait_sec
                        log_event(
                            "upstream_transient_retry",
                            stage="new_chat",
                            attempt=attempt,
                            max_attempts=retry_attempts,
                            wait_sec=wait_sec,
                            error=str(exc)[:200],
                        )
                        time.sleep(wait_sec)
                        continue
                    raise
                log_event("upstream_chat_created", chat_id_fp=sha16(str(chat_id or "")), model=options.model, attempt=attempt)
            elif not user_msg_id:
                user_msg_id = str(uuid.uuid4())
                log_event("upstream_chat_reused", chat_id_fp=sha16(str(chat_id or "")), model=options.model, attempt=attempt)
            if context_out is not None:
                context_out.update(
                    {
                        "chat_id": chat_id,
                        "current_user_message_id": user_msg_id,
                        "assistant_message_id": assistant_msg_id,
                        "model": options.model,
                        "mode": options.mode,
                    }
                )
            captcha = captcha_verify_param
            if fresh_captcha_browser:
                # 每个 attempt 重新求解：繁忙失败的那次请求可能已消费掉旧 captcha。
                # 上游刚以 F018/F019 拒绝过验证码时强制现场重解，绝不再取可能超龄的池码。
                captcha = resolve_fresh_captcha(
                    state,
                    options.model,
                    _CAPTCHA_WORKER,
                    timeout_ms=captcha_timeout_ms,
                    chrome_path=chrome_path,
                    headless=captcha_headless,
                    force_fresh=force_fresh_captcha,
                )
                log_event(
                    "fresh_captcha_ready" if captcha else "fresh_captcha_empty",
                    attempt=attempt,
                    force_fresh=force_fresh_captcha,
                    **describe_captcha_verify_param(captcha),
                )
                force_fresh_captcha = False
            query, signature = query_params(state, prompt, chat_id)
            payload = completion_payload(
                state,
                prompt,
                chat_id,
                user_msg_id,
                assistant_msg_id=assistant_msg_id,
                captcha_verify_param=captcha,
                options=options,
                files=files,
                history=history if create_chat else None,
            )
            url = f"{BASE_URL}/api/v2/chat/completions?{query}"
            req = Request(
                url,
                data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                headers=request_headers(state, signature),
                method="POST",
            )
            try:
                resp = urlopen(req, timeout=upstream_timeout_sec or UPSTREAM_STREAM_TIMEOUT_SEC)
            except HTTPError as exc:
                summary = http_error_summary(exc)
                log_event(
                    "upstream_http_error",
                    chat_id_fp=sha16(str(chat_id or "")),
                    model=options.model,
                    status=exc.code,
                    summary=summary,
                )
                if not last_attempt and own_chat and is_retryable_upstream_error(summary):
                    wait_sec = 0.0 if is_captcha_upstream_error(summary) else retry_wait_sec
                    _best_effort_delete_upstream_chat(state, chat_id)
                    log_event(
                        "upstream_transient_retry",
                        attempt=attempt,
                        max_attempts=retry_attempts,
                        wait_sec=wait_sec,
                        error=summary[:200],
                    )
                    time.sleep(wait_sec)
                    continue
                raise RuntimeError(summary) from exc
            except (URLError, TimeoutError, OSError) as exc:
                log_event(
                    "upstream_connect_failed",
                    chat_id_fp=sha16(str(chat_id or "")),
                    model=options.model,
                    error=str(exc)[:300],
                )
                raise
            with resp:
                log_event("upstream_stream_open", chat_id_fp=sha16(str(chat_id or "")), model=options.model, attempt=attempt)
                buffer = ""
                event_count = 0
                stream_started = time.time()
                retry_transient = ""
                try:
                    for chunk in resp:
                        text = chunk.decode("utf-8", errors="replace")
                        buffer += text
                        if len(buffer) > MAX_SSE_BUFFER_BYTES:
                            raise RuntimeError("上游 SSE 事件超过本地缓冲上限")
                        while True:
                            framed = pop_sse_event(buffer)
                            if framed is None:
                                break
                            event, buffer = framed
                            event = event.strip()
                            if not event:
                                continue
                            error = extract_error_from_event(event)
                            if (
                                error
                                and not content_emitted
                                and not last_attempt
                                and own_chat
                                and is_retryable_upstream_error(error)
                            ):
                                # 流首瞬时繁忙/验证码失效：不透传给客户端，整体换会话重试。
                                retry_transient = error
                                break
                            delta, _phase = extract_delta_from_event(event)
                            if delta:
                                content_emitted = True
                                (thinking_parts if _phase.lower() == "thinking" else answer_parts).append(delta)
                                _hist_progress_tick()
                            event_count += 1
                            yield event
                        if retry_transient:
                            break
                    if not retry_transient and buffer.strip():
                        event = buffer.strip()
                        error = extract_error_from_event(event)
                        if (
                            error
                            and not content_emitted
                            and not last_attempt
                            and own_chat
                            and is_retryable_upstream_error(error)
                        ):
                            retry_transient = error
                        else:
                            delta, _phase = extract_delta_from_event(event)
                            if delta:
                                content_emitted = True
                                (thinking_parts if _phase.lower() == "thinking" else answer_parts).append(delta)
                                _hist_progress_tick()
                            event_count += 1
                            yield event
                finally:
                    log_event(
                        "upstream_stream_done",
                        chat_id_fp=sha16(str(chat_id or "")),
                        model=options.model,
                        attempt=attempt,
                        events=event_count,
                        elapsed_ms=int((time.time() - stream_started) * 1000),
                    )
                if retry_transient:
                    _best_effort_delete_upstream_chat(state, chat_id)
                    captcha_rejected = is_captcha_upstream_error(retry_transient)
                    if captcha_rejected:
                        # 验证码被拒（超龄/一次性已耗）：无需等待，强制下一轮现场重解。
                        force_fresh_captcha = True
                    answer_parts.clear()
                    thinking_parts.clear()
                    if hist_id:
                        # 重试换会话：立即清掉盘上残留的上一轮内容，不受节流限制。
                        update_history_progress(
                            hist_id,
                            status_code=200,
                            elapsed_ms=int((time.monotonic() - hist_started) * 1000),
                        )
                    wait_sec = 0.0 if captcha_rejected else retry_wait_sec
                    log_event(
                        "upstream_transient_retry",
                        attempt=attempt,
                        max_attempts=retry_attempts,
                        wait_sec=wait_sec,
                        reason="captcha_rejected" if captcha_rejected else "transient_busy",
                        error=retry_transient[:200],
                    )
                    time.sleep(wait_sec)
                    continue
            # 成功读完整个上游流：finally 统一把镜像记录更新为 success。
            return
    except GeneratorExit:
        # 客户端断开 / 停止生成：保留已读取的部分内容（含思维链）。
        hist_status = "stopped"
        raise
    except Exception as exc:
        hist_status = "error"
        hist_error = str(exc)[:500]
        raise
    finally:
        if hist_id:
            finish_history_record(
                hist_id,
                status=hist_status,
                content="".join(answer_parts),
                reasoning="".join(thinking_parts),
                error=hist_error,
                elapsed_ms=int((time.monotonic() - hist_started) * 1000),
                chat_id=str(chat_id or ""),
            )



SSE_EVENT_SEPARATOR_RE = re.compile(r"\r?\n\r?\n")


def pop_sse_event(buffer: str) -> tuple[str, str] | None:
    match = SSE_EVENT_SEPARATOR_RE.search(buffer)
    if match is None:
        return None
    return buffer[: match.start()], buffer[match.end() :]


def parse_sse_event(event: str) -> Any:
    """Parse LF/CRLF SSE frames, ignoring comments and joining data fields."""
    data_lines: list[str] = []
    for line in str(event or "").splitlines():
        if not line or line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if separator and field.strip().lower() == "data":
            data_lines.append(value[1:] if value.startswith(" ") else value)
    payload = "\n".join(data_lines).strip() if data_lines else str(event or "").strip()
    return json_or_none(payload)


def extract_delta_from_event(event: str) -> tuple[str, str]:
    obj = parse_sse_event(event)
    if not isinstance(obj, dict):
        return "", ""
    data = obj.get("data") or {}
    if not isinstance(data, dict):
        return "", ""
    delta = str(data.get("delta_content") or "")
    phase = str(data.get("phase") or "")
    # glm-5.3's stream emits the phase-transition markers (<think>/</think>) as
    # bare delta chunks; they are protocol artifacts, never model content.
    if delta.strip().lower() in {"<think>", "</think>"}:
        return "", phase
    return delta, phase


def extract_error_from_event(event: str) -> str:
    obj = parse_sse_event(event)
    if not isinstance(obj, dict):
        return ""
    candidates = [obj]
    data = obj.get("data")
    if isinstance(data, dict):
        candidates.append(data)
    error = obj.get("error")
    if isinstance(error, dict):
        candidates.append(error)
    for item in candidates:
        code = item.get("code")
        detail = item.get("detail") or item.get("message") or item.get("content") or item.get("error")
        if code or detail:
            if isinstance(detail, dict):
                detail = json.dumps(detail, ensure_ascii=False)
            return f"{code or 'UPSTREAM_ERROR'}: {detail or ''}".strip()
    return ""


def should_emit_delta(phase: str, include_thinking: bool) -> bool:
    return include_thinking or phase.lower() != "thinking"


def is_chat_missing_error(error: str) -> bool:
    """Detect upstream errors that mean the reused chat no longer exists."""
    lowered = str(error or "").lower()
    missing = ("not found" in lowered) or ("不存在" in lowered) or ("已被删除" in lowered)
    chat_like = ("chat" in lowered) or ("conversation" in lowered) or ("session" in lowered)
    return missing and chat_like


def is_transient_upstream_error(message: str) -> bool:
    """上游繁忙/限流类瞬时错误：回答未开始时可整体换会话重试。

    分类依据工作区 HAR 实测报文（流首事件）：
    {"code":"MODEL_CONCURRENCY_LIMIT","detail":"当前模型使用人数较多，请稍后再试或切换到其他模型。"}
    鉴权、验证码、内容审核类错误不属于瞬时错误，不会触发内部重试。
    """
    lowered = str(message or "").lower()
    if any(pattern.lower() in lowered for pattern in TRANSIENT_UPSTREAM_ERROR_PATTERNS):
        return True
    # completions POST 阶段的限流/网关错误（http_error_summary 形如 "HTTP Error 429: ..."）
    return bool(re.search(r"http error (429|500|502|503|504)\b", lowered))


def is_captcha_upstream_error(message: str) -> bool:
    """验证码被上游拒绝（F018/F019 verify_failed）：可整体换会话 + 现场重解重试。

    实测报文（流首事件，2026-08-29 04:02 日志实证）：
    {"captcha_error_type":"verify_failed","code":"FRONTEND_CAPTCHA_REQUIRED",
     "detail":"人机验证失败，请重新验证后再试。","verify_code":"F018"/"F019"}
    成因：验证码一次性使用且有远端时效，池/缓存中超过 ~1 分钟的验证码会被拒。
    """
    lowered = str(message or "").lower()
    return (
        "frontend_captcha_required" in lowered
        or "captcha_error_type" in lowered
        or "verify_failed" in lowered
        or "人机验证" in lowered
    )


def is_retryable_upstream_error(message: str) -> bool:
    """回答未开始时可整体换会话重试的流首错误：瞬时繁忙 + 验证码失效。"""
    return is_transient_upstream_error(message) or is_captcha_upstream_error(message)


def _best_effort_delete_upstream_chat(state: HarState, chat_id: str) -> None:
    """重试前的旧会话清理：后台执行，失败只记日志，不阻断重试。"""
    if not chat_id:
        return

    def _work() -> None:
        try:
            delete_zai_chat(state, chat_id)
        except Exception as exc:
            log_event(
                "retry_cleanup_delete_error",
                chat_id_fp=sha16(chat_id),
                error=str(exc)[:200],
            )

    _submit_auto_delete(_work)


# ---------------------------------------------------------------------------
# 后台自动删除执行器：自动删除实测耗时 1-4 秒，从响应路径上摘除，客户端不再等待。
# ---------------------------------------------------------------------------
_DELETE_EXECUTOR: ThreadPoolExecutor | None = None
_DELETE_EXECUTOR_LOCK = threading.Lock()
# 测试钩子：True 时删除任务在当前线程内联执行，保证测试确定性。
_AUTO_DELETE_INLINE = False


def _submit_auto_delete(fn: Callable[[], None]) -> None:
    if _AUTO_DELETE_INLINE:
        fn()
        return
    global _DELETE_EXECUTOR
    with _DELETE_EXECUTOR_LOCK:
        if _DELETE_EXECUTOR is None:
            _DELETE_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="autodel")
        _DELETE_EXECUTOR.submit(fn)


# ---------------------------------------------------------------------------
# 验证码预热池：captcha 实测一次性使用（复用会被上游 F018 verify_failed 拒绝），
# 不能缓存复用；改为在请求间隙后台预解下一个，请求到来直接取用，求解延迟对客户端归零。
# 预解只用 happy-dom（无浏览器实例、轻量可并发）；池空时同步求解兜底，行为同旧版。
# ---------------------------------------------------------------------------
# 验证码远端时效实测：池码 ~61s 仍被上游接受，~114s 即被 F019 verify_failed 拒绝
# （2026-08-29 03:57/04:02 日志对照）。TTL 取 75s：连续请求间隔通常 <30s，池码几乎
# 总是热的；空闲超龄直接丢弃、现场重解，绝不让超龄码进入请求。即便如此仍漏网时，
# stream_zai_completion 会把 F018/F019 当可重试错误整体换会话 + force_fresh 重解。
CAPTCHA_POOL_TTL_SEC = 75.0
_CAPTCHA_POOL: deque[tuple[str, float]] = deque(maxlen=2)
_CAPTCHA_POOL_LOCK = threading.Lock()
_CAPTCHA_PREFETCHING = False
_CAPTCHA_PREFETCH_ENABLED = True  # 测试钩子：False 时禁用后台预解


def _captcha_pool_take() -> str:
    """取一个未过期的新鲜验证码；过期条目就地丢弃。"""
    now = time.monotonic()
    with _CAPTCHA_POOL_LOCK:
        while _CAPTCHA_POOL:
            captcha, solved_at = _CAPTCHA_POOL.popleft()
            if captcha and now - solved_at <= CAPTCHA_POOL_TTL_SEC:
                return captcha
    return ""


def _schedule_captcha_prefetch(timeout_ms: int) -> None:
    """后台预解下一个验证码，填补被取走/消耗的名额。"""
    global _CAPTCHA_PREFETCHING
    if not _CAPTCHA_PREFETCH_ENABLED or not happydom_captcha_available():
        return
    with _CAPTCHA_POOL_LOCK:
        if _CAPTCHA_PREFETCHING:
            return
        _CAPTCHA_PREFETCHING = True

    def _work() -> None:
        global _CAPTCHA_PREFETCHING
        try:
            captcha = get_happydom_captcha(timeout_ms)
        except Exception:
            captcha = ""
        with _CAPTCHA_POOL_LOCK:
            _CAPTCHA_PREFETCHING = False
            if captcha:
                _CAPTCHA_POOL.append((captcha, time.monotonic()))

    threading.Thread(target=_work, name="captcha-prefetch", daemon=True).start()


def direct_prompt(
    state: HarState,
    prompt: str,
    create_chat: bool = True,
    captcha_verify_param: str | None = None,
    fresh_captcha_browser: bool = False,
    chrome_path: str | None = None,
    captcha_headless: bool = True,
    captcha_timeout_ms: int = 75_000,
    upstream_timeout_sec: int | None = None,
    include_thinking: bool = False,
    options: ChatOptions | None = None,
) -> str:
    options = options or ChatOptions(include_thinking=include_thinking)
    parts: list[str] = []
    errors: list[str] = []
    context: dict[str, Any] = {}

    def cleanup_direct_chat() -> None:
        """CLI mode mirrors the web default: delete the created chat afterwards."""
        chat_id = str(context.get("chat_id") or "").strip()
        if not options.delete_chat_after_completion or not chat_id:
            return
        try:
            delete_zai_chat(state, chat_id)
        except Exception as exc:
            log_event(
                "direct_prompt_delete_error",
                chat_id_fp=sha16(chat_id),
                error=str(exc)[:300],
            )

    try:
        for event in stream_zai_completion(
            state,
            prompt,
            create_chat=create_chat,
            chat_id=state.chat_id,
            captcha_verify_param=captcha_verify_param,
            fresh_captcha_browser=fresh_captcha_browser,
            chrome_path=chrome_path,
            captcha_headless=captcha_headless,
            captcha_timeout_ms=captcha_timeout_ms,
            upstream_timeout_sec=upstream_timeout_sec,
            options=options,
            context_out=context,
            history_ctx={
                "surface": "cli_direct",
                "stream": True,
                "user_input": prompt[:HISTORY_PROMPT_CHARS],
                "messages": [{"role": "user", "content": prompt}],
                "context_text": "",
                "account": (state.user_id or "")[:8],
            },
        ):
            error = extract_error_from_event(event)
            if error:
                errors.append(error)
                continue
            delta, phase = extract_delta_from_event(event)
            if delta and should_emit_delta(phase, options.include_thinking):
                print(delta, end="", flush=True)
                parts.append(delta)
    except Exception:
        cleanup_direct_chat()
        raise
    print()
    cleanup_direct_chat()
    if errors:
        raise RuntimeError("; ".join(errors[:3]))
    return "".join(parts)


def openai_message_content_text(content: Any) -> str:
    """Extract textual content from the common OpenAI messages representations."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
                continue
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
                continue
            if isinstance(text, dict) and isinstance(text.get("value"), str):
                parts.append(text["value"])
        return "\n".join(part for part in parts if part)
    if isinstance(content, dict):
        text = content.get("text")
        if isinstance(text, str):
            return text
        if isinstance(text, dict) and isinstance(text.get("value"), str):
            return text["value"]
    return ""


def prompt_from_openai_messages(messages: Any) -> str:
    if not isinstance(messages, list):
        raise ValueError("messages must be a list")
    rendered: list[tuple[str, str]] = []
    has_user = False
    labels = {
        "system": "系统指令",
        "developer": "开发者指令",
        "user": "用户",
        "assistant": "助手",
        "tool": "工具结果",
    }
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "").strip().lower()
        text = openai_message_content_text(message.get("content")).strip()
        if not text or role not in labels:
            continue
        if role == "user":
            has_user = True
        rendered.append((role, text))
    if not has_user:
        raise ValueError("missing user message")
    if len(rendered) == 1 and rendered[0][0] == "user":
        return rendered[0][1]
    return "\n\n".join(f"[{labels[role]}]\n{text}" for role, text in rendered)


# ---------------------------------------------------------------------------
# Protocol compatibility core
# ---------------------------------------------------------------------------
#
# OpenAI Chat/Responses and Anthropic Messages are intentionally normalized
# here, before any chat.z.ai-specific request is assembled.  Keeping this
# layer separate from HTTP renderers prevents subtle drift between protocols:
# the same prompt, tool policy, context-file behavior, and upstream completion
# are used regardless of the client SDK.

TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
TOOL_JSON_WRAPPER_RE = re.compile(
    r"<glm2api_tool_calls>\s*(\{.*?\})\s*</glm2api_tool_calls\s*>",
    re.IGNORECASE | re.DOTALL,
)
# DSML tags may carry a trailing pipe (DeepSeek-style `<|DSML|tool_calls|>`);
# the pipe is consumed before attributes/`>` so both spellings canonicalize.
TOOL_XML_BLOCK_RE = re.compile(
    r"<(?:\|?DSML\|?)?(?:glm2api_)?tool_calls\|?(?:\s[^>]*)?>.*?</(?:\|?DSML\|?)?(?:glm2api_)?tool_calls\|?\s*>",
    re.IGNORECASE | re.DOTALL,
)


def compact_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        return str(value)


def protocol_content_text(content: Any) -> str:
    """Turn common OpenAI/Responses/Anthropic content blocks into transcript text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, bytes):
        return content.decode("utf-8", errors="replace")
    if isinstance(content, list):
        parts = [protocol_content_text(item) for item in content]
        return "\n".join(part for part in parts if part)
    if not isinstance(content, dict):
        return str(content)

    item_type = str(content.get("type") or "").strip().lower()
    text = content.get("text")
    if isinstance(text, str):
        return text
    if isinstance(text, dict):
        for key in ("value", "text", "content"):
            if isinstance(text.get(key), str):
                return str(text[key])
    # Named content blocks have their own semantics below.  Only unwrap a
    # generic ``content`` field when the caller did not label the block; this
    # avoids accidentally treating a tool result or image descriptor as plain
    # user text.  ``output`` and ``value`` are generic wrappers in both APIs.
    for key in ("output", "value"):
        nested = content.get(key)
        if isinstance(nested, (str, list, dict)):
            value = protocol_content_text(nested)
            if value:
                return value
    if not item_type:
        nested = content.get("content")
        if isinstance(nested, (str, list, dict)):
            value = protocol_content_text(nested)
            if value:
                return value

    if item_type in {"image", "input_image", "image_url"}:
        image = content.get("image_url") or content.get("source") or content.get("url") or "image"
        if isinstance(image, dict):
            image = image.get("url") or image.get("media_type") or "image"
        return f"[图像附件: {str(image)[:240]}]"
    if item_type in {"file", "input_file", "document"}:
        file_id = content.get("file_id") or content.get("id") or content.get("filename") or "file"
        return f"[文件附件: {str(file_id)[:240]}]"
    if item_type == "tool_result":
        nested = protocol_content_text(content.get("content"))
        return nested or "[工具结果为空]"
    if item_type in {"thinking", "reasoning"}:
        return protocol_content_text(content.get("thinking") or content.get("summary") or "")
    if item_type in {"tool_use", "function_call"}:
        name = content.get("name") or "unknown_tool"
        args = content.get("input") if "input" in content else content.get("arguments")
        return f"[工具调用 {name}]\n{compact_json(args if args is not None else {})}"
    return ""


def as_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return _repair_tool_path_controls(dict(value))
    if isinstance(value, str):
        parsed, _valid = _parse_tool_json_value(value)
        if isinstance(parsed, dict):
            return _repair_tool_path_controls(parsed)
        if value.strip():
            return {"input": value}
    if value is not None:
        return {"input": value}
    return {}


def _repair_invalid_json_backslashes(text: str) -> str:
    """Escape only backslashes that cannot start a valid JSON escape sequence."""
    out: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char != "\\":
            out.append(char)
            index += 1
            continue
        next_char = text[index + 1] if index + 1 < len(text) else ""
        valid_escape = next_char in {'"', "\\", "/", "b", "f", "n", "r", "t"}
        if next_char == "u":
            unicode_digits = text[index + 2 : index + 6]
            valid_escape = len(unicode_digits) == 4 and all(char in string.hexdigits for char in unicode_digits)
        if not valid_escape:
            out.append("\\\\")
        else:
            out.append("\\")
        index += 1
    return "".join(out)


def _repair_loose_json(text: str) -> str:
    """Handle two common model slips: unquoted object keys and trailing commas."""
    text = re.sub(r"([,{]\s*)([A-Za-z_][A-Za-z0-9_.-]*)(\s*:)", r'\1"\2"\3', text)
    return re.sub(r",\s*([}\]])", r"\1", text)


def _parse_tool_json_value(value: str) -> tuple[Any, bool]:
    raw = html.unescape(str(value or "").strip())
    candidates = [raw]
    repaired_slashes = _repair_invalid_json_backslashes(raw)
    loose = _repair_loose_json(raw)
    for candidate in (repaired_slashes, loose, _repair_loose_json(repaired_slashes)):
        if candidate not in candidates:
            candidates.append(candidate)
    for candidate in candidates:
        try:
            return json.loads(candidate), True
        except (TypeError, ValueError):
            continue
    return raw, False


def _repair_tool_path_controls(value: Any) -> Any:
    """Undo JSON escape interpretation inside obvious Windows drive paths."""
    if isinstance(value, dict):
        return {key: _repair_tool_path_controls(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_repair_tool_path_controls(item) for item in value]
    if isinstance(value, str) and re.match(r"^[A-Za-z]:", value):
        replacements = {"\b": r"\b", "\f": r"\f", "\n": r"\n", "\r": r"\r", "\t": r"\t"}
        for control, escaped in replacements.items():
            value = value.replace(control, escaped)
    return value


def normalized_history_tool_call(raw: Any, fallback_index: int = 0) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    function = raw.get("function") if isinstance(raw.get("function"), dict) else raw
    name = str(function.get("name") or raw.get("name") or "").strip()
    if not name:
        return None
    arguments = function.get("arguments") if "arguments" in function else raw.get("input")
    return {
        "id": str(raw.get("id") or raw.get("call_id") or f"call_history_{fallback_index}"),
        "name": name,
        "arguments": as_json_object(arguments),
    }


def normalize_openai_messages_for_protocol(messages: Any) -> list[dict[str, Any]]:
    if not isinstance(messages, list):
        raise ValueError("messages must be a list")
    out: list[dict[str, Any]] = []
    allowed_roles = {"system", "developer", "user", "assistant", "tool"}
    for index, raw in enumerate(messages):
        if not isinstance(raw, dict):
            continue
        role = str(raw.get("role") or "").strip().lower()
        if role not in allowed_roles:
            continue
        if role == "developer":
            role = "system"
        text = protocol_content_text(raw.get("content")).strip()
        entry: dict[str, Any] = {"role": role, "content": text}
        if role == "assistant":
            reasoning = protocol_content_text(raw.get("reasoning_content")).strip()
            if reasoning:
                entry["reasoning_content"] = reasoning
            calls: list[dict[str, Any]] = []
            raw_calls = raw.get("tool_calls")
            if isinstance(raw_calls, list):
                for call_index, call in enumerate(raw_calls):
                    normalized = normalized_history_tool_call(call, call_index)
                    if normalized:
                        calls.append(normalized)
            if not calls and isinstance(raw.get("function_call"), dict):
                normalized = normalized_history_tool_call(raw["function_call"], index)
                if normalized:
                    calls.append(normalized)
            if calls:
                entry["tool_calls"] = calls
        if role == "tool":
            entry["tool_call_id"] = str(raw.get("tool_call_id") or raw.get("call_id") or "")
            entry["name"] = str(raw.get("name") or "").strip()
        if text or entry.get("reasoning_content") or entry.get("tool_calls") or role == "tool":
            out.append(entry)
    if not out:
        raise ValueError("messages contains no supported content")
    return out


def responses_input_item_to_message(item: dict[str, Any], fallback_index: int) -> dict[str, Any] | None:
    item_type = str(item.get("type") or "").strip().lower()
    if item_type == "message" or "role" in item:
        role = str(item.get("role") or "user").strip().lower()
        if role not in {"system", "developer", "user", "assistant", "tool"}:
            role = "user"
        if role == "developer":
            role = "system"
        entry: dict[str, Any] = {"role": role, "content": protocol_content_text(item.get("content")).strip()}
        if role == "assistant":
            reasoning = protocol_content_text(item.get("reasoning_content")).strip()
            if reasoning:
                entry["reasoning_content"] = reasoning
            calls = []
            raw_calls = item.get("tool_calls")
            if isinstance(raw_calls, list):
                for call_index, call in enumerate(raw_calls):
                    normalized = normalized_history_tool_call(call, call_index)
                    if normalized:
                        calls.append(normalized)
            if calls:
                entry["tool_calls"] = calls
        if role == "tool":
            entry["tool_call_id"] = str(item.get("tool_call_id") or item.get("call_id") or "")
            entry["name"] = str(item.get("name") or "").strip()
        return entry
    if item_type in {"input_text", "text"}:
        text = protocol_content_text(item).strip()
        return {"role": "user", "content": text} if text else None
    if item_type == "reasoning":
        text = protocol_content_text(item).strip()
        return {"role": "assistant", "content": "", "reasoning_content": text} if text else None
    if item_type == "output_text":
        text = protocol_content_text(item).strip()
        return {"role": "assistant", "content": text} if text else None
    if item_type == "function_call":
        name = str(item.get("name") or "").strip()
        if not name:
            return None
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": str(item.get("call_id") or item.get("id") or f"call_history_{fallback_index}"),
                    "name": name,
                    "arguments": as_json_object(item.get("arguments")),
                }
            ],
        }
    if item_type in {"function_call_output", "tool_result"}:
        return {
            "role": "tool",
            "tool_call_id": str(item.get("call_id") or item.get("tool_call_id") or ""),
            "content": protocol_content_text(item.get("output") if "output" in item else item.get("content")).strip(),
        }
    text = protocol_content_text(item).strip()
    return {"role": "user", "content": text} if text else None


def normalize_responses_input_to_messages(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, str):
        return [{"role": "user", "content": value.strip()}] if value.strip() else []
    if isinstance(value, dict):
        item = responses_input_item_to_message(value, 0)
        return [item] if item else []
    if not isinstance(value, list):
        return []
    out: list[dict[str, Any]] = []
    fallback_text: list[str] = []
    for index, raw in enumerate(value):
        if isinstance(raw, str):
            if raw.strip():
                fallback_text.append(raw.strip())
            continue
        if not isinstance(raw, dict):
            continue
        item = responses_input_item_to_message(raw, index)
        if item is None:
            text = protocol_content_text(raw).strip()
            if text:
                fallback_text.append(text)
            continue
        if fallback_text:
            out.append({"role": "user", "content": "\n".join(fallback_text)})
            fallback_text = []
        out.append(item)
    if fallback_text:
        out.append({"role": "user", "content": "\n".join(fallback_text)})
    return out


def normalize_responses_messages_for_protocol(body: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(body.get("messages"), list) and body["messages"]:
        messages = normalize_openai_messages_for_protocol(body["messages"])
    else:
        messages = normalize_responses_input_to_messages(body.get("input"))
    instructions = protocol_content_text(body.get("instructions")).strip()
    if instructions:
        messages.insert(0, {"role": "system", "content": instructions})
    if not messages:
        raise ValueError("Responses request must include input or messages")
    return messages


def normalize_claude_system(system: Any) -> str:
    return protocol_content_text(system).strip()


def normalize_claude_messages_for_protocol(messages: Any, system: Any = None) -> list[dict[str, Any]]:
    if not isinstance(messages, list):
        raise ValueError("messages must be a list")
    out: list[dict[str, Any]] = []
    system_text = normalize_claude_system(system)
    if system_text:
        out.append({"role": "system", "content": system_text})
    for message_index, raw in enumerate(messages):
        if not isinstance(raw, dict):
            continue
        role = str(raw.get("role") or "").strip().lower()
        if role not in {"user", "assistant"}:
            continue
        content = raw.get("content")
        if not isinstance(content, list):
            text = protocol_content_text(content).strip()
            if text:
                out.append({"role": role, "content": text})
            continue
        text_parts: list[str] = []
        calls: list[dict[str, Any]] = []
        tool_results: list[dict[str, Any]] = []
        for part_index, part in enumerate(content):
            if not isinstance(part, dict):
                text = protocol_content_text(part).strip()
                if text:
                    text_parts.append(text)
                continue
            part_type = str(part.get("type") or "text").strip().lower()
            if part_type == "tool_use":
                name = str(part.get("name") or "").strip()
                if name:
                    calls.append(
                        {
                            "id": str(part.get("id") or f"toolu_history_{message_index}_{part_index}"),
                            "name": name,
                            "arguments": as_json_object(part.get("input")),
                        }
                    )
                continue
            if part_type == "tool_result":
                tool_results.append(
                    {
                        "role": "tool",
                        "tool_call_id": str(part.get("tool_use_id") or ""),
                        "content": protocol_content_text(part.get("content")).strip(),
                        "is_error": coerce_bool(part.get("is_error"), False),
                    }
                )
                continue
            text = protocol_content_text(part).strip()
            if text:
                if part_type == "thinking":
                    text = f"[历史思考]\n{text}"
                text_parts.append(text)
        if role == "assistant" and (text_parts or calls):
            entry: dict[str, Any] = {"role": "assistant", "content": "\n".join(text_parts)}
            if calls:
                entry["tool_calls"] = calls
            out.append(entry)
        elif role == "user":
            out.extend(tool_results)
            if text_parts:
                out.append({"role": "user", "content": "\n".join(text_parts)})
    if not out:
        raise ValueError("messages contains no supported content")
    return out


def normalize_tool_definitions(raw_tools: Any, surface: str) -> list[dict[str, Any]]:
    if raw_tools is None:
        return []
    if not isinstance(raw_tools, list):
        raise ValueError("tools must be a list")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_tools:
        if not isinstance(raw, dict):
            continue
        candidate = raw
        tool_type = str(raw.get("type") or "").strip().lower()
        if isinstance(raw.get("function"), dict):
            candidate = raw["function"]
            tool_type = "function"
        if surface == "anthropic_messages" and not tool_type:
            tool_type = "function"
        if tool_type not in {"function", "custom"}:
            continue
        name = str(candidate.get("name") or raw.get("name") or "").strip()
        if not TOOL_NAME_RE.fullmatch(name):
            raise ValueError(f"invalid tool name: {name or '<empty>'}")
        if name in seen:
            raise ValueError(f"duplicate tool name: {name}")
        seen.add(name)
        parameters = candidate.get("parameters")
        if not isinstance(parameters, dict):
            parameters = candidate.get("input_schema")
        if not isinstance(parameters, dict):
            parameters = candidate.get("inputSchema")
        if not isinstance(parameters, dict):
            parameters = candidate.get("schema")
        if not isinstance(parameters, dict):
            parameters = raw.get("parameters")
        if not isinstance(parameters, dict):
            parameters = raw.get("input_schema")
        if not isinstance(parameters, dict):
            parameters = raw.get("inputSchema")
        if not isinstance(parameters, dict):
            parameters = raw.get("schema")
        if not isinstance(parameters, dict):
            parameters = {"type": "object", "properties": {}}
        out.append(
            {
                "name": name,
                "description": str(candidate.get("description") or raw.get("description") or "").strip(),
                "parameters": parameters,
            }
        )
    return out


def coerce_tool_value_to_schema(value: Any, schema: Any) -> Any:
    """Keep emitted arguments compatible with the request's JSON Schema.

    Models occasionally serialize a value structurally even when the client
    declared that parameter as a string.  Match the reference adapter's narrow
    repair rule: only explicitly string-typed fields are stringified; all
    number/bool/object/array declarations retain their native JSON shape.
    """
    if not isinstance(schema, dict):
        return value
    declared_type = schema.get("type")
    types = {str(item).lower() for item in declared_type} if isinstance(declared_type, list) else {str(declared_type).lower()}
    if "string" in types and value is not None and not isinstance(value, str):
        return compact_json(value)
    if ("object" in types or isinstance(schema.get("properties"), dict)) and isinstance(value, dict):
        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        return {
            key: coerce_tool_value_to_schema(item, properties.get(key))
            for key, item in value.items()
        }
    if ("array" in types or isinstance(schema.get("items"), dict)) and isinstance(value, list):
        return [coerce_tool_value_to_schema(item, schema.get("items")) for item in value]
    return value


def coerce_tool_arguments_to_schema(arguments: dict[str, Any], tool: dict[str, Any]) -> dict[str, Any]:
    schema = tool.get("parameters")
    repaired = coerce_tool_value_to_schema(arguments, schema)
    return repaired if isinstance(repaired, dict) else arguments


def tools_enable_web_search(raw_tools: Any) -> bool:
    if not isinstance(raw_tools, list):
        return False
    for raw in raw_tools:
        if not isinstance(raw, dict):
            continue
        tool_type = str(raw.get("type") or "").strip().lower()
        if tool_type.startswith("web_search"):
            return True
    return False


def _normalize_allowed_tool_names(raw_allowed: Any, declared_names: set[str]) -> tuple[str, ...]:
    """Validate the Responses-style allowed_tools subset without silently widening it."""
    if raw_allowed is None:
        return ()
    if not isinstance(raw_allowed, list) or not raw_allowed:
        raise ValueError("tool_choice.allowed_tools must be a non-empty list")
    names: list[str] = []
    for item in raw_allowed:
        if isinstance(item, str):
            name = item.strip()
        elif isinstance(item, dict):
            function = item.get("function") if isinstance(item.get("function"), dict) else item
            name = str(function.get("name") or "").strip()
        else:
            name = ""
        if not name or name not in declared_names:
            raise ValueError("tool_choice.allowed_tools references an undeclared tool")
        if name not in names:
            names.append(name)
    if not names:
        raise ValueError("tool_choice.allowed_tools must contain at least one declared tool")
    return tuple(names)


def normalize_tool_choice(raw_choice: Any, tools: list[dict[str, Any]]) -> ToolChoice:
    names = {str(tool["name"]) for tool in tools}
    policy = ToolChoice(mode="auto" if names else "none")
    if raw_choice is None:
        return policy
    if isinstance(raw_choice, str):
        mode = raw_choice.strip().lower() or "auto"
        if mode == "any":
            mode = "required"
        if mode not in {"auto", "none", "required"}:
            raise ValueError(f"unsupported tool_choice: {raw_choice}")
        if mode == "required" and not names:
            raise ValueError("tool_choice=required requires at least one declared function tool")
        policy.mode = mode
        return policy
    if not isinstance(raw_choice, dict):
        raise ValueError("tool_choice must be a string or object")
    policy.disable_parallel = coerce_bool(raw_choice.get("disable_parallel_tool_use"), False)
    mode = str(raw_choice.get("type") or "auto").strip().lower()
    raw_allowed = raw_choice.get("allowed_tools")
    if mode == "allowed_tools":
        raw_allowed = raw_choice.get("tools") if raw_allowed is None else raw_allowed
        mode = str(raw_choice.get("mode") or "auto").strip().lower()
    policy.allowed_names = _normalize_allowed_tool_names(raw_allowed, names)
    if mode == "any":
        mode = "required"
    if mode in {"auto", "none", "required"}:
        callable_names = set(policy.allowed_names) if policy.allowed_names else names
        if mode == "required" and not callable_names:
            raise ValueError("tool_choice=required requires at least one declared function tool")
        policy.mode = mode
        if mode == "none":
            policy.allowed_names = ()
        return policy
    if mode in {"function", "tool"}:
        function = raw_choice.get("function") if isinstance(raw_choice.get("function"), dict) else raw_choice
        name = str(function.get("name") or "").strip()
        if not name or name not in names:
            raise ValueError("tool_choice references an undeclared tool")
        return ToolChoice(
            mode="forced",
            forced_name=name,
            disable_parallel=policy.disable_parallel,
            allowed_names=(name,),
        )
    raise ValueError(f"unsupported tool_choice.type: {mode}")


# ---------------------------------------------------------------------------
# Reference-project aligned prompts (dkceshi): English tool-call contract,
# natural transcript files, and file-mode execution prompts.
# ---------------------------------------------------------------------------
TOOLS_TRANSCRIPT_INTRO = "The functions listed below are available for you to invoke during this turn."
HISTORY_TRANSCRIPT_INTRO = "The dialogue up to this point. Pick up from the most recent user message."
MODE_B_TOOL_GUIDANCE = (
    "The attached file enumerates the function definitions and parameter contracts available for this turn. "
    "Rely solely on the functions and parameter shapes documented therein; do not fabricate or invoke any "
    "that are not listed."
)
# dkceshi 同款输出完整性守卫（MessagesPrepareWithThinkingAndToolHint 里的
# outputIntegrityGuardPrompt）：当前输入框上下文有工具或仍含 tool 消息时前置，
# 防止模型把 DSML 解析残留 / 上游乱码原样回显给客户端。
OUTPUT_INTEGRITY_GUARD_PROMPT = (
    "Output integrity guard: Should any upstream context, tool output, or parsed text contain garbled, "
    "corrupted, partially parsed, repeated, or otherwise malformed fragments, do not imitate or echo them; "
    "produce only the correct content for the user."
)
CONTEXT_FILE_CACHE_TTL_SECONDS = 600
_CONTEXT_FILE_CACHE: dict[tuple[str, str], tuple[dict[str, Any], float]] = {}
_CONTEXT_FILE_CACHE_LOCK = threading.RLock()
# 上传失败降级（对齐 ds2api 参考实现）：同一账号连续 N 次上下文文件上传失败后，
# 在窗口期内自动回落模式 A（整段上下文进 prompt），避免连续失败触发更严风控。
CONTEXT_UPLOAD_DEGRADE_THRESHOLD = 3
CONTEXT_UPLOAD_DEGRADE_WINDOW_SEC = 30 * 60
_CONTEXT_UPLOAD_FAILURES: dict[str, int] = {}
_CONTEXT_UPLOAD_DEGRADED_UNTIL: dict[str, float] = {}


def context_upload_degraded(user_id: str) -> bool:
    if not user_id:
        return False
    now = time.monotonic()
    with _CONTEXT_FILE_CACHE_LOCK:
        until = _CONTEXT_UPLOAD_DEGRADED_UNTIL.get(user_id)
        if until is None:
            return False
        if now >= until:
            _CONTEXT_UPLOAD_DEGRADED_UNTIL.pop(user_id, None)
            _CONTEXT_UPLOAD_FAILURES.pop(user_id, None)
            return False
        return True


def record_context_upload_failure(user_id: str) -> None:
    if not user_id:
        return
    with _CONTEXT_FILE_CACHE_LOCK:
        failures = _CONTEXT_UPLOAD_FAILURES.get(user_id, 0) + 1
        if failures >= CONTEXT_UPLOAD_DEGRADE_THRESHOLD:
            _CONTEXT_UPLOAD_DEGRADED_UNTIL[user_id] = time.monotonic() + CONTEXT_UPLOAD_DEGRADE_WINDOW_SEC
            _CONTEXT_UPLOAD_FAILURES.pop(user_id, None)
            log_event(
                "context_upload_degrade_enter",
                user_id_fp=sha16(user_id),
                consecutive_failures=failures,
                window_sec=CONTEXT_UPLOAD_DEGRADE_WINDOW_SEC,
                fallback="mode_a",
            )
        else:
            _CONTEXT_UPLOAD_FAILURES[user_id] = failures
            log_event("context_upload_failure_count", user_id_fp=sha16(user_id), consecutive_failures=failures)


def record_context_upload_success(user_id: str) -> None:
    if not user_id:
        return
    with _CONTEXT_FILE_CACHE_LOCK:
        _CONTEXT_UPLOAD_FAILURES.pop(user_id, None)


_CONTEXT_FILE_NAME_COUNTER = 0


def _prompt_cdata(text: str) -> str:
    if not text:
        return ""
    if "]]>" in text:
        return "<![CDATA[" + text.replace("]]>", "]]]]><![CDATA[>") + "]]>"
    return "<![CDATA[" + text + "]]>"


def _wrap_parameter(name: str, inner: str) -> str:
    return f'<|DSML|parameter name="{name}">{inner}</|DSML|parameter>'


def _xml_escape_attr(text: str) -> str:
    return text.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")


def _xml_escape_text(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _example_basic_params(name: str) -> tuple[str, bool]:
    params = {
        "Read": [_wrap_parameter("file_path", _prompt_cdata("README.md"))],
        "Glob": [_wrap_parameter("pattern", _prompt_cdata("**/*.go")), _wrap_parameter("path", _prompt_cdata("."))],
        "read_file": [_wrap_parameter("path", _prompt_cdata("src/main.go"))],
        "list_files": [_wrap_parameter("path", _prompt_cdata("."))],
        "search_files": [_wrap_parameter("query", _prompt_cdata("function-call parser"))],
        "Bash": [_wrap_parameter("command", _prompt_cdata("pwd"))],
        "execute_command": [_wrap_parameter("command", _prompt_cdata("pwd"))],
        "exec_command": [_wrap_parameter("cmd", _prompt_cdata("pwd"))],
        "Write": [_wrap_parameter("file_path", _prompt_cdata("notes.txt")), _wrap_parameter("content", _prompt_cdata("Hello world"))],
        "write_to_file": [_wrap_parameter("path", _prompt_cdata("notes.txt")), _wrap_parameter("content", _prompt_cdata("Hello world"))],
        "Edit": [
            _wrap_parameter("file_path", _prompt_cdata("README.md")),
            _wrap_parameter("old_string", _prompt_cdata("foo")),
            _wrap_parameter("new_string", _prompt_cdata("bar")),
        ],
        "MultiEdit": [
            _wrap_parameter("file_path", _prompt_cdata("README.md")),
            '<|DSML|parameter name="edits"><item><old_string><![CDATA[foo]]></old_string>'
            "<new_string><![CDATA[bar]]></new_string></item></|DSML|parameter>",
        ],
    }.get(name)
    if params is None:
        return "", False
    return "\n".join(params), True


def _pick_example_tool_name(names: list[str]) -> str:
    for name in names:
        if _example_basic_params(name)[1]:
            return name
    if names:
        return names[0]
    return "FUNCTION_NAME"


def _indent_prompt_parameters(body: str, indent: str) -> str:
    if not body.strip():
        return indent + '<|DSML|parameter name="content"></|DSML|parameter>'
    return "\n".join(line if not line.strip() else indent + line for line in body.split("\n"))


def _render_tool_example_block(name: str, params: str) -> str:
    return "\n".join(
        [
            "<|DSML|tool_calls>",
            f'  <|DSML|invoke name="{_xml_escape_attr(name)}">',
            _indent_prompt_parameters(params, "    "),
            "  </|DSML|invoke>",
            "</|DSML|tool_calls>",
        ]
    )


def _first_basic_example(names: list[str]) -> tuple[str, str] | None:
    for name in names:
        params, ok = _example_basic_params(name)
        if ok:
            return name, params
    if names:
        return names[0], _wrap_parameter("input", _prompt_cdata("..."))
    return None


def _has_read_like_tool(names: list[str]) -> bool:
    for name in names:
        if re.sub(r"[^a-z0-9]", "", name.lower()) in {"read", "readfile"}:
            return True
    return False


def tools_allowed_by_policy(tools: list[dict[str, Any]], policy: ToolChoice | None = None) -> list[dict[str, Any]]:
    """Return the exact callable surface exposed by the reference tool-choice policy."""
    if policy is not None and policy.mode == "none":
        return []
    if policy is not None and policy.allowed_names:
        allowed = set(policy.allowed_names)
        tools = [tool for tool in tools if str(tool.get("name") or "").strip() in allowed]
    if policy is not None and policy.mode == "forced" and policy.forced_name:
        return [tool for tool in tools if str(tool.get("name") or "").strip() == policy.forced_name]
    return list(tools)


def build_tool_instruction(tools: list[dict[str, Any]], policy: ToolChoice) -> str:
    """English DSML contract ported from the dkceshi reference project.

    The block shape and the seven rules are copied verbatim; the counter
    examples use a tool name picked from the current request, and a valid
    example is generated from the same preset library as the reference.
    """
    tools = tools_allowed_by_policy(tools, policy)
    if not tools:
        return ""
    names: list[str] = []
    seen: set[str] = set()
    for tool in tools:
        name = str(tool.get("name") or "").strip()
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    example_tool_name = _pick_example_tool_name(names)

    instructions = (
        "When you choose to invoke a function, end your response with the following XML block:\n\n"
        "<|DSML|tool_calls>\n"
        '  <|DSML|invoke name="FUNCTION_NAME">\n'
        '    <|DSML|parameter name="ARG_NAME"><![CDATA[VALUE]]></|DSML|parameter>\n'
        "  </|DSML|invoke>\n"
        "</|DSML|tool_calls>\n\n"
        "Rules:\n"
        "1) You may include explanatory text before the block, but the block must be the final element of your response. Do not append any text, explanation, or greeting after </|DSML|tool_calls>.\n"
        "2) The block itself must be raw XML. Do NOT enclose it in markdown fences, HTML, JSON, or any other code-block wrapper. The first non-whitespace characters of the block must be exactly <|DSML|tool_calls>.\n"
        "3) Strings go inside <![CDATA[...]]>; numbers, booleans, and null remain as plain text. Every string parameter must be wrapped, even short ones.\n"
        "4) Objects use nested XML elements within the parameter body; arrays repeat <item> children.\n"
        "5) Use only parameter names declared in the function schema. Do not fabricate fields, and never emit empty or whitespace-only parameter values. If a required value is unknown, ask the user instead.\n"
        "6) Never emit tool calls as JSON, Markdown, or prose. The only accepted form is the raw DSML XML block above.\n"
        "7) If you are not invoking a function, answer the user normally without emitting any DSML tags.\n\n"
        "Steer clear of the following incorrect patterns:\n\n"
        "Incorrect 1 — text trailing the block:\n"
        "  <|DSML|tool_calls>...</|DSML|tool_calls> I hope this helps.\n"
        "Incorrect 2 — enclosed within markdown fences:\n"
        "```xml\n"
        "  <|DSML|tool_calls>...</|DSML|tool_calls>\n"
        "```\n"
        "Incorrect 3 — opening tag omitted:\n"
        '  <|DSML|invoke name="FUNCTION_NAME">...</|DSML|invoke>\n'
        "  </|DSML|tool_calls>\n"
        "Incorrect 4 — parameter value left empty:\n"
        "  <|DSML|tool_calls>\n"
        f'    <|DSML|invoke name="{example_tool_name}">\n'
        '      <|DSML|parameter name="input"></|DSML|parameter>\n'
        "    </|DSML|invoke>\n"
        "  </|DSML|tool_calls>\n"
        "Incorrect 5 — invocation rendered as JSON or Markdown rather than DSML:\n"
        f"  **Calling:** {example_tool_name}\n"
        '  {"input": "..."}\n\n'
    )
    example = _first_basic_example(names)
    if example:
        instructions += "Valid example:\n" + _render_tool_example_block(example[0], example[1]) + "\n"
    if _has_read_like_tool(names):
        instructions += (
            "\n\nRead-style cache guard: when a Read/read_file-style tool result reports the file is unchanged, "
            "already present in earlier context, or otherwise yields no fresh file body, treat that outcome as "
            "missing content. Do not repeatedly issue the same read for the absent body. Request a full-content "
            "read if the tool supports it, or inform the user that the file contents must be supplied again."
        )
    if policy.mode == "required":
        instructions += "\n7) For this response, you MUST issue at least one call to a tool from the permitted list."
    elif policy.mode == "forced":
        instructions += "\n7) For this response, you MUST issue exactly one call to the tool named: " + str(policy.forced_name or "")
        instructions += "\n8) Do not issue any other tool call."
    return instructions


def _normalize_prompt_tool_call_value(raw: Any) -> Any:
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return ""
        parsed = json_or_none(text)
        return parsed if parsed is not None else raw
    return raw


def _stringify_tool_call_arguments(value: Any) -> str:
    if value is None:
        return "{}"
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return "{}"
        if "}{" in text or "][" in text:
            return value
        return text
    return compact_json(value)


def _render_prompt_tool_xml_node(name: str, value: Any, indent: str) -> tuple[str, bool]:
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_.:-]*$", name):
        return "", False
    if value is None:
        return f"{indent}<{name}></{name}>", True
    if isinstance(value, dict):
        inner, ok = _render_prompt_tool_xml_map(value, indent + "  ")
        if not ok:
            return "", False
        if not inner.strip():
            return f"{indent}<{name}></{name}>", True
        return f"{indent}<{name}>\n{inner}\n{indent}</{name}>", True
    if isinstance(value, list):
        if not value:
            return f"{indent}<{name}></{name}>", True
        lines = []
        for item in value:
            rendered, ok = _render_prompt_tool_xml_node(name, item, indent)
            if not ok:
                return "", False
            lines.append(rendered)
        return "\n".join(lines), True
    if isinstance(value, str):
        return f"{indent}<{name}>{_prompt_cdata(value)}</{name}>", True
    if isinstance(value, (bool, int, float)):
        return f"{indent}<{name}>{_xml_escape_text(str(value))}</{name}>", True
    return f"{indent}<{name}>{_prompt_cdata(str(value))}</{name}>", True


def _render_prompt_tool_xml_map(value: dict[str, Any], indent: str) -> tuple[str, bool]:
    if not value:
        return "", True
    lines = []
    for key in sorted(value):
        rendered, ok = _render_prompt_tool_xml_node(key, value[key], indent)
        if not ok:
            return "", False
        lines.append(rendered)
    return "\n".join(lines), True


def _render_prompt_tool_xml_array(items: list[Any], indent: str) -> tuple[str, bool]:
    if not items:
        return "", True
    lines = []
    for item in items:
        rendered, ok = _render_prompt_tool_xml_node("item", item, indent)
        if not ok:
            return "", False
        lines.append(rendered)
    return "\n".join(lines), True


def _render_prompt_tool_xml_body(value: Any, indent: str) -> tuple[str, bool]:
    if value is None:
        return "", True
    if isinstance(value, dict):
        return _render_prompt_tool_xml_map(value, indent)
    if isinstance(value, list):
        return _render_prompt_tool_xml_array(value, indent)
    if isinstance(value, str):
        return f"{indent}<content>{_prompt_cdata(value)}</content>", True
    if isinstance(value, (bool, int, float)):
        return f"{indent}<value>{_xml_escape_text(str(value))}</value>", True
    return f"{indent}<value>{_prompt_cdata(str(value))}</value>", True


def _render_prompt_parameter_node(name: str, value: Any, indent: str) -> tuple[str, bool]:
    name = name.strip()
    if not name:
        return "", False
    if value is None:
        return f'{indent}<|DSML|parameter name="{_xml_escape_attr(name)}"></|DSML|parameter>', True
    if isinstance(value, dict):
        body, ok = _render_prompt_tool_xml_body(value, indent + "  ")
        if not ok:
            return "", False
        if not body.strip():
            return f'{indent}<|DSML|parameter name="{_xml_escape_attr(name)}"></|DSML|parameter>', True
        return f'{indent}<|DSML|parameter name="{_xml_escape_attr(name)}">\n{body}\n{indent}</|DSML|parameter>', True
    if isinstance(value, list):
        body, ok = _render_prompt_tool_xml_array(value, indent + "  ")
        if not ok:
            return "", False
        if not body.strip():
            return f'{indent}<|DSML|parameter name="{_xml_escape_attr(name)}"></|DSML|parameter>', True
        return f'{indent}<|DSML|parameter name="{_xml_escape_attr(name)}">\n{body}\n{indent}</|DSML|parameter>', True
    if isinstance(value, str):
        return f'{indent}<|DSML|parameter name="{_xml_escape_attr(name)}">{_prompt_cdata(value)}</|DSML|parameter>', True
    return f'{indent}<|DSML|parameter name="{_xml_escape_attr(name)}">{_prompt_cdata(str(value))}</|DSML|parameter>', True


def _render_prompt_tool_parameters(value: Any, indent: str) -> tuple[str, bool]:
    if value is None:
        return "", True
    if isinstance(value, dict):
        if not value:
            return "", True
        lines = []
        for key in sorted(value):
            rendered, ok = _render_prompt_parameter_node(key, value[key], indent)
            if not ok:
                return "", False
            lines.append(rendered)
        return "\n".join(lines), True
    if isinstance(value, list):
        lines = []
        for item in value:
            rendered, ok = _render_prompt_parameter_node("item", item, indent)
            if not ok:
                return "", False
            lines.append(rendered)
        return "\n".join(lines), True
    if isinstance(value, str):
        return f'{indent}<|DSML|parameter name="content">{_prompt_cdata(value)}</|DSML|parameter>', True
    return f'{indent}<|DSML|parameter name="value">{_prompt_cdata(str(value))}</|DSML|parameter>', True


def render_tool_calls_block(calls: list[dict[str, Any]]) -> str:
    blocks: list[str] = []
    for call in calls:
        if not isinstance(call, dict):
            continue
        name = str(call.get("name") or "").strip()
        if not name:
            continue
        parameters = render_tool_call_parameters(call.get("arguments"), "    ")
        if parameters:
            blocks.append(f'  <|DSML|invoke name="{_xml_escape_attr(name)}">\n{parameters}\n  </|DSML|invoke>')
        else:
            blocks.append(f'  <|DSML|invoke name="{_xml_escape_attr(name)}"></|DSML|invoke>')
    if not blocks:
        return ""
    return "<|DSML|tool_calls>\n" + "\n".join(blocks) + "\n</|DSML|tool_calls>"


def render_tool_call_parameters(raw: Any, indent: str) -> str:
    value = _normalize_prompt_tool_call_value(raw)
    body, ok = _render_prompt_tool_parameters(value, indent)
    if ok and body.strip():
        return body
    fallback = _stringify_tool_call_arguments(raw)
    if fallback.strip():
        return indent + '<|DSML|parameter name="content">' + _prompt_cdata(fallback) + "</|DSML|parameter>"
    return ""


_CLAUDE_CODE_TOOL_CALL_RE = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.IGNORECASE | re.DOTALL)


def _rewrite_claude_code_tool_calls(content: str) -> str:
    """Rewrite Claude Code's textual ``<tool_call>`` history into plain prose.

    Claude Code serializes historical tool calls as ``<tool_call>Name
    arg="..."</tool_call>`` inside assistant content. Left verbatim in the
    uploaded transcript, GLM starts imitating that markup (observed output
    included ``<tool_call>Read ...`` fragments and leaked transcript labels),
    which Claude Code cannot parse. Turning it into a plain sentence keeps the
    history informative without handing the model a markup template.
    """
    if "<tool_call>" not in content:
        return content

    def _rewrite(match: re.Match[str]) -> str:
        inner = match.group(1).strip()
        parts = inner.split(None, 1)
        name = parts[0] if parts else "tool"
        args_text = parts[1].strip() if len(parts) > 1 else ""
        args: list[str] = []
        for key, value in re.findall("([A-Za-z_][A-Za-z0-9_]*)\\s*=\\s*(" + chr(34) + "[^" + chr(34) + "]*" + chr(34) + "|'[^']*'|[^\\s]+)", args_text):
            args.append(f"{key}: {value.strip(chr(34) + chr(39))}")
        if args:
            return f"[called tool: {name} ({', '.join(args)})]"
        if args_text:
            return f"[called tool: {name} ({args_text[:200]})]"
        return f"[called tool: {name}]"

    return _CLAUDE_CODE_TOOL_CALL_RE.sub(_rewrite, content)


def render_protocol_history_message(message: dict[str, Any]) -> str:
    """One transcript entry in the reference project's natural label format."""
    role = str(message.get("role") or "").lower()
    content = str(message.get("content") or "").strip()
    if role in {"tool", "function"}:
        parts: list[str] = []
        name = str(message.get("name") or "").strip()
        if name:
            parts.append("function=" + name)
        call_id = str(message.get("tool_call_id") or "").strip()
        if call_id:
            parts.append("invocation_id=" + call_id)
        header = "[" + " ".join(parts) + "]" if parts else ""
        return "\n".join(part for part in (header, content) if part)
    if role == "assistant":
        rendered_parts: list[str] = []
        reasoning = str(message.get("reasoning_content") or "").strip()
        if reasoning:
            rendered_parts.append("[reasoning_content]\n" + reasoning + "\n[/reasoning_content]")
        if content:
            rendered_parts.append(_rewrite_claude_code_tool_calls(content))
        calls = message.get("tool_calls")
        if isinstance(calls, list):
            block = render_tool_calls_block(calls)
            if block:
                rendered_parts.append(block)
        return "\n\n".join(rendered_parts)
    return content


def build_history_transcript(messages: list[dict[str, Any]]) -> str:
    if not messages:
        return ""
    lines = [HISTORY_TRANSCRIPT_INTRO, ""]
    for message in messages:
        content = render_protocol_history_message(message)
        if not content.strip():
            continue
        role = str(message.get("role") or "").lower()
        if role == "developer":
            role = "system"
        label = "tool" if role == "function" else (role or "unknown")
        lines.extend([f"[{label}]", content, ""])
    transcript = "\n".join(lines).strip()
    return transcript + "\n" if transcript else ""


def build_tools_transcript(tools: list[dict[str, Any]], policy: ToolChoice | None = None) -> str:
    schemas: list[str] = []
    for tool in tools_allowed_by_policy(tools, policy):
        name = str(tool.get("name") or "").strip()
        if not name:
            continue
        description = str(tool.get("description") or "").strip() or "No description available"
        schema = tool.get("parameters") or {"type": "object", "properties": {}}
        schemas.append(f"name: {name}\ndescription: {description}\nschema: {compact_json(schema)}")
    if not schemas:
        return ""
    return TOOLS_TRANSCRIPT_INTRO + "\n\n" + "\n\n".join(schemas) + "\n"


def current_input_file_prompt(has_tools_file: bool) -> str:
    prompt = "The attached file holds the earlier conversation. Read it and respond to the most recent user request directly."
    if has_tools_file:
        prompt += (
            " The other attached file enumerates the available function definitions and parameter contracts; "
            "use only those tools and adhere to the function-call contract described below."
        )
    return prompt


def file_mode_execution_prompt(tools: list[dict[str, Any]], policy: ToolChoice) -> str:
    prompt = current_input_file_prompt(bool(build_tools_transcript(tools, policy).strip()))
    instructions = build_tool_instruction(tools, policy)
    if instructions:
        # Reference order is System(tool guidance + DSML contract), then User(continuation).
        # Z.ai receives a single native user prompt, so preserve that semantic order when flattening.
        prompt = MODE_B_TOOL_GUIDANCE + "\n\n" + instructions + "\n\n" + prompt
    return prompt


def build_context_package(surface: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]], policy: ToolChoice) -> str:
    parts: list[str] = []
    # Keep policy/schema content ahead of the dialogue, mirroring the
    # reference adapter's System -> User ordering after flattening to Z.ai.
    tools_text = build_tools_transcript(tools, policy)
    if tools_text:
        parts.append(tools_text)
    instructions = build_tool_instruction(tools, policy)
    if instructions:
        parts.append(instructions)
    transcript = build_history_transcript(messages)
    if transcript:
        parts.append(transcript)
    if not parts:
        return ""
    return "\n\n".join(parts).strip() + "\n"


def context_file_requested(
    body: dict[str, Any],
    surface: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    context_text: str,
) -> bool:
    requested_model = str(body.get("model") or "").strip().lower()
    if is_force_history_model(requested_model):
        return True
    explicit = _first_body_value(
        body,
        ("context_as_file", "current_input_file", "forcehistory", "history_as_file"),
        _MISSING,
    )
    if explicit is not _MISSING:
        return coerce_bool(explicit, False)
    # Match the reference project's explicit opt-in policy: regular model IDs
    # pass the normalized context directly, while -forcehistory selects the
    # uploaded transcript path.  The arguments stay in the signature so the
    # caller can keep one decision point for every protocol surface.
    del surface, messages, tools, context_text
    return False


def protocol_options_from_body(body: dict[str, Any], surface: str, include_thinking_default: bool) -> ChatOptions:
    options = chat_options_from_body(body, include_thinking_default=include_thinking_default)
    requested_model = str(body.get("model") or "").strip().lower()
    force_no_thinking = is_no_thinking_model(requested_model)
    if tools_enable_web_search(body.get("tools")):
        options.auto_web_search = True
    if surface == "openai_responses":
        reasoning = body.get("reasoning")
        if isinstance(reasoning, dict):
            effort = str(reasoning.get("effort") or "").strip().lower()
            if effort and effort not in {"none", "minimal"}:
                options.enable_thinking = True
                options.include_thinking = coerce_bool(body.get("include_thinking"), options.include_thinking)
                if effort in {"high", "xhigh", "max"}:
                    options.reasoning_effort = "max"
                elif effort in {"low", "medium"}:
                    options.reasoning_effort = "high"
    if surface == "anthropic_messages":
        thinking = body.get("thinking")
        if isinstance(thinking, dict) and str(thinking.get("type") or "").lower() == "enabled":
            options.enable_thinking = True
            options.include_thinking = True
    if force_no_thinking:
        options.enable_thinking = False
        options.include_thinking = False
    return options


def requested_model_name(body: dict[str, Any]) -> str:
    value = str(_first_body_value(body, ("model",), DEFAULT_MODEL) or DEFAULT_MODEL).strip()
    return value or DEFAULT_MODEL


def apply_parallel_tool_setting(policy: ToolChoice, body: dict[str, Any]) -> ToolChoice:
    """Honor the standard OpenAI flag in addition to Anthropic's choice flag."""
    if "parallel_tool_calls" in body and not coerce_bool(body.get("parallel_tool_calls"), True):
        policy.disable_parallel = True
    return policy


def normalize_openai_chat_request(body: dict[str, Any], include_thinking_default: bool) -> ProtocolRequest:
    messages = normalize_openai_messages_for_protocol(body.get("messages"))
    tools = normalize_tool_definitions(body.get("tools"), "openai_chat")
    policy = apply_parallel_tool_setting(normalize_tool_choice(body.get("tool_choice"), tools), body)
    options = protocol_options_from_body(body, "openai_chat", include_thinking_default)
    context_text = build_context_package("openai_chat", messages, tools, policy)
    context_as_file = context_file_requested(body, "openai_chat", messages, tools, context_text)
    prompt = file_mode_execution_prompt(tools, policy) if context_as_file else context_text
    return ProtocolRequest(
        surface="openai_chat",
        response_model=requested_model_name(body),
        options=options,
        stream=coerce_bool(body.get("stream"), False),
        messages=messages,
        context_text=context_text,
        execution_prompt=prompt,
        files=chat_files_from_body(body),
        tools=tools,
        tool_choice=policy,
        context_as_file=context_as_file,
        store=coerce_bool(body.get("store"), True),
    )


def normalize_openai_responses_request(
    body: dict[str, Any],
    include_thinking_default: bool,
    prior_messages: list[dict[str, Any]] | None = None,
) -> ProtocolRequest:
    messages = list(prior_messages or []) + normalize_responses_messages_for_protocol(body)
    tools = normalize_tool_definitions(body.get("tools"), "openai_responses")
    policy = apply_parallel_tool_setting(normalize_tool_choice(body.get("tool_choice"), tools), body)
    options = protocol_options_from_body(body, "openai_responses", include_thinking_default)
    context_text = build_context_package("openai_responses", messages, tools, policy)
    context_as_file = context_file_requested(body, "openai_responses", messages, tools, context_text)
    prompt = file_mode_execution_prompt(tools, policy) if context_as_file else context_text
    return ProtocolRequest(
        surface="openai_responses",
        response_model=requested_model_name(body),
        options=options,
        stream=coerce_bool(body.get("stream"), False),
        messages=messages,
        context_text=context_text,
        execution_prompt=prompt,
        files=chat_files_from_body(body),
        tools=tools,
        tool_choice=policy,
        context_as_file=context_as_file,
        store=coerce_bool(body.get("store"), True),
        previous_response_id=str(body.get("previous_response_id") or "").strip(),
    )


def normalize_anthropic_messages_request(body: dict[str, Any], include_thinking_default: bool) -> ProtocolRequest:
    messages = normalize_claude_messages_for_protocol(body.get("messages"), body.get("system"))
    tools = normalize_tool_definitions(body.get("tools"), "anthropic_messages")
    policy = apply_parallel_tool_setting(normalize_tool_choice(body.get("tool_choice"), tools), body)
    options = protocol_options_from_body(body, "anthropic_messages", include_thinking_default)
    context_text = build_context_package("anthropic_messages", messages, tools, policy)
    context_as_file = context_file_requested(body, "anthropic_messages", messages, tools, context_text)
    prompt = file_mode_execution_prompt(tools, policy) if context_as_file else context_text
    return ProtocolRequest(
        surface="anthropic_messages",
        response_model=requested_model_name(body),
        options=options,
        stream=coerce_bool(body.get("stream"), False),
        messages=messages,
        context_text=context_text,
        execution_prompt=prompt,
        files=chat_files_from_body(body),
        tools=tools,
        tool_choice=policy,
        context_as_file=context_as_file,
        store=coerce_bool(body.get("store"), True),
    )


def strip_markdown_fenced_blocks(text: str) -> str:
    """Exclude Markdown examples from tool detection without rewriting prose."""
    if "```" not in text and "~~~" not in text:
        return text
    kept: list[str] = []
    active_marker = ""
    for line in text.splitlines(keepends=True):
        trimmed = line.lstrip()
        if not active_marker:
            marker = "```" if trimmed.startswith("```") else "~~~" if trimmed.startswith("~~~") else ""
            if marker:
                active_marker = marker[0]
                continue
            kept.append(line)
            continue
        if trimmed.startswith(active_marker * 3):
            active_marker = ""
    return "".join(kept)


def canonicalize_dsml_tool_markup(markup: str) -> str:
    # Full-width punctuation and smart quotes are a frequent side effect of
    # multilingual generation. Normalize only characters used by the tag shell.
    markup = str(markup or "").translate(
        str.maketrans(
            {
                "＜": "<",
                "＞": ">",
                "｜": "|",
                "／": "/",
                "＝": "=",
                "＂": '"',
                "“": '"',
                "”": '"',
                "‘": "'",
                "’": "'",
            }
        )
    )
    # 实测上游偶尔把关闭标签写成 `<||DSML|invoke>`（双竖杠无斜杠）， 这里统一
    # 容忍每侧 0-3 个竖杠并把 DSML 前缀剥掉，ElementTree/兜底正则才能继续解析。
    pattern = re.compile(
        r"<\s*(/?)\s*(?:\|{0,3}DSML\|{0,3}\s*)?(tool_calls|invoke|parameter)([^>]*?)\|{0,3}>",
        re.IGNORECASE,
    )

    def replace(match: re.Match[str]) -> str:
        slash = match.group(1)
        tag = match.group(2).lower()
        tail = match.group(3)
        if "DSML" in match.group(0).upper() and not slash and tag in {"invoke", "parameter"} and "=" not in tail:
            # 上游偶尔把关闭标签写成无斜杠的 `<||DSML|invoke>`；DSML 风格的
            # 合法开标签必带 name= 属性，故裸 invoke/parameter 按关闭标签归一。
            # 不带 DSML 前缀的普通标签保持原样，避免误伤。
            return f"</{tag}>"
        return f"<{slash}{tag}{tail}>"

    return pattern.sub(replace, markup)


def _tool_markup_candidate(markup: str) -> str:
    """Return the last complete or safely repairable tool-call wrapper."""
    matches = list(re.finditer(r"<tool_calls(?:\s[^>]*)?>.*?</tool_calls\s*>", markup, re.IGNORECASE | re.DOTALL))
    if matches:
        return matches[-1].group(0)
    # The model commonly finishes the invoke and then gets cut off before the
    # outer closing tag. That structure is unambiguous and safe to close.
    opens = list(re.finditer(r"<tool_calls(?:\s[^>]*)?>", markup, re.IGNORECASE))
    if opens:
        tail = markup[opens[-1].start() :].strip()
        if re.search(r"</invoke\s*>", tail, re.IGNORECASE):
            return tail + "</tool_calls>"
    # Also accept a complete bare invoke, as some models omit both outer tags.
    invokes = list(re.finditer(r"<invoke\s+name=[\"'][^\"']+[\"'][^>]*>.*?</invoke\s*>", markup, re.IGNORECASE | re.DOTALL))
    if invokes:
        return "<tool_calls>" + invokes[-1].group(0) + "</tool_calls>"
    return ""


def _xml_tool_value(node: ElementTree.Element) -> Any:
    children = list(node)
    if children:
        grouped: dict[str, Any] = {}
        for child in children:
            key = str(child.tag).split("}")[-1]
            item = _xml_tool_value(child)
            if key in grouped:
                grouped[key] = grouped[key] + [item] if isinstance(grouped[key], list) else [grouped[key], item]
            else:
                grouped[key] = item
        if set(grouped) == {"item"}:
            item = grouped["item"]
            return item if isinstance(item, list) else [item]
        return grouped
    parsed, valid = _parse_tool_json_value("".join(node.itertext()).strip())
    return _repair_tool_path_controls(parsed) if valid else parsed


def normalize_tool_call_candidates(
    raw_calls: Any,
    tools: list[dict[str, Any]],
    policy: ToolChoice,
    id_prefix: str = "call_",
) -> list[ToolCall]:
    if not isinstance(raw_calls, list) or policy.mode == "none":
        return []
    tools_by_name = {str(tool["name"]): tool for tool in tools_allowed_by_policy(tools, policy)}
    allowed = set(tools_by_name)
    out: list[ToolCall] = []
    for raw in raw_calls:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        if name not in allowed:
            continue
        if policy.mode == "forced" and name != policy.forced_name:
            continue
        arguments = raw.get("arguments") if "arguments" in raw else raw.get("input")
        out.append(
            ToolCall(
                # Surface-native prefixes (call_/fc_/toolu_) and a standard
                # 24-char id body keep strict SDK validation happy.
                id=id_prefix + uuid.uuid4().hex[:24],
                name=name,
                arguments=coerce_tool_arguments_to_schema(as_json_object(arguments), tools_by_name[name]),
            )
        )
    # A forced choice means exactly one invocation of that function, even if
    # the model repeats the block or the client otherwise permits parallelism.
    if (policy.disable_parallel or policy.mode == "forced") and out:
        return out[:1]
    return out


def parse_xml_tool_calls(
    markup: str,
    tools: list[dict[str, Any]],
    policy: ToolChoice,
    id_prefix: str = "call_",
) -> list[ToolCall]:
    normalized = canonicalize_dsml_tool_markup(markup)
    candidate = _tool_markup_candidate(normalized)
    if not candidate:
        return []
    try:
        root = ElementTree.fromstring(candidate)
    except ElementTree.ParseError:
        # 容错：模型偶尔输出未转义的 & / <（未包 CDATA），ElementTree 会拒绝。
        # 此时退回到宽松正则，逐 invoke/parameter 提取。
        raw_calls: list[dict[str, Any]] = []
        for invoke_match in re.finditer(
            r"<invoke\s+name=[\"']([^\"']+)[\"'][^>]*>(.*?)</invoke\s*>",
            candidate,
            re.IGNORECASE | re.DOTALL,
        ):
            name = invoke_match.group(1).strip()
            arguments: dict[str, Any] = {}
            for parameter_match in re.finditer(
                r"<parameter\s+name=[\"']([^\"']+)[\"'][^>]*>(.*?)</parameter\s*>",
                invoke_match.group(2),
                re.IGNORECASE | re.DOTALL,
            ):
                param_name = parameter_match.group(1).strip()
                raw_value = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", parameter_match.group(2), flags=re.DOTALL).strip()
                parsed, valid = _parse_tool_json_value(raw_value)
                arguments[param_name] = _repair_tool_path_controls(parsed) if valid else parsed
            raw_calls.append({"name": name, "arguments": arguments})
        if raw_calls:
            return normalize_tool_call_candidates(raw_calls, tools, policy, id_prefix=id_prefix)
        return []
    raw_calls: list[dict[str, Any]] = []
    for invoke in root.findall(".//invoke"):
        name = str(invoke.attrib.get("name") or "").strip()
        arguments: dict[str, Any] = {}
        for parameter in invoke.findall("./parameter"):
            param_name = str(parameter.attrib.get("name") or "").strip()
            if not param_name:
                continue
            arguments[param_name] = _xml_tool_value(parameter)
        if not arguments:
            arguments_node = invoke.find("./arguments")
            if arguments_node is not None:
                arguments = as_json_object("".join(arguments_node.itertext()).strip())
        raw_calls.append({"name": name, "arguments": arguments})
    return normalize_tool_call_candidates(raw_calls, tools, policy, id_prefix=id_prefix)


def parse_tool_calls_from_output(
    text: str,
    tools: list[dict[str, Any]],
    policy: ToolChoice,
    id_prefix: str = "call_",
) -> list[ToolCall]:
    if not text.strip() or not tools or policy.mode == "none":
        return []
    visible_for_parse = strip_markdown_fenced_blocks(text).strip()
    if not visible_for_parse:
        return []
    matches = list(TOOL_JSON_WRAPPER_RE.finditer(visible_for_parse))
    if matches:
        parsed = json_or_none(matches[-1].group(1))
        if isinstance(parsed, dict):
            calls = parsed.get("tool_calls") if "tool_calls" in parsed else parsed.get("calls")
            result = normalize_tool_call_candidates(calls, tools, policy, id_prefix=id_prefix)
            if result:
                return result
    result = parse_xml_tool_calls(visible_for_parse, tools, policy, id_prefix=id_prefix)
    if result:
        return result
    if visible_for_parse.startswith("{") and visible_for_parse.endswith("}"):
        parsed = json_or_none(visible_for_parse)
        if isinstance(parsed, dict):
            calls = parsed.get("tool_calls") if "tool_calls" in parsed else parsed.get("calls")
            if calls is None and parsed.get("name"):
                calls = [parsed]
            return normalize_tool_call_candidates(calls, tools, policy, id_prefix=id_prefix)
    return []


def tool_markup_attempted(text: str) -> bool:
    visible = canonicalize_dsml_tool_markup(strip_markdown_fenced_blocks(str(text or "")))
    return bool(
        re.search(r"<\s*/?\s*(?:glm2api_)?tool_calls\b", visible, re.IGNORECASE)
        or re.search(r"<\s*/?\s*(?:invoke|parameter)\b", visible, re.IGNORECASE)
    )


class ToolCallFormatError(RuntimeError):
    """The upstream attempted or was required to emit a call, but none converted."""


def protocol_request_with_tool_retry_hint(request: ProtocolRequest, error: str) -> ProtocolRequest:
    allowed_names = [tool["name"] for tool in tools_allowed_by_policy(request.tools, request.tool_choice)]
    hint = (
        "Tool-call correction: the previous attempt could not be converted by the client "
        f"({error[:180]}). Emit one complete DSML tool_calls block using only these names: "
        f"{', '.join(allowed_names)}. Follow the declared parameter schema exactly; do not explain the correction."
    )
    return replace(
        request,
        context_text=request.context_text.rstrip() + "\n\n" + hint + "\n",
        execution_prompt=request.execution_prompt.rstrip() + "\n\n" + hint,
    )


CODE_FENCE_RE = re.compile(r"```[^`]*```", re.DOTALL)


def strip_parsed_tool_markup(text: str) -> str:
    """Remove local tool-call markup, preserving markdown code fences.

    A model may legitimately show `<tool_calls>` inside a fenced code block
    while a real tool turn uses the same tag outside fences; stripping the
    latter while keeping the former prevents both leakage and corrupted
    examples in the answer.
    """
    stashed: list[str] = []

    def stash(match: re.Match[str]) -> str:
        stashed.append(match.group(0))
        return f"\x00GLM2API_FENCE_{len(stashed) - 1}\x00"

    protected = CODE_FENCE_RE.sub(stash, str(text))
    protected = canonicalize_dsml_tool_markup(protected)
    protected = TOOL_JSON_WRAPPER_RE.sub("", protected)
    protected = TOOL_XML_BLOCK_RE.sub("", protected)
    # A complete invoke with a missing outer close is now parseable; remove
    # that entire semantic block instead of leaking its argument values beside
    # the native tool call returned to the client.
    protected = re.sub(
        r"<tool_calls(?:\s[^>]*)?>\s*(?:<invoke\b[^>]*>.*?</invoke\s*>\s*)+(?:</tool_calls\s*>)?",
        "",
        protected,
        flags=re.IGNORECASE | re.DOTALL,
    )
    protected = re.sub(
        r"<invoke\s+name=[\"'][^\"']+[\"'][^>]*>.*?</invoke\s*>",
        "",
        protected,
        flags=re.IGNORECASE | re.DOTALL,
    )
    # Fallback for truncated/malformed blocks: unwrap CDATA, then drop any
    # orphaned adapter tags (tool_calls / invoke / parameter in plain, DSML
    # and glm2api-prefixed spellings). Keeping the parameter text (and only
    # removing the tags) preserves whatever the model actually said.
    protected = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", protected, flags=re.DOTALL)
    protected = re.sub(
        r"</?\s*(?:\|{0,3}DSML\|{0,3}\s*)?(?:glm2api_)?(?:tool_calls|invoke|parameter)\b[^>]*>",
        "",
        protected,
        flags=re.IGNORECASE,
    )
    for index, fence in enumerate(stashed):
        protected = protected.replace(f"\x00GLM2API_FENCE_{index}\x00", fence)
    return protected.strip()


def estimate_protocol_tokens(text: str) -> int:
    """Small deterministic estimate for compatibility usage fields (not billing)."""
    text = str(text or "")
    if not text:
        return 0
    ascii_words = len(re.findall(r"[A-Za-z0-9_]+", text))
    non_ascii = sum(1 for char in text if ord(char) > 127)
    return max(1, ascii_words + (non_ascii + 1) // 2 + max(0, len(text) - non_ascii) // 8)


def tool_call_id_prefix(surface: str) -> str:
    if surface == "openai_responses":
        return "fc_"
    if surface == "anthropic_messages":
        return "toolu_"
    return "call_"


def finalize_protocol_turn(request: ProtocolRequest, text: str, thinking: str) -> ProtocolTurn:
    id_prefix = tool_call_id_prefix(request.surface)
    tool_calls = parse_tool_calls_from_output(text, request.tools, request.tool_choice, id_prefix=id_prefix)
    # Thinking fallback: when the answer carries no call, look in the thinking
    # stream. Auto mode only does this when there is no text at all (the model
    # likely put the call there), while required/forced modes also try it with
    # prose present so a misplaced call does not fail the whole turn.
    fallback_to_thinking = bool(thinking.strip()) and (
        not text.strip() or request.tool_choice.mode in {"required", "forced"}
    )
    calls_source = "output"
    if not tool_calls and fallback_to_thinking:
        tool_calls = parse_tool_calls_from_output(thinking, request.tools, request.tool_choice, id_prefix=id_prefix)
        calls_source = "thinking"
    if not tool_calls and request.tools and (
        tool_markup_attempted(text) or (fallback_to_thinking and tool_markup_attempted(thinking))
    ):
        raise ToolCallFormatError("上游输出了工具调用标记，但其名称或参数格式无法转换")
    if request.tool_choice.mode in {"required", "forced"} and not tool_calls:
        wanted = request.tool_choice.forced_name or "任意已声明工具"
        raise ToolCallFormatError(f"上游未按 tool_choice 输出工具调用: {wanted}")
    # Strip adapter markup unconditionally: a malformed tool block must never
    # leak to the client, even when it cannot be parsed into a ToolCall.
    text = strip_parsed_tool_markup(text)
    thinking = strip_parsed_tool_markup(thinking)
    if tool_calls:
        log_event(
            "tool_calls_parsed",
            model=request.options.model,
            names=[call.name for call in tool_calls],
            source=calls_source,
        )
    return ProtocolTurn(
        text=text,
        thinking=thinking,
        tool_calls=tool_calls,
        input_tokens=estimate_protocol_tokens(request.context_text),
        output_tokens=estimate_protocol_tokens(text) + estimate_protocol_tokens(thinking),
    )


def make_random_short_filename() -> str:
    """User-style short numeric names (111.txt / 4527.txt) as in the reference."""
    global _CONTEXT_FILE_NAME_COUNTER
    with _CONTEXT_FILE_CACHE_LOCK:
        _CONTEXT_FILE_NAME_COUNTER += 1
        seed = (time.time_ns() + _CONTEXT_FILE_NAME_COUNTER * 7919) % 9900 + 100
    return f"{seed}.txt"


def _context_file_cache_lookup(state: HarState, text: str) -> dict[str, Any] | None:
    """Reuse a previously uploaded file id for the same account + content (TTL)."""
    if not state.user_id:
        return None
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    now = time.monotonic()
    with _CONTEXT_FILE_CACHE_LOCK:
        item = _CONTEXT_FILE_CACHE.get((state.user_id, digest))
        if item is None:
            return None
        ref, uploaded_at = item
        if now - uploaded_at > CONTEXT_FILE_CACHE_TTL_SECONDS:
            _CONTEXT_FILE_CACHE.pop((state.user_id, digest), None)
            return None
        return copy.deepcopy(ref)


def _context_file_cache_store(state: HarState, text: str, ref: dict[str, Any]) -> None:
    if not state.user_id:
        return
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    with _CONTEXT_FILE_CACHE_LOCK:
        _CONTEXT_FILE_CACHE[(state.user_id, digest)] = (copy.deepcopy(ref), time.monotonic())
        if len(_CONTEXT_FILE_CACHE) > 512:
            for key in list(_CONTEXT_FILE_CACHE)[:128]:
                _CONTEXT_FILE_CACHE.pop(key, None)


def upload_context_package_to_zai(state: HarState, context_text: str, filename: str | None = None, label: str = "") -> dict[str, Any]:
    """Upload one transcript file (history or tools) with an ephemeral name."""
    raw = context_text.encode("utf-8")
    cached = _context_file_cache_lookup(state, context_text)
    if cached is not None:
        log_event(
            "context_file_cache_hit",
            label=label,
            filename=str(cached.get("name") or ""),
            bytes=len(raw),
        )
        return cached
    if len(raw) > MAX_CONTEXT_FILE_BYTES:
        raise ValueError(f"context file exceeds {MAX_CONTEXT_FILE_BYTES} bytes")
    # 节流抖动 50-200ms：打破“上传即 completion”的固定时序（对齐 ds2api jitterSleep），
    # 真实用户从选文件到点发送之间存在可观察的延迟。
    time.sleep(0.05 + (secrets.randbelow(151) / 1000))
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix="glm2api-context-", suffix=".txt", delete=False) as f:
            tmp_path = Path(f.name)
            f.write(raw)
        ref = upload_file_path_to_zai(state, tmp_path, filename or make_random_short_filename(), "text/plain; charset=utf-8")
        _context_file_cache_store(state, context_text, ref)
        log_event(
            "context_file_uploaded",
            label=label,
            filename=str(ref.get("name") or ""),
            bytes=len(raw),
            file_id_fp=sha16(str(ref.get("id") or "")),
        )
        return ref
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


def _messages_contain_tool_history(messages: list[dict[str, Any]] | None) -> bool:
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        if str(message.get("role") or "").lower() in {"tool", "function"}:
            return True
        if message.get("tool_calls") or message.get("function_call"):
            return True
        content = message.get("content")
        if isinstance(content, list) and any(
            isinstance(item, dict)
            and str(item.get("type") or "").lower() in {"tool_use", "tool_result", "function_call", "function_call_output"}
            for item in content
        ):
            return True
        if isinstance(content, str) and tool_markup_attempted(content):
            return True
    return False


def apply_output_integrity_guard(
    prompt: str,
    tools: list[dict[str, Any]] | None,
    messages: list[dict[str, Any]] | None,
) -> str:
    """dkceshi 同款：当前输入框上下文有工具或含 tool 消息时前置完整性守卫；
    已带守卫（重试/复用路径）不重复加，无工具的纯对话请求不加（避免多余指纹）。"""
    prompt = str(prompt or "")
    if not tools and not _messages_contain_tool_history(messages):
        return prompt
    if "output integrity guard" in prompt[:300].lower():
        return prompt
    return OUTPUT_INTEGRITY_GUARD_PROMPT + "\n\n" + prompt


def _context_file_trace_item(kind: str, ref: dict[str, Any], content: str) -> dict[str, Any]:
    metadata = history_files_snapshot([ref])
    file_meta = metadata[0] if metadata else {}
    return {
        "kind": kind,
        "name": str(file_meta.get("name") or f"{kind}.txt"),
        "size": int(file_meta.get("size") or len(content.encode("utf-8"))),
        "content_type": str(file_meta.get("content_type") or "text/plain; charset=utf-8"),
        "content": content,
    }


def prepare_protocol_upstream_request(
    state: HarState,
    request: ProtocolRequest,
    trace_out: dict[str, Any] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    files = list(request.files)
    if trace_out is not None:
        trace_out.clear()
        trace_out.update(
            {
                "requested_mode": "file" if request.context_as_file else "inline",
                "delivery_mode": "inline",
                "fallback_reason": "",
                "context_files": [],
            }
        )
    if request.context_as_file:
        if context_upload_degraded(state.user_id):
            # 降级窗口内：整段上下文回退模式 A（纯文本 prompt），请求照常处理。
            log_event("context_upload_degrade_skip", user_id_fp=sha16(state.user_id), fallback="mode_a")
            if trace_out is not None:
                trace_out["fallback_reason"] = "degraded_window"
            effective_tools = tools_allowed_by_policy(request.tools, request.tool_choice)
            return apply_output_integrity_guard(request.context_text, effective_tools, request.messages), files
        effective_tools = tools_allowed_by_policy(request.tools, request.tool_choice)
        tools_text = build_tools_transcript(request.tools, request.tool_choice)
        history_text = build_history_transcript(request.messages)
        generated_files: list[dict[str, Any]] = []
        generated_trace: list[dict[str, Any]] = []
        try:
            if tools_text.strip():
                tools_file = upload_context_package_to_zai(state, tools_text, label="tools")
                generated_files.append(tools_file)
                generated_trace.append(_context_file_trace_item("tools", tools_file, tools_text))
            history_file = upload_context_package_to_zai(state, history_text, label="history")
            generated_files.insert(0, history_file)
            generated_trace.insert(0, _context_file_trace_item("history", history_file, history_text))
        except Exception as exc:
            # 上传失败不阻断请求：记入降级计数并回退模式 A。
            log_event("context_file_upload_failed", error=str(exc)[:300], fallback="mode_a")
            for generated in generated_files:
                file_obj = generated.get("file") if isinstance(generated.get("file"), dict) else generated
                file_id = str(file_obj.get("id") or generated.get("id") or "").strip()
                if not file_id:
                    continue
                try:
                    delete_zai_file(state, file_id)
                except Exception as cleanup_exc:
                    log_event(
                        "context_file_cleanup_error",
                        file_id_fp=sha16(file_id),
                        error=str(cleanup_exc)[:200],
                    )
            record_context_upload_failure(state.user_id)
            if trace_out is not None:
                trace_out["fallback_reason"] = "upload_failed"
            return apply_output_integrity_guard(request.context_text, effective_tools, request.messages), files
        record_context_upload_success(state.user_id)
        files = generated_files + files
        if trace_out is not None:
            trace_out["delivery_mode"] = "file"
            trace_out["context_files"] = generated_trace
        # History is already externalized in the uploaded transcript. Like the reference,
        # only tools callable in this turn decide whether the live prompt needs the guard.
        return apply_output_integrity_guard(request.execution_prompt, effective_tools, []), files
    effective_tools = tools_allowed_by_policy(request.tools, request.tool_choice)
    return apply_output_integrity_guard(request.execution_prompt, effective_tools, request.messages), files


def openai_usage(turn: ProtocolTurn) -> dict[str, int]:
    return {
        "prompt_tokens": turn.input_tokens,
        "completion_tokens": turn.output_tokens,
        "total_tokens": turn.input_tokens + turn.output_tokens,
    }


def responses_usage(turn: ProtocolTurn) -> dict[str, Any]:
    return {
        "input_tokens": turn.input_tokens,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": turn.output_tokens,
        "output_tokens_details": {"reasoning_tokens": estimate_protocol_tokens(turn.thinking)},
        "total_tokens": turn.input_tokens + turn.output_tokens,
    }


def openai_tool_calls_payload(tool_calls: list[ToolCall]) -> list[dict[str, Any]]:
    return [
        {
            "id": call.id,
            "type": "function",
            "function": {"name": call.name, "arguments": compact_json(call.arguments)},
        }
        for call in tool_calls
    ]


def build_openai_chat_completion(request: ProtocolRequest, turn: ProtocolTurn, completion_id: str | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": turn.text or None}
    if request.options.include_thinking and turn.thinking:
        message["reasoning_content"] = turn.thinking
    if turn.tool_calls:
        message["tool_calls"] = openai_tool_calls_payload(turn.tool_calls)
    return {
        "id": completion_id or f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": request.response_model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls" if turn.tool_calls else "stop",
            }
        ],
        "usage": openai_usage(turn),
    }


def build_reasoning_output_item(reasoning_id: str, text: str) -> dict[str, Any]:
    """OpenAI Responses `reasoning` output item carrying the thinking summary."""
    return {
        "id": reasoning_id,
        "type": "reasoning",
        "status": "completed",
        "summary": [{"type": "summary_text", "text": text}],
    }


def build_responses_output(turn: ProtocolTurn, include_reasoning: bool = False) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    if include_reasoning:
        output.append(build_reasoning_output_item("rs_" + uuid.uuid4().hex, turn.thinking))
    if turn.text or not turn.tool_calls:
        output.append(
            {
                "id": "msg_" + uuid.uuid4().hex,
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": turn.text, "annotations": []}],
            }
        )
    for call in turn.tool_calls:
        output.append(
            {
                "id": "fc_" + uuid.uuid4().hex[:24],
                "type": "function_call",
                "status": "completed",
                "call_id": call.id,
                "name": call.name,
                "arguments": compact_json(call.arguments),
            }
        )
    return output


def build_openai_response_object(
    response_id: str,
    request: ProtocolRequest,
    turn: ProtocolTurn | None = None,
    status: str = "in_progress",
    output_override: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    completed = status == "completed"
    if output_override is not None:
        output = output_override
    elif completed and turn is not None:
        # Match the streaming path: whenever thinking is enabled and exposed,
        # the output list carries a reasoning item (empty text when the model
        # produced no thinking), so both paths stay SDK-consistent.
        output = build_responses_output(
            turn,
            include_reasoning=bool(request.options.include_thinking and request.options.enable_thinking),
        )
    else:
        output = []
    return {
        "id": response_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": status,
        "completed_at": int(time.time()) if completed else None,
        "error": None,
        "incomplete_details": None,
        "instructions": None,
        "max_output_tokens": None,
        "model": request.response_model,
        "output": output,
        "output_text": turn.text if completed and turn is not None else "",
        "parallel_tool_calls": not request.tool_choice.disable_parallel,
        "previous_response_id": request.previous_response_id or None,
        "reasoning": {
            "effort": request.options.reasoning_effort if request.options.enable_thinking else None,
            "summary": (
                [{"type": "summary_text", "text": turn.thinking}]
                if completed
                and turn is not None
                and request.options.include_thinking
                and turn.thinking
                else None
            ),
        },
        "store": request.store,
        "temperature": None,
        "text": {"format": {"type": "text"}},
        "tool_choice": request.tool_choice.mode,
        "tools": request.tools,
        "top_p": None,
        "truncation": "disabled",
        "usage": responses_usage(turn) if completed and turn is not None else None,
        "metadata": {},
    }


def build_anthropic_message(request: ProtocolRequest, turn: ProtocolTurn, message_id: str | None = None) -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    if request.options.include_thinking and turn.thinking:
        # A real Anthropic signature cannot be synthesized. The empty marker is
        # deliberately explicit so clients can distinguish local compatibility
        # thinking from a provider-signed Claude thinking block.
        content.append({"type": "thinking", "thinking": turn.thinking, "signature": ""})
    if turn.text or not turn.tool_calls:
        content.append({"type": "text", "text": turn.text})
    for call in turn.tool_calls:
        content.append({"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments})
    return {
        "id": message_id or "msg_" + uuid.uuid4().hex,
        "type": "message",
        "role": "assistant",
        "model": request.response_model,
        "content": content,
        "stop_reason": "tool_use" if turn.tool_calls else "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": turn.input_tokens, "output_tokens": turn.output_tokens},
    }


def protocol_messages_with_turn(request: ProtocolRequest, turn: ProtocolTurn) -> list[dict[str, Any]]:
    history = [dict(message) for message in request.messages]
    assistant: dict[str, Any] = {"role": "assistant", "content": turn.text}
    if turn.tool_calls:
        assistant["tool_calls"] = [
            {"id": call.id, "name": call.name, "arguments": dict(call.arguments)} for call in turn.tool_calls
        ]
    history.append(assistant)
    return history


_QUIET_ACCESS_PATHS = {
    # 面板高频轮询端点：不刷访问日志，失败(>=400)仍记录
    "/api/status",
    "/api/auth/profiles",
    "/api/auth/state",
    "/api/logs",
    "/api/metrics",
    "/healthz",
    "/readyz",
    "/favicon.ico",
}


def _guard_dispatch(handler_method: Callable[[Any], None]) -> Callable[[Any], None]:
    """Wrap do_GET/do_POST: request id, access log, and a last-resort error net."""

    def wrapper(self: Any) -> None:
        rid = uuid.uuid4().hex[:8]
        set_current_request_id(rid)
        self._rid = rid
        self._request_started_at = time.time()
        self._request_started_mono = time.monotonic()
        self._response_code = 0
        path = urlsplit(self.path).path
        quiet_access = path in _QUIET_ACCESS_PATHS
        if not self.quiet and not quiet_access:
            LOG.info("[%s] REQ %s %s", rid, self.command, self.path[:300])
        try:
            handler_method(self)
        except (BrokenPipeError, ConnectionResetError):
            if not getattr(self, "_response_code", 0):
                self._response_code = 499
            log_event("client_disconnected", path=path)
        except Exception:
            self._response_code = 500
            LOG.exception("[%s] unhandled dispatcher error %s %s", rid, self.command, self.path[:300])
            try:
                self._json_response(
                    500,
                    {"ok": False, "error": {"message": "internal server error (see logs/glm2api.log)"}},
                )
            except Exception:
                pass
        finally:
            duration_ms = max(0, int((time.monotonic() - self._request_started_mono) * 1000))
            code = max(0, int(getattr(self, "_response_code", 0) or 0))
            if path not in _QUIET_ACCESS_PATHS:
                RUNTIME_METRICS.record_http(self.command, path, code, duration_ms)
            if not (code < 400 and (self.quiet or path in _QUIET_ACCESS_PATHS)):
                level = logging.ERROR if code >= 500 else logging.WARNING if code >= 400 else logging.INFO
                LOG.log(level, "[%s] RES %s %s -> %s (%d ms)", rid, self.command, path, code or "-", duration_ms)
            set_current_request_id("")

    wrapper.__name__ = handler_method.__name__
    return wrapper


class ProxyHandler(BaseHTTPRequestHandler):
    timeout = 120  # 防止慢速/停滞客户端长期占用连接线程；不影响服务端主动推送 SSE
    _head_only: bool = False  # do_HEAD 置位：响应只带头部不写体（见 _send_bytes）
    state: HarState | None = None
    profiles: dict[str, AccountProfile] = {}
    active_profile_id: str = ""
    state_lock = threading.RLock()
    profile_store_path: Path = PROFILE_STORE_PATH
    profile_store_saved_at: str = ""
    profile_store_error: str = ""
    captcha_verify_param: str | None = None
    fresh_captcha_browser: bool = False
    chrome_path: str | None = None
    captcha_headless: bool = True
    captcha_timeout_ms: int = 75_000
    upstream_timeout_sec: int = UPSTREAM_STREAM_TIMEOUT_SEC
    upstream_retry_wait_sec: float = DEFAULT_UPSTREAM_RETRY_WAIT_SEC
    upstream_retry_max_attempts: int = DEFAULT_UPSTREAM_RETRY_ATTEMPTS
    upstream_timeout_locked: bool = False  # CLI --upstream-timeout-sec 显式配置时面板不可覆盖
    browser_login_timeout_ms: int = 300_000
    browser_login_progress: dict[str, Any] = {"running": False, "mode": "", "stage": "空闲", "updated_at": "", "error": ""}
    include_thinking: bool = False
    # API 协议面（OpenAI / Responses / Anthropic）默认回传思维链，不受面板"显示
    # Thinking"开关影响；请求体 include_thinking:false 仍可显式关闭。
    api_include_thinking_default: bool = True
    settings: dict[str, Any] = local_settings_defaults()
    settings_path: Path = SETTINGS_STORE_PATH
    settings_saved_at: str = ""
    settings_error: str = ""
    api_key: str = ""
    api_key_store_path: Path = API_KEY_STORE_PATH
    api_key_saved_at: str = ""
    api_key_store_error: str = ""
    api_key_source: str = "store"
    cors_origins: tuple[str, ...] = ()
    web_index_cache: str = ""
    web_index_cache_mtime_ns: int = 0
    response_store: dict[str, StoredResponse] = {}
    response_store_lock = threading.RLock()
    chat_inflight: dict[str, int] = {}
    chat_inflight_lock = threading.RLock()
    _chat_slot_local = threading.local()

    def _try_acquire_chat_slot(self) -> str | None:
        """Up to MAX_CONCURRENT_GENERATIONS_PER_PROFILE in-flight generations per account profile.

        Returns the profile id that owns the slot, or None when that profile is
        already at the concurrency cap. The owner id is returned so the slot
        can later be released even if the active profile is switched mid-request.
        """
        with self.chat_inflight_lock:
            profile_id = self.active_profile_id
            if self.chat_inflight.get(profile_id, 0) >= MAX_CONCURRENT_GENERATIONS_PER_PROFILE:
                return None
            self.chat_inflight[profile_id] = self.chat_inflight.get(profile_id, 0) + 1
            return profile_id

    def _release_chat_slot(self, profile_id: str) -> None:
        with self.chat_inflight_lock:
            remaining = self.chat_inflight.get(profile_id, 0) - 1
            if remaining > 0:
                self.chat_inflight[profile_id] = remaining
            else:
                self.chat_inflight.pop(profile_id, None)

    def _chat_slot_owner_get(self) -> str | None:
        return getattr(self._chat_slot_local, "owner", None)

    def _chat_slot_owner_set(self, profile_id: str) -> None:
        self._chat_slot_local.owner = profile_id

    def _chat_slot_owner_clear(self) -> None:
        try:
            del self._chat_slot_local.owner
        except AttributeError:
            pass

    def _release_chat_slot_early(self) -> None:
        """Free this request's generation slot as soon as the upstream stream is consumed.

        Post-stream housekeeping (best-effort chat deletion, error logging)
        can block for many seconds; the slot must not stay busy during it, or
        the next requests on the same account would hit the concurrency cap
        even though the previous generation already finished. The owner lives
        in thread-local storage because several generations may run at once.
        """
        owner = self._chat_slot_owner_get()
        if owner is None:
            return
        with self.chat_inflight_lock:
            self._release_chat_slot(owner)
            self._chat_slot_owner_clear()

    @staticmethod
    def _chat_slot_guard(render_busy_name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Decorator: reject requests beyond the per-account concurrency cap."""

        def decorate(method: Callable[..., Any]) -> Callable[..., Any]:
            @functools.wraps(method)
            def wrapper(self_: "ProxyHandler", *args: Any, **kwargs: Any) -> Any:
                acquired = self_._try_acquire_chat_slot()
                if acquired is None:
                    getattr(self_, render_busy_name)()
                    return None
                self_._chat_slot_owner_set(acquired)
                try:
                    return method(self_, *args, **kwargs)
                finally:
                    owner = self_._chat_slot_owner_get()
                    if owner is not None:
                        self_._chat_slot_owner_clear()
                        self_._release_chat_slot(owner)

            return wrapper

        return decorate

    def _web_busy(self) -> None:
        self._json_response(
            429,
            {
                "ok": False,
                "error": {
                    "message": "当前账号同时进行中的生成请求已达上限（3 个），请等待完成后再试",
                    "type": "chat_slot_busy",
                },
            },
            extra_headers={"Retry-After": "3"},
        )

    def _openai_busy(self) -> None:
        self._json_response(
            429,
            {
                "error": {
                    "message": "当前账号同时进行中的生成请求已达上限（3 个），请稍后重试",
                    "type": "rate_limit_error",
                    "code": "chat_slot_busy",
                }
            },
            extra_headers={"Retry-After": "3"},
        )

    def _anthropic_busy(self) -> None:
        self._json_response(
            429,
            {
                "type": "error",
                "error": {
                    "type": "rate_limit_error",
                    "message": "当前账号已有进行中的生成请求，请稍后重试",
                },
            },
            extra_headers={"Retry-After": "3"},
        )

    def _active_state(self) -> HarState:
        with self.state_lock:
            profile = self.profiles.get(self.active_profile_id)
            if profile:
                return profile.state
            if self.state:
                return self.state
            raise RuntimeError("当前没有登录态。请先在网页右侧使用“浏览器登录并切换”或上传 HAR。")

    def _openai_error(self, status: int, message: str, code: str | None = None, extra_headers: dict[str, str] | None = None) -> None:
        self._json_response(
            status,
            {
                "error": {
                    "message": str(message),
                    "type": "invalid_request_error" if status < 500 else "server_error",
                    "param": None,
                    "code": code,
                }
            },
            extra_headers=extra_headers,
        )

    def _anthropic_error(self, status: int, message: str, extra_headers: dict[str, str] | None = None) -> None:
        if status < 500:
            error_type = "invalid_request_error"
        elif status == 529:
            # Anthropic 官方对过载使用 529 overloaded_error，SDK 据此自动重试。
            error_type = "overloaded_error"
        else:
            error_type = "api_error"
        self._json_response(status, {"type": "error", "error": {"type": error_type, "message": str(message)}}, extra_headers=extra_headers)

    def _cleanup_response_store_locked(self) -> None:
        now = time.monotonic()
        expired = [response_id for response_id, item in self.response_store.items() if item.expires_at <= now]
        for response_id in expired:
            self.response_store.pop(response_id, None)
        overflow = len(self.response_store) - MAX_RESPONSE_STORE_ITEMS
        if overflow > 0:
            oldest = sorted(self.response_store.items(), key=lambda item: item[1].expires_at)[:overflow]
            for response_id, _item in oldest:
                self.response_store.pop(response_id, None)

    def _store_response(self, response_id: str, payload: dict[str, Any], messages: list[dict[str, Any]]) -> None:
        with self.response_store_lock:
            self.response_store[response_id] = StoredResponse(
                payload=payload,
                messages=messages,
                expires_at=time.monotonic() + RESPONSE_STORE_TTL_SECONDS,
            )
            # Cleanup after insertion so the configured cap is never exceeded,
            # even between this write and the next store/read operation.
            self._cleanup_response_store_locked()

    def _get_stored_response(self, response_id: str) -> StoredResponse | None:
        with self.response_store_lock:
            self._cleanup_response_store_locked()
            return self.response_store.get(response_id)

    def _clear_deleted_chat_from_active_profile(self, state: HarState, chat_id: str) -> None:
        changed = False
        with self.state_lock:
            profile = self.profiles.get(self.active_profile_id)
            if profile and profile.state is state and profile.state.chat_id == chat_id:
                profile.state.chat_id = ""
                changed = True
            if self.state is state and self.state.chat_id == chat_id:
                self.state.chat_id = ""
                changed = True
        if changed:
            self._save_profile_store()

    def _delete_completed_upstream_chat(
        self,
        state: HarState,
        context: dict[str, Any],
        options: ChatOptions,
    ) -> tuple[str, bool, str]:
        """Best-effort cleanup after a successful response, never hiding its output."""
        chat_id = str(context.get("chat_id") or "").strip()
        if not options.delete_chat_after_completion or not chat_id:
            return chat_id, False, ""
        try:
            delete_zai_chat(state, chat_id)
        except Exception as exc:
            return chat_id, False, str(exc)
        self._clear_deleted_chat_from_active_profile(state, chat_id)
        return chat_id, True, ""

    def _schedule_upstream_chat_delete(
        self,
        state: HarState,
        context: dict[str, Any],
        options: ChatOptions,
    ) -> tuple[str, bool, str]:
        """后台异步删除：自动删除实测需要 1-4 秒，不应阻塞响应尾部。

        返回 (chat_id, False, "")；删除结果通过日志事件暴露
        （upstream_chat_deleted / auto_delete_failed），失败可在面板手动补删。
        """
        chat_id = str(context.get("chat_id") or "").strip()
        if not options.delete_chat_after_completion or not chat_id:
            return chat_id, False, ""
        log_event("auto_delete_scheduled", chat_id_fp=sha16(chat_id))

        def _work() -> None:
            try:
                delete_zai_chat(state, chat_id)
            except Exception as exc:
                log_event("auto_delete_failed", chat_id_fp=sha16(chat_id), error=str(exc)[:300])
                return
            self._clear_deleted_chat_from_active_profile(state, chat_id)
            log_event("auto_delete_completed", chat_id_fp=sha16(chat_id))

        _submit_auto_delete(_work)
        return chat_id, False, ""

    def _cleanup_failed_upstream_chat(
        self,
        state: HarState,
        context: dict[str, Any],
        options: ChatOptions,
    ) -> None:
        """Best-effort delete of a chat created for a request that failed mid-stream."""
        if not options.delete_chat_after_completion:
            return
        chat_id = str(context.get("chat_id") or "").strip()
        if not chat_id:
            return
        if context.get("_failed_cleanup_scheduled"):
            return
        context["_failed_cleanup_scheduled"] = True
        log_event("failed_chat_cleanup_scheduled", chat_id_fp=sha16(chat_id))

        def _work() -> None:
            try:
                delete_zai_chat(state, chat_id)
            except Exception as exc:
                log_event(
                    "failed_chat_cleanup_error",
                    chat_id_fp=sha16(chat_id),
                    error=str(exc)[:300],
                )
                return
            self._clear_deleted_chat_from_active_profile(state, chat_id)
            log_event("failed_chat_cleanup_completed", chat_id_fp=sha16(chat_id))

        _submit_auto_delete(_work)

    def _cleanup_failed_upstream_files(self, state: HarState, files: list[dict[str, Any]] | None) -> None:
        """Best-effort delete of uploaded files orphaned by a failed chat attempt."""
        file_ids: list[str] = []
        for item in files or []:
            if not isinstance(item, dict):
                continue
            file_obj = item.get("file") if isinstance(item.get("file"), dict) else item
            file_id = str(file_obj.get("id") or item.get("id") or "").strip()
            if file_id and file_id not in file_ids:
                file_ids.append(file_id)
        for file_id in file_ids:
            try:
                delete_zai_file(state, file_id)
            except Exception as exc:
                log_event(
                    "orphan_file_cleanup_error",
                    file_id_fp=sha16(file_id),
                    error=str(exc)[:300],
                )

    def _complete_protocol_turn(
        self,
        request: ProtocolRequest,
        state: HarState,
        context: dict[str, Any],
        text: str,
        thinking: str,
    ) -> ProtocolTurn:
        turn = finalize_protocol_turn(request, text, thinking)
        chat_id, deleted, delete_error = self._schedule_upstream_chat_delete(state, context, request.options)
        turn.upstream_chat_id = chat_id
        turn.upstream_chat_deleted = deleted
        turn.upstream_chat_delete_error = delete_error
        if delete_error:
            log_event(
                "auto_delete_failed",
                chat_id_fp=sha16(chat_id) if chat_id else "",
                error=delete_error[:300],
            )
        return turn

    def _start_protocol_completion(
        self,
        request: ProtocolRequest,
        history_record_id: str = "",
    ) -> tuple[Iterable[str], dict[str, Any], HarState]:
        state = self._active_state()
        delivery_trace: dict[str, Any] = {}
        prompt, files = prepare_protocol_upstream_request(state, request, trace_out=delivery_trace)
        create_chat = not (request.options.mode in {"continue", "edit", "reuse"} and request.options.chat_id)
        log_event(
            "upstream_call_start",
            surface=request.surface,
            model=request.options.model,
            reasoning_effort=request.options.reasoning_effort,
            enable_thinking=request.options.enable_thinking,
            stream=request.stream,
            tools=len(request.tools),
            tool_choice=request.tool_choice.mode,
            context_mode=str(delivery_trace.get("delivery_mode") or "inline"),
            context_mode_requested=str(delivery_trace.get("requested_mode") or "inline"),
            context_fallback=str(delivery_trace.get("fallback_reason") or ""),
            context_chars=len(request.context_text),
            prompt_chars=len(prompt),
            files=len(files),
            reuse_chat=not create_chat,
            delete_after=request.options.delete_chat_after_completion,
            user_id_fp=sha16(state.user_id) if state.user_id else "",
        )
        context: dict[str, Any] = {}
        user_input = ""
        for item in reversed(request.messages):
            if str(item.get("role") or "").lower() == "user":
                text = history_display_content(item.get("content")).strip()
                if text:
                    user_input = text[:HISTORY_PROMPT_CHARS]
                    break
        # ds2api 同款请求镜像：完整出站消息 + 文件清单 + 上下文 + 最终 prompt + 账号。
        # 文件模式下 history_text 记实际第一个附件（纯对话 transcript），
        # 与"两个附件 + 一个聊天框"的实发结构一一对应；内联模式记整段上下文。
        mirror_context = request.context_text
        if delivery_trace.get("delivery_mode") == "file":
            history_file = next(
                (
                    item
                    for item in delivery_trace.get("context_files") or []
                    if isinstance(item, dict) and item.get("kind") == "history"
                ),
                None,
            )
            mirror_context = str((history_file or {}).get("content") or "")
        history_ctx = {
            "surface": request.surface,
            "stream": request.stream,
            "user_input": user_input,
            "messages": request.messages,
            "context_text": mirror_context,
            "delivery_mode": str(delivery_trace.get("delivery_mode") or "inline"),
            "context_file_requested": delivery_trace.get("requested_mode") == "file",
            "context_file_fallback": str(delivery_trace.get("fallback_reason") or ""),
            "context_files": delivery_trace.get("context_files") or [],
            "account": (state.user_id or "")[:8],
        }
        if history_record_id:
            history_ctx["_record_id"] = history_record_id
        events = stream_zai_completion(
            state,
            prompt,
            create_chat=create_chat,
            chat_id=request.options.chat_id or None,
            user_msg_id=request.options.user_msg_id or None,
            captcha_verify_param=self.captcha_verify_param,
            fresh_captcha_browser=self.fresh_captcha_browser,
            chrome_path=self.chrome_path,
            captcha_headless=self.captcha_headless,
            captcha_timeout_ms=self.captcha_timeout_ms,
            upstream_timeout_sec=self.upstream_timeout_sec,
            retry_wait_sec=self.upstream_retry_wait_sec,
            retry_attempts=self.upstream_retry_max_attempts,
            options=request.options,
            context_out=context,
            files=files,
            history_ctx=history_ctx,
        )
        return events, context, state

    def _consume_protocol_events(
        self,
        events: Iterable[str],
        state: HarState,
        request: ProtocolRequest,
        context: dict[str, Any],
        progress: Callable[[], None] | None = None,
    ) -> tuple[str, str]:
        text_parts: list[str] = []
        thinking_parts: list[str] = []
        try:
            for event in events:
                if progress is not None:
                    progress()
                error = extract_error_from_event(event)
                if error:
                    raise RuntimeError(error)
                delta, phase = extract_delta_from_event(event)
                if not delta:
                    continue
                if phase.lower() == "thinking":
                    thinking_parts.append(delta)
                else:
                    text_parts.append(delta)
        except Exception:
            close = getattr(events, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
            self._release_chat_slot_early()
            self._cleanup_failed_upstream_chat(state, context, request.options)
            raise
        self._release_chat_slot_early()
        return "".join(text_parts), "".join(thinking_parts)

    def _complete_turn_with_required_retry(
        self,
        request: ProtocolRequest,
        state: HarState,
        context: dict[str, Any],
        text: str,
        thinking: str,
        regenerate: Callable[[ProtocolRequest], tuple[str, str, dict[str, Any], HarState]] | None,
    ) -> ProtocolTurn:
        try:
            return self._complete_protocol_turn(request, state, context, text, thinking)
        except ToolCallFormatError as exc:
            # One bounded correction pass covers both missing required calls
            # and recognizable-but-malformed call markup.
            if regenerate is None:
                raise
            retry_reason = str(exc)
        self._cleanup_failed_upstream_chat(state, context, request.options)
        retry_request = protocol_request_with_tool_retry_hint(request, retry_reason)
        log_event("tool_call_format_retry", model=request.options.model, reason=retry_reason[:200])
        text2, thinking2, context2, state2 = regenerate(retry_request)
        try:
            return self._complete_protocol_turn(retry_request, state2, context2, text2, thinking2)
        except Exception:
            self._cleanup_failed_upstream_chat(state2, context2, retry_request.options)
            raise

    def _collect_protocol_turn(self, request: ProtocolRequest) -> ProtocolTurn:
        events, context, state = self._start_protocol_completion(request)
        text, thinking = self._consume_protocol_events(events, state, request, context)

        def regenerate(retry_request: ProtocolRequest) -> tuple[str, str, dict[str, Any], HarState]:
            events2, context2, state2 = self._start_protocol_completion(
                retry_request, str(context.get("_history_record_id") or "")
            )
            text2, thinking2 = self._consume_protocol_events(events2, state2, retry_request, context2)
            return text2, thinking2, context2, state2

        return self._complete_turn_with_required_retry(request, state, context, text, thinking, regenerate)

    def _profiles_payload(self) -> dict[str, Any]:
        with self.state_lock:
            active_id = self.active_profile_id
            user_counts = profile_user_counts(self.profiles)
            profiles = [
                profile_summary(
                    profile,
                    active=pid == active_id,
                    same_user_count=user_counts.get(profile.state.user_id, 1),
                )
                for pid, profile in self.profiles.items()
            ]
            duplicate_stats = profile_duplicate_stats(self.profiles)
            return {
                "ok": True,
                "active_profile_id": active_id,
                "profiles": profiles,
                "duplicate_stats": duplicate_stats,
                "profile_store": {
                    "path": self.profile_store_path.name,
                    "exists": self.profile_store_path.exists(),
                    "encryption": "windows-dpapi-current-user",
                    "saved_at": self.profile_store_saved_at,
                    "error": self.profile_store_error,
                },
            }

    def _save_profile_store(self) -> None:
        with self.state_lock:
            try:
                save_profile_store(self.profiles, self.active_profile_id, self.profile_store_path)
                self.__class__.profile_store_saved_at = datetime.now().astimezone().isoformat(timespec="seconds")
                self.__class__.profile_store_error = ""
            except Exception as exc:
                self.__class__.profile_store_error = str(exc)

    def _api_key_authorized(self) -> bool:
        configured = str(self.api_key or "")
        if not configured:
            return True
        provided = str(self.headers.get("X-API-Key") or "").strip()
        if not provided:
            auth = str(self.headers.get("Authorization") or "").strip()
            if auth.lower().startswith("bearer "):
                provided = auth[7:].strip()
        return bool(provided) and hmac.compare_digest(provided.encode("utf-8"), configured.encode("utf-8"))

    def _require_api_key(self) -> bool:
        if self._api_key_authorized():
            return True
        self._json_response(
            401,
            {
                "ok": False,
                "error": {
                    "message": "invalid or missing API key",
                    "type": "authentication_error",
                    "code": "invalid_api_key",
                },
            },
        )
        return False

    def _api_key_config_allowed(self) -> bool:
        host = str(self.client_address[0] if self.client_address else "")
        if host and is_loopback_host(host):
            return True
        return self._api_key_authorized()

    def _api_key_config_payload(self) -> dict[str, Any]:
        return {
            "ok": True,
            "enabled": bool(self.api_key),
            "source": self.api_key_source,
            "saved_at": self.api_key_saved_at,
            "error": self.api_key_store_error,
            "path": self.api_key_store_path.name,
        }

    def _save_api_key_from_panel(self, new_key: str, current_key: str) -> dict[str, Any]:
        if self.api_key_source == "cli":
            raise ValueError("当前 API Key 由 GLM2API_API_KEY/--api-key 配置；请在面板配置前移除该启动参数")
        if self.api_key and not hmac.compare_digest(
            current_key.encode("utf-8"),
            self.api_key.encode("utf-8"),
        ):
            raise PermissionError("current API key is incorrect")
        normalized = str(new_key or "").strip()
        saved_at = save_api_key_store(normalized, self.api_key_store_path)
        self.__class__.api_key = normalized
        self.__class__.api_key_saved_at = saved_at
        self.__class__.api_key_source = "store"
        self.__class__.api_key_store_error = ""
        return self._api_key_config_payload()

    def _handle_api_key_config(self) -> None:
        try:
            if not self._api_key_config_allowed():
                self._json_response(
                    401,
                    {
                        "ok": False,
                        "error": {
                            "message": "invalid or missing API key",
                            "type": "authentication_error",
                            "code": "invalid_api_key",
                        },
                    },
                )
                return
            body = self._read_json_body()
            new_key = str(body.get("api_key") or "").strip()
            current_key = str(body.get("current_key") or "").strip()
            try:
                payload = self._save_api_key_from_panel(new_key, current_key)
            except PermissionError as exc:
                self._json_response(
                    401,
                    {
                        "ok": False,
                        "error": {
                            "message": str(exc),
                            "type": "authentication_error",
                            "code": "invalid_current_api_key",
                        },
                    },
                )
                return
            self._json_response(
                200,
                {
                    **payload,
                    "message": "API Key 已更新并加密保存" if payload["enabled"] else "API Key 已清除",
                },
            )
        except Exception as exc:
            self._json_response(400, {"ok": False, "error": {"message": str(exc)}})

    def _settings_payload(self) -> dict[str, Any]:
        return {
            "ok": True,
            "settings": dict(self.settings),
            "saved_at": self.settings_saved_at,
            "path": self.settings_path.name,
            "error": self.settings_error,
        }

    def _save_settings(self, settings: dict[str, Any]) -> None:
        normalized = normalize_local_settings(settings, self.settings)
        try:
            saved_at = save_local_settings(normalized, self.settings_path)
            self.__class__.settings = normalized
            self.__class__.settings_saved_at = saved_at
            self.__class__.settings_error = ""
            self.__class__.include_thinking = bool(normalized.get("include_thinking", False))
            if not self.upstream_timeout_locked:
                self.__class__.upstream_timeout_sec = int(
                    normalized.get("upstream_timeout_sec") or UPSTREAM_STREAM_TIMEOUT_SEC
                )
            self.__class__.upstream_retry_wait_sec = float(
                normalized.get("upstream_retry_wait_sec", DEFAULT_UPSTREAM_RETRY_WAIT_SEC)
            )
            self.__class__.upstream_retry_max_attempts = int(
                normalized.get("upstream_retry_max_attempts", DEFAULT_UPSTREAM_RETRY_ATTEMPTS)
            )
            _HISTORY_CONF["max_records"] = max(50, min(2000, int(normalized.get("history_max_records") or 300)))
        except Exception as exc:
            self.__class__.settings_error = str(exc)
            raise

    def _cors_origin(self) -> str:
        origin = str(self.headers.get("Origin") or "")
        if not origin:
            return ""
        if "*" in self.cors_origins:
            return "*"
        return origin if origin in self.cors_origins else ""

    def _send_cors_headers(self) -> None:
        origin = self._cors_origin()
        if not origin:
            return
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header(
            "Access-Control-Allow-Headers",
            "content-type, authorization, x-filename, x-api-key, anthropic-version, anthropic-beta",
        )
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Max-Age", "600")
        if origin != "*":
            self.send_header("Vary", "Origin")

    def _send_security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
            "frame-ancestors 'none'; form-action 'self'",
        )

    def _send_bytes(self, status: int, raw: bytes, content_type: str, extra_headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self._send_cors_headers()
        self._send_security_headers()
        self.end_headers()
        if not self._head_only:
            self.wfile.write(raw)

    def _json_response(self, status: int, payload: dict[str, Any], extra_headers: dict[str, str] | None = None) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Cache-Control": "no-store"}
        headers.update(extra_headers or {})
        self._send_bytes(status, raw, "application/json; charset=utf-8", headers)

    def _html_response(self, status: int, html: str) -> None:
        self._send_bytes(
            status,
            html.encode("utf-8"),
            "text/html; charset=utf-8",
            {"Cache-Control": "no-cache"},
        )

    def _sse_write(self, event: str, payload: dict[str, Any]) -> None:
        raw = f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")
        self.wfile.write(raw)
        self.wfile.flush()

    def _sse_keepalive(self) -> None:
        self.wfile.write(b": keep-alive\n\n")
        self.wfile.flush()

    @staticmethod
    def _close_upstream_events(events: Iterable[str]) -> None:
        close = getattr(events, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    def _content_length(self, max_bytes: int, allow_empty: bool = False) -> int:
        value = self.headers.get("Content-Length")
        if value is None:
            if allow_empty:
                return 0
            raise ValueError("missing Content-Length")
        try:
            length = int(value)
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if length < 0:
            raise ValueError("invalid Content-Length")
        if length == 0 and not allow_empty:
            raise ValueError("missing request body")
        if length > max_bytes:
            raise ValueError("request body too large")
        return length

    def _read_json_body(self, max_bytes: int = MAX_JSON_BODY_BYTES) -> dict[str, Any]:
        length = self._content_length(max_bytes, allow_empty=True)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise ValueError("incomplete request body")
        body = json.loads(raw.decode("utf-8"))
        if not isinstance(body, dict):
            raise ValueError("JSON body must be an object")
        return body

    def _read_raw_body(self, max_bytes: int = MAX_HAR_UPLOAD_BYTES) -> bytes:
        length = self._content_length(max_bytes)
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise ValueError("incomplete request body")
        return raw

    def _spool_raw_body(self, max_bytes: int, prefix: str, suffix: str) -> Path:
        length = self._content_length(max_bytes)
        tmp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(prefix=prefix, suffix=suffix, delete=False) as f:
                tmp_path = Path(f.name)
                remaining = length
                while remaining:
                    chunk = self.rfile.read(min(UPLOAD_STREAM_CHUNK_BYTES, remaining))
                    if not chunk:
                        raise ValueError("incomplete request body")
                    f.write(chunk)
                    remaining -= len(chunk)
            return tmp_path
        except Exception:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)
            raise

    def _prompt_from_body(self, body: dict[str, Any]) -> str:
        direct = body.get("message") or body.get("prompt")
        if isinstance(direct, str) and direct.strip():
            return direct.strip()
        return prompt_from_openai_messages(body.get("messages") or [])

    def _web_index(self) -> str:
        if WEB_INDEX_PATH.exists():
            mtime_ns = WEB_INDEX_PATH.stat().st_mtime_ns
            if self.web_index_cache and self.web_index_cache_mtime_ns == mtime_ns:
                return self.web_index_cache
            html = WEB_INDEX_PATH.read_text(encoding="utf-8")
            self.__class__.web_index_cache = html
            self.__class__.web_index_cache_mtime_ns = mtime_ns
            return html
        return "<!doctype html><meta charset='utf-8'><title>Z.ai GLM-5.2 Proxy</title><p>web/index.html missing</p>"

    def do_OPTIONS(self) -> None:
        if self.headers.get("Origin") and not self._cors_origin():
            self._json_response(403, {"error": {"message": "origin is not allowed"}})
            return
        self.send_response(204)
        self._send_cors_headers()
        self.end_headers()

    def do_HEAD(self) -> None:
        # 探测器/客户端连通性检查会用 HEAD 访问；BaseHTTPRequestHandler 默认回
        # 501 Unsupported method，会让调用方误判服务不可用。按 GET 语义应答、只省略响应体。
        self._head_only = True
        try:
            self.do_GET()
        finally:
            self._head_only = False

    @_guard_dispatch
    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path in {"/", "/index.html"}:
            self._html_response(200, self._web_index())
            return
        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        if path in {"/healthz", "/readyz"}:
            with self.state_lock:
                auth_ready = bool(self.profiles.get(self.active_profile_id) or self.state)
            self._json_response(200, {"ok": True, "auth_ready": auth_ready})
            return
        if path == "/api/hello":
            # 探测端点：有客户端用 HEAD/GET /api/hello 探活（日志 03:58 实测），不应 404。
            self._json_response(200, {"ok": True})
            return
        if path.startswith(("/api/", "/v1/", "/chat/", "/anthropic/", "/responses/", "/messages")) and path not in {"/api/status", "/api/settings/api-key"}:
            if not self._require_api_key():
                return
        if path == "/api/settings/api-key":
            if not self._api_key_config_allowed():
                self._json_response(
                    401,
                    {
                        "ok": False,
                        "error": {
                            "message": "invalid or missing API key",
                            "type": "authentication_error",
                            "code": "invalid_api_key",
                        },
                    },
                )
                return
            self._json_response(200, self._api_key_config_payload())
            return
        if path == "/api/settings":
            self._json_response(200, self._settings_payload())
            return
        if path == "/api/status":
            if self.api_key and not self._api_key_authorized():
                self._json_response(
                    200,
                    {
                        "ok": True,
                        "api_key_required": True,
                        "api_key_valid": False,
                        "auth_ready": False,
                        "supported_models": list(ADVERTISED_MODELS),
                        "api_key_source": self.api_key_source,
                        "api_key_saved_at": self.api_key_saved_at,
                        "upstream_timeout_sec": self.upstream_timeout_sec,
                        "upstream_retry_wait_sec": self.upstream_retry_wait_sec,
                        "upstream_retry_max_attempts": self.upstream_retry_max_attempts,
                    },
                )
                return
            with self.state_lock:
                active_profile = self.profiles.get(self.active_profile_id)
                state = active_profile.state if active_profile else self.state
            with self.chat_inflight_lock:
                active_chat_busy_count = max(0, int(self.chat_inflight.get(self.active_profile_id, 0)))
            self._json_response(
                200,
                {
                    "ok": True,
                    "api_key_required": bool(self.api_key),
                    "api_key_valid": True,
                    "api_key_source": self.api_key_source,
                    "api_key_saved_at": self.api_key_saved_at,
                    "api_key_store_error": self.api_key_store_error,
                    "auth_ready": state is not None,
                    "model": DEFAULT_MODEL,
                    "supported_models": list(ADVERTISED_MODELS),
                    "default_options": chat_options_public(chat_options_from_body(self.settings)),
                    "base_url": BASE_URL,
                    "captcha_mode": "browser_fresh" if self.fresh_captcha_browser else "provided_or_har",
                    "captcha_solver": _CAPTCHA_MODE,
                    "upstream_timeout_sec": self.upstream_timeout_sec,
                    "upstream_retry_wait_sec": self.upstream_retry_wait_sec,
                    "upstream_retry_max_attempts": self.upstream_retry_max_attempts,
                    "include_thinking": self.include_thinking,
                    "active_profile_id": self.active_profile_id,
                    "profile_label": active_profile.label if active_profile else "",
                    "user_id": state.user_id if state else "",
                    "user_name": state.user_name if state else "",
                    "token_fp": sha16(state.token) if state else "",
                    "device_id_fp": sha16(state.device_id) if state and state.device_id else "",
                    "chrome_path": self.chrome_path or default_chrome_path() or "",
                    "protocol_compatibility": {
                        "openai_chat_completions": True,
                        "openai_responses": True,
                        "anthropic_messages": True,
                        "function_tools": True,
                        "context_file": True,
                        "context_file_default": False,
                        "force_history_suffix": FORCE_HISTORY_MODEL_SUFFIX,
                        "auto_delete_after_completion_default": DEFAULT_DELETE_CHAT_AFTER_COMPLETION,
                    },
                    "profile_store": self._profiles_payload()["profile_store"],
                    "chat_busy": active_chat_busy_count > 0,
                    "chat_busy_count": active_chat_busy_count,
                    "limits": {
                        "chat_file_upload_bytes": MAX_CHAT_FILE_UPLOAD_BYTES,
                        "har_upload_bytes": MAX_HAR_UPLOAD_BYTES,
                        "json_body_bytes": MAX_JSON_BODY_BYTES,
                    },
                },
            )
            return
        if path == "/api/auth/browser-login/status":
            with self.state_lock:
                payload = dict(self.__class__.browser_login_progress)
            self._json_response(200, {"ok": True, **payload})
            return
        if path in {"/api/auth/state", "/api/auth/profiles"}:
            self._json_response(200, self._profiles_payload())
            return
        if path in {"/v1/models", "/models"}:
            self._json_response(
                200,
                {
                    "object": "list",
                    "data": [
                        {"id": model, "object": "model", "owned_by": "z.ai"}
                        for model in ADVERTISED_MODELS
                    ],
                },
            )
            return
        if path == "/anthropic/v1/models":
            self._json_response(
                200,
                {
                    "object": "list",
                    "data": [
                        {"id": model, "type": "model", "display_name": model, "created_at": "2026-01-01T00:00:00Z"}
                        for model in ADVERTISED_MODELS
                    ],
                    "has_more": False,
                    "first_id": ADVERTISED_MODELS[0],
                    "last_id": ADVERTISED_MODELS[-1],
                },
            )
            return
        if path == "/api/logs":
            self._handle_api_logs()
            return
        if path == "/api/metrics":
            self._handle_api_metrics()
            return
        if path == "/api/history/chats":
            self._handle_history_chats()
            return
        if path == "/api/history/chat":
            self._handle_history_chat_detail()
            return
        if path == "/api/history/records":
            self._handle_history_records()
            return
        if path == "/api/history/record":
            self._handle_history_record_detail()
            return
        for prefix in ("/v1/responses/", "/responses/"):
            if path.startswith(prefix):
                response_id = path[len(prefix) :].strip()
                if not response_id:
                    break
                stored = self._get_stored_response(response_id)
                if stored is None:
                    self._openai_error(404, "response not found", "response_not_found")
                else:
                    self._json_response(200, stored.payload)
                return
        self._json_response(404, {"error": {"message": "not found"}})

    @_guard_dispatch
    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path.startswith(("/api/", "/v1/", "/chat/", "/anthropic/", "/responses/", "/messages")) and path != "/api/settings/api-key":
            if not self._require_api_key():
                return
        if path == "/api/settings/api-key":
            self._handle_api_key_config()
            return
        if path == "/api/chat/cancel":
            self._handle_chat_cancel()
            return
        if path == "/api/chat/delete":
            self._handle_chat_delete()
            return
        if path == "/api/history/delete":
            self._handle_history_delete()
            return
        if path == "/api/history/record/delete":
            self._handle_history_record_delete()
            return
        if path == "/api/history/clear":
            self._handle_history_clear()
            return
        if path == "/api/chat":
            self._handle_web_chat()
            return
        if path == "/api/settings":
            self._handle_settings_save()
            return
        if path == "/api/auth/har":
            self._handle_auth_har_upload()
            return
        if path == "/api/auth/token":
            self._handle_auth_token()
            return
        if path == "/api/auth/browser-login":
            self._handle_auth_browser_login()
            return
        if path == "/api/auth/captcha-refresh":
            self._handle_auth_captcha_refresh()
            return
        if path == "/api/auth/switch":
            self._handle_auth_switch()
            return
        if path == "/api/auth/remove":
            self._handle_auth_remove()
            return
        if path in {"/api/auth/compact", "/api/auth/dedupe"}:
            self._handle_auth_compact()
            return
        if path == "/api/files/upload":
            self._handle_file_upload()
            return
        if path == "/api/files/cleanup":
            self._handle_file_cleanup()
            return
        if path in {"/v1/chat/completions", "/chat/completions"}:
            self._handle_openai_chat_completions()
            return
        if path in {"/v1/responses", "/responses"}:
            self._handle_openai_responses()
            return
        if path in {"/anthropic/v1/messages", "/v1/messages", "/messages"}:
            self._handle_anthropic_messages()
            return
        if path in {"/anthropic/v1/messages/count_tokens", "/v1/messages/count_tokens", "/messages/count_tokens"}:
            self._handle_anthropic_count_tokens()
            return
        self._json_response(404, {"error": {"message": "not found"}})
        return

    def _write_openai_chunk(self, payload: dict[str, Any]) -> None:
        self.wfile.write(f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8"))
        self.wfile.flush()

    @_chat_slot_guard("_openai_busy")
    def _handle_openai_chat_completions(self) -> None:
        try:
            body = self._read_json_body()
            request = normalize_openai_chat_request(body, self.api_include_thinking_default)
            if request.stream:
                self._stream_openai_chat_completions(request)
                return
            turn = self._collect_protocol_turn(request)
            self._json_response(200, build_openai_chat_completion(request, turn))
        except Exception as exc:
            if not isinstance(exc, ValueError):
                LOG.exception("[%s] openai chat completions failed", current_request_id())
            message = str(exc)
            # 内部重试耗尽后的上游繁忙/验证码失效：503 + Retry-After，让 SDK 走标准退避。
            if not isinstance(exc, ValueError) and is_retryable_upstream_error(message):
                self._openai_error(503, message, extra_headers={"Retry-After": "3"})
                return
            status = 400 if isinstance(exc, ValueError) else 500
            self._openai_error(status, message)

    def _stream_openai_chat_completions(self, request: ProtocolRequest) -> None:
        events, context, state = self._start_protocol_completion(request)
        completion_id = f"chatcmpl-{uuid.uuid4().hex}"
        created = int(time.time())
        buffer_for_tools = bool(request.tools) and request.tool_choice.mode != "none"
        text_parts: list[str] = []
        thinking_parts: list[str] = []
        last_client_write = time.monotonic()

        def keepalive_if_due() -> None:
            nonlocal last_client_write
            now = time.monotonic()
            if now - last_client_write >= SSE_KEEPALIVE_INTERVAL_SECONDS:
                self._sse_keepalive()
                last_client_write = now

        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "close")
            self._send_cors_headers()
            self._send_security_headers()
            self.end_headers()
            self.close_connection = True
            self._write_openai_chunk(
                {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": request.response_model,
                    "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}],
                }
            )
            for event in events:
                error = extract_error_from_event(event)
                if error:
                    raise RuntimeError(error)
                delta, phase = extract_delta_from_event(event)
                if not delta:
                    continue
                if phase.lower() == "thinking":
                    thinking_parts.append(delta)
                    if request.options.include_thinking and not buffer_for_tools:
                        self._write_openai_chunk(
                            {
                                "id": completion_id,
                                "object": "chat.completion.chunk",
                                "created": created,
                                "model": request.response_model,
                                "choices": [
                                    {"index": 0, "delta": {"reasoning_content": delta}, "finish_reason": None}
                                ],
                            }
                        )
                        last_client_write = time.monotonic()
                    else:
                        keepalive_if_due()
                    continue
                text_parts.append(delta)
                if not buffer_for_tools:
                    self._write_openai_chunk(
                        {
                            "id": completion_id,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": request.response_model,
                            "choices": [{"index": 0, "delta": {"content": delta}, "finish_reason": None}],
                        }
                    )
                    last_client_write = time.monotonic()
                else:
                    keepalive_if_due()
            self._release_chat_slot_early()

            def _regenerate_required_turn(retry_request: ProtocolRequest) -> tuple[str, str, dict[str, Any], HarState]:
                events2, context2, state2 = self._start_protocol_completion(
                    retry_request, str(context.get("_history_record_id") or "")
                )
                text2, thinking2 = self._consume_protocol_events(
                    events2, state2, retry_request, context2, progress=keepalive_if_due
                )
                return text2, thinking2, context2, state2

            turn = self._complete_turn_with_required_retry(
                request, state, context, "".join(text_parts), "".join(thinking_parts), _regenerate_required_turn
            )
            if buffer_for_tools and request.options.include_thinking and turn.thinking:
                self._write_openai_chunk(
                    {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": request.response_model,
                        "choices": [
                            {"index": 0, "delta": {"reasoning_content": turn.thinking}, "finish_reason": None}
                        ],
                    }
                )
            if buffer_for_tools and turn.text:
                self._write_openai_chunk(
                    {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": request.response_model,
                        "choices": [{"index": 0, "delta": {"content": turn.text}, "finish_reason": None}],
                    }
                )
            if turn.tool_calls:
                tool_deltas = []
                for index, call in enumerate(turn.tool_calls):
                    tool_deltas.append(
                        {
                            "index": index,
                            "id": call.id,
                            "type": "function",
                            "function": {"name": call.name, "arguments": compact_json(call.arguments)},
                        }
                    )
                self._write_openai_chunk(
                    {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": request.response_model,
                        "choices": [{"index": 0, "delta": {"tool_calls": tool_deltas}, "finish_reason": None}],
                    }
                )
            self._write_openai_chunk(
                {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": request.response_model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {},
                            "finish_reason": "tool_calls" if turn.tool_calls else "stop",
                        }
                    ],
                    "usage": openai_usage(turn),
                }
            )
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            self._close_upstream_events(events)
            self._release_chat_slot_early()
            self._cleanup_failed_upstream_chat(state, context, request.options)
            return
        except Exception as exc:
            self._close_upstream_events(events)
            self._release_chat_slot_early()
            self._cleanup_failed_upstream_chat(state, context, request.options)
            LOG.exception("[%s] streaming turn failed", current_request_id())
            try:
                self._write_openai_chunk({"object": "error", "error": {"message": str(exc), "type": "server_error"}})
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

    @_chat_slot_guard("_openai_busy")
    def _handle_openai_responses(self) -> None:
        try:
            body = self._read_json_body()
            previous_response_id = str(body.get("previous_response_id") or "").strip()
            prior_messages: list[dict[str, Any]] | None = None
            if previous_response_id:
                stored = self._get_stored_response(previous_response_id)
                if stored is None:
                    raise ValueError("previous_response_id not found or expired")
                prior_messages = stored.messages
            request = normalize_openai_responses_request(body, self.api_include_thinking_default, prior_messages)
            response_id = "resp_" + uuid.uuid4().hex
            if request.stream:
                self._stream_openai_responses(request, response_id)
                return
            turn = self._collect_protocol_turn(request)
            payload = build_openai_response_object(response_id, request, turn, status="completed")
            if request.store:
                self._store_response(response_id, payload, protocol_messages_with_turn(request, turn))
            self._json_response(200, payload)
        except Exception as exc:
            if not isinstance(exc, ValueError):
                LOG.exception("[%s] openai responses failed", current_request_id())
            message = str(exc)
            if not isinstance(exc, ValueError) and is_retryable_upstream_error(message):
                self._openai_error(503, message, extra_headers={"Retry-After": "3"})
                return
            status = 400 if isinstance(exc, ValueError) else 500
            self._openai_error(status, message)

    def _stream_openai_responses(self, request: ProtocolRequest, response_id: str) -> None:
        events, context, state = self._start_protocol_completion(request)
        buffer_for_tools = bool(request.tools) and request.tool_choice.mode != "none"
        text_parts: list[str] = []
        thinking_parts: list[str] = []
        sequence = 0
        message_id = "msg_" + uuid.uuid4().hex
        # Thinking is streamed as a standard `reasoning` output item (index 0),
        # so the assistant message and any tool items start at index 1.
        reasoning_wanted = bool(request.options.include_thinking) and bool(request.options.enable_thinking)
        reasoning_id = ("rs_" + uuid.uuid4().hex) if reasoning_wanted else None
        output_offset = 1 if reasoning_wanted else 0
        last_client_write = time.monotonic()

        def send(event_name: str, payload: dict[str, Any]) -> None:
            nonlocal sequence, last_client_write
            sequence += 1
            data = dict(payload)
            data.setdefault("type", event_name)
            data.setdefault("sequence_number", sequence)
            self._sse_write(event_name, data)
            last_client_write = time.monotonic()

        def keepalive_if_due() -> None:
            nonlocal last_client_write
            now = time.monotonic()
            if now - last_client_write >= SSE_KEEPALIVE_INTERVAL_SECONDS:
                self._sse_keepalive()
                last_client_write = now

        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "close")
            self._send_cors_headers()
            self._send_security_headers()
            self.end_headers()
            self.close_connection = True
            initial = build_openai_response_object(response_id, request, status="in_progress")
            send("response.created", {"response": initial})
            send("response.in_progress", {"response": initial})
            if reasoning_wanted:
                send(
                    "response.output_item.added",
                    {
                        "output_index": 0,
                        "item": {
                            "id": reasoning_id,
                            "type": "reasoning",
                            "status": "in_progress",
                            "summary": [{"type": "summary_text", "text": ""}],
                        },
                    },
                )
                send(
                    "response.content_part.added",
                    {
                        "item_id": reasoning_id,
                        "output_index": 0,
                        "content_index": 0,
                        "part": {"type": "summary_text", "text": ""},
                    },
                )
            if not buffer_for_tools:
                send(
                    "response.output_item.added",
                    {
                        "output_index": output_offset,
                        "item": {
                            "id": message_id,
                            "type": "message",
                            "status": "in_progress",
                            "role": "assistant",
                            "content": [],
                        },
                    },
                )
                send(
                    "response.content_part.added",
                    {
                        "item_id": message_id,
                        "output_index": output_offset,
                        "content_index": 0,
                        "part": {"type": "output_text", "text": "", "annotations": []},
                    },
                )
            for event in events:
                error = extract_error_from_event(event)
                if error:
                    raise RuntimeError(error)
                delta, phase = extract_delta_from_event(event)
                if not delta:
                    continue
                if phase.lower() == "thinking":
                    thinking_parts.append(delta)
                    if reasoning_wanted and not buffer_for_tools:
                        send(
                            "response.reasoning_summary_text.delta",
                            {
                                "item_id": reasoning_id,
                                "output_index": 0,
                                "content_index": 0,
                                "delta": delta,
                            },
                        )
                    else:
                        keepalive_if_due()
                    continue
                text_parts.append(delta)
                if not buffer_for_tools:
                    send(
                        "response.output_text.delta",
                        {
                            "item_id": message_id,
                            "output_index": output_offset,
                            "content_index": 0,
                            "delta": delta,
                        },
                    )
                else:
                    keepalive_if_due()
            self._release_chat_slot_early()

            def _regenerate_required_turn(retry_request: ProtocolRequest) -> tuple[str, str, dict[str, Any], HarState]:
                events2, context2, state2 = self._start_protocol_completion(
                    retry_request, str(context.get("_history_record_id") or "")
                )
                text2, thinking2 = self._consume_protocol_events(
                    events2, state2, retry_request, context2, progress=keepalive_if_due
                )
                return text2, thinking2, context2, state2

            turn = self._complete_turn_with_required_retry(
                request, state, context, "".join(text_parts), "".join(thinking_parts), _regenerate_required_turn
            )
            if reasoning_wanted:
                if buffer_for_tools and turn.thinking:
                    send(
                        "response.reasoning_summary_text.delta",
                        {
                            "item_id": reasoning_id,
                            "output_index": 0,
                            "content_index": 0,
                            "delta": turn.thinking,
                        },
                    )
                send(
                    "response.reasoning_summary_text.done",
                    {
                        "item_id": reasoning_id,
                        "output_index": 0,
                        "content_index": 0,
                        "text": turn.thinking,
                    },
                )
                send(
                    "response.content_part.done",
                    {
                        "item_id": reasoning_id,
                        "output_index": 0,
                        "content_index": 0,
                        "part": {"type": "summary_text", "text": turn.thinking},
                    },
                )
                send(
                    "response.output_item.done",
                    {"output_index": 0, "item": build_reasoning_output_item(reasoning_id, turn.thinking)},
                )
            if buffer_for_tools:
                output = build_responses_output(turn, include_reasoning=reasoning_wanted)
                if reasoning_wanted and output and output[0]["type"] == "reasoning":
                    # Keep the streamed reasoning item id stable in the final payload.
                    output = [build_reasoning_output_item(reasoning_id, turn.thinking)] + output[1:]
                # The reasoning item was already announced incrementally at
                # output index 0, so only the message/tool items are replayed.
                for output_index, item in enumerate(output[1:] if reasoning_wanted else output, start=output_offset):
                    send("response.output_item.added", {"output_index": output_index, "item": item})
                    if item["type"] == "message":
                        item_id = str(item["id"])
                        text = str(item["content"][0].get("text") or "")
                        send(
                            "response.content_part.added",
                            {
                                "item_id": item_id,
                                "output_index": output_index,
                                "content_index": 0,
                                "part": {"type": "output_text", "text": "", "annotations": []},
                            },
                        )
                        if text:
                            send(
                                "response.output_text.delta",
                                {
                                    "item_id": item_id,
                                    "output_index": output_index,
                                    "content_index": 0,
                                    "delta": text,
                                },
                            )
                        send(
                            "response.output_text.done",
                            {
                                "item_id": item_id,
                                "output_index": output_index,
                                "content_index": 0,
                                "text": text,
                            },
                        )
                        send(
                            "response.content_part.done",
                            {
                                "item_id": item_id,
                                "output_index": output_index,
                                "content_index": 0,
                                "part": item["content"][0],
                            },
                        )
                    else:
                        arguments = str(item.get("arguments") or "{}")
                        send(
                            "response.function_call_arguments.delta",
                            {"item_id": item["id"], "output_index": output_index, "delta": arguments},
                        )
                        send(
                            "response.function_call_arguments.done",
                            {
                                "item_id": item["id"],
                                "output_index": output_index,
                                "name": item["name"],
                                "arguments": arguments,
                            },
                        )
                    send("response.output_item.done", {"output_index": output_index, "item": item})
            else:
                message_output = {
                    "id": message_id,
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": turn.text, "annotations": []}],
                }
                output = [message_output]
                if reasoning_wanted:
                    output.insert(0, build_reasoning_output_item(reasoning_id, turn.thinking))
                send(
                    "response.output_text.done",
                    {"item_id": message_id, "output_index": output_offset, "content_index": 0, "text": turn.text},
                )
                send(
                    "response.content_part.done",
                    {
                        "item_id": message_id,
                        "output_index": output_offset,
                        "content_index": 0,
                        "part": message_output["content"][0],
                    },
                )
                send("response.output_item.done", {"output_index": output_offset, "item": message_output})
            completed = build_openai_response_object(
                response_id,
                request,
                turn,
                status="completed",
                output_override=output,
            )
            if request.store:
                self._store_response(response_id, completed, protocol_messages_with_turn(request, turn))
            send("response.completed", {"response": completed})
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            self._close_upstream_events(events)
            self._release_chat_slot_early()
            self._cleanup_failed_upstream_chat(state, context, request.options)
            return
        except Exception as exc:
            self._close_upstream_events(events)
            self._release_chat_slot_early()
            self._cleanup_failed_upstream_chat(state, context, request.options)
            LOG.exception("[%s] streaming turn failed", current_request_id())
            try:
                send("error", {"error": {"message": str(exc), "type": "server_error"}})
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

    @_chat_slot_guard("_anthropic_busy")
    def _handle_anthropic_messages(self) -> None:
        try:
            body = self._read_json_body()
            if "max_tokens" in body and int(body["max_tokens"]) <= 0:
                raise ValueError("max_tokens must be positive")
            request = normalize_anthropic_messages_request(body, self.api_include_thinking_default)
            if request.stream:
                self._stream_anthropic_messages(request)
                return
            turn = self._collect_protocol_turn(request)
            self._json_response(200, build_anthropic_message(request, turn))
        except Exception as exc:
            if not isinstance(exc, ValueError):
                LOG.exception("[%s] anthropic messages failed", current_request_id())
            message = str(exc)
            # 内部重试耗尽后的上游繁忙/验证码失效：Anthropic 规范的 529 overloaded_error，
            # SDK 会按过载自动退避重试，而不是把 500 当作未知服务端故障。
            if not isinstance(exc, ValueError) and is_retryable_upstream_error(message):
                self._anthropic_error(529, message, extra_headers={"Retry-After": "3"})
                return
            status = 400 if isinstance(exc, ValueError) else 500
            self._anthropic_error(status, message)

    def _handle_anthropic_count_tokens(self) -> None:
        try:
            body = self._read_json_body()
            request = normalize_anthropic_messages_request(body, self.api_include_thinking_default)
            self._json_response(200, {"input_tokens": estimate_protocol_tokens(request.context_text)})
        except Exception as exc:
            self._anthropic_error(400 if isinstance(exc, ValueError) else 500, str(exc))

    def _stream_anthropic_messages(self, request: ProtocolRequest) -> None:
        events, context, state = self._start_protocol_completion(request)
        # To avoid leaking adapter markup, tool and thinking responses are
        # rendered once their semantic block is known. Plain text keeps genuine
        # low-latency Anthropic streaming.
        buffer_for_semantics = bool(request.tools) and request.tool_choice.mode != "none"
        buffer_for_semantics = buffer_for_semantics or request.options.include_thinking
        text_parts: list[str] = []
        thinking_parts: list[str] = []
        message_id = "msg_" + uuid.uuid4().hex
        last_client_write = time.monotonic()

        def keepalive_if_due() -> None:
            nonlocal last_client_write
            now = time.monotonic()
            if now - last_client_write >= SSE_KEEPALIVE_INTERVAL_SECONDS:
                self._sse_keepalive()
                last_client_write = now

        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "close")
            self._send_cors_headers()
            self._send_security_headers()
            self.end_headers()
            self.close_connection = True
            self._sse_write(
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": message_id,
                        "type": "message",
                        "role": "assistant",
                        "model": request.response_model,
                        "content": [],
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {"input_tokens": estimate_protocol_tokens(request.context_text), "output_tokens": 0},
                    },
                },
            )
            if not buffer_for_semantics:
                self._sse_write(
                    "content_block_start",
                    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                )
            for event in events:
                error = extract_error_from_event(event)
                if error:
                    raise RuntimeError(error)
                delta, phase = extract_delta_from_event(event)
                if not delta:
                    continue
                if phase.lower() == "thinking":
                    thinking_parts.append(delta)
                    keepalive_if_due()
                    continue
                text_parts.append(delta)
                if not buffer_for_semantics:
                    self._sse_write(
                        "content_block_delta",
                        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": delta}},
                    )
                    last_client_write = time.monotonic()
                else:
                    keepalive_if_due()
            self._release_chat_slot_early()

            def _regenerate_required_turn(retry_request: ProtocolRequest) -> tuple[str, str, dict[str, Any], HarState]:
                events2, context2, state2 = self._start_protocol_completion(
                    retry_request, str(context.get("_history_record_id") or "")
                )
                text2, thinking2 = self._consume_protocol_events(
                    events2, state2, retry_request, context2, progress=keepalive_if_due
                )
                return text2, thinking2, context2, state2

            turn = self._complete_turn_with_required_retry(
                request, state, context, "".join(text_parts), "".join(thinking_parts), _regenerate_required_turn
            )
            final_message = build_anthropic_message(request, turn, message_id)
            if buffer_for_semantics:
                for index, block in enumerate(final_message["content"]):
                    block_type = str(block.get("type") or "text")
                    if block_type == "tool_use":
                        start_block = {"type": "tool_use", "id": block["id"], "name": block["name"], "input": {}}
                    elif block_type == "thinking":
                        start_block = {"type": "thinking", "thinking": "", "signature": ""}
                    else:
                        start_block = {"type": "text", "text": ""}
                    self._sse_write(
                        "content_block_start",
                        {"type": "content_block_start", "index": index, "content_block": start_block},
                    )
                    if block_type == "tool_use":
                        delta = {"type": "input_json_delta", "partial_json": compact_json(block.get("input") or {})}
                    elif block_type == "thinking":
                        delta = {"type": "thinking_delta", "thinking": str(block.get("thinking") or "")}
                    else:
                        delta = {"type": "text_delta", "text": str(block.get("text") or "")}
                    if any(str(value) for key, value in delta.items() if key != "type"):
                        self._sse_write(
                            "content_block_delta",
                            {"type": "content_block_delta", "index": index, "delta": delta},
                        )
                    self._sse_write("content_block_stop", {"type": "content_block_stop", "index": index})
            else:
                self._sse_write("content_block_stop", {"type": "content_block_stop", "index": 0})
            self._sse_write(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": final_message["stop_reason"], "stop_sequence": None},
                    "usage": {"output_tokens": turn.output_tokens},
                },
            )
            self._sse_write("message_stop", {"type": "message_stop"})
        except (BrokenPipeError, ConnectionResetError):
            self._close_upstream_events(events)
            self._release_chat_slot_early()
            self._cleanup_failed_upstream_chat(state, context, request.options)
            return
        except Exception as exc:
            self._close_upstream_events(events)
            self._release_chat_slot_early()
            self._cleanup_failed_upstream_chat(state, context, request.options)
            LOG.exception("[%s] streaming turn failed", current_request_id())
            try:
                # 重试耗尽后的繁忙/验证码失效按 Anthropic 规范标记为 overloaded_error，
                # 客户端 SDK（Claude Code 等）据此识别为可重试的过载而非未知故障。
                error_type = "overloaded_error" if is_retryable_upstream_error(str(exc)) else "api_error"
                self._sse_write(
                    "error",
                    {"type": "error", "error": {"type": error_type, "message": str(exc)}},
                )
            except (BrokenPipeError, ConnectionResetError):
                pass

    @_chat_slot_guard("_web_busy")
    def _handle_web_chat(self) -> None:
        streaming_started = False
        try:
            body = self._read_json_body()
            prompt = self._prompt_from_body(body)
            stream = coerce_bool(body.get("stream", True), True)
            options = chat_options_from_body(body, include_thinking_default=self.include_thinking)
            history = web_history_from_body(body)
            response_model = requested_model_name(body)
            files = chat_files_from_body(body)
            create_chat = not (options.mode in {"continue", "edit", "reuse"} and options.chat_id)
            state = self._active_state()
            assistant_message_id = str(uuid.uuid4())
            context: dict[str, Any] = {"assistant_message_id": assistant_message_id}
            # 面板请求镜像：出站消息 = 面板多轮历史 + 本条用户输入。
            history_ctx = {
                "surface": "panel_chat",
                "stream": stream,
                "user_input": prompt[:HISTORY_PROMPT_CHARS],
                "messages": [*history, {"role": "user", "content": prompt}],
                "context_text": "",
                "account": (state.user_id or "")[:8],
            }

            if not stream:
                answer: list[str] = []
                retried_fallback = False
                while True:
                    attempt_context: dict[str, Any] = {"assistant_message_id": assistant_message_id}
                    attempt_options = options
                    attempt_create_chat = create_chat
                    attempt_chat_id = options.chat_id or None
                    attempt_error = ""
                    for event in stream_zai_completion(
                        state,
                        prompt,
                        create_chat=attempt_create_chat,
                        chat_id=attempt_chat_id,
                        user_msg_id=options.user_msg_id or None,
                        assistant_msg_id=assistant_message_id,
                        captcha_verify_param=self.captcha_verify_param,
                        fresh_captcha_browser=self.fresh_captcha_browser,
                        chrome_path=self.chrome_path,
                        captcha_headless=self.captcha_headless,
                        captcha_timeout_ms=self.captcha_timeout_ms,
                        upstream_timeout_sec=self.upstream_timeout_sec,
                        retry_wait_sec=self.upstream_retry_wait_sec,
                        retry_attempts=self.upstream_retry_max_attempts,
                        options=attempt_options,
                        context_out=attempt_context,
                        files=files,
                        history=history,
                        history_ctx=history_ctx,
                    ):
                        error = extract_error_from_event(event)
                        if error:
                            attempt_error = error
                            break
                        delta, phase = extract_delta_from_event(event)
                        if delta and should_emit_delta(phase, attempt_options.include_thinking):
                            answer.append(strip_parsed_tool_markup(delta))
                    if (
                        attempt_error
                        and not create_chat
                        and not retried_fallback
                        and is_chat_missing_error(attempt_error)
                    ):
                        # The reused upstream chat is gone: degrade to a fresh chat
                        # and embed the local history instead of failing the call.
                        retried_fallback = True
                        options = replace(options, mode="new", chat_id="")
                        create_chat = True
                        continue
                    if attempt_error:
                        context.update(attempt_context)
                        raise RuntimeError(attempt_error)
                    context.update(attempt_context)
                    break
                self._release_chat_slot_early()
                chat_id, chat_deleted, chat_delete_error = self._schedule_upstream_chat_delete(state, context, options)
                self._json_response(
                    200,
                    {
                        "ok": True,
                        "model": response_model,
                        "answer": "".join(answer),
                        "chat_id": chat_id,
                        "current_user_message_id": context.get("current_user_message_id", ""),
                        "assistant_message_id": context.get("assistant_message_id", ""),
                        "options": chat_options_public(options),
                        "files": len(files),
                        "chat_deleted": chat_deleted,
                        "chat_delete_pending": bool(chat_id) and options.delete_chat_after_completion,
                        "chat_delete_error": chat_delete_error,
                    },
                )
                return

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self._send_cors_headers()
            self._send_security_headers()
            self.end_headers()
            self.close_connection = True
            streaming_started = True
            action = "复用当前会话" if not create_chat else "创建新会话"
            self._sse_write(
                "status",
                {
                    "message": f"已连接本地代理，正在{action}/验证码...",
                    "assistant_message_id": assistant_message_id,
                    "model": response_model,
                    "options": chat_options_public(options),
                    "files": len(files),
                },
            )
            context_announced = False
            retried_fallback = False
            while True:
                attempt_context: dict[str, Any] = {"assistant_message_id": assistant_message_id}
                attempt_options = options
                attempt_create_chat = create_chat
                attempt_chat_id = options.chat_id or None
                attempt_error = ""
                for event in stream_zai_completion(
                    state,
                    prompt,
                    create_chat=attempt_create_chat,
                    chat_id=attempt_chat_id,
                    user_msg_id=options.user_msg_id or None,
                    assistant_msg_id=assistant_message_id,
                    captcha_verify_param=self.captcha_verify_param,
                    fresh_captcha_browser=self.fresh_captcha_browser,
                    chrome_path=self.chrome_path,
                    captcha_headless=self.captcha_headless,
                    captcha_timeout_ms=self.captcha_timeout_ms,
                    upstream_timeout_sec=self.upstream_timeout_sec,
                    retry_wait_sec=self.upstream_retry_wait_sec,
                    retry_attempts=self.upstream_retry_max_attempts,
                    options=attempt_options,
                    context_out=attempt_context,
                    files=files,
                    history=history,
                    history_ctx=history_ctx,
                ):
                    if not context_announced:
                        self._sse_write(
                            "context",
                            {
                                "chat_id": attempt_context.get("chat_id", ""),
                                "current_user_message_id": attempt_context.get("current_user_message_id", ""),
                                "assistant_message_id": attempt_context.get("assistant_message_id", ""),
                            },
                        )
                        context_announced = True
                    error = extract_error_from_event(event)
                    if error:
                        attempt_error = error
                        break
                    delta, phase = extract_delta_from_event(event)
                    if delta and should_emit_delta(phase, attempt_options.include_thinking):
                        self._sse_write("delta", {"delta": strip_parsed_tool_markup(delta), "phase": phase})
                if (
                    attempt_error
                    and not create_chat
                    and not retried_fallback
                    and is_chat_missing_error(attempt_error)
                ):
                    # The reused upstream chat is gone: degrade to a fresh chat
                    # and embed the local history instead of failing the call.
                    retried_fallback = True
                    options = replace(options, mode="new", chat_id="")
                    create_chat = True
                    context_announced = False
                    continue
                if attempt_error:
                    context.update(attempt_context)
                    self._release_chat_slot_early()
                    self._cleanup_failed_upstream_chat(state, context, options)
                    self._cleanup_failed_upstream_files(state, files)
                    self._sse_write("error", {"message": attempt_error})
                    self.close_connection = True
                    return
                context.update(attempt_context)
                break
            self._release_chat_slot_early()
            chat_id, chat_deleted, chat_delete_error = self._schedule_upstream_chat_delete(state, context, options)
            self._sse_write(
                "done",
                {
                    "model": response_model,
                    "chat_id": chat_id,
                    "current_user_message_id": context.get("current_user_message_id", ""),
                    "assistant_message_id": context.get("assistant_message_id", ""),
                    "options": chat_options_public(options),
                    "files": len(files),
                    "chat_deleted": chat_deleted,
                    "chat_delete_pending": bool(chat_id) and options.delete_chat_after_completion,
                    "chat_delete_error": chat_delete_error,
                },
            )
            self.close_connection = True
        except Exception as exc:
            if not isinstance(exc, ValueError):
                LOG.exception("[%s] web chat failed", current_request_id())
            if "context" in locals() and "options" in locals() and "state" in locals():
                self._release_chat_slot_early()
                self._cleanup_failed_upstream_chat(state, context, options)
            if "files" in locals() and "state" in locals():
                self._cleanup_failed_upstream_files(state, files)
            if streaming_started and not self.wfile.closed:
                try:
                    self._sse_write("error", {"message": str(exc)})
                    self.close_connection = True
                except Exception:
                    pass
            elif not self.wfile.closed:
                status = 400 if isinstance(exc, ValueError) else 500
                self._json_response(status, {"ok": False, "error": {"message": str(exc)}})

    def _handle_chat_cancel(self) -> None:
        try:
            body = self._read_json_body()
            assistant_message_id = require_uuid(
                body.get("assistant_message_id") or body.get("task_id") or body.get("id"),
                "assistant_message_id",
            )
            result = stop_zai_task(self._active_state(), assistant_message_id)
            self._json_response(
                200,
                {
                    "ok": True,
                    "assistant_message_id": assistant_message_id,
                    "upstream": result,
                },
            )
        except Exception as exc:
            self._json_response(400, {"ok": False, "error": {"message": str(exc)}})

    def _handle_chat_delete(self) -> None:
        try:
            body = self._read_json_body()
            chat_id = require_uuid(body.get("chat_id") or body.get("conversation_id"), "chat_id")
            state = self._active_state()
            delete_zai_chat(state, chat_id)
            with self.state_lock:
                profile = self.profiles.get(self.active_profile_id)
                if profile and profile.state is state and profile.state.chat_id == chat_id:
                    profile.state.chat_id = ""
                    self._save_profile_store()
                elif self.state is state and self.state.chat_id == chat_id:
                    self.state.chat_id = ""
            self._json_response(200, {"ok": True, "chat_id": chat_id, "deleted": True})
        except Exception as exc:
            self._json_response(400, {"ok": False, "error": {"message": str(exc)}})

    def _handle_history_chats(self) -> None:
        """上游账号历史对话列表 + 本地镜像条目合并（本地未在上游出现的补在第 1 页）。"""
        try:
            params = dict(parse_qsl(urlsplit(self.path).query, keep_blank_values=True))
            try:
                page = max(1, int(params.get("page", "1")))
            except ValueError:
                page = 1
            upstream_error = ""
            chats: list[dict[str, Any]] = []
            try:
                state = self._active_state()
                chats = list_zai_chats(state, page=page)
            except Exception as exc:
                upstream_error = str(exc)
            items = [
                {
                    "id": str(item.get("id") or ""),
                    "title": str(item.get("title") or "新聊天"),
                    "updated_at": item.get("updated_at"),
                    "created_at": item.get("created_at"),
                    "type": str(item.get("type") or ""),
                    "source": "upstream",
                }
                for item in chats
                if item.get("id")
            ]
            if page == 1:
                upstream_ids = {item["id"] for item in items}
                latest_local: dict[str, dict[str, Any]] = {}
                for record in local_history_records():
                    chat_id = str(record.get("chat_id") or "")
                    if chat_id and chat_id not in upstream_ids:
                        latest_local[chat_id] = record
                for chat_id, record in latest_local.items():
                    local_ts = int(record.get("created_at") or 0) / 1000
                    items.append(
                        {
                            "id": chat_id,
                            "title": str(record.get("title") or "本地会话"),
                            "updated_at": local_ts,
                            "created_at": local_ts,
                            "type": "local",
                            "source": "local",
                        }
                    )
                items.sort(key=lambda item: item.get("updated_at") or 0, reverse=True)
            if not items and upstream_error:
                raise RuntimeError(upstream_error)
            self._json_response(200, {"ok": True, "page": page, "count": len(items), "chats": items})
        except Exception as exc:
            self._json_response(400, {"ok": False, "error": {"message": str(exc)}})

    def _handle_history_chat_detail(self) -> None:
        """单条历史对话详情：优先本地镜像（含助手完整回复），无镜像再读上游。"""
        try:
            params = dict(parse_qsl(urlsplit(self.path).query, keep_blank_values=True))
            chat_id = require_uuid(params.get("id", ""), "chat_id")
            local_records = [r for r in local_history_records() if str(r.get("chat_id") or "") == chat_id]
            upstream_error = ""
            detail: dict[str, Any] | None = None
            try:
                state = self._active_state()
                detail = get_zai_chat_detail(state, chat_id)
            except Exception as exc:
                upstream_error = str(exc)
            if local_records:
                messages: list[dict[str, Any]] = []
                for record in local_records:
                    ts = int(record.get("created_at") or 0) / 1000
                    messages.append(
                        {
                            "role": "user",
                            "content": str(record.get("user_input") or ""),
                            "files": record.get("files") or [],
                            "timestamp": ts,
                        }
                    )
                    messages.append(
                        {
                            "role": "assistant",
                            "content": str(record.get("content") or ""),
                            "thinking": str(record.get("reasoning") or ""),
                            "timestamp": ts,
                        }
                    )
                if detail is not None:
                    chat_meta = detail.get("chat") if isinstance(detail.get("chat"), dict) else {}
                    models = chat_meta.get("models") if isinstance(chat_meta.get("models"), list) else []
                    meta = {
                        "id": str(detail.get("id") or chat_id),
                        "title": str(detail.get("title") or local_records[0].get("title") or "新聊天"),
                        "models": [str(m) for m in models],
                        "enable_thinking": chat_meta.get("enable_thinking"),
                        "reasoning_effort": chat_meta.get("reasoning_effort"),
                        "created_at": detail.get("created_at"),
                        "updated_at": detail.get("updated_at"),
                    }
                else:
                    meta = {
                        "id": chat_id,
                        "title": str(local_records[0].get("title") or "本地会话"),
                        "models": [str(local_records[0].get("model") or "")] if local_records[0].get("model") else [],
                        "enable_thinking": None,
                        "reasoning_effort": None,
                        "created_at": int(local_records[0].get("created_at") or 0) / 1000,
                        "updated_at": int(local_records[-1].get("created_at") or 0) / 1000,
                    }
                self._json_response(200, {"ok": True, "source": "local", "chat": meta, "messages": messages})
                return
            if detail is None:
                raise RuntimeError(upstream_error or "对话不存在")
            chat = detail.get("chat") or {}
            models = chat.get("models") if isinstance(chat.get("models"), list) else []
            self._json_response(
                200,
                {
                    "ok": True,
                    "source": "upstream",
                    "chat": {
                        "id": str(detail.get("id") or chat_id),
                        "title": str(detail.get("title") or "新聊天"),
                        "models": [str(m) for m in models],
                        "enable_thinking": chat.get("enable_thinking"),
                        "reasoning_effort": chat.get("reasoning_effort"),
                        "created_at": detail.get("created_at"),
                        "updated_at": detail.get("updated_at"),
                    },
                    "messages": extract_chat_history_messages(detail),
                },
            )
        except Exception as exc:
            self._json_response(400, {"ok": False, "error": {"message": str(exc)}})

    def _handle_history_delete(self) -> None:
        """删除上游历史对话（复用 delete_zai_chat；404 已视为成功），并清掉本地镜像。"""
        try:
            body = self._read_json_body()
            chat_id = require_uuid(body.get("chat_id"), "chat_id")
            state = self._active_state()
            delete_zai_chat(state, chat_id)
            local_removed = purge_local_history(chat_id)
            with self.state_lock:
                profile = self.profiles.get(self.active_profile_id)
                if profile and profile.state is state and profile.state.chat_id == chat_id:
                    profile.state.chat_id = ""
                    self._save_profile_store()
                elif self.state is state and self.state.chat_id == chat_id:
                    self.state.chat_id = ""
            self._json_response(200, {"ok": True, "chat_id": chat_id, "deleted": True, "local_removed": local_removed})
        except Exception as exc:
            self._json_response(400, {"ok": False, "error": {"message": str(exc)}})

    def _handle_history_records(self) -> None:
        """请求镜像列表（ds2api 同款：每条请求一条记录，分页、新→旧，支持 text/status 筛选）。"""
        try:
            params = dict(parse_qsl(urlsplit(self.path).query, keep_blank_values=True))
            try:
                page = max(1, int(params.get("page", "1")))
            except ValueError:
                page = 1
            text = str(params.get("text") or "")
            status = str(params.get("status") or "").strip().lower()
            if status and status not in {"streaming", "success", "stopped", "error"}:
                status = ""
            page_size = 50
            start = (page - 1) * page_size
            items, total = local_history_summary_page(
                text=text,
                status=status,
                page=page,
                page_size=page_size,
            )
            self._json_response(
                200,
                {
                    "ok": True,
                    "page": page,
                    "count": len(items),
                    "total": total,
                    "exhausted": start + page_size >= total,
                    "records": items,
                },
            )
        except Exception as exc:
            self._json_response(400, {"ok": False, "error": {"message": str(exc)}})

    def _handle_history_record_detail(self) -> None:
        """单条请求镜像完整记录：出站消息、文件清单、上下文、最终 prompt、回复。"""
        try:
            params = dict(parse_qsl(urlsplit(self.path).query, keep_blank_values=True))
            record_id = str(params.get("id") or "").strip()
            if not re.fullmatch(r"req_[0-9a-f]{8,32}", record_id):
                raise ValueError("record id is invalid")
            record = get_local_history_record(record_id)
            if record is None:
                raise RuntimeError("记录不存在（可能已被清理或超出保留上限）")
            self._json_response(200, {"ok": True, "record": record})
        except Exception as exc:
            self._json_response(400, {"ok": False, "error": {"message": str(exc)}})

    def _handle_history_record_delete(self) -> None:
        try:
            body = self._read_json_body()
            record_id = str(body.get("id") or "").strip()
            if not re.fullmatch(r"req_[0-9a-f]{8,32}", record_id):
                raise ValueError("record id is invalid")
            removed = purge_history_record(record_id)
            if not removed:
                raise RuntimeError("记录不存在")
            self._json_response(200, {"ok": True, "id": record_id, "removed": removed})
        except Exception as exc:
            self._json_response(400, {"ok": False, "error": {"message": str(exc)}})

    def _handle_history_clear(self) -> None:
        try:
            removed = clear_local_history()
            self._json_response(200, {"ok": True, "removed": removed})
        except Exception as exc:
            self._json_response(400, {"ok": False, "error": {"message": str(exc)}})

    def _handle_file_cleanup(self) -> None:
        """Delete uploaded files that never reached a chat (abort/failure paths)."""
        try:
            body = self._read_json_body()
            raw = body.get("files")
            if not isinstance(raw, list):
                raise ValueError("files must be a list")
            if len(raw) > 64:
                raise ValueError("too many files in one cleanup request")
            state = self._active_state()
            removed: list[str] = []
            skipped: list[str] = []
            for item in raw:
                if isinstance(item, dict):
                    item = item.get("id") or item.get("file_id")
                if not isinstance(item, str) or not item.strip():
                    continue
                try:
                    if delete_zai_file(state, item):
                        removed.append(item)
                    else:
                        skipped.append(item)
                except Exception:
                    skipped.append(item)
            self._json_response(200, {"ok": True, "removed": removed, "skipped": skipped, "count": len(removed)})
        except Exception as exc:
            self._json_response(400, {"ok": False, "error": {"message": str(exc)}})

    def _handle_file_upload(self) -> None:
        tmp_path: Path | None = None
        try:
            state = self._active_state()
            query = dict(parse_qsl(urlsplit(self.path).query, keep_blank_values=True))
            filename = safe_filename(
                str(
                    query.get("filename")
                    or query.get("name")
                    or self.headers.get("X-Filename")
                    or "upload.bin"
                )
            )
            content_type = guess_content_type(filename, str(self.headers.get("Content-Type") or ""))
            tmp_path = self._spool_raw_body(
                max_bytes=MAX_CHAT_FILE_UPLOAD_BYTES,
                prefix="glm2api-file-",
                suffix=".upload",
            )
            uploaded = upload_file_path_to_zai(state, tmp_path, filename, content_type)
            meta = uploaded.get("meta") if isinstance(uploaded.get("meta"), dict) else {}
            self._json_response(
                200,
                {
                    "ok": True,
                    "file": uploaded,
                    "summary": {
                        "id": uploaded.get("id"),
                        "name": uploaded.get("filename") or meta.get("name") or filename,
                        "content_type": meta.get("content_type") or content_type,
                        "size": meta.get("size") or 0,
                    },
                },
            )
        except Exception as exc:
            self._json_response(400, {"ok": False, "error": {"message": str(exc)}})
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)

    def _handle_settings_save(self) -> None:
        try:
            body = self._read_json_body()
            source = body.get("settings") if isinstance(body.get("settings"), dict) else body
            self._save_settings(source)
            self._json_response(
                200,
                {
                    **self._settings_payload(),
                    "message": "默认设置已保存，下次启动自动生效",
                },
            )
        except Exception as exc:
            self._json_response(400, {"ok": False, "error": {"message": str(exc)}})

    def _handle_auth_har_upload(self) -> None:
        tmp_path: Path | None = None
        try:
            content_type = str(self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            har_fp = ""
            if content_type == "application/json":
                body = self._read_json_body(max_bytes=MAX_HAR_UPLOAD_BYTES)
                label = safe_profile_label(str(body.get("label") or body.get("name") or "uploaded HAR"))
                source = safe_profile_label(str(body.get("source") or "web upload"))
                har_text = str(body.get("har_text") or body.get("har") or "")
                if har_text.strip():
                    raw = har_text.encode("utf-8")
                    state, har_fp = extract_state_from_uploaded_bytes(raw)
                    raw = b""
                    har_text = ""
                    body = {}
                    gc.collect()
                elif isinstance(body.get("har_json"), dict):
                    har_obj = body["har_json"]
                    if not isinstance(har_obj, dict) or "log" not in har_obj:
                        raise ValueError("invalid HAR JSON")
                    state = extract_state(har_obj)
                    har_obj = None
                    body = {}
                    gc.collect()
                else:
                    raise ValueError("missing har_text")
            else:
                query = dict(parse_qsl(urlsplit(self.path).query, keep_blank_values=True))
                label = safe_profile_label(str(query.get("label") or "uploaded HAR"))
                source = safe_profile_label(str(query.get("source") or "web upload"))
                tmp_path = self._spool_raw_body(
                    max_bytes=MAX_HAR_UPLOAD_BYTES,
                    prefix="glm2api-upload-",
                    suffix=".har",
                )
                state, har_fp = extract_state_via_worker(tmp_path)

            with self.state_lock:
                profile = make_profile(state, label=label, source=source, har_fp=har_fp)
                merge_profile(self.profiles, profile)
                self.__class__.active_profile_id = profile.id
                self.__class__.state = state
                self._save_profile_store()
            self._json_response(
                200,
                {
                    **self._profiles_payload(),
                    "message": "HAR 登录态已加载、保存并切换为当前账号",
                    "profile": profile_summary(profile, active=True),
                },
            )
        except Exception as exc:
            self._json_response(400, {"ok": False, "error": {"message": str(exc)}})
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)

    def _handle_auth_token(self) -> None:
        try:
            body = self._read_json_body()
            state = state_from_token(str(body.get("token") or ""))
            label = safe_profile_label(
                str(body.get("label") or "") or f"token: {state.user_name}"
            )
            with self.state_lock:
                profile = make_profile(state, label=label, source="token paste")
                merge_profile(self.profiles, profile)
                self.__class__.active_profile_id = profile.id
                self.__class__.state = state
                self._save_profile_store()
            self._json_response(
                200,
                {
                    **self._profiles_payload(),
                    "message": "token 登录态已加载、保存并切换为当前账号",
                    "profile": profile_summary(profile, active=True),
                },
            )
        except Exception as exc:
            self._json_response(400, {"ok": False, "error": {"message": str(exc)}})

    def _handle_auth_switch(self) -> None:
        try:
            body = self._read_json_body()
            profile_id = str(body.get("profile_id") or "")
            with self.state_lock:
                profile = self.profiles.get(profile_id)
                if not profile:
                    raise ValueError("unknown profile_id")
                self.__class__.active_profile_id = profile_id
                self.__class__.state = profile.state
                self._save_profile_store()
            self._json_response(
                200,
                {
                    **self._profiles_payload(),
                    "message": "已切换当前账号并保存默认选择",
                    "profile": profile_summary(profile, active=True),
                },
            )
        except Exception as exc:
            self._json_response(400, {"ok": False, "error": {"message": str(exc)}})

    def _handle_auth_remove(self) -> None:
        try:
            body = self._read_json_body()
            profile_id = str(body.get("profile_id") or "")
            with self.state_lock:
                profile = self.profiles.pop(profile_id, None)
                if profile is None:
                    raise ValueError("unknown profile_id")
                if self.active_profile_id == profile_id:
                    self.__class__.active_profile_id = next(iter(self.profiles.keys()), "")
                    next_profile = self.profiles.get(self.active_profile_id)
                    self.__class__.state = next_profile.state if next_profile else None
                self._save_profile_store()
            self._json_response(
                200,
                {
                    **self._profiles_payload(),
                    "message": "登录态已从本机加密保存区删除",
                    "removed_profile_id": profile_id,
                },
            )
        except Exception as exc:
            self._json_response(400, {"ok": False, "error": {"message": str(exc)}})

    def _handle_auth_compact(self) -> None:
        try:
            _body = self._read_json_body()
            with self.state_lock:
                before = len(self.profiles)
                active_profile_id, removed = compact_duplicate_profiles(self.profiles, self.active_profile_id)
                self.__class__.active_profile_id = active_profile_id
                active_profile = self.profiles.get(active_profile_id)
                self.__class__.state = active_profile.state if active_profile else None
                if removed:
                    self._save_profile_store()
                removed_summaries = [
                    {
                        "id": profile.id,
                        "label": profile.label,
                        "source_display": profile_source_display(profile.source)[0],
                        "user_name": profile.state.user_name,
                        "user_id_fp": sha16(profile.state.user_id) if profile.state.user_id else "",
                        "token_fp": sha16(profile.state.token),
                    }
                    for profile in removed
                ]
            self._json_response(
                200,
                {
                    **self._profiles_payload(),
                    "message": f"已清理 {len(removed)} 个同账号重复登录态",
                    "before_count": before,
                    "after_count": before - len(removed),
                    "removed_profiles": removed_summaries,
                },
            )
        except Exception as exc:
            self._json_response(400, {"ok": False, "error": {"message": str(exc)}})

    def _handle_auth_browser_login(self) -> None:
        progress = self.__class__.browser_login_progress
        try:
            with self.state_lock:
                if progress.get("running"):
                    self._json_response(
                        409,
                        {"ok": False, "error": {"message": "已有登录或验证码采集流程正在进行，请先完成。"}},
                    )
                    return
            body = self._read_json_body()
            label = safe_profile_label(str(body.get("label") or "browser login"))
            timeout_sec = int(body.get("timeout_sec") or self.browser_login_timeout_ms // 1000)
            timeout_ms = max(30, min(timeout_sec, 900)) * 1000
            now = datetime.now().astimezone().isoformat(timespec="seconds")

            def report(stage: str) -> None:
                progress["stage"] = stage
                progress["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")

            progress.update({"running": True, "mode": "login", "stage": "正在启动授权浏览器…", "updated_at": now, "error": ""})
            try:
                state = get_browser_login_state(
                    chrome_path=self.chrome_path,
                    timeout_ms=timeout_ms,
                    progress_cb=report,
                )
            except Exception as exc:
                progress.update({"stage": "登录失败", "error": str(exc)})
                raise
            if label == "browser login":
                label = safe_profile_label(f"browser: {state.user_name}")
            profile = make_profile(
                state,
                label=label,
                source="browser login",
                har_text=f"browser-login:{state.user_id}:{sha16(state.token)}",
            )
            with self.state_lock:
                merge_profile(self.profiles, profile)
                self.__class__.active_profile_id = profile.id
                self.__class__.state = state
                self._save_profile_store()
            progress.update({"stage": "已保存并切换账号", "error": ""})
            self._json_response(
                200,
                {
                    **self._profiles_payload(),
                    "message": "浏览器登录态已采集、保存并切换为当前账号；验证码将在发送消息时自动获取，如失败可点击“手动采集验证码”在浏览器中完成。",
                    "profile": profile_summary(profile, active=True),
                    "captcha_ok": False,
                },
            )
        except Exception as exc:
            self._json_response(400, {"ok": False, "error": {"message": str(exc)}})
        finally:
            progress["running"] = False
            progress["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")

    def _handle_auth_captcha_refresh(self) -> None:
        progress = self.__class__.browser_login_progress
        try:
            with self.state_lock:
                if progress.get("running"):
                    self._json_response(
                        409,
                        {"ok": False, "error": {"message": "已有登录或验证码采集流程正在进行，请先完成。"}},
                    )
                    return
            body = self._read_json_body()
            profile_id = str(body.get("profile_id") or self.active_profile_id)
            with self.state_lock:
                profile = self.profiles.get(profile_id)
                if not profile:
                    self._json_response(404, {"ok": False, "error": {"message": "登录态不存在，请先登录或上传 HAR。"}})
                    return
                state = profile.state
            timeout_sec = int(body.get("timeout_sec") or 120)
            timeout_ms = max(30, min(timeout_sec, 300)) * 1000
            now = datetime.now().astimezone().isoformat(timespec="seconds")
            progress.update({"running": True, "mode": "captcha", "stage": "正在打开验证码采集窗口…", "updated_at": now, "error": ""})
            try:
                captcha = get_browser_captcha(
                    state,
                    chrome_path=self.chrome_path,
                    headless=False,
                    timeout_ms=timeout_ms,
                )
            except Exception as exc:
                progress.update({"stage": "验证码采集失败", "error": str(exc)})
                raise
            if not captcha:
                progress.update({"stage": "验证码采集失败", "error": "验证码采集为空"})
                raise RuntimeError("验证码采集为空，请重试。")
            with self.state_lock:
                profile.state.captcha_verify_param = captcha
                if profile_id == self.active_profile_id:
                    self.__class__.state = profile.state
                self._save_profile_store()
            progress.update({"stage": "验证码已保存", "error": ""})
            self._json_response(
                200,
                {
                    **self._profiles_payload(),
                    "message": "验证码已采集并保存到当前账号",
                    "profile": profile_summary(profile, active=profile_id == self.active_profile_id),
                    "captcha_ok": True,
                },
            )
        except Exception as exc:
            self._json_response(400, {"ok": False, "error": {"message": str(exc)}})
        finally:
            progress["running"] = False
            progress["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")

    quiet: bool = False  # --quiet 时抑制逐请求访问日志

    def log_request(self, code: int | str, size: int | None = None) -> None:
        """Capture the response code; the dispatch guard logs full handler duration."""
        try:
            self._response_code = int(code)
        except (TypeError, ValueError):
            self._response_code = 500

    def log_message(self, fmt: str, *args: Any) -> None:
        LOG.info("[proxy] " + fmt % args)

    def _handle_api_logs(self) -> None:
        """Query the structured in-memory ring while keeping legacy line output."""
        params = dict(parse_qsl(urlsplit(self.path).query, keep_blank_values=True))
        try:
            limit = max(1, min(int(params.get("lines", "300")), 2000))
        except ValueError:
            limit = 300
        level = str(params.get("level", "")).upper()
        min_level = getattr(logging, level, logging.DEBUG) if level in {"DEBUG", "INFO", "WARNING", "ERROR"} else logging.DEBUG
        contains = str(params.get("text", ""))[:200]
        state = str(params.get("state", ""))[:120]
        kind = str(params.get("kind", "")).lower()
        if kind not in {"event", "access", "system", "error"}:
            kind = ""
        rid = re.sub(r"[^0-9a-f]", "", str(params.get("rid", "")).lower())[:32]
        try:
            after_seq = max(0, int(params.get("after_seq", "0")))
        except ValueError:
            after_seq = 0
        structured_only = str(params.get("format", "")).lower() == "structured"
        ring = log_ring()
        entries, matched, cursor = ring.query(
            limit=limit,
            min_level=min_level,
            contains=contains,
            state=state,
            kind=kind,
            rid=rid,
            after_seq=after_seq,
        )
        try:
            file_bytes = LOG_FILE_PATH.stat().st_size
        except OSError:
            file_bytes = 0
        payload = {
            "ok": True,
            "entries": [
                {
                    key: item[key]
                    for key in ("seq", "timestamp_ms", "level", "thread", "kind", "state", "rid", "message", "line")
                }
                for item in entries
            ],
            "cursor": cursor,
            "stats": ring.stats(matched),
            "file": log_file_label(),
            "file_bytes": file_bytes,
            "ring_count": len(ring),
            "ring_capacity": ring.capacity,
            "level": logging.getLevelName(LOG.getEffectiveLevel()),
        }
        if not structured_only:
            payload["lines"] = [str(item["line"]) for item in entries]
        self._json_response(200, payload)

    def _handle_api_metrics(self) -> None:
        """Return aggregate operational and retained-history telemetry."""
        params = dict(parse_qsl(urlsplit(self.path).query, keep_blank_values=True))
        try:
            hours = max(1, min(int(params.get("hours", "24")), 24 * 30))
        except ValueError:
            hours = 24
        runtime = RUNTIME_METRICS.snapshot()
        with self.chat_inflight_lock:
            runtime["inflight"] = sum(max(0, int(value)) for value in self.chat_inflight.values())
            runtime["active_profile_inflight"] = max(0, int(self.chat_inflight.get(self.active_profile_id, 0)))
        self._json_response(
            200,
            {
                "ok": True,
                "generated_at": int(time.time() * 1000),
                "window_hours": hours,
                "runtime": runtime,
                "history": local_history_metrics(hours),
                "logs": log_ring().stats(),
            },
        )


class LocalProxyServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    thread_name_prefix = "proxy"


def validate_signature_from_har(har: dict[str, Any]) -> bool:
    ok = True
    checked = 0
    for entry in har.get("log", {}).get("entries", []):
        if entry_url_path(entry) != "/api/v2/chat/completions":
            continue
        body = request_json(entry)
        if not isinstance(body, dict):
            continue
        prompt = body.get("signature_prompt")
        req = entry.get("request", {})
        headers = req.get("headers", [])
        target = header_value(headers, "x-signature")
        q = dict(parse_qsl(urlsplit(req.get("url", "")).query, keep_blank_values=True))
        if not prompt or not target:
            continue
        calc = z_sign(str(prompt), q["signature_timestamp"], q["requestId"], q["user_id"])
        checked += 1
        ok = ok and calc == target
    print(json.dumps({"signature_samples_checked": checked, "all_match": ok}, ensure_ascii=False))
    return ok


def write_evidence(har: dict[str, Any], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    entries = har.get("log", {}).get("entries", [])

    inventory = []
    token_rows = []
    for idx, entry in enumerate(entries):
        req = entry.get("request", {})
        resp = entry.get("response", {})
        sp = urlsplit(req.get("url", ""))
        if sp.netloc != "chat.z.ai":
            continue
        post = request_json(entry)
        response = response_json(entry)
        row = {
            "idx": idx,
            "method": req.get("method"),
            "status": resp.get("status"),
            "path": sp.path,
            "query_keys": [k for k, _v in parse_qsl(sp.query, keep_blank_values=True)],
            "post_keys": sorted(post.keys()) if isinstance(post, dict) else [],
            "mime": resp.get("content", {}).get("mimeType"),
        }
        if isinstance(post, dict):
            row["model"] = post.get("model") or (post.get("chat") or {}).get("models")
        inventory.append(row)
        if isinstance(response, dict) and response.get("token"):
            token = str(response["token"])
            token_rows.append(
                {
                    "idx": idx,
                    "path": sp.path,
                    "token_fp": sha16(token),
                    "token_len": len(token),
                    "role": response.get("role"),
                    "user_id": response.get("id"),
                    "token_type": response.get("token_type"),
                }
            )

    (out_dir / "api_inventory.json").write_text(
        json.dumps(inventory, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    with (out_dir / "token_inventory.redacted.jsonl").open("w", encoding="utf-8", newline="\n") as f:
        for row in token_rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    with (out_dir / "proxy_boundary_matrix.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["field", "source", "observed", "role_in_request"])
        writer.writerow(["model", "request body", "glm-5.3 / x-preview-l / GLM-5-Turbo / glm-5.2", "select upstream model"])
        writer.writerow(["Authorization", "signin token", "Bearer <redacted>", "session auth"])
        writer.writerow(["token", "query string", "<same token, redacted>", "browser telemetry/signature context"])
        writer.writerow(["X-Signature", "front-end HMAC", "sha256 hmac", "request integrity check"])
        writer.writerow(["captcha_verify_param", "AliyunCaptcha success callback", "<redacted>", "anti-abuse gate"])
        writer.writerow(["chat_id", "POST /api/v1/chats/new", "uuid", "conversation binding"])
        writer.writerow(["current_user_message_id", "generated UUID", "uuid", "message binding"])
        writer.writerow(["assistant_message_id", "completion request top-level id", "uuid", "task cancellation binding"])
        writer.writerow(["DELETE /api/v1/chats/{chat_id}", "browser delete action", "true", "delete upstream conversation"])
        writer.writerow(["POST /api/tasks/stop/{assistant_message_id}", "browser cancel action", "{status: true}", "stop upstream generation"])

    (out_dir / "realtime_state_machine.md").write_text(
        "# Realtime State Machine\n\n"
        "```text\n"
        "POST /api/v1/auths/signin -> token\n"
        "POST /api/v1/chats/new -> chat_id + user_message_id\n"
        "AliyunCaptcha success -> captcha_verify_param\n"
        "POST /api/v2/chat/completions?browser_params&signature_timestamp -> text/event-stream\n"
        "SSE data: {type: chat:completion, data: {delta_content, phase}}\n"
        "POST /api/tasks/stop/{completion.id} -> stop active generation\n"
        "DELETE /api/v1/chats/{chat_id} -> delete completed conversation\n"
        "```\n",
        encoding="utf-8",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--har", default="chat.z.ai.har", help="HAR path, default: chat.z.ai.har")
    parser.add_argument("--extract-state-json", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--prompt", help="Send one prompt directly to the selected upstream model")
    parser.add_argument("--serve", action="store_true", help="Serve local OpenAI-compatible proxy")
    parser.add_argument("--open-web", action="store_true", help="Open the local web UI after starting --serve")
    parser.add_argument("--quiet", action="store_true", help="Suppress per-request access logs (errors still print)")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        help="Runtime log level; logs go to logs/glm2api.log (rotated), stderr and the panel log viewer",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8008)
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="Explicitly allow a non-loopback --host; an API key is also required",
    )
    parser.add_argument(
        "--api-key",
        default="",
        help="Optional local API key. Also read from GLM2API_API_KEY. Protects all API routes except public status/health.",
    )
    parser.add_argument(
        "--cors-origin",
        action="append",
        default=[],
        metavar="ORIGIN",
        help="Allow one browser Origin for cross-origin API calls; repeat for multiple origins",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Model route, supported: {', '.join(ADVERTISED_MODELS)}")
    parser.add_argument("--web-search", action="store_true", help="Enable Z.ai auto web search for --prompt")
    parser.add_argument("--no-thinking", action="store_true", help="Disable deep thinking for --prompt")
    parser.add_argument(
        "--reasoning-effort",
        default=DEFAULT_REASONING_EFFORT,
        choices=SUPPORTED_REASONING_EFFORTS,
        help="Deep thinking level for glm-5.3: high or max",
    )
    parser.add_argument("--reuse-har-chat", action="store_true", help="Reuse chat_id from HAR instead of creating a fresh chat")
    parser.add_argument("--captcha", help="Use a fresh captcha_verify_param value instead of the HAR/env value")
    parser.add_argument(
        "--fresh-captcha-browser",
        action="store_true",
        help="Use Chrome + official AliyunCaptcha SDK to obtain a fresh didk33e0 captcha before each completion",
    )
    parser.add_argument("--chrome", help="Chrome/Edge executable path for --fresh-captcha-browser")
    parser.add_argument("--headed", action="store_true", help="Show browser window for --fresh-captcha-browser")
    parser.add_argument("--captcha-timeout-ms", type=int, default=75_000)
    parser.add_argument(
        "--captcha-mode",
        choices=("auto", "happydom", "browser"),
        default="auto",
        help="Fresh captcha solver: auto prefers the fast happy-dom Node solver and falls back to the Playwright browser worker",
    )
    parser.add_argument(
        "--upstream-timeout-sec",
        type=int,
        default=None,
        help="Stream idle timeout in seconds for upstream completions (default 300; panel setting applies unless set)",
    )
    parser.add_argument(
        "--captcha-worker-idle-sec",
        type=float,
        default=900.0,
        help="Keep the reused captcha browser alive this many idle seconds before closing it (0 = keep until exit)",
    )
    parser.add_argument("--browser-login-timeout-sec", type=int, default=300, help="Manual browser login timeout for web UI auth manager")
    parser.add_argument("--include-thinking", action="store_true", help="Forward upstream phase=thinking deltas")
    parser.add_argument("--self-test", action="store_true", help="Validate recovered signature algorithm against HAR samples")
    parser.add_argument("--write-evidence", action="store_true", help="Write redacted evidence artifacts")
    parser.add_argument(
        "--evidence-dir",
        default="exports/ctf-website/2026-08-13-chat-z-ai-model-proxy",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    setup_logging(args.log_level)
    LOG.info("glm2api starting: serve=%s port=%s log_level=%s", args.serve, args.port, args.log_level)
    har_path = Path(args.har)
    if args.extract_state_json:
        if not har_path.exists():
            raise FileNotFoundError(f"HAR 不存在: {har_path}")
        state, har_fp = extract_state_from_har_path(har_path)
        LOG.warning("the following state JSON contains a live token; keep it private and delete it after use")
        print(json.dumps({"state": asdict(state), "har_fp": har_fp}, ensure_ascii=False))
        return 0

    har_fp = ""
    har: dict[str, Any] | None = None
    har_state: HarState | None = None
    stored_profiles: dict[str, AccountProfile] = {}
    stored_active_profile_id = ""
    stored_saved_at = ""
    store_error = ""
    local_settings = local_settings_defaults()
    settings_saved_at = ""
    settings_error = ""
    stored_api_key = ""
    api_key_saved_at = ""
    api_key_store_error = ""

    if args.serve:
        try:
            stored_profiles, stored_active_profile_id, stored_saved_at = load_profile_store(PROFILE_STORE_PATH)
        except Exception as exc:
            store_error = str(exc)
        try:
            local_settings, settings_saved_at, settings_error = load_local_settings(SETTINGS_STORE_PATH)
        except Exception as exc:
            settings_error = str(exc)
        try:
            stored_api_key, api_key_saved_at, api_key_store_error = load_api_key_store(API_KEY_STORE_PATH)
        except Exception as exc:
            api_key_store_error = str(exc)

    har_exists = har_path.exists()
    allow_no_har = args.serve and not args.prompt and not args.self_test and not args.write_evidence
    if not har_exists and not allow_no_har:
        raise FileNotFoundError(f"HAR 不存在: {har_path}")

    if har_exists and args.serve:
        har_fp = file_sha16(har_path)

    same_har_profile_exists = bool(har_fp) and any(profile.har_fp == har_fp for profile in stored_profiles.values())
    can_skip_har_parse = (
        args.serve
        and not args.prompt
        and not args.self_test
        and not args.write_evidence
        and same_har_profile_exists
    )

    if har_exists and not can_skip_har_parse:
        if args.serve and not args.self_test and not args.write_evidence:
            har_state, har_fp = extract_state_via_worker(har_path)
        else:
            har = load_har(har_path)
            if not har_fp:
                har_fp = file_sha16(har_path)

    if args.self_test:
        if har is None:
            raise RuntimeError("--self-test 需要 HAR")
        if not validate_signature_from_har(har):
            return 1
    if args.write_evidence:
        if har is None:
            raise RuntimeError("--write-evidence 需要 HAR")
        write_evidence(har, Path(args.evidence_dir))
        print(f"wrote evidence to {args.evidence_dir}")

    if not args.prompt and not args.serve:
        return 0

    if har_state is None and har is not None:
        har_state = extract_state(har)
    if har_state and args.captcha:
        har_state.captcha_verify_param = args.captcha

    active_state = har_state
    if stored_active_profile_id in stored_profiles:
        active_state = stored_profiles[stored_active_profile_id].state
    cli_options = ChatOptions(
        model=normalize_model(args.model),
        auto_web_search=bool(args.web_search),
        enable_thinking=not bool(args.no_thinking),
        reasoning_effort=normalize_reasoning_effort(args.reasoning_effort),
        include_thinking=bool(args.include_thinking),
    )
    api_key = stored_api_key
    api_key_source = "store"
    env_api_key = str(os.environ.get(API_KEY_ENV_NAME) or "").strip()
    if env_api_key:
        api_key = env_api_key
        api_key_source = "cli"
    if str(args.api_key or "").strip():
        api_key = str(args.api_key).strip()
        api_key_source = "cli"
    if args.serve:
        validate_server_bind(args.host, args.allow_remote, api_key)

    log_event(
        "loaded",
        auth_ready=active_state is not None,
        user_id_fp=sha16(active_state.user_id) if active_state and active_state.user_id else "",
        token_fp=sha16(active_state.token) if active_state else "",
        device_id_fp=sha16(active_state.device_id) if active_state and active_state.device_id else "",
        captcha_fp=sha16(active_state.captcha_verify_param) if active_state and active_state.captcha_verify_param else "",
        captcha_mode="browser_fresh" if args.fresh_captcha_browser else "provided_or_har",
        saved_profile_count=len(stored_profiles),
        profile_store_error=store_error,
        default_model=DEFAULT_MODEL,
        selected_model=cli_options.model,
        supported_models=list(ADVERTISED_MODELS),
        api_key_required=bool(api_key),
        api_key_source=api_key_source,
    )

    if args.prompt:
        state = active_state
        if state is None:
            raise RuntimeError("--prompt 需要可用 HAR 登录态")
        direct_prompt(
            state,
            args.prompt,
            create_chat=not args.reuse_har_chat,
            captcha_verify_param=args.captcha,
            fresh_captcha_browser=args.fresh_captcha_browser,
            chrome_path=args.chrome,
            captcha_headless=not args.headed,
            captcha_timeout_ms=args.captcha_timeout_ms,
            upstream_timeout_sec=args.upstream_timeout_sec or UPSTREAM_STREAM_TIMEOUT_SEC,
            include_thinking=args.include_thinking,
            options=cli_options,
        )
    if args.serve:
        profiles = dict(stored_profiles)
        active_profile_id = stored_active_profile_id if stored_active_profile_id in profiles else ""
        preload_profile_changed = False

        if har_state is not None:
            default_profile = make_profile(
                har_state,
                label=f"default: {har_path.name}",
                source=f"preloaded HAR: {har_path.name}",
                har_fp=har_fp,
            )
            default_profile_id = merge_profile(profiles, default_profile)
            preload_profile_changed = True
            if not active_profile_id:
                active_profile_id = default_profile_id

        if active_profile_id in profiles:
            active_state = profiles[active_profile_id].state
        elif har_state is not None:
            active_state = har_state
        else:
            active_state = None

        if preload_profile_changed:
            try:
                save_profile_store(profiles, active_profile_id, PROFILE_STORE_PATH)
                stored_saved_at = datetime.now().astimezone().isoformat(timespec="seconds")
                store_error = ""
            except Exception as exc:
                store_error = str(exc)

        ProxyHandler.state = active_state
        ProxyHandler.profiles = profiles
        ProxyHandler.active_profile_id = active_profile_id
        ProxyHandler.profile_store_path = PROFILE_STORE_PATH
        ProxyHandler.profile_store_saved_at = stored_saved_at
        ProxyHandler.profile_store_error = store_error
        ProxyHandler.captcha_verify_param = args.captcha
        ProxyHandler.fresh_captcha_browser = args.fresh_captcha_browser
        ProxyHandler.chrome_path = args.chrome
        ProxyHandler.captcha_headless = not args.headed
        ProxyHandler.captcha_timeout_ms = args.captcha_timeout_ms
        global _CAPTCHA_MODE
        _CAPTCHA_MODE = str(getattr(args, "captcha_mode", "") or "auto").strip().lower() or "auto"
        ProxyHandler.upstream_timeout_sec = int(
            args.upstream_timeout_sec
            or local_settings.get("upstream_timeout_sec")
            or UPSTREAM_STREAM_TIMEOUT_SEC
        )
        ProxyHandler.upstream_timeout_locked = bool(args.upstream_timeout_sec)
        ProxyHandler.upstream_retry_wait_sec = float(
            local_settings.get("upstream_retry_wait_sec", DEFAULT_UPSTREAM_RETRY_WAIT_SEC)
        )
        ProxyHandler.upstream_retry_max_attempts = int(
            local_settings.get("upstream_retry_max_attempts", DEFAULT_UPSTREAM_RETRY_ATTEMPTS)
        )
        _HISTORY_CONF["max_records"] = max(50, min(2000, int(local_settings.get("history_max_records") or 300)))
        ProxyHandler.browser_login_timeout_ms = max(30, args.browser_login_timeout_sec) * 1000
        ProxyHandler.settings = local_settings
        ProxyHandler.settings_path = SETTINGS_STORE_PATH
        ProxyHandler.settings_saved_at = settings_saved_at
        ProxyHandler.settings_error = settings_error
        ProxyHandler.api_key = api_key
        ProxyHandler.api_key_store_path = API_KEY_STORE_PATH
        ProxyHandler.api_key_saved_at = api_key_saved_at
        ProxyHandler.api_key_store_error = api_key_store_error
        ProxyHandler.api_key_source = api_key_source
        if not args.include_thinking:
            ProxyHandler.include_thinking = bool(local_settings.get("include_thinking", False))
        else:
            ProxyHandler.include_thinking = True
        ProxyHandler.quiet = args.quiet
        ProxyHandler.cors_origins = tuple(
            dict.fromkeys(origin.strip().rstrip("/") for origin in args.cors_origin if origin.strip())
        )
        if har is not None:
            har.clear()
        har = None
        har_state = None
        gc.collect()
        server = LocalProxyServer((args.host, args.port), ProxyHandler)
        web_url = f"http://{args.host}:{args.port}/"
        LOG.info("Web UI listening on %s", web_url)
        LOG.info("OpenAI-compatible proxy listening on http://%s:%s", args.host, args.port)
        LOG.info("Default model: %s; advertised: %s", DEFAULT_MODEL, ", ".join(ADVERTISED_MODELS))
        if api_key:
            LOG.info("API key protection enabled; requests must include X-API-Key or Authorization: Bearer <key>")
        if ProxyHandler.cors_origins:
            LOG.info("Allowed CORS origins: %s", ", ".join(ProxyHandler.cors_origins))
        if args.fresh_captcha_browser:
            global _CAPTCHA_WORKER
            _CAPTCHA_WORKER = CaptchaWorker(
                chrome_path=args.chrome,
                headless=not args.headed,
                default_timeout_ms=args.captcha_timeout_ms,
                idle_timeout_sec=args.captcha_worker_idle_sec,
            )
            LOG.info(
                "Captcha mode: browser_fresh (reused headless Chrome + official AliyunCaptcha SDK; "
                "idle shutdown after %gs)",
                args.captcha_worker_idle_sec,
            )
        log_event(
            "server_started",
            port=args.port,
            web_url=web_url,
            captcha_mode=_CAPTCHA_MODE,
            captcha_browser_fresh=bool(args.fresh_captcha_browser),
            auth_ready=active_state is not None,
            profile_count=len(ProxyHandler.profiles),
            api_key_protected=bool(api_key),
            log_file=log_file_label(),
            log_level=logging.getLevelName(LOG.getEffectiveLevel()),
        )
        if args.open_web:
            webbrowser.open(web_url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            log_event("server_stopped", reason="keyboard_interrupt")
            print("\n[glm2api] local service stopped.")
        finally:
            server.server_close()
            if _CAPTCHA_WORKER is not None:
                _CAPTCHA_WORKER.close()
                _CAPTCHA_WORKER = None
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
