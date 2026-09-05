"""ToolAndContext protocol regression cases."""

from protocol_cases.support import *  # noqa: F403


class ToolAndContextCases:
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

        focused_retry = app.protocol_request_with_tool_retry_hint(
            request,
            "malformed wrapper",
            '<tool_call>Read\nfile_path="README.md"\n</invoke>',
        )
        self.assertIn("appeared to target the declared tool Read", focused_retry.execution_prompt)
        self.assertIn(
            'Fresh serialization example:\n<|DSML|tool_calls>\n  <|DSML|invoke name="Read"',
            focused_retry.execution_prompt,
        )
        self.assertEqual("auto", focused_retry.tool_choice.mode)

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

        moderate_tools = [
            {
                "name": f"moderate_tool_{index:02d}",
                "description": "Synthetic definition." + (" neutral schema padding" * 140),
                "parameters": {"type": "object", "properties": {"input": {"type": "string"}}},
            }
            for index in range(16)
        ]
        moderate_text = app.build_tools_transcript(moderate_tools, app.ToolChoice(mode="auto"))
        moderate_parts = app.split_generated_context_text(moderate_text, "tools", "glm-5.3")
        self.assertGreater(len(moderate_text.encode("utf-8")), app.GLM53_CONTEXT_FILE_PART_BYTES)
        self.assertEqual(2, len(moderate_parts), "48 KiB edge should keep this tool package to two files")
        self.assertTrue(
            all(len(part.encode("utf-8")) <= app.GLM53_CONTEXT_FILE_PART_BYTES for part in moderate_parts)
        )
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
                "messages": [{"role": "user", "content": "H" * 600_000}],
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
                    "messages": [{"role": "user", "content": "H" * 600_000}],
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
                    "messages": [{"role": "user", "content": "H" * 600_000}],
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
