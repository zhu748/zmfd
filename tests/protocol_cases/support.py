import ast
import io
import json
import logging
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import glm2api as app  # noqa: E402

# 测试期间的运行日志重定向到临时目录，避免污染正式 logs/glm2api.log。
# Windows 下 handler 会一直持有日志文件句柄，退出清理会失败，故忽略清理错误。
_TEST_LOG_DIR = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
app.LOG_DIR = Path(_TEST_LOG_DIR.name)
app.LOG_FILE_PATH = Path(_TEST_LOG_DIR.name) / "glm2api.log"


def fake_state() -> app.HarState:
    return app.HarState(
        token="test-token",
        user_id="test-user",
        user_name="test-user",
        device_id="",
        captcha_verify_param="",
        user_agent="test-agent",
        language="zh-CN",
        languages="zh-CN",
        screen_width="1",
        screen_height="1",
        viewport_width="1",
        viewport_height="1",
        pixel_ratio="1",
        color_depth="24",
        browser_name="Chrome",
        os_name="Windows",
        chat_id="",
    )


def web_source() -> str:
    """Return the UI markup and behavior as one searchable test fixture."""
    paths = (app.WEB_INDEX_PATH, *app.WEB_SCRIPT_PATHS)
    return "\n".join(path.read_text(encoding="utf-8") for path in paths)


class QuietProxyHandler(app.ProxyHandler):
    def log_message(self, *_args):
        pass




class ProtocolAdaptersTestSupport:
    def setUp(self) -> None:
        self.uploads: list[tuple[str, str]] = []
        self.deleted_chats: list[str] = []
        self.chat_sequence = 0
        self.original_upload = app.upload_context_package_to_zai
        self.original_stream = app.stream_zai_completion
        self.original_delete = app.delete_zai_chat
        # 测试钩子：后台自动删除改为内联执行，保证断言确定性；
        # 禁用验证码后台预解并清空验证码池，避免用例间互相污染。
        self._auto_delete_inline_prev = app._AUTO_DELETE_INLINE
        self._auto_delete_max_pending_prev = app.AUTO_DELETE_MAX_PENDING
        app._AUTO_DELETE_INLINE = True
        app._AUTO_DELETE_STOP.clear()
        self._captcha_prefetch_prev = app._CAPTCHA_PREFETCH_ENABLED
        app._CAPTCHA_PREFETCH_ENABLED = False
        app._CAPTCHA_POOL.clear()

        def fake_upload(
            _state,
            context_text: str,
            filename: str | None = None,
            label: str = "",
            **_kwargs,
        ):
            self.uploads.append((filename, context_text))
            resolved_name = filename or f"{label or 'context'}.txt"
            return {
                "id": f"file_{label or 'context'}",
                "filename": resolved_name,
                "meta": {"name": resolved_name, "size": len(context_text.encode('utf-8'))},
            }

        def fake_stream(_state, _prompt, **_kwargs):
            self.chat_sequence += 1
            context = _kwargs.get("context_out")
            if isinstance(context, dict):
                context.update({"chat_id": f"00000000-0000-0000-0000-{self.chat_sequence:012d}"})
            yield 'data: {"data":{"delta_content":"先说明。","phase":"answer"}}'
            yield (
                'data: {"data":{"delta_content":"<glm2api_tool_calls>{\\"tool_calls\\":['
                '{\\"name\\":\\"get_weather\\",\\"arguments\\":{\\"city\\":\\"北京\\"}}]}'
                '</glm2api_tool_calls>","phase":"answer"}}'
            )

        def fake_delete(_state, chat_id: str, **_kwargs):
            self.deleted_chats.append(chat_id)
            return True

        app.upload_context_package_to_zai = fake_upload
        app.stream_zai_completion = fake_stream
        app.delete_zai_chat = fake_delete
        QuietProxyHandler.state = fake_state()
        QuietProxyHandler.profiles = {}
        QuietProxyHandler.active_profile_id = ""
        QuietProxyHandler.response_store = {}
        QuietProxyHandler.chat_inflight = {}
        self.settings_tmp = tempfile.TemporaryDirectory()
        self._profile_store_path_prev = QuietProxyHandler.profile_store_path
        self._profile_store_saved_at_prev = QuietProxyHandler.profile_store_saved_at
        self._profile_store_error_prev = QuietProxyHandler.profile_store_error
        QuietProxyHandler.profile_store_path = Path(self.settings_tmp.name) / "profiles.local.json"
        QuietProxyHandler.profile_store_saved_at = ""
        QuietProxyHandler.profile_store_error = ""
        QuietProxyHandler.settings_path = Path(self.settings_tmp.name) / "settings.local.json"
        QuietProxyHandler.settings = app.local_settings_defaults()
        QuietProxyHandler.settings_saved_at = ""
        QuietProxyHandler.settings_error = ""
        QuietProxyHandler.api_key = ""
        QuietProxyHandler.api_key_store_path = Path(self.settings_tmp.name) / "apikey.local.json"
        QuietProxyHandler.api_key_saved_at = ""
        QuietProxyHandler.api_key_store_error = ""
        QuietProxyHandler.api_key_source = "store"
        # 本地请求镜像同样隔离到临时目录，避免污染仓库根目录的 history.local.json(.d)。
        self._history_path_prev = app.HISTORY_STORE_PATH
        self._history_detail_prev = app.HISTORY_DETAIL_DIR
        app.HISTORY_STORE_PATH = Path(self.settings_tmp.name) / "history.local.json"
        app.HISTORY_DETAIL_DIR = Path(self.settings_tmp.name) / "history.local.json.d"
        app._HISTORY_CACHE = None
        app._HISTORY_DIRTY.clear()
        app._HISTORY_DELETED.clear()
        self._history_store_error_prev = (app._HISTORY_STORE_ERROR, app._HISTORY_STORE_ERROR_AT)
        app._HISTORY_STORE_ERROR = ""
        app._HISTORY_STORE_ERROR_AT = ""
        self._history_conf_prev = dict(app._HISTORY_CONF)
        self._history_max_detail_bytes_prev = app.HISTORY_MAX_DETAIL_BYTES
        self._pending_delete_path_prev = app.PENDING_DELETE_STORE_PATH
        self._pending_delete_cache_prev = app._PENDING_DELETE_CACHE
        self._pending_delete_error_prev = app._PENDING_DELETE_STORE_ERROR
        self._pending_delete_replay_prev = (
            app._PENDING_DELETE_REPLAY_SCHEDULED,
            app._PENDING_DELETE_REPLAY_UNMATCHED,
            app._PENDING_DELETE_REPLAY_DEFERRED,
            app._PENDING_DELETE_REPLAY_THREAD,
        )
        app.PENDING_DELETE_STORE_PATH = Path(self.settings_tmp.name) / "pending_deletes.local.json"
        app._PENDING_DELETE_CACHE = None
        app._PENDING_DELETE_STORE_ERROR = ""
        app._PENDING_DELETE_REPLAY_SCHEDULED = 0
        app._PENDING_DELETE_REPLAY_UNMATCHED = 0
        app._PENDING_DELETE_REPLAY_DEFERRED = 0
        app._PENDING_DELETE_REPLAY_THREAD = None
        self.server = app.LocalProxyServer(("127.0.0.1", 0), QuietProxyHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        app.upload_context_package_to_zai = self.original_upload
        app.stream_zai_completion = self.original_stream
        app.delete_zai_chat = self.original_delete
        app._AUTO_DELETE_INLINE = self._auto_delete_inline_prev
        app.AUTO_DELETE_MAX_PENDING = self._auto_delete_max_pending_prev
        app._CAPTCHA_PREFETCH_ENABLED = self._captcha_prefetch_prev
        app._CAPTCHA_POOL.clear()
        app.HISTORY_STORE_PATH = self._history_path_prev
        app.HISTORY_DETAIL_DIR = self._history_detail_prev
        app._HISTORY_CACHE = None
        app._HISTORY_DIRTY.clear()
        app._HISTORY_DELETED.clear()
        app._HISTORY_STORE_ERROR, app._HISTORY_STORE_ERROR_AT = self._history_store_error_prev
        app._HISTORY_CONF.clear()
        app._HISTORY_CONF.update(self._history_conf_prev)
        app.HISTORY_MAX_DETAIL_BYTES = self._history_max_detail_bytes_prev
        app.PENDING_DELETE_STORE_PATH = self._pending_delete_path_prev
        app._PENDING_DELETE_CACHE = self._pending_delete_cache_prev
        app._PENDING_DELETE_STORE_ERROR = self._pending_delete_error_prev
        (
            app._PENDING_DELETE_REPLAY_SCHEDULED,
            app._PENDING_DELETE_REPLAY_UNMATCHED,
            app._PENDING_DELETE_REPLAY_DEFERRED,
            app._PENDING_DELETE_REPLAY_THREAD,
        ) = self._pending_delete_replay_prev
        app._AUTO_DELETE_STOP.clear()
        QuietProxyHandler.profile_store_path = self._profile_store_path_prev
        QuietProxyHandler.profile_store_saved_at = self._profile_store_saved_at_prev
        QuietProxyHandler.profile_store_error = self._profile_store_error_prev
        self.settings_tmp.cleanup()

    def request(self, method: str, path: str, body: dict | bytes | None = None, headers: dict | None = None) -> tuple[int, str]:
        if body is None:
            data = None
        elif isinstance(body, bytes):
            data = body
        else:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        all_headers = dict(headers or {})
        if data is not None:
            all_headers.setdefault("Content-Type", "application/json")
        request = Request(self.base_url + path, data=data, headers=all_headers, method=method)
        try:
            with urlopen(request, timeout=8) as response:
                return response.status, response.read().decode("utf-8")
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            status = exc.code
            exc.close()
            return status, raw

    def raw_http_request(self, request_bytes: bytes) -> tuple[int, bytes]:
        sock = app.socket.create_connection(("127.0.0.1", self.server.server_port), timeout=3)
        try:
            sock.sendall(request_bytes)
            sock.settimeout(3)
            chunks: list[bytes] = []
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
            raw = b"".join(chunks)
        finally:
            sock.close()
        header, body = raw.split(b"\r\n\r\n", 1)
        status = int(header.split(b"\r\n", 1)[0].split(b" ", 2)[1])
        return status, body

    HISTORY_CHAT_ID = "7c6f0038-580a-4988-93a2-389d5a046556"

    HISTORY_DETAIL = {
        "id": "7c6f0038-580a-4988-93a2-389d5a046556",
        "title": "你好问候",
        "chat": {
            "models": ["x-preview-l"],
            "enable_thinking": True,
            "reasoning_effort": "max",
            "history": {
                "currentId": "04149f27-3f33-4a5b-bba7-883971c16b6e",
                "messages": {
                    "04149f27-3f33-4a5b-bba7-883971c16b6e": {
                        "id": "04149f27-3f33-4a5b-bba7-883971c16b6e",
                        "parentId": "52e499e0-b32e-4983-ad43-d09b917e6698",
                        "childrenIds": [],
                        "role": "assistant",
                        "timestamp": 1787938847,
                        "content": "你好！很高兴见到你。",
                    },
                    "52e499e0-b32e-4983-ad43-d09b917e6698": {
                        "id": "52e499e0-b32e-4983-ad43-d09b917e6698",
                        "parentId": None,
                        "childrenIds": ["04149f27-3f33-4a5b-bba7-883971c16b6e"],
                        "role": "user",
                        "timestamp": 1787938846,
                        "content": "你好",
                    },
                },
            },
        },
        "updated_at": 1787938848,
        "created_at": 1787938845,
    }

    def _make_record(
        self,
        chat_id: str = "00000000-0000-0000-0000-000000000001",
        user: str = "第一问",
        answer: str = "第一答",
        thinking: str = "",
        surface: str = "openai_chat",
        finish_status: str = "success",
        error: str = "",
    ) -> str:
        record_id = app.start_history_record(
            surface=surface,
            model="glm-5.2",
            stream=True,
            user_input=user,
            messages=[{"role": "user", "content": user}],
            chat_id=chat_id,
        )
        app.finish_history_record(
            record_id,
            status=finish_status,
            content=answer,
            reasoning=thinking,
            error=error,
            chat_id=chat_id,
        )
        return record_id
