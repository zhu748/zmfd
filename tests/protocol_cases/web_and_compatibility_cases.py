"""WebAndCompatibility protocol regression cases."""

from protocol_cases.support import *  # noqa: F403


class WebAndCompatibilityCases:
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
        html = web_source()
        start = html.index("if (!state.statusDefaultsApplied)")
        end = html.index("state.statusDefaultsApplied = true", start)
        block = html[start:end]
        rebuild_at = block.index("syncEffortOptions();")
        restore_at = block.index("reasoningEffortSelect.value = defaultEffort;")
        self.assertLess(rebuild_at, restore_at)
        self.assertIn('const defaultEffort = defaults.reasoning_effort || "max";', block)
        self.assertIn("reasoningEffortSelect.options", block)

    def test_web_stream_requires_terminal_and_preserves_partial_output(self) -> None:
        html = web_source()
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
        html = web_source()
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
        html = web_source()
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
        html = web_source()
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

        html = web_source()
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
        real_available = app.happydom_captcha_available
        try:
            app._CAPTCHA_MODE = "auto"
            app.CAPTCHA_RETRY_BACKOFF_SECONDS = 0
            # This routing unit test must not depend on local node_modules.
            app.happydom_captcha_available = lambda: True
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
            app.happydom_captcha_available = real_available
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
