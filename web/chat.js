    // UI STATUS & SESSIONS MANAGEMENT
    // ==========================================================================
    function updateStatusUI(status, stateClass = "connected") {
      statusText.textContent = status;
      statusPill.className = `pill ${stateClass}`;
      $("info-status").textContent = status;
    }

    function updateSessionUI() {
      if (!state.currentChatId) {
        currentChatIdBadge.textContent = "新建独立会话";
        currentChatIdBadge.style.color = "var(--text-dim)";
      } else {
        currentChatIdBadge.textContent = state.currentChatId;
        currentChatIdBadge.style.color = "var(--cyan-core)";
      }
      btnDeleteUpstream.disabled = !state.currentChatId || Boolean(state.activeRequest);
    }

    // 各模型思考挡位（2026-08 官方 UI）：glm-5.3 / Flash 三挡，glm-5.2 两挡，
    // GLM-5-Turbo 只能开关思考、无挡位。
    function effortOptionsForModel(modelId) {
      const m = (modelId || "").toLowerCase();
      if (m === "x-preview-l" || m.startsWith("glm-5.3")) {
        return [["max", "max"], ["high", "high"], ["low", "low"]];
      }
      if (m === "glm-5-turbo") {
        return [["max", "—"]];
      }
      return [["max", "max"], ["high", "high"]];
    }

    function syncEffortOptions() {
      const options = effortOptionsForModel(modelSelect.value);
      reasoningEffortSelect.innerHTML = "";
      for (const [value, label] of options) {
        const opt = document.createElement("option");
        opt.value = value;
        opt.textContent = label;
        reasoningEffortSelect.appendChild(opt);
      }
      // 优先该模型自己记忆的挡位；没有则默认最高挡 max。
      const remembered = state.effortByModel?.[modelSelect.value];
      if (remembered && options.some(o => o[0] === remembered)) {
        reasoningEffortSelect.value = remembered;
      }
      // 设置页的挡位只读回显
      const hiddenEffort = $("reasoning-effort-hidden");
      if (hiddenEffort) hiddenEffort.value = reasoningEffortSelect.value || "max";
    }

    function syncOptionStates() {
      syncEffortOptions();
      const isEffortModel = /^(glm-5\.2|glm-5\.3)/.test(modelSelect.value.toLowerCase());
      reasoningEffortSelect.disabled = !thinkingToggle.checked || !isEffortModel;
      reasoningEffortGroup.style.opacity = reasoningEffortSelect.disabled ? "0.45" : "1";

      reuseChatToggle.disabled = autoDeleteToggle.checked;
      if (autoDeleteToggle.checked) {
        reuseChatToggle.checked = false;
      }
      syncChipsFromToggles();
    }

    // 工具条 chip 开关 ↔ 隐藏 checkbox 双向同步
    function syncChipsFromToggles() {
      setChip("chip-thinking", thinkingToggle.checked);
      setChip("chip-search", webSearchToggle.checked);
      setChip("chip-include-thinking", includeThinkingToggle.checked);
      setChip("chip-auto-delete", autoDeleteToggle.checked);
    }

    function setChip(id, on) {
      const chip = $(id);
      if (chip) chip.classList.toggle("on", !!on);
    }

    function bindChip(chipId, toggleInput) {
      $(chipId).addEventListener("click", () => {
        toggleInput.checked = !toggleInput.checked;
        toggleInput.dispatchEvent(new Event("change"));
        syncChipsFromToggles();
      });
    }

    function renderSessionsList() {
      // Legacy sessions without a profileId stay visible everywhere; sessions
      // recorded after this change are isolated per account.
      const visibleSessions = state.sessions.filter(s => !s.profileId || s.profileId === state.activeProfileId);
      if (!visibleSessions.length) {
        sessionListEl.innerHTML = `<div class="session-list-empty">暂无历史对话<br>发送消息后将自动归档在此</div>`;
        return;
      }

      sessionListEl.innerHTML = "";
      visibleSessions.forEach((s) => {
        const item = document.createElement("div");
        item.className = `session-item ${s.id === state.currentSessionId ? "active" : ""}`;
        item.innerHTML = `
          <div class="session-item-title">${escapeHtml(s.title || "新会话")}</div>
          <div class="session-item-meta">
            <span>${s.messages?.length || 0} 条消息</span>
            <span>${escapeHtml(s.time || "")}</span>
          </div>
          <span class="session-item-del" title="删除记录">✕</span>
        `;

        item.addEventListener("click", (e) => {
          if (e.target.classList.contains("session-item-del")) {
            e.stopPropagation();
            state.sessions = state.sessions.filter(sess => sess.id !== s.id);
            saveLocalSessions();
            renderSessionsList();
            if (state.currentSessionId === s.id) {
              startNewChatSession();
            }
            return;
          }
          loadSessionIntoChat(s.id);
        });

        sessionListEl.appendChild(item);
      });
    }

    function startNewChatSession() {
      if (state.activeRequest) {
        window.showToast("正在生成中，请先停止再新建会话", "error");
        return;
      }
      state.currentChatId = "";
      state.currentChatProfileId = state.activeProfileId || "";
      state.currentSessionId = newLocalSessionId();
      state.pendingEdit = false;
      state.lastUserMessage = "";
      updateSessionUI();
      messageStream.replaceChildren();
      appendMessageNode("system", "已切换为新会话环境，下次发送将创建独立上游会话。");
      renderSessionsList();
      window.showToast("已重置为新会话");
    }

    function applyProfileSessionFilter() {
      state.currentSessionId = "";
      state.currentChatId = "";
      state.currentChatProfileId = state.activeProfileId || "";
      state.pendingEdit = false;
      state.lastUserMessage = "";
      updateSessionUI();
      messageStream.replaceChildren();
      renderSessionsList();
    }

    function loadSessionIntoChat(sessionId) {
      if (state.activeRequest) {
        window.showToast("正在生成中，请先停止再切换会话", "error");
        return;
      }
      const sess = state.sessions.find(s => s.id === sessionId);
      if (!sess) return;
      state.currentSessionId = sess.id;
      state.currentChatId = sess.upstreamChatId || "";
      state.currentChatProfileId = sess.profileId || state.activeProfileId || "";
      state.pendingEdit = false;
      const lastUserMsg = [...(sess.messages || [])].reverse().find(m => m.role === "user");
      state.lastUserMessage = lastUserMsg ? String(lastUserMsg.content || "") : "";
      updateSessionUI();
      messageStream.replaceChildren();

      (sess.messages || []).forEach(msg => {
        const node = appendMessageNode(msg.role, msg.content, {
          model: msg.model,
          attachments: msg.attachments
        });
        if (msg.thinking) {
          const container = node.querySelector(".thinking-container");
          if (container) {
            const thinkingBox = document.createElement("div");
            thinkingBox.className = "thinking-box collapsed";
            thinkingBox.innerHTML = `
              <div class="thinking-header">
                <span>思考过程 (Thinking)</span>
                <span class="thinking-toggle-icon">▸ 展开</span>
              </div>
              <div class="thinking-body">${escapeHtml(msg.thinking)}</div>
            `;
            container.appendChild(thinkingBox);
            thinkingBox.querySelector(".thinking-header").addEventListener("click", () => {
              thinkingBox.classList.toggle("collapsed");
              const isCol = thinkingBox.classList.contains("collapsed");
              thinkingBox.querySelector(".thinking-toggle-icon").textContent = isCol ? "▸ 展开" : "▾ 折叠";
            });
          }
        }
        if (msg.incomplete) {
          appendMessageNode("system", "上一条助手回复在完成事件前中断，仅保留已收到的部分内容。");
        }
      });
      renderSessionsList();
    }

    function bindCurrentSessionProfile(profileId, requestState = null) {
      const id = String(profileId || "");
      if (!id) return;
      state.currentChatProfileId = id;
      if (requestState) requestState.profileId = id;
      const sess = state.sessions.find(s => s.id === state.currentSessionId);
      if (sess && sess.profileId !== id) {
        sess.profileId = id;
        saveLocalSessions();
        renderSessionsList();
      }
    }

    function recordCurrentSession(title, role, content, thinking = "", attachments = [], metadata = {}) {
      if (!state.currentSessionId) {
        state.currentSessionId = newLocalSessionId();
      }
      const existingIndex = state.sessions.findIndex(s => s.id === state.currentSessionId);
      let sess = existingIndex >= 0 ? state.sessions[existingIndex] : null;
      const now = new Date();
      const timeStr = `${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}`;

      if (!sess) {
        const cleanTitle = (title || "新会话").replace(/\s+/g, " ").slice(0, 28);
        sess = {
          id: state.currentSessionId,
          title: cleanTitle,
          time: `${now.getMonth()+1}/${now.getDate()} ${timeStr}`,
          upstreamChatId: state.currentChatId,
          profileId: state.currentChatProfileId || state.activeProfileId,
          messages: []
        };
        state.sessions.unshift(sess);
      } else if (existingIndex > 0) {
        state.sessions.splice(existingIndex, 1);
        state.sessions.unshift(sess);
      }

      sess.time = `${now.getMonth()+1}/${now.getDate()} ${timeStr}`;
      sess.upstreamChatId = state.currentChatId;
      sess.profileId = state.currentChatProfileId || state.activeProfileId || sess.profileId || "";
      if (!Array.isArray(sess.messages)) sess.messages = [];
      sess.messages.push({
        role,
        content,
        thinking,
        time: timeStr,
        attachments: (Array.isArray(attachments) ? attachments : [])
          .slice(0, LOCAL_SESSION_MAX_ATTACHMENTS)
          .map(normalizeLocalAttachment)
          .filter(Boolean),
        model: modelSelect.value,
        incomplete: Boolean(metadata.incomplete)
      });

      saveLocalSessions();
      renderSessionsList();
    }

    // Append Message to Visual Stream
    function appendMessageNode(role, text = "", options = {}) {
      const wrapper = document.createElement("div");
      wrapper.className = `msg-wrapper ${role}`;

      const now = new Date();
      const timeStr = `${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}`;

      let metaHtml = "";
      if (role !== "system") {
        const roleLabel = role === "user" ? "You" : (options.model || modelSelect.value);
        metaHtml = `
          <div class="msg-meta">
            <span class="role-name">${escapeHtml(roleLabel)}</span>
            <span class="time-stamp">${timeStr}</span>
          </div>
        `;
      }

      let contentHtml = "";
      if (role === "assistant") {
        contentHtml = `
          <div class="thinking-container"></div>
          <div class="msg-content">${renderMarkdown(text)}</div>
          <div class="msg-actions">
            <button class="action-chip-btn btn-copy-msg" title="复制回答">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg>
              <span>复制</span>
            </button>
          </div>
        `;
      } else if (role === "user") {
        let attachHtml = "";
        if (options.attachments && options.attachments.length) {
          attachHtml = `
            <div class="msg-attachments">
              ${options.attachments.map(f => `<span class="file-badge">📎 ${escapeHtml(f.name)} (${formatBytes(f.size)})</span>`).join("")}
            </div>
          `;
        }
        contentHtml = `
          <div class="msg-content">${renderMarkdown(text)}</div>
          ${attachHtml}
        `;
      } else {
        contentHtml = `<div class="msg-bubble">${escapeHtml(text)}</div>`;
      }

      if (role !== "system") {
        wrapper.innerHTML = `
          ${metaHtml}
          <div class="msg-bubble">
            ${contentHtml}
          </div>
        `;
      } else {
        wrapper.innerHTML = contentHtml;
      }

      messageStream.appendChild(wrapper);
      messageStream.scrollTop = messageStream.scrollHeight;

      // Bind copy button
      const copyBtn = wrapper.querySelector(".btn-copy-msg");
      if (copyBtn) {
        copyBtn.addEventListener("click", () => {
          const rawText = wrapper.querySelector(".msg-content")?.innerText || "";
          navigator.clipboard.writeText(rawText).then(() => {
            window.showToast("已复制回答内容", "success");
          });
        });
      }

      return wrapper;
    }

    // Attachments File Input Handlers
    const ZAI_MAX_COMPLETION_FILES = 10;

    function completionFileLimit() {
      return Math.max(1, Number(state.maxCompletionFiles) || ZAI_MAX_COMPLETION_FILES);
    }

    function appendSelectedFiles(files) {
      const incoming = Array.from(files || []);
      const maxFiles = completionFileLimit();
      const existingKeys = new Set(state.selectedFiles.map(f => `${f.name}:${f.size}:${f.lastModified}`));
      const unique = incoming.filter(f => {
        const key = `${f.name}:${f.size}:${f.lastModified}`;
        if (existingKeys.has(key)) return false;
        existingKeys.add(key);
        return true;
      });
      const available = Math.max(0, maxFiles - state.selectedFiles.length);
      const accepted = unique.slice(0, available);
      const overflow = unique.length - accepted.length;
      state.selectedFiles = [...state.selectedFiles, ...accepted];
      updateFilePreview();
      if (overflow) {
        window.showToast(`Z.ai 单次最多 ${maxFiles} 个附件，已忽略 ${overflow} 个`, "error");
      }
      return { accepted: accepted.length, duplicates: Math.max(0, incoming.length - unique.length), overflow };
    }

    function updateFilePreview() {
      attachedFilesBar.innerHTML = "";
      if (!state.selectedFiles.length) {
        uploadHint.textContent = "支持多文件 / 图片 / PDF";
        uploadHint.style.color = "var(--text-dim)";
        return;
      }

      const totalSize = state.selectedFiles.reduce((acc, f) => acc + f.size, 0);
      const limit = Number(state.maxChatFileUploadBytes) || 0;
      const oversized = limit > 0 ? state.selectedFiles.filter(f => f.size > limit) : [];
      let hint = `已选择 ${state.selectedFiles.length}/${completionFileLimit()} 个文件 (${formatBytes(totalSize)})`;
      if (oversized.length) {
        hint += `，${oversized.length} 个超过单文件上限 ${formatBytes(limit)}，发送前请移除`;
      }
      uploadHint.textContent = hint;
      uploadHint.style.color = oversized.length ? "var(--rose-core)" : "var(--cyan-core)";

      state.selectedFiles.forEach((file, idx) => {
        const pill = document.createElement("div");
        pill.className = "file-pill-item" + ((limit > 0 && file.size > limit) ? " file-pill-item-over" : "");
        const overBadge = (limit > 0 && file.size > limit) ? ` <span class="file-pill-over">超限</span>` : "";
        pill.innerHTML = `
          <span>${escapeHtml(file.name)} (${formatBytes(file.size)})${overBadge}</span>
          <span class="file-pill-remove" data-idx="${idx}" title="移除">✕</span>
        `;
        attachedFilesBar.appendChild(pill);
      });

      attachedFilesBar.querySelectorAll(".file-pill-remove").forEach(btn => {
        btn.addEventListener("click", (e) => {
          const index = Number(e.target.dataset.idx);
          state.selectedFiles.splice(index, 1);
          updateFilePreview();
        });
      });
    }

    chatFileInput.addEventListener("change", (e) => {
      const files = Array.from(e.target.files || []);
      appendSelectedFiles(files);
      chatFileInput.value = "";
    });

    // Drag & Drop files onto composer
    composerForm.addEventListener("dragover", (e) => {
      e.preventDefault();
      composerForm.style.borderColor = "var(--border-focus)";
    });
    composerForm.addEventListener("dragleave", () => {
      composerForm.style.borderColor = "var(--border-medium)";
    });
    composerForm.addEventListener("drop", (e) => {
      e.preventDefault();
      composerForm.style.borderColor = "var(--border-medium)";
      if (e.dataTransfer.files && e.dataTransfer.files.length) {
        appendSelectedFiles(Array.from(e.dataTransfer.files));
      }
    });

    // API Upload helpers
    const CHAT_FILE_UPLOAD_CONCURRENCY = 3;

    async function mapWithConcurrency(items, limit, worker) {
      const results = new Array(items.length);
      let nextIndex = 0;
      let firstError = null;
      async function runWorker() {
        while (!firstError) {
          const index = nextIndex++;
          if (index >= items.length) return;
          try {
            results[index] = await worker(items[index], index);
          } catch (error) {
            firstError = firstError || error;
          }
        }
      }
      const workerCount = Math.min(Math.max(1, Number(limit) || 1), items.length);
      await Promise.all(Array.from({ length: workerCount }, () => runWorker()));
      if (firstError) {
        firstError.completedResults = results.filter(Boolean);
        throw firstError;
      }
      return results;
    }

    async function uploadSingleFile(file, idx, total, profileId = "", signal = null) {
      updateStatusUI(`上传附件 ${idx + 1}/${total}...`, "standby");
      const params = new URLSearchParams({
        filename: file.name || `file-${idx + 1}.bin`,
        content_type: file.type || "application/octet-stream"
      });
      const res = await fetchWithTimeout(`/api/files/upload?${params.toString()}`, {
        method: "POST",
        headers: profileHeaders(profileId, { "Content-Type": file.type || "application/octet-stream" }),
        body: file,
        signal
      }, FILE_UPLOAD_FETCH_TIMEOUT_MS);
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.ok) {
        const retryAfter = Number(res.headers.get("Retry-After")) || 0;
        const suffix = res.status === 429 && retryAfter > 0 ? `（约 ${retryAfter} 秒后可重试）` : "";
        const error = new Error((data.error?.message || `附件上传失败 HTTP ${res.status}`) + suffix);
        error.code = data.error?.type || data.error?.code || "";
        error.scope = data.error?.scope || "";
        throw error;
      }
      return { file: data.file, profileId: data.profile_id || profileId };
    }

    async function uploadAllFiles(files, preferredProfileId = "", signal = null) {
      if (!files.length) return { files: [], profileId: preferredProfileId };
      const oversized = files.filter(f => f.size > state.maxChatFileUploadBytes);
      if (oversized.length) {
        throw new Error(`${oversized.map(f => f.name).join("、")} 超过单文件上限 ${formatBytes(state.maxChatFileUploadBytes)}`);
      }
      // 首个上传确定账号路由；其余文件并发上传到同一个账号，避免
      // “文件属于账号 A、聊天被切到账号 B”导致上游看不到附件。
      const first = await uploadSingleFile(files[0], 0, files.length, preferredProfileId, signal);
      const profileId = first.profileId || preferredProfileId;
      try {
        const rest = await mapWithConcurrency(
          files.slice(1),
          CHAT_FILE_UPLOAD_CONCURRENCY,
          (file, i) => uploadSingleFile(file, i + 1, files.length, profileId, signal)
        );
        return { files: [first.file, ...rest.map(item => item.file)], profileId };
      } catch (error) {
        // Preserve successfully uploaded ids so the outer failure handler can
        // remove partial-batch orphans instead of leaking them upstream.
        error.uploadedFiles = [first.file, ...(error.completedResults || []).map(item => item.file)];
        error.profileId = profileId;
        throw error;
      }
    }

    // SSE Stream Parser
    function parseSse(block) {
      const result = { event: "message", data: "" };
      for (const line of block.split(/\r?\n/)) {
        if (line.startsWith("event:")) result.event = line.slice(6).trim();
        if (line.startsWith("data:")) {
          const value = line.slice(5).trim();
          if (value) result.data += (result.data ? "\n" : "") + value;
        }
      }
      return result;
    }

    // Send & Cancel Logic
    function rollbackFailedUserMessage() {
      // A failed send leaves a user bubble in the DOM for context, but it must not
      // enter the persisted session: otherwise the next multi-turn request would
      // send a ghost user turn and "edit last" would target a failed message.
      const sess = state.sessions.find(s => s.id === state.currentSessionId);
      if (!sess || !sess.messages.length) return;
      const last = sess.messages[sess.messages.length - 1];
      if (!last || last.role !== "user") return;
      sess.messages.pop();
      if (!sess.messages.length) {
        state.sessions = state.sessions.filter(s => s.id !== sess.id);
        if (state.currentSessionId === sess.id) state.currentSessionId = "";
      }
      saveLocalSessions();
      renderSessionsList();
    }

    async function cancelActiveGeneration() {
      if (!state.activeRequest) return;
      if (!state.activeRequest.assistantMessageId) {
        // The status event has not arrived yet (files still uploading or the
        // upstream chat was never created): nothing to stop server-side, so
        // abort the local stream immediately.
        state.activeRequest.cancelRequested = true;
        state.activeRequest.controller.abort();
        updateStatusUI("已停止请求（尚未建立上游会话）", "standby");
        window.showToast("已停止请求");
        return;
      }
      state.activeRequest.cancelRequested = true;
      btnStopActive.disabled = true;
      updateStatusUI("正在停止上游生成...", "standby");
      try {
        const interruptedChatId = state.activeRequest.chatId || "";
        const res = await fetchWithTimeout("/api/chat/cancel", {
          method: "POST",
          headers: profileHeaders(state.activeRequest.profileId, { "Content-Type": "application/json" }),
          body: JSON.stringify({
            assistant_message_id: state.activeRequest.assistantMessageId,
            chat_id: interruptedChatId,
            profile_id: state.activeRequest.profileId || ""
          })
        }, UPSTREAM_CONTROL_FETCH_TIMEOUT_MS);
        const data = await res.json().catch(() => ({}));
        if (!res.ok || !data.ok) throw new Error(data.error?.message || `HTTP ${res.status}`);
        if (data.chat_delete_pending || data.chat_deleted) {
          // The cancel endpoint queues deletion after stopping the upstream
          // task.  Clear the local pointer immediately so a follow-up request
          // cannot accidentally reuse the interrupted chat while that job is
          // still running.
          state.activeRequest.chatDeleted = true;
          state.activeRequest.chatId = "";
          state.currentChatId = "";
          state.lastUserMessage = "";
          state.pendingEdit = false;
          updateSessionUI();
        }
        state.activeRequest.controller.abort();
        if (data.upstream_stopped === false) {
          updateStatusUI("停止请求未获上游确认，已安排删除会话", "standby");
          window.showToast("上游停止未确认，已断开本地流并安排删除会话", "standby");
        } else {
          window.showToast("已停止生成", "success");
        }
      } catch (err) {
        // Even if the upstream stop call fails, break the local stream so the UI
        // never hangs; the upstream turn may continue briefly but is no longer consumed.
        try { state.activeRequest.controller.abort(); } catch (_) {}
        state.currentChatId = "";
        state.lastUserMessage = "";
        state.pendingEdit = false;
        updateSessionUI();
        window.showToast(`停止请求异常: ${err.message}，已断开本地流`, "error");
      }
    }

    async function executeSend(text, filesToSend = []) {
      if (filesToSend.length > completionFileLimit()) {
        window.showToast(`Z.ai 单次最多 ${completionFileLimit()} 个附件`, "error");
        return;
      }
      const deleteChat = autoDeleteToggle.checked;
      const mode = deleteChat
        ? "new"
        : (state.pendingEdit && state.currentChatId
          ? "edit"
          : (reuseChatToggle.checked && state.currentChatId ? "continue" : "new"));

      // Multi-turn history is always sent: new chats embed it into messages,
      // while continue/edit use it only as fallback data when the reused
      // upstream chat no longer exists (server-side degrade retry).
      const activeSess = state.sessions.find(s => s.id === state.currentSessionId);
      const sessionProfileId = mode === "new" ? "" : (activeSess?.profileId || state.currentChatProfileId || "");
      const histMsgs = activeSess?.messages || [];
      let history = histMsgs.slice(-20).map(m => {
        const attachNames = (m.attachments || []).map(a => a.name || "").filter(Boolean);
        let content = String(m.content || "").trim();
        if (attachNames.length) {
          content = `[附件: ${attachNames.join(", ")}]\n${content}`;
        }
        if (m.role === "assistant" && m.incomplete) {
          content = `[上一条助手回复因流中断，仅保留部分内容]\n${content}`;
        }
        return {
          role: m.role === "user" ? "user" : "assistant",
          content: content.slice(0, 8000)
        };
      }).filter(m => m.content.trim());

      const requestPayload = {
        model: modelSelect.value,
        auto_web_search: webSearchToggle.checked,
        enable_thinking: thinkingToggle.checked,
        reasoning_effort: reasoningEffortSelect.value,
        include_thinking: includeThinkingToggle.checked,
        delete_chat_after_completion: deleteChat,
        mode,
        chat_id: mode === "new" ? "" : state.currentChatId,
        history
      };

      const requestState = {
        controller: new AbortController(),
        profileId: sessionProfileId,
        assistantMessageId: "",
        chatId: mode === "new" ? "" : (state.currentChatId || ""),
        canStop: false,
        cancelRequested: false,
        chatDeleted: false,
        succeeded: false
      };
      state.activeRequest = requestState;

      // Update UI to Generating state
      btnSubmit.className = "btn-send btn-stop";
      btnSubmitText.textContent = "停止";
      btnStopActive.disabled = false;
      promptInput.disabled = true;
      updateStatusUI(mode === "edit" ? "重新编辑发送中..." : "GLM 推理生成中...", "standby");

      // Render User Bubble
      const userText = text || "请分析附件。";
      state.currentChatProfileId = sessionProfileId || state.activeProfileId || "";
      appendMessageNode("user", userText, { attachments: filesToSend });
      recordCurrentSession(userText, "user", userText, "", filesToSend);

      // Render Empty Assistant Bubble
      const assistantNode = appendMessageNode("assistant", "", { model: modelSelect.value });
      const contentEl = assistantNode.querySelector(".msg-content");
      const thinkingContainer = assistantNode.querySelector(".thinking-container");

      let rawContentText = "";
      let rawThinkingText = "";
      let thinkingBoxEl = null;
      let terminalEventReceived = false;
      const STREAM_MARKDOWN_MAX_CHARS = 250000;

      function renderStreamContent() {
        if (!rawContentText) return;
        const plain = rawContentText.length > STREAM_MARKDOWN_MAX_CHARS;
        contentEl.classList.toggle("stream-plain", plain);
        if (plain) {
          contentEl.textContent = rawContentText;
        } else {
          contentEl.innerHTML = renderMarkdown(rawContentText);
        }
      }

      // High-performance streaming throttler (requestAnimationFrame).
      let isRenderScheduled = false;
      let pendingStreamRender = false;
      let lastStreamRenderAt = 0;
      function scheduleStreamRender() {
        pendingStreamRender = true;
        if (isRenderScheduled) return;
        isRenderScheduled = true;
        requestAnimationFrame(() => {
          isRenderScheduled = false;
          const renderChars = Math.max(rawContentText.length, rawThinkingText.length);
          const minGapMs = renderChars > 1000000 ? 1200 : renderChars > 250000 ? 600 : renderChars > 30000 ? 200 : 0;
          const now = performance.now();
          if (now - lastStreamRenderAt < minGapMs) {
            if (pendingStreamRender) scheduleStreamRender();
            return;
          }
          pendingStreamRender = false;
          lastStreamRenderAt = now;
          if (rawThinkingText && thinkingBoxEl) {
            thinkingBoxEl.querySelector(".thinking-body").textContent = rawThinkingText;
          }
          renderStreamContent();
          messageStream.scrollTop = messageStream.scrollHeight;
        });
      }

      let uploadedZaiFiles = [];
      try {
        const uploaded = await uploadAllFiles(
          filesToSend,
          requestState.profileId,
          requestState.controller.signal
        );
        uploadedZaiFiles = uploaded.files;
        requestState.profileId = uploaded.profileId || requestState.profileId || "";
        state.selectedFiles = [];
        updateFilePreview();

        const res = await fetch("/api/chat", {
          method: "POST",
          headers: profileHeaders(requestState.profileId, { "Content-Type": "application/json" }),
          body: JSON.stringify({
            message: text || "请分析附件。",
            stream: true,
            files: uploadedZaiFiles,
            ...requestPayload
          }),
          signal: requestState.controller.signal
        });

        if (!res.ok || !res.body) {
          const errText = await res.text();
          let errMsg = errText || `HTTP ${res.status}`;
          let errCode = "";
          let errScope = "";
          try {
            const errJson = JSON.parse(errText);
            errMsg = errJson?.error?.message || errJson?.message || errMsg;
            errCode = errJson?.error?.code || errJson?.error?.type || "";
            errScope = errJson?.error?.scope || "";
          } catch (_) { /* keep raw body text */ }
          const err = new Error(errMsg);
          err.code = errCode;
          err.scope = errScope;
          throw err;
        }

        const reader = res.body.getReader();
        const decoder = new TextDecoder("utf-8");
        let buffer = "";

        function handleSseBlock(block) {
          const sseEvent = parseSse(block);
          let data = {};
          if (sseEvent.data) {
            try {
              data = JSON.parse(sseEvent.data);
            } catch (parseErr) {
              throw new Error(`SSE 数据解析失败: ${parseErr.message}`);
            }
          }

          if (sseEvent.event === "status") {
            requestState.assistantMessageId = data.assistant_message_id || requestState.assistantMessageId;
            if (data.chat_id) requestState.chatId = data.chat_id;
            if (data.profile_id) bindCurrentSessionProfile(data.profile_id, requestState);
            updateStatusUI(data.message || "处理中...", "standby");
          } else if (sseEvent.event === "context") {
            requestState.assistantMessageId = data.assistant_message_id || requestState.assistantMessageId;
            if (data.chat_id) requestState.chatId = data.chat_id;
            if (data.profile_id) bindCurrentSessionProfile(data.profile_id, requestState);
            state.currentChatId = data.chat_id || state.currentChatId;
            updateSessionUI();
          } else if (sseEvent.event === "delta") {
            requestState.canStop = Boolean(requestState.assistantMessageId);

            if (data.phase === "thinking" || (data.delta && !data.phase && includeThinkingToggle.checked && !rawContentText)) {
              rawThinkingText += data.delta || "";
              if (!thinkingBoxEl && rawThinkingText.trim()) {
                thinkingBoxEl = document.createElement("div");
                thinkingBoxEl.className = "thinking-box";
                thinkingBoxEl.innerHTML = `
                  <div class="thinking-header">
                    <span>思考过程 (Thinking)</span>
                    <span class="thinking-toggle-icon">▾ 折叠</span>
                  </div>
                  <div class="thinking-body"></div>
                `;
                thinkingContainer.appendChild(thinkingBoxEl);
                thinkingBoxEl.querySelector(".thinking-header").addEventListener("click", () => {
                  thinkingBoxEl.classList.toggle("collapsed");
                  const isCol = thinkingBoxEl.classList.contains("collapsed");
                  thinkingBoxEl.querySelector(".thinking-toggle-icon").textContent = isCol ? "▸ 展开" : "▾ 折叠";
                });
              }
              scheduleStreamRender();
            } else {
              rawContentText += data.delta || "";
              scheduleStreamRender();
            }
          } else if (sseEvent.event === "error") {
            throw new Error(data.message || "上游服务返回异常");
          } else if (sseEvent.event === "done") {
            terminalEventReceived = true;
            // Finalize render
            if (rawThinkingText && thinkingBoxEl) {
              thinkingBoxEl.querySelector(".thinking-body").textContent = rawThinkingText;
            }
            renderStreamContent();
            messageStream.scrollTop = messageStream.scrollHeight;

            requestState.succeeded = true;
            if (data.chat_delete_pending) {
              // 自动删除已转后台执行：视为已处理，避免会话被复用到残留 chat 上
              requestState.chatDeleted = true;
              appendMessageNode("system", "上游会话自动删除已在后台执行（日志页可查看结果）。");
            } else {
              requestState.chatDeleted = Boolean(data.chat_deleted);
            }
            if (requestState.chatDeleted) {
              state.currentChatId = "";
              state.lastUserMessage = "";
              state.pendingEdit = false;
              if (!data.chat_delete_pending) {
                appendMessageNode("system", "本次上游会话已按设置自动删除，未在官方后台留存。");
              }
            } else {
              state.currentChatId = data.chat_id || state.currentChatId;
              if (data.chat_id) requestState.chatId = data.chat_id;
              if (data.profile_id) bindCurrentSessionProfile(data.profile_id, requestState);
              if (data.chat_delete_error) {
                appendMessageNode("system", `自动删除会话失败: ${data.chat_delete_error}`);
              }
            }
            updateSessionUI();
            updateStatusUI("已连接", "connected");

            // Save to local session
            recordCurrentSession(userText, "assistant", rawContentText, rawThinkingText);
          }
        }

        while (true) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });

          let splitIndex;
          while ((splitIndex = buffer.indexOf("\n\n")) !== -1) {
            const block = buffer.slice(0, splitIndex);
            buffer = buffer.slice(splitIndex + 2);
            if (!block.trim()) continue;
            handleSseBlock(block);
          }
        }

        // Flush any trailing SSE block that arrived without a final blank line
        buffer += decoder.decode();
        if (buffer.trim()) {
          handleSseBlock(buffer.trim());
        }

        if (!terminalEventReceived) {
          const incomplete = new Error(
            requestState.cancelRequested ? "已停止生成" : "流式响应在完成事件前中断"
          );
          incomplete.code = "stream_incomplete";
          if (requestState.cancelRequested) incomplete.name = "AbortError";
          throw incomplete;
        }

        if (!rawContentText.trim() && !rawThinkingText.trim()) {
          contentEl.textContent = "（上游返回空内容）";
        }
      } catch (err) {
        if (Array.isArray(err?.uploadedFiles)) uploadedZaiFiles = err.uploadedFiles;
        if (err?.profileId) requestState.profileId = err.profileId;
        if (requestState.cancelRequested && err?.name === "AbortError") {
          requestState.succeeded = true;
          updateStatusUI("已停止生成", "standby");
          appendMessageNode(
            "system",
            requestState.chatDeleted
              ? "已停止上游生成，中断的上游会话已安排删除，保留已输出的解答。"
              : "已停止上游生成，保留已输出的解答。"
          );
          recordCurrentSession(userText, "assistant", rawContentText, rawThinkingText);
        } else {
          const hasPartialOutput = Boolean(rawContentText.trim() || rawThinkingText.trim());
          if (hasPartialOutput) {
            if (rawThinkingText && thinkingBoxEl) {
              thinkingBoxEl.querySelector(".thinking-body").textContent = rawThinkingText;
            }
            renderStreamContent();
            appendMessageNode("system", `流式响应中断：${err.message}。以上仅为已收到的部分内容。`);
            recordCurrentSession(
              userText,
              "assistant",
              rawContentText,
              rawThinkingText,
              [],
              { incomplete: true }
            );
          } else {
            contentEl.textContent = `请求失败: ${err.message}`;
            contentEl.style.color = "var(--rose-core)";
            rollbackFailedUserMessage();
          }
          updateStatusUI("请求失败", "error");
          window.showToast(err.message, "error");
          if (!hasPartialOutput && err?.code === "chat_slot_busy") {
            // Keep the user's input so they can resend once capacity returns.
            promptInput.value = text || "";
            promptInput.style.height = "auto";
            promptInput.style.height = `${Math.min(promptInput.scrollHeight, 220)}px`;
            updateStatusUI(
              err?.scope === "profile"
                ? "当前会话所属账号已满；为避免串号不会自动切换账号"
                : "账号池暂时满载（每个账号 3 个槽位），稍后将自动接入",
              "standby"
            );
          } else if (!hasPartialOutput && err?.code === "profile_not_found") {
            promptInput.value = text || "";
            promptInput.style.height = "auto";
            promptInput.style.height = `${Math.min(promptInput.scrollHeight, 220)}px`;
            state.currentChatProfileId = state.activeProfileId || "";
            updateStatusUI("原会话所属账号已删除；内容已保留，请重新发送为新会话", "standby");
          }
          // The upstream chat created for this attempt is cleaned up server-side
          // on failure; drop the stale id so the UI never points at a dead chat.
          state.currentChatId = "";
          updateSessionUI();
        }
        // Files uploaded before the failure never reached a completed chat and
        // would stay orphaned on Z.ai; ask the proxy to delete them best-effort.
        const orphanIds = uploadedZaiFiles.map(f => f && (f.id || f.file?.id)).filter(Boolean);
        if (orphanIds.length) {
          fetchWithTimeout("/api/files/cleanup", {
            method: "POST",
            headers: profileHeaders(requestState.profileId, { "Content-Type": "application/json" }),
            body: JSON.stringify({ files: orphanIds, profile_id: requestState.profileId || "" })
          }, MANAGEMENT_FETCH_TIMEOUT_MS).catch(() => {});
        }
      } finally {
        if (state.activeRequest === requestState) {
          state.activeRequest = null;
        }
        btnSubmit.className = "btn-send";
        btnSubmitText.textContent = "发送";
        btnStopActive.disabled = true;
        promptInput.disabled = false;
        if (requestState.succeeded && !requestState.chatDeleted) {
          state.lastUserMessage = text || "请分析附件。";
        }
        state.pendingEdit = false;
        promptInput.focus();
      }
    }

    // Composer Form Submit
    composerForm.addEventListener("submit", (e) => {
      e.preventDefault();
      if (state.activeRequest) {
        cancelActiveGeneration();
        return;
      }
      const text = promptInput.value.trim();
      const files = [...state.selectedFiles];
      if (!text && !files.length) return;
      if (files.length > completionFileLimit()) {
        window.showToast(`Z.ai 单次最多 ${completionFileLimit()} 个附件`, "error");
        return;
      }
      promptInput.value = "";
      promptInput.style.height = "auto";
      promptInput.style.height = `${Math.min(promptInput.scrollHeight, 220)}px`;
      executeSend(text, files);
    });

    // Auto resize textarea & shortcut key
    promptInput.addEventListener("input", () => {
      promptInput.style.height = "auto";
      promptInput.style.height = `${Math.min(promptInput.scrollHeight, 220)}px`;
    });

    promptInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && e.ctrlKey) {
        e.preventDefault();
        composerForm.requestSubmit();
      }
    });

    // Paste images/files from the clipboard into the attachment bar
    promptInput.addEventListener("paste", (e) => {
      const items = e.clipboardData?.items || [];
      const pasted = [];
      for (const item of items) {
        if (item.kind !== "file") continue;
        const file = item.getAsFile();
        if (file) pasted.push(file);
      }
      if (!pasted.length) return;
      e.preventDefault();
      const result = appendSelectedFiles(pasted);
      if (!result.accepted && result.duplicates && !result.overflow) {
        window.showToast("剪贴板中的文件已在附件列表中", "standby");
        return;
      }
      if (result.accepted) window.showToast(`已粘贴 ${result.accepted} 个文件到附件`);
    });

    // Button Stop Active
    btnStopActive.addEventListener("click", cancelActiveGeneration);

    // Button Clear Chat
    btnClearChat.addEventListener("click", () => {
      messageStream.replaceChildren();
      appendMessageNode("system", "聊天画面已清屏。");
      window.showToast("已清空聊天记录");
    });

    // Button Export Markdown
    btnExportChat.addEventListener("click", () => {
      const messages = messageStream.querySelectorAll(".msg-wrapper");
      if (!messages.length) {
        window.showToast("当前没有可导出的对话", "error");
        return;
      }
      let md = `# GLM Local Proxy Chat Export\n\n*导出时间: ${new Date().toLocaleString()}*\n\n---\n\n`;
      messages.forEach(msg => {
        if (msg.classList.contains("user")) {
          const content = msg.querySelector(".msg-content")?.innerText || "";
          md += `### User\n\n${content}\n\n`;
        } else if (msg.classList.contains("assistant")) {
          const content = msg.querySelector(".msg-content")?.innerText || "";
          md += `### Assistant (${modelSelect.value})\n\n${content}\n\n`;
        }
      });
      const blob = new Blob([md], { type: "text/markdown;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `glm-chat-${Date.now()}.md`;
      a.click();
      URL.revokeObjectURL(url);
      window.showToast("已导出 Markdown 文件", "success");
    });

    // Save current controls as local default settings
    btnSaveSettings.addEventListener("click", async () => {
      btnSaveSettings.disabled = true;
      try {
        const res = await fetchWithTimeout("/api/settings", {
          method: "POST",
          headers: apiHeaders({ "Content-Type": "application/json" }),
          body: JSON.stringify({
            settings: {
              model: modelSelect.value,
              auto_web_search: webSearchToggle.checked,
              enable_thinking: thinkingToggle.checked,
              reasoning_effort: reasoningEffortSelect.value,
              include_thinking: includeThinkingToggle.checked,
              delete_chat_after_completion: autoDeleteToggle.checked,
              upstream_timeout_sec: Number(upstreamTimeoutInput.value) || 300,
              upstream_retry_wait_sec: Number(upstreamRetryWaitInput.value) || 0,
              upstream_retry_max_attempts: Number(upstreamRetryAttemptsInput.value) || 3,
              history_max_records: Number(historyMaxRecordsInput.value) || 300
            }
          })
        }, MANAGEMENT_FETCH_TIMEOUT_MS);
        const data = await res.json();
        if (!res.ok || !data.ok) {
          const retryAfter = Number(res.headers.get("Retry-After")) || 0;
          const suffix = res.status === 429 && retryAfter > 0 ? `（约 ${retryAfter} 秒后可重试）` : "";
          throw new Error((data.error?.message || `HTTP ${res.status}`) + suffix);
        }
        state.statusDefaultsApplied = false;
        await fetchStatus();
        window.showToast(data.message || "默认设置已保存，下次启动自动生效", "success");
      } catch (err) {
        settingsStoreStatus.className = "status-banner error";
        settingsStoreStatus.textContent = `默认设置未保存: ${err.message}`;
        window.showToast(`保存默认设置失败: ${err.message}`, "error");
      } finally {
        btnSaveSettings.disabled = false;
      }
    });

    // Button New Session
    btnNewSession.addEventListener("click", startNewChatSession);
    btnSidebarNew.addEventListener("click", startNewChatSession);

    // Button Edit Last
    btnEditLast.addEventListener("click", () => {
      if (state.activeRequest) {
        window.showToast("正在生成中，请先停止再编辑", "error");
        return;
      }
      if (!state.lastUserMessage) {
        window.showToast("尚无已发送的用户消息可编辑", "error");
        return;
      }
      promptInput.value = state.lastUserMessage;
      state.pendingEdit = true;
      promptInput.focus();
      promptInput.dispatchEvent(new Event("input"));
      window.showToast("已将上一条消息填回输入框（编辑重发模式）");
    });

    // Button Delete Upstream Chat
    btnDeleteUpstream.addEventListener("click", async () => {
      if (!state.currentChatId || state.activeRequest) return;
      const targetId = state.currentChatId;
      if (!confirm(`确认从 Z.ai 上游彻底删除对话 ${targetId}？此操作不可恢复。`)) return;

      btnDeleteUpstream.disabled = true;
      updateStatusUI("正在删除上游会话...", "standby");
      try {
        const res = await fetchWithTimeout("/api/chat/delete", {
          method: "POST",
          headers: profileHeaders(state.currentChatProfileId, { "Content-Type": "application/json" }),
          body: JSON.stringify({ chat_id: targetId, profile_id: state.currentChatProfileId || "" })
        }, UPSTREAM_CONTROL_FETCH_TIMEOUT_MS);
        const data = await res.json().catch(() => ({}));
        if (!res.ok || !data.ok) throw new Error(data.error?.message || `HTTP ${res.status}`);

        state.currentChatId = "";
        state.lastUserMessage = "";
        state.pendingEdit = false;
        updateSessionUI();
        appendMessageNode("system", "上游对话已被彻底删除，本地会话已重置。");
        updateStatusUI("已连接", "connected");
        window.showToast("上游对话已成功删除", "success");
      } catch (err) {
        updateStatusUI("删除失败", "error");
        window.showToast(`删除失败: ${err.message}`, "error");
      } finally {
        btnDeleteUpstream.disabled = !state.currentChatId;
      }
    });

    // Model Change Listener
    modelSelect.addEventListener("change", () => {
      modelPillText.textContent = `model: ${modelSelect.value}`;
      $("info-model").textContent = modelSelect.value;
      renderDashboard(state.lastStatus);
      syncOptionStates();
    });

    thinkingToggle.addEventListener("change", syncOptionStates);
    autoDeleteToggle.addEventListener("change", syncOptionStates);
    reasoningEffortSelect.addEventListener("change", rememberCurrentEffort);

    // 会话抽屉展开/收起
    $("btn-sessions-toggle").addEventListener("click", () => {
      $("chat-shell").classList.toggle("collapsed");
    });

    // 设置页「前往调整」跳到对话页
    const settingsModelJump = $("settings-model-jump");
    if (settingsModelJump) {
      settingsModelJump.addEventListener("click", () => navigateTo("chat"));
    }

    // 工具条 chip 开关联动隐藏 checkbox
    bindChip("chip-thinking", thinkingToggle);
    bindChip("chip-search", webSearchToggle);
    bindChip("chip-include-thinking", includeThinkingToggle);
    bindChip("chip-auto-delete", autoDeleteToggle);
