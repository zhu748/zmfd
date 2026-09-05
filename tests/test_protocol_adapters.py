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


PROJECT_ROOT = Path(__file__).resolve().parents[1]
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


class QuietProxyHandler(app.ProxyHandler):
    def log_message(self, *_args):
        pass


class ProtocolAdaptersTest(unittest.TestCase):
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

    def test_tool_parser_accepts_adapter_and_dsml_but_ignores_code_fences(self) -> None:
        tools = [{"name": "read_file", "description": "", "parameters": {"type": "object"}}]
        policy = app.ToolChoice()
        cases = [
            '<glm2api_tool_calls>{"tool_calls":[{"name":"read_file","arguments":{"path":"README.md"}}]}</glm2api_tool_calls>',
            '<tool_calls><invoke name="read_file"><parameter name="path"><![CDATA[README.md]]></parameter></invoke></tool_calls>',
            '<|DSML|tool_calls><|DSML|invoke name="read_file"><|DSML|parameter name="path"><![CDATA[README.md]]></|DSML|parameter></|DSML|invoke></|DSML|tool_calls>',
        ]
        for text in cases:
            calls = app.parse_tool_calls_from_output(text, tools, policy)
            self.assertEqual(1, len(calls))
            self.assertEqual("read_file", calls[0].name)
        fenced = "```xml\n" + cases[1] + "\n```"
        self.assertEqual([], app.parse_tool_calls_from_output(fenced, tools, policy))

        typed_tools = app.normalize_tool_definitions(
            [
                {
                    "type": "function",
                    "name": "write_file",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "content": {"type": "string"},
                            "overwrite": {"type": "boolean"},
                        },
                    },
                }
            ],
            "openai_responses",
        )
        typed_calls = app.parse_tool_calls_from_output(
            '<glm2api_tool_calls>{"tool_calls":[{"name":"write_file","arguments":'
            '{"path":["README.md"],"content":{"line":1},"overwrite":true}}]}</glm2api_tool_calls>',
            typed_tools,
            app.ToolChoice(),
        )
        self.assertEqual('["README.md"]', typed_calls[0].arguments["path"])
        self.assertEqual('{"line":1}', typed_calls[0].arguments["content"])
        self.assertIs(True, typed_calls[0].arguments["overwrite"])

    def test_tool_parser_accepts_observed_claude_code_fallback_formats(self) -> None:
        tools = [
            {
                "name": "Bash",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string"},
                        "description": {"type": "string"},
                    },
                },
            }
        ]
        policy = app.ToolChoice()
        observed = (
            "先确认文件规模："
            "<tool_call>Bash<arg_key>command</arg_key>"
            "<arg_value>cd &quot;E:/project&quot; &amp;&amp; wc -l src/*.js</arg_value>"
            # 真实历史里第二个参数漏了 opening <arg_key>，仍保留 closing tag。
            "description</arg_key><arg_value>统计源文件行数</arg_value></tool_call>"
        )
        calls = app.parse_tool_calls_from_output(observed, tools, policy)
        self.assertEqual(1, len(calls))
        self.assertEqual("Bash", calls[0].name)
        self.assertEqual('cd "E:/project" && wc -l src/*.js', calls[0].arguments["command"])
        self.assertEqual("统计源文件行数", calls[0].arguments["description"])
        self.assertEqual("先确认文件规模：", app.strip_parsed_tool_markup(observed))

        native = 'Tool: Bash\n<tool_input>{"command":"pwd","description":"查看目录"}</tool_input>'
        calling = '**Calling:** Bash\n{"command":"pwd","description":"查看目录"}'
        function_call = (
            '<function_call>{"name":"Bash","arguments":{"command":"pwd",'
            '"description":"查看目录"}}</function_call>'
        )
        for raw in (native, calling, function_call):
            parsed = app.parse_tool_calls_from_output(raw, tools, policy)
            self.assertEqual(1, len(parsed), raw)
            self.assertEqual("pwd", parsed[0].arguments["command"])
            self.assertEqual("", app.strip_parsed_tool_markup(raw), raw)

    def test_claude_fallback_examples_inside_robust_fences_are_not_executed_or_stripped(self) -> None:
        tools = [{"name": "Bash", "parameters": {"type": "object"}}]
        examples = [
            "~~~xml\n<tool_call>Bash<arg_key>command</arg_key><arg_value>pwd `x`</arg_value></tool_call>\n~~~",
            "````text\nTool: Bash\n<tool_input>{\"command\":\"pwd\"}</tool_input>\n````",
            "```xml\n<function_call>{\"name\":\"Bash\",\"arguments\":{}}</function_call>",
        ]
        for raw in examples:
            self.assertEqual([], app.parse_tool_calls_from_output(raw, tools, app.ToolChoice()), raw)
            self.assertEqual(raw, app.strip_parsed_tool_markup(raw), raw)

    def test_dsml_separator_and_local_name_drift_is_normalized(self) -> None:
        tools = [{"name": "Bash", "parameters": {"type": "object"}}]
        markup = (
            '<！DSML！ToolCalls><DSMLinvoke name="Bash">'
            '<、DSML、parameter name="command">pwd</、DSML、parameter>'
            '〈/DSMLinvoke〉</！DSML！ToolCalls>'
        )
        calls = app.parse_tool_calls_from_output(markup, tools, app.ToolChoice())
        self.assertEqual(1, len(calls))
        self.assertEqual({"command": "pwd"}, calls[0].arguments)
        self.assertEqual("", app.strip_parsed_tool_markup(markup))

    def test_strip_parsed_tool_markup_preserves_code_fences(self) -> None:
        fenced = '示例：\n```xml\n<tool_calls><invoke name="demo"></invoke></tool_calls>\n```\n保留。'
        cleaned = app.strip_parsed_tool_markup(fenced)
        self.assertIn("<tool_calls>", cleaned)
        self.assertIn("```xml", cleaned)
        self.assertIn("保留", cleaned)
        real = '说明\n<glm2api_tool_calls>{"tool_calls":[]}</glm2api_tool_calls>\n正文'
        cleaned_real = app.strip_parsed_tool_markup(real)
        self.assertNotIn("glm2api_tool_calls", cleaned_real)
        self.assertEqual("说明\n\n正文", cleaned_real)

    def test_finalize_rejects_malformed_tool_markup_for_bounded_retry(self) -> None:
        request = app.normalize_openai_chat_request(
            {
                "model": "glm-5.2",
                "messages": [{"role": "user", "content": "hi"}],
                "tools": [{"type": "function", "function": {"name": "read_file", "parameters": {}}}],
            },
            False,
        )
        malformed = '前置<glm2api_tool_calls>{"broken": </glm2api_tool_calls>后置'
        with self.assertRaises(app.ToolCallFormatError):
            app.finalize_protocol_turn(request, malformed, "")
        self.assertEqual("前置后置", app.strip_parsed_tool_markup(malformed))

    def test_tool_output_strict_mode_rejects_partial_or_invalid_batches(self) -> None:
        tools = app.normalize_tool_definitions(
            [
                {
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "parameters": {
                            "type": "object",
                            "properties": {"path": {"type": "string"}},
                            "required": ["path"],
                        },
                    },
                }
            ],
            "openai_chat",
        )
        mixed = (
            '<glm2api_tool_calls>{"tool_calls":['
            '{"name":"read_file","arguments":{"path":"README.md"}},'
            '{"name":"undeclared_tool","arguments":{}}]}'
            '</glm2api_tool_calls>'
        )
        with self.assertRaisesRegex(app.ToolCallFormatError, "未声明"):
            app.parse_tool_calls_from_output(mixed, tools, app.ToolChoice())

        malformed_arguments = (
            '<glm2api_tool_calls>{"tool_calls":['
            '{"name":"read_file","arguments":"{path:"}]}'
            '</glm2api_tool_calls>'
        )
        with self.assertRaisesRegex(app.ToolCallFormatError, "arguments 不是"):
            app.parse_tool_calls_from_output(malformed_arguments, tools, app.ToolChoice())

        missing_required = (
            '<glm2api_tool_calls>{"tool_calls":['
            '{"name":"read_file","arguments":{}}]}'
            '</glm2api_tool_calls>'
        )
        with self.assertRaisesRegex(app.ToolCallFormatError, "缺少必填字段: path"):
            app.parse_tool_calls_from_output(missing_required, tools, app.ToolChoice())

    def test_tool_output_limits_calls_arguments_and_definitions(self) -> None:
        tools = [{"name": "echo", "parameters": {"type": "object", "properties": {}}}]
        previous_calls = app.MAX_TOOL_CALLS_PER_TURN
        previous_arguments = app.MAX_TOOL_ARGUMENTS_BYTES
        try:
            app.MAX_TOOL_CALLS_PER_TURN = 1
            with self.assertRaisesRegex(app.ToolCallFormatError, "超过 1 个"):
                app.normalize_tool_call_candidates(
                    [
                        {"name": "echo", "arguments": {}},
                        {"name": "echo", "arguments": {}},
                    ],
                    tools,
                    app.ToolChoice(),
                    strict=True,
                )
            app.MAX_TOOL_ARGUMENTS_BYTES = 16
            with self.assertRaisesRegex(app.ToolCallFormatError, "arguments 超过"):
                app.normalize_tool_call_candidates(
                    [{"name": "echo", "arguments": {"value": "x" * 64}}],
                    tools,
                    app.ToolChoice(),
                    strict=True,
                )
        finally:
            app.MAX_TOOL_CALLS_PER_TURN = previous_calls
            app.MAX_TOOL_ARGUMENTS_BYTES = previous_arguments

        too_many = [
            {
                "type": "function",
                "function": {"name": f"tool_{index}", "parameters": {"type": "object"}},
            }
            for index in range(app.MAX_TOOL_DEFINITIONS + 1)
        ]
        with self.assertRaisesRegex(ValueError, "definition limit"):
            app.normalize_tool_definitions(too_many, "openai_chat")

    def test_tool_parser_merges_multiple_json_wrappers_in_output_order(self) -> None:
        tools = [{"name": "echo", "parameters": {"type": "object"}}]
        output = (
            '准备两项。<glm2api_tool_calls>{"tool_calls":['
            '{"name":"echo","arguments":{"value":"first"}}]}'
            '</glm2api_tool_calls>继续<glm2api_tool_calls>{"tool_calls":['
            '{"name":"echo","arguments":{"value":"second"}}]}'
            '</glm2api_tool_calls>'
        )
        calls = app.parse_tool_calls_from_output(output, tools, app.ToolChoice())
        self.assertEqual(["first", "second"], [call.arguments["value"] for call in calls])
        self.assertEqual(2, len({call.id for call in calls}))
        self.assertEqual("准备两项。继续", app.strip_parsed_tool_markup(output))

    def test_tool_parser_repairs_common_model_format_slips(self) -> None:
        tools = [
            {
                "name": "read_file",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "options": {"type": "object"},
                        "items": {"type": "array"},
                        "optional": {},
                    },
                },
            }
        ]
        markup = r'''＜|DSML|tool_calls＞
          ＜|DSML|invoke name＝“read_file”＞
            ＜|DSML|parameter name＝“path”＞<![CDATA["C:\workspace\test\notes.txt"]]＞＜/|DSML|parameter＞
            ＜|DSML|parameter name＝“options”＞<mode>fast</mode><limit>2</limit>＜/|DSML|parameter＞
            ＜|DSML|parameter name＝“items”＞<item>one</item><item>2</item>＜/|DSML|parameter＞
            ＜|DSML|parameter name＝“optional”＞null＜/|DSML|parameter＞
          ＜/|DSML|invoke＞'''
        calls = app.parse_tool_calls_from_output(markup, tools, app.ToolChoice())
        self.assertEqual(1, len(calls))
        self.assertEqual(r"C:\workspace\test\notes.txt", calls[0].arguments["path"])
        self.assertEqual({"mode": "fast", "limit": 2}, calls[0].arguments["options"])
        self.assertEqual(["one", 2], calls[0].arguments["items"])
        self.assertIsNone(calls[0].arguments["optional"])

        loose = '<tool_calls><invoke name="read_file"><parameter name="options"><![CDATA[{mode:"fast",items:[1,2,],}]]></parameter></invoke>'
        repaired = app.parse_tool_calls_from_output(loose, tools, app.ToolChoice())
        self.assertEqual({"mode": "fast", "items": [1, 2]}, repaired[0].arguments["options"])
        path_value, valid = app._parse_tool_json_value(r'"C:\workspace\new\file.txt"')
        self.assertTrue(valid)
        self.assertEqual(r"C:\workspace\new\file.txt", app._repair_tool_path_controls(path_value))

    def test_strip_parsed_tool_markup_removes_truncated_adapter_tags(self) -> None:
        cases = [
            ('<tool_calls><invoke name="x"><parameter name="a">1</parameter></invoke>', ""),
            ('<|DSML|parameter name="a"><![CDATA[hello]]></|DSML|parameter>', "hello"),
            ('<glm2api_tool_calls><invoke name="x"></glm2api_tool_calls>', ""),
            ("</tool_calls>", ""),
        ]
        for raw, expected in cases:
            self.assertEqual(expected, app.strip_parsed_tool_markup(raw), raw)
        fenced = '示例：\n```xml\n<invoke name="x"><parameter name="a">1</parameter></invoke>\n```\n保留'
        cleaned = app.strip_parsed_tool_markup(fenced)
        self.assertIn("<invoke", cleaned)
        self.assertIn("保留", cleaned)

    def test_xml_fallback_unwraps_cdata_when_tree_parse_fails(self) -> None:
        tools = [{"name": "get_weather", "description": "", "parameters": {"type": "object"}}]
        policy = app.ToolChoice(mode="required")
        # The raw & inside the second parameter makes ElementTree fail, so the
        # tolerant regex path must unwrap CDATA before treating values as JSON.
        markup = (
            "<tool_calls><invoke name=\"get_weather\">"
            "<parameter name=\"city\"><![CDATA[北京]]></parameter>"
            "<parameter name=\"units\">1 & 2</parameter>"
            "</invoke></tool_calls>"
        )
        calls = app.parse_tool_calls_from_output(markup, tools, policy)
        self.assertEqual(1, len(calls))
        self.assertEqual("北京", calls[0].arguments["city"])
        self.assertEqual("1 & 2", calls[0].arguments["units"])

    def test_dsml_close_tags_without_slash_and_extra_pipes_are_parsed(self) -> None:
        # 线上实测：上游把关闭 invoke 标签写成 `<||DSML|invoke>`（双竖杠、无斜杠），
        # 旧正则归一不了导致整块解析失败、工具调用丢失。真实 payload 回归。
        markup = (
            "我来看看当前项目的结构和关键文件，了解它是做什么的。\n\n"
            '<|DSML|tool_calls>\n'
            '  <|DSML|invoke name="Glob">\n'
            '    <|DSML|parameter name="pattern"><![CDATA[*]]></|DSML|parameter>\n'
            "  <||DSML|invoke>\n"
            '  <|DSML|invoke name="Glob">\n'
            '    <|DSML|parameter name="pattern"><![CDATA[**/*.md]]></|DSML|parameter>\n'
            "  <||DSML|invoke>\n"
            '  <|DSML|invoke name="Glob">\n'
            '    <|DSML|parameter name="pattern"><![CDATA[package.json]]></|DSML|parameter>\n'
            "  <||DSML|invoke>\n"
            "</|DSML|tool_calls>"
        )
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "Glob",
                    "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}}},
                },
            }
        ]
        request = app.normalize_openai_chat_request(
            {"model": "glm-5.2", "messages": [{"role": "user", "content": "hi"}], "tools": tools},
            False,
        )
        turn = app.finalize_protocol_turn(request, markup, "")
        self.assertEqual(3, len(turn.tool_calls), "三个畸形关闭标签的 invoke 都应解析出来")
        self.assertTrue(all(call.name == "Glob" for call in turn.tool_calls))
        self.assertEqual("*", turn.tool_calls[0].arguments["pattern"])
        self.assertEqual("**/*.md", turn.tool_calls[1].arguments["pattern"])
        self.assertEqual("package.json", turn.tool_calls[2].arguments["pattern"])
        self.assertEqual("我来看看当前项目的结构和关键文件，了解它是做什么的。", turn.text)
        # 各种竖杠/斜杠变体都应归一成干净标签；裸 invoke/parameter 视为关闭标签
        self.assertEqual("</invoke>", app.canonicalize_dsml_tool_markup("<||DSML|invoke>"))
        self.assertEqual("</invoke>", app.canonicalize_dsml_tool_markup("<|||DSML|invoke>"))
        self.assertEqual("</invoke>", app.canonicalize_dsml_tool_markup("</|DSML|invoke>"))
        self.assertEqual("<tool_calls>", app.canonicalize_dsml_tool_markup("<|DSML|tool_calls|>"))
        self.assertEqual("<invoke>", app.canonicalize_dsml_tool_markup("<invoke>"))
        # 开标签（带 name= 属性）不能误判为关闭
        self.assertEqual('<invoke name="x">', app.canonicalize_dsml_tool_markup('<||DSML|invoke name="x">'))

    def test_required_tool_choice_falls_back_to_thinking(self) -> None:
        tools = [{"type": "function", "function": {"name": "get_weather", "parameters": {}}}]
        markup = '<|DSML|tool_calls><|DSML|invoke name="get_weather"><|DSML|parameter name="city"><![CDATA[上海]]></|DSML|parameter></|DSML|invoke></|DSML|tool_calls>'

        required = app.normalize_openai_chat_request(
            {"model": "glm-5.2", "messages": [{"role": "user", "content": "天气？"}], "tools": tools, "tool_choice": "required"},
            False,
        )
        turn = app.finalize_protocol_turn(required, "我来查一下。", markup)
        self.assertEqual(1, len(turn.tool_calls))
        self.assertEqual("get_weather", turn.tool_calls[0].name)
        self.assertEqual("我来查一下。", turn.text)
        self.assertEqual("", turn.thinking)

        auto = app.normalize_openai_chat_request(
            {"model": "glm-5.2", "messages": [{"role": "user", "content": "天气？"}], "tools": tools},
            False,
        )
        # Auto mode follows the model's visible final-channel decision. Hidden
        # thinking markup is never promoted into an executable client call.
        auto_turn = app.finalize_protocol_turn(auto, "我来查一下。", markup)
        self.assertEqual([], auto_turn.tool_calls)
        self.assertEqual("我来查一下。", auto_turn.text)
        self.assertEqual("", auto_turn.thinking)

    def test_auto_keeps_tool_free_decision_with_fenced_thinking_call(self) -> None:
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "Read",
                    "parameters": {
                        "type": "object",
                        "properties": {"file_path": {"type": "string"}},
                    },
                },
            }
        ]
        request = app.normalize_openai_chat_request(
            {"model": "glm-5.3", "messages": [{"role": "user", "content": "继续审查项目"}], "tools": tools},
            True,
        )
        thinking = (
            "The task is unfinished, so I will read the core file now.\n\n"
            "```xml\n"
            '<|DSML|tool_calls><|DSML|invoke name="Read">'
            '<|DSML|parameter name="file_path"><![CDATA[C:\\workspace\\src\\server.js]]>'
            '</|DSML|parameter></|DSML|invoke></|DSML|tool_calls>\n'
            "```\n\nThe final channel should contain that call."
        )

        turn = app.finalize_protocol_turn(request, "接下来读取核心文件。", thinking)
        self.assertEqual([], turn.tool_calls)
        self.assertEqual("接下来读取核心文件。", turn.text)

        retry_request = app.protocol_request_with_tool_retry_hint(request, "malformed wrapper")
        self.assertEqual("auto", retry_request.tool_choice.mode)
        self.assertTrue(retry_request.tool_retry_active)
        turn = app.finalize_protocol_turn(retry_request, "接下来读取核心文件。", thinking)
        self.assertEqual([], turn.tool_calls)
        self.assertEqual("接下来读取核心文件。", turn.text)

    def test_auto_retry_does_not_execute_instruction_examples_from_thinking(self) -> None:
        tools = [{"name": "Bash", "description": "Run command", "parameters": {"type": "object"}}]
        request = app.ProtocolRequest(
            surface="anthropic_messages",
            response_model="glm-5.3",
            options=app.ChatOptions(),
            stream=False,
            messages=[],
            context_text="",
            execution_prompt="",
            files=[],
            tools=tools,
            tool_choice=app.ToolChoice(),
            context_as_file=False,
            tool_retry_active=True,
        )
        thinking = "I am checking the instruction examples:\n\n" + app.build_tool_instruction(tools, request.tool_choice)
        turn = app.finalize_protocol_turn(request, "任务已经完整完成。", thinking)
        self.assertEqual([], turn.tool_calls)

    def test_auto_keeps_tool_free_decision_with_claude_style_hidden_call(self) -> None:
        tools = [
            {
                "name": "Bash",
                "parameters": {
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                },
            }
        ]
        request = app.ProtocolRequest(
            surface="anthropic_messages",
            response_model="glm-5.3",
            options=app.ChatOptions(),
            stream=False,
            messages=[],
            context_text="",
            execution_prompt="",
            files=[],
            tools=tools,
            tool_choice=app.ToolChoice(),
            context_as_file=False,
        )
        thinking = (
            "The task is unfinished.\n"
            "<tool_call>Bash<arg_key>command</arg_key><arg_value>pwd</arg_value></tool_call>"
        )
        turn = app.finalize_protocol_turn(request, "接下来检查目录。", thinking)
        self.assertEqual([], turn.tool_calls)
        self.assertNotIn("<tool_call>", turn.thinking)

        retry_request = app.protocol_request_with_tool_retry_hint(request, "hidden Claude-style call")
        turn = app.finalize_protocol_turn(retry_request, "接下来检查目录。", thinking)
        self.assertEqual([], turn.tool_calls)
        self.assertNotIn("<tool_call>", turn.thinking)

        malformed = "<tool_call>Bash<arg_key>command</arg_key><arg_value>pwd</tool_call>"
        malformed_turn = app.finalize_protocol_turn(request, "接下来检查目录。", malformed)
        self.assertEqual([], malformed_turn.tool_calls)

    def test_trailing_pipe_dsml_parses_and_strips(self) -> None:
        tools = [{"name": "get_weather", "description": "", "parameters": {"type": "object"}}]
        policy = app.ToolChoice(mode="required")
        markup = (
            "<|DSML|tool_calls|><|DSML|invoke name=\"get_weather\"|>"
            "<|DSML|parameter name=\"city\"|><![CDATA[北京]]></|DSML|parameter|>"
            "</|DSML|invoke|></|DSML|tool_calls|>"
        )
        calls = app.parse_tool_calls_from_output(markup, tools, policy)
        self.assertEqual(1, len(calls))
        self.assertEqual("get_weather", calls[0].name)
        self.assertEqual("北京", calls[0].arguments["city"])
        self.assertEqual("", app.strip_parsed_tool_markup(markup))
        self.assertEqual(
            '<parameter name="city">',
            app.canonicalize_dsml_tool_markup('<|DSML|parameter name="city"|>'),
        )

    def test_captcha_browser_uses_started_playwright_instance(self) -> None:
        original_factory = app._captcha_playwright
        original_launch = app._launch_captcha_page
        original_solve = app.solve_captcha_on_page
        seen: dict[str, object] = {}

        class FakeInstance:
            marker = "started-instance"

            def stop(self):
                seen["stopped"] = True

        class FakeContextManager:
            def __init__(self, instance):
                self._instance = instance

            def start(self):
                seen["started"] = True
                return self._instance

        fake_instance = FakeInstance()
        fake_cm = FakeContextManager(fake_instance)

        def fake_factory():
            # Mirrors _captcha_playwright(): the returned callable is the
            # sync_playwright entry point, and calling it yields the context manager.
            return lambda: fake_cm

        class FakeBrowser:
            def close(self):
                seen["browser_closed"] = True

        def fake_launch(pw, state, **_kwargs):
            seen["launch_pw"] = pw
            return (FakeBrowser(), "context", "page")

        def fake_solve(page, timeout_ms):
            seen["page"] = page
            return "captcha-ok"

        app._captcha_playwright = fake_factory
        app._launch_captcha_page = fake_launch
        app.solve_captcha_on_page = fake_solve
        try:
            value = app.get_browser_captcha(fake_state(), timeout_ms=10_000)
            self.assertEqual("captcha-ok", value)
            # The launcher must receive the started Playwright instance, not
            # the context manager (PlaywrightContextManager has no .chromium).
            self.assertIs(fake_instance, seen["launch_pw"])
            self.assertTrue(seen.get("started"))
            self.assertTrue(seen.get("stopped"))
        finally:
            app._captcha_playwright = original_factory
            app._launch_captcha_page = original_launch
            app.solve_captcha_on_page = original_solve

    def test_tool_call_id_prefixes_match_surface(self) -> None:
        tools = [{"type": "function", "function": {"name": "get_weather", "parameters": {}}}]
        markup = '<|DSML|tool_calls><|DSML|invoke name="get_weather"><|DSML|parameter name="city"><![CDATA[北京]]></|DSML|parameter></|DSML|invoke></|DSML|tool_calls>'
        expectations = [
            ("openai_chat", "call_", 29),
            ("openai_responses", "fc_", 27),
            ("anthropic_messages", "toolu_", 30),
        ]
        for surface, prefix, expected_len in expectations:
            if surface == "openai_chat":
                request = app.normalize_openai_chat_request(
                    {"model": "glm-5.2", "messages": [{"role": "user", "content": "天气？"}], "tools": tools, "tool_choice": "required"},
                    False,
                )
            elif surface == "openai_responses":
                request = app.normalize_openai_responses_request(
                    {"model": "glm-5.2", "input": "天气？", "tools": tools, "tool_choice": "required"},
                    False,
                )
            else:
                request = app.normalize_anthropic_messages_request(
                    {"model": "glm-5.2", "messages": [{"role": "user", "content": "天气？"}], "tools": [{"name": "get_weather", "input_schema": {"type": "object"}}], "tool_choice": {"type": "any"}},
                    False,
                )
            turn = app.finalize_protocol_turn(request, markup, "")
            self.assertEqual(1, len(turn.tool_calls), surface)
            call_id = turn.tool_calls[0].id
            self.assertTrue(call_id.startswith(prefix), (surface, call_id))
            self.assertEqual(expected_len, len(call_id), (surface, call_id))
            self.assertEqual("北京", turn.tool_calls[0].arguments["city"], surface)

    def test_responses_object_embeds_thinking_summary(self) -> None:
        request = app.normalize_openai_responses_request(
            {"model": "glm-5.2", "input": "hello", "include_thinking": True},
            True,
        )
        turn = app.finalize_protocol_turn(request, "正文", "思考内容")
        payload = app.build_openai_response_object("resp_test", request, turn, status="completed")
        self.assertEqual([{"type": "summary_text", "text": "思考内容"}], payload["reasoning"]["summary"])
        self.assertEqual("正文", payload["output_text"])
        hidden_request = app.normalize_openai_responses_request({"model": "glm-5.2", "input": "hello"}, False)
        hidden = app.build_openai_response_object(
            "resp_test3", hidden_request, app.finalize_protocol_turn(hidden_request, "正文", "思考内容"), status="completed"
        )
        self.assertIsNone(hidden["reasoning"]["summary"])

    def test_responses_output_embeds_reasoning_item(self) -> None:
        request = app.normalize_openai_responses_request(
            {"model": "glm-5.2", "input": "hello", "include_thinking": True},
            True,
        )
        turn = app.finalize_protocol_turn(request, "正文", "思考内容")
        payload = app.build_openai_response_object("resp_reason", request, turn, status="completed")
        reasoning_items = [item for item in payload["output"] if item["type"] == "reasoning"]
        self.assertEqual(1, len(reasoning_items))
        self.assertEqual([{"type": "summary_text", "text": "思考内容"}], reasoning_items[0]["summary"])
        self.assertTrue(reasoning_items[0]["id"].startswith("rs_"))

        hidden = app.build_openai_response_object(
            "resp_reason2",
            app.normalize_openai_responses_request({"model": "glm-5.2", "input": "hello"}, False),
            app.finalize_protocol_turn(request, "正文", "思考内容"),
            status="completed",
        )
        self.assertNotIn("reasoning", [item["type"] for item in hidden["output"]])

    def test_responses_output_item_reasoning_shape(self) -> None:
        turn = app.ProtocolTurn(text="正文", thinking="思考", tool_calls=[], input_tokens=0, output_tokens=0)
        items = app.build_responses_output(turn, include_reasoning=True)
        self.assertEqual("reasoning", items[0]["type"])
        self.assertEqual("completed", items[0]["status"])
        self.assertEqual([{"type": "summary_text", "text": "思考"}], items[0]["summary"])
        self.assertEqual("message", items[1]["type"])
        plain = app.build_responses_output(turn, include_reasoning=False)
        self.assertNotIn("reasoning", [item["type"] for item in plain])

    def test_file_cleanup_endpoint_deletes_orphan_files(self) -> None:
        original_http_json = app.http_json
        delete_calls: list[tuple[str, str]] = []

        def fake_http_json(method, url, headers, payload=None, **_kwargs):
            if method == "DELETE":
                delete_calls.append((method, url))
                return {"deleted": True}
            raise AssertionError(f"unexpected call: {method} {url}")

        app.http_json = fake_http_json
        try:
            status, body = self.request(
                "POST",
                "/api/files/cleanup",
                {"files": [{"id": "file_1"}, "file_2", 123, ""]},
            )
            self.assertEqual(202, status)
            data = json.loads(body)
            self.assertEqual(2, data["count"])
            self.assertEqual(["file_1", "file_2"], data["accepted"])
            self.assertEqual(2, data["accepted_count"])
            self.assertEqual(2, data["invalid_count"])
            self.assertEqual(0, data["journal_capacity_dropped"])
            self.assertTrue(data["scheduled"])
            self.assertTrue(data["cleanup_pending"])
            self.assertTrue(data["journal_persisted"])
            self.assertEqual([], data["removed"])
            self.assertEqual(2, len(delete_calls))
            self.assertTrue(all(url.endswith("/api/v1/files/file_1") or url.endswith("/api/v1/files/file_2") for _, url in delete_calls))
            self.assertEqual(0, app.pending_chat_delete_status()["journal_file_pending"])
        finally:
            app.http_json = original_http_json

    def test_file_cleanup_endpoint_journals_failed_deletes_and_classifies_errors(self) -> None:
        original_delete = app.delete_zai_file
        original_cleanup = app._best_effort_delete_upstream_files
        original_pending_add = app.pending_resource_deletes_add

        def failing_delete(*_args, **_kwargs):
            raise app.UpstreamRequestError("temporary upstream delete failure")

        app.delete_zai_file = failing_delete
        try:
            status, raw = self.request("POST", "/api/files/cleanup", {"files": ["file_retry_1"]})
            self.assertEqual(202, status, raw[:500])
            payload = json.loads(raw)
            self.assertTrue(payload["scheduled"])
            self.assertTrue(payload["cleanup_pending"])
            self.assertEqual(1, app.pending_chat_delete_status()["journal_file_pending"])
            journal = json.loads(app.PENDING_DELETE_STORE_PATH.read_text(encoding="utf-8"))
            self.assertEqual("file_retry_1", journal["items"][0]["resource_id"])
            self.assertEqual(1, journal["items"][0]["attempts"])

            missing_status, missing_raw = self.request(
                "POST",
                "/api/files/cleanup",
                {"files": ["file_retry_2"], "profile_id": "profile_missing"},
            )
            self.assertEqual(404, missing_status, missing_raw[:500])
            self.assertEqual("profile_not_found", json.loads(missing_raw)["error"]["code"])

            leaked_path = "C:" + "\\Users\\cleanup-user\\queue.tmp"

            def failing_enqueue(*_args, **_kwargs):
                raise RuntimeError(f"queue failed at '{leaked_path}'")

            app._best_effort_delete_upstream_files = failing_enqueue
            internal_status, internal_raw = self.request(
                "POST",
                "/api/files/cleanup",
                {"files": ["file_retry_3"]},
            )
            self.assertEqual(500, internal_status, internal_raw[:500])
            self.assertEqual("file_cleanup_enqueue_failed", json.loads(internal_raw)["error"]["code"])
            self.assertNotIn("cleanup-user", internal_raw)

            app._best_effort_delete_upstream_files = original_cleanup
            app.pending_resource_deletes_add = lambda *_args, **_kwargs: []
            capacity_status, capacity_raw = self.request(
                "POST",
                "/api/files/cleanup",
                {"files": ["file_capacity_drop"]},
            )
            self.assertEqual(200, capacity_status, capacity_raw[:500])
            capacity_payload = json.loads(capacity_raw)
            self.assertEqual(0, capacity_payload["accepted_count"])
            self.assertEqual(1, capacity_payload["journal_capacity_dropped"])
            self.assertFalse(capacity_payload["scheduled"])
            self.assertFalse(capacity_payload["cleanup_pending"])
        finally:
            app.delete_zai_file = original_delete
            app._best_effort_delete_upstream_files = original_cleanup
            app.pending_resource_deletes_add = original_pending_add

    def test_delete_zai_file_404_405_semantics(self) -> None:
        original_http_json = app.http_json
        state = fake_state()
        errors: list[HTTPError] = []
        try:
            seen: list[int] = []

            def fake_http_json(method, url, headers, payload=None, **_kwargs):
                seen.append(404)
                error = HTTPError(url, 404, "Not Found", None, io.BytesIO(b"gone"))
                errors.append(error)
                raise error

            app.http_json = fake_http_json
            self.assertTrue(app.delete_zai_file(state, "file_1"))
            self.assertTrue(errors[-1].closed)

            def fake_405(method, url, headers, payload=None, **_kwargs):
                seen.append(405)
                error = HTTPError(url, 405, "Method Not Allowed", None, io.BytesIO(b"unsupported"))
                errors.append(error)
                raise error

            app.http_json = fake_405
            self.assertFalse(app.delete_zai_file(state, "file_2"))
            self.assertTrue(errors[-1].closed)
            self.assertEqual([404, 405], seen)

            with self.assertRaisesRegex(ValueError, "invalid file id"):
                app.delete_zai_file(state, "a/b")
        finally:
            app.http_json = original_http_json

    def test_cleanup_failed_upstream_files_collects_ids(self) -> None:
        original_delete = app.delete_zai_file
        state = fake_state()
        deleted: list[str] = []

        def fake_delete(_state, file_id, **_kwargs):
            deleted.append(file_id)
            return True

        app.delete_zai_file = fake_delete
        handler = QuietProxyHandler.__new__(QuietProxyHandler)
        try:
            handler._cleanup_failed_upstream_files(
                state,
                [
                    {"id": "f1"},
                    {"id": "f1"},
                    {"file": {"id": "f2"}},
                    {"id": ""},
                    "not-a-dict",
                ],
            )
            self.assertEqual(["f1", "f2"], deleted)
            self.assertEqual(0, app.pending_chat_delete_status()["journal_file_pending"])
        finally:
            app.delete_zai_file = original_delete

    def test_tool_policy_and_context_file_switches(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires at least one"):
            app.normalize_openai_chat_request(
                {
                    "model": "gpt-5",
                    "messages": [{"role": "user", "content": "hello"}],
                    "tool_choice": "required",
                },
                False,
            )

        request = app.normalize_openai_chat_request(
            {
                "model": "gpt-5-forcehistory-nothinking",
                "messages": [{"role": "user", "content": "hello"}],
                "tools": [{"type": "function", "function": {"name": "read_file", "parameters": {}}}],
                "parallel_tool_calls": False,
            },
            True,
        )
        self.assertTrue(request.context_as_file)
        self.assertFalse(request.options.enable_thinking)
        self.assertTrue(request.tool_choice.disable_parallel)

        regular_request = app.normalize_openai_responses_request(
            {"model": "glm-5.2", "input": "不应自动走历史文件"},
            False,
        )
        self.assertFalse(regular_request.context_as_file)
        self.assertEqual("glm-5.2", regular_request.options.model)
        self.assertEqual("glm-5.2", app.normalize_model("GLM-5.1-forcehistory"))
        no_thinking_response = app.normalize_openai_responses_request(
            {"model": "gpt-5-nothinking", "input": "x", "reasoning": {"effort": "high"}},
            True,
        )
        self.assertFalse(no_thinking_response.options.enable_thinking)
        self.assertEqual(
            [
                "glm-5.3",
                "x-preview-l",
                "GLM-5-Turbo",
                "glm-5.2",
                "glm-5.3-forcehistory",
                "x-preview-l-forcehistory",
                "GLM-5-Turbo-forcehistory",
                "glm-5.2-forcehistory",
            ],
            list(app.ADVERTISED_MODELS),
        )

    def test_disable_parallel_tool_calls_rejects_multi_call_output_on_all_surfaces(self) -> None:
        openai_tools = [
            {"type": "function", "function": {"name": "echo", "parameters": {"type": "object"}}}
        ]
        anthropic_tools = [
            {"name": "echo", "input_schema": {"type": "object"}}
        ]
        requests = [
            app.normalize_openai_chat_request(
                {
                    "model": "glm-5.2",
                    "messages": [{"role": "user", "content": "echo"}],
                    "tools": openai_tools,
                    "parallel_tool_calls": False,
                },
                False,
            ),
            app.normalize_openai_responses_request(
                {
                    "model": "glm-5.2",
                    "input": "echo",
                    "tools": [{"type": "function", "name": "echo", "parameters": {"type": "object"}}],
                    "parallel_tool_calls": False,
                },
                False,
            ),
            app.normalize_anthropic_messages_request(
                {
                    "model": "glm-5.2",
                    "messages": [{"role": "user", "content": "echo"}],
                    "tools": anthropic_tools,
                    "tool_choice": {"type": "auto", "disable_parallel_tool_use": True},
                },
                False,
            ),
        ]
        output = (
            '<glm2api_tool_calls>{"tool_calls":['
            '{"name":"echo","arguments":{"value":1}},'
            '{"name":"echo","arguments":{"value":2}}]}'
            '</glm2api_tool_calls>'
        )
        for request in requests:
            self.assertTrue(request.tool_choice.disable_parallel, request.surface)
            with self.assertRaisesRegex(app.ToolCallFormatError, "禁用并行"):
                app.finalize_protocol_turn(request, output, "")

        parallel = app.normalize_openai_chat_request(
            {
                "model": "glm-5.2",
                "messages": [{"role": "user", "content": "echo"}],
                "tools": openai_tools,
                "parallel_tool_calls": True,
            },
            False,
        )
        self.assertEqual(2, len(app.finalize_protocol_turn(parallel, output, "").tool_calls))

    def test_legacy_openai_function_result_links_to_prior_function_call(self) -> None:
        messages = app.normalize_openai_messages_for_protocol(
            [
                {"role": "user", "content": "查询天气"},
                {
                    "role": "assistant",
                    "content": "",
                    "function_call": {
                        "name": "get_weather",
                        "arguments": '{"city":"北京"}',
                    },
                },
                {"role": "function", "name": "get_weather", "content": "晴 25 度"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_modern_1",
                            "type": "function",
                            "function": {"name": "get_weather", "arguments": '{"city":"上海"}'},
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_modern_1",
                    "name": "get_weather",
                    "content": "多云 23 度",
                },
            ]
        )
        legacy_call_id = messages[1]["tool_calls"][0]["id"]
        self.assertTrue(legacy_call_id.startswith("call_history_"))
        self.assertEqual("tool", messages[2]["role"])
        self.assertEqual(legacy_call_id, messages[2]["tool_call_id"])
        self.assertEqual("call_modern_1", messages[4]["tool_call_id"])
        transcript = app.build_history_transcript(messages)
        self.assertIn(f"invocation_id={legacy_call_id}", transcript)
        self.assertIn("晴 25 度", transcript)

    def test_allowed_tools_filters_prompt_and_parser(self) -> None:
        body = {
            "model": "glm-5.2",
            "input": "读取文件",
            "tools": [
                {"type": "function", "name": "read_file", "parameters": {"type": "object"}},
                {"type": "function", "name": "get_weather", "parameters": {"type": "object"}},
            ],
            "tool_choice": {
                "type": "allowed_tools",
                "mode": "required",
                "tools": [{"type": "function", "name": "read_file"}],
            },
        }
        request = app.normalize_openai_responses_request(body, False)
        self.assertEqual(("read_file",), request.tool_choice.allowed_names)
        self.assertIn("name: read_file", request.context_text)
        self.assertNotIn("name: get_weather", request.context_text)
        raw_calls = [
            {"name": "get_weather", "arguments": {}},
            {"name": "read_file", "arguments": {"path": "README.md"}},
        ]
        calls = app.normalize_tool_call_candidates(raw_calls, request.tools, request.tool_choice)
        self.assertEqual(["read_file"], [call.name for call in calls])

        with self.assertRaisesRegex(ValueError, "undeclared tool"):
            app.normalize_openai_responses_request(
                {**body, "tool_choice": {"type": "allowed_tools", "tools": [{"name": "missing"}]}},
                False,
            )

        forced = app.ToolChoice(mode="forced", forced_name="read_file", allowed_names=("read_file",))
        duplicate_calls = app.normalize_tool_call_candidates(
            [
                {"name": "read_file", "arguments": {"path": "a"}},
                {"name": "read_file", "arguments": {"path": "b"}},
            ],
            request.tools,
            forced,
        )
        self.assertEqual(1, len(duplicate_calls))

    def test_file_mode_upstream_prompt_guard_and_mirror_alignment(self) -> None:
        # 对齐 dkceshi 文件模式：两个附件（纯对话 / 纯工具）+ 一个聊天框
        # （Output integrity guard + 执行指令）；镜像 history_text 记实际附件内容。
        request = app.normalize_openai_chat_request(
            {
                "model": "glm-5.2-forcehistory",
                "messages": [
                    {"role": "system", "content": "你是助手"},
                    {"role": "user", "content": "北京天气"},
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "get_weather", "arguments": "{\"city\": \"北京\"}"},
                            }
                        ],
                    },
                    {"role": "tool", "tool_call_id": "call_1", "content": "晴 25 度"},
                    {"role": "user", "content": "谢谢，总结一下"},
                ],
                "tools": [
                    {"type": "function", "function": {"name": "get_weather", "parameters": {"type": "object"}}}
                ],
            },
            False,
        )
        self.assertTrue(request.context_as_file)
        self.uploads = []
        trace: dict[str, object] = {}
        prompt, files = app.prepare_protocol_upstream_request(fake_state(), request, trace_out=trace)
        # 聊天框：守卫、System 等价工具指引、User 等价续写提示；不再内联整段上下文
        self.assertTrue(prompt.startswith("Output integrity guard:"))
        self.assertIn("The attached file holds the earlier conversation", prompt)
        self.assertIn("function-call contract", prompt)
        self.assertNotIn("[user]", prompt)
        self.assertLess(prompt.index(app.OUTPUT_INTEGRITY_GUARD_PROMPT), prompt.index(app.MODE_B_TOOL_GUIDANCE))
        self.assertLess(prompt.index(app.MODE_B_TOOL_GUIDANCE), prompt.index("When you choose to invoke a function"))
        self.assertLess(
            prompt.index("When you choose to invoke a function"),
            prompt.index("The attached file holds the earlier conversation"),
        )
        self.assertNotIn("The second attachment", prompt)
        self.assertNotIn("Mandatory call:", prompt)
        # 两个附件：fake_upload 按调用顺序记录（tools 先传、history 后传）
        uploaded = [context for _name, context in self.uploads]
        self.assertEqual(2, len(uploaded))
        self.assertTrue(uploaded[1].startswith(app.HISTORY_TRANSCRIPT_INTRO))
        self.assertIn("[tool]", uploaded[1])
        self.assertNotIn("name: get_weather", uploaded[1], "history 附件不应混入工具 schema")
        self.assertTrue(uploaded[0].startswith(app.TOOLS_TRANSCRIPT_INTRO))
        self.assertIn("name: get_weather", uploaded[0])
        self.assertEqual(2, len(files))
        self.assertEqual("file", trace["delivery_mode"])
        self.assertEqual("file", trace["requested_mode"])
        self.assertEqual("", trace["fallback_reason"])
        context_files = trace["context_files"]
        self.assertIsInstance(context_files, list)
        self.assertEqual(["history", "tools"], [item["kind"] for item in context_files])
        self.assertEqual(["history.txt", "tools.txt"], [item["name"] for item in context_files])
        self.assertEqual(uploaded[1], context_files[0]["content"])
        self.assertEqual(uploaded[0], context_files[1]["content"])
        # 镜像 history_text 与实际 history 附件一致（不是合并包）
        mirror = app.build_history_transcript(request.messages)
        self.assertIn("[tool]", mirror)
        self.assertNotIn("name: get_weather", mirror)

    def test_file_mode_tool_choice_none_matches_reference_attachment_policy(self) -> None:
        request = app.normalize_openai_chat_request(
            {
                "model": "glm-5.2-forcehistory",
                "messages": [
                    {"role": "user", "content": "先查天气"},
                    {"role": "tool", "tool_call_id": "old_call", "name": "get_weather", "content": "晴"},
                    {"role": "user", "content": "现在只总结，不调用工具"},
                ],
                "tools": [
                    {"type": "function", "function": {"name": "get_weather", "parameters": {"type": "object"}}}
                ],
                "tool_choice": "none",
            },
            False,
        )
        self.uploads = []
        trace: dict[str, object] = {}
        prompt, files = app.prepare_protocol_upstream_request(fake_state(), request, trace_out=trace)

        self.assertEqual(app.current_input_file_prompt(False), prompt)
        self.assertNotIn("Output integrity guard", prompt)
        self.assertEqual(1, len(files))
        self.assertEqual(1, len(self.uploads))
        self.assertEqual(["history"], [item["kind"] for item in trace["context_files"]])
        self.assertIn("[tool]", self.uploads[0][1])
        self.assertNotIn(app.TOOLS_TRANSCRIPT_INTRO, self.uploads[0][1])

    def test_glm53_splits_generated_history_and_tools_below_readable_boundary(self) -> None:
        latest_request = "LATEST_USER_REQUEST_MUST_SURVIVE"
        request = app.normalize_openai_chat_request(
            {
                "model": "glm-5.3-forcehistory",
                "messages": [
                    {"role": "system", "content": "system rules"},
                    {"role": "user", "content": "old context\n" + ("历史填充。" * 18000)},
                    {"role": "assistant", "content": "previous answer"},
                    {"role": "user", "content": latest_request},
                ],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "large_schema_tool",
                            "description": "D" * 60000,
                            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
                        },
                    }
                ],
            },
            False,
        )
        self.uploads = []
        trace: dict[str, object] = {}

        prompt, files = app.prepare_protocol_upstream_request(fake_state(), request, trace_out=trace)

        self.assertIn("split across multiple attachments", prompt)
        self.assertGreater(len(files), 2)
        self.assertEqual(len(files), len(self.uploads))
        self.assertTrue(
            all(len(content.encode("utf-8")) <= app.GLM53_CONTEXT_FILE_PART_BYTES for _name, content in self.uploads)
        )
        context_files = trace["context_files"]
        history_parts = [item for item in context_files if item["kind"] == "history"]
        tools_parts = [item for item in context_files if item["kind"] == "tools"]
        self.assertGreater(len(history_parts), 1)
        self.assertGreater(len(tools_parts), 1)
        self.assertEqual(list(range(1, len(history_parts) + 1)), [item["part"] for item in history_parts])
        self.assertTrue(all(item["parts"] == len(history_parts) for item in history_parts))
        self.assertIn(latest_request, history_parts[-1]["content"])
        self.assertIn(
            f"conversation history segment {len(history_parts)}/{len(history_parts)}",
            history_parts[-1]["content"],
        )
        self.assertIn("function definitions segment 1/", tools_parts[0]["content"])
        persisted = app.history_context_files_snapshot(context_files)
        self.assertEqual(
            [(item["kind"], item["part"], item["parts"]) for item in context_files],
            [(item["kind"], item["part"], item["parts"]) for item in persisted],
        )

    def test_glm53_oversized_context_plan_uses_ten_file_preload_waves(self) -> None:
        generated = [{"id": f"generated-{index}"} for index in range(25)]
        trace = [
            {"kind": "history", "part": index + 1, "parts": 25, "content": f"part-{index}"}
            for index in range(25)
        ]
        preload, final_files, final_trace = app.plan_staged_context_files(generated, trace, [])

        self.assertEqual([10, 10], [len(files) for files, _items in preload])
        self.assertEqual([5], [len(final_files)])
        self.assertEqual([20, 21, 22, 23, 24], [item["part"] - 1 for item in final_trace])

        preload_with_user, final_with_user, final_trace_with_user = app.plan_staged_context_files(
            generated[:11],
            trace[:11],
            [{"id": "user-file"}],
        )
        self.assertEqual([10], [len(files) for files, _items in preload_with_user])
        self.assertEqual(["generated-10", "user-file"], [item["id"] for item in final_with_user])
        self.assertEqual([11], [item["part"] for item in final_trace_with_user])

        history_then_tool = trace[:10] + [
            {"kind": "tools", "part": 1, "parts": 1, "content": "tool definitions"}
        ]
        preload_with_tool, final_with_tool, final_tool_trace = app.plan_staged_context_files(
            generated[:11],
            history_then_tool,
            [],
        )
        self.assertEqual([10], [len(files) for files, _items in preload_with_tool])
        self.assertEqual(["generated-10"], [item["id"] for item in final_with_tool])
        self.assertEqual(["tools"], [item["kind"] for item in final_tool_trace])

    def test_glm53_oversized_context_defers_all_generated_uploads(self) -> None:
        request = app.normalize_openai_chat_request(
            {
                "model": "glm-5.3-forcehistory",
                "messages": [{"role": "user", "content": "H" * 430_000}],
            },
            False,
        )
        trace: dict[str, object] = {}

        prompt, files = app.prepare_protocol_upstream_request(fake_state(), request, trace_out=trace)

        self.assertEqual([], files)
        self.assertEqual([], self.uploads, "超限上下文不能在首个预载波次前上传全部分片")
        self.assertTrue(trace["deferred_context_upload"])
        self.assertGreater(len(trace["context_files"]), app.ZAI_MAX_COMPLETION_FILES)
        self.assertIn("attached file holds the earlier conversation", prompt)

    def test_completion_file_limit_is_rejected_before_upstream_generation(self) -> None:
        user_files = [{"id": f"00000000-0000-0000-0000-{index:012d}"} for index in range(11)]
        with self.assertRaisesRegex(ValueError, "at most 10 user files"):
            app.normalize_openai_chat_request(
                {
                    "model": "glm-5.3",
                    "messages": [{"role": "user", "content": "too many files"}],
                    "files": user_files,
                },
                False,
            )
        with self.assertRaisesRegex(ValueError, "at most 10 files"):
            app.completion_payload(
                fake_state(),
                "too many files",
                "00000000-0000-0000-0000-000000000501",
                "00000000-0000-0000-0000-000000000502",
                files=user_files,
            )

        status, raw = self.request(
            "POST",
            "/v1/chat/completions",
            {
                "model": "glm-5.2-forcehistory",
                "messages": [{"role": "user", "content": "history plus ten user files"}],
                "files": user_files[:10],
                "stream": False,
            },
        )
        self.assertEqual(400, status, raw[:500])
        self.assertIn("at most 10 files", raw)
        self.assertEqual(0, self.chat_sequence, "附件总数超限时不能创建上游 generation")
        self.assertEqual([], self.uploads, "不可分批的超限请求必须在内部附件上传前拒绝")

    def test_preload_stop_failure_does_not_poison_upload_health(self) -> None:
        state = fake_state()
        original_stop = app.stop_zai_task
        original_cleanup = app._best_effort_delete_upstream_chat
        app._CONTEXT_UPLOAD_FAILURES.pop(state.user_id, None)
        app._CONTEXT_UPLOAD_DEGRADED_UNTIL.pop(state.user_id, None)

        def thinking_stream(_state, _prompt, **kwargs):
            kwargs["context_out"].update(
                {
                    "chat_id": "00000000-0000-0000-0000-000000000511",
                    "assistant_message_id": "00000000-0000-0000-0000-000000000512",
                }
            )
            yield 'data: {"data":{"delta_content":"thinking","phase":"thinking"}}'

        app.stream_zai_completion = thinking_stream
        app.stop_zai_task = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("stop failed"))
        app._best_effort_delete_upstream_chat = lambda *_args, **_kwargs: True
        poisoned_failure = False
        poisoned_degraded = False
        try:
            with self.assertRaisesRegex(RuntimeError, "stop failed"):
                app.preload_zai_context_waves(
                    state,
                    [([], [{"kind": "history", "part": 1, "parts": 1, "content": "context"}])],
                    app.ChatOptions(model="glm-5.3"),
                )
            poisoned_failure = state.user_id in app._CONTEXT_UPLOAD_FAILURES
            poisoned_degraded = state.user_id in app._CONTEXT_UPLOAD_DEGRADED_UNTIL
        finally:
            app.stop_zai_task = original_stop
            app._best_effort_delete_upstream_chat = original_cleanup
            app._CONTEXT_UPLOAD_FAILURES.pop(state.user_id, None)
            app._CONTEXT_UPLOAD_DEGRADED_UNTIL.pop(state.user_id, None)

        self.assertFalse(poisoned_failure)
        self.assertFalse(poisoned_degraded)

    def test_context_preload_waves_stop_thinking_and_chain_parent(self) -> None:
        original_stop = app.stop_zai_task
        original_cleanup = app._best_effort_delete_upstream_files
        calls: list[dict[str, object]] = []
        stopped: list[str] = []
        cleaned_files: list[list[str]] = []
        chat_id = "00000000-0000-0000-0000-000000000321"

        def preload_stream(_state, prompt, **kwargs):
            index = len(calls) + 1
            assistant_id = f"00000000-0000-0000-0000-{index:012d}"
            context = kwargs["context_out"]
            context.update({"chat_id": kwargs.get("chat_id") or chat_id, "assistant_message_id": assistant_id})
            calls.append({"prompt": prompt, **kwargs})
            if index == 1:
                kwargs["retry_files_factory"]()
            yield 'data: {"data":{"delta_content":"thinking","phase":"thinking"}}'
            raise AssertionError("preload must close the stream after the first thinking delta")

        app.stream_zai_completion = preload_stream
        app.stop_zai_task = lambda _state, assistant_id: stopped.append(assistant_id) or {"status": True}
        app._best_effort_delete_upstream_files = (
            lambda _state, file_ids, **_kwargs: cleaned_files.append(list(file_ids)) or True
        )
        waves = [
            ([], [{"kind": "history", "part": 1, "parts": 2, "content": "one"}]),
            ([], [{"kind": "history", "part": 2, "parts": 2, "content": "two"}]),
        ]
        try:
            result_chat, result_parent = app.preload_zai_context_waves(
                fake_state(),
                waves,
                app.ChatOptions(model="glm-5.3"),
                retry_wait_sec=0,
                retry_attempts=2,
            )
        finally:
            app.stop_zai_task = original_stop
            app._best_effort_delete_upstream_files = original_cleanup

        self.assertEqual(chat_id, result_chat)
        self.assertEqual(stopped[-1], result_parent)
        self.assertEqual(2, len(calls))
        self.assertTrue(calls[0]["create_chat"])
        self.assertFalse(calls[1]["create_chat"])
        self.assertIsNone(calls[0]["parent_message_id"])
        self.assertEqual(stopped[0], calls[1]["parent_message_id"])
        self.assertTrue(all(call["preserve_chat_on_generator_close"] for call in calls))
        self.assertEqual(3, len(self.uploads), "预载应逐波上传，繁忙重试应重新上传当前波")
        self.assertTrue(all(len(call["files"]) == 1 for call in calls))
        self.assertEqual([["file_history_1_of_2"]], cleaned_files, "重传后必须回收被替代的旧附件")
        self.assertIn("Do not answer", calls[0]["prompt"])
        self.assertEqual(2, len(stopped))

    def test_har_aligned_preload_chain_retries_second_wave_on_same_parent(self) -> None:
        state = fake_state()
        chat_id = "00000000-0000-0000-0000-000000000431"
        first_user_id = "00000000-0000-0000-0000-000000000432"
        first_file_id = "00000000-0000-0000-0000-000000000433"
        second_file_id = "00000000-0000-0000-0000-000000000434"
        retried_file_id = "00000000-0000-0000-0000-000000000435"
        original_new_chat = app.new_chat
        original_urlopen = app.urlopen
        original_stop = app.stop_zai_task
        original_cleanup = app._best_effort_delete_upstream_files
        original_upload = app.upload_context_package_to_zai
        payloads: list[dict[str, object]] = []
        stopped: list[str] = []
        cleaned: list[list[str]] = []
        new_chat_calls = 0

        class FakeResp:
            def __init__(self, chunks):
                self.chunks = chunks

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def __iter__(self):
                return iter(self.chunks)

            def close(self):
                pass

        busy = (
            'data: {"data":{"done":true,"error":{"code":"MODEL_CONCURRENCY_LIMIT",'
            '"detail":"当前模型使用人数较多"}},"type":"chat:completion"}\n\n'
        ).encode("utf-8")
        responses = [
            FakeResp([b'data: {"data":{"delta_content":"first thinking","phase":"thinking"}}\n\n']),
            FakeResp([busy]),
            FakeResp([b'data: {"data":{"delta_content":"second thinking","phase":"thinking"}}\n\n']),
        ]

        def fake_new_chat(_state, _prompt, options=None):
            nonlocal new_chat_calls
            new_chat_calls += 1
            return chat_id, first_user_id

        def fake_urlopen(request, timeout=None):
            payloads.append(json.loads(request.data.decode("utf-8")))
            return responses[len(payloads) - 1]

        upload_ids = iter((first_file_id, second_file_id, retried_file_id))

        def fake_upload(_state, _text, filename=None, label="", *, reuse_cache=True, cache_hit_out=None):
            if cache_hit_out is not None:
                cache_hit_out["hit"] = False
            file_id = next(upload_ids)
            return {"id": file_id, "filename": filename or f"{label}.txt"}

        app.new_chat = fake_new_chat
        app.urlopen = fake_urlopen
        app.stop_zai_task = lambda _state, assistant_id: stopped.append(assistant_id) or {"status": True}
        app.upload_context_package_to_zai = fake_upload
        app._best_effort_delete_upstream_files = (
            lambda _state, file_ids, **_kwargs: cleaned.append(list(file_ids)) or True
        )
        app.stream_zai_completion = self.original_stream
        try:
            result_chat, result_parent = app.preload_zai_context_waves(
                state,
                [
                    (
                        [],
                        [{"kind": "history", "part": 1, "parts": 2, "content": "first"}],
                    ),
                    (
                        [],
                        [{"kind": "history", "part": 2, "parts": 2, "content": "second"}],
                    ),
                ],
                app.ChatOptions(model="glm-5.3"),
                retry_wait_sec=0,
                retry_attempts=2,
            )
        finally:
            app.new_chat = original_new_chat
            app.urlopen = original_urlopen
            app.stop_zai_task = original_stop
            app._best_effort_delete_upstream_files = original_cleanup
            app.upload_context_package_to_zai = original_upload

        self.assertEqual(chat_id, result_chat)
        self.assertEqual(1, new_chat_calls)
        self.assertEqual(3, len(payloads))
        first_assistant = payloads[0]["id"]
        self.assertEqual([None, first_assistant, first_assistant], [
            payload["current_user_message_parent_id"] for payload in payloads
        ])
        self.assertEqual([chat_id, chat_id, chat_id], [payload["chat_id"] for payload in payloads])
        self.assertNotEqual(payloads[1]["current_user_message_id"], payloads[2]["current_user_message_id"])
        self.assertNotEqual(payloads[1]["id"], payloads[2]["id"])
        self.assertEqual(second_file_id, payloads[1]["files"][0]["id"])
        self.assertEqual(retried_file_id, payloads[2]["files"][0]["id"])
        self.assertEqual([first_assistant, payloads[2]["id"]], stopped)
        self.assertEqual(payloads[2]["id"], result_parent)
        self.assertEqual([[second_file_id]], cleaned)

    def test_preload_retry_never_deletes_shared_cache_hit(self) -> None:
        original_upload = app.upload_context_package_to_zai
        original_cleanup = app._best_effort_delete_upstream_files
        original_stop = app.stop_zai_task
        shared_id = "00000000-0000-0000-0000-000000000441"
        refreshed_id = "00000000-0000-0000-0000-000000000442"
        cleanup_calls: list[list[str]] = []
        upload_calls = 0

        def fake_upload(_state, _text, filename=None, label="", *, reuse_cache=True, cache_hit_out=None):
            nonlocal upload_calls
            upload_calls += 1
            hit = upload_calls == 1
            if cache_hit_out is not None:
                cache_hit_out["hit"] = hit
            return {"id": shared_id if hit else refreshed_id, "filename": f"{label}.txt"}

        def retrying_stream(_state, _prompt, **kwargs):
            refreshed = kwargs["retry_files_factory"]()
            self.assertEqual(refreshed_id, refreshed[0]["id"])
            kwargs["context_out"].update(
                {
                    "chat_id": "00000000-0000-0000-0000-000000000443",
                    "assistant_message_id": "00000000-0000-0000-0000-000000000444",
                }
            )
            yield 'data: {"data":{"delta_content":"thinking","phase":"thinking"}}'

        app.upload_context_package_to_zai = fake_upload
        app._best_effort_delete_upstream_files = (
            lambda _state, file_ids, **_kwargs: cleanup_calls.append(list(file_ids)) or True
        )
        app.stop_zai_task = lambda *_args, **_kwargs: {"status": True}
        app.stream_zai_completion = retrying_stream
        try:
            app.preload_zai_context_waves(
                fake_state(),
                [([], [{"kind": "history", "part": 1, "parts": 1, "content": "shared"}])],
                app.ChatOptions(model="glm-5.3"),
            )
        finally:
            app.upload_context_package_to_zai = original_upload
            app._best_effort_delete_upstream_files = original_cleanup
            app.stop_zai_task = original_stop

        self.assertEqual([[]], cleanup_calls)
        self.assertNotIn(shared_id, [file_id for batch in cleanup_calls for file_id in batch])

    def test_context_wave_partial_upload_failure_deletes_only_owned_files(self) -> None:
        original_upload = app.upload_context_package_to_zai
        original_cleanup = app._best_effort_delete_upstream_files
        state = fake_state()
        shared_id = "00000000-0000-0000-0000-000000000445"
        owned_id = "00000000-0000-0000-0000-000000000446"
        cleanup_calls: list[list[str]] = []
        upload_calls = 0

        def fake_upload(_state, _text, filename=None, label="", *, reuse_cache=True, cache_hit_out=None):
            nonlocal upload_calls
            upload_calls += 1
            if upload_calls == 3:
                raise app.UpstreamRequestError("wave upload failed")
            hit = upload_calls == 1
            if cache_hit_out is not None:
                cache_hit_out["hit"] = hit
            return {
                "id": shared_id if hit else owned_id,
                "filename": filename or f"{label}.txt",
            }

        app.upload_context_package_to_zai = fake_upload
        app._best_effort_delete_upstream_files = (
            lambda _state, file_ids, **_kwargs: cleanup_calls.append(list(file_ids)) or True
        )
        owned: list[dict[str, object]] = []
        try:
            with self.assertRaisesRegex(app.UpstreamRequestError, "wave upload failed"):
                app.upload_context_trace_files(
                    state,
                    [
                        {"kind": "history", "part": 1, "parts": 3, "content": "shared"},
                        {"kind": "history", "part": 2, "parts": 3, "content": "owned"},
                        {"kind": "history", "part": 3, "parts": 3, "content": "failure"},
                    ],
                    reuse_cache=True,
                    owned_out=owned,
                )
        finally:
            app.upload_context_package_to_zai = original_upload
            app._best_effort_delete_upstream_files = original_cleanup
            app._CONTEXT_UPLOAD_FAILURES.pop(state.user_id, None)
            app._CONTEXT_UPLOAD_DEGRADED_UNTIL.pop(state.user_id, None)

        self.assertEqual([], owned, "失败波次的所有权不应提交给外层并触发重复清理")
        self.assertEqual([[owned_id]], cleanup_calls)
        self.assertNotIn(shared_id, cleanup_calls[0])

    def test_protocol_completion_stages_over_ten_files_and_forces_chat_cleanup(self) -> None:
        original_preload = app.preload_zai_context_waves
        staged_chat = "00000000-0000-0000-0000-000000000451"
        staged_parent = "00000000-0000-0000-0000-000000000452"
        previous_chat = "00000000-0000-0000-0000-000000000453"
        preload_sizes: list[int] = []
        final_calls: list[dict[str, object]] = []

        def fake_preload(_state, waves, _options, **_kwargs):
            preload_sizes.extend(len(trace) for _files, trace in waves)
            return staged_chat, staged_parent

        def final_stream(_state, prompt, **kwargs):
            final_calls.append({"prompt": prompt, **kwargs})
            context = kwargs["context_out"]
            context["chat_id"] = kwargs["chat_id"]
            yield 'data: {"data":{"delta_content":"STAGED_OK","phase":"answer"}}'

        app.preload_zai_context_waves = fake_preload
        app.stream_zai_completion = final_stream
        try:
            status, raw = self.request(
                "POST",
                "/v1/chat/completions",
                {
                    "model": "glm-5.3-forcehistory",
                    "messages": [{"role": "user", "content": "H" * 430_000}],
                    "stream": False,
                    "delete_chat_after_completion": False,
                    "mode": "continue",
                    "chat_id": previous_chat,
                },
            )
        finally:
            app.preload_zai_context_waves = original_preload

        self.assertEqual(200, status, raw[:500])
        self.assertIn("STAGED_OK", raw)
        self.assertEqual([10], preload_sizes)
        self.assertEqual(1, len(final_calls))
        final = final_calls[0]
        self.assertFalse(final["create_chat"])
        self.assertEqual(staged_chat, final["chat_id"])
        self.assertNotEqual(previous_chat, final["chat_id"])
        self.assertEqual(staged_parent, final["parent_message_id"])
        self.assertTrue(final["retry_reused_chat"])
        self.assertLessEqual(len(final["files"]), app.ZAI_MAX_COMPLETION_FILES)
        self.assertIn("intentionally stopped during reasoning", final["prompt"])
        self.assertEqual([staged_chat], self.deleted_chats, "分批上下文 chat 必须在最终响应后回收")
        records = app.local_history_records()
        self.assertEqual(1, len(records))
        self.assertEqual(1, records[0]["context_preload_waves"])
        self.assertEqual(10, records[0]["context_preload_files"])
        self.assertEqual(0, records[0]["tool_retry_count"], "预载不能误计为工具格式重试")
        metrics = app.local_history_metrics(24)
        self.assertEqual(1, metrics["staged_context_requests"])
        self.assertEqual(1, metrics["context_preload_waves"])
        self.assertEqual(10, metrics["context_preload_files"])

    def test_final_wave_upload_failure_records_error_and_deletes_preload_chat(self) -> None:
        original_preload = app.preload_zai_context_waves
        staged_chat = "00000000-0000-0000-0000-000000000461"
        state = fake_state()
        app._CONTEXT_UPLOAD_FAILURES.pop(state.user_id, None)
        app._CONTEXT_UPLOAD_DEGRADED_UNTIL.pop(state.user_id, None)

        app.preload_zai_context_waves = lambda *_args, **_kwargs: (
            staged_chat,
            "00000000-0000-0000-0000-000000000462",
        )
        app.upload_context_package_to_zai = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            app.UpstreamRequestError("final wave upload failed")
        )
        try:
            status, raw = self.request(
                "POST",
                "/v1/chat/completions",
                {
                    "model": "glm-5.3-forcehistory",
                    "messages": [{"role": "user", "content": "H" * 430_000}],
                    "stream": False,
                },
            )
        finally:
            app.preload_zai_context_waves = original_preload
            app._CONTEXT_UPLOAD_FAILURES.pop(state.user_id, None)
            app._CONTEXT_UPLOAD_DEGRADED_UNTIL.pop(state.user_id, None)

        self.assertEqual(502, status, raw[:500])
        self.assertIn("final wave upload failed", raw)
        self.assertEqual([staged_chat], self.deleted_chats)
        records = app.local_history_records()
        self.assertEqual(1, len(records))
        self.assertEqual("error", records[0]["status"])
        self.assertEqual(1, records[0]["context_preload_waves"])
        self.assertEqual(10, records[0]["context_preload_files"])
        self.assertIn("final wave upload failed", records[0]["error"])

    def test_glm52_keeps_large_generated_context_as_single_files(self) -> None:
        request = app.normalize_openai_chat_request(
            {
                "model": "glm-5.2-forcehistory",
                "messages": [{"role": "user", "content": "H" * 70000}],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "large_schema_tool",
                            "description": "D" * 60000,
                            "parameters": {"type": "object"},
                        },
                    }
                ],
            },
            False,
        )
        self.uploads = []
        trace: dict[str, object] = {}

        _prompt, files = app.prepare_protocol_upstream_request(fake_state(), request, trace_out=trace)

        self.assertEqual(2, len(files))
        self.assertEqual(["history", "tools"], [item["kind"] for item in trace["context_files"]])
        self.assertEqual([1, 1], [item["parts"] for item in trace["context_files"]])
        self.assertTrue(all("[glm2api " not in content for _name, content in self.uploads))

    def test_output_integrity_guard_scope(self) -> None:
        # 无工具且无 tool 历史：不加守卫（避免多余指纹）；有工具：守卫前置且不重复
        plain = app.normalize_openai_chat_request(
            {"model": "glm-5.2", "messages": [{"role": "user", "content": "hi"}]},
            False,
        )
        prompt, _files = app.prepare_protocol_upstream_request(fake_state(), plain)
        self.assertNotIn("Output integrity guard", prompt)
        self.assertEqual(plain.context_text, prompt)

        tool_req = app.normalize_openai_chat_request(
            {
                "model": "glm-5.2",
                "messages": [{"role": "user", "content": "天气"}],
                "tools": [{"type": "function", "function": {"name": "get_weather", "parameters": {"type": "object"}}}],
            },
            False,
        )
        prompt_g, _f = app.prepare_protocol_upstream_request(fake_state(), tool_req)
        self.assertTrue(prompt_g.startswith("Output integrity guard:"))
        self.assertTrue(prompt_g.endswith(tool_req.context_text))
        again = app.apply_output_integrity_guard(prompt_g, tool_req.tools, tool_req.messages)
        self.assertEqual(prompt_g, again, "已带守卫不应重复前置")

    def test_api_logs_endpoint_and_ring_capture(self) -> None:
        app.setup_logging("INFO", console=False)
        app.set_current_request_id("abc12345")
        try:
            app.log_event("api_logs_probe", marker="z-1234")
        finally:
            app.set_current_request_id("")
        status, raw = self.request("GET", "/api/logs?lines=50")
        self.assertEqual(200, status)
        data = json.loads(raw)
        self.assertTrue(data["ok"])
        self.assertTrue(any("api_logs_probe" in line for line in data["lines"]))
        self.assertTrue(any("z-1234" in line for line in data["lines"]))
        self.assertTrue(any(entry["state"] == "api_logs_probe" for entry in data["entries"]))
        self.assertTrue(any(entry["rid"] == "abc12345" for entry in data["entries"]))
        self.assertGreaterEqual(data["stats"]["kinds"]["event"], 1)
        self.assertTrue(data["ring_capacity"] >= data["ring_count"])
        self.assertEqual(data["file_bytes"], data["store"]["active_bytes"])
        self.assertGreaterEqual(data["store"]["total_bytes"], data["store"]["active_bytes"])
        self.assertEqual(app.LOG_BACKUP_COUNT + 1, data["store"]["max_segments"])
        self.assertEqual(
            app.LOG_MAX_BYTES * (app.LOG_BACKUP_COUNT + 1),
            data["store"]["max_total_bytes"],
        )
        status, raw = self.request("GET", "/api/logs?lines=50&level=ERROR")
        data = json.loads(raw)
        self.assertTrue(all("[ERROR]" in line for line in data["lines"]))
        status, raw = self.request("GET", "/api/logs?lines=50&text=nomatch-xyz")
        self.assertEqual([], json.loads(raw)["lines"])
        status, raw = self.request(
            "GET",
            "/api/logs?lines=50&kind=event&state=api_logs_probe&rid=abc12345",
        )
        filtered = json.loads(raw)
        self.assertEqual(200, status)
        self.assertEqual(1, filtered["stats"]["matched"])
        self.assertEqual("api_logs_probe", filtered["entries"][0]["state"])

        cursor = int(filtered["cursor"]["last_seq"])
        app.set_current_request_id("abc12345")
        try:
            app.log_event("api_logs_probe", marker="z-5678")
        finally:
            app.set_current_request_id("")
        status, raw = self.request(
            "GET",
            f"/api/logs?lines=50&format=structured&state=api_logs_probe&after_seq={cursor}",
        )
        incremental = json.loads(raw)
        self.assertEqual(200, status)
        self.assertNotIn("lines", incremental)
        self.assertFalse(incremental["cursor"]["reset_required"])
        self.assertEqual(2, incremental["stats"]["matched"])
        self.assertEqual(1, len(incremental["entries"]))
        self.assertIn("z-5678", incremental["entries"][0]["line"])

    def test_log_ring_cursor_recovers_after_eviction_or_restart(self) -> None:
        ring = app.RingBufferHandler(capacity=2)
        ring.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        for index in range(4):
            ring.emit(logging.LogRecord("test", logging.INFO, __file__, 1, f"event-{index}", (), None))

        entries, matched, cursor = ring.query(limit=10, after_seq=1)
        self.assertEqual(2, matched)
        self.assertEqual([3, 4], [entry["seq"] for entry in entries])
        self.assertTrue(cursor["reset_required"])
        self.assertEqual(3, cursor["first_seq"])
        self.assertEqual(4, cursor["last_seq"])
        self.assertEqual(2, ring.stats()["capacity"])

        entries, _matched, cursor = ring.query(limit=10, after_seq=99)
        self.assertEqual([3, 4], [entry["seq"] for entry in entries])
        self.assertTrue(cursor["reset_required"])

    def test_log_ring_caps_individual_records_and_preserves_event_metadata(self) -> None:
        ring = app.RingBufferHandler(capacity=2)
        ring.setFormatter(app.RedactingFormatter("%(levelname)s %(message)s"))
        provider_key = "ghp_" + ("z" * 32)
        record = logging.LogRecord(
            "test",
            logging.WARNING,
            __file__,
            1,
            f"token={provider_key} " + ("长" * (app.LOG_RECORD_MAX_CHARS * 2)),
            (),
            None,
        )
        record.glm2api_event_state = "oversize_probe"
        record.glm2api_event_rid = "abc12345"

        ring.emit(record)

        entries, matched, _cursor = ring.query(limit=2, state="oversize_probe")
        self.assertEqual(1, matched)
        self.assertEqual("event", entries[0]["kind"])
        self.assertEqual("abc12345", entries[0]["rid"])
        self.assertLessEqual(len(entries[0]["message"]), app.LOG_RECORD_MAX_CHARS)
        self.assertLessEqual(len(entries[0]["line"]), app.LOG_RECORD_MAX_CHARS)
        self.assertTrue(entries[0]["line"].endswith(app.LOG_TRUNCATION_SUFFIX))
        self.assertNotIn(provider_key, entries[0]["message"])
        self.assertEqual(1, ring.stats()["truncated_total"])
        self.assertEqual(app.LOG_RECORD_MAX_CHARS, ring.stats()["max_record_chars"])

    def test_runtime_metrics_bounds_path_cardinality_and_length(self) -> None:
        metrics = app.RuntimeMetrics()
        long_path = "/long/" + ("x" * (app.MAX_RUNTIME_METRIC_PATH_CHARS * 3))
        for _ in range(20):
            metrics.record_http("GET", long_path, 404, 1)
        for index in range(app.MAX_RUNTIME_METRIC_PATHS + 50):
            metrics.record_http("GET", f"/random-probe/{index}", 404, 1)

        snapshot = metrics.snapshot()
        self.assertLessEqual(snapshot["tracked_paths"], app.MAX_RUNTIME_METRIC_PATHS)
        self.assertEqual(app.MAX_RUNTIME_METRIC_PATHS, snapshot["max_paths"])
        self.assertGreater(snapshot["path_overflow_total"], 0)
        self.assertTrue(all(len(item["path"]) <= app.MAX_RUNTIME_METRIC_PATH_CHARS for item in snapshot["top_paths"]))
        overflow = next(item for item in snapshot["top_paths"] if item["path"] == app.RUNTIME_METRIC_OTHER_PATH)
        self.assertEqual(snapshot["path_overflow_total"], overflow["count"])

    def test_api_metrics_aggregates_history_without_content(self) -> None:
        now_ms = int(time.time() * 1000)
        app._HISTORY_CACHE = [
            {
                "id": "req_metric_success",
                "status": "success",
                "surface": "openai_chat",
                "caller": "api",
                "model": "glm-5.3",
                "created_at": now_ms - 60_000,
                "elapsed_ms": 1200,
                "status_code": 200,
                "delivery_mode": "file",
                "context_file_fallback": "",
                "usage": {"prompt_tokens": 10, "completion_tokens": 20, "reasoning_tokens": 5, "total_tokens": 35},
                "finish_reason": "tool_calls",
                "tool_calls_count": 2,
                "tool_call_names": ["secret_tool_name"],
                "tool_calls_source": "thinking_retry",
                "tool_retry_count": 1,
                "final_prompt": "secret-prompt-must-not-leak",
                "content": "secret-answer-must-not-leak",
            },
            {
                "id": "req_metric_error",
                "status": "error",
                "surface": "anthropic_messages",
                "caller": "api",
                "model": "glm-5.2",
                "created_at": now_ms - 30_000,
                "elapsed_ms": 2400,
                "status_code": 503,
                "delivery_mode": "inline",
                "context_file_fallback": "upload_failed",
                "usage": {},
                "tool_retry_count": 1,
                "error": "secret-upstream-error-must-not-leak",
            },
        ]
        metrics = app.local_history_metrics(24, now_ms=now_ms)
        self.assertEqual(2, metrics["requests"])
        self.assertEqual({"success": 1, "error": 1, "stopped": 0, "streaming": 0}, metrics["statuses"])
        self.assertEqual(0.5, metrics["success_rate"])
        self.assertEqual(1800, metrics["avg_elapsed_ms"])
        self.assertEqual(2400, metrics["p95_elapsed_ms"])
        self.assertEqual(35, metrics["tokens"]["total_tokens"])
        self.assertEqual(1, metrics["file_delivery_requests"])
        self.assertEqual(1, metrics["fallback_requests"])
        self.assertEqual(
            {
                "turns": 1,
                "calls": 2,
                "turn_rate": 0.5,
                "format_retry_requests": 2,
                "format_retry_successes": 1,
                "format_retry_success_rate": 0.5,
                "thinking_recovered_turns": 1,
            },
            metrics["tools"],
        )
        self.assertEqual(2, sum(bucket["total"] for bucket in metrics["timeline"]))
        serialized = json.dumps(metrics, ensure_ascii=False)
        self.assertNotIn("secret-prompt", serialized)
        self.assertNotIn("secret-answer", serialized)
        self.assertNotIn("secret-upstream-error", serialized)
        self.assertNotIn("secret_tool_name", serialized)

        self.request("GET", "/api/hello")
        status, raw = self.request("GET", "/api/metrics?hours=24")
        self.assertEqual(200, status)
        payload = json.loads(raw)
        self.assertTrue(payload["ok"])
        self.assertEqual(24, payload["window_hours"])
        self.assertEqual(2, payload["history"]["requests"])
        self.assertGreaterEqual(payload["runtime"]["requests_total"], 1)
        self.assertIsInstance(payload["runtime"]["request_timeouts"], int)
        self.assertLessEqual(payload["runtime"]["tracked_paths"], app.MAX_RUNTIME_METRIC_PATHS)
        self.assertEqual(app.MAX_RUNTIME_METRIC_PATHS, payload["runtime"]["max_paths"])
        self.assertIsInstance(payload["runtime"]["path_overflow_total"], int)
        self.assertTrue(any(item["path"] == "/api/hello" for item in payload["runtime"]["top_paths"]))
        self.assertEqual(app.AUTO_DELETE_MAX_PENDING, payload["runtime"]["auto_delete"]["max_pending"])
        self.assertEqual(
            app.PENDING_DELETE_MAX_RECORDS,
            payload["runtime"]["auto_delete"]["journal_max_records"],
        )
        self.assertEqual(0, payload["runtime"]["auto_delete"]["journal_chat_pending"])
        self.assertEqual(0, payload["runtime"]["auto_delete"]["journal_file_pending"])
        self.assertEqual(
            app.CAPTCHA_WORKER_MAX_PENDING,
            payload["runtime"]["captcha_worker"]["max_pending"],
        )
        self.assertEqual(app.MAX_HTTP_HANDLER_THREADS, payload["runtime"]["http_handlers"]["max_active"])
        self.assertIsInstance(payload["runtime"]["http_handlers"]["rejected_total"], int)
        self.assertEqual(
            app.MAX_ACTIVE_CHAT_FILE_UPLOADS,
            payload["runtime"]["upload_slots"]["file"]["max_active"],
        )
        self.assertEqual(
            app.MAX_ACTIVE_HAR_UPLOADS,
            payload["runtime"]["upload_slots"]["har"]["max_active"],
        )
        self.assertEqual(
            app.MAX_UPSTREAM_JSON_RESPONSE_BYTES,
            payload["runtime"]["upstream_responses"]["json_max_bytes"],
        )
        self.assertIsInstance(payload["runtime"]["upstream_responses"]["rejected_total"], int)
        self.assertEqual(
            app.MAX_UPSTREAM_STREAM_OUTPUT_BYTES,
            payload["runtime"]["upstream_responses"]["stream_output_max_bytes"],
        )
        self.assertEqual(
            app.MAX_UPSTREAM_STREAM_WIRE_BYTES,
            payload["runtime"]["upstream_responses"]["stream_wire_max_bytes"],
        )
        self.assertIsInstance(payload["runtime"]["upstream_responses"]["stream_rejected_total"], int)
        self.assertIsInstance(payload["runtime"]["upstream_responses"]["stream_incomplete_total"], int)
        self.assertEqual(app.UPSTREAM_READER_QUEUE_SIZE, payload["runtime"]["upstream_readers"]["queue_size"])
        self.assertIsInstance(payload["runtime"]["upstream_readers"]["heartbeats_total"], int)
        self.assertEqual(
            app.SSE_KEEPALIVE_INTERVAL_SECONDS,
            payload["runtime"]["sse_heartbeat"]["interval_seconds"],
        )
        self.assertIsInstance(payload["runtime"]["sse_heartbeat"]["sent_total"], int)
        self.assertEqual(
            app.CONTEXT_FILE_CACHE_MAX_ITEMS,
            payload["runtime"]["context_cache"]["max_items"],
        )
        self.assertEqual(
            app.CONTEXT_UPLOAD_STATE_MAX_ITEMS,
            payload["runtime"]["context_cache"]["max_state_items"],
        )
        self.assertEqual(
            app.LOG_MAX_BYTES * (app.LOG_BACKUP_COUNT + 1),
            payload["logs"]["store"]["max_total_bytes"],
        )
        self.assertEqual(app.LOG_RECORD_MAX_CHARS, payload["logs"]["max_record_chars"])
        self.assertIsInstance(payload["logs"]["truncated_total"], int)

    def test_log_events_do_not_emit_raw_account_or_chat_identifiers(self) -> None:
        tree = ast.parse((PROJECT_ROOT / "glm2api.py").read_text(encoding="utf-8"))
        forbidden = {"chat_id", "user_id", "token", "api_key", "captcha_verify_param"}
        violations: list[tuple[int, str]] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name) or node.func.id != "log_event":
                continue
            for keyword in node.keywords:
                if keyword.arg in forbidden:
                    violations.append((node.lineno, keyword.arg))
        self.assertEqual([], violations)

    def test_log_redaction_covers_structured_values_bearer_tokens_and_tracebacks(self) -> None:
        jwt = "eyJheader123." + "payload456.signature789"
        provider_key = "ghp_" + "abcdefghijklmnopqrstuvwxyz123456"
        windows_path = "C:" + "\\Users\\log-user\\private.log"
        source = (
            f'Authorization: Bearer short-secret token={jwt} '
            f'{{"api_key":"panel-secret","captcha_verify_param":"captcha-secret"}} {provider_key} '
            f'at \'{windows_path}\' https://example.test/failure?token=query-secret'
        )
        redacted = app.redact_log_text(source)
        for secret in ("short-secret", jwt, "panel-secret", "captcha-secret", provider_key):
            self.assertNotIn(secret, redacted)
        self.assertIn("<redacted>", redacted)

        payload = app.sanitize_log_value(
            {
                "token": "raw-token",
                "nested": {"current_key": "raw-current", "error": source},
                "token_fp": "safe-fingerprint",
            }
        )
        self.assertEqual("<redacted>", payload["token"])
        self.assertEqual("<redacted>", payload["nested"]["current_key"])
        self.assertEqual("safe-fingerprint", payload["token_fp"])
        self.assertNotIn("panel-secret", payload["nested"]["error"])
        self.assertNotIn("log-user", payload["nested"]["error"])
        self.assertNotIn("query-secret", payload["nested"]["error"])

        try:
            raise RuntimeError(
                f"upstream failed token={jwt} at '{windows_path}' "
                "https://example.test/failure?token=query-secret"
            )
        except RuntimeError:
            record = logging.LogRecord(
                "redaction-test",
                logging.ERROR,
                __file__,
                1,
                f"request failed with Bearer {provider_key}",
                (),
                sys.exc_info(),
            )
        line = app.RedactingFormatter("%(message)s").format(record)
        self.assertNotIn(jwt, line)
        self.assertNotIn(provider_key, line)
        self.assertNotIn("log-user", line)
        self.assertNotIn("query-secret", line)
        self.assertIn("<redacted", line)

        app.setup_logging("INFO", console=False)
        self.assertTrue(
            all(isinstance(handler.formatter, app.RedactingFormatter) for handler in app.LOG.handlers)
        )
        app.log_event(
            "redaction_probe",
            error=source,
            nested={"authorization": f"Bearer {provider_key}"},
        )
        entries, matched, _cursor = app.log_ring().query(limit=5, state="redaction_probe")
        self.assertGreaterEqual(matched, 1)
        latest = entries[-1]
        self.assertNotIn(jwt, latest["message"])
        self.assertNotIn(provider_key, latest["message"])
        self.assertNotIn(provider_key, latest["line"])
        self.assertNotIn("log-user", latest["line"])
        self.assertNotIn("query-secret", latest["line"])
        structured = json.loads(latest["message"])
        self.assertEqual("<redacted>", structured["nested"]["authorization"])

    def test_client_error_message_redacts_credentials_queries_paths_and_controls(self) -> None:
        provider_key = "ghp_" + ("c" * 24)
        windows_path = "D:" + "\\Users\\alice\\private file.txt"
        raw = (
            f"token={provider_key}\n"
            "Cookie: session=super-secret\n"
            f"open '{windows_path}' failed\n"
            "GET https://example.test/private?signature=abc&token=query-secret failed\t"
        )
        cleaned = app.client_error_message(raw)
        self.assertNotIn(provider_key, cleaned)
        self.assertNotIn("super-secret", cleaned)
        self.assertNotIn("alice", cleaned)
        self.assertNotIn("signature=abc", cleaned)
        self.assertNotIn("query-secret", cleaned)
        self.assertNotRegex(cleaned, r"[\r\n\t]")
        self.assertIn("<redacted", cleaned)

        long_error = "x" * (app.CLIENT_ERROR_MAX_CHARS + 200)
        truncated = app.client_error_message(long_error)
        self.assertLessEqual(len(truncated), app.CLIENT_ERROR_MAX_CHARS)
        self.assertTrue(truncated.endswith("[error truncated]"))

    def test_json_error_boundary_redacts_exception_details(self) -> None:
        provider_key = "ghp_" + ("d" * 24)
        windows_path = "C:" + "\\Users\\bob\\secret.txt"
        leaked = f"delete failed token={provider_key} at '{windows_path}'"
        original_delete = app.delete_zai_chat

        def failing_delete(*_args, **_kwargs):
            raise RuntimeError(leaked)

        app.delete_zai_chat = failing_delete
        try:
            status, raw = self.request(
                "POST",
                "/api/chat/delete",
                {"chat_id": "00000000-0000-0000-0000-000000000081"},
            )
        finally:
            app.delete_zai_chat = original_delete
        self.assertEqual(400, status)
        self.assertNotIn(provider_key, raw)
        self.assertNotIn("bob", raw)
        self.assertIn("redacted", raw)

    def test_all_streaming_error_boundaries_redact_exception_details(self) -> None:
        provider_key = "ghp_" + ("e" * 24)
        windows_path = "C:" + "\\Users\\carol\\stream.txt"
        leaked = f"stream failed token={provider_key} at '{windows_path}'"
        original_stream = app.stream_zai_completion

        def failing_stream(*_args, **_kwargs):
            if False:
                yield ""
            raise RuntimeError(leaked)

        app.stream_zai_completion = failing_stream
        try:
            requests = [
                (
                    "/v1/chat/completions",
                    {"model": "glm-5.2", "messages": [{"role": "user", "content": "hi"}], "stream": True},
                ),
                ("/v1/responses", {"model": "glm-5.2", "input": "hi", "stream": True}),
                (
                    "/v1/messages",
                    {
                        "model": "glm-5.2",
                        "max_tokens": 32,
                        "messages": [{"role": "user", "content": "hi"}],
                        "stream": True,
                    },
                ),
                ("/api/chat", {"model": "glm-5.2", "message": "hi", "stream": True}),
            ]
            results = [self.request("POST", path, body) for path, body in requests]
        finally:
            app.stream_zai_completion = original_stream
        for status, raw in results:
            self.assertEqual(200, status, raw[:500])
            self.assertNotIn(provider_key, raw)
            self.assertNotIn("carol", raw)
            self.assertIn("redacted", raw)

    def test_success_status_payloads_redact_local_store_errors(self) -> None:
        provider_key = "ghp_" + ("f" * 24)
        windows_path = "C:" + "\\Users\\dora\\state.json"
        leaked = f"store token={provider_key} path '{windows_path}'"
        previous = (
            QuietProxyHandler.profile_store_error,
            QuietProxyHandler.settings_error,
            QuietProxyHandler.api_key_store_error,
            QuietProxyHandler.browser_login_progress,
        )
        try:
            QuietProxyHandler.profile_store_error = leaked
            QuietProxyHandler.settings_error = leaked
            QuietProxyHandler.api_key_store_error = leaked
            QuietProxyHandler.browser_login_progress = {
                "running": False,
                "mode": "",
                "stage": "失败",
                "updated_at": "",
                "error": leaked,
            }
            responses = [
                self.request("GET", "/api/status"),
                self.request("GET", "/api/settings"),
                self.request("GET", "/api/settings/api-key"),
                self.request("GET", "/api/auth/profiles"),
                self.request("GET", "/api/auth/browser-login/status"),
            ]
        finally:
            (
                QuietProxyHandler.profile_store_error,
                QuietProxyHandler.settings_error,
                QuietProxyHandler.api_key_store_error,
                QuietProxyHandler.browser_login_progress,
            ) = previous
        for status, raw in responses:
            self.assertEqual(200, status, raw[:500])
            self.assertNotIn(provider_key, raw)
            self.assertNotIn("dora", raw)
            self.assertIn("redacted", raw)

    def test_success_model_content_is_not_modified_by_error_sanitizer(self) -> None:
        original_stream = app.stream_zai_completion
        model_text = "示例路径 C:" + "\\Users\\example\\project 与 token=not-a-secret"

        def content_stream(*_args, **_kwargs):
            yield "data: " + json.dumps(
                {"data": {"delta_content": model_text, "phase": "answer"}},
                ensure_ascii=False,
            )

        app.stream_zai_completion = content_stream
        try:
            status, raw = self.request(
                "POST",
                "/v1/chat/completions",
                {"model": "glm-5.2", "messages": [{"role": "user", "content": "show example"}]},
            )
        finally:
            app.stream_zai_completion = original_stream
        self.assertEqual(200, status, raw[:500])
        self.assertEqual(model_text, json.loads(raw)["choices"][0]["message"]["content"])

    def test_access_log_target_drops_queries_and_normalizes_dynamic_ids(self) -> None:
        self.assertEqual(
            "/api/logs",
            app.safe_access_log_target("/api/logs?token=secret&text=private-prompt"),
        )
        self.assertEqual(
            "/v1/responses/:id",
            app.safe_access_log_target("/v1/responses/resp_sensitive?api_key=secret"),
        )
        self.assertNotIn("\n", app.safe_access_log_target("/api/test\nforged?token=secret"))

    def test_request_id_scoped_to_dispatch(self) -> None:
        app.setup_logging("INFO", console=False)
        self.assertEqual("", app.current_request_id())
        self.request("GET", "/api/status")
        self.assertEqual("", app.current_request_id())
        rid_holder: list[str] = []

        def probe(*_args, **_kwargs):
            rid_holder.append(app.current_request_id())
            return True

        original_delete = app.delete_zai_chat
        app.delete_zai_chat = probe
        try:
            chat_id = "00000000-0000-0000-0000-000000000001"
            status, _raw = self.request("POST", "/api/chat/delete", {"chat_id": chat_id})
            self.assertEqual(200, status)
        finally:
            app.delete_zai_chat = original_delete
        self.assertEqual(1, len(rid_holder))
        self.assertTrue(rid_holder[0], "handler 线程内应能取到本次请求的 rid")

    def test_transient_upstream_error_classification(self) -> None:
        busy = "MODEL_CONCURRENCY_LIMIT: 当前模型使用人数较多，请稍后再试或切换到其他模型。"
        self.assertTrue(app.is_transient_upstream_error(busy))
        self.assertTrue(app.is_transient_upstream_error("HTTP Error 429: too many requests"))
        self.assertTrue(app.is_transient_upstream_error("HTTP Error 502: bad gateway"))
        self.assertTrue(app.is_transient_upstream_error("上游中断"))
        self.assertFalse(app.is_transient_upstream_error("AUTH_REQUIRED: unauthorized"))
        self.assertFalse(app.is_transient_upstream_error("FRONTEND_CAPTCHA_REQUIRED: missing captcha"))
        self.assertFalse(app.is_transient_upstream_error("上游未按 tool_choice 输出工具调用: get_weather"))
        self.assertFalse(app.is_transient_upstream_error(""))
        before_delta = RuntimeError("上游中断")
        self.assertTrue(app.is_retryable_protocol_exception(before_delta))
        before_delta.protocol_content_emitted = True
        self.assertFalse(app.is_retryable_protocol_exception(before_delta))

    def test_stream_retries_upstream_interrupted_during_new_chat(self) -> None:
        state = fake_state()
        new_chat_calls = 0

        def flaky_new_chat(_state, _prompt, options=None):
            nonlocal new_chat_calls
            new_chat_calls += 1
            if new_chat_calls == 1:
                raise RuntimeError("创建 chat 失败: 上游中断")
            return "00000000-0000-0000-0000-000000000099", "user-message-2"

        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def __iter__(self):
                return iter(
                    [
                        b'data: {"data":{"delta_content":"RECOVERED","phase":"answer"}}\n\n',
                        b'data: {"data":"[DONE]"}\n\n',
                    ]
                )

        real = (app.new_chat, app.urlopen)
        app.new_chat = flaky_new_chat
        app.urlopen = lambda _request, timeout=None: FakeResp()
        try:
            events = list(
                self.original_stream(
                    state,
                    "hi",
                    options=app.ChatOptions(model="glm-5.2"),
                    retry_wait_sec=0,
                    retry_attempts=3,
                )
            )
        finally:
            app.new_chat, app.urlopen = real
        self.assertEqual(2, new_chat_calls)
        self.assertIn("RECOVERED", "".join(events))

    def test_stream_retries_transient_busy_before_first_delta(self) -> None:
        state = fake_state()
        new_chat_calls: list[str] = []
        deleted: list[str] = []

        def fake_new_chat(_state, _prompt, options=None):
            index = len(new_chat_calls)
            chat_id = f"00000000-0000-0000-0000-{index:012d}"
            user_msg = f"11111111-0000-0000-0000-{index:012d}"
            new_chat_calls.append(chat_id)
            return chat_id, user_msg

        def fake_delete(_state, chat_id, **_kwargs):
            deleted.append(chat_id)
            return True

        busy_event = (
            'data: {"data":{"content":"","done":true,"error":{"code":"MODEL_CONCURRENCY_LIMIT",'
            '"detail":"当前模型使用人数较多，请稍后再试或切换到其他模型。","model_id":"GLM-5-Turbo"}},'
            '"type":"chat:completion"}\n\n'
        )

        class FakeResp:
            def __init__(self, chunks: list[bytes]):
                self._chunks = chunks

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def __iter__(self):
                return iter(self._chunks)

        attempts: list[list[bytes]] = [
            [busy_event.encode("utf-8"), b'data: {"data":"[DONE]"}\n\n'],
            [
                b'data: {"data":{"delta_content":"HELLO","phase":"answer"}}\n\n',
                b'data: {"data":"[DONE]"}\n\n',
            ],
        ]
        urlopen_calls: list[int] = []

        def fake_urlopen(_req, timeout=None):
            urlopen_calls.append(len(urlopen_calls))
            return FakeResp(attempts[len(urlopen_calls) - 1])

        real = (app.new_chat, app.delete_zai_chat, app.urlopen)
        app.new_chat = fake_new_chat
        app.delete_zai_chat = fake_delete
        app.urlopen = fake_urlopen
        try:
            context = {}
            events = list(
                self.original_stream(
                    state,
                    "hi",
                    options=app.ChatOptions(model="glm-5.2"),
                    context_out=context,
                    retry_wait_sec=0,
                    retry_attempts=3,
                )
            )
        finally:
            app.new_chat, app.delete_zai_chat, app.urlopen = real
        self.assertEqual(2, len(new_chat_calls), "繁忙后应换新会话重发一次")
        self.assertEqual([new_chat_calls[0]], deleted, "被繁忙中断的空会话应被清理")
        joined = "".join(events)
        self.assertNotIn("MODEL_CONCURRENCY_LIMIT", joined, "繁忙错误不应透传")
        self.assertIn("HELLO", joined)
        self.assertEqual(new_chat_calls[1], context["chat_id"], "context_out 应指向最终 attempt 的会话")

    def test_staged_final_wave_retries_same_parent_with_fresh_files(self) -> None:
        state = fake_state()
        chat_id = "00000000-0000-0000-0000-000000000410"
        parent_id = "00000000-0000-0000-0000-000000000411"
        payloads: list[dict[str, object]] = []
        retry_uploads = 0
        busy_event = (
            'data: {"data":{"content":"","done":true,"error":{"code":"MODEL_CONCURRENCY_LIMIT",'
            '"detail":"当前模型使用人数较多，请稍后再试或切换到其他模型。"}},'
            '"type":"chat:completion"}\n\n'
        )

        class FakeResp:
            def __init__(self, chunks):
                self.chunks = chunks

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def __iter__(self):
                return iter(self.chunks)

            def close(self):
                pass

        responses = [
            FakeResp([busy_event.encode("utf-8")]),
            FakeResp(
                [
                    b'data: {"data":{"delta_content":"OK","phase":"answer"}}\n\n',
                    b'data: {"data":"[DONE]"}\n\n',
                ]
            ),
        ]

        def fake_urlopen(request, timeout=None):
            payloads.append(json.loads(request.data.decode("utf-8")))
            return responses[len(payloads) - 1]

        def fresh_files():
            nonlocal retry_uploads
            retry_uploads += 1
            return [{"id": f"fresh-file-{retry_uploads}", "filename": "retry.txt"}]

        original_urlopen = app.urlopen
        app.urlopen = fake_urlopen
        try:
            context: dict[str, object] = {}
            events = list(
                self.original_stream(
                    state,
                    "continue loaded context",
                    create_chat=False,
                    chat_id=chat_id,
                    parent_message_id=parent_id,
                    options=app.ChatOptions(model="glm-5.3"),
                    context_out=context,
                    files=[{"id": "initial-file", "filename": "initial.txt"}],
                    retry_wait_sec=0,
                    retry_attempts=2,
                    retry_reused_chat=True,
                    retry_files_factory=fresh_files,
                )
            )
        finally:
            app.urlopen = original_urlopen

        self.assertEqual(2, len(payloads))
        self.assertEqual(1, retry_uploads)
        self.assertEqual([chat_id, chat_id], [payload["chat_id"] for payload in payloads])
        self.assertEqual([parent_id, parent_id], [payload["current_user_message_parent_id"] for payload in payloads])
        self.assertNotEqual(payloads[0]["current_user_message_id"], payloads[1]["current_user_message_id"])
        self.assertNotEqual(payloads[0]["id"], payloads[1]["id"])
        self.assertEqual("initial-file", payloads[0]["files"][0]["id"])
        self.assertEqual("fresh-file-1", payloads[1]["files"][0]["id"])
        self.assertEqual([], self.deleted_chats, "同一 chat 的第二波繁忙重试不能删除承载前文的会话")
        self.assertIn("OK", "".join(events))

    def test_upstream_sse_accepts_crlf_and_event_fields(self) -> None:
        state = fake_state()

        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def __iter__(self):
                return iter(
                    [
                        b'event: message\r\ndata: {"data":{"delta_content":"OK","phase":"answer"}}\r\n\r\n',
                        b'data: {"data":"[DONE]"}\r\n\r\n',
                    ]
                )

        real = (app.new_chat, app.urlopen)
        app.new_chat = lambda *_a, **_k: ("00000000-0000-0000-0000-000000000088", "u1")
        app.urlopen = lambda *_a, **_k: FakeResp()
        try:
            events = list(
                self.original_stream(
                    state,
                    "hi",
                    options=app.ChatOptions(model="glm-5.2"),
                    retry_wait_sec=0,
                    retry_attempts=1,
                )
            )
        finally:
            app.new_chat, app.urlopen = real
        self.assertEqual("OK", "".join(app.extract_delta_from_event(event)[0] for event in events))
        self.assertEqual("OK", app.extract_delta_from_event(events[0])[0])

    def test_upstream_terminal_event_detection_matches_captured_shapes(self) -> None:
        self.assertTrue(app.is_upstream_terminal_event("data: [DONE]"))
        self.assertTrue(app.is_upstream_terminal_event('data: {"data":"[DONE]"}'))
        self.assertTrue(
            app.is_upstream_terminal_event(
                'data: {"type":"chat:completion","data":{"done":true,"phase":"answer"}}'
            )
        )
        self.assertTrue(app.is_upstream_terminal_event('data: {"done":true}'))
        self.assertFalse(
            app.is_upstream_terminal_event(
                'data: {"type":"chat:completion","data":{"delta_content":"partial","phase":"answer"}}'
            )
        )

    def test_stream_retries_eof_before_terminal_when_no_content_was_emitted(self) -> None:
        state = fake_state()
        chats: list[str] = []
        deleted: list[str] = []
        attempts = [
            [],
            [
                b'data: {"data":{"delta_content":"RECOVERED","phase":"answer"}}\n\n',
                b'data: {"data":{"done":true,"phase":"answer"},"type":"chat:completion"}\n\n',
            ],
        ]

        class FakeResp:
            def __init__(self, chunks: list[bytes]):
                self.chunks = chunks

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def __iter__(self):
                return iter(self.chunks)

        def fake_new_chat(_state, _prompt, options=None):
            index = len(chats)
            chat_id = f"00000000-0000-0000-0000-{index:012d}"
            chats.append(chat_id)
            return chat_id, f"user-{index}"

        before = app.upstream_response_status()["stream_incomplete_total"]
        real = (app.new_chat, app.delete_zai_chat, app.urlopen)
        app.new_chat = fake_new_chat
        app.delete_zai_chat = lambda _state, chat_id, **_kwargs: deleted.append(chat_id) or True
        app.urlopen = lambda _request, timeout=None: FakeResp(attempts[len(chats) - 1])
        try:
            events = list(
                self.original_stream(
                    state,
                    "retry incomplete",
                    options=app.ChatOptions(model="glm-5.2"),
                    retry_wait_sec=0,
                    retry_attempts=2,
                )
            )
        finally:
            app.new_chat, app.delete_zai_chat, app.urlopen = real
        self.assertEqual(2, len(chats))
        self.assertEqual([chats[0]], deleted)
        self.assertIn("RECOVERED", "".join(events))
        self.assertEqual(before + 1, app.upstream_response_status()["stream_incomplete_total"])

    def test_stream_eof_after_partial_is_error_and_marks_cleanup_context(self) -> None:
        state = fake_state()

        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def __iter__(self):
                return iter([b'data: {"data":{"delta_content":"PARTIAL","phase":"answer"}}\n\n'])

        context: dict[str, object] = {}
        real = (app.new_chat, app.urlopen)
        app.new_chat = lambda *_args, **_kwargs: (
            "00000000-0000-0000-0000-000000000077",
            "user-partial",
        )
        app.urlopen = lambda *_args, **_kwargs: FakeResp()
        try:
            with self.assertRaises(app.UpstreamStreamIncomplete) as raised:
                list(
                    self.original_stream(
                        state,
                        "partial eof",
                        options=app.ChatOptions(model="glm-5.2", delete_chat_after_completion=False),
                        context_out=context,
                        retry_attempts=1,
                        history_ctx={
                            "surface": "openai_chat",
                            "stream": True,
                            "user_input": "partial eof",
                            "messages": [{"role": "user", "content": "partial eof"}],
                        },
                    )
                )
        finally:
            app.new_chat, app.urlopen = real
        self.assertTrue(context["_stream_incomplete"])
        self.assertTrue(raised.exception.protocol_content_emitted)
        record = app.local_history_records()[0]
        self.assertEqual("error", record["status"])
        self.assertEqual("PARTIAL", record["content"])
        self.assertIn("完成标记", record["error"])

    def test_protocol_partial_eof_returns_502_and_forces_chat_cleanup(self) -> None:
        original_stream = app.stream_zai_completion
        chat_id = "00000000-0000-0000-0000-000000000078"

        def incomplete_stream(_state, _prompt, **kwargs):
            context = kwargs.get("context_out")
            if isinstance(context, dict):
                context.update({"chat_id": chat_id, "_stream_incomplete": True})
            yield 'data: {"data":{"delta_content":"PARTIAL","phase":"answer"}}'
            exc = app.UpstreamStreamIncomplete("上游中断：SSE 在完成标记前结束")
            exc.protocol_content_emitted = True
            raise exc

        app.stream_zai_completion = incomplete_stream
        try:
            status, raw = self.request(
                "POST",
                "/v1/chat/completions",
                {
                    "model": "glm-5.2",
                    "messages": [{"role": "user", "content": "partial eof"}],
                    "stream": False,
                    "delete_chat_after_completion": False,
                },
            )
        finally:
            app.stream_zai_completion = original_stream
        self.assertEqual(502, status, raw)
        self.assertIn("完成标记", raw)
        self.assertEqual([chat_id], self.deleted_chats)

    def test_upstream_silence_emits_heartbeat_then_resumes(self) -> None:
        state = fake_state()
        started = threading.Event()
        release = threading.Event()
        reader_threads: list[str] = []

        class BlockingResp:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                release.set()
                return False

            def __iter__(self):
                reader_threads.append(threading.current_thread().name)
                started.set()
                release.wait(timeout=2)
                yield b'data: {"data":{"delta_content":"RESUMED","phase":"answer"}}\n\n'
                yield b'data: {"data":{"done":true,"phase":"answer"},"type":"chat:completion"}\n\n'

            def close(self):
                release.set()

        real = (app.new_chat, app.urlopen, app.SSE_KEEPALIVE_INTERVAL_SECONDS)
        app.new_chat = lambda *_a, **_k: ("00000000-0000-0000-0000-000000000087", "u1")
        app.urlopen = lambda *_a, **_k: BlockingResp()
        app.SSE_KEEPALIVE_INTERVAL_SECONDS = 0.02
        events = None
        try:
            events = self.original_stream(
                state,
                "hi",
                options=app.ChatOptions(model="glm-5.2"),
                retry_wait_sec=0,
                retry_attempts=1,
            )
            first = next(events)
            self.assertTrue(started.is_set())
            self.assertEqual(app.UPSTREAM_IDLE_HEARTBEAT_EVENT, first)
            self.assertTrue(app.is_sse_comment_event(first))
            release.set()
            remaining = list(events)
        finally:
            release.set()
            if events is not None:
                events.close()
            app.new_chat, app.urlopen, app.SSE_KEEPALIVE_INTERVAL_SECONDS = real
        self.assertEqual(["upstream-sse-reader"], reader_threads)
        self.assertIn("RESUMED", "".join(remaining))

    def test_closing_heartbeat_iterator_unblocks_upstream_reader(self) -> None:
        released = threading.Event()
        finished = threading.Event()

        class BlockingResp:
            def __iter__(self):
                try:
                    released.wait(timeout=2)
                    if not released.is_set():
                        raise TimeoutError("test reader was not released")
                finally:
                    finished.set()
                return
                yield b""  # pragma: no cover - keeps this method an iterator

            def close(self):
                released.set()

        chunks = app.iter_upstream_chunks_with_heartbeat(BlockingResp(), 0.02)
        self.assertIsNone(next(chunks))
        chunks.close()
        self.assertTrue(released.is_set())
        self.assertTrue(finished.wait(timeout=0.5))

    def test_upstream_reader_propagates_transport_error_and_counts_it(self) -> None:
        before = app.upstream_reader_status()["errors_total"]

        class ErrorResp:
            def __iter__(self):
                raise OSError("simulated upstream read failure")
                yield b""  # pragma: no cover - keeps this method an iterator

        with self.assertRaisesRegex(OSError, "simulated upstream read failure"):
            list(app.iter_upstream_chunks_with_heartbeat(ErrorResp(), 0.02))
        status = app.upstream_reader_status()
        self.assertEqual(before + 1, status["errors_total"])
        self.assertEqual(0, status["active"])

    def test_auto_visible_tool_free_decision_does_not_retry_hidden_call(self) -> None:
        current_stream = app.stream_zai_completion
        prompts: list[str] = []

        def deciding_stream(_state, prompt, **kwargs):
            prompts.append(prompt)
            context = kwargs.get("context_out")
            if isinstance(context, dict):
                context["chat_id"] = f"00000000-0000-0000-0000-{len(prompts):012d}"
            if len(prompts) == 1:
                hidden_call = (
                    "```xml\n<|DSML|tool_calls><|DSML|invoke name=\"get_weather\">"
                    "<|DSML|parameter name=\"city\">北京</|DSML|parameter>"
                    "</|DSML|invoke></|DSML|tool_calls>\n```"
                )
                yield "data: " + json.dumps(
                    {"data": {"delta_content": hidden_call, "phase": "thinking"}}, ensure_ascii=False
                )
                yield 'data: {"data":{"delta_content":"接下来我会查询天气。","phase":"answer"}}'
                return
            yield 'data: {"data":{"delta_content":"现有信息已经足够，完整答案如下。","phase":"answer"}}'

        app.stream_zai_completion = deciding_stream
        try:
            status, raw = self.request(
                "POST",
                "/v1/chat/completions",
                {
                    "model": "glm-5.3",
                    "messages": [{"role": "user", "content": "是否需要查询天气由你判断"}],
                    "tools": [
                        {
                            "type": "function",
                            "function": {"name": "get_weather", "parameters": {"type": "object"}},
                        }
                    ],
                    "tool_choice": "auto",
                    "stream": False,
                },
            )
        finally:
            app.stream_zai_completion = current_stream
        self.assertEqual(200, status, raw)
        self.assertEqual(1, len(prompts))
        self.assertNotIn("tool_calls", json.loads(raw)["choices"][0]["message"])
        self.assertEqual("接下来我会查询天气。", json.loads(raw)["choices"][0]["message"]["content"])
        self.assertEqual(1, len(self.deleted_chats))

    def test_stream_tool_retry_hides_failed_attempt_and_sends_keepalive(self) -> None:
        current_stream = app.stream_zai_completion
        original_interval = app.SSE_KEEPALIVE_INTERVAL_SECONDS
        original_new_chat = app.new_chat
        original_urlopen = app.urlopen
        attempts = {"count": 0, "chats": 0}

        class FakeResp:
            def __init__(self, chunks):
                self.chunks = chunks

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def __iter__(self):
                return iter(self.chunks)

        committed_thinking = "GOOD_THINKING\nI decided that this tool call is needed now."
        visible_call = (
            "接下来我会查询天气。\n"
            '<|DSML|tool_calls><|DSML|invoke name="get_weather">'
            '<|DSML|parameter name="city">北京</|DSML|parameter>'
            '</|DSML|invoke></|DSML|tool_calls>'
        )
        upstream_chunks = [
            [
                b'data: {"data":{"delta_content":"FAILED_THINKING","phase":"thinking"}}\n\n',
                b'data: {"data":{"delta_content":"<tool_calls><invoke name=bad>","phase":"answer"}}\n\n',
                b'data: {"data":{"done":true,"phase":"answer"},"type":"chat:completion"}\n\n',
            ],
            [
                ("data: " + json.dumps({"data": {"delta_content": committed_thinking, "phase": "thinking"}}, ensure_ascii=False) + "\n\n").encode("utf-8"),
                ("data: " + json.dumps({"data": {"delta_content": visible_call, "phase": "answer"}}, ensure_ascii=False) + "\n\n").encode("utf-8"),
                b'data: {"data":{"done":true,"phase":"answer"},"type":"chat:completion"}\n\n',
            ],
        ]

        def fake_new_chat(_state, _prompt, options=None):
            attempts["chats"] += 1
            return f"00000000-0000-0000-0000-{attempts['chats']:012d}", "u1"

        def fake_urlopen(_request, timeout=None):
            index = attempts["count"]
            attempts["count"] += 1
            return FakeResp(upstream_chunks[index])

        app.stream_zai_completion = self.original_stream
        app.new_chat = fake_new_chat
        app.urlopen = fake_urlopen
        app.SSE_KEEPALIVE_INTERVAL_SECONDS = 0
        try:
            status, raw = self.request(
                "POST",
                "/v1/chat/completions",
                {
                    "model": "glm-5.2",
                    "messages": [{"role": "user", "content": "天气"}],
                    "tools": [
                        {
                            "type": "function",
                            "function": {"name": "get_weather", "parameters": {"type": "object"}},
                        }
                    ],
                    "tool_choice": "auto",
                    "include_thinking": True,
                    "stream": True,
                },
            )
        finally:
            app.stream_zai_completion = current_stream
            app.new_chat = original_new_chat
            app.urlopen = original_urlopen
            app.SSE_KEEPALIVE_INTERVAL_SECONDS = original_interval
        self.assertEqual(200, status, raw[:500])
        self.assertEqual(2, attempts["count"])
        self.assertIn(": keep-alive", raw)
        self.assertNotIn("FAILED_THINKING", raw)
        self.assertIn("GOOD_THINKING", raw)
        self.assertIn('"tool_calls"', raw)
        self.assertIn("接下来我会查询天气", raw)
        self.assertNotIn("<|DSML|tool_calls>", raw)
        self.assertEqual(2, len(self.deleted_chats), "失败首轮与成功重试会话都应清理")
        records = app.local_history_records()
        self.assertEqual(1, len(records), "一次客户端请求的格式纠错不应产生两条历史记录")
        detail = app.get_local_history_record(str(records[0]["id"])) or {}
        self.assertEqual("success", detail["status"])
        self.assertEqual("tool_calls", detail["finish_reason"])
        self.assertEqual(1, detail["tool_calls_count"])
        self.assertEqual(["get_weather"], detail["tool_call_names"])
        self.assertEqual("output", detail["tool_calls_source"])
        self.assertEqual(1, detail["tool_retry_count"])
        self.assertEqual("", detail["tool_retry_error"])
        self.assertIn("Tool-call correction:", str(detail.get("final_prompt") or ""))

    def test_exhausted_tool_format_retry_marks_history_error(self) -> None:
        request = app.normalize_openai_chat_request(
            {
                "model": "glm-5.3",
                "messages": [{"role": "user", "content": "检查文件"}],
                "tools": [
                    {
                        "type": "function",
                        "function": {"name": "Read", "parameters": {"type": "object"}},
                    }
                ],
            },
            True,
        )
        record_id = app.start_history_record(
            surface=request.surface,
            model=request.options.model,
            stream=False,
            user_input="检查文件",
            messages=request.messages,
            final_prompt=request.execution_prompt,
        )
        handler = QuietProxyHandler.__new__(QuietProxyHandler)
        first_context = {
            "_history_record_id": record_id,
            "chat_id": "00000000-0000-0000-0000-0000000000d1",
        }
        malformed = '<tool_calls><invoke name="Read"><parameter name="file_path">'
        released = False

        def release_initial_output():
            nonlocal released
            released = True

        def regenerate(retry_request):
            self.assertTrue(released, "第二轮开始前必须先释放首轮输出缓冲")
            app.restart_history_record(record_id, retry_request.execution_prompt)
            return (
                malformed,
                "",
                {
                    "_history_record_id": record_id,
                    "chat_id": "00000000-0000-0000-0000-0000000000d2",
                },
                fake_state(),
            )

        with self.assertRaises(app.ToolCallFormatError):
            handler._complete_turn_with_tool_retry(
                request,
                fake_state(),
                first_context,
                malformed,
                "",
                regenerate,
                release_initial_output,
            )
        self.assertTrue(released)
        detail = app.get_local_history_record(record_id) or {}
        self.assertEqual("error", detail["status"])
        self.assertEqual(500, detail["status_code"])
        self.assertEqual(1, detail["tool_retry_count"])
        self.assertIn("无法转换", detail["tool_retry_error"])
        self.assertEqual("", detail["finish_reason"])

    def test_stream_passes_through_non_transient_error(self) -> None:
        state = fake_state()

        class FakeResp:
            def __init__(self, chunks: list[bytes]):
                self._chunks = chunks

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def __iter__(self):
                return iter(self._chunks)

        auth_error = (
            'data: {"data":{"content":"","done":true,"error":{"code":"AUTH_REQUIRED",'
            '"detail":"unauthorized"}},"type":"chat:completion"}\n\n'
        )

        def fake_new_chat(_state, _prompt, options=None):
            return "00000000-0000-0000-0000-000000000009", "u1"

        def fake_urlopen(_req, timeout=None):
            return FakeResp([auth_error.encode("utf-8"), b'data: {"data":"[DONE]"}\n\n'])

        real = (app.new_chat, app.delete_zai_chat, app.urlopen)
        app.new_chat = fake_new_chat
        app.delete_zai_chat = lambda _s, _c, **_kwargs: True
        app.urlopen = fake_urlopen
        try:
            events = list(
                self.original_stream(
                    state,
                    "hi",
                    options=app.ChatOptions(model="glm-5.2"),
                    retry_wait_sec=0,
                    retry_attempts=3,
                )
            )
        finally:
            app.new_chat, app.delete_zai_chat, app.urlopen = real
        joined = "".join(events)
        self.assertIn("AUTH_REQUIRED", joined, "非瞬时错误必须原样透传")

    def test_retry_settings_defaults_and_clamps(self) -> None:
        defaults = app.local_settings_defaults()
        self.assertEqual(app.DEFAULT_UPSTREAM_RETRY_WAIT_SEC, defaults["upstream_retry_wait_sec"])
        self.assertEqual(app.DEFAULT_UPSTREAM_RETRY_ATTEMPTS, defaults["upstream_retry_max_attempts"])
        normalized = app.normalize_local_settings(
            {"upstream_retry_wait_sec": 999, "upstream_retry_max_attempts": 99}
        )
        self.assertEqual(120.0, normalized["upstream_retry_wait_sec"])
        self.assertEqual(6, normalized["upstream_retry_max_attempts"])
        normalized = app.normalize_local_settings(
            {"upstream_retry_wait_sec": "bad", "upstream_retry_max_attempts": None}
        )
        self.assertEqual(app.DEFAULT_UPSTREAM_RETRY_WAIT_SEC, normalized["upstream_retry_wait_sec"])
        self.assertEqual(app.DEFAULT_UPSTREAM_RETRY_ATTEMPTS, normalized["upstream_retry_max_attempts"])

    def test_fresh_captcha_pool_hit_skips_solver(self) -> None:
        state = fake_state()
        app._CAPTCHA_POOL.append(("pooled-captcha", time.monotonic()))
        original_mode = app._CAPTCHA_MODE
        real_solver = app.get_happydom_captcha
        try:
            app._CAPTCHA_MODE = "auto"
            app._set_captcha_degraded(1800)  # 冷却激活也不能影响池命中

            def _no_solve(*_a, **_k):
                raise AssertionError("pool hit must not invoke the solver")

            app.get_happydom_captcha = _no_solve
            self.assertEqual(
                "pooled-captcha", app.resolve_fresh_captcha(state, "glm-5.2", None, timeout_ms=1000)
            )
            self.assertEqual([], list(app._CAPTCHA_POOL), "池命中后应取走条目")
        finally:
            app._CAPTCHA_MODE = original_mode
            app.get_happydom_captcha = real_solver
            app._set_captcha_degraded(-3600)

    def test_happydom_cancellation_terminates_owned_node_process(self) -> None:
        original = (app.subprocess.Popen, app.shutil.which, app.happydom_captcha_available)
        created = []

        class FakeProcess:
            def __init__(self, *_args, **_kwargs):
                self.returncode = None
                self.terminated = False
                self.killed = False
                created.append(self)

            def poll(self):
                return self.returncode

            def communicate(self, timeout=None):
                if self.returncode is None:
                    raise app.subprocess.TimeoutExpired("node", timeout)
                return ("", "")

            def terminate(self):
                self.terminated = True
                self.returncode = -15

            def kill(self):
                self.killed = True
                self.returncode = -9

        def cancel():
            raise app.ServiceShuttingDown("test shutdown")

        try:
            app.subprocess.Popen = FakeProcess
            app.shutil.which = lambda _name: "node"
            app.happydom_captcha_available = lambda: True
            with self.assertRaises(app.ServiceShuttingDown):
                app.get_happydom_captcha(30_000, cancel_check=cancel)
            self.assertEqual(1, len(created))
            self.assertTrue(created[0].terminated or created[0].killed)
        finally:
            app.subprocess.Popen, app.shutil.which, app.happydom_captcha_available = original

    def test_har_worker_cancellation_terminates_owned_process(self) -> None:
        original = app.subprocess.Popen
        created = []
        checks = 0

        class FakeProcess:
            def __init__(self, *_args, **_kwargs):
                self.returncode = None
                self.terminated = False
                self.killed = False
                created.append(self)

            def poll(self):
                return self.returncode

            def communicate(self, timeout=None):
                if self.returncode is None:
                    raise app.subprocess.TimeoutExpired("har-worker", timeout)
                return ("", "")

            def terminate(self):
                self.terminated = True
                self.returncode = -15

            def kill(self):
                self.killed = True
                self.returncode = -9

        def cancel():
            nonlocal checks
            checks += 1
            if checks >= 2:
                raise app.ServiceShuttingDown("test shutdown")

        try:
            app.subprocess.Popen = FakeProcess
            with self.assertRaises(app.ServiceShuttingDown):
                app.extract_state_via_worker(Path("unused.har"), cancel_check=cancel)
            self.assertEqual(1, len(created))
            self.assertTrue(created[0].terminated or created[0].killed)
        finally:
            app.subprocess.Popen = original

    def test_har_worker_timeout_terminates_owned_process(self) -> None:
        original = app.subprocess.Popen
        created = []

        class FakeProcess:
            def __init__(self, *_args, **_kwargs):
                self.returncode = None
                self.terminated = False
                created.append(self)

            def poll(self):
                return self.returncode

            def communicate(self, timeout=None):
                if self.returncode is None:
                    raise app.subprocess.TimeoutExpired("har-worker", timeout)
                return ("", "")

            def terminate(self):
                self.terminated = True
                self.returncode = -15

            def kill(self):
                self.returncode = -9

        try:
            app.subprocess.Popen = FakeProcess
            with self.assertRaisesRegex(TimeoutError, "HAR 状态提取超时"):
                app.extract_state_via_worker(Path("unused.har"), timeout_sec=0)
            self.assertEqual(1, len(created))
            self.assertTrue(created[0].terminated)
        finally:
            app.subprocess.Popen = original

    def test_har_worker_real_success_path(self) -> None:
        har = {
            "log": {
                "entries": [
                    {
                        "request": {"url": "https://chat.z.ai/api/v1/auths/signin"},
                        "response": {
                            "status": 200,
                            "content": {
                                "text": json.dumps(
                                    {"token": "worker-token", "id": "worker-user", "name": "worker"}
                                )
                            },
                        },
                    },
                    {
                        "request": {
                            "url": "https://chat.z.ai/api/v2/chat/completions?user_id=worker-user",
                            "headers": [{"name": "user-agent", "value": "worker-agent"}],
                            "postData": {"text": json.dumps({"model": "glm-5.2"})},
                        },
                        "response": {"status": 200},
                    },
                ]
            }
        }
        with tempfile.TemporaryDirectory() as tmp:
            har_path = Path(tmp) / "worker.har"
            har_path.write_text(json.dumps(har), encoding="utf-8")
            expected_fingerprint = app.file_sha16(har_path)
            state, fingerprint = app.extract_state_via_worker(har_path, timeout_sec=10)
        self.assertEqual("worker-token", state.token)
        self.assertEqual("worker-user", state.user_id)
        self.assertEqual("worker-agent", state.user_agent)
        self.assertEqual(expected_fingerprint, fingerprint)

    def test_captcha_prefetch_shutdown_cancels_background_solver(self) -> None:
        original = (
            app._CAPTCHA_PREFETCH_ENABLED,
            app._CAPTCHA_MODE,
            app.get_happydom_captcha,
            app.happydom_captcha_available,
        )
        started = threading.Event()
        cancelled = threading.Event()

        def blocking_solver(_timeout_ms, *, cancel_check=None):
            started.set()
            while True:
                try:
                    if cancel_check is not None:
                        cancel_check()
                except app.ServiceShuttingDown:
                    cancelled.set()
                    raise
                time.sleep(0.01)

        try:
            app._shutdown_captcha_prefetch(timeout=1)
            app._CAPTCHA_PREFETCH_STOP.clear()
            app._CAPTCHA_PREFETCH_ENABLED = True
            app._CAPTCHA_MODE = "happydom"
            app.get_happydom_captcha = blocking_solver
            app.happydom_captcha_available = lambda: True
            app._schedule_captcha_prefetch(30_000)
            self.assertTrue(started.wait(timeout=1))
            self.assertTrue(app._shutdown_captcha_prefetch(timeout=1))
            self.assertTrue(cancelled.wait(timeout=1))
            self.assertFalse(app._CAPTCHA_PREFETCHING)
            self.assertIsNone(app._CAPTCHA_PREFETCH_THREAD)
        finally:
            app._shutdown_captcha_prefetch(timeout=1)
            app._CAPTCHA_PREFETCH_STOP.clear()
            (
                app._CAPTCHA_PREFETCH_ENABLED,
                app._CAPTCHA_MODE,
                app.get_happydom_captcha,
                app.happydom_captcha_available,
            ) = original

    def test_captcha_pool_expiry(self) -> None:
        app._CAPTCHA_POOL.append(("stale-captcha", time.monotonic() - app.CAPTCHA_POOL_TTL_SEC - 1))
        app._CAPTCHA_POOL.append(("fresh-captcha", time.monotonic()))
        self.assertEqual("fresh-captcha", app._captcha_pool_take(), "过期条目应被跳过")
        self.assertEqual("", app._captcha_pool_take())

    def test_browser_captcha_mode_does_not_consume_happydom_pool(self) -> None:
        class BrowserWorker:
            def solve(self, *_args, **_kwargs):
                return "browser-captcha"

        original_mode = app._CAPTCHA_MODE
        app._CAPTCHA_POOL.append(("happydom-pooled-captcha", time.monotonic()))
        try:
            app._CAPTCHA_MODE = "browser"
            app._set_captcha_degraded(-3600)
            result = app.resolve_fresh_captcha(fake_state(), "glm-5.2", BrowserWorker(), timeout_ms=1000)
            self.assertEqual("browser-captcha", result)
            self.assertEqual(1, len(app._CAPTCHA_POOL), "browser 模式不能消费 happy-dom 预热池")
        finally:
            app._CAPTCHA_MODE = original_mode
            app._CAPTCHA_POOL.clear()
            app._set_captcha_degraded(-3600)

    def test_captcha_error_classification(self) -> None:
        # 2026-08-29 04:02 实测：超龄池码被上游拒绝，F018/F019 必须可识别且可重试。
        f019 = (
            'UPSTREAM_ERROR: {"captcha_error_type":"verify_failed",'
            '"code":"FRONTEND_CAPTCHA_REQUIRED","detail":"人机验证失败，请重新验证后再试。",'
            '"verify_code":"F019"}'
        )
        f018 = (
            'UPSTREAM_ERROR: {"captcha_error_type":"verify_failed",'
            '"code":"FRONTEND_CAPTCHA_REQUIRED","detail":"人机验证失败，请重新验证后再试。",'
            '"verify_code":"F018"}'
        )
        self.assertTrue(app.is_captcha_upstream_error(f019))
        self.assertTrue(app.is_captcha_upstream_error(f018))
        self.assertFalse(app.is_captcha_upstream_error("AUTH_REQUIRED: unauthorized"))
        self.assertTrue(app.is_retryable_upstream_error(f018))
        busy = "MODEL_CONCURRENCY_LIMIT: 当前模型使用人数较多"
        self.assertTrue(app.is_retryable_upstream_error(busy))
        self.assertFalse(app.is_retryable_upstream_error("AUTH_REQUIRED: unauthorized"))
        # 实测边界：池码 ~61s 可用、~114s 被拒，TTL 必须落在两者之间。
        self.assertLessEqual(app.CAPTCHA_POOL_TTL_SEC, 120.0)
        self.assertGreaterEqual(app.CAPTCHA_POOL_TTL_SEC, 60.0)

    def test_stream_retries_captcha_error_with_forced_fresh_solve(self) -> None:
        state = fake_state()
        resolve_calls: list[bool] = []

        def fake_resolve(_state, _model, _worker, timeout_ms=None, chrome_path=None, headless=True, force_fresh=False):
            resolve_calls.append(force_fresh)
            return "captcha-forced" if force_fresh else "captcha-pooled"

        captcha_error = (
            'data: {"data":{"content":"","done":true,"error":{"code":"FRONTEND_CAPTCHA_REQUIRED",'
            '"captcha_error_type":"verify_failed","detail":"人机验证失败，请重新验证后再试。",'
            '"verify_code":"F019"}},"type":"chat:completion"}\n\n'
        )

        class FakeResp:
            def __init__(self, chunks: list[bytes]):
                self._chunks = chunks

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def __iter__(self):
                return iter(self._chunks)

        attempts: list[list[bytes]] = [
            [captcha_error.encode("utf-8"), b'data: {"data":"[DONE]"}\n\n'],
            [
                b'data: {"data":{"delta_content":"RECOVERED","phase":"answer"}}\n\n',
                b'data: {"data":"[DONE]"}\n\n',
            ],
        ]

        def fake_urlopen(_req, timeout=None):
            urlopen_calls.append(len(urlopen_calls))
            return FakeResp(attempts[len(urlopen_calls) - 1])

        urlopen_calls: list[int] = []
        deleted: list[str] = []
        new_chat_calls: list[str] = []

        def fake_new_chat(_state, _prompt, options=None):
            chat_id = f"00000000-0000-0000-0000-{len(new_chat_calls):012d}"
            new_chat_calls.append(chat_id)
            return chat_id, "u1"

        real = (app.new_chat, app.delete_zai_chat, app.urlopen, app.resolve_fresh_captcha)
        app.new_chat = fake_new_chat
        app.delete_zai_chat = lambda _s, chat_id, **_kwargs: deleted.append(chat_id) or True
        app.urlopen = fake_urlopen
        app.resolve_fresh_captcha = fake_resolve
        try:
            events = list(
                self.original_stream(
                    state,
                    "hi",
                    options=app.ChatOptions(model="glm-5.2"),
                    fresh_captcha_browser=True,
                    retry_wait_sec=3,
                    retry_attempts=3,
                )
            )
        finally:
            app.new_chat, app.delete_zai_chat, app.urlopen, app.resolve_fresh_captcha = real
        self.assertEqual([False, True], resolve_calls, "验证码被拒后必须强制现场重解（绕开池码）")
        self.assertEqual(2, len(new_chat_calls), "验证码被拒后应换新会话重发")
        joined = "".join(events)
        self.assertNotIn("FRONTEND_CAPTCHA_REQUIRED", joined, "验证码错误不应透传给客户端")
        self.assertIn("RECOVERED", joined)
        self.assertEqual([new_chat_calls[0]], deleted, "被拒的空会话应被清理")

    def test_anthropic_busy_returns_529_when_retries_exhausted(self) -> None:
        busy_event = (
            'data: {"data":{"content":"","done":true,"error":{"code":"MODEL_CONCURRENCY_LIMIT",'
            '"detail":"当前模型使用人数较多，请稍后再试或切换到其他模型。"}},"type":"chat:completion"}'
        )

        def busy_stream(_state, _prompt, **_kwargs):
            yield busy_event

        app.stream_zai_completion = busy_stream
        request = Request(
            self.base_url + "/v1/messages",
            data=json.dumps(
                {"model": "glm-5.2", "max_tokens": 64, "messages": [{"role": "user", "content": "hi"}]}
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urlopen(request, timeout=8)
            raise AssertionError("expected 529")
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            self.assertEqual(529, exc.code)
            self.assertIn("overloaded_error", body)
            self.assertEqual("3", exc.headers.get("Retry-After"))
            exc.close()

    def test_openai_busy_returns_503_when_retries_exhausted(self) -> None:
        busy_event = (
            'data: {"data":{"content":"","done":true,"error":{"code":"MODEL_CONCURRENCY_LIMIT",'
            '"detail":"当前模型使用人数较多，请稍后再试或切换到其他模型。"}},"type":"chat:completion"}'
        )

        def busy_stream(_state, _prompt, **_kwargs):
            yield busy_event

        app.stream_zai_completion = busy_stream
        status, body = self.request(
            "POST",
            "/v1/chat/completions",
            {"model": "glm-5.2", "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(503, status)
        self.assertIn("server_error", body)
        self.assertIn("MODEL_CONCURRENCY_LIMIT", body)

    def test_head_request_supported(self) -> None:
        # 2026-08-29 03:58 实测：HEAD /api/hello 曾返回 501 Unsupported method，
        # 会让探测方误判服务不可用。
        for path in ("/", "/api/status", "/api/hello", "/healthz"):
            request = Request(self.base_url + path, method="HEAD")
            with urlopen(request, timeout=8) as response:
                self.assertEqual(200, response.status, path)
                self.assertEqual(b"", response.read(), "HEAD 响应不应包含响应体")

    def test_health_endpoint_identifies_glm2api_service(self) -> None:
        status, raw = self.request("GET", "/healthz")
        self.assertEqual(200, status)
        payload = json.loads(raw)
        self.assertTrue(payload["ok"])
        self.assertEqual(app.SERVICE_ID, payload["service"])
        self.assertIn("auth_ready", payload)

    def test_http_security_and_cache_headers(self) -> None:
        with urlopen(self.base_url + "/", timeout=8) as response:
            self.assertEqual("DENY", response.headers.get("X-Frame-Options"))
            self.assertEqual("nosniff", response.headers.get("X-Content-Type-Options"))
            self.assertIn("frame-ancestors 'none'", response.headers.get("Content-Security-Policy", ""))
            self.assertIn("camera=()", response.headers.get("Permissions-Policy", ""))
            self.assertEqual("no-cache", response.headers.get("Cache-Control"))
        with urlopen(self.base_url + "/api/status", timeout=8) as response:
            self.assertEqual("no-store", response.headers.get("Cache-Control"))
            self.assertIn("default-src 'self'", response.headers.get("Content-Security-Policy", ""))

    def test_browser_origin_guard_blocks_simple_cross_site_requests(self) -> None:
        original_cors = QuietProxyHandler.cors_origins
        original_settings = dict(QuietProxyHandler.settings)
        try:
            QuietProxyHandler.cors_origins = ()
            hostile = {"Origin": "http://hostile.example"}

            status, raw = self.request("GET", "/api/hello", headers=hostile)
            self.assertEqual(403, status)
            self.assertEqual("origin_not_allowed", json.loads(raw)["error"]["code"])

            status, raw = self.request(
                "POST",
                "/api/settings",
                {"model": "glm-5.2"},
                headers=hostile,
            )
            self.assertEqual(403, status)
            self.assertEqual("origin_not_allowed", json.loads(raw)["error"]["code"])
            self.assertEqual(original_settings, QuietProxyHandler.settings, "跨站简单 POST 不得执行任何设置变更")

            local_origin = f"http://127.0.0.1:{self.server.server_port}"
            status, raw = self.request("GET", "/api/hello", headers={"Origin": local_origin})
            self.assertEqual(200, status)
            self.assertTrue(json.loads(raw)["ok"])
        finally:
            QuietProxyHandler.cors_origins = original_cors
            QuietProxyHandler.settings = original_settings

    def test_management_query_limits_history_clamps_and_upload_connection_close(self) -> None:
        too_many = "&".join(f"field{index}=x" for index in range(app.MAX_QUERY_FIELDS + 1))
        status, raw = self.request("GET", f"/api/logs?{too_many}")
        self.assertEqual(400, status, raw[:500])
        self.assertEqual("invalid_query", json.loads(raw)["error"]["code"])

        long_value = "x" * (app.MAX_QUERY_VALUE_CHARS + 1)
        status, raw = self.request("GET", f"/api/metrics?hours={long_value}")
        self.assertEqual(400, status, raw[:500])
        self.assertEqual("invalid_query", json.loads(raw)["error"]["code"])

        original_page = app.local_history_summary_page
        captured: dict[str, object] = {}

        def capture_page(**kwargs):
            captured.update(kwargs)
            return [], 0

        try:
            app.local_history_summary_page = capture_page
            search = "s" * (app.MAX_HISTORY_SEARCH_CHARS + 20)
            status, raw = self.request(
                "GET",
                f"/api/history/records?page={app.MAX_HISTORY_QUERY_PAGE + 99}&text={search}",
            )
        finally:
            app.local_history_summary_page = original_page
        self.assertEqual(200, status, raw[:500])
        self.assertEqual(app.MAX_HISTORY_QUERY_PAGE, captured["page"])
        self.assertEqual(app.MAX_HISTORY_SEARCH_CHARS, len(str(captured["text"])))

        connection = app.http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=8)
        try:
            connection.request(
                "POST",
                f"/api/files/upload?{too_many}",
                body=b"unread-upload-body",
                headers={"Content-Type": "application/octet-stream"},
            )
            response = connection.getresponse()
            body = response.read().decode("utf-8", errors="replace")
            self.assertEqual(400, response.status, body[:500])
            self.assertEqual("close", str(response.getheader("Connection") or "").lower())
            self.assertEqual("invalid_query", json.loads(body)["error"]["code"])
        finally:
            connection.close()

        html = app.WEB_INDEX_PATH.read_text(encoding="utf-8")
        self.assertIn('id="history-search-input" class="input-custom" type="text" maxlength="256"', html)

    def test_remote_bind_requires_explicit_permission_and_api_key(self) -> None:
        app.validate_server_bind("127.0.0.1", False, "")
        app.validate_server_bind("::1", False, "")
        with self.assertRaisesRegex(RuntimeError, "--allow-remote"):
            app.validate_server_bind("0.0.0.0", False, "secret")
        with self.assertRaisesRegex(RuntimeError, "API Key"):
            app.validate_server_bind("0.0.0.0", True, "")
        app.validate_server_bind("0.0.0.0", True, "secret")

    # ------------------------------------------------------------------
    # 历史对话（上游账号历史浏览，/api/history/*）
    # ------------------------------------------------------------------

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

    def test_list_zai_chats_parses_upstream_array(self) -> None:
        state = fake_state()
        sample = [{"id": self.HISTORY_CHAT_ID, "title": "你好问候", "updated_at": 1, "created_at": 1, "type": "default"}]
        captured: list[tuple[str, str]] = []

        def fake_http_json(method, url, _headers, payload=None):
            captured.append((method, url))
            return sample

        real = app.http_json
        app.http_json = fake_http_json
        try:
            self.assertEqual(sample, app.list_zai_chats(state, page=2))
        finally:
            app.http_json = real
        self.assertEqual(("GET", f"{app.BASE_URL}/api/v1/chats/?page=2&type=default"), captured[0])

        app.http_json = lambda *a, **k: {"unexpected": True}
        try:
            with self.assertRaises(RuntimeError):
                app.list_zai_chats(state)
        finally:
            app.http_json = real

    def test_extract_chat_history_messages_parent_chain(self) -> None:
        messages = app.extract_chat_history_messages(self.HISTORY_DETAIL)
        self.assertEqual(2, len(messages))
        self.assertEqual("user", messages[0]["role"])
        self.assertEqual("你好", messages[0]["content"])
        self.assertEqual("assistant", messages[1]["role"])
        self.assertEqual("你好！很高兴见到你。", messages[1]["content"])

    def test_extract_chat_history_messages_fallback_and_defense(self) -> None:
        # 无 currentId：按时间戳正序兜底；content 缺失/JSON 字符串/结构化数组都要能解析。
        detail = {
            "chat": {
                "history": {
                    "messages": {
                        "b": {"id": "b", "role": "assistant", "timestamp": 200, "content": '{"content":"结构化内容"}'},
                        "a": {"id": "a", "role": "user", "timestamp": 100},
                        "c": {
                            "id": "c",
                            "role": "assistant",
                            "timestamp": 300,
                            "content": [{"type": "text", "text": "数组内容"}, {"type": "text", "text": "第二段"}],
                        },
                    }
                }
            }
        }
        messages = app.extract_chat_history_messages(detail)
        self.assertEqual(["user", "assistant", "assistant"], [m["role"] for m in messages])
        self.assertEqual("", messages[0]["content"])
        self.assertEqual("结构化内容", messages[1]["content"])
        self.assertEqual("数组内容\n第二段", messages[2]["content"])
        # 环形 parent 链不能死循环
        loop = {
            "chat": {
                "history": {
                    "currentId": "x",
                    "messages": {
                        "x": {"id": "x", "parentId": "y", "role": "user", "timestamp": 1, "content": "hi"},
                        "y": {"id": "y", "parentId": "x", "role": "assistant", "timestamp": 2, "content": "yo"},
                    },
                }
            }
        }
        self.assertEqual(2, len(app.extract_chat_history_messages(loop)))
        self.assertEqual([], app.extract_chat_history_messages({}))

    def test_history_endpoints_roundtrip(self) -> None:
        deleted: list[str] = []
        real = (app.list_zai_chats, app.get_zai_chat_detail, app.delete_zai_chat)
        app.list_zai_chats = lambda _state, page=1: [
            {"id": self.HISTORY_CHAT_ID, "title": "你好问候", "updated_at": 1787938848, "created_at": 1787938845, "type": "default"}
        ]
        app.get_zai_chat_detail = lambda _state, chat_id: self.HISTORY_DETAIL
        app.delete_zai_chat = lambda _state, chat_id: deleted.append(chat_id) or True
        try:
            status, body = self.request("GET", "/api/history/chats")
            self.assertEqual(200, status)
            data = json.loads(body)
            self.assertTrue(data["ok"])
            self.assertEqual(1, data["count"])
            self.assertEqual(self.HISTORY_CHAT_ID, data["chats"][0]["id"])

            status, body = self.request("GET", f"/api/history/chat?id={self.HISTORY_CHAT_ID}")
            self.assertEqual(200, status)
            data = json.loads(body)
            self.assertTrue(data["ok"])
            self.assertEqual("你好问候", data["chat"]["title"])
            self.assertEqual(["user", "assistant"], [m["role"] for m in data["messages"]])
            self.assertEqual("你好", data["messages"][0]["content"])

            status, body = self.request("POST", "/api/history/delete", {"chat_id": self.HISTORY_CHAT_ID})
            self.assertEqual(200, status)
            self.assertEqual([self.HISTORY_CHAT_ID], deleted)

            status, body = self.request("GET", "/api/history/chat?id=not-a-uuid")
            self.assertEqual(400, status)
            self.assertIn("chat_id", body)
        finally:
            app.list_zai_chats, app.get_zai_chat_detail, app.delete_zai_chat = real

    def test_history_endpoints_upstream_error_returns_message(self) -> None:
        real = (app.list_zai_chats, app.get_zai_chat_detail)

        def boom(_state, page=1):
            raise app.UpstreamRequestError("获取对话列表失败: HTTP Error 500: upstream sad")

        app.list_zai_chats = boom
        try:
            status, body = self.request("GET", "/api/history/chats")
            self.assertEqual(502, status)
            self.assertIn("获取对话列表失败", body)
        finally:
            app.list_zai_chats, app.get_zai_chat_detail = real

    # ------------------------------------------------------------------
    # 本地对话镜像：上游不为 API 会话保存助手回复，历史页靠本地镜像补全。
    # ------------------------------------------------------------------

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

    def test_history_record_lifecycle_persists_and_caps(self) -> None:
        self._make_record("00000000-0000-0000-0000-000000000001", "第一问", "第一答", "思考过程")
        self._make_record("00000000-0000-0000-0000-000000000002", "第二问", "第二答")
        records = app.local_history_records()
        self.assertEqual(2, len(records))
        first = records[0]
        self.assertEqual("第一答", first["content"])
        self.assertEqual("思考过程", first["reasoning"])
        self.assertEqual("第一问", first["title"])
        self.assertEqual("第一问", first["user_input"])
        self.assertEqual("success", first["status"])
        self.assertEqual("openai_chat", first["surface"])
        self.assertTrue(first["completed_at"] > 0)
        usage = first.get("usage") or {}
        self.assertIn("prompt_tokens", usage)
        self.assertIn("reasoning_tokens", usage)
        self.assertGreater(usage["reasoning_tokens"], 0, "思维链应计入 token 估算")
        # v4 存储：索引携带摘要条目（ds2api SummaryEntry 同构），正文在 detail 目录
        raw = json.loads(app.HISTORY_STORE_PATH.read_text(encoding="utf-8"))
        self.assertEqual(app.HISTORY_SCHEMA, raw["schema"])
        self.assertEqual(2, len(raw["items"]))
        self.assertEqual(records[0]["id"], raw["items"][0]["id"])
        self.assertEqual("第一答", raw["items"][0]["preview"][:3])
        self.assertEqual("success", raw["items"][0]["status"])
        detail_path = app.HISTORY_DETAIL_DIR / f"{records[0]['id']}.json"
        detail_env = json.loads(detail_path.read_text(encoding="utf-8"))
        self.assertEqual("第一答", detail_env["record"]["content"])
        # 超上限裁剪最旧记录
        original_conf = dict(app._HISTORY_CONF)
        try:
            app._HISTORY_CONF["max_records"] = 2
            self._make_record("00000000-0000-0000-0000-000000000004", "第三问", "第三答")
            records = app.local_history_records()
            self.assertEqual(2, len(records))
            self.assertEqual("00000000-0000-0000-0000-000000000002", records[0]["chat_id"])
            # 被裁剪记录的 detail 文件应被清理
            remaining_ids = {r["id"] for r in records}
            for path in app.HISTORY_DETAIL_DIR.glob("req_*.json"):
                self.assertIn(path.stem, remaining_ids)
        finally:
            app._HISTORY_CONF.clear()
            app._HISTORY_CONF.update(original_conf)

    def test_history_detail_byte_budget_evicts_oldest_and_keeps_newest(self) -> None:
        app.HISTORY_MAX_DETAIL_BYTES = 2_200
        record_ids = [
            app.start_history_record(
                surface="openai_chat",
                model="glm-5.3",
                user_input=f"第 {index} 条 " + (chr(96 + index) * 900),
                messages=[],
            )
            for index in range(1, 4)
        ]

        records = app.local_history_records()
        retained_ids = {record["id"] for record in records}
        self.assertLess(len(records), 3)
        self.assertNotIn(record_ids[0], retained_ids)
        self.assertIn(record_ids[-1], retained_ids)
        self.assertFalse((app.HISTORY_DETAIL_DIR / f"{record_ids[0]}.json").exists())
        status = app.history_store_status()
        self.assertLessEqual(status["detail_bytes"], app.HISTORY_MAX_DETAIL_BYTES)
        self.assertEqual(len(records), status["records"])
        self.assertEqual(len(records), status["detail_files"])

        app.HISTORY_MAX_DETAIL_BYTES = 128
        oversized_id = app.start_history_record(
            surface="openai_chat",
            model="glm-5.3",
            user_input="latest " + ("z" * 1_000),
            messages=[],
        )
        records = app.local_history_records()
        self.assertEqual([oversized_id], [record["id"] for record in records])
        self.assertTrue((app.HISTORY_DETAIL_DIR / f"{oversized_id}.json").exists())
        self.assertTrue(app.history_store_status()["over_detail_budget"])

    def test_history_load_recovers_detail_missing_from_index(self) -> None:
        indexed_id = app.start_history_record(
            surface="openai_chat",
            model="glm-5.3",
            user_input="索引内记录",
            messages=[],
        )
        orphan_id = "req_unindexed_recoverable"
        orphan = {
            "id": orphan_id,
            "status": "success",
            "created_at": int(time.time() * 1000) + 1,
            "title": "待恢复记录",
            "user_input": "待恢复记录",
            "messages": [],
            "files": [],
            "context_files": [],
            "account": "legacy-account",
        }
        (app.HISTORY_DETAIL_DIR / f"{orphan_id}.json").write_text(
            json.dumps({"schema": app.HISTORY_SCHEMA, "record": orphan}, ensure_ascii=False),
            encoding="utf-8",
        )
        app._HISTORY_CACHE = None

        records = app.local_history_records()
        self.assertEqual([indexed_id, orphan_id], [record["id"] for record in records])
        recovered = records[-1]
        self.assertEqual(app.sha16("legacy-account"), recovered["account"])
        self.assertEqual(1, recovered["account_fp_version"])
        index = json.loads(app.HISTORY_STORE_PATH.read_text(encoding="utf-8"))
        self.assertEqual([indexed_id, orphan_id], [item["id"] for item in index["items"]])

    def test_history_load_removes_unindexed_detail_older_than_index(self) -> None:
        stale_id = app.start_history_record(
            surface="openai_chat",
            model="glm-5.3",
            user_input="已从索引删除",
            messages=[],
        )
        stale_path = app.HISTORY_DETAIL_DIR / f"{stale_id}.json"
        app.HISTORY_STORE_PATH.write_text(
            json.dumps(
                {
                    "schema": app.HISTORY_SCHEMA,
                    "updated": int(time.time() * 1000) + 10_000,
                    "items": [],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        app._HISTORY_CACHE = None

        self.assertEqual([], app.local_history_records())
        self.assertFalse(stale_path.exists())

    def test_history_store_budgets_ids_and_detail_integrity_are_enforced(self) -> None:
        with self.assertRaisesRegex(ValueError, "storage id is invalid"):
            app._history_detail_path("../outside")

        app.HISTORY_STORE_PATH.write_bytes(b"x" * (app.MAX_HISTORY_INDEX_BYTES + 1))
        app._HISTORY_CACHE = None
        self.assertEqual([], app.local_history_records())

        record_id = "req_0123456789abcdef"
        app.HISTORY_STORE_PATH.write_text(
            json.dumps({"schema": app.HISTORY_SCHEMA, "items": [{"id": record_id}]}),
            encoding="utf-8",
        )
        app.HISTORY_DETAIL_DIR.mkdir(parents=True, exist_ok=True)
        detail_path = app.HISTORY_DETAIL_DIR / f"{record_id}.json"
        detail_path.write_bytes(b"x" * (app.MAX_HISTORY_DETAIL_FILE_BYTES + 1))
        app._HISTORY_CACHE = None
        self.assertEqual([], app.local_history_records())

        detail_path.write_text(
            json.dumps({"schema": app.HISTORY_SCHEMA, "record": {"id": "req_fedcba9876543210"}}),
            encoding="utf-8",
        )
        app._HISTORY_CACHE = None
        self.assertEqual([], app.local_history_records())

        previous_scan_limit = app.MAX_HISTORY_DETAIL_SCAN_FILES
        try:
            app.MAX_HISTORY_DETAIL_SCAN_FILES = 1
            (app.HISTORY_DETAIL_DIR / "req_11111111.json").write_text("{}", encoding="utf-8")
            status = app.history_store_status()
            self.assertEqual(1, status["detail_files"])
            self.assertTrue(status["detail_scan_truncated"])
        finally:
            app.MAX_HISTORY_DETAIL_SCAN_FILES = previous_scan_limit

    def test_history_account_is_fingerprinted_and_legacy_records_are_migrated(self) -> None:
        raw_account = "raw-upstream-user-id"
        record_id = app.start_history_record(
            surface="openai_chat",
            model="glm-5.3",
            user_input="隐私检查",
            messages=[],
            account=raw_account,
        )
        created = next(item for item in app.local_history_records() if item["id"] == record_id)
        self.assertEqual(app.sha16(raw_account), created["account"])
        self.assertEqual(1, created["account_fp_version"])
        self.assertNotIn(raw_account, (app.HISTORY_DETAIL_DIR / f"{record_id}.json").read_text(encoding="utf-8"))

        legacy_id = "req_legacy_account_record"
        legacy_record = {
            "id": legacy_id,
            "account": "legacy12",
            "status": "success",
            "title": "旧记录",
            "user_input": "旧记录",
            "messages": [],
            "files": [],
            "context_files": [],
        }
        app.HISTORY_DETAIL_DIR.mkdir(parents=True, exist_ok=True)
        (app.HISTORY_DETAIL_DIR / f"{legacy_id}.json").write_text(
            json.dumps({"schema": app.HISTORY_SCHEMA, "record": legacy_record}, ensure_ascii=False),
            encoding="utf-8",
        )
        app.HISTORY_STORE_PATH.write_text(
            json.dumps({"schema": app.HISTORY_SCHEMA, "items": [{"id": legacy_id}]}, ensure_ascii=False),
            encoding="utf-8",
        )
        app._HISTORY_CACHE = None
        app._HISTORY_DIRTY.clear()
        migrated = app.local_history_records()
        self.assertEqual(app.sha16("legacy12"), migrated[0]["account"])
        persisted = json.loads((app.HISTORY_DETAIL_DIR / f"{legacy_id}.json").read_text(encoding="utf-8"))
        self.assertEqual(app.sha16("legacy12"), persisted["record"]["account"])
        self.assertEqual(1, persisted["record"]["account_fp_version"])

    def test_v4_store_reloads_from_index_and_detail(self) -> None:
        rid = self._make_record("00000000-0000-0000-0000-0000000000e1", "重载问", "重载答")
        app._HISTORY_CACHE = None
        records = app.local_history_records()
        self.assertEqual(1, len(records))
        self.assertEqual(rid, records[0]["id"])
        self.assertEqual("重载答", records[0]["content"])
        # 删除单条后 detail 文件一并清理
        removed = app.purge_history_record(rid)
        self.assertEqual(1, removed)
        self.assertFalse((app.HISTORY_DETAIL_DIR / f"{rid}.json").exists())
        app._HISTORY_CACHE = None
        self.assertEqual([], app.local_history_records())

    def test_history_progress_persists_midstream(self) -> None:
        rid = app.start_history_record(
            surface="panel_chat",
            model="glm-5.2",
            stream=True,
            user_input="进度问",
            messages=[{"role": "user", "content": "进度问"}],
            chat_id="00000000-0000-0000-0000-0000000000c1",
        )
        self.assertTrue(rid)
        app.update_history_progress(
            rid,
            content="部分回答",
            reasoning="部分思考",
            status_code=200,
            elapsed_ms=321,
        )
        # 进度应立即落盘（detail 文件与索引摘要均可见），记录仍是 streaming
        detail_env = json.loads((app.HISTORY_DETAIL_DIR / f"{rid}.json").read_text(encoding="utf-8"))
        self.assertEqual("streaming", detail_env["record"]["status"])
        self.assertEqual("部分回答", detail_env["record"]["content"])
        self.assertEqual("部分思考", detail_env["record"]["reasoning"])
        self.assertEqual(200, detail_env["record"]["status_code"])
        index = json.loads(app.HISTORY_STORE_PATH.read_text(encoding="utf-8"))
        self.assertEqual("部分回答", index["items"][0]["preview"][:4])
        # 完成后覆盖为终态
        app.finish_history_record(
            rid, status="success", content="完整回答", reasoning="完整思考",
            chat_id="00000000-0000-0000-0000-0000000000c1",
        )
        records = app.local_history_records()
        self.assertEqual("success", records[0]["status"])
        self.assertEqual("完整回答", records[0]["content"])

    def test_history_persist_retries_when_index_write_fails(self) -> None:
        rid = app.start_history_record(
            surface="panel_chat",
            model="glm-5.2",
            stream=True,
            user_input="索引失败重试",
            messages=[{"role": "user", "content": "索引失败重试"}],
        )
        original_write = app._history_write_atomic_locked

        def fail_index(path: Path, body: str) -> None:
            if path == app.HISTORY_STORE_PATH:
                raise OSError("simulated index write failure")
            original_write(path, body)

        try:
            app._history_write_atomic_locked = fail_index
            app.update_history_progress(rid, content="可重试内容", elapsed_ms=100)
            self.assertIn(rid, app._HISTORY_DIRTY, "索引失败后不能丢弃待持久化标记")
            failed_status = app.history_store_status()
            self.assertFalse(failed_status["persisted"])
            self.assertEqual(1, failed_status["pending_writes"])
            self.assertIn("simulated index write failure", failed_status["error"])
        finally:
            app._history_write_atomic_locked = original_write

        with app._HISTORY_LOCK:
            self.assertTrue(app._history_persist_locked())
        self.assertNotIn(rid, app._HISTORY_DIRTY)
        recovered_status = app.history_store_status()
        self.assertTrue(recovered_status["persisted"])
        self.assertEqual("", recovered_status["error"])
        index = json.loads(app.HISTORY_STORE_PATH.read_text(encoding="utf-8"))
        self.assertEqual("可重试内容", index["items"][0]["preview"])

    def test_history_lookup_prefers_recent_record(self) -> None:
        app._HISTORY_CACHE = [
            {"id": "same", "content": "旧记录"},
            {"id": "different", "content": "中间记录"},
            {"id": "same", "content": "最新记录"},
        ]
        with app._HISTORY_LOCK:
            record = app._history_find_locked("same")
        self.assertEqual("最新记录", record["content"])

    def test_unfiltered_history_page_builds_only_requested_summaries(self) -> None:
        app._HISTORY_CACHE = [
            {
                "id": f"req_{index:08x}",
                "status": "success",
                "title": f"记录 {index}",
                "content": f"回答 {index}",
            }
            for index in range(200)
        ]
        original_summary = app._history_summary_locked
        built: list[str] = []

        def counted_summary(record: dict) -> dict:
            built.append(str(record.get("id") or ""))
            return original_summary(record)

        try:
            app._history_summary_locked = counted_summary
            items, total = app.local_history_summary_page(page=1, page_size=50)
        finally:
            app._history_summary_locked = original_summary
        self.assertEqual(200, total)
        self.assertEqual(50, len(items))
        self.assertEqual(50, len(built), "无筛选自动刷新不应构建其余 150 条摘要")
        self.assertEqual("记录 199", items[0]["title"])

    def test_caller_field_derived_from_surface(self) -> None:
        app.start_history_record(surface="panel_chat", model="m", stream=True, user_input="问", messages=[], chat_id="c1")
        app.start_history_record(surface="openai_chat", model="m", stream=True, user_input="问", messages=[], chat_id="c2")
        app.start_history_record(surface="cli_direct", model="m", stream=True, user_input="问", messages=[], chat_id="c3")
        records = {r["chat_id"]: r for r in app.local_history_records()}
        self.assertEqual("panel", records["c1"]["caller"])
        self.assertEqual("api", records["c2"]["caller"])
        self.assertEqual("cli", records["c3"]["caller"])

    def test_stopped_record_status_code_200(self) -> None:
        self._make_record("00000000-0000-0000-0000-0000000000d1", "停止问", "半截答", finish_status="stopped")
        record = app.local_history_records()[0]
        self.assertEqual("stopped", record["status"])
        self.assertEqual(200, record["status_code"])

    def test_history_records_filtering(self) -> None:
        ok_id = self._make_record("00000000-0000-0000-0000-0000000000f1", "蓝色风车是什么", "蓝色风车答")
        self._make_record(
            "00000000-0000-0000-0000-0000000000f2", "失败的请求", "", finish_status="error", error="upstream dead"
        )
        status, body = self.request("GET", "/api/history/records?text=%E9%A3%8E%E8%BD%A6")
        self.assertEqual(200, status)
        data = json.loads(body)
        self.assertEqual(1, data["count"])
        self.assertEqual(ok_id, data["records"][0]["id"])

        status, body = self.request("GET", "/api/history/records?status=error")
        self.assertEqual(200, status)
        data = json.loads(body)
        self.assertEqual(1, data["count"])
        self.assertEqual("error", data["records"][0]["status"])

        status, body = self.request("GET", "/api/history/records?status=bogus")
        self.assertEqual(200, status)
        self.assertEqual(2, json.loads(body)["count"], "非法状态值应被忽略")

    def test_history_messages_snapshot_marks_nontext_parts(self) -> None:
        messages = [
            {"role": "system", "content": "你是助手"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "看图说话"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,xxx"}},
                ],
            },
            {"role": "user", "content": {"type": "file", "filename": "报告.pdf"}},
        ]
        snapshot = app.history_messages_snapshot(messages)
        self.assertEqual(3, len(snapshot))
        self.assertEqual("你是助手", snapshot[0]["content"])
        self.assertIn("看图说话", snapshot[1]["content"])
        self.assertIn("[图片]", snapshot[1]["content"])
        self.assertIn("[文件: 报告.pdf]", snapshot[2]["content"])
        files = app.history_files_snapshot(
            [
                {"name": "报告.pdf", "size": "2048", "content_type": "application/pdf"},
                {"file": {"id": "nested", "meta": {"name": "history.txt", "size": 321, "content_type": "text/plain"}}},
                {"id": "no-name-file"},
            ]
        )
        self.assertEqual(2, len(files))
        self.assertEqual(2048, files[0]["size"])
        self.assertEqual("报告.pdf", files[0]["name"])
        self.assertEqual("history.txt", files[1]["name"])
        self.assertEqual(321, files[1]["size"])

        content = "x" * (app.HISTORY_CONTEXT_FILE_CHARS + 10)
        context_files = app.history_context_files_snapshot(
            [{"kind": "tools", "name": "tools.txt", "content": content, "part": 2, "parts": 3}]
        )
        self.assertEqual(1, len(context_files))
        self.assertEqual("tools", context_files[0]["kind"])
        self.assertEqual(app.HISTORY_CONTEXT_FILE_CHARS, len(context_files[0]["content"]))
        self.assertEqual(len(content), context_files[0]["original_chars"])
        self.assertTrue(context_files[0]["truncated"])
        self.assertEqual((2, 3), (context_files[0]["part"], context_files[0]["parts"]))

    def test_history_record_preserves_upstream_delivery_manifest(self) -> None:
        rid = app.start_history_record(
            surface="openai_chat",
            model="glm-5.2",
            stream=True,
            user_input="最后一问",
            messages=[{"role": "user", "content": "最后一问"}],
            files=[
                {"filename": "history.txt", "meta": {"name": "history.txt", "size": 12}},
                {"filename": "tools.txt", "meta": {"name": "tools.txt", "size": 9}},
            ],
            context_text="历史正文",
            final_prompt="读取附件并回答",
            delivery_mode="file",
            context_file_requested=True,
            context_files=[
                {"kind": "history", "name": "history.txt", "content": "历史正文"},
                {"kind": "tools", "name": "tools.txt", "content": "工具正文"},
            ],
        )
        app.finish_history_record(rid, content="完成")
        record = app.local_history_records()[0]
        self.assertEqual("file", record["delivery_mode"])
        self.assertTrue(record["context_file_requested"])
        self.assertEqual(["history", "tools"], [item["kind"] for item in record["context_files"]])
        self.assertEqual(["history.txt", "tools.txt"], [item["name"] for item in record["context_files"]])
        self.assertGreater(record["usage"]["prompt_tokens"], app.estimate_protocol_tokens(record["final_prompt"]))
        summary = app.local_history_summary()[0]
        self.assertEqual("file", summary["delivery_mode"])
        self.assertEqual(2, summary["context_files"])

    def test_history_chats_merges_local_entries(self) -> None:
        self._make_record("00000000-0000-0000-0000-00000000000a", "本地对话标题问", "本地答")
        real = (app.list_zai_chats, app.get_zai_chat_detail)
        app.list_zai_chats = lambda _state, page=1: [
            {"id": self.HISTORY_CHAT_ID, "title": "上游对话", "updated_at": 1787938848, "created_at": 1787938845, "type": "default"}
        ]
        try:
            status, body = self.request("GET", "/api/history/chats")
            self.assertEqual(200, status)
            data = json.loads(body)
            self.assertEqual(2, data["count"])
            by_source = {c["source"]: c for c in data["chats"]}
            self.assertEqual(self.HISTORY_CHAT_ID, by_source["upstream"]["id"])
            self.assertEqual("00000000-0000-0000-0000-00000000000a", by_source["local"]["id"])
            self.assertEqual("本地对话标题问", by_source["local"]["title"])
        finally:
            app.list_zai_chats, app.get_zai_chat_detail = real

    def test_history_detail_uses_local_mirror_with_full_reply(self) -> None:
        # 上游详情里助手消息没有 content（实测行为），本地镜像必须补全回复。
        detail = json.loads(json.dumps(self.HISTORY_DETAIL))
        msgs = detail["chat"]["history"]["messages"]
        for m in msgs.values():
            if m["role"] == "assistant":
                m.pop("content", None)
        real = (app.list_zai_chats, app.get_zai_chat_detail)
        app.get_zai_chat_detail = lambda _state, chat_id: detail
        try:
            # 无本地记录：走上游路径，助手内容如实为空。
            status, body = self.request("GET", f"/api/history/chat?id={self.HISTORY_CHAT_ID}")
            self.assertEqual(200, status)
            data = json.loads(body)
            self.assertEqual("upstream", data["source"])
            self.assertEqual("", data["messages"][1]["content"])
        finally:
            app.get_zai_chat_detail = real[0]

        chat_uuid = "11111111-1111-1111-1111-111111111111"
        self._make_record(chat_uuid, "暗号是什么", "暗号是蓝色风车", thinking="先回忆暗号再回答")
        app.get_zai_chat_detail = lambda _state, chat_id: detail
        try:
            status, body = self.request("GET", f"/api/history/chat?id={chat_uuid}")
            self.assertEqual(200, status)
            data = json.loads(body)
            self.assertEqual("local", data["source"])
            self.assertEqual(["user", "assistant"], [m["role"] for m in data["messages"]])
            self.assertEqual("暗号是什么", data["messages"][0]["content"])
            self.assertEqual("暗号是蓝色风车", data["messages"][1]["content"])
            self.assertEqual("先回忆暗号再回答", data["messages"][1]["thinking"])
        finally:
            app.list_zai_chats, app.get_zai_chat_detail = real

    def test_history_detail_falls_back_to_local_when_upstream_fails(self) -> None:
        chat_uuid = "22222222-2222-2222-2222-222222222222"
        self._make_record(chat_uuid, "本地问", "本地答")

        def boom(_state, chat_id):
            raise app.UpstreamRequestError("获取对话详情失败: HTTP Error 500: sad")

        real = (app.get_zai_chat_detail,)
        app.get_zai_chat_detail = boom
        try:
            status, body = self.request("GET", f"/api/history/chat?id={chat_uuid}")
            self.assertEqual(200, status)
            data = json.loads(body)
            self.assertEqual("local", data["source"])
            self.assertEqual("本地答", data["messages"][1]["content"])

            status, body = self.request("GET", f"/api/history/chat?id={self.HISTORY_CHAT_ID}")
            self.assertEqual(502, status)
            self.assertIn("获取对话详情失败", body)
        finally:
            app.get_zai_chat_detail = real[0]

    def test_purge_local_history_removes_chat_records(self) -> None:
        self._make_record("00000000-0000-0000-0000-0000000000b1", "问1", "答1")
        self._make_record("00000000-0000-0000-0000-0000000000b1", "问2", "答2")
        self._make_record("00000000-0000-0000-0000-0000000000b2", "别删我", "保留")
        removed = app.purge_local_history("00000000-0000-0000-0000-0000000000b1")
        self.assertEqual(2, removed)
        records = app.local_history_records()
        self.assertEqual(1, len(records))
        self.assertEqual("00000000-0000-0000-0000-0000000000b2", records[0]["chat_id"])

    def test_history_records_endpoints_roundtrip(self) -> None:
        record_id = self._make_record("00000000-0000-0000-0000-0000000000c1", "端点问", "端点答")
        status, body = self.request("GET", "/api/history/records")
        self.assertEqual(200, status)
        data = json.loads(body)
        self.assertTrue(data["ok"])
        self.assertEqual(1, data["count"])
        summary = data["records"][0]
        self.assertEqual(record_id, summary["id"])
        self.assertEqual("success", summary["status"])
        self.assertEqual("端点答", summary["preview"])
        self.assertEqual("openai_chat", summary["surface"])

        status, body = self.request("GET", f"/api/history/record?id={record_id}")
        self.assertEqual(200, status)
        data = json.loads(body)
        self.assertEqual("端点问", data["record"]["user_input"])
        self.assertEqual([{"role": "user", "content": "端点问"}], data["record"]["messages"])

        status, body = self.request("GET", "/api/history/record?id=req_zzzz")
        self.assertEqual(400, status)

        status, body = self.request("POST", "/api/history/record/delete", {"id": record_id})
        self.assertEqual(200, status)
        self.assertTrue(json.loads(body)["persisted"])
        status, body = self.request("GET", f"/api/history/record?id={record_id}")
        self.assertEqual(404, status)
        self.assertEqual("history_record_not_found", json.loads(body)["error"]["code"])
        self.assertIn("不存在", body)

        self._make_record("00000000-0000-0000-0000-0000000000c2", "清空问", "清空答")
        status, body = self.request("POST", "/api/history/clear", {})
        self.assertEqual(200, status)
        clear_payload = json.loads(body)
        self.assertEqual(1, clear_payload["removed"])
        self.assertTrue(clear_payload["persisted"])
        status, body = self.request("GET", "/api/history/records")
        self.assertEqual(0, json.loads(body)["count"])

    def test_history_delete_and_clear_report_persistence_failure(self) -> None:
        provider_key = "ghp_" + ("h" * 24)
        windows_path = "C:" + "\\Users\\history-user\\history.local.json"
        leaked = f"index write failed token={provider_key} at '{windows_path}'"
        original_write = app._history_write_atomic_locked

        def fail_index(path: Path, body: str) -> None:
            if path == app.HISTORY_STORE_PATH:
                raise OSError(leaked)
            original_write(path, body)

        record_id = self._make_record(
            "00000000-0000-0000-0000-0000000000c3",
            "删除持久化失败",
            "回答",
        )
        app._history_write_atomic_locked = fail_index
        try:
            status, raw = self.request("POST", "/api/history/record/delete", {"id": record_id})
            self.assertEqual(200, status, raw[:500])
            payload = json.loads(raw)
            self.assertEqual(1, payload["removed"])
            self.assertFalse(payload["persisted"])
            self.assertIn("重启后可能重新出现", payload["message"])
            self.assertNotIn(provider_key, raw)
            self.assertNotIn("history-user", raw)
            store = app.history_store_status()
            self.assertFalse(store["persisted"])
            self.assertEqual(1, store["pending_deletes"])
            self.assertIn("redacted", store["error"])
        finally:
            app._history_write_atomic_locked = original_write

        with app._HISTORY_LOCK:
            self.assertTrue(app._history_persist_locked())
        self.assertTrue(app.history_store_status()["persisted"])

        self._make_record(
            "00000000-0000-0000-0000-0000000000c4",
            "清空持久化失败",
            "回答",
        )
        app._history_write_atomic_locked = fail_index
        try:
            status, raw = self.request("POST", "/api/history/clear", {})
            self.assertEqual(200, status, raw[:500])
            payload = json.loads(raw)
            self.assertEqual(1, payload["removed"])
            self.assertFalse(payload["persisted"])
            self.assertIn("重启后可能重新出现", payload["message"])
            self.assertEqual(1, app.history_store_status()["pending_deletes"])
        finally:
            app._history_write_atomic_locked = original_write

        with app._HISTORY_LOCK:
            self.assertTrue(app._history_persist_locked())
        self.assertTrue(app.history_store_status()["persisted"])

    def test_stream_success_records_local_mirror(self) -> None:
        state = fake_state()

        class FakeResp:
            def __init__(self, chunks: list[bytes]):
                self._chunks = chunks

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def __iter__(self):
                return iter(self._chunks)

        chunks = [
            'data: {"data":{"delta_content":"回答A","phase":"answer"}}\n\n'.encode("utf-8"),
            'data: {"data":{"delta_content":"想了一下的内容","phase":"thinking"}}\n\n'.encode("utf-8"),
            'data: {"data":{"delta_content":"回答B","phase":"answer"}}\n\n'.encode("utf-8"),
            b'data: {"data":"[DONE]"}\n\n',
        ]
        real = (app.new_chat, app.delete_zai_chat, app.urlopen)
        app.new_chat = lambda _s, _p, options=None: ("00000000-0000-0000-0000-0000000000aa", "u1")
        app.delete_zai_chat = lambda _s, _c, **_kwargs: True
        app.urlopen = lambda _req, timeout=None: FakeResp(chunks)
        try:
            list(
                self.original_stream(
                    state,
                    "镜像是哪来的",
                    options=app.ChatOptions(model="glm-5.2"),
                    retry_wait_sec=0,
                    history_ctx={
                        "surface": "openai_chat",
                        "stream": True,
                        "user_input": "镜像是哪来的",
                        "messages": [{"role": "user", "content": "镜像是哪来的"}],
                        "context_text": "历史上下文",
                    },
                )
            )
        finally:
            app.new_chat, app.delete_zai_chat, app.urlopen = real
        records = app.local_history_records()
        self.assertEqual(1, len(records))
        record = records[0]
        self.assertEqual("success", record["status"], "answer 与 thinking 应按 phase 分流")
        self.assertEqual("回答A回答B", record["content"])
        self.assertEqual("想了一下的内容", record["reasoning"])
        self.assertEqual("镜像是哪来的", record["user_input"])
        self.assertEqual("历史上下文", record["history_text"])
        self.assertEqual("镜像是哪来的", record["final_prompt"])
        self.assertEqual("openai_chat", record["surface"])
        self.assertEqual([{"role": "user", "content": "镜像是哪来的"}], record["messages"])
        self.assertTrue(record["completed_at"] > 0)
        self.assertEqual(200, record["status_code"])

    def test_complete_stream_output_budget_marks_history_error_and_cleans_chat(self) -> None:
        class FakeResp:
            def __init__(self, chunks: list[bytes]):
                self._chunks = chunks
                self.closed = False

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.closed = True
                return False

            def __iter__(self):
                return iter(self._chunks)

            def close(self):
                self.closed = True

        chat_id = "00000000-0000-0000-0000-0000000000ad"
        response = FakeResp(
            [
                b'data: {"data":{"delta_content":"12345678","phase":"answer"}}\n\n',
                b'data: {"data":{"delta_content":"ABCDE","phase":"answer"}}\n\n',
            ]
        )
        previous = (
            app.new_chat,
            app.urlopen,
            app.stream_zai_completion,
            app.MAX_UPSTREAM_STREAM_OUTPUT_BYTES,
        )
        try:
            app.new_chat = lambda _state, _prompt, options=None: (chat_id, "user-message-id")
            app.urlopen = lambda _request, timeout=None: response
            app.stream_zai_completion = self.original_stream
            app.MAX_UPSTREAM_STREAM_OUTPUT_BYTES = 10
            status, raw = self.request(
                "POST",
                "/v1/chat/completions",
                {
                    "model": "glm-5.2",
                    "messages": [{"role": "user", "content": "输出预算"}],
                    "stream": False,
                },
            )
        finally:
            (
                app.new_chat,
                app.urlopen,
                app.stream_zai_completion,
                app.MAX_UPSTREAM_STREAM_OUTPUT_BYTES,
            ) = previous
        self.assertEqual(502, status, raw[:500])
        self.assertIn("超过 10", raw)
        self.assertTrue(response.closed)
        self.assertEqual([chat_id], self.deleted_chats)
        records = app.local_history_records()
        self.assertEqual(1, len(records))
        self.assertEqual("error", records[0]["status"])
        self.assertEqual("12345678", records[0]["content"])
        self.assertIn("超过 10", records[0]["error"])

    def test_protocol_stream_adapters_enforce_secondary_output_budget(self) -> None:
        previous_stream = app.stream_zai_completion
        previous_limit = app.MAX_UPSTREAM_STREAM_OUTPUT_BYTES
        stream_closed = 0
        stream_calls = 0

        def oversized_stream(_state, _prompt, **kwargs):
            nonlocal stream_closed, stream_calls
            stream_calls += 1
            chat_id = f"00000000-0000-0000-0000-{stream_calls:012d}"
            context = kwargs.get("context_out")
            if isinstance(context, dict):
                context["chat_id"] = chat_id
            try:
                yield 'data: {"data":{"delta_content":"12345678","phase":"answer"}}'
                yield 'data: {"data":{"delta_content":"ABCDE","phase":"answer"}}'
            finally:
                stream_closed += 1

        cases = [
            (
                "/v1/chat/completions",
                {"model": "glm-5.2", "messages": [{"role": "user", "content": "hi"}], "stream": True},
            ),
            ("/v1/responses", {"model": "glm-5.2", "input": "hi", "stream": True}),
            (
                "/v1/messages",
                {
                    "model": "glm-5.2",
                    "max_tokens": 32,
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": True,
                    "include_thinking": False,
                },
            ),
        ]
        try:
            app.stream_zai_completion = oversized_stream
            app.MAX_UPSTREAM_STREAM_OUTPUT_BYTES = 10
            results = [self.request("POST", path, body) for path, body in cases]
        finally:
            app.stream_zai_completion = previous_stream
            app.MAX_UPSTREAM_STREAM_OUTPUT_BYTES = previous_limit
        for status, raw in results:
            self.assertEqual(200, status, raw[:500])
            self.assertIn("超过 10", raw)
        self.assertEqual(3, stream_closed)
        self.assertEqual(3, len(self.deleted_chats))

    def test_stream_budget_counts_utf8_wire_events_and_bounded_prefix(self) -> None:
        previous = (
            app.MAX_UPSTREAM_STREAM_WIRE_BYTES,
            app.MAX_UPSTREAM_STREAM_OUTPUT_BYTES,
            app.MAX_UPSTREAM_STREAM_EVENTS,
        )
        try:
            app.MAX_UPSTREAM_STREAM_WIRE_BYTES = 32
            app.MAX_UPSTREAM_STREAM_OUTPUT_BYTES = 32
            app.MAX_UPSTREAM_STREAM_EVENTS = 1
            budget = app.UpstreamStreamBudget()
            budget.observe_event("四")
            self.assertEqual(3, budget.wire_bytes)
            with self.assertRaisesRegex(app.UpstreamResponseTooLarge, "事件数量"):
                budget.observe_event("second")

            app.MAX_UPSTREAM_STREAM_EVENTS = 10
            app.MAX_UPSTREAM_STREAM_WIRE_BYTES = 3
            budget = app.UpstreamStreamBudget()
            budget.observe_event("四")
            with self.assertRaisesRegex(app.UpstreamResponseTooLarge, "原始事件"):
                budget.observe_event("x")
        finally:
            (
                app.MAX_UPSTREAM_STREAM_WIRE_BYTES,
                app.MAX_UPSTREAM_STREAM_OUTPUT_BYTES,
                app.MAX_UPSTREAM_STREAM_EVENTS,
            ) = previous

        parts: list[str] = []
        retained = app.append_text_prefix(parts, "abcdef", 0, 4)
        retained = app.append_text_prefix(parts, "ignored", retained, 4)
        self.assertEqual(4, retained)
        self.assertEqual("abcd", "".join(parts))

    def test_stream_client_disconnect_still_records_partial(self) -> None:
        # ds2api 逻辑：客户端断开 / 停止生成时，已读取的部分内容（含思维链）也要落盘。
        state = fake_state()

        class FakeResp:
            def __init__(self, chunks: list[bytes]):
                self._chunks = chunks

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def __iter__(self):
                return iter(self._chunks)

        chunks = [
            'data: {"data":{"delta_content":"PARTIAL","phase":"answer"}}\n\n'.encode("utf-8"),
            'data: {"data":{"delta_content":"MORE","phase":"answer"}}\n\n'.encode("utf-8"),
            b'data: {"data":"[DONE]"}\n\n',
        ]
        real = (app.new_chat, app.delete_zai_chat, app.urlopen)
        app.new_chat = lambda _s, _p, options=None: ("00000000-0000-0000-0000-0000000000bb", "u1")
        app.delete_zai_chat = lambda _s, chat_id, **_kwargs: self.deleted_chats.append(chat_id) or True
        app.urlopen = lambda _req, timeout=None: FakeResp(chunks)
        try:
            gen = self.original_stream(
                state,
                "只读到一半就断开",
                options=app.ChatOptions(model="glm-5.2"),
                retry_wait_sec=0,
                history_ctx={
                    "surface": "openai_chat",
                    "stream": True,
                    "user_input": "只读到一半就断开",
                    "messages": [{"role": "user", "content": "只读到一半就断开"}],
                },
            )
            next(gen)  # 消费第一个事件后客户端断开
            gen.close()
        finally:
            app.new_chat, app.delete_zai_chat, app.urlopen = real
        records = app.local_history_records()
        self.assertEqual(1, len(records), "中断也应保存已读取的部分")
        record = records[0]
        self.assertEqual("stopped", record["status"])
        self.assertEqual("只读到一半就断开", record["user_input"])
        self.assertEqual("PARTIAL", record["content"])
        self.assertTrue(record["completed_at"] > 0)
        self.assertEqual(
            ["00000000-0000-0000-0000-0000000000bb"],
            self.deleted_chats,
            "直接关闭上游流时也应清理中断会话",
        )

    def test_stream_upstream_error_records_error_status(self) -> None:
        # 上游失败也留痕：status=error + 错误摘要，镜像不丢这次请求的上下文。
        state = fake_state()
        real = (app.new_chat, app.delete_zai_chat, app.urlopen)

        def boom_new_chat(_s, _p, options=None):
            raise RuntimeError("HTTP Error 500: upstream dead")

        app.new_chat = boom_new_chat
        try:
            with self.assertRaises(RuntimeError):
                list(
                    self.original_stream(
                        state,
                        "会失败的请求",
                        options=app.ChatOptions(model="glm-5.2"),
                        retry_attempts=1,
                        history_ctx={
                            "surface": "openai_chat",
                            "stream": True,
                            "user_input": "会失败的请求",
                            "messages": [{"role": "user", "content": "会失败的请求"}],
                        },
                    )
                )
        finally:
            app.new_chat, app.delete_zai_chat, app.urlopen = real
        records = app.local_history_records()
        self.assertEqual(1, len(records))
        record = records[0]
        self.assertEqual("error", record["status"])
        self.assertIn("upstream dead", record["error"])
        self.assertEqual("", record["content"])

    def test_v1_mirror_file_is_adapted(self) -> None:
        v1 = {
            "schema": "glm2api.history.v1",
            "records": [
                {
                    "chat_id": "00000000-0000-0000-0000-00000000d001",
                    "model": "glm-5.2",
                    "timestamp": 1787938848,
                    "title": "旧问",
                    "prompt": "旧问",
                    "answer": "旧答",
                    "thinking": "旧想",
                }
            ],
        }
        app.HISTORY_STORE_PATH.write_text(json.dumps(v1, ensure_ascii=False), encoding="utf-8")
        app._HISTORY_CACHE = None
        try:
            records = app.local_history_records()
            self.assertEqual(1, len(records))
            record = records[0]
            self.assertEqual("req_", record["id"][:4])
            self.assertEqual("success", record["status"])
            self.assertEqual("旧答", record["content"])
            self.assertEqual("旧想", record["reasoning"])
            # v2 记录应已迁移为 v3 索引 + detail 文件
            self.assertEqual(app.HISTORY_SCHEMA, json.loads(app.HISTORY_STORE_PATH.read_text(encoding="utf-8"))["schema"])
            self.assertTrue((app.HISTORY_DETAIL_DIR / f"{record['id']}.json").exists())
        finally:
            app.HISTORY_STORE_PATH.unlink(missing_ok=True)
            if app.HISTORY_DETAIL_DIR.exists():
                for path in app.HISTORY_DETAIL_DIR.glob("req_*.json"):
                    path.unlink(missing_ok=True)
            app._HISTORY_CACHE = None
            app._HISTORY_DIRTY.clear()
            app._HISTORY_DELETED.clear()

    def test_stream_reuses_record_across_history_ctx_attempts(self) -> None:
        # 面板降级重试共用同一个 history_ctx：两次调用应复用同一条记录，最终只留终态。
        state = fake_state()

        class FakeResp:
            def __init__(self, chunks: list[bytes]):
                self._chunks = chunks

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def __iter__(self):
                return iter(self._chunks)

        chunks = [
            'data: {"data":{"delta_content":"第二次成功","phase":"answer"}}\n\n'.encode("utf-8"),
            b'data: {"data":{"done":true,"phase":"answer"},"type":"chat:completion"}\n\n',
        ]
        real = (app.new_chat, app.delete_zai_chat, app.urlopen)
        calls: list[str] = []

        def flaky_new_chat(_s, _p, options=None):
            calls.append("new_chat")
            if len(calls) == 1:
                raise RuntimeError("chat not found: 会话已被上游清理")
            return ("00000000-0000-0000-0000-0000000000cc", "u2")

        app.new_chat = flaky_new_chat
        app.delete_zai_chat = lambda _s, _c, **_kwargs: True
        app.urlopen = lambda _req, timeout=None: FakeResp(chunks)
        history_ctx = {
            "surface": "panel_chat",
            "stream": True,
            "user_input": "降级重试",
            "messages": [{"role": "user", "content": "降级重试"}],
        }
        try:
            # 第一次：失败（generate 异常抛出）
            with self.assertRaises(RuntimeError):
                list(
                    self.original_stream(
                        state, "降级重试", options=app.ChatOptions(model="glm-5.2"), retry_attempts=1, history_ctx=history_ctx
                    )
                )
            # 第二次：同 ctx 复用记录并成功
            list(
                self.original_stream(
                    state, "降级重试", options=app.ChatOptions(model="glm-5.2"), retry_attempts=1, history_ctx=history_ctx
                )
            )
        finally:
            app.new_chat, app.delete_zai_chat, app.urlopen = real
        records = app.local_history_records()
        self.assertEqual(1, len(records), "同一 history_ctx 应复用同一条镜像记录")
        record = records[0]
        self.assertEqual("success", record["status"])
        self.assertEqual("第二次成功", record["content"])
        self.assertEqual("panel_chat", record["surface"])


    def test_delete_404_counts_as_deleted(self) -> None:
        original_http_json = app.http_json
        state = fake_state()

        def fake_http_json(_method, url, _headers, _payload=None, **_kwargs):
            raise app.HTTPError(url, 404, "Not Found", {}, io.BytesIO(b"gone"))

        try:
            app.http_json = fake_http_json
            self.assertTrue(app.delete_zai_chat(state, "00000000-0000-0000-0000-000000000001"))
        finally:
            app.http_json = original_http_json

    def test_stream_retries_transient_new_chat_error(self) -> None:
        state = fake_state()
        new_chat_calls: list[int] = []

        def flaky_new_chat(_state, _prompt, options=None):
            new_chat_calls.append(1)
            if len(new_chat_calls) == 1:
                raise RuntimeError("创建 chat 失败: HTTP Error 429: too many requests")
            return "00000000-0000-0000-0000-000000000002", "u2"

        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def __iter__(self):
                return iter(
                    [
                        b'data: {"data":{"delta_content":"OK","phase":"answer"}}\n\n',
                        b'data: {"data":{"done":true,"phase":"answer"},"type":"chat:completion"}\n\n',
                    ]
                )

        real = (app.new_chat, app.urlopen)
        app.new_chat = flaky_new_chat
        app.urlopen = lambda _req, timeout=None: FakeResp()
        try:
            events = list(
                self.original_stream(
                    state,
                    "hi",
                    options=app.ChatOptions(model="glm-5.2"),
                    retry_wait_sec=0,
                    retry_attempts=3,
                )
            )
        finally:
            app.new_chat, app.urlopen = real
        self.assertEqual(2, len(new_chat_calls), "new_chat 瞬时错误应重试")
        self.assertIn("OK", "".join(events))

    def test_auto_delete_submission_runs_in_background_by_default(self) -> None:
        done = threading.Event()
        prev_inline = app._AUTO_DELETE_INLINE
        try:
            app._AUTO_DELETE_INLINE = False
            app._submit_auto_delete(done.set)
            self.assertTrue(done.wait(timeout=5), "后台删除任务应在线程池中执行")
        finally:
            app._shutdown_auto_delete_executor()
            app._DELETE_EXECUTOR_CLOSED = False
            app._AUTO_DELETE_INLINE = prev_inline

    def test_auto_delete_shutdown_drains_tasks_already_in_queue(self) -> None:
        blocker_started = threading.Event()
        release_blocker = threading.Event()
        queued_done = threading.Event()
        prev_inline = app._AUTO_DELETE_INLINE

        def blocker() -> None:
            blocker_started.set()
            release_blocker.wait(timeout=5)

        try:
            app._shutdown_auto_delete_executor()
            app._DELETE_EXECUTOR_CLOSED = False
            app._AUTO_DELETE_INLINE = False
            self.assertTrue(app._submit_auto_delete(blocker))
            self.assertTrue(blocker_started.wait(timeout=2))
            self.assertTrue(app._submit_auto_delete(queued_done.set))
            app._shutdown_auto_delete_executor()
            release_blocker.set()
            self.assertTrue(queued_done.wait(timeout=5), "服务关闭不能取消已接收的会话删除任务")
        finally:
            release_blocker.set()
            app._DELETE_EXECUTOR_CLOSED = False
            app._AUTO_DELETE_INLINE = prev_inline

    def test_auto_delete_queue_uses_bounded_inline_backpressure(self) -> None:
        blocker_started = threading.Event()
        release_blocker = threading.Event()
        inline_done = threading.Event()
        prev_inline = app._AUTO_DELETE_INLINE
        prev_limit = app.AUTO_DELETE_MAX_PENDING
        before = app.auto_delete_executor_status()["backpressure_total"]

        def blocker() -> None:
            blocker_started.set()
            release_blocker.wait(timeout=5)

        try:
            app._shutdown_auto_delete_executor()
            app._DELETE_EXECUTOR_CLOSED = False
            app._AUTO_DELETE_INLINE = False
            app.AUTO_DELETE_MAX_PENDING = 1
            self.assertTrue(app._submit_auto_delete(blocker))
            self.assertTrue(blocker_started.wait(timeout=2))
            self.assertEqual(1, app.auto_delete_executor_status()["pending"])

            caller_thread = threading.current_thread().name
            executed_on: list[str] = []

            def fallback() -> None:
                executed_on.append(threading.current_thread().name)
                inline_done.set()

            self.assertTrue(app._submit_auto_delete(fallback))
            self.assertTrue(inline_done.is_set())
            self.assertEqual([caller_thread], executed_on)
            status = app.auto_delete_executor_status()
            self.assertEqual(1, status["pending"])
            self.assertEqual(before + 1, status["backpressure_total"])
            self.assertTrue(status["saturated"])
        finally:
            release_blocker.set()
            deadline = time.time() + 5
            while app.auto_delete_executor_status()["pending"] and time.time() < deadline:
                time.sleep(0.01)
            app._shutdown_auto_delete_executor()
            app._DELETE_EXECUTOR_CLOSED = False
            app._AUTO_DELETE_INLINE = prev_inline
            app.AUTO_DELETE_MAX_PENDING = prev_limit

    def test_journaled_file_cleanup_defers_instead_of_blocking_when_queue_is_full(self) -> None:
        blocker_started = threading.Event()
        release = threading.Event()
        prev_inline = app._AUTO_DELETE_INLINE
        prev_limit = app.AUTO_DELETE_MAX_PENDING
        original_delete = app.delete_zai_file

        def blocker() -> None:
            blocker_started.set()
            release.wait(timeout=5)

        def must_not_run(*_args, **_kwargs):
            raise AssertionError("full durable queue must not run cleanup inline")

        try:
            app._shutdown_auto_delete_executor()
            app._DELETE_EXECUTOR_CLOSED = False
            app._AUTO_DELETE_STOP.clear()
            app._AUTO_DELETE_INLINE = False
            app.AUTO_DELETE_MAX_PENDING = 1
            app.delete_zai_file = must_not_run
            self.assertTrue(app._submit_auto_delete(blocker))
            self.assertTrue(blocker_started.wait(timeout=2))
            started = time.monotonic()
            scheduled = app._best_effort_delete_upstream_files(
                fake_state(),
                ["file_deferred_1"],
                reason="failed_chat",
            )
            self.assertFalse(scheduled)
            self.assertLess(time.monotonic() - started, 0.5)
            status = app.auto_delete_executor_status()
            self.assertEqual(1, status["pending"])
            self.assertEqual(1, status["journal_file_pending"])
        finally:
            release.set()
            deadline = time.monotonic() + 5
            while app.auto_delete_executor_status()["pending"] and time.monotonic() < deadline:
                time.sleep(0.01)
            app._shutdown_auto_delete_executor()
            app._DELETE_EXECUTOR_CLOSED = False
            app._AUTO_DELETE_STOP.clear()
            app._AUTO_DELETE_INLINE = prev_inline
            app.AUTO_DELETE_MAX_PENDING = prev_limit
            app.delete_zai_file = original_delete

    def test_pending_delete_journal_persists_failure_and_removes_success(self) -> None:
        state = fake_state()
        chat_id = "00000000-0000-0000-0000-000000000091"
        record_id = app.pending_chat_delete_add(state, chat_id, "client_disconnect")
        self.assertTrue(record_id)
        self.assertTrue(app.PENDING_DELETE_STORE_PATH.exists())
        self.assertEqual(1, app.pending_chat_delete_status()["journal_pending"])

        app.pending_chat_delete_failed(record_id, RuntimeError("temporary delete failure"))
        stored = json.loads(app.PENDING_DELETE_STORE_PATH.read_text(encoding="utf-8"))
        self.assertEqual(1, stored["items"][0]["attempts"])
        self.assertIn("temporary delete failure", stored["items"][0]["last_error"])

        app.pending_chat_delete_completed(record_id)
        self.assertEqual(0, app.pending_chat_delete_status()["journal_pending"])
        self.assertFalse(app.PENDING_DELETE_STORE_PATH.exists())

    def test_pending_delete_store_read_budget_fails_closed(self) -> None:
        app.PENDING_DELETE_STORE_PATH.write_bytes(b"x" * (app.MAX_PENDING_DELETE_STORE_BYTES + 1))
        app._PENDING_DELETE_CACHE = None
        status = app.pending_chat_delete_status()
        self.assertEqual(0, status["journal_pending"])
        self.assertTrue(status["journal_store_error"])
        self.assertEqual(app.MAX_PENDING_DELETE_STORE_BYTES, status["journal_store_max_bytes"])

    def test_pending_delete_replay_matches_account_and_clears_journal(self) -> None:
        state = fake_state()
        chat_id = "00000000-0000-0000-0000-000000000092"
        app.pending_chat_delete_add(state, chat_id, "service_shutdown")
        profile = app.make_profile(state, label="replay", source="test")
        original_delete = app.delete_zai_chat
        calls: list[tuple[str, bool]] = []

        def fake_delete(_state, replay_chat_id, **kwargs):
            cancel_check = kwargs.get("cancel_check")
            calls.append((replay_chat_id, callable(cancel_check)))
            return True

        try:
            app.delete_zai_chat = fake_delete
            result = app.replay_pending_chat_deletes({profile.id: profile})
        finally:
            app.delete_zai_chat = original_delete
        self.assertEqual({"retained": 1, "scheduled": 1, "unmatched": 0}, result)
        self.assertEqual([(chat_id, True)], calls)
        self.assertEqual(0, app.pending_chat_delete_status()["journal_pending"])

    def test_pending_delete_replay_retains_unmatched_account(self) -> None:
        state = fake_state()
        chat_id = "00000000-0000-0000-0000-000000000093"
        app.pending_chat_delete_add(state, chat_id, "auto_delete")
        result = app.replay_pending_chat_deletes({})
        self.assertEqual({"retained": 1, "scheduled": 0, "unmatched": 1}, result)
        self.assertEqual(1, app.pending_chat_delete_status()["journal_pending"])

    def test_pending_file_delete_replay_uses_same_durable_journal(self) -> None:
        state = fake_state()
        file_id = "file_replay_1"
        app.pending_file_delete_add(state, file_id, "failed_chat")
        profile = app.make_profile(state, label="file-replay", source="test")
        original_delete = app.delete_zai_file
        calls: list[tuple[str, bool]] = []

        def fake_delete(_state, replay_file_id, **kwargs):
            calls.append((replay_file_id, callable(kwargs.get("cancel_check"))))
            return True

        try:
            app.delete_zai_file = fake_delete
            result = app.replay_pending_deletes({profile.id: profile})
        finally:
            app.delete_zai_file = original_delete
        self.assertEqual({"retained": 1, "scheduled": 1, "unmatched": 0}, result)
        self.assertEqual([(file_id, True)], calls)
        status = app.pending_chat_delete_status()
        self.assertEqual(0, status["journal_pending"])
        self.assertEqual(0, status["journal_file_pending"])

    def test_pending_delete_replay_feeder_processes_more_than_queue_capacity(self) -> None:
        state = fake_state()
        app.pending_resource_deletes_add(
            state,
            [("file", f"file_feeder_{index}") for index in range(3)],
            "failed_chat",
        )
        profile = app.make_profile(state, label="feeder", source="test")
        original_delete = app.delete_zai_file
        original_inline = app._AUTO_DELETE_INLINE
        original_limit = app.AUTO_DELETE_MAX_PENDING
        first_started = threading.Event()
        release_first = threading.Event()
        calls: list[str] = []
        calls_lock = threading.Lock()

        def fake_delete(_state, file_id, **_kwargs):
            with calls_lock:
                calls.append(file_id)
                first = len(calls) == 1
            if first:
                first_started.set()
                release_first.wait(timeout=5)
            return True

        try:
            app._shutdown_auto_delete_executor()
            app._DELETE_EXECUTOR_CLOSED = False
            app._AUTO_DELETE_STOP.clear()
            app._AUTO_DELETE_INLINE = False
            app.AUTO_DELETE_MAX_PENDING = 1
            app.delete_zai_file = fake_delete
            result = app.replay_pending_deletes({profile.id: profile})
            self.assertEqual({"retained": 3, "scheduled": 0, "unmatched": 0}, result)
            self.assertTrue(first_started.wait(timeout=2))
            counter_deadline = time.monotonic() + 2
            status = app.pending_chat_delete_status()
            while status["replay_deferred"] == 3 and time.monotonic() < counter_deadline:
                time.sleep(0.01)
                status = app.pending_chat_delete_status()
            self.assertTrue(status["replay_active"])
            self.assertEqual(2, status["replay_deferred"])
            release_first.set()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                status = app.pending_chat_delete_status()
                if status["journal_pending"] == 0 and not status["replay_active"]:
                    break
                time.sleep(0.01)
            self.assertEqual(0, status["journal_pending"])
            self.assertFalse(status["replay_active"])
            self.assertEqual(0, status["replay_deferred"])
            self.assertEqual(3, status["replay_scheduled"])
            self.assertEqual(
                {"file_feeder_0", "file_feeder_1", "file_feeder_2"},
                set(calls),
            )
        finally:
            release_first.set()
            app._shutdown_auto_delete_executor(1.0, cancel_pending=True)
            app._DELETE_EXECUTOR_CLOSED = False
            app._AUTO_DELETE_STOP.clear()
            app._AUTO_DELETE_INLINE = original_inline
            app.AUTO_DELETE_MAX_PENDING = original_limit
            app.delete_zai_file = original_delete

    def test_auto_delete_shutdown_stops_waiting_replay_feeder(self) -> None:
        state = fake_state()
        app.pending_resource_deletes_add(
            state,
            [("file", "file_shutdown_feeder_1"), ("file", "file_shutdown_feeder_2")],
            "failed_chat",
        )
        profile = app.make_profile(state, label="shutdown-feeder", source="test")
        original_inline = app._AUTO_DELETE_INLINE
        original_limit = app.AUTO_DELETE_MAX_PENDING
        blocker_started = threading.Event()
        release = threading.Event()

        def blocker() -> None:
            blocker_started.set()
            release.wait(timeout=5)

        try:
            app._shutdown_auto_delete_executor()
            app._DELETE_EXECUTOR_CLOSED = False
            app._AUTO_DELETE_STOP.clear()
            app._AUTO_DELETE_INLINE = False
            app.AUTO_DELETE_MAX_PENDING = 1
            self.assertTrue(app._submit_auto_delete(blocker))
            self.assertTrue(blocker_started.wait(timeout=2))
            app.replay_pending_deletes({profile.id: profile})
            active_deadline = time.monotonic() + 2
            status = app.pending_chat_delete_status()
            while not status["replay_active"] and time.monotonic() < active_deadline:
                time.sleep(0.01)
                status = app.pending_chat_delete_status()
            self.assertTrue(status["replay_active"])
            self.assertEqual(2, status["replay_deferred"])
            result = app._shutdown_auto_delete_executor()
            self.assertTrue(result["replay_stopped"])
            status = app.pending_chat_delete_status()
            self.assertFalse(status["replay_active"])
            self.assertEqual(2, status["replay_deferred"])
            self.assertEqual(2, status["journal_file_pending"])
        finally:
            release.set()
            deadline = time.monotonic() + 5
            while app.auto_delete_executor_status()["pending"] and time.monotonic() < deadline:
                time.sleep(0.01)
            app._DELETE_EXECUTOR_CLOSED = False
            app._AUTO_DELETE_STOP.clear()
            app._AUTO_DELETE_INLINE = original_inline
            app.AUTO_DELETE_MAX_PENDING = original_limit

    def test_pending_delete_v1_chat_store_migrates_to_v2_resource_schema(self) -> None:
        state = fake_state()
        chat_id = "00000000-0000-0000-0000-000000000095"
        legacy = {
            "schema": app.PENDING_DELETE_LEGACY_SCHEMA,
            "items": [
                {
                    "id": "legacy-id-is-recomputed",
                    "account_fp": app.sha16(state.user_id),
                    "chat_id": chat_id,
                    "reason": "service_shutdown",
                    "created_at": int(time.time() * 1000),
                }
            ],
        }
        app.PENDING_DELETE_STORE_PATH.write_text(json.dumps(legacy), encoding="utf-8")
        app._PENDING_DELETE_CACHE = None
        status = app.pending_chat_delete_status()
        self.assertEqual(1, status["journal_chat_pending"])
        self.assertEqual(0, status["journal_file_pending"])

        app.pending_file_delete_add(state, "file_migration_1", "failed_chat")
        migrated = json.loads(app.PENDING_DELETE_STORE_PATH.read_text(encoding="utf-8"))
        self.assertEqual(app.PENDING_DELETE_SCHEMA, migrated["schema"])
        self.assertEqual({"chat", "file"}, {item["kind"] for item in migrated["items"]})
        self.assertTrue(all("resource_id" in item for item in migrated["items"]))

    def test_pending_file_batch_uses_one_atomic_journal_write(self) -> None:
        original_persist = app._pending_delete_persist_locked
        writes = 0

        def counted_persist() -> bool:
            nonlocal writes
            writes += 1
            return original_persist()

        try:
            app._pending_delete_persist_locked = counted_persist
            added = app.pending_resource_deletes_add(
                fake_state(),
                [("file", "file_batch_1"), ("file", "file_batch_2"), ("file", "file_batch_3")],
                "failed_chat",
            )
        finally:
            app._pending_delete_persist_locked = original_persist
        self.assertEqual(3, len(added))
        self.assertEqual(1, writes)
        self.assertEqual(3, app.pending_chat_delete_status()["journal_file_pending"])

    def test_pending_delete_capacity_preserves_chat_before_orphan_files(self) -> None:
        original_limit = app.PENDING_DELETE_MAX_RECORDS
        state = fake_state()
        try:
            app.PENDING_DELETE_MAX_RECORDS = 2
            app.pending_file_delete_add(state, "file_evict_first", "failed_chat")
            app.pending_chat_delete_add(
                state,
                "00000000-0000-0000-0000-000000000096",
                "service_shutdown",
            )
            app.pending_chat_delete_add(
                state,
                "00000000-0000-0000-0000-000000000097",
                "service_shutdown",
            )
            status = app.pending_chat_delete_status()
            self.assertEqual(2, status["journal_chat_pending"])
            self.assertEqual(0, status["journal_file_pending"])
        finally:
            app.PENDING_DELETE_MAX_RECORDS = original_limit

    def test_failed_orphan_file_cleanup_remains_in_restart_journal(self) -> None:
        original_delete = app.delete_zai_file
        state = fake_state()

        def failing_delete(_state, _file_id, **_kwargs):
            raise app.UpstreamRequestError("temporary file cleanup outage")

        handler = QuietProxyHandler.__new__(QuietProxyHandler)
        try:
            app.delete_zai_file = failing_delete
            handler._cleanup_failed_upstream_files(
                state,
                [{"id": "file_orphan_1"}, {"file": {"id": "file_orphan_2"}}],
            )
        finally:
            app.delete_zai_file = original_delete
        status = app.pending_chat_delete_status()
        self.assertEqual(2, status["journal_pending"])
        self.assertEqual(2, status["journal_file_pending"])
        stored = json.loads(app.PENDING_DELETE_STORE_PATH.read_text(encoding="utf-8"))
        self.assertEqual({1}, {item["attempts"] for item in stored["items"]})

    def test_auto_delete_bounded_shutdown_cancels_only_queued_tasks(self) -> None:
        prev_inline = app._AUTO_DELETE_INLINE
        started = threading.Barrier(app.AUTO_DELETE_WORKERS + 1)
        release = threading.Event()
        queued_ran = threading.Event()
        cancelled_before = app.auto_delete_executor_status()["cancelled_total"]

        def blocker() -> None:
            started.wait(timeout=2)
            release.wait(timeout=5)

        try:
            app._shutdown_auto_delete_executor()
            app._DELETE_EXECUTOR_CLOSED = False
            app._AUTO_DELETE_STOP.clear()
            app._AUTO_DELETE_INLINE = False
            for _ in range(app.AUTO_DELETE_WORKERS):
                self.assertTrue(app._submit_auto_delete(blocker))
            started.wait(timeout=2)
            self.assertTrue(app._submit_auto_delete(queued_ran.set))
            result = app._shutdown_auto_delete_executor(0.05, cancel_pending=True)
            self.assertFalse(result["drained"])
            self.assertFalse(queued_ran.is_set())
            self.assertGreaterEqual(
                app.auto_delete_executor_status()["cancelled_total"],
                cancelled_before + 1,
            )
        finally:
            release.set()
            deadline = time.monotonic() + 5
            while app.auto_delete_executor_status()["pending"] and time.monotonic() < deadline:
                time.sleep(0.01)
            app._DELETE_EXECUTOR_CLOSED = False
            app._AUTO_DELETE_STOP.clear()
            app._AUTO_DELETE_INLINE = prev_inline

    def test_delete_chat_uses_short_timeout_and_stops_before_retry(self) -> None:
        original_http_json = app.http_json
        calls: list[float] = []
        checks = 0

        def fake_http_json(_method, _url, _headers, _payload=None, **kwargs):
            calls.append(float(kwargs.get("timeout") or 0))
            raise app.URLError(TimeoutError("delete timeout"))

        def cancelled() -> bool:
            nonlocal checks
            checks += 1
            return checks >= 2

        try:
            app.http_json = fake_http_json
            with self.assertRaises(app.ServiceShuttingDown):
                self.original_delete(
                    fake_state(),
                    "00000000-0000-0000-0000-000000000094",
                    cancel_check=cancelled,
                )
        finally:
            app.http_json = original_http_json
        self.assertEqual([app.AUTO_DELETE_REQUEST_TIMEOUT_SECONDS], calls)

    def test_delete_file_uses_short_timeout_and_honors_shutdown(self) -> None:
        original_http_json = app.http_json
        calls: list[float] = []

        def fake_http_json(_method, _url, _headers, _payload=None, **kwargs):
            calls.append(float(kwargs.get("timeout") or 0))
            raise app.URLError(TimeoutError("file delete timeout"))

        try:
            app.http_json = fake_http_json
            with self.assertRaises(app.URLError):
                app.delete_zai_file(fake_state(), "file_timeout_1")
            with self.assertRaises(app.ServiceShuttingDown):
                app.delete_zai_file(
                    fake_state(),
                    "file_timeout_2",
                    cancel_check=lambda: True,
                )
        finally:
            app.http_json = original_http_json
        self.assertEqual([app.AUTO_DELETE_REQUEST_TIMEOUT_SECONDS], calls)

    def test_api_surfaces_return_thinking_by_default(self) -> None:
        """API 协议面默认回传思维链；请求体 include_thinking:false 可显式关闭。"""
        original_stream = app.stream_zai_completion

        def thinking_stream(_state, _prompt, **_kwargs):
            context = _kwargs.get("context_out")
            if isinstance(context, dict):
                context.update({"chat_id": "00000000-0000-0000-0000-0000000000cc"})
            yield 'data: {"data":{"delta_content":"想一下","phase":"thinking"}}'
            yield 'data: {"data":{"delta_content":"答案","phase":"answer"}}'

        try:
            app.stream_zai_completion = thinking_stream

            # OpenAI chat 非流式：message 带 reasoning_content
            status, raw = self.request(
                "POST",
                "/v1/chat/completions",
                {"model": "glm-5.2", "messages": [{"role": "user", "content": "hi"}]},
            )
            data = json.loads(raw)
            self.assertEqual(200, status)
            message = data["choices"][0]["message"]
            self.assertEqual("答案", message["content"])
            self.assertEqual("想一下", message.get("reasoning_content"))

            # OpenAI chat 显式关闭：不带 reasoning_content
            status, raw = self.request(
                "POST",
                "/v1/chat/completions",
                {
                    "model": "glm-5.2",
                    "messages": [{"role": "user", "content": "hi"}],
                    "include_thinking": False,
                },
            )
            message = json.loads(raw)["choices"][0]["message"]
            self.assertNotIn("reasoning_content", message)

            # OpenAI chat 流式：默认出现 reasoning_content 增量
            status, raw = self.request(
                "POST",
                "/v1/chat/completions",
                {"model": "glm-5.2", "messages": [{"role": "user", "content": "hi"}], "stream": True},
            )
            self.assertEqual(200, status)
            self.assertIn('"reasoning_content"', raw)
            self.assertIn("想一下", raw)

            # Anthropic 非流式：content 首块为 thinking
            status, raw = self.request(
                "POST",
                "/anthropic/v1/messages",
                {"model": "glm-5.2", "max_tokens": 100, "messages": [{"role": "user", "content": "hi"}]},
            )
            data = json.loads(raw)
            self.assertEqual(200, status)
            self.assertEqual("thinking", data["content"][0]["type"])
            self.assertEqual("想一下", data["content"][0]["thinking"])
            self.assertEqual("text", data["content"][1]["type"])

            # Anthropic 显式关闭：无 thinking 块
            status, raw = self.request(
                "POST",
                "/anthropic/v1/messages",
                {
                    "model": "glm-5.2",
                    "max_tokens": 100,
                    "messages": [{"role": "user", "content": "hi"}],
                    "include_thinking": False,
                },
            )
            content = json.loads(raw)["content"]
            self.assertTrue(all(block["type"] != "thinking" for block in content))

            # Responses 非流式：output 含 reasoning 项 + summary
            status, raw = self.request(
                "POST",
                "/v1/responses",
                {"model": "glm-5.2", "input": "hi"},
            )
            data = json.loads(raw)
            self.assertEqual(200, status)
            types = [item["type"] for item in data["output"]]
            self.assertIn("reasoning", types)
            self.assertEqual("想一下", data["reasoning"]["summary"][0]["text"])
        finally:
            app.stream_zai_completion = original_stream

    def test_protocol_http_surfaces_and_context_files(self) -> None:
        openai_tools = [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "天气",
                    "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
                },
            }
        ]
        status, raw = self.request("GET", "/v1/models")
        self.assertEqual(200, status)
        self.assertEqual(list(app.ADVERTISED_MODELS), [item["id"] for item in json.loads(raw)["data"]])
        status, raw = self.request("GET", "/anthropic/v1/models")
        self.assertEqual(200, status)
        self.assertEqual(list(app.ADVERTISED_MODELS), [item["id"] for item in json.loads(raw)["data"]])
        status, raw = self.request("GET", "/api/status")
        self.assertEqual(200, status)
        self.assertTrue(json.loads(raw)["default_options"]["delete_chat_after_completion"])

        status, raw = self.request(
            "POST",
            "/v1/chat/completions",
            {"model": "glm-5.2", "messages": [{"role": "user", "content": "北京天气"}], "tools": openai_tools},
        )
        chat = json.loads(raw)
        self.assertEqual(200, status)
        self.assertEqual("tool_calls", chat["choices"][0]["finish_reason"])
        self.assertEqual("get_weather", chat["choices"][0]["message"]["tool_calls"][0]["function"]["name"])
        self.assertEqual([], self.uploads)

        status, raw = self.request(
            "POST",
            "/v1/responses",
            {
                "model": "gpt-5-codex",
                "input": "北京天气",
                "tools": [{"type": "function", "name": "get_weather", "parameters": {"type": "object"}}],
            },
        )
        response = json.loads(raw)
        self.assertEqual(200, status)
        self.assertEqual("response", response["object"])
        self.assertEqual("function_call", response["output"][-1]["type"])
        response_id = response["id"]
        status, raw = self.request("GET", "/v1/responses/" + response_id)
        self.assertEqual(200, status)
        self.assertEqual(response_id, json.loads(raw)["id"])
        self.assertEqual([], self.uploads)

        status, raw = self.request(
            "POST",
            "/v1/responses",
            {
                "model": "glm-5.2-forcehistory",
                "previous_response_id": response_id,
                "input": "继续，根据刚才的结果补充一句。",
                "tools": [{"type": "function", "name": "get_weather", "parameters": {"type": "object"}}],
            },
        )
        self.assertEqual(200, status)
        self.assertEqual("response", json.loads(raw)["object"])
        history_uploads = [context for _name, context in self.uploads if context.startswith("The dialogue up to this point.")]
        self.assertTrue(history_uploads)
        self.assertIn("北京天气", history_uploads[-1])
        self.assertIn("继续，根据刚才的结果补充一句。", history_uploads[-1])

        status, raw = self.request(
            "POST",
            "/anthropic/v1/messages",
            {
                "model": "claude-sonnet-4-6",
                "max_tokens": 128,
                "messages": [{"role": "user", "content": "北京天气"}],
                "tools": [{"name": "get_weather", "description": "天气", "input_schema": {"type": "object"}}],
            },
        )
        claude = json.loads(raw)
        self.assertEqual(200, status)
        self.assertEqual("tool_use", claude["stop_reason"])
        self.assertEqual("tool_use", claude["content"][-1]["type"])

        status, raw = self.request(
            "POST",
            "/anthropic/v1/messages/count_tokens",
            {"model": "claude-sonnet-4-6", "messages": [{"role": "user", "content": "token test"}]},
        )
        self.assertEqual(200, status)
        self.assertGreater(json.loads(raw)["input_tokens"], 0)
        self.assertEqual(2, len(self.uploads))
        self.assertTrue(any(context.startswith("The dialogue up to this point.") for _name, context in self.uploads))
        self.assertTrue(any(context.startswith("The functions listed below are available for you to invoke during this turn.") for _name, context in self.uploads))
        self.assertGreaterEqual(len(self.deleted_chats), 4)

    def test_auto_delete_defaults_to_on_and_can_be_disabled(self) -> None:
        status, raw = self.request(
            "POST",
            "/api/chat",
            {"message": "默认清理", "stream": False},
        )
        data = json.loads(raw)
        self.assertEqual(200, status)
        self.assertFalse(data["chat_deleted"], "自动删除已异步化，响应时标记为未完成")
        self.assertTrue(data["chat_delete_pending"])
        self.assertEqual(1, len(self.deleted_chats))

        status, raw = self.request(
            "POST",
            "/api/chat",
            {"message": "保留会话", "stream": False, "delete_chat_after_completion": False},
        )
        data = json.loads(raw)
        self.assertEqual(200, status)
        self.assertFalse(data["chat_deleted"])
        self.assertFalse(data["chat_delete_pending"])
        self.assertEqual(1, len(self.deleted_chats), "关闭自动删除后不应新增删除动作")

    def test_delete_uses_browser_headers_and_retries_with_auth_on_403(self) -> None:
        original_http_json = app.http_json
        calls: list[dict[str, str]] = []
        state = fake_state()
        state.device_id = "uid_testdevice"
        state.user_agent = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
        )

        def fake_http_json(_method, url, headers, _payload=None, **_kwargs):
            calls.append(dict(headers))
            if len(calls) == 1:
                raise app.HTTPError(url, 403, "Forbidden", {}, io.BytesIO(b"blocked"))
            return True

        try:
            app.http_json = fake_http_json
            self.assertTrue(self.original_delete(state, "00000000-0000-0000-0000-000000000001"))
        finally:
            app.http_json = original_http_json

        self.assertEqual(2, len(calls))
        self.assertEqual("same-origin", calls[0]["Sec-Fetch-Site"])
        self.assertEqual("cors", calls[0]["Sec-Fetch-Mode"])
        self.assertIn('v="151"', calls[0]["sec-ch-ua"])
        self.assertNotIn("Authorization", calls[0])
        self.assertEqual("Bearer test-token", calls[1]["Authorization"])
        self.assertEqual("uid_testdevice", calls[1]["X-Device-ID"])

    def test_profile_summary_merge_and_manual_compact(self) -> None:
        state_a1 = replace(fake_state(), user_id="same-user", user_name="same", token="token-a1")
        state_a2 = replace(fake_state(), user_id="same-user", user_name="same", token="token-a2")
        state_b1 = replace(fake_state(), user_id="other-user", user_name="other", token="token-b1")
        p1 = app.make_profile(state_a1, "same har", r"D:\open-reverselab\chat.z.ai.har")
        p2 = app.make_profile(state_a2, "same browser", "browser login")
        p3 = app.make_profile(state_b1, "other har", "preloaded HAR: chat.z.ai.har")

        summary = app.profile_summary(p1, same_user_count=2)
        self.assertEqual("本地 HAR: chat.z.ai.har", summary["source_display"])
        self.assertEqual("har", summary["source_type"])
        self.assertTrue(summary["duplicate_user"])
        self.assertEqual(app.sha16("same-user"), summary["user_id_fp"])
        self.assertNotIn("user_id", summary)
        for field in (
            "source",
            "har_fp",
            "loaded_at",
            "device_id_fp",
            "captcha_fp",
            "chat_id",
            "default_model",
            "supported_models",
        ):
            self.assertNotIn(field, summary)
        self.assertNotIn("open-reverselab", json.dumps(summary, ensure_ascii=False))

        profiles = {p1.id: p1, p2.id: p2, p3.id: p3}
        incoming_other = app.make_profile(
            replace(fake_state(), user_id="other-user", user_name="other", token="token-b2"),
            "other refreshed",
            "web upload",
        )
        merged_id = app.merge_profile(profiles, incoming_other)
        self.assertEqual(p3.id, merged_id)
        self.assertEqual(3, len(profiles))
        self.assertEqual("token-b2", profiles[p3.id].state.token)

        active_id, removed = app.compact_duplicate_profiles(profiles, p2.id)
        self.assertEqual(p2.id, active_id)
        self.assertEqual([p1.id], [profile.id for profile in removed])
        self.assertNotIn(p1.id, profiles)
        self.assertIn(p2.id, profiles)
        self.assertIn(p3.id, profiles)

    def test_profile_capacity_allows_refresh_but_rejects_new_account(self) -> None:
        previous_limit = app.MAX_ACCOUNT_PROFILES
        try:
            app.MAX_ACCOUNT_PROFILES = 2
            first = app.make_profile(
                replace(fake_state(), user_id="capacity-a", token="capacity-token-a"),
                "A",
                "test",
            )
            second = app.make_profile(
                replace(fake_state(), user_id="capacity-b", token="capacity-token-b"),
                "B",
                "test",
            )
            profiles = {first.id: first, second.id: second}
            refreshed = app.make_profile(
                replace(fake_state(), user_id="capacity-b", token="capacity-token-b2"),
                "B refreshed",
                "test",
            )
            self.assertEqual(second.id, app.merge_profile(profiles, refreshed))
            self.assertEqual(2, len(profiles))
            incoming = app.make_profile(
                replace(fake_state(), user_id="capacity-c", token="capacity-token-c"),
                "C",
                "test",
            )
            with self.assertRaisesRegex(app.ProfileCapacityError, "账号池已达到 2"):
                app.merge_profile(profiles, incoming)
            self.assertEqual(2, len(profiles))
        finally:
            app.MAX_ACCOUNT_PROFILES = previous_limit

    def test_login_state_fields_are_bounded_before_profile_retention(self) -> None:
        with self.assertRaisesRegex(ValueError, "token 超过"):
            app.jwt_payload_claims("x" * (app.MAX_SESSION_TOKEN_CHARS + 1))
        oversized = replace(
            fake_state(),
            user_agent="u" * (app.MAX_PROFILE_STATE_FIELD_CHARS + 1),
        )
        with self.assertRaisesRegex(ValueError, "user_agent"):
            app.make_profile(oversized, "oversized", "test")

    def test_token_auth_returns_conflict_when_profile_pool_is_full(self) -> None:
        previous_limit = app.MAX_ACCOUNT_PROFILES
        try:
            app.MAX_ACCOUNT_PROFILES = 1
            existing = app.make_profile(
                replace(fake_state(), user_id="capacity-existing", token="capacity-existing-token"),
                "existing",
                "test",
            )
            QuietProxyHandler.profiles = {existing.id: existing}
            QuietProxyHandler.active_profile_id = existing.id
            QuietProxyHandler.state = existing.state
            claims = app.base64.urlsafe_b64encode(
                json.dumps({"id": "capacity-new", "name": "new"}).encode("utf-8")
            ).decode("ascii").rstrip("=")
            status, raw = self.request(
                "POST",
                "/api/auth/token",
                {"token": f"e30.{claims}.signature", "label": "new"},
            )
            self.assertEqual(409, status, raw[:500])
            error = json.loads(raw)["error"]
            self.assertEqual("profile_capacity_reached", error["code"])
            self.assertEqual(1, error["max_profiles"])
            self.assertEqual(1, len(QuietProxyHandler.profiles))
        finally:
            app.MAX_ACCOUNT_PROFILES = previous_limit

    def test_profiles_api_does_not_expose_internal_auth_or_chat_metadata(self) -> None:
        state = replace(
            fake_state(),
            chat_id="00000000-0000-0000-0000-000000000080",
            device_id="private-device-id",
            captcha_verify_param="private-captcha",
        )
        source_path = "D:" + "\\private-workspace\\captures\\account.har"
        profile = app.make_profile(state, "managed account", source_path, har_fp="private-har-fingerprint")
        QuietProxyHandler.profiles = {profile.id: profile}
        QuietProxyHandler.active_profile_id = profile.id
        QuietProxyHandler.state = state
        status, raw = self.request("GET", "/api/auth/profiles")
        self.assertEqual(200, status, raw[:500])
        payload = json.loads(raw)
        self.assertEqual(1, payload["profile_count"])
        self.assertEqual(app.MAX_ACCOUNT_PROFILES, payload["max_profiles"])
        self.assertEqual(app.MAX_ACCOUNT_PROFILES - 1, payload["profile_slots_available"])
        self.assertFalse(payload["profile_limit_reached"])
        summary = payload["profiles"][0]
        self.assertEqual("本地 HAR: account.har", summary["source_display"])
        for forbidden in (
            state.chat_id,
            state.device_id,
            state.captcha_verify_param,
            profile.har_fp,
            "private-workspace",
        ):
            self.assertNotIn(forbidden, raw)
        self.assertNotIn("source", summary)
        self.assertNotIn("chat_id", summary)
        self.assertNotIn("supported_models", summary)

    def test_profile_switch_reports_encrypted_store_write_failure(self) -> None:
        profile_a = app.make_profile(
            replace(fake_state(), user_id="persist-a", token="persist-token-a"),
            "persist A",
            "test",
        )
        profile_b = app.make_profile(
            replace(fake_state(), user_id="persist-b", token="persist-token-b"),
            "persist B",
            "test",
        )
        QuietProxyHandler.profiles = {profile_a.id: profile_a, profile_b.id: profile_b}
        QuietProxyHandler.active_profile_id = profile_a.id
        QuietProxyHandler.state = profile_a.state
        provider_key = "ghp_" + ("p" * 24)
        windows_path = "C:" + "\\Users\\persist-user\\profiles.local.json"
        original_save = app.save_profile_store

        def failing_save(*_args, **_kwargs):
            raise OSError(f"disk full token={provider_key} at '{windows_path}'")

        app.save_profile_store = failing_save
        try:
            status, raw = self.request("POST", "/api/auth/switch", {"profile_id": profile_b.id})
            self.assertEqual(200, status, raw[:500])
            payload = json.loads(raw)
            self.assertTrue(payload["ok"])
            self.assertFalse(payload["persisted"])
            self.assertFalse(payload["profile_store"]["persisted"])
            self.assertIn("保存失败", payload["message"])
            self.assertIn("redacted", payload["profile_store_error"])
            self.assertEqual(profile_b.id, QuietProxyHandler.active_profile_id)
            self.assertIs(profile_b.state, QuietProxyHandler.state)
            self.assertNotIn(provider_key, raw)
            self.assertNotIn("persist-user", raw)

            profiles_status, profiles_raw = self.request("GET", "/api/auth/profiles")
            self.assertEqual(200, profiles_status, profiles_raw[:500])
            profiles_payload = json.loads(profiles_raw)
            self.assertFalse(profiles_payload["profile_store"]["persisted"])
            self.assertIn("redacted", profiles_payload["profile_store"]["error"])
        finally:
            app.save_profile_store = original_save

    def test_api_key_can_be_configured_from_panel(self) -> None:
        status, raw = self.request("POST", "/api/settings/api-key", {"api_key": "panel-secret-1"})
        self.assertEqual(200, status)
        data = json.loads(raw)
        self.assertTrue(data["ok"])
        self.assertTrue(data["enabled"])
        self.assertTrue(data["persisted"])
        self.assertEqual("store", data["source"])

        status, raw = self.request("GET", "/api/settings/api-key")
        self.assertEqual(200, status)
        self.assertTrue(json.loads(raw)["enabled"])

        status, raw = self.request("GET", "/api/auth/profiles")
        self.assertEqual(401, status)
        status, raw = self.request("GET", "/api/auth/profiles", headers={"X-API-Key": "panel-secret-1"})
        self.assertEqual(200, status)

        status, raw = self.request(
            "POST",
            "/api/settings/api-key",
            {"api_key": "panel-secret-2", "current_key": "wrong"},
        )
        self.assertEqual(401, status)

        status, raw = self.request(
            "POST",
            "/api/settings/api-key",
            {"api_key": "panel-secret-2", "current_key": "panel-secret-1"},
        )
        self.assertEqual(200, status)
        self.assertTrue(json.loads(raw)["enabled"])

        status, raw = self.request(
            "POST",
            "/api/settings/api-key",
            {"api_key": "", "current_key": "panel-secret-2"},
        )
        self.assertEqual(200, status)
        self.assertFalse(json.loads(raw)["enabled"])
        self.assertEqual(QuietProxyHandler.api_key, "")

    def test_api_key_configuration_rejects_oversize_and_control_characters(self) -> None:
        oversized = "k" * (app.MAX_LOCAL_API_KEY_CHARS + 1)
        for invalid in (oversized, "valid-prefix\u0000invalid-suffix"):
            status, raw = self.request("POST", "/api/settings/api-key", {"api_key": invalid})
            self.assertEqual(400, status, raw[:500])
            payload = json.loads(raw)
            self.assertEqual("invalid_api_key_config", payload["error"]["code"])
            self.assertFalse(QuietProxyHandler.api_key)
            self.assertFalse(QuietProxyHandler.api_key_store_path.exists())

        self.assertEqual("short-key", app.normalize_local_api_key("  short-key  "))
        self.assertTrue(app.local_api_keys_match("short-key", "short-key"))
        self.assertFalse(app.local_api_keys_match(oversized, "short-key"))

        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "apikey.local.json"
            with self.assertRaisesRegex(ValueError, "4096"):
                app.save_api_key_store(oversized, store_path)
            self.assertFalse(store_path.exists())

            store_path.write_bytes(b"x" * (app.MAX_API_KEY_STORE_BYTES + 1))
            loaded, saved_at, error = app.load_api_key_store(store_path)
            self.assertEqual(("", ""), (loaded, saved_at))
            self.assertIn("store exceeds", error)

            store_path.write_text(
                json.dumps(
                    {
                        "encryption": "windows-dpapi-current-user",
                        "saved_at": "2026-09-02T00:00:00+08:00",
                        "payload": app.base64.b64encode(b"encrypted").decode("ascii"),
                    }
                ),
                encoding="utf-8",
            )
            original_unprotect = app.dpapi_unprotect
            app.dpapi_unprotect = lambda _raw: json.dumps({"api_key": oversized}).encode("utf-8")
            try:
                loaded, saved_at, error = app.load_api_key_store(store_path)
            finally:
                app.dpapi_unprotect = original_unprotect
            self.assertEqual("", loaded)
            self.assertEqual("", saved_at)
            self.assertIn("已保存 API Key 超过", error)

    def test_config_store_write_failures_are_server_errors_and_observable(self) -> None:
        provider_key = "ghp_" + ("w" * 24)
        windows_path = "C:" + "\\Users\\config-user\\local-state.json"
        leaked = f"disk full token={provider_key} at '{windows_path}'"
        original_settings = dict(QuietProxyHandler.settings)
        original_save_settings = app.save_local_settings
        original_save_api_key = app.save_api_key_store

        def failing_save(*_args, **_kwargs):
            raise OSError(leaked)

        app.save_local_settings = failing_save
        app.save_api_key_store = failing_save
        try:
            status, raw = self.request(
                "POST",
                "/api/settings",
                {"settings": {"model": "glm-5.2", "include_thinking": True}},
            )
            self.assertEqual(500, status, raw[:500])
            payload = json.loads(raw)
            self.assertEqual("settings_store_write_failed", payload["error"]["code"])
            self.assertEqual(original_settings, QuietProxyHandler.settings)
            self.assertNotIn(provider_key, raw)
            self.assertNotIn("config-user", raw)

            get_status, get_raw = self.request("GET", "/api/settings")
            self.assertEqual(200, get_status, get_raw[:500])
            settings_payload = json.loads(get_raw)
            self.assertFalse(settings_payload["persisted"])
            self.assertIn("redacted", settings_payload["error"])

            status_status, status_raw = self.request("GET", "/api/status")
            self.assertEqual(200, status_status, status_raw[:500])
            status_payload = json.loads(status_raw)
            self.assertFalse(status_payload["settings_store"]["persisted"])
            self.assertIn("redacted", status_payload["settings_store"]["error"])

            status, raw = self.request(
                "POST",
                "/api/settings/api-key",
                {"api_key": "new-local-key"},
            )
            self.assertEqual(500, status, raw[:500])
            payload = json.loads(raw)
            self.assertEqual("api_key_store_write_failed", payload["error"]["code"])
            self.assertEqual("", QuietProxyHandler.api_key)
            self.assertNotIn(provider_key, raw)
            self.assertNotIn("config-user", raw)

            get_status, get_raw = self.request("GET", "/api/settings/api-key")
            self.assertEqual(200, get_status, get_raw[:500])
            api_key_payload = json.loads(get_raw)
            self.assertFalse(api_key_payload["persisted"])
            self.assertIn("redacted", api_key_payload["error"])
        finally:
            app.save_local_settings = original_save_settings
            app.save_api_key_store = original_save_api_key

    def test_concurrent_settings_saves_publish_in_disk_commit_order(self) -> None:
        handler = object.__new__(QuietProxyHandler)
        original_save = app.save_local_settings
        tracking_lock = threading.Lock()
        start = threading.Event()
        writes: list[str] = []
        errors: list[BaseException] = []
        active = 0
        peak = 0

        def fake_save(settings, _path):
            nonlocal active, peak
            model = str(settings["model"])
            with tracking_lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.05)
            with tracking_lock:
                writes.append(model)
                active -= 1
            return f"saved:{model}"

        def worker(model: str) -> None:
            start.wait(timeout=1)
            try:
                handler._save_settings({"model": model})
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        app.save_local_settings = fake_save
        threads = [
            threading.Thread(target=worker, args=("glm-5.2",)),
            threading.Thread(target=worker, args=("glm-5.3",)),
        ]
        try:
            for thread in threads:
                thread.start()
            start.set()
            for thread in threads:
                thread.join(timeout=2)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual([], errors)
            self.assertEqual(1, peak)
            self.assertEqual(2, len(writes))
            self.assertEqual(writes[-1], QuietProxyHandler.settings["model"])
            self.assertEqual(f"saved:{writes[-1]}", QuietProxyHandler.settings_saved_at)
        finally:
            app.save_local_settings = original_save

    def test_concurrent_api_key_initialization_allows_only_one_winner(self) -> None:
        handler = object.__new__(QuietProxyHandler)
        original_save = app.save_api_key_store
        tracking_lock = threading.Lock()
        start = threading.Event()
        writes: list[str] = []
        successes: list[str] = []
        errors: list[BaseException] = []
        active = 0
        peak = 0

        def fake_save(api_key, _path):
            nonlocal active, peak
            with tracking_lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.05)
            with tracking_lock:
                writes.append(api_key)
                active -= 1
            return f"saved:{api_key}"

        def worker(api_key: str) -> None:
            start.wait(timeout=1)
            try:
                handler._save_api_key_from_panel(api_key, "")
                successes.append(api_key)
            except BaseException as exc:
                errors.append(exc)

        app.save_api_key_store = fake_save
        threads = [
            threading.Thread(target=worker, args=("concurrent-key-a",)),
            threading.Thread(target=worker, args=("concurrent-key-b",)),
        ]
        try:
            for thread in threads:
                thread.start()
            start.set()
            for thread in threads:
                thread.join(timeout=2)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(1, peak)
            self.assertEqual(1, len(writes))
            self.assertEqual(1, len(successes))
            self.assertEqual(1, len(errors))
            self.assertIsInstance(errors[0], PermissionError)
            self.assertEqual(writes[0], QuietProxyHandler.api_key)
            self.assertEqual(successes[0], QuietProxyHandler.api_key)
        finally:
            app.save_api_key_store = original_save

    def test_api_key_protection_covers_status_and_api_routes(self) -> None:
        original_api_key = QuietProxyHandler.api_key
        try:
            QuietProxyHandler.api_key = "test-api-key"

            status, raw = self.request("GET", "/api/status")
            self.assertEqual(200, status)
            data = json.loads(raw)
            self.assertTrue(data["api_key_required"])
            self.assertFalse(data["api_key_valid"])

            status, raw = self.request("GET", "/api/auth/profiles")
            self.assertEqual(401, status)
            self.assertIn("invalid or missing API key", raw)

            status, raw = self.request("GET", "/api/metrics?hours=24")
            self.assertEqual(401, status)
            self.assertIn("invalid or missing API key", raw)

            status, raw = self.request("GET", "/api/auth/profiles", headers={"X-API-Key": "test-api-key"})
            self.assertEqual(200, status)
            self.assertTrue(json.loads(raw)["ok"])

            status, raw = self.request("GET", "/api/metrics?hours=24", headers={"X-API-Key": "test-api-key"})
            self.assertEqual(200, status)
            self.assertTrue(json.loads(raw)["ok"])

            status, raw = self.request("GET", "/api/status", headers={"X-API-Key": "test-api-key"})
            self.assertEqual(200, status)
            data = json.loads(raw)
            self.assertTrue(data["api_key_valid"])
            self.assertNotIn("user_name", data)
            self.assertNotIn("profile_label", data)
            self.assertEqual(app.sha16("test-user"), data["user_id_fp"])

            status, raw = self.request("GET", "/api/auth/profiles", headers={"Authorization": "Bearer test-api-key"})
            self.assertEqual(200, status)
            self.assertTrue(json.loads(raw)["ok"])

            status, raw = self.request("GET", "/api/auth/profiles", headers={"X-API-Key": "wrong-key"})
            self.assertEqual(401, status)

            status, raw = self.request(
                "GET",
                "/api/auth/profiles",
                headers={"X-API-Key": "x" * (app.MAX_LOCAL_API_KEY_CHARS + 1)},
            )
            self.assertEqual(401, status)
        finally:
            QuietProxyHandler.api_key = original_api_key

    def test_settings_endpoint_persists_and_reports_defaults(self) -> None:
        status, raw = self.request(
            "POST",
            "/api/settings",
            {
                "settings": {
                    "model": "GLM-5.1",
                    "auto_web_search": True,
                    "enable_thinking": False,
                    "reasoning_effort": "high",
                    "include_thinking": True,
                    "delete_chat_after_completion": False,
                    "upstream_timeout_sec": 600,
                }
            },
        )
        self.assertEqual(200, status)
        self.assertTrue(json.loads(raw)["ok"])

        status, raw = self.request("GET", "/api/settings")
        self.assertEqual(200, status)
        data = json.loads(raw)
        self.assertTrue(data["ok"])
        self.assertTrue(data["persisted"])
        self.assertEqual("glm-5.2", data["settings"]["model"])
        self.assertTrue(data["settings"]["auto_web_search"])
        self.assertFalse(data["settings"]["enable_thinking"])
        self.assertEqual("high", data["settings"]["reasoning_effort"])
        self.assertTrue(data["settings"]["include_thinking"])
        self.assertFalse(data["settings"]["delete_chat_after_completion"])
        self.assertEqual(600, data["settings"]["upstream_timeout_sec"])
        self.assertEqual(app.MAX_SETTINGS_STORE_BYTES, data["max_bytes"])

        status, raw = self.request("GET", "/api/status")
        self.assertEqual(200, status)
        status_data = json.loads(raw)
        defaults = status_data["default_options"]
        self.assertEqual("glm-5.2", defaults["model"])
        self.assertTrue(defaults["include_thinking"])
        self.assertEqual(600, status_data["upstream_timeout_sec"])
        self.assertTrue(status_data["settings_store"]["persisted"])
        self.assertEqual("", status_data["settings_store"]["error"])

    def test_local_settings_roundtrip_and_partial_normalization(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.local.json"
            changed = {
                **app.local_settings_defaults(),
                "model": "GLM-5.1",
                "auto_web_search": True,
                "enable_thinking": False,
                "reasoning_effort": "high",
                "include_thinking": True,
                "delete_chat_after_completion": False,
                "upstream_timeout_sec": 420,
            }
            saved_at = app.save_local_settings(changed, path)
            loaded, loaded_saved_at, error = app.load_local_settings(path)
            self.assertEqual("", error)
            self.assertEqual(saved_at, loaded_saved_at)
            self.assertEqual("glm-5.2", loaded["model"])
            self.assertTrue(loaded["auto_web_search"])
            self.assertFalse(loaded["enable_thinking"])
            self.assertEqual("high", loaded["reasoning_effort"])
            self.assertTrue(loaded["include_thinking"])
            self.assertFalse(loaded["delete_chat_after_completion"])
            self.assertEqual(420, loaded["upstream_timeout_sec"])

        partial = app.normalize_local_settings({"model": "gpt-5", "reasoning_effort": "超高"})
        self.assertEqual("glm-5.3", partial["model"])
        self.assertEqual("max", partial["reasoning_effort"])
        self.assertFalse(partial["include_thinking"])
        self.assertTrue(partial["enable_thinking"])
        self.assertEqual(app.UPSTREAM_STREAM_TIMEOUT_SEC, partial["upstream_timeout_sec"])

        clamped_low = app.normalize_local_settings({"upstream_timeout_sec": 10})
        self.assertEqual(60, clamped_low["upstream_timeout_sec"])
        clamped_high = app.normalize_local_settings({"upstream_timeout_sec": 99999})
        self.assertEqual(3600, clamped_high["upstream_timeout_sec"])
        invalid = app.normalize_local_settings({"upstream_timeout_sec": "abc"})
        self.assertEqual(app.UPSTREAM_STREAM_TIMEOUT_SEC, invalid["upstream_timeout_sec"])

    def test_settings_store_read_budget_falls_back_to_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.local.json"
            path.write_bytes(b"x" * (app.MAX_SETTINGS_STORE_BYTES + 1))
            loaded, saved_at, error = app.load_local_settings(path)
            self.assertEqual(app.local_settings_defaults(), loaded)
            self.assertEqual("", saved_at)
            self.assertIn("settings store exceeds", error)

    def test_profile_store_rejects_oversize_capacity_and_invalid_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "profiles.local.json"
            path.write_bytes(b"x" * (app.MAX_PROFILE_STORE_BYTES + 1))
            with self.assertRaisesRegex(ValueError, "profile store exceeds"):
                app.load_profile_store(path)

            path.write_text(
                json.dumps(
                    {
                        "encryption": "windows-dpapi-current-user",
                        "payload": app.base64.b64encode(b"encrypted").decode("ascii"),
                    }
                ),
                encoding="utf-8",
            )
            original_unprotect = app.dpapi_unprotect
            try:
                app.dpapi_unprotect = lambda _raw: b"x" * (app.MAX_PROFILE_STORE_PAYLOAD_BYTES + 1)
                with self.assertRaisesRegex(ValueError, "profile store payload exceeds"):
                    app.load_profile_store(path)

                too_many = {"profiles": [{} for _ in range(app.MAX_ACCOUNT_PROFILES + 1)]}
                app.dpapi_unprotect = lambda _raw: json.dumps(too_many).encode("utf-8")
                with self.assertRaisesRegex(app.ProfileCapacityError, "64 profiles"):
                    app.load_profile_store(path)

                profile = app.make_profile(fake_state(), "valid", "test")
                invalid_item = app.profile_to_dict(profile)
                invalid_item["id"] = "profile_bad\r\nX-Injected"
                app.dpapi_unprotect = lambda _raw: json.dumps({"profiles": [invalid_item]}).encode("utf-8")
                with self.assertRaisesRegex(ValueError, "profile id is invalid"):
                    app.load_profile_store(path)

                valid_item = app.profile_to_dict(profile)
                app.dpapi_unprotect = lambda _raw: json.dumps(
                    {"profiles": [valid_item], "active_profile_id": profile.id}
                ).encode("utf-8")
                loaded, active, _saved_at = app.load_profile_store(path)
                self.assertEqual([profile.id], list(loaded))
                self.assertEqual(profile.id, active)
            finally:
                app.dpapi_unprotect = original_unprotect

    def test_streaming_responses_and_anthropic_events(self) -> None:
        response_tools = [{"type": "function", "name": "get_weather", "parameters": {"type": "object"}}]
        status, stream = self.request(
            "POST",
            "/v1/responses",
            {"model": "gpt-5", "input": "北京天气", "stream": True, "tools": response_tools},
        )
        self.assertEqual(200, status)
        self.assertIn("event: response.created", stream)
        self.assertIn("event: response.function_call_arguments.done", stream)
        self.assertIn("event: response.completed", stream)
        self.assertIn("data: [DONE]", stream)

        status, stream = self.request(
            "POST",
            "/v1/messages",
            {
                "model": "claude-sonnet-4-6",
                "max_tokens": 128,
                "stream": True,
                "messages": [{"role": "user", "content": "北京天气"}],
                "tools": [{"name": "get_weather", "input_schema": {"type": "object"}}],
            },
        )
        self.assertEqual(200, status)
        self.assertIn("event: message_start", stream)
        self.assertIn("event: content_block_start", stream)
        self.assertIn('"type": "tool_use"', stream)
        self.assertIn("event: message_stop", stream)
        self.assertEqual(2, len(self.deleted_chats))

    def test_all_streaming_surfaces_forward_upstream_idle_heartbeat(self) -> None:
        original_stream = app.stream_zai_completion
        original_interval = app.SSE_KEEPALIVE_INTERVAL_SECONDS

        def heartbeat_stream(_state, _prompt, **kwargs):
            context = kwargs.get("context_out")
            if isinstance(context, dict):
                context["chat_id"] = "00000000-0000-0000-0000-000000000086"
            yield app.UPSTREAM_IDLE_HEARTBEAT_EVENT
            yield 'data: {"data":{"delta_content":"ok","phase":"answer"}}'

        app.stream_zai_completion = heartbeat_stream
        app.SSE_KEEPALIVE_INTERVAL_SECONDS = 0
        try:
            requests = [
                ("/v1/chat/completions", {"model": "glm-5.2", "messages": [{"role": "user", "content": "hi"}], "stream": True}),
                ("/v1/responses", {"model": "glm-5.2", "input": "hi", "stream": True}),
                ("/v1/messages", {"model": "glm-5.2", "max_tokens": 32, "messages": [{"role": "user", "content": "hi"}], "stream": True}),
                ("/api/chat", {"model": "glm-5.2", "message": "hi", "stream": True}),
            ]
            results = [self.request("POST", path, body) for path, body in requests]
        finally:
            app.stream_zai_completion = original_stream
            app.SSE_KEEPALIVE_INTERVAL_SECONDS = original_interval
        for status, raw in results:
            self.assertEqual(200, status, raw[:500])
            self.assertIn(": keep-alive\n\n", raw)

    def test_heartbeat_pump_covers_block_before_first_upstream_event(self) -> None:
        original_stream = app.stream_zai_completion
        original_interval = app.SSE_KEEPALIVE_INTERVAL_SECONDS
        before = app.sse_heartbeat_status()

        def delayed_stream(_state, _prompt, **kwargs):
            context = kwargs.get("context_out")
            if isinstance(context, dict):
                context["chat_id"] = "00000000-0000-0000-0000-000000000085"
            time.sleep(0.07)
            yield 'data: {"data":{"delta_content":"PUMP_RESUMED","phase":"answer"}}'

        app.stream_zai_completion = delayed_stream
        app.SSE_KEEPALIVE_INTERVAL_SECONDS = 0.02
        try:
            requests = [
                (
                    "/v1/chat/completions",
                    {"model": "glm-5.2", "messages": [{"role": "user", "content": "hi"}], "stream": True},
                ),
                ("/v1/responses", {"model": "glm-5.2", "input": "hi", "stream": True}),
                (
                    "/v1/messages",
                    {
                        "model": "glm-5.2",
                        "max_tokens": 32,
                        "messages": [{"role": "user", "content": "hi"}],
                        "stream": True,
                    },
                ),
                ("/api/chat", {"model": "glm-5.2", "message": "hi", "stream": True}),
            ]
            results = [self.request("POST", path, body) for path, body in requests]
        finally:
            app.stream_zai_completion = original_stream
            app.SSE_KEEPALIVE_INTERVAL_SECONDS = original_interval
        for status, raw in results:
            self.assertEqual(200, status, raw[:500])
            self.assertIn(": keep-alive\n\n", raw)
            self.assertLess(raw.index(": keep-alive"), raw.index("PUMP_RESUMED"))
        after = app.sse_heartbeat_status()
        self.assertGreaterEqual(after["started_total"] - before["started_total"], 4)
        self.assertGreaterEqual(after["sent_total"] - before["sent_total"], 4)
        self.assertEqual(0, after["active"])

    def test_heartbeat_pump_starts_before_protocol_attachment_preparation(self) -> None:
        original_prepare = app.prepare_protocol_upstream_request
        original_stream = app.stream_zai_completion
        original_interval = app.SSE_KEEPALIVE_INTERVAL_SECONDS

        def delayed_prepare(state, request, trace_out=None):
            time.sleep(0.07)
            return original_prepare(state, request, trace_out=trace_out)

        def immediate_stream(_state, _prompt, **kwargs):
            context = kwargs.get("context_out")
            if isinstance(context, dict):
                context["chat_id"] = "00000000-0000-0000-0000-000000000084"
            yield 'data: {"data":{"delta_content":"PREPARED","phase":"answer"}}'

        app.prepare_protocol_upstream_request = delayed_prepare
        app.stream_zai_completion = immediate_stream
        app.SSE_KEEPALIVE_INTERVAL_SECONDS = 0.02
        try:
            status, raw = self.request(
                "POST",
                "/v1/chat/completions",
                {"model": "glm-5.2", "messages": [{"role": "user", "content": "hi"}], "stream": True},
            )
        finally:
            app.prepare_protocol_upstream_request = original_prepare
            app.stream_zai_completion = original_stream
            app.SSE_KEEPALIVE_INTERVAL_SECONDS = original_interval
        self.assertEqual(200, status, raw[:500])
        self.assertIn(": keep-alive\n\n", raw)
        self.assertLess(raw.index(": keep-alive"), raw.index("PREPARED"))

    def test_streaming_setup_failure_stays_inside_sse_protocol(self) -> None:
        original_prepare = app.prepare_protocol_upstream_request

        def failing_prepare(*_args, **_kwargs):
            raise RuntimeError("simulated protocol preparation failure")

        app.prepare_protocol_upstream_request = failing_prepare
        try:
            status, raw = self.request(
                "POST",
                "/v1/chat/completions",
                {"model": "glm-5.2", "messages": [{"role": "user", "content": "hi"}], "stream": True},
            )
        finally:
            app.prepare_protocol_upstream_request = original_prepare
        self.assertEqual(200, status, raw[:500])
        self.assertIn("simulated protocol preparation failure", raw)
        self.assertIn('"object": "error"', raw)
        self.assertTrue(raw.endswith("data: [DONE]\n\n"), raw[-300:])

    def test_heartbeat_pump_write_failure_is_surfaced_and_stops(self) -> None:
        original_interval = app.SSE_KEEPALIVE_INTERVAL_SECONDS
        before_errors = app.sse_heartbeat_status()["errors_total"]

        class FailingWriter:
            def write(self, raw):
                if raw.startswith(b": keep-alive"):
                    raise BrokenPipeError("simulated downstream disconnect")
                return len(raw)

            def flush(self):
                return None

        handler = QuietProxyHandler.__new__(QuietProxyHandler)
        handler.wfile = FailingWriter()
        app.SSE_KEEPALIVE_INTERVAL_SECONDS = 0.02
        try:
            handler._ensure_sse_state()
            handler._start_sse_heartbeat_pump()
            deadline = time.time() + 1
            while handler._sse_heartbeat_error is None and time.time() < deadline:
                time.sleep(0.01)
            self.assertIsNotNone(handler._sse_heartbeat_error)
            with self.assertRaisesRegex(ConnectionResetError, "downstream SSE heartbeat failed"):
                handler._sse_write("delta", {"delta": "late"})
            with self.assertRaisesRegex(ConnectionResetError, "downstream SSE heartbeat failed"):
                handler._check_sse_heartbeat()
        finally:
            handler._stop_sse_heartbeat_pump()
            app.SSE_KEEPALIVE_INTERVAL_SECONDS = original_interval
        status = app.sse_heartbeat_status()
        self.assertEqual(before_errors + 1, status["errors_total"])
        self.assertEqual(0, status["active"])

    def test_sse_write_lock_keeps_concurrent_frames_atomic(self) -> None:
        original_interval = app.SSE_KEEPALIVE_INTERVAL_SECONDS

        class SlowWriter:
            def __init__(self):
                self.data = bytearray()

            def write(self, raw):
                for value in raw:
                    self.data.append(value)
                    time.sleep(0)
                return len(raw)

            def flush(self):
                return None

        handler = QuietProxyHandler.__new__(QuietProxyHandler)
        writer = SlowWriter()
        handler.wfile = writer
        handler._ensure_sse_state()
        app.SSE_KEEPALIVE_INTERVAL_SECONDS = 0
        barrier = threading.Barrier(3)

        def write_events():
            barrier.wait()
            for index in range(20):
                handler._sse_write("delta", {"index": index, "text": "x" * 8})

        def write_heartbeats():
            barrier.wait()
            for _ in range(20):
                handler._sse_keepalive()

        workers = [threading.Thread(target=write_events), threading.Thread(target=write_heartbeats)]
        try:
            for worker in workers:
                worker.start()
            barrier.wait()
            for worker in workers:
                worker.join(timeout=3)
            self.assertTrue(all(not worker.is_alive() for worker in workers))
        finally:
            app.SSE_KEEPALIVE_INTERVAL_SECONDS = original_interval
        frames = writer.data.decode("utf-8").split("\n\n")
        frames = [frame for frame in frames if frame]
        self.assertEqual(40, len(frames))
        self.assertEqual(20, sum(frame == ": keep-alive" for frame in frames))
        event_frames = [frame for frame in frames if frame != ": keep-alive"]
        for frame in event_frames:
            event_line, data_line = frame.split("\n", 1)
            self.assertEqual("event: delta", event_line)
            json.loads(data_line.removeprefix("data: "))

    def test_stream_cancel_check_stops_between_upstream_phases(self) -> None:
        state = fake_state()
        original_new_chat = app.new_chat
        original_urlopen = app.urlopen
        urlopen_calls = 0

        def fake_new_chat(*_args, **_kwargs):
            return "00000000-0000-0000-0000-000000000083", "u1"

        def forbidden_urlopen(*_args, **_kwargs):
            nonlocal urlopen_calls
            urlopen_calls += 1
            raise AssertionError("completion request must not start after cancellation")

        checks = 0

        def cancel_after_chat():
            nonlocal checks
            checks += 1
            if checks >= 2:
                raise ConnectionResetError("cancelled after chat creation")

        app.new_chat = fake_new_chat
        app.urlopen = forbidden_urlopen
        context: dict = {}
        try:
            with self.assertRaisesRegex(ConnectionResetError, "cancelled after chat creation"):
                list(
                    self.original_stream(
                        state,
                        "hi",
                        options=app.ChatOptions(model="glm-5.2"),
                        context_out=context,
                        cancel_check=cancel_after_chat,
                    )
                )
        finally:
            app.new_chat = original_new_chat
            app.urlopen = original_urlopen
        self.assertEqual(0, urlopen_calls)
        self.assertEqual("00000000-0000-0000-0000-000000000083", context["chat_id"])

    def test_stream_cancel_after_connect_closes_unconsumed_response(self) -> None:
        state = fake_state()
        original_new_chat = app.new_chat
        original_urlopen = app.urlopen
        checks = 0

        class FakeResp:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

            def __enter__(self):
                raise AssertionError("cancelled response must not be consumed")

            def __exit__(self, *_args):
                return False

        response = FakeResp()

        def cancel_after_connect():
            nonlocal checks
            checks += 1
            if checks >= 4:
                raise ConnectionResetError("cancelled after connect")

        app.new_chat = lambda *_a, **_k: ("00000000-0000-0000-0000-000000000082", "u1")
        app.urlopen = lambda *_a, **_k: response
        try:
            with self.assertRaisesRegex(ConnectionResetError, "cancelled after connect"):
                list(
                    self.original_stream(
                        state,
                        "hi",
                        options=app.ChatOptions(model="glm-5.2"),
                        cancel_check=cancel_after_connect,
                    )
                )
        finally:
            app.new_chat = original_new_chat
            app.urlopen = original_urlopen
        self.assertTrue(response.closed)


    def test_web_history_from_body_normalizes_and_truncates(self) -> None:
        body = {
            "history": [
                {"role": "user", "content": "第一轮问题"},
                {"role": "assistant", "content": "<glm2api_tool_calls>{}</glm2api_tool_calls>\n第一轮回答"},
                {"role": "system", "content": "系统指令应被忽略"},
                {"role": "tool", "content": "工具结果应被忽略"},
                {"role": "user", "content": "x" * (app.WEB_HISTORY_MAX_CHARS + 100)},
                "not-a-dict",
                None,
            ]
        }
        cleaned = app.web_history_from_body(body)
        self.assertEqual(3, len(cleaned))
        self.assertEqual("user", cleaned[0]["role"])
        self.assertEqual("第一轮问题", cleaned[0]["content"])
        self.assertEqual("assistant", cleaned[1]["role"])
        self.assertNotIn("glm2api_tool_calls", cleaned[1]["content"])
        self.assertEqual("第一轮回答", cleaned[1]["content"])
        self.assertEqual("user", cleaned[2]["role"])
        self.assertTrue(cleaned[2]["content"].startswith("x" * app.WEB_HISTORY_MAX_CHARS))
        self.assertIn("已截断", cleaned[2]["content"])

    def test_completion_payload_embeds_history_before_current_prompt(self) -> None:
        payload = app.completion_payload(
            fake_state(),
            "当前问题",
            "chat-1",
            "user-1",
            history=[
                {"role": "user", "content": "历史问题"},
                {"role": "assistant", "content": "历史回答"},
                {"role": "system", "content": "系统指令不该进入上游 messages"},
                {"role": "tool", "content": "工具结果也不该进入"},
                {"role": "user", "content": ""},
                "not-a-dict",
            ],
        )
        self.assertEqual(
            [
                {"role": "user", "content": "历史问题"},
                {"role": "assistant", "content": "历史回答"},
                {"role": "user", "content": "当前问题"},
            ],
            payload["messages"],
        )
        self.assertEqual("当前问题", payload["signature_prompt"])

    def test_web_chat_forwards_multi_turn_history(self) -> None:
        original_stream = app.stream_zai_completion
        captured: dict = {}

        def capture_stream(_state, _prompt, **_kwargs):
            captured.update(_kwargs)
            captured["prompt"] = _prompt
            context = _kwargs.get("context_out")
            if isinstance(context, dict):
                context.update({"chat_id": "00000000-0000-0000-0000-0000000000cc"})
            yield 'data: {"data":{"delta_content":"ok","phase":"answer"}}'

        try:
            app.stream_zai_completion = capture_stream
            status, raw = self.request(
                "POST",
                "/api/chat",
                {
                    "message": "第二轮",
                    "stream": True,
                    "history": [
                        {"role": "user", "content": "第一轮"},
                        {"role": "assistant", "content": "回答"},
                    ],
                },
            )
        finally:
            app.stream_zai_completion = original_stream
        self.assertEqual(200, status)
        self.assertEqual(
            [{"role": "user", "content": "第一轮"}, {"role": "assistant", "content": "回答"}],
            captured.get("history"),
        )
        self.assertEqual("第二轮", captured.get("prompt"))

    def test_web_chat_sse_stream_and_auto_delete(self) -> None:
        original_stream = app.stream_zai_completion

        def plain_stream(_state, _prompt, **_kwargs):
            context = _kwargs.get("context_out")
            if isinstance(context, dict):
                context.update({"chat_id": "00000000-0000-0000-0000-0000000000aa"})
            yield 'data: {"data":{"delta_content":"你好，我是 GLM。","phase":"answer"}}'

        try:
            app.stream_zai_completion = plain_stream
            status, raw = self.request("POST", "/api/chat", {"message": "你好", "stream": True})
        finally:
            app.stream_zai_completion = original_stream
        self.assertEqual(200, status)
        self.assertIn("event: status", raw)
        self.assertIn("event: context", raw)
        self.assertIn('"chat_id": "00000000-0000-0000-0000-0000000000aa"', raw)
        self.assertIn("event: delta", raw)
        self.assertIn("你好，我是 GLM。", raw)
        self.assertIn("event: done", raw)
        self.assertIn('"chat_delete_pending": true', raw)
        self.assertEqual(["00000000-0000-0000-0000-0000000000aa"], self.deleted_chats)

    def test_web_chat_stream_error_cleans_up_created_chat(self) -> None:
        original_stream = app.stream_zai_completion

        def error_stream(_state, _prompt, **_kwargs):
            context = _kwargs.get("context_out")
            if isinstance(context, dict):
                context.update({"chat_id": "00000000-0000-0000-0000-0000000000dd"})
            yield 'data: {"code":"UPSTREAM_ERROR","detail":"上游爆炸了"}'

        try:
            app.stream_zai_completion = error_stream
            status, raw = self.request("POST", "/api/chat", {"message": "你好", "stream": True})
        finally:
            app.stream_zai_completion = original_stream
        self.assertEqual(200, status)
        self.assertIn("event: error", raw)
        self.assertIn("上游爆炸了", raw)
        self.assertEqual(["00000000-0000-0000-0000-0000000000dd"], self.deleted_chats)

    def test_cancel_stream_also_deletes_interrupted_upstream_chat(self) -> None:
        original_stop = app.stop_zai_task
        try:
            app.stop_zai_task = lambda _state, _assistant_id: {"status": True}
            status, raw = self.request(
                "POST",
                "/api/chat/cancel",
                {
                    "assistant_message_id": "00000000-0000-0000-0000-0000000000de",
                    "chat_id": "00000000-0000-0000-0000-0000000000df",
                },
            )
        finally:
            app.stop_zai_task = original_stop
        self.assertEqual(200, status)
        data = json.loads(raw)
        self.assertTrue(data["ok"])
        self.assertTrue(data["upstream_stopped"])
        self.assertEqual("00000000-0000-0000-0000-0000000000df", data["chat_id"])
        self.assertTrue(data["chat_delete_pending"])
        self.assertEqual(["00000000-0000-0000-0000-0000000000df"], self.deleted_chats)

    def test_stop_task_accepts_captured_empty_ack_variants(self) -> None:
        original_http_json = app.http_json
        assistant_id = "00000000-0000-0000-0000-0000000000e1"
        try:
            for upstream in ({}, "", None, True, {"success": True}, {"ok": True}, {"status": True}):
                app.http_json = lambda *_args, _value=upstream, **_kwargs: _value
                result = app.stop_zai_task(fake_state(), assistant_id)
                self.assertTrue(result["status"], upstream)
        finally:
            app.http_json = original_http_json

    def test_stop_task_rejects_explicit_negative_ack(self) -> None:
        original_http_json = app.http_json
        try:
            app.http_json = lambda *_args, **_kwargs: {"status": False}
            with self.assertRaisesRegex(app.UpstreamRequestError, "停止生成失败"):
                app.stop_zai_task(
                    fake_state(),
                    "00000000-0000-0000-0000-0000000000e2",
                )
        finally:
            app.http_json = original_http_json

    def test_stop_task_uses_short_timeout_and_auth_fallback(self) -> None:
        original_http_json = app.http_json
        calls: list[tuple[float, bool]] = []
        errors: list[HTTPError] = []

        def fake_http_json(_method, url, headers, _payload=None, **kwargs):
            calls.append((float(kwargs.get("timeout") or 0), "Authorization" in headers))
            if len(calls) == 1:
                error = HTTPError(url, 403, "Forbidden", None, io.BytesIO(b"auth required"))
                errors.append(error)
                raise error
            return {}

        try:
            app.http_json = fake_http_json
            result = app.stop_zai_task(
                fake_state(),
                "00000000-0000-0000-0000-0000000000e3",
            )
        finally:
            app.http_json = original_http_json
        self.assertTrue(result["status"])
        self.assertEqual(
            [(app.UPSTREAM_STOP_TIMEOUT_SECONDS, False), (app.UPSTREAM_STOP_TIMEOUT_SECONDS, True)],
            calls,
        )
        self.assertTrue(errors[0].closed)

    def test_stop_task_404_is_already_stopped(self) -> None:
        original_http_json = app.http_json
        errors: list[HTTPError] = []

        def fake_http_json(_method, url, _headers, _payload=None, **_kwargs):
            error = HTTPError(url, 404, "Not Found", None, io.BytesIO(b"gone"))
            errors.append(error)
            raise error

        try:
            app.http_json = fake_http_json
            result = app.stop_zai_task(
                fake_state(),
                "00000000-0000-0000-0000-0000000000e4",
            )
        finally:
            app.http_json = original_http_json
        self.assertTrue(result["status"])
        self.assertTrue(result["already_stopped"])
        self.assertTrue(errors[0].closed)

    def test_cancel_stop_failure_still_journals_and_deletes_known_chat(self) -> None:
        original_stop = app.stop_zai_task

        def failing_stop(_state, _assistant_id):
            raise app.UpstreamRequestError("stop endpoint temporarily unavailable")

        try:
            app.stop_zai_task = failing_stop
            status, raw = self.request(
                "POST",
                "/api/chat/cancel",
                {
                    "assistant_message_id": "00000000-0000-0000-0000-0000000000e5",
                    "chat_id": "00000000-0000-0000-0000-0000000000e6",
                },
            )
        finally:
            app.stop_zai_task = original_stop
        self.assertEqual(202, status)
        payload = json.loads(raw)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["upstream_stopped"])
        self.assertTrue(payload["chat_delete_pending"])
        self.assertIn("temporarily unavailable", payload["stop_error"])
        self.assertEqual(["00000000-0000-0000-0000-0000000000e6"], self.deleted_chats)

    def test_cancel_stop_failure_without_chat_preserves_transport_error(self) -> None:
        original_stop = app.stop_zai_task
        try:
            app.stop_zai_task = lambda *_args, **_kwargs: (_ for _ in ()).throw(
                app.UpstreamRequestError("stop failed without chat")
            )
            status, raw = self.request(
                "POST",
                "/api/chat/cancel",
                {"assistant_message_id": "00000000-0000-0000-0000-0000000000e7"},
            )
        finally:
            app.stop_zai_task = original_stop
        self.assertEqual(502, status)
        self.assertFalse(json.loads(raw)["ok"])

    def test_interrupted_chat_reports_pending_when_executor_is_full_but_journaled(self) -> None:
        previous_inline = app._AUTO_DELETE_INLINE
        previous_limit = app.AUTO_DELETE_MAX_PENDING
        blocker_started = threading.Event()
        release = threading.Event()

        def blocker() -> None:
            blocker_started.set()
            release.wait(timeout=5)

        try:
            app._shutdown_auto_delete_executor()
            app._DELETE_EXECUTOR_CLOSED = False
            app._AUTO_DELETE_STOP.clear()
            app._AUTO_DELETE_INLINE = False
            app.AUTO_DELETE_MAX_PENDING = 1
            self.assertTrue(app._submit_auto_delete(blocker))
            self.assertTrue(blocker_started.wait(timeout=2))
            handler = QuietProxyHandler.__new__(QuietProxyHandler)
            accepted = handler._schedule_interrupted_upstream_chat_delete(
                fake_state(),
                "00000000-0000-0000-0000-0000000000e8",
                reason="client_cancel",
            )
            self.assertTrue(accepted)
            status = app.pending_chat_delete_status()
            self.assertEqual(1, status["journal_chat_pending"])
            self.assertNotIn("00000000-0000-0000-0000-0000000000e8", self.deleted_chats)
        finally:
            release.set()
            deadline = time.monotonic() + 5
            while app.auto_delete_executor_status()["pending"] and time.monotonic() < deadline:
                time.sleep(0.01)
            app._shutdown_auto_delete_executor()
            app._DELETE_EXECUTOR_CLOSED = False
            app._AUTO_DELETE_STOP.clear()
            app._AUTO_DELETE_INLINE = previous_inline
            app.AUTO_DELETE_MAX_PENDING = previous_limit

    def test_forced_interrupted_cleanup_ignores_auto_delete_setting(self) -> None:
        handler = QuietProxyHandler.__new__(QuietProxyHandler)
        chat_id = "00000000-0000-0000-0000-0000000000e0"
        scheduled = handler._cleanup_failed_upstream_chat(
            fake_state(),
            {"chat_id": chat_id},
            app.ChatOptions(delete_chat_after_completion=False),
            force=True,
            reason="client_disconnect",
        )
        self.assertTrue(scheduled)
        self.assertEqual([chat_id], self.deleted_chats)

    def test_stream_incomplete_cleanup_ignores_auto_delete_setting(self) -> None:
        handler = QuietProxyHandler.__new__(QuietProxyHandler)
        chat_id = "00000000-0000-0000-0000-0000000000e1"
        scheduled = handler._cleanup_failed_upstream_chat(
            fake_state(),
            {"chat_id": chat_id, "_stream_incomplete": True},
            app.ChatOptions(delete_chat_after_completion=False),
        )
        self.assertTrue(scheduled)
        self.assertEqual([chat_id], self.deleted_chats)

    def test_direct_prompt_cleans_up_created_chat(self) -> None:
        original_stream = app.stream_zai_completion
        original_delete = app.delete_zai_chat
        deleted: list[str] = []

        def fake_stream(_state, _prompt, **_kwargs):
            context = _kwargs.get("context_out")
            if isinstance(context, dict):
                context.update({"chat_id": "00000000-0000-0000-0000-0000000000cd"})
            yield 'data: {"data":{"delta_content":"ok","phase":"answer"}}'

        def fake_delete(_state, chat_id, **_kwargs):
            deleted.append(chat_id)
            return True

        try:
            app.stream_zai_completion = fake_stream
            app.delete_zai_chat = fake_delete
            result = app.direct_prompt(fake_state(), "hello")
        finally:
            app.stream_zai_completion = original_stream
            app.delete_zai_chat = original_delete
        self.assertEqual("ok", result)
        self.assertEqual(["00000000-0000-0000-0000-0000000000cd"], deleted)

    def test_is_chat_missing_error_heuristics(self) -> None:
        self.assertTrue(app.is_chat_missing_error("HTTP 404: chat not found"))
        self.assertTrue(app.is_chat_missing_error("conversation 不存在"))
        self.assertTrue(app.is_chat_missing_error("chat_id not found"))
        self.assertFalse(app.is_chat_missing_error("UPSTREAM_ERROR: rate limit exceeded"))
        self.assertFalse(app.is_chat_missing_error("HTTP 500: internal error"))
        self.assertFalse(app.is_chat_missing_error("rate limit: chat too fast"))

    def test_web_chat_continue_degrades_to_new_when_chat_missing(self) -> None:
        original_stream = app.stream_zai_completion
        calls: list[dict] = []

        def degrade_stream(_state, _prompt, **_kwargs):
            calls.append({k: _kwargs.get(k) for k in ("create_chat", "chat_id", "history", "context_out")})
            context = _kwargs.get("context_out")
            if len(calls) == 1:
                if isinstance(context, dict):
                    context.update({"chat_id": "00000000-0000-0000-0000-0000000000ee"})
                yield 'data: {"code":"CHAT_NOT_FOUND","detail":"chat not found"}'
                return
            if isinstance(context, dict):
                context.update({"chat_id": "00000000-0000-0000-0000-0000000000ff"})
            yield 'data: {"data":{"delta_content":"降级成功","phase":"answer"}}'

        try:
            app.stream_zai_completion = degrade_stream
            status, raw = self.request(
                "POST",
                "/api/chat",
                {
                    "message": "你好",
                    "stream": True,
                    "mode": "continue",
                    "chat_id": "00000000-0000-0000-0000-0000000000ee",
                    "history": [{"role": "user", "content": "旧问题"}],
                },
            )
        finally:
            app.stream_zai_completion = original_stream
        self.assertEqual(200, status)
        self.assertEqual(2, len(calls))
        self.assertFalse(calls[0]["create_chat"])
        self.assertEqual("00000000-0000-0000-0000-0000000000ee", calls[0]["chat_id"])
        self.assertTrue(calls[1]["create_chat"])
        self.assertIsNone(calls[1]["chat_id"])
        self.assertEqual([{"role": "user", "content": "旧问题"}], calls[1]["history"])
        self.assertIn("降级成功", raw)
        self.assertIn("event: done", raw)
        self.assertEqual(["00000000-0000-0000-0000-0000000000ff"], self.deleted_chats)

    def test_concurrent_generations_capped_at_three(self) -> None:
        original_stream = app.stream_zai_completion
        gate = threading.Event()
        release = threading.Event()
        active = 0
        active_lock = threading.Lock()
        results: list[tuple[int, str]] = []
        results_lock = threading.Lock()

        def blocking_stream(_state, _prompt, **_kwargs):
            nonlocal active
            context = _kwargs.get("context_out")
            if isinstance(context, dict):
                with active_lock:
                    active += 1
                    sequence = active
                context.update({"chat_id": f"00000000-0000-0000-0000-{sequence:012d}"})
            gate.set()
            release.wait(timeout=8)
            yield 'data: {"data":{"delta_content":"完成","phase":"answer"}}'

        def worker(message: str):
            result = self.request("POST", "/api/chat", {"message": message, "stream": True})
            with results_lock:
                results.append(result)

        workers: list[threading.Thread] = []
        try:
            app.stream_zai_completion = blocking_stream
            for message in ("第一条", "第二条", "第三条"):
                thread = threading.Thread(target=worker, args=(message,))
                thread.start()
                workers.append(thread)
            for _ in range(200):
                with active_lock:
                    if active >= 3:
                        break
                time.sleep(0.05)
            with active_lock:
                self.assertEqual(3, active)
            status, raw = self.request("POST", "/api/chat", {"message": "第四条", "stream": True})
            self.assertEqual(429, status)
            self.assertIn("chat_slot_busy", raw)
            busy_status, busy_raw = self.request("GET", "/api/status")
            self.assertEqual(200, busy_status)
            self.assertIn('"chat_busy": true', busy_raw)
            self.assertIn('"chat_busy_count": 3', busy_raw)
            release.set()
            for thread in workers:
                thread.join(timeout=8)
            self.assertEqual(3, len(results))
            for status_code, body in results:
                self.assertEqual(200, status_code, body[:300])
                self.assertIn("event: done", body)
        finally:
            release.set()
            app.stream_zai_completion = original_stream

    def test_concurrent_generations_fail_over_to_next_profile(self) -> None:
        original_stream = app.stream_zai_completion
        original_profiles = QuietProxyHandler.profiles
        original_active = QuietProxyHandler.active_profile_id
        original_state = QuietProxyHandler.state
        first_state = replace(fake_state(), user_id="pool-user-a", user_name="pool-a", token="pool-token-a")
        second_state = replace(fake_state(), user_id="pool-user-b", user_name="pool-b", token="pool-token-b")
        first = app.make_profile(first_state, "账号 A", "test")
        second = app.make_profile(second_state, "账号 B", "test")
        QuietProxyHandler.profiles = {first.id: first, second.id: second}
        QuietProxyHandler.active_profile_id = first.id
        QuietProxyHandler.state = first.state
        QuietProxyHandler.chat_inflight = {}
        release = threading.Event()
        calls: list[str] = []
        calls_lock = threading.Lock()
        results: list[tuple[int, str]] = []

        def blocking_stream(stream_state, _prompt, **kwargs):
            context = kwargs.get("context_out")
            with calls_lock:
                calls.append(stream_state.user_id)
                sequence = len(calls)
            if isinstance(context, dict):
                context.update({"chat_id": f"00000000-0000-0000-0000-{sequence:012d}"})
            release.wait(timeout=8)
            yield 'data: {"data":{"delta_content":"完成","phase":"answer"}}'

        def worker(message: str):
            results.append(self.request("POST", "/api/chat", {"message": message, "stream": True}))

        workers: list[threading.Thread] = []
        try:
            app.stream_zai_completion = blocking_stream
            for message in ("A1", "A2", "A3", "B1"):
                thread = threading.Thread(target=worker, args=(message,))
                thread.start()
                workers.append(thread)
                for _ in range(100):
                    with calls_lock:
                        if len(calls) >= len(workers):
                            break
                    time.sleep(0.02)
            with calls_lock:
                self.assertEqual(4, len(calls))
                self.assertEqual(3, calls.count("pool-user-a"))
                self.assertEqual(1, calls.count("pool-user-b"))

            status, raw = self.request("GET", "/api/status")
            self.assertEqual(200, status, raw[:300])
            data = json.loads(raw)
            pool = data["concurrency"]
            self.assertEqual(2, pool["profile_count"])
            self.assertEqual(6, pool["capacity"])
            self.assertEqual(4, pool["inflight"])
            self.assertEqual(3, pool["active_profile_inflight"])
            self.assertEqual({3, 1}, {row["inflight"] for row in pool["profiles"]})

            release.set()
            for thread in workers:
                thread.join(timeout=8)
            self.assertEqual(4, len(results))
            self.assertTrue(all(status_code == 200 for status_code, _body in results))
        finally:
            release.set()
            for thread in workers:
                thread.join(timeout=8)
            app.stream_zai_completion = original_stream
            QuietProxyHandler.profiles = original_profiles
            QuietProxyHandler.active_profile_id = original_active
            QuietProxyHandler.state = original_state

    def test_profile_pool_routes_to_third_profile_when_first_two_are_full(self) -> None:
        handler = QuietProxyHandler.__new__(QuietProxyHandler)
        state_a = replace(fake_state(), user_id="pool-a", token="pool-a-token")
        state_b = replace(fake_state(), user_id="pool-b", token="pool-b-token")
        state_c = replace(fake_state(), user_id="pool-c", token="pool-c-token")
        profile_a = app.make_profile(state_a, "A", "test")
        profile_b = app.make_profile(state_b, "B", "test")
        profile_c = app.make_profile(state_c, "C", "test")
        handler.profiles = {profile_a.id: profile_a, profile_b.id: profile_b, profile_c.id: profile_c}
        handler.active_profile_id = profile_a.id
        handler.state = profile_a.state
        handler.chat_inflight = {profile_a.id: 3, profile_b.id: 3}
        handler.chat_inflight_lock = threading.RLock()
        self.assertEqual(profile_c.id, handler._try_acquire_chat_slot())
        self.assertEqual(profile_c.id, handler._try_acquire_chat_slot())
        self.assertEqual(profile_c.id, handler._try_acquire_chat_slot())
        self.assertIsNone(handler._try_acquire_chat_slot())
        for _ in range(3):
            handler._release_chat_slot(profile_c.id)
        self.assertEqual({profile_a.id: 3, profile_b.id: 3}, handler.chat_inflight)

    def test_profile_pool_payload_order_busy_scope_and_response_affinity(self) -> None:
        original_profiles = QuietProxyHandler.profiles
        original_active = QuietProxyHandler.active_profile_id
        original_state = QuietProxyHandler.state
        original_inflight = QuietProxyHandler.chat_inflight
        original_cors = QuietProxyHandler.cors_origins
        state_a = replace(fake_state(), user_id="route-a", user_name="route-a", token="route-token-a")
        state_b = replace(fake_state(), user_id="route-b", user_name="route-b", token="route-token-b")
        profile_a = app.make_profile(state_a, "A", "test")
        profile_b = app.make_profile(state_b, "B", "test")
        try:
            # Preserve insertion A -> B while making B the default. API/UI order
            # must reflect actual routing B -> A, not dictionary insertion.
            QuietProxyHandler.profiles = {profile_a.id: profile_a, profile_b.id: profile_b}
            QuietProxyHandler.active_profile_id = profile_b.id
            QuietProxyHandler.state = profile_b.state
            QuietProxyHandler.chat_inflight = {profile_b.id: app.MAX_CONCURRENT_GENERATIONS_PER_PROFILE}
            QuietProxyHandler.cors_origins = ("http://client.test",)

            status, raw = self.request("GET", "/api/auth/profiles")
            self.assertEqual(200, status, raw[:300])
            payload = json.loads(raw)
            self.assertEqual([profile_b.id, profile_a.id], [item["id"] for item in payload["profiles"]])
            self.assertEqual([1, 2], [item["routing_order"] for item in payload["profiles"]])
            self.assertEqual([profile_b.id, profile_a.id], [item["id"] for item in payload["concurrency"]["profiles"]])

            # A continued chat is pinned to B. Even though A is free, it must
            # receive a profile-scoped 429 rather than silently cross accounts.
            status, raw = self.request(
                "POST",
                "/api/chat",
                {"message": "pinned", "stream": False},
                {app.PROFILE_ROUTING_HEADER: profile_b.id},
            )
            self.assertEqual(429, status, raw[:300])
            error = json.loads(raw)["error"]
            self.assertEqual("chat_slot_busy", error["type"])
            self.assertEqual("profile", error["scope"])

            # A new unpinned request fails over to A, and the response exposes
            # the selected profile so external clients can pin continuations.
            body = json.dumps(
                {
                    "message": "new request",
                    "stream": False,
                    "delete_chat_after_completion": False,
                },
                ensure_ascii=False,
            ).encode("utf-8")
            request = Request(
                self.base_url + "/api/chat",
                data=body,
                headers={"Content-Type": "application/json", "Origin": "http://client.test"},
                method="POST",
            )
            with urlopen(request, timeout=8) as response:
                response_payload = json.loads(response.read().decode("utf-8"))
                self.assertEqual(profile_a.id, response.headers.get(app.PROFILE_ROUTING_HEADER))
                self.assertIn(app.PROFILE_ROUTING_HEADER, response.headers.get("Access-Control-Expose-Headers", ""))
            self.assertEqual(profile_a.id, response_payload["profile_id"])
            self.assertEqual(
                app.MAX_CONCURRENT_GENERATIONS_PER_PROFILE,
                QuietProxyHandler.chat_inflight[profile_b.id],
            )
        finally:
            QuietProxyHandler.profiles = original_profiles
            QuietProxyHandler.active_profile_id = original_active
            QuietProxyHandler.state = original_state
            QuietProxyHandler.chat_inflight = original_inflight
            QuietProxyHandler.cors_origins = original_cors

    def test_invalid_profile_header_is_not_misreported_as_capacity_busy(self) -> None:
        invalid_headers = {app.PROFILE_ROUTING_HEADER: "profile_missing"}
        cases = [
            ("/api/chat", {"message": "hello", "stream": False}, 404),
            (
                "/v1/chat/completions",
                {"model": "glm-5.3", "messages": [{"role": "user", "content": "hello"}]},
                400,
            ),
            (
                "/v1/messages",
                {"model": "glm-5.3", "max_tokens": 64, "messages": [{"role": "user", "content": "hello"}]},
                400,
            ),
        ]
        for path, body, expected_status in cases:
            status, raw = self.request("POST", path, body, invalid_headers)
            self.assertEqual(expected_status, status, (path, raw[:300]))
            self.assertIn("profile_not_found", raw, path)
            self.assertNotIn("chat_slot_busy", raw, path)

        status, raw = self.request(
            "POST",
            "/api/files/upload?filename=test.txt",
            b"hello",
            {**invalid_headers, "Content-Type": "text/plain"},
        )
        self.assertEqual(404, status, raw[:300])
        self.assertEqual("profile_not_found", json.loads(raw)["error"]["code"])
        self.assertEqual({}, QuietProxyHandler.chat_inflight)

    def test_busy_profile_cannot_be_removed(self) -> None:
        original_profiles = QuietProxyHandler.profiles
        original_active = QuietProxyHandler.active_profile_id
        original_state = QuietProxyHandler.state
        original_inflight = QuietProxyHandler.chat_inflight
        profile = app.make_profile(fake_state(), "busy", "test")
        try:
            QuietProxyHandler.profiles = {profile.id: profile}
            QuietProxyHandler.active_profile_id = profile.id
            QuietProxyHandler.state = profile.state
            QuietProxyHandler.chat_inflight = {profile.id: 1}
            status, raw = self.request("POST", "/api/auth/remove", {"profile_id": profile.id})
            self.assertEqual(409, status, raw[:300])
            error = json.loads(raw)["error"]
            self.assertEqual("profile_busy", error["code"])
            self.assertEqual(1, error["inflight"])
            self.assertIn(profile.id, QuietProxyHandler.profiles)
            self.assertEqual(profile.id, QuietProxyHandler.active_profile_id)
        finally:
            QuietProxyHandler.profiles = original_profiles
            QuietProxyHandler.active_profile_id = original_active
            QuietProxyHandler.state = original_state
            QuietProxyHandler.chat_inflight = original_inflight

    def test_duplicate_compaction_preserves_busy_profiles(self) -> None:
        original_profiles = QuietProxyHandler.profiles
        original_active = QuietProxyHandler.active_profile_id
        original_state = QuietProxyHandler.state
        original_inflight = QuietProxyHandler.chat_inflight
        state_a = replace(fake_state(), user_id="duplicate-user", token="duplicate-a")
        state_b = replace(fake_state(), user_id="duplicate-user", token="duplicate-b")
        state_c = replace(fake_state(), user_id="active-other", token="active-other")
        profile_a = app.make_profile(state_a, "duplicate A", "test")
        profile_b = app.make_profile(state_b, "duplicate B", "test")
        profile_c = app.make_profile(state_c, "active", "test")
        profile_a.loaded_at = "2026-08-30T10:00:00+08:00"
        profile_b.loaded_at = "2026-08-30T11:00:00+08:00"
        try:
            QuietProxyHandler.profiles = {
                profile_a.id: profile_a,
                profile_b.id: profile_b,
                profile_c.id: profile_c,
            }
            QuietProxyHandler.active_profile_id = profile_c.id
            QuietProxyHandler.state = profile_c.state
            QuietProxyHandler.chat_inflight = {profile_a.id: 1, profile_b.id: 1}
            status, raw = self.request("POST", "/api/auth/compact", {})
            self.assertEqual(200, status, raw[:300])
            payload = json.loads(raw)
            self.assertEqual(0, len(payload["removed_profiles"]))
            self.assertEqual(1, payload["skipped_busy_count"])
            self.assertIn("正在生成", payload["message"])
            self.assertIn(profile_a.id, QuietProxyHandler.profiles)
            self.assertIn(profile_b.id, QuietProxyHandler.profiles)
        finally:
            QuietProxyHandler.profiles = original_profiles
            QuietProxyHandler.active_profile_id = original_active
            QuietProxyHandler.state = original_state
            QuietProxyHandler.chat_inflight = original_inflight

    def test_local_proxy_server_rejects_duplicate_live_bind(self) -> None:
        server = app.LocalProxyServer(("127.0.0.1", 0), QuietProxyHandler)
        try:
            with self.assertRaises(OSError):
                app.LocalProxyServer(("127.0.0.1", server.server_port), QuietProxyHandler)
        finally:
            server.server_close()

    def test_local_proxy_server_bounds_handler_threads(self) -> None:
        previous_limit = app.MAX_HTTP_HANDLER_THREADS
        release = threading.Event()
        started = app.queue.Queue()
        client_errors: list[str] = []
        client_statuses: list[int] = []
        overload_responses: list[tuple[str, dict]] = []

        class BlockingHandler(app.BaseHTTPRequestHandler):
            timeout = 3

            def log_message(self, *_args):
                pass

            def do_GET(self) -> None:
                started.put(True)
                release.wait(timeout=3)
                raw = b"ok"
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        server = None
        serve_thread = None
        clients: list[threading.Thread] = []
        try:
            app.MAX_HTTP_HANDLER_THREADS = 2
            server = app.LocalProxyServer(("127.0.0.1", 0), BlockingHandler)
            serve_thread = threading.Thread(target=server.serve_forever, daemon=True)
            serve_thread.start()
            url = f"http://127.0.0.1:{server.server_port}/"

            def call() -> None:
                try:
                    with urlopen(url, timeout=5) as response:
                        response.read()
                        client_statuses.append(response.status)
                except HTTPError as exc:
                    try:
                        client_statuses.append(exc.code)
                        if exc.code == 503:
                            overload_responses.append(
                                (
                                    str(exc.headers.get("Retry-After") or ""),
                                    json.loads(exc.read().decode("utf-8")),
                                )
                            )
                    finally:
                        exc.close()
                except Exception as exc:
                    client_errors.append(str(exc))

            clients = [threading.Thread(target=call, daemon=True) for _ in range(3)]
            for client in clients:
                client.start()
            started.get(timeout=2)
            started.get(timeout=2)
            deadline = time.time() + 2
            while server.handler_status()["rejected_total"] < 1 and time.time() < deadline:
                time.sleep(0.01)

            saturated = server.handler_status()
            self.assertEqual(2, saturated["active"])
            self.assertEqual(2, saturated["max_active"])
            self.assertEqual(2, saturated["peak"])
            self.assertEqual(0, saturated["waiting"])
            self.assertGreaterEqual(saturated["wait_total"], 1)
            self.assertGreaterEqual(saturated["rejected_total"], 1)
            self.assertEqual(app.HTTP_HANDLER_OVERLOAD_RETRY_SECONDS, saturated["overload_retry_seconds"])
            self.assertTrue(saturated["saturated"])

            shutdown_thread = threading.Thread(target=server.shutdown, daemon=True)
            shutdown_thread.start()
            shutdown_thread.join(timeout=2)
            self.assertFalse(shutdown_thread.is_alive(), "accept loop must not wait for a handler slot during shutdown")
            serve_thread.join(timeout=2)
            self.assertFalse(serve_thread.is_alive())

            release.set()
            for client in clients:
                client.join(timeout=5)
            deadline = time.time() + 2
            while server.handler_status()["active"] and time.time() < deadline:
                time.sleep(0.01)
            drained = server.handler_status()
            self.assertEqual(0, drained["active"])
            self.assertEqual(0, drained["waiting"])
            self.assertEqual([], client_errors)
            self.assertCountEqual([200, 200, 503], client_statuses)
            self.assertEqual(1, len(overload_responses))
            retry_after, overload = overload_responses[0]
            self.assertEqual(str(app.HTTP_HANDLER_OVERLOAD_RETRY_SECONDS), retry_after)
            self.assertEqual("server_overloaded", overload["error"]["type"])
            self.assertEqual("handler_capacity_exhausted", overload["error"]["code"])
        finally:
            release.set()
            if server is not None:
                server.shutdown()
                server.server_close()
            if serve_thread is not None:
                serve_thread.join(timeout=5)
            for client in clients:
                client.join(timeout=5)
            app.MAX_HTTP_HANDLER_THREADS = previous_limit

    def test_graceful_shutdown_force_closes_stalled_handler_and_drains(self) -> None:
        entered = threading.Event()
        exited = threading.Event()
        server = None
        serve_thread = None
        client = None

        class StalledBodyHandler(app.BaseHTTPRequestHandler):
            timeout = 0.2

            def log_message(self, *_args):
                pass

            def do_POST(self):
                entered.set()
                try:
                    self.rfile.read(1)
                finally:
                    exited.set()

        try:
            server = app.LocalProxyServer(("127.0.0.1", 0), StalledBodyHandler)
            serve_thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
            serve_thread.start()
            client = app.socket.create_connection(("127.0.0.1", server.server_port), timeout=2)
            client.sendall(
                b"POST /stalled HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"Content-Length: 1\r\n"
                b"Connection: keep-alive\r\n\r\n"
            )
            self.assertTrue(entered.wait(timeout=2))
            self.assertEqual(1, server.handler_status()["active"])
            server.shutdown()
            result = app.graceful_shutdown_server(server, graceful_timeout=0.05, forced_timeout=1.0)
            self.assertTrue(result["drained"])
            self.assertEqual(1, result["forced_sockets"])
            self.assertEqual(0, result["remaining_handlers"])
            self.assertTrue(server.handler_status()["shutting_down"])
            self.assertTrue(exited.wait(timeout=1))
        finally:
            if client is not None:
                client.close()
            if server is not None:
                server.force_close_active_requests()
                server.server_close()
            if serve_thread is not None:
                serve_thread.join(timeout=2)

    def test_request_cancel_check_observes_service_shutdown(self) -> None:
        class FakeServer:
            shutdown_event = threading.Event()

        handler = QuietProxyHandler.__new__(QuietProxyHandler)
        handler.server = FakeServer()
        handler._check_request_cancelled()
        handler.server.shutdown_event.set()
        with self.assertRaises(app.ServiceShuttingDown):
            handler._check_request_cancelled()
        self.assertEqual("service_shutdown", app.interruption_reason(app.ServiceShuttingDown()))

    def test_upstream_chunk_loop_checks_service_shutdown(self) -> None:
        original_urlopen = app.urlopen
        checks = 0

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def __iter__(self):
                return iter([b'data: {"data":{"delta_content":"never emitted","phase":"answer"}}\n\n'])

            def close(self):
                pass

        def cancel_check():
            nonlocal checks
            checks += 1
            if checks >= 5:
                raise app.ServiceShuttingDown("test shutdown")

        try:
            app.urlopen = lambda *_args, **_kwargs: FakeResponse()
            with self.assertRaises(app.ServiceShuttingDown):
                list(
                    self.original_stream(
                        fake_state(),
                        "shutdown test",
                        create_chat=False,
                        chat_id="00000000-0000-0000-0000-000000000001",
                        user_msg_id="00000000-0000-0000-0000-000000000002",
                        options=app.ChatOptions(model="glm-5.2", delete_chat_after_completion=False),
                        cancel_check=cancel_check,
                    )
                )
            self.assertGreaterEqual(checks, 5)
        finally:
            app.urlopen = original_urlopen

    def test_partial_request_body_timeout_returns_408_without_chat_slot(self) -> None:
        previous_timeout = QuietProxyHandler.timeout
        previous_inflight = QuietProxyHandler.chat_inflight
        before_timeouts = app.RUNTIME_METRICS.snapshot()["request_timeouts"]
        sock = None
        try:
            QuietProxyHandler.timeout = 0.2
            QuietProxyHandler.chat_inflight = {}
            sock = app.socket.create_connection(("127.0.0.1", self.server.server_port), timeout=2)
            sock.sendall(
                b"POST /v1/chat/completions HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: 512\r\n"
                b"Connection: close\r\n\r\n"
                b'{"model":"glm-5.3",'
            )
            sock.settimeout(2)
            chunks: list[bytes] = []
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
            raw = b"".join(chunks)
            header, body = raw.split(b"\r\n\r\n", 1)
            self.assertTrue(header.startswith(b"HTTP/1.0 408"), header[:100])
            payload = json.loads(body.decode("utf-8"))
            self.assertEqual("request_timeout", payload["error"]["code"])
            self.assertEqual({}, QuietProxyHandler.chat_inflight)

            deadline = time.time() + 2
            while self.server.handler_status()["active"] and time.time() < deadline:
                time.sleep(0.01)
            self.assertEqual(0, self.server.handler_status()["active"])
            self.assertGreaterEqual(app.RUNTIME_METRICS.snapshot()["request_timeouts"], before_timeouts + 1)
        finally:
            if sock is not None:
                sock.close()
            QuietProxyHandler.timeout = previous_timeout
            QuietProxyHandler.chat_inflight = previous_inflight

    def test_partial_management_body_timeout_uses_generic_408_shape(self) -> None:
        previous_timeout = QuietProxyHandler.timeout
        sock = None
        try:
            QuietProxyHandler.timeout = 0.2
            sock = app.socket.create_connection(("127.0.0.1", self.server.server_port), timeout=2)
            sock.sendall(
                b"POST /api/settings HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: 128\r\n"
                b"Connection: close\r\n\r\n{"
            )
            sock.settimeout(2)
            chunks: list[bytes] = []
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
            raw = b"".join(chunks)
            header, body = raw.split(b"\r\n\r\n", 1)
            self.assertTrue(header.startswith(b"HTTP/1.0 408"), header[:100])
            payload = json.loads(body.decode("utf-8"))
            self.assertFalse(payload["ok"])
            self.assertEqual("request_timeout", payload["error"]["type"])
            self.assertEqual("request_timeout", payload["error"]["code"])
        finally:
            if sock is not None:
                sock.close()
            QuietProxyHandler.timeout = previous_timeout

    def test_chunked_json_body_and_ambiguous_framing_rejection(self) -> None:
        payload = json.dumps(
            {"model": "glm-5.3", "messages": [{"role": "user", "content": "chunked-body-visible"}]},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        parts = [payload[:17], payload[17:43], payload[43:]]
        chunked = b""
        for index, part in enumerate(parts):
            extension = b";part=first" if index == 0 else b""
            chunked += f"{len(part):X}".encode("ascii") + extension + b"\r\n" + part + b"\r\n"
        chunked += b"0\r\nX-Chunk-Test: accepted\r\n\r\n"
        request_head = (
            b"POST /v1/messages/count_tokens HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Content-Type: application/json\r\n"
            b"Transfer-Encoding: chunked\r\n"
            b"Connection: close\r\n\r\n"
        )
        status, body = self.raw_http_request(request_head + chunked)
        self.assertEqual(200, status)
        self.assertGreater(json.loads(body.decode("utf-8"))["input_tokens"], 0)

        ambiguous_head = (
            b"POST /v1/messages/count_tokens HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Content-Type: application/json\r\n"
            + f"Content-Length: {len(chunked)}\r\n".encode("ascii")
            + b"Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
        )
        status, body = self.raw_http_request(ambiguous_head + chunked)
        self.assertEqual(400, status)
        self.assertIn("cannot be combined", json.loads(body.decode("utf-8"))["error"]["message"])

        duplicate_length_head = (
            b"POST /v1/messages/count_tokens HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Content-Type: application/json\r\n"
            + f"Content-Length: {len(payload)}\r\nContent-Length: {len(payload)}\r\n".encode("ascii")
            + b"Connection: close\r\n\r\n"
        )
        status, body = self.raw_http_request(duplicate_length_head + payload)
        self.assertEqual(400, status)
        self.assertIn("multiple Content-Length", json.loads(body.decode("utf-8"))["error"]["message"])

        unsupported_head = (
            b"POST /v1/messages/count_tokens HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Content-Type: application/json\r\n"
            b"Transfer-Encoding: gzip\r\n"
            b"Connection: close\r\n\r\n"
        )
        status, body = self.raw_http_request(unsupported_head + payload)
        self.assertEqual(400, status)
        self.assertIn("only chunked", json.loads(body.decode("utf-8"))["error"]["message"])

    def test_chunked_raw_body_spools_incrementally_and_enforces_limit(self) -> None:
        content = (b"A" * 1500) + (b"B" * 2300)
        wire = b"5DC\r\n" + content[:1500] + b"\r\n8FC\r\n" + content[1500:] + b"\r\n0\r\n\r\n"

        def handler_for(raw: bytes) -> QuietProxyHandler:
            handler = QuietProxyHandler.__new__(QuietProxyHandler)
            handler.rfile = io.BytesIO(raw)
            handler.headers = app.http.client.parse_headers(io.BytesIO(b"Transfer-Encoding: chunked\r\n\r\n"))
            handler.path = "/unit/chunked"
            handler.close_connection = False
            return handler

        path = handler_for(wire)._spool_raw_body(
            max_bytes=len(content),
            prefix="glm2api-test-chunk-",
            suffix=".bin",
        )
        try:
            self.assertEqual(content, path.read_bytes())
        finally:
            path.unlink(missing_ok=True)

        oversized_wire = b"5\r\n12345\r\n0\r\n\r\n"
        with self.assertRaisesRegex(ValueError, "request body too large"):
            handler_for(oversized_wire)._read_raw_body(max_bytes=4)

    def test_request_body_limit_returns_413_for_fixed_and_chunked_framing(self) -> None:
        previous_inflight = QuietProxyHandler.chat_inflight
        before = app.RUNTIME_METRICS.snapshot()["request_too_large"]
        QuietProxyHandler.chat_inflight = {}
        try:
            oversized = app.MAX_JSON_BODY_BYTES + 1
            fixed_request = (
                b"POST /v1/chat/completions HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"Content-Type: application/json\r\n"
                + f"Content-Length: {oversized}\r\n".encode("ascii")
                + b"Connection: keep-alive\r\n\r\n"
            )
            status, body = self.raw_http_request(fixed_request)
            self.assertEqual(413, status)
            openai_error = json.loads(body.decode("utf-8"))["error"]
            self.assertEqual("invalid_request_error", openai_error["type"])
            self.assertEqual("request_too_large", openai_error["code"])

            chunked_request = (
                b"POST /api/settings HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"Content-Type: application/json\r\n"
                b"Transfer-Encoding: chunked\r\n"
                b"Connection: keep-alive\r\n\r\n"
                + f"{oversized:X}\r\n".encode("ascii")
            )
            status, body = self.raw_http_request(chunked_request)
            self.assertEqual(413, status)
            generic_error = json.loads(body.decode("utf-8"))["error"]
            self.assertEqual("request_too_large", generic_error["type"])
            self.assertEqual("request_too_large", generic_error["code"])
            self.assertEqual({}, QuietProxyHandler.chat_inflight)

            deadline = time.time() + 2
            while app.RUNTIME_METRICS.snapshot()["request_too_large"] < before + 2 and time.time() < deadline:
                time.sleep(0.01)
            self.assertGreaterEqual(app.RUNTIME_METRICS.snapshot()["request_too_large"], before + 2)
        finally:
            QuietProxyHandler.chat_inflight = previous_inflight

    def test_upstream_non_stream_reads_and_error_summaries_are_bounded(self) -> None:
        class FakeResponse:
            def __init__(self, data: bytes, declared: int = 0):
                self._stream = io.BytesIO(data)
                self.headers = {"Content-Length": str(declared)} if declared else {}

            def read(self, size: int = -1) -> bytes:
                return self._stream.read(size)

        self.assertEqual(
            b'{"ok":true}',
            app.read_limited_upstream_response(FakeResponse(b'{"ok":true}'), 32, kind="test"),
        )
        with self.assertRaises(app.UpstreamResponseTooLarge):
            app.read_limited_upstream_response(FakeResponse(b"x" * 65), 64, kind="test")
        with self.assertRaises(app.UpstreamResponseTooLarge):
            app.read_limited_upstream_response(FakeResponse(b"", declared=65), 64, kind="test")

        error = HTTPError(
            "https://upstream.invalid/test",
            502,
            "synthetic",
            {},
            io.BytesIO(b"E" * (app.MAX_UPSTREAM_ERROR_RESPONSE_BYTES + 128)),
        )
        summary = app.http_error_summary(error)
        self.assertIn("HTTP Error 502", summary)
        self.assertIn("[error body truncated]", summary)
        self.assertLess(len(summary), 600)

    def test_nonretryable_upstream_transport_failure_maps_to_502_on_all_protocols(self) -> None:
        original_stream = app.stream_zai_completion

        def fail_stream(*_args, **_kwargs):
            raise app.UpstreamRequestError("synthetic bounded upstream response failure")
            yield ""  # pragma: no cover - preserve generator shape

        cases = [
            (
                "/v1/chat/completions",
                {"model": "glm-5.3", "stream": False, "messages": [{"role": "user", "content": "hi"}]},
            ),
            ("/v1/responses", {"model": "glm-5.3", "stream": False, "input": "hi"}),
            (
                "/v1/messages",
                {"model": "glm-5.3", "stream": False, "max_tokens": 32, "messages": [{"role": "user", "content": "hi"}]},
            ),
            ("/api/chat", {"model": "glm-5.3", "stream": False, "message": "hi"}),
        ]
        app.stream_zai_completion = fail_stream
        try:
            for path, body in cases:
                status, raw = self.request("POST", path, body)
                self.assertEqual(502, status, (path, raw[:300]))
            self.assertEqual({}, QuietProxyHandler.chat_inflight)
        finally:
            app.stream_zai_completion = original_stream

    def test_upload_activity_limiter_is_nonblocking_bounded_and_observable(self) -> None:
        limiter = app.ActivityLimiter(2)
        self.assertTrue(limiter.try_acquire())
        self.assertTrue(limiter.try_acquire())
        self.assertFalse(limiter.try_acquire())
        self.assertEqual(
            {
                "active": 2,
                "max_active": 2,
                "peak": 2,
                "started_total": 2,
                "rejected_total": 1,
            },
            limiter.status(),
        )
        limiter.release()
        limiter.release()
        limiter.release()
        self.assertEqual(0, limiter.status()["active"])

    def test_file_and_har_upload_backpressure_rejects_before_body_processing(self) -> None:
        previous_file = app._CHAT_FILE_UPLOAD_LIMITER
        previous_har = app._HAR_UPLOAD_LIMITER
        file_limiter = app.ActivityLimiter(1)
        har_limiter = app.ActivityLimiter(1)
        app._CHAT_FILE_UPLOAD_LIMITER = file_limiter
        app._HAR_UPLOAD_LIMITER = har_limiter
        self.assertTrue(file_limiter.try_acquire())
        self.assertTrue(har_limiter.try_acquire())
        try:
            status, raw = self.request(
                "POST",
                "/api/files/upload?filename=busy.txt",
                b"body-must-not-be-processed",
                {"Content-Type": "text/plain", "Connection": "keep-alive"},
            )
            self.assertEqual(429, status, raw[:300])
            error = json.loads(raw)["error"]
            self.assertEqual("upload_capacity_busy", error["type"])
            self.assertEqual("file", error["scope"])

            status, raw = self.request(
                "POST",
                "/api/auth/har",
                {"har_text": "body-must-not-be-processed"},
                {"Connection": "keep-alive"},
            )
            self.assertEqual(429, status, raw[:300])
            error = json.loads(raw)["error"]
            self.assertEqual("upload_capacity_busy", error["type"])
            self.assertEqual("har", error["scope"])
            self.assertEqual(1, file_limiter.status()["rejected_total"])
            self.assertEqual(1, har_limiter.status()["rejected_total"])
        finally:
            file_limiter.release()
            har_limiter.release()
            app._CHAT_FILE_UPLOAD_LIMITER = previous_file
            app._HAR_UPLOAD_LIMITER = previous_har

    def test_file_upload_releases_capacity_after_upstream_failure(self) -> None:
        previous_limiter = app._CHAT_FILE_UPLOAD_LIMITER
        previous_upload = app.upload_file_path_to_zai
        limiter = app.ActivityLimiter(1)
        app._CHAT_FILE_UPLOAD_LIMITER = limiter

        def fail_upload(*_args, **_kwargs):
            raise app.UpstreamRequestError("synthetic upload failure")

        app.upload_file_path_to_zai = fail_upload
        try:
            status, raw = self.request(
                "POST",
                "/api/files/upload?filename=failure.txt",
                b"hello",
                {"Content-Type": "text/plain"},
            )
            self.assertEqual(502, status, raw[:300])
            self.assertEqual("upstream_upload_error", json.loads(raw)["error"]["code"])
            for _ in range(100):
                if limiter.status()["active"] == 0:
                    break
                time.sleep(0.01)
            self.assertEqual(0, limiter.status()["active"])
            self.assertTrue(limiter.try_acquire(), "失败路径必须归还上传槽位")
            limiter.release()
        finally:
            app.upload_file_path_to_zai = previous_upload
            app._CHAT_FILE_UPLOAD_LIMITER = previous_limiter

    def test_streaming_file_upload_stops_between_chunks_on_service_shutdown(self) -> None:
        original_base_url = app.BASE_URL
        original_connection = app.http.client.HTTPConnection
        instances = []
        checks = 0

        class FakeConnection:
            def __init__(self, *_args, **_kwargs):
                self.sent = []
                self.closed = False
                instances.append(self)

            def putrequest(self, *_args, **_kwargs):
                pass

            def putheader(self, *_args, **_kwargs):
                pass

            def endheaders(self):
                pass

            def send(self, data):
                self.sent.append(data)

            def getresponse(self):
                raise AssertionError("shutdown must stop before waiting for the response")

            def close(self):
                self.closed = True

        def cancel_check():
            nonlocal checks
            checks += 1
            if checks >= 3:
                raise app.ServiceShuttingDown("test shutdown")

        try:
            app.BASE_URL = "http://upstream.test"
            app.http.client.HTTPConnection = FakeConnection
            with self.assertRaises(app.ServiceShuttingDown):
                app._upload_file_stream_to_zai(
                    fake_state(),
                    "test.bin",
                    "application/octet-stream",
                    2,
                    lambda: iter((b"a", b"b")),
                    cancel_check=cancel_check,
                )
            self.assertEqual(1, len(instances))
            self.assertTrue(instances[0].closed)
            self.assertIn(b"a", instances[0].sent)
            self.assertNotIn(b"b", instances[0].sent)
        finally:
            app.BASE_URL = original_base_url
            app.http.client.HTTPConnection = original_connection

    def test_web_file_uploads_use_bounded_workers_and_preserve_partial_cleanup(self) -> None:
        html = (PROJECT_ROOT / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn("const CHAT_FILE_UPLOAD_CONCURRENCY = 3", html)
        self.assertIn("async function mapWithConcurrency", html)
        self.assertIn("firstError.completedResults = results.filter(Boolean)", html)
        self.assertIn("if (Array.isArray(err?.uploadedFiles)) uploadedZaiFiles = err.uploadedFiles", html)
        self.assertNotIn("Promise.all(files.slice(1).map", html)
        self.assertIn('id="info-upstream-responses"', html)
        self.assertIn("runtime.request_too_large", html)

    def test_slot_release_tracks_acquired_profile(self) -> None:
        handler = QuietProxyHandler.__new__(QuietProxyHandler)
        handler.chat_inflight = {}
        handler.chat_inflight_lock = threading.RLock()
        handler.active_profile_id = "profile_a"
        for _ in range(app.MAX_CONCURRENT_GENERATIONS_PER_PROFILE):
            self.assertEqual("profile_a", handler._try_acquire_chat_slot())
        self.assertIsNone(handler._try_acquire_chat_slot())
        # Switch profiles while the generations are still running, then release
        # one: the original owner must be released, never the new active profile.
        handler.active_profile_id = "profile_b"
        handler._release_chat_slot("profile_a")
        self.assertEqual(app.MAX_CONCURRENT_GENERATIONS_PER_PROFILE - 1, handler.chat_inflight["profile_a"])
        handler.active_profile_id = "profile_a"
        self.assertEqual("profile_a", handler._try_acquire_chat_slot())
        self.assertIsNone(handler._try_acquire_chat_slot())
        handler._release_chat_slot("profile_a")
        handler._release_chat_slot("profile_a")
        handler._release_chat_slot("profile_a")
        self.assertEqual({}, handler.chat_inflight)

    def test_slot_freed_while_post_stream_delete_runs(self) -> None:
        original_stream = app.stream_zai_completion
        original_delete = app.delete_zai_chat
        gate = threading.Event()
        release = threading.Event()
        delete_calls: list[str] = []
        results: list[tuple[int, str]] = []

        def instant_stream(_state, _prompt, **_kwargs):
            context = _kwargs.get("context_out")
            if isinstance(context, dict):
                context.update({"chat_id": "00000000-0000-0000-0000-0000000000cc"})
            yield 'data: {"data":{"delta_content":"完成","phase":"answer"}}'

        def slow_delete(_state, chat_id, **_kwargs):
            delete_calls.append(chat_id)
            if len(delete_calls) == 1:
                gate.set()
                release.wait(timeout=8)
            return True

        def worker():
            results.append(self.request("POST", "/api/chat", {"message": "第一条", "stream": True}))

        try:
            app.stream_zai_completion = instant_stream
            app.delete_zai_chat = slow_delete
            t1 = threading.Thread(target=worker)
            t1.start()
            self.assertTrue(gate.wait(timeout=8))
            # The first generation has already ended; it is only doing
            # best-effort chat deletion, which must not hold the slot.
            status, raw = self.request("POST", "/api/chat", {"message": "第二条", "stream": True})
            self.assertEqual(200, status, raw[:300])
            self.assertIn("event: done", raw)
            release.set()
            t1.join(timeout=8)
            self.assertEqual(200, results[0][0])
            self.assertEqual(2, len(delete_calls))
        finally:
            release.set()
            app.stream_zai_completion = original_stream
            app.delete_zai_chat = original_delete

    def test_reference_aligned_prompts(self) -> None:
        tools = [
            {
                "name": "read_file",
                "description": "读取文件",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
            },
            {"name": "get_weather", "description": "天气", "parameters": {"type": "object", "properties": {}}},
        ]
        auto = app.ToolChoice(mode="auto", forced_name="", disable_parallel=False)
        instructions = app.build_tool_instruction(tools, auto)
        self.assertIn("When you choose to invoke a function", instructions)
        self.assertIn("Rules:\n1)", instructions)
        self.assertIn("Incorrect 1 — text trailing the block", instructions)
        self.assertIn("Incorrect 5 — invocation rendered as JSON or Markdown", instructions)
        self.assertIn('Valid example:\n<|DSML|tool_calls>\n  <|DSML|invoke name="read_file"', instructions)
        self.assertIn("Read-style cache guard", instructions)
        self.assertIn("decide for yourself whether a function is necessary", instructions)
        self.assertIn("a progress update or a plan for later work is not a completed answer", instructions)
        self.assertIn("finish normally without calling a function merely to satisfy this guard", instructions)

        required = app.build_tool_instruction(tools, app.ToolChoice(mode="required", forced_name="", disable_parallel=False))
        self.assertIn("MUST issue at least one call", required)
        forced = app.build_tool_instruction(tools, app.ToolChoice(mode="forced", forced_name="get_weather", disable_parallel=False))
        self.assertIn("MUST issue exactly one call to the tool named: get_weather", forced)
        self.assertIn("Do not issue any other tool call", forced)
        self.assertNotIn("read_file", forced)
        self.assertNotIn("Read-style cache guard", forced)

        transcript = app.build_history_transcript(
            [
                {"role": "user", "content": "你好"},
                {
                    "role": "assistant",
                    "content": "你好！",
                    "reasoning_content": "先礼貌回应",
                    "tool_calls": [{"id": "call_1", "name": "get_weather", "arguments": {"city": "北京"}}],
                },
                {"role": "tool", "tool_call_id": "call_1", "name": "get_weather", "content": "晴"},
            ]
        )
        self.assertIn("The dialogue up to this point. Pick up from the most recent user message.", transcript)
        self.assertIn("[user]\n你好", transcript)
        self.assertIn("[assistant]\n[reasoning_content]", transcript)
        self.assertIn("[reasoning_content]\n先礼貌回应\n[/reasoning_content]\n\n你好！", transcript)
        self.assertIn("[tool]\n[function=get_weather invocation_id=call_1]\n晴", transcript)
        self.assertIn('<|DSML|invoke name="get_weather">', transcript)
        self.assertIn('<|DSML|parameter name="city"><![CDATA[北京]]></|DSML|parameter>', transcript)

        tools_text = app.build_tools_transcript(tools)
        self.assertIn("The functions listed below are available for you to invoke during this turn.", tools_text)
        self.assertIn("name: read_file\ndescription: 读取文件\nschema: ", tools_text)
        self.assertEqual("", app.build_tools_transcript(tools, app.ToolChoice(mode="none")))
        forced_tools_text = app.build_tools_transcript(
            tools, app.ToolChoice(mode="forced", forced_name="get_weather", disable_parallel=False)
        )
        self.assertNotIn("name: read_file", forced_tools_text)
        self.assertIn("name: get_weather", forced_tools_text)

        prompt = app.file_mode_execution_prompt(tools, auto)
        self.assertIn("The attached file holds the earlier conversation. Read it and respond to the most recent user request directly.", prompt)
        self.assertIn("The other attached file or files enumerate the available function definitions", prompt)
        self.assertIn("read every segment in numeric header order", prompt)
        self.assertIn(app.MODE_B_TOOL_GUIDANCE, prompt)
        self.assertTrue(prompt.startswith(app.MODE_B_TOOL_GUIDANCE))
        self.assertTrue(prompt.endswith(app.current_input_file_prompt(True)))
        self.assertNotIn("The second attachment", prompt)
        self.assertNotIn("Mandatory call:", prompt)

        no_tools_prompt = app.file_mode_execution_prompt([], auto)
        self.assertEqual(app.current_input_file_prompt(False), no_tools_prompt)
        self.assertNotIn("The other attached file", no_tools_prompt)

        package = app.build_context_package("openai_chat", [{"role": "user", "content": "hi"}], tools, auto)
        self.assertIn("The dialogue up to this point. Pick up from the most recent user message.", package)
        self.assertIn("Valid example:", package)
        self.assertIn("name: get_weather\ndescription: 天气\nschema: ", package)
        self.assertLess(package.index(app.TOOLS_TRANSCRIPT_INTRO), package.index("Valid example:"))
        self.assertLess(package.index("Valid example:"), package.index(app.HISTORY_TRANSCRIPT_INTRO))

    def test_reference_history_normalization_preserves_role_reasoning_and_tool_name(self) -> None:
        messages = app.normalize_openai_messages_for_protocol(
            [
                {"role": "developer", "content": "遵循内部规则"},
                {"role": "assistant", "reasoning_content": "检查参数", "content": "准备调用"},
                {"role": "tool", "tool_call_id": "call_9", "name": "lookup", "content": "结果"},
            ]
        )
        self.assertEqual("system", messages[0]["role"])
        self.assertEqual("检查参数", messages[1]["reasoning_content"])
        self.assertEqual("lookup", messages[2]["name"])
        transcript = app.build_history_transcript(messages)
        self.assertIn("[system]\n遵循内部规则", transcript)
        self.assertIn("[assistant]\n[reasoning_content]\n检查参数\n[/reasoning_content]\n\n准备调用", transcript)
        self.assertIn("[tool]\n[function=lookup invocation_id=call_9]\n结果", transcript)

    def test_context_upload_degrade_falls_back_to_mode_a(self) -> None:
        body = {
            "model": "glm-5.2-forcehistory",
            "messages": [{"role": "user", "content": "历史拆分降级测试"}],
            "tools": [{
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "天气",
                    "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
                },
            }],
        }
        request = app.normalize_openai_chat_request(body, False)
        self.assertTrue(request.context_as_file)

        state = fake_state()
        app._CONTEXT_UPLOAD_FAILURES.pop(state.user_id, None)
        app._CONTEXT_UPLOAD_DEGRADED_UNTIL.pop(state.user_id, None)
        real_upload = app.upload_context_package_to_zai
        try:
            # 正常路径：Mode B 生效（桩上传），提示词为文件模式执行句，附件顺序 [历史, 工具]。
            app.upload_context_package_to_zai = lambda _s, _text, filename=None, label="", **_kwargs: {
                "id": "fake-file-" + str(len(filename or "")),
                "filename": filename or f"{label}.txt",
            }
            trace: dict[str, object] = {}
            prompt, files = app.prepare_protocol_upstream_request(state, request, trace_out=trace)
            self.assertIn("The attached file holds the earlier conversation.", prompt)
            self.assertEqual(2, len(files))
            self.assertEqual("file", trace["delivery_mode"])
            self.assertEqual(["history", "tools"], [item["kind"] for item in trace["context_files"]])

            # 连续失败达到阈值 -> 降级窗口内回落模式 A（整段上下文进 prompt、无附件）。
            for _ in range(app.CONTEXT_UPLOAD_DEGRADE_THRESHOLD):
                app.record_context_upload_failure(state.user_id)
            app.upload_context_package_to_zai = real_upload
            trace = {}
            prompt, files = app.prepare_protocol_upstream_request(state, request, trace_out=trace)
            # 降级回退也是模式 A：整段上下文进 prompt，且按工具请求语义前置守卫。
            self.assertTrue(prompt.startswith("Output integrity guard:"))
            self.assertTrue(prompt.endswith(request.context_text))
            self.assertEqual([], files)
            self.assertEqual("inline", trace["delivery_mode"])
            self.assertEqual("file", trace["requested_mode"])
            self.assertEqual("degraded_window", trace["fallback_reason"])

            # 成功清零失败计数，但不解除已生效的降级窗口（对齐 ds2api 语义，窗口到期自动解除）。
            app.record_context_upload_success(state.user_id)
            self.assertTrue(app.context_upload_degraded(state.user_id))
            self.assertNotIn(state.user_id, app._CONTEXT_UPLOAD_FAILURES)
        finally:
            app.upload_context_package_to_zai = real_upload
            app._CONTEXT_UPLOAD_FAILURES.pop(state.user_id, None)
            app._CONTEXT_UPLOAD_DEGRADED_UNTIL.pop(state.user_id, None)

    def test_context_file_cache_uses_exact_lru_and_global_expiry_sweep(self) -> None:
        state = fake_state()
        previous_cache = dict(app._CONTEXT_FILE_CACHE)
        previous_max = app.CONTEXT_FILE_CACHE_MAX_ITEMS
        try:
            app._CONTEXT_FILE_CACHE.clear()
            app.CONTEXT_FILE_CACHE_MAX_ITEMS = 3
            for index in range(3):
                app._context_file_cache_store(
                    state,
                    f"text-{index}",
                    {"id": f"file-{index}", "meta": {"size": index + 1}},
                )
            self.assertEqual("file-0", app._context_file_cache_lookup(state, "text-0")["id"])
            app._context_file_cache_store(state, "text-3", {"id": "file-3", "meta": {"size": 4}})
            self.assertIsNone(app._context_file_cache_lookup(state, "text-1"), "最久未命中的条目应淘汰")
            self.assertIsNotNone(app._context_file_cache_lookup(state, "text-0"), "刚命中的条目应保留")
            self.assertEqual(3, len(app._CONTEXT_FILE_CACHE))

            expired_key = (state.user_id, "expired-digest")
            app._CONTEXT_FILE_CACHE[expired_key] = (
                {"id": "expired", "meta": {"size": 99}},
                time.monotonic() - app.CONTEXT_FILE_CACHE_TTL_SECONDS - 1,
            )
            cache_status = app.context_cache_status()
            self.assertNotIn(expired_key, app._CONTEXT_FILE_CACHE)
            self.assertEqual(3, cache_status["items"])
            self.assertEqual(3, cache_status["max_items"])
            self.assertLess(cache_status["bytes"], 99)
        finally:
            app.CONTEXT_FILE_CACHE_MAX_ITEMS = previous_max
            app._CONTEXT_FILE_CACHE.clear()
            app._CONTEXT_FILE_CACHE.update(previous_cache)

    def test_context_file_delete_journal_invalidates_cached_file_ref(self) -> None:
        state = fake_state()
        previous_cache = dict(app._CONTEXT_FILE_CACHE)
        file_id = "00000000-0000-0000-0000-000000000521"
        try:
            app._CONTEXT_FILE_CACHE.clear()
            app._context_file_cache_store(state, "cached context", {"id": file_id})
            self.assertIsNotNone(app._context_file_cache_lookup(state, "cached context"))

            removed = app.invalidate_context_file_cache_refs([file_id])

            self.assertEqual(1, removed)
            self.assertIsNone(app._context_file_cache_lookup(state, "cached context"))
        finally:
            app._CONTEXT_FILE_CACHE.clear()
            app._CONTEXT_FILE_CACHE.update(previous_cache)

    def test_history_context_manifest_retains_full_maximum_glm53_wave_set(self) -> None:
        items = [
            {
                "kind": "history",
                "name": f"{index}.txt",
                "content": f"segment-{index}",
                "part": index + 1,
                "parts": 52,
            }
            for index in range(52)
        ]

        snapshot = app.history_context_files_snapshot(items)

        self.assertEqual(52, len(snapshot))
        self.assertGreaterEqual(app.HISTORY_CONTEXT_FILES_MAX, 52)

    def test_context_upload_state_is_bounded_and_expired_globally(self) -> None:
        previous_failures = dict(app._CONTEXT_UPLOAD_FAILURES)
        previous_degraded = dict(app._CONTEXT_UPLOAD_DEGRADED_UNTIL)
        previous_max = app.CONTEXT_UPLOAD_STATE_MAX_ITEMS
        try:
            app._CONTEXT_UPLOAD_FAILURES.clear()
            app._CONTEXT_UPLOAD_DEGRADED_UNTIL.clear()
            app.CONTEXT_UPLOAD_STATE_MAX_ITEMS = 3
            for index in range(10):
                app.record_context_upload_failure(f"user-{index}")
            self.assertEqual(3, len(app._CONTEXT_UPLOAD_FAILURES))
            self.assertEqual({"user-7", "user-8", "user-9"}, set(app._CONTEXT_UPLOAD_FAILURES))

            app._CONTEXT_UPLOAD_DEGRADED_UNTIL["expired-user"] = time.monotonic() - 1
            app._CONTEXT_UPLOAD_FAILURES["expired-user"] = 1
            cache_status = app.context_cache_status()
            self.assertNotIn("expired-user", app._CONTEXT_UPLOAD_DEGRADED_UNTIL)
            self.assertNotIn("expired-user", app._CONTEXT_UPLOAD_FAILURES)
            self.assertLessEqual(
                cache_status["failure_states"] + cache_status["degraded_states"],
                app.CONTEXT_UPLOAD_STATE_MAX_ITEMS,
            )
            self.assertEqual(3, cache_status["max_state_items"])
        finally:
            app.CONTEXT_UPLOAD_STATE_MAX_ITEMS = previous_max
            app._CONTEXT_UPLOAD_FAILURES.clear()
            app._CONTEXT_UPLOAD_FAILURES.update(previous_failures)
            app._CONTEXT_UPLOAD_DEGRADED_UNTIL.clear()
            app._CONTEXT_UPLOAD_DEGRADED_UNTIL.update(previous_degraded)

    def test_context_upload_partial_failure_does_not_send_half_package(self) -> None:
        request = app.normalize_openai_chat_request(
            {
                "model": "glm-5.2-forcehistory",
                "messages": [{"role": "user", "content": "检查原子降级"}],
                "tools": [{"type": "function", "function": {"name": "read_file", "parameters": {}}}],
            },
            False,
        )
        state = fake_state()
        app._CONTEXT_UPLOAD_FAILURES.pop(state.user_id, None)
        app._CONTEXT_UPLOAD_DEGRADED_UNTIL.pop(state.user_id, None)
        real_upload = app.upload_context_package_to_zai
        real_delete = app.delete_zai_file
        deleted: list[str] = []

        def partial_upload(_state, text, filename=None, label="", **_kwargs):
            if label == "history":
                raise RuntimeError("history upload failed")
            return {"id": "tools-partial", "filename": "tools-random.txt", "meta": {"size": len(text)}}

        app.upload_context_package_to_zai = partial_upload
        app.delete_zai_file = lambda _state, file_id, **_kwargs: deleted.append(file_id) or True
        try:
            trace: dict[str, object] = {}
            prompt, files = app.prepare_protocol_upstream_request(state, request, trace_out=trace)
            self.assertTrue(prompt.startswith("Output integrity guard:"))
            self.assertTrue(prompt.endswith(request.context_text))
            self.assertEqual([], files, "工具附件已成功但历史附件失败时，不得发送半个拆分包")
            self.assertEqual(["tools-partial"], deleted)
            self.assertEqual("inline", trace["delivery_mode"])
            self.assertEqual("upload_failed", trace["fallback_reason"])
            self.assertEqual([], trace["context_files"])
        finally:
            app.upload_context_package_to_zai = real_upload
            app.delete_zai_file = real_delete
            app._CONTEXT_UPLOAD_FAILURES.pop(state.user_id, None)
            app._CONTEXT_UPLOAD_DEGRADED_UNTIL.pop(state.user_id, None)

    def test_context_upload_failure_does_not_delete_shared_cached_part(self) -> None:
        request = app.normalize_openai_chat_request(
            {
                "model": "glm-5.2-forcehistory",
                "messages": [{"role": "user", "content": "cached ownership"}],
                "tools": [{"type": "function", "function": {"name": "read_file", "parameters": {}}}],
            },
            False,
        )
        state = fake_state()
        shared_id = "00000000-0000-0000-0000-000000000531"
        original_upload = app.upload_context_package_to_zai
        original_cleanup = app._best_effort_delete_upstream_files
        cleaned: list[list[str]] = []

        def partial_cached_upload(_state, _text, filename=None, label="", *, cache_hit_out=None, **_kwargs):
            if label == "tools":
                if cache_hit_out is not None:
                    cache_hit_out["hit"] = True
                return {"id": shared_id, "filename": "shared.txt"}
            raise app.UpstreamRequestError("history upload failed")

        app.upload_context_package_to_zai = partial_cached_upload
        app._best_effort_delete_upstream_files = (
            lambda _state, file_ids, **_kwargs: cleaned.append(list(file_ids)) or True
        )
        try:
            trace: dict[str, object] = {}
            prompt, files = app.prepare_protocol_upstream_request(state, request, trace_out=trace)
        finally:
            app.upload_context_package_to_zai = original_upload
            app._best_effort_delete_upstream_files = original_cleanup
            app._CONTEXT_UPLOAD_FAILURES.pop(state.user_id, None)
            app._CONTEXT_UPLOAD_DEGRADED_UNTIL.pop(state.user_id, None)

        self.assertEqual([], files)
        self.assertEqual("inline", trace["delivery_mode"])
        self.assertEqual("upload_failed", trace["fallback_reason"])
        self.assertNotIn(shared_id, [file_id for batch in cleaned for file_id in batch])
        self.assertTrue(prompt.endswith(request.context_text))

    def test_per_model_reasoning_effort(self) -> None:
        # 2026-08 官方 UI：glm-5.3 / Flash 三挡（max/high/low），glm-5.2 两挡，
        # GLM-5-Turbo 仅思考开关（completions features 不携带 reasoning_effort）。
        self.assertEqual("low", app.coerce_reasoning_effort_for_model("glm-5.3", "low"))
        self.assertEqual("low", app.coerce_reasoning_effort_for_model("x-preview-l", "low"))
        self.assertEqual("high", app.coerce_reasoning_effort_for_model("glm-5.2", "low"))
        self.assertEqual("max", app.coerce_reasoning_effort_for_model("GLM-5-Turbo", "max"))

        state = fake_state()
        chat_id = "11111111-2222-3333-4444-555555555555"
        user_msg_id = "aaaaaaaa-2222-3333-4444-555555555555"

        options = app.ChatOptions(model="GLM-5-Turbo", enable_thinking=True, reasoning_effort="max")
        payload = app.completion_payload(
            state, "hi", chat_id, user_msg_id, captcha_verify_param="c", options=options
        )
        self.assertTrue(payload["features"]["enable_thinking"])
        self.assertNotIn("reasoning_effort", payload["features"])

        options_low = app.ChatOptions(model="glm-5.3", enable_thinking=True, reasoning_effort="low")
        payload_low = app.completion_payload(
            state, "hi", chat_id, user_msg_id, captcha_verify_param="c", options=options_low
        )
        self.assertEqual("low", payload_low["features"]["reasoning_effort"])

        options_flash = app.ChatOptions(model="x-preview-l", enable_thinking=True, reasoning_effort="low")
        payload_flash = app.completion_payload(
            state, "hi", chat_id, user_msg_id, captcha_verify_param="c", options=options_flash
        )
        self.assertEqual("low", payload_flash["features"]["reasoning_effort"])

    def test_web_status_rebuilds_effort_options_before_restoring_default(self) -> None:
        html = app.WEB_INDEX_PATH.read_text(encoding="utf-8")
        start = html.index("if (!state.statusDefaultsApplied)")
        end = html.index("state.statusDefaultsApplied = true", start)
        block = html[start:end]
        rebuild_at = block.index("syncEffortOptions();")
        restore_at = block.index("reasoningEffortSelect.value = defaultEffort;")
        self.assertLess(rebuild_at, restore_at)
        self.assertIn('const defaultEffort = defaults.reasoning_effort || "max";', block)
        self.assertIn("reasoningEffortSelect.options", block)

    def test_web_stream_requires_terminal_and_preserves_partial_output(self) -> None:
        html = app.WEB_INDEX_PATH.read_text(encoding="utf-8")
        self.assertIn("let terminalEventReceived = false;", html)
        self.assertIn('sseEvent.event === "done"', html)
        self.assertIn("if (!terminalEventReceived)", html)
        self.assertIn('incomplete.code = "stream_incomplete";', html)
        self.assertIn("const hasPartialOutput = Boolean(rawContentText.trim() || rawThinkingText.trim());", html)
        self.assertIn("{ incomplete: true }", html)
        self.assertIn("incomplete: Boolean(metadata.incomplete)", html)
        self.assertIn("上一条助手回复因流中断，仅保留部分内容", html)
        self.assertIn("const STREAM_MARKDOWN_MAX_CHARS = 250000;", html)
        self.assertIn('contentEl.classList.toggle("stream-plain", plain);', html)
        self.assertIn("contentEl.textContent = rawContentText;", html)

    def test_web_local_sessions_use_recent_first_bounded_normalization(self) -> None:
        html = app.WEB_INDEX_PATH.read_text(encoding="utf-8")
        self.assertIn("const LOCAL_SESSION_MAX_TOTAL = 120;", html)
        self.assertIn("const LOCAL_SESSION_MAX_PER_PROFILE = 30;", html)
        self.assertIn("const LOCAL_SESSION_MAX_SCAN = 1000;", html)
        self.assertIn("const LOCAL_SESSION_STORAGE_CHAR_BUDGET = 1500000;", html)
        self.assertIn("function compactLocalSessions(values, options = {})", html)
        self.assertIn(".slice(-maxMessages)", html)
        self.assertIn("state.sessions.splice(existingIndex, 1);", html)
        self.assertIn(".map(normalizeLocalAttachment)", html)
        self.assertIn("state.sessions = compactLocalSessions(parsed);", html)
        self.assertNotIn("arr.slice(-30)", html)

    def test_history_ui_separates_delivery_overview_and_exact_detail(self) -> None:
        html = app.WEB_INDEX_PATH.read_text(encoding="utf-8")
        self.assertIn('id="btn-record-view-overview"', html)
        self.assertIn('id="btn-record-view-detail"', html)
        self.assertIn('id="record-overview-view"', html)
        self.assertIn('id="record-detail-view"', html)
        self.assertIn("function recordDeliveryInfo(r)", html)
        self.assertIn("function renderRecordOverview(r)", html)
        self.assertIn("function renderRecordDeliveryDetail(r)", html)
        self.assertIn("只有输入框承载上下文", html)
        self.assertIn("function contextFileKindLabel(file)", html)
        self.assertIn("function contextFileWaveLabel(info, index)", html)
        self.assertIn("STAGED FILES", html)
        self.assertIn("预载波次", html)
        self.assertIn("const ZAI_MAX_COMPLETION_FILES = 10;", html)
        self.assertIn("function appendSelectedFiles(files)", html)
        self.assertIn("filesToSend.length > completionFileLimit()", html)
        self.assertIn("match(/\\bsegment\\s+(\\d+)\\/(\\d+)\\b/i)", html)
        self.assertIn("${contextFileWaveLabel(info, index)} · ${contextFileKindLabel(file)}", html)
        self.assertNotIn('id="record-history-card"', html)
        self.assertNotIn('id="record-merged-view"', html)
        self.assertIn('label.textContent = String(text ?? "");', html)
        self.assertNotIn('item.innerHTML = `<span>${text}</span>`;', html)
        self.assertEqual(1, html.count("function formatBytes("))
        self.assertIn('currentPage !== "history" || document.hidden', html)
        self.assertIn('if (!document.hidden && currentPage === "dashboard") refreshDashboardLog();', html)
        self.assertIn('if (!document.hidden && currentPage === "logs") fetchLogs({ incremental: true });', html)
        self.assertIn('format: "structured"', html)
        self.assertIn('params.set("after_seq", String(logLastSeq));', html)
        self.assertIn('document.addEventListener("visibilitychange"', html)
        self.assertIn("async function fetchWithTimeout(resource, options = {}, timeoutMs = 10000)", html)
        self.assertIn("if (document.hidden || statusAutoRefreshRunning) return;", html)
        self.assertIn("statusAutoRefreshRunning = false;", html)
        self.assertIn('fetchWithTimeout(`/api/metrics?hours=${metricHours}`', html)
        self.assertIn('fetchWithTimeout("/api/logs?lines=8"', html)
        self.assertEqual(1, html.count('id="reasoning-effort-group"'))
        self.assertIn('id="reasoning-effort-settings-group"', html)
        self.assertEqual(0.75, app.HISTORY_PROGRESS_INTERVAL_SECONDS)
        self.assertIn("这里每 750ms 重取一次", html)

    def test_web_uses_automatic_captcha_solver_without_manual_capture_controls(self) -> None:
        html = app.WEB_INDEX_PATH.read_text(encoding="utf-8")
        self.assertNotIn('id="btn-captcha-refresh"', html)
        self.assertNotIn('data-profile-action="captcha"', html)
        self.assertNotIn("手动采集验证码", html)
        self.assertNotIn("验证码采集窗口", html)
        self.assertIn("function formatCaptchaMode(data)", html)
        self.assertIn("自动 · happy-dom / 浏览器回退", html)
        self.assertIn("验证码按服务端模式自动处理", html)
        self.assertIn("data.playwright_available !== false", html)
        self.assertIn("浏览器登录组件未安装", html)
        self.assertNotIn("data.user_id ||", html)
        self.assertIn("data.user_id_fp ||", html)
        self.assertNotIn("p.source ||", html)
        self.assertIn("maxProfiles: 0", html)
        self.assertIn("payload.max_profiles ?? state.maxProfiles", html)

    def test_fresh_captcha_cli_is_solver_neutral_and_keeps_legacy_alias(self) -> None:
        current = app.parse_args(["--fresh-captcha", "--captcha-mode", "happydom"])
        legacy = app.parse_args(["--fresh-captcha-browser", "--captcha-mode", "browser"])
        disabled = app.parse_args([])
        self.assertTrue(current.fresh_captcha)
        self.assertEqual("happydom", current.captcha_mode)
        self.assertTrue(legacy.fresh_captcha)
        self.assertEqual("browser", legacy.captcha_mode)
        self.assertFalse(disabled.fresh_captcha)
        self.assertFalse(hasattr(current, "fresh_captcha_browser"))

    def test_start_script_prefers_happydom_without_playwright_dependency(self) -> None:
        script = (PROJECT_ROOT / "start_glm2api.ps1").read_text(encoding="utf-8")
        self.assertIn("if (Ensure-HappyDom)", script)
        self.assertIn("captcha solver: happy-dom (no browser worker required)", script)
        self.assertIn("'--fresh-captcha', '--captcha-mode', $CaptchaMode", script)
        self.assertNotIn("'--fresh-captcha-browser', '--open-web'", script)

    def test_public_release_check_passes_project_tree(self) -> None:
        if not (PROJECT_ROOT / ".git").exists():
            self.skipTest("release check requires a Git worktree")
        completed = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "scripts" / "public_release_check.py")],
            cwd=PROJECT_ROOT,
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual("PASS", payload["overall"])
        self.assertGreater(payload["files_scanned"], 0)

    def test_release_check_rejects_secret_shape_and_capture_name(self) -> None:
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT, prefix=".release-check-test-") as tmp:
            tmp_path = Path(tmp)
            secret_path = tmp_path / "fixture.txt"
            capture_path = tmp_path / "capture.har"
            secret_path.write_text('token = "' + "ghp_" + ("a" * 24) + '"\n', encoding="utf-8")
            capture_path.write_text("{}", encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable,
                    str(PROJECT_ROOT / "scripts" / "public_release_check.py"),
                    str(secret_path.relative_to(PROJECT_ROOT)),
                    str(capture_path.relative_to(PROJECT_ROOT)),
                ],
                cwd=PROJECT_ROOT,
                text=True,
                encoding="utf-8",
                capture_output=True,
                timeout=30,
                check=False,
            )
        self.assertEqual(1, completed.returncode)
        payload = json.loads(completed.stdout)
        self.assertEqual("FAIL", payload["overall"])
        self.assertTrue(any("GitHub token" in failure for failure in payload["failures"]))
        self.assertTrue(any("capture/log artifact" in failure for failure in payload["failures"]))

    def test_release_check_handles_unusual_git_candidate_names(self) -> None:
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT, prefix=".release-check-test-") as tmp:
            tmp_path = Path(tmp)
            secret_path = tmp_path / "中文 空格片段.txt"
            secret_path.write_text('token = "' + "ghp_" + ("b" * 24) + '"\n', encoding="utf-8")
            completed = subprocess.run(
                [sys.executable, str(PROJECT_ROOT / "scripts" / "public_release_check.py")],
                cwd=PROJECT_ROOT,
                text=True,
                encoding="utf-8",
                capture_output=True,
                timeout=30,
                check=False,
            )
        self.assertEqual(1, completed.returncode)
        payload = json.loads(completed.stdout)
        self.assertEqual("FAIL", payload["overall"])
        self.assertTrue(any("GitHub token" in failure for failure in payload["failures"]))

    def test_status_reports_effective_captcha_solver_capabilities(self) -> None:
        previous_enabled = QuietProxyHandler.fresh_captcha_browser
        previous_mode = app._CAPTCHA_MODE
        try:
            QuietProxyHandler.fresh_captcha_browser = True
            app._CAPTCHA_MODE = "happydom"
            status, raw = self.request("GET", "/api/status")
            self.assertEqual(200, status)
            payload = json.loads(raw)
            self.assertEqual("browser_fresh", payload["captcha_mode"])
            self.assertEqual("fresh", payload["captcha_strategy"])
            self.assertTrue(payload["captcha_fresh_enabled"])
            self.assertEqual("happydom", payload["captcha_solver"])
            self.assertIsInstance(payload["captcha_happydom_available"], bool)
            self.assertFalse(payload["captcha_browser_fallback_enabled"])
            self.assertFalse(payload["legacy_browser_captcha_refresh_enabled"])
            self.assertIsInstance(payload["playwright_available"], bool)
            self.assertEqual(app.sha16("test-user"), payload["user_id_fp"])
            self.assertNotIn("user_id", payload)
            self.assertNotIn("user_name", payload)
            self.assertNotIn("profile_label", payload)
            self.assertNotIn("chrome_path", payload)
            self.assertIsInstance(payload["browser_executable_available"], bool)
            self.assertEqual(app.MAX_RESPONSE_STORE_ITEMS, payload["response_store"]["max_items"])
            self.assertEqual(app.MAX_RESPONSE_STORE_BYTES, payload["response_store"]["max_bytes"])
            self.assertEqual(app.MAX_STORED_RESPONSE_BYTES, payload["response_store"]["max_item_bytes"])
            self.assertEqual(app.HISTORY_MAX_DETAIL_BYTES, payload["history_store"]["max_detail_bytes"])
            self.assertEqual(app.MAX_HISTORY_INDEX_BYTES, payload["history_store"]["max_index_bytes"])
            self.assertEqual(
                app.MAX_HISTORY_DETAIL_FILE_BYTES,
                payload["history_store"]["max_detail_file_bytes"],
            )
            self.assertEqual(app._HISTORY_CONF["max_records"], payload["history_store"]["max_records"])
            self.assertEqual(app.HISTORY_MAX_DETAIL_BYTES, payload["limits"]["history_detail_bytes"])
            self.assertEqual(app.MAX_HISTORY_INDEX_BYTES, payload["limits"]["history_index_bytes"])
            self.assertEqual(
                app.MAX_HISTORY_DETAIL_FILE_BYTES,
                payload["limits"]["history_detail_file_bytes"],
            )
            self.assertEqual(app.LOG_MAX_BYTES * (app.LOG_BACKUP_COUNT + 1), payload["log_store"]["max_total_bytes"])
            self.assertEqual(app.AUTO_DELETE_MAX_PENDING, payload["auto_delete"]["max_pending"])
            self.assertEqual(0, payload["auto_delete"]["journal_pending"])
            self.assertEqual(0, payload["auto_delete"]["journal_chat_pending"])
            self.assertEqual(0, payload["auto_delete"]["journal_file_pending"])
            self.assertFalse(payload["auto_delete"]["replay_active"])
            self.assertEqual(0, payload["auto_delete"]["replay_deferred"])
            self.assertEqual(
                app.PENDING_DELETE_MAX_RECORDS,
                payload["auto_delete"]["journal_max_records"],
            )
            self.assertFalse(payload["captcha_worker"]["enabled"])
            self.assertEqual(app.CAPTCHA_WORKER_MAX_PENDING, payload["captcha_worker"]["max_pending"])
            self.assertEqual(
                app.CAPTCHA_WORKER_MAX_PENDING,
                payload["limits"]["captcha_worker_pending"],
            )
            self.assertEqual(app.MAX_HTTP_HANDLER_THREADS, payload["http_handlers"]["max_active"])
            self.assertEqual(app.MAX_HTTP_HANDLER_THREADS, payload["limits"]["http_handler_threads"])
            self.assertEqual(app.MAX_QUERY_FIELDS, payload["limits"]["query_fields"])
            self.assertEqual(app.MAX_QUERY_KEY_CHARS, payload["limits"]["query_key_chars"])
            self.assertEqual(app.MAX_QUERY_VALUE_CHARS, payload["limits"]["query_value_chars"])
            self.assertEqual(app.MAX_HISTORY_SEARCH_CHARS, payload["limits"]["history_search_chars"])
            self.assertEqual(app.MAX_HISTORY_QUERY_PAGE, payload["limits"]["history_query_page"])
            self.assertEqual(app.MAX_ACCOUNT_PROFILES, payload["limits"]["account_profiles"])
            self.assertEqual(app.MAX_PROFILE_STORE_BYTES, payload["limits"]["profile_store_bytes"])
            self.assertEqual(
                app.MAX_PROFILE_STORE_PAYLOAD_BYTES,
                payload["limits"]["profile_store_payload_bytes"],
            )
            self.assertEqual(app.MAX_SETTINGS_STORE_BYTES, payload["limits"]["settings_store_bytes"])
            self.assertEqual(
                app.MAX_PENDING_DELETE_STORE_BYTES,
                payload["limits"]["pending_delete_store_bytes"],
            )
            self.assertEqual(app.MAX_LOCAL_API_KEY_CHARS, payload["limits"]["local_api_key_chars"])
            self.assertEqual(app.MAX_API_KEY_STORE_BYTES, payload["limits"]["api_key_store_bytes"])
            self.assertEqual(app.MAX_SESSION_TOKEN_CHARS, payload["limits"]["session_token_chars"])
            self.assertEqual(
                app.MAX_PROFILE_STATE_FIELD_CHARS,
                payload["limits"]["profile_state_field_chars"],
            )
            self.assertEqual(app.MAX_TOOL_DEFINITIONS, payload["limits"]["tool_definitions"])
            self.assertEqual(
                app.MAX_TOOL_DEFINITIONS_BYTES,
                payload["limits"]["tool_definitions_bytes"],
            )
            self.assertEqual(app.MAX_TOOL_CALLS_PER_TURN, payload["limits"]["tool_calls_per_turn"])
            self.assertEqual(app.MAX_TOOL_ARGUMENTS_BYTES, payload["limits"]["tool_arguments_bytes"])
            self.assertIsInstance(payload["http_handlers"]["rejected_total"], int)
            self.assertEqual(
                app.HTTP_HANDLER_OVERLOAD_RETRY_SECONDS,
                payload["http_handlers"]["overload_retry_seconds"],
            )
            self.assertEqual(
                app.GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS,
                payload["limits"]["graceful_shutdown_seconds"],
            )
            self.assertEqual(
                app.FORCED_SHUTDOWN_TIMEOUT_SECONDS,
                payload["limits"]["forced_shutdown_seconds"],
            )
            self.assertEqual(
                app.REQUEST_SOCKET_IDLE_TIMEOUT_SECONDS,
                payload["limits"]["request_socket_idle_seconds"],
            )
            self.assertEqual(
                app.UPSTREAM_FILE_IDLE_TIMEOUT_SECONDS,
                payload["limits"]["upstream_file_idle_seconds"],
            )
            self.assertEqual(
                app.AUTO_DELETE_REQUEST_TIMEOUT_SECONDS,
                payload["limits"]["auto_delete_request_seconds"],
            )
            self.assertEqual(
                app.AUTO_DELETE_SHUTDOWN_TIMEOUT_SECONDS,
                payload["limits"]["auto_delete_shutdown_seconds"],
            )
            self.assertEqual(
                app.UPSTREAM_STOP_TIMEOUT_SECONDS,
                payload["limits"]["upstream_stop_seconds"],
            )
            self.assertEqual(
                app.PENDING_DELETE_MAX_RECORDS,
                payload["limits"]["pending_delete_records"],
            )
            self.assertEqual(app.MAX_ACTIVE_CHAT_FILE_UPLOADS, payload["upload_slots"]["file"]["max_active"])
            self.assertEqual(app.MAX_ACTIVE_HAR_UPLOADS, payload["upload_slots"]["har"]["max_active"])
            self.assertEqual(
                app.MAX_UPSTREAM_JSON_RESPONSE_BYTES,
                payload["upstream_responses"]["json_max_bytes"],
            )
            self.assertEqual(
                app.MAX_UPSTREAM_STREAM_WIRE_BYTES,
                payload["upstream_responses"]["stream_wire_max_bytes"],
            )
            self.assertEqual(
                app.MAX_UPSTREAM_STREAM_OUTPUT_BYTES,
                payload["upstream_responses"]["stream_output_max_bytes"],
            )
            self.assertEqual(
                app.MAX_UPSTREAM_STREAM_EVENTS,
                payload["upstream_responses"]["stream_max_events"],
            )
            self.assertEqual(
                app.MAX_UPSTREAM_ERROR_RESPONSE_BYTES,
                payload["upstream_responses"]["error_max_bytes"],
            )
            self.assertEqual(
                app.MAX_LEGACY_JSON_HAR_BYTES,
                payload["limits"]["legacy_json_har_bytes"],
            )
            self.assertEqual(app.ZAI_MAX_COMPLETION_FILES, payload["limits"]["zai_completion_files"])
            self.assertEqual(
                app.MAX_ACTIVE_CHAT_FILE_UPLOADS,
                payload["limits"]["active_chat_file_uploads"],
            )
            self.assertEqual(app.MAX_ACTIVE_HAR_UPLOADS, payload["limits"]["active_har_uploads"])
            self.assertEqual(
                app.MAX_UPSTREAM_JSON_RESPONSE_BYTES,
                payload["limits"]["upstream_json_response_bytes"],
            )
            self.assertEqual(
                app.MAX_UPSTREAM_STREAM_WIRE_BYTES,
                payload["limits"]["upstream_stream_wire_bytes"],
            )
            self.assertEqual(
                app.MAX_UPSTREAM_STREAM_OUTPUT_BYTES,
                payload["limits"]["upstream_stream_output_bytes"],
            )
            self.assertEqual(app.MAX_UPSTREAM_STREAM_EVENTS, payload["limits"]["upstream_stream_events"])
            self.assertEqual(
                app.MAX_UPSTREAM_ERROR_RESPONSE_BYTES,
                payload["limits"]["upstream_error_response_bytes"],
            )
            self.assertEqual(
                app.MAX_UPSTREAM_UPLOAD_RESPONSE_BYTES,
                payload["limits"]["upstream_upload_response_bytes"],
            )
            self.assertEqual(app.MAX_RUNTIME_METRIC_PATHS, payload["limits"]["runtime_metric_paths"])
            self.assertEqual(
                app.MAX_RUNTIME_METRIC_PATH_CHARS,
                payload["limits"]["runtime_metric_path_chars"],
            )
            self.assertEqual(app.LOG_RECORD_MAX_CHARS, payload["limits"]["log_record_chars"])
            self.assertEqual(app.UPSTREAM_READER_QUEUE_SIZE, payload["upstream_readers"]["queue_size"])
            self.assertGreaterEqual(payload["upstream_readers"]["peak"], 0)
            self.assertEqual(
                app.SSE_KEEPALIVE_INTERVAL_SECONDS,
                payload["sse_heartbeat"]["interval_seconds"],
            )
            self.assertGreaterEqual(payload["sse_heartbeat"]["peak"], 0)
            self.assertEqual(app.CONTEXT_FILE_CACHE_MAX_ITEMS, payload["context_cache"]["max_items"])
            self.assertEqual(
                app.CONTEXT_UPLOAD_STATE_MAX_ITEMS,
                payload["context_cache"]["max_state_items"],
            )
            self.assertTrue(payload["protocol_compatibility"]["chunked_request_body"])
            self.assertTrue(payload["protocol_compatibility"]["upstream_idle_heartbeat"])
            self.assertEqual(app.MAX_CHUNK_SIZE_LINE_BYTES, payload["limits"]["chunk_size_line_bytes"])
            self.assertEqual(app.MAX_CHUNK_TRAILER_BYTES, payload["limits"]["chunk_trailer_bytes"])
            self.assertEqual(app.HAR_EXTRACT_TIMEOUT_SECONDS, payload["limits"]["har_extract_seconds"])
            self.assertEqual(app.HELPER_PROCESS_POLL_SECONDS, payload["limits"]["helper_process_poll_seconds"])
            self.assertEqual(
                app.BROWSER_LOGIN_LAUNCH_TIMEOUT_MS / 1000,
                payload["limits"]["browser_login_launch_seconds"],
            )
            self.assertEqual(
                app.BROWSER_LOGIN_NAVIGATION_SLICE_MS / 1000,
                payload["limits"]["browser_login_navigation_slice_seconds"],
            )
            self.assertEqual(
                app.BROWSER_LOGIN_AUTH_FETCH_TIMEOUT_MS / 1000,
                payload["limits"]["browser_login_auth_fetch_seconds"],
            )
            app._CAPTCHA_MODE = "auto"
            status, raw = self.request("GET", "/api/status")
            self.assertEqual(200, status)
            browser_capable = json.loads(raw)
            self.assertTrue(browser_capable["captcha_browser_fallback_enabled"])
            self.assertTrue(browser_capable["legacy_browser_captcha_refresh_enabled"])
        finally:
            QuietProxyHandler.fresh_captcha_browser = previous_enabled
            app._CAPTCHA_MODE = previous_mode

    def test_browser_login_navigation_retries_in_cancellable_slices(self) -> None:
        class FakePage:
            def __init__(self):
                self.goto_calls: list[dict] = []
                self.load_state_calls: list[tuple[str, int]] = []

            def goto(self, _url, **kwargs):
                self.goto_calls.append(kwargs)
                if len(self.goto_calls) == 1:
                    raise TimeoutError("first navigation slice timed out")

            def is_closed(self):
                return False

            def wait_for_load_state(self, state, timeout):
                self.load_state_calls.append((state, timeout))

        page = FakePage()
        cancellation_checks = 0

        def cancel_check():
            nonlocal cancellation_checks
            cancellation_checks += 1

        app._navigate_browser_login_page(
            page,
            deadline=time.monotonic() + 2,
            cancel_check=cancel_check,
        )
        self.assertEqual(2, len(page.goto_calls))
        self.assertTrue(all(call["wait_until"] == "commit" for call in page.goto_calls))
        self.assertTrue(
            all(0 < call["timeout"] <= app.BROWSER_LOGIN_NAVIGATION_SLICE_MS for call in page.goto_calls)
        )
        self.assertEqual("domcontentloaded", page.load_state_calls[0][0])
        self.assertLessEqual(page.load_state_calls[0][1], app.BROWSER_LOGIN_DOM_READY_TIMEOUT_MS)
        self.assertGreaterEqual(cancellation_checks, 4)

    def test_browser_login_navigation_cancels_between_slices(self) -> None:
        class FakePage:
            goto_calls = 0

            def goto(self, _url, **_kwargs):
                self.goto_calls += 1
                raise TimeoutError("navigation slice timed out")

            def is_closed(self):
                return False

        page = FakePage()
        checks = 0

        def cancel_check():
            nonlocal checks
            checks += 1
            if checks >= 2:
                raise app.ServiceShuttingDown("test shutdown")

        with self.assertRaises(app.ServiceShuttingDown):
            app._navigate_browser_login_page(
                page,
                deadline=time.monotonic() + 30,
                cancel_check=cancel_check,
            )
        self.assertEqual(1, page.goto_calls)

    def test_browser_login_auth_probe_has_abort_timeout(self) -> None:
        source = Path(app.__file__).read_text(encoding="utf-8")
        self.assertIn('"timeout": BROWSER_LOGIN_LAUNCH_TIMEOUT_MS', source)
        self.assertIn("const authController = new AbortController();", source)
        self.assertIn("signal: authController.signal", source)
        self.assertIn("clearTimeout(authTimer);", source)
        self.assertIn("BROWSER_LOGIN_AUTH_FETCH_TIMEOUT_MS,", source)

    def test_browser_login_fails_fast_when_optional_playwright_is_missing(self) -> None:
        previous = app._PLAYWRIGHT_PACKAGE_AVAILABLE
        try:
            app._PLAYWRIGHT_PACKAGE_AVAILABLE = False
            status, raw = self.request("POST", "/api/auth/browser-login", {"label": "unused"})
            self.assertEqual(503, status)
            payload = json.loads(raw)
            self.assertEqual("browser_automation_unavailable", payload["error"]["code"])
            self.assertIn("Token/HAR", payload["error"]["message"])
        finally:
            app._PLAYWRIGHT_PACKAGE_AVAILABLE = previous

    def test_legacy_browser_captcha_route_is_disabled_in_happydom_mode(self) -> None:
        previous = (
            app._CAPTCHA_MODE,
            app._PLAYWRIGHT_PACKAGE_AVAILABLE,
            app.get_browser_captcha,
            QuietProxyHandler.fresh_captcha_browser,
            QuietProxyHandler.browser_flow_lock,
        )

        def unexpected_browser(*_args, **_kwargs):
            raise AssertionError("happydom mode must not launch a browser captcha flow")

        try:
            app._CAPTCHA_MODE = "happydom"
            app._PLAYWRIGHT_PACKAGE_AVAILABLE = True
            app.get_browser_captcha = unexpected_browser
            QuietProxyHandler.fresh_captcha_browser = True
            QuietProxyHandler.browser_flow_lock = threading.Lock()
            status, raw = self.request("POST", "/api/auth/captcha-refresh", {})
            self.assertEqual(409, status)
            payload = json.loads(raw)
            self.assertEqual("legacy_browser_captcha_disabled", payload["error"]["code"])
            self.assertEqual("feature_disabled", payload["error"]["type"])
            self.assertFalse(QuietProxyHandler.browser_flow_lock.locked())
        finally:
            (
                app._CAPTCHA_MODE,
                app._PLAYWRIGHT_PACKAGE_AVAILABLE,
                app.get_browser_captcha,
                QuietProxyHandler.fresh_captcha_browser,
                QuietProxyHandler.browser_flow_lock,
            ) = previous

    def test_browser_auth_flow_lock_prevents_duplicate_workers_and_progress_clobber(self) -> None:
        previous_available = app._PLAYWRIGHT_PACKAGE_AVAILABLE
        previous_login = app.get_browser_login_state
        previous_mode = app._CAPTCHA_MODE
        previous_fresh = QuietProxyHandler.fresh_captcha_browser
        previous_flow_lock = QuietProxyHandler.browser_flow_lock
        previous_progress_lock = QuietProxyHandler.browser_progress_lock
        previous_progress = QuietProxyHandler.browser_login_progress
        previous_profiles = QuietProxyHandler.profiles
        previous_active = QuietProxyHandler.active_profile_id
        previous_state = QuietProxyHandler.state
        release = threading.Event()
        entered = threading.Event()
        entered_threads: list[str] = []
        cancellation_callbacks: list[object] = []
        sockets: list[app.socket.socket] = []

        def fake_login(**kwargs):
            entered_threads.append(threading.current_thread().name)
            cancellation_callbacks.append(kwargs.get("cancel_check"))
            entered.set()
            release.wait(timeout=5)
            return fake_state()

        def read_response(sock: app.socket.socket) -> tuple[int, dict]:
            sock.settimeout(5)
            chunks: list[bytes] = []
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
            raw = b"".join(chunks)
            header, body = raw.split(b"\r\n\r\n", 1)
            status = int(header.split(b"\r\n", 1)[0].split(b" ", 2)[1])
            return status, json.loads(body.decode("utf-8"))

        try:
            app._PLAYWRIGHT_PACKAGE_AVAILABLE = True
            app.get_browser_login_state = fake_login
            app._CAPTCHA_MODE = "browser"
            QuietProxyHandler.fresh_captcha_browser = True
            QuietProxyHandler.browser_flow_lock = threading.Lock()
            QuietProxyHandler.browser_progress_lock = threading.RLock()
            QuietProxyHandler.browser_login_progress = {
                "running": False,
                "mode": "",
                "stage": "空闲",
                "updated_at": "",
                "error": "",
            }
            QuietProxyHandler.profiles = {}
            QuietProxyHandler.active_profile_id = ""
            QuietProxyHandler.state = None

            head = (
                b"POST /api/auth/browser-login HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: 2\r\n"
                b"Connection: close\r\n\r\n"
            )
            for _ in range(2):
                sock = app.socket.create_connection(("127.0.0.1", self.server.server_port), timeout=2)
                sock.sendall(head)
                sockets.append(sock)
            deadline = time.time() + 2
            while self.server.handler_status()["active"] < 2 and time.time() < deadline:
                time.sleep(0.01)
            for sock in sockets:
                sock.sendall(b"{}")

            self.assertTrue(entered.wait(timeout=2))
            time.sleep(0.15)
            self.assertEqual(1, len(entered_threads), "同一时刻只能有一个浏览器 worker")
            self.assertEqual(1, len(cancellation_callbacks))
            self.assertTrue(callable(cancellation_callbacks[0]))

            status, raw = self.request("GET", "/api/auth/browser-login/status")
            progress = json.loads(raw)
            self.assertEqual(200, status)
            self.assertTrue(progress["running"])
            self.assertTrue(progress["locked"])
            self.assertEqual("login", progress["mode"])

            status, raw = self.request("POST", "/api/auth/captcha-refresh", {})
            busy = json.loads(raw)
            self.assertEqual(409, status)
            self.assertEqual("auth_flow_busy", busy["error"]["code"])
            self.assertTrue(busy["flow"]["running"])
            self.assertTrue(busy["flow"]["locked"])
            self.assertEqual("login", busy["flow"]["mode"])

            release.set()
            responses = [read_response(sock) for sock in sockets]
            self.assertEqual([200, 409], sorted(status for status, _payload in responses))
            rejected = next(payload for status, payload in responses if status == 409)
            self.assertEqual("auth_flow_busy", rejected["error"]["code"])
            with QuietProxyHandler.browser_progress_lock:
                final_progress = dict(QuietProxyHandler.browser_login_progress)
            self.assertFalse(final_progress["running"])
            self.assertEqual("已保存并切换账号", final_progress["stage"])
            self.assertFalse(QuietProxyHandler.browser_flow_lock.locked())
        finally:
            release.set()
            for sock in sockets:
                sock.close()
            app._PLAYWRIGHT_PACKAGE_AVAILABLE = previous_available
            app.get_browser_login_state = previous_login
            app._CAPTCHA_MODE = previous_mode
            QuietProxyHandler.fresh_captcha_browser = previous_fresh
            QuietProxyHandler.browser_flow_lock = previous_flow_lock
            QuietProxyHandler.browser_progress_lock = previous_progress_lock
            QuietProxyHandler.browser_login_progress = previous_progress
            QuietProxyHandler.profiles = previous_profiles
            QuietProxyHandler.active_profile_id = previous_active
            QuietProxyHandler.state = previous_state

    def test_response_store_never_exceeds_configured_cap(self) -> None:
        handler = QuietProxyHandler.__new__(QuietProxyHandler)
        QuietProxyHandler.response_store = {}
        total = app.MAX_RESPONSE_STORE_ITEMS + 5
        monotonic_before = time.monotonic()
        for index in range(total):
            handler._store_response(f"resp_{index}", {"index": index}, [])
        monotonic_after = time.monotonic()
        self.assertEqual(app.MAX_RESPONSE_STORE_ITEMS, len(handler.response_store))
        self.assertNotIn("resp_0", handler.response_store)
        self.assertIn(f"resp_{total - 1}", handler.response_store)
        newest = handler.response_store[f"resp_{total - 1}"]
        self.assertGreaterEqual(newest.expires_at, monotonic_before + app.RESPONSE_STORE_TTL_SECONDS)
        self.assertLessEqual(newest.expires_at, monotonic_after + app.RESPONSE_STORE_TTL_SECONDS)
        self.assertGreater(newest.size_bytes, 0)

    def test_response_store_enforces_total_and_per_item_byte_budgets(self) -> None:
        handler = QuietProxyHandler.__new__(QuietProxyHandler)
        QuietProxyHandler.response_store = {}
        previous_total = app.MAX_RESPONSE_STORE_BYTES
        previous_item = app.MAX_STORED_RESPONSE_BYTES
        try:
            app.MAX_RESPONSE_STORE_BYTES = 500
            app.MAX_STORED_RESPONSE_BYTES = 400
            for index in range(5):
                self.assertTrue(handler._store_response(f"resp_bytes_{index}", {"text": "x" * 150}, []))

            status = handler._response_store_status()
            self.assertLessEqual(status["bytes"], 500)
            self.assertLess(len(handler.response_store), 5)
            self.assertNotIn("resp_bytes_0", handler.response_store)
            self.assertIn("resp_bytes_4", handler.response_store)

            app.MAX_STORED_RESPONSE_BYTES = 64
            accepted = handler._store_response("resp_oversized", {"text": "y" * 500}, [])
            self.assertFalse(accepted)
            self.assertNotIn("resp_oversized", handler.response_store)
        finally:
            app.MAX_RESPONSE_STORE_BYTES = previous_total
            app.MAX_STORED_RESPONSE_BYTES = previous_item
            QuietProxyHandler.response_store = {}

        html = app.WEB_INDEX_PATH.read_text(encoding="utf-8")
        self.assertIn('id="info-response-store"', html)
        self.assertIn("responseStore.max_items", html)
        self.assertIn('id="info-history-store"', html)
        self.assertIn("historyStore.max_records", html)
        self.assertIn('id="info-log-store"', html)
        self.assertIn('id="info-auto-delete"', html)
        self.assertIn('id="stats-runtime-delete-queue"', html)
        self.assertIn('id="info-http-handlers"', html)
        self.assertIn('id="stats-runtime-http-handlers"', html)
        self.assertIn('id="info-upstream-readers"', html)
        self.assertIn('id="stats-runtime-upstream-readers"', html)
        self.assertIn('id="info-sse-heartbeat"', html)
        self.assertIn('id="stats-runtime-sse-heartbeat"', html)
        self.assertIn('id="info-context-cache"', html)
        self.assertIn('id="stats-runtime-context-cache"', html)
        self.assertIn("runtime.request_timeouts", html)

    def test_claude_code_history_and_model_markers(self) -> None:
        # Claude Code serializes historical tool calls as <tool_call> text;
        # the transcript must not hand that markup back to GLM as a template.
        transcript = app.build_history_transcript(
            [
                {"role": "user", "content": "看看项目"},
                {"role": "assistant", "content": "我来看看。<tool_call>Bash command=\"ls\" description=\"List\"</tool_call>"},
            ]
        )
        self.assertIn("[assistant]\n我来看看。[called tool: Bash (command: ls, description: List)]", transcript)
        self.assertNotIn("<tool_call>", transcript)
        self.assertNotIn("</tool_call>", transcript)

        # Claude Code appends context-window hints like [1M] to model IDs.
        self.assertEqual("glm-5.2", app.normalize_model("glm-5.2[1M]"))
        self.assertEqual("glm-5.2", app.normalize_model("glm-5.2-forcehistory[1M]"))
        self.assertEqual("glm-5.2", app.normalize_model("GLM-5.2-forcehistory[200k]"))
        self.assertTrue(app.is_force_history_model("glm-5.2-forcehistory[1M]"))
        self.assertTrue(app.is_no_thinking_model("glm-5.2[1m]-nothinking"))

    def test_fresh_captcha_falls_back_to_stored(self) -> None:
        state = fake_state()
        state.captcha_verify_param = "stored-captcha-280"

        class BrokenWorker:
            def solve(self, *_args, **_kwargs):
                raise TimeoutError("captcha worker timed out after 45.0s")

        class ExplodingWorker:
            def solve(self, *_args, **_kwargs):
                raise AssertionError("worker must be skipped during cooldown")

        original_mode = app._CAPTCHA_MODE
        original_backoff = app.CAPTCHA_RETRY_BACKOFF_SECONDS
        try:
            app._CAPTCHA_MODE = "browser"  # 本测试只考察浏览器 worker 路径
            app.CAPTCHA_RETRY_BACKOFF_SECONDS = 0
            app._set_captcha_degraded(-3600)  # 清除冷却
            result = app.resolve_fresh_captcha(state, "glm-5.2", BrokenWorker(), timeout_ms=1000)
            self.assertEqual("stored-captcha-280", result)
            # 冷却期内直接复用存储验证码，不再触发慢速浏览器流程。
            result = app.resolve_fresh_captcha(state, "glm-5.2", ExplodingWorker(), timeout_ms=1000)
            self.assertEqual("stored-captcha-280", result)
            # 无存储验证码且 solver 失败 -> 同时提示检查本地求解器和登录态。
            empty = fake_state()
            empty.captcha_verify_param = ""
            app._set_captcha_degraded(-3600)
            with self.assertRaisesRegex(RuntimeError, "确认本地求解器可用"):
                app.resolve_fresh_captcha(empty, "glm-5.2", BrokenWorker(), timeout_ms=1000)
        finally:
            app._CAPTCHA_MODE = original_mode
            app.CAPTCHA_RETRY_BACKOFF_SECONDS = original_backoff
            app._set_captcha_degraded(-3600)

    def test_fresh_captcha_uses_fresh_value(self) -> None:
        state = fake_state()
        state.captcha_verify_param = "stored"

        class GoodWorker:
            def solve(self, *_args, **_kwargs):
                return "fresh-captcha"

        original_mode = app._CAPTCHA_MODE
        try:
            app._CAPTCHA_MODE = "browser"
            app._set_captcha_degraded(-3600)
            self.assertEqual("fresh-captcha", app.resolve_fresh_captcha(state, "glm-5.2", GoodWorker(), timeout_ms=1000))
        finally:
            app._CAPTCHA_MODE = original_mode
            app._set_captcha_degraded(-3600)

    def test_fresh_captcha_auto_prefers_happydom_and_falls_back(self) -> None:
        state = fake_state()

        class NeverWorker:
            def solve(self, *_args, **_kwargs):
                raise AssertionError("browser worker must be skipped while happydom succeeds")

        class GoodWorker:
            def solve(self, *_args, **_kwargs):
                return "fresh-captcha"

        original_mode = app._CAPTCHA_MODE
        original_backoff = app.CAPTCHA_RETRY_BACKOFF_SECONDS
        real_solver = app.get_happydom_captcha
        try:
            app._CAPTCHA_MODE = "auto"
            app.CAPTCHA_RETRY_BACKOFF_SECONDS = 0
            app._set_captcha_degraded(1800)  # 无存储验证码时冷却必须自愈，不能裸发
            app.get_happydom_captcha = lambda *_a, **_k: "hd-captcha"
            self.assertEqual(
                "hd-captcha", app.resolve_fresh_captcha(state, "glm-5.2", NeverWorker(), timeout_ms=1000)
            )
            # happydom 返回空 -> 回落到浏览器 worker（先清验证码池，避开预解残留）。
            app._CAPTCHA_POOL.clear()
            app._set_captcha_degraded(-3600)
            app.get_happydom_captcha = lambda *_a, **_k: ""
            self.assertEqual(
                "fresh-captcha", app.resolve_fresh_captcha(state, "glm-5.2", GoodWorker(), timeout_ms=1000)
            )
        finally:
            app._CAPTCHA_MODE = original_mode
            app.CAPTCHA_RETRY_BACKOFF_SECONDS = original_backoff
            app.get_happydom_captcha = real_solver
            app._set_captcha_degraded(-3600)

    def test_web_chat_non_stream_roundtrip(self) -> None:
        original_stream = app.stream_zai_completion

        def plain_stream(_state, _prompt, **_kwargs):
            context = _kwargs.get("context_out")
            if isinstance(context, dict):
                context.update({"chat_id": "00000000-0000-0000-0000-0000000000bb"})
            yield 'data: {"data":{"delta_content":"你好，我是 GLM。","phase":"answer"}}'

        try:
            app.stream_zai_completion = plain_stream
            status, raw = self.request("POST", "/api/chat", {"message": "你好", "stream": False})
        finally:
            app.stream_zai_completion = original_stream
        data = json.loads(raw)
        self.assertEqual(200, status)
        self.assertTrue(data["ok"])
        self.assertEqual("你好，我是 GLM。", data["answer"])
        self.assertEqual("00000000-0000-0000-0000-0000000000bb", data["chat_id"])
        self.assertTrue(data["chat_delete_pending"])
        self.assertEqual(["00000000-0000-0000-0000-0000000000bb"], self.deleted_chats)

    def test_failed_background_chat_delete_remains_in_restart_journal(self) -> None:
        original_delete = app.delete_zai_chat

        def failing_delete(_state, _chat_id, **_kwargs):
            raise app.UpstreamRequestError("temporary cleanup outage")

        try:
            app.delete_zai_chat = failing_delete
            status, raw = self.request("POST", "/api/chat", {"message": "journal cleanup", "stream": False})
        finally:
            app.delete_zai_chat = original_delete
        self.assertEqual(200, status)
        payload = json.loads(raw)
        self.assertTrue(payload["chat_delete_pending"])
        journal_status = app.pending_chat_delete_status()
        self.assertEqual(1, journal_status["journal_pending"])
        stored = json.loads(app.PENDING_DELETE_STORE_PATH.read_text(encoding="utf-8"))
        self.assertEqual("auto_delete", stored["items"][0]["reason"])
        self.assertEqual(1, stored["items"][0]["attempts"])

    def test_file_upload_requires_active_state(self) -> None:
        original_state = QuietProxyHandler.state
        original_profiles = QuietProxyHandler.profiles
        original_active = QuietProxyHandler.active_profile_id
        try:
            QuietProxyHandler.state = None
            QuietProxyHandler.profiles = {}
            QuietProxyHandler.active_profile_id = ""
            status, raw = self.request(
                "POST",
                "/api/files/upload?filename=report.txt",
                b"hello world",
                {"Content-Type": "application/octet-stream"},
            )
        finally:
            QuietProxyHandler.state = original_state
            QuietProxyHandler.profiles = original_profiles
            QuietProxyHandler.active_profile_id = original_active
        self.assertEqual(400, status)
        data = json.loads(raw)
        self.assertIn("登录态", data["error"]["message"])


    def test_captcha_worker_routes_concurrent_results_to_the_original_caller(self) -> None:
        worker = app.CaptchaWorker(idle_timeout_sec=0)
        first_taken = threading.Event()
        errors: list[BaseException] = []
        results: dict[str, str] = {}
        results_lock = threading.Lock()

        def fake_run() -> None:
            first = worker._requests.get(timeout=2)
            self.assertIsNotNone(first)
            first_taken.set()
            second = worker._requests.get(timeout=2)
            self.assertIsNotNone(second)
            assert first is not None and second is not None
            # Deliberately complete in reverse order. A shared result queue can
            # let each waiter consume and discard the other request's result.
            worker._publish(second, f"captcha-{second.selected_model}")
            worker._publish(first, f"captcha-{first.selected_model}")

        worker._run = fake_run  # type: ignore[method-assign]

        def solve(model: str) -> None:
            try:
                value = worker.solve(fake_state(), model, timeout_ms=10_000)
                with results_lock:
                    results[model] = value
            except BaseException as exc:
                errors.append(exc)

        first_thread = threading.Thread(target=solve, args=("model-a",), daemon=True)
        second_thread = threading.Thread(target=solve, args=("model-b",), daemon=True)
        try:
            first_thread.start()
            self.assertTrue(first_taken.wait(timeout=2))
            second_thread.start()
            first_thread.join(timeout=3)
            second_thread.join(timeout=3)
            self.assertFalse(first_thread.is_alive())
            self.assertFalse(second_thread.is_alive())
            self.assertEqual([], errors)
            self.assertEqual(
                {"model-a": "captcha-model-a", "model-b": "captcha-model-b"},
                results,
            )
        finally:
            worker.close()

    def test_captcha_worker_startup_failure_reaches_waiter_immediately(self) -> None:
        class BrokenPlaywrightFactory:
            def __call__(self):
                return self

            def start(self):
                raise RuntimeError("playwright startup failed")

        original_loader = app._captcha_playwright
        worker = app.CaptchaWorker(idle_timeout_sec=0)
        try:
            app._captcha_playwright = lambda: BrokenPlaywrightFactory()
            started = time.monotonic()
            with self.assertRaisesRegex(RuntimeError, "playwright startup failed"):
                worker.solve(fake_state(), "glm-5.2", timeout_ms=10_000)
            self.assertLess(time.monotonic() - started, 2.0)
            status = worker.status()
            self.assertFalse(status["thread_alive"])
            self.assertEqual(0, status["pending"])
        finally:
            worker.close()
            app._captcha_playwright = original_loader

    def test_captcha_worker_backpressure_is_bounded(self) -> None:
        worker = app.CaptchaWorker(idle_timeout_sec=0, max_pending=1)
        worker._start_locked = lambda: None  # type: ignore[method-assign]
        parked = app._CaptchaWorkItem(
            request_id="parked",
            state=fake_state(),
            selected_model="glm-5.2",
            timeout_ms=10_000,
            deadline=time.monotonic() + 10,
        )
        worker._requests.put_nowait(parked)
        try:
            with self.assertRaisesRegex(RuntimeError, "backlog is full"):
                worker.solve(fake_state(), "glm-5.2", timeout_ms=10_000)
            status = worker.status()
            self.assertEqual(1, status["pending"])
            self.assertEqual(1, status["max_pending"])
            self.assertEqual(1, status["backpressure_total"])
        finally:
            worker.close()

    def test_captcha_worker_cancellation_marks_queued_task_abandoned(self) -> None:
        worker = app.CaptchaWorker(idle_timeout_sec=0)
        worker._start_locked = lambda: None  # type: ignore[method-assign]
        checks = 0

        def cancel_check() -> None:
            nonlocal checks
            checks += 1
            if checks >= 2:
                raise app.ServiceShuttingDown("test shutdown")

        try:
            with self.assertRaises(app.ServiceShuttingDown):
                worker.solve(
                    fake_state(),
                    "glm-5.2",
                    timeout_ms=10_000,
                    cancel_check=cancel_check,
                )
            item = worker._requests.get_nowait()
            self.assertIsNotNone(item)
            assert item is not None
            self.assertTrue(item.cancelled.is_set())
            self.assertTrue(item.result.empty())
        finally:
            worker.close()

    def test_captcha_worker_reuses_page_and_retries_once(self) -> None:
        class FakePage:
            def goto(self, *_a, **_kw):
                pass

            def evaluate(self, *_a, **_kw):
                return None

            def reload(self, *_a, **_kw):
                pass

            def wait_for_timeout(self, *_a):
                pass

        class FakeContext:
            def new_page(self):
                return FakePage()

        class FakeBrowser:
            def __init__(self):
                self.closed = False

            def new_context(self, **_kw):
                return FakeContext()

            def close(self):
                self.closed = True

        class FakeChromium:
            def __init__(self):
                self.browsers = []

            def launch(self, **_kw):
                browser = FakeBrowser()
                self.browsers.append(browser)
                return browser

        class FakePW:
            def __init__(self, chromium):
                self.chromium = chromium
                self.stopped = False

            def start(self):
                return self

            def stop(self):
                self.stopped = True

        class FakePlaywrightFactory:
            def __init__(self):
                self.chromium = FakeChromium()
                self.current = None

            def __call__(self):
                self.current = FakePW(self.chromium)
                return self.current

        factory = FakePlaywrightFactory()
        original_loader = app._captcha_playwright
        original_solve = app.solve_captcha_on_page
        try:
            app._captcha_playwright = lambda: factory
            calls = {"n": 0}

            def fake_solve(_page, _timeout_ms):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("captcha page broken")
                return "captcha-value"

            app.solve_captcha_on_page = fake_solve
            worker = app.CaptchaWorker(idle_timeout_sec=0)
            try:
                # 首次：页面失败后自动重建并重试成功
                self.assertEqual("captcha-value", worker.solve(fake_state(), "glm-5.2"))
                self.assertEqual(2, calls["n"])
                self.assertEqual(2, len(factory.chromium.browsers))
                # 第二次：相同 token/device 复用页面，不再 launch
                self.assertEqual("captcha-value", worker.solve(fake_state(), "glm-5.2"))
                self.assertEqual(3, calls["n"])
                self.assertEqual(2, len(factory.chromium.browsers))
            finally:
                worker.close()
            self.assertTrue(factory.chromium.browsers[-1].closed)
            self.assertTrue(factory.current.stopped)
        finally:
            app._captcha_playwright = original_loader
            app.solve_captcha_on_page = original_solve

    def test_captcha_worker_failure_is_reported_after_retry(self) -> None:
        class FakePage:
            def goto(self, *_a, **_kw):
                pass

            def evaluate(self, *_a, **_kw):
                return None

            def reload(self, *_a, **_kw):
                pass

            def wait_for_timeout(self, *_a):
                pass

        class FakeContext:
            def new_page(self):
                return FakePage()

        class FakeBrowser:
            def __init__(self):
                self.closed = False

            def new_context(self, **_kw):
                return FakeContext()

            def close(self):
                self.closed = True

        class FakeChromium:
            def __init__(self):
                self.browsers = []

            def launch(self, **_kw):
                browser = FakeBrowser()
                self.browsers.append(browser)
                return browser

        class FakePW:
            def __init__(self, chromium):
                self.chromium = chromium

            def start(self):
                return self

            def stop(self):
                pass

        class FakePlaywrightFactory:
            def __init__(self):
                self.chromium = FakeChromium()

            def __call__(self):
                return FakePW(self.chromium)

        factory = FakePlaywrightFactory()
        original_loader = app._captcha_playwright
        original_solve = app.solve_captcha_on_page
        try:
            app._captcha_playwright = lambda: factory
            calls = {"n": 0}

            def always_fail(_page, _timeout_ms):
                calls["n"] += 1
                raise RuntimeError("captcha always broken")

            app.solve_captcha_on_page = always_fail
            worker = app.CaptchaWorker(idle_timeout_sec=0)
            try:
                with self.assertRaises(RuntimeError):
                    worker.solve(fake_state(), "glm-5.2")
            finally:
                worker.close()
            self.assertEqual(2, calls["n"])
            self.assertEqual(2, len(factory.chromium.browsers))
        finally:
            app._captcha_playwright = original_loader
            app.solve_captcha_on_page = original_solve


    def test_failed_protocol_turn_cleans_up_upstream_chat(self) -> None:
        original_stream = app.stream_zai_completion

        def failing_stream(_state, _prompt, **_kwargs):
            context = _kwargs.get("context_out")
            if isinstance(context, dict):
                context.update({"chat_id": "00000000-0000-0000-0000-0000000000cc"})
            yield 'data: {"data":{"delta_content":"开始生成","phase":"answer"}}'
            raise RuntimeError("上游中断")

        try:
            app.stream_zai_completion = failing_stream
            status, raw = self.request(
                "POST",
                "/v1/chat/completions",
                {
                    "model": "glm-5.2",
                    "messages": [{"role": "user", "content": "你好"}],
                    "stream": False,
                },
            )
        finally:
            app.stream_zai_completion = original_stream
        self.assertEqual(500, status)
        self.assertIn("上游中断", raw)
        self.assertEqual(["00000000-0000-0000-0000-0000000000cc"], self.deleted_chats)

    def test_zero_delta_upstream_interrupt_is_exposed_as_retryable(self) -> None:
        original_stream = app.stream_zai_completion

        def failing_before_delta(_state, _prompt, **_kwargs):
            if False:
                yield ""
            raise RuntimeError("上游中断")

        try:
            app.stream_zai_completion = failing_before_delta
            status, raw = self.request(
                "POST",
                "/v1/chat/completions",
                {
                    "model": "glm-5.2",
                    "messages": [{"role": "user", "content": "你好"}],
                    "stream": False,
                },
            )
        finally:
            app.stream_zai_completion = original_stream
        self.assertEqual(503, status)
        self.assertIn("上游中断", raw)

    def test_tool_parser_xml_fallback_for_unescaped_text(self) -> None:
        tools = [{"name": "search", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}}}]
        policy = app.ToolChoice()
        markup = '<tool_calls><invoke name="search"><parameter name="query">R&D 最新进展</parameter></invoke></tool_calls>'
        calls = app.parse_tool_calls_from_output(markup, tools, policy)
        self.assertEqual(1, len(calls))
        self.assertEqual("search", calls[0].name)
        self.assertEqual({"query": "R&D 最新进展"}, calls[0].arguments)

    def test_tool_markup_in_thinking_is_stripped(self) -> None:
        tools = [{"name": "get_weather", "parameters": {"type": "object"}}]
        request = app.ProtocolRequest(
            surface="openai_chat",
            response_model="glm-5.2",
            options=app.ChatOptions(),
            stream=False,
            messages=[],
            context_text="",
            execution_prompt="",
            files=[],
            tools=tools,
            tool_choice=app.ToolChoice(),
            context_as_file=False,
        )
        markup = (
            '<|DSML|tool_calls><|DSML|invoke name="get_weather">'
            '<|DSML|parameter name="city"><![CDATA[北京]]></|DSML|parameter>'
            '</|DSML|invoke></|DSML|tool_calls>'
        )
        turn = app.finalize_protocol_turn(request, "", markup)
        self.assertEqual([], turn.tool_calls)
        self.assertEqual("", turn.text)
        self.assertNotIn("DSML", turn.thinking)

    def test_openai_tool_result_roundtrip_via_history(self) -> None:
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
                },
            }
        ]
        status, raw = self.request(
            "POST",
            "/v1/chat/completions",
            {
                "model": "glm-5.2",
                "stream": False,
                "messages": [
                    {"role": "user", "content": "北京天气"},
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "get_weather", "arguments": '{"city":"北京"}'},
                            }
                        ],
                    },
                    {"role": "tool", "tool_call_id": "call_1", "content": "25 度"},
                ],
                "tools": tools,
            },
        )
        self.assertEqual(200, status)
        data = json.loads(raw)
        message = data["choices"][0]["message"]
        self.assertEqual("先说明。", message["content"])
        self.assertEqual("get_weather", message["tool_calls"][0]["function"]["name"])
        self.assertEqual("tool_calls", data["choices"][0]["finish_reason"])

    def test_responses_previous_response_id_tool_roundtrip(self) -> None:
        tools = [
            {
                "type": "function",
                "name": "get_weather",
                "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
            }
        ]
        status, raw = self.request(
            "POST",
            "/v1/responses",
            {"model": "gpt-5", "input": "北京天气", "tools": tools, "stream": False},
        )
        self.assertEqual(200, status)
        first = json.loads(raw)
        function_calls = [item for item in first["output"] if item["type"] == "function_call"]
        self.assertEqual(1, len(function_calls))
        response_id = first["id"]

        status, raw2 = self.request(
            "POST",
            "/v1/responses",
            {
                "model": "gpt-5",
                "previous_response_id": response_id,
                "input": [
                    {
                        "type": "function_call_output",
                        "call_id": function_calls[0]["call_id"],
                        "output": "25 度",
                    }
                ],
                "stream": False,
            },
        )
        self.assertEqual(200, status)
        second = json.loads(raw2)
        self.assertTrue(second["id"].startswith("resp_"))
        self.assertEqual("completed", second["status"])


if __name__ == "__main__":


    unittest.main()
