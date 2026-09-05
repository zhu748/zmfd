    // 历史页：请求镜像记录（ds2api 同款：思维链 / 流式输出 / 发给上游的提示词）
    // ==========================================================================
    const recordState = {
      records: [],
      page: 0,
      loading: false,
      exhausted: false,
      currentId: "",
      current: null,
      view: "overview", // chat | overview | detail
      activeProfileKey: "",
      text: "",
      status: "",
      pollTimer: 0,
      pollCount: 0
    };

    function renderHistoryStoreStatus(store) {
      const banner = $("history-store-status");
      const pendingWrites = Number(store?.pending_writes || 0);
      const pendingDeletes = Number(store?.pending_deletes || 0);
      if (store?.persisted !== false && !store?.error && !pendingWrites && !pendingDeletes) {
        banner.style.display = "none";
        banner.textContent = "";
        return;
      }
      const pending = [];
      if (pendingWrites) pending.push(`${pendingWrites} 条待写入`);
      if (pendingDeletes) pending.push(`${pendingDeletes} 条待删除`);
      const details = [store?.error || "历史存储尚未完全持久化", pending.join(" · ")].filter(Boolean).join("；");
      banner.className = "status-banner error";
      banner.style.display = "block";
      banner.textContent = `${details}。服务会在下一次历史变更时重试。`;
    }

    function showHistoryMutationResult(data, fallbackMessage) {
      window.showToast?.(data?.message || fallbackMessage, data?.persisted === false ? "standby" : "success");
    }

    const SURFACE_LABELS = {
      "openai_chat": "OpenAI Chat",
      "openai_responses": "OpenAI Responses",
      "anthropic_messages": "Anthropic Messages",
      "panel_chat": "控制台对话",
      "cli_direct": "CLI 直连",
      "unknown": "未标记入口"
    };
    function surfaceLabel(surface) {
      return SURFACE_LABELS[String(surface || "")] || (String(surface || "") || "—");
    }

    function statusChipInfo(status) {
      switch (String(status || "")) {
        case "success": return { label: "成功", cls: "success" };
        case "error": return { label: "失败", cls: "error" };
        case "stopped": return { label: "已停止", cls: "stopped" };
        default: return { label: "进行中", cls: "streaming" };
      }
    }

    function formatHistoryTime(unixSec) {
      const n = Number(unixSec);
      if (!n || n <= 0) return "";
      const d = new Date(n * 1000);
      if (isNaN(d.getTime())) return "";
      const now = Date.now();
      const diffMin = Math.round((now - d.getTime()) / 60000);
      if (diffMin >= 0 && diffMin < 1) return "刚刚";
      if (diffMin >= 1 && diffMin < 60) return `${diffMin} 分钟前`;
      const diffHr = Math.round(diffMin / 60);
      if (diffHr >= 1 && diffHr < 24) return `${diffHr} 小时前`;
      const pad = (v) => String(v).padStart(2, "0");
      return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
    }

    function formatRecordTime(ms) {
      const n = Number(ms);
      if (!n || n <= 0) return "";
      return formatHistoryTime(Math.floor(n / 1000));
    }

    function formatElapsed(ms) {
      const n = Number(ms);
      if (!n || n <= 0) return "—";
      if (n < 1000) return `${n}ms`;
      return `${(n / 1000).toFixed(n < 10000 ? 2 : 1)}s`;
    }

    function formatBytes(n) {
      const v = Number(n) || 0;
      if (v <= 0) return "";
      if (v < 1024) return `${v}B`;
      if (v < 1024 * 1024) return `${(v / 1024).toFixed(1)}KB`;
      return `${(v / 1024 / 1024).toFixed(1)}MB`;
    }

    function formatCompactNumber(value) {
      const n = Number(value) || 0;
      if (Math.abs(n) < 1000) return n.toLocaleString();
      if (Math.abs(n) < 1000000) return `${(n / 1000).toFixed(n < 10000 ? 1 : 0)}K`;
      if (Math.abs(n) < 1000000000) return `${(n / 1000000).toFixed(n < 10000000 ? 1 : 0)}M`;
      return `${(n / 1000000000).toFixed(1)}B`;
    }

    function formatPercent(value, hasSamples = true) {
      if (!hasSamples) return "—";
      return `${(Number(value || 0) * 100).toFixed(1)}%`;
    }

    function formatUptime(seconds) {
      let remaining = Math.max(0, Math.floor(Number(seconds) || 0));
      const days = Math.floor(remaining / 86400);
      remaining %= 86400;
      const hours = Math.floor(remaining / 3600);
      const minutes = Math.floor((remaining % 3600) / 60);
      if (days) return `${days}d ${hours}h`;
      if (hours) return `${hours}h ${minutes}m`;
      return `${minutes}m`;
    }

    function downloadTextFile(filename, text, mime = "text/plain;charset=utf-8") {
      const blob = new Blob([text], { type: mime });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      URL.revokeObjectURL(url);
    }

    async function copyTextToClipboard(text) {
      try {
        if (navigator.clipboard?.writeText) {
          await navigator.clipboard.writeText(text);
          return true;
        }
      } catch (e) { /* fallthrough */ }
      try {
        const ta = document.createElement("textarea");
        ta.value = text;
        ta.setAttribute("readonly", "");
        ta.style.position = "fixed";
        ta.style.top = "-9999px";
        document.body.appendChild(ta);
        ta.select();
        const ok = document.execCommand("copy");
        document.body.removeChild(ta);
        return ok;
      } catch (e) {
        return false;
      }
    }

    // 长文本折叠：超过阈值截断，点击展开/收起（ds2api ExpandableText 同款）。
    function buildExpandable(text, threshold = 700) {
      const wrap = document.createElement("div");
      const full = String(text || "");
      const needFold = full.length > threshold;
      let expanded = false;
      const body = document.createElement("div");
      body.style.whiteSpace = "pre-wrap";
      body.style.wordBreak = "break-word";
      const renderBody = () => {
        body.textContent = needFold && !expanded ? full.slice(0, threshold) + "…" : full;
      };
      renderBody();
      wrap.appendChild(body);
      if (needFold) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "exp-btn";
        const renderBtn = () => { btn.textContent = expanded ? "收起" : "展开全部"; };
        renderBtn();
        btn.addEventListener("click", () => { expanded = !expanded; renderBody(); renderBtn(); });
        wrap.appendChild(btn);
      }
      return wrap;
    }

    function ensureHistoryLoaded() {
      const key = state.activeProfileId || "";
      if (key !== recordState.activeProfileKey) {
        recordState.activeProfileKey = key;
        recordState.currentId = "";
        recordState.current = null;
      }
      // 请求镜像是本地文件，每次进入页面都重新拉取，保证刚发完的请求立即可见。
      loadRecordList(true);
    }

    function stopRecordPoll() {
      if (recordState.pollTimer) {
        clearTimeout(recordState.pollTimer);
        recordState.pollTimer = 0;
      }
      recordState.pollCount = 0;
    }

    // streaming 记录自动刷新：后端与详情页同频，每 750ms 节流落盘，
    // 这里每 750ms 重取一次即可接近实时看到思维链/回复（最多 3 分钟）。
    function scheduleRecordPoll() {
      if (recordState.pollTimer) {
        clearTimeout(recordState.pollTimer);
        recordState.pollTimer = 0;
      }
      const r = recordState.current;
      if (currentPage !== "history" || document.hidden || !r || String(r.status) !== "streaming") return;
      recordState.pollTimer = setTimeout(async () => {
        recordState.pollTimer = 0;
        if (currentPage !== "history" || document.hidden) return;
        if (recordState.pollCount >= 240) return;
        recordState.pollCount += 1;
        const id = recordState.currentId;
        try {
          const resp = await fetchWithTimeout(
            `/api/history/record?id=${encodeURIComponent(id)}`,
            { headers: apiHeaders() },
            MANAGEMENT_FETCH_TIMEOUT_MS
          );
          const data = await resp.json().catch(() => ({}));
          if (data.ok && data.record && recordState.currentId === id) {
            recordState.current = data.record;
            renderRecordDetail();
            const idx = recordState.records.findIndex((x) => x.id === id);
            if (idx >= 0) {
              recordState.records[idx] = { ...recordState.records[idx], ...buildRecordSummaryPatch(data.record) };
              renderRecordList();
            }
          }
        } catch (e) { /* 下轮再试 */ }
        scheduleRecordPoll();
      }, 750);
    }

    // 列表常驻自动刷新（ds2api LIST_REFRESH_MS 同款）：停留在第一页时每 1.5s
    // 静默重拉，新请求自动出现、进行中记录的状态实时变化；翻到更早页时暂停。
    let recordListTimer = 0;
    function startRecordListAuto() {
      if (recordListTimer) return;
      recordListTimer = setInterval(() => {
        if (document.hidden || currentPage !== "history" || recordState.loading || recordState.page !== 1) return;
        loadRecordList(true, { silent: true });
      }, 1500);
    }
    function stopRecordListAuto() {
      if (recordListTimer) {
        clearInterval(recordListTimer);
        recordListTimer = 0;
      }
    }

    function buildRecordSummaryPatch(full) {
      return {
        status: full.status,
        preview: String(full.content || full.reasoning || full.error || full.user_input || "").slice(0, 160),
        elapsed_ms: full.elapsed_ms || 0,
        status_code: full.status_code || 0,
        error: full.error || ""
      };
    }

    function recordToMarkdown(r) {
      const chip = statusChipInfo(r.status);
      const pad = (v) => String(v).padStart(2, "0");
      const d = new Date(Number(r.created_at) || 0);
      const timeStr = d.getTime() ? `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}` : "—";
      const usage = r.usage || {};
      const lines = [];
      lines.push(`# ${r.title || "请求记录"}`);
      lines.push("");
      lines.push(`- 状态: ${chip.label} · ${r.model || "—"} · ${surfaceLabel(r.surface)} · ${r.stream ? "流式" : "非流式"}`);
      lines.push(`- 时间: ${timeStr} · 耗时 ${formatElapsed(r.elapsed_ms)}`);
      if (r.account) lines.push(`- 账号: ${r.account}`);
      if (r.caller) lines.push(`- 调用方: ${callerLabel(r.caller)}`);
      if (r.chat_id) lines.push(`- 上游会话: ${r.chat_id}`);
      if (usage.total_tokens) lines.push(`- Tokens(估): P ${usage.prompt_tokens || 0} · C ${usage.completion_tokens || 0} · R ${usage.reasoning_tokens || 0}`);
      const delivery = recordDeliveryInfo(r);
      lines.push(`- 上游实发: ${delivery.mode === "file" ? (delivery.preloadWaves ? `多波次历史拆分（${delivery.preloadWaves} 个预载波次 + 最终波次）` : "历史拆分（附件 + 输入框）") : (delivery.requested && delivery.fallback ? "历史拆分失败后降级直传" : "输入框直传")}`);
      if (delivery.preloadWaves) lines.push(`- 上下文预载: ${delivery.preloadFiles} 个附件 / ${delivery.preloadWaves} 波`);
      lines.push("");
      lines.push("## 对话");
      lines.push("");
      const messages = Array.isArray(r.messages) && r.messages.length ? r.messages : [{ role: "user", content: r.user_input }];
      const roleNames = { user: "用户", assistant: "助手", tool: "工具", system: "系统" };
      messages.forEach((m) => {
        lines.push(`### ${roleNames[String(m.role || "").toLowerCase()] || "系统"}`);
        lines.push("");
        lines.push(String(m.content || ""));
        lines.push("");
      });
      const reasoning = String(r.reasoning || "").trim();
      if (reasoning) {
        lines.push("## 思考过程 (Thinking)");
        lines.push("");
        lines.push(reasoning);
        lines.push("");
      }
      lines.push("## 回复");
      lines.push("");
      if (r.status === "error" && String(r.error || "").trim()) {
        lines.push(`> 失败: ${r.error}`);
        lines.push("");
      }
      lines.push(String(r.content || "（空回复）"));
      lines.push("");
      if (Array.isArray(r.files) && r.files.length) {
        lines.push("## 文件清单");
        lines.push("");
        r.files.forEach((f) => lines.push(`- ${f.name}${f.size ? ` (${formatBytes(f.size)})` : ""}${f.content_type ? ` — ${f.content_type}` : ""}`));
        lines.push("");
      }
      if (delivery.contextFiles.length) {
        lines.push("## 内部附件");
        lines.push("");
        delivery.contextFiles.forEach((file, index) => {
          lines.push(`### ${contextFileWaveLabel(delivery, index)} · ${contextFileKindLabel(file)} · ${file.name || "未命名"}`);
          lines.push("");
          lines.push("```");
          lines.push(String(file.content || ""));
          lines.push("```");
          lines.push("");
        });
      }
      if (delivery.finalPrompt.trim()) {
        lines.push("## 上游网页输入框");
        lines.push("");
        lines.push("```");
        lines.push(delivery.finalPrompt);
        lines.push("```");
        lines.push("");
      }
      return lines.join("\n");
    }

    async function loadRecordList(reset, opts = {}) {
      if (recordState.loading) return;
      const silent = Boolean(opts.silent);
      const listEl = $("history-list");
      const keepScroll = silent ? listEl.scrollTop : 0;
      if (reset) {
        recordState.page = 0;
        recordState.records = [];
        recordState.exhausted = false;
        if (!silent) {
          listEl.innerHTML = `<div class="history-list-empty">正在加载…</div>`;
          showHistoryEmpty();
        }
      }
      $("btn-history-more").style.display = "none";
      const nextPage = recordState.page + 1;
      recordState.loading = true;
      const moreBtn = $("btn-history-more");
      if (!reset && recordState.records.length) moreBtn.disabled = true;
      try {
        const qs = new URLSearchParams({ page: String(nextPage) });
        if (recordState.text) qs.set("text", recordState.text);
        if (recordState.status) qs.set("status", recordState.status);
        const resp = await fetchWithTimeout(
          `/api/history/records?${qs.toString()}`,
          { headers: apiHeaders() },
          MANAGEMENT_FETCH_TIMEOUT_MS
        );
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok || !data.ok) {
          throw new Error(data.error?.message || `HTTP ${resp.status}`);
        }
        recordState.page = nextPage;
        const items = Array.isArray(data.records) ? data.records : [];
        if (!items.length) {
          recordState.exhausted = true;
        } else {
          recordState.records = reset ? items : recordState.records.concat(items);
        }
        renderRecordList();
        if (silent) listEl.scrollTop = keepScroll;
      } catch (err) {
        if (reset && !silent) {
          listEl.innerHTML = `<div class="history-list-empty">加载失败：${escapeHtml(String(err.message || err))}<br>点击上方「刷新」重试</div>`;
        } else if (!silent) {
          window.showToast?.(`加载更多记录失败：${err.message || err}`, "error");
        }
      } finally {
        recordState.loading = false;
        moreBtn.disabled = false;
        moreBtn.style.display = (!recordState.exhausted && recordState.records.length) ? "" : "none";
      }
    }

    function renderRecordList() {
      const listEl = $("history-list");
      const countEl = $("history-count");
      if (countEl) countEl.textContent = String(recordState.records.length);
      if (!recordState.records.length) {
        listEl.innerHTML = `<div class="history-list-empty">暂无请求记录<br>通过任意接口或控制台发送一条消息后，这里会出现完整镜像</div>`;
        return;
      }
      listEl.innerHTML = "";
      recordState.records.forEach((r) => {
        const chip = statusChipInfo(r.status);
        const item = document.createElement("div");
        item.className = `history-item ${r.id === recordState.currentId ? "active" : ""} ${r.status === "error" ? "err" : ""}`;
        const bits = [];
        if (r.model) bits.push(r.model);
        bits.push(surfaceLabel(r.surface));
        if (r.account) bits.push(`账号 ${r.account}`);
        const t = formatRecordTime(r.updated_at || r.created_at);
        if (t) bits.push(t);
        item.innerHTML = `
          <div style="display:flex; align-items:center; gap:6px;">
            <div class="history-item-title" style="flex:1; min-width:0;">${escapeHtml(r.title || "请求记录")}</div>
            <span class="hst-chip ${chip.cls}">${chip.label}</span>
            <button class="rec-item-del" type="button" title="删除这条记录">✕</button>
          </div>
          <div class="history-item-preview">${escapeHtml(r.preview || "")}</div>
          <div class="history-item-meta">
            <span style="overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">${escapeHtml(bits.join(" · "))}</span>
            <span class="history-badge">${r.delivery_mode === "file" ? (Number(r.context_preload_waves) > 0 ? `分批${Number(r.context_preload_waves) + 1}波` : `拆分${r.context_files || ""}`) : (r.context_file_fallback ? "降级直传" : "直传")}</span>
            ${r.files ? `<span class="history-badge">📎${r.files}</span>` : ""}
          </div>
        `;
        item.addEventListener("click", () => openRecord(r.id));
        item.querySelector(".rec-item-del").addEventListener("click", (ev) => {
          ev.stopPropagation();
          deleteRecordById(r.id);
        });
        listEl.appendChild(item);
      });
    }

    async function deleteRecordById(id) {
      if (!id) return;
      if (!window.confirm("确定删除这条本地镜像记录？仅影响本地回看，不影响上游会话。")) return;
      try {
        if (recordState.currentId === id) stopRecordPoll();
        const resp = await fetchWithTimeout("/api/history/record/delete", {
          method: "POST",
          headers: apiHeaders({ "Content-Type": "application/json" }),
          body: JSON.stringify({ id })
        }, MANAGEMENT_FETCH_TIMEOUT_MS);
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok || !data.ok) {
          throw new Error(data.error?.message || `HTTP ${resp.status}`);
        }
        recordState.records = recordState.records.filter((x) => x.id !== id);
        if (recordState.currentId === id) {
          recordState.currentId = "";
          recordState.current = null;
          showHistoryEmpty();
        }
        renderRecordList();
        showHistoryMutationResult(data, "已删除记录");
      } catch (err) {
        window.showToast?.(`删除失败：${err.message || err}`, "error");
      }
    }

    function showHistoryEmpty() {
      stopRecordPoll();
      $("record-view").style.display = "none";
      $("history-empty").style.display = "";
    }

    function setRecordView(view) {
      recordState.view = view;
      $("btn-record-view-chat").classList.toggle("active", view === "chat");
      $("btn-record-view-overview").classList.toggle("active", view === "overview");
      $("btn-record-view-detail").classList.toggle("active", view === "detail");
      $("record-chat-view").style.display = view === "chat" ? "flex" : "none";
      $("record-overview-view").style.display = view === "overview" ? "flex" : "none";
      $("record-detail-view").style.display = view === "detail" ? "block" : "none";
    }

    function metaCard(label, value, mono) {
      return `<div class="rec-meta-card">
        <div class="rec-meta-label">${escapeHtml(label)}</div>
        <div class="rec-meta-value${mono ? " mono" : ""}">${escapeHtml(value || "—")}</div>
      </div>`;
    }

    function recFileChip(f) {
      const size = formatBytes(f.size);
      return `<span class="rec-file-chip" title="${escapeHtml(f.content_type || "")}">
        <span>📄</span><span class="name">${escapeHtml(f.name || "")}</span>
        ${size ? `<span style="color:var(--text-dim)">${escapeHtml(size)}</span>` : ""}
      </span>`;
    }

    function recBubbleNode(role, content, files) {
      const wrap = document.createElement("div");
      wrap.className = `rec-msg ${role}`;
      const avatarChar = role === "user" ? "👤" : (role === "assistant" ? "🤖" : (role === "tool" ? "🔧" : "⚙️"));
      const roleLabel = role === "user" ? "用户" : (role === "assistant" ? "助手" : (role === "tool" ? "工具" : "系统"));
      const fileChips = (files || []).map(recFileChip).join("");
      wrap.innerHTML = `
        <div class="rec-avatar">${avatarChar}</div>
        <div class="rec-msg-body">
          <div class="rec-role-label">${roleLabel}</div>
          ${fileChips ? `<div class="rec-file-chips">${fileChips}</div>` : ""}
          <div class="rec-bubble"></div>
        </div>
      `;
      const bubble = wrap.querySelector(".rec-bubble");
      const text = String(content || "").trim();
      if (role === "assistant") {
        bubble.style.whiteSpace = "normal";
        bubble.innerHTML = text ? renderMarkdown(text) : `<span class="history-empty-content">（无内容）</span>`;
      } else if (text) {
        bubble.appendChild(buildExpandable(text));
      } else {
        bubble.innerHTML = `<span class="history-empty-content">（无内容）</span>`;
      }
      return wrap;
    }

    function renderRecordChatView(r) {
      const container = $("record-chat-view");
      container.replaceChildren();
      const messages = Array.isArray(r.messages) && r.messages.length
        ? r.messages
        : [{ role: "user", content: r.user_input }];
      const files = recordDeliveryInfo(r).userFiles;
      // 文件清单挂在最后一条 user 消息上（对齐"对话框发了什么文件"）
      let lastUserIdx = -1;
      messages.forEach((m, i) => {
        if (String(m.role || "").toLowerCase() === "user") lastUserIdx = i;
      });
      messages.forEach((m, i) => {
        const rawRole = String(m.role || "").toLowerCase();
        const role = ["user", "assistant", "tool"].includes(rawRole) ? rawRole : "system";
        container.appendChild(recBubbleNode(role, m.content, i === lastUserIdx ? files : []));
      });

      // 助手响应块：思考过程 + 流式回复（或失败原因 / 停止前的部分内容）
      const wrap = document.createElement("div");
      wrap.className = "rec-msg assistant";
      wrap.innerHTML = `
        <div class="rec-avatar">🤖</div>
        <div class="rec-msg-body">
          <div class="rec-role-label">助手 · 回复</div>
          <div class="rec-attach"></div>
          <div class="rec-bubble resp" style="white-space:normal;"></div>
        </div>
      `;
      const attach = wrap.querySelector(".rec-attach");
      attach.style.display = "flex";
      attach.style.flexDirection = "column";
      attach.style.gap = "8px";
      const reasoning = String(r.reasoning || "").trim();
      if (reasoning) {
        const box = document.createElement("div");
        box.className = "rec-reasoning";
        const title = document.createElement("div");
        title.className = "rec-reasoning-title";
        title.textContent = "✨ 思考过程 (Thinking)";
        box.appendChild(title);
        box.appendChild(buildExpandable(reasoning, 1200));
        attach.appendChild(box);
      }
      const respBubble = wrap.querySelector(".resp");
      const content = String(r.content || "").trim();
      if (r.status === "error") {
        respBubble.innerHTML = `<div class="rec-error-banner">${escapeHtml(r.error || "请求失败")}</div>`;
        if (content) {
          const partial = document.createElement("div");
          partial.innerHTML = `<div style="font-size:10.5px; color:var(--text-dim); margin:8px 0 4px;">中断前已生成的部分内容：</div><div>${renderMarkdown(content)}</div>`;
          respBubble.appendChild(partial);
        }
      } else {
        respBubble.innerHTML = content ? renderMarkdown(content) : `<span class="history-empty-content">（空回复）</span>`;
      }
      container.appendChild(wrap);
    }

    function renderTextCard(label, text, filenameBase, emptyText, cls = "rec-merged") {
      const container = document.createElement("div");
      const value = String(text || "");
      if (!value.trim()) {
        if (emptyText) {
          const empty = document.createElement("div");
          empty.className = "history-empty-content";
          empty.textContent = emptyText;
          container.appendChild(empty);
        }
        return container;
      }
      const card = document.createElement("div");
      card.className = cls;
      card.innerHTML = `
        <div class="rec-merged-head">
          <div class="rec-merged-label">${escapeHtml(label)}</div>
          <div class="rec-merged-actions">
            <button class="tool-btn" type="button" title="复制" data-act="copy"><span>复制</span></button>
            <button class="tool-btn" type="button" title="下载" data-act="download"><span>下载</span></button>
          </div>
        </div>
        <div class="rec-merged-text"></div>
      `;
      card.querySelector(".rec-merged-text").appendChild(buildExpandable(value, 700));
      card.querySelector('[data-act="copy"]').addEventListener("click", async () => {
        const ok = await copyTextToClipboard(value);
        window.showToast?.(ok ? "已复制" : "复制失败", ok ? "success" : "error");
      });
      card.querySelector('[data-act="download"]').addEventListener("click", () => {
        downloadTextFile(filenameBase, value);
        window.showToast?.("已下载文本", "success");
      });
      container.appendChild(card);
      return container;
    }

    function contextKindLabel(kind) {
      switch (String(kind || "").toLowerCase()) {
        case "history": return "历史对话";
        case "tools": return "工具定义";
        default: return "内部上下文";
      }
    }

    function contextFileKindLabel(file) {
      const label = contextKindLabel(file && file.kind);
      const headerMatch = String(file && file.content || "").slice(0, 320).match(/\bsegment\s+(\d+)\/(\d+)\b/i);
      const part = Number(file && file.part) || Number(headerMatch && headerMatch[1]) || 1;
      const parts = Number(file && file.parts) || Number(headerMatch && headerMatch[2]) || 1;
      return parts > 1 ? `${label} ${part}/${parts}` : label;
    }

    function recordDeliveryInfo(r) {
      const storedContextFiles = Array.isArray(r.context_files)
        ? r.context_files.filter((f) => f && typeof f === "object")
        : [];
      const finalPrompt = String(r.final_prompt || r.user_input || "");
      let mode = String(r.delivery_mode || "").toLowerCase();
      if (!['inline', 'file'].includes(mode)) {
        mode = storedContextFiles.length || (
          String(r.history_text || "").trim()
          && finalPrompt.toLowerCase().includes("attached file holds the earlier conversation")
        ) ? "file" : "inline";
      }
      const legacy = mode === "file" && !storedContextFiles.length && Boolean(String(r.history_text || "").trim());
      const contextFiles = storedContextFiles.slice();
      if (legacy) {
        contextFiles.push({
          kind: "history",
          name: "历史附件（旧记录未保存原名）.txt",
          size: new Blob([String(r.history_text || "")]).size,
          content_type: "text/plain; charset=utf-8",
          content: String(r.history_text || ""),
          truncated: false
        });
      }
      const allFiles = Array.isArray(r.files) ? r.files : [];
      const internalNames = new Set(contextFiles.map((f) => String(f.name || "")));
      const userFiles = legacy
        ? []
        : allFiles.filter((f) => !internalNames.has(String(f.name || "")));
      const preloadWaves = Math.max(0, Number(r.context_preload_waves) || 0);
      const preloadFiles = Math.max(0, Math.min(contextFiles.length, Number(r.context_preload_files) || 0));
      return {
        mode,
        requested: Boolean(r.context_file_requested) || mode === "file",
        fallback: String(r.context_file_fallback || ""),
        finalPrompt,
        contextFiles,
        userFiles,
        allFiles,
        legacy,
        preloadWaves,
        preloadFiles
      };
    }

    function contextFileWaveLabel(info, index) {
      if (!info.preloadWaves) return `附件 ${index + 1}`;
      if (index < info.preloadFiles) {
        return `预载波次 ${Math.min(info.preloadWaves, Math.floor(index / 10) + 1)}/${info.preloadWaves}`;
      }
      return "最终波次";
    }

    function manifestRowsHtml(files, kindResolver) {
      if (!files.length) return `<div class="history-empty-content">没有附件</div>`;
      return `<div class="rec-manifest">${files.map((f, index) => `
        <div class="rec-manifest-row">
          <span class="rec-manifest-kind">${escapeHtml(kindResolver(f, index))}</span>
          <span class="rec-manifest-name" title="${escapeHtml(f.name || "")}">${escapeHtml(f.name || "未命名文件")}</span>
          <span class="rec-manifest-size">${escapeHtml(formatBytes(f.size) || "—")}</span>
        </div>
      `).join("")}</div>`;
    }

    function renderDeliveryBanner(r) {
      const info = recordDeliveryInfo(r);
      const host = $("record-delivery-banner");
      let cls = info.mode;
      let title = "直传文本";
      let copy = "历史未拆分；下方输入框文本就是实际发送给上游的提示词。";
      let chip = "INLINE";
      if (info.mode === "file") {
        title = info.preloadWaves
          ? `多波次历史拆分 · ${info.preloadWaves} 个预载波次 + 最终波次`
          : `历史拆分 · ${info.contextFiles.length} 个内部附件 + 输入框`;
        copy = info.preloadWaves
          ? `${info.preloadFiles} 个附件先分批读取并在 thinking 阶段停止，其余附件与输入框在同一 chat 的最终波次发送。`
          : "内部附件和输入框分别展示；实发详情可核对每个文件的名称与正文。";
        chip = info.preloadWaves ? "STAGED FILES" : "FILES + PROMPT";
      } else if (info.requested && info.fallback) {
        cls = "fallback";
        title = "历史拆分已回退为直传";
        copy = info.fallback === "degraded_window"
          ? "附件上传处于降级窗口，本次把完整上下文放回输入框发送。"
          : "内部附件上传失败，本次把完整上下文放回输入框发送。";
        chip = "FALLBACK";
      }
      host.className = `rec-delivery-banner ${cls}`;
      host.innerHTML = `
        <div>
          <div class="rec-delivery-kicker">UPSTREAM DELIVERY</div>
          <div class="rec-delivery-title">${escapeHtml(title)}</div>
          <div class="rec-delivery-copy">${escapeHtml(copy)}</div>
        </div>
        <span class="rec-mode-chip">${escapeHtml(chip)}</span>
      `;
    }

    function renderRecordOverview(r) {
      const container = $("record-overview-view");
      container.replaceChildren();
      const info = recordDeliveryInfo(r);
      const route = document.createElement("div");
      route.className = "rec-send-route";
      if (info.mode === "file") {
        route.innerHTML = `
          <div class="rec-send-node">
            <div class="rec-send-node-label">${info.preloadWaves ? "Context preload" : "Attachments"}</div>
            <div class="rec-send-node-title">${info.preloadWaves ? `${info.preloadWaves} 波 · ${info.preloadFiles} 个预载附件` : `${info.contextFiles.length} 个内部附件${info.userFiles.length ? ` + ${info.userFiles.length} 个用户附件` : ""}`}</div>
            <div class="rec-send-node-meta">${info.preloadWaves ? "每波进入 thinking 后停止，并由 assistant parent 接续。" : "附件先上传，再随 completion 请求一并引用。"}</div>
          </div>
          <div class="rec-send-arrow">→</div>
          <div class="rec-send-node primary">
            <div class="rec-send-node-label">${info.preloadWaves ? "Final wave" : "Web input"}</div>
            <div class="rec-send-node-title">输入框文本${info.preloadWaves ? ` + ${info.contextFiles.length - info.preloadFiles} 个内部附件${info.userFiles.length ? ` + ${info.userFiles.length} 个用户附件` : ""}` : ""}</div>
            <div class="rec-send-node-meta">${info.finalPrompt.length.toLocaleString()} 字符 · ${info.preloadWaves ? "复用同一 chat 完成回答" : "与附件共同发送"}</div>
          </div>
        `;
      } else {
        route.innerHTML = `
          <div class="rec-send-node primary" style="grid-column:1 / -1;">
            <div class="rec-send-node-label">Web input · direct</div>
            <div class="rec-send-node-title">只有输入框承载上下文${info.userFiles.length ? `，另附 ${info.userFiles.length} 个用户文件` : ""}</div>
            <div class="rec-send-node-meta">没有生成历史/工具内部附件；${info.finalPrompt.length.toLocaleString()} 字符原样进入上游输入框。</div>
          </div>
        `;
      }
      container.appendChild(route);

      if (info.mode === "file") {
        const manifest = document.createElement("div");
        manifest.innerHTML = `<div class="rec-section-title">内部附件清单</div>${manifestRowsHtml(info.contextFiles, (f) => contextFileKindLabel(f))}`;
        container.appendChild(manifest);
      }
      if (info.userFiles.length) {
        const userManifest = document.createElement("div");
        userManifest.innerHTML = `<div class="rec-section-title">用户附件清单</div>${manifestRowsHtml(info.userFiles, () => "用户附件")}`;
        container.appendChild(userManifest);
      }
      container.appendChild(renderTextCard(
        "网页输入框 · 实际发送文本",
        info.finalPrompt,
        `upstream_input_${String(r.id || "record").replace(/^req_/, "")}.txt`,
        "（无输入框文本记录）"
      ));
    }

    function renderRecordDeliveryDetail(r) {
      const container = $("record-detail-view");
      container.replaceChildren();
      const stack = document.createElement("div");
      stack.className = "rec-detail-stack";
      const info = recordDeliveryInfo(r);
      if (info.legacy) {
        const note = document.createElement("div");
        note.className = "rec-legacy-note";
        note.textContent = "这是旧版镜像：只留存了历史附件正文，原始文件名和工具定义附件未记录，无法事后还原。新请求会完整记录。";
        stack.appendChild(note);
      }
      if (info.mode === "file") {
        info.contextFiles.forEach((file, index) => {
          const label = `${contextFileWaveLabel(info, index)} · ${contextFileKindLabel(file)} · ${file.name || "未命名"}${file.truncated ? " · 镜像已截断" : ""}`;
          stack.appendChild(renderTextCard(
            label,
            file.content || "",
            file.name || `context_${index + 1}.txt`,
            "（附件正文为空）",
            "rec-history"
          ));
        });
      }
      if (info.userFiles.length) {
        const note = document.createElement("div");
        note.className = "rec-legacy-note";
        note.innerHTML = `<strong>用户附件只保存元数据</strong><br>${info.userFiles.map((f) => escapeHtml(f.name || "未命名文件")).join(" · ")}`;
        stack.appendChild(note);
      }
      stack.appendChild(renderTextCard(
        info.mode === "file" ? "输入框 · 与上述附件共同发送" : "输入框 · 实际发送给上游的全部提示词",
        info.finalPrompt,
        `upstream_input_${String(r.id || "record").replace(/^req_/, "")}.txt`,
        "（无输入框文本记录）"
      ));
      container.appendChild(stack);
    }

    function callerLabel(caller) {
      switch (String(caller || "")) {
        case "panel": return "控制台面板";
        case "cli": return "CLI 直连";
        case "api": return "API Key 接入";
        default: return "";
      }
    }

    function renderRecordDetail() {
      const r = recordState.current;
      if (!r) return;
      const chip = statusChipInfo(r.status);
      const statusEl = $("record-status");
      statusEl.className = `hst-chip ${chip.cls}`;
      statusEl.textContent = chip.label;
      $("record-title").textContent = r.title || "请求记录";
      const metaBits = [];
      if (r.model) metaBits.push(r.model);
      metaBits.push(surfaceLabel(r.surface));
      metaBits.push(r.stream ? "流式" : "非流式");
      const t = formatRecordTime(r.created_at);
      if (t) metaBits.push(t);
      $("record-meta").textContent = metaBits.join(" · ");
      const usage = r.usage || {};
      const delivery = recordDeliveryInfo(r);
      $("record-meta-grid").innerHTML = [
        metaCard("接口面", surfaceLabel(r.surface)),
        metaCard("模型", r.model || ""),
        metaCard("上游实发", delivery.mode === "file" ? (delivery.preloadWaves ? `${delivery.preloadWaves + 1} 波分批` : "附件 + 输入框") : (delivery.requested && delivery.fallback ? "降级直传" : "输入框直传")),
        metaCard("耗时", formatElapsed(r.elapsed_ms)),
        metaCard("状态码", r.status_code ? String(r.status_code) : ""),
        metaCard("调用模式", r.stream ? "流式" : "非流式"),
        metaCard("请求时间", t || ""),
        metaCard("调用方", callerLabel(r.caller)),
        metaCard("账号", r.account || "", true),
        metaCard("上游会话", r.chat_id ? `${r.chat_id.slice(0, 8)}…` : "", true),
        metaCard("Tokens（估）", usage.total_tokens ? `P ${usage.prompt_tokens || 0} · C ${usage.completion_tokens || 0} · R ${usage.reasoning_tokens || 0}` : "")
      ].join("");
      $("record-tokens").textContent = delivery.mode === "file"
        ? `内部附件 ${delivery.contextFiles.length} · 用户附件 ${delivery.userFiles.length}${delivery.preloadWaves ? ` · 预载 ${delivery.preloadWaves} 波/${delivery.preloadFiles} 文件` : ""} · 输入框 ${delivery.finalPrompt.length.toLocaleString()} 字符`
        : `输入框 ${delivery.finalPrompt.length.toLocaleString()} 字符${delivery.userFiles.length ? ` · 用户附件 ${delivery.userFiles.length}` : ""}`;
      renderDeliveryBanner(r);
      renderRecordChatView(r);
      renderRecordOverview(r);
      renderRecordDeliveryDetail(r);
      setRecordView(recordState.view);
    }

    async function openRecord(id) {
      stopRecordPoll();
      recordState.currentId = id;
      renderRecordList();
      $("history-empty").style.display = "none";
      $("record-view").style.display = "";
      $("record-title").textContent = "加载中…";
      $("record-meta").textContent = "";
      $("record-status").textContent = "";
      $("record-meta-grid").innerHTML = "";
      $("record-delivery-banner").replaceChildren();
      $("record-chat-view").replaceChildren();
      $("record-overview-view").replaceChildren();
      $("record-detail-view").replaceChildren();
      try {
        const resp = await fetchWithTimeout(
          `/api/history/record?id=${encodeURIComponent(id)}`,
          { headers: apiHeaders() },
          MANAGEMENT_FETCH_TIMEOUT_MS
        );
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok || !data.ok) {
          throw new Error(data.error?.message || `HTTP ${resp.status}`);
        }
        recordState.current = data.record || null;
        renderRecordDetail();
        scheduleRecordPoll();
        $("record-scroll").scrollTop = 0;
      } catch (err) {
        $("record-title").textContent = "加载失败";
        $("record-meta").textContent = String(err.message || err);
      }
    }

    async function deleteRecord() {
      const id = recordState.currentId;
      if (!id) return;
      if (!window.confirm("确定删除这条本地镜像记录？仅影响本地回看，不影响上游会话。")) return;
      try {
        stopRecordPoll();
        const resp = await fetchWithTimeout("/api/history/record/delete", {
          method: "POST",
          headers: apiHeaders({ "Content-Type": "application/json" }),
          body: JSON.stringify({ id })
        }, MANAGEMENT_FETCH_TIMEOUT_MS);
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok || !data.ok) {
          throw new Error(data.error?.message || `HTTP ${resp.status}`);
        }
        recordState.records = recordState.records.filter((r) => r.id !== id);
        recordState.currentId = "";
        recordState.current = null;
        renderRecordList();
        showHistoryEmpty();
        showHistoryMutationResult(data, "已删除记录");
      } catch (err) {
        window.showToast?.(`删除失败：${err.message || err}`, "error");
      }
    }

    function exportRecordMarkdown() {
      const r = recordState.current;
      if (!r) return;
      downloadTextFile(`record_${String(r.id || "export").replace(/^req_/, "")}.md`, recordToMarkdown(r));
      window.showToast?.("已导出 Markdown", "success");
    }

    $("btn-history-refresh").addEventListener("click", () => loadRecordList(true));
    $("btn-history-more").addEventListener("click", () => loadRecordList(false));
    $("btn-record-delete").addEventListener("click", deleteRecord);
    $("btn-record-top").addEventListener("click", () => {
      $("record-scroll").scrollTo({ top: 0, behavior: "smooth" });
    });
    $("btn-record-export").addEventListener("click", exportRecordMarkdown);
    $("btn-record-view-chat").addEventListener("click", () => setRecordView("chat"));
    $("btn-record-view-overview").addEventListener("click", () => setRecordView("overview"));
    $("btn-record-view-detail").addEventListener("click", () => setRecordView("detail"));

    // 搜索/状态筛选：输入防抖 300ms，选择即查
    let historySearchDebounce = 0;
    $("history-search-input").addEventListener("input", () => {
      clearTimeout(historySearchDebounce);
      historySearchDebounce = setTimeout(() => {
        recordState.text = $("history-search-input").value.trim();
        loadRecordList(true);
      }, 300);
    });
    $("history-status-select").addEventListener("change", () => {
      recordState.status = $("history-status-select").value;
      loadRecordList(true);
    });

    document.querySelectorAll(".nav-item").forEach(btn => {
      btn.addEventListener("click", () => navigateTo(btn.dataset.page));
    });
    document.querySelectorAll("[data-goto]").forEach(btn => {
      btn.addEventListener("click", () => navigateTo(btn.dataset.goto));
    });
    $("nav-account-card").addEventListener("click", () => navigateTo("accounts"));

    // ==========================================================================
