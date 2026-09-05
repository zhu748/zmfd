"""ServerRuntime protocol regression cases."""

from protocol_cases.support import *  # noqa: F403


class ServerRuntimeCases:
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
        html = web_source()
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
