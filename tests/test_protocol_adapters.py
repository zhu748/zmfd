import ast
import io
import json
import logging
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
        app._AUTO_DELETE_INLINE = True
        self._captcha_prefetch_prev = app._CAPTCHA_PREFETCH_ENABLED
        app._CAPTCHA_PREFETCH_ENABLED = False
        app._CAPTCHA_POOL.clear()

        def fake_upload(_state, context_text: str, filename: str | None = None, label: str = ""):
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

        def fake_delete(_state, chat_id: str):
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
        self._history_conf_prev = dict(app._HISTORY_CONF)
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
        app._CAPTCHA_PREFETCH_ENABLED = self._captcha_prefetch_prev
        app._CAPTCHA_POOL.clear()
        app.HISTORY_STORE_PATH = self._history_path_prev
        app.HISTORY_DETAIL_DIR = self._history_detail_prev
        app._HISTORY_CACHE = None
        app._HISTORY_DIRTY.clear()
        app._HISTORY_DELETED.clear()
        app._HISTORY_CONF.clear()
        app._HISTORY_CONF.update(self._history_conf_prev)
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
        auto_turn = app.finalize_protocol_turn(auto, "我来查一下。", markup)
        # Auto mode stays conservative: prose plus a thinking example must not
        # turn into a real call.
        self.assertEqual([], auto_turn.tool_calls)
        self.assertEqual("", auto_turn.thinking)

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

        def fake_http_json(method, url, headers, payload=None):
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
            self.assertEqual(200, status)
            data = json.loads(body)
            self.assertEqual(2, data["count"])
            self.assertEqual(["file_1", "file_2"], data["removed"])
            self.assertEqual(2, len(delete_calls))
            self.assertTrue(all(url.endswith("/api/v1/files/file_1") or url.endswith("/api/v1/files/file_2") for _, url in delete_calls))
        finally:
            app.http_json = original_http_json

    def test_delete_zai_file_404_405_semantics(self) -> None:
        original_http_json = app.http_json
        state = fake_state()
        errors: list[HTTPError] = []
        try:
            seen: list[int] = []

            def fake_http_json(method, url, headers, payload=None):
                seen.append(404)
                error = HTTPError(url, 404, "Not Found", None, io.BytesIO(b"gone"))
                errors.append(error)
                raise error

            app.http_json = fake_http_json
            self.assertTrue(app.delete_zai_file(state, "file_1"))
            self.assertTrue(errors[-1].closed)

            def fake_405(method, url, headers, payload=None):
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

        def fake_delete(_state, file_id):
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
        self.assertEqual(2, sum(bucket["total"] for bucket in metrics["timeline"]))
        serialized = json.dumps(metrics, ensure_ascii=False)
        self.assertNotIn("secret-prompt", serialized)
        self.assertNotIn("secret-answer", serialized)
        self.assertNotIn("secret-upstream-error", serialized)

        self.request("GET", "/api/hello")
        status, raw = self.request("GET", "/api/metrics?hours=24")
        self.assertEqual(200, status)
        payload = json.loads(raw)
        self.assertTrue(payload["ok"])
        self.assertEqual(24, payload["window_hours"])
        self.assertEqual(2, payload["history"]["requests"])
        self.assertGreaterEqual(payload["runtime"]["requests_total"], 1)
        self.assertTrue(any(item["path"] == "/api/hello" for item in payload["runtime"]["top_paths"]))

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
        self.assertFalse(app.is_transient_upstream_error("AUTH_REQUIRED: unauthorized"))
        self.assertFalse(app.is_transient_upstream_error("FRONTEND_CAPTCHA_REQUIRED: missing captcha"))
        self.assertFalse(app.is_transient_upstream_error("上游未按 tool_choice 输出工具调用: get_weather"))
        self.assertFalse(app.is_transient_upstream_error(""))

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

        def fake_delete(_state, chat_id):
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

        upstream_chunks = [
            [
                b'data: {"data":{"delta_content":"FAILED_THINKING","phase":"thinking"}}\n\n',
                b'data: {"data":{"delta_content":"<tool_calls><invoke name=bad>","phase":"answer"}}\n\n',
            ],
            [
                b'data: {"data":{"delta_content":"GOOD_THINKING","phase":"thinking"}}\n\n',
                (
                    'data: {"data":{"delta_content":"<tool_calls><invoke name=\\"get_weather\\">'
                    '<parameter name=\\"city\\">北京</parameter></invoke></tool_calls>","phase":"answer"}}\n\n'
                ).encode("utf-8"),
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
                    "tool_choice": "required",
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
        self.assertEqual(2, len(self.deleted_chats), "失败首轮与成功重试会话都应清理")
        records = app.local_history_records()
        self.assertEqual(1, len(records), "一次客户端请求的格式纠错不应产生两条历史记录")
        detail = app.get_local_history_record(str(records[0]["id"])) or {}
        self.assertIn("Tool-call correction:", str(detail.get("final_prompt") or ""))

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
        app.delete_zai_chat = lambda _s, _c: True
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

    def test_captcha_pool_expiry(self) -> None:
        app._CAPTCHA_POOL.append(("stale-captcha", time.monotonic() - app.CAPTCHA_POOL_TTL_SEC - 1))
        app._CAPTCHA_POOL.append(("fresh-captcha", time.monotonic()))
        self.assertEqual("fresh-captcha", app._captcha_pool_take(), "过期条目应被跳过")
        self.assertEqual("", app._captcha_pool_take())

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
        app.delete_zai_chat = lambda _s, chat_id: deleted.append(chat_id) or True
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
            raise RuntimeError("获取对话列表失败: HTTP Error 500: upstream sad")

        app.list_zai_chats = boom
        try:
            status, body = self.request("GET", "/api/history/chats")
            self.assertEqual(400, status)
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
        finally:
            app._history_write_atomic_locked = original_write

        with app._HISTORY_LOCK:
            app._history_persist_locked()
        self.assertNotIn(rid, app._HISTORY_DIRTY)
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
            [{"kind": "tools", "name": "tools.txt", "content": content}]
        )
        self.assertEqual(1, len(context_files))
        self.assertEqual("tools", context_files[0]["kind"])
        self.assertEqual(app.HISTORY_CONTEXT_FILE_CHARS, len(context_files[0]["content"]))
        self.assertEqual(len(content), context_files[0]["original_chars"])
        self.assertTrue(context_files[0]["truncated"])

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
            raise RuntimeError("获取对话详情失败: HTTP Error 500: sad")

        real = (app.get_zai_chat_detail,)
        app.get_zai_chat_detail = boom
        try:
            status, body = self.request("GET", f"/api/history/chat?id={chat_uuid}")
            self.assertEqual(200, status)
            data = json.loads(body)
            self.assertEqual("local", data["source"])
            self.assertEqual("本地答", data["messages"][1]["content"])

            status, body = self.request("GET", f"/api/history/chat?id={self.HISTORY_CHAT_ID}")
            self.assertEqual(400, status)
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
        status, body = self.request("GET", f"/api/history/record?id={record_id}")
        self.assertEqual(400, status)
        self.assertIn("不存在", body)

        self._make_record("00000000-0000-0000-0000-0000000000c2", "清空问", "清空答")
        status, body = self.request("POST", "/api/history/clear", {})
        self.assertEqual(200, status)
        self.assertEqual(1, json.loads(body)["removed"])
        status, body = self.request("GET", "/api/history/records")
        self.assertEqual(0, json.loads(body)["count"])

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
        app.delete_zai_chat = lambda _s, _c: True
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
        app.delete_zai_chat = lambda _s, _c: True
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

        chunks = ['data: {"data":{"delta_content":"第二次成功","phase":"answer"}}\n\n'.encode("utf-8")]
        real = (app.new_chat, app.delete_zai_chat, app.urlopen)
        calls: list[str] = []

        def flaky_new_chat(_s, _p, options=None):
            calls.append("new_chat")
            if len(calls) == 1:
                raise RuntimeError("chat not found: 会话已被上游清理")
            return ("00000000-0000-0000-0000-0000000000cc", "u2")

        app.new_chat = flaky_new_chat
        app.delete_zai_chat = lambda _s, _c: True
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

        def fake_http_json(_method, url, _headers, _payload=None):
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
                return iter([b'data: {"data":{"delta_content":"OK","phase":"answer"}}\n\n'])

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
            app._AUTO_DELETE_INLINE = prev_inline

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

        def fake_http_json(_method, url, headers, _payload=None):
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

    def test_api_key_can_be_configured_from_panel(self) -> None:
        status, raw = self.request("POST", "/api/settings/api-key", {"api_key": "panel-secret-1"})
        self.assertEqual(200, status)
        data = json.loads(raw)
        self.assertTrue(data["ok"])
        self.assertTrue(data["enabled"])
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
            self.assertEqual("test-user", data["user_name"])

            status, raw = self.request("GET", "/api/auth/profiles", headers={"Authorization": "Bearer test-api-key"})
            self.assertEqual(200, status)
            self.assertTrue(json.loads(raw)["ok"])

            status, raw = self.request("GET", "/api/auth/profiles", headers={"X-API-Key": "wrong-key"})
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
        self.assertEqual("glm-5.2", data["settings"]["model"])
        self.assertTrue(data["settings"]["auto_web_search"])
        self.assertFalse(data["settings"]["enable_thinking"])
        self.assertEqual("high", data["settings"]["reasoning_effort"])
        self.assertTrue(data["settings"]["include_thinking"])
        self.assertFalse(data["settings"]["delete_chat_after_completion"])
        self.assertEqual(600, data["settings"]["upstream_timeout_sec"])

        status, raw = self.request("GET", "/api/status")
        self.assertEqual(200, status)
        status_data = json.loads(raw)
        defaults = status_data["default_options"]
        self.assertEqual("glm-5.2", defaults["model"])
        self.assertTrue(defaults["include_thinking"])
        self.assertEqual(600, status_data["upstream_timeout_sec"])

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

    def test_direct_prompt_cleans_up_created_chat(self) -> None:
        original_stream = app.stream_zai_completion
        original_delete = app.delete_zai_chat
        deleted: list[str] = []

        def fake_stream(_state, _prompt, **_kwargs):
            context = _kwargs.get("context_out")
            if isinstance(context, dict):
                context.update({"chat_id": "00000000-0000-0000-0000-0000000000cd"})
            yield 'data: {"data":{"delta_content":"ok","phase":"answer"}}'

        def fake_delete(_state, chat_id):
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

        def slow_delete(_state, chat_id):
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
        self.assertIn("The other attached file enumerates the available function definitions", prompt)
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
            app.upload_context_package_to_zai = lambda _s, _text, filename=None, label="": {
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

        def partial_upload(_state, text, filename=None, label=""):
            if label == "history":
                raise RuntimeError("history upload failed")
            return {"id": "tools-partial", "filename": "tools-random.txt", "meta": {"size": len(text)}}

        app.upload_context_package_to_zai = partial_upload
        app.delete_zai_file = lambda _state, file_id: deleted.append(file_id) or True
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
        self.assertIn("附件 ${index + 1} · ${contextKindLabel(file.kind)}", html)
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
        self.assertEqual(1, html.count('id="reasoning-effort-group"'))
        self.assertIn('id="reasoning-effort-settings-group"', html)
        self.assertEqual(0.75, app.HISTORY_PROGRESS_INTERVAL_SECONDS)
        self.assertIn("这里每 750ms 重取一次", html)

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
            # 无存储验证码且 worker 失败 -> 明确提示重新授权。
            empty = fake_state()
            empty.captcha_verify_param = ""
            app._set_captcha_degraded(-3600)
            with self.assertRaisesRegex(RuntimeError, "重新完成浏览器授权"):
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
        self.assertEqual(1, len(turn.tool_calls))
        self.assertEqual("get_weather", turn.tool_calls[0].name)
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
