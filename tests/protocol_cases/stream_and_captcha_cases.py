"""StreamAndCaptcha protocol regression cases."""

from protocol_cases.support import *  # noqa: F403


class StreamAndCaptchaCases:
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
        for path in (
            "/",
            "/assets/styles.css",
            "/assets/core.js",
            "/assets/history.js",
            "/assets/chat.js",
            "/assets/admin.js",
            "/api/status",
            "/api/hello",
            "/healthz",
        ):
            request = Request(self.base_url + path, method="HEAD")
            with urlopen(request, timeout=8) as response:
                self.assertEqual(200, response.status, path)
                self.assertEqual(b"", response.read(), "HEAD 响应不应包含响应体")
