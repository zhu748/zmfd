
    // ---- 统计与遥测 ----
    let metricHours = 24;
    let metricAutoTimer = null;
    let metricLastData = null;

    function metricWindowText(hours) {
      if (hours === 1) return "最近 1 小时";
      if (hours === 168) return "最近 7 天";
      return `最近 ${hours} 小时`;
    }

    function renderMetricTimeline(container, timeline) {
      if (!container) return;
      container.replaceChildren();
      const rows = Array.isArray(timeline) ? timeline : [];
      const maxTotal = Math.max(1, ...rows.map(row => Number(row.total) || 0));
      rows.forEach(row => {
        const total = Number(row.total) || 0;
        const column = document.createElement("div");
        column.className = "signal-column";
        const when = new Date(Number(row.start_ms) || 0);
        const timeLabel = isNaN(when.getTime()) ? "—" : when.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
        column.dataset.label = `${timeLabel} · 总计 ${total} / 成功 ${row.success || 0} / 错误 ${row.error || 0} / 停止 ${row.stopped || 0}`;
        const stack = document.createElement("div");
        stack.className = "signal-stack";
        stack.style.height = total ? `${Math.max(4, (total / maxTotal) * 100)}%` : "2px";
        ["success", "stopped", "error"].forEach(status => {
          const count = Number(row[status]) || 0;
          if (!count) return;
          const segment = document.createElement("span");
          segment.className = `signal-segment ${status}`;
          segment.style.flex = String(count);
          stack.appendChild(segment);
        });
        column.appendChild(stack);
        container.appendChild(column);
      });
    }

    function renderBreakdown(container, items, labelKey, metaBuilder, tone = "") {
      if (!container) return;
      container.replaceChildren();
      const rows = Array.isArray(items) ? items : [];
      if (!rows.length) {
        const empty = document.createElement("div");
        empty.className = "stats-empty";
        empty.textContent = "当前时间窗口暂无数据。";
        container.appendChild(empty);
        return;
      }
      const maxCount = Math.max(1, ...rows.map(item => Number(item.count) || 0));
      rows.forEach(item => {
        const row = document.createElement("div");
        row.className = "breakdown-row";
        const label = document.createElement("span");
        label.className = "breakdown-label";
        label.textContent = String(item[labelKey] || "unknown");
        label.title = label.textContent;
        const track = document.createElement("div");
        track.className = "breakdown-track";
        const fill = document.createElement("div");
        fill.className = `breakdown-fill ${tone}`.trim();
        fill.style.width = `${Math.max(2, ((Number(item.count) || 0) / maxCount) * 100)}%`;
        track.appendChild(fill);
        const meta = document.createElement("span");
        meta.className = "breakdown-meta";
        meta.textContent = metaBuilder(item);
        row.append(label, track, meta);
        container.appendChild(row);
      });
    }

    function renderStatsTable(container, items, nameKey, valueKey) {
      if (!container) return;
      container.replaceChildren();
      const rows = Array.isArray(items) ? items : [];
      if (!rows.length) {
        const empty = document.createElement("div");
        empty.className = "stats-empty";
        empty.textContent = "暂无数据。";
        container.appendChild(empty);
        return;
      }
      rows.forEach(item => {
        const row = document.createElement("div");
        row.className = "stats-table-row";
        const name = document.createElement("span");
        name.className = "stats-table-name";
        name.textContent = String(item[nameKey] || "unknown");
        name.title = name.textContent;
        const value = document.createElement("span");
        value.className = "stats-table-value";
        value.textContent = formatCompactNumber(item[valueKey]);
        row.append(name, value);
        container.appendChild(row);
      });
    }

    function renderMetrics(data) {
      metricLastData = data;
      const history = data.history || {};
      const runtime = data.runtime || {};
      const logs = data.logs || {};
      const autoDelete = runtime.auto_delete || {};
      const captchaWorker = runtime.captcha_worker || {};
      const httpHandlers = runtime.http_handlers || {};
      const uploadSlots = runtime.upload_slots || {};
      const upstreamResponses = runtime.upstream_responses || {};
      const upstreamReaders = runtime.upstream_readers || {};
      const sseHeartbeat = runtime.sse_heartbeat || {};
      const contextCache = runtime.context_cache || {};
      const statuses = history.statuses || {};
      const success = Number(statuses.success) || 0;
      const errors = Number(statuses.error) || 0;
      const stopped = Number(statuses.stopped) || 0;
      const outcomes = success + errors;
      const tokens = history.tokens || {};
      const generated = new Date(Number(data.generated_at) || Date.now());
      const generatedText = `更新 ${generated.toLocaleTimeString("zh-CN", { hour12: false })}`;

      document.querySelectorAll("[data-metric-hours]").forEach(button => {
        button.classList.toggle("active", Number(button.dataset.metricHours) === metricHours);
      });
      $("metric-generated").textContent = generatedText;
      $("metric-window-label").textContent = metricWindowText(metricHours);
      $("metric-request-count").textContent = formatCompactNumber(history.requests || 0);
      $("metric-request-sub").textContent = `成功 ${success} · 错误 ${errors} · 停止 ${stopped} · 保留 ${history.retained_total || 0}`;
      const successRate = $("metric-success-rate");
      successRate.textContent = formatPercent(history.success_rate, outcomes > 0);
      successRate.className = `metric-value ${outcomes && history.success_rate >= 0.95 ? "good" : outcomes && history.success_rate < 0.8 ? "bad" : ""}`.trim();
      $("metric-success-meta").textContent = outcomes ? `${success} / ${outcomes} 个有效结果` : "暂无成功或错误结果";
      $("metric-p95").textContent = formatElapsed(history.p95_elapsed_ms);
      $("metric-latency-meta").textContent = `平均 ${formatElapsed(history.avg_elapsed_ms)} · P50 ${formatElapsed(history.p50_elapsed_ms)}`;
      $("metric-tokens").textContent = formatCompactNumber(tokens.total_tokens || 0);
      $("metric-token-meta").textContent = `P ${formatCompactNumber(tokens.prompt_tokens)} · C ${formatCompactNumber(tokens.completion_tokens)} · R ${formatCompactNumber(tokens.reasoning_tokens)}`;
      $("metric-runtime").textContent = formatUptime(runtime.uptime_seconds);
      $("metric-runtime-meta").textContent = `本次进程 ${runtime.requests_total || 0} 请求 · 当前并发 ${runtime.inflight || 0}`;
      renderMetricTimeline($("metric-timeline"), history.timeline);

      $("stats-generated").textContent = `${metricWindowText(metricHours)} · ${generatedText} · 只聚合状态、耗时与 Token`;
      $("stats-kpi-requests").textContent = formatCompactNumber(history.requests || 0);
      $("stats-kpi-requests-meta").textContent = `历史保留 ${history.retained_total || 0} 条 · 进行中 ${statuses.streaming || 0}`;
      $("stats-kpi-success").textContent = formatPercent(history.success_rate, outcomes > 0);
      $("stats-kpi-success-meta").textContent = `成功 ${success} / 错误 ${errors}`;
      $("stats-kpi-errors").textContent = formatCompactNumber(errors + stopped);
      $("stats-kpi-errors-meta").textContent = `错误 ${errors} · 主动停止 ${stopped}`;
      $("stats-kpi-file-rate").textContent = formatPercent(history.file_delivery_rate, Number(history.requests) > 0);
      $("stats-kpi-file-meta").textContent = `拆分 ${history.file_delivery_requests || 0} · 降级 ${history.fallback_requests || 0}`;
      $("stats-timeline-meta").textContent = `${history.bucket_minutes || 0} 分钟 / 桶`;
      renderMetricTimeline($("stats-timeline"), history.timeline);

      const promptTokens = Number(tokens.prompt_tokens) || 0;
      const completionTokens = Number(tokens.completion_tokens) || 0;
      const reasoningTokens = Number(tokens.reasoning_tokens) || 0;
      const tokenTotal = promptTokens + completionTokens + reasoningTokens;
      $("stats-token-total").textContent = `总计 ${formatCompactNumber(tokenTotal)}`;
      $("stats-token-prompt").textContent = formatCompactNumber(promptTokens);
      $("stats-token-completion").textContent = formatCompactNumber(completionTokens);
      $("stats-token-reasoning").textContent = formatCompactNumber(reasoningTokens);
      const composition = $("stats-token-composition");
      composition.replaceChildren();
      [["prompt", promptTokens], ["completion", completionTokens], ["reasoning", reasoningTokens]].forEach(([name, value]) => {
        if (!value || !tokenTotal) return;
        const segment = document.createElement("span");
        segment.className = `token-segment ${name}`;
        segment.style.width = `${(value / tokenTotal) * 100}%`;
        segment.title = `${name}: ${value.toLocaleString()}`;
        composition.appendChild(segment);
      });
      $("stats-latency-avg").textContent = formatElapsed(history.avg_elapsed_ms);
      $("stats-latency-percentiles").textContent = `${formatElapsed(history.p50_elapsed_ms)} / ${formatElapsed(history.p95_elapsed_ms)}`;
      $("stats-fallbacks").textContent = `${history.fallback_requests || 0} 次`;
      renderBreakdown(
        $("stats-models"),
        history.models,
        "model",
        item => `${item.count} · ${formatCompactNumber(item.tokens)} tk · ${formatElapsed(item.avg_elapsed_ms)}`
      );
      const surfaces = (history.surfaces || []).map(item => ({ ...item, display: surfaceLabel(item.surface) }));
      renderBreakdown($("stats-surfaces"), surfaces, "display", item => `${item.count} 次`);

      $("stats-uptime").textContent = `已运行 ${formatUptime(runtime.uptime_seconds)}`;
      $("stats-runtime-requests").textContent = `${runtime.requests_total || 0} 次`;
      $("stats-runtime-recent").textContent = `${runtime.requests_5m || 0} 请求 · ${runtime.errors_5m || 0} 个 5xx`;
      $("stats-runtime-latency").textContent = `${formatElapsed(runtime.p50_duration_ms)} / ${formatElapsed(runtime.p95_duration_ms)}`;
      $("stats-runtime-errors").textContent = `${runtime.status_4xx || 0} / ${runtime.status_5xx || 0} · 408 ${runtime.request_timeouts || 0} · 413 ${runtime.request_too_large || 0}`;
      $("stats-runtime-inflight").textContent = `${runtime.inflight || 0}（当前账号 ${runtime.active_profile_inflight || 0}）`;
      $("stats-runtime-metric-paths").textContent = `${runtime.tracked_paths || 0}/${runtime.max_paths || 0} · 聚合 ${runtime.path_overflow_total || 0}`;
      const replayState = autoDelete.replay_active
        ? `重放中 · 待提交 ${autoDelete.replay_deferred || 0}`
        : autoDelete.replay_deferred
          ? `重放已暂停 · 待提交 ${autoDelete.replay_deferred}`
          : "重放空闲";
      $("stats-runtime-delete-queue").textContent = `${autoDelete.pending || 0}/${autoDelete.max_pending || 0} · 补偿 会话 ${autoDelete.journal_chat_pending || 0} / 文件 ${autoDelete.journal_file_pending || 0} · ${replayState} · 取消 ${autoDelete.cancelled_total || 0} · 回压 ${autoDelete.backpressure_total || 0}`;
      $("stats-runtime-captcha-worker").textContent = captchaWorker.enabled
        ? `${captchaWorker.active ? "求解中" : "空闲"} · 排队 ${captchaWorker.pending || 0}/${captchaWorker.max_pending || 0} · 回压 ${captchaWorker.backpressure_total || 0}`
        : "未启用（当前无需浏览器回退）";
      $("stats-runtime-http-handlers").textContent = `${httpHandlers.active || 0}/${httpHandlers.max_active || 0} · 峰值 ${httpHandlers.peak || 0} · 拒绝 ${httpHandlers.rejected_total ?? httpHandlers.wait_total ?? 0}`;
      $("stats-runtime-upload-slots").textContent = `附件 ${uploadSlots.file?.active || 0}/${uploadSlots.file?.max_active || 0} · HAR ${uploadSlots.har?.active || 0}/${uploadSlots.har?.max_active || 0} · 拒绝 ${uploadSlots.rejected_total || 0}`;
      $("stats-runtime-upstream-responses").textContent = `JSON拒绝 ${upstreamResponses.rejected_total || 0} · 流拒绝 ${upstreamResponses.stream_rejected_total || 0} · 未完整 ${upstreamResponses.stream_incomplete_total || 0} · 错误截断 ${upstreamResponses.error_truncated_total || 0} · 流输出 ${formatBytes(upstreamResponses.stream_output_max_bytes || 0)} / 原始 ${formatBytes(upstreamResponses.stream_wire_max_bytes || 0)}`;
      $("stats-runtime-upstream-readers").textContent = `${upstreamReaders.active || 0} 活跃 · 峰值 ${upstreamReaders.peak || 0} · 静默 ${upstreamReaders.heartbeats_total || 0} · 强关 ${upstreamReaders.forced_closes_total || 0}`;
      $("stats-runtime-sse-heartbeat").textContent = `${sseHeartbeat.active || 0} 活跃 · 峰值 ${sseHeartbeat.peak || 0} · 已发 ${sseHeartbeat.sent_total || 0} · 错误 ${sseHeartbeat.errors_total || 0}`;
      $("stats-runtime-context-cache").textContent = `${contextCache.items || 0}/${contextCache.max_items || 0} · ${formatBytes(contextCache.bytes || 0)} · 失败 ${contextCache.failure_states || 0} · 降级 ${contextCache.degraded_states || 0}`;
      renderStatsTable($("stats-paths"), runtime.top_paths, "path", "count");

      const levels = logs.levels || {};
      const kinds = logs.kinds || {};
      const logStore = logs.store || {};
      $("stats-log-total").textContent = `内存环 ${logs.total || 0}/${logs.capacity || 0} · 单条截断 ${logs.truncated_total || 0} · 磁盘 ${formatBytes(logStore.total_bytes || 0)}`;
      $("stats-log-info").textContent = levels.INFO || 0;
      $("stats-log-warning").textContent = levels.WARNING || 0;
      $("stats-log-error").textContent = (levels.ERROR || 0) + (levels.CRITICAL || 0);
      $("stats-log-events").textContent = kinds.event || 0;
      renderStatsTable($("stats-event-list"), logs.top_states, "state", "count");
    }

    async function refreshMetrics() {
      try {
        const response = await fetchWithTimeout(`/api/metrics?hours=${metricHours}`, { headers: apiHeaders() }, 8000);
        const data = await response.json();
        if (!response.ok || !data.ok) throw new Error(data.error?.message || `HTTP ${response.status}`);
        renderMetrics(data);
        return true;
      } catch (error) {
        if (currentPage === "stats") $("stats-generated").textContent = `统计加载失败：${error.message}`;
        return false;
      }
    }

    function setMetricAuto(on) {
      if (metricAutoTimer) {
        clearInterval(metricAutoTimer);
        metricAutoTimer = null;
      }
      if (on) metricAutoTimer = setInterval(() => {
        if (!document.hidden && (currentPage === "dashboard" || currentPage === "stats")) refreshMetrics();
      }, 10000);
    }

    document.querySelectorAll("[data-metric-hours]").forEach(button => {
      button.addEventListener("click", () => {
        metricHours = Number(button.dataset.metricHours) || 24;
        document.querySelectorAll("[data-metric-hours]").forEach(item => item.classList.toggle("active", Number(item.dataset.metricHours) === metricHours));
        refreshMetrics();
      });
    });
    $("btn-stats-export").addEventListener("click", () => {
      if (!metricLastData) {
        window.showToast("统计数据尚未加载", "error");
        return;
      }
      const exportedAt = new Date().toISOString();
      const payload = { ...metricLastData, schema: "glm2api.metrics.v1", exported_at: exportedAt };
      const stamp = exportedAt.replace(/[:.]/g, "-");
      downloadTextFile(
        `glm2api-metrics-${metricHours}h-${stamp}.json`,
        JSON.stringify(payload, null, 2),
        "application/json;charset=utf-8"
      );
      window.showToast("已导出脱敏聚合统计", "success");
    });

    // ---- 运行日志查看器 ----
    const logViewer = $("log-viewer");
    const logMeta = $("log-meta");
    const logLevelSel = $("log-level");
    const logKindSel = $("log-kind");
    const logStateInput = $("log-state");
    const logRidInput = $("log-rid");
    const logFilterInput = $("log-filter");
    let logAutoTimer = null;
    let logFilterTimer = null;
    let logLastText = "";
    let logLastSeq = 0;
    let logVisibleEntries = [];
    let logFetchController = null;
    const LOG_VISIBLE_LIMIT = 500;

    function formatLogClock(timestampMs) {
      const date = new Date(Number(timestampMs) || 0);
      if (isNaN(date.getTime())) return "--:--:--";
      const pad = value => String(value).padStart(2, "0");
      return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}.${String(date.getMilliseconds()).padStart(3, "0")}`;
    }

    function compactLogValue(value) {
      if (typeof value === "string") return value;
      try { return JSON.stringify(value); } catch (_) { return String(value); }
    }

    function displayLogMessage(entry) {
      const message = String(entry.message || entry.line || "");
      if (entry.kind === "event") {
        try {
          const payload = JSON.parse(message);
          delete payload.state;
          delete payload.rid;
          const fields = Object.entries(payload).map(([key, value]) => `${key}=${compactLogValue(value)}`);
          return fields.join("  ") || "事件已记录";
        } catch (_) {}
      }
      const body = entry.kind === "access" ? message.replace(/^\[[0-9a-f]{8}\]\s*/i, "") : message;
      const fullLine = String(entry.line || "");
      const traceAt = fullLine.indexOf("\n");
      return traceAt >= 0 ? `${body}${fullLine.slice(traceAt)}` : body;
    }

    function fallbackLogEntries(lines) {
      return (lines || []).map((line, index) => ({
        seq: index,
        timestamp_ms: 0,
        level: line.includes("[ERROR]") ? "ERROR" : line.includes("[WARNING]") ? "WARNING" : line.includes("[DEBUG]") ? "DEBUG" : "INFO",
        thread: "",
        kind: "system",
        state: "",
        rid: "",
        message: line,
        line
      }));
    }

    function createLogRow(entry) {
      const row = document.createElement("div");
      row.className = `log-row level-${String(entry.level || "INFO").toLowerCase()}`;
      row.title = String(entry.line || "");
      const time = document.createElement("span");
      time.className = "log-row-time";
      time.textContent = formatLogClock(entry.timestamp_ms);
      const level = document.createElement("span");
      level.className = "log-level-chip";
      level.textContent = String(entry.level || "INFO");
      const thread = document.createElement("span");
      thread.className = "log-row-thread";
      thread.textContent = String(entry.thread || "—");
      thread.title = thread.textContent;
      const main = document.createElement("div");
      main.className = "log-row-main";
      const heading = document.createElement("div");
      heading.className = "log-row-heading";
      const kind = document.createElement("span");
      kind.className = "log-kind";
      kind.textContent = String(entry.kind || "system");
      heading.appendChild(kind);
      if (entry.state) {
        const stateButton = document.createElement("button");
        stateButton.type = "button";
        stateButton.className = "log-state-btn";
        stateButton.textContent = entry.state;
        stateButton.title = "按此事件筛选";
        stateButton.addEventListener("click", () => {
          logStateInput.value = entry.state;
          fetchLogs();
        });
        heading.appendChild(stateButton);
      }
      if (entry.rid) {
        const ridButton = document.createElement("button");
        ridButton.type = "button";
        ridButton.className = "log-rid-btn";
        ridButton.textContent = `#${entry.rid}`;
        ridButton.title = "追踪此请求";
        ridButton.addEventListener("click", () => {
          logRidInput.value = entry.rid;
          fetchLogs();
        });
        heading.appendChild(ridButton);
      }
      const message = document.createElement("div");
      message.className = "log-row-message";
      message.textContent = displayLogMessage(entry);
      main.append(heading, message);
      row.append(time, level, thread, main);
      return row;
    }

    function renderLogs(entries, lines = [], append = false) {
      const nearBottom =
        logViewer.scrollHeight - logViewer.scrollTop - logViewer.clientHeight < 60 || !logLastText;
      let rows = entries && entries.length ? entries : fallbackLogEntries(lines);
      if (append) {
        const known = new Set(logVisibleEntries.map(entry => Number(entry.seq) || 0));
        rows = rows.filter(entry => !known.has(Number(entry.seq) || 0));
        if (!rows.length) return;
        logViewer.querySelector(".log-empty")?.remove();
        logVisibleEntries = logVisibleEntries.concat(rows).slice(-LOG_VISIBLE_LIMIT);
        const frag = document.createDocumentFragment();
        rows.forEach(entry => frag.appendChild(createLogRow(entry)));
        logViewer.appendChild(frag);
        while (logViewer.childElementCount > logVisibleEntries.length) logViewer.firstElementChild?.remove();
      } else {
        logVisibleEntries = rows.slice(-LOG_VISIBLE_LIMIT);
        logViewer.replaceChildren();
        if (!logVisibleEntries.length) {
          const empty = document.createElement("div");
          empty.className = "log-empty";
          empty.textContent = "没有符合当前筛选条件的日志。\n可清除事件名、RID 或关键字后重试。";
          logViewer.appendChild(empty);
        } else {
          const frag = document.createDocumentFragment();
          logVisibleEntries.forEach(entry => frag.appendChild(createLogRow(entry)));
          logViewer.appendChild(frag);
        }
      }
      if (nearBottom) logViewer.scrollTop = logViewer.scrollHeight;
      logLastText = logVisibleEntries.map(entry => entry.line || entry.message || "").join("\n");
    }

    async function fetchLogs(options = {}) {
      const incremental = Boolean(options.incremental && logLastSeq);
      if (incremental && logFetchController) return;
      if (!incremental && logFetchController) logFetchController.abort();
      const controller = new AbortController();
      logFetchController = controller;
      try {
        const params = new URLSearchParams({
          lines: "500",
          format: "structured",
          level: logLevelSel.value || "",
          kind: logKindSel.value || "",
          state: logStateInput.value.trim() || "",
          rid: logRidInput.value.trim() || "",
          text: logFilterInput.value.trim() || ""
        });
        if (incremental) params.set("after_seq", String(logLastSeq));
        const resp = await fetchWithTimeout("/api/logs?" + params.toString(), {
          headers: apiHeaders(),
          signal: controller.signal
        }, 8000);
        const data = await resp.json();
        if (!resp.ok || !data.ok) throw new Error((data.error && data.error.message) || "加载失败");
        if (controller !== logFetchController) return;
        const cursor = data.cursor || {};
        const resetRequired = Boolean(cursor.reset_required);
        renderLogs(data.entries || [], data.lines || [], incremental && !resetRequired);
        logLastSeq = Number(cursor.last_seq) || 0;
        const stats = data.stats || {};
        const levels = stats.levels || {};
        const kinds = stats.kinds || {};
        $("log-stat-matched").textContent = stats.matched ?? (data.lines || []).length;
        $("log-stat-warning").textContent = levels.WARNING || 0;
        $("log-stat-error").textContent = (levels.ERROR || 0) + (levels.CRITICAL || 0);
        $("log-stat-events").textContent = kinds.event || 0;
        const stateOptions = $("log-state-options");
        stateOptions.replaceChildren();
        (stats.top_states || []).forEach(item => {
          const option = document.createElement("option");
          option.value = item.state;
          option.label = `${item.count} 次`;
          stateOptions.appendChild(option);
        });
        const store = data.store || {};
        logMeta.textContent = `内存 ${stats.total ?? data.ring_count}/${stats.capacity ?? data.ring_capacity} · 显示 ${logVisibleEntries.length}/${stats.matched ?? 0} · 游标 ${logLastSeq || "—"} · 单条截断 ${stats.truncated_total || 0} · 磁盘 ${formatBytes(store.total_bytes ?? data.file_bytes) || "0B"}/${formatBytes(store.max_total_bytes || 0)} · ${store.segments || 0}/${store.max_segments || 0} 段 · ${data.level}`;
        $("btn-log-errors").classList.toggle("active", logLevelSel.value === "WARNING");
      } catch (e) {
        if (e.name !== "AbortError" && controller === logFetchController) {
          logMeta.textContent = "加载失败: " + e.message;
        }
      } finally {
        if (controller === logFetchController) logFetchController = null;
      }
    }

    function setLogAuto(on) {
      if (logAutoTimer) {
        clearInterval(logAutoTimer);
        logAutoTimer = null;
      }
      if (on) logAutoTimer = setInterval(() => {
        if (!document.hidden && currentPage === "logs") fetchLogs({ incremental: true });
      }, 2500);
      const btn = $("btn-log-autorefresh");
      btn.classList.toggle("active", !!on);
      btn.querySelector("span").textContent = on ? "自动:开" : "自动:关";
    }

    $("btn-log-refresh").addEventListener("click", () => fetchLogs());
    $("btn-log-autorefresh").addEventListener("click", () => setLogAuto(logAutoTimer === null));
    $("btn-log-errors").addEventListener("click", () => {
      logLevelSel.value = logLevelSel.value === "WARNING" ? "" : "WARNING";
      fetchLogs();
    });
    $("btn-log-wrap").addEventListener("click", () => {
      const wrapped = !logViewer.classList.contains("wrap");
      logViewer.classList.toggle("wrap", wrapped);
      $("btn-log-wrap").classList.toggle("active", wrapped);
      $("btn-log-wrap").querySelector("span").textContent = wrapped ? "折行:开" : "折行:关";
    });
    $("btn-log-copy").addEventListener("click", () => {
      navigator.clipboard.writeText(logLastText)
        .then(() => window.showToast("已复制当前日志", "success"))
        .catch(error => window.showToast(`复制失败: ${error.message}`, "error"));
    });
    logLevelSel.addEventListener("change", fetchLogs);
    logKindSel.addEventListener("change", fetchLogs);
    [logStateInput, logRidInput, logFilterInput].forEach(input => {
      input.addEventListener("input", () => {
        if (logFilterTimer) clearTimeout(logFilterTimer);
        logFilterTimer = setTimeout(fetchLogs, 300);
      });
      input.addEventListener("keydown", event => {
        if (event.key === "Enter") fetchLogs();
      });
    });

    // ---- 概览页最近日志 mini 视图 ----
    let dashLogTimer = null;
    async function refreshDashboardLog() {
      try {
        const resp = await fetchWithTimeout("/api/logs?lines=8", { headers: apiHeaders() }, 4000);
        const data = await resp.json();
        if (data.ok) {
          $("dash-log").textContent = (data.lines || []).join("\n") || "暂无日志";
        }
      } catch (e) { /* 概览日志 best-effort */ }
    }
    function setDashLogAuto(on) {
      if (dashLogTimer) { clearInterval(dashLogTimer); dashLogTimer = null; }
      if (on) dashLogTimer = setInterval(() => {
        if (!document.hidden && currentPage === "dashboard") refreshDashboardLog();
      }, 5000);
    }

    // Copy endpoint buttons（URL 由当前页面 origin 动态生成）
    function renderEndpointUrls() {
      const origin = window.location.origin;
      document.querySelectorAll("[data-endpoint]").forEach(el => {
        el.textContent = origin + el.textContent.replace(/^https?:\/\/[^\/]+/, "");
      });
      document.querySelectorAll(".btn-copy-chip[data-copy-path]").forEach(btn => {
        btn.dataset.copy = origin + btn.dataset.copyPath;
      });
      const curlEl = $("curl-sample-code");
      curlEl.textContent = `curl -X POST ${origin}/v1/chat/completions \\
  -H "Content-Type: application/json" \\
  -d '{
    "model": "glm-5.3",
    "messages": [{"role": "user", "content": "你好"}]
  }'`;
    }

    document.querySelectorAll(".btn-copy-chip").forEach(btn => {
      btn.addEventListener("click", () => {
        const text = btn.dataset.copy;
        if (!text) return;
        navigator.clipboard.writeText(text).then(() => {
          window.showToast("已复制协议端点 URL", "success");
        });
      });
    });

    // Copy cURL sample
    $("btn-copy-curl").addEventListener("click", () => {
      const code = $("curl-sample-code").innerText;
      navigator.clipboard.writeText(code).then(() => {
        window.showToast("已复制 cURL 示例代码", "success");
      });
    });

    // API Key session helpers
    function applyApiKey(key) {
      state.apiKey = key.trim();
      try {
        if (state.apiKey) {
          sessionStorage.setItem(API_KEY_SESSION_KEY, state.apiKey);
        } else {
          sessionStorage.removeItem(API_KEY_SESSION_KEY);
        }
      } catch (e) {}
      apiKeyInput.value = state.apiKey;
    }

    btnSetApiKey.addEventListener("click", async () => {
      const key = apiKeyInput.value.trim();
      if (!key) {
        window.showToast("请输入 API Key", "error");
        return;
      }
      btnSetApiKey.disabled = true;
      applyApiKey(key);
      try {
        await reloadAllData();
        window.showToast("API Key 已保存到当前标签页", "success");
      } catch (err) {
        window.showToast(`API Key 校验失败: ${err.message}`, "error");
      } finally {
        btnSetApiKey.disabled = false;
      }
    });

    btnClearApiKey.addEventListener("click", async () => {
      btnClearApiKey.disabled = true;
      applyApiKey("");
      try {
        await reloadAllData();
      } catch (err) {}
      btnClearApiKey.disabled = false;
    });

    // Server API Key config (persisted, DPAPI-encrypted)
    function renderSettingsStoreStatus(store) {
      if (!store) {
        settingsStoreStatus.className = "status-banner standby";
        settingsStoreStatus.textContent = "验证服务状态后显示本地设置存储信息";
        return;
      }
      if (store.persisted === false || store.error) {
        settingsStoreStatus.className = "status-banner error";
        const reason = store.error ? `: ${store.error}` : "";
        settingsStoreStatus.textContent = `最近一次默认设置写入失败${reason}；当前继续使用原设置`;
        return;
      }
      settingsStoreStatus.className = store.saved_at ? "status-banner success" : "status-banner standby";
      settingsStoreStatus.textContent = store.saved_at
        ? `默认设置已持久化 · ${store.saved_at}`
        : "尚未保存自定义默认设置，当前使用内置默认值";
    }

    function renderServerApiKeyConfig(data) {
      const enabled = Boolean(data ? (data.api_key_required !== undefined ? data.api_key_required : data.enabled) : false);
      const source = (data && (data.api_key_source || data.source)) || "store";
      const savedAt = (data && (data.api_key_saved_at || data.saved_at)) || "";
      const storeError = (data && (data.api_key_store_error || data.error)) || "";
      const cliConfigured = source === "cli";
      btnSaveApiKeyConfig.disabled = cliConfigured;
      btnClearApiKeyConfig.disabled = cliConfigured;
      if (cliConfigured) {
        apiKeyConfigStatus.className = "status-banner standby";
        apiKeyConfigStatus.textContent = "由启动参数 GLM2API_API_KEY / --api-key 配置，面板不可修改";
        return;
      }
      if (storeError) {
        apiKeyConfigStatus.className = "status-banner error";
        apiKeyConfigStatus.textContent = `本地加密存储异常: ${storeError}`;
        return;
      }
      if (enabled) {
        apiKeyConfigStatus.className = "status-banner success";
        apiKeyConfigStatus.textContent = savedAt ? `已启用 · 保存于 ${savedAt}` : "已启用";
      } else {
        apiKeyConfigStatus.className = "status-banner standby";
        apiKeyConfigStatus.textContent = "未启用 · 保存新密钥后启用访问保护";
      }
    }

    btnSaveApiKeyConfig.addEventListener("click", async () => {
      const newKey = apiKeyConfigInput.value.trim();
      if (!newKey) {
        window.showToast("请输入新密钥；如需清除请使用右侧按钮", "error");
        return;
      }
      const currentKey = apiKeyCurrentInput.value.trim();
      btnSaveApiKeyConfig.disabled = true;
      try {
        const res = await fetchWithTimeout("/api/settings/api-key", {
          method: "POST",
          headers: apiHeaders({ "Content-Type": "application/json" }),
          body: JSON.stringify({ api_key: newKey, current_key: currentKey })
        }, MANAGEMENT_FETCH_TIMEOUT_MS);
        const data = await res.json();
        if (!res.ok || !data.ok) throw new Error(data.error?.message || `HTTP ${res.status}`);
        apiKeyConfigInput.value = "";
        apiKeyCurrentInput.value = "";
        renderServerApiKeyConfig(data);
        applyApiKey(newKey);
        await reloadAllData();
        window.showToast(data.message || "服务端 API Key 已保存", "success");
      } catch (err) {
        apiKeyConfigStatus.className = "status-banner error";
        apiKeyConfigStatus.textContent = `保存失败: ${err.message}`;
        window.showToast(`保存服务端 API Key 失败: ${err.message}`, "error");
      } finally {
        btnSaveApiKeyConfig.disabled = state.apiKeySource === "cli";
      }
    });

    btnClearApiKeyConfig.addEventListener("click", async () => {
      let currentKey = apiKeyCurrentInput.value.trim();
      if (!currentKey) {
        currentKey = prompt("请输入当前服务密钥以确认清除") || "";
        if (!currentKey) return;
      }
      if (!confirm("确认清除服务端 API Key？清除后服务将不再需要密钥即可访问。")) return;
      btnClearApiKeyConfig.disabled = true;
      try {
        const res = await fetchWithTimeout("/api/settings/api-key", {
          method: "POST",
          headers: apiHeaders({ "Content-Type": "application/json" }),
          body: JSON.stringify({ api_key: "", current_key: currentKey })
        }, MANAGEMENT_FETCH_TIMEOUT_MS);
        const data = await res.json();
        if (!res.ok || !data.ok) throw new Error(data.error?.message || `HTTP ${res.status}`);
        apiKeyConfigInput.value = "";
        apiKeyCurrentInput.value = "";
        applyApiKey("");
        renderServerApiKeyConfig(data);
        await reloadAllData();
        window.showToast(data.message || "服务端 API Key 已清除", "success");
      } catch (err) {
        apiKeyConfigStatus.className = "status-banner error";
        apiKeyConfigStatus.textContent = `清除失败: ${err.message}`;
        window.showToast(`清除服务端 API Key 失败: ${err.message}`, "error");
      } finally {
        btnClearApiKeyConfig.disabled = state.apiKeySource === "cli";
      }
    });

    function populateModelSelect(models) {
      if (!Array.isArray(models) || !models.length) return;
      const curVal = modelSelect.value;
      modelSelect.innerHTML = "";
      models.forEach(m => {
        const opt = document.createElement("option");
        opt.value = m;
        opt.textContent = m;
        modelSelect.appendChild(opt);
      });
      if (curVal && models.includes(curVal)) {
        modelSelect.value = curVal;
      }
      modelPillText.textContent = `model: ${modelSelect.value}`;
      $("info-model").textContent = modelSelect.value;
      // 接口页模型列表
      const list = $("api-models-list");
      if (list) {
        list.innerHTML = "";
        models.forEach(m => {
          const chip = document.createElement("code");
          chip.style.cssText = "padding: 3px 9px; background: var(--bg-code); border: 1px solid var(--border-subtle); border-radius: 6px; color: var(--text-muted); font-size: 11px;";
          chip.textContent = m;
          list.appendChild(chip);
        });
      }
    }

    function profileRuntime(p, concurrency) {
      const rows = Array.isArray(concurrency?.profiles) ? concurrency.profiles : [];
      const row = rows.find(item => item && item.id === p.id) || {};
      const capacity = Math.max(1, Number(row.capacity ?? p.concurrency_limit ?? concurrency?.per_profile ?? 3) || 3);
      const inflight = Math.max(0, Number(row.inflight ?? p.inflight ?? 0) || 0);
      return {
        capacity,
        inflight,
        available: Math.max(0, Number(row.available ?? p.available_slots ?? capacity - inflight) || 0)
      };
    }

    function renderAccountPool(payload = {}) {
      const incomingProfiles = Array.isArray(payload.profiles) ? payload.profiles : (state.profiles || []);
      const profiles = [...incomingProfiles].sort((a, b) => {
        const aOrder = Number(a.routing_order) || Number.MAX_SAFE_INTEGER;
        const bOrder = Number(b.routing_order) || Number.MAX_SAFE_INTEGER;
        return aOrder - bOrder;
      });
      const concurrency = payload.concurrency || state.concurrency || {};
      state.profiles = profiles;
      state.concurrency = concurrency;
      if (profileSelect && profiles.length) {
        const selectedId = profiles.some(p => p.id === profileSelect.value)
          ? profileSelect.value
          : (payload.active_profile_id || state.activeProfileId || profiles[0].id);
        profileSelect.value = selectedId || "";
      }

      const count = Number(concurrency.profile_count ?? profiles.length) || 0;
      const maxProfiles = Number(payload.max_profiles ?? state.maxProfiles) || 0;
      if (maxProfiles > 0) state.maxProfiles = maxProfiles;
      const perProfile = Number(concurrency.per_profile) || 3;
      const capacity = Number(concurrency.capacity ?? count * perProfile) || 0;
      const inflight = Number(concurrency.inflight) || 0;
      const available = Number(concurrency.available ?? Math.max(0, capacity - inflight)) || 0;
      const activeInflight = Number(concurrency.active_profile_inflight) || 0;
      const activeProfile = profiles.find(p => p.active) || profiles.find(p => p.id === state.activeProfileId);
      const activeName = activeProfile?.label || "当前优先账号";

      $("account-pool-profile-count").textContent = maxProfiles ? `${count}/${maxProfiles}` : String(count);
      $("account-pool-profile-meta").textContent = count ? `${activeName} 为默认优先` : "先添加一个登录态";
      $("account-pool-capacity").textContent = String(capacity);
      $("account-pool-capacity-meta").textContent = `每账号 ${perProfile} 个槽位`;
      $("account-pool-inflight").textContent = String(inflight);
      $("account-pool-inflight-meta").textContent = `当前账号 ${activeInflight}`;
      $("account-pool-available").textContent = String(available);
      $("account-pool-available-meta").textContent = available ? "新请求可自动接入" : "所有账号暂时满载";

      if (!count) {
        accountRoutingPill.className = "account-routing-pill standby";
        accountRoutingPill.textContent = "未配置账号";
        accountRoutingStatus.className = "account-routing-status";
        accountRoutingStatus.innerHTML = `<span class="account-routing-dot"></span><span>添加账号后，服务会按容量自动分配新请求。</span>`;
      } else if (!available) {
        accountRoutingPill.className = "account-routing-pill busy";
        accountRoutingPill.textContent = "容量已满";
        accountRoutingStatus.className = "account-routing-status busy";
        accountRoutingStatus.innerHTML = `<span class="account-routing-dot"></span><span>所有账号均已占用；新请求会收到 429，已有请求不受影响。</span>`;
      } else {
        accountRoutingPill.className = "account-routing-pill ready";
        accountRoutingPill.textContent = "自动分配已启用";
        accountRoutingStatus.className = "account-routing-status ready";
        accountRoutingStatus.innerHTML = `<span class="account-routing-dot"></span><span>新请求优先进入 ${escapeHtml(activeName)}；满 ${perProfile} 个后自动顺延到下一个账号。</span>`;
      }

      const selectedProfile = profiles.find(p => p.id === profileSelect.value);
      const selectedRuntime = selectedProfile ? profileRuntime(selectedProfile, concurrency) : null;
      const selectedBusy = Boolean(selectedRuntime && selectedRuntime.inflight > 0);
      btnRemoveProfile.disabled = !selectedProfile || selectedBusy;
      btnRemoveProfile.title = selectedBusy
        ? `该账号仍有 ${selectedRuntime.inflight} 个生成请求，完成或停止后才能删除`
        : "删除选中的本地登录态";

      profilePoolList.replaceChildren();
      if (!profiles.length) {
        const empty = document.createElement("div");
        empty.className = "account-empty";
        empty.innerHTML = "暂无已保存账号<br><span>添加一个账号后，自动并发分配会立即可用</span>";
        profilePoolList.appendChild(empty);
        return;
      }
      profiles.forEach((p, index) => {
        const runtime = profileRuntime(p, concurrency);
        const selected = p.id === profileSelect.value;
        const full = runtime.inflight >= runtime.capacity;
        const duplicateMark = p.duplicate_user ? " · 重复登录态" : "";
        const source = p.source_display || "登录态";
        const user = p.user_name || p.user_id_fp || "未知用户";
        const fp = p.token_fp ? `token ${p.token_fp}` : "token 未就绪";
        const profileName = p.label || `账号 ${index + 1}`;
        const routingOrder = Math.max(1, Number(p.routing_order) || index + 1);
        const hasInflight = runtime.inflight > 0;
        const removeTitle = hasInflight ? `仍有 ${runtime.inflight} 个生成请求，暂不可删除` : "删除本地登录态";
        const badgeClass = full ? "busy" : (p.active ? "active" : "available");
        const badgeText = p.active ? (full ? "默认 · 已满" : "默认优先") : (full ? "已满" : "可接入");
        const card = document.createElement("article");
        card.className = `account-profile-card${p.active ? " active" : ""}${selected ? " selected" : ""}${full ? " full" : ""}`;
        card.dataset.profileKey = p.id;
        card.innerHTML = `
          <div class="account-profile-head">
            <div class="account-profile-ident">
              <div class="account-profile-name" title="${escapeHtml(profileName)}"><span class="account-route-index" title="第 ${routingOrder} 调度顺位">${String(routingOrder).padStart(2, "0")}</span><span class="account-profile-name-text">${escapeHtml(profileName)}</span></div>
              <div class="account-profile-user" title="${escapeHtml(user)}">${escapeHtml(user)}</div>
            </div>
            <span class="account-profile-badge ${badgeClass}">${badgeText}</span>
          </div>
          <div class="account-profile-meta"><span>${escapeHtml(source)}</span><span>${escapeHtml(fp)}${escapeHtml(duplicateMark)}</span></div>
          <div class="account-profile-slot">
            <div class="account-profile-slot-head"><span>生成槽位</span><strong>${runtime.inflight} / ${runtime.capacity}</strong></div>
            <div class="account-slot-track" role="progressbar" aria-label="账号并发占用" aria-valuemin="0" aria-valuemax="${runtime.capacity}" aria-valuenow="${runtime.inflight}"><div class="account-slot-fill${full ? " full" : ""}" style="width:${Math.min(100, runtime.inflight / runtime.capacity * 100)}%"></div></div>
          </div>
          <div class="account-profile-actions">
            <button class="tool-btn" type="button" data-profile-action="select" data-profile-key="${escapeHtml(p.id)}" ${selected ? "disabled" : ""}>${selected ? "已选中" : "选中"}</button>
            <button class="tool-btn" type="button" data-profile-action="remove" data-profile-key="${escapeHtml(p.id)}" ${hasInflight ? "disabled" : ""} title="${escapeHtml(removeTitle)}">删除</button>
          </div>`;
        profilePoolList.appendChild(card);
      });
    }

    function formatCaptchaMode(data) {
      const mode = String(data?.captcha_strategy || data?.captcha_mode || "");
      const solver = String(data?.captcha_solver || "");
      if (mode === "fresh" || mode === "browser_fresh") {
        if (solver === "happydom") return "自动 · happy-dom";
        if (solver === "auto") {
          if (data?.captcha_happydom_available === false) return "自动 · 浏览器回退";
          if (data?.captcha_browser_fallback_enabled === false) return "自动 · happy-dom";
          return "自动 · happy-dom / 浏览器回退";
        }
        if (solver === "browser") return "自动 · 浏览器回退";
        return "自动求解";
      }
      return "账号 / HAR 验证码";
    }

    function selectProfileCard(profileId) {
      const id = String(profileId || "");
      if (!id) return;
      profileSelect.value = id;
      renderAccountPool({ profiles: state.profiles, concurrency: state.concurrency, active_profile_id: state.activeProfileId });
    }

    function activeProfileForStatus(data = {}) {
      const activeId = data.active_profile_id || state.activeProfileId || "";
      return state.profiles.find(p => p.id === activeId)
        || state.profiles.find(p => p.active)
        || null;
    }

    function accountNameForStatus(data = {}) {
      const profile = activeProfileForStatus(data);
      return profile?.user_name || profile?.user_id_fp || data.user_id_fp || "";
    }

    function showProfileMutationResult(data, fallbackMessage) {
      const persisted = data?.persisted !== false;
      window.showToast(data?.message || fallbackMessage, persisted ? "success" : "standby");
    }

    // Profile & Auth Handlers
    async function fetchProfiles() {
      try {
        const res = await fetchWithTimeout("/api/auth/profiles", { headers: apiHeaders() });
        const data = await res.json();
        if (!data.ok) throw new Error(data.error?.message || "无法读取登录态");

        profileSelect.innerHTML = "";
        (data.profiles || []).forEach(p => {
          const opt = document.createElement("option");
          opt.value = p.id;
          opt.selected = p.active;
          const duplicateMark = p.duplicate_user ? " · 重复" : "";
          const source = p.source_display || "profile";
          const runtime = profileRuntime(p, data.concurrency);
          opt.textContent = `${p.active ? "● " : ""}${p.label} (${runtime.inflight}/${runtime.capacity} · ${p.user_name || p.user_id_fp || 'ID'} · ${source}${duplicateMark})`;
          profileSelect.appendChild(opt);
        });

        state.activeProfileId = data.active_profile_id || state.activeProfileId || "";
        renderAccountPool(data);
        if (state.lastStatus) {
          renderDashboard(state.lastStatus);
          $("info-user-name").textContent = accountNameForStatus(state.lastStatus) || "-";
        }

        const store = data.profile_store || {};
        const dupStats = data.duplicate_stats || {};
        const duplicateCount = Number(dupStats.duplicate_profile_count || 0);
        btnCompactProfiles.disabled = duplicateCount <= 0;
        const savedTime = store.saved_at ? ` · 保存于 ${store.saved_at}` : "";
        const dupText = duplicateCount > 0 ? ` · 发现 ${duplicateCount} 个同账号重复项` : "";
        const capacityText = data.max_profiles ? ` / 上限 ${data.max_profiles}` : "";
        if (store.persisted === false || store.error) {
          authStatusBanner.className = "status-banner error";
          const reason = store.error ? `：${store.error}` : "";
          authStatusBanner.textContent = `已加载 ${data.profiles?.length || 0} 个登录态，但本地加密存储异常${reason}。当前进程中的账号变更可能在重启后丢失${dupText}`;
        } else {
          authStatusBanner.className = "status-banner success";
          authStatusBanner.textContent = `已安全加载 ${data.profiles?.length || 0} 个登录态${capacityText} (DPAPI 加密)${savedTime}${dupText}`;
        }
        return true;
      } catch (err) {
        authStatusBanner.className = "status-banner error";
        authStatusBanner.textContent = `读取登录态失败: ${err.message}`;
        return false;
      }
    }

    async function fetchStatus() {
      try {
        const res = await fetchWithTimeout("/api/status", { headers: apiHeaders() });
        const data = await res.json();
        if (!data.ok) throw new Error("获取状态失败");
        state.lastStatus = data;
        state.apiKeyRequired = Boolean(data.api_key_required);
        state.apiKeySource = data.api_key_source || "store";
        state.playwrightAvailable = data.playwright_available !== false;
        btnBrowserLogin.disabled = state.browserLoginBusy || !state.playwrightAvailable;
        btnBrowserLogin.title = state.playwrightAvailable
          ? "打开官方页面获取登录态"
          : "未安装可选 Playwright 组件；请使用 Token/HAR，或先安装 requirements.txt";
        const browserLoginLabel = btnBrowserLogin.querySelector("span");
        if (browserLoginLabel) {
          browserLoginLabel.textContent = state.playwrightAvailable ? "拉起官方浏览器登录" : "浏览器登录组件未安装";
        }
        state.activeProfileId = data.active_profile_id || "";
        state.chatBusy = Boolean(data.chat_busy);
        state.concurrency = data.concurrency || state.concurrency;
        if (state.profiles.length) {
          renderAccountPool({
            profiles: state.profiles,
            concurrency: state.concurrency,
            active_profile_id: state.activeProfileId
          });
        }
        renderSettingsStoreStatus(data.settings_store);
        renderServerApiKeyConfig(data);
        renderDashboard(data);
        populateModelSelect(data.supported_models);
        if (state.apiKeyRequired && !data.api_key_valid) {
          // 受保护状态失效时丢弃页面内旧的账号展示，避免继续显示缓存身份。
          state.profiles = [];
          profileSelect.replaceChildren();
          renderAccountPool({ profiles: [], concurrency: {}, active_profile_id: "" });
          renderDashboard(data);
          updateStatusUI("需要 API Key", "standby");
          authStatusBanner.className = "status-banner error";
          authStatusBanner.textContent = "服务已启用 API Key，请在接口页输入密钥后保存到当前标签页。";
          apiKeyStatus.className = "status-banner error";
          apiKeyStatus.textContent = "已启用：请输入密钥";
          if (document.activeElement !== apiKeyInput) apiKeyInput.value = state.apiKey;
          return false;
        }
        apiKeyStatus.className = state.apiKeyRequired ? "status-banner success" : "status-banner standby";
        apiKeyStatus.textContent = state.apiKeyRequired ? "已启用：当前标签页密钥有效" : "未启用";
        if (document.activeElement !== apiKeyInput) apiKeyInput.value = state.apiKeyRequired ? state.apiKey : "";

        populateModelSelect(data.supported_models);

        const defaults = data.default_options || {};
        if (!state.statusDefaultsApplied) {
          modelSelect.value = defaults.model || data.model || "glm-5.3";
          webSearchToggle.checked = Boolean(defaults.auto_web_search);
          thinkingToggle.checked = defaults.enable_thinking !== false;
          // Rebuild the model-specific options before restoring the saved
          // value. The static HTML only contains max/high, so assigning a
          // saved low tier first makes the browser silently clear it.
          syncEffortOptions();
          const defaultEffort = defaults.reasoning_effort || "max";
          if ([...reasoningEffortSelect.options].some(opt => opt.value === defaultEffort)) {
            reasoningEffortSelect.value = defaultEffort;
          }
          rememberCurrentEffort();
          includeThinkingToggle.checked = defaults.include_thinking !== false;
          autoDeleteToggle.checked = defaults.delete_chat_after_completion !== false;
          if (data.upstream_timeout_sec) upstreamTimeoutInput.value = data.upstream_timeout_sec;
          if (data.upstream_retry_wait_sec !== undefined) upstreamRetryWaitInput.value = data.upstream_retry_wait_sec;
          if (data.upstream_retry_max_attempts !== undefined) upstreamRetryAttemptsInput.value = data.upstream_retry_max_attempts;
          if (data.history_max_records !== undefined) historyMaxRecordsInput.value = data.history_max_records;
          state.statusDefaultsApplied = true;
        }

        syncOptionStates();

        modelPillText.textContent = `model: ${modelSelect.value}`;
        $("info-model").textContent = modelSelect.value;
        $("info-captcha").textContent = formatCaptchaMode(data);
        $("info-user-name").textContent = accountNameForStatus(data) || "-";
        $("info-token-fp").textContent = data.token_fp || "-";
        $("info-device-fp").textContent = data.device_id_fp || "-";
        $("dash-timeout").textContent = (data.upstream_timeout_sec || "-") + "s";
        $("dash-retry").textContent = `wait ${data.upstream_retry_wait_sec ?? "-"}s × ${data.upstream_retry_max_attempts ?? "-"}`;
        const responseStore = data.response_store || {};
        $("info-response-store").textContent = responseStore.max_items
          ? `${responseStore.items || 0}/${responseStore.max_items} · ${formatBytes(responseStore.bytes || 0)}/${formatBytes(responseStore.max_bytes || 0)}`
          : "-";
        const historyStore = data.history_store || {};
        renderHistoryStoreStatus(historyStore);
        $("info-history-store").textContent = historyStore.max_records
          ? `${historyStore.records || 0}/${historyStore.max_records} · ${formatBytes(historyStore.detail_bytes || 0)}/${formatBytes(historyStore.max_detail_bytes || 0)}`
          : "-";
        const logStore = data.log_store || {};
        $("info-log-store").textContent = logStore.max_total_bytes
          ? `${formatBytes(logStore.total_bytes || 0)}/${formatBytes(logStore.max_total_bytes)} · ${logStore.segments || 0}/${logStore.max_segments || 0} 段`
          : "-";
        const autoDelete = data.auto_delete || {};
        $("info-auto-delete").textContent = autoDelete.max_pending
          ? `${autoDelete.pending || 0}/${autoDelete.max_pending} · 补偿 会话 ${autoDelete.journal_chat_pending || 0} / 文件 ${autoDelete.journal_file_pending || 0} · ${autoDelete.replay_active ? `重放中(${autoDelete.replay_deferred || 0})` : "重放空闲"}`
          : "-";
        const captchaWorker = data.captcha_worker || {};
        $("info-captcha-worker").textContent = captchaWorker.enabled
          ? `${captchaWorker.active ? "求解中" : "空闲"} · ${captchaWorker.pending || 0}/${captchaWorker.max_pending || 0} · 回压 ${captchaWorker.backpressure_total || 0}`
          : "未启用（当前无需浏览器回退）";
        const httpHandlers = data.http_handlers || {};
        $("info-http-handlers").textContent = httpHandlers.max_active
          ? `${httpHandlers.active || 0}/${httpHandlers.max_active} · 峰值 ${httpHandlers.peak || 0} · 拒绝 ${httpHandlers.rejected_total ?? httpHandlers.wait_total ?? 0}`
          : "-";
        const uploadSlots = data.upload_slots || {};
        $("info-upload-slots").textContent = uploadSlots.max_active
          ? `附件 ${uploadSlots.file?.active || 0}/${uploadSlots.file?.max_active || 0} · HAR ${uploadSlots.har?.active || 0}/${uploadSlots.har?.max_active || 0} · 拒绝 ${uploadSlots.rejected_total || 0}`
          : "-";
        const upstreamResponses = data.upstream_responses || {};
        $("info-upstream-responses").textContent = upstreamResponses.json_max_bytes
          ? `JSON拒绝 ${upstreamResponses.rejected_total || 0} · 流拒绝 ${upstreamResponses.stream_rejected_total || 0} · 未完整 ${upstreamResponses.stream_incomplete_total || 0} · 流输出 ${formatBytes(upstreamResponses.stream_output_max_bytes || 0)}`
          : "-";
        const upstreamReaders = data.upstream_readers || {};
        $("info-upstream-readers").textContent = upstreamReaders.queue_size
          ? `${upstreamReaders.active || 0} 活跃 · 峰值 ${upstreamReaders.peak || 0} · 静默 ${upstreamReaders.heartbeats_total || 0}`
          : "-";
        const sseHeartbeat = data.sse_heartbeat || {};
        $("info-sse-heartbeat").textContent = sseHeartbeat.interval_seconds
          ? `${sseHeartbeat.active || 0} 活跃 · ${sseHeartbeat.sent_total || 0} 次 · ${sseHeartbeat.interval_seconds}s`
          : "-";
        const contextCache = data.context_cache || {};
        $("info-context-cache").textContent = contextCache.max_items
          ? `${contextCache.items || 0}/${contextCache.max_items} · ${formatBytes(contextCache.bytes || 0)} · 降级 ${contextCache.degraded_states || 0}`
          : "-";

        if (data.limits?.chat_file_upload_bytes) {
          state.maxChatFileUploadBytes = Number(data.limits.chat_file_upload_bytes);
        }
        if (data.limits?.zai_completion_files) {
          state.maxCompletionFiles = Number(data.limits.zai_completion_files);
        }

        if (data.auth_ready) {
          if (state.activeRequest) {
            // A local generation is in progress; the 30s polling must not
            // overwrite its progress/upload status.
          } else if (state.chatBusy && Number(data.concurrency?.available || 0) <= 0) {
            updateStatusUI("当前账号有进行中的生成请求（含其它入口）", "standby");
          } else if (state.chatBusy) {
            updateStatusUI("当前账号生成中，后续请求将自动分配到其它账号", "connected");
          } else {
            updateStatusUI("已连接", "connected");
          }
        } else {
          updateStatusUI("待配置登录态", "standby");
          authStatusBanner.className = "status-banner error";
          authStatusBanner.textContent = "尚未检测到有效登录态，请到账号页添加（Token / 浏览器 / HAR）。";
        }
        return true;
      } catch (err) {
        updateStatusUI("连接失败", "error");
        window.showToast(`无法连接到本地服务端: ${err.message}`, "error");
        return false;
      }
    }

    // 概览磁贴渲染
    function renderDashboard(data) {
      if (!data) return;
      const serviceEl = $("dash-service");
      serviceEl.textContent = "运行中";
      serviceEl.className = "tile-value ok";

      const authEl = $("dash-auth");
      if (data.auth_ready) {
        authEl.textContent = "登录态就绪";
        authEl.className = "tile-hint";
        authEl.style.color = "var(--emerald-core)";
      } else {
        authEl.textContent = "未配置登录态";
        authEl.className = "tile-hint";
        authEl.style.color = "var(--rose-core)";
      }

      const activeProfile = activeProfileForStatus(data);
      const accountName = accountNameForStatus(data);
      $("dash-account").textContent = accountName || "-";
      $("dash-account").className = "tile-value" + (activeProfile?.user_name ? "" : " warn");
      $("dash-model").textContent = modelSelect.value || data.model || "-";
      $("dash-models-count").textContent = `${(data.supported_models || []).length} 个可用模型`;
      $("dash-captcha").textContent = formatCaptchaMode(data);
      const settingsModelValue = $("settings-model-value");
      if (settingsModelValue) settingsModelValue.textContent = modelSelect.value || data.model || "-";

      const protEl = $("dash-protection");
      if (data.api_key_required) {
        protEl.textContent = data.api_key_valid ? "API Key 已启用 · 本页密钥有效" : "API Key 已启用 · 待输入密钥";
        protEl.style.color = data.api_key_valid ? "var(--emerald-core)" : "var(--amber-core)";
      } else {
        protEl.textContent = "API Key 未启用";
        protEl.style.color = "var(--text-dim)";
      }

      // 左下角账号迷你卡
      const name = accountName;
      $("nav-account-name").textContent = name || "未登录";
      $("nav-account-avatar").textContent = name ? name.slice(0, 1).toUpperCase() : "?";
      $("nav-account-hint").textContent = data.auth_ready
        ? (activeProfile?.label || "登录态就绪")
        : "点击添加账号";
    }

    async function reloadAllData() {
      const ok = await fetchStatus();
      return ok ? fetchProfiles() : false;
    }

    btnRefreshProfiles.addEventListener("click", async () => {
      const ok = await reloadAllData();
      window.showToast(ok ? "已刷新登录态与状态" : "刷新失败，请检查本地服务", ok ? "success" : "error");
    });

    profileSelect.addEventListener("change", () => {
      renderAccountPool({ profiles: state.profiles, concurrency: state.concurrency, active_profile_id: state.activeProfileId });
    });

    profilePoolList.addEventListener("click", (event) => {
      const target = event.target.closest("[data-profile-action]");
      const card = event.target.closest("[data-profile-key]");
      const profileId = target?.dataset.profileKey || card?.dataset.profileKey || "";
      if (!profileId) return;
      selectProfileCard(profileId);
      if (!target) return;
      if (target.dataset.profileAction === "remove") btnRemoveProfile.click();
    });

    // Switch profile
    async function switchSelectedProfile() {
      const pId = profileSelect.value;
      if (!pId) return;
      btnSwitchProfile.disabled = true;
      authStatusBanner.textContent = "正在切换登录态...";
      try {
        const res = await fetchWithTimeout("/api/auth/switch", {
          method: "POST",
          headers: apiHeaders({ "Content-Type": "application/json" }),
          body: JSON.stringify({ profile_id: pId })
        }, MANAGEMENT_FETCH_TIMEOUT_MS);
        const data = await res.json();
        if (!res.ok || !data.ok) throw new Error(data.error?.message || `HTTP ${res.status}`);

        state.currentChatId = "";
        updateSessionUI();
        await reloadAllData();
        applyProfileSessionFilter();
        showProfileMutationResult(data, `已切换账号: ${data.profile.label}，本地会话已按账号隔离`);
      } catch (err) {
        window.showToast(`切换失败: ${err.message}`, "error");
      } finally {
        btnSwitchProfile.disabled = false;
      }
    }
    btnSwitchProfile.addEventListener("click", switchSelectedProfile);

    // Remove profile
    async function removeSelectedProfile() {
      const pId = profileSelect.value;
      if (!pId) return;
      const selectedProfile = state.profiles.find(p => p.id === pId);
      const runtime = selectedProfile ? profileRuntime(selectedProfile, state.concurrency || {}) : null;
      if (runtime && runtime.inflight > 0) {
        window.showToast(`该账号仍有 ${runtime.inflight} 个生成请求，请先完成或停止`, "error");
        return;
      }
      const optText = profileSelect.selectedOptions[0]?.textContent || pId;
      if (!confirm(`确认从本地安全区删除账号？\n${optText}`)) return;

      btnRemoveProfile.disabled = true;
      try {
        const res = await fetchWithTimeout("/api/auth/remove", {
          method: "POST",
          headers: apiHeaders({ "Content-Type": "application/json" }),
          body: JSON.stringify({ profile_id: pId })
        }, MANAGEMENT_FETCH_TIMEOUT_MS);
        const data = await res.json();
        if (!res.ok || !data.ok) throw new Error(data.error?.message || `HTTP ${res.status}`);

        state.currentChatId = "";
        updateSessionUI();
        await reloadAllData();
        applyProfileSessionFilter();
        showProfileMutationResult(data, "登录态已从本地加密区删除");
      } catch (err) {
        window.showToast(`删除失败: ${err.message}`, "error");
      } finally {
        renderAccountPool({ profiles: state.profiles, concurrency: state.concurrency, active_profile_id: state.activeProfileId });
      }
    }
    btnRemoveProfile.addEventListener("click", removeSelectedProfile);

    btnCompactProfiles.addEventListener("click", async () => {
      if (btnCompactProfiles.disabled) return;
      if (!confirm("确认清理同一账号的重复登录态？\n会保留当前启用项；正在生成的账号会跳过，任务结束后可再次清理。")) return;

      btnCompactProfiles.disabled = true;
      try {
        const res = await fetchWithTimeout("/api/auth/compact", {
          method: "POST",
          headers: apiHeaders({ "Content-Type": "application/json" }),
          body: "{}"
        }, MANAGEMENT_FETCH_TIMEOUT_MS);
        const data = await res.json();
        if (!res.ok || !data.ok) throw new Error(data.error?.message || `HTTP ${res.status}`);

        state.currentChatId = "";
        updateSessionUI();
        await reloadAllData();
        showProfileMutationResult(data, "已清理重复登录态");
      } catch (err) {
        window.showToast(`清理失败: ${err.message}`, "error");
      } finally {
        await fetchProfiles();
      }
    });

    // Browser login trigger
    btnBrowserLogin.addEventListener("click", async () => {
      if (!state.playwrightAvailable) {
        window.showToast("浏览器登录组件未安装，请使用 Token/HAR 或安装 requirements.txt", "error");
        return;
      }
      const label = profileNameInput.value.trim() || "browser login";
      state.browserLoginBusy = true;
      btnBrowserLogin.disabled = true;
      authStatusBanner.className = "status-banner standby";
      authStatusBanner.textContent = "正在拉起官方 Chrome 窗口...";
      window.showToast("已打开浏览器登录窗口，请在官方页面完成登录");
      const loginStartedAt = Date.now();
      let loginPollBusy = false;
      let loginPolling = true;
      const loginPollTimer = setInterval(async () => {
        if (!loginPolling || loginPollBusy) return;
        loginPollBusy = true;
        try {
          const res = await fetchWithTimeout(
            "/api/auth/browser-login/status",
            { headers: apiHeaders() },
            5000
          );
          const data = await res.json();
          if (loginPolling && data && data.stage) {
            const elapsed = Math.round((Date.now() - loginStartedAt) / 1000);
            const extra = data.error ? ` (${data.error})` : "";
            authStatusBanner.className = "status-banner standby";
            authStatusBanner.textContent = `浏览器登录中: ${data.stage} · 已等待 ${elapsed}s${extra}`;
          }
        } catch (e) { /* status polling is best-effort */ }
        finally { loginPollBusy = false; }
      }, 1500);

      try {
        const res = await fetchWithTimeout("/api/auth/browser-login", {
          method: "POST",
          headers: apiHeaders({ "Content-Type": "application/json" }),
          body: JSON.stringify({ label, timeout_sec: 300 })
        }, BROWSER_LOGIN_FETCH_TIMEOUT_MS);
        const data = await res.json();
        if (!res.ok || !data.ok) throw new Error(data.error?.message || `HTTP ${res.status}`);

        profileNameInput.value = "";
        state.currentChatId = "";
        updateSessionUI();
        await reloadAllData();
        applyProfileSessionFilter();
        showProfileMutationResult(data, `浏览器登录成功并已加密保存: ${data.profile.label} · 验证码将由本地求解器自动处理`);
      } catch (err) {
        authStatusBanner.className = "status-banner error";
        authStatusBanner.textContent = `浏览器登录失败: ${err.message}`;
        window.showToast(`浏览器登录异常: ${err.message}`, "error");
      } finally {
        loginPolling = false;
        clearInterval(loginPollTimer);
        state.browserLoginBusy = false;
        btnBrowserLogin.disabled = !state.playwrightAvailable;
      }
    });

    // Upload HAR
    btnUploadHar.addEventListener("click", async () => {
      const file = harFileInput.files?.[0];
      if (!file) {
        window.showToast("请先选择 .har 文件", "error");
        return;
      }
      const label = profileNameInput.value.trim() || file.name;
      btnUploadHar.disabled = true;
      authStatusBanner.textContent = `正在上传并解析 ${file.name}...`;

      try {
        const params = new URLSearchParams({ label, source: file.name });
        const res = await fetchWithTimeout(`/api/auth/har?${params.toString()}`, {
          method: "POST",
          headers: apiHeaders({ "Content-Type": "application/har+json" }),
          body: file
        }, HAR_UPLOAD_FETCH_TIMEOUT_MS);
        const data = await res.json();
        if (!res.ok || !data.ok) throw new Error(data.error?.message || `HTTP ${res.status}`);

        harFileInput.value = "";
        profileNameInput.value = "";
        state.currentChatId = "";
        updateSessionUI();
        await reloadAllData();
        applyProfileSessionFilter();
        showProfileMutationResult(data, `HAR 解析成功并保存登录态: ${data.profile.label}`);
      } catch (err) {
        authStatusBanner.className = "status-banner error";
        authStatusBanner.textContent = `HAR 上传失败: ${err.message}`;
        window.showToast(`HAR 解析失败: ${err.message}`, "error");
      } finally {
        btnUploadHar.disabled = false;
      }
    });

    // Token login (bare JWT, no HAR needed)
    btnTokenLogin.addEventListener("click", async () => {
      const token = tokenInput.value.trim();
      if (!token) {
        window.showToast("请先粘贴 chat.z.ai 会话 token", "error");
        return;
      }
      const label = profileNameInput.value.trim();
      btnTokenLogin.disabled = true;
      authStatusBanner.textContent = "正在校验 token...";
      try {
        const res = await fetchWithTimeout("/api/auth/token", {
          method: "POST",
          headers: apiHeaders({ "Content-Type": "application/json" }),
          body: JSON.stringify({ token, label })
        }, MANAGEMENT_FETCH_TIMEOUT_MS);
        const data = await res.json();
        if (!res.ok || !data.ok) throw new Error(data.error?.message || `HTTP ${res.status}`);

        tokenInput.value = "";
        profileNameInput.value = "";
        state.currentChatId = "";
        updateSessionUI();
        await reloadAllData();
        applyProfileSessionFilter();
        showProfileMutationResult(data, `token 登录成功并切换账号: ${data.profile.label}`);
      } catch (err) {
        authStatusBanner.className = "status-banner error";
        authStatusBanner.textContent = `token 登录失败: ${err.message}`;
        window.showToast(`token 登录失败: ${err.message}`, "error");
      } finally {
        btnTokenLogin.disabled = false;
      }
    });

    // Initial Bootstrap
    loadLocalSessions();
    renderSessionsList();
    updateSessionUI();
    renderEndpointUrls();
    reloadAllData();
    navigateTo("dashboard");

    document.addEventListener("visibilitychange", () => {
      if (document.hidden) return;
      if (currentPage === "history") {
        loadRecordList(true, { silent: true });
        scheduleRecordPoll();
      } else if (currentPage === "logs") {
        fetchLogs();
      } else if (currentPage === "dashboard") {
        refreshDashboardLog();
        refreshMetrics();
      } else if (currentPage === "stats") {
        refreshMetrics();
      }
    });

    // 轻量状态自动刷新：仅页面可见时执行，且后台轮询不重入。
    let statusAutoRefreshRunning = false;
    setInterval(async () => {
      if (document.hidden || statusAutoRefreshRunning) return;
      statusAutoRefreshRunning = true;
      try {
        await reloadAllData();
      } catch (e) {
        // fetchStatus/fetchProfiles 已将故障反映到 UI；定时器保持运行。
      } finally {
        statusAutoRefreshRunning = false;
      }
    }, 30000);
