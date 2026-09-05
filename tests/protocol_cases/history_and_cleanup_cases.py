"""HistoryAndCleanup protocol regression cases."""

from protocol_cases.support import *  # noqa: F403


class HistoryAndCleanupCases:
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
            csp = response.headers.get("Content-Security-Policy", "")
            self.assertIn("frame-ancestors 'none'", csp)
            self.assertIn("script-src 'self';", csp)
            self.assertNotIn("script-src 'self' 'unsafe-inline'", csp)
            self.assertIn("camera=()", response.headers.get("Permissions-Policy", ""))
            self.assertEqual("no-cache", response.headers.get("Cache-Control"))
        with urlopen(self.base_url + "/api/status", timeout=8) as response:
            self.assertEqual("no-store", response.headers.get("Cache-Control"))
            self.assertIn("default-src 'self'", response.headers.get("Content-Security-Policy", ""))
        with urlopen(self.base_url + "/assets/styles.css", timeout=8) as response:
            self.assertEqual("text/css; charset=utf-8", response.headers.get("Content-Type"))
            self.assertEqual("no-cache", response.headers.get("Cache-Control"))
            self.assertIn(b":root", response.read())
        with urlopen(self.base_url + "/assets/core.js", timeout=8) as response:
            self.assertEqual("text/javascript; charset=utf-8", response.headers.get("Content-Type"))
            self.assertEqual("no-cache", response.headers.get("Cache-Control"))
            self.assertIn(b"const state", response.read())
        status, _raw = self.request("GET", "/assets/../glm2api.py")
        self.assertEqual(404, status, "静态资源路由不得暴露任意仓库文件")

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

        html = web_source()
        self.assertIn('id="history-search-input" class="input-custom" type="text" maxlength="256"', html)

    def test_remote_bind_requires_explicit_permission_and_api_key(self) -> None:
        app.validate_server_bind("127.0.0.1", False, "")
        app.validate_server_bind("::1", False, "")
        with self.assertRaisesRegex(RuntimeError, "--allow-remote"):
            app.validate_server_bind("0.0.0.0", False, "secret")
        with self.assertRaisesRegex(RuntimeError, "API Key"):
            app.validate_server_bind("0.0.0.0", True, "")
        app.validate_server_bind("0.0.0.0", True, "secret")

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
